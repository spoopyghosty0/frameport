// Words the docs use without explaining them. The docs pages underline the first use of each on a page and show
// this on hover or tap. `doc` = the docs page that says more (slug, optional #section).

export interface Term { term: string; def: string; doc?: string; ci?: boolean }

export const GLOSSARY: Term[] = [
  { term: 'Lepton', def: "Valve's Android app support on the Steam Frame. Each Quest game runs in its own Lepton container.", doc: 'frame-runtime' },
  { term: 'OVRPort', def: "The open-source converter that makes a Quest game run on other headsets. FramePort runs it, then adds the Frame's own patches.", doc: 'architecture' },
  { term: 'FrameBridge', def: "FramePort's layer between a game and the Frame. It fills in features the Frame lacks.", doc: 'architecture' },
  { term: 'Developer Mode', def: "A Steam Frame setting (Settings → System) that lets FramePort connect. FramePort's setup turns it on.", doc: 'frame-setup' },
  { term: 'OpenXR', def: 'The open standard VR games are written for. On the Frame, SteamVR provides it.' },
  { term: 'VrApi', def: "Meta's older, Quest-only VR interface. FramePort translates it to OpenXR.", doc: 'playbook' },
  { term: 'OBB', def: 'The extra data file of a big Android game. FramePort uploads it with the game.', doc: 'faq' },
  { term: 'APK', def: 'An Android app file. Quest games are APKs.' },
  { term: 'Proton', def: "Valve's tool for running Windows programs on Linux. FramePort uses it to run PC VR games on the Frame.", doc: 'install#pc-vr-games' },
  { term: 'Revive', def: 'Open-source software that runs Oculus Rift games on SteamVR.', doc: 'install#pc-vr-games' },
  { term: 'Zink', def: "Mesa's OpenGL-on-Vulkan driver. The Frame runs OpenGL ES games through it.", doc: 'frame-runtime' },
  { term: 'FEX', def: "An emulator that runs x86_64 (Intel/AMD) Linux programs on the Frame's ARM processor.", doc: 'install#linux-apps' },
  { term: 'SteamVR', def: "Valve's VR runtime. On the Frame it draws everything you see in the headset." },
  { term: 'Konsole', def: "The terminal app on the Frame's desktop. You run the setup line there once.", doc: 'install#connecting-the-steam-frame' },
  { term: 'recipe', ci: true, def: 'The patches and settings that make one game run on the Frame. Tested ones come from the catalog.', doc: 'compatibility' },
  { term: 'catalog', ci: true, def: "FramePort's list of tested recipes. It updates without an app update.", doc: 'compatibility' },
  { term: 'agent', def: 'A small program FramePort keeps on the Frame. It installs, starts and tests games.', doc: 'architecture' },
];
