"""Makes FramePort the system's handler of `framedrop://` and `frameport://` links (no Flet; see deeplink.py).

The handler is never FramePort's own executable: a `flet build` bundle treats any command-line argument as a
developer page URL (the Flutter host would try to connect to "framedrop://…"). The registered command runs a small
script instead (PowerShell on Windows, sh on Linux and WSL) that drops the link into `<data>/links/` and starts
FramePort unless it is already running (its heartbeat file is fresh). The running app takes links from that folder
(`take_links`), so a click while FramePort is open reaches the open window.

Settings `links.framedrop` / `links.frameport` (default on, Settings → Install links; skipped with FRAMEPORT_HOME
or FRAMEPORT_NO_LINK_HANDLER). A scheme that already belongs to another program (FrameDrop) is only taken over when
the user says so (`register(force=True)`); turning the setting off removes FramePort's registration only where
FramePort is the handler.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .core import applog, winhost
from .core.paths import user_data_dir, write_atomic
from .deeplink import SCHEMES

HEARTBEAT_EVERY = 2.0  # seconds between the app's heartbeat touches
TAKEN_WAIT = 4  # seconds a handler script waits for a running FramePort to take the link before starting one
FRESH = 10.0  # a heartbeat younger than this = FramePort runs
STARTING = 30  # seconds the handler script waits for an app it started before starting another
MAX_LINK_AGE = 600  # links older than this (FramePort never came up) are dropped unopened
MARK = "frameport-link-handler"  # in every command FramePort registers (how it recognises its own)
DESKTOP_FILE = "frameport-links.desktop"


@contextmanager
def temporary_oculus_handler(command: str):
    """Borrow oculus:// for an interactive FEX login only. Never use the install-link disk inbox for credentials.

    Save the entire HKCU tree (including unknown values/subkeys/types), not just its open command: Meta normally
    owns it. reg.exe export/import works both natively and through WSL. Linux restores the exact mimeapps bytes,
    including an absent default. The caller supplies a handler which transports links only in memory.
    """
    import tempfile

    kind = platform_kind()
    if kind not in ("windows", "wsl", "linux"):
        raise RuntimeError("--via-pc sign-in supports Windows, WSL and Linux")
    if kind in ("windows", "wsl"):
        user_data_dir().mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="oculus-handler-", dir=user_data_dir()) as tmp:
            backup = Path(tmp) / "previous.reg"
            reg = winhost.system32("reg.exe")
            key = _reg_key("oculus")
            existed = winhost.run_win([reg, "query", key]).returncode == 0
            if existed and winhost.run_win([reg, "export", key, winhost.to_windows(backup), "/y"]).returncode:
                raise RuntimeError("couldn't save the PC's previous Oculus link handler")
            try:
                _reg_set("oculus", command)   # explicit --via-pc authorizes a temporary forced registration
                yield
            finally:
                removed = winhost.run_win([reg, "delete", key, "/f"])
                if existed:
                    restored = winhost.run_win([reg, "import", winhost.to_windows(backup)])
                    if restored.returncode:
                        # Keep the non-secret registry backup for recovery if Windows refuses restoration.
                        recovery = user_data_dir() / "oculus-handler-restore.reg"
                        shutil.copyfile(backup, recovery)
                        raise RuntimeError(f"couldn't restore Oculus link handler; import {recovery}")
                elif removed.returncode and winhost.run_win([reg, "query", key]).returncode == 0:
                    raise RuntimeError("couldn't remove the PC's temporary Oculus link handler")
        return
    if not shutil.which("xdg-mime"):
        raise RuntimeError("--via-pc needs xdg-mime on this PC")
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    apps = _desktop_path().parent
    desktop = apps / "frameport-oculus-login.desktop"
    paths = {desktop, config / "mimeapps.list", apps / "mimeapps.list", apps / "defaults.list"}
    for base in (config, apps):
        paths.update(base.glob("*-mimeapps.list"))
        for name in os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":"):
            if name and all(c.isalnum() or c in "_-" for c in name):
                paths.add(base / f"{name}-mimeapps.list")
    saved = {p: p.read_bytes() if p.exists() else None for p in paths}
    try:
        apps.mkdir(parents=True, exist_ok=True)
        desktop.write_text(desktop_entry(command, ["oculus"]), encoding="utf-8")
        r = subprocess.run(["xdg-mime", "default", desktop.name, "x-scheme-handler/oculus"],
                           capture_output=True, timeout=10)
        if r.returncode:
            raise RuntimeError("couldn't register the PC's temporary Oculus link handler")
        yield
    finally:
        for p, data in saved.items():
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)


def links_dir() -> Path:
    return user_data_dir() / "links"


def heartbeat_file() -> Path:
    return user_data_dir() / "gui.alive"


def starting_file() -> Path:
    return user_data_dir() / "gui.starting"


def script_path(kind: str) -> Path:
    return user_data_dir() / ("frameport-link-handler.ps1" if kind == "ps1" else "frameport-link-handler.sh")


# ------------------------------------------------------------------------------------------- running app side
def heartbeat() -> None:
    """Called by the running GUI every HEARTBEAT_EVERY seconds."""
    path = heartbeat_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        starting_file().unlink(missing_ok=True)
    except OSError:
        pass


def stop_heartbeat() -> None:
    heartbeat_file().unlink(missing_ok=True)


def app_running(now: float | None = None) -> bool:
    try:
        return (now or time.time()) - heartbeat_file().stat().st_mtime < FRESH
    except OSError:
        return False


def drop_link(uri: str) -> Path:
    """Queue a link for the GUI (what the handler scripts do; also `frameport-gui <link>` and tests)."""
    folder = links_dir()
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{os.getpid()}.link"
    write_atomic(folder / name, uri.strip())
    return folder / name


def take_links(now: float | None = None) -> list[str]:
    """Links waiting in the inbox, oldest first; each is removed (stale ones unopened)."""
    folder = links_dir()
    if not folder.is_dir():
        return []
    now = now or time.time()
    out = []
    for f in sorted(folder.glob("*.link")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace").strip()
            age = now - f.stat().st_mtime
            f.unlink()
        except OSError:
            continue
        if text and age < MAX_LINK_AGE:
            out.append(text[:8192])
    return out


# ------------------------------------------------------------------------------------------- start command
def start_command() -> list[str]:
    """How to start this FramePort's GUI without arguments (bundle executable, else this Python)."""
    from . import updates

    root = updates.bundle_root()
    if root:
        if sys.platform == "darwin":
            return ["open", str(root)]
        return [str(updates.running_executable())]
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"  # no console window
    return [str(exe), "-m", "frameport.ui.app"]


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def ps1_script(data: Path, cmd: list[str]) -> str:
    links, alive, starting = data / "links", data / "gui.alive", data / "gui.starting"
    args = ", ".join(_ps_quote(a) for a in cmd[1:]) or ""
    start = (f"Start-Process -FilePath {_ps_quote(cmd[0])}" + (f" -ArgumentList @({args})" if args else ""))
    return f"""# {MARK}: written by FramePort. Queues a framedrop:// or frameport:// link, starts FramePort if needed.
param([string]$Link)
$ErrorActionPreference = 'SilentlyContinue'
if (-not $Link) {{ exit 0 }}
$links = {_ps_quote(str(links))}
New-Item -ItemType Directory -Force -Path $links | Out-Null
$name = [string][DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + '-' + $PID
[IO.File]::WriteAllText((Join-Path $links ($name + '.tmp')), $Link)
$queued = Join-Path $links ($name + '.link')
Move-Item -Force (Join-Path $links ($name + '.tmp')) $queued
function Age($p) {{ if (Test-Path $p) {{ ((Get-Date) - (Get-Item $p).LastWriteTime).TotalSeconds }} else {{ 1e9 }} }}
# a fresh heartbeat may be a window that was just closed: only trust it once that window took the link
if ((Age {_ps_quote(str(alive))}) -lt {int(FRESH)}) {{
    for ($i = 0; $i -lt {TAKEN_WAIT * 4}; $i++) {{
        if (-not (Test-Path $queued)) {{ exit 0 }}
        Start-Sleep -Milliseconds 250
    }}
}}
if ((Age {_ps_quote(str(starting))}) -lt {STARTING}) {{ exit 0 }}
Set-Content -Path {_ps_quote(str(starting))} -Value $PID
{start}
"""


