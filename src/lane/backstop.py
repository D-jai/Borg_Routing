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
escapes (``\\n``) would break matches.

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


def serialise(body: object) -> str:
    """Every key and every string or number in ``body``, one per line."""
    out: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                out.append(str(key))
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            out.append(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append(str(value))

    walk(body)
    return "\n".join(out)


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
    text = body if isinstance(body, str) else serialise(body)
    body_norm = normalise(text)
    if not body_norm:
        return None
    body_grams: Optional[set] = None
    for doc in docs:
        n = len(doc.norm)
        if n < MIN_SHORT_DOC_CHARS:
            continue
        if n < max_verbatim_chars:
            if doc.norm in body_norm:
                return doc.doc_id, "short_document"
            continue
        if doc.shares_run(body_norm, max_verbatim_chars):
            return doc.doc_id, "verbatim"
        if doc.grams:
            if body_grams is None:
                body_grams = _grams(body_norm.split())
            share = len(doc.grams & body_grams) / len(doc.grams)
            if share > max_ngram_share:
                return doc.doc_id, "coverage"
    return None
