from datetime import UTC, datetime
from pathlib import Path

from test_status import SOURCES, add_extraction, add_item, add_run
from test_verifier import Q_SUMMARY, SOURCE, make
from typer.testing import CliRunner

from spidercrawler.cli import app
from spidercrawler.notice import MARKER, build_notice
from spidercrawler.records import Item, RecordStore
from spidercrawler.router import decide
from spidercrawler.status import monitoring_status

NOW = datetime(2026, 11, 9, 14, tzinfo=UTC)
SOURCES_YAML = Path(__file__).parent.parent / "sources.yaml"


def store_with_run(tmp_path):
    store = RecordStore(tmp_path)
    return store, add_run(store, NOW)


def test_no_notice_when_nothing_awaits(tmp_path):
    store, run = store_with_run(tmp_path)
    assert build_notice(store, SOURCES) is None
    add_item(store, "/seeded.pdf", run, seeded=True)  # history, never reviewed
    add_item(store, "/civil.pdf", run, source="civil", title="Civil rule 1")  # not tracked
    approved = add_item(store, "/a.pdf", run)
    store.write(decide(add_extraction(store, approved), "approve", "r"))
    rejected = add_item(store, "/b.pdf", run)
    store.write(decide(add_extraction(store, rejected), "reject", "r"))
    assert build_notice(store, SOURCES) is None


def test_one_item(tmp_path):
    store, run = store_with_run(tmp_path)
    item = add_item(store, "/Storage/media/pdfs/20261101/rule-632.pdf", run)
    add_extraction(store, item)
    notice = build_notice(store, SOURCES)
    assert notice.count == 1
    assert notice.title == "Review needed: 1 rule item awaits review"
    assert notice.body.startswith(MARKER + "\n**1 item awaits human review.**")
    assert "### Ready to review (1)" in notice.body
    assert (
        "- Proposed Amendment of Pa.R.Crim.P. 632 "
        "([rule-632.pdf](https://www.pacourts.us/Storage/media/pdfs/20261101/rule-632.pdf))"
    ) in notice.body
    assert "make data-checkout && make review DATA_DIR=data-branch/data" in notice.body
    assert "not yet summarized" not in notice.body


def test_many_items_in_both_states(tmp_path):
    store, run = store_with_run(tmp_path)
    for n in range(3):
        add_extraction(store, add_item(store, f"/ready-{n}.pdf", run, title=f"Ready {n}"))
    for n in range(2):
        add_item(store, f"/waiting-{n}.pdf", run, title=f"Waiting {n}")
    notice = build_notice(store, SOURCES)
    assert notice.count == 5
    assert notice.title == "Review needed: 5 rule items await review"
    assert "**5 items await human review.**" in notice.body
    ready, waiting = notice.body.split("### Detected, not yet summarized (2)")
    assert "### Ready to review (3)" in ready
    assert all(f"- Ready {n} (" in ready for n in range(3))
    assert all(f"- Waiting {n} (" in waiting for n in range(2))
    # Same numbers as the public status block.
    status = monitoring_status(store, SOURCES, NOW)
    assert status.awaiting_review + status.awaiting_extraction == notice.count


def test_notice_is_stable_so_the_issue_edit_is_idempotent(tmp_path):
    store, run = store_with_run(tmp_path)
    for n in range(3):
        add_extraction(store, add_item(store, f"/ready-{n}.pdf", run, title=f"Ready {n}"))
    assert build_notice(store, SOURCES) == build_notice(store, SOURCES)


def test_no_model_output_reaches_the_notice(tmp_path):
    store, run = store_with_run(tmp_path)
    item = add_item(store, "/a.pdf", run, title="Public title")
    output = make(
        summary="UNREVIEWED summary: you should file now, before a person has approved this.",
        affected_parties=["UNREVIEWED party"],
    ).model_dump(mode="json")
    ext = add_extraction(store, item, output)
    body = build_notice(store, SOURCES).body
    assert "Public title" in body  # the notice was built from this item
    assert ext.output["summary"] in store.path_for("extractions", ext.id).read_text()
    assert ext.verifier_notes and len(ext.output["claims"]) >= 3  # the list below is not empty
    private = [
        "UNREVIEWED",
        "you should",
        ext.id,
        SOURCE.splitlines()[1],  # document text
        *ext.verifier_notes,
        *(c["evidence_quote"] for c in ext.output["claims"]),
    ]
    assert Q_SUMMARY in private
    for text in private:
        assert text not in body, text


def test_website_text_cannot_inject_markdown_mentions_or_links(tmp_path):
    store, run = store_with_run(tmp_path)
    hostile = "Pa.R.Crim.P. 1 @octocat [click](https://evil.test) <img src=x> #1\n# Heading `x`"
    store.write(
        Item(
            id="item-h",
            item_key="/h.pdf",
            source_id="crim",
            table="proposed",
            title=hostile + " " + "long " * 80,
            row_date_literal=None,
            doc_urls=[
                "https://www.pacourts.us/a b).pdf",
                "javascript:alert(1)//x.pdf",
            ],
            first_seen_run=run.id,
            seeded=False,
        )
    )
    body = build_notice(store, SOURCES).body
    line = next(x for x in body.splitlines() if x.startswith("- Pa.R.Crim.P. 1"))
    assert "@octocat" not in line and "@​octocat" in line
    assert r"\[click\]\(https://evil.test\)" in line
    assert r"\<img src=x\>" in line and r"\#1 \# Heading \`x\`" in line
    assert "(https://www.pacourts.us/a%20b%29.pdf)" in line
    assert "javascript" not in body
    assert line.count("long") < 80 and "…" in line


def test_item_without_a_usable_link_still_listed(tmp_path):
    store, run = store_with_run(tmp_path)
    store.write(
        Item(
            id="item-n",
            item_key="/n.pdf",
            source_id="crim",
            table="proposed",
            title="No link",
            row_date_literal=None,
            doc_urls=["ftp://x/n.pdf"],
            first_seen_run=run.id,
            seeded=False,
        )
    )
    assert "- No link (no source link)" in build_notice(store, SOURCES).body


def test_cli_prints_body_or_title_and_nothing_when_the_queue_is_empty(tmp_path):
    store, run = store_with_run(tmp_path)
    base = ["review-notice", "--data-dir", str(tmp_path), "--config", str(SOURCES_YAML)]
    assert CliRunner().invoke(app, base).output == ""
    assert CliRunner().invoke(app, [*base, "--title"]).output == ""
    add_extraction(store, add_item(store, "/a.pdf", run))
    body = CliRunner().invoke(app, base)
    assert body.exit_code == 0 and body.output == build_notice(store, SOURCES).body
    title = CliRunner().invoke(app, [*base, "--title"]).output
    assert title == "Review needed: 1 rule item awaits review\n"
