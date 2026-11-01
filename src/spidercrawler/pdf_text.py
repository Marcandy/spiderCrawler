"""Extract normalized text from committee PDFs, keeping the change markup (PLAN.md section 3).

Rule-text PDFs mark additions with **bold underline** and deletions with **bold [brackets]**.
Brackets survive plain text extraction; underline does not, so additions would look like
unchanged text. Underlines in these PDFs are thin filled rectangles under the glyphs, so each
character is tested against them and underlined runs are wrapped as `{+ ... +}`.
"""

import io
import re
from dataclasses import dataclass
from typing import Any

import pdfplumber
from pdfplumber.utils import cluster_objects, extract_text

from spidercrawler.normalize import ADD_CLOSE, ADD_OPEN, normalize

UNDERLINE_MAX_HEIGHT = 2.5  # points; table cell backgrounds are ~15
UNDERLINE_MIN_WIDTH = 3.0
LINE_TOLERANCE = 3.0
_ADJACENT_RUNS = re.compile(re.escape(ADD_CLOSE) + r"(\s*)" + re.escape(ADD_OPEN))
_PAGE_NUMBER_LINE = re.compile(r"\n[ \t]*(?:-[ \t]*)?\d{1,3}(?:[ \t]*-)?[ \t]*$")
_PRIVATE_USE = re.compile("[\ue000-\uf8ff]")  # icon-font glyphs, e.g. form checkboxes
_MARKUP_LEGEND = re.compile(r"\b(bold(ed)?\s+and\s+underlined|underlined)\b", re.IGNORECASE)

Obj = dict[str, Any]


class PdfError(Exception):
    pass


@dataclass(frozen=True)
class PdfText:
    text: str
    underlined_chars: int
    markup_lost: bool  # the document marks changes, but no underline was detected
    no_text: bool  # no text layer (scanned); OCR is out of scope


def _underlines(page: Any) -> list[Obj]:
    return [
        r
        for r in [*page.rects, *page.lines]
        if r["bottom"] - r["top"] <= UNDERLINE_MAX_HEIGHT
        and r["x1"] - r["x0"] >= UNDERLINE_MIN_WIDTH
    ]


def _is_underlined(char: Obj, underlines: list[Obj]) -> bool:
    if not char["text"].strip():
        return False
    center = (char["x0"] + char["x1"]) / 2
    height = char["bottom"] - char["top"]
    # Below the lower part of the glyph, not through its middle (that would be strikethrough).
    low, high = char["top"] + 0.6 * height, char["bottom"] + 3
    return any(
        u["x0"] - 0.5 <= center <= u["x1"] + 0.5 and low <= u["top"] <= high for u in underlines
    )


def _mark_page(page: Any) -> tuple[list[Obj], int, bool]:
    """Copy the page's chars, wrapping underlined runs. Returns chars, count, bold bracket seen."""
    underlines = _underlines(page)
    chars = [dict(c) for c in page.chars]
    count = 0
    bold_bracket = False
    for line in cluster_objects(chars, lambda c: c["top"], LINE_TOLERANCE):
        line.sort(key=lambda c: c["x0"])
        visible = [c for c in line if c["text"].strip()]
        flags = [_is_underlined(c, underlines) for c in visible]
        count += sum(flags)
        bold_bracket |= any(c["text"] == "[" and "Bold" in c["fontname"] for c in visible)
        for i, (char, flag) in enumerate(zip(visible, flags, strict=True)):
            if not flag:
                continue
            if i == 0 or not flags[i - 1]:
                char["text"] = ADD_OPEN + char["text"]
            if i == len(visible) - 1 or not flags[i + 1]:
                char["text"] = char["text"] + ADD_CLOSE
    return chars, count, bold_bracket


def extract_pdf_text(data: bytes) -> PdfText:
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:  # pdfminer raises many unrelated types for bad input
        raise PdfError(f"cannot open PDF: {exc}") from exc
    pages: list[str] = []
    underlined = 0
    bold_bracket = False
    any_chars = False
    with pdf:
        for page in pdf.pages:
            chars, count, bracket = _mark_page(page)
            any_chars |= bool(chars)
            underlined += count
            bold_bracket |= bracket
            page_text = extract_text(chars, x_tolerance=1.5, y_tolerance=LINE_TOLERANCE)
            # A bare page number as the last line would sit inside any quote that
            # spans the page break, and the quote would then fail to match.
            pages.append(_PAGE_NUMBER_LINE.sub("", page_text))

    text = _PRIVATE_USE.sub("", "\n\n".join(pages))
    text = _ADJACENT_RUNS.sub(r"\1", text)
    text = normalize(text)
    marks_changes = bool(_MARKUP_LEGEND.search(text)) or bold_bracket
    return PdfText(
        text=text,
        underlined_chars=underlined,
        markup_lost=any_chars and marks_changes and underlined == 0,
        no_text=not any_chars,
    )
