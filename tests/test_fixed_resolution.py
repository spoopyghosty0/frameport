"""Full swapchain recommendations and opt-in OVRPlugin routing."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_patches import _analysis

from frameport.patches import base
from frameport.patches.frame.adapter import needs_xrshim
from frameport.patches.settings import UI, adapter_settings

ROOT = Path(__file__).resolve().parents[1]


def test_setting_is_opt_in_and_routes_ovrplugin():
    base.load_all()
    patch = base.get("adapter.fixed_resolution")
    assert patch.default == 0
    assert patch.detect(_analysis()) is None
    assert not adapter_settings({}).get("fixed_resolution")
    assert needs_xrshim({patch.id: {"value": 1}})
    assert not needs_xrshim({patch.id: {"value": 0}, "adapter.haptic_fix": {"value": 0}})
    assert UI[patch.key]["group"] == "picture"
    assert patch.applies(_analysis(xr="OpenXR", libs=[]))
    assert patch.applies(_analysis(xr="VrApi", libs=["libOVRPlugin.so"]))
    assert not patch.applies(_analysis(xr="VrApi", libs=[]))
    assert not patch.applies(_analysis(abis=["armeabi-v7a"], xr="OpenXR"))


@pytest.mark.native
def test_layer_dimensions(tmp_path):
    inc = next(iter((ROOT / "native/.cache").glob("openxr-*")), None)
    cc = shutil.which("cc")
    if not os.environ.get("FRAMEPORT_NATIVE_TESTS") or not inc or not cc:
        pytest.skip("needs FRAMEPORT_NATIVE_TESTS=1, OpenXR headers and cc")
    exe = tmp_path / "fixed-resolution"
    subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-missing-field-initializers", "-I", str(inc),
                    "-I", str(ROOT / "native/adapter"),
                    str(ROOT / "tests/fixtures/src/fixed_resolution_test.c"), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


def test_triage_only_offers_fix_for_offset_rects():
    from frameport.validate.triage import triage
    log = "projection view 0 rect change: swapchain=0x123 rect=0,32 2736x2736 array=0"
    found = [f for f in triage(log).findings if f.id == "dynamic-resolution-foveation"]
    assert found and found[0].suggest == ["adapter.fixed_resolution"] and found[0].question
    assert not any(f.id == "dynamic-resolution-foveation" for f in
                   triage(log.replace("rect=0,32", "rect=0,0")).findings)
    assert not any(f.id == "dynamic-resolution-foveation" for f in
                   triage(log + "\nfixed_resolution: recommended 2704x2704 -> 2768x2768").findings)


def test_catalog_requires_app_with_the_fix(monkeypatch):
    import yaml

    import frameport
    from frameport.recommend.catalog import unusable_reason
    entry = yaml.safe_load((ROOT / "catalog/games/com.camouflaj.manta.yaml").read_text())
    assert entry["adapter"]["fixed_resolution"] == 1
    monkeypatch.setattr(frameport, "__version__", "1.2.1")
    assert unusable_reason(entry) == "needs FramePort 1.2.2"
    monkeypatch.setattr(frameport, "__version__", "1.2.2")
    assert unusable_reason(entry) is None


@pytest.mark.native
def test_recommendation_wrapper(tmp_path):
    inc = next(iter((ROOT / "native/.cache").glob("openxr-*")), None)
    cc = shutil.which("cc")
    if not os.environ.get("FRAMEPORT_NATIVE_TESTS") or not inc or not cc:
        pytest.skip("needs FRAMEPORT_NATIVE_TESTS=1, OpenXR headers and cc")
    # Compile the actual wrapper with a stand-in runtime and swapchain table.
    source = (ROOT / "native/adapter/frame_adapter.c").read_text()
    wrapper = source[source.index("static XRAPI_ATTR XrResult XRAPI_CALL fixed_recommended_resolution"):
                     source.index("// Clamp `r` to a w x h image;")]
    harness = (ROOT / "tests/fixtures/src/fixed_resolution_wrapper_test.c").read_text()
    unit = tmp_path / "wrapper.c"
    unit.write_text(harness.replace("/* WRAPPER */", wrapper))
    exe = tmp_path / "wrapper"
    subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-missing-field-initializers", "-I", str(inc),
                    "-I", str(ROOT / "native/adapter"), str(unit), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


def _unity_workspace(tmp_path, quest_manifest, lib):
    import zipfile

    from frameport.apk.workspace import ApkWorkspace
    from frameport.patches.frame.unity_text_input import METADATA
    apk = tmp_path / "in.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", quest_manifest)
        z.writestr("classes.dex", b"dex\n035\0")
        z.writestr("lib/arm64-v8a/libopenxr_loader_generic.so", b"\x7fELF-original-loader")
        z.writestr("lib/arm64-v8a/libil2cpp.so", lib)
        z.writestr(METADATA, b"metadata")
    return ApkWorkspace(apk)


@pytest.mark.parametrize("on, engine", [(1, "Unity"), (0, "Unity"), (1, "Unreal")])
def test_unity_viewport_setters_pinned_with_the_setting(tmp_path, quest_manifest, monkeypatch, on, engine):
    """#574: a Unity game's own scaler (Rogue Piñatas) reaches the picture through XRSettings.renderViewportScale and
    ScalableBufferManager.ResizeBuffers; with fixed_resolution both return at once, without it nothing changes."""
    from frameport.analysis import il2cpp
    from frameport.core.events import Reporter
    from frameport.patches.frame import unity_fixed_viewport as V
    from frameport.patches.frame import unity_text_input as U

    lib = sorted((ROOT / "tests/fixtures").glob("libfake*_arm64.so"))[0].read_bytes()
    lo, _hi = U._executable(lib)[0]
    a, b = (lo + 0x40) & ~3, (lo + 0x80) & ~3
    calls = []

    def find_methods(lib_bytes, metadata, version, wanted, **kw):
        calls.append(wanted)
        assert set(V.UnityFixedViewport.targets) <= set(wanted)  # one shared Cpp2IL lookup
        return {V.XR_SETTINGS: {"set_renderViewportScale": (a, 0xB0)},
                V.BUFFER_MANAGER: {"ResizeBuffers": (b, 0x40)}}
    monkeypatch.setattr(il2cpp, "find_methods", find_methods)
    base.load_all()
    recipe = {"frame.adapter": {}, "adapter.fixed_resolution": {"value": on}}
    analysis = _analysis(engine=engine, libs=["libil2cpp.so", "libunity.so", "libOVRPlugin.so"],
                         extra={"size": 1, "unity_version": "2022.3.52f1"})
    with _unity_workspace(tmp_path, quest_manifest, lib) as ws:
        ctx = base.ApkContext(ws, analysis, {}, Reporter(), recipe)
        assert base.get("frame.adapter").apply(ctx)
        out = ws.read_lib("libil2cpp.so")
    if on and engine == "Unity":
        assert out[a:a + 8] == V.RET_VOID and out[b:b + 8] == V.RET_VOID and len(calls) == 1
        assert any("XRSettings.set_renderViewportScale" in n for n in ctx.notes)
        assert any("ScalableBufferManager.ResizeBuffers" in n for n in ctx.notes)
    else:
        assert out == lib and not calls


def test_setting_is_an_apk_setting():
    """Switched on in Game settings, the setting has to rebuild the APK (xrshim + Unity setters), not only reach
    settings.conf: #574's build had neither."""
    from frameport.game_update import APK_SETTINGS, apk_recipe_fingerprint

    assert "adapter.fixed_resolution" in APK_SETTINGS
    off = {"patches": {"frame.adapter": {}}}
    on = {"patches": {"frame.adapter": {}, "adapter.fixed_resolution": {"value": 1}}}
    assert apk_recipe_fingerprint(off, "com.NerdNinjas.fiesta") != apk_recipe_fingerprint(on, "com.NerdNinjas.fiesta")


