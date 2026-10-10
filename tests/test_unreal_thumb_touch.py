"""frame.unreal_thumb_touch (GitHub #49, found by Klownicle in Vader Immortal: Episode I 1.1.1): UE4 OculusInput's
ThumbUp axis from the capacitive touches instead of near-touch, which the Frame never reports."""
import struct
import zipfile

from frameport.analysis import detect
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.patches import base
from frameport.patches.frame import unreal_thumb_touch as tt

SYM = detect.UNREAL_THUMB_TOUCH
TEXT = 0x1000
# Vader Immortal: Episode I 1.1.1's libUE4.so at 0x578f188 (the capacitive-axis loop of SendControllerEvents): the
# per-hand masks (csel), the jump table, Touches loads at [sp, #240] and NearTouches at [sp, #244] (IndexPointing, then
# ThumbUp: ldr w8, [sp, #244]; tst w8, w23; fcsel s8, s13, s14, ne)
VADER = [
    0xAA1F03F4, 0xF100039F, 0x321C03E8, 0x321403E9, 0xF94037FA, 0x1A880135, 0x320003E8, 0x321E03E9, 0x1A890116,
    0x321F03E8, 0x321D03E9, 0x1A890117, 0x1E2703E8, 0x7100169F, 0x54000348, 0x92407E88, 0xB8A87B28, 0x8B190108,
    0xD61F0100, 0xB940F3E8, 0xB94087E9, 0x14000009, 0xB940F7E8, 0x6A16011F, 0x1400000F, 0xB940F3E8, 0xB94077E9,
    0x14000003, 0xB940F3E8, 0xB9408BE9, 0x6A09011F, 0x1E2D1DC8, 0x14000008, 0xB940F3E8, 0x6A15011F, 0x1E2D1DC8,
    0x14000004, 0xB940F7E8, 0x6A17011F, 0x1E2E1DA8, 0xBD400340, 0x1E202100, 0x54000120, 0xF9401260, 0xF85F8341,
    0xB9400302, 0x4EA81D00, 0xF9400008, 0xF9404508, 0xD63F0100, 0xBD000348, 0x91000694, 0x9100435A, 0xF1001A9F,
    0x54FFFAC1, 0xF94033E8, 0x9100079C,
]
MASK_LEFT, MASK_RIGHT, THUMB_LOAD = 9, 10, 37  # word indexes Klownicle changed (0x578f1ac, 0x578f1b0, 0x578f21c)


def make_engine(words=VADER) -> bytes:
    """A minimal ELF64 (one PT_LOAD at vaddr 0 = file offset, .dynsym/.dynstr/.text) exporting SendControllerEvents."""
    text = struct.pack(f"<{len(words)}I", *words)
    dynstr = b"\0" + SYM.encode() + b"\0"
    dynsym = bytes(24) + struct.pack("<IBBHQQ", 1, 0x12, 0, 3, TEXT, len(text))
    shstr = b"\0.dynsym\0.dynstr\0.text\0.shstrtab\0"
    out = bytearray(TEXT) + text
    dynstr_off = len(out)
    out += dynstr
    while len(out) % 8:
        out += b"\0"
    dynsym_off = len(out)
    out += dynsym
    shstr_off = len(out)
    out += shstr
    while len(out) % 8:
        out += b"\0"
    sh_off = len(out)

    def sh(name, typ, off, size, link=0, entsize=0, flags=0, addr=0):
        return struct.pack("<IIQQQQIIQQ", name, typ, flags, addr, off, size, link, 1 if typ == 11 else 0, 8, entsize)

    out += bytes(64)
    out += sh(1, 11, dynsym_off, len(dynsym), link=2, entsize=24, flags=2, addr=dynsym_off)
    out += sh(9, 3, dynstr_off, len(dynstr), flags=2, addr=dynstr_off)
    out += sh(17, 1, TEXT, len(text), flags=6, addr=TEXT)
    out += sh(23, 3, shstr_off, len(shstr))
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<16sHHIQQQIHHHHHH", out, 0, ident, 3, 183, 1, 0, 64, sh_off, 0, 64, 56, 1, 64, 5, 4)
    struct.pack_into("<IIQQQQQQ", out, 64, 1, 5, 0, 0, 0, sh_off + 5 * 64, sh_off + 5 * 64, 0x4000)
    return bytes(out)


