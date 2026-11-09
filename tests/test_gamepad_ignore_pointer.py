"""device.steam_gamepad's "Ignore the laser pointer" (GitHub #162): SteamVR's laser reaches a 2D app as Waydroid's
wayland_pointer and SDL games flip between gamepad and mouse prompts. The option binds /dev/null at the pointer pipe's
path: the hwcomposer writes into it, Waydroid's EventHub can't poll it and drops the device."""
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
def test_wrapper_binds_dev_null_at_the_pointer_pipe(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    frame_inputs(tmp_path)
    sys_root, dev_root = str(tmp_path / "sys"), str(tmp_path / "dev")
    plain = agent.podman_run_args(RUN, ENV, sys_root, dev_root)
    assert not any("wl_pointer_events" in a for a in plain)
    env = dict(ENV, FRAMEPORT_GAMEPAD_IGNORE_POINTER="1")
    out = agent.podman_run_args(RUN, env, sys_root, dev_root)
    mount = f"type=bind,source={dev_root}/null,destination=/dev/input/wl_pointer_events"
    assert out[out.index(mount) - 1] == "--mount" and out[len(out) - len(RUN) + 1:] == RUN[1:]
    assert out.count(mount) == 1 and not any("excluded-input-devices" in a for a in out)
    # a destination already mounted isn't mounted twice
    taken = ["run", "--mount", "type=bind,source=/x,destination=/dev/input/wl_pointer_events", *RUN[1:]]
    assert " ".join(agent.podman_run_args(taken, env, sys_root, dev_root)).count("wl_pointer_events") == 1
    # only with the gamepad: the kill switch drops it too
    assert agent.podman_run_args(RUN, dict(env, FRAMEPORT_NO_GAMEPAD="1"), sys_root, dev_root) == RUN
    assert agent.podman_run_args(RUN, dict(env, FRAMEPORT_GAMEPAD_IGNORE_POINTER="0"), sys_root, dev_root) == plain


def test_production_source_is_the_hosts_dev_null(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    monkeypatch.setattr(agent, "steam_gamepads", lambda *a: [])
    out = agent.gamepad_mounts({"FRAMEPORT_GAMEPAD_IGNORE_POINTER": "1"}, {agent.PAD_DIR})
    assert out == ["--mount", "type=bind,source=/dev/null,destination=/dev/input/wl_pointer_events"]
