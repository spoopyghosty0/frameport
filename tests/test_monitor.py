"""Monitor client: the `_monitor` line protocol (samples, replies, end of stream) and the pure helpers."""
import io
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from frameport.frame import monitor as M


class _Chan:
    def __init__(self):
        self.shut = False

    def settimeout(self, t):
        pass

    def shutdown_write(self):
        self.shut = True


class _In(io.StringIO):
    def __init__(self, on_line=None):
        super().__init__()
        self.channel = _Chan()
        self.on_line = on_line

    def write(self, s):
        n = super().write(s)
        if self.on_line:
            for line in s.splitlines():
                self.on_line(json.loads(line))
        return n

    def close(self):
        pass


class _Out:
    """stdout of the SSH channel: lines are fed by the test; None ends it, an exception breaks it."""

    def __init__(self):
        self.q = queue.Queue()
        self.channel = _Chan()

    def feed(self, obj):
        self.q.put(obj if obj is None or isinstance(obj, Exception) else json.dumps(obj) + "\n")

    def readline(self):
        item = self.q.get(timeout=5)
        return item or ""

    def __iter__(self):
        while True:
            item = self.q.get(timeout=5)
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item


def _session(ready=None, on_line=None):
    out = _Out()
    out.feed(ready if ready is not None else {"ready": 1, "static": {"agent": 60, "cores": 8}})
    stdin = _In(on_line)
    client = SimpleNamespace(exec_command=lambda cmd, timeout=None: (stdin, out, None))
    frame = SimpleNamespace(client=client, home="/home/steamos", ensure_agent=lambda: None)
    samples, ended = [], []
    return frame, stdin, out, samples, ended


def test_stream_samples_controls_and_close():
    frame, stdin, out, samples, ended = _session()
    s = M.MonitorSession(frame, samples.append, ended.append)
    assert s.static == {"agent": 60, "cores": 8}
    out.feed({"t": 1, "cpu": {"total": 5}})
    out.feed("not json")  # ignored
    out.feed({"t": 2})
    deadline = time.time() + 5
    while len(samples) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert [x["t"] for x in samples] == [1, 2]
    s.set_interval(2)
    s.set_filter("all")
    s.pause(True)
    s.close()
    out.feed(None)
    assert [json.loads(x) for x in stdin.getvalue().splitlines()] == [{"interval": 2}, {"procs": "all"},
                                                                     {"pause": True}]
    assert stdin.channel.shut and s.closed
    s._thread.join(5)
    assert ended == []  # closed by us: no "ended" callback
    s.set_interval(5)  # after close: ignored
    assert len(stdin.getvalue().splitlines()) == 3


def test_replies_kill_end_game_and_critical():
    holder = {}

    def answer(msg):  # the "agent": answers each request on stdout
        out = holder["out"]
        if msg.get("kill") == 101 and not msg.get("force"):
            out.feed({"reply": msg["id"], "ok": False, "error": "CRITICAL: vrcompositor"})
        elif msg.get("kill") == 1:
            out.feed({"reply": msg["id"], "ok": False, "error": "systemd (1) is part of the system"})
        elif "kill" in msg:
            out.feed({"reply": msg["id"], "ok": True, "result": {"ended": True, "pid": msg["kill"],
                                                                 "sig": msg["sig"], "force": msg["force"]}})
        elif "end_game" in msg:
            out.feed({"reply": msg["id"], "ok": True, "result": {"ended": True, "via": "steam"}})
    frame, stdin, out, samples, ended = _session(on_line=answer)
    holder["out"] = out
    s = M.MonitorSession(frame, samples.append, ended.append)
    assert s.kill(42) == {"ended": True, "pid": 42, "sig": "TERM", "force": False}
    assert s.kill(101, "KILL", force=True)["force"] is True
    with pytest.raises(M.ProcessCritical, match="vrcompositor"):
        s.kill(101)
    with pytest.raises(RuntimeError, match="part of the system"):
        s.kill(1)
    assert s.end_game("com.x.y") == {"ended": True, "via": "steam"}
    s.close()
    out.feed(None)


