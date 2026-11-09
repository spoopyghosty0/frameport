"""Lepton gives a game a new Android device id (ANDROID_ID) at every start, so saves or logins tied to it break.

Since Android 8, `Settings.Secure.getString(cr, "android_id")` answers each app with its own id (SSAID), which the
settings provider makes up the first time and keeps in `/data/system/users/0/settings_ssaid.xml`. Lepton throws the
container's whole /data overlay (`<base>/lepton-data/baked/data_overlay`) away at every exit, so that file never
survives and the game gets a new id at every start (only the device-wide legacy id, `settings get secure
android_id`, stays the same). Unity's `SystemInfo.deviceUniqueIdentifier` comes from it: RUNNER (GitHub #420)
encrypts its save with that value and can't decrypt it at the next start (`CryptographicException: Padding is
invalid` in its save loader), and any game that files saves, keys or guest logins under the id has the same problem.

The patch makes the game read a fixed id instead: 16 hex digits from a hash of the package name and the game's
signing keystore (like frame.ovr_stable_user: the same for every build and reinstall of the game, different for each
game and each FramePort user). It adds a class `android.provider.Settings$Securf` in an extra classesN.dex (written by
`apk/dexgen.py`, the id inside its code) that forwards every member of `Settings.Secure` to the real class, except
`getString(cr, "android_id")`, which returns the fixed id (and logs it once, logcat tag `fp_android_id`). The game
reaches it two ways:
- Unity reads the id in native code (libunity.so's JNI binding of `android/provider/Settings$Secure`: field
  ANDROID_ID + getString), so libunity's class name string becomes the helper's (same length, in place).
- Java code (`Settings.Secure.getString` in classes*.dex, e.g. Unity Ads, bHaptics, Unreal's GameActivity): the
  type name `Landroid/provider/Settings$Secure;` in the app's dex is renamed in place to the helper's. Same length and
  the same place in the sorted string table, so the dex stays valid and every other byte stays as it was; every
  method and field of the type the dex uses is forwarded.
Code that looks the class up by name at runtime (C# `AndroidJavaClass("android.provider.Settings$Secure")`, other
native libraries) still gets the changing id.
"""
from __future__ import annotations

import hashlib
import re

from ...apk import dexgen
from ...apk.dex import Dex
from ...apk.dexgen import ACC_CONSTRUCTOR, ACC_PRIVATE, ACC_PUBLIC, ACC_STATIC, Asm, F, M
from ..base import ApkContext, Patch, Suggestion, register

SECURE = "android/provider/Settings$Secure"
HELPER = "android/provider/Settings$Securf"  # same length; sorts right after SECURE
SECURE_T, HELPER_T = f"L{SECURE};", f"L{HELPER};"
LOG_TAG = "fp_android_id"
KEY = "android_id"
CR, STR = "Landroid/content/ContentResolver;", "Ljava/lang/String;"

