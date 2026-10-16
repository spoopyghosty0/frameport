# Rift on Frame: performance stream

Implemented offline on `rift/perf`, from `98786ab`. No Frame measurements were made. Existing research measurements
are in `native/fexwine/README.md`; the commands below are for the maintainer to run on the paired Frame.

## What changed

`agent/fexrift_perf.py` registers `settings_get`, `settings_set`, `perf_prepare` and `bench`. Setup's existing `revive`
step also builds three Revive folders. The PC's existing artifact discovery uploads the extension automatically.
The only launcher template changes select the core list, Revive folder and motion-smoothing JSON from variables.
The agent and CLI have separate settings/bench blocks; the agent version is unchanged.

Settings live in `<ROOT>/settings/<Meta-app>.json`, separately from the game catalog. Defaults come from the game's
`perf` object, with the existing `env.FP_MEM` / `env.FP_BIGCACHE_MB` as backward-compatible defaults. Overrides take
precedence. `null` removes an override. Preparation runs on every launch, writes `<app>.dxvk.conf`, and exports
validated values before Wine starts. Updating a setting requires no reinstall after the new launcher is installed.

| Setting | Default | Accepted values / effect |
| --- | --- | --- |
| `dxvk_disable_msaa` | `false` | Boolean; `d3d11.disableMsaa` |
| `dxgi_sync_interval` | `-1` | Integer -1..4; `dxgi.syncInterval` |
| `dxgi_max_frame_latency` | `0` | Integer 0..16; `dxgi.maxFrameLatency` |
| `eye_scale` | `1.5` | Divisor 1.0 / 1.25 / 1.5 (100 / 80 / 67 percent nominal eye size) |
| `motion_smoothing` | `2` | 0 = default, 1 = force on, 2 = force off; SteamVR `motionSmoothingOverride` |
| `cores` | `4-7` | taskset CPU list within 0..7; e.g. `4-7` or `0-3,7` |
| `ntsync` | `true` | Off sets `PROTON_NO_NTSYNC=1`; on unsets it |
| `fp_mem` | per-game opt-in | Boolean; guest preload enabled only when true |
| `bigcache_mb` | `256` | Cache minimum block size, 1..6144 MiB (not the total cache budget) |
| `fp_mem_zero` | `false` | Boolean; `FP_MEM_ZERO=1` opts into lazy zero pages |
| `wine_large_address_aware` | `true` | Boolean; `WINE_LARGE_ADDRESS_AWARE=1/0` |
| `fex` | `{}` | JSON map of whitelisted keys with 0/1 values |

FEX keys: `FEX_TSOENABLED`, `FEX_VECTORTSOENABLED`, `FEX_MEMCPYSETTSOENABLED`, `FEX_MULTIBLOCK`,
`FEX_X87REDUCEDPRECISION`. The first three retain the launcher's default 0, MULTIBLOCK defaults to 1, X87 to 0.
Unknown keys, arbitrary environment variables, shell fragments and invalid paths are refused. The commands do not
inspect login files, print process command lines, or change Platform SDK/entitlement checks.

### Revive variants

The checked-in PC `REVIVE_PATCHES` contains the six visibility patches but **does not contain the research eye-size
redirect described in the brief**. Verified locally against the installed DLL's pinned upstream hash:

- Stock `LibRevive64.dll`: SHA-256 `ef9ffd5342bb4afad9b1c0f4fee1ca3bf32f9d3159d49dd841cc9e22f02663e4`.
- After the six PC patches: `eda2a449d230832c95734a03e8c83eeb531668ce676693f636e19c315765af4e`.
- RVA `0x12eab` (file `0x122ab`) is `movss xmm4,[rip+0x4bb15]`, loading a shared 1.0 constant.
- The research redirect loads the existing 1.5 at **RVA** `0x4eaa4` (file `0x4d4a4`). It is not file offset `0x4eaa4`.

The extension accepts the pinned six-patch upload, optionally with exactly that research redirect. It redirects
this one load into verified alignment padding at RVA `0x12e9c`, after the preceding function's `ret`. Each copy
gets its own divisor there. It leaves shared constants untouched. `revive-scales/{1,1.25,1.5}/` hard-links all other
files to `revive/`; the DLL is unlinked before writing, so variants and the base never modify one another. The
injector is launched from the selected folder, which is where it finds `LibRevive64.dll`.
Unexpected hashes, instructions or occupied padding fail explicitly. No Oculus binaries are patched here.

### fp_mem v3

