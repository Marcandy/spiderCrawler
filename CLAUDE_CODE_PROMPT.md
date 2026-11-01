# Prompt for Claude Code: build spiderCrawler v2

Paste everything below the line into a new Claude Code session opened in a clone of github.com/Marcandy/spiderCrawler. Put `spidercrawler-plan.md` in the repo root as `PLAN.md` first.

---

You are helping me rebuild my repo `spiderCrawler` into a policy change monitor. The full plan is in `PLAN.md`. Read it fully before doing anything. It is the source of truth. If something in this prompt conflicts with PLAN.md, follow PLAN.md and tell me.

## Context

- Current repo is a broken 2023 Node/Express Amazon scraper (9 files). We are not keeping it on `main`.
- New project is Python. I want it to work as a portfolio piece for AI engineering and AI safety fellowships, so code quality, tests, and honest docs matter more than features.
- I'm a full-stack JS/TS developer getting stronger in Python. Write idiomatic Python and explain any pattern I might not know in one line in the PR or commit message, not in long comments.

## Working rules

- Work milestone by milestone (PLAN.md section 10). Start with Week 1 only. Stop at the end of each milestone, summarize what was built, and wait for me.
- Small commits with clear messages. One concern per commit.
- Tests come with the code, not after. Use saved HTML fixtures in `tests/fixtures/`. No test may hit the network.
- Never call a real website or the Anthropic API from tests or CI.
- Never write secrets to code, logs, or fixtures. API key comes from the `ANTHROPIC_API_KEY` env var.
- Before writing fetcher code against a real site, check that site's robots.txt and tell me what it allows. If it disallows our paths, stop and ask.
- Keep the LLM to the extractor step only. Every other component is deterministic code.
- Do not add features outside PLAN.md. If you think something is missing, ask.
- Ask me before: adding a dependency not listed below, changing the schema, changing verifier rules, or changing the data model.

## Stack

- Python 3.12, `uv` for env and deps, `ruff` for lint and format, `pytest`.
- `httpx` for HTTP, `selectolax` or `beautifulsoup4` for parsing (pick one and say why).
- `pydantic` v2 for the schema and config.
- `sqlite3` from the standard library (no ORM).
- `anthropic` Python SDK. Use structured output through a tool-use JSON schema built from the Pydantic model. Model id comes from config with env override; default `claude-sonnet-5-5`. Temperature 0.
- `fastapi` + `jinja2` for the local review page (Week 2).
- `typer` for the CLI.
- GitHub Actions for CI and the weekly run.

## Week 1 tasks (do these now)

1. Create git tag `v0-amazon-scraper` on the current `main` HEAD. Then remove the old Node files from `main` in one commit.
2. Set up `pyproject.toml`, `Makefile` (`install`, `lint`, `test`, `run`), `ruff` config, and `.github/workflows/ci.yml` (lint + test on push and PR).
3. Create `sources.yaml` with the two sources from PLAN.md section 3. Check robots.txt for both and report back before step 4.
4. Build and test, in this order:
   - `store.py` with the schema from PLAN.md section 4 and a `migrate()` function.
   - `fetcher.py`: robots check, 5 second per-host delay, 30 request cap per run, conditional GET, project user agent with contact email from config.
   - `normalizer.py`: content-area extraction by selector, whitespace and quote normalization, content hash.
   - `parse_health.py`: mark broken when selector missing or text shrinks more than 50%.
   - `differ.py`: section-level diff for `page` sources, new-notice detection for `index` sources, noise filtering, Title 234 filter for notices.
   - `pipeline.py` and `cli.py run` wiring fetch through diff, writing a `runs` record. No extractor yet.
5. Fixtures needed: a normal rule page, the same page with a one-paragraph change, a page with the content area missing, a Bulletin index page, and a Bulletin page with an attorney discipline notice (must produce no change).
6. Write a short README stub: what this is, that it was rebuilt from the tagged prototype, and how to run tests.

Then stop and give me:
- What was built, as a short list.
- Test count and coverage of the core modules.
- The robots.txt findings.
- Anything in PLAN.md you think is wrong or risky now that you've built part of it.

## Later milestones (do not start yet)

- Week 2: `schema.py`, `prompts/extract_v1.md`, `extractor.py`, `verifier.py`, `router.py`, `budget.py`, eval harness with response cache, review app.
- Week 3: `weekly.yml` with the `data` branch, `digest.py`, `docs/EVAL.md`, `docs/LIMITATIONS.md`, full README.

## Definition of good work here

- I can run `make install && make test` on a fresh clone and everything passes offline.
- Every guard in PLAN.md section 6 has at least one test by the end of Week 2.
- The README never claims more than the eval shows.
