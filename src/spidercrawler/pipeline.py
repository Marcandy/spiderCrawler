"""One run end to end: fetch → parse → health → detect → PDF text → extract → run record.

A failing source never stops the run; a broken source makes the run status `failed` so the
scheduled job alerts. Every step is timed into a trace written beside the run record.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime

import anthropic

from spidercrawler.budget import Budget
from spidercrawler.canonical import canonicalize
from spidercrawler.config import Config, Source
from spidercrawler.detector import KnownItem, detect, load_known
from spidercrawler.extractor import (
    CallResult,
    LLMClient,
    build_extraction,
    build_user_message,
    call_conversation,
    load_prompt,
    related_block,
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
from spidercrawler.repair import (
    attempt_records,
    combined_result,
    repair_flags,
    run_attempts,
)
from spidercrawler.retrieval import open_index, related_context
from spidercrawler.schema import SCHEMA_VERSION, ChangeSummary
from spidercrawler.trace import Trace
from spidercrawler.verifier import RowContext, is_suspicious, verify

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
    trace = Trace()

    for source in config.sources:
        try:
            with trace.step("fetch", source_id=source.id):
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

        with trace.step("parse", source_id=source.id) as event:
            parsed = parse_committee_page(page.content.decode("utf-8", "replace"), page.url)
            known_here = sum(1 for k in known.values() if k.item.source_id == source.id)
            health = check_health(parsed, known_here)
            event["outcome"] = "ok" if health.ok else "broken"
        if not health.ok:
            broken.append(source.id)
            errors.extend(f"{source.id}: broken: {reason}" for reason in health.reasons)
            continue  # keep old validators so the page is re-read next run

        seeding = seed and (seed_only is None or source.id in seed_only)
        with trace.step("detect", source_id=source.id):
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

    documents = 0 if cap_hit else fetch_documents(config, records, fetcher, errors, trace=trace)
    extraction_stats = (0, 0, 0)
    if llm is None:
        errors.append("extraction skipped: no LLM client (set ANTHROPIC_API_KEY)")
    else:
        extraction_stats = _extract(config, records, known, llm, errors, trace)
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
    trace.write(records, run_id)
    return run


def _second_opinion(config, records, llm, extraction, budget, trace) -> None:
    """Optional, off by default: the review assistant, inside the same run budget."""
    from spidercrawler.assistant import Bounds, assist
    from spidercrawler.tools import Toolbox

    settings = config.assistant
    with trace.step("assist", item_id=extraction.item_id, extraction_id=extraction.id) as event:
        brief = assist(
            llm,
            records,
            Toolbox(records, config),
            extraction,
            model=settings.model or config.extractor.model,
            effort=settings.effort,
            bounds=Bounds(settings.max_steps, settings.max_tokens, config.limits.max_output_tokens),
            budget=budget,
            trace=trace,
        )
        event.update(outcome=brief.status, brief_id=brief.id)


def _status(config: Config, broken: list[str], unreachable: list[str], checked: int) -> str:
    """Failed if any source is broken, nothing was checked, or the focus source was missed.

    Without the last two, a site outage would produce a green run that monitored nothing.
    """
    focus = {s.id for s in config.sources if s.role == "focus"}
    if broken or checked == 0 or focus & set(unreachable):
        return "failed"
    return "ok"


def fetch_documents(
    config: Config,
    records: RecordStore,
    fetcher: Fetcher,
    errors: list[str],
    wanted: set[str] | None = None,
    trace: Trace | None = None,
) -> int:
    """Fetch PDFs for items that need them and lack them. Leftovers wait for the next run.

    `wanted` (item ids) overrides the normal rule, e.g. to collect historical eval items.
    """
    trace = trace or Trace()
    sources = {s.id: s for s in config.sources}
    have = {(d.item_id, d.url) for d in records.iter(Document)}
    fetched = 0
    for item in sorted(records.iter(Item), key=lambda i: i.first_seen_run):
        eligible = (
            (item.id in wanted)
            if wanted is not None
            else needs_documents(item, sources.get(item.source_id))
        )
        if not eligible:
            continue
        for url in item.doc_urls:
            if (item.id, url) in have:
                continue
            try:
                with trace.step("fetch", item_id=item.id):
                    result = fetcher.get(url)
                with trace.step("pdf_text", item_id=item.id) as event:
                    pdf = extract_pdf_text(result.content)
                    flags = [f for f in ("markup_lost", "no_text") if getattr(pdf, f)]
                    event["outcome"] = "+".join(flags) or "ok"
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
    trace: Trace,
) -> tuple[int, int, int]:
    """Extract every eligible item that has all its documents and no current extraction.

    Returns (extractions made, blocked, tokens used). Stops when the budget is hit.
    """
    sources = {s.id: s for s in config.sources}
    system, prompt_version = load_prompt(config.extractor.prompt)
    model, effort = config.extractor.model, config.extractor.effort
    budget = Budget.from_limits(config.limits)
    # Prompt v2 only: an index of source documents and titles, never of model outputs.
    index = open_index(records, config.sources) if config.extractor.prompt == "v2" else None
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
        row = RowContext(
            current_table.get(item.id, item.table), item.title, item.row_date_literal, prefix
        )
        doc_flags = sorted(
            {f for d in ordered for f in ("markup_lost", "no_text") if getattr(d, f)}
        )
        ext_id = Extraction.make_id(
            item.id, model, effort, prompt_version, SCHEMA_VERSION, content_hash(source_text)
        )
        if records.exists(Extraction, ext_id):
            continue
        related = ""
        if index is not None:
            chunks = related_context(item, index, config.sources, config.extractor.related_chunks)
            related = related_block(chunks)
            if is_suspicious(related):  # retrieved text is a second injection channel
                doc_flags.append("suspicious_related_context")
        ids = {"item_id": item.id, "extraction_id": ext_id}
        attempts = []
        if "no_text" in doc_flags:
            result = CallResult(None, "no_text: document has no text layer", 0, 0)
        elif not budget.try_start(source_text + related):
            errors.append(f"extraction stopped: {budget.stopped_reason}; rest wait for next run")
            break
        else:

            def call(messages: list[dict], number: int, digest: str):
                return call_conversation(
                    llm, system, messages, model, effort, config.limits.max_output_tokens
                )

            def check(output: dict, row=row, prefix=prefix, source_text=source_text):
                summary = ChangeSummary.model_validate(output)
                verdict = verify(summary, canonicalize(summary, prefix), source_text, row)
                return verdict.status, verdict.reasons

            try:
                attempts = run_attempts(
                    call,
                    check,
                    build_user_message(source_text, row, related),
                    max_repairs=config.repair.max_repairs,
                    budget=budget,
                    trace=trace,
                    ids=ids,
                )
            except anthropic.APIError as exc:  # record and keep going; our own bugs still raise
                errors.append(f"{item.id}: extraction call failed: {exc}")
                continue
            result = combined_result(attempts)
        attempt_recs = attempt_records(attempts, ext_id, item.id)
        with trace.step("route", **ids) as event:
            extraction = build_extraction(
                item.id,
                source_text,
                result,
                model=model,
                effort=effort,
                prompt_version=prompt_version,
                row=row,
                rule_prefix=prefix,
                document_flags=doc_flags + repair_flags(attempts),
                attempt_ids=[a.id for a in attempt_recs],
            )
            event["outcome"] = extraction.queue_status
        # The extraction first: if the run dies between the two, the item is not re-extracted
        # into attempt ids that already hold a different (non-deterministic) output.
        records.write(extraction)
        for record in attempt_recs:
            records.write(record)
        made += bool(attempts)
        blocked += extraction.queue_status == "blocked"
        if (
            config.assistant.in_pipeline
            and extraction.queue_status != "blocked"
            and extraction.output
        ):
            try:
                _second_opinion(config, records, llm, extraction, budget, trace)
            except anthropic.APIError as exc:  # advice is optional; the extraction stands
                errors.append(f"{item.id}: assistant call failed: {exc}")
    return made, blocked, budget.tokens_used
