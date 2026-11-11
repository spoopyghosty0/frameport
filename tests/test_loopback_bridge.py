"""device.loopback_bridge: app-local servers bound to the container's network address get a localhost forward
(Plex's DownloadsServer on the Wi-Fi IP while its player loads http://localhost:<port>/...)."""
from types import SimpleNamespace

from test_device_lang_offline_folder import _env
from test_steam_gamepad import LEPTON, agent_module

from frameport.patches import base

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
# seen in Plex's container on a Frame: adb on [::]:5555 (wildcard) and DownloadsServer on 192.0.2.10:33003
TCP6 = HEADER + (
    "   0: 00000000000000000000000000000000:15B3 00000000000000000000000000000000:0000 0A 00000000:00000000 "
    "00:00000000 00000000     0        0 406526 1 0000000098fbe7f0 100 0 0 10 0\n"
    "   1: 0000000000000000FFFF00000A0200C0:80EB 00000000000000000000000000000000:0000 0A 00000000:00000000 "
    "00:00000000 00000000     0        0 487902 1 00000000a1f27ac3 100 0 0 10 0\n")
TCP = HEADER + (
    "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 1 0 100 0 0 10 0\n"
    "   1: 0A0200C0:A1B2 016433C6:01BB 01 00000000:00000000 00:00000000 00000000     0        0 2 1 0 100 0 0 10 0\n"
    "   2: 0A0200C0:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 3 1 0 100 0 0 10 0\n")


def test_patch_sets_its_variable():
    assert _env({"device.loopback_bridge": {}})["FRAMEPORT_LOOPBACK"] == "1"
    patch = base.get("device.loopback_bridge")
    assert patch.category == "device" and patch.stage == "install" and not patch.needs_vr


def test_targets_are_listeners_on_the_network_address_only(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    # wildcard adb, loopback 8080 and an established connection are left out
    assert agent.bridge_targets([TCP, TCP6]) == {33003: "192.0.2.10", 80: "192.0.2.10"}
    assert agent.bridge_targets([HEADER]) == {}


def test_launcher_line_comes_and_goes(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    launcher = LEPTON.format(lines="")
    on = agent.set_loopback_line(launcher, True)
    assert on.count(" _loopback_bridge ") == 1 and on.index("_loopback_bridge") < on.index("/lepton start")
    assert agent.set_loopback_line(on, True) == on
    assert agent.set_loopback_line(on, False) == launcher


class Runner:
    """podman inspect / exec calls answered from a script of (container pid, tcp tables)."""

    def __init__(self, script):
        self.script, self.step, self.calls = script, 0, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        pid, tables = self.script[min(self.step, len(self.script) - 1)]
        if cmd[:2] == ["podman", "inspect"]:
            return SimpleNamespace(returncode=0 if pid else 125, stdout=f"{pid}\n" if pid else "", stderr="")
        if cmd[:3] == ["podman", "exec", cmd[2]] and cmd[3:4] == ["cat"]:
            return SimpleNamespace(returncode=0, stdout="".join(tables), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def run_bridge(agent, runner, rounds, monkeypatch):
    monkeypatch.setattr(agent, "container_tables", lambda c, pid, r=None: [
        "".join(runner.script[min(runner.step, len(runner.script) - 1)][1])])
    left = [rounds]

    def alive(_):
        left[0] -= 1
        return left[0] >= 0

    def sleep(_):
        runner.step += 1
    agent.loopback_bridge("123", "1", runner=runner, alive=alive, sleep=sleep)


def test_bridge_starts_nc_inside_the_container_and_follows_the_port(monkeypatch, tmp_path, capsys):
    agent = agent_module(monkeypatch, tmp_path)
    moved = TCP6.replace(":80EB", ":A7B5")  # Plex restarted on another port (42933)
    runner = Runner([(0, [HEADER]), (4242, [TCP6]), (4242, [TCP6]), (4242, [moved])])
    run_bridge(agent, runner, 4, monkeypatch)
    starts = [c for c in runner.calls if c[:3] == ["podman", "exec", "-d"]]
    assert starts[0] == ["podman", "exec", "-d", "lepton-steamlaunch-123", "/system/bin/nc", "-s", "127.0.0.1", "-p",
                         "33003", "-L", "/system/bin/nc", "192.0.2.10", "33003"]
    assert len(starts) == 2 and starts[1][8] == "42933"  # not started twice for the same server
    stops = [c for c in runner.calls if "pkill" in c]
    assert stops == [["podman", "exec", "lepton-steamlaunch-123", "pkill", "-f", "nc -s 127.0.0.1 -p 33003 -L"]]
    out = capsys.readouterr().out
    assert "localhost:33003 -> 192.0.2.10:33003" in out and "bridge stopped" in out


def test_triage_suggests_the_bridge_for_a_refused_localhost_server():
    from frameport.validate.triage import triage

    log = ("11-11 21:17:21.279   215   243 I ActivityManager: Start proc 1193:com.plexapp.android/u0a14 for top-"
           "activity {com.plexapp.android/tv.plex.app.MainActivity}\n"
           "11-11 21:17:36.521  1193  1301 W System.err: Caused by: java.net.ConnectException: Failed to connect to "
           "localhost/::1:41653\n")
    r = triage(log, "RUNNING", "com.plexapp.android", applied=set())
    assert [f.suggest for f in r.findings if f.id == "app-server-off-localhost"] == [["device.loopback_bridge"]]
