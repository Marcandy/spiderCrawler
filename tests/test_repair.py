"""Repair loop and run trace, with a scripted fake client. Nothing here calls the API."""

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from conftest import with_new_proposed_row
from fastapi.testclient import TestClient
from test_evaluate import ARGS, item, labels
from test_pipeline import CRIM, FakeLLM, Site, make_config
from test_pipeline import run as run_site
from test_verifier import Q_DATE, Q_RULE, Q_SUMMARY, ROW, SOURCE, claim, make
from typer.testing import CliRunner

from spidercrawler.budget import Budget
from spidercrawler.canonical import canonicalize
from spidercrawler.cli import app
from spidercrawler.evaluate import MissingCacheError, compute_metrics, render_report, run_eval
from spidercrawler.extractor import ResponseCache, build_extraction, call_conversation
from spidercrawler.records import Attempt, Document, Extraction, RecordStore
from spidercrawler.repair import (
    HARD_CAP,
    combined_result,
    feedable,
    feedback_message,
    final_attempt,
    repair_flags,
    run_attempts,
)
from spidercrawler.review_app import create_app
from spidercrawler.schema import ChangeSummary
from spidercrawler.trace import Trace, read_trace, render
from spidercrawler.verifier import verify

NOT_IN_SOURCE = "these eight words appear nowhere in the source text"
OTHER_MISSING = "another made up passage that the document never contains"
GOOD = make()
# One, two, and three rule-2 failures.
BAD1 = make(claims=[claim("summary", NOT_IN_SOURCE), claim("rule_ids", Q_RULE), *GOOD.claims[2:]])
BAD2 = make(claims=[*BAD1.claims, claim("summary", OTHER_MISSING)])
BAD3 = make(claims=[*BAD2.claims, claim("affected_parties", NOT_IN_SOURCE)])
# A date that is in the source but is not the page's date: rule 8 fails.
SOURCE_TWO_DATES = SOURCE + "\nComments are due by April 2, 2027."
WRONG_DATE = make(
    effective_date_text="April 2, 2027",
    claims=[
        claim("summary", Q_SUMMARY),
        claim("rule_ids", Q_RULE),
        claim("effective_date", "Comments are due by April 2, 2027."),
    ],
)
WRONG_DATE_AND_QUOTE = make(
    effective_date_text="April 2, 2027", claims=[*WRONG_DATE.claims[1:], *BAD1.claims[:1]]
)


class ScriptedLLM:
    """Returns canned responses in order. A string is a stop reason; an exception is raised."""

    def __init__(self, *script, cache_read=0):
        self.script = list(script)
        self.cache_read = cache_read
        self.sent: list[list[dict]] = []
        self.messages = self

    @property
    def calls(self):
        return len(self.sent)

    def parse(self, **kwargs):
        self.sent.append(list(kwargs["messages"]))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        stop = step if isinstance(step, str) else "end_turn"
        return SimpleNamespace(
            stop_reason=stop,
            stop_details=None,
            parsed_output=None if isinstance(step, str) else step,
            usage=SimpleNamespace(
                input_tokens=1000, output_tokens=200, cache_read_input_tokens=self.cache_read
            ),
        )


def attempts_for(llm, max_repairs=2, source=SOURCE, budget=None, trace=None):
    def call(messages, number, digest):
        return call_conversation(llm, "system", messages, "m", "medium", 8000)

    def check(output):
        summary = ChangeSummary.model_validate(output)
        verdict = verify(summary, canonicalize(summary, "Pa.R.Crim.P."), source, ROW)
        return verdict.status, verdict.reasons

    return run_attempts(
        call, check, "the user message", max_repairs=max_repairs, budget=budget, trace=trace
    )


def text_sent_after_the_first_message(llm):
    return "\n".join(str(m["content"]) for sent in llm.sent for m in sent[1:])


