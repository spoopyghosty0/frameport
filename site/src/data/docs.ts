// The documentation pages: some of docs/*.md, rendered at build time. Links between these docs become site links
// (anchors kept: headings get GitHub's ids), other repo links go to GitHub, images come from the site's media.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Marked, type Tokens } from 'marked';
import { REPO, url } from './site';
import { fileTree } from '../scripts/filetree';

export interface DocInfo { file: string; slug: string; name: string; group: string; blurb: string; summary: string }
export const DOCS: DocInfo[] = [
  { file: 'INSTALL.md', slug: 'install', name: 'Install and first steps', group: 'Get started', blurb: 'Download, connect the Frame, install games.',
    summary: 'Install FramePort on Windows, macOS or Linux, connect your Steam Frame once, then install and play Quest and PC VR games.' },
  { file: 'FRAME_SETUP.md', slug: 'frame-setup', name: 'What the setup changes', group: 'Get started', blurb: 'Changes on the Frame, firewalls, undoing it.',
    summary: "What FramePort's one-time setup changes on the Steam Frame, how to undo it, and what your network and firewall need." },
  { file: 'FAQ.md', slug: 'faq', name: 'FAQ', group: 'Get started', blurb: 'Game folders and common questions.',
    summary: "Common questions about FramePort: how to lay out game folders with APK and OBB files, and what to do when a game doesn't run." },
  { file: 'COMPATIBILITY.md', slug: 'compatibility', name: 'Compatibility', group: 'Using FramePort', blurb: 'What runs on the Frame.',
    summary: 'Which Quest games, PC VR games and Android apps run on the Steam Frame with FramePort, and how to share a recipe that works.' },
  { file: 'THEMES.md', slug: 'themes', name: 'Themes', group: 'Using FramePort', blurb: 'Color themes and making your own.',
    summary: "FramePort's dark color themes (Portal, Portal OLED, Original) and how to make and share your own theme file." },
  { file: 'INSTALL_BUTTON.md', slug: 'install-button', name: 'Install button', group: 'For developers', blurb: 'An install button for your download page.',
    summary: 'Add an Install with FramePort button to your download page or README, so one click installs your game on a Steam Frame.' },
  { file: 'DIAGNOSTICS.md', slug: 'diagnostics', name: 'Diagnostics and problem reports', group: 'For developers', blurb: 'What a problem report contains.',
    summary: "What FramePort's problem reports and shared recipes contain, how personal data is removed, and how recipes reach the catalog." },
  { file: 'ARCHITECTURE.md', slug: 'architecture', name: 'Architecture', group: 'For developers', blurb: 'How FramePort is built and what it adds.',
    summary: 'How FramePort is built: the projects it wraps, the pipeline from scan to launch test, and what it adds for the Steam Frame.' },
  { file: 'PLAYBOOK.md', slug: 'playbook', name: 'Porting playbook', group: 'For developers', blurb: 'Symptoms and fixes from real games.',
    summary: 'Symptoms, causes and fixes from porting real Quest and PC VR games to the Steam Frame: graphics, input, audio and startup.' },
  { file: 'FRAME_RUNTIME.md', slug: 'frame-runtime', name: 'Steam Frame runtime reference', group: 'For developers', blurb: 'Facts about the Frame\u2019s runtime.',
    summary: "Technical facts about the Steam Frame's runtime: OpenXR, graphics, Lepton (Android), Proton, Steam and what games can use." },
];
const BY_FILE = new Map(DOCS.map((d) => [d.file, d]));
const ROOT = join(process.cwd(), '..');

/** GitHub's heading ids: lowercase, punctuation dropped, spaces to dashes, numbered when repeated. */
export function slugger() {
  const seen = new Map<string, number>();
  return (text: string) => {
    const base = text.toLowerCase().trim().replace(/<[^>]+>/g, '').replace(/[^\p{L}\p{N}\s_-]/gu, '').replace(/\s/g, '-');
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    return n ? `${base}-${n}` : base;
  };
}

