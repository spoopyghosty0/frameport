"""SDL apps (SDL2 / LÖVE games, e.g. Dramatic Shape) crash at start in Lepton: its Android has no clipboard service
(`service check clipboard`: not found), getSystemService(CLIPBOARD_SERVICE) returns null and SDLClipboardHandler's
constructor calls addPrimaryClipChangedListener on it (NullPointerException in SDLActivity.onCreate). That one call is
turned into no-ops in classes.dex (in place; other clipboard calls only happen when the app uses the clipboard)."""
from __future__ import annotations

from ...apk.dex import Dex
from ..base import ApkContext, Patch, Suggestion, register

HANDLER = "Lorg/libsdl/app/SDLClipboardHandler;"
LISTENER = ("Landroid/content/ClipboardManager;", "addPrimaryClipChangedListener")


class SdlClipboard(Patch):
    id = "frame.sdl_clipboard"
    title = "SDL apps: start without a clipboard service"
    description = ("Lepton's Android has no clipboard service, and SDL's Java code (SDL2 and LÖVE games, for example "
                   "Dramatic Shape) registers a clipboard listener at start without checking, so the app crashes "
                   "with a NullPointerException in SDLClipboardHandler. That call is skipped (an in-place edit of "
                   "classes.dex).")
    order = 44
    needs_vr = False

    def applies(self, a):
        return bool((a.extra or {}).get("sdl_java"))

    def detect(self, a):
        if self.applies(a):
            return Suggestion(True, "SDL app: it crashes at start in Lepton, which has no clipboard service.")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        changed = False
        for name in sorted(n for n in ctx.ws.names() if n.startswith("classes") and n.endswith(".dex")):
            data = ctx.ws.read(name)
            if HANDLER.encode() not in data:
                continue
            dex = Dex(data)
            targets = set(dex.method_index(*LISTENER))
            done = sum(dex.nop_calls(code, targets) for code in dex.code_items(HANDLER, "<init>")) if targets else 0
            if done:
                ctx.ws.put(name, dex.finish())
                ctx.notes.append(f"{name}: SDLClipboardHandler no longer needs a clipboard service ({done} call)")
                changed = True
        return changed


register(SdlClipboard)
