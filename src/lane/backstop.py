"""Content checks against the run's documents (design note sections 7 and 8).

Used twice:

- the **backstop**: every request body headed off the machine, while the lane
  is on and the run has documents, is checked before it is sent;
- the **output guard**: what a raw (local) step returns is checked before any
  of it can be placed in another request.

Text on both sides is normalised the same way: Unicode NFKC, casefold, then
every run of characters that are not letters or digits becomes one space.
A request body is serialised by walking it and joining every key and string
value (numbers as text) with newlines; ``json.dumps`` is not used, because its
escapes (``\\n``, ``\\u00eb``) would break matches. The walk works on decoded
values: a string holding JSON (a prompt block built with ``json.dumps``, or
JSON of JSON) is decoded too, a few levels deep, before normalising. The
output guard uses the same walk and the same copy rules (``copied_rule``).

A body is flagged for a document when any of these holds:

(a) verbatim: it shares a run of ``max_verbatim_chars`` (default 120) or more
    consecutive normalised characters with the document;
(b) coverage: more than ``max_ngram_share`` (default 0.10) of the document's
    distinct word 8-grams appear in it (a document passed on in pieces);
(c) short document: a document shorter than the verbatim limit occurs whole
    in it. Documents under 24 normalised characters are skipped (they would
    match ordinary words); that is a stated limit.

Not caught, and said so wherever the lane is described: paraphrase,
translation, summaries, encodings (base64, spelling out) and copied pieces
below both limits. This is a backstop behind routing and taint, not proof.

The verbatim scan indexes each document's normalised text in aligned windows
of half the limit; any shared run of the full limit must contain one whole
aligned window, so the body is scanned once and each window hit is extended
both ways to its true length.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)
MIN_SHORT_DOC_CHARS = 24
_NGRAM = 8


def normalise(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return _NON_ALNUM.sub(" ", folded).strip()


_JSON_ESCAPE = re.compile(r'\\(?:u[0-9a-fA-F]{4}(?:\\u[0-9a-fA-F]{4})?|["\\/bfnrt])')
_UNESCAPE_LEVELS = 3


def _unescape_once(text: str) -> str:
    """Decode JSON string escapes (``\\n``, ``\\u00eb``, ``\\"``) in ``text``.

    A prompt often holds a ``json.dumps`` block whose escapes split the
    document's words; decoding them gives back the text as written."""
    def rep(m: re.Match) -> str:
        try:
            return json.loads('"' + m.group(0) + '"')
        except ValueError:
            return m.group(0)
    return _JSON_ESCAPE.sub(rep, text)


def _decoded_forms(text: str) -> list[str]:
    """``text`` plus each further decoding of its escapes (JSON nested in a
    string, and JSON of JSON), up to a few levels."""
    forms = [text]
    current = text
    for _ in range(_UNESCAPE_LEVELS):
        if "\\" not in current:
            break
        nxt = _unescape_once(current)
        if nxt == current:
            break
        forms.append(nxt)
        current = nxt
    return forms


def serialise(body: object) -> str:
    """Every key and every string or number in ``body``, one per line.

    Works on decoded values, never on escaped JSON text: a string that is
    itself JSON is parsed and walked too, and every string is also added
    with its JSON escapes decoded (see ``_decoded_forms``). A ``str`` body is
    parsed as JSON first when it is JSON.
    """
    out: list[str] = []

    def walk(value: object, depth: int = 0) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(str(key), depth)
                walk(item, depth)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, depth)
        elif isinstance(value, str):
            out.extend(_decoded_forms(value))
            stripped = value.strip()
            if depth < _UNESCAPE_LEVELS and stripped[:1] in ("{", "[", '"'):
                try:
                    parsed = json.loads(stripped)
                except ValueError:
                    return
                if parsed != value:
                    walk(parsed, depth + 1)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append(str(value))

    walk(body)
    return "\n".join(out)


def copied_rule(doc: "DocIndex", body_norm: str, max_verbatim_chars: int) -> Optional[str]:
    """The copy rule ``body_norm`` breaks for ``doc``, else None.

    "short_document" when a document of at least 24 and under
    ``max_verbatim_chars`` normalised characters occurs whole; "verbatim"
    when a run of ``max_verbatim_chars`` is shared. Used by the backstop
    and by the output guard, so both apply the same rules."""
    n = len(doc.norm)
    if n < MIN_SHORT_DOC_CHARS:
        return None
    if n < max_verbatim_chars:
        return "short_document" if doc.norm in body_norm else None
    return "verbatim" if doc.shares_run(body_norm, max_verbatim_chars) else None


def _grams(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + _NGRAM]) for i in range(len(words) - _NGRAM + 1)}


@dataclass
class DocIndex:
    """One document (or part) prepared for fast checks."""
    doc_id: str
    text: str
    verbatim_chars: int
    norm: str = ""
    _window: int = 0
    _index: dict = field(default_factory=dict)
    grams: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self.norm = normalise(self.text)
        self._window = max(1, self.verbatim_chars // 2)
        w = self._window
        for pos in range(0, len(self.norm) - w + 1, w):
            self._index.setdefault(self.norm[pos:pos + w], []).append(pos)
        self.grams = _grams(self.norm.split())

    def shares_run(self, other_norm: str, limit: Optional[int] = None) -> bool:
        """True when ``other_norm`` shares ``limit`` or more consecutive
        characters with this document (both already normalised)."""
        limit = limit or self.verbatim_chars
        doc, body, w = self.norm, other_norm, self._window
        if len(doc) < limit or len(body) < limit:
            return False
        if limit // 2 < w:      # a smaller limit than the index was built for
            return _plain_shares_run(doc, body, limit)
        n, m = len(doc), len(body)
        for j in range(0, m - w + 1):
            positions = self._index.get(body[j:j + w])
            if not positions:
                continue
            for p in positions:
                back = 0
                while p - back - 1 >= 0 and j - back - 1 >= 0 and doc[p - back - 1] == body[j - back - 1]:
                    back += 1
                fwd = w
                while p + fwd < n and j + fwd < m and doc[p + fwd] == body[j + fwd]:
                    fwd += 1
                if back + fwd >= limit:
                    return True
        return False


def _plain_shares_run(doc: str, body: str, limit: int) -> bool:
    for start in range(0, len(doc) - limit + 1):
        if doc[start:start + limit] in body:
            return True
    return False


def find_leak(
    body: object,
    docs: Iterable[DocIndex],
    max_verbatim_chars: int,
    max_ngram_share: float,
) -> Optional[tuple[str, str]]:
    """(doc id, rule) for the first document ``body`` carries, else None.

    ``body`` is a request body (dict/list) or a plain string.
    """
    body_norm = normalise(serialise(body))
    if not body_norm:
        return None
    body_grams: Optional[set] = None
    for doc in docs:
        n = len(doc.norm)
        if n < MIN_SHORT_DOC_CHARS:
            continue
        rule = copied_rule(doc, body_norm, max_verbatim_chars)
        if rule is not None:
            return doc.doc_id, rule
        if n < max_verbatim_chars:
            continue
        if doc.grams:
            if body_grams is None:
                body_grams = _grams(body_norm.split())
            share = len(doc.grams & body_grams) / len(doc.grams)
            if share > max_ngram_share:
                return doc.doc_id, "coverage"
    return None
