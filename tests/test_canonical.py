from datetime import date

import pytest

from spidercrawler.canonical import canonicalize, parse_date, rule_ids
from spidercrawler.schema import ChangeSummary

CRIM = "Pa.R.Crim.P."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Rule 632 is amended", ["Pa.R.Crim.P. 632"]),
        ("Pa.R.Crim.P. 702", ["Pa.R.Crim.P. 702"]),
        (
            "Notice and Proposed Amendment of Pa.R.Crim.P. 312 and 319",
            ["Pa.R.Crim.P. 312", "Pa.R.Crim.P. 319"],
        ),
        (
            "Order Amending Rules 115, 550, 590, And 591 of the Pennsylvania Rules of Criminal Procedure",
            ["Pa.R.Crim.P. 115", "Pa.R.Crim.P. 550", "Pa.R.Crim.P. 590", "Pa.R.Crim.P. 591"],
        ),
        (
            "Proposed Amendments of Pa.Rs.Crim.P. 490 and 790",
            ["Pa.R.Crim.P. 490", "Pa.R.Crim.P. 790"],
        ),
        ("Proposed Amendment of Pa.Crim.P.150", ["Pa.R.Crim.P. 150"]),  # site typo
        ("Proposed Adoption of Pa.R.Crim.P. 579.1", ["Pa.R.Crim.P. 579.1"]),
        ("Replacement with Pa.R.Crim.P. 520.1-520.19", ["Pa.R.Crim.P. 520.1-520.19"]),
        (
            "Amendment of Pa.R.A.P. 1517; Adoption of Pa.R.C.P. 243",
            ["Pa.R.A.P. 1517", "Pa.R.Civ.P. 243"],
        ),
        ("Pa.R.C.P.M.D.J. No. 201", ["Pa.R.Civ.P.M.D.J. 201"]),
        ("Publication Report - Competency to be Executed", []),
        ("Order (Sept. 16, 2026)", []),  # dates are not rule numbers
        ("Rule 632 and Rule 632", ["Pa.R.Crim.P. 632"]),
    ],
)
def test_rule_ids(text, expected):
    assert rule_ids(text, CRIM) == expected


def test_bare_rule_takes_committee_prefix():
    assert rule_ids("Rule 404", "Pa.R.E.") == ["Pa.R.E. 404"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("April 01, 2027", date(2027, 4, 1)),
        ("effective April 1, 2027", date(2027, 4, 1)),
        ("Sept. 16, 2026", date(2026, 9, 16)),
        ("4/1/2027", date(2027, 4, 1)),
        ("30 days after publication in the Pennsylvania Bulletin", None),
        ("immediately", None),
        (None, None),
        ("February 30, 2027", None),
        ("April 1, 2027 or July 1, 2027", None),  # ambiguous
        ("April 1, 2027 (April 1, 2027)", date(2027, 4, 1)),  # same date twice is fine
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def make_summary(**kw):
    base = dict(
        is_substantive=True,
        stage="adopted",
        summary="s",
        rule_ids=["Rule 632"],
        effective_date_text="April 1, 2027",
        affected_parties=[],
        expungement_relevance="no",
        claims=[],
        confidence="high",
        abstain_reason=None,
    )
    return ChangeSummary(**{**base, **kw})


def test_canonicalize_keeps_unparseable_literal_ids():
    c = canonicalize(make_summary(rule_ids=["Rule 632", "the juror questionnaire rule"]), CRIM)
    assert c.rule_ids == ["Pa.R.Crim.P. 632", "the juror questionnaire rule"]
    assert c.effective_date == date(2027, 4, 1)


def test_schema_rejects_extra_fields():
    with pytest.raises(ValueError):
        ChangeSummary.model_validate({**make_summary().model_dump(), "advice": "x"})
