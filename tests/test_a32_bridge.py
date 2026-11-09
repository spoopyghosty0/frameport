"""32-bit games through the a32 bridge (frame.a32_bridge): APK conversion, recipe, analysis, checks, triage."""
import zipfile

from conftest import build_axml

from frameport.analysis import detect
from frameport.apk import axml
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.patches import base
from frameport.patches.frame import a32_bridge
from frameport.recommend import engine
from frameport.validate.triage import triage

UNITY = b"\x7fELF-unity\0com.oculus.systemactivities\0rest"


def _manifest(target_sdk=21, headtracking=None):
    """headtracking: (version, required) of a uses-feature android.hardware.vr.headtracking, None for none"""
    feature = [] if headtracking is None else [
        ("start", "uses-feature", [("name", "str", "android.hardware.vr.headtracking"),
                                   ("required", "bool", headtracking[1]), ("version", "int", headtracking[0])]),
        ("end", "uses-feature")]
    return build_axml([
        ("start", "manifest", [("package", "str", "com.example.gearvr")]),
        ("start", "uses-sdk", [("minSdkVersion", "int", 19), ("targetSdkVersion", "int", target_sdk)]),
        ("end", "uses-sdk"),
        *feature,
        ("start", "application", [("extractNativeLibs", "bool", False)]),
        ("start", "activity", [("name", "str", "com.unity3d.player.UnityPlayerActivity")]),
        ("start", "intent-filter", []),
        ("start", "action", [("name", "str", "android.intent.action.MAIN")]),
        ("end", "action"),
        ("start", "category", [("name", "str", "android.intent.category.LAUNCHER")]),
        ("end", "category"),
        ("end", "intent-filter"),
        ("end", "activity"),
        ("start", "meta-data", [("name", "str", "com.samsung.android.vr.application.mode"),
                                ("value", "str", "vr_only")]),
        ("end", "meta-data"),
        ("end", "application"),
        ("end", "manifest"),
    ])


def _apk(tmp_path, platform=True, manifest=None):
    p = tmp_path / "gearvr.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", manifest or _manifest())
        z.writestr("classes.dex", b"dex\n035\0")
        z.writestr("META-INF/CERT.RSA", b"old signature")
        z.writestr("lib/armeabi-v7a/libmain.so", b"\x7fELF-main32")
        z.writestr("lib/armeabi-v7a/libunity.so", UNITY)
        z.writestr("lib/armeabi-v7a/libvrapi.so", b"\x7fELF-oculus-vrapi32")
        z.writestr("lib/x86/libmain.so", b"\x7fELF-x86")
        if platform:
            z.writestr("lib/armeabi-v7a/libovrplatformloader.so", b"\x7fELF-meta-loader32")
    return p


def _prebuilt(name):
    return {"liba32stub.so": b"STUB", "liba32bridge.so": b"BRIDGE", "liba32rt.so": b"RUNTIME",
            "libvrapi.so": b"VRAPI64", "libopenxr_loader_generic.so": b"FRAMEBRIDGE"}[name]


def _ovrport(name, abi):
    return f"OVRPORT {abi} {name}".encode()


def _signed_cert_pkcs7():
    """A PKCS#7 signature block (like META-INF/CERT.RSA) with a self-signed certificate; returns (block, cert DER)."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs7
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Game Studio")])
    now = datetime.datetime(2016, 1, 1, tzinfo=datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=9000))
            .sign(key, hashes.SHA256()))
    block = (pkcs7.PKCS7SignatureBuilder().set_data(b"SF").add_signer(cert, key, hashes.SHA256())
             .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature]))
    return block, cert.public_bytes(serialization.Encoding.DER)


def test_original_signature_for_unreal(tmp_path):
    """UE4's AndroidThunkJava_GetSignature = Base64.DEFAULT(SHA-1(signing certificate DER)); a re-signed APK has
    another, so the conversion keeps the original's for the bridge to answer with (Gunjack quits otherwise)."""
    import base64
    import hashlib

    block, der = _signed_cert_pkcs7()
    p = tmp_path / "ue4.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", _manifest())
        z.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n")
        z.writestr("META-INF/CERT.RSA", block)
        z.writestr("lib/armeabi-v7a/libUE4.so", b"\x7fELF-ue4")
    with ApkWorkspace(p) as ws:
        a32_bridge.convert(ws, _prebuilt, _ovrport)
        assert ws.read(a32_bridge.SIGNATURE_ASSET) == base64.b64encode(hashlib.sha1(der).digest()) + b"\n"
    with ApkWorkspace(_apk(tmp_path)) as ws:  # not Unreal (and an unreadable signature block): nothing added
        a32_bridge.convert(ws, _prebuilt, _ovrport)
        assert not ws.has(a32_bridge.SIGNATURE_ASSET)


