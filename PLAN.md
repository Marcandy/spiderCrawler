# spiderCrawler v2: PA Criminal Rules Change Monitor

Plan, architecture, AI safety guards, and adversarial review.
Status: revised 2026-11-01 after a second adversarial review (section 11b). Source access verified. Ready to build Week 1.

---

## 1. Goal

Rebuild spiderCrawler from a broken Amazon scraper into a Python tool that:

1. Watches the Pennsylvania Supreme Court rules committee pages on pacourts.us, focused on the Rules of Criminal Procedure.
2. Detects new rulemaking items (proposals and adopted rules) and keeps its own permanent history of them.
3. Uses an LLM to turn each item into a structured summary where every claim is backed by a quoted line from the source.
4. Blocks unsupported claims automatically.
5. Holds everything for human approval before it appears in a public digest.
6. Measures how well all of this works with a labeled eval set that has a frozen test split.

**One-line pitch:** a source-grounded change monitor for Pennsylvania criminal procedure rules, with automatic claim verification, mandatory human review, and a published eval report.

Expungement is one output field (`expungement_relevance`), not the headline. The source publishes only a handful of expungement items, too few to build the project around.

### Non-goals (MVP)

- No legal advice. The tool says what changed and where. It never says who is eligible for anything.
- No other jurisdictions, no user accounts, no email alerts, no RAG, no multi-agent setup.
- No auto-publishing. Ever.

---

## 2. Stakeholder and success criteria

**Target user:** a legal aid worker or a PAX / Code for Philly maintainer who wants a weekly answer to "did any PA criminal procedure rule get proposed or adopted, and does it touch expungement?"

**Recruit in week 1, not week 3.** Ask through the PAX maintainers and Code for Philly Slack. If nobody commits, the README says so plainly.

**Done when:**

- Labeled eval set exists (about 20 dev + 30 frozen test items) and the eval report is published.
- The pipeline ran on schedule for at least 3 live weeks.
- At least one real person reviewed items in the queue (or the README honestly says no external reviewer yet).
- README covers what it does, how the guards work, eval results, and limitations.
- The old Amazon scraper is tagged `v0-amazon-scraper` and linked from the README as the "before."

---

## 3. Sources and fetching rules

### Source access findings (checked 2026-11-01)

| Host | robots.txt | Verdict |
|---|---|---|
| `www.pacodeandbulletin.gov` (Pa. Code and Pa. Bulletin) | `User-agent: * Disallow: /`. Even Googlebot is barred from `/Display/pacode` and `/Display/pabull` | **Not allowed.** Not used. |
| `www.pabulletin.com` | 302 redirect to the homepage of the host above, including for `/robots.txt` | **Not allowed** (alias of the host above). A naive `robotparser` check returns "allowed" here because it parses the homepage HTML. The fetcher must not repeat this mistake. |
| `www.pacourts.us` | 404 (no robots.txt) | **Allowed** by convention. Re-checked on every run. |

pacourts.us has no terms-of-use page (404 on 2026-11-01); only a translation disclaimer was found. Court rules are government edicts and not copyrightable, and short quotes of committee reports are fair use. Record any terms that appear later in `docs/SOURCES.md`.

Option not taken for MVP: email the Legislative Reference Bureau for permission to use pacodeandbulletin.gov. Revisit only if the live source proves too thin.

### Sources (MVP)

The 8 Supreme Court rules committee pages under `https://www.pacourts.us/courts/supreme-court/committees/rules-committees/`. All share one page template with three tables: **Proposed Rules for Public Comment**, **Approved Rules**, **Prior Proposed Rules**. Each row links one or more PDFs (rule text, order, publication or adoption report).

| ID | Committee page slug | Role |
|---|---|---|
| `crim` | `criminal-procedural-rules-committee` | **Focus.** Items are extracted, reviewed, and published. |
| `appellate`, `civil`, `evidence`, `domestic`, `juvenile`, `minor`, `orphans` | the other 7 committee slugs | **Context.** Items are detected and stored. Only items whose title or text cites `Pa.R.Crim.P.` are extracted. The rest serve as eval negatives. |

