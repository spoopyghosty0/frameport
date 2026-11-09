"""frame.horizon_platform_stub (GitHub #511): a local HorizonPlatformCore for Meta's Horizon Platform SDK. The dex must
offer exactly the constructors, methods and fields Oculus.Platform's AndroidClient reaches through JNI, and the answer
table must never answer a licence or account-proof request with a success."""
from __future__ import annotations

import json
import struct
import zipfile

import pytest
from conftest import build_axml

from frameport.apk.dex import Dex
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.core.paths import artifacts_dir
from frameport.patches import base
from frameport.patches.frame import horizon_platform_stub as hp
from frameport.validate.triage import triage

PKG = "horizonos/supplement/hzplatformclientcore/"
CORE, CONFIG_CLS, MESSAGE = f"L{PKG}HorizonPlatformCore;", f"L{PKG}AndroidHorizonPlatformConfig;", \
    f"L{PKG}PlatformMessage;"
INFO = {"user_id": 6000000001015022177, "org_scoped_id": hp.org_scoped_id(6000000001015022177),
        "version_code": 109, "version_name": "1.0.9"}


def _protos(d: Dex, idx: int) -> tuple[str, list[str]]:
    """(return type, parameter types) of method id `idx`."""
    proto = struct.unpack_from("<HHI", d.data, d.methods_off + 8 * idx)[1]
    protos_off = struct.unpack_from("<I", d.data, 76)[0]
    _shorty, ret, params = struct.unpack_from("<III", d.data, protos_off + 12 * proto)
    out = []
    if params:
        n = struct.unpack_from("<I", d.data, params)[0]
        out = [d.type_name(struct.unpack_from("<H", d.data, params + 4 + 2 * i)[0]) for i in range(n)]
    return d.type_name(ret), out


def _defined(d: Dex, cls: str) -> dict[str, tuple[str, list[str]]]:
    return {d.method(i)[1]: _protos(d, i) for i, _code in d.class_methods(cls)}


def test_dex_has_the_jni_surface_the_sdk_calls():
    d = Dex((artifacts_dir() / hp.ARTIFACT).read_bytes())
    assert {CORE, CONFIG_CLS, MESSAGE} <= set(d.class_names())
    core = _defined(d, CORE)
    # AndroidClient: new HorizonPlatformCore(); setup(config); Call<long> getGlobalSessionId; Call<long>
    # makeRequest/makeSession(string, string, int, string, int); Call<int> getMessageCount(long); stopSession(long);
    # Call<AndroidJavaObject> popMessage(long, bool)
    assert core["<init>"] == ("V", [])
    assert core["setup"] == ("V", [CONFIG_CLS])
    assert core["getGlobalSessionId"] == ("J", [])
    s = "Ljava/lang/String;"
    assert core["makeRequest"] == ("J", [s, s, "I", s, "I"])
    assert core["makeSession"] == ("J", [s, s, "I", s, "I"])
    assert core["getMessageCount"] == ("I", ["J"])
    assert core["stopSession"] == ("V", ["J"])
    assert core["popMessage"] == (MESSAGE, ["J", "Z"])
    # new AndroidHorizonPlatformConfig(currentActivity, AppID): Unity matches the activity by assignability
    assert _defined(d, CONFIG_CLS)["<init>"] == ("V", ["Landroid/content/Context;", s])
    fields_n = struct.unpack_from("<I", d.data, 80)[0]
    fields = {name: typ for cls, typ, name in (d.field(i) for i in range(fields_n)) if cls == MESSAGE}
    assert fields == {"requestId": "J", "statusCode": "I", "statusMessage": s, "data": s, "cookie": "I"}
    assert hp.DEX_CLASS.decode() == CORE
    assert b"frameport/horizon-platform.json" in d.data  # the asset the patch writes (hp.CONFIG)
    assert hp.CONFIG == "assets/frameport/horizon-platform.json"


