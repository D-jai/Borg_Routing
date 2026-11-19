Borg.ready(function () {
  var A = window.BorgAdmin, h = A.h, B = window.Borg;
  B.api('/config').then(function (cfg) { if (cfg && cfg.global_rules) A.$('global-rules').value = cfg.global_rules; }, function () {});
  A.$('save-global').addEventListener('click', function () {
    B.api('/config', { json: { key: 'global_rules', value: A.$('global-rules').value.trim() } }).then(function () {
      A.$('global-msg').textContent = 'Saved. It applies from the next run.';
    }, function () {});
  });
  function load() {
    return B.api('/config/rules').then(function (r) {
      var box = A.clear(A.$('ledger')), rules = r.rules || [];
      if (!rules.length) { box.appendChild(h('p', { class: 'muted small' }, 'No rules yet.')); return; }
      rules.forEach(function (rule) {
        var retired = !!rule.retired_at;
        var meta = 'Added ' + A.ago(rule.added_at) + (retired ? ', retired ' + A.ago(rule.retired_at) : '');
        var btn = null;
        if (!retired) {
          btn = h('button', { class: 'btn', type: 'button' }, 'Retire');
          btn.addEventListener('click', function () {
            B.api('/config/rules/' + encodeURIComponent(rule.id) + '/retire', { method: 'POST' }).then(load, function () {});
          });
        }
        box.appendChild(h('div', { class: 'rule-item' + (retired ? ' retired' : '') }, [
          h('div', {}, [h('div', {}, rule.text), h('div', { class: 'small muted' }, meta)]), btn
        ]));
      });
    }, function () { A.clear(A.$('ledger')).appendChild(h('p', { class: 'muted' }, 'The rules could not be loaded.')); });
  }
  A.$('add-rule').addEventListener('click', function () {
    var input = A.$('new-rule'), text = input.value.trim(), msg = A.$('rule-msg');
    if (!text) { msg.textContent = 'Type the rule first.'; return; }
    B.api('/config/rules', { json: { text: text } }).then(function () {
      input.value = ''; msg.textContent = 'Rule added.'; return load();
    }, function () {});
  });
  load();
});
