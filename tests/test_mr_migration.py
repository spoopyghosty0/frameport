from copy import deepcopy

from test_patches import _analysis

from frameport.core import library
from frameport.patches.applicability import uses_scene


def test_scene_migration_only_changes_automatic_recipes_once():
    data = {"settings": {"migrations": []}, "games": {
        "com.test.scene": {"analysis": _analysis(meta_permissions=["com.oculus.permission.USE_SCENE"]).to_dict(),
                           "recipe": {"source": "heuristic", "patches": {}}},
        "com.test.anchor": {"analysis": _analysis(meta_permissions=["com.oculus.permission.USE_ANCHOR_API"]).to_dict(),
                            "recipe": {"source": "catalog", "patches": {"adapter.scene_emul": {"value": 0}}}},
        "com.test.mine": {"analysis": _analysis(meta_permissions=["com.oculus.permission.USE_SCENE"]).to_dict(),
                          "recipe": {"source": "user", "patches": {}}},
        "com.test.other": {"analysis": _analysis().to_dict(), "recipe": {"source": "heuristic", "patches": {}}}}}
    library._migrate(data)
    for pkg in ("com.test.scene", "com.test.anchor"):
        assert data["games"][pkg]["recipe"]["patches"]["adapter.scene_emul"] == {"value": 1}
    for pkg in ("com.test.mine", "com.test.other"):
        assert "adapter.scene_emul" not in data["games"][pkg]["recipe"]["patches"]
    data["games"]["com.test.scene"]["recipe"]["patches"].pop("adapter.scene_emul")
    before = deepcopy(data)
    library._migrate(data)
    assert data == before


def test_scene_games_get_the_meta_permissions():
    # NOPE / Pinball FX never asked for the room: Android couldn't grant USE_SCENE without the declarations
    from frameport.patches import base

    data = {"settings": {"migrations": ["scene_emul_all"]}, "games": {
        "com.test.scene": {"analysis": _analysis(meta_permissions=["com.oculus.permission.USE_SCENE"]).to_dict(),
                           "recipe": {"source": "catalog", "patches": {"adapter.scene_emul": {"value": 1}}}},
        "com.test.off": {"analysis": _analysis(meta_permissions=["com.oculus.permission.USE_SCENE"]).to_dict(),
                         "recipe": {"source": "catalog", "patches": {"adapter.scene_emul": {"value": 0}}}},
        "com.test.mine": {"analysis": _analysis(meta_permissions=["com.oculus.permission.USE_SCENE"]).to_dict(),
                          "recipe": {"source": "user", "patches": {"adapter.scene_emul": {"value": 1}}}}}}
    library._migrate(data)
    patches = {pkg: g["recipe"]["patches"] for pkg, g in data["games"].items()}
    assert "frame.meta_permissions" in patches["com.test.scene"]
    assert "frame.meta_permissions" not in patches["com.test.off"]
    assert "frame.meta_permissions" not in patches["com.test.mine"]
    anchor = _analysis(meta_permissions=["com.oculus.permission.USE_ANCHOR_API"])
    assert base.get("frame.meta_permissions").detect(anchor).recommended
    assert base.get("frame.meta_permissions").detect(_analysis()) is None


def test_scene_emulation_brings_the_xrshim():
    # OVRPort's dispatcher refuses the scene functions; only the shim gets them to FrameBridge (Pinball FX VR)
    from frameport.patches.frame.adapter import needs_xrshim

    assert needs_xrshim({"adapter.scene_emul": {"value": 1}})
    assert not needs_xrshim({"adapter.scene_emul": {"value": 0}})


def test_both_scene_permissions_apply_without_mr_only():
    for permission in ("USE_SCENE", "USE_ANCHOR_API"):
        assert uses_scene(_analysis(meta_permissions=["com.oculus.permission." + permission]))
        assert uses_scene(_analysis(extra={"mr_only": False, "meta_permissions_used": [permission]}))
