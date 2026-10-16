"""Experimental Rift-on-Frame mode (fexrift): the Frame helper, the agent commands and the PC side, without a Frame."""
import base64
import hashlib
import importlib.util
import io
import json
import struct
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(monkeypatch, tmp_path, name):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), ROOT / "agent" / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fx(monkeypatch, tmp_path):
    return load(monkeypatch, tmp_path, "fexrift.py")


# ------------------------------------------------------------------------------------------ Meta's signatures
def _sign(payload: bytes, n: int, d: int) -> str:
    k = (n.bit_length() + 7) // 8
    m = b"\x00\x01" + b"\xff" * (k - 3 - len(payload)) + b"\x00" + payload
    return base64.b64encode(pow(int.from_bytes(m, "big"), d, n).to_bytes(k, "big")).decode()


def test_rsa_public_decrypt_type1(fx):
    p, q, e = 2 ** 127 - 1, 2 ** 89 - 1, 65537     # Mersenne primes: a small test key
    n, d = p * q, pow(e, -1, (p - 1) * (q - 1))
    sig = _sign(b"abc123", n, d)
    assert fx.rsa_public_decrypt(sig, keys=[(n, e)]) == "abc123"
    other = (2 ** 61 - 1) * (2 ** 107 - 1)          # wrong key -> refused
    with pytest.raises(fx.FexError):
        fx.rsa_public_decrypt(sig, keys=[(other, e)])


def test_meta_keys_parse(fx):
    keys = [fx.rsa_key(k) for k in fx.META_KEYS_B64]
    assert [n.bit_length() for n, _ in keys] == [4096, 4096] and all(e == 65537 for _, e in keys)


def test_dawn_manifest_order_and_keys(fx):
    m = fx.dawn_manifest({"files": {"a/b.dll": {"size": 1}}, "version": "1.2", "canonicalName": "x", "extra": 5})
    keys = list(m)
    assert keys[0] == "canonicalName" and keys.index("launchFile2D") < keys.index("version") < keys.index("files")
    assert "a\\b.dll" in m["files"]
    assert m["dlcs"] == {} and m["extra"] == 5 and list(m)[-1] == "extra"


# ------------------------------------------------------------------------------------------ game fixes
def _pe(code_at: int, code: bytes) -> bytearray:
    """Minimal PE: one section mapping RVA 0x1000.. to file offset 0x200.."""
    data = bytearray(0x1200)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3c, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x86, 1)            # one section
    struct.pack_into("<H", data, 0x94, 0xf0)         # optional header size
    sec = 0x80 + 24 + 0xf0
    struct.pack_into("<8sIIII", data, sec, b".text", 0x1000, 0x1000, 0x1000, 0x200)
    data[0x200 + code_at - 0x1000:0x200 + code_at - 0x1000 + len(code)] = code
    return data


def test_byte_patch_states(fx, tmp_path):
    patch = {"rva": 0x1010, "old": "c7862001000001000000", "new": "c7862001000000000000"}
    exe = tmp_path / "game.exe"
    exe.write_bytes(_pe(0x1010, bytes.fromhex(patch["old"])))
    assert fx.apply_byte_patch(str(exe), patch) == "patched"
    assert exe.read_bytes()[0x210:0x21a] == bytes.fromhex(patch["new"])
    assert (tmp_path / "game.exe.orig").read_bytes()[0x210:0x21a] == bytes.fromhex(patch["old"])
    assert fx.apply_byte_patch(str(exe), patch) == "already"
    other = tmp_path / "other.exe"
    other.write_bytes(_pe(0x1010, b"\x90" * 10))
    assert fx.apply_byte_patch(str(other), patch) == "unknown build"
    assert not (tmp_path / "other.exe.orig").exists()


def test_first_contact_patch_matches_the_real_instruction(fx):
    p = fx.GAMES["oculus-first-contact"]["ime_patch"]
    old, new = bytes.fromhex(p["old"]), bytes.fromhex(p["new"])
    # mov dword ptr [rsi+0x120], imm32: only the immediate changes (1 -> 0)
    assert old[:6] == new[:6] == bytes.fromhex("c78620010000") and old[6:] == (1).to_bytes(4, "little")
    assert new[6:] == bytes(4)