def test_stream_lost_calls_on_end_and_unblocks_requests():
    frame, stdin, out, samples, ended = _session()
    s = M.MonitorSession(frame, samples.append, ended.append)
    result = {}

    def ask():
        try:
            s.kill(5)
        except M.MonitorUnavailable as exc:
            result["err"] = str(exc)
    t = threading.Thread(target=ask)
    t.start()
    time.sleep(0.1)
    out.feed(OSError("Socket is closed"))
    t.join(5)
    s._thread.join(5)
    assert ended == ["Socket is closed"] and s.closed
    assert "stopped" in result["err"]


def test_refused_by_an_old_agent():
    frame, *_ = _session(ready={"ok": False, "error": "usage: frameport_agent.py <...>"})
    with pytest.raises(M.MonitorUnavailable, match="usage"):
        M.MonitorSession(frame, lambda s: None)


def test_history_and_series():
    h = M.History(size=3)
    for v in (1, 2, None, 4):
        h.add("cpu", v)
    assert h.get("cpu") == [2, None, 4] and h.get("nope") == []
    s = {"cpu": {"total": 12.5}, "gpu": {"busy": None}, "mem": {"total": 1000, "avail": 250},
         "temps": {"CPU": 50.0, "GPU": 61.5}, "power": {"system": 6.2}, "battery": {"percent": 80},
         "games": [{"fps": 71.9}]}
    assert M.series_of(s) == {"cpu": 12.5, "gpu": None, "mem": 75.0, "temp": 61.5, "power": 6.2, "battery": 80,
                              "fps": 71.9, "temp:CPU": 50.0, "temp:GPU": 61.5}
    assert M.series_of({**s, "fan": 8200})["fan"] == 8200


def test_history_buckets_by_time():
    """Several samples within one second become one (averaged) point: 2 minutes of chart at any interval."""
    h = M.History(size=3)
    for t, v in ((10.0, 2), (10.5, 4), (11.2, 6), (11.9, None), (12.0, 1), (13.0, 5)):
        h.add("cpu", v, t)
    assert h.get("cpu") == [6.0, 1.0, 5.0]  # 10 s = (2+4)/2 rolled out of the 3-point window
    h.add("x", None, 1.0)
    assert h.get("x") == [None]


def test_group_procs():
    procs = [{"pid": 1, "ppid": 10, "name": "vrwebhelper", "cpu": 1.0, "gpu": 0, "rss": 5, "age": 30},
             {"pid": 2, "ppid": 99, "name": "steam", "cpu": 0.8, "gpu": 0, "rss": 9, "age": 50},
             {"pid": 3, "ppid": 10, "name": "vrwebhelper", "cpu": 0.5, "gpu": 2, "rss": 7, "age": 40,
              "critical": True},
             {"pid": 4, "ppid": 11, "name": "vrwebhelper", "cpu": 0.1, "gpu": 0, "rss": 1, "age": 5}]
    rows = M.group_procs(procs)
    assert [r["kind"] for r in rows] == ["group", "proc", "proc"]
    g = rows[0]
    assert g["name"] == "vrwebhelper" and g["count"] == 2 and g["cpu"] == 1.5 and g["gpu"] == 2 and g["rss"] == 12
    assert g["age"] == 40 and g["critical"] and not g["expanded"] and rows[2]["pid"] == 4  # other parent: alone
    rows = M.group_procs(procs, {(10, "vrwebhelper")})
    assert [(r["kind"], r.get("pid")) for r in rows] == [("group", None), ("member", 1), ("member", 3),
                                                         ("proc", 2), ("proc", 4)]
    assert M.fmt_interval(0.25) == "0.25 s" and M.fmt_interval(1) == "1 s" and M.DEFAULT_INTERVAL in M.INTERVALS
    assert M.series_of({})["fps"] is None and M.series_of({})["mem"] is None


