"""Upload an existing Rift copy and register it with Meta's real runtime; no extraction or licence changes."""
from __future__ import annotations

import json
import os
import re
import shlex
import struct
import zlib
from pathlib import Path

from . import fexrift
from .analysis import rift, unreal_pak
from .core.events import Reporter


def skipped_file(name: str) -> bool:
    name = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return (name.endswith((".7z", ".ag")) or bool(re.search(r"\.7z\.0", name))
            or name in ("data.bin", "agrepackinstaller.exe", "vd.bat"))


def relative_file(name: str) -> str:
    name = name.replace("\\", "/")
    if (not name or name.startswith("/") or any(p in ("", ".", "..") for p in name.split("/"))
            or any(c in name for c in ':\x00\r\n')):
        raise ValueError(f"path must stay inside the game folder: {name!r}")
    return name


def inventory(folder: Path) -> tuple[dict, list[str]]:
    """Stat only: hashing happens on the Frame, avoiding a second full read from slow source drives."""
    files, skipped, seen = {}, [], set()
    for root, dirs, names in os.walk(folder):
        for name in dirs:
            if (Path(root) / name).is_symlink():
                raise ValueError("game folders must not contain directory symlinks")
        for name in sorted(names):
            path = Path(root) / name
            rel = relative_file(path.relative_to(folder).as_posix())
            if skipped_file(rel):
                skipped.append(rel)
                continue
            if path.is_symlink():
                raise ValueError(f"symlink in game folder: {rel}")
            if not path.is_file():
                raise ValueError(f"not a regular game file: {rel}")
            if rel.casefold() in seen:
                raise ValueError(f"duplicate Windows filename: {rel}")
            seen.add(rel.casefold())
            st = path.stat()
            files[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return files, sorted(skipped)


def ini_app_id(text: str) -> str | None:
    section = ""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].lower()
        elif section == "onlinesubsystemoculus" and not line.startswith((";", "#")):
            key, _, value = line.partition("=")
            value = value.split(";", 1)[0].strip().strip('"\'')
            if value.isdecimal() and int(value) > 0:
                values[key.strip().lower()] = value
    return next((values[k] for k in ("riftappid", "oculusappid", "appid") if k in values), None)


def config_app_id(folder: Path, files: dict) -> tuple[str | None, str | None]:
    # Only unencrypted config entries are read. No archive extraction, guessing from binary strings or traversal
    # into repack installers. Loose configs also cover Unity/custom engines using Oculus' common config names.
    for rel in sorted(files):
        if not rel.lower().endswith(".pak"):
            continue
        try:
            ver, _, entries, names = unreal_pak.pak_entries(folder / rel)
            for name, ent in entries.items():
                if not name.lower().endswith("config/defaultengine.ini") or ent[2] > 4 << 20:
                    continue
                value = ini_app_id(unreal_pak.pak_read(folder / rel, ver, ent, names).decode("utf-8-sig", "replace"))
                if value:
                    return value, f"config:{rel}:{name}"
        except (ValueError, OSError, struct.error, IndexError, zlib.error):
            continue
    for rel in sorted(files):
        path = folder / rel
        name = path.name.lower()
        if files[rel]["size"] > 4 << 20:
            continue
        if name in ("defaultengine.ini", "oculus.ini", "oculusconfig.ini"):
            value = ini_app_id(path.read_text("utf-8-sig", errors="replace"))
        elif name in ("oculusconfig.json", "oculus-platform-config.json"):
            try:
                data = json.loads(path.read_text("utf-8-sig"))
                value = next((str(data[k]) for k in ("RiftAppId", "OculusAppId", "AppId", "app_id")
                              if str(data.get(k, "")).isdecimal() and int(data[k]) > 0), None)
            except (ValueError, TypeError, KeyError):
                value = None
        else:
            continue
        if value:
            return value, f"config:{rel}"
    return None, None