def test_convert_layout(tmp_path):
    with ApkWorkspace(_apk(tmp_path)) as ws:
        converted = a32_bridge.convert(ws, _prebuilt, _ovrport)
        assert (ws.abi, ws.libdir) == ("arm64-v8a", "lib/arm64-v8a/")  # later patches see the arm64 layout
        out = ws.write(tmp_path / "out.apk")
    assert converted == ["libmain.so", "libovrplatformloader.so", "libunity.so"]  # libvrapi.so: translated instead
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        lib = {n.split("/")[-1]: z.read(n) for n in names if n.startswith("lib/arm64-v8a/")}
        manifest = z.read("AndroidManifest.xml")
    assert not any(n.startswith(("lib/armeabi-v7a/", "lib/x86/")) for n in names)  # Lepton would pick those
    assert lib["libmain.a32.so"] == b"\x7fELF-main32" and lib["libmain.so"] == b"STUB"
    assert b"systemactivities" not in lib["libunity.a32.so"] and b"\0android\0" in lib["libunity.a32.so"]
    assert len(lib["libunity.a32.so"]) == len(UNITY)
    assert lib["libovrplatformloader.a32.so"] == b"OVRPORT armeabi-v7a libovrplatformloader.so"
    assert lib["libovrplatformloader_meta.a32.so"] == b"OVRPORT armeabi-v7a libovrplatformloader_meta.so"
    assert "libovrplatformloader_meta.so" not in lib  # only the guest dispatcher loads it: no stub
    assert "libvrapi.a32.so" not in lib and lib["libvrapi.so"].startswith(b"\x7fELF\x02")  # Java's: an empty arm64 one
    assert lib["libfp_vrapi64.so"] == b"VRAPI64"
    assert lib["libopenxr_loader.so"] == b"OVRPORT arm64-v8a libopenxr_loader.so"
    assert lib["libopenxr_loader_original.so"] == b"OVRPORT arm64-v8a libopenxr_loader_generic.so"
    assert lib["libopenxr_loader_generic.so"] == b"FRAMEBRIDGE"
    assert lib["liba32bridge.so"] == b"BRIDGE" and lib["liba32rt.so"] == b"RUNTIME"
    assert axml.Axml(manifest).get_bool("application", "extractNativeLibs")
    x = axml.Axml(manifest)
    names_ = x.strings()
    sdk = {names_[a.name]: a.value for el in x.elements() if el.name == "uses-sdk" for a in el.attrs}
    assert sdk == {"minSdkVersion": 19, "targetSdkVersion": 23}  # no "older Android" dialog taking the focus


