"""The one text normalizer. Hashing, storage, and quote matching all go through here.

`normalize()` produces the stored text that the extractor sees.
`for_matching()` is the stricter form used to compare a quote against source text.
Both sides of every comparison must go through the same function, or matches drift.
"""

import hashlib
import re
import unicodedata

ADD_OPEN = "{+"
ADD_CLOSE = "+}"

_QUOTES = str.maketrans(
    {
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote
        "\u201a": "'",  # low single quote
        "\u201b": "'",  # reversed single quote
        "\u2032": "'",  # prime
        "\u201c": '"',  # left double quote
        "\u201d": '"',  # right double quote
        "\u201e": '"',  # low double quote
        "\u201f": '"',  # reversed double quote
        "\u2033": '"',  # double prime
    }
)
# hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar, minus
_DASHES = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
# soft hyphen, zero-width space / non-joiner / joiner, byte order mark
_INVISIBLE = re.compile("[\u00ad\u200b\u200c\u200d\ufeff]")

# Order matters: each rule assumes the ones above it already ran.
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[ \t\f\v]+"), " "),  # runs of horizontal space
    (re.compile(r" *\n *"), "\n"),  # trailing / leading space on lines
    (re.compile(r"(?<=[a-z])-\n(?=[a-z])"), ""),  # line-end hyphenation: proce-\ndure
    (re.compile(r"\n{3,}"), "\n\n"),  # at most one blank line
    (re.compile(r"(?<=\w) +\.(?=\w)"), "."),  # PDF glitch: "Pa.R.J .A." -> "Pa.R.J.A."
    (re.compile(r"([(\[]) +"), r"\1"),  # "( h)" -> "(h)"
    (re.compile(r" +([)\]])"), r"\1"),  # "jurors ]" -> "jurors]"
    (re.compile(r" +([,;:])"), r"\1"),  # "Rules ," -> "Rules,"
    (re.compile(r"\{\+\s+"), ADD_OPEN),  # "{+ text" -> "{+text"
    (re.compile(r"\s+\+\}"), ADD_CLOSE),  # "text +}" -> "text+}"
]
_ANY_SPACE = re.compile(r"\s+")
_INTRA_WORD_HYPHEN = re.compile(r"(?<=[A-Za-z])-(?=[A-Za-z])")


def normalize(text: str) -> str:
    """Canonical stored form: unified quotes/dashes, cleaned PDF spacing, paragraphs kept."""
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text).translate(_QUOTES)
    text = _DASHES.sub("-", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text.strip()


def strip_markup(text: str) -> str:
    """Remove addition markers so a quote matches with or without them."""
    return text.replace(ADD_OPEN, "").replace(ADD_CLOSE, "")


def for_matching(text: str) -> str:
    """Form used for quote matching: normalized, markup stripped, all whitespace collapsed.

    Hyphens inside words are dropped, because `normalize()` joins line-end hyphenation
    ("non-\nviolent" -> "nonviolent") and a quote may restore the hyphen.
    Case is folded: a quote that capitalizes its first word, or retypes an all-caps heading,
    is still the same words. Stored and displayed text keep their original case.
    """
    text = _INTRA_WORD_HYPHEN.sub("", strip_markup(normalize(text)))
    return _ANY_SPACE.sub(" ", text).strip().casefold()


def quote_in_source(quote: str, source: str) -> bool:
    q = for_matching(quote)
    return bool(q) and q in for_matching(source)


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()
