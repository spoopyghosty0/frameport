"""Predicates shared by patch `applies()` / `detect()` rules."""
from __future__ import annotations

from ..core.models import Analysis


def is_unreal(a: Analysis) -> bool:
    return a.engine == "Unreal"


def is_unity(a: Analysis) -> bool:
    return a.engine == "Unity"


def has_vrapi(a: Analysis) -> bool:
    return "libvrapi.so" in a.libs


def is_gles(a: Analysis) -> bool:
    return "GLES" in a.graphics


def is_vulkan(a: Analysis) -> bool:
    return a.graphics.startswith("Vulkan")


def arm64(a: Analysis) -> bool:
    return "arm64-v8a" in a.abis


def meta_audio_libs(a: Analysis) -> list[str]:
    prefixes = ("libmetaxraudio", "libovraudio", "libaksoundengine", "libovravatar")
    return [lib for lib in a.libs if lib.lower().startswith(prefixes)]


def uses_scene(a: Analysis) -> bool:
    perms = a.extra.get("meta_permissions_used") or a.meta_permissions
    return any(p.endswith(("USE_SCENE", "USE_ANCHOR_API")) for p in perms)


def uses_room_model(a: Analysis) -> bool:
    """The game declares Meta's USE_SCENE permission: it asks the system for the room layout (scene model)."""
    perms = a.extra.get("meta_permissions_used") or a.meta_permissions
    return any(p.endswith("USE_SCENE") for p in perms)


def needs_scene(a: Analysis) -> bool:
    """Mixed-reality-only game that builds its world from the room model."""
    perms = a.extra.get("meta_permissions_used") or a.meta_permissions
    return bool(a.extra.get("mr_only")) and any(p.endswith("USE_SCENE") for p in perms)


def unreal_version(a: Analysis) -> tuple[int, int] | None:
    v = a.extra.get("unreal_version")
    try:
        major, minor = v.split(".")[:2]
        return int(major), int(minor)
    except (AttributeError, ValueError):
        return None


def uses_render_models(a: Analysis) -> bool:
    """The game declares Meta's runtime controller models (XR_FB_render_model): permission or feature RENDER_MODEL."""
    perms = a.extra.get("meta_permissions_used") or a.meta_permissions
    return (any(p.endswith("RENDER_MODEL") for p in perms)
            or "com.oculus.feature.RENDER_MODEL" in (a.extra.get("features") or {}))


def may_use_render_models(a: Analysis) -> bool:
    """Could ask for runtime controller models at all: declares them, or ships Meta's OVRPlugin (Unity/Unreal)."""
    return uses_render_models(a) or "libOVRPlugin.so" in a.libs


def uses_equirect_layers(a: Analysis) -> bool:
    """The game's own native code (not Meta's OVRPlugin, which lists every layer type) requests 360° composition
    layers, which the Frame runtime lacks; only GLES games can get them back (equirect_emul)."""
    exts = a.extra.get("xr_layer_exts") or []
    return is_gles(a) and any(e.startswith("XR_KHR_composition_layer_equirect") for e in exts)
