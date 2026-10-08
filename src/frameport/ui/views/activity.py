"""Activity panel (right side): running, queued and finished background jobs with steps, checks and the log."""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import flet as ft

from ...i18n import tr
from .. import components as C
from .. import theme as T
from ..jobs import Job

if TYPE_CHECKING:
    from ..app import FramePortApp

CHECK_ICON: dict = {}
STATE_STYLE: dict[str, tuple] = {}


def _fill_styles() -> None:
    """(Re)fill the maps with the active theme's colours, in place."""
    CHECK_ICON.update({True: (ft.Icons.CHECK_CIRCLE_ROUNDED, T.OK), False: (ft.Icons.CANCEL_ROUNDED, T.ERROR),
                       None: (ft.Icons.WARNING_AMBER_ROUNDED, T.WARN)})
    STATE_STYLE.update({"queued": (ft.Icons.SCHEDULE_ROUNDED, T.TEXT_3, tr("Waiting")),
                        "running": (ft.Icons.SYNC_ROUNDED, T.ACCENT, tr("Working")),
                        "done": (ft.Icons.CHECK_CIRCLE_ROUNDED, T.OK, tr("Done")),
                        "failed": (ft.Icons.ERROR_ROUNDED, T.ERROR, tr("Failed")),
                        "cancelled": (ft.Icons.DO_NOT_DISTURB_ON_ROUNDED, T.TEXT_3, tr("Canceled"))})


T.on_change(_fill_styles)


def _dur(job: Job) -> str:
    if not job.started:
        return ""
    s = int((job.finished or time.time()) - job.started)
    return f"{s // 60}:{s % 60:02d}"



def _progress_text(job: Job) -> str:
    """Speed first: the line is ellipsized, and the file name matters least."""
    return " · ".join(x for x in (job.speed, job.message) if x)

