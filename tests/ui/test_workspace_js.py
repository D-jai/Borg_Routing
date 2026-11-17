"""Work order 4.2: behaviour of ui/assets/workspace.js, run under node (harness.js).

Covers the plan diagram's layout (columns by dependency depth, apply_to_each folded
into one node with a count, readable at 12 steps), building steps from a GET /runs/{id}
body, the chat history lane-tag rule (16-Nov ruling "2.2 runtime review"), and the
ruling-18 switch that keeps the baseline estimate off the page.
Skipped only when node is not on PATH.
"""
import json
import uuid

import pytest

from tests.ui.test_app_js import NODE, run_js

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH; workspace.js tests need it")

LOAD = "const {BorgWork: W, Borg} = H.load({extra: ['workspace.js']});"


def js(tmp_path, body):
    # The result travels as ASCII-escaped JSON: the subprocess pipe decodes with the
    # Windows code page, and the page text has curly quotes and ellipses.
    wrapped = (
        LOAD + "const __f = () => {" + body + "};"
        "return JSON.stringify(__f() === undefined ? null : __f())"
        ".replace(/[\\u007f-\\uffff]/g, c => '\\\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));"
    )
    return json.loads(run_js(tmp_path, wrapped))


def step(id_, deps=(), **kw):
    return dict({"id": id_, "deps": list(deps), "label": id_, "type": "answerer"}, **kw)


def layout(tmp_path, steps):
    return js(tmp_path, f"return W.planLayout({json.dumps(steps)});")


def cols(lay):
    return {n["key"]: n["col"] for n in lay["nodes"]}


def overlaps(a, b):
    return not (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
                or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])


def assert_readable(lay):
    nodes = lay["nodes"]
    for i, a in enumerate(nodes):
        assert a["x"] >= 0 and a["y"] >= 0
        assert a["x"] + a["w"] <= lay["width"] and a["y"] + a["h"] <= lay["height"]
        for b in nodes[i + 1:]:
            assert not overlaps(a, b), (a["key"], b["key"])
    # Fixed node size: twelve steps never shrink a node (the page draws at 1:1 and scrolls).
    step_nodes = [n for n in nodes if n["kind"] == "step"]
    assert {(n["w"], n["h"]) for n in step_nodes} == {(lay["sizes"]["NODE_W"], lay["sizes"]["NODE_H"])}
    assert lay["sizes"]["NODE_W"] >= 180


# ---------- columns by dependency depth ----------

def test_columns_follow_dependency_depth(tmp_path):
    lay = layout(tmp_path, [step("n1"), step("n2"), step("n3", ["n1", "n2"]), step("n4", ["n3"])])
    c = cols(lay)
    assert c == {"plan": 0, "step:n1": 1, "step:n2": 1, "step:n3": 2, "step:n4": 3, "compose": 4}
    assert lay["columns"] == 5
    xs = {n["key"]: n["x"] for n in lay["nodes"]}
    assert xs["plan"] < xs["step:n1"] == xs["step:n2"] < xs["step:n3"] < xs["step:n4"] < xs["compose"]
    edges = {(e["from"], e["to"]) for e in lay["edges"]}
    assert edges == {("plan", "step:n1"), ("plan", "step:n2"), ("step:n1", "step:n3"),
                     ("step:n2", "step:n3"), ("step:n3", "step:n4"), ("step:n4", "compose")}


def test_depth_is_the_longest_path_not_the_first_dep(tmp_path):
    # n3 depends on n1 (depth 0) and n2 (depth 1): it sits after n2.
    c = cols(layout(tmp_path, [step("n1"), step("n2", ["n1"]), step("n3", ["n1", "n2"])]))
    assert c["step:n3"] == 3 and c["step:n2"] == 2


def test_no_steps_draws_plan_to_compose(tmp_path):
    lay = layout(tmp_path, [])
    assert [n["kind"] for n in lay["nodes"]] == ["plan", "compose"]
    assert [(e["from"], e["to"]) for e in lay["edges"]] == [("plan", "compose")]


