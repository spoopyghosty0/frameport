"""MonitorHub: one app-owned `_monitor` stream shared by every place that shows live Frame data (the Monitor tab, the
sidebar's live Frame card). Subscribers name the modules they need and how often; the hub runs at most one
MonitorSession with the union of the active subscribers' modules and the fastest interval, reconfigures it when
subscribers change, fans every sample out, reconnects every RETRY_SECONDS after the stream was lost and stops it when
nobody (unpaused) is left. No Flet here: callbacks run on the session's reader thread or the hub's own threads.

States passed to `on_state(name, state, detail)`: "connecting", "live", "lost" (detail = why; a retry is scheduled),
"error" (detail = the exception; no automatic retry, `reconnect()` tries again)."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..core import applog
from . import monitor as M

RETRY_SECONDS = 5.0  # reconnect after the stream was lost, while someone subscribes
ALL = "all"


@dataclass
class Subscriber:
    name: str
    modules: frozenset | str  # MODULES names or "all"
    interval: float
    on_sample: Callable[[dict], None]
    on_state: Callable[[str, str, object], None] | None = None
    paused: bool = False


def norm_modules(modules) -> frozenset | str:
    if modules is None or modules == ALL:
        return ALL
    if isinstance(modules, str):
        return frozenset({modules})
    return frozenset(modules)


def snap_interval(seconds: float) -> float:
    """The agent only takes M.INTERVALS: the slowest one that is at least as fast as asked."""
    fits = [i for i in M.INTERVALS if i <= seconds + 1e-9]
    return float(max(fits) if fits else M.INTERVALS[0])


def merge(subs: Iterable[Subscriber]) -> tuple[frozenset | str | None, float | None]:
    """(modules, interval) one stream needs for the active (unpaused) subscribers: the union of their modules ("all"
    if any wants everything) and the fastest interval. (None, None) when nobody is active."""
    active = [s for s in subs if not s.paused]
    if not active:
        return None, None
    if any(s.modules == ALL for s in active):
        modules: frozenset | str = ALL
    else:
        modules = frozenset().union(*(s.modules for s in active))
    return modules, snap_interval(min(s.interval for s in active))


def _timer(delay: float, fn: Callable[[], None]):
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()
    return t


def _thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="monitor-hub-connect", daemon=True).start()


class MonitorHub:
    def __init__(self, get_target: Callable[[], object], session_factory=M.MonitorSession,
                 retry_seconds: float = RETRY_SECONDS, spawn: Callable = _thread, timer: Callable = _timer):
        self.get_target = get_target
        self.session_factory = session_factory
        self.retry_seconds = retry_seconds
        self._spawn = spawn
        self._timer = timer
        self._lock = threading.RLock()
        self._subs: dict[str, Subscriber] = {}
        self.session = None
        self.static: dict = {}
        self.state, self.detail = "idle", None
        self._gen = 0
        self._connecting = False
        self._pending = None  # generation whose connect waits for the lock to be released
        self._retry = None
        self._applied: tuple = (ALL, M.DEFAULT_INTERVAL)  # what the live session runs with
        self._filter = "game"
        self._stream_paused = False
        self.closed = False

    # ---------------------------------------------------------------- subscribers
    def subscribe(self, name: str, modules, interval: float, on_sample: Callable[[dict], None],
                  on_state: Callable[[str, str, object], None] | None = None) -> None:
        with self._lock:
            self._subs[name] = Subscriber(name, norm_modules(modules), float(interval), on_sample, on_state)
            note = self._reconcile()
        self._after(note, name)

    def update(self, name: str, modules=None, interval: float | None = None) -> None:
        with self._lock:
            sub = self._subs.get(name)
            if sub is None:
                return
            if modules is not None:
                sub.modules = norm_modules(modules)
            if interval is not None:
                sub.interval = float(interval)
            note = self._reconcile()
        self._after(note)

    def unsubscribe(self, name: str) -> None:
        with self._lock:
            if self._subs.pop(name, None) is None:
                return
            note = self._reconcile()
        self._after(note)

    def pause(self, name: str, paused: bool = True) -> None:
        """A paused subscriber gets no samples and doesn't count towards the stream's modules/interval."""
        with self._lock:
            sub = self._subs.get(name)
            if sub is None or sub.paused == bool(paused):
                return
            sub.paused = bool(paused)
            note = self._reconcile()
        self._after(note, None if paused else name)

    def subscribed(self, name: str) -> bool:
        return name in self._subs

    def reconnect(self) -> None:
        """Try again now (after an "error", or when the Frame connection came back)."""
        with self._lock:
            if self.session is not None or self._connecting or self.closed:
                return
            self._cancel_retry()
            note = self._reconcile()
        self._after(note)

    def close(self) -> None:
        with self._lock:
            self.closed = True
            self._subs.clear()
            self._stop()

    # ---------------------------------------------------------- pass-through actions
    def set_filter(self, which: str) -> None:
        """The process filter ("game" | "steam" | "all"); kept for reconnects."""
        with self._lock:
            self._filter = which
            session = self.session
        if session is not None:
            session.set_filter(which)

    def pause_stream(self, paused: bool) -> None:
        """Pause the agent's samples for every subscriber (the stream stays open; kept for reconnects)."""
        with self._lock:
            self._stream_paused = bool(paused)
            session = self.session
        if session is not None:
            session.pause(bool(paused))

    def _live(self):
        session = self.session
        if session is None:
            raise M.MonitorUnavailable("the monitor isn't connected")
        return session

    def kill(self, pid: int, sig: str = "TERM", force: bool = False) -> dict:
        return self._live().kill(pid, sig, force)

    def end_game(self, package: str) -> dict:
        return self._live().end_game(package)

    # ---------------------------------------------------------------- internals
    def _reconcile(self) -> list:
        """Start, stop or reconfigure the stream for the current subscribers. Called with the lock held; returns the
        state notifications to send once it's released."""
        if self.closed:
            return []
        modules, interval = merge(self._subs.values())
        if modules is None:
            self._stop()
            return []
        if self.session is not None:
            self._configure(self.session, modules, interval)
            return []
        if self._connecting or self._retry is not None:
            return []
        return self._start()

    def _configure(self, session, modules, interval) -> None:
        old_modules, old_interval = self._applied
        try:
            if interval != old_interval:
                session.set_interval(interval)
            if modules != old_modules:
                session.set_modules(modules)
        except Exception:  # noqa: BLE001 - the stream ending is reported through on_end
            applog.log.exception("monitor hub: reconfigure failed")
        self._applied = (modules, interval)

    def _start(self) -> list:
        self._connecting = True
        self._set_state("connecting", None)
        self._pending = self._gen  # connected by _after() once the lock is released
        return self._targets()

    def _connect(self, gen: int) -> None:
        try:
            target = self.get_target()
            if target is None:
                raise M.MonitorUnavailable("not connected to a Frame")
            session = self.session_factory(target.frame, lambda s, g=gen: self._on_sample(g, s),
                                           lambda why, g=gen: self._on_end(g, why))
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                if gen != self._gen:
                    return
                self._connecting = False
                self._set_state("error", exc)
                note = self._targets()
            self._notify(note)
            return
        with self._lock:
            self._connecting = False
            modules, interval = merge(self._subs.values())
            if gen != self._gen or self.closed or modules is None:  # nobody left while connecting
                stale = True
            else:
                stale = False
                self.session = session
                self.static = getattr(session, "static", {}) or {}
                self._applied = (ALL, M.DEFAULT_INTERVAL)  # the agent's defaults
                self._configure(session, modules, interval)
                if self._filter != "game":
                    session.set_filter(self._filter)
                if self._stream_paused:
                    session.pause(True)
                self._set_state("live", None)
                note = self._targets()
        if stale:
            session.close()
            return
        self._notify(note)

    def _on_sample(self, gen: int, sample: dict) -> None:
        if gen != self._gen:
            return
        for sub in [s for s in list(self._subs.values()) if not s.paused]:
            try:
                sub.on_sample(sample)
            except Exception:  # noqa: BLE001 - one subscriber's bug must not starve the others
                applog.log.exception("monitor hub: subscriber %s failed", sub.name)

    def _on_end(self, gen: int, why: str) -> None:
        with self._lock:
            if gen != self._gen or self.closed:
                return
            self.session = None
            if merge(self._subs.values())[0] is None:
                return
            self._set_state("lost", why)
            note = self._targets()
            self._retry = self._timer(self.retry_seconds, lambda: self._retry_now(gen))
        self._notify(note)

    def _retry_now(self, gen: int) -> None:
        with self._lock:
            if gen != self._gen or self.closed:
                return
            self._retry = None
            note = self._reconcile()
        self._after(note)

    def _cancel_retry(self) -> None:
        if self._retry is not None:
            try:
                self._retry.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._retry = None

    def _stop(self) -> None:
        """No active subscriber: end the stream (and anything pending). Lock held."""
        self._gen += 1
        self._connecting = False
        self._cancel_retry()
        session, self.session = self.session, None
        self.state, self.detail = "idle", None
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

    def _set_state(self, state: str, detail) -> None:
        self.state, self.detail = state, detail

    def _targets(self) -> list:
        return [(s, self.state, self.detail) for s in self._subs.values() if not s.paused and s.on_state]

    def _tell(self, name: str) -> None:
        """Bring one (new or resumed) subscriber up to date with the stream's state."""
        sub = self._subs.get(name)
        if sub and sub.on_state and not sub.paused and self.state != "idle":
            self._notify([(sub, self.state, self.detail)])

    def _after(self, note: list, name: str | None = None) -> None:
        """Outside the lock: send state notifications (or bring `name` up to date), then start a queued connect."""
        if note:
            self._notify(note)
        elif name is not None:
            self._tell(name)
        with self._lock:
            gen, self._pending = self._pending, None
        if gen is not None:
            self._spawn(lambda: self._connect(gen))

    @staticmethod
    def _notify(note: list) -> None:
        for sub, state, detail in note:
            try:
                sub.on_state(sub.name, state, detail)
            except Exception:  # noqa: BLE001
                applog.log.exception("monitor hub: state handler of %s failed", sub.name)
