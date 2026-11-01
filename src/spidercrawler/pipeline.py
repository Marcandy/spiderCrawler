"""One run end to end: fetch → parse → health → detect → PDF text → run record.

The extractor is added in Day 2. A failing source never stops the run; a broken source
makes the run status `failed` so the scheduled job alerts.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime

from spidercrawler.config import Config, Source
from spidercrawler.detector import KnownItem, detect, load_known
from spidercrawler.fetcher import Fetcher, FetchError, RequestCapError
from spidercrawler.index_parser import parse_committee_page
from spidercrawler.normalize import content_hash
from spidercrawler.parse_health import check_health
from spidercrawler.pdf_text import PdfError, extract_pdf_text
from spidercrawler.records import (
    Document,
    Item,
    RecordStore,
    Run,
    utcnow,
)

# Context committees' items are only worth reading if they touch criminal rules.
_CRIMINAL_REF = re.compile(r"Crim\.?\s*P|Criminal", re.IGNORECASE)


def needs_documents(item: Item, source: Source | None) -> bool:
    if item.seeded:
        return False
    if source is not None and source.role == "focus":
        return True
    return bool(_CRIMINAL_REF.search(item.title))


def run_pipeline(
    config: Config,
    records: RecordStore,
    fetcher: Fetcher,
    seed: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Run:
    started = now()
    run_id = Run.make_id(started)
    known: dict[str, KnownItem] = load_known(records)
    previous = records.latest_run()
    validators = dict(previous.page_validators) if previous else {}
    errors: list[str] = []
    broken: list[str] = []
    checked = 0
    found = 0
    cap_hit = False

    for source in config.sources:
        try:
            page = fetcher.get(config.source_url(source), validators.get(source.id))
        except RequestCapError as exc:
            errors.append(f"{source.id}: {exc}; remaining sources skipped")
            cap_hit = True
            break
        except FetchError as exc:
            errors.append(f"{source.id}: {exc}")
            continue
        checked += 1
        if page.not_modified:
            continue

        parsed = parse_committee_page(page.content.decode("utf-8", "replace"), page.url)
        known_here = sum(1 for k in known.values() if k.item.source_id == source.id)
        health = check_health(parsed, known_here)
        if not health.ok:
            broken.append(source.id)
            errors.extend(f"{source.id}: broken: {reason}" for reason in health.reasons)
            continue  # keep old validators so the page is re-read next run

        detection = detect(source.id, parsed.rows, known, run_id, seed)
        for item in detection.new_items:
            records.write(item)
            known[item.item_key] = KnownItem(item, item.table)
        for event in detection.events:
            records.write(event)
        errors.extend(
            f"{source.id}: row without PDF skipped: {r.title}" for r in detection.skipped_rows
        )
        found += len(detection.new_items)
        validators[source.id] = page.validators

    documents = 0 if cap_hit else _fetch_documents(config, records, fetcher, errors)

    run = Run(
        id=run_id,
        started_at=started,
        finished_at=now(),
        seed=seed,
        sources_checked=checked,
        sources_broken=broken,
        items_found=found,
        documents_fetched=documents,
        requests_made=fetcher.requests_made,
        errors=errors,
        page_validators=validators,
        status="failed" if broken else "ok",
    )
    records.write(run)
    return run


def _fetch_documents(
    config: Config, records: RecordStore, fetcher: Fetcher, errors: list[str]
) -> int:
    """Fetch PDFs for items that need them and lack them. Leftovers wait for the next run."""
    sources = {s.id: s for s in config.sources}
    have = {(d.item_id, d.url) for d in records.iter(Document)}
    fetched = 0
    for item in sorted(records.iter(Item), key=lambda i: i.first_seen_run):
        if not needs_documents(item, sources.get(item.source_id)):
            continue
        for url in item.doc_urls:
            if (item.id, url) in have:
                continue
            try:
                result = fetcher.get(url)
                pdf = extract_pdf_text(result.content)
            except RequestCapError as exc:
                errors.append(f"documents: {exc}; rest wait for next run")
                return fetched
            except (FetchError, PdfError) as exc:
                errors.append(f"{item.id}: {exc}")
                continue
            digest = content_hash(pdf.text)
            records.write(
                Document(
                    id=Document.make_id(url, digest),
                    item_id=item.id,
                    url=url,
                    fetched_at=utcnow(),
                    content_hash=digest,
                    text=pdf.text,
                    text_len=len(pdf.text),
                    markup_lost=pdf.markup_lost,
                    no_text=pdf.no_text,
                )
            )
            have.add((item.id, url))
            fetched += 1
    return fetched
