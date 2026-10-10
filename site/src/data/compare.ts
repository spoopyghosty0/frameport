// FramePort next to the other ways to put apps on a Steam Frame, as questions a user would ask. Facts about other
// tools come from their own pages (checked 2026-10-09, links in SOURCES); what they don't state is shown as
// "not stated", never guessed. The README's "Compared with other tools" says the same: change both together.

export type Mark = 'yes' | 'no' | 'part' | 'unknown';
export interface Cell { mark: Mark; text: string }
export interface Row { label: string; cells: [Cell, Cell, Cell] }
export interface Headline { title: string; frameport: string; others: [string, string]; icon: 'code' | 'game' | 'pc' | 'link' }

export const CHECKED = '2026-10-09';
export const TOOLS = ['FramePort', 'FrameDrop', 'By hand with Valve’s tools'] as const;
export const TOOL_NOTES = ['This project', 'Sideloader by CadeXR', 'SteamOS Devkit Client, or adb with Lepton Development'] as const;

/** The few differences most people decide on. */
export const HEADLINES: Headline[] = [
  { icon: 'code', title: 'Free and open source',
    frameport: 'GPL-3.0. Read every line, build it yourself, change it, share it.',
    others: ['Free (donationware); source not published', 'Free, from Valve'] },
  { icon: 'game', title: 'Quest games that don’t run on the Frame as they are',
    frameport: 'Converts them to OpenXR and patches them for the Frame, with a tested recipe for 100+ games.',
    others: ['Installs the APK as it is', 'Installs the APK as it is'] },
  { icon: 'pc', title: 'Your computer',
    frameport: 'Windows, macOS and Linux.',
    others: ['Windows', 'Depends on the tool'] },
  { icon: 'link', title: 'Wi-Fi or a USB cable',
    frameport: 'Either: your home Wi-Fi, the Frame’s own hotspot, or a USB cable. No pairing screen.',
    others: ['Same Wi-Fi network', 'Wi-Fi pairing, or adb'] },
];

const y = (text: string): Cell => ({ mark: 'yes', text });
const n = (text: string): Cell => ({ mark: 'no', text });
const p = (text: string): Cell => ({ mark: 'part', text });
const u = (text = 'Not stated'): Cell => ({ mark: 'unknown', text });

/** Every difference, as a user would ask it. */
export const ROWS: Row[] = [
  { label: 'What does it cost, and can I see the code?', cells: [y('Free, open source (GPL-3.0)'), p('Free (donationware), source not published'), y('Free, from Valve')] },
  { label: 'Which computer can I use?', cells: [y('Windows, macOS, Linux'), p('Windows'), p('Depends on the tool')] },
  { label: 'How do I connect the Frame the first time?', cells: [
    y('Type one command in the Frame’s Konsole; it turns on Developer Mode itself. No password'),
    p('Turn on Developer Mode, then Settings → Developer → Pair new host'),
    p('Turn on Developer Mode and pair, or start Lepton Development and use adb')] },
  { label: 'Wireless or cable?', cells: [y('Wi-Fi, the Frame’s hotspot, or a USB cable'), p('Same Wi-Fi network'), p('Wi-Fi pairing, or adb')] },
  { label: 'Will a Quest game that doesn’t run on the Frame work?', cells: [
    y('Converted (OVRPort: Meta’s VR runtime → OpenXR) and patched for the Frame'),
    n('Not mentioned: the APK must meet Lepton’s requirements as it is (arm64, minSdk 30 or lower)'),
    n('Installs the APK as it is')] },
  { label: 'Does it know which fixes a game needs?', cells: [y('A tested recipe for 100+ games, updated without an app update'), u(), n('No')] },
  { label: 'Will the game be in my Steam library?', cells: [
    y('Yes, with artwork and tags'), p('As a “Devkit Game” shortcut'), p('As “Devkit Game: <title>” (Devkit Client); not with adb')] },
  { label: 'Android apps, Linux apps, Windows programs?', cells: [
    y('All three: Android apps in a window, Linux arm64 and x86_64, Windows programs through Proton (installed for you)'),
    y('All three: Lepton Flatscreen, Linux arm64 zips, Windows .exe through Proton (install Proton first)'),
    p('Through the Devkit Client; 2D Android apps need a marker file')] },
  { label: 'PC VR (Rift) games?', cells: [y('On your PC through Revive; SteamVR and OpenXR ones on the Frame through Proton'), n('Not supported'), n('Not supported')] },
  { label: 'Do “Install with …” buttons on websites work?', cells: [y('Its own and FrameDrop’s'), y('FrameDrop’s (it defined them)'), n('No')] },
  { label: 'What if a game doesn’t start?', cells: [y('A launch test reads the logs and names the likely fix'), p('A log viewer pulls the headset log'), n('No help')] },
  { label: 'Can I see and use the Frame from my computer?', cells: [y('Live view, Monitor, Files, Screenshots, Type on Frame'), u(), n('No')] },
];

export const SOURCES = [
  { label: 'FrameDrop: about', href: 'https://framedropvr.com/about' },
  { label: 'FrameDrop: how-to', href: 'https://framedropvr.com/how-to' },
  { label: 'FrameDrop: install buttons', href: 'https://framedropvr.com/docs' },
  { label: 'Valve: loading games on Steam Frame', href: 'https://partner.steamgames.com/doc/steamhardware/steamframe/loadgames' },
];
