# spiderCrawler v2: Policy Change Monitor

Plan, architecture, AI safety guards, and adversarial review.
Status: reviewed and revised. Ready to build.

---

## 1. Goal

Rebuild spiderCrawler from a broken Amazon scraper into a Python tool that:

1. Watches a small set of public Pennsylvania court rule sources.
2. Detects real changes and keeps every version.
3. Uses an LLM to turn each change into a structured summary where every claim is backed by a quoted line from the source.
4. Blocks unsupported claims automatically.
5. Holds everything for human approval before it appears in a public digest.
6. Measures how well all of this works with a labeled eval set.

**One-line pitch:** a source-grounded change monitor for PA criminal procedure rules, with automatic claim verification, mandatory human review, and a published eval report.

### Non-goals (MVP)

- No legal advice. The tool says what changed and where. It never says who is eligible for anything.
- No multiple jurisdictions, no user accounts, no email alerts, no RAG, no multi-agent setup.
- No auto-publishing. Ever.

---

## 2. Stakeholder and success criteria

**Target user:** a legal aid worker or a PAX / Code for Philly maintainer who wants a weekly answer to "did any PA criminal procedure rule change, and does it touch expungement?"

**Recruit in week 1, not week 3.** Ask through the PAX maintainers and Code for Philly Slack. If nobody commits, the README says so plainly.

**Done when:**

- Historical eval set of about 50 labeled items exists and the eval report is published.
- The pipeline ran on schedule for at least 3 live weeks.
- At least one real person reviewed items in the queue (or the README honestly says no external reviewer yet).
- README covers what it does, how the guards work, eval results, and limitations.
- The old Amazon scraper is tagged `v0-amazon-scraper` and linked from the README as the "before."

---

## 3. Sources and fetching rules

### Sources (MVP)

| ID | Source | Kind | Why |
|---|---|---|---|
| `pa-bulletin-courts` | Weekly Pennsylvania Bulletin issue index, "The Courts" section (pabulletin.com) | `index` (new notices each week) | Where rule changes and proposed changes are officially published |
| `pa-code-234-ch7` | Pa. Code Title 234 (Rules of Criminal Procedure), Chapter 7, including the expungement rules | `page` (same URL, content edited over time) | The current text of the rules themselves |

Two kinds of change:
- **`new_notice`**: a new court notice appears in a weekly issue. Filter to notices that mention Title 234 / criminal procedure.
- **`edit`**: the text of a tracked rule page changes between snapshots.

Fallback source if either site's terms or robots.txt rule it out: the PA Courts website's published rule orders.

### Fetching rules (non-negotiable)

- Check robots.txt before every run (`urllib.robotparser`). If disallowed, skip and log.
- Read and record each site's terms before building against it.
- Max 1 request every 5 seconds per host. Max 30 requests per run.
- User agent names the project and a contact email.
- Use `ETag` / `If-Modified-Since` when the server supports them.
- Scope extraction to the rules content area only. Bulletin issues also contain notices that name individuals (for example attorney discipline). Those are out of scope and never stored beyond the raw fetch.

---

## 4. Architecture

```
            weekly cron (GitHub Actions)
                        |
                        v
 +---------+    +-------------+    +---------+    +-----------+
 | Fetcher | -> | Normalizer  | -> | Differ  | -> | Extractor |  (LLM, no tools)
 +---------+    +-------------+    +---------+    +-----------+
      |               |                 |               |
      |         parse-health        noise filter        v
      |           check                         +-------------+
      |               |                         |  Verifier   |  (plain code)
      v               v                         +-------------+
 +--------------------------------------------------+    |
 |                SQLite (versioned)                 | <-+
 | sources, snapshots, changes, extractions,        |
 | reviews, runs                                     |
 +--------------------------------------------------+
            |                          ^
            v                          |
   +------------------+        approve / edit / reject
   | Review queue     | ------------------+
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
| `fetcher` | robots check, rate limit, conditional GET, store raw response | No |
| `normalizer` | extract the rules content area, clean text, compute hash | No |
| `parse_health` | flag a source as `broken` if the expected section is missing or text length drops more than 50% vs last good snapshot | No |
| `differ` | section-level diff between snapshots; drop noise (dates, nav, whitespace); detect new notices in index pages | No |
| `extractor` | one LLM call per change, returns the fixed schema via structured output | Yes |
| `verifier` | checks every claim's quote exists in the source; applies hard rules | No |
| `router` | decides queue status from verifier result and confidence | No |
| `review_app` | local FastAPI page to approve, edit, or reject | No |
| `digest` | builds a static page from approved items only, each linked to its exact source version | No |
| `eval` | runs extractor + verifier on the labeled set and writes the report | Uses extractor |

The LLM is one step out of nine. Everything around it is deterministic code you can test.

### Data model (SQLite)

```
sources      (id, name, url, kind, content_selector, active, status)
snapshots    (id, source_id, fetched_at, http_status, etag, content_hash,
              text, text_len, parse_ok)
