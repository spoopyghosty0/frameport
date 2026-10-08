"""Detects OVRPort's fix for the haptic-envelope bug, which makes `adapter.haptic_fix` unnecessary.

OVRPort's dispatcher (libopenxr_loader.so) turns XrHapticAmplitudeEnvelopeVibrationFB into 100 Hz samples. Runtimes
up to 3.4.3-23204ea used the envelope's duration (nanoseconds) as seconds when counting them: one vibration asked for
gigabytes (Lucky's Tale froze the Frame, BattleSisters was killed for 11 GB). Runtime 3.4.3-aa54c3f converts it first
(ovrport/app#73): right after the type check for the envelope (XR_TYPE_HAPTIC_AMPLITUDE_ENVELOPE_VIBRATION_FB =
1000173001) it builds the constant 1e9 (`count * 1e9 / duration` = samples per second), which the buggy code never
had. Verified in the headset with Lucky's Tale and haptic_fix off (2026-10-07). See patches/upstream.py.
"""
from __future__ import annotations

import struct

from ..upstream import UpstreamFix, code_words, constants, find_constant_load, lib_probe, register

DISPATCHER = "libopenxr_loader.so"
ENVELOPE_TYPE = 1000173001  # XR_TYPE_HAPTIC_AMPLITUDE_ENVELOPE_VIBRATION_FB
WINDOW = 48  # instructions after the type check that hold the sample count (the fix: 7 after it)
NS_PER_S = {struct.unpack("<Q", struct.pack("<d", v))[0] for v in (1e9, 1e-9)} | \
           {struct.unpack("<I", struct.pack("<f", v))[0] for v in (1e9, 1e-9)}


def _literals(data: bytes, offsets: list[int], words: list[int]) -> list[int]:
    """Values loaded PC-relative (LDR literal: GP w/x, SIMD s/d) by the given instructions."""
    out = []
    for off, w in zip(offsets, words, strict=True):
        if w & 0x3B000000 != 0x18000000:  # LDR (literal), GP (V=0) or SIMD (V=1)
            continue
        size = {0: 4, 1: 8}.get(w >> 30)  # opc: w / s = 4 bytes, x / d = 8 (q isn't a scalar constant)
        if size is None:
            continue
        imm19 = (w >> 5) & 0x7FFFF
        target = off + ((imm19 - (1 << 19)) if imm19 & (1 << 18) else imm19) * 4
        if 0 <= target <= len(data) - size:
            out.append(int.from_bytes(data[target:target + size], "little"))
    return out


def envelope_duration_fixed(data: bytes) -> bool | None:
    """True when the dispatcher converts the envelope duration from nanoseconds (OVRPort's fix), False when its
    envelope code has no such conversion (the bug), None when there's no envelope code to judge."""
    code = list(code_words(data))
    offsets, words = [o for o, _ in code], [w for _, w in code]
    seen = False
    start = 0
    while (i := find_constant_load(words[start:], ENVELOPE_TYPE)) is not None:
        i += start
        seen = True
        window = slice(i, i + WINDOW)
        if NS_PER_S & set(constants(words[window])) or NS_PER_S & set(_literals(data, offsets[window], words[window])):
            return True
        start = i + 1
    return False if seen else None


register(UpstreamFix(
    id="ovrport.haptic_envelope",
    workaround="adapter.haptic_fix",
    title="OVRPort converts the vibration envelope's duration from nanoseconds",
    upstream="ovrport/app#73 (runtime 3.4.3-aa54c3f)",
    probe=lib_probe(DISPATCHER, envelope_duration_fixed),
    tracker="#74",
))
