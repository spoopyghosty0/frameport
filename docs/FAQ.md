# FAQ

## How should I lay out a game that has OBB files?

Some Quest games come as an APK (the app) plus `.obb` files (the game's data). Give every game its own folder. Put
the APK in the game's folder, and the data in a folder named after the game's **package name** (for example `com.Armature.VR4`):

```tree
Games/                                  ← scan this folder (Add games → Scan a folder)
├── Resident Evil 4/                    ← one folder per game (any name)
│   ├── VR4.apk                         ← the APK (any file name)
│   └── com.Armature.VR4/               ← the data folder: exactly the package name
│       ├── main.203.com.Armature.VR4.obb
│       └── patch.203.com.Armature.VR4.obb
└── Beat Saber/
    └── com.beatgames.beatsaber.apk     ← games without OBBs: just the APK
```

- **Data folder name:** the package name or `obb`. Everything in the data folder is copied to the Frame,
  subfolders included.
- **One game per folder.** Several APKs in one folder count as one game.
- **Scanning:** pick the folder with the game folders to add them all, or one game's folder for one game.
  FramePort looks up to 5 levels deep. **Add games → Add an APK…** also finds the data folder next to the APK.

The package name is in the OBB file names (`main.<version>.<package name>.obb`) and under **Details** on the game's
page.

## I moved my games to another folder or drive. How do I update the library?

Scan the new folder (**Add games → Scan a folder…** or **Settings → Library folders → Add folder…**). Games found
there whose old files are gone keep their library entry: name, tags, artwork, your recipe and the install on the
Frame stay. **Rescan folders** in the Library checks every library folder
the same way.

- **Remove the old folder** in **Settings → Library folders** (bin icon). The folder's games stay in the library.
- **Another version in the new folder:** **Scan a folder…** takes the new version like **Update with a newer
  APK…** would.
  **Rescan folders** leaves such games alone; use the game's **Update** menu to switch.
- **Both copies still there:** the library keeps the copy it already has. Delete or rename the old
  copy and rescan.
- PC VR games are matched by folder name, or by program file if renamed. Linux apps aren't scanned: add the moved app
  again with **Add a Linux app…** (the app keeps its name, art and switches).
- Command line: `frameport folders [--add DIR] [--remove DIR]`, `frameport rescan`.

## Can I scan SideQuest backups or AXRB downloads directly?

Yes. Scan the folder as it is:

```tree
SideQuest Backups/                      ← scan this folder
└── com.Armature.VR4/                   ← one folder per game (the package name)
    └── 2026-10-07T02-10-19-171Z/       ← one folder per backup
        ├── apk/com.Armature.VR4.apk    ← the game
        ├── obb/                        ← its OBB files, sent to the Frame
        │   ├── main.203.com.Armature.VR4.obb
        │   ├── patch.203.com.Armature.VR4.obb
        │   └── VR4-Android-Shipping-arm64.apk   ← ignored (not a second game)
        ├── data/                       ← the app's own files on the Quest: not needed
        ├── icon.png
        └── manifest.json

AXRB/                                   ← AXRB's download folder (Downloads/AXRB): scan this folder
├── 1234567890/                         ← AXRB's numbers for the game and the build
│   └── 987654321/
│       ├── base.apk                    ← the game
│       ├── main.203.com.Armature.VR4.obb   ← sent to the Frame, with any other files here (DLC)
│       └── patch.203.com.Armature.VR4.obb
└── patched/                            ← AXRB's own PC builds: ignored
```

- **Several backups of one game:** FramePort uses the newest. To use another, scan just that backup's folder.
- **AXRB's patched builds** (`patched/`, `*-axrb.apk`) are made for AXRB's PC runtime and lack the game's data, so
  FramePort skips them.
- **AXRB export ZIPs:** unpack them first.
- **"Game data (.obb) is missing"** although you have the OBB files: the backup didn't include them (copy
  `Android/obb/<package>` from the Quest with SideQuest), or the files are somewhere FramePort doesn't look. Use the layout in
  the first answer.
- **The game starts and stops at once (Steam shows Resume):** add the game again from its original download or
  backup, click **Update on Frame**, and report the problem if the game still stops.

## Can I add .xapk, .apks, .apkm or .apkx files (app bundles)?

Yes. Add them like an APK: **Add an APK…**, drag them onto the Library, scan a folder, use an install link, or
`frameport scan <file>`.

