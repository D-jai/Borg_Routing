/* BORG_ROUTE workspace helpers (work order 4.2): Ask, Run and Runs pages.
 * Loaded after assets/app.js. Pure functions first (tested under node in tests/ui),
 * then small DOM helpers. Server data is only ever set with textContent or SVG text
 * nodes; markdown goes through Borg.markdown (DOMPurify).
 */
(function () {
  'use strict';

  var LANE_MODES = ['off', 'raw_local', 'all_local', 'by_reference'];

  // ---------- small utilities ----------
  function parseJSON(v) {
    if (v && typeof v === 'object') return v;
    if (typeof v !== 'string' || !v) return null;
    try { return JSON.parse(v); } catch (_) { return null; }
  }
  function decisionOf(ev) {
    var d = parseJSON(ev && ev.decision_json);
    return d && typeof d === 'object' && !Array.isArray(d) ? d : null;
  }
  function num(v) {
    if (typeof v === 'string' && v.trim() !== '') v = Number(v);
    return typeof v === 'number' && isFinite(v) ? v : null;
  }
  function shortText(s, n) {
    s = String(s === null || s === undefined ? '' : s).replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }
  // "openrouter/deepseek/deepseek-v3.2" -> "deepseek-v3.2"
  function shortModel(id) {
    var s = String(id || '');
    var i = s.lastIndexOf('/');
    return i >= 0 ? s.slice(i + 1) : s;
  }

  // ---------- chat history and the lane tag (16-Nov ruling "2.2 runtime review") ----------
  /* Each stored turn carries the lane mode it ran under. A turn tagged all_local is resent
   * only while the mode is all_local; under any other mode it stays on screen but is never
   * sent again. An untagged turn, or a mode that could not be read, counts as all_local
   * (fail closed). */
  function laneTag(mode) { return LANE_MODES.indexOf(mode) >= 0 ? mode : 'all_local'; }
  /* The lane mode, read right now (never remembered from page load: it can change while the
   * page is open). `api` is Borg.api. A value that is not one of the four modes is "unknown",
   * not "off"; so is a setting that is missing or a request that fails. Unknown fails closed:
   * laneTag() gives all_local and historyForMode() resends no all_local turn. */
  function readLaneMode(api) {
    return Promise.resolve().then(function () { return api('/config/effective', { quiet: true }); }).then(function (cfg) {
      var row = cfg && Array.isArray(cfg.settings) ? cfg.settings.filter(function (s) { return s.key === 'lane_mode'; })[0] : null;
      var v = row ? String(row.value) : '';
      return LANE_MODES.indexOf(v) >= 0 ? v : 'unknown';
    }, function () { return 'unknown'; });
  }
  /* prepareSend(api, turns) -> {mode, tag, history, docs}: everything the send path needs
   * from the lane, worked out from a mode read for THIS send. */
  function prepareSend(api, turns) {
    return readLaneMode(api).then(function (mode) {
      return { mode: mode, tag: laneTag(mode), history: historyForMode(turns, mode), docs: laneLine(mode).docs };
    });
  }
  function historyForMode(turns, mode) {
    var known = LANE_MODES.indexOf(mode) >= 0;
    return (Array.isArray(turns) ? turns : []).filter(function (t) {
      if (!t || (t.role !== 'user' && t.role !== 'assistant')) return false;
      if (typeof t.content !== 'string' || !t.content) return false;
      if (t.failed) return false;
      var tag = LANE_MODES.indexOf(t.lane) >= 0 ? t.lane : 'all_local';
      return tag !== 'all_local' || (known && mode === 'all_local');
    }).map(function (t) { return { role: t.role, content: t.content }; });
  }
  function laneLine(mode) {
    switch (mode) {
      case 'off': return { on: false, docs: false, text: 'Lane off: documents cannot be added, and any step may use a cloud model.' };
      case 'raw_local': return { on: true, docs: true, local: true, text: 'Lane on (raw_local): raw documents are read only by models on this machine.' };
      case 'all_local': return { on: true, docs: true, local: true, text: 'Lane on (all_local): nothing is sent to any cloud model.' };
      case 'by_reference': return { on: true, docs: true, local: false, text: 'Lane by_reference: cloud models may read your documents. This mode makes no privacy promise.' };
      default: return { on: true, docs: false, text: 'Could not read the lane setting. Earlier turns are kept on screen but not resent, and new turns are treated as all_local.' };
    }
  }

  // ---------- plan steps from GET /runs/{id} ----------
  var ATE_CHILD = /^apply_to_each:([^:]+):/;

  function planNodesById(events) {
    var byId = {}, order = [];
    (events || []).forEach(function (ev) {
      if (ev.event_type !== 'orchestrator_decision') return;
      var d = decisionOf(ev);
      if (!d || (d.mode !== 'plan' && d.mode !== 're_plan')) return;
      var plan = d.parsed && d.parsed.plan;
      (plan && Array.isArray(plan.nodes) ? plan.nodes : []).forEach(function (n) {
        if (!n || typeof n !== 'object' || !n.id) return;
        var id = String(n.id);
        if (!byId[id]) order.push(id);
        byId[id] = n;
      });
    });
    return { byId: byId, order: order };
  }

  // Every id a plan node reads: depends_on, inputs_ref, source, and a skill's *_ref args.
  function nodeRefs(n) {
    var out = [];
    function add(v) { if (typeof v === 'string' && v) out.push(v); }
    (Array.isArray(n.depends_on) ? n.depends_on : []).forEach(add);
    add(n.inputs_ref); add(n.source);
    var args = n.args || n.skill_args;
    if (args && typeof args === 'object') {
      Object.keys(args).forEach(function (k) { if (/_ref$/.test(k)) add(args[k]); });
    }
    return out;
  }

  /* stepsFromRun(data) -> [{id, envelope, type, label, instruction, deps, items, routing,
   *   status, model, dollars, latency_ms, tokens, failure, childRunId, dataScope, skill}]
   * One step per envelope of this run. apply_to_each item envelopes (trigger
   * "apply_to_each:<parent>:item_N") fold into their parent with a count. */
  function stepsFromRun(data) {
    data = data || {};
    var envs = Array.isArray(data.envelopes) ? data.envelopes : [];
    var plan = planNodesById(data.events);
    var childCount = {}, parents = [];
    envs.forEach(function (e) {
      var m = ATE_CHILD.exec(String(e.trigger_reason || ''));
      if (m) childCount[m[1]] = (childCount[m[1]] || 0) + 1;
      else parents.push(e);
    });
    // Planner ruling 23: one pack_outcome event per packed post, on the step's envelope.
    var packs = {};
    (data.events || []).forEach(function (ev) {
      var d = ev.event_type === 'orchestrator_decision' ? decisionOf(ev) : null;
      if (!d || d.mode !== 'pack_outcome') return;
      var key = ev.envelope_id || d.step;
      var p = packs[key] || (packs[key] = { posts: 0, accepted: 0, alone: 0 });
      p.posts += 1; p.accepted += num(d.accepted) || 0; p.alone += num(d.alone) || 0;
    });
    var usedIds = {};
    var steps = parents.map(function (e, i) {
      var out = parseJSON(e.outbound_payload) || {};
      var inb = parseJSON(e.inbound_payload) || {};
      var routing = e.routing && typeof e.routing === 'object' ? e.routing : null;
      // The engine stamps each envelope with its plan node id (state_ops have no route event).
      var id = out.node_id ? String(out.node_id) : (routing && routing.node_id ? String(routing.node_id) : null);
      if (!id && out.instruction) {
        // No route event (a skill or state step): match the plan node by its instruction.
        for (var k = 0; k < plan.order.length; k++) {
          var pn = plan.byId[plan.order[k]];
          if (!usedIds[plan.order[k]] && pn.instruction && pn.instruction === out.instruction) { id = plan.order[k]; break; }
        }
      }
      if (!id || usedIds[id]) id = 'step ' + (i + 1);
      usedIds[id] = true;
      var pnode = plan.byId[id] || null;
      var deps = pnode ? nodeRefs(pnode) : [];
      var di = out.dependency_input && typeof out.dependency_input === 'object' ? out.dependency_input : null;
      var up = di && di.upstream_results;
      if (up && typeof up === 'object') deps = deps.concat(Object.keys(up));
      if (di && typeof di.source_ref === 'string' && di.source_ref) deps.push(di.source_ref);
      var items = num(routing && routing.items) || childCount[e.envelope_id] ||
        (pnode && Array.isArray(pnode.items) ? pnode.items.length : null);
      var pk = packs[e.envelope_id] || null;
      var tin = num(e.tokens_input), tout = num(e.tokens_output);
      return {
        id: id, envelope: e, type: e.type, instruction: out.instruction || (pnode && pnode.instruction) || '',
        label: shortText(e.skill_id ? e.skill_id.replace(/_/g, ' ') + ': ' + (out.instruction || '') : (out.instruction || id), 30),
        deps: deps, items: items || null, routing: routing, status: e.status,
        packPosts: pk ? pk.posts : 0, packAccepted: pk ? pk.accepted : 0, retriedAlone: pk ? pk.alone : 0,
        model: e.model_id || (routing && routing.chosen) || null,
        dollars: num(e.dollars), latency_ms: num(e.latency_ms),
        tokens: tin === null && tout === null ? null : (tin || 0) + (tout || 0),
        tokens_input: tin, tokens_output: tout,
        failure: inb.status === 'failed' ? (inb.failure_reason || 'failed') : null,
        childRunId: (routing && routing.child_run_id) || (inb.output && inb.output.child_run_id) || null,
        dataScope: out.data_scope || 'derived', skill: e.skill_id || null,
        task_type: (routing && routing.task_type) || (pnode && pnode.task_type) || null,
        tier: (routing && routing.tier) || (pnode && pnode.tier) || null
      };
    });
    var known = {};
    steps.forEach(function (s) { known[s.id] = true; });
    steps.forEach(function (s) {
      var seen = {};
      s.deps = s.deps.filter(function (d) {
        if (!known[d] || d === s.id || seen[d]) return false;
        seen[d] = true; return true;
      });
    });
    return steps;
  }

  // ---------- plan layout (plain SVG; columns by dependency depth) ----------
  var L = { NODE_W: 184, NODE_H: 76, CORE_W: 150, CORE_H: 60, COL_GAP: 44, ROW_GAP: 16, PAD: 10 };

  function depths(steps) {
    var byId = {}, memo = {};
    steps.forEach(function (s) { byId[s.id] = s; });
    function depth(id, stack) {
      if (memo[id] !== undefined) return memo[id];
      if (stack[id]) return 0; // a cycle: draw it, do not loop
      stack[id] = true;
      var d = 0;
      (byId[id].deps || []).forEach(function (p) { if (byId[p]) d = Math.max(d, depth(p, stack) + 1); });
      stack[id] = false;
      memo[id] = d;
      return d;
    }
    steps.forEach(function (s) { depth(s.id, {}); });
    return memo;
  }

  /* planLayout(steps) -> {width, height, columns, nodes:[{key, kind, col, x, y, w, h, step}],
   * edges:[{from, to, d}]}. Column 0 is the plan, then one column per dependency depth,
   * then compose. Nodes keep a fixed size, so twelve steps make the drawing taller or
   * wider (it scrolls in its box) instead of shrinking the text. */
  function planLayout(steps) {
    steps = Array.isArray(steps) ? steps : [];
    var dep = depths(steps);
    var nStepCols = 0;
    steps.forEach(function (s) { nStepCols = Math.max(nStepCols, dep[s.id] + 1); });
    var cols = [];
    for (var c = 0; c < nStepCols; c++) cols.push([]);
    steps.forEach(function (s) { cols[dep[s.id]].push(s); });
    var maxRows = 1;
    cols.forEach(function (col) { maxRows = Math.max(maxRows, col.length); });
    var height = L.PAD * 2 + Math.max(maxRows * L.NODE_H + (maxRows - 1) * L.ROW_GAP, L.CORE_H);
    var nodes = [], byKey = {};
    function place(key, kind, col, x, y, w, h, step) {
      var n = { key: key, kind: kind, col: col, x: x, y: y, w: w, h: h, step: step || null };
      nodes.push(n); byKey[key] = n; return n;
    }
    place('plan', 'plan', 0, L.PAD, (height - L.CORE_H) / 2, L.CORE_W, L.CORE_H);
    var x = L.PAD + L.CORE_W + L.COL_GAP;
    cols.forEach(function (col, ci) {
      var total = col.length * L.NODE_H + (col.length - 1) * L.ROW_GAP;
      var y0 = (height - total) / 2;
      col.forEach(function (s, ri) {
        place('step:' + s.id, 'step', ci + 1, x, y0 + ri * (L.NODE_H + L.ROW_GAP), L.NODE_W, L.NODE_H, s);
      });
      x += L.NODE_W + L.COL_GAP;
    });
    place('compose', 'compose', nStepCols + 1, x, (height - L.CORE_H) / 2, L.CORE_W + 10, L.CORE_H);
    var width = x + L.CORE_W + 10 + L.PAD;

    var edges = [];
    function edge(a, b) {
      var A = byKey[a], B = byKey[b];
      if (!A || !B) return;
      var x1 = A.x + A.w, y1 = A.y + A.h / 2, x2 = B.x, y2 = B.y + B.h / 2, mx = (x1 + x2) / 2;
      edges.push({ from: a, to: b, d: 'M' + x1 + ' ' + y1 + ' C' + mx + ' ' + y1 + ' ' + mx + ' ' + y2 + ' ' + x2 + ' ' + y2 });
    }
    var hasDependant = {};
    steps.forEach(function (s) { s.deps.forEach(function (p) { hasDependant[p] = true; }); });
    steps.forEach(function (s) {
      var real = s.deps.filter(function (p) { return dep[p] < dep[s.id]; });
      if (!real.length) edge('plan', 'step:' + s.id);
      real.forEach(function (p) { edge('step:' + p, 'step:' + s.id); });
    });
    steps.forEach(function (s) { if (!hasDependant[s.id]) edge('step:' + s.id, 'compose'); });
    if (!steps.length) edge('plan', 'compose');
    return { width: width, height: height, columns: nStepCols + 2, nodes: nodes, edges: edges, sizes: L };
  }

  // ---------- run totals and receipt figures ----------
  function seconds(a, b) {
    var B = window.Borg;
    var s = B.parseWhen(a), e = b ? B.parseWhen(b) : new Date();
    return s && e ? Math.max(0, (e.getTime() - s.getTime()) / 1000) : null;
  }
  // A logged call is a log row with tokens or dollars (model_call, priced orchestrator calls).
  function isCall(ev) {
    return (ev.event_type === 'model_call' || ev.event_type === 'orchestrator_decision' || ev.event_type === 'tool_call') &&
      (num(ev.tokens_input) !== null || num(ev.dollars) !== null) && !!ev.model_id && !packShare(ev);
  }
  /* Planner ruling 23: a packed call logs one model_call per item with its share. Only the
   * first share of each post counts as a call; the others are packShare. */
  function packShare(ev) {
    if (!ev || ev.event_type !== 'model_call') return false;
    var d = decisionOf(ev);
    var p = d && d.pack;
    return !!(p && typeof p === 'object' && !p.first);
  }
  function callsByModel(data) {
    var out = {}, order = [];
    (data.events || []).forEach(function (ev) {
      if (!isCall(ev)) return;
      if (!out[ev.model_id]) { out[ev.model_id] = 0; order.push(ev.model_id); }
      out[ev.model_id] += 1;
    });
    (data.child_envelopes || []).forEach(function (e) {
      if (!e.model_id || e.type === 'orchestrate') return;
      if (!out[e.model_id]) { out[e.model_id] = 0; order.push(e.model_id); }
      out[e.model_id] += 1;
    });
    return order.map(function (m) { return { model: m, calls: out[m] }; });
  }
  /* runTotals(data) -> {cost, costSoFar, seconds, steps, calls, left, leftSource}.
   * Cost is the digest's total for the whole run tree. Child envelopes' dollars are already
   * inside it and are never added again. Before a digest exists, the run's own logged
   * dollars are summed and marked "so far". */
  function runTotals(data) {
    data = data || {};
    var run = data.run || {};
    var digest = data.digest || null;
    var soFar = 0;
    (data.events || []).forEach(function (ev) { var d = num(ev.dollars); if (d !== null) soFar += d; });
    var cost = digest && num(digest.cost_dollars) !== null ? num(digest.cost_dollars) : null;
    var calls = (data.events || []).filter(isCall);
    // Worked out by the server (ledger, else the locality of every call, child runs included).
    // Unknown stays null and the page says "Not recorded": it is never turned into 0.
    var clm = data.calls_left_machine;
    var left = clm && typeof clm.count === 'number' ? clm.count : null;
    var leftSource = left !== null && (clm.source === 'ledger' || clm.source === 'locality') ? clm.source : 'unknown';
    if (left === null) leftSource = 'unknown';
    var done = run.status === 'completed' || run.status === 'failed';
    return {
      cost: cost, costSoFar: cost === null ? soFar : null,
      // A finished run with no end time has no known duration; a live one counts to now.
      seconds: done && !run.completed_at ? null : seconds(run.created_at, run.completed_at),
      done: done,
      steps: stepsFromRun(data).length, calls: calls.length, left: left, leftSource: leftSource
    };
  }

  /* RULING 22 (BUILD.md section 8): the receipt's comparison figure is a floor, the server's
   * digest.direct_floor_dollars ("One pass on <baseline model>: at least US$X"). It is never
   * worded as what the run would have cost, and the old per-run baseline_dollars is never
   * shown (ruling 18). null when the run stored no floor (older runs, direct-to-model runs).
   * The ratio range in the note comes from the server (digest.direct_floor_ratio), so the page
   * and the user guide quote the one figure kept in src/registry/cost.py. */
  function baselineEstimate(data) {
    var d = data && data.digest;
    if (!d) return null;
    var v = d.direct_floor_dollars;
    if (typeof v !== 'number' || !isFinite(v)) return null;
    var model = d.direct_floor_model ? shortModel(d.direct_floor_model) : 'the baseline model';
    var ratio = Array.isArray(d.direct_floor_ratio) && d.direct_floor_ratio.length === 2 ? d.direct_floor_ratio : null;
    return { value: v, model: model, ratio: ratio, label: 'One pass on ' + model + ': at least' };
  }
  /* The floor's amount, rounded DOWN to three significant figures (B.money rounds half-up, which could
   * show a figure above the stored floor). Used only for the floor line. */
  function floorMoney(v) {
    var n = Number(v);
    if (!isFinite(n) || n < 0) return '-';
    if (n === 0) return '$0.00';
    // toPrecision(12) cleans float error first (1.15 * 100 is 114.999...), so an exact value is not shown one unit low.
    if (n >= 1) return '$' + (Math.floor(Number((n * 100).toPrecision(12))) / 100).toFixed(2);
    var digits = 2 - Math.floor(Math.log10(n));
    var f = Math.pow(10, digits);
    return '$' + (Math.floor(Number((n * f).toPrecision(12))) / f).toFixed(digits);
  }
  /* What the floor leaves out, as one short note under the receipt. */
  function floorNote(est) {
    if (!est) return '';
    var s = 'This is a floor, not a price. It leaves out retries, and structured output such as JSON costs more tokens than the estimate assumes.';
    if (est.ratio) s += ' On the round-1 benchmark, the real direct cost was between ' + est.ratio[0] + ' and ' + est.ratio[1] + ' times this floor (the top of the range includes a retry that was thrown away).';
    return s;
  }

  /* Planner ruling 26a: when every planned step failed and the planning model still wrote the
   * answer (run.answer_kind "planner_only"), the receipt says so in plain words and lists the
   * steps' failure codes from the BRT-ORC-033 event (a document step: code and document id
   * only). null for any other answer. */
  function noStepNote(data) {
    var run = data && data.run;
    if (!run || run.answer_kind !== 'planner_only') return null;
    var d = null;
    (data.events || []).forEach(function (ev) {
      var x = decisionOf(ev);
      if (x && x.code === 'BRT-ORC-033') d = x;
    });
    var codes = d && Array.isArray(d.step_codes) ? d.step_codes : [];
    // A viewer whose events were redacted may read no count: leave it out, never "0 of 0".
    var n = d && typeof d.failed_steps === 'number' && d.failed_steps > 0 ? d.failed_steps
      : codes.length || null;
    var list = codes.map(function (c) {
      if (c && typeof c === 'object') return String(c.code) + (c.doc ? ' (document ' + c.doc + ')' : '');
      return String(c);
    }).join(', ');
    return 'Every planned step failed' + (n ? ' (' + n + ' of ' + n + ')' : '') +
      ', so the planning model wrote this answer alone, with no step’s work behind it.' +
      (list ? ' Step failure codes: ' + list + '.' : '');
  }

  /* Ruling 26e: {steps, stepsFailed, items, itemsFailed}. A step failed when its envelope failed,
   * when (ruling 26f) it is an apply_to_each whose every item failed, or when it failed before any
   * call and so has no envelope (BRT-LANE-002, -ORC-025, -APE-005, -ORC-008, -ORC-024, -REG-020):
   * those are read from the run's error events by node_id (a warning is not a failure). Steps are
   * counted once each by node id. An apply_to_each with some failed items is not a failed step:
   * its items count. */
  function failedCounts(data) {
    var failed = {}, ok = {}, out = { steps: 0, stepsFailed: 0, items: 0, itemsFailed: 0 };
    stepsFromRun(data).forEach(function (s) {
      var inb = parseJSON(s.envelope && s.envelope.inbound_payload) || {};
      var o = inb.output && typeof inb.output === 'object' ? inb.output : {};
      var allItemsFailed = Array.isArray(o.results) && !o.results.length && Array.isArray(o.failures) && o.failures.length > 0;
      if (s.status === 'failed' || allItemsFailed) { failed[s.id] = true; return; }
      ok[s.id] = true;
      var bad = num(o.failure_count), total = num(o.total);
      if (s.type === 'apply_to_each' && bad && total) { out.itemsFailed += bad; out.items += total; }
    });
    (data && data.events || []).forEach(function (ev) {
      if (!ev || ev.event_type !== 'error') return;
      var d = decisionOf(ev);
      if (!d || typeof d.node_id !== 'string' || !d.node_id || d.severity === 'warning') return;
      if (!ok[d.node_id]) failed[d.node_id] = true;
    });
    out.stepsFailed = Object.keys(failed).length;
    out.steps = out.stepsFailed + Object.keys(ok).filter(function (k) { return !failed[k]; }).length;
    return out;
  }
  /* Ruling 32c: one receipt line per field a plan asked of a skill step that the skill does not
   * return, after the plan repeated the fault (the step ran without it). Read from the run's
   * BRT-ORC-035 plan note (an orchestrator_decision event). */
  function notProducedLines(data) {
    var out = [];
    (data && data.events || []).forEach(function (ev) {
      var d = ev && ev.event_type === 'orchestrator_decision' ? decisionOf(ev) : null;
      if (!d || d.action !== 'ran_without_fields' || !Array.isArray(d.fields)) return;
      if (d.code === 'BRT-ORC-035') {
        d.fields.forEach(function (f) {
          out.push('the plan asked for ' + f + ' from ' + d.skill_id + ', which does not return it; not produced');
        });
      } else if (d.code === 'BRT-ORC-036') {
        /* Ruling 32d: a key no node or skill takes, dropped; from the skill for a skill_args
         * key, from the step for a node key (its skill_id is empty). */
        var from = d.skill_id ? d.skill_id : 'step ' + d.node_id;
        d.fields.forEach(function (f) {
          out.push('the plan asked for ' + f + ' from ' + from + ', which does not take it; not used');
        });
      }
    });
    return out;
  }
  /* The receipt's failure lines: "k of N steps failed" only when k > 0, then "m of n items
   * failed" only when m > 0. */
  function failedLines(r) {
    var f = r && r.failed, out = [];
    if (!f) return out;
    if (f.stepsFailed > 0) out.push(f.stepsFailed + ' of ' + f.steps + ' steps failed');
    if (f.itemsFailed > 0) out.push(f.itemsFailed + ' of ' + f.items + ' items failed');
    return out;
  }

  // ---------- what the run was run under, and what the pages show about it ----------
  /* The lane mode the run recorded when it started (runs.lane_mode). Not the setting now.
   * null means the run did not record one (a run from before the column). */
  function runLaneMode(data) {
    var m = data && data.run ? data.run.lane_mode : null;
    return LANE_MODES.indexOf(m) >= 0 ? m : null;
  }
  var LANE_WORDS = { off: 'Lane off: any step could use a cloud model',
    raw_local: 'Lane raw_local: raw documents were read only on this machine',
    all_local: 'Lane all_local: nothing was sent to a cloud model',
    by_reference: 'Lane by_reference: cloud models could read the documents (no privacy promise)' };
  function runLaneWords(data) {
    var m = runLaneMode(data);
    return m ? LANE_WORDS[m] : 'Lane mode not recorded for this run';
  }

  /* The "What left this machine" card: shown whenever the ledger has rows, whatever mode the run
   * recorded; otherwise when the run used the lane (its recorded mode, else its documents). The
   * note follows the recorded mode. */
  function leftCard(data) {
    var rows = data && data.outbound ? data.outbound.length : 0;
    var m = runLaneMode(data);
    var used = m ? m !== 'off' : !!(data && data.run && data.run.documents);
    var note = m === 'off' ? 'The lane was off for this run; these are the requests that were recorded.'
      : m ? 'Lane ' + m + ': every request below left this machine.' : '';
    return { show: rows > 0 || used, note: rows > 0 ? note : '' };
  }

  /* The four tiles of the Run header as plain text: [{k, v, sub}]. No baseline figure and no
   * flagship-comparison wording (ruling 18): the cost tile says what the run cost, nothing else. */
  function headerTiles(data) {
    var B = window.Borg;
    var t = runTotals(data);
    var longest = 0;
    stepsFromRun(data).forEach(function (s) { longest = Math.max(longest, s.latency_ms || 0); });
    var est = baselineEstimate(data);
    return [
      { k: t.cost !== null ? 'Cost of this run' : 'Cost so far', v: B.money(t.cost !== null ? t.cost : t.costSoFar),
        sub: est ? est.label + ' US' + floorMoney(est.value) : (t.cost !== null ? 'Whole run, steps’ own runs included' : 'This run’s logged calls') },
      { k: 'Time', v: B.duration(t.seconds), sub: longest ? 'Longest step: ' + B.duration(longest / 1000) : null },
      { k: 'Steps', v: String(t.steps), sub: t.calls + (t.calls === 1 ? ' model call' : ' model calls') },
      { k: 'Calls that left this machine', v: t.left === null ? 'Not recorded' : String(t.left),
        sub: t.leftSource === 'ledger' ? 'From the lane’s record of every request'
          : t.leftSource === 'locality' ? 'Calls to models that are not on this machine, from where each model runs' : 'Could not be worked out for this run' }
    ];
  }
  /* The receipt's figures: numbers only, so they can be stored with the turn. */
  function receiptFrom(data, modelLocal) {
    var t = runTotals(data);
    var mode = runLaneMode(data);
    return {
      steps: t.steps, seconds: t.seconds, cost: t.cost, costSoFar: t.costSoFar,
      models: callsByModel(data), left: t.left, leftSource: t.leftSource,
      laneMode: mode,
      rawStayed: !!(data.run && data.run.documents) && (mode === 'raw_local' || mode === 'all_local'),
      estimate: baselineEstimate(data),   // ruling 22: the floor, or null when none was stored
      noStep: noStepNote(data),           // ruling 26a: the planner wrote the answer alone, or null
      notProduced: notProducedLines(data), // ruling 32c: fields asked for and not produced
      failed: failedCounts(data)          // ruling 26e: failed steps, and failed items of the steps that went on
    };
  }
  /* The receipt's stat boxes as [k, v] pairs. */
  function receiptStats(r) {
    var B = window.Borg;
    var out = [['Steps', String(r.steps)], ['Models used', String(r.models.length)], ['Time', B.duration(r.seconds)],
      r.cost !== null && r.cost !== undefined ? ['Cost of this run', B.money(r.cost)] : ['Cost so far', B.money(r.costSoFar)]];
    if (r.estimate) out.push([r.estimate.label, 'US' + floorMoney(r.estimate.value)]);
    return out;
  }
  function leftChipText(left) {
    if (left === null || left === undefined) return 'Calls that left this machine: not recorded';
    return left === 0 ? 'No call left this machine' : left + (left === 1 ? ' call' : ' calls') + ' left this machine';
  }

  /* Ruling 18: the raw Details view carries no baseline figure. Removes every key that starts
   * with "baseline" (baseline_dollars, baseline_est_cost, routing.baseline, baseline_note,
   * per_item_baseline_est_cost) at any depth, also inside payloads stored as JSON text. */
  function stripBaseline(v) {
    if (typeof v === 'string') {
      if (!/baseline/i.test(v)) return v;
      var p = parseJSON(v);
      return p && typeof p === 'object' ? JSON.stringify(stripBaseline(p)) : v;
    }
    if (Array.isArray(v)) return v.map(stripBaseline);
    if (v && typeof v === 'object') {
      var out = {};
      Object.keys(v).forEach(function (k) { if (!/baseline/i.test(k)) out[k] = stripBaseline(v[k]); });
      return out;
    }
    return v;
  }

  /* "Why this model" rows. The router ranks the models that clear the bar by effective cost
   * (dollars plus waiting time); est_dollars is the dollars part alone. A model with no speed
   * figure, in a pool where no model has one, has no known cost (est_cost null) and is ranked
   * after every model with a cost (ruling 28h). The row it chose is labelled by what actually
   * decided it: lowest effective cost, a name from the planner, the 28h rule, or an alias or
   * strategy (written in routing.notes). `lowest` ignores nulls. */
  function whyRows(routing) {
    var cands = routing && Array.isArray(routing.candidates) ? routing.candidates : [];
    var lowest = null;
    cands.forEach(function (c) { var v = num(c.est_cost); if (v !== null && (lowest === null || v < lowest)) lowest = v; });
    return cands.map(function (c) {
      var won = c.model_id === routing.chosen;
      var cost = num(c.est_cost);
      var note = '';
      if (won) {
        if (routing.reason === 'planner_pick') note = 'Router’s choice: the planner named it';
        else if (routing.reason === 'fallback_default') note = 'Run’s default model: none cleared the bar';
        else if (cost !== null && (lowest === null || cost === lowest)) note = 'Router’s choice: lowest effective cost (dollars plus time) among those that clear ' + routing.floor;
        else if (cost === null && lowest === null) note = 'Router’s choice: speed not measured, ranked after priced models; no priced model cleared ' + routing.floor;
        else note = 'Router’s choice: picked by the routing alias or strategy (see the notes), not by cost alone';
      }
      return { model_id: c.model_id, won: won, score: c.score === undefined ? null : c.score,
        est_cost: num(c.est_cost), est_dollars: num(c.est_dollars), note: note };
    });
  }

  // ---------- live refresh (Run page) ----------
  /* makeRefresher({get, apply}) -> {request, cancel}. One GET in flight at a time: a request
   * made while one is running is remembered and sent once when it returns. Every GET gets a
   * sequence number; a response is applied only if it is the newest one issued and nothing
   * cancelled it, so an old answer can never overwrite a newer screen. */
  function makeRefresher(opts) {
    var seq = 0, applied = 0, inflight = false, again = false, dead = false;
    function run() {
      inflight = true; again = false;
      var mine = ++seq;
      return Promise.resolve().then(opts.get).then(function (data) {
        inflight = false;
        if (!dead && mine === seq && mine > applied) { applied = mine; opts.apply(data, mine); }
      }, function (err) {
        inflight = false;
        if (opts.onError && !dead) opts.onError(err);
      }).then(function () { if (again && !dead) return run(); });
    }
    return {
      request: function () { if (dead) return Promise.resolve(); if (inflight) { again = true; return Promise.resolve(); } return run(); },
      cancel: function () { dead = true; }
    };
  }
  /* makeLiveStream({open, finished, wait, onMessage}) -> {start, stop}. Opens the run's event
   * stream and, when it errors or closes, reconnects with a growing wait (1 s, 2 s, 4 s ... up
   * to 15 s; reset by any message) for as long as the run is not finished. `open(onmsg, onerr)`
   * returns an object with close(); `wait(fn, ms)` is setTimeout. */
  function makeLiveStream(opts) {
    var src = null, delay = 1000, timer = null, stopped = false;
    function lost() {
      if (src) { try { src.close(); } catch (_) {} src = null; }
      if (stopped || opts.finished() || timer) return;
      var d = delay; delay = Math.min(delay * 2, 15000);
      timer = opts.wait(connect, d);
    }
    function connect() {
      timer = null;
      if (stopped || src || opts.finished()) return;
      src = opts.open(function (msg) { delay = 1000; opts.onMessage(msg); }, lost);
    }
    return {
      start: connect, lost: lost,
      stop: function () { stopped = true; if (src) { try { src.close(); } catch (_) {} src = null; } },
      delay: function () { return delay; }
    };
  }

  // ---------- plain words ----------
  var FAILURES = {
    'BRT-PIP-008': 'The run took longer than its time limit and was stopped.',
    'BRT-PIP-002': 'Planning or running the steps failed.',
    'BRT-LANE-001': 'A document path was outside the documents folder, so it was refused.',
    'BRT-LANE-002': 'No model on this machine is good enough for a step that reads raw documents, so the step was not sent anywhere.',
    'BRT-LANE-004': 'The model on this machine reported a failure on a document step.',
    'BRT-LANE-005': 'The lane setting does not allow this request.',
    'BRT-ORC-003': 'The planner answered without a plan, but this run has documents.',
    'BRT-ORC-024': 'The step waited on a loop of steps that can never finish.',
    'BRT-ORC-025': 'A step it depends on failed, so it was not run.',
    'BRT-ANS-009': 'The model’s reply was cut off by the token limit.',
    'BRT-ANS-010': 'The model said it could not do the step.',
    'BRT-REG-020': 'No ticked model clears the quality bar for this kind of work.',
    'BRT-REG-016': 'The speed of a model on this machine could not be measured, so it is priced as no faster than the slowest measured model.',
    'BRT-APE-005': 'The list this step should work through was empty or missing.',
    'BRT-APE-006': 'The list this step should work through could not be read.',
    'BRT-SKL-008': 'The skill was given data it cannot use.'
  };
  function failureWords(text) {
    if (text === null || text === undefined || text === '') return null;
    var s = String(text);
    var m = /BRT-[A-Z]+-\d+[A-Z-]*/.exec(s);
    var code = m ? m[0] : null;
    var detail = s.replace(/^\s*BRT-[A-Z]+-\d+[A-Z-]*\s*:?\s*/, '');
    return { code: code, words: (code && FAILURES[code]) || 'It failed.', detail: detail };
  }

  /* Plain words for a stage or decision code, as the live Ask line and the Run timeline show it. A
   * code with no entry reads "Working": never the raw code. */
  var STAGE_WORDS = {
    plan: 'Planning the steps', re_plan: 'Reading the results', compose: 'Writing the answer',
    route: 'Choosing a model for a step', rule_zero_shortcut: 'Answering in one call', bypass: 'Answering directly',
    orchestrator: 'Working',
    'BRT-ORC-018': 'Retrying a reply that was cut off', 'BRT-ORC-028': 'Writing the answer',
    'BRT-ORC-029': 'Asking for the answer once more', 'BRT-ORC-030': 'Putting the finished steps together',
    'BRT-ORC-031': 'The planner declined to answer', 'BRT-ORC-032': 'A list marker named no list step', 'BRT-ZERO-01-ESCALATED': 'Moving from one call to a plan',
    'BRT-APE-CAPPED': 'Limiting a long list', 'BRT-ORC-NO-FRONTIER': 'Choosing a model for a step'
  };
  function stageWords(stage) {
    var key = stage === null || stage === undefined ? '' : String(stage);
    return Object.prototype.hasOwnProperty.call(STAGE_WORDS, key) ? STAGE_WORDS[key] : 'Working';
  }

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  /* itemsLine(step) -> "N items in M calls" (ruling 23): M is the step's packed posts plus one
   * call for every item not filed from a pack value; "; k retried alone" when items went to a
   * single call after being in a pack. With packing off M equals N. */
  function itemsLine(step) {
    var n = step && num(step.items);
    if (!n) return null;
    var posts = num(step.packPosts) || 0, accepted = num(step.packAccepted) || 0, alone = num(step.retriedAlone) || 0;
    var m = posts + Math.max(0, n - accepted);
    return plural(n, 'item') + ' in ' + plural(m, 'call') + (alone ? '; ' + alone + ' retried alone' : '');
  }
  function clock(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var m = Math.floor(sec / 60), s = sec - m * 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  var REASONS = { routed: 'the lowest effective cost among the models that clear the bar', planner_pick: 'the planner named it',
    fallback_default: 'no model cleared the bar, so the run’s default model was used' };

  /* eventSentences(data) -> [{at: "m:ss", text}]. Back-to-back calls to one model become one
   * sentence with a count. An event whose text was hidden (lane on, not admin) says so. */
  function eventSentences(data) {
    data = data || {};
    var B = window.Borg;
    var start = B.parseWhen((data.run || {}).created_at);
    var steps = stepsFromRun(data), stepByEnv = {};
    steps.forEach(function (s) { stepByEnv[s.envelope.envelope_id] = s; });
    var out = [], last = null;
    function at(ev) {
      var t = B.parseWhen(ev.created_at);
      return clock(t && start ? (t.getTime() - start.getTime()) / 1000 : 0);
    }
    function push(ev, text) { last = null; out.push({ at: at(ev), text: text }); }
    (data.events || []).forEach(function (ev) {
      var d = decisionOf(ev);
      var model = shortModel(ev.model_id);
      var step = stepByEnv[ev.envelope_id];
      var name = step ? '“' + step.label + '”' : 'a step';
      if (ev.event_type === 'model_call') {
        if (packShare(ev)) return;          // a packed post is told once, by its first share
        if (last && last.model === ev.model_id && last.env === (step ? step.id : null)) {
          last.count += 1;
          last.entry.text = last.count + ' calls went to ' + model + (step ? ' for ' + name : '') + '.';
          return;
        }
        var entry = { at: at(ev), text: model + ' answered ' + name + '.' };
        out.push(entry);
        last = { model: ev.model_id, env: step ? step.id : null, count: 1, entry: entry };
        return;
      }
      if (ev.event_type === 'orchestrator_decision') {
        var mode = d && d.mode;
        if (mode === 'pack_outcome') return;   // counts only; the calls are told by their model_call
        if (!d) return push(ev, 'The planner made a decision (details hidden while the lane is on).');
        if (mode === 'plan') {
          var nodes = d.parsed && d.parsed.plan && d.parsed.plan.nodes;
          if (d.parsed && d.parsed.direct_answer) return push(ev, 'The planner (' + model + ') answered directly, with no steps.');
          return push(ev, 'The planner (' + model + ') split the job into ' + (Array.isArray(nodes) ? plural(nodes.length, 'step') : 'steps') + '.');
        }
        if (mode === 're_plan') {
          var more = d.parsed && d.parsed.plan && d.parsed.plan.nodes;
          return push(ev, Array.isArray(more) && more.length
            ? 'The planner (' + model + ') read the results and added ' + plural(more.length, 'more step') + '.'
            : 'The planner (' + model + ') read the results and decided no more steps were needed.');
        }
        if (mode === 'compose') return push(ev, model + ' wrote the final answer.');
        if (mode === 'route') {
          var why = REASONS[d.reason] || d.reason || 'routed';
          return push(ev, (step ? name : 'Step ' + (d.node_id || '')) + ' went to ' + shortModel(d.chosen) + ' (' + why + ').');
        }
        if (mode === 'rule_zero_shortcut') return push(ev, 'The question was answered in one call, without a plan.');
        if (!mode && d.code) return push(ev, stageWords(d.code) + '.');
        return push(ev, mode && Object.prototype.hasOwnProperty.call(STAGE_WORDS, mode) ? stageWords(mode) + '.' : 'The planner recorded a decision.');
      }
      if (ev.event_type === 'error') {
        if (!d) return push(ev, 'A problem was recorded (details hidden while the lane is on).');
        var f = failureWords(d.code ? d.code + ': ' + (d.msg || '') : d.msg);
        var warn = d.severity === 'warning';
        return push(ev, (warn ? 'Warning' : 'Problem') + (step ? ' in ' + name : '') + ': ' + (f ? f.words : 'see Details') + (d.code ? ' (' + d.code + ')' : ''));
      }
      if (ev.event_type === 'tool_call') return push(ev, 'A tool ran' + (step ? ' for ' + name : '') + '.');
      if (ev.event_type === 'child_residue') return push(ev, 'A step’s own run handed back its results.');
      if (ev.event_type === 'reorchestration') return push(ev, 'The one-call shortcut did not work, so the planner made a plan.');
      push(ev, String(ev.event_type || 'event').replace(/_/g, ' ') + '.');
    });
    return out;
  }

  // ---------- DOM helpers shared by the three pages ----------
  function svgEl(tag, attrs, text) {
    var n = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, String(attrs[k])); });
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function whereDot(local) {
    var d = document.createElement('span');
    d.className = 'dot ' + (local ? 'local' : 'cloud');
    d.setAttribute('aria-hidden', 'true');
    return d;
  }

  window.BorgWork = {
    LANE_MODES: LANE_MODES, laneTag: laneTag, historyForMode: historyForMode, laneLine: laneLine,
    readLaneMode: readLaneMode, prepareSend: prepareSend, runLaneMode: runLaneMode, runLaneWords: runLaneWords, leftCard: leftCard,
    headerTiles: headerTiles, receiptFrom: receiptFrom, noStepNote: noStepNote, failedLines: failedLines, notProducedLines: notProducedLines, receiptStats: receiptStats, leftChipText: leftChipText,
    stripBaseline: stripBaseline, whyRows: whyRows, makeRefresher: makeRefresher, makeLiveStream: makeLiveStream,
    parseJSON: parseJSON, decisionOf: decisionOf, stageWords: stageWords, shortModel: shortModel, shortText: shortText,
    planNodesById: planNodesById, stepsFromRun: stepsFromRun, planLayout: planLayout,
    callsByModel: callsByModel, runTotals: runTotals, baselineEstimate: baselineEstimate, floorNote: floorNote, floorMoney: floorMoney,
    failureWords: failureWords, eventSentences: eventSentences, clock: clock, REASONS: REASONS,
    itemsLine: itemsLine, packShare: packShare,
    svgEl: svgEl, whereDot: whereDot
  };
})();
