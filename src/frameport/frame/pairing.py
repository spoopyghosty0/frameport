"""Pairing server: while the Setup page is open, serve the bootstrap script and FramePort's public key over HTTP on
the LAN. The user types one line on the Frame; the script calls back /paired so the UI knows who connected.

Every request needs the pairing code, a random secret that is only in that one line. The server stops after a
successful pairing, after MAX_FAILURES wrong codes (someone guessing) and after LIFETIME seconds, so a short code
(32 bits) is plenty. The line is typed by hand on the Frame, so it's kept short: no scheme (curl defaults to http),
the code is the path, and a fixed port when it's free.

The setup URL (SETUP_URL, bootstrap/setup.sh) needs no code typed: the server also announces itself over mDNS
(`_frameport-pair._tcp`), the Frame asks with /hello, the user allows it here (both sides show the same 4 digits),
and /wait then hands the Frame the pairing code, which fetches bootstrap.sh exactly as the typed line does."""
from __future__ import annotations

import hashlib
import http.server
import json
import secrets
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from ..core.paths import bootstrap_dir
from .connection import app_public_key
from .discovery import local_ip_towards

MAX_FAILURES = 20
LIFETIME = 30 * 60
PORTS = (8765, 8766, 8767, 0)  # 0 = any free port
FIREWALL_RULE = "FramePort-Pairing"
HINT_AFTER = 45  # seconds without any request from the Frame before the UI suggests what may block it

# the static setup: one fixed address on the project page serves bootstrap/setup.sh (copied there by the site build)
SETUP_PAGE = "https://frameport.app/setup/"
SETUP_URL = "frameport.app/s"
SETUP_LINE = f"curl -sL {SETUP_URL} | bash"
SERVICE = "_frameport-pair._tcp.local."
MAX_ASKS = 3  # open requests at a time (more get 429)
WAIT = 100  # seconds one /wait may hold before the Frame asks again

# two words that name this PC's app key (its public key: same on every start), shown on both sides
_ADJ = ("amber", "brisk", "calm", "dusky", "eager", "fable", "gentle", "hazel", "ivory", "jolly", "keen", "lunar",
        "mellow", "noble", "olive", "polar", "quiet", "rapid", "sunny", "tidal", "umber", "vivid", "witty", "zesty",
        "azure", "bold", "coral", "dawn", "early", "frosty", "golden", "humble")
_NOUN = ("otter", "falcon", "maple", "comet", "harbor", "lantern", "meadow", "pebble", "raven", "willow", "canyon",
         "ember", "glacier", "island", "juniper", "kestrel", "lagoon", "marten", "nebula", "orchid", "prairie",
         "quartz", "river", "summit", "thistle", "valley", "walrus", "yarrow", "badger", "cedar", "delta", "fjord")


def pc_words(key: str | None = None) -> str:
    """'amber-otter': this PC's app key in two words (the Frame shows the same, from the announcement)."""
    h = hashlib.sha256((key if key is not None else app_public_key()).encode()).digest()
    return f"{_ADJ[h[0] % len(_ADJ)]}-{_NOUN[h[1] % len(_NOUN)]}"


def ask_digits(nonce: str) -> str:
    """The 4 digits both sides show for one request (setup.sh computes the same from its nonce)."""
    return f"{int(hashlib.sha256(nonce.encode()).hexdigest()[:8], 16) % 10000:04d}"


def announce_properties(name: str) -> dict[str, str]:
    """The TXT record of the announcement: public facts only (never the pairing code)."""
    return {"v": "1", "pc": name, "words": pc_words()}


@dataclass
class Ask:
    """A Frame asking to be set up (from /hello), waiting for the user to allow it."""
    id: str
    frame: str  # the Frame's host name
    address: str
    digits: str
    state: str = "open"  # open | allowed | denied
    created: float = field(default_factory=time.time)
    event: threading.Event = field(default_factory=threading.Event, repr=False)


def ensure_reachable(server: PairingServer) -> str:
    """Under WSL, let the Frame reach the pairing ports through Windows' Hyper-V firewall while `server` runs (one
    admin prompt; the rule is removed again when the server stops, see winhost.open_wsl_inbound).
    Returns "ok" (nothing blocks), "opened" or "failed" (declined / no admin)."""
    from ..core import winhost

    if not winhost.wsl_inbound_blocked(FIREWALL_RULE):
        return "ok"
    temp = winhost.env_path("TEMP")
    if temp is None:
        return "failed"
    server.flag = temp / f"frameport-pairing-{server.code}.flag"
    try:
        server.flag.write_text("FramePort's setup page is open: the firewall rule stays while this file exists\n")
    except OSError:
        return "failed"
    ports = f"{PORTS[0]}-{PORTS[-2]}"
    return "opened" if winhost.open_wsl_inbound(FIREWALL_RULE, "FramePort setup (WSL, temporary)", ports,
                                                server.flag) else "failed"


