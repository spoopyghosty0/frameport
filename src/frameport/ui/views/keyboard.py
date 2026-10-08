"""Type on Frame: this computer's keyboard becomes a keyboard on the Frame while the tab is open (frame/keyboard.py).
Keys go to whatever has focus on the Frame: an app's text field, Steam, the desktop, a PC game.

Persistent like the other tabs: the virtual keyboard is created when the tab is shown and removed when another tab is
opened or the Frame disconnects (app.go / app.disconnect call stop()), so no keyboard lingers on the Frame.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import flet as ft

from ...errors import explain
from ...i18n import tr
from .. import components as C
from .. import glyphs as G
from .. import theme as T

if TYPE_CHECKING:
    from ..app import FramePortApp


class KeyboardView:
    def __init__(self, app: FramePortApp):
        self.app = app
        self.session = None  # frame.keyboard.KeyboardSession while the tab is open
        self._connecting = False
        self._gen = 0  # bumped by stop(): a connect that finishes after the tab was left closes its session
        self.stopped = True  # by stop() (tab left, Frame gone): the next mount connects again
        self.root = None
        self.status = C.meta("")
        self.last = ft.Text("", size=T.px(28), weight=ft.FontWeight.W_600, color=T.ACCENT)
        hint = C.body(tr("Click here, then type. Everything you type goes to the Frame (Esc and shortcuts too)."),
                      T.TEXT)
        self.pad = ft.Container(ft.Column([hint, self.last], spacing=T.S2,
                                          horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                                padding=T.S5, border_radius=T.RADIUS, bgcolor=T.BG, border=ft.Border.all(1, T.BORDER),
                                alignment=ft.Alignment.CENTER, height=T.px(180), ink=True,
                                on_click=lambda e: self.focus_keys())
        self.listener = ft.KeyboardListener(self.pad, autofocus=True, on_key_down=self._forward("down"),
                                            on_key_up=self._forward("up"), on_key_repeat=self._forward("repeat"))
        self.paste = C.field(hint_text=tr("Or paste text to type it in one go"), expand=True,
                             on_submit=self._send_text)
        self.reconnect = C.secondary(tr("Reconnect"), ft.Icons.REFRESH_ROUNDED, lambda e: self.start())

    # ---------------------------------------------------------------- building
    def mount(self) -> ft.Control:
        app = self.app
        heading, sub = tr("Type on Frame"), tr("Use this PC's keyboard on the Frame")
        if not (app.target and app.frame_state == "connected"):
            return ft.Column([
                app.top_bar(heading, sub),
                C.empty_state(G.KEYS, tr("Connect your Frame first"),
                              tr("Typing on the Frame works once FramePort is connected to it."),
                              C.primary(tr("Connect"), ft.Icons.LINK_ROUNDED, lambda e: app.go("frame")))], expand=True)
        if self.root is None:
            self.root = ft.Column([
                app.top_bar(heading, sub),
                C.card(ft.Column([
                    ft.Row([self.status, self.reconnect], spacing=T.S3,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    self.listener,
                    ft.Row([self.paste, C.secondary(tr("Type it"), ft.Icons.SEND_OUTLINED, self._send_text)],
                           spacing=T.S2),
                    ft.Row([C.meta(tr("In the headset, select a text field, then type here."), T.TEXT_2),
                            C.help_icon("type_on_frame", 16)], spacing=T.px(2), tight=True,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ], spacing=T.S3)),
            ], spacing=T.S4, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.start()
        return self.root

    def _set_status(self, text: str, color: str, update: bool = True) -> None:
        self.status.value, self.status.color = text, color
        self.reconnect.visible = color == T.ERROR
        if update:
            C.update(self.status, self.reconnect)

    # ---------------------------------------------------------------- session
    def start(self) -> None:
        """Connect the virtual keyboard (in the background) unless it is connected or connecting."""
        self.app._typing_on_frame = True  # app._on_key leaves Esc / Ctrl+F alone: they go to the Frame
        self.stopped = False
        if self.session is not None or self._connecting:
            return
        self._connecting = True
        self._set_status(tr("Connecting the keyboard…"), T.TEXT_2, update=self.root is not None)
        gen, target = self._gen, self.app.target
        self.app.run_bg(self._connect, gen, target)

    def _connect(self, gen: int, target) -> None:
        from ...frame.keyboard import KeyboardSession

        try:
            session = KeyboardSession(target.frame)
        except Exception as exc:  # noqa: BLE001
            self._connecting = False
            if gen == self._gen:
                self._fail(exc)
            return
        self._connecting = False
        if gen != self._gen:  # the tab was left while connecting
            session.close()
            return
        self.session = session
        self._set_status(tr("Keyboard connected to {label}.").format(label=target.label), T.OK)
        self.focus_keys()

    def stop(self) -> None:
        """Remove the keyboard from the Frame (leaving the tab, disconnecting, closing the app)."""
        self._gen += 1
        self.stopped = True
        self.app._typing_on_frame = False
        session, self.session = self.session, None
        if session is not None:
            session.close()

    def _fail(self, exc) -> None:
        session, self.session = self.session, None
        if session is not None:
            session.close()
        self._set_status(tr("Keyboard disconnected: {error}").format(error=explain(exc)), T.ERROR)

    # ---------------------------------------------------------------- input
    def _forward(self, action: str):
        def handler(e):
            s = self.session
            if s is None:
                return
            try:
                if s.key(e.key, action) and action == "down":
                    self.last.value = e.key if len(e.key) > 1 else e.key.upper()
                    C.update(self.last)
            except Exception as exc:  # noqa: BLE001 - the connection dropped
                self._fail(exc)
        return handler

    def press(self, key: str) -> None:
        """Simulate a key press (ui_smoke screenshots)."""
        self.listener.on_key_down(SimpleNamespace(key=key))

    def focus_keys(self) -> None:
        try:
            self.app.page.run_task(self.listener.focus)
        except Exception:  # noqa: BLE001
            pass

    def _send_text(self, e=None) -> None:
        s, text = self.session, self.paste.value or ""
        if s is None or not text:
            return

        def work():
            skipped = s.text(text)
            self.paste.value = ""
            C.update(self.paste)
            self.focus_keys()
            if skipped:
                self.app.toast(tr("Typed it, except characters the Frame's US keyboard layout doesn't have: {chars}")
                               .format(chars=skipped), error=True)
        self.app.run_bg(work)
