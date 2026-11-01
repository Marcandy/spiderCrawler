from conftest import CRIM_URL, with_new_proposed_row

from spidercrawler.detector import detect, load_known
from spidercrawler.index_parser import Row, parse_committee_page
from spidercrawler.records import RecordStore


def row(key="a", table="proposed", title="Proposed Amendment of Pa.R.Crim.P. 1"):
    return Row(
        table=table,
        title=title,
        doc_urls=(f"https://x.test/{key}.pdf",),
        date_literal="May 1, 2027",
    )


def save(store, detection):
    for item in detection.new_items:
        store.write(item)
    for event in detection.events:
        store.write(event)


def test_first_sight_creates_items(tmp_path):
    result = detect("crim", [row("a"), row("b")], {}, "run-1", seed=False)
    assert [i.item_key for i in result.new_items] == ["/a.pdf", "/b.pdf"]
    assert not any(i.seeded for i in result.new_items)


def test_seed_mode_marks_items_seeded():
    result = detect("crim", [row("a")], {}, "run-1", seed=True)
    assert result.new_items[0].seeded


def test_known_rows_produce_nothing_regardless_of_order(tmp_path):
    store = RecordStore(tmp_path)
    save(store, detect("crim", [row("a"), row("b")], {}, "run-1", seed=True))
    result = detect("crim", [row("b"), row("a")], load_known(store), "run-2", seed=False)
    assert result.new_items == [] and result.events == []


def test_table_move_is_an_event_not_a_new_item(tmp_path):
    store = RecordStore(tmp_path)
    save(store, detect("crim", [row("a", "proposed")], {}, "run-1", seed=True))
    result = detect("crim", [row("a", "prior_proposed")], load_known(store), "run-2", False)
    assert result.new_items == []
    assert [(e.from_table, e.to_table) for e in result.events] == [("proposed", "prior_proposed")]

    save(store, result)
    assert load_known(store)["/a.pdf"].current_table == "prior_proposed"
    # Seeing it in the same table again is quiet.
    assert (
        detect("crim", [row("a", "prior_proposed")], load_known(store), "run-3", False).events == []
    )


def test_duplicate_key_on_one_page_counts_once():
    result = detect("crim", [row("a", "approved"), row("a", "prior_proposed")], {}, "run-1", False)
    assert len(result.new_items) == 1
    assert result.new_items[0].table == "approved"


def test_rows_without_pdf_are_skipped_not_keyed():
    bare = Row(table="proposed", title="No link", doc_urls=(), date_literal=None)
    result = detect("crim", [bare], {}, "run-1", False)
    assert result.new_items == [] and result.skipped_rows == [bare]


def test_real_page_seed_then_one_new_row(tmp_path, crim_html):
    store = RecordStore(tmp_path)
    rows = parse_committee_page(crim_html, CRIM_URL).rows
    seeded = detect("crim", rows, {}, "run-1", seed=True)
    assert len(seeded.new_items) == 117
    save(store, seeded)

    rows2 = parse_committee_page(with_new_proposed_row(crim_html), CRIM_URL).rows
    result = detect("crim", rows2, load_known(store), "run-2", seed=False)
    assert [i.title for i in result.new_items] == ["Proposed Amendment of Pa.R.Crim.P. 999"]
    assert result.new_items[0].row_date_literal == "January 15, 2027"
    assert not result.new_items[0].seeded
