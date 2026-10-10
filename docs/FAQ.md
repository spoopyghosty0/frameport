# FAQ

## How should I lay out a game that has OBB files?

Some Quest games come as an APK (the app file) plus `.obb` files (the game's data). Give every game its own folder,
put the APK in it and put the data in a folder named after the game's **package name** (e.g. `com.Armature.VR4`):

```
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
