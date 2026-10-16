"""Local-copy Rift installation: no Frame, source dumps, network or login state required."""
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import types
import zlib
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from frameport import fexinstall
from frameport.core.events import Cancelled, Reporter

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fx(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in ("fexrift.py", "fexrift_install.py", "fexrift_games.json"):
        shutil.copy2(ROOT / "agent" / name, artifacts / name)
    core = load(artifacts / "fexrift.py")
    core.load_extensions()
    calls = []
    monkeypatch.setattr(core, "wine", lambda *args, **kw: calls.append((args, kw)))
    core.reg_calls = calls
    return core


@pytest.fixture
def game(tmp_path):
    folder = tmp_path / "Demo v1.2.3"
    (folder / "Game/Config").mkdir(parents=True)
    (folder / "Game/Game.exe").write_bytes(b"game bytes")
    (folder / "Game/Config/DefaultEngine.ini").write_text("[OnlineSubsystemOculus]\nRiftAppId=1184903171584429\n")
    (folder / "content.bin").write_bytes(b"data\0" * 20)
    return folder


def entry(game):
    return {"kind": "rift", "package": "rift.demo", "game_dir": str(game), "exe": "Game/Game.exe",
            "title": "Demo", "analysis": {"extra": {"launch_args": ["-a", "two words"]}},
            "details": {"sources": ["oculusdb"], "store_url": "https://www.meta.com/experiences/1217155751659625/"}}


def upload(fx, game, dest=None):
    info = fexinstall.source_info(game, entry(game))
    prep = fx.COMMANDS["prepare_local"]({"app": info["app"], "package": info["package"], "files": info["files"],
                                        "dest": str(dest) if dest else None})
    for rel in info["files"]:
        target = Path(prep["game_dir"]) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(game / rel, target)
    return info, prep


def register(fx, info):
    return fx.COMMANDS["register_local"]({k: v for k, v in info.items() if k not in ("folder", "files")})


@pytest.mark.parametrize("name", ["Crack.7z", "crack.7z.001", "CRACK.7Z.01", "Data.bin", "AGRepackInstaller.exe",
                                  "crack.7z.001.bak",
                                  "VD.bat", "Game.exe.AG", "x/Original.dll.ag"])
def test_skipped(name):
    assert fexinstall.skipped_file(name)
    ext = load(ROOT / "agent/fexrift_install.py")
    assert ext.skipped(name)


def test_inventory_does_not_read_or_upload_repack_leftovers(game):
    for name in ("crack.7z.001", "Data.bin", "Game.exe.AG", "VD.bat", "AGRepackInstaller.exe"):
        (game / name).write_bytes(b"untouched")
    info = fexinstall.source_info(game, entry(game))
    assert len(info["skipped"]) == 5 and len(info["files"]) == 3
    assert info["version"] == "1.2.3"
    assert info["app_id"] == "1184903171584429"
    assert info["store_app_id"] == "1217155751659625"  # distinct game and store IDs both survive
    assert info["app_id_source"].startswith("config:")


def test_app_id_from_pak(game):
    ini = b"[OnlineSubsystemOculus]\nAppId=333\nOculusAppId=222\nRiftAppId=111\n"
    def fstr(text):
        data = text.encode() + b"\0"
        return struct.pack("<i", len(data)) + data
    header = struct.pack("<qqqi", 0, len(ini), len(ini), 0) + bytes(20) + bytes(5)
    body = header + ini
    index = fstr("../../../") + struct.pack("<i", 1) + fstr("Game/Config/DefaultEngine.ini") + header
    footer = struct.pack("<IIqq", 0x5A6F12E1, 3, len(body), len(index)) + bytes(20)
    (game / "content.pak").write_bytes(body + index + footer)
    info = fexinstall.source_info(game, entry(game), app_id="999")
    assert info["app_id"] == "111" and info["app_id_source"] == "config:content.pak:Game/Config/DefaultEngine.ini"


def test_id_fallbacks_and_other_engine_config(game):
    (game / "Game/Config/DefaultEngine.ini").unlink()
    info = fexinstall.source_info(game, entry(game), app_id="999")
    assert info["app_id_source"] == "oculusdb:details.store_url"
    e = entry(game)
    e.pop("details")
    assert fexinstall.source_info(game, e, app_id="999")["app_id_source"] == "cli:--app-id"
    with pytest.raises(ValueError, match="supply --app-id"):
        fexinstall.source_info(game, e)
    (game / "OculusConfig.json").write_text('{"RiftAppId":"1234"}')
    assert fexinstall.source_info(game, e)["app_id"] == "1234"
    assert fexinstall.ini_app_id("[Other]\nAppId=123\n[OnlineSubsystemOculus]\n;AppId=456") is None


@pytest.mark.parametrize("bad", ["../escape", "/tmp/x", "C:/x", "bad/name", "xx\n", "xx\\yy", "."])
def test_canonical_rejected(fx, bad):
    with pytest.raises(ValueError):
        fx.COMMANDS["prepare_local"]({"app": bad, "package": "rift.x", "files": {}})


@pytest.mark.parametrize("bad", ["../Game.exe", "/Game.exe", "C:\\Game.exe", "a/../../Game.exe", "a//Game.exe"])
def test_exe_escape_rejected(fx, game, bad):
    info, _ = upload(fx, game)
    info["exe"] = bad
    with pytest.raises(ValueError):
        register(fx, info)
    with pytest.raises(ValueError):
        fexinstall.relative_file(bad)


def test_manifest_fixture_shape_and_local_registration(fx, game):
    info, prep = upload(fx, game)
    before = {r: (game / r).read_bytes() for r in info["files"]}
    out = register(fx, info)
    mdir = Path(prep["library"]) / "Manifests"
    m = json.loads((mdir / "demo.json").read_text())
    mini = json.loads((mdir / "demo.json.mini").read_text())
    for name, manifest in (("oculus-first-contact.json", m), ("oculus-first-contact.json.mini", mini)):
        fixture = json.loads((ROOT / "tests/fixtures/fexrift" / name).read_text())
        assert list(manifest) == list(fixture)
        assert {k: type(v) for k, v in manifest.items()} == {k: type(v) for k, v in fixture.items()}
        sample = next(iter(fixture["files"].values()))
        for f in manifest["files"].values():
            assert list(f) == list(sample)
            assert {k: type(v) for k, v in f.items()} == {k: type(v) for k, v in sample.items()}
    assert m["appId"] == info["app_id"] and m["launchFile"] == "Game/Game.exe"
    assert m["redistributables"] == [] and m["versionCode"] == 1
    assert set(mini["files"]) == {"Game\\Game.exe"}
    for rel, original in before.items():
        assert (Path(prep["game_dir"]) / rel).read_bytes() == original
        fm = m["files"][rel.replace("/", "\\")]
        assert fm["sha256"] == hashlib.sha256(original).hexdigest()
        assert fm["segments"] == [[0, fm["sha256"], len(zlib.compress(original))]]
    raw = (mdir / "demo.json").read_bytes()
    assert b"\r\n" in raw and b"\r\r\n" not in raw
    assert out["source"] == "local_copy" and out["store_app_id"] != out["app_id"]
    inf, verified = fx.game_info("demo")
    assert not verified and inf["game_dir"] == prep["game_dir"]
    assert inf["args"] == ["-a", "two words"]
    fx.write_launcher("demo", inf, str(game))
    assert prep["game_dir"] + "/Game" in (game / "launch.sh").read_text()
    assert "-a 'two words'" in (game / "launch.sh").read_text()
    assert fx.reg_calls[0][0][:2] == ("regedit", "/S")
    assert "DefaultLibrary" in (Path(fx.ROOT) / "local-library.reg").read_text()
    (mdir / "demo.json").unlink()
    with pytest.raises(ValueError, match="manifest is missing"):
        fx.game_info("demo")


def test_chunk_hashes(fx, tmp_path):
    ext = load(ROOT / "agent/fexrift_install.py")
    path = tmp_path / "large"
    data = b"x" * (ext.SEGMENT_SIZE + 3)
    path.write_bytes(data)
    m = ext.hash_file(path)
    assert len(m["segments"]) == 2
    assert m["segments"][1] == [0, hashlib.sha256(b"xxx").hexdigest(), len(zlib.compress(b"xxx"))]
    assert m["sha256"] == hashlib.sha256(data).hexdigest()
    path.write_bytes(b"")
    assert ext.hash_file(path)["segments"] == []


def test_incomplete_upload_and_symlinks(fx, game, tmp_path):
    info, prep = upload(fx, game)
    target = Path(prep["game_dir"]) / "content.bin"
    target.unlink()
    with pytest.raises(ValueError, match="incomplete"):
        register(fx, info)
    outside = tmp_path / "outside"
    outside.write_bytes(b"anything")
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        register(fx, info)
    with pytest.raises(ValueError, match="symlink"):
        fexinstall.inventory(Path(prep["game_dir"]))


def test_resume_changed_source_and_foreign_collision(fx, game):
    info, prep = upload(fx, game)
    args = {"app": info["app"], "package": info["package"], "files": info["files"]}
    resumed = fx.COMMANDS["prepare_local"](args)
    assert len(resumed["existing"]) == 3
    part = Path(prep["game_dir"]) / "content.bin.part"
    part.write_bytes(b"old partial")
    (game / "content.bin").write_bytes(b"new!\0" * 20)  # same size; a cancelled retry must not retain the old bytes
    new_time = info["files"]["content.bin"]["mtime_ns"] + 1000000000
    os.utime(game / "content.bin", ns=(new_time, new_time))
    args["files"] = fexinstall.inventory(game)[0]
    resumed = fx.COMMANDS["prepare_local"](args)
    assert "content.bin" not in resumed["existing"] and not part.exists()
    resumed = fx.COMMANDS["prepare_local"](args)
    assert "content.bin" not in resumed["existing"]
    with pytest.raises(ValueError, match="another package"):
        fx.COMMANDS["prepare_local"](dict(args, package="rift.other"))
    del args["files"]["content.bin"]
    with pytest.raises(ValueError, match="removed files"):
        fx.COMMANDS["prepare_local"](args)
    other = Path(prep["game_dir"]).with_name("foreign")
    other.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        fx.COMMANDS["prepare_local"](dict(args, app="foreign"))


def test_uninstall_default_and_external_library(fx, game, tmp_path):
    info, prep = upload(fx, game)
    register(fx, info)
    fx.COMMANDS["unregister_local"]({"app": info["app"], "package": info["package"]})
    assert not Path(prep["game_dir"]).exists()
    assert not (Path(prep["library"]) / "Manifests/demo.json").exists()
    assert len(fx.reg_calls) == 1                      # default library is shared and remains registered
    assert Path(fx.PFX).is_dir()
    drive = tmp_path / "mount"
    drive.mkdir()
    info, prep = upload(fx, game, drive)
    register(fx, info)
    assert Path(prep["game_dir"]) == drive / "FramePort/fexrift-games/demo"
    inf, _ = fx.game_info("demo")
    assert inf["game_dir"] == prep["game_dir"]
    assert (Path(prep["library"]) / "Software/demo/Game/Game.exe").is_file()
    volume = Path(fx.PFX) / "dosdevices" / ("volume{" + prep["library_guid"] + "}")
    assert volume.resolve() == drive
    reg = (Path(fx.ROOT) / "local-library.reg").read_text()
    assert "OriginalPath" in reg and "Volume{" in reg and "DefaultLibrary" not in reg
    fx.COMMANDS["unregister_local"]({"app": "demo", "package": "rift.demo"})
    assert not Path(prep["game_dir"]).exists()
    assert "[-HKEY_CURRENT_USER" in (Path(fx.ROOT) / "local-library.reg").read_text()


def test_agent_deployment_and_uninstall(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    agent = load(ROOT / "agent/frameport_agent.py")
    calls = []
    folder = tmp_path / "game"
    folder.mkdir()
    local = {"source": "local_copy", "registered": True, "package": "rift.demo", "app_id": "123",
             "app_id_source": "config:DefaultEngine.ini", "skipped": ["Data.bin"], "dest": None}
    def helper(cmd, args, **kwargs):
        calls.append(cmd)
        if cmd == "local_info":
            return local
        if cmd == "unregister_local":
            shutil.rmtree(folder)
            return {"removed": True}
        Path(args["anchor"]).mkdir(parents=True)
        return {"game_dir": str(folder), "exe": str(folder / "Game.exe"), "title": "Demo",
                "launcher": str(Path(args["anchor"]) / "launch.sh")}
    monkeypatch.setattr(agent, "fex_helper", helper)
    monkeypatch.setattr(agent, "pcvr_pids", lambda _: [])
    agent.cmd_fex_install({"app": "demo", "package": "rift.demo", "source": "local_copy"})
    dep = agent.deployment("rift.demo")
    assert dep["source"] == "local_copy" and dep["app_id"] == "123" and dep["skipped"] == ["Data.bin"]
    assert agent.cmd_uninstall({"package": "rift.demo"})["removed"]
    assert "unregister_local" in calls and not folder.exists()
    monkeypatch.setattr(agent, "cmd_drives", lambda _: {"drives": []})
    with pytest.raises(agent.AgentError, match="available external drive"):
        agent.cmd_fex_prepare_local({"app": "demo", "package": "rift.demo", "dest": "/missing", "files": {}})


def test_cli_and_real_orchestration_with_fake_transfer(monkeypatch, game):
    from frameport import cli
    from frameport.core import library
    from frameport.install import installer
    monkeypatch.setenv("FRAMEPORT_EXPERIMENTAL_FEX", "1")
    monkeypatch.setattr(library, "load", lambda: {"games": {"rift.demo": entry(game)}})
    calls, transfers = [], []
    class Frame:
        def agent(self, cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd == "fex_prepare":
                return {"artifacts": "/frame/artifacts"}
            if cmd == "fex_prepare_local":
                return {"game_dir": "/prefix/game", "existing": {"content.bin": 100}, "free_bytes": 20 << 30}
            if cmd == "fex_install":
                return {"verified": False}
            if cmd == "fex_runtime_state":
                return {"runtime_state": "ready"}
            return {}
    frame = Frame()
    monkeypatch.setattr(cli, "_fex_target", lambda _: types.SimpleNamespace(frame=frame))
    @contextmanager
    def link(fr, reporter, need):
        assert fr is frame
        yield "fast link"
    monkeypatch.setattr(installer, "transfer_link", link)
    monkeypatch.setattr(installer, "upload_files", lambda *args: transfers.append(args))
    result = CliRunner().invoke(cli.app, ["fex", "install", "--from-library", "rift.demo", "--force-unverified",
                                           "--no-steam", "--runtime-check"])
    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls] == ["fex_prepare", "fex_prepare_local", "fex_register_local", "fex_install",
                                     "fex_runtime_state"]
    # first upload: the helper scripts (a fixed helper must reach a Frame set up earlier), then the game
    assert transfers[0][2] == "/frame/artifacts" and {r for _, r, _ in transfers[0][1]} >= {"fexrift.py"}
    assert transfers[1][0] == "fast link"
    assert "content.bin" not in [r for _, r, _ in transfers[1][1]]
    assert calls[2][1]["app_id"] == "1184903171584429"
    calls.clear()
    monkeypatch.setattr(fexinstall.rift, "analyze", lambda _: types.SimpleNamespace(
        extra={"exe": "Game/Game.exe", "exe_confirmed": True}))
    result = CliRunner().invoke(cli.app, ["fex", "install", "--folder", str(game), "--title", "Local demo",
                                           "--force-unverified", "--no-steam", "--dest", "/mnt/card"])
    assert result.exit_code == 0, result.output
    assert calls[1][1]["dest"] == "/mnt/card" and calls[1][1]["app"] == "local-demo"
    result = CliRunner().invoke(cli.app, ["fex", "install", "--folder", str(game), "--from-library", "rift.demo"])
    assert result.exit_code != 0


def test_no_space_cancel_and_unverified_before_upload(monkeypatch, game):
    from frameport.core import library
    monkeypatch.setenv("FRAMEPORT_EXPERIMENTAL_FEX", "1")
    monkeypatch.setattr(library, "load", lambda: {"games": {"rift.demo": entry(game)}})
    from frameport.install import installer
    monkeypatch.setattr(installer, "upload_files", lambda *args: None)
    frame = types.SimpleNamespace(agent=lambda *a, **kw: {"game_dir": "/game", "existing": {}, "free_bytes": 0,
                                                          "artifacts": "/frame/artifacts"})
    with pytest.raises(ValueError, match="unverified"):
        fexinstall.install(frame, Reporter(), from_library="rift.demo")
    with pytest.raises(RuntimeError, match="not enough space"):
        fexinstall.install(frame, Reporter(), from_library="rift.demo", force_unverified=True)
    reporter = Reporter()
    reporter.cancelled.set()
    with pytest.raises(Cancelled):
        fexinstall.install(frame, reporter, from_library="rift.demo", force_unverified=True)


@pytest.mark.parametrize("state", ["ready", "install_available"])
def test_runtime_state_uses_only_new_lines_and_reuses_server(fx, monkeypatch, state):
    probe = fx.COMMANDS["runtime_state"]
    sub = probe.__globals__["subprocess"]
    log = Path(fx.USER_DIR) / "AppData/Local/Oculus/Service_123.txt"
    log.parent.mkdir(parents=True)
    log.write_text("demo state: ready\n")  # old state must not be used
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        with log.open("a") as f:
            f.write(f"demo state: {state}\n")
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(sub, "run", run)
    result = probe({"app": "demo"})
    assert result == {"runtime_state": state, "reused_server": True}
    assert len(calls) == 1 and calls[0][0] == "pgrep"  # never stop a runtime we didn't start


def test_runtime_state_starts_and_stops_own_server(fx, monkeypatch):
    probe = fx.COMMANDS["runtime_state"]
    sub = probe.__globals__["subprocess"]
    log = Path(fx.USER_DIR) / "AppData/Local/Oculus/OVRServer.log"
    log.parent.mkdir(parents=True)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return types.SimpleNamespace(returncode=1)
    def start(argv, **kwargs):
        calls.append(argv)
        assert kwargs["stdout"] == sub.DEVNULL and kwargs["stderr"] == sub.DEVNULL
        log.write_text("demo state: ready\n")
        return types.SimpleNamespace(poll=lambda: None, wait=lambda **kw: None)
    monkeypatch.setattr(sub, "run", run)
    monkeypatch.setattr(sub, "Popen", start)
    monkeypatch.setattr(fx, "wine_env", lambda: {})
    result = probe({"app": "demo"})
    assert result == {"runtime_state": "ready", "reused_server": False}
    assert calls[-1][-1] == "-k"


def test_large_inventory_sent_to_helper_on_stdin(monkeypatch, tmp_path):
    agent = load(ROOT / "agent/frameport_agent.py")
    helper = tmp_path / "fexrift.py"
    helper.touch()
    monkeypatch.setattr(agent, "FEX_HELPER", str(helper))
    received = []
    def run(argv, **kwargs):
        received.append((argv, kwargs))
        return types.SimpleNamespace(stdout='{"ok":true,"result":{}}')
    monkeypatch.setattr(agent, "run", run)
    agent.fex_helper("prepare_local", {"files": {"x": "x" * 200000}})
    assert received[0][0][-1] == "prepare_local"
    assert len(received[0][1]["input"]) > 200000


def test_runtime_state_installed_from_package_manager_line(fx, monkeypatch):
    """The PackageManager start-up listing (seen on the Frame) counts as registered when no state line comes."""
    probe = fx.COMMANDS["runtime_state"]
    sub = probe.__globals__["subprocess"]
    log = Path(fx.USER_DIR) / "AppData/Local/Oculus/Service_123.txt"
    log.parent.mkdir(parents=True)
    log.write_text("")

    def run(argv, **kwargs):
        with log.open("a") as f:
            f.write("15/10 20:37:03.294 [ 404] {INFO} [PackageManager] Currently installed package: demo-other, version code: 1\n"
                    "15/10 20:37:03.294 [ 404] {INFO} [PackageManager] Currently installed package: demo, version code: 1\n")
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(sub, "run", run)
    assert probe({"app": "demo"})["runtime_state"] == "installed"


def test_folder_in_a_meta_library_uses_metas_manifest(tmp_path):
    """C:\\Program Files\\Meta Horizon\\Software layout: Meta's manifest gives exe, -vr arguments, ID and version."""
    lib = tmp_path / "Meta Horizon" / "Software"
    game = lib / "Software" / "oculus-dreamdeck"
    exe = game / "WindowsNoEditor/Dreamdeck/Binaries/Win64/Dreamdeck-Win64-Shipping.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ" + b"\0" * 64)
    (lib / "Manifests").mkdir()
    (lib / "Manifests" / "oculus-dreamdeck.json").write_text(json.dumps({
        "canonicalName": "oculus-dreamdeck", "appId": "941682542593981", "version": "1.3",
        "launchFile": "WindowsNoEditor\\Dreamdeck\\Binaries\\Win64\\Dreamdeck-Win64-Shipping.exe",
        "launchParameters": "-fullscreen -vr"}), encoding="utf-8")
    info = fexinstall.source_info(game, title="Oculus Dreamdeck")
    assert info["exe"] == "WindowsNoEditor/Dreamdeck/Binaries/Win64/Dreamdeck-Win64-Shipping.exe"
    assert info["args"] == ["-fullscreen", "-vr"] and info["version"] == "1.3"
    assert (info["app"], info["app_id"], info["app_id_source"]) == ("oculus-dreamdeck", "941682542593981",
                                                                     "meta-manifest")
    # a manifest for another name is not trusted
    (lib / "Manifests" / "oculus-dreamdeck.json").write_text(json.dumps({"canonicalName": "other", "appId": "1",
                                                                         "launchFile": "x.exe"}))
    assert fexinstall.meta_manifest(game) is None


def test_launch_args_from_the_pcs_meta_library(tmp_path, monkeypatch):
    """A copy outside Meta's library gets the launch arguments of the same build in the user's Meta app library."""
    from frameport.core import winhost
    lib = tmp_path / "Meta Horizon" / "Software"
    (lib / "Manifests").mkdir(parents=True)
    (lib / "Manifests" / "oculus-dreamdeck.json").write_text(json.dumps({
        "canonicalName": "oculus-dreamdeck", "appId": "941682542593981", "launchParameters": "-fullscreen -vr",
        "launchFile": "WindowsNoEditor\\Dreamdeck\\Binaries\\Win64\\Dreamdeck-Win64-Shipping.exe"}))
    monkeypatch.setattr(winhost, "meta_libraries", lambda: [lib])
    m = fexinstall.library_manifest("941682542593981", None, "x/Dreamdeck-Win64-Shipping.exe")
    assert m and m["launchParameters"] == "-fullscreen -vr"
    assert fexinstall.library_manifest("941682542593981", None, "x/Other-Win64-Shipping.exe") is None   # other build
    assert fexinstall.library_manifest("1", "other", "x/Dreamdeck-Win64-Shipping.exe") is None


def test_meta_libraries_on_this_pc():
    """Real lookup on a PC with Meta's app (skipped elsewhere): every result holds a Manifests folder."""
    from frameport.core import winhost
    libs = winhost.meta_libraries()
    if not libs:
        pytest.skip("no Meta app library on this PC")
    assert all((p / "Manifests").is_dir() for p in libs)
