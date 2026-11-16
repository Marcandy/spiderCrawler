"""Labeling, freeze, and agreement tests.

Every item is synthetic and says so. Label values here are test inputs for the tools, typed
into obviously fake items; none describes a real document.
"""

import json
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spidercrawler import eval_label as tools
from spidercrawler.cli import app
from spidercrawler.eval_data import GoldItem, Labels, Row, load_items
from spidercrawler.evaluate import render_report

GUIDE = Path(__file__).parent.parent / "eval" / "LABELING.md"
TEXT = "SYNTHETIC SOURCE TEXT. Not a real rule. {+added words+} [removed words]"
SENTINEL = "MODEL-OUTPUT-SENTINEL"
# is_substantive, stage, rule_ids, effective_date, relevance, hard, notes, save
ANSWERS = ["true", "adopted", "Pa.R.Crim.P. 9001", "null", "no", "false", "", "yes"]


def labels(**kw):
    base = dict(
        is_substantive=True,
        stage="adopted",
        rule_ids=["Pa.R.Crim.P. 9001"],
        effective_date=None,
        expungement_relevance="no",
        hard=False,
    )
    return Labels(**(base | kw))


def make_item(root, item_id, split="dev", first=None, second=None, synthetic=False):
    folder = root / split / item_id
    folder.mkdir(parents=True)
    (folder / "input.txt").write_text(TEXT, encoding="utf-8")
    gold = GoldItem(
        id=item_id,
        split=split,
        synthetic=synthetic,
        injection="SYNTHETIC inserted sentence." if synthetic else None,
        source_id="crim",
        item_id=item_id,
        rule_prefix="Pa.R.Crim.P.",
        row=Row(table="approved", title=f"SYNTHETIC title {item_id}", date_literal=None),
        labels=first,
    )
    (folder / "gold.json").write_text(gold.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if second is not None:
        other = gold.model_copy(update={"labels": second})
        (folder / tools.SECOND_GOLD).write_text(other.model_dump_json(), encoding="utf-8")
    return folder


def label(root, *args, answers=ANSWERS):
    return CliRunner().invoke(
        app,
        ["eval-label", *args, "--eval-dir", str(root), "--guide", str(GUIDE)],
        input="\n".join(answers) + "\n",
    )


def gold_labels(folder, name="gold.json"):
    return json.loads((folder / name).read_text())["labels"]


# -- typed values -----------------------------------------------------------------


def test_the_guide_has_a_definition_for_every_field():
    sections = tools.guide_sections(GUIDE.read_text(encoding="utf-8"))
    assert set(tools.LABEL_FIELDS) <= set(sections)
    assert "comment deadline" in sections["effective_date"]
    assert "Injection items" not in sections["hard"]  # stops at the next section
    with pytest.raises(ValueError, match="no section for"):
        tools.guide_sections("### `stage`\ntext")


def test_parse_value():
    assert tools.parse_value("is_substantive", " Y ") is True
    assert tools.parse_value("hard", "false") is False
    assert tools.parse_value("stage", "Proposed") == "proposed"
    assert tools.parse_value("expungement_relevance", "unsure") == "unsure"
    assert tools.parse_value("effective_date", "2027-04-01") == date(2027, 4, 1)
    assert tools.parse_value("effective_date", "null") is None
    assert tools.parse_value("rule_ids", "none") == []
    assert tools.parse_value("rule_ids", "Pa.R.Crim.P. 9001, Pa.R.E. 9002,Pa.R.Crim.P. 9001") == [
        "Pa.R.Crim.P. 9001",
        "Pa.R.E. 9002",
    ]


@pytest.mark.parametrize(
    "name,raw",
    [
        ("is_substantive", ""),
        ("is_substantive", "maybe"),
        ("stage", "pending"),
        ("expungement_relevance", "true"),
        ("effective_date", "April 1, 2027"),
        ("effective_date", ""),
        ("rule_ids", "  "),
        ("nope", "x"),
    ],
)
def test_parse_value_refuses_empty_and_out_of_schema_input(name, raw):
    with pytest.raises(ValueError):
        tools.parse_value(name, raw)


def test_non_canonical_ids_are_spotted():
    assert tools.non_canonical(["Pa.R.Crim.P. 9001", "Pa.R.Crim.P. 520.1-520.19"]) == []
    assert tools.non_canonical(["Rule 9001", "Pa.R.Crim.P. 9001"]) == ["Rule 9001"]


# -- the prompt -------------------------------------------------------------------


def test_label_writes_what_was_typed_and_shows_source_and_guide(tmp_path):
    root = tmp_path / "items"
    folder = make_item(root, "synthetic-1")
    result = label(root, "synthetic-1")
    assert result.exit_code == 0, result.output
    assert gold_labels(folder) == labels().model_dump(mode="json")
    assert TEXT in result.output
    assert "SYNTHETIC title synthetic-1" in result.output
    assert "A comment deadline is **not** an" in result.output  # the guide's definition
    assert not list(folder.glob("*.tmp"))


def test_label_never_shows_model_output_or_a_default(tmp_path, monkeypatch):
    root = tmp_path / "eval" / "items"
    folder = make_item(root, "synthetic-1")
    for name in ("cache/abc.json", "reports/dev.json", "reports/dev.md"):
        path = tmp_path / "eval" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"output": {"summary": SENTINEL, "stage": SENTINEL}}))

    def no_cache(*args, **kwargs):
        raise AssertionError("eval-label must not read the response cache")

    monkeypatch.setattr("spidercrawler.extractor.ResponseCache.get", no_cache)
    monkeypatch.chdir(tmp_path)
    # Enter alone is never accepted as a value: the prompt asks again.
    result = label(root, "synthetic-1", answers=["", *ANSWERS])
    assert result.exit_code == 0, result.output
    assert TEXT in result.output  # the output was really captured
    assert SENTINEL not in result.output
    assert result.output.count("there is no default") == 1
    assert "[" not in result.output.split("is_substantive (true / false)")[1].split("\n")[0]
    assert gold_labels(folder)["stage"] == "adopted"