def test_patch_with_adapter(tmp_path, monkeypatch):
    """frame.a32_bridge, then frame.adapter as in a build: the adapter only adds its settings."""
    from frameport.tools import overport

    def fake_runtime(name, abi, reporter):
        f = tmp_path / f"{abi}-{name}"
        f.write_bytes(_ovrport(name, abi))
        return f

    monkeypatch.setattr(overport, "ensure_runtime_lib", fake_runtime)
    a = _analysis()
    recipe = {"frame.a32_bridge": {}, "frame.adapter": {}}
    with ApkWorkspace(_apk(tmp_path, platform=False)) as ws:
        for pid in ("frame.a32_bridge", "frame.adapter"):
            ctx = base.ApkContext(ws, a, {}, Reporter(), recipe)
            base.get(pid).apply(ctx)
            assert all(ok for _, ok, _ in base.get(pid).validate(ctx))
        assert not base.get("frame.a32_bridge").apply(base.ApkContext(ws, a, {}, Reporter(), recipe))  # once
        out = ws.write(tmp_path / "out.apk")
    with zipfile.ZipFile(out) as z:
        assert z.read("lib/arm64-v8a/libframe_settings.so").startswith(b"scale=")
        assert z.read("lib/arm64-v8a/libopenxr_loader_original.so") == _ovrport("libopenxr_loader_generic.so",
                                                                                 "arm64-v8a")


def _analysis(**kw):
    d = dict(package="com.example.gearvr", version="1.0", label="Gear VR Game", abis=["armeabi-v7a", "x86"],
             engine="Unity", xr="VrApi", graphics="GLES or unknown (no Vulkan declaration)", direct_vrapi=False,
             libs=["libmain.so", "libunity.so", "libvrapi.so", "libOVRPlugin.so"], launcher_activity=None,
             has_info_category=False, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
             oculus_os_classes=False, is_overport_output=False, debuggable=False,
             extra={"size": 1, "vr_kind": "quest", "headset": "gearvr", "unity_oculus_check": True})
    d.update(kw)
    return Analysis(**d)


def test_recipe_for_a_32bit_game():
    r = engine.suggest(_analysis(), use_catalog=False)
    assert r.status != "unsupported" and "32-bit bridge" in r.notes
    assert not r.overport and not r.alt_patches  # OVRPort handles 64-bit apps only
    assert {"frame.a32_bridge", "frame.adapter", "device.text_input_window"} <= set(r.patches)
    assert not [p for p in r.patches if base.get(p).category == "overport"]
    # the 64-bit library fixes can't apply to translated code
    assert not {"frame.unity_oculus_check", "frame.ovrstubs", "adapter.haptic_fix"} & set(r.patches)
    shown, _ = engine.visible_patches(_analysis(), r)
    assert not [p for p in shown if p.category == "overport"]


def test_catalog_verdict_from_before_the_bridge(monkeypatch):
    """A catalog entry written before the bridge ("unsupported: no AArch32") no longer overrides it."""
    from frameport.recommend import catalog

    old = catalog.CatalogEntry(package="com.example.gearvr", title="Old", status="unsupported",
                               notes="32-bit only; the Frame has no AArch32.",
                               alt_overport=["patch_remove_unreal_force_quit"])
    monkeypatch.setattr(catalog, "lookup", lambda pkg: old)
    r = engine.suggest(_analysis())
    assert "frame.a32_bridge" in r.patches and r.status == "unknown" and "32-bit bridge" in r.notes
    assert not r.overport and not r.alt_patches
    # an entry that names the bridge (tested with it) keeps its own verdict
    tested = catalog.CatalogEntry(package="com.example.gearvr", title="New", status="unsupported",
                                  frame=["frame.a32_bridge"])
    monkeypatch.setattr(catalog, "lookup", lambda pkg: tested)
    r = engine.suggest(_analysis())
    assert "frame.a32_bridge" in r.patches and r.status == "unsupported"


