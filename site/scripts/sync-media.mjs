// Copies the showcase renders (docs/images, docs/media) and the install badges (docs/badges) into the site before a
// build. They are rendered by scripts/showcase in CI and never by hand, so the site always shows the current ones.
// Screenshots go to src/assets/media (Astro optimizes them), videos and badges to public/media (served as they are).
import { spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const site = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repo = resolve(site, '..');

const SHOTS = ['library.png', 'game.png', 'monitor.png', 'live-view.png', 'files.png', 'type-on-frame.png',
  'screenshots.png', 'install-progress.png', 'patches.png'];
const PUBLIC = {
  'docs/images': ['tour-teaser.webp'],
  'docs/media': ['frameport-tour.mp4', 'frameport-tour.jpg', 'frameport-install.mp4', 'frameport-install.jpg'],
  'docs/badges': ['install-with-frameport.svg', 'install-with-frameport-animated.svg', 'install-with-frameport-hover.svg',
    'install-with-frameport@2x.png'],
};

function copy(from, to) {
  if (!existsSync(from)) throw new Error(`missing ${from}`);
  mkdirSync(dirname(to), { recursive: true });
  copyFileSync(from, to);
}

for (const name of SHOTS) copy(join(repo, 'docs/images', name), join(site, 'src/assets/media', name));
// sharper copies of the videos (record_video.py --size 2560x1440, in site/media-hq) win over the docs ones
const HQ = join(site, 'media-hq');
for (const [dir, names] of Object.entries(PUBLIC)) {
  for (const name of names) {
    const hq = join(HQ, name);
    copy(dir === 'docs/media' && existsSync(hq) ? hq : join(repo, dir, name), join(site, 'public/media', name));
  }
}
// short silent loops of each video for the video tiles (light: they play while on screen); needs ffmpeg, else the
// tiles show their poster
for (const [id, from] of [['tour', 18], ['install', 28]]) {
  const src = join(site, 'public/media', `frameport-${id}.mp4`);
  const out = join(site, 'public/media', `preview-${id}.mp4`);
  const r = spawnSync('ffmpeg', ['-v', 'error', '-y', '-ss', String(from), '-t', '14', '-i', src, '-an',
    '-vf', 'fps=24,scale=960:-2:flags=lanczos', '-c:v', 'libx264', '-preset', 'slow', '-crf', '28', '-pix_fmt', 'yuv420p',
    '-movflags', '+faststart', out]);
  console.log(r.status === 0 ? `preview ${id}` : `no preview for ${id} (ffmpeg missing or failed)`);
}

// the setup URL: bootstrap/setup.sh (tested with the app) served as /s and /setup.sh, one source for both
copy(join(repo, 'bootstrap/setup.sh'), join(site, 'public/s'));
copy(join(repo, 'bootstrap/setup.sh'), join(site, 'public/setup.sh'));

// every image the docs pages show (docs/images → media/docs)
let docImages = 0;
for (const name of readdirSync(join(repo, 'docs/images'))) {
  if (!/\.(png|jpe?g|webp|gif|svg)$/i.test(name)) continue;
  copy(join(repo, 'docs/images', name), join(site, 'public/media/docs', name));
  docImages++;
}
console.log(`synced ${docImages} doc images`);
console.log(`synced ${SHOTS.length} screenshots and ${Object.values(PUBLIC).flat().length} media files`);
