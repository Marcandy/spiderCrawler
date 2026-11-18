"""Chunks of everything the monitor has seen, for retrieval (docs/AGENTIC.md section 2).

Chunks come from two record kinds only: `Document` text and `Item` titles. Both are data from
the court website. A model output is never chunked, so retrieval can never feed a summary
back into a summary. Titles are indexed because seeded items have a title and no document,
and most of the history is seeded.
"""

import re
from dataclasses import dataclass

from spidercrawler.canonical import rule_ids
from spidercrawler.config import Source
from spidercrawler.detector import load_known
from spidercrawler.records import Document, RecordStore, short_hash

MAX_CHARS = 1_500
OVERLAP = 200
_HEADING = re.compile(r"(?m)^(?=Rule\s+\d+(?:\.\d+)*\.)")  # "Rule 632." at a line start
_HEADING_LINE = re.compile(r"Rule\s+\d+(?:\.\d+)*\.")


@dataclass(frozen=True)
class Chunk:
    id: str
    kind: str  # "document" or "title"; there is no third kind
    item_id: str
    document_id: str | None
    source_id: str
    table: str
    row_date: str | None
    first_seen_run: str
    url: str | None
    rule_ids: tuple[str, ...]  # canonical, exact; filters match these, never the text
    text: str


def _tail(text: str) -> str:
    """The end of a chunk, cut at a space, repeated at the start of the next one."""
    tail = text[-OVERLAP:]
    return tail[tail.find(" ") + 1 :] if " " in tail else tail


def split_text(text: str) -> list[tuple[str, str]]:
    """(rule heading, chunk text) pairs: split on rule headings, then pack paragraphs.

    The `{+ +}` and `[ ]` markup is kept as is. Within one rule's section, each chunk after
    the first starts with the tail of the one before it.
    """
    chunks: list[tuple[str, str]] = []
    for section in _HEADING.split(text):
        match = _HEADING_LINE.match(section)
        heading = match.group(0) if match else ""
        current = ""
        for paragraph in (p.strip() for p in section.split("\n\n")):
            pieces = [paragraph[i : i + MAX_CHARS] for i in range(0, len(paragraph), MAX_CHARS)]
            for piece in pieces:
                if current and len(current) + len(piece) + 2 > MAX_CHARS:
                    chunks.append((heading, current))
                    current = _tail(current)
                current = f"{current}\n\n{piece}" if current else piece
        if current.strip():
            chunks.append((heading, current))
    return chunks


def build_chunks(records: RecordStore, sources: list[Source]) -> list[Chunk]:
    """Every title and document chunk, rebuilt from the records alone."""
    prefixes = {s.id: s.rule_prefix for s in sources}
    docs: dict[str, list[Document]] = {}
    for doc in records.iter(Document):
        docs.setdefault(doc.item_id, []).append(doc)
    chunks: dict[str, Chunk] = {}
    for known in load_known(records).values():
        item, prefix = known.item, prefixes.get(known.item.source_id, "Pa.R.Crim.P.")
        meta = (item.source_id, known.current_table, item.row_date_literal, item.first_seen_run)

        def add(kind: str, doc: Document | None, ids: list[str], text: str, item=item, meta=meta):
            chunk_id = "chk-" + short_hash(kind, item.id, doc.content_hash if doc else "", text)
            chunks.setdefault(  # identical text in one document is one chunk
                chunk_id,
                Chunk(
                    chunk_id,
                    kind,
                    item.id,
                    doc.id if doc else None,
                    *meta,
                    doc.url if doc else None,
                    tuple(ids),
                    text,
                ),
            )

        add("title", None, rule_ids(item.title, prefix), item.title)
        for doc in docs.get(item.id, []):
            for heading, text in split_text(doc.text):
                # A continuation chunk may not repeat its rule number; the heading supplies it.
                add("document", doc, rule_ids(f"{heading}\n{text}", prefix), text)
    return list(chunks.values())