def test_bridge_turned_on_by_hand(tmp_path, monkeypatch):
    """Turning the bridge on in a saved recipe switches OVRPort and the 64-bit-only fixes off."""
    from frameport import pipeline
    from frameport.core import library
    from frameport.core.models import Recipe

    monkeypatch.setattr(library, "_path", lambda: tmp_path / "library.json")
    library.upsert_game("com.example.gearvr", title="X", analysis=_analysis().to_dict())
    r = Recipe(package="com.example.gearvr", source="user", alt_patches=["patch_remove_unreal_force_quit"],
               patches={"patch_copy_libraries": {}, "frame.vk_sanitize": {}, "frame.adapter": {},
                        "adapter.haptic_fix": {"value": 1}, "adapter.scale": {"value": 0.9}})
    pipeline.set_recipe("com.example.gearvr", r)
    assert r.overport  # without the bridge nothing changes
    r.patches["frame.a32_bridge"] = {}
    pipeline.set_recipe("com.example.gearvr", r)
    saved = library.recipe_from_dict(library.game("com.example.gearvr")["recipe"])
    assert not saved.overport and not saved.alt_patches
    assert set(saved.patches) == {"frame.a32_bridge", "frame.adapter", "adapter.scale", "device.text_input_window"}
    assert set(r.patches) == set(saved.patches)  # changed in place: the game page redraws from it
    # turned off by the user afterwards: stays off; hand-added 64-bit fixes are still dropped
    r.patches.pop("device.text_input_window")
    r.patches["frame.vk_sanitize"] = {}
    pipeline.set_recipe("com.example.gearvr", r)
    assert set(library.game("com.example.gearvr")["recipe"]["patches"]) == {"frame.a32_bridge", "frame.adapter",
                                                                             "adapter.scale"}


def test_bridge_by_hand_lifts_the_old_verdict(tmp_path, monkeypatch):
    """A saved "unsupported" (no AArch32, from before the bridge) no longer blocks installing once it's turned on."""
    from frameport import pipeline
    from frameport.core import library
    from frameport.core.models import Recipe
    from frameport.recommend import catalog

    monkeypatch.setattr(library, "_path", lambda: tmp_path / "library.json")
    library.upsert_game("com.example.gearvr", title="X", analysis=_analysis().to_dict())
    old = catalog.CatalogEntry(package="com.example.gearvr", title="Old", status="unsupported")
    monkeypatch.setattr(catalog, "lookup", lambda pkg: old)
    r = Recipe(package="com.example.gearvr", source="user", status="unsupported", notes="no AArch32",
               patches={"frame.a32_bridge": {}})
    pipeline.set_recipe("com.example.gearvr", r)
    assert r.status == "unknown" and "32-bit bridge" in r.notes
    tested = catalog.CatalogEntry(package="com.example.gearvr", title="New", status="unsupported",
                                  frame=["frame.a32_bridge"])
    monkeypatch.setattr(catalog, "lookup", lambda pkg: tested)
    r = Recipe(package="com.example.gearvr", source="user", status="unsupported", patches={"frame.a32_bridge": {}})
    library.upsert_game("com.example.gearvr", recipe={"package": "com.example.gearvr", "patches": {}})
    pipeline.set_recipe("com.example.gearvr", r)
    assert r.status == "unsupported"  # tested with the bridge: the verdict stands


def test_64bit_games_keep_their_recipe():
    a = _analysis(abis=["arm64-v8a"], extra={"size": 1, "vr_kind": "quest", "headset": "quest"})
    r = engine.suggest(a, use_catalog=False)
    assert r.overport and "frame.a32_bridge" not in r.patches


def test_headset():
    gear = {"com.samsung.android.vr.application.mode": "vr_only"}
    assert detect.headset(gear, ["armeabi-v7a"], "quest") == "gearvr"
    assert detect.headset({"com.oculus.supportedDevices": "go"}, ["armeabi-v7a"], "quest") == "go"
    assert detect.headset({"com.oculus.supportedDevices": "quest|quest2"}, ["armeabi-v7a"], "quest") == "quest"
    assert detect.headset(gear, ["arm64-v8a", "armeabi-v7a"], "quest") == "quest"  # a Quest build kept the entry
    assert detect.headset({}, ["armeabi-v7a"], "none") == ""
    # a 32-bit Santa Cruz build (Sports Scramble) kept Samsung's VR mode but requires 6DoF head tracking
    assert detect.headset(gear, ["armeabi-v7a"], "quest", six_dof=True) == "quest"


def test_requires_6dof():
    assert detect.requires_6dof(_manifest(headtracking=(1, True)))
    assert not detect.requires_6dof(_manifest(headtracking=(1, False)))  # optional: a Go app may run on a Quest too
    assert not detect.requires_6dof(_manifest(headtracking=(0, True)))  # version 0 = 3DoF
    assert not detect.requires_6dof(_manifest())


