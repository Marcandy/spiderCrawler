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
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Source(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str
    slug: str
    role: Literal["focus", "context"]
    rule_prefix: str = "Pa.R.Crim.P."  # citation set a bare "Rule 632" belongs to


class Limits(BaseModel):
    min_delay_seconds: float = Field(default=5, ge=5)
    max_requests_per_run: int = Field(default=30, ge=1, le=30)


class Guards(BaseModel):
    """Volume guards (PLAN.md section 6). Tripping one marks the source broken; nothing is
    recorded until the maintainer accepts the page with `run --seed`."""

    max_new_items: int = Field(default=10, ge=1)
    max_new_ratio: float = Field(default=0.25, gt=0, le=1)
    min_rows_ratio: float = Field(default=0.5, gt=0, le=1)


class Config(BaseModel):
    base_url: HttpUrl
    limits: Limits = Limits()
    guards: Guards = Guards()
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
        return self.contact_email != PLACEHOLDER_EMAIL and bool(_EMAIL.match(self.contact_email))

    @property
    def user_agent(self) -> str:
        return (
            f"spiderCrawler/{__version__} "
            f"(+https://github.com/Marcandy/spiderCrawler; {self.contact_email})"
        )


def load_config(path: Path = DEFAULT_SOURCES_PATH) -> Config:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if email := os.environ.get(CONTACT_EMAIL_ENV):
        data["contact_email"] = email
    return Config.model_validate(data)
