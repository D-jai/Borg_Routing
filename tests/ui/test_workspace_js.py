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


def test_ruling_22_the_displayed_floor_never_exceeds_the_stored_value(tmp_path):
    values = [0.092993, 0.6012, 0.09, 0.0999999, 0.0001239, 1.2399, 12.999, 0.5, 0.000001, 0.1, 0.3]
    shown = js(tmp_path, f"return {json.dumps(values)}.map(v => W.floorMoney(v));")
    for v, text in zip(values, shown):
        assert text.startswith("$")
        assert float(text[1:]) <= v, (v, text)
    assert shown[0] == "$0.0929"


def test_ruling_22_an_exact_floor_is_shown_whole_not_one_unit_low(tmp_path):
    """Rounding down must not drop a digit to float error: 1.15 * 100 is 114.999... in floating point."""
    values = [1.15, 4.35, 2.01, 0.0012, 0.00104, 0.6012]
    shown = js(tmp_path, f"return {json.dumps(values)}.map(v => W.floorMoney(v));")
    assert shown == ["$1.15", "$4.35", "$2.01", "$0.00120", "$0.00104", "$0.601"]


FLOOR_DIGEST = {"cost_dollars": 0.05, "direct_floor_dollars": 0.092993,
                "direct_floor_model": "openrouter/anthropic/claude-opus-5.5", "direct_floor_ratio": [1.3, 4.4]}


def test_ruling_22_the_old_baseline_alone_renders_nothing(tmp_path):
    data = run_body([env("n1", baseline_dollars=0.5)], digest={"cost_dollars": 0.05, "baseline_dollars": 0.5})
    assert js(tmp_path, f"return W.baselineEstimate({json.dumps(data)});") is None


def test_ruling_22_the_receipt_line_is_one_pass_at_least_with_what_it_leaves_out(tmp_path):
    data = run_body([env("n1")], digest=FLOOR_DIGEST)
    out = js(tmp_path, f"""const d = {json.dumps(data)}; const r = W.receiptFrom(d);
        return {{stats: W.receiptStats(r), note: W.floorNote(r.estimate), tiles: W.headerTiles(d)}};""")
    line = [p for p in out["stats"] if p[0].startswith("One pass on")][0]
    assert line[0] == "One pass on claude-opus-5.5: at least" and line[1] == "US$0.0929"
    assert "retries" in out["note"] and "JSON" in out["note"] and "1.3" in out["note"] and "4.4" in out["note"]
    assert "retry that was thrown away" in out["note"]
    assert "17" not in out["note"] and "23%" not in out["note"]
    assert "would have cost" not in json.dumps(out).lower()
    assert any("One pass on claude-opus-5.5: at least US$0.0929" in (t.get("sub") or "") for t in out["tiles"])


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


# ---------- planner ruling 23: packed calls ----------

def _item_event(env_id, step_id, call, first, accepted=True, model="openrouter/stub/cheap"):
    pack = {"call": call, "step": step_id, "size": 20, "position": 0, "cut": False,
            "accepted": accepted, "first": first, "share_picodollars": 1, "share_baseline_picodollars": 2}
    return {"event_type": "model_call", "envelope_id": env_id, "model_id": model, "dollars": 0.0001,
            "tokens_input": 5, "tokens_output": 2, "created_at": "2026-11-17 10:00:03",
            "decision_json": json.dumps({"pack": pack})}


def _outcome(step_id, items, accepted, alone):
    return {"event_type": "orchestrator_decision", "envelope_id": step_id, "model_id": "openrouter/stub/cheap",
            "dollars": None, "tokens_input": None, "created_at": "2026-11-17 10:00:04",
            "decision_json": json.dumps({"mode": "pack_outcome", "step": step_id, "call": "c",
                                         "items": items, "accepted": accepted, "alone": alone})}


def test_ruling_23_a_packed_post_counts_as_one_call(tmp_path):
    pid = str(uuid.uuid4())
    parent = env("n1", type_="apply_to_each", envelope_id=pid)
    parent["routing"]["items"] = 40
    events = [plan_event([{"id": "n1"}])]
    for call in ("c1", "c2"):
        events += [_item_event(str(uuid.uuid4()), pid, call, first=(k == 0)) for k in range(20)]
    data = run_body([parent], events)
    t = totals(tmp_path, data)
    assert t["calls"] == 1 + 2            # the plan call and two packed posts
    by_model = js(tmp_path, f"return W.callsByModel ? W.callsByModel({json.dumps(data)}) : null;")
    if by_model is not None:
        assert {m["model"]: m["calls"] for m in by_model}["openrouter/stub/cheap"] == 2
    sentences = [o["text"] for o in js(tmp_path, f"return W.eventSentences({json.dumps(data)});")]
    assert any(s.startswith("2 calls went to cheap") for s in sentences), sentences


