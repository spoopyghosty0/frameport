"""Uninstall FramePort: remove every file it generated.

This PC:
  - the data folder (user_data_dir: tools, library, artwork, cache, builds, logs, remembered Frames, the app's SSH key
    and the per-game signing keys — back those up first: updates must be signed with the same key)
  - Steam shortcuts FramePort added for PC VR games (Windows Steam) and their grid artwork
  - the Revive copy under %LOCALAPPDATA%\\FramePort (WSL only; on Windows that *is* the data folder)
The Frame (optional; needs a connection): FramePort's games and their Steam entries (saves optionally kept),
~/Applications/quest-frame and ~/.local/share/frameport. Proton/Lepton stay (Steam apps you can remove in Steam).
The FramePort program itself: delete its folder (portable app) or `uv tool uninstall` / `pip uninstall`.
"""
from __future__ import annotations

import os
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .core import applog, paths
from .core.events import Reporter
from .core.paths import user_data_dir


@dataclass
class Item:
    what: str
    path: str | None = None
    size: int = 0
    kind: str = "pc"  # pc | steam | frame


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    keys: list[Path] = field(default_factory=list)  # signing keystores (backup candidates)

    @property
    def size(self) -> int:
        return sum(i.size for i in self.items)


def _tree_size(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def keystores() -> list[Path]:
    root = user_data_dir()
    return sorted({*root.glob("overport-workspace/signatures/*.keystore"), *root.glob("keystores/**/*.keystore"),
                   *root.glob("keystores/**/*.jks")})


def wsl_revive_copy() -> Path | None:
    from .core import winhost

    if not winhost.is_wsl():
        return None
    local = winhost.env_path("LOCALAPPDATA")
    p = local / "FramePort" if local else None
    return p if p and p.exists() else None


def plan(frame_info: dict | None = None) -> Plan:
    from .targets.pc_revive import local_installs

    pl = Plan(keys=keystores())
    data = user_data_dir()
    pl.items.append(Item("FramePort data (tools, library, artwork, cache, builds, keys)", str(data), _tree_size(data)))
    for pkg, dep in local_installs().items():
        pl.items.append(Item(f"Steam shortcut on this PC: {dep.get('title') or pkg}", kind="steam"))
    rv = wsl_revive_copy()
    if rv:
        pl.items.append(Item("Revive copy for Windows", str(rv), _tree_size(rv)))
    for d in (frame_info or {}).get("installed") or []:
        pl.items.append(Item(f"On the Frame: {d.get('title') or d['package']}", d.get("base"), d.get("apk_size") or 0,
                             kind="frame"))
    return pl


def backup_keys(dest: Path) -> Path | None:
    """Zip the signing keystores (and the app's SSH key) to dest (a folder or a .zip path)."""
    keys = keystores()
    ssh = list((user_data_dir() / "ssh").glob("*"))
    if not keys and not ssh:
        return None
    dest = Path(dest)
    if dest.suffix.lower() != ".zip":
        dest.mkdir(parents=True, exist_ok=True)
        dest = dest / f"FramePort-keys-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    root = user_data_dir()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in keys + ssh:
            if f.is_file():
                z.write(f, f.relative_to(root).as_posix())
        z.writestr("README.txt", "FramePort signing keys (password 'password', alias 'key') and the app's SSH key.\n"
                                 "Restore: `frameport tools import-keys <folder with the .keystore files>`.\n")
    return dest


def default_backup_dir() -> Path:
    from .core import winhost

    if winhost.is_wsl():
        home = winhost.env_path("USERPROFILE")
        if home:
            return home / "Documents"
    docs = Path.home() / "Documents"
    return docs if docs.is_dir() else Path.home()


def grid_files(grid: Path, appid) -> list[Path]:
    """A Steam shortcut's grid artwork by exact name (<appid>p.jpg, <appid>_hero.png, …), not every file that merely
    starts with the same digits (another game's appid can)."""
    import re

    pat = re.compile(rf"^{re.escape(str(appid))}(p|_hero|_logo|_icon)?\.[A-Za-z0-9]+$")
    return [p for p in grid.iterdir() if pat.match(p.name)] if appid and grid.is_dir() else []


def remove_pc_shortcuts(reporter: Reporter) -> None:
    """All PC shortcuts (PC VR and Quest games) FramePort added to Windows Steam, with one Steam restart. AXRB and
    the games inside its Android stay: AXRB is the user's program (uninstall it from Windows' app list)."""
    from .core import winhost
    from .targets.pc_revive import _vdf, local_installs, options_key, shortcut_fields

    deps = local_installs()
    if not deps or not winhost.available():
        return
    root = winhost.steam_root()
    user = winhost.steam_user(root) if root else None
    if not user:
        reporter.check("Steam shortcuts on this PC", None, "Steam user not found; remove them in Steam")
        return
    vdf_mod = _vdf()
    cfg = root / "userdata" / user / "config"
    was_running = winhost.stop_steam(root)
    try:
        for pkg, dep in deps.items():
            exe = shortcut_fields(dep)[0]
            removed = vdf_mod.remove_shortcut(str(cfg / "shortcuts.vdf"), exe, options_key(dep))
            for art in grid_files(cfg / "grid", dep.get("appid")):
                art.unlink(missing_ok=True)
            reporter.check(f"Steam shortcut: {dep.get('title') or pkg}", True if removed else None,
                           "removed" if removed else "wasn't there")
    finally:
        if was_running:
            winhost.start_steam(root)


PURGE_STATUS = "~/.cache/frameport-purge.json"  # written by the agent's detached purge worker (outside its folder)


def purge_frame(frame, reporter: Reporter, keep_saves: bool = True, wait: float = 180) -> dict:
    """Remove FramePort's games and files from the Frame and wait for the detached worker to finish. The status file
    is read directly (the agent deletes itself at the end, so asking it would re-upload it)."""
    import json

    reporter.stage("Removing FramePort from the Frame")
    frame.agent("purge", keep_saves=keep_saves)
    end = time.time() + wait
    st: dict = {}
    while time.time() < end:
        time.sleep(3)
        try:  # SSH may hiccup while Steam restarts on the Frame: keep trying until the deadline
            code, out, _ = frame.run(f"cat {PURGE_STATUS}", timeout=30)
            st = json.loads(out) if code == 0 and out.strip() else st
        except Exception:  # noqa: BLE001
            continue
        if st.get("state") in ("done", "failed"):
            break
    if st.get("state") not in ("done", "failed"):
        reporter.check("Frame", False, "couldn't confirm that the removal finished; check the Frame's Steam library")
    for r in st.get("removed", []):
        reporter.check(r, True)
    for e in st.get("errors", []):
        reporter.check("Frame", False, e)
    if st.get("kept"):
        reporter.log(f"kept saves on the Frame in: {', '.join(st['kept'])}")
    return st


def run(reporter: Reporter, frame=None, keep_frame_saves: bool = True, backup_dir: Path | None = None,
        remove_frame: bool = False) -> dict:
    """Uninstall. Returns {backup, removed}. The data folder goes last."""
    out: dict = {"backup": None}
    if backup_dir is not None:
        reporter.stage("Backing up signing keys")
        z = backup_keys(backup_dir)
        out["backup"] = str(z) if z else None
        reporter.check("Signing keys backup", True, str(z) if z else "no keys to back up")
    if remove_frame and frame is not None:
        purge_frame(frame, reporter, keep_frame_saves)
        try:
            frame.close()
        except Exception:  # noqa: BLE001
            pass
    reporter.stage("Removing Steam shortcuts on this PC")
    try:
        remove_pc_shortcuts(reporter)
    except Exception as exc:  # noqa: BLE001
        reporter.check("Steam shortcuts on this PC", False, str(exc))
    rv = wsl_revive_copy()
    if rv:
        shutil.rmtree(rv, ignore_errors=True)
        reporter.check("Revive copy for Windows", True, str(rv))
    reporter.stage("Removing FramePort's data")
    data = user_data_dir()
    applog.shutdown()     # the open log file would block the delete on Windows
    paths.mark_removed()  # from now on nothing writes into the data folder (job logs, library, caches)
    left = remove_data_dir(data)
    reporter.check("FramePort data", not left, str(data) if not left else
                   f"{data}: not removed: {', '.join(left[:5])}" + (" …" if len(left) > 5 else ""))
    return out


# what FramePort creates in its data folder (a custom FRAMEPORT_HOME may hold other files: those stay)
DATA_ENTRIES = {"artwork", "cache", "catalog", "frames.json", "library.json", "logs", "output", "overport-workspace",
                "pc", "ssh", "tools", "work", "updates", "xrlayer"}


def remove_data_dir(data: Path) -> list[str]:
    """Delete FramePort's data folder. The default folder is FramePort's own and goes entirely; with FRAMEPORT_HOME
    only FramePort's entries are removed. Returns what is left (couldn't be deleted, or isn't FramePort's)."""
    if not data.is_dir():
        return []
    own_folder = not os.environ.get("FRAMEPORT_HOME")
    for entry in list(data.iterdir()):
        ours = own_folder or entry.name in DATA_ENTRIES or entry.name.startswith(("library.json.", "."))
        if not ours:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    left = sorted(e.name for e in data.iterdir()) if data.is_dir() else []
    if not left:
        data.rmdir()
    return left
