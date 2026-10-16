"""Compatibility profiles/graphics/old Unreal modules, without Wine, FEX, network or the owner's dumps."""
import hashlib
import importlib.util
import json
import shutil
import struct
from pathlib import Path

import pytest
from test_fexrift import _ime_pe, _meta_game
from test_fexrift_ext import load_copy

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fx(monkeypatch, tmp_path):
    core = load_copy(monkeypatch, tmp_path)
    shutil.copy2(ROOT / "agent/fexrift_compat.py", Path(core.HERE) / "fexrift_compat.py")
    core.load_extensions()
    return core


def compat():
    spec = importlib.util.spec_from_file_location("compat", ROOT / "agent/fexrift_compat.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_profile_match_modes_and_ambiguity(tmp_path):
    path = tmp_path / "Game.exe"
    path.write_bytes(b"game")
    mod = compat()
    for match in ({"canonical_names": ["studio-game"]},
                  {"sha256": [hashlib.sha256(b"game").hexdigest()]},
                  {"executables": [{"name": "game.EXE", "size": 4}]}):
        profile = {"match": match, "status": "likely"}
        assert mod.match_profile({"profile": profile}, "studio-game", str(path)) == ("profile", profile)
    assert mod.match_profile({"p": {"match": {"executables": [{"name": "Game.exe", "size": 5}]}}},
                             "unknown", str(path)) == (None, None)
    with pytest.raises(ValueError, match="ambiguous"):
        mod.match_profile({k: {"match": {"canonical_names": ["same"]}} for k in ("a", "b")}, "same", "")


def test_likely_installs_with_warning_and_without_fp_mem(fx, tmp_path):
    exe = _meta_game(fx, "manifest-name", "Proj/Binaries/Win64/Game.exe")
    fx.GAMES["survey-name"] = {"match": {"executables": [{"name": exe.name, "size": exe.stat().st_size}]},
                               "status": "likely", "graphics": "d3d11", "fp_mem": "candidate"}
    info, verified = fx.game_info("manifest-name")
    assert info["exe"] == "Proj/Binaries/Win64/Game.exe" and not verified
    result = fx.cmd_install({"app": "manifest-name", "anchor": str(tmp_path / "anchor")})
    assert result["warnings"] and result["verified"] is False
    assert result["checks"]["profile_warnings"] == result["warnings"]
    assert result["checks"]["profile"] == "survey-name" and result["checks"]["fp_mem"] == "candidate"
    text = Path(result["launcher"]).read_text()
    assert "export FP_MEM=1" not in text and "graphics=d3d11" in text
    assert "ReviveInjector.exe" in text


def test_untested_requires_force_and_blocked_cannot_be_forced(fx, tmp_path):
    exe = _meta_game(fx, "test", "Proj/Binaries/Win64/Game.exe")
    fx.GAMES["test"] = {"status": "untested"}
    args = {"app": "test", "anchor": str(tmp_path / "anchor")}
    with pytest.raises(fx.FexError, match="force_unverified"):
        fx.cmd_install(args)
    assert not Path(str(exe) + ".orig").exists()
    assert fx.cmd_install(dict(args, force_unverified=True))["verified"] is False
    fx.GAMES["test"]["status"] = "blocked"
    with pytest.raises(fx.FexError, match="blocked"):
        fx.cmd_install(dict(args, force_unverified=True))


def test_profile_key_does_not_bypass_its_build_match(fx):
    _meta_game(fx, "same-name", "Proj/Binaries/Win64/Game.exe")
    fx.GAMES["same-name"] = {"match": {"executables": [{"name": "Game.exe", "size": 99}]},
                             "status": "likely", "graphics": "d3d12"}
    info, verified = fx.game_info("same-name")
    assert not verified and "status" not in info and "graphics" not in info


@pytest.mark.parametrize("route", ["d3d11", "d3d9", "d3d12", "opengl", "vulkan"])
def test_launcher_routes_and_config_are_scoped_to_anchor(fx, tmp_path, route):
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    info = {"exe": "game.exe", "title": "Game", "graphics": route, "revive": False,
            "dxvk": ["dxgi.maxFrameRate = 72", "d3d11.zeroInitWorkgroupMemory = True"]}
    fx.write_launcher("test", info, str(anchor))
    text = (anchor / "launch.sh").read_text()
    assert "unset PROTON_USE_WINED3D PROTON_NO_D3D12 PROTON_NO_D3D11" in text
    assert ("export PROTON_USE_WINED3D=1" in text) == (route == "opengl")
    assert 'export DXVK_CONFIG_FILE="$A/dxvk.conf"' in text and ' > "$A/dxvk.conf"' in text
    assert "ReviveInjector.exe" not in text
    assert 'taskset -c "$FP_CORES" "$G/proton" run ' in text and "FP_CORES=4-7" in text


def test_32bit_and_vkd3d_layout_report(fx, tmp_path, monkeypatch):
    exe = tmp_path / "game.exe"
    data = _ime_pe()
    struct.pack_into("<H", data, 0x84, 0x14c)
    exe.write_bytes(data)
    ge = tmp_path / "ge"
    dll = ge / "files/lib64/wine/vkd3d-proton/d3d12.dll"
    dll.parent.mkdir(parents=True)
    dll.touch()
    monkeypatch.setattr(fx, "GE", str(ge))
    facts = fx.INSTALL_CHECK_HOOKS[0]("test", {"graphics": "d3d12"}, {"exe": str(exe)})
    assert facts["exe_32bit"] and "untested" in facts["wow64"]
    assert facts["vkd3d_dlls"] == ["files/lib64/wine/vkd3d-proton/d3d12.dll"]


def test_old_modular_unreal_core_has_the_same_ime_cvar(fx, tmp_path):
    # Old Core contains FWindowsApplication; Slate itself and the exe need not contain the registration at all.
    exe = tmp_path / "RoboRecall-Win64-Shipping.exe"
    exe.write_bytes(b"no registration here")
    core = tmp_path / "RoboRecall-Core-Win64-Shipping.dll"
    core.write_bytes(_ime_pe())
    target, patch, why = fx.find_ime_target([str(exe)])
    assert target == str(core) and patch["rva"] == 0x1100 and why is None
    assert fx.apply_byte_patch(target, patch) == "patched"
    assert fx.find_ime_target([str(exe)])[1] == patch       # repeated install is idempotent
    core.write_bytes(_ime_pe("unreferenced"))
    assert fx.find_ime_target([str(exe)])[1] is None         # a CVar string alone is insufficient


def test_profiles_do_not_enable_candidate_caches():
    games = json.loads((ROOT / "agent/fexrift_games.json").read_text())
    for key, info in games.items():
        if key.startswith("_"):
            continue
        assert info.get("status", "verified") in compat().STATUSES
        if info.get("status") != "verified":
            assert info.get("fp_mem") != "enabled" and info.get("env", {}).get("FP_MEM") != "1"


@pytest.mark.parametrize(("log", "sig"), [
    ("OVRServer: Missing entitlement", "fex-missing-entitlement"),
    ("OVRServer: app status install_available", "fex-registration-missing"),
    ("Rejecting new committed universe since IMU fallback is active", "fex-imu-fallback"),
    ("OVRLibrarian: Service invocation completed\nFramePort FEX: OVRLibrarian still running", "fex-librarian-stuck"),
    ("err:vkd3d: Failed to create Vulkan device", "fex-vkd3d-init"),
    ("err:wined3d:wined3d_adapter_gl_init Failed to initialize OpenGL", "fex-wined3d-init"),
    ("ReviveInjector: Failed to inject process", "fex-revive-injector"),
])
def test_fex_triage(log, sig):
    from frameport.validate.triage import triage

    result = triage(log, "EXITED", "rift.test")
    finding = next(f for f in result.findings if f.id == sig)
    assert finding.suggest == []
    if sig == "fex-missing-entitlement":
        assert finding.severity == "info" and "not a FramePort bug" in finding.diagnosis


def test_fex_triage_does_not_treat_normal_completion_as_hang():
    from frameport.validate.triage import triage

    log = "OVRLibrarian: Service invocation completed\nReviveInjector: Succesfully injected!"
    result = triage(log, "RUNNING", "rift.test")
    assert not any(f.id in ("fex-librarian-stuck", "fex-revive-injector") for f in result.findings)
