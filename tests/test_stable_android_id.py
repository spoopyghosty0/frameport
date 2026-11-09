"""frame.stable_android_id (GitHub #420, RUNNER): Lepton makes up a new app-specific ANDROID_ID at every start; the
game reads Settings.Secure through a generated forwarding class that answers android_id with a fixed id."""
from __future__ import annotations

import hashlib
import struct
import zipfile
import zlib
from unittest import mock

import pytest
from test_vivox_audio_route import build_dex

from frameport.apk import dexgen
from frameport.apk.dex import WIDTHS, Dex
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.patches import base
from frameport.patches.frame import stable_android_id as sa
from frameport.validate.triage import triage

GET_STRING_DESC = "(Landroid/content/ContentResolver;Ljava/lang/String;)Ljava/lang/String;"


def _analysis(libs=("libunity.so",)) -> Analysis:
    return Analysis(package="com.example.runner", version="1", label="Runner", abis=["arm64-v8a"], engine="Unity",
                    xr="OpenXR", graphics="GLES", direct_vrapi=False, libs=list(libs), launcher_activity=None,
                    has_info_category=False, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
                    oculus_os_classes=False, is_overport_output=False, debuggable=False)


def _class(dex: Dex, idx: int) -> dict:
    """class_def `idx`: name, static field names, {method name+descriptor: (registers, ins, outs, tries, insns)}."""
    base_off = dex.classes_off + 32 * idx
    cls, _acc, _sup, _if, _src, _ann, data_off, _sv = struct.unpack_from("<8I", dex.data, base_off)
    pos = data_off

    def uleb():
        nonlocal pos
        value = shift = 0
        while True:
            b = dex.data[pos]
            pos += 1
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value
            shift += 7

    sf, inf, dm, vm = uleb(), uleb(), uleb(), uleb()
    fields, f = [], 0
    for _ in range(sf):
        f += uleb()
        uleb()
        fields.append(dex.field(f)[2])
    methods, m = {}, 0
    for _ in range(dm):
        m += uleb()
        uleb()
        code = uleb()
        regs, ins, outs, tries, _dbg, n = struct.unpack_from("<4HII", dex.data, code)
        methods[dex.method(m)[1] + dex.descriptor(m)] = (regs, ins, outs, tries,
                                                         list(struct.unpack_from(f"<{n}H", dex.data, code + 16)))
    return {"name": dex.type_name(cls), "fields": fields, "methods": methods, "virtual": vm, "instance": inf}


def test_stable_id_per_game_and_keystore():
    a = sa.stable_id("com.truantpixel.Runner", b"keystore")
    assert len(a) == 16 and int(a, 16) >= 0
    assert a == sa.stable_id("com.truantpixel.Runner", b"keystore")
    assert a != sa.stable_id("com.example.other", b"keystore")
    assert a != sa.stable_id("com.truantpixel.Runner", b"another user's keystore")


def test_helper_dex_is_a_sorted_valid_dex():
    raw = sa.helper_dex("0123456789abcdef")
    dex = Dex(raw)
    assert struct.unpack_from("<I", raw, 32)[0] == len(raw)  # file_size
    assert struct.unpack_from("<I", raw, 8)[0] == zlib.adler32(raw[12:])
    assert raw[12:32] == hashlib.sha1(raw[32:]).digest()
    strings = [dex.string(i) for i in range(dex.strings_n)]
    assert strings == sorted(strings) and len(set(strings)) == len(strings)
    types = [dex.type_name(i) for i in range(dex.types_n)]
    assert types == sorted(types)
    # the map list covers every section, in file order
    map_off = struct.unpack_from("<I", raw, 52)[0]
    n = struct.unpack_from("<I", raw, map_off)[0]
    entries = [struct.unpack_from("<HHII", raw, map_off + 4 + 12 * i) for i in range(n)]
    assert [e[3] for e in entries] == sorted(e[3] for e in entries)
    assert {e[0] for e in entries} == {0x0000, 0x0001, 0x0002, 0x0003, 0x0004, 0x0005, 0x0006, 0x1000, 0x1001,
                                       0x2000, 0x2001, 0x2002}
    cls = _class(dex, 0)
    assert dex.classes_n == 1 and cls["name"] == sa.HELPER_T and not cls["virtual"] and not cls["instance"]
    assert {"ANDROID_ID", "ACCESSIBILITY_ENABLED", "CONTENT_URI", "logged"} <= set(cls["fields"])
    assert set(cls["methods"]) == {n + d for n, d in sa.METHODS} | {"<clinit>()V"}


