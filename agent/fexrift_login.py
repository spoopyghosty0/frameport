"""Meta's own browser sign-in, in the Frame's shared prefix. No credential files are read.

The broker is a separate, owner-only Unix socket process: native_sso URLs live only in its memory, never in a
status file, launcher, journal or argv sent over SSH. Helpers and Wine subprocesses have their output discarded.
"""
import base64
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

CORE = None
DESKTOP = "frameport-oculus.desktop"
REGKEY = r"HKCU\Software\Wine\WineBrowser"
MAX_URL = 65536


def folder():
    return Path(CORE.ROOT) / "login"


def request(message):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(90)
        s.connect(str(folder() / "broker.sock"))
        s.sendall(json.dumps(message).encode())
        s.shutdown(socket.SHUT_WR)
        return json.loads(receive(s))


def receive(s):
    data = bytearray()
    while True:
        part = s.recv(8192)
        if not part:
            return data.decode()
        data.extend(part)
        if len(data) > MAX_URL * 2:
            raise ValueError("sign-in message too long")


def valid_url(url, callback=False):
    if not isinstance(url, str) or len(url) > MAX_URL or any(ord(c) < 32 for c in url):
        return False
    try:
        u = urllib.parse.urlsplit(url)
        return (u.scheme, u.netloc, u.path) == (("oculus", "link", "/frl_login") if callback else
                                                ("https", "auth.meta.com", "/native_sso/confirm"))
    except ValueError:
        return False


def host_env():
    env = dict(os.environ)
    env.update(CORE.steam_display_env())
    env.update(XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}",
               DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{os.getuid()}/bus")
    env.pop("LD_PRELOAD", None)
    return env


def quiet(argv, **kw):
    return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, **kw)


def wine_env():
    env = CORE.wine_env({"ENABLE_GAMESCOPE_WSI": "0", "PROTON_NO_ZENITY": "1", "PROTONFIXES_DISABLE": "1",
                         "STEAM_COMPAT_CLIENT_INSTALL_PATH": CORE.STEAM})
    env.pop("LD_PRELOAD", None)
    env.pop("STEAM_COMPAT_INSTALL_PATH", None)
    return env


def snapshot(paths):
    return {str(p): base64.b64encode(p.read_bytes()).decode() if p.exists() else None for p in paths}


def restore_files(saved):
    for name, data in saved.items():
        p = Path(name)
        if data is None:
            p.unlink(missing_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(data))


def desktop_paths():
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    apps = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "applications"
    paths = {apps / DESKTOP, config / "mimeapps.list", apps / "mimeapps.list", apps / "defaults.list"}
    for base in (config, apps):
        paths.update(base.glob("*-mimeapps.list"))
        for desktop in os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":"):
            if desktop and re.fullmatch(r"[a-z0-9_-]+", desktop):
                paths.add(base / f"{desktop}-mimeapps.list")
    return apps, paths


def desktop_entry(helper):
    # Desktop Exec has its own quoting (single shell quotes are not valid here); %% is a literal percent.
    escaped = str(helper).replace("%", "%%")
    for c in ("\\", '"', "`", "$"):
        escaped = escaped.replace(c, "\\" + c)
    escaped = re.sub(r"[\r\n\t]", " ", escaped.replace("\\", "\\\\"))   # Desktop string-value layer
    return ('[Desktop Entry]\nType=Application\nName=Meta sign-in (FramePort)\n'
            f'Exec="{escaped}" %u\nNoDisplay=true\nTerminal=false\nMimeType=x-scheme-handler/oculus;\n')


def helper_script(op):
    # This tiny script never imports the core or prints exceptions (which could contain the supplied URL).
    return ("#!/usr/bin/python3 -I\nimport json,socket,sys\ntry:\n"
            "    with socket.socket(socket.AF_UNIX) as s:\n"
            "        s.settimeout(90)\n"
            f"        s.connect({str(folder() / 'broker.sock')!r})\n"
            f"        s.sendall(json.dumps({{'op': {op!r}, 'url': sys.argv[1]}}).encode())\n"
            "        s.shutdown(socket.SHUT_WR)\n        s.recv(4096)\n"
            "except Exception:\n    sys.exit(1)\n")