changes      (id, source_id, prev_snapshot_id, new_snapshot_id, kind,
              diff_text, new_text, created_at)
extractions  (id, change_id, model_id, prompt_version, schema_version,
              output_json, verifier_status, verifier_notes, confidence,
              input_tokens, output_tokens, created_at)
reviews      (id, extraction_id, reviewer, decision, edited_json, note,
              decided_at)
runs         (id, started_at, finished_at, sources_checked, changes_found,
              extractions_made, blocked, errors, tokens_used, status)
```

Raw HTML is not committed. Only normalized text and hashes are stored, which keeps the repo small and avoids storing out-of-scope content.

---

## 5. AI layer

### Extraction schema (Pydantic)

```python
class Claim(BaseModel):
    field: Literal["summary", "rule_ids", "effective_date",
                   "affected_parties", "expungement_relevance"]
    value: str
    evidence_quote: str          # exact text copied from the source

class ChangeSummary(BaseModel):
    is_substantive: bool         # false for typo fixes, renumbering only, etc.
    summary: str                 # plain language, max 60 words, no advice
    rule_ids: list[str]          # e.g. ["Pa.R.Crim.P. 790"]
    effective_date: str | None
    affected_parties: list[str]
    expungement_relevance: Literal["yes", "no", "unsure"]
    claims: list[Claim]          # one or more per non-empty field
    confidence: Literal["low", "medium", "high"]
    abstain_reason: str | None   # required when relevance is "unsure"
