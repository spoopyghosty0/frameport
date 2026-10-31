"""Inspect an APK and describe everything the patch suggestions depend on."""
from __future__ import annotations

import json
import logging
import re
import zipfile
from pathlib import Path

from ..apk import axml
from ..core.models import Analysis
from . import elf, static_openxr
from .luajit import xlua_47bit, xlua_sha256
from .unity_split import split_build as unity_split_build

logging.getLogger("pyaxmlparser").setLevel(logging.ERROR)

UNITY_GGM = "assets/bin/Data/globalgamemanagers"
IL2CPP_METADATA = "assets/bin/Data/Managed/Metadata/global-metadata.dat"

# The version of what analyze() stores (Analysis.extra["analysis_version"]). BUMP IT whenever analyze() gains a field
# or detects something differently that patches/heuristics read: library entries analysed by an older FramePort are
# then analysed again in the background at the next start (pipeline.refresh_analyses), so their new patches are
# offered (GitHub #104: entries from before `sdl_java` never got frame.sdl_clipboard). Entries without it are 0.
# 1: sdl_java, min_sdk, web_wrapper, expects_obb, vr_activity, unity_version (2026-10)
# 2: unity_split (a Unity split-binary build expects an OBB too: expects_obb) (2026-10)
# 3: unreal_ovrp_lookups (the OVRPlugin functions Unreal's Oculus module looks up: frame.unreal_ovrp_entrypoints)
# 4: vivox_api31 (Vivox's audio routing calls Android 12 AudioManager methods: frame.vivox_audio_route) (2026-10)
# 5: unreal_quest_gates (Quest-only branches in ILMxLAB's Unreal: frame.unreal_quest_precompile/_keymap) (2026-10)
# 6: gl_multiview_libs (own-engine libraries with OVR_multiview GLSL: frame.gl_multiview_fbo) (2026-10)
# 7: unreal_thumb_touch (UE4 OculusInput's ThumbUp from near-touch, matched exactly: frame.unreal_thumb_touch)
# 8: media_codec (plays video through Android's decoders: MediaCodec/ExoPlayer/Media3 or VLC: frame.hw_video_decode)
# 9: gamepad (the manifest declares gamepad support: android.hardware.gamepad / Android TV's LEANBACK_LAUNCHER:
#    device.steam_gamepad) (2026-10)
# 10: godot_clipboard (Godot 4.2-4.4's non-null ClipboardManager cast: frame.godot_clipboard) (2026-10)
# 11: unreal_verify_obb (Unreal's bVerifyOBBOnStartUp: frame.unreal_skip_obb_check, GitHub #159) (2026-10)
# 12: eos_keychain (Epic Online Services' Java keychain: frame.eos_keychain_loop, GitHub #214); CryEngine also from
#     libAndroidLauncher.so + a .cryproject (Climb 1 has no libCrySystem.so, GitHub #238) (2026-10)
# 13: validate monolithic CryEngine projects/config, OBB filename formats and foveation cvar (The Climb, GitHub #238)
# 14: bhaptics_permission (bHaptics asks for Android 12's BLUETOOTH_CONNECT: frame.bhaptics_permission, GitHub #235)
# 15: verified xLua/LuaJIT 47-bit lightuserdata engine (frame.luajit_pointers, GitHub #257)
# 16: unreal_obb_delivery (Java expansionFilesDelivered gate, GitHub #283) (2026-10)
# 17: headset (the Meta headset a VR app was made for: Gear VR / Go games run through frame.a32_bridge) (2026-10)
# 18: headset "quest" also for 32-bit apps that require 6DoF head tracking (Santa Cruz builds, e.g. Sports
#     Scramble: Touch controllers on the a32 bridge) (2026-10)
# 19: unreal_obb_delivery also for obfuscated DownloaderActivity predicates (GitHub #159) (2026-10)
# 20: google_vr (Google Daydream / Cardboard builds: GVR or Cardboard SDK, no path to the Frame's headset,
#     GitHub #335) (2026-10)
# 21: photon_voice_api31 (Photon Voice's AudioInAEC calls Android 12 AudioManager methods:
#     frame.photon_voice_audio_route, GitHub #362/#325); cortopia_org_id (Cortopia's Meta start-up waits for
#     Users.GetOrgScopedID: frame.cortopia_org_id, GitHub #360); unity_appui_vibrator (Unity App UI's activity uses
#     Android 12's VibratorManager: frame.unity_appui_vibrator, GitHub #297); gms_folded_sdk_checks (Play services'
#     Android 12 checks folded to true by R8: frame.gms_platform_version, GitHub #310); usb_midi_watcher (kshoji's
#     USB MIDI driver polls the missing USB service: frame.usb_midi_watcher, GitHub #297) (2026-10)
# 22: unreal_binned3 (Unreal built with FMallocBinned3 reserves tens of GiB at load: frame.unreal_noreserve,
#     GitHub #349) (2026-10)
# 23: separate_process (MAIN activities in another process than the main one, which Lepton doesn't watch:
#     frame.main_process, GitHub #178) (2026-10)
# 24: statically linked Khronos OpenXR loaders (frame.static_openxr_loader, GitHub #254) (2026-10)
# 25: meta_depth_occlusion (Unity games with Meta's depth-occlusion keywords: frame.meta_depth_occlusion, PR #246)
#     (2026-10)
# 26: browser_query (the manifest's <queries> look for a web browser: frame.browser_handoff, GitHub #466) (2026-10)
# 27: uses_libraries (the original manifest's <uses-library> names, which OVRPort removes:
#     frame.keep_system_libraries, GitHub #484) (2026-10)
# 28: vr_activity counts an INFO activity as the start activity (a single MAIN + INFO + VR activity was taken for
#     a VR part behind a flat launcher, GitHub #476) (2026-10)
# 29: fmod_record_ring (a Unity game that records the microphone through FMOD's 4-buffer OpenSL ring:
#     frame.unity_mic_ring) (2026-10)
ANALYSIS_VERSION = 29  # (cortopia_org_id's TMNT layout, GitHub #422, needed no re-analysis: see the patch)


