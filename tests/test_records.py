import json
from datetime import UTC, datetime

import pytest

from spidercrawler.records import (
    Document,
    Item,
    ItemEvent,
    RecordConflictError,
    RecordStore,
    Run,
)
from spidercrawler.store import rebuild

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def make_item(key="/Storage/media/pdfs/20260917/a.pdf", title="Rule 632 is amended"):
    return Item(
        id=Item.make_id(key),
        item_key=key,
        source_id="crim",
        table="approved",
        title=title,
        row_date_literal="April 01, 2027",
        doc_urls=["https://www.pacourts.us" + key],
        first_seen_run="run-1",
        seeded=False,
    )


def make_run(started=T0, **kw):
    base = dict(
        id=Run.make_id(started),
        started_at=started,
        finished_at=started,
        seed=False,
        sources_checked=8,
        items_found=0,
        documents_fetched=0,
        requests_made=9,
        status="ok",
    )
    return Run(**{**base, **kw})


def test_write_creates_one_file_per_record(tmp_path):
    store = RecordStore(tmp_path)
    item = make_item()
    assert store.write(item) is True
    path = tmp_path / "items" / f"{item.id}.json"
    assert json.loads(path.read_text())["title"] == "Rule 632 is amended"
    assert list(store.iter(Item)) == [item]


def test_rewriting_identical_record_is_noop(tmp_path):
    store = RecordStore(tmp_path)
    store.write(make_item())
    assert store.write(make_item()) is False


def test_records_are_never_overwritten(tmp_path):
    store = RecordStore(tmp_path)
    store.write(make_item())
    with pytest.raises(RecordConflictError):
        store.write(make_item(title="Something else"))
    assert next(store.iter(Item)).title == "Rule 632 is amended"


def test_ids_are_content_derived():
    assert Item.make_id("/a.pdf") == Item.make_id("/a.pdf")
    assert Item.make_id("/a.pdf") != Item.make_id("/b.pdf")
    assert Document.make_id("u", "h1") != Document.make_id("u", "h2")


def test_no_temp_files_left_behind(tmp_path):
    RecordStore(tmp_path).write(make_item())
    assert not list(tmp_path.rglob("*.tmp"))


def test_iter_missing_kind_is_empty(tmp_path):
    assert list(RecordStore(tmp_path).iter(Run)) == []


def test_latest_run(tmp_path):
    store = RecordStore(tmp_path)
    assert store.latest_run() is None
    store.write(make_run(T0))
    later = T0.replace(hour=13)
    store.write(make_run(later))
    assert store.latest_run().started_at == later


def test_rebuild_loads_all_records_and_tracks_current_table(tmp_path):
    store = RecordStore(tmp_path / "records")
    item = make_item()
    store.write(item)
    store.write(
        ItemEvent(
            id=ItemEvent.make_id("run-2", item.id, "prior_proposed"),
            item_id=item.id,
            run_id="run-2",
            event="table_moved",
            from_table="approved",
            to_table="prior_proposed",
        )
    )
    store.write(
        Document(
            id=Document.make_id(item.doc_urls[0], "h"),
            item_id=item.id,
            url=item.doc_urls[0],
            fetched_at=T0,
            content_hash="h",
            text="Rule 632.",
            text_len=9,
            markup_lost=False,
            no_text=False,
        )
    )
    store.write(make_run())

    db = tmp_path / "cache.sqlite"
    conn = rebuild(store, db)
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    row = conn.execute("SELECT current_table FROM item_current").fetchone()
    assert row["current_table"] == "prior_proposed"
    conn.close()

    # Rebuilding from scratch gives the same result.
    conn = rebuild(store, db)
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    conn.close()


def test_conflict_detected_even_if_file_appears_after_check(tmp_path, monkeypatch):
    """Simulate another writer creating the file between our checks."""
    store = RecordStore(tmp_path)
    item = make_item()
    other = make_item(title="Written by someone else")
    real_link = __import__("os").link

    def racing_link(src, dst):
        RecordStore(tmp_path).path_for("items", item.id).write_text(other.model_dump_json())
        return real_link(src, dst)

    monkeypatch.setattr("spidercrawler.records.os.link", racing_link)
    with pytest.raises(RecordConflictError):
        store.write(item)
    assert "someone else" in store.path_for("items", item.id).read_text()


def test_extraction_and_review_round_trip(tmp_path):
    from spidercrawler.records import Extraction, Review

    store = RecordStore(tmp_path)
    ext_id = Extraction.make_id("item-1", "claude-sonnet-5-5", "medium", "v1", "1", "h")
    ext = Extraction(
        id=ext_id,
        item_id="item-1",
        model_id="claude-sonnet-5-5",
        effort="medium",
        prompt_version="v1",
        schema_version="1",
        input_hash="h",
        output=None,
        canonical=None,
        error="refusal",
        verifier_status="error",
        confidence=None,
        queue_status="needs_attention",
        created_at=T0,
    )
    review = Review(
        id=Review.make_id(ext_id, T0),
        extraction_id=ext_id,
        reviewer="maintainer",
        decision="reject",
        note="model refused",
        decided_at=T0,
    )
    store.write(ext)
    store.write(review)
    assert list(store.iter(Extraction)) == [ext]
    assert list(store.iter(Review)) == [review]
    # Same inputs give the same id, so a cached re-run is idempotent.
    assert Extraction.make_id("item-1", "claude-sonnet-5-5", "medium", "v1", "1", "h") == ext_id


def test_cache_has_extractions_reviews_and_queue_view(tmp_path):
    from test_verifier import ROW, SOURCE, make

    from spidercrawler.extractor import CallResult, build_extraction
    from spidercrawler.router import decide

    store = RecordStore(tmp_path / "records")
    store.write(make_item())
    ext = build_extraction(
        make_item().id,
        SOURCE,
        CallResult(make().model_dump(mode="json"), None, 5, 6),
        model="m",
        effort="medium",
        prompt_version="v",
        row=ROW,
        rule_prefix="Pa.R.Crim.P.",
        document_flags=[],
    )
    store.write(ext)
    store.write(decide(ext, "approve", "maintainer", now=T0))
    conn = rebuild(store, tmp_path / "cache.sqlite")
    row = conn.execute("SELECT queue_status, decision FROM queue").fetchone()
    assert (row["queue_status"], row["decision"]) == ("ready_for_review", "approve")
    assert conn.execute("SELECT input_tokens + output_tokens FROM extractions").fetchone()[0] == 11
    conn.close()
