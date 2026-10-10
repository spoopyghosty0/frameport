# x86_64 Wine under FEX: performance pieces (research)

Research for Oculus Rift games on the Steam Frame through x86_64 GE-Proton under FEX (see CLAUDE.md, "Rift via
x86_64 Wine under FEX"). Nothing here is used by FramePort yet, and no binaries are committed.

| File | What |
|---|---|
| `prep_ge_wine.sh` | Recreates the exact Wine source of a GE-Proton release (its Wine and wine-staging submodules plus GE's patches) by running only the WINE section of GE's `protonprep-valve-staging.sh`. |
| `build_wineserver.sh` | Builds a **native aarch64 wineserver** from that tree on an aarch64 host (the Frame has gcc, make, flex and bison). It speaks the same protocol as GE's x86_64 server (GE-Proton11-7: protocol 938, 321 requests; plain Valve Wine at the same commit lacks 15 of GE's requests). `-DFP_EMULATED_X86_64` gives it the x86_64 machine model, because its clients are x86_64 processes under FEX. Use it by having the `wineserver` wrapper exec it instead of the FEX-run x86_64 server. |
| `fp_mem.c` | x86_64 `LD_PRELOAD` for the guest. `FP_BIGCACHE_MB=<n>` keeps released anonymous regions of at least n MB (up to 8, 3 GB in total) and returns them when the same start address is mapped again; a shorter request releases the rest, a longer one maps only the extra part fresh. In the headset First Contact allocated 0.5-1.6 GB blocks several times at once, which the first, single-block version missed. `FP_THP=1` sets `MADV_HUGEPAGE` on large writable regions. Build it with `gcc -O2 -shared -fPIC -o fp_mem.so fp_mem.c -ldl -lpthread` on an x86_64 host (needs glibc 2.34). **Under FEX, pass it as `FEX_ENV=LD_PRELOAD=<path>`**: Valve's `fex-compat-tool` deletes `LD_PRELOAD`. |

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
