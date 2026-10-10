"""FrameBridge: wraps OVRPort's generic OpenXR loader to paper over Steam Frame runtime gaps."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, Suggestion, register
from . import artifact

GENERIC = "libopenxr_loader_generic.so"
ORIGINAL = "libopenxr_loader_original.so"
SETTINGS = "libframe_settings.so"
XRSHIM = "libframe_xrshim.so"
OVERPORT_LOADER = "libopenxr_loader.so"
OVRPLUGIN = "libOVRPlugin.so"


def settings_text(recipe_patches: dict) -> bytes:
    from .headset_model import adapter_settings

    settings = adapter_settings(recipe_patches)
    if settings.get("headset_model") == 2:
        settings.pop("headset_model")  # native default 0 preserves the old name; keep Quest 2 settings byte-stable
    return "".join(f"{k}={v}\n" for k, v in settings.items()).encode()


class FrameBridgeAdapter(Patch):
    id = "frame.adapter"
    package_revisions = {"com.camouflaj.manta": 3}  # above revision 2: Batman builds before #389 are outdated
    a32 = True  # also for 32-bit games on the bridge
    title = "FrameBridge OpenXR adapter"
    description = (
        "Wraps OVRPort's generic OpenXR loader (renamed libopenxr_loader_original.so). Fixes the Frame runtime's "
        "gaps: retries rejected GLES swapchain formats/MSAA (Frame takes sRGB only), drops unsupported instance "
        "extensions and layers, emulates XR_FB_passthrough (ALPHA_BLEND), Meta scene/spatial entities (guardian-sized "
        "room), XR_KHR_convert_timespec_time, and vertically flipped quad layers; maps Frame controllers to Touch; "
        "optionally serves Steam Frame controller models (XR_FB_render_model). "
        "Its settings are under Game settings."
    )
    category = "frame"
    revision = 2  # dormant foveation diagnostic support; runtime choice never changes APK inputs
    order = 10
    default_on = True

    def detect(self, analysis):
        return Suggestion(True, "Required for every OpenXR/overport build on the Frame.")

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if not ws.has(ws.lib(GENERIC)):
            raise RuntimeError("OVRPort output has no libopenxr_loader_generic.so (not an OVRPort build?)")
        adapter = artifact(ws.abi, GENERIC)
        if ws.abi == "arm64-v8a":
            from .foveation import LAYER
            adapter = elf.add_needed(adapter, LAYER)
        settings = settings_text(ctx.recipe_patches)
        changed = False
        if not ws.has(ws.lib(ORIGINAL)):
            ws.move(ws.lib(GENERIC), ws.lib(ORIGINAL))
            ws.add[ws.lib(GENERIC)] = adapter
            changed = True
        elif ws.read_lib(GENERIC) != adapter:  # already wrapped: update to the current build
            ws.put(ws.lib(GENERIC), adapter)
            changed = True
        if not ws.has(ws.lib(SETTINGS)) or ws.read_lib(SETTINGS) != settings:
            ws.put(ws.lib(SETTINGS), settings)
            changed = True
        if needs_xrshim(ctx.recipe_patches) and ws.abi == "arm64-v8a":
            changed |= add_xrshim(ctx)
        # The decoder now lives in the agent's shared, versioned codec store (frame.hw_video_decode selects it per
        # game). Remove old embedded payloads without changing any game/video assets.
        for name in ("libstagefrighthw.so", "media_codecs_frameport.xml", "podman.py", "manifest.json",
                     "COPYING.FFmpeg"):
            target = f"assets/frameport/hevc/{name}"
            if ws.has(target):
                ws.remove.add(target)
                ws.add.pop(target, None)
                ws.replace.pop(target, None)
                changed = True
        if ws.abi == "arm64-v8a":
            from .foveation import diagnostic_support
            changed |= diagnostic_support(ws)
        return changed

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        ok = ws.has(ws.lib(ORIGINAL)) and ws.has(ws.lib(SETTINGS))
        expected = artifact(ws.abi, GENERIC)
        if ws.abi == "arm64-v8a":
            from .foveation import LAYER
            expected = elf.add_needed(expected, LAYER)
        ok = ok and ws.read_lib(GENERIC) == expected
        return [("FrameBridge adapter present", ok, "adapter, original loader and settings library")]


def needs_xrshim(recipe_patches: dict) -> bool:
    """Adapter features whose functions OVRPort's dispatcher doesn't forward (it only knows a fixed table)."""
    from ..settings import XRSHIM_SETTINGS, adapter_settings

    s = adapter_settings(recipe_patches)
    return any(s.get(key) for key in (*XRSHIM_SETTINGS, "scene_emul"))


def add_xrshim(ctx: ApkContext) -> bool:
    """Route Meta OVRPlugin's OpenXR lookups through native/xrshim so XR_FB_render_model reaches the adapter.

    OVRPlugin gets xrGetInstanceProcAddr with dlopen("libopenxr_loader.so") + dlsym (checked in Toy Master's
    OVRPlugin), so the loader name string it dlopens is pointed at the shim instead (same length, in place); the shim
    forwards every other lookup to OVRPort's dispatcher."""
    ws = ctx.ws
    shim = artifact(ws.abi, XRSHIM)
    changed = False
    if not ws.has(ws.lib(XRSHIM)) or ws.read_lib(XRSHIM) != shim:
        ws.put(ws.lib(XRSHIM), shim)
        changed = True
    if ws.has(ws.lib(OVRPLUGIN)):
        data, count = elf.replace_rodata_string(ws.read_lib(OVRPLUGIN), OVERPORT_LOADER, XRSHIM)
        if count:
            ws.put(ws.lib(OVRPLUGIN), data)
            ctx.notes.append(f"{OVRPLUGIN} looks up OpenXR functions through {XRSHIM}")
            changed = True
    return changed


register(FrameBridgeAdapter)
