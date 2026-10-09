"""Unpaired pointer-authentication hints (PAC) in a game's native libraries.

paciasp signs the return address on function entry and autiasp checks it before returning. Both are HINT
instructions: CPUs without pointer authentication (the Quest's) execute them as NOPs, the Steam Frame's CPU checks
them and raises SIGILL (ILL_ILLOPN at an autiasp) when a check fails. Some OpenSSL ARMv8 assembly built into game
engines has an autiasp without the matching paciasp (e.g. Star Wars Pinball VR's libUE4.so: 38 paciasp, 40 autiasp,
the crash in the Poly1305 NEON code on Unreal's HttpManager thread), so the game dies as soon as it opens a TLS
connection. Only libraries whose PAC hints come from hand-written assembly are changed: compiler-protected code
signs almost every function and has several autiasp per paciasp where a function has several returns (OVRPort's
libopenxr_loader_generic.so: 1084 paciasp, 1114 autiasp, 7092 ret; stripping it hung OVRPlugin's start-up), while an
engine without branch protection has a few dozen hints among hundreds of thousands of returns (libUE4.so: 38 among
342581). So a library is changed when its counts differ and its paciasp are under 0.2 % of its ret instructions; then
every paciasp/autiasp becomes a NOP (the same 4 bytes in place) and the code behaves as on the Quest. The combined
forms (retaa/retab, pacibsp/autibsp) aren't touched; a library using them is left alone."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, register

PACIASP = (0xD503233F).to_bytes(4, "little")
AUTIASP = (0xD50323BF).to_bytes(4, "little")
NOP = (0xD503201F).to_bytes(4, "little")
RET = (0xD65F03C0).to_bytes(4, "little")
MAX_ASM_SHARE = 0.002  # paciasp per ret: above this the library is compiler-protected (left alone)
COMBINED = tuple(w.to_bytes(4, "little") for w in (0xD65F0BFF, 0xD65F0FFF, 0xD503237F, 0xD50323FF))  # retaa/b, *ibsp


def _exec_ranges(data: bytes) -> list[tuple[int, int]]:
    """File ranges of the executable PT_LOAD segments."""
    import io

    from elftools.elf.elffile import ELFFile

    e = ELFFile(io.BytesIO(data))
    return [(s["p_offset"], s["p_offset"] + s["p_filesz"]) for s in e.iter_segments()
            if s["p_type"] == "PT_LOAD" and s["p_flags"] & 1]


def _find(data: bytes, word: bytes, ranges) -> list[int]:
    out = []
    for lo, hi in ranges:
        i = data.find(word, lo, hi)
        while i >= 0:
            if i % 4 == 0:
                out.append(i)
            i = data.find(word, i + 1, hi)
    return out


def unpaired(data: bytes) -> tuple[list[int], list[int]] | None:
    """(paciasp offsets, autiasp offsets) when a library's counts differ, else None."""
    if not elf.is_elf(data):
        return None
    ranges = _exec_ranges(data)
    pac, aut = _find(data, PACIASP, ranges), _find(data, AUTIASP, ranges)
    if len(pac) == len(aut) or any(_find(data, w, ranges) for w in COMBINED):
        return None
    if len(pac) > MAX_ASM_SHARE * max(len(_find(data, RET, ranges)), 1):
        return None  # compiler-protected (several returns per signed function), not hand-written assembly
    return pac, aut


class PacHints(Patch):
    id = "frame.pac_hints"
    title = "Unpaired pointer-authentication checks"
    description = ("Some engines contain assembly (e.g. OpenSSL's) that checks a return-address signature it never "
                   "made. The Quest's CPU ignores those checks, the Steam Frame's enforces them: the game crashes with "
                   "SIGILL as soon as it uses that code, e.g. on its first network connection (Star Wars Pinball "
                   "VR). In a library with unpaired checks, the signing and checking instructions become no-ops.")
    order = 47
    revision = 1

    def applies(self, a):
        return "arm64-v8a" in a.abis

    def detect(self, a):
        return None  # reading every engine library at analysis time is too slow; triage (pac-unpaired) suggests it

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a":
            return False
        changed = False
        for lib in ws.libs():
            data = ws.read_lib(lib)
            found = unpaired(data)
            if not found:
                continue
            pac, aut = found
            out = bytearray(data)
            for off in pac + aut:
                out[off:off + 4] = NOP
            ws.put(ws.lib(lib), bytes(out))
            ctx.notes.append(f"{lib}: {len(pac)} paciasp / {len(aut)} autiasp turned into NOPs (unpaired)")
            changed = True
        return changed


register(PacHints)
