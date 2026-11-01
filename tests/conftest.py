from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CRIM_URL = "https://www.pacourts.us/courts/supreme-court/committees/rules-committees/criminal-procedural-rules-committee"

NEW_ROW = """
                    <tbody>
                        <tr>
                            <td>
                                <a href="/Storage/media/pdfs/20261101/120000-proposed-rule-999.pdf" target="_blank">Proposed Amendment of Pa.R.Crim.P. 999</a>
                                    <ul class="rulechanges">
    </ul>
                            </td>
                            <td>January 15, 2027</td>
                        </tr>
                    </tbody>
            </table>"""


@pytest.fixture
def crim_html() -> str:
    """Real criminal committee page saved 2026-11-01."""
    return (FIXTURES / "pages" / "crim.html").read_text(encoding="utf-8")


@pytest.fixture
def evidence_html() -> str:
    """Real evidence committee page saved 2026-11-01."""
    return (FIXTURES / "pages" / "evidence.html").read_text(encoding="utf-8")


# Variants are derived from the real page so they stay in sync with its markup.


def with_new_proposed_row(html: str) -> str:
    """The real page with one extra row in the Proposed table."""
    head, sep, tail = html.partition("</tbody>\n            </table>")
    assert sep, "fixture markup changed"
    return head + "</tbody>" + NEW_ROW + tail


def with_renamed_heading(html: str) -> str:
    return html.replace("<caption>Approved Rules</caption>", "<caption>Adopted Rules</caption>")


def with_empty_tables(html: str) -> str:
    import re

    return re.sub(r"(?s)<tbody>.*?</tbody>", "<tbody></tbody>", html)


def with_moved_pdfs(html: str) -> str:
    """Every PDF under a new folder: what a site migration looks like (all keys change)."""
    return html.replace('href="/Storage/media/pdfs/', 'href="/Storage/archive/pdfs/').replace(
        'href="/storage/rules/', 'href="/storage/archive/rules/'
    )


def without_last_rows(html: str, count: int) -> str:
    """Drop the last `count` rows (each row sits in its own <tbody>): a partial page."""
    import re

    bodies = list(re.finditer(r"(?s)<tbody>.*?</tbody>", html))
    for match in reversed(bodies[-count:]):
        html = html[: match.start()] + html[match.end() :]
    return html
