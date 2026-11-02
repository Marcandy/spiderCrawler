"""Static digest of approved changes (PLAN.md sections 4 and 6, accountability).

Only items whose newest extraction has a publishable newest review appear. Each entry links
its source PDFs and names every record behind it, so any line can be traced back to the
snapshot, model, prompt version, verifier result, and reviewer.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from spidercrawler.canonical import canonicalize
from spidercrawler.records import Extraction, Item, RecordStore, Review
from spidercrawler.router import is_publishable
from spidercrawler.schema import ChangeSummary

_templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "digest_templates"),
    autoescape=select_autoescape(["html"]),
)


@dataclass(frozen=True)
class Entry:
    item: Item
    extraction: Extraction
    review: Review
    output: dict  # the reviewer's edit if there was one, else the model's output
    canonical: dict  # derived from `output`, so an edited rule id or date is what publishes

    @property
    def safe_doc_urls(self) -> list[str]:
        return [u for u in self.item.doc_urls if u.startswith(("https://", "http://"))]

    def as_json(self) -> dict:
        return {
            "item_id": self.item.id,
            "title": self.item.title,
            "source_id": self.item.source_id,
            "doc_urls": self.safe_doc_urls,
            "summary": self.output["summary"],
            "stage": self.output["stage"],
            "rule_ids": self.canonical.get("rule_ids", []),
            "effective_date": self.canonical.get("effective_date"),
            "expungement_relevance": self.output["expungement_relevance"],
            "extraction_id": self.extraction.id,
            "model_id": self.extraction.model_id,
            "prompt_version": self.extraction.prompt_version,
            "verifier_status": self.review.edited_verifier_status
            or self.extraction.verifier_status,
            "review_id": self.review.id,
            "reviewer": self.review.reviewer,
            "decision": self.review.decision,
            "decided_at": self.review.decided_at.isoformat(),
        }


def collect(records: RecordStore, rule_prefixes: dict[str, str] | None = None) -> list[Entry]:
    items = {i.id: i for i in records.iter(Item)}
    newest: dict[str, Extraction] = {}
    for ext in records.iter(Extraction):
        if ext.item_id not in newest or ext.created_at > newest[ext.item_id].created_at:
            newest[ext.item_id] = ext
    reviews: dict[str, list[Review]] = {}
    for review in records.iter(Review):
        reviews.setdefault(review.extraction_id, []).append(review)

    entries = []
    for item_id, ext in newest.items():  # an approval of an older extraction never publishes
        decisions = reviews.get(ext.id, [])
        if item_id not in items or not is_publishable(decisions):
            continue
        latest = max(decisions, key=lambda r: r.decided_at)
        output = latest.edited_output if latest.decision == "edit" else ext.output
        if output is None:
            continue
        prefix = (rule_prefixes or {}).get(items[item_id].source_id, "Pa.R.Crim.P.")
        canonical = canonicalize(ChangeSummary.model_validate(output), prefix).model_dump(
            mode="json"
        )
        entries.append(Entry(items[item_id], ext, latest, output, canonical))
    return sorted(entries, key=lambda e: e.review.decided_at, reverse=True)


def build(
    records: RecordStore,
    out_dir: Path,
    generated_at: datetime,
    rule_prefixes: dict[str, str] | None = None,
) -> list[Entry]:
    entries = collect(records, rule_prefixes)
    out_dir.mkdir(parents=True, exist_ok=True)
    html = _templates.get_template("index.html").render(entries=entries, generated_at=generated_at)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    payload = {"generated_at": generated_at.isoformat(), "entries": [e.as_json() for e in entries]}
    (out_dir / "digest.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return entries