# Android versions by API level (for messages); the Frame's Lepton container runs Android 11 (API 30)
ANDROID_VERSIONS = {29: "10", 30: "11", 31: "12", 32: "12L", 33: "13", 34: "14", 35: "15", 36: "16", 37: "17"}
FRAME_API = 30
# Quest games declare up to API 32 (Quest's Android 12L) and run on the Frame (OVRPort lowers minSdk to 29; they
# don't call newer Android classes). From API 33 on, apps call Android 13+ classes at start (GitHub #71/#72: minSdk
# 34, NoClassDefFoundError android/window/OnBackInvokedCallback, NoSuchMethodError VarHandle.storeStoreFence).
MAX_RUNNABLE_MIN_SDK = 32


def android_version(api: int) -> str:
    """'14' for API 34."""
    return ANDROID_VERSIONS.get(api, f"API {api}")


def too_new_android(min_sdk: int | None) -> bool:
    """The APK needs a newer Android than the Frame's Lepton (11): it crashes at start on missing Android classes."""
    return bool(min_sdk) and min_sdk > MAX_RUNNABLE_MIN_SDK


TWA_URL_KEY = "android.support.customtabs.trusted.DEFAULT_URL"
TWA_ACTIVITY = "com.google.androidbrowserhelper.trusted.LauncherActivity"
UE_OBB_KEY = ".GameActivity.bHasOBBFiles"  # com.epicgames.ue4.… (UE4) / com.epicgames.unreal.… (UE5)


def expects_obb(meta: dict) -> bool:
    """Unreal packaged the game's content as an OBB (expansion file) and opens it at start."""
    return any(k.endswith(UE_OBB_KEY) and v in (True, "true", "True") for k, v in meta.items())


def cryengine_project(data: bytes) -> bool:
    """Monolithic CryEngine builds ship game.cryproject instead of a separate libCrySystem.so."""
    try:
        project = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(project, dict) and project.get("type") == "CRYENGINE Project"


UE_VERIFY_OBB_KEY = ".GameActivity.bVerifyOBBOnStartUp"


def unreal_binned3(engine: bytes) -> bool:
    """Unreal built with FMallocBinned3 as its allocator (its own messages and source path are compiled in; other
    builds only name its console variables). It reserves its whole range while the library loads (GitHub #349)."""
    return (b"/HAL/MallocBinned3.cpp\0" in engine
            or "FMallocBinned3 Attempt to free".encode("utf-16-le") in engine)


def cortopia_org_id(il2cpp_meta: bytes | None) -> bool:
    """Cortopia's Meta platform start-up awaits an org-scoped user id OVRPort never answers (frame.cortopia_org_id):
    its OculusPlatform in Cortopia.Scripts.Platform.Meta (Escaping Wonderland, GitHub #360) or directly in
    Cortopia.Scripts.Platform (TMNT: Empire City, GitHub #422)."""
    m = il2cpp_meta or b""
    return b"\0GetOrgScopedID\0" in m and (b"\0Cortopia.Scripts.Platform.Meta\0" in m or (
        b"\0Cortopia.Scripts.Platform\0" in m and b"\0OculusPlatform\0" in m))


def unreal_verify_obb(meta: dict) -> bool:
    """Unreal's DownloaderActivity checks every OBB entry's CRC at the first start (a full read; on a mismatch it waits
    on an error screen). Only a real boolean counts: GameActivity reads it with Bundle.getBoolean."""
    return any(k.endswith(UE_VERIFY_OBB_KEY) and v is True for k, v in meta.items())


def _read_manifest_info(path: Path):
    """(package, versionName, label, main activity, pyaxmlparser APK)."""
    from pyaxmlparser import APK

    apk = APK(str(path))
    label = apk.application or apk.package
    return apk.package, apk.version_name or "", label, apk.get_main_activity(), apk


def _resolve_string(apk, value) -> str | None:
    """A meta-data value: the string itself, or a @string/… reference resolved through resources.arsc."""
    if isinstance(value, str):
        return value
    if apk is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    try:
        res = apk.get_android_resources()
        for _config, v in (res.get_resolved_res_configs(value) if res else ()):
            if isinstance(v, str) and v:
                return v
    except Exception:  # noqa: BLE001 - a broken resource table only loses the URL
        pass
    return None


def web_wrapper(meta: dict, manifest_strings: list[str], apk=None) -> dict | None:
    """A Trusted Web Activity (Bubblewrap, Meta's PWA packaging): the APK only opens a website in a browser app
    (Meta's com.oculus.browser on Quest), which Lepton doesn't have (GitHub #86). {"url": str | None} or None."""
    if TWA_URL_KEY not in meta and TWA_ACTIVITY not in manifest_strings:
        return None
    url = _resolve_string(apk, meta.get(TWA_URL_KEY))
    return {"url": url if url and url.startswith(("https://", "http://")) else None}


