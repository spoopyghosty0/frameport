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


def test_vk_hide_fdm_is_a_per_game_unreal_setting():
    """Metro Awakening: Unreal's Vulkan isn't always detected, so it's offered for every Unreal game; off by default."""
    from frameport.patches.settings import UI

    patch = base.get("adapter.vk_hide_fdm")
    assert patch.applies(_analysis(engine="Unreal", graphics="GLES or unknown (no Vulkan declaration)"))
    assert not patch.applies(_analysis(engine="Unity"))
    assert "vk_hide_fdm" not in adapter_settings({}) and UI["vk_hide_fdm"]["group"] == "troubleshooting"


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


def test_ovr_microphone_guards_unopened_stream():
    from frameport.analysis import elf
    from frameport.patches.frame.ovr_microphone import guard_microphone

    lib = _fixture("libfakemicrophone_arm64.so")
    out = guard_microphone(lib)
    assert out is not None and len(out) == len(lib)
    ins = [(m, o) for a, m, o in elf.text_instructions(out)]
    start = ins.index(("ldr", "x0, [x0, #0x18]"))
    assert [m for m, _ in ins[start:start + 7]] == ["ldr", "cbz", "stp", "bl", "ldp", "sxtw", "ret"]
    calls = lambda data: [o for _, m, o in elf.text_instructions(data) if m == "bl"]  # noqa: E731
    assert calls(out) == calls(lib)  # still calls AAudioStream_getFramesPerBurst
    assert guard_microphone(out) is None  # idempotent
    assert guard_microphone(_fixture("libfakeoverport_arm64.so")) is None  # no such function


def test_ovr_microphone_suggested_only_for_microphone_games():
    p = base.get("frame.ovr_microphone")
    assert p.detect(_analysis(engine="Unreal", libs=["libUE4.so"], extra={"ovr_microphone": True})).recommended
    assert p.detect(_analysis(engine="Unreal", libs=["libUE4.so"])) is None
    assert not p.default_on


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
                        lambda src, out, *a: (used.append(src.read_bytes()), out.write_bytes(b"u"), ([], [], {}))[2])
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


def test_foveation_choice_sets_the_launcher_env_and_wins_over_lepton_env():
    from frameport.install.installer import install_context

    assert base.get("device.foveation").CHOICES[0][0] == ""  # Valve's default needs no patch
    r = Recipe("com.x.game", patches={"device.foveation": {"mode": "off"},
                                      "device.lepton_env": {"env": {"VK_INSTANCE_LAYERS": "VK_LAYER_x"}}})
    assert install_context(r).env["VK_INSTANCE_LAYERS"] == ""  # applied last, whatever the recipe's order
    r = Recipe("com.x.game", patches={"device.foveation": {"mode": "fixed"}})
    assert install_context(r).env == {"FDM_DEBUG": "disable_offsets"}


def test_catalog_foveation_reaches_the_recipe(monkeypatch):
    from frameport.recommend import catalog, engine

    entry = catalog.CatalogEntry(package="com.drbeef.rtcwquest", title="RTCWQuest", status="works", foveation="off")
    assert catalog.CatalogEntry.from_dict(yaml_load(catalog.to_yaml(entry)), "bundled").foveation == "off"
    monkeypatch.setattr(catalog, "lookup", lambda package: entry)
    r = engine.suggest(_analysis(package="com.drbeef.rtcwquest", engine="Other", libs=["libopenxr_loader.so"],
                                 extra={"size": 1}))
    assert r.params("device.foveation") == {"mode": "off"}


def yaml_load(text):
    import yaml

    return yaml.safe_load(text)


def test_vrapi_stub_keeps_every_function_of_metas_loader(tmp_path, quest_manifest):
    from frameport.analysis import elf

    apk = _apk(tmp_path, quest_manifest)
    metas = build_stub_library(["vrapi_Initialize", "vrapi_SetPropertyInt", "vrapi_GetHmdInfo"], soname="libvrapi.so")
    metas = metas.replace(b"\x00\x00\x80\xd2", b"\x20\x00\x80\xd2")  # different code, same exports (mov x0, #1)
    with ApkWorkspace(apk) as ws:
        ws.put(ws.lib("libvrapi.so"), metas)
        ws.put(ws.lib("libOVRPlugin.so"), build_stub_library(["ovrp_GetVersion"], soname="libOVRPlugin.so"))
        patch = base.get("frame.vrapi_stub")
        a = _analysis(libs=["libvrapi.so", "libOVRPlugin.so"], direct_vrapi=False)
        assert patch.applies(a) and patch.detect(a) is None  # suggested by triage only
        assert not patch.applies(_analysis(libs=["libvrapi.so"], direct_vrapi=True))  # that's the bridge's case
        ctx = base.ApkContext(ws, a, {}, Reporter(), {"frame.vrapi_stub": {}})
        assert patch.apply(ctx)
        stub = ws.read_lib("libvrapi.so")
        assert elf.dyn_symbols(stub, True) == {"vrapi_Initialize", "vrapi_SetPropertyInt", "vrapi_GetHmdInfo"}
        assert elf.soname(stub) == "libvrapi.so" and not patch.apply(ctx)  # applying twice changes nothing
    assert "frame.vrapi_bridge" in base.get("frame.vrapi_stub").conflicts


