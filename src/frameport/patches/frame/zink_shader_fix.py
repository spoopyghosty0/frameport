"""Shader fixes for OpenGL ES games: a Vulkan layer between Zink and the Frame's driver (native/zinkfix).

On the Frame, OpenGL ES runs on Zink (Mesa's GL on Vulkan), which turns the game's GLSL into SPIR-V inside the driver.
The Vulkan shim's vk_shader_fix only reaches engines that load Vulkan themselves, so GLES games get this layer instead:
it inserts words into the SPIR-V modules whose size and SHA-256 match the adapter setting zink_shader_fix (same format
as vk_shader_fix) and passes every other module through. The engine library loads the layer library first
(DT_NEEDED); its constructor adds the layer to the process's Vulkan debug layer list (Android's GraphicsEnv) before
the game starts EGL. Found for Vader Immortal's lightspeed shaders (loop counters and accumulators read before they
are set hang the GPU) by Klownicle, GitHub #49."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, register
from . import artifact

LAYER = "libVkLayer_fp_shaderfix.so"
ENGINE_LIBS = ("libUE4.so", "libUnreal.so", "libunity.so")
SETTING = "adapter.zink_shader_fix"
# not the engine: XR/Meta/FramePort libraries that may start EGL too
NOT_ENGINE = ("libopenxr", "libOVR", "libovr", "libvrapi", "libfp_", "libframe", "libVkLayer", "libglshim", "libc++")


def engine_lib(ws) -> str | None:
    """The library that starts EGL: the engine's, else the first of the game's libraries that imports eglInitialize."""
    lib = next((n for n in ENGINE_LIBS if ws.has(ws.lib(n))), None)
    if lib:
        return lib
    for name in ws.libs():
        if name.startswith(NOT_ENGINE):
            continue
        data = ws.read_lib(name)
        if elf.is_elf(data) and "eglInitialize" in elf.dyn_symbols(data, False):
            return name
    return None


def configured(recipe_patches: dict) -> bool:
    params = recipe_patches.get(SETTING) or {}
    dump = (recipe_patches.get("adapter.zink_shader_dump") or {}).get("value")
    return bool(str(params.get("value") or "").strip()) or bool(dump)


class ZinkShaderFix(Patch):
    id = "frame.zink_shader_fix"
    title = "OpenGL ES: shader fixes (Vulkan layer under Zink)"
    description = ("Loads FramePort's shader-fix Vulkan layer into an OpenGL ES game. On the Frame, OpenGL ES runs on "
                   "Zink (Mesa's GL on Vulkan); the layer sits between Zink and the driver and inserts the words of "
                   "the adapter setting zink_shader_fix into the SPIR-V modules whose size and SHA-256 match (every "
                   "other module is untouched), for example stores that set loop counters and accumulators a shader "
                   "reads before writing, which hang the GPU (Vader Immortal's lightspeed jump; found by Klownicle, "
                   "GitHub #49). Does nothing without zink_shader_fix (or zink_shader_dump) in the recipe. The layer "
                   "adds itself to Android's Vulkan debug layer list for the game's process only.")
    order = 73

    def applies(self, a):
        return "GLES" in a.graphics and "arm64-v8a" in a.abis

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a" or not configured(ctx.recipe_patches):
            return False
        lib = engine_lib(ws)
        if not lib:
            ctx.notes.append(f"{self.id}: no library that starts EGL found, layer not added")
            return False
        layer = artifact(ws.abi, LAYER)
        changed = False
        data = ws.read_lib(lib)
        if LAYER not in elf.needed(data):
            ws.put(ws.lib(lib), elf.add_needed(data, LAYER))
            changed = True
        if not ws.has(ws.lib(LAYER)) or ws.read_lib(LAYER) != layer:
            ws.put(ws.lib(LAYER), layer)
            changed = True
        if changed:
            ctx.notes.append(f"shader-fix Vulkan layer loaded by {lib}")
        return changed

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        if ws.abi != "arm64-v8a" or not configured(ctx.recipe_patches):
            return []
        lib = engine_lib(ws)
        loaded = bool(lib) and LAYER in elf.needed(ws.read_lib(lib))
        return [("Shader-fix layer present and loaded", loaded and ws.has(ws.lib(LAYER)), lib or "no EGL library")]


register(ZinkShaderFix)