`FP_MEM_ZERO=1` calls `madvise(MADV_DONTNEED)` on exactly the reused part before returning it: the requested prefix
for short/equal reuse, the cached prefix for a longer allocation. The fresh tail needs no advice. Failed advice
evicts the cache entry and falls through to real mmap, rather than returning stale contents. Without ZERO, v2's
unzeroed behavior remains. Debug output counts advice calls, discarded bytes and failures.

The fake-syscall host harness in `tests/test_fex_perf.py` checks equal, short and long reuse, advice failure,
unzeroed compatibility, and `FP_MEM_ZERO=0`. It skips only the C test when no host C compiler is installed.
The guest artifact has **not** been rebuilt in this stream. On an x86_64 glibc 2.34-compatible build host, from the
repository root, the maintainer must run:

```sh
gcc -O2 -shared -fPIC -o artifacts/fexrift/fp_mem.so native/fexwine/fp_mem.c -ldl -lpthread
python3 - <<'PY'
import hashlib
from pathlib import Path
root = Path('artifacts/fexrift')
manifest = root / 'SHA256SUMS'
lines = manifest.read_text().splitlines()
hash_ = hashlib.sha256((root / 'fp_mem.so').read_bytes()).hexdigest()
lines = [f'{hash_}  ./fp_mem.so' if line.endswith('  ./fp_mem.so') else line for line in lines]
manifest.write_text('\n'.join(lines) + '\n')
PY
```

This is an **x86_64 guest** shared library, even though wineserver is native aarch64. Commit the rebuilt artifact
and checksum at integration. ZERO experiments with the old artifact would silently measure v2 instead.

## Benchmark behavior and limits

`frameport fex bench APP --seconds N` and `scripts/rift_fex_bench.py` each request exactly one run. N is an integer
1..120; it includes launcher/runtime startup, not just game sampling. The Frame enforces the deadline independently
of the PC connection. A bench lock excludes another bench. The agent first rejects active Lepton/FramePort games;
the helper rejects Wine/Proton/FEX processes, other Steam reapers, and Lepton container supervisors. It excludes
SteamVR's own AppId 250820 reaper. An open Meta login session also blocks the benchmark.

The helper requires `IVRSystem_022::GetTrackedDeviceActivityLevel(HMD)` to report **Standby (3)**. All other values,
including unknown, idle, recently interacting and interacting, are refused. A write to Steam's `logs/controller.txt`
within the last 60 seconds also blocks it (conservative: reconnection is enough). The same owner-activity checks run
while sampling; interaction stops the run. This does not claim to detect every desktop keyboard event; do not use
this headless tool while someone is using the Frame. A native non-Steam app outside Steam's reaper may also evade
the process guard. Do not run simultaneous Play/login/install operations against the shared prefix.

Before launch, read a positive finite `power/turnOffScreensTimeout`, set `power/pauseCompositorOnStandby=false`
through the existing `vrsetting` helper, and temporarily set the screen timeout to N+10. Start `launch.sh` in its
own process group. Finally, stop the shared-prefix wineserver (5-second limit), terminate/kill the launcher group,
set `pauseCompositorOnStandby=true`, and restore the original screen timeout. Each restoration is attempted even
when a preceding cleanup fails. SIGTERM and ordinary errors run this cleanup; SIGKILL, power loss or a dead SteamVR
service cannot execute Python's finally. The temporary screen timeout is finite even in those cases.

Every 0.5 seconds, read up to 128 compositor records via pinned `IVRCompositor_022::GetFrameTimings`. Deduplicate by
frame index, retain updated present counts, exclude the initial ring, and count only while scene focus belongs to
the game's PID. A hitch is one unique frame presented at least three times. GPU activity uses the actual game
process's DRM fdinfo engine-nanosecond deltas, deduplicating shared DRM client IDs; no debugfs permissions needed.
Thread wait states are histogrammed from the game's `/proc/<pid>/task/*/wchan` files. No shell commands or logs
containing account data enter the summary.

Successful stdout is one JSON line: effective settings, elapsed time, unique frames, `valid`, frame span, fps,
hitches (also per 20 seconds), average render GPU time and client frame interval, game GPU busy percent, and sampled
thread wait states. GPU busy is the sum of available engines and may exceed 100 percent on devices with independent
engines. `null` means unavailable, not zero. Client frame interval is **not CPU ApplicationTime**. `valid=false`,
zero frames or a short sample span means the launch produced insufficient data; do not treat it as a fast run.
Use equivalent spans and a stable scene when comparing results. A launcher exiting early is an error.

## Exact device checks

The maintainer runs these commands; this stream did not access the network or the Frame. Use the account owner's
normal sign-in workflow from the login stream. A missing entitlement is a failed run to report, never bypass.

