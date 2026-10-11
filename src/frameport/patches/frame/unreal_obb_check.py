"""Unreal (UE4/UE5) games with an OBB start through Epic's DownloaderActivity (GameActivity.onResume starts it for a
result before the engine's main init). It checks that every OBB exists with the name and size compiled into the APK
(OBBData), and with the manifest meta-data `com.epicgames.ue4.GameActivity.bVerifyOBBOnStartUp` = true it then reads
the whole OBB and compares each zip entry's CRC32 (once: the result is cached in files/cacheFile.txt by the OBB's
modified time). A CRC mismatch shows an error screen that waits for a button, and the check itself shows a progress
screen while it reads gigabytes; Lepton runs VR apps headless, so neither is visible and the game seems to hang on
the downloader (GitHub #159, Contractors). Setting the meta-data to false skips only that CRC pass: the name + size
check stays (a missing or wrong-size OBB still stops at the downloader). One 4-byte manifest value changes."""
from __future__ import annotations

from ...analysis.detect import UE_VERIFY_OBB_KEY
from ...apk import axml
from ..base import ApkContext, Patch, Suggestion, register

MANIFEST = "AndroidManifest.xml"


class UnrealSkipObbCheck(Patch):
    id = "frame.unreal_skip_obb_check"
    title = "Unreal: start without the OBB check"
    description = ("Unreal games can check their whole data file (.obb) before starting (bVerifyOBBOnStartUp). On "
                   "the Frame that check's progress and error screens are invisible (Lepton runs VR games without "
                   "an Android window), so the game seems to hang on its DownloaderActivity, for example Contractors. "
                   "This turns the check off in the manifest; Unreal still checks that the OBB is there with the "
                   "right size, so a missing or wrong-version OBB still stops the game.")
    order = 23
    needs_vr = False

    def applies(self, a):
        return bool((a.extra or {}).get("unreal_verify_obb"))

    def detect(self, a):
        if self.applies(a):
            return Suggestion(True, "Unreal checks the OBB's contents before starting (bVerifyOBBOnStartUp); on the "
                                    "Frame a failed or slow check can't be seen and the game seems to hang.")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        fixed = axml.set_meta_data_bool(ctx.ws.read(MANIFEST), UE_VERIFY_OBB_KEY, False)
        if fixed:
            ctx.ws.put(MANIFEST, fixed)
            ctx.notes.append("bVerifyOBBOnStartUp=false")
        return bool(fixed)

    def validate(self, ctx):
        meta = axml.meta_data(ctx.ws.read(MANIFEST))
        on = [k for k, v in meta.items() if k.endswith(UE_VERIFY_OBB_KEY) and v is True]
        return [("OBB check at start off", not on, ", ".join(on) or "bVerifyOBBOnStartUp=false")]


register(UnrealSkipObbCheck)