def test_a_dependency_cycle_is_drawn_not_looped(tmp_path):
    lay = layout(tmp_path, [step("a", ["b"]), step("b", ["a"])])
    assert {n["key"] for n in lay["nodes"]} == {"plan", "step:a", "step:b", "compose"}


# ---------- readable at 12 steps ----------

@pytest.mark.parametrize("shape", ["parallel", "chain", "mixed"])
def test_twelve_steps_stay_readable(tmp_path, shape):
    if shape == "parallel":
        steps = [step(f"s{i}") for i in range(12)]
    elif shape == "chain":
        steps = [step("s0")] + [step(f"s{i}", [f"s{i-1}"]) for i in range(1, 12)]
    else:
        steps = [step(f"s{i}", [] if i < 4 else [f"s{i-4}"]) for i in range(12)]
    lay = layout(tmp_path, steps)
    assert sum(1 for n in lay["nodes"] if n["kind"] == "step") == 12
    assert_readable(lay)


def test_labels_from_run_data_fit_their_node(tmp_path):
    long = "Read every one of the support emails and classify each by urgency and topic " * 3
    data = run_body([env("n1", instruction=long)])
    steps = js(tmp_path, f"return W.stepsFromRun({json.dumps(data)});")
    assert len(steps[0]["label"]) <= 30


# ---------- steps from GET /runs/{id} ----------

def env(node_id, type_="answerer", routing=True, instruction=None, trigger="initial_plan",
        envelope_id=None, upstream=None, **kw):
    eid = envelope_id or str(uuid.uuid4())
    out = {"instruction": instruction or f"Do {node_id}.", "data_scope": "derived", "dependency_input": {}}
    if upstream:
        out["dependency_input"]["upstream_results"] = {u: {} for u in upstream}
    e = {"envelope_id": eid, "type": type_, "trigger_reason": trigger, "status": "resolved",
         "model_id": "openrouter/stub/cheap", "outbound_payload": json.dumps(out),
         "inbound_payload": json.dumps({"status": "resolved", "output": {}}),
         "tokens_input": 10, "tokens_output": 5, "dollars": 0.001, "latency_ms": 1200, "skill_id": None,
         "routing": ({"mode": "route", "node_id": node_id, "chosen": "openrouter/stub/cheap", "reason": "routed",
                      "task_type": "extraction", "tier": "light", "floor": 6.0, "candidates": []} if routing else None)}
    e.update(kw)
    return e


def plan_event(nodes, mode="plan"):
    return {"event_type": "orchestrator_decision", "envelope_id": None, "model_id": "openrouter/stub/top",
            "dollars": 0.01, "tokens_input": 100, "tokens_output": 50, "created_at": "2026-11-17 10:00:01",
            "decision_json": json.dumps({"mode": mode, "parsed": {"plan": {"nodes": nodes}}})}


def run_body(envs, events=(), **kw):
    body = {"run": {"run_id": str(uuid.uuid4()), "status": "completed", "created_at": "2026-11-17 10:00:00",
                    "completed_at": "2026-11-17 10:00:41", "user_instruction": "x"},
            "envelopes": envs, "child_envelopes": [], "events": list(events), "digest": None, "outbound": []}
    body.update(kw)
    return body


def steps_of(tmp_path, data):
    return js(tmp_path, f"return W.stepsFromRun({json.dumps(data)}).map(s => ({{id: s.id, deps: s.deps, items: s.items}}));")


def test_depends_on_comes_from_the_plan_event(tmp_path):
    data = run_body([env("n1"), env("n2"), env("n3")], [plan_event([
        {"id": "n1"}, {"id": "n2"}, {"id": "n3", "depends_on": ["n1", "n2"]}])])
    assert steps_of(tmp_path, data) == [
        {"id": "n1", "deps": [], "items": None}, {"id": "n2", "deps": [], "items": None},
        {"id": "n3", "deps": ["n1", "n2"], "items": None}]


def test_refs_count_as_dependencies_and_re_plan_steps_join(tmp_path):
    data = run_body([env("n1"), env("n2")], [
        plan_event([{"id": "n1"}]),
        plan_event([{"id": "n2", "type": "apply_to_each", "inputs_ref": "n1"}], mode="re_plan")])
    assert [s["deps"] for s in steps_of(tmp_path, data)] == [[], ["n1"]]