def _unreal_version(z: zipfile.ZipFile, lib: str) -> str | None:
    """'4.20' from the '++UE4+Release-4.20' build string (streamed; engine libs can be >1 GB)."""
    tail = b""
    with z.open(lib) as f:
        while True:
            chunk = f.read(1 << 24)
            if not chunk:
                return None
            m = re.search(rb"\+\+UE[45]\+Release-([0-9]+\.[0-9]+)", tail + chunk)
            if m:
                return m.group(1).decode()
            tail = chunk[-64:]


def _features(manifest: bytes) -> dict[str, bool]:
    """uses-feature name -> required (android:required defaults to true)."""
    x = axml.Axml(manifest)
    names = x.strings()
    out = {}
    for el in x.elements():
        if el.name != "uses-feature":
            continue
        name = x.attr_str(el, "name")
        if not name:
            continue
        required = True
        for a in el.attrs:
            if a.name < len(names) and names[a.name] == "required":
                required = a.value != 0
        out[name] = required
    return out


def _split_base(manifest: bytes) -> bool:
    """The base of a split APK set (Play "app bundle" installs): android:isSplitRequired is true or the manifest
    names requiredSplitTypes."""
    try:
        return bool(axml.Axml(manifest).get_bool("application", "isSplitRequired")) or axml.has_attr(
            manifest, "manifest", "requiredSplitTypes")
    except Exception:  # noqa: BLE001 - an unusual manifest isn't a split base
        return False


def requires_6dof(manifest: bytes) -> bool:
    """The app requires 6DoF head tracking (uses-feature android.hardware.vr.headtracking, version 1, required): Meta's
    Quest declaration, which Gear VR and Go apps don't make."""
    x = axml.Axml(manifest)
    names = x.strings()
    for el in x.elements():
        if el.name != "uses-feature" or x.attr_str(el, "name") != "android.hardware.vr.headtracking":
            continue
        attrs = {names[a.name]: a.value for a in el.attrs if a.name < len(names)}
        if attrs.get("required", 1) != 0 and (attrs.get("version") or 0) >= 1:
            return True
    return False


def vr_kind(libs: set[str], manifest_strings: list[str]) -> str:
    """What kind of Android app this is, for choosing how to port it:
    quest       Meta Quest app (VrApi / OVRPlugin / Meta's OpenXR loader + Oculus manifest entries): OVRPort
    openxr      another headset's OpenXR app (Pico, Khronos loader): OVRPort's loader + the Frame adapter
    android_xr  Android XR (Jetpack XR / spatial) app: needs system services Lepton doesn't have
    pico_sdk    Pico's pre-OpenXR SDK, wave: HTC Vive Wave SDK: no OpenXR path, can't run
    none        an ordinary (2D) Android app or game: installed without the VR translation"""
    text = " ".join(manifest_strings)
    meta = (libs & {"libvrapi.so", "libOVRPlugin.so", "libovrplatformloader.so"}
            or "com.oculus." in text or "oculus.software." in text or "horizonos." in text)
    if meta:
        return "quest"
    if any(lib.startswith("libwvr") for lib in libs) or "com.htc.vr" in text:
        return "wave"
    if "libopenxr_loader.so" in libs or "org.khronos.openxr" in text:
        return "openxr"
    if libs & {"libPvr_UnitySDK.so", "libpxr_api.so", "libPxr_api.so", "libPvrSDK.so"} or "pvr.app.type" in text:
        return "pico_sdk"
    if "android.software.xr" in text or "androidx.xr" in text or "com.google.android.xr" in text:
        return "android_xr"
    return "none"


# Google VR (Daydream / Cardboard): the manifest categories of a Daydream / Cardboard app, Daydream's VR service
# (android:enableVrMode="com.google.vr.vrcore/..."), the GVR SDK's library and the Cardboard SDK's
# (github.com/googlevr/cardboard), and the GVR SDK's Java classes. Not libgvraudio.so / Unity's
# libaudioplugingvrunity.so: Google's spatial audio, which Quest games ship too (e.g. Shadow Point).
DAYDREAM_CATEGORY = "com.google.intent.category.DAYDREAM"
CARDBOARD_CATEGORY = "com.google.intent.category.CARDBOARD"
GOOGLE_VR_LIBS = {"libgvr.so", "libcardboard_api.so", "libGfxPluginCardboard.so"}
GVR_DEX_MARKERS = (b"Lcom/google/vr/ndk/base/GvrLayout;", b"Lcom/google/vr/sdk/base/GvrView;",
                   b"Lcom/google/vr/sdk/base/GvrActivity;", b"Lcom/google/cardboard/sdk/")


