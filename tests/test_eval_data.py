import json
from datetime import UTC, datetime

import pytest

from spidercrawler.eval_data import (
    INJECTIONS,
    Labels,
    export_item,
    load_items,
    make_injection,
    status,
)
from spidercrawler.records import Document, Item, RecordStore
from spidercrawler.verifier import _SUSPICIOUS

T0 = datetime(2026, 11, 2, tzinfo=UTC)
URL = "https://www.pacourts.us/Storage/media/pdfs/20260917/a.pdf"


def setup(tmp_path, text="Rule 632.\n\nParagraph two.\n\nParagraph three."):
    store = RecordStore(tmp_path / "records")
    item = Item(id="item-1", item_key="/a.pdf", source_id="crim", table="approved", title="Rule 632 is amended",
                row_date_literal="April 01, 2027", doc_urls=[URL], first_seen_run="run-1", seeded=True)  # fmt: skip
    store.write(item)
    store.write(Document(id="doc-1", item_id="item-1", url=URL, fetched_at=T0, content_hash="h", text=text,
                         text_len=len(text), markup_lost=False, no_text=False))  # fmt: skip
    return store, item


def test_export_writes_input_and_unlabeled_gold(tmp_path):
    store, item = setup(tmp_path)
    folder = export_item(store, item, "dev", tmp_path / "eval", "Pa.R.Crim.P.", "approved")
    assert (folder / "input.txt").read_text().startswith("Rule 632.")
    gold = json.loads((folder / "gold.json").read_text())
    assert gold["labels"] is None and gold["row"]["date_literal"] == "April 01, 2027"


def test_export_never_rewrites_a_labeled_item(tmp_path):
    store, item = setup(tmp_path)
    root = tmp_path / "eval"
    folder = export_item(store, item, "test", root, "Pa.R.Crim.P.", "approved")
    gold = json.loads((folder / "gold.json").read_text())
    gold["labels"] = Labels(is_substantive=True, stage="adopted", rule_ids=["Pa.R.Crim.P. 632"],
                            effective_date=None, expungement_relevance="no", hard=False).model_dump(mode="json")  # fmt: skip
    (folder / "gold.json").write_text(json.dumps(gold))
    export_item(store, item, "test", root, "Pa.R.Crim.P.", "approved")  # same text: no-op
    assert json.loads((folder / "gold.json").read_text())["labels"]["stage"] == "adopted"
    (folder / "input.txt").write_text("changed")
    with pytest.raises(ValueError, match="refusing"):
        export_item(store, item, "test", root, "Pa.R.Crim.P.", "approved")


def test_export_requires_documents(tmp_path):
    store = RecordStore(tmp_path)
    item = Item(id="item-2", item_key="/b.pdf", source_id="crim", table="approved", title="t",
                row_date_literal=None, doc_urls=["https://x/b.pdf"], first_seen_run="r", seeded=True)  # fmt: skip
    with pytest.raises(ValueError, match="not fetched"):
        export_item(store, item, "dev", tmp_path / "eval", "Pa.R.Crim.P.", "approved")


def test_injection_item_is_marked_synthetic_and_trips_the_flag(tmp_path):
    store, item = setup(tmp_path)
    root = tmp_path / "eval"
    src = export_item(store, item, "dev", root, "Pa.R.Crim.P.", "approved")
    folder = make_injection(src, root, "test", variant=0)
    gold = json.loads((folder / "gold.json").read_text())
    assert gold["synthetic"] is True and gold["injection"] == INJECTIONS[0]
    assert INJECTIONS[0] in (folder / "input.txt").read_text()
    with pytest.raises(ValueError):
        make_injection(src, root, "test", variant=0)


def test_every_injection_template_trips_suspicious_input():
    assert all(_SUSPICIOUS.search(text) for text in INJECTIONS)


def test_status_counts(tmp_path):
    store, item = setup(tmp_path)
    root = tmp_path / "eval"
    src = export_item(store, item, "dev", root, "Pa.R.Crim.P.", "approved")
    make_injection(src, root, "dev", variant=1)
    assert status(root)["dev"] == {"total": 2, "labeled": 0, "synthetic": 1, "expungement_yes": 0}
    assert len(load_items(root, "dev")) == 2 and load_items(root, "test") == []