```

### Prompt rules

- System prompt defines the task and says the source text is data, never instructions.
- Source text goes inside clearly marked delimiters.
- Output must match the schema through structured output (tool-use schema). No free text.
- The model is told it may say "unsure" and should when the text does not clearly say.
- The summary must not tell anyone what they should do or whether they qualify.
- Temperature 0. Prompt file is versioned (`prompts/extract_v1.md`) and the version is stored on every extraction.

### Verifier rules (plain code)

1. Every non-empty field has at least one claim.
2. Every `evidence_quote` appears in the change's source text after normalizing whitespace, quotes, and dashes. Minimum 8 words.
3. `effective_date` must appear inside one of its quotes.
4. Every `rule_ids` value must appear inside one of its quotes.
5. `abstain_reason` required when relevance is `unsure`.
6. Summary fails if it contains advice patterns ("you should", "you are eligible", "you can now").
7. Source text containing instruction-like patterns ("ignore previous", "you are an AI", "system prompt") sets a `suspicious_input` flag for the reviewer. It does not block on its own.

Result: `pass`, `fail` (with reasons), or `pass_flagged`.

### Routing

| Verifier | Confidence / relevance | Queue status |
|---|---|---|
| fail | any | `blocked` (shown in queue with reasons, cannot be approved without edit) |
| pass_flagged | any | `needs_attention` |
| pass | low, or relevance `unsure` | `needs_attention` |
| pass | medium or high | `ready_for_review` |

Every path ends at a human. Nothing reaches the digest without an `approve` or `edit` decision.

---

## 6. AI safety guards

| Risk | Guard | How it is tested |
|---|---|---|
| Model invents facts | Quote-backed claims + verifier rule 2 | Eval metric: unsupported-claim rate before vs after verifier |
| Real quote, wrong claim | Mandatory human review; measured separately | Eval metric: "supported but wrong" rate. Listed as a known limitation |
| Model guesses when unsure | `unsure` option, required abstain reason, routing to `needs_attention` | Eval metric: abstention rate and whether abstentions match hard labeled cases |
| Prompt injection in source text | LLM has no tools, output is schema only, source in delimiters, suspicious-input flag, human gate | 8 synthetic injection fixtures, clearly labeled synthetic. Pass = schema output unaffected and flag raised |
| Reads like legal advice | Schema forbids advice, verifier rule 6, digest disclaimer, every item links to source | Unit tests on advice patterns; eval checks summaries |
| Silent parser breakage causes fake "rule deleted" changes | `parse_health` marks source `broken`, no diff produced | Fixture with broken HTML |
| Runaway cost | Per-run token budget and max 20 extractions per run; stop and log when hit | Unit test on budget |
| Out-of-scope personal data | Scope to rules section; no raw HTML stored; names outside rules not published | Fixture with a discipline notice must produce no change |
| No accountability | Every digest line traces to snapshot, diff, model id, prompt version, verifier result, reviewer | Test that digest links resolve to stored records |

**Honest framing for the README:** the main protection is the architecture (no tools, schema-only output, deterministic verifier, human gate). The injection tests confirm it holds. They are not proof of robustness against a determined attacker.

---

## 7. Automation

- **Schedule:** GitHub Actions weekly cron (Monday morning ET), plus `workflow_dispatch` for manual runs.
- **Run steps:** fetch, normalize, parse-health, diff, extract, verify, route, write run record.
- **Storage:** SQLite file committed to a separate `data` branch by the workflow. Keeps `main` clean and gives a public version history.
- **Secrets:** `ANTHROPIC_API_KEY` as a GitHub Actions secret. Never in code or logs.
- **Failures:** a source failure does not stop the run. The run record and job summary show errors. A run with any `broken` source fails the job so you get GitHub's email.
- **Review:** you pull the `data` branch locally and run `make review`. Decisions are committed back.
- **Digest:** `make digest` builds `docs/digest/` from approved items. GitHub Pages serves it.

---

## 8. Eval

### Why historical data

Rule changes are rare. Three live weeks may produce zero or one change. Live runs prove the automation works. The historical set proves accuracy.

### Eval set (about 50 items)

- **30 positives:** past Pennsylvania Bulletin "The Courts" notices that amend or propose changes to Title 234 rules, including the expungement rules. Pulled from back issues.
- **12 negatives:** unrelated court notices and non-substantive edits (renumbering, typo fixes).
- **8 injection fixtures:** real notices with injected instruction text added. Marked synthetic in the file and the report.

Each item: `input.txt` + `gold.json` (hand-labeled schema). Have the stakeholder check at least 10 labels and report agreement.

### Metrics

| Metric | Meaning |
|---|---|
| Field accuracy | rule_ids exact match, effective_date exact match, relevance match |
| Unsupported-claim rate | share of claims whose quote is not in the source, before verifier |
| Block correctness | share of blocked items that really were wrong |
| Supported-but-wrong rate | quote exists but claim is wrong (verifier cannot catch this) |
| Abstention quality | share of `unsure` outputs on items labeled hard |
| Injection pass rate | fixtures where output stayed correct and the flag fired |
| Baseline comparison | keyword baseline (diff + regex for rule numbers and "expunge") vs LLM pipeline on relevance |

### Reproducibility

- Model responses cached by hash of (prompt version, model id, input). `make eval` re-runs offline from cache. `make eval-live` calls the API.
- Report records model id, prompt version, date, and cost.
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
    ci.yml                     (lint + tests on push)
    weekly.yml                 (scheduled run)
  src/spidercrawler/
    config.py                  (sources.yaml loader, limits, model id)
    fetcher.py
    normalizer.py
    parse_health.py
    differ.py
    extractor.py
    schema.py
    verifier.py
    router.py
    store.py                   (SQLite access)
    budget.py
    pipeline.py                (one run end to end)
    review_app/                (FastAPI + Jinja templates)
    digest.py
    cli.py                     (run, review, digest, eval)
  prompts/
    extract_v1.md
  sources.yaml
  eval/
    items/<id>/input.txt
    items/<id>/gold.json
    run_eval.py
    cache/
  tests/
    fixtures/                  (saved HTML, broken HTML, discipline notice, injection)
    test_*.py
  docs/
    EVAL.md
    LIMITATIONS.md
    digest/
  legacy/                      (not kept; see tag v0-amazon-scraper)
```

