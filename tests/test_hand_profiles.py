"""FrameBridge's hide_hand_profiles: an app that binds its actions for hand interaction next to Touch got
ext/hand_interaction_ext for both hands on the Frame and no controller input (Gorilla Tag)."""
from frameport.core.models import Analysis
from frameport.patches import base
from frameport.patches.settings import adapter_settings


def _analysis():
    return Analysis(package="com.AnotherAxiom.GorillaTag", version="1.1.146", label="Gorilla Tag",
                    abis=["arm64-v8a"], engine="Unity", xr="OpenXR", graphics="Vulkan (declared in manifest)",
                    direct_vrapi=False, libs=["libOVRPlugin.so", "libUnityOpenXR.so"], launcher_activity=None,
                    has_info_category=False, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
                    oculus_os_classes=False, is_overport_output=False, debuggable=False, extra={"size": 1})


def test_hide_hand_profiles_setting():
    patch = base.get("adapter.hide_hand_profiles")
    assert patch.applies(_analysis()) and not patch.detect(_analysis())  # opt-in: hand-tracking games need the hands
    assert adapter_settings({"adapter.hide_hand_profiles": {"value": 1}})["hide_hand_profiles"] == 1
    assert "hide_hand_profiles" not in adapter_settings({})
    from frameport.patches.frame import artifact

    for abi in ("arm64-v8a", "armeabi-v7a"):
        adapter = artifact(abi, "libopenxr_loader_generic.so")
        assert b"hide_hand_profiles=%f" in adapter and b"/interaction_profiles/ext/hand_interaction_ext" in adapter


def test_gorilla_tag_recipe_from_the_catalog():
    from frameport.recommend import catalog

    entry = catalog.load()["com.AnotherAxiom.GorillaTag"]
    assert entry.adapter["hide_hand_profiles"] == 1
    assert {"haptic_fix", "pose_time_fix"} <= set(entry.adapter)