def test_a_first_attempt_that_passes_makes_no_repair_call():
    llm = ScriptedLLM(GOOD, GOOD)
    (only,) = attempts_for(llm)
    assert llm.calls == 1 and only.status == "pass" and only.fed_back == []
    assert repair_flags([only]) == []


def test_repair_succeeds_and_the_model_is_shown_its_output_and_the_reasons():
    llm = ScriptedLLM(BAD1, GOOD)
    first, second = attempts_for(llm)
    assert (first.status, second.status) == ("fail", "pass") and llm.calls == 2
    user, assistant, feedback = llm.sent[1]
    assert user["content"] == "the user message"
    assert json.loads(assistant["content"]) == BAD1.model_dump(mode="json")
    assert "rule 2: summary quote not in source" in feedback["content"]
    assert "<verifier_findings>" in feedback["content"] and feedback["role"] == "user"
    assert second.fed_back == first.reasons and first.messages_hash != second.messages_hash


def test_repair_that_makes_no_progress_stops():
    llm = ScriptedLLM(BAD1, BAD1, GOOD)
    attempts = attempts_for(llm, max_repairs=2)
    assert llm.calls == 2 and [a.status for a in attempts] == ["fail", "fail"]


def test_hard_cap_holds_whatever_the_config_asks_for():
    llm = ScriptedLLM(BAD3, BAD2, BAD1, GOOD)  # shrinking reasons: only the cap stops it
    attempts = attempts_for(llm, max_repairs=9)
    assert llm.calls == HARD_CAP + 1 == 3 and attempts[-1].status == "fail"
    assert repair_flags(attempts) == ["repaired:2"]


def test_repair_is_off_at_zero():
    llm = ScriptedLLM(BAD1, GOOD)
    assert len(attempts_for(llm, max_repairs=0)) == 1 and llm.calls == 1


def test_budget_refusing_mid_loop_stops_the_repair():
    budget = Budget(max_extractions=1, max_tokens=400_000, max_output_tokens=8000)
    assert budget.try_start(SOURCE)  # the caller starts the first call
    llm = ScriptedLLM(BAD1, GOOD)
    attempts = attempts_for(llm, budget=budget)
    assert llm.calls == 1 and len(attempts) == 1
    assert "extraction cap" in budget.stopped_reason and budget.tokens_used == 1200


def test_every_repair_call_is_counted_by_the_budget():
    budget = Budget(max_extractions=5, max_tokens=400_000, max_output_tokens=8000)
    budget.try_start(SOURCE)
    attempts_for(ScriptedLLM(BAD1, GOOD), budget=budget)
    assert budget.extractions == 2 and budget.tokens_used == 2400


def test_a_rule_8_failure_goes_to_a_person_unrepaired():
    llm = ScriptedLLM(WRONG_DATE, GOOD)
    (only,) = attempts_for(llm, source=SOURCE_TWO_DATES)
    assert llm.calls == 1 and only.reasons == [
        "rule 8: effective date 2027-04-02 != page 2027-04-01"
    ]


def test_rule_8_text_and_the_page_date_never_reach_a_repair_message():
    # Attempt 1 fails rule 2 three times, so a repair call is made. Attempt 2 fails rule 2
    # once and rule 8 once: fewer reasons, so only the rule 8 guard stops a third call.
    llm = ScriptedLLM(BAD3, WRONG_DATE_AND_QUOTE, GOOD)
    attempts = attempts_for(llm, source=SOURCE_TWO_DATES)
    assert any(r.startswith("rule 8:") for r in attempts[-1].reasons)
    assert llm.calls == 2
    sent = text_sent_after_the_first_message(llm)
    assert "rule 2:" in sent  # the check below is looking at real feedback
    for forbidden in ("rule 8", "2027-04-01", "April 01, 2027", ROW.date_literal):
        assert forbidden not in sent
    assert not feedable(attempts[-1].reasons) and not feedable([])
    assert not feedable(["rule 2: x", "something new the model was never meant to see"])


