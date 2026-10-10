"""AXRB (Android XR Bridge) as an external tool: Quest games on this Windows PC.

AXRB (github.com/Android-XR-Bridge/AXRB) runs Android OpenXR apps in an Android 16 emulator (x86_64 image, arm64 code
through Android's ARM translation) and shows them through the PC's OpenXR runtime (SteamVR, ...). FramePort never
bundles or modifies it: the official installer is downloaded only when a user installs a Quest game on this PC, and
FramePort talks to it through its public interfaces (its PowerShell scripts, adb). See docs/PC_ANDROID.md.

The emulator runtime (Android SDK pieces + the AVD) is set up the way AXRB's own launcher does it, in AXRB's managed
folder (%LOCALAPPDATA%\\AXRB Runtime), so AXRB's launcher sees the same runtime and games: the pinned downloads come
from the installed AXRB's own component list (resources/app.asar core/components.json).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import time
import zipfile
from pathlib import Path

from ..core import cache, winhost
from ..core.paths import write_atomic

AXRB_RELEASE = "https://api.github.com/repos/Android-XR-Bridge/AXRB/releases/latest"
APP_FOLDER = "axrb-launcher"  # electron-builder installs under %LOCALAPPDATA%\Programs\<package name>
RUNTIME_FOLDER = "AXRB Runtime"
AVD = "axrb-managed-api36"
PORT = 5584  # emulator console port of AXRB's managed AVD -> adb serial emulator-5584
SERIAL = f"emulator-{PORT}"
ADB_PORT = "5038"  # AXRB's own adb server, away from Android Studio's 5037
RUNTIME_PKG = "com.axrb.openxrruntime"
RUNTIME_APK = "out/android/runtime-arm64-v8a/axrb-openxr-runtime-debug.apk"
DOWNLOAD_HOST = "https://dl.google.com/android/repository/"
MIN_MEMORY_GB = 12
MEMORY_MB, CPU_CORES, STORAGE_GB = 8192, 4, 32  # AXRB's launcher defaults
LAUNCHER_NAME = "fp-axrb-run.ps1"


class AxrbError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------ where things are
def local_appdata() -> Path | None:
    return winhost.env_path("LOCALAPPDATA") if winhost.available() else None


def app_dir() -> Path | None:
    """The installed AXRB (folder with AXRB.exe): FRAMEPORT_AXRB_DIR, else the per-user install."""
    if os.environ.get("FRAMEPORT_AXRB_DIR"):
        d = Path(os.environ["FRAMEPORT_AXRB_DIR"])
        return d if (d / "resources" / "runtime").is_dir() else None
    local = local_appdata()
    d = local / "Programs" / APP_FOLDER if local else None
    return d if d and (d / "AXRB.exe").is_file() and (d / "resources" / "runtime").is_dir() else None


def resources(app: Path | None = None) -> Path | None:
    app = app or app_dir()
    return app / "resources" / "runtime" if app else None


def runtime_root() -> Path | None:
    """AXRB's managed emulator folder (SDK, AVD, logs)."""
    if os.environ.get("FRAMEPORT_AXRB_RUNTIME"):
        return Path(os.environ["FRAMEPORT_AXRB_RUNTIME"])
    local = local_appdata()
    return local / RUNTIME_FOLDER if local else None


def adb_exe(root: Path | None = None) -> Path | None:
    root = root or runtime_root()
    p = root / "sdk" / "platform-tools" / "adb.exe" if root else None
    return p if p and p.is_file() else None


# ------------------------------------------------------------------------------------------ app.asar
def asar_read(asar: Path, name: str) -> bytes:
    """One file from an Electron asar archive (header: pickle sizes + a JSON index; data after the header)."""
    with open(asar, "rb") as f:
        _, header_size, _, json_len = struct.unpack("<4I", f.read(16))
        index = json.loads(f.read(json_len))
        node = index
        for part in name.split("/"):
            node = (node.get("files") or {}).get(part)
            if node is None:
                raise KeyError(name)
        if "offset" not in node:
            raise KeyError(f"{name} is unpacked or a folder")
        f.seek(8 + header_size + int(node["offset"]))
        return f.read(node["size"])


def installed_version(app: Path | None = None) -> str | None:
    app = app or app_dir()
    if not app:
        return None
    try:
        return json.loads(asar_read(app / "resources" / "app.asar", "package.json")).get("version")
    except (OSError, KeyError, ValueError, struct.error):
        return None