def _invoked(dex: Dex, insns: list[int]) -> list[tuple[str, str, str]]:
    out, i = [], 0
    while i < len(insns):
        op = insns[i] & 0xFF
        if op in (0x6E, 0x71, 0x77):
            out.append((*dex.method(insns[i + 1]), dex.descriptor(insns[i + 1])))
        i += max(1, WIDTHS[op])
    return out


def test_get_string_answers_android_id_and_forwards_the_rest():
    dex = Dex(sa.helper_dex("0123456789abcdef"))
    regs, ins, outs, tries, insns = _class(dex, 0)["methods"]["getString" + GET_STRING_DESC]
    assert (regs, ins, outs, tries) == (4, 2, 2, 0)
    consts = [dex.string(insns[i + 1]) for i in range(len(insns) - 1) if insns[i] & 0xFF == 0x1A]
    assert consts[0] == "android_id" and "0123456789abcdef" in consts and sa.LOG_TAG in consts
    calls = _invoked(dex, insns)
    assert ("Ljava/lang/String;", "equals", "(Ljava/lang/Object;)Z") in calls
    assert calls[-1] == (sa.SECURE_T, "getString", GET_STRING_DESC)  # anything else goes to the real one
    # every other method forwards to Settings.Secure with the same arguments
    _r, ins, outs, _t, insns = _class(dex, 0)["methods"]["getLong(Landroid/content/ContentResolver;"
                                                          "Ljava/lang/String;J)J"]
    assert (ins, outs) == (4, 4) and insns[0] == (4 << 8) | 0x77 and insns[2] == 2  # invoke-static/range {v2..v5}
    assert _invoked(dex, insns) == [(sa.SECURE_T, "getLong", "(Landroid/content/ContentResolver;"
                                                             "Ljava/lang/String;J)J")]
    _r, _i, _o, tries, insns = _class(dex, 0)["methods"]["<clinit>()V"]
    assert tries == 1 and ("Ljava/lang/reflect/Field;", "set", "(Ljava/lang/Object;Ljava/lang/Object;)V") in \
        _invoked(dex, insns)


def test_helper_dex_takes_the_apps_own_references():
    uri_for = ("getUriFor", "(Landroid/net/Uri;Ljava/lang/String;)Landroid/net/Uri;")  # NameValueTable's
    dex = Dex(sa.helper_dex("0123456789abcdef", {uri_for}, {"SOME_INT": "I"}))
    cls = _class(dex, 0)
    assert "getUriFor(Landroid/net/Uri;Ljava/lang/String;)Landroid/net/Uri;" in cls["methods"]
    assert "SOME_INT" in cls["fields"]
    with pytest.raises(ValueError):
        sa.helper_dex("not hex")


def test_dexgen_try_ranges_and_branches():
    a = dexgen.Asm()
    a.const4(0, 0)
    a.label("loop")
    a.if_z("nez", 0, "end")
    a.label("try")
    a.const4(0, 1)
    a.label("after")
    a.goto("loop")
    a.label("end")
    a.ret("void")
    a.try_range("try", "after", "end")
    units, tries = a.finish()
    assert units == [0x0012, 0x0039, 5, 0x1012, 0x0029, (-3) & 0xFFFF, 0x000E]  # goto/16 at 4 back to 1
    assert tries == [(3, 1, 6)]
    assert dexgen.parse_descriptor("(I[JLa/B;[[La/C;)V") == (["I", "[J", "La/B;", "[[La/C;"], "V")


