# Frame setup: what changes, networks, undoing it

For the steps themselves see [INSTALL.md](INSTALL.md#connecting-the-steam-frame).

## What the setup changes

The setup command runs [`bootstrap/bootstrap.sh`](../bootstrap/bootstrap.sh), served by the app over your
local network. Everything it changes:

| Change | Where | How to undo |
|---|---|---|
| Turns on **Developer Mode** (only if it's off). Steam restarts once, which closes Desktop Mode; the rest of the setup finishes on its own as a user service (log: `~/.cache/frameport-setup.log`). | `"DevModeEnabled" "1"` in `~/.local/share/Steam/config/config.vdf` (old file kept as `config.vdf.before-frameport`), then Valve's own `steamos-polkit-helpers/steamos-devkit-mode --enable`. That helper enables the SSH server (`sshd`), the devkit service that makes the Frame findable on the network, the remote-desktop and debug services, and system crash dumps. | Settings → System → Developer → Developer Mode off. Valve's helper switches all of those services off again. |
| Lets the app's SSH key in. | One line ending in `frameport` in `~/.ssh/authorized_keys`. The folder and file are created if missing. | Delete that line. |
| Configures podman for Lepton. Rootless podman leaks one kernel keyring per container start, and after about 200 game starts every game fails. | `[containers]` / `keyring = false` in `~/.config/containers/containers.conf`. | Remove those lines. |
| Asks Steam to install **Lepton** (Valve's Android runtime, Steam app 3029110) if it's missing. You confirm it in Steam. | Steam library | Uninstall it in Steam. |

The script runs as your user: no root, no `sudo`, no password. The only system-level change, Developer Mode, is
made by Valve's own helper, the same one the Settings switch uses. If Developer Mode can't be turned on
automatically, the script asks you to turn it on in Settings → System → Developer and run the command again.

Nothing else on the system is touched: no packages, no read-only-filesystem changes, no polkit rules. The script
also leaves two files: `~/.cache/frameport-setup.sh` (the part that runs on its own) and its log.

**Later, the app adds** (all as your user, no root, no `sudo`):
- FramePort's helper in `~/.local/share/frameport/`;
- the games, each with a launcher and its data in `~/Applications/quest-frame/<package>/`;
- their Steam library entries and artwork (`shortcuts.vdf` + `config/grid/`);
- for PC VR games: an OpenXR layer (`~/.local/share/openxr/1/api_layers/explicit.d/XR_APILAYER_FRAMEPORT_timefix.json`)
  and, when the first PC VR game is installed, Valve's ARM64 Proton and its Steam Linux Runtime (Steam downloads them;
  Steam restarts once).

**Settings → Uninstall FramePort → Also remove from the Frame** deletes the games, their Steam entries, the helper
folder, the OpenXR layer and the setup script's files. Developer Mode, the SSH key line, the podman setting, Lepton
and Proton stay. Undo them as shown above or in Steam.

## Network and firewalls

The setup is the only time the Frame connects to your computer: it downloads the script from FramePort on TCP port
8765 (8766/8767 if taken), only while the setup page is open and for at most 30 minutes. With the setup line from
the project page (`curl -sL spoopyghosty0.github.io/frameport/s | bash`, the script is
[bootstrap/setup.sh](../bootstrap/setup.sh)) the Frame first finds FramePort: FramePort announces itself over mDNS
(`_frameport-pair._tcp`, with this computer's name and two words, never the code) while that page is open; without
an answer the script tries the USB cable's address and the Frame's network on port 8765. It then asks FramePort, and
only after you click **Allow** (both sides show the same 4 digits) does FramePort hand over the one-time code, which
fetches the same setup script as the typed command. Everything
else goes from the computer to the Frame. If the command just says "timed out", the setup page shows what is likely
blocking it after about 45 seconds:

- **Windows:** allow FramePort (or Python, when running from source) when Windows asks. On a network Windows treats as
  **Public** it stays blocked unless you allow public networks; set your home network to Private in Windows'
  network settings instead.
- **macOS:** with the firewall on (System Settings → Network → Firewall), allow incoming connections for FramePort
  when asked.
- **Linux:** firewalld: `sudo firewall-cmd --add-port=8765/tcp` (until the next restart). ufw:
  `sudo ufw allow 8765/tcp`, afterwards `sudo ufw delete allow 8765/tcp`.
- **WSL:** Windows' Hyper-V firewall blocks connections into WSL without asking. FramePort adds a temporary rule for
  the setup ports (one admin prompt) and removes it again when setup is done or after 35 minutes. WSL must use
  mirrored networking: `networkingMode=mirrored` under `[wsl2]` in `%UserProfile%\.wslconfig`, then `wsl --shutdown`.
- Or skip the setup command and use the devkit pairing (see [INSTALL.md](INSTALL.md#connecting-the-steam-frame)): it needs no connection into your computer.
