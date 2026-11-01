"""Command line interface: `spidercrawler run [--seed]`."""

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
    result = run_pipeline(
        config,
        RecordStore(data_dir),
        fetcher,
        seed=seed,
        seed_only=frozenset(source) if source else None,
    )

    typer.echo(
        f"{result.id}: {result.status} | sources {result.sources_checked}/{len(config.sources)}"
        f" | new items {result.items_found} | documents {result.documents_fetched}"
        f" | requests {result.requests_made}"
    )
    for error in result.errors:
        typer.echo(f"  - {error}", err=True)
    if result.status != "ok":
        raise typer.Exit(1)
