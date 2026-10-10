// The download page's backdrop: streaks of light rushing out of the portal, blue on the PC's side (left) and orange
// on the Frame's (right). speed() sets how fast (eased), burst() is the moment the package goes through. Draws only
// while the tab is shown; one still frame under reduced motion.

export interface Warp { speed(v: number): void; burst(): void }

export function startWarp(canvas: HTMLCanvasElement, opts: { still: boolean }): Warp {
  const g = canvas.getContext('2d');
  if (!g) return { speed() {}, burst() {} };
  const N = matchMedia('(max-width: 760px)').matches ? 140 : 260;
  // each streak: angle, distance from the centre (0..1 of the half-diagonal), its own pace
  const stars = Array.from({ length: N }, () => ({ a: Math.random() * Math.PI * 2, d: Math.random(), v: 0.4 + Math.random() * 0.9 }));
  let w = 0, h = 0, dpr = 1, target = 1, cur = 1, boost = 0, last = performance.now(), raf = 0;

  const size = () => {
    dpr = Math.min(devicePixelRatio || 1, 2);
    w = canvas.clientWidth; h = canvas.clientHeight;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  };
  const draw = (dt: number) => {
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2, R = Math.hypot(cx, cy);
    cur += (target + boost - cur) * Math.min(1, dt * 3);
    boost *= Math.pow(0.25, dt);
    g.lineCap = 'round';
    for (const s of stars) {
      s.d += dt * 0.08 * s.v * cur * (0.3 + s.d);
      if (s.d > 1) { s.d = 0.02 + Math.random() * 0.05; s.a = Math.random() * Math.PI * 2; }
      const r1 = s.d * R, len = Math.min(R * 0.25, 4 + s.d * s.d * 60 * cur);
      const cos = Math.cos(s.a), sin = Math.sin(s.a);
      const x1 = cx + cos * r1, y1 = cy + sin * r1 * 0.8;
      const x0 = cx + cos * Math.max(0, r1 - len), y0 = cy + sin * Math.max(0, r1 - len) * 0.8;
      const alpha = Math.min(1, s.d * 2.2) * (1 - s.d * 0.6) * 0.8;
      g.strokeStyle = cos < 0 ? `rgba(58,168,255,${alpha})` : `rgba(255,138,31,${alpha})`;
      g.lineWidth = 0.6 + s.d * 1.8;
      g.beginPath(); g.moveTo(x0, y0); g.lineTo(x1, y1); g.stroke();
    }
  };
  const frame = (now: number) => {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    draw(dt);
    raf = requestAnimationFrame(frame);
  };
  size();
  addEventListener('resize', () => { size(); if (opts.still) draw(0); });
  if (opts.still) {
    for (const s of stars) s.d = Math.random();
    draw(0);
  } else {
    raf = requestAnimationFrame(frame);
    document.addEventListener('visibilitychange', () => {
      cancelAnimationFrame(raf);
      if (!document.hidden) { last = performance.now(); raf = requestAnimationFrame(frame); }
    });
  }
  return {
    speed(v) { target = v; },
    burst() { boost = 9; },
  };
}
