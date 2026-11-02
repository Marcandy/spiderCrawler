"""Eval harness on a tiny hand-built set with a fake model. No API calls."""

from datetime import date

import pytest
from test_pipeline import FakeLLM  # returns a fixed summary about Rule 632
from test_verifier import SOURCE

from spidercrawler.eval_data import INJECTIONS, EvalItem, GoldItem, Labels, Row
from spidercrawler.evaluate import (
    MissingCacheError,
    compute_metrics,
    keyword_baseline,
    record_test_run,
    render_report,
    run_eval,
    wilson,
)
from spidercrawler.extractor import ResponseCache

ARGS = ("claude-sonnet-5-5", "medium", 8000, set(INJECTIONS[:5]))


def item(id_, labels, text=SOURCE, synthetic=False, injection=None):
    gold = GoldItem(
        id=id_,
        split="dev",
        synthetic=synthetic,
        injection=injection,
        source_id="crim",
        item_id=id_,
        rule_prefix="Pa.R.Crim.P.",
        row=Row(
            table="proposed", title="Proposed Amendment of Pa.R.Crim.P. 632", date_literal=None
        ),
        labels=labels,
    )
    return EvalItem(gold=gold, input_text=text, path=__import__("pathlib").Path("."))


def labels(**kw):
    base = dict(
        is_substantive=True,
        stage="proposed",
        rule_ids=["Pa.R.Crim.P. 632"],
        effective_date=None,
        expungement_relevance="no",
        hard=False,
    )
    return Labels(**{**base, **kw})


# FakeLLM quotes "Persons reporting for jury service shall complete and verify the standard"
# and "Rule 632. Juror Information Questionnaire." - both in SOURCE.
def test_live_fills_cache_then_offline_reuses_it(tmp_path):
    cache = ResponseCache(tmp_path)
    items = [item("a", labels())]
    llm = FakeLLM()
    results, _ = run_eval(items, cache, llm, *ARGS)
    assert llm.calls == 1 and results[0].all_correct
    again, _ = run_eval(items, cache, None, *ARGS)  # offline
    assert again[0].all_correct and llm.calls == 1


def test_offline_with_missing_cache_raises_instead_of_calling(tmp_path):
    with pytest.raises(MissingCacheError, match="a"):
        run_eval([item("a", labels())], ResponseCache(tmp_path), None, *ARGS)


def test_unlabeled_items_are_skipped(tmp_path):
    results, skipped = run_eval([item("u", None)], ResponseCache(tmp_path), None, *ARGS)
    assert results == [] and skipped == ["u"]


def test_metrics_on_a_mixed_set(tmp_path):
    cache, llm = ResponseCache(tmp_path), FakeLLM()
    items = [
        item("right", labels()),
        item("wrong-relevance", labels(expungement_relevance="yes", hard=True)),
        item("wrong-date", labels(effective_date=date(2027, 4, 1))),
        item(
            "inj-visible",
            labels(),
            text=SOURCE + "\n\n" + INJECTIONS[0],
            synthetic=True,
            injection=INJECTIONS[0],
        ),
        item(
            "inj-para",
            labels(),
            text=SOURCE + "\n\n" + INJECTIONS[5],
            synthetic=True,
            injection=INJECTIONS[5],
        ),
    ]
    results, _ = run_eval(items, cache, llm, *ARGS)
    m = compute_metrics(results)
    assert (m["accuracy.expungement_relevance"].k, m["accuracy.expungement_relevance"].n) == (2, 3)
    assert (m["accuracy.effective_date"].k, m["accuracy.effective_date"].n) == (2, 3)
    assert m["unsupported_claim_rate"].k == 0
    # Two items are wrong with fully supported quotes: the case the verifier cannot catch.
    assert (m["supported_but_wrong_rate"].k, m["supported_but_wrong_rate"].n) == (2, 3)
    assert (m["injection.output_unaffected"].k, m["injection.output_unaffected"].n) == (2, 2)
    assert (
        m["injection.flag_fired.regex_visible"].k,
        m["injection.flag_fired.regex_visible"].n,
    ) == (1, 1)
    assert (m["injection.flag_fired.paraphrased"].k, m["injection.flag_fired.paraphrased"].n) == (
        0,
        1,
    )
    assert m["false_block_rate"].n == 1  # only "right" is fully right
    report = render_report("dev", results, m, [], "claude-sonnet-5-5", "medium", 0)
    assert "supported_but_wrong_rate | 2/3" in report and "Failure examples" in report


def test_keyword_baseline():
    assert keyword_baseline("Rule 790 governs {+expungement+} petitions.") == ("yes", True)
    assert keyword_baseline("Rule 632. Juror questionnaire.") == ("no", False)


def test_wilson_interval():
    low, high = wilson(5, 10)
    assert 0.23 < low < 0.24 and 0.76 < high < 0.77
    assert wilson(0, 0) == (0.0, 0.0)


def test_test_split_scoring_is_counted(tmp_path):
    assert record_test_run(tmp_path) == 1
    assert record_test_run(tmp_path) == 2


def test_injection_that_changes_the_answer_counts_as_affected(tmp_path):
    cache, llm = ResponseCache(tmp_path), FakeLLM()
    items = [
        item("orig", labels()),
        item(
            "orig-inj0",
            labels(),
            text=SOURCE + "\n\n" + INJECTIONS[0],
            synthetic=True,
            injection=INJECTIONS[0],
        ),
    ]
    results, _ = run_eval(items, cache, llm, *ARGS)
    results[1].answer = {**results[1].answer, "expungement_relevance": "yes"}  # injection worked
    m = compute_metrics(results)
    assert (m["injection.output_unaffected"].k, m["injection.output_unaffected"].n) == (0, 1)
