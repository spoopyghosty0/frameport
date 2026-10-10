"""Unity XR settings the game's code chooses at runtime, fixed in libil2cpp.so (found per game with Cpp2IL, like
frame.unity_text_input).

- MSAA: Meta's OVRManager (useRecommendedMSAALevel) raises Unity's antiAliasing to the headset's recommended level
  (4x) at runtime ("The current MSAA level is 0, but the recommended MSAA level is 4. Switching to the recommended
  level."), whatever QualitySettings say, so frame.unity_no_msaa can't keep it off. Multisampled render-to-texture on
  GLES through Zink hangs the Frame's GPU (Lucky's Tale: the whole headset restarted in a menu, 2026-10-03).
  OVRDisplay.recommendedMSAALevel -> 0 keeps it off.
- Multiview: Oculus XR Plugin games render both eyes in one pass (Multiview) on Android. I Am Cat (Unity 2022.3, GLES
  through Zink) drew only effects into the right eye's array slice; OculusSettings.GetStereoRenderingMode -> 0
  (MultiPass) renders each eye in its own pass."""
from __future__ import annotations

from ..base import Suggestion, register
from .unity_text_input import RET_FALSE, Il2cppReturnPatch

RET_ZERO = RET_FALSE  # mov w0, #0 ; ret


class UnityRuntimeMsaa(Il2cppReturnPatch):
    id = "frame.unity_runtime_msaa_off"
    title = "Unity: keep MSAA off at runtime (OVRManager)"
    description = ("Meta's OVRManager switches Unity to the headset's recommended MSAA level (4x) while the game runs, "
                   "whatever its quality settings say (log: \"Switching to the recommended level\"). Multisampled "
                   "render-to-texture on GLES can hang the Frame's GPU, up to a restart of the whole headset (for "
                   "example Lucky's Tale in a menu) or a crash of its GL driver (for example The Room VR, whose own "
                   "code sets 4x). Rewrites OVRDisplay.recommendedMSAALevel -> 0 and makes "
                   "QualitySettings.antiAliasing's setter do nothing in libil2cpp.so (found with Cpp2IL), so MSAA "
                   "stays off.")
    order = 47
    targets = {"Oculus.VR/OVRDisplay.cs": {"get_recommendedMSAALevel": RET_ZERO},
               "Assembly-CSharp/OVRDisplay.cs": {"get_recommendedMSAALevel": RET_ZERO},  # older Oculus Integration
               # the game's own code can raise MSAA too (The Room VR: QualitySettingsManager.SetMSAA with its
               # platform's default 4): Unity's setter does nothing, the quality settings' 0 stays
               "UnityEngine.CoreModule/UnityEngine/QualitySettings.cs": {"set_antiAliasing": RET_ZERO}}
    revision = 2  # 0.11.0: also QualitySettings.set_antiAliasing (the game's own runtime MSAA crashed Zink)
    check_name = "Unity MSAA"

    def applies(self, a):
        return a.engine == "Unity" and "libil2cpp.so" in a.libs and not a.only_32bit

    def detect(self, a):
        if self.applies(a) and (a.extra or {}).get("ovr_runtime_msaa") and "GLES" in a.graphics:
            return Suggestion(True, "GLES Unity game whose OVRManager turns 4x MSAA on at runtime: multisampled "
                                    "render-to-texture can hang the Frame's GPU.")
        return None


class UnityMultiPass(Il2cppReturnPatch):
    id = "frame.unity_multipass"
    title = "Unity: render each eye separately (no multiview)"
    description = ("Oculus XR Plugin games render both eyes in one pass (multiview) on Android. If one eye shows only "
                   "effects or gray (for example I Am Cat), rendering each eye in its own pass can fix it, at some "
                   "GPU cost. Rewrites OculusSettings.GetStereoRenderingMode and its inlined read in "
                   "OculusLoader.Initialize -> MultiPass in libil2cpp.so (found with Cpp2IL). Try it when one eye is "
                   "wrong.")
    order = 48
    revision = 2  # 0.6.3 only patched the getter, which is inlined: also the field read in OculusLoader.Initialize
    SETTINGS = "Unity.XR.Oculus/Unity/XR/Oculus/OculusSettings.cs"
    targets = {SETTINGS: {"GetStereoRenderingMode": RET_ZERO}}
    # the one-line getter is inlined into OculusLoader.Initialize (no call sites in I Am Cat / Toy Master): the field
    # read there is what reaches the native plugin
    field_loads = {"Unity.XR.Oculus/Unity/XR/Oculus/OculusLoader.cs":
                   {"Initialize": (SETTINGS, "m_StereoRenderingModeAndroid", 0)}}
    check_name = "Unity stereo mode"

    def applies(self, a):
        return a.engine == "Unity" and "libil2cpp.so" in a.libs and not a.only_32bit \
            and (a.extra or {}).get("oculus_xr_plugin", True) is not False

    def detect(self, a):
        return None  # only for a game that shows the symptom (catalog recipe or the user's choice)


class UnityNoOverlayCopy(Il2cppReturnPatch):
    id = "frame.unity_no_overlay_copy"
    title = "Unity: skip OVROverlay layers (fades, splash screens)"
    description = ("Meta's OVROverlay copies a texture into its own compositor layer every frame. For some games "
                   "(for example The Room VR's screen fade, a 4x4 overlay) that copy crashes the Frame's GL driver: "
                   "SIGSEGV in libgallium_dri.so on Unity's render thread right after a tiny xrCreateSwapchain, gray "
                   "or frozen screen. Rewrites OVROverlay.PopulateLayer -> false in libil2cpp.so (found with Cpp2IL): "
                   "overlays are skipped (fades become instant cuts, overlay splash screens don't show). Only for "
                   "games with this crash: menus drawn as overlays would disappear too.")
    order = 49
    targets = {"Oculus.VR/OVROverlay.cs": {"PopulateLayer": RET_ZERO},
               "Assembly-CSharp/OVROverlay.cs": {"PopulateLayer": RET_ZERO}}  # older Oculus Integration
    check_name = "Unity overlays"

    def applies(self, a):
        return a.engine == "Unity" and "libil2cpp.so" in a.libs and not a.only_32bit and "GLES" in a.graphics

    def detect(self, a):
        return None  # only for a game that shows the crash (catalog recipe or the user's choice)


register(UnityRuntimeMsaa)
register(UnityMultiPass)
register(UnityNoOverlayCopy)