def google_vr(libs: set[str], cats: set[str], manifest_strings: list[str], gvr_java: bool = False,
              boot: str = "") -> str:
    """A Google VR app: "daydream" (Daydream: needs Google's VR services, VrCore), "cardboard" (Cardboard: draws
    both eyes side by side with lens distortion onto a phone screen) or "" (neither). Neither has a path to the
    Frame's headset: a Lepton app only reaches it through OpenXR (VrApi / OVRPlugin games are translated to it).
    `boot` = Unity's boot.config (vr-device-list=, where Unity builds list their VR devices)."""
    devices = next((ln.split("=", 1)[1].lower() for ln in boot.splitlines() if ln.startswith("vr-device-list=")), "")
    if (DAYDREAM_CATEGORY in cats or any(s.startswith("com.google.vr.vrcore") for s in manifest_strings)
            or "daydream" in devices):
        return "daydream"
    if CARDBOARD_CATEGORY in cats or "cardboard" in devices or libs & GOOGLE_VR_LIBS or gvr_java:
        return "cardboard"
    return ""


# Kotlin's message for a failed non-null cast to ClipboardManager (Godot 4.2-4.4; 4.1 is Java, 4.5+ uses `as?`)
GODOT_CLIPBOARD_CAST = b"null cannot be cast to non-null type android.content.ClipboardManager"


def headset(meta: dict, abis: list[str], kind: str, six_dof: bool = False) -> str:
    """The Meta headset a VR app was made for: "quest", "go" (Oculus Go), "gearvr" (Samsung Gear VR, which most Go
    games also kept) or "" (not a Meta VR app). Quest games list their devices in com.oculus.supportedDevices or
    require 6DoF head tracking (six_dof: 32-bit Santa Cruz builds, which also kept Samsung's VR mode); Gear VR and Go
    games declare Samsung's VR mode and only ship 32-bit code (the Quest needs arm64)."""
    if kind != "quest":
        return ""
    devices = str(meta.get("com.oculus.supportedDevices") or "").lower()
    if "quest" in devices or "eureka" in devices or "arm64-v8a" in abis or six_dof:
        return "quest"
    if "go" in devices.split("|") or "pacific" in devices:
        return "go"
    if meta.get("com.samsung.android.vr.application.mode") or "armeabi-v7a" in abis:
        return "gearvr"
    return "quest"


# dex type names of Android's video decoding APIs (frame.hw_video_decode)
MEDIA_CODEC_MARKERS = (b"Landroid/media/MediaCodec;", b"Landroidx/media3/exoplayer", b"Lcom/google/android/exoplayer2/")

# OpenXR composition-layer extensions the Frame runtime lacks (see docs/FRAME_RUNTIME.md).
LAYER_EXTENSIONS = ("XR_KHR_composition_layer_cylinder", "XR_KHR_composition_layer_equirect",
                    "XR_KHR_composition_layer_equirect2", "XR_KHR_composition_layer_cube")