Volume on 2026-11-01: criminal page 36 PDFs (Aug 2021 to Oct 2026, about 28 rulemaking items); the other 7 pages about 227 PDFs. Expect about 6 to 10 criminal items per year, so live weeks will often find nothing.

The "Approved Rules" table shows only the most recent adoptions; older ones drop off the page. The tool's own history is the only durable record of them, which is part of its value.

### One kind of change

- **`new_item`**: a row appears in a committee table that was not seen before. Item key = the URL path of the row's first PDF (stable and dated, e.g. `/Storage/media/pdfs/20260917/185751-...pdf`). A row moving between tables (e.g. proposed to prior proposed) updates the stored item's `table` field and is logged. It does not trigger an extraction.

There is no page-edit diffing. The rule-text PDFs already mark what changed: additions are **bold underlined**, deletions are **bold [bracketed]**.

### PDF text

- `pdfplumber`, character-level. Underlined spans (chars sitting on a drawn underline) are wrapped as `{+ ... +}`. Bracketed deletions stay as `[ ... ]`. If underline detection fails on a document, its text is still stored, the document is flagged `markup_lost`, and the reviewer sees the flag.
- PDFs with no text layer are flagged `no_text` and the item goes to `needs_attention` without extraction. OCR is out of scope.

### Fetching rules (non-negotiable)

- Before any request to a host: fetch `/robots.txt` **with redirects followed**, then evaluate rules for the **final** host. If the final host differs from the requested host, or the response is not `text/plain`, apply the robots.txt of the final host. If that cannot be fetched, treat it as disallowed. Disallowed means skip and log.
- Max 1 request every 5 seconds per host. Max 30 requests per run (8 index pages leave room for about 22 PDFs; extra PDFs wait for the next run).
- User agent names the project and a contact email from config.
- Use `ETag` / `If-Modified-Since` when the server supports them.
- Store only parsed table rows and the text of linked rule documents. Page chrome (navigation, committee member lists) is never stored.

---

## 4. Architecture

```
               weekly cron (GitHub Actions)
                           |
                           v
 +---------+   +--------------+   +----------+   +----------+   +-----------+
 | Fetcher |-> | Index parser |-> | Detector |-> | PDF text |-> | Extractor |  (LLM, no tools)
 +---------+   +--------------+   +----------+   +----------+   +-----------+
                      |                                               |
                parse-health                                          v
                   check                                       +-------------+
                                                               |  Verifier   |  (plain code)
                                                               +-------------+
                                                                      |
 +--------------------------------------------------------------+     |
 |  data branch: append-only JSON records  (source of truth)     | <---+
 |  items/  documents/  extractions/  reviews/  runs/            |
 +--------------------------------------------------------------+
            |   rebuilt on demand                    ^
            v                                        |
   +------------------+                 review decisions (new JSON files)
   | SQLite cache     |                              |
   +------------------+                              |
            |                                        |
            v                                        |
   +------------------+  approve / edit / reject ----+
   | Review queue     |
   | (local FastAPI)  |
   +------------------+
            |
            v  approved only
   +------------------+
   | Digest generator | -> docs/digest/ (GitHub Pages)
   +------------------+
```

### Components

