# Prompt for Claude Code: build spiderCrawler v2

Paste everything below the line into a new Claude Code session opened in a clone of github.com/Marcandy/spiderCrawler. `PLAN.md` is already in the repo root.

---

You are helping me rebuild my repo `spiderCrawler` into a change monitor for Pennsylvania criminal procedure rules. The full plan is in `PLAN.md`. Read it fully before doing anything. It is the source of truth. If something in this prompt conflicts with PLAN.md, follow PLAN.md and tell me.

## Context

- Current repo is a broken 2023 Node/Express Amazon scraper (9 files). We are not keeping it on `main`.
- New project is Python. I want it to work as a portfolio piece for AI engineering and AI safety fellowships, so code quality, tests, and honest docs matter more than features.
- I'm a full-stack JS/TS developer getting stronger in Python. Write idiomatic Python and explain any pattern I might not know in one line in the PR or commit message, not in long comments.
- The source is the pacourts.us rules committee pages (PLAN.md section 3). The Pa. Code / Pa. Bulletin site disallows crawling and must never be fetched, including through the `pabulletin.com` alias.

## Working rules

- Work milestone by milestone (PLAN.md section 10). Start with Week 1 only. Stop at the end of each milestone, summarize what was built, and wait for me.
- Small commits with clear messages. One concern per commit.
- Tests come with the code, not after. Use saved fixtures in `tests/fixtures/`. No test may hit the network.
- Never call a real website or the Anthropic API from tests or CI.
- Never write secrets to code, logs, or fixtures. API key comes from the `ANTHROPIC_API_KEY` env var.
- Before the first live fetch, re-check robots.txt for `www.pacourts.us` (redirect-aware, as in PLAN.md section 3) and tell me the result. If it now disallows our paths, stop and ask.
- Keep the LLM to the extractor step only. Every other component is deterministic code.
- Do not add features outside PLAN.md. If you think something is missing, ask.
- Ask me before: adding a dependency not listed below, changing the schema, changing verifier rules, or changing the data model.

## Stack

- Python 3.12, `uv` for env and deps, `ruff` for lint and format, `pytest`.
- `httpx` for HTTP. `selectolax` or `beautifulsoup4` for the HTML index pages (pick one and say why).
- `pdfplumber` for PDF text, character-level, to keep underline (addition) markup.
- `pydantic` v2 for the schema and config. `pyyaml` for `sources.yaml`.
- `sqlite3` from the standard library (no ORM) as a cache rebuilt from append-only JSON records.
- `anthropic` Python SDK (Week 2). Structured output through `output_config.format` / `messages.parse()` built from the Pydantic model. No `temperature` or other sampling params, and no forced `tool_choice`; current models reject them. Model id and effort come from config with env override; default model `claude-sonnet-5-5`.
- `fastapi` + `jinja2` for the local review page (Week 2).
- `typer` for the CLI.
- GitHub Actions for CI and the weekly run.

## Week 1 tasks (do these now)

1. Create git tag `v0-amazon-scraper` on the current `main` HEAD. Then remove the old Node files from `main` in one commit (`src/`, `__tests__/`, `data/`, `package.json`, `package-lock.json`, `.circleci/`; update `.gitignore`).
2. Set up `pyproject.toml`, `Makefile` (`install`, `lint`, `test`, `run`), `ruff` config, and `.github/workflows/ci.yml` (lint + test on push and PR).
3. Create `sources.yaml` with the 8 committee pages from PLAN.md section 3 (`crim` as focus, the other 7 as context). Write `docs/SOURCES.md` with the robots and terms findings from PLAN.md section 3. Re-check pacourts.us robots.txt and report back before step 4.
4. Build and test, in this order:
   - `normalize.py`: the one shared normalizer (whitespace, quotes, dashes, PDF spacing glitches like `Pa.R.J .A.` and `( h)`), plus `strip_markup()` for `{+ +}`.
   - `records.py`: append-only JSON records per PLAN.md section 4. Writes never overwrite; content-derived ids.
   - `store.py`: SQLite cache schema and `rebuild()` from records.
   - `fetcher.py`: redirect-aware robots check on the final host (non-text robots treated as suspect), 5 second per-host delay, 30 request cap per run, conditional GET, project user agent with contact email from config.
   - `index_parser.py`: parse the three committee tables into rows (title, PDF links, date column literal, table name).
   - `parse_health.py`: mark broken when an expected table heading is missing, or zero rows when the last good parse had rows.
   - `detector.py`: new items by item key (first PDF URL path), table moves recorded as events, seed mode.
   - `pdf_text.py`: pdfplumber text with underlined spans wrapped `{+ ... +}`, brackets kept, content hash, `markup_lost` and `no_text` flags.
   - `pipeline.py` and `cli.py run` / `run --seed` wiring fetch through PDF text, writing a run record. No extractor yet.
5. Fixtures needed:
   - a saved criminal committee page;
   - the same page with one new row added;
   - the same page with a table heading renamed (must mark broken);
   - the same page with empty tables (must mark broken);
   - one other committee page;
   - a small rule-text PDF with known underlined additions and bracketed deletions;
   - a publication-report PDF;
   - a mocked 302 from `/robots.txt` to an HTML page (must be treated as disallowed).

   Real pacourts.us PDFs and pages are public court documents and fine to commit as fixtures. Keep them small.
6. Write a short README stub: what this is, that it was rebuilt from the tagged prototype, and how to run tests.

Then stop and give me:
- What was built, as a short list.
- Test count and coverage of the core modules.
- The robots.txt re-check result.
- How well underline detection worked on the fixture PDFs.
- Anything in PLAN.md you think is wrong or risky now that you've built part of it.
- A reminder for me to message PAX / Code for Philly for a reviewer.

## Later milestones (do not start yet)

- Week 2: `eval/LABELING.md` first, then dev/test labels; `schema.py`, `canonical.py`, `prompts/extract_v1.md`, `extractor.py`, `verifier.py` (including edit re-verification and the index-row cross-check), `router.py`, `budget.py`, eval harness with response cache, review app.
- Week 3: `weekly.yml` with the `data` branch and seed run, `digest.py`, `docs/EVAL.md`, `docs/LIMITATIONS.md`, full README.

## Definition of good work here

- I can run `make install && make test` on a fresh clone and everything passes offline.
- Every guard in PLAN.md section 6 has at least one test by the end of Week 2.
- Test-split numbers are computed once, after tuning on dev.
- The README never claims more than the eval shows.
