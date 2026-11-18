"""4.2 review fixes, behaviour under node: the send path re-reads the lane mode, steps map to
plan nodes by node_id, the "why this model" rows, live refresh, the lane mode a run recorded,
and ruling 18 on the rendered receipt and Run header.
"""
import json

import pytest

from tests.ui.test_app_js import NODE, run_js
from tests.ui.test_workspace_js import env, js, plan_event, run_body

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


def raw(tmp_path, body):
    """Run a JS body with workspace.js loaded; the body returns a JSON-able value."""
    return run_js(tmp_path, "const {BorgWork: W, Borg} = H.load({extra: ['workspace.js']});" + body)


# ---------- 1. the lane mode is re-read right before each send ----------

SEND_SETUP = """
const turns = [
  {role: 'user', content: 'secret q', lane: 'all_local'},
  {role: 'assistant', content: 'secret a', lane: 'all_local'},
  {role: 'user', content: 'plain q', lane: 'off'},
  {role: 'assistant', content: 'plain a', lane: 'off'},
  {role: 'user', content: 'untagged q'}];
const cfg = (v) => ({settings: [{key: 'lane_mode', value: v}]});
"""


def test_each_send_reads_the_mode_again(tmp_path):
    out = raw(tmp_path, SEND_SETUP + """
    let current = 'all_local', reads = 0;
    const api = async (path) => { reads++; if (path !== '/config/effective') throw new Error(path); return cfg(current); };
    const a = await W.prepareSend(api, turns);
    current = 'off';                                   // changed while the page stayed open
    const b = await W.prepareSend(api, turns);
    return {reads, a: [a.mode, a.tag, a.history.map(t => t.content)], b: [b.mode, b.tag, b.history.map(t => t.content)]};
    """)
    assert out["reads"] == 2
    assert out["a"] == ["all_local", "all_local", ["secret q", "secret a", "plain q", "plain a", "untagged q"]]
    assert out["b"] == ["off", "off", ["plain q", "plain a"]]


@pytest.mark.parametrize("how", ["fail", "garbage", "missing", "empty_object", "null"])
def test_an_unreadable_or_unknown_mode_counts_as_all_local(tmp_path, how):
    out = raw(tmp_path, SEND_SETUP + f"""
    const how = {json.dumps(how)};
    const api = async () => {{
      if (how === 'fail') throw new Error('network down');
      if (how === 'garbage') return cfg('strict_mode');
      if (how === 'missing') return {{settings: [{{key: 'other', value: 'x'}}]}};
      if (how === 'empty_object') return {{}};
      return null;
    }};
    const p = await W.prepareSend(api, turns);
    return [p.mode, p.tag, p.history.map(t => t.content), p.docs];
    """)
    assert out[0] == "unknown"                     # an unrecognised value is "unknown", not "off"
    assert out[1] == "all_local"                   # new turns are tagged all_local
    assert out[2] == ["plain q", "plain a"]        # nothing all_local-tagged (or untagged) is resent
    assert out[3] is False                         # no documents either


def test_the_four_real_modes_are_read_as_they_are(tmp_path):
    out = raw(tmp_path, SEND_SETUP + """
    const got = [];
    for (const m of ['off', 'raw_local', 'all_local', 'by_reference']) got.push(await W.readLaneMode(async () => cfg(m)));
    return got;
    """)
    assert out == ["off", "raw_local", "all_local", "by_reference"]


def test_ask_page_sends_through_prepare_send_only():
    from pathlib import Path
    src = Path("ui/assets/pages/ask.js").read_text(encoding="utf-8")
    send = src[src.index("function sendQuestion"):src.index("function doSend")]
    assert "W.prepareSend(B.api, turns)" in send
    body = src[src.index("function doSend"):]
    assert "historyForMode" not in body and "laneMode" not in body.split("fetch(")[0], \
        "the send path must use the mode read for this send, not the remembered one"


# ---------- 5. envelopes map to plan nodes by node_id; source_ref is a dependency ----------

