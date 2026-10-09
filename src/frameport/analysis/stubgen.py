"""Generate a tiny Android shared library whose exported functions all `return 0` (no compiler needed).

Used for Meta platform functions (ovr_*) that OVRPort's platform loader lacks: without them the game fails with
UnsatisfiedLinkError / dlopen errors. The layout follows what bionic requires: section headers present (.dynsym,
.dynstr, .hash, .text, .dynamic, .shstrtab), PT_LOAD segments 16 KiB aligned, PT_DYNAMIC matching the .dynamic
section, DT_HASH, DT_SONAME and PT_GNU_STACK non-executable.
"""
from __future__ import annotations

import struct

PAGE = 16384
EMPTY_STRING_AT = 9  # e_ident padding (bytes 9-15 are zero), mapped with the text segment at vaddr 0


def _elf_hash(name: bytes) -> int:
    h = 0
    for c in name:
        h = ((h << 4) + c) & 0xFFFFFFFF
        g = h & 0xF0000000
        if g:
            h ^= g >> 24
        h &= ~g & 0xFFFFFFFF
    return h


def build_stub_library(symbols: list[str], soname: str = "libovrstubs.so", abi: str = "arm64-v8a",
                       result: int = 0) -> bytes:
    """`result`: what every function returns (a 32-bit int; arm64 only for values other than 0), e.g. OVRPlugin's
    ovrpFailure (-1000) for stand-ins whose callers must not trust untouched output arguments."""
    is64 = abi == "arm64-v8a"
    symbols = sorted(set(symbols))
    if result and not (is64 and -0x10000 <= result <= 0xFFFF):
        raise ValueError(f"stub result {result} not supported for {abi}")
    if is64:
        machine, ehsize, phentsize, shentsize, symsize, dynsize = 183, 64, 56, 64, 24, 16  # EM_AARCH64
        body = struct.pack("<II", 0xD2800000, 0xD65F03C0)  # mov x0, #0 ; ret
        if result > 0:  # movz w0, #result ; ret
            body = struct.pack("<II", 0x52800000 | result << 5, 0xD65F03C0)
        elif result < 0:  # movn w0, #~result ; ret  (w0 = result)
            body = struct.pack("<II", 0x12800000 | (~result & 0xFFFF) << 5, 0xD65F03C0)
    else:
        machine, ehsize, phentsize, shentsize, symsize, dynsize = 40, 52, 32, 40, 16, 8  # EM_ARM
        body = struct.pack("<II", 0xE3A00000, 0xE12FFF1E)  # mov r0, #0 ; bx lr

    # ---- string tables
    dynstr = bytearray(b"\0")
    def add_str(s: str) -> int:
        off = len(dynstr)
        dynstr.extend(s.encode() + b"\0")
        return off
    soname_off = add_str(soname)
    name_offs = [add_str(s) for s in symbols]
    shstr = bytearray(b"\0")
    sec_names = {}
    for n in (".dynsym", ".dynstr", ".hash", ".text", ".dynamic", ".shstrtab"):
        sec_names[n] = len(shstr)
        shstr.extend(n.encode() + b"\0")

    nsyms = len(symbols) + 1
    phnum = 4  # LOAD (R+X), LOAD (RW), DYNAMIC, GNU_STACK
    off = ehsize + phnum * phentsize
    def align(x, a):
        return (x + a - 1) // a * a
    dynsym_off = align(off, 8)
    dynstr_off = dynsym_off + nsyms * symsize
    hash_off = align(dynstr_off + len(dynstr), 4)
    nbucket = max(1, len(symbols) // 2 + 1)
    hash_size = 4 * (2 + nbucket + nsyms)
    text_off = align(hash_off + hash_size, 16)
    text_size = 8 * len(symbols)
    seg1_end = text_off + text_size
    dyn_off = align(seg1_end, PAGE)  # own page, RW
    dyn_entries = [(5, dynstr_off), (6, dynsym_off), (10, len(dynstr)), (11, symsize), (4, hash_off),
                   (14, soname_off), (0, 0)]
    dyn_bytes = b"".join(struct.pack("<qQ" if is64 else "<iI", t, v) for t, v in dyn_entries)
    shstr_off = dyn_off + len(dyn_bytes)
    sh_off = align(shstr_off + len(shstr), 8)

    # ---- symbols
    SHN_TEXT = 4
    syms = bytearray(b"\0" * symsize)
    for i, name_off in enumerate(name_offs):
        value = text_off + 8 * i
        info = (1 << 4) | 2  # STB_GLOBAL, STT_FUNC
        if is64:
            syms += struct.pack("<IBBHQQ", name_off, info, 0, SHN_TEXT, value, 8)
        else:
            syms += struct.pack("<IIIBBH", name_off, value, 8, info, 0, SHN_TEXT)
    buckets, chains = [0] * nbucket, [0] * nsyms
    for idx in range(1, nsyms):
        b = _elf_hash(symbols[idx - 1].encode()) % nbucket
        chains[idx] = buckets[b]
        buckets[b] = idx
    hash_bytes = struct.pack(f"<II{nbucket}I{nsyms}I", nbucket, nsyms, *buckets, *chains)

    # ---- headers
    PF_R, PF_W, PF_X = 4, 2, 1
    phdrs = [
        (1, PF_R | PF_X, 0, 0, seg1_end, seg1_end, PAGE),
        (1, PF_R | PF_W, dyn_off, dyn_off, len(dyn_bytes), len(dyn_bytes), PAGE),
        (2, PF_R | PF_W, dyn_off, dyn_off, len(dyn_bytes), len(dyn_bytes), 8),
        (0x6474E551, PF_R | PF_W, 0, 0, 0, 0, 16),  # PT_GNU_STACK
    ]
    ph_bytes = b""
    for typ, flags, o, va, fsz, msz, al in phdrs:
        if is64:
            ph_bytes += struct.pack("<IIQQQQQQ", typ, flags, o, va, va, fsz, msz, al)
        else:
            ph_bytes += struct.pack("<IIIIIIII", typ, o, va, va, fsz, msz, flags, al)
    SHT_PROGBITS, SHT_STRTAB, SHT_HASH, SHT_DYNAMIC, SHT_DYNSYM = 1, 3, 5, 6, 11
    SHF_WRITE, SHF_ALLOC, SHF_EXEC = 1, 2, 4
    sections = [
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        (sec_names[".dynsym"], SHT_DYNSYM, SHF_ALLOC, dynsym_off, dynsym_off, nsyms * symsize, 2, 1, 8, symsize),
        (sec_names[".dynstr"], SHT_STRTAB, SHF_ALLOC, dynstr_off, dynstr_off, len(dynstr), 0, 0, 1, 0),
        (sec_names[".hash"], SHT_HASH, SHF_ALLOC, hash_off, hash_off, hash_size, 1, 0, 4, 4),
        (sec_names[".text"], SHT_PROGBITS, SHF_ALLOC | SHF_EXEC, text_off, text_off, text_size, 0, 0, 16, 0),
        (sec_names[".dynamic"], SHT_DYNAMIC, SHF_ALLOC | SHF_WRITE, dyn_off, dyn_off, len(dyn_bytes), 2, 0, 8, dynsize),
        (sec_names[".shstrtab"], SHT_STRTAB, 0, 0, shstr_off, len(shstr), 0, 0, 1, 0),
    ]
    sh_bytes = b""
    for name, typ, flags, addr, o, size, link, info, al, entsize in sections:
        if is64:
            sh_bytes += struct.pack("<IIQQQQIIQQ", name, typ, flags, addr, o, size, link, info, al, entsize)
        else:
            sh_bytes += struct.pack("<IIIIIIIIII", name, typ, flags, addr, o, size, link, info, al, entsize)
    ident = b"\x7fELF" + bytes([2 if is64 else 1, 1, 1, 0]) + b"\0" * 8
    if is64:
        eh = ident + struct.pack("<HHIQQQIHHHHHH", 3, machine, 1, 0, ehsize, sh_off, 0, ehsize, phentsize, phnum,
                                 shentsize, len(sections), 6)
    else:
        eh = ident + struct.pack("<HHIIIIIHHHHHH", 3, machine, 1, 0, ehsize, sh_off, 0x05000000, ehsize, phentsize,
                                 phnum, shentsize, len(sections), 6)  # EF_ARM_EABI_VER5

    out = bytearray(sh_off + len(sh_bytes))
    out[0:len(eh)] = eh
    out[ehsize:ehsize + len(ph_bytes)] = ph_bytes
    out[dynsym_off:dynsym_off + len(syms)] = syms
    out[dynstr_off:dynstr_off + len(dynstr)] = dynstr
    out[hash_off:hash_off + len(hash_bytes)] = hash_bytes
    text = bytearray(body * len(symbols))
    if is64:  # *_ToString answers "" instead of NULL (a caller may build a string from it): adr x0, <a zero byte>
        for i, name in enumerate(symbols):
            if name.endswith("_ToString"):
                pc = text_off + 8 * i
                imm = (EMPTY_STRING_AT - pc) & 0x1FFFFF  # 21-bit signed offset; byte 9 of e_ident is always 0
                text[8 * i:8 * i + 8] = struct.pack("<II", 0x10000000 | (imm & 3) << 29 | (imm >> 2) << 5, 0xD65F03C0)
    out[text_off:text_off + text_size] = text
    out[dyn_off:dyn_off + len(dyn_bytes)] = dyn_bytes
    out[shstr_off:shstr_off + len(shstr)] = shstr
    out[sh_off:] = sh_bytes
    return bytes(out)
