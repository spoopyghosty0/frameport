"""The Windows PC FramePort runs on: native Windows, or WSL (Windows programs via interop, C: at /mnt/c).

Used by the PC VR (Revive) target: finding Windows Steam, its active user, SteamVR, and starting/stopping Windows
processes. Everything is discovered (registry, loginusers.vdf, libraryfolders.vdf); nothing about the user is stored.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

STEAMID64_BASE = 76561197960265728
STEAMVR_APPID = "250820"


def is_windows() -> bool:
    return sys.platform == "win32"


@lru_cache(maxsize=1)
def is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def available() -> bool:
    return is_windows() or is_wsl()


def _no_window() -> dict:
    return {"creationflags": 0x08000000} if is_windows() else {}  # CREATE_NO_WINDOW


def system32(exe: str) -> str:
    """Path to a Windows system tool (tasklist.exe, reg.exe, ...) callable from here."""
    if is_windows():
        return exe
    for root in ("/mnt/c/Windows/System32", "/mnt/c/WINDOWS/system32"):
        if Path(root, exe).exists():
            return str(Path(root, exe))
    return shutil.which(exe) or exe


def run_win(args: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout, **_no_window())


def to_windows(path: Path | str) -> str:
    """Local path -> Windows path (D:\\Games\\X). Identity on Windows."""
    if is_windows():
        return str(Path(path).resolve())
    p = str(Path(path).resolve())
    m = re.match(r"^/mnt/([a-zA-Z])(/.*)?$", p)
    if m:
        return f"{m[1].upper()}:" + (m[2] or "/").replace("/", "\\")
    try:
        r = subprocess.run(["wslpath", "-w", p], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"{p} is not reachable from Windows ({exc})") from None
    if r.returncode:
        raise ValueError(f"{p} is not reachable from Windows")
    return r.stdout.strip()


def to_local(winpath: str) -> Path:
    """Windows path -> local path (/mnt/d/... on WSL)."""
    if is_windows():
        return Path(winpath)
    m = re.match(r"^([a-zA-Z]):[\\/](.*)$", winpath.strip())
    if m:
        return Path(f"/mnt/{m[1].lower()}") / m[2].replace("\\", "/")
    try:
        r = subprocess.run(["wslpath", "-u", winpath], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"can't map {winpath} to a local path ({exc})") from None
    if r.returncode or not r.stdout.strip():  # never Path("") (= the current folder)
        raise ValueError(f"can't map {winpath} to a local path")
    return Path(r.stdout.strip())


def reg_query(key: str, value: str) -> str | None:
    """A registry value ("" = the key's default value). 64-bit view first, then the 32-bit one."""
    if is_windows():
        import winreg

        hive, _, sub = key.partition("\\")
        root = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}[hive]
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | view) as k:
                    return str(winreg.QueryValueEx(k, value)[0])
            except OSError:
                continue
        return None
    for view in ("/reg:64", "/reg:32"):
        try:
            r = run_win([system32("reg.exe"), "query", key, *(["/ve"] if value == "" else ["/v", value]), view])
        except (OSError, subprocess.TimeoutExpired):
            return None
        name = r"\(Default\)|\(Standard\)|\S*" if value == "" else re.escape(value)  # "(Default)" is localized
        m = re.search(rf"^\s*(?:{name})\s+REG_\w+\s+(.+?)\s*$", r.stdout, re.M)
        if m:
            return m[1]
    return None