def test_vrapi_stub_covers_jurassic_world_aftermath():
    """With the real APK's libraries (FRAMEPORT_GAMES): every vrapi_* OVRPlugin imports is in the stub."""
    import os
    from pathlib import Path

    from frameport.analysis import elf

    root = os.environ.get("FRAMEPORT_GAMES")
    if not root:
        import pytest
        pytest.skip("FRAMEPORT_GAMES not set")
    apks = list(Path(root).glob("Jurassic World Aftermath*/com.coatsink.alone.apk"))
    if not apks:
        import pytest
        pytest.skip("Jurassic World Aftermath not in FRAMEPORT_GAMES")
    with zipfile.ZipFile(apks[0]) as z:
        vrapi, plugin = (z.read(f"lib/arm64-v8a/{n}") for n in ("libvrapi.so", "libOVRPlugin.so"))
    exports = sorted(s for s in elf.dyn_symbols(vrapi, True) if s.startswith(("vrapi_", "ovr")))
    need = {s for s in elf.dyn_symbols(plugin, False) if s.startswith("vrapi_")}
    assert need and need <= elf.dyn_symbols(build_stub_library(exports, soname="libvrapi.so"), True)


def test_static_check_finds_vrapi_functions_the_bridge_lacks(tmp_path, monkeypatch, quest_manifest):
    from frameport.apk import sign
    from frameport.validate import static

    monkeypatch.setattr(sign, "verify", lambda apk: (True, ""))
    monkeypatch.setattr(sign, "alignment_problems", lambda apk: [])
    apk = tmp_path / "game.apk"
    game = build_stub_library(["game_main"], soname="libtargemapp.so")
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", quest_manifest)
        z.writestr("lib/arm64-v8a/libvrapi.so", build_stub_library(["vrapi_Initialize"], soname="libvrapi.so"))
        z.writestr("lib/arm64-v8a/libtargemapp.so", game)
    monkeypatch.setattr(static.elf, "dyn_symbols", lambda data, defined: (
        {"vrapi_Initialize"} if defined else {"vrapi_Initialize", "vrapi_PollEvent"}) if data == game or defined
        else set())
    check = next(c for c in static.check_apk(apk, expect_adapter=False) if c["name"] == "VrApi functions resolvable")
    assert check["ok"] is False and check["detail"] == "vrapi_PollEvent"  # BlazeRush (GitHub #57)


def test_bridge_exports_what_blazerush_imports():
    from frameport.analysis import elf
    from frameport.patches.frame import artifact

    have = elf.dyn_symbols(artifact("arm64-v8a", "libvrapi.so"), True)
    assert {"vrapi_PollEvent", "vrapi_GetSystemPropertyFloatArray", "vrapi_RecenterPose",
            "vrapi_SetDisplayRefreshRate"} <= have


def test_tbxr_vendor_adds_the_valve_loader(tmp_path, quest_manifest):
    from frameport.analysis import elf

    apk = _apk(tmp_path, quest_manifest)
    patch = base.get("frame.tbxr_vendor")
    a = _analysis(libs=["libxash.so", "libopenxr_loader_meta.so"], extra={"size": 1, "tbxr_libs": ["libxash.so"]})
    assert patch.applies(a) and patch.detect(a) and not patch.applies(_analysis())
    with ApkWorkspace(apk) as ws:
        assert patch.apply(base.ApkContext(ws, a, {}, Reporter(), {patch.id: {}}))  # (libxash.so absent here)
        loader = ws.read_lib("libopenxr_loader_valve.so")
        assert elf.soname(loader) == "libopenxr_loader_valve.so"  # loadLibrary("openxr_loader_valve") finds it
        assert not patch.apply(base.ApkContext(ws, a, {}, Reporter(), {patch.id: {}}))


