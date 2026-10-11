"""frame.godot_clipboard (GitHub #134, EndoparasiticVR): Godot 4.2-4.4's Kotlin `as ClipboardManager` throws in Lepton
(no clipboard service). The null check before that cast becomes nops and Godot's clipboard methods return at once.

The fixtures are synthetic dex files; the code units copy what kotlinc + D8 emit in the real godot-lib 4.3.0 / 4.4.1
(Godot.<init>, getClipboard, hasClipboard, setClipboard; Maven Central and the release export templates), with this
dex's own indices. The real libraries were checked by hand (see docs/PLAYBOOK.md); nothing is downloaded here."""
from __future__ import annotations

import struct
import zipfile

from test_vivox_audio_route import build_dex

from frameport.analysis import detect
from frameport.apk.dex import Dex
from frameport.patches.frame.godot_clipboard import MESSAGE, GodotClipboard, patch_dex
from frameport.validate.triage import triage

GODOT = "Lorg/godotengine/godot/Godot;"
LAMBDA = "Lorg/godotengine/godot/Godot$mClipboard$2;"
CM = "Landroid/content/ClipboardManager;"
NPE = "Ljava/lang/NullPointerException;"
SENSOR_MSG = "null cannot be cast to non-null type android.hardware.SensorManager"
STRINGS = ("", MESSAGE, SENSOR_MSG)  # string ids 0, 1, 2
TYPES = (CM, NPE, "Landroid/hardware/SensorManager;")  # type ids 0, 1, 2
REFS = [("Lkotlin/jvm/internal/Intrinsics;", "checkNotNull", "V", None),  # method 0
        (NPE, "<init>", "V", None),                                      # 1
        (CM, "hasPrimaryClip", "Z", None),                               # 2
        (CM, "getPrimaryClip", "Landroid/content/ClipData;", None),      # 3
        (CM, "setPrimaryClip", "V", None)]                               # 4

INIT = [0x0012,              # const/4 v0, 0  (getSystemService("clipboard") on Lepton)
        0x011A, 1,           # const-string v1, MESSAGE
        0x2071, 0, 0x0010,   # invoke-static {v0, v1}, Intrinsics.checkNotNull(Object, String)
        0x001F, 0,           # check-cast v0, ClipboardManager
        0x011A, 2,           # const-string v1, SENSOR_MSG
        0x2071, 0, 0x0010,   # invoke-static {v0, v1}, Intrinsics.checkNotNull  (another service: left alone)
        0x001F, 2,           # check-cast v0, SensorManager
        0x000E]
GET = [0x2254, 0, 0x106E, 3, 0x0002, 0x020C, 0x011A, 0, 0x0011]  # iget-object; getPrimaryClip; ...; return-object
HAS = [0x0054, 0, 0x106E, 2, 0x0000, 0x000A, 0x000F]              # iget-object; hasPrimaryClip; move-result; return
SET = [0x1054, 0, 0x206E, 4, 0x0021, 0x000E]                      # iget-object; setPrimaryClip; return-void
# kotlinc without Intrinsics.checkNotNull: if-nez v0, :cast; new-instance v1, NPE; const-string v2, MESSAGE;
# invoke-direct {v1, v2}, NPE.<init>; throw v1; :cast check-cast v0, ClipboardManager; return-object v0
LEGACY = [0x0039, 10, 0x0122, 1, 0x021A, 1, 0x2070, 1, 0x0021, 0x0127, 0x001F, 0, 0x0011]
# the same with the throw block after the return: if-eqz v0, :throw; check-cast v0; return-object v0; :throw ...
EQZ = [0x0038, 5, 0x001F, 0, 0x0011, 0x0122, 1, 0x021A, 1, 0x2070, 1, 0x0021, 0x0127]


def _dex(*methods, strings=STRINGS) -> Dex:
    return Dex(build_dex(REFS + list(methods), strings_first=strings, types_first=TYPES))


def _code(dex: Dex, cls: str, name: str) -> list[int]:
    off = dex.code_items(cls, name)[0]
    n = struct.unpack_from("<I", dex.data, off + 12)[0]
    return list(struct.unpack_from(f"<{n}H", dex.data, off + 16))


def test_godot_4_3_constructor_and_clipboard_methods():
    dex = _dex((GODOT, "<init>", "V", INIT), (GODOT, "getClipboard", "Ljava/lang/String;", GET),
               (GODOT, "hasClipboard", "Z", HAS), (GODOT, "setClipboard", "V", SET))
    raw = bytes(dex.data)
    assert patch_dex(dex) == ["Godot.<init> (clipboard cast)", "Godot.getClipboard (returns at once)",
                              "Godot.hasClipboard (returns at once)", "Godot.setClipboard (returns at once)"]
    assert _code(dex, GODOT, "<init>") == INIT[:3] + [0, 0, 0] + INIT[6:]  # only the clipboard's null check
    assert _code(dex, GODOT, "getClipboard") == [0x001A, 0, 0x0011, 0, 0] + GET[5:]  # const-string v0, ""; return
    assert _code(dex, GODOT, "hasClipboard") == [0x0012, 0x000F] + HAS[2:]  # const/4 v0, 0; return v0
    assert _code(dex, GODOT, "setClipboard") == [0x000E, 0] + SET[2:]
    out = dex.finish()
    assert len(out) == len(raw) and out[12:32] != raw[12:32]  # same size, new signature
    assert patch_dex(Dex(out)) == []  # a second run changes nothing


