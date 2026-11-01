"""Detect a broken page layout so a redesign never produces fake items or silent gaps."""

from dataclasses import dataclass, field

from spidercrawler.index_parser import EXPECTED_TABLES, ParseResult


@dataclass(frozen=True)
class Health:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def check_health(result: ParseResult, known_items: int) -> Health:
    """Broken if an expected table is missing, or the page has no rows when it used to."""
    reasons = [f"missing table: {t}" for t in sorted(EXPECTED_TABLES - result.tables_found)]
    if not result.rows and known_items > 0:
        reasons.append(f"no rows parsed, but {known_items} items were seen before")
    return Health(ok=not reasons, reasons=reasons)
