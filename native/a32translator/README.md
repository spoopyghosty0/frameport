# a32translator: AArch32 → AArch64 dynamic binary translator

`a32run` runs 32-bit ARM Linux programs (ARM, Thumb-2, VFPv4, NEON, plus the ARMv8 AArch32 additions) on an
AArch64 host without AArch32 support, such as the Steam Frame's SteamOS. It is a user-mode translator (like
`qemu-arm`). It decodes guest instructions, runs them through a JIT that emits native A64 code, and translates
system calls to the host kernel.

Status: proof of concept. It runs static bionic (Android NDK) programs, including C++ with exceptions, pthreads,
signals, fork, self-modifying code and NEON. The Android bridge (below) runs 32-bit JNI and NativeActivity
libraries inside a 64-bit app, verified in a Lepton container on the Frame; games additionally need GLES/EGL,
OpenSL/AAudio, libandroid and VrApi thunks (see "Next steps").

```
python native/a32translator/build.py          # -> native/a32translator/out/a32run (static AArch64, NDK clang)
a32run [--interp] [--strace] [--trace] [--sysroot DIR] [-v] program [args...]
```

Environment: `A32_STATS=1` (block/chain statistics at exit), `A32_DUMP=file` (translated code, see
`tools/jitdump.py`), `A32_EXACT_ADDR=1` (guest address arithmetic modulo 2^32, see below), `A32_CACHE_KB=n`
(small code cache, exercises flushes), `A32_INTERP=1`, `A32_STRACE=1`, `A32_PERFMAP=<file>|1` (a perf map of the
translated code: `a32:<pc>` block bodies, `a32:exits` exit stubs, `a32:fpcr` FPCR writes; `1` writes
`/tmp/perf-<pid>.map`, which `perf report` reads).

32-bit bionic refuses pids above 65535, which hosts with a large `pid_max` (SteamOS: 4194304) exceed. Start such
programs in a fresh pid namespace: `unshare -Urpf --kill-child a32run prog`.

## How it works

```
 guest bytes ─┬─ a32_decode.c (A32) ───┐
              └─ t32_decode.c (T32) ───┼─► insn_t ─┬─► interp*.c  (reference semantics, fallback)
   vfp_neon_decode.c (VFP/NEON) ◄──────┘           └─► jit.c / jit_vfp.c (A64 code) ─► code cache
```

- **One instruction form, two backends.** Both decoders produce `insn_t` (`insn.h`). T32 VFP/NEON encodings are
  rewritten to their A32 form first, so one decoder serves both instruction sets. The interpreter (`interp.c`,
  `interp_vfp.c`, `interp_neon.c`) defines the semantics of everything. The JIT translates the common
  instructions natively and calls the interpreter for the rest (`jx_call_helper`), so every instruction the
  interpreter knows also works in JIT mode.
- **Register mapping.** Guest r0-r14 are pinned in x0-x14 (upper halves always zero), q0-q15 in v16-v31, and the
  guest NZCV lives in the host NZCV. x19 points to the `a32_cpu` state. The guest PC is a translation-time
  constant. Scratch registers: x15-x17, x20-x23, v0-v7. Within a block, scalar VFP values are cached in v8-v15:
  writing a lane of a pinned Q register would chain loop iterations through the merge. The cache is flushed at
  exits, helper calls and branches.
- **Flags.** A64 ADDS/SUBS/ADCS/SBCS set NZCV exactly like AArch32. Logical flag-setting instructions get N/Z
  from the result, C from the shifter and V unchanged (`set_nzc`). Conditional instructions branch over their
  code (the condition encodings match).