def test_triage_spots_a_shrinking_eye_rect():
    from frameport.validate.triage import triage
    log = ("FrameBridge: projection view 0 rect change: swapchain=0xfffe7579d180 rect=0,0 4048x4048 array=0\n"
           "FrameBridge: projection view 1 rect change: swapchain=0xfffe7579d180 rect=0,0 4048x4048 array=1\n"
           "FrameBridge: projection view 0 rect change: swapchain=0xfffe7579d180 rect=0,0 1504x1504 array=0\n")
    found = [f for f in triage(log).findings if f.id == "dynamic-resolution-shrink"]
    assert found and found[0].suggest == ["adapter.fixed_resolution"] and found[0].question
    # another swapchain (a recreated eye buffer) or the same size again is no shrink
    other = log.replace("swapchain=0xfffe7579d180 rect=0,0 1504", "swapchain=0xfffe75457280 rect=0,0 1504")
    assert not any(f.id == "dynamic-resolution-shrink" for f in triage(other).findings)
    same = log.replace("1504x1504", "4048x4048")
    assert not any(f.id == "dynamic-resolution-shrink" for f in triage(same).findings)
    fixed = log + "FrameBridge: fixed_resolution: recommended 1504x1504 -> 4048x4048 (runtime=0, layer)\n"
    assert not any(f.id == "dynamic-resolution-shrink" for f in triage(fixed).findings)
