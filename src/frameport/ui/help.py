"""Help texts for the GUI's non-obvious terms and actions, shown as "?" hints and tooltips (components.help_icon).
One place for the wording, so the same term is explained the same way everywhere. Plain text, no Flet.
The English texts are the translation keys (frameport.i18n): HELP[key] returns the translated text."""
from __future__ import annotations

from ..i18n import tr


class _Translated(dict):
    """A dict of English texts whose lookups return the translation. An unknown key is an error (a typo would
    otherwise show the key itself as the help text)."""

    def __getitem__(self, key: str) -> str:
        return tr(super().__getitem__(key))

    def get(self, key: str, default=None):
        return self[key] if key in self else default


HELP: dict[str, str] = _Translated({
    # ---- game status / library
    "status": "How well the game runs on the Steam Frame. \"Works\" and \"Works with issues\" come from recipes tested "
              "on a real Frame. \"Untested\" means FramePort suggested a recipe from the game's engine and VR API that "
              "nobody has confirmed yet. \"Can't run\" means there's a known blocker (for example a 32-bit-only game).",
    "update_ready": "The copy on your Frame differs from what FramePort would install now (the recipe or your game "
                    "files changed). Update to install the new build; saves are kept.",
    "check_exe": "This game's folder has several programs and FramePort isn't sure which one starts the game. Open the "
                 "game (or right-click → Change executable) to pick it.",
    "platform_quest": "A Meta Quest game (APK). It's rebuilt for the Frame and runs in Lepton, Valve's Android "
                      "container.",
    "platform_windows": "A Windows game without VR, added with \"Add one game folder…\". On the Frame, Proton runs it "
                        "as a window (like Steam's own Windows games); it's in the Frame's Steam library.",
    "platform_pcvr": "A Windows PC VR game (OpenXR, SteamVR or Oculus). It runs on this PC with SteamVR, or on the "
                     "Frame with Proton. Oculus-only games also need Revive (Oculus → OpenXR).",
    "platform_android": "An ordinary Android app or game (no VR). It's installed unchanged and runs in Lepton, Valve's "
                        "Android container.",
    "platform_android_vr": "A VR app made for another Android headset. OpenXR apps are translated like Quest games; "
                           "apps built on a headset maker's own SDK (Pico, HTC Wave) can't run on the Frame.",
    "platform_linux": "A native Linux program built for arm64 (aarch64), like the Frame's own CPU. It's installed "
                      "unchanged and runs directly on SteamOS (no Android container, no Proton), started from the "
                      "Frame's Steam library. VR apps use the Frame's OpenXR runtime.",
    "linux_app": "An AppImage, a folder or a .zip/.tar archive with a Linux program built for arm64 (aarch64). x86_64 "
                 "builds can't run on the Frame. The app must bring the libraries SteamOS doesn't have.",
    "appimage": "An AppImage is a Linux app packed into one file. FramePort unpacks it on the Frame once, so it starts "
                "without FUSE.",
    "select": "Pick several games and install them in one go. FramePort asks every question first, then works through "
              "the queue in the background.",
    "tags": "Your own tags (filled) can be anything you like. Outlined tags are added automatically from the engine, "
            "VR API and store genres. Filter the library by either.",
    # ---- game page
    "recipe": "A recipe is the list of patches FramePort applies to a game before installing it. Known-good recipes "
              "were tested on a Frame; the others are suggested from the game's engine and VR API. Hover a patch "
              "for why it's there.",
    "standard_fixes": "Patches every game of this kind gets (on by default). Customize lists them all.",
    "as_is": "Turn this on if your copy is already patched: an APK is installed unchanged instead of being patched "
             "again. PC VR games are never modified on this PC; the Frame's copy only gets launch fixes.",
    "alt_build": "Some games need a second build with different OVRPort patches (for some Unreal games: one with "
                 "Unreal's ForceQuit removed). Turn this on to install that build instead.",
    "patch_details": "Each patch is described in plain words. Turn this on to also see its id, exactly what it "
                     "changes in the game (useful when reporting a problem) and its parameters.",
    "show_all": "Patches that can't matter for this game (wrong engine or API) are hidden. Show them to force one on "
                "anyway.",
    "where": "Steam Frame: installed on the headset with an entry in its Steam library. This PC: a Steam shortcut on "
             "this Windows PC that starts the game through Revive, for a PC-tethered headset.",
    "launch_test": "Starts the game on the Frame while nobody is wearing it and reads the log: did it start, create a "
                   "VR session and render frames. It can't check what you'd see: tracking only runs with the "
                   "headset on.",
    "uninstall": "Removes the game from the Frame. Saves are kept, so a reinstall picks up where you left off.",
    "experimental": "Not verified on many games yet: try it if the game doesn't work without it.",
    "abis": "The CPU types the game ships code for. The Frame runs only 64-bit ARM (arm64-v8a); 32-bit-only games "
            "can't run on it.",
    "graphics": "OpenGL ES games run on the Frame through Zink (OpenGL on top of Vulkan), which is stricter than "
                "Quest drivers. Vulkan games run natively.",
    "recipe_source": "Where this game's recipe came from: catalog (tested on a Frame), heuristics (suggested by "
                     "FramePort) or user (changed by you).",
    # ---- patch categories
    "cat_pcvr": "How a PC VR game starts: Proton runs the Windows program on the Frame; for Oculus games, Revive "
                "translates the Oculus API to OpenXR.",
    "cat_frame": "Patches for what the Frame's runtime does differently from a Quest (graphics formats, missing OpenXR "
                 "extensions, Lepton's launcher requirements).",
    "cat_overport": "OVRPort converts Quest games from Meta's own VR APIs to standard OpenXR. These are its "
                    "optional patches.",
    "cat_adapter": "Game settings: picture, controllers, menus and more, used by the adapter FramePort adds to Quest "
                   "games. Change them here or from the Frame page; an installed game uses them the next time it "
                   "starts.",
    "cat_device": "Files and environment variables placed next to the game on the Frame.",
    # ---- Frame
    "developer_mode": "Developer Mode (on the Frame: Settings → System → Developer) lets FramePort find the Frame on "
                      "your network and connect to it over SSH. The first-time setup command turns it on for you.",
    "first_time_setup": "The command fetches a small setup script from this app over your local network. It turns on "
                        "Developer Mode (which includes SSH), lets this app's key in and installs Lepton if needed. "
                        "No password needed.",
    "password": "The Frame's desktop password, only needed the first time so FramePort can add its own key. After "
                "that it connects with the key.",
    "lepton": "Lepton is Valve's Android container on the Frame. Quest games run inside it, one container per game. "
              "It needs Developer Mode.",
    "proton": "Proton is Valve's Windows compatibility layer. On the Frame it runs PC VR games. "
              "Its ARM64 build isn't installed by default; FramePort can install it (Steam restarts once).",
    "openxr": "The VR runtime games talk to. On the Frame that's SteamVR.",
    "kernel_keys": "Every game start on the Frame used to leak one kernel key, and after about 200 launches every game "
                   "fails to start. FramePort switches that leak off, but keys already used only come back after a "
                   "restart of the Frame.",
    "ui_scale": "Size of text and layout. Automatic follows Windows' display scaling halfway "
                "(150 % in Windows → 125 %) when FramePort runs under WSL, where the window doesn't get it from "
                "Windows itself.",
    "app_updates": "FramePort checks GitHub for a new release every few hours and shows it in the sidebar and the "
                   "Library. \"Update now\" downloads it, checks its checksum (and on Windows its signature), closes "
                   "FramePort and opens the new version; your games, settings and Frame connection are kept. With "
                   "automatic install, the download happens in the background and the new version starts next time.",
    "update_all": "Updates every game marked \"Update ready\" (a newer build, or patch settings changed since it was "
                  "installed), one after another.",
    "rescan": "Scans the folders you added games from again and adds games that appeared since. Games already in the "
              "library keep their recipe, tags and art; unchanged ones aren't analyzed again.",
    "files": "Browse and manage files on the Frame (Files tab): upload from this PC, download, rename, delete. "
                  "Videos, Downloads and Documents are shared by every Quest game (/sdcard/Movies, /sdcard/Download, "
                  "/sdcard/Documents); each installed game also has its own storage (its /sdcard). Apps find files "
                  "by browsing folders. Video players that list only their own folder (e.g. 4XVR's Internal "
                  "Storage = 4XPlayer): upload into that folder in the game's storage.",
    "free_space": "Deletes the rollback copies kept from each game's previous install and leftover uploads. The "
                  "games and saves stay.",
    "adapter_settings": "Sharpness, refresh rate, controllers, menus and more for this game. Changes are used the "
                        "next time it starts.",
    "frame_summary": "Quest ✓: Lepton is installed, so Quest games can run. PC VR ✓: Proton is installed, so PC VR "
                     "games can run on the Frame.",
    # ---- settings
    "data_folder": "FramePort's tools, library, artwork and the signing keys of your rebuilt games. Back it up: an "
                   "update must be signed with the same key, or the game (and its saves) has to be reinstalled.",
    "catalog": "Recipes tested on a real Frame: bundled with FramePort, fetched from the online catalog, and the ones "
               "you saved as known-good.",
    "revive": "Revive (by LibreVR) lets Oculus Rift games run on OpenXR headsets. FramePort uses your installed Revive "
              "if you have one, and never replaces it.",
    "steamvr_pc": "PC VR games on this PC run through SteamVR, so a headset connected to this PC works with them.",
    "frame_agent": "The small helper program FramePort runs on the Frame (installs, launch tests, Steam entries). It "
                   "comes with this app and is replaced on the Frame automatically whenever it differs.",
    # ---- sharing / diagnostics
    "share_config": "Sends this game's recipe (the patches and settings it uses) to FramePort's GitHub as a prefilled "
                    "issue, so it can become a built-in recipe for everyone. You review and submit it in the browser; "
                    "no GitHub token and no game files are involved.",
    "diag_bundle": "A zip with FramePort's logs, the game's recipe and analysis, launch-test logs and the Frame's "
                   "runtime details — enough to debug without the game files. IP addresses, host and user names, "
                   "home folders and Steam ids are replaced by placeholders. Attach it to a GitHub issue.",
    "monitor_gpu": "How much of the time the Frame's graphics chip was busy, added up from every program that draws "
                   "(the game, SteamVR's compositor, Steam). Near 100 % the game can't keep its frame rate.",
    "monitor_pressure": "Memory in use by everything on the Frame. \"Waiting\" is the share of time programs had to "
                        "wait for memory in the last 10 s: when it climbs, the Frame is running out and may freeze or "
                        "close a game.",
    "monitor_power": "What the whole Frame draws right now (its main power rail), with the share of the CPU and the "
                     "graphics chip. Higher power means more heat and a shorter battery.",
    "monitor_filter": "Game: the running FramePort game's processes (its Android container or Proton) plus the busiest "
                      "others. Steam & SteamVR: Steam, SteamVR and the desktop. All: every program of your user on "
                      "the Frame. A lock marks programs whose end would close Steam, SteamVR or the desktop.",
})
