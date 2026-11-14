/* Ghost mark: glowing dots drift in from a soft cloud and settle into the Ghost logo
   (two orbits + nucleus), then breathe gently. Usage: ghostMark(canvasElement, sizeInPx) */
window.ghostMark = function (cv, size) {
  size = size || 120;
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var dpr = Math.min(2, window.devicePixelRatio || 1), W = size * 1.6;
  cv.width = W * dpr; cv.height = W * dpr; cv.style.width = cv.style.height = W + 'px';
  var x = cv.getContext('2d'); x.scale(dpr, dpr);
  var c = W / 2, R = size * 0.404, r = size * 0.2, pts = [], TAU = Math.PI * 2;
  function orbit(deg, n, a, big) {
    var rot = deg * Math.PI / 180, cs = Math.cos(rot), sn = Math.sin(rot);
    for (var i = 0; i < n; i++) {
      var t = i / n * TAU, ex = R * Math.cos(t), ey = r * Math.sin(t);
      pts.push({ tx: c + ex * cs - ey * sn, ty: c + ex * sn + ey * cs, a: a, s: big ? 1.15 : 0.95 });
    }
  }
  orbit(-32, 96, 1, true);
  orbit(38, 74, 0.42, false);
  var nx = c + size * 0.1, ny = c - size * 0.062, nr = size * 0.081;
  for (var k = 0; k < 46; k++) {
    var an = Math.random() * TAU, d = Math.sqrt(Math.random()) * nr;
    pts.push({ tx: nx + Math.cos(an) * d, ty: ny + Math.sin(an) * d, a: 1, s: 1.25, nuc: 1 });
  }
  pts.forEach(function (p) {
    var an = Math.random() * TAU, d = size * (0.55 + Math.random() * 0.35);
    p.sx = c + Math.cos(an) * d; p.sy = c + Math.sin(an) * d;
    p.delay = Math.random() * 1.1 + (p.nuc ? 0.9 : 0);
    p.ph = Math.random() * TAU; p.sp = 0.6 + Math.random() * 0.8;
  });
  var t0 = performance.now(), alive = true;
  function ease(k) { return 1 - Math.pow(1 - k, 3); }
  function frame(now) {
    if (!alive || !cv.isConnected) return;
    var t = reduce ? 99 : (now - t0) / 1000;
    x.clearRect(0, 0, W, W);
    // soft halo that grows as the logo forms
    var form = Math.min(1, Math.max(0, (t - 0.6) / 2.2));
    var breathe = 0.5 + 0.5 * Math.sin(t * 1.3);
    var g = x.createRadialGradient(c, c, 0, c, c, size * 0.75);
    g.addColorStop(0, 'rgba(170,210,255,' + (0.10 + 0.07 * breathe) * form + ')');
    g.addColorStop(1, 'rgba(170,210,255,0)');
    x.fillStyle = g; x.fillRect(0, 0, W, W);
    x.shadowColor = 'rgba(205,230,255,.9)';
    for (var i = 0; i < pts.length; i++) {
      var p = pts[i];
      var k = Math.min(1, Math.max(0, (t - p.delay) / 2.0)); k = ease(k);
      var drift = (1 - k) * size * 0.05, wob = size * 0.006 * Math.sin(t * p.sp + p.ph) * (0.4 + 0.6 * breathe);
      var px = p.sx + (p.tx - p.sx) * k + Math.cos(t * 0.7 + p.ph) * drift + wob;
      var py = p.sy + (p.ty - p.sy) * k + Math.sin(t * 0.9 + p.ph) * drift + wob * 0.6;
      var al = p.a * (0.25 + 0.75 * k) * (0.85 + 0.15 * Math.sin(t * 2 + p.ph));
      x.shadowBlur = 6 + 6 * k * breathe;
      x.fillStyle = 'rgba(240,246,255,' + al + ')';
      x.beginPath(); x.arc(px, py, p.s * (0.8 + 0.4 * k), 0, TAU); x.fill();
    }
    x.shadowBlur = 0;
    if (!reduce) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  return { stop: function () { alive = false; } };
};
