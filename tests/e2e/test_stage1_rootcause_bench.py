"""Bench stage 1 (17-Nov-2026) root causes: reproductions and their fixes.

Planner ruling 16 wrote the reproductions (each failed on the tree it was
written against); planner ruling 20 fixed R1 and R3 and added the tests below
the reproductions for the ruling's conditions (BRT-ORC-026 to -030).

R1  A re_plan "continue" step that reuses the id of a step that already ran
    (a retry named like the failed step) is taken as done and never runs; the
    loop falls through to compose. Seen: A cloud (fix_classify_failures,
    retry_classify, merge_all_classifications re-sent), A local and D local
    (classify_emails / retry_classify re-sent after BRT-LANE-002).
R2  A compose reply in the re_plan shape ({"action": "compose" | "continue"})
    with only apply_to_each results in stage_results raises BRT-ORC-009: the
    salvage reads only string keys (response/answer/text/content/summary),
    never an apply_to_each ``{"results": [...]}``, and compose is not asked
    again. Seen: all four ORC-009 runs.
R3  A re_plan (or plan) node list holding a non-object element (job C: the
    string "rationale: ") crashes the loop with AttributeError
    "'str' object has no attribute 'get'" at engine.py ``n.get("id")``.

The real engine, executor and router run against the scripted stand-in model
server and temp databases. Nothing here needs a key or the network.
"""
import json
from dataclasses import dataclass
from typing import Optional

import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import (
    StubModels, drain_log_writes, lane_matrix, three_tier_matrix,
)

REVIEWS = ["Kettle: boils fast", "Toaster: burnt everything", "Blender: loud but fine"]
PER_ITEM = "Rate the sentiment of this review."


@dataclass
class ComposeShapeStub(StubModels):
    """StubModels whose compose reply can be any JSON object (e.g. re_plan shape)."""
    compose_content: Optional[dict] = None

    def _answer(self, url, payload):
        resp = super()._answer(url, payload)
        if self.requests and self.requests[-1].kind == "compose" and self.compose_content is not None:
            resp._body["choices"][0]["message"]["content"] = json.dumps(self.compose_content)
        return resp


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "stage1_rootcause.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-stage1")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _ans(instr, **extra):
    node = {"type": "answerer", "instruction": instr, "task_type": "extraction",
            "tier": "light", "output_schema": {"result": "string"}}
    node.update(extra)
    return node


def _per_item(**extra):
    node = {"type": "apply_to_each", "id": "rate", "instruction": PER_ITEM,
            "task_type": "extraction", "tier": "light", "items": REVIEWS,
            "output_schema": {"sentiment": "string"}}
    node.update(extra)
    return node


def _calls_with(stub, fragment):
    return [r for r in stub.of_kind("answerer") if fragment in r.system]


def _replan_then_compose(next_nodes):
    sent = []

    def replan(_req):
        if sent:
            return {"action": "compose", "rationale": "done"}
        sent.append(1)
        return {"action": "continue", "next_nodes": next_nodes, "rationale": "retry"}
    return replan


# --- R1: a retry that reuses a failed step's id never runs -------------------

@pytest.mark.asyncio
async def test_r1_replan_retry_reusing_a_failed_step_id_is_dispatched(env):
    db_path, mp = env
    first = _ans("First try AAA.", id="classify")
    retry = _ans("Retry BBB.", id="classify")      # same id, as grok did in A/D
    stub = StubModels(
        plan_nodes=[first],
        replan_for=_replan_then_compose([retry]),
        finish_for=lambda r: "length" if "First try AAA." in r.system else "stop",
    ).install(mp)
    await run_pipeline("Classify.", db_path=db_path)
    await drain_log_writes()
    assert len(_calls_with(stub, "First try AAA.")) >= 1
    assert len(_calls_with(stub, "Retry BBB.")) == 1, (
        "re_plan's retry step (id 'classify', already failed) was taken as done "
        "and never dispatched; the loop went straight to compose"
    )


