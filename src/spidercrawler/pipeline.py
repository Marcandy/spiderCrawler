"""One run end to end: fetch → parse → health → detect → PDF text → run record.

The extractor is added in Day 2. A failing source never stops the run; a broken source
makes the run status `failed` so the scheduled job alerts.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime

from spidercrawler.budget import Budget
from spidercrawler.config import Config, Source
from spidercrawler.detector import KnownItem, detect, load_known
from spidercrawler.extractor import (
    CallResult,
    LLMClient,
    build_extraction,
    build_user_message,
    call_model,
    load_prompt,
)
from spidercrawler.fetcher import Fetcher, FetchError, RequestCapError
from spidercrawler.index_parser import parse_committee_page
from spidercrawler.normalize import content_hash
from spidercrawler.parse_health import check_health, check_volume
from spidercrawler.pdf_text import PdfError, extract_pdf_text
from spidercrawler.records import (
    Document,
    Extraction,
    Item,
    RecordStore,
    Run,
    utcnow,
)
from spidercrawler.schema import SCHEMA_VERSION
from spidercrawler.verifier import RowContext

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
    seed_only: frozenset[str] | None = None,
    llm: LLMClient | None = None,
) -> Run:
    """Run every source once. With `seed`, items are recorded without extraction; `seed_only`
    limits that to the named sources (accepting one tripped page must not swallow a genuinely
    new item on another)."""
    started = now()
    run_id = Run.make_id(started)
    known: dict[str, KnownItem] = load_known(records)
    previous = records.latest_run()
    validators = dict(previous.page_validators) if previous else {}
    rows_seen = dict(previous.rows_seen) if previous else {}
    errors: list[str] = []
    broken: list[str] = []
    unreachable: list[str] = []
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
            unreachable.append(source.id)
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

        seeding = seed and (seed_only is None or source.id in seed_only)
        detection = detect(source.id, parsed.rows, known, run_id, seeding)
        if not seeding:  # seeding is how the maintainer accepts a page that tripped a guard
            volume = check_volume(
                rows=len(parsed.rows),
                new_items=len(detection.new_items),
                known_items=known_here,
                previous_rows=rows_seen.get(source.id),
                guards=config.guards,
            )
            if not volume.ok:
                broken.append(source.id)
                errors.extend(f"{source.id}: broken: {reason}" for reason in volume.reasons)
                continue  # stop and record nothing for this source

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
        rows_seen[source.id] = len(parsed.rows)

    documents = 0 if cap_hit else _fetch_documents(config, records, fetcher, errors)
    extraction_stats = (0, 0, 0)
    if llm is None:
        errors.append("extraction skipped: no LLM client (set ANTHROPIC_API_KEY)")
    else:
        extraction_stats = _extract(config, records, known, llm, errors)
    extracted, blocked, tokens = extraction_stats

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
        extractions_made=extracted,
        blocked=blocked,
        tokens_used=tokens,
        errors=errors,
        page_validators=validators,
        rows_seen=rows_seen,
        status=_status(config, broken, unreachable, checked),
    )
    records.write(run)
    return run


def _status(config: Config, broken: list[str], unreachable: list[str], checked: int) -> str:
    """Failed if any source is broken, nothing was checked, or the focus source was missed.

    Without the last two, a site outage would produce a green run that monitored nothing.
    """
    focus = {s.id for s in config.sources if s.role == "focus"}
    if broken or checked == 0 or focus & set(unreachable):
        return "failed"
    return "ok"


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


def _extract(
    config: Config,
    records: RecordStore,
    known: dict[str, KnownItem],
    llm: LLMClient,
    errors: list[str],
) -> tuple[int, int, int]:
    """Extract every eligible item that has all its documents and no current extraction.

    Returns (extractions made, blocked, tokens used). Stops when the budget is hit.
    """
    sources = {s.id: s for s in config.sources}
    system, prompt_version = load_prompt()
    model, effort = config.extractor.model, config.extractor.effort
    budget = Budget.from_limits(config.limits)
    docs_by_item: dict[str, dict[str, Document]] = {}
    for doc in records.iter(Document):
        docs_by_item.setdefault(doc.item_id, {})[doc.url] = doc
    current_table = {k.item.id: k.current_table for k in known.values()}
    made = blocked = 0

    for item in sorted(records.iter(Item), key=lambda i: i.first_seen_run):
        source = sources.get(item.source_id)
        docs = docs_by_item.get(item.id, {})
        if not needs_documents(item, source) or any(u not in docs for u in item.doc_urls):
            continue
        ordered = [docs[u] for u in item.doc_urls]
        source_text = "\n\n".join(d.text for d in ordered)
        prefix = source.rule_prefix if source else "Pa.R.Crim.P."
        row = RowContext(current_table.get(item.id, item.table), item.title,
                         item.row_date_literal, prefix)  # fmt: skip
        doc_flags = sorted(
            {f for d in ordered for f in ("markup_lost", "no_text") if getattr(d, f)}
        )
        ext_id = Extraction.make_id(
            item.id, model, effort, prompt_version, SCHEMA_VERSION, content_hash(source_text)
        )
        if records.exists(Extraction, ext_id):
            continue
        if "no_text" in doc_flags:
            result = None  # nothing to read; a human looks at the PDF
        elif not budget.try_start(source_text):
            errors.append(f"extraction stopped: {budget.stopped_reason}; rest wait for next run")
            break
        else:
            try:
                result = call_model(llm, system, build_user_message(source_text, row), model,
                                    effort, config.limits.max_output_tokens)  # fmt: skip
            except Exception as exc:  # API errors: record, keep going within budget
                errors.append(f"{item.id}: extraction call failed: {exc}")
                continue
            budget.record(result.input_tokens, result.output_tokens)
        extraction = build_extraction(
            item.id, source_text,
            result or CallResult(None, "no_text: document has no text layer", 0, 0),
            model=model, effort=effort, prompt_version=prompt_version, row=row,
            rule_prefix=prefix, document_flags=doc_flags,
        )  # fmt: skip
        records.write(extraction)
        made += result is not None
        blocked += extraction.queue_status == "blocked"
    return made, blocked, budget.tokens_used
