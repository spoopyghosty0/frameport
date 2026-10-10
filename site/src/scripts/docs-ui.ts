// What makes the docs pages more than text: search across every page, definitions on hover, tables you can filter
// and sort, step lists you can tick off (remembered), the tutorial video in place, pictures that zoom, section links
// that copy themselves, keys for search and paging, and on the overview a reading path for what you came to do.
// Everything here only adds to the rendered markdown: without it the pages read the same.
import { GLOSSARY } from './glossary';

interface Section { slug: string; doc: string; id: string; heading: string; text: string }

const shell = document.querySelector<HTMLElement>('[data-docs]');
const BASE = shell?.dataset.docs ?? '/docs/';
const SLUG = shell?.dataset.slug ?? '';
const prose = document.querySelector<HTMLElement>('.dmain .prose');
const coarse = matchMedia('(pointer: coarse)').matches;

// ------------------------------------------------------------------ small helpers
const KEY = 'frameport.docs';
function store<T>(name: string, fallback: T): T {
  try { return (JSON.parse(localStorage.getItem(`${KEY}.${name}`) || 'null') as T) ?? fallback; } catch { return fallback; }
}
function save(name: string, value: unknown) {
  try { localStorage.setItem(`${KEY}.${name}`, JSON.stringify(value)); } catch { /* private mode */ }
}
const esc = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const reEsc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
/** Matches that start a word ("cable" finds "cable" and "cables", not "applicable"). */
const startRe = (words: string[], flags: string) => new RegExp(`(?<![\\p{L}\\p{N}])(${words.map(reEsc).join('|')})`, `${flags}u`);
const wordHits = (hay: string, w: string) => hay.match(startRe([w], 'gi'))?.length ?? 0;
const wordsOf = (q: string) => [...new Set(q.toLowerCase().split(/\s+/).filter((w) => w.length > 1))];

let toastTimer = 0;
function toast(text: string) {
  let t = document.querySelector<HTMLElement>('.dtoast');
  if (!t) { t = document.createElement('div'); t.className = 'dtoast'; t.setAttribute('role', 'status'); document.body.append(t); }
  t.textContent = text;
  t.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => t!.classList.remove('on'), 1800);
}

/** Wraps every match of `words` in text under `root` in <mark class="hl">; returns the marks. */
function highlight(root: HTMLElement, words: string[]): HTMLElement[] {
  if (!words.length) return [];
  const re = startRe(words, 'gi');
  const has = startRe(words, 'i');   // no lastIndex state
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement?.closest('script, style, mark, button, .anchor, .tbl-tools') ? NodeFilter.FILTER_REJECT
      : has.test(n.nodeValue ?? '') ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
  });
  const nodes: Text[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n as Text);
  const marks: HTMLElement[] = [];
  for (const node of nodes) {
    const frag = document.createDocumentFragment();
    for (const [i, part] of (node.nodeValue ?? '').split(re).entries()) {
      if (!part) continue;
      if (i % 2) { const m = document.createElement('mark'); m.className = 'hl'; m.textContent = part; frag.append(m); marks.push(m); } else frag.append(part);
    }
    node.replaceWith(frag);
  }
  return marks;
}
function unhighlight(root: HTMLElement) {
  root.querySelectorAll('mark.hl').forEach((m) => m.replaceWith(m.textContent ?? ''));
  root.normalize();
}

// ------------------------------------------------------------------ search across every page
let index: Promise<Section[]> | null = null;
const loadIndex = () => (index ??= fetch(`${BASE}search.json`).then((r) => r.json() as Promise<Section[]>).catch(() => { index = null; return []; }));