def test_tbxr_vendor_on_lambda1vr():
    """With the real APK (FRAMEPORT_GAMES): the six "meta" checks of libxash.so now match "valve"."""
    import os
    from pathlib import Path

    import pytest

    from frameport.analysis import elf

    root = os.environ.get("FRAMEPORT_GAMES")
    apks = list(Path(root).glob("Half Life 1 VR*/com.drbeef.lambda1vr.apk")) if root else []
    if not apks:
        pytest.skip("Lambda1VR not in FRAMEPORT_GAMES")
    with zipfile.ZipFile(apks[0]) as z:
        xash = z.read("lib/arm64-v8a/libxash.so")
    assert b"\0OPENXR_HMD\0" in xash and b"\0meta\0" in xash
    fixed, count = elf.replace_rodata_string(xash, "meta", "alve")
    assert count == 1 and b"\0alve\0" in fixed and len(fixed) == len(xash)


def test_unity_user_presence_points_the_xr_plugin_at_the_shim(tmp_path, quest_manifest):
    """BONELAB: OVRPlugin reported the worn headset as not worn and the Marrow rig froze (no tracking, no controls)."""
    from frameport.analysis import elf
    from frameport.patches.frame import unity_user_presence as P

    fake = _fixture("libfakeovrplugin_arm64.so")
    xr_plugin, n = elf.replace_rodata_string(fake, "xrGetInstanceProcAddr", P.CALL)  # a stand-in with the lookup name
    assert n == 1
    apk = _apk(tmp_path, quest_manifest)
    with zipfile.ZipFile(apk, "a") as z:
        z.writestr("lib/arm64-v8a/libOculusXRPlugin.so", xr_plugin)
        z.writestr("lib/arm64-v8a/libOVRPlugin.so", fake)
    p = base.get("frame.unity_user_presence")
    a = _analysis(engine="Unity", libs=["libunity.so", "libOVRPlugin.so", "libOculusXRPlugin.so"])
    assert p.applies(a) and p.detect(a) is None  # per game, from the catalog
    assert not p.applies(_analysis(engine="Unity", libs=["libunity.so", "libOVRPlugin.so"]))
    with ApkWorkspace(apk) as ws:
        ctx = base.ApkContext(ws, a, {}, Reporter(), {})
        assert p.apply(ctx)
        assert p.validate(ctx) == [("User presence shim loaded", True, P.SHIM)]
        out = ws.write(tmp_path / "out.apk")
    with zipfile.ZipFile(out) as z:
        patched = z.read("lib/arm64-v8a/libOculusXRPlugin.so")
        assert len(patched) == len(xr_plugin) and P.SHIM_CALL.encode() + b"\0" in patched
        assert P.SHIM in elf.needed(z.read("lib/arm64-v8a/libOVRPlugin.so"))
        shim = z.read(f"lib/arm64-v8a/{P.SHIM}")
    assert P.SHIM_CALL in elf.dyn_symbols(shim, True)  # the rebuilt artifact exports the wrapper


def test_microphone_fix_is_not_suggested_for_unitys_platform_wrapper(quest_manifest, tmp_path):
    """Unity's C# platform wrapper (libil2cpp.so) names every Meta function, used or not: no reason to patch."""
    from frameport.analysis.detect import analyze

    def apk(lib):
        p = tmp_path / f"{lib}.apk"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("AndroidManifest.xml", quest_manifest)
            z.writestr(f"lib/arm64-v8a/{lib}", b"\x7fELF\0ovr_Microphone_GetOutputBufferMaxSize\0")
        return p
    assert not analyze(apk("libil2cpp.so")).extra["ovr_microphone"]
    assert analyze(apk("libUE4.so")).extra["ovr_microphone"]  # Unreal's voice code calls it (TWD Ch. 2)


def test_to_string_stubs_return_an_empty_string():
    """ovrPeerConnectionState_ToString & co. (BlazeRush): OVRPort's loader lacks them; NULL could crash the caller."""
    import struct

    from frameport.analysis import detect, elf

    lib = build_stub_library(["ovr_Foo", "ovrPeerConnectionState_ToString"], soname="libovrstubs.so")
    syms = elf._elf(lib).get_section_by_name(".dynsym")
    at = syms.get_symbol_by_name("ovrPeerConnectionState_ToString")[0]["st_value"]
    word = struct.unpack_from("<I", lib, at)[0]
    imm = ((word >> 29) & 3) | ((word >> 5) & 0x7FFFF) << 2
    imm -= (1 << 21) if imm & (1 << 20) else 0
    assert word & 0x9F00001F == 0x10000000 and lib[at + imm] == 0  # adr x0, <a zero byte>
    foo = syms.get_symbol_by_name("ovr_Foo")[0]["st_value"]
    assert struct.unpack_from("<I", lib, foo)[0] == 0xD2800000  # everything else still returns 0
    assert detect.OVR_TO_STRING.match("ovrVoipMuteState_ToString")
    assert not detect.OVR_TO_STRING.match("ovrAvatar_Create")


