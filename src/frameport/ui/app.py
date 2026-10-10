"""FramePort desktop GUI (Flet). The shell: sidebar (navigation, Frame connection, activity), routing, background
jobs and the actions views call. Views live in ui/views/, styling in ui/theme.py + ui/components.py.

    Library → Game (one click: patch/check → install → add to Steam → launch test) · Frame · Settings · Welcome

The UI only calls `frameport.pipeline`, the targets and the toolchain; long work runs as queued background jobs
(ui/jobs.py) shown in the activity panel.
"""
from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

import flet as ft

from .. import pipeline
from ..core import applog, library
from ..errors import explain
from ..frame.connection import NOT_IN_LIBRARY, AgentFailed
from ..i18n import fmt_size, tr, tr_n
from ..recommend import catalog
from . import components as C
from . import jobs as jobs_module
from . import theme as T
from .components import install_state  # noqa: F401  (re-exported: tests and older callers import it from here)
from .jobs import Job

NAV = [("library", tr("Library"), ft.Icons.GRID_VIEW_ROUNDED),
       ("frame", tr("Steam Frame"), ft.Icons.VIEW_IN_AR_ROUNDED),
       ("files", tr("Files"), ft.Icons.FOLDER_OPEN_ROUNDED),
       ("screenshots", tr("Screenshots"), ft.Icons.PHOTO_LIBRARY_OUTLINED),
       ("live", tr("Live view"), ft.Icons.CAST_ROUNDED),
       ("keyboard", tr("Type on Frame"), ft.Icons.KEYBOARD_ROUNDED),
       ("monitor", tr("Monitor"), ft.Icons.MONITOR_HEART_OUTLINED),
       ("settings", tr("Settings"), ft.Icons.TUNE_ROUNDED)]
POLL_SECONDS = 30
_link_state = {"owner": None, "thread": None, "closing": False}  # install links go to the newest window session


def _stop_link_watch() -> None:
    """The window is closing: no more heartbeats, so a link clicked now starts FramePort again (a heartbeat left
    behind made the handler script think the closed window would take it)."""
    from .. import urlhandler

    _link_state["closing"] = True
    urlhandler.stop_heartbeat()


def _watch_links() -> None:
    """Heartbeat for the link handler scripts (FramePort runs) + hand queued install links to the window."""
    from .. import urlhandler

    while not _link_state["closing"]:
        urlhandler.heartbeat()
        app = _link_state["owner"]
        if app is not None:
            for link in urlhandler.take_links():
                applog.log.info("install link received")
                app.page.run_thread(app.open_install_link, link, True)
        time.sleep(1)



def window_geometry(saved: dict | None, scale: float) -> dict:
    """Window properties for the start from the saved `ui.window` setting: the size (first start: 16:10 sized for the
    UI scale), the position when it is plausible (a window left on a monitor that is gone would open off screen; far
    off values are ignored, the system then places it) and maximized."""
    saved = saved or {}
    w, h = saved.get("size") or (round(1280 * scale), round(780 * scale))
    out = {"width": max(int(w), 1000), "height": max(int(h), 680)}
    pos = saved.get("pos")
    if isinstance(pos, (list, tuple)) and len(pos) == 2 and all(isinstance(v, (int, float)) for v in pos) \
            and -16000 < pos[0] < 16000 and -8 <= pos[1] < 9000:
        out["left"], out["top"] = int(pos[0]), int(pos[1])
    if saved.get("maximized"):
        out["maximized"] = True
    return out