function search(all: Section[], q: string) {
  const words = wordsOf(q);
  if (!words.length) return [];
  const hits: { s: Section; score: number }[] = [];
  for (const s of all) {
    const head = s.heading.toLowerCase(), body = s.text.toLowerCase(), doc = s.doc.toLowerCase();
    let score = 0;
    for (const w of words) {
      const inHead = wordHits(head, w) > 0, inDoc = wordHits(doc, w) > 0, n = wordHits(body, w);
      if (!inHead && !n && !inDoc) { score = 0; break; }
      score += (inHead ? 8 : 0) + (inDoc ? 2 : 0) + Math.min(n, 6);
    }
    if (score) hits.push({ s, score: score + (head.includes(q.toLowerCase().trim()) ? 10 : 0) });
  }
  return hits.sort((a, b) => b.score - a.score).slice(0, 12).map((h) => h.s);
}

function snippet(text: string, words: string[]) {
  const low = text.toLowerCase();
  const at = Math.max(0, Math.min(...words.map((w) => { const i = low.search(startRe([w], 'i')); return i < 0 ? Infinity : i; })));
  const from = Number.isFinite(at) ? Math.max(0, at - 50) : 0;
  let s = text.slice(from, from + 170);
  if (from > 0) s = '…' + s.replace(/^\S*\s/, '');
  if (from + 170 < text.length) s = s.replace(/\s\S*$/, '') + '…';
  const re = startRe(words, 'gi');
  return esc(s).replace(re, '<mark>$1</mark>');
}

function initSearch(box: HTMLElement) {
  const input = box.querySelector<HTMLInputElement>('input')!;
  const panel = box.querySelector<HTMLElement>('[data-results]')!;
  let active = -1, results: Section[] = [], seq = 0;
  const open = (on: boolean) => { panel.hidden = !on; input.setAttribute('aria-expanded', String(on)); };
  const pick = (i: number) => {
    const items = panel.querySelectorAll<HTMLElement>('a');
    active = Math.max(-1, Math.min(items.length - 1, i));
    items.forEach((a, j) => a.classList.toggle('on', j === active));
    items[active]?.scrollIntoView({ block: 'nearest' });
  };
  const run = async () => {
    const q = input.value.trim(), me = ++seq;
    if (!q) { open(false); return; }
    const all = await loadIndex();
    if (me !== seq) return;
    results = search(all, q);
    const words = wordsOf(q);
    panel.innerHTML = results.length
      ? `<p class="sr-n">${results.length === 12 ? 'Best 12 matches' : `${results.length} match${results.length === 1 ? '' : 'es'}`}</p>`
        + results.map((s) => `<a href="${BASE}${s.slug}/?q=${encodeURIComponent(q)}${s.id ? `#${s.id}` : ''}" role="option">`
          + `<small>${esc(s.doc)}${s.slug === SLUG ? ' · this page' : ''}</small><b>${snippet(s.heading, words).replace(/…/g, '')}</b>`
          + `<span>${snippet(s.text, words)}</span></a>`).join('')
      : all.length ? '<p class="sr-none">Nothing matches every word. Try fewer or shorter words.</p>' : '<p class="sr-none">Search needs a connection to load.</p>';
    open(true);
    pick(results.length ? 0 : -1);
  };
  input.addEventListener('focus', () => { loadIndex(); if (input.value.trim()) run(); });
  input.addEventListener('input', run);
  input.addEventListener('keydown', (e) => {
    const items = panel.querySelectorAll<HTMLAnchorElement>('a');
    if (e.key === 'ArrowDown') { e.preventDefault(); pick(active + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); pick(active - 1); }
    else if (e.key === 'Enter' && items[active]) { e.preventDefault(); items[active].click(); }
    else if (e.key === 'Escape') { input.value ? (input.value = '', open(false)) : input.blur(); }
  });
  panel.addEventListener('mousemove', (e) => {
    const a = (e.target as HTMLElement).closest('a');
    if (a) pick([...panel.querySelectorAll('a')].indexOf(a));
  });
  document.addEventListener('pointerdown', (e) => { if (!box.contains(e.target as Node)) open(false); });
}
document.querySelectorAll<HTMLElement>('[data-docsearch]').forEach(initSearch);

