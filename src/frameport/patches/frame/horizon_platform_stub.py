"""A local stand-in for Horizon OS's platform component, for games built with Meta's Horizon Platform SDK
(com.meta.xr.sdk.platform 2xx; GitHub #511, TMNT: Empire City 1.0.9).

On Android that SDK doesn't use libovrplatformloader.so (which OVRPort replaces) for its requests: Oculus.Platform's
AndroidClient creates `horizonos.supplement.hzplatformclientcore.HorizonPlatformCore` and
`AndroidHorizonPlatformConfig(activity, appId)`, calls `setup(config)` and `getGlobalSessionId()`, and then sends every
request as JSON (`makeRequest(module, name, apiVersion, json, cookie)` -> request id; `makeSession` for notification
streams) and collects the answers with `getMessageCount(session)` / `popMessage(session, yield)` (fields requestId,
statusCode, statusMessage, data, cookie). Those classes belong to a Horizon OS system component (the manifest's
`<horizonos:uses-horizonos-supplement name="horizonos-supplement-hzplatformclientcore">`), which the Frame doesn't
have: the start fails with ClassNotFoundException and games that wait for it stay on their loading screen.

This patch adds those classes to the game's own APK (an extra classesN.dex, sources `native/java-horizon`, artifact
`dex/horizon-platform-stub.dex`) plus an answer table `assets/frameport/horizon-platform.json` written here, so
`REQUESTS` below is the single place that decides what is answered:

- a short list of requests a device without Meta's services can answer harmlessly gets a success with minimal data
  (`ANSWERS`, each with the reason it isn't a licence answer);
- licence and account-proof requests (`LICENCE_MODULES`, `LICENCE_REQUESTS`: entitlement, purchases and products,
  DLC downloads, user proof / access tokens / auth links, linked and SDK accounts, device attestation, graph API calls
  made with the user's token) always get an error status;
- everything else, including requests this table doesn't know (newer SDKs), gets the same error status
  (12 = RequestNotSupported). FramePort never answers a licence check or claims a Meta sign-in.

The `<uses-horizonos-supplement>` entry stays: Android (and Lepton) ignore an element in an unknown namespace under
<manifest>, and the game already installed and ran with it (#511's log comes from the running game).
A game that requires its licence check to succeed still stops after this, just later and with a clear log line.
"""
from __future__ import annotations

import json
import re

from ...apk import axml
from ...core.paths import artifacts_dir
from ..base import ApkContext, Patch, Suggestion, register
from .ovr_stable_user import _keystore, stable_id

SUPPLEMENT = "horizonos-supplement-hzplatformclientcore"
CORE_CLASS = "horizonos.supplement.hzplatformclientcore.HorizonPlatformCore"
DEX_CLASS = b"Lhorizonos/supplement/hzplatformclientcore/HorizonPlatformCore;"
ARTIFACT = "dex/horizon-platform-stub.dex"
CONFIG = "assets/frameport/horizon-platform.json"
NOT_SUPPORTED = 12  # HorizonStatus RequestNotSupported: the status of every answer that isn't a success
DISPLAY_NAME = "Player"

# Licence and account-proof requests: never answered with a success, whatever the request name within the module.
LICENCE_MODULES = {
    "entitlements": "licence check (is the user entitled to the app)",
    "iap": "purchases, products, checkout, trial offers: ownership",
    "asset_file": "downloadable content: tied to purchases",
    "device_application_integrity": "attestation / integrity tokens that servers verify",
    "graph_api": "Meta server calls made with the user's token",
}
LICENCE_REQUESTS = {
    "users/get_user_proof": "a nonce a game server verifies with Meta: proof of the account",
    "users/get_access_token": "the user's Meta access token",
    "users/send_auth_url": "an account sign-in hand-off",
    "users/get_linked_accounts": "accounts linked to the Meta account",
    "users/get_linked_account_page_by_url": "accounts linked to the Meta account",
    "users/get_sdk_accounts": "accounts of the Meta account",
    "users/get_sdk_account_page_by_url": "accounts of the Meta account",
    "users/new_test_user": "creates Meta test accounts",
    "users/new_test_user_friends": "creates Meta test accounts",
}