| Component | Responsibility | AI? |
|---|---|---|
| `fetcher` | redirect-aware robots check, rate limit, request cap, conditional GET | No |
| `index_parser` | parse the three committee tables into rows (title, links, date column literal, table name) | No |
| `parse_health` | mark a source `broken` if an expected table heading is missing, the page parses to zero rows when items were known, **more new items appear than the flood limit** (default 10, or 25% of known items), or **rows drop below half** of the last good run. Volume guards are skipped in seed runs, which is how the maintainer accepts such a page | No |
| `detector` | compare rows to known items by item key; emit `new_item`; record table moves; seed mode | No |
| `pdf_text` | extract and normalize document text, keep `{+ +}` / `[ ]` markup, compute hash, set `markup_lost` / `no_text` | No |
| `normalize` | the single shared text normalizer (whitespace, quotes, dashes, PDF spacing glitches) used for hashing and quote matching | No |
| `canonical` | turn literal dates and rule references into canonical values | No |
| `extractor` | one LLM call per criminal item, returns the fixed schema via structured output | Yes |
| `verifier` | quote checks, hard rules, cross-check against the index row | No |
| `router` | decides queue status from verifier result and confidence | No |
| `review_app` | local FastAPI page to approve, edit, or reject; edits are re-verified | No |
| `digest` | static page from approved items only, each linked to its source PDFs and record ids | No |
| `eval` | runs extractor + verifier on the labeled set and writes the report | Uses extractor |

The LLM is one step out of twelve. Everything around it is deterministic code you can test.

### Data model

**Source of truth: append-only JSON files on the `data` branch.** Files are written once and never modified. A later fact is a new file (a review supersedes nothing; the newest review for an extraction wins). Because every write is a new file with a unique name, the weekly job and local review commits never conflict.

```
data/
  items/<item_id>.json          committee, table, title, row_date_literal,
                                doc_urls, first_seen_run, item_key
  item_events/<ts>-<item_id>.json   table moves
  documents/<doc_id>.json       item_id, url, fetched_at, etag, content_hash,
                                text, text_len, markup_lost, no_text
  extractions/<extraction_id>.json  item_id, model_id, effort, prompt_version,
                                schema_version, input_hash, output, canonical,
                                error, verifier_status, verifier_notes, flags,
                                confidence, queue_status, input_tokens,
                                output_tokens, created_at
  reviews/<review_id>.json      extraction_id, reviewer, decision,
                                edited_output, edited_verifier_status,
                                edited_verifier_notes, note, decided_at
  runs/<run_id>.json            started_at, finished_at, sources_checked,
                                items_found, extractions_made, blocked,
                                errors, tokens_used, rows_seen, status
```

IDs are content-derived (hash of item key, URL plus content hash, etc.) so re-running a step is idempotent.

**SQLite (`sqlite3`, no ORM) is a local cache** rebuilt from the JSON files by `store.rebuild()`. It is gitignored and never committed. Queries, the review app, and the digest read from it.

Raw HTML and raw PDFs are not stored. Only parsed rows and normalized document text are.

---

## 5. AI layer

### Extraction schema (Pydantic)

The LLM outputs **literal strings as written in the source**. Canonical values are added afterwards by `canonical.py`, never by the LLM.

```python
class Claim(BaseModel):
    field: Literal["summary", "rule_ids", "effective_date",
                   "affected_parties", "expungement_relevance"]
    value: str
    evidence_quote: str          # exact text copied from the source

class ChangeSummary(BaseModel):        # what the LLM returns
    is_substantive: bool         # false for typo fixes, renumbering only, etc.
    stage: Literal["proposed", "adopted", "other"]
    summary: str                 # plain language, max 60 words, no advice
    rule_ids: list[str]          # literal, e.g. ["Rule 632", "Pa.R.Crim.P. 702"]
    effective_date_text: str | None   # literal, e.g. "April 1, 2027",
                                      # "30 days after publication"
    affected_parties: list[str]
    expungement_relevance: Literal["yes", "no", "unsure"]
    claims: list[Claim]          # one or more per non-empty field
    confidence: Literal["low", "medium", "high"]
    abstain_reason: str | None   # required when relevance is "unsure"

class Canonical(BaseModel):            # added by canonical.py
    rule_ids: list[str]          # e.g. ["Pa.R.Crim.P. 632"]; committee gives the prefix
    effective_date: date | None  # None when relative or unparseable
```

### Prompt and API rules