def components(app: Path | None = None) -> list[dict]:
    """The emulator pieces the installed AXRB pins (emulator, platform-tools, build-tools, system image)."""
    app = app or app_dir()
    if not app:
        raise AxrbError("AXRB is not installed")
    items = json.loads(asar_read(app / "resources" / "app.asar", "core/components.json"))
    for c in items:  # same rule as AXRB's launcher: only Google's official repository
        if not str(c.get("url", "")).startswith(DOWNLOAD_HOST) or not (c.get("sha256") or c.get("sha1")):
            raise AxrbError(f"unexpected AXRB component download: {c.get('url')}")
    return items


# ------------------------------------------------------------------------------------------ install AXRB
def latest() -> dict | None:
    """{version, setup_url, sums_url} of AXRB's latest release."""
    rel = cache.cached_json("axrb-release.json", AXRB_RELEASE, max_age=6 * 3600)
    if not rel:
        return None
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    setup = next((n for n in assets if re.fullmatch(r"AXRB-Setup-[\w.]+\.exe", n)), None)
    sums = next((n for n in assets if n.startswith("SHA256SUMS") and n.endswith(".txt")), None)
    if not setup or not sums:
        return None
    return {"version": rel.get("tag_name", "").lstrip("v"), "setup": setup, "setup_url": assets[setup],
            "sums_url": assets[sums]}


