"""Vulkan shim that drops invalid pNext pointers from render-pass create infos (native/vkshim)."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, Suggestion, register
from . import artifact

SHIM = "libfp_vk.so"  # not longer than "libvulkan.so": the engine's dlopen string is rewritten in place
VULKAN = "libvulkan.so"
ENGINE_LIBS = ("libUE4.so", "libUnreal.so")
# not the engine: XR/Meta/FramePort libraries that may name libvulkan.so too
NOT_ENGINE = ("libopenxr", "libOVR", "libovr", "libvrapi", "libfp_", "libframe", "libVkLayer", "libc++", "libunity")


def engine_libs(ws, analysis) -> list[str]:
    """The game's libraries that load Vulkan by name: Unreal's engine library, or for other engines (own engines,
    e.g. Roblox) every library of the game naming libvulkan.so. Unity loads Vulkan differently and isn't touched."""
    if analysis.engine == "Unreal":
        return [lib for lib in ENGINE_LIBS if ws.has(ws.lib(lib))]
    if analysis.engine != "Other":
        return []
    out = []
    for lib in analysis.libs:
        if lib.startswith(NOT_ENGINE) or not ws.has(ws.lib(lib)):
            continue
        if b"\0" + VULKAN.encode() + b"\0" in ws.read_lib(lib):
            out.append(lib)
    return out


class VulkanSanitize(Patch):
    id = "frame.vk_sanitize"
    title = "Vulkan: drop invalid render-pass pointers"
    description = ("Some engines leave the pNext of unused Vulkan attachment references uninitialized. The Frame's "
                   "driver never reads it, but Lepton always loads Steam's Fossilize shader-cache layer, which follows "
                   "it and crashes on the first frame (SIGSEGV in libVkLayer_fossilize.so from FVulkanRenderPass, "
                   "for example Deadpool VR). Loads Vulkan through a small shim that keeps valid pointers and drops "
                   "only unreadable ones or ones pointing at the wrong structure type. It also drops a depth resolve "
                   "named in a subpass without a depth attachment, which crashes the Frame's driver at the first "
                   "render pass (for example Metro Awakening).")
    order = 72
    default_on = True

    def detect(self, a):
        if a.engine == "Other":  # changes nothing unless one of its libraries loads Vulkan by name
            return Suggestion(True, "Own-engine game: applied when one of its libraries loads Vulkan by name (for "
                                    "example Roblox); keeps Lepton's Fossilize layer from crashing on uninitialized "
                                    "pointers.")
        if a.engine == "Unreal":
            return Suggestion(True, "Unreal game: applied automatically when the engine loads Vulkan by name; keeps "
                                    "Lepton's Fossilize layer from crashing on uninitialized pointers "
                                    "(for example Deadpool VR).")
        return None

    def applies(self, a):
        return a.engine in ("Unreal", "Other")

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a":
            return False
        changed = False
        for lib in engine_libs(ws, ctx.analysis):
            data, count = elf.replace_rodata_string(ws.read_lib(lib), VULKAN, SHIM)
            if count:
                ws.put(ws.lib(lib), data)
                ctx.notes.append(f"{lib} loads Vulkan through {SHIM}")
                changed = True
        if changed:
            shim = artifact(ws.abi, SHIM)
            if not ws.has(ws.lib(SHIM)) or ws.read_lib(SHIM) != shim:
                ws.put(ws.lib(SHIM), shim)
        return changed

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        uses = [lib for lib in ctx.analysis.libs if not lib.startswith(NOT_ENGINE) and ws.has(ws.lib(lib))
                and b"\0" + SHIM.encode() + b"\0" in ws.read_lib(lib)] if ctx.analysis.engine == "Other" else [1]
        return [("Vulkan shim present", ws.has(ws.lib(SHIM)), SHIM)] if uses else []


register(VulkanSanitize)
