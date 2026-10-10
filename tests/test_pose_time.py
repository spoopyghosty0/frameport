"""pose_time_fix (FrameBridge): located times from the monotonic clock or far in the past are moved, others kept."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from frameport.patches import base
from frameport.patches.settings import adapter_settings


def test_pose_time_fix_moves_monotonic_and_far_past_times(tmp_path):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc or os.name == "nt":
        pytest.skip("needs a POSIX host C compiler")
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "pose-time-test"
    subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread", "-I", str(root / "native/adapter"),
                    str(root / "tests/fixtures/src/pose_time_test.c"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)


def _analysis(**kw):
    from frameport.core.models import Analysis

    d = dict(package="com.x.game", version="1", label="X", abis=["arm64-v8a"], engine="Unity", xr="VrApi",
             graphics="GLES", direct_vrapi=False, libs=["libunity.so", "libOVRPlugin.so"], launcher_activity=None,
             has_info_category=True, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
             oculus_os_classes=False, is_overport_output=False, debuggable=True, extra={})
    d.update(kw)
    return Analysis(**d)


def test_pose_time_fix_suggested_for_unity_builtin_oculus_games():
    base.load_all()
    p = base.REGISTRY["adapter.pose_time_fix"]
    s = p.detect(_analysis(extra={"oculus_xr_plugin": False}))
    assert s and s.recommended and s.params == {"value": 1}
    assert p.detect(_analysis(extra={"oculus_xr_plugin": True})) is None  # Oculus XR Plugin: not measured
    assert p.detect(_analysis(libs=["libunity.so", "libOVRPlugin.so", "libOculusXRPlugin.so"], extra={})) is None
    assert p.detect(_analysis(engine="Unreal", libs=["libUE4.so", "libOVRPlugin.so"], extra={})) is None
    assert not p.applies(_analysis(libs=["libunity.so"], extra={}))
    assert adapter_settings({"adapter.pose_time_fix": {"value": 1}})["pose_time_fix"] == 1
    assert base.REGISTRY["adapter.pose_debug"].default == 0
