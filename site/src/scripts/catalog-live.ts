// The board's live refresh: the catalog on GitHub's main branch, like the app's catalog.refresh_remote. One API call
// lists catalog/games with each file's blob sha; only files that differ from the build's copy (or from this
// browser's cache) are downloaded from raw.githubusercontent.com. Kept for 6 h in localStorage. Any failure leaves
// the prebuilt board as it is.
import { toGame, type Game, type Snapshot } from './catalog';

const LIST = 'https://api.github.com/repos/spoopyghosty0/frameport/contents/catalog/games?ref=main';
const KEY = 'frameport.catalog.v1';
const TTL = 6 * 3600 * 1000;

type Files = Snapshot['files'];
interface Cache { at: number; files: Files }
interface Listed { name: string; sha: string; type: string; download_url: string | null }

function readCache(): Cache | null {
  try {
    const c = JSON.parse(localStorage.getItem(KEY) || 'null');
    return c && typeof c.at === 'number' && c.files ? c : null;
  } catch { return null; }
}
function writeCache(files: Files) {
  try { localStorage.setItem(KEY, JSON.stringify({ at: Date.now(), files })); } catch { /* private mode, full */ }
}

async function get(url: string, ms = 10000): Promise<Response> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(url, { signal: ctl.signal, headers: url.startsWith('https://api.github.com/') ? { Accept: 'application/vnd.github+json' } : {} });
    if (!r.ok) throw new Error(`${r.status} ${url}`);
    return r;
  } finally { clearTimeout(timer); }
}

const games = (files: Files): Game[] => Object.values(files).map((f) => f.game).filter((g): g is Game => !!g);
const same = (a: Files, b: Files) => {
  const ka = Object.keys(a), kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => b[k]?.sha === a[k].sha);
};

export interface LiveResult { games: Game[]; changed: boolean; at: number }

export async function refreshCatalog(snapshot: Snapshot): Promise<LiveResult | null> {
  const cache = readCache();
  if (cache && Date.now() - cache.at < TTL) {
    return { games: games(cache.files), changed: !same(cache.files, snapshot.files), at: cache.at };
  }
  let listed: Listed[];
  try {
    listed = (await (await get(LIST)).json() as Listed[]).filter((f) => f.type === 'file' && f.name.endsWith('.yaml'));
  } catch {
    // offline or rate-limited (60 calls an hour per address): an older cache still beats the build
    return cache ? { games: games(cache.files), changed: !same(cache.files, snapshot.files), at: cache.at } : null;
  }

  const known: Files = { ...snapshot.files, ...(cache?.files ?? {}) };
  const files: Files = {};
  const todo: Listed[] = [];
  for (const f of listed) {
    if (known[f.name]?.sha === f.sha) files[f.name] = known[f.name];
    else if (f.download_url) todo.push(f);
  }
  if (todo.length) {
    const { parse } = await import('yaml');
    await Promise.all(todo.map(async (f) => {
      try {
        files[f.name] = { sha: f.sha, game: toGame(parse(await (await get(f.download_url!)).text())) };
      } catch {
        if (known[f.name]) files[f.name] = known[f.name]; // keep the older copy of a file that didn't come
      }
    }));
  }
  writeCache(files);
  return { games: games(files), changed: !same(files, snapshot.files), at: Date.now() };
}
