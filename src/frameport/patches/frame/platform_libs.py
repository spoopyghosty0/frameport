"""Meta platform SDK gaps in OVRPort's libovrplatformloader.so."""
from __future__ import annotations

from ...analysis import elf
from ...analysis.detect import missing_ovr_symbols
from ...analysis.stubgen import build_stub_library
from ..base import ApkContext, Patch, Suggestion, register
from . import artifact

LOADER = "libovrplatformloader.so"
COMPAT = "libovrplatformcompat.so"
STUBS = "libovrstubs.so"


def _lib_bytes(ws) -> dict[str, bytes]:
    out = {}
    for lib in ws.libs():
        data = ws.read_lib(lib)
        if elf.is_elf(data):
            out[lib] = data
    return out


class PlatformCompat(Patch):
    id = "frame.ovrplatformcompat"
    title = "Platform compat (ovrMessageType_ToString)"
    description = ("Adds a real ovrMessageType_ToString (OVRPort's platform compat library) for games whose platform "
                   "loader lacks it (e.g. The Climb 2). OVRPort 1.2.5+ adds the same library itself; then this is "
                   "skipped.")
    order = 30
    default_on = True
    on_pc = True

    def detect(self, a):
        return Suggestion(True, "Applied automatically when the OVRPort build needs it.")

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a" or not ws.has(ws.lib(LOADER)) or ws.has(ws.lib(COMPAT)):
            return False
        if "ovrMessageType_ToString" not in missing_ovr_symbols(_lib_bytes(ws)):
            return False
        ws.put(ws.lib(LOADER), elf.add_needed(ws.read_lib(LOADER), COMPAT))
        ws.put(ws.lib(COMPAT), artifact(ws.abi, COMPAT))
        return True


class OvrStubs(Patch):
    id = "frame.ovrstubs"
    title = "Stub missing Meta platform functions"
    description = ("Generates no-op stubs for ovr_* functions the game imports but OVRPort's platform loader "
                   "lacks (e.g. Espire 1/2, Wallace & Gromit, The Climb 2). Symptom: UnsatisfiedLinkError / "
                   "'cannot locate symbol ovr_...'. Online/store features stay unavailable.")
    order = 31
    default_on = True
    on_pc = True  # missing Meta platform symbols crash the game in any Android, not only on the Frame

    def detect(self, a):
        return Suggestion(True, "Applied automatically when the OVRPort build needs it.")

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if not ws.has(ws.lib(LOADER)):
            return False
        loader = ws.read_lib(LOADER)
        if ws.has(ws.lib(STUBS)):
            if STUBS in elf.needed(loader):
                return False  # already linked (an earlier build)
            # the stand-ins exist but nothing loads them (the loader was replaced, e.g. a converted APK converted
            # again): link them again
            ws.put(ws.lib(LOADER), elf.add_needed(loader, STUBS))
            ctx.notes.append("linked the existing stand-ins again")
            return True
        missing = missing_ovr_symbols(_lib_bytes(ws))
        if not missing:
            return False
        ws.put(ws.lib(LOADER), elf.add_needed(loader, STUBS))
        ws.put(ws.lib(STUBS), build_stub_library(sorted(missing), STUBS, ws.abi))
        ctx.notes.append(f"stubbed {len(missing)} function(s)")
        return True

    def validate(self, ctx):
        missing = missing_ovr_symbols(_lib_bytes(ctx.ws))
        return [("Meta platform symbols resolvable", not missing, ", ".join(sorted(missing)[:5]))]


register(PlatformCompat)
register(OvrStubs)
