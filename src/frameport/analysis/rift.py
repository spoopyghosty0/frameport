"""Inspect an Oculus Rift (Windows PC VR) game folder: which program starts it, bitness, engine, XR API, graphics API,
and whether it uses the Oculus Platform SDK (entitlement checks that need the Oculus app).

Game folders come in many shapes (repacks nest the game one or more levels down, next to installers and archives):
    Stormland/Stormland.exe
    Asgards Wrath/WindowsNoEditor/WrathGame.exe + …/WrathGame/Binaries/Win64/WrathGame-Win64-Shipping.exe
    The Climb/bin/win_x64/Climb.exe + The Climb/bin/win_x64-steam/Climb.exe
One recursive walk (walk()) collects every .exe/.dll; candidates are GUI-subsystem x86/x64 PE files that aren't
installers or helpers; rank_exes() scores them with human-readable reasons and flags ambiguous cases for the user.
A tiny PE reader (header, imports, icon resources) is enough; no third-party dependency (Pillow only for icons).
"""
from __future__ import annotations

import json
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..core.models import Analysis
from . import vroverlay

MACHINES = {0x14C: "x86", 0x8664: "x86_64", 0xAA64: "arm64"}
GUI_SUBSYSTEM = 2
SKIP_EXE = re.compile(
    r"(?i)(crash|unins|setup|install|redist|vc_?redist|dxsetup|prereq|launcher_helper|cefprocess|ueprereq|dotnet|"
    r"oculus.?platform|easyanticheat|eac_|report|uploader|bssndrpt|agrepack|modinstaller|^cli(-\d+)?\.exe$|"
    r"^gui(-\d+)?\.exe$|^t(32|64)\.exe$|^w(32|64)\.exe$|updater|patcher|benchmark|helper|touchup|"
    r"vrmonitor|steamerrorreporter|unitycrashhandler|ue4editor|editor\.exe$)")
PRUNE_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages", "_uninstall", "uninstallme",
              "redist", "_commonredist", "directx", "support", "prereqs", "__installer", "installers", "vcredist",
              "dotnetfx", "$pluginsdir", "__macosx"}
MAX_DEPTH = 8
_OVR_ASCII = (b"LibOVRRT%s_%d.dll", b"LibOVRRT64_1.dll", b"LibOVRRT32_1.dll", b"LibOVRRT%hs_%d.dll",
              b"OVR_CAPI", b"ovr_Initialize", b"OculusHMD", b"OculusRift")
# Unreal stores most strings as UTF-16 (TEXT("…")), so look for both encodings
LIBOVR_MARKERS = _OVR_ASCII + tuple(m.decode().encode("utf-16-le") for m in _OVR_ASCII)
PLATFORM_DLLS = ("libovrplatform64_1.dll", "libovrplatform32_1.dll")
# engine DLLs that wrap the Platform SDK (Ready At Dawn's platform services: Lone Echo, Echo Arena)
PLATFORM_WRAPPERS = ("pnsovr.dll",)
GRAPHICS = (("d3d12.dll", "D3D12"), ("d3d11.dll", "D3D11"), ("vulkan-1.dll", "Vulkan"), ("opengl32.dll", "OpenGL"))
# bump when analyze() learns something new: folders analysed by an older version are analysed again at the next scan
# (part of the fingerprint). 2: vr_overlay (SteamVR overlay apps), pyopenvr's libopenvr_api_64.dll
RIFT_ANALYSIS = 2
AMBIGUITY = 15  # a runner-up within this many points means "ask the user"
# repacks start their bundled Revive through a proxy DLL next to the exe (Windows loads DLLs from the exe's folder
# first)
LOADER_DLLS = ("xinput1_3.dll", "xinput1_4.dll", "xinput9_1_0.dll", "dinput8.dll", "version.dll", "winmm.dll")
REVIVE_DLLS = ("librevive64.dll", "librevive32.dll", "librevivexr64.dll", "librevivexr32.dll")
# OpenVR's client library under its usual names (games ship openvr_api.dll; pyopenvr apps libopenvr_api_64.dll)
OPENVR_DLLS = ("openvr_api64.dll", "openvr_api32.dll", "openvr_api.dll", "libopenvr_api_64.dll", "libopenvr_api_32.dll")


