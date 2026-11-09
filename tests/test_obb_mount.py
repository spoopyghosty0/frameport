"""frame.obb_mount_emul (GitHub #308, GRID Legends): games that mount their OBBs as FAT disk images. The agent unpacks
each image into a folder with the image's name (agent unpack_obb), the dex rewrite makes the game's OBB helper report
MOUNTED at once and use that folder as the mount point."""
from __future__ import annotations

import importlib.util
import json
import os
import struct
from pathlib import Path

import pytest

from frameport.apk.dex import Dex
from frameport.patches.frame.obb_mount import HANDLERS, ObbMountEmulation, handler_classes, patch_dex
from frameport.validate.triage import triage

AGENT = Path(__file__).resolve().parents[1] / "agent" / "frameport_agent.py"
H = "Landroidutils/OBBHandler;"


def _uleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def build_dex(methods):
    """A minimal dex: methods = (class, name, shorty, registers, ins, code units or None). Types in a shorty: V void,
    I int, L java/lang/String (enough for these methods)."""
    strings: list[str] = []

    def s(x):
        if x not in strings:
            strings.append(x)
        return strings.index(x)

    types: list[int] = []

    def t(x):
        i = s(x)
        if i not in types:
            types.append(i)
        return types.index(i)

    names = {"V": "V", "I": "I", "L": "Ljava/lang/String;"}
    protos: list[tuple[int, int, tuple[int, ...]]] = []

    def p(shorty):
        key = (s(shorty), t(names[shorty[0]]), tuple(t(names[c]) for c in shorty[1:]))
        if key not in protos:
            protos.append(key)
        return protos.index(key)

    mids = [(t(c), p(sh), s(n)) for c, n, sh, *_ in methods]
    header = 0x70
    off = header + 4 * len(strings) + 4 * len(types) + 12 * len(protos) + 8 * len(mids)
    classes = sorted({m[0] for m in methods if m[5] is not None})
    sid_off = header
    tid_off = sid_off + 4 * len(strings)
    pid_off = tid_off + 4 * len(types)
    mid_off = pid_off + 12 * len(protos)
    cdef_off = off
    off += 32 * len(classes)
    data = bytearray()

    def place(blob, align=1):
        while (off + len(data)) % align:
            data.append(0)
        pos = off + len(data)
        data.extend(blob)
        return pos

    str_offs = [place(_uleb(len(x)) + x.encode() + b"\0") for x in strings]
    plists = [place(struct.pack("<I", len(ps)) + b"".join(struct.pack("<H", x) for x in ps), 4) if ps else 0
              for _s, _r, ps in protos]
    code_offs = {}
    for i, (_c, _n, _sh, regs, ins, code) in enumerate(methods):
        if code is not None:
            item = struct.pack("<4HII", regs, ins, 2, 0, 0, len(code)) + struct.pack(f"<{len(code)}H", *code)
            code_offs[i] = place(item, 4)
    class_data = {}
    for c in classes:
        idxs = [i for i, m in enumerate(methods) if m[0] == c and m[5] is not None]
        blob = _uleb(0) + _uleb(0) + _uleb(len(idxs)) + _uleb(0)  # direct (static) methods
        prev = 0
        for i in idxs:
            blob += _uleb(i - prev) + _uleb(9) + _uleb(code_offs[i])  # public static
            prev = i
        class_data[c] = place(blob)
    out = bytearray(header)
    out[:8] = b"dex\n035\0"
    struct.pack_into("<10I", out, 56, len(strings), sid_off, len(types), tid_off, len(protos), pid_off, 0, 0,
                     len(mids), mid_off)
    struct.pack_into("<2I", out, 96, len(classes), cdef_off)
    out += b"".join(struct.pack("<I", o) for o in str_offs)
    out += b"".join(struct.pack("<I", i) for i in types)
    out += b"".join(struct.pack("<3I", a, b, pl) for (a, b, _ps), pl in zip(protos, plists))
    out += b"".join(struct.pack("<HHI", *m) for m in mids)
    out += b"".join(struct.pack("<8I", t(c), 1, 0, 0, 0, 0, class_data[c], 0) for c in classes)
    out += data
    return bytes(out)


