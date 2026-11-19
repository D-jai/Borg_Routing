Borg.ready(function () {
  var A = window.BorgAdmin, h = A.h, B = window.Borg;
  var GROUPS = [
    ['Which model does the work', ['leaf_quality_light', 'leaf_quality_mid', 'leaf_quality_heavy', 'orchestrator_quality_requirement', 'baseline_model_id', 'time_cost_per_sec', 'orchestrator_endpoint', 'orchestrator_model_name']],
    ['Limits', ['max_run_seconds', 'rule_zero_shortcut', 'orchestrator_max_tokens', 'answerer_max_tokens']],
    ['On-premise lane (documents)', ['lane_mode', 'lane_manifest', 'lane_local_allowlist', 'lane_max_output_chars', 'lane_max_verbatim_chars', 'lane_max_ngram_share', 'lane_part_chars']]
  ];
  var SOURCE = { db: ['Saved here', 'ok'], env: ['From .env', ''], 'default': ['Default', ''] };

  function input(s) {
    var v = s.value === null || s.value === undefined ? '' : s.value;
    if (s.type === 'choice') {
      var sel = h('select', { 'aria-label': s.label }, s.choices.map(function (c) { return h('option', { value: c }, c); }));
      sel.value = String(v).toLowerCase(); return sel;
    }
    if (s.type === 'bool') {
      var b = h('select', { 'aria-label': s.label }, [h('option', { value: 'true' }, 'on'), h('option', { value: 'false' }, 'off')]);
      b.value = String(v).toLowerCase() === 'false' ? 'false' : 'true'; return b;
    }
    var attrs = { 'aria-label': s.label, type: s.type === 'text' ? 'text' : 'number' };
    if (s.type !== 'text') { attrs.step = s.type === 'int' ? '1' : 'any'; if (s.min !== null) attrs.min = String(s.min); if (s.max !== null) attrs.max = String(s.max); }
    var inp = h('input', attrs); inp.value = String(v); return inp;
  }

  function row(s) {
    var label = h('div', {}, [h('strong', {}, s.label), ' ', badge(s.key, s.source)]);
    var help = h('p', { class: 'small muted help' }, s.help_text);
    if (!s.editable) {
      return h('div', { class: 'setting' }, [label, h('div', { class: 'num' }, String(s.value)), h('span', { class: 'small muted' }, 'Set in .env (' + s.env_var + ')'), help]);
    }
    var inp = input(s), msg = h('span', { class: 'small muted', role: 'status' }), btn = h('button', { class: 'btn', type: 'button' }, 'Save');
    var reset = h('button', { class: 'btn', type: 'button', title: 'Forget the saved value: the .env value (or the default) applies again.' }, 'Reset');
    // Saving an empty value deletes the saved one. Show what applies afterwards.
    function afterReset() {
      return B.api('/config/effective').then(function (r) {
        var now = r.settings.filter(function (x) { return x.key === s.key; })[0];
        if (!now) return;
        var fresh = input(now); inp.replaceWith(fresh); inp = fresh;
        A.$('badge-' + s.key).replaceWith(badge(s.key, now.source));
        msg.textContent = 'Reset. ' + (now.source === 'env' ? 'The .env value applies again.' : 'The default applies again.');
      });
    }
    function save(value) {
      msg.textContent = 'Saving...';
      B.api('/config', { json: { key: s.key, value: value } }).then(function (res) {
        if (res && res.status === 'reset') return afterReset();
        msg.textContent = 'Saved. Used from the next run.';
        A.$('badge-' + s.key).replaceWith(badge(s.key, 'db'));
      }, function () { msg.textContent = ''; });
    }
    btn.addEventListener('click', function () { save(String(inp.value).trim()); });
    reset.addEventListener('click', function () { save(''); });
    return h('div', { class: 'setting' }, [label, inp, h('div', { class: 'actions' }, [btn, reset, msg]), help]);
  }
  function badge(key, source) {
    var t = SOURCE[source] || [source, ''], c = A.chip(t[0], t[1]); c.id = 'badge-' + key; return c;
  }

  B.api('/config/effective').then(function (r) {
    var by = {}; r.settings.forEach(function (s) { by[s.key] = s; });
    var box = A.clear(A.$('groups'));
    GROUPS.forEach(function (g) {
      var rows = g[1].filter(function (k) { return by[k]; }).map(function (k) { return row(by[k]); });
      box.appendChild(h('div', { class: 'card' }, [h('h2', { style: 'margin-bottom:4px' }, g[0])].concat(rows)));
    });
  }, function () { A.clear(A.$('groups')).appendChild(h('p', { class: 'muted' }, 'The settings could not be loaded.')); });
});
