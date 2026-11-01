"""SQLite cache rebuilt from the JSON records. Never committed; safe to delete."""

import json
import sqlite3
from pathlib import Path

from spidercrawler.records import Document, Item, ItemEvent, RecordStore, Run

SCHEMA = """
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    item_key TEXT NOT NULL UNIQUE,
    source_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    title TEXT NOT NULL,
    row_date_literal TEXT,
    doc_urls TEXT NOT NULL,
    first_seen_run TEXT NOT NULL,
    seeded INTEGER NOT NULL
);
CREATE TABLE item_events (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id),
    run_id TEXT NOT NULL,
    event TEXT NOT NULL,
    from_table TEXT NOT NULL,
    to_table TEXT NOT NULL
);
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id),
    url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    text TEXT NOT NULL,
    text_len INTEGER NOT NULL,
    markup_lost INTEGER NOT NULL,
    no_text INTEGER NOT NULL
);
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    seed INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE VIEW item_current AS
SELECT i.*,
       COALESCE(
           (SELECT e.to_table FROM item_events e WHERE e.item_id = i.id
            ORDER BY e.run_id DESC LIMIT 1),
           i.table_name) AS current_table
FROM items i;
"""


def rebuild(records: RecordStore, db_path: Path) -> sqlite3.Connection:
    """Drop the cache and load every record. Returns an open connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    i.id,
                    i.item_key,
                    i.source_id,
                    i.table,
                    i.title,
                    i.row_date_literal,
                    json.dumps(i.doc_urls),
                    i.first_seen_run,
                    int(i.seeded),
                )
                for i in records.iter(Item)
            ],
        )
        conn.executemany(
            "INSERT INTO item_events VALUES (?, ?, ?, ?, ?, ?)",
            [
                (e.id, e.item_id, e.run_id, e.event, e.from_table, e.to_table)
                for e in records.iter(ItemEvent)
            ],
        )
        conn.executemany(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    d.id,
                    d.item_id,
                    d.url,
                    d.fetched_at.isoformat(),
                    d.content_hash,
                    d.text,
                    d.text_len,
                    int(d.markup_lost),
                    int(d.no_text),
                )
                for d in records.iter(Document)
            ],
        )
        conn.executemany(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    r.id,
                    r.started_at.isoformat(),
                    r.finished_at.isoformat(),
                    int(r.seed),
                    r.status,
                    r.model_dump_json(),
                )
                for r in records.iter(Run)
            ],
        )
    return conn
