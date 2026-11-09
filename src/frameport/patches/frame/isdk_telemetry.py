"""Meta Interaction SDK telemetry that crashes without Quest's OS SDK (GitHub #546, Ghost Signal: A Stellaris Game).

libInteractionSdk.so (Meta's Interaction SDK, Unity and Unreal) starts its telemetry the first time the game records an
interaction value: `interaction_sdk::v1::telemetry::initializeTelemetry()` builds a `TelemetrySender`, whose
constructor asks Quest's `libossdk` for a telemetry handler. The Frame has no libossdk (`OsSdkLoader: libossdk not
found`), the handler stays null and the constructor dereferences it (SIGSEGV in `TelemetrySender::TelemetrySender(...)`
called from `initializeTelemetry()+140`), before the title screen.

The library has its own off switch, the global `interaction_sdk::v1::telemetry::telemetryDisabled`: every telemetry
entry point tests it first and returns an error code without touching the sender. `initializeTelemetry()` begins with
that test:

    adrp xN, page ; ldr xN, [xN, #slot]   // &telemetryDisabled from the GOT
    ldrb wM, [xN] ; cbz wM, <initialise>
    mov  w0, #-2  ; b <return>            // "telemetry disabled"

The patch replaces the `ldrb` + `cbz` pair with `mov w16, #1` + `strb w16, [xN]`: the function turns the switch on and
takes its own "disabled" return (-2), so this and every later telemetry call (send, increment, get) go the disabled
way. Found through the exported symbols (initializeTelemetry, telemetryDisabled) and the structure above, never a
game or SDK version; the GOT slot is checked against telemetryDisabled's address when the library's relocations say
where it points. Two instructions change, nothing moves; arm64 only."""
from __future__ import annotations

import io
import struct

from ...analysis import elf
from ..base import ApkContext, Patch, Suggestion, register

LIB = "libInteractionSdk.so"
INIT = "_ZN15interaction_sdk2v19telemetry19initializeTelemetryEv"  # interaction_sdk::v1::telemetry::initializeTelemetry()
DISABLED = "_ZN15interaction_sdk2v19telemetry17telemetryDisabledE"  # ...::telemetryDisabled (bool)
MOV_W16_1 = 0x52800030  # mov w16, #1
STRB_W16 = 0x39000010   # strb w16, [xN]  (| N << 5)
HEAD = 16  # instructions of the prologue searched for the switch test

R_AARCH64_GLOB_DAT, R_AARCH64_RELATIVE = 1025, 1027


def _slot_target(data: bytes, slot: int) -> int | None:
    """Where the GOT slot `slot` points per the library's RELA relocations (RELATIVE addend or GLOB_DAT symbol), or
    None when no plain relocation names it (e.g. Android's packed relocations)."""
    from elftools.elf.elffile import ELFFile
    from elftools.elf.relocation import RelocationSection

    e = ELFFile(io.BytesIO(data))
    dynsym = elf.dynsym_section(e)
    for sec in e.iter_sections():
        if not isinstance(sec, RelocationSection) or sec["sh_type"] != "SHT_RELA":
            continue
        for r in sec.iter_relocations():
            if r["r_offset"] != slot:
                continue
            if r["r_info_type"] == R_AARCH64_RELATIVE:
                return r["r_addend"]
            if r["r_info_type"] == R_AARCH64_GLOB_DAT and dynsym is not None and r["r_info_sym"]:
                return dynsym.get_symbol(r["r_info_sym"])["st_value"] + r["r_addend"]
    return None


def _decode_adrp(word: int, pc: int) -> tuple[int, int] | None:
    if word & 0x9F000000 != 0x90000000:
        return None
    imm = ((word >> 29) & 3) | ((word >> 5) & 0x7FFFF) << 2
    if imm & 1 << 20:
        imm -= 1 << 21
    return word & 31, (pc & ~0xFFF) + (imm << 12)