class FramePortApp:
    def __init__(self, page: ft.Page):
        from .views.activity import ActivityPanel
        from .views.library import load_filters

        self.page = page
        self.target = None  # FrameLeptonTarget when connected
        self.frame_info: dict | None = None
        self.frame_state = "none"  # none | connecting | connected | offline
        self.pairing = None
        self.route: tuple = ("library",)
        self.lib_filters = load_filters()
        self.search_field: ft.TextField | None = None
        self.welcome_started = False
        self._pc_cache: tuple[float, dict] | None = None
        self.library_view = None  # created once (views/library.LibraryView), re-mounted on every visit
        self.files_view = None  # likewise (views/files.FilesView): keeps the location/folder between visits
        self.screenshots_view = None  # likewise (views/screenshots.ScreenshotsView): keeps the game filter
        self.live_view = None  # likewise (views/live.LiveView): owns the running stream, which outlives the tab
        self.keyboard_view = None  # likewise (views/keyboard.KeyboardView): its keyboard exists only while it's shown
        self.monitor_view = None  # likewise (views/monitor.MonitorView): its stream runs only while it's shown
        self._typing_on_frame = False
        self.exe_queue: list[str] = []  # games whose executable the user should confirm (after a scan)
        self._failures: list[Job] = []  # failed installs/tests, shown together when the queue is done
        self.jobs = jobs_module.shared()  # one queue per process, shared by every window session
        self.jobs.subscribe(self._on_job)
        self._handled_jobs: set[int] = set()  # finished jobs this session has reacted to (pop-ups, refreshes)
        page.on_close = lambda e: (self.jobs.unsubscribe(self._on_job),  # session gone: stop drawing into it
                                   self.stop_live(),  # and stop a live view (it would keep the Frame encoding)
                                   self.stop_keyboard(), self.stop_monitor(),
                                   _link_state.update(owner=None) if _link_state["owner"] is self else None)
        from .updater import Updater

        self.updater = Updater(self)  # new FramePort releases (sidebar card, Library bar, one-click update)

        page.title = tr("FramePort")
        T.apply(page)
        page.padding = 0
        page.window.min_width, page.window.min_height = 1000, 680
        if not page.web:
            # 16:10 sized for the UI scale (the first start used to open too narrow at 125 %: toolbars were cut off);
            # later starts reopen where the window was left: size, position (GitHub #32: always top left on Windows),
            # maximized
            for key, value in window_geometry(library.setting("ui.window"), T.SCALE).items():
                setattr(page.window, key, value)
            page.window.on_event = self._on_window_event
        page.on_keyboard_event = self._on_key
        if page.web:  # the library's right-click menu; otherwise the browser shows its own
            try:
                page.run_task(ft.BrowserContextMenu().disable)
            except Exception:  # noqa: BLE001
                pass
        self.body = ft.Container(expand=True, padding=ft.Padding(T.S6, T.S5, T.S5, 0))
        self.nav_col = ft.Column(spacing=T.px(2))
        self.conn_card = ft.Container()
        self.power_row = ft.Container(visible=False)  # the Frame's power button (sleep / restart / shut down)
        self.activity_card = ft.Container()
        self.activity = ActivityPanel(self)
        sidebar = ft.Container(ft.Column([
            ft.Container(ft.Row([
                ft.Container(ft.Icon(ft.Icons.VIEW_IN_AR_ROUNDED, size=T.px(18), color=T.ON_ACCENT),
                             width=T.px(32), height=T.px(32), border_radius=T.px(9), bgcolor=T.ACCENT,
                             alignment=ft.Alignment.CENTER),
                ft.Text(tr("FramePort"), size=T.px(17), weight=ft.FontWeight.W_800, color=T.TEXT)], spacing=T.S3),
                padding=ft.Padding(T.S2, T.S2, 0, T.S5)),
            self.nav_col,
            ft.Container(expand=True),
            self.updater.card,
            self.activity_card,
            self.conn_card,
            self.power_row,  # under the Frame card (owner's choice)
        ], spacing=T.S2), width=T.px(236), bgcolor=T.SIDEBAR, padding=T.S4,
            border=ft.Border(right=ft.BorderSide(1, T.BORDER)))
        page.add(ft.Row([sidebar, self.body, self.activity.root], expand=True, spacing=0,
                        vertical_alignment=ft.CrossAxisAlignment.STRETCH))
        from .views.welcome import needed

        self.go("welcome" if needed() else "library")
        threading.Thread(target=self._startup, daemon=True).start()
        threading.Thread(target=self._poll, daemon=True).start()
        threading.Thread(target=self._backfill_covers, daemon=True).start()
        self.updater.start()
        self.run_bg(self._refresh_catalog)  # confirmed game configs from GitHub main (no release needed)
        _link_state["owner"] = self
        if _link_state["thread"] is None:
            import atexit

            from .. import urlhandler

            _link_state["thread"] = threading.Thread(target=_watch_links, daemon=True)
            _link_state["thread"].start()
            atexit.register(_stop_link_watch)
            self.run_bg(urlhandler.apply_setting)  # framedrop:// + frameport:// → FramePort (Settings → General)

    # ================================================================== shell
    def _on_window_event(self, e) -> None:
        """Remember the window's size and position (once a resize/move ends; not while maximized or full screen) and
        whether it is maximized."""
        win, change = self.page.window, None
        if e.type == ft.WindowEventType.CLOSE:
            _stop_link_watch()  # also when maximized (that returns below)
        if e.type in (ft.WindowEventType.MAXIMIZE, ft.WindowEventType.UNMAXIMIZE):
            change = {"maximized": e.type == ft.WindowEventType.MAXIMIZE}
        elif win.maximized or win.full_screen:
            return
        elif e.type == ft.WindowEventType.RESIZED and win.width and win.height:
            change = {"size": [round(win.width), round(win.height)]}
            if win.left is not None and win.top is not None:  # resizing from the left/top edge moves it too
                change["pos"] = [round(win.left), round(win.top)]
        elif e.type in (ft.WindowEventType.MOVED, ft.WindowEventType.CLOSE) and win.left is not None and \
                win.top is not None:
            change = {"pos": [round(win.left), round(win.top)]}
            if e.type == ft.WindowEventType.CLOSE:  # the app is going away: write it now
                library.update_setting("ui.window", lambda v: {**(v or {}), **change}, {})
                return
        if change:
            self.page.run_thread(library.update_setting, "ui.window", lambda v: {**(v or {}), **change}, {})

    def top_bar(self, heading: str, subtitle: str = "", actions: list[ft.Control] | None = None) -> ft.Control:
        heads = [C.title(heading), C.body(subtitle)] if subtitle else [C.title(heading)]
        if not actions:
            return ft.Column(heads, spacing=T.px(2))
        # the actions wrap (right-aligned) in a narrow window instead of squeezing the heading away
        return ft.Row([ft.Column(heads, spacing=T.px(2), expand=1),
                       ft.Container(ft.Row(actions, spacing=T.S3, run_spacing=T.S2, wrap=True,
                                           alignment=ft.MainAxisAlignment.END,
                                           vertical_alignment=ft.CrossAxisAlignment.CENTER), expand=2)],
                      vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=T.S3)

    def _build_sidebar_controls(self) -> None:
        """The sidebar's controls are created once and only their properties change: rebuilding them while a job
        reports progress (several times a second) swallowed clicks — the pressed control was gone on release."""
        nav = {}
        for key, label, icon in NAV:
            ic = ft.Icon(icon, size=T.px(20), color=T.TEXT_2)
            tx = ft.Text(label, size=T.px(14), weight=ft.FontWeight.W_500, color=T.TEXT_2, expand=True)
            badge = C.dot(T.OK, 7)
            badge.visible = False
            box = ft.Container(ft.Row([ic, tx, badge], spacing=T.S3),
                               padding=ft.Padding(T.S3, T.px(10), T.S3, T.px(10)), border_radius=T.RADIUS_SM, ink=True,
                               on_click=lambda e, k=key: self.go(k))
            nav[key] = (box, ic, tx, badge)
        self.nav_col.controls = [v[0] for v in nav.values()]
        # activity card: a "running" layout and an "idle" layout, switched by visibility
        self._act_title = ft.Text("", size=T.px(12), weight=ft.FontWeight.W_600, color=T.TEXT, expand=True, max_lines=1,
                                  overflow=ft.TextOverflow.ELLIPSIS)
        self._act_pct = C.meta("")
        self._act_bar = C.progress_bar(None)
        self._act_stage = C.meta("", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self._act_running = ft.Column([
            ft.Row([ft.ProgressRing(width=T.px(14), height=T.px(14), stroke_width=T.px(2), color=T.ACCENT),
                    self._act_title, self._act_pct], spacing=T.S2), self._act_bar, self._act_stage],
            spacing=T.px(6), visible=False)
        self._act_idle_text = C.meta(tr("No activity"), expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self._act_idle = ft.Row([ft.Icon(ft.Icons.HISTORY_ROUNDED, size=T.px(16), color=T.TEXT_3), self._act_idle_text],
                                spacing=T.S2)
        self._act_box = ft.Container(ft.Column([self._act_running, self._act_idle], spacing=0), padding=T.S3,
                                     border_radius=T.RADIUS_SM, border=ft.Border.all(1, T.BORDER), ink=True,
                                     tooltip=tr("Activity"),
                                     on_click=lambda e: self.show_activity(not self.activity.open))
        self.activity_card.content = self._act_box
        # connection card
        self._conn_dot = C.dot(T.TEXT_3, 9)
        self._conn_name = C.body(tr("Steam Frame"), T.TEXT, weight=ft.FontWeight.W_600, max_lines=1,
                                 overflow=ft.TextOverflow.ELLIPSIS)
        self._conn_line = C.meta(tr("Not set up"))
        # battery: icon + "7 %" next to the name, like a phone's status bar (a text suffix wrapped in the sidebar)
        self._conn_bat_icon = ft.Icon(ft.Icons.BATTERY_FULL_ROUNDED, size=T.px(15), color=T.TEXT_2)
        self._conn_bat_text = C.meta("")
        self._conn_bat = ft.Container(ft.Row([self._conn_bat_icon, self._conn_bat_text], spacing=T.px(2), tight=True,
                                             vertical_alignment=ft.CrossAxisAlignment.CENTER), visible=False)
        self._conn_extra = ft.Container(C.meta(""), visible=False, tooltip=C.tip(C.HELP["frame_summary"]))
        self.conn_card.content = ft.Container(ft.Row([
            ft.Stack([ft.Icon(ft.Icons.VIEW_IN_AR_ROUNDED, size=T.px(22), color=T.TEXT_2),
                      ft.Container(self._conn_dot, right=0, bottom=0)], width=T.px(24), height=T.px(24)),
            ft.Column([ft.Row([ft.Container(self._conn_name, expand=True), self._conn_bat],
                              spacing=T.px(6), vertical_alignment=ft.CrossAxisAlignment.CENTER),
                       self._conn_line, self._conn_extra], spacing=1, expand=True),
        ], spacing=T.S3), padding=T.S3, border_radius=T.RADIUS_SM, bgcolor=T.SURFACE, ink=True,
            border=ft.Border.all(1, T.BORDER), on_click=lambda e: self.go("frame"))
        # the Frame's power: three equal buttons in one bar styled like the cards around it (icon over a label)
        def power_button(action: str, label: str, icon, tip: str) -> ft.Control:
            return ft.Container(
                ft.Column([ft.Icon(icon, size=T.px(17), color=T.TEXT_2),
                           ft.Text(label, size=T.T_META, color=T.TEXT_2, max_lines=1, no_wrap=True)],
                          spacing=T.px(2), tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                expand=True, alignment=ft.Alignment.CENTER, padding=ft.Padding(0, T.px(7), 0, T.px(6)),
                border_radius=T.RADIUS_SM, ink=True, tooltip=C.tip(tip),
                on_click=lambda e: self.frame_power(action))
        divider = ft.Container(width=1, height=T.px(26), bgcolor=T.BORDER)
        self.power_row.content = ft.Container(ft.Row([
            power_button("sleep", tr("Sleep"), ft.Icons.BEDTIME_OUTLINED, tr("Put the Frame to sleep")),
            divider,
            power_button("restart", tr("Restart"), ft.Icons.RESTART_ALT_ROUNDED, tr("Restart the Frame")),
            ft.Container(width=1, height=T.px(26), bgcolor=T.BORDER),
            power_button("shutdown", tr("Shut down"), ft.Icons.POWER_SETTINGS_NEW_ROUNDED, tr("Turn the Frame off")),
        ], spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=T.SURFACE, border=ft.Border.all(1, T.BORDER), border_radius=T.RADIUS_SM, padding=T.px(2))
        self._nav = nav  # last: _refresh_sidebar (also called from job threads) treats it as "all built"

    def _refresh_sidebar(self, update: bool = True) -> None:
        if not hasattr(self, "_nav"):
            self._build_sidebar_controls()
        for key, (box, ic, tx, badge) in self._nav.items():
            on = self.route[0] == key or (key == "library" and self.route[0] == "game")
            box.bgcolor = T.ACCENT_SOFT if on else None
            ic.color = T.ACCENT if on else T.TEXT_2
            tx.color = T.TEXT if on else T.TEXT_2
            tx.weight = ft.FontWeight.W_600 if on else ft.FontWeight.W_500
            badge.visible = key == "frame" and self.frame_state == "connected"
        cur = self.jobs.current()
        queued = len(self.jobs.pending())
        self._act_running.visible, self._act_idle.visible = bool(cur), not cur
        self._act_box.bgcolor = T.SURFACE if cur else None
        self._act_box.border = ft.Border.all(1, T.ACCENT if cur else T.BORDER)
        if cur:
            self._act_title.value = cur.title
            self._act_pct.value = (f"{cur.fraction:.0%}" if cur.fraction is not None else "") + \
                (f" · {cur.speed.split(' · ')[0]}" if cur.speed else "")
            self._act_bar.value = cur.fraction
            more = tr(" · {queued} more queued").format(queued=queued) if queued else ""
            self._act_stage.value = (cur.stage or tr("Starting…")) + more
        elif self.jobs.paused == "frame":
            self._act_idle_text.value = tr("Paused: waiting for your Frame")
            self._act_idle_text.color = T.WARN
        elif self.jobs.paused == "battery":
            self._act_idle_text.value = tr("Paused: Frame battery low, plug it in")
            self._act_idle_text.color = T.WARN
        else:
            recent = next((j for j in self.jobs.recent(1)), None)
            self._act_idle_text.value = tr("No activity") if not recent else \
                f"{recent.title} · " + {"done": tr("done"), "failed": tr("failed"),
                                        "cancelled": tr("cancelled")}.get(recent.state, "")
            self._act_idle_text.color = T.ERROR if recent and recent.state == "failed" else T.TEXT_3
        st = self.frame_state
        color = {"connected": T.OK, "connecting": T.WARN, "offline": T.ERROR}.get(st, T.TEXT_3)
        self._conn_dot.bgcolor = color
        self._conn_name.value = (self.target.label if self.target else None) or self._saved_name() or tr("Steam Frame")
        self._conn_line.value = {"connected": tr("Connected"), "connecting": tr("Connecting…"),
                                 "offline": tr("Offline")}.get(
            st, "Not set up")
        self._conn_line.color = color
        bat = (self.frame_info or {}).get("battery") if st == "connected" else None
        self._conn_bat.visible = bool(bat)
        self.power_row.visible = st == "connected"
        if bat:
            from .battery import charging, icon, low

            warn = low(bat)
            self._conn_bat_icon.icon = getattr(ft.Icons, icon(bat))
            self._conn_bat_icon.color = T.WARN if warn else T.OK if charging(bat) else T.TEXT_2
            self._conn_bat_text.value = f"{bat.get('percent', 0)} %"
            self._conn_bat_text.color = T.WARN if warn else T.TEXT_2
            self._conn_bat.tooltip = tr("Frame battery: charging") if charging(bat) else \
                tr("Frame battery: not charging")
        if st == "connected" and self.frame_info:
            pr = (self.frame_info.get("proton") or {}).get("ready")
            quest = tr("Quest ✓") if self.frame_info.get("lepton") else tr("Quest ✗")
            self._conn_extra.content.value = quest + "   " + \
                (tr("PC VR ✓") if pr else tr("PC VR —"))
            self._conn_extra.visible = True
        else:
            self._conn_extra.visible = False
        if update:
            C.update(self.nav_col, self.activity_card, self.power_row, self.conn_card)

    def _saved_name(self) -> str | None:
        from ..frame.connection import saved_targets

        s = saved_targets()
        return s[0].label if s else None

    def go(self, route: str, *args) -> None:
        # a redraw of the same game page (a switch in its patch list, Customize, ...) keeps the scroll position;
        # another page or game starts at the top
        if not (route == "game" and self.route[:1] == ("game",) and self.route[1:2] == args[:1]):
            self.game_scroll = 0.0
        if self.route[0] == "keyboard" and route != "keyboard":
            self.stop_keyboard()  # leaving the tab removes the virtual keyboard from the Frame
        if self.route[0] == "monitor" and route != "monitor":
            self.stop_monitor()  # leaving the tab ends the Frame's monitor stream
        self.route = (route, *args)
        self.render()

    def render(self) -> None:
        from .views.frame import FrameView
        from .views.game import GameView
        from .views.library import LibraryView
        from .views.settings import SettingsView
        from .views.welcome import WelcomeView

        kind = self.route[0]
        try:
            if kind == "library":
                if self.library_view is None:
                    self.library_view = LibraryView(self)
                view = self.library_view.mount()
            elif kind == "game":
                view = GameView(self, *self.route[1:]).build()
            elif kind == "frame":
                view = FrameView(self).build()
            elif kind == "files":
                from .views.files import FilesView

                if self.files_view is None:
                    self.files_view = FilesView(self)
                view = self.files_view.mount(*self.route[1:])
            elif kind == "screenshots":
                from .views.screenshots import ScreenshotsView

                if self.screenshots_view is None:
                    self.screenshots_view = ScreenshotsView(self)
                view = self.screenshots_view.mount(*self.route[1:])
            elif kind == "live":
                from .views.live import LiveView

                if self.live_view is None:
                    self.live_view = LiveView(self)
                view = self.live_view.mount()
            elif kind == "keyboard":
                from .views.keyboard import KeyboardView

                if self.keyboard_view is None:
                    self.keyboard_view = KeyboardView(self)
                view = self.keyboard_view.mount()
            elif kind == "monitor":
                from .views.monitor import MonitorView

                if self.monitor_view is None:
                    self.monitor_view = MonitorView(self)
                view = self.monitor_view.mount()
            elif kind == "settings":
                view = SettingsView(self).build()
            else:
                view = ft.Row([WelcomeView(self).build()], alignment=ft.MainAxisAlignment.CENTER, expand=True,
                              vertical_alignment=ft.CrossAxisAlignment.START)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            view = C.empty_state(ft.Icons.ERROR_OUTLINE_ROUNDED, tr("Something went wrong"), explain(exc),
                                 C.primary(tr("Back to library"), on_click=lambda e: self.go("library")))
        self.body.content = view
        self._refresh_sidebar(update=False)
        self.page.update()
        if kind == "library":
            self.library_view.refresh_async()

    def refresh_view(self) -> None:
        """Bring the visible view up to date after a state change without blocking: the library updates only the
        cards that changed; other views re-render."""
        if self.route[0] == "library" and self.library_view is not None:
            self._refresh_sidebar()
            self.library_view.refresh_async()
        elif self.route[0] in ("game", "frame", "welcome", "settings"):
            self.render()
        elif self.route[0] == "files" and (self.files_view is None or self.files_view.root is None
                                           or self.frame_state != "connected"):
            self.render()  # connected / disconnected: switch between the browser and "connect first"
        elif self.route[0] == "screenshots" and (self.screenshots_view is None or self.screenshots_view.root is None
                                                 or self.frame_state != "connected"):
            self.render()
        elif self.route[0] == "live" and (self.live_view is None or self.live_view.root is None
                                          or self.frame_state != "connected"):
            self.render()
        elif self.route[0] == "keyboard" and (self.keyboard_view is None or self.keyboard_view.root is None
                                              or self.keyboard_view.stopped or self.frame_state != "connected"):
            if self.frame_state != "connected":
                self.stop_keyboard()
            self.render()
        elif self.route[0] == "monitor" and (self.monitor_view is None or self.monitor_view.root is None
                                             or self.monitor_view.stopped or self.frame_state != "connected"):
            if self.frame_state != "connected":
                self.stop_monitor()
            self.render()
        else:
            self._refresh_sidebar()

    def stop_live(self) -> None:
        if self.live_view is not None:
            self.live_view.stop()

    def stop_keyboard(self) -> None:
        if self.keyboard_view is not None:
            self.keyboard_view.stop()

    def stop_monitor(self) -> None:
        if self.monitor_view is not None:
            self.monitor_view.stop()

    def open_game(self, package: str, advanced: bool = False, show_all: bool = False) -> None:
        self.go("game", package, advanced, show_all)

    def navigate(self, index: int, **kw) -> None:  # older callers (scripts)
        routes = [key for key, _, _ in NAV]
        self.go(routes[index] if 0 <= index < len(routes) else "library")

    def show_activity(self, on: bool) -> None:
        self.activity.set_open(on)
        self.page.update()

    def toast(self, message: str, error: bool = False, action: str | None = None, on_action=None) -> None:
        """A temporary notification: closes itself (errors and ones with a button stay a little longer) or with its
        X. persist=False: Flutter otherwise keeps a snack bar with an action open until it's swiped away."""
        self.page.show_dialog(ft.SnackBar(
            ft.Text(message, color=T.TEXT), bgcolor=T.soft(T.ERROR, 0.9) if error else T.SURFACE_3,
            action=action, on_action=on_action, behavior=ft.SnackBarBehavior.FLOATING, width=T.px(520),
            shape=ft.RoundedRectangleBorder(radius=T.RADIUS_SM), duration=8000 if error else 6000 if action else 4000,
            persist=False, show_close_icon=True, close_icon_color=T.TEXT_2))

    def run_bg(self, fn, *args) -> None:
        def wrapper():
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                applog.log.exception("background task failed")
                self.toast(explain(exc), error=True)
        self.page.run_thread(wrapper)

    def copy(self, text: str) -> None:
        try:
            self.page.run_task(ft.Clipboard().set, text)
            self.toast(tr("Copied"))
        except Exception:  # noqa: BLE001
            self.toast(tr("Copy failed; select the text instead"), error=True)

    def _on_key(self, e: ft.KeyboardEvent) -> None:
        if self._typing_on_frame:
            return  # Type on Frame is open: Esc and shortcuts belong to the Frame
        if e.key == "Escape" and self.activity.open:
            self.show_activity(False)
        elif e.key.upper() == "F" and (e.ctrl or e.meta) and self.route[0] == "library" and self.search_field:
            try:
                self.page.run_task(self.search_field.focus)
            except Exception:  # noqa: BLE001
                pass

    # ================================================================== jobs
    def _on_job(self, job: Job | None) -> None:
        self._refresh_sidebar()
        self.activity.refresh()
        self._keep_frame_awake()
        if job and job.state in ("done", "failed", "cancelled") and job.finished and id(job) not in self._handled_jobs:
            self._handled_jobs.add(id(job))
            if job.kind == "app-update" and job.state != "done":
                self.updater.restart = None  # the update didn't get ready: don't quit
            self.updater.on_jobs_changed()  # an update waiting for the queue to empty installs now
            if job.kind in ("install", "test", "uninstall", "tool-frame") and self.target:
                self.refresh_frame(quiet=True)
            if job.kind == "tool-frame" and self.route[0] == "files" and self.files_view is not None:
                # show what an upload added  # re-renders when the Frame's list changed (e.g. after an uninstall)
                self.files_view.load()
            if job.kind == "install" and job.package and job.package in self._records():
                self._record(job.package, to=job.to, state="paused" if job.state == "cancelled" else "failed",
                             error=job.error, stage=job.stage)
            summary = job.summary or {}
            if job.kind in ("install", "test") and (job.state == "failed" or
                                                    (job.state == "done" and summary.get("verdict") == "fail")):
                self._failures.append(job)
            if not any(j.active and j.kind in ("install", "test") for j in self.jobs.jobs) and self._failures:
                failures, self._failures = self._failures, []
                self.page.run_thread(lambda: self.show_failures(failures))
            if job.state == "done":
                msg = job.result if isinstance(job.result, str) else tr("{title}: done").format(title=job.title)
                pkg = job.package
                self.toast(msg, action=tr("Open") if pkg and self.route[:2] != ("game", pkg) else tr("Details"),
                           on_action=(lambda e: self.open_game(pkg)) if pkg and self.route[:2] != ("game", pkg)
                           else (lambda e: self.show_activity(True)))
            elif job.state == "failed":
                msg = tr("{title} failed: {error}").format(title=job.title, error=job.error)
                pkg = job.package
                if job.kind in ("install", "test") and pkg:  # one click to the diagnostics + prefilled issue
                    self.toast(msg, error=True, action=tr("Report a problem"),
                               on_action=lambda e: self.report_problem_dialog(pkg))
                else:
                    self.toast(msg, error=True, action=tr("Details"),
                               on_action=lambda e: self.show_activity(True))
            self.refresh_view()
            if job.kind == "scan" and self.exe_queue:
                self.page.run_thread(self.next_exe_choice)
        elif job and job.state == "running" and job.package and self.route[:2] == ("game", job.package) and \
                job.stage and getattr(job, "_shown_stage", None) != job.stage:
            job._shown_stage = job.stage  # keep the hero's progress button current
            self.render()
        elif job and job.state == "running" and job.package and self.route[0] == "library" and \
                not getattr(job, "_card_marked", 0):
            job._card_marked = 1  # "Working…" badge on the card
            self.refresh_view()

    def submit(self, title: str, run, package: str | None = None, kind: str = "task", open_panel: bool = False,
               to: str = "frame") -> Job:
        # jobs that use the Frame wait for it (queue paused) when it drops off the network, instead of failing
        needs_frame = kind in ("install", "test", "uninstall", "tool-frame") and to == "frame"
        job = self.jobs.submit(Job(title, run, package, kind, to=to, needs_frame=needs_frame))
        if open_panel:
            self.show_activity(True)
        elif self.route[0] == "game":
            self.render()
        else:
            self.refresh_view()
        return job

    def job_followups(self, job: Job) -> list[ft.Control]:
        out = []
        summary = getattr(job, "summary", None)
        if job.kind == "install" and job.state in ("failed", "cancelled") and job.package and \
                job.package in self._records():
            out.append(C.primary(tr("Resume"), ft.Icons.PLAY_ARROW_ROUNDED,
                                 lambda e: self._submit_install(job.package, getattr(job, "to", "frame"))))
        if job.kind in ("install", "test") and job.package and (summary or {}).get("verdict") == "pass":
            from .views.game import should_ask_to_share

            g = library.game(job.package) or {}
            if should_ask_to_share(g, True):
                out.append(C.secondary(tr("Works in the headset? Share…"), ft.Icons.VOLUNTEER_ACTIVISM_OUTLINED,
                                       lambda e: self.share_config_dialog(job.package)))
        if summary and summary.get("suggestions") and job.package:
            sugg = summary["suggestions"]
            label = (tr("Try Proton Experimental and reinstall") if sugg == [pipeline.PROTON_TOOL]
                     else tr("Apply the suggested patches and reinstall"))
            out.append(C.primary(label, ft.Icons.HEALING_ROUNDED,
                                 lambda e: (pipeline.apply_suggestions(job.package, sugg),
                                            self.install(job.package, getattr(job, "to", "frame")))))
        if job.package and job.state != "running" and library.game(job.package):
            out.append(C.ghost(tr("Open game"), ft.Icons.ARROW_FORWARD_ROUNDED, lambda e: self.open_game(job.package)))
        return out

    # ================================================================== game actions
    def pc_installs(self) -> dict:
        from ..targets.pc_revive import local_installs

        if not self._pc_cache or time.time() - self._pc_cache[0] > 2:
            self._pc_cache = (time.time(), local_installs())
        return self._pc_cache[1]

    def _title(self, pkg: str) -> str:
        from .views.library import display_title, twins

        g = library.game(pkg)
        return display_title(g, twins(library.games())) if g else pkg

    def install_options(self, g: dict) -> list[tuple]:
        """[(label, icon, on_click, disabled, tooltip)] — the first is the primary action."""
        pkg = g["package"]
        connected = self.frame_state == "connected"
        st = C.install_state(g, self.frame_info)
        frame_label = {"installed": tr("Reinstall on Frame"),
                       "outdated": tr("Update on Frame")}.get(st, tr("Install on Frame"))
        blocked = (g.get("recipe") or {}).get("status") == "unsupported" and g.get("kind") != "rift"
        if blocked and connected:  # known blocker: still allowed (e.g. to try a fix), after a warning
            frame_label = {"installed": tr("Reinstall anyway"),
                           "outdated": tr("Update anyway")}.get(st, tr("Install anyway"))
            return [(frame_label, ft.Icons.WARNING_AMBER_ROUNDED, lambda e: self.install_blocked(pkg), False,
                     tr("Marked \"Can't run\": ") + ((g.get("recipe") or {}).get("notes") or tr("a known blocker")))]
        frame_opt = (frame_label, ft.Icons.VIEW_IN_AR_ROUNDED, lambda e: self.install(pkg, "frame"), False, None) \
            if connected else (tr("Connect your Frame"), ft.Icons.LINK_ROUNDED, lambda e: self.go("frame"), False,
                               tr("Set up the connection to your Steam Frame first"))
        if g.get("kind") != "rift":
            return [frame_opt]
        from ..core import winhost

        on_pc = pkg in self.pc_installs()
        stale = on_pc and C.pc_outdated(g, self.pc_installs()[pkg])
        pc_label = (tr("Update on this PC") if stale else tr("Reinstall on this PC") if on_pc
                    else tr("Install on this PC"))
        pc_opt = (pc_label,
                  ft.Icons.COMPUTER_ROUNDED, lambda e: self.install(pkg, "pc"), not winhost.available(),
                  tr("The launch settings changed since it was installed: update the Steam shortcut") if stale else
                  None if winhost.available() else tr("Needs Windows (or WSL on Windows)"))
        return [frame_opt, pc_opt] if connected or not winhost.available() else [pc_opt, frame_opt]

    def play_options(self, g: dict) -> list[tuple]:
        """[(label, icon, on_click, disabled, tooltip)] for where the game is installed and can be started now."""
        pkg = g["package"]
        out = []
        if self.frame_state == "connected" and C.install_state(g, self.frame_info) in ("installed", "outdated"):
            out.append((tr("Play on Frame"), ft.Icons.PLAY_ARROW_ROUNDED, lambda e: self.play(pkg, "frame"), False,
                        tr("Starts the game through the Frame's Steam — put the headset on")))
        if g.get("kind") == "rift" and pkg in self.pc_installs() and \
                not ((g.get("analysis") or {}).get("extra") or {}).get("flat"):
            out.append((tr("Play on this PC"), ft.Icons.PLAY_ARROW_ROUNDED, lambda e: self.play(pkg, "pc"), False,
                        tr("Starts the game through Steam on this PC (SteamVR + Revive)")))
        return out

    PLAY_COOLDOWN = 20  # s: a second Play while Steam/Lepton still start the game only gets Steam's AppError_16

    def play(self, pkg: str, to: str = "frame") -> None:
        title = self._title(pkg)
        started = getattr(self, "_play_started", {})
        self._play_started = started
        if time.time() - started.get((pkg, to), 0) < self.PLAY_COOLDOWN:
            self.toast(tr("{title} is already starting — put the headset on").format(title=title))
            return
        started[(pkg, to)] = time.time()

        def work():
            try:
                res = self._target_for(to).launch(pkg) or {}
            except Exception as exc:
                started.pop((pkg, to), None)  # a failed start can be retried right away
                if not isinstance(exc, AgentFailed) or to != "frame" or NOT_IN_LIBRARY not in str(exc):
                    raise
                self._add_then_play(pkg, title)  # e.g. the shortcut step failed during the install
                return
            library.upsert_game(pkg, last_played=time.time())
            steam = res.get("steam") or {}
            if to == "frame" and steam:  # what the Frame's Steam logged about this launch (GitHub #21/#30)
                if res.get("first_try"):
                    applog.log.info("Play %s: Steam refused the library entry %s; devkit entry: %s", pkg,
                                    res["first_try"].get("lines"), res.get("gameid"))
                applog.log.info("Play %s (%s): Steam %s %s", pkg, res.get("via"), steam.get("result"),
                                steam.get("lines"))
            if to == "frame" and res.get("fallback_error"):
                applog.log.info("Play %s: devkit entry failed: %s", pkg, res["fallback_error"])
            if to == "frame" and res.get("first_try") and steam.get("result") != "error":
                # Steam ignored FramePort's library entry: the game was added the way Valve's devkit tool does it
                self.toast(tr("The Frame's Steam didn't accept {title}'s library entry, so FramePort added it as "
                              "\"Devkit Game: …\" instead. Starting it now — put the headset on.").format(title=title))
            elif to == "frame" and steam.get("result") == "error" and steam.get("code") == 16:
                # AppError_16: "WaitingPrevProcess" - the game is still starting or running (GitHub #41)
                self.toast(tr("{title} is already running on the Frame — put the headset on").format(title=title))
            elif to == "frame" and steam.get("result") == "error":  # 9 = "Game configuration unavailable"
                started.pop((pkg, to), None)
                self.toast(tr("The Frame's Steam couldn't start {title} (Steam error {code}). Please send a problem "
                              "report so we can see why.").format(title=title, code=steam.get("code")), error=True,
                           action=tr("Report a problem"), on_action=lambda e: self.report_problem_dialog(pkg))
            elif to == "frame":
                self.toast(tr("Starting {title} on the Frame — put the headset on").format(title=title))
            elif res.get("steamvr") is False:
                msg = tr("Starting {title}, but SteamVR isn't running — it may open as a flat window. "
                         "Start SteamVR and relaunch.").format(title=title)
                self.toast(msg, error=True)
            else:
                self.toast(tr("Starting SteamVR and {title} — put your headset on").format(title=title))
        self.run_bg(work)

    def _add_then_play(self, pkg: str, title: str) -> None:
        def run(job: Job):  # (jobs get the Job, not a reporter: this raised TypeError in 0.6.3)
            target = self._target_for("frame")
            status = target.add_to_library([pkg], job.reporter)
            if status.get("state") == "waiting":  # Desktop Mode is open (FramePort on the Frame) or a game runs
                return (tr("{title} will be in the Frame's Steam library when you're back in Gaming Mode: start it "
                           "from there").format(title=title) if status.get("reason") == "desktop" else
                        tr("{title} will be in the Frame's Steam library when the game that's running is closed")
                        .format(title=title))
            if not any(a.get("package") == pkg for a in status.get("added", [])):
                raise RuntimeError(tr("{title} couldn't be added to the Frame's Steam library: {why}").format(
                    title=title, why="; ".join(status.get("errors") or []) or tr("no answer from the Frame")))
            time.sleep(15)  # Steam restarted: give it time to load the library before asking it to launch
            target.launch(pkg)
            library.upsert_game(pkg, last_played=time.time())
            return tr("Starting {title} on the Frame — put the headset on").format(title=title)
        self.toast(tr("{title} isn't in the Frame's Steam library yet: adding it (Steam restarts once)").format(
            title=title))
        self.submit(tr("Add to Steam library: {title}").format(title=title), run, pkg, "tool-frame")

    def quick_action(self, g: dict) -> tuple[str | None, str | None]:
        """(tooltip, kind) for a library card's round button; kind: play | install | update | reinstall (None: no
        button, e.g. while a job runs for the game or before a Frame is connected)."""
        pkg = g["package"]
        if self.jobs.busy_with(pkg):
            return None, None
        play = self.play_options(g)
        if play:
            return play[0][0], "play"
        opts = [o for o in self.install_options(g) if o[2] and not o[3]]
        if not opts or opts[0][1] == ft.Icons.LINK_ROUNDED:  # "Connect your Frame" isn't a quick action
            return None, None
        if opts[0][1] == ft.Icons.COMPUTER_ROUNDED:  # installing on this PC comes first
            dep = self.pc_installs().get(pkg)
            kind = "update" if dep and C.pc_outdated(g, dep) else "reinstall" if dep else "install"
        else:
            kind = {"installed": "reinstall", "outdated": "update"}.get(C.install_state(g, self.frame_info), "install")
        return opts[0][0], kind

    def primary_action(self, pkg: str) -> None:
        g = library.game(pkg)
        opts = [o for o in self.play_options(g) + self.install_options(g) if o[2] and not o[3]]
        if opts:
            opts[0][2](None)

    def game_actions(self, pkg: str, quick: bool = True) -> list[tuple | None]:
        """A game's menu as [(label, icon, handler)], None = divider. quick=True is the library's right-click menu
        (open, install, test, uninstall first); False is the game page's "…" menu (those have buttons there)."""
        g = library.game(pkg)
        if not g:
            return []
        rift = g.get("kind") == "rift"
        linux = g.get("kind") == "linux"  # installed as it is: nothing to analyze, convert or share as a recipe
        job = self.jobs.busy_with(pkg)
        out: list[tuple | None] = []
        if quick:
            out.append((tr("Open"), ft.Icons.OPEN_IN_NEW_ROUNDED, lambda e: self.open_game(pkg)))
            if job:
                out.append((tr("Show progress"), ft.Icons.SYNC_ROUNDED, lambda e: self.show_activity(True)))
                out.append((tr("Cancel"), ft.Icons.CLOSE_ROUNDED, lambda e: self.jobs.cancel(job)))
            else:
                out += [(label, icon, handler) for label, icon, handler, disabled, _ in
                        self.play_options(g) + self.install_options(g) if handler and not disabled]
                on_frame = self.frame_state == "connected" and \
                    C.install_state(g, self.frame_info) in ("installed", "outdated")
                on_pc = rift and pkg in self.pc_installs()
                if on_frame:
                    out.append((tr("Launch test on Frame"), ft.Icons.SCIENCE_OUTLINED,
                                lambda e: self.test_game(pkg, "frame")))
                if on_pc:
                    out.append((tr("Launch test on this PC"), ft.Icons.SCIENCE_OUTLINED,
                                lambda e: self.test_game(pkg, "pc")))
                if self.has_game_settings(g):
                    out.append((tr("Game settings…"), ft.Icons.TUNE_ROUNDED, lambda e: self.settings_dialog(pkg)))
                if on_frame and not rift and not linux:
                    out.append((tr("Add videos and files…"), ft.Icons.VIDEO_LIBRARY_OUTLINED,
                                lambda e: self.go("files", pkg)))
                if on_frame:
                    out.append((tr("Type on Frame"), ft.Icons.KEYBOARD_ROUNDED, lambda e: self.type_on_frame()))
                if not rift and not linux:
                    out.append((tr("Analyze again"), ft.Icons.MANAGE_SEARCH_ROUNDED, lambda e: self.reanalyze(pkg)))
                if on_frame:
                    out.append((tr("Uninstall from Frame"), ft.Icons.DELETE_OUTLINE_ROUNDED,
                                lambda e: self.uninstall(pkg, "frame")))
                if on_pc:
                    out.append((tr("Remove from this PC"), ft.Icons.DELETE_OUTLINE_ROUNDED,
                                lambda e: self.uninstall(pkg, "pc")))
            out.append(None)
            if self.library_view is not None:
                lv = self.library_view
                out.append((tr("Select"), ft.Icons.CHECKLIST_ROUNDED,
                            lambda e: (lv.selected.add(pkg), lv.set_select_mode(True))))
        if rift:
            out.append((tr("Change executable…"), ft.Icons.TERMINAL_ROUNDED, lambda e: self.choose_exe(pkg)))
        if linux and self.linux_programs(g):
            out.append((tr("Change program…"), ft.Icons.TERMINAL_ROUNDED, lambda e: self.choose_exe(pkg)))
        if self.frame_state == "connected":
            out.append((tr("Screenshots"), ft.Icons.PHOTO_LIBRARY_OUTLINED, lambda e: self.go("screenshots", pkg)))
        out.append((tr("Find artwork…"), ft.Icons.IMAGE_SEARCH_ROUNDED, lambda e: self.find_artwork(pkg)))
        out.append((tr("Use your own artwork…"), ft.Icons.UPLOAD_FILE_ROUNDED, lambda e: self.custom_artwork(pkg)))
        if not job and self.frame_state == "connected" and \
                C.install_state(g, self.frame_info) in ("installed", "outdated"):
            out.append((tr("Update Steam art on Frame"), ft.Icons.WALLPAPER_ROUNDED,
                        lambda e: self.update_steam_art(pkg)))
            if C.frame_drive(g, self.frame_info) is not None:  # agent v63+: games can live on a microSD card
                out.append((tr("Move to…"), ft.Icons.DRIVE_FILE_MOVE_OUTLINED, lambda e: self.move_game(pkg)))
        out.append((tr("Refresh store details"), ft.Icons.SYNC_ROUNDED, lambda e: self.refresh_details(pkg)))
        if not job and not linux:
            out.append((tr("Rebuild only (no install)") if not rift else tr("Check game files"), ft.Icons.BUILD_ROUNDED,
                        lambda e: self.build_game(pkg)))
        if not linux:
            out += [(tr("Reset to suggested recipe"), ft.Icons.RESTART_ALT_ROUNDED,
                     lambda e: (pipeline.reset_recipe(pkg), self.toast(tr("Recipe reset")), self.refresh_view())),
                    (tr("Save as known-good recipe"), ft.Icons.VERIFIED_ROUNDED, lambda e: self.save_known_good(pkg)),
                    (tr("Share working config…"), ft.Icons.SHARE_ROUNDED, lambda e: self.share_config_dialog(pkg))]
        out += [(tr("Collect logs"), ft.Icons.FOLDER_ZIP_OUTLINED, lambda e: self.collect_logs(pkg)),
                (tr("Report a problem…"), ft.Icons.BUG_REPORT_OUTLINED, lambda e: self.report_problem_dialog(pkg)),
                None,
                (tr("Remove from library"), ft.Icons.DELETE_OUTLINE_ROUNDED, lambda e: self.remove_from_library(pkg))]
        return out

    @staticmethod
    def linux_programs(g: dict) -> list[str]:
        """The other arm64 programs a Linux app could start with (empty for a lone AppImage/program)."""
        extra = (g.get("analysis") or {}).get("extra") or {}
        cands = [] if extra.get("files") else list(extra.get("candidates") or [])
        return cands if len(cands) > 1 else []

    def install(self, pkg: str, to: str = "frame", confirmed: bool = False) -> Job | None:
        if confirmed:
            return self._submit_install(pkg, to)
        self.install_many([pkg], to)
        return None

    # ---------------------------------------------------------------- queueing several installs
    def upload_estimate(self, pkgs: list[str], to: str = "frame") -> int:
        """Bytes these installs add on the target: the APK, plus the game's data unless it's installed already
        (updates only re-send what changed)."""
        info = getattr(self, "frame_info", None) or {}
        installed = {d.get("package") for d in info.get("installed", [])} if to == "frame" else set()
        total = 0
        for p in pkgs:
            g = library.game(p) or {}
            if g.get("kind") == "rift":
                total += 0 if p in installed else int((g.get("analysis") or {}).get("extra", {}).get("data_bytes") or 0)
                continue
            apk = (g.get("build") or {}).get("apk") or g.get("apk")
            try:
                total += Path(apk).stat().st_size if apk else 0
            except OSError:
                pass
            if p not in installed:
                total += int(g.get("data_bytes") or 0)
        return total

    def install_many(self, pkgs: list[str], to: str = "frame", allow_blocked: bool = False,
                     then: Callable[[], None] | None = None) -> None:
        """Queue installs. Everything that needs a decision is asked first (which program starts each Rift game, one
        at a time; then one dialog per question with a checkbox per game), then all of them run in the background.
        Games marked "Can't run" are skipped unless allow_blocked (the user chose "Install anyway"). `then` runs once
        the installs are queued or there was nothing to queue (not when the user cancels)."""
        from .views.exe_dialog import show_exe_dialog

        if getattr(self, "_asking", False):  # another install's questions are still open
            self.toast(tr("Answer the open install question first"))
            return
        self._asking = True

        def finished() -> None:
            self._asking = False
            if then:
                then()

        def cancel(e=None) -> None:
            self._asking = False
            self.page.pop_dialog()

        def closed(e=None) -> None:  # closed without a button (Esc): like Cancel
            self._asking = False

        games = [library.game(p) for p in pkgs]
        games = [g for g in games if g and not self.jobs.busy_with(g["package"])]
        skipped = [g for g in games if to == "pc" and g.get("kind") != "rift" or
                   not allow_blocked and g.get("kind") != "rift"
                   and (g.get("recipe") or {}).get("status") == "unsupported"]
        games = [g for g in games if g not in skipped]
        if not games:
            why = tr(" ({len} can't be installed there)").format(len=len(skipped)) if skipped else ""
            self.toast(tr("Nothing to install") + why)
            finished()
            return
        need_exe = [g["package"] for g in games if g.get("kind") == "rift" and g.get("exe_confirmed") is False]

        def ask_exe(i=0):
            if i < len(need_exe):
                show_exe_dialog(self, need_exe[i], remaining=len(need_exe) - i - 1, on_done=lambda: ask_exe(i + 1))
            else:
                ask_frame_oculus()

        def ask_frame_oculus():
            # Installing an Oculus/LibOVR Rift game on the Frame: warn that it needs Revive (which can't run there)
            if to != "frame":
                return ask_obb()
            oculus = [g for g in games if g.get("kind") == "rift"
                      and "pcvr.revive" in (g.get("recipe") or {}).get("patches", {})]
            if not oculus:
                return ask_obb()
            boxes = {g["package"]: ft.Checkbox(label=self._title(g["package"]), value=False, active_color=T.ACCENT)
                     for g in oculus}
            pick = C.one_choice()

            def ok(e):
                nonlocal games
                self.page.pop_dialog()
                keep = {p for p, b in boxes.items() if b.value}
                games = [g for g in games if g not in oculus or g["package"] in keep]
                if not games:
                    self.toast(tr("Nothing to install on the Frame — "
                                  "those Oculus games need PC mode (SteamVR + Revive)."))
                    finished()
                    return
                ask_obb()
            self.page.show_dialog(ft.AlertDialog(
                title=ft.Text(tr("These games can't run on the Steam Frame"), weight=ft.FontWeight.W_600),
                content=ft.Container(ft.Column([
                    C.body(tr("They're Oculus games that need Revive to reach VR, and Revive can't run on the Frame. "
                           "Play them on this PC instead (Install on this PC — SteamVR + Revive). Tick any you still "
                           "want to put on the Frame to experiment (they'll likely run flat or crash).")),
                    *boxes.values()], spacing=T.S2, tight=True, scroll=ft.ScrollMode.AUTO), width=T.px(520),
                    height=T.px(min(130 + 36 * len(boxes), 480))),  # fits the list; scrolls when long
                bgcolor=T.SURFACE_2, shape=ft.RoundedRectangleBorder(radius=T.RADIUS),
                modal=True, on_dismiss=pick(closed),
                actions=[C.ghost(tr("Cancel"), on_click=pick(cancel)),
                         C.primary(tr("Continue"), on_click=pick(ok))]))

        def ask_obb():
            # the game expects a data file (.obb) that wasn't found next to its APK: it would hang at start (#85)
            missing = [g for g in games if pipeline.missing_obb(g)]
            if not missing:
                return ask_license()
            boxes = {g["package"]: ft.Checkbox(label=self._title(g["package"]), value=False, active_color=T.ACCENT)
                     for g in missing}
            pick = C.one_choice()

            def ok(e):
                nonlocal games
                self.page.pop_dialog()
                keep = {p for p, b in boxes.items() if b.value}
                games = [g for g in games if g not in missing or g["package"] in keep]
                if not games:
                    self.toast(tr("Nothing to install: add the games' .obb files first"))
                    finished()
                    return
                ask_license()
            self.page.show_dialog(ft.AlertDialog(
                title=ft.Text(tr("Game data (.obb) not found"), weight=ft.FontWeight.W_600),
                content=ft.Container(ft.Column([
                    C.body(tr("These games keep their content in a data file (.obb), and none was found next to "
                              "their APK. Without it they hang at start. Put the .obb files in a folder named like "
                              "the game's package (or obb/) next to the APK and add the folder again. Tick any you "
                              "still want to install.")),
                    *boxes.values()], spacing=T.S2, tight=True, scroll=ft.ScrollMode.AUTO), width=T.px(520),
                    height=T.px(min(150 + 36 * len(boxes), 480))),  # fits the list; scrolls when long
                bgcolor=T.SURFACE_2, shape=ft.RoundedRectangleBorder(radius=T.RADIUS),
                modal=True, on_dismiss=pick(closed),
                actions=[C.ghost(tr("Cancel"), on_click=pick(cancel)),
                         C.primary(tr("Continue"), on_click=pick(ok))]))

        def ask_license():
            from ..core import winhost

            sdk = [g for g in games
                   if g.get("kind") == "rift" and (g["analysis"].get("extra") or {}).get("platform_sdk")]
            if not sdk or to == "pc" and winhost.oculus_platform_dir():
                return go([g["package"] for g in games])  # the Meta Horizon app provides the Platform SDK here
            frame = to == "frame"
            boxes = {g["package"]: ft.Checkbox(label=self._title(g["package"]), value=not frame,
                                               active_color=T.ACCENT) for g in sdk}
            pick = C.one_choice()

            def ok(e):
                self.page.pop_dialog()
                keep = {p for p, b in boxes.items() if b.value}
                go([g["package"] for g in games if g not in sdk or g["package"] in keep])
            self.page.show_dialog(ft.AlertDialog(
                title=ft.Text(tr("These games check their Oculus license"), weight=ft.FontWeight.W_600),
                content=ft.Container(ft.Column([
                    C.body(tr("They use the Oculus Platform SDK, which comes with the Meta Horizon (Oculus) app. It "
                           "doesn't exist on the Steam Frame, so there they crash right at startup (seen with Robo "
                           "Recall, Lies Beneath and Lone Echo) — play them on this PC. Tick any you still want to "
                           "try on the Frame.") if frame else
                           tr("They use the Oculus Platform SDK, which comes with the Meta Horizon (Oculus) app — it "
                           "isn't installed on this PC, so they may quit right after starting. FramePort doesn't "
                           "change how a game checks its license. Untick the ones you'd rather skip.")),
                    *boxes.values()], spacing=T.S2, tight=True, scroll=ft.ScrollMode.AUTO), width=T.px(520),
                    height=T.px(min(130 + 36 * len(boxes), 480))),  # fits the list; scrolls when long
                bgcolor=T.SURFACE_2, shape=ft.RoundedRectangleBorder(radius=T.RADIUS),
                modal=True, on_dismiss=pick(closed),
                actions=[C.ghost(tr("Cancel"), on_click=pick(cancel)),
                         C.primary(tr("Install"), on_click=pick(ok))]))

        def go(final: list[str]):
            need, free = self.upload_estimate(final, to), (getattr(self, "frame_info", None) or {}).get("free_bytes")
            if to == "frame" and free is not None and need > free - 2**30:
                # warn before queueing: the installs would fail one by one once the Frame is full
                pick = C.one_choice()
                self.page.show_dialog(ft.AlertDialog(
                    title=ft.Text(tr("Not enough space on the Frame"), weight=ft.FontWeight.W_600),
                    bgcolor=T.SURFACE_2, shape=ft.RoundedRectangleBorder(radius=T.RADIUS), modal=True,
                    content=ft.Container(C.body(tr(
                        "These installs need about {need} on the Frame, and it has {free} free. Remove games you "
                        "don't play (or use Free up space on the Steam Frame page), or install fewer at once.")
                        .format(need=fmt_size(need), free=fmt_size(max(free, 0)))), width=T.px(480)),
                    on_dismiss=pick(closed),
                    actions=[C.ghost(tr("Cancel"), on_click=pick(cancel)),
                             C.secondary(tr("Install anyway"), on_click=pick(lambda e: (self.page.pop_dialog(),
                                                                                         queue(final))))]))
                return
            queue(final)

        def queue(final: list[str]):
            for p in final:
                self._submit_install(p, to)
            if len(final) > 1:
                msg = tr("Queued {len} installs — they run one after another in the background").format(len=len(final))
                self.toast(msg,
                           action=tr("Activity"), on_action=lambda e: self.show_activity(True))
            if self.library_view:
                self.library_view.set_select_mode(False)
            finished()
        try:
            ask_exe()
        except Exception:
            self._asking = False  # never leave installs blocked behind a question that failed to show
            raise

    def show_failures(self, jobs: list[Job]) -> None:
        """One pop-up for everything that went wrong in a batch: what happened, and Resume / Uninstall / log."""
        installed = {d["package"] for d in (self.frame_info or {}).get("installed", [])}
        recs = self._records()
        rows = []
        for job in jobs:
            pkg, to = job.package, getattr(job, "to", "frame")
            s = job.summary or {}
            fatal = [f for f in s.get("findings", []) if f.get("severity") == "fatal"] or s.get("findings", [])
            if job.state == "failed" and pkg in recs:
                why = tr("Didn't finish ({value}): {error}").format(value=job.stage or 'install', error=job.error)
            elif job.state == "failed":
                why = tr("Failed: {error}").format(error=job.error)
            else:
                why = tr("Installed, but it didn't start properly: ") + (
                    fatal[0]["diagnosis"] if fatal
                    else tr("it stopped at '{value}'").format(value=s.get('milestone') or 'the start'))
            buttons = []
            if pkg in recs:
                buttons.append(C.primary(tr("Resume"), ft.Icons.PLAY_ARROW_ROUNDED,
                                         lambda e, p=pkg, t=to: (self.page.pop_dialog(), self._submit_install(p, t))))
            if to == "frame" and (pkg in installed or pkg in recs):
                buttons.append(C.secondary(tr("Uninstall from Frame"), ft.Icons.DELETE_OUTLINE_ROUNDED,
                                           lambda e, p=pkg: (self.page.pop_dialog(), self.uninstall(p, "frame"))))
            elif to == "pc" and pkg in self.pc_installs():
                buttons.append(C.secondary(tr("Remove from this PC"), ft.Icons.DELETE_OUTLINE_ROUNDED,
                                           lambda e, p=pkg: (self.page.pop_dialog(), self.uninstall(p, "pc"))))
            if job.log_path:
                buttons.append(C.ghost(tr("Launch log"), ft.Icons.DESCRIPTION_ROUNDED,
                                       lambda e, j=job: self.show_log_file(j.log_path, j.title)))
            buttons.append(C.ghost(tr("Report a problem…"), ft.Icons.BUG_REPORT_OUTLINED,
                                   lambda e, p=pkg: (self.page.pop_dialog(), self.report_problem_dialog(p))))
            rows.append(C.card(ft.Column([
                C.body(self._title(pkg) if pkg else job.title, T.TEXT, weight=ft.FontWeight.W_600),
                C.body(why, T.TEXT_2, selectable=True),
                ft.Row(buttons, spacing=T.S2, wrap=True),
            ], spacing=T.S2)))
        n = len(jobs)
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text(tr_n("{n} game didn't work out", "{n} games didn't work out", n) if n > 1 else
                          tr("{value} didn't work out").format(
                              value=self._title(jobs[0].package) if jobs[0].package else jobs[0].title),
                          weight=ft.FontWeight.W_600),
            content=ft.Container(ft.Column(rows, spacing=T.S3, scroll=ft.ScrollMode.AUTO, tight=True), width=T.px(600),
                                 height=min(160 * n + 20, 520)),
            bgcolor=T.SURFACE_2, shape=ft.RoundedRectangleBorder(radius=T.RADIUS),
            actions=[C.ghost(tr("Details"), on_click=lambda e: (self.page.pop_dialog(), self.show_activity(True))),
                     C.primary(tr("Close"), on_click=lambda e: self.page.pop_dialog())]))

    def show_log_file(self, path: str | None, title: str = "") -> None:
        """The full launch log in a viewer where it can be selected and copied as a whole."""
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace") if path else ""
        except OSError as exc:
            text = tr("Couldn't read {path}: {exc}").format(path=path, exc=explain(exc))
        lines = text.splitlines()
        shown = "\n".join(lines[-4000:])
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text(tr("Launch log · {title}").format(title=title), weight=ft.FontWeight.W_600),
            content=ft.Container(ft.Column([
                C.meta(f"{path} · " + (tr_n("{n} line", "{n} lines", len(lines)) if len(lines) <= 4000 else
                                       tr("{n} lines, the last 4000 shown").format(n=len(lines))),
                       selectable=True),
                ft.Container(ft.Column([ft.Text(shown, size=T.px(11), font_family="monospace", color=T.TEXT_2,
                                                selectable=True)], scroll=ft.ScrollMode.AUTO),
                             bgcolor=T.BG, border_radius=T.RADIUS_SM, padding=T.S3, expand=True),
            ], spacing=T.S2), width=T.px(980), height=T.px(620)),
            bgcolor=T.SURFACE_2, shape=ft.RoundedRectangleBorder(radius=T.RADIUS),
            actions=[C.ghost(tr("Copy all"), ft.Icons.CONTENT_COPY_ROUNDED, lambda e: self.copy(text)),
                     C.primary(tr("Close"), on_click=lambda e: self.page.pop_dialog())]))

    def _records(self) -> dict:
        return dict(library.setting("ui.installs") or {})

    def _record(self, pkg: str, **fields) -> None:
        def change(recs):
            recs = dict(recs or {})
            if fields.get("remove"):
                recs.pop(pkg, None)
            else:
                recs[pkg] = {**recs.get(pkg, {}), **fields, "time": time.time()}
            return recs
        library.update_setting("ui.installs", change, {})

    def unfinished_installs(self) -> dict:
        """Installs that were queued/running when the app closed, or were cancelled or failed."""
        known = library.load()["games"]  # one read for all records
        return {p: r for p, r in self._records().items() if not self.jobs.busy_with(p) and p in known}

    def resume_installs(self) -> None:
        for pkg, r in self.unfinished_installs().items():
            self._submit_install(pkg, r.get("to", "frame"))
        self.toast(tr("Resuming — files already copied are skipped"), action=tr("Activity"),
                   on_action=lambda e: self.show_activity(True))

    def forget_installs(self) -> None:
        library.set_setting("ui.installs", {})
        self.refresh_view()

    def _submit_install(self, pkg: str, to: str = "frame") -> Job | None:
        if self.jobs.busy_with(pkg):
            return None
        g = library.game(pkg)
        if not g:
            return None  # removed from the library meanwhile
        rift = g.get("kind") == "rift"
        where = tr("your Frame") if to == "frame" else tr("this PC")
        self._record(pkg, to=to, state="queued")

        def run(job: Job):
            rep = job.reporter
            self._record(pkg, to=to, state="running")
            as_is = library.recipe_from_dict(library.game(pkg)["recipe"]).as_is
            rep.stage("Checking the game" if rift or as_is else "Patching the game")
            info = pipeline.build_game(pkg, rep)
            if not info["ok"]:
                raise RuntimeError(tr("the game didn't pass its checks (see the list above)"))
            rep.check_cancel()
            target = self._target_for(to)
            pipeline.install_game(pkg, target, rep, apk_only=False)
            self._record(pkg, remove=True)  # installed; the launch test below is a separate question
            if to == "frame":  # the install sent the current artwork to the Frame's Steam library
                library.update_game(pkg, lambda e: e.pop("steam_art_stale", None))
            if to == "pc":  # a PC launch test would start the game on the user's desktop — skip it
                rep.stage("Installed")
                return (tr("{get} is installed on this PC — launch it from your Steam library or the Play button "
                           "(SteamVR starts with it)").format(get=g.get('title')))
            rep.check_cancel()
            if not library.setting("install.launch_test", True):  # Settings → Installing
                rep.stage("Installed")
                return (tr("{get} is installed on {where}: put the headset on and launch it from your Steam library "
                           "(automatic launch test is off in Settings)").format(get=g.get('title'), where=where))
            summary = pipeline.test_game(pkg, target, rep)
            job.summary, job.to, job.log_path = summary, to, summary.get("log_path")
            rep.stage(f"Launch test: {'passed' if summary['verdict'] == 'pass' else summary['verdict']}")
            ok = summary["verdict"] == "pass"
            return (tr("{get} is ready on {where}: put the headset on and launch it from your Steam library")
                    .format(get=g.get('title'), where=where)
                    if ok else tr("{get} is installed on {where}, but the launch test needs a look")
                    .format(get=g.get('title'), where=where))
        job_title = tr("Install {title} on {value}").format(title=self._title(pkg),
                                                             value='Frame' if to == 'frame' else 'this PC')
        return self.submit(job_title, run, pkg, "install", to=to)

    def updatable(self) -> list[tuple[str, str]]:
        """[(package, "frame" | "pc")] installs with an update ready (a newer build or changed patch settings)."""
        out = []
        frame_ok = self.frame_state == "connected"
        pc = self.pc_installs() if any(g.get("kind") == "rift" for g in library.games()) else {}
        for g in library.games():
            pkg = g["package"]
            if self.jobs.busy_with(pkg):
                continue
            if frame_ok and C.install_state(g, self.frame_info) == "outdated":
                out.append((pkg, "frame"))
            if pkg in pc and C.pc_outdated(g, pc[pkg]):
                out.append((pkg, "pc"))
        return out

    def update_all(self) -> None:
        """Queue an update for every install marked "update ready" (one at a time, like any install)."""
        todo = self.updatable()
        if not todo:
            self.toast(tr("Everything is up to date"))
            return
        # one batch per target, so every question (e.g. Oculus games on the Frame) is asked once for all games instead
        # of once per game; games marked "Can't run" are included (the user installed them already)
        groups: dict[str, list[str]] = {}
        for pkg, to in todo:
            groups.setdefault(to, []).append(pkg)
        batches = list(groups.items())

        def next_batch(i: int = 0) -> None:
            if i < len(batches):
                to, pkgs = batches[i]
                self.install_many(pkgs, to, allow_blocked=True, then=lambda: next_batch(i + 1))
            else:
                self.show_activity(True)
        next_batch()

    def install_blocked(self, pkg: str) -> None:
        """Install a game marked "Can't run" after saying why it's marked so."""
        g = library.game(pkg) or {}
        notes = (g.get("recipe") or {}).get("notes") or tr("It has a known blocker on the Steam Frame.")

        def go(e):
            self.page.pop_dialog()
            self.install_many([pkg], "frame", allow_blocked=True)
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text(tr("Install {title} anyway?").format(title=self._title(pkg))), bgcolor=T.SURFACE_2,
            content=ft.Column([C.body(tr("This game is marked \"Can't run\" on the Steam Frame:"), T.TEXT_2),
                               C.body(notes),
                               C.body(tr("Install it anyway to try it, e.g. with different patches."), T.TEXT_2)],
                              tight=True, width=T.px(520)),
            actions=[C.ghost(tr("Cancel"), on_click=lambda e: self.page.pop_dialog()),
                     C.primary(tr("Install anyway"), ft.Icons.WARNING_AMBER_ROUNDED, go)]))

    def test_game(self, pkg: str, to: str = "frame") -> Job:
        title = self._title(pkg)

        def run(job: Job):
            target = self._target_for(to)
            if library.game(pkg):
                summary = pipeline.test_game(pkg, target, job.reporter)
            else:  # installed on the Frame but not in this library
                res, _ = target.launch_test(pkg, job.reporter)
                summary = {"verdict": res.verdict, "milestone": res.milestone, "suggestions": []}
            job.summary, job.to, job.log_path = summary, to, summary.get("log_path")
            return tr("{title}: launch test {verdict} (furthest: {value})").format(
                title=title, verdict=summary['verdict'], value=summary.get('milestone') or '—')
        return self.submit(tr("Launch test: {title}").format(title=title), run, pkg, "test", to=to)

    def update_steam_art(self, pkg: str) -> Job:
        """Send the game's current artwork to its Steam entry on the Frame (Steam restarts once)."""
        def run(job: Job):
            self._target_for("frame").update_steam_art(pkg, job.reporter)
            library.update_game(pkg, lambda e: e.pop("steam_art_stale", None))
            return tr("{title}: Steam artwork updated on the Frame").format(title=self._title(pkg))
        return self.submit(tr("Update Steam art: {title}").format(title=self._title(pkg)), run, pkg, "art")

    def move_game(self, pkg: str) -> None:
        """Move to…: pick another drive of the Frame (microSD card, internal storage) for an installed game; the copy
        runs as a Frame job (GitHub #90)."""
        from ..install import drives

        title = self._title(pkg)
        here = C.frame_drive(library.game(pkg) or {"package": pkg}, self.frame_info)
        group = ft.RadioGroup(content=ft.Column([C.meta(tr("Looking for drives…"))], spacing=T.S2))
        go = C.primary(tr("Move"), ft.Icons.DRIVE_FILE_MOVE_OUTLINED, disabled=True)
        found: dict[str, dict] = {}

        def picked(e):
            go.disabled = not group.value
            C.update(go)

        group.on_change = picked

        def fill():
            try:
                listed = self._target_for("frame").drives()
            except Exception as exc:  # noqa: BLE001
                group.content.controls = [C.meta(tr("Couldn't list the Frame's drives: {error}")
                                                 .format(error=explain(exc)))]
                C.update(group)
                return
            rows = []
            for d in listed:
                key = drives.INTERNAL if d["internal"] else d["install_dir"]
                current = drives.is_on({"drive": here}, d) if here else d["internal"]
                found[key] = d
                note = tr("it's here now") if current else \
                    (d.get("reason") or "") if not d.get("usable") else drives.free_text(d)
                rows.append(ft.Row([
                    ft.Radio(value=key, active_color=T.ACCENT, disabled=current or not d.get("usable")),
                    ft.Icon(ft.Icons.SD_CARD_ROUNDED if d.get("removable") else ft.Icons.STORAGE_ROUNDED,
                            size=T.px(18), color=T.TEXT_2),
                    ft.Column([C.body(tr("Internal storage") if d["internal"] else d["label"], T.TEXT),
                               C.meta(note)], spacing=T.px(2), expand=True),
                ], spacing=T.S2))
            group.content.controls = rows or [C.meta(tr("Only internal storage"))]
            C.update(group)

        def start(e):
            dest = group.value
            if not dest:
                return
            self.page.pop_dialog()
            label = tr("Internal storage") if dest == drives.INTERNAL else found.get(dest, {}).get("label", dest)

            def run(job: Job):
                self._target_for("frame").move(pkg, dest, job.reporter)
                return tr("{title} moved to {label}").format(title=title, label=label)
            self.submit(tr("Move {title} to {label}").format(title=title, label=label), run, pkg, "tool-frame")

        go.on_click = start
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text(tr("Move {title} to…").format(title=title), weight=ft.FontWeight.W_600),
            content=ft.Container(ft.Column([C.body(C.HELP["move_game"]), group], spacing=T.S3, tight=True),
                                 width=T.px(460)),
            bgcolor=T.SURFACE_2, shape=ft.RoundedRectangleBorder(radius=T.RADIUS),
            actions=[C.ghost(tr("Cancel"), on_click=lambda e: self.page.pop_dialog()), go]))
        self.run_bg(fill)

    def build_game(self, pkg: str) -> Job:
        def run(job: Job):
            info = pipeline.build_game(pkg, job.reporter)
            return f"{self._title(pkg)}: {'ready' if info['ok'] else 'checks failed'}"
        return self.submit(tr("Prepare {title}").format(title=self._title(pkg)), run, pkg, "build")

    def uninstall(self, pkg: str, to: str = "frame") -> None:
        title = self._title(pkg)
        text = tr("Removes {title}'s game files from the Frame. Saves and the game's own files are kept unless you "
                  "tick the box below; the Steam entry disappears after the next Steam restart.").format(
            title=title) if to == "frame" else \
            tr("Removes {title} from this PC's Steam library (Steam restarts once). "
               "The game folder isn't touched.").format(title=title)

        delete_local, extra = self._delete_local_option(pkg)
        delete_data = None
        if to == "frame":  # GitHub #130: what the game stored (saves, mods, downloads) used to survive every uninstall
            delete_data = ft.Checkbox(label=tr("Also delete its saves and everything it stored on the Frame (mods, "
                                               "downloaded content)"), value=False, active_color=T.ERROR)
            extra = delete_data if extra is None else ft.Column([delete_data, extra], spacing=T.px(8), tight=True)

        def run(job: Job):
            self._target_for(to).uninstall(pkg, keep_data=not (delete_data and delete_data.value))
            self._pc_cache = None
            if delete_local and delete_local.value:
                done, freed = pipeline.delete_local_files(pkg)
                job.reporter.log(f"deleted {len(done)} item(s) on this PC, {fmt_size(freed)}")
                self.go("library")
                return tr("Uninstalled {title} and deleted its files on this PC ({size})").format(
                    title=title, size=fmt_size(freed))
            return tr("Uninstalled {title}").format(title=title)
        C.confirm(self.page, tr("Uninstall {title}?").format(title=title), text, tr("Uninstall"),
                  lambda: self.submit(tr("Uninstall {title}").format(title=title), run, pkg, "uninstall"), danger=True,
                  extra=extra)

    def _delete_local_option(self, pkg: str) -> tuple[ft.Checkbox | None, ft.Control | None]:
        """The 'Also delete this game's files on this PC' checkbox (off by default) with the paths it would delete."""
        files = pipeline.local_game_files(pkg)
        if not files:
            return None, None
        size = sum((sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size)
                   for p in files)
        box = ft.Checkbox(label=tr("Also delete this game's files on this PC ({size})").format(size=fmt_size(size)),
                          value=False, active_color=T.ERROR)
        from ..core import winhost

        shown = [winhost.to_windows(p) if winhost.is_wsl() else str(p) for p in files]
        return box, ft.Column([box, *[C.body("• " + s, T.TEXT_3) for s in shown[:4]]], spacing=T.px(4), tight=True)

    def remove_from_library(self, pkg: str) -> None:
        title = self._title(pkg)
        delete_local, extra = self._delete_local_option(pkg)

        def remove():
            if delete_local and delete_local.value:
                _done, freed = pipeline.delete_local_files(pkg)  # (removes the library entry too)
                message = tr("Removed {title} and deleted its files on this PC ({size})").format(
                    title=title, size=fmt_size(freed))
            else:
                library.remove_game(pkg)
                message = tr("Removed {title}").format(title=title)
            self.go("library")
            self.toast(message)
        C.confirm(self.page, tr("Remove {title} from the library?").format(title=title),
                  tr("Removes FramePort's entry. Anything installed on the Frame stays."), tr("Remove"), remove,
                  danger=True, extra=extra)

    def _target_for(self, to: str):
        if to == "pc":
            from ..targets.pc_revive import PcReviveTarget

            self._pc_cache = None
            return PcReviveTarget()
        if not self.target:
            raise RuntimeError(tr("the Frame isn't connected"))
        return self.target

    def save_known_good(self, package: str) -> None:
        catalog.save_user_entry(catalog.entry_from_library(library.game(package)))
        self.toast(tr("Saved as a known-good recipe"))

    # ================================================================== sharing / diagnostics
    def open_url(self, url: str) -> None:
        from ..core import winhost

        if winhost.is_wsl() and winhost.open_url(url):  # the desktop client would open a Linux browser in WSL
            return
        try:
            self.page.run_task(ft.UrlLauncher().launch_url, url)
        except Exception:  # noqa: BLE001
            winhost.open_url(url)

    def _diag_target(self, pkg: str | None):
        g = library.game(pkg) if pkg else None
        if g and g.get("kind") == "rift" and pkg in self.pc_installs() and \
                C.install_state(g, self.frame_info) not in ("installed", "outdated"):
            return self._target_for("pc")
        return self.target if self.frame_state == "connected" else None

    def collect_logs(self, pkg: str | None = None, report: str | None = None) -> None:
        """Diagnostics zip (redacted) → shown in the file manager; report (a description, may be "") also opens a
        prefilled GitHub problem report to attach it to."""
        from ..core import winhost

        target, info = self._diag_target(pkg), self.frame_info

        def run(job: Job):
            path = pipeline.collect_diagnostics([pkg] if pkg else None, target, job.reporter)
            winhost.open_folder(path, select=True)
            if report is not None:
                self.open_url(pipeline.problem_report(pkg, report, path, info if target is self.target else None))
                return tr("Saved {name}: drag it into the GitHub issue that just opened").format(name=path.name)
            return tr("Saved {name} (in {parent})").format(name=path.name, parent=path.parent)
        job_title = tr("Collect logs: {title}").format(title=self._title(pkg)) if pkg else tr("Collect app logs")
        self.submit(job_title, run, pkg, "diag")

    def report_problem_dialog(self, pkg: str | None = None) -> None:
        text = ft.TextField(label=tr("What happens? (optional: you can also write it on GitHub)"), multiline=True,
                            min_lines=3, max_lines=8, width=T.px(560), border_color=T.BORDER)

        def go(e):
            self.page.pop_dialog()
            self.collect_logs(pkg, text.value or "")
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text(tr("Report a problem · {title}").format(title=self._title(pkg)) if pkg
                          else tr("Report a problem")),
            bgcolor=T.SURFACE_2,
            content=ft.Column([
                C.body(tr("FramePort saves a diagnostics zip (logs, recipe, device info; no game files, personal data "
                       "removed) and opens a prefilled GitHub issue. Drag the zip into it, check the text, submit."),
                       T.TEXT_2),
                ft.Row([text, C.help_icon("diag_bundle")]),
            ], tight=True, spacing=T.S3, width=T.px(600)),
            actions=[C.ghost(tr("Cancel"), on_click=lambda e: self.page.pop_dialog()),
                     C.primary(tr("Collect and open GitHub"), ft.Icons.OPEN_IN_NEW_ROUNDED, on_click=go)]))

    def share_config_dialog(self, pkg: str, preset: str | None = None) -> None:
        g = library.game(pkg) or {}
        last = g.get("last_test") or {}
        status = ft.RadioGroup(ft.Row([ft.Radio(value="works", label=tr("Works")),
                                       ft.Radio(value="issues", label=tr("Works with issues"))]),
                                value=preset or ("issues" if g.get("recipe", {}).get("status") == "issues"
                                                 else "works"))
        notes = ft.TextField(label=tr("Notes (what you checked, known issues)"), multiline=True, min_lines=2,
                             max_lines=6, width=T.px(560), border_color=T.BORDER)
        played = ft.Checkbox(label=tr("I played it in the headset with this recipe"), value=False)
        send = C.primary(tr("Open GitHub issue"), ft.Icons.OPEN_IN_NEW_ROUNDED, on_click=None)

        def sync(e=None):
            send.disabled = not played.value
            C.update(send)
        played.on_change = sync
        sync()

        def go(e):
            self.page.pop_dialog()
            info = self.frame_info if self.frame_state == "connected" else None

            def work():
                url = pipeline.share_working_config(pkg, status.value or "works", notes.value or "", info)
                library.upsert_game(pkg, shared_config=time.time())  # the game page stops asking
                self.open_url(url)
                self.toast(tr("Saved as known-good. Check the issue on GitHub and submit it"))
            self.run_bg(work)
        send.on_click = go
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text(tr("Share working config · {title}").format(title=self._title(pkg))), bgcolor=T.SURFACE_2,
            content=ft.Column([
                C.body(tr("Opens a prefilled GitHub issue with this game's recipe, so it can join the built-in catalog "
                       "(no account token needed; you review and submit it on GitHub). No game files or personal "
                       "data are sent."), T.TEXT_2),
                *([C.body(tr("Last launch test: {get} · furthest: {value}").format(
                              get=last.get('verdict'), value=last.get('milestone') or '—'),
                          T.TEXT_3)] if last else []),
                ft.Row([status, C.help_icon("share_config")]), notes, played,
                C.body(tr("Launch tests run without the headset worn, "
                          "so only you can confirm the picture and controls."),
                       T.TEXT_3),
            ], tight=True, spacing=T.S3, width=T.px(600)),
            actions=[C.ghost(tr("Cancel"), on_click=lambda e: self.page.pop_dialog()), send]))

    @staticmethod
    def has_game_settings(g: dict | None) -> bool:
        """Quest games built with FramePort's adapter have game settings; PC VR games and 2D Android apps don't."""
        if not g:
            return True  # installed on the Frame but not in this library: a Quest game
        if g.get("kind") in ("rift", "linux") or not g.get("analysis"):
            return g.get("kind") not in ("rift", "linux")
        return library.analysis_from_dict(g["analysis"]).vr_kind != "none"

    def settings_dialog(self, package: str) -> None:
        from .views.adapter_dialog import show_adapter_dialog

        show_adapter_dialog(self, package)

    # ================================================================== library
    async def pick_folder(self, e=None):
        path = await ft.FilePicker().get_directory_path(
            dialog_title=tr("Folder with VR games (Android APKs or PC VR games)"))
        if path:
            self.scan(path)

    async def pick_game_folder(self, e=None):
        path = await ft.FilePicker().get_directory_path(dialog_title=tr("One PC VR game folder"))
        if path:
            self.scan(path, single=True)

    async def pick_apk(self, e=None):
        files = await ft.FilePicker().pick_files(allow_multiple=True, allowed_extensions=["apk"])
        for f in files or []:
            if f.path:
                self.scan(f.path)

    async def pick_windows_exe(self, e=None):
        files = await ft.FilePicker().pick_files(dialog_title=tr("A Windows program (.exe)"), allow_multiple=True,
                                                 allowed_extensions=["exe"])
        for f in files or []:
            if f.path:
                self.add_windows_exe(f.path)

    def add_windows_exe(self, path: str) -> Job:
        """One Windows program: run by Proton on the Frame (flat unless it has VR), like "Add one game folder…"."""
        def run(job: Job):
            job.reporter.stage("Looking at the program")
            g = pipeline.add_windows_exe(path, job.reporter)
            try:
                from ..artwork import thumbs

                thumbs.prewarm(g["package"])
            except Exception:  # noqa: BLE001 - artwork is optional
                pass
            library.set_setting("ui.welcome_done", True)
            self.open_game(g["package"])
            return tr("Added {title}").format(title=g.get("title"))
        return self.submit(tr("Add {name}").format(name=Path(path).name), run, None, "task")

    def pick_link(self, e=None) -> None:
        from .views.link_dialog import show_paste_dialog

        show_paste_dialog(self)

    def open_install_link(self, text: str, from_web: bool = False) -> None:
        """A framedrop:// / frameport:// link (from a web page's button) or a pasted one: ask, download, install."""
        from .views.link_dialog import open_link

        if from_web and not self.page.web:
            try:
                self.page.run_task(self.page.window.to_front)
            except Exception:  # noqa: BLE001
                pass
        open_link(self, text, pasted=not from_web)

    def add_dropped(self, paths: list[str]) -> None:
        """Files dropped on the Library: APKs, Linux builds, Windows programs, folders, FrameDrop manifests."""
        from . import dropped

        for kind, path in dropped.route(paths):
            if kind == "apk":
                self.scan(path)
            elif kind == "linux":
                self.add_linux(path)
            elif kind == "exe":
                self.add_windows_exe(path)
            elif kind == "manifest":
                self.open_install_link(path)
            elif kind == "folder":
                self.scan(path)
            else:
                self.toast(tr("FramePort can't add {name}: drop an APK, a Linux build (.zip, AppImage), a Windows "
                              "program (.exe) or a folder").format(name=Path(path).name), error=True)

    async def pick_linux_app(self, e=None):
        """An AppImage or an archive (.zip/.tar.*) with an arm64 Linux app. Any file can be picked: AppImages often
        have no extension."""
        from ..analysis import linux

        title = tr("A Linux app (AppImage, .zip or .tar.gz; arm64, or x86_64 through translation)")
        files = await ft.FilePicker().pick_files(dialog_title=title, allow_multiple=True)
        for f in files or []:
            if not f.path:
                continue
            if linux.looks_like_linux_app(Path(f.path)):
                self.add_linux(f.path)
            else:
                self.toast(tr("{name} isn't a Linux app (an AppImage, a Linux program or a .zip/.tar archive)")
                           .format(name=Path(f.path).name), error=True)

    async def pick_linux_folder(self, e=None):
        path = await ft.FilePicker().get_directory_path(dialog_title=tr("Folder with a Linux app"))
        if path:
            self.add_linux(path)

    def add_linux(self, path: str) -> Job:
        """Add a Linux app (GitHub #31; arm64, or x86_64 through FEX) in the background, then open its page."""
        name = Path(path).name

        def run(job: Job):
            job.reporter.stage("Looking at the app")
            g = pipeline.add_linux_app(path, job.reporter)  # ValueError (not a Linux program): the job fails with it
            pkg = g["package"]
            try:
                from ..artwork import thumbs

                thumbs.prewarm(pkg)  # the card's thumbnail of the placeholder cover
            except Exception:  # noqa: BLE001 - artwork is optional
                pass
            library.set_setting("ui.welcome_done", True)
            self.open_game(pkg)
            extra = (g.get("analysis") or {}).get("extra") or {}
            if len(extra.get("candidates") or []) > 1 and not extra.get("files"):  # (the page has "Change…")
                return tr("Added {title}: it starts with {exe}. Not the right program? Change it on its page.").format(
                    title=g.get("title"), exe=g.get("exe", "").rsplit("/", 1)[-1])
            return tr("Added {title}").format(title=g.get("title"))
        return self.submit(tr("Add {name}").format(name=name), run, None, "task")

    def scan_roots(self) -> list[str]:
        """Folders to rescan: the ones scanned before (remembered from now on), else the folders the library's games
        came from (one level up from each game folder)."""
        roots = [r for r in library.setting("scan.roots", []) if Path(r).exists()]
        if not roots:
            found = set()
            for g in library.load().get("games", {}).values():
                if g.get("kind") == "linux":  # added one by one (its origin may be e.g. Downloads)
                    continue
                origin = g.get("origin") or (str(Path(g["apk"]).parent) if g.get("apk") else None)
                if origin and Path(origin).parent.exists():
                    found.add(str(Path(origin).parent))
            roots = sorted(found)
        # a folder inside another one is scanned with it (scanning it again would only repeat work)
        return [r for r in roots if not any(o != r and Path(r).is_relative_to(o) for o in roots)]

    def rescan(self, e=None) -> None:
        """Scan the library's folders again for games added since (unchanged games aren't analyzed again)."""
        roots = self.scan_roots()
        if not roots:
            self.toast(tr("No folders to rescan yet: add games with Scan a folder"))
            return
        for r in roots:
            self.scan(r, only_new=True)
        self.toast(tr_n("Rescanning {n} folder for new games", "Rescanning {n} folders for new games", len(roots)))

    def scan(self, path: str, single: bool = False, only_new: bool = False) -> Job:
        last = {"t": 0.0}
        if not single:  # remembered for "Rescan folders"
            roots = [r for r in library.setting("scan.roots", []) if r != str(path)]
            library.set_setting("scan.roots", roots + [str(path)])

        def added_one(entry: dict):
            from ..artwork import thumbs

            try:
                thumbs.prewarm(entry["package"])
            except Exception:  # noqa: BLE001
                pass
            if entry.get("kind") == "rift" and entry.get("exe_confirmed") is False:
                self.exe_queue.append(entry["package"])
            if self.route[0] == "library" and time.time() - last["t"] > 1.0:  # stream new cards in
                last["t"] = time.time()
                self.refresh_view()

        def run(job: Job):
            rep = job.reporter
            rep.stage("Looking for games")
            added = pipeline.add_path(Path(path), rep, on_added=added_one, force_rift=single, art=True,
                                      only_new=only_new)
            if self.route[0] == "welcome" and added:
                library.set_setting("ui.welcome_done", True)
                self.route = ("library",)
            n = len(added)
            if only_new:
                return tr_n("{n} new game in {name}", "{n} new games in {name}", n,
                            name=Path(path).name) if added else \
                    tr("No new games in {name}").format(name=Path(path).name)
            return tr_n("Added {n} game", "Added {n} games", n) if added else tr("No games found in that folder")
        return self.submit(tr("Scan {name}").format(name=Path(path).name), run, None, "scan")

    # ------------------------------------------------------------------ executable choice / artwork
    def next_exe_choice(self) -> None:
        from .views.exe_dialog import show_exe_dialog

        while self.exe_queue:
            pkg = self.exe_queue.pop(0)
            g = library.game(pkg)
            if g and g.get("exe_confirmed") is False:
                show_exe_dialog(self, pkg, remaining=len(self.exe_queue), on_done=self.next_exe_choice)
                return

    def reanalyze(self, pkg: str) -> None:
        def run(job: Job):
            pipeline.reanalyze(pkg, job.reporter)
            return tr("{title}: analyzed again").format(title=self._title(pkg))
        self.submit(tr("Analyze {title} again").format(title=self._title(pkg)), run, pkg, "task")

    def _refresh_catalog(self, force: bool = False) -> int:
        """New/changed confirmed configs from the repo's main branch; games following the catalog get them (their
        page then offers Update on Frame). Quiet unless something changed or `force` (Settings → Check now)."""
        from ..recommend import catalog

        n = catalog.refresh_remote(force=force)
        if n:
            library.load()  # re-derives recipes whose catalog entry changed
            self.refresh_view()
            self.toast(tr_n("{n} game config was updated", "{n} game configs were updated", n))
        elif force:
            self.toast(tr("Game configs are up to date"))
        return n

    def frame_power(self, action: str, force: bool = False) -> None:
        """Sleep / restart / shut down the Frame (agent `power`, run a few seconds later so the answer arrives)."""
        heading = {"sleep": tr("Put the Frame to sleep?"), "restart": tr("Restart the Frame?"),
                   "shutdown": tr("Shut down the Frame?")}[action]
        text = {"sleep": tr("The Frame goes to sleep in a few seconds. Wake it with its power button."),
                "restart": tr("The Frame restarts in a few seconds; FramePort reconnects when it's back."),
                "shutdown": tr("The Frame turns off in a few seconds. Turn it on again with its power button.")}[action]
        if force:
            heading, text = tr("A game is running on the Frame"), tr("It will be closed without saving. Continue?")
        label = {"sleep": tr("Sleep"), "restart": tr("Restart"), "shutdown": tr("Shut down")}[action]

        def go():
            def work():
                target = self.target
                if not target:
                    self.toast(tr("The Frame isn't connected"), error=True)
                    return
                try:
                    target.frame.agent("power", action=action, force=force, timeout=30)
                except AgentFailed as exc:
                    if "game is running" in str(exc) and not force:
                        self.page.run_thread(lambda: self.frame_power(action, force=True))
                        return
                    raise
                done = {"sleep": tr("The Frame goes to sleep now."), "restart": tr("The Frame is restarting."),
                        "shutdown": tr("The Frame is shutting down.")}[action]
                self.toast(done)
            self.run_bg(work)
        C.confirm(self.page, heading, text, label, go, danger=action != "sleep" or force)

    def type_on_frame(self) -> None:
        self.go("keyboard")

    def choose_exe(self, pkg: str) -> None:
        from .views.exe_dialog import show_exe_dialog

        show_exe_dialog(self, pkg)

    def refresh_details(self, pkg: str) -> Job:
        def run(job: Job):
            d = pipeline.fetch_details(pkg, job.reporter)
            n = len(d.get("screenshots") or [])
            sources = ', '.join(d.get('sources') or []) or 'nowhere'
            return tr("{title}: details from {value}").format(title=self._title(pkg), value=sources) + \
                (tr(", {n} screenshots").format(n=n) if n else "")
        return self.submit(tr("Store details: {title}").format(title=self._title(pkg)), run, pkg, "art")

    def find_artwork(self, pkg: str) -> None:
        from .views.art_dialog import show_art_dialog

        show_art_dialog(self, pkg)

    def custom_artwork(self, pkg: str) -> None:
        from .views.art_dialog import show_custom_art_dialog

        show_custom_art_dialog(self, pkg)

    # ================================================================== Frame
    def _startup(self):
        from ..frame.connection import parse_target, saved_targets

        self.run_bg(self._analysis_refresh)
        self._art_backfill()
        saved = saved_targets()
        if saved:
            self.connect(saved[0], quiet=True)
            return
        from ..frame.discovery import browse

        for f in browse(4, scan=False):
            self.connect(parse_target(f"{f.user}@{f.host}"), quiet=True)
            break

    def _analysis_refresh(self) -> None:
        """Games analysed by an older FramePort lack fields newer fixes depend on (GitHub #104): their APKs are read
        again once, in the background. Not a job: ~8 s per large APK, and the queue would hold the user's installs
        for minutes (a build of such a game analyses it first itself). Recipes the user didn't edit then follow."""
        if not pipeline.outdated_analyses():
            return
        n = pipeline.refresh_analyses()
        if n:
            self.refresh_view()
            self.toast(tr_n("{n} game was analyzed again for new fixes", "{n} games were analyzed again for new fixes",
                            n))

    def _art_backfill(self) -> None:
        """Rift games added before automatic artwork (or while offline): fetch it once in the background."""
        from ..artwork import sources

        games = library.games()
        art = [g["package"] for g in games
               if g.get("kind") == "rift" and not g.get("art_source") and not sources.has_art(g["package"])]
        info = [g["package"] for g in games if "details" not in g]
        if not art and not info:
            return

        def run(job: Job):
            n = len(art) + len(info)
            for i, pkg in enumerate(art):
                job.reporter.check_cancel()
                job.reporter.progress(i / n, self._title(pkg))
                pipeline.fetch_art(pkg, job.reporter)
            for i, pkg in enumerate(info, len(art)):
                job.reporter.check_cancel()
                job.reporter.progress(i / n, self._title(pkg))
                pipeline.fetch_details(pkg, job.reporter)
            return tr_n("Store details for {n} game", "Store details for {n} games", n)
        self.submit(tr("Find artwork and store details"), run, kind="art")

    def _poll(self):
        """Keep the connection card honest: refresh when connected, retry quietly when offline."""
        from ..frame.connection import saved_targets

        while True:
            time.sleep(10 if self.jobs.paused else POLL_SECONDS)
            if self.jobs.paused == "frame":
                self.retry_frame(quiet=True)
                continue
            if self.jobs.current() or self.jobs.paused == "battery":
                self._battery_check(fetch=True)  # the job is using the connection: only ask for the battery
                continue
            try:
                if self.frame_state == "connected":
                    self.refresh_frame(quiet=True, background=False)
                    self._battery_check(fetch=False)
                elif self.frame_state == "offline" and saved_targets():
                    self.connect(saved_targets()[0], quiet=True)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def _check_frame_restart(self, info: dict | None) -> None:
        """The Frame restarted without shutting down (a crash, a GPU reset) soon after a FramePort game started: say
        so, with Report a problem (Reddit 2026-10-03: "Resident Evil 4 crashes the Frame after the opening cutscene").
        Uses the last boot seen (kept in settings, so a crash while FramePort was closed counts too)."""
        from . import restart

        boot = (info or {}).get("boot") or {}
        if not boot.get("boot_id"):
            return
        seen = library.setting("frame.boot_id")
        if seen == boot["boot_id"]:
            return
        library.set_setting("frame.boot_id", boot["boot_id"])
        game = restart.crashed_game(boot, seen)
        if game is None:
            return
        applog.log.info("Frame restarted unexpectedly after %s was started: %s", game["package"], boot)
        library.upsert_game(game["package"], frame_restart=boot.get("boot_time")) if library.game(game["package"]) \
            else None
        pkg = game["package"]
        self.toast(tr("Your Frame restarted unexpectedly while {title} was running (or soon after). If that game "
                      "crashed it, please report it.").format(title=game.get("title") or pkg), error=True,
                   action=tr("Report a problem"), on_action=lambda e: self.report_problem_dialog(pkg))

    def _check_sessions(self, info: dict | None) -> None:
        """A FramePort game's play session ended since FramePort last looked (agent v70 `last_play`): fetch and triage
        its log on a background thread (pipeline.triage_session); the game page shows the result ("Last session")."""
        if getattr(self, "_sessions_busy", False):
            return
        try:
            due = pipeline.sessions_due((info or {}).get("installed") or [])
        except Exception:  # noqa: BLE001 (never fail the connection refresh over it)
            applog.log.exception("checking for finished play sessions failed")
            return
        if not due or self.target is None:
            return
        self._sessions_busy, target = True, self.target

        def work():
            try:
                for pkg, lp, fetch in due:
                    if not fetch:
                        pipeline.mark_session_checked(pkg, lp["end"])
                        continue
                    try:
                        summary = pipeline.triage_session(pkg, target)
                    except Exception as exc:  # noqa: BLE001 (an older agent, the connection dropped: look next time)
                        applog.log.warning("session triage of %s failed: %s", pkg, exc)
                        if isinstance(exc, AgentFailed):  # the agent answered: asking again won't help
                            pipeline.mark_session_checked(pkg, lp["end"])
                        continue
                    self._session_triaged(pkg, summary)
            finally:
                self._sessions_busy = False
        threading.Thread(target=work, daemon=True).start()

    def _session_triaged(self, pkg: str, summary: dict) -> None:
        from ..validate.session import shown_findings

        if not summary or not shown_findings(summary):
            return
        if self.route[:2] == ("game", pkg):
            self.refresh_view()
        else:
            self.toast(tr("FramePort looked at your last session of {title} and has something to try.")
                       .format(title=self._title(pkg)), action=tr("Open game"),
                       on_action=lambda e: self.open_game(pkg))

    def apply_session_fix(self, pkg: str, suggestions: list[str], finding: str | None = None) -> None:
        """A fix from the game page's "Last session": FrameBridge settings are written on the Frame at once (no
        rebuild); anything else goes into the recipe and the game is rebuilt and reinstalled."""
        g = library.game(pkg) or {}

        def mark():
            def fn(entry):
                ls = entry.get("last_session")
                if isinstance(ls, dict):
                    ls.setdefault("applied", []).extend(x for x in suggestions if x not in ls.get("applied", []))
                    if finding:
                        ls.setdefault("answered", []).append(finding)
            library.update_game(pkg, fn)
        on_frame = self.target is not None and C.install_state(g, self.frame_info) in ("installed", "outdated")
        if pipeline.adapter_only(suggestions) and on_frame:
            target = self.target

            def work():
                pipeline.apply_suggestions_live(pkg, suggestions, target)
                mark()
                self.toast(tr("Done. It's used the next time you start {title}.").format(title=self._title(pkg)))
                self.refresh_view()
            self.run_bg(work)
            return
        pipeline.apply_suggestions(pkg, suggestions)
        mark()
        if self.frame_state == "connected":
            self.install(pkg, "frame")
        else:
            self.toast(tr("Added to the game's patches. Install it on your Frame to use it."))
            self.refresh_view()

    def answer_session_question(self, pkg: str, finding: str) -> None:
        """No to a "Last session" question: don't ask it again for this session."""
        library.update_game(pkg, lambda g: isinstance(g.get("last_session"), dict) and
                            g["last_session"].setdefault("answered", []).append(finding))
        self.refresh_view()

    def dismiss_session(self, pkg: str) -> None:
        library.update_game(pkg, lambda g: isinstance(g.get("last_session"), dict) and
                            g["last_session"].__setitem__("dismissed", True))
        self.refresh_view()

    def _battery_check(self, fetch: bool) -> None:
        """Battery level for the sidebar, and the queue on battery power: warn once, pause before the Frame would
        switch itself off mid-upload, continue when it charges (ui/battery.py)."""
        from . import battery

        target = self.target
        if self.frame_state != "connected" or target is None:
            return
        b = (self.frame_info or {}).get("battery")
        if fetch:
            try:
                b = target.frame.agent("battery", timeout=20, ensure=False).get("battery")
            except Exception:  # noqa: BLE001 - losing the Frame is the job's business; an old agent has no reading
                frame = getattr(target, "frame", None)
                if self.jobs.paused == "battery" and not (frame is not None and frame.alive()):
                    self.jobs.pause("frame")  # it switched off after all: reconnect and continue when it's back
                return
            if self.frame_info is not None:
                self.frame_info["battery"] = b
        act = battery.advice(b, self.jobs.has_frame_work(), self.jobs.paused, getattr(self, "_battery_warned", False))
        if act in ("warn", "pause"):
            self._battery_warned = True
            if act == "pause":
                self.jobs.pause("battery")
            applog.log.info("Frame battery %s: %s", act, b)
            self.toast(battery.message(act, b), error=True)
        elif act == "resume":
            had_work = self.jobs.has_frame_work()
            self.jobs.resume()
            applog.log.info("Frame battery: continuing (%s)", b)
            if had_work:
                self.toast(battery.message("resume", b or {}))
        if not battery.low(b):
            self._battery_warned = False  # warn again the next time it runs low
        self._refresh_sidebar()

    def retry_frame(self, quiet: bool = False) -> bool:
        """The queue waits for the Frame: reconnect, and continue the queue if it answers."""
        target = self.target
        if target is None:
            return False
        try:
            target.connect()
            info = target.describe()
        except Exception as exc:  # noqa: BLE001 - still away
            if not quiet:
                self.toast(explain(exc), error=True)
            return False
        self.frame_info, self.frame_state = info, "connected"
        self._check_frame_restart(info)
        self._awake_at = 0.0  # the lock went away with the old connection's Frame session: take it again
        self.jobs.resume()
        self.toast(tr("Your Frame is back: continuing"))
        self.refresh_view()
        return True

    def _backfill_covers(self) -> None:
        """Games without store art (2D Android apps) get a cover with their name and icon (once; cached)."""
        from ..artwork import steam

        made = 0
        for g in library.games():
            if g.get("kind") == "rift":
                continue
            try:
                had = (steam.fetch.artwork_dir(g["package"]) / "cover.jpg").exists()
                made += bool(steam.ensure_cover(g["package"])) and not had
            except Exception:  # noqa: BLE001 - artwork is optional
                applog.log.info("cover for %s failed", g.get("package"), exc_info=True)
        if made:
            self.refresh_view()

    def _keep_frame_awake(self) -> None:
        """Hold a wake lock on the Frame while jobs that use it run or wait (renewed every 30 min; it expires on its
        own after an hour if FramePort goes away), release it when they're done."""
        want = self.jobs.has_frame_work() and self.target is not None and self.frame_state == "connected" \
            and self.jobs.paused != "frame"  # paused for the battery: stay awake to see it charging
        now = time.time()
        held = getattr(self, "_awake_at", 0.0)
        if want == bool(held) and (not want or now - held < 1800) or getattr(self, "_awake_busy", False):
            return
        self._awake_busy = True
        target = self.target

        def work():
            try:
                r = target.frame.agent("keep_awake", on=want, minutes=60)
                self._awake_at = time.time() if want and r.get("awake") else 0.0
                applog.log.info("Frame wake lock: %s", r)
            except Exception as exc:  # noqa: BLE001 - only a convenience
                applog.log.info("Frame wake lock failed: %s", exc)
                self._awake_at = 0.0 if not want else time.time()  # don't retry on every event
            finally:
                self._awake_busy = False
        threading.Thread(target=work, daemon=True).start()

    def connect(self, target, password=None, quiet=False, devkit=False):
        """devkit: the Frame was found in Developer Mode; if FramePort's keys aren't on it yet, pair through Valve's
        devkit service (approve in the headset) instead of failing."""
        from ..frame.connection import save_target
        from ..targets.frame_lepton import FrameLeptonTarget

        self.frame_state = "connecting"
        self._refresh_sidebar()

        def work():
            try:
                try:
                    t = FrameLeptonTarget(target, password).connect()
                except ConnectionError as exc:
                    if not devkit or "authentication failed" not in str(exc).lower():
                        raise
                    from ..frame import devkit as dk

                    self.toast(tr("Pairing: on the Frame open Settings → Developer → Pair new host, then approve "
                                  "FramePort."))
                    dk.register(target.host)
                    t = FrameLeptonTarget(target, password).connect()
                if password:
                    t.frame.install_key()
                info = t.describe()
                target.name = info.get("hostname") or target.name
                t.label = target.label
                save_target(target)
                old, self.target, self.frame_info, self.frame_state = self.target, t, info, "connected"
                self._check_frame_restart(info)
                if old is not None and old is not t and not self.jobs.current():
                    try:  # the connection it replaces (a running job keeps using its own until it ends)
                        old.close()
                    except Exception:  # noqa: BLE001 - it's being dropped anyway
                        pass
                if not quiet:
                    self.toast(tr("Connected to {label}").format(label=target.label))
                self._not_paired = False
            except Exception as exc:  # noqa: BLE001
                from ..frame.connection import FrameNotPaired

                self.frame_state = "offline"
                self.frame_info = None
                # reachable, but it doesn't let FramePort in: the Frame page points at the first-time setup
                self._not_paired = isinstance(exc, FrameNotPaired)
                if not isinstance(exc, (FrameNotPaired, OSError)):  # unexpected: keep the traceback for reports
                    applog.log.exception("connect to %s failed", target.label)
                if not quiet:
                    self.toast(tr("Couldn't connect: {exc}").format(exc=explain(exc)), error=True)
            if self.route[0] in ("frame", "library", "game", "welcome"):
                self.refresh_view()
            else:
                self._refresh_sidebar()
        self.page.run_thread(work)

    def connect_manual(self, address: str, password: str | None):
        from ..frame.connection import parse_target

        if not (address or "").strip():
            self.toast(tr("Enter the Frame's address (e.g. steamos@frame.local or its IP)"), error=True)
            return
        try:
            target = parse_target(address)
        except ValueError as exc:
            self.toast(explain(exc), error=True)
            return
        self.connect(target, password or None)

    def refresh_frame(self, quiet: bool = False, rerender: bool = True, background: bool = True):
        def work():
            target = self.target
            if not target:
                return
            try:
                info = target.describe()
                if self.target is not target:
                    return  # reconnected meanwhile: that connection's state wins
                changed = info.get("installed") != (self.frame_info or {}).get("installed") or \
                    self.frame_state != "connected"
                self.frame_info, self.frame_state = info, "connected"
                self._check_frame_restart(info)
                self._check_sessions(info)
            except Exception as exc:  # noqa: BLE001
                frame = getattr(target, "frame", None)
                if frame is not None and frame.alive():
                    # the connection works, only this status query failed (e.g. an agent error): stay connected and
                    # keep the connection (a job may be using it)
                    applog.log.warning("Frame status refresh failed: %s", exc)
                    return
                if self.target is not target:
                    return
                changed = self.frame_state == "connected"
                self.frame_state, self.frame_info = "offline", None
                try:
                    target.close()
                except Exception:  # noqa: BLE001
                    pass
                if not quiet:
                    self.toast(tr("The Frame went offline: {exc}").format(exc=explain(exc)), error=True)
            if changed and rerender and self.route[0] in ("frame", "library", "game"):
                self.refresh_view()
            else:
                self._refresh_sidebar()
        if background:
            self.page.run_thread(work)
        else:
            work()

    def disconnect(self):
        self.stop_live()
        self.stop_keyboard()
        self.stop_monitor()
        if self.target:
            try:
                self.target.close()
            except Exception:  # noqa: BLE001
                pass
        self.target, self.frame_info, self.frame_state = None, None, "none"
        self.go("frame")

    def install_lepton(self):
        def run(job: Job):
            r = self.target.install_lepton()
            return tr("Lepton is installed") if r.get("installed") else \
                r.get("hint") or tr("Asked Steam to install Lepton")
        self.submit(tr("Install Lepton on the Frame"), run, kind="tool-frame")

    def install_proton(self):
        def go():
            from ..install.installer import ensure_proton

            def run(job: Job):
                tool = ensure_proton(self.target.frame, job.reporter)
                return tr("{display_name} is installed on your Frame").format(display_name=tool['display_name'])
            self.submit(tr("Install Proton on the Frame"), run, kind="tool-frame", open_panel=True)
        C.confirm(self.page, tr("Install Proton on the Frame?"),
                  tr("FramePort has Steam on the Frame download Proton and the runtime it needs (about 1 GiB). Steam "
                  "restarts once, which closes a running game."), tr("Install"), go)

    def test_proton(self):
        def run(job: Job):
            job.reporter.stage("Running a Windows program under Proton")
            r = self.target.proton_selftest()
            job.reporter.check("Windows program ran", bool(r.get("ran")), f"{r.get('seconds')} s")
            job.reporter.check("OpenXR bridge registered", bool(r.get("openxr_runtime")), r.get("openxr_runtime") or "")
            if not r.get("ran"):
                raise RuntimeError(tr("Proton couldn't run a test program (see the log)"))
            return tr("Proton works on your Frame ({tool})").format(tool=r['tool'])
        self.submit(tr("Test Proton on the Frame"), run, kind="tool-frame")

    def cleanup_frame(self):
        def go():
            def run(job: Job):
                r = self.target.frame.agent("cleanup", rollback=True, paths=[])
                return tr("Freed {value:.1f} GiB on the Frame").format(value=r['freed_bytes'] / 2**30)
            self.submit(tr("Free up space on the Frame"), run, kind="tool-frame")
        C.confirm(self.page, tr("Free up space?"), tr("Removes the previous version kept after each reinstall and any "
                  "leftover uploads. Games and saves aren't touched."), tr("Free up space"), go)

    # ================================================================== tools / welcome
    def update_tools(self, update: bool = False, quiet: bool = False) -> Job:
        from ..tools import toolchain

        def run(job: Job):
            rep = job.reporter
            rep.stage("Checking for updates" if update else "Downloading tools")
            statuses = toolchain.status(check_latest=update)
            todo = [s for s in statuses if not s.optional and (not s.installed or (update and s.latest and s.version
                                                                                      and s.latest != s.version))]
            for i, s in enumerate(todo):
                rep.stage(f"Installing {s.name}")
                installer = {"java": toolchain.install_java, "overport": toolchain.install_overport,
                             "apksigner": toolchain.install_apksigner}[s.name]
                installer(lambda f, i=i, name=s.name: rep.progress((i + f) / len(todo), name))
                rep.check(s.name, True, "ready")
            from ..patches import overport as op
            from ..tools import overport as ov
            from .views.settings import TOOL_TITLES

            try:
                op.refresh(ov.list_patches)
            except Exception:  # noqa: BLE001
                pass
            return tr("Tools are up to date") if not todo else \
                tr("Installed {join}").format(join=", ".join(TOOL_TITLES.get(s.name, s.name) for s in todo))
        return self.submit(tr("Update tools") if update else tr("Get FramePort ready"), run, kind="tools",
                           open_panel=not quiet)

    def uninstall_app(self) -> None:
        from .. import uninstall as un

        connected = self.frame_state == "connected" and self.target is not None
        pl = un.plan(self.frame_info if connected else None)
        from ..core import winhost

        backup_dir = un.default_backup_dir()
        shown_dir = winhost.to_windows(backup_dir) if winhost.is_wsl() else str(backup_dir)
        keys_label = tr("Back up the signing keys ({len}) to {shown_dir} first").format(len=len(pl.keys),
                                                                                         shown_dir=shown_dir)
        cb_keys = ft.Checkbox(label=keys_label,
                              value=bool(pl.keys), active_color=T.ACCENT)
        cb_frame = ft.Checkbox(label=tr("Also remove FramePort's games and files from the Frame") +
                               ("" if connected else tr(" (connect the Frame first)")), value=connected,
                               disabled=not connected, active_color=T.ACCENT)
        cb_saves = ft.Checkbox(label=tr("Keep game saves on the Frame"), value=True, active_color=T.ACCENT)
        frame_items = [i for i in pl.items if i.kind == "frame"]
        steam_items = [i for i in pl.items if i.kind == "steam"]
        other = [i for i in pl.items if i.kind == "pc"]
        lines = [C.body(f"• {i.what}" + (tr(" — {value:.1f} GiB").format(value=i.size / 2**30)
                                         if i.size > 2**28 else ""), T.TEXT_2)
                 for i in other]
        if steam_items:
            lines.append(C.body(tr_n("• {n} Steam shortcut on this PC for PC VR games",
                                     "• {n} Steam shortcuts on this PC for PC VR games", len(steam_items)), T.TEXT_2))
        if frame_items:
            size = sum(i.size for i in frame_items) / 2**30
            lines.append(C.body(tr_n("• On the Frame (if selected below): {n} game ({size}), "
                                     "its Steam entry and FramePort's files",
                                     "• On the Frame (if selected below): {n} games ({size}), "
                                     "their Steam entries and FramePort's files",
                                     len(frame_items), size=fmt_size(size * 2**30, 0)), T.TEXT_2))

        def go(e):
            self.page.pop_dialog()
            keys = backup_dir if cb_keys.value else None
            frame = self.target.frame if (cb_frame.value and connected) else None
            keep = cb_saves.value

            for pending in self.jobs.pending():  # nothing else may run after (or during) the uninstall
                self.jobs.cancel(pending)

            def run(job: Job):
                out = un.run(job.reporter, frame, keep, keys, remove_frame=frame is not None)
                self.target, self.frame_info, self.frame_state = None, None, "none"
                self.page.run_thread(lambda: self._uninstalled(out))
                return tr("FramePort was removed")
            self.submit(tr("Uninstall FramePort"), run, kind="uninstall-app", open_panel=True)
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text(tr("Uninstall FramePort?"), weight=ft.FontWeight.W_600),
            content=ft.Container(ft.Column([C.body(tr("This removes:"), T.TEXT), *lines, ft.Container(height=T.S2),
                                            cb_keys, cb_frame, cb_saves,
                                            C.meta(tr("Signing keys matter: game updates must be signed with "
                                                      "the same key or their saves are lost on reinstall."))],
                                           spacing=T.S2, tight=True, scroll=ft.ScrollMode.AUTO), width=T.px(560)),
            bgcolor=T.SURFACE_2, shape=ft.RoundedRectangleBorder(radius=T.RADIUS),
            actions=[C.ghost(tr("Cancel"), on_click=lambda e: self.page.pop_dialog()),
                     ft.FilledButton(tr("Uninstall"), icon=ft.Icons.DELETE_FOREVER_ROUNDED, on_click=go,
                                     style=ft.ButtonStyle(bgcolor=T.ERROR, color=T.ON_ACCENT,
                                                          shape=ft.RoundedRectangleBorder(radius=T.RADIUS_SM)))]))

    def _uninstalled(self, out: dict) -> None:
        async def close(e=None):
            try:
                await self.page.window.close()
            except Exception:  # noqa: BLE001
                pass
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(tr("FramePort was removed"), weight=ft.FontWeight.W_600),
            content=ft.Container(ft.Column([
                C.body(tr("All of FramePort's data on this PC is gone") +
                       (tr(", and your signing keys were saved to {backup}.").format(backup=out['backup'])
                        if out.get("backup") else ".")),
                C.body(tr("To finish, close FramePort and delete its program folder.")),
            ], spacing=T.S2, tight=True), width=T.px(520)),
            bgcolor=T.SURFACE_2, actions=[C.primary(tr("Close FramePort"), on_click=close)]))

    def finish_welcome(self):
        library.set_setting("ui.welcome_done", True)
        self.go("library")

    # ================================================================== compatibility (scripts/ui_smoke.py)
    @property
    def job_running(self) -> bool:
        return self.jobs.current() is not None or bool(self.jobs.pending())

    def start_job(self, package: str, build: bool = True, install: bool = False, test: bool | None = None,
                  to: str = "frame"):
        if install:
            return self.install(package, to)
        if test or test is None and not build:
            return self.test_game(package, to)
        return self.build_game(package)


