// What changed in a FramePort release, from its GitHub notes: the parts the app's changelog shows
// (src/frameport/updates.py whats_new): the "What's new" section, else the notes without what CI and older releases
// add to every release (the intro line, the update hint, install and download help). Used at build time.
import { marked } from 'marked';

export interface RawRelease { tag: string; name: string; date: string; url: string; dev: boolean; notes: string }

export interface Release {
  tag: string;
  version: string;
  date: string;
  url: string;
  dev: boolean;
  /** x.y.0 adds features; x.y.z (z > 0) mostly fixes */
  kind: 'feature' | 'fix' | 'dev';
  html: string;
  /** the first changes as plain text, for short lists */
  highlights: string[];
  changes: number;
}

// sections that aren't about what changed
const HELP = /^(install|first launch|verify a download|uninstalling|updating|connecting the steam frame|command line|sharing a working game, reporting a problem|changes since .*)$/i;
const BOILERPLATE = [/^FramePort v?\S+: Windows.*$/gm, /^Already have FramePort\?.*$/gm, /^Dev build \*\*[^]*?(?=\n\n|\n#|$)/gm];

/** The notes as [heading, text] parts; the text before the first heading has heading ''. */
function sections(notes: string): [string, string][] {
  const out: [string, string][] = [['', '']];
  for (const line of notes.replace(/\r\n/g, '\n').split('\n')) {
    const m = /^#{1,3}\s+(.+?)\s*$/.exec(line);
    if (m) out.push([m[1], '']);
    else out[out.length - 1][1] += line + '\n';
  }
  return out.map(([h, t]) => [h, t.trim()] as [string, string]).filter(([h, t]) => h || t);
}

/** What changed, as Markdown: "What's new" alone when the notes have it (dev builds: "Please test"). */
export function whatsNew(notes: string, dev = false): string {
  const parts = sections(notes);
  const own = parts.find(([h]) => (dev ? /^please test$/i : /^what'?s new$/i).test(h));
  let md = own ? own[1]
    : parts.filter(([h]) => !HELP.test(h)).map(([h, t]) => (h ? `#### ${h}\n\n${t}` : t)).join('\n\n');
  for (const re of BOILERPLATE) md = md.replace(re, '');
  return md.replace(/\n{3,}/g, '\n\n').trim();
}

/** "#48" → a link to that issue or pull request (outside code spans). */
export function linkIssues(md: string, repo: string): string {
  return md.split(/(`[^`]*`)/).map((part, i) => (i % 2 ? part
    : part.replace(/(^|[\s(])#(\d{1,5})\b/g, (_, pre, n) => `${pre}[#${n}](${repo}/issues/${n})`))).join('');
}

/** The Markdown's list items as plain text. */
export function plainItems(md: string): string[] {
  return md.split('\n').filter((l) => /^[-*]\s+/.test(l)).map((l) => l.replace(/^[-*]\s+/, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[*`]/g, '').trim());
}

export function render(md: string, repo: string): string {
  const html = marked.parse(linkIssues(md, repo), { async: false, gfm: true }) as string;
  // list items that round up fixes ("Fixes: …") get a tag of their own
  return html.replace(/<li>(?:<p>)?Fix(?:es)?:\s*/g, (m) => `${m.startsWith('<li><p>') ? '<li class="fixes"><p>' : '<li class="fixes">'}<span class="tag">Fixes</span> `)
    .replace(/<a href="(https?:[^"]*)"/g, '<a rel="noopener" href="$1"')
    .replace(/<h4>/g, '<h4 class="part">');
}

const VERSION = /(\d+\.\d+\.\d+(?:\.dev\d+)?)/;

export function toRelease(r: RawRelease, repo: string): Release | null {
  const version = VERSION.exec(r.dev ? r.name : r.tag)?.[1];
  if (!version) return null;
  const md = whatsNew(r.notes, r.dev) || (r.dev ? '' : 'The first release.');
  const items = plainItems(md);
  return {
    tag: r.tag, version, date: r.date, url: r.url, dev: r.dev,
    kind: r.dev ? 'dev' : /\.0$/.test(version) ? 'feature' : 'fix',
    html: render(md, repo), highlights: items.slice(0, 4), changes: items.length,
  };
}

/** Whether version a is newer than b; a release is newer than its own dev builds (0.12.1 > 0.12.1.dev9). */
export function newer(a: string, b: string): boolean {
  const parse = (v: string) => v.split(/\.(?:dev)?/).map(Number);
  const x = parse(a), y = parse(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const p = x[i] ?? Infinity, q = y[i] ?? Infinity;
    if (p !== q) return p > q;
  }
  return false;
}
