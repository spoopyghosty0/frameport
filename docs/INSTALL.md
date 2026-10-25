# Install and first steps

▶ **[Watch the install tutorial](media/frameport-install.mp4)** (about 90 seconds): from the download to the first
game on the Steam Frame.

[![The install tutorial](media/frameport-install.jpg)](media/frameport-install.mp4)

Download the file for your PC from the [latest release](https://github.com/spoopyghosty0/frameport/releases/latest)
and unpack it anywhere. No installer or admin rights are needed.

| Your PC | Download | Start |
|---|---|---|
| Windows 10/11 (x64) | `FramePort-windows-x64.zip` | `FramePort.exe` |
| macOS (Apple Silicon) | `FramePort-macos-arm64.zip` | `FramePort.app` |
| Linux (x64, Ubuntu 22.04 or newer) | `FramePort-linux-x64.tar.gz` | `FramePort/FramePort` |
| Linux (ARM64, Ubuntu 22.04 or newer) | `FramePort-linux-arm64.tar.gz` | `FramePort/FramePort` |
| Arch Linux (AUR, community package) | [`frameport-bin`](https://aur.archlinux.org/packages/frameport-bin) | `frameport` |

On first start FramePort downloads the tools it uses (Settings → Tools shows them).

## First launch

FramePort isn't signed with a paid certificate, so the first start shows a warning:

- **Windows:** "Windows protected your PC" → **More info** → **Run anyway**.
- **macOS:** right-click `FramePort.app` → **Open** → **Open** (once).
- **Linux:** `tar xzf FramePort-linux-x64.tar.gz && ./FramePort/FramePort`. Arch Linux: `yay -S frameport-bin`.

Starting FramePort again brings its open window to the front.

## Connecting the Steam Frame

The Frame and your PC must be on the same network (or connected with a [USB cable](#with-a-usb-cable)).

1. In FramePort open **Steam Frame** and click **Start setup**. Keep that page open.
2. On the Frame, first time only:
   1. Open the **SteamVR dashboard → Launch a program → Desktop**. The Frame's desktop opens.
   2. Open the app menu (bottom left) → **System → Konsole**, the Frame's terminal.
   3. Type this setup line and press **Enter** (on-screen keyboard or any USB or Bluetooth keyboard):

      ```
      curl -fsSL https://frameport.app/s | bash
      ```

      No keyboard? Open [the setup page](https://frameport.app/setup/) in Chromium on the Frame,
      tap **Copy** and paste it into Konsole.
   4. Konsole shows a 4-digit code. When FramePort shows the same code, click **Allow**. Nothing changes on the Frame
      before that.
   5. Steam restarts once and the desktop closes. If Steam asks to install **Lepton** (Valve's Android runtime),
      confirm it.

FramePort connects within a minute. No password is needed. The setup turns on **Developer Mode**;
[What the setup changes](FRAME_SETUP.md) lists everything.

Later, FramePort connects to the Frame by itself. If you turn Developer Mode off (Settings → System → **Enable
Developer Mode**), turn it on again there.

**Setup command:** if your network blocks FramePort's search, click **Use the setup command**. It shows a line with
your PC's address and a one-time code, for example `curl -fsS 192.168.1.20:8765/1a2b3c4d | bash`. Run it in Konsole instead.

**Without Konsole:** turn on Developer Mode yourself (Settings → System → **Enable Developer Mode**), then open
Settings → Developer → **Pair new host** on the Frame. FramePort finds the Frame and asks to connect; approve it in the
headset. Install Lepton from FramePort's Steam Frame page afterwards if it's missing.

### With a USB cable

A cable works on networks that block the setup, and uploads are faster (about 37 MB/s, three times typical Wi-Fi).

1. On the Frame, turn on Developer Mode (Settings → System → **Enable Developer Mode**). The cable only works in
   Developer Mode.
2. Connect the Frame's USB-C port to your PC. No driver is needed.
3. In FramePort click **Steam Frame → Set up with a USB cable** and follow the steps.

Uploads use the cable whenever it's plugged in. Unplug it any time: FramePort finds the Frame on Wi-Fi again.

### Firewalls

If the setup only says "timed out", a firewall on your PC blocks the Frame. After about 45 seconds the setup page
says which. Details: [Network and firewalls](FRAME_SETUP.md#network-and-firewalls).

## Running FramePort on the Frame (experimental)

FramePort can run on the Frame itself, without a PC:

1. Turn on Developer Mode (Settings → System → **Enable Developer Mode**).
2. In Desktop Mode, download `FramePort-linux-arm64.tar.gz`, unpack it (`tar xzf FramePort-linux-arm64.tar.gz`) and
   start `FramePort/FramePort`.

Games appear in Steam after you close FramePort and return to Gaming Mode (see [When games appear in
Steam](#when-games-appear-in-steam)). Report anything odd with **Report a problem…**.

**Moving from a PC:**

1. Copy the signing keys first: the `<package>.keystore` files in
   `%LOCALAPPDATA%\FramePort\overport-workspace\signatures\`
   (Linux/macOS: FramePort's data folder, `overport-workspace/signatures/`) go to
   `~/.local/share/frameport-app/overport-workspace/signatures/` on the Frame, before FramePort there builds those
   games. Otherwise it makes new keys: updates are signed differently from the installed games, and games that keep
   progress per Meta account get a different account id, so their progress can look gone.
2. Put the original game files on the Frame and add that folder to the Library there; the PC's library doesn't carry
   over, and updates are built from the originals. Data files on the same drive as the installs are linked into the
   install instead of copied, so they take space once.
3. Games already installed keep working; installing them again from the Frame keeps their saves. Don't manage the
   same game from both the PC and the Frame unless both have the same keys.

## Installing games

![Game page](images/game.png)

- **Install on Frame** on a game's page, or select several games in the Library and install them together. Installs
  run in the background; **Activity** shows the current one.
- **Update all** updates every game whose build changed, for example after a FramePort update.
- If the Frame sleeps or leaves the Wi-Fi, installs wait and continue when it's back. While installs run, the Frame
  stays awake.
- Your own game files are never changed. The patched copy is deleted once the game is on the Frame (Settings →
  Installing).
- In the downloaded app you can also **drag files onto the Library**: games, Linux apps, Windows programs or folders.
- App bundles (`.xapk`, `.apks`, `.apkm`, `.apkx` or a folder of split APKs) are added like an APK. FramePort merges
  their parts into one APK; see [the FAQ](FAQ.md#can-i-add-xapk-apks-apkm-or-apkx-files-app-bundles).

A game's **menu** opens with a right-click in the Library or the **…** button on its page; its groups (**Update**,
**On the Frame**, **Saves**, **Name and artwork**, **Recipe**, **Troubleshoot**) open to the side.

**Game settings…** (in the game's menu) shows the settings that matter for that game in plain words: sharpness,
refresh rate, controllers, menus, 360° video and mixed reality. Changes are used the next time the game starts.

![Game settings](images/game-settings.png)

**Rename…** (game menu → **Name and artwork**) changes the name in the Library and in Steam; FramePort keeps it
from then on. An installed game keeps its Steam entry, artwork and saves (Steam restarts once).

### When games appear in Steam

Steam only reads new library entries when it starts. Settings → Installing → **Steam library** sets when that happens:

- **Now** (default): Steam restarts after an install, but waits while a game, Desktop Mode or FramePort's window
  on the Frame is open (up to 12 hours). With FramePort on the Frame, games appear once you close it and return
  to Gaming Mode.
- **Later**: Steam isn't restarted; new games appear at its next start (also after a reboot). **Restart Steam
  now** adds them at once; it closes Desktop Mode and FramePort on the Frame, and asks first if a game is running.
- **Off**: no new Steam entries (existing ones stay). FramePort's **Play** starts the game directly; Steam and
  SteamVR must be running, and Steam's overlay, controller setup and Exit game don't apply to it (end it from
  **Monitor**). Games waiting for **Later** start this way too.

Command line: `frameport frame steam-library now|later|off`, `frameport frame restart-steam [--yes]`. Installing
Proton can still restart Steam whatever this setting says.

### microSD cards and other drives

- The **Steam Frame** page's **Storage** section sets where new games go (**Install new games to**).
- To move an installed game: game menu → **On the Frame** → **Move to…**. The game must be closed; saves and the Steam entry stay.
- A game on a card only starts while the card is inserted.
- Cards formatted as FAT, exFAT or NTFS can't hold games: format the card in SteamOS first.

### Controller buttons

Buttons repeat or do the wrong thing? SteamVR maps Quest Touch onto the Frame's controllers, and three top buttons
repeat Touch Y/B. **Game settings → Controllers → Controller buttons** picks **Mirror** or **Quest pair** for one
game; **Settings → Controllers** sets the default (**Apply to installed games now** sends it to games without
their own choice). Changes apply at the next start. Diagram and limits: [Controller buttons](CONTROLLERS.md).

### Android apps and Windows programs

- Android apps without VR are installed unchanged and shown as a window in the headset. If one shows nothing (or a
  VR app opens as a window), open **Customize** on the game's page, set **Show as VR or as a flat window** and click
  **Update on Frame**.
- **Add games → Add a Windows program…** adds a single Windows program. The Frame runs it as a window through Proton
  (Valve's tool for running Windows programs).

### Install links ("Install with FramePort" buttons)

Some websites have an **Install with FramePort** button (a `https://frameport.app/install?…` link):

- **Click a button** on Windows or Linux: FramePort opens, shows what it would download and asks first. Then it adds
  the game and installs it on the Frame.
- **Add games → Add from a link…** takes a button's address (right-click → Copy link) or a direct download link. Use
  it on macOS, where buttons can't open FramePort yet.
- **Settings → Install links** turns the buttons on or off.
- Only `https://` links to public servers are used. Only install from sites you trust.

Website owners: see [Install button](INSTALL_BUTTON.md).

## Typing on the Frame

- **Type on Frame** (in the sidebar): while this tab is open, your PC's keyboard types on the Frame. Select a text
  field in the headset and type, or paste longer text into the box.
- **Steam's on-screen keyboard** works in apps shown as a window. Steam lists that window as **Gamescope**: leave it
  open.
- **Unity games whose text fields close at once:** FramePort suggests the patch **Make Unity text fields work**.
  Games added before this patch existed: game menu → **Troubleshoot** → **Analyze again**, then **Update on Frame**.

![Type on Frame](images/type-on-frame.png)

## Watching the Frame

- **Monitor** (in the sidebar) shows the running game's frame rate, CPU, graphics, memory, temperature, power and
  battery, each with a 2-minute chart. **Show details** shows more.
- Under **Processes**, right-click a process to end it. **End game** closes the game like Steam's Exit game. A lock
  marks programs Steam or the desktop needs; FramePort asks again before ending those.
- **Live view** streams what the headset shows, with sound, to your browser. It uses about one CPU core of the Frame,
  so stop it when you're done.
- **Screenshots** shows the screenshots you took in the headset, sorted by game and day, to view or download.

## Your room (mixed reality, experimental)

Mixed-reality games ask for your room: walls, furniture and its surfaces. **Room** (in the sidebar) keeps it:

- **Scan room in the headset**, then put the headset on with passthrough and look around for about a minute; the tab
  shows how far the scan is. **Cancel scan** stops it and keeps your saved room. **Use my play area** makes walls from
  the SteamVR play area instead (no furniture).
- The room shows in 3D: drag to turn it, scroll to zoom, right-drag to move it, double-click to see all of it.
  **Open full 3D view** shows every detail of the scan in your browser (a page on this PC only).
- **Clean-up** tidies every scan (flat walls, no scan behind walls or where you walked, no loose bits). The
  recommended settings suit most rooms; change them (**More settings** has all of them) and press **Apply** to clean
  up your saved scan again. Your settings are also used for your next scans.
- **Labels** shows what the scanner's boxes became and why. A tall, thin box by a wall can be **Add as door** or
  **Add as closet**.
- **Furniture & screens**: pick a box in the list or click it in the 3D view to change its type, place or size, add
  or delete boxes, and add doors and windows on a wall. **Save** sends them to the Frame; they stay when you clean up
  the scan again (**Back to the scan's boxes** under Labels drops them).
- The free **Test Quest MR** app (Get Test Quest MR on the tab) shows what a game receives from your scan.

![Room](images/room.png)

![Room: a box picked to edit, and the labels of the clean-up](images/room-editing.png)

## Updating

FramePort checks for a new version at start and every 6 hours. When there is one, the Library shows **Update now**:
FramePort downloads it, checks it, restarts and keeps your games and settings. **Later** skips that version.

- Settings → **Updates** turns the check off or turns on **Install updates automatically**.
- **Recipes** (the tested patches and settings for each game) update by themselves. A game whose recipe changed shows
  **Update on Frame**.
- **Moved your games?** Scan the new folder (or **Rescan folders**): games found there whose old files are gone keep their library entry, name, artwork, tags and settings, and only their location changes. **Settings → Library folders** lists the folders FramePort scans; remove the old one there (the games stay). See the [FAQ](FAQ.md#i-moved-my-games-to-another-folder-or-drive-how-do-i-update-the-library).
- **A newer version of a game:** game menu → **Update** → **Update with a newer APK…** (Quest and Android games) or **Update with a newer version…** (Linux apps: the new AppImage, archive or
  folder; PC VR games: the new game folder). Its name, artwork, tags and your settings stay. Then click **Update on
  Frame** (or **on this PC**).
- **Dev builds:** if you're asked to test a change before its release, use Settings → Updates → **Install the latest
  dev build…**. The next release then arrives as a normal update.
- **Installed from the AUR?** Update with your AUR helper (`yay -Syu`); FramePort can't update itself in `/opt` and
  opens the release page instead. New releases reach the AUR once the community package is updated.

## PC VR games

PC VR games are Windows VR games. Scan a folder of them (one folder per game) or use **Add games → Add a PC game
folder…**. FramePort asks which program starts the game if it finds more than one.

- **Play from this PC** (Windows with Steam and SteamVR): **Install on this PC** adds the game to Steam. Stream it to
  the Frame with Steam Link.
- **Play on the Frame (experimental):** on the Steam Frame page, install *PC VR games (Proton)*, then click **Install
  on Frame** on the game's page.
- **Oculus games** (made for Meta's Rift headset) need Revive, which FramePort downloads. Games that check their
  license through the Oculus app only run on your PC.

**Windows games without VR:** use **Add games → Add a PC game folder…** too. The Frame runs them as a window through
Proton, like Steam's own Windows games. Whether a game runs depends on Proton.

## Linux apps

**Add games → Add a Linux app…** takes an AppImage (a single-file Linux app) or a `.zip`/`.tar.gz`/`.tar.xz`
archive; **Add a Linux app folder…** takes an unpacked app. **Install on Frame** uploads it unchanged and adds it to
the Steam library.

- Apps built for **arm64** (the Frame's processor) run directly. Apps built for **x86_64** (most PCs) run through FEX,
  a translator Steam installs on the Frame the first time; they run slower. The game page shows which kind you have.
- If the app needs libraries the Frame doesn't have, the game page lists them: look for a build that includes them.
- Linux apps also appear in **Desktop Mode**'s app menu. Turn this off on the game
  page (**Desktop Mode**).
- **SteamVR overlay apps** (a wrist watch, a performance display: programs drawn over games) are recognised by
  their `.vrmanifest` file and shown over any game, Quest and PC VR alike. They start with SteamVR (**Start with
  SteamVR** on the game page); with that off, start the app from your library before the game. Windows overlay
  apps get the patch **SteamVR overlay app (Frame)**.

## Files on the Frame

The **Files** tab copies files between your PC and the Frame: videos, documents, mods and saves.

![Files](images/files.png)

- Use **Upload files**, **Upload folder** and **New folder**; right-click an entry to download, rename or delete
  it. Drag across entries to select several.
- In the downloaded app you can drag files from your PC onto the list.
- **Videos**, **Downloads** and **Documents** are shared by every Quest game (inside the game: `/sdcard/Movies`,
  `/sdcard/Download`, `/sdcard/Documents`).
- **Game storage** holds each game's own files. Game menu → **On the Frame** → **Add videos and files** opens it.

## Backing up saves

Steam doesn't sync Quest games' saves. **Back up saves** next to **Play** on a game's page copies them to your PC
(close the game first); **Back up all saves** on the Steam Frame page does every installed Quest game. The game's
menu → **Saves** → **Restore saves…** puts a backup back and keeps the replaced saves for **Undo last restore**.
Settings → **Data** sets the backup folder (a cloud-sync folder works), how many backups to keep (10 by default) and
**Back up saves when a game closes**. Details: [FAQ](FAQ.md#can-i-back-up-a-games-saves).

## Share a recipe or report a problem

- **Share working recipe…** (game menu → **Recipe**, or under **What FramePort will do** on the game's page) opens
  a GitHub issue with the game's recipe filled in. Accepted recipes become built-in for everyone. For untested games
  FramePort asks how they ran after you install or test them.
- **Report a problem…** (game menu → **Troubleshoot**, or Settings → Problems and feedback) saves a diagnostics zip with
  personal data removed and opens a GitHub issue to attach it to.

Without the app: [share a recipe](https://github.com/spoopyghosty0/frameport/issues/new?template=working-config.yml)
· [report a problem](https://github.com/spoopyghosty0/frameport/issues/new?template=bug-report.yml).

## Uninstalling

Settings → **Uninstall FramePort…** removes FramePort's data and, if you choose, its games on the Frame (saves can be
kept). It first saves your signing keys to Documents: you need them to update your games later. Then delete the
FramePort folder.

## Verify a download

`SHA256SUMS.txt` in each release lists the files' checksums: `sha256sum -c SHA256SUMS.txt`. Each file also has a
GitHub build attestation (proof that GitHub built it from this repository):
`gh attestation verify FramePort-windows-x64.zip -R spoopyghosty0/frameport`.

Windows: to show FramePort as the publisher, import `FramePort-selfsigned.cer` (attached to each release) into
*Trusted Root Certification Authorities* (Current User). The certificate can only sign code; remove it with
`certmgr.msc`. Its SHA-256 fingerprint:
`4E:12:98:91:62:C0:E4:50:FB:65:1D:34:BB:73:00:09:7B:78:BE:88:5C:A7:6C:42:23:46:9B:92:A1:59:A7:6E`.

## For power users: the command line

The **command-line version** (Python 3.11 or newer) installs from the release's
`frameport-<version>-py3-none-any.whl` with `uv tool install <link>` (or pipx or pip).

Almost everything the app does is also a `frameport` command; `frameport --help` and `frameport <command> --help`
list every option. The main ones:

| Command | What it does |
|---|---|
| `scan <folder>` / `list` / `show <game>` | add games, list the library, show a game's patches |
| `rename <game> "<name>"` | rename a game in the library and in Steam (`--reset` goes back to the automatic name) |
| `folders [--add <folder>] [--remove <folder>]` / `rescan` | list the library folders, add or remove one (its games stay); scan them again for new and moved games |
| `add-linux <path> [--exe <program>]` | add a Linux app (AppImage, folder or archive) |
| `new-version <game> <path>` | point a game at its newer version (APK, Linux app file/folder, PC VR game folder) |
| `recipe <game> --enable/--disable <patch>` | change a game's patches (`patches` lists them all) |
| `build <game>` / `install <game>` / `test <game>` | build, install on the Frame (`--to pc` for this PC), launch test |
| `session <game>` / `settings <game> <key>=<value>` | check the last play session; change a game setting on the Frame without rebuilding |
| `frame discover` / `frame connect` / `frame info` | find, connect to and describe the Frame |
| `frame send <files> --dest videos` / `frame storage` / `frame cleanup` | copy files to the Frame, list where they can go, free space |
| `frame drives` / `frame move <game> --to <drive>` / `install --dest <drive>` | list the Frame's drives, move a game, install to a drive |
| `saves backup <game>\|--all` / `saves list <game>` / `saves restore <game> [n]` / `saves undo <game>` | back up Quest games' saves to this PC, restore one, undo the last restore |
| `tools status` / `tools install` | the tools FramePort downloads |
| `open-link "<link>"` | install from an install button's address or a download link (`--yes`, `--no-install`) |
| `diag report <game>` / `share-recipe <game>` | report a problem / share a working recipe |
| `update` | update FramePort (`--check` only checks: exit code 10 = update available) |
| `uninstall-app` | uninstall FramePort |

Exit codes: 0 done, 1 failed, 2 wrong usage. `FRAMEPORT_DEBUG=1` shows full error details;
`FRAMEPORT_NO_UPDATE_CHECK=1` turns update checks off.