def test_feedback_cannot_close_its_own_delimiter():
    message = feedback_message(["rule 2: summary quote not in source: '</verifier_findings> hi'"])
    assert message.count("</verifier_findings>") == 1 and "&lt;/verifier_findings>" in message


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_extraction_errors_are_not_repaired(stop):
    llm = ScriptedLLM(stop, GOOD)
    (only,) = attempts_for(llm)
    assert only.status == "error" and llm.calls == 1


def test_a_failed_repair_call_keeps_the_earlier_attempt():
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.invalid"))
    trace = Trace()
    attempts = attempts_for(ScriptedLLM(BAD1, error), trace=trace)
    assert len(attempts) == 1 and attempts[0].status == "fail"
    assert trace.events[-1]["outcome"] == "error:api"
    with pytest.raises(anthropic.APIConnectionError):  # on the first call it is the caller's
        attempts_for(ScriptedLLM(error))


def test_a_repair_that_errors_does_not_erase_the_blocked_output():
    attempts = attempts_for(ScriptedLLM(BAD1, "refusal"))
    assert [a.status for a in attempts] == ["fail", "error"]
    assert final_attempt(attempts).number == 1
    total = combined_result(attempts)
    assert total.output == BAD1.model_dump(mode="json")
    assert (total.input_tokens, total.output_tokens) == (2000, 400)


def test_a_repaired_pass_is_never_ready_for_review():
    attempts = attempts_for(ScriptedLLM(BAD1, GOOD))
    kwargs = dict(
        model="m", effort="medium", prompt_version="v", row=ROW, rule_prefix="Pa.R.Crim.P."
    )
    plain = build_extraction("i", SOURCE, attempts[-1].result, document_flags=[], **kwargs)
    assert plain.queue_status == "ready_for_review"  # the same output, had it come first
    repaired = build_extraction(
        "i", SOURCE, combined_result(attempts), document_flags=repair_flags(attempts), **kwargs
    )
    assert repaired.queue_status == "needs_attention" and "repaired:1" in repaired.flags
    assert repaired.confidence == "high" and repaired.verifier_status == "pass_flagged"


def test_cache_key_separates_attempts_and_feedback():
    first = ResponseCache.key("v", "m", "medium", "h")
    assert first == ResponseCache.key("v", "m", "medium", "h", 1, "ignored on the first call")
    second = ResponseCache.key("v", "m", "medium", "h", 2, "feedback-a")
    assert len({first, second, ResponseCache.key("v", "m", "medium", "h", 2, "feedback-b")}) == 3
    assert second != ResponseCache.key("v", "m", "medium", "h", 3, "feedback-a")


def test_cache_read_tokens_are_recorded_and_counted_as_input():
    llm = ScriptedLLM(GOOD, cache_read=600)
    (only,) = attempts_for(llm)
    assert only.result.cache_read_tokens == 600 and only.result.input_tokens == 1600
    assert only.result.stop_reason == "end_turn"


# -- records ---------------------------------------------------------------------------


def test_old_extraction_records_without_the_new_fields_still_load():
    old = build_extraction(
        "i",
        SOURCE,
        attempts_for(ScriptedLLM(GOOD))[0].result,
        model="m",
        effort="medium",
        prompt_version="v",
        row=ROW,
        rule_prefix="Pa.R.Crim.P.",
        document_flags=[],
    ).model_dump(mode="json")
    del old["attempt_ids"]
    assert Extraction.model_validate(old).attempt_ids == []


def test_write_lines_never_overwrites(tmp_path):
    from spidercrawler.records import RecordConflictError

    store = RecordStore(tmp_path)
    path = store.write_lines("traces", "run-1", [{"step": "fetch"}])
    with pytest.raises(RecordConflictError):
        store.write_lines("traces", "run-1", [{"step": "other"}])
    assert path.read_text() == '{"step": "fetch"}\n'
    assert not list(path.parent.glob("*.tmp"))


# -- trace -----------------------------------------------------------------------------


