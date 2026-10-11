"""Godot 4 apps (Godot 4.2-4.4 Android library, e.g. EndoparasiticVR, GitHub #134) crash at start in Lepton: its
Android has no clipboard service, getSystemService(CLIPBOARD_SERVICE) returns null, and Godot's Kotlin code casts it
with `as ClipboardManager` (a non-null cast: `Intrinsics.checkNotNull(x, "null cannot be cast to non-null type
android.content.ClipboardManager")` before the check-cast) → NullPointerException in Godot.<init> (4.3/4.4) or the
first time the clipboard is used (4.2's lazy property). Godot 4.1 (Java) and 4.5+ (`as?`) don't throw.

In place in classes*.dex: the null check in front of that one cast is turned into nops (check-cast accepts null, so the
field simply holds null), and, because the property was non-null typed, Godot's clipboard methods use it without a
null check: they return at once instead (hasClipboard false, getClipboard "", setClipboard nothing). Only exactly that
bytecode is edited; a dex without it is left alone."""
from __future__ import annotations

from ...apk.dex import Dex
from ..base import ApkContext, Patch, Suggestion, register

PACKAGE = "Lorg/godotengine/"
CLIPBOARD = "Landroid/content/ClipboardManager;"
MESSAGE = "null cannot be cast to non-null type android.content.ClipboardManager"
CHECK_NOT_NULL = ("Lkotlin/jvm/internal/Intrinsics;", "checkNotNull")
NPE = "Ljava/lang/NullPointerException;"
CHECK_CAST, NEW_INSTANCE, THROW, IF_EQZ, IF_NEZ, GOTO16 = 0x1F, 0x22, 0x27, 0x38, 0x39, 0x29
INVOKE_DIRECT, INVOKE_STATIC, INVOKE_STATIC_RANGE = 0x70, 0x71, 0x77


def _const_string(dex: Dex, u) -> tuple[int, str] | None:
    """(register, text) of a const-string(/jumbo), else None."""
    op = u[0] & 0xFF
    if op == 0x1A:
        return u[0] >> 8, dex.string(u[1])
    if op == 0x1B:
        return u[0] >> 8, dex.string(u[1] | u[2] << 16)
    return None


def _args(u) -> list[int]:
    """Argument registers of an invoke (35c or 3rc)."""
    op = u[0] & 0xFF
    if op in (INVOKE_STATIC_RANGE, 0x76):  # invoke-static/range, invoke-direct/range
        return list(range(u[2], u[2] + (u[0] >> 8)))
    regs = [u[2] & 0xF, (u[2] >> 4) & 0xF, (u[2] >> 8) & 0xF, u[2] >> 12, (u[0] >> 8) & 0xF]
    return regs[:u[0] >> 12]


def _s16(unit: int) -> int:
    return unit - 0x10000 if unit & 0x8000 else unit


def _is_message(dex: Dex, u, reg: int | None = None) -> bool:
    cs = _const_string(dex, u)
    return cs is not None and cs[1] == MESSAGE and (reg is None or cs[0] == reg)


def _throw_block(dex: Dex, insns: list, i: int) -> bool:
    """insns[i:i+4] = new-instance vX, NullPointerException; const-string vY, MESSAGE; invoke-direct {vX, vY},
    NullPointerException.<init>; throw vX (what kotlinc before Intrinsics.checkNotNull emitted)."""
    if i + 4 > len(insns):
        return False
    (_, new), (_, msg), (_, init), (_, thr) = insns[i:i + 4]
    if new[0] & 0xFF != NEW_INSTANCE or dex.type_name(new[1]) != NPE or not _is_message(dex, msg):
        return False
    x, y = new[0] >> 8, msg[0] >> 8
    return (init[0] & 0xFF == INVOKE_DIRECT and dex.method(init[1]) == (NPE, "<init>") and _args(init) == [x, y]
            and thr[0] & 0xFF == THROW and thr[0] >> 8 == x)