CVAR_AT, VAR_AT = 0x1800, 0x1900


def _ime_pe(form="member", value=1):
    """Synthetic Unreal-like code: the CVar's UTF-16 name, its registration (lea r8 <variable>; lea rdx <name>) and
    for the member form the constructor's `mov dword ptr [rsi+0x120], value` before it."""
    code = bytearray(0x1000)

    def put(rva, b):
        code[rva - 0x1000:rva - 0x1000 + len(b)] = b
    put(CVAR_AT, ("Slate.DeferWindowsMessageProcessing" + "\0").encode("utf-16-le"))
    if form == "member":
        put(0x1100, bytes.fromhex("c78620010000") + value.to_bytes(4, "little"))
        put(0x1200, bytes.fromhex("4c8d8620010000"))                                   # lea r8, [rsi+0x120]
    elif form == "global":
        put(VAR_AT, value.to_bytes(4, "little"))
        put(0x1200, bytes.fromhex("4c8d05") + struct.pack("<i", VAR_AT - 0x1207))     # lea r8, [rip+var]
    if form != "unreferenced":
        put(0x1207, bytes.fromhex("488d15") + struct.pack("<i", CVAR_AT - 0x120e))    # lea rdx, [rip+name]
    return _pe(0x1000, bytes(code))


def test_find_ime_patch_forms(fx, tmp_path):
    patch, why = fx.find_ime_patch(bytes(_ime_pe("member")))
    assert why is None and patch == {"rva": 0x1100, "old": "c7862001000001000000", "new": "c7862001000000000000"}
    patch, _ = fx.find_ime_patch(bytes(_ime_pe("global")))
    assert patch == {"rva": VAR_AT, "old": "01000000", "new": "00000000"}
    patch, _ = fx.find_ime_patch(bytes(_ime_pe("member", value=0)))    # already patched: same site
    assert patch["rva"] == 0x1100
    exe = tmp_path / "g.exe"
    exe.write_bytes(_ime_pe("member", value=0))
    assert fx.apply_byte_patch(str(exe), patch) == "already"
    assert fx.find_ime_patch(bytes(_ime_pe("unreferenced")))[0] is None
    assert "no Slate" in fx.find_ime_patch(bytes(_pe(0x1000, b"\x90" * 16)))[1]


def test_find_ime_patch_first_contact(fx):
    """The real exe (FRAMEPORT_FIRST_CONTACT_EXE, original or patched): the finder lands on the known site."""
    import os

    path = os.environ.get("FRAMEPORT_FIRST_CONTACT_EXE")
    if not path or not os.path.isfile(path):
        pytest.skip("set FRAMEPORT_FIRST_CONTACT_EXE to First Contact's TouchNUX-Win64-Shipping.exe")
    patch, why = fx.find_ime_patch(open(path, "rb").read())
    assert why is None and patch["rva"] == fx.GAMES["oculus-first-contact"]["ime_patch"]["rva"]


def _meta_game(fx, app, exe_rel, manifest=True, params='-mode="vr" -x'):
    game = Path(fx.META_DIR) / "Software" / "Software" / app
    exe = game / exe_rel
    exe.parent.mkdir(parents=True)
    exe.write_bytes(_ime_pe("member"))
    if manifest:
        man = Path(fx.META_DIR) / "Software" / "Manifests"
        man.mkdir(parents=True, exist_ok=True)
        (man / f"{app}.json").write_text(json.dumps({"canonicalName": app, "launchFile": exe_rel.replace("/", "\\"),
                                                    "launchParameters": params}))
    return exe


def test_manifest_launch(fx):
    _meta_game(fx, "some-game", "Proj/Binaries/Win64/Proj-Win64-Shipping.exe")
    m = fx.manifest_launch("some-game")
    assert m == {"exe": "Proj/Binaries/Win64/Proj-Win64-Shipping.exe", "args": ["-mode=vr", "-x"],
                 "title": "some-game"}
    assert fx.manifest_launch("missing") is None
    (Path(fx.META_DIR) / "Software" / "Manifests" / "bad.json").write_text(json.dumps({"launchFile": "..\\x.exe"}))
    with pytest.raises(fx.FexError):
        fx.manifest_launch("bad")
    assert fx.unreal_project("Proj/Binaries/Win64/P.exe") == "Proj" and fx.unreal_project("Game.exe") is None


