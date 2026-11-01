"""Load and validate sources.yaml."""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator

from spidercrawler import __version__

PLACEHOLDER_EMAIL = "CHANGE-ME@example.com"
CONTACT_EMAIL_ENV = "SPIDERCRAWLER_CONTACT_EMAIL"
DEFAULT_SOURCES_PATH = Path("sources.yaml")


class Source(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str
    slug: str
    role: Literal["focus", "context"]


class Limits(BaseModel):
    min_delay_seconds: float = Field(default=5, ge=5)
    max_requests_per_run: int = Field(default=30, ge=1, le=30)


class Config(BaseModel):
    base_url: HttpUrl
    limits: Limits = Limits()
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
        return self.contact_email != PLACEHOLDER_EMAIL and "@" in self.contact_email

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
