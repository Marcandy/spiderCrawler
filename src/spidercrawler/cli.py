"""Command line interface: `spidercrawler run [--seed]`."""

import os
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


EvalDirOpt = Annotated[Path, typer.Option("--eval-dir", help="Eval items root.")]


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
    item_ids: Annotated[list[str], typer.Argument(help="Item ids from eval-list.")],
    split: Annotated[str, typer.Option(help="dev or test.")] = "dev",
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
    data_dir: DataOpt = Path("data"),
    eval_dir: EvalDirOpt = Path("eval/items"),
) -> None:
    """Fetch the chosen items' PDFs (politely, within the request cap) and export them."""
    from spidercrawler.detector import load_known
    from spidercrawler.eval_data import export_item
    from spidercrawler.pipeline import fetch_documents

    if split not in ("dev", "test"):
        raise typer.BadParameter("split must be dev or test")
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
    item_id: Annotated[str, typer.Argument(help="An exported item to copy.")],
    variant: Annotated[int, typer.Option(help="Which of the 8 synthetic injections.")] = 0,
    split: Annotated[str, typer.Option(help="dev or test.")] = "dev",
    eval_dir: EvalDirOpt = Path("eval/items"),
) -> None:
    """Make a synthetic injection item from an exported one (labeled synthetic)."""
    from spidercrawler.eval_data import make_injection

    matches = list(eval_dir.glob(f"*/{item_id}"))
    if len(matches) != 1:
        typer.echo(f"error: {item_id} not exported (found {len(matches)})", err=True)
        raise typer.Exit(2)
    folder = make_injection(matches[0], eval_dir, split, variant)  # type: ignore[arg-type]
    typer.echo(f"wrote {folder}")


@app.command("eval-status")
def eval_status(eval_dir: EvalDirOpt = Path("eval/items")) -> None:
    """Counts per split: total, labeled, synthetic, expungement yes."""
    from spidercrawler.eval_data import dumps, status

    typer.echo(dumps(status(eval_dir)))


@app.command("eval")
def eval_cmd(
    split: Annotated[str, typer.Option(help="dev or test.")] = "dev",
    live: Annotated[
        bool, typer.Option("--live", help="Call the API for responses missing from the cache.")
    ] = False,
    config_path: ConfigOpt = DEFAULT_SOURCES_PATH,
    eval_dir: EvalDirOpt = Path("eval/items"),
    cache_dir: Annotated[Path, typer.Option("--cache-dir")] = Path("eval/cache"),
    reports_dir: Annotated[Path, typer.Option("--reports-dir")] = Path("eval/reports"),
) -> None:
    """Score the extractor on a split. Offline from the response cache unless --live."""
    from spidercrawler.eval_data import INJECTIONS, REGEX_VISIBLE_INJECTIONS, load_items
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
    client = None
    if live:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            typer.echo("error: --live needs ANTHROPIC_API_KEY", err=True)
            raise typer.Exit(2)
        import anthropic

        client = anthropic.Anthropic()
    model, effort = config.extractor.model, config.extractor.effort
    try:
        results, skipped = run_eval(
            load_items(eval_dir, split),  # type: ignore[arg-type]
            ResponseCache(cache_dir),
            client,
            model,
            effort,
            config.limits.max_output_tokens,
            set(INJECTIONS[:REGEX_VISIBLE_INJECTIONS]),
        )
    except MissingCacheError as exc:
        typer.echo(f"error: no cached response for: {exc}. Run `make eval-live` first.", err=True)
        raise typer.Exit(2) from exc
    test_runs = record_test_run(reports_dir) if split == "test" and results else 0
    report = render_report(
        split, results, compute_metrics(results), skipped, model, effort, test_runs
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"{split}.md").write_text(report, encoding="utf-8")
    (reports_dir / f"{split}.json").write_text(results_json(results) + "\n", encoding="utf-8")
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

    prefixes = {s.id: s.rule_prefix for s in load_config(config_path).sources}
    entries = build(RecordStore(data_dir), out_dir, datetime.now(UTC), prefixes)
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
    typer.echo("\n".join(lines))
