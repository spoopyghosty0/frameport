"""Live view: watch what the Frame shows, in a browser window on this PC.

Flet can't play video outside packaged builds, so this tab only starts and stops the stream (install/livestream.py)
and opens the player page in the user's browser. Persistent: the stream keeps running while other tabs are open; the
status line is refreshed once a second while the tab is shown (properties only, no new controls).
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import flet as ft

from ...core import applog, library
from ...errors import explain
from ...i18n import tr, tr_n
from .. import components as C
from .. import glyphs as G
from .. import live_players as LP
from .. import theme as T
from .files_dialog import human

if TYPE_CHECKING:
    from ..app import FramePortApp

QUALITIES = [("360p", tr("360p (lightest)")), ("480p", "480p"), ("720p", tr("720p (recommended)")),
             ("1080p", "1080p"), ("full", tr("Full (headset view size)"))]


def status_text(st: dict) -> str:
    """One line for the stream's status (livestream.LiveStream.status)."""
    if st.get("ended"):
        return tr("Stopped: {why}").format(why=st["ended"])
    if not st.get("ready"):
        return tr("Starting the stream on the Frame…")
    parts = [tr("Live")]
    if st.get("width"):
        parts.append(f"{st['width']}×{st['height']}")
    if st.get("fps"):
        parts.append(tr("{fps} fps").format(fps=st["fps"]))
    if st.get("encoder"):
        parts.append(tr("hardware encoder") if st["encoder"] == "hardware" else tr("software encoder"))
    secs = max(st.get("seconds") or 0, 1)
    parts.append(tr("{rate}/s").format(rate=human(int((st.get("bytes") or 0) / secs))))
    n = st.get("viewers") or 0
    parts.append(tr("no viewer open") if not n else tr_n("{n} viewer", "{n} viewers", n))
    return " · ".join(parts)


def health_text(st: dict) -> str:
    """A warning when the Frame dropped frames in the encoder's last 10 s (hardware encoder only), else ""."""
    if not st.get("dropping") or st.get("ended"):
        return ""
    return tr_n("The Frame can't keep up at this quality: {n} frame dropped in the last 10 seconds. "
                "Choose a lower quality for a smoother picture.",
                "The Frame can't keep up at this quality: {n} frames dropped in the last 10 seconds. "
                "Choose a lower quality for a smoother picture.", st.get("dropped") or 0)


def live_features() -> list[tuple[str, str, str]]:
    """What the live view does (its empty states)."""
    return [(G.LIVE, tr("What the player sees"),
             tr("The headset's view on this PC, for watching along or recording.")),
            (ft.Icons.VOLUME_UP_ROUNDED, tr("With sound"), tr("The game's audio streams along with the picture.")),
            (ft.Icons.MEMORY_ROUNDED, tr("Light on the game"),
             tr("The Frame's own video encoder does the work where it can."))]


VIDEO_START_S = 12  # the in-window player must show a moving picture within this, else the next player takes over
VIDEO_REOPENS = 3  # times a minute the in-window player reconnects after an unexpected end of the stream


def player_label(mode: str) -> str:
    return {"auto": tr("Automatic"), "app": tr("In this window"), "mpv": tr("mpv window"),
            "browser": tr("Web browser")}[mode]


def where_text(mode: str | None) -> str:
    """Where a running stream is shown (the bar's detail line)."""
    return {"app": tr("Playing below, in this window."), "mpv": tr("Playing in an mpv window."),
            "browser": tr("Playing in your web browser.")}.get(mode or "", "")


def default_quality() -> str:
    from ...install.livestream import DEFAULT_QUALITY

    return DEFAULT_QUALITY


