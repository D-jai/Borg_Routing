"""The run's lane state, carried in a context variable.

``prepare_lane(documents, db_path)`` reads the lane settings and loads the
request's documents; it returns None when the lane is off and no documents
were sent, so a run with ``lane_mode: off`` takes exactly the Phase 1 path.
``orchestrate_run`` puts the context in ``lane_scope`` for the run; child runs
and every task the run starts inherit it.

Taint rules live in the engine (which steps are raw); this module holds what
every layer needs: locality as the lane sees it, document text by id (loaded
only inside the executing child task), the planner's manifest block, and the
output guard (P2).

Error codes:
- BRT-LANE-004: a raw step's output was withheld (too large, or it copies a
  long passage of a document)
- BRT-LANE-005: documents sent while lane_mode is off
"""
from __future__ import annotations

import contextvars
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

from src.envelopes.schema import InboundEnvelope, WrappedTask
from src.registry.locality import is_local_endpoint, is_local_model
from src.registry.models import ModelInfo

from .backstop import DocIndex, copied_rule, normalise, serialise
from .documents import LoadedDocuments, load_documents
from .errors import LaneError
from .settings import LaneSettings, load_lane_settings

_current: contextvars.ContextVar[Optional["LaneContext"]] = contextvars.ContextVar(
    "borg_lane", default=None
)


@dataclass
class LaneContext:
    settings: LaneSettings
    docs: Optional[LoadedDocuments]
    db_path: str
    _indexes: dict = field(default_factory=dict)

    # -- mode -------------------------------------------------------------------
    @property
    def mode(self) -> str:
        return self.settings.mode

    @property
    def all_local(self) -> bool:
        return self.settings.mode == "all_local"

    @property
    def keeps_raw_local(self) -> bool:
        """``raw_local``/``all_local``: raw steps stay local, guard and
        backstop run. False in ``by_reference`` (no privacy promise)."""
        return self.settings.keeps_raw_local

    @property
    def has_documents(self) -> bool:
        return bool(self.docs and self.docs.documents)

    # -- locality ---------------------------------------------------------------
    def is_local(self, model: Optional[ModelInfo], url: Optional[str] = None) -> bool:
        return is_local_model(
            model,
            local_hosts=list(self.settings.local_hosts),
            allowlist=list(self.settings.local_allowlist) or None,
            url=url,
        )

    def is_local_url(self, url: Optional[str]) -> bool:
        return is_local_endpoint(url, list(self.settings.local_hosts))

    # -- documents --------------------------------------------------------------
    def all_part_ids(self) -> list[str]:
        if not self.docs:
            return []
        return [p.part_id for d in self.docs.documents for p in d.parts]

    def expand(self, ids: object) -> list[str]:
        """Part ids for a list of document or part ids.

        Raises:
            ValueError: BRT-ORC-008 for an id that names no loaded document.
        """
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list) or not ids:
            raise ValueError("BRT-ORC-008: 'documents' must be a non-empty list of document ids")
        out: list[str] = []
        for raw in ids:
            key = str(raw).strip()
            if self.docs and key in self.docs.part_index:
                out.append(key)
            elif self.docs and key in self.docs.doc_index:
                out.extend(p.part_id for p in self.docs.doc_index[key].parts)
            else:
                raise ValueError(f"BRT-ORC-008: unknown document id '{key}'")
        return list(dict.fromkeys(out))

    def is_part_id(self, value: object) -> bool:
        return bool(self.docs) and isinstance(value, str) and value in self.docs.part_index

    def chars_of(self, part_ids: list[str]) -> int:
        if not self.docs:
            return 0
        return sum(len(self.docs.part_index[p].text) for p in part_ids if p in self.docs.part_index)

    def text_block(self, part_ids: list[str]) -> str:
        """The document text a raw task reads. Call only inside the task that
        is about to send it to a local model."""
        if not self.docs:
            return ""
        blocks = []
        for pid in part_ids:
            part = self.docs.part_index.get(pid)
            if part is None:
                continue
            doc = self.docs.doc_index[part.doc_id]
            label = pid if self.settings.manifest == "ids" else f"{pid} ({doc.name})"
            blocks.append(f"Document {label}:\n{part.text}")
        return "\n\n".join(blocks)

    def indexes(self, part_ids: Optional[list[str]] = None) -> list[DocIndex]:
        """Prepared texts for the checks: the named parts, else every document."""
        if not self.docs:
            return []
        if part_ids:
            wanted = [self.docs.part_index[p] for p in part_ids if p in self.docs.part_index]
            items = [(p.part_id, p.text) for p in wanted]
        else:
            items = [(d.doc_id, d.text) for d in self.docs.documents]
        out = []
        for key, text in items:
            if key not in self._indexes:
                self._indexes[key] = DocIndex(key, text, self.settings.max_verbatim_chars)
            out.append(self._indexes[key])
        return out

    # -- the planner's view -------------------------------------------------------
    def manifest(self) -> list[dict]:
        return self.docs.manifest(self.settings.manifest) if self.docs else []

    def manifest_block(self) -> str:
        """Prepended to the orchestrator's stage prompt when the run has
        documents. A manifest only: ids, types, sizes and (unless
        ``lane_manifest: ids``) names; never any text."""
        lines = []
        for row in self.manifest():
            if "id" in row:
                name = f" {row['name']}" if "name" in row else ""
                parts = f", {row['parts']} parts" if row.get("parts", 1) > 1 else ""
                lines.append(f"- {row['id']}:{name} ({row['type']}, {row['size']} bytes{parts})")
            else:
                name = f" {row['name']}" if "name" in row else " (a file)"
                lines.append(f"-{name} ({row['type']}, {row['size']} bytes): unsupported, not read")
        if self.keeps_raw_local:
            where = (
                "The operator attached the documents below. You never see their text:\n"
                "steps that read them run on the operator's own machines. Refer to\n"
                "them by id only:\n"
            )
        else:
            # by_reference: honest wording, no claim that the text stays local.
            where = (
                "The operator attached the documents below. You never see their text:\n"
                "the step that reads a document is given its text, and the models\n"
                "serving such steps may be cloud models. Refer to them by id only:\n"
            )
        return (
            "# Documents (by reference)\n"
            + where +
            '- an apply_to_each step with "inputs_ref": "documents" runs once per\n'
            "  document (or part of one). \"documents\" is the one reserved name that\n"
            "  is not a step id; use it, do not copy document ids into \"items\";\n"
            '- an answerer step with "documents": ["doc_001"] reads the named documents.\n'
            "Ask such steps for short structured fields (names, dates, labels), never\n"
            "for copies of the text. Do not answer directly: the answer must come from\n"
            "steps that read the documents.\n\n"
            + "\n".join(lines)
            + "\n\n---\n\n"
        )