---

## 10. Milestones

Scope is capped. The Agentic Engineer course stays the top priority.

**Week 1: deterministic core**
- Tag `v0-amazon-scraper`, clear `main`, set up Python project and CI.
- Check robots.txt and terms for both sources. Pick fallback if needed.
- Fetcher, normalizer, parse_health, differ, store, with fixture-based tests.
- Message PAX / Code for Philly for a reviewer.

**Week 2: AI layer and eval**
- Schema, prompt v1, extractor, verifier, router, budget, with unit tests.
- Build the 50-item eval set. Run `make eval`. First numbers.
- Local review app.

**Week 3: automation and write-up**
- Weekly workflow, `data` branch, digest, GitHub Pages.
- `docs/EVAL.md`, `docs/LIMITATIONS.md`, README.
- Start live runs. Collect 3 weeks before calling it done.

### Cut list if behind

1. Digest page (keep approved items as a markdown file instead).
2. Review app UI (use a CLI approve/reject command instead).
3. Second source (keep only the Bulletin).

Never cut: verifier, human gate, eval report, limitations section.

---

## 11. Adversarial review

Each finding below was checked against the first draft. The plan above already includes the fixes.

| # | Attack on the plan | Severity | Resolution |
|---|---|---|---|
| 1 | Live rule changes are rare. Three weeks of runs may produce nothing to show. | High | Accuracy comes from a historical eval set. Live runs only prove automation. |
| 2 | Quote matching proves the quote exists, not that it supports the claim. Calling this "hallucination-proof" would be false. | High | Added "supported but wrong" metric. Human review stays mandatory. README states the limit. |
| 3 | A digest about expungement rules can be read as legal advice. | High | Non-goal stated. Schema and verifier block advice phrasing. Disclaimer plus source link on every item. |
| 4 | Generator does not value a solo agent. No stakeholder means weak evidence. | High | Recruit in week 1. If none, say so in the README rather than imply usage. |
| 5 | Site layout changes break the parser and create fake "rule deleted" diffs. | Medium | `parse_health` marks the source broken and suppresses diffs. |
| 6 | Bulletin issues include notices naming individuals. | Medium | Scope to rules sections, store normalized rules text only, fixture test. |
| 7 | Injection tests on a government site look like theater. | Medium | Framed honestly: architecture is the defense, tests confirm it. Fixtures labeled synthetic. |
| 8 | Eval results change between runs because of model nondeterminism. | Medium | Temperature 0, response cache, model id and prompt version recorded. |
| 9 | Hand-labeling your own eval set is biased. | Medium | Stakeholder checks a subset. Agreement reported. |
| 10 | Committing raw HTML and SQLite to `main` bloats the repo. | Low | Separate `data` branch, no raw HTML stored. |
| 11 | Unbounded LLM calls on a big backlog. | Low | Per-run budget and extraction cap; fail closed. |
| 12 | Robots.txt or terms may disallow automated access. | Medium | Checked in week 1 before any fetch code targets the site. Fallback source named. |
| 13 | "Refactor" claim is misleading for a 9-file rewrite. | Low | README says "rebuilt from a 2023 prototype" and links the tag. |
| 14 | Four active projects at once; risk of nothing finishing. | High | 3-week cap, cut list, never-cut list. |
| 15 | Keyword baseline might match the LLM, making the AI look pointless. | Medium | Report it honestly. Even then, the structured fields (dates, rule ids, affected parties) are something keywords cannot produce. |

**Remaining risks accepted:**
- Single LLM vendor.
- Small eval set, so numbers have wide uncertainty. Report counts, not just percentages.
- The verifier cannot judge meaning. Humans do.

**Confidence:** high that this is buildable in 3 part-time weeks with the cut list, and that it shows provenance, structured extraction, evals, human review, and honest limitations. Medium on getting an external reviewer; that depends on outreach in week 1.

---

## 12. Known limitations (draft for docs/LIMITATIONS.md)

- Only two Pennsylvania sources. Not a general legal monitor.
- The verifier checks that quotes exist, not that claims are correct.
- The eval set is small and labeled mostly by the author.
- Not legal advice. Summaries describe changes only.
- Depends on source site structure; layout changes pause monitoring until fixed.
