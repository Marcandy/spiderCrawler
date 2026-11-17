"""Gold links for the retrieval eval: "this adoption's proposal is that item".

A person types every link, as with every other label. The prompt shows the adopted item and
nothing ranked: no retriever output, no candidate, no default. Scoring then asks whether
`related_items` puts the labeled proposal in its top k (recall@k) and how high (MRR).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from spidercrawler.eval_label import write_atomic
from spidercrawler.evaluate import Metric
from spidercrawler.records import Item, utcnow


class LinkLabel(BaseModel):
    item_id: str
    proposal_item_id: str | None  # None: the labeler found no proposal among the records
    labeled_by: str
    labeled_at: datetime


def load_links(links_dir: Path) -> dict[str, LinkLabel]:
    paths = sorted(links_dir.glob("*.json")) if links_dir.is_dir() else []
    labels = [LinkLabel.model_validate_json(p.read_text(encoding="utf-8")) for p in paths]
    return {label.item_id: label for label in labels}


def save_link(links_dir: Path, label: LinkLabel) -> Path:
    links_dir.mkdir(parents=True, exist_ok=True)
    path = links_dir / f"{label.item_id}.json"
    write_atomic(path, label.model_dump_json(indent=2) + "\n")
    return path


def parse_answer(raw: str, item: Item, items: dict[str, Item]) -> str | None:
    """'none' -> None; otherwise the id of a known item other than `item`."""
    answer = raw.strip()
    if answer.lower() == "none":
        return None
    if answer == item.id or answer not in items:
        raise ValueError(f"{answer!r} is not the id of another recorded item")
    return answer


def label_link(
    item: Item,
    items: dict[str, Item],
    labeler: str,
    ask: Callable[[str], str],
    say: Callable[[str], None],
) -> LinkLabel | None:
    """Ask for one link. A blank answer skips the item and saves nothing."""
    say(f"\n{item.id}\n  title: {item.title}\n  date column: {item.row_date_literal}")
    for url in item.doc_urls:
        say(f"  {url}")
    say("List proposals in page order with `spidercrawler eval-link --list`.")
    while True:
        raw = ask("Item id of this adoption's proposal ('none', or blank to skip)")
        if not raw.strip():
            return None
        try:
            target = parse_answer(raw, item, items)
        except ValueError as exc:
            say(f"  {exc}")
            continue
        return LinkLabel(
            item_id=item.id, proposal_item_id=target, labeled_by=labeler, labeled_at=utcnow()
        )


@dataclass(frozen=True)
class RetrievalScore:
    k: int
    recall: Metric  # labeled proposals found in the top k
    mrr: float  # mean of 1/rank over the same items; 0 for a miss
    no_proposal: int  # items labeled "none": not scored


def score_links(
    labels: list[LinkLabel], rank: Callable[[str], list[str]], k: int
) -> RetrievalScore:
    """`rank(item_id)` returns related item ids, best first."""
    scored = [label for label in labels if label.proposal_item_id is not None]
    hits, reciprocal = 0, 0.0
    for label in scored:
        ranked = rank(label.item_id)[:k]
        if label.proposal_item_id in ranked:
            hits += 1
            reciprocal += 1 / (ranked.index(label.proposal_item_id) + 1)
    mrr = reciprocal / len(scored) if scored else 0.0
    return RetrievalScore(k, Metric(hits, len(scored)), mrr, len(labels) - len(scored))


def render_score(score: RetrievalScore) -> str:
    lines = ["# Retrieval eval: linking an adoption to its proposal", ""]
    if score.recall.n == 0:
        lines.append("_Pending: no link is labeled yet (`spidercrawler eval-link`)._")
    else:
        lines += [
            "| Metric | Result |",
            "|---|---|",
            f"| recall@{score.k} | {score.recall} |",
            f"| MRR@{score.k} | {score.mrr:.2f} over {score.recall.n} item(s) |",
        ]
    lines.append(
        f"\nItems labeled as having no recorded proposal (not scored): {score.no_proposal}"
    )
    lines.append("Retrieval is lexical (BM25 plus exact rule-id filters). No embeddings.")
    return "\n".join(lines) + "\n"