def test_avatar_stub_keeps_the_loaders_functions(tmp_path, quest_manifest):
    from frameport.analysis import elf

    apk = _apk(tmp_path, quest_manifest)
    with ApkWorkspace(apk) as ws:
        real = build_stub_library(["ovrAvatar_InitializeAndroid", "ovrAvatarMessage_Pop"],
                                  soname="libovravatarloader.so")
        ws.put(ws.lib("libovravatarloader.so"), real.replace(b"\x00\x00\x80\xd2", b"\x20\x00\x80\xd2"))
        p = base.get("frame.avatar_stub")
        a = _analysis(libs=["libovravatarloader.so"])
        assert p.applies(a) and p.detect(a) is None
        assert p.apply(base.ApkContext(ws, a, {}, Reporter(), {p.id: {}}))
        stub = ws.read_lib("libovravatarloader.so")
        assert elf.dyn_symbols(stub, True) == {"ovrAvatar_InitializeAndroid", "ovrAvatarMessage_Pop"}


def test_unreal_gl_shim_offered_for_unreal_gles_only():
    """Star Wars Pinball VR (GitHub #83): Zink crashes Unreal's MSRTT path; the shim is an option, not a suggestion."""
    shim = base.get("frame.unreal_gl_shim")
    gles = _analysis(engine="Unreal", graphics="GLES or unknown (no Vulkan declaration)")
    assert shim.applies(gles) and shim.detect(gles) is None
    assert not shim.applies(_analysis(engine="Unreal"))  # Vulkan
    assert not shim.applies(_analysis(engine="Unity", graphics="GLES (Unity boot.config)"))
    assert not shim.applies(gles.__class__(**{**gles.__dict__, "abis": ["armeabi-v7a"]}))


def test_gl_shim_keeps_unreal_multiview_msrtt():
    """Unreal 4.25 enables multiview only with GL_OVR_multiview_multisampled_render_to_texture listed: the shim keeps it
    for Unreal and draws its function single-sampled (the prebuilt library must be rebuilt from the current source)."""
    from frameport.patches.frame import artifact

    shim = artifact("arm64-v8a", "libglshim.so")
    assert b"multiview multisampled render-to-texture (%d samples) drawn single-sampled" in shim
    assert b"glFramebufferTextureMultiviewOVR\0" in shim


def test_slz_vulkan_hooks_switches_off_both_registrations(tmp_path, quest_manifest):
    """BONELAB 1.2974: SLZ's graphics plugin crashed Unity's Vulkan start-up; only its two registrations go."""
    import struct

    from frameport.patches.frame import slz_vulkan_hooks as S

    lib = _fixture("libfakeslz_arm64.so")
    sites = S.find_registrations(lib)
    assert len(sites) == 2
    apk = _apk(tmp_path, quest_manifest)
    with zipfile.ZipFile(apk, "a") as z:
        z.writestr(f"lib/arm64-v8a/{S.LIB}", lib)
    p = base.get("frame.slz_vulkan_hooks")
    a = _analysis(engine="Unity", libs=["libunity.so", S.LIB])
    assert p.applies(a) and p.detect(a).recommended
    assert not p.applies(_analysis(engine="Unity", libs=["libunity.so"]))
    with ApkWorkspace(apk) as ws:
        ctx = base.ApkContext(ws, a, {}, Reporter(), {})
        assert p.apply(ctx)
        assert p.validate(ctx) == [("SLZ Vulkan hooks off", True, "")]
        assert not p.apply(ctx)  # nothing left to switch off
        out = ws.write(tmp_path / "out.apk")
    with zipfile.ZipFile(out) as z:
        patched = z.read(f"lib/arm64-v8a/{S.LIB}")
    changed = [i for i in range(0, len(lib), 4) if lib[i:i + 4] != patched[i:i + 4]]
    assert changed == sites and all(struct.unpack_from("<I", patched, o)[0] == S.NOP for o in sites)


def test_bonelab_recipe_serves_both_builds():
    """One catalog entry for BONELAB 1.2068 (no SLZ plugin: the Vulkan-hook patch changes nothing) and 1.2974."""
    from pathlib import Path

    import yaml

    entry = yaml.safe_load((Path(__file__).resolve().parents[1] / "catalog/games/com.StressLevelZero.BONELAB.yaml")
                           .read_text(encoding="utf-8"))
    assert {"frame.unity_user_presence", "frame.slz_vulkan_hooks"} <= set(entry["frame"])
    hooks, presence = base.get("frame.slz_vulkan_hooks"), base.get("frame.unity_user_presence")
    libs = ["libunity.so", "libOVRPlugin.so", "libOculusXRPlugin.so"]
    old, new = _analysis(engine="Unity", libs=libs), _analysis(engine="Unity", libs=libs + ["libSLZQuestNative.so"])
    assert presence.applies(old) and presence.applies(new)
    assert not hooks.applies(old) and hooks.applies(new)
