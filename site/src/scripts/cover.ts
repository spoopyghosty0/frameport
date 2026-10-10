// Generated cover art for a game, in the spirit of the app's placeholder Steam art (artwork/steam.py: the name on a
// colour picked from the title): no third-party artwork on the site.

/** A stable 0..359 hue for a title (FNV-1a). */
export function hue(title: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < title.length; i++) {
    h ^= title.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h % 360;
}

/** A second number from the same title, for the shapes' layout. */
export function seed(title: string): number {
  return hue(title.split('').reverse().join('') + '·');
}

/** Initials for the generated app icon: first letters of the first two words ("Pistol Whip" → "PW"). */
export function initials(title: string): string {
  const words = title.replace(/[^\p{L}\p{N} ]/gu, ' ').split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  if (words.length === 1) {
    const caps = words[0].match(/\p{Lu}/gu) ?? [];  // "BattleGlide" → "BG"
    return (caps.length >= 2 && caps.length < words[0].length ? caps.slice(0, 2).join('') : words[0].slice(0, 2)).toUpperCase();
  }
  return (words[0][0] + words[1][0]).toUpperCase();
}

/**
 * The cover title's size in cqi (container-relative), so the longest word fits on one line and the title takes at
 * most three lines: words are never broken.
 */
export function titleSize(title: string, inner = 24, max = 4.6, min = 2.2): number {
  const words = title.split(/\s+/);
  const longest = Math.max(...words.map((w) => w.length), 1);
  const byWord = inner / (longest * 0.62);
  const byLines = (inner * 3) / (title.length * 0.6);
  return Math.max(min, Math.min(max, byWord, byLines));
}

/** The file name the APK side shows: the package's last part, as a file. */
export function apkName(pkg: string, title: string): string {
  const last = pkg.split('.').filter(Boolean).pop() || title.replace(/\s+/g, '');
  return `${last}.apk`;
}

/** CSS custom properties for one cover. */
export function coverVars(title: string): string {
  const h = hue(title);
  const s = seed(title);
  return `--h:${h};--h2:${(h + 40 + (s % 80)) % 360};--sx:${20 + (s % 60)}%;--sy:${15 + ((s >> 3) % 40)}%;`
    + `--rot:${(s % 50) - 25}deg;--ts:${titleSize(title).toFixed(2)}cqi`;
}