class PEError(Exception):
    pass


@dataclass
class PEInfo:
    machine: str
    imports: list[str] = field(default_factory=list)  # lower-case DLL names
    subsystem: int = 0
    delay_imports: list[str] = field(default_factory=list)  # DLLs loaded on first use (e.g. the Oculus Platform SDK)


# ------------------------------------------------------------------------------------------ PE reading
def _pe_offsets(data: bytes) -> tuple[int, int, int, int, int]:
    if data[:2] != b"MZ" or len(data) < 0x40:
        raise PEError("not a PE file")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise PEError("no PE header")
    machine, nsec, _, _, _, opt_size, _ = struct.unpack_from("<HHIIIHH", data, pe + 4)
    return pe, machine, nsec, opt_size, pe + 24


def pe_header(path: Path) -> PEInfo:
    """Machine + subsystem from the first 4 KiB (cheap: used to filter candidates)."""
    with open(path, "rb") as f:
        data = f.read(4096)
    try:
        pe, machine, _, _, opt = _pe_offsets(data)
        subsystem = struct.unpack_from("<H", data, opt + 68)[0]
    except (struct.error, PEError, IndexError):
        raise PEError("not a PE file") from None
    return PEInfo(MACHINES.get(machine, hex(machine)), [], subsystem)


def _sections(data: bytes, opt: int, opt_size: int, nsec: int) -> list[tuple[int, int, int]]:
    out = []
    for k in range(nsec):
        s = opt + opt_size + 40 * k
        vsize, va, rsize, raw = struct.unpack_from("<IIII", data, s + 8)
        out.append((va, max(vsize, rsize), raw))
    return out


def _rva_to_off(sections, rva):
    for va, size, raw in sections:
        if va <= rva < va + size:
            return rva - va + raw
    return None


def read_pe(path: Path, limit: int = 256 << 20) -> tuple[PEInfo, bytes]:
    data = path.read_bytes() if path.stat().st_size <= limit else path.open("rb").read(limit)
    pe, machine, nsec, opt_size, opt = _pe_offsets(data)
    magic = struct.unpack_from("<H", data, opt)[0]
    subsystem = struct.unpack_from("<H", data, opt + 68)[0]
    dd = opt + (96 if magic == 0x10B else 112)  # data directories (PE32 / PE32+)
    imp_rva = struct.unpack_from("<I", data, dd + 8)[0] if opt_size >= dd - opt + 16 else 0
    sections = _sections(data, opt, opt_size, nsec)
    delay_rva = struct.unpack_from("<I", data, dd + 13 * 8)[0] if opt_size >= dd - opt + 14 * 8 else 0

    def names(table_rva, entry_size, name_at):
        out = []
        p = _rva_to_off(sections, table_rva) if table_rva else None
        while p is not None and p + entry_size <= len(data) and len(out) < 512:
            name_rva = struct.unpack_from("<I", data, p + name_at)[0]
            if not name_rva:
                break
            q = _rva_to_off(sections, name_rva)
            if q is not None:
                end = data.find(b"\0", q, q + 256)
                out.append(data[q:end].decode("ascii", "replace").lower())
            p += entry_size
        return out
    return PEInfo(MACHINES.get(machine, hex(machine)), names(imp_rva, 20, 12), subsystem,
                  names(delay_rva, 32, 4)), data