def test_quest_build_gets_touch_controllers(tmp_path):
    """The bridge passes the Frame's controllers through as Touch controllers for Quest builds (fp_a32_headset.txt),
    and keeps the Gear VR remote emulation for the others."""
    with ApkWorkspace(_apk(tmp_path, manifest=_manifest(headtracking=(1, True)))) as ws:
        a32_bridge.convert(ws, _prebuilt, _ovrport)
        assert ws.read(a32_bridge.HEADSET_ASSET) == b"quest"
    with ApkWorkspace(_apk(tmp_path)) as ws:
        a32_bridge.convert(ws, _prebuilt, _ovrport)
        assert not ws.has(a32_bridge.HEADSET_ASSET)


def test_static_checks_skip_guest_libraries(tmp_path, monkeypatch):
    from frameport.analysis import elf
    from frameport.analysis.stubgen import build_stub_library
    from frameport.apk import sign
    from frameport.validate.static import check_apk

    monkeypatch.setattr(sign, "verify", lambda apk: (True, ""))
    monkeypatch.setattr(sign, "alignment_problems", lambda apk: [])

    apk = tmp_path / "bridged.apk"
    guest = elf.add_needed(build_stub_library(["main"], soname="libmain.so"), "libvrapi.so")  # the guest runtime's
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", _manifest(23))
        z.writestr("lib/arm64-v8a/libmain.a32.so", guest)  # (not really 32-bit: only its file name matters)
        z.writestr("lib/arm64-v8a/libmain.so", build_stub_library(["stub"], soname="libmain.so"))
    checks = {c["name"]: c for c in check_apk(apk, expect_adapter=False)}
    assert checks["Library dependencies resolvable"]["ok"] is True


def test_triage_doesnt_suggest_the_64bit_vrapi_bridge():
    log = """
10-10 15:59:39.927  1300  1324 I a32bridge: vrapi_Initialize(app VrApi 1.31.0, graphics 0x10200) -> 0
10-10 15:59:42.000  1300  1324 I FrameBridge: pacing: 72.2 fps, displayTime vs predicted: avg 0.00 ms
"""
    r = triage(log, "RUNNING", "com.example.gearvr")
    assert "direct-vrapi" not in {f.id for f in r.findings}
    r = triage("I Unity: vrapi_Initialize\n", "RUNNING", "com.example.game")
    assert "direct-vrapi" in {f.id for f in r.findings}


def test_which_patches_work_on_the_bridge():
    """Declared per patch (base.works_on_a32): OVRPort and the 64-bit Frame fixes don't, FrameBridge settings and
    device options do unless they need the xrshim (settings.XRSHIM_SETTINGS, the same list frame.adapter uses)."""
    from frameport.patches.settings import XRSHIM_SETTINGS

    ok = {p.id for p in base.all_patches() if base.works_on_a32(p)}
    assert {p for p in ok if base.get(p).category in ("overport", "frame")} == {
        "frame.a32_bridge", "frame.adapter", "frame.launcher", "frame.main_process",
        "frame.browser_handoff", "frame.stable_android_id"}  # manifest edits / Java / strings: any ABI
    assert "adapter.scale" in ok and "device.text_input_window" in ok
    assert not {f"adapter.{k}" for k in XRSHIM_SETTINGS} & ok
    a = _analysis()
    assert not base.get("adapter.haptic_fix").applies(a)  # hidden in the Game settings dialog too
    assert base.get("adapter.haptic_fix").applies(_analysis(abis=["arm64-v8a"]))