def test_label_asks_again_on_a_bad_value_and_on_a_non_canonical_id(tmp_path):
    root = tmp_path / "items"
    folder = make_item(root, "synthetic-1")
    answers = ["true", "pending", "?", "adopted", "Rule 9001", "no", *ANSWERS[2:]]
    result = label(root, "synthetic-1", answers=answers)
    assert result.exit_code == 0, result.output
    assert "type one of: proposed, adopted, other" in result.output
    assert "not in the guide's canonical form: Rule 9001" in result.output
    assert gold_labels(folder)["rule_ids"] == ["Pa.R.Crim.P. 9001"]

    kept = make_item(root, "synthetic-2")
    result = label(
        root, "synthetic-2", answers=["true", "adopted", "Rule 9001", "yes", *ANSWERS[3:]]
    )
    assert gold_labels(kept)["rule_ids"] == ["Rule 9001"]


def test_label_saves_nothing_unless_confirmed(tmp_path):
    root = tmp_path / "items"
    folder = make_item(root, "synthetic-1")
    before = (folder / "gold.json").read_text()
    declined = label(root, "synthetic-1", answers=[*ANSWERS[:-1], "perhaps", "no"])
    assert declined.exit_code == 1 and "not saved" in declined.output
    cut_off = label(root, "synthetic-1", answers=ANSWERS[:3])  # input ends mid-way
    assert cut_off.exit_code == 1
    assert (folder / "gold.json").read_text() == before


def test_label_without_an_id_takes_the_next_unlabeled_item(tmp_path):
    root = tmp_path / "items"
    make_item(root, "synthetic-1", first=labels())
    todo = make_item(root, "synthetic-2", synthetic=True)
    result = label(root)
    assert result.exit_code == 0 and gold_labels(todo) is not None
    assert "SYNTHETIC injection item" in result.output
    assert "nothing left to label" in label(root).output
    assert label(root, "synthetic-9").exit_code == 2


