"""Decide which parsed rows are new items (PLAN.md section 3, one kind of change)."""

from dataclasses import dataclass, field

from spidercrawler.index_parser import Row
from spidercrawler.records import Item, ItemEvent, RecordStore, TableName


@dataclass(frozen=True)
class KnownItem:
    item: Item
    current_table: TableName


@dataclass
class Detection:
    new_items: list[Item] = field(default_factory=list)
    events: list[ItemEvent] = field(default_factory=list)
    skipped_rows: list[Row] = field(default_factory=list)  # rows with no PDF to key on


def load_known(records: RecordStore) -> dict[str, KnownItem]:
    """Every known item by key, with its table after applying recorded moves."""
    items = {i.id: i for i in records.iter(Item)}
    current = {i.id: i.table for i in items.values()}
    for event in sorted(records.iter(ItemEvent), key=lambda e: e.run_id):
        if event.item_id in current:
            current[event.item_id] = event.to_table
    return {i.item_key: KnownItem(i, current[i.id]) for i in items.values()}


def detect(
    source_id: str,
    rows: list[Row],
    known: dict[str, KnownItem],
    run_id: str,
    seed: bool,
) -> Detection:
    """New keys become items; known keys in a different table become move events.

    Row order never matters. In seed mode new items are marked `seeded` so they are
    recorded but never sent for extraction.
    """
    result = Detection()
    seen: set[str] = set()
    for row in rows:
        key = row.item_key
        if key is None:
            result.skipped_rows.append(row)
            continue
        if key in seen:  # same PDF listed twice on one page: first row wins
            continue
        seen.add(key)
        if (prior := known.get(key)) is None:
            result.new_items.append(
                Item(
                    id=Item.make_id(key),
                    item_key=key,
                    source_id=source_id,
                    table=row.table,
                    title=row.title,
                    row_date_literal=row.date_literal,
                    doc_urls=list(row.doc_urls),
                    first_seen_run=run_id,
                    seeded=seed,
                )
            )
        elif prior.current_table != row.table:
            result.events.append(
                ItemEvent(
                    id=ItemEvent.make_id(run_id, prior.item.id, row.table),
                    item_id=prior.item.id,
                    run_id=run_id,
                    event="table_moved",
                    from_table=prior.current_table,
                    to_table=row.table,
                )
            )
    return result
