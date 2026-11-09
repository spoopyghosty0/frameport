"""Find Quest games on disk.

Accepted layouts:
  <folder>/<something>.apk [+ <folder>/<package>/ (OBB or raw asset data)]   e.g. common downloader layouts
  <folder>/<package>.apk + <folder>/obb/                                       FramePort/PATCHED output layout
  <folder>/x.apk + <folder>/obb/<package>/ or Android/obb/<package>/          backups with an obb folder
  <game>/apk/x.apk + <game>/obb/<package>/                                     SideQuest-style backups (one game)
  <game>/apk/x.apk + <game>/obb/<files>                                        SideQuest backups with loose OBB + PAKs
  <folder>/x.apk + (main|patch).<versionCode>.<package>.obb anywhere nearby    found by file name (next to the APK,
                                                                               <package>/apk/ + <package>/obb/, ...)
  <folder>/x.apk + loose asset files (*.bundle, *.pak, …)                      the folder's only APK: they're its data
  a single .apk file
  app bundles: .xapk / .apks / .apkm / .apkx files and folders of split APKs (base.apk + split_config.*.apk), unpacked
  into FramePort's data folder first (sources/bundles.py)
(find_data has the details.)
"""
from __future__ import annotations

import contextvars
import os
import re
import zipfile
from dataclasses import dataclass
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


def version_name(apk: Path) -> str | None:
    """The APK's versionName (None if it can't be read)."""
    try:
        from pyaxmlparser import APK

        return APK(str(apk)).version_name or None
    except Exception:
        return None


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
# Library recovery must distinguish unreadable folders from a conclusive data selection.
_STRICT_READS: contextvars.ContextVar[bool] = contextvars.ContextVar("quest_dump_strict_reads", default=False)


def _list(d: Path) -> list[tuple[Path, bool]]:
    """(entry, is a folder) in d, sorted; [] when unreadable, unless strict discovery is active."""
    cache = None if _STRICT_READS.get() else _LISTINGS.get()
    if cache is not None and d in cache:
        return cache[d]
    try:
        with os.scandir(d) as it:
            out = sorted((Path(e.path), e.is_dir()) for e in it)
    except OSError:
        if _STRICT_READS.get():
            raise
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
        if _STRICT_READS.get():
            raise
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
      with APKs of their own are other games and are skipped).
    An apk/apks folder belongs to its parent game folder: its sibling obb/ can hold raw assets (e.g. FMOD banks),
    with or without a package subfolder or .obb files. Loose sibling OBBs are selected by version in find_data
    before that folder's extra assets are retained."""
    roots = [apk_dir]
    if apk_dir.name.lower() in APK_FOLDERS:
        roots.append(apk_dir.parent)
    for root in roots:
        own = _child(root, pkg) if pkg else None
        if _nonempty(own):
            return own
        for obb in (_child(root, name) for name in OBB_FOLDERS):
            if obb is None:
                continue
            inner = _child(obb, pkg) if pkg else None
            if _nonempty(inner):
                return inner
            if root != apk_dir:
                entries = _list(obb)
                # Named expansion files need the package/version selection in find_data, even in a sibling obb/.
                # A wrapper containing only timestamp folders also needs that search, rather than sending itself.
                if any(not is_dir and OBB_NAME.match(p.name) for p, is_dir in entries):
                    continue
                if not any(not is_dir for _, is_dir in entries):
                    continue
            if _nonempty(obb):
                return obb
    if not pkg:
        return None
    found = _package_dir_below(apk_dir, pkg, 3)
    if found is None and apk_dir.parent != apk_dir:
        found = _package_dir_below(apk_dir.parent, pkg, 2, skip=apk_dir, other_games=True)
    return found