def exe_icon(path: Path, max_bytes: int = 64 << 20) -> bytes | None:
    """The largest icon image in a PE's resources (RT_GROUP_ICON → RT_ICON), as PNG bytes; None if there's none."""
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes)
        pe, machine, nsec, opt_size, opt = _pe_offsets(data)
        magic = struct.unpack_from("<H", data, opt)[0]
        dd = opt + (96 if magic == 0x10B else 112)
        res_rva = struct.unpack_from("<I", data, dd + 16)[0]
        sections = _sections(data, opt, opt_size, nsec)
        base = _rva_to_off(sections, res_rva) if res_rva else None
        if base is None:
            return None

        def entries(off):
            named, ids = struct.unpack_from("<HH", data, off + 12)
            for i in range(named + ids):
                name, target = struct.unpack_from("<II", data, off + 16 + 8 * i)
                yield name, target

        def leaves(off):  # (id, data offset, size) under a type directory: name → language → data
            for name, t in entries(off):
                if not t & 0x80000000:
                    continue
                for _, lang in entries(base + (t & 0x7FFFFFFF)):
                    if lang & 0x80000000:
                        continue
                    rva, size = struct.unpack_from("<II", data, base + lang)
                    o = _rva_to_off(sections, rva)
                    if o is not None:
                        yield name & 0x7FFFFFFF, o, size
        types = {name: t for name, t in entries(base) if t & 0x80000000}
        if 14 not in types or 3 not in types:
            return None
        icons = {i: (o, s) for i, o, s in leaves(base + (types[3] & 0x7FFFFFFF))}
        best = None
        for _, o, _ in leaves(base + (types[14] & 0x7FFFFFFF)):
            count = struct.unpack_from("<H", data, o + 4)[0]
            for k in range(count):
                w, h, colors, _, planes, bits, size, icon_id = struct.unpack_from("<BBBBHHIH", data, o + 6 + 14 * k)
                area = (w or 256) * (h or 256) * max(bits, 1)
                if icon_id in icons and (best is None or area > best[0]):
                    best = (area, icon_id, w, h, bits)
        if not best:
            return None
        o, size = icons[best[1]]
        raw = data[o:o + size]
        if raw[:4] == b"\x89PNG":
            return raw
        # a DIB: wrap it as a one-image .ico and let Pillow decode it
        w, h, bits = best[2], best[3], best[4]
        ico = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", w, h, 0, 0, 1, bits, len(raw), 22) + raw
        import io

        from PIL import Image

        with Image.open(io.BytesIO(ico)) as im:
            out = io.BytesIO()
            im.convert("RGBA").save(out, "PNG")
            return out.getvalue()
    except Exception:  # noqa: BLE001 - any malformed resource: no icon
        return None


# ------------------------------------------------------------------------------------------ folder walk
@dataclass
class Tree:
    root: Path
    exes: list[Path] = field(default_factory=list)  # every .exe (unfiltered)
    dlls: dict[str, Path] = field(default_factory=dict)  # lower-case name -> first path
    has_apk: bool = False
    size: int = 0
    files: int = 0
    manifest: dict | None = None

    def child_of(self, p: Path) -> str:
        rel = p.relative_to(self.root).parts
        return rel[0] if len(rel) > 1 else "."


def walk(root: Path, max_depth: int = MAX_DEPTH) -> Tree:
    """One pass over a folder: exes, dll names, apk presence, total size (and an Oculus manifest json if present)."""
    root = Path(root)
    t = Tree(root)
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if depth < max_depth and e.name.lower() not in PRUNE_DIRS:
                            stack.append((Path(e.path), depth + 1))
                        continue
                    name = e.name.lower()
                    t.files += 1
                    try:
                        t.size += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
                    if name.endswith(".exe"):
                        t.exes.append(Path(e.path))
                    elif name.endswith(".dll"):
                        t.dlls.setdefault(name, Path(e.path))
                    elif name.endswith(".apk"):
                        t.has_apk = True
                    elif name.endswith(".json") and depth <= 2 and t.manifest is None and e.stat().st_size < 1 << 20:
                        t.manifest = _manifest(Path(e.path))
                except OSError:
                    continue
    return t


