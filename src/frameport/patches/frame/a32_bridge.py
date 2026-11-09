"""32-bit ARM games (Gear VR, Oculus Go, early Quest builds) through the a32 bridge (native/a32translator/bridge).

The Frame has no AArch32. The bridge runs a game's 32-bit libraries inside the 64-bit app: each
lib/armeabi-v7a/libX.so becomes lib/arm64-v8a/libX.a32.so (the guest code) plus lib/arm64-v8a/libX.so (a small arm64
stub that hands Java's calls to the translator). The guest runtime (liba32rt.so) provides the 32-bit "system
libraries" (bionic, GLES/EGL, OpenSL ES, VrApi), translated to the host's. VrApi games get the 64-bit VrApi bridge
(libvrapi.so artifact, loaded as libfp_vrapi64.so) on OVRPort's OpenXR loader chain, which frame.adapter then wraps
like any OVRPort build. OVRPort itself doesn't run (it handles 64-bit apps only)."""
from __future__ import annotations

import struct
from collections.abc import Callable

from ...analysis import detect
from ...apk import axml
from ..base import ApkContext, Patch, Suggestion, register
from . import artifact

MANIFEST = "AndroidManifest.xml"
ABIS32 = ("armeabi-v7a", "armeabi")
HOST = "arm64-v8a"
BRIDGE, RUNTIME, STUB = "liba32bridge.so", "liba32rt.so", "liba32stub.so"
VRAPI64 = "libfp_vrapi64.so"  # the 64-bit VrApi bridge under a name the 32-bit libvrapi.so doesn't clash with
PLATFORM = "libovrplatformloader.so"
PLATFORM_META = ("libovrplatformloader_meta.so", "libovrplatformloader_meta_q1.so")
OVERPORT_CONFIG = (b'{"version":1,"database":[{"app_id":"__current__","products":[],"assets":[]}],'
                   b'"patched":{"by":"FramePort a32 bridge","patches":[]}}')


SIGNATURE_ASSET = "assets/fp_a32_signature.txt"  # read by the bridge (native_activity.c), answered in jni.c
HEADSET_ASSET = "assets/fp_a32_headset.txt"  # "quest": the bridge passes the Touch controllers through (vrapi.c)


def original_signature(ws) -> bytes | None:
    """What UE4's GameActivity.AndroidThunkJava_GetSignature() returns for the original APK: Base64 (Android's
    Base64.DEFAULT, newline included) of the SHA-1 of the signing certificate (Signature.toByteArray() = the
    certificate's DER). UE4 Gear VR games (Gunjack) compare it with a copy in their code and quit when it differs,
    which every re-signed build does; the bridge returns this value instead."""
    import base64
    import hashlib

    from cryptography.hazmat.primitives.serialization import Encoding, pkcs7

    from ...apk.workspace import SIGNATURE_FILE

    for name in sorted(ws.names()):
        m = SIGNATURE_FILE.match(name)
        if not m or (m.group(2) or "").upper() not in ("RSA", "DSA", "EC"):
            continue
        try:
            certs = pkcs7.load_der_pkcs7_certificates(ws.read(name))
        except ValueError:
            continue
        if certs:
            # the signer's own certificate: the one that issued none of the others (a block may carry its CA
            # chain, in any order)
            leaves = [c for c in certs if not any(o is not c and o.issuer == c.subject for o in certs)] or certs
            der = leaves[0].public_bytes(Encoding.DER)
            return base64.b64encode(hashlib.sha1(der).digest()) + b"\n"
    return None


# Repacks carry a Frida gadget (libfrda.so + its config and script, loaded from Java with System.loadLibrary("frda"))
# that rewrites Meta's entitlement and platform checks. OVRPort's patch_clean_up_frida removes it from 64-bit games;
# OVRPort doesn't run on this path, so the conversion does the same (in the translator the gadget also crashed while
# scanning the process's memory: Shadow Point).
FRIDA_LIBS = ("libfrda.so", "libfrda.config.so", "libscript.so")


def strip_frida_load(data: bytes) -> bytes | None:
    """The dex with every `System.loadLibrary("frda")` call turned into nops; None if it has none."""
    import struct

    from ...apk.dex import Dex

    if b"frda" not in data:
        return None
    dex = Dex(data)
    frda = [i for i in range(dex.strings_n) if dex.string(i) == "frda"]
    loads = set(dex.method_index("Ljava/lang/System;", "loadLibrary"))
    if not frda or not loads:
        return None
    done = 0
    for cls in dex.class_names():
        for _idx, code in dex.class_methods(cls):
            consts = {}
            for pos, unit in list(dex._insns(code)):
                op = unit[0] & 0xFF
                if op == 0x1A:  # const-string vAA, string@BBBB
                    consts[unit[0] >> 8] = unit[1]
                elif op == 0x1B:  # const-string/jumbo vAA, string@BBBBBBBB
                    consts[unit[0] >> 8] = unit[1] | unit[2] << 16
                elif op == 0x71 and unit[1] in loads and unit[0] >> 12 == 1 and consts.get(unit[2] & 0xF) in frda:
                    struct.pack_into("<3H", dex.data, code + 16 + 2 * pos, 0, 0, 0)  # invoke-static: 3 nops
                    done += 1
    return dex.finish() if done else None