def analyze(path: Path, deep: bool = True, data_bytes: int | None = None) -> Analysis:
    path = Path(path)
    package, version, label, activity, apk_info = _read_manifest_info(path)
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        abis = sorted({n.split("/")[1] for n in names if n.startswith("lib/") and n.count("/") >= 2})
        abi = next((a for a in ("arm64-v8a", "armeabi-v7a") if a in abis), None)
        prefix = f"lib/{abi}/" if abi else None
        libs = sorted(n[len(prefix):] for n in names if prefix and n.startswith(prefix) and n.endswith(".so"))
        # SDL's Java side (SDL2 / LÖVE apps): crashes in Lepton without a clipboard service (frame.sdl_clipboard)
        dexes = [z.read(n) for n in names if n.startswith("classes") and n.endswith(".dex")]
        sdl_java = any(b"Lorg/libsdl/app/SDLClipboardHandler;" in d for d in dexes)
        # Godot 4.2-4.4's Kotlin `as ClipboardManager` (non-null cast): the same crash (frame.godot_clipboard)
        godot_clipboard = any(b"Lorg/godotengine/godot/Godot;" in d and GODOT_CLIPBOARD_CAST in d for d in dexes)
        # Vivox voice chat calling Android 12 audio-routing methods: crashes in Lepton (Android 11)
        vivox_api31 = any(b"Lcom/vivox/sdk/AudioChangeListener;" in d and b"CommunicationDevice" in d for d in dexes)
        # Photon Voice's microphone plugin does the same (frame.photon_voice_audio_route, GitHub #362)
        photon_voice_api31 = any(b"Lcom/exitgames/photon/audioinaec/AudioInAEC;" in d and b"CommunicationDevice" in d
                                 for d in dexes)
        # Unity App UI's activity gets Android 12's VibratorManager in onCreate: crashes in Lepton (Android 11)
        unity_appui_vibrator = any(b"Lcom/unity3d/player/appui/" in d and b"Landroid/os/VibratorManager;" in d
                                   and b"vibrator_manager" in d for d in dexes)
        # kshoji's USB MIDI driver polls Android's USB service, which Lepton lacks (frame.usb_midi_watcher)
        usb_midi_watcher = any(b"Ljp/kshoji/driver/midi/device/MidiDeviceConnectionWatcher"
                               b"$MidiDeviceConnectionWatchThread;"
                               in d for d in dexes)
        # Play services' PlatformVersion.isAtLeastS() & co. folded to `return true` (minSdk 31+ builds): crash on
        # Android 11 (frame.gms_platform_version)
        from ..patches.frame.gms_platform_version import has_folded_checks

        gms_folded_sdk_checks = has_folded_checks(dexes)
        # Epic Online Services' keychain (it can retry without end when it can't write: frame.eos_keychain_loop)
        eos_keychain = any(b"Lcom/epicgames/mobile/eossdk/EOSSDK;" in d and b"resetKeychain" in d for d in dexes)
        # bHaptics asks for an Android 12 permission; the prompt interrupts the game (frame.bhaptics_permission)
        bhaptics_permission = any(b"Lcom/bhaptics/commons/PermissionUtils;" in d and b"requestBluetoothPermission" in d
                                  for d in dexes)
        # QuestCraft's pojlib downloads its Java runtime only on Wi-Fi; Lepton reports Ethernet
        # (frame.pojlib_connection; no ANALYSIS_VERSION bump: the patch also applies to QuestCraft's package)
        pojlib_connection = any(b"Lpojlib/API;" in d and b"hasConnection" in d for d in dexes)
        # video through Android's decoders (frame.hw_video_decode): Java MediaCodec, ExoPlayer/Media3, or libVLC
        media_codec = any(m in d for d in dexes for m in MEDIA_CODEC_MARKERS) or "libvlc.so" in libs
        # Google VR's Java side (GvrLayout / GvrView, the Cardboard SDK): a Daydream / Cardboard app (google_vr)
        gvr_java = any(m in d for d in dexes for m in GVR_DEX_MARKERS)
        from ..patches.frame.unity_mic_ring import record_ring
        from ..patches.frame.unreal_obb_check import has_delivery_check

        # Includes obfuscated DownloaderActivity predicates; CRC verification is a separate manifest switch.
        unreal_obb_delivery = any(has_delivery_check(d, package) for d in dexes)
        del dexes
        manifest = z.read("AndroidManifest.xml")
        lib_bytes = {}
        if deep and prefix:
            for lib in libs:
                # only the libraries that drive decisions (all engine libs can be hundreds of MB)
                if lib in ("libovrplatformloader.so",) or not lib.startswith(("libopenxr_loader", "libfrda")):
                    info = z.getinfo(prefix + lib)
                    if info.file_size < 400 * 2**20:
                        lib_bytes[lib] = z.read(info)
        engine_lib = next((prefix + n for n in ("libUE4.so", "libUnreal.so") if prefix and prefix + n in names), None)
        unreal_version = _unreal_version(z, engine_lib) if deep and engine_lib else None
        boot = (z.read("assets/bin/Data/boot.config").decode("utf-8", "replace")
                if "assets/bin/Data/boot.config" in names else "")
        ggm = z.read(UNITY_GGM) if deep and UNITY_GGM in names else None
        il2cpp_meta = z.read(IL2CPP_METADATA) if deep and IL2CPP_METADATA in names and "libil2cpp.so" in libs else None
        # a Unity split build: the rest of the game is in a zip OBB (analysis/unity_split.py)
        unity_split = "libunity.so" in libs and unity_split_build(z, names, ggm, deep)
        cry_project = any(cryengine_project(z.read(n)) for n in names if n.endswith(".cryproject"))
        system_cfg = (z.read("assets/system.cfg").decode("utf-8", "replace")
                      if "assets/system.cfg" in names else "")

    libset = set(libs)
    engine = ("Unreal" if libset & {"libUE4.so", "libUnreal.so"} else "Unity" if "libunity.so" in libset
              else "CryEngine" if "libCrySystem.so" in libset or cry_project else "Other")
    static_loaders = [n for n, d in lib_bytes.items() if static_openxr.detected(n, d)] if abi == "arm64-v8a" else []
    has_openxr, has_vrapi = bool(static_loaders) or "libopenxr_loader.so" in libset, "libvrapi.so" in libset
    xr = "OpenXR+VrApi" if has_openxr and has_vrapi else "OpenXR" if has_openxr else "VrApi" if has_vrapi else "?"
    is_overport = "libopenxr_loader_generic.so" in libset or "liboverport.config.so" in libset
    # VrApi called directly by the engine (no OVRPlugin): overport cannot translate it.
    direct_vrapi = has_vrapi and "libOVRPlugin.so" not in libset
    if is_overport:  # overport adds OVRPlugin to direct-VrApi games; judge by the engine lib instead
        direct_vrapi = any("libvrapi.so" in elf.needed(b) for n, b in lib_bytes.items()
                           if elf.is_elf(b) and n not in ("libOVRPlugin.so", "libvrapi.so"))
    vulkan_declared = b"android.hardware.vulkan" in manifest or _uses_feature(manifest, "android.hardware.vulkan")
    if vulkan_declared:
        graphics = "Vulkan (declared in manifest)"
    elif engine == "Unity" and "vulkan" in boot.lower():
        graphics = "Vulkan (Unity boot.config)"
    elif engine == "CryEngine" and re.search(r'(?im)^\s*r_Driver\s*=\s*"?vk"?\s*(?:;.*)?$', system_cfg):
        graphics = "Vulkan (CryEngine system.cfg)"
    else:
        graphics = "GLES or unknown (no Vulkan declaration)"

    uses_glad = False
    oculus_os_refs = []
    layer_exts = set()
    for name, data in lib_bytes.items():
        if not elf.is_elf(data):
            continue
        if b"com/oculus/os/AnalyticsEvent" in data:
            oculus_os_refs.append(name)
        if name != "libOVRPlugin.so":  # OVRPlugin lists every layer extension; only the game's own requests count
            layer_exts |= {ext for ext in LAYER_EXTENSIONS if ext.encode() + b"\0" in data}
        if (name not in ("libvrapi.so", "libOVRPlugin.so") and b"GLAD_GL_" in data
                and "eglGetProcAddress" in elf.dyn_symbols(data, False)):
            uses_glad = True

    msaa_levels = 0
    if ggm is not None:
        try:
            from ..patches.frame.unity_no_msaa import count_msaa_levels

            msaa_levels = count_msaa_levels(ggm)
        except Exception:
            msaa_levels = 0

    cats = axml.categories(manifest)
    manifest_strings = axml.Axml(manifest).strings()
    features = _features(manifest)
    used_perms, _ = axml.used_and_declared_permissions(manifest)
    meta = axml.meta_data(manifest)
    return Analysis(
        package=package,
        version=version,
        label=label,
        abis=abis,
        engine=engine,
        xr=xr,
        graphics=graphics,
        direct_vrapi=direct_vrapi,
        libs=libs,
        launcher_activity=activity,
        has_info_category=axml.INFO in cats and axml.LAUNCHER not in cats,
        meta_permissions=axml.undeclared_meta_permissions(manifest),
        uses_glad_gl=uses_glad,
        unity_msaa_levels=msaa_levels,
        oculus_os_classes=bool(oculus_os_refs),
        is_overport_output=is_overport,
        debuggable=bool(axml.Axml(manifest).get_bool("application", "debuggable")),
        extra={
            "static_openxr_loader": static_loaders,
            "analysis_version": ANALYSIS_VERSION,
            "xlua_47bit": xlua_47bit(lib_bytes.get("libxlua.so", b"")),
            "xlua_sha256": xlua_sha256(lib_bytes.get("libxlua.so", b"")),
            "frame_patched": "libframe_settings.so" in libset,  # already has FramePort's FrameBridge adapter
            # Team Beef's TBXR ports pick their OpenXR path by headset maker (frame.tbxr_vendor): their libraries
            # the game asks Meta's platform for asset files (content shipped as separate files, frame.asset_files)
            "asset_file_api": any(b"\0ovr_AssetFile_GetList\0" in d for n, d in lib_bytes.items()
                                  if n != "libovrplatformloader.so"),
            "tbxr_libs": sorted(n for n, d in lib_bytes.items() if b"\0OPENXR_HMD\0" in d and b"\0meta\0" in d),
            "missing_ovr_symbols": sorted(missing_ovr_symbols(lib_bytes)), "size": path.stat().st_size,
            # the game sizes Meta's microphone buffer (Unreal's Oculus voice): OVRPort crashes there
            # (frame.ovr_microphone)
            # (not libil2cpp.so: Unity's C# platform wrapper names every function, used or not; a Unity game that
            # does crash there is found by triage ovr-microphone-crash)
            "ovr_microphone": any(b"ovr_Microphone_GetOutputBufferMaxSize\0" in d for n, d in lib_bytes.items()
                                  if not n.startswith("libovrplatformloader") and n != "libil2cpp.so"),
            "data_bytes": data_bytes or 0,
            "features": features,
            "meta_permissions_used": sorted(p for p in used_perms
                                            if p.startswith(("com.oculus.permission.", "horizonos."))),
            # mixed-reality-only: passthrough required and no guardian (Meta's BOUNDARYLESS_APP)
            "mr_only": (features.get("com.oculus.feature.PASSTHROUGH", False)
                        and "com.oculus.feature.BOUNDARYLESS_APP" in features),
            "hand_tracking_only": features.get("oculus.software.handtracking", False),
            "unreal_version": unreal_version,
            "oculus_os_refs": sorted(oculus_os_refs),
            "xr_layer_exts": sorted(layer_exts),  # composition-layer extensions the game's own libraries request
            "vr_kind": ("openxr" if static_loaders
                        and vr_kind(libset, manifest_strings) == "none" else vr_kind(libset, manifest_strings)),
            "headset": headset(meta, abis, vr_kind(libset, manifest_strings), requires_6dof(manifest)),
            # a Google Daydream / Cardboard build (GVR / Cardboard SDK): no path to the Frame's headset (GitHub #335)
            "google_vr": google_vr(libset, cats, manifest_strings, gvr_java, boot),
            # the base of a split APK set (Play "app bundle" installs): the code/libraries live in split APKs
            # (the attributes, not their strings: a base merged from its bundle keeps the strings in its pool)
            "split_apk": _split_base(manifest),
            # Unity (IL2CPP) text fields: they close at once on the Frame (frame.unity_text_input)
            "text_fields": unity_text_fields(il2cpp_meta) if il2cpp_meta else [],
            # Meta's OVRManager raises MSAA at runtime (frame.unity_runtime_msaa_off); Oculus XR Plugin (multiview)
            "ovr_runtime_msaa": bool(il2cpp_meta) and b"\0useRecommendedMSAALevel\0" in il2cpp_meta,
            # Cortopia's Meta platform start-up awaits an org-scoped user id OVRPort never answers
            # (frame.cortopia_org_id, GitHub #360)
            "cortopia_org_id": cortopia_org_id(il2cpp_meta),
            "sdl_java": sdl_java,
            "godot_clipboard": godot_clipboard,
            "vivox_api31": vivox_api31,
            "photon_voice_api31": photon_voice_api31,
            "unity_appui_vibrator": unity_appui_vibrator,
            "gms_folded_sdk_checks": gms_folded_sdk_checks,
            "usb_midi_watcher": usb_midi_watcher,
            "eos_keychain": eos_keychain,
            "bhaptics_permission": bhaptics_permission,
            "pojlib_connection": pojlib_connection,
            "media_codec": media_codec,
            # declares gamepad support (SDL's manifest template has both): device.steam_gamepad
            "gamepad": "android.hardware.gamepad" in features or axml.LEANBACK_LAUNCHER in cats,
            # the manifest's <queries> look for a web browser (sign-ins; Lepton has none): frame.browser_handoff
            "browser_query": axml.queries_browser(manifest),
            # shared Java libraries the original manifest asks for (OVRPort drops them): frame.keep_system_libraries
            "uses_libraries": axml.uses_libraries(manifest),
            "oculus_xr_plugin": bool(il2cpp_meta) and b"\0m_StereoRenderingModeAndroid\0" in il2cpp_meta,
            "meta_depth_occlusion": (
                bool(il2cpp_meta) and (b"HARD_OCCLUSION" in il2cpp_meta or b"SOFT_OCCLUSION" in il2cpp_meta)
            ),
            # Unity's FMOD records the microphone into a 4-buffer OpenSL ring that Lepton's capture bursts lap
            # (frame.unity_mic_ring); only games that ask for the microphone
            "fmod_record_ring": ("android.permission.RECORD_AUDIO" in used_perms
                                 and record_ring(lib_bytes.get("libunity.so", b"")) == "original"),
            # Unity's built-in Oculus support checks for Meta's system apps before VR (frame.unity_oculus_check)
            "unity_oculus_check": b"\0com.oculus.systemactivities\0" in lib_bytes.get("libunity.so", b""),
            # a 2D launcher activity that starts a separate VR activity (frame.start_activity)
            "vr_activity": axml.vr_activity(manifest),
            # a MAIN activity in its own process: Lepton watches only the main one (frame.main_process)
            "separate_process": axml.separate_process_activities(manifest),
            "unity_version": unity_version(ggm, lib_bytes.get("libunity.so")) if engine == "Unity" else None,
            # the minimum Android version the APK declares (read from the original: OVRPort lowers it to 29)
            "min_sdk": axml.min_sdk(manifest),
            # a website in an Android wrapper (TWA): nothing to port
            "web_wrapper": web_wrapper(meta, manifest_strings, apk_info),
            # Unreal packaged its content as an OBB (expansion file), or a Unity split build keeps all but its first
            # scene in one; CryEngine's launcher formats external OBB paths (including versionless ones).
            # Without it the game hangs or crashes at start (GitHub #85, #92, #238).
            # (not when the APK carries it: older Unreal's "OBB inside the APK", assets/main.obb.png, e.g. Gunjack)
            "expects_obb": ((expects_obb(meta) and "assets/main.obb.png" not in names) or unity_split
                            or engine == "CryEngine" and any(b"\0%s/%s.%i.%s.obb\0" in d or b"\0%s/%s.%s.obb\0" in d
                                                             for d in lib_bytes.values())),
            # The Climb uses the older foveation cvar, not The Climb 2's r_variable_rate_shading.
            "cryengine_vr_foveated": engine == "CryEngine" and any(
                b"\0r_vr_foveated\0" in d for d in lib_bytes.values()),
            "unity_split": unity_split,
            # Unreal CRC-checks the OBB before the game starts (frame.unreal_skip_obb_check, GitHub #159)
            "unreal_verify_obb": unreal_verify_obb(meta),
            "unreal_obb_delivery": unreal_obb_delivery,
            # Unreal's Binned3 allocator reserves more address space than the Frame's kernel grants in one writable
            # mapping (frame.unreal_noreserve, GitHub #349)
            "unreal_binned3": (unreal_binned3(lib_bytes.get(engine_lib.rsplit("/", 1)[1], b""))
                               if engine_lib else False),
            # Unreal's Oculus module needs every OVRPlugin function it looks up (frame.unreal_ovrp_entrypoints)
            "unreal_ovrp_lookups": (ovrp_lookups(lib_bytes.get(engine_lib.rsplit("/", 1)[1], b""))
                                    if engine_lib and "libOVRPlugin.so" in libset else []),
            # Quest-only branches that leave ILMxLAB's Unreal games stuck on the Frame (frame.unreal_quest_*)
            "unreal_quest_gates": (unreal_quest_gates(lib_bytes.get(engine_lib.rsplit("/", 1)[1], b""))
                                   if engine_lib else []),
            # UE4's Oculus input animates the thumb from near-touch, which the Frame never reports
            # (frame.unreal_thumb_touch: True only where its exact code matches)
            "unreal_thumb_touch": (unreal_thumb_touch(lib_bytes.get(engine_lib.rsplit("/", 1)[1], b""))
                                   if engine_lib and engine_lib.startswith("lib/arm64-v8a/") else False),
            # own-engine libraries whose GLSL declares OVR_multiview views (frame.gl_multiview_fbo, e.g. Doom3Quest)
            "gl_multiview_libs": multiview_glsl_libs(lib_bytes) if engine == "Other" else [],
        },
    )


