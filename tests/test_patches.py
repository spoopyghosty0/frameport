import zipfile

from frameport.analysis.stubgen import build_stub_library
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis, Recipe
from frameport.patches import base
from frameport.patches.settings import adapter_settings


def _analysis(**kw):
    d = dict(package="com.example.questgame", version="1.0", label="Quest Game", abis=["arm64-v8a"], engine="Unity",
             xr="OpenXR", graphics="Vulkan (declared in manifest)", direct_vrapi=False, libs=[], launcher_activity=None,
             has_info_category=True, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
             oculus_os_classes=False, is_overport_output=True, debuggable=True, extra={"size": 1})
    d.update(kw)
    return Analysis(**d)


def _apk(tmp_path, manifest):
    p = tmp_path / "in.apk"
    loader_imports = build_stub_library(["ovr_Present"], soname="libovrplatformloader.so")
    game = build_stub_library(["game_main"], soname="libgame.so")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
        z.writestr("classes.dex", b"dex\n035\0")
        z.writestr("lib/arm64-v8a/libopenxr_loader_generic.so", b"\x7fELF-original-loader")
        z.writestr("lib/arm64-v8a/libovrplatformloader.so", loader_imports)
        z.writestr("lib/arm64-v8a/libgame.so", game)
    return p


def test_registry_has_everything():
    ids = {p.id for p in base.all_patches()}
    for pid in ("patch_copy_libraries", "patch_remove_unreal_force_quit", "frame.adapter", "frame.launcher",
                "frame.ovrstubs", "frame.vrapi_bridge", "frame.gl_shim", "adapter.scene_emul", "device.files"):
        assert pid in ids


def test_adapter_and_launcher(tmp_path, quest_manifest):
    apk = _apk(tmp_path, quest_manifest)
    recipe = {"frame.adapter": {}, "frame.launcher": {}, "adapter.scene_emul": {"value": 1}}
    with ApkWorkspace(apk) as ws:
        for pid in ("frame.adapter", "frame.launcher"):
            ctx = base.ApkContext(ws, _analysis(), {}, Reporter(), recipe)
            assert base.get(pid).apply(ctx)
        out = ws.write(tmp_path / "out.apk")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert z.read("lib/arm64-v8a/libopenxr_loader_original.so") == b"\x7fELF-original-loader"
        assert z.read("lib/arm64-v8a/libopenxr_loader_generic.so").startswith(b"\x7fELF")
        assert (z.read("lib/arm64-v8a/libframe_settings.so")
                == b"scale=1.0\nfoveation_fix=1\ncontroller_fix=1\nscene_emul=1\n")
        assert names.count("lib/arm64-v8a/libopenxr_loader_generic.so") == 1


def test_adapter_refreshes_existing_wrapper(tmp_path, quest_manifest):
    """Reinstalling an already wrapped APK replaces its adapter with the shipped repair."""
    from frameport.patches.frame import artifact

    apk = _apk(tmp_path, quest_manifest)
    with ApkWorkspace(apk) as ws:
        ws.put(ws.lib("libopenxr_loader_original.so"), b"original-loader")
        ws.put(ws.lib("libopenxr_loader_generic.so"), b"old-adapter")
        patch = base.get("frame.adapter")
        ctx = base.ApkContext(ws, _analysis(), {}, Reporter(), {"frame.adapter": {}})
        assert patch.apply(ctx)
        assert ws.read_lib("libopenxr_loader_generic.so") == artifact(ws.abi, "libopenxr_loader_generic.so")
        assert ws.read_lib("libopenxr_loader_original.so") == b"original-loader"
        assert not patch.apply(ctx)