# Settings.Secure's public static methods (Android SDK): forwarded unchanged, except getString
METHODS = [
    ("getFloat", f"({CR}{STR})F"), ("getFloat", f"({CR}{STR}F)F"),
    ("getInt", f"({CR}{STR})I"), ("getInt", f"({CR}{STR}I)I"),
    ("getLong", f"({CR}{STR})J"), ("getLong", f"({CR}{STR}J)J"),
    ("getString", f"({CR}{STR}){STR}"),
    ("getUriFor", f"({STR})Landroid/net/Uri;"),
    ("isLocationProviderEnabled", f"({CR}{STR})Z"),
    ("putFloat", f"({CR}{STR}F)Z"), ("putInt", f"({CR}{STR}I)Z"), ("putLong", f"({CR}{STR}J)Z"),
    ("putString", f"({CR}{STR}{STR})Z"),
    ("setLocationProviderEnabled", f"({CR}{STR}Z)V"),
]
GET_STRING = ("getString", f"({CR}{STR}){STR}")
# Settings.Secure's constants as Unity's JNI binding lists them; the helper copies their values from the real class
# when it loads (missing ones stay null/0)
FIELDS = (
    "ACCESSIBILITY_DISPLAY_INVERSION_ENABLED ACCESSIBILITY_ENABLED ACCESSIBILITY_SPEAK_PASSWORD ADB_ENABLED "
    "ALLOWED_GEOLOCATION_ORIGINS ALLOW_MOCK_LOCATION ANDROID_ID BACKGROUND_DATA BLUETOOTH_ON CONTENT_URI DATA_ROAMING "
    "DEFAULT_INPUT_METHOD DEVELOPMENT_SETTINGS_ENABLED DEVICE_PROVISIONED ENABLED_ACCESSIBILITY_SERVICES "
    "ENABLED_INPUT_METHODS HTTP_PROXY INPUT_METHOD_SELECTOR_VISIBILITY INSTALL_NON_MARKET_APPS LOCATION_MODE "
    "LOCATION_MODE_BATTERY_SAVING LOCATION_MODE_HIGH_ACCURACY LOCATION_MODE_OFF LOCATION_MODE_SENSORS_ONLY "
    "LOCATION_PROVIDERS_ALLOWED LOCK_PATTERN_ENABLED LOCK_PATTERN_TACTILE_FEEDBACK_ENABLED LOCK_PATTERN_VISIBLE "
    "LOGGING_ID PARENTAL_CONTROL_ENABLED PARENTAL_CONTROL_LAST_UPDATE PARENTAL_CONTROL_REDIRECT_URL RTT_CALLING_MODE "
    "SELECTED_INPUT_METHOD_SUBTYPE SETTINGS_CLASSNAME SKIP_FIRST_USE_HINTS TOUCH_EXPLORATION_ENABLED "
    "TTS_DEFAULT_COUNTRY TTS_DEFAULT_LANG TTS_DEFAULT_PITCH TTS_DEFAULT_RATE TTS_DEFAULT_SYNTH TTS_DEFAULT_VARIANT "
    "TTS_ENABLED_PLUGINS TTS_USE_DEFAULTS USB_MASS_STORAGE_ENABLED USE_GOOGLE_MAIL WIFI_MAX_DHCP_RETRY_COUNT "
    "WIFI_MOBILE_DATA_TRANSITION_WAKELOCK_TIMEOUT_MS WIFI_NETWORKS_AVAILABLE_NOTIFICATION_ON "
    "WIFI_NETWORKS_AVAILABLE_REPEAT_DELAY WIFI_NUM_OPEN_NETWORKS_KEPT WIFI_ON "
    "WIFI_WATCHDOG_ACCEPTABLE_PACKET_LOSS_PERCENTAGE WIFI_WATCHDOG_AP_COUNT WIFI_WATCHDOG_BACKGROUND_CHECK_DELAY_MS "
    "WIFI_WATCHDOG_BACKGROUND_CHECK_ENABLED WIFI_WATCHDOG_BACKGROUND_CHECK_TIMEOUT_MS "
    "WIFI_WATCHDOG_INITIAL_IGNORED_PING_COUNT WIFI_WATCHDOG_MAX_AP_CHECKS WIFI_WATCHDOG_ON WIFI_WATCHDOG_PING_COUNT "
    "WIFI_WATCHDOG_PING_DELAY_MS WIFI_WATCHDOG_PING_TIMEOUT_MS WIFI_WATCHDOG_WATCH_LIST").split()
FIELD_TYPES = {"CONTENT_URI": "Landroid/net/Uri;", "LOCATION_MODE_OFF": "I", "LOCATION_MODE_SENSORS_ONLY": "I",
               "LOCATION_MODE_BATTERY_SAVING": "I", "LOCATION_MODE_HIGH_ACCURACY": "I"}  # the rest are Strings
NATIVE_CLASS = b"\0" + SECURE.encode() + b"\0"
# libunity.so, or its 32-bit original next to frame.a32_bridge's stub (the bridge runs first)
UNITY_LIB = re.compile(r"lib/[^/]+/libunity(?:\.a32)?\.so")


def stable_id(package: str, keystore: bytes) -> str:
    """16 hex digits (an Android id's form) from the package name and the game's signing keystore."""
    return hashlib.sha256(b"android_id\0" + package.encode() + b"\0" + keystore).hexdigest()[:16]


# --- native: libunity.so's JNI binding -------------------------------------------------------------------------------

def native_fields(lib: bytes) -> list[str]:
    """Constant names that follow the class name in a JNI binding's string table (Unity's member list)."""
    at = lib.find(NATIVE_CLASS)
    if at < 0:
        return []
    out = []
    for name in lib[at + len(NATIVE_CLASS):at + len(NATIVE_CLASS) + 8192].split(b"\0"):
        if not re.fullmatch(rb"[A-Z][A-Z0-9_]*", name):
            break
        out.append(name.decode())
    return out


def redirect_native(lib: bytes) -> bytes | None:
    """The library with its `android/provider/Settings$Secure` class name turned into the helper's, or None when it
    doesn't read the Android id through that class (exactly one class name and an ANDROID_ID member name)."""
    if lib.count(NATIVE_CLASS) != 1 or b"\0ANDROID_ID\0" not in lib:
        return None
    return lib.replace(NATIVE_CLASS, b"\0" + HELPER.encode() + b"\0")


# --- Java: the app's classes*.dex ------------------------------------------------------------------------------------

