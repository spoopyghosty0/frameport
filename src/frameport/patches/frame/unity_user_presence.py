"""Unity's Oculus XR Plugin gives its HMD input device a UserPresence feature from OVRPlugin (ovrp_GetUserPresent2).
On the Frame, OVRPort's OVRPlugin reports the headset as not worn a few seconds after start, while it is worn (BONELAB,
right after Unity switches XR loaders). Games that drive their player rig only while the user is present then show a
frozen body: no head tracking, controllers stuck to the model, no buttons (BONELAB's Marrow rig checks
XRHMD.IsUserPresent every frame). The plugin's lookup of that function is pointed at native/ovrpshim, which reports
the user as present (the session's focus still pauses the game when the headset is taken off)."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, register
from . import artifact
from .unity_oculus_check import SHIM

XR_PLUGIN = "libOculusXRPlugin.so"
CALL, SHIM_CALL = "ovrp_GetUserPresent2", "fpov_GetUserPresent2"  # same length: an in-place string edit


class UnityUserPresence(Patch):
    id = "frame.unity_user_presence"
    title = "Unity: headset always counts as worn"
    description = ("Unity's Oculus XR Plugin asks OVRPlugin whether the headset is worn; on the Frame the answer "
                   "turns to \"not worn\" a few seconds after start. Games that only move the player while the "
                   "headset is worn then show a frozen body with no head tracking and no controls (for example "
                   "BONELAB). Reports the headset as worn; taking it off still pauses the game through the session's "
                   "focus.")
    order = 46
    revision = 1

    def applies(self, a):
        return a.engine == "Unity" and "arm64-v8a" in a.abis and XR_PLUGIN in a.libs and "libOVRPlugin.so" in a.libs

    def detect(self, a):
        return None  # per game (catalog): most games don't depend on user presence

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        xr, plugin = ws.lib(XR_PLUGIN), ws.lib("libOVRPlugin.so")
        if ws.abi != "arm64-v8a" or not ws.has(xr) or not ws.has(plugin):
            return False
        data, count = elf.replace_rodata_string(ws.read(xr), CALL, SHIM_CALL)
        if not count:
            return False
        ws.put(xr, data)
        ovrp = ws.read(plugin)
        if SHIM not in elf.needed(ovrp):  # the plugin's dlsym on OVRPlugin's handle also searches its dependencies
            ws.put(plugin, elf.add_needed(ovrp, SHIM))
        ws.put(ws.lib(SHIM), artifact(ws.abi, SHIM))
        ctx.notes.append(f"{XR_PLUGIN}: {CALL} -> {SHIM_CALL} ({SHIM} reports the user as present)")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        xr, plugin = ws.lib(XR_PLUGIN), ws.lib("libOVRPlugin.so")
        if not ws.has(xr) or SHIM_CALL.encode() not in ws.read(xr):
            return []
        linked = ws.has(plugin) and SHIM in elf.needed(ws.read(plugin)) and ws.has(ws.lib(SHIM))
        return [("User presence shim loaded", linked, SHIM)]


register(UnityUserPresence)
