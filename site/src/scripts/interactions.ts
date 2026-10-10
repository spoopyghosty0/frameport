// Small reactions all over the page (styles in global.css under "everything reacts"). None of them is needed to read
// or use the page; with reduced motion only the ones that don't move stay (count-ups show their number at once).

const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
const fine = matchMedia('(pointer: fine)').matches;

/** A soft portal light under the pointer. */
function cursorLight() {
  if (reduce || !fine) return;
  const light = document.createElement('div');
  light.className = 'cursor-light';
  light.setAttribute('aria-hidden', 'true');
  document.body.append(light);
  let x = innerWidth / 2, y = innerHeight / 2, tx = x, ty = y, raf = 0;
  const step = () => {
    x += (tx - x) * 0.18; y += (ty - y) * 0.18;
    light.style.transform = `translate(${x}px, ${y}px)`;
    if (Math.abs(tx - x) + Math.abs(ty - y) > 0.5) raf = requestAnimationFrame(step); else raf = 0;
  };
  addEventListener('pointermove', (e) => {
    tx = e.clientX; ty = e.clientY;
    light.classList.add('on');
    if (!raf) raf = requestAnimationFrame(step);
  }, { passive: true });
  document.addEventListener('pointerleave', () => light.classList.remove('on'));
}

/** Clicking empty space: the app's Play ripple, a blue ring then an orange one. */
function clickRipples() {
  if (reduce) return;
  addEventListener('pointerdown', (e) => {
    const t = e.target as Element;
    if (t.closest('a, button, input, select, textarea, label, summary, [data-mock], [data-crossing], canvas, [role="button"], .row, [tabindex]')) return;
    for (const [i, cls] of ['b', 'o'].entries()) {
      const r = document.createElement('span');
      r.className = `click-ring ${cls}`;
      r.style.left = `${e.clientX}px`; r.style.top = `${e.clientY}px`;
      r.style.animationDelay = `${i * 160}ms`;
      document.body.append(r);
      setTimeout(() => r.remove(), 1000);
    }
  });
}

/** Headings: the portal's colours light the letters under the pointer; words rise in when the heading appears. */
function headings() {
  document.querySelectorAll<HTMLElement>('main h2, .prose h1').forEach((h) => {
    if (!reduce && !h.dataset.split) {
      h.dataset.split = '1';
      let i = 0;
      const wrap = (node: Node) => {
        for (const child of [...node.childNodes]) {
          if (child.nodeType === Node.TEXT_NODE) {
            const frag = document.createDocumentFragment();
            for (const part of (child.textContent ?? '').split(/(\s+)/)) {
              if (!part) continue;
              if (/^\s+$/.test(part)) { frag.append(part); continue; }
              const w = document.createElement('span');
              w.className = 'w';
              w.style.setProperty('--wi', String(i++));
              w.textContent = part;
              frag.append(w);
            }
            child.replaceWith(frag);
          } else if (child.nodeType === Node.ELEMENT_NODE && !(child as Element).matches('a.anchor')) wrap(child);
        }
      };
      wrap(h);
      h.classList.add('words');
    }
    if (!fine) return;
    h.addEventListener('pointermove', (e) => {
      const r = h.getBoundingClientRect();
      h.style.setProperty('--hx', `${e.clientX - r.left}px`);
      h.style.setProperty('--hy', `${e.clientY - r.top}px`);
      h.classList.add('lit');
    });
    h.addEventListener('pointerleave', () => h.classList.remove('lit'));
  });
}

/** Section labels unscramble the first time they show; hover scrambles them again. */
function labels() {
  const GLYPHS = '01<>/\\|=+*#%·-_';
  const scramble = (el: HTMLElement) => {
    const final = el.dataset.text ?? (el.dataset.text = el.textContent ?? '');
    if (reduce) return;
    let frame = 0;
    const total = 18;
    const tick = () => {
      frame++;
      const done = Math.floor((frame / total) * final.length);
      el.textContent = final.split('').map((c, k) => (k < done || c === ' ' ? c : GLYPHS[(k * 7 + frame) % GLYPHS.length])).join('');
      if (frame < total) requestAnimationFrame(tick); else el.textContent = final;
    };
    requestAnimationFrame(tick);
  };
  const io = new IntersectionObserver((es) => es.forEach((e) => {
    if (e.isIntersecting) { e.target.querySelectorAll<HTMLElement>('.scr').forEach(scramble); io.unobserve(e.target); }
  }), { threshold: 0.6 });
  document.querySelectorAll<HTMLElement>('.label').forEach((l) => {
    // wrap the label's own text so the line after it stays put
    for (const n of [...l.childNodes]) {
      if (n.nodeType === Node.TEXT_NODE && n.textContent?.trim()) {
        const s = document.createElement('span'); s.className = 'scr'; s.textContent = n.textContent.trim(); n.replaceWith(' ', s);
      } else if (n.nodeType === Node.ELEMENT_NODE) (n as Element).classList.add('scr');
    }
    io.observe(l);
    l.addEventListener('pointerenter', () => l.querySelectorAll<HTMLElement>('.scr').forEach(scramble));
  });
}

/** Numbers marked data-count count up from zero when they come into view. */
export function countUp(root: ParentNode = document) {
  const io = new IntersectionObserver((es) => es.forEach((e) => {
    if (!e.isIntersecting) return;
    io.unobserve(e.target);
    const el = e.target as HTMLElement;
    const to = Number(el.textContent?.replace(/\D/g, '') || 0);
    if (reduce || !to) return;
    const t0 = performance.now(), dur = 900 + Math.min(900, to * 8);
    const tick = (now: number) => {
      const p = Math.min(1, (now - t0) / dur);
      el.textContent = String(Math.round(to * (1 - Math.pow(1 - p, 3))));
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }), { threshold: 0.8 });
  root.querySelectorAll<HTMLElement>('[data-count]').forEach((el) => io.observe(el));
}

/** Scrolling away from the top flies the hero toward its portal; the hero's own scene leans in. */
function heroScroll() {
  const hero = document.querySelector<HTMLElement>('.hero');
  if (!hero || reduce) return;
  let raf = 0;
  const update = () => {
    raf = 0;
    const p = Math.min(1, Math.max(0, scrollY / Math.max(1, hero.offsetHeight)));
    hero.style.setProperty('--sp', p.toFixed(3));
  };
  addEventListener('scroll', () => { if (!raf) raf = requestAnimationFrame(update); }, { passive: true });
  update();
}

/** The install log completes line by line as you scroll past it (instead of all at once). */
function logScroll() {
  const log = document.querySelector<HTMLElement>('[data-log]');
  if (!log) return;
  const stages = [...log.querySelectorAll<HTMLElement>('.stage')];
  const bar = log.querySelector<HTMLElement>('.log-progress i');
  log.classList.add('scrubbed');
  let raf = 0;
  const update = () => {
    raf = 0;
    const line = innerHeight * 0.78;
    let done = 0;
    stages.forEach((s) => {
      const on = s.getBoundingClientRect().top < line;
      s.classList.toggle('done', on);
      if (on) done++;
    });
    if (bar) bar.style.transform = `scaleX(${done / stages.length})`;
    log.classList.toggle('complete', done === stages.length);
  };
  addEventListener('scroll', () => { if (!raf) raf = requestAnimationFrame(update); }, { passive: true });
  addEventListener('resize', update);
  update();
}

export function initInteractions() {
  cursorLight();
  clickRipples();
  headings();
  labels();
  countUp();
  heroScroll();
  logScroll();
}