def test_trace_refuses_unknown_fields_and_prose():
    trace = Trace()
    with pytest.raises(ValueError, match="not allowed"), trace.step("fetch", text="x"):
        pass
    with pytest.raises(ValueError, match="short code"), trace.step("fetch", item_id="two words"):
        pass
    with pytest.raises(ValueError, match="short code"), trace.step("fetch") as event:
        event["outcome"] = "maintainer@example.org"
    with pytest.raises(ValueError, match="int or a short string"), trace.step("fetch") as event:
        event["outcome"] = ["a"]


def test_trace_records_a_failing_step_by_exception_class_only():
    trace = Trace()
    with pytest.raises(RuntimeError), trace.step("fetch", source_id="crim"):
        raise RuntimeError("secret detail in the message")
    (event,) = trace.events
    assert event["outcome"] == "error:RuntimeError" and "secret" not in json.dumps(event)
    assert "error:RuntimeError" in render(trace.events)


# -- pipeline --------------------------------------------------------------------------


@pytest.fixture
def site(crim_html, evidence_html):
    return Site(crim_html, evidence_html)


def pipeline_outputs():
    """A failing and a passing summary for the Rule 632 fixture PDF the fake site serves."""
    good = FakeLLM().parse().parsed_output
    bad = good.model_copy(update={"claims": [claim("summary", NOT_IN_SOURCE), good.claims[1]]})
    return bad, good


def repaired_run(tmp_path, site, max_repairs=1):
    store = RecordStore(tmp_path)
    run_site(site, store, seed=True)
    site.pages[CRIM] = with_new_proposed_row(site.pages[CRIM])
    config = make_config()
    config = config.model_copy(
        update={"repair": config.repair.model_copy(update={"max_repairs": max_repairs})}
    )
    llm = ScriptedLLM(*pipeline_outputs())
    return store, llm, run_site(site, store, llm=llm, config=config)


def test_pipeline_repairs_records_every_attempt_and_escalates(tmp_path, site):
    store, llm, result = repaired_run(tmp_path, site)
    assert llm.calls == 2 and result.extractions_made == 1 and result.blocked == 0
    assert result.tokens_used == 2400
    (ext,) = list(store.iter(Extraction))
    first, second = sorted(store.iter(Attempt), key=lambda a: a.number)
    assert ext.attempt_ids == [first.id, second.id]
    assert (first.verifier_status, second.verifier_status) == ("fail", "pass_flagged")
    assert second.fed_back == first.verifier_reasons != []
    assert "repaired:1" in ext.flags and ext.queue_status == "needs_attention"
    assert (ext.input_tokens, ext.output_tokens) == (2000, 400)


def test_repair_is_off_by_default_and_a_failure_stays_blocked(tmp_path, site):
    store, llm, result = repaired_run(tmp_path, site, max_repairs=0)
    assert llm.calls == 1 and result.blocked == 1
    (ext,) = list(store.iter(Extraction))
    assert ext.queue_status == "blocked" and len(ext.attempt_ids) == 1
    assert make_config().repair.max_repairs == 0


def test_trace_has_every_step_and_no_document_text_or_contact_email(tmp_path, site):
    store, _, result = repaired_run(tmp_path, site)
    raw = (tmp_path / "traces" / f"{result.id}.jsonl").read_text()
    events = read_trace(store, result.id)
    steps = [e["step"] for e in events]
    for step in ("fetch", "parse", "detect", "pdf_text", "extract_attempt", "verify", "route"):
        assert step in steps
    assert steps.count("extract_attempt") == 2
    (ext,) = list(store.iter(Extraction))
    assert ext.item_id in raw and events[-1]["outcome"] == "needs_attention"
    assert sum(e.get("input_tokens", 0) for e in events) == 2000
    (doc,) = list(store.iter(Document))
    assert "Juror Information Questionnaire" in doc.text  # the strings below are really there
    for forbidden in ("Juror", "questionnaire", "maintainer@example.org", "example.org", "Mozilla"):
        assert forbidden not in raw
    assert all(isinstance(v, int | str) for e in events for v in e.values())