def env_path(name: str) -> Path | None:
    """%LOCALAPPDATA% etc. of the Windows user, as a local path."""
    if is_windows():
        v = os.environ.get(name)
        return Path(v) if v else None
    try:
        r = run_win([system32("cmd.exe"), "/c", f"echo %{name}%"], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    v = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if not v or "%" in v:
        return None
    try:
        return to_local(v)
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------ Steam
def steam_root() -> Path | None:
    candidates = []
    v = reg_query(r"HKCU\Software\Valve\Steam", "SteamPath")
    if v:
        candidates.append(to_local(v.replace("/", "\\")))
    candidates += [to_local(r"C:\Program Files (x86)\Steam"), to_local(r"C:\Program Files\Steam")]
    return next((c for c in candidates if (c / "steam.exe").exists()), None)


def steam_libraries(root: Path) -> list[Path]:
    libs = [root / "steamapps"]
    try:
        text = (root / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        for p in re.findall(r'"path"\s+"([^"]+)"', text):
            lib = to_local(p.replace("\\\\", "\\")) / "steamapps"
            if lib not in libs and lib.is_dir():
                libs.append(lib)
    except OSError:
        pass
    return libs


def app_installed(root: Path, appid: str) -> bool:
    return any((lib / f"appmanifest_{appid}.acf").exists() for lib in steam_libraries(root))


def steam_user(root: Path) -> str | None:
    """userdata folder name (account id) of the most recent Steam login; the only one if there's just one."""
    users = sorted(p.name for p in (root / "userdata").glob("*") if p.name.isdigit() and p.name != "0")
    try:
        text = (root / "config/loginusers.vdf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for sid, body in re.findall(r'"(\d{17})"\s*\{([^}]*)\}', text):
        if re.search(r'"MostRecent"\s+"1"', body):
            acct = str(int(sid) - STEAMID64_BASE)
            if acct in users:
                return acct
    return users[0] if len(users) == 1 else None


def process_running(image: str) -> bool:
    try:
        r = run_win([system32("tasklist.exe"), "/FI", f"IMAGENAME eq {image}", "/NH"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return image.lower() in r.stdout.lower()


def kill(image: str) -> None:
    try:
        run_win([system32("taskkill.exe"), "/IM", image, "/F", "/T"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        pass


def start_detached(exe: Path, args: list[str] = (), cwd: Path | None = None) -> None:
    """Start a Windows program without waiting (Steam, the Revive injector)."""
    if is_windows():
        subprocess.Popen([str(exe), *args], cwd=cwd, close_fds=True,
                         creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        return
    subprocess.Popen([str(exe), *args], cwd=str(cwd) if cwd else None, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)


def stop_steam(root: Path, wait: float = 40) -> bool:
    """Ask Steam to exit; True if it was running."""
    if not process_running("steam.exe"):
        return False
    start_detached(root / "steam.exe", ["-shutdown"])
    end = time.time() + wait
    while time.time() < end:
        time.sleep(1)
        if not process_running("steam.exe"):
            time.sleep(2)  # let it finish writing its files
            return True
    raise RuntimeError("Steam did not close; the library was not changed")


def start_steam(root: Path) -> None:
    start_detached(root / "steam.exe")


def steamvr_running() -> bool:
    return process_running("vrmonitor.exe") or process_running("vrserver.exe")


def start_steamvr(root: Path, wait: float = 25) -> bool:
    """Start SteamVR (app 250820) and wait until it's up. Revive needs SteamVR running to provide VR; without it an
    Oculus game falls back to a flat window. No-op (returns True) if it's already running; returns False if SteamVR
    isn't installed or didn't come up in time."""
    if steamvr_running():
        return True
    if not app_installed(root, STEAMVR_APPID):
        return False
    start_detached(root / "steam.exe", ["-applaunch", STEAMVR_APPID])
    end = time.time() + wait
    while time.time() < end:
        time.sleep(1)
        if steamvr_running():
            time.sleep(2)  # let vrserver finish coming up before the game connects
            return True
    return False


def open_url(url: str) -> bool:
    """Open a link in the user's browser (the Windows browser under WSL)."""
    try:
        if is_wsl():
            # rundll32 passes the URL through untouched (cmd.exe /c start would split it at every '&')
            run_win([system32("rundll32.exe"), "url.dll,FileProtocolHandler", url], timeout=15)
            return True
        import webbrowser

        return webbrowser.open(url)
    except (OSError, subprocess.SubprocessError):
        return False


def open_folder(path: Path | str, select: bool = False) -> bool:
    """Show a folder (or, with select, a file inside its folder) in the file manager."""
    path = Path(path)
    try:
        if is_windows() or is_wsl():
            target = to_windows(path)
            exe = next((p for p in ("/mnt/c/Windows/explorer.exe",) if is_wsl() and Path(p).exists()), "explorer.exe")
            args = [exe] + ([f"/select,{target}"] if select else [target])
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_no_window())
            return True
        folder = path.parent if select or path.is_file() else path
        cmd = ["open", "-R", str(path)] if sys.platform == "darwin" and select else \
            ["open" if sys.platform == "darwin" else "xdg-open", str(folder)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


PLATFORM_DIRS = ("Meta Horizon/Support/oculus-runtime", "Oculus/Support/oculus-runtime")


@lru_cache(maxsize=1)
def oculus_platform_dir() -> Path | None:
    """Where this PC's Meta Horizon / Oculus app keeps the Oculus Platform SDK runtime (LibOVRPlatform64_1.dll, used
    by Oculus Store games for their licence check), or None."""
    if not available():
        return None
    roots = [env_path("ProgramFiles"), env_path("ProgramW6432")] + ([Path("/mnt/c/Program Files")] if is_wsl() else [])
    for root in [r for r in roots if r]:
        for sub in PLATFORM_DIRS:
            d = root / sub
            if (d / "LibOVRPlatform64_1.dll").is_file():
                return d
    return None


def meta_libraries() -> list[Path]:
    """This PC's Meta Horizon / Oculus app libraries (folders holding Software/ and Manifests/): the default one under
    Program Files and the extra ones the app registered (HKCU\\Software\\Oculus VR, LLC\\Oculus\\Libraries\\{id}
    OriginalPath)."""
    if not available():
        return []
    out: list[Path] = []
    roots = [env_path("ProgramFiles"), env_path("ProgramW6432")] + ([Path("/mnt/c/Program Files")] if is_wsl() else [])
    for root in [r for r in roots if r]:
        for sub in ("Meta Horizon/Software", "Oculus/Software"):
            out.append(root / sub)
    key = r"HKCU\Software\Oculus VR, LLC\Oculus\Libraries"
    try:
        if is_windows():
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key.partition("\\")[2]) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sub:
                        out.append(Path(str(winreg.QueryValueEx(sub, "OriginalPath")[0])))
        else:
            r = run_win([system32("reg.exe"), "query", key, "/s", "/v", "OriginalPath"])
            for m in re.finditer(r"^\s*OriginalPath\s+REG_\w+\s+(.+?)\s*$", r.stdout, re.M):
                out.append(to_local(m[1]))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    seen, result = set(), []
    for p in out:
        if p not in seen and (p / "Manifests").is_dir():
            seen.add(p)
            result.append(p)
    return result


# ------------------------------------------------------------------------------------------ firewall (pairing)
WSL_VM_CREATOR = "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}"  # Hyper-V firewall id of WSL's VM


def powershell(command: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """Run Windows PowerShell 5.1 (from Windows or WSL). PSModulePath is dropped: inherited from PowerShell 7 it keeps
    5.1 from loading its own modules (see updates._powershell)."""
    if is_windows():
        exe = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        cwd = None
    else:
        exe = system32("WindowsPowerShell/v1.0/powershell.exe")
        cwd = "/mnt/c" if Path("/mnt/c").is_dir() else None  # a \\wsl$ working dir makes Windows programs complain
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    return subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True,
                          errors="replace", timeout=timeout, cwd=cwd, env=env, **_no_window())


def wsl_networking_mode() -> str | None:
    """"mirrored" or "nat" (WSL's default, where nothing on the network can reach WSL at all); None if unknown."""
    if not is_wsl():
        return None
    try:
        out = subprocess.run(["wslinfo", "--networking-mode"], capture_output=True, text=True, timeout=10).stdout
        if out.strip():
            return out.strip().lower()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def wsl_inbound_blocked(rule: str) -> bool:
    """WSL with mirrored networking: Windows' Hyper-V firewall blocks connections from the network to WSL by default
    (no "Allow access?" prompt as for Windows programs), so a Frame's request to our pairing server just times out.
    True when that block is on and our allow rule `rule` isn't there."""
    if not is_wsl():
        return False
    try:
        out = powershell(
            f"$s = Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore -Name '{WSL_VM_CREATOR}';"
            f"$r = Get-NetFirewallHyperVRule -PolicyStore ActiveStore -Name '{rule}' -ErrorAction SilentlyContinue;"
            "\"$($s.Enabled) $($s.DefaultInboundAction) $([bool]$r)\"").stdout.split()
    except (OSError, subprocess.SubprocessError):
        return False
    return out[:3] == ["True", "Block", "False"]


def open_wsl_inbound(rule: str, title: str, ports: str, flag: Path, minutes: int = 35) -> bool:
    """Let the network reach WSL on TCP `ports` only while `flag` exists (at most `minutes`): one admin prompt (UAC)
    starts a hidden elevated PowerShell that adds a Hyper-V firewall rule, waits, and removes the rule again (only if
    it added it). Returns True once the rule is in place."""
    import base64

    script = f"""
$name = '{rule}'
$added = $false
if (-not (Get-NetFirewallHyperVRule -PolicyStore ActiveStore -Name $name -ErrorAction SilentlyContinue)) {{
    New-NetFirewallHyperVRule -Name $name -DisplayName '{title}' -Direction Inbound -VMCreatorId '{WSL_VM_CREATOR}' `
        -Protocol TCP -LocalPorts {ports} -Action Allow | Out-Null
    $added = $true
}}
$end = (Get-Date).AddMinutes({minutes})
while ((Test-Path -LiteralPath '{to_windows(flag)}') -and ((Get-Date) -lt $end)) {{ Start-Sleep -Seconds 2 }}
if ($added) {{ Remove-NetFirewallHyperVRule -Name $name -ErrorAction SilentlyContinue }}
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        powershell("Start-Process powershell -Verb RunAs -WindowStyle Hidden "
                   f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{encoded}'", timeout=300)
    except (OSError, subprocess.SubprocessError):
        return False
    for _ in range(20):  # the elevated script needs a moment to add the rule
        if not wsl_inbound_blocked(rule):
            return True
        time.sleep(1)
    return False


def network_category(ip: str) -> str | None:
    """Windows: the firewall profile of the network that has `ip` ("Public", "Private", "DomainAuthenticated")."""
    if not is_windows():
        return None
    try:
        out = powershell(f"(Get-NetConnectionProfile -InterfaceIndex (Get-NetIPAddress -IPAddress '{ip}' "
                         "-ErrorAction Stop).InterfaceIndex).NetworkCategory").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None
