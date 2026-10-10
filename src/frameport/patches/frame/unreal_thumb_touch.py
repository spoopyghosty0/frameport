"""Unreal 4's Oculus input: the ThumbUp capacitive axis from the capacitive touches instead of near-touch.

UE4's stock OculusInput (FOculusInput::SendControllerEvents) sets each controller's ThumbUp axis ("thumb lifted", what
games animate the hand's thumb with) from OVRPlugin's NearTouches (ovrpNearTouch_LThumbButtons 0x2 / RThumbButtons
0x8): 1.0 when the thumb is near no button. The Frame's runtime has no XR_FB_touch_controller_proximity, so near-touch
is never reported and the thumb always points up, whatever it touches (FrameBridge's proximity_emul binding didn't
reach these games either). The fix uses the actual touches instead, with the same inversion: the left hand's
X/Y/stick/thumb rest (0x0f00), the right hand's A/B/stick/thumb rest (0x000f).

Three instructions change in the capacitive-axis loop: the two mask constants the per-hand ThumbUp mask is selected
from (`mov wA, #2; mov wB, #8; csel wM, wA, wB, <hand>` → 0xf00 / 0xf) and the load of NearTouches before
`tst wN, wM; fcsel` (its stack slot → the Touches slot 4 bytes before it, which the other touch axes load). The code is
located by its exported symbol and checked instruction by instruction; anything else is left alone. Found by Klownicle
in Vader Immortal: Episode I 1.1.1 (GitHub #49); the same compiled code is in other UE4 Oculus games of that engine
generation (e.g. Robo Recall, Phantom: Covert Ops, Time Stall, Star Wars: Tales from the Galaxy's Edge)."""
from __future__ import annotations

import struct

from ...analysis import elf
from ...analysis.detect import UNREAL_THUMB_TOUCH
from ..base import ApkContext, Patch, Suggestion, register

ENGINE_LIBS = ("libUE4.so", "libUnreal.so")
NEAR_LEFT, NEAR_RIGHT = 0x2, 0x8        # ovrpNearTouch_LThumbButtons / RThumbButtons
TOUCH_LEFT, TOUCH_RIGHT = 0xF00, 0x00F  # X|Y|LThumb|LThumbRest / A|B|RThumb|RThumbRest
ORR_WZR = {0x321F03E0: 0x2, 0x321D03E0: 0x8}  # orr wD, wzr, #imm (logical immediates the compiler used)
SCAN_LIMIT = 0x4000  # bytes of the function searched


def _const(w: int) -> tuple[int, int] | None:
    """(register, value) of `orr wD, wzr, #2|#8` or `movz wD, #imm` (shift 0)."""
    if w & 0xFFFFFFE0 in ORR_WZR:
        return w & 31, ORR_WZR[w & 0xFFFFFFE0]
    if w & 0xFFE00000 == 0x52800000:
        return w & 31, (w >> 5) & 0xFFFF
    return None


def _movz(reg: int, value: int) -> int:
    return 0x52800000 | value << 5 | reg


def _csel(w: int) -> tuple[int, int, int] | None:  # csel wD, wN, wM, cond -> (d, n, m)
    if w & 0xFFE00C00 != 0x1A800000:
        return None
    return w & 31, (w >> 5) & 31, (w >> 16) & 31


def _tst(w: int) -> tuple[int, int] | None:  # tst wN, wM (ands wzr, wN, wM) -> (n, m)
    if w & 0xFFE0FC1F != 0x6A00001F:
        return None
    return (w >> 5) & 31, (w >> 16) & 31


def _is_fcsel(w: int) -> bool:
    return w & 0xFFE00C00 == 0x1E200C00


def _load(w: int) -> tuple[int, int, int] | None:
    """(Rt, base register, byte offset) of `ldr wT, [xN, #imm]` (unsigned offset) or `ldur wT, [xN, #simm]`."""
    if w & 0xFFC00000 == 0xB9400000:
        return w & 31, (w >> 5) & 31, ((w >> 10) & 0xFFF) * 4
    if w & 0xFFE00C00 == 0xB8400000:
        imm = (w >> 12) & 0x1FF
        return w & 31, (w >> 5) & 31, imm - 0x200 if imm & 0x100 else imm
    return None