def test_build_refuses_a_32bit_recipe_that_cant_work():
    import pytest

    from frameport import build
    from frameport.core.models import Recipe

    a = _analysis()
    with pytest.raises(ValueError, match="only 32-bit ARM code"):
        build.check_a32(a, Recipe(package=a.package, patches={"frame.adapter": {}}))
    with pytest.raises(ValueError, match="OVRPort"):
        build.check_a32(a, Recipe(package=a.package, patches={"frame.a32_bridge": {}}))
    with pytest.raises(ValueError, match=base.get("frame.vk_sanitize").title):
        build.check_a32(a, Recipe(package=a.package, overport=False,
                                  patches={"frame.a32_bridge": {}, "frame.vk_sanitize": {}}))
    build.check_a32(a, engine.suggest(a, use_catalog=False))  # the suggested recipe builds
    build.check_a32(_analysis(abis=["arm64-v8a"]), Recipe(package=a.package))  # 64-bit games: no check


def test_warnings_for_32bit_games():
    from frameport.core.models import Recipe

    a = _analysis()
    assert "only 32-bit ARM code" in engine.warnings(Recipe(package=a.package), a)[0]
    assert engine.warnings(engine.suggest(a, use_catalog=False), a) == []
    r = engine.suggest(a, use_catalog=False)
    r.patches["frame.vk_sanitize"] = {}
    assert engine.warnings(r, a) == [f"{base.get('frame.vk_sanitize').title} doesn't work with the 32-bit bridge."]


def test_parity_has_no_baseline_for_bridge_builds(tmp_path):
    from frameport import parity

    old = tmp_path / "old.apk"
    with zipfile.ZipFile(old, "w") as z:
        z.writestr("lib/armeabi-v7a/libmain.so", b"x")
    new = tmp_path / "new.apk"
    with zipfile.ZipFile(new, "w") as z:
        z.writestr(f"lib/arm64-v8a/{a32_bridge.BRIDGE}", b"x")
    assert not parity._bridge_baseline(old) and parity._bridge_baseline(new)
    assert not parity._bridge_baseline(tmp_path / "missing.apk")
    rows = [{"game": "A", "primary": {"verdict": "identical", "diffs": []}},
            {"game": "B", "primary": {"verdict": parity.NO_BASELINE, "diffs": []}}]
    parity.write_report(rows, tmp_path / "r.md")
    text = (tmp_path / "r.md").read_text()
    assert "**1/1 games at parity**" in text and "1 32-bit bridge build(s) without a baseline" in text


def test_game_page_bridge_switch(tmp_path, monkeypatch):
    """The game page's switch (no browser): turning the bridge on saves a bridge recipe and redraws the page, since
    other patches changed with it."""
    from types import SimpleNamespace

    import flet as ft

    from frameport.core import library
    from frameport.core.models import Recipe
    from frameport.ui import components as C
    from frameport.ui.views.game import GameView

    monkeypatch.setattr(library, "_path", lambda: tmp_path / "library.json")
    # a library that ran the a32_bridge_on migration, and the user switched the bridge off afterwards
    (tmp_path / "library.json").write_text('{"games": {}, "settings": {"migrations": ["a32_bridge_on"]}}')
    a = _analysis()
    r = Recipe(package=a.package, source="user", status="unsupported", notes="32-bit only; no AArch32.",
               patches={"patch_copy_libraries": {}, "frame.adapter": {}, "frame.vk_sanitize": {}})
    library.upsert_game(a.package, title="X", analysis=a.to_dict(), recipe=library.recipe_to_dict(r))
    redraws = []
    app = SimpleNamespace(has_game_settings=lambda g: True, settings_dialog=print,
                          open_game=lambda *args, **kw: redraws.append(kw))
    panel = GameView(app, a.package, advanced=True, show_all=True).advanced_panel()

    def walk(c):
        yield c
        for attr in ("controls", "content", "actions"):
            v = getattr(c, attr, None)
            for x in (v if isinstance(v, list) else [v] if v is not None and not isinstance(v, str) else []):
                yield from walk(x)

    def switch_for(title):
        """The real ft.Switch in the patch row whose title is `title` (C.switch wraps it in a painted track)."""
        shown = C.patch_title(title, False)
        for c in walk(panel):
            ctrls = getattr(c, "controls", None)
            if isinstance(ctrls, list) and len(ctrls) > 1 \
                    and any(getattr(x, "value", None) == shown for x in walk(ctrls[0])):
                sw = next((x for x in walk(ctrls[-1]) if isinstance(x, ft.Switch)), None)
                if sw is not None:
                    return sw
        raise AssertionError(f"no switch for {title}")

    # the panel warns before anything is changed
    assert any("only 32-bit ARM code" in str(getattr(c, "value", "")) for c in walk(panel))
    sw = switch_for(base.get("frame.a32_bridge").title)
    assert sw.value is False
    sw.value = True
    sw.on_change(SimpleNamespace(control=sw))
    saved = library.recipe_from_dict(library.game(a.package)["recipe"])
    assert set(saved.patches) == {"frame.a32_bridge", "frame.adapter", "device.text_input_window"}
    assert not saved.overport and saved.status == "unknown" and "32-bit bridge" in saved.notes
    assert redraws == [{"advanced": True, "show_all": True}]  # the switches now show what changed