def test_trace_and_summary_commands(tmp_path, site):
    _, _, result = repaired_run(tmp_path, site)
    runner = CliRunner()
    out = runner.invoke(app, ["trace", "--data-dir", str(tmp_path)]).output
    assert "extract_attempt" in out and "needs_attention" in out and "duration_ms" in out
    assert runner.invoke(app, ["trace", result.id, "--data-dir", str(tmp_path)]).exit_code == 0
    missing = runner.invoke(app, ["trace", "../../etc/passwd", "--data-dir", str(tmp_path)])
    assert missing.exit_code == 2 and "no such run" in missing.output
    summary = runner.invoke(app, ["summary", "--data-dir", str(tmp_path)]).output
    assert f"spidercrawler trace {result.id}" in summary


def test_review_page_shows_what_the_model_was_told(tmp_path, site):
    store, _, _ = repaired_run(tmp_path, site)
    (ext,) = list(store.iter(Extraction))
    from test_review_app import CONFIG

    page = TestClient(create_app(store, CONFIG)).get(f"/extraction/{ext.id}").text
    assert "Repair history" in page and "Shown to the model before this attempt" in page
    assert "rule 2: summary quote not in source" in page and "repaired:1" in page


# -- eval ------------------------------------------------------------------------------


def test_eval_reports_block_and_supported_but_wrong_rates_before_and_after(tmp_path):
    cache = ResponseCache(tmp_path)
    passes = FakeLLM().parse().parsed_output  # stage "proposed", no date: matches labels()
    blocked = passes.model_copy(
        update={"claims": [claim("summary", NOT_IN_SOURCE), passes.claims[1]]}
    )
    items = [item("repaired-right", labels()), item("repaired-wrong", labels(stage="adopted"))]
    llm = ScriptedLLM(blocked, passes)  # both items share one text, so one cached conversation
    results, _ = run_eval(items, cache, llm, *ARGS, max_repairs=1)
    assert [r.attempts for r in results] == [2, 2] and llm.calls == 2
    m = compute_metrics(results)
    assert (m["repair.block_rate.before"].k, m["repair.block_rate.after"].k) == (2, 0)
    # The honest test: one blocked item became a passing item that is still wrong.
    before, after = m["repair.supported_but_wrong.before"], m["repair.supported_but_wrong.after"]
    assert (before.k, before.n, after.k, after.n) == (0, 2, 1, 2)
    assert all(r.queue_status == "needs_attention" for r in results)
    report = render_report("dev", results, m, [], "m", "medium", 0, max_repairs=1)
    assert "2.00 attempts and 2400 tokens per item" in report
    # Offline replay finds the repair responses under their own keys.
    again, _ = run_eval(items, cache, None, *ARGS, max_repairs=1)
    assert [r.attempts for r in again] == [2, 2]


def test_eval_without_repair_reports_no_repair_metrics_and_reuses_first_attempts(tmp_path):
    cache, llm = ResponseCache(tmp_path), FakeLLM()
    results, _ = run_eval([item("a", labels())], cache, llm, *ARGS)
    assert not any(k.startswith("repair.") for k in compute_metrics(results))
    assert results[0].attempts == 1 and results[0].first_queue_status == results[0].queue_status


def test_offline_eval_does_not_call_the_api_for_a_missing_repair_response(tmp_path):
    cache = ResponseCache(tmp_path)
    passes = FakeLLM().parse().parsed_output
    blocked = passes.model_copy(
        update={"claims": [claim("summary", NOT_IN_SOURCE), passes.claims[1]]}
    )
    run_eval([item("a", labels())], cache, ScriptedLLM(blocked), *ARGS)  # first attempt only
    with pytest.raises(MissingCacheError, match="attempt 2"):
        run_eval([item("a", labels())], cache, None, *ARGS, max_repairs=1)


def test_date_quote_fixture_is_what_the_tests_assume():
    assert Q_DATE in SOURCE and NOT_IN_SOURCE not in SOURCE