def parse_sums(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[m[2]] = m[1].lower()
    return out


def install(progress=None, cancel=None, wait: float = 600) -> Path:
    """Download AXRB's official installer (checksum from the release's SHA256SUMS) and run it silently (per user,
    no admin). Returns the app folder."""
    if app_dir():
        return app_dir()
    rel = latest()
    if not rel:
        raise AxrbError("could not reach GitHub to find AXRB's latest release")
    sums = parse_sums(cache.http_get(rel["sums_url"], timeout=30).text)
    want = sums.get(rel["setup"])
    if not want:
        raise AxrbError(f"AXRB's release has no checksum for {rel['setup']}")
    temp = winhost.env_path("TEMP") or local_appdata()
    if not temp:
        raise AxrbError("Windows' temporary folder was not found")
    setup = cache.download(rel["setup_url"], temp / "FramePort" / rel["setup"], progress, expected_sha256=want)
    if cancel:
        cancel()
    subprocess.run([str(setup), "/S"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=wait, cwd=str(setup.parent))
    end = time.time() + wait  # the NSIS stub can return before its installer child is done
    while time.time() < end and not app_dir():
        if cancel:
            cancel()
        time.sleep(2)
    setup.unlink(missing_ok=True)
    if not app_dir():
        raise AxrbError("AXRB's installer finished but AXRB was not found in %LOCALAPPDATA%\\Programs")
    return app_dir()


# ------------------------------------------------------------------------------------------ runtime setup
def avd_config(image_win: str, memory_mb: int = MEMORY_MB, cores: int = CPU_CORES, storage_gb: int = STORAGE_GB) -> str:
    """The AVD config.ini AXRB's launcher writes (core/setup.mjs avdConfig)."""
    cfg = {"avd.ini.encoding": "UTF-8", "AvdId": AVD, "avd.ini.displayname": "AXRB", "abi.type": "x86_64",
           "hw.cpu.arch": "x86_64", "hw.cpu.ncore": cores, "hw.ramSize": memory_mb, "hw.gpu.enabled": "yes",
           "hw.gpu.mode": "host", "hw.audioOutput": "yes", "hw.audioInput": "yes", "hw.lcd.width": 1080,
           "hw.lcd.height": 1920, "hw.lcd.density": 420, "hw.keyboard": "no", "hw.mainKeys": "no", "hw.useext4": "yes",
           "disk.dataPartition.size": f"{storage_gb}G", "disk.cachePartition.size": "66MB", "vm.heapSize": 576,
           "image.sysdir.1": image_win.rstrip("\\") + "\\", "tag.id": "google_apis", "target": "android-36",
           "fastboot.forceColdBoot": "no", "fastboot.forceFastBoot": "yes", "showDeviceFrame": "no",
           "runtime.network.speed": "full", "runtime.network.latency": "none", "PlayStore.enabled": "no"}
    return "".join(f"{k}={v}\n" for k, v in cfg.items())


def _digest(c: dict) -> str:
    return c.get("sha256") or c["sha1"]


def missing_components(root: Path | None = None, app: Path | None = None) -> list[dict]:
    root = root or runtime_root()
    out = []
    for c in components(app):
        dest = root / "sdk" / c["destination"]
        try:
            ok = (dest / c["probe"]).exists() and \
                json.loads((dest / ".axrb-component.json").read_text()).get("digest") == _digest(c)
        except (OSError, ValueError):
            ok = False
        if not ok:
            out.append(c)
    return out


def runtime_hash(app: Path | None = None) -> str:
    h = hashlib.sha256()
    with open(resources(app) / RUNTIME_APK, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def setup_state(root: Path | None = None, app: Path | None = None) -> dict:
    """What the runtime still needs: {components: [...], download_bytes, avd: bool, runtime_apk: bool, ready}."""
    root = root or runtime_root()
    if not root:
        raise AxrbError("Windows' local app data folder was not found")
    comps = missing_components(root, app)
    avd = (root / "avd" / f"{AVD}.avd" / "config.ini").is_file()
    try:
        receipt = json.loads((root / "ready.json").read_text())
    except (OSError, ValueError):
        receipt = {}
    apk_ok = receipt.get("runtimeHash") == runtime_hash(app)
    return {"components": [c["id"] for c in comps], "download_bytes": sum(c["size"] for c in comps), "avd": avd,
            "runtime_apk": apk_ok, "ready": not comps and avd and apk_ok}


def setup_files(progress=None, cancel=None, root: Path | None = None, app: Path | None = None) -> None:
    """Download + verify + unpack the pinned emulator pieces and write the AVD (no emulator start yet)."""
    root = root or runtime_root()
    sdk, cache_dir = root / "sdk", root / "downloads"
    cache_dir.mkdir(parents=True, exist_ok=True)
    write_atomic(root / "license-acceptance.json",
                 json.dumps({"license": "android-sdk-license", "acceptedAt": _now()}))
    todo = missing_components(root, app)
    total = sum(c["size"] for c in todo) or 1
    done = 0
    for c in todo:
        if cancel:
            cancel()
        archive = cache_dir / f"{c['id']}.zip"
        cache.download(c["url"], archive,
                       (lambda f, base=done, size=c["size"], name=c["name"]: progress((base + f * size) / total, name))
                       if progress else None,
                       expected_sha256=c.get("sha256"), expected_sha1=c.get("sha1"))
        if cancel:
            cancel()
        staging = cache_dir / f"{c['id']}-extract"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            with zipfile.ZipFile(archive) as z:
                z.extractall(staging)
            src = staging / c["prefix"]
            if not (src / c["probe"]).exists():
                raise AxrbError(f"{c['name']}: expected files are missing from the download")
            dest = sdk / c["destination"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(src), str(dest))
            (dest / ".axrb-component.json").write_text(json.dumps({"digest": _digest(c)}))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            archive.unlink(missing_ok=True)
        done += c["size"]
    avd_dir = root / "avd" / f"{AVD}.avd"
    avd_dir.mkdir(parents=True, exist_ok=True)
    root_win = winhost.to_windows(root)
    if not (avd_dir / "config.ini").is_file():
        (avd_dir / "config.ini").write_text(avd_config(root_win + r"\sdk\system-images\android-36\google_apis\x86_64"))
    (root / "avd" / f"{AVD}.ini").write_text(
        f"avd.ini.encoding=UTF-8\npath={root_win}\\avd\\{AVD}.avd\ntarget=android-36\n")
    for d in ("android", "output"):
        (root / d).mkdir(exist_ok=True)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


# ------------------------------------------------------------------------------------------ scripts + adb
def script_env(root: Path | None = None, app: Path | None = None) -> dict[str, str]:
    """Environment every AXRB script needs when it runs without AXRB's launcher (Windows paths)."""
    root_win = winhost.to_windows(root or runtime_root())
    res_win = winhost.to_windows(resources(app))
    return {"AXRB_DATA_HOME": root_win + r"\output", "ANDROID_AVD_HOME": root_win + r"\avd",
            "ANDROID_USER_HOME": root_win + r"\android", "ANDROID_HOME": root_win + r"\sdk",
            "ANDROID_SDK_ROOT": root_win + r"\sdk", "ANDROID_ADB_SERVER_PORT": ADB_PORT, "ADB_USB_LEGACY": "1",
            "ADB_LOCAL_TRANSPORT_MAX_PORT": "5683", "FP_AXRB_PYTHON": res_win + r"\tools\python"}


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def ps_command(script: str, args: dict, root: Path | None = None, app: Path | None = None,
               log_win: str | None = None) -> str:
    """A PowerShell command line that sets AXRB's environment and runs one of its scripts (its output appended to
    log_win when given)."""
    env = script_env(root, app)
    sets = "; ".join(f"$env:{k} = {_ps_quote(v)}" for k, v in env.items() if k != "FP_AXRB_PYTHON")
    path = f"$env:PATH = {_ps_quote(env['FP_AXRB_PYTHON'])} + ';' + $env:PATH; $env:ADB_SERVER_SOCKET = $null"
    parts = []
    for k, v in args.items():
        if v is True:
            parts.append(f"-{k}")
        elif v not in (None, False):
            parts.append(f"-{k} {_ps_quote(v)}")
    call = f"& {_ps_quote(winhost.to_windows(resources(app) / script))} {' '.join(parts)}"
    if log_win:
        return f"$ErrorActionPreference = 'Stop'; {sets}; {path}; {logged(call, log_win)}"
    return f"$ErrorActionPreference = 'Stop'; {sets}; {path}; {call}; exit $LASTEXITCODE"


EXIT_MARK = "FP_EXIT"


def logged(call: str, log_win: str) -> str:
    """A PowerShell call with all its output appended to a log, including the error that ended it (AXRB's scripts
    throw their failure reason), and a final "FP_EXIT <code>" line (the caller can't see the exit code of a process
    started through WMI)."""
    log = _ps_quote(log_win)
    return (f"try {{ {call} *>> {log}; $c = $(if ($LASTEXITCODE) {{ $LASTEXITCODE }} else {{ 0 }}) }} "
            f"catch {{ ($_ | Out-String) *>> {log}; $c = 1 }}; \"{EXIT_MARK} $c\" *>> {log}; exit $c")


def exit_code(log: Path) -> int | None:
    found = re.findall(rf"^{EXIT_MARK} (-?\d+)\s*$", read_log(log, 1 << 16), re.M)
    return int(found[-1]) if found else None


def powershell_win() -> str:
    """powershell.exe as a Windows path."""
    if winhost.is_windows():
        return powershell_exe()
    root = winhost.env_path("SystemRoot")
    return (winhost.to_windows(root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe") if root
            else r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")


def spawn_detached(command: str) -> int:
    """Start a hidden PowerShell running `command` through WMI (Win32_Process.Create) and return its process id.
    Nothing is inherited from FramePort: the emulator and adb server an AXRB script leaves running would otherwise hold
    FramePort's pipes (or WSL's interop relay, which exists even for /dev/null) open, and the call would never end."""
    encoded = base64.b64encode(command.encode("utf-16-le")).decode()
    cmdline = (f'"{powershell_win()}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden '
               f"-EncodedCommand {encoded}")
    ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow = [uint16]0}; "
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          f"@{{CommandLine = {_ps_quote(cmdline)}; CurrentDirectory = 'C:\\'; ProcessStartupInformation = $si}}; "
          '"$($r.ReturnValue) $($r.ProcessId)"')
    r = winhost.powershell(ps, timeout=90)
    last = (r.stdout.strip().splitlines() or [""])[-1].split()
    if len(last) != 2 or last[0] != "0" or not last[1].isdigit():
        raise AxrbError(f"could not start PowerShell for AXRB: {(r.stdout + r.stderr).strip()[-300:]}")
    return int(last[1])


def pid_running(pid: int) -> bool:
    try:
        r = winhost.run_win([winhost.system32("tasklist.exe"), "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return True  # unknown: keep waiting until the deadline
    return f'"{pid}"' in r.stdout


def powershell_exe() -> str:
    if winhost.is_windows():
        return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    return winhost.system32("WindowsPowerShell/v1.0/powershell.exe")


def run_script(script: str, args: dict, log: Path, timeout: float, root: Path | None = None,
               app: Path | None = None) -> int:
    """Run an AXRB script detached (spawn_detached), its output in the log file, and wait for it. Returns its exit
    code (1 when it ended without saying)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    log.unlink(missing_ok=True)
    pid = spawn_detached(ps_command(script, args, root, app, winhost.to_windows(log)))
    end = time.time() + timeout
    while pid_running(pid):
        if time.time() > end:
            raise AxrbError(f"AXRB's {Path(script).name} didn't finish within {timeout / 60:.0f} minutes")
        time.sleep(2)
    code = exit_code(log)
    return 1 if code is None else code


def adb(args: list[str], timeout: float = 60, serial: bool = True,
        root: Path | None = None) -> subprocess.CompletedProcess:
    exe = adb_exe(root)
    if not exe:
        raise AxrbError("AXRB's Android runtime is not set up")
    cmd = [str(exe), "-P", ADB_PORT] + (["-s", SERIAL] if serial else []) + args
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                          timeout=timeout)


def start_adb_server(root: Path | None = None) -> None:
    """Start AXRB's adb server ourselves with no inherited pipes (see run_script)."""
    exe = adb_exe(root)
    if exe:
        subprocess.run([str(exe), "-P", ADB_PORT, "start-server"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


def emulator_online(root: Path | None = None) -> bool:
    try:
        r = adb(["devices"], timeout=20, serial=False, root=root)
    except (AxrbError, OSError, subprocess.TimeoutExpired):
        return False
    return bool(re.search(rf"^{SERIAL}\s+device\s*$", r.stdout, re.M))


def log_dir(root: Path | None = None) -> Path:
    return (root or runtime_root()) / "output" / "logs"


def read_log(path: Path, limit: int = 1 << 20) -> str:
    """A log written on Windows: Windows PowerShell 5.1's redirection writes UTF-16 with a BOM, others UTF-8."""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    else:
        text = data.decode("utf-8-sig", errors="replace")
    return text[-limit:]


def start_emulator(root: Path | None = None, app: Path | None = None, timeout: float = 1200) -> bool:
    """Boot AXRB's AVD headless (AXRB's own Start action: clock adapter, GPU layer, guest checks). Returns True if
    this call started it (the caller then stops it again)."""
    if emulator_online(root):
        return False
    start_adb_server(root)
    log = log_dir(root) / "frameport-emulator-start.log"
    rc = run_script("scripts/emulator/windows_android_emulator.ps1",
                    {"Action": "Start", "Sdk": winhost.to_windows((root or runtime_root()) / "sdk"), "Avd": AVD,
                     "Port": PORT, "ApiLevel": 36, "Abi": "arm64-v8a", "MemoryMB": MEMORY_MB, "CpuCores": CPU_CORES,
                     "GuestClock": "TscCorrected", "GpuSharing": True}, log, timeout, root, app)
    if rc != 0 or not emulator_online(root):
        tail = read_log(log, 600)
        raise AxrbError(f"Android (AXRB's emulator) did not start: {tail.strip() or f'exit {rc}'}")
    return True


def stop_emulator(root: Path | None = None, wait: float = 300) -> None:
    try:
        adb(["shell", "sync"], timeout=60, root=root)
        adb(["emu", "kill"], timeout=30, root=root)
    except (AxrbError, OSError, subprocess.TimeoutExpired):
        return
    lock = (root or runtime_root()) / "avd" / f"{AVD}.avd" / "hardware-qemu.ini.lock"
    end = time.time() + wait  # shutdown writes a quick-boot snapshot: can take a while, never fatal
    while time.time() < end and (emulator_online(root) or lock.exists()):
        time.sleep(2)


def ensure_runtime_apk(root: Path | None = None, app: Path | None = None) -> None:
    """AXRB's OpenXR runtime inside Android (reinstalled when AXRB updated it), and AXRB's ready receipt."""
    root = root or runtime_root()
    want = runtime_hash(app)
    try:
        receipt = json.loads((root / "ready.json").read_text())
    except (OSError, ValueError):
        receipt = {}
    have = "package:" in adb(["shell", "pm", "path", RUNTIME_PKG], root=root).stdout
    if not have or receipt.get("runtimeHash") != want:
        r = adb(["install", "--no-incremental", "--force-queryable", "-r",
                 winhost.to_windows(resources(app) / RUNTIME_APK)], timeout=240, root=root)
        if "Success" not in r.stdout:
            raise AxrbError(f"AXRB's runtime didn't install: {(r.stdout + r.stderr).strip()[-300:]}")
    write_atomic(root / "ready.json", json.dumps({"version": 1, "runtimeHash": want, "completedAt": _now()}))


def requirements(app: Path | None = None) -> dict:
    """AXRB's own hardware check: {hypervisor, gpu, supportedGpu, memoryGB, x64} (+ ok)."""
    app = app or app_dir()
    if not app:
        return {}
    script = winhost.to_windows(resources(app) / "scripts" / "emulator" / "check_windows.ps1")
    r = winhost.powershell(f"& {_ps_quote(script)}", timeout=90)
    try:
        info = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": (r.stdout + r.stderr).strip()[-300:]}
    info["ok"] = bool(info.get("hypervisor") and info.get("supportedGpu") and info.get("x64")
                      and (info.get("memoryGB") or 0) >= MIN_MEMORY_GB)
    return info


def requirement_problems(info: dict) -> list[str]:
    out = []
    if info.get("error"):
        return [f"AXRB's hardware check failed: {info['error']}"]
    if not info.get("x64"):
        out.append("needs a 64-bit (x64) Windows PC")
    if (info.get("memoryGB") or 0) < MIN_MEMORY_GB:
        out.append(f"needs at least {MIN_MEMORY_GB} GB of memory ({info.get('memoryGB')} GB found)")
    if not info.get("supportedGpu"):
        out.append(f"needs an NVIDIA or AMD graphics card ({info.get('gpu') or 'none found'})")
    if not info.get("hypervisor"):
        out.append("needs Windows Hypervisor Platform: turn it on in 'Turn Windows features on or off', then restart")
    return out


def status() -> dict:
    """For `frameport pc info` / Settings: what is installed and set up (no downloads, no emulator start)."""
    app = app_dir()
    out = {"installed": bool(app), "version": installed_version(app), "app": str(app) if app else None,
           "runtime": str(runtime_root()) if runtime_root() else None}
    if app:
        try:
            out["setup"] = setup_state(app=app)
        except (AxrbError, OSError, KeyError, ValueError) as exc:
            out["setup"] = {"error": str(exc)}
    return out


# ------------------------------------------------------------------------------------------ the Play launcher
LAUNCHER = r"""param([Parameter(Mandatory)][string]$Package, [Parameter(Mandatory)][string]$Activity,
      [string]$GameName = $Package, [switch]$CaptureGuestLog)
# Written by FramePort: starts a Quest game installed on this PC through AXRB's own run script, with AXRB's managed
# Android runtime (what AXRB's launcher passes it). AXRB boots the emulator when needed and shuts it down afterwards.
$ErrorActionPreference = 'Stop'
@ENV@
$env:ADB_SERVER_SOCKET = $null
$env:PATH = (Join-Path @RES@ 'tools\python') + ';' + $env:PATH
$run = Join-Path @RES@ 'scripts\run\run_windows_game.ps1'
& $run -Avd '@AVD@' -Port @PORT@ -Sdk $env:ANDROID_HOME -MemoryMB @MEM@ -CpuCores @CORES@ -TextureMode gpu `
    -Package $Package -Activity $Activity -GameName $GameName -GuestClock Auto -CaptureGuestLog:$CaptureGuestLog `
    -RuntimeApk (Join-Path @RES@ '@APK@')
exit $LASTEXITCODE
"""


def launcher_text(root: Path | None = None, app: Path | None = None) -> str:
    env = script_env(root, app)
    sets = "\n".join(f"$env:{k} = {_ps_quote(v)}" for k, v in env.items() if k != "FP_AXRB_PYTHON")
    return (LAUNCHER.replace("@ENV@", sets).replace("@RES@", _ps_quote(winhost.to_windows(resources(app))))
            .replace("@AVD@", AVD).replace("@PORT@", str(PORT)).replace("@MEM@", str(MEMORY_MB))
            .replace("@CORES@", str(CPU_CORES)).replace("@APK@", RUNTIME_APK.replace("/", "\\")))


def write_launcher(root: Path | None = None, app: Path | None = None) -> Path:
    """FramePort's launcher script in %LOCALAPPDATA%\\FramePort\\axrb (Steam shortcuts point at it). Local path."""
    local = local_appdata()
    if not local:
        raise AxrbError("Windows' local app data folder was not found")
    path = local / "FramePort" / "axrb" / LAUNCHER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    text = launcher_text(root, app)
    if not path.exists() or path.read_text(encoding="utf-8-sig", errors="replace") != text:
        path.write_text(text, encoding="utf-8-sig")  # BOM: Windows PowerShell 5.1 reads BOM-less files as ANSI
    return path


def launcher_args(launcher_win: str, package: str, activity: str, title: str) -> str:
    """Steam shortcut launch options for powershell.exe."""
    name = re.sub(r'["`$]', "", title).strip() or package
    return (f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{launcher_win}" '
            f'-Package {package} -Activity {activity} -GameName "{name}"')
