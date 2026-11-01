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
    errors: list[str] = Field(default_factory=list)
    page_validators: dict[str, PageValidators] = Field(default_factory=dict)
    status: Literal["ok", "failed"]

    @staticmethod
    def make_id(started_at: datetime) -> str:
        return "run-" + started_at.strftime("%Y%m%dT%H%M%SZ")


RECORD_TYPES: tuple[type[Record], ...] = (Item, ItemEvent, Document, Run)


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
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) == payload:
                return False
            raise RecordConflictError(f"{record.kind}/{record.id} already exists with other data")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then rename, so a crash never leaves half a record.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
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
