Borg.ready(function () {
  var A = window.BorgAdmin, h = A.h, B = window.Borg;
  function tile(k, v, sub) { return h('div', { class: 'tile' }, [h('div', { class: 'k' }, k), h('div', { class: 'v num' }, String(v)), sub ? h('div', { class: 'small muted' }, sub) : null]); }
  function fail(id, text) { A.clear(A.$(id)).appendChild(h('p', { class: 'muted' }, text)); }
  var loaded = {};

  /* ---- Events ---- */
  function loadEvents() {
    var run = A.$('f-run').value.trim(), type = A.$('f-type').value, q = '/logs?limit=200';
    if (run) q += '&run_id=' + encodeURIComponent(run);
    if (type) q += '&event_type=' + encodeURIComponent(type);
    B.api(q).then(function (r) {
      var box = A.clear(A.$('events')), rows = r.logs || [];
      if (!rows.length) { box.appendChild(h('p', { class: 'muted' }, 'No events match.')); return; }
      box.appendChild(A.table(['When', 'Event', 'Run', 'Model', 'Tokens in / out', 'Cost', 'Time'], rows.map(function (e) {
        var run = e.run_id ? h('a', { href: 'run.html?id=' + encodeURIComponent(e.run_id) }, A.short(e.run_id, 10)) : '-';
        return h('tr', {}, [A.td(A.ago(e.created_at), 'num'), A.td(A.chip(e.event_type)), A.td(run, 'num'), A.td(e.model_id || '-'),
          A.td((e.tokens_input === null ? '-' : e.tokens_input) + ' / ' + (e.tokens_output === null ? '-' : e.tokens_output), 'num'),
          A.td(e.dollars === null || e.dollars === undefined ? '-' : B.money(e.dollars), 'num'),
          A.td(e.latency_ms === null || e.latency_ms === undefined ? '-' : B.duration(e.latency_ms / 1000), 'num')]);
      })));
    }, function () { fail('events', 'The events could not be loaded.'); });
  }
  A.$('f-go').addEventListener('click', loadEvents);
  A.$('f-export').addEventListener('click', function () {
    var run = A.$('f-run').value.trim();
    B.download('/logs/export?limit=1000' + (run ? '&run_id=' + encodeURIComponent(run) : ''), 'borg_route_logs.jsonl', 'application/x-ndjson')
      .catch(function () { /* the notice is already shown */ });
  });

  /* ---- Cost summary ---- */
  function loadCost() {
    return B.api('/logs/cost-summary').then(function (r) {
      var t = r.totals || {}, tiles = A.clear(A.$('cost-tiles'));
      tiles.appendChild(tile('Total spent', B.money(t.total_cost || 0), (t.total_calls || 0) + ' paid calls'));
      tiles.appendChild(tile('Tokens in', (t.total_tokens_in || 0).toLocaleString()));
      tiles.appendChild(tile('Tokens out', (t.total_tokens_out || 0).toLocaleString()));
      var box = A.clear(A.$('cost')), by = r.by_model || [];
      if (!by.length) { box.appendChild(h('p', { class: 'muted' }, 'No paid calls logged yet.')); return r; }
      box.appendChild(A.table(['Model', 'Calls', 'Tokens in', 'Tokens out', 'Cost'], by.map(function (x) {
        return h('tr', {}, [A.td(x.model_id || '(unknown)'), A.td(String(x.call_count), 'num'), A.td((x.total_tokens_in || 0).toLocaleString(), 'num'),
          A.td((x.total_tokens_out || 0).toLocaleString(), 'num'), A.td(B.money(x.total_cost), 'num')]);
      })));
      return r;
    }, function () { fail('cost', 'The cost summary could not be loaded.'); return null; });
  }

  /* ---- Stats ---- */
  function loadOutliers() {
    var sort = A.$('o-sort').value;
    B.api('/logs/outlier-runs?sort=' + sort + '&limit=5').then(function (r) {
      var box = A.clear(A.$('outliers')), rows = r.runs || [];
      if (!rows.length) { box.appendChild(h('p', { class: 'muted' }, 'No runs to rank yet.')); return; }
      box.appendChild(A.table(['Run', 'Question', 'Cost', 'Time', 'Tokens'], rows.map(function (x) {
        return h('tr', {}, [A.td(h('a', { href: 'run.html?id=' + encodeURIComponent(x.run_id) }, A.short(x.run_id, 10)), 'num'),
          A.td(h('div', { class: 'clip', title: x.user_instruction || '' }, A.short(x.user_instruction, 80))),
          A.td(B.money(x.cost), 'num'), A.td(x.duration_s === null ? 'not finished' : B.duration(x.duration_s), 'num'), A.td((x.tokens || 0).toLocaleString(), 'num')]);
      })));
    }, function () { fail('outliers', 'The outlier runs could not be loaded.'); });
  }
  A.$('o-sort').addEventListener('change', loadOutliers);

  function loadStats() {
    Promise.all([loadCost(), B.api('/runs?limit=200', { quiet: true }).catch(function () { return { runs: [] }; })]).then(function (r) {
      var by = (r[0] && r[0].by_model) || [], runs = r[1].runs || [], n = { completed: 0, failed: 0, running: 0 }, up = 0, down = 0;
      runs.forEach(function (x) { if (n[x.status] !== undefined) n[x.status]++; if (x.user_rating === 1) up++; if (x.user_rating === -1) down++; });
      var c = r[1].counts, total = runs.length;
      if (c) { n = c; up = c.thumbs_up; down = c.thumbs_down; total = c.total; }  // true totals from the server
      var oc = A.clear(A.$('outcomes'));
      oc.appendChild(tile('Runs', total)); oc.appendChild(tile('Completed', n.completed));
      oc.appendChild(tile('Failed', n.failed)); oc.appendChild(tile('Running', n.running));
      var fb = A.clear(A.$('feedback'));
      fb.appendChild(tile('Thumbs up', up)); fb.appendChild(tile('Thumbs down', down)); fb.appendChild(tile('Not rated', total - up - down));
      var lb = A.clear(A.$('leaderboard')), tok = function (x) { return (x.total_tokens_in || 0) + (x.total_tokens_out || 0); };
      var top = by.slice().sort(function (a, b) { return tok(b) - tok(a); }).slice(0, 8), max = top.length ? tok(top[0]) : 0;
      if (!top.length) lb.appendChild(h('p', { class: 'muted small' }, 'No model calls logged yet.'));
      else lb.appendChild(A.table(['Model', '', 'Tokens'], top.map(function (x) {
        var w = max > 0 ? Math.round(tok(x) / max * 100) : 0;
        return h('tr', {}, [A.td(x.model_id || '(unknown)'), A.td(h('div', { class: 'bar' }, h('i', { style: 'width:' + w + '%' }))), A.td(tok(x).toLocaleString(), 'num')]);
      })));
      var kids = {}; runs.forEach(function (x) { if (x.parent_run_id) (kids[x.parent_run_id] = kids[x.parent_run_id] || []).push(x); });
      var tree = A.clear(A.$('tree')), parents = runs.filter(function (x) { return kids[x.run_id]; }).slice(0, 10);
      if (!parents.length) tree.appendChild(h('p', { class: 'muted small' }, 'No run has started other runs yet.'));
      parents.forEach(function (p) {
        tree.appendChild(h('div', {}, [h('a', { href: 'run.html?id=' + encodeURIComponent(p.run_id) }, A.short(p.run_id, 10)), ' ', A.statusChip(p.status), ' ', A.short(p.user_instruction, 60)]));
        kids[p.run_id].forEach(function (c) { tree.appendChild(h('div', { class: 'small muted', style: 'padding-left:24px' }, ['started ', h('a', { href: 'run.html?id=' + encodeURIComponent(c.run_id) }, A.short(c.run_id, 10)), ' ', A.statusChip(c.status)])); });
      });
    });
    loadOutliers();
  }

  /* ---- Tabs ---- */
  var starters = { events: loadEvents, cost: loadCost, stats: loadStats };
  function select(name) {
    document.querySelectorAll('.tabs button').forEach(function (b) { b.setAttribute('aria-selected', String(b.getAttribute('data-tab') === name)); });
    ['events', 'cost', 'stats'].forEach(function (t) { A.$('tab-' + t).classList.toggle('hidden', t !== name); });
    if (!loaded[name]) { loaded[name] = true; starters[name](); }
  }
  document.querySelectorAll('.tabs button').forEach(function (b) { b.addEventListener('click', function () { select(b.getAttribute('data-tab')); }); });
  select('events');
});
