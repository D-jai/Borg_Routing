Borg.ready(function () {
  'use strict';
  var B = window.Borg, A = window.BorgAdmin, h = A.h;
  var WORDS = { completed: 'Completed', failed: 'Failed', running: 'Running', pending: 'Waiting' };

  function load() {
    var withBypass = A.$('bypass').checked;
    B.api('/runs?limit=200' + (withBypass ? '&include_bypass=true' : ''), { quiet: true }).then(function (res) {
      var runs = (res && res.runs) || [], c = (res && res.counts) || {};
      A.$('counts').textContent = (c.total || 0) + ' runs · ' + (c.completed || 0) + ' completed · ' + (c.failed || 0) + ' failed' +
        ((c.thumbs_up || c.thumbs_down) ? ' · ' + (c.thumbs_up || 0) + ' 👍 ' + (c.thumbs_down || 0) + ' 👎' : '');
      var box = A.clear(A.$('list'));
      if (!runs.length) { box.appendChild(h('p', { class: 'muted small' }, 'No runs yet. Ask a question on the Ask page.')); return; }
      box.appendChild(A.table(['When', 'Question', 'Status', 'Rating', ''], runs.map(function (r) {
        var kind = r.kind === 'bypass' ? A.chip('direct', '') : null;
        var st = A.statusChip(r.status);
        st.textContent = WORDS[r.status] || r.status || '-';
        var rating = r.user_rating === 1 ? '👍' : r.user_rating === -1 ? '👎' : '';
        return h('tr', {}, [
          A.td(B.dateTime(r.created_at), 'num'),
          A.td(h('div', { class: 'clip', title: r.user_instruction || '' }, [kind, kind ? ' ' : null, A.short(r.user_instruction, 100)])),
          A.td(st), A.td(rating),
          A.td(h('a', { href: 'run.html?id=' + encodeURIComponent(r.run_id) }, 'Open'))
        ]);
      })));
    }, function (err) {
      A.clear(A.$('list')).appendChild(h('p', { class: 'muted small' }, 'Could not load the runs: ' + A.errorMessage(err)));
    });
  }
  A.$('bypass').addEventListener('change', load);
  A.$('reload').addEventListener('click', load);
  load();
});
