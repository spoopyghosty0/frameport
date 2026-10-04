#!/usr/bin/env python3
"""Rebuild the prebuilt native artifacts in ../artifacts (developers only; users never need this).

Everything is fetched on demand into native/.cache (git-ignored):
  - Android NDK r27c (27.2.12479018) from Google's repository (checksummed via repository2-3.xml)
  - OpenXR headers at the commits each component was written against (KhronosGroup/OpenXR-SDK)
  - Temurin JDK 21 (javac) and Android build-tools (d8) for the Java stub classes
Then builds: FrameBridge adapter (arm64 + arm32), VrApi bridge, platform compat, language packs, GL shim, oculusos stub dex, the
timefix OpenXR layer for Proton games (linux-arm64, glibc; the NDK's clang builds it freestanding), the
OculusHMDConnected helper for Rift games under Proton (win-x64 PE; the NDK's clang + lld-link, no Windows SDK), and
rewrites artifacts/SHA256SUMS. Run `frameport parity` afterwards to see which games change.

    python native/build.py [--only adapter,bridge,compat,langpack,glshim,dex,xrlayer,oculushmd,xrshim,vkshim,vrsettings] [--ndk PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CACHE = HERE / ".cache"
ART = Path(os.environ.get("FRAMEPORT_ARTIFACTS_OUT", ROOT / "artifacts"))
NDK_VERSION = "27.2.12479018"  # r27c
REPO = "https://dl.google.com/android/repository/"
OPENXR = {  # component -> OpenXR-SDK commit its headers came from
    "adapter": "f2448a8797c85814aa892efc1ab8707900fbcc78",
    "bridge": "7f9285bce1ce8b69bb75554bf788666579d0c35e",
    "xrlayer": "f2448a8797c85814aa892efc1ab8707900fbcc78",
}
OPENXR_HEADERS = ("openxr.h", "openxr_platform.h", "openxr_platform_defines.h", "openxr_loader_negotiation.h")


def log(msg):
    print(f"== {msg}", flush=True)


def fetch(url: str, dest: Path, sha1: str | None = None) -> Path:
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"download {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    if sha1 and hashlib.sha1(tmp.read_bytes()).hexdigest() != sha1:
        raise SystemExit(f"checksum mismatch: {url}")
    tmp.replace(dest)
    return dest


def repo_archive(path_prefix: str) -> tuple[str, str]:
    """(url, sha1) of a package in Google's repository for this host OS."""
    host = {"Linux": "linux", "Darwin": "macosx", "Windows": "windows"}[platform.system()]
    idx = fetch(REPO + "repository2-3.xml", CACHE / "repository2-3.xml")
    root = ElementTree.fromstring(idx.read_text())
    for pkg in root.iter("remotePackage"):
        if pkg.get("path") != path_prefix:
            continue
        for a in pkg.iter("archive"):
            if a.findtext("host-os") in (host, None):
                return REPO + a.findtext("complete/url"), a.findtext("complete/checksum")
    raise SystemExit(f"{path_prefix} not found in the Android repository index")


