#!/usr/bin/env python3
"""Generates the bridge's tables from the NDK:

  guest/gen/exports.S  the runtime's "system library" exports: every libc/libm/libstdc++/libz symbol of the NDK's
                       arm API stubs that bionic's static archives define, plus the runtime's own dl*/liblog
                       implementations and the host import stubs; (name, address) pairs sorted by name
  guest/gen/stubs.S    JNIEnv / JavaVM function tables and host import stubs (each one a hypercall)
  host/gen/jni_gen.c   host side of every JNI function, marshalled from jni.h's signatures (special cases are
                       written by hand in host/jni.c)
  host/gen/imports.c   host side of the imports listed in imports.txt

    python bridge/gen_stubs.py [--ndk PATH]
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NDK_DEFAULT = HERE.parents[1] / ".cache" / "ndk-27.2.12479018"
API = 30
HC_JNI, HC_VM, HC_IMPORT = 0x100, 0x200, 0x300

# exported names implemented by the runtime itself (name -> symbol)
OVERRIDES = {
    "dlopen": "grt_dlopen", "dlsym": "grt_dlsym", "dlclose": "grt_dlclose", "dlerror": "grt_dlerror",
    "dladdr": "grt_dladdr", "dl_iterate_phdr": "dl_iterate_phdr",
    "dl_unwind_find_exidx": "__wrap_dl_unwind_find_exidx", "android_dlopen_ext": "grt_android_dlopen_ext",
    # libgcc's unwinder (gnustl-era libraries such as Unity 2017's libil2cpp.so carry their own) asks this one; static
    # bionic's version ignores the pc and returns the runtime's own table (#439). Same signature as
    # dl_unwind_find_exidx.
    "__gnu_Unwind_Find_exidx": "__wrap_dl_unwind_find_exidx",
    "dlvsym": "grt_dlvsym", "eglGetProcAddress": "grt_eglGetProcAddress",
}
COMPAT = ["_tolower_tab_", "_toupper_tab_", "__isthreaded", "bsd_signal", "pthread_atfork",  # pthread_atfork: crtbegin_static.o
          "fdprintf", "vfdprintf",
          "putw", "get_malloc_leak_info", "free_malloc_leak_info", "__page_size", "getdtablesize", "tkill",
          "SHA1Init", "SHA1Update", "SHA1Final", "__google_potentially_blocking_region_begin",
          "__google_potentially_blocking_region_end",  # guest/compat.c
          "slCreateEngine", "slQueryNumSupportedEngineInterfaces", "slQuerySupportedEngineInterfaces", "SL_IID_NULL",
          "SL_IID_OBJECT", "SL_IID_ENGINE", "SL_IID_PLAY", "SL_IID_BUFFERQUEUE", "SL_IID_ANDROIDSIMPLEBUFFERQUEUE",
          "SL_IID_VOLUME", "SL_IID_ANDROIDCONFIGURATION", "SL_IID_RECORD", "SL_IID_OUTPUTMIX"]  # guest/opensles.c
LIBLOG = ["__android_log_write", "__android_log_print", "__android_log_vprint", "__android_log_buf_write",
          "__android_log_buf_print", "__android_log_assert", "__android_log_is_loggable"]
# libc symbols that must not be exported (static-only internals)
SKIP = {"__libc_init", "__libc_preinit", "_start"}

OBJ = {"jobject", "jclass", "jstring", "jthrowable", "jarray", "jobjectArray", "jbooleanArray", "jbyteArray",
       "jcharArray", "jshortArray", "jintArray", "jlongArray", "jfloatArray", "jdoubleArray", "jweak"}
INT = {"jint", "jsize", "jboolean", "jbyte", "jchar", "jshort", "jobjectRefType"}
# JNI functions written by hand in host/jni.c (pointer results, varargs, freeing handles, natives)
SPECIAL = re.compile(r"^(Call\w*Method[VA]?|NewObject[VA]?|Get\w*ArrayElements|Release\w*ArrayElements|"
                     r"GetStringUTFChars|ReleaseStringUTFChars|GetStringChars|ReleaseStringChars|"
                     r"GetStringCritical|ReleaseStringCritical|GetPrimitiveArrayCritical|"
                     r"ReleasePrimitiveArrayCritical|RegisterNatives|GetJavaVM|GetDirectBufferAddress|"
                     r"Delete\w*Ref|NewGlobalRef|NewWeakGlobalRef|PopLocalFrame|GetMethodID|GetStaticMethodID|"
                     r"FromReflectedMethod)$")


def tool(ndk, name):
    return Path(ndk) / "toolchains/llvm/prebuilt/linux-x86_64/bin" / name


def defined(ndk, path, dynamic):
    args = [str(tool(ndk, "llvm-nm")), "--defined-only", "-g", "-j"] + (["-D"] if dynamic else []) + [str(path)]
    out = subprocess.run(args, capture_output=True, text=True).stdout
    return {l.strip().split("@")[0] for l in out.splitlines() if l.strip() and not l.endswith(":")}


def code(t):
    t = t.replace("const", "").strip()
    if t.endswith("*") or "*" in t:
        return "E" if t.replace(" ", "") in ("JNIEnv*", "JavaVM*") else "P"
    if t in OBJ:
        return "O"
    if t == "jmethodID":
        return "M"
    if t == "jfieldID":
        return "F"
    if t in INT:
        return "I"
    if t == "jlong":
        return "J"
    if t == "jfloat":
        return "f"
    if t == "jdouble":
        return "D"
    if t == "va_list":
        return "V"
    if t == "...":
        return "."
    if t == "void":
        return "v"
    raise ValueError(t)


def parse_jni(ndk):
    h = (Path(ndk) / "toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/include/jni.h").read_text()
    body = h[h.index("struct JNINativeInterface {"):]
    body = body[:body.index("};")]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    funcs = []
    for m in re.finditer(r"(void\*\s+reserved\d;)|([\w ]+?\**)\s*\(\*(\w+)\)\s*\(([^)]*)\);", body):
        if m.group(1):
            funcs.append(None)
            continue
        ret, name, params = m.group(2).strip(), m.group(3), m.group(4)
        ptypes = []
        for p in params.split(","):
            p = p.strip()
            if p == "...":
                ptypes.append("...")
                continue
            p = re.sub(r"\b\w+$", "", p).strip() if not p.endswith("*") and len(p.split()) > 1 else p
            ptypes.append(p)
        funcs.append((name, ret, ptypes))
    if len(funcs) != 233:
        sys.exit(f"jni.h: {len(funcs)} JNINativeInterface entries, expected 233")
    return funcs


CTYPE = {"O": "jobject", "M": "jmethodID", "F": "jfieldID", "I": "jint", "J": "jlong", "f": "jfloat",
         "D": "jdouble", "P": "void *"}


def gen_jni_host(funcs, out):
    lines = ["/* generated by bridge/gen_stubs.py from the NDK's jni.h: do not edit */", "",
             '#include "../jni_internal.h"', "",
             "int jni_dispatch_generated(a32_cpu *c, JNIEnv *env, int index, argreader *R)", "{",
             "    switch (index) {"]
    names = []
    for i, f in enumerate(funcs):
        if f is None:
            names.append("reserved")
            continue
        name, ret, ptypes = f
        names.append(name)
        if SPECIAL.match(name):
            continue
        codes = [code(p) for p in ptypes]
        rc = code(ret)
        assert codes[0] == "E", name
        if any(c in ".V" for c in codes) or rc == "P":
            sys.exit(f"{name}: needs a hand-written implementation (varargs or pointer result)")
        decl, args = [], ["env"]
        for k, (cd, t) in enumerate(zip(codes[1:], ptypes[1:]), 1):
            v = f"a{k}"
            if cd == "O":
                decl.append(f"{t.replace('const', '').strip()} {v} = ({t.replace('const', '').strip()})h2o(rd32(R));")
            elif cd == "M":
                decl.append(f"jmethodID {v} = h2mid(rd32(R));")
            elif cd == "F":
                decl.append(f"jfieldID {v} = h2fid(rd32(R));")
            elif cd == "I":
                decl.append(f"{t} {v} = ({t})rd32(R);")
            elif cd == "J":
                decl.append(f"jlong {v} = (jlong)rd64(R);")
            elif cd == "f":
                decl.append(f"jfloat {v} = rdf(R);")
            elif cd == "D":
                decl.append(f"jdouble {v} = rdd(R);")
            elif cd == "P":
                decl.append(f"{t} {v} = ({t})g2p(rd32(R));")
            args.append(v)
        call = f"(*env)->{name}({', '.join(args)})"
        lines.append(f"    case {i}: {{ /* {name} */")
        lines += ["        " + d for d in decl]
        if rc == "v":
            lines.append(f"        {call};")
        elif rc == "O":
            lines.append(f"        ret32(c, o2h_local({call}));")
        elif rc == "M":
            lines.append(f"        ret32(c, mid2h({call}));")
        elif rc == "F":
            lines.append(f"        ret32(c, fid2h({call}));")
        elif rc == "I":
            lines.append(f"        ret32(c, (uint32_t){call});")
        elif rc == "J":
            lines.append(f"        ret64(c, (uint64_t){call});")
        elif rc == "f":
            lines.append(f"        retf(c, {call});")
        elif rc == "D":
            lines.append(f"        retd(c, {call});")
        lines += ["        return 1;", "    }"]
    lines += ["    }", "    return 0;", "}", "",
              "const char *const jni_names[JNI_TABLE_SIZE] = {"]
    lines += [f'    "{n}",' for n in names]
    lines += ["};", ""]
    out.write_text("\n".join(lines))


def parse_imports():
    out = []
    for line in (HERE / "imports.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        m = re.match(r"(!\s*)?(\w)\s+(\w+)\s*\((\w*)\)$", line)
        if not m:
            sys.exit(f"imports.txt: bad line {line!r}")
        out.append((m.group(3), m.group(2), m.group(4), bool(m.group(1))))
    return out


def gen_imports_host(imports, out):
    lines = ["/* generated by bridge/gen_stubs.py from imports.txt: do not edit */", "",
             "#pragma clang diagnostic ignored \"-Wdeprecated-declarations\"",
             "#include <android/asset_manager_jni.h>", "#include <android/configuration.h>", "#include <android/input.h>", "#include <android/looper.h>",
             "#include <android/native_window_jni.h>", "#include <android/sensor.h>",
             "#include <media/NdkMediaCodec.h>", "#include <media/NdkMediaExtractor.h>",
             "#include <media/NdkMediaFormat.h>",
             '#include "../imports_internal.h"', ""]
    for name, ret, args, manual in imports:
        if manual:
            lines.append(f"void imp_{name}(a32_cpu *c, argreader *R);")
        else:
            lines.append(f"extern __typeof__({name}) {name} __attribute__((weak));")
    lines += ["", "int import_dispatch(a32_cpu *c, int index, argreader *R)", "{", "    switch (index) {"]
    for i, (name, ret, args, manual) in enumerate(imports):
        if manual:
            lines.append(f"    case {i}: imp_{name}(c, R); return 1;")
            continue
        decl, vals = [], []
        for k, a in enumerate(args):
            v = f"a{k}"
            if a == "I":
                decl.append(f"int32_t {v} = (int32_t)rd32(R);")
            elif a == "J":
                decl.append(f"int64_t {v} = (int64_t)rd64(R);")
            elif a == "f":
                decl.append(f"float {v} = rdf(R);")
            elif a == "P":
                decl.append(f"void *{v} = g2p(rd32(R));")
            elif a == "H":
                decl.append(f"void *{v} = h2ptr(rd32(R));")
            elif a == "A":
                decl.append(f"ANativeActivity *{v} = guest_activity(rd32(R));")
            elif a == "E":
                decl.append(f"JNIEnv *{v} = (rd32(R), host_env());")
            elif a == "O":
                decl.append(f"jobject {v} = h2o(rd32(R));")
            else:
                sys.exit(f"imports.txt: {name}: unknown argument type {a}")
            vals.append(v)
        call = f"{name}({', '.join(vals)})"
        lines.append(f"    case {i}: {{ /* {name} */")
        lines.append(f"        if (!{name}) return 0;")
        lines += ["        " + d for d in decl]
        if ret == "v":
            lines.append(f"        {call};")
        elif ret == "I":
            lines.append(f"        ret32(c, (uint32_t){call});")
        elif ret == "J":
            lines.append(f"        ret64(c, (uint64_t){call});")
        elif ret == "f":
            lines.append(f"        retf(c, {call});")
        elif ret == "H":
            lines.append(f"        ret32(c, ptr2h((void *){call}));")
        elif ret == "S":
            lines.append(f"        ret32(c, guest_cstr({call}));")
        else:
            sys.exit(f"imports.txt: {name}: unknown result type {ret}")
        lines += ["        return 1;", "    }"]
    lines += ["    }", "    return 0;", "}", "", "const char *const import_names[] = {"]
    lines += [f'    "{n}",' for n, _, _, _ in imports]
    lines += ["    0,", "};", ""]
    out.write_text("\n".join(lines))


def stub(label, n):
    hc = 0x0a320000 | n
    return [f"{label}:", "    push {r7, lr}",
            f"    movw r7, #{hc & 0xffff:#x}", f"    movt r7, #{hc >> 16:#x}",
            "    svc 0", "    pop {r7, pc}"]


def gen_guest(ndk, imports, gdir, extra):
    sysroot = Path(ndk) / "toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/arm-linux-androideabi"
    want = set()
    for so in ("libc.so", "libm.so", "libstdc++.so", "libz.so"):
        want |= defined(ndk, sysroot / str(API) / so, True)
    have = set()
    for a in ("libc.a", "libm.a", "libstdc++.a", "libz.a"):
        have |= defined(ndk, sysroot / a, False)
    have |= defined(ndk, Path(ndk) / "toolchains/llvm/prebuilt/linux-x86_64/lib/clang/18/lib/linux/arm/libunwind.a",
                    False)
    names = {n: n for n in want & have if n not in SKIP}
    for n in OVERRIDES:
        names[n] = OVERRIDES[n]
    for n in LIBLOG + COMPAT:
        names[n] = n
    for n, *_ in imports:
        names[n] = n
    # libmediandk's data symbols (const char * variables), defined in guest/media_keys.c
    for n in re.findall(r"^KEY\((\w+),", (HERE / "guest" / "media_keys.c").read_text(), re.M):
        names[f"AMEDIAFORMAT_KEY_{n}"] = f"AMEDIAFORMAT_KEY_{n}"
    for n in extra:
        names.setdefault(n, OVERRIDES.get(n, n))
    missing = sorted(want - have - set(OVERRIDES) - SKIP - set(COMPAT))
    (gdir / "missing.txt").write_text("\n".join(missing) + "\n")

    ex = ["/* generated by bridge/gen_stubs.py: do not edit */", "    .section .rodata.grt_names, \"aMS\", %progbits, 1"]
    order = sorted(names, key=lambda s: s.encode())
    for k, n in enumerate(order):
        ex.append(f".Ln{k}: .asciz \"{n}\"")
    ex += ["    .section .data.rel.ro.grt_exports, \"aw\"", "    .p2align 2", "    .global grt_export_table",
           "grt_export_table:"]
    for k, n in enumerate(order):
        ex.append(f"    .word .Ln{k}, {names[n]}")
    ex += ["    .global grt_export_count", "grt_export_count:", f"    .word {len(order)}", ""]
    (gdir / "exports.S").write_text("\n".join(ex))

    st = ["/* generated by bridge/gen_stubs.py: do not edit */", "    .syntax unified", "    .arm", "    .text",
          "    .p2align 2"]
    for i in range(233):
        st += stub(f"jni_{i}", HC_JNI + i)
    for i in range(8):
        st += stub(f"vm_{i}", HC_VM + i)
    for i, (n, *_) in enumerate(imports):
        st += [f"    .global {n}", f"    .type {n}, %function"] + stub(n, HC_IMPORT + i)
    st += ["    .section .data.rel.ro.grt_tables, \"aw\"", "    .p2align 2", "grt_jni_table:"]
    st += [f"    .word jni_{i}" for i in range(233)]
    st += ["grt_vm_table:"] + [f"    .word vm_{i}" for i in range(8)]
    st += ["    .global grt_jni_env", "grt_jni_env:", "    .word grt_jni_table",
           "    .global grt_java_vm", "grt_java_vm:", "    .word grt_vm_table", ""]
    (gdir / "stubs.S").write_text("\n".join(st))
    return len(order), len(missing)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ndk", default=str(NDK_DEFAULT))
    a = ap.parse_args()
    imports = parse_imports()
    gdir, hdir = HERE / "guest" / "gen", HERE / "host" / "gen"
    gdir.mkdir(parents=True, exist_ok=True)
    hdir.mkdir(parents=True, exist_ok=True)
    import gen_gl
    gl_names, gl_skipped = gen_gl.generate(a.ndk, gdir, hdir)
    n, miss = gen_guest(a.ndk, imports, gdir, gl_names)
    funcs = parse_jni(a.ndk)
    gen_jni_host(funcs, hdir / "jni_gen.c")
    gen_imports_host(imports, hdir / "imports.c")
    print(f"{n} exports ({miss} stub symbols without a static definition, see guest/gen/missing.txt), "
          f"{len(imports)} imports, {len(funcs)} JNI functions, {len(gl_names)} GLES/EGL functions ({gl_skipped} "
          "not supported, see guest/gen/gl_skipped.txt)")


if __name__ == "__main__":
    main()
