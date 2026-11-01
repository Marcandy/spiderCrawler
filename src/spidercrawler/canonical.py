"""Turn literal rule citations and dates into canonical values (deterministic, no LLM)."""

import re
from datetime import date

from spidercrawler.normalize import normalize
from spidercrawler.schema import Canonical, ChangeSummary

# Spellings seen on the committee pages, mapped to one canonical prefix.
_PREFIX_ALIASES = {
    "Pa.Crim.P.": "Pa.R.Crim.P.",  # typo on the criminal committee page
    "Pa.R.C.P.": "Pa.R.Civ.P.",
    "Pa.R.C.P.M.D.J.": "Pa.R.Civ.P.M.D.J.",
}
_PREFIX = r"Pa\.\s?(?:Rs?\.)?(?:\s?(?!Nos?\.)[A-Z][A-Za-z]*\.)+"
_RULE_WORD = r"\bRules?\b"
_NUM = r"\d+(?:\.\d+)*(?:\s?-\s?\.?\d+(?:\.\d+)*)?"
_SEP = r"\s*(?:,\s*(?:(?i:and)\s+)?|(?i:and)\s+|&\s*)"
_CITATION = re.compile(
    rf"(?P<prefix>{_PREFIX}|{_RULE_WORD})\s*(?:Nos?\.\s*)?(?P<nums>{_NUM}(?:{_SEP}{_NUM})*)"
)
_ONE_NUM = re.compile(_NUM)

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}  # fmt: skip
_WORD_DATE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def canonical_prefix(prefix: str) -> str:
    compact = re.sub(r"\s+", "", prefix).replace("Pa.Rs.", "Pa.R.")
    return _PREFIX_ALIASES.get(compact, compact)


def rule_ids(text: str, default_prefix: str) -> list[str]:
    """All rule citations in `text`, canonical and deduplicated, in order of appearance.

    A bare "Rule 632" takes `default_prefix` (the committee's own rule set). Ranges such as
    "520-529" are kept as one id; both sides of any comparison use this same function.
    """
    found: list[str] = []
    for match in _CITATION.finditer(normalize(text)):
        raw = match.group("prefix")
        prefix = default_prefix if re.fullmatch(_RULE_WORD, raw) else canonical_prefix(raw)
        for num in _ONE_NUM.findall(match.group("nums")):
            rule = f"{prefix} {_expand_range(re.sub(r'\s+', '', num))}"
            if rule not in found:
                found.append(rule)
    return found


def _expand_range(num: str) -> str:
    """'520.1-.19' (shorthand on the site) -> '520.1-520.19', so both spellings match."""
    start, sep, end = num.partition("-")
    if sep and end.startswith("."):
        return f"{start}-{start.split('.')[0]}{end}"
    return num


def parse_date(text: str | None) -> date | None:
    """A single calendar date in `text`, or None (relative, missing, or ambiguous)."""
    if not text:
        return None
    dates: set[date] = set()
    for month, day, year in _WORD_DATE.findall(text):
        if (m := _MONTHS.get(month.lower())) is not None:
            dates |= _safe_date(int(year), m, int(day))
    for month, day, year in _NUMERIC_DATE.findall(text):
        dates |= _safe_date(int(year), int(month), int(day))
    return dates.pop() if len(dates) == 1 else None


def _safe_date(year: int, month: int, day: int) -> set[date]:
    try:
        return {date(year, month, day)}
    except ValueError:
        return set()


def canonicalize(summary: ChangeSummary, default_prefix: str) -> Canonical:
    ids: list[str] = []
    for literal in summary.rule_ids:
        for rule in rule_ids(literal, default_prefix) or [literal.strip()]:
            if rule not in ids:
                ids.append(rule)
    return Canonical(rule_ids=ids, effective_date=parse_date(summary.effective_date_text))
