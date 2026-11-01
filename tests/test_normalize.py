import pytest

from spidercrawler.normalize import (
    content_hash,
    for_matching,
    normalize,
    quote_in_source,
    strip_markup,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\u201cquoted\u201d and \u2018single\u2019", "\"quoted\" and 'single'"),
        ("2026\u20132027 \u2014 em", "2026-2027 - em"),
        ("a\u00a0b", "a b"),  # NBSP via NFKC
        ("of\ufb01ce", "office"),  # ligature via NFKC
        ("soft\u00adhyphen", "softhyphen"),
        ("many    spaces\tand\ttabs", "many spaces and tabs"),
        ("line one   \n   line two", "line one\nline two"),
        ("proce-\ndure", "procedure"),
        ("Pre-\nTrial", "Pre-\nTrial"),  # capitalised continuation kept
        ("a\n\n\n\n\nb", "a\n\nb"),
        ("Pa.R.J .A. 103", "Pa.R.J.A. 103"),
        ("subdivision ( h)", "subdivision (h)"),
        ("[by the jurors ]", "[by the jurors]"),
        ("Rules 115 , 550", "Rules 115, 550"),
        ("{+  added text  +}", "{+added text+}"),
        ("  padded  ", "padded"),
    ],
)
def test_normalize_rules(raw, expected):
    assert normalize(raw) == expected


def test_normalize_is_idempotent():
    raw = "\u201cRule 632.\u201d  Juror ( h) Pa.R.J .A.\n\n\n{+ new +} proce-\ndure"
    once = normalize(raw)
    assert normalize(once) == once


def test_strip_markup():
    assert strip_markup("keep {+added+} text") == "keep added text"


def test_for_matching_collapses_lines_and_markup():
    assert for_matching("Persons {+reporting+}\nfor   jury") == "Persons reporting for jury"


def test_quote_matches_across_pdf_line_breaks_and_markup():
    source = "(1) {+Persons reporting for jury service+} shall\ncomplete and verify the standard"
    assert quote_in_source("Persons reporting for jury service shall complete and verify", source)
    assert quote_in_source("{+Persons reporting for jury service+} shall complete", source)


def test_quote_with_curly_quotes_matches_straight_source():
    assert quote_in_source("the \u201cstandard\u201d form", 'the "standard" form')


def test_quote_not_in_source():
    assert not quote_in_source("shall be destroyed after trial", "shall be confidential")


def test_empty_quote_never_matches():
    assert not quote_in_source("   ", "anything")


def test_content_hash_ignores_cosmetic_differences():
    assert content_hash("a  \u201cb\u201d") == content_hash('a "b"')
    assert content_hash("a b") != content_hash("a c")
