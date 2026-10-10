"""Team Beef's TBXR ports (Lambda1VR, RTCWQuest, ...) choose their OpenXR setup by headset maker.

Their Java activity does System.loadLibrary("openxr_loader_" + Build.MANUFACTURER) and sets OPENXR_HMD to the same
name; Lepton reports the maker "valve", so the app stops at once (UnsatisfiedLinkError: libopenxr_loader_valve.so not
found). Past that, the native code takes Meta's path only where strstr(OPENXR_HMD, "meta") matches and Pico's
otherwise (XR_PICO_configs_ext and a NULL xrSetConfigPICO call). The patch adds an empty libopenxr_loader_valve.so and
turns the "meta" literal those checks compare with into "alve", which "valve" contains: the Meta path, which OVRPort
translates. Checked in Lambda1VR 1.7.3's libxash.so: all six uses of the literal are these strstr checks.

TBXR also draws through multisampled render-to-texture (glFramebufferTexture2DMultisampleEXT +
glRenderbufferStorageMultisampleEXT) whatever its --msaa setting; on the Frame that framebuffer is incomplete
(GL_FRAMEBUFFER_INCOMPLETE_MULTISAMPLE, black eyes). The GL shim (libglshim.so, linked into the TBXR library) hands
it single-sampled versions of both.
"""
from __future__ import annotations

from ...analysis import elf
from ...analysis.stubgen import build_stub_library
from ..base import ApkContext, Patch, Suggestion, register
from . import artifact
from .vrapi_bridge import SHIM

VENDOR_LOADER = "libopenxr_loader_valve.so"


class TbxrVendor(Patch):
    id = "frame.tbxr_vendor"
    title = "Team Beef ports: treat the Frame as a Meta headset"
    description = ("Team Beef's ports (for example Lambda1VR, RTCWQuest) load an OpenXR library named after the "
                   "headset maker and pick their VR setup by maker; for \"valve\" they stop at start "
                   "(libopenxr_loader_valve.so not found) or take the Pico path. Adds that library and lets them take "
                   "the Meta path.")
    order = 47

    def applies(self, a):
        return bool((a.extra or {}).get("tbxr_libs")) and "arm64-v8a" in a.abis

    def detect(self, a):
        if self.applies(a):
            return Suggestion(True, "Team Beef port: it picks its OpenXR setup by headset maker, which the Frame "
                                    "reports as \"valve\".")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a":
            return False
        changed = False
        for lib in (ctx.analysis.extra or {}).get("tbxr_libs") or []:
            if not ws.has(ws.lib(lib)):
                continue
            data, count = elf.replace_rodata_string(ws.read_lib(lib), "meta", "alve")
            if count:
                ws.put(ws.lib(lib), data)
                ctx.notes.append(f"{lib}: headset checks for \"meta\" match \"valve\" ({count})")
                changed = True
            data = ws.read_lib(lib)
            if "eglGetProcAddress" in elf.dyn_symbols(data, False) and SHIM not in elf.needed(data):
                ws.put(ws.lib(lib), elf.add_needed(data, SHIM))  # its GL comes through eglGetProcAddress
                ctx.notes.append(f"GL shim loaded by {lib} (single-sampled render-to-texture)")
                changed = True
        if changed or ws.has(ws.lib(SHIM)):
            ws.put(ws.lib(SHIM), artifact(ws.abi, SHIM))
        if not ws.has(ws.lib(VENDOR_LOADER)):
            ws.put(ws.lib(VENDOR_LOADER), build_stub_library([], soname=VENDOR_LOADER))
            changed = True
        return changed


register(TbxrVendor)