# Android's expansion file names, also CryEngine's main.<package>.obb without a versionCode (GitHub #238).
OBB_NAME = re.compile(r"^(main|patch)\.(?:(\d+)\.)?(.+)\.obb$", re.IGNORECASE)
# Found by name next to an APK, only with that APK's package: other kinds too, e.g. Crisis Brigade 2's
# office.<versionCode>.<package>.obb and ta1.<versionCode>.<package>.obb (GitHub #415)
NAMED_OBB = re.compile(r"^([a-z][a-z0-9]*)\.(?:(\d+)\.)?(.+)\.obb$", re.IGNORECASE)


# What downloads and copies add after the package (GitHub #450/#481): main.203.com.Armature.VR4-003.obb,
# "… (1).obb", "… - Copy.obb". A package name has only letters, digits, '_' and '.', so none of these can be part of
# one: the file is the game's own under a changed name.
COPY_SUFFIX = re.compile(r"(?:-\d{1,4}|\s*\(\d{1,3}\)|\s+-\s+copy(?:\s*\(\d{1,3}\))?)", re.IGNORECASE)
_COPY_TAIL = re.compile(f"(?:{COPY_SUFFIX.pattern})$", re.IGNORECASE)
# a game's own split expansion files: nDreams' main.com.nDreams.PhantomQuest.part0.obb … part5
SPLIT_PART = re.compile(r"\.part\d{1,3}", re.IGNORECASE)


@dataclass(frozen=True)
class ObbName:
    """An expansion file whose name isn't the one the game looks for. rename: FramePort installs it on the Frame as
    `wanted` (the file on the PC keeps its name); else it is sent as it is and the game page warns."""
    name: str
    wanted: str
    rename: bool
    reason: str  # case | copy | another-app | duplicate | ambiguous | unclear-version


def obb_names(package: str, rels) -> list[ObbName]:
    """Expansion files (top level of the game's data) not named <kind>.<versionCode>.<package>.obb exactly, with what
    to do about each. Other kinds than main/patch (Crisis Brigade 2's office/alleys/ta1…, GitHub #415) only count
    when their package part is the game's in another case or with a copy suffix (never "another app's": they may be
    any data). Renamed (the name says which file it is, only its spelling is off): a
    different case (Main.203.com.armature.vr4.obb) and a copy suffix after the package (-003, " (1)", " - Copy").
    Warned, sent as they are: another app's name (a renamed build's main.1716.com.beatgames.beatsaber.lj369vr.obb,
    GitHub #129), a copy of a file that is there under its right name too, two files for one name, a copy suffix
    on a name without a versionCode (the number may be the versionCode). The versionCode is never changed: expansion
    files keep the one they were introduced with. Unreal's chunk files (patch.pakchunk27-Android_ASTC.<package>.obb)
    and split parts (.part0) are the game's own names. Names only, no disk access."""
    pkg = package or ""
    if not pkg:
        return []
    low = pkg.lower()
    names = sorted({r for r in rels if "/" not in r})
    found: list[tuple[str, str, str]] = []  # (name, wanted, reason)
    for name in names:
        m = OBB_NAME.match(name)
        if not m:
            m = NAMED_OBB.match(name)
            if m:
                # other kinds (Crisis Brigade 2's office.222.<package>.obb, GitHub #415): only the game's own files
                # with the package's case or a copy suffix off; anything else is some other data, never warned about
                own = _own_named(name, m, pkg, low)
                if own:
                    found.append(own)
            continue
        kind, vc, rest = m.group(1).lower(), m.group(2), m.group(3)
        version = f"{vc}." if vc else ""
        rl = rest.lower()
        if rl.endswith("." + low) and (not vc or rest[:-len(pkg) - 1].isdigit()):
            # Unreal chunk files: <kind>.<chunk>.<package>.obb, and numbered patch files
            # <kind>.<n>.<versionCode>.<package>.obb (Medal of Honor's patch.0.12297.com.Virtuos.R6DLM.obb … patch.20,
            # GitHub #159): only the spelling of the package/kind can be off
            wanted = f"{kind}.{version}{rest[:-len(pkg)]}{pkg}.obb"
            if wanted != name:
                found.append((name, wanted, "case"))
            continue
        if rl == low:
            reason, tail = "case", ""
        elif rl.startswith(low) and SPLIT_PART.fullmatch(rest[len(pkg):]):
            reason, tail = "case", rest[len(pkg):]
        elif rl.startswith(low) and COPY_SUFFIX.fullmatch(rest[len(pkg):]):
            reason, tail = ("copy" if vc else "unclear-version"), ""
        else:
            found.append((name, f"{kind}.{version}{pkg}.obb", "another-app"))
            continue
        wanted = f"{kind}.{version}{pkg}{tail}.obb"
        if wanted != name:
            found.append((name, wanted, reason))
    out = []
    present = set(names)
    for name, wanted, reason in found:
        if reason in ("case", "copy"):
            rivals = [n for n, w, r in found if w.lower() == wanted.lower() and r in ("case", "copy")]
            if wanted in present:
                reason = "duplicate"
            elif len(rivals) > 1:
                reason = "ambiguous"
        out.append(ObbName(name, wanted, reason in ("case", "copy"), reason))
    return out