def _manifest(path: Path) -> dict | None:
    """Oculus CoreData manifest (<canonicalName>.json with launchFile)."""
    try:
        d = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if isinstance(d, dict) and d.get("launchFile") and (d.get("canonicalName") or d.get("appId")):
        return d
    return None


def candidates(tree: Tree) -> list[Path]:
    """Programs that could start the game: not an installer/helper, a Windows GUI program for x86/x64."""
    out = []
    for p in tree.exes:
        if SKIP_EXE.search(p.name):
            continue
        try:
            h = pe_header(p)
        except (PEError, OSError):
            continue
        if h.machine in ("x86", "x86_64") and h.subsystem == GUI_SUBSYSTEM:
            out.append(p)
    return out


# ------------------------------------------------------------------------------------------ naming
# words that describe a build rather than the game (neutral; release tags are removed by their form, not by name)
_BUILD_WORDS = r"(?:Shipping|GOG|Oculus|Rift|PCVR|Repacks?|Release)"
# a release tag at the end: " -TAG", " -TAG v76", " -[TAG Repacks]" (a space before the dash and none after it, so
# subtitles like " - Episode II" and hyphenated words like "Rick-ality" stay)
_TRAILING_TAG = r"\s-(?=[\[A-Za-z0-9])(?:\[[^\]]*\]|[A-Za-z0-9]{2,12}\b)(?:\s+v\d+)?.*$"


def clean_title(name: str) -> str:
    """'Asgards Wrath v1.6.0 -TAG' -> 'Asgards Wrath'; 'Arktika 1 (v1.0.0.7) -TAG' -> 'Arktika 1';
    'Vader Immortal - Episode II v2.0.2+236948 Shipping' -> 'Vader Immortal - Episode II'."""
    t = re.sub(_TRAILING_TAG, " ", name)
    t = re.sub(r"\[[^\]]*\]", " ", t)  # [TAG Repacks]
    t = re.sub(r"\((?:[^)]*\d[^)]*|[^)]*\b" + _BUILD_WORDS + r"\b[^)]*)\)", " ", t, flags=re.I)  # (v1.0.0.7)
    t = re.sub(r"\bv\d[\w.+\-]*", " ", t, flags=re.I)  # v1.6.0, v008, v2.0.2+236948
    t = re.sub(r"(?<![\w.])\d+(?:\.\d+){2,}[\w+\-]*", " ", t)  # 4.15.20, 21.11.08.358012
    t = re.sub(r"\b" + _BUILD_WORDS + r"\b", " ", t, flags=re.I)
    t = re.sub(r"[_]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -_.")
    return t or name.strip()


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower() or "game"
    return ("g" + s) if s[0].isdigit() else s


def game_id(folder: Path, manifest: dict | None = None) -> str:
    base = (manifest or {}).get("canonicalName") or clean_title(folder.name)
    return "rift." + slug(base)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ------------------------------------------------------------------------------------------ ranking
def is_electron(exe: Path) -> bool:
    """An Electron (Chromium) app: e.g. a game's own launcher next to the game (SUPERHOT VR's SHVR.exe, GitHub #105).
    Its files sit next to the exe; a Unity/Unreal game's own exe has its `<name>_Data` folder instead."""
    d = exe.parent
    if (d / f"{exe.stem}_Data").is_dir():
        return False
    return (d / "resources" / "app.asar").is_file() or ((d / "icudtl.dat").is_file() and any(d.glob("*.pak")))