- **Floating point.** Guest FP operations run as the identical A64 instructions with the guest's FPSCR control
  bits in FPCR (FPCR/FPSR share FPSCR's bit layout), so rounding, flush-to-zero, default NaN and exception flags
  match by construction. Advanced SIMD floating point runs with the "standard FPSCR value" (DN, FZ, round to
  nearest). Blocks switch FPCR only around such operations. VMLA/VMLS stay unfused (multiply, then add) and
  VRECPS/VRSQRTS go to the interpreter (non-fused in AArch32, fused in A64). The interpreter uses inline A64
  instructions as well.
- **Blocks.** A block (a superblock) ends at the first unconditional branch or other PC write, an SVC/UDF, or
  after 96 instructions. Conditional branches and CBZ/CBNZ are side exits: translation continues on the
  fall-through path, so a loop with an early exit is still one block. Blocks are keyed by (pc, Thumb, ITSTATE), so
  IT blocks may span block boundaries. Direct exits are patchable `b` instructions that get chained to their
  target. Indirect branches (BX LR, POP {pc}, LDR pc, TBB) look the target up inline in a direct-mapped table. A
  branch back to the block's own start is a plain `b`/`b.cond`. Every block starts with a check of
  `cpu->interrupt`, which signals and code flushes set, so chained loops still stop.
- **Code quality.** Conditional data-processing instructions that don't set flags become compute + `csel`
  (branchless). VCMP followed by VMRS APSR_nzcv leaves the compare's flags in NZCV directly. LDREX/STREX (all
  sizes) are an inline compare-and-swap loop (`ldxr`/`stxr`). Thumb's flag-setting register shifts
  (LSLS/LSRS/ASRS/RORS Rdn, Rm) compute the AArch32 carry for 0-255 bit amounts inline. In the benchmark below,
  no instruction goes to the interpreter (`A32_STATS=1` lists the ones that do).
- **Memory.** Guest addresses are host addresses: the low 4 GiB are reserved at start-up (`mem.c`) and the
  guest's mappings live there, so loads and stores need no translation and most syscalls pass pointers through.
  Translated code uses A64 addressing modes (`[xn, #imm]`). They differ from AArch32 only when guest address
  arithmetic wraps past 4 GiB: such an access then faults above 4 GiB, and the fault is still delivered to the
  guest. `A32_EXACT_ADDR=1` computes every address modulo 2^32 instead. Unaligned LDRD/LDM/VLDn are allowed
  (newer arm64 kernels fix them up for AArch32 too); qemu raises SIGBUS. The host page size must be 4 KiB.
