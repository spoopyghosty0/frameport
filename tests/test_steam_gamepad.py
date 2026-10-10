"""device.steam_gamepad (GitHub #162): Steam Input's virtual gamepad passed into a 2D app's Lepton container by
FramePort's Podman wrapper, chosen per game; works with or without the hardware video codec wrapper."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from frameport.core.models import Analysis
from frameport.patches import base

ROOT = Path(__file__).resolve().parents[1]
NEEDS_POSIX = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="Linux paths and bash")


def agent_module(monkeypatch, tmp_path):
    if sys.platform == "win32":
        monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace(flock=lambda *_: None, LOCK_EX=2))
    spec = importlib.util.spec_from_file_location("gamepad_agent", ROOT / "agent/frameport_agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    monkeypatch.setattr(agent, "VIDEO_CODEC_DIR", str(tmp_path / "codec"))
    monkeypatch.setattr(agent, "ANCHORS", str(tmp_path / "apps"))
    monkeypatch.setattr(agent, "PODMAN_BIN", str(tmp_path / "agent/bin"))
    return agent


# capabilities/key of an Xbox 360 pad (BTN_SOUTH..BTN_THUMBR set) and of steamos-manager's keys (no gamepad buttons),
# as the Frame's kernel prints them (64-bit words, most significant first, not zero-padded)
PAD_KEYS = "7cdb000000000000 0 0 0 0"
MANAGER_KEYS = "568000000000 4040860000000"


def fake_input(root, number, vendor, product, name, keys, virtual=True):
    """sysfs (class/input/eventN -> devices/.../inputN/eventN, device -> ..) and its /dev/input node."""
    parent = "devices/virtual/input" if virtual else "devices/platform/soc/usb1/input"
    device = root / "sys" / parent / f"input{number}"
    event = device / f"event{number}"
    (device / "id").mkdir(parents=True)
    (device / "capabilities").mkdir()
    event.mkdir()
    (event / "device").symlink_to("..")
    (device / "id/vendor").write_text(vendor + "\n")
    (device / "id/product").write_text(product + "\n")
    (device / "name").write_text(name + "\n")
    (device / "capabilities/key").write_text(keys + "\n")
    (root / "sys/class/input").mkdir(parents=True, exist_ok=True)
    (root / "sys/class/input" / f"event{number}").symlink_to(event)
    (root / "dev/input").mkdir(parents=True, exist_ok=True)
    (root / "dev/input" / f"event{number}").write_text("")


def frame_inputs(root):
    fake_input(root, 4, "28de", "0000", "steamos-manager", MANAGER_KEYS)
    fake_input(root, 5, "28de", "11ff", "Microsoft X-Box 360 pad 0", PAD_KEYS)
    fake_input(root, 6, "28de", "1205", "Steam Deck", PAD_KEYS, virtual=False)
    fake_input(root, 7, "045e", "028e", "Microsoft X-Box 360 pad", PAD_KEYS)


RUN = ["run", "--rm", "--name", "lepton-steamlaunch-123", "--rootfs", "/lepton/rootfs:O", "/init"]
ENV = {"FRAMEPORT_GAMEPAD": "1", "SteamAppId": "123"}


@NEEDS_POSIX
def test_only_steam_input_virtual_pads_are_found(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    frame_inputs(tmp_path)
    pads = agent.steam_gamepads(str(tmp_path / "sys"), str(tmp_path / "dev"))
    assert pads == [{"event": "event5", "product": "11ff", "name": "Microsoft X-Box 360 pad 0"}]
    assert agent.has_key_bit(PAD_KEYS, agent.BTN_SOUTH) and not agent.has_key_bit(MANAGER_KEYS, agent.BTN_SOUTH)
    assert not agent.has_key_bit("", agent.BTN_SOUTH) and not agent.has_key_bit("zz 0 0 0 0", agent.BTN_SOUTH)


@NEEDS_POSIX
def test_run_args_get_the_pads_and_their_key_layout(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    frame_inputs(tmp_path)
    fake_input(tmp_path, 8, "28de", "11ff", "Microsoft X-Box 360 pad 1", PAD_KEYS)
    sys_root, dev_root = str(tmp_path / "sys"), str(tmp_path / "dev")
    out = agent.podman_run_args(RUN, ENV, sys_root, dev_root)
    layout = tmp_path / "agent/steam-gamepad.kl"
    assert out == ["run",
                   "--mount", f"type=bind,source={dev_root}/input/event5,destination=/dev/input/event5,rw",
                   "--mount", f"type=bind,source={dev_root}/input/event8,destination=/dev/input/event8,rw",
                   "--mount", f"type=bind,source={layout},destination=/system/usr/keylayout/"
                              "Vendor_28de_Product_11ff.kl,ro",
                   *RUN[1:]]
    assert layout.read_text() == agent.GAMEPAD_KL
    assert "key 304   BUTTON_A" in agent.GAMEPAD_KL and "axis 0x05 RTRIGGER" in agent.GAMEPAD_KL
    # a destination Lepton mounts itself isn't mounted twice
    taken = ["run", "--mount", "type=bind,source=/x,destination=/dev/input/event5,rw", *RUN[1:]]
    out = " ".join(agent.podman_run_args(taken, ENV, sys_root, dev_root))
    assert out.count("destination=/dev/input/event5,") == 1 and "destination=/dev/input/event8," in out


@NEEDS_POSIX
@pytest.mark.parametrize("args,env", [
    (RUN, {"SteamAppId": "123"}),  # the launcher didn't ask for it
    (RUN, dict(ENV, FRAMEPORT_NO_GAMEPAD="1")),  # kill switch
    (RUN, dict(ENV, SteamAppId="999")),  # another game's container
    (["run", "--name=lepton-other", "/init"], ENV),
    (["exec", "lepton-steamlaunch-123", "logcat"], ENV),  # only `run`
])
def test_run_args_unchanged(monkeypatch, tmp_path, args, env):
    agent = agent_module(monkeypatch, tmp_path)
    frame_inputs(tmp_path)
    assert agent.podman_run_args(args, env, str(tmp_path / "sys"), str(tmp_path / "dev")) == args


@NEEDS_POSIX
def test_no_pads_no_mounts(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    fake_input(tmp_path, 4, "28de", "0000", "steamos-manager", MANAGER_KEYS)
    assert agent.podman_run_args(RUN, ENV, str(tmp_path / "sys"), str(tmp_path / "dev")) == RUN
    assert not (tmp_path / "agent/steam-gamepad.kl").exists()


def fake_podman(folder, label):
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "podman"
    script.write_text(f'#!/bin/sh\necho "{label} PATH=$PATH ARGS=$*"\n')
    script.chmod(0o755)


def wrapper(agent, tmp_path):
    assert agent.ensure_podman_wrapper() is True
    assert agent.ensure_podman_wrapper() is False  # unchanged
    path = tmp_path / "agent/bin/podman"
    assert os.access(path, os.X_OK)
    return path


@NEEDS_POSIX
def test_wrapper_hands_on_to_the_codec_wrapper_and_leaves_path(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    ours = wrapper(agent, tmp_path)
    codec, real = tmp_path / "codec/current/bin", tmp_path / "usr/bin"
    fake_podman(codec, "codec")
    fake_podman(real, "real")
    path = f"{ours.parent}:{codec}:{real}:/bin"
    out = subprocess.run([str(ours), "exec", "x", "logcat"], env={"PATH": path}, capture_output=True, text=True,
                         check=True).stdout
    assert out == f"codec PATH={codec}:{real}:/bin ARGS=exec x logcat\n"
    # the other order (codec wrapper first: it calls the next Podman it finds, this wrapper): no loop back
    path = f"{codec}:{ours.parent}:{real}:/bin"
    out = subprocess.run([str(ours), "ps"], env={"PATH": path}, capture_output=True, text=True, check=True).stdout
    assert out == f"real PATH={codec}:{real}:/bin ARGS=ps\n"


@NEEDS_POSIX
def test_wrapper_run_goes_through_the_agent_and_never_fails_the_start(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    ours = wrapper(agent, tmp_path)
    real = tmp_path / "usr/bin"
    fake_podman(real, "real")
    env = dict(ENV, PATH=f"{ours.parent}:{real}:/bin")
    # no agent next to the wrapper (or a broken one): arguments unchanged
    done = subprocess.run([str(ours), *RUN], env=env, capture_output=True, text=True, check=True)
    assert done.stdout == f"real PATH={real}:/bin ARGS={' '.join(RUN)}\n"
    assert "left the arguments unchanged" in done.stderr
    # with the agent: podman_run_args runs (this machine's /sys has no Steam pads to add)
    shutil.copy(ROOT / "agent/frameport_agent.py", tmp_path / "agent/frameport_agent.py")
    done = subprocess.run([str(ours), *RUN], env=env, capture_output=True, text=True, check=True)
    assert done.stdout.startswith(f"real PATH={real}:/bin ARGS=run ")
    assert "FramePort gamepad: Steam Input virtual gamepads for this container" in done.stderr


LEPTON = "export LEPTON_ENV_FRAMEBRIDGE_CONFIG=\"$app_dir/settings.conf\"\n{lines}child=''\nsetsid /lepton start\n"


def anchor(tmp_path, name, launcher, **dep):
    directory = tmp_path / "apps" / name
    directory.mkdir(parents=True)
    (directory / "deployment.json").write_text(json.dumps({"package": name, **dep}))
    (directory / "launch.sh").write_text(launcher)
    return directory


COMBOS = [(False, False), (True, False), (False, True), (True, True)]


@pytest.mark.parametrize("hw_video,gamepad", COMBOS)
def test_new_launchers_get_the_wrapper_lines(monkeypatch, tmp_path, hw_video, gamepad):
    agent = agent_module(monkeypatch, tmp_path)
    directory = tmp_path / "apps/game"
    directory.mkdir(parents=True)
    agent.write_launcher(str(directory), str(tmp_path / "base"), "com.example.game", "Game", 123, "/lepton/lepton", {},
                         hw_video=hw_video, gamepad=gamepad)
    text = (directory / "launch.sh").read_text()
    assert (agent.VIDEO_CODEC_LINE in text) is hw_video and (agent.GAMEPAD_LINE in text) is gamepad
    if hw_video and gamepad:  # FramePort's wrapper first on PATH: its line comes last
        assert text.index(agent.VIDEO_CODEC_LINE) < text.index(agent.GAMEPAD_LINE)
    assert agent.set_gamepad_line(agent.set_codec_line(text, hw_video), gamepad) == text  # upgrades keep it


@pytest.mark.parametrize("hw_video,gamepad", COMBOS)
def test_upgrade_launchers_follow_the_deployment(monkeypatch, tmp_path, hw_video, gamepad):
    agent = agent_module(monkeypatch, tmp_path)
    plain = LEPTON.format(lines="")
    both = LEPTON.format(lines=agent.VIDEO_CODEC_LINE + "\n" + agent.GAMEPAD_LINE + "\n")
    # one launcher from nothing, one with both lines (the other way round: a reinstall without them)
    new = anchor(tmp_path, "com.new.game", plain, hw_video_decode=hw_video, steam_gamepad=gamepad)
    old = anchor(tmp_path, "com.old.game", both, hw_video_decode=hw_video, steam_gamepad=gamepad)
    agent.upgrade_launchers()
    for directory in (new, old):
        text = (directory / "launch.sh").read_text()
        assert text.count(agent.VIDEO_CODEC_LINE) == int(hw_video), directory.name
        assert text.count(agent.GAMEPAD_LINE) == int(gamepad), directory.name
        if hw_video and gamepad:
            assert text.index(agent.VIDEO_CODEC_LINE) < text.index(agent.GAMEPAD_LINE)
        assert text.endswith("child=''\nsetsid /lepton start\n")
    assert agent.upgrade_launchers() == []  # idempotent


def test_codec_added_later_still_comes_before_the_gamepad_line(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    directory = anchor(tmp_path, "com.game", LEPTON.format(lines=agent.GAMEPAD_LINE + "\n"),
                       recipe={"patches": ["device.steam_gamepad", "frame.hw_video_decode"]})
    assert agent.upgrade_launchers() == ["com.game"]
    text = (directory / "launch.sh").read_text()
    assert text.index(agent.VIDEO_CODEC_LINE) < text.index(agent.GAMEPAD_LINE)


def test_finalize_records_the_choice(monkeypatch, tmp_path):
    """wants_gamepad: deployment.json's steam_gamepad (finalize), else the recipe."""
    agent = agent_module(monkeypatch, tmp_path)
    assert agent.wants_gamepad(str(anchor(tmp_path, "a.b", "", recipe={"patches": ["device.steam_gamepad"]})))
    assert not agent.wants_gamepad(str(anchor(tmp_path, "c.d", "", steam_gamepad=False,
                                              recipe={"patches": ["device.steam_gamepad"]})))
    assert not agent.wants_gamepad(str(tmp_path / "apps/missing"))
    source = (ROOT / "agent/frameport_agent.py").read_text()
    assert 'GAMEPAD_PATCH = "device.steam_gamepad"' in source and '"steam_gamepad": gamepad' in source


