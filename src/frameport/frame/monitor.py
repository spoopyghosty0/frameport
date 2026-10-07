"""Monitor: a live view of the Frame (CPU/GPU/memory/temperatures/power/battery, the running game, processes) streamed
by the agent's `_monitor` session as one JSON line per tick over one SSH channel, plus actions on processes and games
(answered on the same channel). The session runs only while the GUI's Monitor tab is open; the agent ends it at EOF.

The helpers below (History, formatting, levels, sort_filter) are pure so the view stays thin and they are testable."""
from __future__ import annotations

import itertools
import json
import posixpath
import threading
from collections import deque
from collections.abc import Callable

from ..core import applog

MIN_AGENT = 60  # first agent with `_monitor`
HISTORY = 120   # points per sparkline (2 min at 1 s)
REPLY_TIMEOUT = 15.0


class MonitorUnavailable(RuntimeError):
    pass


class ProcessCritical(RuntimeError):
    """Ending the process would stop Steam, SteamVR or the desktop: ask the user, then retry with force=True."""


class MonitorSession:
    """One `_monitor` stream. `on_sample(dict)` is called from the reader thread for every sample; `on_end(reason)`
    once when the stream stops by itself (connection lost, agent ended)."""

    def __init__(self, frame, on_sample: Callable[[dict], None], on_end: Callable[[str], None] | None = None,
                 timeout: float = 20):
        self.frame = frame
        self.on_sample = on_sample
        self.on_end = on_end
        self.closed = False
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._waiting: dict[int, list] = {}  # id -> [Event, reply]
        frame.ensure_agent()
        remote = posixpath.join(frame.home, ".local/share/frameport/agent/frameport_agent.py")
        self._stdin, self._stdout, _err = frame.client.exec_command(f"python3 {remote} _monitor", timeout=timeout)
        first = json.loads(self._stdout.readline() or "{}")
        if not first.get("ready"):
            self.close()
            raise MonitorUnavailable(first.get("error") or "the Frame didn't start the monitor")
        self.static: dict = first.get("static") or {}
        self._stdout.channel.settimeout(None)
        self._thread = threading.Thread(target=self._read, name="frame-monitor", daemon=True)
        self._thread.start()

    # ----------------------------------------------------------------- stream
    def _read(self) -> None:
        reason = "ended"
        try:
            for line in self._stdout:
                if self.closed:
                    return
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "reply" in msg:
                    slot = self._waiting.get(msg.get("reply"))
                    if slot:
                        slot[1] = msg
                        slot[0].set()
                    continue
                try:
                    self.on_sample(msg)
                except Exception:  # noqa: BLE001 - a view bug must not end the stream
                    applog.log.exception("monitor: sample handler failed")
        except Exception as exc:  # noqa: BLE001 - connection lost
            reason = str(exc) or type(exc).__name__
        finally:
            for slot in list(self._waiting.values()):
                slot[0].set()
        if not self.closed:
            self.closed = True
            if self.on_end:
                self.on_end(reason)

    def send(self, msg: dict) -> None:
        with self._lock:
            if self.closed:
                return
            self._stdin.write(json.dumps(msg) + "\n")
            self._stdin.flush()

    def set_interval(self, seconds: int) -> None:
        self.send({"interval": seconds})

    def set_filter(self, which: str) -> None:
        self.send({"procs": which})

    def pause(self, paused: bool) -> None:
        self.send({"pause": bool(paused)})

    def _request(self, msg: dict, timeout: float = REPLY_TIMEOUT) -> dict:
        rid = next(self._ids)
        slot = [threading.Event(), None]
        self._waiting[rid] = slot
        try:
            self.send({**msg, "id": rid})
            if not slot[0].wait(timeout) or slot[1] is None:
                raise MonitorUnavailable("the Frame didn't answer" if not self.closed else "the monitor stopped")
        finally:
            self._waiting.pop(rid, None)
        reply = slot[1]
        if not reply.get("ok"):
            err = reply.get("error") or "failed"
            if err.startswith("CRITICAL:"):
                raise ProcessCritical(err.split(":", 1)[1].strip())
            raise RuntimeError(err)
        return reply.get("result") or {}

    def kill(self, pid: int, sig: str = "TERM", force: bool = False) -> dict:
        """End one process: {"ended": bool, "pid", "name"}. TERM waits up to 3 s on the Frame."""
        return self._request({"kill": int(pid), "sig": sig, "force": force})

    def end_game(self, package: str) -> dict:
        """Steam's Exit game, then a direct stop: {"ended": bool, "via": "steam"|"stop"}."""
        return self._request({"end_game": package}, timeout=30)

    def close(self) -> None:
        with self._lock:
            self.closed = True
            try:
                self._stdin.channel.shutdown_write()  # EOF: the agent's monitor ends
                self._stdin.close()
            except Exception:  # noqa: BLE001 - the connection may be gone already
                pass


# --------------------------------------------------------------------------------------------- pure helpers
class History:
    """Rolling series for the sparklines: name -> deque of the last HISTORY values (None = a gap)."""

    def __init__(self, size: int = HISTORY):
        self.size = size
        self.series: dict[str, deque] = {}

    def add(self, name: str, value: float | None) -> None:
        self.series.setdefault(name, deque(maxlen=self.size)).append(value)

    def get(self, name: str) -> list:
        return list(self.series.get(name, ()))

    def clear(self) -> None:
        self.series.clear()


