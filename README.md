# FramePort

[![Release](https://img.shields.io/github/v/release/spoopyghosty0/frameport)](https://github.com/spoopyghosty0/frameport/releases/latest)
[![Build](https://img.shields.io/github/actions/workflow/status/spoopyghosty0/frameport/build.yml?branch=main)](https://github.com/spoopyghosty0/frameport/actions/workflows/build.yml)
[![Downloads](https://img.shields.io/github/downloads/spoopyghosty0/frameport/total)](https://github.com/spoopyghosty0/frameport/releases)
[![License](https://img.shields.io/github/license/spoopyghosty0/frameport)](LICENSE)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-blue)
![Steam Frame](https://img.shields.io/badge/Steam%20Frame-supported-1b2838?logo=steam&logoColor=white)

Install games that target the Meta Quest, Android, or general PCVR onto your **Valve Steam Frame**. FramePort handles everything from uploading game files, setting up your Frame, injecting compatibility patches, and adding shortcuts to your Steam library. FramePort aims to be as simple as possible by taking advantage of the fact that the Steam Frame runs on Linux.

[![FramePort: library, one-click install, play, monitor](docs/images/tour-teaser.webp)](docs/media/frameport-tour.mp4)

▶ [Watch the full tour](docs/media/frameport-tour.mp4) · New to FramePort? [Watch the install tutorial](docs/media/frameport-install.mp4)
(about 90 seconds each, MP4; also attached to every release)

> **Notice:** FramePort explicitly does NOT download, share, or unlock games. You must provide legally obtained game
> executables. Core features of FramePort simply download and wrap other published tools (see [Built on](#built-on))
> with patches provided by FramePort adding a hardware compatibility layer. This enables users to use games/apps legally
> purchased on sites like [SideQuest](https://sidequestvr.com/). 

## Features

![Library](docs/images/library.png)

- **Painless setup:** one short command on the Frame. No root, no `sudo`, no password.
  [What it changes](docs/FRAME_SETUP.md).
- **Type on Frame:** use your computer's keyboard on the Frame: in VR apps, Android apps, Steam and the desktop.
  [More](#type-on-frame).
- **One click per game:** convert, patch, sign, upload, add to Steam with artwork, launch test. Artwork that can't be found automatically can be picked from the stores or replaced with your own images.
- **Per-game recipes:** a tested catalog plus detection rules; every patch explained in plain words.
- **FrameBridge:** FramePort's OpenXR adapter emulates what the Frame natively lacks (passthrough, room, controller models,
  curved and 360° layers); game settings as simple switches.
- **Beyond Quest:** Android apps as windows, PC VR via Proton or Revive, Windows (non-VR) games via Proton, a Files
  tab with drag and drop.
- **Linux apps:** install Linux apps (AppImage, a folder, or a zip/tar archive) on the Frame with a Steam
  shortcut; arm64 builds run natively on SteamOS, x86_64 builds through Valve's FEX translator.
- **Install links:** "Install with FrameDrop" buttons on web pages (the one-click protocol of the FrameDrop
  sideloader) and pasted links open in FramePort, which asks, downloads, adds and installs the build.
- **Screenshots tab:** the screenshots you took in the headset, sorted by game (matched by play time) and day;
  view them and download them to your computer.
- **Live view tab:** watch what the headset shows, with sound, in a browser window on your computer.
- **Monitor tab:** the running game's frame rate, the Frame's load, temperatures, power and battery live, and its
  processes, which you can end. [More](#monitor).
- **Self-updating** releases, redacted diagnostics, one-click problem reports and working-config sharing.

## Quick start

1. [Download](https://github.com/spoopyghosty0/frameport/releases/latest) and unzip the build for Windows, macOS
   (Apple Silicon) or Linux, then start FramePort.
2. **Connect the Frame** (once):
   1. In FramePort click **Steam Frame → Start setup**. Keep FramePort open; the Frame and your computer must be on
      the same Wi-Fi (or see the USB cable option in the app).
   2. On the Frame open the **SteamVR dashboard → Launch a program → Desktop**: the Linux desktop opens on a virtual
      screen.
   3. Open the app menu (bottom-left corner of that desktop) → **System → Konsole** (or search for Konsole).
   4. Run `curl -sL spoopyghosty0.github.io/frameport/s | bash` (the same for every Frame; or copy it from the
      [setup page](https://spoopyghosty0.github.io/frameport/setup/) in the Frame's browser). It finds FramePort and
      shows a 4-digit code: click **Allow** in FramePort when it shows the same one. It then runs this
      [bash setup script](bootstrap/bootstrap.sh). FramePort's **Use the setup command** gives a line with your PC's
      address instead, for networks that block the search.
   5. After a few seconds the desktop closes by itself (Steam restarts once); that's expected. If
      Steam asks to install **Lepton**, confirm it. FramePort shows the Frame as connected within a minute. No
      password needed.
3. **Add games → Scan a folder** with your game backups (APK + OBB, or PC VR game folders).
4. Open a game → **Install on Frame**, then play it from the Frame's Steam library.

Full guide, firewalls and troubleshooting: [docs/INSTALL.md](docs/INSTALL.md). Questions (e.g. how to lay out games
with OBB files): [docs/FAQ.md](docs/FAQ.md).

## Type on Frame

Typing in VR is painful, so FramePort turns your computer's keyboard into a keyboard for the Frame. Open **Type on
Frame** (its own tab in the sidebar), select a text field in the headset and type: searches, logins, chat, in any
app, in Steam or on the desktop. Paste longer text to type it in one go. Nothing to install: FramePort adds a virtual
keyboard on the Frame while the tab is open, without root.

![Type on Frame](docs/images/type-on-frame.png)

Unity apps whose text fields close the moment you select them on the Frame (no system keyboard there) get a per-game
fix, so Steam's on-screen keyboard and Type on Frame work in them too.
[Details](docs/INSTALL.md#typing-on-the-frame).

## Screenshots

Screenshots you take in the headset show up in FramePort's **Screenshots** tab, grouped by day and matched to the game
you were playing. Open one full size, step through them, and download single shots, a selection or all of them to
your computer.

![Screenshots](docs/images/screenshots.png)

![Screenshot viewer](docs/images/screenshot-viewer.png)

## Live view

The **Live view** tab streams what the headset shows, with its sound, to your computer: click **Start live view** and
it opens in your default web browser (full screen with a double-click; click **Sound on** to hear it, as browsers start
videos muted). Pick 360p to 1080p, or the headset view's full size. The picture comes from SteamVR's built-in headset
view on the Frame and is encoded there while you watch (about one CPU core), so stop it when you're done. It's black
while the headset sleeps.

## Monitor

The **Monitor** tab shows what the Frame is doing while it's open: the running game with its frame rate against the
display's refresh rate, CPU, graphics chip, memory, the hottest temperature with the fan speed, power draw and battery
time left, each with a 2-minute chart. **Show details** adds every CPU core, all temperature sensors, where the power
goes and the network. Below, the game's processes (or Steam's, or all of them) with their CPU, GPU and memory use:
right-click one to end it, or end the whole game. Programs that Steam, SteamVR or the desktop need are marked and ask
again. The Frame sends the numbers itself (about 1 % of one CPU core) and stops when you leave the tab.

![Monitor](docs/images/monitor.png)

## Compatibility

If a game has already been tested with FramePort, it will automatically use the optimal game config. Otherwise, FramePort
will attempt to guess key patches. If you find a new config that works for an app you are testing, please consider submitting it to the community!

**[List of tested games](docs/GAMES.md)**

**Tested something? Share it.** In FramePort open the game → **…** → **Share working recipe…** (it fills in the
recipe for you) or **Report a problem…** (attaches a diagnostics zip with personal data removed). Without the app:
[share a working config](https://github.com/spoopyghosty0/frameport/issues/new?template=working-config.yml) · [report a problem](https://github.com/spoopyghosty0/frameport/issues/new?template=bug-report.yml). Shared configs become built-in recipes for everyone.

## Compared with other tools

FrameDrop and Valve's own tools install an app as it is. FramePort differs in four ways most people decide on:

- **Free and open source** (GPL-3.0): read every line, build it yourself, change it, share it. FrameDrop is free
  (donationware) without published source.
- **Quest games that don't run on the Frame as they are** get converted to OpenXR and patched, with a tested recipe
  for 100+ games. The others install the APK as it is.
- **Windows, macOS and Linux.** FrameDrop is for Windows.
- **Wi-Fi or a USB cable**: your home Wi-Fi, the Frame's own hotspot, or a cable. No pairing screen.

<details>
<summary>All differences</summary>

| | **FramePort** | **FrameDrop** | **By hand with Valve's tools** |
|---|---|---|---|
| What does it cost, and can I see the code? | Free, open source (GPL-3.0) | Free (donationware), source not published | Free, from Valve |
| Which computer can I use? | Windows, macOS, Linux | Windows | Depends on the tool |
| How do I connect the Frame the first time? | Type one command in the Frame's Konsole; it turns on Developer Mode itself. No password | Turn on Developer Mode, then Settings → Developer → Pair new host | Turn on Developer Mode and pair, or start Lepton Development and use adb |
| Wireless or cable? | Wi-Fi, the Frame's hotspot, or a USB cable | Same Wi-Fi network | Wi-Fi pairing, or adb |
| Will a Quest game that doesn't run on the Frame work? | Converted (OVRPort: Meta's VR runtime → OpenXR) and patched for the Frame | Not mentioned: the APK must meet Lepton's requirements as it is (arm64, minSdk 30 or lower) | Installs the APK as it is |
| Does it know which fixes a game needs? | A tested recipe for 100+ games, updated without an app update | Not stated | No |
| Will the game be in my Steam library? | Yes, with artwork and tags | As a "Devkit Game" shortcut | As "Devkit Game: &lt;title&gt;" (Devkit Client); not with adb |
| Android apps, Linux apps, Windows programs? | All three: Android apps in a window, Linux arm64 and x86_64, Windows programs through Proton (installed for you) | All three: Lepton Flatscreen, Linux arm64 zips, Windows .exe through Proton (install Proton first) | Through the Devkit Client; 2D Android apps need a marker file |
| PC VR (Rift) games? | On your PC through Revive; SteamVR and OpenXR ones on the Frame through Proton | Not stated | Not stated |
| Do "Install with …" buttons on websites work? | Its own and FrameDrop's | FrameDrop's (it defined them) | No |
| What if a game doesn't start? | A launch test reads the logs and names the likely fix | A log viewer pulls the headset log | No help |
| Can I see and use the Frame from my computer? | Live view, Monitor, Files, Screenshots, Type on Frame | Not stated | No |
| Does it update itself? | Yes | Run the new installer (automatic updates not stated) | Per tool |

</details>

Other tools as described on their own pages, checked 2026-10-09:
[FrameDrop about](https://framedropvr.com/about) · [how-to](https://framedropvr.com/how-to) ·
[install buttons](https://framedropvr.com/docs) · [Valve: loading games on Steam Frame](https://partner.steamgames.com/doc/steamhardware/steamframe/loadgames).
Something out of date? [Open an issue](https://github.com/spoopyghosty0/frameport/issues/new).

## Built on

[OVRPort](https://github.com/Android-XR-Bridge/OVRPort) (Quest → OpenXR, originally
[ovrport/app](https://github.com/ovrport/app)) · Valve Lepton, Proton and SteamVR ·
[Revive](https://github.com/LibreVR/Revive) · Mesa (Zink) · [Khronos OpenXR SDK](https://github.com/KhronosGroup/OpenXR-SDK)
· Eclipse Temurin, Android apksigner and NDK · [Flet](https://flet.dev) · OculusDB and Steam store data.
What FramePort adds itself: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Documentation

| | |
|---|---|
| [INSTALL.md](docs/INSTALL.md) | Install, connect, update, PC VR, files, problem reports |
| [FRAME_SETUP.md](docs/FRAME_SETUP.md) | What setup changes on the Frame, networks and firewalls, undoing it |
| [COMPATIBILITY.md](docs/COMPATIBILITY.md) | What runs and how well |
| [GAMES.md](docs/GAMES.md) | Tested games and how well they run |
| [PLAYBOOK.md](docs/PLAYBOOK.md) | Symptoms and fixes per game |
| [FRAME_RUNTIME.md](docs/FRAME_RUNTIME.md) | Steam Frame runtime facts |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the code is organised |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Code, recipes, translations |

## Development

```
uv sync --extra dev
uv run frameport-gui        # the app
uv run frameport --help     # command line
uv run pytest               # tests
```

## AI Usage Notice
While I would like to program everything manually, I no longer have much free time for personal projects. As a result I make use of AI tools to make it significantly quicker to debug compatibility issues.

## License

GPL-3.0-only (includes GPL-3.0 code from OVRPort). Not affiliated with Valve or Meta.
