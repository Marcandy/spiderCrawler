"""An eval built for the review assistant (docs/EVAL.md, "Review assistant").

Planted errors: take an output that matches its gold labels, change one field in code (no
model writes the mutation), and ask whether the assistant flags that field. The same items
unchanged measure false alarms. The baseline asks the same question in one call with the
whole source in context and no tools. Everything here needs labeled dev items; until the
owner labels them, the report says pending.
"""

import json
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from spidercrawler.assistant import PROMPT_PATH as ASSIST_PROMPT
from spidercrawler.assistant import Bounds, Outcome, ReviewBrief, load_prompt, run_assistant
from spidercrawler.canonical import parse_date, rule_ids
from spidercrawler.config import Config
from spidercrawler.eval_data import EvalItem
from spidercrawler.evaluate import PRICES, Metric
from spidercrawler.extractor import LLMClient
from spidercrawler.normalize import content_hash, quote_in_source
from spidercrawler.records import Document, Extraction, Item, RecordStore, utcnow
from spidercrawler.tools import Toolbox, neutralize

_AUXILIARY = re.compile(r"\b(shall|will|would|may|must|is|are|does|can)\b")


def _shift_date(out: dict, source: str, rng: random.Random) -> dict | None:
    day = parse_date(out.get("effective_date_text"))
    if day is None:
        return None
    moved: date = day + timedelta(days=rng.choice([31, 61, 92]))
    return {**out, "effective_date_text": f"{moved:%B} {moved.day}, {moved.year}"}


def _drop_rule(out: dict, source: str, rng: random.Random) -> dict | None:
    ids = list(out.get("rule_ids") or [])
    if not ids:
        return None
    ids.pop(rng.randrange(len(ids)))
    return {**out, "rule_ids": ids}


def _add_cited_rule(out: dict, source: str, rng: random.Random) -> dict | None:
    listed = set(rule_ids(" ".join(out.get("rule_ids") or []), "Pa.R.Crim.P."))
    cited = [r for r in rule_ids(source, "Pa.R.Crim.P.") if r not in listed]
    if not cited:
        return None
    return {**out, "rule_ids": [*out.get("rule_ids", []), rng.choice(cited)]}


def _flip_stage(out: dict, source: str, rng: random.Random) -> dict | None:
    return {**out, "stage": "proposed" if out.get("stage") == "adopted" else "adopted"}


def _negate_summary(out: dict, source: str, rng: random.Random) -> dict | None:
    summary = out.get("summary") or ""
    if not _AUXILIARY.search(summary):
        return None
    return {**out, "summary": _AUXILIARY.sub(r"\1 not", summary, count=1)}


def _flip_relevance(out: dict, source: str, rng: random.Random) -> dict | None:
    flipped = {"yes": "no", "no": "yes", "unsure": "yes"}[out.get("expungement_relevance", "no")]
    return {**out, "expungement_relevance": flipped}


# name -> (field the assistant should flag, mutation)
MUTATIONS: dict[str, tuple[str, Callable[[dict, str, random.Random], dict | None]]] = {
    "shift_effective_date": ("effective_date_text", _shift_date),
    "drop_rule_id": ("rule_ids", _drop_rule),
    "add_cited_rule_id": ("rule_ids", _add_cited_rule),
    "flip_stage": ("stage", _flip_stage),
    "negate_summary": ("summary", _negate_summary),
    "flip_expungement_relevance": ("expungement_relevance", _flip_relevance),
}


@dataclass(frozen=True)
class Case:
    item_id: str
    output: dict
    mutation: str | None  # None: the unchanged, correct output
    field: str | None  # the field that was changed


def make_cases(item_id: str, output: dict, source: str, seed: int) -> list[Case]:
    """The unchanged output plus one case per mutation that applies. Same seed, same cases."""
    rng = random.Random(f"{seed}:{item_id}")
    cases = [Case(item_id, output, None, None)]
    for name, (field, mutate) in MUTATIONS.items():
        changed = mutate(json.loads(json.dumps(output)), source, rng)
        if changed is not None and changed != output:
            cases.append(Case(item_id, changed, name, field))
    return cases


def flags(outcome: Outcome, field: str | None = None) -> bool:
    """Did a complete brief raise a non-consistent finding (on `field`, if given)?"""
    if outcome.status != "complete" or outcome.brief is None:
        return False
    return any(
        f.kind != "consistent" and (field is None or f.field == field)
        for f in outcome.brief.findings
    )


def single_call_baseline(
    client: LLMClient, output: dict, source: str, *, model: str, effort: str, max_tokens: int
) -> Outcome:
    """The same question in one call: the whole source in context, no tools."""
    system, _ = load_prompt(ASSIST_PROMPT)
    user = (
        f"<source_document>\n{neutralize(source)}\n</source_document>\n\n"
        f"<extraction_under_review>\n{json.dumps(neutralize(output), indent=2)}\n"
        "</extraction_under_review>\n\nNo tools are available; the whole source is above."
    )
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort},
        output_format=ReviewBrief,
    )
    usage = getattr(response, "usage", None)
    out = Outcome(
        status="cut_short",
        steps=1,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )
    parsed = getattr(response, "parsed_output", None)
    if getattr(response, "stop_reason", None) != "end_turn" or parsed is None:
        out.cut_reason = f"model stopped: {getattr(response, 'stop_reason', None)}"
        return out
    out.status = "complete"
    out.brief = ReviewBrief.model_validate(
        parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
    )
    return out