def _with_offset(w: int, offset: int) -> int | None:
    if w & 0xFFC00000 == 0xB9400000:
        return (w & ~(0xFFF << 10)) | (offset // 4) << 10 if 0 <= offset < 0x4000 and offset % 4 == 0 else None
    if w & 0xFFE00C00 == 0xB8400000:
        return (w & ~(0x1FF << 12)) | (offset & 0x1FF) << 12 if -256 <= offset < 256 else None
    return None


def thumb_site(data: bytes) -> dict | None:
    """The ThumbUp code in OculusInput::FOculusInput::SendControllerEvents: {"edits": [(file offset, old word, new
    word), ...], "done": bool}, or None when the code isn't the expected one (exactly one site must match)."""
    syms = elf.find_symbols(data, (UNREAL_THUMB_TOUCH,))
    if UNREAL_THUMB_TOUCH not in syms:
        return None
    va, size = syms[UNREAL_THUMB_TOUCH]
    segs = elf.load_segments(data)
    off = elf.vaddr_to_offset(segs, va)
    size = min(size, SCAN_LIMIT) & ~3
    if off is None or size <= 0 or off + size > len(data):
        return None
    words = list(struct.unpack_from(f"<{size // 4}I", data, off))
    loads = [_load(w) for w in words]
    sites = []
    for i in range(len(words) - 4):
        a, b = _const(words[i]), _const(words[i + 1])
        if not a or not b or a[0] == b[0]:
            continue
        pair = {a[1], b[1]}
        if pair not in ({NEAR_LEFT, NEAR_RIGHT}, {TOUCH_LEFT, TOUCH_RIGHT}):
            continue
        done = pair == {TOUCH_LEFT, TOUCH_RIGHT}
        # the csel picks one of the two per hand, at most one unrelated instruction in between
        sel, k = None, i + 2
        for k in (i + 2, i + 3):
            c = _csel(words[k])
            if c and {c[1], c[2]} == {a[0], b[0]}:
                sel = c
                break
            if any((words[k] >> s) & 31 in (a[0], b[0]) for s in (0, 5, 16)):
                break  # something else uses the registers
        if not sel:
            continue
        mask = sel[0]
        for j in range(k + 1, len(words) - 2):
            t = _tst(words[j + 1])
            ld = loads[j]
            if not (t and ld and t[1] == mask and t[0] == ld[0] and _is_fcsel(words[j + 2])):
                continue
            base, at = ld[1], ld[2]
            touches = at if done else at - 4
            # the other touch axes load the Touches slot (NearTouches is the next field of ovrpControllerState)
            others = sum(1 for n, x in enumerate(loads) if n != j and x and x[1] == base and x[2] == touches)
            if others < 2:
                continue
            new_load = words[j] if done else _with_offset(words[j], touches)
            if new_load is None:
                continue
            new = {NEAR_LEFT: TOUCH_LEFT, NEAR_RIGHT: TOUCH_RIGHT}
            edits = [(off + 4 * i, words[i], _movz(a[0], new.get(a[1], a[1]))),
                     (off + 4 * (i + 1), words[i + 1], _movz(b[0], new.get(b[1], b[1]))),
                     (off + 4 * j, words[j], new_load)]
            sites.append({"edits": edits, "done": done})
    return sites[0] if len(sites) == 1 else None


def _engine(ws) -> str | None:
    return next((n for n in ENGINE_LIBS if ws.has(ws.lib(n))), None)


class UnrealThumbTouch(Patch):
    id = "frame.unreal_thumb_touch"
    title = "Unreal: thumb follows the capacitive touches"
    description = ("Unreal 4's Oculus input sets the ThumbUp axis (games animate the hand's thumb with it) from "
                   "Meta's near-touch sensing, which the Frame never reports: the thumb always points up. Uses the "
                   "touches instead (left X/Y/stick/thumb rest, right A/B/stick/thumb rest). Changes three "
                   "instructions in OculusInput::SendControllerEvents, only where the code matches exactly (UE4 "
                   "Oculus games of one engine generation, for example Vader Immortal, Robo Recall). Found by "
                   "Klownicle (GitHub #49).")
    order = 47
    note = "OculusInput ThumbUp: from the capacitive touches (left 0x0f00, right 0x000f)"
    reason = ("Unreal's Oculus input animates the thumb from near-touch sensing the Frame lacks (the thumb always "
              "points up, for example Vader Immortal).")

    def applies(self, a):
        if a.engine != "Unreal" or "arm64-v8a" not in a.abis:
            return False
        found = (a.extra or {}).get("unreal_thumb_touch")
        return found is None or bool(found)  # None: analysed by an older FramePort, maybe

    def detect(self, a):
        if (a.extra or {}).get("unreal_thumb_touch") and "arm64-v8a" in a.abis:
            return Suggestion(True, self.reason)
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        lib = _engine(ws) if ws.abi == "arm64-v8a" else None
        if not lib:
            return False
        data = ws.read_lib(lib)
        site = thumb_site(data)
        if not site:
            ctx.notes.append(f"{self.id}: {lib} doesn't match the expected code, left unchanged")
            return False
        if site["done"]:
            return False
        buf = bytearray(data)
        for at, _old, new in site["edits"]:
            struct.pack_into("<I", buf, at, new)
        ws.put(ws.lib(lib), bytes(buf))
        ctx.notes.append(f"{self.note} ({lib} file offsets " + ", ".join(f"{e[0]:#x}" for e in site["edits"]) + ")")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        lib = _engine(ws)
        if not lib or ws.abi != "arm64-v8a":
            return []
        site = thumb_site(ws.read_lib(lib))
        return [("ThumbUp reads the capacitive touches", bool(site) and site["done"],
                 "" if site else "code not found (different build)")]


register(UnrealThumbTouch)