def test_controller_models_adds_xrshim(tmp_path, quest_manifest):
    from pathlib import Path

    from frameport.analysis import elf

    # like Meta's OVRPlugin: DT_NEEDED libopenxr_loader.so + dlopen("libopenxr_loader.so") /
    # dlsym(xrGetInstanceProcAddr)
    plugin = (Path(__file__).with_name("fixtures") / "libfakeovrplugin_arm64.so").read_bytes()
    for enabled in (True, False):
        apk = _apk(tmp_path, quest_manifest)
        with zipfile.ZipFile(apk, "a") as z:
            z.writestr("lib/arm64-v8a/libOVRPlugin.so", plugin)
        recipe = {"frame.adapter": {}, **({"adapter.controller_models": {"value": 1}} if enabled else {})}
        with ApkWorkspace(apk) as ws:
            assert base.get("frame.adapter").apply(base.ApkContext(ws, _analysis(), {}, Reporter(), recipe))
            out = ws.write(tmp_path / f"out{enabled}.apk")
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            patched = z.read("lib/arm64-v8a/libOVRPlugin.so")
        if enabled:
            assert "lib/arm64-v8a/libframe_xrshim.so" in names
            assert len(patched) == len(plugin) and b"libframe_xrshim.so\0\0" in patched
            assert patched.count(b"libopenxr_loader.so\0") == 1  # only the DT_NEEDED name (.dynstr) is left
            assert elf.needed(patched) == elf.needed(plugin)
        else:  # off: builds stay byte-identical to before
            assert "lib/arm64-v8a/libframe_xrshim.so" not in names and patched == plugin


def test_replace_rodata_string_whole_strings_only():
    from pathlib import Path

    from frameport.analysis import elf

    plugin = (Path(__file__).with_name("fixtures") / "libfakeovrplugin_arm64.so").read_bytes()
    out, n = elf.replace_rodata_string(plugin, "libopenxr_loader.so", "libframe_xrshim.so")
    assert n == 1 and elf.replace_rodata_string(out, "libopenxr_loader.so", "x")[1] == 0
    assert elf.replace_rodata_string(plugin, "openxr_loader.so", "y")[1] == 0  # a tail of a longer string


def test_settings_order_and_types():
    s = adapter_settings({"adapter.controller_fix": {"value": 0}, "adapter.scene_height": {"value": "3"}})
    assert list(s) == ["scale", "foveation_fix", "controller_fix", "scene_height"]
    assert s["controller_fix"] == 0 and s["scene_height"] == 3.0


def test_controller_models_setting():
    patch = base.get("adapter.controller_models")
    plain = _analysis()
    assert patch.detect(plain) is None and not patch.applies(plain)
    meta_sdk = _analysis(libs=["libOVRPlugin.so"])
    assert patch.applies(meta_sdk) and patch.detect(meta_sdk) is None  # SDK present, runtime models not declared
    for a in (_analysis(meta_permissions=["com.oculus.permission.RENDER_MODEL"]),
              _analysis(extra={"features": {"com.oculus.feature.RENDER_MODEL": False}})):
        s = patch.detect(a)
        assert s.recommended and s.params == {"value": 1} and patch.applies(a)
    assert adapter_settings({"adapter.controller_models": {"value": 1}})["controller_models"] == 1
    assert "controller_models" not in adapter_settings({})  # off unless selected: existing builds are unchanged


def test_equirect_emul_setting():
    patch = base.get("adapter.equirect_emul")
    gles = dict(graphics="GLES or unknown (no Vulkan declaration)")
    vulkan_360 = _analysis(extra={"xr_layer_exts": ["XR_KHR_composition_layer_equirect2"]})
    assert not patch.applies(vulkan_360) and patch.detect(vulkan_360) is None  # never offered for Vulkan games
    plain = _analysis(**gles, extra={"xr_layer_exts": ["XR_KHR_composition_layer_cylinder"]})
    assert patch.applies(plain) and patch.detect(plain) is None  # GLES but no 360 layers requested
    player = _analysis(**gles, extra={"xr_layer_exts": ["XR_KHR_composition_layer_cylinder",
                                                        "XR_KHR_composition_layer_equirect2"]})
    s = patch.detect(player)
    assert s.recommended and s.params == {"value": 1}
    for key in ("equirect_face", "equirect_res", "equirect_flip", "equirect_fps", "equirect_stereo"):
        assert base.get(f"adapter.{key}").applies(player) and not base.get(f"adapter.{key}").applies(vulkan_360)


def test_per_game_session_settings_are_off_unless_selected():
    keys = ("equirect_emul", "layer_debug", "stable_local", "focus_hold", "aim_pitch", "aim_yaw", "aim_forward",
            "refresh_rate", "equirect_face", "equirect_flip", "equirect_fps", "equirect_stereo")
    assert not set(keys) & set(adapter_settings({}))  # existing builds are unchanged
    chosen = adapter_settings({"adapter.refresh_rate": {"value": 90}, "adapter.aim_pitch": {"value": "-12.5"},
                               "adapter.focus_hold": {"value": 1}})
    assert chosen["refresh_rate"] == 90.0 and chosen["aim_pitch"] == -12.5 and chosen["focus_hold"] == 1
    for key in keys:
        patch = base.get(f"adapter.{key}")
        assert patch.detect(_analysis()) is None