/** A link in docs/<file> → where it points on the site (or on GitHub). */
export function rewrite(href: string): string {
  if (!href || /^(https?:|mailto:|#)/.test(href)) return href;
  const [path, hash] = href.split('#');
  const doc = BY_FILE.get(path.replace(/^\.\//, ''));
  if (doc) return url(`docs/${doc.slug}/`) + (hash ? `#${hash}` : '');
  const m = path.match(/^(?:\.\/)?(images|media|badges)\/(.+)$/);
  if (m) return url(m[1] === 'images' ? `media/docs/${m[2]}` : `media/${m[2]}`);
  // anything else in the repo: on GitHub (docs/ is the base)
  const target = path.startsWith('../') ? path.slice(3) : `docs/${path.replace(/^\.\//, '')}`;
  return `${REPO}/blob/main/${target}${hash ? `#${hash}` : ''}`;
}

/** A description that fits a search result (about 155 characters), cut at a word. */
export function clip(text: string, max = 155): string {
  if (text.length <= max) return text;
  const cut = text.slice(0, max - 1);
  return `${cut.slice(0, Math.max(cut.lastIndexOf(' '), max / 2)).replace(/[\s,;:.(-]+$/, '')}…`;
}

export interface Doc extends DocInfo { title: string; html: string; toc: { id: string; text: string; depth: number }[]; description: string }

export function loadDoc(info: DocInfo): Doc {
  let md = readFileSync(join(ROOT, 'docs', info.file), 'utf8');
  const h1 = md.match(/^#\s+(.+)$/m);
  const title = h1 ? h1[1].trim() : info.file.replace(/\.md$/, '');
  if (h1) md = md.replace(h1[0], '');
  const slug = slugger();
  const toc: Doc['toc'] = [];
  const marked = new Marked({
    gfm: true,
    walkTokens(token) {
      if (token.type === 'link' || token.type === 'image') {
        const link = token as Tokens.Link;
        // a link that only names a file ("INSTALL.md") gets that doc's name when the doc is on the site
        const named = BY_FILE.get(link.href.split('#')[0].replace(/^\.\//, ''));
        if (token.type === 'link' && named && /^[\w.-]+\.md(#\S*)?$/.test(link.text)) {
          link.text = named.name;
          link.tokens = [{ type: 'text', raw: named.name, text: named.name }];
        }
        link.href = rewrite(link.href);
      }
      if (token.type === 'html') {
        (token as Tokens.HTML).text = (token as Tokens.HTML).text.replace(/(src|href)="([^"]+)"/g, (_, attr, v) => `${attr}="${rewrite(v)}"`);
      }
    },
    renderer: {
      // ```tree blocks (folder layouts) become a real file tree; every other code block stays as it is
      code({ text, lang }) {
        return lang === 'tree' ? fileTree(text) : false;
      },
      // diagrams (SVG) keep a readable size on phones: they scroll sideways there (and still open large on a tap)
      image({ href, text }) {
        if (!/\.svg$/i.test(href)) return false;
        const alt = text.replace(/"/g, '&quot;');
        return `<figure class="diagram"><div class="diagram-scroll"><img src="${href}" alt="${alt}" loading="lazy" /></div>`
          + '<figcaption>Scroll sideways or tap to enlarge.</figcaption></figure>';
      },
      heading({ tokens, depth, text }) {
        const id = slug(text);
        if (depth <= 3) toc.push({ id, text: text.replace(/[`*_]/g, ''), depth });
        return `<h${depth} id="${id}"><a class="anchor" href="#${id}" aria-label="Link to this section">#</a>${this.parser.parseInline(tokens)}</h${depth}>\n`;
      },
      table(token) {
        const head = token.header.map((c) => `<th>${this.parser.parseInline(c.tokens)}</th>`).join('');
        const rows = token.rows.map((r) => `<tr>${r.map((c) => `<td>${this.parser.parseInline(c.tokens)}</td>`).join('')}</tr>`).join('');
        return `<div class="tbl"><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>\n`;
      },
    },
  });
  const html = marked.parse(md, { async: false }) as string;
  const blocks = md.split(/\n\s*\n/).map((p) => p.trim());
  const at = blocks.findIndex((p) => p && !p.startsWith('#') && !p.startsWith('!') && !p.startsWith('<') && !p.startsWith('```'));
  let first = at >= 0 ? blocks[at] : info.blurb;
  // "… three themes:" introduces a list: name its items, so the description says something on its own
  if (first.endsWith(':') && /^[-*\d]/.test(blocks[at + 1] ?? '')) {
    const items = blocks[at + 1].split('\n').filter((l) => /^([-*]|\d+\.)\s/.test(l))
      .map((l) => l.replace(/^([-*]|\d+\.)\s+/, '').split(/:\s|\s—\s|\s\(/)[0].replace(/[*`]/g, '').trim());
    first = `${first} ${items.join(', ')}.`;
  }
  const description = clip(first.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/[*_`>|]/g, '').replace(/\s+/g, ' ').trim());
  return { ...info, title, html, toc, description: info.summary || description };
}

/** Words → minutes at an unhurried 220 words a minute (tables and code count too: they're what people read here). */
export const minutes = (doc: Doc) => Math.max(1, Math.round(plain(doc.html).split(/\s+/).length / 220));

const ENTITIES: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'", nbsp: ' ' };
/** HTML → text for the search index (good enough for our own rendered markdown). */
export function plain(html: string): string {
  return html.replace(/<a class="anchor"[^>]*>#<\/a>/g, '').replace(/<[^>]+>/g, ' ')
    .replace(/&(#?\w+);/g, (m, e) => ENTITIES[e] ?? (e.startsWith('#') ? String.fromCodePoint(Number(e.slice(1))) : m))
    .replace(/\s+/g, ' ').trim();
}

export interface Section { slug: string; doc: string; id: string; heading: string; text: string }
/** One search entry per h2/h3 section (the part before the first heading belongs to the page itself). */
export function sections(doc: Doc): Section[] {
  return doc.html.split(/(?=<h[23] id=")/).map((p) => {
    const m = p.match(/^<h[23] id="([^"]+)">([\s\S]*?)<\/h[23]>/);
    return { slug: doc.slug, doc: doc.title, id: m ? m[1] : '', heading: m ? plain(m[2]) : doc.title, text: plain(m ? p.slice(m[0].length) : p) };
  }).filter((s) => s.text || s.id);
}