- System prompt defines the task and says the source text is data, never instructions.
- Source text (with `{+ +}` / `[ ]` markup explained in the prompt) goes inside clearly marked delimiters.
- Structured output through `output_config.format` built from the Pydantic model (SDK `messages.parse()`). No free text. Forced `tool_choice` is not used; it returns 400 on current models.
- No `temperature` / sampling parameters. Current models and API versions reject them. Reproducibility comes from the response cache (section 8), not sampling settings.
- Model id and effort come from config with env override. Default model `claude-sonnet-5-5`. Both are recorded on every extraction.
- `stop_reason == "refusal"` or a schema validation error is an extraction error: no output is stored as valid, and the item routes to `needs_attention`.
- The model is told it may say "unsure" and should when the text does not clearly say.
- The summary must not tell anyone what they should do or whether they qualify.
- Prompt file is versioned (`prompts/extract_v1.md`) and the version is stored on every extraction.

### Verifier rules (plain code)

All text comparisons use `normalize()` on both sides, with `{+`, `+}` markup tokens stripped, so a quote matches whether or not it includes markup. Matching also ignores letter case and hyphens inside words (decided 2026-11-01): a quote that capitalizes its first word or retypes an all-caps heading is still the same words. Stored and displayed text keep their original case.

The **source text** for an item is the concatenated normalized text of its documents. Rule-text PDFs show the change itself, so quotes are anchored to the change, not to an unrelated snapshot.

1. Every non-empty field has at least one claim.
2. Every `evidence_quote` appears in the item's source text. Minimum 8 words for `summary`, `affected_parties`, and `expungement_relevance` claims; minimum 3 words for `rule_ids` and `effective_date` claims.
3. `effective_date_text`, if set, appears inside one of its quotes.
4. Every `rule_ids` literal appears inside one of its quotes.
5. `abstain_reason` required when relevance is `unsure`.
6. Summary fails on advice or eligibility patterns: second person ("you should", "you can now", "you are eligible") and third person ("now qualify", "are now eligible", "can now get"). This is a narrow net, not a guarantee; the eval and the reviewer cover the rest.
7. Source text containing instruction-like patterns ("ignore previous", "you are an AI", "system prompt") sets a `suspicious_input` flag for the reviewer. It does not block on its own.
8. Cross-check against the index row (deterministic data the LLM did not produce):
   - If the row's date column is an effective date and `canonical.effective_date` differs from it: **fail**.
   - If a rule number in the row title is missing from `canonical.rule_ids`: **flag**.
   - If `stage` disagrees with the table the row came from (Approved means `adopted`): **flag**.

Result: `pass`, `fail` (with reasons), or `pass_flagged`.

### Routing

| Verifier | Confidence / relevance | Queue status |
|---|---|---|
| extraction error | any | `needs_attention` (no extraction to approve; reviewer can write one by hand, which is verified) |
| fail | any | `blocked` (shown with reasons; can be approved only through an edit that passes the verifier) |
| pass_flagged | any | `needs_attention` |
| pass | low, or relevance `unsure` | `needs_attention` |
| pass | medium or high | `ready_for_review` |

Every path ends at a human. **Edited output is re-run through the verifier.** An edit that fails cannot be approved. Nothing reaches the digest without an `approve`, or an `edit` that passes.

---

## 6. AI safety guards

