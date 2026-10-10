// The documentation pages: some of docs/*.md, rendered at build time. Links between these docs become site links
// (anchors kept: headings get GitHub's ids), other repo links go to GitHub, images come from the site's media.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Marked, type Tokens } from 'marked';
import { REPO, url } from './site';

export interface DocInfo { file: string; slug: string; name: string; group: string; blurb: string }
export const DOCS: DocInfo[] = [
  { file: 'INSTALL.md', slug: 'install', name: 'Install and first steps', group: 'Get started', blurb: 'Download, connect the Frame, add and install games.' },
  { file: 'FRAME_SETUP.md', slug: 'frame-setup', name: 'What the setup changes', group: 'Get started', blurb: 'What the setup command changes on the Frame, and how to undo it.' },
  { file: 'FAQ.md', slug: 'faq', name: 'FAQ', group: 'Get started', blurb: 'Folders, OBB data, and the questions people ask most.' },
  { file: 'COMPATIBILITY.md', slug: 'compatibility', name: 'Compatibility', group: 'Using FramePort', blurb: 'What runs on the Frame, and why some games can’t.' },
  { file: 'THEMES.md', slug: 'themes', name: 'Themes', group: 'Using FramePort', blurb: 'Colour themes, and how to make your own.' },
  { file: 'DIAGNOSTICS.md', slug: 'diagnostics', name: 'Diagnostics and problem reports', group: 'Using FramePort', blurb: 'Reporting a problem without sharing personal data.' },
  { file: 'INSTALL_BUTTON.md', slug: 'install-button', name: 'Install button', group: 'For developers', blurb: 'An “Install with FramePort” button for your download page.' },
  { file: 'ARCHITECTURE.md', slug: 'architecture', name: 'Architecture', group: 'For developers', blurb: 'How FramePort is put together, and what it adds itself.' },
  { file: 'PLAYBOOK.md', slug: 'playbook', name: 'Playbook: symptoms and fixes', group: 'For developers', blurb: 'Symptoms and fixes from porting real games.' },
  { file: 'FRAME_RUNTIME.md', slug: 'frame-runtime', name: 'Frame runtime facts', group: 'For developers', blurb: 'Facts about the Steam Frame’s runtime, learned the hard way.' },
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
  const first = md.split(/\n\s*\n/).map((p) => p.trim()).find((p) => p && !p.startsWith('#') && !p.startsWith('!') && !p.startsWith('<') && !p.startsWith('```')) ?? info.blurb;
  const description = first.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/[*_`>]/g, '').replace(/\s+/g, ' ').slice(0, 200);
  return { ...info, title, html, toc, description };
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