def ndk(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    dest = CACHE / f"ndk-{NDK_VERSION}"
    if not (dest / "source.properties").exists():
        url, sha1 = repo_archive(f"ndk;{NDK_VERSION}")
        z = fetch(url, CACHE / Path(url).name, sha1)
        log("extract NDK")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(CACHE / "ndk-extract")
        top = next((CACHE / "ndk-extract").iterdir())
        shutil.move(str(top), str(dest))  # (a plain rename can fail on NTFS while the folder is being scanned)
        shutil.rmtree(CACHE / "ndk-extract", ignore_errors=True)
        restore_symlinks(z, dest)
        for p in (dest / "toolchains/llvm/prebuilt").rglob("bin/*"):
            p.chmod(0o755)
    return dest


def restore_symlinks(archive: Path, dest: Path) -> None:
    """zipfile writes symlinks (clang -> clang-18, ...) as small text files; replace them with copies of their
    targets (copies also work on filesystems without symlinks)."""
    with zipfile.ZipFile(archive) as zf:
        links = [i for i in zf.infolist() if (i.external_attr >> 16) & 0o170000 == 0o120000]
        for _ in range(3):  # links to links
            for info in links:
                rel = Path(*Path(info.filename).parts[1:])
                path = dest / rel
                target = (path.parent / zf.read(info).decode()).resolve()
                if target.is_file() and (not path.exists() or path.stat().st_size < 256):
                    path.unlink(missing_ok=True)
                    shutil.copy2(target, path)


def clang_dir(ndk_root: Path) -> Path:
    host = {"Linux": "linux-x86_64", "Darwin": "darwin-x86_64", "Windows": "windows-x86_64"}[platform.system()]
    return ndk_root / "toolchains/llvm/prebuilt" / host


def openxr_include(component: str) -> Path:
    commit = OPENXR[component]
    inc = CACHE / f"openxr-{commit[:10]}"
    for h in OPENXR_HEADERS:
        fetch(f"https://raw.githubusercontent.com/KhronosGroup/OpenXR-SDK/{commit}/include/openxr/{h}", inc / "openxr" / h)
    return inc


def run(cmd, cwd=None):
    cmd = [str(c) for c in cmd]
    p = subprocess.run(cmd, cwd=cwd)
    if p.returncode:
        raise SystemExit(f"failed: {' '.join(cmd[:3])} …")


def exe(tc: Path, name: str) -> Path:
    return tc / "bin" / (name + (".cmd" if os.name == "nt" and "clang" in name and "-" in name else ""))


def build_adapter(tc: Path):
    src = HERE / "adapter"
    run([sys.executable, src / "gen_forwarders.py"])
    inc = openxr_include("adapter")
    common = ["-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-gnu-conditional-omitted-operand",
              "-Wno-missing-field-initializers", "-Wl,-Bsymbolic", "-Wl,-soname,libopenxr_loader_generic.so", "-I", inc]
    run([exe(tc, "aarch64-linux-android29-clang"), *common, "-Wl,-z,max-page-size=16384", "frame_adapter.c", "forwarders.S",
         "-ldl", "-llog", "-lm", "-o", ART / "arm64-v8a/libopenxr_loader_generic.so"], cwd=src)
    run([exe(tc, "armv7a-linux-androideabi29-clang"), *common, "frame_adapter.c", "forwarders_arm32.S", "-ldl", "-llog", "-lm",
         "-o", ART / "armeabi-v7a/libopenxr_loader_generic.so"], cwd=src)


def build_xrlayer(tc: Path):
    """OpenXR API layer for Linux aarch64 (glibc), loaded by Proton's wineopenxr host loader on the Frame. Freestanding:
    no libc headers or link-time libs; clock_gettime binds to the process's libc at load time."""
    src = HERE / "xrlayer"
    inc = openxr_include("xrlayer")
    out = ART / "linux-arm64"
    out.mkdir(parents=True, exist_ok=True)
    run([tc / "bin/clang", "--target=aarch64-linux-gnu", "-ffreestanding", "-nostdlibinc", "-fno-stack-protector",
         "-fvisibility=hidden", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror", "-I", src / "include", "-I", inc,
         f"-ffile-prefix-map={HERE}=native", "-shared", "-nostdlib", "-fuse-ld=lld", "-Wl,--build-id=none",
         "-Wl,-z,max-page-size=65536", "-Wl,--hash-style=both", "-Wl,-soname,libxr_frameport_timefix.so",
         "timefix_layer.c", "-o", out / "libxr_frameport_timefix.so"], cwd=src)
    shutil.copy(src / "XR_APILAYER_FRAMEPORT_timefix.json", out / "XR_APILAYER_FRAMEPORT_timefix.json")


OCULUSHMD_IMPORTS = ("CreateEventW", "CreateJobObjectW", "AssignProcessToJobObject", "QueryInformationJobObject",
                     "CreateProcessW", "ResumeThread", "WaitForSingleObject", "GetExitCodeProcess", "CloseHandle",
                     "Sleep", "GetCommandLineW", "GetLastError", "GetStdHandle", "WriteFile", "ExitProcess")


def build_oculushmd(tc: Path, out: Path | None = None, defines=()):
    """fp_oculushmd.exe (Windows x64, run by Proton on the Frame): provides the OculusHMDConnected event for Unreal
    Rift games. Freestanding: no CRT; the kernel32 import library is generated from a .def by llvm-dlltool."""
    src = HERE / "oculushmd"
    out = out or ART / "win-x64"
    out.mkdir(parents=True, exist_ok=True)
    work = CACHE / "oculushmd"
    work.mkdir(parents=True, exist_ok=True)
    (work / "kernel32.def").write_text("LIBRARY kernel32.dll\nEXPORTS\n" + "".join(f"    {n}\n" for n in OCULUSHMD_IMPORTS))
    run([tc / "bin/llvm-dlltool", "-m", "i386:x86-64", "-d", work / "kernel32.def", "-l", work / "kernel32.lib"])
    run([tc / "bin/clang", "--target=x86_64-pc-windows-msvc", "-ffreestanding", "-nostdlibinc", "-fno-stack-protector",
         "-fno-builtin", "-O2", "-Wall", "-Wextra", "-Werror", f"-ffile-prefix-map={HERE}=native", *defines, "-c",
         "fp_oculushmd.c", "-o", work / "fp_oculushmd.obj"], cwd=src)
    run([tc / "bin/lld-link", "/nologo", "/Brepro", "/nodefaultlib", "/entry:start", "/subsystem:console",
         "/dynamicbase", "/highentropyva", "/nxcompat", work / "fp_oculushmd.obj", work / "kernel32.lib",
         f"/out:{out / 'fp_oculushmd.exe'}"])


VRSETTINGS_IMPORTS = ("LoadLibraryA", "GetProcAddress", "GetCommandLineA", "GetStdHandle", "WriteFile", "ExitProcess")


def build_vrsettings(tc: Path):
    """fp_vrsettings.exe (Windows x64, run on the PC): SteamVR settings through OpenVR's IVRSettings (see the source)."""
    src = HERE / "vrsettings"
    out = ART / "win-x64"
    out.mkdir(parents=True, exist_ok=True)
    work = CACHE / "vrsettings"
    work.mkdir(parents=True, exist_ok=True)
    (work / "kernel32.def").write_text("LIBRARY kernel32.dll\nEXPORTS\n" + "".join(f"    {n}\n" for n in VRSETTINGS_IMPORTS))
    run([tc / "bin/llvm-dlltool", "-m", "i386:x86-64", "-d", work / "kernel32.def", "-l", work / "kernel32.lib"])
    run([tc / "bin/clang", "--target=x86_64-pc-windows-msvc", "-ffreestanding", "-nostdlibinc", "-fno-stack-protector",
         "-fno-builtin", "-O2", "-Wall", "-Wextra", "-Werror", f"-ffile-prefix-map={HERE}=native", "-c",
         "fp_vrsettings.c", "-o", work / "fp_vrsettings.obj"], cwd=src)
    run([tc / "bin/lld-link", "/nologo", "/Brepro", "/nodefaultlib", "/entry:start", "/subsystem:console",
         "/dynamicbase", "/highentropyva", "/nxcompat", work / "fp_vrsettings.obj", work / "kernel32.lib",
         f"/out:{out / 'fp_vrsettings.exe'}"])


def build_bridge(tc: Path):
    src = HERE / "vrapi-bridge"
    inc = openxr_include("bridge")
    run([tc / "bin/clang++", "--target=aarch64-linux-android29", f"--sysroot={tc / 'sysroot'}", "-std=c++17", "-O2", "-g",
         "-fPIC", f"-ffile-prefix-map={src}=native/vrapi-bridge", f"-ffile-prefix-map={CACHE}=native/.cache",
         "-fvisibility=hidden", "-fvisibility-inlines-hidden", "-Wall", "-Wextra",
         "-Wno-missing-field-initializers", "-Werror=return-type", "-DXR_NO_PROTOTYPES", "-DXR_USE_PLATFORM_ANDROID",
         "-DXR_USE_GRAPHICS_API_VULKAN", "-DXR_USE_GRAPHICS_API_OPENGL_ES", "-DXR_USE_TIMESPEC", f"-I{inc}", "-shared",
         "-static-libstdc++", "runtime.cpp", "graphics.cpp", "input.cpp", "-Wl,--no-undefined",
         "-Wl,-z,max-page-size=16384", "-Wl,--version-script=exports.map", "-llog", "-landroid", "-lvulkan", "-lEGL",
         "-lGLESv3", "-ldl", "-o", ART / "arm64-v8a/libvrapi.so"], cwd=src)


def build_compat(tc: Path):
    src = HERE / "platformcompat"
    run([tc / "bin/clang++", "--target=aarch64-linux-android29", f"--sysroot={tc / 'sysroot'}", "-std=c++17", "-O2",
         "-fPIC", "-fvisibility=hidden", "-fno-exceptions", "-fno-rtti", "-ffunction-sections", "-fdata-sections",
         "-Wall", "-Wextra", "-Werror=return-type", "-shared", "-nostdlib++", "message_type.cpp", "-Wl,--no-undefined",
         "-Wl,--gc-sections", "-Wl,-z,max-page-size=16384", "-Wl,-soname,libovrplatformcompat.so",
         "-Wl,--version-script=exports.map", "-o", ART / "arm64-v8a/libovrplatformcompat.so"], cwd=src)


def build_langpack(tc: Path):
    """Language packs from the game's files (see langpack/langpack.c): DT_NEEDED-injected into overport's platform loader."""
    src = HERE / "langpack"
    run([exe(tc, "aarch64-linux-android29-clang"), "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror",
         "-fvisibility=hidden", f"-ffile-prefix-map={src}=native/langpack", "-Wl,--no-undefined",
         "-Wl,-z,max-page-size=16384", "-Wl,-soname,libfp_langpack.so", "langpack.c", "-ldl",
         "-o", ART / "arm64-v8a/libfp_langpack.so"], cwd=src)


def build_glshim(tc: Path):
    src = HERE / "glshim"
    run([exe(tc, "aarch64-linux-android29-clang"), "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror",
         "-Wno-unused-function", "-Wno-incompatible-pointer-types", "-fvisibility=hidden", "-Wl,-soname,libglshim.so",
         "-Wl,-z,max-page-size=16384", "glshim.c", "-ldl", "-llog", "-o", ART / "arm64-v8a/libglshim.so"], cwd=src)


def build_xrshim(tc: Path):
    """FrameBridge extension shim (see xrshim/xrshim.c): DT_NEEDED-injected in front of overport's libopenxr_loader.so."""
    src = HERE / "xrshim"
    run([exe(tc, "aarch64-linux-android29-clang"), "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror",
         "-fvisibility=hidden", "-I", openxr_include("adapter"), "-Wl,-soname,libframe_xrshim.so",
         "-Wl,-z,max-page-size=16384", "xrshim.c", "-ldl", "-llog", "-o", ART / "arm64-v8a/libframe_xrshim.so"], cwd=src)


def build_vkshim(tc: Path):
    """Vulkan shim (see vkshim/vkshim.c): the engine's dlopen("libvulkan.so") string is pointed at it."""
    src = HERE / "vkshim"
    run([exe(tc, "aarch64-linux-android29-clang"), "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror",
         "-fvisibility=hidden", "-Wl,-soname,libfp_vk.so", "-Wl,-z,max-page-size=16384", "vkshim.c",
         "-Wl,--no-as-needed", "-lvulkan", "-Wl,--as-needed", "-ldl", "-llog", "-o", ART / "arm64-v8a/libfp_vk.so"],
        cwd=src)


def build_dex():
    javac = shutil.which("javac")
    if not javac:
        raise SystemExit("javac not found: install a JDK (e.g. Temurin 21) to rebuild the Java stubs")
    url, sha1 = repo_archive(next(p for p in _build_tools_paths()))
    bt = fetch(url, CACHE / Path(url).name, sha1)
    d8 = CACHE / "d8.jar"
    if not d8.exists():
        with zipfile.ZipFile(bt) as z:
            d8.write_bytes(z.read(next(m for m in z.namelist() if m.endswith("lib/d8.jar"))))
    out = CACHE / "java-classes"
    shutil.rmtree(out, ignore_errors=True)
    # compile-only stand-ins for the Android classes the stubs reference (not dexed; the device provides them)
    android = CACHE / "android-stubs"
    (android / "android/content").mkdir(parents=True, exist_ok=True)
    (android / "android/content/Context.java").write_text("package android.content;\npublic abstract class Context {}\n")
    android_out = CACHE / "android-classes"
    run([javac, "--release", "8", "-d", android_out, android / "android/content/Context.java"])
    srcs = sorted((HERE / "java-stubs").rglob("*.java"))
    run([javac, "--release", "8", "-cp", android_out, "-d", out, *srcs])
    classes = sorted(out.rglob("*.class"))
    dexdir = CACHE / "dex"
    shutil.rmtree(dexdir, ignore_errors=True)
    dexdir.mkdir(parents=True)
    run(["java", "-cp", d8, "com.android.tools.r8.D8", "--release", "--min-api", "24", "--output", dexdir, *classes])
    shutil.copy(dexdir / "classes.dex", ART / "dex/oculusos-stubs.dex")


def _build_tools_paths():
    idx = ElementTree.fromstring(fetch(REPO + "repository2-3.xml", CACHE / "repository2-3.xml").read_text())
    paths = [p.get("path") for p in idx.iter("remotePackage") if p.get("path", "").startswith("build-tools;")
             and "rc" not in p.get("path")]
    return sorted(paths, key=lambda s: [int(x) for x in re.findall(r"\d+", s)], reverse=True)


def write_sums():
    lines = []
    for f in sorted(p for p in ART.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        lines.append(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  ./{f.relative_to(ART).as_posix()}")
    (ART / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="adapter,bridge,compat,langpack,glshim,dex,xrlayer,oculushmd,xrshim,vkshim,vrsettings")
    ap.add_argument("--ndk")
    args = ap.parse_args()
    parts = set(args.only.split(","))
    for d in ("arm64-v8a", "armeabi-v7a", "dex"):
        (ART / d).mkdir(parents=True, exist_ok=True)
    tc = clang_dir(ndk(args.ndk)) if parts - {"dex"} else None
    steps = {"adapter": lambda: build_adapter(tc), "bridge": lambda: build_bridge(tc), "compat": lambda: build_compat(tc),
             "langpack": lambda: build_langpack(tc),
             "glshim": lambda: build_glshim(tc), "xrshim": lambda: build_xrshim(tc), "dex": build_dex, "xrlayer": lambda: build_xrlayer(tc),
             "oculushmd": lambda: build_oculushmd(tc), "vkshim": lambda: build_vkshim(tc),
             "vrsettings": lambda: build_vrsettings(tc)}
    for name in ("adapter", "bridge", "compat", "langpack", "glshim", "xrshim", "vkshim", "dex", "xrlayer", "oculushmd", "vrsettings"):
        if name in parts:
            log(f"build {name}")
            steps[name]()
    write_sums()
    log("artifacts/SHA256SUMS updated")


if __name__ == "__main__":
    main()
