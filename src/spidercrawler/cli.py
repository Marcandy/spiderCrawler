"""Command line interface: `spidercrawler run [--seed]`."""

import os
import pydoc
from pathlib import Path
from typing import Annotated

import typer

from spidercrawler.config import DEFAULT_SOURCES_PATH, load_config
from spidercrawler.fetcher import Fetcher
from spidercrawler.pipeline import run_pipeline
from spidercrawler.records import RecordStore

app = typer.Typer(no_args_is_help=True, add_completion=False)

ConfigOpt = Annotated[Path, typer.Option("--config", help="Path to sources.yaml.")]
DataOpt = Annotated[Path, typer.Option("--data-dir", help="Directory holding JSON records.")]


@app.callback()
def main() -> None:
    """Change monitor for Pennsylvania criminal procedure rules."""


@app.command()
def run(
    seed: Annotated[
        bool, typer.Option("--seed", help="Record existing items without extracting them.")
    ] = False,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="With --seed: seed only this source (repeatable)."),
    ] = None,
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
    data_dir: DataOpt = Path("data"),
) -> None:
    """Fetch every source, detect new items, and store their documents."""
    config = load_config(config_path)
    known_ids = {s.id for s in config.sources}
    if source and (not seed or set(source) - known_ids):
        typer.echo(f"error: --source needs --seed and one of {sorted(known_ids)}", err=True)
        raise typer.Exit(2)
    try:
        fetcher = Fetcher(config)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    llm = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic  # only needed when extracting

        llm = anthropic.Anthropic()
    result = run_pipeline(
        config,
        RecordStore(data_dir),
        fetcher,
        seed=seed,
        seed_only=frozenset(source) if source else None,
        llm=llm,
    )

    typer.echo(
        f"{result.id}: {result.status} | sources {result.sources_checked}/{len(config.sources)}"
        f" | new items {result.items_found} | documents {result.documents_fetched}"
        f" | requests {result.requests_made} | extractions {result.extractions_made}"
        f" (blocked {result.blocked}, tokens {result.tokens_used})"
    )
    for error in result.errors:
        typer.echo(f"  - {error}", err=True)
    if result.status != "ok":
        raise typer.Exit(1)


@app.command()
def doctor(
    live: Annotated[
        bool, typer.Option("--live", help="Check readiness for a non-seed run (needs a seed run).")
    ] = False,
    github: Annotated[
        bool, typer.Option("--github", help="Also print GitHub Actions annotations.")
    ] = False,
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
    data_dir: DataOpt = Path("data"),
) -> None:
    """Offline preflight: what is missing before a run, with a one-line fix for each."""
    from spidercrawler.doctor import blocked, run_checks

    checks = run_checks(config_path, data_dir, live=live)
    for check in checks:
        typer.echo(check.line())
        if github and (annotation := check.annotation()):
            typer.echo(annotation)
    if blocked(checks):
        typer.echo("not ready: fix the items marked missing", err=True)
        raise typer.Exit(1)


EvalDirOpt = Annotated[Path, typer.Option("--eval-dir", help="Eval items root.")]
PlanOpt = Annotated[
    Path | None, typer.Option("--plan", help="Take the items from this eval-sample plan.")
]


@app.command("eval-list")
def eval_list(
    source: Annotated[str, typer.Option(help="Source id.")] = "crim",
    grep: Annotated[str, typer.Option(help="Case-insensitive title filter.")] = "",
    data_dir: DataOpt = Path("data"),
) -> None:
    """List known items to choose eval candidates from."""
    from spidercrawler.detector import load_known

    for known in load_known(RecordStore(data_dir)).values():
        item = known.item
        if item.source_id == source and grep.lower() in item.title.lower():
            typer.echo(f"{item.id}\t{known.current_table}\t{item.row_date_literal}\t{item.title}")