def sh_script(data: Path, cmd: list[str]) -> str:
    q = shlex.quote
    links, alive, starting = data / "links", data / "gui.alive", data / "gui.starting"
    return f"""#!/bin/sh
# {MARK}: written by FramePort. Queues a framedrop:// or frameport:// link and starts FramePort if needed.
[ -n "$1" ] || exit 0
mkdir -p {q(str(links))}
name="$(date +%s%N 2>/dev/null || date +%s)-$$"
queued={q(str(links))}/"$name.link"
printf '%s' "$1" > {q(str(links))}/"$name.tmp" && mv -f {q(str(links))}/"$name.tmp" "$queued"
age() {{ [ -e "$1" ] && echo $(( $(date +%s) - $(stat -c %Y "$1") )) || echo 1000000000; }}
# a fresh heartbeat may be a window that was just closed: only trust it once that window took the link
if [ "$(age {q(str(alive))})" -lt {int(FRESH)} ]; then
    i=0
    while [ $i -lt {TAKEN_WAIT * 4} ]; do [ -e "$queued" ] || exit 0; sleep 0.25; i=$((i + 1)); done
fi
[ "$(age {q(str(starting))})" -lt {STARTING} ] && exit 0
echo $$ > {q(str(starting))}
nohup setsid {" ".join(q(a) for a in cmd)} >/dev/null 2>&1 &
exit 0
"""


