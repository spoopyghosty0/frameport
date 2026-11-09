"""FrameBridge's XR_FB_space_warp emulation (adapter space_warp_emul, GitHub #336): host harness for
native/adapter/space_warp.c, the hooks that route to it, the setting, its triage signature and OutRun's recipe."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INC = ROOT / "native/.cache/openxr-f2448a8797"
CC = shutil.which("cc") or shutil.which("clang")
GRAPHICS = ("GLES3", "EGL", "KHR", "vulkan")
ADAPTER = ROOT / "native/adapter/frame_adapter.c"


@pytest.mark.native
@pytest.mark.skipif(not os.environ.get("FRAMEPORT_NATIVE_TESTS") or not INC.is_dir() or not CC,
                    reason="set FRAMEPORT_NATIVE_TESTS=1; needs cached OpenXR and host GLES/EGL/Vulkan headers")
def test_space_warp_harness(tmp_path):
    inc = tmp_path / "include"
    candidates = [Path("/usr/include"), *(ROOT / "native/.cache").glob(
        "ndk-*/toolchains/llvm/prebuilt/*/sysroot/usr/include")]
    graphics = next((p for p in candidates if all((p / d).is_dir() for d in GRAPHICS)), None)
    if graphics is None:
        pytest.skip("needs GLES3/EGL/KHR/vulkan headers (host development packages or cached NDK)")
    for directory in GRAPHICS + tuple(d for d in ("vk_video",) if (graphics / d).is_dir()):
        shutil.copytree(graphics / directory, inc / directory)
    exe = tmp_path / "space_warp"
    subprocess.run([CC, "-std=c11", "-pthread", "-Wall", "-Wextra", "-Werror", "-Wno-missing-field-initializers",
                    "-I", str(INC), "-I", str(inc), "-I", str(ROOT / "native/adapter"),
                    str(ROOT / "tests/native/space_warp_harness.c"), "-o", str(exe), "-ldl"], check=True)
    out = subprocess.run([str(exe)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_adapter_routes_space_warp_swapchains():
    src = ADAPTER.read_text()
    assert '#include "space_warp.c"' in src
    assert 'sscanf(line, "space_warp_emul=%f", &value)' in src
    # served before every other swapchain path, and every swapchain call knows the served handles
    create = src.split("XRAPI_ATTR XrResult XRAPI_CALL xrCreateSwapchain(", 1)[1].split("\n}\n", 1)[0]
    assert create.index("sw_create(session, info, swapchain)") < create.index("emul_virtual_create")
    assert create.index("sw_create(session, info, swapchain)") < create.index("fn(session, &fixed, swapchain)")
    for fn, marker in (("xrDestroySwapchain", "sw_destroy(swapchain)"),
                       ("xrEnumerateSwapchainImages", "sw_enumerate(swapchain"),
                       ("xrAcquireSwapchainImage", "sw_acquire(swapchain, index)"),
                       ("xrWaitSwapchainImage", "sw_served_wait(swapchain)"),
                       ("xrReleaseSwapchainImage", "sw_served_release(swapchain)")):
        body = src.split(f"XRAPI_ATTR XrResult XRAPI_CALL {fn}(", 1)[1].split("\n}\n", 1)[0]
        assert marker in body, fn
    # properties filled, the extension offered when the runtime lacks it, info stripped at xrEndFrame
    assert "sw_properties(instance, system, properties)" in src
    assert "if (space_warp_emul && !sw_runtime_has_extension)" in src
    assert "(hide_space_warp || space_warp_emul) && layer->type == XR_TYPE_COMPOSITION_LAYER_PROJECTION" in src
    end_frame = src.split("XRAPI_ATTR XrResult XRAPI_CALL xrEndFrame(", 1)[1]
    assert "!hide_space_warp && !space_warp_emul) || !info" in end_frame  # the fast path can't skip the strip
    # gipa: wait/release wrapped in front of the other hooks, formats only with the setting
    gipa = src.split("XRAPI_ATTR XrResult XRAPI_CALL xrGetInstanceProcAddr(", 1)[1]
    sw_block = gipa.index("if (space_warp_emul) {")
    assert sw_block < gipa.index("if (surface_native) {")
    assert "HOOK(xrEnumerateSwapchainFormats)" in gipa
    formats = src.split("XRAPI_ATTR XrResult XRAPI_CALL xrEnumerateSwapchainFormats(", 1)[1].split("\n}\n", 1)[0]
    assert "enumerate_formats_inner(session, capacity, count, formats)" in formats  # exported: dlsym callers too
    inner = src.split("static XrResult enumerate_formats_inner(", 1)[1].split("\n}\n", 1)[0]
    assert "if (space_warp_emul) return sw_enumerate_formats(" in inner  # (+ unorm_formats' twins after it, #400)
    assert "sw_session_destroy(session)" in (ROOT / "native/adapter/layer_emul_gl.c").read_text()


def test_artifacts_carry_space_warp_emulation():
    for abi in ("arm64-v8a", "armeabi-v7a"):
        data = (ROOT / "artifacts" / abi / "libopenxr_loader_generic.so").read_bytes()
        for marker in (b"space_warp_emul=%f", b"space_warp_emul: xrCreateSwapchain",
                       b"space warp: runtime motion-vector"):
            assert marker in data, (abi, marker)


def test_setting_registered_with_ui_and_docs():
    from frameport.patches import settings
    from frameport.patches.base import get

    keys = {k for k, *_ in settings.SETTINGS}
    assert "space_warp_emul" in keys
    default = next(d for k, _kind, d, *_ in settings.SETTINGS if k == "space_warp_emul")
    assert default == 0
    assert settings.UI["space_warp_emul"]["group"] == "troubleshooting"
    patch = get("adapter.space_warp_emul")
    assert patch is not None
    assert "space_warp_emul" in (ROOT / "docs/PATCHES.md").read_text()


def test_triage_suggests_space_warp_emulation():
    from frameport.validate import triage

    line = "10-24 06:14:55.694  1251  1300 E OutRunQuest: AppSW recommended buffers unavailable"
    findings = triage.triage(line).findings
    assert any(f.id == "appsw-required" and "adapter.space_warp_emul" in f.suggest for f in findings)
    on = line + "\n10-24 06:14:53.328  1251  1300 I FrameBridge: per-game: space_warp_emul=1 (XR_FB_space_warp emulated"
    assert not any(f.id == "appsw-required" for f in triage.triage(on).findings)


def test_outrun_recipe_uses_the_emulation():
    import yaml

    entry = yaml.safe_load((ROOT / "catalog/games/org.outrun.quest.vr.yaml").read_text())
    adapter = entry.get("adapter") or {}
    assert adapter.get("space_warp_emul") == 1 and adapter.get("fb_color_space") == 1
    assert not adapter.get("hide_space_warp")
    assert re.match(r"\d{4}-\d{2}-\d{2}", str(entry.get("updated") or entry["verified"]["date"]))
