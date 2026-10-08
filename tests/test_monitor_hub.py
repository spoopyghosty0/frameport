"""MonitorHub: one shared `_monitor` stream for every subscriber (union of modules, fastest interval, fan-out,
reconnect), and the agent's per-module collection (monitor_plan, {"modules": …})."""
import io
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest
from test_monitor_agent import fa, monitor  # noqa: F401  (fixture + helper)

from frameport.frame import monitor as M
from frameport.frame import monitor_hub as H


class FakeSession:
    instances: list = []

    def __init__(self, frame, on_sample, on_end, agent=63):
        self.frame, self.on_sample, self.on_end = frame, on_sample, on_end
        self.static = {"agent": agent}
        self.calls: list = []
        self.closed = False
        FakeSession.instances.append(self)

    def set_interval(self, s):
        self.calls.append(("interval", s))

    def set_modules(self, m):
        self.calls.append(("modules", m))
        return True

    def set_filter(self, w):
        self.calls.append(("filter", w))

    def pause(self, p):
        self.calls.append(("pause", p))

    def kill(self, pid, sig="TERM", force=False):
        return {"ended": True, "pid": pid, "sig": sig, "force": force}

    def end_game(self, pkg):
        return {"ended": True, "package": pkg}

    def close(self):
        self.closed = True

    # test helpers
    def push(self, sample):
        self.on_sample(sample)

    def end(self, why="gone"):
        self.closed = True
        self.on_end(why)


class Timers:
    def __init__(self):
        self.pending: list = []

    def __call__(self, delay, fn):
        t = SimpleNamespace(delay=delay, fn=fn, cancelled=False)
        t.cancel = lambda: setattr(t, "cancelled", True)
        self.pending.append(t)
        return t

    def fire(self):
        due, self.pending = self.pending, []
        for t in due:
            if not t.cancelled:
                t.fn()


@pytest.fixture
def hub():
    FakeSession.instances = []
    timers = Timers()
    target = SimpleNamespace(frame="frame")
    box = {"target": target}
    h = H.MonitorHub(lambda: box["target"], session_factory=FakeSession, spawn=lambda fn: fn(), timer=timers)
    h.timers, h.box = timers, box
    return h


def sub(modules, interval, paused=False):
    s = H.Subscriber("x", H.norm_modules(modules), interval, lambda _s: None)
    s.paused = paused
    return s


def test_merge():
    assert H.merge([]) == (None, None)
    assert H.merge([sub({"cpu"}, 2), sub({"games", "battery"}, 1)]) == (frozenset({"cpu", "games", "battery"}), 1.0)
    assert H.merge([sub({"cpu"}, 2), sub("all", 5)]) == ("all", 2.0)
    assert H.merge([sub({"cpu"}, 2), sub("all", 0.1, paused=True)]) == (frozenset({"cpu"}), 2.0)
    assert H.merge([sub("all", 0.1, paused=True)]) == (None, None)
    assert H.merge([sub({"cpu"}, 0.3)]) == (frozenset({"cpu"}), 0.25)  # snapped to what the agent accepts
    assert H.snap_interval(0.01) == 0.1 and H.snap_interval(10) == 5.0


def test_subscribe_starts_once_and_reconfigures(hub):
    states = []
    hub.subscribe("card", {"battery", "games"}, 2, lambda s: None, lambda *a: states.append(a))
    assert len(FakeSession.instances) == 1
    s = FakeSession.instances[0]
    assert s.calls == [("interval", 2.0), ("modules", frozenset({"battery", "games"}))]
    assert states == [("card", "connecting", None), ("card", "live", None)]
    assert hub.static == {"agent": 63}
    later = []
    hub.subscribe("tab", "all", 0.5, lambda s: None, lambda *a: later.append(a))
    assert len(FakeSession.instances) == 1  # same stream, reconfigured
    assert s.calls[-2:] == [("interval", 0.5), ("modules", "all")]
    assert later == [("tab", "live", None)]
    hub.update("tab", interval=0.1)
    assert s.calls[-1] == ("interval", 0.1)
    hub.unsubscribe("tab")
    assert s.calls[-2:] == [("interval", 2.0), ("modules", frozenset({"battery", "games"}))] and not s.closed
    hub.unsubscribe("card")
    assert s.closed and hub.session is None and hub.state == "idle"
    hub.subscribe("card", {"cpu"}, 1, lambda s: None)
    assert len(FakeSession.instances) == 2  # lazily started again


def test_pause_excludes_and_stops(hub):
    got = {"a": [], "b": []}
    hub.subscribe("a", {"cpu"}, 1, got["a"].append)
    hub.subscribe("b", "all", 0.25, got["b"].append)
    s = FakeSession.instances[0]
    hub.pause("b", True)
    assert s.calls[-2:] == [("interval", 1.0), ("modules", frozenset({"cpu"}))]
    s.push({"n": 1})
    assert got == {"a": [{"n": 1}], "b": []}
    hub.pause("a", True)  # nobody active: the stream ends
    assert s.closed and hub.session is None
    hub.pause("b", False)
    assert len(FakeSession.instances) == 2 and FakeSession.instances[1].calls[-1] == ("interval", 0.25)


def test_fan_out_survives_a_failing_subscriber(hub):
    got = []

    def bad(_s):
        raise RuntimeError("view bug")
    hub.subscribe("bad", "all", 1, bad)
    hub.subscribe("good", "all", 1, got.append)
    FakeSession.instances[0].push({"n": 1})
    FakeSession.instances[0].push({"n": 2})
    assert got == [{"n": 1}, {"n": 2}]


