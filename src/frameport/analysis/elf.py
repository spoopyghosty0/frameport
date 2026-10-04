"""ELF helpers (pure Python: pyelftools for reading, capstone for disassembly, own DT_NEEDED writer)."""
from __future__ import annotations

import io
from functools import lru_cache

from elftools.elf.dynamic import DynamicSection
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

ELF_MAGIC = b"\x7fELF"


def is_elf(data: bytes) -> bool:
    return data[:4] == ELF_MAGIC


def _elf(data: bytes) -> ELFFile:
    return ELFFile(io.BytesIO(data))


def needed(data: bytes) -> list[str]:
    elf = _elf(data)
    for sec in elf.iter_sections():
        if isinstance(sec, DynamicSection):
            return [t.needed for t in sec.iter_tags() if t.entry.d_tag == "DT_NEEDED"]
    return []


def soname(data: bytes) -> str | None:
    elf = _elf(data)
    for sec in elf.iter_sections():
        if isinstance(sec, DynamicSection):
            for t in sec.iter_tags():
                if t.entry.d_tag == "DT_SONAME":
                    return t.soname
    return None


def dyn_symbols(data: bytes, defined: bool) -> set[str]:
    """Dynamic symbols that are defined (exports) or undefined (imports)."""
    if not is_elf(data):
        return set()
    return _dyn_symbols_cached(data, defined)


@lru_cache(maxsize=256)
def _dyn_symbols_cached(data: bytes, defined: bool) -> frozenset[str]:
    elf = _elf(data)
    sec = elf.get_section_by_name(".dynsym")
    out = set()
    if isinstance(sec, SymbolTableSection):
        for sym in sec.iter_symbols():
            if not sym.name or sym["st_info"]["type"] not in ("STT_FUNC", "STT_NOTYPE", "STT_OBJECT"):
                continue
            is_def = sym["st_shndx"] != "SHN_UNDEF"
            if is_def == defined:
                out.add(sym.name.split("@")[0])
    return frozenset(out)


def is_64bit(data: bytes) -> bool:
    return is_elf(data) and data[4] == 2


def load_segments(data: bytes) -> list[tuple[int, int, int]]:
    """(vaddr, file offset, filesz) for PT_LOAD segments."""
    elf = _elf(data)
    return [(s["p_vaddr"], s["p_offset"], s["p_filesz"]) for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]


def vaddr_to_offset(segments: list[tuple[int, int, int]], va: int) -> int | None:
    return next((va - v + o for v, o, sz in segments if v <= va < v + sz), None)


def text_instructions(data: bytes):
    """Yield (address, mnemonic, op_str) for executable sections (arm64 only)."""
    import capstone

    elf = _elf(data)
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
    md.skipdata = True
    for sec in elf.iter_sections():
        if sec["sh_type"] == "SHT_PROGBITS" and sec["sh_flags"] & 0x4:  # SHF_EXECINSTR
            code = sec.data()
            for ins in md.disasm_lite(code, sec["sh_addr"]):
                yield ins[0], ins[2], ins[3]


def replace_rodata_string(data: bytes, old: str, new: str) -> tuple[bytes, int]:
    """Replace NUL-terminated `old` with `new` (not longer; NUL-padded) inside .rodata only, in place (nothing moves;
    .dynstr, e.g. DT_NEEDED names, is untouched). Returns (data, number of replacements)."""
    if len(new) > len(old):
        raise ValueError("replacement must not be longer")
    sec = _elf(data).get_section_by_name(".rodata")
    if sec is None:
        return data, 0
    start, end = sec["sh_offset"], sec["sh_offset"] + sec["sh_size"]
    needle, repl = old.encode() + b"\0", new.encode().ljust(len(old) + 1, b"\0")
    buf, count, i = bytearray(data), 0, start
    while (i := buf.find(needle, i, end)) != -1:
        if i == start or buf[i - 1] == 0:  # a whole string, not the tail of a longer one
            buf[i:i + len(needle)] = repl
            count += 1
        i += len(needle)
    return bytes(buf), count


