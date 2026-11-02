# spiderCrawler

A source-grounded change monitor for **Pennsylvania criminal procedure rules**: it watches the
Pennsylvania Supreme Court rules committee pages, turns each new rulemaking item into a
structured summary where every claim quotes the source, blocks unsupported claims
automatically, and publishes nothing until a person approves it.

> **Status (2026-11-01):** pipeline, verifier, review app, digest, and eval harness are built
> and tested (250 offline tests). **No eval results yet** and **no external reviewer yet**.
> See [docs/EVAL.md](docs/EVAL.md) and [docs/LIMITATIONS.md](docs/LIMITATIONS.md).
> Not legal advice.

## How it works

```
weekly GitHub Actions run
  fetch ─▶ parse committee tables ─▶ health checks ─▶ detect new items ─▶ PDF text
                                                                            │
     records (append-only JSON, data branch) ◀── route ◀── verify ◀── extract (LLM)
                       │
                       ▼
            local review app (approve / edit / reject) ─▶ digest (GitHub Pages)
```

The LLM is one step out of twelve. Everything around it is deterministic, tested code:

| Step | What it does |
|---|---|
| Fetcher | Checks robots.txt on the host it lands on after redirects, 5 s between requests, 30 requests per run, identifies itself |
| Parser + health | Reads the three committee tables; stops a source on a layout change, a flood of "new" items, or a drop in rows |
| Detector | New items keyed on their PDF; first run records history without sending it to the model |
| PDF text | Keeps the court's change markup: additions as `{+ … +}`, deletions as `[ … ]` |
| Extractor | One structured-output call per item (Claude Sonnet 5.5 by default); refusals and truncation become errors, never partial output |
| Verifier | Every quote must exist in the source; dates and rule ids must sit in their quotes and agree with the committee page; advice phrasing fails |
| Router | Every item ends at a person; blocked items cannot be approved, and edits are re-verified |
| Digest | Publishes approved items only, each traced to its source, model, prompt version, verifier result, and reviewer |

## Safeguards

| Risk | Guard |
|---|---|
| Model invents facts | Quote-backed claims, checked in code against the source |
| Real quote, wrong claim | Mandatory human review; measured as "supported but wrong" |
| Reads like legal advice | Schema and verifier reject advice phrasing; disclaimer and source link on every item |
| Prompt injection in source text | No tools, schema-only output, delimited data, flag for the reviewer, human gate |
| Parser breakage or site migration | Health and volume guards stop the source; nothing is recorded until a person accepts |
| Runaway cost | 20 extractions and a token budget per run; fails closed |
| Publishing unchecked text | Nothing reaches the digest without an approval or an edit that passes the verifier |

What these do **not** prove is in [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Evaluation

A labeled set of historical items (about 20 dev, 30 frozen test, 8 synthetic injection),
scored for field accuracy, unsupported claims, block precision, false blocks,
supported-but-wrong, abstention, injection, and a keyword baseline, each with a 95% interval.
**Results: pending.** Method in [docs/EVAL.md](docs/EVAL.md).

## Run it

Requires [uv](https://docs.astral.sh/uv/). Tests never touch the network or the API.

```sh
make install
make test          # or: make cov
make lint
```

Live runs fetch from pacourts.us and need a contact email for the user agent; the LLM step
also needs an API key (without it, runs record "extraction skipped"):

```sh
export SPIDERCRAWLER_CONTACT_EMAIL=you@example.org
export ANTHROPIC_API_KEY=...        # optional
uv run spidercrawler run --seed     # first run: record existing items, no LLM calls
uv run spidercrawler run            # later runs: new items, PDFs, extraction
make review                         # local review page at http://127.0.0.1:8000
make digest                         # docs/digest/ from approved items
```

In production the weekly workflow commits records to the `data` branch; review with
`make data-checkout`, `make review DATA_DIR=data-branch/data`, `make data-push`, `make publish`.
Source access decisions are recorded in [docs/SOURCES.md](docs/SOURCES.md).

## Before

This is a rebuild of a 2023 Node/Express Amazon scraper prototype, preserved at tag
[`v0-amazon-scraper`](https://github.com/Marcandy/spiderCrawler/tree/v0-amazon-scraper).
The plan, including two rounds of adversarial review, is in [PLAN.md](PLAN.md).
