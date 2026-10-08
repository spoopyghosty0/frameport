"""OVRPort's platform loader crashes when a game sizes its microphone buffer before starting the microphone.

OVRPort's `ovr_Microphone_Create` only allocates the handle; the AAudio input stream is opened in
`ovr_Microphone_Start`. `ovr_Microphone_GetOutputBufferMaxSize` returns `AAudioStream_getFramesPerBurst(stream)`
without a check, so a game that asks for the buffer size right after Create (Unreal's Oculus voice capture, e.g. The
Walking Dead: Saints & Sinners Ch. 2) dereferences a NULL stream: SIGSEGV in libaaudio.so on the GameThread a few
seconds after the logo. Meta's own library allows that order. The function is rewritten in place (same size) to return
0 while no stream is open and behave as before otherwise.

Upstream: Android-XR-Bridge/OVRPort#2, ovrport/app#73; tracked in GitHub #73. OVRPort runtime 3.4.3-aa54c3f checks
the handle and the stream itself (returns 48000 without one): builds made with it leave this patch out
(`microphone_checked`, patches/upstream.py).
"""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, Suggestion, register
from ..upstream import UpstreamFix, lib_probe
from ..upstream import register as register_upstream_fix

LOADER = "libovrplatformloader.so"
FUNC = "ovr_Microphone_GetOutputBufferMaxSize"

STP = 0xA9BF7BFD       # stp x29, x30, [sp, #-16]!
MOV_FP = 0x910003FD    # mov x29, sp
LDR_STREAM = 0xF9400C00  # ldr x0, [x0, #0x18]   (the handle's AAudioStream*)
BL, BL_MASK = 0x94000000, 0xFC000000
SXTW = 0x93407C00      # sxtw x0, w0
LDP = 0xA8C17BFD       # ldp x29, x30, [sp], #16
RET = 0xD65F03C0
CBZ_TO_RET = 0xB40000A0  # cbz x0, +0x14 (from the 2nd instruction to the final ret: x0 = 0 is the result)


def _words(data: bytes | bytearray, off: int, n: int) -> list[int]:
    return [int.from_bytes(data[off + 4 * i:off + 4 * i + 4], "little") for i in range(n)]


def _function_offset(data: bytes, size: int) -> int | None:
    """File offset of FUNC in the loader (None: not an ELF, no such export, or shorter than `size` bytes)."""
    try:
        dynsym = elf._elf(data).get_section_by_name(".dynsym")
    except Exception:  # noqa: BLE001
        return None
    found = dynsym.get_symbol_by_name(FUNC) if dynsym is not None else None
    if not found or not found[0]["st_value"]:
        return None
    off = elf.vaddr_to_offset(elf.load_segments(data), found[0]["st_value"])
    return off if off is not None and off + size <= len(data) else None


def microphone_checked(data: bytes) -> bool | None:
    """True when OVRPort's GetOutputBufferMaxSize tests something (a cbz/cbnz) before its first call, as the fixed
    runtime does; False when it is the unchecked version this patch rewrites; None when it can't tell."""
    if guard_microphone(data) is not None:
        return False
    off = _function_offset(data, 48)
    if off is None:
        return None
    for word in _words(data, off, 12):
        if word & BL_MASK == BL:
            return None
        if word & 0x7E000000 == 0x34000000:  # CBZ / CBNZ (32 or 64 bit)
            return True
    return None


def guard_microphone(data: bytes) -> bytes | None:
    """The loader with a NULL-stream check in ovr_Microphone_GetOutputBufferMaxSize, or None if the function isn't
    OVRPort's unchecked 7-instruction version (already fixed, Meta's library, another OVRPort build)."""
    off = _function_offset(data, 28)
    if off is None:
        return None
    w = _words(data, off, 7)
    if w[:3] != [STP, MOV_FP, LDR_STREAM] or w[3] & BL_MASK != BL or w[4:] != [SXTW, LDP, RET]:
        return None
    # mov x29, sp goes (the frame record is still pushed), which makes room for the check; the bl stays at +12, so
    # its PC-relative offset is unchanged
    new = [LDR_STREAM, CBZ_TO_RET, STP, w[3], LDP, SXTW, RET]
    out = bytearray(data)
    for i, word in enumerate(new):
        out[off + 4 * i:off + 4 * i + 4] = word.to_bytes(4, "little")
    return bytes(out)


class OvrMicrophone(Patch):
    id = "frame.ovr_microphone"
    title = "Fix crash when the game prepares the microphone"
    description = ("OVRPort's Meta platform library opens the microphone only when it's started, but answers a "
                   "game's question about the microphone buffer size from that not-yet-opened stream: games that ask "
                   "first (Unreal's Oculus voice chat, e.g. The Walking Dead: Saints & Sinners Ch. 2) crash a few "
                   "seconds after the logo (SIGSEGV in libaaudio.so). Adds the missing check (size 0 until the "
                   "microphone is started).")
    order = 46

    def detect(self, a):
        if a.extra.get("ovr_microphone"):
            return Suggestion(True, "The game uses Meta's microphone API (ovr_Microphone_GetOutputBufferMaxSize), "
                                    "which crashes in OVRPort's library before the microphone is started.")
        return None

    def applies(self, a):
        return bool(a.extra.get("ovr_microphone")) or LOADER in a.libs

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if not ws.has(ws.lib(LOADER)):
            return False
        fixed = guard_microphone(ws.read_lib(LOADER))
        if fixed:
            ws.put(ws.lib(LOADER), fixed)
            ctx.notes.append(f"{LOADER}: {FUNC} checks for an unopened microphone stream")
        return bool(fixed)


register(OvrMicrophone)
register_upstream_fix(UpstreamFix(
    id="ovrport.microphone_stream",
    workaround=OvrMicrophone.id,
    title="OVRPort checks for an unopened microphone stream",
    upstream="ovrport/app#73 (runtime 3.4.3-aa54c3f)",
    probe=lib_probe(LOADER, microphone_checked),
    tracker="#73",
))
