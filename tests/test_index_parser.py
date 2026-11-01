from conftest import CRIM_URL, with_empty_tables, with_new_proposed_row, with_renamed_heading

from spidercrawler.index_parser import parse_committee_page
from spidercrawler.parse_health import check_health

EVIDENCE_URL = CRIM_URL.replace(
    "criminal-procedural-rules-committee", "committee-on-rules-of-evidence"
)


def test_parses_all_three_tables_from_real_page(crim_html):
    result = parse_committee_page(crim_html, CRIM_URL)
    assert result.tables_found == {"proposed", "approved", "prior_proposed"}
    by_table = {t: [r for r in result.rows if r.table == t] for t in result.tables_found}
    assert len(by_table["proposed"]) == 1
    assert len(by_table["approved"]) == 3
    assert len(by_table["prior_proposed"]) == 113


def test_row_fields(crim_html):
    rows = parse_committee_page(crim_html, CRIM_URL).rows
    approved = next(r for r in rows if r.table == "approved")
    assert approved.title == "Rule 632 is amended"
    assert approved.date_literal == "April 01, 2027"
    assert approved.doc_urls == (
        "https://www.pacourts.us/Storage/media/pdfs/20260917/185751-criminal3of2024-ruletext.pdf",
        "https://www.pacourts.us/Storage/media/pdfs/20260917/185829-criminal3of2024-order.pdf",
        "https://www.pacourts.us/Storage/media/pdfs/20260917/185910-criminal3of2024-adoptionreport.pdf",
    )
    assert approved.item_key == "/storage/media/pdfs/20260917/185751-criminal3of2024-ruletext.pdf"


def test_page_chrome_is_never_returned(crim_html):
    rows = parse_committee_page(crim_html, CRIM_URL).rows
    blob = " ".join(r.title + " " + (r.date_literal or "") for r in rows)
    assert "Esq." not in blob  # committee member list
    assert "Committee Members" not in blob


def test_every_row_has_a_pdf_key(crim_html, evidence_html):
    for html, url in [(crim_html, CRIM_URL), (evidence_html, EVIDENCE_URL)]:
        rows = parse_committee_page(html, url).rows
        assert rows and all(r.item_key for r in rows)


def test_other_committee_page_uses_same_template(evidence_html):
    result = parse_committee_page(evidence_html, EVIDENCE_URL)
    assert result.tables_found == {"proposed", "approved", "prior_proposed"}
    assert check_health(result, known_items=10).ok


def test_new_row_is_parsed(crim_html):
    before = parse_committee_page(crim_html, CRIM_URL).rows
    after = parse_committee_page(with_new_proposed_row(crim_html), CRIM_URL).rows
    added = set(after) - set(before)
    assert [r.title for r in added] == ["Proposed Amendment of Pa.R.Crim.P. 999"]


def test_health_ok_on_real_page(crim_html):
    assert check_health(parse_committee_page(crim_html, CRIM_URL), known_items=117).ok


def test_renamed_heading_marks_broken(crim_html):
    health = check_health(parse_committee_page(with_renamed_heading(crim_html), CRIM_URL), 117)
    assert not health.ok
    assert health.reasons == ["missing table: approved"]


def test_empty_tables_mark_broken_when_items_were_known(crim_html):
    result = parse_committee_page(with_empty_tables(crim_html), CRIM_URL)
    assert result.rows == []
    assert not check_health(result, known_items=117).ok
    assert check_health(result, known_items=0).ok  # brand-new committee with nothing yet


def test_unrelated_html_marks_broken():
    result = parse_committee_page("<html><body><h1>Maintenance</h1></body></html>", CRIM_URL)
    assert len(check_health(result, known_items=0).reasons) == 3


def test_non_pdf_links_are_ignored_and_key_uses_first_pdf():
    html = """<table><caption>Approved Rules</caption><tbody><tr><td>
      <a href="/news/item">Order announcing rule 1</a>
      <ul class="rulechanges"><li><a href="/Storage/media/pdfs/20260101/x-ruletext.PDF">Rule text</a></li></ul>
    </td><td>March 1, 2027</td></tr></tbody></table>"""
    (row,) = parse_committee_page(html, CRIM_URL).rows
    assert row.title == "Order announcing rule 1"
    assert row.doc_urls == ("https://www.pacourts.us/Storage/media/pdfs/20260101/x-ruletext.PDF",)
    assert row.item_key == "/storage/media/pdfs/20260101/x-ruletext.pdf"
