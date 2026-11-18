"""A per-run trace: one line per step, so a bad output can be debugged after the fact.

Written once at the end of a run as `traces/<run_id>.jsonl`. It holds ids, timings, outcomes,
and token counts. It never holds document text, a request header, or a secret: only the
fields in `ALLOWED` are accepted, and string values must look like an id or a short code.
"""

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from spidercrawler.records import RecordStore

KIND = "traces"
ALLOWED = frozenset(
    {
        "step",
        "source_id",
        "item_id",
        "document_id",
        "extraction_id",
        "attempt",
        "started_at",
        "duration_ms",
        "outcome",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "brief_id",
        "tool",
        "argument_chars",
        "result_chars",
    }
)
_SAFE_VALUE = re.compile(r"[\w.:+-]{0,80}")  # an id, a timestamp, or a code; never prose
_COLUMNS = (
    "step",
    "outcome",
    "duration_ms",
    "source_id",
    "item_id",
    "attempt",
    "tool",
    "argument_chars",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
)


def _check(event: dict) -> dict:
    for key, value in event.items():
        if key not in ALLOWED:
            raise ValueError(f"trace field {key!r} is not allowed")
        if isinstance(value, bool) or not isinstance(value, int | str):
            raise ValueError(f"trace field {key!r} must be an int or a short string")
        if isinstance(value, str) and not _SAFE_VALUE.fullmatch(value):
            raise ValueError(f"trace field {key!r} must be an id or a short code")
    return event


class Trace:
    def __init__(self) -> None:
        self.events: list[dict] = []

    @contextmanager
    def step(self, name: str, **fields: int | str) -> Iterator[dict]:
        """Time one step. The caller may set `outcome` and token counts on the event."""
        event: dict = {
            "step": name,
            **fields,
            "started_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }
        started = time.monotonic()
        try:
            yield event
        except Exception as exc:
            event["outcome"] = f"error:{type(exc).__name__}"  # the class only, never the message
            raise
        finally:
            event.setdefault("outcome", "ok")
            event["duration_ms"] = int((time.monotonic() - started) * 1000)
            self.events.append(_check(event))

    def write(self, records: RecordStore, run_id: str) -> Path:
        return records.write_lines(KIND, run_id, self.events)


def read_trace(records: RecordStore, run_id: str) -> list[dict]:
    path = records.root / KIND / f"{run_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def render(events: list[dict]) -> str:
    """A plain table, one row per step, in the order the steps ran."""
    rows = [[str(e.get(c, "")) for c in _COLUMNS] for e in events]
    widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(_COLUMNS)]
    lines = [[c.ljust(w) for c, w in zip(_COLUMNS, widths, strict=True)]]
    lines += [[v.ljust(w) for v, w in zip(r, widths, strict=True)] for r in rows]
    return "\n".join("  ".join(line).rstrip() for line in lines)
