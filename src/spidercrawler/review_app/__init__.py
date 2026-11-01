"""Local review page (PLAN.md sections 4 and 7): approve, edit, or reject extractions.

Reads the JSON records directly and writes each decision as a new `reviews/*.json` record.
Every decision goes through `router.decide`, so the page cannot approve a blocked item or
accept an edit that fails the verifier. Website text is HTML-escaped (Jinja autoescape).
"""

import json
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from spidercrawler.config import Config
from spidercrawler.detector import load_known
from spidercrawler.records import Document, Extraction, Item, RecordStore, Review
from spidercrawler.router import DecisionRefusedError, decide, is_publishable
from spidercrawler.verifier import RowContext

QUEUE_ORDER = ("blocked", "needs_attention", "ready_for_review")
_templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


def create_app(records: RecordStore, config: Config, reviewer: str = "maintainer") -> FastAPI:
    app = FastAPI(title="spiderCrawler review", docs_url=None, redoc_url=None)

    def latest_extractions() -> dict[str, Extraction]:
        latest: dict[str, Extraction] = {}
        for ext in records.iter(Extraction):
            if ext.item_id not in latest or ext.created_at > latest[ext.item_id].created_at:
                latest[ext.item_id] = ext
        return latest

    def reviews_for(extraction_id: str) -> list[Review]:
        return sorted(
            (r for r in records.iter(Review) if r.extraction_id == extraction_id),
            key=lambda r: r.decided_at,
        )

    def context(ext: Extraction) -> tuple[Item, str, RowContext, list[Document]]:
        items = {i.id: i for i in records.iter(Item)}
        item = items[ext.item_id]
        docs = {d.url: d for d in records.iter(Document) if d.item_id == item.id}
        ordered = [docs[u] for u in item.doc_urls if u in docs]
        tables = {k.item.id: k.current_table for k in load_known(records).values()}
        prefix = next((s.rule_prefix for s in config.sources if s.id == item.source_id), "")
        row = RowContext(tables.get(item.id, item.table), item.title, item.row_date_literal, prefix)
        return item, "\n\n".join(d.text for d in ordered), row, ordered

    def find(extraction_id: str) -> Extraction:
        for ext in records.iter(Extraction):
            if ext.id == extraction_id:
                return ext
        raise HTTPException(404, "no such extraction")

    def render_detail(ext: Extraction, error: str = "", edited: str = "", status: int = 200):
        item, _, row, docs = context(ext)
        reviews = reviews_for(ext.id)
        html = _templates.get_template("detail.html").render(
            ext=ext,
            item=item,
            row=row,
            docs=docs,
            reviews=reviews,
            error=error,
            output_json=edited or json.dumps(ext.output, indent=2) if ext.output or edited else "",
            publishable=is_publishable(reviews),
            reviewer=reviewer,
            can_approve=ext.queue_status != "blocked" and ext.output is not None,
        )
        return HTMLResponse(html, status_code=status)

    @app.get("/", response_class=HTMLResponse)
    def queue() -> str:
        items = {i.id: i for i in records.iter(Item)}
        rows = []
        for ext in latest_extractions().values():
            reviews = reviews_for(ext.id)
            rows.append(
                {
                    "ext": ext,
                    "item": items.get(ext.item_id),
                    "decision": reviews[-1].decision if reviews else None,
                }
            )
        rows.sort(
            key=lambda r: (r["decision"] is not None, QUEUE_ORDER.index(r["ext"].queue_status))
        )
        return _templates.get_template("queue.html").render(rows=rows)

    @app.get("/extraction/{extraction_id}", response_class=HTMLResponse)
    def detail(extraction_id: str) -> HTMLResponse:
        return render_detail(find(extraction_id))

    @app.post("/extraction/{extraction_id}/decide")
    async def decide_route(extraction_id: str, request: Request):
        ext = find(extraction_id)
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8")).items()}
        decision = form.get("decision", "")
        edited_text = form.get("edited_json", "")
        _, source_text, row, _ = context(ext)
        edited = None
        if decision == "edit":
            try:
                edited = json.loads(edited_text)
            except json.JSONDecodeError as exc:
                return render_detail(
                    ext, f"edited output is not valid JSON: {exc}", edited_text, 400
                )
        try:
            review = decide(
                ext,
                decision,
                form.get("reviewer") or reviewer,
                note=form.get("note", ""),
                edited=edited,
                source_text=source_text,
                row=row,
                rule_prefix=row.rule_prefix,
            )
        except DecisionRefusedError as exc:
            return render_detail(ext, str(exc), edited_text, 400)
        records.write(review)
        return RedirectResponse(f"/extraction/{extraction_id}", status_code=303)

    return app