def test_labeled_items_need_relabel(tmp_path):
    root = tmp_path / "items"
    folder = make_item(root, "synthetic-1", first=labels(stage="proposed"))
    assert label(root, "synthetic-1").exit_code == 2
    assert gold_labels(folder)["stage"] == "proposed"
    assert label(root, "synthetic-1", "--relabel").exit_code == 0
    assert gold_labels(folder)["stage"] == "adopted"


def test_second_labeler_writes_a_separate_file(tmp_path):
    root = tmp_path / "items"
    folder = make_item(root, "synthetic-1", first=labels(stage="proposed"))
    result = label(root, "synthetic-1", "--second")
    assert result.exit_code == 0, result.output
    assert gold_labels(folder)["stage"] == "proposed"  # the first labels are untouched
    assert gold_labels(folder, tools.SECOND_GOLD)["stage"] == "adopted"
    assert "proposed" not in result.output.split("You typed:")[1]


def test_write_atomic_keeps_the_old_file_when_the_write_fails(tmp_path, monkeypatch):
    path = tmp_path / "gold.json"
    path.write_text("old")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        tools.write_atomic(path, "new")
    assert path.read_text() == "old" and list(tmp_path.iterdir()) == [path]


# -- the freeze -------------------------------------------------------------------


def frozen_root(tmp_path):
    root = tmp_path / "items"
    make_item(root, "synthetic-d1", "dev", first=labels())
    make_item(root, "synthetic-t1", "test", first=labels())
    make_item(root, "synthetic-t2", "test", first=labels(stage="proposed"))
    tools.freeze(root)
    return root


def test_freeze_needs_a_fully_labeled_test_split(tmp_path):
    root = tmp_path / "items"
    with pytest.raises(tools.FreezeError, match="no test items"):
        tools.freeze(root)
    make_item(root, "synthetic-t1", "test", first=labels())
    make_item(root, "synthetic-t2", "test")
    with pytest.raises(tools.FreezeError, match="unlabeled test items: synthetic-t2"):
        tools.freeze(root)
    assert not tools.freeze_state(root).frozen


def test_freeze_guard_refuses_edits_to_test_items(tmp_path):
    root = frozen_root(tmp_path)
    state = tools.freeze_state(root)
    assert state.intact and set(state.manifest.items) == {"synthetic-t1", "synthetic-t2"}
    before = (root / "test" / "synthetic-t1" / "gold.json").read_text()

    refused = label(root, "synthetic-t1", "--relabel")
    assert refused.exit_code == 2 and "frozen test split" in refused.output
    assert label(root, "synthetic-t1", "--relabel", "--unfreeze").exit_code == 2  # no reason
    assert (root / "test" / "synthetic-t1" / "gold.json").read_text() == before
    assert [e["action"] for e in tools.read_log(root)] == ["freeze"]
    assert label(root, "synthetic-d1", "--relabel").exit_code == 0  # dev is never frozen


def test_unfreeze_edit_is_logged_and_breaks_the_freeze_until_refrozen(tmp_path):
    root = frozen_root(tmp_path)
    result = label(
        root, "synthetic-t2", "--relabel", "--unfreeze", "--reason", "synthetic typo fix"
    )
    assert result.exit_code == 0, result.output
    log = tools.read_log(root)
    assert log[-1]["action"] == "unfreeze-edit"
    assert log[-1]["detail"] == "synthetic-t2: synthetic typo fix"
    state = tools.freeze_state(root)
    assert state.changed == ["synthetic-t2"] and not state.intact

    args = ["--eval-dir", str(root)]
    assert CliRunner().invoke(app, ["eval-freeze", "--check", *args]).exit_code == 1
    assert CliRunner().invoke(app, ["eval-freeze", *args]).exit_code == 2  # already frozen
    assert CliRunner().invoke(app, ["eval-freeze", "--refreeze", *args]).exit_code == 0
    assert CliRunner().invoke(app, ["eval-freeze", "--check", *args]).exit_code == 0
    assert [e["action"] for e in tools.read_log(root)] == ["freeze", "unfreeze-edit", "refreeze"]


