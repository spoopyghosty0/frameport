"""Find Quest games on disk.

Accepted layouts:
  <folder>/<something>.apk [+ <folder>/<package>/ (OBB or raw asset data)]   e.g. common downloader layouts
  <folder>/<package>.apk + <folder>/obb/                                       FramePort/PATCHED output layout
  <folder>/x.apk + <folder>/obb/<package>/ or Android/obb/<package>/          backups with an obb folder
  <game>/apk/x.apk + <game>/obb/<package>/                                     SideQuest-style backups (one game)
  <folder>/x.apk + (main|patch).<versionCode>.<package>.obb anywhere nearby    found by file name (next to the APK,
                                                                               <package>/apk/ + <package>/obb/, ...)
  a single .apk file
(find_data has the details.)
"""
from __future__ import annotations

import contextvars
import os
import re
import zipfile
from pathlib import Path

from ..core.models import SourceGame

# a release tag at the end of a download folder name: " -TAG" or " -TAG v76" (no space after the dash)
TAG = re.compile(r"\s+-(?=[A-Za-z])[A-Za-z0-9]{2,12}(?:\s+[A-Za-z]?\d{1,4})?\s*$")


def display_name(folder_name: str) -> str:
    """'PowerWash Simulator VR v3055+2.5.0 -TAG v76' -> 'PowerWash Simulator VR v3055+2.5.0'."""
    return re.sub(r'[<>:"/\\|?*]', "_", TAG.sub("", folder_name).strip())


def _apk_ids(apk: Path) -> tuple[str | None, int | None]:
    """(package, versionCode) from the APK's manifest; None for what can't be read."""
    try:
        from pyaxmlparser import APK

        a = APK(str(apk))
    except Exception:
        return None, None
    try:
        vc = int(a.version_code)
    except (TypeError, ValueError):
        vc = None
    return a.package, vc


def _package_of(apk: Path) -> str | None:
    return _apk_ids(apk)[0]


def _axrb_patched(path: Path) -> bool:
    """AXRB's own PC builds (OVRPort-converted for its Windows runtime, without the game's OBBs): <name>-axrb.apk,
    AXRB/patched/<package>/ or an axrb-patched folder. The original download next to them is the game."""
    parts = [p.lower() for p in path.parts]
    return path.stem.lower().endswith("-axrb") or "axrb-patched" in parts or any(
        a == "axrb" and b == "patched" for a, b in zip(parts, parts[1:], strict=False))


def _axrb_download(folder: Path, apk: Path) -> bool:
    """AXRB's launcher download: <app id>/<binary id>/base.apk (both folder names numbers)."""
    return apk.name.lower() == "base.apk" and folder.name.isdigit() and folder.parent.name.isdigit()


def _is_apk(path: Path) -> bool:
    if _axrb_patched(path):
        return False
    try:
        with zipfile.ZipFile(path) as z:
            return "AndroidManifest.xml" in z.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


OBB_FOLDERS = ("obb", "obbs")


# folder listings, kept while scan() runs: every game of a collection searches its neighbours (slow on NTFS drives)
_LISTINGS: contextvars.ContextVar[dict | None] = contextvars.ContextVar("quest_dump_listings", default=None)


def _list(d: Path) -> list[tuple[Path, bool]]:
    """(entry, is a folder) in d, sorted; [] when it can't be read."""
    cache = _LISTINGS.get()
    if cache is not None and d in cache:
        return cache[d]
    try:
        with os.scandir(d) as it:
            out = sorted((Path(e.path), e.is_dir()) for e in it)
    except OSError:
        out = []
    if cache is not None:
        cache[d] = out
    return out


def _subdirs(d: Path) -> list[Path]:
    return [p for p, is_dir in _list(d) if is_dir and not p.name.startswith(("_", "."))]


def _has_apks(d: Path) -> bool:
    return any(p.suffix.lower() == ".apk" and not is_dir for p, is_dir in _list(d))


def _child(d: Path, name: str) -> Path | None:
    """d/name, matching the name's case loosely (backups come from Windows: 'OBB', a lower-case package folder)."""
    exact = d / name
    if exact.is_dir():
        return exact
    return next((p for p in _subdirs(d) if p.name.lower() == name.lower()), None)


def _nonempty(d: Path | None) -> bool:
    try:
        return bool(d) and d.is_dir() and any(d.iterdir())
    except OSError:
        return False


def _has_obb(d: Path) -> bool:
    return any(p.suffix.lower() == ".obb" and not is_dir for p, is_dir in _list(d))


def _package_dir_below(d: Path, pkg: str, depth: int, skip: Path | None = None, other_games: bool = False) \
        -> Path | None:
    """A folder named like the package holding .obb files, at most `depth` levels below d. other_games: skip
    folders with APKs of their own (a neighbouring game folder, e.g. another version of the same game)."""
    for sub in _subdirs(d):
        if skip is not None and sub == skip:
            continue
        if sub.name.lower() == pkg.lower():
            if _has_obb(sub):
                return sub
            continue
        if depth > 1 and not (other_games and _has_apks(sub)):
            found = _package_dir_below(sub, pkg, depth - 1)
            if found:
                return found
    return None


