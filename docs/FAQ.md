# FAQ

## How should I lay out a game that has OBB files (an APK plus a data folder)?

Give every game its own folder, put the APK in it, and put the game's data next to the APK in a folder named
after the game's **package name** (the name the `.obb` files contain, e.g. `com.Armature.VR4`):

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

- **Data folder name:** the package name (`com.Armature.VR4` above), or `obb`. FramePort uses the first of the two
  that exists and isn't empty.
- **Everything in that folder is copied** to the game's `Android/obb/<package>/` on the Frame, subfolders included.
  So games that ship raw data files instead of `.obb` files work the same way.
- **One game per folder.** Several APKs in one folder count as one game; FramePort uses the first and keeps the others
  as alternates.
- **Scanning:** pick the folder that contains the game folders (`Games/` above) to add them all, or one game's folder
  to add just that game. FramePort looks up to 5 levels deep.
- **A single game:** Add games → Add an APK… works with a lone APK too. Its data is found when the data folder sits next
  to the APK, named as above.

Not sure of the package name? Look at the OBB file names: `main.<version>.<package name>.obb`. Or add the APK
first: the game's page shows the package name under **Details**.
