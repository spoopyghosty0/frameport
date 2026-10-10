"""Pair Frames in Developer Mode without a click on the PC: while no Frame is connected, watch the network for Frames
announcing themselves as SteamOS devkits (mDNS `_steamos-devkit._tcp`). One whose SSH already lets FramePort in is
connected right away. For one that doesn't know FramePort yet, keep offering FramePort's key to Valve's pairing
service (devkit.register): the service refuses at once until "Pair new host" is open on the Frame, so the polling is
cheap, and the moment it's open the request appears in the headset; approving it pairs the Frame and FramePort
connects. Everything goes from the PC to the Frame (no firewall question). No Flet here: the UI passes callbacks.

Setting `frame.auto_pair` (default on) switches it off."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .connection import FrameNotPaired, FrameTarget

POLL = 5.0  # seconds between looks while nothing is connected
RETRY_REFUSED = 4.0  # "Pair new host" isn't open yet: ask again soon (refused at once, costs nothing)
RETRY_IGNORED = 45.0  # a request went unanswered (not approved in the headset in time): don't nag right away


@dataclass
class Candidate:
    target: FrameTarget
    paired: bool | None = None  # None: not checked yet; False: SSH said FramePort isn't known there
    next_try: float = 0.0
    asking: bool = False


@dataclass
class AutoPairer:
    """find() -> FrameTargets of Frames in Developer Mode on the network; try_login(t) -> True (FramePort is let in),
    False (the Frame doesn't know FramePort) or raises (unreachable); register(host) asks Valve's pairing service and
    raises devkit.PairingRefused; on_ready(t) connects; on_status(kind, detail) shows what's going on (may be None):
    kind "waiting" (detail = the Frame's name: Pair new host isn't open yet) or "refused" (detail = why)."""
    find: Callable[[], list[FrameTarget]]
    try_login: Callable[[FrameTarget], bool]
    register: Callable[[str], None]
    on_ready: Callable[[FrameTarget], None]
    active: Callable[[], bool]  # only while no Frame is connected and the setting is on
    on_status: Callable[[str, str], None] | None = None
    clock: Callable[[], float] = time.monotonic
    candidates: dict[str, Candidate] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)

    def step(self) -> None:
        """One look: called every POLL seconds (and by the tests directly)."""
        if not self.active():
            return
        for t in self.find():
            c = self.candidates.setdefault(t.host, Candidate(t))
            c.target = t
        now = self.clock()
        for host, c in list(self.candidates.items()):
            if c.asking or now < c.next_try:
                continue
            if c.paired is None:
                try:
                    c.paired = self.try_login(c.target)
                except Exception:  # noqa: BLE001 - gone from the network: forget it until it shows up again
                    del self.candidates[host]
                    continue
            if c.paired:
                self.candidates.pop(host, None)
                self.on_ready(c.target)
                return  # one Frame at a time
            self._ask(c)

    def _ask(self, c: Candidate) -> None:
        from .devkit import PairingRefused

        c.asking = True
        try:
            self.register(c.target.host)
        except PairingRefused as exc:
            msg = str(exc).lower()
            if "pair new host" in msg:  # pairing mode not open yet: the normal case while waiting
                self._status("waiting", c.target.label)
                c.next_try = self.clock() + RETRY_REFUSED
            else:  # not approved in time, Steam not running, ...: wait a little longer before asking again
                self._status("refused", str(exc))
                c.next_try = self.clock() + RETRY_IGNORED
            return
        except Exception:  # noqa: BLE001 - unreachable now
            c.next_try = self.clock() + RETRY_IGNORED
            return
        finally:
            c.asking = False
        self.candidates.pop(c.target.host, None)
        self.on_ready(c.target)

    def _status(self, kind: str, detail: str) -> None:
        if self.on_status:
            self.on_status(kind, detail)

    def run(self) -> None:
        while not self._stop.wait(POLL):
            try:
                self.step()
            except Exception:  # noqa: BLE001 - never let the watcher die
                pass

    def start(self) -> AutoPairer:
        threading.Thread(target=self.run, daemon=True, name="frame-autopair").start()
        return self

    def stop(self) -> None:
        self._stop.set()


SCAN_EVERY = 30.0  # seconds between network scans for Valve's pairing port (mDNS is checked on every look)
_scan_cache: tuple[float, list[FrameTarget]] = (-1e9, [])


def devkit_login(host: str, timeout: float = 0.6) -> str | None:
    """The login name Valve's devkit service gives on `host` (only a Frame in Developer Mode answers), else None."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:32000/login-name", timeout=timeout) as r:
            name = r.read(64).decode("utf-8", "replace").strip()
        return name if name.isidentifier() else None
    except (OSError, ValueError):
        return None


def _scan_devkits() -> list[FrameTarget]:
    """Every host on the PC's /24 networks answering on Valve's devkit port: for PCs that don't hear the Frame's mDNS
    (a firewall in front of WSL, some routers). At most every SCAN_EVERY seconds."""
    global _scan_cache
    from concurrent.futures import ThreadPoolExecutor

    from .discovery import local_addresses, local_subnets

    if time.monotonic() - _scan_cache[0] < SCAN_EVERY:
        return _scan_cache[1]
    own = local_addresses()
    hosts = [str(h) for n in local_subnets() for h in n.hosts() if str(h) not in own]

    def probe(h):
        try:
            import socket

            with socket.create_connection((h, 32000), timeout=0.4):
                pass
        except OSError:
            return None
        login = devkit_login(h)
        return FrameTarget(h, login, 22, "") if login else None
    with ThreadPoolExecutor(128) as pool:
        found = [t for t in pool.map(probe, hosts) if t]
    _scan_cache = (time.monotonic(), found)
    return found


def find_devkits() -> list[FrameTarget]:
    """Frames in Developer Mode: announcing themselves (Valve's devkit mDNS service, SSH reachable), else found by a
    slow scan for Valve's pairing port."""
    from .discovery import Found, browse, dedupe

    found = [f for f in browse(3.0, scan=False) if f.source == "devkit"]
    known = {f.host for f in found}
    found += [Found(t.name or t.host, t.host, 22, t.user, "devkit") for t in _scan_devkits() if t.host not in known]
    # one Frame reached over several links (home Wi-Fi, its hotspot, USB) once, on the fastest link (as discovery does)
    return [FrameTarget(f.host, f.user or "steamos", f.port, f.name if f.name != f.host else "") for f in dedupe(found)]


def try_login(target: FrameTarget) -> bool:
    from .connection import Frame

    try:
        frame = Frame(target).connect(timeout=6)
    except FrameNotPaired:
        return False
    try:
        frame.close()
    except Exception:  # noqa: BLE001
        pass
    return True