def series_of(sample: dict) -> dict[str, float | None]:
    """The values one sample adds to the History."""
    mem = sample.get("mem") or {}
    total = mem.get("total") or 0
    temps = sample.get("temps") or {}
    bat = sample.get("battery") or {}
    game = (sample.get("games") or [None])[0] or {}
    return {
        "cpu": (sample.get("cpu") or {}).get("total"),
        "gpu": (sample.get("gpu") or {}).get("busy"),
        "mem": 100.0 * (total - mem.get("avail", 0)) / total if total else None,
        "temp": max(temps.values()) if temps else None,
        "power": (sample.get("power") or {}).get("system"),
        "battery": bat.get("percent"),
        "fps": game.get("fps"),
    }


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "–"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_rate(n: float | None) -> str:
    return "–" if n is None else fmt_bytes(n) + "/s"


def fmt_watts(w: float | None) -> str:
    if w is None:
        return "–"
    return f"{w:.2f} W" if abs(w) < 10 else f"{w:.1f} W"


def fmt_duration(s: float | None) -> str:
    """'1 h 05 min', '12 min', '45 s'."""
    if s is None:
        return "–"
    s = int(s)
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60:02d} min"
    if s >= 60:
        return f"{s // 60} min"
    return f"{s} s"


def fmt_pct(v: float | None) -> str:
    return "–" if v is None else f"{v:.0f} %"


# (warn, error) thresholds; "low" metrics warn when the value falls below them
LEVELS = {
    "cpu": (85, 97), "gpu": (90, 99), "mem": (85, 93), "psi_memory": (10, 30),
    "temp": (80, 90), "temp_Battery": (45, 50), "fps_ratio": (0.9, 0.75),
}
LOW = {"fps_ratio"}


def level(metric: str, value: float | None) -> str:
    """ok | warn | error for a metric's value (unknown metric or no value: ok)."""
    if value is None or metric not in LEVELS:
        return "ok"
    warn, err = LEVELS[metric]
    if metric in LOW:
        return "error" if value < err else "warn" if value < warn else "ok"
    return "error" if value >= err else "warn" if value >= warn else "ok"


def temp_level(group: str, value: float | None) -> str:
    return level(f"temp_{group}" if f"temp_{group}" in LEVELS else "temp", value)


SORT_KEYS = ("cpu", "gpu", "rss", "name")


def sort_filter(procs: list[dict], query: str = "", key: str = "cpu", descending: bool = True) -> list[dict]:
    """Processes matching the search (name, PID or game package) in the chosen order."""
    q = (query or "").strip().lower()
    if q:
        procs = [p for p in procs if q in p.get("name", "").lower() or q == str(p.get("pid"))
                 or q in (p.get("game") or "").lower()]
    if key == "name":
        return sorted(procs, key=lambda p: (p.get("name", "").lower(), p.get("pid", 0)), reverse=descending)
    return sorted(procs, key=lambda p: (p.get(key) or 0, p.get("rss") or 0), reverse=descending)


def group_label(group: str) -> str:
    """'game:<pkg>' -> 'Game'; steam/steamvr/desktop/other as shown in the table."""
    if group.startswith("game:"):
        return "Game"
    return {"steam": "Steam", "steamvr": "SteamVR", "desktop": "Desktop"}.get(group, "Other")


def power_split(power: dict) -> list[tuple[str, float]]:
    """[(label, W)] for the stacked power bar: CPU, GPU, NPU, and the rest of the system rail."""
    system = power.get("system") or 0.0
    parts = [(k.upper(), power.get(k) or 0.0) for k in ("cpu", "gpu", "npu")]
    rest = max(0.0, system - sum(v for _k, v in parts))
    return [*parts, ("Other", rest)]


def battery_line(bat: dict | None) -> str:
    """'2 h 10 min left · −4.0 W' / 'Charging · full in 35 min' / 'Full'."""
    if not bat:
        return "No battery"
    status = bat.get("status") or ""
    watts = bat.get("watts")
    if status == "Full":
        return "Full"
    if status == "Charging" or (bat.get("plugged") and not bat.get("draining")):
        full = bat.get("full_s")
        return "Charging" + (f" · full in {fmt_duration(full)}" if full and full < 360000 else "")
    left = bat.get("empty_s")
    parts = [f"{fmt_duration(left)} left"] if left and left < 360000 else []
    if watts is not None:
        parts.append(fmt_watts(abs(watts)))
    return " · ".join(parts) or status


REFRESH_RATES = (72, 80, 90, 96, 108, 120, 144)  # what the Frame's display offers (vrserver.txt)


def fps_target(values: list) -> int | None:
    """The refresh rate a game is presumably aiming for: the lowest Frame rate at or above its best recent fps
    (with 3 % slack), for the dashed guide on the fps sparkline."""
    best = max((v for v in values if v is not None), default=None)
    if best is None:
        return None
    return next((r for r in REFRESH_RATES if best <= r * 1.03), REFRESH_RATES[-1])