def exe_version(path: Path) -> str | None:
    """VS_FIXEDFILEINFO from the PE resource section, without reading a large executable a second time."""
    try:
        with path.open("rb") as f:
            head = f.read(4096)
            pe = struct.unpack_from("<I", head, 0x3c)[0]
            if head[:2] != b"MZ" or head[pe:pe + 4] != b"PE\0\0":
                return None
            count = struct.unpack_from("<H", head, pe + 6)[0]
            opt_size = struct.unpack_from("<H", head, pe + 20)[0]
            f.seek(pe + 24 + opt_size)
            sections = f.read(min(count, 96) * 40)
            for off in range(0, len(sections), 40):
                if sections[off:off + 8].rstrip(b"\0") != b".rsrc":
                    continue
                size, raw = struct.unpack_from("<II", sections, off + 16)
                f.seek(raw)
                data = f.read(min(size, 32 << 20))
                pos = data.find(struct.pack("<II", 0xfeef04bd, 0x10000))
                if pos >= 0:
                    ms, ls = struct.unpack_from("<II", data, pos + 8)
                    return ".".join(str(v) for v in (ms >> 16, ms & 0xffff, ls >> 16, ls & 0xffff))
    except (OSError, ValueError, struct.error, IndexError):
        pass
    return None


def meta_manifest(folder: Path) -> dict | None:
    """Meta's own manifest when the folder is in a Meta app library (<library>/Software/<name> next to
    <library>/Manifests/<name>.json, e.g. C:\\Program Files\\Meta Horizon\\Software): its exe, launch arguments
    (Unreal games need -vr), app ID and version beat anything guessed from the files."""
    path = folder.parent.parent / "Manifests" / f"{folder.name}.json"
    if folder.parent.name.lower() != "software" or not path.is_file():
        return None
    try:
        m = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if m.get("canonicalName") != folder.name or not m.get("launchFile") or not str(m.get("appId", "")).isdigit():
        return None
    return m


def library_manifest(app_id: str | None, canonical: str | None, exe: str) -> dict | None:
    """The same game's manifest in one of this PC's Meta app libraries (installed with the user's own account), for its
    launch arguments: only when Meta's exe has the same file name as the one being installed (same game build)."""
    from .core import winhost

    for lib in winhost.meta_libraries():
        for path in sorted((lib / "Manifests").glob("*.json")):
            try:
                m = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not (str(m.get("appId")) == str(app_id) or (canonical and m.get("canonicalName") == canonical)):
                continue
            if Path(str(m.get("launchFile", "")).replace("\\", "/")).name.lower() == Path(exe).name.lower():
                return m
    return None


