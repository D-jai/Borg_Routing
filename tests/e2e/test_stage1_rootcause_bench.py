"""Bench stage 1 (17-Nov-2026) root causes: reproductions and their fixes.

Planner ruling 16 wrote the reproductions (each failed on the tree it was
written against); planner ruling 20 fixed R1-R3 and added the tests below
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

import httpx
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


# --- R2: compose answers in the re_plan shape -> BRT-ORC-009 ----------------

@pytest.mark.parametrize("compose_reply", [
    {"action": "compose", "next_nodes": None, "rationale": "can synthesise now"},  # A/D cloud
    {"action": "continue", "next_nodes": [_ans("More CCC.", id="more")],
     "rationale": "one more pass"},                                               # A/D local
])
@pytest.mark.asyncio
async def test_r2_compose_in_replan_shape_with_apply_to_each_results_is_not_orc_009(
    env, compose_reply,
):
    db_path, mp = env
    stub = ComposeShapeStub(plan_nodes=[_per_item()], compose_content=compose_reply,
                            answer_for=lambda r: {"sentiment": "positive"}).install(mp)
    result = await run_pipeline("Rate each review; return a JSON list.", db_path=db_path)
    await drain_log_writes()
    assert len(_calls_with(stub, PER_ITEM)) == len(REVIEWS)
    assert "BRT-ORC-009" not in str(result.get("error")), (
        f"three resolved per-item results were discarded: {result.get('error')}"
    )
    assert result["status"] == "completed", result.get("error")


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


# --- R2: compose recovery, in the ruling's order -----------------------------

@dataclass
class ComposeSeqStub(StubModels):
    """Compose replies taken in turn from ``compose_seq`` (an Exception is
    raised as the call's error); after the list, the default compose reply."""
    compose_seq: list = None

    def _answer(self, url, payload):
        resp = super()._answer(url, payload)
        if self.requests and self.requests[-1].kind == "compose" and self.compose_seq:
            nth = len(self.of_kind("compose")) - 1
            if nth < len(self.compose_seq):
                reply = self.compose_seq[nth]
                if isinstance(reply, Exception):
                    raise reply
                resp._body["choices"][0]["message"]["content"] = json.dumps(reply)
        return resp


async def _answer_kind(db_path, run_id):
    from src.db.state import get_run
    from uuid import UUID
    return (await get_run(UUID(run_id), db_path=db_path)).get("answer_kind")


async def _codes(db_path, run_id):
    found = []
    for kind in ("error", "orchestrator_decision"):
        found += [d.get("code") for d in await _decisions(db_path, run_id, kind)]
    return found


@pytest.mark.parametrize("reply, expected", [
    ({"action": "compose", "response": "Answer WRONG-SHAPE."}, "Answer WRONG-SHAPE."),
    ({"final_answer": "Answer FINAL-FIELD."}, "Answer FINAL-FIELD."),
    ({"response": [{"review": "Kettle", "sentiment": "positive"}]}, '"sentiment": "positive"'),
])
@pytest.mark.asyncio
async def test_r2_wrong_shape_with_final_text_is_used_without_a_second_call(env, reply, expected):
    """TC-ORC-028-A: step 1, a usable final text in the wrong shape."""
    db_path, mp = env
    stub = ComposeSeqStub(plan_nodes=[_per_item()], compose_seq=[reply],
                          answer_for=lambda r: {"sentiment": "positive"}).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert expected in result["response"]
    assert len(stub.of_kind("compose")) == 1
    codes = await _codes(db_path, result["run_id"])
    assert "BRT-ORC-028" in codes and "BRT-ORC-029" not in codes
    assert await _answer_kind(db_path, result["run_id"]) == "composed"


@pytest.mark.asyncio
async def test_r2_right_shape_is_not_logged_as_wrong(env):
    """TC-ORC-028-B: a plain {"response": ...} logs nothing and is composed;
    the mode is named in the user message of re_plan and compose."""
    db_path, mp = env
    stub = ComposeSeqStub(plan_nodes=[_ans("One HHH.", id="a")],
                          compose_seq=[{"response": "Plain answer."}]).install(mp)
    result = await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert result["response"] == "Plain answer."
    codes = await _codes(db_path, result["run_id"])
    assert not {"BRT-ORC-028", "BRT-ORC-029", "BRT-ORC-030"} & set(codes)
    assert await _answer_kind(db_path, result["run_id"]) == "composed"
    assert stub.of_kind("re_plan")[0].user == engine._MODE_USER_MESSAGES["re_plan"]
    assert stub.of_kind("compose")[0].user == engine._MODE_USER_MESSAGES["compose"]


@pytest.mark.asyncio
async def test_r2_no_final_text_asks_compose_once_more(env):
    """TC-ORC-029-A: step 2, the re-ask's answer is used."""
    db_path, mp = env
    stub = ComposeSeqStub(plan_nodes=[_per_item()],
                          compose_seq=[{"action": "compose"}, {"response": "SECOND TRY."}],
                          answer_for=lambda r: {"sentiment": "positive"}).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["response"] == "SECOND TRY."
    composes = stub.of_kind("compose")
    # Ruling 21: with a list step the re-ask also carries the row-marker block.
    assert len(composes) == 2
    assert composes[1].user.startswith(engine._COMPOSE_REASK + "\nList steps the code")
    codes = await _codes(db_path, result["run_id"])
    assert "BRT-ORC-029" in codes and "BRT-ORC-030" not in codes
    assert await _answer_kind(db_path, result["run_id"]) == "composed"


@pytest.mark.parametrize("second", [
    {"action": "continue", "next_nodes": [], "rationale": "again"},
    httpx.ConnectError("compose unreachable"),
])
@pytest.mark.asyncio
async def test_r2_salvages_finished_results_when_compose_fails_twice(env, second):
    """TC-ORC-030-A: step 3, the finished apply_to_each results come back with
    a note, a warning code and answer_kind "salvaged" (shown by GET /runs/{id})."""
    db_path, mp = env
    stub = ComposeSeqStub(plan_nodes=[_per_item()],
                          compose_seq=[{"action": "compose"}, second],
                          answer_for=lambda r: {"sentiment": "positive"}).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    text = result["response"]
    assert text.startswith(engine.SALVAGE_NOTE)
    assert "## rate (3 item(s) finished)" in text
    for review in REVIEWS:
        assert review in text
    assert text.count('"sentiment": "positive"') == 3
    assert len(stub.of_kind("compose")) == 2
    assert "BRT-ORC-030" in await _codes(db_path, result["run_id"])

    from src.api import tasks
    mp.setattr(tasks, "DB_PATH", db_path)
    resp = await tasks.get_run_state(result["run_id"])
    assert json.loads(resp.body)["run"]["answer_kind"] == "salvaged"


@pytest.mark.asyncio
async def test_r2_nothing_finished_is_still_orc_009(env):
    """TC-ORC-030-B (failure direction): no finished step, compose fails twice:
    BRT-ORC-009, the run fails, no answer_kind."""
    db_path, mp = env
    stub = ComposeSeqStub(
        plan_nodes=[_ans("Doomed III.", id="a")],
        compose_seq=[{"action": "compose"}, {"action": "compose"}],
        error_for=lambda r: httpx.ConnectError("down") if r.kind == "answerer" else None,
    ).install(mp)
    result = await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "failed" and "BRT-ORC-009" in str(result.get("error"))
    assert len(stub.of_kind("compose")) == 2
    assert await _answer_kind(db_path, result["run_id"]) is None


class _RawLane:
    keeps_raw_local = True

    @staticmethod
    def is_part_id(value):
        return isinstance(value, str) and value.startswith("doc_")


def _env(output, scope=None, guarded=None, status="resolved"):
    from uuid import uuid4
    from src.envelopes.schema import InboundEnvelope
    env = InboundEnvelope(envelope_id=uuid4(), subtask_id=uuid4(), status=status,
                          output=output, lane_guarded=guarded,
                          **({"failure_reason": "x"} if status == "failed" else {}))
    env._data_scope = scope
    return env


def test_r2_salvage_keeps_raw_text_out_in_raw_local():
    """TC-ORC-030-C: in raw_local / all_local a raw step's unguarded output is
    withheld and an item that is not a part id is not shown; guarded output
    and derived steps are shown."""
    results = {
        "raw_unguarded": _env({"result": "SECRET DOCUMENT TEXT"}, "raw", None),
        "raw_guarded": _env({"result": "guarded summary"}, "raw", True),
        "raw_each": _env({"results": [
            {"index": 0, "item": "doc_001", "output": {"bucket": "billing"}},
            {"index": 1, "item": "RAW ITEM TEXT", "output": {"bucket": "tech"}},
        ], "failures": [], "success_count": 2}, "raw", False),
        "derived": _env({"response": "derived answer"}, "derived"),
        "failed": _env({"result": "FAILED OUT"}, "derived", status="failed"),
    }
    text = engine._salvage_finished_results(results, _RawLane())
    assert "SECRET DOCUMENT TEXT" not in text and "RAW ITEM TEXT" not in text
    assert "FAILED OUT" not in text
    assert "guarded summary" in text and "derived answer" in text
    assert "doc_001" in text and '"bucket": "tech"' in text
    assert "raw_unguarded" in text.split("Not shown")[1]
    # Lane off (or by_reference): everything finished is shown.
    open_text = engine._salvage_finished_results(results, None)
    assert "SECRET DOCUMENT TEXT" in open_text and "RAW ITEM TEXT" in open_text


@pytest.mark.asyncio
async def test_r2_salvage_in_raw_local_never_carries_document_text(tmp_path, monkeypatch):
    """TC-ORC-030-D: end to end in raw_local, a salvaged answer over a
    per-document step shows the outputs and part ids, never the text."""
    secret = "My invoice number 99-ZETA is wrong, please fix it today."
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "email_001.txt").write_text(secret, encoding="utf-8")
    db_path = str(tmp_path / "salvage_lane.db")
    await create_all(db_path)
    for name in ("BORG_LANE_MANIFEST", "BORG_LOCAL_HOSTS", "BORG_LANE_LOCAL_ALLOWLIST",
                 "BORG_LANE_MAX_OUTPUT_CHARS", "BORG_LANE_MAX_VERBATIM_CHARS",
                 "BORG_LANE_MAX_NGRAM_SHARE", "BORG_LANE_PART_CHARS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BORG_DOCS_ROOT", str(docs))
    monkeypatch.setenv("BORG_LANE_MODE", "raw_local")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-salvage-lane")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", lane_matrix())
    step = {"type": "apply_to_each", "id": "classify", "skill_id": "classify_into_buckets",
            "skill_version": 1, "task_type": "extraction", "tier": "light",
            "skill_args": {"buckets": ["billing", "technical"], "items_ref": "documents"}}
    stub = ComposeSeqStub(
        plan_nodes=[step], compose_seq=[{"action": "compose"}, {"action": "compose"}],
        answer_for=lambda r: {"bucket": "billing", "confidence": 0.9, "defer_reason": ""},
    ).install(monkeypatch)
    result = await run_pipeline("Classify each email.", db_path=db_path,
                                documents={"folder": "."})
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert [r for r in stub.of_kind("answerer") if r.is_local]
    text = result["response"]
    assert text.startswith(engine.SALVAGE_NOTE) and '"bucket": "billing"' in text
    assert "99-ZETA" not in text and "invoice" not in text
    assert await _answer_kind(db_path, result["run_id"]) == "salvaged"


