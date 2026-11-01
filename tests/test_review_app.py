from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from test_verifier import ROW, SOURCE, make

from spidercrawler.config import load_config
from spidercrawler.extractor import CallResult, build_extraction
from spidercrawler.records import Document, Item, RecordStore, Review
from spidercrawler.review_app import create_app

URL = "https://www.pacourts.us/Storage/media/pdfs/20260917/rule632.pdf"
CONFIG = load_config(Path(__file__).parent.parent / "sources.yaml")


def setup(tmp_path, output=None, text=SOURCE):
    store = RecordStore(tmp_path)
    store.write(
        Item(
            id="item-1",
            item_key="/a.pdf",
            source_id="crim",
            table="approved",
            title="Rule 632 is amended",
            row_date_literal="April 01, 2027",
            doc_urls=[URL],
            first_seen_run="run-1",
            seeded=False,
        )
    )
    store.write(
        Document(
            id="doc-1",
            item_id="item-1",
            url=URL,
            fetched_at=datetime(2026, 11, 2, tzinfo=UTC),
            content_hash="h",
            text=text,
            text_len=len(text),
            markup_lost=False,
            no_text=False,
        )
    )
    result = CallResult(
        output if output is not None else make().model_dump(mode="json"), None, 10, 10
    )
    ext = build_extraction(
        "item-1",
        text,
        result,
        model="claude-sonnet-5-5",
        effort="medium",
        prompt_version="v",
        row=ROW,
        rule_prefix="Pa.R.Crim.P.",
        document_flags=[],
    )
    store.write(ext)
    return store, ext, TestClient(create_app(store, CONFIG))


def token(client, ext):
    page = client.get(f"/extraction/{ext.id}").text
    return page.split('name="csrf_token" value="')[1].split('"')[0]


def post(client, ext, with_token=True, **form):
    if with_token:
        form["csrf_token"] = token(client, ext)
    return client.post(
        f"/extraction/{ext.id}/decide",
        content=urlencode(form),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def blocked_output():
    bad = make().model_dump(mode="json")
    bad["claims"][0]["evidence_quote"] = (
        "Persons reporting for jury service shall be paid a daily stipend"
    )
    return bad


def test_queue_lists_extractions(tmp_path):
    _, _, client = setup(tmp_path)
    page = client.get("/").text
    assert "Rule 632 is amended" in page and "ready_for_review" in page


def test_website_text_is_escaped(tmp_path):
    hostile = SOURCE + "\n<script>alert('x')</script>"
    _, ext, client = setup(tmp_path, text=hostile)
    page = client.get(f"/extraction/{ext.id}").text
    assert "<script>alert" not in page and "&lt;script&gt;" in page


def test_approve_writes_a_review(tmp_path):
    store, ext, client = setup(tmp_path)
    assert post(client, ext, decision="approve", reviewer="m").status_code == 303
    (review,) = list(store.iter(Review))
    assert review.decision == "approve"
    assert "Publishable: <strong>yes" in client.get(f"/extraction/{ext.id}").text


def test_blocked_item_cannot_be_approved(tmp_path):
    store, ext, client = setup(tmp_path, output=blocked_output())
    assert ext.queue_status == "blocked"
    assert "disabled" in client.get(f"/extraction/{ext.id}").text
    response = post(client, ext, decision="approve")  # even if the button is bypassed
    assert response.status_code == 400 and "cannot approve" in response.text
    assert list(store.iter(Review)) == []


def test_failing_edit_is_refused_and_kept_for_fixing(tmp_path):
    import json

    store, ext, client = setup(tmp_path, output=blocked_output())
    response = post(client, ext, decision="edit", edited_json=json.dumps(blocked_output()))
    assert response.status_code == 400 and "rule 2" in response.text
    assert list(store.iter(Review)) == []


def test_passing_edit_unblocks(tmp_path):
    import json

    store, ext, client = setup(tmp_path, output=blocked_output())
    response = post(
        client, ext, decision="edit", edited_json=json.dumps(make().model_dump(mode="json"))
    )
    assert response.status_code == 303
    (review,) = list(store.iter(Review))
    assert review.decision == "edit" and review.edited_verifier_status == "pass"


def test_invalid_json_edit(tmp_path):
    _, ext, client = setup(tmp_path)
    response = post(client, ext, decision="edit", edited_json="{not json")
    assert response.status_code == 400 and "not valid JSON" in response.text


def test_unknown_extraction_is_404(tmp_path):
    _, _, client = setup(tmp_path)
    assert client.get("/extraction/nope").status_code == 404


def test_reject(tmp_path):
    store, ext, client = setup(tmp_path, output=blocked_output())
    assert post(client, ext, decision="reject", note="wrong").status_code == 303
    (review,) = list(store.iter(Review))
    assert review.note == "wrong"


def test_post_without_token_is_refused(tmp_path):
    store, ext, client = setup(tmp_path)
    assert post(client, ext, with_token=False, decision="approve").status_code == 403
    assert (
        post(client, ext, with_token=False, decision="approve", csrf_token="guess").status_code
        == 403
    )
    assert list(store.iter(Review)) == []


def test_non_local_host_is_refused(tmp_path):
    _, _, client = setup(tmp_path)
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
