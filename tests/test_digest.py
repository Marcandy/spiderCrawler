import json
from datetime import UTC, datetime, timedelta

from test_verifier import ROW, SOURCE, make

from spidercrawler.digest import build, collect
from spidercrawler.extractor import CallResult, build_extraction
from spidercrawler.normalize import content_hash
from spidercrawler.records import Document, Extraction, Item, RecordStore, Review
from spidercrawler.router import decide

T0 = datetime(2026, 11, 2, tzinfo=UTC)
URL = "https://www.pacourts.us/Storage/media/pdfs/20260917/rule632.pdf"


def setup(tmp_path, title="Rule 632 is amended", summary=None):
    store = RecordStore(tmp_path)
    store.write(
        Item(
            id="item-1",
            item_key="/a.pdf",
            source_id="crim",
            table="approved",
            title=title,
            row_date_literal="April 01, 2027",
            doc_urls=[URL, "javascript:alert(1)//x.pdf"],
            first_seen_run="run-1",
            seeded=False,
        )
    )
    store.write(
        Document(
            id="doc-1",
            item_id="item-1",
            url=URL,
            fetched_at=T0,
            content_hash=content_hash(SOURCE),
            text=SOURCE,
            text_len=len(SOURCE),
            markup_lost=False,
            no_text=False,
        )
    )
    out = make(**({"summary": summary} if summary else {})).model_dump(mode="json")
    ext = build_extraction(
        "item-1",
        SOURCE,
        CallResult(out, None, 1, 1),
        model="claude-sonnet-5-5",
        effort="medium",
        prompt_version="extract_v1:abc",
        row=ROW,
        rule_prefix="Pa.R.Crim.P.",
        document_flags=[],
    )
    store.write(ext)
    return store, ext


def review(store, ext, decision, at=T0, **kw):
    r = decide(ext, decision, "maintainer", now=at, source_text=SOURCE, row=ROW, **kw)
    store.write(r)
    return r


def test_only_approved_items_are_published(tmp_path):
    store, ext = setup(tmp_path)
    assert collect(store) == []  # pending
    review(store, ext, "reject")
    assert collect(store) == []
    review(store, ext, "approve", at=T0 + timedelta(hours=1))
    (entry,) = collect(store)
    assert entry.review.decision == "approve"


def test_newest_decision_wins(tmp_path):
    store, ext = setup(tmp_path)
    review(store, ext, "approve")
    review(store, ext, "reject", at=T0 + timedelta(hours=1))
    assert collect(store) == []


def test_approval_of_an_older_extraction_does_not_publish(tmp_path):
    store, old = setup(tmp_path)
    review(store, old, "approve")
    newer = old.model_copy(
        update={
            "id": "ext-newer",
            "prompt_version": "extract_v2:def",
            "created_at": old.created_at + timedelta(days=1),
        }
    )
    store.write(newer)  # re-extracted with a new prompt, not yet reviewed
    assert collect(store) == []


def test_edited_output_replaces_model_output(tmp_path):
    store, ext = setup(tmp_path)
    edited = make(
        summary="Rule 632 now refers to persons reporting for jury service, per the order."
    ).model_dump(mode="json")
    review(store, ext, "edit", edited=edited)
    (entry,) = collect(store)
    assert entry.output["summary"].endswith("per the order.")


def test_built_page_escapes_text_has_disclaimer_and_safe_links(tmp_path):
    store, ext = setup(tmp_path, title="Rule 632 <script>alert(1)</script>")
    review(store, ext, "approve")
    out = tmp_path / "site"
    build(store, out, T0)
    html = (out / "index.html").read_text()
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "Not legal advice" in html
    assert URL in html and "javascript:" not in html


def test_every_digest_line_traces_to_stored_records(tmp_path):
    store, ext = setup(tmp_path)
    review(store, ext, "approve")
    out = tmp_path / "site"
    build(store, out, T0)
    (entry,) = json.loads((out / "digest.json").read_text())["entries"]
    assert store.exists(Item, entry["item_id"])
    assert store.exists(Extraction, entry["extraction_id"])
    assert store.exists(Review, entry["review_id"])
    assert entry["model_id"] == "claude-sonnet-5-5" and entry["prompt_version"] == "extract_v1:abc"
    assert entry["rule_ids"] == ["Pa.R.Crim.P. 632"] and entry["effective_date"] == "2027-04-01"


def test_empty_digest(tmp_path):
    store, _ = setup(tmp_path)
    build(store, tmp_path / "site", T0)
    assert "No approved items yet" in (tmp_path / "site" / "index.html").read_text()


def test_reviewer_corrections_to_rule_ids_and_date_are_what_publishes(tmp_path):
    store, ext = setup(tmp_path)
    edited = make(rule_ids=["Rule 632"], effective_date_text=None).model_dump(mode="json")
    edited["claims"] = [c for c in edited["claims"] if c["field"] != "effective_date"]
    review(store, ext, "edit", edited=edited)
    out = tmp_path / "site"
    build(store, out, T0, {"crim": "Pa.R.Crim.P."})
    (entry,) = json.loads((out / "digest.json").read_text())["entries"]
    assert entry["effective_date"] is None  # model said 2027-04-01; the reviewer removed it
    assert "effective 2027-04-01" not in (out / "index.html").read_text()
