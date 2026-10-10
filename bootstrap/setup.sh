#!/usr/bin/env bash
# FramePort setup from the project page: the same for every Frame and every PC, nothing to copy from the PC. Run in
# the Frame's desktop terminal (SteamVR dashboard -> Launch a program -> Desktop, then System -> Konsole):
#   curl -sL frameport.app/s | bash
# with FramePort open on your PC at Steam Frame -> Connect. Options: --pc <address[:port]> (skip the search).
#
# What it does:
#   1. finds FramePort on your network (it announces itself over mDNS while that page is open; else the USB cable's
#      address and a quick scan of this network)
#   2. asks it to set up this Frame: FramePort shows the same 4 digits as this terminal, you click Allow there
#   3. runs FramePort's setup script from your PC, the one the typed line `curl -fsS <pc>/<code> | bash` runs (it
#      authorizes the app's key, configures podman, turns on Developer Mode, asks for Lepton)
# Nothing changes on the Frame before you allow it on the PC.
set -euo pipefail
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
PORTS="8765 8766 8767"
USB_PC=10.86.200.234  # the PC's fixed address on the Frame's USB cable network (Developer Mode only)

PC=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --pc) PC=${2:-}; shift 2 ;;
        --pc=*) PC=${1#--pc=}; shift ;;
        -h|--help) sed -n '2,13p' "$0" 2>/dev/null || true; exit 0 ;;
        *) shift ;;
    esac
done
for tool in curl python3; do
    command -v "$tool" >/dev/null || { echo "This needs $tool, which SteamOS normally has. Run the setup command FramePort shows instead."; exit 1; }
done