@pytest.mark.parametrize("key", [
    "entitlements/get_is_viewer_entitled", "iap/get_viewer_purchases", "iap/launch_checkout_flow",
    "iap/get_products_by_sku", "iap/consume_purchase", "asset_file/download_by_name",
    "device_application_integrity/get_integrity_token", "graph_api/post", "users/get_user_proof",
    "users/get_access_token", "users/send_auth_url", "users/get_linked_accounts", "users/get_sdk_accounts",
    "entitlements/some_future_check", "iap/some_future_request"])
def test_licence_and_account_proof_are_errors(key):
    module, name = key.split("/")
    assert hp.classify(module, name) == "licence"
    table = hp.config(INFO)
    status, _data = hp.respond(table, module, name, "{}")
    assert status == hp.NOT_SUPPORTED
    assert key not in table["answers"]
    assert key in table["licence"] or f"{module}/*" in table["licence"]


def test_harmless_requests_get_minimal_success():
    table = hp.config(INFO)
    uid = str(INFO["user_id"])
    status, data = hp.respond(table, "users", "get_logged_in_user")
    assert status == 0 and json.loads(data) == {"id": uid, "display_name": "Player", "alias": "Player"}
    # the same player by id; another id is an error
    assert hp.respond(table, "users", "get", json.dumps({"user_id": uid}))[0] == 0
    assert hp.respond(table, "users", "get", json.dumps({"user_id": "5"}))[0] == hp.NOT_SUPPORTED
    assert hp.respond(table, "users", "get", "not json")[0] == hp.NOT_SUPPORTED
    status, data = hp.respond(table, "users", "get_org_scoped_id", json.dumps({"user_id": uid}))
    assert status == 0 and json.loads(data) == {"org_scoped_id": str(INFO["org_scoped_id"])}
    status, data = hp.respond(table, "application", "get_version")
    assert status == 0 and json.loads(data) == {"currentVersionCode": 109, "currentVersion": "1.0.9",
                                                "latestVersionCode": 109, "latestVersion": "1.0.9"}
    for module, name in [("users", "get_logged_in_user_friends"), ("achievements", "get_all_progress"),
                         ("achievements", "get_all_definitions")]:
        assert hp.respond(table, module, name) == (0, '{"data":[]}')
    for module, name in [("group_presence", "set"), ("in_app_analytics", "send_event"),
                         ("notifications", "trigger_notification"), ("analytics", "send_metric_event")]:
        assert hp.respond(table, module, name) == (0, "{}")
    assert json.loads(hp.respond(table, "application_lifecycle", "get_launch_details")[1])["type"] == "NORMAL"
    assert all(hp.classify(*k.split("/")) == "answer" for k in hp.ANSWERS)
    assert all(e["why"] for e in hp.ANSWERS.values())  # each success says why it isn't a licence answer


def test_everything_else_is_an_error_with_deserializable_data():
    table = hp.config(INFO)
    assert hp.classify("leaderboards", "write_entry") == "unsupported"
    assert hp.respond(table, "leaderboards", "write_entry") == (hp.NOT_SUPPORTED, "false")  # Request<bool>
    assert hp.respond(table, "voip", "get_pcm_size") == (hp.NOT_SUPPORTED, "0")
    assert hp.respond(table, "consent", "get_consent_status") == (hp.NOT_SUPPORTED, "[]")
    assert hp.respond(table, "users", "get_access_token") == (hp.NOT_SUPPORTED, "")  # Request<string>
    assert hp.respond(table, "user_data_store", "private_get_entries") == (hp.NOT_SUPPORTED, "{}")
    assert hp.respond(table, "no_such_module", "anything") == (hp.NOT_SUPPORTED, "{}")
    assert not set(table["answers"]) & set(hp.LICENCE_REQUESTS)