# GRID Legends' OBBHandler, first instructions as in 1.0.0.41 (sget-object; if-nez; sget-object; const-string ...)
MOUNT = [0x0062, 0x1C, 0x0039, 0x0E, 0x0062, 0x19, 0x011A, 0x47CD, 0x000E]
MOUNTED_PATH = [0x0062, 0x1C, 0x0039, 0x09, 0x011A, 0x2D1A, 0x0011]


def _dex() -> bytes:
    return build_dex([(H, "onMountOBBStateChanged", "VIL", 0, 0, None),     # 0: native
                      (H, "getOBBFullPath", "LL", 2, 1, [0x0000, 0x0011]),  # 1
                      (H, "mountOBB", "VL", 5, 1, MOUNT),                   # 2: name in v4
                      (H, "getMountedOBBFullPath", "LL", 2, 1, MOUNTED_PATH),  # 3: name in v1
                      (H, "unmountOBB", "VL", 5, 1, [0x000E])])


def _code(dex: Dex, name: str) -> list[int]:
    off = dex.code_items(H, name)[0]
    n = struct.unpack_from("<I", dex.data, off + 12)[0]
    return list(struct.unpack_from(f"<{n}H", dex.data, off + 16))


def test_handler_rewrite_in_place():
    raw = _dex()
    assert handler_classes(raw + b"mountObb") == [H]
    dex = Dex(raw)
    assert dex.shorty(0) == "VIL" and dex.shorty(2) == "VL"
    assert sorted(patch_dex(dex)) == ["androidutils.OBBHandler.getMountedOBBFullPath",
                                      "androidutils.OBBHandler.mountOBB"]
    # const/4 v0, #1; invoke-static {v0, v4}, onMountOBBStateChanged; return-void; nop (to the boundary)
    assert _code(dex, "mountOBB") == [0x1012, 0x2071, 0, 0x0040, 0x000E, 0x0000] + MOUNT[6:]
    # invoke-static {v1}, getOBBFullPath; move-result-object v0; return-object v0; nop
    assert _code(dex, "getMountedOBBFullPath") == [0x1071, 1, 0x0001, 0x000C, 0x0011, 0x0000, 0x0011]
    assert _code(dex, "unmountOBB") == [0x000E]
    out = dex.finish()
    assert len(out) == len(raw) and patch_dex(Dex(out)) == []


def test_invoke_static_encoding():
    assert Dex.invoke_static(0xCE, [0, 1]) == [0x2071, 0xCE, 0x0010]  # GRID's own access$000
    with pytest.raises(ValueError):
        Dex.invoke_static(1, [16])


def test_detect_triage_and_handlers():
    class A:
        extra = {"obb_mount": [H]}
    assert set(HANDLERS) == {H}
    assert ObbMountEmulation().detect(A()).recommended
    A.extra = {"obb_mount": ["other"]}
    assert ObbMountEmulation().applies(A()) and ObbMountEmulation().detect(A()) is None
    log = ("10-15 20:00:01.000  900  950 E AndroidRuntime: FATAL EXCEPTION: main\n"
           "10-15 20:00:01.000  900  950 E AndroidRuntime: java.lang.RuntimeException: Can't mount OBB "
           "DataSet0.obb (state=21)\n")
    r = triage(log, "EXITED", "com.codemasters.gridlegends")
    assert [f.id for f in r.findings] == ["obb-mount-failed"]
    assert r.findings[0].suggest == ["frame.obb_mount_emul"]


