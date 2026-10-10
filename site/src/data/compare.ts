// FramePort next to the other ways to put apps on a Steam Frame. Facts about other tools come from their own pages
// (checked 2026-10-09, links in SOURCES); what they don't state is shown as "not stated", never guessed. The README's
// "Compared with other tools" table says the same: change both together.

export type Mark = 'yes' | 'no' | 'part' | 'unknown';
export interface Cell { mark: Mark; text: string }
export interface Row { label: string; cells: [Cell, Cell, Cell] }

export const CHECKED = '2026-10-09';
export const TOOLS = ['FramePort', 'FrameDrop', 'By hand with Valve’s tools'] as const;
export const TOOL_NOTES = [
  'This project',
  'Sideloader by CadeXR',
  'SteamOS Devkit Client, or adb with Lepton Development',
] as const;

const y = (text: string): Cell => ({ mark: 'yes', text });
const n = (text: string): Cell => ({ mark: 'no', text });
const p = (text: string): Cell => ({ mark: 'part', text });
const u = (text = 'Not stated'): Cell => ({ mark: 'unknown', text });

export const ROWS: Row[] = [
  { label: 'Price and source', cells: [y('Free, open source (GPL-3.0)'), p('Free (donationware), source not published'), y('Free, from Valve')] },
  { label: 'Your computer', cells: [y('Windows, macOS, Linux'), p('Windows'), p('Depends on the tool')] },
  { label: 'Connecting the Frame', cells: [
    y('One command in the Frame’s Konsole; it turns on Developer Mode itself. Wi-Fi or USB cable'),
    p('Developer Mode, then Settings → Developer → Pair new host. Same Wi-Fi'),
    p('Developer Mode and pairing, or Lepton Development and adb')] },
  { label: 'Meta Quest games that don’t run as they are', cells: [
    y('Converted (OVRPort: Meta’s VR runtime → OpenXR) and patched for the Frame'),
    n('Not mentioned: the APK must meet Lepton’s requirements as it is (arm64, minSdk 30 or lower)'),
    n('Installs APKs as they are')] },
  { label: 'Tested per-game recipes', cells: [y('100+ tested games, followed automatically'), u(), n('No')] },
  { label: 'Steam library entry', cells: [
    y('Shortcut with artwork and tags'), p('“Devkit Game” shortcut'), p('“Devkit Game: <title>” (Devkit Client); none with adb')] },
  { label: 'Android 2D apps', cells: [y('Yes, in a window'), y('Yes (Lepton Flatscreen)'), y('Yes, with a marker file')] },
  { label: 'Linux apps', cells: [y('arm64, and x86_64 through FEX'), y('arm64 zips; x86_64 less reliable'), y('Yes (Devkit Client)')] },
  { label: 'Windows programs and PC VR games', cells: [
    y('Proton (installed for you); PC VR games also through Revive on your PC'), p('Windows .exe through Proton (install Proton first)'),
    p('Proton (Devkit Client)')] },
  { label: '“Install with …” buttons on web pages', cells: [y('frameport:// and FrameDrop’s framedrop:// links'), y('framedrop:// links (FrameDrop defined them)'), n('No')] },
  { label: 'After installing', cells: [y('Launch test that reads the logs and names the likely fix'), p('Log viewer (pull the headset log)'), n('No')] },
  { label: 'Also on the PC', cells: [y('Files, Screenshots, Live view, Monitor, Type on Frame'), u(), n('No')] },
  { label: 'Updates', cells: [y('Updates itself'), p('Run the new installer (automatic updates not stated)'), p('Per tool')] },
];

export const SOURCES = [
  { label: 'FrameDrop: about', href: 'https://framedropvr.com/about' },
  { label: 'FrameDrop: how-to', href: 'https://framedropvr.com/how-to' },
  { label: 'FrameDrop: install buttons', href: 'https://framedropvr.com/docs' },
  { label: 'Valve: loading games on Steam Frame', href: 'https://partner.steamgames.com/doc/steamhardware/steamframe/loadgames' },
];
