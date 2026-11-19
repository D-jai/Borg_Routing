"""The on-premise lane's error type.

Every lane failure carries one of the ``BRT-LANE-*`` codes (see
``docs/errors/error-registry.md``). ``LaneError`` subclasses ``ValueError`` so
the callers' existing ``except ValueError: raise`` paths pass it on untouched;
callers that have a generic fallback (the router's ``fallback_default``) must
catch ``LaneError`` first, because a lane refusal never falls back.

Messages name codes, model ids, URLs and relative paths only: never document
text and never an absolute path (design note, sections 4 and 5.4).
"""
from __future__ import annotations

import re
from typing import Optional

_CODE_RE = re.compile(r"BRT-[A-Z]+-\d{3}(?:-[A-Z]+)?")


class LaneError(ValueError):
    """A lane rule refused something. ``code`` is the ``BRT-LANE-*`` code."""

    def __init__(self, code: str, message: str, doc: Optional[str] = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.doc = doc


class RawStepError(ValueError):
    """``by_reference`` only: a raw step failed while it ran (a ``LaneError``
    such as a ledger write that failed, ``BRT-LANE-007``, or a runtime error
    such as ``BRT-ANS-003``).

    ``by_reference`` makes no privacy promise, so the failure goes up as
    ``failure``: ``{"code", "doc", "message"}``, its own code, the document
    ids and the first ``MESSAGE_MAX`` characters of the message.
    """

    MESSAGE_MAX = 200

    def __init__(self, code: str, message: str, doc: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.doc = doc
        self.message = message[: self.MESSAGE_MAX]

    @property
    def failure(self) -> dict:
        return {"code": self.code, "doc": self.doc, "message": self.message}


def code_of(text: object, default: str = "BRT-ANS-008") -> str:
    """The first ``BRT-...`` code in ``text``, else ``default``."""
    if isinstance(text, LaneError):
        return text.code
    m = _CODE_RE.search(str(text or ""))
    return m.group(0) if m else default
