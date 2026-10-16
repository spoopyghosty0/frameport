"""Read unencrypted Unreal paks and DefaultEngine.ini without extracting game files (stdlib only)."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

# ------------------------------------------------------------------------------------------ Unreal paks
PAK_MAGIC = 0x5A6F12E1
# pak format version -> the Unreal versions that write it (when the exe doesn't name its version)
PAK_UE = {1: "4.0-4.2", 2: "4.0-4.2", 3: "4.3-4.15", 4: "4.16-4.19", 5: "4.20", 6: "4.20", 7: "4.21",
          8: "4.22-4.24", 9: "4.25", 10: "4.26", 11: "4.26-4.27"}


class Reader:
    def __init__(self, buf: bytes, pos: int = 0):
        self.buf, self.p = buf, pos

    def take(self, fmt: str):
        v = struct.unpack_from(fmt, self.buf, self.p)
        self.p += struct.calcsize(fmt)
        return v if len(v) > 1 else v[0]

    def fstring(self) -> str:
        n = self.take("<i")
        if n < 0:
            s = self.buf[self.p:self.p - 2 * n].decode("utf-16-le", "replace")
            self.p += -2 * n
        else:
            s = self.buf[self.p:self.p + n].decode("latin-1")
            self.p += n
        return s.rstrip("\0")


def entry_header_size(ver: int, compressed: bool, nblocks: int) -> int:
    """Size of the FPakEntry copy in front of a file's data (offset, sizes, method, hash, blocks, flags, block size)."""
    return 8 + 8 + 8 + 4 + 20 + (4 + 16 * nblocks if compressed else 0) + (1 + 4 if ver >= 3 else 0)


def legacy_entry(r: Reader, ver: int):
    """A full FPakEntry: (offset, compressed size, size, method, blocks, encrypted)."""
    off, csize, usize = r.take("<qqq")
    method = r.take("<i")
    r.p += 20
    blocks, enc = [], 0
    if ver >= 3:
        if method:
            nb = r.take("<i")
            blocks = [r.take("<qq") for _ in range(nb)]
        enc = r.buf[r.p]
        r.p += 1 + 4
    return off, csize, usize, method, blocks, enc


def encoded_entry(buf: bytes, pos: int, ver: int):
    """A v10+ bit-packed entry (FPakFile::DecodePakEntry); blocks relative to the entry like v7+ full entries."""
    r = Reader(buf, pos)
    v = r.take("<I")
    if v & 0x3f == 0x3f:
        r.take("<I")                               # compression block size (not needed to read)
    method = (v >> 23) & 0x3f
    off = r.take("<I" if v & (1 << 31) else "<Q")
    usize = r.take("<I" if v & (1 << 30) else "<Q")
    csize = r.take("<I" if v & (1 << 29) else "<Q") if method else usize
    enc = bool(v & (1 << 22))
    nb = (v >> 6) & 0xffff
    base = entry_header_size(ver, bool(method), nb)
    blocks = []
    if nb == 1 and not enc:
        blocks = [(base, base + csize)]
    elif nb:
        o = base
        for _ in range(nb):
            s = r.take("<I")
            blocks.append((o, o + s))
            o += (s + 15) & ~15 if enc else s
    return off, csize, usize, method, blocks, enc