@app.command("eval-collect")
def eval_collect(
    item_ids: Annotated[
        list[str] | None, typer.Argument(help="Item ids from eval-list (or use --plan).")
    ] = None,
    split: Annotated[str, typer.Option(help="dev or test.")] = "dev",
    plan: PlanOpt = None,
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
    data_dir: DataOpt = Path("data"),
    eval_dir: EvalDirOpt = Path("eval/items"),
) -> None:
    """Fetch the chosen items' PDFs (politely, within the request cap) and export them."""
    from spidercrawler.detector import load_known
    from spidercrawler.eval_data import export_item
    from spidercrawler.eval_label import FreezeError, require_unfrozen
    from spidercrawler.eval_sample import load_plan
    from spidercrawler.pipeline import fetch_documents

    if split not in ("dev", "test"):
        raise typer.BadParameter("split must be dev or test")
    if bool(item_ids) == bool(plan):
        typer.echo("error: give item ids or --plan, not both", err=True)
        raise typer.Exit(2)
    try:
        require_unfrozen(eval_dir, split, "add items to it")
    except FreezeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if plan:
        item_ids = load_plan(plan).item_ids(split)  # type: ignore[arg-type]
    assert item_ids
    config = load_config(config_path)
    records = RecordStore(data_dir)
    known = {k.item.id: k for k in load_known(records).values()}
    unknown = [i for i in item_ids if i not in known]
    if unknown:
        typer.echo(f"error: unknown item ids: {unknown}", err=True)
        raise typer.Exit(2)
    try:
        fetcher = Fetcher(config)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    errors: list[str] = []
    fetched = fetch_documents(config, records, fetcher, errors, wanted=set(item_ids))
    prefixes = {s.id: s.rule_prefix for s in config.sources}
    exported = 0
    for item_id in item_ids:
        k = known[item_id]
        try:
            export_item(
                records,
                k.item,
                split,
                eval_dir,
                prefixes.get(k.item.source_id, ""),
                k.current_table,
            )
            exported += 1
        except ValueError as exc:
            errors.append(str(exc))
    typer.echo(
        f"fetched {fetched} documents, exported {exported}/{len(item_ids)} to {eval_dir}/{split}"
    )
    for error in errors:
        typer.echo(f"  - {error}", err=True)


@app.command("eval-inject")
def eval_inject(
    item_id: Annotated[
        str | None, typer.Argument(help="An exported item to copy (or use --plan).")
    ] = None,
    variant: Annotated[int, typer.Option(help="Which of the 8 synthetic injections.")] = 0,
    split: Annotated[str, typer.Option(help="dev or test.")] = "dev",
    plan: PlanOpt = None,
    eval_dir: EvalDirOpt = Path("eval/items"),
) -> None:
    """Make a synthetic injection item from an exported one (labeled synthetic).

    With --plan, make every injection item the plan lists whose base item is exported.
    """
    from spidercrawler.eval_data import item_dir, make_injection
    from spidercrawler.eval_label import FreezeError, require_unfrozen
    from spidercrawler.eval_sample import load_plan

    if bool(item_id) == bool(plan):
        typer.echo("error: give an item id or --plan, not both", err=True)
        raise typer.Exit(2)
    if plan:
        slots = [
            (slot.base_item_id, slot.variant, name)
            for name, sp in load_plan(plan).splits.items()
            for slot in sp.injections
        ]
    else:
        assert item_id
        slots = [(item_id, variant, split)]
    for base, number, target in slots:
        matches = list(eval_dir.glob(f"*/{base}"))
        if len(matches) != 1:
            typer.echo(f"error: {base} not exported (found {len(matches)})", err=True)
            raise typer.Exit(2)
        if plan and (item_dir(eval_dir, target, f"{base}-inj{number}") / "gold.json").exists():
            continue  # made by an earlier call
        try:
            require_unfrozen(eval_dir, target, "add items to it")
        except FreezeError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from exc
        folder = make_injection(matches[0], eval_dir, target, number)  # type: ignore[arg-type]
        typer.echo(f"wrote {folder}")