@pytest.mark.asyncio
async def test_r1_lane_retry_after_lane_002_reaches_the_local_model(tmp_path, monkeypatch):
    """A local repro: plan step has no task_type -> 'reasoning' -> LANE-002;
    re_plan resends the SAME id with task_type extraction -> never runs."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "email_001.txt").write_text("My invoice is wrong, please fix.", encoding="utf-8")
    db_path = str(tmp_path / "lane.db")
    await create_all(db_path)
    for name in ("BORG_LANE_MANIFEST", "BORG_LOCAL_HOSTS", "BORG_LANE_LOCAL_ALLOWLIST",
                 "BORG_LANE_MAX_OUTPUT_CHARS", "BORG_LANE_MAX_VERBATIM_CHARS",
                 "BORG_LANE_MAX_NGRAM_SHARE", "BORG_LANE_PART_CHARS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BORG_DOCS_ROOT", str(docs))
    monkeypatch.setenv("BORG_LANE_MODE", "raw_local")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-stage1-lane")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    matrix = lane_matrix(local_score=7.0)
    local = next(m for m in matrix.all() if m.source == "ollama")
    # The bench row for ollama/llama3.2:3b: reasoning 4.6, extraction 6.8.
    local.task_scores["reasoning"] = 4.6
    local.task_scores["extraction"] = 6.8
    matrix.upsert(local)
    monkeypatch.setattr(engine, "_shared_matrix", matrix)

    step = {"type": "apply_to_each", "id": "classify_emails", "skill_id": "classify_into_buckets",
            "skill_version": 1,
            "skill_args": {"buckets": ["billing", "technical"], "items_ref": "documents"},
            "tier": "light"}                       # no task_type, as in A local's plan
    retry = dict(step, task_type="extraction")     # A local's re_plan: same id
    stub = StubModels(plan_nodes=[step], replan_for=_replan_then_compose([retry]),
                      answer_for=lambda r: {"bucket": "billing", "confidence": 0.9,
                                            "defer_reason": ""}).install(monkeypatch)
    await run_pipeline("Classify each email.", db_path=db_path, documents={"folder": "."})
    await drain_log_writes()
    assert [r for r in stub.of_kind("answerer") if r.is_local], (
        "no worker ran: the extraction retry reused id 'classify_emails' and was skipped"
    )


# --- R3: a non-object element in the node list ------------------------------

@pytest.mark.asyncio
async def test_r3_replan_next_nodes_with_a_string_element_does_not_crash(env):
    db_path, mp = env
    stub = StubModels(
        plan_nodes=[_ans("First DDD.", id="a")],
        replan_for=_replan_then_compose([_ans("Second EEE.", id="b"), "rationale: "]),
    ).install(mp)
    result = await run_pipeline("Do two things.", db_path=db_path)
    await drain_log_writes()
    assert "has no attribute 'get'" not in str(result.get("error")), result.get("error")
    assert len(_calls_with(stub, "Second EEE.")) == 1, "the valid sibling never ran"


@pytest.mark.asyncio
async def test_r3_plan_nodes_with_a_string_element_does_not_crash(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_ans("Only FFF.", id="a"), "rationale: "]).install(mp)
    result = await run_pipeline("Do one thing.", db_path=db_path)
    await drain_log_writes()
    assert "has no attribute 'get'" not in str(result.get("error")), result.get("error")
    assert len(_calls_with(stub, "Only FFF.")) == 1


# --- Ruling 20 conditions, beyond the reproductions --------------------------

async def _decisions(db_path, run_id, event_type="error"):
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT decision_json FROM logs WHERE run_id=? AND event_type=? ORDER BY log_id",
            (run_id, event_type),
        )).fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


def _answer_by_marker(table):
    """answer_for: the output whose marker appears in the step's prompt."""
    def answer(req):
        for marker, out in table.items():
            if marker in req.system:
                return {"result": out}
        return {"result": "other"}
    return answer


def _replans(*batches):
    """re_plan answers: each batch in turn, then compose."""
    sent = []

    def replan(_req):
        if len(sent) >= len(batches):
            return {"action": "compose", "rationale": "done"}
        sent.append(1)
        return {"action": "continue", "next_nodes": batches[len(sent) - 1], "rationale": "r"}
    return replan


@pytest.mark.asyncio
async def test_r1_batch_ref_to_a_reused_id_follows_the_renamed_step(env):
    """TC-ORC-026-A: a sibling's depends_on on the reused id reads the NEW step."""
    db_path, mp = env
    stub = StubModels(
        plan_nodes=[_ans("Step A1 MARK-A1.", id="a")],
        replan_for=_replans([
            _ans("Step A2 MARK-A2.", id="a"),
            _ans("Step B MARK-B.", id="b", depends_on=["a"]),
        ]),
        answer_for=_answer_by_marker({"MARK-B": "OUT-B", "MARK-A2": "NEW-A",
                                      "MARK-A1": "OLD-A"}),
    ).install(mp)
    result = await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    b = stub.step_request("MARK-B")
    assert "NEW-A" in b.system and "OLD-A" not in b.system
    # B waited for the renamed step: it ran after it.
    order = [r for r in stub.of_kind("answerer")]
    assert order.index(stub.step_request("MARK-A2")) < order.index(b)
    renames = [d for d in await _decisions(db_path, result["run_id"])
               if d.get("code") == "BRT-ORC-026"]
    assert renames and renames[0]["renamed"] == [{"from": "a", "to": "a_2"}]
    # The earlier result stays under its original id; compose sees both.
    compose = stub.of_kind("compose")[-1].system
    assert '"a"' in compose and '"a_2"' in compose and "OLD-A" in compose