# Quest-only branches in ILMxLAB's Unreal (IsRunningOnSantaCruz) that leave the game stuck on the Frame: the exported
# function each frame.unreal_quest_* patch rewrites
UNREAL_QUEST_GATES = {
    "quest_precompile": "_ZN8UVRUtils31GetQuestShaderPrecompilePercentEv",
    "rpoc_keymap": "_ZN27URPOCKeyMapManagerComponent14AddAxisMappingERK15FRPOCKeyMappingR16FRPOCInputMapSet",
}


# libraries that hold GLSL but aren't the game's renderer
NOT_GL_ENGINE = ("libopenxr", "libOVR", "libovr", "libvrapi", "libfp", "libframe", "libglshim", "libVkLayer", "libc++")


def multiview_glsl_libs(lib_bytes: dict[str, bytes]) -> list[str]:
    """Own-engine libraries with OVR_multiview shaders (`layout(num_views=…) in;` + gl_ViewID_OVR): such an engine may
    also draw them into ordinary framebuffers, which Mesa refuses (frame.gl_multiview_fbo, GitHub #77)."""
    return sorted(n for n, d in lib_bytes.items() if not n.startswith(NOT_GL_ENGINE) and elf.is_elf(d)
                  and b"num_views" in d and b"gl_ViewID_OVR" in d)