- **Self-modifying code.** `cacheflush` (what `__builtin___clear_cache` calls on ARM), `mprotect`, `munmap` and
  `mremap` invalidate the translated blocks of the affected pages and unlink chains into them. Each guest page keeps
  a list of its blocks, so an invalidation only visits those (Unity's Mono JIT flushes new code next to translated
  code all the time; scanning every block made that 72 % of Spunky Dungeon's main thread). A full cache
  (128 MiB) is flushed once every thread has left translated code.
- **Faults.** A guest memory fault in translated code is mapped back to the guest instruction (per-instruction
  host offsets, plus the VFP cache state). The guest registers are rebuilt from the signal context, and the
  guest's handler gets a proper ARM `rt_sigframe` (VFP state included). Returning from it restarts the
  instruction.
- **Syscalls** (`syscall.c`): ARM EABI numbers (generated from the NDK headers, `tools/gen_syscalls.py`) map to
  AArch64 ones. Structures whose 32-bit layout differs are converted: stat64, statfs64, timespec/timeval, iovec,
  rusage, sysinfo, rlimit, flock, msghdr with control messages, getdents. The guest persona (`PER_LINUX32`) is
  emulated, because hosts without AArch32 refuse it. ARM private calls (set_tls, cacheflush) and kuser helpers
  (0xffff0fxx) are implemented. `execve` of a 32-bit ARM ELF re-executes `a32run`.
- **Signals** (`signals.c`): host handlers queue signals; delivery happens at block boundaries with the ARM
  kernel's frame layouts (`sigframe`/`rt_sigframe`, `sigaltstack`, SA_RESTORER, SA_NODEFER, SA_RESETHAND).
  Guest SIG_DFL/SIG_IGN dispositions stay on the host, so the kernel applies default actions itself.
- **Threads** (`threads.c`): `clone` with CLONE_VM|CLONE_THREAD starts a host pthread running its own `a32_cpu`.
  Other clones become `fork`. The code cache is shared: a mutex guards translation and chaining only. The run loop
  finds blocks without it (the inline lookup table first, then the hash chains) and puts each block it dispatched
  back into the inline table, so a block translated later into the same slot can't shut a hot return target out.
  Each thread flags itself while it is in the run loop or translated code; a cache flush waits for the flags (a
  shared counter cost every dispatch a contended atomic).
  LDREX/STREX are emulated as compare-and-swap on the exclusive value; ABA changes are not detected.

Not supported: big-endian data (SETEND BE), ARMv8.1+ AArch32 extensions (FP16 arithmetic, RDM, JSCVT, dot
product), VFP short vectors (Len/Stride, absent on ARMv8 anyway), robust futex lists, and the iWMMXt/ThumbEE
relics. FPSCR exception *traps* are not emulated (flags are).

## Android bridge

A converted APK runs its 32-bit libraries in its own 64-bit process, without Android's native-bridge mechanism
(Lepton would need `ro.dalvik.vm.native.bridge` and 32-bit ABI lists for that). Every 32-bit library `libX.so`
moves to `lib/arm64-v8a/libX.a32.so`; `libX.so` becomes a copy of the stub, next to the bridge and the guest
runtime:

```
python native/a32translator/bridge/build.py [--test]
  out/bridge/liba32stub.so    arm64 stub (copied as libX.so): JNI_OnLoad, ANativeActivity_onCreate -> bridge
  out/bridge/liba32bridge.so  arm64: the translator + bridge/host (exports a32b_*, bridge/host/a32bridge.h)
  out/bridge/liba32rt.so      32-bit ARM static PIE: the guest runtime and the guest's "system libraries"
```

- **Guest runtime** (`bridge/guest`): bionic's static libc/libm/libz/libstdc++/libunwind, linked whole and
  exported through a table (`gen/exports.S`), plus a loader for the game's libraries (bionic's lookup order, packed
  relocations, RELR, `dlopen`/`dlsym`/`dladdr`/`dl_iterate_phdr`, ARM EHABI unwinding through
  `dl_unwind_find_exidx` and libgcc's `__gnu_Unwind_Find_exidx`). Symbols nothing defines become stubs that log their name and return 0. Libraries the
  guest knows as system libraries (`libGLESv2.so`, ...) resolve against the export table.
- **Hypercalls** (`bridge/abi.h`): guest code calls the host with `SVC 0` and `r7 = 0x0a32xxxx`; host-to-guest
  calls return through a trampoline in the kuser page. The thread that starts the bridge becomes the guest's main
  thread; any other host thread gets its own guest thread on its first call (the runtime's service thread creates
  it, and the clone is handed to the waiting host thread). Calls nest: Java -> guest -> JNI -> Java -> guest.
- **JNI** (`bridge/host`): the guest gets 32-bit `JNIEnv`/`JavaVM` tables whose entries are hypercalls.
  `gen_stubs.py` generates the simple functions from `jni.h`; `jni.c` has the rest (variadic/`va_list`/`jvalue[]`
  calls read with the method's shorty, strings and arrays: shared when the data lies below 4 GiB, as ART's heap
  does, else copied and written back). Objects, method and field IDs are 32-bit handles (`handles.c`). Native
  methods (RegisterNatives, and exported `Java_*` functions, matched to their Java declarations by reflection) get a
  trampoline slot (`tramp.S`, `natives.c`) that converts AAPCS64 arguments to AAPCS32.
- **NativeActivity** (`native_activity.c`): a 32-bit `ANativeActivity` mirrors the host's; the host callbacks
  forward to the guest's. Windows, input queues and asset managers are handles. libandroid functions are listed in
  `bridge/imports.txt` (generated host side).
- The bridge shares the low 4 GiB with the host process: `mem.c` reads `/proc/self/maps` and never hands out host
  mappings; host faults go to the host's own handlers (ART's); translator reports on stderr go to logcat (tag
  `a32`).

- **GLES/EGL** (`gen_gl.py`, `host/gl.c`): every function of the NDK's GLES 3.2 + extension and EGL headers is a
  hypercall stub (Unity fetches most through `dlsym`/`eglGetProcAddress`); EGL objects and GLsyncs are handles,
  driver strings are copied, buffer mappings outside the guest's reach get a guest copy written back at flush/unmap.