def test_formatting():
    assert M.fmt_bytes(512) == "512 B" and M.fmt_bytes(2048) == "2 KB"
    assert M.fmt_bytes(5 * 1024 ** 2) == "5.0 MB" and M.fmt_bytes(16 * 1024 ** 3) == "16.0 GB"
    assert M.fmt_bytes(None) == "–" and M.fmt_rate(2048) == "2 KB/s"
    assert M.fmt_watts(3.558) == "3.56 W" and M.fmt_watts(12.34) == "12.3 W"
    assert M.fmt_duration(45) == "45 s" and M.fmt_duration(720) == "12 min" and M.fmt_duration(3900) == "1 h 05 min"
    assert M.fmt_pct(33.4) == "33%" and M.fmt_pct(None) == "–"


def test_levels():
    assert M.level("cpu", 50) == "ok" and M.level("cpu", 90) == "warn" and M.level("cpu", 99) == "error"
    assert M.level("temp", 79.9) == "ok" and M.level("temp", 85) == "warn" and M.level("temp", 90) == "error"
    assert M.temp_level("Battery", 46) == "warn" and M.temp_level("GPU", 46) == "ok"
    assert M.level("fps_ratio", 1.0) == "ok" and M.level("fps_ratio", 0.8) == "warn"
    assert M.level("fps_ratio", 0.5) == "error"
    assert M.level("unknown", 1e9) == "ok" and M.level("cpu", None) == "ok"


def test_sort_filter_and_labels():
    procs = [{"pid": 1, "name": "steam", "cpu": 5, "rss": 10, "game": None, "gpu": 0},
             {"pid": 22, "name": "com.game", "cpu": 40, "rss": 5, "game": "com.ex.game", "gpu": 30},
             {"pid": 3, "name": "Xwayland", "cpu": 5, "rss": 50, "game": None, "gpu": 2}]
    assert [p["pid"] for p in M.sort_filter(procs)] == [22, 3, 1]
    assert [p["pid"] for p in M.sort_filter(procs, key="name", descending=False)] == [22, 1, 3]
    assert [p["pid"] for p in M.sort_filter(procs, "ex.game")] == [22]
    assert [p["pid"] for p in M.sort_filter(procs, "3")] == [3]  # a PID matches exactly
    assert M.group_label("game:com.x") == "Game" and M.group_label("steamvr") == "SteamVR"
    assert M.group_label("other") == "Other"


def test_power_split_and_battery_line():
    split = M.power_split({"system": 6.0, "cpu": 1.5, "gpu": 2.0, "npu": 0.1})
    assert split == [("CPU", 1.5), ("GPU", 2.0), ("NPU", 0.1), ("Other", pytest.approx(2.4))]
    assert M.battery_line(None) == "No battery"
    assert M.battery_line({"status": "Discharging", "draining": True, "empty_s": 7800, "watts": -4.0}) == \
        "2 h 10 min left · 4.00 W"
    assert M.battery_line({"status": "Charging", "plugged": True, "full_s": 2100}) == "Charging · full in 35 min"
    assert M.battery_line({"status": "Full"}) == "Full"


def test_fps_target():
    assert M.fps_target([]) is None and M.fps_target([None]) is None
    assert M.fps_target([50.0, 71.9, 72.4]) == 72 and M.fps_target([88.0, 90.1]) == 90
    assert M.fps_target([200]) == 144


def test_modules_need_the_agent_that_has_them():
    """Per-module collection came to main with agent 73 (main's 72 had `_monitor` without it): an older agent would
    ignore {"modules": …} and keep sending everything, so the client must not count on it."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "agent" / "frameport_agent.py").read_text()
    version = next(n.value.value for n in ast.parse(src).body if isinstance(n, ast.Assign)
                   and getattr(n.targets[0], "id", "") == "AGENT_VERSION")
    assert "def set_modules" in src and M.MIN_AGENT_MODULES <= version
    assert M.MIN_AGENT_MODULES == 73
    s = SimpleNamespace(static={"agent": 72})
    assert not M.MonitorSession.supports_modules.fget(s)
    s.static["agent"] = 73
    assert M.MonitorSession.supports_modules.fget(s)