def test_install_unverified_needs_force(fx, tmp_path):
    exe = _meta_game(fx, "some-game", "Proj/Binaries/Win64/Proj-Win64-Shipping.exe")
    anchor = str(tmp_path / "anchor")
    with pytest.raises(fx.FexError, match="force_unverified"):
        fx.cmd_install({"app": "some-game", "anchor": anchor})
    assert not (exe.parent / (exe.name + ".orig")).exists()          # refused before anything changed
    res = fx.cmd_install({"app": "some-game", "anchor": anchor, "force_unverified": True, "msaa": 2})
    assert res["verified"] is False and res["ime_patch"] == "patched" and "msaa" not in res   # msaa: per game only
    assert res["checks"]["engine"] == "unreal" and res["checks"]["ime"]["rva"] == 0x1100
    text = (Path(anchor) / "launch.sh").read_text()
    assert "Proj-Win64-Shipping.exe' -mode=vr -x" in text and "FP_MEM" not in text   # fp_mem: per game only
    assert fx.cmd_install({"app": "some-game", "anchor": anchor, "force_unverified": True})["ime_patch"] == "already"


def test_install_verified_uses_the_finder(fx, tmp_path):
    _meta_game(fx, "oculus-first-contact", fx.GAMES["oculus-first-contact"]["exe"], manifest=False)
    res = fx.cmd_install({"app": "oculus-first-contact", "anchor": str(tmp_path / "a"), "msaa": 2})
    assert res["verified"] and res["ime_patch"] == "patched" and res["msaa"] == 2   # the finder's site, not 0x22a605
    ini = Path(fx.USER_DIR) / "AppData/Local/TouchNUX/Saved/Config/WindowsNoEditor/Engine.ini"
    assert "r.MobileMSAA=2" in ini.read_text()
    with pytest.raises(fx.FexError, match="no manifest"):
        fx.cmd_install({"app": "not-here", "anchor": str(tmp_path / "b"), "force_unverified": True})


def test_ini_values_keep_crlf_and_replace(fx, tmp_path):
    ini = tmp_path / "Engine.ini"
    ini.write_bytes(b"[Core.System]\r\nPaths=x\r\n\r\n[SystemSettings]\r\nr.MobileMSAA=4\r\nr.Other=1\r\n")
    fx.set_ini_values(str(ini), "SystemSettings", {"r.MobileMSAA": "2"})
    fx.set_ini_values(str(ini), "/Script/Engine.RendererSettings", {"r.MobileMSAA": "2"})
    text = ini.read_bytes().decode()
    assert "\n" not in text.replace("\r\n", "")
    assert text.count("r.MobileMSAA=2") == 2 and "r.MobileMSAA=4" not in text and "r.Other=1" in text
    assert "[/Script/Engine.RendererSettings]\r\nr.MobileMSAA=2" in text


def test_launcher_renders(fx, tmp_path):
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    exe = fx.write_launcher("oculus-first-contact", fx.GAMES["oculus-first-contact"], str(anchor))
    text = (anchor / "launch.sh").read_text()
    assert exe.endswith("TouchNUX-Win64-Shipping.exe")
    for needle in ("flock -n 9", "FP_NATIVE_WSERVER=1", "FP_MEM=1", "FP_BIGCACHE_MB=256", "taskset -c 4-7",
                   "ReviveInjector.exe", "OVRLibrarian", "wineserver\" -k", "motionSmoothingOverride",
                   "-gamemode=experienceonly", "plays.log", "ENABLE_GAMESCOPE_WSI=0", "unset LD_PRELOAD"):
        assert needle in text, needle
    assert (anchor / "launch.sh").stat().st_mode & 0o100