- **VrApi 1.x** (`host/vrapi.c`): Gear VR-era games call the old API (`vrapi_SubmitFrame` with `ovrFrameParms`,
  `vrapi_GetPredictedTracking`). The runtime exports it itself (`libvrapi.so` is a system library; the game's copy is
  Oculus' loader and is dropped) and translates it to FramePort's 64-bit VrApi -> OpenXR bridge (shipped as
  `libfp_vrapi64.so`, with OVRPort's OpenXR dispatcher and the FrameBridge adapter). Layouts are VrApi 1.5's.
- **NDK media** (`host/media.c`, imports.txt): libmediandk's AMediaFormat/AMediaCodec/AMediaExtractor functions and
  its `AMEDIAFORMAT_KEY_*` string variables (`guest/media_keys.c`, AOSP's values), as Unity's video player looks
  them up (it drops NDK playback if one is missing, and its JNI fallback needs
  `GetDirectBufferAddress` of codec buffers above 4 GiB). Format strings/buffers get guest copies freed with the
  format; codec buffers get a guest mirror per (codec, input/output, index): input mirrors go to the codec at
  `queueInputBuffer` (the queued range), output buffers come in at `getOutputBuffer` (the range
  `dequeueOutputBuffer` reported). `AMediaCodecBufferInfo` has the same layout on both sides.
- **OpenSL ES** (`guest/opensles.c`): engine, output mix and PCM players with an Android buffer queue, implemented in
  the guest (OpenSL objects are function tables); a guest thread per player writes to AAudio on the host.
- **Signals**: host handlers never use SA_RESTART; a blocking system call interrupted by a guest signal is
  restarted after the guest's handler (as the kernel's -ERESTARTSYS), and a thread running host code (Java, a JNI or
  GL call) runs the guest handler at once. Boehm GC (Unity's Mono) stops the world with signals and hung without it.
- `bridge/convert.py IN.apk OUT.apk` (prototype of the FramePort patch): moves the 32-bit libraries, adds the stubs,
  bridge, runtime and VR libraries, sets LAUNCHER and targetSdkVersion >= 23 (an older one gets Android's "built for
  an older version" dialog, which takes the focus: Unity never starts), and points libunity's Oculus check at
  "android" (like `frame.unity_oculus_check`). Unity games also need the flat window (`device.text_input_window`):
  Unity only renders with window focus.
- Debugging: `A32_WATCH=<s>` logs every guest thread's pc and the return addresses on its stack as `lib+offset`;
  `A32B_TRACE=1` logs native registrations and calls; `A32_STRACE`, `A32_INTERP` as for a32run. Set them through the
  recipe's `device.lepton_env` (`LEPTON_ENV_<NAME>`). `OVP_HEADLESS_TEST=1` (tests without a worn headset): the
  VrApi bridge submits images although the runtime doesn't ask for them and `vrapi.c` keeps untracked default poses
  instead of dropping the frame, so FrameBridge's `snapshot=N` shows what the game draws.

Learned on the way: `-static-pie` links `crtbegin_dynamic.o`, which leaves `.init_array` to a dynamic linker (so
none of libc's own constructors ran: bionic's systrace property crashed in an Android process); bionic's arm32
static start-up code also reads `PT_TLS`/`PT_GNU_RELRO` as if the program sat at its link address, so the loader
moves both by the load bias.

Tests:
- `python bridge/test/run.py [--interp]` (after `build.py --test`): the harness (`bridge/test/harness.c`) plays ART
  with a fake JVM (`fakejni.c`) for three guest libraries: RegisterNatives and `Java_*` exports, 12-argument
  natives (stack arguments on both sides), floats/doubles/bytes, strings, arrays below and above 4 GiB, every
  Call*Method style, NewObject, nested calls, guest threads that attach, several host threads, exceptions, a
  NativeActivity with a DT_NEEDED library, NDK media against a fake libmediandk whose buffers lie above 4 GiB
  (`fakemedia.c`, `libbtest4.c`), and C++ exceptions in guest libraries using ordinary REL,
  standard RELR and Android's older RELR tags (with APS2), and the unwind-table lookup libgcc's unwinder uses
  (`__gnu_Unwind_Find_exidx`, `libbtest5.c`). Runs under qemu-aarch64 or
  natively (passes on the Frame with JIT and interpreter).
- `uv run python bridge/test/make_apk.py`: `org.frameport.a32test`, a NativeActivity APK without Java code whose
  library is 32-bit (`bridge/test/libapktest.c`). Installed with FramePort (`frameport scan`, then `frameport
  install org.frameport.a32test --apk ...`, `frameport test`), it logs `A32TEST ok|FAIL` lines (tag `a32test`):
  real ART calls (`Build.MODEL`, `String.format`, `Math.hypot`, arrays, exceptions), a guest thread, a dlopen'd C++
  library, and the activity's callbacks with the real window. All pass in Lepton (2026-10-10).

## Testing

All of these run on an x86-64 PC through `qemu-aarch64`, with `qemu-arm` as the reference, or natively on an
AArch64 machine with `--runner ""`.

- `tests/test_a64.py`: every A64 encoder in `src/a64.h` against `llvm-mc` (400 encodings).
- `tests/fuzz.py`: differential instruction fuzzer. Random encodings per class (`arm-dp`, `arm-ldst`,
  `arm-media`, `arm-ldm`, `arm-vfp`, `arm-neon`, `arm-neon-ldst`, `arm-v8`, `t16`, `t32`, `t32-neon`,
  `t32-vfp`) are filtered with `arm-none-eabi-objdump` and with the translator's own UNPREDICTABLE flag
  (`tools/a32dis.c`). Each runs once in a harness that loads random registers, flags, FPSCR and NEON state, and
  dumps them afterwards together with a hash of the touched memory. The harness's own signal handler records
  UNDEF and memory faults per test, so UNDEF decoding is compared too. Runs `qemu-arm -cpu max`, then `a32run
  --interp`, then `a32run`. Known, deliberate differences are filtered: qemu's alignment faults, ARMv8.1+
  features, and encodings in ARMv7's unallocated space that qemu executes.
- `tests/run_programs.py`: C/C++ programs built with the NDK for ARM and Thumb (static bionic), compared with
  `qemu-arm` in both modes: libc/libm/printf, memory and string routines, NEON intrinsics, pthreads and atomics,
  signals (handlers, SIGSEGV recovery, itimers, fork), file system calls, C++ exceptions/RTTI/STL/std::thread,
  run-time generated code (`jitguest`) and a benchmark. `A32_CACHE_KB=300` exercises cache flushes.

Benchmark (`tests/programs/bench.c 10`) on the Steam Frame, medians of three runs (the headset's clocks vary by
up to ±30 % between runs), against the same source built natively for AArch64:

| | native AArch64 | a32run, ARM build | a32run, Thumb build |
|---|---|---|---|
| sieve | 0.11 s | 0.12 s | 0.10 s |
| crc32 + sha256 | 0.39 s | 0.42 s | 0.57 s |
| qsort | 0.15 s | 0.21 s | 0.25 s |
| float matmul | 0.017 s (auto-vectorized) | 0.27 s (scalar VFP: ARMv7 compilers don't vectorize float) | 0.27 s |
| double mandelbrot | 0.21 s | 0.44 s | 0.41 s |
| libm | 0.06 s | 0.25 s | 0.27 s |

## Next steps

1. First game: AFFECTED The Manor (Gear VR, Unity 5.6.0f3 + Mono, OVRPlugin 1.12, 2017). Headless on the Frame
   (2026-10-10): Unity and its C# scripts run, audio plays, the OpenXR session starts and submits frames at 72 fps.
   Not yet seen in a headset. Open: Gear VR input (touchpad/back, controller API reports no devices), the rest of
   libandroid (ALooper callbacks, AInputQueue, AAsset*), host-to-guest callbacks (GL debug output, sensor queues).
2. A FramePort patch that converts an APK (libraries moved, stubs, bridge, runtime, `extractNativeLibs=true`) +
   triage signature + PLAYBOOK row.
3. Bridge gaps: guest threads of host threads that exit are never freed; `Java_*` functions of libraries the guest
   dlopens itself aren't registered; `GetDirectBufferAddress` of buffers above 4 GiB returns 0 (Java-side writes would need syncing at JNI
   calls; NDK media avoids it for Unity's video); signals the guest
   handles are only delivered to guest threads.
4. JIT: VLDn single-lane/all-lanes forms, flag liveness (skip unused C/V computations), keeping the VFP cache
   across a loop's back edge, and VRECPS/VRSQRTS/VCVTR natively.