def _own_named(name: str, m: re.Match, pkg: str, low: str) -> tuple[str, str, str] | None:
    """(name, wanted, reason) for a <kind>.<versionCode>.<package>.obb of another kind than main/patch whose package
    part is the game's in another case or with a copy suffix; the kind keeps its own spelling."""
    kind, vc, rest = m.group(1), m.group(2), m.group(3)
    rl = rest.lower()
    if rl == low:
        reason = "case"
    elif rl.startswith(low) and COPY_SUFFIX.fullmatch(rest[len(pkg):]):
        reason = "copy" if vc else "unclear-version"
    else:
        return None
    wanted = f"{kind}.{vc + '.' if vc else ''}{pkg}.obb"
    return (name, wanted, reason) if wanted != name else None


def frame_names(package: str, manifest: dict[str, int]) -> dict[str, str]:
    """{name in the game's data: name on the Frame} for the files obb_names renames (the rest keep theirs)."""
    return {o.name: o.wanted for o in obb_names(package, manifest) if o.rename}


def frame_manifest(package: str, manifest: dict[str, int] | None) -> dict[str, int]:
    """The data manifest as the Frame stores it (renamed expansion files under their Frame names): what an install
    sends as obb_manifest and what plan_update compares with the deployment, so a renamed file isn't "changed"."""
    names = frame_names(package, manifest or {})
    return {names.get(rel, rel): size for rel, size in (manifest or {}).items()}


def _recursive_obb_names(d: Path) -> set[str]:
    """Expansion file paths below d; propagate read errors so callers cannot mistake a partial scan for all files."""
    names = set()
    pending = [d]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif OBB_NAME.match(entry.name) and entry.is_file():
                    names.add(path.relative_to(d).as_posix())
    return names


def _named_obbs(d: Path, pkg: str) -> list[tuple[str, int | None, Path]]:
    """(kind, versionCode, file) of the package's expansion files directly in d. Unreal's extra chunk files
    (patch.pakchunk27-Android_ASTC.<package>.obb: Asgard's Wrath 2 has 81 of them, ~27 GB, GitHub #471) are each a
    kind of their own ("patch.pakchunk27-android_astc"), so every one is sent."""
    out = []
    suffix = "." + pkg.lower()
    for f, is_dir in _list(d):
        m = NAMED_OBB.match(f.name)
        if not m or is_dir:
            continue
        rest = m.group(3).lower()
        # a download's copy suffix too (any kind), installed renamed (obb_names)
        if rest == pkg.lower() or (rest.startswith(pkg.lower()) and COPY_SUFFIX.fullmatch(rest[len(pkg):])):
            # a download's copy suffix too (main.203.com.Armature.VR4-003.obb, GitHub #481): installed renamed
            out.append((m.group(1).lower(), int(m.group(2)) if m.group(2) else None, f))
        elif not m.group(2) and rest.endswith(suffix) and "." not in rest[:-len(suffix)]:
            out.append((m.group(1).lower() + "." + rest[:-len(suffix)], None, f))
        elif m.group(2) and rest.endswith(suffix) and rest[:-len(suffix)].isdigit():
            # numbered patch files <kind>.<n>.<versionCode>.<package>.obb: Medal of Honor has patch.0 … patch.20
            # (~41 GB, GitHub #159), each a kind of its own ("patch.3") with the versionCode after the number
            out.append((m.group(1).lower() + "." + m.group(2), int(rest[:-len(suffix)]), f))
    return out


