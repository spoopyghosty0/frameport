// Copies the showcase renders (docs/images, docs/media) and the install badges (docs/badges) into the site before a
// build. They are rendered by scripts/showcase in CI and never by hand, so the site always shows the current ones.
// Screenshots go to src/assets/media (Astro optimizes them), videos and badges to public/media (served as they are).
import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const site = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repo = resolve(site, '..');

const SHOTS = ['library.png', 'game.png', 'monitor.png', 'live-view.png', 'files.png', 'type-on-frame.png',
  'screenshots.png', 'install-progress.png', 'patches.png'];
const PUBLIC = {
  'docs/images': ['tour-teaser.webp'],
  'docs/media': ['frameport-tour.mp4', 'frameport-tour.jpg', 'frameport-install.mp4', 'frameport-install.jpg'],
  'docs/badges': ['install-with-frameport.svg', 'install-with-frameport-animated.svg', 'install-with-frameport@2x.png'],
};

function copy(from, to) {
  if (!existsSync(from)) throw new Error(`missing ${from}`);
  mkdirSync(dirname(to), { recursive: true });
  copyFileSync(from, to);
}

for (const name of SHOTS) copy(join(repo, 'docs/images', name), join(site, 'src/assets/media', name));
for (const [dir, names] of Object.entries(PUBLIC)) {
  for (const name of names) copy(join(repo, dir, name), join(site, 'public/media', name));
}
console.log(`synced ${SHOTS.length} screenshots and ${Object.values(PUBLIC).flat().length} media files`);
