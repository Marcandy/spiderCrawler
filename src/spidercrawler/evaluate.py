"""Eval harness (PLAN.md section 8): extractor + verifier on the labeled set, with honest stats.

Offline by default: every model response comes from the response cache, and a missing entry
is an error rather than a silent API call. `live=True` calls the API and fills the cache.
Only labeled items are scored. Counts are reported with 95% Wilson intervals.
"""

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from spidercrawler.eval_data import EvalItem
from spidercrawler.extractor import (
    LLMClient,
    ResponseCache,
    build_extraction,
    build_user_message,
    call_model,
    load_prompt,
)
from spidercrawler.normalize import content_hash, for_matching
from spidercrawler.verifier import RowContext

# Claude Sonnet 5.5 list prices, USD per million tokens (input, output). Report only.
PRICES = {"claude-sonnet-5-5": (2.0, 10.0)}
_EXPUNGE = re.compile(r"\b(expunge\w*|expungement|seal(ed|ing)?|limited\s+access)\b", re.IGNORECASE)
_RULE_NUMBER = re.compile(r"\bRules?\s+\d+|Pa\.\s?Rs?\.\s?Crim\.\s?P\.\s?\d+")
FIELDS = ("rule_ids", "effective_date", "stage", "expungement_relevance", "is_substantive")


class MissingCacheError(Exception):
    pass


@dataclass
class ItemResult:
    id: str
    synthetic: bool
    regex_visible_injection: bool
    hard: bool
    correct: dict[str, bool]
    claims: int
    unsupported_claims: int
    verifier_status: str
    queue_status: str
    relevance: str | None
    flags: list[str]
    baseline_relevance: str
    baseline_substantive: bool
    gold_relevance: str
    gold_substantive: bool
    input_tokens: int
    output_tokens: int

    @property
    def all_correct(self) -> bool:
        return all(self.correct.values())

    @property
    def really_wrong(self) -> bool:
        return not self.all_correct or self.unsupported_claims > 0


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    denom = 1 + z * z / n
    return (max(0.0, (centre - spread) / denom), min(1.0, (centre + spread) / denom))


def keyword_baseline(text: str) -> tuple[str, bool]:
    """The baseline the LLM must beat: regexes for expungement words and changed text."""
    relevance = "yes" if _EXPUNGE.search(text) else "no"
    substantive = ("{+" in text or "[" in text) and bool(_RULE_NUMBER.search(text))
    return relevance, substantive


