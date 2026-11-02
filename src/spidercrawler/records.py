"""Append-only JSON records: the source of truth (PLAN.md section 4).

Each record is one file at `<root>/<kind>/<id>.json`. Files are written once and never
modified, so the weekly job and local review commits can never conflict. Writing the same
record twice is a no-op; writing a different payload under an existing id is an error.
"""

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

TableName = Literal["proposed", "approved", "prior_proposed"]


def short_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class Record(BaseModel):
    kind: ClassVar[str]
    id: str


class Item(Record):
    """A rulemaking row on a committee page, as first seen."""

    kind: ClassVar[str] = "items"
    item_key: str
    source_id: str
    table: TableName
    title: str
    row_date_literal: str | None
    doc_urls: list[str]
    first_seen_run: str
    seeded: bool  # recorded by a seed run; never sent for extraction

    @staticmethod
    def make_id(item_key: str) -> str:
        return "item-" + short_hash(item_key)


class ItemEvent(Record):
    """A later fact about an item, e.g. it moved from one table to another."""

    kind: ClassVar[str] = "item_events"
    item_id: str
    run_id: str
    event: Literal["table_moved"]
    from_table: TableName
    to_table: TableName

    @staticmethod
    def make_id(run_id: str, item_id: str, to_table: str) -> str:
        return f"{run_id}-{item_id}-{to_table}"


class Document(Record):
    """Normalized text of one PDF linked from an item."""

    kind: ClassVar[str] = "documents"
    item_id: str
    url: str
    fetched_at: datetime
    content_hash: str
    text: str
    text_len: int
    markup_lost: bool
    no_text: bool

    @staticmethod
    def make_id(url: str, content_hash: str) -> str:
        return "doc-" + short_hash(url, content_hash)


class PageValidators(BaseModel):
    etag: str | None = None
    last_modified: str | None = None


class Run(Record):
    kind: ClassVar[str] = "runs"
    started_at: datetime
    finished_at: datetime
    seed: bool
    sources_checked: int
    sources_broken: list[str] = Field(default_factory=list)
    items_found: int
    documents_fetched: int
    requests_made: int
    extractions_made: int = 0
    blocked: int = 0
    tokens_used: int = 0
    errors: list[str] = Field(default_factory=list)
    page_validators: dict[str, PageValidators] = Field(default_factory=dict)
    rows_seen: dict[str, int] = Field(default_factory=dict)  # last good row count per source
    status: Literal["ok", "failed"]

    @staticmethod
    def make_id(started_at: datetime) -> str:
        # Microseconds: two runs in the same second (a manual re-run, tests) must not collide.
        return "run-" + started_at.strftime("%Y%m%dT%H%M%S%fZ")


VerifierStatus = Literal["pass", "pass_flagged", "fail", "error"]
QueueStatus = Literal["blocked", "needs_attention", "ready_for_review"]


class Extraction(Record):
    """One extractor call for one item, with the verifier result and queue status."""

    kind: ClassVar[str] = "extractions"
    item_id: str
    model_id: str
    effort: str
    prompt_version: str
    schema_version: str
    input_hash: str  # content hash of the source text the model saw
    output: dict | None  # ChangeSummary as JSON; None on extraction error
    canonical: dict | None  # Canonical as JSON
    error: str | None  # refusal, schema error, ...
    verifier_status: VerifierStatus
    verifier_notes: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)  # suspicious_input, markup_lost, ...
    confidence: str | None
    queue_status: QueueStatus
    input_tokens: int = 0
    output_tokens: int = 0
    created_at: datetime

    @staticmethod
    def make_id(item_id: str, model_id: str, effort: str, prompt_version: str,
                schema_version: str, input_hash: str) -> str:  # fmt: skip
        return "ext-" + short_hash(item_id, model_id, effort, prompt_version, schema_version,
                                   input_hash)  # fmt: skip


class Review(Record):
    """A human decision on an extraction. The newest review for an extraction wins."""

    kind: ClassVar[str] = "reviews"
    extraction_id: str
    reviewer: str
    decision: Literal["approve", "edit", "reject"]
    edited_output: dict | None = None
    edited_verifier_status: VerifierStatus | None = None
    edited_verifier_notes: list[str] = Field(default_factory=list)
    note: str = ""
    decided_at: datetime

    @staticmethod
    def make_id(extraction_id: str, decided_at: datetime) -> str:
        return f"rev-{extraction_id}-{decided_at.strftime('%Y%m%dT%H%M%S%fZ')}"


RECORD_TYPES: tuple[type[Record], ...] = (Item, ItemEvent, Document, Run, Extraction, Review)


class RecordConflictError(Exception):
    """A different payload already exists under this record id."""


class RecordStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, kind: str, record_id: str) -> Path:
        return self.root / kind / f"{record_id}.json"

    def write(self, record: Record) -> bool:
        """Write a record. Returns False if an identical record already exists."""
        path = self.path_for(record.kind, record.id)
        payload = record.model_dump(mode="json")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write a temp file, then hard-link it into place. os.link fails if the target
        # exists, so creation is atomic and can never overwrite (os.replace would).
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
                f.write("\n")
            os.link(tmp, path)
        except FileExistsError:
            if json.loads(path.read_text(encoding="utf-8")) == payload:
                return False
            raise RecordConflictError(
                f"{record.kind}/{record.id} already exists with other data"
            ) from None
        finally:
            os.unlink(tmp)
        return True

    def exists(self, record_type: type[Record], record_id: str) -> bool:
        return self.path_for(record_type.kind, record_id).exists()

    def iter[R: Record](self, record_type: type[R]) -> Iterator[R]:
        folder = self.root / record_type.kind
        if not folder.is_dir():
            return
        for path in sorted(folder.glob("*.json")):
            yield record_type.model_validate_json(path.read_text(encoding="utf-8"))

    def latest_run(self) -> Run | None:
        runs = list(self.iter(Run))
        return max(runs, key=lambda r: r.started_at) if runs else None
