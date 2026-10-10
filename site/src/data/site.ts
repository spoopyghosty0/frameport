// Links and names used across the site. Release asset names must match updates.ASSETS / build.yml (never renamed).
export const REPO = 'https://github.com/spoopyghosty0/frameport';
export const RELEASES = `${REPO}/releases/latest`;
export const blob = (path: string) => `${REPO}/blob/main/${path}`;
export const ISSUES = `${REPO}/issues/new?template=`;
export const SHARE_CONFIG = `${ISSUES}working-config.yml`;
export const REPORT_PROBLEM = `${ISSUES}bug-report.yml`;

export interface Download { os: 'windows' | 'macos' | 'linux' | 'linux-arm64'; label: string; detail: string; asset: string; first: string }
export const DOWNLOADS: Download[] = [
  { os: 'windows', label: 'Windows', detail: 'x64 · zip', asset: 'FramePort-windows-x64.zip',
    first: 'open FramePort.exe. At “Windows protected your PC” choose More info → Run anyway (the app is signed with a free certificate).' },
  { os: 'macos', label: 'macOS', detail: 'Apple Silicon · zip', asset: 'FramePort-macos-arm64.zip',
    first: 'right-click FramePort.app → Open → Open (macOS asks once for apps from outside the App Store).' },
  { os: 'linux', label: 'Linux', detail: 'x64 · tar.gz', asset: 'FramePort-linux-x64.tar.gz', first: 'run FramePort/FramePort from the unpacked folder.' },
  { os: 'linux-arm64', label: 'Linux arm64', detail: 'arm64 · tar.gz', asset: 'FramePort-linux-arm64.tar.gz', first: 'run FramePort/FramePort from the unpacked folder.' },
];
export const downloadUrl = (asset: string) => `${RELEASES}/download/${asset}`;
/** The download page for one platform: it starts the download itself and keeps a button for when that fails. */
export const downloadPage = (os?: string) => url(os ? `download/?os=${os}` : 'download/');

/** The site's own URL for a path (GitHub Pages serves it under /frameport/). */
export const url = (path = '') => `${import.meta.env.BASE_URL.replace(/\/$/, '')}/${path.replace(/^\//, '')}`;
