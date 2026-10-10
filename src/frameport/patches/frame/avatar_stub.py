"""Meta's Avatar SDK loader (libovravatarloader.so) needs Meta's Horizon app for its driver. On the Frame it fails
(ovrAvatar_Initialize: Failed to load AvatarSDK driver), tries to start Meta's SystemActivities error screen, which
doesn't exist either, and aborts the game ("OVRAvatar-Loader: DisplayErrorAndExit: Failed to launch
SystemActivities", e.g. BlazeRush). The loader is replaced by a library with the same functions, each doing nothing:
the game gets no avatar (no message ever arrives, every handle is NULL) and goes on without one.
"""
from __future__ import annotations

from ...analysis import elf
from ...analysis.stubgen import build_stub_library
from ..base import ApkContext, Patch, register

AVATAR = "libovravatarloader.so"


class AvatarStub(Patch):
    id = "frame.avatar_stub"
    title = "Skip Meta avatars"
    description = ("Meta's avatar library needs Meta's Horizon app, which the Frame doesn't have; it then stops the "
                   "game at start (\"Failed to launch SystemActivities\", for example BlazeRush). Replaces it with a "
                   "library that does nothing, so the game runs without Meta avatars.")
    order = 58

    def applies(self, a):
        return AVATAR in a.libs and "arm64-v8a" in a.abis

    def detect(self, a):
        return None  # triage avatar-driver-missing: games that ship the library without starting it at launch run fine

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a" or not ws.has(ws.lib(AVATAR)):
            return False
        data = ws.read_lib(AVATAR)
        if not elf.is_elf(data):
            return False
        exports = sorted(s for s in elf.dyn_symbols(data, True) if s.startswith("ovr"))
        stub = build_stub_library(exports, soname=AVATAR)
        if not exports or data == stub:
            return False
        ws.put(ws.lib(AVATAR), stub)
        ctx.notes.append(f"{AVATAR}: {len(exports)} avatar functions do nothing (Meta's avatar driver isn't available)")
        return True


register(AvatarStub)