def hide_exports(data: bytes, names) -> tuple[bytes, list[str]]:
    """Make the exported functions `names` invisible to symbol lookup without removing them: their .dynsym entries
    become STB_LOCAL (name, value and section stay, nothing moves, the hash tables are untouched). Both bionic's and
    glibc's lookups only match GLOBAL/WEAK entries, so a lookup for such a name (a game's import, or dlsym on this
    library's handle) goes on to the next library in the dependency list; code that reads the library's own .dynsym
    (native/langpack) still finds the original address. Returns (data, names actually hidden); ELF64 only."""
    if not is_elf(data) or not is_64bit(data) or data[5] != 1:
        return data, []
    sec = _elf(data).get_section_by_name(".dynsym")
    if not isinstance(sec, SymbolTableSection):
        return data, []
    wanted = set(names)
    entsize, start = sec["sh_entsize"] or 24, sec["sh_offset"]
    buf, hidden = bytearray(data), []
    for i, sym in enumerate(sec.iter_symbols()):
        if (sym.name in wanted and sym["st_shndx"] != "SHN_UNDEF"
                and sym["st_info"]["bind"] in ("STB_GLOBAL", "STB_WEAK")):
            at = start + i * entsize + 4  # Elf64_Sym.st_info: binding in the high nibble, type in the low one
            buf[at] &= 0x0F
            hidden.append(sym.name)
    return bytes(buf), sorted(hidden)


