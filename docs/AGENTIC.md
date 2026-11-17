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

_Not built yet._

## 3. Read-only MCP server

_Not built yet._

## 4. Review assistant (the one tool-using agent)

_Not built yet._