def test_freeze_state_sees_changed_text_and_added_or_missing_items(tmp_path):
    root = frozen_root(tmp_path)
    (root / "test" / "synthetic-t1" / "input.txt").write_text("SYNTHETIC edited text")
    make_item(root, "synthetic-t3", "test", first=labels())
    for path in (root / "test" / "synthetic-t2").iterdir():
        path.unlink()
    state = tools.freeze_state(root)
    assert (state.changed, state.added, state.missing) == (
        ["synthetic-t1"],
        ["synthetic-t3"],
        ["synthetic-t2"],
    )
    assert len(state.problems()) == 3


def test_frozen_test_split_takes_no_new_items_but_second_labels_are_fine(tmp_path):
    root = frozen_root(tmp_path)
    args = ["--eval-dir", str(root)]
    inject = CliRunner().invoke(app, ["eval-inject", "synthetic-d1", "--split", "test", *args])
    assert inject.exit_code == 2 and "frozen" in inject.output
    assert len(load_items(root, "test")) == 2
    assert (
        CliRunner().invoke(app, ["eval-inject", "synthetic-d1", "--split", "dev", *args]).exit_code
        == 0
    )
    collect = CliRunner().invoke(app, ["eval-collect", "item-x", "--split", "test", *args])
    assert collect.exit_code == 2 and "frozen" in collect.output

    assert label(root, "synthetic-t1", "--second").exit_code == 0
    assert tools.freeze_state(root).intact


def test_eval_status_shows_labeled_second_labeled_and_frozen_counts(tmp_path):
    root = frozen_root(tmp_path)
    make_item(root, "synthetic-d2", "dev")
    label(root, "synthetic-t1", "--second")
    result = CliRunner().invoke(app, ["eval-status", "--eval-dir", str(root)])
    status = json.loads(result.output[: result.output.rindex("}") + 1])
    assert status["dev"] | {"total": 2, "labeled": 1, "frozen": 0} == status["dev"]
    assert (
        status["test"] | {"total": 2, "labeled": 2, "second_labeled": 1, "frozen": 2}
        == status["test"]
    )
    assert "test split: frozen" in result.output

    (root / "test" / "synthetic-t1" / "input.txt").write_text("SYNTHETIC edited text")
    assert tools.labeling_status(root)["test"]["frozen"] == 1
    assert (
        "changed since the freeze: synthetic-t1"
        in CliRunner().invoke(app, ["eval-status", "--eval-dir", str(root)]).output
    )


def test_eval_refuses_an_unfrozen_or_altered_test_split(tmp_path):
    root = tmp_path / "items"
    make_item(root, "synthetic-t1", "test", first=labels())
    sources = str(Path(__file__).parent.parent / "sources.yaml")
    args = ["eval", "--split", "test", "--config", sources, "--eval-dir", str(root)]
    args += ["--cache-dir", str(tmp_path / "cache"), "--reports-dir", str(tmp_path / "reports")]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2 and "it is not frozen" in result.output
    tools.freeze(root)
    (root / "test" / "synthetic-t1" / "input.txt").write_text("SYNTHETIC edited text")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2 and "changed since the freeze: synthetic-t1" in result.output
    assert not (tmp_path / "reports").exists()  # nothing was scored or counted


# -- agreement --------------------------------------------------------------------


def test_cohen_kappa_two_categories_by_hand():
    a = list("yyynnnynyy")
    b = list("yynnnyynyy")
    # 8 of 10 agree: observed 0.80. Each labeler said y 6 times and n 4 times:
    # expected = 0.6*0.6 + 0.4*0.4 = 0.52. kappa = (0.80 - 0.52) / (1 - 0.52) = 0.5833...
    assert tools.cohen_kappa(a, b) == pytest.approx(0.28 / 0.48)