class ActivityPanel:
    def __init__(self, app: FramePortApp):
        self.app = app
        self.expanded: set[int] = set()
        self.logs: set[int] = set()
        self._tiles: dict[int, tuple[tuple, ft.Control]] = {}  # job id -> (state key, tile)
        self._live: dict[int, tuple] = {}  # running job id -> (progress bar, message, stage text, log text or None)
        # job id -> (log text, its scrolling column): kept across tile rebuilds so reading the log doesn't jump
        self._logviews: dict[int, tuple[ft.Text, ft.Column]] = {}
        self._lock = threading.Lock()  # refresh() runs from job threads and the UI thread
        # the running job: always visible above the list, scrolling on its own when it's taller than its share
        self.pinned = ft.Column(spacing=T.S3, scroll=ft.ScrollMode.AUTO)
        self.pinned_box = ft.Container(self.pinned, expand=3, visible=False)
        self.list = ft.Column(spacing=T.S3, scroll=ft.ScrollMode.AUTO, expand=True)
        self.root = ft.Container(
            ft.Column([
                ft.Row([C.h2(tr("Activity")), ft.Container(expand=True),
                        C.ghost(tr("Clear finished"), on_click=lambda e: (app.jobs.clear_finished(), self.refresh())),
                        C.icon_btn(ft.Icons.CLOSE_ROUNDED, tr("Close"), lambda e: app.show_activity(False))]),
                self.pinned_box,
                ft.Container(self.list, expand=2),
            ], spacing=T.S3, expand=True),
            width=0, bgcolor=T.SIDEBAR, padding=ft.Padding(T.S4, T.S4, T.S4, T.S4),
            border=ft.Border(left=ft.BorderSide(1, T.BORDER)),
            animate_size=ft.Animation(180, ft.AnimationCurve.EASE_OUT),
            clip_behavior=ft.ClipBehavior.HARD_EDGE)

    @property
    def open(self) -> bool:
        return bool(self.root.width)

    def set_open(self, on: bool):
        self.root.width = 400 if on else 0
        if on:
            self.refresh(update=False)

    def refresh(self, update: bool = True):
        if not self.open:
            return
        with self._lock:
            self._refresh(update)

    def _refresh(self, update: bool):
        jobs = self.app.jobs.recent(20)
        tiles, pinned = [], []
        queued = [j for j in jobs if j.state == "queued"]
        rebuilt, live = False, []
        for j in jobs:
            running = j.state == "running"
            # a running job's tile is rebuilt only when its structure changes (new stage/check, log shown); progress
            # and new log lines go to its live controls — rebuilding on every tick swallowed clicks on Cancel
            key = (j.state, len(j.stages), len(j.checks), j.id in self.expanded, j.id in self.logs) if running else \
                (j.version, j.id in self.expanded, j.id in self.logs)
            cached = self._tiles.get(j.id)
            if not cached or cached[0] != key:  # unchanged tiles are reused, so text selections survive refreshes
                cached = (key, self.tile(j))
                self._tiles[j.id] = cached
                rebuilt = True
            elif running and j.id in self._live:
                bar, msg, meta, log = self._live[j.id]
                bar.value = j.fraction
                msg.value = _progress_text(j)
                meta.value = j.stage or ""
                live += [bar, msg, meta]
                if log is not None and len(j.log) != getattr(log, "_lines", -1):  # only when it grew
                    log.value = "\n".join(j.log[-400:])
                    log._lines = len(j.log)
                    live.append(log)
            if running:
                pinned.append(cached[1])
            else:
                if j is (queued[0] if queued else None):
                    tiles.append(self._queue_header(queued))
                tiles.append(cached[1])
        layout = (tuple(j.id for j in jobs), self.app.jobs.paused)
        if not rebuilt and layout == getattr(self, "_layout", None):
            # nothing but progress changed (several times a second during an upload): update only those controls;
            # comparing the whole panel each time (long queue, open log) made the app sluggish
            if update and live:
                C.update(*live)
            return
        self._layout = layout
        self._tiles = {j.id: self._tiles[j.id] for j in jobs}
        self._logviews = {jid: v for jid, v in self._logviews.items() if jid in self._tiles}
        if self.app.jobs.paused == "frame":
            pinned.insert(0, self._paused_notice())
        self.pinned.controls = pinned
        self.pinned_box.visible = bool(pinned)
        if not tiles and not pinned:
            tiles = [ft.Container(C.body(tr("Nothing running. Installs, launch tests and downloads show up here."),
                                         text_align=ft.TextAlign.CENTER), padding=T.S6, alignment=ft.Alignment.CENTER)]
        self.list.controls = tiles
        if update:
            C.update(self.root)

    def _paused_notice(self) -> ft.Control:
        return C.callout(ft.Column([
            C.body(tr("Waiting for your Frame"), T.TEXT, weight=ft.FontWeight.W_600),
            C.body(tr("It can't be reached (asleep, turned off or out of Wi-Fi). Wake it or turn it on: FramePort "
                      "continues by itself, and uploads pick up where they stopped."), T.TEXT_2),
            C.secondary(tr("Try now"), ft.Icons.REFRESH_ROUNDED,
                        lambda e: self.app.run_bg(lambda: self.app.retry_frame())),
        ], spacing=T.S2, horizontal_alignment=ft.CrossAxisAlignment.START), "warn", ft.Icons.WIFI_OFF_ROUNDED)

    def _queue_header(self, queued: list[Job]) -> ft.Control:
        def cancel_all(e):
            for j in list(queued):
                self.app.jobs.cancel(j)
            self.refresh()
        return ft.Row([C.meta(tr("Waiting ({n})").format(n=len(queued)), T.TEXT_2), ft.Container(expand=True),
                       C.ghost(tr("Cancel all"), ft.Icons.CLOSE_ROUNDED, cancel_all)])

    def tile(self, job: Job) -> ft.Control:
        if job.state == "queued":  # compact: a long queue must not push everything else out of view
            return ft.Container(ft.Row([
                ft.Icon(ft.Icons.SCHEDULE_ROUNDED, color=T.TEXT_3, size=T.px(16)),
                C.body(job.title, T.TEXT_2, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                C.icon_btn(ft.Icons.CLOSE_ROUNDED, tr("Cancel"), lambda e: self.app.jobs.cancel(job)),
            ], spacing=T.S2, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                padding=ft.Padding(T.S3, T.px(2), T.px(4), T.px(2)), border_radius=T.RADIUS_SM, bgcolor=T.SURFACE)
        icon, color, word = STATE_STYLE[job.state]
        if job.state == "done" and (job.summary or {}).get("verdict") == "fail":
            icon, color, word = ft.Icons.WARNING_AMBER_ROUNDED, T.WARN, tr("Installed · launch test failed")
        running = job.state == "running"
        expanded = running or job.id in self.expanded
        meta = C.meta(job.stage if running and job.stage else
                      (job.error or word) + (f" · {_dur(job)}" if job.finished else ""),
                      T.ERROR if job.state == "failed" else T.TEXT_2, max_lines=2)
        head = ft.Row([
            C.spinner() if running
            else ft.Icon(icon, color=color, size=T.px(20)),
            ft.Column([C.body(job.title, T.TEXT, weight=ft.FontWeight.W_600, max_lines=2,
                              overflow=ft.TextOverflow.ELLIPSIS), meta],
                      spacing=T.px(2), expand=True),
            *([C.icon_btn(ft.Icons.CLOSE_ROUNDED, tr("Cancel"), lambda e: self.app.jobs.cancel(job))]
              if job.active else []),
        ], vertical_alignment=ft.CrossAxisAlignment.START, spacing=T.S3)
        parts: list[ft.Control] = [head]
        if running:
            bar = C.progress_bar(job.fraction)
            msg = C.meta(_progress_text(job), max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
            parts += [bar, msg]
            self._live[job.id] = (bar, msg, meta, None)
        if expanded:
            if job.stages:
                parts.append(ft.Column([
                    ft.Row([ft.Icon(ft.Icons.CHECK_ROUNDED if (i < len(job.stages) - 1 or not running)
                                    else ft.Icons.ARROW_RIGHT_ROUNDED, size=T.px(14),
                                    color=T.OK if (i < len(job.stages) - 1 or job.state == "done") else T.ACCENT),
                            C.meta(s, T.TEXT_2 if i < len(job.stages) - 1 else T.TEXT)], spacing=T.px(6))
                    for i, s in enumerate(job.stages[-8:])], spacing=T.px(2)))
            if job.checks:
                parts.append(ft.Column([
                    ft.Row([ft.Icon(CHECK_ICON[c["ok"]][0], color=CHECK_ICON[c["ok"]][1], size=T.px(14)),
                            ft.Text(f"{c['name']}" + (f" — {c['detail']}" if c.get("detail") else ""), size=T.T_META,
                                    color=T.TEXT_2, expand=True, selectable=True)],
                           spacing=T.px(6), vertical_alignment=ft.CrossAxisAlignment.START)
                    for c in job.checks[-40:]], spacing=T.px(3)))
            extra = self.app.job_followups(job)
            if extra:
                parts.append(ft.Row(extra, spacing=T.S2, wrap=True))
            show_log = job.id in self.logs
            parts.append(ft.Row([
                C.ghost(tr("Hide log") if show_log else tr("Show log"), ft.Icons.TERMINAL_ROUNDED,
                        lambda e: self._toggle(self.logs, job.id)),
                C.ghost(tr("Copy log"), ft.Icons.CONTENT_COPY_ROUNDED, lambda e: self.app.copy(self.job_text(job))),
                *([C.ghost(tr("Full launch log"), ft.Icons.DESCRIPTION_ROUNDED,
                           lambda e: self.app.show_log_file(job.log_path, job.title))] if job.log_path else []),
            ], spacing=0, wrap=True))
            if show_log:
                log, col = self._log_view(job)
                log.value = "\n".join(job.log[-400:])
                if running:
                    self._live[job.id] = (*self._live[job.id][:3], log)
                else:
                    col.auto_scroll = False
                parts.append(ft.Container(col, bgcolor=T.BG, border_radius=T.RADIUS_SM, padding=T.S2,
                                          height=T.px(240)))
        return ft.Container(ft.Column(parts, spacing=T.S2), bgcolor=T.SURFACE, border_radius=T.RADIUS,
                            border=ft.Border.all(1, T.ACCENT if running else T.BORDER), padding=T.S3,
                            on_click=None if running else lambda e: self._toggle(self.expanded, job.id),
                            ink=not running)

    def _log_view(self, job: Job) -> tuple[ft.Text, ft.Column]:
        """The job's log view, the same one for the job's whole life: rebuilding it on every new stage or check reset
        its scroll position. It follows new lines only while it's scrolled to the bottom."""
        view = self._logviews.get(job.id)
        if view is None:
            log = ft.Text("", size=T.px(11), font_family="monospace", color=T.TEXT_2, selectable=True)
            col = ft.Column([log], scroll=ft.ScrollMode.AUTO, auto_scroll=True, scroll_interval=100)

            def follow(e, col=col):
                at_bottom = e.pixels >= (e.max_scroll_extent or 0) - 24
                if col.auto_scroll != at_bottom:
                    col.auto_scroll = at_bottom  # scrolled up to read: stop following; back at the bottom: follow
                    C.update(col)
            col.on_scroll = follow
            view = self._logviews[job.id] = (log, col)
        return view

    @staticmethod
    def job_text(job: Job) -> str:
        return job.text()

    def _toggle(self, s: set[int], jid: int):
        s.symmetric_difference_update({jid})
        self.refresh()
