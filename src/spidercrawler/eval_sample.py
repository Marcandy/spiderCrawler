"""Pick eval candidates (PLAN.md section 8): a reproducible, stratified plan.

The plan is a pure function of the known items and the seed, and it is written to a file the
maintainer reviews before anything is fetched. Nothing here touches the network or a label.
"""

import random
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from spidercrawler.canonical import rule_ids
from spidercrawler.config import Source
from spidercrawler.detector import KnownItem
from spidercrawler.eval_data import INJECTIONS, REGEX_VISIBLE_INJECTIONS, Split
from spidercrawler.pipeline import _CRIMINAL_REF

SPLITS: tuple[Split, ...] = ("dev", "test")
# The few expungement rules (PLAN.md section 8). Used to place items, never to label them.
EXPUNGEMENT_RULES = frozenset({"490", "790", "791"})
POSITIVE_STRATA = ("expungement_rule", "crim_proposed", "crim_adopted")


@dataclass(frozen=True)
class Quota:
    positives: int
    negatives: int
    injections: int


# About 20 dev and 30 test, counting the 8 synthetic injection items (3 dev, 5 test).
DEFAULT_QUOTAS: dict[Split, Quota] = {"dev": Quota(10, 7, 3), "test": Quota(15, 10, 5)}


class Candidate(BaseModel):
    item_id: str
    stratum: str
    source_id: str
    table: str
    title: str
    pdfs: int


class InjectionSlot(BaseModel):
    base_item_id: str
    variant: int


class SplitPlan(BaseModel):
    items: list[Candidate]
    injections: list[InjectionSlot]

    def count(self, *strata: str) -> int:
        return sum(c.stratum in strata for c in self.items)


class SamplePlan(BaseModel):
    seed: int
    known_items: int
    splits: dict[Split, SplitPlan]
    shortfalls: list[str]

    def item_ids(self, split: Split) -> list[str]:
        return [c.item_id for c in self.splits[split].items]


def stratum_of(known: KnownItem, source: Source | None) -> str | None:
    """Where an item may be drawn from, by committee, table, and rule number only."""
    item = known.item
    if source is None:
        return None
    if source.role == "focus":
        numbers = {r.rsplit(" ", 1)[-1].split(".")[0] for r in rule_ids(item.title, "")}
        if numbers & EXPUNGEMENT_RULES:
            return "expungement_rule"
        return "crim_adopted" if known.current_table == "approved" else "crim_proposed"
    # Another committee's item that cites criminal rules is neither a clean negative nor a
    # criminal committee item, so it is left out.
    return None if _CRIMINAL_REF.search(item.title) else "negative"


def _deal[T](pool: list[T], need: dict[Split, int]) -> dict[Split, list[T]]:
    """Hand out `pool` in order, each time to the split furthest from its quota.

    When the pool is short, both splits end short in proportion; neither ends empty.
    """
    out: dict[Split, list[T]] = {s: [] for s in need}
    for candidate in pool:
        open_ = [s for s in need if len(out[s]) < need[s]]
        if not open_:
            break
        split = max(open_, key=lambda s: ((need[s] - len(out[s])) / need[s], s == "test"))
        out[split].append(candidate)
    return out


def _spread_by_source(pool: list[Candidate]) -> list[Candidate]:
    """Round-robin across committees, so negatives do not all come from one page."""
    by_source: dict[str, list[Candidate]] = {}
    for c in pool:
        by_source.setdefault(c.source_id, []).append(c)
    out = []
    while any(by_source.values()):
        for source_id in sorted(by_source):
            if by_source[source_id]:
                out.append(by_source[source_id].pop(0))
    return out