def switch_site(data: bytes) -> tuple[int, int, bool] | None:
    """(file offset of the `ldrb`, base register N, already patched) of initializeTelemetry's telemetryDisabled test,
    or None when the library doesn't have this shape."""
    if not elf.is_elf(data) or not elf.is_64bit(data) or data[5] != 1:
        return None
    syms = elf.find_symbols(data, (INIT, DISABLED))
    if INIT not in syms or DISABLED not in syms:
        return None
    start, size = syms[INIT]
    segs = elf.load_segments(data)
    base = elf.vaddr_to_offset(segs, start)
    if base is None:
        return None
    count = min(HEAD, (size or HEAD * 4) // 4)
    words = struct.unpack_from(f"<{count}I", data, base) if base + count * 4 <= len(data) else ()
    for i in range(len(words) - 4):
        adrp = _decode_adrp(words[i], start + i * 4)
        ldr = words[i + 1]
        if adrp is None or ldr & 0xFFC00000 != 0xF9400000:  # ldr xT, [xN, #imm*8]
            continue
        n, page = adrp
        if (ldr >> 5) & 31 != n or ldr & 31 != n:
            continue
        slot = page + ((ldr >> 10) & 0xFFF) * 8
        test, branch, ret = words[i + 2], words[i + 3], words[i + 4]
        # mov w0, #-k (movn w0, #k-1) is the "disabled" return the switch leads to
        if ret & 0xFFE0001F != 0x12800000:
            continue
        unpatched = (test & 0xFFFFFC00 == 0x39400000 and (test >> 5) & 31 == n      # ldrb wM, [xN]
                     and branch & 0xFF00001F == 0x34000000 | (test & 31))           # cbz wM, ...
        patched = test == MOV_W16_1 and branch == STRB_W16 | n << 5
        if not (unpatched or patched):
            continue
        target = _slot_target(data, slot)
        if target is not None and target != syms[DISABLED][0]:
            continue
        return base + (i + 2) * 4, n, patched
    return None


def patch_isdk_telemetry(data: bytes) -> bytes | None:
    """The library with initializeTelemetry() switching telemetry off, or None (not this shape / already done)."""
    site = switch_site(data)
    if site is None or site[2]:
        return None
    off, n, _ = site
    out = bytearray(data)
    struct.pack_into("<II", out, off, MOV_W16_1, STRB_W16 | n << 5)
    return bytes(out)


class IsdkTelemetry(Patch):
    id = "frame.isdk_telemetry"
    title = "Interaction SDK: telemetry off"
    description = ("Meta's Interaction SDK (libInteractionSdk.so) starts Quest telemetry through the Quest OS SDK, "
                   "which the Frame doesn't have, and crashes on the missing handler before the title screen "
                   "(\"libossdk not found\", then SIGSEGV in TelemetrySender, e.g. Ghost Signal). Turns on the "
                   "library's own telemetry-disabled switch, so the interaction code runs without telemetry.")
    order = 70
    default_on = True
    a32 = False

    @staticmethod
    def _has_lib(a) -> bool:
        return any(lib == LIB for lib in a.libs)

    def detect(self, a):
        if self._has_lib(a):
            return Suggestion(True, "Meta's Interaction SDK is present; its telemetry is switched off if the "
                                    "library has the Quest telemetry start-up (it crashes without Quest's OS SDK).")
        return None

    def applies(self, a):
        return self._has_lib(a)

    def apply(self, ctx: ApkContext) -> bool:
        changed = False
        for lib in ctx.ws.libs():
            if lib != LIB:
                continue
            data = ctx.ws.read_lib(lib)
            fixed = patch_isdk_telemetry(data)
            if fixed:
                ctx.ws.put(ctx.ws.lib(lib), fixed)
                changed = True
            elif switch_site(data) is None and elf.find_symbols(data, (INIT,)):
                ctx.checks.append(("Interaction SDK telemetry switched off", None,
                                   "initializeTelemetry() has an unknown shape: left unchanged"))
        return changed

    def validate(self, ctx):
        out = []
        for lib in ctx.ws.libs():
            if lib == LIB:
                site = switch_site(ctx.ws.read_lib(lib))
                if site is not None:
                    out.append(("Interaction SDK telemetry switched off", site[2], lib))
        return out


register(IsdkTelemetry)