def unchecked_casts(dex: Dex, code: int) -> list[tuple[int, list[int]]]:
    """(code unit position, replacement units) that let `x as ClipboardManager` pass a null x in one method."""
    insns = list(dex._insns(code))
    pos_of = {pos: k for k, (pos, _u) in enumerate(insns)}
    edits = []
    for k, (pos, u) in enumerate(insns):
        if u[0] & 0xFF != CHECK_CAST or dex.type_name(u[1]) != CLIPBOARD:
            continue
        reg = u[0] >> 8
        # Kotlin >= 1.4: const-string vB, MESSAGE; invoke-static {vA, vB}, Intrinsics.checkNotNull; check-cast vA
        if k >= 2:
            (_, msg), (ipos, inv) = insns[k - 2], insns[k - 1]
            if (inv[0] & 0xFF in (INVOKE_STATIC, INVOKE_STATIC_RANGE) and dex.method(inv[1]) == CHECK_NOT_NULL
                    and _args(inv) == [reg, msg[0] >> 8] and _is_message(dex, msg)):
                edits.append((ipos, [0, 0, 0]))
                continue
        # older kotlinc: if-nez vA, :cast; <throw block>; :cast check-cast vA  → the branch is always taken
        if k >= 5:
            bpos, br = insns[k - 5]
            if (br[0] & 0xFF == IF_NEZ and br[0] >> 8 == reg and bpos + _s16(br[1]) == pos
                    and _throw_block(dex, insns, k - 4)):
                edits.append((bpos, [GOTO16, br[1]]))
                continue
        # the same with the throw block moved away: if-eqz vA, :throw; check-cast vA  → never branches
        if k >= 1:
            bpos, br = insns[k - 1]
            if (br[0] & 0xFF == IF_EQZ and br[0] >> 8 == reg and bpos + _s16(br[1]) in pos_of
                    and _throw_block(dex, insns, pos_of[bpos + _s16(br[1])])):
                edits.append((bpos, [0, 0]))
    return edits


def patch_dex(dex: Dex) -> list[str]:
    """What was changed (method names); [] if this dex doesn't have Godot's throwing clipboard cast."""
    if MESSAGE.encode() not in dex.data:
        return []
    classes = [c for c in dex.class_names() if c.startswith(PACKAGE)]
    done = []
    for cls in classes:
        for idx, code in dex.class_methods(cls):
            edits = unchecked_casts(dex, code)
            for pos, units in edits:
                for i, unit in enumerate(units):
                    dex.data[code + 16 + 2 * (pos + i): code + 18 + 2 * (pos + i)] = unit.to_bytes(2, "little")
            if edits:
                done.append(f"{_short(cls)}.{dex.method(idx)[1]} (clipboard cast)")
    if not done:
        return []
    # the property was typed non-null, so nothing checks it before use: its users return at once
    users = {i for i in range(dex.methods_n) if dex.method(i)[0] == CLIPBOARD}
    for cls in classes:
        for idx, code in dex.class_methods(cls):
            if dex.invoked(code) & users and dex.return_early(code, dex.return_type(idx)):
                done.append(f"{_short(cls)}.{dex.method(idx)[1]} (returns at once)")
    return done


def _short(cls: str) -> str:
    return cls[1:-1].rsplit("/", 1)[-1]


class GodotClipboard(Patch):
    id = "frame.godot_clipboard"
    title = "Godot apps: start without a clipboard service"
    description = ("Lepton's Android has no clipboard service, and the Android code of Godot 4.2-4.4 (for example "
                   "EndoparasiticVR) casts the missing service to a ClipboardManager without checking, so the app "
                   "crashes at start with a NullPointerException in Godot.<init>. That check is skipped and Godot's "
                   "clipboard functions do nothing instead (an in-place edit of classes.dex).")
    order = 44
    needs_vr = False

    def applies(self, a):
        return bool((a.extra or {}).get("godot_clipboard"))

    def detect(self, a):
        if self.applies(a):
            return Suggestion(True, "Godot 4 app: it crashes at start in Lepton, which has no clipboard service.")
        return None

    def apply(self, ctx: ApkContext) -> bool:
        changed = False
        for name in sorted(n for n in ctx.ws.names() if n.startswith("classes") and n.endswith(".dex")):
            data = ctx.ws.read(name)
            if MESSAGE.encode() not in data:
                continue
            dex = Dex(data)
            done = patch_dex(dex)
            if done:
                ctx.ws.put(name, dex.finish())
                ctx.notes.append(f"{name}: Godot runs without a clipboard service ({', '.join(done)})")
                changed = True
        return changed


register(GodotClipboard)