| Risk | Guard | How it is tested |
|---|---|---|
| Model invents facts | Quote-backed claims + verifier rule 2 | Eval metric: unsupported-claim rate before vs after verifier |
| Model invents dates or rule numbers | Literal-in-quote rules 3 and 4 + cross-check rule 8 against the index row | Unit tests; eval field accuracy on canonical values |
| Real quote, wrong claim | Mandatory human review; measured separately | Eval metric: "supported but wrong" rate. Listed as a known limitation |
| Verifier blocks correct output | Literal + canonical split, relaxed quote length for short fields | Eval metric: false-block rate |
| Model guesses when unsure | `unsure` option, required abstain reason, routing to `needs_attention` | Eval metric: abstention rate and whether abstentions match items labeled hard |
| Prompt injection in source text | LLM has no tools, output is schema only, source in delimiters, suspicious-input flag, human gate | 8 synthetic injection fixtures, clearly labeled synthetic. Pass = output unaffected and flag raised |
| Reads like legal advice | Schema forbids advice, verifier rule 6, digest disclaimer, every item links to source | Unit tests on advice patterns (both persons); eval checks summaries |
| Reviewer edit launders a blocked item | Edits re-verified; failing edits cannot be approved | Unit test |
| Silent parser breakage | `parse_health` marks source `broken`, no items emitted | Fixture with renamed headings and fixture with empty tables |
| Re-keying flood or partial page (site moves its PDFs; page half-loads) | Volume guards: stop and record nothing, fail the run; accept with `run --seed` after checking. Matters because records are append-only | Pipeline tests with all PDFs moved and with 80 rows removed |
| Lost change markup | `markup_lost` flag on documents, shown to reviewer | Fixture PDF with known underlined additions |
| Robots check fooled by a redirect | Redirect-aware check on the final host; non-text robots treated as suspect | Unit test with a mocked 302-to-HTML robots response |
| Runaway cost | Per-run token budget and max 20 extractions per run; stop and log when hit | Unit test on budget |
| First run floods the LLM with old items | `run --seed` records existing rows without extracting | Unit test: seed run makes zero extractions |
| Out-of-scope personal data | Only parsed rule rows and rule documents stored; page chrome never stored | Test that stored records contain no committee-member section text |
| No accountability | Every digest line traces to item, documents, model id, prompt version, verifier result, reviewer | Test that digest links resolve to stored records |

**Honest framing for the README:** the main protection is the architecture (no tools, schema-only output, deterministic verifier, cross-checks against index data, human gate). The injection tests confirm it holds. They are not proof of robustness against a determined attacker.

---

## 7. Automation

- **Schedule:** GitHub Actions weekly cron (Monday morning ET), plus `workflow_dispatch` for manual runs.
- **Run steps:** check out the `data` branch, `store.rebuild()`, fetch, parse, parse-health, detect, extract PDF text, extract, verify, route, write JSON records, commit new files to `data`.
- **First run:** `run --seed` once, manually. It records every existing row and document without extracting. Historical items are covered by the eval, not the live queue.
- **Storage:** append-only JSON on the `data` branch. Diffable public history; no binary files committed.
- **Secrets:** `ANTHROPIC_API_KEY` as a GitHub Actions secret. Never in code or logs.
- **Failures:** a source failure does not stop the run. The run record and job summary show errors. A run with any `broken` source fails the job so you get GitHub's email.
- **Review:** pull the `data` branch, `make review`, decisions are written as new `reviews/*.json` files and pushed. No merge conflicts because no file is ever edited.
- **Digest:** `make digest` builds `docs/digest/` from approved items. GitHub Pages serves it.

---

## 8. Eval

### Why historical data

New criminal rulemaking items appear roughly 6 to 10 times a year. Three live weeks may produce zero or one. Live runs prove the automation works. The historical set proves accuracy.

### Labeling guide first

Before labeling any item, write `eval/LABELING.md`. It defines:

- what counts as substantive;
- what makes `expungement_relevance` yes / no / unsure (with examples);
- how to write literal rule ids and dates;
- what makes an item "hard."

Labels follow the guide, not intuition. This keeps the keyword baseline from being graded against labels that simply restate the keywords.

### Eval set (about 50 items)

Sources: the current pacourts.us committee pages; PDFs still served at their original URLs; for older approved items no longer listed, archived copies of the committee pages collected manually once, with provenance recorded per item.