def test_login_archive_members_are_checked(fx, tmp_path):
    def tar_with(name, kind="file"):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            info = tarfile.TarInfo(name)
            if kind == "sym":
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            t.addfile(info, io.BytesIO(b"") if kind == "file" else None)
        buf.seek(0)
        return tarfile.open(fileobj=buf)
    assert [m.name for m in fx.safe_members(tar_with("sessions/a"))] == ["sessions/a"]
    for bad in ("../x", "/abs", "other/x", "sessions/../../x"):
        with pytest.raises(fx.FexError):
            list(fx.safe_members(tar_with(bad)))
    with pytest.raises(fx.FexError):
        list(fx.safe_members(tar_with("sessions/link", "sym")))


def test_fixes_reg_has_meta_library_and_root(fx):
    der = (ROOT / "artifacts" / "fexrift" / "digicert-assured-id-root-ca.der").read_bytes()
    reg = fx.fixes_reg(der)
    assert "\r\n" in reg and fx.DIGICERT_ROOT_SHA1 in reg
    # Meta's default library exactly as Meta's app writes it in user.reg (lab prefix, where imported games run)
    assert f'"DefaultLibrary"="{fx.LIBRARY_GUID}"' in reg
    assert '"OriginalPath"="C:\\\\Program Files\\\\Meta Horizon\\\\Software"' in reg
    assert ('"Path"="\\\\\\\\?\\\\Volume{00000000-0000-0000-0000-000000000043}\\\\Program Files\\\\Meta Horizon'
            '\\\\Software"') in reg
    assert "Windows.Devices.WiFi.WiFiAdapter" in reg and "[HKEY_LOCAL_MACHINE\\Software\\Revive]" in reg


def test_status_without_setup(fx):
    st = fx.cmd_status({})
    assert st["proton"] is False and st["prefix"] is False and st["login"] is False and st["apps"] == []


# ------------------------------------------------------------------------------------------ agent commands
def test_agent_fex_install_and_uninstall(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path, "frameport_agent.py")
    game_dir = tmp_path / "pfx/drive_c/Program Files/Meta Horizon/Software/Software/oculus-first-contact"
    calls = []

    def fake_helper(cmd, args=None, timeout=900):
        calls.append((cmd, args))
        (Path(args["anchor"])).mkdir(parents=True, exist_ok=True)
        (Path(args["anchor"]) / "launch.sh").write_text("#!/bin/sh\n")
        return {"exe": str(game_dir / "x.exe"), "game_dir": str(game_dir), "title": "Oculus First Contact",
                "launcher": args["anchor"] + "/launch.sh", "ime_patch": "patched"}
    monkeypatch.setattr(a, "fex_helper", fake_helper)
    game_dir.mkdir(parents=True)
    res = a.cmd_fex_install({"package": "rift.oculus_first_contact", "app": "oculus-first-contact", "msaa": 2})
    assert calls[0][0] == "install" and calls[0][1]["msaa"] == 2
    dep = a.deployment("rift.oculus_first_contact")
    assert dep["kind"] == "frame_fex" and dep["base"] == str(game_dir) and dep["fixes"]["ime_patch"] == "patched"
    launcher = f'"{a.ANCHORS}/rift.oculus_first_contact/launch.sh"'
    assert res["appid"] == a.shortcut_appid(launcher, "Oculus First Contact")
    assert a.shortcut_args("rift.oculus_first_contact")[4] == "PC VR on Frame"
    assert calls[0][1]["force_unverified"] is False
    with pytest.raises(a.AgentError):
        a.cmd_fex_install({"package": "rift.oculus_first_contact", "app": "../evil"})
    monkeypatch.setattr(a, "pcvr_pids", lambda base: [])
    out = a.cmd_uninstall({"package": "rift.oculus_first_contact", "keep_data": False})
    assert out["removed"] and game_dir.is_dir()          # the prefix (and the game in it) stays
    assert a.deployment("rift.oculus_first_contact") is None


def test_agent_fex_launch_test_refused(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path, "frameport_agent.py")
    anchor = Path(a.ANCHORS) / "rift.oculus_first_contact"
    anchor.mkdir(parents=True)
    (anchor / "deployment.json").write_text(json.dumps({"package": "rift.oculus_first_contact", "kind": "frame_fex",
                                                        "appid": 1, "base": str(anchor), "title": "X"}))
    with pytest.raises(a.AgentError, match="launch tests"):
        a.cmd_launch_test({"package": "rift.oculus_first_contact", "seconds": 5})


