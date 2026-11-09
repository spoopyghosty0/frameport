"""Google Daydream / Cardboard (GVR SDK) apps: detected, warned about, never marked unsupported (GitHub #335)."""
import zipfile

from conftest import build_axml

from frameport.analysis import detect
from frameport.recommend import engine
from frameport.validate.triage import triage

DAYDREAM_VR_MODE = "com.google.vr.vrcore/com.google.vr.vrcore.common.VrCoreListenerService"


def _manifest(package="com.example.daydream", categories=(), enable_vr_mode=None):
    activity = [("name", "str", "com.unity3d.player.UnityPlayerActivity")]
    if enable_vr_mode:
        activity.append(("enableVrMode", "str", enable_vr_mode))
    cats = []
    for c in ("android.intent.category.LAUNCHER", *categories):
        cats += [("start", "category", [("name", "str", c)]), ("end", "category")]
    return build_axml([
        ("start", "manifest", [("package", "str", package)]),
        ("start", "uses-sdk", [("minSdkVersion", "int", 24), ("targetSdkVersion", "int", 26)]),
        ("end", "uses-sdk"),
        ("start", "application", []),
        ("start", "activity", activity),
        ("start", "intent-filter", []),
        ("start", "action", [("name", "str", "android.intent.action.MAIN")]),
        ("end", "action"),
        *cats,
        ("end", "intent-filter"),
        ("end", "activity"),
        ("end", "application"),
        ("end", "manifest"),
    ])


def _apk(tmp_path, manifest, libs=("libmain.so", "libunity.so"), abi="armeabi-v7a", dex=b"dex\n035\0"):
    p = tmp_path / "game.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
        z.writestr("classes.dex", dex)
        for lib in libs:
            z.writestr(f"lib/{abi}/{lib}", b"\x7fELF-" + lib.encode())
    return p


def test_google_vr_signals():
    assert detect.google_vr(set(), {detect.DAYDREAM_CATEGORY}, []) == "daydream"
    assert detect.google_vr(set(), set(), [DAYDREAM_VR_MODE]) == "daydream"
    assert detect.google_vr(set(), {detect.CARDBOARD_CATEGORY}, []) == "cardboard"
    assert detect.google_vr({"libgvr.so"}, set(), []) == "cardboard"
    assert detect.google_vr({"libGfxPluginCardboard.so"}, set(), []) == "cardboard"
    assert detect.google_vr(set(), set(), [], gvr_java=True) == "cardboard"
    assert detect.google_vr(set(), set(), [], boot="vr-enabled=1\nvr-device-list=Daydream,Cardboard\n") == "daydream"
    # both: Daydream is the more specific
    assert detect.google_vr({"libgvr.so"}, {detect.CARDBOARD_CATEGORY, detect.DAYDREAM_CATEGORY}, []) == "daydream"
    # Google's spatial audio alone isn't Google VR (Quest games ship it, e.g. Shadow Point)
    assert detect.google_vr({"libgvraudio.so", "libaudioplugingvrunity.so"}, set(), []) == ""
    assert detect.google_vr(set(), set(), [], boot="vr-enabled=1\nvr-device-list=Oculus\n") == ""


def test_daydream_apk_is_detected(tmp_path):
    """Blade Runner: Revelations' shape: Unity 2017, 32-bit, Daydream category + VrCore's VR mode, libgvr.so."""
    m = _manifest(categories=[detect.DAYDREAM_CATEGORY], enable_vr_mode=DAYDREAM_VR_MODE)
    a = detect.analyze(_apk(tmp_path, m, ("libmain.so", "libunity.so", "libgvr.so", "libaudioplugingvrunity.so")))
    assert a.extra["google_vr"] == "daydream"
    assert a.vr_kind == "none" and a.extra["analysis_version"] == detect.ANALYSIS_VERSION


def test_cardboard_from_java_classes(tmp_path):
    dex = b"dex\n035\0...Lcom/google/vr/sdk/base/GvrView;..."
    a = detect.analyze(_apk(tmp_path, _manifest(), dex=dex))
    assert a.extra["google_vr"] == "cardboard"


def test_quest_game_with_google_audio_is_not_google_vr(tmp_path):
    a = detect.analyze(_apk(tmp_path, _manifest(), ("libmain.so", "libunity.so", "libvrapi.so", "libgvraudio.so")))
    assert a.extra["google_vr"] == "" and engine.google_vr_note(a) is None


def test_warning_but_never_unsupported(tmp_path):
    m = _manifest(categories=[detect.DAYDREAM_CATEGORY], enable_vr_mode=DAYDREAM_VR_MODE)
    a = detect.analyze(_apk(tmp_path, m, ("libmain.so", "libunity.so", "libgvr.so")))
    note = engine.google_vr_note(a)
    assert note and "Daydream" in note and "Oculus build" in note
    assert note in engine.blocker_notes(a)
    r = engine.suggest(a, use_catalog=False)
    assert r.status != "unsupported"  # the manifest alone never decides; the launch test's triage does
    assert note in r.notes and "Android app without VR" not in r.notes


def test_daydream_and_oculus_build_goes_the_normal_way(tmp_path):
    """A Unity build with both Daydream and Oculus support: the Oculus path works, so no warning."""
    m = _manifest(categories=[detect.DAYDREAM_CATEGORY], enable_vr_mode=DAYDREAM_VR_MODE)
    a = detect.analyze(_apk(tmp_path, m, ("libmain.so", "libunity.so", "libgvr.so", "libvrapi.so")))
    assert a.extra["google_vr"] == "daydream" and a.vr_kind == "quest"
    assert engine.google_vr_note(a) is None


def test_triage_gvr_not_supported():
    """Lines from #335's launch log (Blade Runner: Revelations on the 32-bit bridge)."""
    lines = [
        "10-16 05:27:34.526  1355  1384 W a32bridge: libgvr.so: Java_com_google_vr_sdk_base_CardboardViewNativeImpl_"
        "nativeDestroy: class com/google/vr/sdk/base/CardboardViewNativeImpl not found",
        "10-16 05:27:34.560  1355  1384 E Unity   : Google VR Error [Daydream]: Google VR had a fatal issue while "
        "loading. VR will not be available.",
        "10-16 05:27:43.854   225  1097 W ActivityManager: Unable to start service Intent { "
        "act=com.google.vr.vrcore.controller.BIND pkg=com.google.vr.vrcore } U=0: not found",
        # GitHub #507 (YouTube VR's Daydream build): GVR fell back to its Cardboard phone-viewer HMD
        "10-26 11:05:03.387  1170  1299 I GVR     : [vr/gvr/hmd/cardboard/cardboard_api_impl.cc:481] Creating swap "
        "chain with id: [1]",
    ]
    for line in lines:
        r = triage(line + "\n", "RUNNING")
        assert [f.id for f in r.findings] == ["gvr-not-supported"] and r.verdict == "fail", line
    # GVR's own start-up line alone isn't a failure (a Daydream build with Oculus support logs it too)
    assert not triage("10-16 05:27:34.555  1355  1355 I GVR     : [vr/gvr/capi/src/gvr.cc:103] Initialized GVR "
                      "version 1.101.0\n", "RUNNING").findings
