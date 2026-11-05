"""Monitoring status for the public digest: is the monitor running, and what is it seeing?

An empty digest is ambiguous: nothing changed, the monitor broke, or items await review.
This answers the target user's weekly question (PLAN.md section 2) with counts only, never
unreviewed content, so nothing reaches the public page without a human decision.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from spidercrawler.config import Source
from spidercrawler.pipeline import needs_documents
from spidercrawler.records import Extraction, Item, RecordStore, Review, Run
from spidercrawler.router import is_publishable

STALE_AFTER = timedelta(days=8)  # weekly schedule plus a day of slack
RECENT = timedelta(days=7)


@dataclass(frozen=True)
class MonitoringStatus:
    headline: str
    healthy: bool
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    last_ok_run_at: datetime | None = None
    monitoring_since: datetime | None = None
    sources_checked: int = 0
    sources_total: int = 0
    broken_sources: list[str] = field(default_factory=list)
    new_items_last_7_days: int = 0
    tracked_items: int = 0
    awaiting_extraction: int = 0
    awaiting_review: int = 0
    approved: int = 0
    rejected: int = 0

    def as_json(self) -> dict:
        data = asdict(self)
        for key in ("last_run_at", "last_ok_run_at", "monitoring_since"):
            data[key] = data[key].isoformat() if data[key] else None
        return data


def _day(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d")


def monitoring_status(
    records: RecordStore, sources: list[Source], now: datetime
) -> MonitoringStatus:
    runs = sorted(records.iter(Run), key=lambda r: r.started_at)
    if not runs:
        return MonitoringStatus(
            headline="Monitoring has not run yet.", healthy=False, sources_total=len(sources)
        )

    last = runs[-1]
    ok_runs = [r for r in runs if r.status == "ok"]
    last_ok = ok_runs[-1] if ok_runs else None
    run_started = {r.id: r.started_at for r in runs}
    by_id = {s.id: s for s in sources}

    # Items detected as new by a live run (not recorded as history by a seed run) that the
    # pipeline would send for extraction: the criminal-rule items the digest is about.
    tracked = [i for i in records.iter(Item) if needs_documents(i, by_id.get(i.source_id))]
    recent = [
        i
        for i in tracked
        if (started := run_started.get(i.first_seen_run)) is not None and now - started <= RECENT
    ]

    newest: dict[str, Extraction] = {}
    for ext in records.iter(Extraction):
        if ext.item_id not in newest or ext.created_at > newest[ext.item_id].created_at:
            newest[ext.item_id] = ext
    reviews: dict[str, list[Review]] = {}
    for review in records.iter(Review):
        reviews.setdefault(review.extraction_id, []).append(review)

    awaiting_extraction = awaiting_review = approved = rejected = 0
    for item in tracked:
        ext = newest.get(item.id)
        if ext is None:
            awaiting_extraction += 1
            continue
        decisions = reviews.get(ext.id, [])
        if not decisions:
            awaiting_review += 1
        elif is_publishable(decisions):
            approved += 1
        else:
            rejected += 1

    stale = last_ok is None or now - last_ok.started_at > STALE_AFTER
    if last_ok is None:
        headline = "No check has succeeded yet."
    elif stale:
        headline = f"Monitoring is stale: the last successful check was {_day(last_ok.started_at)}."
    elif last.status != "ok":
        broken = ", ".join(last.sources_broken) or "a source could not be reached"
        headline = (
            f"The latest check on {_day(last.started_at)} failed ({broken}); "
            "results may be incomplete."
        )
    else:
        headline = (
            f"Checked {_day(last.started_at)}: {len(recent)} new criminal-rule "
            f"item(s) in the last 7 days."
        )
    if awaiting_extraction or awaiting_review:
        headline += f" {awaiting_extraction + awaiting_review} awaiting human review."

    return MonitoringStatus(
        headline=headline,
        healthy=not stale and last.status == "ok",
        last_run_at=last.started_at,
        last_run_status=last.status,
        last_ok_run_at=last_ok.started_at if last_ok else None,
        monitoring_since=runs[0].started_at,
        sources_checked=last.sources_checked,
        sources_total=len(sources),
        broken_sources=list(last.sources_broken),
        new_items_last_7_days=len(recent),
        tracked_items=len(tracked),
        awaiting_extraction=awaiting_extraction,
        awaiting_review=awaiting_review,
        approved=approved,
        rejected=rejected,
    )
