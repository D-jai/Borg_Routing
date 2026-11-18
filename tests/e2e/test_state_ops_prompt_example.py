"""Planner ruling 25: the engine runs the state ops the planner prompt teaches.

The planner prompt's own example ("2000 emails"): an apply_to_each step gives
``{bucket, topic}`` per item, a state_op groups by bucket with counts, a
state_op samples 3 per bucket, and one answerer summarises the sample. Its
rows are apply_to_each rows (``{index, item, output: {...}}``), so a field is
read from the row's ``output`` first.

Then the failing cases of ruling 25e (an unknown op, a missing param, an
unknown param: each fails, naming the op's params within the 200 characters
re_plan is shown) and the job 1 plan as recorded in the demo database
(run 19f56430: group_by with ``{"group_by": ..., "count": true}``, and a
second one with ``order`` and ``limit``, which no op takes).

The real engine, executor and router run against the scripted stand-in
model server. Nothing here needs a key or the network.
"""
import json
import re
import sqlite3

import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

EMAILS = {
    "Email 1: invoice is wrong": ("billing", "wrong invoice"),
    "Email 2: charged twice": ("billing", "double charge"),
    "Email 3: refund please": ("billing", "refund"),
    "Email 4: card declined": ("billing", "card declined"),
    "Email 5: app crashes": ("bug", "crash"),
    "Email 6: login loops": ("bug", "login loop"),
    "Email 7: sync stuck": ("bug", "sync"),
    "Email 8: love the update": ("praise", "update"),
}
APE_INSTRUCTION = "Classify this email into a bucket and a topic of at most five words."
SUMMARY_INSTRUCTION = "Summarise these representative rows into overall themes."


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state_ops_prompt.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-state-ops")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _classify(req):
    for text, (bucket, topic) in EMAILS.items():
        if text in req.system or text in req.user:
            return {"bucket": bucket, "topic": topic}
    return {"theme": "billing pain, bugs, one happy user"}


def _prompt_example_plan():
    return [
        {"type": "apply_to_each", "id": "n1", "instruction": APE_INSTRUCTION,
         "items": list(EMAILS), "output_schema": {"bucket": "string", "topic": "string"},
         "tier": "light", "task_type": "extraction"},
        {"type": "state_op", "id": "n2", "op": "group_by", "source": "n1",
         "params": {"group_by": "bucket", "count": True}, "depends_on": ["n1"]},
        {"type": "state_op", "id": "n3", "op": "sample", "source": "n1",
         "params": {"group_by": "bucket", "n": 3, "seed": 7}, "depends_on": ["n1"]},
        {"type": "answerer", "id": "n4", "instruction": SUMMARY_INSTRUCTION,
         "depends_on": ["n3"], "tier": "light", "task_type": "summarize"},
    ]


def _state_op_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT outbound_payload, inbound_payload FROM envelopes "
            "WHERE type='state_op' ORDER BY rowid"
        ).fetchall()
    out = {}
    for outbound, inbound in rows:
        node_id = (json.loads(outbound or "null") or {}).get("node_id")
        out[node_id] = json.loads(inbound or "null") or {}
    return out


@pytest.mark.asyncio
async def test_prompt_example_runs_end_to_end(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=_prompt_example_plan(), answer_for=_classify).install(mp)
    result = await run_pipeline("Sort these emails and summarise the themes.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")

    sops = _state_op_rows(db_path)
    assert sops["n2"].get("status") == "resolved", sops["n2"]
    assert sops["n2"]["output"]["result"] == {"billing": 4, "bug": 3, "praise": 1}

    assert sops["n3"].get("status") == "resolved", sops["n3"]
    sample = sops["n3"]["output"]["result"]
    assert isinstance(sample, list) and len(sample) == 3 + 3 + 1, sample
    # Rows keep their full shape, and come from every bucket.
    assert all({"index", "item", "output"} <= set(r) for r in sample), sample
    per_bucket: dict = {}
    for r in sample:
        per_bucket[r["output"]["bucket"]] = per_bucket.get(r["output"]["bucket"], 0) + 1
    assert per_bucket == {"billing": 3, "bug": 3, "praise": 1}

    # The one answerer reads the sample, not the 8 emails.
    summary = [r for r in stub.of_kind("answerer") if SUMMARY_INSTRUCTION in r.system]
    assert len(summary) == 1
    text = summary[0].system + summary[0].user
    for r in sample:
        assert r["item"] in text