# ------------------------------------------------------------------------------------------- registration
def platform_kind() -> str | None:
    """windows | wsl (Windows browser → wsl.exe → this Linux) | linux | None (macOS: Flet can't receive links)."""
    if sys.platform == "win32":
        return "windows"
    if winhost.is_wsl():
        return "wsl"
    if sys.platform.startswith("linux"):
        return "linux"
    return None


def handler_command(kind: str) -> str:
    """The open command written to the registry / .desktop file (%1 / %u = the link)."""
    if kind == "windows":
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        ps = system32 / "WindowsPowerShell/v1.0/powershell.exe"
        # conhost --headless: no console window at all (-WindowStyle Hidden alone still flashes one for a moment)
        host = f'"{system32 / "conhost.exe"}" --headless ' if (system32 / "conhost.exe").exists() else ""
        return (f'{host}"{ps}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File '
                f'"{script_path("ps1")}" "%1"')
    if kind == "wsl":
        distro = os.environ.get("WSL_DISTRO_NAME", "")
        wsl = r"C:\Windows\System32\wsl.exe"
        return f'"{wsl}" ' + (f'-d "{distro}" ' if distro else "") + f'-e sh "{script_path("sh")}" "%1"'
    return f"sh {shlex.quote(str(script_path('sh')))} %u"


def _write_script(kind: str) -> None:
    data = user_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    if kind == "windows":
        write_atomic(script_path("ps1"), ps1_script(data, start_command()))
    else:
        path = script_path("sh")
        write_atomic(path, sh_script(data, start_command()))
        path.chmod(0o755)


# Windows registry (native winreg, or reg.exe from WSL)
def _reg_key(scheme: str) -> str:
    return rf"HKCU\Software\Classes\{scheme}"


def windows_handler(scheme: str) -> str | None:
    """The command Windows runs for scheme:// links (None = no handler for this user or machine)."""
    for root in ("HKCU", "HKLM"):
        cmd = winhost.reg_query(rf"{root}\Software\Classes\{scheme}\shell\open\command", "")
        if cmd:
            return cmd
    return None


def _reg_set(scheme: str, command: str) -> None:
    key = _reg_key(scheme)
    if sys.platform == "win32":
        import winreg

        sub = key.partition("\\")[2]
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, sub) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f"URL:{scheme} (FramePort)")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, sub + r"\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)
        return
    reg = winhost.system32("reg.exe")
    for args in (["add", key, "/ve", "/d", f"URL:{scheme} (FramePort)", "/f"],
                 ["add", key, "/v", "URL Protocol", "/d", "", "/f"],
                 ["add", key + r"\shell\open\command", "/ve", "/d", command, "/f"]):
        r = winhost.run_win([reg, *args])
        if r.returncode:
            raise OSError(f"reg.exe {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()}")


def _reg_delete(scheme: str) -> None:
    key = _reg_key(scheme)
    if sys.platform == "win32":
        import winreg

        sub = key.partition("\\")[2]
        for part in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub + part)
            except OSError:
                pass
        return
    winhost.run_win([winhost.system32("reg.exe"), "delete", key, "/f"])


# Linux desktop (xdg)
def _desktop_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return base / "applications" / DESKTOP_FILE


def desktop_entry(command: str, schemes=SCHEMES) -> str:
    mimes = "".join(f"x-scheme-handler/{s};" for s in schemes)
    return ("[Desktop Entry]\nType=Application\nName=FramePort (install links)\n"
            f"Comment={MARK}\nExec={command}\nNoDisplay=true\nTerminal=false\nMimeType={mimes}\n")


def _desktop_schemes() -> list[str]:
    """The schemes FramePort's .desktop file lists now."""
    try:
        text = _desktop_path().read_text(encoding="utf-8")
    except OSError:
        return []
    return [s for s in SCHEMES if f"x-scheme-handler/{s};" in text]


