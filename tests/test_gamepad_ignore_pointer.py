"""device.steam_gamepad's "Ignore the laser pointer" (GitHub #162): SteamVR's laser reaches a 2D app as Waydroid's
wayland_pointer and SDL games flip between gamepad and mouse prompts. The option mounts an excluded-input-devices file
for Android's input reader and keeps the pointer pipe read inside the container."""
import types

from test_steam_gamepad import ENV, NEEDS_POSIX, RUN, agent_module, frame_inputs

from frameport.patches import base
from frameport.patches.base import InstallContext


def install_env(**params):
    ctx = InstallContext(package="org.example.sdlgame", params=params, files={}, env={}, adapter_settings={})
    base.get("device.steam_gamepad").install(ctx)
    return ctx.env


def test_option_is_off_by_default_and_becomes_launcher_env():
    patch = base.get("device.steam_gamepad")
    param = next(q for q in patch.params if q.key == "ignore_pointer")
    assert param.kind == "bool" and param.default is False
    assert "no longer clicks" in patch.description and "no longer clicks" in param.help
    assert install_env() == {}
    assert install_env(ignore_pointer=True) == {"FRAMEPORT_GAMEPAD_IGNORE_POINTER": "1"}


@NEEDS_POSIX
def test_wrapper_mounts_the_excluded_devices_file(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    frame_inputs(tmp_path)
    sys_root, dev_root = str(tmp_path / "sys"), str(tmp_path / "dev")
    plain = agent.podman_run_args(RUN, ENV, sys_root, dev_root)
    assert not any("excluded-input-devices" in a for a in plain)
    env = dict(ENV, FRAMEPORT_GAMEPAD_IGNORE_POINTER="1")
    out = agent.podman_run_args(RUN, env, sys_root, dev_root)
    source = tmp_path / "agent/excluded-input-devices.xml"
    mount = f"type=bind,source={source},destination=/system/etc/excluded-input-devices.xml,ro"
    assert out[out.index(mount) - 1] == "--mount" and out[len(out) - len(RUN) + 1:] == RUN[1:]
    text = source.read_text()
    assert text == agent.EXCLUDED_DEVICES
    assert "<devices>" in text and '<device name="wayland_pointer" />' in text and text.count("<device ") == 1
    # a destination already mounted isn't mounted twice
    taken = ["run", "--mount", "type=bind,source=/x,destination=/system/etc/excluded-input-devices.xml,ro", *RUN[1:]]
    assert " ".join(agent.podman_run_args(taken, env, sys_root, dev_root)).count("excluded-input-devices.xml,") == 1
    # only with the gamepad: the kill switch drops it too
    assert agent.podman_run_args(RUN, dict(env, FRAMEPORT_NO_GAMEPAD="1"), sys_root, dev_root) == RUN


def test_drain_reads_the_pipe_without_blocking_or_end_of_file(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    assert agent.POINTER_DRAIN == ("[ -e /dev/input/wl_pointer_events ] && "
                                   "exec cat 0<>/dev/input/wl_pointer_events >/dev/null")


class Reader:
    """A Popen stand-in: ends after `ends_after` polls (None = runs until terminated)."""

    def __init__(self, ends_after):
        self.left, self.terminated = ends_after, False

    def poll(self):
        if self.terminated:
            return -15
        if self.left is None:
            return None
        self.left -= 1
        return None if self.left > 0 else 1

    def terminate(self):
        self.terminated = True


def test_link_worker_keeps_the_pointer_reader_running(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    clock = iter(range(100))
    monkeypatch.setattr(agent.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    readers = [Reader(1), Reader(None)]  # the first ends at once (container not up yet)
    started = []

    def drain():
        started.append(readers[len(started)])
        return started[-1]

    left = iter([True] * 6 + [False])
    agent.gamepad_links("123", 1, exec_=lambda s: types.SimpleNamespace(returncode=0), pads=lambda: [],
                        alive=lambda pid: next(left), drain=drain, retry=2.0)
    assert started == readers  # started again after `retry`, then kept
    assert readers[1].terminated  # ended with the worker


def test_link_worker_without_the_option_starts_no_reader(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    monkeypatch.delenv("FRAMEPORT_GAMEPAD_IGNORE_POINTER", raising=False)
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)

    def popen(*a, **kw):
        raise AssertionError("no reader without the option")
    monkeypatch.setattr(agent.subprocess, "Popen", popen)
    left = iter([True, True, False])
    agent.gamepad_links("123", 1, exec_=lambda s: types.SimpleNamespace(returncode=0), pads=lambda: [],
                        alive=lambda pid: next(left))


def test_link_worker_reads_the_option_from_the_launcher_env(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    monkeypatch.setenv("FRAMEPORT_GAMEPAD_IGNORE_POINTER", "1")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    calls = []

    def popen(cmd, **kw):
        calls.append(cmd)
        return Reader(None)
    monkeypatch.setattr(agent.subprocess, "Popen", popen)
    left = iter([True, True, False])
    agent.gamepad_links("123", 1, exec_=lambda s: types.SimpleNamespace(returncode=0), pads=lambda: [],
                        alive=lambda pid: next(left))
    assert calls == [["podman", "exec", "lepton-steamlaunch-123", "sh", "-c", agent.POINTER_DRAIN]]