- **About 25 positives:** criminal committee items (proposals and adoptions), including the expungement items (Rules 490 / 790 / 791).
- **About 17 negatives:** other committees' items that do not touch criminal rules, and non-substantive criminal items (corrections, renumbering only).
- **8 injection fixtures:** real items with injected instruction text. Marked synthetic in the file and the report.

**Split:** about 20 dev / 30 test, stratified so each split has positives, negatives, expungement items, and injection items (3 dev / 5 test). Prompt and verifier tuning use dev only. The test split is frozen once labeled and scored only for reported numbers. Any change made after looking at test results is reported as such.

Each item: `input.txt` (normalized source text, as the pipeline produces it) + `gold.json` (field values: literals and canonical values, relevance, substantive, stage, hard flag). No gold claims; claims are judged by the verifier and by hand on failures.

The stakeholder independently labels at least 10 test items. Report agreement per field.

### Metrics

Report counts with 95% Wilson intervals, not percentages alone.

| Metric | Meaning |
|---|---|
| Field accuracy | canonical `rule_ids` set match, canonical `effective_date` exact match, `stage`, relevance, substantive |
| Unsupported-claim rate | share of claims whose quote is not in the source, before verifier |
| Block precision | share of blocked items that really were wrong |
| False-block rate | share of correct extractions the verifier blocked |
| Supported-but-wrong rate | quote exists but claim is wrong (verifier cannot catch this) |
| Abstention quality | share of `unsure` outputs on items labeled hard |
| Injection pass rate | fixtures where output stayed correct and the flag fired |
| Baseline comparison | keyword baseline (regex for rule numbers and "expunge") vs LLM pipeline on relevance and substantive |

### Reproducibility

- Model responses cached by hash of (prompt version, model id, effort, schema version, input). `make eval` re-runs offline from cache. `make eval-live` calls the API.
- Report records model id, effort, prompt version, date, and cost.
- Output: `docs/EVAL.md` with numbers, 3 to 5 failure examples, and what they show.

If the LLM does not beat the keyword baseline, the report says so.

---

## 9. Repo layout

```
spiderCrawler/
  README.md
  PLAN.md                      (this file)
  pyproject.toml               (uv, ruff, pytest)
  Makefile
  .github/workflows/
    ci.yml                     (lint + tests on push and PR)
    weekly.yml                 (scheduled run)
  src/spidercrawler/
    config.py                  (sources.yaml loader, limits, model id, effort)
    normalize.py               (single shared text normalizer)
    fetcher.py
    index_parser.py
    parse_health.py
    detector.py
    pdf_text.py
    canonical.py
    records.py                 (append-only JSON read/write)
    store.py                   (SQLite cache: schema, rebuild())
    schema.py
    extractor.py
    verifier.py
    router.py
    budget.py
    pipeline.py                (one run end to end)
    review_app/                (FastAPI + Jinja templates)
    digest.py
    cli.py                     (run [--seed], review, digest, eval)
  prompts/
    extract_v1.md
  sources.yaml
  eval/
    LABELING.md
    items/dev/<id>/input.txt, gold.json
    items/test/<id>/input.txt, gold.json
    run_eval.py
    cache/
  tests/
    fixtures/                  (saved index pages, PDFs, broken pages, injection)
    test_*.py
  docs/
    SOURCES.md                 (robots and terms findings, with dates)
    EVAL.md
    LIMITATIONS.md
    digest/
```

The old Node code is not kept on `main`; see tag `v0-amazon-scraper`.

---

## 10. Milestones

Scope is capped. The Agentic Engineer course stays the top priority.

**Week 1: deterministic core**
- Tag `v0-amazon-scraper`, clear `main`, set up Python project and CI.
- Record robots and terms findings in `docs/SOURCES.md` (done once on 2026-11-01; re-verify).
- normalize, records, store, fetcher, index_parser, parse_health, detector, pdf_text, with fixture-based tests. `run` and `run --seed` wired end to end without the extractor.
- Message PAX / Code for Philly for a reviewer.