# Error answers still go through Newtonsoft's DeserializeObject<T>(data) in Message<T>; requests whose result isn't an
# object need data of their own type there (from the SDK's Request<T> types), the rest get "{}" (strings: "").
_ERROR_DATA_BY_KIND = {
    "false": ["http/write", "leaderboards/write_entry", "leaderboards/write_entry_with_supplementary_metric",
              "net_sync/get_listener_position", "net_sync/get_voip_amplitude", "parties/plugin_get_voip_passthrough",
              "push_notification/unregister"],
    "0": ["asset_file/download_by_id_list", "asset_file/download_by_name_list", "parties/plugin_get_shared_mem_handle",
          "http/start_transfer", "voip/get_sync_timestamp_difference", "voip/get_sync_timestamp",
          "net_sync/get_ambisonic_float_pcm", "net_sync/get_ambisonic_int16_pcm",
          "net_sync/get_ambisonic_interleaved_float_pcm", "net_sync/get_ambisonic_interleaved_int16_pcm",
          "net_sync/get_monostream_float_pcm", "net_sync/get_monostream_int16_pcm",
          "net_sync/get_pcm_buffer_max_samples", "voip/get_output_buffer_max_size", "voip/get_pcm_size"],
    "[]": ["consent/get_consent_status", "in_app_analytics/close_all_open_segments",
           "in_app_analytics/get_all_segments"],
    "": ["application/launch_other_app", "device_application_integrity/get_attestation_token",
         "device_application_integrity/get_integrity_token", "graph_api/get", "graph_api/post", "http/get",
         "http/get_to_file", "http/multi_part_post", "http/post", "users/get_access_token", "users/new_test_user",
         "users/new_test_user_friends"],
}
ERROR_DATA = {key: data for data, keys in _ERROR_DATA_BY_KIND.items() for key in keys}

EMPTY_LIST = {"data": []}
NO_RESULT = {}  # requests whose result the SDK doesn't read (Request without a type)


def _no_op(why: str):
    return {"data": NO_RESULT, "why": why}


# key -> {"data": JSON value or callable(info) -> value, "match": {field: value or callable}, "why": reason}
ANSWERS = {
    "users/get_logged_in_user": {
        "data": lambda i: _user(i),
        "why": "the local player: the same per-game, per-FramePort-user id frame.ovr_stable_user gives OVRPort's "
               "platform library, no token or proof (servers can't verify it with Meta)"},
    "users/get": {
        "data": lambda i: _user(i), "match": {"user_id": lambda i: str(i["user_id"])},
        "why": "the same local player, only when the game asks for that id (other ids: error)"},
    "users/get_logged_in_user_friends": {"data": EMPTY_LIST, "why": "no friends list without Meta's services"},
    "users/get_blocked_users": {"data": EMPTY_LIST, "why": "nobody blocked"},
    "users/get_org_scoped_id": {
        "data": lambda i: {"org_scoped_id": str(i["org_scoped_id"])},
        "match": {"user_id": lambda i: str(i["user_id"])},
        "why": "the hash of the local id that frame.ovr_org_scoped_id answers for OVRPort's library: an analytics/"
               "save key, not a proof (only for the local player's id)"},
    "application/get_version": {
        "data": lambda i: {"currentVersionCode": i["version_code"], "currentVersion": i["version_name"],
                           "latestVersionCode": i["version_code"], "latestVersion": i["version_name"]},
        "why": "the installed APK's own version as both current and latest: no update prompt"},
    "application_lifecycle/get_launch_details": {
        "data": {"type": "NORMAL", "launch_source": ""}, "why": "a normal start (no invite or deep link)"},
    "application_lifecycle/log_deeplink_result": _no_op("logging only"),
    "achievements/get_all_definitions": {"data": EMPTY_LIST, "why": "no achievements service: none defined"},
    "achievements/get_definitions_by_name": {"data": EMPTY_LIST, "why": "no achievements service: none defined"},
    "achievements/get_all_progress": {"data": EMPTY_LIST, "why": "no achievements service: no progress"},
    "achievements/get_progress_by_name": {"data": EMPTY_LIST, "why": "no achievements service: no progress"},
    "achievements/unlock": {"data": {"just_unlocked": False}, "why": "accepted, nothing stored or unlocked"},
    "achievements/add_count": {"data": {"just_unlocked": False}, "why": "accepted, nothing stored or unlocked"},
    "achievements/add_fields": {"data": {"just_unlocked": False}, "why": "accepted, nothing stored or unlocked"},
    "analytics/send_metric_event": _no_op("analytics: dropped"),
    "analytics/send_segment_event": _no_op("analytics: dropped"),
    "in_app_analytics/queue_metric_event": _no_op("analytics: dropped"),
    "in_app_analytics/queue_segment_event": _no_op("analytics: dropped"),
    "in_app_analytics/send_event": _no_op("analytics: dropped"),
    "notifications/device_notification": _no_op("system notification: not shown"),
    "notifications/trigger_notification": _no_op("system notification: not shown"),
    "group_presence/set": _no_op("rich presence: nobody to show it to"),
    "group_presence/clear": _no_op("rich presence: nobody to show it to"),
    "group_presence/set_destination": _no_op("rich presence: nobody to show it to"),
    "group_presence/set_is_joinable": _no_op("rich presence: nobody to show it to"),
    "group_presence/set_lobby_session": _no_op("rich presence: nobody to show it to"),
    "group_presence/set_match_session": _no_op("rich presence: nobody to show it to"),
    "group_presence/set_deeplink_message_override": _no_op("rich presence: nobody to show it to"),
    "abuse_report/report_request_handled": _no_op("acknowledgement of the game's own report UI"),
}


def _user(info: dict) -> dict:
    return {"id": str(info["user_id"]), "display_name": DISPLAY_NAME, "alias": DISPLAY_NAME}


def is_licence(key: str) -> bool:
    return key in LICENCE_REQUESTS or key.split("/", 1)[0] in LICENCE_MODULES