1. Build/checksum the guest artifact above. From this worktree on the PC:

   ```sh
   export FRAMEPORT_HOME="$PWD/.home"
   export FRAMEPORT_EXPERIMENTAL_FEX=1
   uv run frameport fex setup
   uv run frameport fex install oculus-first-contact
   uv run frameport fex settings oculus-first-contact
   ```

   Setup should complete the `revive` step and create all three scale folders. Install once to get the new launcher.
   Settings should show fp_mem on, threshold 256, eye divisor 1.5, motion smoothing 2, cores 4-7 and ntsync on.
   Run the Frame helper with `python3 -I` as usual; no additional pip packages are needed there.

2. Wear the headset once after boot, then remove it and let it reach standby. End every game and the Meta client;
   wait at least 60 seconds after controller input. A worn headset or a running game must make bench refuse **before
   changing display settings**. Do not work around a refusal by disabling the checks.

3. Establish a baseline, then run exactly one bounded experiment per command pair below. Each line is independent;
   restore the previous changed key with `null` before moving to the next experiment.

   ```sh
   uv run frameport fex settings oculus-first-contact dxvk_disable_msaa=false dxgi_sync_interval=-1 dxgi_max_frame_latency=0 eye_scale=1.5 motion_smoothing=2 cores=4-7 ntsync=true fp_mem=true bigcache_mb=256 fp_mem_zero=false 'fex={}' wine_large_address_aware=true
   uv run frameport fex bench oculus-first-contact --seconds 120
   ```

   Expected: one summary JSON, `valid=true`, nonzero frames, finite fps/GPU timings; DRM busy may be null if fdinfo
   is unavailable. After it returns, the game/runtime should be gone and the headset should sleep normally.
   `launch.log` in the game's anchor should contain `FramePort: starting Meta's runtime` and
   `FramePort: starting Oculus First Contact`. No rendering with `r.MobileMSAA=2` was proven; do not use that knob.

4. Run these candidate experiments separately, returning to the baseline between them:

   ```sh
   uv run frameport fex settings oculus-first-contact dxvk_disable_msaa=true
   uv run frameport fex bench oculus-first-contact --seconds 120
   uv run frameport fex settings oculus-first-contact dxvk_disable_msaa=null

   uv run frameport fex settings oculus-first-contact dxgi_max_frame_latency=1
   uv run frameport fex bench oculus-first-contact --seconds 120
   uv run frameport fex settings oculus-first-contact dxgi_max_frame_latency=null

   uv run frameport fex settings oculus-first-contact eye_scale=1.25
   uv run frameport fex bench oculus-first-contact --seconds 120
   uv run frameport fex settings oculus-first-contact eye_scale=null

   uv run frameport fex settings oculus-first-contact 'fex={"FEX_X87REDUCEDPRECISION":1}'
   uv run frameport fex bench oculus-first-contact --seconds 120
   uv run frameport fex settings oculus-first-contact fex=null

   uv run frameport fex settings oculus-first-contact wine_large_address_aware=false
   uv run frameport fex bench oculus-first-contact --seconds 120
   uv run frameport fex settings oculus-first-contact wine_large_address_aware=null

   uv run frameport fex settings oculus-first-contact ntsync=false
   uv run frameport fex bench oculus-first-contact --seconds 120
   uv run frameport fex settings oculus-first-contact ntsync=null

   uv run frameport fex settings oculus-first-contact fp_mem_zero=true
   uv run frameport fex bench oculus-first-contact --seconds 120
   uv run frameport fex settings oculus-first-contact fp_mem_zero=null
   ```

   The baseline covers eye divisor 1.5, ntsync on, and ZERO off. MSAA off previously improved GPU load but reduced
   edge quality. Compare hitches, frame span, GPU time and thread wait distributions; this stream makes no claim
   that X87 or large-address-awareness helps. GE may ignore the latter for a 64-bit game. Test image quality later
   in an ordinary worn-headset Play session; the headless bench intentionally refuses that session.

   Script alternative for a single run (do not run this in addition unless another run is intended):

   ```sh
   uv run python scripts/rift_fex_bench.py oculus-first-contact --seconds 120
   ```

5. Interrupt a run by sending SIGTERM to the `python3 -I .../fexrift.py bench` helper **on the Frame**, after checking
   its PID with `pgrep -af '[f]exrift.py bench'`. Use `kill -TERM <that-pid>` (never a broad pkill). Expected: interrupted
   result, game stopped, compositor standby enabled and original screen timeout restored. Also check refusal while
   the headset is worn and a deliberately invalid setting, e.g. `eye_scale=1.4`, without launching anything.
   On the Frame, this reads the two settings without printing any account data:

   ```sh
   python3 -I - <<'PY'
   import ctypes as C, importlib.util, os
   path = os.path.expanduser('~/.local/share/frameport/fexrift/artifacts/fexrift.py')
   spec = importlib.util.spec_from_file_location('fx', path)
   fx = importlib.util.module_from_spec(spec); spec.loader.exec_module(fx)
   core, method, settings = fx.vr_settings()
   try:
       for key, idx, typ in [('pauseCompositorOnStandby', 5, C.c_bool), ('turnOffScreensTimeout', 7, C.c_float)]:
           err = C.c_int()
           value = method(settings, idx, typ, C.c_char_p, C.c_char_p, C.POINTER(C.c_int))(
               b'power', key.encode(), C.byref(err))
           print(key, value, 'error', err.value)
   finally:
       method(core, 1, None)()
   PY
   ```

   Record these values before the test. Afterwards expect `pauseCompositorOnStandby True error 0` and the original
   positive timeout with error 0. Confirm the physical display sleeps. With guest `FP_MEM_DEBUG=1`, ZERO reuse logs
   should show `fp_mem: zero calls ..., bytes ..., failures 0`. This debug variable can be set manually in a test
   launcher for one run; remove that line afterwards (it is deliberately outside the user environment whitelist).

## Undo and remaining device questions

Reset every override (no login or game files removed):

```sh
uv run frameport fex settings oculus-first-contact dxvk_disable_msaa=null dxgi_sync_interval=null dxgi_max_frame_latency=null eye_scale=null motion_smoothing=null cores=null ntsync=null fp_mem=null bigcache_mb=null fp_mem_zero=null fex=null wine_large_address_aware=null
```

To remove this experiment entirely, restore the integration's previous launcher/module and guest artifact, then
remove only `settings/<app>.json`, `settings/<app>.dxvk.conf` and `revive-scales/` after stopping all FEX games.
Do not remove `compat/pfx`; it holds the owner's login and installed games. For emergency display restoration on the
Frame (replace 300 with the previously recorded timeout):

```sh
R="$HOME/.local/share/frameport/fexrift"
python3 -I "$R/artifacts/fexrift.py" vrsetting '{"section":"power","key":"pauseCompositorOnStandby","kind":"b","value":true}'
python3 -I "$R/artifacts/fexrift.py" vrsetting '{"section":"power","key":"turnOffScreensTimeout","kind":"f","value":300}'
```

Unverified: SteamVR's availability of the pinned historical compositor/system interfaces; whether scene focus PID
matches the FEX game's host PID; whether the HMD reports Standby while compositor rendering is forced; how many
usable game frames remain after runtime startup within 120 seconds; fdinfo visibility on this OS; the cost of ZERO
on the device; all visual/entitlement behavior for other games. Failures must stay visible, not be replaced by fake
performance numbers. If startup consumes the window, report `valid=false` and adjust startup in the owning stream
before drawing conclusions; do not silently extend the bounded run.

## Offline validation and integration

Run `uv run pytest -q` in a normal environment and `uv run python scripts/patch_docs.py --check`. In this sandbox,
use a writable UV cache and the already installed environment:

```sh
UV_CACHE_DIR=/tmp/frameport-perf-uv uv run --offline --no-sync pytest -q
UV_CACHE_DIR=/tmp/frameport-perf-uv uv run --offline --no-sync pytest -q tests/test_fex_perf.py tests/test_fexrift.py tests/test_fexrift_ext.py
```

The original worktree's Git common directory is mounted read-only. Commits are therefore in the local, writable
repository `/tmp/frameport-rift-perf.git` (same base and `rift/perf` ref) and exported as
`/tmp/frameport-rift-perf.bundle`. The worktree contains the complete edits. A maintainer with writable original
Git metadata can fetch that bundle and fast-forward/cherry-pick the commits; do not discard the worktree edits
until integration is confirmed. Local-only instruction files and `.home` are excluded from every commit.

Validation result in this environment: 80 focused tests passed, 1 skipped; Ruff passed for the three new Python
files; `scripts/patch_docs.py --check` passed; `git diff --check` passed. The full suite ran: **1676 passed,
43 skipped, 11 failed, 16 errors**. Every remaining failure/error raised `PermissionError: [Errno 1] Operation not
permitted` when existing pairing/download/live-stream tests attempted to create sockets. This sandbox forbids
sockets, including loopback; these tests were not disabled or modified. A clean full-suite pass remains an
integration check in a normal environment. The complete short traceback log is
`/tmp/frameport-rift-perf-pytest.log`.