def chain_envelope(eid, type_, node_id, instr, dep=None, model="m"):
    out = {"instruction": instr, "data_scope": "derived", "dependency_input": dep or {}}
    if node_id:
        out["node_id"] = node_id
    return {"envelope_id": eid, "type": type_, "trigger_reason": "initial_plan", "status": "resolved",
            "model_id": None if type_ == "state_op" else model, "outbound_payload": json.dumps(out),
            "inbound_payload": json.dumps({"status": "resolved", "output": {}}), "routing": None,
            "tokens_input": None, "tokens_output": None, "dollars": None, "latency_ms": None, "skill_id": None}


def test_the_state_op_chain_n1_n2_n3_is_drawn_as_a_chain(tmp_path):
    plan = {"event_type": "orchestrator_decision", "decision_json": json.dumps({"mode": "plan", "parsed": {"plan": {"nodes": [
        {"id": "n1", "type": "answerer", "instruction": "List the tickets."},
        {"id": "n2", "type": "state_op", "op": "count", "source": "n1"},
        {"id": "n3", "type": "answerer", "instruction": "Report the count.", "depends_on": ["n2"]}]}}})}
    data = run_body([
        chain_envelope("e1", "answerer", "n1", "List the tickets."),
        chain_envelope("e2", "state_op", "n2", "Apply state_op: count", {"op": "count", "source_ref": "n1", "items": [1, 2]}),
        chain_envelope("e3", "answerer", "n3", "Report the count.")], [plan])
    out = js(tmp_path, f"""
      const steps = W.stepsFromRun({json.dumps(data)});
      return {{steps: steps.map(s => [s.id, s.deps]), edges: W.planLayout(steps).edges.map(e => [e.from, e.to])}};""")
    assert out["steps"] == [["n1", []], ["n2", ["n1"]], ["n3", ["n2"]]]
    assert out["edges"] == [["plan", "step:n1"], ["step:n1", "step:n2"], ["step:n2", "step:n3"], ["step:n3", "compose"]]


def test_two_identical_instructions_are_told_apart_by_node_id(tmp_path):
    data = run_body([chain_envelope("e1", "answerer", "n2", "Same words."),
                     chain_envelope("e2", "answerer", "n1", "Same words.")])
    ids = js(tmp_path, f"return W.stepsFromRun({json.dumps(data)}).map(s => s.id);")
    assert ids == ["n2", "n1"]


# ---------- 2. calls that left: unknown is "Not recorded", never 0 ----------

def test_the_header_says_not_recorded_when_the_server_does_not_know(tmp_path):
    data = run_body([env("n1")], [plan_event([{"id": "n1"}])], calls_left_machine={"count": None, "source": None})
    tiles = js(tmp_path, f"return W.headerTiles({json.dumps(data)});")
    left = [t for t in tiles if t["k"] == "Calls that left this machine"][0]
    assert left["v"] == "Not recorded" and "0" not in left["v"]
    zero = dict(data, calls_left_machine={"count": 0, "source": "ledger"})
    left0 = [t for t in js(tmp_path, f"return W.headerTiles({json.dumps(zero)});") if t["k"].startswith("Calls that left")][0]
    assert left0["v"] == "0"
    assert js(tmp_path, "return W.leftChipText(null);") == "Calls that left this machine: not recorded"


# ---------- 9. the Run page shows the mode the run ran under ----------

def test_the_run_shows_the_lane_mode_it_recorded(tmp_path):
    def words(mode):
        data = run_body([env("n1")])
        data["run"]["lane_mode"] = mode
        return js(tmp_path, f"return [W.runLaneMode({json.dumps(data)}), W.runLaneWords({json.dumps(data)})];")
    assert words("all_local")[0] == "all_local" and "all_local" in words("all_local")[1]
    assert words("off")[0] == "off"
    none = words(None)
    assert none[0] is None and "not recorded" in none[1]
    assert words("bogus")[0] is None


def test_the_run_page_does_not_read_todays_lane_setting():
    from pathlib import Path
    src = Path("ui/assets/pages/run.js").read_text(encoding="utf-8")
    assert "/config/effective" not in src and "laneMode" not in src


# ---------- 6. why this model ----------