@app.command("eval-status")
def eval_status(eval_dir: EvalDirOpt = Path("eval/items")) -> None:
    """Counts per split: total, labeled, second-labeled, frozen, synthetic, expungement yes."""
    from spidercrawler.eval_data import dumps
    from spidercrawler.eval_label import freeze_state, labeling_status

    typer.echo(dumps(labeling_status(eval_dir)))
    state = freeze_state(eval_dir)
    if state.manifest is None:
        typer.echo("test split: not frozen")
    else:
        typer.echo(f"test split: frozen {state.manifest.frozen_at.date().isoformat()}")
        for problem in state.problems():
            typer.echo(f"  - {problem}", err=True)


@app.command("eval-sample")
def eval_sample(
    seed: Annotated[int, typer.Option(help="Same seed and same known items: same plan.")],
    out: Annotated[Path, typer.Option("--out", help="Where to write the plan.")] = Path(
        "eval/sample-plan.json"
    ),
    force: Annotated[bool, typer.Option("--force", help="Replace a different plan.")] = False,
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
    data_dir: DataOpt = Path("data"),
) -> None:
    """Pick stratified eval candidates and write the plan for review. Fetches nothing."""
    from spidercrawler.detector import load_known
    from spidercrawler.eval_label import write_atomic
    from spidercrawler.eval_sample import build_plan, describe, dumps

    config = load_config(config_path)
    known = list(load_known(RecordStore(data_dir)).values())
    if not known:
        typer.echo(f"error: no items in {data_dir}; seed or check out the data first", err=True)
        raise typer.Exit(2)
    plan = build_plan(known, {s.id: s for s in config.sources}, seed)
    text = dumps(plan)
    if out.exists() and out.read_text(encoding="utf-8") != text and not force:
        typer.echo(f"error: {out} holds a different plan; --force replaces it", err=True)
        raise typer.Exit(2)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(out, text)
    typer.echo(describe(plan))
    typer.echo(f"wrote {out}. Nothing was fetched. Review it, then collect each split:")
    typer.echo(f"  spidercrawler eval-collect --plan {out} --split dev")


@app.command("eval-label")
def eval_label(
    item_id: Annotated[
        str | None, typer.Argument(help="Item to label (default: the next unlabeled one).")
    ] = None,
    split: Annotated[str | None, typer.Option(help="With no item id: only this split.")] = None,
    second: Annotated[
        bool, typer.Option("--second", help="Label as the second labeler (gold.second.json).")
    ] = False,
    relabel: Annotated[bool, typer.Option("--relabel", help="Replace existing labels.")] = False,
    unfreeze: Annotated[
        bool, typer.Option("--unfreeze", help="Edit a frozen test item. Needs --reason; logged.")
    ] = False,
    reason: Annotated[str, typer.Option(help="Why the frozen item is edited. Logged.")] = "",
    eval_dir: EvalDirOpt = Path("eval/items"),
    guide_path: Annotated[Path, typer.Option("--guide")] = Path("eval/LABELING.md"),
) -> None:
    """Label one item at the terminal. Shows the source text and the guide, never a model's
    output or a suggested value; every label is typed by a person."""
    from spidercrawler import eval_label as tools

    def fail(message: str) -> typer.Exit:
        typer.echo(f"error: {message}", err=True)
        return typer.Exit(2)

    if split not in (None, "dev", "test"):
        raise typer.BadParameter("split must be dev or test")
    try:
        guide = tools.guide_sections(guide_path.read_text(encoding="utf-8"))
        item = (
            tools.find_item(eval_dir, item_id)
            if item_id
            else tools.next_unlabeled(eval_dir, split, second)  # type: ignore[arg-type]
        )
    except (OSError, ValueError) as exc:
        raise fail(str(exc)) from exc
    if item is None:
        typer.echo("nothing left to label")
        return
    existing = tools.second_gold(item) if second else item.gold
    if existing is not None and existing.labels is not None and not relabel:
        raise fail(f"{item.gold.id} is already labeled; --relabel replaces the labels")
    # Second labels live in their own file and never change the frozen gold.json.
    frozen = not second and item.gold.split == "test" and tools.freeze_state(eval_dir).frozen
    if frozen and not (unfreeze and reason.strip()):
        raise fail(
            f"{item.gold.id} is in the frozen test split; "
            "--unfreeze --reason '...' edits it and is logged"
        )

    result = tools.label_interactively(
        item,
        guide,
        ask=lambda text: typer.prompt(text, default="", show_default=False),
        say=typer.echo,
        page=pydoc.pager,  # a pager at a terminal, plain output otherwise
    )
    if result is None:
        typer.echo("not saved")
        raise typer.Exit(1)
    if frozen:  # logged before the write, so an edit can never go unrecorded
        tools.log_event(eval_dir, "unfreeze-edit", f"{item.gold.id}: {reason.strip()}")
    path = tools.save_labels(item, *result, second=second)
    typer.echo(f"saved {path}")
    if frozen:
        typer.echo("the freeze no longer matches; run `spidercrawler eval-freeze --refreeze`")