# --- R4: a skill step's own extra_fields are asked for and checked ----------

def _classify_step():
    # Job A's node, with the reviews inline instead of documents.
    return {"type": "apply_to_each", "id": "classify", "skill_id": "classify_into_buckets",
            "skill_version": 1, "task_type": "extraction", "tier": "light",
            "skill_args": {"buckets": ["good", "bad"], "items_ref": REVIEWS,
                           "extra_fields": {"urgency": "integer from 1 to 5"}},
            "output_schema": {"category": "string", "urgency": "integer"}}


@pytest.mark.parametrize("answer, resolved", [
    ({"bucket": "good", "confidence": 0.9, "defer_reason": "", "urgency": 2,
      "category": "good"}, 3),
    ({"bucket": "good", "confidence": 0.9, "defer_reason": "", "category": "good"}, 0),
])
@pytest.mark.asyncio
async def test_r4_classify_step_asks_for_its_extra_fields_and_checks_them(env, answer, resolved):
    """TC-SKL-036: every item prompt asks for urgency (its extra_fields; the
    node's output_schema is not read, R1-R4 review 6); an item answer
    without urgency fails the inbound check (BRT-ENV-011) instead of passing
    without it."""
    db_path, mp = env
    stub = StubModels(plan_nodes=[_classify_step()], answer_for=lambda r: answer).install(mp)
    result = await run_pipeline("Classify each review with its urgency.", db_path=db_path)
    await drain_log_writes()
    items = stub.of_kind("answerer")
    assert len(items) == len(REVIEWS)
    for req in items:
        assert "Also return these fields: urgency (integer from 1 to 5)." in req.system
        assert "category (string)" not in req.system
        assert '"urgency": "integer from 1 to 5"' in req.system
    compose = stub.of_kind("compose")[-1].system
    assert f'"success_count": {resolved}' in compose
    if not resolved:
        assert "BRT-ENV-011" in compose


