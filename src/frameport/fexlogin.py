"""Interactive Meta sign-in on the Frame. Credential URLs travel in memory, over SSH stdin / a private callback.

There is intentionally no Reporter or app log in this module: external programs' exception strings and Meta's
Electron output can contain login URLs. Only fixed, friendly progress text reaches the CLI.
"""
from __future__ import annotations

import os
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import fexrift, urlhandler
from .core import winhost
from .core.paths import user_data_dir

TITLE = "Meta sign-in (FramePort)"


def valid_callback(url):
    if not isinstance(url, str) or len(url) > 65536 or any(ord(c) < 32 for c in url):
        return False
    try:
        u = urllib.parse.urlsplit(url)
        return (u.scheme, u.netloc, u.path) == ("oculus", "link", "/frl_login")
    except ValueError:
        return False


def open_page(url):
    try:
        u = urllib.parse.urlsplit(url)
        if (u.scheme, u.netloc, u.path) != ("https", "auth.meta.com", "/native_sso/confirm"):
            raise ValueError
        if winhost.is_wsl():
            # Start-Process on Windows, without interpolating the sign-in URL into PowerShell code or argv.
            subprocess.run([winhost.system32("WindowsPowerShell/v1.0/powershell.exe"), "-NoProfile",
                            "-NonInteractive", "-Command", "Start-Process ([Console]::In.ReadToEnd())"],
                           input=url, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15, check=True)
        elif not webbrowser.open(url):
            raise ValueError
    except Exception:
        raise RuntimeError("couldn't open Meta's sign-in page in the PC browser") from None


@contextmanager
def pc_callback():
    """The temporary scheme script POSTs straight into this loopback server, never to links/*.link.

    A random endpoint and no CORS prevent arbitrary websites from posting callbacks. Handler errors and HTTP
    request lines are deliberately never logged. Both the script and registration are removed in finally blocks.
    """
    inbox = queue.Queue(maxsize=4)
    endpoint = "/" + secrets.token_hex(24)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if self.path != endpoint or not 0 < n <= 65536:
                    self.send_error(400)
                    return
                url = self.rfile.read(n).decode("utf-8")
                if not valid_callback(url):
                    self.send_error(400)
                    return
                inbox.put_nowait(url)
                self.send_response(204)
                self.end_headers()
            except (ValueError, OSError, queue.Full):
                self.send_error(400)

    class Server(ThreadingHTTPServer):
        def handle_error(self, *args):
            pass

    server = Server(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    address = f"http://127.0.0.1:{server.server_address[1]}{endpoint}"
    user_data_dir().mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="fex-login-", dir=user_data_dir()) as tmp:
            kind = urlhandler.platform_kind()
            if kind == "windows":
                script = Path(tmp) / "callback.ps1"
                script.write_text("param([string]$Link)\n$ErrorActionPreference='SilentlyContinue'\n"
                                  f"try {{ Invoke-WebRequest -UseBasicParsing -Uri '{address}' -Method POST "
                                  "-Body ([Text.Encoding]::UTF8.GetBytes($Link)) | Out-Null } catch {}\n")
                ps = (Path(os.environ.get("SystemRoot", r"C:\Windows")) /
                      "System32/WindowsPowerShell/v1.0/powershell.exe")
                command = f'"{ps}" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{script}" "%1"'
            else:
                script = Path(tmp) / "callback.py"
                script.write_text("import sys,urllib.request\ntry:\n"
                                  f"    r=urllib.request.Request({address!r},data=sys.argv[1].encode(),method='POST')\n"
                                  "    urllib.request.urlopen(r,timeout=10).close()\nexcept Exception:\n    pass\n")
                if kind == "wsl":
                    distro = os.environ.get("WSL_DISTRO_NAME", "")
                    if not distro:
                        raise RuntimeError("WSL_DISTRO_NAME is needed for --via-pc")
                    command = (f'"C:\\Windows\\System32\\wsl.exe" -d "{distro}" -e '
                               f'python3 -I "{script}" "%1"')
                else:
                    # Desktop Exec syntax uses double quotes, with literal percent signs escaped.
                    def dq(s):
                        s = str(s).replace("%", "%%")
                        for c in ("\\", '"', "`", "$"):
                            s = s.replace(c, "\\" + c)
                        return '"' + s.replace("\\", "\\\\") + '"'   # Desktop string-value layer
                    command = f"{dq(sys.executable)} -I {dq(script)} %u"
            with urlhandler.temporary_oculus_handler(command):
                yield inbox
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def login(frame, *, start_now=False, timeout=900, via_pc=False, stop=False, emit=print):
    fexrift.require_enabled()
    if stop:
        frame.agent("fex_login_cancel")
        emit("Meta sign-in stopped. Your saved login stays on the Frame.")
        return {"stopped": True}
    if not 1 <= timeout <= 86400:
        raise ValueError("sign-in timeout must be between 1 and 86400 seconds")
    with pc_callback() if via_pc else nullcontext(None) as inbox:
        try:
            started = frame.agent("fex_login", start_now=start_now, via_pc=via_pc, login_timeout=timeout)
            if started.get("package"):
                from .core.events import Reporter
                from .install import installer

                result = installer.add_to_steam(frame, [started["package"]], Reporter())
                if result.get("state") != "done" or result.get("errors"):
                    raise RuntimeError("Steam hasn't confirmed Meta sign-in; return to Gaming Mode and retry")
            emit("Meta's app is starting on the Frame." if start_now else
                 f"Put on the headset: open '{TITLE}' in your Steam library.")
            if via_pc:
                emit("Complete the Meta sign-in page in this PC's browser.")
            deadline = time.monotonic() + timeout
            page_seen = callback_seen = False
            while time.monotonic() < deadline:
                st = frame.agent("fex_login_status", take_url=via_pc)
                # Pop immediately, so even the local status variable never retains a login URL after opening it.
                url = st.pop("open_url", None)
                if url:
                    open_page(url)
                    url = None
                if st.get("page_opened_at") and not page_seen:
                    emit("Sign-in page opened in the PC browser." if via_pc else "Sign-in page opened on the Frame.")
                    page_seen = True
                if inbox is not None:
                    try:
                        link = inbox.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        try:
                            reply = frame.agent("fex_login_callback", url=link)
                            if not reply.get("delivered"):
                                raise RuntimeError("Meta's running app didn't accept the callback; retry sign-in")
                        finally:
                            link = None
                if st.get("callback_received_at") and not callback_seen:
                    emit("Sign-in response reached Meta's app on the Frame.")
                    callback_seen = True
                if st.get("signed_in"):
                    emit("Signed in.")
                    return {"signed_in": True}
                time.sleep(3)
            raise RuntimeError("Meta sign-in timed out; if the callback didn't reach the Frame, retry with --via-pc")
        finally:
            # Runs on success, Ctrl-C, timeout and partial setup errors; the broker also expires if SSH is lost.
            frame.agent("fex_login_cancel")
