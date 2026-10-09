"""Steam screenshots taken on the Frame (the GUI's Screenshots tab).

Steam keeps them per account in userdata/<id>/760/remote/<appid>/screenshots/ (+ thumbnails/). The Frame files every
headset screenshot under SteamVR (250820), so the agent (`list_screenshots`) matches them to FramePort games by time:
launch.sh logs each play session to <anchor>/plays.log. Thumbnails and opened images are cached on this PC in
<user data>/screenshots-cache/<frame>/ and shown by asset URL (never image bytes in controls).
"""
from __future__ import annotations

import hashlib
import posixpath
import re
import threading
from pathlib import Path

from ..core.events import Reporter
from ..core.paths import user_data_dir
from . import installer

THUMB_WIDTH = 480
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def list_shots(frame, package: str | None = None, offset: int = 0, limit: int = 0) -> dict:
    """{shots, total, games} from the agent, newest first. package: a game ("" = shots of no FramePort game)."""
    args = {"offset": offset, "limit": limit}
    if package is not None:
        args["package"] = package
    return frame.agent("list_screenshots", **args)


def frame_id(frame) -> str:
    target = getattr(frame, "target", None)
    raw = (getattr(target, "name", "") or getattr(target, "host", "") or "frame") if target else "frame"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._") or "frame"


def cache_dir(frame) -> Path:
    d = user_data_dir() / "screenshots-cache" / frame_id(frame)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(shot: dict) -> str:
    return hashlib.sha1(f"{shot['path']}:{shot.get('size')}".encode()).hexdigest()[:16]


def _lock(key: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def _fetch(frame, remote: str, dest: Path) -> Path:
    with _lock(str(dest)):
        if not dest.exists():
            tmp = dest.with_suffix(dest.suffix + ".part")
            frame.sftp.get(remote, str(tmp))
            tmp.replace(dest)
    return dest


def cached(frame, shot: dict, kind: str = "thumb") -> Path | None:
    """The cached thumbnail/full image if it was already downloaded (no network: for render paths)."""
    p = cache_dir(frame) / _cache_name(shot, kind)
    return p if p.exists() else None


def _cache_name(shot: dict, kind: str) -> str:
    ext = posixpath.splitext(shot["path"])[1].lower() if kind == "full" else ".jpg"
    return f"{kind}_{_key(shot)}{ext or '.jpg'}"


def image_path(frame, shot: dict) -> Path:
    """The full image, downloaded once into the cache (the viewer shows it from there)."""
    return _fetch(frame, shot["path"], cache_dir(frame) / _cache_name(shot, "full"))


def thumb_path(frame, shot: dict) -> Path:
    """A small image for the grid: Steam's own thumbnail, downloaded once; without one, made from the full image."""
    dest = cache_dir(frame) / _cache_name(shot, "thumb")
    if dest.exists():
        return dest
    if shot.get("thumb"):
        try:
            return _fetch(frame, shot["thumb"], dest)
        except OSError:
            pass  # thumbnail gone: make one
    full = image_path(frame, shot)
    with _lock(str(dest)):
        if not dest.exists():
            try:
                from PIL import Image

                with Image.open(full) as im:
                    im.thumbnail((THUMB_WIDTH, THUMB_WIDTH))
                    tmp = dest.with_suffix(".jpg.part")
                    im.convert("RGB").save(tmp, "JPEG", quality=85)
                    tmp.replace(dest)
            except Exception:  # noqa: BLE001  (unreadable image: show the original)
                return full
    return dest


def safe_name(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text or "").strip(" .") or "Screenshots"


def download(frame, shots: list[dict], dest_dir: Path, reporter: Reporter | None = None) -> dict:
    """Copy the full images into dest_dir/<game title>/ (files already there with the same size are skipped).
    Interruptible (Cancel)."""
    reporter = reporter or Reporter()
    dest_dir = Path(dest_dir)
    items = []
    for s in shots:
        dest = dest_dir / safe_name(s.get("title") or "") / posixpath.basename(s["path"])
        if dest.exists() and dest.stat().st_size == s.get("size"):
            continue
        items.append((s, dest))
    total = sum(s.get("size") or 0 for s, _ in items)
    skipped = len(shots) - len(items)
    reporter.log(f"{len(items)} screenshot(s), {total / 2**20:.1f} MiB → {dest_dir}"
                 + (f" ({skipped} already there)" if skipped else ""))
    done = 0
    if items:
        with installer.transfer_link(frame, reporter, total) as xfer:
            reporter.stage(f"Download {len(items)} screenshot(s)")
            for s, dest in items:
                reporter.check_cancel()
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".part")

                def progress(got, _total, base=done, name=dest.name):
                    reporter.check_cancel()
                    reporter.progress((base + got) / max(total, 1), name)
                xfer.sftp.get(s["path"], str(tmp), callback=progress)
                tmp.replace(dest)
                done += s.get("size") or 0
    return {"files": len(items), "skipped": skipped, "bytes": total, "folder": str(dest_dir)}


def delete(frame, shots: list[dict]) -> int:
    """Delete screenshots on the Frame (the agent only deletes images in Steam's screenshot folders) and their cached
    copies here. Steam's own screenshot list may still show them until Steam restarts."""
    if not shots:
        return 0
    r = frame.agent("delete_screenshots", paths=[s["path"] for s in shots])
    for s in shots:
        for kind in ("thumb", "full"):
            (cache_dir(frame) / _cache_name(s, kind)).unlink(missing_ok=True)
    return len(r.get("deleted") or [])


def take(frame, wait: float = 8) -> dict:
    """Take a headset screenshot on the Frame (agent v66 `take_screenshot`: SteamVR's own screenshot, saved by Steam
    under SteamVR like one taken in the headset): {taken, path, hmd}. Not taken while the headset is in standby."""
    return frame.agent("take_screenshot", wait=wait)
