"""Sampling plan tests. Every item here is synthetic and says so in its title."""

import random
from pathlib import Path

from typer.testing import CliRunner

from spidercrawler.cli import app
from spidercrawler.config import load_config
from spidercrawler.detector import KnownItem
from spidercrawler.eval_data import REGEX_VISIBLE_INJECTIONS
from spidercrawler.eval_sample import POSITIVE_STRATA, build_plan, load_plan, stratum_of
from spidercrawler.records import Item, RecordStore

SOURCES_YAML = Path(__file__).parent.parent / "sources.yaml"
SOURCES = {s.id: s for s in load_config(SOURCES_YAML).sources}


def known(n, source="crim", table="proposed", title="SYNTHETIC item"):
    key = f"/synthetic/{source}-{table}-{len(title)}-{n}.pdf"
    item = Item(
        id=Item.make_id(key),
        item_key=key,
        source_id=source,
        table=table,
        title=f"{title} {n}",
        row_date_literal=None,
        doc_urls=[f"https://example.test/synthetic/{source}-{table}-{n}.pdf"],
        first_seen_run="run-synthetic",
        seeded=True,
    )
    return KnownItem(item, table)


def pool(expungement=4, negatives=12):
    items = [known(n, table="proposed") for n in range(20)]
    items += [known(n, table="approved") for n in range(20)]
    items += [known(n, title="SYNTHETIC amendment of Rule 490 no.") for n in range(expungement)]
    for source in ("civil", "evidence", "juvenile"):
        items += [known(n, source=source) for n in range(negatives)]
    items += [known(n, source="appellate", title="SYNTHETIC Pa.R.Crim.P. cite") for n in range(5)]
    return items


def test_each_split_has_positives_negatives_and_injection_slots():
    plan = build_plan(pool(), SOURCES, seed=7)
    dev, test = plan.splits["dev"], plan.splits["test"]
    assert (dev.count(*POSITIVE_STRATA), dev.count("negative"), len(dev.injections)) == (10, 7, 3)
    assert (test.count(*POSITIVE_STRATA), test.count("negative"), len(test.injections)) == (
        15,
        10,
        5,
    )
    assert plan.shortfalls == []
    assert not set(plan.item_ids("dev")) & set(plan.item_ids("test"))
    for sp in (dev, test):
        assert sp.count("crim_proposed") >= 1 and sp.count("crim_adopted") >= 1
        positives = {c.item_id for c in sp.items if c.stratum in POSITIVE_STRATA}
        bases = [slot.base_item_id for slot in sp.injections]
        assert set(bases) <= positives and len(set(bases)) == len(bases)
        variants = [slot.variant for slot in sp.injections]
        assert any(v < REGEX_VISIBLE_INJECTIONS for v in variants)
        assert any(v >= REGEX_VISIBLE_INJECTIONS for v in variants)
    assert sorted(s.variant for sp in (dev, test) for s in sp.injections) == list(range(8))


def test_same_seed_gives_the_same_plan_whatever_the_input_order():
    items = pool()
    shuffled = items[:]
    random.Random(1).shuffle(shuffled)
    assert build_plan(items, SOURCES, seed=7) == build_plan(shuffled, SOURCES, seed=7)
    assert build_plan(items, SOURCES, seed=7) != build_plan(items, SOURCES, seed=8)


def test_expungement_rule_items_are_all_drawn_and_reach_both_splits():
    plan = build_plan(pool(expungement=4), SOURCES, seed=3)
    counts = [plan.splits[s].count("expungement_rule") for s in ("dev", "test")]
    assert sum(counts) == 4 and min(counts) >= 1


def test_negatives_come_from_several_committees():
    plan = build_plan(pool(), SOURCES, seed=7)
    for sp in plan.splits.values():
        assert len({c.source_id for c in sp.items if c.stratum == "negative"}) == 3


