"""Load and validate sources.yaml."""

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator

from spidercrawler import __version__

PLACEHOLDER_EMAIL = "CHANGE-ME@example.com"
CONTACT_EMAIL_ENV = "SPIDERCRAWLER_CONTACT_EMAIL"
DEFAULT_SOURCES_PATH = Path("sources.yaml")
# Printable ASCII only, no spaces: the email goes into the User-Agent header. Anything else
# is an illegal header value, and httpx quotes the whole header in the error it raises.
_EMAIL = re.compile(r"[!-?A-~]+@[!-?A-~]+\.[!-?A-~]+")


class Source(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str
    slug: str
    role: Literal["focus", "context"]
    rule_prefix: str = "Pa.R.Crim.P."  # citation set a bare "Rule 632" belongs to


class Limits(BaseModel):
    min_delay_seconds: float = Field(default=5, ge=5)
    max_requests_per_run: int = Field(default=30, ge=1, le=30)
    max_extractions_per_run: int = Field(default=20, ge=0, le=20)
    max_tokens_per_run: int = Field(default=400_000, ge=0)
    # Thinking tokens count as output on current models; too low truncates the JSON.
    max_output_tokens: int = Field(default=8_000, ge=1_024)


class Guards(BaseModel):
    """Volume guards (PLAN.md section 6). Tripping one marks the source broken; nothing is
    recorded until the maintainer accepts the page with `run --seed`."""

    max_new_items: int = Field(default=10, ge=1)
    max_new_ratio: float = Field(default=0.25, gt=0, le=1)
    min_rows_ratio: float = Field(default=0.5, gt=0, le=1)


class Extractor(BaseModel):
    model: str = "claude-sonnet-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    prompt: Literal["v1", "v2"] = "v1"  # v2 adds retrieved context (docs/AGENTIC.md section 2)
    related_chunks: int = Field(default=3, ge=0, le=5)  # v2 only: chunks of related context


class Repair(BaseModel):
    """Verifier-feedback repair loop (docs/AGENTIC.md section 1). 0 turns it off."""

    max_repairs: int = Field(default=0, ge=0, le=2)  # hard cap: 2 repair calls per item


class Config(BaseModel):
    base_url: HttpUrl
    limits: Limits = Limits()
    guards: Guards = Guards()
    extractor: Extractor = Extractor()
    repair: Repair = Repair()
    contact_email: str = PLACEHOLDER_EMAIL
    sources: list[Source]

    @field_validator("sources")
    @classmethod
    def unique_ids(cls, sources: list[Source]) -> list[Source]:
        ids = [s.id for s in sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source ids must be unique")
        return sources

    def source_url(self, source: Source) -> str:
        return str(self.base_url).rstrip("/") + "/" + source.slug

    @property
    def has_contact_email(self) -> bool:
        email = self.contact_email
        return email != PLACEHOLDER_EMAIL and bool(_EMAIL.fullmatch(email))

    @property
    def user_agent(self) -> str:
        return (
            f"spiderCrawler/{__version__} "
            f"(+https://github.com/Marcandy/spiderCrawler; {self.contact_email})"
        )


def load_config(path: Path = DEFAULT_SOURCES_PATH) -> Config:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Stripped: a secret pasted or piped in often ends with a newline.
    if email := os.environ.get(CONTACT_EMAIL_ENV, "").strip():
        data["contact_email"] = email
    extractor = data.setdefault("extractor", {})
    if model := os.environ.get("SPIDERCRAWLER_MODEL"):
        extractor["model"] = model
    if effort := os.environ.get("SPIDERCRAWLER_EFFORT"):
        extractor["effort"] = effort
    return Config.model_validate(data)