@pytest.mark.asyncio
async def test_r1_refs_to_ids_not_redefined_keep_the_existing_result(env):
    """TC-ORC-026-B: refs to an id not redefined in the batch, and the renamed
    step's own ref to its old id, keep reading the earlier results."""
    db_path, mp = env
    stub = StubModels(
        plan_nodes=[_ans("Step A1 MARK-A1.", id="a"), _ans("Step C MARK-C.", id="c")],
        replan_for=_replans([
            _ans("Step A2 MARK-A2.", id="a", depends_on=["a"]),
            _ans("Step D MARK-D.", id="d", depends_on=["c"]),
        ]),
        answer_for=_answer_by_marker({"MARK-A2": "NEW-A", "MARK-A1": "OLD-A",
                                      "MARK-C": "OLD-C", "MARK-D": "OUT-D"}),
    ).install(mp)
    result = await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert "OLD-C" in stub.step_request("MARK-D").system
    assert "OLD-A" in stub.step_request("MARK-A2").system
    renames = [d for d in await _decisions(db_path, result["run_id"])
               if d.get("code") == "BRT-ORC-026"]
    assert [r["to"] for r in renames[0]["renamed"]] == ["a_2"]   # d was not renamed


@pytest.mark.asyncio
async def test_r1_ids_stay_unique_across_the_run(env):
    """TC-ORC-026-C: a second re_plan reusing the same id gets ``_3``."""
    db_path, mp = env
    stub = StubModels(
        plan_nodes=[_ans("Try one MARK-1.", id="a")],
        replan_for=_replans([_ans("Try two MARK-2.", id="a")],
                            [_ans("Try three MARK-3.", id="a")]),
    ).install(mp)
    result = await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    for marker in ("MARK-1", "MARK-2", "MARK-3"):
        assert len(_calls_with(stub, marker)) == 1, marker
    renamed = [d["renamed"][0]["to"] for d in await _decisions(db_path, result["run_id"])
               if d.get("code") == "BRT-ORC-026"]
    assert renamed == ["a_2", "a_3"]


@pytest.mark.asyncio
async def test_r1_a_new_id_is_not_renamed(env):
    """TC-ORC-026-D (failure direction): a re_plan step with a fresh id runs
    as itself and no rename is logged."""
    db_path, mp = env
    stub = StubModels(
        plan_nodes=[_ans("First MARK-X.", id="x")],
        replan_for=_replans([_ans("Fresh MARK-Y.", id="y", depends_on=["x"])]),
        answer_for=_answer_by_marker({"MARK-X": "OUT-X", "MARK-Y": "OUT-Y"}),
    ).install(mp)
    result = await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert "OUT-X" in stub.step_request("MARK-Y").system
    assert not [d for d in await _decisions(db_path, result["run_id"])
                if d.get("code") == "BRT-ORC-026"]


@pytest.mark.asyncio
async def test_r3_replan_with_only_non_objects_composes(env):
    """TC-ORC-027-A: nothing valid left in re_plan means compose; logged."""
    db_path, mp = env
    stub = StubModels(
        plan_nodes=[_ans("First GGG.", id="a")],
        replan_for=_replans(["rationale: ", 42]),
    ).install(mp)
    result = await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert len(stub.of_kind("answerer")) == 1
    assert len(stub.of_kind("compose")) == 1
    dropped = [d for d in await _decisions(db_path, result["run_id"])
               if d.get("code") == "BRT-ORC-027"]
    assert dropped and dropped[0]["mode"] == "re_plan" and dropped[0]["dropped_count"] == 2


@pytest.mark.asyncio
async def test_r3_plan_with_only_non_objects_takes_the_no_plan_path(env):
    """TC-ORC-027-B: a plan with no object node is never run as an empty plan;
    the existing no-plan path (compose) answers, and the drop is logged."""
    db_path, mp = env
    stub = StubModels(plan_nodes=["rationale: ", ["x"]]).install(mp)
    result = await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert not stub.of_kind("answerer") and not stub.of_kind("re_plan")
    assert len(stub.of_kind("compose")) == 1
    dropped = [d for d in await _decisions(db_path, result["run_id"])
               if d.get("code") == "BRT-ORC-027"]
    assert dropped and dropped[0]["mode"] == "plan" and dropped[0]["kept_count"] == 0


@pytest.mark.asyncio
async def test_r3_plan_with_only_non_objects_and_documents_is_orc_003(tmp_path, monkeypatch):
    """TC-ORC-027-C: with documents, a plan left with no object node is
    rejected by the existing BRT-ORC-003 rule."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("hello", encoding="utf-8")
    db_path = str(tmp_path / "r3docs.db")
    await create_all(db_path)
    monkeypatch.setenv("BORG_DOCS_ROOT", str(docs))
    monkeypatch.setenv("BORG_LANE_MODE", "raw_local")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-r3")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", lane_matrix())
    StubModels(plan_nodes=["rationale: "]).install(monkeypatch)
    result = await run_pipeline("Read it.", db_path=db_path, documents={"folder": "."})
    await drain_log_writes()
    assert result["status"] == "failed" and "BRT-ORC-003" in str(result.get("error"))