def unity_oculus_check(data: bytes) -> bytes:
    """Unity's built-in Oculus support starts VR only when com.oculus.systemactivities is installed: point the lookup
    at "android" (as frame.unity_oculus_check does for 64-bit Unity)."""
    old = b"\0com.oculus.systemactivities\0"
    return data.replace(old, b"\0android" + b"\0" * (len(old) - 8))


def raise_target_sdk(manifest: bytes, minimum: int = 23) -> bytes:
    """Android 11 shows a "built for an older version of Android" dialog for targetSdkVersion < 23, which takes the
    window focus (Unity then never starts rendering)."""
    x = axml.Axml(manifest)
    names = x.strings()
    for el in x.elements():
        if el.name == "uses-sdk":
            for at in el.attrs:
                if (at.name < len(names) and names[at.name] == "targetSdkVersion" and at.dtype == 0x10
                        and at.value < minimum):
                    struct.pack_into("<I", x.data, at.offset + 16, minimum)
    return x.bytes()


def convert(ws, prebuilt: Callable[[str], bytes], ovrport: Callable[[str, str], bytes],
            replace: dict[str, bytes] | None = None) -> list[str]:
    """Turns the workspace's 32-bit ARM app into the bridged arm64 layout; returns the converted library names.
    prebuilt(name): an artifact (liba32bridge.so, liba32rt.so, liba32stub.so, libvrapi.so = the 64-bit VrApi
    bridge, libopenxr_loader_generic.so = FrameBridge); ovrport(name, abi): a library of OVRPort's runtime;
    replace: 32-bit libraries put in place of the APK's own (or added)."""
    replace = dict(replace or {})
    names = ws.names()
    abi = next((a for a in ABIS32 if any(n.startswith(f"lib/{a}/") for n in names)), None)
    if not abi:
        raise RuntimeError("no 32-bit ARM libraries in this APK")
    if any(n.startswith(f"lib/{HOST}/") for n in names):
        raise RuntimeError("the APK has 64-bit libraries already")
    libs = sorted(n[len(f"lib/{abi}/"):] for n in names if n.startswith(f"lib/{abi}/") and n.endswith(".so"))
    if "libfrda.so" in libs:  # a repack's Frida gadget: not shipped, and its load is removed (FRIDA_LIBS)
        for name in [n for n in names if n.startswith("classes") and n.endswith(".dex")]:
            fixed = strip_frida_load(ws.read(name))
            if fixed:
                ws.put(name, fixed)
        libs = [b for b in libs if b not in FRIDA_LIBS]
    signature = original_signature(ws)
    if signature and any(n.startswith(f"lib/{abi}/libUE4.so") for n in names):
        ws.put(SIGNATURE_ASSET, signature)
    manifest = ws.read(MANIFEST)
    if detect.requires_6dof(manifest):  # a Quest (Santa Cruz) build: Touch controllers, not the Gear VR remote
        ws.put(HEADSET_ASSET, b"quest")
    manifest = axml.set_bool_attr(manifest, "application", "extractNativeLibs", True) or manifest
    ws.put(MANIFEST, raise_target_sdk(manifest))
    guest_only = {}  # libraries only guest code loads (no stub for Java)
    if PLATFORM in libs:  # Meta's Platform SDK: OVRPort's 32-bit dispatcher, as every FramePort conversion ships
        replace.setdefault(PLATFORM, ovrport(PLATFORM, "armeabi-v7a"))
        for extra in PLATFORM_META:  # Meta's own loaders, which the dispatcher loads by name
            try:
                guest_only[extra] = ovrport(extra, "armeabi-v7a")
            except (FileNotFoundError, RuntimeError):
                pass
    stub = prebuilt(STUB)
    out = f"lib/{HOST}/"
    converted = []
    for base in sorted(set(libs) | set(replace)):
        if base == "libvrapi.so" and base not in replace:
            continue  # Oculus' loader stub: VrApi is translated by the bridge instead
        data = replace[base] if base in replace else ws.read(f"lib/{abi}/{base}")
        if base == "libunity.so":
            data = unity_oculus_check(data)
        ws.put(f"{out}{base[:-3]}.a32.so", data)
        ws.put(f"{out}{base}", stub)
        converted.append(base)
    for base, data in guest_only.items():
        ws.put(f"{out}{base[:-3]}.a32.so", data)
    for n in [n for n in names if n.startswith("lib/") and not n.startswith(out)]:
        ws.remove.add(n)  # every other ABI: Lepton would pick a 32-bit one
        ws.add.pop(n, None)
        ws.replace.pop(n, None)
    if "libvrapi.so" in libs:
        # the app's Java side may load VrApi itself (System.loadLibrary("vrapi"), e.g. Unreal's GameActivity: "GearVR
        # library not loaded and required!"): an empty arm64 library under that name (the guest's VrApi is the bridge)
        from ...analysis.stubgen import build_stub_library

        ws.put(f"{out}libvrapi.so", build_stub_library(["fp_a32_vrapi_placeholder"], soname="libvrapi.so"))
        ws.put(f"{out}{VRAPI64}", prebuilt("libvrapi.so"))
        ws.put(f"{out}libopenxr_loader.so", ovrport("libopenxr_loader.so", HOST))  # OVRPort's dispatcher
        # OVRPort's generic loader under the name frame.adapter gives it, with FrameBridge in its place (frame.adapter
        # can only rename what the APK had); frame.adapter then writes the settings
        ws.put(f"{out}libopenxr_loader_original.so", ovrport("libopenxr_loader_generic.so", HOST))
        ws.put(f"{out}libopenxr_loader_generic.so", prebuilt("libopenxr_loader_generic.so"))
        ws.put(f"{out}liboverport.config.so", OVERPORT_CONFIG)
    ws.put(f"{out}{BRIDGE}", prebuilt(BRIDGE))
    ws.put(f"{out}{RUNTIME}", prebuilt(RUNTIME))
    ws.abi, ws.libdir = HOST, out  # the patches after this one see the arm64 layout
    return converted


