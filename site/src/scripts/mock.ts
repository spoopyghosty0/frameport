// The interactive FramePort window on the project page: a simplified copy of the app (same layout, colours, words)
// driven by a small state machine. Two ways in: explore a set-up demo (a connected Frame, a library), or a guided
// tour from a fresh start (connect the Frame, scan a folder, install, play). Nothing here talks to a real Frame, and
// files dropped on the Files tab never leave the browser; the window says so.
import { coverVars, initials } from './cover';
import { checkUrl, fileName as urlFileName } from './install-link';

export interface MockGame { title: string; engine: string; xr: string; status: 'works' | 'issues'; platform: string }

export type Route = 'library' | 'game' | 'frame' | 'files' | 'shots' | 'live' | 'keys' | 'monitor' | 'settings';
interface FileRow { name: string; size: string; progress?: number }
interface State {
  connected: boolean;
  link: 'wifi' | 'usb';
  games: MockGame[];
  onFrame: Set<string>;
  route: Route;
  current: string | null;
  queue: string[];
  installing: { title: string; stage: number } | null;
  tested: Set<string>;
  playing: string | null;
  starting: string | null;
  live: boolean;
  quality: string;
  scanning: boolean;
  filter: 'all' | 'frame' | 'pc';
  query: string;
  selecting: boolean;
  selected: Set<string>;
  location: string;
  files: Record<string, FileRow[]>;
  typed: string;
  patches: Record<string, boolean>;
  /** Customize opened or closed by the reader (null: the default, open until the game is installed) */
  custOpen: boolean | null;
  details: boolean;
  theme: string;
  processes: [string, string, string][];
}

const STAGES = ['scan', 'analyze', 'recipe', 'convert', 'patch', 'sign', 'check', 'upload', 'library', 'test'];
const PATCHES: [string, string, string, string][] = [
  ['framebridge', 'FrameBridge adapter', 'Fills in what the Frame lacks: passthrough, the room, curved and 360° layers.', 'frame.framebridge'],
  ['haptics', 'Controller vibration fix', 'Vibrations stop when the game stops them, at the strength it asked for.', 'adapter.haptic_fix'],
  ['focus', 'Keep playing through short focus dips', 'The Frame drops focus for a moment now and then; the game keeps running.', 'adapter.focus_hold'],
  ['textinput', 'Text fields work', 'Lets the game open the Frame’s keyboard for text fields.', 'frame.unity_text_input'],
  ['spacewarp', 'Turn off space warp', 'Some games flicker with it on the Frame.', 'overport.patch_disable_space_warp'],
];
export const THEMES: Record<string, { name: string; dual: boolean; c: Record<string, string> }> = {
  portal: { name: 'Portal', dual: true, c: { bg: '#0D0E12', sidebar: '#111217', surface: '#16181E', 'surface-2': '#1D1F27', 'surface-3': '#252832',
    border: '#2A2D37', 'border-strong': '#393D49', text: '#EDEEF2', 'text-2': '#A7ABB7', 'text-3': '#717583', accent: '#FF8A1F',
    'accent-soft': '#3A2512', 'on-accent': '#160C03', secondary: '#3AA8FF', ok: '#3DD68C', warn: '#F2C94C' } },
  portal_oled: { name: 'Portal (OLED)', dual: true, c: { bg: '#000000', sidebar: '#000000', surface: '#0A0A0D', 'surface-2': '#121317',
    'surface-3': '#1A1B21', border: '#1E1F26', 'border-strong': '#2B2D36', text: '#EDEEF2', 'text-2': '#A7ABB7', 'text-3': '#717583',
    accent: '#FF8A1F', 'accent-soft': '#2B1A0A', 'on-accent': '#160C03', secondary: '#3AA8FF', ok: '#3DD68C', warn: '#F2C94C' } },
  original: { name: 'Original', dual: false, c: { bg: '#0E0F13', sidebar: '#121319', surface: '#171920', 'surface-2': '#1E2029',
    'surface-3': '#262935', border: '#2C2F3B', 'border-strong': '#3A3E4D', text: '#ECEDF3', 'text-2': '#A9ADBD', 'text-3': '#737889',
    accent: '#8B7CFF', 'accent-soft': '#2A2550', 'on-accent': '#0E0F13', secondary: '#38BDF8', ok: '#4ADE80', warn: '#FBBF24' } },
};

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const h32 = (s: string) => { let h = 2166136261; for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 16777619) >>> 0; return h; };
const size = (t: string) => `${(0.4 + (h32(t) % 38) / 10).toFixed(1)} GiB`;
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const human = (b: number) => b >= 1 << 30 ? `${(b / (1 << 30)).toFixed(1)} GiB` : b >= 1 << 20 ? `${(b / (1 << 20)).toFixed(1)} MiB`
  : `${Math.max(1, Math.round(b / 1024))} KiB`;

const PLAY = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M8 5.5v13l11-6.5z" fill="currentColor"/></svg>';

function cover(g: MockGame, cls = '') {
  return `<div class="cv ${cls}" style="${coverVars(g.title)}"><i class="sun"></i><i class="band"></i>`
    + `<span class="ic">${esc(initials(g.title))}</span><b class="tt">${esc(g.title)}</b></div>`;
}

/** compact: one screen only (no window bar, sidebar or tour), for the live slides of the screenshot carousel. */
export interface MockOptions { route?: Route; compact?: boolean; live?: boolean }

