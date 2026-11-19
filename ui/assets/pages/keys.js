Borg.ready(function () {
  var A = window.BorgAdmin, B = window.Borg;
  function show(id, saved, fileOnly) {
    var c = A.$(id + '-state');
    c.className = 'chip ' + (saved ? 'ok' : 'warn');
    c.textContent = saved ? 'Saved' : fileOnly ? 'In .env, restart needed' : 'Not saved';
  }
  function refresh() {
    return B.api('/keys/status').then(function (r) {
      r.providers.forEach(function (p) { if (A.$(p.id + '-state')) show(p.id, p.saved, p.env_file_only); });
    }, function () {});
  }
  A.$('openrouter-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var input = A.$('openrouter-key'), key = input.value.trim(), msg = A.$('openrouter-msg');
    if (!key) { msg.textContent = 'Paste a key first.'; return; }
    msg.textContent = 'Saving...';
    B.api('/keys', { json: { name: 'openrouter', base_url: 'https://openrouter.ai/api/v1', api_key: key } }).then(function () {
      input.value = '';
      msg.textContent = 'Saved. The key is not shown again.';
      return refresh();
    }, function () { msg.textContent = ''; });
  });
  refresh();
});
