// A ```tree block in the docs (a `tree`-style listing, "← note" after an entry) as a real file tree on the site:
// folders and files with icons, guide lines and notes. GitHub shows the same block as plain text.

interface Node { name: string; note: string; depth: number; children: Node[] }

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const FOLDER = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4.6l2 2h8.4A1.5 1.5 0 0 1 21 9.5v8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z"/></svg>';
const FILE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 3h7.5l4.5 4.5v12A1.5 1.5 0 0 1 17 21H6.5A1.5 1.5 0 0 1 5 19.5v-15A1.5 1.5 0 0 1 6.5 3z"/><path d="M13.5 3v5h5"/></svg>';

/** The listing's entries with their depth (one level = 4 characters of ├── / │   / └── / spaces). */
export function parseTree(text: string): Node[] {
  const roots: Node[] = [];
  const stack: Node[] = [];
  for (const raw of text.replace(/\s+$/, '').split('\n')) {
    if (!raw.trim()) continue;
    const m = /^((?:[│|]   |    )*)(?:[├└|`][─-]{2} )?(.*)$/.exec(raw)!;
    const depth = m[1].length / 4 + (m[0].length > m[1].length && /^[├└|`]/.test(raw.slice(m[1].length)) ? 1 : 0);
    const [name, note = ''] = m[2].split(/\s*←\s*/);
    const node: Node = { name: name.trim(), note: note.trim(), depth, children: [] };
    while (stack.length && stack[stack.length - 1].depth >= depth) stack.pop();
    (stack.length ? stack[stack.length - 1].children : roots).push(node);
    stack.push(node);
  }
  return roots;
}

function item(n: Node): string {
  const folder = n.name.endsWith('/') || n.children.length > 0;
  const kind = folder ? 'dir' : (/\.(\w+)$/.exec(n.name)?.[1] ?? 'file').toLowerCase();
  const kids = n.children.length ? `<ul>${n.children.map(item).join('')}</ul>` : '';
  return `<li class="ft-${folder ? 'dir' : 'file'}" data-kind="${esc(kind)}"><span class="ft-row">${folder ? FOLDER : FILE}`
    + `<code class="ft-name">${esc(n.name)}</code>${n.note ? `<span class="ft-note">${esc(n.note)}</span>` : ''}</span>${kids}</li>`;
}

export function fileTree(text: string): string {
  return `<div class="ftree" role="group" aria-label="Folder layout"><ul>${parseTree(text).map(item).join('')}</ul></div>\n`;
}