def test_org_scoped_id_matches_the_native_answer():
    # native/langpack org_scoped_id(): FNV-1a over the 8 little-endian bytes, seeded ^ "org", 63 bits
    h = 1469598103934665603 ^ 0x6F7267
    for b in (42).to_bytes(8, "little"):
        h = ((h ^ b) * 1099511628211) % 2 ** 64
    assert hp.org_scoped_id(42) == h & (2 ** 63 - 1)


SUPPLEMENT_MANIFEST = [
    ("start", "manifest", [("package", "str", "com.CortopiaStudios.draxPrototype"), ("versionCode", "int", 109),
                           ("versionName", "str", "1.0.9")]),
    ("start", "uses-horizonos-supplement", [("name", "str", hp.SUPPLEMENT)]), ("end", "uses-horizonos-supplement"),
    ("start", "application", []), ("end", "application"), ("end", "manifest")]


def _analysis(*supplements) -> Analysis:
    return Analysis(package="com.CortopiaStudios.draxPrototype", version="1.0.9", label="TMNT", abis=["arm64-v8a"],
                    engine="Unity", xr="OpenXR", graphics="?", direct_vrapi=False, libs=[], launcher_activity=None,
                    has_info_category=False, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
                    oculus_os_classes=False, is_overport_output=False, debuggable=False,
                    extra={"horizonos_supplements": list(supplements)})


def test_detect_and_applies():
    patch = base.get("frame.horizon_platform_stub")
    with_it, without = _analysis(hp.SUPPLEMENT), _analysis()
    assert patch.applies(with_it) and not patch.applies(without)
    assert not patch.applies(_analysis("horizonos-supplement-something-else"))
    s = patch.detect(with_it)
    assert s is not None and s.recommended
    assert patch.detect(without) is None


def test_patch_adds_dex_and_answer_table(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "_keystore", lambda package: b"keystore")
    apk = tmp_path / "in.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", build_axml(SUPPLEMENT_MANIFEST))
        z.writestr("classes.dex", b"dex\n035\0")
    patch = base.get("frame.horizon_platform_stub")
    a = _analysis(hp.SUPPLEMENT)
    with ApkWorkspace(apk) as ws:
        ctx = base.ApkContext(ws, a, {}, Reporter(), {patch.id: {}})
        assert patch.apply(ctx)
        assert [ok for _, ok, _ in patch.validate(ctx)] == [True]
        assert not patch.apply(base.ApkContext(ws, a, {}, Reporter(), {}))  # once
        out = ws.write(tmp_path / "out.apk")
    with zipfile.ZipFile(out) as z:
        assert CORE in Dex(z.read("classes2.dex")).class_names()
        table = json.loads(z.read(hp.CONFIG))
        assert z.read("AndroidManifest.xml") == build_axml(SUPPLEMENT_MANIFEST)  # manifest untouched
    uid = hp.stable_id(a.package, b"keystore")  # frame.ovr_stable_user's id for this game
    assert json.loads(table["answers"]["users/get_logged_in_user"]["data"])["id"] == str(uid)
    assert json.loads(table["answers"]["application/get_version"]["data"])["currentVersionCode"] == 109


def test_triage_suggests_the_stub_and_flags_licence_refusals():
    log = ("E Unity   : Failed to initialize HorizonPlatformCore: java.lang.ClassNotFoundException: "
           "horizonos.supplement.hzplatformclientcore.HorizonPlatformCore\n")
    found = {f.id: f for f in triage(log, "RUNNING").findings}
    assert found["horizon-platform-core-missing"].suggest == ["frame.horizon_platform_stub"]
    log = ("I FramePort: Horizon platform: entitlements/get_is_viewer_entitled -> not answered "
           "(licence or account check)\n")
    assert "horizon-platform-licence-refused" in [f.id for f in triage(log, "RUNNING").findings]
    log = "I FramePort: Horizon platform: users/get_logged_in_user -> answered locally\n"
    assert "horizon-platform-licence-refused" not in [f.id for f in triage(log, "RUNNING").findings]
