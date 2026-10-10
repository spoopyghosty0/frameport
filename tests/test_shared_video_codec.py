"""Shared deployment: no APK edits or renderer changes; games opt in through their recipe (frame.hw_video_decode)."""
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
    assert agent.cmd_video_codec_status({}) == {"digest": first["digest"], "revision": 3, "disabled": False}
    assert agent.cmd_install_video_codec(first)["digest"] == first["digest"]
    assert agent.cmd_install_video_codec(bundle(revision=2))["digest"] == first["digest"]
    assert len(activated) == 1
    with pytest.raises(agent.AgentError, match="checksum mismatch"):
        agent.cmd_install_video_codec(bundle(revision=4, corrupt=True))
    assert agent.cmd_video_codec_status({})["digest"] == first["digest"]
    (current / "libstagefrighthw.so").write_bytes(b"bad on disk")
    assert agent.cmd_video_codec_status({}) == {"disabled": False}


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
    assert agent.cmd_video_codec_status({})["digest"] == installed[2]


def test_corrupt_bundle_never_activates(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    with pytest.raises(agent.AgentError, match="checksum mismatch"):
        agent.cmd_install_video_codec(bundle(corrupt=True))
    assert not (tmp_path / "codec").exists()


LEPTON = "export LEPTON_ENV_FRAMEBRIDGE_CONFIG=\"$app_dir/settings.conf\"\n{line}\nchild=''\nsetsid /lepton start\n"


def anchor(agent, tmp_path, name, launcher, patches=(), legacy=False, dep_extra=None):
    directory = tmp_path / "apps" / name
    base = tmp_path / "games" / name
    directory.mkdir(parents=True)
    (base / "lepton-app").mkdir(parents=True)
    (base / "lepton-app/game.apk").write_bytes(b"unchanged APK")
    if legacy:  # agent <= 70 extracted this game's codec from its APK
        (base / "frameport-codec/bin").mkdir(parents=True)
        (base / "frameport-codec/bin/podman").write_text("old wrapper")
    dep = {"package": name, "base": str(base), "recipe": {"patches": list(patches)}, **(dep_extra or {})}
    (directory / "deployment.json").write_text(json.dumps(dep))
    (directory / "launch.sh").write_text(launcher)
    return directory, base


def test_launchers_get_the_codec_line_only_for_games_with_the_patch(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    old, pr = agent.OLD_CODEC_LINES
    plain, _ = anchor(agent, tmp_path, "com.any.game", LEPTON.format(line=old))
    player, _ = anchor(agent, tmp_path, "cn.vr4p.oculus4xvrplayerov", LEPTON.format(line=pr),
                       patches=["frame.adapter", "frame.hw_video_decode"])
    batman, batman_base = anchor(agent, tmp_path, "com.camouflaj.manta", LEPTON.format(line=old), legacy=True)
    other_old, other_base = anchor(agent, tmp_path, "com.old.game", LEPTON.format(line=old))
    (other_base / "frameport-codec").mkdir()  # an APK without codec assets: agent 70 removed its wrapper
    rift, _ = anchor(agent, tmp_path, "rift.game", "exec proton\n")
    assert set(agent.upgrade_launchers()) == {"com.any.game", "cn.vr4p.oculus4xvrplayerov", "com.camouflaj.manta",
                                              "com.old.game"}
    for directory, want in ((plain, False), (player, True), (batman, True), (other_old, False)):
        text = (directory / "launch.sh").read_text()
        assert (agent.VIDEO_CODEC_LINE in text) is want, directory.name
        assert not any(line in text for line in agent.OLD_CODEC_LINES)
        assert text.endswith("child=''\nsetsid /lepton start\n")
        assert "surface_native" not in text
    assert (player / "launch.sh").read_text().count(agent.VIDEO_CODEC_LINE) == 1
    assert (rift / "launch.sh").read_text() == "exec proton\n"
    # Batman's choice is kept, so its old codec folder can go (ensure_host_fixes)
    assert json.loads((batman / "deployment.json").read_text())["hw_video_decode"] is True
    assert set(agent.remove_old_codec_dirs()) == {"com.camouflaj.manta", "com.old.game"}
    assert not (batman_base / "frameport-codec").exists() and not (other_base / "frameport-codec").exists()
    assert (batman_base / "lepton-app/game.apk").read_bytes() == b"unchanged APK"
    assert agent.upgrade_launchers() == []  # idempotent, and Batman keeps the line without its old folder
    assert agent.VIDEO_CODEC_LINE in (batman / "launch.sh").read_text()
    # a reinstall without the patch (finalize writes hw_video_decode: false) drops the line
    dep = json.loads((player / "deployment.json").read_text())
    (player / "deployment.json").write_text(json.dumps(dict(dep, hw_video_decode=False)))
    assert agent.upgrade_launchers() == ["cn.vr4p.oculus4xvrplayerov"]
    assert agent.VIDEO_CODEC_LINE not in (player / "launch.sh").read_text()


def test_unconverted_old_codec_folder_stays(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    _, base = anchor(agent, tmp_path, "com.camouflaj.manta", LEPTON.format(line=agent.OLD_CODEC_LINES[0]),
                     legacy=True)
    assert agent.remove_old_codec_dirs() == []  # its launcher hasn't been converted yet
    assert (base / "frameport-codec/bin/podman").exists()


def test_new_launchers_follow_the_recipe(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    for want in (True, False):
        directory = tmp_path / f"apps/game{want}"
        directory.mkdir(parents=True)
        agent.write_launcher(str(directory), str(tmp_path / "base"), "com.example.game", "Game", 123,
                             "/lepton/lepton", {}, hw_video=want)
        text = (directory / "launch.sh").read_text()
        assert (agent.VIDEO_CODEC_LINE in text) is want
        assert agent.set_codec_line(text, want) == text  # upgrade_launchers leaves it as it is


@pytest.mark.skipif(sys.platform == "win32", reason="runs the launcher line in bash")
@pytest.mark.parametrize("case,expected", [("on", True), ("env", False), ("env0", True), ("flag", False),
                                           ("missing", False)])
def test_codec_line_switches(monkeypatch, tmp_path, case, expected):
    """The launcher line: the shared wrapper first on PATH unless switched off (FramePort's setting writes
    video-codec/disabled; FRAMEPORT_NO_HW_VIDEO=1 in a game's Steam launch options) or not installed."""
    import shutil
    import subprocess

    if not shutil.which("bash"):
        pytest.skip("no bash")
    agent = agent_module(monkeypatch, tmp_path)
    home = tmp_path / "home"
    codec = home / ".local/share/frameport/video-codec"
    if case != "missing":
        (codec / "versions/abc/bin").mkdir(parents=True)
        (codec / "versions/abc/bin/podman").write_text("#!/bin/sh\n")
        (codec / "versions/abc/bin/podman").chmod(0o755)
        (codec / "current").symlink_to("versions/abc")
    if case == "flag":
        (codec / "disabled").write_text("off")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    if case.startswith("env"):
        env["FRAMEPORT_NO_HW_VIDEO"] = "0" if case == "env0" else "1"
    out = subprocess.run(["bash", "-euc", agent.VIDEO_CODEC_LINE + '\necho "$PATH"'], env=env, capture_output=True,
                         text=True, check=True).stdout.strip()
    assert out.startswith(str(codec / "current/bin") + ":") is expected
    assert out.endswith("/usr/bin:/bin")


def test_switch_writes_the_frame_wide_flag(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    assert agent.cmd_video_codec_switch({"enabled": False}) == {"disabled": True}
    assert (tmp_path / "codec/disabled").exists()
    assert agent.cmd_video_codec_switch({"enabled": True}) == {"disabled": False}
    assert not (tmp_path / "codec/disabled").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="agent publication uses Linux symlinks")
def test_same_revision_built_elsewhere_is_kept(monkeypatch, tmp_path):
    """Two PCs whose builds of one revision differ must not replace each other's codec at every connection."""
    agent = agent_module(monkeypatch, tmp_path)
    first = bundle(revision=7)
    agent.cmd_install_video_codec(first)
    other = bundle(revision=7)
    data = io.BytesIO(base64.b64decode(other["bundle"]))
    with zipfile.ZipFile(data) as z:
        files = {n: z.read(n) for n in z.namelist()}
    manifest = json.loads(files["manifest.json"])
    manifest["build"] = "another PC"
    files["manifest.json"] = json.dumps(manifest).encode()
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(rebuilt, "w") as z:
        for name, payload in files.items():
            z.writestr(name, payload)
    other = {"bundle": base64.b64encode(rebuilt.getvalue()).decode(),
             "digest": hashlib.sha256(files["manifest.json"]).hexdigest()}
    result = agent.cmd_install_video_codec(other)
    assert result["kept"] and result["digest"] == first["digest"]
    assert agent.cmd_video_codec_status({})["digest"] == first["digest"]
    assert agent.cmd_install_video_codec(bundle(revision=8))["revision"] == 8  # a newer revision still installs


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


def connection_frame(monkeypatch, replies):
    from frameport.frame import connection

    frame = connection.Frame.__new__(connection.Frame)
    frame._video_codec_lock = threading.Lock()
    frame._video_codec_digest = ""
    calls = []

    def remote(command, **args):
        assert args.pop("ensure") is False
        calls.append((command, args.get("enabled")))
        reply = replies.get(command, {})
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(frame, "agent", remote)
    return frame, calls


def test_failed_install_is_not_retried_on_this_connection(monkeypatch):
    from frameport.frame import connection

    frame, calls = connection_frame(monkeypatch, {"install_video_codec": connection.AgentFailed("disk full")})
    with pytest.raises(connection.AgentFailed):
        frame.ensure_video_codec()
    frame.ensure_video_codec()  # every agent call comes through here: no second ~6 MB upload
    frame._ensure_video_codec_if_supported(b"AGENT_VERSION = 71\n")
    assert [c for c, _ in calls] == ["video_codec_status", "install_video_codec"]


def test_same_revision_on_the_frame_is_kept(monkeypatch):
    revision = json.loads((ROOT / "artifacts/hevc/manifest.json").read_text())["revision"]
    frame, calls = connection_frame(monkeypatch, {"video_codec_status": {"digest": "built-elsewhere",
                                                                         "revision": revision}})
    frame.ensure_video_codec()
    assert [c for c, _ in calls] == ["video_codec_status"]
    frame, calls = connection_frame(monkeypatch, {"video_codec_status": {"digest": "older",
                                                                         "revision": revision - 1}})
    frame.ensure_video_codec()
    assert [c for c, _ in calls] == ["video_codec_status", "install_video_codec"]


def test_setting_off_switches_the_frame_off_without_uploading(monkeypatch):
    from frameport.core import library

    library.set_setting("video.hw_decode", False)
    replies = {"video_codec_status": {"disabled": False}, "video_codec_switch": {"disabled": True}}
    frame, calls = connection_frame(monkeypatch, replies)
    frame.ensure_video_codec()
    assert calls == [("video_codec_status", None), ("video_codec_switch", False)]
    # switched on again in Settings: applied at once (switch + install)
    library.set_setting("video.hw_decode", True)
    replies.update(video_codec_status={"disabled": True}, video_codec_switch={"disabled": False})
    monkeypatch.setattr(frame, "ensure_agent", lambda: frame.ensure_video_codec())
    calls.clear()
    frame.apply_video_decoding()
    assert [c for c, _ in calls] == ["video_codec_status", "video_codec_switch", "install_video_codec"]
    assert calls[1] == ("video_codec_switch", True)