# FramePort PCs, one per line: name<TAB>address:port<TAB>words. Stdlib Python: avahi isn't always answering on the
# Frame, and its firewall only lets mDNS in on port 5353, so this listens there in the group, like avahi does.
find_pcs() {
    python3 - "$PORTS" "$USB_PC" <<'PY'
import json, socket, struct, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
PORTS = [int(p) for p in sys.argv[1].split()]
USB_PC = sys.argv[2]
SERVICE = "_frameport-pair._tcp.local"

def qname(name):
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"

def read_name(d, off):
    labels, jumped, end = [], False, off
    while True:
        n = d[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = struct.unpack("!H", d[off:off + 2])[0] & 0x3FFF
            if not jumped:
                end = off + 2
            jumped, off = True, ptr
            continue
        labels.append(d[off + 1:off + 1 + n].decode(errors="replace"))
        off += 1 + n
    return ".".join(labels), (end if jumped else off)

def mdns(seconds=3.0):
    found, srv, txt, addr = {}, {}, {}, {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.bind(("", 5353))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton("224.0.0.251") + socket.inet_aton("0.0.0.0"))
        query = struct.pack("!6H", 0, 0, 1, 0, 0, 0) + qname(SERVICE) + struct.pack("!HH", 12, 1)
        s.sendto(query, ("224.0.0.251", 5353))
    except OSError:
        return []
    end, again = time.time() + seconds, True
    while time.time() < end:
        if again and time.time() > end - seconds / 2:
            s.sendto(query, ("224.0.0.251", 5353))
            again = False
        s.settimeout(max(0.05, min(0.5, end - time.time())))
        try:
            d, src = s.recvfrom(9000)
        except socket.timeout:
            continue
        try:
            _, _, qd, an, ns, ar = struct.unpack("!6H", d[:12])
            off = 12
            for _ in range(qd):
                _, off = read_name(d, off)
                off += 4
            for _ in range(an + ns + ar):
                name, off = read_name(d, off)
                rtype, _, _, rdlen = struct.unpack("!HHIH", d[off:off + 10])
                off += 10
                if rtype == 12 and name.lower() == SERVICE:
                    found.setdefault(read_name(d, off)[0], src[0])
                elif rtype == 33:
                    srv[name] = (read_name(d, off + 6)[0], struct.unpack("!H", d[off + 4:off + 6])[0])
                elif rtype == 16:
                    items, p = {}, off
                    while p < off + rdlen:
                        n = d[p]
                        k, _, v = d[p + 1:p + 1 + n].decode(errors="replace").partition("=")
                        items[k] = v
                        p += 1 + n
                    txt[name] = items
                elif rtype == 1 and rdlen == 4:
                    addr[name] = socket.inet_ntoa(d[off:off + 4])
                off += rdlen
        except (IndexError, struct.error, UnicodeError):
            continue
    out = []
    for inst, src in found.items():
        target, port = srv.get(inst, ("", PORTS[0]))
        props = txt.get(inst, {})
        out.append((props.get("pc") or inst.split(".")[0], f"{addr.get(target) or src}:{port}", props.get("words", "")))
    return out

def ping(host_port):
    try:
        with urllib.request.urlopen(f"http://{host_port}/ping", timeout=1.5) as r:
            info = json.load(r)
        return (info.get("pc", host_port), host_port, info.get("words", ""))
    except Exception:
        return None

def scan():
    """This network's /24 (and the USB cable's PC address) for a FramePort setup port."""
    hosts = [f"{USB_PC}:{p}" for p in PORTS]
    try:
        out = __import__("subprocess").run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
    except OSError:
        out = ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[1] not in ("lo",) and "/" in parts[3]:
            mine = parts[3].split("/")[0]
            base = mine.rsplit(".", 1)[0]
            hosts += [f"{base}.{i}:{p}" for i in range(1, 255) if f"{base}.{i}" != mine for p in PORTS[:1]]
    def open_port(hp):
        h, p = hp.rsplit(":", 1)
        try:
            with socket.create_connection((h, int(p)), timeout=0.4):
                return hp
        except OSError:
            return None
    with ThreadPoolExecutor(64) as ex:
        live = [hp for hp in ex.map(open_port, hosts) if hp]
    return [r for r in map(ping, live) if r]

pcs = mdns()
if not pcs:
    pcs = scan()
seen = set()
for name, hp, words in pcs:
    key = (name, words) if words else hp  # one PC on several links (home Wi-Fi and the Frame's hotspot): once
    if key not in seen:
        seen.add(key)
        print(f"{name}\t{hp}\t{words}")
PY
}

say "FramePort setup for $(hostname)"
if [[ -n "$PC" ]]; then
    [[ "$PC" == *:* ]] || PC="$PC:${PORTS%% *}"
    WORDS=""
else
    echo "Looking for FramePort on your network (in FramePort on your PC: Steam Frame → Start setup)..."
    mapfile -t pcs < <(find_pcs)
    if [[ ${#pcs[@]} -eq 0 ]]; then
        echo
        echo "FramePort wasn't found. Check that:"
        echo "  - FramePort is open on your PC, at Steam Frame → Start setup"
        echo "  - the Frame and the PC are on the same Wi-Fi (or connected with a USB cable)"
        echo "Or run the setup command FramePort shows there (Use the setup command)."
        exit 1
    fi
    pick=0
    if [[ ${#pcs[@]} -gt 1 ]]; then
        echo "More than one FramePort is open on this network:"
        for i in "${!pcs[@]}"; do
            IFS=$'\t' read -r name hp words <<<"${pcs[$i]}"
            echo "  $((i + 1))) $name  ($hp)  $words"
        done
        # stdin is this script itself (curl | bash): ask on the terminal; without one, take the first
        if { exec 3</dev/tty; } 2>/dev/null; then
            read -r -u 3 -p "Which one? [1-${#pcs[@]}] " n
            exec 3<&-
            [[ "$n" =~ ^[0-9]+$ && $n -ge 1 && $n -le ${#pcs[@]} ]] || { echo "No such number."; exit 1; }
            pick=$((n - 1))
        else
            echo "No terminal to ask in: using the first. (Choose with --pc <address:port>.)"
        fi
    fi
    IFS=$'\t' read -r name PC WORDS <<<"${pcs[$pick]}"
    echo "Found FramePort on $name ($PC)${WORDS:+, PC words: $WORDS}"
fi

nonce=$(python3 -c 'import secrets; print(secrets.token_hex(12))')
digits=$(python3 -c 'import hashlib, sys; print(f"{int(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:8], 16) % 10000:04d}")' "$nonce")
reply=$(curl -fsS -G --data-urlencode "host=$(hostname)" --data-urlencode "nonce=$nonce" "http://$PC/hello") || {
    echo "FramePort at $PC didn't answer. Is it still open at Steam Frame → Start setup?"; exit 1; }
id=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["id"])' "$reply")
WORDS=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1]).get("words", ""))' "$reply")

say "FramePort on your PC asks to set up this Frame"
printf '   Click Allow there if it shows  \033[1;33m%s\033[0m  (PC: %s)\n' "$digits" "${WORDS:-?}"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
code=""
for _ in $(seq 12); do  # up to about 20 minutes
    status=$(curl -sS -o "$tmp" -w '%{http_code}' -m 115 "http://$PC/wait?id=$id" || echo 000)
    case $status in
        200) code=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["code"])' "$tmp"); break ;;
        202) continue ;;
        403) echo "Not allowed on the PC. Nothing was changed on this Frame."; exit 1 ;;
        *) echo "Lost FramePort at $PC (HTTP $status). Run this again, or run the setup command FramePort shows."; exit 1 ;;
    esac
done
[[ -n "$code" ]] || { echo "Not allowed in time. Run this again when you're at your PC."; exit 1; }

say "Allowed. Running the setup from $PC"
curl -fsS "http://$PC/$code" | bash
