"""scripts/rift_fex_survey.py: Unreal version strings, pak indexes (v3, v11) on synthetic paks, RHI and verdicts."""
import importlib.util
import struct
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sv():
    spec = importlib.util.spec_from_file_location("rift_fex_survey", ROOT / "scripts" / "rift_fex_survey.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fstr(s: str) -> bytes:
    b = s.encode("latin-1") + b"\0"
    return struct.pack("<i", len(b)) + b


INI = b"[/Script/WindowsTargetPlatform.WindowsTargetSettings]\r\n+TargetedRHIs=PCD3D_SM5\r\n" \
      b"[/Script/Engine.RendererSettings]\r\nr.MobileMSAA=4\r\nr.ForwardShading=True\r\n"


def legacy_entry(off: int, data: bytes) -> bytes:
    return struct.pack("<qqqi", off, len(data), len(data), 0) + bytes(20) + b"\0" + struct.pack("<I", 0)


def pak_v3(path: Path):
    body = legacy_entry(0, INI) + INI
    index = fstr("../../../") + struct.pack("<i", 1) + fstr("Proj/Config/DefaultEngine.ini") + legacy_entry(0, INI)
    footer = struct.pack("<IIqq", 0x5A6F12E1, 3, len(body), len(index)) + bytes(20)
    path.write_bytes(body + index + footer)


def pak_v11(path: Path, encrypted_index=False):
    """Two files: the ini stored, a text zlib-compressed (one block), both bit-packed entries + full directory index."""
    other = b"hello " * 50
    packed = zlib.compress(other)
    head = 8 + 8 + 8 + 4 + 20                               # entry copy in front of the data
    stored_at, comp_at = 0, head + 1 + 4 + len(INI)
    body = bytes(head + 1 + 4) + INI + bytes(head + 4 + 16 + 1 + 4) + packed
    flags32 = (1 << 31) | (1 << 30) | (1 << 29)
    enc = struct.pack("<III", flags32, stored_at, len(INI))
    second = len(enc)
    enc += struct.pack("<IIII", flags32 | (1 << 23) | (1 << 6), comp_at, len(other), len(packed))
    fdi = struct.pack("<i", 1) + fstr("Proj/Config/") + struct.pack("<i", 2) + \
        fstr("DefaultEngine.ini") + struct.pack("<i", 0) + fstr("notes.txt") + struct.pack("<i", second)
    fdi_off = len(body)
    index = fstr("../../../") + struct.pack("<iQii", 2, 0, 0, 1) + struct.pack("<qq", fdi_off, len(fdi)) + bytes(20) \
        + struct.pack("<i", len(enc)) + enc + struct.pack("<i", 0)
    ioff = fdi_off + len(fdi)
    names = b"".join(n.ljust(32, b"\0") for n in (b"Zlib", b"", b"", b"", b""))
    footer = bytes(16) + bytes([1 if encrypted_index else 0]) + \
        struct.pack("<IIqq", 0x5A6F12E1, 11, ioff, len(index)) + bytes(20) + names
    path.write_bytes(body + fdi + index + footer)
    return other


def test_unreal_version(sv):
    assert sv.unreal_version(b"x\0++UE4+Release-4.19\0") == "4.19"
    assert sv.unreal_version(b"\0\0" + "++UE4+Studio-4.27-CL-0".encode("utf-16-le") + b"\0\0") == "4.27"
    assert sv.unreal_version(b"\0++unreal+4.10+UE4-Studio\0") == "4.10"
    assert sv.unreal_version(b"\0++depot+Game-CL-55578\0") is None        # no number in the branch
    assert sv.unreal_version(b"\0++5.6+5\0++CrossGPUFence-\0") is None


def test_pak_v3(sv, tmp_path):
    pak = tmp_path / "a.pak"
    pak_v3(pak)
    ver, mount, entries, _ = sv.pak_entries(pak)
    assert ver == 3 and mount == "../../../"
    assert sv.pak_read(pak, ver, entries["Proj/Config/DefaultEngine.ini"]) == INI


def test_pak_v11(sv, tmp_path):
    pak = tmp_path / "b.pak"
    other = pak_v11(pak)
    ver, _, entries, names = sv.pak_entries(pak)
    assert ver == 11 and names[0] == "Zlib"
    assert set(entries) == {"Proj/Config/DefaultEngine.ini", "Proj/Config/notes.txt"}
    assert sv.pak_read(pak, ver, entries["Proj/Config/DefaultEngine.ini"], names) == INI
    assert sv.pak_read(pak, ver, entries["Proj/Config/notes.txt"], names) == other
    cfg = sv.unreal_config(tmp_path)
    assert cfg["pak"] in ("v3", "v11") and cfg["config"]["TargetedRHIs"] == ["PCD3D_SM5"]
    enc = tmp_path / "enc" / "c.pak"
    enc.parent.mkdir()
    pak_v11(enc, encrypted_index=True)
    with pytest.raises(ValueError, match="encrypted"):
        sv.pak_entries(enc)
    assert "encrypted" in sv.unreal_config(enc.parent)["pak_notes"][0]


def test_engine_ini_and_rhi(sv):
    cfg = sv.parse_engine_ini(INI.decode())
    assert cfg == {"TargetedRHIs": ["PCD3D_SM5"], "r.MobileMSAA": "4", "r.ForwardShading": "True"}
    assert sv.unreal_rhi(cfg) == ("d3d11", "config")
    assert sv.unreal_rhi({}) == ("d3d11", "Unreal default")
    assert sv.unreal_rhi({"DefaultGraphicsRHI": "DefaultGraphicsRHI_DX12"})[0] == "d3d12"
    assert sv.unreal_rhi({"TargetedRHIs": ["SF_VULKAN_SM5"]})[0] == "vulkan"
    assert sv.unreal_rhi({"TargetedRHIs": ["PCD3D_SM6"]})[0] == "d3d12"
    removed = sv.parse_engine_ini("+TargetedRHIs=PCD3D_SM5\n+TargetedRHIs=SF_VULKAN_SM5\n-TargetedRHIs=PCD3D_SM5\n")
    assert removed["TargetedRHIs"] == ["SF_VULKAN_SM5"]


def test_verdict(sv):
    ok = {"arch": "x86_64", "graphics": ["d3d11"], "vr": ["LibOVR"], "drm": [], "engine": "Unreal 4.19",
          "ime_patch": {"rva": "0x1", "module": "exe"}}
    assert sv.verdict(ok)[0] == "likely OK"
    assert sv.verdict(dict(ok, graphics=["d3d12"]))[0] == "needs work"
    assert sv.verdict(dict(ok, ime_patch="CVar name found but not its reference"))[0] == "needs work"
    assert sv.verdict(dict(ok, ime_patch="no Slate.DeferWindowsMessageProcessing (older Unreal or not Unreal)"))[0] \
        == "needs work"  # a missing marker is not proof that an older engine cannot defer IME messages
    assert sv.verdict(dict(ok, engine="custom", ime_patch=None))[0] == "needs work"
    assert sv.verdict(dict(ok, drm=["anti-cheat files"]))[0] == "blocked"


def test_runtime_facts_and_routes(sv):
    facts = sv.runtime_facts({"XAUDIO2_7.dll", "MFReadWrite.dll", "bink2w64.dll"}, {"game.exe"})
    assert facts == {"xaudio2": True, "media_foundation": True, "bink": True, "anti_cheat": False}
    assert sv.runtime_facts({"EasyAntiCheat.dll"}, set())["anti_cheat"]
    assert sv.graphics_route({"graphics": ["opengl", "d3d9"]}) == "d3d9"
    assert sv.graphics_route({"graphics": ["d3d12"]}) == "d3d12"
    assert sv.graphics_route({"graphics": ["opengl"]}) == "opengl"


def test_ready_to_paste_profile_and_cache_hint(sv):
    r = {"title": "Game", "exe": "Proj/Binaries/Win64/Game.exe", "exe_size": 123,
         "graphics": ["d3d11"], "large_block_alloc": "FMallocBinned2: fp_mem candidate",
         "runtime": {"media_foundation": True}, "verdict": ("likely OK", "static survey")}
    p = sv.suggested_profile(r)
    assert p["match"] == {"executables": [{"name": "Game.exe", "size": 123}]}
    assert p["status"] == "likely" and p["fp_mem"] == "candidate" and p["env"] == {}
    assert "args" not in p  # preserve Meta's manifest arguments when pasted into the profile catalog
    assert "Media Foundation" in p["notes"]
    assert sv.suggested_profile(dict(r, vr=["OpenVR"]))["revive"] is False
    assert sv.suggested_profile(dict(r, verdict=("blocked", "anti-cheat")))["status"] == "blocked"


def test_survey_per_exe_facts_and_profile(sv, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from frameport.analysis.rift import PEInfo

    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"not executed")
    analysis = SimpleNamespace(xr="OpenVR", engine="Unity", extra={"platform_sdk": False,
                               "exe_candidates": [{"path": "Game.exe"}]})
    monkeypatch.setattr(sv.rift, "analyze", lambda *a: analysis)
    pi = PEInfo("x86", ["opengl32.dll", "mfplat.dll"])
    monkeypatch.setattr(sv.rift, "read_pe", lambda *a, **kw: (pi, b"5.4.2f2"))
    result = sv.survey_game("Game", tmp_path, "Game.exe")
    assert result["exe_32bit"] and result["executables"][0]["exe_32bit"]
    assert result["runtime"]["media_foundation"] and result["suggested_graphics"] == "opengl"
    assert result["profile"]["status"] == "untested"
