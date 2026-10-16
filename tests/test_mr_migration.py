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


def test_both_scene_permissions_apply_without_mr_only():
    for permission in ("USE_SCENE", "USE_ANCHOR_API"):
        assert uses_scene(_analysis(meta_permissions=["com.oculus.permission." + permission]))
        assert uses_scene(_analysis(extra={"mr_only": False, "meta_permissions_used": [permission]}))