def _find_data_dir(apk_dir: Path, pkg: str | None) -> Path | None:
    """The folder whose contents go to Android/obb/<package>/ (so the folder holding the .obb files themselves):
      <apk dir>/<package>/                       downloader layouts
      <apk dir>/obb/<package>/ or <apk dir>/obb/  FramePort/PATCHED output, backups with an obb folder
      a <package> folder with .obb files up to 3 levels below the APK's folder (e.g. Android/obb/<package>/) or 2
      below its parent (SideQuest-style backups: <game>/apk/x.apk + <game>/obb/<package>/; the parent's folders
      with APKs of their own are other games and are skipped)."""
    own = _child(apk_dir, pkg) if pkg else None
    if _nonempty(own):
        return own
    for obb in (_child(apk_dir, name) for name in OBB_FOLDERS):
        if obb is None:
            continue
        inner = _child(obb, pkg) if pkg else None
        if _nonempty(inner):
            return inner
        if _nonempty(obb):
            return obb
    if not pkg:
        return None
    found = _package_dir_below(apk_dir, pkg, 3)
    if found is None and apk_dir.parent != apk_dir:
        found = _package_dir_below(apk_dir.parent, pkg, 2, skip=apk_dir, other_games=True)
    return found


# Android's expansion file names: main.<versionCode>.<package>.obb, patch.<versionCode>.<package>.obb
OBB_NAME = re.compile(r"^(main|patch)\.(\d+)\.(.+)\.obb$", re.IGNORECASE)


def _named_obbs(d: Path, pkg: str) -> list[tuple[str, int, Path]]:
    """(kind, versionCode, file) of the package's expansion files directly in d."""
    out = []
    for f, is_dir in _list(d):
        m = OBB_NAME.match(f.name)
        if m and m.group(3).lower() == pkg.lower() and not is_dir:
            out.append((m.group(1).lower(), int(m.group(2)), f))
    return out


def _obb_folders(apk_dir: Path, pkg: str, depth: int = 3) -> list[tuple[Path, list[tuple[str, int, Path]]]]:
    """Folders holding the package's expansion files by name, nearest first: the APK's own folder, the folders below
    it, its parent, then the parent's other folders (each up to `depth` levels). Folders with APKs of their own are
    other games (e.g. another version of this one) and aren't searched."""
    found: list[tuple[Path, list]] = []

    def visit(d: Path) -> None:
        obbs = _named_obbs(d, pkg)
        if obbs:
            found.append((d, obbs))

    def below(d: Path, level: int, skip: Path | None = None) -> None:
        for sub in _subdirs(d):
            # a folder with APKs is another game, except an obb folder holding this package's files (SideQuest's VR4
            # backup has Unreal's VR4-Android-Shipping-arm64.apk next to its OBBs)
            if sub == skip or (_has_apks(sub) and not (sub.name.lower() in OBB_FOLDERS and _named_obbs(sub, pkg))):
                continue
            visit(sub)
            if level > 1:
                below(sub, level - 1)

    visit(apk_dir)
    below(apk_dir, depth)
    parent = apk_dir.parent
    if parent != apk_dir:
        visit(parent)
        below(parent, depth, skip=apk_dir)
    return found


def _pick_obbs(obbs: list[tuple[str, int, Path]], version_code: int | None) -> list[Path]:
    """The expansion files for this APK: per kind (main, patch) the newest one not newer than the APK's versionCode
    (expansion files keep the versionCode they were introduced with, so that's its own one when there is one); only
    when there is none at all, the newest ones (another version of the game's files: better than nothing)."""
    usable = [o for o in obbs if version_code is not None and o[1] <= version_code] or obbs
    picked = []
    for kind in ("main", "patch"):
        same = sorted((vc, f) for k, vc, f in usable if k == kind)
        if same:
            picked.append(same[-1][1])
    return picked


def find_data(apk_dir: Path, pkg: str | None, version_code: int | None = None) -> tuple[Path | None, list[str] | None]:
    """(data folder, files) for the APK in apk_dir. files is None when the whole folder is the game's data (the
    layouts of _find_data_dir), else the names of the expansion files to send from it (found by file name, e.g.
    lying next to the APK: the APK and anything else in that folder stay on the PC)."""
    data = _find_data_dir(apk_dir, pkg)
    if data is not None or not pkg:
        return data, None
    folders = _obb_folders(apk_dir, pkg)
    if not folders:
        return None, None
    d, obbs = next((f for f in folders if any(vc == version_code for _, vc, _ in f[1])), folders[0])
    files = _pick_obbs(obbs, version_code)
    everything = {p.name for p, _ in _list(d)}
    names = [f.name for f in files]
    return d, (None if everything == set(names) else names)


