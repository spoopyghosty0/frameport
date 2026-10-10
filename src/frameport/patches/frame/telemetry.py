"""Quest-only telemetry lookups that abort the app on other devices."""
from __future__ import annotations

import re

from ...analysis import elf
from ...core.paths import artifacts_dir
from ..base import ApkContext, Patch, Suggestion, register

CLASS = b"com/oculus/os/AnalyticsEvent\0"


def patch_metaxr_telemetry(data: bytes) -> bytes | None:
    """Meta XR Audio (Unreal build) looks up com/oculus/os/AnalyticsEvent via JNI FindClass and leaves the
    ClassNotFoundException pending, which aborts ART. Replace that FindClass call (blr) with `mov x0, xzr` so the
    library takes its 'class missing' path cleanly. Pattern: adrp x1,page; add x1,x1,#lo; ...; ldr xN,[xN,#0x30];
    blr xN; cbz ..."""
    if CLASS not in data or not elf.is_64bit(data):
        return None
    segs = elf.load_segments(data)
    ins = list(elf.text_instructions(data))
    out = bytearray(data)
    patched = 0
    for i in range(len(ins) - 2):
        addr, mnem, ops = ins[i]
        if not (mnem == "ldr" and re.fullmatch(r"x(\d+), \[x\1, #0x30\]", ops)
                and ins[i + 1][1] == "blr" and ins[i + 2][1] == "cbz"):
            continue
        page = low = None
        for j in range(i - 1, max(i - 14, 0), -1):
            m2, o2 = ins[j][1], ins[j][2]
            if m2 == "add" and low is None:
                m = re.fullmatch(r"x1, x1, #0x([0-9a-f]+)", o2)
                if m:
                    low = int(m[1], 16)
            if m2 == "adrp":
                m = re.fullmatch(r"x1, #0x([0-9a-f]+)", o2)
                if m:
                    page = int(m[1], 16)
                    break
        if page is None or low is None:
            continue
        so = elf.vaddr_to_offset(segs, page + low)
        if so is None or not data[so:].startswith(CLASS):
            continue
        blr = elf.vaddr_to_offset(segs, ins[i + 1][0])
        out[blr:blr + 4] = bytes.fromhex("e0031faa")  # mov x0, xzr
        patched += 1
    return bytes(out) if patched else None


class MetaXrTelemetry(Patch):
    id = "frame.metaxr_telemetry"
    title = "Meta XR Audio: skip Quest telemetry class"
    description = ("Unreal builds of Meta XR Audio abort ('ClassNotFoundException com.oculus.os.AnalyticsEvent') on "
                   "non-Quest devices. Patches that one JNI FindClass call so the library continues (for example NOPE "
                   "Challenge).")
    order = 70
    default_on = True

    def detect(self, a):
        if any(lib.startswith("libmetaxraudio") for lib in a.libs):
            return Suggestion(True, "Meta XR Audio present; patched automatically if it has the telemetry lookup.")
        return None

    def applies(self, a):
        return a.engine == "Unreal" and any(lib.lower().startswith("libmetaxraudio") for lib in a.libs)

    def apply(self, ctx: ApkContext) -> bool:
        changed = False
        for lib in ctx.ws.libs():
            if lib.startswith("libmetaxraudio"):
                fixed = patch_metaxr_telemetry(ctx.ws.read_lib(lib))
                if fixed:
                    ctx.ws.put(ctx.ws.lib(lib), fixed)
                    changed = True
        return changed


class OculusOsStubs(Patch):
    id = "frame.oculusos"
    title = "Oculus OS telemetry class stubs"
    description = ("Adds no-op com.oculus.os.AnalyticsEvent / UnifiedTelemetryLogger classes for native code that "
                   "looks them up through the app class loader (for example Nano, NOPE Challenge).")
    order = 71

    def detect(self, a):
        refs = a.extra.get("oculus_os_refs") or []
        if any(r.startswith("libmetaxraudio") for r in refs):
            return Suggestion(True, "The Unreal build of Meta XR Audio looks up com.oculus.os.AnalyticsEvent and "
                                    "aborts without it (for example NOPE Challenge).")
        if len(refs) >= 2:
            return Suggestion(True, "Several Meta SDK libraries look up com.oculus.os.AnalyticsEvent "
                                    f"({', '.join(refs)}); games like this abort without the stub (for example Nano).")
        if a.oculus_os_classes:
            return Suggestion(False, f"{', '.join(refs) or 'Native code'} references com.oculus.os.AnalyticsEvent; "
                                     "usually harmless (for example Batman: Arkham Shadow and LEGO Bricktales run "
                                     "without it). Enable if the game aborts with ClassNotFoundException.")
        return None

    def applies(self, a):
        return a.oculus_os_classes

    def apply(self, ctx: ApkContext) -> bool:
        ws = ctx.ws
        uses = any(b"com/oculus/os/AnalyticsEvent" in ws.read_lib(lib) for lib in ws.libs())
        dexes = sorted(n for n in ws.names() if re.fullmatch(r"classes\d*\.dex", n))
        if not uses or any(b"Lcom/oculus/os/AnalyticsEvent;" in ws.read(n) for n in dexes):
            return False
        ws.put(f"classes{len(dexes) + 1}.dex", (artifacts_dir() / "dex/oculusos-stubs.dex").read_bytes())
        return True


register(MetaXrTelemetry)
register(OculusOsStubs)
