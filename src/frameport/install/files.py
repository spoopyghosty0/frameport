"""Send files (videos, documents, mods, saves, ...) to Lepton apps on the Frame.

Lepton gives every app the Frame's ~/Videos, ~/Downloads and ~/Documents as /sdcard/Movies, /sdcard/Download and
/sdcard/Documents (shared by all apps), and each app its own /sdcard (<install>/lepton-data/external). The agent reports
these destinations (`storage_targets`, read from Lepton itself). Android's media index doesn't work inside Lepton, so
media apps find these files by browsing folders, not in "all videos" lists. Some players only list their own folder
in /sdcard (4XVR: "Internal Storage" = /sdcard/4XPlayer): `link_app` hard-links what was sent to the shared folders into
that app's own folder too (agent `link_media`; no copy, no extra space). Uploads are resumable and use the fast link
(USB / the Frame's hotspot) like game installs.
"""
from __future__ import annotations

import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path

from ..core.events import Reporter
from ..frame.connection import Frame, sh_quote
from . import installer

TARGETS_HELP = ("videos (/sdcard/Movies in every app), downloads (/sdcard/Download), documents (/sdcard/Documents), "
                "app (the game's own /sdcard; needs a game), app-files (/sdcard/Android/data/<package>/files)")


def storage_targets(frame: Frame, package: str | None = None) -> list[dict]:
    args = {"package": package} if package else {}
    return frame.agent("storage_targets", **args)["targets"]


def _items(paths: list[Path], subdir: str) -> list[tuple[Path, str, int]]:
    """(local file, relative remote path, size); folders keep their structure under their own name."""
    items = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            for f in sorted(x for x in p.rglob("*") if x.is_file()):
                items.append((f, posixpath.join(subdir, p.name, f.relative_to(p).as_posix()), f.stat().st_size))
        elif p.is_file():
            items.append((p, posixpath.join(subdir, p.name), p.stat().st_size))
        else:
            raise FileNotFoundError(f"not found: {p}")
    return items


def _empty_dirs(paths: list[Path], subdir: str) -> list[str]:
    """Relative remote paths of the uploaded folders with no file anywhere below them (GitHub #532: a game may need
    an empty folder, and uploading files alone never creates it)."""
    out = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            below = [x.relative_to(p) for x in p.rglob("*")]
            full = {a for x in below if (p / x).is_file() for a in x.parents}  # folders with a file below them
            for d in [Path("."), *sorted(x for x in below if (p / x).is_dir())]:
                if d not in full:
                    out.append(posixpath.normpath(posixpath.join(subdir, p.name, d.as_posix())))
    return out


def _remote_size(frame: Frame, path: str) -> int | None:
    try:
        return frame.sftp.stat(path).st_size
    except OSError:
        return None


def _safe_subdir(subdir: str) -> str:
    parts = [x for x in (subdir or "").replace("\\", "/").split("/") if x and x != "."]
    if any(x == ".." for x in parts):
        raise ValueError("the folder must stay inside the destination (no '..')")
    return "/".join(parts)


def upload(frame: Frame, paths: list[Path], remote_dir: str, reporter: Reporter) -> tuple[list, list, int]:
    """Upload files/folders into remote_dir (folders keep their structure). Files already there with the same size are
    skipped. Returns (sent items, skipped items, bytes sent); items are (local, path relative to remote_dir, size)."""
    items = _items(paths, "")
    have = [i for i in items if _remote_size(frame, posixpath.join(remote_dir, i[1])) == i[2]]
    if have:
        reporter.log(f"{len(have)} file(s) already on the Frame; not re-sending")
        items = [i for i in items if i not in have]
    total = sum(i[2] for i in items)
    reporter.log(f"{len(items)} file(s), {total / 2**20:.0f} MiB → {remote_dir}")
    if items:
        with installer.transfer_link(frame, reporter, total) as xfer:
            reporter.stage(f"Upload {len(items)} file(s)")
            installer.upload_files(xfer, items, remote_dir, reporter, max(total, 1))
    empty = [inside(remote_dir, posixpath.join(remote_dir, rel)) for rel in _empty_dirs(paths, "")]
    if empty:
        frame.mkdirs(empty)
        reporter.log(f"{len(empty)} empty folder(s) created")
    return items, have, total


def send_files(frame: Frame, paths: list[Path], target: str = "videos", package: str | None = None,
               subdir: str = "", reporter: Reporter | None = None, link_app: str | None = None) -> dict:
    """Upload to `target`. With link_app (a shared target only): the files also appear in that app's own folder."""
    reporter = reporter or Reporter()
    reporter.stage("Prepare the Frame")
    dests = {t["id"]: t for t in storage_targets(frame, package if target.startswith("app") else None)}
    if target not in dests:
        raise ValueError(f"unknown destination {target!r}; choose one of: {', '.join(dests) or TARGETS_HELP}")
    dest = dests[target]
    sub = _safe_subdir(subdir)
    reporter.log(f"apps see {dest['path']} as {dest['android']}")
    items, have, total = upload(frame, paths, posixpath.join(dest["path"], sub) if sub else dest["path"], reporter)
    if sub:
        items = [(f, posixpath.join(sub, rel), n) for f, rel, n in items]
        have = [(f, posixpath.join(sub, rel), n) for f, rel, n in have]
    where = posixpath.join(dest["android"], sub) if sub else dest["android"]
    linked = None
    if link_app and dest["shared"]:
        remote = [posixpath.join(dest["path"], i[1]) for i in items + have]
        linked = frame.agent("link_media", package=link_app, files=remote)
        if linked.get("folder"):
            reporter.log(f"added {len(linked['linked']) + len(linked['existing'])} file(s) to the game's own folder "
                         f"{linked['android']}")
            where = linked["android"]
        else:
            reporter.log("the game has no folder of its own yet (start it once); it finds the files in " + where)
    reporter.log(f"done: in the app, open {where}")
    return {"files": len(items), "skipped": len(have), "bytes": total, "path": posixpath.join(dest["path"], sub),
            "android": where,
            "shared": dest["shared"], "linked": linked}


