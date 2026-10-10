"""Inspect an APK and describe everything the patch suggestions depend on."""
from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

from ..apk import axml
from ..core.models import Analysis
from . import elf
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
ANALYSIS_VERSION = 9


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


# dex type names of Android's video decoding APIs (frame.hw_video_decode)
MEDIA_CODEC_MARKERS = (b"Landroid/media/MediaCodec;", b"Landroidx/media3/exoplayer", b"Lcom/google/android/exoplayer2/")

# OpenXR composition-layer extensions the Frame runtime lacks (see docs/FRAME_RUNTIME.md).
LAYER_EXTENSIONS = ("XR_KHR_composition_layer_cylinder", "XR_KHR_composition_layer_equirect",
                    "XR_KHR_composition_layer_equirect2", "XR_KHR_composition_layer_cube")


def analyze(path: Path, deep: bool = True, data_bytes: int | None = None) -> Analysis:
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        abis = sorted({n.split("/")[1] for n in names if n.startswith("lib/") and n.count("/") >= 2})
        abi = next((a for a in ("arm64-v8a", "armeabi-v7a") if a in abis), None)
        prefix = f"lib/{abi}/" if abi else None
        libs = sorted(n[len(prefix):] for n in names if prefix and n.startswith(prefix) and n.endswith(".so"))
        # SDL's Java side (SDL2 / LÖVE apps): crashes in Lepton without a clipboard service (frame.sdl_clipboard)
        dexes = [z.read(n) for n in names if n.startswith("classes") and n.endswith(".dex")]
        sdl_java = any(b"Lorg/libsdl/app/SDLClipboardHandler;" in d for d in dexes)
        # Vivox voice chat calling Android 12 audio-routing methods: crashes in Lepton (Android 11)
        vivox_api31 = any(b"Lcom/vivox/sdk/AudioChangeListener;" in d and b"CommunicationDevice" in d for d in dexes)
        # video through Android's decoders (frame.hw_video_decode): Java MediaCodec, ExoPlayer/Media3, or libVLC
        media_codec = any(m in d for d in dexes for m in MEDIA_CODEC_MARKERS) or "libvlc.so" in libs
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

    package, version, label, activity, apk_info = _read_manifest_info(path)
    libset = set(libs)
    engine = ("Unreal" if libset & {"libUE4.so", "libUnreal.so"} else "Unity" if "libunity.so" in libset
              else "CryEngine" if "libCrySystem.so" in libset else "Other")
    has_openxr, has_vrapi = "libopenxr_loader.so" in libset, "libvrapi.so" in libset
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
            "analysis_version": ANALYSIS_VERSION,
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
            "vr_kind": vr_kind(libset, manifest_strings),
            # the base of a split APK set (Play "app bundle" installs): the code/libraries live in split APKs
            "split_apk": bool({"isSplitRequired", "requiredSplitTypes"} & set(manifest_strings)),
            # Unity (IL2CPP) text fields: they close at once on the Frame (frame.unity_text_input)
            "text_fields": unity_text_fields(il2cpp_meta) if il2cpp_meta else [],
            # Meta's OVRManager raises MSAA at runtime (frame.unity_runtime_msaa_off); Oculus XR Plugin (multiview)
            "ovr_runtime_msaa": bool(il2cpp_meta) and b"\0useRecommendedMSAALevel\0" in il2cpp_meta,
            "sdl_java": sdl_java,
            "vivox_api31": vivox_api31,
            "media_codec": media_codec,
            # declares gamepad support (SDL's manifest template has both): device.steam_gamepad
            "gamepad": "android.hardware.gamepad" in features or axml.LEANBACK_LAUNCHER in cats,
            "oculus_xr_plugin": bool(il2cpp_meta) and b"\0m_StereoRenderingModeAndroid\0" in il2cpp_meta,
            # Unity's built-in Oculus support checks for Meta's system apps before VR (frame.unity_oculus_check)
            "unity_oculus_check": b"\0com.oculus.systemactivities\0" in lib_bytes.get("libunity.so", b""),
            # a 2D launcher activity that starts a separate VR activity (frame.start_activity)
            "vr_activity": axml.vr_activity(manifest),
            "unity_version": unity_version(ggm, lib_bytes.get("libunity.so")) if engine == "Unity" else None,
            # the minimum Android version the APK declares (read from the original: OVRPort lowers it to 29)
            "min_sdk": axml.min_sdk(manifest),
            # a website in an Android wrapper (TWA): nothing to port
            "web_wrapper": web_wrapper(meta, manifest_strings, apk_info),
            # Unreal packaged its content as an OBB (expansion file), or a Unity split build keeps all but its first
            # scene in one: without it the game hangs at start (GitHub #85, #92)
            "expects_obb": expects_obb(meta) or unity_split,
            "unity_split": unity_split,
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
