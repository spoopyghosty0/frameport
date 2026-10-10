"""BONELAB 1.2974 (and other Stress Level Zero builds) ship libSLZQuestNative.so, a Unity native plugin that hooks
Unity's Vulkan start-up (IUnityGraphicsVulkanV2 interception: its vkCreateInstance/vkCreateDevice wrappers raise the
API version, enable device features and check its pipeline cache) and replaces Unity's vkCreateSampler (anisotropy
<= 4, LOD bias -0.25). On the Frame its vkCreateInstance wrapper calls through an invalid pointer right after
OVRPlugin's pre-init OpenXR instance is destroyed: a jump into an unloaded library, to address 0, or a hang, depending
on the run. The plugin's two registrations with Unity are turned into no-ops (in place, 4 bytes each), so Unity runs
Vulkan itself; the plugin still loads. Its sampler hook must go too: without the device hook it never learns the real
vkCreateSampler."""
from __future__ import annotations

import struct

from ...analysis import elf
from ..base import ApkContext, Patch, Suggestion, register

LIB = "libSLZQuestNative.so"
ON_LOAD = "RenderAPI_Vulkan_OnPluginLoad"  # registers the Vulkan-init interception
GFX_INIT = "_ZN8PluginVk12GfxEventInitEv"  # PluginVk::GfxEventInit: InterceptVulkanAPI("vkCreateSampler", hook)
SAMPLER = b"vkCreateSampler\0"
NOP = 0xD503201F
MOV_X1_XZR, MOV_W2_WZR = 0xAA1F03E1, 0x2A1F03E2  # AddInterceptInitialization(func, userdata = NULL, flags = 0)


def _is_blr(insn: int) -> bool:
    return insn & 0xFFFFFC1F == 0xD63F0000


def _x0_address(data: bytes, segs, at: int, back: int = 8) -> int | None:
    """The address `adrp xN, page` + `add x0, xN, #imm` puts in x0 within `back` instructions before `at`."""
    for i in range(1, back):
        add_va = at - 4 * i
        add_off = elf.vaddr_to_offset(segs, add_va)
        if add_off is None:
            return None
        add = struct.unpack_from("<I", data, add_off)[0]
        if add & 0xFFC0001F != 0x91000000:  # add x0, xN, #imm12 (no shift)
            continue
        rn, imm12 = (add >> 5) & 0x1F, (add >> 10) & 0xFFF
        for j in range(i + 1, back + 4):
            adrp_off = elf.vaddr_to_offset(segs, at - 4 * j)
            if adrp_off is None:
                return None
            adrp = struct.unpack_from("<I", data, adrp_off)[0]
            if adrp & 0x9F000000 == 0x90000000 and adrp & 0x1F == rn:
                imm = ((adrp >> 5) & 0x7FFFF) << 2 | (adrp >> 29) & 3
                if imm & (1 << 20):
                    imm -= 1 << 21
                return ((at - 4 * j) & ~0xFFF) + (imm << 12) + imm12
        return None
    return None


def _function(data: bytes, name: str) -> tuple[int, int] | None:
    sec = elf._elf(data).get_section_by_name(".dynsym")
    for sym in (sec.get_symbol_by_name(name) or []) if sec else []:
        if sym["st_value"] and sym["st_size"]:
            return sym["st_value"], sym["st_size"]
    return None


def find_registrations(data: bytes) -> list[int]:
    """File offsets of the plugin's two `blr` registrations (interception, vkCreateSampler), [] when not this plugin."""
    segs = elf.load_segments(data)
    sampler = data.find(b"\0" + SAMPLER) + 1
    on_load, gfx_init = _function(data, ON_LOAD), _function(data, GFX_INIT)
    if not on_load or not gfx_init or sampler <= 0:
        return []
    out = []
    for (start, size), wanted in ((on_load, "intercept"), (gfx_init, "sampler")):
        for va in range(start, start + size, 4):
            off = elf.vaddr_to_offset(segs, va)
            if off is None or not _is_blr(struct.unpack_from("<I", data, off)[0]):
                continue
            prev = [struct.unpack_from("<I", data, o)[0] for k in range(1, 5)
                    if va - 4 * k >= start and (o := elf.vaddr_to_offset(segs, va - 4 * k)) is not None]
            if wanted == "intercept" and MOV_X1_XZR in prev and MOV_W2_WZR in prev:
                out.append(off)
                break
            # the string lives in the first (read-only) segment, where file offset == address
            if wanted == "sampler" and _x0_address(data, segs, va) == sampler:
                out.append(off)
                break
    return out if len(out) == 2 else []


class SlzVulkanHooks(Patch):
    id = "frame.slz_vulkan_hooks"
    title = "Stress Level Zero: let Unity start Vulkan itself"
    description = ("Stress Level Zero's graphics plugin (libSLZQuestNative.so, for example BONELAB 1.2974) hooks "
                   "Unity's Vulkan start-up and its texture samplers. On the Frame the hook calls an invalid function "
                   "while Vulkan starts, and the game crashes or hangs before the first frame. The plugin's two hooks "
                   "are switched off, so Unity starts Vulkan itself. Its pipeline cache isn't used: the first start "
                   "spends about half a minute prewarming shaders.")
    order = 47
    revision = 1

    def applies(self, a):
        return "arm64-v8a" in a.abis and LIB in a.libs

    def detect(self, a):
        if self.applies(a):
            return Suggestion(True, "Stress Level Zero's graphics plugin crashes Unity's Vulkan start-up on the Frame "
                                    "(for example BONELAB 1.2974).")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        name = ws.lib(LIB)
        if ws.abi != "arm64-v8a" or not ws.has(name):
            return False
        data = ws.read(name)
        sites = find_registrations(data)
        if not sites:
            return False
        buf = bytearray(data)
        for off in sites:
            struct.pack_into("<I", buf, off, NOP)
        ws.put(name, bytes(buf))
        ctx.notes.append(f"{LIB}: Vulkan interception and vkCreateSampler hook switched off "
                         f"({', '.join(hex(o) for o in sites)})")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        name = ws.lib(LIB)
        if not ws.has(name):
            return []
        left = find_registrations(ws.read(name))
        return [("SLZ Vulkan hooks off", not left, "" if not left else "still registered")]


register(SlzVulkanHooks)
