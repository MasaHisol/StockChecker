/* 部材トラッカー 共通スクリプト: CSRF / 一括取得の進捗 / グラフ / 一覧の選択 */
(function () {
  var TOKEN = document.querySelector('meta[name=csrf]');
  TOKEN = TOKEN ? TOKEN.content : '';
  // すべてのフォーム送信に CSRF トークンを付ける
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if ((f.method || '').toLowerCase() === 'post' && !f.querySelector('input[name=_csrf]')) {
      var i = document.createElement('input'); i.type = 'hidden'; i.name = '_csrf'; i.value = TOKEN; f.appendChild(i);
    }
    var b = f.querySelector('button[data-busy]');
    if (b) { setTimeout(function () { b.disabled = true; b.textContent = b.dataset.busy; }, 0); }
  }, true);

  // ---- 通知 (トースト): 成功は数秒で消える・エラーは閉じるまで表示
  document.querySelectorAll('.toast').forEach(function (el, i) {
    var close = function () { el.classList.add('out'); setTimeout(function () { el.remove(); }, 260); };
    el.querySelector('.toast-x').addEventListener('click', close);
    if (!el.classList.contains('error')) setTimeout(close, 5000 + i * 800);
  });

  // ---- 「/」キーで検索へ
  var gs = document.getElementById('global-search');
  document.addEventListener('keydown', function (e) {
    var tag = (document.activeElement || {}).tagName;
    if (e.key === '/' && gs && !/INPUT|TEXTAREA|SELECT/.test(tag)) { e.preventDefault(); gs.focus(); gs.select(); }
  });

  // ---- ホーム: やることの絞り込み
  var chips = document.querySelector('[data-chips]'), todo = document.querySelector('[data-todo]');
  var setChip = function (f) {
    if (!chips || !todo) return;
    chips.querySelectorAll('.chip').forEach(function (c) { c.classList.toggle('on', c.dataset.f === f); });
    todo.querySelectorAll('li[data-type]').forEach(function (li) { li.style.display = (f === 'all' || li.dataset.type === f) ? '' : 'none'; });
  };
  if (chips) chips.addEventListener('click', function (e) { var c = e.target.closest('.chip'); if (c) setChip(c.dataset.f); });
  document.querySelectorAll('[data-jump]').forEach(function (a) { a.addEventListener('click', function () { setChip(a.dataset.jump); }); });

  // ---- 一括取得の進捗 (実行中はページ上部に表示し、数秒ごとに更新)
  var bar = document.getElementById('jobbar');
  if (bar) {
    var id = bar.dataset.job;
    var tick = function () {
      fetch('/api/fetch/' + id, {credentials: 'same-origin'}).then(function (r) { return r.json(); }).then(function (j) {
        var pct = j.total ? Math.round(j.done / j.total * 100) : 0;
        bar.querySelector('.progress i').style.width = pct + '%';
        bar.querySelector('[data-f=count]').textContent = j.done + ' / ' + j.total + ' 件';
        bar.querySelector('[data-f=current]').textContent = j.current ? '取得中: ' + j.current : '';
        bar.querySelector('[data-f=stat]').textContent = '成功 ' + j.ok + ' ・ 失敗 ' + j.failed + ' ・ 対象外 ' + j.skipped;
        if (j.status === 'running' || j.status === 'cancelling') { setTimeout(tick, 2500); }
        else { location.href = '/fetch/' + id; }
      }).catch(function () { setTimeout(tick, 5000); });
    };
    setTimeout(tick, 1500);
  }

  // ---- 一覧: チェックボックスでまとめて操作
  document.querySelectorAll('[data-bulk]').forEach(function (form) {
    var all = form.querySelector('input[data-all]');
    var boxes = function () { return form.querySelectorAll('input[name=ids]'); };
    var bb = form.querySelector('.bulkbar');
    var sync = function () {
      var n = [].filter.call(boxes(), function (b) { return b.checked; }).length;
      if (bb) { bb.classList.toggle('on', n > 0); bb.querySelector('[data-f=n]').textContent = n; }
    };
    if (all) all.addEventListener('change', function () {
      boxes().forEach(function (b) { if (b.closest('tr').style.display !== 'none') b.checked = all.checked; }); sync();
    });
    form.addEventListener('change', function (e) { if (e.target.name === 'ids') sync(); });
  });

  // ---- 一覧の絞り込み (状態・担当・方式・キーワード)
  var tbl = document.querySelector('[data-filter-table]');
  if (tbl) {
    var rows = [].slice.call(tbl.querySelectorAll('tbody tr'));
    var st = {status: 'all', q: '', owner: '', method: ''};
    var apply = function () {
      var n = 0;
      rows.forEach(function (r) {
        var ok = (st.status === 'all' || r.dataset.status === st.status) && (!st.q || r.dataset.text.indexOf(st.q) >= 0)
          && (!st.owner || r.dataset.owner === st.owner) && (!st.method || r.dataset.method === st.method);
        r.style.display = ok ? '' : 'none'; if (ok) n++;
      });
      var nh = document.getElementById('nohit'); if (nh) nh.style.display = (rows.length && !n) ? '' : 'none';
      document.querySelectorAll('[data-seg] [data-f]').forEach(function (b) { b.classList.toggle('on', b.dataset.f === st.status); });
    };
    document.querySelectorAll('[data-seg] [data-f]').forEach(function (b) { b.addEventListener('click', function (e) { e.preventDefault(); st.status = b.dataset.f; apply(); }); });
    var q = document.getElementById('q'); if (q) q.addEventListener('input', function () { st.q = q.value.toLowerCase().trim(); apply(); });
    ['owner', 'method'].forEach(function (k) { var s = document.getElementById('f-' + k); if (s) s.addEventListener('change', function () { st[k] = s.value; apply(); }); });
    var params = new URLSearchParams(location.search);
    var init = params.get('status'); if (init) { st.status = init; }
    var iq = params.get('q'); if (iq && q) { q.value = iq; st.q = iq.toLowerCase().trim(); }
    if (init || iq) apply();
  }

  // ---- グラフ (1 系列の折れ線。十字線とツールチップ付き)
  function fmt(v, unit) {
    if (unit === '円') return '¥' + (v < 100 ? (+v.toFixed(2)).toLocaleString() : Math.round(v).toLocaleString());
    return Math.round(v).toLocaleString() + ' ' + unit;
  }
  function lineChart(el) {
    var pts = JSON.parse(el.dataset.points || '[]'), unit = el.dataset.unit || '', color = el.dataset.color || 'var(--series-1)';
    var step = el.dataset.step === '1';
    var svg = el.querySelector('svg'), tip = el.querySelector('.tip');
    if (pts.length === 0) { return; }
    var compact = el.dataset.compact === '1';
    var W = compact ? 300 : 640, H = +(el.dataset.h || (compact ? 84 : 220)), L = compact ? 6 : 64, R = compact ? 6 : 14, T = compact ? 8 : 12, B = compact ? 8 : 28;
    var ts = pts.map(function (p) { return new Date(p.t.replace(' ', 'T')).getTime(); });
    var t0 = Math.min.apply(null, ts), t1 = Math.max.apply(null, ts); if (t1 === t0) { t0 -= 864e5; t1 += 864e5; }
    var vs = pts.map(function (p) { return p.v; });
    var lo = Math.min.apply(null, vs), hi = Math.max.apply(null, vs), pad = (hi - lo) * .15 || Math.abs(hi) * .08 || 1;
    lo = Math.max(0, lo - pad); hi += pad;
    var x = function (t) { return L + (t - t0) / (t1 - t0) * (W - L - R); }, y = function (v) { return T + (hi - v) / (hi - lo) * (H - T - B); };
    var h = '';
    if (!compact) for (var k = 0; k <= 4; k++) { var v = lo + (hi - lo) * k / 4, yy = y(v);
      h += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + yy + '" y2="' + yy + '" stroke="var(--line)" stroke-width="1"/>' +
        '<text x="' + (L - 8) + '" y="' + (yy + 4) + '" text-anchor="end" font-size="11" fill="var(--muted)">' + fmt(v, unit) + '</text>'; }
    var d0 = new Date(t0), d1 = new Date(t1), ds = function (d) { return (d.getMonth() + 1) + '/' + d.getDate(); };
    if (d0.getFullYear() !== d1.getFullYear()) ds = function (d) { return d.getFullYear() + '/' + (d.getMonth() + 1) + '/' + d.getDate(); };
    if (!compact) for (var k2 = 0; k2 <= 4; k2++) { var tt = t0 + (t1 - t0) * k2 / 4;
      h += '<text x="' + x(tt) + '" y="' + (H - 8) + '" font-size="11" text-anchor="' + (k2 === 0 ? 'start' : k2 === 4 ? 'end' : 'middle') + '" fill="var(--muted)">' + ds(new Date(tt)) + '</text>'; }
    var path = '';
    pts.forEach(function (p, i) { var px = x(ts[i]), py = y(p.v);
      if (i === 0) path += 'M' + px + ',' + py; else if (step) path += 'H' + px + 'V' + py; else path += 'L' + px + ',' + py; });
    h += '<path d="' + path + '" fill="none" stroke="' + color + '" stroke-width="2" stroke-linejoin="round"/>';
    if (compact) h = '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + (H - B) + '" y2="' + (H - B) + '" stroke="var(--line)"/>' + h;
    pts.forEach(function (p, i) { if (!compact || i === pts.length - 1 || pts.length <= 12) h += '<circle cx="' + x(ts[i]) + '" cy="' + y(p.v) + '" r="' + (compact ? 3 : 4) + '" fill="' + color + '" stroke="var(--surface)" stroke-width="2"/>'; });
    h += '<line class="xh" y1="' + T + '" y2="' + (H - B) + '" stroke="var(--muted)" stroke-dasharray="3 3" visibility="hidden"/>';
    h += '<rect x="' + L + '" y="0" width="' + (W - L - R) + '" height="' + H + '" fill="transparent" class="hit"/>';
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H); svg.innerHTML = h;
    var xh = svg.querySelector('.xh');
    var move = function (ev) {
      var r = svg.getBoundingClientRect(), sx = (ev.clientX - r.left) * W / r.width, best = 0, bd = 1e18;
      ts.forEach(function (t, i) { var dd = Math.abs(x(t) - sx); if (dd < bd) { bd = dd; best = i; } });
      var p = pts[best], px = x(ts[best]);
      xh.setAttribute('x1', px); xh.setAttribute('x2', px); xh.setAttribute('visibility', 'visible');
      tip.style.display = 'block'; tip.innerHTML = '<b>' + fmt(p.v, unit) + '</b><br>' + p.t.slice(0, 10) + (p.s ? '<br>' + p.s : '');
      var lx = px * r.width / W; tip.style.left = lx + 'px'; tip.style.top = (y(p.v) * r.height / H) + 'px';
      tip.style.transform = 'translate(' + (lx > r.width * .7 ? '-105%' : lx < r.width * .3 ? '5%' : '-50%') + ',-115%)';
    };
    svg.addEventListener('mousemove', move);
    svg.addEventListener('mouseleave', function () { tip.style.display = 'none'; xh.setAttribute('visibility', 'hidden'); });
  }
  document.querySelectorAll('.chart[data-points]').forEach(lineChart);

  // ---- スパークライン (一覧・カード用の小さな推移。ホバーで値を表示)
  document.querySelectorAll('svg.spark[data-points]').forEach(function (svg) {
    var pts = JSON.parse(svg.dataset.points || '[]'); if (pts.length < 2) { return; }
    var W = 110, H = 30, vs = pts.map(function (p) { return p.v; }), lo = Math.min.apply(null, vs), hi = Math.max.apply(null, vs);
    if (hi === lo) { hi += 1; lo -= 1; }
    var x = function (i) { return 3 + i * (W - 6) / (pts.length - 1); }, y = function (v) { return 4 + (hi - v) / (hi - lo) * (H - 8); };
    var d = pts.map(function (p, i) { return (i ? 'L' : 'M') + x(i) + ',' + y(p.v); }).join('');
    var last = pts[pts.length - 1];
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.innerHTML = '<path d="' + d + '" fill="none" stroke="var(--series-1)" stroke-width="1.5" stroke-linejoin="round"/>' +
      '<circle cx="' + x(pts.length - 1) + '" cy="' + y(last.v) + '" r="2.5" fill="var(--series-1)"/>' +
      '<title>' + pts.map(function (p) { return p.t.slice(0, 10) + ' ' + fmt(p.v, '円'); }).join('\n') + '</title>';
  });
})();
