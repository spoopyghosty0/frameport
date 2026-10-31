"""Unity's FMOD records the microphone through OpenSL ES into a ring of four DSP buffers (4 x 512 frames, 43 ms at
48 kHz) and tells new audio by the ring position alone: its buffer-queue callback re-enqueues the buffer it got and
moves the position one buffer on, and the mixer reads (position - last read) mod ring. Lepton's microphone is a
48 kHz stereo 32-bit stream, FMOD asks for mono 16-bit, so AudioFlinger denies the fast capture path
("createRecordTrack_l(): mismatch between requested flags (00000005)") and the recorder gets the normal one: 20 ms
notifications on a 2880-frame track, read by OpenSL in bursts (mostly four 512-frame buffers at once, every ~40 ms).
A burst of four buffers laps the ring, the position ends where it was, FMOD sees no new audio and the burst is
overwritten: Unity's Microphone.GetPosition advanced 13 % of real time and voice chat cut in and out (Bigscreen,
Unity 2020.3; traced live with gdb on the Frame). On a Quest the fast path delivers a buffer at a time.

The fix gives the recorder a ring of 16 buffers, longer than the whole AudioFlinger track, so no burst can lap it.
Right after FMOD's recorder start asks System::getDSPBufferSize for the buffer length and count, the count it stored
is set to 16; the queue length, the ring allocation and the mixer's ring length all read that count. Output mixing
keeps Unity's own buffers (no added latency). Four instructions change in place: `bl getDSPBufferSize; cbnz w0, fail;
adrp xR, ...; ldr dS, [xR, ...]; ldur wR, [x29, #-B]` becomes `...; adrp; ldr; mov wR, #16; stur wR, [x29, #-B]`
(the dropped check is for invalid arguments only). The site is the one whose next instructions build OpenSL's
buffer-queue locator (0x800007BD) and the default audio input device (-1); FMOD's output player has no such site."""
from __future__ import annotations

import struct

from ...analysis import elf
from ..base import ApkContext, Patch, Suggestion, register

BUFFERS = 16
LOCATOR_MOV = 0x5280F7A0  # mov wN, #0x7bd (SL_DATALOCATOR_ANDROIDSIMPLEBUFFERQUEUE = 0x800007BD, low half)
DEVICE_MOV = 0x12800000  # mov wN, #-1 (SL_DEFAULTDEVICEID_AUDIOINPUT)


def _word(data: bytes, at: int) -> int:
    return struct.unpack_from("<I", data, at)[0]


def _adrp_page(word: int, address: int) -> int:
    imm = ((word >> 5) & 0x7FFFF) << 2 | (word >> 29) & 3
    if imm & (1 << 20):
        imm -= 1 << 21
    return (address & ~0xFFF) + (imm << 12)


def _adrp(rd: int, address: int, page: int) -> int:
    imm = ((page - (address & ~0xFFF)) >> 12) & 0x1FFFFF
    return 0x90000000 | (imm & 3) << 29 | (imm >> 2) << 5 | rd


def _ldur_slot(word: int) -> tuple[int, int] | None:
    """(register, offset) of `ldur wR, [x29, #off]`, else None."""
    if word & 0xFFE00C00 != 0xB8400000 or (word >> 5) & 0x1F != 29:
        return None
    off = (word >> 12) & 0x1FF
    return word & 0x1F, off - 0x200 if off & 0x100 else off


def _stur(rt: int, off: int) -> int:
    return 0xB8000000 | (off & 0x1FF) << 12 | 29 << 5 | rt