def test_ruling_23_the_items_line_says_n_items_in_m_calls(tmp_path):
    pid = str(uuid.uuid4())
    parent = env("n1", type_="apply_to_each", envelope_id=pid)
    parent["routing"]["items"] = 45
    events = [plan_event([{"id": "n1"}]),
              _outcome(pid, 20, 20, 0), _outcome(pid, 20, 18, 2), _outcome(pid, 5, 5, 0)]
    data = run_body([parent], events)
    line = js(tmp_path, f"return W.itemsLine(W.stepsFromRun({json.dumps(data)})[0]);")
    assert line == "45 items in 5 calls; 2 retried alone"     # 3 packed posts + (45 - 43) single calls


def test_ruling_23_with_packing_off_each_item_is_its_own_call(tmp_path):
    parent = env("n1", type_="apply_to_each")
    parent["routing"]["items"] = 7
    line = js(tmp_path, f"return W.itemsLine(W.stepsFromRun({json.dumps(run_body([parent]))})[0]);")
    assert line == "7 items in 7 calls"


# ---------- planner ruling 26a: an answer with no step behind it ----------

def _no_step_body(kind, codes=("BRT-ORC-004", "BRT-REG-020")):
    event = {"event_type": "error", "run_id": "r", "decision_json": json.dumps({
        "code": "BRT-ORC-033", "severity": "warning", "failed_steps": len(codes),
        "step_codes": list(codes)})}
    body = run_body([env("n1", status="failed")], events=[event] if kind == "planner_only" else [])
    body["run"]["answer_kind"] = kind
    return body


def test_ruling_26a_receipt_says_the_planner_wrote_it_alone(tmp_path):
    data = _no_step_body("planner_only")
    out = js(tmp_path, f"""const d = {json.dumps(data)}; const r = W.receiptFrom(d);
        return {{note: r.noStep, direct: W.noStepNote(d)}};""")
    assert out["note"] == out["direct"]
    note = out["note"]
    assert "Every planned step failed (2 of 2)" in note
    assert "the planning model wrote this answer alone" in note
    assert "BRT-ORC-004, BRT-REG-020" in note


def test_ruling_26a_a_composed_answer_has_no_note(tmp_path):
    data = _no_step_body("composed")
    assert js(tmp_path, f"return W.receiptFrom({json.dumps(data)}).noStep;") is None


def test_ruling_26a_raw_step_codes_show_code_and_document_only(tmp_path):
    data = _no_step_body("planner_only", codes=({"code": "BRT-LANE-002", "doc": "doc_001"},))
    note = js(tmp_path, f"return W.noStepNote({json.dumps(data)});")
    assert "BRT-LANE-002 (document doc_001)" in note and "(1 of 1)" in note


# ---------- planner ruling 26e: failed steps (and items) on the receipt ----------

def _agg_env(node_id, ok, bad, status="resolved"):
    out = {"results": [{"index": i, "item": i, "output": {}} for i in range(ok)],
           "failures": [{"index": ok + i, "item": ok + i, "reason": "BRT-ANS-005"} for i in range(bad)],
           "total": ok + bad, "success_count": ok, "failure_count": bad}
    return env(node_id, type_="apply_to_each",
               inbound_payload=json.dumps({"status": status, "output": out}))


def test_ruling_26e_receipt_line_k_of_n_steps_failed_only_when_k_above_zero(tmp_path):
    def lines(envs):
        data = run_body(envs)
        return js(tmp_path, f"return W.failedLines(W.receiptFrom({json.dumps(data)}));")

    # Nothing failed: no line.
    assert lines([env("n1"), _agg_env("n2", 3, 0)]) == []
    # A failed step, an apply_to_each whose every item failed (26f), and one
    # with some failed items (not a failed step; its items are counted).
    got = lines([env("n1", status="failed"), _agg_env("n2", 0, 2), _agg_env("n3", 3, 1),
                 env("n4")])
    assert got == ["2 of 4 steps failed", "1 of 4 items failed"]


def test_ruling_26a_note_for_a_redacted_viewer_leaves_out_the_count(tmp_path):
    """Lane on, non-admin: an event with no readable decision never says '0 of 0'."""
    data = _no_step_body("planner_only")
    data["events"][0]["decision_json"] = None
    note = js(tmp_path, f"return W.noStepNote({json.dumps(data)});")
    assert note.startswith("Every planned step failed, so the planning model wrote this answer alone")
    assert "0 of 0" not in note and "codes" not in note


# ---------- round 2 of 82d3da4: steps failed before any call are counted ----------

def _err(code, node_id, **kw):
    return {"event_type": "error", "run_id": "r",
            "decision_json": json.dumps(dict({"code": code, "node_id": node_id}, **kw))}


def test_receipt_counts_steps_failed_before_any_call(tmp_path):
    def lines(envs, events):
        data = run_body(envs, events=events)
        return js(tmp_path, f"return W.failedLines(W.receiptFrom({json.dumps(data)}));")

    # A bad state_op (its envelope failed), then one reading it: failed before
    # any call (BRT-APE-005), so it has an event and no envelope.
    assert lines([env("n1", type_="state_op", status="failed")],
                 [_err("BRT-APE-005", "n2")]) == ["2 of 2 steps failed"]
    # One step refused in an otherwise finished run; a warning is not a failure.
    assert lines([env("n1"), env("n2")],
                 [_err("BRT-LANE-002", "n3"), _err("BRT-ORC-019", "n1", severity="warning")]
                 ) == ["1 of 3 steps failed"]