@app.command("eval-freeze")
def eval_freeze(
    refreeze: Annotated[
        bool, typer.Option("--refreeze", help="Replace an existing freeze. Logged.")
    ] = False,
    check: Annotated[bool, typer.Option("--check", help="Only verify the freeze.")] = False,
    eval_dir: EvalDirOpt = Path("eval/items"),
) -> None:
    """Freeze the labeled test split: a hash of every item's text and labels."""
    from spidercrawler.eval_label import FreezeError, freeze, freeze_state

    if check:
        state = freeze_state(eval_dir)
        if state.manifest is None:
            typer.echo("test split: not frozen", err=True)
            raise typer.Exit(1)
        for problem in state.problems():
            typer.echo(f"  - {problem}", err=True)
        if not state.intact:
            raise typer.Exit(1)
        typer.echo(f"test split: {len(state.manifest.items)} items match the freeze")
        return
    try:
        manifest = freeze(eval_dir, refreeze=refreeze)
    except FreezeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"froze {len(manifest.items)} test items")


@app.command("eval-agreement")
def eval_agreement(
    split: Annotated[str, typer.Option(help="dev or test.")] = "test",
    docs: Annotated[
        Path | None, typer.Option("--docs", help="Also write the table into this file's block.")
    ] = None,
    eval_dir: EvalDirOpt = Path("eval/items"),
) -> None:
    """Per-field agreement between gold.json and a second labeler's gold.second.json."""
    from spidercrawler.eval_label import agreement, render_agreement, update_docs

    if split not in ("dev", "test"):
        raise typer.BadParameter("split must be dev or test")
    table = render_agreement(agreement(eval_dir, split))  # type: ignore[arg-type]
    typer.echo(table)
    if docs:
        update_docs(docs, table)
        typer.echo(f"updated {docs}")


