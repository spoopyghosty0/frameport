"""Meta Platform SDK tracer (diagnostics): logs the game's ovr_* calls and their answers (native/ovrtrace)."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, register
from . import artifact

LIB = "libfp_ovrtrace.so"
SKIP = ("libovrplatformloader", "libfp_", "libframe", "libopenxr", "libOVRPlugin", "libvrapi", "libc++")


def ovr_importers(ws) -> list[str]:
    """The game's libraries that call Meta Platform SDK functions (ovr_* imports)."""
    out = []
    for lib in ws.libs():
        if lib.startswith(SKIP):
            continue
        data = ws.read_lib(lib)
        if elf.is_elf(data) and any(s.startswith("ovr_") for s in elf.dyn_symbols(data, False)):
            out.append(lib)
    return out


class OvrTrace(Patch):
    id = "frame.ovr_trace"
    title = "Trace Meta platform requests (diagnostics)"
    description = ("Logs every Meta Platform SDK request the game makes (user, entitlement, cloud saves, ...) and the "
                   "messages that answer them, and every 10 s the requests that never got an answer (logcat tag "
                   "fp_ovrtrace, in launch.log). For games that wait forever on a loading screen (for example Vader "
                   "Immortal after its intro). Changes nothing: every call goes to the real function.")
    order = 95
    needs_vr = False
    experimental = True

    def applies(self, a):
        return "arm64-v8a" in a.abis and "libovrplatformloader.so" in a.libs

    def detect(self, a):
        return None  # diagnostics only, on request

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a":
            return False
        libs = ovr_importers(ws)
        for lib in libs:
            data = ws.read_lib(lib)
            if LIB not in elf.needed(data):
                ws.put(ws.lib(lib), elf.add_needed(data, LIB))
        if libs:
            ws.put(ws.lib(LIB), artifact(ws.abi, LIB))
            ctx.notes.append(f"Meta platform calls traced in {', '.join(libs)}")
        return bool(libs)

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        traced = []
        for lib in ws.libs():
            data = b"" if lib.startswith(SKIP) else ws.read_lib(lib)
            if elf.is_elf(data) and LIB in elf.needed(data):
                traced.append(lib)
        return [("Platform tracer loaded", bool(traced) and ws.has(ws.lib(LIB)), ", ".join(traced))] if traced else []


register(OvrTrace)