def firewall_hint(port: int) -> str:
    """What may keep the Frame from reaching this PC, for this OS (shown when no request came in after HINT_AFTER).
    Empty when nothing specific is known."""
    import platform
    import shutil
    import subprocess

    from ..core import winhost
    from ..i18n import tr

    def out(*cmd) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.lower()
        except (OSError, subprocess.SubprocessError):
            return ""

    if winhost.is_wsl():
        if winhost.wsl_networking_mode() == "nat":
            return tr("The Frame can't reach FramePort in WSL's default network mode. Add networkingMode=mirrored "
                      "under [wsl2] in %UserProfile%\\.wslconfig, run wsl --shutdown and start FramePort again.")
        if winhost.wsl_inbound_blocked(FIREWALL_RULE):
            return tr("Windows' firewall for WSL blocks the Frame. Show the setup command again and allow the "
                      "change when Windows asks.")
        return ""
    if winhost.is_windows():
        if winhost.network_category(local_ip_towards()) == "Public":
            return tr("Windows' firewall blocks the Frame on public networks. Set this network to Private in "
                      "Windows' network settings.")
        return tr("If Windows asks whether FramePort may use the network, allow it.")
    if platform.system() == "Darwin":
        fw = out("/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate")
        if "enabled" in fw:
            return tr("The macOS firewall is on. Allow incoming connections when macOS asks about FramePort.")
        return ""
    if shutil.which("firewall-cmd") and "running" in out("firewall-cmd", "--state"):
        return tr("firewalld is active. Allow the setup port until the next restart with: "
                  "sudo firewall-cmd --add-port={port}/tcp").format(port=port)
    if "active" == out("systemctl", "is-active", "ufw").strip():
        return tr("ufw is active. Allow the setup port with: sudo ufw allow {port}/tcp (remove it afterwards "
                  "with: sudo ufw delete allow {port}/tcp)").format(port=port)
    return ""


