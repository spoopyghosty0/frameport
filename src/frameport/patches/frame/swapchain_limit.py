"""OVRPort's dispatcher aborts on swapchains wider or taller than 4096 px; raise that guard so the runtime decides."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, Suggestion, register

DISPATCHER = "libopenxr_loader.so"  # overport's dispatcher (the runtime-specific loader is libopenxr_loader_generic.so)
FUNCS = ("xrCreateSwapchain", "xrCreateSwapchainAndroidSurfaceKHR")
CMP_4096 = 0x7140041F  # cmp wN, #1, lsl #12   (subs wzr, wN, #0x1000)
CMP_MASK = 0xFFFFFC1F  # everything but Rn
LIMIT_UNITS = 4  # 4 << 12 = 16384 (the Frame's runtime reports 8192 max and rejects larger sizes itself)
VIDEO_LIBS = ("libavcodec", "libffmpeg", "libvlc", "libmpv", "libijkffmpeg")


def raise_swapchain_limit(data: bytes) -> tuple[bytes | None, int]:
    """Rewrite the `cmp wN, #4096` size guards at the start of OVRPort's swapchain entry points."""
    try:
        dynsym = elf._elf(data).get_section_by_name(".dynsym")
    except Exception:  # noqa: BLE001
        return None, 0
    if dynsym is None:
        return None, 0
    segs, out, n = elf.load_segments(data), bytearray(data), 0
    for name in FUNCS:
        found = dynsym.get_symbol_by_name(name)
        if not found or not found[0]["st_value"]:
            continue
        off = elf.vaddr_to_offset(segs, found[0]["st_value"])
        if off is None:
            continue
        for at in range(off, off + min(found[0]["st_size"] or 256, 256), 4):  # the guard sits in the prologue
            word = int.from_bytes(out[at:at + 4], "little")
            if word & CMP_MASK == CMP_4096:
                out[at:at + 4] = ((word & ~(0xFFF << 10)) | (LIMIT_UNITS << 10)).to_bytes(4, "little")
                n += 1
    return (bytes(out) if n else None), n


class SwapchainLimit(Patch):
    id = "frame.swapchain_limit"
    title = "Allow swapchains larger than 4096 px"
    description = ("OVRPort's OpenXR dispatcher aborts the app ('Wrong createInfo size', SIGABRT in "
                   "libopenxr_loader.so xrCreateSwapchain) when a swapchain is wider or taller than 4096 px, which "
                   "video players do for 8K video or big theater textures (for example 4XVR). Raises the guard to "
                   "16384 px so the Frame's runtime (8192 px max) decides instead.")
    order = 45
    default_on = True

    def detect(self, a):
        video = sorted(lib for lib in a.libs if lib.lower().startswith(VIDEO_LIBS))
        if video:
            return Suggestion(True, f"Video player ({', '.join(video[:2])}): creates swapchains larger than 4096 px "
                                    "(8K video, theater textures), which OVRPort's dispatcher aborts on.")
        return Suggestion(True, "Recommended for every game: OVRPort aborts on swapchains larger than 4096 px; "
                                "the Frame's runtime handles them.")

    def applies(self, a):
        return a.xr == "OpenXR"

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if not ws.has(ws.lib(DISPATCHER)):
            return False
        fixed, n = raise_swapchain_limit(ws.read_lib(DISPATCHER))
        if fixed:
            ws.put(ws.lib(DISPATCHER), fixed)
            ctx.notes.append(f"{DISPATCHER}: swapchain size guard raised to 16384 px ({n} check(s))")
        return bool(fixed)


register(SwapchainLimit)
