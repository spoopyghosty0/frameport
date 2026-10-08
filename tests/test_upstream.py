"""Upstream fixes replacing our workarounds per build (patches/upstream.py)."""
import struct
import zipfile
from pathlib import Path

import pytest

from frameport import build
from frameport.core.events import Reporter
from frameport.core.models import Analysis, Recipe
from frameport.patches import base, upstream
from frameport.patches.frame.haptic_envelope import envelope_duration_fixed
from frameport.patches.frame.ovr_microphone import microphone_checked

FIXTURES = Path(__file__).with_name("fixtures")
OLD = (FIXTURES / "libfakeruntime_old_arm64.so").read_bytes()  # OVRPort runtime 3.4.3-23204ea's code
FIXED = (FIXTURES / "libfakeruntime_fixed_arm64.so").read_bytes()  # 3.4.3-aa54c3f's


class FakeWs:
    abi = "arm64-v8a"

    def __init__(self, libs):
        self.libs = libs

    def lib(self, name):
        return f"lib/{self.abi}/{name}"

    def has(self, path):
        return path.rsplit("/", 1)[-1] in self.libs

    def read_lib(self, name):
        return self.libs[name]


class Log(Reporter):
    def __init__(self):
        super().__init__()
        self.lines = []

    def log(self, msg, *a, **k):
        self.lines.append(msg)


@pytest.fixture
def temp_fix():
    added = []

    def add(**kw):
        fix = upstream.register(upstream.UpstreamFix(**{"title": "t", "upstream": "u", **kw}))
        added.append(fix.id)
        return fix

    yield add
    for fid in added:
        upstream.REGISTRY.pop(fid, None)


def test_move_wide_constants():
    # mov x8, #0xcd6500000000 ; movk x8, #0x41cd, lsl #48  (1e9 as a double)
    words = [0xD2800000 | 2 << 21 | 0xCD65 << 5 | 8, 0xF2800000 | 3 << 21 | 0x41CD << 5 | 8]
    assert struct.pack("<Q", upstream.constants(words)[-1]) == struct.pack("<d", 1e9)
    assert upstream.move_wide(0xD503201F) is None  # nop
    # mov w9, #0x6dc9 ; nop ; movk w9, #0x3b9d, lsl #16
    words = [0x52800000 | 0x6DC9 << 5 | 9, 0xD503201F, 0x72A00000 | 0x3B9D << 5 | 9]
    assert upstream.find_constant_load(words, 1000173001) == 0
    assert upstream.find_constant_load(words[1:], 1000173001) is None  # the movk alone isn't a load


def test_haptic_probe_tells_old_and_fixed_runtime_apart():
    assert envelope_duration_fixed(OLD) is False
    assert envelope_duration_fixed(FIXED) is True
    assert envelope_duration_fixed((FIXTURES / "libfakemicrophone_arm64.so").read_bytes()) is None  # no envelope code


def test_microphone_probe_tells_old_and_fixed_runtime_apart():
    assert microphone_checked(OLD) is False
    assert microphone_checked(FIXED) is True
    assert microphone_checked((FIXTURES / "libfakeoverport_arm64.so").read_bytes()) is None  # no such function


def test_known_fixes_are_registered_for_existing_workarounds():
    base.load_all()
    fixes = {f.id: f for f in upstream.REGISTRY.values()}
    assert fixes["ovrport.haptic_envelope"].workaround == "adapter.haptic_fix"
    assert fixes["ovrport.microphone_stream"].workaround == "frame.ovr_microphone"
    for fix in fixes.values():
        assert fix.workaround in base.REGISTRY, fix.id


def test_resolve_drops_workarounds_only_when_the_fix_is_found():
    recipe = {"frame.adapter": {}, "adapter.haptic_fix": {"value": 1}, "frame.ovr_microphone": {}}
    log = Log()
    res = upstream.resolve(FakeWs({"libopenxr_loader.so": FIXED, "libovrplatformloader.so": OLD}), recipe, log)
    assert res.superseded == {"adapter.haptic_fix": "ovrport.haptic_envelope"}
    assert "adapter.haptic_fix" not in res.patches and "frame.ovr_microphone" in res.patches
    assert "adapter.haptic_fix" in recipe  # the recipe itself is unchanged
    assert any("not needed: adapter.haptic_fix" in line for line in log.lines)
    # libraries missing (can't tell) or the old runtime: every workaround stays
    assert upstream.resolve(FakeWs({}), recipe).patches == recipe
    assert upstream.resolve(FakeWs({"libopenxr_loader.so": OLD, "libovrplatformloader.so": OLD}),
                            recipe).superseded == {}


def test_resolve_keeps_the_workaround_when_a_probe_breaks(temp_fix):
    temp_fix(id="test.broken", workaround="adapter.swap_eyes", probe=lambda ws: 1 / 0)
    log = Log()
    res = upstream.resolve(FakeWs({}), {"adapter.swap_eyes": {"value": 1}}, log)
    assert "adapter.swap_eyes" in res.patches and not res.superseded
    assert any("test.broken failed" in line for line in log.lines)


def test_resolve_can_be_switched_off(temp_fix, monkeypatch):
    temp_fix(id="test.always", workaround="adapter.swap_eyes", probe=lambda ws: True)
    assert upstream.resolve(FakeWs({}), {"adapter.swap_eyes": {}}).superseded == {"adapter.swap_eyes": "test.always"}
    monkeypatch.setenv("FRAMEPORT_KEEP_WORKAROUNDS", "1")
    assert upstream.resolve(FakeWs({}), {"adapter.swap_eyes": {}}).superseded == {}


def test_without_superseded():
    assert upstream.without_superseded({"a": {}, "b": {"x": 1}}, {"a": "fix"}) == {"b": {"x": 1}}
    assert upstream.without_superseded({"a": {}}, None) == {"a": {}}


@pytest.mark.parametrize("runtime, xrshim", [(OLD, True), (FIXED, False)])
def test_build_leaves_out_the_haptic_workaround_with_a_fixed_runtime(tmp_path, monkeypatch, runtime, xrshim):
    from frameport.patches.frame import adapter

    apk = tmp_path / "in.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("lib/arm64-v8a/libopenxr_loader.so", runtime)
        z.writestr("lib/arm64-v8a/libopenxr_loader_generic.so", b"loader")
    monkeypatch.setattr(adapter, "artifact", lambda abi, name: b"artifact " + name.encode())
    recipe = Recipe("com.x", patches={"frame.adapter": {}, "adapter.haptic_fix": {"value": 1}})
    analysis = Analysis(package="com.x", version="1", label="X", abis=["arm64-v8a"], engine="Unity", xr="OpenXR",
                        graphics="Vulkan", direct_vrapi=False, libs=["libOVRPlugin.so"], launcher_activity=None,
                        has_info_category=True, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
                        oculus_os_classes=False, is_overport_output=False, debuggable=False, extra={})
    applied, _checks, superseded = build.apply_frame_fixes(apk, tmp_path / "out.apk", analysis, recipe, Log())
    assert applied == ["frame.adapter"]
    assert superseded == ({} if xrshim else {"adapter.haptic_fix": "ovrport.haptic_envelope"})
    with zipfile.ZipFile(tmp_path / "out.apk") as z:
        settings = z.read("lib/arm64-v8a/libframe_settings.so").decode()
        assert ("lib/arm64-v8a/libframe_xrshim.so" in z.namelist()) == xrshim
    assert ("haptic_fix=1" in settings) == xrshim