UNREAL_THUMB_TOUCH = "_ZN11OculusInput12FOculusInput20SendControllerEventsEv"


def unreal_thumb_touch(data: bytes) -> bool:
    """UE4's OculusInput sets ThumbUp from near-touch in exactly the code frame.unreal_thumb_touch rewrites (or
    already rewrote)."""
    if b"\0" + UNREAL_THUMB_TOUCH.encode() + b"\0" not in data:
        return False
    from ..patches.frame.unreal_thumb_touch import thumb_site

    try:
        return thumb_site(data) is not None
    except Exception:  # noqa: BLE001 - a malformed library: no suggestion
        return False


def unreal_quest_gates(data: bytes) -> list[str]:
    """The gate functions an Unreal engine library exports (a search of the symbol names, no ELF parsing)."""
    return sorted(k for k, sym in UNREAL_QUEST_GATES.items() if b"\0" + sym.encode() + b"\0" in data)


def ovrp_lookups(data: bytes) -> list[str]:
    """The OVRPlugin function names (`ovrp_*` strings) an engine library holds: Unreal's Oculus module looks each one
    up with dlsym in libOVRPlugin.so and gives up on VR when any is missing (FOculusHMDModule::
    InitializeOculusPluginWrapper)."""
    names, at = set(), data.find(b"\0ovrp_")
    while at >= 0:
        end = data.find(b"\0", at + 1)
        if end < 0:
            break
        name = data[at + 1:end]
        if re.fullmatch(rb"ovrp_[A-Za-z0-9_]+", name):
            names.add(name.decode())
        at = data.find(b"\0ovrp_", end)
    return sorted(names)


