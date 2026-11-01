"""End to end through the pipeline with a mock network: no real requests."""

import httpx
import pytest
from conftest import FIXTURES, with_new_proposed_row, with_renamed_heading
from typer.testing import CliRunner

from spidercrawler.cli import app
from spidercrawler.config import Config
from spidercrawler.fetcher import Fetcher
from spidercrawler.pipeline import run_pipeline
from spidercrawler.records import Document, Item, RecordStore, Run

BASE = "https://www.pacourts.us/courts/supreme-court/committees/rules-committees/"
CRIM = BASE + "criminal-procedural-rules-committee"
EVID = BASE + "committee-on-rules-of-evidence"
NEW_PDF = "https://www.pacourts.us/Storage/media/pdfs/20261101/120000-proposed-rule-999.pdf"
RULE632 = (FIXTURES / "pdfs" / "rule632-ruletext.pdf").read_bytes()


def make_config(cap=30):
    return Config.model_validate(
        {
            "base_url": BASE,
            "contact_email": "maintainer@example.org",
            "limits": {"min_delay_seconds": 5, "max_requests_per_run": cap},
            "sources": [
                {
                    "id": "crim",
                    "name": "Criminal",
                    "slug": "criminal-procedural-rules-committee",
                    "role": "focus",
                },
                {
                    "id": "evidence",
                    "name": "Evidence",
                    "slug": "committee-on-rules-of-evidence",
                    "role": "context",
                },
            ],
        }
    )


class Site:
    """A fake pacourts.us. Unknown URLs (including robots.txt) return 404."""

    def __init__(self, crim_html, evidence_html):
        self.pages = {CRIM: crim_html, EVID: evidence_html}
        self.pdfs = {NEW_PDF: RULE632}
        self.etags: dict[str, str] = {}
        self.requests: list[str] = []

    def handler(self, request):
        url = str(request.url)
        self.requests.append(url)
        if url in self.pages:
            etag = self.etags.get(url)
            if etag and request.headers.get("if-none-match") == etag:
                return httpx.Response(304)
            headers = {"content-type": "text/html"} | ({"etag": etag} if etag else {})
            return httpx.Response(200, text=self.pages[url], headers=headers)
        if url in self.pdfs:
            return httpx.Response(
                200, content=self.pdfs[url], headers={"content-type": "application/pdf"}
            )
        return httpx.Response(404)


def run(site, store, seed=False, cap=30):
    config = make_config(cap)
    client = httpx.Client(transport=httpx.MockTransport(site.handler))
    fetcher = Fetcher(config, client=client, sleep=lambda s: None)
    return run_pipeline(config, store, fetcher, seed=seed)


@pytest.fixture
def site(crim_html, evidence_html):
    return Site(crim_html, evidence_html)


def test_seed_run_records_items_without_fetching_documents(tmp_path, site):
    store = RecordStore(tmp_path)
    result = run(site, store, seed=True)
    assert result.status == "ok"
    assert result.sources_checked == 2
    # Evidence has 29 rows, 28 distinct PDFs; 4 are joint packages the criminal page lists.
    assert result.items_found == 117 + 24
    assert sum(i.source_id == "evidence" for i in store.iter(Item)) == 24
    assert result.documents_fetched == 0
    assert all(i.seeded for i in store.iter(Item))
    assert not any(".pdf" in u for u in site.requests)


def test_new_item_after_seed_gets_its_document_with_markup(tmp_path, site):
    store = RecordStore(tmp_path)
    run(site, store, seed=True)
    site.pages[CRIM] = with_new_proposed_row(site.pages[CRIM])
    result = run(site, store)
    assert result.status == "ok"
    assert result.items_found == 1
    assert result.documents_fetched == 1
    (doc,) = list(store.iter(Document))
    assert doc.url == NEW_PDF
    assert "{+Persons reporting for jury service+}" in doc.text
    assert not doc.markup_lost


def test_unchanged_page_is_not_reparsed(tmp_path, site):
    store = RecordStore(tmp_path)
    site.etags[CRIM] = '"v1"'
    run(site, store, seed=True)
    result = run(site, store)
    assert result.status == "ok" and result.items_found == 0
    assert store.latest_run().page_validators["crim"].etag == '"v1"'


def test_broken_source_fails_run_without_items(tmp_path, site):
    store = RecordStore(tmp_path)
    run(site, store, seed=True)
    site.pages[CRIM] = with_renamed_heading(with_new_proposed_row(site.pages[CRIM]))
    result = run(site, store)
    assert result.status == "failed"
    assert result.sources_broken == ["crim"]
    assert result.items_found == 0
    assert any("missing table: approved" in e for e in result.errors)


def test_runs_in_the_same_second_do_not_collide(tmp_path, site):
    store = RecordStore(tmp_path)
    run(site, store, seed=True)
    run(site, store)
    assert len(list(store.iter(Run))) == 2


def test_one_failing_source_does_not_stop_the_others(tmp_path, site):
    del site.pages[CRIM]  # 404
    result = run(site, RecordStore(tmp_path), seed=True)
    assert result.status == "ok"
    assert result.sources_checked == 1
    assert result.items_found == 28  # the evidence page lists one PDF twice
    assert any(e.startswith("crim:") and "404" in e for e in result.errors)


def test_request_cap_stops_cleanly_and_documents_wait(tmp_path, site):
    store = RecordStore(tmp_path)
    run(site, store, seed=True)
    site.pages[CRIM] = with_new_proposed_row(site.pages[CRIM])
    # robots + crim + evidence = 3 requests; no room for the PDF.
    result = run(site, store, cap=3)
    assert result.items_found == 1 and result.documents_fetched == 0
    assert any("request cap" in e for e in result.errors)
    # Next run has room: the waiting document is fetched.
    assert run(site, store).documents_fetched == 1


def test_cli_refuses_without_contact_email(tmp_path, monkeypatch):
    monkeypatch.delenv("SPIDERCRAWLER_CONTACT_EMAIL", raising=False)
    sources = FIXTURES.parent.parent / "sources.yaml"
    result = CliRunner().invoke(app, ["run", "--config", str(sources), "--data-dir", str(tmp_path)])
    assert result.exit_code == 2
    assert "contact email" in result.output