def assets_dir() -> str:
    """The GUI's assets folder is the user data dir, so artwork thumbnails load by URL (/artwork/<pkg>/…)."""
    from ..core.paths import user_data_dir

    return str(user_data_dir())


def serialize_flet_updates() -> None:
    """Send page updates one at a time. Flet 1.0 diffs and sends a control's patch without a lock, and FramePort
    updates the page from several threads (jobs, connection checks, the library loader, dialogs): two patches computed
    at once desynchronised the window ("dropped a patch for unknown control … needs a reload"), e.g. a dialog shown
    while a finished scan redrew the view never closed again."""
    from flet.messaging.session import Session

    if getattr(Session.patch_control, "_serialized", False):
        return
    original, lock = Session.patch_control, threading.RLock()  # re-entrant: did_mount() may update again

    def patch_control(self, *args, **kwargs):
        with lock:
            return original(self, *args, **kwargs)
    patch_control._serialized = True
    Session.patch_control = patch_control


def main(argv=None):
    import sys

    from .. import urlhandler

    args = list(sys.argv[1:] if argv is None else argv)
    links = [a for a in args if "://" in a or a.lower().endswith(".json")]
    for link in links:  # `frameport-gui <install link>` (source/wheel installs; bundles go through the handler script)
        urlhandler.drop_link(link)
    if links and urlhandler.app_running():
        return  # the open window takes it
    applog.setup("gui")
    serialize_flet_updates()
    from ..core import library
    from .updater import apply_pending_at_start

    if apply_pending_at_start():  # "Install updates automatically": the new version starts instead of this one
        return
    T.set_scale(T.scale_from_setting(library.setting("ui.scale", "auto")))  # before any view is built
    applog.log.info("ui scale %.2f", T.SCALE)
    from .. import updates
    from ..artwork import thumbs

    if updates.install_kind() == "bundle":  # a packaged app finds artwork by file path, not by URL (GitHub #16)
        thumbs.use_file_paths(True)
    ft.run(lambda page: FramePortApp(page), assets_dir=assets_dir())


if __name__ == "__main__":
    main()
