// The "Install with FramePort" button's landing page (/install/?manifest=… or ?url=…): the same link rules as the
// app's deeplink.check_url (FrameDrop's rules), so the page never offers a link the app would refuse. No name
// lookups here: the app checks where a name resolves right before it downloads.

export type Kind = 'manifest' | 'url';
export type Parsed = { ok: true; kind: Kind; target: string; link: string } | { ok: false; error: string };

const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);
const APP_FILES = ['.apk', '.exe', '.zip', '.tar.gz', '.tgz', '.tar.xz', '.txz', '.tar.bz2', '.tar', '.appimage'];

function ipv4Private(host: string): boolean | null {
  const m = host.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
  if (!m) return null;
  const [a, b] = [Number(m[1]), Number(m[2])];
  return a === 0 || a === 10 || a === 127 || a >= 224 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && b === 168) || (a === 192 && b === 0 && Number(m[3]) === 0) || (a === 198 && (b === 18 || b === 19))
    || (a === 100 && b >= 64 && b <= 127);
}

function ipv6Private(host: string): boolean | null {
  if (!host.startsWith('[')) return null;
  const h = host.slice(1, -1).toLowerCase();
  if (h === '::' || h === '::1') return true;
  const mapped = h.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
  if (mapped) return ipv4Private(mapped[1]) ?? true;
  if (/^::ffff:[0-9a-f]{1,4}:[0-9a-f]{1,4}$/.test(h)) return true; // v4-mapped, written in hex: refuse
  const first = parseInt(h.split(':')[0] || '0', 16);
  return (first & 0xffc0) === 0xfe80 || (first & 0xfe00) === 0xfc00 || (first & 0xff00) === 0xff00;
}

/** Whether the address names this computer or a local network by its IP (what deeplink._private refuses). */
export function isPrivateHost(hostname: string): boolean {
  return ipv4Private(hostname) ?? ipv6Private(hostname) ?? false;
}

export function fileName(url: URL): string {
  const last = url.pathname.split('/').pop() ?? '';
  try { return decodeURIComponent(last); } catch { return last; }
}

/** null when FramePort may fetch the address, else the reason in plain words (deeplink.check_url's wording). */
export function checkUrl(text: string, what = 'link'): string | null {
  let url: URL;
  try { url = new URL(text.trim()); } catch { return `The ${what} isn't a valid web address.`; }
  const scheme = url.protocol.replace(/:$/, '');
  const host = url.hostname.toLowerCase();
  if ((scheme !== 'https' && scheme !== 'http') || !host) return `The ${what} must be an https:// address (got ${scheme || 'none'}).`;
  if (url.username || url.password) return `The ${what} contains a user name or password: FramePort doesn't use those.`;
  const loopback = LOOPBACK.has(host);
  if (scheme === 'http' && !loopback) return `The ${what} must use https:// (http:// is only allowed on this PC, for testing).`;
  if (scheme === 'https' && !loopback && isPrivateHost(host)) {
    return `The ${what} points into a local network (${host}): FramePort only fetches public addresses.`;
  }
  if (!url.pathname || url.pathname.endsWith('/') || !fileName(url)) return `The ${what} must end in a file name, not a folder.`;
  return null;
}

export function toFrameportLink(kind: Kind, target: string): string {
  return `frameport://install?${kind}=${encodeURIComponent(target)}`;
}

/** The page's query string → what to install, or why not. */
export function parseInstallParams(search: string): Parsed {
  const q = new URLSearchParams(search);
  const manifest = q.get('manifest')?.trim();
  const file = q.get('url')?.trim();
  if (manifest) {
    const error = checkUrl(manifest, 'manifest address');
    return error ? { ok: false, error } : { ok: true, kind: 'manifest', target: manifest, link: toFrameportLink('manifest', manifest) };
  }
  if (file) {
    const error = checkUrl(file, 'file address');
    if (error) return { ok: false, error };
    const name = fileName(new URL(file)).toLowerCase();
    if (!APP_FILES.some((ext) => name.endsWith(ext))) {
      return { ok: false, error: 'The file address must point to an APK, a Windows program (.exe) or a Linux app (zip, tar or AppImage).' };
    }
    return { ok: true, kind: 'url', target: file, link: toFrameportLink('url', file) };
  }
  return { ok: false, error: "The link doesn't say what to install (no manifest= or url=)." };
}

export interface ManifestInfo { name: string; description: string; icon: string | null; files: string[] }

/** The parts of a framedrop.install/v1 manifest the page shows (deeplink.manifest_from_data, lenient). */
export function readManifest(data: unknown): ManifestInfo | null {
  if (!data || typeof data !== 'object') return null;
  const d = data as Record<string, any>;
  if (d.schema !== 'framedrop.install/v1' || typeof d.name !== 'string' || !d.name.trim() || !Array.isArray(d.files)) return null;
  const files = d.files.map((f: any) => (typeof f?.url === 'string' && !checkUrl(f.url) ? fileName(new URL(f.url)) : null))
    .filter((n: string | null): n is string => !!n);
  if (!files.length) return null;
  const fp = d.frameport && typeof d.frameport === 'object' ? d.frameport : {};
  const description = typeof fp.description === 'string' ? fp.description.trim().slice(0, 2000) : '';
  const icon = typeof fp.icon === 'string' && !checkUrl(fp.icon, 'icon address') ? fp.icon : null;
  return { name: d.name.trim().slice(0, 200), description, icon, files: files.slice(0, 16) };
}
