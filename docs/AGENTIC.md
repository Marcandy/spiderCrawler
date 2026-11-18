# The agentic parts, and what they do not prove

Four additions around the original single LLM call. Each section says what it does, the design
choice and the alternative rejected, what is measured, and what it does not prove.

Two facts limit every claim on this page (2026-11-16): no live run has succeeded, so none of
this has made a real model call; and no eval item is labeled, so every number is pending.

The tension the design answers: the original guards rely on the model having no tools and
only a fixed output schema. Each addition below keeps that true for the extraction call, and
keeps the human gate as the only path to a public surface.

## 1. Verifier-feedback repair loop and run trace

**What it does.** When the verifier fails an output, `repair.py` sends a follow-up turn in the
same conversation: the model's output, the verifier's reasons inside `<verifier_findings>`
tags, and a request for a corrected output. It stops when an attempt passes, when the reasons
do not shrink, when the budget refuses, or after 2 repair calls (hard cap). Every model call
is an `attempts/` record; every run writes `traces/<run_id>.jsonl` (`spidercrawler trace`).

**Design choices.**
- Only rules 1–6 are fed back. Rule 8 compares the output with the committee page; told
  "your date differs from the page's", the model could copy the page and the cross-check
  would check nothing. Any rule 8 reason sends the item to a person unrepaired. (The listing
  the model sees already shows the page's date column, so rule 8 was never fully independent.)
- A repaired pass is flagged `repaired:N` and routes to `needs_attention`, never
  `ready_for_review`. The review page lists each attempt and what the model was shown.
- Rejected: retrying refusals or truncation, and a separate repair budget (each repair call
  counts against the 20 calls per run).
- The trace accepts a fixed set of fields; strings must look like an id or a short code.
- Prompt caching: the system prompt is an estimated 600–800 tokens against a 512-token
  minimum listed for `claude-sonnet-5-5`. It is marked cacheable and each attempt records
  `cache_read_tokens`. Whether it hits is unknown until a live run.

**Measured.** `eval --repair 1` on dev: block rate and supported-but-wrong rate before and
after, attempts and tokens per item. **Pending labels.** Repair is off by default until then.

**Does not prove.** A repaired output passed a checker it was told how to pass: its quotes
exist. If blocked items turn into supported-but-wrong items, the loop is a regression.

## 2. Retrieval over the rule corpus

**What it does.** `corpus.py` chunks every stored document (on rule headings, then
paragraphs) and every item title. `retrieval.py` indexes the chunks with SQLite FTS5 (BM25).
`spidercrawler search` queries it; `related_items` links an item to earlier items on the same
rules and shows the reason on the review page; prompt `extract_v2` can add up to 3 retrieved
chunks in a `<related_context>` block.

**Design choices.**
- Lexical, not semantic: no embeddings, no second vendor, no new dependency. An `Embedder`
  protocol exists and is not implemented; a vendor should arrive with a comparison.
- Rule-id filters match canonical ids stored as metadata, never text, so 632 is not 6320.
- Only `Document` text and `Item` titles are indexed. A model output is never retrieved,
  reviewed or not: summaries feeding summaries would be a feedback loop.
- Retrieved text is a second injection channel. It is neutralized and delimited like the
  source, a `suspicious_related_context` flag is raised, and it counts against the budget.
- The verifier is unchanged: a quote lifted from related context is not in the source and
  fails rule 2. Rejected: letting quotes cite related documents (the verifier would have to
  trust retrieval).
- Related items are those seen no later than the item. Seeded history shares one run, so
  within it "earlier" is not known.

**Measured.** `eval-retrieval`: recall@k and MRR against links typed by the owner
(`eval-link`, which shows no candidate). `eval --prompt v2` against v1 on dev: field
accuracy, supported-but-wrong, tokens per item. **Both pending labels.** v1 stays the default.

**Does not prove.** That retrieval helps. Seeded items have titles and no documents, so the
document corpus starts near empty; "no measurable gain" is a likely first result.

## 3. Read-only MCP server

**What it does.** `tools.py` is one registry of ten read-only tools (search, items, document
pages, related items, quote check, rule-id canonicalization, review queue, extractions,
published entries, status). `spidercrawler mcp` serves them over stdio with the official
`mcp` SDK, plus two resources (digest, items). `.mcp.json` registers it for Claude Code.
Setup and examples: [MCP.md](MCP.md).

**Design choices.**
- One implementation for two callers: the MCP server and the feature 4 assistant both call
  `Toolbox` methods, so there is one set of tests.
- Read-only is enforced by tests, not by convention: the tool list must equal an explicit
  allowlist, the data directory's hash is unchanged after calling every tool, and the tool
  modules may not import the API client, the fetcher, or `decide`.
- Unreviewed model output is returned only under `unapproved_model_output`. Only
  `list_published` carries content a person approved.
- Results are untrusted for whoever calls: every description says so, tag look-alikes are
  neutralized, ids are looked up rather than used as paths, and sizes are capped.
- Rejected: write tools (approve, reject) behind a confirmation. Approval belongs to the
  review page, where edits are re-verified; one more door to the gate is one more to test.

**Measured.** Nothing quantitative; this is plumbing. Tests drive the server through the
SDK's in-memory client.

**Does not prove.** That a client model will respect the labels. Neutralizing tags and
marking output unreviewed tells a model what it is reading; it does not stop one from
repeating it. The human gate is still the control.

## 4. Review assistant (the one tool-using agent)

**What it does.** `assistant.py` gives a model the extraction under review and six read-only
tools from the registry (`read_document`, `search_rules`, `related_items`, `check_quote`,
`canonical_rule_ids`, `get_item`). It returns a `ReviewBrief`: per-field findings
(`consistent`, `contradicted`, `unsupported`, `missing`), each with an evidence quote. Code
then checks every quote with `quote_in_source`; a quote not in the source is kept, marked
unverified. The brief is a `briefs/` record shown in a box titled "AI second opinion, not
verified". `spidercrawler assist <id>` runs it; a pipeline step exists, off by default.

**Design choices.**
- A hand-written loop on `messages.parse` instead of the SDK's tool runner. The runner
  would loop and execute tools for us; writing it out makes each rule testable: an 8-turn
  cap, a per-brief token budget inside the run budget, an allowlist, parallel calls answered
  in one message, unknown names, bad arguments, and raising tools returned as errors.
- Fails closed. A bound, a refusal, or truncation stores a `cut_short` brief with no findings.
- Escalate only. A complete brief with any non-consistent finding moves `ready_for_review`
  to `needs_attention` (`router.effective_status`). It never unblocks, approves, edits the
  extraction, or reaches the digest, the issue, or `list_published`. Each "never" is a test.
- Why tools plus untrusted documents is acceptable here: the tools only read local records,
  nothing fetches or writes, the output is advisory, and a person decides every item. The
  worst an injection can do is a misleading brief or a needless escalation.
- `assistant.model` may differ from the extractor's: a model checking itself tends to agree.

**Measured.** `eval-assist --live` on dev: detection of six seeded, code-written mutations,
false alarms on unmutated outputs, steps, tool calls, tokens, cost, bound hits, verified
quotes, injection items, against a no-tools single call. **Pending labels and a live run.**

**Does not prove.** That it catches real errors, which are subtler than planted ones. On
documents this short, the single call may win; the loop should pay only on long documents
or across items.
