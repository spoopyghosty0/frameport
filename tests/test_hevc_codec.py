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
    (tmp_path / "run").mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    args = ["run", "--name", "lepton-steamlaunch-123", "--rootfs", str(runtime) + ":O", "/init"]
    extra = wrapper.mounts(directory, config, args)
    assert [s.replace("\\", "/") for s in extra[:2]] == [
        "--mount", "type=bind,source=/dev/video-dec0,destination=/dev/video-dec0,rw"]
    # the merged codec list goes to the runtime dir: the verified version directory is never written
    merged = next((tmp_path / "run/frameport-video").glob("media_codecs.*.xml"))
    assert "media_codecs_frameport.xml" in merged.read_text()
    assert sorted(p.name for p in directory.iterdir()) == ["libstagefrighthw.so", "media_codecs_frameport.xml"]
    for spec in extra[1::2][1:]:
        fields = dict(item.split("=", 1) for item in spec.split(",") if "=" in item)
        assert Path(fields["source"]).is_file()
    assert "frameport" not in xml.read_text()  # shared runtime stays unchanged
    assert wrapper.mounts(directory, config, ["kill", "lepton-steamlaunch-123"]) == []
    assert wrapper.mounts(directory, config, [s.replace("123", "456") for s in args]) == []
    config["runtime_sha256"] = "wrong ABI"
    assert wrapper.mounts(directory, config, args) == []

    # The shared wrapper serves any package with an unchanged APK: the launcher (frame.hw_video_decode in the game's
    # recipe) puts it on PATH, the container identity limits it to that game's container.
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


def test_codec_list_matches_the_plugin_table():
    """H.264, HEVC and VP9: the codec XML names exactly the components the plugin's table enumerates."""
    from xml.etree import ElementTree as ET

    xml = ET.parse(ROOT / "artifacts/hevc/media_codecs_frameport.xml").getroot()
    entries = {c.get("name"): c.get("type") for c in xml.iter("MediaCodec")}
    assert entries == {"OMX.frameport.avc.decoder": "video/avc", "OMX.frameport.hevc.decoder": "video/hevc",
                       "OMX.frameport.vp9.decoder": "video/x-vnd.on2.vp9"}
    for codec in xml.iter("MediaCodec"):
        limits = {limit.get("name"): limit for limit in codec.iter("Limit")}
        # VP9 at 7680x3840 never returned a picture on the Frame (2026-10-09): it stops at 4K
        expected = "4096x2304" if "vp9" in codec.get("name") else "8192x8192"
        assert limits["concurrent-instances"].get("max") == "1" and limits["size"].get("max") == expected
    source = (ROOT / "native/hevc/frameport_hevc.cpp").read_text()
    for name, mime in entries.items():
        assert f'"{name}"' in source and f'"{mime}"' in source
    assert "4096,2304}" in source  # the plugin's own VP9 limit (larger VP9 decodes in software)
    build = (ROOT / "native/hevc/build.py").read_text()
    for decoder in ("h264_v4l2m2m", "hevc_v4l2m2m", "vp9_v4l2m2m"):
        assert f'"{decoder}"' in source and decoder in build
    assert (ROOT / "artifacts/hevc/media_codecs_frameport.xml").read_bytes() == (
        ROOT / "native/hevc/media_codecs_frameport.xml").read_bytes()


@pytest.mark.parametrize("how", ["env", "flag"])
def test_switched_off_wrapper_runs_stock_podman_untouched(tmp_path, monkeypatch, how):
    """FRAMEPORT_NO_HW_VIDEO=1 or FramePort's setting (video-codec/disabled): the wrapper adds nothing."""
    wrapper = load_module(ROOT / "native/hevc/podman.py", f"off_wrapper_{how}")
    directory = tmp_path / "video-codec/versions/abc"
    own = directory / "bin/podman"
    own.parent.mkdir(parents=True)
    (directory / "deployment.json").write_text('{"scope": "shared", "runtime_sha256": "x"}')
    monkeypatch.setattr(wrapper, "__file__", str(own))
    monkeypatch.setattr(wrapper, "real_podman", lambda _: "/usr/bin/podman")
    monkeypatch.delenv("FRAMEPORT_NO_HW_VIDEO", raising=False)
    if how == "env":
        monkeypatch.setenv("FRAMEPORT_NO_HW_VIDEO", "1")
    else:
        (tmp_path / "video-codec/disabled").write_text("off")
    monkeypatch.setattr(wrapper, "mounts", lambda *_: pytest.fail("mounts while switched off"))
    args = ["run", "--name", "lepton-steamlaunch-123", "/init"]
    monkeypatch.setattr(wrapper.sys, "argv", [str(own), *args])
    executed = []

    class ExecSucceeded(BaseException):
        pass

    def execute(path, argv):
        executed.append(argv)
        raise ExecSucceeded

    monkeypatch.setattr(wrapper.os, "execv", execute)
    with pytest.raises(ExecSucceeded):
        wrapper.main()
    assert executed == [["/usr/bin/podman", *args]]
    monkeypatch.setenv("FRAMEPORT_NO_HW_VIDEO", "0")
    (tmp_path / "video-codec/disabled").unlink(missing_ok=True)
    assert not wrapper.switched_off(directory)


def test_empty_capture_buffers_are_requeued_not_taken_for_the_end():
    """Iris returns an empty capture buffer (no LAST flag) for each hidden VP9 frame. FFmpeg's wrapper ended the EOS
    drain at the first one and lost the pictures still in the driver (two-pass VP9 4K: 573 of 600, 2026-10-10)."""
    build = load_module(ROOT / "native/hevc/build.py", "hevc_build")
    stand_in = ("static V4L2Buffer* v4l2_dequeue_v4l2buf(V4L2Context *ctx, int timeout)\n{\n    struct pollfd pfd = {\n"
                + build.EMPTY_DECLARE + "start:\n" + build.EMPTY_POLL + "dequeue:\n"
                + build.EMPTY_DRAIN + " = 0;\n        }\n}\n")
    patched = build.skip_empty_pictures(stand_in)
    assert "int i, ret, skipped_empty = 0;" in patched
    # empty buffers are skipped before the drain's "empty buffer = end" check, unless LAST/ERROR is set
    assert patched.index("ff_v4l2_buffer_enqueue(&ctx->buffers[buf.index])") < patched.index(build.EMPTY_DRAIN)
    assert "!(buf.flags & (V4L2_BUF_FLAG_LAST | V4L2_BUF_FLAG_ERROR))" in patched
    # a drain can't block forever after a skipped empty buffer
    assert "if (!ret && skipped_empty && ctx_to_m2mctx(ctx)->draining) {" in patched
    with pytest.raises(RuntimeError):
        build.skip_empty_pictures(stand_in.replace(build.EMPTY_POLL, ""))
    # the component no longer guesses which pictures belong to hidden frames
    assert "vp9Hidden" not in (ROOT / "native/hevc/frameport_hevc.cpp").read_text()
