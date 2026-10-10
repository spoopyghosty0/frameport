"""Upstream fixes that make one of our workarounds unnecessary, applied per build.

Several patches work around bugs in what OVRPort puts into a converted APK (its closed-source runtime libraries, its
CLI). When upstream fixes such a bug, users don't all get the fix at once: OVRPort downloads runtime builds into its
workspace and each conversion uses whichever one it picks, so one library can hold games built with the old and the
new runtime. A version number doesn't help either (runtime builds are named `<version>-<commit>`, the commit part is
unordered). So each fix is detected in the converted APK itself: a probe looks at the bytes OVRPort produced, and when
it finds the upstream fix, the build leaves the workaround out.

The recipe is never changed: it still asks for the fix (`adapter.haptic_fix` on), the build decides how to provide it.
A build made with an old runtime keeps the workaround; the next build with a fixed runtime drops it by itself, and a
build made with a later runtime that regressed would get the workaround back. Builds record what was left out
(`build.superseded`), the installer leaves those out of settings.conf too, and the build log says why.

Add one: write a probe (True = the upstream fix is there, False = the bug is there, None = can't tell, e.g. the library
is missing or looks different) and `register(UpstreamFix(...))` in the workaround's own module. Only True drops the
workaround, so a probe that can't recognise newer code errs on the safe side. Then update the workaround's tracker
issue (`upstream` label). FRAMEPORT_KEEP_WORKAROUNDS=1 keeps every workaround (to compare builds).
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..apk.workspace import ApkWorkspace
    from ..core.events import Reporter

Probe = Callable[["ApkWorkspace"], "bool | None"]


@dataclass(frozen=True)
class UpstreamFix:
    id: str  # for example "ovrport.haptic_envelope"
    workaround: str  # the patch id this fix makes unnecessary, for example "adapter.haptic_fix"
    title: str  # what upstream fixed, for the build log
    upstream: str  # where it was fixed, for example "ovrport/app#73 (runtime 3.4.3-aa54c3f)"
    probe: Probe
    tracker: str = ""  # our tracking issue, for example "#74"


REGISTRY: dict[str, UpstreamFix] = {}


def register(fix: UpstreamFix) -> UpstreamFix:
    if not fix.id or fix.id in REGISTRY:
        raise ValueError(f"bad or duplicate upstream fix id {fix.id!r}")
    REGISTRY[fix.id] = fix
    return fix


def lib_probe(library: str, check: Callable[[bytes], bool | None]) -> Probe:
    """A probe that runs `check` on one of the APK's native libraries (None when the APK doesn't have it)."""
    def probe(ws: ApkWorkspace) -> bool | None:
        return check(ws.read_lib(library)) if ws.abi and ws.has(ws.lib(library)) else None

    return probe


@dataclass
class Resolution:
    patches: dict  # the selection this build applies
    superseded: dict[str, str] = field(default_factory=dict)  # workaround patch id -> upstream fix id
    found: dict[str, bool | None] = field(default_factory=dict)  # upstream fix id -> probe result (for the log)


def resolve(ws: ApkWorkspace, patches: dict, reporter: Reporter | None = None) -> Resolution:
    """Leave out the workarounds whose upstream fix is in this (converted) APK."""
    from . import base

    base.load_all()
    res = Resolution(dict(patches))
    if os.environ.get("FRAMEPORT_KEEP_WORKAROUNDS"):
        return res
    for fix in REGISTRY.values():
        if fix.workaround not in patches:
            continue
        try:
            found = fix.probe(ws)
        except Exception as e:  # noqa: BLE001  (a probe that breaks must not break the build: keep the workaround)
            found = None
            if reporter:
                reporter.log(f"upstream fix check {fix.id} failed ({e}); keeping {fix.workaround}")
        res.found[fix.id] = found
        if found:
            res.patches.pop(fix.workaround, None)
            res.superseded[fix.workaround] = fix.id
            if reporter:
                reporter.log(f"not needed: {fix.workaround} ({fix.title}, fixed upstream in {fix.upstream})")
    return res


def without_superseded(patches: dict, superseded) -> dict:
    """A recipe's selection minus the workarounds a build left out (for the install of that build)."""
    return {pid: params for pid, params in patches.items() if pid not in (superseded or ())}


# ---------------------------------------------------------------- arm64 helpers for probes

def code_words(data: bytes):
    """(file offset, instruction word) for every word of the library's executable sections."""
    from ..analysis import elf

    for sec in elf._elf(data).iter_sections():
        if sec["sh_type"] == "SHT_PROGBITS" and sec["sh_flags"] & 0x4:  # SHF_EXECINSTR
            start, size = sec["sh_offset"], sec["sh_size"]
            for off in range(start, start + size - 3, 4):
                yield off, int.from_bytes(data[off:off + 4], "little")


def move_wide(word: int) -> tuple[str, int, int, int, int] | None:
    """Decode MOVZ/MOVK/MOVN: (kind, sf, rd, imm16, shift) or None."""
    kinds = {0x52800000: "movz", 0x72800000: "movk", 0x12800000: "movn"}
    kind = kinds.get(word & 0x7F800000)
    if kind is None:
        return None
    return kind, word >> 31, word & 31, (word >> 5) & 0xFFFF, ((word >> 21) & 3) * 16


def constants(words) -> list[int]:
    """Every value a register holds after each MOVZ/MOVK/MOVN in a run of instruction words (how compilers build
    64-bit constants such as doubles: movz + movk)."""
    regs: dict[int, int] = {}
    out = []
    for w in words:
        mw = move_wide(w)
        if mw is None:
            continue
        kind, sf, rd, imm, shift = mw
        mask = (1 << 64) - 1 if sf else (1 << 32) - 1
        if kind == "movz":
            regs[rd] = imm << shift
        elif kind == "movn":
            regs[rd] = ~(imm << shift) & mask
        else:
            regs[rd] = (regs.get(rd, 0) & ~(0xFFFF << shift) | imm << shift) & mask
        out.append(regs[rd])
    return out


def find_constant_load(words: list[int], value: int) -> int | None:
    """Index of the first MOVZ/MOVN that starts building `value` (32-bit) in its register, followed by the MOVK that
    completes it within the next few instructions."""
    for i, w in enumerate(words):
        mw = move_wide(w)
        if mw is None or mw[0] == "movk":
            continue
        if value in constants(words[i:i + 4]):
            return i
    return None