def test_without_plan_text_depends_on_comes_from_the_envelope(tmp_path):
    # Lane on, not admin: the plan event's text is dropped; the envelope still names its inputs.
    hidden = dict(plan_event([]), decision_json=None)
    data = run_body([env("n1"), env("n2", upstream=["n1"])], [hidden])
    assert [s["deps"] for s in steps_of(tmp_path, data)] == [[], ["n1"]]


def test_unknown_dependency_names_are_dropped(tmp_path):
    data = run_body([env("n1")], [plan_event([{"id": "n1", "depends_on": ["ghost", "n1"]}])])
    assert steps_of(tmp_path, data)[0]["deps"] == []


def test_apply_to_each_is_one_node_with_its_item_count(tmp_path):
    parent = env("n1", type_="apply_to_each")
    parent["routing"]["items"] = 200
    data = run_body([parent, env("n2", upstream=["n1"])])
    steps = steps_of(tmp_path, data)
    assert steps[0] == {"id": "n1", "deps": [], "items": 200}
    lay = js(tmp_path, f"return W.planLayout(W.stepsFromRun({json.dumps(data)}));")
    assert sum(1 for n in lay["nodes"] if n["kind"] == "step") == 2


def test_apply_to_each_item_envelopes_fold_into_the_parent(tmp_path):
    pid = str(uuid.uuid4())
    parent = env("n1", type_="apply_to_each", envelope_id=pid)
    kids = [env(None, routing=False, trigger=f"apply_to_each:{pid}:item_{i}") for i in range(3)]
    steps = steps_of(tmp_path, run_body([parent] + kids))
    assert steps == [{"id": "n1", "deps": [], "items": 3}]


# ---------- totals: digest is the run total; child envelopes are never added ----------

def test_cost_is_the_digest_total_and_child_dollars_are_not_added(tmp_path):
    data = run_body([env("n1")], [plan_event([{"id": "n1"}])],
                    digest={"cost_dollars": 0.05},
                    child_envelopes=[dict(env("c1"), dollars=9.0, run_id="child")])
    t = js(tmp_path, f"return W.runTotals({json.dumps(data)}, () => false);")
    assert t["cost"] == 0.05 and t["costSoFar"] is None
    assert t["seconds"] == 41 and t["steps"] == 1


def test_before_a_digest_cost_is_this_runs_logged_dollars_so_far(tmp_path):
    data = run_body([env("n1")], [plan_event([{"id": "n1"}])])
    t = js(tmp_path, f"return W.runTotals({json.dumps(data)}, () => true);")
    assert t["cost"] is None and t["costSoFar"] == pytest.approx(0.01)


def totals(tmp_path, data):
    return js(tmp_path, f"return W.runTotals({json.dumps(data)});")


def test_calls_that_left_are_the_servers_figure_and_unknown_is_never_zero(tmp_path):
    base = run_body([env("n1")], [plan_event([{"id": "n1"}])])
    t = totals(tmp_path, dict(base, calls_left_machine={"count": 2, "source": "ledger"}))
    assert (t["left"], t["leftSource"]) == (2, "ledger")
    t = totals(tmp_path, dict(base, calls_left_machine={"count": 0, "source": "locality"}))
    assert (t["left"], t["leftSource"]) == (0, "locality")          # a real zero stays a zero
    for unknown in ({"count": None, "source": None}, None):
        t = totals(tmp_path, dict(base, calls_left_machine=unknown))
        assert (t["left"], t["leftSource"]) == (None, "unknown")
    assert totals(tmp_path, base)["left"] is None                   # an older server: not recorded


def test_ruling_18_baseline_estimate_renders_nothing(tmp_path):
    data = run_body([env("n1", baseline_dollars=0.5)], digest={"cost_dollars": 0.05, "baseline_dollars": 0.5})
    assert js(tmp_path, f"return W.baselineEstimate({json.dumps(data)});") is None


# ---------- chat history lane tag ----------