# ------------------------------------------------------------------ browsing (the GUI's Files tab)
@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float
    link: bool = False  # a symbolic link (e.g. Lepton's /sdcard/Movies → ~/Videos in a game's storage)


def inside(root: str, path: str) -> str:
    """`path` normalised; refuses anything outside `root` (every Files-tab operation stays in the chosen location)."""
    root = posixpath.normpath(root)
    path = posixpath.normpath(path if path.startswith("/") else posixpath.join(root, path))
    if path != root and not path.startswith(root.rstrip("/") + "/"):
        raise ValueError(f"{path} is outside {root}")
    return path


def _name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise ValueError(f"not a valid name: {name!r}")
    return name


def list_dir(frame: Frame, root: str, path: str, hidden: bool = False) -> list[Entry]:
    """Folders first, then files, by name (case-insensitive). Links to folders count as folders."""
    path = inside(root, path)
    out = []
    for a in frame.sftp.listdir_attr(path):
        if not hidden and a.filename.startswith("."):
            continue
        full = posixpath.join(path, a.filename)
        link = stat.S_ISLNK(a.st_mode or 0)
        mode, size = a.st_mode or 0, a.st_size or 0
        if link:
            try:
                target = frame.sftp.stat(full)
                mode, size = target.st_mode or 0, target.st_size or 0
            except OSError:  # dangling link
                pass
        out.append(Entry(a.filename, full, stat.S_ISDIR(mode), size, float(a.st_mtime or 0), link))
    return sorted(out, key=lambda e: (not e.is_dir, e.name.lower()))


def make_dir(frame: Frame, root: str, parent: str, name: str) -> str:
    path = inside(root, posixpath.join(inside(root, parent), _name(name)))
    frame.sftp.mkdir(path)
    return path


def rename(frame: Frame, root: str, path: str, new_name: str) -> str:
    path = inside(root, path)
    if path == posixpath.normpath(root):
        raise ValueError("the location itself can't be renamed")
    new = inside(root, posixpath.join(posixpath.dirname(path), _name(new_name)))
    frame.sftp.rename(path, new)
    return new


def delete(frame: Frame, root: str, paths: list[str]) -> int:
    """Delete files/folders (recursively) inside root. A link is removed, never what it points to."""
    targets = [inside(root, p) for p in paths]
    if any(t == posixpath.normpath(root) for t in targets):
        raise ValueError("the location itself can't be deleted")
    if targets:
        code, _, err = frame.run("rm -rf -- " + " ".join(sh_quote(t) for t in targets), timeout=900)
        if code:
            raise OSError(err.strip() or f"rm failed ({code})")
    return len(targets)


def _remote_files(frame: Frame, path: str, rel: str = "") -> list[tuple[str, str, int]]:
    """(remote file, path relative to the downloaded item, size), recursively (links are not followed)."""
    a = frame.sftp.lstat(path)
    if not stat.S_ISDIR(a.st_mode or 0):
        return [(path, rel or posixpath.basename(path), a.st_size or 0)]
    out = []
    for child in frame.sftp.listdir_attr(path):
        if stat.S_ISLNK(child.st_mode or 0):
            continue
        out += _remote_files(frame, posixpath.join(path, child.filename),
                             posixpath.join(rel or posixpath.basename(path), child.filename))
    return out


def download(frame: Frame, root: str, paths: list[str], local_dir: Path, reporter: Reporter | None = None) -> dict:
    """Copy files/folders from the Frame into local_dir (folders keep their structure). Interruptible (Cancel)."""
    reporter = reporter or Reporter()
    files = [f for p in paths for f in _remote_files(frame, inside(root, p))]
    total = sum(f[2] for f in files)
    reporter.log(f"{len(files)} file(s), {total / 2**20:.0f} MiB → {local_dir}")
    done = 0
    with installer.transfer_link(frame, reporter, total) as xfer:
        reporter.stage(f"Download {len(files)} file(s)")
        for remote, rel, size in files:
            dest = Path(local_dir) / Path(*rel.split("/"))
            dest.parent.mkdir(parents=True, exist_ok=True)

            def progress(sent, _total, base=done, rel=rel):
                reporter.check_cancel()
                reporter.progress((base + sent) / max(total, 1), rel)
            xfer.sftp.get(remote, str(dest), callback=progress)
            done += size
    return {"files": len(files), "bytes": total, "folder": str(local_dir)}