class LiveView:
    def __init__(self, app: FramePortApp):
        self.app = app
        self.live = None  # install.livestream.LiveStream while streaming
        self.quality = default_quality()
        self._busy = False
        self._ticker: threading.Thread | None = None
        self.root = None
        self.idle = None  # the "start the live view" hint below the bar while not streaming
        self.dot = C.dot(T.TEXT_3, 10)
        self.state = C.body(tr("Not streaming"), T.TEXT, weight=ft.FontWeight.W_500)
        self.detail = C.meta("")
        self.quality_dd = C.dropdown(label=tr("Quality"), value=self.quality, width=T.px(230),
                                      options=[ft.DropdownOption(key=k, text=t) for k, t in QUALITIES],
                                      on_select=self._set_quality)
        self.start_btn = C.primary(tr("Start live view"), ft.Icons.PLAY_ARROW_ROUNDED, self._start)
        self.open_btn = C.secondary(tr("Open in browser"), ft.Icons.OPEN_IN_NEW_ROUNDED, self._open)
        self.stop_btn = C.ghost(tr("Stop"), ft.Icons.STOP_ROUNDED, self._stop_click)
        self.sound_btn = C.ghost(tr("Sound off"), ft.Icons.VOLUME_OFF_ROUNDED, self._toggle_sound)
        self.window_btn = C.ghost(tr("Show the mpv window"), ft.Icons.OPEN_IN_NEW_ROUNDED, self._reopen_mpv)
        self.url = C.meta("", selectable=True)
        self._push = None  # components.LoopUpdater (updates from background threads; a direct update dropped patches)
        # where the stream plays (ui/live_players): the user's pick, what's available here, what plays it now
        self.player = library.setting(LP.SETTING, "auto")
        self.embedded_ok: bool | None = None  # decided at the first mount (needs page.web)
        self.mpv_exe: str | None = None
        self.mode: str | None = None  # "app" | "mpv" | "browser" while streaming
        self.mpv = None  # live_players.MpvWindow
        self.video = None  # the flet-video control while it plays in the window
        self.video_error = ""  # its last error message (logged; the watchdog decides about falling back)
        self._video_reopens: list[float] = []  # when it reconnected (VIDEO_REOPENS a minute)
        self.muted = False
        self.screen = ft.Container(expand=True, visible=False, bgcolor="#000000", border_radius=T.RADIUS,
                                   clip_behavior=ft.ClipBehavior.ANTI_ALIAS, border=ft.Border.all(1, T.BORDER))
        self.player_dd = None  # built at the first mount (its options depend on what's available)

    # ---------------------------------------------------------------- building
    def mount(self) -> ft.Control:
        app = self.app
        heading, sub = tr("Live view"), tr("Watch what the headset shows, in a window on this PC")
        if not (app.target and app.frame_state == "connected"):
            return ft.Column([
                app.top_bar(heading, sub),
                C.empty_state(G.LIVE, tr("See what the headset sees"),
                              tr("Connect to your Steam Frame to stream its view and sound to this PC."),
                              C.primary(tr("Connect"), ft.Icons.LINK_ROUNDED, lambda e: app.go("frame")),
                              features=live_features())], expand=True)
        if self.embedded_ok is None:
            self.embedded_ok = LP.embedded_available(web=bool(getattr(app.page, "web", False)))
            self.mpv_exe = LP.find_mpv()
            applog.log.info("live view players: in the window %s, mpv %s", self.embedded_ok, self.mpv_exe or "no")
        if self.root is None:
            avail = [m for m, ok in (("app", self.embedded_ok), ("mpv", bool(self.mpv_exe)), ("browser", True)) if ok]
            if self.player not in ("auto", *avail):
                self.player = "auto"
            self.player_dd = C.dropdown(label=tr("Play in"), value=self.player, width=T.px(190),
                                        options=[ft.DropdownOption(key=m, text=player_label(m))
                                                 for m in ("auto", *avail)],
                                        on_select=self._set_player)
            self.where = C.meta("", T.TEXT_2)
            self.idle_text = C.body("", T.TEXT_2, text_align=ft.TextAlign.CENTER)
            self.idle_title = C.title(tr("Ready when you are"), T.T_DISPLAY)
            # while it plays in the browser or an mpv window, the stage says so (the page isn't left empty)
            self.idle_open = C.primary(tr("Open in browser"), ft.Icons.OPEN_IN_NEW_ROUNDED, self._open, big=True)
            parts = C.transit_parts()
            h = T.px(88)
            self.idle_start = C.primary(tr("Start live view"), ft.Icons.PLAY_ARROW_ROUNDED, self._start, big=True)
            # the "screen" the stream would fill: the portal and the headset on the portal's light
            stage = ft.Container(ft.Column([
                ft.Row([C.portal_image(parts["portal"], h),
                        ft.Image(src=parts["headset"], height=h * 0.86, fit=ft.BoxFit.CONTAIN)],
                       spacing=T.S4, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Container(height=T.S2),
                self.idle_title,
                self.idle_text,
                ft.Container(height=T.S2),
                self.idle_start,
                self.idle_open,
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=T.S2, tight=True),
                alignment=ft.Alignment.CENTER, expand=True, padding=T.S6, gradient=C.portal_glow(),
                bgcolor=T.soft("#000000", 0.25), border=ft.Border.all(1, T.BORDER), border_radius=T.RADIUS)
            self.idle = ft.Column([stage, C.feature_row(live_features())], spacing=T.S4, expand=True,
                                  horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
            self.root = ft.Column([
                app.top_bar(heading, sub),
                ft.Row([self.where, C.help_icon("live_view", 16)],
                       spacing=T.px(2), tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                # two rows: the status with the actions; where it plays, Quality and the viewer address (one row
                # squeezed the status into a column of single words while streaming)
                C.card(ft.Column([
                    ft.Row([self.dot, ft.Column([self.state, self.detail], spacing=T.px(2), expand=True),
                            self.start_btn, self.sound_btn, self.window_btn, self.open_btn, self.stop_btn],
                           spacing=T.S3, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Row([self.player_dd, self.quality_dd, ft.Container(self.url, expand=True)],
                           spacing=T.S3, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ], spacing=T.S3)),
                self.idle,
                self.screen,  # the stream, when it plays in this window
                ft.Container(height=T.S2),  # the body has no bottom padding: keep the screen off the window's edge
            ], spacing=T.S4, horizontal_alignment=ft.CrossAxisAlignment.STRETCH, expand=True)
        self._refresh(update=False)
        self._ensure_ticker()
        return self.root

    def _refresh(self, update: bool = True) -> None:
        self.app.sync_on_air()  # the sidebar's "ON AIR" sign (easter egg)
        if self.root is None:  # not built yet (e.g. stop() on a disconnect before the tab was opened)
            return
        live = self.live
        running = bool(live and live.running)
        first = LP.order(self.player, bool(self.embedded_ok), bool(self.mpv_exe))[0]
        if live is None:
            self.dot.bgcolor, self.state.value = T.TEXT_3, tr("Not streaming")
            self.detail.value = {"app": tr("Starts a video stream on the Frame and shows it here."),
                                 "mpv": tr("Starts a video stream on the Frame and opens it in an mpv window.")
                                 }.get(first, tr("Starts a video stream on the Frame and opens it in your browser."))
        else:
            st = live.status()
            ok = running and st.get("ready")
            warning = health_text(st) if ok else ""
            self.dot.bgcolor = T.WARN if warning else T.OK if ok else T.ERROR if st.get("ended") else T.WARN
            self.state.value = status_text(st)
            self.detail.value = (warning or where_text(self.mode) if ok else "" if st.get("ended")
                                 else tr("The first picture takes a few seconds."))
        self.where.value = {
            "app": tr("The headset's view and sound play right here. Open it in your browser any time."),
            "mpv": tr("The headset's view and sound open in an mpv window. Open it in your browser any time."),
        }.get(self.mode if running else first,
              tr("The headset's view and sound open in your web browser. Stop the stream when you're done."))
        self.idle_text.value = {"app": tr("Start the live view to watch the headset here."),
                                "mpv": tr("Start the live view to watch the headset in an mpv window.")
                                }.get(first, tr("Start the live view to watch the headset in your browser."))
        self.start_btn.visible = not running
        self.start_btn.disabled = self.idle_start.disabled = self._busy
        self.open_btn.visible = self.stop_btn.visible = running
        self.sound_btn.visible = running and self.mode == "app"
        self.sound_btn.content = tr("Sound on") if self.muted else tr("Sound off")
        self.sound_btn.icon = ft.Icons.VOLUME_UP_ROUNDED if self.muted else ft.Icons.VOLUME_OFF_ROUNDED
        self.window_btn.visible = running and self.mode == "mpv" and not (self.mpv and self.mpv.alive)
        self.quality_dd.disabled = self._busy  # (while streaming: applies at the next start; disabled looked outlined)
        self.url.value = (tr("Viewer address on this PC: {url}").format(url=live.url) if running else "")
        self.url.visible = running
        elsewhere = running and self.mode in ("browser", "mpv")
        self.idle.visible = live is None or elsewhere
        self.idle_start.visible = live is None
        self.idle_open.visible = elsewhere
        self.idle_title.value = (tr("Streaming to an mpv window") if elsewhere and self.mode == "mpv" else
                                 tr("Streaming to your browser") if elsewhere else tr("Ready when you are"))
        if elsewhere:
            self.idle_text.value = (tr("The headset's view is open in an mpv window. You can also watch it in your "
                                       "browser.") if self.mode == "mpv" else
                                    tr("The headset's view is open in your browser. Closed the tab? Open it again."))
        self.screen.visible = running and self.mode == "app"
        if update:  # called from the ticker / start / stream-end threads: send through Flet's event loop
            if self._push is None:
                self._push = C.LoopUpdater(self.app.page)
            self._push(self.dot, self.state, self.detail, self.where, self.start_btn, self.open_btn, self.stop_btn,
                       self.sound_btn, self.window_btn, self.quality_dd, self.url, self.idle, self.idle_text,
                       self.idle_start, self.idle_open, self.idle_title, self.screen)

    def _ensure_ticker(self) -> None:
        if self._ticker and self._ticker.is_alive():
            return

        def tick():
            while self.app.route[:1] == ("live",):
                self._refresh()
                time.sleep(1)

        self._ticker = threading.Thread(target=tick, name="live-status", daemon=True)
        self._ticker.start()

    # ---------------------------------------------------------------- actions
    def _set_quality(self, e) -> None:
        self.quality = e.control.value or default_quality()

    def _set_player(self, e) -> None:
        """Play in…: remembered; while streaming, the stream moves there now."""
        self.player = e.control.value or "auto"
        library.set_setting(LP.SETTING, self.player)
        if self.live and self.live.running:
            self.app.run_bg(self._switch_player)
        else:
            self._refresh()

    def _switch_player(self) -> None:
        self._dismiss_player()
        self._present()

    def _start(self, e=None) -> None:
        if self._busy or (self.live and self.live.running):
            return
        self._busy = True
        self._refresh()
        self.app.run_bg(self._start_bg)

    def _start_bg(self) -> None:
        from ...install import livestream

        try:
            if self.live:
                self.live.stop()
            self.live = livestream.start(self.app.target.frame, quality=self.quality)
            self.live.on_end = lambda why: self._on_stream_end()
        except Exception as exc:  # noqa: BLE001
            self.live = None
            self.app.toast(tr("Couldn't start the live view: {exc}").format(exc=explain(exc)), error=True)
        finally:
            self._busy = False
        self._refresh()
        if self.live:
            self._present()

    def _present(self, tried: tuple[str, ...] = ()) -> None:
        """Show the running stream in the first player that works here (the user's pick first), falling back to the
        browser. Runs in a background thread (mpv is given a moment to show it could open the stream)."""
        live = self.live
        if not (live and live.running):
            return
        for mode in LP.order(self.player, bool(self.embedded_ok), bool(self.mpv_exe)):
            if mode in tried:
                continue
            try:
                if mode == "app":
                    self._show_in_window(live)
                elif mode == "mpv":
                    self.mpv = LP.MpvWindow(self.mpv_exe, LP.stream_url(live.url), tr("FramePort live view")).start()
                else:
                    self.app.open_url(live.url)
            except Exception as exc:  # noqa: BLE001 - try the next one
                applog.log.warning("live view: the %s player failed: %s", mode, exc)
                tried = (*tried, mode)
                continue
            self.mode = mode
            if tried:  # the user's (or the first) choice didn't work: say where it went instead
                self.app.toast(tr("The live view couldn't play {first}, so it opened {where}.").format(
                    first=self._place(tried[0]), where=self._place(mode)))
            break
        self._refresh()

    @staticmethod
    def _place(mode: str) -> str:
        return {"app": tr("in this window"), "mpv": tr("in an mpv window"), "browser": tr("in your browser")}[mode]

    def _show_in_window(self, live) -> None:
        self.video_error = ""
        self.video = LP.video_control(LP.stream_url(live.url), on_error=self._video_error,
                                      on_complete=self._video_complete, muted=self.muted)
        self.screen.content = self.video
        self.app.page.run_task(self._watch_video, self.video)

    def _video_error(self, e) -> None:
        """mpv's error messages: some don't stop playback, so they are only logged (the watchdog decides)."""
        self.video_error = str(getattr(e, "data", "") or "")
        applog.log.info("live view: in-window player said: %s", self.video_error)

    def _video_complete(self, e) -> None:
        """media_kit reports `completed` changes, starting with false: only true means the stream ended. While the
        stream still runs, the player reconnects (a new viewer starts at a keyframe, as the browser page does), at
        most VIDEO_REOPENS times a minute; after that the next player takes over."""
        if getattr(e, "data", None) not in (True, "true"):
            return
        live = self.live
        if self.mode != "app" or not (live and live.running):
            return
        now = time.time()
        self._video_reopens = [t for t in self._video_reopens if now - t < 60]
        if len(self._video_reopens) < VIDEO_REOPENS:
            self._video_reopens.append(now)
            applog.log.info("live view: the in-window player reached the stream's end while it runs: reconnecting "
                            "(%d in the last minute)", len(self._video_reopens))
            self._show_in_window(live)
            self._refresh()
            return
        self._video_failed(e)

    async def _watch_video(self, video) -> None:
        """The in-window player must show progress (its position moving) within VIDEO_START_S, else the next player
        takes over. Once it plays, the watch ends (stream ends come as `complete`)."""
        import asyncio

        start, first = time.time(), None
        while self.video is video:
            await asyncio.sleep(2)
            try:
                ms = (await video.get_current_position()).in_milliseconds
            except Exception:  # noqa: BLE001 - not mounted yet / player not ready
                ms = None
            if ms is not None:
                if first is None:
                    first = ms
                elif ms > first:
                    applog.log.info("live view: playing in the window (%.1f s in)", time.time() - start)
                    return
            if time.time() - start > VIDEO_START_S:
                applog.log.warning("live view: the in-window player showed no picture in %d s (%s)",
                                   VIDEO_START_S, self.video_error or "no error reported")
                self._video_failed()
                return

    def _video_failed(self, e=None) -> None:
        """The in-window player reported an error or stopped while the stream still runs: next player."""
        if self.mode != "app" or not (self.live and self.live.running):
            return
        applog.log.warning("live view: the in-window player stopped (%s)", getattr(e, "data", ""))
        self._dismiss_player()
        self.app.run_bg(self._present, ("app",))

    def _dismiss_player(self) -> None:
        if self.mpv:
            self.mpv.stop()
            self.mpv = None
        if self.video is not None:
            self.screen.content, self.video = None, None
        self.mode = None
        self._refresh()

    def _toggle_sound(self, e=None) -> None:
        self.muted = not self.muted
        if self.video is not None:
            self.video.muted = self.muted
            C.update(self.video)
        self._refresh()

    def _reopen_mpv(self, e=None) -> None:
        """The mpv window was closed: open it again (or fall back to the browser)."""
        self.app.run_bg(self._present, ("app",))

    def _on_stream_end(self) -> None:
        self._dismiss_player()

    def _open(self, e=None) -> None:
        if self.live and self.live.running:
            self.app.open_url(self.live.url)

    def _stop_click(self, e=None) -> None:
        self.app.run_bg(self.stop)

    def stop(self) -> None:
        """Stop streaming (also called when the Frame disconnects or the app closes)."""
        live, self.live = self.live, None
        self._dismiss_player()
        if live:
            live.stop()
        self._refresh()