TURNS = [
    {"role": "user", "content": "u1", "lane": "off"},
    {"role": "assistant", "content": "a1", "lane": "off"},
    {"role": "user", "content": "u2 secret", "lane": "all_local"},
    {"role": "assistant", "content": "a2 secret", "lane": "all_local"},
    {"role": "user", "content": "u3", "lane": "raw_local"},
    {"role": "assistant", "content": "a3", "lane": "by_reference"},
    {"role": "user", "content": "u4 untagged"},
    {"role": "user", "content": "u5 failed", "lane": "off", "failed": True},
]


def history(tmp_path, mode):
    out = js(tmp_path, f"return W.historyForMode({json.dumps(TURNS)}, {json.dumps(mode)});")
    return [t["content"] for t in out]


@pytest.mark.parametrize("mode", ["off", "raw_local", "by_reference"])
def test_all_local_turns_are_not_resent_under_another_mode(tmp_path, mode):
    assert history(tmp_path, mode) == ["u1", "a1", "u3", "a3"]


def test_all_local_turns_are_resent_while_the_mode_is_all_local(tmp_path):
    assert history(tmp_path, "all_local") == ["u1", "a1", "u2 secret", "a2 secret", "u3", "a3", "u4 untagged"]


@pytest.mark.parametrize("mode", [None, "", "bogus"])
def test_unknown_mode_fails_closed(tmp_path, mode):
    assert history(tmp_path, mode) == ["u1", "a1", "u3", "a3"]


def test_history_sends_role_and_content_only(tmp_path):
    out = js(tmp_path, f"return W.historyForMode({json.dumps(TURNS)}, 'off');")
    assert all(set(t) == {"role", "content"} for t in out)


def test_new_turns_are_tagged_with_the_mode_or_all_local_when_unknown(tmp_path):
    out = js(tmp_path, "return ['off','raw_local','all_local','by_reference',null,'x'].map(W.laneTag);")
    assert out == ["off", "raw_local", "all_local", "by_reference", "all_local", "all_local"]


def test_lane_line_says_which_mode_is_on(tmp_path):
    out = js(tmp_path, "return ['off','raw_local','all_local','by_reference',null].map(m => W.laneLine(m));")
    assert out[0]["docs"] is False and "off" in out[0]["text"].lower()
    assert "raw_local" in out[1]["text"] and out[1]["docs"]
    assert "all_local" in out[2]["text"]
    assert "no privacy promise" in out[3]["text"]
    assert out[4]["docs"] is False


# ---------- plain words ----------

def test_failure_reason_in_plain_words(tmp_path):
    out = js(tmp_path, "return W.failureWords('BRT-PIP-008: run exceeded wall-clock budget (500s).');")
    assert out["code"] == "BRT-PIP-008"
    assert "time limit" in out["words"]
    assert out["detail"].startswith("run exceeded")
    assert js(tmp_path, "return W.failureWords('');") is None


def test_timeline_sentences_group_calls_and_say_what_happened(tmp_path):
    e1 = env("n1")
    calls = [{"event_type": "model_call", "envelope_id": e1["envelope_id"], "model_id": "openrouter/stub/cheap",
              "dollars": 0.0, "tokens_input": 5, "created_at": "2026-11-17 10:00:03", "decision_json": None}] * 3
    compose = dict(plan_event([]), decision_json=json.dumps({"mode": "compose"}), created_at="2026-11-17 10:00:39")
    hidden = {"event_type": "error", "envelope_id": None, "model_id": None, "created_at": "2026-11-17 10:00:40", "decision_json": None}
    data = run_body([e1], [plan_event([{"id": "n1"}])] + calls + [compose, hidden])
    out = js(tmp_path, f"return W.eventSentences({json.dumps(data)});")
    texts = [o["text"] for o in out]
    assert texts[0] == "The planner (top) split the job into 1 step."
    assert texts[1].startswith("3 calls went to cheap")
    assert texts[2] == "top wrote the final answer."
    assert "hidden" in texts[3]
    assert [o["at"] for o in out] == ["0:01", "0:03", "0:39", "0:40"]
