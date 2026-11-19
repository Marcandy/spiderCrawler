"""The review assistant: the one tool-using agent (docs/AGENTIC.md section 4).

For one extraction, a model reads the item's documents through read-only tools and returns
a `ReviewBrief`: per-field findings, each with an evidence quote. The loop is written by
hand on the Messages API so every decision in it is visible and testable.

Bounds: a step cap, its own token budget inside the run budget, a tool allowlist, and
read-only tools. Hitting any bound, a refusal, or truncation stops the loop and the brief is
stored as cut short with no findings: no brief is better than a partial one. An API error
is one more such stop. After the loop, code checks every evidence quote against the source;
a finding whose quote is not there is kept and marked unverified.

Advisory only. A brief can move an item from ready_for_review to needs_attention
(`router.effective_status`). It never unblocks, approves, edits, or reaches a public surface.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ConfigDict, Field

from spidercrawler.budget import Budget
from spidercrawler.extractor import LLMClient, load_prompt
from spidercrawler.normalize import quote_in_source
from spidercrawler.records import Brief, Document, Extraction, Item, RecordStore
from spidercrawler.tools import Toolbox, ToolInputError, arguments_model, description, neutralize
from spidercrawler.tools import call as call_tool
from spidercrawler.trace import Trace

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "assist_v1.md"
# The assistant's tools: a subset of the registry, read-only. Never the review-queue or
# extraction tools: the assistant sees only the output under review and the sources.
ASSISTANT_TOOLS = (
    "read_document",
    "search_rules",
    "related_items",
    "check_quote",
    "canonical_rule_ids",
    "get_item",
)
MAX_RESULT_CHARS = 20_000

BriefField = Literal[
    "summary",
    "stage",
    "rule_ids",
    "effective_date_text",
    "affected_parties",
    "expungement_relevance",
    "is_substantive",
]


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: BriefField
    kind: Literal["contradicted", "unsupported", "missing", "consistent"]
    evidence_quote: str = Field(description="Copied exactly from the item's own documents.")
    note: str = Field(description="One short sentence for the reviewer.")


class ReviewBrief(BaseModel):
    """What the assistant returns. Free text is never accepted outside this schema."""

    model_config = ConfigDict(extra="forbid")

    findings: list[Finding]
    related_proposal_item_id: str | None


@dataclass
class Bounds:
    max_steps: int = 8
    max_tokens: int = 80_000
    max_output_tokens: int = 8_000


@dataclass
class Outcome:
    """One assistant run, before it becomes a `Brief` record."""

    status: Literal["complete", "cut_short"]
    cut_reason: str | None = None
    brief: ReviewBrief | None = None
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tools_used: list[str] = field(default_factory=list)


def tool_definitions(toolbox: Toolbox) -> list[dict]:
    return [
        {
            "name": name,
            "description": description(getattr(toolbox, name)),
            "input_schema": arguments_model(getattr(toolbox, name)).model_json_schema(),
        }
        for name in ASSISTANT_TOOLS
    ]


def build_message(extraction: Extraction, item: Item, documents: list[Document]) -> str:
    """The output under review and where to look. Everything from outside is neutralized."""
    listing = {
        "item_id": item.id,
        "title": item.title,
        "row_date_literal": item.row_date_literal,
        "document_ids": [d.id for d in documents],
    }
    under_review = json.dumps(neutralize(extraction.output), indent=2, sort_keys=True)
    return (
        f"Item:\n{json.dumps(neutralize(listing), indent=2)}\n\n"
        f"<extraction_under_review>\n{under_review}\n</extraction_under_review>"
    )


def run_tool(toolbox: Toolbox, name: str, arguments: Any) -> tuple[str, bool]:
    """(result text, is_error). A tool failure is an error result for the model, never a
    crash of the loop; only the exception class is shown for an unexpected one."""
    if name not in ASSISTANT_TOOLS:
        return f"unknown tool {name!r}; use one of {', '.join(ASSISTANT_TOOLS)}", True
    if not isinstance(arguments, dict):
        return "invalid arguments: expected an object", True
    try:
        text = json.dumps(call_tool(toolbox, name, arguments))
    except ToolInputError as exc:
        return str(exc), True
    except Exception as exc:  # a bug in a tool must not end the review
        return f"tool failed: {type(exc).__name__}", True
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + " [truncated: ask for less]"
    return text, False


def _size(messages: list[dict]) -> str:
    """Plain text of the conversation, for the budget's conservative estimate."""
    return json.dumps(messages, default=lambda o: getattr(o, "__dict__", str(o)))


