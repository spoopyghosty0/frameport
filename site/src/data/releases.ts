// FramePort's releases for the "What's new" page and the home page's section. scripts/releases.mjs saves GitHub's
// list in releases.json before each build; src/scripts/releasenotes.ts picks what changed from each release's notes.
import data from './releases.json';
import { REPO } from './site';
import { newer, toRelease, type RawRelease, type Release } from '../scripts/releasenotes';

export type { Release };

const all = (data.releases as RawRelease[]).map((r) => toRelease(r, REPO)).filter((r): r is Release => !!r);
/** Stable releases, newest first. */
export const RELEASES = all.filter((r) => !r.dev).sort((a, b) => (newer(a.version, b.version) ? -1 : 1));
export const LATEST: Release | undefined = RELEASES[0];
/** The dev build, when it is ahead of the latest release. */
export const NEXT: Release | undefined = all.find((r) => r.dev && (!LATEST || newer(r.version, LATEST.version)));
export const UPDATED: string = data.updated;