def dex_refs(dex: Dex) -> tuple[set[tuple[str, str]], dict[str, str]] | None:
    """(methods (name, descriptor), fields {name: type}) the dex uses on Settings.Secure, or None when one can't be
    forwarded (a constructor)."""
    methods = {(dex.method(i)[1], dex.descriptor(i)) for i in range(dex.methods_n) if dex.method(i)[0] == SECURE_T}
    if any(name.startswith("<") for name, _d in methods):
        return None
    fields = {}
    for i in range(dex.fields_n):
        cls, typ, name = dex.field(i)
        if cls == SECURE_T:
            fields[name] = typ
    return methods, fields


def redirect_dex(dex: Dex) -> bool:
    """Rename the type `Landroid/provider/Settings$Secure;` to the helper's in place. Only when the new name keeps its
    place in the sorted string table (the next string sorts after it) and isn't there already."""
    idx = dex.find_string(SECURE_T)
    if idx is None or dex.find_string(HELPER_T) is not None:
        return False
    if idx + 1 < dex.strings_n and list(dex.string(idx + 1).encode("utf-16-be")) <= list(HELPER_T.encode("utf-16-be")):
        return False
    at = dex.string_data_offset(idx)
    dex.data[at:at + len(HELPER_T)] = HELPER_T.encode()
    return True


# --- the helper class ------------------------------------------------------------------------------------------------

def _kind(t: str) -> str:
    return "void" if t == "V" else "wide" if t in ("J", "D") else "object" if t[0] in "L[" else ""


def _forward(name: str, desc: str) -> dexgen.Method:
    params, ret = dexgen.parse_descriptor(desc)
    ins, res = dexgen.words(params), {"void": 0, "wide": 2}.get(_kind(ret), 1)
    a = Asm()
    a.invoke_static_range(M(SECURE_T, name, desc), res, ins)
    if ret != "V":
        a.move_result(_kind(ret), 0)
    a.ret(_kind(ret), 0)
    return dexgen.Method(name, desc, ACC_PUBLIC | ACC_STATIC, res + ins, ins, a)


def _get_string(android_id: str) -> dexgen.Method:
    """getString(cr, name): the fixed id for "android_id" (logged once), else Settings.Secure.getString."""
    logged = F(HELPER_T, "logged", "Z")
    a = Asm()  # v0, v1; cr = v2, name = v3
    a.const_string(0, KEY)
    a.invoke("virtual", M(STR, "equals", "(Ljava/lang/Object;)Z"), [0, 3])
    a.move_result("", 1)
    a.if_z("eqz", 1, "forward")
    a.sget("boolean", 1, logged)
    a.if_z("nez", 1, "answer")
    a.const4(1, 1)
    a.sput("boolean", 1, logged)
    a.const_string(0, LOG_TAG)
    a.const_string(1, f"android_id -> {android_id} (frame.stable_android_id: the same at every start)")
    a.invoke("static", M("Landroid/util/Log;", "i", f"({STR}{STR})I"), [0, 1])
    a.label("answer")
    a.const_string(0, android_id)
    a.ret("object", 0)
    a.label("forward")
    a.invoke("static", M(SECURE_T, *GET_STRING), [2, 3])
    a.move_result("object", 0)
    a.ret("object", 0)
    return dexgen.Method(*GET_STRING, ACC_PUBLIC | ACC_STATIC, 4, 2, a)


def _clinit(names: list[str]) -> dexgen.Method:
    """Copy each constant's value from Settings.Secure (reflection; one that is missing or of another type is
    skipped). ANDROID_ID is set first, so it never depends on the copy."""
    cls, fld = "Ljava/lang/Class;", "Ljava/lang/reflect/Field;"
    get_field = M(cls, "getField", f"({STR}){fld}")
    a = Asm()
    a.const_string(3, KEY)
    a.sput("object", 3, F(HELPER_T, "ANDROID_ID", STR))
    a.const_class(0, SECURE_T)
    a.const_class(1, HELPER_T)
    a.const16(2, len(names))
    a.new_array(2, 2, f"[{STR}")
    for i, name in enumerate(names):
        a.const16(3, i)
        a.const_string(4, name)
        a.aput_object(4, 2, 3)
    a.array_length(6, 2)
    a.const4(7, 0)
    a.label("loop")
    a.if_ge(7, 6, "done")
    a.aget_object(3, 2, 7)
    a.label("try")
    a.invoke("virtual", get_field, [0, 3])
    a.move_result("object", 4)
    a.invoke("virtual", get_field, [1, 3])
    a.move_result("object", 5)
    a.const4(8, 0)
    a.invoke("virtual", M(fld, "get", "(Ljava/lang/Object;)Ljava/lang/Object;"), [4, 8])
    a.move_result("object", 4)
    a.invoke("virtual", M(fld, "set", "(Ljava/lang/Object;Ljava/lang/Object;)V"), [5, 8, 4])
    a.label("next")
    a.add_int_lit8(7, 7, 1)
    a.goto("loop")
    a.label("done")
    a.ret("void")
    a.try_range("try", "next", "next")
    return dexgen.Method("<clinit>", "()V", ACC_STATIC | ACC_CONSTRUCTOR, 9, 3, a)