@NEEDS_POSIX
@pytest.mark.parametrize("case", ["on", "off", "off0", "missing"])
def test_gamepad_line(tmp_path, case):
    """The launcher line: SDL's hint for the app and FramePort's wrapper first on PATH, unless FRAMEPORT_NO_GAMEPAD=1
    (the hint alone when the wrapper isn't there)."""
    spec = importlib.util.spec_from_file_location("gamepad_agent_line", ROOT / "agent/frameport_agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    home = tmp_path / "home"
    if case != "missing":
        fake_podman(home / ".local/share/frameport/agent/bin", "ours")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    if case.startswith("off"):
        env["FRAMEPORT_NO_GAMEPAD"] = "0" if case == "off0" else "1"
    script = agent.GAMEPAD_LINE + '\necho "$PATH|${FRAMEPORT_GAMEPAD:-}|${LEPTON_ENV_SDL_GAMECONTROLLER_ALLOW_STEAM_' \
                                  'VIRTUAL_GAMEPAD:-}"'
    out = subprocess.run(["bash", "-euc", script], env=env, capture_output=True, text=True, check=True).stdout.strip()
    path, flag, hint = out.split("|")
    on = case in ("on", "off0")
    assert path.startswith(str(home / ".local/share/frameport/agent/bin") + ":") is on
    assert (flag, hint) == (("1", "1") if case != "off" else ("", ""))


def analysis(vr_kind="none", **extra):
    return Analysis("com.example", "1", "Example", ["arm64-v8a"], "Other", "?", "GLES or unknown", False, [], None,
                    False, [], False, 0, False, False, False, extra={"vr_kind": vr_kind, **extra})


def test_patch_applies_to_2d_apps_and_is_suggested_for_gamepad_ones():
    patch = base.get("device.steam_gamepad")
    assert patch.stage == "install" and patch.category == "device" and not patch.needs_vr
    assert patch.applies(analysis()) and not patch.applies(analysis("quest"))
    assert patch.detect(analysis()) is None  # applicable, not suggested
    assert patch.detect(analysis(gamepad=True)).recommended
    assert patch.detect(analysis("quest", gamepad=True)) is None
    assert patch.summary and patch.title
    ctx = base.InstallContext("com.example", {}, {}, {}, {})
    patch.install(ctx)
    assert ctx.env == {} and not ctx.flatscreen  # the agent does it all (launcher line + wrapper)


def test_manifest_gamepad_detection():
    from frameport.analysis import detect

    assert detect.ANALYSIS_VERSION >= 9
    source = (ROOT / "src/frameport/analysis/detect.py").read_text()
    assert '"gamepad": "android.hardware.gamepad" in features or axml.LEANBACK_LAUNCHER in cats' in source
