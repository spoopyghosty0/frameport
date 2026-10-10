"""Heuristics for games without a catalog recipe (catalog switched off)."""
from test_patches import _analysis

from frameport.recommend import engine


def suggest(**kw):
    kw.setdefault("package", "x.y.unknown")
    a = _analysis(**kw)
    return a, engine.suggest(a, use_catalog=False)


def test_mixed_reality_only_scene_game():
    _, r = suggest(extra={"mr_only": True, "meta_permissions_used": ["com.oculus.permission.USE_SCENE"], "size": 1},
                   meta_permissions=["com.oculus.permission.USE_SCENE"])
    assert "patch_force_passthrough" in r.patches
    assert r.params("adapter.scene_emul") == {"value": 1}
    assert "frame.meta_permissions" in r.patches


def test_mixed_reality_without_scene():
    _, r = suggest(extra={"mr_only": True, "meta_permissions_used": ["com.oculus.permission.USE_ANCHOR_API"],
                          "size": 1})
    assert "patch_force_passthrough" in r.patches and "adapter.scene_emul" not in r.patches


def test_hand_tracking_only():
    _, r = suggest(extra={"hand_tracking_only": True, "size": 1})
    assert r.params("adapter.controller_fix") == {"value": 0}


def test_heavy_ovrplugin_game_disables_space_warp():
    _, r = suggest(libs=["libOVRPlugin.so", "libunity.so"], extra={"size": 2**30, "data_bytes": 25 * 2**30})
    assert "patch_disable_space_warp" in r.patches
    _, r = suggest(libs=["libOVRPlugin.so", "libunity.so"], extra={"size": 2**30, "data_bytes": 2**30})
    assert "patch_disable_space_warp" not in r.patches


def test_old_unreal_nodebug_and_alt_build():
    _, r = suggest(engine="Unreal", xr="VrApi", libs=["libUE4.so", "libOVRPlugin.so", "libvrapi.so"],
                   extra={"unreal_version": "4.20", "size": 1})
    assert "frame.nodebug" in r.patches and r.alt_patches == ["patch_remove_unreal_force_quit"]
    _, r = suggest(engine="Unreal", libs=["libUE4.so"], extra={"unreal_version": "4.27", "size": 1})
    assert "frame.nodebug" not in r.patches


def test_cryengine_disables_vrs():
    _, r = suggest(engine="CryEngine", xr="VrApi", direct_vrapi=True,
                   libs=["libCrySystem.so", "libCryRenderVulkan.so", "libvrapi.so"],
                   graphics="Vulkan (declared in manifest)")
    assert r.params("device.files")["files"]["user.cfg"].startswith("r_variable_rate_shading = 0")
    assert "frame.vrapi_bridge" in r.patches


def test_legacy_vrapi_unity_gles_msaa():
    _, r = suggest(xr="VrApi", graphics="GLES or unknown", unity_msaa_levels=3)
    assert "frame.unity_no_msaa" in r.patches


def test_irrelevant_patches_hidden_for_unity():
    a, r = suggest(engine="Unity", libs=["libunity.so", "libOVRPlugin.so"])
    shown, hidden = engine.visible_patches(a, r)
    hidden_ids = {p.id for p in hidden}
    assert {"patch_oculus_unreal", "patch_remove_unreal_force_quit", "frame.vrapi_bridge", "frame.gl_shim",
            "frame.metaxr_telemetry"} <= hidden_ids
    assert "patch_oculus_unity" not in hidden_ids and "frame.adapter" not in hidden_ids
    # hidden overport defaults stay in the recipe (no-ops), so builds are unchanged
    assert "patch_oculus_unreal" in r.patches


def test_enabled_patch_is_never_hidden():
    a, r = suggest(engine="Unity", libs=["libunity.so"])
    r.patches["frame.vrapi_bridge"] = {}
    shown, _ = engine.visible_patches(a, r)
    assert "frame.vrapi_bridge" in {p.id for p in shown}


def test_newer_android_than_the_frames_only_warns():
    """GitHub #71/#72: minSdk 34 apps crash at start on Android 13+ classes; Lepton runs Android 11."""
    _, r = suggest(extra={"size": 1, "min_sdk": 34})
    assert r.status != "unsupported"  # the manifest alone never decides (a launch test does)
    assert "asks for Android 14 (API 34)" in r.notes and "Android is 11 (API 30)" in r.notes
    # Quest games declare up to 32 (Quest's Android 12L) and run (16 catalog games do)
    for ok in (None, 23, 29, 32):
        _, r = suggest(extra={"size": 1, "min_sdk": ok})
        assert r.status != "unsupported" and "asks for Android" not in r.notes


def test_web_wrapper_only_warns():
    """GitHub #86: a Trusted Web Activity opens a website in Meta's browser; there's nothing to port."""
    _, r = suggest(extra={"size": 1, "web_wrapper": {"url": "https://mahjong-vr.pages.dev/"}})
    assert r.status != "unsupported" and "open https://mahjong-vr.pages.dev/ in a browser" in r.notes
    _, r = suggest(extra={"size": 1, "web_wrapper": {"url": None}})
    assert r.status != "unsupported" and "website" in r.notes


def test_scene_emul_suggested_for_games_that_ask_for_the_room():
    """VR HOT (not mixed-reality-only) declares USE_SCENE and its room setup retried forever without a room model."""
    from frameport.patches import base

    p = base.get("adapter.scene_emul")
    a = _analysis(extra={"meta_permissions_used": ["com.oculus.permission.USE_SCENE"], "mr_only": False})
    s = p.detect(a)
    assert s and s.recommended and s.params == {"value": 1}
    assert p.detect(_analysis(extra={"meta_permissions_used": ["com.oculus.permission.USE_ANCHOR_API"]})) is None


def test_pose_time_fix_suggested_for_unreal4_oculus_input():
    """Vader Immortal (UE4, OVRPlugin of the thumb-touch generation) asks for poses at OVRPlugin's own clock."""
    from frameport.patches import base

    p = base.get("adapter.pose_time_fix")
    s = p.detect(_analysis(engine="Unreal", libs=["libUE4.so", "libOVRPlugin.so"], extra={"unreal_thumb_touch": True}))
    assert s and s.recommended and s.params == {"value": 1}
    assert p.detect(_analysis(engine="Unreal", libs=["libUnreal.so", "libOVRPlugin.so"], extra={})) is None
