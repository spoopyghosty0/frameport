"""Self-update from GitHub releases (no Flet here: the GUI, the CLI and tests share it).

How FramePort is installed decides how it updates (`install_kind()`):
  bundle  the app from a release archive (Windows FramePort.exe folder, macOS FramePort.app, Linux FramePort/ folder):
          `prepare()` downloads this platform's archive, checks it against the release's SHA256SUMS.txt (and on Windows
          that FramePort.exe has the same code-signing certificate as the running one), unpacks it into
          <data>/updates/<version>/; `apply()` starts a small script that waits for FramePort to exit, puts the new
          version in place (Windows: copies the new files over the old ones, since the zip has no top folder of its
          own; macOS/Linux: swaps the .app / folder, keeping the old one until the new one is in place), clears macOS
          quarantine and starts the new version. Its log: <data>/logs/update.log.
  wheel   installed with `uv tool install` / pipx / pip from the release's wheel: reinstall from the new wheel.
  source  a git checkout: `git pull --ff-only` + `uv sync`.
User data lives outside the program folder (core/paths.user_data_dir), so updates never touch libraries or settings.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import REPO_URL, __version__
from .core import cache, library
from .core.events import Reporter
from .core.paths import user_data_dir

_log = logging.getLogger("frameport.updates")

REPO = REPO_URL.removeprefix("https://github.com/").strip("/")
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
DEV_API = f"https://api.github.com/repos/{REPO}/releases/tags/dev"  # the rolling pre-release CI publishes on demand
CHECK_EVERY = 6 * 3600          # seconds between automatic checks
ASSETS = {"win32": "FramePort-windows-x64.zip", "darwin": "FramePort-macos-arm64.zip",
          "linux": "FramePort-linux-x64.tar.gz", "linux-arm64": "FramePort-linux-arm64.tar.gz"}  # never rename
SUMS = "SHA256SUMS.txt"


class UpdateError(RuntimeError):
    pass


@dataclass
class Update:
    version: str            # "0.3.0"
    tag: str                # "v0.3.0"
    notes: str              # release notes (markdown)
    page: str               # release page URL
    asset: str | None       # this platform's archive name, if the release has one
    asset_url: str | None
    sums_url: str | None
    wheel_url: str | None
    published: str = ""


# ------------------------------------------------------------------------------------------------- versions
def parse_version(text: str) -> tuple:
    """(major, minor, patch, final, dev) from "v1.2.3" / "1.2.3-rc1" / "1.2.4.dev57": pre-releases and dev builds sort
    before the final release, dev builds among themselves by build number."""
    m = re.match(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$", (text or "").strip())
    if not m:
        return (0, 0, 0, 0, 0)
    rest = m.group(4).strip()
    dev = re.match(r"[.-]?dev(\d+)", rest)
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0), 0 if rest else 1,
            int(dev.group(1)) if dev else 0)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def platform_asset() -> str | None:
    key = "linux" if sys.platform.startswith("linux") else sys.platform
    if key == "linux" and platform.machine().lower() in ("aarch64", "arm64"):
        key = "linux-arm64"  # the ARM64 bundle (from 0.6.0)
    return ASSETS.get(key)


def update_from_release(release: dict, asset_name: str | None = None, dev: bool = False) -> Update | None:
    """The Update a GitHub "latest release" JSON describes (None for drafts/pre-releases or unusable data). dev=True:
    the rolling `dev` pre-release, whose version comes from its wheel (frameport-0.9.1.dev57-py3-none-any.whl)."""
    if (not isinstance(release, dict) or release.get("draft") or (release.get("prerelease") and not dev)
            or not release.get("tag_name")):
        return None
    assets = {a.get("name"): a.get("browser_download_url") for a in release.get("assets") or [] if isinstance(a, dict)}
    asset_name = asset_name if asset_name is not None else platform_asset()
    wheel_name = next((n for n in assets if n and n.endswith(".whl")), None)
    wheel = assets.get(wheel_name) if wheel_name else None
    tag = release["tag_name"]
    version = tag.lstrip("v")
    if dev:
        m = re.match(r"frameport-([^-]+)-", wheel_name or "")
        if not m:
            return None
        version = m.group(1)
    return Update(version=version, tag=tag, notes=release.get("body") or "", page=release.get("html_url") or "",
                  asset=asset_name if asset_name in assets else None, asset_url=assets.get(asset_name),
                  sums_url=assets.get(SUMS), wheel_url=wheel, published=release.get("published_at") or "")


# ------------------------------------------------------------------------------------------------- checking
def checks_disabled() -> bool:
    """Automatic checks off (setting, or FRAMEPORT_NO_UPDATE_CHECK=1). "Check now" / `frameport update` still work."""
    return bool(os.environ.get("FRAMEPORT_NO_UPDATE_CHECK")) or not library.setting("update.auto_check", True)


def check(force: bool = False) -> Update | None:
    """The newer release, or None (up to date, skipped by the user unless forced, or offline: never raises)."""
    try:
        data = cache.cached_json("app-release.json", LATEST_API, max_age=0 if force else CHECK_EVERY)
    except Exception:  # noqa: BLE001
        data = None
    library.set_setting("update.last_check", time.time())
    up = update_from_release(data) if data else None
    if not up or not is_newer(up.version):
        return None
    if not force and hidden(up.version):
        return None
    return up


def check_dev() -> Update | None:
    """The latest dev build (Settings → "Install the latest dev build"), or None when none is published or GitHub
    can't be reached. Returned even when it isn't newer: the caller says so."""
    try:
        data = cache.cached_json("app-dev-release.json", DEV_API, max_age=0)
    except Exception:  # noqa: BLE001
        return None
    return update_from_release(data, dev=True) if data else None


def refresh_cache() -> None:
    """Fetch the latest release into the cache only (no settings written: safe in a CLI background thread)."""
    try:
        cache.cached_json("app-release.json", LATEST_API, max_age=0)
    except Exception:  # noqa: BLE001
        pass


def cached_update() -> Update | None:
    """What the last check found, without touching the network (for the CLI's one-line hint)."""
    path = cache.cache_dir() / "app-release.json"
    try:
        up = update_from_release(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if not up or not is_newer(up.version) or hidden(up.version):
        return None
    return up


def cache_age() -> float | None:
    path = cache.cache_dir() / "app-release.json"
    return time.time() - path.stat().st_mtime if path.exists() else None


def skip(version: str) -> None:
    """"Skip this version": never offered again automatically ("Check now" still finds it)."""
    library.set_setting("update.skipped", version)


def snooze(version: str, hours: float = 24) -> None:
    """The banner's "Later": hide this version for a while (a day by default), then offer it again."""
    library.set_setting("update.snoozed", {"version": version, "until": time.time() + hours * 3600})


def hidden(version: str) -> bool:
    """True when the user skipped this version, or snoozed it and the snooze hasn't run out yet."""
    if library.setting("update.skipped") == version:
        return True
    snoozed = library.setting("update.snoozed")
    if isinstance(snoozed, dict) and snoozed.get("version") == version:
        try:
            return time.time() < float(snoozed.get("until") or 0)
        except (TypeError, ValueError):
            return False
    return False


# ------------------------------------------------------------------------------------------------- how installed
def running_executable() -> Path | None:
    try:
        import psutil

        exe = psutil.Process().exe()
        if exe:
            return Path(exe)
    except Exception:  # noqa: BLE001
        pass
    return Path(sys.executable) if sys.executable else None


def bundle_root(exe: Path | None = None, platform: str | None = None) -> Path | None:
    """The installed app (Windows: the folder with FramePort.exe; macOS: FramePort.app; Linux: the folder with the
    FramePort executable) when running from a release bundle, else None."""
    exe = exe or running_executable()
    platform = platform or sys.platform
    if not exe:
        return None
    if platform == "darwin":
        for parent in [exe, *exe.parents]:
            if parent.suffix == ".app" and parent.name.lower().startswith("frameport"):
                return parent
        return None
    if platform == "win32":
        return exe.parent if exe.name.lower() == "frameport.exe" else None
    return exe.parent if exe.name == "FramePort" else None


def source_root() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    return root if (root / ".git").exists() and (root / "pyproject.toml").exists() else None


def install_kind() -> str:
    if bundle_root():
        return "bundle"
    if source_root():
        return "source"
    return "wheel"


def wheel_installer() -> str:
    prefix = sys.prefix.replace("\\", "/").lower()
    if "/uv/tools/" in prefix:
        return "uv"
    if "/pipx/venvs/" in prefix:
        return "pipx"
    return "pip"


UPGRADE_TIMEOUT = 1800  # seconds for one git/uv/pip step of a source or wheel update


def upgrade_env() -> dict:
    """Environment for git/uv/pip during an update: never stop at a credential or confirmation prompt."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "PIP_NO_INPUT": "1"}


def upgrade_commands(up: Update, kind: str | None = None) -> list[list[str]]:
    """Commands that update a wheel or source install (bundles use prepare()/apply())."""
    kind = kind or install_kind()
    if kind == "source":
        root = str(source_root())
        return [["git", "-C", root, "pull", "--ff-only"], ["uv", "sync", "--project", root]]
    if kind == "wheel":
        if not up.wheel_url:
            raise UpdateError(f"release {up.tag} has no wheel to install from")
        tool = wheel_installer()
        if tool == "uv":
            return [["uv", "tool", "install", "--force", up.wheel_url]]
        if tool == "pipx":
            return [["pipx", "install", "--force", up.wheel_url]]
        return [[sys.executable, "-m", "pip", "install", "--upgrade", up.wheel_url]]
    raise UpdateError("bundles update with prepare()/apply()")


def source_is_dirty() -> bool:
    root = source_root()
    if not root:
        return False
    out = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                         capture_output=True, text=True)
    return bool(out.stdout.strip())


# ------------------------------------------------------------------------------------------------- bundles
def updates_dir() -> Path:
    path = user_data_dir() / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_sums(text: str) -> dict[str, str]:
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract(archive: Path, dest: Path, platform: str) -> Path:
    """Unpack and return the app inside (FramePort.app / the folder with FramePort[.exe])."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    if platform == "darwin":
        # ditto keeps the app bundle's symlinks and permissions (Python's zipfile doesn't)
        subprocess.run(["ditto", "-x", "-k", str(archive), str(dest)], check=True, timeout=900)
        app = next((p for p in dest.iterdir() if p.suffix == ".app"), None)
        if not app or not (app / "Contents/MacOS").is_dir():
            raise UpdateError("the downloaded archive doesn't contain FramePort.app")
        return app
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
        if not (dest / "FramePort.exe").exists():
            raise UpdateError("the downloaded archive doesn't contain FramePort.exe")
        return dest
    with tarfile.open(archive) as t:
        t.extractall(dest, filter="tar") if hasattr(tarfile, "data_filter") else t.extractall(dest)
    folder = dest / "FramePort"
    if not (folder / "FramePort").is_file():
        raise UpdateError("the downloaded archive doesn't contain FramePort/FramePort")
    return folder


def _powershell() -> tuple[str, dict]:
    """Windows PowerShell 5.1 (always installed) and an environment it can load its own modules in: started from
    PowerShell 7 (pwsh), PSModulePath points at pwsh's modules and 5.1 fails to load Microsoft.PowerShell.Security /
    .Management (Get-AuthenticodeSignature, Copy-Item). Without PSModulePath it rebuilds its default."""
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    return (str(exe) if exe.exists() else "powershell"), env


START_TIMEOUT = 15  # seconds for the swap script to log that it runs


def spawn_hidden(cmd: list[str], env: dict | None = None) -> subprocess.Popen:
    """Windows: start a console program in its own hidden console, independent of FramePort. DETACHED_PROCESS
    doesn't work for PowerShell: started that way from the packaged app it exits 0 without running anything."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return subprocess.Popen(cmd, creationflags=0x00000010 | 0x00000200,  # CREATE_NEW_CONSOLE, NEW_PROCESS_GROUP
                            startupinfo=si, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env)


def _signer_thumbprint(exe: Path) -> str | None:
    """Windows: the Authenticode signer certificate's thumbprint ('' if unsigned, None if it can't be read)."""
    ps, env = _powershell()
    try:
        out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command",
                              f"(Get-AuthenticodeSignature -LiteralPath {_ps_quote(exe)})"
                              ".SignerCertificate.Thumbprint"],
                             capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("reading the signature of %s failed: %s", exe, exc)
        return None
    if out.returncode:
        _log.warning("reading the signature of %s failed (%s): %s", exe, out.returncode, out.stderr.strip()[-500:])
        return None
    return out.stdout.strip()


def prepare(up: Update, reporter: Reporter | None = None, platform: str | None = None,
            current: Path | None = None) -> Path:
    """Download, verify and unpack the new version. Returns the unpacked app, ready for apply()."""
    reporter = reporter or Reporter()
    platform = platform or sys.platform
    if not up.asset_url or not up.asset:
        raise UpdateError(f"release {up.tag} has no download for this system; get it from {up.page}")
    if not up.sums_url:
        raise UpdateError(f"release {up.tag} has no {SUMS}; refusing an unverified update")
    work = updates_dir() / up.version
    work.mkdir(parents=True, exist_ok=True)
    reporter.stage("Check the release")
    expected = parse_sums(cache.http_get(up.sums_url).text).get(up.asset)
    if not expected:
        raise UpdateError(f"{SUMS} of {up.tag} doesn't list {up.asset}")
    archive = work / up.asset
    if archive.exists() and _sha256(archive) == expected:
        reporter.log("already downloaded")
    else:
        reporter.stage(f"Download FramePort {up.version}")
        cache.download(up.asset_url, archive, progress=lambda f: reporter.progress(f, f"{f:.0%}"),
                       expected_sha256=expected)
    reporter.check("Checksum matches the release", True, expected[:16] + "…")
    reporter.stage("Unpack")
    app = _extract(archive, work / "staged", platform)
    if platform == "win32":
        current = current or bundle_root()
        mine = _signer_thumbprint(current / "FramePort.exe") if current else None
        if mine:  # the running build is signed: the new one must carry the same certificate
            theirs = _signer_thumbprint(app / "FramePort.exe")
            if theirs != mine:
                raise UpdateError("the new FramePort.exe isn't signed with FramePort's certificate")
            reporter.check("Signed with FramePort's certificate", True, mine[:16] + "…")
    (work / "ready.json").write_text(json.dumps({"version": up.version, "app": str(app), "update": asdict(up)}))
    reporter.log(f"FramePort {up.version} is ready to install")
    return app


def ready_update() -> tuple[str, Path] | None:
    """A prepared (downloaded + verified) newer version waiting to be installed: (version, app)."""
    best = None
    for marker in updates_dir().glob("*/ready.json"):
        try:
            info = json.loads(marker.read_text())
            app = Path(info["app"])
        except (OSError, ValueError, KeyError):
            continue
        if app.exists() and is_newer(info["version"]) and (best is None or is_newer(info["version"], best[0])):
            best = (info["version"], app)
    return best


def ready_update_info() -> Update | None:
    """The release a prepared update came from (for installing it without asking GitHub again)."""
    ready = ready_update()
    if not ready:
        return None
    try:
        return Update(**json.loads((updates_dir() / ready[0] / "ready.json").read_text())["update"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def cleanup_old() -> None:
    """Remove downloads of versions that are no longer newer than the running one."""
    for d in updates_dir().iterdir():
        if d.is_dir() and not is_newer(d.name):
            shutil.rmtree(d, ignore_errors=True)


def can_replace(target: Path) -> bool:
    return target.exists() and os.access(target, os.W_OK) and os.access(target.parent, os.W_OK)


def _ps_quote(path: Path | str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _sh_quote(path: Path | str) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def swap_script(app: Path, target: Path, pid: int, platform: str, relaunch: bool, log: Path) -> str:
    """The script that installs `app` over `target` once process `pid` has exited (text; see apply())."""
    if platform == "win32":
        start = f"Start-Process -FilePath {_ps_quote(target / 'FramePort.exe')} -WorkingDirectory {_ps_quote(target)}" \
            if relaunch else "Write-Log 'not relaunching'"
        return (f"""$ErrorActionPreference = 'Stop'
$log = {_ps_quote(log)}
function Write-Log($m) {{ Add-Content -LiteralPath $log -Value ("$(Get-Date -Format s) $m") }}
Write-Log 'update: waiting for FramePort (pid {pid}) to exit'
for ($i = 0; $i -lt 240 -and (Get-Process -Id {pid} -ErrorAction SilentlyContinue); $i++) {{ Start-Sleep"""
f""" -Milliseconds 500 }}
Start-Sleep -Milliseconds 500
$src = {_ps_quote(app)}; $dst = {_ps_quote(target)}
$backup = Join-Path (Split-Path -Parent $src) 'previous'
try {{
  if (Test-Path -LiteralPath $backup) {{ Remove-Item -LiteralPath $backup -Recurse -Force }}
  New-Item -ItemType Directory -Path $backup | Out-Null
  # keep the files that will be replaced, so a failed copy can be undone
  Get-ChildItem -LiteralPath $src -Recurse -File | ForEach-Object {{
    $rel = $_.FullName.Substring($src.Length).TrimStart('\\')
    $old = Join-Path $dst $rel
    if (Test-Path -LiteralPath $old) {{
      $keep = Join-Path $backup $rel
      New-Item -ItemType Directory -Force -Path (Split-Path -Parent $keep) | Out-Null
      Copy-Item -LiteralPath $old -Destination $keep -Force
    }}
  }}
  Copy-Item -Path (Join-Path $src '*') -Destination $dst -Recurse -Force
  Write-Log 'update: installed'
}} catch {{
  Write-Log "update FAILED: $_ (restoring the previous files)"
  Copy-Item -Path (Join-Path $backup '*') -Destination $dst -Recurse -Force -ErrorAction SilentlyContinue
}}
{start}
""")
    if platform == "darwin":
        start = f"open {_sh_quote(target)}" if relaunch else "echo 'not relaunching' >>\"$log\""
        extra = f"xattr -dr com.apple.quarantine {_sh_quote(target)} 2>/dev/null || true"
    else:
        start = (f"nohup {_sh_quote(target / 'FramePort')} >/dev/null 2>&1 &" if relaunch
                 else "echo 'not relaunching' >>\"$log\"")
        extra = ":"
    return f"""#!/bin/sh
log={_sh_quote(log)}
echo "$(date) update: waiting for FramePort (pid {pid}) to exit" >>"$log"
i=0; while kill -0 {pid} 2>/dev/null && [ $i -lt 240 ]; do sleep 0.5; i=$((i+1)); done
src={_sh_quote(app)}; dst={_sh_quote(target)}; old="$dst.old"
rm -rf "$old"
if mv "$dst" "$old" && mv "$src" "$dst"; then
  {extra}
  rm -rf "$old"
  echo "$(date) update: installed" >>"$log"
else
  echo "$(date) update FAILED: restoring the previous version" >>"$log"
  [ -e "$dst" ] || mv "$old" "$dst"
fi
{start}
"""


def apply(app: Path, target: Path | None = None, relaunch: bool = True, pid: int | None = None,
          platform: str | None = None, wait: bool = False) -> Path:
    """Start the swap script (detached unless wait=True) and return its path; the caller then quits FramePort."""
    platform = platform or sys.platform
    target = target or bundle_root()
    if not target:
        raise UpdateError("FramePort isn't running from a release bundle")
    if not can_replace(target):
        raise UpdateError(f"no permission to replace {target}")
    pid = os.getpid() if pid is None else pid
    log = user_data_dir() / "logs" / "update.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    script = updates_dir() / ("apply.ps1" if platform == "win32" else "apply.sh")
    script.write_text(swap_script(app, target, pid, platform, relaunch, log), encoding="utf-8")
    started = log.stat().st_size if log.exists() else 0
    if platform == "win32":
        ps, env = _powershell()
        proc = spawn_hidden([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle",
                             "Hidden", "-File", str(script)], env=env)
    else:
        proc = subprocess.Popen(["/bin/sh", str(script)], start_new_session=not wait, close_fds=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait:  # make sure the script really runs before FramePort quits (it logs first thing)
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if log.exists() and f"(pid {pid})" in log.read_text(errors="replace")[started:]:
                break
            if proc.poll() not in (None, 0):
                break
            time.sleep(0.2)
        else:
            raise UpdateError(f"the update script didn't start (see {log})")
        if proc.poll() not in (None, 0):
            raise UpdateError(f"the update script failed to start (exit {proc.returncode})")
    if wait:
        proc.wait()
    return script
