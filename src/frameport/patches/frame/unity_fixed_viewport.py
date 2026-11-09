"""Unity's dynamic resolution, held at full size: the IL2CPP part of the adapter setting `fixed_resolution` (#574).

Unity XR games change their resolution at runtime by shrinking the viewport they draw into the eye texture:
XRSettings.renderViewportScale (the rect FrameBridge sees in xrEndFrame gets smaller inside the same swapchain) and
ScalableBufferManager.ResizeBuffers for cameras with dynamic resolution. Meta's OVRManager does it from OVRPlugin's
layer recommendation (xrGetRecommendedLayerResolutionMETA, which FrameBridge answers when the setting is on); games
can also run a scaler of their own. Rogue Piñatas VRmageddon (Unity 2022.3) has an ECS system that takes the smaller
of OVRPlugin.GetEyeLayerRecommendedResolution and a cap from its averaged frame time (1.8x down to 0.6x of the
recommended size between 1/72 and 1/60 s), moves the width 32 pixels a frame and calls both setters: the eye rect
went from 4048 to 1504 within seconds and stayed there, whatever FrameBridge's `scale`.

Whatever computes the scale, these two engine setters are where it reaches the picture, and both are plain managed
methods in every IL2CPP game (found per game with Cpp2IL, like frame.unity_runtime_msaa_off's
QualitySettings.set_antiAliasing). With the setting on they return at once: the viewport keeps Unity's default 1.0
(the full eye texture). Mono builds (no libil2cpp.so) keep their scaler."""
from __future__ import annotations

from ..base import ApkContext
from .unity_text_input import RET_FALSE, Il2cppReturnPatch

RET_VOID = RET_FALSE  # mov w0, #0 ; ret: a void method that does nothing
XR_SETTINGS = "UnityEngine.VRModule/UnityEngine/XR/XRSettings.cs"
BUFFER_MANAGER = "UnityEngine.CoreModule/UnityEngine/ScalableBufferManager.cs"


class UnityFixedViewport(Il2cppReturnPatch):
    """Not a patch of its own (not registered): frame.adapter runs it when adapter.fixed_resolution is on."""
    id = "adapter.fixed_resolution"
    title = "Keep full resolution: Unity viewport"
    targets = {XR_SETTINGS: {"set_renderViewportScale": RET_VOID},
               BUFFER_MANAGER: {"ResizeBuffers": RET_VOID}}
    check_name = "Unity full resolution"


def pin_unity_viewport(ctx: ApkContext) -> bool:
    """Make Unity's dynamic-resolution setters do nothing in an IL2CPP game; False for other engines and Mono."""
    ws = ctx.ws
    if ctx.analysis.engine != "Unity" or ws.abi != "arm64-v8a" or not ws.has(ws.lib("libil2cpp.so")):
        return False
    return UnityFixedViewport().apply(ctx)