def _sites(data: bytes):
    """(offset of the instruction after `bl getDSPBufferSize`, state) for FMOD's OpenSL recorder start; state is
    "original" (cbnz w0; adrp xR; ldr dS, [xR]; ldur wR, [x29, #-B]) or "widened" (adrp; ldr; mov wR, #16; stur)."""
    end = len(data) - len(data) % 4 - 64
    start = 0
    while True:
        at = data.find(struct.pack("<H", LOCATOR_MOV >> 16), start)  # cheap prefilter: the opcode's high half
        if at < 0 or at >= end:
            return
        start = at + 1
        at -= 2
        if at % 4 or _word(data, at) & 0xFFFFFFE0 != LOCATOR_MOV:
            continue
        if not any(_word(data, at + 4 * i) & 0xFFFFFFE0 == DEVICE_MOV for i in range(-6, 8)):
            continue  # an output player's locator: no input device next to it
        for back in range(4, 40, 4):  # the count's load comes a few instructions before the locator
            load = at - back
            if load < 20:
                break
            sub, bl = _word(data, load - 20), _word(data, load - 16)
            if bl & 0xFC000000 != 0x94000000 or sub & 0xFFC003FF != 0xD10003A2:  # bl; sub x2, x29, #B before it
                continue
            count = -((sub >> 10) & 0xFFF)
            w0, w1, w2, w3 = (_word(data, load - 12 + 4 * i) for i in range(4))
            slot = _ldur_slot(w3)
            if slot is not None and slot[1] == count:
                rd = slot[0]
                if (w0 & 0xFF00001F == 0x35000000 and w1 & 0x9F000000 == 0x90000000 and w1 & 0x1F == rd
                        and w2 & 0xFFC00000 == 0xFD400000 and (w2 >> 5) & 0x1F == rd):
                    yield load - 12, "original"
                    break
            rd = w3 & 0x1F
            if (w3 == _stur(rd, count) and w2 == 0x52800000 | BUFFERS << 5 | rd and w0 & 0x9F000000 == 0x90000000
                    and w0 & 0x1F == rd and w1 & 0xFFC00000 == 0xFD400000):
                yield load - 12, "widened"
                break


def record_ring(data: bytes) -> str | None:
    """"original", "widened" or None (no recognised FMOD OpenSL recorder start in this libunity.so)."""
    states = {state for _, state in _sites(data)}
    return "widened" if states == {"widened"} else "original" if "original" in states else None


def widen_record_ring(data: bytes, base: int = 0) -> tuple[bytes, int]:
    """Patch every unpatched recorder start (`base` = the file offset's virtual address minus the offset, for the
    re-encoded adrp); returns (data, count)."""
    buf, count = bytearray(data), 0
    for at, state in list(_sites(data)):
        if state != "original":
            continue
        adrp, ldrd, load = _word(data, at + 4), _word(data, at + 8), _word(data, at + 12)
        rd, off = _ldur_slot(load)
        page = _adrp_page(adrp, base + at + 4)
        struct.pack_into("<4I", buf, at, _adrp(rd, base + at, page), ldrd, 0x52800000 | BUFFERS << 5 | rd,
                         _stur(rd, off))
        count += 1
    return bytes(buf), count


def _text_base(data: bytes) -> int:
    """vaddr - file offset of the executable segment (0 for Android's libunity.so; raw test blobs are 0 too)."""
    if not elf.is_elf(data):
        return 0
    return next((v - o for v, o, _ in elf.load_segments(data) if o == 0), 0)


class UnityMicRing(Patch):
    id = "frame.unity_mic_ring"
    title = "Unity: microphone without dropouts"
    description = (
        "Unity's audio engine (FMOD) records the microphone into a ring of only four short buffers and can't tell "
        "when a whole ring arrived at once. Lepton's microphone reaches games in bursts of about that size (Android "
        "denies the fast capture path because Lepton's microphone is a stereo 32-bit stream), so most of what the "
        "player says is overwritten before Unity reads it: voice chat cuts in and out for everyone else (Bigscreen "
        "sent 13 % of the audio). Gives FMOD's recorder 16 buffers in libunity.so (four instructions); sound output "
        "is unchanged.")
    order = 47
    needs_vr = False
    a32 = False

    def applies(self, a):
        return a.engine == "Unity" and "arm64-v8a" in a.abis and bool((a.extra or {}).get("fmod_record_ring"))

    def detect(self, a):
        if self.applies(a):
            return Suggestion(True, "Unity game that records the microphone: FMOD's 4-buffer recording ring loses "
                                    "most of the voice on the Frame (voice chat cuts in and out, for example "
                                    "Bigscreen).")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        name = ws.lib("libunity.so")
        if ws.abi != "arm64-v8a" or not ws.has(name):
            return False
        data = ws.read(name)
        patched, count = widen_record_ring(data, _text_base(data))
        if not count:
            return False
        ws.put(name, patched)
        ctx.notes.append(f"libunity.so: FMOD's microphone ring {BUFFERS} buffers ({count}x)")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        name = ws.lib("libunity.so")
        if ws.abi != "arm64-v8a" or not ws.has(name):
            return []
        return [("FMOD microphone ring widened", record_ring(ws.read(name)) == "widened", "libunity.so")]


register(UnityMicRing)