def test_input_diag_is_an_off_by_default_diagnostic():
    from frameport.patches.settings import UI

    patch = base.get("adapter.input_diag")
    assert patch.default == 0 and patch.applies(_analysis()) and patch.detect(_analysis()) is None
    assert "input_diag" not in adapter_settings({})  # existing builds are unchanged
    assert adapter_settings({"adapter.input_diag": {"value": 1}})["input_diag"] == 1
    assert UI["input_diag"]["group"] == "troubleshooting" and UI["input_diag"]["level"] == "advanced"


def test_detect_direct_vrapi_suggests_bridge_and_shim():
    a = _analysis(xr="VrApi", direct_vrapi=True, uses_glad_gl=True, graphics="GLES or unknown", package="x.y.unknown")
    from frameport.recommend import engine

    r = engine.suggest(a)
    assert "frame.vrapi_bridge" in r.patches and "frame.gl_shim" in r.patches
    assert r.source == "heuristics"


def test_catalog_recipe_climb2():
    from frameport.recommend import engine

    r = engine.suggest(_analysis(package="com.crytek.climb2", xr="VrApi", direct_vrapi=True, engine="Other"))
    assert r.source.startswith("catalog")
    assert "frame.vrapi_bridge" in r.patches
    assert r.params("device.files")["files"]["user.cfg"].startswith("r_variable_rate_shading = 0")
    assert r.status == "works"


def test_catalog_phantom_uses_alt():
    from frameport.recommend import engine

    r = engine.suggest(_analysis(package="com.nDreams.PhantomQuest", engine="Unreal", xr="VrApi"))
    assert r.use_alt and r.alt_patches == ["patch_remove_unreal_force_quit"]


def test_32bit_unsupported():
    from frameport.recommend import engine

    r = engine.suggest(_analysis(package="x.y.old", abis=["armeabi-v7a"]))
    assert r.status == "unsupported"


def test_recipe_roundtrip():
    from frameport.core import library

    r = Recipe("a.b", {"frame.adapter": {}}, ["patch_remove_unreal_force_quit"], True)
    assert library.recipe_from_dict(library.recipe_to_dict(r)) == r


def _fixture(name):
    from pathlib import Path

    return (Path(__file__).with_name("fixtures") / name).read_bytes()


def test_swapchain_limit_raises_overport_guard():
    from frameport.analysis import elf
    from frameport.patches.frame.swapchain_limit import raise_swapchain_limit

    lib = _fixture("libfakeoverport_arm64.so")
    out, n = raise_swapchain_limit(lib)
    assert n == 2 and len(out) == len(lib)
    cmps = [o for _, m, o in elf.text_instructions(out) if m == "cmp" and "lsl #12" in o]
    assert cmps and all(o.endswith("#4, lsl #12") for o in cmps)  # 16384
    assert raise_swapchain_limit(out) == (None, 0)  # idempotent
    assert elf.dyn_symbols(out, True) == elf.dyn_symbols(lib, True)
    assert raise_swapchain_limit(_fixture("libfakeengine_arm64.so")) == (None, 0)  # no xrCreateSwapchain


def test_swapchain_limit_suggested_for_video_players():
    p = base.get("frame.swapchain_limit")
    assert p.detect(_analysis(engine="Other", libs=["libavcodec4x.so", "libvr4p-oculus.so"])).recommended
    assert p.default_on and p.detect(_analysis(engine="Unity", libs=["libunity.so"])).recommended


