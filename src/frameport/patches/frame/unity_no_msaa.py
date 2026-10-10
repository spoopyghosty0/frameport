"""Unity: turn off MSAA in QualitySettings (in-place int patch)."""
from __future__ import annotations

import struct

from ..base import ApkContext, Patch, Suggestion, register

GGM = "assets/bin/Data/globalgamemanagers"


def _levels(ggm_bytes: bytes):
    import UnityPy

    env = UnityPy.load(ggm_bytes)
    for obj in env.objects:
        if obj.type.name != "QualitySettings":
            continue
        raw = obj.get_raw_data()
        for level in obj.read_typetree()["m_QualitySettings"]:
            yield obj, raw, level


def count_msaa_levels(ggm_bytes: bytes) -> int:
    return sum(1 for _, _, level in _levels(ggm_bytes) if level.get("antiAliasing", 0) > 1)


def disable_msaa(ggm_bytes: bytes) -> bytes | None:
    """Set every quality level's antiAliasing to 0 by patching the int in place (re-serializing with UnityPy breaks
    scene loading). The field is found through the 4-int run skinWeights (blendWeights before Unity 2019),
    textureQuality, anisotropicTextures, antiAliasing."""
    data = bytearray(ggm_bytes)
    patched = 0
    for obj, raw, level in _levels(ggm_bytes):
        aa = level.get("antiAliasing", 0)
        if aa <= 1:
            continue
        base = obj.byte_start
        if ggm_bytes[base:base + len(raw)] != raw:  # (not `data`: an earlier level of the same object changed it)
            raise RuntimeError("QualitySettings object bytes not found in place")
        weights = level.get("skinWeights", level.get("blendWeights"))
        if weights is None:
            raise RuntimeError(f"quality level {level.get('name')}: unknown QualitySettings layout")
        key = struct.pack("<4i", weights, level["textureQuality"], level["anisotropicTextures"], aa)
        at = raw.find(key)
        if at < 0:
            raise RuntimeError(f"antiAliasing field not found for quality level {level['name']}")
        struct.pack_into("<i", data, base + at + 12, 0)
        patched += 1
    return bytes(data) if patched else None


class UnityNoMsaa(Patch):
    id = "frame.unity_no_msaa"
    title = "Unity: disable MSAA"
    description = ("Sets Unity QualitySettings antiAliasing to 0. Unity's multisampled render-to-texture path can "
                   "hang the Frame's GL driver ('zink: DEVICE LOST', for example Sniper Elite VR). Try it for GLES "
                   "Unity games that freeze or crash the GPU.")
    order = 40

    def detect(self, a):
        if a.engine != "Unity" or not a.unity_msaa_levels or "GLES" not in a.graphics or a.only_32bit:
            return None
        if a.xr == "VrApi":
            return Suggestion(True, f"Legacy VrApi Unity game on GLES with MSAA ({a.unity_msaa_levels} quality "
                                    "levels): multisampled render-to-texture hangs the Frame's GL driver "
                                    "(for example Sniper Elite VR).")
        return Suggestion(False, f"GLES Unity game with MSAA on ({a.unity_msaa_levels} quality levels); enable if "
                                 "it hangs the GPU ('zink: DEVICE LOST').")

    def applies(self, a):
        return a.engine == "Unity" and "GLES" in a.graphics

    def apply(self, ctx: ApkContext) -> bool:
        if not ctx.ws.has(GGM):
            return False
        fixed = disable_msaa(ctx.ws.read(GGM))
        if fixed:
            ctx.ws.put(GGM, fixed)
        return bool(fixed)


register(UnityNoMsaa)