def add_needed(data: bytes, library: str) -> bytes:
    """Add a DT_NEEDED entry in front of the existing ones (same effect as `patchelf --add-needed`; being first matters
    for symbol interposition, e.g. the GL shim must precede libEGL).

    Nothing already in the file moves: the extended .dynstr and a rebuilt .dynamic are appended in a new PT_LOAD
    (page aligned to 16 KiB), which reuses the PT_NOTE program header (bionic ignores notes in shared libraries).
    PT_DYNAMIC, DT_STRTAB/DT_STRSZ and the section headers are re-pointed. Handles ELF32 and ELF64 little-endian."""
    import struct

    if not is_elf(data) or data[5] != 1:
        raise ValueError("not a little-endian ELF")
    if library in needed(data):
        return data
    is64 = data[4] == 2
    buf = bytearray(data)
    if is64:
        eh = struct.unpack_from("<16sHHIQQQIHHHHHH", buf, 0)
        phoff, shoff, phentsize, phnum, shentsize, shnum = eh[5], eh[6], eh[9], eh[10], eh[11], eh[12]
        ph_fmt, dyn_fmt, sh_fmt = "<IIQQQQQQ", "<qQ", "<IIQQQQIIQQ"
    else:
        eh = struct.unpack_from("<16sHHIIIIIHHHHHH", buf, 0)
        phoff, shoff, phentsize, phnum, shentsize, shnum = eh[5], eh[6], eh[9], eh[10], eh[11], eh[12]
        ph_fmt, dyn_fmt, sh_fmt = "<IIIIIIII", "<iI", "<IIIIIIIIII"
    PT_LOAD, PT_DYNAMIC, PT_NOTE = 1, 2, 4
    DT_NULL, DT_NEEDED, DT_STRTAB, DT_STRSZ = 0, 1, 5, 10
    PAGE = 16384

    def ph(i):
        vals = list(struct.unpack_from(ph_fmt, buf, phoff + i * phentsize))
        if is64:  # p_type, p_flags, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align
            return {"type": vals[0], "flags": vals[1], "offset": vals[2], "vaddr": vals[3], "filesz": vals[5],
                    "memsz": vals[6], "align": vals[7]}
        # p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_flags, p_align
        return {"type": vals[0], "offset": vals[1], "vaddr": vals[2], "filesz": vals[4], "memsz": vals[5],
                "flags": vals[6], "align": vals[7]}

    def write_ph(i, p):
        if is64:
            vals = (p["type"], p["flags"], p["offset"], p["vaddr"], p["vaddr"], p["filesz"], p["memsz"], p["align"])
        else:
            vals = (p["type"], p["offset"], p["vaddr"], p["vaddr"], p["filesz"], p["memsz"], p["flags"], p["align"])
        struct.pack_into(ph_fmt, buf, phoff + i * phentsize, *vals)

    phs = [ph(i) for i in range(phnum)]
    dyn_i = next((i for i, p in enumerate(phs) if p["type"] == PT_DYNAMIC), None)
    note_i = next((i for i, p in enumerate(phs) if p["type"] == PT_NOTE), None)
    if dyn_i is None:
        raise ValueError("no PT_DYNAMIC")
    loads = [p for p in phs if p["type"] == PT_LOAD]
    segs = [(p["vaddr"], p["offset"], p["filesz"]) for p in loads]

    def va2off(va):
        off = vaddr_to_offset(segs, va)
        if off is None:
            raise ValueError(f"vaddr {va:#x} not in a loaded segment")
        return off

    # read dynamic entries
    dyn = phs[dyn_i]
    esz = struct.calcsize(dyn_fmt)
    entries = []
    for k in range(dyn["filesz"] // esz):
        tag, val = struct.unpack_from(dyn_fmt, buf, dyn["offset"] + k * esz)
        entries.append([tag, val])
        if tag == DT_NULL:
            break
    tags = {t: v for t, v in entries}
    strtab_va, strsz = tags[DT_STRTAB], tags[DT_STRSZ]
    old_str = bytes(buf[va2off(strtab_va): va2off(strtab_va) + strsz])
    new_str = old_str + library.encode() + b"\0"
    name_off = len(old_str)

    body_entries = [e for e in entries if e[0] != DT_NULL]
    first_needed = next((k for k, e in enumerate(body_entries) if e[0] == DT_NEEDED), 0)
    body_entries.insert(first_needed, [DT_NEEDED, name_off])
    body_entries += [[DT_NULL, 0]] * 2

    file_end = (len(buf) + PAGE - 1) // PAGE * PAGE
    va_end = max(p["vaddr"] + p["memsz"] for p in loads)
    va_start = (va_end + PAGE - 1) // PAGE * PAGE  # both page multiples, so vaddr ≡ offset (mod PAGE)
    PT_PHDR = 6
    relocate = note_i is None  # no spare header: move the program header table into the new segment
    new_phnum = phnum + (2 if relocate and not any(p["type"] == PT_PHDR for p in phs) else 1 if relocate else 0)
    phdr_bytes = new_phnum * phentsize if relocate else 0
    str_off = (phdr_bytes + 15) // 16 * 16
    dyn_off = (str_off + len(new_str) + 15) // 16 * 16
    for e in body_entries:
        if e[0] == DT_STRTAB:
            e[1] = va_start + str_off
        elif e[0] == DT_STRSZ:
            e[1] = len(new_str)
    dyn_bytes = b"".join(struct.pack(dyn_fmt, t, v) for t, v in body_entries)
    blob_len = dyn_off + len(dyn_bytes)
    new_load = {"type": PT_LOAD, "flags": 6, "offset": file_end, "vaddr": va_start, "filesz": blob_len,
                "memsz": blob_len, "align": PAGE}  # PF_R|PF_W
    new_dyn = {**dyn, "offset": file_end + dyn_off, "vaddr": va_start + dyn_off, "filesz": len(dyn_bytes),
               "memsz": len(dyn_bytes)}

    table = [dict(p) for p in phs]
    table[dyn_i] = new_dyn
    if relocate:
        phdr_entry = {"type": PT_PHDR, "flags": 4, "offset": file_end, "vaddr": va_start, "filesz": phdr_bytes,
                      "memsz": phdr_bytes, "align": 8}
        existing = next((i for i, p in enumerate(table) if p["type"] == PT_PHDR), None)
        if existing is not None:
            table[existing] = phdr_entry
        else:
            table.insert(0, phdr_entry)  # PT_PHDR must precede the loadable segments
        table.append(new_load)
    else:
        table[note_i] = new_load
    # PT_LOAD entries must stay sorted by vaddr (bionic); the new one has the highest vaddr.
    load_slots = [i for i, p in enumerate(table) if p["type"] == PT_LOAD]
    for i, p in zip(load_slots, sorted((table[i] for i in load_slots), key=lambda p: p["vaddr"]), strict=True):
        table[i] = p

    buf += b"\0" * (file_end - len(buf))
    buf += b"\0" * blob_len
    buf[file_end + str_off:file_end + str_off + len(new_str)] = new_str
    buf[file_end + dyn_off:file_end + dyn_off + len(dyn_bytes)] = dyn_bytes
    if relocate:
        phoff = file_end
        if is64:
            struct.pack_into("<Q", buf, 32, phoff)
            struct.pack_into("<H", buf, 56, new_phnum)
        else:
            struct.pack_into("<I", buf, 28, phoff)
            struct.pack_into("<H", buf, 44, new_phnum)
        phnum = new_phnum
    for i, p in enumerate(table):
        write_ph(i, p)

    # section headers: point .dynamic / .dynstr at the new copies (for tools; the loader uses program headers)
    if shoff and shnum:
        shstrndx = eh[13]
        def sh(i):
            return list(struct.unpack_from(sh_fmt, buf, shoff + i * shentsize))
        names_hdr = sh(shstrndx)
        names_off = names_hdr[4]
        def sec_name(h):
            end = buf.index(b"\0", names_off + h[0])
            return bytes(buf[names_off + h[0]:end]).decode()
        for i in range(shnum):
            h = sh(i)
            n = sec_name(h)
            if n == ".dynstr":
                h[3], h[4], h[5] = va_start + str_off, file_end + str_off, len(new_str)
            elif n == ".dynamic":
                h[3], h[4], h[5] = va_start + dyn_off, file_end + dyn_off, len(dyn_bytes)
            else:
                continue
            struct.pack_into(sh_fmt, buf, shoff + i * shentsize, *h)
    result = bytes(buf)
    got = needed(result)
    if not got or got[0] != library:
        raise RuntimeError(f"add_needed({library}) produced NEEDED={got}")
    return result
