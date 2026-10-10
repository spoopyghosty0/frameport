// Words the docs use without explaining them. The docs pages underline the first use of each on a page and show
// this on hover or tap. `doc` = the docs page that says more (slug, optional #section).

export interface Term { term: string; def: string; doc?: string; ci?: boolean }

export const GLOSSARY: Term[] = [
  { term: 'Lepton', def: 'Valve’s Android runtime on the Steam Frame. Every Quest game runs in its own Lepton container, started from its Steam library entry.', doc: 'frame-runtime' },
  { term: 'OVRPort', def: 'The open-source converter that turns a Quest APK into a standard OpenXR app. FramePort runs it for you, then adds the Frame’s own fixes.', doc: 'architecture' },
  { term: 'FrameBridge', def: 'FramePort’s layer between a game and the Frame’s OpenXR runtime: it fills in features the Frame lacks and smooths over its differences.', doc: 'architecture' },
  { term: 'Developer Mode', def: 'A Steam Frame setting (Settings → System) that turns on SSH and the USB network. FramePort’s setup switches it on; nothing else changes with it.', doc: 'frame-setup' },
  { term: 'OpenXR', def: 'The open standard VR programming interface. On the Frame, SteamVR provides it.' },
  { term: 'VrApi', def: 'Meta’s older, Quest-only VR interface. Games that still use it get FramePort’s VrApi bridge, which translates to OpenXR.', doc: 'playbook' },
  { term: 'OBB', def: 'An Android data file that big games keep next to their APK, in Android/obb/<package>. FramePort uploads it with the game.', doc: 'faq' },
  { term: 'APK', def: 'An Android app package. Quest games are APKs; FramePort converts, re-signs and installs them.' },
  { term: 'Proton', def: 'Valve’s compatibility layer for Windows programs. FramePort uses its ARM64 build to run PC VR games on the Frame.', doc: 'install#pc-vr-games' },
  { term: 'Revive', def: 'An open-source layer that lets Oculus Rift games run on other VR runtimes, such as SteamVR.', doc: 'install#pc-vr-games' },
  { term: 'Zink', def: 'Mesa’s OpenGL-on-Vulkan driver. The Frame runs every OpenGL ES game through it, which is why some shader quirks show up.', doc: 'frame-runtime' },
  { term: 'FEX', def: 'An emulator that runs x86-64 Linux programs on the Frame’s ARM processor.', doc: 'install#linux-apps' },
  { term: 'SteamVR', def: 'Valve’s VR runtime. On the Frame it draws everything you see in the headset, games included.' },
  { term: 'Konsole', def: 'The terminal app in the Frame’s Linux desktop (Desktop Mode). The one-line setup is typed or pasted there once.', doc: 'install#connecting-the-steam-frame' },
  { term: 'recipe', ci: true, def: 'The patches and settings that make one game run on the Frame. FramePort suggests one; known-good ones come from the catalog.', doc: 'compatibility' },
  { term: 'catalog', ci: true, def: 'FramePort’s list of tested recipes. Installed copies fetch changes from GitHub every few hours, no app update needed.', doc: 'compatibility' },
  { term: 'agent', def: 'A small Python program FramePort keeps on the Frame. It installs, launches and tests games over SSH.', doc: 'architecture' },
];
