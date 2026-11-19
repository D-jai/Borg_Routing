"""Export one finished run as a replay file (work order 4.5).

    python scripts/export_run.py <run_id> --db demo/runs/db/demo.db [--out demo/runs]
                                 [--docs-root demo/data] [--canary TEXT ...]

Writes ``<out>/job<N>_<run8>.json`` (``run_<run8>.json`` for a run not in the demo manifest)
holding the run, its envelopes (each with its routing decision), its events with times relative
to the run's start, its costs (the digest), the stage timeline the Ask page streams, and the
answer. Replay mode (``BORG_DEMO_MODE=replay``, ``src/api/replay.py``) plays these files.

The source database is never written: it is opened read-only and copied to a temporary file,
and the run view is built there by the same handler that serves ``GET /runs/{id}``, as a
non-admin viewer sees it. For a run with the lane on (``lane_mode`` not ``off``) the view is
the one a non-admin gets while the lane is on (``_redact_events`` in ``src/api/tasks.py``). The
view is kept whole, envelope payloads included (a local step's derived fields are shown by
design, planner ruling 37); the bodies of outbound requests (``outbound[].body``, admin only)
are removed whatever the run's lane mode, and so is the round-1 benchmark ratio
(``digest.direct_floor_ratio``).

Stripped from every string, also inside JSON held as text: keys (``[key]``) and absolute local
paths (``[local path]``); the ``instance_id`` machine marker is dropped. The script then runs the
strong check (``problems``) and REFUSES and writes nothing (exit 2) when the export would hold a
canary, whole or as its bare body, any case (BRT-RPL-010), any 24-character window of a run
document, any case and spacing, or documents it cannot read to check against (BRT-RPL-011), a
key or path left after stripping (BRT-RPL-012), an answer it cannot check (BRT-RPL-013: the
length check always applies), or a run that is missing or not finished (BRT-RPL-014).

The answer: the run database does not store the final answer. It is read from
``<db folder>/answers/<run_id>.md`` when the recorder saved one, else rebuilt from the run's
last compose reply with the engine's own row placement (``row_markers.place``). Either way its
length must equal the one the run's stored floor was priced on (``direct_floor_dollars``,
ruling 22), or the export is refused.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FORMAT = "borg_route.replay/1"
LABEL = "demo run"
DEFAULT_DB = ROOT / "demo" / "runs" / "db" / "demo.db"
DEFAULT_OUT = ROOT / "demo" / "runs"
DEFAULT_DOCS = ROOT / "demo" / "data"

# --- what is stripped and what is refused -------------------------------------------------

KEY_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,}|AIza[0-9A-Za-z_-]{20,}"
    r"|hf_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,})")
KEY_FIELDS = re.compile(r"(?i)^(api[_-]?key|.*_api_key|authorization|token|secret|password)$")
# Checked on decoded text (never on JSON held as text, where "rules:\n" would look like "s:\").
ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]*"                     # C:\x, C:/x
    r"|\\\\[A-Za-z0-9_.$-]+\\[^\s\"'<>|]*"                             # \\server\share
    r"|(?<![\w.:/-])/(?:home|Users|root|tmp|var|etc|opt|mnt|srv|usr|private|Volumes)/[^\s\"'<>]*")
CANARY_RE = re.compile(r"CANARY-[A-Za-z0-9]", re.IGNORECASE)
CANARY_TOKEN = re.compile(r"[A-Za-z0-9_-]*CANARY-([A-Za-z0-9_-]+)", re.IGNORECASE)
MIN_CANARY_BODY = 6
DROPPED_KEYS = {"instance_id"}
WINDOW = 24             # no 24-character run of a document may appear in an export (lead ruling)


class ExportRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _norm(text: str) -> str:
    return " ".join(text.split())


def _decoded(value: object):
    """Every string leaf, and the leaves of any JSON object or list held as text."""
    if isinstance(value, str):
        yield value
        s = value.strip()
        if s[:1] in ("{", "[") and s[-1:] in ("}", "]"):
            try:
                inner = json.loads(s)
            except ValueError:
                return
            yield from _decoded(inner)
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _decoded(v)
    elif isinstance(value, list):
        for v in value:
            yield from _decoded(v)


def _values(value: object):
    """The string values in exported order, keys left out and JSON held as text read as its
    own values: the sequence a canary split across two fields would run through."""
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in ("{", "[") and s[-1:] in ("}", "]"):
            try:
                inner = json.loads(s)
            except ValueError:
                inner = None
            if isinstance(inner, (dict, list)):
                yield from _values(inner)
                return
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _values(v)
    elif isinstance(value, list):
        for v in value:
            yield from _values(v)


def _scrub_text(text: str, stripped: dict) -> str:
    def key(_m):
        stripped["keys"] += 1
        return "[key]"

    def path(_m):
        stripped["paths"] += 1
        return "[local path]"

    return ABS_PATH_RE.sub(path, KEY_RE.sub(key, text))


def scrub(value: object, stripped: dict) -> object:
    """Keys and absolute paths replaced, ``instance_id`` dropped, at any depth; JSON held as
    text is decoded, scrubbed and written back only when something changed."""
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in ("{", "[") and s[-1:] in ("}", "]"):
            try:
                inner = json.loads(s)
            except ValueError:
                inner = None
            if isinstance(inner, (dict, list)):
                before = json.dumps(inner, sort_keys=True)
                cleaned = scrub(inner, stripped)
                return value if json.dumps(cleaned, sort_keys=True) == before else json.dumps(cleaned)
        return _scrub_text(value, stripped)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in DROPPED_KEYS:
                stripped["machine_ids"] += 1
                continue
            if KEY_FIELDS.match(str(k)) and isinstance(v, str) and v:
                stripped["keys"] += 1
                out[k] = "[key]"
                continue
            out[k] = scrub(v, stripped)
        return out
    if isinstance(value, list):
        return [scrub(v, stripped) for v in value]
    return value


def stripped(text: object) -> str:
    """Letters and digits only, lower case, after NFKC normalisation: how canaries are
    compared, so no separator, case or look-alike character hides one."""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", str(text or "")).lower())


def _body_parts(body: str) -> list:
    """The parts of a canary body of ``MIN_CANARY_BODY`` or more characters: its segments
    grouped into runs of the same kind (letters only, or holding a digit), each run stripped.
    ``7f3a91-ZEBRA-LANTERN`` gives ``7f3a91`` and ``zebralantern``."""
    segments = [s for s in re.split(r"[^A-Za-z0-9]+", unicodedata.normalize("NFKC", body)) if s]
    groups: list = []
    for seg in segments:
        kind = any(ch.isdigit() for ch in seg)
        if groups and groups[-1][0] == kind:
            groups[-1][1].append(seg)
        else:
            groups.append((kind, [seg]))
    return [p for p in (stripped("".join(g)) for _, g in groups) if len(p) >= MIN_CANARY_BODY]


def canary_forms(texts: list, extra: Optional[list] = None) -> list:
    """``[(stripped body, [stripped parts])]`` for every canary to refuse: each
    ``...CANARY-<body>`` token found in ``texts`` (the run's documents) or ``extra``, and each
    ``extra`` value without a prefix as a body of its own."""
    found: dict = {}

    def add(body: str) -> None:
        key = stripped(body)
        if len(key) >= MIN_CANARY_BODY:
            found.setdefault(key, _body_parts(body))

    for text in list(texts) + list(extra or []):
        for m in CANARY_TOKEN.finditer(unicodedata.normalize("NFKC", text or "")):
            add(m.group(1))
    for c in extra or []:
        if c and c.strip() and not CANARY_TOKEN.search(c):
            add(c)
    return sorted(found.items())


def has_canary(leaves: list, forms: list, values: Optional[list] = None) -> bool:
    """Lead ruling (review round 2): a leaf holds a stripped body or any stripped part of 6 or
    more characters, or the stripped concatenation of all leaves, in exported order, holds a
    stripped body (a canary split across two fields). The concatenation runs over ``values``
    (the string values without keys, ``_values``) when given, so a key between two halves
    does not hide the split; both orders are checked."""
    flat = [stripped(s) for s in leaves]
    whole = "".join(flat) + "\n" + "".join(stripped(s) for s in (values or []))
    for body, parts in forms:
        if body in whole:
            return True
        if any(body in s or any(p in s for p in parts) for s in flat):
            return True
    return False


def _windows(text: str) -> set:
    t = _norm(text).lower()
    return {t[i:i + WINDOW] for i in range(len(t) - WINDOW + 1)}


def problems(doc: dict, documents: dict, canaries: list) -> list:
    """``[(code, sentence)]`` for an export; empty when it may be written. ``documents`` maps
    each run document's name to its text. Sentences name a code and a document, never text.

    BRT-RPL-010: a ``CANARY-`` marker, or any canary of ``canary_forms`` (whole or bare body),
    anywhere, case-insensitively. BRT-RPL-011: any 24-character window of a document
    (whitespace normalised, case-insensitive) inside any exported string, JSON held as text
    included. BRT-RPL-012: a key or an absolute local path."""
    leaves = list(_decoded(doc))
    lowered = [_norm(s).lower() for s in leaves]
    joined = "\n".join(lowered)
    found = []
    forms = canary_forms(list(documents.values()), canaries)
    if CANARY_RE.search(joined) or has_canary(leaves, forms, list(_values(doc))):
        found.append(("BRT-RPL-010", "the export holds a canary"))
    owner: dict = {}
    for name, text in documents.items():
        for w in _windows(text):
            owner.setdefault(w, name)
    hit: list = []
    if owner:
        for s in lowered:
            for i in range(len(s) - WINDOW + 1):
                name = owner.get(s[i:i + WINDOW])
                if name is not None and name not in hit:
                    hit.append(name)
    for name in hit:
        found.append(("BRT-RPL-011", f"the export holds raw text of document {name}"))
    if any(KEY_RE.search(s) for s in leaves):
        found.append(("BRT-RPL-012", "a key is left after stripping"))
    if any(ABS_PATH_RE.search(s) for s in leaves):
        found.append(("BRT-RPL-012", "an absolute local path is left after stripping"))
    return found


# --- times ------------------------------------------------------------------------------

def _when(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def toronto_day(utc: datetime) -> str:
    """The Toronto calendar date (YYYY-MM-DD) of a UTC time stored by SQLite."""
    from src.api.replay import toronto_date  # noqa: PLC0415

    return toronto_date(utc).isoformat()


def _rel(value: object, start: datetime) -> Optional[int]:
    t = _when(value)
    return None if t is None else max(0, int(round((t - start).total_seconds() * 1000)))


# --- the answer -------------------------------------------------------------------------

def _decisions(con, run_id: str) -> list:
    out = []
    for (dj,) in con.execute("SELECT decision_json FROM logs WHERE run_id=? AND "
                             "event_type='orchestrator_decision' ORDER BY log_id", (run_id,)):
        try:
            d = json.loads(dj or "null")
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _plan_nodes(decisions: list) -> dict:
    nodes: dict = {}
    for d in decisions:
        parsed = d.get("parsed") or {}
        if d.get("mode") == "plan":
            batch = (parsed.get("plan") or {}).get("nodes") or []
        elif d.get("mode") == "re_plan":
            batch = parsed.get("next_nodes") or []
        else:
            continue
        for n in batch:
            if isinstance(n, dict) and n.get("id"):
                nodes.setdefault(n["id"], n)
    return nodes


def _scope(node: dict) -> Optional[str]:
    reads = node.get("inputs_ref") == "documents" or bool(node.get("documents")) or (
        (node.get("skill_args") or {}).get("items_ref") == "documents")
    return "raw" if reads else None


def _type_of(out: object) -> str:
    if isinstance(out, dict) and isinstance(out.get("results"), list) and (
            "success_count" in out or "failures" in out):
        return "apply_to_each"
    if isinstance(out, dict) and isinstance(out.get("result"), list):
        return "state_op"
    return "answerer"


_STAGE_HEAD = "Stage results so far (if any; relevant for re_plan / compose):\n"
_DOC_LINE = re.compile(r"^- (doc_\d+): (\S+) \((\w+), (\d+) bytes(?:, (\d+) parts)?\)$")


def _step_results(con, run_id: str, nodes: dict) -> tuple:
    """(results, documents) as compose saw them: the stage results of the run's compose request
    when the lane ledger holds one, else each plan step's stored envelope."""
    from uuid import uuid4

    from src.envelopes.schema import InboundEnvelope

    def env(status, output, node):
        e = InboundEnvelope(envelope_id=uuid4(), subtask_id=uuid4(), status=status,
                            output=output if isinstance(output, dict) else {"value": output},
                            **({"failure_reason": "stored failure"} if status == "failed" else {}))
        e._node_type = node.get("type") or _type_of(output)
        e._data_scope = _scope(node)
        if status == "resolved" and e._data_scope == "raw":
            e.lane_guarded = True          # it was shown to compose, so it passed the guard
        return e

    results, docs = {}, []
    row = con.execute("SELECT body FROM outbound_requests WHERE run_id=? AND "
                      "purpose='orchestrator:compose' ORDER BY started_at, rowid LIMIT 1",
                      (run_id,)).fetchone()
    if row and row[0]:
        system = json.loads(row[0])["messages"][0]["content"]
        a = system.index(_STAGE_HEAD) + len(_STAGE_HEAD)
        b = system.index("\n\nRaw tool_results", a)
        stage = json.loads(system[a:b]) if system[a:b].strip() != "(none)" else {}
        for line in system.splitlines():
            m = _DOC_LINE.match(line.strip())
            if m:
                parts = int(m.group(5) or 1)
                ids = [m.group(1)] if parts == 1 else [f"{m.group(1)}#{k}" for k in range(1, parts + 1)]
                docs.append({"id": m.group(1), "name": m.group(2), "parts": ids})
        for sid, out in stage.items():
            failed = isinstance(out, dict) and set(out) == {"failure"}
            results[sid] = env("failed" if failed else "resolved", out, nodes.get(sid) or {})
        return results, docs
    for ob, ib in con.execute("SELECT outbound_payload, inbound_payload FROM envelopes "
                              "WHERE run_id=? ORDER BY created_at", (run_id,)):
        sid = (json.loads(ob or "{}") or {}).get("node_id")
        inbound = json.loads(ib or "{}") or {}
        if sid is None or sid not in nodes or not inbound:
            continue
        results[sid] = env(inbound.get("status") or "resolved", inbound.get("output") or {}, nodes[sid])
    return results, docs


def rebuilt_answers(con, run_id: str, lane_mode: str, instruction: str) -> list:
    """The final answer rebuilt from the last compose reply, once per manifest form the row
    labels could have used ("names", then "ids"); [] when the run has no compose reply."""
    from src.lane.context import LaneContext, lane_scope
    from src.lane.documents import Document, DocPart, LoadedDocuments
    from src.lane.settings import LaneSettings
    from src.orchestrator import engine, row_markers

    decisions = _decisions(con, run_id)
    replies = [d.get("parsed") for d in decisions if d.get("mode") == "compose"]
    if not replies or not isinstance(replies[-1], dict):
        return []
    reply = replies[-1]
    results, docs = _step_results(con, run_id, _plan_nodes(decisions))
    out = []
    for manifest in ("names", "ids"):
        lane = None
        if lane_mode not in (None, "off") and docs:
            built = [Document(d["id"], d["name"], "txt", 1, "placeholder",
                              [DocPart(p, d["id"], f"placeholder {p}") for p in d["parts"]])
                     for d in docs]
            lane = LaneContext(settings=LaneSettings(mode=lane_mode, manifest=manifest),
                               docs=LoadedDocuments(built, []), db_path=":memory:")
        with lane_scope(lane):
            ctx = engine._RowContext.for_run(results, lane, instruction, None, None)
            text, field, _ = engine._compose_text(reply)
            if not text:
                return out
            resolver = row_markers.Resolver(ctx.steps, ctx.outputs)
            value, placed = row_markers.place(reply[field], ctx.steps, resolver=resolver)
            if placed:
                text, _, _ = engine._compose_text({**reply, field: value})
        if text not in out:
            out.append(text)
    return out


def floor_answer_chars(digest: Optional[dict], instruction: str, doc_chars: int, matrix) -> Optional[float]:
    """The answer length the run's stored floor was priced on, or None when it cannot be known."""
    from src.registry.cost import CHARS_PER_TOKEN_FLOOR

    if not digest or digest.get("direct_floor_dollars") is None or matrix is None:
        return None
    row = matrix.get(digest.get("direct_floor_model"))
    pricing = getattr(row, "pricing", None)
    if pricing is None or not pricing.output_per_token:
        return None
    inputs = ((len(instruction) + doc_chars) / CHARS_PER_TOKEN_FLOOR) * pricing.input_per_token
    return (digest["direct_floor_dollars"] - inputs) * CHARS_PER_TOKEN_FLOOR / pricing.output_per_token


# --- the run view ---------------------------------------------------------------------------

class _State:
    def __init__(self, matrix):
        self.matrix = matrix


class _App:
    def __init__(self, matrix):
        self.state = _State(matrix)


class _Request:
    """Just enough of a request for ``get_run_state``: the matrix it judges locality with."""

    def __init__(self, matrix):
        self.app = _App(matrix)


async def run_view(run_id: str, db_path: str, matrix, lane_on: bool) -> dict:
    """``GET /runs/{id}`` as a non-admin sees it (with the lane on when ``lane_on``)."""
    from src.api import tasks

    saved = (tasks.DB_PATH, tasks.lane_admin_allowed, tasks._lane_is_on)

    async def lane_is_on() -> bool:
        return lane_on

    tasks.DB_PATH = db_path
    tasks.lane_admin_allowed = lambda request=None: False
    tasks._lane_is_on = lane_is_on
    try:
        response = await tasks.get_run_state(run_id, request=_Request(matrix))
    finally:
        tasks.DB_PATH, tasks.lane_admin_allowed, tasks._lane_is_on = saved
    return json.loads(response.body)


def stage_timeline(events: list) -> list:
    """The stage events the Ask page's stream shows, as ``_poll_stage_events`` builds them,
    from the (already redacted) events."""
    out = []
    for ev in events:
        if ev.get("event_type") != "orchestrator_decision":
            continue
        try:
            decision = json.loads(ev.get("decision_json") or "null") or {}
        except (TypeError, ValueError):
            decision = {}
        if not isinstance(decision, dict):
            decision = {}
        stage = decision.get("mode") or decision.get("code") or "orchestrator"
        detail = decision.get("msg") or decision.get("rationale") or ev.get("model_id") or ""
        out.append({"t_ms": ev.get("t_ms") or 0, "stage": str(stage), "detail": str(detail)[:200]})
    return out


def _models(view: dict, matrix, rules) -> list:
    from src.api.locality_view import model_row

    ids = set()
    for env in view.get("envelopes", []) + view.get("child_envelopes", []):
        if env.get("model_id"):
            ids.add(env["model_id"])
        for c in ((env.get("routing") or {}).get("candidates") or []):
            if isinstance(c, dict) and c.get("model_id"):
                ids.add(c["model_id"])
    ids.update(ev["model_id"] for ev in view.get("events", []) if ev.get("model_id"))
    rows = []
    for mid in sorted(ids):
        m = matrix.get(mid) if matrix is not None else None
        if m is None:
            continue
        try:
            is_local = bool(model_row(m, rules).get("is_local"))
        except Exception:
            is_local = bool(getattr(m, "local", False))
        rows.append({"model_id": mid, "is_local": is_local})
    return rows


def _manifest_entry(db_path: Path, run_id: str) -> dict:
    for candidate in (db_path.parent.parent / "manifest.json", DEFAULT_OUT / "manifest.json"):
        try:
            for entry in json.loads(candidate.read_text(encoding="utf-8")):
                if entry.get("run_id") == run_id:
                    return entry
        except (OSError, ValueError):
            continue
    return {}


def _read_documents(run: dict, docs_root: Optional[str]) -> dict:
    """``{name: text}`` for every document the run read; refuses when one cannot be read."""
    try:
        manifest = json.loads(run.get("documents") or "null") or []
    except (TypeError, ValueError):
        manifest = []
    texts = {}
    for d in manifest if isinstance(manifest, list) else []:
        name = d.get("name") if isinstance(d, dict) else None
        if not name:
            continue
        path = Path(docs_root or DEFAULT_DOCS) / name
        try:
            texts[name] = path.read_text(encoding="utf-8")
        except OSError:
            raise ExportRefused("BRT-RPL-011", f"document {d.get('id') or name} could not be read, "
                                               "so the export cannot be checked for its text; "
                                               "pass --docs-root")
    return texts


async def build_export(run_id: str, db_path: str, matrix, docs_root: Optional[str] = None,
                       canaries: Optional[list] = None, answer_file: Optional[str] = None) -> dict:
    """The export for one run; raises ExportRefused (nothing is written by this function)."""
    from src.api.locality_view import locality_rules

    src = Path(db_path)
    if not src.exists():
        raise ExportRefused("BRT-RPL-014", "the run database was not found")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "copy.db"
        ro = sqlite3.connect(f"file:{src.resolve().as_posix()}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(copy))
            ro.backup(dst)
            dst.close()
        finally:
            ro.close()
        con = sqlite3.connect(str(copy))
        con.row_factory = sqlite3.Row
        try:
            row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise ExportRefused("BRT-RPL-014", f"run {run_id[:8]} is not in the database")
            run = dict(row)
            if run["status"] not in ("completed", "failed") or not run.get("completed_at"):
                raise ExportRefused("BRT-RPL-014", f"run {run_id[:8]} has not finished")
            lane_mode = run.get("lane_mode") or "off"
            documents = _read_documents(run, docs_root)
            found_canaries = list(canaries or [])
            view = await run_view(run_id, str(copy), matrix, lane_on=lane_mode != "off")
            view["outbound"] = [dict(o, body=None) for o in view.get("outbound") or []]
            rules = await locality_rules(str(copy))

            answer, source = None, None
            stored = Path(answer_file) if answer_file else src.parent / "answers" / f"{run_id}.md"
            if stored.exists():
                answer, source = stored.read_bytes().decode("utf-8"), "stored"
            try:
                doc_chars = sum(int(d.get("chars") or 0) for d in json.loads(run.get("documents") or "[]"))
            except (TypeError, ValueError, AttributeError):
                doc_chars = 0
            want = floor_answer_chars(view.get("digest"), run["user_instruction"], doc_chars, matrix)
            if want is None:
                # Lead ruling: the length check always applies; nothing is exported unchecked.
                raise ExportRefused("BRT-RPL-013", "the run has no priced floor, so its answer "
                                                   "cannot be checked")
            if answer is None:
                candidates = rebuilt_answers(con, run_id, lane_mode, run["user_instruction"])
                answer = next((c for c in candidates if abs(len(c) - want) < 0.5), None)
                source = "rebuilt"
                if answer is None:
                    raise ExportRefused("BRT-RPL-013", "the answer could not be rebuilt exactly "
                                                       "(its length does not match the stored floor)")
                check = "matches the stored floor"
            elif abs(len(answer) - want) < 0.5:
                check = "matches the stored floor"
            else:
                raise ExportRefused("BRT-RPL-013", "the stored answer's length does not match the "
                                                   "stored floor")
        finally:
            con.close()

    start = _when(run["created_at"])
    # Lead ruling (review of 4.5, item 4): no figure from the benchmark rides along; the
    # receipt's ratio sentence is built from this field.
    if isinstance(view.get("digest"), dict):
        view["digest"]["direct_floor_ratio"] = None
    for ev in view.get("events", []):
        ev["t_ms"] = _rel(ev.get("created_at"), start)
    for env in view.get("envelopes", []) + view.get("child_envelopes", []):
        env["t_ms"] = _rel(env.get("created_at"), start)
        env["t_done_ms"] = _rel(env.get("completed_at"), start)
    for o in view.get("outbound", []):
        o["t_ms"] = _rel(o.get("started_at"), start)
        o["t_done_ms"] = _rel(o.get("finished_at"), start)
    entry = _manifest_entry(src, run_id)
    digest = view.get("digest") or {}
    doc = {
        "format": FORMAT,
        "label": LABEL,
        "job": entry.get("job"),
        "title": entry.get("title"),
        "run_id": run_id,
        "recorded_on": toronto_day(start),
        "lane_mode": lane_mode,
        "redaction": "lane_viewer" if lane_mode != "off" else "viewer",
        "prompt": run["user_instruction"],
        "view": view,
        "timeline": {"stages": stage_timeline(view.get("events", [])),
                     "end_ms": _rel(run["completed_at"], start)},
        "answer": answer,
        "answer_source": source,
        "answer_check": check,
        "models": _models(view, matrix, rules),
        "totals": {"cost_dollars": digest.get("cost_dollars"),
                   "tokens_input": digest.get("tokens_input"),
                   "tokens_output": digest.get("tokens_output"),
                   "envelopes": len(view.get("envelopes", [])),
                   "events": len(view.get("events", []))},
    }
    stripped = {"keys": 0, "paths": 0, "machine_ids": 0}
    doc = scrub(doc, stripped)
    doc["stripped"] = stripped
    found = problems(doc, documents, found_canaries)
    if found:
        raise ExportRefused(found[0][0], "; ".join(f"{c} {s}" for c, s in found))
    return doc


def load_matrix():
    """The bench's proof pool the demo runs were recorded with (scripts/record_demo_runs.py)."""
    spec = importlib.util.spec_from_file_location("record_demo_runs", ROOT / "scripts" / "record_demo_runs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.load_matrix()


def out_name(doc: dict) -> str:
    return (f"job{doc['job']}_{doc['run_id'][:8]}.json" if doc.get("job")
            else f"run_{doc['run_id'][:8]}.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export one finished run for replay mode (work order 4.5).")
    ap.add_argument("run_id")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="the run database (opened read-only)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="folder for the export file")
    ap.add_argument("--docs-root", default=None, help="folder the run's documents are named from")
    ap.add_argument("--canary", action="append", default=[], help="a string the export must not hold")
    ap.add_argument("--answer-file", default=None, help="the run's final answer, when saved elsewhere")
    args = ap.parse_args(argv)
    try:
        doc = asyncio.run(build_export(args.run_id, args.db, load_matrix(), docs_root=args.docs_root,
                                       canaries=args.canary, answer_file=args.answer_file))
    except ExportRefused as exc:
        print(f"REFUSED, nothing written: {exc}")
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / out_name(doc)
    text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    s = doc["stripped"]
    print(f"[{LABEL}] run {doc['run_id'][:8]} -> {path.name} ({len(text.encode('utf-8')):,} bytes); "
          f"redaction {doc['redaction']}; answer {doc['answer_source']} ({doc['answer_check']}); "
          f"stripped {s['keys']} keys, {s['paths']} absolute paths, {s['machine_ids']} machine ids; "
          f"request bodies not exported")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