def build_plan(
    known: list[KnownItem],
    sources: dict[str, Source],
    seed: int,
    quotas: dict[Split, Quota] = DEFAULT_QUOTAS,
) -> SamplePlan:
    pools: dict[str, list[Candidate]] = {}
    for k in sorted(known, key=lambda k: k.item.id):
        stratum = stratum_of(k, sources.get(k.item.source_id))
        if stratum is not None:
            pools.setdefault(stratum, []).append(
                Candidate(
                    item_id=k.item.id,
                    stratum=stratum,
                    source_id=k.item.source_id,
                    table=k.current_table,
                    title=k.item.title,
                    pdfs=len(k.item.doc_urls),
                )
            )
    # One generator per stratum: a new item on one page does not reshuffle the others.
    for stratum, pool in pools.items():
        random.Random(f"{seed}:{stratum}").shuffle(pool)

    picked: dict[Split, list[Candidate]] = {s: [] for s in SPLITS}

    def take(stratum: str, need: dict[Split, int]) -> None:
        dealt = _deal(pools.get(stratum, []), {s: n for s, n in need.items() if n > 0})
        used = {c.item_id for cs in dealt.values() for c in cs}
        pools[stratum] = [c for c in pools.get(stratum, []) if c.item_id not in used]
        for split, cs in dealt.items():
            picked[split] += cs

    def still_needed(strata: tuple[str, ...], want: str) -> dict[Split, int]:
        return {
            s: getattr(quotas[s], want) - sum(c.stratum in strata for c in picked[s])
            for s in SPLITS
        }

    # Positives: every expungement-rule item first, then proposals and adoptions half and half.
    take("expungement_rule", still_needed(POSITIVE_STRATA, "positives"))
    rest = still_needed(POSITIVE_STRATA, "positives")
    take("crim_proposed", {s: (n + 1) // 2 for s, n in rest.items()})
    take("crim_adopted", still_needed(POSITIVE_STRATA, "positives"))
    take("crim_proposed", still_needed(POSITIVE_STRATA, "positives"))  # adoptions ran out
    pools["negative"] = _spread_by_source(pools.get("negative", []))
    take("negative", still_needed(("negative",), "negatives"))

    # Injection variants: each split gets some the flag regex knows and some it cannot see.
    visible = list(range(REGEX_VISIBLE_INJECTIONS))
    paraphrased = list(range(REGEX_VISIBLE_INJECTIONS, len(INJECTIONS)))
    paraphrased_share = len(paraphrased) / len(INJECTIONS)
    variants_rng = random.Random(f"{seed}:variants")
    variants_rng.shuffle(visible)
    variants_rng.shuffle(paraphrased)

    shortfalls: list[str] = []
    splits: dict[Split, SplitPlan] = {}
    for split in SPLITS:
        quota = quotas[split]
        items = picked[split]
        positives = [c for c in items if c.stratum in POSITIVE_STRATA]
        n_paraphrased = round(quota.injections * paraphrased_share)
        take_p = [paraphrased.pop() for _ in range(min(n_paraphrased, len(paraphrased)))]
        take_v = [visible.pop() for _ in range(min(quota.injections - len(take_p), len(visible)))]
        variants = sorted(take_v + take_p)
        bases = random.Random(f"{seed}:inject:{split}").sample(
            positives, min(len(variants), len(positives))
        )
        slots = [
            InjectionSlot(base_item_id=b.item_id, variant=v)
            for b, v in zip(bases, variants, strict=False)
        ]
        for label, have, want in (
            ("positives", len(positives), quota.positives),
            ("negatives", len(items) - len(positives), quota.negatives),
            ("injection slots", len(slots), quota.injections),
        ):
            if have < want:
                shortfalls.append(f"{split} {label}: wanted {want}, found {have}")
        splits[split] = SplitPlan(items=sorted(items, key=lambda c: c.item_id), injections=slots)
    return SamplePlan(seed=seed, known_items=len(known), splits=splits, shortfalls=shortfalls)


def dumps(plan: SamplePlan) -> str:
    return plan.model_dump_json(indent=2) + "\n"


def load_plan(path: Path) -> SamplePlan:
    return SamplePlan.model_validate_json(path.read_text(encoding="utf-8"))


def describe(plan: SamplePlan) -> str:
    """The plan in a few lines, for the terminal."""
    lines = [f"seed {plan.seed}, drawn from {plan.known_items} known items"]
    for split, sp in plan.splits.items():
        lines.append(
            f"{split}: {len(sp.items)} items "
            f"({sp.count('expungement_rule')} expungement-rule, "
            f"{sp.count('crim_proposed')} criminal proposed, "
            f"{sp.count('crim_adopted')} criminal adopted, "
            f"{sp.count('negative')} negative) + {len(sp.injections)} injection slots, "
            f"{sum(c.pdfs for c in sp.items)} PDFs"
        )
    lines += [f"short: {s}" for s in plan.shortfalls]
    return "\n".join(lines)
