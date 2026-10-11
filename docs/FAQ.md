# FAQ

## How should I lay out a game that has OBB files?

Some Quest games come as an APK (the app file) plus `.obb` files (the game's data). Give every game its own folder,
put the APK in it and put the data in a folder named after the game's **package name** (for example `com.Armature.VR4`):

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

- **Data folder name:** the package name (`com.Armature.VR4` above) or `obb`.
- **Everything in that folder is copied** to the Steam Frame, subfolders included, so other data files work too.
- **One game per folder.** Several APKs in one folder count as one game.
- **Scanning:** pick the folder that contains the game folders (`Games/` above) to add them all, or one game's folder
  to add just that game. FramePort looks up to 5 levels deep.
- **A single game:** **Add games → Add an APK…** also finds the data folder next to the APK.

Not sure of the package name? It's in the OBB file names (`main.<version>.<package name>.obb`), and the game's page
shows it under **Details**.

## Can I scan SideQuest backups or AXRB downloads directly?

Yes. Scan the folder as it is; FramePort finds the APK and its OBB files in both layouts:

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

- **Several backups of one game:** FramePort uses the newest one. To use another, scan just that backup's folder.
- **AXRB's patched builds** (`patched/`, `*-axrb.apk`) are converted for AXRB's PC runtime and lack the game's data, so
  FramePort skips them and, on the next scan, replaces a game that was added from one with the original download.
- **AXRB export ZIPs:** unzip first. A game's ZIP holds `base.apk` and `Android/obb/<package>/`, which FramePort finds
  as is.
- **The game page says the data file (.obb) is missing** although you have one: the backup didn't include it (copy
  the game's `Android/obb/<package>` folder from the Quest with SideQuest), or it sits in a folder FramePort doesn't
  look in. Then use the layout in the previous answer.
- **The game starts and stops at once (Steam shows Resume):** add the game again from its original download or
  backup, click **Update on Frame**, then **Report a problem…** on its page if it still stops.