| File | Made by | Inside |
|---|---|---|
| `.xapk` | APKPure and others | `manifest.json`, the APK (or `base.apk` + `config.*.apk` splits), `Android/obb/<package>/` |
| `.apks` | bundletool, SAI | `splits/base-master.apk` + `splits/base-arm64_v8a.apk` …, or `universal.apk`; SAI: `base.apk` + `split_*.apk` |
| `.apkm` | APKMirror | `base.apk` + `split_config.*.apk`, `info.json` |
| `.apkx` | various | the same kind of zip under another name |
| a folder of split APKs | `adb pull` of `pm path`, unpacked bundles | `base.apk` + `split_config.arm64_v8a.apk` … |

- FramePort merges the parts into one APK (the Frame installs one APK per game), in FramePort's data folder
  (`bundles/`). Your file isn't changed. FramePort takes the 64-bit ARM part, else the 32-bit one. Language and screen
  parts are merged with [APKEditor](https://github.com/REAndroid/APKEditor), downloaded on first use.
- OBB files in the bundle, or in an `obb/` or `<package>` folder next to the bundle, are sent like a normal game's data.
- The merged APK is signed with the game's own key, like every build. A game that checks its own signature may
  refuse to run.
- **"is a protected APKMirror bundle":** older `.apkm` files are encrypted for APKMirror's installer. Download the
  app again (current `.apkm` files work), or get an `.apk` or `.xapk`.
- Unpacking needs about twice the bundle's size in free space.

## Where are a game's settings in FramePort?

- **Game settings:** right-click the game → **Game settings…**. Render resolution, refresh rate, controllers, menus
  and more. Changes apply at the next start.
- **Patches:** open the game's page → **Customize** (next to **What FramePort will do**). Turn a patch on or off,
  then click **Update on Frame**.
- **The game's menu:** right-click the game, or **…** on the game's page. Groups: **Update**, **On the Frame**, **Saves**,
  **Name and artwork**, **Recipe** (**Reset to suggested recipe…**, **Share working recipe…**) and **Troubleshoot**
  (**Report a problem…**).

For troubleshooting, the sidebar has **Monitor** (frame rate, CPU, graphics) and **Files** (each game's files on the
Frame under **Game storage**; microSD cards and other drives under **Drives**).

## A game doesn't work right. Where do I start?

1. **Update FramePort**, then open the game's page. If the page shows **Update on Frame**, click **Update on Frame**.
2. **Read the game's page.** The last launch test and **Last session** often suggest a patch or setting, with a
   button that applies the suggestion. Try the suggestion first.
3. **Changed patches yourself?** **Recipe → Reset to suggested recipe…** brings back the tested ones.
4. **Find your symptom below.**
5. **Still stuck?** [Report the problem](#how-do-i-report-a-problem-well).

## The game doesn't start, or stops right away

The game's page shows the last launch test's result with an explanation.

- **"Game data (.obb) is missing":** the data files never reached the Frame.
  1. Find the game's `.obb` files (`main.<version>.<package>.obb`).
  2. Put them in a folder named exactly the package name, next to the APK ([layout](#how-should-i-lay-out-a-game-that-has-obb-files)).
  3. **Add games → Scan a folder…** and pick the game's folder.
  4. Click **Update on Frame**.
- **"Lepton didn't start the game this time":** click **Play** again.
- **32-bit-only APK:** the Frame has no 32-bit support. FramePort's 32-bit bridge (experimental) is suggested; if the
  game still doesn't run, look for a PC VR version.
- **"The game quit itself right after starting":** click **Update on Frame** so the suggested patches apply.
- **Anything else:** report the problem.

## The game hangs on a logo, a black screen or a loading screen

Leave the game running and open **Monitor**.

- **CPU or graphics busy:** the game is still working, often preparing shaders on the first start. Wait up to 10 minutes
  once; later starts are quicker.
- **CPU and graphics near zero:** the game is stuck. **While the game still hangs**, use **Troubleshoot → Report a problem…**: the
  zip then shows what the game waits for.
- **The game starts with a video** and hangs at its logo: **Customize** → turn off **Hardware video decoding** →
  **Update on Frame**.

## The picture is wrong

Change one thing at a time, then start the game again (patches also need **Update on Frame**).

| You see | Try |
|---|---|
| Something shows in one eye only, or one eye is gray (Unity games) | **Customize** → [Unity: render each eye separately](PATCHES.md#frame-unity_multipass) |
| Left and right look swapped (depth feels wrong) | **Game settings…** → **Swap eyes** |
| Textures flicker, objects pop in and out | **Game settings…** → **Turn off space warp** |
| One eye shimmers while menus look steady | **Game settings…** → **Foveated rendering (Valve)**: **Fixed**, else **Off** |
| Menus or screens upside down | **Game settings…** → **Fix upside-down menus** |
| Curved menus or video screens missing | **Game settings…** → **Show curved menus and screens** |
| Blurry | **Game settings…** → raise **Render resolution (supersampling)** (costs frame rate) |

Every patch is in the [patch reference](PATCHES.md). Nothing helps? Report the problem and describe what you see.

## The game stutters or runs slowly

Open **Monitor** while you play. Smooth play needs the frame rate at the refresh rate (for example 72 or 90 fps).

- **Below the refresh rate:** **Game settings…** → lower **Render resolution (supersampling)** (for example to 0.8×) or the
  **Refresh rate**. After a session, the game's page may suggest a value.
- **At the refresh rate, but the game still judders:** report the problem.

## The game pauses or recenters by itself

The Frame briefly tells games they lost focus, for example when the headset's sensor thinks you took the headset off.
**Game settings…**: keep **Don't pause on short interruptions** on and raise **Ignore focus dips up to** (for example
to 2.5 s).

## Controllers don't work in a game

In **Game settings…**:

- **No controllers, or only hands:** turn on **Use controllers**.
- **Can't aim at menus:** adjust **Pointer angle**.
- **Vibration too strong:** lower **Vibration strength**.
- **Buttons repeat or do the wrong thing:** **Controller buttons** → **Mirror** or **Quest pair**
  ([Controller buttons](CONTROLLERS.md)).

## The Frame turns off or gets hot during a game

Only on battery, never plugged in? The cause is power, not FramePort: heavy games can draw more than the battery delivers.

- Play plugged in, or lower **Render resolution (supersampling)** in **Game settings…**.
- The Frame also turns off while plugged in: report the problem.

## My progress isn't saved

1. Quit through the game's own menu. Most games save when Steam's **Exit game** pauses them, but some only save when
   you quit them yourself.
2. Check that the game saves at all: **Files** → **Game storage** → the game → look at the files' dates. Play, save, quit
   and look again.
3. Nothing changes: report the problem.

Saves stay on the Frame when you update or reinstall a game.

## Can I back up a game's saves?

Yes. Steam doesn't sync Quest games' saves, so FramePort copies them to your PC. Close the game first, then use
**Back up saves** next to **Play**, or **Back up all saves** on the Steam Frame page. Caches and large downloaded
content (videos, OBBs, files over 64 MB) are left out.

- **Where:** `save-backups/<package>/` in FramePort's data folder (**Saves → Open backups folder**). Settings →
  **Data** → **Choose backup folder…** picks another, for example a cloud-sync folder. Older backups stay restorable.
- **How many:** the 10 newest per game by default (Settings → Data).
- **Automatically:** **Back up saves when a game closes** (Settings → Data, off by default). The Frame keeps 3
  snapshots per game, even while FramePort is closed; FramePort copies the snapshots over when FramePort
  connects. Connect once after turning the setting on.
- **Restore:** **Saves → Restore saves…**. **Undo last restore** in the same window takes you back. Uninstalling a
  game with *Also delete its saves* backs them up first.

Command line: `frameport saves backup|list|restore|undo <game>`.

## A game with mods crashes

Play once without mods. If the game then works, a mod is the cause (the diagnostics zip usually names the mod).
Report the crash to the mod's author, and check for a mod version that matches your game version.

## The game needs files I supply (ports, emulators, mods)

The game's instructions say where; the place is usually `Android/data/<package>/files` (the game's `launch.log`
names the path).

1. Unpack the files on your PC first: these apps read folders, not disc images (`.iso`) or archives.
2. **Files** → **Game storage** → the game → that folder → **Upload folder…** or **Upload files…**.

In-game "choose a file" buttons don't work: the Frame has no file picker.

## How do I report a problem well?

1. Reproduce the problem, then right away use **Troubleshoot → Report a problem…** in the game's menu. For a hang,
   report while the game still hangs.
2. Say what you saw in the headset and what you tried.
3. **Collect and open GitHub** saves a diagnostics zip and opens a prefilled report: attach the zip. The zip holds logs,
   not your game files, and personal details are removed.
