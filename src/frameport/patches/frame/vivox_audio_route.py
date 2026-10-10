"""Vivox voice chat (Unity's Vivox package, e.g. Green Hell VR, GitHub #101) crashes at start in Lepton: its Java
audio-route listener (com.vivox.sdk.AudioChangeListener) calls Android 12 AudioManager methods
(getAvailableCommunicationDevices, setCommunicationDevice, ...) without checking the Android version, and Lepton runs
Android 11 (this Vivox build has no version checks at all) → java.lang.NoSuchMethodError. Every method of that class
that calls one of them is made to return at once (in-place classes.dex edit: void methods return, boolean/int ones
return false/0; methods returning objects are left alone). Voice chat then keeps Android's default audio route; nothing
else changes. The calls themselves can't simply be skipped (the following move-result would fail Android's bytecode
verifier), which is why whole methods return instead."""
from __future__ import annotations

from ...apk.dex import Dex
from ..base import ApkContext, Patch, Suggestion, register

LISTENER = "Lcom/vivox/sdk/AudioChangeListener;"
AUDIO_MANAGER = "Landroid/media/AudioManager;"
API31 = ("getAvailableCommunicationDevices", "setCommunicationDevice", "clearCommunicationDevice",
         "getCommunicationDevice", "addOnCommunicationDeviceChangedListener",
         "removeOnCommunicationDeviceChangedListener")


def patch_dex(dex: Dex) -> list[str]:
    """Methods of Vivox's AudioChangeListener that now return at once; [] if nothing applies."""
    targets = {i for name in API31 for i in dex.method_index(AUDIO_MANAGER, name)}
    if not targets:
        return []
    done = []
    for idx, code in dex.class_methods(LISTENER):
        if not dex.invoked(code) & targets:
            continue
        if dex.return_early(code, dex.return_type(idx)):
            done.append(dex.method(idx)[1])
    return done


class VivoxAudioRoute(Patch):
    id = "frame.vivox_audio_route"
    title = "Vivox voice chat: skip Android 12 audio routing"
    description = ("Vivox's voice-chat SDK (for example Green Hell VR) checks the audio route with Android 12 methods "
                   "(AudioManager communication devices) without checking the Android version; Lepton runs Android "
                   "11, so the game crashes at start with a NoSuchMethodError. Those Vivox methods return at once "
                   "instead (an in-place edit of classes.dex); voice chat keeps the default audio route.")
    order = 44
    needs_vr = False

    def applies(self, a):
        return bool((a.extra or {}).get("vivox_api31"))

    def detect(self, a):
        if self.applies(a):
            return Suggestion(True, "Vivox voice chat: it calls Android 12 audio methods, which Lepton (Android 11) "
                                    "lacks, and crashes.")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        changed = False
        for name in sorted(n for n in ctx.ws.names() if n.startswith("classes") and n.endswith(".dex")):
            data = ctx.ws.read(name)
            if LISTENER.encode() not in data:
                continue
            dex = Dex(data)
            done = patch_dex(dex)
            if done:
                ctx.ws.put(name, dex.finish())
                ctx.notes.append(f"{name}: Vivox AudioChangeListener returns at once in {', '.join(sorted(done))}")
                changed = True
        return changed


register(VivoxAudioRoute)
