"""Answers Meta's "latest version of this app" request (`ovr_Application_GetVersion`), which OVRPort's platform loader
never answers.

OVRPort's libovrplatformloader.so (runtimes 3.4.3-23204ea and -aa54c3f) exports `ovr_Application_GetVersion` as
`mov x0, xzr; ret`: the request id is 0 and no message ever comes back. Meta's C# wrapper (Oculus.Platform
`Request<T>.OnComplete`) then logs "An unknown error occurred. Request failed." and never calls the callback. A game
that waits for the version check before it loads its menu stays on a black screen: C-Smash VRS (GitHub #503) starts the
check in `OculusNetworkManager.CheckLatestVersion`, its `PlatformNetworkManager` stays in CHECKING_UPDATE, and
`MenuSceneManager.Initialise` waits for that state to change before it loads the menu scene (so no music, no models).

The fix lives in the language-pack library (native/langpack, `libfp_langpack.so`, the loader's first DT_NEEDED with the
loader's own exports hidden): with the marker byte `@FPAPPVER@` set it answers the request at once with an error message
(`ovrMessage_Application_GetVersion`, IsError): Meta's store can't be asked on the Frame, as on a Quest without a
connection to Meta. No version is made up, so a game never reads "up to date" or "update required" from it. If the
loader answers the call itself (request id other than 0), its answer is kept. When no other patch linked the library,
language packs stay off in it (`@FPLANGS@` = '0').
"""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, Suggestion, register
from . import artifact
from .langpack import APPVER_MARK, EXPORTS, LIB, LOADER, with_app_version, with_metadata, without_langs

# games seen waiting for this answer (no analysis flag: the game's code has to be read to know it waits)
WAITING_GAMES = {
    "com.wolfandwood.csmashvs": "C-Smash VRS",  # GitHub #503
}


class OvrAppVersion(Patch):
    id = "frame.ovr_app_version"
    title = "Answer Meta's app version check"
    description = ("OVRPort's platform library never answers Meta's \"latest version of this app\" request "
                   "(ovr_Application_GetVersion returns 'no request'), so Meta's Unity wrapper logs \"An unknown "
                   "error occurred. Request failed.\" and a game that waits for the check before its menu stays on a "
                   "black screen (e.g. C-Smash VRS). Adds the small platform library FramePort also uses for language "
                   "packs and answers that request at once with an error (Meta's store can't be reached), as a "
                   "Quest without a connection to Meta would; no version is made up. Every other platform call goes "
                   "to OVRPort's library unchanged.")
    order = 36  # after frame.ovr_org_scoped_id: links the library only if no earlier patch did
    default_on = True  # suggested only where a game is known to wait for the answer (detect)

    def applies(self, a):
        return LOADER in a.libs and "arm64-v8a" in a.abis

    def detect(self, a):
        if not self.applies(a):
            return None
        name = WAITING_GAMES.get(getattr(a, "package", "") or "")
        if name:
            return Suggestion(True, f"{name} waits for Meta's app version check, which OVRPort's platform "
                                    "library never answers (black screen before the menu).")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        if ws.abi != "arm64-v8a" or not ws.has(ws.lib(LOADER)):
            return False
        loader = ws.read_lib(LOADER)
        if LIB not in elf.needed(loader):  # no earlier patch linked the library: do it here
            patched, hidden = elf.hide_exports(loader, EXPORTS)
            if "ovr_Application_GetVersion" not in hidden:
                ctx.notes.append("the platform loader has no ovr_Application_GetVersion: nothing to replace")
                return False
            ws.put(ws.lib(LOADER), elf.add_needed(patched, LIB))
            lib = with_metadata(artifact(ws.abi, LIB), (ctx.analysis.version or "").strip())
            ws.put(ws.lib(LIB), without_langs(lib))  # only the version check: language packs pass through
        lib = ws.read_lib(LIB)
        flagged = with_app_version(lib)
        if flagged == lib:
            return False
        ws.put(ws.lib(LIB), flagged)
        ctx.notes.append(f"Meta's app version check answered by {LIB} (with an error: no store to ask)")
        return True

    def validate(self, ctx: ApkContext):
        ws = ctx.ws
        if not ws.has(ws.lib(LOADER)):
            return []
        linked = LIB in elf.needed(ws.read_lib(LOADER))
        enabled = ws.has(ws.lib(LIB)) and APPVER_MARK + b"1" in ws.read_lib(LIB)
        return [("App version check answered (platform library linked)", linked and enabled, LIB)]


register(OvrAppVersion)