def _obb_folders(apk_dir: Path, pkg: str, depth: int = 3) -> list[tuple[Path, list[tuple[str, int | None, Path]]]]:
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


def _pick_obbs(obbs: list[tuple[str, int | None, Path]], version_code: int | None) -> list[Path]:
    """The expansion files for this APK: per kind (main, patch, others like office) the newest one not newer than the
    APK's versionCode (expansion files keep the versionCode they were introduced with, so that's its own one); only
    when there is none at all, the newest ones (another version of the game's files: better than nothing).
    Versionless files are usable for any APK; a usable numbered file takes precedence."""
    usable = [o for o in obbs if version_code is None or o[1] is None or o[1] <= version_code] or obbs
    picked = []
    for kind in ("main", "patch", *sorted({k for k, _, _ in usable} - {"main", "patch"})):
        # the right name before a copy of the same version (main.203.<package>.obb over main.203.<package>-003.obb)
        same = sorted((vc if vc is not None else -1, not _COPY_TAIL.search(f.name[:-4]), f)
                      for k, vc, f in usable if k == kind)
        if same:
            picked.append(same[-1][2])
    return picked


def find_data(apk_dir: Path, pkg: str | None, version_code: int | None = None, *, strict: bool = False) \
        -> tuple[Path | None, list[str] | None]:
    """(data folder, files) for the APK in apk_dir. files is None when the whole folder is the game's data (the
    layouts of _find_data_dir), else the names of the expansion files to send from it (found by file name, e.g.
    lying next to the APK: the APK and anything else in that folder stay on the PC). With strict=True, discovery
    I/O errors propagate so library recovery can retry instead of treating an unreadable folder as empty."""
    token = _STRICT_READS.set(strict)
    try:
        return _find_data(apk_dir, pkg, version_code)
    finally:
        _STRICT_READS.reset(token)


# Asset files a store install puts into Android/obb/<package>/ next to (or instead of) the OBBs, by kind: Unity asset
# bundles (Ghost Town's localisation-translations_assets_all.bundle, GitHub #444), Unreal paks / IoStore containers,
# FMOD banks. Lying loose next to a game's only APK they are that game's data; other files there (readme, images,
# archives, other APKs) are not.
LOOSE_DATA_EXTS = (".bundle", ".assetbundle", ".unity3d", ".pak", ".utoc", ".ucas", ".bank")


def _loose_data(apk_dir: Path) -> list[str]:
    """Names of the asset files lying next to the APK in apk_dir, when it is the folder's only game APK (a folder of
    several APKs is a collection: whose files they are is unknown)."""
    entries = [(p, is_dir) for p, is_dir in _list(apk_dir) if not is_dir]
    apks = [p for p, _ in entries if p.suffix.lower() == ".apk" and ".alt-" not in p.name and not _axrb_patched(p)]
    if len(apks) != 1:
        return []
    return [p.name for p, _ in entries if p.suffix.lower() in LOOSE_DATA_EXTS]