@app.command("eval")
def eval_cmd(
    split: Annotated[str, typer.Option(help="dev or test.")] = "dev",
    live: Annotated[
        bool, typer.Option("--live", help="Call the API for responses missing from the cache.")
    ] = False,
    repair: Annotated[
        int | None,
        typer.Option("--repair", min=0, max=2, help="Repair calls per item (default: config)."),
    ] = None,
    prompt: Annotated[
        str | None, typer.Option("--prompt", help="v1 or v2 (default: config). Dev only.")
    ] = None,
    data_dir: Annotated[
        Path, typer.Option("--data-dir", help="Records to retrieve related context from (v2).")
    ] = Path("data"),
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
    eval_dir: EvalDirOpt = Path("eval/items"),
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = Path("eval/cache"),
    reports_dir: Annotated[Path, typer.Option("--reports-dir")] = Path("eval/reports"),
) -> None:
    """Score the extractor on a split. Offline from the response cache unless --live."""
    from spidercrawler.eval_data import INJECTIONS, REGEX_VISIBLE_INJECTIONS, load_items
    from spidercrawler.eval_label import agreement, freeze_state, read_log, render_agreement
    from spidercrawler.evaluate import (
        MissingCacheError,
        compute_metrics,
        record_test_run,
        render_report,
        results_json,
        run_eval,
    )
    from spidercrawler.extractor import ResponseCache

    if split not in ("dev", "test"):
        raise typer.BadParameter("split must be dev or test")
    config = load_config(config_path)
    repair = config.repair.max_repairs if repair is None else repair
    prompt = prompt or config.extractor.prompt
    if prompt not in ("v1", "v2"):
        raise typer.BadParameter("prompt must be v1 or v2")
    freeze_note = None
    if split == "test":
        # The test split scores the configured pipeline only; variants are compared on dev.
        if (prompt, repair) != (config.extractor.prompt, config.repair.max_repairs):
            typer.echo("error: --prompt and --repair variants are scored on dev only", err=True)
            raise typer.Exit(2)
        # Reported numbers come from a frozen split only (PLAN.md section 8).
        state = freeze_state(eval_dir)
        if not state.intact:
            why = "; ".join(state.problems()) or "it is not frozen"
            typer.echo(f"error: test split: {why}. Run `spidercrawler eval-freeze`.", err=True)
            raise typer.Exit(2)
        assert state.manifest
        edits = sum(e["action"] != "freeze" for e in read_log(eval_dir))
        freeze_note = (
            f"Test split frozen {state.manifest.frozen_at.date().isoformat()}; "
            f"unfreeze edits and refreezes on record: {edits}"
        )
    client = None
    if live:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            typer.echo("error: --live needs ANTHROPIC_API_KEY", err=True)
            raise typer.Exit(2)
        import anthropic

        client = anthropic.Anthropic()
    model, effort = config.extractor.model, config.extractor.effort
    related_for = _related_for(data_dir, config) if prompt == "v2" else (lambda item: "")
    try:
        results, skipped = run_eval(
            load_items(eval_dir, split),  # type: ignore[arg-type]
            ResponseCache(cache_dir),
            client,
            model,
            effort,
            config.limits.max_output_tokens,
            set(INJECTIONS[:REGEX_VISIBLE_INJECTIONS]),
            repair,
            prompt,
            related_for,
        )
    except MissingCacheError as exc:
        typer.echo(f"error: no cached response for: {exc}. Run `make eval-live` first.", err=True)
        raise typer.Exit(2) from exc
    test_runs = record_test_run(reports_dir) if split == "test" and results else 0
    pairs = agreement(eval_dir, split)  # type: ignore[arg-type]
    report = render_report(
        split,
        results,
        compute_metrics(results),
        skipped,
        model,
        effort,
        test_runs,
        freeze_note=freeze_note,
        agreement=render_agreement(pairs) if pairs[0].n else None,
        max_repairs=repair,
        prompt=prompt,
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    name = split if prompt == "v1" else f"{split}-{prompt}"  # v1 and v2 reports sit side by side
    (reports_dir / f"{name}.md").write_text(report, encoding="utf-8")
    (reports_dir / f"{name}.json").write_text(results_json(results) + "\n", encoding="utf-8")
    typer.echo(report)


def _related_for(data_dir: Path, config):
    """Related context for an eval item, retrieved exactly as the pipeline would."""
    from spidercrawler.extractor import related_block
    from spidercrawler.records import Item
    from spidercrawler.retrieval import open_index, related_context

    records = RecordStore(data_dir)
    index = open_index(records, config.sources)
    items = {i.id: i for i in records.iter(Item)}

    def related_for(item) -> str:
        known = items.get(item.gold.item_id)
        if known is None:
            return ""
        chunks = related_context(known, index, config.sources, config.extractor.related_chunks)
        return related_block(chunks)

    return related_for


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Words to look for.")],
    rule: Annotated[
        str | None, typer.Option("--rule", help='Exact rule id, e.g. 632 or "Pa.R.Crim.P. 632".')
    ] = None,
    k: Annotated[int, typer.Option("-k", min=1, max=50, help="How many chunks.")] = 5,
    data_dir: DataOpt = Path("data"),
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
) -> None:
    """Search every document and title the monitor has recorded (lexical, BM25)."""
    from spidercrawler.retrieval import canonical_rule_filter, open_index

    index = open_index(RecordStore(data_dir), load_config(config_path).sources)
    hits = index.search(query, k, rule_ids=[canonical_rule_filter(rule)] if rule else None)
    if not hits:
        typer.echo("no match")
    for hit in hits:
        chunk = hit.chunk
        where = chunk.url or f"title, {chunk.table} table"
        typer.echo(f"\n[{hit.score:.2f}] {chunk.item_id} {chunk.id} ({where})")
        typer.echo(f"  rules: {', '.join(chunk.rule_ids) or 'none'}")
        typer.echo("  " + chunk.text[:400].replace("\n", "\n  "))


