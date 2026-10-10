"""Unreal games that pick Quest code paths with IsRunningOnSantaCruz() (ILMxLAB's Unreal 4 fork, e.g. Vader Immortal).

The engine answers "not a Quest" on the Frame (Lepton's Android isn't a Quest), so the game takes its Gear VR / PC
paths. Two of them leave a Quest build stuck; each fix rewrites one instruction in the engine library at the branch that
matters and changes nothing else (the device stays "not a Quest" everywhere else):

* GetQuestShaderPrecompilePercent: the Quest branch reports the progress of the shader precompile the game starts on a
  Quest; the other branch returns 0.0 although no precompile was ever started there. The menu waits for 100 %: the
  loading card (Vader's portrait with a progress bar) never ends. The other branch returns 1.0 instead.
* URPOCKeyMapManagerComponent's key selector (shared by AddAxisMapping and AddActionMapping): for an OculusHMD it
  chooses the Quest ("SantaCruz") key set or the Gear VR one by IsRunningOnSantaCruz(). A Quest build's Gear VR set is
  empty, so grip/trigger/touch bindings never reach the game's hands (input arrives in Unreal, the game's axes stay 0).
  The branch to the Gear VR set is removed for the OculusHMD case only.

Both are located by their exported symbol and checked instruction by instruction (found by Klownicle in Vader Immortal:
Episode I 1.1.1, GitHub #49); a library whose code doesn't match exactly is left alone. Already patched code counts as
done."""
from __future__ import annotations

import struct

from ...analysis import elf
from ...analysis.detect import UNREAL_QUEST_GATES
from ..base import ApkContext, Patch, Suggestion, register

ENGINE_LIBS = ("libUE4.so", "libUnreal.so")
SANTACRUZ = ("_ZN15FSantaCruzUtils20IsRunningOnSantaCruzEv", "_ZN23UNTNinjaFunctionLibrary20IsRunningOnSantaCruzEv")
PRECOMPILE = UNREAL_QUEST_GATES["quest_precompile"]
ADD_AXIS = UNREAL_QUEST_GATES["rpoc_keymap"]
ADD_ACTION = "_ZN27URPOCKeyMapManagerComponent16AddActionMappingERK15FRPOCKeyMappingR16FRPOCInputMapSet"

FMOV_S0_ZERO = 0x1E2703E0  # fmov s0, wzr
FMOV_S0_ONE = 0x1E2E1000   # fmov s0, #1.0
LDP_FP_LR = 0xA8C17BFD     # ldp x29, x30, [sp], #0x10
RET = 0xD65F03C0
NOP = 0xD503201F
SELECTOR_SCAN = 0x400      # bytes of the key selector searched for the branch


def _words(data: bytes, segs, va: int, size: int) -> list[int] | None:
    off = elf.vaddr_to_offset(segs, va)
    if off is None or size <= 0 or off + size > len(data):
        return None
    return list(struct.unpack_from(f"<{size // 4}I", data, off))


def _bl_target(va: int, w: int) -> int | None:
    if w & 0xFC000000 != 0x94000000:
        return None
    imm = w & 0x03FFFFFF
    return va + ((imm - (1 << 26) if imm & (1 << 25) else imm) << 2)


def _tbz_w0_0_target(va: int, w: int) -> int | None:  # tbz w0, #0, <target>
    if w & 0xFFF8001F != 0x36000000:
        return None
    imm = (w >> 5) & 0x3FFF
    return va + ((imm - (1 << 14) if imm & (1 << 13) else imm) << 2)


def _is_add_x0_x19(w: int) -> bool:  # add x0, x19, #imm
    return w & 0xFFC003FF == 0x91000260


def _is_b(w: int) -> bool:
    return w & 0xFC000000 == 0x14000000


def precompile_site(data: bytes) -> tuple[int, int] | None:
    """(file offset, current word) of the non-Quest return value in GetQuestShaderPrecompilePercent: the target of
    `bl IsRunningOnSantaCruz; tbz w0, #0, L` where L is `fmov s0, wzr; ldp x29, x30, [sp], #16; ret` (or already
    `fmov s0, #1.0`)."""
    syms = elf.find_symbols(data, (PRECOMPILE, *SANTACRUZ))
    if PRECOMPILE not in syms:
        return None
    targets = {syms[s][0] for s in SANTACRUZ if s in syms}
    va, size = syms[PRECOMPILE]
    segs = elf.load_segments(data)
    words = _words(data, segs, va, min(size, 0x100))
    if not words or not targets:
        return None
    sites = []
    for i in range(len(words) - 1):
        if _bl_target(va + 4 * i, words[i]) not in targets:
            continue
        to = _tbz_w0_0_target(va + 4 * (i + 1), words[i + 1])
        j = (to - va) // 4 if to is not None else -1
        if 0 <= j < len(words) - 2 and words[j] in (FMOV_S0_ZERO, FMOV_S0_ONE) and words[j + 1] == LDP_FP_LR \
                and words[j + 2] == RET:
            sites.append((elf.vaddr_to_offset(segs, va + 4 * j), words[j]))
    return sites[0] if len(sites) == 1 else None


