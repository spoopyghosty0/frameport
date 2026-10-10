"""Unity text fields that close at once on the Frame: make them edit in place, like on a PC.

Unity's text fields (TextMeshPro TMP_InputField, and uGUI's older InputField) open the system on-screen keyboard on
Android and close themselves a frame later when none is visible. Lepton's Android has no on-screen keyboard (Meta's
system keyboard does this job on a Quest), so the field shows a caret for a moment and loses focus: nothing can be
typed (found with Stremio VR, 2026-10-03). Two return values are rewritten so the field behaves as on a PC: it doesn't
wait for a system keyboard and takes key presses as events, from Steam's keyboard (with device.text_input_window) or
the PC keyboard (Type on Frame). The methods are found per game with Cpp2IL (analysis/il2cpp.py)."""
from __future__ import annotations

from ...analysis import elf
from ..base import ApkContext, Patch, Suggestion, register

METADATA = "assets/bin/Data/Managed/Metadata/global-metadata.dat"
RET_FALSE = bytes.fromhex("00008052c0035fd6")  # mov w0, #0 ; ret
RET_TRUE = bytes.fromhex("20008052c0035fd6")   # mov w0, #1 ; ret
# Cpp2IL class file -> {method: what it should return}
TARGETS = {
    "Unity.TextMeshPro/TMPro/TMP_InputField.cs": {"TouchScreenKeyboardShouldBeUsed": RET_FALSE,
                                                  "isKeyboardUsingEvents": RET_TRUE},
    # uGUI's InputField: its LateUpdate returns early (keeps the field) when InPlaceEditing() is true
    "UnityEngine.UI/UnityEngine/UI/InputField.cs": {"TouchScreenKeyboardShouldBeUsed": RET_FALSE,
                                                   "InPlaceEditing": RET_TRUE},
}


def _executable(data: bytes) -> list[tuple[int, int]]:
    return [(s["p_offset"], s["p_offset"] + s["p_filesz"]) for s in elf._elf(data).iter_segments()
            if s["p_type"] == "PT_LOAD" and s["p_flags"] & 1]


def patch_methods(lib: bytes, found: dict[str, dict[str, tuple[int, int]]],
                  targets: dict[str, dict[str, bytes]] | None = None) -> tuple[bytes | None, list[str]]:
    """Write the return values over the methods' first two instructions. found = il2cpp.find_methods' result,
    targets = {class file: {method: RET_*}} (default: the text field methods).
    Returns (patched library or None when nothing changed, notes)."""
    out, notes, changed = bytearray(lib), [], False
    code = _executable(lib)
    for cls, methods in (targets or TARGETS).items():
        for method, new in methods.items():
            off, length = found.get(cls, {}).get(method, (None, 0))
            if off is None:
                continue
            name = f"{cls.rsplit('/', 1)[-1][:-3]}.{method}"
            if off % 4 or length < len(new) or not any(a <= off and off + len(new) <= b for a, b in code):
                raise RuntimeError(f"{name}: offset {off:#x} isn't code in libil2cpp.so (Cpp2IL mismatch)")
            if bytes(out[off:off + len(new)]) == new:
                notes.append(f"{name} already patched")
                continue
            out[off:off + len(new)] = new
            changed = True
            notes.append(f"{name} -> {'true/1' if new == RET_TRUE else 'false/0'} (at {off:#x})")
    return (bytes(out) if changed else None), notes


