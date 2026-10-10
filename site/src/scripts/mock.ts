// The interactive FramePort window on the project page: a simplified copy of the app (same layout, colours and
// words) driven by a small state machine. Two ways in: explore a demo setup (a connected Frame, a library), or a
// guided tour from a fresh start (connect the Frame, scan a folder, install a game, play). Nothing here talks to a
// real Frame; the window says so.
import { coverVars, initials } from './cover';

export interface MockGame { title: string; engine: string; xr: string; status: 'works' | 'issues'; platform: string }

type Route = 'library' | 'game' | 'frame' | 'files' | 'live' | 'keys' | 'monitor';
interface State {
  connected: boolean;
  games: MockGame[];
  onFrame: Set<string>;
  route: Route;
  current: string | null;
  installing: { title: string; stage: number } | null;
  playing: string | null;
  starting: string | null;
  live: boolean;
  scanning: boolean;
  filter: 'all' | 'frame' | 'pc';
  location: string;
  typed: string;
  patches: Record<string, boolean>;
}

const STAGES = ['scan', 'analyze', 'recipe', 'convert', 'patch', 'sign', 'check', 'upload', 'library', 'test'];
const PATCHES: [string, string, string][] = [
  ['framebridge', 'FrameBridge adapter', 'Fills in what the Frame lacks: passthrough, the room, curved and 360° layers.'],
  ['haptics', 'Controller vibration fix', 'Vibrations stop when the game stops them, at the strength it asked for.'],
  ['focus', 'Keep playing through short focus dips', 'The Frame drops focus for a moment now and then; the game keeps running.'],
  ['spacewarp', 'Turn off space warp', 'Some games flicker with it on the Frame.'],
];

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const h32 = (s: string) => { let h = 2166136261; for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 16777619) >>> 0; return h; };
const size = (t: string) => `${(0.4 + (h32(t) % 38) / 10).toFixed(1)} GiB`;

const ICON: Record<string, string> = {
  library: '<path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z"/>',
  frame: '<path d="M3 9.5A3.5 3.5 0 0 1 6.5 6h11A3.5 3.5 0 0 1 21 9.5v4a3.5 3.5 0 0 1-3.5 3.5h-2.2a2 2 0 0 1-1.7-.95l-.75-1.2a1 1 0 0 0-1.7 0l-.75 1.2A2 2 0 0 1 8.7 17H6.5A3.5 3.5 0 0 1 3 13.5z"/>',
  files: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  live: '<circle cx="12" cy="12" r="2"/><path d="M7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4M5 5a10 10 0 0 0 0 14M19 5a10 10 0 0 1 0 14"/>',
  keys: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
  monitor: '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
};
const icon = (name: string, size = 18) =>
  `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name] ?? ''}</svg>`;
const PLAY = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M8 5.5v13l11-6.5z" fill="currentColor"/></svg>';

function cover(g: MockGame, cls = '') {
  return `<div class="cv ${cls}" style="${coverVars(g.title)}"><i class="sun"></i><i class="band"></i>`
    + `<span class="ic">${esc(initials(g.title))}</span><b class="tt">${esc(g.title)}</b></div>`;
}

