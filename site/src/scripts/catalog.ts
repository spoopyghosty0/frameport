// The tested-games board's data and rows. Shared by the build (prebuilt rows) and the browser (the live refresh from
// GitHub), so both draw the same thing. Mirrors scripts/compat_list.py (docs/GAMES.md).

export const STATUS = {
  works: { label: 'Works', pill: 'pill-ok' },
  issues: { label: 'With issues', pill: 'pill-warn' },
  unsupported: { label: "Doesn't run", pill: 'pill-error' },
} as const;
export type Status = keyof typeof STATUS;
const ORDER: Record<Status, number> = { works: 0, issues: 1, unsupported: 2 };

export interface Game {
  title: string;
  package: string;
  status: Status;
  platform: string;
  engine: string;
  xr: string;
  note: string;
  checked: string;
  /** what opens under a row: the recipe as the catalog has it */
  details: string;
  patches: string[];
  version: string;
  app: string;
}

/** The note's first sentence, without technical asides (compat_list.short). */
export function short(note = ''): string {
  const first = note.split(/\s+/).join(' ').trim().split(/(?<=[.!?])\s/)[0] ?? '';
  return first.length <= 160 ? first : first.slice(0, 157).trimEnd() + '…';
}

const text = (v: unknown) => (v === undefined || v === null ? '' : String(v));
/** A YAML date can come back as a Date object; we want "YYYY-MM-DD". */
const day = (v: unknown) => (v instanceof Date ? v.toISOString() : text(v)).slice(0, 10);

/** One catalog entry (the parsed YAML) → a board row, or null when it isn't a tested game. */
export function toGame(raw: unknown): Game | null {
  if (!raw || typeof raw !== 'object') return null;
  const d = raw as Record<string, any>;
  if (!(d.status in STATUS) || !d.title) return null;
  const status = d.status as Status;
  return {
    title: text(d.title),
    package: text(d.package),
    status,
    platform: d.kind === 'rift' ? 'PC VR' : 'Quest',
    engine: text(d.engine),
    xr: text(d.xr),
    note: status === 'works' ? '' : short(text(d.notes)),
    checked: day(d.verified?.date ?? d.updated),
    details: [text(d.notes), text(d.details)].filter(Boolean).join(' ').split(/\s+/).join(' ').trim(),
    patches: [
      ...(Array.isArray(d.frame) ? d.frame : []), ...(Array.isArray(d.device) ? d.device : []),
      ...(Array.isArray(d.alt_overport) ? d.alt_overport.map((p: string) => `overport.${p}`) : []),
      ...(Array.isArray(d.overport_extra) ? d.overport_extra.map((p: string) => `overport.${p}`) : []),
      ...(Array.isArray(d.pcvr) ? d.pcvr.map(String) : []),
      ...(d.lepton_env && typeof d.lepton_env === 'object' ? Object.keys(d.lepton_env).map((k) => `env.${k}`) : []),
      ...(d.adapter && typeof d.adapter === 'object' ? Object.keys(d.adapter).map((k) => `adapter.${k}`) : []),
    ].map(String),
    version: text(d.tested_version),
    app: text(d.verified?.app),
  };
}

export function sortGames(games: Game[]): Game[] {
  return [...games].sort((a, b) => ORDER[a.status] - ORDER[b.status]
    || a.title.localeCompare(b.title, 'en', { sensitivity: 'base' }));
}

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

export function rowHtml(g: Game, showPackage = false): string {
  const s = STATUS[g.status];
  const facts = [g.version && `tested with version ${g.version}`, g.app && `checked with FramePort ${g.app}`, g.checked]
    .filter(Boolean).join(' · ');
  const more = `<div class="more" role="cell" hidden>`
    + (g.details ? `<p>${esc(g.details)}</p>` : '<p>Runs with the default patches; nothing special needed.</p>')
    + (g.patches.length ? `<p class="pt"><span>Recipe</span>${g.patches.map((p) => `<code>${esc(p)}</code>`).join('')}</p>` : '')
    + (facts ? `<p class="fx">${esc(facts)}</p>` : '') + '</div>';
  return `<li class="row" role="row" tabindex="0" aria-expanded="false" data-status="${g.status}" data-engine="${esc(g.engine)}" `
    + `data-text="${esc(`${g.title} ${g.engine} ${g.xr} ${g.platform} ${g.patches.join(' ')}`.toLowerCase())}">`
    + `<span class="c-game" role="cell"><span class="title">${esc(g.title)}</span>`
    + (g.platform !== 'Quest' ? `<span class="tag">${esc(g.platform)}</span>` : '')
    + (showPackage && g.package ? `<code class="pkg">${esc(g.package)}</code>` : '')
    + (g.note ? `<span class="note">${esc(g.note)}</span>` : '')
    + `</span><span class="c-eng" role="cell">${esc(g.engine)}</span>`
    + `<span class="c-xr" role="cell">${esc(g.xr)}</span>`
    + `<span role="cell"><span class="pill ${s.pill}">${s.label}</span></span>`
    + `<span class="c-date" role="cell">${esc(g.checked)}</span>${more}</li>`;
}

/** All rows; games that share a title and platform (e.g. two builds) show their package to tell them apart. */
export function rowsHtml(games: Game[]): string {
  const seen = new Map<string, number>();
  const key = (g: Game) => `${g.title.toLowerCase()}|${g.platform}`;
  for (const g of games) seen.set(key(g), (seen.get(key(g)) ?? 0) + 1);
  return games.map((g) => rowHtml(g, seen.get(key(g))! > 1)).join('');
}

export function counts(games: Game[]): Record<Status, number> {
  const c = { works: 0, issues: 0, unsupported: 0 };
  for (const g of games) c[g.status]++;
  return c;
}

export function engines(games: Game[]): string[] {
  const n = new Map<string, number>();
  for (const g of games) if (g.engine) n.set(g.engine, (n.get(g.engine) ?? 0) + 1);
  return [...n.keys()].sort((a, b) => n.get(b)! - n.get(a)! || a.localeCompare(b));
}

export const latestCheck = (games: Game[]) => games.map((g) => g.checked).filter(Boolean).sort().at(-1) ?? '';

/** What the build embeds: each catalog file's git blob sha and its row (null = not a tested game). */
export interface Snapshot { builtAt: string; files: Record<string, { sha: string; game: Game | null }> }
