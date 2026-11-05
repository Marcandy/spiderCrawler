from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_verifier import ROW, SOURCE, make

from spidercrawler.config import load_config
from spidercrawler.digest import build
from spidercrawler.extractor import CallResult, build_extraction
from spidercrawler.records import Item, RecordStore, Run
from spidercrawler.router import decide
from spidercrawler.status import monitoring_status

NOW = datetime(2026, 11, 9, 14, tzinfo=UTC)
SOURCES = load_config(Path(__file__).parent.parent / "sources.yaml").sources


def add_run(store, started, status="ok", broken=(), seed=False):
    run = Run(
        id=Run.make_id(started),
        started_at=started,
        finished_at=started,
        seed=seed,
        sources_checked=8 - len(broken),
        sources_broken=list(broken),
        items_found=0,
        documents_fetched=0,
        requests_made=9,
        status=status,
    )
    store.write(run)
    return run


def add_item(
    store, key, run, source="crim", title="Proposed Amendment of Pa.R.Crim.P. 632", seeded=False
):
    item = Item(
        id=Item.make_id(key),
        item_key=key,
        source_id=source,
        table="proposed",
        title=title,
        row_date_literal=None,
        doc_urls=[f"https://www.pacourts.us{key}"],
        first_seen_run=run.id,
        seeded=seeded,
    )
    store.write(item)
    return item


def add_extraction(store, item, output=None):
    result = CallResult(output or make().model_dump(mode="json"), None, 1, 1)
    ext = build_extraction(
        item.id,
        SOURCE,
        result,
        model="m",
        effort="medium",
        prompt_version="v",
        row=ROW,
        rule_prefix="Pa.R.Crim.P.",
        document_flags=[],
    )
    store.write(ext)
    return ext


def test_never_run(tmp_path):
    status = monitoring_status(RecordStore(tmp_path), SOURCES, NOW)
    assert status.headline == "Monitoring has not run yet." and not status.healthy


def test_healthy_week_counts_new_items_but_not_seeded_history(tmp_path):
    store = RecordStore(tmp_path)
    seed = add_run(store, NOW - timedelta(days=14), seed=True)
    add_item(store, "/old.pdf", seed, seeded=True)  # history, not "new"
    week = add_run(store, NOW - timedelta(hours=2))
    add_item(store, "/new.pdf", week)
    status = monitoring_status(store, SOURCES, NOW)
    assert status.healthy
    assert status.headline == (
        "Checked 2026-11-09: 1 new criminal-rule item in the last 7 days. 1 item awaiting human review."
    )
    assert (status.tracked_items, status.awaiting_extraction, status.monitoring_since) == (
        1,
        1,
        NOW - timedelta(days=14),
    )


def test_quiet_week_says_so(tmp_path):
    store = RecordStore(tmp_path)
    add_run(store, NOW - timedelta(hours=2))
    assert monitoring_status(store, SOURCES, NOW).headline == (
        "Checked 2026-11-09: 0 new criminal-rule items in the last 7 days."
    )


def test_stale_when_no_successful_check_for_over_a_week(tmp_path):
    store = RecordStore(tmp_path)
    add_run(store, NOW - timedelta(days=9))
    status = monitoring_status(store, SOURCES, NOW)
    assert not status.healthy
    assert status.headline == "Monitoring is stale: the last successful check was 2026-10-31."


def test_failed_latest_run_names_broken_sources(tmp_path):
    store = RecordStore(tmp_path)
    add_run(store, NOW - timedelta(days=7))
    add_run(store, NOW - timedelta(hours=1), status="failed", broken=["crim"])
    status = monitoring_status(store, SOURCES, NOW)
    assert not status.healthy and status.broken_sources == ["crim"]
    assert "failed (crim); results may be incomplete" in status.headline


def test_never_succeeded(tmp_path):
    store = RecordStore(tmp_path)
    add_run(store, NOW - timedelta(hours=1), status="failed", broken=["crim"])
    assert monitoring_status(store, SOURCES, NOW).headline == "No check has succeeded yet."


def test_review_outcome_counts(tmp_path):
    store = RecordStore(tmp_path)
    run = add_run(store, NOW - timedelta(hours=2))
    approved, rejected, pending = (add_item(store, f"/{k}.pdf", run) for k in ("a", "b", "c"))
    store.write(decide(add_extraction(store, approved), "approve", "maintainer", now=NOW))
    store.write(decide(add_extraction(store, rejected), "reject", "maintainer", now=NOW))
    add_extraction(store, pending)
    s = monitoring_status(store, SOURCES, NOW)
    assert (s.approved, s.rejected, s.awaiting_review, s.awaiting_extraction) == (1, 1, 1, 0)


def test_only_criminal_rule_items_from_context_committees_count(tmp_path):
    store = RecordStore(tmp_path)
    run = add_run(store, NOW - timedelta(hours=2))
    add_item(store, "/civ.pdf", run, source="civil", title="Proposed Amendment of Pa.R.Civ.P. 1910")
    add_item(
        store,
        "/joint.pdf",
        run,
        source="civil",
        title="Amendment of Pa.R.Crim.P. 150 and Pa.R.Civ.P. 243",
    )
    assert monitoring_status(store, SOURCES, NOW).tracked_items == 1


def test_digest_shows_status_but_never_unreviewed_content(tmp_path):
    store = RecordStore(tmp_path)
    run = add_run(store, NOW - timedelta(hours=2))
    pending = add_item(store, "/pending.pdf", run, title="Secret pending item about Rule 632")
    add_extraction(store, pending)
    out = tmp_path / "site"
    build(store, out, NOW, {"crim": "Pa.R.Crim.P."}, SOURCES)
    html = (out / "index.html").read_text()
    assert "1 item awaiting human review" in html and "8 of 8 committee pages" in html
    assert "Secret pending item" not in html
    assert "Secret pending item" not in (out / "digest.json").read_text()
    assert '"healthy": true' in (out / "digest.json").read_text()


def test_page_carries_last_success_for_the_client_side_staleness_check(tmp_path):
    store = RecordStore(tmp_path)
    add_run(store, NOW - timedelta(hours=2))
    build(store, tmp_path / "site", NOW, {}, SOURCES)
    html = (tmp_path / "site" / "index.html").read_text()
    assert f'data-last-ok="{(NOW - timedelta(hours=2)).isoformat()}"' in html
    assert 'class="stale-warning" hidden' in html and "8 * 24 * 3600 * 1000" in html