// ------------------------------------------------------------------ definitions on hover
let card: HTMLElement | null = null, cardFor: HTMLElement | null = null, hideTimer = 0;
function showTerm(el: HTMLElement) {
  const t = GLOSSARY.find((g) => g.term === el.dataset.term);
  if (!t) return;
  clearTimeout(hideTimer);
  if (!card) {
    card = document.createElement('div');
    card.className = 'term-card';
    card.id = 'term-card';
    card.setAttribute('role', 'tooltip');
    card.addEventListener('mouseenter', () => clearTimeout(hideTimer));
    card.addEventListener('mouseleave', () => hideTerm(250));
    document.body.append(card);
  }
  const [slug, hash] = (t.doc ?? '').split('#');
  card.innerHTML = `<b>${esc(t.term)}</b><p>${esc(t.def)}</p>`
    + (t.doc && slug !== SLUG ? `<a href="${BASE}${slug}/${hash ? `#${hash}` : ''}">More in the docs →</a>` : '');
  cardFor?.removeAttribute('aria-describedby');
  cardFor = el;
  el.setAttribute('aria-describedby', 'term-card');
  const r = el.getBoundingClientRect();
  card.classList.add('on');
  const w = card.offsetWidth, h = card.offsetHeight;
  const left = Math.max(12, Math.min(innerWidth - w - 12, r.left + r.width / 2 - w / 2));
  const below = r.bottom + 10 + h < innerHeight;
  card.style.left = `${left}px`;
  card.style.top = `${below ? r.bottom + 10 : r.top - h - 10}px`;
  card.dataset.side = below ? 'below' : 'above';
}
function hideTerm(delay = 0) {
  clearTimeout(hideTimer);
  hideTimer = window.setTimeout(() => { card?.classList.remove('on'); cardFor?.removeAttribute('aria-describedby'); cardFor = null; }, delay);
}

function addTerms(root: HTMLElement) {
  for (const t of GLOSSARY) {
    const re = new RegExp(`\\b${reEsc(t.term)}\\b`, t.ci ? 'i' : '');
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (!n.parentElement?.closest('p, li, td') || n.parentElement.closest('code, pre, a, h1, h2, h3, h4, .term, mark, button')
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    for (let n = walker.nextNode() as Text | null; n; n = walker.nextNode() as Text | null) {
      const m = re.exec(n.nodeValue ?? '');
      if (!m) continue;
      const word = n.splitText(m.index);
      word.splitText(m[0].length);
      const span = document.createElement('span');
      span.className = 'term';
      span.tabIndex = 0;
      span.dataset.term = t.term;
      span.textContent = m[0];
      word.replaceWith(span);
      break;   // the first use on the page only
    }
  }
  root.addEventListener('mouseover', (e) => { const el = (e.target as HTMLElement).closest<HTMLElement>('.term'); if (el && el !== cardFor) showTerm(el); });
  root.addEventListener('mouseout', (e) => { if ((e.target as HTMLElement).closest('.term')) hideTerm(250); });
  root.addEventListener('focusin', (e) => { const el = (e.target as HTMLElement).closest<HTMLElement>('.term'); if (el) showTerm(el); });
  root.addEventListener('focusout', (e) => { if ((e.target as HTMLElement).closest('.term')) hideTerm(200); });
  root.addEventListener('click', (e) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>('.term');
    if (el) el === cardFor && card?.classList.contains('on') ? hideTerm() : showTerm(el);
  });
  addEventListener('scroll', () => hideTerm(), { passive: true });
}

// ------------------------------------------------------------------ tables: filter and sort
function initTable(wrap: HTMLElement) {
  const table = wrap.querySelector('table');
  const body = table?.tBodies[0];
  if (!table || !body) return;
  const rows = [...body.rows];
  const original = rows.slice();
  if (rows.length >= 6) {
    const tools = document.createElement('div');
    tools.className = 'tbl-tools';
    tools.innerHTML = `<label><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></svg>`
      + `<input type="search" placeholder="Filter ${rows.length} rows…" aria-label="Filter this table" /></label><span aria-live="polite"></span>`;
    wrap.before(tools);
    const input = tools.querySelector('input')!, count = tools.querySelector('span')!;
    const texts = rows.map((r) => r.textContent!.toLowerCase());
    input.addEventListener('input', () => {
      const words = wordsOf(input.value);
      unhighlight(body);
      let shown = 0;
      rows.forEach((r, i) => { const ok = words.every((w) => wordHits(texts[i], w) > 0); r.hidden = !ok; if (ok) shown++; });
      if (words.length) highlight(body, words);
      count.textContent = words.length ? `${shown} of ${rows.length}` : '';
      wrap.classList.toggle('empty', words.length > 0 && !shown);
    });
    input.addEventListener('keydown', (e) => { if (e.key === 'Escape' && input.value) { input.value = ''; input.dispatchEvent(new Event('input')); } });
  }
  if (rows.length < 4) return;
  const heads = [...table.tHead?.rows[0]?.cells ?? []];
  heads.forEach((th, col) => {
    th.tabIndex = 0;
    th.classList.add('sortable');
    th.title = 'Sort by this column';
    const sort = () => {
      const state = th.getAttribute('aria-sort');
      const next = state === 'ascending' ? 'descending' : state === 'descending' ? 'none' : 'ascending';
      heads.forEach((h) => h.removeAttribute('aria-sort'));
      if (next !== 'none') th.setAttribute('aria-sort', next);
      const ordered = next === 'none' ? original : rows.slice().sort((a, b) => {
        const c = a.cells[col]?.textContent!.trim().localeCompare(b.cells[col]?.textContent!.trim() ?? '', 'en', { numeric: true, sensitivity: 'base' }) ?? 0;
        return next === 'ascending' ? c : -c;
      });
      body.append(...ordered);
    };
    th.addEventListener('click', sort);
    th.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); } });
  });
}