# --- R1-R4 review (6): the lead's final focused fix -------------------------

def _job_a_step(**args):
    # Job A's real node: output_schema {file, category, urgency} describes the
    # final answer's rows; extra_fields {urgency} is the step's own ask.
    return {"type": "apply_to_each", "id": "classify_emails",
            "skill_id": "classify_into_buckets", "skill_version": 1,
            "task_type": "extraction", "tier": "light",
            "skill_args": {"buckets": ["good", "bad"], "items_ref": REVIEWS, **args},
            "output_schema": {"file": "string", "category": "string", "urgency": "integer"}}


@pytest.mark.asyncio
async def test_r4_job_a_node_asks_for_skill_keys_and_urgency_only(env):
    """TC-SKL-038: the recorded worker outputs (bucket, confidence,
    defer_reason, plus urgency) resolve without file or category; an item
    missing defer_reason still fails, as 11 of 200 did in job A."""
    db_path, mp = env

    def answer(req):
        out = {"bucket": "good", "confidence": 1.0, "urgency": 3}
        if "Toaster" not in req.system:
            out["defer_reason"] = ""
        return out

    stub = StubModels(plan_nodes=[_job_a_step(extra_fields={
        "urgency": "integer from 1 (lowest) to 5 (highest)"})],
        answer_for=answer).install(mp)
    result = await run_pipeline("Classify each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    for req in stub.of_kind("answerer"):
        line = req.system.split("Also return these fields:")[-1].split("\n")[0]
        assert line == " urgency (integer from 1 (lowest) to 5 (highest))."
        assert '"file"' not in req.system and '"category"' not in req.system
    compose = stub.of_kind("compose")[-1].system
    assert '"success_count": 2' in compose


@pytest.mark.asyncio
async def test_r4_node_json_schema_array_adds_nothing(env):
    """TC-SKL-039 (failure direction): a JSON-Schema array output_schema on
    the node and no extra_fields: nothing is added, the skill's keys resolve."""
    db_path, mp = env
    node = _job_a_step()
    node["output_schema"] = {"type": "array", "items": {"type": "object", "properties": {
        "file": {"type": "string"}, "risk": {"type": "string"}}}}
    stub = StubModels(plan_nodes=[node], answer_for=lambda r: {
        "bucket": "good", "confidence": 0.9, "defer_reason": ""}).install(mp)
    result = await run_pipeline("Classify each review.", db_path=db_path)
    await drain_log_writes()
    for req in stub.of_kind("answerer"):
        assert "Also return" not in req.system
        assert '"risk"' not in req.system and '"file"' not in req.system
    assert '"success_count": 3' in stub.of_kind("compose")[-1].system


@pytest.mark.asyncio
async def test_r4_defer_and_collect_with_a_node_schema_still_defers(env):
    """TC-SKL-040: defer_and_collect's node output_schema {label, category}
    is not required of each item, so a 'defer' answer passes the check."""
    db_path, mp = env
    node = {"type": "apply_to_each", "id": "first_pass", "skill_id": "defer_and_collect",
            "skill_version": 1, "task_type": "extraction", "tier": "light",
            "skill_args": {"items_ref": REVIEWS, "instruction": "Label the review.",
                           "output_schema": {"label": "string"}, "confidence_threshold": 0.7},
            "output_schema": {"label": "string", "category": "string"}}
    stub = StubModels(plan_nodes=[node], answer_for=lambda r: {
        "result_or_defer": "defer", "confidence": 0.2, "defer_reason": "unclear"}).install(mp)
    result = await run_pipeline("Label each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    for req in stub.of_kind("answerer"):
        assert "Also return" not in req.system
    compose = stub.of_kind("compose")[-1].system
    assert '"success_count": 3' in compose and "BRT-ENV-011" not in compose


@pytest.mark.asyncio
async def test_r1_remap_that_would_close_a_cycle_keeps_the_earlier_result(env):
    """TC-ORC-026-E: plan [a]; re_plan [{a, depends_on [b]}, {b, inputs_ref a}].
    b runs once on the finished a; a_2 runs after b; no cycle failure."""
    db_path, mp = env
    stub = StubModels(
        plan_nodes=[_ans("Step A1 MARK-A1.", id="a")],
        replan_for=_replans([
            _ans("Step A2 MARK-A2.", id="a", depends_on=["b"]),
            _ans("Step B MARK-B.", id="b", inputs_ref="a"),
        ]),
        answer_for=_answer_by_marker({"MARK-B": "OUT-B", "MARK-A2": "NEW-A",
                                      "MARK-A1": "OLD-A"}),
    ).install(mp)
    result = await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    b = stub.step_request("MARK-B")
    a2 = stub.step_request("MARK-A2")
    assert "NEW-A" not in b.system                 # b never waited for a_2
    assert "OUT-B" in a2.system                    # a_2 ran on b's result
    order = stub.of_kind("answerer")
    assert order.index(b) < order.index(a2)
    codes = await _codes(db_path, result["run_id"])
    assert "BRT-ORC-024" not in codes and "BRT-ORC-025" not in codes


def test_r1_cycle_guard_unit():
    """TC-ORC-026-F: the remap is kept where it closes no cycle."""
    nodes, renames = engine._rename_reused_ids(
        [{"id": "a", "depends_on": ["b"]}, {"id": "b", "inputs_ref": "a"},
         {"id": "c", "depends_on": ["a"]}], {"a"}, {"a"})
    assert renames == [{"from": "a", "to": "a_2"}]
    assert nodes[0]["id"] == "a_2" and nodes[0]["depends_on"] == ["b"]
    assert nodes[1]["inputs_ref"] == "a"          # would close a_2 -> b -> a_2
    assert nodes[2]["depends_on"] == ["a_2"]      # no cycle: follows the new step


@pytest.mark.asyncio
async def test_r1_numeric_ids_are_compared_like_strings(env):
    """TC-ORC-026-G: a re_plan step reusing numeric id 1 is renamed and runs;
    a numeric depends_on waits for its step."""
    db_path, mp = env
    stub = StubModels(
        plan_nodes=[_ans("Try one MARK-1.", id=1),
                    _ans("After one MARK-W.", id=2, depends_on=[1])],
        replan_for=_replans([_ans("Try two MARK-2.", id=1)]),
        answer_for=_answer_by_marker({"MARK-1": "OUT-1", "MARK-2": "OUT-2",
                                      "MARK-W": "OUT-W"}),
    ).install(mp)
    result = await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    for marker in ("MARK-1", "MARK-2", "MARK-W"):
        assert len(_calls_with(stub, marker)) == 1, marker
    assert "OUT-1" in stub.step_request("MARK-W").system
    renames = [d for d in await _decisions(db_path, result["run_id"])
               if d.get("code") == "BRT-ORC-026"]
    assert renames and renames[0]["renamed"] == [{"from": "1", "to": "1_2"}]


@pytest.mark.parametrize("reply", [
    {"action": "continue", "next_nodes": [{"type": "answerer", "id": "x"}],
     "message": "Retrying the failed items first."},
    {"action": "compose", "next_nodes": None,
     "output": {"next_nodes": [], "status": "ready"}, "rationale": "synthesise now"},
])
@pytest.mark.asyncio
async def test_r2_control_reply_fields_are_not_an_answer(env, reply):
    """TC-ORC-028-C: with action or next_nodes present, message/output/etc.
    are not taken as the answer; compose is asked again."""
    db_path, mp = env
    stub = ComposeSeqStub(plan_nodes=[_per_item()], compose_seq=[reply],
                          compose_text="AFTER RE-ASK.",
                          answer_for=lambda r: {"sentiment": "positive"}).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["response"] == "AFTER RE-ASK."
    assert len(stub.of_kind("compose")) == 2
    codes = await _codes(db_path, result["run_id"])
    assert "BRT-ORC-029" in codes and "BRT-ORC-028" not in codes


@pytest.mark.parametrize("field", ["response", "direct_answer", "final_answer", "answer"])
@pytest.mark.asyncio
async def test_r2_answer_fields_next_to_an_action_still_count(env, field):
    """TC-ORC-028-D: the four answer fields count next to action/next_nodes."""
    db_path, mp = env
    stub = ComposeSeqStub(plan_nodes=[_per_item()],
                          compose_seq=[{"action": "compose", "next_nodes": None,
                                        field: "THE ANSWER."}],
                          answer_for=lambda r: {"sentiment": "positive"}).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["response"] == "THE ANSWER."
    assert len(stub.of_kind("compose")) == 1


@pytest.mark.asyncio
async def test_answer_kind_direct(env):
    """TC-ORC-031-A: a plan direct_answer marks the run "direct"."""
    db_path, mp = env
    StubModels(plan_nodes=[], plan_content_for=lambda r: {
        "direct_answer": "DIRECT REPLY.", "plan": None, "rationale": "r"}).install(mp)
    result = await run_pipeline("What is two plus two?", db_path=db_path)
    await drain_log_writes()
    assert result["response"] == "DIRECT REPLY."
    assert await _answer_kind(db_path, result["run_id"]) == "direct"


@pytest.mark.asyncio
async def test_answer_kind_declined(env):
    """TC-ORC-031-B: compose action fail returns its rationale, "declined"."""
    db_path, mp = env
    ComposeSeqStub(plan_nodes=[_ans("One JJJ.", id="a")],
                   compose_seq=[{"action": "fail", "rationale": "Cannot do that."}]).install(mp)
    result = await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed"
    assert result["response"] == "Cannot do that."
    assert await _answer_kind(db_path, result["run_id"]) == "declined"
    assert "BRT-ORC-031" in await _codes(db_path, result["run_id"])


@pytest.mark.asyncio
async def test_answer_kind_shortcut(env):
    """TC-ORC-031-C: a Rule Zero shortcut answer marks the run "shortcut"."""
    db_path, mp = env
    mp.setenv("BORG_RULE_ZERO_SHORTCUT", "true")
    stub = StubModels(plan_nodes=[], compose_text="Hello back.").install(mp)
    result = await run_pipeline("hello there", db_path=db_path)
    await drain_log_writes()
    assert result["response"] == "Hello back."
    assert not stub.of_kind("plan")
    assert await _answer_kind(db_path, result["run_id"]) == "shortcut"


@pytest.mark.parametrize("error", [httpx.ConnectError("compose unreachable"),
                                   httpx.ReadTimeout("compose slow")])
@pytest.mark.asyncio
async def test_first_compose_call_raising_salvages_finished_work(env, error):
    """TC-ORC-030-E (overrule): the first compose call raises; the finished
    steps' results are salvaged (BRT-ORC-030, "salvaged"), the run completes."""
    db_path, mp = env
    stub = ComposeSeqStub(plan_nodes=[_per_item()], compose_seq=[error],
                          answer_for=lambda r: {"sentiment": "positive"}).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert result["response"].startswith(engine.SALVAGE_NOTE)
    assert result["response"].count('"sentiment": "positive"') == 3
    assert len(stub.of_kind("compose")) == 1
    assert "BRT-ORC-030" in await _codes(db_path, result["run_id"])
    assert await _answer_kind(db_path, result["run_id"]) == "salvaged"


@pytest.mark.asyncio
async def test_a_plain_answerer_step_is_salvaged_when_compose_fails(env):
    """C10: a plain answerer step (not apply_to_each) that finished comes back
    under the note when compose fails twice."""
    db_path, mp = env
    ComposeSeqStub(plan_nodes=[_ans("Name the capital of France.", id="a")],
                   compose_seq=[{"action": "compose"}, httpx.ConnectError("compose down")],
                   answer_for=lambda r: {"result": "PARIS-SALVAGE-MARKER"}).install(mp)
    result = await run_pipeline("Name the capital of France.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert result["response"].startswith(engine.SALVAGE_NOTE)
    assert "PARIS-SALVAGE-MARKER" in result["response"]
    assert "BRT-ORC-030" in await _codes(db_path, result["run_id"])
    assert await _answer_kind(db_path, result["run_id"]) == "salvaged"


@pytest.mark.asyncio
async def test_first_compose_call_raising_with_nothing_finished_fails(env):
    """TC-ORC-030-F (failure direction): no step finished, the first compose
    call raises: the run fails as before, no answer_kind."""
    db_path, mp = env
    ComposeSeqStub(
        plan_nodes=[_ans("Doomed KKK.", id="a")],
        compose_seq=[httpx.ConnectError("compose down")],
        error_for=lambda r: httpx.ConnectError("down") if r.kind == "answerer" else None,
    ).install(mp)
    result = await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "failed"
    assert "BRT-ORC-001" in str(result.get("error"))
    assert await _answer_kind(db_path, result["run_id"]) is None