def launcher():
    q = shlex.quote
    env = wine_env()
    keys = ("STEAM_COMPAT_DATA_PATH", "WINEPREFIX", "WINEDLLOVERRIDES", "WINEDEBUG", "FP_NATIVE_WSERVER",
            "PROTON_NO_ZENITY", "PROTONFIXES_DISABLE", "STEAM_COMPAT_CLIENT_INSTALL_PATH", "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS", "ENABLE_GAMESCOPE_WSI")
    exports = "".join(f"export {k}={q(env[k])}\n" for k in keys)
    display = "".join(f"export {k}={q(v)}\n" for k, v in CORE.steam_display_env().items())
    return f'''#!/usr/bin/env bash
# FramePort Meta sign-in: discard Electron output, which can contain native_sso URLs.
exec >/dev/null 2>&1
exec 9>{q(str(folder() / '.launch.lock'))}; flock -n 9 || exit 0
{exports}{display}unset LD_PRELOAD STEAM_COMPAT_INSTALL_PATH STEAM_COMPAT_LIBRARY_PATHS PROTON_LOG PROTON_LOG_DIR
python3 -I {q(str(Path(CORE.HERE) / 'fexrift.py'))} login_ping '{{}}' || exit 0
G={q(CORE.GEF)}
L="$WINEPREFIX/drive_c/users/steamuser/AppData/Local/Oculus"
cleanup() {{
    kill $LK 2>/dev/null
    "$G/files/bin/wineserver" -k
    rm -f {q(str(folder() / 'launch.pid'))}
}}
trap cleanup EXIT
trap 'exit 0' TERM INT HUP
# The start tick prevents --stop from signalling a reused PID after an interrupted launcher.
printf '%s %s\\n' "$$" "$(awk '{{print $22}}' /proc/$$/stat)" > {q(str(folder() / 'launch.pid'))}
cd {q(str(Path(CORE.META_DIR) / 'Support/oculus-runtime'))}
"$G/files/bin/wine" ./OVRServer_x64.exe </dev/null &
for i in $(seq 1 45); do
    sleep 2
    tail -n 3 "$L/OVRLibrarian.log" 2>/dev/null | grep -qi "invocation completed" && break
done
sleep 3; pkill -9 -f "[O]VRLibrarian.exe"; sleep 10
( while sleep 5; do pkill -9 -f "[O]VRLibrarian.exe"; done ) & LK=$!
cd {q(str(Path(CORE.META_DIR) / 'Support/oculus-client'))}
"$G/proton" run ./Client.exe
'''


def signed_in():
    sessions = Path(CORE.PFX) / "drive_c/users/steamuser/AppData/Roaming/Oculus/sessions"
    return CORE.signed_in(str(sessions))   # sessions/<numeric user id>/, not Meta's _oaf framework folder


def running(name):
    # /proc, rather than pgrep -f, avoids matching the command requesting status. Scope to this prefix.
    for p in Path("/proc").glob("[0-9]*/environ"):
        try:
            if (f"WINEPREFIX={CORE.PFX}".encode() in p.read_bytes().split(b"\0") and
                    name.encode().lower() in p.with_name("cmdline").read_bytes().lower()):
                return True
        except OSError:
            pass
    return False


def status(args):
    st = CORE.read_json(str(folder() / "state.json"), {}) or {}
    out = {"runtime_running": running("OVRServer_x64.exe"), "client_running": running("Client.exe"),
           "page_opened_at": st.get("page_opened_at"), "callback_received_at": st.get("callback_received_at"),
           "signed_in": signed_in()}
    if args.get("take_url"):
        try:
            out.update(request({"op": "take_url"}))
        except (OSError, ValueError):
            pass
    return out


