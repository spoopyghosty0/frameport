"""Linux apps for the Frame (GitHub #31): an AppImage, a folder, or a .zip/.tar.* archive with an aarch64 program
(or an x86_64 one, which the Frame runs through FEX). Finds the program to start, checks the CPU architecture and
whether it's an OpenXR (VR) app. Archives are unpacked once into FramePort's data folder, so installs upload a
plain folder like PC VR games do."""
from __future__ import annotations

import hashlib
import re
import shutil
import tarfile
import zipfile
from pathlib import Path

from ..core.paths import user_data_dir

EM_AARCH64, EM_X86_64 = 183, 62
ARCHIVES = (".zip", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tar")
OPENXR_MARKERS = (b"libopenxr_loader.so", b"xrCreateInstance")
MAX_SCAN = 256 << 20  # don't read huge files looking for OpenXR markers


def elf_machine(path: Path) -> int | None:
    """e_machine of an ELF file (183 = aarch64, 62 = x86_64), None if it isn't ELF."""
    try:
        with open(path, "rb") as f:
            head = f.read(20)
    except OSError:
        return None
    if head[:4] != b"\x7fELF" or len(head) < 20:
        return None
    return int.from_bytes(head[18:20], "little" if head[5] == 1 else "big")


def is_appimage(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(11)
    except OSError:
        return False
    return head[:4] == b"\x7fELF" and head[8:11] == b"AI\x02"


def is_archive(path: Path) -> bool:
    return path.is_file() and path.name.lower().endswith(ARCHIVES)


def looks_like_linux_app(path: Path) -> bool:
    """For the file picker / drag-and-drop: something this module can add."""
    path = Path(path)
    return path.is_dir() or is_archive(path) or is_appimage(path) or elf_machine(path) is not None


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:48] or "app"


def clean_title(name: str) -> str:
    """'Venera-Prime-2.4.2-aarch64.AppImage' -> 'Venera Prime'."""
    stem = re.sub(r"(\.AppImage|\.tar\.\w+|\.tgz|\.txz|\.zip)$", "", name, flags=re.I)
    stem = re.split(r"[-_ .]v?\d+(\.\d+)+", stem, maxsplit=1)[0]
    stem = re.sub(r"[-_ .](linux|aarch64|arm64|x86_64|amd64)\b.*$", "", stem, flags=re.I)
    return re.sub(r"[-_.]+", " ", stem).strip() or name


def unpack(archive: Path) -> Path:
    """Unpack an archive into <data>/linux-apps/<slug>-<hash>/ (once per archive version)."""
    st = archive.stat()
    key = hashlib.sha1(f"{archive.resolve()}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:8]
    dest = user_data_dir() / "linux-apps" / f"{slug(clean_title(archive.name))}-{key}"
    done = dest / ".unpacked"
    if done.exists():
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    if archive.name.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                target = (dest / info.filename).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    raise ValueError(f"unsafe path in archive: {info.filename}")
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data")
    done.write_text(archive.name, encoding="utf-8")
    return dest


def _desktop_exec(root: Path) -> str | None:
    for desktop in sorted(root.rglob("*.desktop"))[:5]:
        m = re.search(r"^Exec=(\S+)", desktop.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            return Path(m.group(1).strip('"')).name
    return None


def _desktop_name(root: Path) -> str | None:
    for desktop in sorted(root.rglob("*.desktop"))[:5]:
        m = re.search(r"^Name=(.+)$", desktop.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            return m.group(1).strip()
    return None


def _png_width(path: Path) -> int | None:
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n" or len(head) < 24:
        return None
    return int.from_bytes(head[16:20], "big")


def find_icon(root: Path) -> Path | None:
    """The app's own icon in a folder (GitHub #99): the Icon= of its bundled .desktop file, looked up next to the
    file, in (usr/)share/icons/hicolor/<size>/apps and (usr/)share/pixmaps, or a top-level .DirIcon; the biggest PNG
    (the PC can't draw SVG icons; the Frame's agent uses those for Desktop Mode). Nothing outside `root` (symlinks
    are resolved and checked). AppImages are read on the Frame instead (their files are compressed)."""
    root = Path(root)
    try:
        real_root = root.resolve()
    except OSError:
        return None
    paths = [root / ".DirIcon"]
    desktops = [p for p in sorted(root.rglob("*.desktop"))[:5] if len(p.relative_to(root).parts) <= 4]
    for desktop in desktops:
        m = re.search(r"^Icon=(.+)$", desktop.read_text(encoding="utf-8", errors="replace"), re.M)
        name = m.group(1).strip() if m else ""
        if not name or name.startswith("/") or ".." in name.split("/"):
            continue
        stem = re.sub(r"\.(png|svg|xpm)$", "", name, flags=re.I)
        bases = [root] + [d for d in desktop.parents if d != root and real_root in d.resolve().parents]
        for base in bases:
            paths += [base / name, base / f"{stem}.png"]
            for share in (base / "usr" / "share", base / "share"):
                paths += sorted(share.glob(f"icons/hicolor/*/apps/{glob_escape(stem)}.png"))
                paths.append(share / "pixmaps" / f"{stem}.png")
        break
    best: tuple[int, Path] | None = None
    for p in paths:
        try:
            real = p.resolve()
            if real_root not in real.parents or not real.is_file() or real.stat().st_size > 4 << 20:
                continue
        except OSError:
            continue
        width = _png_width(real)
        if width and (best is None or width > best[0]):
            best = (width, real)
    return best[1] if best else None


def glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def rank_programs(root: Path) -> list[tuple[Path, int]]:
    """(ELF program, machine) candidates, best first: the .desktop Exec= name, then a name like the folder's, then
    shallow and big ones. Libraries (*.so*) are never programs."""
    wanted = _desktop_exec(root)
    folder = slug(root.name.split("-")[0])
    found = []
    for p in root.rglob("*"):
        if not p.is_file() or ".so" in p.name or p.suffix in (".py", ".pyc", ".txt", ".json"):
            continue
        machine = elf_machine(p)
        if machine is None:
            continue
        rel_depth = len(p.relative_to(root).parts)
        score = (100 if wanted and p.name == wanted else 0) + (30 if slug(p.stem) and slug(p.stem) in folder else 0) \
            + (20 if folder and folder in slug(p.stem) else 0) - 5 * rel_depth
        found.append((score, p.stat().st_size, p, machine))
    found.sort(key=lambda t: (-t[0], -t[1]))
    return [(p, m) for _s, _size, p, m in found]


def app_libraries(root: Path, exe: Path) -> list[Path]:
    """Libraries the program loads: next to it, in a lib*/ folder beside it, or the app's usr/lib*. Libraries deep
    in data folders don't count (FramePort's own build ships FrameBridge's OpenXR loader as data and isn't VR)."""
    dirs = {exe.parent}
    for base in (exe.parent, root, root / "usr"):
        dirs.update(d for d in base.glob("lib*") if d.is_dir())
    out = []
    for d in dirs:
        out += [p for p in d.glob("*.so*") if p.is_file()]
    return sorted(out)[:200]


def uses_openxr(paths: list[Path]) -> bool:
    for p in paths:
        try:
            if p.stat().st_size > MAX_SCAN:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if any(m in data for m in OPENXR_MARKERS):
            return True
    return False


def runs_on_frame(machine: int | None) -> bool:
    """aarch64 natively, x86_64 through FEX (x86 translation, slower)."""
    return machine in (EM_AARCH64, EM_X86_64)


def inspect(path: Path) -> dict:
    """{root, exe (relative to root), appimage, machine, arch_ok (aarch64: native), openxr, title, candidates} for an
    AppImage, a
    folder or an archive. Raises ValueError when there's no Linux program in it."""
    path = Path(path)
    if path.is_file() and is_archive(path):
        root = unpack(path)
        subdirs = [d for d in root.iterdir() if not d.name.startswith(".")]
        if len(subdirs) == 1 and subdirs[0].is_dir():  # the usual single top folder
            pass
        title = clean_title(path.name)
    elif path.is_file():
        root = path.parent
        title = clean_title(path.name)
    else:
        root = path
        title = clean_title(path.name)
    if path.is_file() and not is_archive(path):
        machine = elf_machine(path)
        if machine is None:
            raise ValueError(f"{path.name} isn't a Linux program (AppImage or ELF)")
        appimage = is_appimage(path)
        return {"root": str(path.parent), "exe": path.name, "files": [path.name], "appimage": appimage,
                "machine": machine, "arch_ok": machine == EM_AARCH64, "openxr": uses_openxr([path]),
                "title": title, "candidates": [path.name],
                "vr_overlay": False, "vr_overlay_app": None, "vr_overlay_from": ""}
    programs = rank_programs(root)
    if not programs:
        raise ValueError(f"no Linux program found in {path.name}")
    # an arm64 build wins; x86_64 programs only when there is nothing else (they run through FEX on the Frame)
    for want in (EM_AARCH64, EM_X86_64):
        if any(m == want for _p, m in programs):
            programs = [(p, m) for p, m in programs if m == want]
            break
    appimages = [p for p, _m in programs if is_appimage(p)]
    exe, machine = (appimages[0], elf_machine(appimages[0])) if appimages else programs[0]
    libs = app_libraries(root, exe)
    title = _desktop_name(root) or title
    return {"root": str(root), "exe": exe.relative_to(root).as_posix(), "files": None,
            "appimage": is_appimage(exe), "machine": machine, "arch_ok": machine == EM_AARCH64,
            "openxr": uses_openxr([exe] + libs), "title": title,
            "candidates": [p.relative_to(root).as_posix() for p, m in programs[:20] if m == machine],
            **overlay_info(root, exe, libs)}


def overlay_info(root: Path, exe: Path, libs: list[Path]) -> dict:
    """A SteamVR overlay app (analysis/vroverlay.py): a bundled .vrmanifest, or an OpenVR program using IVROverlay."""
    from . import vroverlay

    openvr = any(p.name.startswith(("libopenvr_api", "openvr_api")) for p in libs) or \
        any(p.name.startswith("libopenvr_api") for p in root.rglob("libopenvr_api*.so"))
    try:
        data = exe.read_bytes() if exe.stat().st_size <= MAX_SCAN else b""
    except OSError:
        data = b""
    return vroverlay.detect(root, exe, data, openvr)
