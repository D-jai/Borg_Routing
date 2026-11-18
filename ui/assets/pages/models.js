Borg.ready(function () {
  var A = window.BorgAdmin, h = A.h, B = window.Borg;
  var TASKS = [['reasoning', 'Reason'], ['coding', 'Code'], ['summarize', 'Summ.'], ['writing', 'Write'], ['roleplay', 'Role'], ['agentic', 'Agent'], ['extraction', 'Extract']];
  var statusEl = A.$('status');

  function ctx(n) { return !n ? '-' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n); }
  function price(m) {
    if (!m.pricing) return A.isLocalModel(m) ? 'free' : '-';
    var i = m.pricing.input_per_token * 1e6, o = m.pricing.output_per_token * 1e6;
    if (i === 0 && o === 0) return 'free';
    return B.money(i) + ' / ' + B.money(o);
  }
  function researched(m) {
    var s = m.task_scores || {};
    return TASKS.some(function (t) { var v = s[t[0]]; return typeof v === 'number' && v !== 5.0; });
  }
  function scoreCells(m) {
    if (!researched(m)) return [h('td', { colspan: '7', class: 'muted small' }, 'Not researched yet (flat 5.0). Press Get Model Info.')];
    return TASKS.map(function (t) {
      var v = (m.task_scores || {})[t[0]];
      return h('td', { class: 'num score' + (typeof v === 'number' ? '' : ' dim') }, typeof v === 'number' ? v.toFixed(1) : '-');
    });
  }
  function toggle(m, box) {
    var path = '/models/' + m.model_id.split('/').map(encodeURIComponent).join('/') + '/checked';
    B.api(path, { method: 'PATCH', json: { checked: box.checked } }).then(function () {
      m.checked = box.checked;
      statusEl.textContent = m.model_id + (box.checked ? ' is in the pool.' : ' is out of the pool.');
      A.$('no-pool').classList.toggle('hidden', models.some(function (x) { return x.checked; }));
      if (box.checked && A.isLocalModel(m) && !m.throughput_tps) load();  // shows "speed: measuring"
    }, function () { box.checked = !box.checked; });
  }

  var models = [];
  function render() {
    A.$('no-pool').classList.toggle('hidden', models.some(function (x) { return x.checked; }));
    var card = A.clear(A.$('pool'));
    if (!models.length) { card.appendChild(h('p', { class: 'muted' }, 'No models yet. Press Discover.')); return; }
    var sorted = models.slice().sort(function (a, b) {
      return (A.isLocalModel(b) - A.isLocalModel(a)) || String(a.model_id).localeCompare(String(b.model_id));
    });
    var heads = ['In pool', 'Model', 'Where', 'Context', { label: 'Price in / out per million', title: 'US dollars per million tokens' }, { label: 'Speed', title: 'Tokens per second' }]
      .concat(TASKS.map(function (t) { return { label: t[1], title: t[0] }; }));
    card.appendChild(A.table(heads, sorted.map(function (m) {
      var box = h('input', { type: 'checkbox', 'aria-label': 'In pool: ' + m.model_id });
      box.checked = !!m.checked;
      box.addEventListener('change', function () { toggle(m, box); });
      var cells = [A.td(box), A.td(m.model_id), A.td(A.whereChip(A.isLocalModel(m))), A.td(ctx(m.context_length), 'num'),
        A.td(price(m), 'num'), A.td(A.speedText(m), 'num')];
      return h('tr', {}, cells.concat(scoreCells(m)));
    })));
  }
  // A speed probe runs in the background after Discover or a tick: refetch until none is measuring.
  var again = null;
  function load() {
    return B.api('/models/matrix').then(function (list) {
      models = list || []; render();
      if (again) { clearTimeout(again); again = null; }
      if (models.some(function (x) { return x.speed_status === 'measuring'; })) again = setTimeout(load, 5000);
    }, function () {
      A.clear(A.$('pool')).appendChild(h('p', { class: 'muted' }, 'The model list could not be loaded.'));
    });
  }
  function busy(btn, text, call, done) {
    btn.disabled = true; statusEl.textContent = text;
    call().then(function (r) { statusEl.textContent = done(r); return load(); }, function () { statusEl.textContent = ''; })
      .then(function () { btn.disabled = false; });
  }
  A.$('discover').addEventListener('click', function () {
    busy(this, 'Looking for models...', function () { return B.api('/models/discover', { method: 'POST' }); },
      function (r) { return 'Discover finished: ' + r.upserted + ' models added or updated.'; });
  });
  A.$('research').addEventListener('click', function () {
    busy(this, 'Researching each model. This can take a minute or two...', function () { return B.api('/models/research', { json: {} }); },
      function (r) { return 'Get Model Info finished: ' + r.scored.length + ' models scored.'; });
  });
  load();
});
