"""Find Steam Frames on the network.

Sources, merged per device:
  1. mDNS `_steamos-devkit._tcp` — every SteamOS device in Developer Mode announces itself (name, login user).
  2. mDNS `_frameport._tcp` — reserved for a FramePort service on the Frame (nothing publishes it yet; the PC side
     announces `_frameport-pair._tcp` while its setup page is open, see pairing.py).
  3. Remembered Frames (frames.json).
  4. Fallback: a quick TCP scan of the PC's local /24 subnets for SSH (port 22), for Frames without Developer Mode.
A Frame can have several addresses (home Wi-Fi, its own hotspot `wlanap` 10.35.78.1, USB `usb0` 10.86.200.x); each
is probed and only addresses where SSH answers are offered.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

SERVICES = ["_steamos-devkit._tcp.local.", "_frameport._tcp.local."]


@dataclass
class Found:
    name: str
    host: str  # an address with SSH reachable
    port: int = 22
    user: str = "steamos"
    source: str = ""  # devkit | frameport | saved | scan
    addresses: list[str] = field(default_factory=list)
    properties: dict = field(default_factory=dict)

    @property
    def is_frameport(self) -> bool:  # kept for the UI: device already bootstrapped by FramePort
        return self.source == "frameport"

    @property
    def via(self) -> str:
        return link_label(self.host)


def link_label(ip: str) -> str:
    if ip.startswith("10.35.78."):
        return "Frame hotspot"
    if ip.startswith("10.86.200."):
        return "USB"
    return "network"


def ssh_open(host: str, port: int = 22, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(8).startswith(b"SSH-")
    except OSError:
        return False


def _mdns(seconds: float) -> dict[str, Found]:
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    found: dict[str, Found] = {}

    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=2000)
            if not info:
                return
            props = {k.decode(): (v.decode() if isinstance(v, bytes) else v)
                     for k, v in (info.properties or {}).items()}
            dev = (info.server or name).split(".")[0]
            f = found.setdefault(dev, Found(dev, "", source="devkit" if "devkit" in type_ else "frameport"))
            if "frameport" in type_:
                f.source = "frameport"
            f.user = props.get("login") or props.get("user") or f.user
            f.properties.update(props)
            for a in info.parsed_addresses():
                if ":" not in a and a not in f.addresses:
                    f.addresses.append(a)
            if info.server and info.server.rstrip(".") not in f.addresses:
                f.addresses.append(info.server.rstrip("."))

        def update_service(self, zc, type_, name):
            self.add_service(zc, type_, name)

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, SERVICES, Listener())
        time.sleep(seconds)
    finally:
        zc.close()
    return found


def local_addresses() -> set[str]:
    """IPv4 addresses of this PC (excluded from scans)."""
    import psutil

    return {a.address for addrs in psutil.net_if_addrs().values() for a in addrs if a.family == socket.AF_INET}


def local_subnets() -> list[ipaddress.IPv4Network]:
    """/24 networks of the PC's non-loopback, non-link-local IPv4 interfaces (virtual switches skipped)."""
    import psutil

    nets = set()
    stats = psutil.net_if_stats()
    for nic, addrs in psutil.net_if_addrs().items():
        virtual = any(v in nic.lower() for v in ("vethernet", "docker", "virbr", "vmnet", "wsl"))
        if not stats.get(nic) or not stats[nic].isup or virtual:
            continue
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.", "172.")):
                nets.add(ipaddress.ip_network(f"{a.address}/24", strict=False))
    return sorted(nets, key=str)


def _scan(nets: list[ipaddress.IPv4Network], skip: set[str]) -> list[str]:
    own = local_addresses()
    hosts = [str(h) for n in nets for h in n.hosts() if str(h) not in skip and str(h) not in own]
    with ThreadPoolExecutor(128) as pool:
        results = pool.map(lambda h: ssh_open(h, timeout=0.4), hosts)
        return [h for h, ok in zip(hosts, results, strict=True) if ok and h not in own]


def browse(seconds: float = 4.0, scan: bool = True) -> list[Found]:
    from .connection import saved_targets

    devices = _mdns(seconds)
    for t in saved_targets():
        name = t.name or t.host
        f = devices.get(name) or devices.setdefault(name, Found(name, "", t.port, t.user, "saved"))
        if t.host not in f.addresses:
            f.addresses.insert(0, t.host)
    # probe every address; keep the reachable ones (a device may have LAN, hotspot and USB addresses)
    out = []
    with ThreadPoolExecutor(32) as pool:
        for f in devices.values():
            cands = list(dict.fromkeys(f.addresses))
            ok = [a for a, up in zip(cands, pool.map(ssh_open, cands), strict=True) if up]
            ips = []
            for a in ok:
                try:
                    ips.append(socket.gethostbyname(a))
                except OSError:
                    pass
            for ip in dict.fromkeys(ips):
                out.append(Found(f.name, ip, f.port, f.user, f.source, cands, f.properties))
    if scan:
        known = {f.host for f in out}
        for ip in _scan(local_subnets(), known):
            out.append(Found(ip, ip, 22, "steamos", "scan", [ip]))
    out = dedupe(out)
    return sorted(out, key=lambda f: (SOURCE_RANK.get(f.source, 9), f.name, LINK_RANK.get(f.via, 9), f.host))


SOURCE_RANK = {"frameport": 0, "devkit": 1, "saved": 2, "scan": 3}
LINK_RANK = {"USB": 0, "Frame hotspot": 1, "network": 2}  # fastest first (USB ~37 MB/s, hotspot ~85, but the hotspot
# needs the PC on the Frame's Wi-Fi: when both answer, the cable is the deliberate choice)


def dedupe(found: list[Found], key_of=None) -> list[Found]:
    """One entry per Frame: entries whose SSH host key matches (the same device reached over home Wi-Fi, its hotspot
    and USB, or found by mDNS, the saved list and the network scan) merge into one that uses the fastest link, keeps
    the best-known source/name and lists the other addresses. Entries whose key can't be read stay as they are."""
    if key_of is None:
        from .connection import server_key

        def key_of(f: Found):
            k = server_key(f.host, f.port, timeout=3)
            return k.get_base64() if k else None
    with ThreadPoolExecutor(16) as pool:
        keys = list(pool.map(key_of, found))
    groups: dict[str, list[Found]] = {}
    out = []
    for f, k in zip(found, keys, strict=True):
        if k is None:
            out.append(f)
        else:
            groups.setdefault(k, []).append(f)
    for members in groups.values():
        best = min(members, key=lambda f: (LINK_RANK.get(f.via, 9), f.host))
        named = min(members, key=lambda f: (SOURCE_RANK.get(f.source, 9), f.source == "scan"))
        addresses = list(dict.fromkeys([best.host] + [a for f in members for a in [f.host, *f.addresses]]))
        out.append(Found(named.name if named.source != "scan" else best.name, best.host, best.port, named.user,
                         named.source, addresses, {**best.properties, **named.properties}))
    return out


def local_ip_towards(host: str = "8.8.8.8") -> str:
    """The PC's address on the route towards host (used in the bootstrap one-liner)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