def test_why_rows_label_the_routers_real_choice(tmp_path):
    routing = {"chosen": "a", "reason": "routed", "floor": 6.0, "candidates": [
        {"model_id": "a", "score": 7.0, "est_cost": 0.002, "est_dollars": 0.0},
        {"model_id": "b", "score": 8.0, "est_cost": 0.004, "est_dollars": 0.003}]}
    rows = js(tmp_path, f"return W.whyRows({json.dumps(routing)});")
    assert [r["won"] for r in rows] == [True, False]
    assert "lowest effective cost" in rows[0]["note"] and rows[1]["note"] == ""
    assert (rows[0]["est_cost"], rows[0]["est_dollars"]) == (0.002, 0.0)      # both columns kept apart
    assert rows[1]["est_cost"] == 0.004 and rows[1]["est_dollars"] == 0.003


def test_why_rows_do_not_call_a_strategy_pick_the_cheapest(tmp_path):
    routing = {"chosen": "b", "reason": "routed", "floor": 6.0, "notes": ["run alias asked for a local model first"],
               "candidates": [{"model_id": "a", "score": 7.0, "est_cost": 0.002, "est_dollars": 0.0},
                              {"model_id": "b", "score": 8.0, "est_cost": 0.004, "est_dollars": 0.0}]}
    rows = js(tmp_path, f"return W.whyRows({json.dumps(routing)});")
    assert "alias or strategy" in rows[1]["note"] and "cheapest" not in rows[1]["note"].lower()
    planner = dict(routing, reason="planner_pick")
    assert "planner named it" in js(tmp_path, f"return W.whyRows({json.dumps(planner)});")[1]["note"]


# ---------- 7. live refresh ----------

def test_only_one_get_is_in_flight_and_a_late_request_is_sent_once_after(tmp_path):
    out = raw(tmp_path, """
    const pending = []; let live = 0, maxLive = 0, applied = [];
    const get = () => new Promise((res) => { live++; maxLive = Math.max(maxLive, live); pending.push((v) => { live--; res(v); }); });
    const r = W.makeRefresher({get, apply: (d, seq) => applied.push([d, seq])});
    r.request(); r.request(); r.request();             // three requests while the first is running
    await new Promise(x => setTimeout(x, 0));
    const startedFirst = pending.length;
    pending[0]('first');
    await new Promise(x => setTimeout(x, 5));
    const startedAfter = pending.length;
    pending[1]('second');
    await new Promise(x => setTimeout(x, 5));
    return {maxLive, startedFirst, startedAfter, applied};
    """)
    assert out["maxLive"] == 1 and out["startedFirst"] == 1 and out["startedAfter"] == 2
    assert out["applied"] == [["first", 1], ["second", 2]]            # sequence numbers rise


def test_a_cancelled_or_superseded_response_is_discarded(tmp_path):
    out = raw(tmp_path, """
    const pending = []; const applied = [];
    const get = () => new Promise((res) => pending.push(res));
    const r = W.makeRefresher({get, apply: (d) => applied.push(d)});
    r.request();
    await new Promise(x => setTimeout(x, 0));
    r.cancel();                                         // the page went away
    pending[0]('late');
    await new Promise(x => setTimeout(x, 5));
    return applied;
    """)
    assert out == []


def test_a_failed_get_frees_the_slot_for_the_next(tmp_path):
    out = raw(tmp_path, """
    let n = 0; const applied = [], errors = [];
    const r = W.makeRefresher({get: async () => { n++; if (n === 1) throw new Error('boom'); return 'ok'; },
      apply: (d) => applied.push(d), onError: (e) => errors.push(e.message)});
    await r.request(); await r.request();
    return {applied, errors};
    """)
    assert out == {"applied": ["ok"], "errors": ["boom"]}