def classify(module: str, name: str) -> str:
    """"answer" (a local success), "licence" (licence or account proof: error) or "unsupported" (error)."""
    key = f"{module}/{name}"
    if is_licence(key):
        return "licence"
    return "answer" if key in ANSWERS else "unsupported"


def org_scoped_id(user_id: int) -> int:
    """native/langpack's org_scoped_id(): FNV-1a over the user id's 8 bytes, seeded with "org", 63 bits, never 0
    (frame.ovr_org_scoped_id answers the same id for OVRPort's platform library)."""
    h = 1469598103934665603 ^ 0x6F7267
    for i in range(8):
        h ^= (user_id >> (8 * i)) & 0xFF
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    h &= 0x7FFFFFFFFFFFFFFF
    return h or 1


def config(info: dict) -> dict:
    """The answer table HorizonPlatformCore reads. `info`: user_id, org_scoped_id, version_code, version_name."""
    answers = {}
    for key, entry in ANSWERS.items():
        assert not is_licence(key), key
        data = entry["data"](info) if callable(entry["data"]) else entry["data"]
        out = {"data": json.dumps(data, separators=(",", ":"))}
        if entry.get("match"):
            out["match"] = {f: (v(info) if callable(v) else v) for f, v in entry["match"].items()}
        answers[key] = out
    return {"answers": answers, "error_data": dict(ERROR_DATA),
            "licence": sorted([f"{m}/*" for m in LICENCE_MODULES] + list(LICENCE_REQUESTS))}


def respond(table: dict, module: str, name: str, request_json: str = "{}") -> tuple[int, str]:
    """(status, data) the Java stand-in answers for a request with `table` (its logic, for tests and docs)."""
    key = f"{module}/{name}"
    entry = table["answers"].get(key)
    if entry:
        try:
            request = json.loads(request_json or "{}")
        except ValueError:
            request = None
        match = entry.get("match") or {}
        if isinstance(request, dict) and all(str(request.get(f)) == v and f in request for f, v in match.items()):
            return 0, entry["data"]
    return NOT_SUPPORTED, table["error_data"].get(key, "{}")


def has_supplement(a) -> bool:
    return SUPPLEMENT in ((a.extra or {}).get("horizonos_supplements") or [])


class HorizonPlatformStub(Patch):
    id = "frame.horizon_platform_stub"
    title = "Horizon platform stand-in (no licence answers)"
    description = ("Games built with Meta's newer Horizon Platform SDK (2xx) start Meta's services through a Horizon "
                   "OS system component the Frame doesn't have (log: Failed to initialize HorizonPlatformCore: "
                   "ClassNotFoundException), and games that wait for it stay on their loading screen. This adds a "
                   "local stand-in to the game: it answers harmless requests (the local player with a fixed id, the "
                   "app's version, empty friends and achievement lists, analytics and rich presence accepted and "
                   "dropped) and answers an error to everything else. Licence, purchase and account checks are "
                   "never answered with a success, so a game that requires them still stops, with a clear log line.")
    order = 46
    revision = 1

    def applies(self, a):
        return has_supplement(a)

    def detect(self, a):
        if has_supplement(a):
            return Suggestion(True, "The game uses Meta's Horizon Platform SDK, whose start needs a Horizon OS "
                                    "component the Frame doesn't have; without a stand-in it waits forever.")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        dexes = sorted((n for n in ws.names() if re.fullmatch(r"classes\d*\.dex", n)),
                       key=lambda n: int(n[7:-4] or 1))
        if any(DEX_CLASS in ws.read(n) for n in dexes):
            ctx.checks.append(("Horizon platform stand-in", None, "the APK already has HorizonPlatformCore: "
                                                                  "nothing added"))
            return False
        manifest = axml.manifest_attrs(ws.read("AndroidManifest.xml"))
        package = ctx.analysis.package
        user_id = stable_id(package, _keystore(package))
        info = {"user_id": user_id, "org_scoped_id": org_scoped_id(user_id),
                "version_code": int(manifest.get("versionCode") or 0),
                "version_name": str(manifest.get("versionName") or ctx.analysis.version or "")}
        name = f"classes{len(dexes) + 1}.dex" if dexes else "classes.dex"
        ws.put(name, (artifacts_dir() / ARTIFACT).read_bytes())
        ws.put(CONFIG, json.dumps(config(info), indent=1, sort_keys=True).encode())
        ctx.notes.append(f"Horizon platform stand-in {CORE_CLASS} added ({name}, answers {CONFIG}): "
                         f"{len(ANSWERS)} harmless requests answered, licence and account checks not")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        dexes = [n for n in ws.names() if re.fullmatch(r"classes\d*\.dex", n)]
        if any(DEX_CLASS in ws.read(n) for n in dexes) and ws.has(CONFIG):
            return [("Horizon platform stand-in", True, "HorizonPlatformCore and its answer table are in the APK")]
        return [("Horizon platform stand-in", False, "HorizonPlatformCore or its answer table is missing")]


register(HorizonPlatformStub)
