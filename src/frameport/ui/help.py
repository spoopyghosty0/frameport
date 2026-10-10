"""Help texts for the GUI's non-obvious terms and actions, shown as "?" hints and tooltips (components.help_icon).
One place for the wording, so the same term is explained the same way everywhere. Plain text, no Flet.
The English texts are the translation keys (frameport.i18n): HELP[key] returns the translated text.
Style (docs/STYLE.md): at most three short sentences, about 200 characters; say what the user does or gets."""
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
    "status": "How well the game runs on the Steam Frame. \"Works\" and \"Works with issues\" were tested on a Frame; "
              "\"Untested\" is a suggestion nobody has confirmed yet. \"Can't run\" means a known blocker.",
    "update_ready": "The recipe or your game files changed since the install. Update to install the new build; saves "
                    "are kept.",
    "check_exe": "FramePort isn't sure which program starts this game. Open the game and use … → Change program… to "
                 "pick it.",
    "platform_quest": "A Meta Quest game. FramePort patches it for the Frame, where it runs in Lepton, Valve's Android "
                      "container.",
    "platform_windows": "A Windows game without VR. On the Frame, Proton runs it in a window from the Steam library.",
    "platform_pcvr": "A Windows VR game. It runs on this PC with SteamVR, or on the Frame with Proton. Oculus-only "
                     "games also need Revive.",
    "platform_android": "An ordinary Android app or game without VR. It's installed unchanged and runs in Lepton, "
                        "Valve's Android container.",
    "platform_android_vr": "A VR app made for another Android headset. OpenXR apps work like Quest games; apps built "
                           "for Pico or HTC Wave can't run on the Frame.",
    "platform_linux": "A Linux program for the Frame's arm64 processor. It's installed unchanged and starts from the "
                      "Frame's Steam library.",
    "linux_app": "An AppImage, folder or .zip/.tar archive with a Linux program, best built for arm64. Builds for "
                 "x86_64 PCs run too, but slower. The app must bring the libraries SteamOS lacks.",
    "appimage": "A Linux app packed into one file. FramePort unpacks it on the Frame once.",
    "select": "Pick several games and install them in one go. FramePort asks every question first, then works "
              "through them in the background.",
    "tags": "Filled tags are your own. Outlined tags come from the engine, VR API and store genres. Filter the "
            "library by either.",
    # ---- game page
    "recipe": "A recipe is the list of patches and settings FramePort applies to a game. Tested recipes come from a "
              "real Frame; the others are suggested. Hover a patch to see why it's there.",
    "standard_fixes": "Patches every game of this kind gets. Customize lists them all.",
    "as_is": "Turn on if your copy is already patched: the APK is installed unchanged. PC VR games are never changed "
             "on this PC.",
    "alt_build": "A second build with different OVRPort patches, for example for Unreal games that close themselves "
                 "right after starting. Turn on to install that build.",
    "patch_details": "Also shows each patch's ID, exactly what it changes and its parameters. Useful when reporting "
                     "a problem.",
    "show_all": "Patches that can't matter for this game are hidden. Show them to turn one on anyway.",
    "where": "Steam Frame: installed on the Frame, with an entry in its Steam library. This PC: a Steam shortcut that "
             "starts the game through Revive, for a headset connected to this PC.",
    "hw_video_decode": "Games with the Hardware video decoding patch play their videos on the Frame's video hardware. "
                       "Turn it off if videos misbehave; games pick it up at their next start. For one game only, put "
                       "FRAMEPORT_NO_HW_VIDEO=1 before %command% in its Steam launch options.",
    "launch_test": "Starts the game on the Frame while nobody wears the headset and reads the log: did it start and "
                   "draw frames. It can't check the picture.",
    "last_session": "After you play a game on the Frame, FramePort reads that session's log: crashes, frame rate and "
                    "moments the game lost focus. Game settings apply at the next start; other patches reinstall the "
                    "game. Your saves stay.",
    "install_links": "Some websites have \"Install with FramePort\" buttons (frameport.app links). FramePort opens "
                     "these links, shows what they offer and installs it when you agree. On macOS, use Add games → "
                     "Add from a link… instead.",
    "linux_x86": "The Frame has an arm64 processor. Programs built for x86_64 PCs run through Valve's translator, "
                 "like Steam's x86 Linux games, but slower. Use an arm64 build when there is one.",
    "flatscreen": "How the app shows in the headset: in VR, or as a flat window like a phone or tablet app. "
                  "Automatic picks a window for apps without VR.",
    "uninstall": "Removes the game from the Frame. Saves are kept, so a reinstall picks up where you left off.",
    "experimental": "Not tested on many games yet. Try it if the game doesn't work without it.",
    "abis": "The processor types the game has code for. The Frame runs only 64-bit ARM (arm64-v8a); 32-bit-only "
            "games can't run on it.",
    "graphics": "OpenGL ES games run through a translation to Vulkan that is stricter than Quest drivers. Vulkan "
                "games run directly.",
    "recipe_source": "Where the recipe came from: catalog (tested on a Frame), heuristics (suggested by FramePort) or "
                     "user (changed by you).",
    # ---- patch categories
    "cat_pcvr": "How a PC VR game starts: Proton runs it on the Frame; Revive lets Oculus games run on SteamVR.",
    "cat_frame": "Patches for what the Frame does differently from a Quest, such as graphics formats and missing VR "
                 "features.",
    "cat_overport": "OVRPort converts Quest games to standard OpenXR. These are its optional patches.",
    "cat_adapter": "Picture, controllers, menus and more for this game. Changes take effect the next time it starts.",
    "cat_device": "Files and settings placed next to the game on the Frame.",
    # ---- Frame
    "developer_mode": "Developer Mode lets FramePort find and connect to the Frame; the setup turns it on for you. "
                      "With it on, you can also open Settings → Developer → Pair new host on the Frame and approve "
                      "FramePort there.",
    "first_time_setup": "The setup turns on Developer Mode, lets FramePort in and installs Lepton if needed. The "
                        "Frame's desktop closes during it, then FramePort connects by itself. No password needed.",
    "password": "The Frame's desktop password, needed only once so FramePort can add its key.",
    "lepton": "Valve's Android container on the Frame. Quest games run in it, one container per game. It needs "
              "Developer Mode.",
    "proton": "Valve's tool that runs Windows games. On the Frame it runs PC VR games; FramePort installs it when "
              "needed (Steam restarts once).",
    "openxr": "The VR runtime games talk to. On the Frame that's SteamVR.",
    "kernel_keys": "Each game start used to use up one kernel key; after about 200 starts no game started. FramePort "
                   "stops that, but used keys only come back after a restart of the Frame.",
    "ui_scale": "Size of text and layout. Under WSL, Automatic follows half of Windows' display scaling (150% → "
                "125%).",
    "app_updates": "FramePort checks GitHub for a new version every few hours. Update now downloads and checks it, "
                   "then restarts FramePort; your games and settings stay.",
    "update_all": "Updates every game marked \"Update ready\", one after another.",
    "rescan": "Scans your game folders again and adds new games. Games already in the library keep their recipe, "
              "tags and art.",
    "files": "Upload, download, rename and delete files on the Frame. Videos, Downloads and Documents are shared by "
             "every Quest game; each game also has its own storage.",
    "live_view": "Shows the headset's view with sound, whatever is running. It plays here where possible, else in "
                 "mpv or your browser. Stop it when you're done: it costs the game some speed.",
    "type_on_frame": "This PC's keyboard types on the Frame while this tab is open. In the headset, select a text "
                     "field, then type here.",
    "free_space": "Deletes copies kept from earlier installs and unfinished uploads. Games and saves stay.",
    "install_drive": "Where new games go on the Frame, such as a microSD card. Installed games stay put; right-click → "
                     "Move to… moves one. Cards formatted as FAT, exFAT or NTFS can't hold games.",
    "move_game": "Copies the game and its saves to the other drive, checks the copy, then removes the old one. The "
                 "game must be closed. A game on a microSD card only starts while the card is in.",
    "vr_overlay": "A SteamVR overlay app: SteamVR on the Frame draws it over whatever you play, Quest and PC VR "
                  "games alike. With 'Start with SteamVR' on, SteamVR starts it by itself; otherwise start it from "
                  "the library before the game.",
    "desktop_entry": "Adds the app to Desktop Mode's app menu and desktop. Some apps work better there with a mouse "
                     "and keyboard.",
    "adapter_settings": "Sharpness, refresh rate, controllers, menus and more for this game. Changes take effect the "
                        "next time it starts.",
    "frame_summary": "Quest ✓: Lepton is installed, so Quest games can run. PC VR ✓: Proton is installed, so PC VR "
                     "games can run on the Frame.",
    # ---- settings
    "data_folder": "FramePort's tools, library, artwork and your games' signing keys. Back it up: an update needs the "
                   "same key, or the game must be reinstalled and loses its saves.",
    "catalog": "Recipes tested on a real Frame: built in, fetched online and the ones you saved as known-good.",
    "revive": "Revive (by LibreVR) lets Oculus Rift games run on SteamVR. FramePort uses your installed Revive if "
              "there is one and never replaces it.",
    "steamvr_pc": "PC VR games on this PC run through SteamVR, so a headset connected to this PC works with them.",
    "frame_agent": "A small helper FramePort runs on the Frame for installs, launch tests and Steam entries. "
                   "FramePort updates it by itself.",
    # ---- sharing / diagnostics
    "share_config": "Opens a prefilled GitHub issue with this game's recipe, so it can become a built-in recipe for "
                    "everyone. You review and submit it in your browser; no game files are sent.",
    "diag_bundle": "A zip with FramePort's logs, the game's recipe and the Frame's details, without game files. "
                   "Addresses, names and Steam IDs are replaced. Attach it to a GitHub issue.",
    "monitor_gpu": "How busy the Frame's graphics chip is, from every program that draws. Near 100% the game can't "
                   "keep its frame rate.",
    "monitor_pressure": "Memory used by everything on the Frame. \"Waiting\" is how often programs waited for memory "
                        "in the last 10 s; when it climbs, the Frame may freeze or close a game.",
    "monitor_power": "What the whole Frame draws right now, with the processor's and graphics chip's share. More "
                     "power means more heat and shorter battery life.",
    "monitor_filter": "Game: the running game's processes plus the busiest others. Steam and SteamVR: Steam, SteamVR "
                      "and the desktop. All: every program. A lock marks programs that would close Steam or SteamVR.",
})