# ---------- ruling 28h: a model with no speed figure has a null cost ----------

def test_why_rows_ignore_null_costs_and_label_a_cost_ranked_pick_by_cost(tmp_path):
    routing = {"chosen": "cloud", "reason": "routed", "floor": 6.0,
               "notes": ["speed not measured: ranked after priced models"], "candidates": [
                   {"model_id": "cloud", "score": 7.0, "est_cost": 0.004, "est_dollars": 0.003, "est_time_cost": 0.001},
                   {"model_id": "local", "score": 9.0, "est_cost": None, "est_dollars": 0.0, "est_time_cost": None}]}
    rows = js(tmp_path, f"return W.whyRows({json.dumps(routing)});")
    assert "lowest effective cost" in rows[0]["note"]
    assert "alias or strategy" not in rows[0]["note"]
    assert rows[1]["est_cost"] is None and rows[1]["note"] == ""


def test_why_rows_label_a_ranked_after_pick_when_no_priced_model_qualifies(tmp_path):
    routing = {"chosen": "local", "reason": "routed", "floor": 6.0, "candidates": [
        {"model_id": "local", "score": 9.0, "est_cost": None, "est_dollars": 0.0},
        {"model_id": "local2", "score": 8.0, "est_cost": None, "est_dollars": 0.0}]}
    rows = js(tmp_path, f"return W.whyRows({json.dumps(routing)});")
    assert "ranked after priced models" in rows[0]["note"]
    assert "alias or strategy" not in rows[0]["note"] and "lowest effective cost" not in rows[0]["note"]


def test_a_redacted_speed_probe_warning_reads_as_a_warning_with_its_code(tmp_path):
    warn = {"event_type": "error", "envelope_id": None, "model_id": "ollama/llama3.2:3b",
            "created_at": "2026-11-17 10:00:02",
            "decision_json": json.dumps({"code": "BRT-REG-016", "severity": "warning", "model_id": "ollama/llama3.2:3b"})}
    data = run_body([], [plan_event([]), warn])
    texts = [o["text"] for o in js(tmp_path, f"return W.eventSentences({json.dumps(data)});")]
    line = next(t for t in texts if "BRT-REG-016" in t)
    assert line.startswith("Warning") and "speed" in line
    assert not any("A problem was recorded" in t for t in texts)


def test_receipt_has_one_line_per_field_a_plan_asked_for_and_the_skill_did_not_return(tmp_path):
    """Ruling 32c: the BRT-ORC-035 plan note (second hit) becomes receipt lines."""
    note = {"event_type": "orchestrator_decision", "decision_json": json.dumps({
        "code": "BRT-ORC-035", "action": "ran_without_fields", "node_id": "n1",
        "skill_id": "classify_into_buckets", "fields": ["file", "urgency"]})}
    other = {"event_type": "orchestrator_decision", "decision_json": json.dumps({"mode": "route"})}
    got = js(tmp_path, f"return W.notProducedLines({json.dumps({'events': [other, note]})});")
    assert got == [
        "the plan asked for file from classify_into_buckets, which does not return it; not produced",
        "the plan asked for urgency from classify_into_buckets, which does not return it; not produced",
    ]
    assert js(tmp_path, f"return W.notProducedLines({json.dumps({'events': [other]})});") == []


def test_receipt_has_one_line_per_key_the_plan_gave_that_nothing_takes(tmp_path):
    """Ruling 32d: the BRT-ORC-036 plan note (second hit) becomes receipt lines,
    from the skill for a skill_args key, from the step for a node key."""
    skill = {"event_type": "orchestrator_decision", "decision_json": json.dumps({
        "code": "BRT-ORC-036", "action": "ran_without_fields", "node_id": "n2",
        "skill_id": "classify_into_buckets", "fields": ["instruction"]})}
    node = {"event_type": "orchestrator_decision", "decision_json": json.dumps({
        "code": "BRT-ORC-036", "action": "ran_without_fields", "node_id": "n2",
        "skill_id": "", "fields": ["colour"]})}
    unused = {"event_type": "orchestrator_decision", "decision_json": json.dumps({
        "action": "accepted_unused", "node_id": "n2", "fields": ["state_query"]})}
    got = js(tmp_path, f"return W.notProducedLines({json.dumps({'events': [skill, node, unused]})});")
    assert got == [
        "the plan asked for instruction from classify_into_buckets, which does not take it; not used",
        "the plan asked for colour from step n2, which does not take it; not used",
    ]


def test_not_produced_line_renders_from_the_redacted_note_shape(tmp_path):
    """What a non-admin gets under a lane: only the five whitelisted keys."""
    note = {"event_type": "orchestrator_decision", "decision_json": json.dumps({
        "code": "BRT-ORC-035", "action": "ran_without_fields", "node_id": "n1",
        "skill_id": "classify_into_buckets", "fields": ["urgency"]})}
    got = js(tmp_path, f"return W.notProducedLines({json.dumps({'events': [note]})});")
    assert got == ["the plan asked for urgency from classify_into_buckets, which does not return it; not produced"]