def test_a_stream_error_reconnects_with_backoff_until_the_run_finishes(tmp_path):
    out = raw(tmp_path, """
    let finished = false; const waits = []; const opened = []; let onerr = null, onmsg = null;
    const timers = [];
    const s = W.makeLiveStream({finished: () => finished, wait: (fn, ms) => { waits.push(ms); timers.push(fn); return timers.length; },
      open: (m, e) => { opened.push(1); onerr = e; onmsg = m; return {close() {}}; }, onMessage: () => {}});
    s.start();
    onerr(); timers.shift()();       // 1 s, reconnect
    onerr(); timers.shift()();       // 2 s
    onerr(); timers.shift()();       // 4 s
    onmsg({type: 'envelope_update'});   // a message resets the wait
    onerr();
    const afterReset = waits[waits.length - 1];
    timers.shift()();
    finished = true;
    onerr();                         // run finished: no further reconnect
    return {waits, opened: opened.length, afterReset, timersLeft: timers.length};
    """)
    assert out["waits"] == [1000, 2000, 4000, 1000]
    assert out["opened"] == 5 and out["timersLeft"] == 0


def test_backoff_stops_growing_at_fifteen_seconds(tmp_path):
    out = raw(tmp_path, """
    const waits = []; const timers = []; let onerr;
    W.makeLiveStream({finished: () => false, wait: (fn, ms) => { waits.push(ms); timers.push(fn); return 1; },
      open: (m, e) => { onerr = e; return {close() {}}; }, onMessage() {}}).start();
    for (let i = 0; i < 7; i++) { onerr(); timers.shift()(); }
    return waits;
    """)
    assert out == [1000, 2000, 4000, 8000, 15000, 15000, 15000]


# ---------- 8. ruling 18: no baseline in the raw view or on the rendered pages ----------

BASE_ENV = {"baseline_dollars": 0.4242, "routing": {"baseline": "openrouter/flagship", "baseline_est_cost": 0.31,
            "candidates": [{"model_id": "a", "baseline_est_cost": 0.2}], "chosen": "a"},
            "outbound_payload": json.dumps({"instruction": "x", "baseline_dollars": 0.4242}),
            "inbound_payload": json.dumps({"status": "resolved", "baseline_dollars": 0.4242, "output": {}})}


def test_the_raw_details_view_carries_no_baseline_at_any_depth(tmp_path):
    out = js(tmp_path, f"return JSON.stringify(W.stripBaseline({json.dumps(BASE_ENV)}));")
    assert "baseline" not in out.lower() and "0.4242" not in out
    assert "a" in out and "resolved" in out                    # everything else stays


def test_the_rendered_receipt_and_run_header_show_no_baseline_or_top_model(tmp_path):
    envelope = env("n1", baseline_dollars=0.4242)
    envelope["routing"]["baseline"] = "openrouter/flagship"
    data = run_body([envelope], [plan_event([{"id": "n1"}])],
                    digest={"cost_dollars": 0.05, "baseline_dollars": 0.4242},
                    calls_left_machine={"count": 1, "source": "locality"})
    data["run"]["lane_mode"] = "off"
    out = raw(tmp_path, f"""
    const data = {json.dumps(data)};
    const receipt = W.receiptFrom(data);
    return JSON.stringify({{tiles: W.headerTiles(data), receipt: W.receiptStats(receipt), receiptObj: receipt}});""")
    low = out.lower()
    for bad in ("baseline", "top model", "one top", "0.42", "$0.42", "would have cost"):
        assert bad not in low, bad
    assert "cost of this run" in low and "one pass on" not in low     # no floor stored: no comparison line


def test_the_floor_line_reads_one_pass_at_least_and_never_would_have_cost(tmp_path):
    data = run_body([env("n1")], [plan_event([{"id": "n1"}])],
                    digest={"cost_dollars": 0.05, "direct_floor_dollars": 0.093,
                            "direct_floor_model": "openrouter/anthropic/claude-opus-5.5",
                            "direct_floor_ratio": [1.3, 4.4]},
                    calls_left_machine={"count": 1, "source": "locality"})
    data["run"]["lane_mode"] = "off"
    out = raw(tmp_path, f"""
    const data = {json.dumps(data)};
    const receipt = W.receiptFrom(data);
    return JSON.stringify({{tiles: W.headerTiles(data), receipt: W.receiptStats(receipt), note: W.floorNote(receipt.estimate)}});""")
    low = out.lower()
    assert "one pass on claude-opus-5.5: at least" in low
    for bad in ("baseline", "top model", "would have cost"):
        assert bad not in low, bad


