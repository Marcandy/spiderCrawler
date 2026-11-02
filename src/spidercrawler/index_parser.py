"""Parse a rules committee page into table rows (PLAN.md section 3).

Every committee page has three tables, found by their <caption>:
Proposed Rules for Public Comment, Approved Rules, Prior Proposed Rules.
Each row: a main PDF link (the title), optional extra PDFs in a <ul>, and a date cell
(deadline for proposals, effective date for approved rules). Page chrome is never returned.
"""

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from spidercrawler.normalize import normalize
from spidercrawler.records import TableName

CAPTIONS: dict[str, TableName] = {
    "proposed rules for public comment": "proposed",
    "approved rules": "approved",
    "prior proposed rules": "prior_proposed",
}
EXPECTED_TABLES: frozenset[TableName] = frozenset(CAPTIONS.values())


@dataclass(frozen=True)
class Row:
    table: TableName
    title: str
    doc_urls: tuple[str, ...]
    date_literal: str | None

    @property
    def item_key(self) -> str | None:
        """Path of the first PDF, lower-cased: stable across table moves and reordering."""
        return urlsplit(self.doc_urls[0]).path.lower() if self.doc_urls else None


@dataclass
class ParseResult:
    rows: list[Row] = field(default_factory=list)
    tables_found: set[TableName] = field(default_factory=set)


def _text(node: LexborNode | None) -> str:
    return normalize(node.text(separator=" ")) if node is not None else ""


def _parse_row(tr: LexborNode, table: TableName, base_url: str) -> Row | None:
    cells = tr.css("td")
    if not cells:
        return None
    first = cells[0]
    links = [a for a in first.css("a") if a.attributes.get("href")]
    pdfs = [
        url
        for a in links
        if (url := urljoin(base_url, a.attributes["href"] or "")).startswith(
            ("https://", "http://")
        )
        and urlsplit(url).path.lower().endswith(".pdf")
    ]
    main = links[0] if links else None
    title = _text(main) if main is not None else _text(first)
    if not title:
        return None
    date = _text(cells[-1]) if len(cells) > 1 else ""
    return Row(
        table=table, title=title, doc_urls=tuple(dict.fromkeys(pdfs)), date_literal=date or None
    )


def parse_committee_page(html: str, base_url: str) -> ParseResult:
    tree = LexborHTMLParser(html)
    result = ParseResult()
    for table_node in tree.css("table"):
        caption = _text(table_node.css_first("caption")).lower()
        table = CAPTIONS.get(caption)
        if table is None:
            continue
        result.tables_found.add(table)
        for tr in table_node.css("tbody tr"):
            if (row := _parse_row(tr, table, base_url)) is not None:
                result.rows.append(row)
    return result