def deliver(url):
    if not valid_url(url, callback=True):
        return {"delivered": False}
    try:
        # Never put the callback in argv, even between two local processes. The PE reads UTF-8 on stdin.
        p = quiet([str(Path(CORE.GEF) / "proton"), "run", str(Path(CORE.HERE) / "fp_pipesend.exe")],
                  input=url.encode(), env=wine_env())
        return {"delivered": p.returncode == 0}
    except (OSError, subprocess.SubprocessError):
        return {"delivered": False}


class Broker:
    def __init__(self, via_pc):
        self.via_pc = via_pc
        self.pending = None
        self.stopped = False

    def handle(self, msg):
        op = msg.get("op")
        if op in ("page", "callback"):
            url = msg.get("url")
            if not valid_url(url, callback=op == "callback"):
                return {"accepted": False}
            st = CORE.read_json(str(folder() / "state.json"), {}) or {}
            st["page_opened_at" if op == "page" else "callback_received_at"] = time.time()
            if op == "page":
                st["page_url"] = "https://auth.meta.com/native_sso/confirm"
            CORE.write_json(str(folder() / "state.json"), st)
            if op == "callback":
                return deliver(url)
            if self.via_pc:
                self.pending = url
            else:
                quiet(["xdg-open", url], env=host_env())
            return {"accepted": True}
        if op == "take_url":
            url, self.pending = self.pending, None
            return {"open_url": url} if url else {}
        if op == "stop":
            self.pending = None
            self.stopped = True
        return {"ready": True}


def broker(args):
    path = folder() / "broker.sock"
    service = Broker(bool(args.get("via_pc")))
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(str(path))
            os.chmod(path, 0o600)
            s.listen(4)
            s.settimeout(1)
            end = time.monotonic() + int(args.get("timeout", 900))
            while not service.stopped and time.monotonic() < end:
                try:
                    conn, _ = s.accept()
                except TimeoutError:
                    continue
                with conn:
                    conn.settimeout(5)
                    try:
                        reply = service.handle(json.loads(receive(conn)))
                        conn.sendall(json.dumps(reply).encode())
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass   # do not serialize an exception that might contain a URL
    finally:
        service.pending = None
        restore()
        path.unlink(missing_ok=True)
    return {"stopped": True}


def restore():
    path = folder() / "restore.json"
    saved = CORE.read_json(str(path), None)
    if saved is None:
        return
    # Keep the restore record until every step succeeds: --stop can repair an interrupted cleanup.
    launcher_pid = stop_launcher()
    CORE.wineserver_kill()
    if launcher_pid:
        for _ in range(100):
            if process_tick(launcher_pid[0]) != launcher_pid[1]:
                break
            time.sleep(.1)
        else:
            raise CORE.FexError("Meta sign-in launcher is still stopping; retry fex login --stop")
    (folder() / "launch.pid").unlink(missing_ok=True)
    old = saved.get("browser")
    if old:
        CORE.wine("reg", "add", REGKEY, "/v", "Browsers", "/t", old[0], "/d", old[1], "/f")
    else:
        CORE.wine("reg", "delete", REGKEY, "/v", "Browsers", "/f", check=False)
    restore_files(saved["files"])
    path.unlink()


def stop_launcher():
    # Killing Wine alone while launch.sh is waiting for OVRLibrarian lets it start Client.exe *after* cleanup.
    # Signal the owning shell too; its pending TERM exits the startup loop as soon as the current sleep ends.
    path = folder() / "launch.pid"
    try:
        pid, tick = path.read_text().split()
        pid = int(pid)
        if pid <= 1:
            return
        if process_tick(pid) == tick:
            os.kill(pid, signal.SIGTERM)
            return pid, tick
    except (OSError, ValueError, IndexError):
        pass


def process_tick(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]
    except (OSError, IndexError):
        return None


def stop(args):
    try:
        request({"op": "stop"})
        for _ in range(900):
            if not (folder() / "broker.sock").exists():
                break
            time.sleep(.1)
        else:
            raise CORE.FexError("Meta sign-in is still stopping; retry fex login --stop")
    except (OSError, ValueError):
        restore()
        (folder() / "broker.sock").unlink(missing_ok=True)
    return {"stopped": True, "signed_in": signed_in()}