def linux_handler(scheme: str) -> str | None:
    """The .desktop file that opens scheme:// links (None = none, or xdg-mime isn't installed)."""
    if not shutil.which("xdg-mime"):
        return None
    try:
        r = subprocess.run(["xdg-mime", "query", "default", f"x-scheme-handler/{scheme}"], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None


# public API
def current_handler(scheme: str) -> str | None:
    kind = platform_kind()
    if kind in ("windows", "wsl"):
        return windows_handler(scheme)
    if kind == "linux":
        return linux_handler(scheme)
    return None


def is_ours(handler: str | None) -> bool:
    return bool(handler) and (MARK in handler or handler == DESKTOP_FILE)


def status() -> dict:
    """{scheme: "ours" | "other" | "none"}, "<scheme>_by" (the other program, e.g. FrameDrop) and "supported" (False
    on macOS)."""
    out: dict = {"supported": platform_kind() is not None}
    if not out["supported"]:
        return out
    for s in SCHEMES:
        h = current_handler(s)
        out[s] = "ours" if is_ours(h) else ("other" if h else "none")
        if h and not is_ours(h):
            out[f"{s}_by"] = _program_name(h)
    return out


def _program_name(handler: str) -> str:
    """"FrameDrop" for FrameDrop's handler, else the program's file name (a .desktop file's name on Linux)."""
    if "framedrop" in handler.lower() and "frameport" not in handler.lower():
        return "FrameDrop"
    first = handler.split('"')[1] if handler.startswith('"') and handler.count('"') >= 2 else handler.split()[0]
    return Path(first.replace("\\", "/")).stem or first


def enabled(scheme: str) -> bool:
    """Settings → Install links: one switch per scheme (`links.framedrop`, `links.frameport`; default on)."""
    from .core import library

    return bool(library.setting(f"links.{scheme}", True))


def set_enabled(scheme: str, on: bool) -> dict:
    """Turn one scheme on or off and apply it now. Returns status()."""
    from .core import library

    library.set_setting(f"links.{scheme}", bool(on))
    return register([scheme]) if on else unregister([scheme])


def register(schemes=None, force: bool = False) -> dict:
    """Register FramePort for these schemes (default: the enabled ones). Without `force`, a scheme another program
    handles is left alone. Returns status() afterwards."""
    kind = platform_kind()
    if not kind:
        return status()
    schemes = [s for s in (schemes if schemes is not None else [s for s in SCHEMES if enabled(s)]) if s in SCHEMES]
    if not schemes:
        return status()
    _write_script(kind)
    command = handler_command(kind)
    if kind in ("windows", "wsl"):
        for s in schemes:
            h = windows_handler(s)
            if force or not h or is_ours(h):
                if h != command:
                    _reg_set(s, command)
    else:
        path = _desktop_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        listed = sorted(set(_desktop_schemes()) | set(schemes), key=SCHEMES.index)
        write_atomic(path, desktop_entry(command, listed))
        if shutil.which("xdg-mime"):
            for s in schemes:
                h = linux_handler(s)
                if force or not h or is_ours(h):
                    subprocess.run(["xdg-mime", "default", DESKTOP_FILE, f"x-scheme-handler/{s}"],
                                   capture_output=True, timeout=10)
        _update_desktop_db()
    applog.log.info("link handler registered for %s (%s, force=%s)", ", ".join(schemes), kind, force)
    return status()


def _update_desktop_db() -> None:
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(_desktop_path().parent)], capture_output=True, timeout=20)


def unregister(schemes=None) -> dict:
    """Remove FramePort's registration for these schemes (default: both) where FramePort is the handler (another
    program's stays)."""
    kind = platform_kind()
    schemes = [s for s in (schemes if schemes is not None else SCHEMES) if s in SCHEMES]
    if kind in ("windows", "wsl"):
        for s in schemes:
            if is_ours(windows_handler(s)):
                _reg_delete(s)
    elif kind == "linux":
        keep = [s for s in _desktop_schemes() if s not in schemes]
        if keep:
            write_atomic(_desktop_path(), desktop_entry(handler_command(kind), keep))
        else:
            _desktop_path().unlink(missing_ok=True)
        _update_desktop_db()
    applog.log.info("link handler removed for %s (%s)", ", ".join(schemes), kind)
    return status()


def apply_setting() -> dict | None:
    """At GUI start: register the enabled schemes (never taking one from another program) and remove FramePort's
    registration of the disabled ones. Errors are logged, never raised (links are a convenience)."""
    # an isolated data dir (FRAMEPORT_HOME: tests, screenshots) never takes over the user's link handler, nor does a
    # WSL run on the Windows copy's data (it would point the Windows copy's links at WSL)
    from .core.paths import shares_windows_data

    if (os.environ.get("FRAMEPORT_NO_LINK_HANDLER") or os.environ.get("FRAMEPORT_HOME") or shares_windows_data()
            or not platform_kind()):
        return None
    try:
        off = [s for s in SCHEMES if not enabled(s)]
        if off:
            unregister(off)
        return register()
    except Exception as exc:  # noqa: BLE001
        applog.log.warning("link handler: %s", exc)
        return None
