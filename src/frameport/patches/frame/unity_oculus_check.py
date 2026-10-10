"""Unity 2017-era built-in Oculus support starts its VR device only on a Quest/Go: libunity.so asks Android's package
manager for com.oculus.systemactivities (Meta's system UI) and, when the lookup fails, quietly falls back to its "None"
VR device. Lepton's Android has no such package, so the game ran as a plain 2D app that Lepton never shows (only the
Android home screen, e.g. Accounting+, Unity 2017.4). The package name in libunity.so is pointed at "android" (always
installed): a same-length, in-place string edit; everything else about the check stays as it is."""
from __future__ import annotations

import os

from ...analysis import elf
from ..base import ApkContext, Patch, Suggestion, register
from . import artifact

PACKAGE = "com.oculus.systemactivities"
ALWAYS_THERE = "android"
# Unity's legacy frame loop never calls ovrp_WaitToBeginFrame: its ovrp_Update2 lookup goes to native/ovrpshim
SHIM = "libfp_ovrp.so"
UPDATE, SHIM_UPDATE = "ovrp_Update2", "fpov_Update2"
# the shim follows Unity's frame begins/ends so its frame wait never blocks for a frame Unity didn't begin (Sniper Elite
# VR deadlocked at a scene switch: xrWaitFrame waits for the previous frame's xrBeginFrame)
BEGIN, SHIM_BEGIN = "ovrp_BeginFrame", "fpov_BeginFrame"
END, SHIM_END = "ovrp_EndFrame", "fpov_EndFrame"
# Unity creates its XR controller devices for the hand nodes OVRPlugin reports present (logged/fixed by the shim)
NODE, SHIM_NODE = "ovrp_GetNodePresent", "fpov_GetNodePresent"
# input diagnostics (off): the C# P/Invoke names in libil2cpp.so pointed at the shim's wrappers, which log what they
# return, report input focus as true and release buttons held > 2 s. Accounting+ still didn't pass "press any button"
# with them (input reached the game cleanly), so they stay off; switch on to investigate another game.
INPUT_PROBE = os.environ.get("FRAMEPORT_INPUT_PROBE") == "1"  # diagnostic builds only
INPUT_CALLS = ("ovrp_GetConnectedControllers", "ovrp_GetControllerState4", "ovrp_GetControllerState2",
               "ovrp_GetAppHasInputFocus")
UNITY_INPUT_CALLS = ("ovrp_GetControllerState", "ovrp_GetControllerState2")


ORR_W2_6 = 0x321F07E2  # orr w2, wzr, #6   (strncmp length 6)
MOV_W2_0 = 0x52800002  # mov w2, #0        (strncmp length 0: always equal)


def oculus_model_checks(data: bytes) -> tuple[bytes, int]:
    """Unity's built-in Oculus input only reports controllers on a device whose model starts with "Oculus"
    (strncmp(SystemInfo.deviceModel, "Oculus", 6), next to its Oculus Go check "Oculus Pacific"). Lepton's Android is
    "Valve Lepton", so BattleSisters got no controller buttons. Every such compare (the literal loaded with adrp/add
    right before `orr w2, wzr, #6`) gets length 0, so any device but the Go counts as an Oculus one. The "Oculus"
    string itself stays: Unity also uses it as its VR device name."""
    import struct

    literal = data.find(b"\0Oculus\0") + 1
    if literal <= 0:
        return data, 0
    buf, count = bytearray(data), 0
    end = len(data) - len(data) % 4
    for at in range(0, end, 4):
        if struct.unpack_from("<I", data, at)[0] != ORR_W2_6:
            continue
        # look back for `adrp xA, page` + `add x1, xA, #off` that computes the literal's address
        for back in range(4, 16, 4):
            add = struct.unpack_from("<I", data, at - back)[0] if at >= back else 0
            if add & 0xFFC00000 != 0x91000000 or add & 0x1F != 1:  # add x1, xN, #imm (no shift)
                continue
            rn, imm12 = (add >> 5) & 0x1F, (add >> 10) & 0xFFF
            adrp_at = at - back - 4
            adrp = struct.unpack_from("<I", data, adrp_at)[0] if adrp_at >= 0 else 0
            if adrp & 0x9F000000 != 0x90000000 or adrp & 0x1F != rn:
                continue
            imm = ((adrp >> 5) & 0x7FFFF) << 2 | (adrp >> 29) & 3
            if imm & (1 << 20):
                imm -= 1 << 21
            if (adrp_at & ~0xFFF) + (imm << 12) + imm12 == literal:
                struct.pack_into("<I", buf, at, MOV_W2_0)
                count += 1
            break
    return bytes(buf), count


