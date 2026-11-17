Borg.ready(function () {
  'use strict';
  var B = window.Borg, W = window.BorgWork, A = window.BorgAdmin, h = A.h;
  var KEY = 'borg_ask_history_v2', MAX_TURNS = 60;
  var thread = A.$('thread'), q = A.$('q'), send = A.$('send');
  var laneMode = null;          // last mode read (null until read); re-read before every send
  var modelLocal = {};          // model_id -> true (this machine) / false (cloud)
  var turns = [];
  var pending = null;           // {stage, live, shown} while a question is running

  // ---------- history (localStorage, every access guarded) ----------
  function load() {
    try {
      var parsed = JSON.parse(B.storage.get('localStorage', KEY) || '[]');
      return Array.isArray(parsed) ? parsed : [];
    } catch (_) { return []; }
  }
  function save() {
    try { B.storage.set('localStorage', KEY, JSON.stringify(turns.slice(-MAX_TURNS))); } catch (_) {}
  }

  // ---------- lane line and documents control ----------
  function showLane() {
    var line = W.laneLine(laneMode);
    A.$('lane-text').textContent = line.text;
    A.$('lane-dot').className = 'dot ' + (line.local ? 'local' : line.on ? 'gold' : 'cloud');
    var btn = A.$('docs-btn');
    btn.disabled = !line.docs;
    btn.title = line.docs ? 'Send a folder by reference; the documents are read where the lane allows.'
      : 'Documents need the lane on (Settings, lane mode).';
    if (!line.docs) { A.$('docs-box').classList.add('hidden'); A.$('docs-folder').value = ''; btn.setAttribute('aria-expanded', 'false'); }
  }
  A.$('docs-btn').addEventListener('click', function () {
    var box = A.$('docs-box'), open = box.classList.contains('hidden');
    box.classList.toggle('hidden', !open);
    A.$('docs-btn').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) A.$('docs-folder').focus();
  });
  A.$('docs-clear').addEventListener('click', function () {
    A.$('docs-folder').value = '';
    A.$('docs-box').classList.add('hidden');
    A.$('docs-btn').setAttribute('aria-expanded', 'false');
  });

  // ---------- rendering ----------
  function userBubble(text, lane) {
    var b = h('div', { class: 'bubble' }, text);
    if (lane === 'all_local' && laneMode !== 'all_local') {
      b.setAttribute('title', 'Asked while nothing could leave this machine. Not resent under the current lane mode.');
    }
    return b;
  }
  function stat(k, v, cls) {
    return h('div', { class: 'stat' }, [h('div', { class: 'k' }, k), h('div', { class: 'v num' + (cls ? ' ' + cls : '') }, v)]);
  }
  function modelChip(model, calls) {
    var local = modelLocal[model];
    var kids = [];
    if (local !== undefined) kids.push(W.whereDot(local));
    kids.push(W.shortModel(model) + (local === true ? ' (this machine)' : local === false ? ' (cloud)' : '') + ' · ' + calls + (calls === 1 ? ' call' : ' calls'));
    var c = h('span', { class: 'chip', title: model }, kids);
    return c;
  }
  /* Receipt figures from GET /runs/{id}; numbers only, so they can be stored with the turn. */
  function receiptFrom(data) { return W.receiptFrom(data); }
  function receiptNode(turn) {
    var r = turn.receipt;
    var box = h('div', { class: 'receipt' }, h('h3', {}, 'Receipt'));
    if (!r) {
      box.appendChild(h('p', { class: 'small muted' }, turn.runId ? 'Loading the receipt...' : 'No run was recorded for this answer, so there is no receipt.'));
      return box;
    }
    var grid = h('div', { class: 'receipt-grid' }, W.receiptStats(r).map(function (p) { return stat(p[0], p[1]); }));
    box.appendChild(grid);
    var row = h('div', { class: 'row' }, r.models.map(function (m) { return modelChip(m.model, m.calls); }));
    if (r.rawStayed) row.appendChild(h('span', { class: 'chip' }, [h('span', { class: 'dot gold' }), 'Raw documents stayed on this machine']));
    // Worked out by the server. Unknown is "not recorded", never a zero.
    row.appendChild(h('span', { class: 'chip' }, r.left === null || r.left === undefined
      ? W.leftChipText(null) : [W.whereDot(r.left === 0), W.leftChipText(r.left)]));
    box.appendChild(row);
    return box;
  }
  function thumbs(turn) {
    var up = h('button', { class: 'btn', type: 'button', 'aria-label': 'Good answer', 'aria-pressed': String(turn.rating === 1) }, '👍');
    var down = h('button', { class: 'btn', type: 'button', 'aria-label': 'Bad answer', 'aria-pressed': String(turn.rating === -1) }, '👎');
    function rate(v) {
      B.api('/runs/' + encodeURIComponent(turn.runId) + '/rating', { json: { rating: v } }).then(function () {
        turn.rating = v; save();
        up.setAttribute('aria-pressed', String(v === 1));
        down.setAttribute('aria-pressed', String(v === -1));
      }, function () {});
    }
    up.addEventListener('click', function () { rate(1); });
    down.addEventListener('click', function () { rate(-1); });
    return h('span', { class: 'actions thumbs' }, [up, down]);
  }
  function answerCard(turn) {
    var card = h('article', { class: 'card answer' });
    var body = h('div', { class: 'md' });
    body.innerHTML = B.markdown(turn.content); // sanitised by DOMPurify inside Borg.markdown
    card.appendChild(body);
    card.appendChild(receiptNode(turn));
    var foot = h('div', { class: 'row', style: 'margin-top:8px' });
    if (turn.runId) {
      foot.appendChild(h('a', { href: 'run.html?id=' + encodeURIComponent(turn.runId) }, 'See how it was done'));
      foot.appendChild(thumbs(turn));
    }
    if (turn.lane === 'all_local' && laneMode !== 'all_local') {
      foot.appendChild(h('span', { class: 'small muted' }, 'Answered while nothing could leave this machine; not resent now.'));
    }
    card.appendChild(foot);
    return card;
  }
  function render() {
    A.clear(thread);
    if (!turns.length) thread.appendChild(h('p', { class: 'muted small' }, 'Ask for a job. Each answer shows which models did the steps, how long it took and what it cost.'));
    turns.forEach(function (t) {
      thread.appendChild(t.role === 'user' ? userBubble(t.content, t.lane) : answerCard(t));
    });
    // A question in progress keeps its partial answer and progress line across re-renders.
    if (pending) {
      if (pending.shown) thread.appendChild(pending.live);
      thread.appendChild(pending.stage.node);
    }
  }

  function fetchReceipt(turn) {
    if (!turn.runId) return Promise.resolve();
    return B.api('/runs/' + encodeURIComponent(turn.runId), { quiet: true }).then(function (data) {
      turn.receipt = receiptFrom(data); save(); render();
    }, function (err) {
      // A pipeline error answers with a run id that has no record: say so, keep no link.
      if (err && err.status === 404) { turn.runId = null; save(); render(); }
    });
  }

  // ---------- stage progress ----------
  var STAGE_WORDS = { plan: 'Planning the steps', re_plan: 'Reading the results', compose: 'Writing the answer',
    route: 'Choosing a model for a step', rule_zero_shortcut: 'Answering in one call' };
  function stageLine() {
    var label = h('span', {}, 'Starting...');
    var clock = h('span', { class: 'num' }, '0:00');
    var node = h('div', { class: 'stage' }, [h('span', { class: 'spin', 'aria-hidden': 'true' }), label, clock]);
    var t0 = Date.now();
    var tick = setInterval(function () { clock.textContent = W.clock((Date.now() - t0) / 1000); }, 1000);
    return {
      node: node,
      set: function (stage, detail) {
        var words = STAGE_WORDS[stage] || String(stage || 'Working').replace(/_/g, ' ');
        label.textContent = words + (detail && stage === 'route' ? ': ' + W.shortText(detail, 80) : '');
      },
      stop: function () { clearInterval(tick); }
    };
  }

  // ---------- send ----------
  function sendQuestion() {
    var text = q.value.trim();
    if (!text || send.disabled) return;
    send.disabled = true;
    // The lane mode is read again right now: it may have changed since the page opened.
    // An unknown or unreadable mode counts as all_local (nothing all_local is resent).
    W.prepareSend(B.api, turns).then(function (p) {
      laneMode = p.mode; showLane();
      doSend(text, p);
    });
  }
  function doSend(text, prep) {
    var tag = prep.tag;
    var folder = A.$('docs-folder').value.trim();
    var body = { model: A.$('alias').value, stream: true, messages: prep.history.concat([{ role: 'user', content: text }]) };
    if (folder && prep.docs) body.documents = { folder: folder };

    q.value = '';
    var userTurn = { role: 'user', content: text, lane: tag, folder: folder || null };
    turns.push(userTurn); save(); render();
    var stage = stageLine();
    var live = h('article', { class: 'card answer' }, h('div', { class: 'md' }));
    pending = { stage: stage, live: live, shown: false };
    thread.appendChild(stage.node);

    var full = '', runId = null, shown = false;
    function finish(failed) {
      stage.stop();
      pending = null;
      var turn = { role: 'assistant', content: full || '(empty answer)', runId: runId, lane: tag, failed: !!failed };
      if (failed) userTurn.failed = true;
      turns.push(turn); save(); render();
      send.disabled = false;
      return fetchReceipt(turn);
    }

    fetch('/v1/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (res) {
        if (!res.ok || !res.body) {
          return res.json().catch(function () { return null; }).then(function (err) {
            var d = err && err.detail;
            full = 'The request was refused: ' + (d && typeof d === 'object' ? (d.message || d.error) : (d || ('HTTP ' + res.status))) + (d && d.error ? ' (' + d.error + ')' : '');
            return finish(true);
          });
        }
        var reader = res.body.getReader(), decoder = new TextDecoder(), buf = '';
        function pump() {
          return reader.read().then(function (r) {
            if (r.done) return finish(false);
            buf += decoder.decode(r.value, { stream: true });
            var lines = buf.split('\n');
            buf = lines.pop();
            lines.forEach(function (line) {
              if (line.indexOf('data:') !== 0) return;
              var payload = line.slice(5).trim();
              if (payload === '[DONE]') return;
              var chunk;
              try { chunk = JSON.parse(payload); } catch (_) { return; }
              if (chunk.event === 'stage') { stage.set(chunk.stage, chunk.detail); return; }
              if (!runId && typeof chunk.id === 'string' && chunk.id.indexOf('chatcmpl-') === 0) runId = chunk.id.slice(9);
              var delta = chunk.choices && chunk.choices[0] && chunk.choices[0].delta && chunk.choices[0].delta.content;
              if (typeof delta === 'string' && delta) {
                if (!shown) { thread.insertBefore(live, stage.node); shown = pending.shown = true; stage.set('compose'); }
                full += delta;
                live.firstChild.innerHTML = B.markdown(full); // sanitised by DOMPurify
              }
            });
            return pump();
          });
        }
        return pump();
      })
      .catch(function () {
        full = full || 'Could not reach the server. Check that BORG_ROUTE is running, then ask again.';
        return finish(!runId);
      });
  }

  send.addEventListener('click', sendQuestion);
  q.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendQuestion(); }
  });
  A.$('clear').addEventListener('click', function () { turns = []; save(); render(); });

  // ---------- start ----------
  turns = load();
  render();
  Promise.all([
    W.readLaneMode(B.api),
    B.api('/models/matrix', { quiet: true }).catch(function () { return null; })
  ]).then(function (r) {
    laneMode = r[0];
    (r[1] || []).forEach(function (m) { modelLocal[m.model_id] = A.isLocalModel(m); });
    showLane();
    render();
    // Refresh receipts that were still loading when the page was left.
    turns.forEach(function (t) { if (t.role === 'assistant' && t.runId && !t.receipt) fetchReceipt(t); });
  });
});
