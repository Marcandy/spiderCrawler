"""Offline preflight: what is missing before a run, and the one-line fix for each.

Reads the environment, the config, and the records on disk. Makes no request. Reports that a
secret is set or not; never its value.
"""

import importlib.util
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from spidercrawler.config import CONTACT_EMAIL_ENV, PLACEHOLDER_EMAIL, load_config
from spidercrawler.extractor import PROMPT_PATH
from spidercrawler.records import RecordStore, Run

API_KEY_ENV = "ANTHROPIC_API_KEY"
SECRETS_PAGE = "Settings → Secrets and variables → Actions → Secrets"


@dataclass(frozen=True)
class Check:
    name: str
    status: Literal["ok", "missing", "warn"]
    detail: str
    fix: str = ""
    blocks_run: bool = False  # a "missing" check that stops a run from starting
    in_ci: bool = True  # worth an annotation in a workflow run

    def line(self) -> str:
        text = f"{self.status:<8}{self.name}: {self.detail}"
        return f"{text}\n        fix: {self.fix}" if self.fix else text

    def annotation(self) -> str | None:
        """A GitHub Actions `::error::` or `::warning::` line, or None when nothing is wrong."""
        if self.status == "ok" or not self.in_ci:
            return None
        level = "error" if self.blocks_run else "warning"
        return f"::{level} title={self.name}::{self.detail}. Fix: {self.fix}"


def _config_check(config_path: Path) -> Check:
    try:
        config = load_config(config_path)
    except (OSError, ValidationError, yaml.YAMLError, AttributeError, TypeError) as exc:
        # The exception type only: a validation message can quote the value it rejects.
        return Check(
            "sources.yaml",
            "missing",
            f"{config_path} does not load ({type(exc).__name__})",
            "restore it from the repository, or pass --config",
            blocks_run=True,
        )
    return Check("sources.yaml", "ok", f"{len(config.sources)} sources")


def _email_check(config_path: Path, env: Mapping[str, str]) -> Check:
    name = "contact email"
    fix = (
        f"add the repository secret {CONTACT_EMAIL_ENV} ({SECRETS_PAGE}); "
        f"locally, export {CONTACT_EMAIL_ENV}"
    )
    try:
        config = load_config(config_path)
    except Exception:
        config = None
    if config is not None and config.has_contact_email:
        return Check(name, "ok", "set")
    in_env = bool(env.get(CONTACT_EMAIL_ENV, "").strip())
    if not in_env and (config is None or config.contact_email == PLACEHOLDER_EMAIL):
        detail = f"{CONTACT_EMAIL_ENV} is not set"
    else:
        detail = "the contact email is set but is not a plain email address"
    return Check(name, "missing", detail, fix, blocks_run=True)


def _api_key_check(env: Mapping[str, str]) -> Check:
    if env.get(API_KEY_ENV):
        return Check("API key", "ok", "set")
    return Check(
        "API key",
        "warn",
        f"{API_KEY_ENV} is not set; runs record 'extraction skipped'",
        f"add the repository secret {API_KEY_ENV} ({SECRETS_PAGE})",
    )


def _prompt_check(prompt_path: Path) -> Check:
    if prompt_path.is_file():
        return Check("prompt file", "ok", prompt_path.name)
    return Check(
        "prompt file",
        "missing",
        f"{prompt_path} does not exist",
        "restore prompts/ from the repository",
        blocks_run=True,
    )


def _has_data_branch(repo: Path) -> bool:
    for ref in ("refs/heads/data", "refs/remotes/origin/data"):
        try:
            done = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode == 0:
            return True
    return False


def _records_check(data_dir: Path, repo: Path, live: bool) -> Check:
    name = "records"
    runs = list(RecordStore(data_dir).iter(Run))
    if any(r.seed for r in runs):
        return Check(name, "ok", f"{len(runs)} runs in {data_dir}, with a seed run")
    seed_fix = (
        "dispatch the 'Weekly run' workflow with seed: true; locally, `spidercrawler run --seed`"
    )
    if runs:
        detail = f"{len(runs)} runs in {data_dir}, but no seed run"
        fix = seed_fix
    elif _has_data_branch(repo):
        detail = f"no records in {data_dir}, but a data branch exists"
        fix = "`make data-checkout`, then pass --data-dir data-branch/data"
    else:
        detail = f"no records in {data_dir} and no data branch"
        fix = seed_fix
    if live:
        # Without a seed run every existing row looks new. Large pages trip the flood guard;
        # a page with ten rows or fewer would be recorded as new items for good.
        return Check(name, "missing", detail + "; a live run needs one", fix, blocks_run=True)
    return Check(name, "warn", detail, fix)


def _uvicorn_check() -> Check:
    if importlib.util.find_spec("uvicorn") is not None:
        return Check("uvicorn", "ok", "importable", in_ci=False)
    return Check(
        "uvicorn",
        "warn",
        "not installed; `spidercrawler review` needs it",
        "use `make review`, which pulls it in",
        in_ci=False,
    )


def run_checks(
    config_path: Path,
    data_dir: Path,
    env: Mapping[str, str] | None = None,
    prompt_path: Path = PROMPT_PATH,
    repo: Path = Path("."),
    live: bool = False,
) -> list[Check]:
    """Every check, in the order a first-time setup fixes them. `live`: for a non-seed run."""
    env = os.environ if env is None else env
    return [
        _config_check(config_path),
        _email_check(config_path, env),
        _api_key_check(env),
        _prompt_check(prompt_path),
        _records_check(data_dir, repo, live),
        _uvicorn_check(),
    ]


def blocked(checks: list[Check]) -> bool:
    return any(c.status == "missing" and c.blocks_run for c in checks)
