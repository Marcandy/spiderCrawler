"""The only LLM step (PLAN.md section 5): one structured-output call per item.

The model gets the item's source text as delimited data and returns a `ChangeSummary` via
structured output. Everything after the call (canonicalizing, verifying, routing) is code.
A refusal, truncation, or schema violation is an extraction error, never partial output.
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from spidercrawler.canonical import canonicalize
from spidercrawler.corpus import Chunk
from spidercrawler.normalize import content_hash
from spidercrawler.records import Extraction, utcnow
from spidercrawler.router import route
from spidercrawler.schema import SCHEMA_VERSION, ChangeSummary
from spidercrawler.verifier import RowContext, verify

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
PROMPT_VERSIONS = ("v1", "v2")  # v2 adds <related_context>; v1 is never edited
PROMPT_PATH = PROMPTS / "extract_v1.md"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "medium"
OPEN_TAG, CLOSE_TAG = "<source_document>", "</source_document>"
# Any spelling of our delimiter tags inside data: case, spaces, or the listing tag.
_TAG_LOOKALIKE = re.compile(
    r"<\s*/?\s*(source_document|committee_listing|related_context)\s*>", re.IGNORECASE
)


def _neutralize(text: str) -> str:
    return _TAG_LOOKALIKE.sub(lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"), text)


def load_prompt(path: Path | str = PROMPT_PATH) -> tuple[str, str]:
    """Returns (system prompt, version). The version changes whenever the file changes.

    Accepts a path or a short name ("v1", "v2").
    """
    if path in PROMPT_VERSIONS:
        path = PROMPTS / f"extract_{path}.md"
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    return text, f"{path.stem}:{hashlib.sha256(text.encode()).hexdigest()[:8]}"


def related_block(chunks: list[Chunk]) -> str:
    """Retrieved chunks as one text. Empty when there is nothing to add."""
    return "\n\n---\n\n".join(c.text for c in chunks)


def build_user_message(source_text: str, row: RowContext | None, related: str = "") -> str:
    """Everything from the website goes inside delimiters, including the page listing.

    Data cannot close a delimiter early: any look-alike tag in it is neutralized. `related`
    (prompt v2 only) is retrieved text from other source documents: a second channel for
    injected text, so it gets the same treatment and its own delimiter.
    """
    parts = []
    if row is not None:
        listing = f"table: {row.table}\ntitle: {row.title}\ndate column: {row.date_literal}"
        parts.append(f"<committee_listing>\n{_neutralize(listing)}\n</committee_listing>")
    if related:
        parts.append(f"<related_context>\n{_neutralize(related)}\n</related_context>")
    parts.append(f"{OPEN_TAG}\n{_neutralize(source_text)}\n{CLOSE_TAG}")
    return "\n\n".join(parts)


class MessagesAPI(Protocol):
    def parse(self, **kwargs: Any) -> Any: ...


class LLMClient(Protocol):
    messages: MessagesAPI


@dataclass(frozen=True)
class CallResult:
    output: dict | None
    error: str | None
    input_tokens: int  # everything billed as input: uncached, cache writes, and cache reads
    output_tokens: int
    cache_read_tokens: int = 0  # the part of input_tokens served from the prompt cache
    stop_reason: str | None = None
    latency_ms: int = 0


class ResponseCache:
    """Model responses keyed by everything that determines them. Lets the eval re-run offline."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    @staticmethod
    def key(
        prompt_version: str,
        model: str,
        effort: str,
        input_hash: str,
        attempt: int = 1,
        feedback_hash: str = "",
    ) -> str:
        """A repair attempt answers a longer conversation, so its number and a hash of the
        feedback it was sent are part of the key. First-attempt keys are unchanged."""
        parts = [prompt_version, model, effort, SCHEMA_VERSION, input_hash]
        if attempt > 1:
            parts += [str(attempt), feedback_hash]
        raw = "\x1f".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def get(self, key: str) -> CallResult | None:
        path = self._path(key)
        return CallResult(**json.loads(path.read_text(encoding="utf-8"))) if path.exists() else None

    def put(self, key: str, result: CallResult) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(
            json.dumps(result.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def call_model(
    client: LLMClient,
    system: str,
    user: str,
    model: str,
    effort: str,
    max_tokens: int,
) -> CallResult:
    """One structured-output call. No sampling params and no forced tool_choice (both 400)."""
    messages = [{"role": "user", "content": user}]
    return call_conversation(client, system, messages, model, effort, max_tokens)[0]


def call_conversation(
    client: LLMClient,
    system: str,
    messages: list[dict],
    model: str,
    effort: str,
    max_tokens: int,
) -> tuple[CallResult, Any]:
    """One call on a conversation. Returns the result and the assistant content to replay.

    A follow-up turn must send the assistant content back unchanged (it can hold thinking
    blocks), so the raw content is returned beside the parsed result. The system prompt is
    marked cacheable; a prefix under the model's minimum is simply not cached.
    """
    started = time.monotonic()
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        output_config={"effort": effort},
        output_format=ChangeSummary,
    )
    usage = getattr(response, "usage", None)
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    stop = getattr(response, "stop_reason", None)

    def result(output: dict | None, error: str | None) -> CallResult:
        return CallResult(
            output,
            error,
            (getattr(usage, "input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + cache_read,
            getattr(usage, "output_tokens", 0) or 0,
            cache_read,
            stop,
            int((time.monotonic() - started) * 1000),
        )

    content = getattr(response, "content", None)
    if stop == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        return result(None, f"refusal ({category})"), content
    if stop == "max_tokens":
        return result(None, "truncated at max_tokens"), content
    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        return result(None, f"no parsed output (stop_reason={stop})"), content
    try:
        summary = ChangeSummary.model_validate(
            parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
        )
    except ValidationError as exc:
        return result(None, f"schema: {exc.error_count()} errors"), content
    return result(summary.model_dump(mode="json"), None), content


def build_extraction(
    item_id: str,
    source_text: str,
    result: CallResult,
    *,
    model: str,
    effort: str,
    prompt_version: str,
    row: RowContext | None,
    rule_prefix: str,
    document_flags: list[str],
    attempt_ids: list[str] | None = None,
) -> Extraction:
    """Canonicalize, verify, and route a model result into an Extraction record."""
    input_hash = content_hash(source_text)
    canonical = None
    notes: list[str] = []
    flags = list(document_flags)
    confidence = relevance = None
    if result.output is None:
        status = "error"
        notes = [result.error or "unknown error"]
    else:
        summary = ChangeSummary.model_validate(result.output)
        canon = canonicalize(summary, rule_prefix)
        verdict = verify(summary, canon, source_text, row, flags)
        status, notes, flags = verdict.status, verdict.reasons, verdict.flags
        canonical = canon.model_dump(mode="json")
        confidence, relevance = summary.confidence, summary.expungement_relevance
    return Extraction(
        id=Extraction.make_id(item_id, model, effort, prompt_version, SCHEMA_VERSION, input_hash),
        item_id=item_id,
        model_id=model,
        effort=effort,
        prompt_version=prompt_version,
        schema_version=SCHEMA_VERSION,
        input_hash=input_hash,
        output=result.output,
        canonical=canonical,
        error=result.error,
        verifier_status=status,
        verifier_notes=notes,
        flags=flags,
        confidence=confidence,
        queue_status=route(status, confidence, relevance),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        attempt_ids=attempt_ids or [],
        created_at=utcnow(),
    )
