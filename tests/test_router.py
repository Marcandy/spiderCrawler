from datetime import UTC, datetime, timedelta

import pytest
from test_verifier import ROW, SOURCE, make

from spidercrawler.normalize import content_hash
from spidercrawler.records import Extraction
from spidercrawler.router import DecisionRefusedError, check_edit, decide, is_publishable, route

T0 = datetime(2026, 11, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "confidence", "relevance", "queue"),
    [
        ("fail", "high", "no", "blocked"),
        ("error", None, None, "needs_attention"),
        ("pass_flagged", "high", "no", "needs_attention"),
        ("pass", "low", "no", "needs_attention"),
        ("pass", "high", "unsure", "needs_attention"),
        ("pass", "medium", "yes", "ready_for_review"),
        ("pass", "high", "no", "ready_for_review"),
    ],
)
def test_routing_table(status, confidence, relevance, queue):
    assert route(status, confidence, relevance) == queue


def extraction(queue="ready_for_review", output=True):
    return Extraction(
        id="ext-1", item_id="item-1", model_id="m", effort="medium", prompt_version="v1",
        schema_version="1", input_hash=content_hash(SOURCE),
        output=make().model_dump() if output else None,
        canonical=None, error=None if output else "refusal",
        verifier_status="pass" if queue != "blocked" else "fail", confidence="high",
        queue_status=queue, created_at=T0,
    )  # fmt: skip


def test_approve_ready_item():
    review = decide(extraction(), "approve", "maintainer", now=T0)
    assert review.decision == "approve" and is_publishable([review])


def test_cannot_approve_blocked_item():
    with pytest.raises(DecisionRefusedError, match="blocked"):
        decide(extraction("blocked"), "approve", "maintainer")


def test_cannot_approve_failed_extraction():
    with pytest.raises(DecisionRefusedError):
        decide(extraction("needs_attention", output=False), "approve", "maintainer")


def test_edit_that_passes_unblocks():
    review = decide(extraction("blocked"), "edit", "maintainer", edited=make().model_dump(),
                    source_text=SOURCE, row=ROW, now=T0)  # fmt: skip
    assert review.edited_verifier_status == "pass"
    assert is_publishable([review])


def test_edit_that_fails_verifier_is_refused():
    bad = make(summary="You should file a petition under Rule 632 right away.").model_dump()
    with pytest.raises(DecisionRefusedError, match="rule 6"):
        decide(extraction("blocked"), "edit", "maintainer", edited=bad, source_text=SOURCE, row=ROW)


def test_edit_with_invented_quote_is_refused():
    bad = make().model_dump()
    bad["claims"][0]["evidence_quote"] = (
        "Persons reporting for jury service shall be paid a daily stipend"
    )
    with pytest.raises(DecisionRefusedError, match="rule 2"):
        decide(extraction(), "edit", "maintainer", edited=bad, source_text=SOURCE, row=ROW)


def test_edit_with_schema_violation_is_refused():
    bad = {**make().model_dump(), "advice": "extra field"}
    check = check_edit(bad, SOURCE, ROW, "Pa.R.Crim.P.")
    assert not check.ok and check.reasons[0].startswith("schema:")


def test_hand_written_extraction_for_failed_model_call():
    review = decide(extraction("needs_attention", output=False), "edit", "maintainer",
                    edited=make().model_dump(), source_text=SOURCE, row=ROW, now=T0)  # fmt: skip
    assert is_publishable([review])


def test_newest_review_wins():
    approve = decide(extraction(), "approve", "maintainer", now=T0)
    reject = decide(extraction(), "reject", "maintainer", now=T0 + timedelta(hours=1))
    assert not is_publishable([approve, reject])
    assert not is_publishable([])


def test_unknown_decision():
    with pytest.raises(DecisionRefusedError):
        decide(extraction(), "publish", "maintainer")


def test_edit_verified_against_other_text_is_refused():
    edited = make().model_dump()
    edited["claims"][0]["evidence_quote"] = (
        "Persons reporting for jury service shall be paid a daily stipend"
    )
    forged = SOURCE + "\nPersons reporting for jury service shall be paid a daily stipend."
    with pytest.raises(DecisionRefusedError, match="input_hash"):
        decide(extraction(), "edit", "maintainer", edited=edited, source_text=forged, row=ROW)
