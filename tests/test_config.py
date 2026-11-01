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
