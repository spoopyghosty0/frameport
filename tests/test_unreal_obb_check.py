"""frame.unreal_skip_obb_check (GitHub #159, Contractors): Unreal's DownloaderActivity CRC-checks the OBB when the
manifest says bVerifyOBBOnStartUp=true; its screens are invisible in Lepton, so the game seems to hang."""
from __future__ import annotations

import zipfile

from conftest import build_axml

from frameport.analysis.detect import analyze, unreal_verify_obb
from frameport.apk import axml
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.patches import base
from frameport.validate.triage import triage

PKG = "com.CaveManStudio.ContractorsVR"
VERIFY = "com.epicgames.ue4.GameActivity.bVerifyOBBOnStartUp"


def _manifest(verify=True, kind="bool"):
    return build_axml([
        ("start", "manifest", [("package", "str", PKG)]),
        ("start", "application", []),
        ("start", "meta-data", [("name", "str", "com.epicgames.ue4.GameActivity.bHasOBBFiles"),
                                ("value", "bool", True)]),
        ("end", "meta-data"),
        ("start", "meta-data", [("name", "str", VERIFY), ("value", kind, verify if kind == "bool" else str(verify))]),
        ("end", "meta-data"),
        ("start", "meta-data", [("name", "str", "com.epicgames.ue4.GameActivity.bShouldHideUI"),
                                ("value", "bool", True)]),
        ("end", "meta-data"),
        ("end", "application"),
        ("end", "manifest"),
    ])


def _apk(tmp_path, manifest):
    p = tmp_path / "game.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
        z.writestr("classes.dex", b"dex\n035\0")
    return p


def _analysis(**extra):
    return Analysis(package=PKG, version="3.9", label="Contractors", abis=["arm64-v8a"], engine="Unreal", xr="OpenXR",
                    graphics="GLES", direct_vrapi=False, libs=["libUE4.so", "libOVRPlugin.so"],
                    launcher_activity=None, has_info_category=False, meta_permissions=[], uses_glad_gl=False,
                    unity_msaa_levels=0, oculus_os_classes=False, is_overport_output=False, debuggable=False,
                    extra={"size": 1, **extra})


def test_set_meta_data_bool_changes_only_that_value():
    m = _manifest()
    out = axml.set_meta_data_bool(m, ".GameActivity.bVerifyOBBOnStartUp", False)
    assert out is not None and len(out) == len(m)
    assert sum(a != b for a, b in zip(m, out, strict=True)) == 4  # one 32-bit boolean
    meta = axml.meta_data(out)
    assert meta[VERIFY] is False
    assert meta["com.epicgames.ue4.GameActivity.bHasOBBFiles"] is True  # the others stay
    assert meta["com.epicgames.ue4.GameActivity.bShouldHideUI"] is True
    assert axml.set_meta_data_bool(out, ".GameActivity.bVerifyOBBOnStartUp", False) is None  # already off
    assert axml.set_meta_data_bool(out, ".GameActivity.bVerifyOBBOnStartUp", True) == m  # round trip


def test_set_meta_data_bool_leaves_string_values():
    """Bundle.getBoolean reads a string "true" as false: nothing to change."""
    assert axml.set_meta_data_bool(_manifest(kind="str"), ".GameActivity.bVerifyOBBOnStartUp", False) is None


def test_analysis_flag(tmp_path):
    assert unreal_verify_obb({VERIFY: True})
    assert unreal_verify_obb({"com.epicgames.unreal.GameActivity.bVerifyOBBOnStartUp": True})  # UE5's name
    assert not unreal_verify_obb({VERIFY: False})
    assert not unreal_verify_obb({VERIFY: "true"})  # not a boolean: Unreal reads false
    assert not unreal_verify_obb({"com.epicgames.ue4.GameActivity.bHasOBBFiles": True})
    assert analyze(_apk(tmp_path, _manifest())).extra["unreal_verify_obb"] is True
    other = tmp_path / "off"
    other.mkdir()
    assert analyze(_apk(other, _manifest(verify=False))).extra["unreal_verify_obb"] is False


def test_patch_detects_applies_and_validates(tmp_path):
    base.load_all()
    patch = base.get("frame.unreal_skip_obb_check")
    assert patch.detect(_analysis(unreal_verify_obb=True)).recommended
    assert patch.detect(_analysis()) is None and not patch.applies(_analysis())
    with ApkWorkspace(_apk(tmp_path, _manifest())) as ws:
        ctx = base.ApkContext(ws, _analysis(unreal_verify_obb=True), {}, Reporter(), {patch.id: {}})
        assert patch.validate(ctx)[0][1] is False
        assert patch.apply(ctx)
        assert patch.validate(ctx) == [("OBB check at start off", True, "bVerifyOBBOnStartUp=false")]
        assert axml.meta_data(ws.read("AndroidManifest.xml"))[VERIFY] is False
        assert not patch.apply(ctx)  # nothing left to change


# the launch log of GitHub #159 (Lepton's logcat mirror; system_server lines have another pid than the game)
STUCK = f"""Boot complete!
10-09 18:02:11.120  1201  1230 I ActivityManager: Start proc 2345:{PKG}/u0a120 for pre-top-activity
10-09 18:02:12.004  2345  2345 D UE4     : [GameActivity] Target SDK is 32.  This may cause issues if below 27.
10-09 18:02:12.410  1201  1802 I ActivityTaskManager: START u0 {{flg=0x10000 cmp={PKG}/.DownloaderActivity}}
10-09 18:02:12.900  1201  1240 I ActivityTaskManager: Displayed {PKG}/.DownloaderActivity: +394ms
"""


def test_triage_flags_the_stuck_downloader():
    res = triage(STUCK, "RUNNING", PKG)
    f = next(f for f in res.findings if f.id == "unreal-obb-check-stuck")
    assert f.severity == "fatal" and f.suggest == ["frame.unreal_skip_obb_check"]
    assert "DownloaderActivity" in f.evidence
    assert res.verdict == "fail"  # used to pass with only "Lepton container started"


def test_triage_ignores_a_downloader_that_returned():
    later = STUCK + ("10-09 18:02:14.000  2345  2380 I FrameBridge: xrCreateInstance result=0\n"
                     "10-09 18:02:20.000  2345  2380 I FrameBridge: pacing: 72.0 fps\n")
    assert not any(f.id == "unreal-obb-check-stuck" for f in triage(later, "RUNNING", PKG).findings)
    crashed = STUCK + "10-09 18:02:14.000  2345  2380 F libc    : Fatal signal 11 (SIGSEGV), code 1\n"
    ids = [f.id for f in triage(crashed, "EXITED", PKG).findings]
    assert "unreal-obb-check-stuck" not in ids and "native-crash" in ids


def test_triage_keeps_only_the_games_own_activity_lines():
    other = STUCK.replace(f"{PKG}/.DownloaderActivity", "com.other.app/.DownloaderActivity")
    assert not any(f.id == "unreal-obb-check-stuck" for f in triage(other, "RUNNING", PKG).findings)