// ------------------------------------------------------------------ step lists you can tick off
function initSteps(root: HTMLElement) {
  const all = store<Record<string, boolean[][]>>('steps', {});
  const mine = all[SLUG] ?? [];
  [...root.querySelectorAll<HTMLOListElement>(':scope > ol')].filter((ol) => ol.children.length >= 3).forEach((ol, k) => {
    const items = [...ol.children] as HTMLLIElement[];
    const done = items.map((_, i) => !!mine[k]?.[i]);
    ol.classList.add('steps');
    const bar = document.createElement('div');
    bar.className = 'steps-bar';
    bar.innerHTML = '<span class="sb-track"><i></i></span><span class="sb-n"></span><button type="button" class="sb-reset">Reset</button>';
    ol.before(bar);
    const paint = () => {
      const n = done.filter(Boolean).length;
      items.forEach((li, i) => { li.classList.toggle('done', done[i]); li.querySelector('.tick')?.setAttribute('aria-pressed', String(done[i])); });
      bar.querySelector<HTMLElement>('.sb-track i')!.style.width = `${(n / items.length) * 100}%`;
      bar.querySelector('.sb-n')!.textContent = n === items.length ? 'All done' : n ? `${n} of ${items.length} done` : `${items.length} steps · tick them off as you go`;
      bar.classList.toggle('complete', n === items.length);
      bar.querySelector<HTMLElement>('.sb-reset')!.hidden = !n;
      mine[k] = done.slice();
      all[SLUG] = mine;
      save('steps', all);
    };
    items.forEach((li, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'tick';
      b.innerHTML = `<span class="num">${i + 1}</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>`;
      b.setAttribute('aria-label', `Step ${i + 1} done`);
      b.addEventListener('click', () => { done[i] = !done[i]; paint(); });
      li.prepend(b);
    });
    bar.querySelector('.sb-reset')!.addEventListener('click', () => { done.fill(false); paint(); });
    paint();
  });
}

