"""OVRPlugin functions Unreal looks up that OVRPort's OVRPlugin lacks.

Unreal's Oculus module (FOculusHMDModule::InitializeOculusPluginWrapper) looks up every `ovrp_*` function of the
OVRPlugin version it was built against with dlsym on libOVRPlugin.so's handle and ANDs the results: one missing name
and the wrapper fails, OculusHMD never pre-initialises, no OpenXR session starts and the game later crashes without an
HMD (for example Star Wars Pinball VR, UE 4.25 built against OVRPlugin 1.44: OVRPort's OpenXR OVRPlugin has no
ovrp_GetPTWNear). OVRPort replaces the plugin, so the names come from the engine library and are compared with the
plugin the build ships. Each missing one gets a stand-in that returns ovrpFailure (-1000), so callers take their "not
supported" path and never read untouched output arguments. The stand-ins live in a generated library that
libOVRPlugin.so loads (DT_NEEDED): dlsym on a library's handle also searches its dependencies, and the plugin's own
functions are found first, so a later OVRPort that adds a function wins over its stand-in. The engine library stays
byte-identical."""
from __future__ import annotations

from ...analysis import elf
from ...analysis.detect import ovrp_lookups
from ...analysis.stubgen import build_stub_library
from ..base import ApkContext, Patch, Suggestion, register

PLUGIN = "libOVRPlugin.so"
STUBS = "libfp_ovrpstubs.so"
ENGINE_LIBS = ("libUE4.so", "libUnreal.so")
OVRP_FAILURE = -1000  # ovrpResult ovrpFailure
# fallback before OVRPort downloaded a runtime (nothing to compare with yet): names its OpenXR OVRPlugin (3.4.3) is
# known to lack that Unreal builds look up. The build itself always compares with the plugin it ships.
KNOWN_MISSING = ("ovrp_GetPTWNear",)


def _shipped_exports(abi: str) -> set[str] | None:
    """Exports of the newest OVRPort runtime's libOVRPlugin.so, cached per file version (None: none downloaded)."""
    from ...tools import overport

    path = overport.runtime_lib(PLUGIN, abi)
    if not path:
        return None
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _cache.get("key") != key:
        _cache.update(key=key, exports=elf.dyn_symbols(path.read_bytes(), True))
    return _cache["exports"]


_cache: dict = {}


def missing_lookups(lookups, plugin_exports) -> list[str]:
    return sorted(set(lookups) - set(plugin_exports))


class UnrealOvrpEntrypoints(Patch):
    id = "frame.unreal_ovrp_entrypoints"
    title = "Unreal: stand-ins for OVRPlugin functions OVRPort lacks"
    description = ("Unreal's Oculus module looks up every OVRPlugin function it was built against and starts no VR "
                   "when one is missing; OVRPort's OpenXR OVRPlugin lacks some old ones (for example ovrp_GetPTWNear, "
                   "which Unreal 4.25 builds like Star Wars Pinball VR look up). The game then never starts an OpenXR "
                   "session and crashes a few seconds in. Adds a generated library to libOVRPlugin.so with a stand-in "
                   "for each missing function that reports failure (ovrpFailure), so Unreal's Oculus support starts. "
                   "The functions OVRPort has are untouched.")
    order = 46

    def applies(self, a):
        return a.engine == "Unreal" and PLUGIN in a.libs and "arm64-v8a" in a.abis

    def detect(self, a):
        lookups = (a.extra or {}).get("unreal_ovrp_lookups") or []
        if not self.applies(a) or not lookups:
            return None
        exports = _shipped_exports("arm64-v8a")
        if exports is None:
            missing = sorted(set(lookups) & set(KNOWN_MISSING))
        else:
            missing = missing_lookups(lookups, exports)
        if not missing:
            return None
        return Suggestion(True, f"Unreal looks up OVRPlugin function(s) OVRPort's OVRPlugin lacks: "
                                f"{', '.join(missing[:6])}{' …' if len(missing) > 6 else ''}. Without them Unreal "
                                "starts no VR.")

    def _missing(self, ctx: ApkContext) -> list[str]:
        ws = ctx.ws
        lookups = (ctx.analysis.extra or {}).get("unreal_ovrp_lookups")
        if not lookups:  # analysed by an older FramePort: read the engine library itself
            engine = next((ws.lib(n) for n in ENGINE_LIBS if ws.has(ws.lib(n))), None)
            lookups = ovrp_lookups(ws.read(engine)) if engine else []
        return missing_lookups(lookups, elf.dyn_symbols(ws.read_lib(PLUGIN), True))

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a" or not ws.has(ws.lib(PLUGIN)):
            return False
        missing = self._missing(ctx)
        if not missing:
            return False
        plugin = ws.read_lib(PLUGIN)
        stubs = build_stub_library(missing, STUBS, ws.abi, result=OVRP_FAILURE)
        changed = False
        if STUBS not in elf.needed(plugin):
            ws.put(ws.lib(PLUGIN), elf.add_needed(plugin, STUBS))
            changed = True
        if not ws.has(ws.lib(STUBS)) or ws.read_lib(STUBS) != stubs:
            ws.put(ws.lib(STUBS), stubs)
            changed = True
        if changed:
            ctx.notes.append(f"{len(missing)} OVRPlugin stand-in(s) returning ovrpFailure: {', '.join(missing)}")
        return changed

    def validate(self, ctx):
        ws = ctx.ws
        if not ws.has(ws.lib(PLUGIN)):
            return []
        plugin = ws.read_lib(PLUGIN)
        exports = set(elf.dyn_symbols(plugin, True))
        if STUBS in elf.needed(plugin) and ws.has(ws.lib(STUBS)):
            exports |= elf.dyn_symbols(ws.read_lib(STUBS), True)
        lookups = (ctx.analysis.extra or {}).get("unreal_ovrp_lookups") or []
        unresolved = missing_lookups(lookups, exports)
        return [("OVRPlugin functions Unreal looks up resolvable", not unresolved, ", ".join(unresolved[:5]))]


register(UnrealOvrpEntrypoints)