@dataclass
class PairingServer:
    port: int = 0
    code: str = field(default_factory=lambda: secrets.token_hex(4))
    failures: int = 0
    paired: list[dict] = field(default_factory=list)
    on_paired: object = None
    requests: int = 0  # requests from the network (right or wrong code): the Frame can reach us
    flag: Path | None = None  # WSL: the temporary firewall rule stays while this file exists
    hint: str = ""  # set by the UI when nothing reached us after HINT_AFTER seconds: what may block the Frame
    host: str = ""  # the address the Frame uses to reach this PC; "" = the PC's address towards the internet. The USB
    # setup uses the cable's fixed PC address (10.86.200.234), so no Wi-Fi, router or discovery is involved
    asks: list[Ask] = field(default_factory=list)
    on_ask: object = None  # called with each new Ask (the UI shows Allow / Deny)
    announce: bool = True  # announce over mDNS so the setup URL finds this PC
    _httpd: http.server.ThreadingHTTPServer | None = None
    _zc: object = None
    _info: object = None

    @property
    def url(self) -> str:
        return f"http://{self.host or local_ip_towards()}:{self.port}"

    @property
    def one_liner(self) -> str:
        return f"curl -fsS {self.url.removeprefix('http://')}/{self.code} | bash"

    def start(self) -> PairingServer:
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body: bytes, ctype="text/plain", status=200):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                server.requests += 1
                url = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                path, code = url.path, q.get("code", "")
                if path == "/ping":  # setup.sh's fallbacks (USB address, network scan) recognize a FramePort PC
                    return self._send(json.dumps({"v": 1, "pc": socket.gethostname().split(".")[0],
                                                  "words": pc_words()}).encode(), "application/json")
                if path == "/hello":  # the setup URL: a Frame asks; nothing is handed out until the user allows it
                    nonce = q.get("nonce", "")
                    if not (8 <= len(nonce) <= 64) or not nonce.isalnum():
                        return self._send(b"bad request\n", status=400)
                    if sum(a.state == "open" for a in server.asks) >= MAX_ASKS:
                        return self._send(b"too many open requests\n", status=429)
                    ask = Ask(secrets.token_hex(8), q.get("host", "")[:64] or "Steam Frame", self.client_address[0],
                              ask_digits(nonce))
                    server.asks.append(ask)
                    if callable(server.on_ask):
                        server.on_ask(ask)
                    return self._send(json.dumps({"id": ask.id, "pc": socket.gethostname(), "words": pc_words(),
                                                  "digits": ask.digits}).encode(), "application/json")
                if path == "/wait":  # held until the user decides (or WAIT seconds: the Frame asks again)
                    ask = next((a for a in server.asks if secrets.compare_digest(a.id, q.get("id", ""))), None)
                    if ask is None:
                        return self._send(b"unknown request\n", status=404)
                    ask.event.wait(WAIT)
                    if ask.state == "allowed":
                        return self._send(json.dumps({"code": server.code}).encode(), "application/json")
                    if ask.state == "denied":
                        return self._send(b"denied\n", status=403)
                    return self._send(b'{"wait": true}', "application/json", status=202)
                if not code and path.count("/") == 1:  # the short form: /<code> = the script
                    path, code = "/bootstrap.sh", path[1:]
                if not secrets.compare_digest(code, server.code):
                    server.failures += 1
                    if server.failures >= MAX_FAILURES:
                        server.stop_soon()
                    return self._send(b"wrong or missing pairing code\n", status=403)
                if path == "/bootstrap.sh":
                    text = (bootstrap_dir() / "bootstrap.sh").read_text()
                    text = text.replace("__PC_URL__", server.url).replace("__PAIR_CODE__", server.code)
                    return self._send(text.encode(), "text/x-shellscript")
                if path == "/key":
                    return self._send((app_public_key() + "\n").encode())
                if path == "/paired":
                    info = {"host": self.client_address[0], "user": q.get("user", "steamos"), "name": q.get("host", "")}
                    server.paired.append(info)
                    if callable(server.on_paired):
                        server.on_paired(info)
                    server.stop_soon()  # paired: nothing else to serve
                    return self._send(b"ok\n")
                return self._send(b"not found\n", status=404)

        for port in ((self.port,) if self.port else PORTS):
            try:
                self._httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
                break
            except OSError:
                if port == PORTS[-1]:
                    raise
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self._timer = threading.Timer(LIFETIME, self.stop)
        self._timer.daemon = True
        self._timer.start()
        if self.announce:
            threading.Thread(target=self._announce, daemon=True).start()
        return self

    def decide(self, ask_id: str, allow: bool) -> None:
        """The user allowed (or denied) a Frame that asked via /hello."""
        for a in self.asks:
            if a.id == ask_id and a.state == "open":
                a.state = "allowed" if allow else "denied"
                a.event.set()

    def _announce(self) -> None:
        """mDNS `_frameport-pair._tcp` while the server runs: the setup URL's script finds this PC with it. Never the
        code: only the name, the port and the PC's two words. Failing to announce only loses that convenience."""
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf

            name = (socket.gethostname().split(".")[0] or "FramePort")[:40]
            ip = self.host or local_ip_towards()
            info = ServiceInfo(SERVICE, f"{name}.{SERVICE}", addresses=[socket.inet_aton(ip)], port=self.port,
                               properties=announce_properties(name), server=f"{name}.local.")
            zc = Zeroconf(ip_version=IPVersion.V4Only)
            zc.register_service(info, allow_name_change=True)
            if self._httpd is None:  # stopped meanwhile
                zc.unregister_service(info)
                zc.close()
                return
            self._zc, self._info = zc, info
        except Exception:  # noqa: BLE001 - no multicast, no address, zeroconf missing: the typed line still works
            return

    def _unannounce(self) -> None:
        zc, info, self._zc, self._info = self._zc, self._info, None, None
        if zc is not None:
            try:
                zc.unregister_service(info)
                zc.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def stop_soon(self) -> None:
        """Stop from inside a request handler (shutdown() waits for the serving thread, so not from that thread)."""
        threading.Thread(target=self.stop, daemon=True).start()

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        timer = getattr(self, "_timer", None)
        if timer:
            timer.cancel()
        for a in self.asks:  # let a waiting Frame go
            a.event.set()
        threading.Thread(target=self._unannounce, daemon=True).start()
        if self.flag is not None:  # lets the temporary firewall rule go
            self.flag.unlink(missing_ok=True)
