Borg.ready(function () {
  'use strict';
  var B = window.Borg, W = window.BorgWork, A = window.BorgAdmin, h = A.h;
  var KEY = 'borg_ask_history_v2', MAX_TURNS = 60;
  var thread = A.$('thread'), q = A.$('q'), send = A.$('send');
  var laneMode = null;          // last mode read (null until read); re-read before every send
  var modelLocal = {};          // model_id -> true (this machine) / false (cloud)
  var turns = [];
  var pending = null;           // {stage, live, shown} while a question is running
  var replay = null;            // GET /replay/status while this server only plays recorded runs (work order 4.5)

  // ---------- history (localStorage, every access guarded) ----------
  function load() {
    try {
      var parsed = JSON.parse(B.storage.get('localStorage', KEY) || '[]');
      return Array.isArray(parsed) ? parsed : [];
    } catch (_) { return []; }
  }
  function save() {
    if (replay) return;         // a playback's run id lives only as long as the server keeps it
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
    if (r.estimate) box.appendChild(h('p', { class: 'small muted' }, W.floorNote(r.estimate)));
    // Ruling 26a: an answer no step stands behind says so.
    if (r.noStep) box.appendChild(h('p', { class: 'small no-step' }, r.noStep));
    // Reading (2) under ruling 35: planning stopped early; the answer uses the finished steps.
    if (r.stoppedEarly) box.appendChild(h('p', { class: 'small no-step' }, r.stoppedEarly));
    if (r.plainRows) box.appendChild(h('p', { class: 'small no-step' }, r.plainRows));
    // Ruling 26e: "k of N steps failed" (and "m of n items failed"), only when there are any.
    var fl = W.failedLines(r);
    if (fl.length) box.appendChild(h('p', { class: 'small muted' }, fl.join(' · ')));
    (r.notProduced || []).forEach(function (line) { box.appendChild(h('p', { class: 'small muted' }, line)); });
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
      if (!replay) foot.appendChild(thumbs(turn));
      else foot.appendChild(h('span', { class: 'chip replay' }, 'demo run'));
    }
    if (turn.lane === 'all_local' && laneMode !== 'all_local') {
      foot.appendChild(h('span', { class: 'small muted' }, 'Answered while nothing could leave this machine; not resent now.'));
    }
    card.appendChild(foot);
    return card;
  }
  function render() {
    A.clear(thread);
    if (!turns.length) thread.appendChild(h('p', { class: 'muted small' }, replay
      ? 'Choose a recorded demo run below to watch it play back: its steps, the models chosen, the cost and the answer, as they were recorded. Nothing is sent to any model.'
      : 'Ask for a job. Each answer shows which models did the steps, how long it took and what it cost.'));
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
  function stageLine() {
    var label = h('span', {}, 'Starting...');
    var clock = h('span', { class: 'num' }, '0:00');
    var node = h('div', { class: 'stage' }, [h('span', { class: 'spin', 'aria-hidden': 'true' }), label, clock]);
    var t0 = Date.now();
    var tick = setInterval(function () { clock.textContent = W.clock((Date.now() - t0) / 1000); }, 1000);
    return {
      node: node,
      set: function (stage, detail, recordedMs) {
        var words = W.stageWords(stage);
        // Replay (planner, under ruling 38 (c)): a shortened wait shows the time it took when recorded.
        var rec = typeof recordedMs === 'number' && recordedMs >= 0 ? ' (recorded: ' + B.duration(recordedMs / 1000) + ')' : '';
        label.textContent = words + (detail && stage === 'route' ? ': ' + W.shortText(detail, 80) : '') + rec;
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
              if (chunk.event === 'stage') { stage.set(chunk.stage, chunk.detail, chunk.recorded_ms); return; }
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

  // ---------- replay mode (work order 4.5): recorded prompts as buttons, no free text ----------
  function enterReplay(st) {
    replay = st;
    turns = [];
    ['q', 'docs-btn', 'docs-box', 'lane-line', 'alias', 'send'].forEach(function (id) { A.$(id).classList.add('hidden'); });
    var composer = q.parentNode;
    Array.prototype.forEach.call(composer.querySelectorAll('label'), function (l) { l.classList.add('hidden'); });
    var box = h('div', { class: 'stack', id: 'replay-runs' }, h('p', { class: 'small muted', style: 'margin:0' },
      'Recorded demo runs. Choose one to play it back; it backs no claim.'));
    var runs = Array.isArray(st.runs) ? st.runs : [];
    if (!runs.length) box.appendChild(h('p', { class: 'small muted' }, 'No recorded run was found on this server.'));
    runs.forEach(function (r) {
      var name = (r.job ? 'Job ' + r.job + ': ' : '') + (r.title || 'recorded run');
      var btn = h('button', { class: 'btn', type: 'button', title: W.shortText(r.prompt, 300) },
        'Play ' + name + ' (' + (r.label || 'demo run') + ', ' + W.clock(r.seconds) + ' as recorded' +
        (r.play_seconds && r.play_seconds < r.seconds - 0.5 ? ', plays in ' + W.clock(r.play_seconds) : '') + ')');
      btn.addEventListener('click', function () {
        if (send.disabled) return;
        send.disabled = true;
        var tag = W.LANE_MODES.indexOf(r.lane_mode) >= 0 ? r.lane_mode : 'off';
        doSend(r.prompt, { tag: tag, history: [], docs: false });
      });
      box.appendChild(btn);
    });
    composer.insertBefore(box, composer.firstChild);
    render();
    return B.api('/models/matrix', { quiet: true }).then(function (rows) {
      (rows || []).forEach(function (m) { modelLocal[m.model_id] = A.isLocalModel(m); });
      render();
    }, function () {});
  }

  // ---------- start ----------
  B.replayStatus().then(function (st) {
    if (st && st.mode === 'replay') return enterReplay(st);
    startLive();
  });
  function startLive() {
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
  }
});
