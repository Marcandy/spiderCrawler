"""Queue routing and review decisions (PLAN.md section 5, routing).

Every path ends at a human. Nothing is approvable while blocked, and an edit is only
accepted if the edited output passes the verifier: editing cannot launder a blocked item.
"""

from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from spidercrawler.canonical import canonicalize
from spidercrawler.records import Extraction, QueueStatus, Review, utcnow
from spidercrawler.schema import ChangeSummary
from spidercrawler.verifier import RowContext, VerifierResult, verify


def route(verifier_status: str, confidence: str | None, relevance: str | None) -> QueueStatus:
    if verifier_status == "fail":
        return "blocked"
    if verifier_status in ("error", "pass_flagged"):
        return "needs_attention"
    if confidence == "low" or relevance == "unsure":
        return "needs_attention"
    return "ready_for_review"


class DecisionRefusedError(Exception):
    """The review decision is not allowed for this extraction."""


@dataclass(frozen=True)
class EditCheck:
    ok: bool
    result: VerifierResult | None
    canonical: dict | None
    reasons: list[str]


def check_edit(
    edited: dict, source_text: str, row: RowContext | None, rule_prefix: str
) -> EditCheck:
    """Run a reviewer's edited output through the same schema and verifier as the model's."""
    try:
        summary = ChangeSummary.model_validate(edited)
    except ValidationError as exc:
        return EditCheck(
            False, None, None, [f"schema: {e['msg']} at {e['loc']}" for e in exc.errors()]
        )
    canonical = canonicalize(summary, rule_prefix)
    result = verify(summary, canonical, source_text, row)
    return EditCheck(
        result.status != "fail", result, canonical.model_dump(mode="json"), result.reasons
    )


def decide(
    extraction: Extraction,
    decision: str,
    reviewer: str,
    *,
    note: str = "",
    edited: dict | None = None,
    source_text: str = "",
    row: RowContext | None = None,
    rule_prefix: str = "Pa.R.Crim.P.",
    now: datetime | None = None,
) -> Review:
    """Build a Review record, refusing decisions the rules do not allow."""
    if decision == "approve":
        if extraction.queue_status == "blocked" or extraction.output is None:
            raise DecisionRefusedError(
                "cannot approve a blocked or failed extraction; edit it so it passes the verifier"
            )
        edited_status, edited_notes = None, []
    elif decision == "edit":
        if edited is None:
            raise DecisionRefusedError("an edit needs the edited output")
        check = check_edit(edited, source_text, row, rule_prefix)
        if not check.ok:
            raise DecisionRefusedError(
                "edited output fails the verifier: " + "; ".join(check.reasons)
            )
        edited_status = check.result.status if check.result else None
        edited_notes = check.result.reasons + check.result.flags if check.result else []
    elif decision == "reject":
        edited_status, edited_notes = None, []
    else:
        raise DecisionRefusedError(f"unknown decision {decision!r}")

    decided_at = now or utcnow()
    return Review(
        id=Review.make_id(extraction.id, decided_at),
        extraction_id=extraction.id,
        reviewer=reviewer,
        decision=decision,  # type: ignore[arg-type]
        edited_output=edited if decision == "edit" else None,
        edited_verifier_status=edited_status,
        edited_verifier_notes=edited_notes,
        note=note,
        decided_at=decided_at,
    )


def is_publishable(reviews: list[Review]) -> bool:
    """Approved or passing-edit as the newest decision. Only these reach the digest."""
    if not reviews:
        return False
    latest = max(reviews, key=lambda r: r.decided_at)
    return latest.decision == "approve" or (
        latest.decision == "edit" and latest.edited_verifier_status in ("pass", "pass_flagged")
    )
