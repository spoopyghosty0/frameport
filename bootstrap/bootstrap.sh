#!/usr/bin/env bash
# FramePort one-time Steam Frame setup. Run in the Frame's desktop terminal (SteamVR dashboard -> Launch a program
# -> Desktop, then System -> Konsole):
#   curl -fsS <pc-ip>:<port>/<code> | bash
# (the FramePort app shows the exact line and serves this script while its Setup page is open).
#
# What it does (no password, no sudo):
#   1. authorizes the FramePort app's SSH key (no password needed afterwards)
#   2. configures podman for Lepton (avoids a kernel keyring leak after ~200 game starts)
#   3. turns on Developer Mode if it's off (Valve's own helper; it also enables the SSH server and announces the
#      Frame on the network as a SteamOS devkit). Steam restarts for that, which closes Desktop Mode: the rest
#      runs on its own as a user service and the Frame returns to its normal view.
#   4. asks Steam to install Valve's Lepton Android runtime if it's missing, then tells the app it's done
# Log of the part that runs on its own: ~/.cache/frameport-setup.log
set -euo pipefail
PC_URL="__PC_URL__"          # filled in by the app when serving the script
PAIR_CODE="__PAIR_CODE__"
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

DEVKIT_HELPER=/usr/bin/steamos-polkit-helpers/steamos-devkit-mode
STEAM_CONFIG=~/.local/share/Steam/config/config.vdf
JOB=~/.cache/frameport-setup.sh
LOG=~/.cache/frameport-setup.log

# Desktop Mode on SteamOS is a nested Plasma session inside steam.service, with its own XDG_RUNTIME_DIR and D-Bus:
# systemctl/systemd-run --user only reach the user's systemd through the real runtime dir.
user_systemd() {
    XDG_RUNTIME_DIR="/run/user/$(id -u)" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus" "$@"
}

say "FramePort setup for $(hostname) ($(. /etc/os-release; echo "$NAME $VERSION_ID"))"

say "Letting FramePort log in"
key=$(curl -fsS "$PC_URL/key?code=$PAIR_CODE")
[[ "$key" == ssh-ed25519\ * ]] || { echo "Couldn't reach FramePort at $PC_URL. Is it still open?"; exit 1; }
mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
grep -qxF "$key" ~/.ssh/authorized_keys || echo "$key" >> ~/.ssh/authorized_keys

say "Setting up Lepton's containers"
# rootless podman leaks one kernel keyring per container start; ~200 game launches would exhaust the quota
mkdir -p ~/.config/containers
grep -qs '^ *keyring *=' ~/.config/containers/containers.conf || printf '[containers]\nkeyring = false\n' >> ~/.config/containers/containers.conf

# The rest: Developer Mode (if needed), Lepton, telling the app. As a file, so it can run as its own user service:
# stopping Steam ends Desktop Mode and every program started in it, this terminal included.
mkdir -p ~/.cache
cat >"$JOB" <<'JOB'
set -u
MODE=$1 PC_URL=$2 PAIR_CODE=$3 CONFIG=$4 HELPER=$5
log() { echo "$(date +%T) $*"; }
if [[ $MODE == devmode ]]; then
    sleep 5  # let the user read the terminal before Desktop Mode closes
    # Steam re-applies "DevModeEnabled" from config.vdf at every start (calling Valve's helper) and rewrites
    # config.vdf when it exits: stop Steam, set the value, run the helper (pkexec, allowed without a password), start.
    log "stopping Steam"
    systemctl --user stop steam.service
    for _ in $(seq 40); do pgrep -x steam >/dev/null || break; sleep 1; done
    if pgrep -x steam >/dev/null; then
        log "Steam didn't stop"
        systemctl --user start steam.service
        exit 1
    fi
    cp "$CONFIG" "$CONFIG.before-frameport"
    python3 - "$CONFIG" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
if re.search(r'"DevModeEnabled"\s+"\d+"', text):
    text = re.sub(r'("DevModeEnabled"\s+)"\d+"', r'\1"1"', text)
else:  # Steam keeps it in InstallConfigStore/developer
    lines, keys, key, at = text.splitlines(keepends=True), [], None, None
    for i, line in enumerate(lines):
        s = line.strip()
        if s == "{":
            keys.append((key or "").lower())
            if keys == ["installconfigstore", "developer"]:
                at = ("in", i, line[:len(line) - len(line.lstrip())] + "\t")
                break
        elif s == "}":
            if keys == ["installconfigstore"]:  # end of the top section without a developer block
                at = ("new", i, line[:len(line) - len(line.lstrip())] + "\t")
                break
            keys.pop()
        elif re.fullmatch(r'"[^"]*"', s):
            key = s.strip('"')
    if at is None:
        sys.exit("unexpected config.vdf layout")
    kind, i, ind = at
    if kind == "in":
        lines.insert(i + 1, f'{ind}"DevModeEnabled"\t\t"1"\n')
    else:
        lines[i:i] = [f'{ind}"developer"\n', f"{ind}{{\n", f'{ind}\t"DevModeEnabled"\t\t"1"\n', f"{ind}}}\n"]
    text = "".join(lines)
open(path, "w", encoding="utf-8").write(text)
PY
    rc=$?
    [[ $rc == 0 ]] && { "$HELPER" --enable; rc=$?; }
    log "Developer Mode: $([[ $rc == 0 && -f /etc/steamos-devkit-enabled ]] && echo on || echo "failed ($rc)")"
    systemctl --user start steam.service
    [[ $rc == 0 ]] || exit 1
    for _ in $(seq 90); do pgrep -x steam >/dev/null && break; sleep 1; done
    sleep 20  # let Steam finish starting before asking it for anything
fi
if [[ -e ~/.local/share/Steam/steamapps/common/Lepton/lepton ]]; then
    log "Lepton found"
else
    log "asking Steam to install Lepton (confirm it in Steam)"
    steam steam://install/3029110 >/dev/null 2>&1 &
fi
if curl -fsS "$PC_URL/paired?code=$PAIR_CODE&user=$(id -un)&host=$(hostname)" >/dev/null; then
    log "told FramePort: done"
else
    log "couldn't reach FramePort at $PC_URL"
fi
JOB

if [[ -f /etc/steamos-devkit-enabled ]]; then
    say "Developer Mode is on"
    bash "$JOB" finish "$PC_URL" "$PAIR_CODE" "$STEAM_CONFIG" "$DEVKIT_HELPER" 2>&1 | tee "$LOG"
    say "Done. FramePort on your PC shows this Frame as connected."
    exit 0
fi

say "Turning on Developer Mode"
if [[ ! -x "$DEVKIT_HELPER" || ! -f "$STEAM_CONFIG" ]] || \
        ! user_systemd systemd-run --user --collect --quiet --unit="frameport-setup-$$" \
            bash -c 'bash "$0" "$@" >"$HOME/.cache/frameport-setup.log" 2>&1' \
            "$JOB" devmode "$PC_URL" "$PAIR_CODE" "$STEAM_CONFIG" "$DEVKIT_HELPER"; then
    echo "Couldn't turn it on. Turn it on in Settings → System → Enable Developer Mode, then run this again."
    exit 1
fi
echo "Steam restarts and the desktop closes. Confirm Lepton if asked."
echo "Then check FramePort on your PC."
