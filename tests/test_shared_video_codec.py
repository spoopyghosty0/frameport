"""Shared deployment: no APK edits, renderer changes or per-game opt-in."""
import base64
import hashlib
import importlib.util
import io
import json
import sys
import threading
import types
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def agent_module(monkeypatch, tmp_path):
    if sys.platform == "win32":
        monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace(flock=lambda *_: None, LOCK_EX=2))
    spec = importlib.util.spec_from_file_location("shared_codec_agent", ROOT / "agent/frameport_agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    monkeypatch.setattr(agent, "VIDEO_CODEC_DIR", str(tmp_path / "codec"))
    monkeypatch.setattr(agent, "ANCHORS", str(tmp_path / "apps"))
    return agent


def bundle(revision=2, corrupt=False):
    files = {"libstagefrighthw.so": b"plugin", "media_codecs_frameport.xml": b"xml",
             "podman.py": b"wrapper", "COPYING.FFmpeg": b"license"}
    manifest = {"revision": revision, "runtime_sha256": "runtime",
                "files": {n: hashlib.sha256(data).hexdigest() for n, data in files.items()}}
    raw = json.dumps(manifest).encode()
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("manifest.json", raw)
        for name, payload in files.items():
            z.writestr(name, b"broken" if corrupt and name == "libstagefrighthw.so" else payload)
    return {"bundle": base64.b64encode(data.getvalue()).decode(), "digest": hashlib.sha256(raw).hexdigest()}


@pytest.mark.skipif(sys.platform == "win32", reason="agent publication uses Linux symlinks")
def test_shared_deployment_without_any_apk_and_no_downgrade(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    current = tmp_path / "codec/current"
    replace = agent.os.replace
    activated = []

    def activate(source, destination):
        if Path(destination) == current:
            version = Path(source).resolve()
            assert json.loads((version / "deployment.json").read_text())["scope"] == "shared"
            assert (version / "bin/podman").read_bytes() == b"wrapper"
            assert (version / "libstagefrighthw.so").read_bytes() == b"plugin"
            assert not current.exists()  # no partially published first version
            activated.append(version)
        replace(source, destination)

    monkeypatch.setattr(agent.os, "replace", activate)
    first = bundle(revision=3)
    assert agent.cmd_install_video_codec(first)["digest"] == first["digest"]
    assert len(activated) == 1
    assert agent.cmd_video_codec_status({}) == {"digest": first["digest"], "revision": 3}
    assert agent.cmd_install_video_codec(first)["digest"] == first["digest"]
    assert agent.cmd_install_video_codec(bundle(revision=2))["digest"] == first["digest"]
    assert len(activated) == 1
    with pytest.raises(agent.AgentError, match="checksum mismatch"):
        agent.cmd_install_video_codec(bundle(revision=4, corrupt=True))
    assert agent.cmd_video_codec_status({})["digest"] == first["digest"]
    (current / "libstagefrighthw.so").write_bytes(b"bad on disk")
    assert agent.cmd_video_codec_status({}) == {}


@pytest.mark.skipif(sys.platform == "win32", reason="agent publication uses Linux symlinks")
def test_superseded_versions_are_pruned(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    versions = tmp_path / "codec/versions"
    installed = [agent.cmd_install_video_codec(bundle(revision=r))["digest"] for r in (3, 4)]
    (versions / ".install-stale").mkdir()
    (versions / "notes.txt").write_text("not a codec version")
    installed.append(agent.cmd_install_video_codec(bundle(revision=5))["digest"])
    # The active version and the one it replaced (in-flight launches) remain.
    assert {p.name for p in versions.iterdir()} == {installed[1], installed[2], "notes.txt"}
    assert agent.cmd_video_codec_status({}) == {"digest": installed[2], "revision": 5}


def test_corrupt_bundle_never_activates(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    with pytest.raises(agent.AgentError, match="checksum mismatch"):
        agent.cmd_install_video_codec(bundle(corrupt=True))
    assert not (tmp_path / "codec").exists()


def test_existing_lepton_launchers_upgrade_without_touching_apks(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    for name in ("com.any.game", "cn.vr4p.oculus4xvrplayerov", "rift.game"):
        directory = tmp_path / "apps" / name
        directory.mkdir(parents=True)
        (directory / "game.apk").write_bytes(b"unchanged APK")
        launcher = ("export LEPTON_ENV_FRAMEBRIDGE_CONFIG=\"$app_dir/settings.conf\"\n"
                    + agent.OLD_CODEC_LINE + "\nsetsid /lepton start\n") if name != "rift.game" else "exec proton\n"
        (directory / "launch.sh").write_text(launcher)
    updated = agent.upgrade_launchers()
    assert set(updated) == {"com.any.game", "cn.vr4p.oculus4xvrplayerov"}
    for name in updated:
        directory = tmp_path / "apps" / name
        text = (directory / "launch.sh").read_text()
        assert agent.VIDEO_CODEC_LINE in text and agent.OLD_CODEC_LINE not in text
        assert "surface_native" not in text
        assert (directory / "game.apk").read_bytes() == b"unchanged APK"
    assert agent.upgrade_launchers() == []


def test_connection_deploys_shared_codec_once_and_does_not_rebuild_games(monkeypatch):
    from frameport.frame import connection

    frame = connection.Frame.__new__(connection.Frame)
    frame._video_codec_lock = threading.Lock()
    frame._video_codec_digest = ""
    calls = []

    def remote(command, **args):
        assert args.pop("ensure") is False  # no ensure_agent recursion
        calls.append(command)
        if command == "video_codec_status":
            return {}
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(args["bundle"]))) as z:
            assert "manifest.json" in z.namelist()
            assert not any(name.endswith(".apk") for name in z.namelist())
        return {"digest": args["digest"]}

    monkeypatch.setattr(frame, "agent", remote)
    frame.ensure_video_codec()
    frame.ensure_video_codec()
    assert calls == ["video_codec_status", "install_video_codec"]


def test_newer_shared_codec_is_kept(monkeypatch):
    from frameport.frame import connection

    frame = connection.Frame.__new__(connection.Frame)
    frame._video_codec_lock = threading.Lock()
    frame._video_codec_digest = ""
    calls = []

    def remote(command, **_):
        calls.append(command)
        return {"digest": "newer-version", "revision": 999}

    monkeypatch.setattr(frame, "agent", remote)
    frame.ensure_video_codec()
    assert calls == ["video_codec_status"]
