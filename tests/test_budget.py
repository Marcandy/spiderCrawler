import pytest
from pydantic import ValidationError

from spidercrawler.budget import Budget, estimate_input_tokens
from spidercrawler.config import Limits

TEXT = "x" * 3_000  # ~1,000 tokens by the conservative estimate


def test_extraction_cap_stops_and_stays_stopped():
    budget = Budget(max_extractions=2, max_tokens=10**9, max_output_tokens=1_000)
    assert budget.try_start(TEXT) and budget.try_start(TEXT)
    assert not budget.try_start(TEXT)
    assert "extraction cap of 2" in budget.stopped_reason
    assert budget.extractions == 2


def test_token_budget_refuses_a_call_that_might_not_fit():
    per_call = estimate_input_tokens(TEXT) + 1_000
    budget = Budget(max_extractions=20, max_tokens=per_call * 2, max_output_tokens=1_000)
    assert budget.try_start(TEXT)
    budget.record(input_tokens=per_call - 1_000, output_tokens=1_000)
    assert budget.try_start(TEXT)
    budget.record(input_tokens=per_call - 1_000, output_tokens=1_000)
    assert not budget.try_start(TEXT)
    assert budget.stopped_reason.startswith("token budget")


def test_fails_closed_after_first_refusal_even_for_small_items():
    budget = Budget(max_extractions=20, max_tokens=5_000, max_output_tokens=1_000)
    assert not budget.try_start("x" * 30_000)  # big item does not fit
    assert not budget.try_start("tiny")  # later small item is refused too: stop means stop


def test_zero_budget_never_calls():
    assert not Budget(0, 10**9, 1_000).try_start("x")


def test_limits_cannot_raise_extraction_cap_above_plan():
    with pytest.raises(ValidationError):
        Limits(max_extractions_per_run=21)
    budget = Budget.from_limits(Limits())
    assert (budget.max_extractions, budget.max_tokens) == (20, 400_000)