def test_vk_sanitize_routes_engine_vulkan_through_shim(tmp_path, quest_manifest):
    from frameport.analysis import elf

    engine = _fixture("libfakeengine_arm64.so")
    apk = _apk(tmp_path, quest_manifest)
    with zipfile.ZipFile(apk, "a") as z:
        z.writestr("lib/arm64-v8a/libUE4.so", engine)
    with ApkWorkspace(apk) as ws:
        assert base.get("frame.vk_sanitize").apply(base.ApkContext(ws, _analysis(engine="Unreal"), {}, Reporter(), {}))
        out = ws.write(tmp_path / "out.apk")
    with zipfile.ZipFile(out) as z:
        patched = z.read("lib/arm64-v8a/libUE4.so")
        shim = z.read("lib/arm64-v8a/libfp_vk.so")
    assert len(patched) == len(engine) and b"libfp_vk.so\0\0" in patched and b"libvulkan.so\0" not in patched
    assert elf.soname(shim) == "libfp_vk.so" and "libvulkan.so" in elf.needed(shim)
    assert {"vkGetInstanceProcAddr", "vkGetDeviceProcAddr", "vkCreateRenderPass2"} <= elf.dyn_symbols(shim, True)
    # a game without the dlopen string is left alone
    apk2 = _apk(tmp_path, quest_manifest)
    with ApkWorkspace(apk2) as ws:
        ctx = base.ApkContext(ws, _analysis(engine="Unreal"), {}, Reporter(), {})
        assert not base.get("frame.vk_sanitize").apply(ctx)


def test_source_hints_are_generic_and_match_loosely():
    from frameport.recommend.catalog import generic_source_hint, source_hint_matches

    assert generic_source_hint("4XVR Video Player (Pro) v20022+2.0.22 -RLS") == "4XVR Video Player"
    assert generic_source_hint("Marvels Deadpool VR (English Only) v8742+1.0.40.356975.Quest") == "Marvels Deadpool VR"
    assert generic_source_hint("Batman- Arkham Shadow (Inc Lang Packs) v350961+1.4.1-350961") == "Batman- Arkham Shadow"
    # other release, other name
    assert source_hint_matches("Marvels Deadpool VR", "Marvel's Deadpool VR v9000+1.1 -XYZ")
    assert source_hint_matches("The Climb 2 v974+2.2", "the climb 2 (quest) v1000")
    assert not source_hint_matches("The Climb 2", "The Climb v100")


def test_ovrport_125_patches():
    """OVRPort 1.2.5's new patches: off by default, shown only where they can matter, mutual exclusions warned."""
    from frameport.recommend import engine

    quest = _analysis()
    nexus = _analysis(package="com.Ubisoft.ACNexusVR", version="MAIN.450412.207706.final")
    other_nexus = _analysis(package="com.Ubisoft.ACNexusVR", version="MAIN.999.1.final")
    vrapi = _analysis(direct_vrapi=True, libs=["libvrapi.so"])
    for pid in ("patch_vrapi_openxr", "patch_disable_meta_xr_audio_telemetry", "patch_ac_nexus_no_appsw_72",
                "patch_ac_nexus_no_appsw_90"):
        assert not base.get(pid).default_on
        assert not base.get(pid).applies(quest)
    assert base.get("patch_ac_nexus_no_appsw_90").applies(nexus) and base.get("patch_vrapi_openxr").applies(vrapi)
    assert not base.get("patch_disable_meta_xr_audio_telemetry").applies(nexus)  # emulators only
    assert not base.get("patch_ac_nexus_no_appsw_90").applies(other_nexus)  # OVRPort checks the exact build
    from frameport.recommend import engine as eng
    assert "patch_ac_nexus_no_appsw_90" in eng.suggest(nexus).patches  # catalog default (owner, 2026-10-02)
    assert "patch_ac_nexus_no_appsw_90" not in eng.suggest(other_nexus).patches  # other builds would fail to patch
    recipe = Recipe(package="com.Ubisoft.ACNexusVR",
                    patches=["patch_copy_libraries", "frame.adapter",
                             "patch_ac_nexus_no_appsw_72", "patch_ac_nexus_no_appsw_90"])
    assert any("conflicts" in w for w in engine.warnings(recipe))


def test_overport_release_sources(monkeypatch):
    from frameport.core import cache
    from frameport.tools import toolchain

    fork = {"tag_name": "v1.2.5", "assets": [{"name": "OVRPort-1.2.5-stable-cli.jar",
                                              "browser_download_url": "https://example.invalid/OVRPort.jar"}]}
    old = {"tag_name": "1.2.3", "assets": [{"name": "cli-jar.zip", "browser_download_url": "https://example.invalid/z"}]}
    replies = {"overport-release-ovrport.json": fork, "overport-release.json": old}
    monkeypatch.setattr(cache, "cached_json", lambda name, *a, **k: replies[name])
    assert toolchain.latest_overport() == ("1.2.5", "https://example.invalid/OVRPort.jar")
    replies["overport-release-ovrport.json"] = None  # fork unreachable: the original project
    assert toolchain.latest_overport() == ("1.2.3", "https://example.invalid/z")


