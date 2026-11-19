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
  // A model is local when the SERVER says so: every row of GET /models and /models/matrix
  // carries is_local, worked out from the endpoint's host, -cloud tags and the allow-list
  // (src/registry/locality.py). The page never guesses from the source label; a row without
  // the field counts as not local.
  function isLocalModel(m) {
    return !!m && m.is_local === true;
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
    'It does not continue where the run stopped. It is offered only for failed runs, and it refuses runs that read documents.';
  function runAgainButton(runId, onDone) {
    var b = h('button', { class: 'btn', type: 'button', title: RUN_AGAIN_HELP }, 'Run again');
    b.addEventListener('click', function () {
      b.disabled = true;
      B.api('/runs/' + encodeURIComponent(runId) + '/resume', { method: 'POST', quiet: true }).then(function (res) {
        if (res && res.status === 'no_pending_work') B.notify('Nothing to run again: this run has nothing waiting to be done.');
        else B.notify('Run again started. It plans the run from the start; open the run to watch it.', 'ok');
        if (onDone) onDone();
      }, function (err) {
        B.notify('Could not run it again: ' + errorMessage(err), 'bad');
      }).then(function () { b.disabled = false; });
    });
    return b;
  }

  // Models page speed cell (ruling 28). A probe running in the background shows "speed: measuring";
  // a failed one shows its code. Below 10 tok/s one decimal, so a slow host never reads "0 t/s".
  function speedText(m) {
    if (m && m.speed_status === 'measuring') return 'speed: measuring';
    var v = m ? m.throughput_tps : null;
    if (typeof v === 'number' && v > 0) {
      var t = v >= 10 ? v.toFixed(0) : v.toFixed(1);
      if (Number(t) === 0) t = v.toPrecision(1);
      return t + ' t/s';
    }
    if (m && m.speed_status === 'BRT-REG-016') return 'not measured (BRT-REG-016)';
    return '-';
  }

  window.BorgAdmin = {
    h: h, clear: clear, $: $, chip: chip, whereChip: whereChip, isLocalModel: isLocalModel,
    table: table, td: td, emptyRow: emptyRow, ago: ago, short: short, statusChip: statusChip,
    errorMessage: errorMessage, runAgainButton: runAgainButton, RUN_AGAIN_HELP: RUN_AGAIN_HELP,
    speedText: speedText
  };
})();