def current_lane() -> Optional[LaneContext]:
    return _current.get()


@contextmanager
def lane_scope(ctx: Optional[LaneContext]) -> Iterator[None]:
    """Make ``ctx`` the lane of everything run inside the block."""
    token = _current.set(ctx)
    try:
        yield
    finally:
        _current.reset(token)


async def prepare_lane(documents: Optional[dict], db_path: str) -> Optional[LaneContext]:
    """The lane state for a new run, or None when the lane is off.

    Raises:
        LaneError: BRT-LANE-005 when documents are sent with the lane off, or a
            lane setting is invalid; BRT-LANE-001 for a refused document path.
    """
    settings = await load_lane_settings(db_path)
    if not settings.on:
        if documents:
            raise LaneError(
                "BRT-LANE-005",
                "documents need the on-premise lane: set lane_mode to raw_local, "
                "all_local or by_reference",
            )
        return None
    docs = load_documents(documents, part_chars=settings.part_chars) if documents else None
    return LaneContext(settings=settings, docs=docs, db_path=db_path)


# -- failures and the output guard ---------------------------------------------------

def doc_label(part_ids: list[str]) -> Optional[str]:
    return ",".join(part_ids) if part_ids else None


def failure_of(code: str, part_ids: list[str]) -> dict:
    """How a raw failure is seen upward: a code and document ids, nothing else."""
    return {"code": code, "doc": doc_label(part_ids)}


def failed_envelope(task: WrappedTask, code: str, base: Optional[InboundEnvelope] = None) -> InboundEnvelope:
    """A failed envelope carrying only the code and the task's document ids.

    Costs already incurred (``base``) are kept so the run total stays true.
    """
    failure = failure_of(code, list(task.documents))
    return InboundEnvelope(
        envelope_id=task.envelope_id,
        subtask_id=task.subtask_id,
        status="failed",
        output={"failure": failure},
        failure_reason=json.dumps(failure),
        tokens_used=(base.tokens_used if base else {}),
        dollars=(base.dollars if base else None),
        baseline_dollars=(base.baseline_dollars if base else None),
        lane_guarded=False,
    )


