// FramePort's GitHub releases for the "What's new" page: published stable releases plus the rolling `dev` build.
// Writes src/data/releases.json with the raw notes (src/data/releases.ts picks the "What's new" part). Runs before
// each build; without network (or over the rate limit) the committed file stays as it is, so the site still builds.
// GITHUB_TOKEN (set in CI) raises the limit. The pages workflow also runs when a release is published.
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = 'spoopyghosty0/frameport';
const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'data', 'releases.json');
const headers = { Accept: 'application/vnd.github+json', 'User-Agent': 'frameport-site' };
if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;

try {
  const all = [];
  for (let page = 1; page <= 5; page++) {
    const r = await fetch(`https://api.github.com/repos/${REPO}/releases?per_page=100&page=${page}`,
      { headers, signal: AbortSignal.timeout(15000) });
    if (!r.ok) throw new Error(`${r.status} releases`);
    const items = await r.json();
    all.push(...items);
    if (items.length < 100) break;
  }
  const releases = all
    .filter((r) => !r.draft && r.tag_name && (!r.prerelease || r.tag_name === 'dev'))
    .map((r) => ({
      tag: r.tag_name,
      name: r.name || r.tag_name,
      date: (r.published_at || r.created_at || '').slice(0, 10),
      url: r.html_url,
      dev: r.tag_name === 'dev',
      notes: (r.body || '').replace(/\r\n/g, '\n'),
    }));
  writeFileSync(OUT, JSON.stringify({ updated: new Date().toISOString().slice(0, 10), releases }, null, 1) + '\n');
  console.log(`releases: ${releases.length}`);
} catch (e) {
  let kept = 0;
  try { kept = JSON.parse(readFileSync(OUT, 'utf8')).releases.length; } catch { /* none yet */ }
  console.warn(`releases: kept the saved list (${kept}): ${e.message}`);
}
