"""The "items await review" notice for a public GitHub issue.

The human gate only works if a human knows there is something to review. The notice holds
titles, counts, and source links: all of it already public on the court's website. It never
holds a summary, a quote, or any other model output, because none of that has passed review.
It is built from `Item` records only, so there is no extraction text to leak.
"""

import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from spidercrawler.config import Source
from spidercrawler.records import Item, RecordStore
from spidercrawler.status import _count, review_queue

LABEL = "review-needed"
MARKER = "<!-- spidercrawler:review-notice -->"
MAX_TITLE = 200
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()<>#|!~])")
_SPACE = re.compile(r"\s+")
_URL_SAFE = ":/?#@!$&'*+,;=%~._-"  # no brackets or spaces: they would end a Markdown link


@dataclass(frozen=True)
class ReviewNotice:
    title: str
    body: str
    count: int


def _plain(text: str) -> str:
    """Website text as inert Markdown: one line, no formatting, links, or @mentions."""
    text = _SPACE.sub(" ", text).strip()
    if len(text) > MAX_TITLE:
        text = text[: MAX_TITLE - 1].rstrip() + "…"
    # A zero-width space after @ keeps a title from notifying a GitHub user.
    return _MARKDOWN.sub(r"\\\1", text).replace("&", "&amp;").replace("@", "@​")


def _links(item: Item) -> str:
    links = []
    for url in item.doc_urls:
        if not url.startswith(("https://", "http://")):
            continue
        name = urlsplit(url).path.rsplit("/", 1)[-1] or "document"
        links.append(f"[{_plain(name)}]({quote(url, safe=_URL_SAFE)})")
    return ", ".join(links) or "no source link"


def _lines(items: list[Item]) -> list[str]:
    return [f"- {_plain(i.title)} ({_links(i)})" for i in sorted(items, key=lambda i: i.id)]


def build_notice(records: RecordStore, sources: list[Source]) -> ReviewNotice | None:
    """The notice, or None when nothing awaits a person. The same records give the same text."""
    queue = review_queue(records, sources)
    ready, waiting = queue.awaiting_review, queue.awaiting_extraction
    count = len(ready) + len(waiting)
    if count == 0:
        return None
    verb = "awaits" if count == 1 else "await"
    lines = [
        MARKER,
        f"**{_count(count, 'item')} {verb} human review.** "
        "Nothing is published until a person decides.",
    ]
    if ready:
        lines += ["", f"### Ready to review ({len(ready)})", "", *_lines(ready)]
    if waiting:
        lines += [
            "",
            f"### Detected, not yet summarized ({len(waiting)})",
            "",
            "These wait for the next weekly run (a document, the API key, or the run budget "
            "was missing).",
            "",
            *_lines(waiting),
        ]
    lines += [
        "",
        "### How to review",
        "",
        "```sh",
        "make data-checkout && make review DATA_DIR=data-branch/data",
        "make data-push      # after deciding",
        "make publish        # rebuild the digest",
        "```",
        "",
        "This issue lists titles and source links only, never an unreviewed summary. "
        "The weekly run updates it and closes it when nothing is left.",
    ]
    title = f"Review needed: {_count(count, 'rule item')} {verb} review"
    return ReviewNotice(title, "\n".join(lines) + "\n", count)