def source_info(folder: Path, entry: dict | None = None, title: str | None = None,
                app: str | None = None, app_id: str | None = None) -> dict:
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"game folder not found: {folder}")
    files, skipped = inventory(folder)
    entry = entry or {}
    extra = (entry.get("analysis") or {}).get("extra") or {}
    meta = None if entry else meta_manifest(folder)
    exe = entry.get("exe") or extra.get("exe") or (meta and meta["launchFile"].replace("\\", "/"))
    if not exe:
        analysis = rift.analyze(folder)
        if not analysis.extra.get("exe_confirmed"):
            raise ValueError("several game programs found; choose one in the FramePort library first")
        extra = analysis.extra
        exe = extra["exe"]
    exe = relative_file(str(exe))
    if exe not in files:
        raise ValueError("the selected game program is missing or is a skipped repack file")
    title = title or entry.get("title") or folder.name
    canonical = (app or extra.get("canonical_name") or (meta and meta["canonicalName"])
                 or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"))
    package = fexrift.package_for(canonical)  # validate before any remote mutation
    store_url = (entry.get("details") or {}).get("store_url") or ""
    match = re.fullmatch(r"https://(?:www\.)?meta\.com/experiences/(\d+)/?", store_url)
    store_id = match[1] if match else None
    found, source = (str(meta["appId"]), "meta-manifest") if meta else config_app_id(folder, files)
    if not found and store_id:
        found, source = store_id, "oculusdb:details.store_url"
    if not found and app_id:
        if not re.fullmatch(r"[1-9][0-9]*", app_id):
            raise ValueError("--app-id must be a positive numeric Meta app ID")
        found, source = app_id, "cli:--app-id"
    if not found:
        raise ValueError("no Meta app ID in the game's config or cached store details; supply --app-id")
    args = entry.get("launch_args", extra.get("launch_args", [])) or (shlex.split(meta.get("launchParameters") or "")
                                                                      if meta else [])
    if isinstance(args, str):
        args = shlex.split(args)
    if not args and not meta:
        # Meta launches many Rift games with arguments (Unreal: -vr); a copy installed elsewhere has none of them
        lib = library_manifest(found, canonical, exe)
        if lib:
            args = shlex.split(lib.get("launchParameters") or "")
    version = re.search(r"(?i)(?<![a-z0-9])(?:v|version[ _-]*)?(\d+(?:\.\d+)+)", folder.name)
    return {"folder": folder, "app": canonical, "package": entry.get("package") or package, "title": title,
            "exe": exe, "args": args, "app_id": found, "app_id_source": source, "store_app_id": store_id,
            "version": (meta and meta.get("version")) or (version[1] if version else exe_version(folder / exe))
                       or "0.0.0",
            "files": files, "skipped": skipped}


def install(frame, reporter: Reporter, *, from_library: str | None = None, folder: Path | None = None,
            title: str | None = None, app: str | None = None, app_id: str | None = None, dest: str | None = None,
            msaa: int | None = None, add_to_steam: bool = True, force_unverified: bool = False,
            runtime_check: bool = False) -> dict:
    from .core import library
    from .install import installer

    fexrift.require_enabled()
    if bool(from_library) == bool(folder):
        raise ValueError("choose exactly one of --from-library or --folder")
    entry = None
    if from_library:
        entry = library.load().get("games", {}).get(from_library)
        if not entry or entry.get("kind") != "rift":
            raise ValueError("--from-library needs a Rift library entry")
        folder = Path(entry["game_dir"])
    reporter.stage("Inspect local game files")
    info = source_info(folder, entry, title, app, app_id)
    if info["app"] not in fexrift.GAMES and not force_unverified:
        raise ValueError("this game is unverified; use --force-unverified to upload and install it")
    for rel in info["skipped"]:
        reporter.log(f"Skipped repack leftover: {rel}")
    reporter.check("Meta app ID", True, f"{info['app_id']} ({info['app_id_source']})")
    reporter.check_cancel()
    fexrift.sync_helpers(frame, reporter)
    prep = frame.agent("fex_prepare_local", app=info["app"], package=info["package"], dest=dest,
                       files=info["files"])
    items = [(info["folder"] / rel, rel, st["size"]) for rel, st in info["files"].items()
             if prep["existing"].get(rel) != st["size"]]
    need = sum(size for _, _, size in items)
    if need > prep["free_bytes"] - (1 << 30):
        raise RuntimeError("not enough space for the game + 1 GiB headroom")
    reporter.stage(f"Upload {info['title']} ({need / 2**30:.1f} GiB remaining)")
    with installer.transfer_link(frame, reporter, need) as xfer:
        if items:
            installer.upload_files(xfer, items, prep["game_dir"], reporter, max(need, 1))
    reporter.check_cancel()
    if inventory(info["folder"])[0] != info["files"]:
        raise RuntimeError("source files changed during upload; retry with a stable game folder")
    reporter.stage("Hash and register the game on the Frame")
    metadata = {k: v for k, v in info.items() if k not in ("folder", "files")}
    frame.agent("fex_register_local", timeout=7200, **metadata)
    reporter.stage("Install launcher and generic fixes")
    result = frame.agent("fex_install", package=info["package"], app=info["app"], title=info["title"],
                         source="local_copy", msaa=msaa, force_unverified=force_unverified)
    if add_to_steam:
        installer.add_to_steam(frame, [info["package"]], reporter)
    if runtime_check:
        result["checks"] = dict(result.get("checks") or {},
                                **frame.agent("fex_runtime_state", app=info["app"], timeout=125))
        reporter.check("Meta library state", result["checks"].get("runtime_state") in ("ready", "installed"),
                       result["checks"].get("runtime_state", "unknown"))
    return result