@app.command("eval-link")
def eval_link(
    item_id: Annotated[
        str | None, typer.Argument(help="Adopted item to label (default: the next one).")
    ] = None,
    show_list: Annotated[
        bool, typer.Option("--list", help="List proposed items in page order and exit.")
    ] = False,
    labeler: Annotated[str, typer.Option(help="Name recorded on the label.")] = "maintainer",
    data_dir: DataOpt = Path("data"),
    links_dir: Annotated[Path, typer.Option("--links-dir")] = Path("eval/links"),
) -> None:
    """Label which item is an adoption's proposal. Shows no candidate and no retriever
    output; a person types the id."""
    from spidercrawler import eval_links as tools
    from spidercrawler.detector import load_known

    known = sorted(load_known(RecordStore(data_dir)).values(), key=lambda k: k.item.id)
    items = {k.item.id: k.item for k in known}
    if show_list:
        for k_ in known:
            if k_.current_table != "approved":
                typer.echo(f"{k_.item.id}  {k_.item.row_date_literal or '-':<20} {k_.item.title}")
        return
    done = tools.load_links(links_dir)
    todo = [k_.item for k_ in known if k_.current_table == "approved" and k_.item.id not in done]
    item = items.get(item_id) if item_id else (todo[0] if todo else None)
    if item is None:
        typer.echo("no such item" if item_id else "nothing left to link")
        raise typer.Exit(2 if item_id else 0)
    label = tools.label_link(
        item,
        items,
        labeler,
        ask=lambda text: typer.prompt(text, default="", show_default=False),
        say=typer.echo,
    )
    if label is None:
        typer.echo("not saved")
        raise typer.Exit(1)
    typer.echo(f"saved {tools.save_link(links_dir, label)}")


@app.command("eval-retrieval")
def eval_retrieval(
    k: Annotated[int, typer.Option("-k", min=1, max=20)] = 5,
    data_dir: DataOpt = Path("data"),
    links_dir: Annotated[Path, typer.Option("--links-dir")] = Path("eval/links"),
    reports_dir: Annotated[Path, typer.Option("--reports-dir")] = Path("eval/reports"),
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
) -> None:
    """Score `related_items` against the hand-labeled links: recall@k and MRR."""
    from spidercrawler import eval_links as tools
    from spidercrawler.records import Item
    from spidercrawler.retrieval import open_index, related_items

    records, sources = RecordStore(data_dir), load_config(config_path).sources
    items = {i.id: i for i in records.iter(Item)}
    index = open_index(records, sources)

    def rank(item_id: str) -> list[str]:
        if item_id not in items:
            return []
        return [link.item_id for link in related_items(items[item_id], index, items, sources, k)]

    report = tools.render_score(
        tools.score_links(list(tools.load_links(links_dir).values()), rank, k)
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "retrieval.md").write_text(report, encoding="utf-8")
    typer.echo(report)


@app.command()
def review(
    port: Annotated[int, typer.Option(help="Local port.")] = 8000,
    reviewer: Annotated[str, typer.Option(help="Name recorded on decisions.")] = "maintainer",
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
    data_dir: DataOpt = Path("data"),
) -> None:
    """Open the local review page (http://127.0.0.1:PORT). Use `make review`."""
    try:
        import uvicorn
    except ImportError as exc:
        typer.echo("error: uvicorn is not installed; run `make review`", err=True)
        raise typer.Exit(2) from exc
    from spidercrawler.review_app import create_app

    app_ = create_app(RecordStore(data_dir), load_config(config_path), reviewer)
    uvicorn.run(app_, host="127.0.0.1", port=port)