def test_every_bundled_catalog_file_loads():
    """A YAML error silently drops a recipe from the catalog: catch it here."""
    from pathlib import Path

    from frameport.recommend import catalog

    files = sorted((Path(__file__).resolve().parents[1] / "catalog/games").glob("*.yaml"))
    loaded = {e.package for e in catalog.load(refresh=True).values() if e.origin == "bundled"}
    assert {f.stem for f in files} <= loaded


def test_vr_kind_classifies_android_apps():
    from frameport.analysis.detect import vr_kind

    assert vr_kind({"libOVRPlugin.so", "libunity.so"}, []) == "quest"
    assert vr_kind({"libopenxr_loader.so"}, ["com.oculus.supportedDevices"]) == "quest"  # Meta's OpenXR build
    assert vr_kind({"libopenxr_loader.so", "libunity.so"}, ["pvr.app.type"]) == "openxr"  # Pico OpenXR build
    assert vr_kind({"libPvr_UnitySDK.so"}, []) == "pico_sdk"
    assert vr_kind({"libwvr_api.so"}, []) == "wave"
    assert vr_kind(set(), ["android.software.xr.immersive"]) == "android_xr"
    assert vr_kind({"libgame.so"}, ["android.intent.category.LAUNCHER"]) == "none"


def test_android_app_without_vr_gets_no_vr_translation():
    from frameport.recommend import engine

    flat = _analysis(package="org.example.flat", engine="Other", xr="?", libs=["libgame.so"], is_overport_output=False,
                     has_info_category=False,
                     extra={"size": 1, "vr_kind": "none"})
    r = engine.suggest(flat)
    assert r.as_is and not r.overport and list(r.patches) == ["device.hide_navbar"] and not engine.warnings(r)
    from frameport.install.installer import install_context

    assert install_context(r).env["LEPTON_GFXRECON_FP_PROPS"] == "0\nqemu.hw.mainkeys=1"  # on by default
    needs_launcher = _analysis(package="org.example.info", engine="Other", xr="?", libs=[], has_info_category=True,
                               is_overport_output=False, extra={"size": 1, "vr_kind": "none"})
    r = engine.suggest(needs_launcher)
    assert not r.as_is and not r.overport and sorted(r.patches) == ["device.hide_navbar", "frame.launcher"]
    shown, _ = engine.visible_patches(needs_launcher, r)  # VR patches can't take effect in a 2D app
    assert "frame.launcher" in {p.id for p in shown} and all(not p.needs_vr for p in shown)
    pico = _analysis(package="com.pico.game", libs=["libPvr_UnitySDK.so"], extra={"size": 1, "vr_kind": "pico_sdk"})
    assert engine.suggest(pico).status == "unsupported"
    x86 = _analysis(package="org.example.x86", abis=["x86_64"], extra={"size": 1, "vr_kind": "none"})
    assert engine.suggest(x86).status == "unsupported"
    quest = _analysis(libs=["libOVRPlugin.so"], extra={"size": 1})  # old library entries: no vr_kind = Quest
    assert engine.suggest(quest).overport and "patch_copy_libraries" in engine.suggest(quest).patches



def test_patch_registry_is_complete_for_every_thread(monkeypatch):
    """A lookup from a second thread while the first is still importing the patch modules waits for them."""
    import threading
    import time

    from frameport.patches import base

    real = dict(base.REGISTRY)
    registry: dict = {}

    def slow_import():  # registers the patches one by one, like the real module imports
        for pid, p in real.items():
            registry[pid] = p
            time.sleep(0.0005)

    monkeypatch.setattr(base, "_loaded", False)
    monkeypatch.setattr(base, "REGISTRY", registry)
    monkeypatch.setattr(base, "_import_modules", slow_import)
    found, errors = [], []

    def lookup():
        try:
            found.append(base.get("device.hide_navbar").id)
        except KeyError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=lookup) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and found == ["device.hide_navbar"] * 4