export function initMock(root: HTMLElement, games: MockGame[]) {
  const $ = <T extends Element = HTMLElement>(sel: string) => root.querySelector<T>(sel)!;
  const view = $('[data-view]');
  const side = $('[data-sidebar]');
  const frameCard = $('[data-framecard]');
  const activity = $('[data-activity]');
  const layer = $('[data-layer]');
  const coach = $('[data-coach]');
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  const demo = (): State => ({
    connected: true, games: games.slice(), onFrame: new Set(games.slice(0, 5).map((g) => g.title)), route: 'library',
    current: null, installing: null, playing: games[0]?.title ?? null, starting: null, live: false, scanning: false,
    filter: 'all', location: 'Videos', typed: '', patches: { framebridge: true, haptics: true, focus: true, spacewarp: false },
  });
  const fresh = (): State => ({ ...demo(), connected: false, games: [], onFrame: new Set(), playing: null });
  let s = demo();
  let timers: number[] = [];
  const later = (fn: () => void, ms: number) => { timers.push(window.setTimeout(fn, reduce ? Math.min(ms, 60) : ms)); };
  const clearTimers = () => { timers.forEach(clearTimeout); timers = []; };

  // ------------------------------------------------------------------ rendering
  function render() {
    side.querySelectorAll<HTMLElement>('[data-route]').forEach((b) => b.setAttribute('aria-current', String(b.dataset.route === s.route
      || (s.route === 'game' && b.dataset.route === 'library'))));
    frameCard.innerHTML = s.connected
      ? `<div class="fc-row"><span class="ring" style="--p:82"><b>82</b></span><div><b>steamframe</b><span class="ok">● Connected</span></div></div>`
        + (s.playing ? `<div class="np"><span>${esc(s.playing)}</span><span class="fps">72 <small>fps</small></span></div>` : '')
      : `<div class="fc-row off"><span class="ring" style="--p:0"><b>–</b></span><div><b>Steam Frame</b><span>Not connected</span></div></div>`;
    activity.innerHTML = s.installing
      ? `<div class="act"><b>Installing ${esc(s.installing.title)}</b><span>${STAGES[Math.min(s.installing.stage, 9)]}…</span>`
        + `<i style="--w:${((s.installing.stage + 1) / STAGES.length) * 100}%"></i></div>`
      : '<div class="act idle">No activity</div>';
    view.innerHTML = VIEWS[s.route]();
    view.scrollTop = 0;
    afterRender();
  }

  const VIEWS: Record<Route, () => string> = {
    library: () => {
      const list = s.games.filter((g) => s.filter === 'all' || (s.filter === 'frame') === s.onFrame.has(g.title));
      const chips = ([['all', 'All'], ['frame', 'On Frame'], ['pc', 'On this PC']] as const)
        .map(([k, l]) => `<button class="chip" data-filter="${k}" aria-pressed="${s.filter === k}">${l}</button>`).join('');
      return `<header class="vh"><div><h3>Library</h3><p>${s.games.length} ${s.games.length === 1 ? 'game' : 'games'} · ${s.onFrame.size} on your Frame</p></div>`
        + `<div class="vh-act"><span class="search">Search games and tags</span>`
        + `<span class="menu-wrap"><button class="btn-p" data-act="add">+ Add games</button>`
        + `<span class="menu" data-menu hidden><button data-act="scan">Scan a folder…</button><button disabled>Add a Linux app…</button>`
        + `<button disabled>Install from a link…</button></span></span></div></header>`
        + `<div class="chips">${chips}</div>`
        + (s.scanning ? '<p class="scan">Scanning <code>D:\\VR games</code>…</p>' : '')
        + (s.games.length
          ? `<div class="grid">${list.map((g, i) => `<button class="card" data-game="${esc(g.title)}" style="--d:${i * 50}ms">`
            + cover(g) + `<span class="tags"><span class="tag">${g.platform === 'PC VR' ? 'PC VR' : 'Quest'}</span>`
            + (s.onFrame.has(g.title) ? '<span class="tag on">On Frame</span>' : '') + '</span>'
            + `<span class="sub">${g.status === 'works' ? '<i class="ok">●</i> Works' : '<i class="warn">●</i> Works with issues'}</span>`
            + (s.onFrame.has(g.title) ? `<span class="cplay" data-act="quickplay" data-title="${esc(g.title)}">${PLAY}</span>` : '')
            + '</button>').join('')}</div>`
          : `<div class="empty"><div class="portal-ico"></div><h4>Ready when you are</h4><p>Add a folder with your games. FramePort finds Quest games, PC VR games, Android and Linux apps in it.</p></div>`);
    },
    game: () => {
      const g = s.games.find((x) => x.title === s.current);
      if (!g) return '';
      const on = s.onFrame.has(g.title);
      const busy = s.installing?.title === g.title;
      const starting = s.starting === g.title;
      const main = busy
        ? `<button class="btn-p" data-act="installing" disabled>Installing… ${STAGES[Math.min(s.installing!.stage, 9)]}</button>`
        : on
          ? `<button class="btn-p" data-act="play">${starting ? '<span class="spin"></span> Starting on Frame…' : `${PLAY} Play on Frame`}</button>`
          : `<button class="btn-p" data-act="install" ${s.connected ? '' : 'disabled title="Connect a Frame first"'}>Install on Frame</button>`;
      return `<div class="hero" style="${coverVars(g.title)}"><i class="sun"></i><i class="band"></i>`
        + `<button class="back" data-route="library">← Library</button>`
        + `<div class="hero-t"><p class="meta">${g.platform === 'PC VR' ? 'PC VR' : 'Meta Quest'} · ${esc(g.engine)} · ${esc(g.xr)}</p>`
        + `<h3>${esc(g.title)}</h3><p class="pills"><span class="${g.status === 'works' ? 'p-ok' : 'p-warn'}">● ${g.status === 'works' ? 'Works' : 'Works with issues'}</span>`
        + (on ? '<span class="p-on">On Frame</span>' : '') + `</p><div class="row">${main}<button class="btn-s">…</button></div></div></div>`
        + `<details class="cust" ${on ? '' : 'open'}><summary>Customize <span>${Object.values(s.patches).filter(Boolean).length} patches on</span></summary>`
        + PATCHES.map(([k, t, d]) => `<label class="sw"><span><b>${t}</b><small>${d}</small></span>`
          + `<input type="checkbox" data-patch="${k}" ${s.patches[k] ? 'checked' : ''}><i></i></label>`).join('')
        + `</details><div class="kv"><span>Size</span><b>${size(g.title)}</b><span>Recipe</span><b>Tested recipe from the catalog</b>`
        + `<span>Where</span><b>${on ? 'Steam Frame · Steam library' : 'This PC'}</b></div>`;
    },
    frame: () => s.connected
      ? `<header class="vh"><div><h3>Steam Frame</h3><p>steamframe · SteamOS · connected over Wi-Fi</p></div></header>`
        + '<ul class="checks">' + ['Developer Mode is on', 'FramePort can sign in (SSH key)', 'Lepton (Android) is installed',
          'Proton for PC VR games', 'Storage: 212 GiB free'].map((c) => `<li><i class="ok">✓</i>${c}</li>`).join('') + '</ul>'
        + `<p class="note">Battery 82 % · ${s.onFrame.size} ${s.onFrame.size === 1 ? 'game' : 'games'} installed by FramePort</p>`
      : `<header class="vh"><div><h3>Connect your Steam Frame</h3><p>Once. After that FramePort finds it by itself.</p></div></header>`
        + `<ol class="wiz"><li>Keep FramePort open. The Frame and this computer must be on the same Wi-Fi (or a USB cable).</li>`
        + `<li>Show the setup command and type it on the Frame.</li></ol>`
        + `<button class="btn-p" data-act="show-command">Show setup command</button>`,
    files: () => {
      const locs: Record<string, [string, string][]> = {
        Videos: [['nasa-360-launch.mp4', '412 MiB'], ['tour-of-the-moon.mp4', '1.1 GiB']],
        Downloads: [['fpv-demo.apk', '96 MiB'], ['notes.txt', '2 KiB']],
        Documents: [['setlist.pdf', '340 KiB']],
        [`${s.games[0]?.title ?? 'Game'} storage`]: [['saves/', '—'], ['config.ini', '4 KiB']],
      };
      if (!locs[s.location]) s.location = 'Videos';
      return `<header class="vh"><div><h3>Files</h3><p>The Frame's files. Drag files here to upload them.</p></div>`
        + `<button class="btn-s" data-act="upload">Upload…</button></header><div class="files">`
        + `<ul class="locs">${Object.keys(locs).map((l) => `<li><button data-loc="${esc(l)}" aria-current="${l === s.location}">${esc(l)}</button></li>`).join('')}</ul>`
        + `<ul class="flist">${locs[s.location].map(([n, z]) => `<li><span>${esc(n)}</span><span>${z}</span></li>`).join('')}</ul></div>`;
    },
    live: () => `<header class="vh"><div><h3>Live view</h3><p>What the headset shows, with sound, in a browser window.</p></div></header>`
      + `<div class="screen ${s.live ? 'on' : ''}">${s.live
        ? '<div class="scene3d"><i class="sky"></i><i class="floor"></i><i class="orb"></i></div><span class="badge">● Live · 32 fps · hardware encoder</span>'
        : `<button class="btn-p" data-act="live" ${s.connected ? '' : 'disabled'}>Start live view</button>`}</div>`
      + (s.live ? '<button class="btn-s" data-act="live">Stop</button>' : ''),
    keys: () => `<header class="vh"><div><h3>Type on Frame</h3><p>Your keyboard, typing on the Frame: in VR apps, Steam and the desktop.</p></div></header>`
      + `<label class="typebox"><span>Type here</span><input data-type placeholder="Try it" value="${esc(s.typed)}" ${s.connected ? '' : 'disabled'}></label>`
      + `<div class="onframe"><span>On the Frame</span><p data-typed>${esc(s.typed) || '<em>…</em>'}<i class="caret"></i></p></div>`,
    monitor: () => s.playing
      ? `<header class="vh"><div><h3>Monitor</h3><p>Live from the Frame, while this tab is open.</p></div></header>`
        + `<div class="mon-game"><div>${cover(s.games.find((g) => g.title === s.playing) ?? s.games[0], 'mini')}</div>`
        + `<div><b>${esc(s.playing)}</b><span class="big"><b data-fps>72</b> fps</span><span class="sub">target 72 · frames paced</span></div>`
        + `<svg class="spark big-spark" viewBox="0 0 120 30" preserveAspectRatio="none"><polyline data-spark="fps"/></svg></div>`
        + `<div class="tiles">${[['CPU', 'cpu', '%'], ['GPU', 'gpu', '%'], ['Temperature', 'temp', '°C'], ['Battery', 'bat', '%']]
          .map(([l, k, u]) => `<div class="tile"><span>${l}</span><b><span data-v="${k}">–</span>${u}</b>`
            + `<svg class="spark" viewBox="0 0 120 30" preserveAspectRatio="none"><polyline data-spark="${k}"/></svg></div>`).join('')}</div>`
      : `<div class="empty"><div class="portal-ico"></div><h4>Nothing playing</h4><p>Start a game on the Frame to see its frame rate, load and temperatures.</p></div>`,
  };

  // ------------------------------------------------------------------ live numbers on the Monitor tab
  const series: Record<string, number[]> = {};
  const base: Record<string, [number, number]> = { fps: [72, 0.6], cpu: [46, 6], gpu: [71, 5], temp: [52, 1.5], bat: [82, 0.1] };
  let tick = 0;
  window.setInterval(() => {
    if (s.route !== 'monitor' || !s.playing || document.hidden) return;
    tick++;
    for (const [k, [mid, amp]] of Object.entries(base)) {
      const a = (series[k] ??= Array.from({ length: 40 }, () => mid));
      const next = k === 'bat' ? Math.max(0, a[a.length - 1] - (tick % 20 === 0 ? 1 : 0))
        : mid + (Math.random() - 0.5) * 2 * amp + (k === 'fps' && Math.random() < 0.04 ? -6 : 0);
      a.push(k === 'fps' ? Math.min(72, next) : next);
      a.shift();
      const el = view.querySelector(`[data-v="${k}"]`);
      if (el) el.textContent = String(Math.round(a[a.length - 1]));
      if (k === 'fps') { const f = view.querySelector('[data-fps]'); if (f) f.textContent = String(Math.round(a[a.length - 1])); }
      const line = view.querySelector(`[data-spark="${k}"]`);
      if (line) {
        const lo = Math.min(...a) - 1, hi = Math.max(...a) + 1;
        line.setAttribute('points', a.map((v, i) => `${(i / (a.length - 1)) * 120},${30 - ((v - lo) / (hi - lo)) * 28 - 1}`).join(' '));
      }
    }
  }, reduce ? 1500 : 500);

  function afterRender() { coachUpdate(); }

  // ------------------------------------------------------------------ actions
  function go(route: Route, current: string | null = s.current) {
    s.route = route;
    s.current = current;
    render();
    emit(`route:${route}`);
  }

  function install(title: string) {
    s.installing = { title, stage: 0 };
    render();
    const step = () => {
      if (!s.installing) return;
      if (s.installing.stage >= STAGES.length - 1) {
        s.onFrame.add(title);
        s.installing = null;
        render();
        emit('installed');
        return;
      }
      s.installing.stage++;
      render();
      later(step, s.installing.stage === 7 ? 1100 : 420);
    };
    later(step, 500);
  }

  function play(title: string) {
    s.starting = title;
    s.playing = title;
    render();
    root.querySelector('[data-act="play"], [data-act="quickplay"]')?.classList.add('ripple');
    later(() => { s.starting = null; render(); emit('played'); }, 1800);
  }

  function scan() {
    s.scanning = true;
    s.games = [];
    render();
    games.forEach((g, i) => later(() => {
      s.games.push(g);
      if (i === games.length - 1) { s.scanning = false; emit('scanned'); }
      render();
    }, 400 + i * 160));
  }

  function showCommand() {
    layer.innerHTML = `<div class="dlg" role="dialog" aria-label="Setup command"><h4>Set up your Steam Frame</h4>`
      + `<p>On the Frame: open the SteamVR dashboard → <b>Desktop</b>, then <b>Konsole</b>, and type:</p>`
      + `<div class="cmdl"><code>curl -fsS 192.0.2.10:8765/k3f9q | bash</code></div>`
      + `<p class="small">Example address and code; FramePort shows yours. No root, no sudo, no password.</p>`
      + `<ul class="progress" data-prog></ul>`
      + `<div class="row"><button class="btn-p" data-act="ran">I typed it on the Frame</button><button class="btn-s" data-act="close">Close</button></div></div>`;
    layer.hidden = false;
    emit('command');
  }

  function connect() {
    const items = ['Frame answered the setup server', 'Developer Mode on (Steam restarts once)', 'Lepton installed', 'SSH key added'];
    const prog = layer.querySelector('[data-prog]');
    const btn = layer.querySelector<HTMLButtonElement>('[data-act="ran"]');
    if (btn) btn.disabled = true;
    items.forEach((t, i) => later(() => { prog?.insertAdjacentHTML('beforeend', `<li><i class="ok">✓</i>${t}</li>`); }, 500 + i * 650));
    later(() => { layer.hidden = true; layer.innerHTML = ''; s.connected = true; render(); emit('connected'); }, 500 + items.length * 650 + 400);
  }

  root.addEventListener('click', (ev) => {
    const t = ev.target as HTMLElement;
    const el = t.closest<HTMLElement>('[data-route],[data-game],[data-act],[data-filter],[data-loc]');
    if (!el || !root.contains(el)) {
      root.querySelector('[data-menu]')?.setAttribute('hidden', '');
      return;
    }
    if (el.dataset.route) return go(el.dataset.route as Route, el.dataset.route === 'game' ? s.current : null);
    if (el.dataset.filter) { s.filter = el.dataset.filter as State['filter']; return render(); }
    if (el.dataset.loc) { s.location = el.dataset.loc; return render(); }
    const act = el.dataset.act;
    if (act === 'quickplay') { ev.stopPropagation(); return play(el.dataset.title!); }
    if (el.dataset.game) return go('game', el.dataset.game);
    switch (act) {
      case 'add': root.querySelector('[data-menu]')?.toggleAttribute('hidden'); coachUpdate(); break;
      case 'scan': scan(); break;
      case 'install': if (s.current) install(s.current); break;
      case 'play': if (s.current) play(s.current); break;
      case 'show-command': showCommand(); break;
      case 'ran': connect(); break;
      case 'close': layer.hidden = true; layer.innerHTML = ''; break;
      case 'live': s.live = !s.live; render(); break;
      case 'upload': {
        const list = view.querySelector('.flist');
        list?.insertAdjacentHTML('afterbegin', '<li class="new"><span>holiday-video.mp4</span><span><i class="up"></i></span></li>');
        break;
      }
      case 'tour': startTour(); break;
      case 'explore': exploreMode(); break;
      case 'tour-next': advance(); break;
      case 'tour-skip': endTour(); break;
    }
  });
  root.addEventListener('change', (ev) => {
    const t = ev.target as HTMLInputElement;
    if (t.dataset.patch) s.patches[t.dataset.patch] = t.checked;
  });
  root.addEventListener('input', (ev) => {
    const t = ev.target as HTMLInputElement;
    if (t.matches('[data-type]')) {
      s.typed = t.value;
      const out = view.querySelector('[data-typed]');
      if (out) out.innerHTML = (esc(s.typed) || '<em>…</em>') + '<i class="caret"></i>';
    }
  });

  // ------------------------------------------------------------------ the guided tour
  interface Step { target?: string; alt?: string; text: string; title: string; wait?: string; next?: string; place?: 'center' }
  const STEPS: Step[] = [
    { title: 'You just started FramePort', text: 'The library is empty and no Frame is connected. Set up the Frame first: click <b>Steam Frame</b>.',
      target: '[data-sidebar] [data-route="frame"]', wait: 'route:frame' },
    { title: 'One command, once', text: 'FramePort makes a one-time setup command for your Frame.',
      target: '[data-act="show-command"]', wait: 'command' },
    { title: 'Type it on the Frame', text: 'On the Frame: SteamVR dashboard → Desktop → Konsole. Type the command, press Enter. Then click the button.',
      target: '.dlg', wait: 'connected' },
    { title: 'Connected', text: 'The Frame card shows the battery and later what is playing. FramePort finds the Frame again by itself.',
      target: '[data-framecard]', next: 'Next' },
    { title: 'Add your games', text: 'Back to the <b>Library</b>, then <b>+ Add games → Scan a folder…</b>',
      target: '[data-sidebar] [data-route="library"]', wait: 'route:library' },
    { title: 'Scan a folder', text: 'Open <b>+ Add games</b> and choose <b>Scan a folder…</b>. FramePort reads each game without changing it.',
      target: '[data-act="add"]', alt: '[data-menu]:not([hidden])', wait: 'scanned' },
    { title: 'Pick a game', text: 'Each game already knows its tested recipe. Open one.', target: '.grid .card', wait: 'route:game' },
    { title: 'One click', text: 'Install on Frame: convert, patch, sign, upload, add to Steam with artwork, launch test. Watch the activity card.',
      target: '[data-act="install"]', wait: 'installed' },
    { title: 'Play', text: 'It is in your Steam library on the Frame now. Start it from here, or in the headset.',
      target: '[data-act="play"]', wait: 'played' },
    { title: "That's the whole setup", text: 'Explore the other tabs (Monitor, Live view, Files, Type on Frame) or get FramePort.',
      place: 'center', next: 'Explore' },
  ];
  let step = -1;

  function startTour() {
    clearTimers();
    $('[data-start]').hidden = true;
    s = fresh();
    s.route = 'library';
    render();
    step = 0;
    later(coachUpdate, 50);
  }
  function exploreMode() {
    $('[data-start]').hidden = true;
    step = -1;
    coachUpdate();
  }
  function endTour() {
    step = -1;
    coach.hidden = true;
    root.querySelectorAll('.hl').forEach((e) => e.classList.remove('hl'));
  }
  function advance() {
    if (step < 0) return;
    step++;
    if (step >= STEPS.length) return endTour();
    coachUpdate();
  }
  function emit(event: string) {
    if (step >= 0 && STEPS[step].wait === event) later(advance, 250);
  }
  function coachUpdate() {
    root.querySelectorAll('.hl').forEach((e) => e.classList.remove('hl'));
    if (step < 0) { coach.hidden = true; return; }
    const st = STEPS[step];
    const target = (st.alt && root.querySelector<HTMLElement>(st.alt)) || (st.target ? root.querySelector<HTMLElement>(st.target) : null);
    // while installing, point at the activity card
    const card = root.querySelector<HTMLElement>('[data-activity]');
    const anchor = st.wait === 'installed' && s.installing
      ? (card && card.getBoundingClientRect().width > 0 ? card : root.querySelector<HTMLElement>('[data-act="installing"]'))
      : target;
    anchor?.classList.add('hl');
    coach.innerHTML = `<p class="cstep">Step ${step + 1} of ${STEPS.length}</p><h4>${st.title}</h4><p>${st.text}</p>`
      + `<div class="crow">${st.next ? `<button class="btn-p" data-act="tour-next">${st.next}</button>` : ''}`
      + `<button class="btn-s" data-act="tour-skip">${step === STEPS.length - 1 ? 'Close' : 'Skip tour'}</button></div>`;
    coach.hidden = false;
    const box = root.getBoundingClientRect();
    const shown = anchor && anchor.getBoundingClientRect().width > 0;
    if (!shown || st.place === 'center') {
      coach.style.left = `${box.width / 2 - coach.offsetWidth / 2}px`;
      coach.style.top = `${box.height / 2 - coach.offsetHeight / 2}px`;
      coach.dataset.side = 'none';
      return;
    }
    const r = anchor!.getBoundingClientRect();
    const w = coach.offsetWidth, hgt = coach.offsetHeight;
    // beside the target if there is room, else above or below it, else docked where it covers the least: never on
    // the target's lower part, where its buttons are
    const top0 = r.top - box.top, bottom0 = r.bottom - box.top;
    let left = r.right - box.left + 14, top = top0;
    let sideName = 'right';
    if (left + w > box.width - 8) { left = r.left - box.left - w - 14; sideName = 'left'; }
    if (left < 8) {
      left = Math.max(8, Math.min(box.width - w - 8, r.left - box.left));
      const below = box.height - bottom0 - 12, above = top0 - 12;
      if (below >= hgt + 8) { top = bottom0 + 12; sideName = 'below'; }
      else if (above >= hgt + 8) { top = top0 - hgt - 12; sideName = 'above'; }
      else { top = 8; sideName = 'docked'; }
    }
    top = Math.max(8, Math.min(top, box.height - hgt - 8));
    coach.style.left = `${left}px`;
    coach.style.top = `${top}px`;
    coach.dataset.side = sideName;
  }
  new ResizeObserver(() => coachUpdate()).observe(root);

  render();
}