// ------------------------------------------------------------------ section links, video, pictures
function initAnchors(root: HTMLElement) {
  root.addEventListener('click', async (e) => {
    const a = (e.target as HTMLElement).closest<HTMLAnchorElement>('a.anchor');
    if (!a) return;
    e.preventDefault();
    const id = a.getAttribute('href')!.slice(1);
    history.replaceState(null, '', `#${id}`);
    flash(document.getElementById(id));
    try { await navigator.clipboard.writeText(location.href); toast('Link to this section copied'); } catch { toast('Link is in the address bar'); }
  });
}

function flash(el: HTMLElement | null) {
  if (!el) return;
  el.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
}

function initMedia(root: HTMLElement) {
  root.querySelectorAll<HTMLAnchorElement>('a[href$=".mp4"]').forEach((a) => {
    const img = a.querySelector('img');
    if (!img) return;
    a.classList.add('playable');
    a.insertAdjacentHTML('beforeend', '<span class="play" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M8 5.5v13l11-6.5z"/></svg></span>');
    a.setAttribute('aria-label', `Play: ${img.alt || 'video'}`);
    a.addEventListener('click', (e) => {
      e.preventDefault();
      const v = document.createElement('video');
      Object.assign(v, { src: a.href, poster: img.currentSrc || img.src, controls: true, autoplay: true, playsInline: true });
      v.className = 'inline-video';
      a.replaceWith(v);
      v.focus();
    });
  });
  const pics = [...root.querySelectorAll<HTMLImageElement>('img')].filter((i) => !i.closest('a'));
  pics.forEach((img) => {
    img.classList.add('zoomable');
    img.tabIndex = 0;
    const open = () => {
      const d = document.createElement('dialog');
      d.className = 'zoom';
      d.innerHTML = `<img src="${img.currentSrc || img.src}" alt="${esc(img.alt)}" />${img.alt ? `<p>${esc(img.alt)}</p>` : ''}`;
      d.addEventListener('click', () => d.close());
      d.addEventListener('close', () => d.remove());
      document.body.append(d);
      d.showModal();
    };
    img.addEventListener('click', open);
    img.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
  });
}

// ------------------------------------------------------------------ arriving from a search: the words stay lit
function initArrival(root: HTMLElement) {
  const q = new URLSearchParams(location.search).get('q');
  const words = q ? wordsOf(q) : [];
  if (location.hash) requestAnimationFrame(() => flash(document.getElementById(decodeURIComponent(location.hash.slice(1)))));
  if (!words.length) return;
  const marks = highlight(root, words);
  if (!marks.length) return;
  let at = Math.max(0, marks.findIndex((m) => m.getBoundingClientRect().top > 60));
  const pill = document.createElement('div');
  pill.className = 'found';
  pill.innerHTML = `<span>"${esc(q!)}" · <b></b></span><button type="button" data-d="-1" aria-label="Previous match">↑</button>`
    + `<button type="button" data-d="1" aria-label="Next match">↓</button><button type="button" data-x>Clear</button>`;
  document.body.append(pill);
  const go = (d: number) => {
    marks[at]?.classList.remove('cur');
    at = (at + d + marks.length) % marks.length;
    marks[at].classList.add('cur');
    marks[at].scrollIntoView({ block: 'center', behavior: 'smooth' });
    pill.querySelector('b')!.textContent = `${at + 1} of ${marks.length}`;
  };
  pill.querySelector('b')!.textContent = `${marks.length} on this page`;
  pill.addEventListener('click', (e) => {
    const b = (e.target as HTMLElement).closest('button');
    if (!b) return;
    if (b.hasAttribute('data-x')) {
      unhighlight(root);
      pill.remove();
      history.replaceState(null, '', location.pathname + location.hash);
    } else go(Number(b.dataset.d));
  });
}