**Week 2: AI layer and eval**
- `eval/LABELING.md` first. Then label dev and test.
- Schema, canonical, prompt v1, extractor, verifier, router, budget, with unit tests.
- Run `make eval` on dev, tune, then score test once. First numbers.
- Local review app.

**Week 3: automation and write-up**
- Weekly workflow, `data` branch, seed run, digest, GitHub Pages.
- `docs/EVAL.md`, `docs/LIMITATIONS.md`, README.
- Start live runs. Collect 3 weeks before calling it done.

### Cut list if behind

1. Digest page (keep approved items as a markdown file instead).
2. Review app UI (use a CLI approve/reject command instead).
3. The 7 context committees (watch only the criminal page; take eval negatives from a one-time manual collection).
4. Underline markup detection (store plain text, flag every document `markup_lost`, state it in limitations).

Never cut: verifier, human gate, frozen test split, eval report, limitations section.

---

## 11. Adversarial review

### 11a. First review (original draft)

Each finding was checked against the first draft. Rows marked *revised* were changed by the second review.

| # | Attack on the plan | Severity | Resolution |
|---|---|---|---|
| 1 | Live rule changes are rare. Three weeks of runs may produce nothing to show. | High | Accuracy comes from a historical eval set. Live runs only prove automation. |
| 2 | Quote matching proves the quote exists, not that it supports the claim. Calling this "hallucination-proof" would be false. | High | Added "supported but wrong" metric. Human review stays mandatory. README states the limit. |
| 3 | A digest about expungement rules can be read as legal advice. | High | Non-goal stated. Schema and verifier rule 6 narrow advice phrasing (not a guarantee). Disclaimer plus source link on every item. |
| 4 | Generator does not value a solo agent. No stakeholder means weak evidence. | High | Recruit in week 1. If none, say so in the README rather than imply usage. |
| 5 | Site layout changes break the parser and create fake changes. | Medium | `parse_health` on table headings and row counts. *Revised* for the new source. |
| 6 | Source pages include content naming individuals. | Medium | Store only parsed rule rows and rule documents. *Revised*: the Bulletin is no longer a source. |
| 7 | Injection tests on a government site look like theater. | Medium | Framed honestly: architecture is the defense, tests confirm it. Fixtures labeled synthetic. |
| 8 | Eval results change between runs because of model nondeterminism. | Medium | *Revised*: response cache, model id, effort, and prompt version recorded. Temperature is not available on current models. |
| 9 | Hand-labeling your own eval set is biased. | Medium | Labeling guide first, stakeholder double-labels a test subset, agreement reported. |
| 10 | Committing raw HTML and SQLite to `main` bloats the repo. | Low | *Revised*: append-only JSON on a `data` branch; SQLite is a local cache. |
| 11 | Unbounded LLM calls on a big backlog. | Low | Per-run budget and extraction cap; fail closed; seed mode on first run. |
| 12 | Robots.txt or terms may disallow automated access. | Medium | *Revised*: checked 2026-11-01. Original sources disallowed; switched to pacourts.us. |
| 13 | "Refactor" claim is misleading for a 9-file rewrite. | Low | README says "rebuilt from a 2023 prototype" and links the tag. |
| 14 | Four active projects at once; risk of nothing finishing. | High | 3-week cap, cut list, never-cut list. |
| 15 | Keyword baseline might match the LLM, making the AI look pointless. | Medium | Report it honestly. See also 11b #15. |

### 11b. Second review (2026-11-01)