def unity_text_fields(metadata: bytes) -> list[str]:
    """The Unity text field classes an IL2CPP game contains (names from its global-metadata.dat string table)."""
    out = []
    if b"\0TMP_InputField\0" in metadata:
        out.append("TMP_InputField")
    if b"\0InputField\0" in metadata and b"\0UnityEngine.UI\0" in metadata:  # not the tail of TMP_InputField
        out.append("InputField")
    return out


UNITY_VERSION = re.compile(rb"(?<![\d.])(\d{4}\.\d+\.\d+[abfpx]\d+)")


def unity_version(ggm: bytes | None = None, libunity: bytes | None = None) -> str | None:
    """Unity version (e.g. 6000.2.7f2): from the header of globalgamemanagers, else the version string libunity.so
    repeats most (games packed into data.unity3d have no loose globalgamemanagers)."""
    m = UNITY_VERSION.search(ggm[:512]) if ggm else None
    if m:
        return m.group(1).decode()
    if libunity:
        from collections import Counter

        found = Counter(x.group(1) for x in UNITY_VERSION.finditer(libunity))
        if found:
            return found.most_common(1)[0][0].decode()
    return None


def _uses_feature(manifest: bytes, feature: str) -> bool:
    x = axml.Axml(manifest)
    return any(el.name == "uses-feature" and (x.attr_str(el, "name") or "").startswith(feature) for el in x.elements())


# Platform SDK enum helpers, e.g. ovrPeerConnectionState_ToString (BlazeRush): OVRPort's loader lacks several
OVR_TO_STRING = re.compile(r"^ovr[A-Z][A-Za-z0-9]*_ToString$")


def missing_ovr_symbols(lib_bytes: dict[str, bytes]) -> set[str]:
    """ovr_* / ovrMessageType_* / ovr<Enum>_ToString functions the game imports that the platform loader (+compat/stub
    libs) lacks."""
    loader = lib_bytes.get("libovrplatformloader.so")
    if not loader or not elf.is_elf(loader):
        return set()
    exported = set(elf.dyn_symbols(loader, True))
    linked = set(elf.needed(loader))
    for extra in ("libovrplatformcompat.so", "libovrstubs.so", "libfp_langpack.so"):
        if extra in lib_bytes and extra in linked:  # only counts when the loader loads it (a lone file doesn't help)
            exported |= elf.dyn_symbols(lib_bytes[extra], True)
    wanted = set()
    for name, data in lib_bytes.items():
        if (name.startswith(("libovrplatformloader", "libopenxr_loader", "libframe_settings", "libfrda"))
                or not elf.is_elf(data)):
            continue
        wanted |= {s for s in elf.dyn_symbols(data, False)
                   if s.startswith(("ovr_", "ovrMessageType_")) or OVR_TO_STRING.match(s)}
    return wanted - exported
