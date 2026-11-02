"""The only LLM step (PLAN.md section 5): one structured-output call per item.

The model gets the item's source text as delimited data and returns a `ChangeSummary` via
structured output. Everything after the call (canonicalizing, verifying, routing) is code.
A refusal, truncation, or schema violation is an extraction error, never partial output.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from spidercrawler.canonical import canonicalize
from spidercrawler.normalize import content_hash
from spidercrawler.records import Extraction, utcnow
from spidercrawler.router import route
from spidercrawler.schema import SCHEMA_VERSION, ChangeSummary
from spidercrawler.verifier import RowContext, verify

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "extract_v1.md"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "medium"
OPEN_TAG, CLOSE_TAG = "<source_document>", "</source_document>"
# Any spelling of our delimiter tags inside data: case, spaces, or the listing tag.
_TAG_LOOKALIKE = re.compile(r"<\s*/?\s*(source_document|committee_listing)\s*>", re.IGNORECASE)


def _neutralize(text: str) -> str:
    return _TAG_LOOKALIKE.sub(lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"), text)


def load_prompt(path: Path = PROMPT_PATH) -> tuple[str, str]:
    """Returns (system prompt, version). The version changes whenever the file changes."""
    text = path.read_text(encoding="utf-8")
    return text, f"{path.stem}:{hashlib.sha256(text.encode()).hexdigest()[:8]}"


def build_user_message(source_text: str, row: RowContext | None) -> str:
    """Everything from the website goes inside delimiters, including the page listing.

    Data cannot close a delimiter early: any look-alike tag in it is neutralized.
    """
    parts = []
    if row is not None:
        listing = f"table: {row.table}\ntitle: {row.title}\ndate column: {row.date_literal}"
        parts.append(f"<committee_listing>\n{_neutralize(listing)}\n</committee_listing>")
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
    input_tokens: int
    output_tokens: int


class ResponseCache:
    """Model responses keyed by everything that determines them. Lets the eval re-run offline."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    @staticmethod
    def key(prompt_version: str, model: str, effort: str, input_hash: str) -> str:
        raw = "\x1f".join([prompt_version, model, effort, SCHEMA_VERSION, input_hash])
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
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort},
        output_format=ChangeSummary,
    )
    usage = getattr(response, "usage", None)
    tokens_in = getattr(usage, "input_tokens", 0) or 0
    tokens_out = getattr(usage, "output_tokens", 0) or 0
    stop = getattr(response, "stop_reason", None)
    if stop == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        return CallResult(None, f"refusal ({category})", tokens_in, tokens_out)
    if stop == "max_tokens":
        return CallResult(None, "truncated at max_tokens", tokens_in, tokens_out)
    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        return CallResult(None, f"no parsed output (stop_reason={stop})", tokens_in, tokens_out)
    try:
        summary = ChangeSummary.model_validate(
            parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
        )
    except ValidationError as exc:
        return CallResult(None, f"schema: {exc.error_count()} errors", tokens_in, tokens_out)
    return CallResult(summary.model_dump(mode="json"), None, tokens_in, tokens_out)


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
        created_at=utcnow(),
    )
