# What the setup changes

For the setup steps see [Install and first steps](INSTALL.md#connecting-the-steam-frame). This page lists what the
setup changes on the Steam Frame, how to undo it and what your network needs.

## Changes on the Frame

The setup runs [`bootstrap/bootstrap.sh`](../bootstrap/bootstrap.sh), which FramePort sends from your PC. It changes:

| Change | Where | How to undo |
|---|---|---|
| Turns on **Developer Mode** if it's off. Steam restarts once, which closes the desktop; the rest finishes on its own (log: `~/.cache/frameport-setup.log`). | `"DevModeEnabled" "1"` in `~/.local/share/Steam/config/config.vdf` (old copy kept as `config.vdf.before-frameport`), then Valve's own Developer Mode helper. | Settings → System → **Enable Developer Mode** off. |
| Lets FramePort log in. | One line ending in `frameport` in `~/.ssh/authorized_keys`. | Delete that line. |
| Stops game starts from failing after about 200 launches (a limit in podman, the tool Lepton runs games with). | `[containers]` / `keyring = false` in `~/.config/containers/containers.conf`. | Remove those lines. |
| Asks Steam to install **Lepton** (Valve's Android runtime) if it's missing. You confirm it in Steam. | Steam library | Uninstall it in Steam. |

Valve's Developer Mode helper is the same one the Settings switch uses. It turns on remote login (SSH), the service
that makes the Frame findable on your network, remote desktop, debugging and crash dumps; turning Developer Mode off
turns them all off again.

The script runs as your user: no root, no `sudo`, no password. Nothing else on the system changes. It also leaves two
files: `~/.cache/frameport-setup.sh` (the part that finishes on its own) and its log.

If Developer Mode can't be turned on automatically, the script asks you to turn it on in Settings → System →
**Enable Developer Mode** and run the setup line again.

**Later, FramePort adds** (as your user):
- its helper in `~/.local/share/frameport/`;
- the games, each with a launcher and its data in `~/Applications/quest-frame/<package>/`;
- their Steam library entries and artwork (`shortcuts.vdf` + `config/grid/`);
- for PC VR games: an OpenXR layer (`~/.local/share/openxr/1/api_layers/explicit.d/XR_APILAYER_FRAMEPORT_timefix.json`)
  and Valve's ARM64 Proton with its Steam Linux Runtime (Steam downloads them and restarts once).

**Settings → Uninstall FramePort → Also remove from the Frame** deletes the games, their Steam entries, the helper
folder, the OpenXR layer and the setup files. Developer Mode, the login line, the podman setting, Lepton and Proton
stay: undo them as shown above.

## Network and firewalls

The setup is the only time the Frame connects to your PC. It downloads the setup script from FramePort on TCP port
8765 (8766 or 8767 if taken), only while the setup page is open and for at most 30 minutes. Everything else goes
from your PC to the Frame.

How the setup line finds your PC:

1. FramePort announces itself on your network while the setup page is open (with your PC's name and two words, never
   the code). If nothing answers, the setup line tries the USB cable's address and then scans the Frame's network.
2. Both sides show the same 4 digits. Nothing happens until you click **Allow** in FramePort.
3. FramePort then hands over the one-time code, and the Frame downloads the same setup script as the setup command.

The setup line's script is [bootstrap/setup.sh](../bootstrap/setup.sh). If the setup just says "timed out", the
setup page shows what is likely blocking it after about 45 seconds:

- **Windows:** allow FramePort when Windows asks. On a network Windows treats as **Public** it stays blocked; set
  your home network to Private in Windows' network settings.
- **macOS:** with the firewall on (System Settings → Network → Firewall), allow incoming connections for FramePort
  when asked.
- **Linux:** firewalld: `sudo firewall-cmd --add-port=8765/tcp` (until the next restart). ufw:
  `sudo ufw allow 8765/tcp`, afterwards `sudo ufw delete allow 8765/tcp`.
- **WSL** (FramePort's Linux version on Windows): Windows blocks connections into WSL without asking. FramePort
  adds a temporary firewall rule (one admin prompt) and removes it when setup is done or after 35 minutes. WSL must
  share Windows' network: `networkingMode=mirrored` under `[wsl2]` in `%UserProfile%\.wslconfig`, then
  `wsl --shutdown`.
- Or skip the setup line and use **Pair new host** (see [Install and first steps](INSTALL.md#connecting-the-steam-frame)):
  it needs no connection into your PC.
