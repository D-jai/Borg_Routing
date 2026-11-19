Borg.ready(function () {
  var A = window.BorgAdmin, h = A.h, B = window.Borg;
  A.$('again-help').textContent = A.RUN_AGAIN_HELP;

  function check(ok, title, detail, link) {
    var d = [detail];
    if (link) d.push(' ', h('a', { href: link[1] }, link[0]));
    return h('div', { class: 'check' }, [
      h('span', { class: 'mark ' + (ok ? 'yes' : 'no') }, ok ? '✓' : '!'),
      h('div', {}, [h('strong', {}, title), h('div', { class: 'small muted' }, d)])
    ]);
  }
  function get(path) { return B.api(path, { quiet: true }).catch(function () { return null; }); }

  Promise.all([get('/keys/status'), get('/models/matrix'), get('/status/local'), get('/config/effective')]).then(function (r) {
    var keys = r[0], models = r[1], local = r[2], cfg = r[3], box = A.clear(A.$('checklist'));
    var or = keys && keys.providers.filter(function (p) { return p.id === 'openrouter'; })[0];
    box.appendChild(check(!!(or && or.saved), or && or.saved ? 'Cloud key saved' : (or && or.env_file_only ? 'Cloud key in .env, restart needed' : 'Cloud key not saved'),
      'OpenRouter. The key is never shown here. ', ['Keys', 'keys.html']));
    var pool = (models || []).filter(function (m) { return m.checked; });
    var nLocal = pool.filter(A.isLocalModel).length;
    box.appendChild(check(pool.length > 0, pool.length + (pool.length === 1 ? ' model' : ' models') + ' in the pool',
      pool.length ? nLocal + ' on this machine, ' + (pool.length - nLocal) + ' cloud. ' : 'With none ticked, every question fails. ', ['Models', 'models.html']));
    var up = local && local.servers.filter(function (s) { return s.reachable; });
    box.appendChild(check(!!(local && local.reachable),
      local && local.reachable ? 'Local model server reachable' : 'Local model server not reachable',
      local ? (up.length ? up.map(function (s) { return s.name + ' at ' + s.url; }).join(', ') : 'Checked ' + local.servers.map(function (s) { return s.name; }).join(', ') + '. Cloud-only use still works.') : 'Could not check.'));
    var tokenSet = !!(keys && keys.admin_token_set);
    box.appendChild(check(tokenSet, tokenSet ? 'Admin token set' : 'Admin token not set',
      tokenSet ? 'Admin pages ask for it once per browser tab. ' : 'Admin pages are open to this computer only. Set BORG_ADMIN_TOKEN in .env, then restart, before others can reach this server. '));
    var lane = cfg && cfg.settings.filter(function (s) { return s.key === 'lane_mode'; })[0];
    var mode = lane ? lane.value : 'off';
    var words = { off: 'Lane is off: documents are not read', raw_local: 'Lane: raw documents stay local', all_local: 'Lane: nothing leaves this machine',
      by_reference: 'Lane: by reference, no privacy promise' };
    var laneDetail = mode === 'by_reference' ? 'Steps that read documents may run on cloud models, which receive the text. The copy checks do not run. ' : '';
    box.appendChild(check(mode !== 'off' && mode !== 'by_reference', words[mode] || ('Lane: ' + mode), laneDetail, ['Settings', 'settings.html']));
  });

  Promise.all([get('/logs/cost-summary'), get('/runs?limit=200')]).then(function (r) {
    var cost = r[0] || { by_model: [], totals: {} }, runs = (r[1] && r[1].runs) || [];
    var counts = (r[1] && r[1].counts) || { total: runs.length, failed: runs.filter(function (x) { return x.status === 'failed'; }).length };
    var failed = counts.failed;
    var t = cost.totals || {}, tiles = A.clear(A.$('tiles'));
    function tile(k, v, sub) { return h('div', { class: 'tile' }, [h('div', { class: 'k' }, k), h('div', { class: 'v num' }, v), sub ? h('div', { class: 'small muted' }, sub) : null]); }
    tiles.appendChild(tile('Spent, all logged calls', B.money(t.total_cost || 0), (t.total_calls || 0) + ' paid calls'));
    tiles.appendChild(tile('Runs', String(counts.total), failed + ' failed'));
    tiles.appendChild(tile('Tokens', ((t.total_tokens_in || 0) + (t.total_tokens_out || 0)).toLocaleString()));
    var spend = A.clear(A.$('spend')), by = cost.by_model || [];
    var max = by.reduce(function (m, x) { return Math.max(m, Number(x.total_cost) || 0); }, 0);
    if (!by.length) { spend.appendChild(h('p', { class: 'muted small' }, 'Nothing spent yet. Ask a question and it appears here.')); }
    else {
      spend.appendChild(A.table(['Model', '', 'Spent'], by.slice(0, 8).map(function (x) {
        var w = max > 0 ? Math.round((Number(x.total_cost) || 0) / max * 100) : 0;
        var bar = h('div', { class: 'bar' }, h('i', { style: 'width:' + w + '%' }));
        return h('tr', {}, [A.td(x.model_id || '(unknown)'), A.td(bar), A.td(B.money(x.total_cost), 'num')]);
      })));
    }
    var box = A.clear(A.$('runs'));
    if (!runs.length) { box.appendChild(h('p', { class: 'muted small' }, 'No runs yet.')); return; }
    box.appendChild(A.table(['When', 'Question', 'Status', ''], runs.slice(0, 8).map(function (x) {
      var again = x.status === 'failed' ? A.runAgainButton(x.run_id, null) : null;
      var open = h('a', { href: 'run.html?id=' + encodeURIComponent(x.run_id) }, 'Open');
      return h('tr', {}, [A.td(A.ago(x.created_at), 'num'), A.td(h('div', { class: 'clip', title: x.user_instruction || '' }, A.short(x.user_instruction, 90))),
        A.td(A.statusChip(x.status)), A.td(h('div', { class: 'actions' }, [open, again]))]);
    })));
  });
});
