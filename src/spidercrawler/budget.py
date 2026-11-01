"""Per-run LLM budget that fails closed (PLAN.md section 6, runaway cost).

A call is only started if it fits: under the extraction cap, and the conservative estimate
of its input plus its full output allowance stays within the token budget. When the budget
is hit, extraction stops for the run and the remaining items wait for the next one.
"""

from dataclasses import dataclass, field

from spidercrawler.config import Limits

CHARS_PER_TOKEN = 3  # conservative for English legal text (real ratio is closer to 4)
PROMPT_OVERHEAD_TOKENS = 2_000  # system prompt and schema


def estimate_input_tokens(source_text: str) -> int:
    return PROMPT_OVERHEAD_TOKENS + len(source_text) // CHARS_PER_TOKEN + 1


@dataclass
class Budget:
    max_extractions: int
    max_tokens: int
    max_output_tokens: int
    extractions: int = 0
    tokens_used: int = 0
    stopped_reason: str | None = field(default=None)

    @classmethod
    def from_limits(cls, limits: Limits) -> "Budget":
        return cls(
            limits.max_extractions_per_run, limits.max_tokens_per_run, limits.max_output_tokens
        )

    def try_start(self, source_text: str) -> bool:
        """True if one more extraction fits. Once refused, every later call is refused too."""
        if self.stopped_reason:
            return False
        if self.extractions >= self.max_extractions:
            self.stopped_reason = f"extraction cap of {self.max_extractions} reached"
            return False
        worst_case = estimate_input_tokens(source_text) + self.max_output_tokens
        if self.tokens_used + worst_case > self.max_tokens:
            self.stopped_reason = (
                f"token budget: {self.tokens_used} used + up to {worst_case} for the next call "
                f"> {self.max_tokens}"
            )
            return False
        self.extractions += 1
        return True

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self.tokens_used += input_tokens + output_tokens
