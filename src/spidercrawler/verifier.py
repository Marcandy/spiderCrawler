"""Deterministic checks on an extraction (PLAN.md section 5, verifier rules 1-8).

The verifier proves quotes exist and that dates and rule ids agree with data the LLM did not
produce. It cannot judge whether a claim is *correct*; that is the human reviewer's job.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from spidercrawler.canonical import parse_date, rule_ids
from spidercrawler.normalize import for_matching
from spidercrawler.schema import Canonical, ChangeSummary

MIN_WORDS = {"rule_ids": 3, "effective_date": 3}
DEFAULT_MIN_WORDS = 8
MAX_SUMMARY_WORDS = 60

# Rule 6: advice or eligibility phrasing. A narrow net, not a guarantee.
_ADVICE = re.compile(
    r"\b(you\s+(should|must|can\s+now|may\s+now|are\s+(now\s+)?eligible|qualify|need\s+to)"
    r"|(now|newly)\s+(qualify|qualifies|eligible)"
    r"|are\s+now\s+eligible|is\s+now\s+eligible|can\s+now\s+(get|have|file|petition|seek)"
    r"|we\s+recommend|it\s+is\s+advisable)\b",
    re.IGNORECASE,
)
# Rule 7: instruction-like text in the *source*. Flags for the reviewer; never blocks alone.
_SUSPICIOUS = re.compile(
    r"\b(ignore\s+(all\s+|any\s+)?(previous|prior|above)\s+instructions?"
    r"|you\s+are\s+(an?\s+)?(ai|language\s+model|assistant)"
    r"|system\s+prompt|disregard\s+(the\s+)?(above|previous)"
    r"|new\s+instructions?:)",
    re.IGNORECASE,
)
_DELETION = re.compile(r"\[[^\[\]]*\]")

STAGE_FOR_TABLE = {"approved": "adopted", "proposed": "proposed", "prior_proposed": "proposed"}


@dataclass(frozen=True)
class RowContext:
    """What the committee page itself says about the item (rule 8 cross-check)."""

    table: str
    title: str
    date_literal: str | None
    rule_prefix: str


@dataclass(frozen=True)
class VerifierResult:
    status: Literal["pass", "pass_flagged", "fail"]
    reasons: list[str] = field(default_factory=list)  # why it failed
    flags: list[str] = field(default_factory=list)  # for the reviewer's attention


def _words(text: str) -> int:
    return len(text.split())


def verify(
    summary: ChangeSummary,
    canonical: Canonical,
    source_text: str,
    row: RowContext | None = None,
    extra_flags: list[str] | None = None,
) -> VerifierResult:
    reasons: list[str] = []
    flags: list[str] = list(extra_flags or [])
    source = for_matching(source_text)
    without_deletions = for_matching(_DELETION.sub(" ", source_text))
    by_field: dict[str, list[str]] = {}
    for claim in summary.claims:
        by_field.setdefault(claim.field, []).append(claim.evidence_quote)

    # Rule 1: every non-empty field has a claim.
    required = {
        "summary": True,
        "rule_ids": bool(summary.rule_ids),
        "effective_date": bool(summary.effective_date_text),
        "affected_parties": bool(summary.affected_parties),
        "expungement_relevance": summary.expungement_relevance == "yes",
    }
    reasons += [
        f"rule 1: no claim for {name}"
        for name, need in required.items()
        if need and name not in by_field
    ]

    # Rule 2: every quote is in the source and long enough.
    for claim in summary.claims:
        quote = for_matching(claim.evidence_quote)
        minimum = MIN_WORDS.get(claim.field, DEFAULT_MIN_WORDS)
        if _words(quote) < minimum:
            reasons.append(
                f"rule 2: {claim.field} quote under {minimum} words: {claim.evidence_quote!r}"
            )
        elif quote not in source:
            reasons.append(f"rule 2: {claim.field} quote not in source: {claim.evidence_quote!r}")
        elif quote not in without_deletions:
            flags.append(f"quote_from_deleted_text: {claim.evidence_quote!r}")

    # Rules 3 and 4: literals appear inside their own quotes (same matching as rule 2).
    if summary.effective_date_text and not any(
        for_matching(summary.effective_date_text) in for_matching(q)
        for q in by_field.get("effective_date", [])
    ):
        reasons.append(f"rule 3: effective date {summary.effective_date_text!r} not in its quotes")
    for literal in summary.rule_ids:
        if not any(for_matching(literal) in for_matching(q) for q in by_field.get("rule_ids", [])):
            reasons.append(f"rule 4: rule id {literal!r} not in its quotes")

    # Rule 5: abstaining needs a reason.
    if summary.expungement_relevance == "unsure" and not (summary.abstain_reason or "").strip():
        reasons.append("rule 5: relevance is unsure but abstain_reason is empty")

    # Rule 6: no advice, and the summary stays short.
    if match := _ADVICE.search(summary.summary):
        reasons.append(f"rule 6: advice phrasing in summary: {match.group(0)!r}")
    if _words(summary.summary) > MAX_SUMMARY_WORDS:
        reasons.append(f"rule 6: summary over {MAX_SUMMARY_WORDS} words")

    # Rule 7: instruction-like source text is flagged, never blocking on its own.
    if _SUSPICIOUS.search(source_text):
        flags.append("suspicious_input")

    # Rule 8: cross-check against the committee page row.
    if row is not None:
        row_date = parse_date(row.date_literal) if row.table == "approved" else None
        if row_date and canonical.effective_date and canonical.effective_date != row_date:
            reasons.append(f"rule 8: effective date {canonical.effective_date} != page {row_date}")
        missing = [r for r in rule_ids(row.title, row.rule_prefix) if r not in canonical.rule_ids]
        if missing:
            flags.append(f"rule_ids_missing_from_title: {', '.join(missing)}")
        if (expected := STAGE_FOR_TABLE.get(row.table)) and summary.stage != expected:
            flags.append(f"stage {summary.stage!r} but page table is {row.table!r}")

    status = "fail" if reasons else ("pass_flagged" if flags else "pass")
    return VerifierResult(status=status, reasons=reasons, flags=flags)
