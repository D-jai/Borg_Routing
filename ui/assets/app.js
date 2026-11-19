/* BORG_ROUTE shared UI: shell, theme, api(), formatters, safe markdown.
 * Plain JS, no build step. Pages load this after marked/purify (only when they render markdown):
 *   <link rel="stylesheet" href="assets/app.css">
 *   <script src="assets/app.js"></script>
 * A page puts its content in <main id="page" data-page="ask">; the shell is built around it.
 * Pages call Borg.ready(fn) to run code once the shell exists.
 */
(function () {
  'use strict';

  var NAV = [
    { group: 'Workspace', items: [
      { id: 'ask', label: 'Ask', href: 'ask.html' },
      { id: 'runs', label: 'Runs', href: 'runs.html', also: ['run'] }
    ] },
    { group: 'Admin', items: [
      { id: 'admin_overview', label: 'Overview', href: 'admin_overview.html' },
      { id: 'models', label: 'Models', href: 'models.html' },
      { id: 'keys', label: 'Keys', href: 'keys.html' },
      { id: 'rules', label: 'Rules', href: 'rules.html' },
      { id: 'settings', label: 'Settings', href: 'settings.html' },
      { id: 'skills', label: 'Skills', href: 'skills.html' },
      { id: 'logs', label: 'Logs', href: 'logs.html' }
    ] }
  ];

  var THEME_KEY = 'borg_theme';
  var TOKEN_KEY = 'borg_admin_token';

  // ---------- storage (every access guarded: it may throw or be unavailable) ----------
  function safeGet(store, key) {
    try { return window[store].getItem(key); } catch (_) { return null; }
  }
  function safeSet(store, key, value) {
    try { window[store].setItem(key, value); return true; } catch (_) { return false; }
  }

  // ---------- dom helpers ----------
  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'text') n.textContent = attrs[k];
      else if (k === 'class') n.className = attrs[k];
      else n.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { n.appendChild(c); });
    return n;
  }

  // ---------- theme ----------
  function currentIsDark() {
    var t = document.documentElement.getAttribute('data-theme');
    if (t) return t === 'dark';
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  function applySavedTheme() {
    var saved = safeGet('localStorage', THEME_KEY);
    if (saved === 'light' || saved === 'dark') document.documentElement.setAttribute('data-theme', saved);
  }
  function toggleTheme() {
    var next = currentIsDark() ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    safeSet('localStorage', THEME_KEY, next);
  }

  // ---------- notices (inline, never alert) ----------
  function noticeSlot() {
    var slot = document.getElementById('notice-slot');
    if (!slot) {
      slot = el('div', { id: 'notice-slot', class: 'notice-slot', 'aria-live': 'polite' });
      var page = document.getElementById('page');
      if (page) page.insertBefore(slot, page.firstChild);
      else document.body.appendChild(slot);
    }
    return slot;
  }
  function notify(message, kind) {
    var n = el('div', { class: 'notice' + (kind ? ' ' + kind : ''), role: kind === 'bad' ? 'alert' : 'status', text: message });
    noticeSlot().appendChild(n);
    return n;
  }
  function clearNotices() {
    var slot = document.getElementById('notice-slot');
    if (slot) slot.textContent = '';
  }

  // ---------- api ----------
  function getToken() { return safeGet('sessionStorage', TOKEN_KEY); }
  function setToken(t) { return safeSet('sessionStorage', TOKEN_KEY, t); }

  function sameOrigin(path) {
    try { return new URL(path, location.href).origin === location.origin; } catch (_) { return false; }
  }

  function errorWords(status, body) {
    var detail = body && (body.detail || body.error || body.message);
    if (detail && typeof detail !== 'string') {
      try { detail = JSON.stringify(detail); } catch (_) { detail = null; }
    }
    if (status === 401) return 'The admin token is missing or wrong. Enter it again and retry.';
    if (status === 403) {
      var code = body && body.detail && body.detail.error;
      if (code === 'BRT-API-046') return 'Remote admin is off. This server has no BORG_ADMIN_TOKEN set, so only the computer it runs on can change settings. Set BORG_ADMIN_TOKEN in .env on the server, restart it, then enter the token here.';
      return 'This server does not allow that here' + (detail ? ': ' + detail : '.');
    }
    if (status === 404) return 'Not found' + (detail ? ': ' + detail : '.');
    if (status >= 500) return 'The server failed' + (detail ? ': ' + detail : '.') + ' Check the Logs page, then retry.';
    return detail ? String(detail) : 'The request failed (HTTP ' + status + ').';
  }

  /* Ask for the admin token once, inline (never alert). Parallel 401s share one form.
   * Resolves with the token after Save; rejects after Cancel. */
  var tokenAsk = null;
  function askToken() {
    if (tokenAsk) return tokenAsk;
    tokenAsk = new Promise(function (resolve, reject) {
      var input = el('input', { type: 'password', id: 'admin-token-input', autocomplete: 'off', 'aria-label': 'Admin token' });
      var save = el('button', { type: 'button', class: 'btn', text: 'Save token' });
      var cancel = el('button', { type: 'button', class: 'btn', text: 'Cancel' });
      var box = el('div', { class: 'notice', role: 'alert' }, [
        el('div', { text: 'This action needs the admin token (BORG_ADMIN_TOKEN on the server). It is kept for this browser tab only.' }),
        input, save, cancel
      ]);
      function done() { if (box.parentNode) box.parentNode.removeChild(box); tokenAsk = null; }
      save.addEventListener('click', function () {
        var v = String(input.value || '').trim();
        if (!v) return;
        setToken(v); done(); resolve(v);
      });
      cancel.addEventListener('click', function () { done(); reject(new Error('cancelled')); });
      noticeSlot().appendChild(box);
    });
    return tokenAsk;
  }

  /* api(path, opts): returns parsed JSON (or text) on success; on failure shows an inline
   * notice and rejects with an Error that has .status. opts.json is sent as a JSON body.
   * opts.quiet = true skips the notice. A 401 from this origin asks for the admin token
   * once (inline form), keeps it in sessionStorage and retries the call once. */
  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', headers: {} };
    Object.keys(opts.headers || {}).forEach(function (k) { init.headers[k] = opts.headers[k]; });
    var token = sameOrigin(path) ? getToken() : null; // never send the token to another origin
    if (token) init.headers['Authorization'] = 'Bearer ' + token;
    if (opts.json !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.json);
      if (!opts.method) init.method = 'POST';
    } else if (opts.body !== undefined) {
      init.body = opts.body;
    }
    if (opts.signal) init.signal = opts.signal;

    function fail(status, body, text) {
      var words = text || errorWords(status, body);
      if (!opts.quiet) notify(words, 'bad');
      var err = new Error(words);
      err.status = status;
      err.body = body;
      return err;
    }

    return fetch(path, init).then(function (res) {
      var ctype = res.headers.get('content-type') || '';
      var read = ctype.indexOf('json') >= 0 && ctype.indexOf('ndjson') < 0 ? res.json() : res.text(); // JSON Lines is text
      return read.catch(function () { return null; }).then(function (body) {
        if (res.status === 401 && token) setToken(''); // a wrong token is not kept
        if (res.status === 401 && !opts.retried && sameOrigin(path)) {
          var again = {};
          Object.keys(opts).forEach(function (k) { again[k] = opts[k]; });
          again.retried = true;
          return askToken().then(function () { return api(path, again); },
            function () { throw fail(401, typeof body === 'object' ? body : null); });
        }
        if (!res.ok) throw fail(res.status, typeof body === 'object' ? body : null);
        return body;
      });
    }, function () {
      throw fail(0, null, 'Could not reach the server. Check that BORG_ROUTE is running, then retry.');
    });
  }

  // ---------- formatters ----------
  // A number, or a non-blank numeric string; anything else (true, [], '', null, NaN) is not one.
  function toNum(v) {
    if (typeof v === 'string' && v.trim() !== '') v = Number(v);
    return typeof v === 'number' && isFinite(v) ? v : null;
  }
  function money(v) {
    var n = toNum(v);
    if (n === null) return '-';
    var sign = n < 0 ? '-' : '';
    n = Math.abs(n);
    if (n === 0) return '$0.00';
    if (n >= 1) return sign + '$' + n.toFixed(2);
    // Below $1: three significant figures, so a paid amount never reads as zero.
    if (n < 1e-9) return sign + '$' + n.toExponential(2);
    var s = n.toFixed(2 - Math.floor(Math.log10(n)));
    if (parseFloat(s) >= 1) s = n.toFixed(2); // rounded up to a whole dollar: two decimals
    return sign + '$' + s;
  }
  function duration(seconds) {
    var s = toNum(seconds);
    if (s === null || s < 0) return '-';
    if (s < 10) return (Math.round(s * 10) / 10) + ' s';
    if (s < 59.5) return Math.round(s) + ' s';
    var m = Math.floor(s / 60), r = Math.round(s - m * 60);
    if (r === 60) { m += 1; r = 0; }
    if (m < 60) return m + ' min ' + r + ' s';
    var h = Math.floor(m / 60);
    return h + ' h ' + (m - h * 60) + ' min';
  }
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
  function isDateOnly(v) { return typeof v === 'string' && DATE_ONLY.test(v.trim()); }
  function parseWhen(v) {
    if (v === null || v === undefined || v === '' || v === 0) return null;
    var d;
    if (typeof v === 'number') d = new Date(v < 1e12 ? v * 1000 : v);
    else if (v instanceof Date) d = v;
    else if (typeof v !== 'string') return null;
    else {
      var s = v.trim();
      if (DATE_ONLY.test(s)) {
        // A date with no time: a local calendar date, no time of day invented.
        d = new Date(Number(s.slice(0, 4)), Number(s.slice(5, 7)) - 1, Number(s.slice(8, 10)));
      } else {
        // Server timestamps without a zone (SQLite) are UTC.
        if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(s)) s = s.replace(' ', 'T') + 'Z';
        d = new Date(s);
      }
    }
    return isNaN(d.getTime()) ? null : d;
  }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function dateTime(v) {
    var d = parseWhen(v);
    if (!d) return '-';
    var day = pad2(d.getDate()) + '-' + MONTHS[d.getMonth()] + '-' + d.getFullYear();
    return isDateOnly(v) ? day : day + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }
  function timeOnly(v) {
    var d = parseWhen(v);
    return d && !isDateOnly(v) ? pad2(d.getHours()) + ':' + pad2(d.getMinutes()) : '-';
  }

  // ---------- safe markdown: marked, then DOMPurify ----------
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  /* A model's answer must never make the page load something from another site (a
   * tracking pixel, an exfiltrating image). Run by DOMPurify on every sanitised element:
   * src, srcset and poster survive only when they point at this origin (images may also
   * be data:image/); a link to another site loses its href and stays as plain text. The
   * Content-Security-Policy header (main.py) refuses the same loads a second time. */
  function sameOrigin(url) {
    try { return new URL(String(url).trim(), location.href).origin === location.origin; } catch (_) { return false; }
  }
  function okLoad(attr, value, tag) {
    var v = String(value).trim();
    if (attr === 'srcset') {
      return v.split(',').every(function (part) { var u = part.trim().split(/\s+/)[0]; return !u || okLoad('src', u, tag); });
    }
    if (/^data:image\/(png|jpe?g|gif|webp|svg\+xml);/i.test(v) && attr !== 'href') return true;
    return sameOrigin(v);
  }
  function dropExternalLoads(node) {
    if (!node || !node.tagName || !node.getAttribute) return;
    var tag = String(node.tagName).toLowerCase();
    ['src', 'srcset', 'poster', 'href', 'xlink:href', 'action'].forEach(function (attr) {
      var v = node.getAttribute(attr);
      if (v === null || v === undefined) return;
      if (!okLoad(attr, v, tag)) node.removeAttribute(attr);
    });
  }
  /* An external link is not clickable: it becomes its own text followed by its address in
   * brackets, so the reader still sees where it points. Runs on marked's output (its href is
   * already HTML-escaped, so it is safe as text). Same-origin and #anchor links stay links. */
  function externalLinksToText(html) {
    return String(html).replace(/<a\b[^>]*?\bhref="([^"]*)"[^>]*>([\s\S]*?)<\/a>/gi, function (m, href, inner) {
      var raw = href.replace(/&amp;/g, '&');
      if (!/^(https?:)?\/\//i.test(raw) || sameOrigin(raw)) return m;   // other schemes: the hook drops the href
      return inner + ' (' + escapeHtml(raw) + ')';
    });
  }
  var hooked = false;
  function markdown(text) {
    var src = text === null || text === undefined ? '' : String(text);
    if (window.marked && window.DOMPurify && window.DOMPurify.isSupported !== false) {
      if (!hooked && typeof window.DOMPurify.addHook === 'function') {
        window.DOMPurify.addHook('afterSanitizeAttributes', dropExternalLoads);
        hooked = true;
      }
      var parse = window.marked.parse || window.marked;
      var out = parse.call(window.marked, src, { gfm: true, breaks: true });
      return window.DOMPurify.sanitize(externalLinksToText(out));
    }
    // Libraries not loaded: show plain escaped text rather than unsanitised HTML.
    return '<p>' + escapeHtml(src).replace(/\n/g, '<br>') + '</p>';
  }

  // ---------- shell ----------
  function pageId() {
    var p = document.getElementById('page');
    if (p && p.getAttribute('data-page')) return p.getAttribute('data-page');
    var m = location.pathname.match(/([^\/]+)\.html$/);
    return m ? m[1] : '';
  }

  function buildShell() {
    var page = document.getElementById('page');
    if (!page) { page = el('main', { id: 'page', class: 'main' }); while (document.body.firstChild) page.appendChild(document.body.firstChild); }
    page.classList.add('main');
    var current = pageId();

    var logo = el('img', { class: 'brandmark', src: 'assets/logo.svg', alt: '' });
    var brand = el('div', {}, [
      el('div', { class: 'wordmark', text: 'BORG_ROUTE' }),
      el('div', { class: 'tagline', text: 'Brokered Orchestration, Retrieval and Generation' })
    ]);
    var replay = el('span', { class: 'chip replay hidden', id: 'replay-chip', text: 'Replay of a recorded run' });
    var theme = el('button', { class: 'btn', id: 'theme', type: 'button', 'aria-label': 'Switch light or dark', text: 'Theme' });
    theme.addEventListener('click', toggleTheme);
    var top = el('header', { class: 'topbar' }, [logo, brand, el('span', { class: 'spacer' }), replay, theme]);

    var side = el('nav', { class: 'side', 'aria-label': 'Sections' });
    NAV.forEach(function (g) {
      side.appendChild(el('h3', { text: g.group }));
      g.items.forEach(function (it) {
        var a = el('a', { href: it.href, text: it.label });
        if (it.id === current || (it.also || []).indexOf(current) >= 0) a.setAttribute('aria-current', 'page');
        side.appendChild(a);
      });
    });

    var layout = el('div', { class: 'layout' });
    layout.appendChild(side);
    layout.appendChild(page);
    document.body.insertBefore(layout, document.body.firstChild);
    document.body.insertBefore(top, layout);
    noticeSlot(); // notices sit at the top of the main column
  }

  var readyFns = [], isReady = false;
  function ready(fn) { if (isReady) fn(); else readyFns.push(fn); }

  /* Replay mode (work order 4.5): GET /replay/status says whether this server only plays
   * recorded demo runs. replayNotice(status) is the line every page shows then, or null in
   * live mode. replayStatus() asks once per page and resolves {mode: 'live'} on any failure. */
  function replayNotice(status) {
    if (!status || status.mode !== 'replay') return null;
    var text = typeof status.notice === 'string' && status.notice ? status.notice : 'Replay of a recorded run';
    return text + ' · demo run, not a benchmark result';
  }
  var replayAsk = null;
  function replayStatus() {
    if (!replayAsk) {
      replayAsk = api('/replay/status', { quiet: true }).then(function (st) {
        return st && typeof st === 'object' ? st : { mode: 'live' };
      }, function () { return { mode: 'live' }; });
    }
    return replayAsk;
  }
  function showReplayChip() {
    replayStatus().then(function (st) {
      var text = replayNotice(st), chip = document.getElementById('replay-chip');
      if (!text || !chip) return;
      chip.textContent = text;
      chip.classList.remove('hidden');
    });
  }

  function init() {
    applySavedTheme();
    buildShell();
    showReplayChip();
    isReady = true;
    readyFns.splice(0).forEach(function (fn) { fn(); });
  }

  applySavedTheme();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  /* download(path, filename, mime): fetches `path` through api() (so a missing token
   * prompts and retries), then saves the text as a file via a Blob and an object URL.
   * Resolves once the save was triggered; rejects (notice already shown) on failure. */
  function download(path, filename, mime) {
    return api(path).then(function (data) {
      var text = typeof data === 'string' ? data : JSON.stringify(data);
      var url = URL.createObjectURL(new Blob([text], { type: mime || 'text/plain' }));
      var a = el('a', { href: url, download: filename });
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    });
  }

  window.Borg = {
    api: api, download: download, notify: notify, clearNotices: clearNotices, ready: ready,
    money: money, duration: duration, parseWhen: parseWhen, dateTime: dateTime, timeOnly: timeOnly,
    markdown: markdown, escapeHtml: escapeHtml, el: el, dropExternalLoads: dropExternalLoads,
    getToken: getToken, setToken: setToken, replayNotice: replayNotice, replayStatus: replayStatus,
    storage: { get: safeGet, set: safeSet },
    nav: NAV
  };
  window.api = api;
})();