def keymap_site(data: bytes) -> tuple[int, int] | None:
    """(file offset, current word) of the `tbz w0, #0, <Gear VR keys>` after `bl IsRunningOnSantaCruz` in the key
    selector both URPOCKeyMapManagerComponent::AddAxisMapping and ::AddActionMapping call. The Quest side must be
    `add x0, x19, #field; b <return>` and the Gear VR side the same with another field; a nop there = already done."""
    syms = elf.find_symbols(data, (ADD_AXIS, ADD_ACTION, *SANTACRUZ))
    if ADD_AXIS not in syms or ADD_ACTION not in syms:
        return None
    targets = {syms[s][0] for s in SANTACRUZ if s in syms}
    segs = elf.load_segments(data)
    calls = []
    for name in (ADD_AXIS, ADD_ACTION):
        va, size = syms[name]
        words = _words(data, segs, va, min(size, 0x1000)) or []
        calls.append({_bl_target(va + 4 * i, w) for i, w in enumerate(words)} - {None})
    sites = []
    for sel in sorted(calls[0] & calls[1]):
        words = _words(data, segs, sel, SELECTOR_SCAN)
        if not words:
            continue
        for i in range(len(words) - 3):
            if _bl_target(sel + 4 * i, words[i]) not in targets:
                continue
            branch, quest, back = words[i + 1], words[i + 2], words[i + 3]
            if not (_is_add_x0_x19(quest) and _is_b(back)):
                continue
            if branch == NOP:
                sites.append((elf.vaddr_to_offset(segs, sel + 4 * (i + 1)), NOP))
                continue
            to = _tbz_w0_0_target(sel + 4 * (i + 1), branch)
            j = (to - sel) // 4 if to is not None else -1
            if 0 <= j < len(words) - 1 and _is_add_x0_x19(words[j]) and words[j] != quest and _is_b(words[j + 1]):
                sites.append((elf.vaddr_to_offset(segs, sel + 4 * (i + 1)), branch))
    return sites[0] if len(sites) == 1 else None


def _engine(ws) -> str | None:
    return next((n for n in ENGINE_LIBS if ws.has(ws.lib(n))), None)


class _SantaCruzGate(Patch):
    gate = ""
    done_word = 0
    order = 47

    def site(self, data: bytes) -> tuple[int, int] | None:
        raise NotImplementedError

    def applies(self, a):
        if a.engine != "Unreal" or "arm64-v8a" not in a.abis:
            return False
        gates = (a.extra or {}).get("unreal_quest_gates")
        return gates is None or self.gate in gates  # analysed by an older FramePort: maybe

    def detect(self, a):
        if self.gate in ((a.extra or {}).get("unreal_quest_gates") or []) and "arm64-v8a" in a.abis:
            return Suggestion(True, self.reason)
        return None

    reason = ""

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        lib = _engine(ws) if ws.abi == "arm64-v8a" else None
        if not lib:
            return False
        data = ws.read_lib(lib)
        found = self.site(data)
        if not found:
            ctx.notes.append(f"{self.id}: {lib} doesn't match the expected code, left unchanged")
            return False
        off, word = found
        if word == self.done_word:
            return False
        buf = bytearray(data)
        struct.pack_into("<I", buf, off, self.done_word)
        ws.put(ws.lib(lib), bytes(buf))
        ctx.notes.append(f"{self.note} ({lib} file offset {off:#x})")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        lib = _engine(ws)
        if not lib or ws.abi != "arm64-v8a":
            return []
        found = self.site(ws.read_lib(lib))
        return [(self.check, bool(found) and found[1] == self.done_word,
                 "" if found else "code not found (different build)")]


class QuestPrecompileDone(_SantaCruzGate):
    id = "frame.unreal_quest_precompile"
    title = "Unreal: don't wait for the Quest shader precompile"
    description = ("ILMxLAB's Unreal games (for example Vader Immortal) wait on their loading card until "
                   "UVRUtils::GetQuestShaderPrecompilePercent reaches 100 %. The precompile only starts on a Quest "
                   "(IsRunningOnSantaCruz); on any other device the function returns 0.0, so the loading card (a "
                   "portrait with a progress bar) never ends. Makes that non-Quest branch return 1.0. The Quest "
                   "branch and the game's normal shader compiling stay as they are. Found by Klownicle (GitHub #49).")
    gate = "quest_precompile"
    done_word = FMOV_S0_ONE
    note = "GetQuestShaderPrecompilePercent: non-Quest branch returns 1.0"
    check = "Quest shader precompile reported finished"
    reason = ("The menu waits for a Quest-only shader precompile that never starts on the Frame (stuck loading card, "
              "for example Vader Immortal).")

    def site(self, data):
        return precompile_site(data)


class QuestKeyMap(_SantaCruzGate):
    id = "frame.unreal_quest_keymap"
    title = "Unreal: Quest controller bindings (RPOC key map)"
    description = ("ILMxLAB's Unreal games (for example Vader Immortal) bind their controls through URPOCKeyMapManager"
                   "Component, which for an Oculus headset picks the Quest (\"SantaCruz\") or the Gear VR key set by "
                   "IsRunningOnSantaCruz. On the Frame it picks Gear VR, which is empty in a Quest build: grip, "
                   "trigger and touch reach Unreal but never the game's hands (grabbing does nothing). Removes the "
                   "branch to the Gear VR set in the Oculus case of the key selector both action and axis mappings "
                   "use. Found by Klownicle (GitHub #49).")
    gate = "rpoc_keymap"
    done_word = NOP
    note = "RPOC key map: Oculus headsets get the Quest key set"
    check = "Quest key set selected for Oculus headsets"
    reason = ("The game's key map picks the empty Gear VR controls when the headset isn't a Quest (grip/trigger do "
              "nothing, for example Vader Immortal).")

    def site(self, data):
        return keymap_site(data)


register(QuestPrecompileDone)
register(QuestKeyMap)
