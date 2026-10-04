"""Inspect an APK and describe everything the patch suggestions depend on."""
from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

from ..apk import axml
from ..core.models import Analysis
from . import elf

logging.getLogger("pyaxmlparser").setLevel(logging.ERROR)

UNITY_GGM = "assets/bin/Data/globalgamemanagers"
IL2CPP_METADATA = "assets/bin/Data/Managed/Metadata/global-metadata.dat"


def _read_manifest_info(path: Path) -> tuple[str, str, str, str | None]:
    from pyaxmlparser import APK

    apk = APK(str(path))
    label = apk.application or apk.package
    return apk.package, apk.version_name or "", label, apk.get_main_activity()


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

    package, version, label, activity = _read_manifest_info(path)
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
            "frame_patched": "libframe_settings.so" in libset,  # already has FramePort's FrameBridge adapter
            "missing_ovr_symbols": sorted(missing_ovr_symbols(lib_bytes)), "size": path.stat().st_size,
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
            "oculus_xr_plugin": bool(il2cpp_meta) and b"\0m_StereoRenderingModeAndroid\0" in il2cpp_meta,
            "unity_version": unity_version(ggm, lib_bytes.get("libunity.so")) if engine == "Unity" else None,
        },
    )


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


def missing_ovr_symbols(lib_bytes: dict[str, bytes]) -> set[str]:
    """ovr_* / ovrMessageType_* functions the game imports that the platform loader (+compat/stub libs) lacks."""
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
        wanted |= {s for s in elf.dyn_symbols(data, False) if s.startswith(("ovr_", "ovrMessageType_"))}
    return wanted - exported