# ---------------------------------------------------------------------------------------------------- agent side
def load_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("frameport_agent", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _short(name: str) -> bytes:
    base, _, ext = name.upper().partition(".")
    return base[:8].ljust(8).encode() + ext[:3].ljust(3).encode()


def _lfn_entries(name: str, short: bytes) -> list[bytes]:
    csum = 0
    for b in short:
        csum = (((csum & 1) << 7) + (csum >> 1) + b) & 0xFF
    chars = [name[i] for i in range(len(name))]
    pieces = [chars[i:i + 13] for i in range(0, len(chars), 13)]
    out = []
    for n, piece in enumerate(pieces, 1):
        u = "".join(piece).encode("utf-16-le")
        if len(piece) < 13:
            u += b"\0\0" + b"\xff\xff" * (12 - len(piece))
        e = bytearray(32)
        e[0] = n | (0x40 if n == len(pieces) else 0)
        e[1:11], e[11], e[13], e[14:26], e[28:32] = u[0:10], 0x0F, csum, u[10:22], u[22:26]
        out.append(bytes(e))
    return list(reversed(out))


def make_fat32(path: Path, tree: dict, cluster=1024, footer_flags=None, fragment=False):
    """A small FAT32 image (fat32lib layout: 32 reserved sectors, 2 FATs) with long names; tree = {name: bytes |
    dict}. fragment=True interleaves files' clusters (non-contiguous chains)."""
    bps, spc = 512, cluster // 512
    nclusters = 70000  # FAT32 needs >= 65525 clusters
    fat_sectors = (nclusters + 2) * 4 // bps + 1
    res, nfat = 32, 2
    data_off = (res + nfat * fat_sectors) * bps
    fat = [0x0FFFFFF8, 0x0FFFFFFF] + [0] * nclusters
    clusters: dict[int, bytes] = {}
    nxt = [3]

    def alloc(blob: bytes) -> int:
        n = max(1, (len(blob) + cluster - 1) // cluster)
        step = 2 if fragment else 1
        cs = [nxt[0] + i * step for i in range(n)]
        nxt[0] = cs[-1] + 1
        for i, c in enumerate(cs):
            fat[c] = cs[i + 1] if i + 1 < n else 0x0FFFFFFF
            clusters[c] = blob[i * cluster:(i + 1) * cluster].ljust(cluster, b"\0")
        return cs[0]

    def directory(entries: dict, first: int | None = None) -> int:
        raw = bytearray()
        for i, (name, content) in enumerate(entries.items()):
            short = _short(name)[:6] + b"~" + str(i % 10).encode() + _short(name)[8:]
            if isinstance(content, dict):
                cl = directory(content)
                attr, size = 0x10, 0
            else:
                cl = alloc(content) if content else 0
                attr, size = 0x20, len(content)
            raw += b"".join(_lfn_entries(name, short))
            e = bytearray(32)
            e[0:11], e[11] = short, attr
            struct.pack_into("<H", e, 26, cl & 0xFFFF)
            struct.pack_into("<H", e, 20, cl >> 16)
            struct.pack_into("<I", e, 28, size)
            raw += e
        raw += b"\xe5" + b"\0" * 31  # a deleted entry
        if first is not None:
            fat[first] = 0x0FFFFFFF
            clusters[first] = bytes(raw).ljust(cluster, b"\0")
            return first
        return alloc(bytes(raw) + b"\0" * 32)

    directory(tree, first=2)
    total = data_off // bps + nclusters * spc
    bs = bytearray(512)
    bs[0:3], bs[3:11] = b"\xeb\x3c\x90", b"fat32lib"
    struct.pack_into("<HBHBHHBH", bs, 11, bps, spc, res, nfat, 0, 0, 0xF8, 0)
    struct.pack_into("<II", bs, 32, total, fat_sectors)
    struct.pack_into("<I", bs, 44, 2)
    bs[82:90], bs[510:512] = b"FAT32   ", b"\x55\xaa"
    fat_bytes = b"".join(struct.pack("<I", v) for v in fat).ljust(fat_sectors * bps, b"\0")
    with open(path, "wb") as f:
        f.write(bs)
        f.write(b"\0" * (res * bps - 512))
        f.write(fat_bytes * nfat)
        for c in sorted(clusters):
            f.seek(data_off + (c - 2) * cluster)
            f.write(clusters[c])
        f.truncate(total * bps)
        f.seek(0, 2)
        if footer_flags is not None:
            pkg = b"com.example.game"
            ft = struct.pack("<III", 1, 51, footer_flags) + b"\0" * 8 + struct.pack("<I", len(pkg)) + pkg
            f.write(ft + struct.pack("<II", len(ft), 0x01059983))


TREE = {"index.txt": b"data/x\n", "data": {"Mixed_Case.bk2": b"B" * 3000, "empty.bin": b"",
                                          "deep": {"a.erp": bytes(range(256)) * 9}}}


def _check(root: Path):
    assert (root / "index.txt").read_bytes() == b"data/x\n"
    assert (root / "data/Mixed_Case.bk2").read_bytes() == b"B" * 3000
    assert (root / "data/empty.bin").read_bytes() == b""
    assert (root / "data/deep/a.erp").read_bytes() == bytes(range(256)) * 9


@pytest.mark.parametrize("fragment", [False, True])
def test_fat_reader_unpacks_long_names_and_chains(monkeypatch, tmp_path, fragment):
    a = load_agent(monkeypatch, tmp_path)
    img = tmp_path / "DataSet0.obb"
    make_fat32(img, TREE, footer_flags=0, fragment=fragment)
    assert a.is_obb_image(str(img)) and a.obb_footer(str(img))["package"] == "com.example.game"
    assert a.unpack_obb_image(str(img), str(tmp_path / "out")) == (4, 7 + 3000 + 2304)
    _check(tmp_path / "out")


def test_encrypted_and_non_fat_images_are_refused(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    img = tmp_path / "x.obb"
    make_fat32(img, TREE, footer_flags=1)
    with pytest.raises(a.AgentError, match="encrypted"):
        a.unpack_obb_image(str(img), str(tmp_path / "out"))
    zipped = tmp_path / "main.1.com.x.obb"
    zipped.write_bytes(b"PK\3\4" + b"\0" * 600)
    assert not a.is_obb_image(str(zipped))


def test_unsafe_names_are_refused(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    img = tmp_path / "x.obb"
    make_fat32(img, {"..\\evil": b"x"})
    with pytest.raises(a.AgentError):
        a.unpack_obb_image(str(img), str(tmp_path / "out"))
    assert not (tmp_path / "evil").exists()


def test_unpack_command_and_manifest(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    base = tmp_path / "game"
    obb = base / "lepton-app" / "obb"
    obb.mkdir(parents=True)
    make_fat32(obb / "DataSet0.obb", TREE, footer_flags=0)
    (obb / "plain.obb").write_bytes(b"PK\3\4 not an image")
    size = (obb / "DataSet0.obb").stat().st_size
    monkeypatch.setattr(a, "deployment", lambda pkg: {"base": str(base), "appid": 1, "title": "G"})
    monkeypatch.setattr(a, "container_running", lambda appid: False)
    assert a.cmd_unpack_obb({"package": "com.example.game"}) == {"pending": ["DataSet0.obb"]}
    done = a.cmd_unpack_obb({"package": "com.example.game", "image": "DataSet0.obb"})
    assert done["files"] == 4
    assert (obb / "DataSet0.obb").is_dir()
    _check(obb / "DataSet0.obb")
    assert json.loads((obb / "DataSet0.obb" / a.OBB_MARKER).read_text())["size"] == size
    # the PC's "already on the Frame" check and finalize's data check see the image, not its files
    assert a.obb_manifest(str(obb)) == {"DataSet0.obb": size, "plain.obb": 17}
    assert a.cmd_unpack_obb({"package": "com.example.game"}) == {"pending": []}
    with pytest.raises(a.AgentError):
        a.cmd_unpack_obb({"package": "com.example.game", "image": "plain.obb"})


def test_interrupted_unpack_is_finished_or_dropped(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    obb = tmp_path / "obb"
    done = obb / ("DataSet1.obb" + a.OBB_UNPACKING)  # complete, image already removed: only the rename was missing
    done.mkdir(parents=True)
    (done / a.OBB_MARKER).write_text(json.dumps({"size": 5}))
    half = obb / ("DataSet2.obb" + a.OBB_UNPACKING)  # no marker: dropped (the image is still there)
    half.mkdir()
    (half / "f").write_bytes(b"x")
    a.finish_obb_unpacks(str(obb))
    assert sorted(os.listdir(obb)) == ["DataSet1.obb"]
    assert a.obb_manifest(str(obb)) == {"DataSet1.obb": 5}
