"""Lexical retrieval over the corpus: SQLite FTS5 with BM25 (docs/AGENTIC.md section 2).

No embeddings and no second vendor: this finds shared words, not shared meaning. The index
is a cache rebuilt from the records, in memory or beside `store.rebuild`; it is never
committed. Rule-id filters are exact matches on chunk metadata, never text search, so
"Pa.R.Crim.P. 632" cannot match 6320 or 63.
"""

import re
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from spidercrawler.canonical import rule_ids
from spidercrawler.config import Source
from spidercrawler.corpus import Chunk, build_chunks
from spidercrawler.records import Item, RecordStore

SCHEMA = """
CREATE TABLE chunks (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, item_id TEXT NOT NULL, document_id TEXT,
    source_id TEXT NOT NULL, table_name TEXT NOT NULL, row_date TEXT,
    first_seen_run TEXT NOT NULL, url TEXT, rule_ids TEXT NOT NULL, text TEXT NOT NULL
);
CREATE TABLE chunk_rules (chunk_id TEXT NOT NULL, rule_id TEXT NOT NULL);
CREATE INDEX chunk_rules_rule ON chunk_rules (rule_id);
CREATE VIRTUAL TABLE chunk_fts USING fts5(text, chunk_id UNINDEXED, tokenize = 'porter');
"""
_WORD = re.compile(r"\w+")
_SEP = "\x1f"


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float  # BM25, higher is better; 0.0 when only a filter was given


@dataclass(frozen=True)
class Link:
    item_id: str
    title: str
    reason: str  # why the two items are linked, in words a reviewer can check


class Retriever(Protocol):
    def search(
        self,
        query: str,
        k: int = 5,
        *,
        rule_ids: list[str] | None = None,
        kind: str | None = None,
        exclude_item: str | None = None,
        not_after_run: str | None = None,
    ) -> list[Hit]: ...


class Embedder(Protocol):
    """Left unimplemented on purpose: adding a vendor is a decision, and it should arrive
    with a comparison against the BM25 numbers."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def canonical_rule_filter(value: str, default_prefix: str = "Pa.R.Crim.P.") -> str:
    """'632', 'Rule 632', or 'Pa.R.Crim.P. 632' -> 'Pa.R.Crim.P. 632'."""
    value = value.strip()
    found = rule_ids(value if re.search(r"[A-Za-z]", value) else f"Rule {value}", default_prefix)
    return found[0] if found else value


class FtsIndex:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def build(cls, chunks: list[Chunk], conn: sqlite3.Connection | None = None) -> "FtsIndex":
        conn = conn or sqlite3.connect(":memory:")
        with conn:
            conn.executescript(SCHEMA)
            conn.executemany(
                "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        c.id,
                        c.kind,
                        c.item_id,
                        c.document_id,
                        c.source_id,
                        c.table,
                        c.row_date,
                        c.first_seen_run,
                        c.url,
                        _SEP.join(c.rule_ids),
                        c.text,
                    )
                    for c in chunks
                ],
            )
            conn.executemany(
                "INSERT INTO chunk_rules VALUES (?, ?)",
                [(c.id, rule) for c in chunks for rule in c.rule_ids],
            )
            conn.executemany(
                "INSERT INTO chunk_fts (text, chunk_id) VALUES (?, ?)",
                [(c.text, c.id) for c in chunks],
            )
        return cls(conn)

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        rule_ids: list[str] | None = None,
        kind: str | None = None,
        exclude_item: str | None = None,
        not_after_run: str | None = None,
    ) -> list[Hit]:
        """Best chunks first. Ties break on chunk id, so the order is deterministic."""
        # Each word is quoted, so nothing in the query is read as FTS5 syntax.
        words = " OR ".join(f'"{w}"' for w in _WORD.findall(query))
        where, params = [], []
        if words:
            sql = (
                "SELECT c.*, -bm25(chunk_fts) AS score FROM chunk_fts "
                "JOIN chunks c ON c.id = chunk_fts.chunk_id WHERE chunk_fts MATCH ?"
            )
            params.append(words)
        elif rule_ids:
            sql = "SELECT c.*, 0.0 AS score FROM chunks c WHERE 1"
        else:
            return []
        if rule_ids:
            marks = ", ".join("?" * len(rule_ids))
            where.append(f"c.id IN (SELECT chunk_id FROM chunk_rules WHERE rule_id IN ({marks}))")
            params += rule_ids
        for column, value, op in (
            ("c.kind", kind, "="),
            ("c.item_id", exclude_item, "!="),
            ("c.first_seen_run", not_after_run, "<="),
        ):
            if value is not None:
                where.append(f"{column} {op} ?")
                params.append(value)
        sql += "".join(f" AND {w}" for w in where) + " ORDER BY score DESC, c.id LIMIT ?"
        rows = self.conn.execute(sql, [*params, max(1, min(k, 50))]).fetchall()
        return [
            Hit(Chunk(*row[:9], tuple(filter(None, row[9].split(_SEP))), row[10]), row[11])
            for row in rows
        ]


def open_index(records: RecordStore, sources: list[Source]) -> FtsIndex:
    """An in-memory index built from the records. The corpus is small; this takes moments."""
    return FtsIndex.build(build_chunks(records, sources))


def _prefix(item: Item, sources: list[Source]) -> str:
    return next((s.rule_prefix for s in sources if s.id == item.source_id), "Pa.R.Crim.P.")


def related_items(
    item: Item, index: Retriever, items: dict[str, Item], sources: list[Source], limit: int = 5
) -> list[Link]:
    """Items seen no later than this one that touch the same rules, then similar titles.

    Deterministic: exact rule-id matches first, then BM25 on the title for what is left.
    Every link says why it was made.
    """
    links: dict[str, Link] = {}
    scope = {"exclude_item": item.id, "not_after_run": item.first_seen_run}
    wanted = rule_ids(item.title, _prefix(item, sources))
    if wanted:
        # Ranked by the title's words first, then any chunk the words missed.
        ranked = index.search(item.title, 50, rule_ids=wanted, **scope)
        for hit in [*ranked, *index.search("", 50, rule_ids=wanted, **scope)]:
            shared = ", ".join(r for r in wanted if r in hit.chunk.rule_ids)
            if hit.chunk.item_id in items:
                links.setdefault(
                    hit.chunk.item_id,
                    Link(
                        hit.chunk.item_id,
                        items[hit.chunk.item_id].title,
                        f"same rule id: {shared}",
                    ),
                )
    for hit in index.search(item.title, 50, kind="title", **scope):
        if hit.chunk.item_id in items:
            links.setdefault(
                hit.chunk.item_id,
                Link(
                    hit.chunk.item_id,
                    items[hit.chunk.item_id].title,
                    f"title match, score {hit.score:.2f}",
                ),
            )
    return list(links.values())[:limit]


def related_context(item: Item, index: Retriever, sources: list[Source], n: int) -> list[Chunk]:
    """Up to `n` chunks of other items' source documents about the same rules.

    Document chunks only: never a title, and never a model output (none is ever indexed).
    """
    wanted = rule_ids(item.title, _prefix(item, sources))
    if not wanted or n <= 0:
        return []
    hits = index.search(
        item.title,
        n,
        rule_ids=wanted,
        kind="document",
        exclude_item=item.id,
        not_after_run=item.first_seen_run,
    )
    return [h.chunk for h in hits]