def score(results: list[tuple[Case, Outcome, str]]) -> dict[str, Metric | str]:
    """(case, outcome, source text) triples -> the report's numbers."""
    mutated = [(c, o) for c, o, _ in results if c.mutation]
    clean = [(c, o) for c, o, _ in results if not c.mutation]
    outcomes = [o for _, o, _ in results]
    quotes = [
        quote_in_source(f.evidence_quote, src)
        for _, o, src in results
        if o.brief
        for f in o.brief.findings
        if f.evidence_quote
    ]
    n = len(outcomes) or 1
    return {
        "detection_rate": Metric(sum(flags(o, c.field) for c, o in mutated), len(mutated)),
        "false_alarm_rate": Metric(sum(flags(o) for _, o in clean), len(clean)),
        "bound_hit_rate": Metric(sum(o.status == "cut_short" for o in outcomes), len(outcomes)),
        "evidence_quotes_verified": Metric(sum(quotes), len(quotes)),
        "steps_per_item": f"{sum(o.steps for o in outcomes) / n:.2f}",
        "tool_calls_per_item": f"{sum(o.tool_calls for o in outcomes) / n:.2f}",
        "tokens_per_item": f"{sum(o.input_tokens + o.output_tokens for o in outcomes) / n:.0f}",
    }


def cost_per_item(results: list[tuple[Case, Outcome, str]], model: str) -> float:
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    total = sum(o.input_tokens * price_in + o.output_tokens * price_out for _, o, _ in results)
    return total / 1e6 / (len(results) or 1)


def injection_unaffected(pairs: list[tuple[Outcome, Outcome]]) -> Metric:
    """(original, injected) briefs: unaffected means the same (field, kind) findings."""

    def shape(o: Outcome) -> set[tuple[str, str]] | None:
        return {(f.field, f.kind) for f in o.brief.findings} if o.brief else None

    return Metric(sum(shape(a) == shape(b) is not None for a, b in pairs), len(pairs))


def render(
    agent: dict[str, Metric | str] | None,
    baseline: dict[str, Metric | str] | None = None,
    injection: Metric | None = None,
    note: str = "",
) -> str:
    lines = ["# Review assistant eval (dev split)", ""]
    if agent is None:
        lines.append(
            "_Pending: needs labeled dev items with a correct cached extraction "
            "(`spidercrawler eval-label`, then `make eval-live`)._"
        )
        return "\n".join(lines) + "\n"
    if note:
        lines += [f"- {note}", ""]
    lines += ["| Metric | Agent (tools) | Single call, no tools |", "|---|---|---|"]
    for name, value in agent.items():
        other = baseline.get(name, "-") if baseline else "-"
        lines.append(f"| {name} | {value} | {other} |")
    if injection is not None:
        lines += ["", f"Injection items with an unaffected brief: {injection}"]
    lines.append(
        "\nThe assistant is advisory. A high detection rate on planted errors does not mean it "
        "catches real ones, which are subtler."
    )
    return "\n".join(lines) + "\n"


def item_store(eval_item: EvalItem, root: Path) -> tuple[RecordStore, Item, list[Document]]:
    """A throwaway record store holding one eval item and its text as a document, so the
    assistant's tools read exactly the text the eval scores (injected text included)."""
    gold = eval_item.gold
    store = RecordStore(root / gold.id)
    item = Item(
        id=f"item-eval-{gold.id}",
        item_key=f"/eval/{gold.id}",
        source_id=gold.source_id,
        table=gold.row.table,  # type: ignore[arg-type]
        title=gold.row.title,
        row_date_literal=gold.row.date_literal,
        doc_urls=[f"eval://{gold.id}"],
        first_seen_run="run-eval",
        seeded=False,
    )
    doc = Document(
        id=f"doc-eval-{gold.id}",
        item_id=item.id,
        url=item.doc_urls[0],
        fetched_at=utcnow(),
        content_hash=content_hash(eval_item.input_text),
        text=eval_item.input_text,
        text_len=len(eval_item.input_text),
        markup_lost=False,
        no_text=False,
    )
    store.write(item)
    store.write(doc)
    return store, item, [doc]


def run_cases(
    eval_item: EvalItem,
    output: dict,
    client: LLMClient,
    config: Config,
    root: Path,
    seed: int,
    *,
    mutate: bool = True,
) -> tuple[list[tuple[Case, Outcome, str]], list[tuple[Case, Outcome, str]]]:
    """(agent results, baseline results) for one item's cases."""
    store, item, docs = item_store(eval_item, root)
    toolbox = Toolbox(store, config)
    model = config.assistant.model or config.extractor.model
    effort = config.assistant.effort
    bounds = Bounds(config.assistant.max_steps, config.assistant.max_tokens)
    cases = make_cases(eval_item.gold.id, output, eval_item.input_text, seed)
    agent, baseline = [], []
    for case in cases if mutate else cases[:1]:
        ext = Extraction.model_construct(id=f"ext-eval-{eval_item.gold.id}", output=case.output)
        outcome = run_assistant(
            client, toolbox, ext, item, docs, model=model, effort=effort, bounds=bounds
        )
        agent.append((case, outcome, eval_item.input_text))
        single = single_call_baseline(
            client,
            case.output,
            eval_item.input_text,
            model=model,
            effort=effort,
            max_tokens=bounds.max_output_tokens,
        )
        baseline.append((case, single, eval_item.input_text))
    return agent, baseline
