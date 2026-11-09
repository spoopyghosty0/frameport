#!/usr/bin/env python3
"""Builds the Android bridge with the NDK (see ../README.md, "Android bridge"):

  out/bridge/liba32rt.so      guest runtime: static 32-bit ARM bionic program (static PIE), the "system libraries"
  out/bridge/liba32bridge.so  host bridge (arm64): the translator + JNI/NativeActivity/libandroid glue
  out/bridge/liba32stub.so    host stub (arm64), copied once per 32-bit library as libX.so
  --test: out/bridge/test/harness (static arm64 program: fake JNI + the bridge) and libbtest.a32.so (armv7 guest
          library); python bridge/test/run.py runs them

    python bridge/build.py [--test] [--debug] [--ndk PATH]
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NDK_DEFAULT = ROOT.parent / ".cache" / "ndk-27.2.12479018"
OUT = ROOT / "out" / "bridge"
TRANSLATOR = ["dispatch.c", "mem.c", "loader.c", "syscall.c", "signals.c", "threads.c", "interp.c", "interp_vfp.c",
              "interp_neon.c", "a32_decode.c", "t32_decode.c", "vfp_neon_decode.c", "jit.c", "jit_vfp.c",
              "jit_glue.S"]
HOST = ["bridge.c", "handles.c", "jni.c", "natives.c", "native_activity.c", "android.c", "vrapi.c", "input.c", "media.c", "tramp.S", "gen/jni_gen.c",
        "gen/imports.c"]
HOST_GL = ["gl.c", "gen/gl.c"]  # not in the PC harness (no GLES there)
GUEST = ["main.c", "loader.c", "compat.c", "log.c", "gl.c", "opensles.c", "media_keys.c", "hc.S", "gen/stubs.S", "gen/gl_stubs.S", "gen/exports.S"]
WARN = ["-Wall", "-Wextra", "-Wno-unused-parameter", "-Wno-missing-field-initializers"]


def run(cmd):
    print(" ".join(str(c) for c in cmd[:1]) + " ... -o " + str(cmd[cmd.index("-o") + 1]))
    r = subprocess.run([str(c) for c in cmd])
    if r.returncode:
        sys.exit(r.returncode)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ndk", default=str(NDK_DEFAULT))
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    bin_ = Path(a.ndk) / "toolchains/llvm/prebuilt/linux-x86_64/bin"
    if not bin_.exists():
        sys.exit(f"{bin_} not found (run python native/build.py once to fetch the NDK)")
    cc64 = bin_ / "aarch64-linux-android30-clang"
    cc32 = bin_ / "armv7a-linux-androideabi30-clang"
    OUT.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(HERE / "gen_stubs.py"), "--ndk", a.ndk], check=True)
    opt = ["-O0", "-g"] if a.debug else ["-O2", "-g"]
    # The cache can be symlinked into a worktree. Clang records both the spelling
    # used on the command line and the resolved NDK path, plus the compilation cwd.
    prefix = [f"-ffile-prefix-map={ROOT.parent.parent}=frameport",
              f"-ffile-prefix-map={ROOT}=a32translator",
              f"-ffile-prefix-map={Path(a.ndk).absolute()}=ndk",
              f"-ffile-prefix-map={Path(a.ndk).resolve()}=ndk"]

    # guest runtime: everything a guest library may link against comes from the static archives (exports.S
    # references each symbol, so the linker keeps them)
    g = HERE / "guest"
    # -static-pie links crtbegin_dynamic.o, which leaves .init_array to a dynamic linker that isn't there: use the
    # static start files (libc's own constructors run from them)
    crt = Path(a.ndk) / "toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/arm-linux-androideabi/30"
    run([cc32, *opt, *prefix, *WARN, "-mthumb", "-mfpu=neon-vfpv4", "-std=gnu11", "-static-pie", "-nostartfiles",
         crt / "crtbegin_static.o", *[g / s for s in GUEST], "-lz", "-lm", "-lstdc++", "-Wl,--no-gc-sections", "-Wl,--wrap=dl_unwind_find_exidx", crt / "crtend_android.o", "-o",
         OUT / "liba32rt.so"])

    h = HERE / "host"
    src = ROOT / "src"
    common = [*opt, *prefix, *WARN, "-march=armv8-a+crypto", "-std=gnu11", "-fno-strict-aliasing", "-fPIC"]
    run([cc64, *common, "-shared", "-fvisibility=hidden", *[src / s for s in TRANSLATOR], *[h / s for s in HOST + HOST_GL],
         "-llog", "-landroid", "-laaudio", "-lmediandk", "-lEGL", "-lGLESv3", "-ldl", "-lm", "-Wl,--no-undefined", "-Wl,-soname,liba32bridge.so", "-o", OUT / "liba32bridge.so"])
    run([cc64, *common, "-shared", "-fvisibility=hidden", h / "stub.c", "-L", OUT, "-la32bridge",
         "-Wl,--no-undefined", "-o", OUT / "liba32stub.so"])

    if a.test:
        t = HERE / "test"
        to = OUT / "test"
        to.mkdir(exist_ok=True)
        # the harness is static (runs under qemu-aarch64 or on any arm64 Linux): high image base keeps the
        # low 4 GiB free, like a32run's
        run([cc64, *common, "-DA32B_STDERR_LOG", "-DA32B_TEST", "-static", "-Wl,--image-base=0x7000000000", "-I", h,
             t / "harness.c", t / "fakejni.c", t / "fakemedia.c", *[src / s for s in TRANSLATOR], *[h / s for s in HOST], "-lm", "-ldl",
             "-o", to / "harness"])
        run([cc32, *opt, *prefix, *WARN, "-mthumb", "-mfpu=neon-vfpv4", "-shared", "-fPIC", t / "libbtest.c",
             "-Wl,-soname,libbtest.so", "-llog", "-o", to / "libbtest.a32.so"])
        run([cc32, *opt, *prefix, *WARN, "-marm", "-mfpu=neon-vfpv4", "-shared", "-fPIC", t / "libbtest2.c",
             to / "libbtest.a32.so", "-landroid", "-llog", "-o", to / "libbtest2.a32.so"])
        run([cc32, *opt, *prefix, *WARN, "-mthumb", "-mfpu=neon-vfpv4", "-shared", "-fPIC", "-I", t,
             t / "libbtest4.c", "-ldl", "-o", to / "libbtest4.a32.so"])
        run([cc32, *opt, *prefix, *WARN, "-mthumb", "-shared", "-fPIC", "-funwind-tables", t / "libbtest5.c",
             "-ldl", "-o", to / "libbtest5.a32.so"])
        run([bin_ / "armv7a-linux-androideabi30-clang++", *opt, *prefix, *WARN, "-mthumb", "-shared", "-fPIC",
             "-static-libstdc++", t / "libbtest3.cpp", "-o", to / "libbtest3.a32.so"])
        # Exercise both RELR tag families through the real guest loader. C++ constructors,
        # pointer tables and exceptions need relative relocations; Android's older tags
        # occur in Quest libraries (e.g. Journey of the Gods' OVRPlugin).
        for tags in ("android", "standard"):
            run([bin_ / "armv7a-linux-androideabi30-clang++", *opt, *prefix, *WARN, "-mthumb", "-shared", "-fPIC",
                 "-static-libstdc++", t / "libbtest3.cpp", "-Wl,--pack-dyn-relocs=android+relr",
                 "-Wl,--use-android-relr-tags" if tags == "android" else "-Wl,--no-use-android-relr-tags",
                 "-o", to / f"libbtest3_{tags}.a32.so"])
        shutil.copy(OUT / "liba32rt.so", to / "liba32rt.so")


if __name__ == "__main__":
    main()