def test_migration_turns_the_bridge_on_once(tmp_path, monkeypatch):
    """Recipes from before the bridge (also the user's own) get it once at load; switched off later, it stays off."""
    import json

    from frameport import pipeline
    from frameport.core import library
    from frameport.recommend import catalog

    monkeypatch.setattr(library, "_path", lambda: tmp_path / "library.json")
    removed = catalog.CatalogEntry(package="com.x.removed", title="R", frame_remove=["frame.a32_bridge"])
    monkeypatch.setattr(catalog, "lookup", lambda pkg: removed if pkg == "com.x.removed" else None)
    old = {"patches": {"patch_copy_libraries": {}, "frame.adapter": {}, "frame.vk_sanitize": {}},
           "source": "user", "status": "unsupported", "notes": "32-bit only; no AArch32.", "overport": True}

    def game(pkg, **kw):
        return {"package": pkg, "analysis": _analysis(package=pkg, **kw).to_dict(), "recipe": {"package": pkg, **old}}
    games = {"com.x.old": game("com.x.old"), "com.x.removed": game("com.x.removed"),
             "com.x.arm64": game("com.x.arm64", abis=["arm64-v8a"])}
    (tmp_path / "library.json").write_text(json.dumps({"games": games, "settings": {}}))

    r = library.recipe_from_dict(library.game("com.x.old")["recipe"])
    assert {"frame.a32_bridge", "frame.adapter", "device.text_input_window"} <= set(r.patches)
    assert not r.overport and not {"patch_copy_libraries", "frame.vk_sanitize"} & set(r.patches)
    assert r.status == "unknown" and r.source == "user"
    assert "frame.a32_bridge" not in library.game("com.x.removed")["recipe"]["patches"]  # the catalog says no
    assert library.game("com.x.arm64")["recipe"]["overport"]  # 64-bit games: untouched
    assert "a32_bridge_on" in library.setting("migrations")
    r.patches.pop("frame.a32_bridge")  # the user switches it off: not turned on again
    pipeline.set_recipe("com.x.old", r)
    assert "frame.a32_bridge" not in library.game("com.x.old")["recipe"]["patches"]


def test_original_signature_takes_the_signer_not_its_ca(tmp_path):
    """A signature block can carry the CA chain in any order: the hash is of the signer's own certificate."""
    import base64
    import datetime
    import hashlib

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs7
    from cryptography.x509.oid import NameOID

    now = datetime.datetime(2016, 1, 1, tzinfo=datetime.UTC)
    later = now + datetime.timedelta(days=9000)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Studio CA")])
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Game Studio")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now).not_valid_after(later).sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(name).issuer_name(ca_name).public_key(key.public_key())
            .serial_number(2).not_valid_before(now).not_valid_after(later).sign(ca_key, hashes.SHA256()))
    block = pkcs7.serialize_certificates([ca, leaf], serialization.Encoding.DER)  # the CA first
    p = tmp_path / "ue4.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", _manifest())
        z.writestr("META-INF/CERT.RSA", block)
    with ApkWorkspace(p) as ws:
        expected = base64.b64encode(hashlib.sha1(leaf.public_bytes(serialization.Encoding.DER)).digest()) + b"\n"
        assert a32_bridge.original_signature(ws) == expected