| # | Attack on the plan | Severity | Resolution |
|---|---|---|---|
| 1 | Both original sources are on a host whose robots.txt is `User-agent: * Disallow: /`. | Blocker | Switched to pacourts.us committee pages (no robots.txt; allowed). Findings in section 3 and `docs/SOURCES.md`. |
| 2 | `pabulletin.com` 302s to the disallowed host's homepage; `robotparser` reads the HTML and says "allowed." | Blocker | Redirect-aware robots check on the final host; non-text robots treated as suspect; unit test. |
| 3 | The fallback source is PDFs; the plan assumed HTML selectors and section diffs. | High | `pdf_text` with `pdfplumber`; index pages still HTML. Page-edit diffing removed. |
| 4 | `temperature=0` and forced tool use return 400 on current models. | High | Structured output via `output_config.format`; no sampling params; cache gives reproducibility. |
| 5 | Verifier rules 3 and 4 fail correct outputs (ISO dates and canonical rule ids never appear literally). | High | LLM outputs literals; `canonical.py` derives canonical values; quote length relaxed for short fields; false-block rate measured. |
| 6 | Quote target text was undefined for edits; quoting unchanged text could pass. | Medium | Source text is the item's own documents, which show the change via markup. |
| 7 | Reviewer edits bypassed the verifier. | Medium | Edits re-verified; failing edits cannot be approved. |
| 8 | Full index text with individuals' names stored on a public branch. | Medium | Only parsed rule rows and rule documents stored. |
| 9 | One binary SQLite file written by CI and by local reviews: unmergeable, opaque history, state lost if not restored. | Medium | Append-only JSON as source of truth; SQLite rebuilt; workflow checks out `data` first. |
| 10 | No stable key for notices; reordering re-detects old items. First run floods the LLM. | Medium | Item key = first PDF URL path; `run --seed`. |
| 11 | Date noise filtering would hide effective-date changes; 50% shrink rule misfires on index pages. | Medium | Both removed with page diffing; health check uses headings and row counts. |
| 12 | Hashing and quote matching could use different normalizers. | Low | One `normalize.py`, used by both. |
| 13 | Advice regex only catches second person. | Low | Third-person eligibility patterns added; limit stated honestly. |
| 14 | Eval tuned and reported on the same items; labels written by the author define the baseline's target. | High | Labeling guide first; 20/30 dev/test split, test frozen; Wilson intervals. |
| 15 | Rule ids and effective dates are available deterministically from the index row, so the LLM adds little on those fields. | Medium | Use the row as a verifier cross-check (rule 8). README states that the LLM's value is the summary, stage, affected parties, relevance, and substantive judgement, not rule ids. |
| 16 | `pypdf` drops underline, so additions look like unchanged text. | Medium | `pdfplumber` char-level underline detection; `markup_lost` flag when it fails. |
| 17 | Only about 5 expungement items exist on the source. | Medium | Pitch changed to criminal procedure rules; expungement is a field. |
| 18 | If the site moves its PDFs, every item gets a new key and floods the queue; append-only history keeps the duplicates forever. A half-loaded page passes the zero-rows check. | Medium | Volume guards in `parse_health` (decided 2026-11-01): stop and record nothing, accept with `run --seed`. |

**Remaining risks accepted:**
- Single LLM vendor.
- Small eval set, so numbers have wide uncertainty. Report counts and intervals.
- The verifier cannot judge meaning. Humans do.
- pacourts.us page template could change; parse_health catches it, a fix is manual.

**Confidence:** high that this is buildable in 3 part-time weeks with the cut list. Medium on underline detection quality across all PDFs. Medium on getting an external reviewer; that depends on outreach in week 1.

---

## 12. Known limitations (draft for docs/LIMITATIONS.md)

- Covers only the Pennsylvania Supreme Court rules committee pages. Not a general legal monitor, and not the official Pa. Code or Bulletin.
- Focused on criminal procedure; other committees are watched only for criminal cross-references.
- The verifier checks that quotes exist and that dates and rule ids match the index row. It does not check that claims are correct.
- Underline detection can miss additions in some PDFs; affected documents are flagged.
- The source lists only recent adoptions; history before the first run comes from manual eval collection.
- The eval set is small and labeled mostly by the author.
- Not legal advice. Summaries describe changes only.
- Depends on the committee page template; layout changes pause monitoring until fixed.