def _analysis(**extra):
    return Analysis(package="com.example.ue4", version="1.0", label="UE4 Game", abis=["arm64-v8a"],
                    engine="Unreal", xr="VrApi", graphics="GLES or unknown (no Vulkan declaration)",
                    direct_vrapi=False, libs=["libOVRPlugin.so", "libUE4.so"], launcher_activity=None,
                    has_info_category=False, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
                    oculus_os_classes=False, is_overport_output=True, debuggable=False, extra={"size": 1, **extra})


def _apk(tmp_path, manifest, engine):
    p = tmp_path / "in.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
        z.writestr("classes.dex", b"dex\n035\0")
        z.writestr("lib/arm64-v8a/libUE4.so", engine)
    return p


def test_the_edits_are_klownicles():
    site = tt.thumb_site(make_engine())
    assert site and not site["done"]
    at = {TEXT + 4 * i: i for i in (MASK_LEFT, MASK_RIGHT, THUMB_LOAD)}
    assert [(at[o], old, new) for o, old, new in site["edits"]] == [
        (MASK_LEFT, 0x321F03E8, 0x5281E008),   # orr w8, wzr, #0x2 -> mov w8, #0xf00 (bytes 08 e0 81 52)
        (MASK_RIGHT, 0x321D03E9, 0x528001E9),  # orr w9, wzr, #0x8 -> mov w9, #0xf   (bytes e9 01 80 52)
        (THUMB_LOAD, 0xB940F7E8, 0xB940F3E8),  # ldr w8, [sp, #0xf4] -> ldr w8, [sp, #0xf0]
    ]
    assert detect.unreal_thumb_touch(make_engine())


def test_other_code_is_left_alone():
    words = list(VADER)
    words[MASK_RIGHT] = 0x321E03E9  # orr w9, wzr, #0x4: not the thumb masks
    assert tt.thumb_site(make_engine(words)) is None
    words = list(VADER)
    words[THUMB_LOAD + 2] = 0x14000004  # no fcsel after the tst
    assert tt.thumb_site(make_engine(words)) is None
    words = list(VADER)
    for i in (19, 25, 28, 33):  # no other loads of the Touches slot: not OculusInput's state layout
        words[i] = 0xB940EBE8
    assert tt.thumb_site(make_engine(words)) is None
    assert not detect.unreal_thumb_touch(b"\0" + SYM.encode() + b"\0")


def test_patch_rewrites_three_instructions_once(tmp_path, quest_manifest):
    data = make_engine()
    a = _analysis(unreal_thumb_touch=True)
    patch = base.get("frame.unreal_thumb_touch")
    assert patch.applies(a) and patch.detect(a).recommended
    assert not patch.applies(_analysis(unreal_thumb_touch=False)) and not patch.detect(_analysis())
    assert patch.applies(_analysis())  # analysed by an older FramePort: maybe
    with ApkWorkspace(_apk(tmp_path, quest_manifest, data)) as ws:
        ctx = base.ApkContext(ws, a, {}, Reporter(), {})
        assert patch.apply(ctx)
        out = ws.read_lib("libUE4.so")
        diff = [i for i in range(0, len(data), 4) if data[i:i + 4] != out[i:i + 4]]
        assert diff == [TEXT + 4 * i for i in (MASK_LEFT, MASK_RIGHT, THUMB_LOAD)]
        assert tt.thumb_site(out)["done"]
        assert not patch.apply(ctx)  # already patched: no-op
        assert patch.validate(ctx)[0][1]
        assert any("ThumbUp" in n for n in ctx.notes)


def test_unexpected_code_is_reported_not_patched(tmp_path, quest_manifest):
    words = list(VADER)
    words[MASK_LEFT] = 0x321E03E8
    data = make_engine(words)
    with ApkWorkspace(_apk(tmp_path, quest_manifest, data)) as ws:
        ctx = base.ApkContext(ws, _analysis(), {}, Reporter(), {})
        assert not base.get("frame.unreal_thumb_touch").apply(ctx)
        assert ws.read_lib("libUE4.so") == data
        assert any("doesn't match" in n for n in ctx.notes)


def test_analysis_records_the_match(tmp_path, quest_manifest):
    a = detect.analyze(_apk(tmp_path, quest_manifest, make_engine()))
    assert a.extra["unreal_thumb_touch"] is True
    assert a.extra["analysis_version"] == detect.ANALYSIS_VERSION >= 7


def test_vader_recipe_uses_the_engine_fix():
    from frameport.recommend import catalog

    entry = catalog.load()["com.ILMxLAB.VaderImmortal.ep1"]
    assert "frame.unreal_thumb_touch" in entry.frame
    assert "proximity_emul" not in entry.adapter  # the binding didn't animate the thumbs (GitHub #49)
