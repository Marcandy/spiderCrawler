"""Preflight checks: temp dirs and a fake environment, no network."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spidercrawler import doctor
from spidercrawler.cli import app
from spidercrawler.config import CONTACT_EMAIL_ENV
from spidercrawler.doctor import API_KEY_ENV, blocked, run_checks
from spidercrawler.records import RecordStore, Run

SOURCES = Path(__file__).parent.parent / "sources.yaml"
EMAIL = "secret.person@example.org"
KEY = "sk-ant-SECRET-VALUE-123"
READY = {CONTACT_EMAIL_ENV: EMAIL, API_KEY_ENV: KEY}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # load_config reads the real environment; keep the developer's own values out.
    monkeypatch.delenv(CONTACT_EMAIL_ENV, raising=False)
    monkeypatch.delenv(API_KEY_ENV, raising=False)


def add_run(data_dir, seed, minute=0):
    started = datetime(2030, 1, 1, 0, minute, tzinfo=UTC)
    RecordStore(data_dir).write(
        Run(
            id=Run.make_id(started),
            started_at=started,
            finished_at=started,
            seed=seed,
            sources_checked=8,
            items_found=0,
            documents_fetched=0,
            requests_made=9,
            status="ok",
        )
    )


def checks(tmp_path, monkeypatch, env=READY, **kw):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    kw.setdefault("config_path", SOURCES)
    kw.setdefault("data_dir", tmp_path / "data")
    kw.setdefault("repo", tmp_path)  # not a git repository: no data branch
    return {c.name: c for c in run_checks(env=env, **kw)}


def test_everything_ready(tmp_path, monkeypatch):
    add_run(tmp_path / "data", seed=True)
    result = checks(tmp_path, monkeypatch, live=True)
    assert [c.status for c in result.values()][:5] == ["ok"] * 5
    assert not blocked(list(result.values()))


def test_contact_email_missing_names_the_secret_and_where_to_add_it(tmp_path, monkeypatch):
    check = checks(tmp_path, monkeypatch, env={})["contact email"]
    assert (check.status, check.blocks_run) == ("missing", True)
    assert "SPIDERCRAWLER_CONTACT_EMAIL is not set" in check.detail
    assert "Settings → Secrets and variables → Actions → Secrets" in check.fix
    assert check.annotation().startswith("::error title=contact email::")


@pytest.mark.parametrize("value", ["CHANGE-ME@example.com", "not-an-email", "a b@c.org"])
def test_contact_email_placeholder_or_malformed_is_missing(tmp_path, monkeypatch, value):
    check = checks(tmp_path, monkeypatch, env={CONTACT_EMAIL_ENV: value})["contact email"]
    assert check.status == "missing" and check.blocks_run
    assert value not in check.line() + (check.annotation() or "")


def test_api_key_missing_is_a_warning_not_a_block(tmp_path, monkeypatch):
    add_run(tmp_path / "data", seed=True)
    result = checks(tmp_path, monkeypatch, env={CONTACT_EMAIL_ENV: EMAIL})
    check = result["API key"]
    assert (check.status, check.blocks_run) == ("warn", False)
    assert check.annotation().startswith("::warning title=API key::")
    assert not blocked(list(result.values()))


def test_sources_yaml_that_does_not_load(tmp_path, monkeypatch):
    missing = checks(tmp_path, monkeypatch, config_path=tmp_path / "nope.yaml")
    assert missing["sources.yaml"].status == "missing" and missing["sources.yaml"].blocks_run
    bad = tmp_path / "bad.yaml"
    bad.write_text("base_url: not a url\nsources: []\n")
    assert checks(tmp_path, monkeypatch, config_path=bad)["sources.yaml"].status == "missing"
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    assert checks(tmp_path, monkeypatch, config_path=empty)["sources.yaml"].status == "missing"


def test_prompt_file_missing_blocks(tmp_path, monkeypatch):
    check = checks(tmp_path, monkeypatch, prompt_path=tmp_path / "gone.md")["prompt file"]
    assert (check.status, check.blocks_run) == ("missing", True)


def test_records_without_a_seed_run_warn_and_block_only_a_live_run(tmp_path, monkeypatch):
    first = checks(tmp_path, monkeypatch)["records"]
    assert (first.status, first.blocks_run) == ("warn", False)
    assert "seed: true" in first.fix
    live = checks(tmp_path, monkeypatch, live=True)["records"]
    assert (live.status, live.blocks_run) == ("missing", True)

    add_run(tmp_path / "data", seed=False)
    assert "no seed run" in checks(tmp_path, monkeypatch)["records"].detail
    add_run(tmp_path / "data", seed=True, minute=1)
    assert checks(tmp_path, monkeypatch, live=True)["records"].status == "ok"


def test_records_on_a_data_branch_point_to_data_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_has_data_branch", lambda repo: True)
    check = checks(tmp_path, monkeypatch)["records"]
    assert check.status == "warn" and "make data-checkout" in check.fix


def test_data_branch_lookup_is_local_and_survives_a_non_repository(tmp_path):
    assert doctor._has_data_branch(tmp_path) is False
    assert doctor._has_data_branch(tmp_path / "does-not-exist") is False


def test_uvicorn_check(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None)
    check = checks(tmp_path, monkeypatch)["uvicorn"]
    assert check.status == "warn" and "make review" in check.fix
    assert check.annotation() is None  # not a CI concern
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: object())
    assert checks(tmp_path, monkeypatch)["uvicorn"].status == "ok"


def cli(tmp_path, *args):
    base = ["doctor", "--config", str(SOURCES), "--data-dir", str(tmp_path / "data")]
    return CliRunner().invoke(app, [*base, *args])


def test_cli_exits_nonzero_only_for_blocking_problems(tmp_path, monkeypatch):
    result = cli(tmp_path)
    assert result.exit_code == 1 and "missing contact email" in result.output
    monkeypatch.setenv(CONTACT_EMAIL_ENV, EMAIL)
    result = cli(tmp_path)  # warnings only: no API key, no seed run
    assert result.exit_code == 0 and "warn    API key" in result.output
    assert cli(tmp_path, "--live").exit_code == 1  # a live run needs the seed run


def test_cli_github_annotations(tmp_path):
    result = cli(tmp_path, "--github")
    assert "::error title=contact email::SPIDERCRAWLER_CONTACT_EMAIL is not set" in result.output
    assert "::warning title=API key::" in result.output
    assert "::" not in cli(tmp_path).output


@pytest.mark.parametrize("email", [EMAIL, EMAIL + " oops", "CHANGE-ME@example.com"])
@pytest.mark.parametrize("args", [(), ("--github",), ("--live", "--github")])
def test_no_secret_value_is_ever_printed(tmp_path, monkeypatch, email, args):
    monkeypatch.setenv(CONTACT_EMAIL_ENV, email)
    monkeypatch.setenv(API_KEY_ENV, KEY)
    result = cli(tmp_path, *args)
    assert "ok      API key: set" in result.output  # the checks ran and saw the values
    assert KEY not in result.output and "SECRET-VALUE" not in result.output
    assert "secret.person" not in result.output and "oops" not in result.output
