# spiderCrawler

A source-grounded change monitor for Pennsylvania criminal procedure rules.

**Status: work in progress. Day 1 (deterministic core) done; no LLM step yet.** See [PLAN.md](PLAN.md).

It watches the Pennsylvania Supreme Court rules committee pages on pacourts.us, detects new
rulemaking items, and keeps its own history of them. A later milestone adds an LLM step that
summarizes each item with a quote from the source for every claim, a deterministic verifier,
and a mandatory human review before anything is published.

Not legal advice. Summaries describe changes only.

## Before

This is a rebuild of a 2023 Node/Express Amazon scraper prototype, preserved at tag
[`v0-amazon-scraper`](https://github.com/Marcandy/spiderCrawler/tree/v0-amazon-scraper).

## Run the tests

Requires [uv](https://docs.astral.sh/uv/). Tests never touch the network.

```sh
make install
make test        # or: make cov
make lint
```

## Run it

Live runs fetch from pacourts.us and need a contact email for the user agent:

```sh
export SPIDERCRAWLER_CONTACT_EMAIL=you@example.org
uv run spidercrawler run --seed   # first run: record existing items, fetch nothing else
uv run spidercrawler run          # later runs: detect new items and fetch their PDFs
```

Records are written as JSON under `data/` (one file per record, never overwritten). Source
access decisions are recorded in [docs/SOURCES.md](docs/SOURCES.md).