# ------------------------------------------------------------------------------------------ PC side
def test_gate(monkeypatch):
    from frameport import fexrift

    monkeypatch.delenv(fexrift.EXPERIMENT_ENV, raising=False)
    assert not fexrift.enabled()
    with pytest.raises(RuntimeError, match=fexrift.EXPERIMENT_ENV):
        fexrift.require_enabled()
    monkeypatch.setenv(fexrift.EXPERIMENT_ENV, "1")
    assert fexrift.enabled()


def test_package_for():
    from frameport import fexrift

    assert fexrift.package_for("oculus-first-contact") == "rift.oculus_first_contact"
    assert fexrift.package_for("some-game") == "rift.some_game"
    for bad in ("../x", "a*", "A"):
        with pytest.raises(ValueError):
            fexrift.package_for(bad)


def test_patch_revive_refuses_other_builds(tmp_path):
    from frameport import fexrift

    src = tmp_path / "revive"
    src.mkdir()
    (src / "LibRevive64.dll").write_bytes(b"not revive 3.2.0")
    with pytest.raises(RuntimeError, match=fexrift.REVIVE_VERSION):
        fexrift.patch_revive(src, tmp_path / "out")


def test_revive_patch_list_is_consistent():
    from frameport import fexrift

    offs = [o for o, _, _ in fexrift.REVIVE_PATCHES]
    assert offs == sorted(offs)
    assert all(len(a) == len(b) for _, a, b in fexrift.REVIVE_PATCHES)


def test_login_archive(tmp_path):
    from frameport import fexrift

    pfx = tmp_path / "pfx"
    user = pfx / "drive_c/users/someone/AppData/Roaming/Oculus/sessions"
    (user / "_oaf").mkdir(parents=True)              # Meta's framework folder alone isn't a sign-in
    with pytest.raises(RuntimeError, match="sign in"):
        fexrift.login_archive(pfx, ["oculus-first-contact"], tmp_path / "l0.tar")
    user.mkdir(parents=True, exist_ok=True)
    (user / "token").write_text("secret")
    (user / "1234567890-1275611841").mkdir()
    meta = pfx / "drive_c/Program Files/Meta Horizon"
    (meta / "CoreData").mkdir(parents=True)
    (meta / "CoreData" / "x").write_text("1")
    (meta / "Software/Manifests").mkdir(parents=True)
    (meta / "Software/Manifests/oculus-first-contact.json").write_text("{}")
    (meta / "Software/Software/oculus-first-contact/TouchNUX").mkdir(parents=True)
    (meta / "Software/Software/oculus-first-contact/TouchNUX/a.exe").write_text("MZ")
    out = tmp_path / "login.tar"
    info = fexrift.login_archive(pfx, ["oculus-first-contact", "missing-app"], out)
    assert info["apps"] == ["oculus-first-contact"]
    assert out.stat().st_mode & 0o077 == 0                # owner-only (holds sign-in tokens)
    with tarfile.open(out) as t:
        names = set(t.getnames())
    assert {"sessions/token", "CoreData/x", "Manifests/oculus-first-contact.json",
            "Software/oculus-first-contact/TouchNUX/a.exe"} <= names
    with pytest.raises(RuntimeError, match="sign in"):
        fexrift.login_archive(tmp_path / "empty", ["x"], tmp_path / "l2.tar")


def test_artifact_files_listed():
    from frameport import fexrift

    rels = {rel for _, rel in fexrift.artifact_files()}
    assert {"fexrift.py", "fp_mem.so", "wineserver.arm64", "windows.devices.wifi.dll",
            "digicert-assured-id-root-ca.der", "dlls/crypt32.dll", "dlls/sechost.dll", "dlls/dnsapi.dll",
            "dlls/windows.devices.enumeration.dll"} <= rels
    root = ROOT / "artifacts" / "fexrift"
    sums = dict(reversed(line.split(None, 1)) for line in (root / "SHA256SUMS").read_text().splitlines())
    for rel, digest in ((r.lstrip("./"), d) for r, d in sums.items()):
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == digest, rel