def start(args):
    timeout = int(args.get("timeout") or 900)
    if not 1 <= timeout <= 86400:
        raise CORE.FexError("sign-in timeout must be between 1 and 86400 seconds")
    if not (Path(CORE.HERE) / "fp_pipesend.exe").is_file():
        raise CORE.FexError("fp_pipesend.exe missing: build native/fexwine/pipesend.c and upload it with fex setup")
    if not (Path(CORE.META_DIR) / "Support/oculus-client/Client.exe").is_file():
        raise CORE.FexError("Meta's PC app isn't installed yet; run fex setup first")
    if running("Client.exe") or running("OVRServer_x64.exe"):
        raise CORE.FexError("close the Rift game or Meta app before starting sign-in (the prefix is shared)")
    try:
        request({"op": "ping"})
    except (OSError, ValueError):
        pass
    else:
        raise CORE.FexError("Meta sign-in already active; use fex login --stop first")
    stop({})   # repair a previous interrupted run before snapshotting its handler
    folder().mkdir(parents=True, exist_ok=True, mode=0o700)
    folder().chmod(0o700)
    apps, paths = desktop_paths()
    previous = subprocess.run(["xdg-mime", "query", "default", "x-scheme-handler/oculus"],
                              capture_output=True, text=True, timeout=10, env=host_env()).stdout.strip()
    reg = CORE.wine("reg", "query", REGKEY, "/v", "Browsers", check=False)
    match = re.search(r"Browsers\s+(REG_\w+)\s+(.*)", reg.stdout)
    CORE.write_json(str(folder() / "restore.json"),
                    {"browser": list(match.groups()) if match else None, "handler": previous,
                     "files": snapshot(paths)})
    try:
        for name, text in (("fp-open-url", helper_script("page")),
                           ("fp-oculus-callback", helper_script("callback")), ("launch.sh", launcher())):
            CORE.put_text(str(folder() / name), text, 0o700)
        CORE.write_json(str(folder() / "state.json"), {})
        apps.mkdir(parents=True, exist_ok=True)
        (apps / DESKTOP).write_text(desktop_entry(folder() / "fp-oculus-callback"))
        if quiet(["xdg-mime", "default", DESKTOP, "x-scheme-handler/oculus"], env=host_env()).returncode:
            raise CORE.FexError("couldn't register the Frame's temporary oculus link handler")
        CORE.wine("reg", "add", REGKEY, "/v", "Browsers", "/t", "REG_SZ", "/d",
                  str(folder() / "fp-open-url"), "/f")
        subprocess.Popen([sys.executable, "-I", str(Path(CORE.HERE) / "fexrift.py"), "login_broker",
                          json.dumps({"via_pc": bool(args.get("via_pc")), "timeout": timeout + 180})],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        for _ in range(100):
            try:
                request({"op": "ping"})
                break
            except (OSError, ValueError):
                time.sleep(.05)
        else:
            raise CORE.FexError("couldn't start Meta sign-in's private URL broker")
        if args.get("start_now"):
            if quiet(["systemd-run", "--user", "--collect", "--quiet", "--unit=frameport-fexlogin",
                      "--property=StandardOutput=null", "--property=StandardError=null",
                      str(folder() / "launch.sh")], env=host_env()).returncode:
                raise CORE.FexError("couldn't start Meta's app; retry without --start-now and use Steam")
        return {"launcher": str(folder() / "launch.sh"), "started": bool(args.get("start_now")),
                "signed_in": signed_in()}
    except Exception:
        stop({})
        raise


def register(core):
    global CORE
    CORE = core
    core.COMMANDS.update(login_start=start, login_status=status, login_stop=stop, login_broker=broker,
                         login_ping=lambda args: request({"op": "ping"}),
                         login_callback=lambda args: request({"op": "callback", "url": args.get("url")}))
