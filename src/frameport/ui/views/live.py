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

from ...errors import explain
from ...i18n import tr, tr_n
from .. import components as C
from .. import glyphs as G
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
        self.open_btn = C.secondary(tr("Open viewer"), ft.Icons.OPEN_IN_NEW_ROUNDED, self._open)
        self.stop_btn = C.ghost(tr("Stop"), ft.Icons.STOP_ROUNDED, self._stop_click)
        self.url = C.meta("", selectable=True)
        self._push = None  # components.LoopUpdater (updates from background threads; a direct update dropped patches)

    # ---------------------------------------------------------------- building
    def mount(self) -> ft.Control:
        app = self.app
        heading, sub = tr("Live view"), tr("Watch what the headset shows, in a window on this PC")
        if not (app.target and app.frame_state == "connected"):
            return ft.Column([
                app.top_bar(heading, sub),
                C.empty_state(G.LIVE, tr("Connect your Frame first"),
                              tr("The live view can start once FramePort is connected to the Frame."),
                              C.primary(tr("Connect"), ft.Icons.LINK_ROUNDED, lambda e: app.go("frame")))], expand=True)
        if self.root is None:
            parts = C.transit_parts()
            h = T.px(56)
            self.idle = ft.Container(ft.Column([
                ft.Row([ft.Image(src=parts["portal"], height=h, width=h * 24 / 50, fit=ft.BoxFit.CONTAIN),
                        ft.Image(src=parts["headset"], height=h * 0.86, fit=ft.BoxFit.CONTAIN)],
                       spacing=T.S3, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                C.body(tr("Start the live view to watch the headset in your browser"), T.TEXT_2,
                       text_align=ft.TextAlign.CENTER),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=T.S4, tight=True),
                alignment=ft.Alignment.CENTER, expand=True, padding=T.S6)
            self.root = ft.Column([
                app.top_bar(heading, sub),
                ft.Row([C.meta(tr("The headset's view and sound open in your web browser. Stop the stream when "
                                  "you're done."), T.TEXT_2), C.help_icon("live_view", 16)],
                       spacing=T.px(2), tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                # status, Start/Open/Stop and Quality in one bar
                C.card(ft.Column([
                    ft.Row([self.dot, ft.Column([self.state, self.detail], spacing=T.px(2), expand=True),
                            self.quality_dd, self.start_btn, self.open_btn, self.stop_btn],
                           spacing=T.S3, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    self.url,
                ], spacing=T.S2)),
                self.idle,
            ], spacing=T.S4, horizontal_alignment=ft.CrossAxisAlignment.STRETCH, expand=True)
        self._refresh(update=False)
        self._ensure_ticker()
        return self.root

    def _refresh(self, update: bool = True) -> None:
        live = self.live
        running = bool(live and live.running)
        if live is None:
            self.dot.bgcolor, self.state.value = T.TEXT_3, tr("Not streaming")
            self.detail.value = tr("Starts a video stream on the Frame and opens it in your browser.")
        else:
            st = live.status()
            ok = running and st.get("ready")
            warning = health_text(st) if ok else ""
            self.dot.bgcolor = T.WARN if warning else T.OK if ok else T.ERROR if st.get("ended") else T.WARN
            self.state.value = status_text(st)
            self.detail.value = (warning if ok else "" if st.get("ended")
                                 else tr("The first picture takes a few seconds."))
        self.start_btn.visible = not running
        self.start_btn.disabled = self._busy
        self.open_btn.visible = self.stop_btn.visible = running
        self.quality_dd.disabled = running or self._busy
        self.url.value = (tr("Viewer address on this PC: {url}").format(url=live.url) if running else "")
        self.url.visible = running
        self.idle.visible = live is None
        if update:  # called from the ticker / start / stream-end threads: send through Flet's event loop
            if self._push is None:
                self._push = C.LoopUpdater(self.app.page)
            self._push(self.dot, self.state, self.detail, self.start_btn, self.open_btn, self.stop_btn,
                       self.quality_dd, self.url, self.idle)

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
            self.live.on_end = lambda why: self._refresh()
        except Exception as exc:  # noqa: BLE001
            self.live = None
            self.app.toast(tr("Couldn't start the live view: {exc}").format(exc=explain(exc)), error=True)
        finally:
            self._busy = False
        self._refresh()
        if self.live:
            self._open()

    def _open(self, e=None) -> None:
        if self.live and self.live.running:
            self.app.open_url(self.live.url)

    def _stop_click(self, e=None) -> None:
        self.app.run_bg(self.stop)

    def stop(self) -> None:
        """Stop streaming (also called when the Frame disconnects or the app closes)."""
        live, self.live = self.live, None
        if live:
            live.stop()
        self._refresh()