def _find_data(apk_dir: Path, pkg: str | None, version_code: int | None) -> tuple[Path | None, list[str] | None]:
    data = _find_data_dir(apk_dir, pkg)
    if data is not None or not pkg:
        return data, None
    folders = _obb_folders(apk_dir, pkg)
    loose = _loose_data(apk_dir)
    if not folders:
        return (apk_dir, loose) if loose else (None, None)
    d, obbs = next((f for f in folders if version_code is not None and any(vc == version_code for _, vc, _ in f[1])),
                  folders[0])
    files = _pick_obbs(obbs, version_code)
    everything = {p.name for p, _ in _list(d)}
    names = [f.name for f in files]
    if d == apk_dir:
        names += [n for n in loose if n not in names]
    if (apk_dir.name.lower() in APK_FOLDERS and d.parent == apk_dir.parent
            and d.name.lower() in OBB_FOLDERS and not _has_apks(d)):
        # Retain SideQuest's sibling-folder assets only after the version-aware OBB search has chosen it.
        # Expanding a shared folder or one holding unselected OBB versions would undo that selection.
        try:
            obb_names = _recursive_obb_names(d)
        except OSError:
            if _STRICT_READS.get():
                raise
            return d, names
        if obb_names == set(names):
            return d, None
    return d, (None if everything == set(names) else names)


def find_data_dir(apk_dir: Path, pkg: str | None, version_code: int | None = None) -> Path | None:
    return find_data(apk_dir, pkg, version_code)[0]


# a folder that only holds the APK inside a game folder (<game>/apk/x.apk + <game>/obb/…): the game is the parent
APK_FOLDERS = ("apk", "apks")
# SideQuest's backup folder names: 2026-10-08T12-08-41-153Z (also with ':' / '.' separators)
BACKUP_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}[-:.]\d{2}[-:.]\d{2}([-:.]\d+)?Z?$")


def from_path(path: Path, reporter=None) -> SourceGame | None:
    """The game at path (an APK, an app bundle or a game folder); None when there is none. A bundle that can't be
    used raises bundles.BundleError (it says why)."""
    from . import bundles

    path = Path(path)
    if bundles.is_bundle_file(path):
        return bundles.source_game(path, reporter)
    if path.is_dir() and bundles.split_folder(path):
        return bundles.source_game(path, reporter)
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


def _bundle_files(d: Path) -> list[Path]:
    from .bundles import EXTS

    return [p for p, is_dir in _list(d) if not is_dir and p.suffix.lower() in EXTS]


def scan(root: Path, depth: int = 5, reporter=None, on_error=None) -> list[SourceGame]:
    """Find games under root, up to `depth` folder levels down (e.g. a download manager's
    "<library>/data/downloads/<game>/game.apk"). A folder with an APK and nothing but that game's data folder is one
    game; a folder that also has other folders is a collection: its loose APKs are games and its folders are searched
    (a "VR" folder with a stray APK next to the game folders used to count as one game). App bundles (.xapk, .apks,
    .apkm, .apkx) are games of their own wherever they lie; on_error(path, exc) hears about the ones that can't be
    used (a root that is one raises). PC program folders and code checkouts aren't searched."""
    from .bundles import BundleError

    found: list[SourceGame] = []

    def failed(path: Path, exc: Exception) -> None:
        if on_error:
            on_error(path, exc)

    def walk(d: Path, level: int) -> None:
        for b in _bundle_files(d):
            try:
                found.append(from_path(b, reporter))
            except (BundleError, OSError) as exc:
                failed(b, exc)
        try:
            game = from_path(d, reporter)
        except (BundleError, OSError) as exc:  # a folder of split APKs that can't be merged
            failed(d, exc)
            return
        if game and (level >= depth or not _other_folders(d, game)):
            found.append(game)
            return
        if game and game.bundle is None:  # a collection with loose APKs: each is a game of its own
            found.extend(g for g in (from_path(p) for p in sorted(d.glob("*.apk")) if _is_apk(p)) if g)
        elif game:
            found.append(game)
        if level >= depth:
            return
        try:
            children = sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
        except OSError:
            return
        for child in children:
            if game and game.data_dir and child.resolve() == game.data_dir.resolve():
                continue
            if _has_apks(child) or _bundle_files(child) or not _no_quest_games_below(child):
                walk(child, level + 1)

    root = Path(root)
    if root.is_file():
        g = from_path(root, reporter)
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
