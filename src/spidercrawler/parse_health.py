"""Detect a broken page layout so a redesign never produces fake items or silent gaps."""

from dataclasses import dataclass, field

from spidercrawler.config import Guards
from spidercrawler.index_parser import EXPECTED_TABLES, ParseResult

ACCEPT_HINT = "if expected, accept the page with `spidercrawler run --seed`"


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


def check_volume(
    rows: int, new_items: int, known_items: int, previous_rows: int | None, guards: Guards
) -> Health:
    """Broken on a flood of new items or a sharp drop in rows (PLAN.md section 6).

    A flood usually means re-keying (the site moved its PDFs, so every item looks new).
    A drop usually means a partial page load. Either way nothing should be recorded
    until a person has looked, because records are append-only.
    """
    reasons = []
    ratio_hit = known_items > 0 and new_items / known_items > guards.max_new_ratio
    if new_items > guards.max_new_items or ratio_hit:
        reasons.append(
            f"flood: {new_items} new items (limit {guards.max_new_items}, or "
            f"{guards.max_new_ratio:.0%} of {known_items} known); {ACCEPT_HINT}"
        )
    if previous_rows and rows < guards.min_rows_ratio * previous_rows:
        reasons.append(
            f"drop: {rows} rows, last good run saw {previous_rows} "
            f"(minimum {guards.min_rows_ratio:.0%}); {ACCEPT_HINT}"
        )
    return Health(ok=not reasons, reasons=reasons)