@app.command()
def digest(
    data_dir: DataOpt = Path("data"),
    out_dir: Annotated[Path, typer.Option("--out-dir")] = Path("docs/digest"),
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
) -> None:
    """Build the static digest from approved items only."""
    from datetime import UTC, datetime

    from spidercrawler.digest import build

    config = load_config(config_path)
    prefixes = {s.id: s.rule_prefix for s in config.sources}
    entries = build(RecordStore(data_dir), out_dir, datetime.now(UTC), prefixes, config.sources)
    typer.echo(f"wrote {out_dir}/index.html and digest.json with {len(entries)} approved item(s)")


@app.command()
def summary(data_dir: DataOpt = Path("data")) -> None:
    """Markdown summary of the latest run (used for the GitHub Actions job summary)."""
    run = RecordStore(data_dir).latest_run()
    if run is None:
        typer.echo("No runs recorded yet.")
        return
    lines = [
        f"## spiderCrawler run `{run.id}`: **{run.status}**",
        "",
        "| | |",
        "|---|---|",
        f"| Seed run | {'yes' if run.seed else 'no'} |",
        f"| Sources checked | {run.sources_checked} |",
        f"| Broken sources | {', '.join(run.sources_broken) or 'none'} |",
        f"| New items | {run.items_found} |",
        f"| Documents fetched | {run.documents_fetched} |",
        f"| Requests made | {run.requests_made} |",
        f"| Extractions | {run.extractions_made} (blocked {run.blocked}) |",
        f"| Tokens used | {run.tokens_used} |",
    ]
    if run.errors:
        lines += ["", "### Errors and notes", ""] + [f"- {e}" for e in run.errors]
    lines += [
        "",
        f"Step-by-step trace: `data/traces/{run.id}.jsonl` on the `data` branch, or "
        f"`spidercrawler trace {run.id}`. It holds ids, timings, and token counts only.",
    ]
    typer.echo("\n".join(lines))


@app.command()
def trace(
    run_id: Annotated[str | None, typer.Argument(help="Run id; the latest run if omitted.")] = None,
    data_dir: DataOpt = Path("data"),
) -> None:
    """Print one run's trace: each step with its outcome, duration, and token counts."""
    from spidercrawler.records import Run
    from spidercrawler.trace import read_trace, render

    records = RecordStore(data_dir)
    known = {r.id for r in records.iter(Run)}  # the id is looked up, never used as a path
    latest = records.latest_run()
    run_id = run_id or (latest.id if latest else None)
    if run_id not in known:
        typer.echo("error: no such run", err=True)
        raise typer.Exit(2)
    try:
        events = read_trace(records, run_id)
    except FileNotFoundError:
        typer.echo(f"error: {run_id} has no trace (it predates tracing)", err=True)
        raise typer.Exit(2) from None
    typer.echo(render(events))


@app.command("review-notice")
def review_notice(
    title: Annotated[
        bool, typer.Option("--title", help="Print the issue title instead of the body.")
    ] = False,
    data_dir: DataOpt = Path("data"),
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
) -> None:
    """Markdown for the "review needed" issue: titles, counts, and source links only.

    Prints nothing when no item awaits review; the workflow then closes the issue.
    """
    from spidercrawler.notice import build_notice

    notice = build_notice(RecordStore(data_dir), load_config(config_path).sources)
    if notice is not None:
        typer.echo(notice.title if title else notice.body, nl=title)


@app.command()
def cache(
    data_dir: DataOpt = Path("data"),
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
    db: Annotated[Path, typer.Option("--db")] = Path(".cache/spidercrawler.sqlite"),
) -> None:
    """Rebuild the SQLite query cache and the search index from the JSON records."""
    from spidercrawler.corpus import build_chunks
    from spidercrawler.retrieval import FtsIndex
    from spidercrawler.store import rebuild

    records = RecordStore(data_dir)
    conn = rebuild(records, db)
    FtsIndex.build(build_chunks(records, load_config(config_path).sources), conn)
    counts = {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("items", "documents", "extractions", "reviews", "runs", "chunks")
    }
    conn.close()
    typer.echo(f"rebuilt {db}: " + ", ".join(f"{n} {t}" for t, n in counts.items()))
