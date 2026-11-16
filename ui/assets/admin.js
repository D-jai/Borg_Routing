/* BORG_ROUTE admin pages: small shared helpers (work order 4.3).
 * Loaded after assets/app.js. Builds DOM with textContent only, never innerHTML with data.
 */
(function () {
  'use strict';
  var B = window.Borg;

  // h('div', {class: 'x'}, ['text', node, ...]) -> element. Strings become text nodes.
  function h(tag, attrs, kids) {
    var n = B.el(tag, attrs || {}, []);
    (kids === undefined ? [] : Array.isArray(kids) ? kids : [kids]).forEach(function (k) {
      if (k === null || k === undefined || k === false) return;
      n.appendChild(typeof k === 'object' ? k : document.createTextNode(String(k)));
    });
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }
  function $(id) { return document.getElementById(id); }

  function chip(text, kind) { return h('span', { class: 'chip' + (kind ? ' ' + kind : '') }, text); }
  function whereChip(local) {
    return h('span', { class: 'chip' }, [h('span', { class: 'dot ' + (local ? 'local' : 'cloud') }), local ? 'this machine' : 'cloud']);
  }
  // A model is local when it runs on this machine: an lmstudio or ollama row (unless the
  // server said it runs elsewhere), or a local OpenAI-compatible row (custom_local_*).
  function isLocalModel(m) {
    if (m.remote) return false;
    if (m.source === 'lmstudio' || m.source === 'ollama') return true;
    return m.source === 'custom_key' && String(m.model_id || '').indexOf('custom_local_') === 0;
  }

  function table(headers, rows, opts) {
    var head = h('tr', {}, headers.map(function (t) {
      var th = h('th', {}, t.label === undefined ? t : t.label);
      if (t.title) th.setAttribute('title', t.title);
      return th;
    }));
    var t = h('table', {}, [h('thead', {}, head), h('tbody', {}, rows)]);
    return h('div', { class: 'tscroll' }, t);
  }
  function td(content, cls) { return h('td', cls ? { class: cls } : {}, content); }
  function emptyRow(cols, text) { return h('tr', {}, h('td', { colspan: String(cols), class: 'muted' }, text)); }

  function ago(v) { return B.dateTime(v); }
  function short(text, n) {
    var s = String(text === null || text === undefined ? '' : text);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }
  function statusChip(status) {
    var kind = status === 'completed' ? 'ok' : status === 'failed' ? 'bad' : status === 'running' ? 'warn' : '';
    return chip(status || '-', kind);
  }

  // The words an error carries: {detail: {error, message}} or {detail: "text"}.
  function errorMessage(err) {
    var d = err && err.body && err.body.detail;
    if (d && typeof d === 'object') return d.message || d.error || 'The request failed.';
    return typeof d === 'string' ? d : (err && err.message) || 'The request failed.';
  }

  // Run again: POST /runs/{id}/resume. It re-plans the run from the start (R8).
  var RUN_AGAIN_HELP = 'Run again plans the run from the start, with the words you first sent. ' +
    'It does not continue where the run stopped, and it refuses runs that read documents.';
  function runAgainButton(runId, onDone) {
    var b = h('button', { class: 'btn', type: 'button', title: RUN_AGAIN_HELP }, 'Run again');
    b.addEventListener('click', function () {
      b.disabled = true;
      B.api('/runs/' + encodeURIComponent(runId) + '/resume', { method: 'POST', quiet: true }).then(function (res) {
        if (res && res.status === 'no_pending_work') B.notify('Nothing to run again: no step of this run failed or was left unfinished.');
        else B.notify('Run again started. It plans the run from the start; open the run to watch it.', 'ok');
        if (onDone) onDone();
      }, function (err) {
        B.notify('Could not run it again: ' + errorMessage(err), 'bad');
      }).then(function () { b.disabled = false; });
    });
    return b;
  }

  window.BorgAdmin = {
    h: h, clear: clear, $: $, chip: chip, whereChip: whereChip, isLocalModel: isLocalModel,
    table: table, td: td, emptyRow: emptyRow, ago: ago, short: short, statusChip: statusChip,
    errorMessage: errorMessage, runAgainButton: runAgainButton, RUN_AGAIN_HELP: RUN_AGAIN_HELP
  };
})();