def pak_entries(path: Path):
    """(version, mount point, {name: entry}, compression names) of an unencrypted UE4 pak, v1-v11; ValueError if the
    index can't be read (encrypted, unknown footer)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 512))
        tail = f.read()
        i = tail.rfind(struct.pack("<I", PAK_MAGIC), 0, len(tail) - 40)
        while i >= 0 and len(tail) - i not in (44, 44 + 4 * 32, 44 + 5 * 32):   # magic..end, 0/4/5 method names
            i = tail.rfind(struct.pack("<I", PAK_MAGIC), 0, i)
        if i < 0:
            raise ValueError("no pak footer")
        _, ver, ioff, isize = struct.unpack_from("<IIqq", tail, i)
        if ver >= 4 and i >= 1 and tail[i - 1] == 1:
            raise ValueError(f"pak v{ver}: index encrypted")
        names = [tail[j:j + 32].split(b"\0")[0].decode("latin-1") for j in range(i + 44, len(tail) - 31, 32)]
        if not 0 <= ioff < size or not 0 < isize <= size - ioff:
            raise ValueError(f"pak v{ver}: index outside the file")
        f.seek(ioff)
        r = Reader(f.read(isize))
        mount = r.fstring()
        count = r.take("<i")
        entries = {}
        if ver < 10:
            for _ in range(count):
                name = r.fstring()
                entries[name] = legacy_entry(r, ver)
            return ver, mount, entries, names
        r.take("<Q")                                   # path hash seed
        if r.take("<i"):
            r.take("<qq")
            r.p += 20                                  # path hash index: hashes only, no names
        if not r.take("<i"):
            raise ValueError(f"pak v{ver}: no full directory index (names hashed only)")
        fdi_off, fdi_size = r.take("<qq")
        r.p += 20
        enc_size = r.take("<i")
        encoded = r.buf[r.p:r.p + enc_size]
        r.p += enc_size
        files = [legacy_entry(r, ver) for _ in range(r.take("<i"))]
        f.seek(fdi_off)
        d = Reader(f.read(fdi_size))
    for _ in range(d.take("<i")):
        folder = d.fstring()
        for _ in range(d.take("<i")):
            name, loc = d.fstring(), d.take("<i")
            entries[(folder + name).lstrip("/")] = encoded_entry(encoded, loc, ver) if loc >= 0 else files[-loc - 1]
    return ver, mount, entries, names


def pak_read(path: Path, ver: int, entry, names: list[str] | None = None) -> bytes:
    off, csize, usize, method, blocks, enc = entry
    if enc:
        raise ValueError("entry encrypted")
    with open(path, "rb") as f:
        if not method:
            f.seek(off + entry_header_size(ver, False, 0))
            return f.read(usize)
        kind = (names[method - 1] if ver >= 8 and names and 0 < method <= len(names) else "zlib").lower()
        if kind not in ("zlib", "gzip"):
            raise ValueError(f"{kind}-compressed")
        out = b""
        for s, e in blocks:
            if ver >= 7:
                s, e = s + off, e + off
            f.seek(s)
            out += zlib.decompress(f.read(e - s), 31 if kind == "gzip" else 15)
        return out


CONFIG_KEYS = ("r.MobileMSAA", "r.MSAACount", "r.DefaultFeature.AntiAliasing", "r.ForwardShading",
               "r.useclusteredforward", "vr.InstancedStereo", "r.MobileHDR", "DefaultGraphicsRHI", "TargetedRHIs",
               "bUseVulkan")


def parse_engine_ini(text: str) -> dict:
    """The keys survey cares about; repeated keys (+TargetedRHIs=…) become lists."""
    want = {k.lower(): k for k in CONFIG_KEYS}
    vals: dict = {}
    for line in text.splitlines():
        k, _, v = line.strip().partition("=")
        key = want.get(k.lstrip("+-.!").lower())
        if not key:
            continue
        if key == "TargetedRHIs":
            vals.setdefault(key, [])
            if k.startswith("-"):
                vals[key] = [x for x in vals[key] if x != v]
            elif v not in vals[key]:
                vals[key].append(v)
        else:
            vals[key] = v
    return vals


def unreal_config(game_dir: Path) -> dict:
    """Pak format + rendering/AA settings from the project's DefaultEngine.ini (paks, else a loose file)."""
    paks = sorted(game_dir.rglob("*.pak"), key=lambda p: -p.stat().st_size)
    notes, ver_seen = [], None
    for pak in paks[:6]:
        try:
            ver, _, entries, names = pak_entries(pak)
        except (ValueError, OSError, struct.error, IndexError) as e:
            notes.append(f"{pak.name}: {e}")
            continue
        ver_seen = ver_seen or ver
        name = next((n for n in entries if n.endswith("Config/DefaultEngine.ini")), None)
        if not name:
            continue
        try:
            text = pak_read(pak, ver, entries[name], names).decode("utf-8-sig", "replace")
        except (ValueError, zlib.error, OSError) as e:
            notes.append(f"{pak.name}: DefaultEngine.ini {e}")
            continue
        return {"pak": f"v{ver}", "config": parse_engine_ini(text), "pak_notes": notes}
    loose = next((p for p in game_dir.rglob("DefaultEngine.ini") if p.parent.name == "Config"), None)
    if loose:
        text = loose.read_text("utf-8", "replace")
        return {"pak": f"v{ver_seen}" if ver_seen else "", "config": parse_engine_ini(text),
                "pak_notes": notes + ["config from a loose DefaultEngine.ini"]}
    return {"pak": f"v{ver_seen}" if ver_seen else "unreadable", "pak_notes": notes or ["no DefaultEngine.ini found"]}

