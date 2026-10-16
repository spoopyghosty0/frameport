# x86_64 Wine under FEX: Rift games with Meta's runtime on the Frame (experimental)

Sources of `artifacts/fexrift/` (prebuilt, with `SHA256SUMS`) for the experimental mode `frameport fex ...`
(FRAMEPORT_EXPERIMENTAL_FEX=1; PC side `src/frameport/fexrift.py`, Frame side `agent/fexrift.py`). Research notes:
CLAUDE.md, "Rift via x86_64 Wine under FEX". Rebuild order: `prep_ge_wine.sh` (GE's exact Wine source) ->
`build_pe_dlls.sh` (x86_64 host, llvm-mingw) + `build_wineserver.sh` (aarch64 host, e.g. the Frame; then `strip`) +
fp_mem.so (command below).

| File | What |
|---|---|
| `pipesend.c` | `fp_pipesend.exe`, built by `build_pe_dlls.sh` with llvm-mingw. Sends a browser's `oculus://link/frl_login` callback into the running Meta app's `\\.\pipe\oculus`; reads UTF-8 from stdin so SSH forwarding never puts credentials in argv. Retries a busy/missing pipe for 20 seconds. Does not print the link. Add the rebuilt exe to `artifacts/fexrift/SHA256SUMS`. Pipe framing still needs the device check in `docs/rift-fex/login.md`. |
| `prep_ge_wine.sh` | Recreates the exact Wine source of a GE-Proton release (its Wine and wine-staging submodules plus GE's patches) by running only the WINE section of GE's `protonprep-valve-staging.sh`. |
| `build_wineserver.sh` | Builds a **native aarch64 wineserver** from that tree on an aarch64 host (the Frame has gcc, make, flex and bison). It speaks the same protocol as GE's x86_64 server (GE-Proton11-7: protocol 938, 321 requests; plain Valve Wine at the same commit lacks 15 of GE's requests). `-DFP_EMULATED_X86_64` gives it the x86_64 machine model, because its clients are x86_64 processes under FEX. Use it by having the `wineserver` wrapper exec it instead of the FEX-run x86_64 server. |
| `build_pe_dlls.sh` + `patches/` | The four Wine DLLs FramePort replaces in GE-Proton, built from GE's tree + only these patches: `crypt32-chain-newest-issuer` (Windows' newest-issuer rule between equal chains: Meta's OculusAppFramework signer check needs the 4-certificate chain), `sechost-realtime-trace-consumer` (OpenTraceW real-time sessions: the runtime's ETW USB tracing), `dnsapi-dnsservice-functions` (DnsService* from Wine 11.19: Air Link mDNS, OVRServer quits without them), `devenum-devicewatcher-handlers` (DeviceWatcher Updated/Removed handler lists). Exports equal GE's (+8 DnsService*). Also builds `windows.devices.wifi.dll` from `wifi_stub.c`: a Windows.Devices.WiFi.WiFiAdapter class (GetDeviceSelector matches nothing) that the logged-in runtime needs. |
| `fp_mem.c` | x86_64 `LD_PRELOAD` for the guest. `FP_BIGCACHE_MB=<n>` keeps released anonymous regions of at least n MB (up to 12, 6 GB in total) and returns them when the same start address is mapped again; a shorter request releases the rest, a longer one maps only the extra part fresh. In the headset First Contact allocated 0.5-1.6 GB blocks several times at once, which the first, single-block version missed. `FP_MEM_ZERO=1` (v3) lazily zeroes only the reused range with `MADV_DONTNEED` before returning it; unset keeps v2 behavior. Debug counters report advice calls/bytes/failures. `FP_THP=1` sets `MADV_HUGEPAGE` on large writable regions. From the repo root on the build host, build with `gcc -O2 -shared -fPIC -o artifacts/fexrift/fp_mem.so native/fexwine/fp_mem.c -ldl -lpthread` on an x86_64 host (needs glibc 2.34). **Under FEX, pass it as `FEX_ENV=LD_PRELOAD=<path>`**: Valve's `fex-compat-tool` deletes `LD_PRELOAD`. |

## Measurements: Oculus First Contact, headless, dev Frame, 2026-10-10

SteamVR compositor frame timings were read through `IVRCompositor::GetFrameTimings`.

| Setup | Hitches per 20 s | Frames timed out | Game CPU per frame |
|---|---|---|---|
| FEX-run x86_64 wineserver, server sync | 17 | 40 % | 10.4 ms |
| Native wineserver, server sync | 9 | 32 % | 10.4 ms |
| Native wineserver + ntsync | 0-4 | 9-10 % | 6.2 ms |
| Native wineserver + ntsync + `FP_BIGCACHE_MB=256` | **0** (3 of 3 runs) | 5.4-5.9 % | 5.3 ms |

Each remaining hitch (one app frame shown about 9 times) was Unreal's large-block allocator:
1. It calls VirtualAlloc for 514 MB (First Contact's exe RVA 0x1a188e) every 1-8 s.
2. A game task fills the block with a single `rep stos`/`rep movs` while the game thread waits for it.
3. Another thread then frees the block.

Under FEX every 4 KiB page of it faults in and gets zeroed and charged by the kernel: about 130k faults and 110-140 ms per hitch.

Huge pages work in isolation (514 MB in 22 ms instead of 212 ms), but fail in practice. On a Frame that has been up for a while, memory is fragmented, so allocations fall back to 4 KiB pages (`thp_fault_fallback` far above `thp_fault_alloc`). The kernel's `defrag=madvise` setting also lets those attempts stall in compaction.

The block cache returns the region with stale contents instead of zeroed pages. That is only safe for games that overwrite the whole block, so it has to stay a per-game setting.


## Launch-time experiments and bounded measurements

The First Contact numbers above are research measurements, not new results from the perf extension. Subsequent
owner tests found the remaining bottleneck to be GPU work: about 55 percent game GPU busy at 36 fps, about
20 percent for the compositor, and 5–10 ms CPU ApplicationTime. With per-app motion smoothing off, it reached
72 fps with occasional misses. DXVK `d3d11.disableMsaa=True` reduced measured game GPU busy from 91 to 69 percent;
the owner reported much better performance and rougher edges. `r.MobileMSAA=2` prevented rendering.

`agent/fexrift_perf.py` adds validated per-game settings read at every launch, including MSAA, frame latency,
Revive eye-size divisors, core affinity, ntsync, guest cache options and a small FEX environment whitelist.
`FP_MEM_ZERO=1` restores zero-page semantics for cache reuse, with the cost of page faults on later touches.
When advice fails the region is evicted and mapped fresh. ZERO is opt-in; the default cache remains unzeroed.
The host test uses fake mmap/munmap/madvise to check full, short and long reuse and advice-failure fallback.
The real x86_64 guest artifact and its checksum must be rebuilt by the maintainer before ZERO experiments.

`frameport fex bench APP --seconds 120` requests one headless run with a total launch/sampling window of at most
120 seconds, then stops Wine and the launcher and restores compositor standby and the screen timeout. It refuses
an active game/login or a headset outside standby. It reports deduplicated compositor frame timings, hitches
(frames presented at least three times), game DRM fdinfo GPU deltas and sampled thread wait states. These new
paths have only offline tests; the pinned OpenVR interfaces and process attribution need device validation.
See [the perf stream notes](../../docs/rift-fex/perf.md) for exact build, baseline, experiment and undo commands.