def test_older_kotlin_branch_forms():
    dex = _dex((LAMBDA, "invoke", CM, LEGACY), (LAMBDA, "invoke2", CM, EQZ))
    assert sorted(patch_dex(dex)) == ["Godot$mClipboard$2.invoke (clipboard cast)",
                                      "Godot$mClipboard$2.invoke2 (clipboard cast)"]
    assert _code(dex, LAMBDA, "invoke") == [0x0029, 10] + LEGACY[2:]  # if-nez -> goto/16, same target
    assert _code(dex, LAMBDA, "invoke2") == [0, 0] + EQZ[2:]  # if-eqz -> nops


def test_no_blind_edits():
    other_msg = INIT[:2] + [2] + INIT[3:6] + INIT[6:8] + [0x000E]  # checkNotNull with another message before the cast
    wrong_reg = INIT[:3] + [0x2071, 0, 0x0001] + INIT[6:8] + [0x000E]  # checkNotNull {v1, v0}: not the cast's value
    dex = _dex((GODOT, "a", "V", other_msg), (GODOT, "b", "V", wrong_reg), (GODOT, "hasClipboard", "Z", HAS),
               ("Lcom/example/Other;", "<init>", "V", INIT))  # outside org/godotengine
    assert patch_dex(dex) == []
    assert _code(dex, GODOT, "a") == other_msg and _code(dex, GODOT, "b") == wrong_reg
    assert _code(dex, GODOT, "hasClipboard") == HAS and _code(dex, "Lcom/example/Other;", "<init>") == INIT
    # Godot 4.5+ (`as?`, no message in the dex): its null-safe clipboard methods stay as they are
    dex = _dex((GODOT, "hasClipboard", "Z", HAS), strings=("",))
    assert patch_dex(dex) == [] and _code(dex, GODOT, "hasClipboard") == HAS


def test_apply_on_workspace():
    raw = build_dex(REFS + [(GODOT, "<init>", "V", INIT)], strings_first=STRINGS, types_first=TYPES)

    class Ws:
        files = {"classes.dex": b"dex\n035\0", "classes2.dex": raw}

        def names(self):
            return list(self.files)

        def read(self, n):
            return self.files[n]

        def put(self, n, data):
            self.files[n] = data

    class Ctx:
        ws = Ws()
        notes: list[str] = []

    assert GodotClipboard().apply(Ctx()) is True
    assert Ctx.ws.files["classes2.dex"] != raw and Ctx.notes == [
        "classes2.dex: Godot runs without a clipboard service (Godot.<init> (clipboard cast))"]


def test_detect_and_triage():
    class A:
        extra = {"godot_clipboard": True}
    assert GodotClipboard().applies(A()) and GodotClipboard().detect(A()).recommended
    A.extra = {"sdl_java": True}
    assert not GodotClipboard().applies(A()) and GodotClipboard().detect(A()) is None
    pre = "10-09 21:00:01.000  900  900 E AndroidRuntime: "
    log = (f"{pre}FATAL EXCEPTION: main\n"
           f"{pre}Process: org.godotengine.endoparasitic_vr, PID: 900\n"
           f"{pre}java.lang.RuntimeException: Unable to start activity ComponentInfo{{org.godotengine.endoparasitic_vr/"
           "com.godot.game.GodotApp}: java.lang.NullPointerException: null cannot be cast to non-null type "
           "android.content.ClipboardManager\n"
           f"{pre}\tat android.app.ActivityThread.performLaunchActivity(ActivityThread.java:3449)\n"
           f"{pre}Caused by: java.lang.NullPointerException: null cannot be cast to non-null type "
           "android.content.ClipboardManager\n"
           f"{pre}\tat org.godotengine.godot.Godot.<init>(Godot.kt:96)\n"
           f"{pre}\tat org.godotengine.godot.GodotFragment.onCreate(GodotFragment.java:194)\n")
    r = triage(log, "EXITED", "org.godotengine.endoparasitic_vr")
    hits = [f for f in r.findings if f.id == "godot-no-clipboard"]
    assert hits and hits[0].suggest == ["frame.godot_clipboard"]
    assert "sdl-no-clipboard" not in [f.id for f in r.findings]


def test_analysis_flags_only_the_throwing_cast(tmp_path, quest_manifest):
    assert detect.ANALYSIS_VERSION >= 10  # older library entries are analysed again for the new field
    cases = [(b"Lorg/godotengine/godot/Godot;..." + MESSAGE.encode(), True),  # Godot 4.2-4.4
             (b"Lorg/godotengine/godot/Godot;...Landroid/content/ClipboardManager;", False),  # 4.1 / 4.5+
             (MESSAGE.encode(), False)]  # another Kotlin app
    for i, (dex, expected) in enumerate(cases):
        path = tmp_path / f"{i}.apk"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("AndroidManifest.xml", quest_manifest)
            z.writestr("classes.dex", b"dex\n035\0" + dex)
        assert detect.analyze(path).extra["godot_clipboard"] is expected
