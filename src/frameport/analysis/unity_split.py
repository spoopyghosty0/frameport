"""Unity "split application binary" builds: the APK keeps the first scene, the rest of the game is a zip OBB
(main.<versionCode>.<package>.obb holding assets/bin/Data/...). Without it the game starts into nothing.

Two signs, either is enough (checked against 48 Unity APKs, 30 games: all 13 split builds flagged, none of the 17 full
builds, whose OBBs, where they have one, are asset bundles or sound banks):
  A  an XR-plugin build (Oculus XR Plugin / Unity OpenXR library) without assets/bin/Data/UnitySubsystems/ in the APK:
     the split moves the UnitySubsystems manifests into the OBB; full XR-plugin builds always have them (zip names only;
     the only sign for split builds with one scene, e.g. BONELAB)
  B  the build settings list more scenes than the APK has level files (a split keeps level0 only). The scene list
     comes from globalgamemanagers (loose, or the node of data.unity3d: only the bundle's head and the blocks up to it
     are read, a few hundred KB of a file that can be hundreds of MB)
"""
from __future__ import annotations

import lzma
import re
import struct
import zipfile

DATA = "assets/bin/Data/"
XR_PLUGIN_LIBS = ("libOculusXRPlugin.so", "libUnityOpenXR.so")
LEVEL = re.compile(re.escape(DATA) + r"level(\d+)(?:\.|$)")
MAX_NODE = 64 * 2**20  # globalgamemanagers is 0.1-3 MB; a larger node isn't read


def xr_plugin_without_subsystems(names: list[str]) -> bool:
    """Sign A (zip names only)."""
    xr_plugin = any(n.startswith("lib/") and n.rsplit("/", 1)[-1] in XR_PLUGIN_LIBS for n in names)
    return xr_plugin and not any(n.startswith(DATA + "UnitySubsystems/") for n in names)


def _cstr(f) -> bytes:
    out = bytearray()
    while (c := f.read(1)) not in (b"\0", b""):
        out += c
    return bytes(out)


def _decompress(data: bytes, kind: int, size: int) -> bytes:
    if kind == 0:
        return data
    if kind in (2, 3):  # LZ4 / LZ4HC
        import lz4.block

        return lz4.block.decompress(data, uncompressed_size=size)
    if kind == 1:  # LZMA: 5 bytes of properties, then raw LZMA1
        props, dict_size = data[0], struct.unpack("<I", data[1:5])[0]
        dec = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{
            "id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": props % 9, "lp": (props // 9) % 5,
            "pb": props // 45}])
        return dec.decompress(data[5:], size)
    raise ValueError(f"unknown compression {kind}")


class _Counting:
    """A forward-only stream that knows its position (zip member streams have no reliable tell())."""

    def __init__(self, f):
        self.f, self.pos = f, 0

    def read(self, n: int) -> bytes:
        b = self.f.read(n)
        self.pos += len(b)
        return b

    def align(self, n: int = 16) -> None:
        if self.pos % n:
            self.read(n - self.pos % n)


def bundle_node(stream, want: str = "globalgamemanagers") -> tuple[list[str], bytes | None]:
    """(node names, bytes of node `want`) of a UnityFS bundle, reading only up to the end of that node. ([], None)
    for anything else (old formats, block info at the end of the file)."""
    f = _Counting(stream)
    if f.read(8) != b"UnityFS\0":
        return [], None
    version = struct.unpack(">I", f.read(4))[0]
    _cstr(f), _cstr(f)
    _size, csize, usize, flags = struct.unpack(">qIII", f.read(20))
    if version >= 7:
        f.align()
    if flags & 0x80:  # block info at the end of the file
        return [], None
    info = _decompress(f.read(csize), flags & 0x3F, usize)
    off = 16  # data hash
    (nblocks,) = struct.unpack(">i", info[off:off + 4])
    off += 4
    blocks = []
    for _ in range(nblocks):
        blocks.append(struct.unpack(">IIH", info[off:off + 10]))
        off += 10
    (nnodes,) = struct.unpack(">i", info[off:off + 4])
    off += 4
    nodes = []
    for _ in range(nnodes):
        node_off, node_size, _flags = struct.unpack(">qqI", info[off:off + 20])
        off += 20
        end = info.index(b"\0", off)
        nodes.append((info[off:end].decode("utf-8", "replace"), node_off, node_size))
        off = end + 1
    if flags & 0x200:  # padding before the first block
        f.align()
    names = [n[0] for n in nodes]
    target = next((n for n in nodes if n[0] == want), None)
    if target is None or target[2] > MAX_NODE:
        return names, None
    need = target[1] + target[2]
    out = bytearray()
    for u, c, bf in blocks:
        if len(out) >= need:
            break
        out += _decompress(f.read(c), bf & 0x3F, u)
    return names, bytes(out[target[1]:need])


def build_scene_count(ggm: bytes) -> int | None:
    """Number of scenes in the build settings (BuildSettings.scenes) of a globalgamemanagers file."""
    try:
        import UnityPy

        for obj in UnityPy.load(ggm).objects:
            if obj.type.name == "BuildSettings":
                return len(obj.read_typetree().get("scenes") or [])
    except Exception:  # noqa: BLE001 - unreadable/unknown version: no answer
        return None
    return None


def more_scenes_than_levels(z: zipfile.ZipFile, names: list[str], ggm: bytes | None = None) -> bool:
    """Sign B. ggm: the loose globalgamemanagers when already read."""
    nameset = set(names)
    levels: set[int] = set()
    if ggm is None and DATA + "globalgamemanagers" in nameset:
        ggm = z.read(DATA + "globalgamemanagers")
    if ggm is not None:
        levels = {int(m.group(1)) for n in names if (m := LEVEL.match(n))}
    elif DATA + "data.unity3d" in nameset:
        try:
            with z.open(DATA + "data.unity3d") as raw:
                nodes, ggm = bundle_node(raw)
        except Exception:  # noqa: BLE001
            return False
        levels = {int(m.group(1)) for n in nodes if (m := re.fullmatch(r"level(\d+)", n))}
    if ggm is None:
        return False
    scenes = build_scene_count(ggm)
    return scenes is not None and scenes > len(levels)


def split_build(z: zipfile.ZipFile, names: list[str], ggm: bytes | None = None, deep: bool = True) -> bool:
    """The APK of a Unity split build (its OBB holds the rest of the game). Without `deep` only sign A."""
    return xr_plugin_without_subsystems(names) or (deep and more_scenes_than_levels(z, names, ggm))
