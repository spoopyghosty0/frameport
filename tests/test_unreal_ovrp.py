"""frame.unreal_ovrp_entrypoints (GitHub #83): Unreal looks up every OVRPlugin function it was built against; OVRPort's
OVRPlugin lacks some (Star Wars Pinball VR, UE 4.25: ovrp_GetPTWNear), so they get stand-ins returning ovrpFailure."""
import struct
import zipfile

from frameport.analysis import detect, elf
from frameport.analysis.stubgen import build_stub_library
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.core.paths import user_data_dir
from frameport.patches import base
from frameport.patches.frame import unreal_ovrp

LOOKUPS = ["ovrp_GetPTWNear", "ovrp_GetVersion2", "ovrp_Initialize5"]
# libUE4.so's wrapper name table: the names are plain strings to dlsym (a stub library's .dynstr holds them alike)
ENGINE = build_stub_library(LOOKUPS + ["FEngineLoop_Init"], soname="libUE4.so")
PLUGIN = build_stub_library(["ovrp_GetVersion2", "ovrp_Initialize5", "ovrp_GetNewThing"], soname="libOVRPlugin.so")


def _analysis(**extra):
    return Analysis(package="com.example.unreal", version="1.0", label="Unreal Game", abis=["arm64-v8a"],
                    engine="Unreal", xr="OpenXR", graphics="GLES or unknown (no Vulkan declaration)",
                    direct_vrapi=False, libs=["libOVRPlugin.so", "libUE4.so"], launcher_activity=None,
                    has_info_category=False, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
                    oculus_os_classes=False, is_overport_output=True, debuggable=False, extra={"size": 1, **extra})


def _apk(tmp_path, manifest, plugin=PLUGIN):
    p = tmp_path / "in.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
        z.writestr("classes.dex", b"dex\n035\0")
        z.writestr("lib/arm64-v8a/libUE4.so", ENGINE)
        z.writestr("lib/arm64-v8a/libOVRPlugin.so", plugin)
    return p


def test_stub_library_can_return_a_failure_code():
    assert build_stub_library(["a"]) == build_stub_library(["a"], result=0)  # default unchanged (byte-stable)
    lib = build_stub_library(["ovrp_GetPTWNear"], "libx.so", result=-1000)
    assert struct.pack("<II", 0x12807CE0, 0xD65F03C0) in lib  # movn w0, #999 (= -1000) ; ret
    assert struct.pack("<II", 0x52800000 | 7 << 5, 0xD65F03C0) in build_stub_library(["f"], result=7)


def test_engine_lookups_are_read_from_the_engine_library():
    assert detect.ovrp_lookups(ENGINE) == LOOKUPS
    assert detect.ovrp_lookups(b"xx\0ovrp_A\0ovrp_B\0ovrp_bad name\0") == ["ovrp_A", "ovrp_B"]


def test_analysis_records_unreal_lookups(tmp_path, quest_manifest):
    a = detect.analyze(_apk(tmp_path, quest_manifest))
    assert a.engine == "Unreal" and a.extra["unreal_ovrp_lookups"] == LOOKUPS
    assert a.extra["analysis_version"] == detect.ANALYSIS_VERSION >= 3


def test_missing_functions_get_failing_stand_ins(tmp_path, quest_manifest):
    patch = base.get("frame.unreal_ovrp_entrypoints")
    a = _analysis(unreal_ovrp_lookups=LOOKUPS)
    with ApkWorkspace(_apk(tmp_path, quest_manifest)) as ws:
        ctx = base.ApkContext(ws, a, {}, Reporter(), {patch.id: {}})
        assert patch.apply(ctx)
        plugin = ws.read_lib("libOVRPlugin.so")
        assert elf.needed(plugin)[0] == unreal_ovrp.STUBS
        stubs = ws.read_lib(unreal_ovrp.STUBS)
        assert elf.dyn_symbols(stubs, True) == {"ovrp_GetPTWNear"}
        assert struct.pack("<II", 0x12807CE0, 0xD65F03C0) in stubs
        assert ws.read_lib("libUE4.so") == ENGINE  # the engine library stays as it is
        assert patch.validate(ctx) == [("OVRPlugin functions Unreal looks up resolvable", True, "")]
        assert not patch.apply(ctx)  # a second run changes nothing
        assert ws.read_lib("libOVRPlugin.so") == plugin


def test_nothing_missing_changes_nothing(tmp_path, quest_manifest):
    full = build_stub_library(LOOKUPS, soname="libOVRPlugin.so")
    patch = base.get("frame.unreal_ovrp_entrypoints")
    with ApkWorkspace(_apk(tmp_path, quest_manifest, full)) as ws:
        # an entry analysed before the field existed: the engine library is read instead
        assert not patch.apply(base.ApkContext(ws, _analysis(), {}, Reporter(), {patch.id: {}}))
        assert not ws.has(ws.lib(unreal_ovrp.STUBS))


def test_suggested_from_the_shipped_plugin():
    patch = base.get("frame.unreal_ovrp_entrypoints")
    a = _analysis(unreal_ovrp_lookups=LOOKUPS)
    assert patch.applies(a)
    # no OVRPort runtime downloaded yet: the bundled list of known gaps
    s = patch.detect(a)
    assert s and s.recommended and "ovrp_GetPTWNear" in s.reason
    assert patch.detect(_analysis(unreal_ovrp_lookups=["ovrp_GetVersion2"])) is None
    # with a runtime: its libOVRPlugin.so decides
    lib = user_data_dir() / "overport-workspace" / "libraries" / "9.9.9-test" / "lib" / "arm64-v8a"
    lib.mkdir(parents=True)
    (lib / "libOVRPlugin.so").write_bytes(build_stub_library(["ovrp_GetPTWNear"], soname="libOVRPlugin.so"))
    s = patch.detect(a)
    assert s and "ovrp_GetVersion2, ovrp_Initialize5" in s.reason and "GetPTWNear" not in s.reason
    assert patch.detect(_analysis(unreal_ovrp_lookups=["ovrp_GetPTWNear"])) is None
    assert not patch.applies(Analysis(**{**a.__dict__, "engine": "Unity"}))
    assert not patch.applies(Analysis(**{**a.__dict__, "libs": ["libUE4.so"]}))