def run_assistant(
    client: LLMClient,
    toolbox: Toolbox,
    extraction: Extraction,
    item: Item,
    documents: list[Document],
    *,
    model: str,
    effort: str,
    bounds: Bounds,
    budget: Budget | None = None,
    trace: Trace | None = None,
) -> Outcome:
    trace = trace or Trace()
    system, _ = load_prompt(PROMPT_PATH)
    tools = tool_definitions(toolbox)
    messages: list[dict] = [{"role": "user", "content": build_message(extraction, item, documents)}]
    out = Outcome(status="cut_short")
    ids = {"item_id": item.id, "extraction_id": extraction.id}

    def cut(reason: str) -> Outcome:
        out.status, out.cut_reason, out.brief = "cut_short", reason, None
        return out

    for step in range(1, bounds.max_steps + 1):
        text = _size(messages)
        estimate = len(text) // 3 + 2_000 + bounds.max_output_tokens
        if out.input_tokens + out.output_tokens + estimate > bounds.max_tokens:
            return cut(f"assistant token budget of {bounds.max_tokens} reached")
        if budget is not None and not budget.try_start(text):
            return cut(f"run budget: {budget.stopped_reason}")
        try:
            with trace.step("assist_turn", attempt=step, **ids) as event:
                response = client.messages.parse(
                    model=model,
                    max_tokens=bounds.max_output_tokens,
                    system=system,
                    messages=messages,
                    tools=tools,
                    output_config={"effort": effort},
                    output_format=ReviewBrief,
                )
                usage = getattr(response, "usage", None)
                tokens_in = getattr(usage, "input_tokens", 0) or 0
                tokens_out = getattr(usage, "output_tokens", 0) or 0
                stop = getattr(response, "stop_reason", None)
                event.update(input_tokens=tokens_in, output_tokens=tokens_out, outcome=str(stop))
        except anthropic.APIError as exc:  # fails closed like any other bound; class only
            return cut(f"api error: {type(exc).__name__}")
        out.steps, out.input_tokens, out.output_tokens = (
            step,
            out.input_tokens + tokens_in,
            out.output_tokens + tokens_out,
        )
        if budget is not None:
            budget.record(tokens_in, tokens_out)
        if stop in ("refusal", "max_tokens"):
            return cut(f"model stopped: {stop}")
        content = list(getattr(response, "content", None) or [])
        uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
        if uses:
            # Every tool_use in the turn gets its result in one user message, in order.
            messages.append({"role": "assistant", "content": content})
            results = []
            for use in uses:
                name = use.name if use.name in ASSISTANT_TOOLS else "unknown"
                with trace.step(
                    "tool_call", tool=name, argument_chars=len(json.dumps(use.input)), **ids
                ) as event:
                    result, is_error = run_tool(toolbox, use.name, use.input)
                    event.update(outcome="error" if is_error else "ok", result_chars=len(result))
                out.tool_calls += 1
                out.tools_used.append(name)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "content": result,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})
            continue
        if stop != "end_turn":
            return cut(f"unexpected stop_reason {stop}")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            return cut("no structured brief returned")
        out.status, out.brief = (
            "complete",
            ReviewBrief.model_validate(
                parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
            ),
        )
        return out
    return cut(f"step cap of {bounds.max_steps} reached")


def to_record(
    outcome: Outcome,
    extraction: Extraction,
    source_text: str,
    known_items: set[str],
    *,
    model: str,
    effort: str,
) -> Brief:
    """Check every evidence quote in code, then store. Unverified findings are kept, marked."""
    findings = []
    proposal = None
    if outcome.brief is not None:
        for f in outcome.brief.findings:
            verified = bool(f.evidence_quote) and quote_in_source(f.evidence_quote, source_text)
            findings.append({**f.model_dump(), "verified": verified})
        if outcome.brief.related_proposal_item_id in known_items - {extraction.item_id}:
            proposal = outcome.brief.related_proposal_item_id
    created = datetime.now(UTC)  # microseconds: two briefs in one second must not collide
    _, version = load_prompt(PROMPT_PATH)
    return Brief(
        id=Brief.make_id(extraction.id, model, created),
        extraction_id=extraction.id,
        item_id=extraction.item_id,
        model_id=model,
        effort=effort,
        prompt_version=version,
        status=outcome.status,
        cut_reason=outcome.cut_reason,
        findings=findings,
        related_proposal_item_id=proposal,
        steps=outcome.steps,
        tool_calls=outcome.tool_calls,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        created_at=created,
    )


def assist(
    client: LLMClient,
    records: RecordStore,
    toolbox: Toolbox,
    extraction: Extraction,
    *,
    model: str,
    effort: str,
    bounds: Bounds,
    budget: Budget | None = None,
    trace: Trace | None = None,
) -> Brief:
    """Run the assistant on one stored extraction and write its brief. Writes nothing else."""
    items = {i.id: i for i in records.iter(Item)}
    item = items[extraction.item_id]
    docs = {d.url: d for d in records.iter(Document) if d.item_id == item.id}
    ordered = [docs[u] for u in item.doc_urls if u in docs]
    outcome = run_assistant(
        client,
        toolbox,
        extraction,
        item,
        ordered,
        model=model,
        effort=effort,
        bounds=bounds,
        budget=budget,
        trace=trace,
    )
    source = "\n\n".join(d.text for d in ordered)
    brief = to_record(outcome, extraction, source, set(items), model=model, effort=effort)
    records.write(brief)
    return brief
