import io

import pytest
from conftest import FIXTURES

from spidercrawler.normalize import content_hash, quote_in_source
from spidercrawler.pdf_text import PdfError, extract_pdf_text

PDFS = FIXTURES / "pdfs"


@pytest.fixture(scope="module")
def rule632():
    """Real rule-text PDF (Pa.R.Crim.P. 632, adopted Sept. 16, 2026)."""
    return extract_pdf_text((PDFS / "rule632-ruletext.pdf").read_bytes())


@pytest.fixture(scope="module")
def report702():
    """Real notice + rule text + publication report (Pa.R.Crim.P. 702 proposal)."""
    return extract_pdf_text((PDFS / "rule702-publication-report.pdf").read_bytes())


def blank_pdf() -> bytes:
    import pypdfium2 as pdfium  # installed with pdfplumber

    doc = pdfium.PdfDocument.new()
    doc.new_page(612, 792)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_additions_are_wrapped_and_deletions_keep_brackets(rule632):
    text = rule632.text
    assert "[Each prospective juror] {+Persons reporting for jury service+} shall" in text
    assert "[(A)]{+(a)+}[Prior to voir dire:] {+Prior to Voir Dire.+}" in text
    assert "[by the jurors]" in text


def test_runs_continue_across_line_breaks(rule632):
    assert "{+located in\nsubdivision (h)+}" in rule632.text


def test_unchanged_text_is_not_marked(rule632):
    assert "(2) The president judge shall designate the method" in rule632.text


def test_flags_on_marked_documents(rule632, report702):
    for doc in (rule632, report702):
        assert doc.underlined_chars > 100
        assert not doc.markup_lost
        assert not doc.no_text


def test_report_keeps_legend_and_its_rule_text_markup(report702):
    assert "Additions to the text of the proposal are bolded and underlined" in report702.text
    assert "{+" in report702.text


def test_quotes_match_marked_text(rule632):
    assert quote_in_source(
        "Persons reporting for jury service shall complete and verify the standard",
        rule632.text,
    )


def test_markup_lost_when_underlines_cannot_be_detected(monkeypatch):
    monkeypatch.setattr("spidercrawler.pdf_text.UNDERLINE_MAX_HEIGHT", 0)
    result = extract_pdf_text((PDFS / "rule632-ruletext.pdf").read_bytes())
    assert result.underlined_chars == 0
    assert result.markup_lost
    assert "{+" not in result.text
    assert "[Each prospective juror] Persons reporting" in result.text  # text still kept


def test_scanned_or_blank_pdf_is_no_text():
    result = extract_pdf_text(blank_pdf())
    assert result.no_text
    assert result.text == ""
    assert not result.markup_lost


def test_not_a_pdf_raises():
    with pytest.raises(PdfError):
        extract_pdf_text(b"<html>not a pdf</html>")


def test_extraction_is_deterministic(rule632):
    again = extract_pdf_text((PDFS / "rule632-ruletext.pdf").read_bytes())
    assert content_hash(again.text) == content_hash(rule632.text)


def test_page_numbers_are_dropped_so_quotes_span_page_breaks(report702):
    # Page 3 of the 702 PDF ends "... 18 P.S. § 11.201(5)." then a bare "2".
    assert (
        "{+18 P.S. § 11.201(5)+}.\n\nA psychiatric or psychological examination" in report702.text
    )
    assert quote_in_source(
        "18 P.S. § 11.201(5). A psychiatric or psychological examination may be ordered",
        report702.text,
    )
    assert not report702.text.rstrip().endswith("6")


def test_private_use_glyphs_are_removed(rule632):
    assert not any("\ue000" <= ch <= "\uf8ff" for ch in rule632.text)
