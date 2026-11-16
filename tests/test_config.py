from pathlib import Path

import pytest
from pydantic import ValidationError

from spidercrawler.config import CONTACT_EMAIL_ENV, Config, load_config

REPO_SOURCES = Path(__file__).parent.parent / "sources.yaml"


def test_repo_sources_yaml_is_valid():
    config = load_config(REPO_SOURCES)
    assert len(config.sources) == 8
    assert [s.id for s in config.sources if s.role == "focus"] == ["crim"]
    url = config.source_url(config.sources[0])
    assert url.startswith(
        "https://www.pacourts.us/courts/supreme-court/committees/rules-committees/"
    )
    assert url.endswith("/criminal-procedural-rules-committee")


def test_placeholder_email_blocks_live_runs(monkeypatch):
    monkeypatch.delenv(CONTACT_EMAIL_ENV, raising=False)
    assert not load_config(REPO_SOURCES).has_contact_email


def test_env_overrides_contact_email(monkeypatch):
    monkeypatch.setenv(CONTACT_EMAIL_ENV, "maintainer@example.org")
    config = load_config(REPO_SOURCES)
    assert config.has_contact_email
    assert "maintainer@example.org" in config.user_agent


def test_limits_cannot_be_loosened():
    base = {"base_url": "https://x.test/", "sources": []}
    with pytest.raises(ValidationError):
        Config.model_validate({**base, "limits": {"min_delay_seconds": 1}})
    with pytest.raises(ValidationError):
        Config.model_validate({**base, "limits": {"max_requests_per_run": 100}})


def test_duplicate_source_ids_rejected():
    src = {"id": "a", "name": "A", "slug": "a", "role": "focus"}
    with pytest.raises(ValidationError):
        Config.model_validate({"base_url": "https://x.test/", "sources": [src, src]})


@pytest.mark.parametrize("email", ["a@", "@b.org", "no-at-sign", "a@b", "a b@c.org"])
def test_malformed_contact_email_is_rejected(monkeypatch, email):
    monkeypatch.setenv(CONTACT_EMAIL_ENV, email)
    assert not load_config(REPO_SOURCES).has_contact_email


def test_contact_email_with_trailing_newline_is_stripped(monkeypatch):
    # Regression: "a@b.org\n" was accepted, made an illegal User-Agent header, and httpx
    # quoted that header (email included) in the error stored in the public run record.
    monkeypatch.setenv(CONTACT_EMAIL_ENV, "maintainer@example.org\n")
    config = load_config(REPO_SOURCES)
    assert config.has_contact_email
    assert config.user_agent.endswith("; maintainer@example.org)")


@pytest.mark.parametrize("email", ["a@b.org\n", "a@b.org\x00", "\u00e9@b.org", " a@b.org"])
def test_email_that_cannot_be_a_header_value_is_rejected(email):
    config = Config.model_validate(
        {"base_url": "https://x.test/", "sources": [], "contact_email": email}
    )
    assert not config.has_contact_email
