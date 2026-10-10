"""ILMxLAB's Unreal Quest gates (GitHub #49, found by Klownicle in Vader Immortal: Episode I 1.1.1):
frame.unreal_quest_precompile (GetQuestShaderPrecompilePercent's non-Quest branch returns 1.0) and
frame.unreal_quest_keymap (the RPOC key selector keeps the Quest key set), plus frame.zink_shader_fix (the shader-fix
Vulkan layer for GLES games) and the adapter's proximity_emul setting."""
import struct
import zipfile

from frameport.analysis import detect, elf
from frameport.analysis.stubgen import build_stub_library
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.patches import base
from frameport.patches.frame import unreal_santacruz as sc
from frameport.patches.frame import zink_shader_fix
from frameport.patches.settings import adapter_settings

FSC = "_ZN15FSantaCruzUtils20IsRunningOnSantaCruzEv"
NINJA = "_ZN23UNTNinjaFunctionLibrary20IsRunningOnSantaCruzEv"
SELECTOR = "_ZN27URPOCKeyMapManagerComponent11SelectKeysEv"  # local in the game; named here for the test
STP, MOV_FP, LDRB, RET, LDP = 0xA9BF7BFD, 0x910003FD, 0x39400100, 0xD65F03C0, 0xA8C17BFD


def bl(pc, target):
    return 0x94000000 | ((target - pc) >> 2) & 0x3FFFFFF


def b(pc, target):
    return 0x14000000 | ((target - pc) >> 2) & 0x3FFFFFF


def tbz(pc, target):
    return 0x36000000 | (((target - pc) >> 2) & 0x3FFF) << 5


def add_x0_x19(imm):
    return 0x91000260 | imm << 10


TEXT = 0x1000


def layout():
    """Function addresses: each function gets 0x80 bytes from TEXT on."""
    names = [FSC, NINJA, sc.PRECOMPILE, SELECTOR, sc.ADD_AXIS, sc.ADD_ACTION]
    return {n: TEXT + 0x80 * i for i, n in enumerate(names)}


def bodies(at, fmov=sc.FMOV_S0_ZERO, keymap_branch=None):
    p, s = at[sc.PRECOMPILE], at[SELECTOR]
    return {
        FSC: [LDRB, RET],
        NINJA: [b(at[NINJA], at[FSC])],
        # stp; mov; bl IsRunningOnSantaCruz; tbz w0, #0, L; ldp; b <precompile progress>; L: fmov s0, wzr; ldp; ret
        sc.PRECOMPILE: [STP, MOV_FP, bl(p + 8, at[FSC]), tbz(p + 12, p + 24), LDP, b(p + 20, at[FSC]), fmov, LDP, RET],
        # ...; bl IsRunningOnSantaCruz; tbz w0, #0, GearVR; add x0, x19, #0x38; b E; GearVR: add x0, x19, #0x68; b E;
        # E: ldp; ret
        SELECTOR: [STP, MOV_FP, bl(s + 8, at[NINJA]),
                   keymap_branch if keymap_branch is not None else tbz(s + 12, s + 24),
                   add_x0_x19(0x38), b(s + 20, s + 32), add_x0_x19(0x68), b(s + 28, s + 32), LDP, RET],
        sc.ADD_AXIS: [STP, bl(at[sc.ADD_AXIS] + 4, s), RET],
        sc.ADD_ACTION: [STP, bl(at[sc.ADD_ACTION] + 4, s), RET],
    }