def test_game_settings_dialog_shows_relevant_settings_and_saves_only_changes():
    from dataclasses import asdict

    from frameport.core import library
    from frameport.core.models import Recipe
    from frameport.patches.settings import SETTINGS, UI
    from frameport.ui.views import adapter_dialog as ad

    assert set(UI) == {k for k, kind, *_ in SETTINGS if kind != "str"}  # every number/switch has a label/control
    vulkan = _analysis()
    gles = _analysis(graphics="GLES (declared in manifest)")
    assert ad.relevant(vulkan, "flip_emul", 1) and not ad.relevant(gles, "flip_emul", 1)  # Vulkan-only
    assert ad.relevant(gles, "equirect_emul", 0) and not ad.relevant(vulkan, "equirect_emul", 0)
    assert ad.relevant(vulkan, "equirect_emul", 1)  # changed from its default: always shown
    r = Recipe("com.x", patches={"adapter.scale": {"value": 1.5}, "frame.launcher": {}})
    library.upsert_game("com.x", title="X", recipe=asdict(r), analysis=asdict(vulkan))
    vals = ad.recipe_values(library.game("com.x"))
    assert vals["scale"] == 1.5 and vals["controller_fix"] == 1 and vals["refresh_rate"] == 0.0
    vals.update(scale=1.0, refresh_rate=90.0, aim_pitch=-5.0)
    ad.save_to_recipe("com.x", vals)
    saved = library.game("com.x")["recipe"]
    adapter = {k: v for k, v in saved["patches"].items() if k.startswith("adapter.")}
    assert adapter == {"adapter.refresh_rate": {"value": 90.0}, "adapter.aim_pitch": {"value": -5.0}}  # no defaults
    assert "frame.launcher" in saved["patches"]  # other patches untouched
    r = library.recipe_from_dict(saved)
    r.patches["adapter.vk_shader_fix"] = {"value": "8:" + "0" * 64 + ":4:1"}
    library.upsert_game("com.x", recipe=asdict(r))
    ad.save_to_recipe("com.x", ad.recipe_values(library.game("com.x")))
    assert library.game("com.x")["recipe"]["patches"]["adapter.vk_shader_fix"]["value"].startswith("8:")  # kept
    assert saved["source"] == "user" and saved["reasons"]["adapter.aim_pitch"] == "Set by you."


def test_saving_a_recipe_keeps_works_with_issues():
    """'Save as known-good' used to store every recipe as 'works', even one marked 'works with issues'."""
    from dataclasses import asdict

    from frameport.core.models import Recipe
    from frameport.recommend.catalog import entry_from_library

    def entry(status):
        return {"package": "com.x", "title": "X", "analysis": {},
                "recipe": asdict(Recipe("com.x", status=status, notes="Right eye distorts."))}
    assert entry_from_library(entry("issues")).status == "issues"
    assert entry_from_library(entry("unknown")).status == "works"  # saved as known-good = it works
    assert entry_from_library(entry("issues"), status="works").status == "works"  # an explicit choice wins


def test_every_patch_has_a_plain_summary():
    """The game page shows patch.summary by default (plain words); adapter settings use the Game settings dialog."""
    from frameport.patches import base

    missing = [p.id for p in base.all_patches() if p.category != "adapter" and not p.summary]
    assert not missing, f"add plain summaries to patches/summaries.py: {missing}"


