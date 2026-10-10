"""Depth formats for LTW, the GL-to-GLES wrapper of Minecraft launchers (QuestCraft): native/glshim/eglfmt.c."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, Suggestion, register
from . import artifact

LTW = "libltw.so"
EGL = "libEGL.so"
SHIM = "libfpg.so"  # not longer than "libEGL.so": LTW's dlopen string is rewritten in place (.rodata only)


class LtwDepthFormats(Patch):
    id = "frame.ltw_depth"
    needs_vr = False
    title = "GL wrapper: OpenGL ES depth formats (Minecraft)"
    description = ("Minecraft asks for desktop OpenGL's GL_DEPTH_COMPONENT32 depth textures. Some builds of LTW, the "
                   "OpenGL-to-OpenGL ES wrapper in Minecraft launchers such as QuestCraft, pass that on unchanged; "
                   "Quest's driver accepts it, the Frame's Mesa rejects it (GL_INVALID_OPERATION in glTexImage2D), so "
                   "the depth buffer never exists and the picture stays black. LTW then loads OpenGL ES through a "
                   "small shim that asks for GL_DEPTH_COMPONENT32F (or 24-bit) instead.")
    order = 74
    default_on = True

    def applies(self, a):
        return LTW in a.libs

    def detect(self, a):
        if self.applies(a):
            return Suggestion(True, "Ships LTW (Minecraft's OpenGL-to-OpenGL ES wrapper): Minecraft's 32-bit depth "
                                    "textures need the OpenGL ES format on the Frame (for example QuestCraft).")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a" or not ws.has(ws.lib(LTW)):
            return False
        data, count = elf.replace_rodata_string(ws.read_lib(LTW), EGL, SHIM)
        if not count:
            return False
        ws.put(ws.lib(LTW), data)
        ws.put(ws.lib(SHIM), artifact(ws.abi, SHIM))
        ctx.notes.append(f"{LTW} loads OpenGL ES through {SHIM}")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        if not ws.has(ws.lib(LTW)):
            return []
        uses = b"\0" + SHIM.encode() + b"\0" in ws.read_lib(LTW)
        return [("LTW loads the depth-format shim", uses and ws.has(ws.lib(SHIM)), SHIM)]


register(LtwDepthFormats)