_RAW_CODE_RE = re.compile(r"\s*(BRT-[A-Z]+-\d+)\b")
_RAW_CODE_FULL = re.compile(r"BRT-[A-Z]+-\d+")
_RAW_CODE_MAX = 16          # registry codes are like BRT-LANE-004 (12 characters)
RAW_FAILURE_DEFAULT = "BRT-LANE-004"


def raw_failure_code(reason: object, default: str = RAW_FAILURE_DEFAULT) -> str:
    """The code of a failure the runtime itself produced, as shown upward.

    Call it only on runtime reasons (our own exceptions and envelopes); a
    reason the local model wrote never reaches it (``guard_envelope`` turns
    those into ``BRT-LANE-004`` by source). The code is the
    ``BRT-[A-Z]+-\\d+`` that the reason starts with, when it is code-sized,
    else the fixed ``default``; nothing later in the text (which may quote
    model output) is read.
    """
    if isinstance(reason, LaneError):
        return reason.code
    m = _RAW_CODE_RE.match(str(reason or ""))
    if m and len(m.group(1)) <= _RAW_CODE_MAX:
        return m.group(1)
    return default


def mark_model_reason(env: InboundEnvelope) -> InboundEnvelope:
    """Mark ``env`` as built from the model's own JSON: its failure_reason
    was written by the model. Returns ``env``."""
    env._reason_from_model = True
    return env


def lane_failure_code(env: InboundEnvelope) -> Optional[str]:
    """The code of a failure ``failed_envelope`` built (its output carries
    ``{"failure": {"code": ...}}`` and ``lane_guarded`` is False, which an
    envelope built from model JSON never has), else None."""
    if env.status != "failed" or env.lane_guarded is not False:
        return None
    failure = (env.output or {}).get("failure")
    code = failure.get("code") if isinstance(failure, dict) else None
    if isinstance(code, str) and _RAW_CODE_FULL.fullmatch(code) and len(code) <= _RAW_CODE_MAX:
        return code
    return None


_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def prompt_json(value: object, **kwargs) -> str:
    """``json.dumps`` for a block placed in a prompt.

    Lane off: exactly ``json.dumps(value, **kwargs)`` (Phase 1, ASCII
    escapes). Lane on: ``ensure_ascii=False``, so the backstop reads the
    text as written, with any lone surrogate replaced by U+FFFD (a lone
    surrogate cannot be encoded as UTF-8, and the request would fail)."""
    if current_lane() is None:
        return json.dumps(value, **kwargs)
    return _LONE_SURROGATE.sub("�", json.dumps(value, ensure_ascii=False, **kwargs))


def guard_envelope(env: InboundEnvelope, task: WrappedTask, ctx: LaneContext) -> InboundEnvelope:
    """The output guard (P2), applied to a raw envelope whatever its status.

    A resolved envelope passes when its serialised output is at most
    ``lane_max_output_chars`` long and breaks none of the backstop's copy
    rules (``backstop.copied_rule``) for the documents the task read (all of
    the run's documents when it read none directly): no run of
    ``lane_max_verbatim_chars`` characters shared, and no short document
    (24 to the limit, normalised) present whole. The check walks the decoded
    output values, keys included, never escaped JSON text. A passed envelope
    is stamped ``lane_guarded``. Anything else, including a failed or partial
    envelope, is replaced by a failure carrying a code and document ids only.
    The code is decided by source: a failure the model reported (the
    envelope was built from its JSON, ``mark_model_reason``) is always
    ``BRT-LANE-004``; only a failure the runtime wrote keeps its own code.

    ``by_reference`` (``not ctx.keeps_raw_local``): the guard does not run;
    the envelope comes back unchanged and unstamped.
    """
    if not ctx.keeps_raw_local:
        return env
    if env.status != "resolved":
        code = (
            RAW_FAILURE_DEFAULT if env._reason_from_model
            else raw_failure_code(env.failure_reason)
        )
        return failed_envelope(task, code, base=env)
    serialised = json.dumps(env.output, ensure_ascii=False)
    if len(serialised) > ctx.settings.max_output_chars:
        return failed_envelope(task, "BRT-LANE-004", base=env)
    out_norm = normalise(serialise(env.output))
    for doc in ctx.indexes(list(task.documents) or None):
        if copied_rule(doc, out_norm, ctx.settings.max_verbatim_chars):
            return failed_envelope(task, "BRT-LANE-004", base=env)
    return env.model_copy(update={"lane_guarded": True})