def test_other_committee_items_citing_criminal_rules_are_left_out():
    assert (
        stratum_of(
            known(1, source="appellate", title="SYNTHETIC Pa.R.Crim.P. cite"), SOURCES["appellate"]
        )
        is None
    )
    assert stratum_of(known(1, source="civil"), SOURCES["civil"]) == "negative"
    assert stratum_of(known(1, table="approved"), SOURCES["crim"]) == "crim_adopted"
    assert (
        stratum_of(known(1, title="SYNTHETIC Rules 790 and 791"), SOURCES["crim"])
        == "expungement_rule"
    )
    assert stratum_of(known(1, source="gone"), None) is None
    plan = build_plan(pool(), SOURCES, seed=7)
    assert not any(c.source_id == "appellate" for sp in plan.splits.values() for c in sp.items)


def test_a_short_pool_is_reported_and_leaves_neither_split_empty():
    items = [known(n) for n in range(5)] + [known(n, source="civil") for n in range(3)]
    plan = build_plan(items, SOURCES, seed=7)
    for sp in plan.splits.values():
        assert sp.count(*POSITIVE_STRATA) >= 1 and sp.count("negative") >= 1
    assert "test positives: wanted 15, found 3" in plan.shortfalls
    assert "dev negatives: wanted 7, found 1" in plan.shortfalls


def write_records(tmp_path):
    store = RecordStore(tmp_path / "data")
    for k in pool():
        store.write(k.item)
    return tmp_path / "data"


def test_cli_writes_a_plan_for_review_and_fetches_nothing(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("eval-sample must not build a fetcher")

    monkeypatch.setattr("spidercrawler.cli.Fetcher", no_network)
    data, out = write_records(tmp_path), tmp_path / "plan.json"
    base = [
        "eval-sample",
        "--config",
        str(SOURCES_YAML),
        "--data-dir",
        str(data),
        "--out",
        str(out),
    ]
    result = CliRunner().invoke(app, [*base, "--seed", "7"])
    assert result.exit_code == 0, result.output
    assert "Nothing was fetched" in result.output
    assert "dev: 17 items" in result.output and "test: 25 items" in result.output
    plan = load_plan(out)
    assert plan.seed == 7 and len(plan.item_ids("test")) == 25
    assert not (tmp_path / "data" / "documents").exists()

    first = out.read_text()
    assert CliRunner().invoke(app, [*base, "--seed", "7"]).exit_code == 0  # same plan: no-op
    refused = CliRunner().invoke(app, [*base, "--seed", "8"])
    assert refused.exit_code == 2 and out.read_text() == first
    assert CliRunner().invoke(app, [*base, "--seed", "8", "--force"]).exit_code == 0
    assert load_plan(out).seed == 8


def test_cli_needs_known_items(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "eval-sample",
            "--seed",
            "1",
            "--config",
            str(SOURCES_YAML),
            "--data-dir",
            str(tmp_path),
            "--out",
            str(tmp_path / "p.json"),
        ],
    )
    assert result.exit_code == 2 and not (tmp_path / "p.json").exists()


def test_inject_from_plan_makes_each_listed_item_once(tmp_path):
    from test_eval_label import make_item

    from spidercrawler.eval_data import load_items
    from spidercrawler.eval_sample import dumps

    plan = build_plan(pool(), SOURCES, seed=7)
    plan_path, root = tmp_path / "plan.json", tmp_path / "items"
    plan_path.write_text(dumps(plan))
    for split, sp in plan.splits.items():
        for slot in sp.injections:
            make_item(root, slot.base_item_id, split)
    args = ["eval-inject", "--plan", str(plan_path), "--eval-dir", str(root)]
    assert CliRunner().invoke(app, args).exit_code == 0
    made = {s: [i.gold for i in load_items(root, s) if i.gold.synthetic] for s in ("dev", "test")}
    assert (len(made["dev"]), len(made["test"])) == (3, 5)
    again = CliRunner().invoke(app, args)  # nothing left to make, nothing overwritten
    assert again.exit_code == 0 and "wrote" not in again.output

    assert CliRunner().invoke(app, ["eval-inject", "--eval-dir", str(root)]).exit_code == 2
    both = CliRunner().invoke(app, ["eval-collect", "item-x", "--plan", str(plan_path)])
    assert both.exit_code == 2 and "not both" in both.output
