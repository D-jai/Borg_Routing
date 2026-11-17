Borg.ready(function () {
  var A = window.BorgAdmin, h = A.h, B = window.Borg;
  B.api('/skills').then(function (r) {
    var box = A.clear(A.$('skills')), list = r.skills || [];
    if (!list.length) { box.appendChild(h('p', { class: 'muted' }, 'No skills found.')); return; }
    box.appendChild(A.table(['Skill', 'Version', 'Tier', 'What it does', 'Calls', 'Succeeded', 'Avg tokens', 'Avg time'], list.map(function (s) {
      var ok = s.success_rate === null || s.success_rate === undefined ? '-' : Math.round(s.success_rate * 100) + '%';
      return h('tr', {}, [A.td(h('strong', {}, s.skill_id)), A.td('v' + s.version, 'num'), A.td(A.chip(s.recommended_tier || '-')),
        A.td(s.description), A.td(String(s.call_count || 0), 'num'), A.td(ok, 'num'),
        A.td(String(s.avg_tokens || 0), 'num'), A.td(s.avg_latency_ms ? B.duration(s.avg_latency_ms / 1000) : '-', 'num')]);
    })));
  }, function () { A.clear(A.$('skills')).appendChild(h('p', { class: 'muted' }, 'The skills could not be loaded.')); });
});
