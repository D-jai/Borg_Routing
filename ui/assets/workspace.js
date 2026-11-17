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
      default: return { on: true, docs: false, text: 'Could not read the lane setting. Earlier turns are kept on screen but not resent.' };
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
    var usedIds = {};
    var steps = parents.map(function (e, i) {
      var out = parseJSON(e.outbound_payload) || {};
      var inb = parseJSON(e.inbound_payload) || {};
      var routing = e.routing && typeof e.routing === 'object' ? e.routing : null;
      var id = routing && routing.node_id ? String(routing.node_id) : null;
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
      var up = out.dependency_input && out.dependency_input.upstream_results;
      if (up && typeof up === 'object') deps = deps.concat(Object.keys(up));
      var items = num(routing && routing.items) || childCount[e.envelope_id] ||
        (pnode && Array.isArray(pnode.items) ? pnode.items.length : null);
      var tin = num(e.tokens_input), tout = num(e.tokens_output);
      return {
        id: id, envelope: e, type: e.type, instruction: out.instruction || (pnode && pnode.instruction) || '',
        label: shortText(e.skill_id ? e.skill_id.replace(/_/g, ' ') + ': ' + (out.instruction || '') : (out.instruction || id), 30),
        deps: deps, items: items || null, routing: routing, status: e.status,
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
      (num(ev.tokens_input) !== null || num(ev.dollars) !== null) && !!ev.model_id;
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
  /* runTotals(data, isLocal) -> {cost, costSoFar, seconds, steps, calls, left, leftSource}.
   * Cost is the digest's total for the whole run tree. Child envelopes' dollars are already
   * inside it and are never added again. Before a digest exists, the run's own logged
   * dollars are summed and marked "so far". */
  function runTotals(data, isLocal) {
    data = data || {};
    var run = data.run || {};
    var digest = data.digest || null;
    var soFar = 0;
    (data.events || []).forEach(function (ev) { var d = num(ev.dollars); if (d !== null) soFar += d; });
    var cost = digest && num(digest.cost_dollars) !== null ? num(digest.cost_dollars) : null;
    var calls = (data.events || []).filter(isCall);
    var outbound = Array.isArray(data.outbound) ? data.outbound : [];
    var left, leftSource;
    if (outbound.length) { left = outbound.length; leftSource = 'ledger'; }
    else if (isLocal) {
      left = calls.filter(function (ev) { return isLocal(ev.model_id) === false; }).length;
      leftSource = 'calls';
    } else { left = null; leftSource = 'unknown'; }
    var done = run.status === 'completed' || run.status === 'failed';
    return {
      cost: cost, costSoFar: cost === null ? soFar : null,
      // A finished run with no end time has no known duration; a live one counts to now.
      seconds: done && !run.completed_at ? null : seconds(run.created_at, run.completed_at),
      done: done,
      steps: stepsFromRun(data).length, calls: calls.length, left: left, leftSource: leftSource
    };
  }

  /* RULING 18 (BUILD.md section 8, 17-Nov-2026): the baseline figure (baseline_dollars /
   * baseline_est_cost: BORG's own tokens priced at the flagship) must not be shown as
   * "what one top model would have cost". This is the single switch for the receipt's and
   * the Run header's "Estimate, same steps on <baseline model>" line. It renders nothing
   * until the planner's replacement figure exists; the replacement plugs in here and both
   * pages pick it up (ask.html receipt grid, run.html Cost tile). */
  var SHOW_BASELINE_ESTIMATE = false;
  function baselineEstimate(data) {
    if (!SHOW_BASELINE_ESTIMATE) return null;
    return null; // replacement figure goes here (ruling 18)
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

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }
  function clock(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var m = Math.floor(sec / 60), s = sec - m * 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  var REASONS = { routed: 'the cheapest that clears the bar', planner_pick: 'the planner named it',
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
        return push(ev, 'The planner recorded a ' + String(mode || 'decision').replace(/_/g, ' ') + ' step.');
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
    parseJSON: parseJSON, decisionOf: decisionOf, shortModel: shortModel, shortText: shortText,
    planNodesById: planNodesById, stepsFromRun: stepsFromRun, planLayout: planLayout,
    callsByModel: callsByModel, runTotals: runTotals, baselineEstimate: baselineEstimate,
    failureWords: failureWords, eventSentences: eventSentences, clock: clock, REASONS: REASONS,
    svgEl: svgEl, whereDot: whereDot
  };
})();
