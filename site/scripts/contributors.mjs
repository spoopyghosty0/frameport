// Everyone who contributed to FramePort on GitHub: code (the contributors list) and issues / pull requests (their
// authors). Writes src/data/contributors.json. Runs before each build; without network (or over the rate limit) the
// committed file stays as it is, so the site still builds. GITHUB_TOKEN (set in CI) raises the limit.
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = 'spoopyghosty0/frameport';
const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'data', 'contributors.json');
const headers = { Accept: 'application/vnd.github+json', 'User-Agent': 'frameport-site' };
if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;

async function pages(path) {
  const all = [];
  for (let page = 1; page <= 30; page++) {
    const r = await fetch(`https://api.github.com/repos/${REPO}/${path}${path.includes('?') ? '&' : '?'}per_page=100&page=${page}`,
      { headers, signal: AbortSignal.timeout(15000) });
    if (!r.ok) throw new Error(`${r.status} ${path}`);
    const items = await r.json();
    all.push(...items);
    if (items.length < 100) break;
  }
  return all;
}

const isBot = (u) => !u || u.type === 'Bot' || u.login.endsWith('[bot]');

try {
  const people = new Map();
  const person = (u) => {
    if (!people.has(u.login)) {
      people.set(u.login, { login: u.login, avatar: u.avatar_url, url: u.html_url, commits: 0, issues: 0, prs: 0 });
    }
    return people.get(u.login);
  };
  for (const c of await pages('contributors')) if (!isBot(c)) person(c).commits += c.contributions;
  for (const i of await pages('issues?state=all')) {
    if (isBot(i.user)) continue;
    if (i.pull_request) person(i.user).prs++;
    else person(i.user).issues++;
  }
  const list = [...people.values()]
    .map((p) => ({ ...p, total: p.commits + p.issues * 3 + p.prs * 5 }))
    .sort((a, b) => b.total - a.total || a.login.localeCompare(b.login));
  const data = { updated: new Date().toISOString().slice(0, 10), people: list };
  writeFileSync(OUT, JSON.stringify(data, null, 1) + '\n');
  console.log(`contributors: ${list.length} people`);
} catch (e) {
  let kept = 0;
  try { kept = JSON.parse(readFileSync(OUT, 'utf8')).people.length; } catch { /* none yet */ }
  console.warn(`contributors: kept the saved list (${kept} people): ${e.message}`);
}
