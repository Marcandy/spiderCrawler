"""What the extractor returns (PLAN.md section 5).

The LLM outputs literal strings as written in the source. Canonical values are added
afterwards by `canonical.py`, never by the LLM.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"

ClaimField = Literal[
    "summary", "rule_ids", "effective_date", "affected_parties", "expungement_relevance"
]


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: ClaimField
    value: str = Field(description="The claim, restating one field value.")
    evidence_quote: str = Field(description="Exact text copied from the source.")


class ChangeSummary(BaseModel):
    """The LLM's output. Free text is never accepted outside this schema."""

    model_config = ConfigDict(extra="forbid")

    is_substantive: bool = Field(description="False for typo fixes, renumbering only, etc.")
    stage: Literal["proposed", "adopted", "other"]
    summary: str = Field(description="Plain language, at most 60 words, no advice.")
    rule_ids: list[str] = Field(description='Literal, as written, e.g. "Rule 632".')
    effective_date_text: str | None = Field(
        description='Literal, as written, e.g. "April 1, 2027" or "30 days after publication".'
    )
    affected_parties: list[str]
    expungement_relevance: Literal["yes", "no", "unsure"]
    claims: list[Claim] = Field(description="One or more per non-empty field.")
    confidence: Literal["low", "medium", "high"]
    abstain_reason: str | None = Field(description='Required when relevance is "unsure".')


class Canonical(BaseModel):
    """Derived by code from the literals. Used for eval scoring and cross-checks."""

    rule_ids: list[str]  # e.g. ["Pa.R.Crim.P. 632"]
    effective_date: date | None  # None when relative or unparseable
