"""Controller battery level for OVRPlugin games (GitHub #572).

OVRPlugin's controller state (ovrp_GetControllerState4/5/6) carries each Touch controller's battery level. OVRPort's
OpenXR OVRPlugin has no source for it on the Frame and reports 0 %, so games that show or check it warn about empty
controllers all the time (In Death: Unchained through Unreal's OculusTouch_*_BatteryPercentRemaining keys). The
library native/ovrpbattery (`libfp_ovrpbattery.so`) wraps those three functions and reports 100 % for a connected
Touch controller whose level reads 0; everything else, and a real level, passes unchanged. It is added to
libOVRPlugin.so's DT_NEEDED and the plugin's own three exports are made STB_LOCAL (`elf.hide_exports`, nothing moves),
so Unity's P/Invoke, Unreal's dlsym on the plugin and plain imports all reach the wrapper; the wrapper finds the
plugin's originals in its in-memory .dynsym (as native/langpack does for the platform loader). Cosmetic, so opt-in:
it changes libOVRPlugin.so, which every OVRPlugin game would then have to reinstall."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, register
from . import artifact

PLUGIN = "libOVRPlugin.so"
LIB = "libfp_ovrpbattery.so"
# keep equal to the WRAP(N) list in native/ovrpbattery/ovrpbattery.c (tests/test_framebridge_round2.py checks)
EXPORTS = ("ovrp_GetControllerState4", "ovrp_GetControllerState5", "ovrp_GetControllerState6")


class OvrpControllerBattery(Patch):
    id = "frame.ovrp_controller_battery"
    title = "Report full controller batteries to OVRPlugin games"
    description = ("OVRPort's OVRPlugin reports 0 % battery for both controllers on the Frame, so games that show or "
                   "check the level warn about empty controllers all the time (for example In Death: Unchained). "
                   "Adds a small library to libOVRPlugin.so that reports 100 % for a connected controller whose "
                   "level reads 0 (ovrp_GetControllerState4/5/6); every other value passes unchanged.")
    order = 47

    def applies(self, a):
        return PLUGIN in a.libs and "arm64-v8a" in a.abis

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a" or not ws.has(ws.lib(PLUGIN)):
            return False
        plugin = ws.read_lib(PLUGIN)
        changed = False
        if LIB not in elf.needed(plugin):
            patched, hidden = elf.hide_exports(plugin, EXPORTS)
            if not hidden:
                ctx.notes.append(f"{PLUGIN} exports none of {', '.join(EXPORTS)}: battery level left alone")
                return False
            ws.put(ws.lib(PLUGIN), elf.add_needed(patched, LIB, first=False))
            ctx.notes.append(f"controller battery level from {LIB} ({', '.join(hidden)})")
            changed = True
        lib = artifact(ws.abi, LIB)
        if not ws.has(ws.lib(LIB)) or ws.read_lib(LIB) != lib:
            ws.put(ws.lib(LIB), lib)
            changed = True
        return changed

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        if not ws.has(ws.lib(PLUGIN)):
            return []
        plugin = ws.read_lib(PLUGIN)
        _, visible = elf.hide_exports(plugin, EXPORTS)  # dry run: the names still visible to lookups
        linked = LIB in elf.needed(plugin) and ws.has(ws.lib(LIB))
        return [("Controller battery wrapper linked (plugin's own state functions hidden)", linked and not visible,
                 ", ".join(sorted(visible)))]


register(OvrpControllerBattery)