def test_an_already_converted_apk_is_not_converted_again(tmp_path, monkeypatch):
    """A converted copy (e.g. from an earlier FramePort) gets only the Steam Frame patches: converting it again
    replaced the platform loader and dropped its link to the Meta stand-ins (crash: cannot locate symbol ovr_...)."""
    from frameport import build
    from frameport.core.events import Reporter
    from frameport.core.models import SourceGame

    apk = tmp_path / "com.x.apk"
    apk.write_bytes(b"converted")
    (tmp_path / "com.x.alt-noforcequit.apk").write_bytes(b"converted alt")
    used = []
    monkeypatch.setattr(build.overport_tool, "patch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("OVRPort")))
    monkeypatch.setattr(build, "apply_frame_fixes",
                        lambda src, out, *a: (used.append(src.read_bytes()), out.write_bytes(b"u"), ([], []))[2])
    monkeypatch.setattr(build.sign, "sign", lambda src, dst, pkg: dst.write_bytes(b"signed"))
    monkeypatch.setattr(build, "check_apk", lambda *a, **k: [])
    analysis = _analysis(package="com.x", is_overport_output=True)
    recipe = Recipe("com.x", patches={"patch_copy_libraries": {}}, alt_patches=["patch_remove_unreal_force_quit"],
                    use_alt=True)
    src = SourceGame(name="X", apk=apk)
    res = build.build(src, analysis, recipe, tmp_path / "out", Reporter())
    assert used == [b"converted", b"converted alt"]  # both builds without OVRPort: the alternate one is the saved copy
    assert res.alt_apk.exists() and res.apk.exists()
    (tmp_path / "com.x.alt-noforcequit.apk").unlink()  # no saved alternate copy: OVRPort makes it (only that one)
    calls = []

    def fake_overport(src, work, name, ids, rep):
        calls.append(ids)
        (work / name).write_bytes(b"alt")
        return work / name
    monkeypatch.setattr(build.overport_tool, "patch", fake_overport)
    used.clear()
    build.build(src, analysis, recipe, tmp_path / "out", Reporter())
    assert used == [b"converted", b"alt"] and len(calls) == 1 and "patch_remove_unreal_force_quit" in calls[0]



def test_stand_ins_count_only_when_the_loader_links_them(monkeypatch):
    """A libovrstubs.so that nothing loads doesn't provide anything (the check passed while the game crashed)."""
    from frameport.analysis import detect

    exports = {b"loader": {"ovr_Present"}, b"stubs": {"ovr_Room_GetNextRoomArrayPage"}, b"game": set()}
    imports = {b"game": {"ovr_Present", "ovr_Room_GetNextRoomArrayPage"}}
    linked = {b"loader": []}
    monkeypatch.setattr(detect.elf, "is_elf", lambda d: True)
    monkeypatch.setattr(detect.elf, "dyn_symbols", lambda d, defined: exports[d] if defined else imports.get(d, set()))
    monkeypatch.setattr(detect.elf, "needed", lambda d: linked.get(d, []))
    libs = {"libgame.so": b"game", "libovrplatformloader.so": b"loader", "libovrstubs.so": b"stubs"}
    assert detect.missing_ovr_symbols(libs) == {"ovr_Room_GetNextRoomArrayPage"}
    linked[b"loader"] = ["libovrstubs.so"]
    assert detect.missing_ovr_symbols(libs) == set()


def test_start_activity_after_overport_conversion(tmp_path):
    """OVRPort gives every MAIN activity LAUNCHER + VR categories (WiiCompiled: LauncherActivity and QuestActivity),
    so the VR activity must come from the original APK's analysis; only it may stay a launcher."""
    from conftest import build_axml

    from frameport.apk import axml

    def activity(name):
        out = [("start", "activity", [("name", "str", name)]), ("start", "intent-filter", []),
               ("start", "action", [("name", "str", axml.MAIN)]), ("end", "action")]
        for c in (axml.LAUNCHER, axml.VR_CATEGORY):
            out += [("start", "category", [("name", "str", c)]), ("end", "category")]
        return out + [("end", "intent-filter"), ("end", "activity")]
    manifest = build_axml([("start", "manifest", [("package", "str", "org.x.game")]),
                           *activity("org.x.game.launcher.LauncherActivity"), *activity("org.x.game.QuestActivity"),
                           ("end", "manifest")])
    assert axml.vr_activity(manifest) is None  # can't be told apart any more
    a = _analysis()
    a.extra = {**(a.extra or {}), "vr_activity": "org.x.game.QuestActivity"}
    patch = base.get("frame.start_activity")
    with ApkWorkspace(_apk(tmp_path, manifest)) as ws:
        ctx = base.ApkContext(ws, a, {}, Reporter(), {"frame.start_activity": {}})
        assert patch.apply(ctx)
        assert patch.validate(ctx) == [("Only the VR activity is a launcher", True, "org.x.game.QuestActivity")]


def test_haptic_fix_suggested_for_ovrplugin_games():
    base.load_all()
    p = base.REGISTRY["adapter.haptic_fix"]
    s = p.detect(_analysis(libs=["libunity.so", "libOVRPlugin.so"]))
    assert s and s.recommended and s.params == {"value": 1}
    assert p.detect(_analysis(libs=["libunity.so"])) is None
