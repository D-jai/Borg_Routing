Borg.ready(function () {
  'use strict';
  var B = window.Borg, W = window.BorgWork, A = window.BorgAdmin, h = A.h;
  var runId = new URLSearchParams(location.search).get('id');
  var data = null, modelLocal = {}, selected = 'plan', showRaw = false;
  var refreshTimer = null, tickTimer = null, refresher = null, stream = null;

  if (!runId) { A.$('loading').textContent = 'No run chosen. Open one from the Runs page.'; return; }

  function where(m) {
    var l = modelLocal[m];
    if (l === undefined) return h('span', {}, W.shortModel(m) || '-');
    return h('span', {}, [W.shortModel(m) + ' ', h('span', { class: 'chip' }, [W.whereDot(l), l ? 'this machine' : 'cloud'])]);
  }
  // ---------- header ----------
  function renderHeader() {
    var run = data.run || {};
    var status = run.status || '-';
    var cls = status === 'completed' ? 'ok' : status === 'failed' ? 'bad' : 'run';
    var words = { completed: 'Completed', failed: 'Failed', running: 'Running', pending: 'Waiting to start' };
    A.clear(A.$('status-chip')).appendChild(h('span', { class: 'status ' + cls }, words[status] || status));
    A.$('run-meta').textContent = B.dateTime(run.created_at) + ' · run ' + String(run.run_id || runId).slice(0, 8) + '…';
    A.$('prompt').textContent = run.user_instruction || '-';
    var pl = A.$('parent-line');
    if (run.parent_run_id) {
      A.clear(pl).appendChild(document.createTextNode('This run was started by a step of '));
      pl.appendChild(h('a', { href: 'run.html?id=' + encodeURIComponent(run.parent_run_id) }, 'another run'));
      pl.appendChild(document.createTextNode('.'));
      pl.classList.remove('hidden');
    } else pl.classList.add('hidden');

    var f = status === 'failed' ? W.failureWords(run.failure_reason || 'failed') : null;
    A.$('failure').classList.toggle('hidden', !f);
    if (f) {
      A.$('failure-words').textContent = f.words + (f.code ? ' (' + f.code + ')' : '');
      A.$('failure-detail').textContent = f.detail;
    }

    A.$('run-lane').textContent = W.runLaneWords(data);
    var tiles = A.clear(A.$('tiles'));
    // Ruling 22: the comparison is a floor ("One pass on <model>: at least US$X"), never "would have cost" (workspace.js).
    W.headerTiles(data).forEach(function (t) {
      tiles.appendChild(h('div', { class: 'tile' }, [h('div', { class: 'k' }, t.k), h('div', { class: 'v num' }, t.v), t.sub ? h('div', { class: 'small muted' }, t.sub) : null]));
    });
    var oldNote = document.getElementById('floor-note');
    if (oldNote) oldNote.remove();
    var floorEst = W.baselineEstimate(data);
    if (floorEst) tiles.insertAdjacentElement('afterend', h('p', { class: 'small muted', id: 'floor-note' }, W.floorNote(floorEst)));
    // Ruling 26a: every planned step failed and the planning model wrote the answer alone.
    var oldNoStep = document.getElementById('no-step-note');
    if (oldNoStep) oldNoStep.remove();
    var noStep = W.noStepNote(data);
    if (noStep) tiles.insertAdjacentElement('afterend', h('p', { class: 'small no-step', id: 'no-step-note' }, noStep));
  }

  // ---------- plan diagram ----------
  function coreFigures(modes) {
    var out = { model: null, dollars: 0, ms: 0, tokens: 0, calls: 0, events: [] };
    (data.events || []).forEach(function (ev) {
      if (ev.event_type !== 'orchestrator_decision') return;
      var d = W.decisionOf(ev);
      if (!d || modes.indexOf(d.mode) < 0) return;
      out.events.push(ev);
      out.model = out.model || ev.model_id;
      out.calls += 1;
      out.dollars += Number(ev.dollars) || 0;
      out.ms += Number(ev.latency_ms) || 0;
      out.tokens += (Number(ev.tokens_input) || 0) + (Number(ev.tokens_output) || 0);
    });
    return out;
  }
  function renderPlan() {
    var steps = W.stepsFromRun(data);
    var lay = W.planLayout(steps);
    var running = !(data.run.status === 'completed' || data.run.status === 'failed');
    var svg = W.svgEl('svg', { class: 'plan fixed', viewBox: '0 0 ' + lay.width + ' ' + lay.height, width: lay.width, height: lay.height,
      role: 'img', 'aria-label': 'Plan diagram: plan, ' + steps.length + ' steps, compose' });
    lay.edges.forEach(function (e) { svg.appendChild(W.svgEl('path', { class: 'edge', d: e.d })); });
    var plan = coreFigures(['plan', 're_plan']), compose = coreFigures(['compose']);
    /* Lead ruling (plan-node text): text in a node is cut to the room it has, measured once the drawing is on the page,
     * so a model name never runs into the cost and a long title stays inside its box. */
    var fits = [];
    function fitLater(el, right) { fits.push(function () { fitEl(el, right()); }); return el; }
    function room(el, left, cost) { return function () { var cw = cost ? measure(cost) : 0; return (cost ? Number(cost.getAttribute('x')) - cw - 6 : left) - Number(el.getAttribute('x')); }; }
    lay.nodes.forEach(function (n) {
      var cls = 'node' + (n.kind !== 'step' ? ' core' : '') + (selected === n.key ? ' sel' : '');
      var s = n.step;
      if (s && s.status === 'failed') cls += ' failed';
      if (s && s.status === 'in_flight') cls += ' live';
      var g = W.svgEl('g', { class: cls, tabindex: '0', role: 'button', 'data-key': n.key });
      g.appendChild(W.svgEl('rect', { x: n.x, y: n.y, width: n.w, height: n.h }));
      var x = n.x + 14, y = n.y;
      function dotFor(model, cx, cy) {
        var l = modelLocal[model];
        if (l === undefined || !model) return;
        g.appendChild(W.svgEl('circle', { class: l ? 'loc' : 'cld', cx: cx, cy: cy, r: 4 }));
      }
      if (n.kind !== 'step') {
        var f = n.kind === 'plan' ? plan : compose;
        g.appendChild(W.svgEl('text', { class: 't', x: x, y: y + 24 }, n.kind === 'plan' ? 'Plan' : 'Compose answer'));
        if (f.model) {
          dotFor(f.model, x + 4, y + 42);
          var cm = g.appendChild(W.svgEl('text', { class: 'm', x: x + 14, y: y + 46 }, W.shortText(W.shortModel(f.model), 24)));
          var cc = g.appendChild(W.svgEl('text', { class: 'c', x: n.x + n.w - 8, y: y + 46, 'text-anchor': 'end' }, B.money(f.dollars)));
          fitLater(cm, room(cm, 0, cc));
        } else {
          g.appendChild(W.svgEl('text', { class: 'm', x: x, y: y + 46 }, running ? 'waiting' : 'no call recorded'));
        }
      } else {
        var st = g.appendChild(W.svgEl('text', { class: 't', x: x, y: y + 22 }, s.label));
        fitLater(st, room(st, n.x + n.w - 8));
        var meta = [];
        if (s.items) meta.push(s.items + ' items');
        if (s.type === 'state_op') meta.push('no model needed');
        else if (s.childRunId || (s.routing && s.routing.served_by === 'child_run')) meta.push('its own run');
        if (s.task_type) meta.push(s.task_type);
        if (s.tier) meta.push(s.tier);
        var sm = g.appendChild(W.svgEl('text', { class: 'm', x: x, y: y + 39 }, W.shortText(meta.join(' · ') || s.type, 40)));
        fitLater(sm, room(sm, n.x + n.w - 8));
        if (s.status === 'failed') {
          g.appendChild(W.svgEl('text', { class: 'm bad', x: x, y: y + 63 }, 'Failed'));
        } else if (s.model && s.type !== 'state_op') {
          dotFor(s.model, x + 4, y + 59);
          var smod = g.appendChild(W.svgEl('text', { class: 'm', x: x + 14, y: y + 63 }, W.shortText(W.shortModel(s.model), 24)));
        }
        var cost = s.status === 'in_flight' ? 'running' : B.money(s.dollars) + (s.latency_ms !== null ? ' · ' + B.duration(s.latency_ms / 1000) : '');
        var sc = g.appendChild(W.svgEl('text', { class: 'c', x: n.x + n.w - 8, y: y + 63, 'text-anchor': 'end' }, cost));
        if (smod) fitLater(smod, room(smod, 0, sc));
      }
      function pick() { selected = n.key; showRaw = false; renderPlan(); renderPanel(); }
      g.addEventListener('click', pick);
      g.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
      svg.appendChild(g);
    });
    A.clear(A.$('plan-box')).appendChild(svg);
    fits.forEach(function (f) { f(); });
  }
  function measure(el) { try { return el.getComputedTextLength ? el.getComputedTextLength() : 0; } catch (e) { return 0; } }
  function fitEl(el, maxPx) {
    var full = el.textContent;
    var cut = W.fitText(full, maxPx, function (t) { el.textContent = t; return measure(el); });
    el.textContent = cut;
    if (cut !== full) el.appendChild(W.svgEl('title', {}, full));
  }

  // ---------- side panel ----------
  function kv(rows) {
    var dl = h('dl', { class: 'kv' });
    rows.forEach(function (r) { if (r) { dl.appendChild(h('dt', {}, r[0])); dl.appendChild(h('dd', r[2] ? { class: r[2] } : {}, r[1])); } });
    return dl;
  }
  function leftFor(step) {
    // An apply_to_each item call (packed or single) names its step in step_envelope_id (ruling 23).
    var id = step.envelope.envelope_id;
    var rows = (data.outbound || []).filter(function (o) { return o.envelope_id === id || o.step_envelope_id === id; });
    if (rows.length) return 'Yes: ' + rows.length + (rows.length === 1 ? ' request' : ' requests') + ' (listed below)';
    if ((data.outbound || []).length) return 'No';
    var l = modelLocal[step.model];
    if (step.type === 'state_op') return 'No (no model call)';
    if (l === true) return 'No (a model on this machine)';
    if (l === false) return 'Yes (a cloud model)';
    return 'Not recorded';
  }
  function whyTable(routing) {
    var cands = Array.isArray(routing.candidates) ? routing.candidates : [];
    if (!cands.length) return h('p', { class: 'small muted' }, 'No candidate list was recorded for this step.');
    var rows = W.whyRows(routing).map(function (c) {
      var l = modelLocal[c.model_id];
      var name = h('span', {}, [l !== undefined ? W.whereDot(l) : null, ' ' + W.shortModel(c.model_id)]);
      name.setAttribute('title', c.model_id);
      var money = function (v) { return v === null ? '-' : B.money(v); };
      return h('tr', c.won ? { class: 'win' } : {}, [A.td(name), A.td(c.score === null ? '-' : String(c.score), 'num'),
        A.td(money(c.est_cost), 'num'), A.td(money(c.est_dollars), 'num'), A.td(c.note)]);
    });
    var t = A.table([
      'Model', 'Score',
      { label: 'Est. effective cost', title: 'What the router ranks by: estimated dollars plus the cost of waiting (time cost per second).' },
      { label: 'Est. dollars', title: 'Estimated dollars only (0 for a model on this machine).' }, ''], rows);
    var box = h('div', {}, [h('h3', { style: 'margin-bottom:6px' }, 'Why this model'), t]);
    if (routing.reason === 'fallback_default') box.appendChild(h('p', { class: 'small muted', style: 'margin:6px 0 0' }, 'No ticked model cleared the bar, so the run’s default model was used.'));
    if (routing.local_only) box.appendChild(h('p', { class: 'small muted', style: 'margin:6px 0 0' }, 'This step reads raw documents, so only models on this machine were offered.'));
    (Array.isArray(routing.notes) ? routing.notes : []).forEach(function (n) { box.appendChild(h('p', { class: 'small muted', style: 'margin:4px 0 0' }, String(n))); });
    box.appendChild(h('p', { class: 'small muted', style: 'margin:6px 0 0' }, 'The router ranks the models that clear the bar by effective cost (dollars plus time), lowest first. Both cost columns are estimates made before the step ran.'));
    return box;
  }
  function rawSwitch(obj) {
    var btn = h('button', { class: 'btn', type: 'button', 'aria-expanded': String(showRaw) }, showRaw ? 'Hide details' : 'Details (raw record)');
    var wrap = h('div', {}, btn);
    if (showRaw) {
      var text;
      // Ruling 18: baseline figures are not shown in the raw record either.
      try { text = JSON.stringify(W.stripBaseline(obj), null, 2); } catch (_) { text = String(obj); }
      wrap.appendChild(h('pre', { class: 'raw', style: 'margin-top:8px' }, text));
    }
    btn.addEventListener('click', function () { showRaw = !showRaw; renderPanel(); });
    return wrap;
  }
  function renderPanel() {
    var panel = A.clear(A.$('panel'));
    if (selected === 'plan' || selected === 'compose') {
      var f = coreFigures(selected === 'plan' ? ['plan', 're_plan'] : ['compose']);
      panel.appendChild(h('div', {}, [h('h3', {}, 'Step'), h('h2', {}, selected === 'plan' ? 'Plan' : 'Compose answer')]));
      panel.appendChild(h('p', { class: 'small muted', style: 'margin:0' }, selected === 'plan'
        ? 'The planner splits the job into steps and, after they run, may add more.'
        : 'The planner model writes the final answer from the steps’ results.'));
      panel.appendChild(kv([
        ['Model', f.model ? where(f.model) : 'No call recorded yet'],
        ['Calls', String(f.calls), 'num'],
        ['Cost, time', B.money(f.dollars) + ' · ' + B.duration(f.ms / 1000) + ' · ' + f.tokens.toLocaleString() + ' tokens', 'num']
      ]));
      panel.appendChild(rawSwitch(f.events));
      return;
    }
    var step = W.stepsFromRun(data).filter(function (s) { return 'step:' + s.id === selected; })[0];
    if (!step) { selected = 'plan'; return renderPanel(); }
    panel.appendChild(h('div', {}, [h('h3', {}, 'Step ' + step.id), h('h2', {}, W.shortText(step.instruction || step.label, 120))]));
    var r = step.routing || {};
    var f2 = step.failure ? W.failureWords(step.failure) : null;
    var child = step.childRunId ? h('a', { href: 'run.html?id=' + encodeURIComponent(step.childRunId) }, 'Open its run') : null;
    panel.appendChild(kv([
      ['Model', step.type === 'state_op' ? 'No model needed' : r.served_by === 'child_run' ? h('span', {}, ['This step ran as its own run, which chose its own models. ', child]) : where(step.model)],
      ['Kind of work', [step.task_type, step.tier].filter(Boolean).join(', ') || '-'],
      r.floor !== undefined && r.floor !== null ? ['Needed score', r.floor + ' or more', 'num'] : null,
      step.items ? ['Items', W.itemsLine(step), 'num'] : null,
      ['Cost, time', B.money(step.dollars) + ' · ' + (step.latency_ms === null ? '-' : B.duration(step.latency_ms / 1000)) +
        ' · ' + (step.tokens === null ? '-' : step.tokens.toLocaleString() + ' tokens'), 'num'],
      ['Input left this machine', leftFor(step)],
      step.dataScope === 'raw' ? ['Reads', 'Raw documents'] : null,
      ['Status', step.status === 'resolved' ? 'Done' : step.status === 'failed' ? 'Failed' : step.status === 'in_flight' ? 'Running' : String(step.status || '-')],
      f2 ? ['Why it failed', f2.words + (f2.code ? ' (' + f2.code + ')' : '') + (f2.detail ? ' ' + f2.detail : '')] : null
    ]));
    if (r.served_by === 'child_run') {
      var kids = (data.child_envelopes || []).filter(function (e) { return e.run_id === step.childRunId; });
      if (kids.length) {
        panel.appendChild(h('div', {}, [h('h3', { style: 'margin-bottom:6px' }, 'Its steps'),
          A.table(['Model', 'Status'], kids.map(function (e) { return h('tr', {}, [A.td(where(e.model_id)), A.td(e.status || '-')]); })),
          h('p', { class: 'small muted', style: 'margin:6px 0 0' }, 'Their cost is already inside this run’s total.')]));
      }
    } else if (step.routing) {
      panel.appendChild(whyTable(step.routing));
    }
    panel.appendChild(rawSwitch(step.envelope));
  }

  // ---------- timeline and what left ----------
  function renderTimeline() {
    var ol = A.clear(A.$('timeline'));
    var items = W.eventSentences(data);
    if (!items.length) ol.appendChild(h('li', {}, [h('span', { class: 'at' }, '-'), h('span', {}, 'Nothing recorded yet.')]));
    items.forEach(function (it) { ol.appendChild(h('li', {}, [h('span', { class: 'at' }, it.at), h('span', {}, it.text)])); });
  }
  var PURPOSE = { 'orchestrator:plan': 'Your request (and the file list, if any), for the planner', 'orchestrator:re_plan': 'Step results, for the planner',
    'orchestrator:compose': 'Step results, for the final answer', 'orchestrator:rule_zero_shortcut': 'Your request, answered in one call',
    answerer: 'A step’s request', bypass: 'A direct question', relay: 'A direct question', tool: 'A tool call (such as a web search)' };
  function renderLeft() {
    var card = A.$('left-card');
    var lc = W.leftCard(data);
    card.classList.toggle('hidden', !lc.show);
    if (!lc.show) return;
    var box = A.clear(A.$('left-list'));
    var rows = data.outbound || [];
    if (rows.length && lc.note) box.appendChild(h('p', { class: 'small muted', style: 'margin:0 0 8px' }, lc.note));
    if (!rows.length) { box.appendChild(h('p', { class: 'small muted', style: 'margin:0' }, 'Nothing left this machine for this run.')); return; }
    var hasBodies = rows.some(function (o) { return typeof o.body === 'string'; });
    box.appendChild(A.table(['To', 'What was sent', 'Size', 'Status'], rows.map(function (o) {
      var what = h('div', {}, PURPOSE[o.purpose] || String(o.purpose || 'A request'));
      if (typeof o.body === 'string') {
        var det = h('details', {}, [h('summary', { class: 'small' }, 'Read exactly what was sent'), h('pre', { class: 'raw' }, o.body)]);
        what.appendChild(det);
      }
      var size = typeof o.body === 'string' ? o.body.length.toLocaleString() + ' chars' : '-';
      var st = o.status === 'sent' ? 'Sent' + (o.http_status ? ' (' + o.http_status + ')' : '') : o.status === 'failed' ? 'Failed' : 'Not finished';
      return h('tr', {}, [A.td(W.shortModel(o.destination) || o.url || '-'), A.td(what), A.td(size, 'num'), A.td(st)]);
    })));
    box.appendChild(h('p', { class: 'small muted', style: 'margin:10px 0 0' }, hasBodies
      ? 'Open any row to read exactly what was sent.'
      : 'The text of each request is shown to the admin only. Enter the admin token on an Admin page, then reload.'));
  }

  function renderAll() {
    A.$('loading').classList.add('hidden');
    A.$('content').classList.remove('hidden');
    renderHeader(); renderPlan(); renderPanel(); renderTimeline(); renderLeft();
  }

  // ---------- loading and live updates ----------
  // One GET /runs/{id} in flight at a time; each response carries a sequence number and a
  // stale one is dropped (W.makeRefresher). The event stream reconnects with a growing wait
  // for as long as the run is not finished (W.makeLiveStream).
  function finished() { return !!data && (data.run.status === 'completed' || data.run.status === 'failed'); }
  refresher = W.makeRefresher({
    get: function () { return B.api('/runs/' + encodeURIComponent(runId), { quiet: true }); },
    apply: function (d) { data = d; renderAll(); if (finished()) stopLive(); },
    onError: function (err) {
      if (!data) A.$('loading').textContent = err && err.status === 404 ? 'No run with this id.' : 'Could not load the run: ' + A.errorMessage(err);
    }
  });
  function fetchRun() { return refresher.request(); }
  function scheduleRefresh() {
    if (refreshTimer) return;
    refreshTimer = setTimeout(function () { refreshTimer = null; fetchRun(); }, 400);
  }
  function stopLive() {
    if (stream) stream.stop();
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
    A.$('live').classList.add('hidden');
  }
  function startLive() {
    if (!data || finished() || stream || !window.EventSource) return;
    A.$('live').classList.remove('hidden');
    stream = W.makeLiveStream({
      finished: finished,
      wait: function (fn, ms) { return setTimeout(fn, ms); },
      open: function (onmsg, onerr) {
        var es = new EventSource('/runs/' + encodeURIComponent(runId) + '/stream');
        es.onmessage = function (e) { var msg; try { msg = JSON.parse(e.data); } catch (_) { return; } onmsg(msg); };
        es.onerror = function () { fetchRun(); onerr(); };   // the server closes the stream after 90 s too
        return es;
      },
      onMessage: function (msg) {
        if (msg.type === 'envelope_update') scheduleRefresh();
        else if (msg.type === 'run_terminal') { fetchRun(); }
        else if (msg.type === 'error') { fetchRun(); stream.lost(); }
      }
    });
    stream.start();
    tickTimer = setInterval(function () { if (data) renderHeader(); }, 1000);
  }

  B.api('/models/matrix', { quiet: true }).catch(function () { return null; }).then(function (rows) {
    (rows || []).forEach(function (m) { modelLocal[m.model_id] = A.isLocalModel(m); });
    return fetchRun();
  }).then(function () { startLive(); });
});