def make_engine(**kw) -> bytes:
    """A minimal ELF64 (one PT_LOAD at vaddr 0 = file offset, .dynsym/.dynstr/.text sections) with these functions."""
    at = layout()
    text = bytearray(0x80 * len(at))
    for name, words in bodies(at, **kw).items():
        struct.pack_into(f"<{len(words)}I", text, at[name] - TEXT, *words)
    dynstr = bytearray(b"\0")
    syms = [bytes(24)]
    for name, addr in at.items():
        off = len(dynstr)
        dynstr += name.encode() + b"\0"
        syms.append(struct.pack("<IBBHQQ", off, 0x12, 0, 3, addr, 0x80))  # GLOBAL FUNC in section 3 (.text)
    dynsym = b"".join(syms)
    shstr = b"\0.dynsym\0.dynstr\0.text\0.shstrtab\0"
    out = bytearray(TEXT)
    out += text
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
    return Analysis(package="com.example.ilm", version="1.0", label="ILM Game", abis=["arm64-v8a"],
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


def word_at(data, va):
    return struct.unpack_from("<I", data, elf.vaddr_to_offset(elf.load_segments(data), va))[0]


def test_find_symbols_reads_dynsym_by_name():
    data, at = make_engine(), layout()
    found = elf.find_symbols(data, [sc.PRECOMPILE, FSC, "missing"])
    assert found == {sc.PRECOMPILE: (at[sc.PRECOMPILE], 0x80), FSC: (at[FSC], 0x80)}


def test_sites_are_found_at_the_checked_instructions():
    data, at = make_engine(), layout()
    assert sc.precompile_site(data) == (at[sc.PRECOMPILE] + 24, sc.FMOV_S0_ZERO)
    assert sc.keymap_site(data) == (at[SELECTOR] + 12, tbz(at[SELECTOR] + 12, at[SELECTOR] + 24))
    assert detect.unreal_quest_gates(data) == ["quest_precompile", "rpoc_keymap"]
    assert detect.unreal_quest_gates(build_stub_library(["x"], soname="libUE4.so")) == []


def test_other_code_is_left_alone():
    assert sc.precompile_site(make_engine(fmov=0x1E270000)) is None  # not fmov s0, wzr: another build
    assert sc.keymap_site(make_engine(keymap_branch=0x37000000)) is None  # tbnz: not the expected branch
    assert sc.precompile_site(build_stub_library([sc.PRECOMPILE], soname="libUE4.so")) is None  # mov x0, #0; ret


def test_patches_rewrite_one_instruction_each_and_only_once(tmp_path, quest_manifest):
    data, at = make_engine(), layout()
    a = _analysis(unreal_quest_gates=["quest_precompile", "rpoc_keymap"])
    for pid in ("frame.unreal_quest_precompile", "frame.unreal_quest_keymap"):
        patch = base.get(pid)
        assert patch.applies(a) and patch.detect(a).recommended
        assert not patch.applies(_analysis(unreal_quest_gates=[]))
        assert patch.applies(_analysis())  # analysed by an older FramePort: maybe
    with ApkWorkspace(_apk(tmp_path, quest_manifest, data)) as ws:
        ctx = base.ApkContext(ws, a, {}, Reporter(), {})
        pre, key = base.get("frame.unreal_quest_precompile"), base.get("frame.unreal_quest_keymap")
        assert pre.apply(ctx) and key.apply(ctx)
        out = ws.read_lib("libUE4.so")
        assert word_at(out, at[sc.PRECOMPILE] + 24) == sc.FMOV_S0_ONE
        assert word_at(out, at[SELECTOR] + 12) == sc.NOP
        diff = [i for i in range(0, len(data), 4) if data[i:i + 4] != out[i:i + 4]]
        assert len(diff) == 2  # nothing else changed
        assert not pre.apply(ctx) and not key.apply(ctx)  # already patched: no-op
        assert pre.validate(ctx)[0][1] and key.validate(ctx)[0][1]
        assert any("non-Quest branch returns 1.0" in n for n in ctx.notes)


def test_unexpected_code_is_reported_not_patched(tmp_path, quest_manifest):
    data = make_engine(fmov=0x1E270000)
    with ApkWorkspace(_apk(tmp_path, quest_manifest, data)) as ws:
        ctx = base.ApkContext(ws, _analysis(), {}, Reporter(), {})
        assert not base.get("frame.unreal_quest_precompile").apply(ctx)
        assert ws.read_lib("libUE4.so") == data
        assert any("doesn't match" in n for n in ctx.notes)


def test_analysis_records_the_gates(tmp_path, quest_manifest):
    a = detect.analyze(_apk(tmp_path, quest_manifest, make_engine()))
    assert a.extra["unreal_quest_gates"] == ["quest_precompile", "rpoc_keymap"]
    assert a.extra["analysis_version"] == detect.ANALYSIS_VERSION >= 4


def test_shader_fix_layer_is_loaded_only_when_configured(tmp_path, quest_manifest):
    engine = build_stub_library(["eglInitialize_user"], soname="libUE4.so")
    patch = base.get("frame.zink_shader_fix")
    a = _analysis()
    assert patch.applies(a) and not patch.detect(a)
    with ApkWorkspace(_apk(tmp_path, quest_manifest, engine)) as ws:
        assert not patch.apply(base.ApkContext(ws, a, {}, Reporter(), {patch.id: {}}))  # no fix configured
        assert not ws.has(ws.lib(zink_shader_fix.LAYER))
        recipe = {patch.id: {}, "adapter.zink_shader_fix": {"value": "8:" + "0" * 64 + ":20:1"}}
        ctx = base.ApkContext(ws, a, {}, Reporter(), recipe)
        assert patch.apply(ctx)
        assert elf.needed(ws.read_lib("libUE4.so"))[0] == zink_shader_fix.LAYER
        assert ws.has(ws.lib(zink_shader_fix.LAYER))
        assert patch.validate(ctx)[0][1]
        assert not patch.apply(ctx)
    assert adapter_settings(recipe)["zink_shader_fix"].startswith("8:")


def test_shader_fix_layer_exports_what_androids_loader_looks_up():
    from frameport.patches.frame import artifact

    layer = artifact("arm64-v8a", zink_shader_fix.LAYER)
    exports = elf.dyn_symbols(layer, True)
    assert {"vkEnumerateInstanceLayerProperties", "vkEnumerateInstanceExtensionProperties",
            "VK_LAYER_FP_shader_fixGetInstanceProcAddr", "VK_LAYER_FP_shader_fixGetDeviceProcAddr"} <= exports
    # never the plain entry points: the engine library loads the layer first and would resolve Vulkan to it
    assert not {"vkGetInstanceProcAddr", "vkCreateInstance", "vkCreateShaderModule"} & exports
    assert b"VK_LAYER_FP_shader_fix\0" in layer and b"zink_shader_fix=" in layer


def test_proximity_emul_setting():
    patch = base.get("adapter.proximity_emul")
    assert patch.applies(_analysis()) and not patch.detect(_analysis())
    assert adapter_settings({"adapter.proximity_emul": {"value": 1}})["proximity_emul"] == 1
    from frameport.patches.frame import artifact

    adapter = artifact("arm64-v8a", "libopenxr_loader_generic.so")
    assert b"proximity_emul=%f" in adapter and b"hand_thumb_proximity" in adapter


def test_vader_recipe_from_the_catalog():
    from frameport.recommend import catalog

    entry = catalog.load()["com.ILMxLAB.VaderImmortal.ep1"]
    assert {"frame.unreal_quest_precompile", "frame.unreal_quest_keymap", "frame.zink_shader_fix"} <= set(entry.frame)
    assert entry.adapter["pose_time_fix"] == 1
    fixes = entry.adapter["zink_shader_fix"].split(";")
    assert [f.split(":")[0] for f in fixes] == ["12016", "11436"]
    for f in fixes:  # twelve OpStores (3 words each) after all OpVariables
        words = f.split(":")[3].split(",")
        assert len(words) == 36 and words[::3] == ["0x0003003e"] * 12
