"""Work order 2.3: the on-premise lane, as tests.

The promise (docs/design/on_premise_lane.md section 1): "Raw document text is
sent only to models on your own machines. Everything sent to a cloud model is
listed, and you can read it."

The real engine, executor and router run against the stand-in model servers
(``stub_model_server.py``): one local model at 127.0.0.1 and three cloud
models. The documents hold canary strings the task's output shape never asks
for; a canary in any request to a non-local address is a leak.

(a)-(f) are the six assertions of BUILD.md 2.3; the rest are the extra tests
of the design note, section 12.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import (
    StubModels,
    drain_log_writes,
    lane_matrix,
    three_tier_matrix,
)

# -- the documents ------------------------------------------------------------

CANARY_ACCOUNT = "ACCT-CANARY-7731-9902"
CANARY_PHRASE = "the vault code is orchid fourteen seventy one"
CANARY_BIRCH = "BIRCH-CANARY-SECRET-5512"
CANARIES = (CANARY_ACCOUNT, CANARY_PHRASE, CANARY_BIRCH)

ACME_TEXT = (
    "Client note for Acme Holdings, prepared by the relationship desk.\n"
    f"Account {CANARY_ACCOUNT}. The client said {CANARY_PHRASE}, and asked us "
    "to keep it on file. Exposure grew in the third quarter after two late "
    "payments on the equipment lease, and the treasurer has resigned. We rate "
    "the client high risk until the new treasurer is in place and the lease "
    "is current again. Next review in January."
)
BIRCH_TEXT = (
    "# Birch Logistics\n\n"
    f"Reference {CANARY_BIRCH}.\n\n"
    "Birch pays on time, has no open disputes and renewed the warehouse "
    "contract for three years. The owners plan a second depot next spring "
    "funded from retained earnings. Low risk; review in twelve months as usual."
)

OPERATOR = "Read the attached client notes and tell me which clients are high risk."

EXTRACT = "From this client note, give the client name and the risk level."
SUMMARY = "List the clients rated high risk and say why in one line each."

PLAN_RAW = [
    {"type": "apply_to_each", "id": "n1", "inputs_ref": "documents",
     "instruction": EXTRACT, "task_type": "extraction", "tier": "light",
     "output_schema": {"client": "string", "risk": "string"}},
    {"type": "answerer", "id": "n2", "depends_on": ["n1"], "instruction": SUMMARY,
     "task_type": "reasoning", "tier": "mid", "output_schema": {"result": "string"}},
]


def _local_extract(req):
    """What a well-behaved local model returns: short fields, no canary."""
    if CANARY_ACCOUNT in req.system:
        return {"client": "Acme Holdings", "risk": "high"}
    return {"client": "Birch Logistics", "risk": "low"}


def _answers(req):
    if req.is_local:
        return _local_extract(req)
    return {"result": f"done by {req.model}"}


def _leaks(req) -> list[str]:
    text = json.dumps(req.body)
    return [c for c in CANARIES if c in text]


# -- fixtures -------------------------------------------------------------------

_LANE_ENV = (
    "BORG_LANE_MODE", "BORG_LANE_MANIFEST", "BORG_LOCAL_HOSTS",
    "BORG_LANE_LOCAL_ALLOWLIST", "BORG_LANE_MAX_OUTPUT_CHARS",
    "BORG_LANE_MAX_VERBATIM_CHARS", "BORG_LANE_MAX_NGRAM_SHARE", "BORG_LANE_PART_CHARS",
    "LEAF_QUALITY_LIGHT", "LEAF_QUALITY_MID", "LEAF_QUALITY_HEAVY", "BASELINE_MODEL_ID",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
)


@pytest.fixture
async def lane(tmp_path, monkeypatch):
    """A docs root with two notes, a temp database and a clean lane env."""
    from src.db.schema import create_all

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "acme_note.txt").write_text(ACME_TEXT, encoding="utf-8")
    (docs / "birch_note.md").write_text(BIRCH_TEXT, encoding="utf-8")
    (docs / "ledger.pdf").write_bytes(b"%PDF-1.4 not read")

    db_path = str(tmp_path / "lane.db")
    await create_all(db_path)

    for name in _LANE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BORG_DOCS_ROOT", str(docs))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-lane")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("TIME_COST_PER_SEC", "0.0001")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    return {"docs": docs, "db": db_path, "tmp": tmp_path, "mp": monkeypatch}


async def _run(lane, mode, matrix, plan=PLAN_RAW, documents={"folder": "."},
               instruction=OPERATOR, stub_cls=StubModels, **stub_kwargs):
    lane["mp"].setenv("BORG_LANE_MODE", mode)
    lane["mp"].setattr(engine, "_shared_matrix", matrix)
    stub_kwargs.setdefault("answer_for", _answers)
    waiting = [n for n in plan if n.get("depends_on")]
    if waiting and "replan_for" not in stub_kwargs:
        # The engine runs one wave, then asks re_plan: continue once with the
        # steps that waited on the first wave, then compose.
        sent = {"done": False}

        def replan_for(req):
            if sent["done"]:
                return {"action": "compose", "rationale": "all steps done"}
            sent["done"] = True
            return {"action": "continue", "next_nodes": waiting, "rationale": "wave 2"}

        stub_kwargs["replan_for"] = replan_for
    stub = stub_cls(plan_nodes=plan, **stub_kwargs).install(lane["mp"])
    result = await run_pipeline(instruction, db_path=lane["db"], documents=documents)
    await drain_log_writes()
    return stub, result


def _ledger(db_path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM outbound_requests ORDER BY started_at, rowid"
        ).fetchall()]


def _error_codes(db_path, run_id) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT decision_json FROM logs WHERE event_type='error'"
        ).fetchall()
    codes = []
    for (raw,) in rows:
        try:
            codes.append(json.loads(raw).get("code"))
        except Exception:
            pass
    return codes


def _envelope_output(db_path, run_id, node_type) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT inbound_payload FROM envelopes WHERE run_id=? AND type=?",
            (run_id, node_type),
        ).fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


# ============================================================================
# The six assertions of work order 2.3
# ============================================================================

async def test_a_raw_local_no_canary_reaches_a_cloud_model(lane):
    stub, result = await _run(lane, "raw_local", lane_matrix())
    assert result["status"] == "completed", result.get("error")

    cloud = [r for r in stub.requests if not r.is_local]
    local = [r for r in stub.requests if r.is_local]
    assert cloud, "the planner should still be the cloud top model"
    for req in cloud:
        assert not _leaks(req), f"canary sent to {req.url} ({req.kind}): {_leaks(req)}"

    # The documents were read, locally: each raw item request carries its text.
    assert len(local) == 2
    assert any(CANARY_ACCOUNT in r.system for r in local)
    assert any(CANARY_BIRCH in r.system for r in local)

    # The planner got a manifest: names, no text.
    plan_req = stub.of_kind("plan")[0]
    assert not plan_req.is_local
    assert "acme_note.txt" in plan_req.system and "birch_note.md" in plan_req.system
    assert "ledger.pdf" in plan_req.system  # listed as unsupported, never read
    assert "relationship desk" not in plan_req.system
    assert "warehouse contract" not in plan_req.system

    # The derived step (reads only guarded outputs) may go to the cloud.
    summary = stub.step_request(SUMMARY)
    assert not summary.is_local
    assert "Acme Holdings" in summary.system


async def test_b_raw_local_without_a_local_model_fails_and_sends_nothing(lane):
    stub, result = await _run(lane, "raw_local", three_tier_matrix())
    assert stub.of_kind("answerer") == [], "a raw step went to a cloud model"
    for req in stub.requests:
        assert not _leaks(req)
    assert "BRT-LANE-002" in _error_codes(lane["db"], result["run_id"])
    # re_plan sees the failure as code plus id, nothing more.
    replan = stub.of_kind("re_plan")[0]
    assert "BRT-LANE-002" in replan.system


async def test_b_local_model_below_the_floor_never_falls_back_to_cloud(lane):
    stub, result = await _run(lane, "raw_local", lane_matrix(local_score=5.0))
    assert stub.of_kind("answerer") == []
    assert "BRT-LANE-002" in _error_codes(lane["db"], result["run_id"])
    assert "BRT-ORC-019" not in _error_codes(lane["db"], result["run_id"])


async def test_c_all_local_sends_nothing_off_the_machine(lane):
    plan = [dict(PLAN_RAW[0]), dict(PLAN_RAW[1], tier="light")]
    stub, result = await _run(lane, "all_local", lane_matrix(), plan=plan)
    assert result["status"] == "completed", result.get("error")
    assert stub.requests, "no request was made at all"
    for req in stub.requests:
        assert req.is_local, f"{req.kind} went to {req.url}"
    kinds = {r.kind for r in stub.requests}
    assert {"plan", "answerer", "re_plan", "compose"} <= kinds
    assert all(row["left_machine"] == 0 for row in _ledger(lane["db"]))


async def test_d_off_behaves_as_phase_one(lane):
    from tests.e2e.test_claim_routing import CODE, EXTRACT as C_EXTRACT, PLAN, RECOMMEND

    stub, result = await _run(lane, "off", three_tier_matrix(), plan=PLAN,
                              documents=None, answer_for=None)
    assert result["status"] == "completed", result.get("error")
    assert stub.step_request(C_EXTRACT).model == "stub/cheap"
    assert stub.step_request(CODE).model == "stub/mid"
    assert stub.step_request(RECOMMEND).model == "stub/top"
    assert _ledger(lane["db"]) == [], "the ledger must not run with the lane off"
    for req in stub.requests:
        assert "Documents (by reference)" not in req.system


async def test_d_off_refuses_documents(lane):
    stub, result = await _run(lane, "off", lane_matrix())
    assert result["status"] == "failed"
    assert "BRT-LANE-005" in result["error"]
    assert stub.requests == []


def _outside_target(lane, form: str) -> str:
    """A request string of the given form whose target really exists outside
    the root, so a refusal can only come from the path check (not from
    "not found"). Skips a form that cannot be made to exist on this OS."""
    tmp = lane["tmp"]
    outside = tmp / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (lane["docs"] / "sub").mkdir(exist_ok=True)
    win = sys.platform == "win32"
    no_drive = str(outside)[len(outside.drive):]       # "\Users\...\outside.txt"
    if form == "dotdot":
        return "../outside.txt"
    if form == "sub_dotdot":
        return "sub/../../outside.txt"
    if form == "drive_relative":
        # "C:outside.txt" means "outside.txt in the current folder of drive C";
        # run from the temp folder (outside the root) so it names a real file.
        lane["mp"].chdir(tmp)
        return f"{outside.drive}outside.txt" if win else "C:outside.txt"
    if form == "root_relative_backslash":
        if not win:
            pytest.skip("'\\x' is an ordinary file name, not root-relative, off Windows")
        lane["mp"].chdir(tmp)
        return no_drive
    if form == "root_relative_slash":
        if not win:
            return str(outside)          # "/tmp/.../outside.txt" is root-relative here
        lane["mp"].chdir(tmp)
        return no_drive.replace("\\", "/")
    if form == "unc":
        if not win:
            pytest.skip("UNC paths exist only on Windows")
        unc = "\\\\localhost\\" + outside.drive.rstrip(":") + "$" + no_drive
        if not os.path.exists(unc):
            pytest.skip("the administrative share is not reachable on this machine")
        return unc
    if form == "device_question":
        if not win:
            pytest.skip("device prefixes exist only on Windows")
        return "\\\\?\\" + str(outside)
    if form == "device_dot":
        if not win:
            pytest.skip("device prefixes exist only on Windows")
        return "\\\\.\\" + str(outside)
    raise AssertionError(form)


@pytest.mark.parametrize("form", [
    "dotdot", "sub_dotdot", "drive_relative", "root_relative_backslash",
    "root_relative_slash", "unc", "device_question", "device_dot",
])
async def test_e_paths_outside_the_root_are_refused(lane, form):
    from src.lane.documents import load_documents
    from src.lane.errors import LaneError

    bad = _outside_target(lane, form)
    if form in ("dotdot", "sub_dotdot"):
        assert (lane["docs"] / bad).resolve().is_file(), "the target must exist"
    else:
        assert os.path.isfile(bad), f"the target of {bad!r} must exist"
    with pytest.raises(LaneError) as info:
        load_documents({"files": [bad]})
    assert info.value.code == "BRT-LANE-001"
    message = str(info.value)
    assert "not found" not in message, "refused for the wrong reason"
    assert "is not a relative path" in message or "resolves outside" in message, message
    assert str(lane["tmp"]) not in message.replace(bad, "")


async def test_e_an_absolute_path_is_refused_even_inside_the_root(lane):
    from src.lane.documents import load_documents
    from src.lane.errors import LaneError

    with pytest.raises(LaneError) as info:
        load_documents({"files": [str(lane["docs"] / "acme_note.txt")]})
    assert info.value.code == "BRT-LANE-001"


async def test_e_a_mixed_case_path_inside_the_root_is_accepted(lane):
    from src.lane.documents import load_documents

    if sys.platform != "win32":
        pytest.skip("case-insensitive paths are a Windows behaviour")
    loaded = load_documents({"files": ["ACME_Note.TXT"]})
    assert [d.doc_id for d in loaded.documents] == ["doc_001"]


async def test_e_links_pointing_out_of_the_root_are_refused(lane):
    from src.lane.documents import load_documents
    from src.lane.errors import LaneError

    outside_dir = lane["tmp"] / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("outside secret", encoding="utf-8")
    made = []
    try:
        os.symlink(outside_dir / "secret.txt", lane["docs"] / "link.txt")
        made.append("link.txt")
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(lane["docs"] / "junction"), str(outside_dir)],
            capture_output=True,
        )
        if done.returncode == 0:
            made.append("junction/secret.txt")
    if not made:
        pytest.skip("this machine cannot make a symlink or a junction")
    for rel in made:
        with pytest.raises(LaneError) as info:
            load_documents({"files": [rel]})
        assert info.value.code == "BRT-LANE-001"
    # A folder listing that meets the link is refused too.
    with pytest.raises(LaneError):
        load_documents({"folder": "."})


async def test_e_a_file_swapped_after_the_check_is_refused(lane):
    from src.lane.documents import check_path, docs_root, read_verified
    from src.lane.errors import LaneError

    root = docs_root()
    resolved, stat = check_path(root, "acme_note.txt")
    replacement = lane["docs"] / "other.txt"
    replacement.write_text("swapped in", encoding="utf-8")
    os.replace(replacement, lane["docs"] / "acme_note.txt")
    with pytest.raises(LaneError) as info:
        read_verified(resolved, stat, "acme_note.txt")
    assert info.value.code == "BRT-LANE-001"


@pytest.mark.parametrize("root_kind", ["dot", "app_dir", "unset", "empty"])
async def test_e_an_unsafe_root_is_refused(lane, root_kind):
    from src.lane.documents import docs_root
    from src.lane.errors import LaneError

    app_dir = Path(__file__).resolve().parents[2]
    if root_kind == "dot":
        lane["mp"].setenv("BORG_DOCS_ROOT", ".")
    elif root_kind == "app_dir":
        lane["mp"].setenv("BORG_DOCS_ROOT", str(app_dir))
    elif root_kind == "empty":
        lane["mp"].setenv("BORG_DOCS_ROOT", "")
    else:
        lane["mp"].delenv("BORG_DOCS_ROOT")
    with pytest.raises(LaneError) as info:
        docs_root()
    assert info.value.code == "BRT-LANE-001"


async def test_e_a_root_that_contains_the_app_directory_is_refused(lane):
    from src.lane.documents import docs_root
    from src.lane.errors import LaneError

    app_dir = Path(__file__).resolve().parents[2]
    lane["mp"].setenv("BORG_DOCS_ROOT", str(app_dir.parent))
    with pytest.raises(LaneError):
        docs_root()


async def test_f_every_call_is_in_the_ledger_with_the_right_left_machine(lane):
    stub, result = await _run(lane, "raw_local", lane_matrix())
    rows = _ledger(lane["db"])
    assert len(rows) == len(stub.requests)
    unmatched = list(rows)
    for req in stub.requests:
        hit = next(
            (row for row in unmatched
             if json.loads(row["body"]) == req.body and row["url"] == req.url),
            None,
        )
        assert hit is not None, f"no ledger row for the {req.kind} call to {req.url}"
        assert hit["left_machine"] == (0 if req.is_local else 1)
        assert hit["status"] == "sent"
        unmatched.remove(hit)


# ============================================================================
# Extra tests from the design note, section 12
# ============================================================================

# -- routing --------------------------------------------------------------------

async def test_a_cloud_recommended_model_on_a_raw_step_is_served_locally(lane):
    plan = [dict(PLAN_RAW[0], recommended_model="openrouter/stub/top"), PLAN_RAW[1]]
    stub, result = await _run(lane, "raw_local", lane_matrix(), plan=plan)
    items = [r for r in stub.of_kind("answerer") if EXTRACT in r.system]
    assert len(items) == 2 and all(r.is_local for r in items)


async def test_all_local_default_and_degraded_orchestrator_are_local(lane):
    # The local model (7.0) is below the 9.0 orchestrator bar: the degraded
    # 5.1 path runs, and it must not pick the cloud models it would see.
    plan = [dict(PLAN_RAW[0]), dict(PLAN_RAW[1], tier="light")]
    stub, result = await _run(lane, "all_local", lane_matrix(), plan=plan)
    assert {r.model for r in stub.requests} == {"stub/local"}


async def test_all_local_with_no_local_model_fails_the_run(lane):
    stub, result = await _run(lane, "all_local", three_tier_matrix(), documents=None)
    assert result["status"] == "failed"
    assert "BRT-LANE-002" in result["error"]
    assert stub.requests == []


# -- the call guard -----------------------------------------------------------------

async def _raw_task(lane, model_name, endpoint):
    from src.envelopes.schema import WrappedTask

    return WrappedTask(
        envelope_id=uuid4(), run_id=uuid4(), subtask_id=uuid4(), type="answerer",
        current_depth=1, max_depth=5, level_path="1a",
        model_endpoint=endpoint, model_name=model_name,
        instruction=EXTRACT, output_schema={"client": "string"},
        data_scope="raw", documents=["doc_001"],
    )


@pytest.mark.parametrize("model_name,endpoint", [
    ("openrouter/stub/top", "https://openrouter.ai/api"),
    ("mystery-model", "http://10.0.0.5:1234"),     # unknown id, non-local address
])
async def test_guard_blocks_a_raw_task_headed_off_the_machine(lane, model_name, endpoint):
    from src.agents.executor import execute
    from src.lane.context import lane_scope, prepare_lane

    lane["mp"].setenv("BORG_LANE_MODE", "raw_local")
    lane["mp"].setattr(engine, "_shared_matrix", lane_matrix())
    stub = StubModels(plan_nodes=[]).install(lane["mp"])
    ctx = await prepare_lane({"folder": "."}, lane["db"])
    task = await _raw_task(lane, model_name, endpoint)
    with lane_scope(ctx):
        with pytest.raises(ValueError, match="BRT-LANE-003"):
            await execute(task, db_path=lane["db"])
    assert stub.requests == []


# -- locality ----------------------------------------------------------------------

async def test_an_ollama_cloud_tag_is_not_a_local_model(lane):
    from src.registry.models import ModelInfo

    matrix = three_tier_matrix()
    matrix.upsert(ModelInfo(
        model_id="ollama/gpt-oss:120b-cloud", source="ollama",
        endpoint="http://127.0.0.1:11434", context_length=128_000,
        task_scores={"extraction": 9.9}, checked=True,
    ))
    stub, result = await _run(lane, "raw_local", matrix)
    assert stub.of_kind("answerer") == []
    assert "BRT-LANE-002" in _error_codes(lane["db"], result["run_id"])


async def test_an_allow_list_refuses_an_unlisted_local_model(lane):
    lane["mp"].setenv("BORG_LANE_LOCAL_ALLOWLIST", "ollama/some-other-model")
    stub, result = await _run(lane, "raw_local", lane_matrix())
    assert stub.of_kind("answerer") == []
    lane["mp"].setenv("BORG_LANE_LOCAL_ALLOWLIST", "127.0.0.1:11434")
    stub, result = await _run(lane, "raw_local", lane_matrix())
    assert len(stub.of_kind("answerer")) >= 2


async def test_a_local_call_ignores_a_proxy_variable(lane):
    built: list[dict] = []
    real_init = httpx.AsyncClient.__init__

    def spy(self, *args, **kwargs):
        built.append(kwargs)
        real_init(self, *args, **kwargs)

    lane["mp"].setenv("HTTP_PROXY", "http://proxy.example.test:3128")
    lane["mp"].setattr(httpx.AsyncClient, "__init__", spy)
    stub, result = await _run(lane, "all_local", lane_matrix(),
                              plan=[dict(PLAN_RAW[0])])
    assert built and all(kw.get("trust_env") is False for kw in built)


# -- taint --------------------------------------------------------------------------

def _copying_local(req):
    """A local model that copies the whole note into its answer."""
    if req.is_local and EXTRACT in req.system:
        text = ACME_TEXT if CANARY_ACCOUNT in req.system else BIRCH_TEXT
        return {"client": text[:300], "risk": "high"}
    return _answers(req)


def _route_of(db_path, node_id) -> dict:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT decision_json FROM logs WHERE event_type='orchestrator_decision'"
        ).fetchall()
    for (raw,) in rows:
        decision = json.loads(raw or "{}")
        if decision.get("mode") == "route" and decision.get("node_id") == node_id:
            return decision
    return {}


async def test_output_guard_withholds_copied_text_and_taints_the_reader(lane):
    # Local at 8.0 clears the mid floor, so n2 can be served locally.
    stub, result = await _run(lane, "raw_local", lane_matrix(local_score=8.0),
                              answer_for=_copying_local)
    for req in stub.requests:
        if not req.is_local:
            assert not _leaks(req), f"{req.kind} carried copied text"
            assert "relationship desk" not in json.dumps(req.body)
    # n2 reads a raw step whose output was withheld: n2 is raw, so local only.
    assert _route_of(lane["db"], "n2").get("local_only") is True
    assert stub.step_request(SUMMARY).is_local
    assert "BRT-LANE-004" in stub.of_kind("re_plan")[0].system
    assert "doc_001" in stub.of_kind("re_plan")[0].system


async def test_a_reader_of_guarded_output_is_derived(lane):
    stub, result = await _run(lane, "raw_local", lane_matrix(local_score=8.0))
    assert "local_only" not in _route_of(lane["db"], "n2")
    assert _route_of(lane["db"], "n1").get("local_only") is True


async def test_an_apply_to_each_aggregate_holds_ids_not_content(lane):
    stub, result = await _run(lane, "raw_local", lane_matrix())
    outputs = _envelope_output(lane["db"], result["run_id"], "apply_to_each")
    assert outputs
    items = [r["item"] for r in outputs[0]["output"]["results"]]
    assert sorted(items) == ["doc_001", "doc_002"]
    # The saved outbound envelope holds ids, never text.
    with sqlite3.connect(lane["db"]) as conn:
        saved = " ".join(r[0] for r in conn.execute("SELECT outbound_payload FROM envelopes"))
    assert not any(c in saved for c in CANARIES)


async def test_a_skill_step_over_documents_is_raw(lane):
    plan = [{"type": "answerer", "id": "n1", "skill_id": "extract_fields",
             "skill_args": {"items_ref": "documents", "concurrency_limit": 2,
                            "fields": [{"name": "client", "type": "string",
                                        "description": "client name"}]}}]
    stub, result = await _run(lane, "raw_local", lane_matrix(), plan=plan)
    answers = stub.of_kind("answerer")
    assert len(answers) == 2 and all(r.is_local for r in answers)
    for req in stub.requests:
        if not req.is_local:
            assert not _leaks(req)


async def test_the_trivial_shortcut_does_not_fire_with_documents(lane):
    lane["mp"].setenv("BORG_RULE_ZERO_SHORTCUT", "true")
    stub, result = await _run(lane, "raw_local", lane_matrix(), instruction="Thanks, summarise.")
    assert stub.of_kind("plan"), "the shortcut skipped the plan"


async def test_a_direct_answer_is_rejected_when_documents_are_present(lane):
    stub, result = await _run(
        lane, "raw_local", lane_matrix(),
        plan_content_for=lambda req: {"direct_answer": "Acme is high risk."},
    )
    assert result["status"] == "failed"
    assert "BRT-ORC-003" in result["error"]
    assert stub.of_kind("answerer") == []


# -- output guard ---------------------------------------------------------------------

async def test_output_guard_size_cap_and_failed_envelopes(lane):
    from src.envelopes.schema import InboundEnvelope
    from src.lane.context import guard_envelope, prepare_lane

    lane["mp"].setenv("BORG_LANE_MODE", "raw_local")
    lane["mp"].setenv("BORG_LANE_MAX_OUTPUT_CHARS", "100")
    ctx = await prepare_lane({"folder": "."}, lane["db"])
    task = await _raw_task(lane, "ollama/stub/local", "http://127.0.0.1:11434")

    def env_with(output, status="resolved", reason=None):
        return InboundEnvelope(envelope_id=task.envelope_id, subtask_id=task.subtask_id,
                               status=status, output=output, failure_reason=reason)

    under = {"a": "x" * 90}            # serialises to 99 characters
    over = {"a": "x" * 92}             # serialises to 101 characters
    assert len(json.dumps(under)) == 99 and len(json.dumps(over)) == 101
    passed = guard_envelope(env_with(under), task, ctx)
    assert passed.status == "resolved" and passed.lane_guarded is True
    held = guard_envelope(env_with(over), task, ctx)
    assert held.status == "failed" and "BRT-LANE-004" in held.failure_reason
    assert held.output.get("failure") == {"code": "BRT-LANE-004", "doc": "doc_001"}

    # A failed raw envelope is guarded too: its reason leaves as code plus id.
    failed = guard_envelope(
        env_with({}, status="failed", reason=f"BRT-ANS-004: Raw: {ACME_TEXT[:200]}"), task, ctx
    )
    assert CANARY_ACCOUNT not in (failed.failure_reason or "")
    assert json.loads(failed.failure_reason) == {"code": "BRT-ANS-004", "doc": "doc_001"}


# -- backstop -------------------------------------------------------------------------

async def test_backstop_refuses_document_text_in_a_tool_query(lane):
    plan = [{"type": "answerer", "id": "n1", "instruction": SUMMARY,
             "task_type": "reasoning", "tier": "mid", "output_schema": {"result": "string"},
             "tool_requests": [{"tool": "web_search",
                                "args": {"query": ACME_TEXT[60:260]}}]}]
    lane["mp"].setenv("TAVILY_API_KEY", "tvly-test")
    stub, result = await _run(lane, "raw_local", lane_matrix(), plan=plan)
    assert stub.of_kind("tool") == [], "the query left the machine"
    assert "BRT-LANE-006" in _error_codes(lane["db"], result["run_id"])


async def test_backstop_refuses_a_pasted_document_in_the_prompt(lane):
    stub, result = await _run(lane, "raw_local", lane_matrix(),
                              instruction=OPERATOR + "\n\n" + ACME_TEXT.upper())
    assert result["status"] == "failed"
    assert "BRT-LANE-006" in result["error"]
    assert [r for r in stub.requests if not r.is_local] == []


# -- modes ----------------------------------------------------------------------------------

async def test_tools_are_refused_in_all_local(lane):
    plan = [{"type": "answerer", "id": "n1", "instruction": SUMMARY,
             "task_type": "reasoning", "tier": "light", "output_schema": {"result": "string"},
             "tool_requests": [{"tool": "web_search", "args": {"query": "lease rates"}}]}]
    lane["mp"].setenv("TAVILY_API_KEY", "tvly-test")
    stub, result = await _run(lane, "all_local", lane_matrix(), plan=plan)
    assert stub.of_kind("tool") == []
    assert stub.of_kind("answerer") == []
    replan = stub.of_kind("re_plan")[0]
    assert "BRT-LANE-005" in replan.system


async def test_documents_on_an_orchestrate_step_are_refused(lane):
    plan = [{"type": "orchestrate", "id": "n1", "instruction": SUMMARY,
             "documents": ["doc_001"], "task_type": "reasoning", "tier": "mid",
             "output_schema": {"result": "string"}}]
    stub, result = await _run(lane, "raw_local", lane_matrix(), plan=plan)
    assert len(stub.of_kind("plan")) == 1, "a child planner was started"
    assert "BRT-LANE-005" in _error_codes(lane["db"], result["run_id"])


async def test_manifest_ids_puts_no_file_name_in_the_planner_request(lane):
    lane["mp"].setenv("BORG_LANE_MANIFEST", "ids")
    stub, result = await _run(lane, "raw_local", lane_matrix())
    for req in stub.requests:
        if not req.is_local:
            assert "acme_note" not in json.dumps(req.body)
            assert "birch_note" not in json.dumps(req.body)
    assert "doc_001" in stub.of_kind("plan")[0].system


# -- ledger ---------------------------------------------------------------------------------

async def test_ledger_keeps_a_failed_call_and_each_retry(lane):
    seen = {"summary": 0}

    def error_for(req):
        if req.kind == "answerer" and SUMMARY in req.system and seen["summary"] == 1:
            return httpx.ConnectError("refused")
        return None

    def finish_for(req):
        if req.kind == "answerer" and SUMMARY in req.system:
            seen["summary"] += 1
            return "length" if seen["summary"] == 1 else "stop"
        return "stop"

    stub, result = await _run(lane, "raw_local", lane_matrix(),
                              error_for=error_for, finish_for=finish_for)
    rows = [r for r in _ledger(lane["db"]) if SUMMARY in r["body"]]
    assert [r["attempt"] for r in rows] == [1, 2], "a retry must be its own row"
    assert rows[0]["status"] == "sent"
    assert rows[1]["status"] == "failed" and rows[1]["error"]
    assert all(r["left_machine"] == 1 for r in rows)


async def test_a_ledger_write_failure_blocks_the_request(lane):
    from src.lane import outbound

    async def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    lane["mp"].setattr(outbound, "_insert_row", broken)
    stub, result = await _run(lane, "raw_local", lane_matrix())
    assert result["status"] == "failed"
    assert "BRT-LANE-007" in result["error"]
    assert stub.requests == []


async def test_child_run_requests_appear_in_the_parents_list(lane):
    from src.db.state import get_outbound_requests

    plan_parent = [{"type": "orchestrate", "id": "n1", "instruction": "Draft a letter.",
                    "task_type": "writing", "tier": "mid", "output_schema": {"result": "string"}}]
    child_plan = [{"type": "answerer", "id": "c1", "instruction": "Write the letter body.",
                   "task_type": "writing", "tier": "mid", "output_schema": {"result": "string"}}]

    def plan_for(req):
        return child_plan if "Your Assignment In This Run" in req.system else plan_parent

    stub, result = await _run(lane, "raw_local", lane_matrix(), plan=plan_parent,
                              documents=None, plan_for=plan_for)
    rows = await get_outbound_requests(result["run_id"], db_path=lane["db"])
    run_ids = {r["run_id"] for r in rows}
    assert result["run_id"] in run_ids and len(run_ids) == 2
    assert len(rows) == len([r for r in stub.requests if not r.is_local])
    assert all(r["left_machine"] == 1 for r in rows)
