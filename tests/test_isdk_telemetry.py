"""frame.isdk_telemetry (GitHub #546, Ghost Signal): Meta's Interaction SDK crashes in TelemetrySender's constructor
without Quest's libossdk; the patch turns on the library's own telemetryDisabled switch in initializeTelemetry()."""
import struct
import zipfile

from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.patches import base
from frameport.patches.frame import isdk_telemetry as it
from frameport.validate.triage import triage

TEXT, GOT, VAR, OTHER = 0x1000, 0x2000, 0x2010, 0x2018


def adrp(reg: int, pc: int, target: int) -> int:
    imm = ((target & ~0xFFF) - (pc & ~0xFFF)) >> 12
    return 0x90000000 | (imm & 3) << 29 | ((imm >> 2) & 0x7FFFF) << 5 | reg


def init_words(head=None):
    """The shape of Ghost Signal's initializeTelemetry() (libInteractionSdk.so, 0x9aa08): prologue, the
    telemetryDisabled test through the GOT, "disabled" = -2, else initialise and return 1."""
    words = [
        0xA9BD7BFD,                     # stp x29, x30, [sp, #-0x30]!
        0x910003FD,                     # mov x29, sp
        adrp(8, TEXT + 8, GOT),         # adrp x8, GOT page
        0xF9400108,                     # ldr x8, [x8]
        0x39400108,                     # ldrb w8, [x8]
        0x34000068,                     # cbz w8, +12
        0x12800020,                     # mov w0, #-2
        0x14000002,                     # b +8
        0x52800020,                     # mov w0, #1
        0xA8C37BFD,                     # ldp x29, x30, [sp], #0x30
        0xD65F03C0,                     # ret
    ]
    if head:
        words[4:6] = head
    return words


def make_lib(words=None, slot_target=VAR, with_switch=True) -> bytes:
    """A minimal ELF64 (one PT_LOAD at vaddr 0 = file offset) exporting initializeTelemetry and telemetryDisabled, with
    the GOT slot's R_AARCH64_RELATIVE relocation pointing at `slot_target`."""
    text = struct.pack("<11I", *(words or init_words()))
    names = [it.INIT] + ([it.DISABLED] if with_switch else [])
    dynstr = b"\0" + b"".join(n.encode() + b"\0" for n in names)
    dynsym = bytes(24) + struct.pack("<IBBHQQ", 1, 0x12, 0, 3, TEXT, len(text))
    if with_switch:
        dynsym += struct.pack("<IBBHQQ", 1 + len(it.INIT) + 1, 0x11, 0, 3, VAR, 1)
    rela = struct.pack("<QQq", GOT, it.R_AARCH64_RELATIVE, slot_target)
    out = bytearray(TEXT) + text
    out += bytes(GOT - len(out)) + bytes(0x20)  # GOT slot + the switch + another variable
    dynstr_off = len(out)
    out += dynstr
    while len(out) % 8:
        out += b"\0"
    dynsym_off = len(out)
    out += dynsym
    rela_off = len(out)
    out += rela
    shstr = b"\0.dynsym\0.dynstr\0.text\0.shstrtab\0.rela.dyn\0"
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
    out += sh(33, 4, rela_off, len(rela), link=1, entsize=24, flags=2, addr=rela_off)
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<16sHHIQQQIHHHHHH", out, 0, ident, 3, 183, 1, 0, 64, sh_off, 0, 64, 56, 1, 64, 6, 4)
    struct.pack_into("<IIQQQQQQ", out, 64, 1, 7, 0, 0, 0, sh_off + 6 * 64, sh_off + 6 * 64, 0x4000)
    return bytes(out)


def _analysis(libs=("libunity.so", "libInteractionSdk.so")):
    return Analysis(package="com.example.isdk", version="1.0", label="ISDK Game", abis=["arm64-v8a"],
                    engine="Unity", xr="OpenXR", graphics="Vulkan", direct_vrapi=False, libs=list(libs),
                    launcher_activity=None, has_info_category=False, meta_permissions=[], uses_glad_gl=False,
                    unity_msaa_levels=0, oculus_os_classes=False, is_overport_output=True, debuggable=False,
                    extra={"size": 1})


