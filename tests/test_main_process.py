"""frame.main_process (GitHub #178): a game activity in its own process leaves no main process, which is the one
Lepton watches; it closed Halo CE VR about a minute in ("Target app ... did not start")."""
from conftest import build_axml

from frameport.apk import axml
from frameport.patches import base

PKG = "com.halo.decomp.vr"


def _activity(name, process=None, category=axml.LAUNCHER):
    attrs = [("name", "str", name)] + ([("process", "str", process)] if process else [])
    return [
        ("start", "activity", attrs),
        ("start", "intent-filter", []),
        ("start", "action", [("name", "str", axml.MAIN)]), ("end", "action"),
        ("start", "category", [("name", "str", category)]), ("end", "category"),
        ("end", "intent-filter"),
        ("end", "activity"),
    ]


def _halo_manifest():
    """Halo CE VR's layout: the VR activity runs in ":halo_game", the 2D launcher starts it."""
    return build_axml([
        ("start", "manifest", [("package", "str", PKG)]),
        ("start", "application", []),
        *_activity("com.halo.decomp.HaloActivity", ":halo_game", axml.VR_CATEGORY),
        *_activity("com.halo.decomp.LauncherActivity"),
        ("start", "service", [("name", "str", "com.halo.decomp.Crash"), ("process", "str", ":crash")]),
        ("end", "service"),
        ("end", "application"),
        ("end", "manifest"),
    ])


def _processes(manifest):
    x = axml.Axml(manifest)
    return {x.attr_str(e, "name"): x.attr_str(e, "process") for e in x.elements() if e.name in ("activity", "service")}


def test_detects_and_moves_the_game_activity():
    m = _halo_manifest()
    assert axml.separate_process_activities(m) == ["com.halo.decomp.HaloActivity"]
    fixed = axml.set_main_process(m, axml.separate_process_activities(m))
    assert axml.separate_process_activities(fixed) == []
    assert _processes(fixed) == {"com.halo.decomp.HaloActivity": PKG, "com.halo.decomp.LauncherActivity": None,
                                 "com.halo.decomp.Crash": ":crash"}  # services keep their own process
    assert len(fixed) == len(m)  # the package name is already in the pool: one value changes, nothing moves
    assert axml.set_main_process(fixed, ["com.halo.decomp.HaloActivity"]) is None


def test_main_process_activities_are_left_alone():
    m = build_axml([
        ("start", "manifest", [("package", "str", PKG)]),
        ("start", "application", []),
        *_activity("com.x.Game", PKG),  # explicitly the main process
        *_activity("com.x.Other"),
        ("end", "application"),
        ("end", "manifest"),
    ])
    assert axml.separate_process_activities(m) == []
    assert axml.set_main_process(m, ["com.x.Game"]) is None


def test_patch_is_offered_only_when_detected():
    patch = base.get("frame.main_process")
    assert patch.default_on

    class A:
        extra = {"separate_process": ["com.halo.decomp.HaloActivity"]}
    assert patch.applies(A()) and patch.detect(A()).recommended
    A.extra = {}
    assert not patch.applies(A()) and patch.detect(A()) is None


def test_xelee_keeps_its_vr_process():
    """GitHub #475: Xelee's launcher (MeleeActivity, main process) starts MeleeXrActivity (":xr"); both are SDL
    activities, and in one process SDL ends the app at the hand-over ("SDL main() finished", System.exit). Lepton
    starts the launcher in the main process, so the catalog keeps the VR part in its own process, as on Quest."""
    from frameport.core.models import Analysis
    from frameport.recommend import catalog, engine

    entry = catalog.lookup("dev.melee.game")
    assert entry and "frame.main_process" in entry.frame_remove
    a = Analysis(package="dev.melee.game", version="0.1.0-beta", label="Xelee", abis=["arm64-v8a"], engine="Other",
                 xr="OpenXR", graphics="Vulkan", direct_vrapi=False, libs=["libmelee.so", "libopenxr_loader.so"],
                 launcher_activity="dev.melee.MeleeActivity", has_info_category=False, meta_permissions=[],
                 uses_glad_gl=False, unity_msaa_levels=0, oculus_os_classes=False, is_overport_output=False,
                 debuggable=False, extra={"size": 1, "separate_process": ["dev.melee.MeleeXrActivity"],
                                          "vr_activity": "dev.melee.MeleeXrActivity"})
    assert base.get("frame.main_process").detect(a).recommended  # other apps with this layout still get it
    r = engine.suggest(a)
    assert "frame.main_process" not in r.patches and "device.text_input_window" in r.patches