def rank_exes(folder: Path, cands: list[Path], manifest: dict | None = None,
              dlls: dict[str, Path] | None = None) -> list[dict]:
    """Candidates best-first: {path (relative, /), score, reasons, size, machine}."""
    folder = Path(folder)
    title = _norm(clean_title(folder.name))
    launch = (manifest or {}).get("launchFile", "").replace("\\", "/").lower()
    out = []
    for p in cands:
        rel = p.relative_to(folder).as_posix()
        score, reasons = 0, []
        stem = p.stem.lower()
        if launch and rel.lower().endswith(launch):
            score += 1000
            reasons.append("named in the Oculus manifest")
        if stem.endswith("-win64-shipping") or stem.endswith("-win32-shipping"):
            score += 50
            reasons.append("Unreal game build")
        elif (p.parent / f"{p.stem}_Data").is_dir() or ((p.parent / "UnityPlayer.dll").exists()
                                                         and not is_electron(p)):
            score += 50
            reasons.append("Unity game (next to its data)")
        if is_electron(p):
            score -= 30
            reasons.append("Electron launcher (starts the game)")
        if list(p.parent.glob(f"*/Binaries/Win64/{p.stem}-Win64-Shipping.exe")):
            score -= 30
            reasons.append("Unreal launcher (starts the real game build)")
        n = _norm(p.stem.replace("-Win64-Shipping", ""))
        if n and title and (n in title or title in n or n[:4] == title[:4]):
            score += 20
            reasons.append("name matches the game")
        try:
            machine = pe_header(p).machine
        except (PEError, OSError):
            machine = "?"
        if machine == "x86_64":
            score += 5
            reasons.append("64-bit")
        try:
            sib = {x.name.lower() for x in p.parent.iterdir()}
        except OSError:
            sib = set()
        if sib & set(REVIVE_DLLS) and sib & set(LOADER_DLLS):
            score += 40
            reasons.append("set up for SteamVR by the repack (bundled Revive + loader)")
        parts = [x.lower() for x in p.relative_to(folder).parts[:-1]]
        if any("steam" in x for x in parts):
            score -= 10
            reasons.append("Steam build")
        if any(x in ("win7", "dx11") for x in parts):
            reasons.append(parts[-1])
        size = p.stat().st_size
        score += min(10, size // (40 << 20)) - len(parts)
        out.append({"path": rel, "score": score, "reasons": reasons, "size": size, "machine": machine})
    return sorted(out, key=lambda c: (-c["score"], c["path"]))


def is_ambiguous(ranked: list[dict]) -> bool:
    return len(ranked) > 1 and ranked[0]["score"] < 1000 and ranked[1]["score"] >= ranked[0]["score"] - AMBIGUITY


# ------------------------------------------------------------------------------------------ analysis
def fingerprint(folder: Path, exe_rel: str) -> str:
    p = Path(folder) / exe_rel
    try:
        st = p.stat()
        return f"{exe_rel}:{st.st_size}:{int(st.st_mtime)}:{RIFT_ANALYSIS}"
    except OSError:
        return ""


def analyze(folder: Path, exe: str | None = None, tree: Tree | None = None) -> Analysis:
    """exe: a relative path chosen by the user (else the best-ranked candidate)."""
    folder = Path(folder)
    tree = tree or walk(folder)
    manifest = tree.manifest
    ranked = rank_exes(folder, candidates(tree), manifest, tree.dlls)
    if exe:
        chosen = exe
        confirmed = True
    elif ranked:
        chosen = ranked[0]["path"]
        confirmed = not is_ambiguous(ranked)
    else:
        raise PEError(f"no game program found in {folder}")
    exe_path = folder / chosen
    info, data = read_pe(exe_path)
    names = set(tree.dlls)
    unity = (exe_path.parent / "UnityPlayer.dll").exists() or (exe_path.parent / f"{exe_path.stem}_Data").is_dir() \
        or "unityplayer.dll" in names
    unreal = exe_path.name.lower().endswith("-shipping.exe") or "engine" in [x.lower() for x in exe_path.parts]
    engine = "Unity" if unity else "Unreal" if unreal else "Other"
    blob = data
    for extra in ("unityplayer.dll", "ovrplugin.dll"):
        p = tree.dlls.get(extra)
        if p:
            try:
                with open(p, "rb") as f:
                    blob += f.read(64 << 20)
            except OSError:
                pass
    # monolithic engines carry the Oculus code in the exe; modular Unreal builds ship it as a plugin DLL
    # (Engine/Plugins/Runtime/OculusRift/Binaries/Win64/<Game>-OculusRift-Win64-Shipping.dll)
    libovr = any(m in blob for m in LIBOVR_MARKERS) or \
        any(x in n for n in names for x in ("ovrplugin", "oculushmd", "libovr", "oculusrift"))
    openxr = "openxr_loader.dll" in info.imports or "openxr_loader.dll" in names or b"xrCreateInstance" in data
    # OpenVR/SteamVR: the game ships/links openvr_api. NOTE: an Unreal build carries SteamVR plugin binaries + strings
    # whether or not it uses them, so this alone does NOT prove the game runs on SteamVR — it only matters for routing
    # when the game has NO Oculus/LibOVR code (see frame_native below). We ignore the (universal in UE) IVRSystem/
    # VR_InitInternal string markers for exactly this reason.
    openvr = any(d in info.imports or d in names for d in OPENVR_DLLS)
    xr = "+".join(n for n, on in (("LibOVR", libovr), ("OpenXR", openxr), ("OpenVR", openvr)) if on) or "?"
    gfx_imports = set(info.imports)
    up = exe_path.parent / "UnityPlayer.dll"
    if up.exists():
        try:
            gfx_imports |= set(read_pe(up, 64 << 20)[0].imports)
        except (PEError, OSError, struct.error):
            pass
    graphics = next((g for dll, g in GRAPHICS if dll in gfx_imports), "D3D11 or unknown")
    # the Oculus Platform SDK (licence check, needs the Oculus app): shipped DLL, (delay-)imported by the exe, or by
    # a modular Unreal build's OnlineSubsystemOculus plugin DLL
    platform_imports = set(info.imports + info.delay_imports)
    for n, p in tree.dlls.items():
        if "onlinesubsystemoculus" in n:
            try:
                sub = read_pe(p, 64 << 20)[0]
                platform_imports |= set(sub.imports + sub.delay_imports)
            except (PEError, OSError, struct.error):
                pass
    platform_sdk = (any(n in names for n in PLATFORM_DLLS + PLATFORM_WRAPPERS)
                    or bool(platform_imports & set(PLATFORM_DLLS))
                    or b"ovr_PlatformInitializeWindows" in blob or b"ovr_Entitlement_GetIsViewerEntitled" in blob)
    # how to start it with VR on SteamVR / the Frame (see launch_mode())
    try:
        exe_dir = {p.name.lower() for p in exe_path.parent.iterdir()}
    except OSError:
        exe_dir = set()
    loader = sorted(n for n in exe_dir if n in LOADER_DLLS) if exe_dir & set(REVIVE_DLLS) else []
    mode, args, args_from = launch_mode(folder, chosen, engine, libovr, openxr, openvr, loader)
    overlay = vroverlay.detect(folder, exe_path, data, openvr, engine)  # a SteamVR overlay app (pcvr.vr_overlay)
    canonical_hint = next((part for part in exe_path.relative_to(folder).parts[:-1]
                           if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+){2,}", part)), None)  # Oculus Software dir name
    title = (manifest or {}).get("displayName") or clean_title(folder.name)
    return Analysis(
        package=game_id(folder, manifest), version=str((manifest or {}).get("version") or ""), label=title,
        abis=[info.machine], engine=engine, xr=xr, graphics=graphics, direct_vrapi=False, libs=sorted(names)[:200],
        launcher_activity=None, has_info_category=False, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
        oculus_os_classes=False, is_overport_output=False, debuggable=False,
        extra={"kind": "rift", "exe": chosen, "folder": str(folder), "exe_candidates": ranked[:12],
               "exe_confirmed": confirmed, "fingerprint": fingerprint(folder, chosen),
               "platform_sdk": platform_sdk, "openxr_native": openxr and not libovr,
               "openvr_native": openvr and not libovr,
               # frame_native = confidently runs on the Frame through wineopenxr (SteamVR), no Revive: only when there
               # is NO Oculus/LibOVR code (pure OpenXR or pure OpenVR). A LibOVR game is treated as Oculus (needs
               # Revive, PC only) unless a known-good catalog recipe overrides it (e.g. Rick and Morty, verified on
               # the Frame) — static analysis can't tell which runtime a dual-API Unreal build picks at runtime.
               # launch: "repack" (bundled Revive, started by its loader DLL: run the exe directly), "native"
               # (SteamVR/OpenXR: run the exe directly, with launch_args) or "revive" (inject FramePort's Revive)
               "launch": mode, "loader_dlls": loader, "launch_args": args, "launch_args_from": args_from,
               "frame_native": mode == "native",
               **overlay,
               "needs_revive": mode == "revive",
               "vr_found": xr != "?", "data_bytes": tree.size, "files": tree.files,
               # a LibRevive*.dll anywhere in the folder (see "launch" for whether the game actually starts it)
               "revive_bundled": any(n in names for n in ("librevive64.dll", "librevive32.dll", "librevivexr64.dll",
                                                          "librevivexr32.dll")),
               "oculus_app_id": (manifest or {}).get("appId"),
               "canonical_name": (manifest or {}).get("canonicalName") or canonical_hint},
    )


def _vd_to_steamvr(args: str) -> str:
    """Virtual Desktop launcher arguments → the SteamVR/OpenXR equivalent (Unreal's Oculus HMD module → OpenXR)."""
    return re.sub(r"(?i)(-hmd=)Oculus(XR)?HMD\b", r"\1OpenXR", args).strip()


def vd_args(folder: Path, exe_rel: str) -> str | None:
    """Arguments the repack's Virtual Desktop launcher (VD.bat next to the exe) passes to the game."""
    try:
        text = ((folder / exe_rel).parent / "VD.bat").read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r'"?' + re.escape(Path(exe_rel).name) + r'"?[ \t]*([^\r\n]*)', text, re.I)
    return m.group(1).strip() if m else None


def launch_mode(folder: Path, exe_rel: str, engine: str, libovr: bool, openxr: bool, openvr: bool,
                loader: list[str]) -> tuple[str, str, str]:
    """(mode, game arguments, where the arguments come from), from the game files alone.

    repack: the exe's folder has a bundled Revive and a proxy DLL (xinput*.dll) that starts it — the repack is
            already set up for SteamVR; launching it through another Revive breaks it (two Revives; e.g. the Oculus
            Platform entitlement check then fails). Run the exe directly.
    native: no Oculus code, or a dual-API build that also ships SteamVR/OpenXR support (Unreal's OpenVR/OpenXR
            plugin under Engine/Binaries/ThirdParty, Unity's openvr_api plugin): run the exe directly with the
            SteamVR/OpenXR runtime selected by arguments — the repack's VD.bat arguments with the Oculus module
            swapped for OpenXR, else Unreal's -hmd= / Unity's -vrmode.
    revive: Oculus-only (LibOVR/OVRPlugin): FramePort's Revive injector translates it."""
    if loader:
        return "repack", "", "the repack's own launcher (bundled Revive)"
    if not libovr and (openxr or openvr):
        return "native", "", ""
    if engine in ("Unreal", "Unity") and (openxr or openvr):
        vd = vd_args(folder, exe_rel)
        if vd:
            return "native", _vd_to_steamvr(vd), "VD.bat (Oculus HMD module → OpenXR)"
        if engine == "Unity":
            return "native", "-vrmode OpenVR", "Unity build with SteamVR support"
        return ("native", "-hmd=OpenXR", "Unreal build with the OpenXR plugin") if openxr else \
            ("native", "-hmd=SteamVR", "Unreal build with the SteamVR plugin")
    return "revive", "", ""


def is_rift(analysis: Analysis | dict) -> bool:
    extra = analysis.get("extra", {}) if isinstance(analysis, dict) else analysis.extra
    return (extra or {}).get("kind") == "rift"