def test_no_page_script_or_slide_offers_a_top_model_comparison():
    from pathlib import Path
    for path in list(Path("ui").rglob("*.js")) + list(Path("ui").glob("*.html")):
        if "vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8").lower()
        assert "top model" not in text and "est. on one" not in text, path
    slides = Path("docs/presentation/draft_v1/project/slides")
    for name in ("receipt.html", "cover.html"):
        text = (slides / name).read_text(encoding="utf-8").lower()
        assert "on one top model" not in text and "est. on one top" not in text and "baseline" not in text, name


# ---------- 3. admin.js trusts the server's is_local, not the source label ----------

def test_is_local_model_uses_the_servers_flag_only(tmp_path):
    out = run_js(tmp_path, """
    const {BorgAdmin: A} = H.load({extra: ['admin.js']});
    return [
      A.isLocalModel({source: 'ollama', model_id: 'x', is_local: false}),             // remote Ollama host
      A.isLocalModel({source: 'ollama', model_id: 'x:cloud', is_local: false}),
      A.isLocalModel({source: 'openrouter', model_id: 'y', is_local: true}),          // listed in BORG_LOCAL_HOSTS
      A.isLocalModel({source: 'lmstudio', model_id: 'z'}),                            // no flag: not local
    ];""")
    assert out == [False, False, True, False]


# ---------- round 2: the "What left this machine" card ----------

def test_the_left_card_shows_whenever_the_ledger_has_rows(tmp_path):
    def card(mode, rows, docs=None):
        data = run_body([env("n1")])
        data["run"]["lane_mode"] = mode
        data["run"]["documents"] = docs
        data["outbound"] = [{"envelope_id": "e1"}] * rows
        return js(tmp_path, f"return W.leftCard({json.dumps(data)});")
    assert card("off", 1)["show"] is True and "off" in card("off", 1)["note"]
    assert card(None, 2)["show"] is True
    assert "raw_local" in card("raw_local", 1)["note"]
    assert card("off", 0)["show"] is False
    assert card("raw_local", 0)["show"] is True        # the lane ran: an empty list is a real answer
    assert card(None, 0)["show"] is False
    assert card(None, 0, docs="[1]")["show"] is True


# ---------- stage and decision codes in plain words (5.3 review) ----------

def test_every_known_stage_has_plain_words_and_an_unknown_one_reads_working(tmp_path):
    out = raw(tmp_path, """
    const known = ['plan', 're_plan', 'compose', 'route', 'rule_zero_shortcut', 'bypass', 'orchestrator',
      'BRT-ORC-018', 'BRT-ORC-028', 'BRT-ORC-029', 'BRT-ORC-030', 'BRT-ORC-031', 'BRT-ZERO-01-ESCALATED',
      'BRT-APE-CAPPED', 'BRT-ORC-NO-FRONTIER'];
    return {known: known.map(k => W.stageWords(k)),
            odd: ['BUG-020-COMPOSE-DECLINED', 'something_new', '', null, undefined, 'toString'].map(k => W.stageWords(k))};
    """)
    assert all(w and "BRT" not in w and "_" not in w for w in out["known"])
    assert out["odd"] == ["Working"] * 6


def test_the_timeline_shows_words_for_a_decision_code_and_an_unknown_mode(tmp_path):
    out = raw(tmp_path, """
    const ev = (d) => ({event_type: 'orchestrator_decision', created_at: '2026-01-01T00:00:00Z', model_id: 'm/x',
                        decision_json: JSON.stringify(d)});
    const data = {run: {created_at: '2026-01-01T00:00:00Z'}, events: [
      ev({code: 'BRT-ORC-031', msg: 'compose returned action=fail'}), ev({mode: 'weird_new_mode'}), ev({code: 'BUG-X'})]};
    return W.eventSentences(data).map(s => s.text);
    """)
    assert out == ["The planner declined to answer.", "The planner recorded a decision.", "Working."]