LIB = (b"\x7fELF" + b"\0" * 60 + b"what\0android/provider/Settings$Secure\0ACCESSIBILITY_ENABLED\0ANDROID_ID\0"
       b"getString\0(Landroid/content/ContentResolver;Ljava/lang/String;)Ljava/lang/String;\0")


def test_libunity_class_name_redirected_in_place():
    out = sa.redirect_native(LIB)
    assert out is not None and len(out) == len(LIB)
    assert b"\0android/provider/Settings$Securf\0ACCESSIBILITY_ENABLED\0" in out and b"Settings$Secure" not in out
    assert sa.native_fields(LIB) == ["ACCESSIBILITY_ENABLED", "ANDROID_ID"]
    assert sa.redirect_native(out) is None  # once
    assert sa.redirect_native(LIB.replace(b"ANDROID_ID", b"ANDROID_XX")) is None  # doesn't read the id
    assert sa.redirect_native(LIB + LIB[64:]) is None  # two class names: unclear, left alone


def _app_dex(extra_string: str | None = None) -> bytes:
    """An app class calling Settings.Secure.getString and getInt (invoke-static, the way javac compiles it)."""
    a = dexgen.Asm()  # v0 = name, v1 = int; cr = v2
    a.const_string(0, extra_string or "android_id")
    a.invoke("static", dexgen.M(sa.SECURE_T, "getString", GET_STRING_DESC), [2, 0])
    a.const4(1, 0)
    a.invoke("static", dexgen.M(sa.SECURE_T, "getInt", "(Landroid/content/ContentResolver;Ljava/lang/String;I)I"),
             [2, 0, 1])
    a.ret("void")
    cls = dexgen.ClassDef("Lcom/example/Ids;", methods=[
        dexgen.Method("read", "(Landroid/content/ContentResolver;)V", dexgen.ACC_PUBLIC | dexgen.ACC_STATIC, 3, 3, a)])
    return dexgen.build([cls])


def test_app_dex_type_renamed_in_place():
    raw = _app_dex()
    dex = Dex(raw)
    methods, fields = sa.dex_refs(dex)
    assert {n for n, _d in methods} == {"getString", "getInt"} and fields == {}
    assert sa.redirect_dex(dex)
    out = dex.finish()
    assert len(out) == len(raw)
    again = Dex(out)
    calls = {again.method(i) for i in range(again.methods_n)}
    assert {(sa.HELPER_T, "getString"), (sa.HELPER_T, "getInt"), ("Lcom/example/Ids;", "read")} == calls
    strings = [again.string(i) for i in range(again.strings_n)]
    assert strings == sorted(strings)  # still a valid (sorted) string table
    assert not sa.redirect_dex(again)  # once
    # a string that would end up between the old and the new name: left alone
    assert not sa.redirect_dex(Dex(_app_dex(sa.SECURE_T + "X")))


