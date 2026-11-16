"""Labeling tools (PLAN.md section 8): a terminal prompt, the test-split freeze, agreement.

The prompt shows the source text and the labeling guide, and nothing else: no model output,
no cached response, no default, no suggested value. A person types every label. Nothing here
reads `eval/cache` or `eval/reports`.
"""

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel

from spidercrawler.canonical import rule_ids
from spidercrawler.eval_data import EvalItem, GoldItem, Labels, Split, load_items, status

LABEL_FIELDS = (
    "is_substantive",
    "stage",
    "rule_ids",
    "effective_date",
    "expungement_relevance",
    "hard",
)
SECOND_GOLD = "gold.second.json"
MANIFEST = "test.frozen.json"
FREEZE_LOG = "freeze-log.jsonl"
_GUIDE_HEADING = re.compile(r"^### `(\w+)`.*$", re.MULTILINE)
_TRUE, _FALSE = {"true", "t", "yes", "y"}, {"false", "f", "no", "n"}
_CHOICES = {
    "stage": ("proposed", "adopted", "other"),
    "expungement_relevance": ("yes", "no", "unsure"),
}
HINTS = {
    "is_substantive": "true / false",
    "stage": "proposed / adopted / other",
    "rule_ids": "canonical ids separated by commas, or none",
    "effective_date": "YYYY-MM-DD, or null",
    "expungement_relevance": "yes / no / unsure",
    "hard": "true / false",
}


# -- the guide and the values a person types -------------------------------------


def guide_sections(guide: str) -> dict[str, str]:
    """The guide's definition of each field: its `### \\`field\\`` section, heading included."""
    sections: dict[str, str] = {}
    headings = list(_GUIDE_HEADING.finditer(guide))
    for i, match in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(guide)
        body = guide[match.start() : end]
        sections[match.group(1)] = body.split("\n## ")[0].strip()
    missing = [f for f in LABEL_FIELDS if f not in sections]
    if missing:
        raise ValueError(f"labeling guide has no section for: {', '.join(missing)}")
    return sections


def parse_value(name: str, raw: str) -> object:
    """Turn typed text into a field value. Empty input is never a value."""
    text = raw.strip()
    if not text:
        raise ValueError("type a value; there is no default")
    low = text.lower()
    if name in ("is_substantive", "hard"):
        if low in _TRUE or low in _FALSE:
            return low in _TRUE
        raise ValueError("type true or false")
    if name in _CHOICES:
        if low in _CHOICES[name]:
            return low
        raise ValueError(f"type one of: {', '.join(_CHOICES[name])}")
    if name == "effective_date":
        if low in ("null", "none"):
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise ValueError("type a date as YYYY-MM-DD, or null") from None
    if name == "rule_ids":
        if low == "none":
            return []
        ids = [part.strip() for part in text.split(",") if part.strip()]
        return list(dict.fromkeys(ids))
    raise ValueError(f"unknown field {name}")


def non_canonical(ids: list[str]) -> list[str]:
    """Ids that `canonical.py` would write differently, so they could never match in scoring."""
    return [i for i in ids if rule_ids(i, "") != [i]]


# -- files ------------------------------------------------------------------------


