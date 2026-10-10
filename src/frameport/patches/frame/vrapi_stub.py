"""Meta's own VrApi loader left in an OpenXR game: replaced by a library that answers every VrApi call with 0.

Some Unity games ship Meta's libvrapi.so next to an OpenXR OVRPlugin (for example Jurassic World Aftermath, OVRPlugin
1.89.1). OVRPlugin runs on OpenXR, but still calls a few VrApi functions (vrapi_SetPropertyInt) without ever calling
vrapi_Initialize; Meta's loader aborts on that ("vrapi_SetPropertyInt was called before vrapi_Initialize()!", SIGABRT
on UnityMain right after the OpenXR instance is created). The VrApi bridge can't take its place: it implements a
different, smaller set of functions (39 of the loader's 114), so OVRPlugin wouldn't link at all.
"""
from __future__ import annotations

from ...analysis import elf
from ...analysis.stubgen import build_stub_library
from ..base import ApkContext, Patch, register

VRAPI = "libvrapi.so"


class VrApiStub(Patch):
    id = "frame.vrapi_stub"
    title = "Quiet Meta's VrApi loader (OpenXR games)"
    description = ("Some OpenXR games still ship Meta's VrApi loader and call it a few times without starting VrApi "
                   "(for example Jurassic World Aftermath); Meta's loader then stops the game (\"vrapi_SetPropertyInt "
                   "was called before vrapi_Initialize()\"). Replaces it with a library that has the same functions, "
                   "each doing nothing. Only for games whose VR runs through OVRPlugin on OpenXR.")
    order = 59
    conflicts = ("frame.vrapi_bridge",)

    def applies(self, a):
        return VRAPI in a.libs and "libOVRPlugin.so" in a.libs and not a.direct_vrapi and "arm64-v8a" in a.abis

    def detect(self, a):
        return None  # suggested by triage (signature vrapi-before-init): most such games never call it

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a" or not ws.has(ws.lib(VRAPI)):
            return False
        data = ws.read_lib(VRAPI)
        if not elf.is_elf(data):
            return False
        exports = sorted(s for s in elf.dyn_symbols(data, True) if s.startswith(("vrapi_", "ovr")))
        stub = build_stub_library(exports, soname=VRAPI)
        if not exports or data == stub:
            return False  # nothing to stand in for, or already the stub
        ws.put(ws.lib(VRAPI), stub)
        ctx.notes.append(f"{VRAPI}: {len(exports)} functions answer 0 (Meta's loader aborted calls made before init)")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        if not ws.has(ws.lib(VRAPI)) or not ws.has(ws.lib("libOVRPlugin.so")):
            return []
        have = elf.dyn_symbols(ws.read_lib(VRAPI), True)
        need = {s for s in elf.dyn_symbols(ws.read_lib("libOVRPlugin.so"), False) if s.startswith("vrapi_")}
        return [("OVRPlugin's VrApi functions present", need <= have, ", ".join(sorted(need - have)[:5]))]


register(VrApiStub)
