"""Single-view draws of OVR_multiview programs into ordinary framebuffers (native/glmv, GitHub #77)."""
from __future__ import annotations

from ...analysis import elf
from ...analysis.detect import multiview_glsl_libs
from ..applicability import is_gles
from ..base import ApkContext, Patch, Suggestion, register
from . import artifact

SHIM = "libfpglmv.so"  # as long as "libGLESv3.so": the engine's dlopen string is rewritten in place
GLES = "libGLESv3.so"


def engine_libs(ws, analysis) -> list[str]:
    """The game's libraries with OVR_multiview GLSL (from the analysis; scanned again when it predates the field)."""
    names = (analysis.extra or {}).get("gl_multiview_libs")
    if names is None:
        names = multiview_glsl_libs({n: ws.read_lib(n) for n in analysis.libs if ws.has(ws.lib(n))})
    return [n for n in names if ws.has(ws.lib(n))]


class GlMultiviewFbo(Patch):
    id = "frame.gl_multiview_fbo"
    title = "OpenGL ES: multiview shaders on flat panels (HUD, menus)"
    description = (
        "Some OpenGL ES engines compile every shader for multiview (both eyes in one draw, `num_views=2`) and use the "
        "same shaders to draw into ordinary single-layer framebuffers, for example a HUD or PDA screen rendered to a "
        "texture. Quest's driver draws them anyway; the Frame's driver (Mesa) follows the OVR_multiview rule and "
        "silently drops every such draw, so those panels stay black (for example Doom3Quest's HUD and PDA). Loads "
        "OpenGL ES through a small interposer (libfpglmv.so) that draws those cases with a single-view copy of the "
        "shader program (left-eye view; uniforms copied before each such draw). Eye-buffer draws are unchanged. Logs "
        "under the tag GLMV; the setting gl_mv_debug adds error checks and counters.")
    order = 63
    experimental = True

    def applies(self, a):
        return (a.engine == "Other" and is_gles(a) and "arm64-v8a" in a.abis
                and bool((a.extra or {}).get("gl_multiview_libs")))

    def detect(self, a):
        if self.applies(a):
            return Suggestion(False, "Own OpenGL ES engine with multiview shaders: turn on if HUD, menu or other flat "
                                     "panels stay black on the Frame (for example Doom3Quest's HUD and PDA).")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a":
            return False
        changed = False
        for lib in engine_libs(ws, ctx.analysis):
            data = ws.read_lib(lib)
            if not elf.is_elf(data):
                continue
            patched = elf.add_needed(data, SHIM)  # direct gl* imports bind to the interposer first
            patched, count = elf.replace_rodata_string(patched, GLES, SHIM)  # dlopen + dlsym tables
            if patched == data:
                continue
            ws.put(ws.lib(lib), patched)
            ctx.notes.append(f"{lib} draws through {SHIM} ({count} libGLESv3.so load(s) redirected)")
            changed = True
        if changed:
            shim = artifact(ws.abi, SHIM)
            if not ws.has(ws.lib(SHIM)) or ws.read_lib(SHIM) != shim:
                ws.put(ws.lib(SHIM), shim)
        return changed

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        users = [lib for lib in ((ctx.analysis.extra or {}).get("gl_multiview_libs") or [])
                 if ws.has(ws.lib(lib)) and SHIM in elf.needed(ws.read_lib(lib))]
        return [("Multiview interposer present", ws.has(ws.lib(SHIM)), SHIM)] if users else []


register(GlMultiviewFbo)