def test_flat_32bit_app_keeps_the_bridge():
    """A 32-bit Android app without VR still needs its native code translated (the flat-app rules keep only patches
    that aren't about VR)."""
    r = engine.suggest(_analysis(extra={"size": 1, "vr_kind": "none"}), use_catalog=False)
    assert "frame.a32_bridge" in r.patches and not r.overport and not r.as_is


def test_frida_gadget_load_is_removed():
    """A repack's Frida gadget (System.loadLibrary("frda")) is removed as OVRPort's patch_clean_up_frida does for
    64-bit games; the game's own loadLibrary calls stay."""
    import struct

    from test_vivox_audio_route import build_dex

    from frameport.apk.dex import Dex

    code = [0x001A, 0, 0x1071, 1, 0,  # const-string v0, "frda"; invoke-static {v0}, System.loadLibrary
            0x001A, 1, 0x1071, 1, 0,  # const-string v0, "main"; invoke-static {v0}, System.loadLibrary
            0x000E]
    data = build_dex([("Lcom/game/App;", "onCreate", "V", code), ("Ljava/lang/System;", "loadLibrary", "V", None)],
                     strings_first=("frda", "main"))
    out = a32_bridge.strip_frida_load(data)
    dex = Dex(out)
    (_idx, off), = dex.class_methods("Lcom/game/App;")
    units = list(struct.unpack_from("<11H", dex.data, off + 16))
    assert units == [0x001A, 0, 0, 0, 0, 0x001A, 1, 0x1071, 1, 0, 0x000E]
    assert a32_bridge.strip_frida_load(out) is None and a32_bridge.strip_frida_load(b"dex\n" + bytes(108)) is None


def test_jit_stub_overflow_is_triaged_and_rebuilds_pinball():
    """GitHub #342: OpenSSL's ARMv8 SHA-1 rounds overflowed the translator's stub table (fatal, exit 127) in Pinball
    FX2 VR. The log line is recognised, and the fixed bridge marks that game "Update on Frame" (only that one)."""
    line = "10-16 14:09:04.833  1170  1195 W a32     : a32: block at 0xf68587a0 overflowed the code buffer\n"
    hit = next(f for f in triage(line, "EXITED").findings if f.id == "a32-jit-overflow")
    assert "frame.a32_bridge" in hit.suggest
    patch = a32_bridge.A32Bridge()
    assert patch.revision_for("com.zenstudios.PinballFX2VRQuest") > patch.revision
    assert patch.revision_for("com.fallen.affected") == patch.revision


def test_jit_ends_blocks_before_the_stub_table_is_full():
    """The block loop in jit.c also ends a block when the exit-stub table is nearly full (it used to check only the
    instruction count and the code size); tests/programs/sha1ce.c is the run-time check (run_programs.py)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "native" / "a32translator"
    assert "x->nstubs > MAX_STUBS - STUB_RESERVE" in (root / "src" / "jit.c").read_text()
    assert '"sha1ce": ("sha1ce.c"' in (root / "tests" / "run_programs.py").read_text()


def test_audio_pacing(tmp_path):
    """GitHub #439: the OpenSL ES player reads the guest's buffers on the clock (host/audio_pacing.h), one per buffer
    duration, instead of in pairs at AudioFlinger's 20 ms bursts (Ghost Giant's FMOD then mixed discontinuous
    blocks: distorted sound). The fixture simulates the Frame's device: steady state, clock drift both ways, a guest
    stall, an underrun, a late device start and buffers larger than the stream."""
    import os
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc or os.name == "nt":
        pytest.skip("needs a POSIX host C compiler")
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "a32-audio-pacing-test"
    subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-I", str(root / "native/a32translator/bridge/host"),
                    str(root / "tests/fixtures/src/a32_audio_pacing_test.c"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=60)
    host = (root / "native/a32translator/bridge/host/android.c").read_text()
    assert "a32_pace_before(" in host and 'getenv("A32_AUDIO_PACING")' in host