def _apk(tmp_path, entries: dict[str, bytes]):
    apk = tmp_path / "game.apk"
    with zipfile.ZipFile(apk, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return apk


def test_patch_finds_unity_behind_the_a32_bridge(tmp_path):
    """frame.a32_bridge (order 5) moves a 32-bit libunity.so to libunity.a32.so and puts a stub in its place."""
    apk = _apk(tmp_path, {"classes.dex": build_dex([("Lcom/example/A;", "f", "V", [0x000E])]),
                          "lib/arm64-v8a/libunity.so": b"\x7fELF stub", "lib/arm64-v8a/libunity.a32.so": LIB})
    with mock.patch.object(sa, "_keystore", lambda p: b"ks"), ApkWorkspace(apk) as ws:
        ctx = base.ApkContext(ws, _analysis(), {}, Reporter(), {})
        assert base.get("frame.stable_android_id").apply(ctx)
        assert b"Settings$Securf" in ws.read("lib/arm64-v8a/libunity.a32.so")
        assert "classes2.dex" in ws.names()


def test_patch_redirects_libunity_and_dex_and_adds_the_helper(tmp_path):
    apk = _apk(tmp_path, {"classes.dex": _app_dex(),
                          "lib/arm64-v8a/libunity.so": LIB, "lib/arm64-v8a/libmain.so": b"\x7fELF"})
    patch = base.get("frame.stable_android_id")
    with mock.patch.object(sa, "_keystore", lambda p: b"ks"), ApkWorkspace(apk) as ws:
        ctx = base.ApkContext(ws, _analysis(), {}, Reporter(), {"frame.stable_android_id": {}})
        assert patch.apply(ctx)
        assert b"Settings$Securf" in ws.read("lib/arm64-v8a/libunity.so")
        app = Dex(ws.read("classes.dex"))
        assert (sa.HELPER_T, "getString") in {app.method(i) for i in range(app.methods_n)}
        helper = Dex(ws.read("classes2.dex"))
        assert _class(helper, 0)["name"] == sa.HELPER_T
        wanted = sa.stable_id("com.example.runner", b"ks")
        assert wanted in [helper.string(i) for i in range(helper.strings_n)]
        assert wanted in ctx.notes[0]
        assert patch.validate(ctx)[0][1] is True


def test_patch_without_readers_changes_nothing(tmp_path):
    apk = _apk(tmp_path, {"classes.dex": build_dex([("Lcom/example/A;", "f", "V", [0x000E])]),
                          "lib/arm64-v8a/libmain.so": b"\x7fELF"})
    with mock.patch.object(sa, "_keystore", lambda p: b"ks"), ApkWorkspace(apk) as ws:
        ctx = base.ApkContext(ws, _analysis(), {}, Reporter(), {})
        assert not base.get("frame.stable_android_id").apply(ctx)
        assert "classes2.dex" not in ws.names()


def test_suggested_for_unity_games():
    p = base.get("frame.stable_android_id")
    assert p.detect(_analysis()).recommended
    assert p.detect(_analysis(libs=["libUE4.so"])) is None
    assert p.applies(_analysis(libs=["libUE4.so"]))  # Java readers exist in any app: available, not suggested
    assert base.works_on_a32(p) and not p.needs_vr


PREFIX = "10-25 22:16:54.006  1210  1234 E Unity   : "
TAIL = " [0x00000] in <00000000000000000000000000000000>:0"
RUNNER_LOG = "\n".join([  # GitHub #420's session log (argument lists shortened)
    "10-25 22:16:50.000   500   520 I ActivityManager: Start proc 1210:com.truantpixel.Runner/u0a61 for activity",
    PREFIX + "CryptographicException: Padding is invalid and cannot be removed.",
    PREFIX + "  at System.Security.Cryptography.RijndaelManagedTransform.DecryptData (System.Byte[] inputBuffer, "
             "System.Int32 inputOffset, System.Boolean fLast)" + TAIL,
    PREFIX + "  at System.Security.Cryptography.RijndaelManagedTransform.TransformFinalBlock (System.Byte[] "
             "inputBuffer)" + TAIL,
    PREFIX + "  at EncryptedXmlSerializer.DecryptData (System.String toDecrypt)" + TAIL,
    PREFIX + "  at EncryptedXmlSerializer.Load[T] (System.String path)" + TAIL,
    PREFIX + "  at DataManager..cctor ()" + TAIL, ""])


def test_triage_save_decrypt_failure():
    r = triage(RUNNER_LOG, "RUNNING", "com.truantpixel.Runner", applied=set())
    found = [f for f in r.findings if f.id == "save-decrypt-device-id"]
    assert found and found[0].suggest == ["frame.stable_android_id"]
    # with the patch built in, the old save's failure is expected once: no finding
    r = triage(RUNNER_LOG, "RUNNING", "com.truantpixel.Runner", applied={"frame.stable_android_id"})
    assert not [f for f in r.findings if f.id == "save-decrypt-device-id"]
    # padding errors outside save loading (e.g. a network library) don't match
    other = RUNNER_LOG.replace("EncryptedXmlSerializer.Load[T]", "Net.Session.Handshake").replace(
        "DataManager..cctor", "Net.Session.Connect").replace("EncryptedXmlSerializer.DecryptData", "Net.Cipher.Open")
    assert not [f for f in triage(other, "RUNNING", "com.truantpixel.Runner", applied=set()).findings
                if f.id == "save-decrypt-device-id"]
