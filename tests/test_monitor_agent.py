"""The agent's Monitor stream (`_monitor`): readers against a fake /proc + /sys tree, process grouping, kill safety."""
import io
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from test_agent import AGENT, load_agent

GAME = "com.example.game"
APPID = 2541636856


def w(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def proc(root, pid, comm, ppid, cpu=0, start=100, rss=1000, cmdline=None, fds=None, cgroup=None):
    d = root / "proc" / str(pid)
    stat = f"{pid} ({comm}) S {ppid} 1 1 0 -1 0 0 0 0 0 {cpu} 0 0 0 20 0 1 0 {start} 100000 {rss} 0"
    w(d / "stat", stat)
    w(d / "cmdline", "\0".join(cmdline or [comm]) + "\0")
    w(d / "status", f"Name:\t{comm}\nState:\tS (sleeping)\n")
    if cgroup:
        w(d / "cgroup", f"0::{cgroup}\n")
    (d / "fd").mkdir(parents=True, exist_ok=True)
    for fd, (target, ns) in (fds or {}).items():
        os.symlink(target, d / "fd" / str(fd))
        w(d / "fdinfo" / str(fd), f"pos:\t0\ndrm-driver:\tmsm\ndrm-engine-gpu:\t{ns} ns\ndrm-cycles-gpu:\t0\n")


def set_cpu(root, pid, cpu):
    p = root / "proc" / str(pid) / "stat"
    f = p.read_text().split(" ")
    f[13] = str(cpu)  # utime (field 14)
    p.write_text(" ".join(f))


def set_gpu(root, pid, fd, ns):
    w(root / "proc" / str(pid) / "fdinfo" / str(fd), f"drm-driver:\tmsm\ndrm-engine-gpu:\t{ns} ns\n")


def frame_tree(root, home):
    """A small Frame: 2 clusters, GPU devfreq, thermal zones, power rails, fan, battery, one running Quest game."""
    p = root / "proc"
    w(p / "stat", "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0 0 0\n"
                  "cpu1 50 0 50 400 0 0 0 0 0 0\nintr 1\nbtime 1000\n")
    w(p / "meminfo", "MemTotal:       16000000 kB\nMemFree:  1 kB\nMemAvailable:    8000000 kB\n"
                     "SwapTotal:       8000000 kB\nSwapFree:        7000000 kB\n")
    for k in ("cpu", "memory", "io"):
        w(p / "pressure" / k, "some avg10=2.50 avg60=1.00 avg300=0.00 total=1\n"
                              "full avg10=0.00 avg60=0 avg300=0 total=0\n")
    w(p / "net" / "dev", "Inter-|   Receive\n face |bytes\n    lo: 5 0 0 0 0 0 0 0 5 0 0 0 0 0 0 0\n"
                         "  wlan0: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\n")
    s = root / "sys"
    for i, (cpus, mx, cur) in enumerate((("0", 2265600, 1248000), ("1", 3148800, 1708800))):
        d = s / "devices/system/cpu/cpufreq" / f"policy{i}"
        w(d / "related_cpus", cpus + "\n")
        w(d / "cpuinfo_max_freq", f"{mx}\n")
        w(d / "scaling_cur_freq", f"{cur}\n")
    g = s / "class/devfreq/3d00000.gpu"
    w(g / "name", "3d00000.gpu\n")
    w(g / "cur_freq", "231000000\n")
    w(g / "max_freq", "903000000\n")
    w(s / "class/devfreq/1d84000.ufshc/name", "1d84000.ufshc\n")
    for i, (kind, mc) in enumerate((("cpu0-thermal", 41000), ("cpu7-top-thermal", 52500), ("gpuss-0-thermal", 47000),
                                    ("max1720x_bat_7-36", 23200), ("aoss0-thermal", 30000))):
        w(s / f"class/thermal/thermal_zone{i}/type", kind + "\n")
        w(s / f"class/thermal/thermal_zone{i}/temp", f"{mc}\n")
    h = s / "class/hwmon/hwmon51"
    for i, (label, uw) in enumerate((("apc0", 500000), ("apc1", 250000), ("gfx", 1500000), ("vph", 6000000),
                                     ("unused", 8), ("bob", 160000)), 1):
        w(h / f"power{i}_label", label + "\n")
        w(h / f"power{i}_input", f"{uw}\n")
    w(s / "class/hwmon/hwmon50/fan1_input", "8200\n")
    b = root / "power_supply/max1720x_bat_7-36"
    for k, v in {"type": "Battery", "capacity": "80", "status": "Discharging", "current_now": "-500000",
                 "voltage_now": "8000000", "time_to_empty_now": "7200", "cycle_count": "7", "temp": "233",
                 "health": "Good", "charge_full": "2698000", "charge_full_design": "2730000"}.items():
        w(b / k, v + "\n")
    w(root / "power_supply/usb/type", "USB\n")
    w(root / "power_supply/usb/online", "0\n")
    cg = s / "fs/cgroup/user.slice/libpod-abc.scope"
    w(cg / "memory.current", "2000000000\n")
    w(cg / "cpu.stat", "usage_usec 1000000\n")
    w(s / "fs/cgroup/user.slice/app.slice/memory.current", "1\n")
    # a Quest game: launch.sh (Steam's reaper) + the podman container (conmon → Android init → the game)
    base = home / "Applications/quest-frame" / GAME
    w(base / "deployment.json", json.dumps({"package": GAME, "appid": APPID, "title": "Example Game",
                                            "kind": "quest"}))
    w(base / "launch.log", "x\n10-07 02:00:14.554  1183  1212 I FrameBridge: pacing: 71.9 fps, displayTime vs "
                           "predicted: avg 0.10 ms, max 0.00 ms\n")
    proc(root, 1, "systemd", 0)
    proc(root, 2, "kthreadd", 0)
    proc(root, 3, "kworker/0:0", 2)
    proc(root, 100, "steam", 1, cpu=10, fds={5: ("/dev/dri/renderD128", 0)})
    proc(root, 101, "vrcompositor", 1, fds={7: ("/dev/dri/renderD128", 1000), 9: ("/dev/dri/renderD128", 0)})
    launcher = str(home / "Applications/quest-frame" / GAME / "launch.sh")
    proc(root, 200, "launch.sh", 100, cmdline=["/bin/bash", launcher])
    proc(root, 300, "conmon", 1, cmdline=["/usr/bin/conmon", "-n", f"lepton-steamlaunch-{APPID}"],
         cgroup="/user.slice/app.slice")
    proc(root, 301, "init", 300, cgroup="/user.slice/libpod-abc.scope/container")
    proc(root, 302, "com.example.gam", 301, cpu=50, fds={3: ("/dev/kgsl-3d0", 5000)},
         cgroup="/user.slice/libpod-abc.scope/container")
    proc(root, 400, "bash", 1, cmdline=["bash", "-c", "sleep 9"])
    return base


@pytest.fixture
def fa(monkeypatch, tmp_path):
    """The agent with PROC/SYS/POWER_SUPPLY pointing at a fake Frame."""
    home = tmp_path / "home"
    home.mkdir()
    a = load_agent(monkeypatch, home)
    root = tmp_path / "frame"
    frame_tree(root, home)
    monkeypatch.setattr(a, "PROC", str(root / "proc"))
    monkeypatch.setattr(a, "SYS", str(root / "sys"))
    monkeypatch.setattr(a, "POWER_SUPPLY", str(root / "power_supply"))
    a.ROOT = root
    return a


def monitor(fa):
    clock = {"t": 100.0}
    m = fa.Monitor(clock=lambda: clock["t"])
    return m, clock


def test_static_and_readers(fa):
    m, _ = monitor(fa)
    st = m.static()
    assert st["agent"] == fa.AGENT_VERSION and st["cores"] == 2 and st["gpu_max_mhz"] == 903
    assert st["clusters"] == [{"cpus": [0], "max_mhz": 2265}, {"cpus": [1], "max_mhz": 3148}]
    assert st["rails"] == ["apc0", "apc1", "gfx", "vph"]  # "unused"/"bob" aren't read (each read is an I2C transfer)
    assert st["temp_groups"] == ["CPU", "GPU", "Battery"] and st["fan"] is True
    s = m.sample(wall=2000.0)
    assert s["temps"] == {"CPU": 52.5, "GPU": 47.0, "Battery": 23.2}
    assert s["zones"]["CPU"] == {"cpu0": 41.0, "cpu7-top": 52.5}
    assert s["power"]["system"] == 6.0 and s["power"]["cpu"] == 0.75 and s["power"]["gpu"] == 1.5
    assert s["fan"] == 8200 and s["cpu"]["mhz"] == [1248, 1708]
    assert s["mem"] == {"total": 16000000 * 1024, "avail": 8000000 * 1024, "swap_total": 8000000 * 1024,
                        "swap_free": 7000000 * 1024}
    assert s["psi"] == {"cpu": 2.5, "memory": 2.5, "io": 2.5}
    b = s["battery"]
    assert b["percent"] == 80 and b["watts"] == -4.0 and b["draining"] and not b["plugged"]
    assert b["empty_s"] == 7200 and b["cycles"] == 7 and b["temp"] == 23.3 and b["health"] == "Good"
    assert s["gpu"] == {"busy": None, "mhz": 231}  # no delta yet
    assert "disk" in s and s["self_ms"] >= 0


def test_cpu_gpu_net_deltas(fa):
    m, clock = monitor(fa)
    m.sample(wall=2000.0)
    p = fa.ROOT / "proc"
    w(p / "stat", "cpu  200 0 100 900 0 0 0 0 0 0\ncpu0 100 0 50 450 0 0 0 0 0 0\n"
                  "cpu1 100 0 50 450 0 0 0 0 0 0\nbtime 1000\n")
    w(p / "net" / "dev", "a\nb\n  wlan0: 3000 0 0 0 0 0 0 0 2500 0 0 0 0 0 0 0\n")
    set_gpu(fa.ROOT, 302, 3, 5000 + 250_000_000)   # 25 % of 1 s
    set_gpu(fa.ROOT, 101, 7, 1000 + 100_000_000)   # 10 %
    clock["t"] += 1.0
    s = m.sample(wall=2001.0)
    assert s["cpu"]["total"] == 50.0 and s["cpu"]["cores"] == [50.0, 50.0]
    assert s["net"] == {"wlan0": [2000, 500]}
    assert s["gpu"]["busy"] == 35.0


def test_processes_games_and_filters(fa):
    m, clock = monitor(fa)
    s = m.sample(wall=2000.0)
    names = {p["pid"]: p for p in m.procs}
    assert 3 not in names and 2 not in names  # kernel threads
    assert {names[pid]["game"] for pid in (200, 300, 301, 302)} == {GAME}
    assert names[100]["group"] == "steam" and names[101]["group"] == "steamvr" and names[400]["group"] == "other"
    assert names[101]["critical"] and names[1]["locked"]
    # default filter: the game's processes + the busiest others as context
    shown = s["procs"]
    assert s["filter"] == "game"
    assert {p["pid"] for p in shown if p["game"]} == {200, 300, 301, 302}
    assert all(p.get("context") for p in shown if not p["game"]) and len([p for p in shown if not p["game"]]) == 3
    m.filter = "steam"
    assert {p["pid"] for p in m.filtered()} == {100, 101}
    m.filter = "all"
    assert len(m.filtered()) == len(m.procs)
    # game card: fps from the pacing line, memory from the container's cgroup (not conmon's), process count
    g = s["games"][0]
    assert g["package"] == GAME and g["title"] == "Example Game" and g["appid"] == APPID
    assert g["fps"] == 71.9 and g["frame_ms"] == 0.1 and g["mem"] == 2000000000 and g["processes"] == 4
    # next scan: CPU % per process + the container cgroup's CPU %
    set_cpu(fa.ROOT, 302, 50 + 100)  # 100 jiffies in 1 s = 1 core of 2 → 50 %
    clock["t"] += 1.0
    m.sample(wall=2001.0)  # odd tick: no process scan (GPU and container CPU % are still per tick)
    set_gpu(fa.ROOT, 302, 3, 5000 + 250_000_000)
    w(fa.ROOT / "sys/fs/cgroup/user.slice/libpod-abc.scope/cpu.stat", "usage_usec 1500000\n")  # 0.5 s of 2 cores
    clock["t"] += 1.0
    s = m.sample(wall=2002.0)
    game = next(p for p in s["procs"] if p["pid"] == 302)
    assert game["cpu"] == 25.0 and game["gpu"] == 25.0
    assert s["games"][0]["cpu"] == 25.0 and s["games"][0]["gpu"] == 25.0


def test_pacing_tail_is_incremental_and_goes_stale(fa):
    m, clock = monitor(fa)
    base = str(fa.ROOT.parent / "home/Applications/quest-frame" / GAME)
    assert m.fps(base, 100.0) == (71.9, 0.1)
    with open(os.path.join(base, "launch.log"), "a") as f:
        f.write("I FrameBridge: pacing: 45.0 fps, displayTime vs predicted: avg 2.00 ms, max 0.00 ms\n")
    assert m.fps(base, 101.0) == (45.0, 2.0)
    assert m.fps(base, 120.0) == (None, None)  # no line for 15 s: loading or ended
    open(os.path.join(base, "launch.log"), "w").write("I FrameBridge: pacing: 72.0 fps\n")  # a new launch
    assert m.fps(base, 121.0) == (72.0, None)


def test_kill_safety(fa):
    m, _ = monitor(fa)
    m.sample(wall=2000.0)
    with pytest.raises(fa.AgentError, match="system"):
        m.kill(1)
    with pytest.raises(fa.AgentError, match="CRITICAL: vrcompositor"):
        m.kill(101)
    assert m.kill(999) == {"ended": True, "pid": 999, "note": "already gone"}


def test_kill_other_user_only_inside_a_game(fa, monkeypatch):
    m = fa.Monitor(uid=12345)  # nothing in the fake tree belongs to this user
    m.sample(wall=2000.0)
    with pytest.raises(fa.AgentError, match="another user"):
        m.kill(400)


def test_kill_real_process(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    m = a.Monitor()
    with pytest.raises(a.AgentError):
        m.kill(os.getpid())  # its own process tree (the SSH session) is never ended
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        res = m.kill(child.pid, "TERM")
        assert res["ended"] is True and res["pid"] == child.pid
    finally:
        child.kill()
        child.wait()


def test_end_game_steam_then_stop(fa, monkeypatch):
    calls, state = [], {"running": True}
    monkeypatch.setattr(fa, "devkit_gameid", lambda pkg: None)
    monkeypatch.setattr(fa, "container_running", lambda appid: state["running"])
    monkeypatch.setattr(fa, "steam_js", lambda expr, timeout=5: calls.append(expr))

    def stop(args):
        calls.append(("stop", args["package"]))
        state["running"] = False
        return {"stopped": True}
    monkeypatch.setattr(fa, "cmd_stop", stop)
    res = fa.monitor_end_game(GAME, wait=0.2)
    assert calls[0] == f"SteamClient.Apps.TerminateApp('{fa.steam_gameid(APPID)}', false)"
    assert calls[1] == ("stop", GAME) and res == {"ended": True, "via": "stop", "package": GAME}
    # Steam's Exit game was enough: no podman kill
    calls.clear()
    monkeypatch.setattr(fa, "steam_js", lambda expr, timeout=5: state.update(running=False))
    state["running"] = True
    assert fa.monitor_end_game(GAME, wait=0.2)["via"] == "steam"
    with pytest.raises(fa.AgentError, match="bad package"):
        fa.monitor_end_game("x y")


class FakeMon:
    def __init__(self):
        self.filter, self.n = "game", 0

    def static(self):
        return {"agent": 60}

    def sample(self):
        self.n += 1
        return {"n": self.n, "filter": self.filter}

    def kill(self, pid, sig="TERM", force=False):
        if pid == 1:
            raise RuntimeError("nope")
        return {"ended": True, "pid": pid, "sig": sig, "force": force}


def test_session_protocol(fa):
    r, wfd = os.pipe()
    stdin, writer = os.fdopen(r), os.fdopen(wfd, "w")
    out = io.StringIO()
    mon = FakeMon()
    t = threading.Thread(target=fa.monitor_session, args=(stdin, out, mon), daemon=True)
    t.start()

    def lines():
        return [json.loads(x) for x in out.getvalue().splitlines()]
    writer.write(json.dumps({"procs": "all"}) + "\n" + json.dumps({"interval": 5}) + "\n"
                 + json.dumps({"id": 7, "kill": 42, "sig": "KILL", "force": True}) + "\n"
                 + json.dumps({"id": 8, "kill": 1}) + "\n" + "not json\n")
    writer.flush()
    deadline = time.time() + 5
    while time.time() < deadline and len([x for x in lines() if "reply" in x]) < 2:
        time.sleep(0.02)
    writer.close()  # EOF ends the session
    t.join(5)
    assert not t.is_alive()
    got = lines()
    assert got[0] == {"ready": 1, "static": {"agent": 60}}
    assert mon.filter == "all"
    replies = {x["reply"]: x for x in got if "reply" in x}
    assert replies[7] == {"reply": 7, "ok": True, "result": {"ended": True, "pid": 42, "sig": "KILL", "force": True}}
    assert replies[8]["ok"] is False and "nope" in replies[8]["error"]
    assert any(x.get("filter") == "all" for x in got if "n" in x)  # a fresh sample after the filter change


def test_monitor_cli_ends_at_eof(tmp_path):
    """`_monitor` on the real machine: ready line, a sample, and exit once stdin closes."""
    p = subprocess.Popen([sys.executable, str(AGENT), "_monitor"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    ready = json.loads(p.stdout.readline())
    sample = json.loads(p.stdout.readline())
    p.stdin.close()
    assert p.wait(timeout=10) == 0
    assert ready["ready"] == 1 and ready["static"]["cores"] >= 1
    assert "cpu" in sample and "mem" in sample and "procs" in sample
