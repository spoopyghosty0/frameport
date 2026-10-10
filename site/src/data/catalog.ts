// Build time: reads catalog/games/*.yaml from this checkout. Each file's git blob sha goes along, so the browser can
// tell which files changed on GitHub since the build and fetch only those.
import { createHash } from 'node:crypto';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { parse } from 'yaml';
import { toGame, type Snapshot } from '../scripts/catalog';

const DIR = join(process.cwd(), '..', 'catalog', 'games');

const blobSha = (data: Buffer) =>
  createHash('sha1').update(`blob ${data.length}\0`).update(data).digest('hex');

export function loadSnapshot(): Snapshot {
  const files: Snapshot['files'] = {};
  for (const name of readdirSync(DIR).filter((n) => n.endsWith('.yaml')).sort()) {
    const data = readFileSync(join(DIR, name));
    files[name] = { sha: blobSha(data), game: toGame(parse(data.toString('utf8'))) };
  }
  return { builtAt: new Date().toISOString().slice(0, 10), files };
}
