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
    """One line for the relay's status (livestream.Relay.status)."""
    if st.get("ended"):
        return tr("Stopped: {why}").format(why=st["ended"])
    if not st.get("ready"):
        return tr("Starting the stream on the Frame…")
    parts = [tr("Live")]
    if st.get("width"):
        parts.append(f"{st['width']}×{st['height']}")
    secs = max(st.get("seconds") or 0, 1)
    parts.append(tr("{rate}/s").format(rate=human(int((st.get("bytes") or 0) / secs))))
    n = st.get("viewers") or 0
    parts.append(tr("no viewer open") if not n else tr_n("{n} viewer", "{n} viewers", n))
    return " · ".join(parts)


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
        self.dot = C.dot(T.TEXT_3, 10)
        self.state = C.body(tr("Not streaming"), T.TEXT, weight=ft.FontWeight.W_500)
        self.detail = C.meta("")
        self.quality_dd = ft.Dropdown(label=tr("Quality"), value=self.quality, width=T.px(260), dense=True,
                                      options=[ft.DropdownOption(key=k, text=t) for k, t in QUALITIES],
                                      on_select=self._set_quality)
        self.start_btn = C.primary(tr("Start live view"), ft.Icons.PLAY_ARROW_ROUNDED, self._start, big=True)
        self.open_btn = C.secondary(tr("Open viewer"), ft.Icons.OPEN_IN_NEW_ROUNDED, self._open)
        self.stop_btn = C.ghost(tr("Stop"), ft.Icons.STOP_ROUNDED, self._stop_click)
        self.url = C.meta("", selectable=True)

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
            self.root = ft.Column([
                app.top_bar(heading, sub),
                C.card(ft.Column([
                    ft.Row([self.dot, self.state], spacing=T.S2,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    self.detail,
                    ft.Container(height=T.S2),
                    ft.Row([self.start_btn, self.open_btn, self.stop_btn, self.quality_dd],  # (no expand child: wraps)
                           spacing=T.S3, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    self.url,
                ], spacing=T.S2)),
                C.callout(tr("The picture opens in your default web browser, where it plays smoothly and can go "
                             "full screen. It shows the headset's view with its sound "
                             "(click Sound on in the player) whatever is running: Steam's menus, SteamVR or a game; "
                             "while the headset sleeps the picture is black and updates about once a second. "
                             "Streaming costs the Frame a little performance; "
                             "stop it when you're done.")),
            ], spacing=T.S4, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
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
            st = live.relay.status()
            ok = running and st.get("ready")
            self.dot.bgcolor = T.OK if ok else T.ERROR if st.get("ended") else T.WARN
            self.state.value = status_text(st)
            self.detail.value = "" if ok or st.get("ended") else tr("The first picture takes a few seconds.")
        self.start_btn.visible = not running
        self.start_btn.disabled = self._busy
        self.open_btn.visible = self.stop_btn.visible = running
        self.quality_dd.disabled = running or self._busy
        self.url.value = (tr("Viewer address on this PC: {url}").format(url=live.url) if running else "")
        self.url.visible = running
        if update:
            C.update(self.dot, self.state, self.detail, self.start_btn, self.open_btn, self.stop_btn,
                     self.quality_dd, self.url)

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