def find_data_dir(apk_dir: Path, pkg: str | None, version_code: int | None = None) -> Path | None:
    return find_data(apk_dir, pkg, version_code)[0]


# a folder that only holds the APK inside a game folder (<game>/apk/x.apk + <game>/obb/…): the game is the parent
APK_FOLDERS = ("apk", "apks")
# SideQuest's backup folder names: 2026-10-08T12-08-41-153Z (also with ':' / '.' separators)
BACKUP_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}[-:.]\d{2}[-:.]\d{2}([-:.]\d+)?Z?$")


def from_path(path: Path) -> SourceGame | None:
    path = Path(path)
    if path.is_file() and path.suffix.lower() == ".apk":
        pkg, vc = _apk_ids(path)
        data, files = find_data(path.parent, pkg, vc)
        return SourceGame(display_name(path.stem), path, data, path.parent, data_files=files)
    if not path.is_dir():
        return None
    apks = sorted(p for p in path.glob("*.apk") if _is_apk(p))
    if not apks:
        return None
    primary = [a for a in apks if ".alt-" not in a.name]
    apk = primary[0] if primary else apks[0]
    pkg, vc = _apk_ids(apk)
    data, files = find_data(path, pkg, vc)
    game_dir = path.parent if path.name.lower() in APK_FOLDERS else path
    # SideQuest backups: <package>/<backup time>/apk/x.apk; the game is named after the package folder, not the time.
    # AXRB downloads: <app id>/<binary id>/base.apk: numbers, so the package names it (the store title comes later)
    name = game_dir.parent.name if BACKUP_TIME.match(game_dir.name) else game_dir.name
    if name.isdigit() and pkg:
        name = pkg
    if _axrb_download(path, apk) and data == path and files is not None:
        # AXRB puts the game's other store files (DLC, extra assets) next to its OBBs: they belong in Android/obb too
        files = sorted(f.name for f, is_dir in _list(path) if not is_dir and f.suffix.lower() != ".apk")
    return SourceGame(display_name(name), apk, data, game_dir, [a for a in apks if a != apk], files)


def _no_quest_games_below(d: Path) -> bool:
    """Folders not worth searching for APKs: a PC program (exe/dll files), a git checkout, a Python environment."""
    try:
        names = [p.name.lower() for p in d.iterdir()]
    except OSError:
        return True
    return any(n.endswith((".exe", ".dll")) for n in names) or ".git" in names or "pyvenv.cfg" in names


def _other_folders(d: Path, game: SourceGame) -> list[Path]:
    """Subfolders of a folder with an APK that aren't that game's own data (OBB) folder."""
    try:
        subs = [p for p in d.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))]
    except OSError:
        return []
    own = {game.data_dir.resolve()} if game.data_dir else set()
    own |= set(game.data_dir.resolve().parents) if game.data_dir else set()  # e.g. Android/ of Android/obb/<pkg>
    return [p for p in subs if p.resolve() not in own and p.name.lower() not in (*OBB_FOLDERS, "android")]


def scan(root: Path, depth: int = 5) -> list[SourceGame]:
    """Find games under root, up to `depth` folder levels down (e.g. a download manager's
    "<library>/data/downloads/<game>/game.apk"). A folder with an APK and nothing but that game's data folder is one
    game; a folder that also has other folders is a collection: its loose APKs are games and its folders are searched
    (a "VR" folder with a stray APK next to the game folders used to count as one game). PC program folders and code
    checkouts aren't searched."""
    found: list[SourceGame] = []

    def walk(d: Path, level: int) -> None:
        game = from_path(d)
        if game and (level >= depth or not _other_folders(d, game)):
            found.append(game)
            return
        if game:  # a collection with loose APKs: each is a game of its own
            found.extend(g for g in (from_path(p) for p in sorted(d.glob("*.apk")) if _is_apk(p)) if g)
        if level >= depth:
            return
        try:
            children = sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
        except OSError:
            return
        for child in children:
            if game and game.data_dir and child.resolve() == game.data_dir.resolve():
                continue
            if from_path(child) or not _no_quest_games_below(child):
                walk(child, level + 1)

    root = Path(root)
    if root.is_file():
        g = from_path(root)
        return [g] if g else []
    token = _LISTINGS.set({})
    try:
        walk(root, 0)
    finally:
        _LISTINGS.reset(token)
    # an APK inside another game's data folder is part of that game's data, not a game (SideQuest's VR4 backup keeps
    # Unreal's VR4-Android-Shipping-arm64.apk next to its OBBs)
    data = [(g, g.data_dir.resolve()) for g in found if g.data_dir and g.data_dir.resolve() != g.apk.resolve().parent]
    return [g for g in found if not any(h is not g and d in g.apk.resolve().parents for h, d in data)]
