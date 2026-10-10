"""Codec packaging and per-game container isolation; no game assets needed."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_assets_have_matching_checksums():
    directory = ROOT / "artifacts/hevc"
    manifest = json.loads((directory / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        path = directory / (name + ".txt" if name == "podman.py" else name)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    assert (directory / "podman.py.txt").read_bytes() == (ROOT / "native/hevc/podman.py").read_bytes()


def test_mounts_only_matching_game_and_runtime(tmp_path, monkeypatch):
    wrapper = load_module(ROOT / "native/hevc/podman.py", "codec_wrapper")
    runtime = tmp_path / "lepton/images/rootfs"
    (runtime / "vendor/lib64").mkdir(parents=True)
    (runtime / "vendor/etc").mkdir()
    (runtime / "vendor/lib64/libstagefright_softomx.so").write_bytes(b"runtime ABI")
    xml = runtime / "vendor/etc/media_codecs.xml"
    xml.write_text('<MediaCodecs><Include href="stock.xml" /></MediaCodecs>')
    directory = tmp_path / "game/frameport-codec"
    directory.mkdir(parents=True)
    for name in ("libstagefrighthw.so", "media_codecs_frameport.xml"):
        (directory / name).write_bytes(b"codec asset")
    config = {"lepton": str(tmp_path / "lepton/lepton"), "appid": "123",
              "runtime_sha256": hashlib.sha256(b"runtime ABI").hexdigest()}
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda p: True if p.as_posix() == "/dev/video-dec0" else exists(p))
    args = ["run", "--name", "lepton-steamlaunch-123", "--rootfs", str(runtime) + ":O", "/init"]
    extra = wrapper.mounts(directory, config, args)
    assert [s.replace("\\", "/") for s in extra[:2]] == [
        "--mount", "type=bind,source=/dev/video-dec0,destination=/dev/video-dec0,rw"]
    merged = next(directory.glob("media_codecs.*.xml"))
    assert "media_codecs_frameport.xml" in merged.read_text()
    for spec in extra[1::2][1:]:
        fields = dict(item.split("=", 1) for item in spec.split(",") if "=" in item)
        assert Path(fields["source"]).is_file()
    assert "frameport" not in xml.read_text()  # shared runtime stays unchanged
    assert wrapper.mounts(directory, config, ["kill", "lepton-steamlaunch-123"]) == []
    assert wrapper.mounts(directory, config, [s.replace("123", "456") for s in args]) == []
    config["runtime_sha256"] = "wrong ABI"
    assert wrapper.mounts(directory, config, args) == []

    # One shared wrapper also accelerates arbitrary packages with unchanged
    # APKs. The launcher/container identity, not a recipe, selects its scope.
    app = tmp_path / "another-package/lepton-app"
    app.mkdir(parents=True)
    monkeypatch.setenv("SteamAppId", "123")
    monkeypatch.setenv("STEAM_COMPAT_INSTALL_PATH", str(app))
    shared = {"scope": "shared", "runtime_sha256": hashlib.sha256(b"runtime ABI").hexdigest()}
    assert wrapper.mounts(directory, shared, args)
    assert wrapper.mounts(directory, shared, [s.replace("123", "456") for s in args]) == []
    assert wrapper.mounts(directory, shared, [s.replace(":O", "") for s in args]) == []
    assert wrapper.mounts(directory, shared, ["kill", "lepton-steamlaunch-123"]) == []
    monkeypatch.delenv("SteamAppId")
    assert wrapper.mounts(directory, shared, args) == []


@pytest.mark.parametrize("configuration", [None, "{", "[]", "{}", '{"podman":null}',
                                          '{"podman":"missing-podman"}', "self"])
def test_broken_wrapper_configuration_executes_stock_podman(tmp_path, monkeypatch, configuration):
    wrapper = load_module(ROOT / "native/hevc/podman.py", "fallback_wrapper")
    directory = tmp_path / "codec"
    own = directory / "bin/podman"
    own.parent.mkdir(parents=True)
    own.write_text("wrapper")
    own.chmod(0o755)
    real = tmp_path / "system/podman"
    real.parent.mkdir()
    real.write_text("stock podman")
    real.chmod(0o755)
    monkeypatch.setattr(wrapper, "__file__", str(own))
    monkeypatch.setenv("PATH", str(own.parent) + os.pathsep + str(real.parent))
    # Avoid Windows' executable-extension rules: this launcher runs on Linux.
    monkeypatch.setattr(wrapper.shutil, "which", lambda _, path: str(Path(path) / "podman"))
    if configuration == "self":
        configuration = json.dumps({"podman": str(own)})
    if configuration is not None:
        (directory / "deployment.json").write_text(configuration)
    args = ["run", "--name", "lepton-steamlaunch-123", "/init"]
    monkeypatch.setattr(wrapper.sys, "argv", [str(own), *args])
    executed = []

    class ExecSucceeded(BaseException):
        pass

    def execute(path, argv):
        if Path(path) != real:
            raise FileNotFoundError(path)
        executed.append((path, argv))
        raise ExecSucceeded

    monkeypatch.setattr(wrapper.os, "execv", execute)
    with pytest.raises(ExecSucceeded):
        wrapper.main()
    assert executed == [(str(real), [str(real), *args])]


def test_failed_codec_mount_or_exec_preserves_original_arguments(tmp_path, monkeypatch):
    wrapper = load_module(ROOT / "native/hevc/podman.py", "mount_failure_wrapper")
    directory = tmp_path / "codec"
    directory.mkdir()
    own = directory / "bin/podman"
    monkeypatch.setattr(wrapper, "__file__", str(own))
    monkeypatch.setattr(wrapper, "real_podman", lambda _: "/usr/bin/podman")
    (directory / "deployment.json").write_text('{"podman":"/missing/podman"}')
    args = ["run", "--name", "lepton-steamlaunch-123", "/init"]
    monkeypatch.setattr(wrapper.sys, "argv", [str(own), *args])
    executed = []

    class ExecSucceeded(BaseException):
        pass

    def execute(path, argv):
        if path != "/usr/bin/podman":
            raise OSError("configured executable is unavailable")
        executed.append(argv)
        raise ExecSucceeded

    monkeypatch.setattr(wrapper.os, "execv", execute)
    monkeypatch.setattr(wrapper, "mounts", lambda *_: ["--mount", "private-codec"])
    with pytest.raises(ExecSucceeded):
        wrapper.main()
    assert executed == [["/usr/bin/podman", *args]]

    def malformed(*_):
        raise KeyError("runtime_sha256")

    monkeypatch.setattr(wrapper, "mounts", malformed)
    with pytest.raises(ExecSucceeded):
        wrapper.main()
    assert executed == [["/usr/bin/podman", *args]] * 2


def test_wrapper_finds_podman_under_the_android_path(tmp_path, monkeypatch):
    """Lepton runs `podman exec` with the Android guest's PATH (/system/bin:...): the wrapper still finds the host's
    Podman (it used to raise, which broke Lepton's logcat mirror and app-pid checks and stopped the container)."""
    wrapper = load_module(ROOT / "native/hevc/podman.py", "android_path_wrapper")
    monkeypatch.setattr(wrapper, "__file__", str(tmp_path / "codec/bin/podman"))
    monkeypatch.setenv("PATH", "/product/bin:/system/bin:/vendor/bin")
    monkeypatch.setattr(
        wrapper.shutil, "which",
        lambda _, path: path + "/podman" if Path(path).as_posix().endswith("/usr/bin") else None,
    )
    monkeypatch.setattr(wrapper.Path, "resolve", lambda self: self)
    assert Path(wrapper.real_podman(tmp_path / "codec")).as_posix().endswith("/usr/bin/podman")