export function initMock(root: HTMLElement, games: MockGame[], opts: MockOptions = {}) {
  const $ = <T extends Element = HTMLElement>(sel: string) => root.querySelector<T>(sel)!;
  const view = $('[data-view]');
  const side = $('[data-sidebar]');
  const frameCard = $('[data-framecard]');
  const activity = $('[data-activity]');
  const layer = $('[data-layer]');
  const coach = $('[data-coach]');
  const toasts = $('[data-toasts]');
  const menu = $('[data-ctx]');
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  const demoFiles = (): Record<string, FileRow[]> => ({
    Videos: [{ name: 'nasa-360-launch.mp4', size: '412 MiB' }, { name: 'tour-of-the-moon.mp4', size: '1.1 GiB' }],
    Downloads: [{ name: 'notes.txt', size: '2 KiB' }],
    Documents: [{ name: 'setlist.pdf', size: '340 KiB' }],
  });
  const demo = (): State => ({
    connected: true, link: 'wifi', games: games.slice(), onFrame: new Set(games.slice(0, 5).map((g) => g.title)), route: 'library',
    current: null, queue: [], installing: null, tested: new Set(games.slice(0, 5).map((g) => g.title)), playing: games[0]?.title ?? null,
    starting: null, live: false, quality: 'Balanced', scanning: false, filter: 'all', query: '', selecting: false, selected: new Set(),
    location: 'Videos', files: demoFiles(), typed: '', details: false, theme: 'portal', custOpen: null,
    patches: { framebridge: true, haptics: true, focus: true, textinput: true, spacewarp: false },
    processes: [['Game', 'the game', '38 %'], ['Steam', 'steamwebhelper', '6 %'], ['SteamVR', 'vrcompositor', '11 %'],
      ['Lepton', 'lepton-container', '4 %'], ['System', 'kwin_wayland', '2 %']],
  });
  const fresh = (): State => ({ ...demo(), connected: false, games: [], onFrame: new Set(), tested: new Set(), playing: null });
  let s = demo();
  let timers: number[] = [];
  const later = (fn: () => void, ms: number) => { timers.push(window.setTimeout(fn, reduce ? Math.min(ms, 60) : ms)); };
  const clearTimers = () => { timers.forEach(clearTimeout); timers = []; };

  function toast(text: string, kind: 'ok' | 'info' = 'ok') {
    const t = document.createElement('div');
    t.className = `toast ${kind}`;
    t.textContent = text;
    toasts.append(t);
    window.setTimeout(() => t.classList.add('out'), 2600);
    window.setTimeout(() => t.remove(), 3000);
  }

  function applyTheme() {
    const th = THEMES[s.theme];
    for (const [k, v] of Object.entries(th.c)) root.style.setProperty(`--${k}`, v);
    root.style.setProperty('--portal', th.dual ? `linear-gradient(90deg, ${th.c.secondary}, ${th.c.accent})` : th.c.accent);
    root.dataset.dual = String(th.dual);
  }

  // ------------------------------------------------------------------ rendering
  function render() {
    renderChrome();
    view.innerHTML = VIEWS[s.route]();
    afterRender();
  }
  /** The sidebar only (the Frame card, the activity card): progress ticks must not rebuild the page you're on. */
  function renderChrome() {
    side.querySelectorAll<HTMLElement>('[data-route]').forEach((b) => b.setAttribute('aria-current', String(b.dataset.route === s.route
      || (s.route === 'game' && b.dataset.route === 'library'))));
    frameCard.innerHTML = s.connected
      ? `<div class="fc-row"><span class="ring" style="--p:82"><b>82</b></span><div><b>steamframe</b>`
        + `<span class="ok">● ${s.link === 'usb' ? 'USB cable' : 'Wi-Fi'}</span></div></div>`
        + (s.playing ? `<div class="np"><span>${esc(s.playing)}</span><span class="fps">72 <small>fps</small></span></div>` : '')
      : `<div class="fc-row off"><span class="ring" style="--p:0"><b>–</b></span><div><b>Steam Frame</b><span>Not connected</span></div></div>`;
    activity.innerHTML = s.installing
      ? `<div class="act"><b>Installing ${esc(s.installing.title)}</b><span>${STAGES[Math.min(s.installing.stage, 9)]}…`
        + (s.queue.length ? ` · ${s.queue.length} queued` : '') + `</span>`
        + `<i style="--w:${((s.installing.stage + 1) / STAGES.length) * 100}%"></i></div>`
      : '<div class="act idle">No activity</div>';
  }

  const inTab = (g: MockGame) => s.filter === 'all' || (s.filter === 'frame') === s.onFrame.has(g.title);
  const matches = (g: MockGame) => !s.query.trim()
    || `${g.title} ${g.engine} ${g.xr} ${g.platform}`.toLowerCase().includes(s.query.trim().toLowerCase());
  const libraryList = () => s.games.filter((g) => inTab(g) && matches(g));

  const VIEWS: Record<Route, () => string> = {
    library: () => {
      const list = s.games.filter(inTab);
      const shown = list.filter(matches).length;
      const chips = ([['all', 'All'], ['frame', 'On Frame'], ['pc', 'On this PC']] as const)
        .map(([k, l]) => `<button class="chip" data-filter="${k}" aria-pressed="${s.filter === k}">${l}</button>`).join('');
      return `<header class="vh"><div><h3>Library</h3><p>${plural(s.games.length, 'game')} · ${s.onFrame.size} on your Frame</p></div>`
        + `<div class="vh-act"><input class="search" data-search placeholder="Search games and tags" value="${esc(s.query)}" aria-label="Search games">`
        + `<button class="btn-s" data-act="select" aria-pressed="${s.selecting}">${s.selecting ? 'Done' : 'Select'}</button>`
        + `<span class="menu-wrap"><button class="btn-p" data-act="add">+ Add games</button>`
        + `<span class="menu" data-menu hidden><button data-act="scan">Scan a folder…</button>`
        + `<button data-act="link">Install from a link…</button><button disabled>Add a Linux app…</button></span></span></div></header>`
        + `<div class="chips">${chips}<span class="hintr">Right-click a game for more</span></div>`
        + (s.scanning ? '<p class="scan">Scanning <code>D:\\VR games</code>…</p>' : '')
        + (s.selecting ? `<div class="selbar"><span>${plural(s.selected.size, 'game')} selected</span>`
          + `<button class="btn-p" data-act="batch" ${s.selected.size && s.connected ? '' : 'disabled'}>Install on Frame</button>`
          + `<button class="btn-s" data-act="selall">Select all</button></div>` : '')
        + (s.games.length
          ? (list.length ? `<div class="grid ${s.selecting ? 'selecting' : ''}">${list.map((g, i) => {
            const on = s.onFrame.has(g.title);
            return `<button class="card ${s.selected.has(g.title) ? 'sel' : ''}" data-game="${esc(g.title)}" style="--d:${Math.min(i, 12) * 40}ms"`
              + ` data-text="${esc(`${g.title} ${g.engine} ${g.xr} ${g.platform}`.toLowerCase())}" ${matches(g) ? '' : 'hidden'}>`
              + cover(g) + `<span class="tags"><span class="tag">${g.platform === 'PC VR' ? 'PC VR' : 'Quest'}</span>`
              + (on ? '<span class="tag on">On Frame</span>' : '') + '</span>'
              + (s.selecting ? `<span class="check">${s.selected.has(g.title) ? '✓' : ''}</span>` : '')
              + `<span class="sub">${g.status === 'works' ? '<i class="ok">●</i> Works' : '<i class="warn">●</i> Works with issues'}</span>`
              + (on && !s.selecting ? `<span class="cplay" data-act="quickplay" data-title="${esc(g.title)}" title="Play on Frame">${PLAY}</span>` : '')
              + '</button>';
          }).join('')}</div><p class="none" ${shown ? 'hidden' : ''}>No game matches “${esc(s.query)}”.</p>`
            : '<p class="none">Nothing here yet.</p>')
          : `<div class="empty"><div class="portal-ico"></div><h4>Ready when you are</h4><p>Add a folder with your games. FramePort finds Quest games, PC VR games, Android and Linux apps in it.</p>`
            + `<button class="btn-p" data-act="scan">Scan a folder…</button></div>`);
    },
    game: () => {
      const g = s.games.find((x) => x.title === s.current);
      if (!g) return '';
      const on = s.onFrame.has(g.title);
      const busy = s.installing?.title === g.title;
      const queued = s.queue.includes(g.title);
      const main = busy
        ? `<button class="btn-p" data-act="installing" disabled>Installing… ${STAGES[Math.min(s.installing!.stage, 9)]}</button>`
        : queued ? '<button class="btn-p" disabled>Queued</button>'
        : on
          ? `<button class="btn-p" data-act="play">${s.starting === g.title ? '<span class="spin"></span> Starting on Frame…' : `${PLAY} Play on Frame`}</button>`
          : `<button class="btn-p" data-act="install" ${s.connected ? '' : 'disabled title="Connect a Frame first"'}>Install on Frame</button>`;
      return `<div class="hero" style="${coverVars(g.title)}"><i class="sun"></i><i class="band"></i>`
        + `<button class="back" data-route="library">← Library</button>`
        + `<div class="hero-t"><p class="meta">${g.platform === 'PC VR' ? 'PC VR' : 'Meta Quest'} · ${esc(g.engine)} · ${esc(g.xr)}</p>`
        + `<h3>${esc(g.title)}</h3><p class="pills"><span class="${g.status === 'works' ? 'p-ok' : 'p-warn'}">● ${g.status === 'works' ? 'Works' : 'Works with issues'}</span>`
        + (on ? `<span class="p-on">On Frame · ${s.link === 'usb' ? 'USB' : 'Wi-Fi'}</span>` : '') + `</p><div class="row">${main}`
        + `<span class="menu-wrap"><button class="btn-s" data-act="more" aria-label="More actions">…</button><span class="menu" data-more hidden>`
        + `<button data-act="toast" data-msg="Steam artwork updated on the Frame">Update Steam art on Frame</button>`
        + `<button data-act="toast" data-msg="Artwork picker: store art, or your own images">Find artwork…</button>`
        + `<button data-act="toast" data-msg="Opens a prefilled GitHub issue">Share working recipe…</button>`
        + `<button data-act="toast" data-msg="Moved to the SD card">Move to…</button>`
        + (on ? `<button data-act="uninstall" class="danger">Uninstall from Frame</button>` : '')
        + `</span></span></div></div></div>`
        + (s.tested.has(g.title) ? `<div class="test"><span class="ok">✓</span><div><b>Launch test passed</b>`
          + `<span>Started in 6.4 s · OpenXR session running · frames paced at 72 fps · no errors in the log</span></div>`
          + `<button class="btn-s" data-act="toast" data-msg="The launch log opens in the app">Log</button></div>` : '')
        + `<details class="cust" ${(s.custOpen ?? !on) ? 'open' : ''}><summary>Customize <span>${Object.values(s.patches).filter(Boolean).length} patches on · from the tested recipe</span></summary>`
        + `<label class="sw tech"><span><b>Show technical details</b></span><input type="checkbox" data-details ${s.details ? 'checked' : ''}><i></i></label>`
        + PATCHES.map(([k, t, d, id]) => `<label class="sw"><span><b>${t}</b><small>${d}</small>${s.details ? `<code>${id}</code>` : ''}</span>`
          + `<input type="checkbox" data-patch="${k}" ${s.patches[k] ? 'checked' : ''}><i></i></label>`).join('')
        + `</details><div class="kv"><span>Size</span><b>${size(g.title)}</b><span>Recipe</span><b>Tested recipe from the catalog</b>`
        + `<span>Where</span><b>${on ? 'Steam Frame · Steam library' : 'This PC'}</b></div>`;
    },
    frame: () => s.connected
      ? `<header class="vh"><div><h3>Steam Frame</h3><p>steamframe · SteamOS · connected ${s.link === 'usb' ? 'with a USB cable' : 'over Wi-Fi'}</p></div>`
        + `<div class="pw"><button class="btn-s" data-act="toast" data-msg="The Frame goes to sleep">Sleep</button><button class="btn-s" data-act="toast" data-msg="The Frame restarts">Restart</button></div></header>`
        + '<ul class="checks">' + ['Developer Mode is on', 'FramePort can sign in (SSH key)', 'Lepton (Android) is installed',
          'Proton for PC VR games', 'Storage: 212 GiB free'].map((c) => `<li><i class="ok">✓</i>${c}</li>`).join('') + '</ul>'
        + `<p class="note">Battery 82 % · ${plural(s.onFrame.size, 'game')} installed by FramePort</p>`
      : `<header class="vh"><div><h3>Connect your Steam Frame</h3><p>Once. After that FramePort finds it by itself.</p></div></header>`
        + `<div class="ways" role="radiogroup" aria-label="How to connect">`
        + `<button class="way" data-link="wifi" aria-checked="${s.link === 'wifi'}" role="radio"><b>Wi-Fi</b><span>Same network as this PC, or the Frame’s own hotspot.</span></button>`
        + `<button class="way" data-link="usb" aria-checked="${s.link === 'usb'}" role="radio"><b>USB cable</b><span>A USB-C data cable. Turn on Developer Mode on the Frame first.</span></button></div>`
        + `<button class="btn-p" data-act="show-command">Show setup command</button>`,
    files: () => {
      const locs = Object.keys(s.files);
      if (!locs.includes(s.location)) s.location = locs[0];
      return `<header class="vh"><div><h3>Files</h3><p>The Frame's files. Drop files from your computer here.</p></div>`
        + `<label class="btn-s">Upload…<input type="file" multiple data-pick hidden></label></header><div class="files">`
        + `<ul class="locs">${locs.map((l) => `<li><button data-loc="${esc(l)}" aria-current="${l === s.location}">${esc(l)}</button></li>`).join('')}</ul>`
        + `<div class="drop" data-drop><ul class="flist">${s.files[s.location].map((f, i) => `<li><span>${esc(f.name)}</span>`
          + (f.progress !== undefined && f.progress < 100 ? `<span class="upbar"><i style="width:${f.progress}%"></i></span>` : `<span>${f.size}</span>`)
          + `<button class="x" data-del="${i}" aria-label="Delete ${esc(f.name)}">×</button></li>`).join('') || '<li class="empty-f">Empty</li>'}</ul>`
        + `<p class="dz">Drag files here from your desktop · demo: they stay in this browser tab</p></div></div>`;
    },
    shots: () => {
      const list = s.games.filter((g) => s.onFrame.has(g.title));
      return `<header class="vh"><div><h3>Screenshots</h3><p>The pictures you took in the headset, by game.</p></div></header>`
        + (list.length ? list.map((g) => `<h4 class="sh">${esc(g.title)}</h4><div class="shots">${[0, 1, 2].map((k) =>
          `<button class="shot-t" data-shot="${esc(g.title)}" data-k="${k}" style="${coverVars(g.title + k)}"><i class="sun"></i><i class="band"></i></button>`).join('')}</div>`).join('')
          : '<div class="empty"><h4>No screenshots yet</h4><p>Install and play a game; screenshots you take in the headset show up here.</p></div>');
    },
    live: () => `<header class="vh"><div><h3>Live view</h3><p>What the headset shows, with sound, in a browser window.</p></div>`
      + `<label class="qsel">Quality <select data-quality>${['Full', 'Balanced', 'Smooth'].map((q) => `<option ${q === s.quality ? 'selected' : ''}>${q}</option>`).join('')}</select></label></header>`
      + `<div class="screen ${s.live ? 'on' : ''}">${s.live
        ? `<div class="scene3d"><i class="sky"></i><i class="floor"></i><i class="orb"></i></div><span class="badge">● Live · ${s.quality === 'Full' ? '1920×1080' : s.quality === 'Balanced' ? '1280×720' : '960×540'} · 32 fps · hardware encoder</span>`
        : `<button class="btn-p" data-act="live" ${s.connected ? '' : 'disabled'}>Start live view</button>`}</div>`
      + (s.live ? '<button class="btn-s" data-act="live">Stop</button>' : ''),
    keys: () => `<header class="vh"><div><h3>Type on Frame</h3><p>Your keyboard, typing on the Frame: in VR apps, Steam and the desktop.</p></div></header>`
      + `<label class="typebox"><span>Type here</span><input data-type placeholder="Try it" value="${esc(s.typed)}" ${s.connected ? '' : 'disabled'}></label>`
      + `<div class="onframe"><span>On the Frame</span><p data-typed>${esc(s.typed) || '<em>…</em>'}<i class="caret"></i></p></div>`,
    monitor: () => s.playing
      ? `<header class="vh"><div><h3>Monitor</h3><p>Live from the Frame, while this tab is open.</p></div></header>`
        + `<div class="mon-game"><div>${cover(s.games.find((g) => g.title === s.playing) ?? s.games[0], 'mini')}</div>`
        + `<div><b>${esc(s.playing)}</b><span class="big"><b data-fps>72</b> fps</span><span class="sub">target 72 · frames paced</span>`
        + `<button class="btn-s endg" data-act="endgame">End game</button></div>`
        + `<svg class="spark big-spark" viewBox="0 0 120 30" preserveAspectRatio="none"><polyline data-spark="fps"/></svg></div>`
        + `<div class="tiles">${[['CPU', 'cpu', '%'], ['GPU', 'gpu', '%'], ['Temperature', 'temp', '°C'], ['Battery', 'bat', '%']]
          .map(([l, k, u]) => `<div class="tile"><span>${l}</span><b><span data-v="${k}">${Math.round(series[k]?.at(-1) ?? base[k][0])}</span>${u}</b>`
            + `<svg class="spark" viewBox="0 0 120 30" preserveAspectRatio="none"><polyline data-spark="${k}"/></svg></div>`).join('')}</div>`
        + `<table class="procs"><thead><tr><th>Process</th><th>Group</th><th>CPU</th><th></th></tr></thead><tbody>`
        + s.processes.map(([grp, name, cpu], i) => `<tr><td>${grp === 'Game' ? esc(s.playing!) : name}</td><td>${grp}</td><td>${cpu}</td>`
          + `<td>${grp === 'System' ? '<span class="lock" title="FramePort never ends this one">protected</span>' : `<button class="btn-s sm" data-end="${i}">End</button>`}</td></tr>`).join('')
        + '</tbody></table>'
      : `<div class="empty"><div class="portal-ico"></div><h4>Nothing playing</h4><p>Start a game on the Frame to see its frame rate, load and temperatures.</p></div>`,
    settings: () => `<header class="vh"><div><h3>Settings</h3><p>Appearance, updates, this PC.</p></div></header>`
      + `<h4 class="sh">Appearance</h4><div class="themes">${Object.entries(THEMES).map(([k, t]) =>
        `<button class="theme" data-theme="${k}" aria-pressed="${s.theme === k}" style="--a:${t.c.accent};--b:${t.c.secondary};--g:${t.c.bg};--s:${t.c.surface}">`
        + `<span class="sw-prev"><i></i><i></i><i></i></span><b>${t.name}</b></button>`).join('')}</div>`
      + `<h4 class="sh">Updates</h4><div class="row"><span class="note">FramePort updates itself; you can turn that off.</span>`
      + `<button class="btn-s" data-act="check">Check now</button></div>`,
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
    closeMenus();
    render();
    view.scrollTop = 0;
    emit(`route:${route}`);
  }

  function install(title: string) {
    if (s.installing) { if (!s.queue.includes(title)) s.queue.push(title); render(); return; }
    s.installing = { title, stage: 0 };
    render();
    const step = () => {
      if (!s.installing) return;
      if (s.installing.stage >= STAGES.length - 1) {
        s.onFrame.add(title);
        s.tested.add(title);
        s.installing = null;
        toast(`${title} is on your Frame. Launch test passed.`);
        const next = s.queue.shift();
        render();
        emit('installed');
        if (next) later(() => install(next), 400);
        return;
      }
      s.installing.stage++;
      renderChrome();
      const label = view.querySelector('[data-act="installing"]');
      if (label) label.textContent = `Installing… ${STAGES[s.installing.stage]}`;
      coachUpdate();
      later(step, s.installing.stage === 7 ? 1000 : 380);
    };
    later(step, 450);
  }

  function play(title: string) {
    s.starting = title;
    s.playing = title;
    render();
    root.querySelector('[data-act="play"]')?.classList.add('ripple');
    later(() => { s.starting = null; render(); toast(`${title} is starting in the headset`, 'info'); emit('played'); }, 1800);
  }

  function scan() {
    closeMenus();
    s.scanning = true;
    s.games = [];
    render();
    games.forEach((g, i) => later(() => {
      if (!s.games.some((x) => x.title === g.title)) s.games.push(g);
      if (i === games.length - 1) { s.scanning = false; emit('scanned'); toast(`Found ${plural(games.length, 'game')}, each with a tested recipe`); }
      render();
    }, 400 + i * 150));
  }

  function dialog(html: string) { layer.innerHTML = `<div class="dlg" role="dialog">${html}</div>`; layer.hidden = false; }
  function closeDialog() { layer.hidden = true; layer.innerHTML = ''; }

  function showCommand() {
    const addr = s.link === 'usb' ? '10.86.200.234' : '192.0.2.10';
    dialog(`<h4>Set up your Steam Frame</h4>`
      + (s.link === 'usb' ? '<p>Found the Frame on the USB cable.</p>' : '')
      + `<p>On the Frame: open the SteamVR dashboard → <b>Desktop</b>, then <b>Konsole</b>, and type:</p>`
      + `<div class="cmdl"><code>curl -fsS ${addr}:8765/k3f9q | bash</code></div>`
      + `<p class="small">${s.link === 'usb' ? 'Over the cable this PC is always 10.86.200.234.' : 'Example address and code; FramePort shows yours.'} No root, no sudo, no password.</p>`
      + `<ul class="progress" data-prog></ul>`
      + `<div class="row"><button class="btn-p" data-act="ran">I typed it on the Frame</button><button class="btn-s" data-act="close">Close</button></div>`);
    emit('command');
  }

  function connect() {
    const items = ['Frame answered the setup server', 'Developer Mode on (Steam restarts once)', 'Lepton installed', 'SSH key added'];
    const prog = layer.querySelector('[data-prog]');
    const btn = layer.querySelector<HTMLButtonElement>('[data-act="ran"]');
    if (btn) btn.disabled = true;
    items.forEach((t, i) => later(() => { prog?.insertAdjacentHTML('beforeend', `<li><i class="ok">✓</i>${t}</li>`); }, 500 + i * 600));
    later(() => { closeDialog(); s.connected = true; render(); toast(`Connected ${s.link === 'usb' ? 'with the USB cable' : 'over Wi-Fi'}`); emit('connected'); }, 500 + items.length * 600 + 400);
  }

  function linkDialog() {
    closeMenus();
    dialog(`<h4>Install from a link</h4><p>Paste an “Install with FramePort” or FrameDrop link, a manifest (.json) or an APK address.</p>`
      + `<input class="lin" data-link-in placeholder="https://example.com/games/cool-game-arm64.apk" value="https://example.com/games/cool-game-arm64.apk">`
      + `<p class="small" data-link-msg></p><div class="row"><button class="btn-p" data-act="link-go">Check link</button><button class="btn-s" data-act="close">Cancel</button></div>`);
    layer.querySelector<HTMLInputElement>('[data-link-in]')?.focus();
  }
  function linkGo() {
    const input = layer.querySelector<HTMLInputElement>('[data-link-in]');
    const msg = layer.querySelector<HTMLElement>('[data-link-msg]');
    if (!input || !msg) return;
    let text = input.value.trim();
    try {
      const u = new URL(text);
      if (/^(framedrop|frameport):$/.test(u.protocol) || u.hostname === 'framedropvr.com') text = u.searchParams.get('url') || u.searchParams.get('manifest') || '';
    } catch { /* checkUrl says why */ }
    const err = checkUrl(text);
    if (err) { msg.textContent = err; msg.className = 'small bad'; return; }
    const file = urlFileName(new URL(text));
    const title = file.replace(/\.(apk|zip|json|exe|appimage|tar\.gz)$/i, '').replace(/[-_]+(arm64|v8a|x86_64|\d[\w.]*)/gi, '')
      .replace(/[-_]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()).trim() || 'Linked app';
    closeDialog();
    if (!s.games.some((g) => g.title === title)) s.games.unshift({ title, engine: 'Unity', xr: 'OpenXR', status: 'works', platform: 'Quest' });
    toast(`Downloaded ${file} · added “${title}”`);
    go('game', title);
  }

  function addFiles(list: FileList | File[]) {
    const rows = s.files[s.location];
    for (const f of Array.from(list)) {
      const row: FileRow = { name: f.name, size: human(f.size), progress: 0 };
      rows.unshift(row);
      const stepUp = () => {
        row.progress = Math.min(100, (row.progress ?? 0) + 8 + Math.random() * 18);
        if (s.route === 'files') render();
        if (row.progress < 100) later(stepUp, 140);
        else toast(`${f.name} uploaded (not really: it stayed in your browser)`, 'info');
      };
      later(stepUp, 120);
    }
    render();
  }

  // ------------------------------------------------------------------ context menu (right-click on a game)
  function closeMenus() {
    root.querySelectorAll('[data-menu], [data-more]').forEach((m) => m.setAttribute('hidden', ''));
    menu.hidden = true;
  }
  function openCtx(x: number, y: number, title: string) {
    const on = s.onFrame.has(title);
    menu.innerHTML = `<p class="ctx-t">${esc(title)}</p>`
      + (on ? `<button data-ctx-act="play">${PLAY} Play on Frame</button>` : `<button data-ctx-act="install" ${s.connected ? '' : 'disabled'}>Install on Frame</button>`)
      + `<button data-ctx-act="open">Open game page</button><button data-ctx-act="select">Select</button>`
      + (on ? '<button data-ctx-act="uninstall" class="danger">Uninstall from Frame</button>' : '');
    menu.dataset.title = title;
    const box = root.getBoundingClientRect();
    menu.hidden = false;
    menu.style.left = `${Math.min(x - box.left, box.width - menu.offsetWidth - 8)}px`;
    menu.style.top = `${Math.min(y - box.top, box.height - menu.offsetHeight - 8)}px`;
  }

  root.addEventListener('contextmenu', (ev) => {
    const card = (ev.target as HTMLElement).closest<HTMLElement>('[data-game]');
    if (!card) return;
    ev.preventDefault();
    openCtx(ev.clientX, ev.clientY, card.dataset.game!);
  });

  root.addEventListener('click', (ev) => {
    const t = ev.target as HTMLElement;
    const ctxBtn = t.closest<HTMLElement>('[data-ctx-act]');
    if (ctxBtn) {
      const title = menu.dataset.title!;
      menu.hidden = true;
      switch (ctxBtn.dataset.ctxAct) {
        case 'play': s.current = title; return play(title);
        case 'install': return install(title);
        case 'open': return go('game', title);
        case 'select': s.selecting = true; s.selected.add(title); return render();
        case 'uninstall': s.onFrame.delete(title); toast(`${title} removed from the Frame (saves kept)`, 'info'); return render();
      }
    }
    const el = t.closest<HTMLElement>('[data-route],[data-game],[data-act],[data-filter],[data-loc],[data-link],[data-theme],[data-end],[data-del],[data-shot]');
    if (!t.closest('.menu, [data-ctx]')) { if (!el || !['add', 'more'].includes(el.dataset.act ?? '')) closeMenus(); }
    if (!el || !root.contains(el)) return;
    if (el.dataset.route) return go(el.dataset.route as Route, el.dataset.route === 'game' ? s.current : null);
    if (el.dataset.filter) { s.filter = el.dataset.filter as State['filter']; return render(); }
    if (el.dataset.loc) { s.location = el.dataset.loc; return render(); }
    if (el.dataset.link) { s.link = el.dataset.link as State['link']; return render(); }
    if (el.dataset.theme) { s.theme = el.dataset.theme; applyTheme(); toast(`Theme: ${THEMES[s.theme].name}`, 'info'); return render(); }
    if (el.dataset.end) {
      const [grp, name] = s.processes[Number(el.dataset.end)];
      if (grp === 'Game') return endGame();
      s.processes.splice(Number(el.dataset.end), 1);
      toast(`Ended ${name}`, 'info');
      return render();
    }
    if (el.dataset.del) { const [f] = s.files[s.location].splice(Number(el.dataset.del), 1); toast(`Deleted ${f.name}`, 'info'); return render(); }
    if (el.dataset.shot) return viewShot(el);
    const act = el.dataset.act;
    if (act === 'quickplay') { ev.stopPropagation(); s.current = el.dataset.title!; return play(el.dataset.title!); }
    if (el.dataset.game) {
      if (s.selecting) {
        const g = el.dataset.game;
        if (s.selected.has(g)) s.selected.delete(g); else s.selected.add(g);
        return syncSelection();
      }
      return go('game', el.dataset.game);
    }
    switch (act) {
      case 'add': root.querySelector('[data-menu]')?.toggleAttribute('hidden'); coachUpdate(); break;
      case 'more': root.querySelector('[data-more]')?.toggleAttribute('hidden'); break;
      case 'scan': scan(); break;
      case 'link': linkDialog(); break;
      case 'link-go': linkGo(); break;
      case 'select': s.selecting = !s.selecting; s.selected.clear(); render(); break;
      case 'selall': libraryList().forEach((g) => s.selected.add(g.title)); syncSelection(); break;
      case 'batch': {
        const titles = [...s.selected].filter((x) => !s.onFrame.has(x));
        s.selecting = false; s.selected.clear();
        toast(`${plural(titles.length, 'install')} queued`);
        titles.forEach((x) => install(x));
        break;
      }
      case 'install': if (s.current) install(s.current); break;
      case 'play': if (s.current) play(s.current); break;
      case 'uninstall': if (s.current) { s.onFrame.delete(s.current); toast(`${s.current} removed from the Frame (saves kept)`, 'info'); render(); } break;
      case 'endgame': endGame(); break;
      case 'show-command': showCommand(); break;
      case 'ran': connect(); break;
      case 'close': closeDialog(); break;
      case 'live': s.live = !s.live; render(); break;
      case 'check': toast('You have the latest version', 'info'); break;
      case 'toast': toast(el.dataset.msg!, 'info'); closeMenus(); break;
      case 'tour': startTour(); break;
      case 'explore': exploreMode(); break;
      case 'tour-next': advance(); break;
      case 'tour-skip': endTour(); break;
    }
  });
  /** Selection changes in place: re-rendering would replay every card's entrance. */
  function syncSelection() {
    view.querySelectorAll<HTMLElement>('.grid .card').forEach((c) => {
      const on = s.selected.has(c.dataset.game!);
      c.classList.toggle('sel', on);
      const check = c.querySelector('.check');
      if (check) check.textContent = on ? '✓' : '';
    });
    const bar = view.querySelector('.selbar span');
    if (bar) bar.textContent = plural(s.selected.size, 'game') + ' selected';
    const go = view.querySelector<HTMLButtonElement>('[data-act="batch"]');
    if (go) go.disabled = !s.selected.size || !s.connected;
  }
  function endGame() {
    if (!s.playing) return;
    toast(`${s.playing} ended (Steam's Exit game)`, 'info');
    s.playing = null;
    render();
  }
  function viewShot(el: HTMLElement) {
    dialog(`<div class="shotview" style="${el.getAttribute('style')}"><i class="sun"></i><i class="band"></i></div>`
      + `<div class="row"><b>${esc(el.dataset.shot!)}</b><span class="note">Screenshot ${Number(el.dataset.k) + 1} · taken in the headset</span></div>`
      + `<div class="row"><button class="btn-p" data-act="toast" data-msg="Copied to the clipboard (in the app)">Copy image</button>`
      + `<button class="btn-s" data-act="toast" data-msg="Saved to your Pictures folder (in the app)">Download</button><button class="btn-s" data-act="close">Close</button></div>`);
  }

  // remember only the reader's own click: a <details open> that is inserted fires "toggle" by itself
  root.addEventListener('click', (ev) => {
    const sum = (ev.target as HTMLElement).closest('details.cust > summary');
    if (sum) s.custOpen = !(sum.parentElement as HTMLDetailsElement).open;
  }, true);
  root.addEventListener('change', (ev) => {
    const t = ev.target as HTMLInputElement | HTMLSelectElement;
    if (t instanceof HTMLInputElement && t.dataset.patch) { s.patches[t.dataset.patch] = t.checked; toast(`${t.checked ? 'On' : 'Off'}: the game is rebuilt on the next install`, 'info'); }
    if (t instanceof HTMLInputElement && t.matches('[data-details]')) { s.details = t.checked; render(); }
    if (t instanceof HTMLInputElement && t.matches('[data-pick]') && t.files) addFiles(t.files);
    if (t instanceof HTMLSelectElement && t.matches('[data-quality]')) { s.quality = t.value; render(); }
  });
  root.addEventListener('input', (ev) => {
    const t = ev.target as HTMLInputElement;
    if (t.matches('[data-type]')) {
      s.typed = t.value;
      const out = view.querySelector('[data-typed]');
      if (out) out.innerHTML = (esc(s.typed) || '<em>…</em>') + '<i class="caret"></i>';
    }
    if (t.matches('[data-search]')) {
      // filter in place: re-rendering would replay every card's entrance
      s.query = t.value;
      const q = s.query.trim().toLowerCase();
      let shown = 0;
      view.querySelectorAll<HTMLElement>('.grid .card').forEach((c) => {
        const ok = !q || (c.dataset.text ?? '').includes(q);
        c.hidden = !ok;
        if (ok) shown++;
      });
      let none = view.querySelector<HTMLElement>('.none');
      if (!shown && !none) { view.querySelector('.grid')?.insertAdjacentHTML('afterend', '<p class="none"></p>'); none = view.querySelector('.none'); }
      if (none) { none.hidden = shown > 0; none.textContent = `No game matches “${s.query}”.`; }
    }
  });
  root.addEventListener('keydown', (ev) => {
    if ((ev.target as HTMLElement).matches('[data-link-in]') && ev.key === 'Enter') linkGo();
    if (ev.key === 'Escape') { closeMenus(); if (!layer.hidden) closeDialog(); }
  });
  // real files from the desktop: they never leave this tab
  root.addEventListener('dragover', (ev) => {
    if (s.route !== 'files' || !ev.dataTransfer?.types.includes('Files')) return;
    ev.preventDefault();
    view.querySelector('[data-drop]')?.classList.add('over');
  });
  root.addEventListener('dragleave', (ev) => {
    if (!(ev.relatedTarget instanceof Node) || !root.contains(ev.relatedTarget)) view.querySelector('[data-drop]')?.classList.remove('over');
  });
  root.addEventListener('drop', (ev) => {
    if (s.route !== 'files' || !ev.dataTransfer?.files.length) return;
    ev.preventDefault();
    addFiles(ev.dataTransfer.files);
  });

  // ------------------------------------------------------------------ the guided tour
  interface Step { target?: string; alt?: string; text: string; title: string; wait?: string; next?: string; place?: 'center' }
  const STEPS: Step[] = [
    { title: 'You just started FramePort', text: 'The library is empty and no Frame is connected. Set up the Frame first: click <b>Steam Frame</b>.',
      target: '[data-sidebar] [data-route="frame"]', wait: 'route:frame' },
    { title: 'Wi-Fi or a cable', text: 'Pick how the Frame connects (either works), then click <b>Show setup command</b>.',
      target: '[data-act="show-command"]', wait: 'command' },
    { title: 'Type it on the Frame', text: 'On the Frame: SteamVR dashboard → Desktop → Konsole. Type the command, press Enter. Then click the button.',
      target: '.dlg', wait: 'connected' },
    { title: 'Connected', text: 'The Frame card shows the battery, the connection and later what is playing. FramePort finds the Frame again by itself.',
      target: '[data-framecard]', next: 'Next' },
    { title: 'Add your games', text: 'Back to the <b>Library</b>.', target: '[data-sidebar] [data-route="library"]', wait: 'route:library' },
    { title: 'Scan a folder', text: 'Open <b>+ Add games</b> and choose <b>Scan a folder…</b>. FramePort reads each game without changing it.',
      target: '[data-act="add"]', alt: '[data-menu]:not([hidden])', wait: 'scanned' },
    { title: 'Pick a game', text: 'Each game already knows its tested recipe. Open one. (Right-click shows more; <b>Select</b> installs several.)',
      target: '.grid .card', wait: 'route:game' },
    { title: 'One click', text: 'Install on Frame: convert, patch, sign, upload, add to Steam with artwork, launch test. Watch the activity card.',
      target: '[data-act="install"]', wait: 'installed' },
    { title: 'Play', text: 'It is in your Steam library on the Frame now. Start it from here, or in the headset.',
      target: '[data-act="play"]', wait: 'played' },
    { title: "That's the whole setup", text: 'Now try the rest: Monitor, Live view, Screenshots, Files (drop a real file on it), Type on Frame, Settings → themes.',
      place: 'center', next: 'Explore' },
  ];
  let step = -1;

  function startTour() {
    clearTimers();
    $('[data-start]').hidden = true;
    s = fresh();
    applyTheme();
    render();
    step = 0;
    later(coachUpdate, 50);
  }
  function exploreMode() { $('[data-start]').hidden = true; step = -1; coachUpdate(); }
  function endTour() { step = -1; coach.hidden = true; root.querySelectorAll('.hl').forEach((e) => e.classList.remove('hl')); }
  function advance() { if (step < 0) return; step++; if (step >= STEPS.length) return endTour(); coachUpdate(); }
  function emit(event: string) { if (step >= 0 && STEPS[step].wait === event) later(advance, 250); }
  function coachUpdate() {
    root.querySelectorAll('.hl').forEach((e) => e.classList.remove('hl'));
    if (step < 0) { coach.hidden = true; return; }
    const st = STEPS[step];
    const target = (st.alt && root.querySelector<HTMLElement>(st.alt)) || (st.target ? root.querySelector<HTMLElement>(st.target) : null);
    const card = root.querySelector<HTMLElement>('[data-activity]');
    const anchor = st.wait === 'installed' && s.installing
      ? (card && card.getBoundingClientRect().width > 0 ? card : root.querySelector<HTMLElement>('[data-act="installing"]'))
      : target;
    anchor?.classList.add('hl');
    coach.innerHTML = `<p class="cstep">Step ${step + 1} of ${STEPS.length}</p><h4>${st.title}</h4><p>${st.text}</p>`
      + `<div class="crow">${st.next ? `<button class="btn-p" data-act="tour-next">${st.next}</button>` : ''}`
      + `<button class="btn-s" data-act="tour-skip">${step === STEPS.length - 1 ? 'Close' : 'Skip tour'}</button></div>`
      + `<span class="cbar"><i style="width:${((step + 1) / STEPS.length) * 100}%"></i></span>`;
    coach.hidden = false;
    const box = root.getBoundingClientRect();
    const shown = anchor && anchor.getBoundingClientRect().width > 0;
    if (!shown || st.place === 'center') {
      coach.style.left = `${box.width / 2 - coach.offsetWidth / 2}px`;
      coach.style.top = `${box.height / 2 - coach.offsetHeight / 2}px`;
      return;
    }
    const r = anchor!.getBoundingClientRect();
    const w = coach.offsetWidth, hgt = coach.offsetHeight;
    // beside the target if there is room, else above or below it, else docked where it covers the least: never on
    // the target's lower part, where its buttons are
    const top0 = r.top - box.top, bottom0 = r.bottom - box.top;
    let left = r.right - box.left + 14, top = top0;
    if (left + w > box.width - 8) left = r.left - box.left - w - 14;
    if (left < 8) {
      left = Math.max(8, Math.min(box.width - w - 8, r.left - box.left));
      const below = box.height - bottom0 - 12, above = top0 - 12;
      if (below >= hgt + 8) top = bottom0 + 12;
      else if (above >= hgt + 8) top = top0 - hgt - 12;
      else top = 8;
    }
    top = Math.max(8, Math.min(top, box.height - hgt - 8));
    coach.style.left = `${left}px`;
    coach.style.top = `${top}px`;
  }
  new ResizeObserver(() => coachUpdate()).observe(root);

  if (opts.route) s.route = opts.route;
  if (opts.route === 'game') s.current = s.games[0]?.title ?? null;
  if (opts.live) s.live = true;
  applyTheme();
  render();
}