class A32Bridge(Patch):
    id = "frame.a32_bridge"
    a32 = True  # the bridge itself
    # the translator ended blocks before its exit-stub table ran out only from this release on (GitHub #342: OpenSSL's
    # ARMv8 SHA-1 rounds in Pinball FX2 VR killed the game): "Update on Frame" for the game that hit it
    # Ghost Giant (GitHub #439): Unity's video player needs libmediandk (AMediaFormat/AMediaCodec/AMediaExtractor
    # functions, then its AMEDIAFORMAT_KEY_* data symbols); 4: OpenSL ES buffers read on the clock (distorted sound)
    # 2 (every game, the runtime ships inside each APK): libgcc's unwinder in gnustl-era libraries (Unity 2017's
    # libil2cpp.so) got the runtime's own unwind table from __gnu_Unwind_Find_exidx, so every C++ throw ended in
    # terminate (GitHub #439); the per-package numbers moved up with it
    revision = 2
    package_revisions = {"com.zenstudios.PinballFX2VRQuest": 3, "com.zoink.ghostgiant": 5}
    title = "32-bit ARM bridge (Gear VR / Go games)"
    description = (
        "Runs a 32-bit-only game (armeabi-v7a: Gear VR, Oculus Go and early Quest games) on the Frame, which has no "
        "32-bit ARM support: an AArch32 → AArch64 translator inside the app runs the game's own libraries, with "
        "GLES/EGL, OpenSL ES and VrApi translated to the Frame's 64-bit ones (VrApi through the VrApi → OpenXR "
        "bridge). The Gear VR / Go controller: left stick = touchpad (walk, strafe), right stick sideways = turn "
        "(a click at the touchpad's edge), stick press or A = click, right trigger, B = back. Experimental: games "
        "run slower than native ones.")
    order = 5  # before frame.adapter, which wraps the OpenXR loader this patch adds
    experimental = True
    needs_vr = False  # translates the game's native code: a flat 32-bit app needs it as much as a VR game

    def detect(self, a):
        if self.applies(a):
            return Suggestion(True, "32-bit-only game: the Frame can't run 32-bit ARM code, the bridge translates it.")
        return None

    def applies(self, a):
        return a.only_32bit

    def apply(self, ctx: ApkContext) -> bool:
        from ...tools import overport

        if ctx.ws.abi == HOST:
            return False  # converted already (or a 64-bit game)

        def ovrport(name: str, abi: str) -> bytes:
            return overport.ensure_runtime_lib(name, abi, ctx.reporter).read_bytes()

        converted = convert(ctx.ws, lambda name: artifact(HOST, name), ovrport)
        ctx.notes.append(f"{len(converted)} libraries run through the bridge")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        ok = ws.has(f"lib/{HOST}/{BRIDGE}") and ws.has(f"lib/{HOST}/{RUNTIME}")
        return [("32-bit bridge present", ok, "liba32bridge.so + liba32rt.so")]


register(A32Bridge)