def patch_field_loads(lib: bytes, method: tuple[int, int], field_offset: int, value: int, name: str
                      ) -> tuple[bytes | None, list[str]]:
    """Inside one method (file offset, length), turn every load of the instance field at field_offset (ldr/ldrh/ldrb
    w<t>, [x<n>, #field_offset]) into mov w<t>, #value: for a getter the compiler inlined into its caller, patching the
    getter alone changes nothing. Refuses when there's no such load (unless already patched) or more than 2."""
    start, length = method
    if start % 4 or not any(a <= start and start + length <= b for a, b in _executable(lib)):
        raise RuntimeError(f"{name}: offset {start:#x} isn't code in libil2cpp.so (Cpp2IL mismatch)")
    kinds = [(0xB9400000, 4), (0x79400000, 2), (0x39400000, 1)]  # ldr w (32-bit), ldrh, ldrb (unsigned offset)
    mov = 0x52800000 | (value & 0xFFFF) << 5  # movz w<t>, #value
    out, hits, already = bytearray(lib), [], 0
    for off in range(start, start + length, 4):
        w = int.from_bytes(lib[off:off + 4], "little")
        if w & 0xFFFFFFE0 == mov:
            already += 1
        for base, size in kinds:
            if field_offset % size == 0 and field_offset // size < 4096 \
                    and w & 0xFFFFFC00 == base | (field_offset // size) << 10:
                hits.append(off)
                out[off:off + 4] = (mov | w & 31).to_bytes(4, "little")
    if not hits:
        if already:
            return None, [f"{name} already patched"]
        raise RuntimeError(f"{name}: no load of the field at {field_offset:#x} in this method")
    if len(hits) > 2:
        raise RuntimeError(f"{name}: {len(hits)} loads of offset {field_offset:#x}; not guessing which one")
    return bytes(out), [f"{name} -> {value} (at {', '.join(hex(h) for h in hits)})"]


ALL_TARGETS: dict[str, list[str]] = {}  # every IL2CPP patch's methods: one Cpp2IL run (and cache entry) serves all


class Il2cppReturnPatch(Patch):
    """Make IL2CPP methods return a constant (targets), found per game with Cpp2IL (analysis/il2cpp.py)."""
    targets: dict[str, dict[str, bytes]] = {}
    # loads of a field inlined into a method: {method's class file: {method: (field's class file, field, value)}}
    field_loads: dict[str, dict[str, tuple[str, str, int]]] = {}
    check_name = ""

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        wanted = [(c, m) for c, methods in cls.targets.items() for m in methods]
        for c, methods in cls.field_loads.items():
            for m, (fc, field, _v) in methods.items():
                wanted += [(c, m), (fc, f"field:{field}")]
        for c, m in wanted:
            ALL_TARGETS.setdefault(c, [])
            if m not in ALL_TARGETS[c]:
                ALL_TARGETS[c].append(m)

    @staticmethod
    def _unity_version(ws) -> str | None:
        """For library entries analyzed before the version was recorded: read it from the APK itself."""
        from ...analysis.detect import UNITY_GGM, unity_version

        ggm = ws.read(UNITY_GGM) if ws.has(UNITY_GGM) else None
        libunity = ws.read_lib("libunity.so") if ws.has(ws.lib("libunity.so")) else None
        return unity_version(ggm, libunity) if ggm or libunity else None

    def apply(self, ctx: ApkContext) -> bool:
        from ...analysis.il2cpp import find_methods

        ws = ctx.ws
        lib_name = ws.lib("libil2cpp.so")
        if ws.abi != "arm64-v8a" or not ws.has(lib_name) or not ws.has(METADATA):
            return False
        version = (ctx.analysis.extra or {}).get("unity_version") or self._unity_version(ws)
        if not version:
            ctx.reporter.check(self.check_name, False, "not fixed: unknown Unity version (rescan the game)")
            return False
        lib = ws.read(lib_name)
        ctx.reporter.log(f"{self.check_name}: finding the code (Cpp2IL; the first time can take a minute)")
        try:
            found = find_methods(lib, ws.read(METADATA), version, ALL_TARGETS)
            patched, notes = patch_methods(lib, found, self.targets)
            for c, methods in self.field_loads.items():
                for m, (fc, field, value) in methods.items():
                    method, offset = found.get(c, {}).get(m), found.get(fc, {}).get(f"field:{field}")
                    if not method or not offset:
                        continue
                    name = f"{c.rsplit('/', 1)[-1][:-3]}.{m}: {field}"
                    again, more = patch_field_loads(patched or lib, method, offset[0], value, name)
                    patched, notes = again or patched, notes + more
        except Exception as exc:  # noqa: BLE001 - optional fix: the game still builds, unchanged
            ctx.reporter.check(self.check_name, False, f"not fixed: {exc}")
            return False
        ctx.notes.extend(notes or [f"{self.check_name}: code not found in this game"])
        if patched is not None:
            ws.put(lib_name, patched)
        return patched is not None


class UnityTextInput(Il2cppReturnPatch):
    id = "frame.unity_text_input"
    title = "Make Unity text fields work"
    description = ("Unity's TMP_InputField / InputField wait for Android's on-screen keyboard and close themselves a "
                   "frame later when there is none (Lepton has no on-screen keyboard; Meta's system keyboard does "
                   "this on a Quest), so a selected text field only flashes a caret. Rewrites "
                   "TouchScreenKeyboardShouldBeUsed -> false and isKeyboardUsingEvents (TMP) / InPlaceEditing "
                   "(uGUI) -> true in libil2cpp.so, found per game with Cpp2IL (downloaded on first use), so fields "
                   "stay selected and take key presses: Steam's keyboard (with \"Show the app window\") or Type on "
                   "Frame from the PC.")
    order = 46
    needs_vr = False
    targets = TARGETS
    check_name = "Unity text fields"

    def applies(self, a):
        return a.engine == "Unity" and "libil2cpp.so" in a.libs and bool((a.extra or {}).get("text_fields"))

    def detect(self, a):
        if self.applies(a):
            kinds = " and ".join((a.extra or {}).get("text_fields"))
            return Suggestion(True, f"Unity app with text fields ({kinds}): they close at once on the Frame because "
                                    "it has no system keyboard.")
        return None


register(UnityTextInput)