def evaluate_item(
    item: EvalItem,
    cache: ResponseCache,
    client: LLMClient | None,
    model: str,
    effort: str,
    max_tokens: int,
    regex_visible: set[str],
) -> ItemResult:
    gold, labels = item.gold, item.gold.labels
    assert labels is not None
    system, prompt_version = load_prompt()
    row = RowContext(gold.row.table, gold.row.title, gold.row.date_literal, gold.rule_prefix)
    key = ResponseCache.key(prompt_version, model, effort, content_hash(item.input_text))
    result = cache.get(key)
    if result is None:
        if client is None:
            raise MissingCacheError(gold.id)
        result = call_model(
            client, system, build_user_message(item.input_text, row), model, effort, max_tokens
        )
        cache.put(key, result)
    ext = build_extraction(
        gold.id,
        item.input_text,
        result,
        model=model,
        effort=effort,
        prompt_version=prompt_version,
        row=row,
        rule_prefix=gold.rule_prefix,
        document_flags=[],
    )
    out, canon = ext.output or {}, ext.canonical or {}
    correct = {
        "rule_ids": ext.output is not None
        and set(canon.get("rule_ids", [])) == set(labels.rule_ids),
        "effective_date": ext.output is not None
        and canon.get("effective_date")
        == (labels.effective_date.isoformat() if labels.effective_date else None),
        "stage": out.get("stage") == labels.stage,
        "expungement_relevance": out.get("expungement_relevance") == labels.expungement_relevance,
        "is_substantive": out.get("is_substantive") == labels.is_substantive,
    }
    source = for_matching(item.input_text)
    quotes = [c["evidence_quote"] for c in out.get("claims", [])]
    base_rel, base_sub = keyword_baseline(item.input_text)
    return ItemResult(
        id=gold.id,
        synthetic=gold.synthetic,
        regex_visible_injection=gold.injection in regex_visible,
        hard=labels.hard,
        correct=correct,
        claims=len(quotes),
        unsupported_claims=sum(for_matching(q) not in source for q in quotes),
        verifier_status=ext.verifier_status,
        queue_status=ext.queue_status,
        relevance=out.get("expungement_relevance"),
        flags=ext.flags,
        baseline_relevance=base_rel,
        baseline_substantive=base_sub,
        gold_relevance=labels.expungement_relevance,
        gold_substantive=labels.is_substantive,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


@dataclass
class Metric:
    k: int
    n: int
    low: float = field(init=False)
    high: float = field(init=False)

    def __post_init__(self) -> None:
        self.low, self.high = wilson(self.k, self.n)

    def __str__(self) -> str:
        if self.n == 0:
            return "n/a (0 items)"
        return f"{self.k}/{self.n} = {self.k / self.n:.0%} (95% CI {self.low:.0%}-{self.high:.0%})"


def compute_metrics(results: list[ItemResult]) -> dict[str, Metric]:
    real = [r for r in results if not r.synthetic]
    synth = [r for r in results if r.synthetic]
    m: dict[str, Metric] = {}
    for f in FIELDS:
        m[f"accuracy.{f}"] = Metric(sum(r.correct[f] for r in real), len(real))
    m["unsupported_claim_rate"] = Metric(
        sum(r.unsupported_claims for r in real), sum(r.claims for r in real)
    )
    blocked = [r for r in real if r.queue_status == "blocked"]
    m["block_precision"] = Metric(sum(r.really_wrong for r in blocked), len(blocked))
    fine = [r for r in real if not r.really_wrong]
    m["false_block_rate"] = Metric(sum(r.queue_status == "blocked" for r in fine), len(fine))
    answered = [r for r in real if r.relevance is not None]
    m["supported_but_wrong_rate"] = Metric(
        sum(r.unsupported_claims == 0 and not r.all_correct for r in answered), len(answered)
    )
    unsure = [r for r in real if r.relevance == "unsure"]
    hard = [r for r in real if r.hard]
    m["abstention.unsure_on_hard"] = Metric(sum(r.hard for r in unsure), len(unsure))
    m["abstention.hard_marked_unsure"] = Metric(
        sum(r.relevance == "unsure" for r in hard), len(hard)
    )
    m["injection.output_unaffected"] = Metric(sum(r.all_correct for r in synth), len(synth))
    m["injection.flag_fired.regex_visible"] = Metric(
        sum("suspicious_input" in r.flags for r in synth if r.regex_visible_injection),
        sum(r.regex_visible_injection for r in synth),
    )
    m["injection.flag_fired.paraphrased"] = Metric(
        sum("suspicious_input" in r.flags for r in synth if not r.regex_visible_injection),
        sum(not r.regex_visible_injection for r in synth),
    )
    m["baseline.relevance_accuracy"] = Metric(
        sum(r.baseline_relevance == r.gold_relevance for r in real), len(real)
    )
    m["baseline.substantive_accuracy"] = Metric(
        sum(r.baseline_substantive == r.gold_substantive for r in real), len(real)
    )
    return m


def run_eval(
    items: list[EvalItem],
    cache: ResponseCache,
    client: LLMClient | None,
    model: str,
    effort: str,
    max_tokens: int,
    regex_visible: set[str],
) -> tuple[list[ItemResult], list[str]]:
    """Returns (results, ids skipped because unlabeled). Raises MissingCacheError offline."""
    labeled = [i for i in items if i.gold.labels is not None]
    skipped = [i.gold.id for i in items if i.gold.labels is None]
    if client is None:
        _, version = load_prompt()
        missing = [
            i.gold.id
            for i in labeled
            if cache.get(ResponseCache.key(version, model, effort, content_hash(i.input_text)))
            is None
        ]
        if missing:
            raise MissingCacheError(", ".join(missing))
    results = [
        evaluate_item(i, cache, client, model, effort, max_tokens, regex_visible) for i in labeled
    ]
    return results, skipped


def render_report(
    split: str,
    results: list[ItemResult],
    metrics: dict[str, Metric],
    skipped: list[str],
    model: str,
    effort: str,
    test_runs: int,
) -> str:
    _, version = load_prompt()
    tokens_in = sum(r.input_tokens for r in results)
    tokens_out = sum(r.output_tokens for r in results)
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    cost = tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out
    lines = [
        f"# Eval report: {split} split",
        "",
        f"- Date: {datetime.now(UTC).date().isoformat()}",
        f"- Model: `{model}`, effort `{effort}`, prompt `{version}`",
        f"- Items scored: {len(results)} ({sum(r.synthetic for r in results)} synthetic); "
        f"unlabeled skipped: {len(skipped)}",
        f"- Tokens: {tokens_in} in / {tokens_out} out; estimated cost ${cost:.2f} at list price",
    ]
    if split == "test":
        lines.append(
            f"- Test split scored {test_runs} time(s). Numbers are only clean on the first."
        )
    lines += ["", "| Metric | Result |", "|---|---|"]
    lines += [f"| {name} | {metric} |" for name, metric in metrics.items()]
    wrong = [r for r in results if not r.synthetic and r.really_wrong][:5]
    if wrong:
        lines += ["", "## Failure examples", ""]
        for r in wrong:
            bad = [f for f, ok in r.correct.items() if not ok]
            lines.append(
                f"- `{r.id}`: wrong {bad or '-'}; unsupported claims {r.unsupported_claims}; "
                f"queue `{r.queue_status}`"
            )
    return "\n".join(lines) + "\n"


def record_test_run(reports_dir: Path) -> int:
    """Count how many times the test split has been scored. Shown in every test report."""
    path = reports_dir / "test-runs.json"
    runs = json.loads(path.read_text()) if path.exists() else []
    runs.append(datetime.now(UTC).isoformat())
    reports_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(runs, indent=2) + "\n")
    return len(runs)


def results_json(results: list[ItemResult]) -> str:
    return json.dumps([asdict(r) for r in results], indent=2, sort_keys=True)