// ------------------------------------------------------------------ pages you've read, keys
function initRead() {
  const read = new Set(store<string[]>('read', []));
  const paint = () => document.querySelectorAll<HTMLElement>('[data-doc-link]').forEach((a) => a.classList.toggle('read', read.has(a.dataset.docLink!)));
  paint();
  if (!SLUG) return;
  let timer = window.setTimeout(mark, 25000);
  function mark() {
    if (read.has(SLUG)) return;
    read.add(SLUG);
    save('read', [...read]);
    paint();
    clearTimeout(timer);
  }
  addEventListener('scroll', () => {
    const max = document.documentElement.scrollHeight - innerHeight;
    if (max > 0 && scrollY / max > 0.6) mark();
  }, { passive: true });
}

function initKeys() {
  addEventListener('keydown', (e) => {
    const el = e.target as HTMLElement;
    if (el.closest('input, textarea, select, [contenteditable="true"]') || e.altKey) return;
    if (e.key === '/' || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k')) {
      const input = document.querySelector<HTMLInputElement>('[data-docsearch] input');
      if (!input) return;
      e.preventDefault();
      input.focus();
      input.select();
    } else if (!e.ctrlKey && !e.metaKey && (e.key === '[' || e.key === ']')) {
      document.querySelector<HTMLAnchorElement>(e.key === '[' ? '.pn .pv' : '.pn .nx')?.click();
    } else if (e.key === 'Escape') hideTerm();
  });
  if (coarse) document.querySelectorAll<HTMLElement>('[data-keyhint]').forEach((k) => (k.hidden = true));
}

// ------------------------------------------------------------------ overview: a reading path for what you came to do
function initPaths() {
  const chips = [...document.querySelectorAll<HTMLButtonElement>('[data-path]')];
  if (!chips.length) return;
  const cards = [...document.querySelectorAll<HTMLElement>('[data-doc-card]')];
  const strip = document.querySelector<HTMLElement>('[data-path-strip]')!;
  const names = new Map(cards.map((c) => [c.dataset.docCard!, c.querySelector('b')?.textContent ?? '']));
  const choose = (chip: HTMLButtonElement | null) => {
    chips.forEach((c) => c.setAttribute('aria-pressed', String(c === chip)));
    const path = chip ? chip.dataset.path!.split(',') : [];
    cards.forEach((c) => {
      const i = path.indexOf(c.dataset.docCard!);
      c.classList.toggle('dim', !!chip && i < 0);
      c.classList.toggle('in-path', i >= 0);
      if (i >= 0) c.dataset.step = String(i + 1); else delete c.dataset.step;
    });
    strip.hidden = !chip;
    strip.innerHTML = chip ? `<p>${esc(chip.dataset.say ?? '')}</p><ol>${path.map((s) => `<li><a href="${BASE}${s}/" data-doc-link="${s}">${esc(names.get(s) ?? s)}</a></li>`).join('')}</ol>` : '';
    save('path', chip?.dataset.key ?? '');
    initRead();
  };
  chips.forEach((c) => c.addEventListener('click', () => choose(c.getAttribute('aria-pressed') === 'true' ? null : c)));
  const kept = store<string>('path', '');
  const start = chips.find((c) => c.dataset.key === kept);
  if (start) choose(start);
}

// ------------------------------------------------------------------ go
if (prose) {
  addTerms(prose);
  prose.querySelectorAll<HTMLElement>('.tbl').forEach(initTable);
  initSteps(prose);
  initAnchors(prose);
  initMedia(prose);
  initArrival(prose);
  document.querySelectorAll<HTMLAnchorElement>('[data-toc]').forEach((a) => a.addEventListener('click', (e) => {
    e.preventDefault();
    history.replaceState(null, '', `#${a.dataset.toc}`);
    flash(document.getElementById(a.dataset.toc!));
  }));
}
initRead();
initKeys();
initPaths();