@pytest.mark.asyncio
async def test_sample_reads_a_grouped_source(env):
    """Ruling 25d: sample over a group_by step (rows per group) takes n from each."""
    db_path, mp = env
    plan = _prompt_example_plan()[:1] + [
        {"type": "state_op", "id": "g", "op": "group_by", "source": "n1",
         "params": {"key": "output.bucket"}, "depends_on": ["n1"]},
        {"type": "state_op", "id": "s", "op": "sample", "source": "g",
         "params": {"n": 2}, "depends_on": ["g"]},
    ]
    StubModels(plan_nodes=plan, answer_for=_classify).install(mp)
    result = await run_pipeline("Sort these emails.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    sops = _state_op_rows(db_path)
    groups = sops["g"]["output"]["result"]
    assert sorted(groups) == ["billing", "bug", "praise"]
    assert all(isinstance(v, list) and {"index", "output"} <= set(v[0]) for v in groups.values())
    sample = sops["s"]["output"]["result"]
    assert len(sample) == 2 + 2 + 1, sample
    assert all({"index", "item", "output"} <= set(r) for r in sample)


def _replan_messages(stub):
    replans = stub.of_kind("re_plan")
    assert replans, "re_plan was not called"
    return replans[0].system + replans[0].user


def _failure_message(stub, node_id):
    """The failure message re_plan is shown for one step (200 characters at most)."""
    replans = stub.of_kind("re_plan")
    assert replans, "re_plan was not called"
    body = replans[0].system + "\n" + replans[0].user
    hit = re.search(
        r'"' + re.escape(node_id) + r'": \{.*?"failure": \{.*?"message": ("(?:[^"\\]|\\.)*")',
        body, re.DOTALL,
    )
    assert hit, f"no failure message for {node_id} in re_plan"
    return json.loads(hit.group(1))


@pytest.mark.parametrize("node, must_name", [
    ({"op": "sort", "params": {"key": "bucket"}},
     ["BRT-SOP-001", "filter", "count", "group_by", "sample", "negate", "seed"]),
    ({"op": "sample", "params": {}}, ["BRT-SOP-002", "n", "key", "seed"]),
    ({"op": "group_by", "params": {"field": "bucket"}},
     ["BRT-SOP-006", "field", "key", "group_by", "count"]),
    ({"op": "filter", "params": {"key": "bucket", "value": "bug", "limit": 2}},
     ["BRT-SOP-006", "limit", "key", "value", "negate"]),
])
@pytest.mark.asyncio
async def test_bad_op_or_param_fails_naming_the_params(env, node, must_name):
    db_path, mp = env
    plan = _prompt_example_plan()[:1] + [
        {"type": "state_op", "id": "bad", "source": "n1", "depends_on": ["n1"], **node},
    ]
    stub = StubModels(plan_nodes=plan, answer_for=_classify).install(mp)
    await run_pipeline("Sort these emails.", db_path=db_path)
    await drain_log_writes()
    assert _state_op_rows(db_path)["bad"].get("status") == "failed"
    message = _failure_message(stub, "bad")
    assert len(message) <= 200
    for word in must_name:
        assert word in message, (word, message)


# The job 1 plan as recorded (demo database, run 19f56430), with five items.
JOB1_LINES = [
    "1. The Tidewell app reminds me to drink water and I actually do it now, love it.",
    "2. Syncing with my bottle takes forever, sometimes it never connects.",
    "3. Battery on the bottle lasts about two weeks, which is fine.",
    "4. The streak badges are a fun touch but the sound is too loud.",
    "5. Setup was confusing, I had to scan the code four times.",
]
JOB1_ANSWERS = {
    "1. The Tidewell": {"line_number": 1, "sentiment": "positive", "feature": "reminders"},
    "2. Syncing": {"line_number": 2, "sentiment": "negative", "feature": "sync"},
    "3. Battery": {"line_number": 3, "sentiment": "neutral", "feature": "battery"},
    "4. The streak": {"line_number": 4, "sentiment": "mixed", "feature": "streak badges"},
    "5. Setup": {"line_number": 5, "sentiment": "negative", "feature": "setup"},
}


def _job1_answer(req):
    for prefix, out in JOB1_ANSWERS.items():
        if prefix in req.system or prefix in req.user:
            return out
    return {"result": "?"}


def _job1_plan():
    return [
        {"type": "apply_to_each", "id": "n1", "skill_id": "extract_fields",
         "skill_version": 1,
         "skill_args": {
             "items_ref": None, "items": JOB1_LINES,
             "fields": {
                 "line_number": "integer",
                 "sentiment": "string (one of: positive, negative, neutral, mixed)",
                 "feature": "string (the single main product feature mentioned, or 'none')",
             }},
         "concurrency_limit": 20, "tier": "light", "task_type": "extraction",
         "output_schema": {"line_number": "integer", "sentiment": "string",
                           "feature": "string"}},
        {"type": "state_op", "id": "n2", "op": "group_by", "source": "n1",
         "params": {"group_by": "sentiment", "count": True}, "depends_on": ["n1"]},
        {"type": "state_op", "id": "n3", "op": "group_by", "source": "n1",
         "params": {"group_by": "feature", "count": True, "order": "count desc",
                    "limit": 5},
         "depends_on": ["n1"]},
    ]


@pytest.mark.asyncio
async def test_job1_plan_as_recorded(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=_job1_plan(), answer_for=_job1_answer).install(mp)
    result = await run_pipeline("Classify each feedback line.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    sops = _state_op_rows(db_path)
    # n2: counts per sentiment, read from each row's output.
    assert sops["n2"].get("status") == "resolved", sops["n2"]
    assert sops["n2"]["output"]["result"] == {
        "positive": 1, "negative": 2, "neutral": 1, "mixed": 1}
    # n3: order and limit are not group_by params; never silently ignored.
    assert sops["n3"].get("status") == "failed", sops["n3"]
    message = _failure_message(stub, "n3")
    for word in ("BRT-SOP-006", "order", "limit", "key", "group_by", "count"):
        assert word in message, (word, message)
    assert "BRT-SOP-005" not in _replan_messages(stub)


# --- Review of 1071659, folded into ruling 25 --------------------------------

def _error_codes(db_path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT decision_json FROM logs WHERE event_type='error' ORDER BY rowid"
        ).fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


@pytest.mark.asyncio
async def test_state_op_over_a_failed_apply_to_each_fails_not_zero(env):
    """A failed apply_to_each leaves ``output.results == []``; a count over it
    used to resolve with 0. It now fails with BRT-APE-005, naming the step."""
    db_path, mp = env
    plan = [
        {"type": "apply_to_each", "id": "n1", "instruction": APE_INSTRUCTION, "items": []},
        {"type": "state_op", "id": "n2", "op": "count", "source": "n1", "depends_on": ["n1"]},
    ]
    StubModels(plan_nodes=plan, answer_for=_classify).install(mp)
    await run_pipeline("Count the emails.", db_path=db_path)
    await drain_log_writes()
    assert "n2" not in _state_op_rows(db_path)  # never ran on []
    dead = [e for e in _error_codes(db_path) if e.get("code") == "BRT-APE-005"]
    assert [e.get("node_id") for e in dead] == ["n2"], dead


@pytest.mark.asyncio
async def test_state_op_inputs_ref_is_its_source(env):
    """``inputs_ref`` on a state_op is read as its ``source`` (the ready check
    and the failed-dependency check already treat it as an input ref)."""
    db_path, mp = env
    plan = _prompt_example_plan()[:1] + [
        {"type": "state_op", "id": "c", "op": "count", "inputs_ref": "n1",
         "depends_on": ["n1"]},
    ]
    StubModels(plan_nodes=plan, answer_for=_classify).install(mp)
    await run_pipeline("Count the emails.", db_path=db_path)
    await drain_log_writes()
    assert _state_op_rows(db_path)["c"]["output"]["result"] == {"count": len(EMAILS)}


@pytest.mark.asyncio
async def test_state_op_inputs_ref_to_a_failed_step_fails(env):
    db_path, mp = env
    plan = [
        {"type": "apply_to_each", "id": "n1", "instruction": APE_INSTRUCTION, "items": []},
        {"type": "state_op", "id": "c", "op": "count", "inputs_ref": "n1",
         "depends_on": ["n1"]},
    ]
    StubModels(plan_nodes=plan, answer_for=_classify).install(mp)
    await run_pipeline("Count the emails.", db_path=db_path)
    await drain_log_writes()
    assert "c" not in _state_op_rows(db_path)
    dead = [e for e in _error_codes(db_path) if e.get("code") == "BRT-APE-005"]
    assert [e.get("node_id") for e in dead] == ["c"], dead