class UnityOculusCheck(Patch):
    id = "frame.unity_oculus_check"
    title = "Unity: start VR without Meta's system apps"
    description = ("Unity's built-in Oculus support (Unity 2017–2019) only starts VR when Android has Meta's "
                   "com.oculus.systemactivities package; without it the game runs as a 2D app (the Android home "
                   "screen or a black window, for example Accounting+, BattleSisters). Points that package name in "
                   "libunity.so at \"android\", which always exists. Games on Unity's built-in VR (2017–2018, and 2019 "
                   "without the Oculus XR Plugin) also get the frame wait their legacy frame loop never makes "
                   "(libfp_ovrp.so calls ovrp_WaitToBeginFrame before ovrp_Update2; without it no frame starts, the "
                   "dashboard freezes or the GPU hangs). Per game, the settings ovrp_begin_gate (a freeze at the first "
                   "scene switch) and ovrp_hold_physics (hands trailing the controllers) switch on two more frame-loop "
                   "fixes (for example Sniper Elite VR). The shim also "
                   "counts a newly pressed trigger or A/B/X/Y as a mouse click (Input.GetMouseButtonDown), which "
                   "Go-era screens wait for (for example Accounting+'s motion "
                   "warning) and which Lepton never delivers.")
    order = 45
    # 2: frame wait also for Unity 2019 without the Oculus XR Plugin; 3: controller presses as mouse clicks
    # (ovrpshim); 4: Unity 2019's Oculus device-model checks (Touch controllers on Lepton)
    # 5: frame begins gate the frame wait (no deadlock when Unity skips a frame); physics-step pose
    # updates kept from OVRPlugin (they located poses in the past: hands lagged); both only with the per-game settings
    # ovrp_begin_gate / ovrp_hold_physics (BattleSisters' hands lagged with them)
    revision = 5

    @staticmethod
    def _major(a) -> int:
        return int(str((a.extra or {}).get("unity_version") or "0").split(".")[0] or 0)

    @classmethod
    def legacy_loop(cls, a) -> bool:
        """Unity's built-in Oculus VR drives OVRPlugin without waiting for frames: Unity 2017-2018, and 2019 games
        without the Oculus XR Plugin (BattleSisters flooded "outside of frame bounds" and hung the GPU). With
        libOculusXRPlugin.so (XR Plugin Management, for example Lucky's Tale) the plugin waits itself."""
        major = cls._major(a)
        return 0 < major < 2019 or (major == 2019 and "libOculusXRPlugin.so" not in a.libs)

    def applies(self, a):
        # any Unity with built-in Oculus support whose libunity.so has the check (BattleSisters, Unity 2019.4, stayed
        # a 2D app without it); Lucky's Tale (2019.4) runs either way
        return a.engine == "Unity" and "libOVRPlugin.so" in a.libs and bool((a.extra or {}).get("unity_oculus_check"))

    def detect(self, a):
        if self.applies(a):
            return Suggestion(True, "Unity with built-in Oculus support: it checks for Meta's system apps before "
                                    "starting VR, else it runs as a 2D app (for example Accounting+, BattleSisters).")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        name = ws.lib("libunity.so")
        if not ws.has(name):
            return False
        data, count = elf.replace_rodata_string(ws.read(name), PACKAGE, ALWAYS_THERE)
        if not count:
            return False
        data, loops = elf.replace_rodata_string(data, UPDATE, SHIM_UPDATE) if self.legacy_loop(ctx.analysis) \
            else (data, 0)
        if loops:
            data, begins = elf.replace_rodata_string(data, BEGIN, SHIM_BEGIN)
            if begins:  # the end goes with it: a begin the shim gave the waited frame's index ends with that index
                data, _ = elf.replace_rodata_string(data, END, SHIM_END)
                ctx.notes.append(f"libunity.so: {BEGIN}/{END} -> {SHIM_BEGIN}/{SHIM_END}")
        if loops and self._major(ctx.analysis) >= 2019:  # Accounting+ (2017) reads OVRInput: works without
            data, models = oculus_model_checks(data)
            if models:
                ctx.notes.append(f"libunity.so: {models} Oculus device-model checks accept Lepton's model")
            data, nodes = elf.replace_rodata_string(data, NODE, SHIM_NODE)
            if nodes:
                ctx.notes.append(f"libunity.so: {NODE} -> {SHIM_NODE}")
        plugin = ws.lib("libOVRPlugin.so")
        if loops and ws.abi == "arm64-v8a" and ws.has(plugin):
            ovrp = ws.read(plugin)
            if SHIM.encode() not in ovrp:
                ws.put(plugin, elf.add_needed(ovrp, SHIM))
            ws.put(ws.lib(SHIM), artifact(ws.abi, SHIM))
            ctx.notes.append(f"libunity.so: {UPDATE} -> {SHIM_UPDATE} ({SHIM} waits for each frame)")
        il2cpp = ws.lib("libil2cpp.so")
        if loops and INPUT_PROBE and ws.has(il2cpp):  # diagnostics: the game's C# input calls go through the shim
            code, probes = ws.read(il2cpp), 0
            for real in INPUT_CALLS:
                code, n = elf.replace_rodata_string(code, real, "fpov_" + real[5:])
                probes += n
            if probes:
                ws.put(il2cpp, code)
                ctx.notes.append(f"libil2cpp.so: {probes} input calls logged by {SHIM}")
        if loops and INPUT_PROBE:  # Unity's own input reads too
            for real in UNITY_INPUT_CALLS:
                data, n = elf.replace_rodata_string(data, real, "fpov_" + real[5:])
                if n:
                    ctx.notes.append(f"libunity.so: {real} logged by {SHIM}")
        ws.put(name, data)
        ctx.notes.append(f"libunity.so: {PACKAGE} -> {ALWAYS_THERE} ({count}x)")
        return True


register(UnityOculusCheck)
