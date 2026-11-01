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
    errors: list[str] = []
    fetched = fetch_documents(config, records, Fetcher(config), errors, wanted=set(item_ids))
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