def test_reconnect_after_loss(hub):
    states = []
    hub.subscribe("tab", {"procs"}, 1, lambda s: None, lambda *a: states.append(a[1:]))
    hub.set_filter("all")
    hub.pause_stream(True)
    first = FakeSession.instances[0]
    first.end("connection reset")
    assert hub.session is None and states[-1] == ("lost", "connection reset")
    assert len(hub.timers.pending) == 1 and hub.timers.pending[0].delay == H.RETRY_SECONDS
    hub.timers.fire()
    second = FakeSession.instances[1]
    assert hub.session is second and states[-1] == ("live", None)
    # the new stream gets the subscriber's config plus the kept filter and pause
    assert second.calls == [("interval", 1.0), ("modules", frozenset({"procs"})), ("filter", "all"), ("pause", True)]
    assert hub.kill(5)["pid"] == 5 and hub.end_game("com.x.y")["package"] == "com.x.y"


def test_retry_cancelled_when_nobody_is_left(hub):
    hub.subscribe("tab", "all", 1, lambda s: None)
    FakeSession.instances[0].end()
    hub.unsubscribe("tab")
    hub.timers.fire()
    assert len(FakeSession.instances) == 1 and hub.session is None


def test_error_without_target_then_reconnect(hub):
    states = []
    hub.box["target"] = None
    hub.subscribe("tab", "all", 1, lambda s: None, lambda *a: states.append(a[1]))
    assert states == ["connecting", "error"] and hub.session is None and not hub.timers.pending
    with pytest.raises(M.MonitorUnavailable):
        hub.kill(1)
    hub.box["target"] = SimpleNamespace(frame="frame")
    hub.reconnect()
    assert hub.session is FakeSession.instances[0] and states[-1] == "live"


def test_close_ends_everything(hub):
    hub.subscribe("tab", "all", 1, lambda s: None)
    s = FakeSession.instances[0]
    hub.close()
    assert s.closed and hub.session is None
    hub.subscribe("tab", "all", 1, lambda s: None)
    assert len(FakeSession.instances) == 1


def test_session_set_modules_needs_a_new_agent():
    sent = []
    s = M.MonitorSession.__new__(M.MonitorSession)
    s.send = sent.append
    s.static = {"agent": 62}
    assert s.set_modules({"cpu"}) is False and sent == []
    s.static = {"agent": M.MIN_AGENT_MODULES}
    assert s.set_modules({"cpu", "battery"}) is True and sent == [{"modules": ["battery", "cpu"]}]
    assert s.set_modules("all") and sent[-1] == {"modules": "all"}


# ------------------------------------------------------------------------------------------------ agent side
def test_monitor_plan(fa):  # noqa: F811
    every = set(fa.MON_MODULES) | {"scan", "gpu_ns"}
    assert fa.monitor_plan() == every and fa.monitor_plan("all") == every and fa.monitor_plan("cpu") == every
    assert fa.monitor_plan([]) == frozenset()
    assert fa.monitor_plan(["battery", "cpu", "bogus"]) == {"battery", "cpu"}  # no process scan
    assert fa.monitor_plan(["games"]) == {"games", "scan", "gpu_ns"}
    assert fa.monitor_plan(["gpu"]) == {"gpu", "scan", "gpu_ns"}  # GPU busy comes from the scanned render fds
    assert set(fa.MON_MODULES) == set(M.MODULES)


def test_sample_collects_only_requested_modules(fa):  # noqa: F811
    m, clock = monitor(fa)
    m.set_modules(["battery", "cpu"])
    scanned = []
    m.scan = lambda *a: scanned.append(a)  # instance attribute, removed below (monkeypatch.undo would undo `fa`)
    s = m.sample(wall=2000.0)
    assert set(s) == {"t", "dt", "battery", "cpu", "self_ms"} and not scanned
    m.set_modules(["games"])
    del m.scan
    clock["t"] += 0.25
    s = m.sample(wall=2000.25)
    assert "games" in s and s["games"][0]["package"] == "com.example.game" and "procs" not in s and "cpu" not in s
    m.set_modules(["procs"])  # newly wanted: scanned at once, not 2 s later
    clock["t"] += 0.25
    assert "procs" in m.sample(wall=2000.5)
    m.set_modules("all")
    clock["t"] += 0.25
    full = m.sample(wall=2000.75)
    assert {"cpu", "mem", "psi", "temps", "power", "battery", "gpu", "games"} <= set(full)
    assert fa.Monitor(clock=lambda: 0.0).static()["modules"] == list(fa.MON_MODULES)


def test_session_modules_message(fa):  # noqa: F811
    class Mon:
        def __init__(self):
            self.modules = None

        def static(self):
            return {"agent": fa.AGENT_VERSION}

        def sample(self):
            return {"modules": self.modules}

        def set_modules(self, modules):
            self.modules = modules

    r, wfd = os.pipe()
    stdin, writer = os.fdopen(r), os.fdopen(wfd, "w")
    out = io.StringIO()
    mon = Mon()
    t = threading.Thread(target=fa.monitor_session, args=(stdin, out, mon), daemon=True)
    t.start()
    writer.write(json.dumps({"modules": ["battery"]}) + "\n")
    writer.flush()
    deadline = time.time() + 5
    while time.time() < deadline and mon.modules != ["battery"]:
        time.sleep(0.02)
    writer.close()
    t.join(5)
    assert mon.modules == ["battery"] and not t.is_alive()