def helper_dex(android_id: str, methods: set[tuple[str, str]] = frozenset(),
               fields: dict[str, str] | None = None) -> bytes:
    """classesN.dex with `android.provider.Settings$Securf`: METHODS + `methods` forwarded, FIELDS + `fields` copied,
    getString answering "android_id" with `android_id`."""
    if not re.fullmatch(r"[0-9a-f]{16}", android_id):
        raise ValueError("android_id must be 16 hex digits")
    all_fields = {name: FIELD_TYPES.get(name, STR) for name in FIELDS}
    all_fields.update(fields or {})
    all_methods = sorted(set(METHODS) | set(methods))
    cls = dexgen.ClassDef(HELPER_T)
    cls.static_fields = [(n, t, ACC_PUBLIC | ACC_STATIC) for n, t in sorted(all_fields.items())]
    cls.static_fields.append(("logged", "Z", ACC_PRIVATE | ACC_STATIC))
    cls.methods = [_get_string(android_id) if m == GET_STRING else _forward(*m) for m in all_methods]
    cls.methods.append(_clinit(sorted(all_fields)))
    return dexgen.build([cls])


def _keystore(package: str) -> bytes:
    from ...apk import sign

    try:
        return sign.ensure_keystore(package).read_bytes()
    except OSError:
        return b""


def _dex_names(ws) -> list[str]:
    return sorted((n for n in ws.names() if re.fullmatch(r"classes\d*\.dex", n)), key=lambda n: int(n[7:-4] or 1))


class StableAndroidId(Patch):
    id = "frame.stable_android_id"
    title = "Keep the same Android device id between starts"
    description = ("Lepton throws the container's /data away at every exit, so Android makes up a new app-specific "
                   "device id (ANDROID_ID, which Unity's SystemInfo.deviceUniqueIdentifier comes from) at every "
                   "start. Games that encrypt or file their saves with it can't read them at the next start (for "
                   "example RUNNER: 'CryptographicException: Padding is invalid'). Gives the game a fixed id "
                   "instead, different for each game and each FramePort user: libunity.so and the app's Java code "
                   "read Settings.Secure through a small forwarding class (an extra classes.dex) that answers "
                   "android_id with it.")
    order = 46
    needs_vr = False
    a32 = True  # Java + a string in libunity.so: the same on a 32-bit game

    def detect(self, a):
        if "libunity.so" in a.libs:
            return Suggestion(True, "Unity game: Unity's device id comes from Android's app-specific id, which "
                                    "Lepton makes up anew at every start; saves or logins tied to it break.")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        changed: list[str] = []
        fields: dict[str, str] = {}
        methods: set[tuple[str, str]] = set()
        for name in sorted(n for n in ws.names() if UNITY_LIB.fullmatch(n)):
            lib = ws.read(name)
            fixed = redirect_native(lib)
            if fixed:
                fields.update((f, FIELD_TYPES.get(f, STR)) for f in native_fields(lib))
                ws.put(name, fixed)
                changed.append(name)
        dexes = _dex_names(ws)
        for name in dexes:
            data = ws.read(name)
            if SECURE_T.encode() not in data:
                continue
            dex = Dex(data)
            refs = dex_refs(dex)
            if refs is None or not redirect_dex(dex):
                ctx.checks.append(("Stable Android id", None, f"{name}: Settings.Secure left as it is"))
                continue
            methods |= refs[0]
            fields.update(refs[1])
            ws.put(name, dex.finish())
            changed.append(name)
        if not changed:
            return False
        package = ctx.analysis.package
        android_id = stable_id(package, _keystore(package))
        helper = f"classes{len(dexes) + 1}.dex" if dexes else "classes.dex"
        ws.put(helper, helper_dex(android_id, methods, fields))
        ctx.notes.append(f"android_id is always {android_id} ({helper}: {HELPER.replace('/', '.')}; "
                         f"Settings.Secure redirected in {', '.join(changed)})")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        if any(HELPER_T.encode() in ws.read(n) for n in _dex_names(ws)):
            return [("Stable Android id", True, f"Settings.Secure reads go through {HELPER.replace('/', '.')}")]
        return []


register(StableAndroidId)