def write_atomic(path: Path, text: str) -> None:
    """Replace `path` in one step: a crash leaves the old file or the new one, never half."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def find_item(root: Path, item_id: str) -> EvalItem:
    matches = [i for i in load_items(root) if i.gold.id == item_id]
    if len(matches) != 1:
        raise ValueError(f"{item_id}: not an exported eval item (found {len(matches)})")
    return matches[0]


def second_gold(item: EvalItem) -> GoldItem | None:
    path = item.path / SECOND_GOLD
    if not path.exists():
        return None
    return GoldItem.model_validate_json(path.read_text(encoding="utf-8"))


def next_unlabeled(root: Path, split: Split | None, second: bool) -> EvalItem | None:
    for item in load_items(root, split):
        gold = second_gold(item) if second else item.gold
        if gold is None or gold.labels is None:
            return item
    return None


def save_labels(item: EvalItem, labels: Labels, notes: str, second: bool) -> Path:
    gold = item.gold.model_copy(update={"labels": labels, "notes": notes})
    path = item.path / (SECOND_GOLD if second else "gold.json")
    write_atomic(path, gold.model_dump_json(indent=2) + "\n")
    return path


# -- the prompt -------------------------------------------------------------------


def label_interactively(
    item: EvalItem,
    guide: dict[str, str],
    ask: Callable[[str], str],
    say: Callable[[str], None],
    page: Callable[[str], None],
) -> tuple[Labels, str] | None:
    """Ask for every field. Returns None if the labeler does not confirm."""
    gold = item.gold
    say(f"Item {gold.id} ({gold.split} split)")
    if gold.synthetic:
        say(
            "SYNTHETIC injection item. Label the original document; the inserted text below "
            f"changes nothing:\n  {gold.injection}"
        )
    say(
        f"Committee page: table {gold.row.table} | date column {gold.row.date_literal}\n"
        f"Title: {gold.row.title}"
    )
    page(item.input_text)
    say("At any prompt: ? shows the guide again, ! shows the source text again.")

    values: dict[str, object] = {}
    for name in LABEL_FIELDS:
        say("\n" + guide[name])
        while True:
            raw = ask(f"{name} ({HINTS[name]})")
            if raw.strip() == "?":
                say(guide[name])
                continue
            if raw.strip() == "!":
                page(item.input_text)
                continue
            try:
                value = parse_value(name, raw)
            except ValueError as exc:
                say(f"  {exc}")
                continue
            if name == "rule_ids" and (odd := non_canonical(value)):  # type: ignore[arg-type]
                say(f"  not in the guide's canonical form: {', '.join(odd)}")
                if ask("  keep as typed? (yes / no)").strip().lower() not in _TRUE:
                    continue
            values[name] = value
            break
    labels = Labels.model_validate(values)
    notes = ask("notes (free text; Enter for none)").strip()
    say("\nYou typed:")
    for name in LABEL_FIELDS:
        say(f"  {name}: {json.dumps(labels.model_dump(mode='json')[name])}")
    while (answer := ask("save? (yes / no)").strip().lower()) not in _TRUE | _FALSE:
        say("  type yes or no")
    return (labels, notes) if answer in _TRUE else None


# -- freezing the test split ------------------------------------------------------


class FrozenItem(BaseModel):
    input_sha256: str
    gold_sha256: str


class FreezeManifest(BaseModel):
    split: Split = "test"
    frozen_at: datetime
    items: dict[str, FrozenItem]


@dataclass(frozen=True)
class FreezeState:
    manifest: FreezeManifest | None
    changed: list[str] = field(default_factory=list)  # labels or text differ from the manifest
    missing: list[str] = field(default_factory=list)  # frozen, but gone from disk
    added: list[str] = field(default_factory=list)  # on disk, but not frozen

    @property
    def frozen(self) -> bool:
        return self.manifest is not None

    @property
    def intact(self) -> bool:
        return self.frozen and not (self.changed or self.missing or self.added)

    def problems(self) -> list[str]:
        return [
            f"{label}: {', '.join(ids)}"
            for label, ids in (
                ("changed since the freeze", self.changed),
                ("missing", self.missing),
                ("added after the freeze", self.added),
            )
            if ids
        ]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprint(item: EvalItem) -> FrozenItem:
    return FrozenItem(
        input_sha256=_sha(item.path / "input.txt"), gold_sha256=_sha(item.path / "gold.json")
    )


def freeze_state(root: Path) -> FreezeState:
    path = root / MANIFEST
    if not path.exists():
        return FreezeState(None)
    manifest = FreezeManifest.model_validate_json(path.read_text(encoding="utf-8"))
    on_disk = {i.gold.id: _fingerprint(i) for i in load_items(root, manifest.split)}
    return FreezeState(
        manifest,
        changed=sorted(i for i, f in manifest.items.items() if i in on_disk and on_disk[i] != f),
        missing=sorted(set(manifest.items) - set(on_disk)),
        added=sorted(set(on_disk) - set(manifest.items)),
    )


class FreezeError(Exception):
    pass


def log_event(root: Path, action: str, detail: str, now: datetime | None = None) -> None:
    """Append one line to the freeze log. Every freeze, unfreeze, and refreeze is on record."""
    entry = {"at": (now or datetime.now(UTC)).isoformat(), "action": action, "detail": detail}
    root.mkdir(parents=True, exist_ok=True)
    with (root / FREEZE_LOG).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_log(root: Path) -> list[dict]:
    path = root / FREEZE_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def freeze(root: Path, refreeze: bool = False, now: datetime | None = None) -> FreezeManifest:
    """Write the hash manifest for the test split. Refuses an unlabeled or already frozen set."""
    items = load_items(root, "test")
    if not items:
        raise FreezeError("no test items to freeze")
    unlabeled = [i.gold.id for i in items if i.gold.labels is None]
    if unlabeled:
        raise FreezeError(f"unlabeled test items: {', '.join(unlabeled)}")
    if (root / MANIFEST).exists() and not refreeze:
        raise FreezeError("the test split is already frozen; --refreeze replaces it and is logged")
    manifest = FreezeManifest(
        frozen_at=now or datetime.now(UTC), items={i.gold.id: _fingerprint(i) for i in items}
    )
    write_atomic(root / MANIFEST, manifest.model_dump_json(indent=2) + "\n")
    log_event(root, "refreeze" if refreeze else "freeze", f"{len(items)} test items", now)
    return manifest


def require_unfrozen(root: Path, split: str, what: str) -> None:
    """Guard for anything that would add to or change the test split."""
    if split == "test" and freeze_state(root).frozen:
        raise FreezeError(f"the test split is frozen; refusing to {what}")


def labeling_status(root: Path) -> dict[str, dict[str, int]]:
    """`eval_data.status` plus, per split, items with second labels and items held by the freeze."""
    out = status(root)
    state = freeze_state(root)
    held = set(state.manifest.items) - set(state.changed) if state.manifest else set()
    for split, counts in out.items():
        items = load_items(root, split)  # type: ignore[arg-type]
        counts["second_labeled"] = sum(
            (second := second_gold(i)) is not None and second.labels is not None for i in items
        )
        counts["frozen"] = sum(i.gold.id in held for i in items) if split == "test" else 0
    return out


# -- agreement between two labelers -----------------------------------------------

KAPPA_FIELDS = ("is_substantive", "stage", "expungement_relevance", "hard")


def cohen_kappa(a: list[object], b: list[object]) -> float | None:
    """Agreement beyond chance. None when undefined: no items, or chance agreement of 1
    (both labelers used one and the same category throughout)."""
    n = len(a)
    if n == 0 or n != len(b):
        return None
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    count_a, count_b = Counter(a), Counter(b)
    expected = sum(count_a[c] * count_b[c] for c in count_a) / (n * n)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


@dataclass(frozen=True)
class FieldAgreement:
    name: str
    agree: int
    n: int
    kappa: float | None  # None where kappa does not apply or is undefined

    def row(self) -> str:
        if self.n == 0:
            return f"| {self.name} | n/a (0 items) | n/a |"
        kappa = "n/a" if self.kappa is None else f"{self.kappa:.2f}"
        return f"| {self.name} | {self.agree}/{self.n} = {self.agree / self.n:.0%} | {kappa} |"


def _comparable(labels: Labels, name: str) -> object:
    value = getattr(labels, name)
    return frozenset(value) if name == "rule_ids" else value  # order does not matter


def agreement(root: Path, split: Split | None = "test") -> list[FieldAgreement]:
    """Per-field agreement on items that both labelers labeled."""
    pairs = [
        (item.gold.labels, second.labels)
        for item in load_items(root, split)
        if item.gold.labels is not None
        and (second := second_gold(item)) is not None
        and second.labels is not None
    ]
    out = []
    for name in LABEL_FIELDS:
        first = [_comparable(a, name) for a, _ in pairs]
        other = [_comparable(b, name) for _, b in pairs]
        agree = sum(x == y for x, y in zip(first, other, strict=True))
        kappa = cohen_kappa(first, other) if name in KAPPA_FIELDS else None
        out.append(FieldAgreement(name, agree, len(pairs), kappa))
    return out


def render_agreement(rows: list[FieldAgreement]) -> str:
    n = rows[0].n if rows else 0
    lines = [
        f"Items labeled by both labelers: {n}.",
        "",
        "| Field | Agreement | Cohen's kappa |",
        "|---|---|---|",
        *[r.row() for r in rows],
        "",
        "Kappa is given for the fields with a fixed set of values. It is n/a for rule ids and "
        "dates, and where both labelers used a single value throughout.",
    ]
    return "\n".join(lines)


DOCS_START, DOCS_END = "<!-- agreement:start -->", "<!-- agreement:end -->"


def update_docs(docs: Path, table: str) -> None:
    """Put the agreement table between the markers in docs/EVAL.md."""
    text = docs.read_text(encoding="utf-8")
    head, start, rest = text.partition(DOCS_START)
    _, end, tail = rest.partition(DOCS_END)
    if not (start and end):
        raise ValueError(f"{docs} has no {DOCS_START} ... {DOCS_END} block")
    write_atomic(docs, f"{head}{start}\n{table}\n{end}{tail}")