def test_cohen_kappa_three_categories_by_hand():
    a = ["p", "p", "a", "a", "o", "o"]
    b = ["p", "a", "a", "a", "o", "p"]
    # 4 of 6 agree: observed 2/3. Counts a: p2 a2 o2; b: p2 a3 o1:
    # expected = (2*2 + 2*3 + 2*1) / 36 = 1/3. kappa = (2/3 - 1/3) / (2/3) = 0.5
    assert tools.cohen_kappa(a, b) == pytest.approx(0.5)


def test_cohen_kappa_edges():
    assert tools.cohen_kappa(list("yyn"), list("yyn")) == pytest.approx(1.0)
    assert tools.cohen_kappa(list("yn"), list("ny")) == pytest.approx(-1.0)
    assert tools.cohen_kappa(list("yyy"), list("yyy")) is None  # no variation: undefined
    assert tools.cohen_kappa([], []) is None
    # Chance-level agreement: observed 0.5, expected 0.5.
    assert tools.cohen_kappa(list("yynn"), list("ynyn")) == pytest.approx(0.0)


def test_agreement_per_field_on_items_both_labelers_labeled(tmp_path):
    root = tmp_path / "items"
    ids = ["Pa.R.Crim.P. 9001", "Pa.R.Crim.P. 9002"]
    make_item(root, "s1", "test", labels(rule_ids=ids), labels(rule_ids=ids[::-1]))
    make_item(root, "s2", "test", labels(stage="proposed"), labels(stage="adopted"))
    make_item(
        root,
        "s3",
        "test",
        labels(stage="proposed"),
        labels(stage="proposed", effective_date=date(2030, 1, 1)),
    )
    make_item(root, "s4", "test", labels(), labels(rule_ids=[]))
    make_item(root, "s5", "test", labels())  # no second labels: not counted
    make_item(root, "s6", "dev", labels(), labels())  # other split
    rows = {r.name: r for r in tools.agreement(root, "test")}
    assert all(r.n == 4 for r in rows.values())
    assert rows["rule_ids"].agree == 3 and rows["rule_ids"].kappa is None  # order ignored
    assert rows["effective_date"].agree == 3 and rows["effective_date"].kappa is None
    # stage: first a,p,p,a; second a,a,p,a -> observed 3/4; expected (2*3 + 2*1)/16 = 0.5
    assert rows["stage"].agree == 3 and rows["stage"].kappa == pytest.approx(0.5)
    assert rows["hard"].agree == 4 and rows["hard"].kappa is None  # one value throughout

    table = tools.render_agreement(list(rows.values()))
    assert "Items labeled by both labelers: 4." in table
    assert "| stage | 3/4 = 75% | 0.50 |" in table
    assert "| rule_ids | 3/4 = 75% | n/a |" in table
    assert "| stage | n/a (0 items) | n/a |" in tools.render_agreement(
        tools.agreement(root / "none")
    )


def test_agreement_command_feeds_the_docs_block(tmp_path):
    root = tmp_path / "items"
    make_item(root, "s1", "test", labels(), labels())
    docs = tmp_path / "EVAL.md"
    docs.write_text(f"# Evaluation\n\n{tools.DOCS_START}\n_Pending._\n{tools.DOCS_END}\n\nAfter.\n")
    result = CliRunner().invoke(
        app, ["eval-agreement", "--eval-dir", str(root), "--docs", str(docs)]
    )
    assert result.exit_code == 0, result.output
    text = docs.read_text()
    assert "| stage | 1/1 = 100% | n/a |" in text and "_Pending._" not in text
    assert text.startswith("# Evaluation\n") and text.endswith("\n\nAfter.\n")
    with pytest.raises(ValueError, match="has no"):
        tools.update_docs(GUIDE, "x")


def test_report_carries_the_freeze_note_and_agreement():
    report = render_report(
        "test",
        [],
        {},
        [],
        "m",
        "medium",
        1,
        freeze_note="Test split frozen 2030-01-01",
        agreement="TABLE",
    )
    assert "- Test split frozen 2030-01-01" in report
    assert report.rstrip().endswith("## Label agreement\n\nTABLE")
    assert "Label agreement" not in render_report("dev", [], {}, [], "m", "medium", 0)