def _apk(tmp_path, manifest, lib):
    p = tmp_path / "in.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
        z.writestr("classes.dex", b"dex\n035\0")
        z.writestr("lib/arm64-v8a/libInteractionSdk.so", lib)
    return p


def test_switch_test_becomes_switch_on():
    data = make_lib()
    out = it.patch_isdk_telemetry(data)
    diff = [i for i in range(0, len(data), 4) if data[i:i + 4] != out[i:i + 4]]
    assert diff == [TEXT + 16, TEXT + 20]
    assert struct.unpack_from("<II", out, TEXT + 16) == (0x52800030, 0x39000110)  # mov w16, #1; strb w16, [x8]
    assert struct.unpack_from("<I", out, TEXT + 24) == (0x12800020,)  # falls into the "disabled" return
    assert it.switch_site(out)[2] and it.patch_isdk_telemetry(out) is None


def test_other_shapes_are_left_alone():
    assert it.patch_isdk_telemetry(make_lib(slot_target=OTHER)) is None  # the GOT slot isn't telemetryDisabled
    assert it.patch_isdk_telemetry(make_lib(with_switch=False)) is None  # no switch exported
    assert it.patch_isdk_telemetry(make_lib(init_words(head=[0x39400108, 0x35000068]))) is None  # cbnz
    assert it.patch_isdk_telemetry(b"not an elf") is None


def test_patch_suggested_and_applied_once(tmp_path, quest_manifest):
    patch = base.get("frame.isdk_telemetry")
    a = _analysis()
    assert patch.applies(a) and patch.detect(a).recommended
    assert not patch.applies(_analysis(["libunity.so"])) and patch.detect(_analysis(["libunity.so"])) is None
    data = make_lib()
    with ApkWorkspace(_apk(tmp_path, quest_manifest, data)) as ws:
        ctx = base.ApkContext(ws, a, {}, Reporter(), {})
        assert patch.apply(ctx)
        assert it.switch_site(ws.read_lib("libInteractionSdk.so"))[2]
        assert not patch.apply(ctx)
        assert patch.validate(ctx) == [("Interaction SDK telemetry switched off", True, "libInteractionSdk.so")]


def test_unknown_shape_is_reported(tmp_path, quest_manifest):
    data = make_lib(init_words(head=[0x39400108, 0x35000068]))
    with ApkWorkspace(_apk(tmp_path, quest_manifest, data)) as ws:
        ctx = base.ApkContext(ws, _analysis(), {}, Reporter(), {})
        assert not base.get("frame.isdk_telemetry").apply(ctx)
        assert ws.read_lib("libInteractionSdk.so") == data
        assert ctx.checks and ctx.checks[0][1] is None


def test_triage_names_the_patch():
    lib = "/data/app/~~x==/com.fasttravelgames.x15-y==/lib/arm64/libInteractionSdk.so"
    log = ("10-31 07:51:53.354  1221  1244 I ISDK:Telemetry: Attempting to initialize telemetry sender...\n"
           "10-31 07:51:53.354  1221  1244 E OsSdkLoader: libossdk not found\n"
           "10-31 07:51:53.358  1221  1244 F libc    : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR)\n"
           f"10-31 07:51:53.358  1221  1244 E CRASH   :       #00 pc 000000000009fe14  {lib} (interaction_sdk::v1::"
           "telemetry::TelemetrySender::TelemetrySender(interaction_sdk::v1::telemetry::TelemetryCache&, float)+192)\n"
           f"10-31 07:51:53.358  1221  1244 E CRASH   :       #01 pc 000000000009aa94  {lib} (interaction_sdk::v1::"
           "telemetry::initializeTelemetry()+140)\n")
    r = triage(log, "EXITED", "com.fasttravelgames.x15")
    found = {f.id: f.suggest for f in r.findings}
    assert found.get("isdk-telemetry-crash") == ["frame.isdk_telemetry"]
