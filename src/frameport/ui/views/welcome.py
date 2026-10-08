"""First run: get the toolchain, find the Frame, add games — each step starts by itself and can be skipped."""
from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from ...core import library
from ...i18n import tr
from .. import components as C
from .. import theme as T

if TYPE_CHECKING:
    from ..app import FramePortApp


def needed() -> bool:
    """Show the welcome screen: never finished it and something basic is missing."""
    from ...frame.connection import saved_targets
    from ...tools import toolchain

    if library.setting("ui.welcome_done"):
        return False
    tools_ok = all(s.installed for s in toolchain.status(include_optional=False))
    return not tools_ok or (not saved_targets() and not library.games())


class WelcomeView:
    def __init__(self, app: FramePortApp):
        self.app = app

    def step(self, n: int, head: str, text: str, state: str, *content: ft.Control) -> ft.Control:
        icon = {"done": (ft.Icons.CHECK_ROUNDED, T.OK), "busy": (None, T.ACCENT),
                "todo": (None, T.TEXT_3), "error": (ft.Icons.PRIORITY_HIGH_ROUNDED, T.ERROR)}[state]
        marker = C.spinner("m") if state == "busy" else \
            ft.Icon(icon[0], size=T.px(18), color=icon[1]) if icon[0] else ft.Text(str(n), weight=ft.FontWeight.W_700,
                                                                            color=T.TEXT_2)
        return C.card(ft.Row([
            ft.Container(marker, width=T.px(36), height=T.px(36), border_radius=T.px(18), alignment=ft.Alignment.CENTER,
                         bgcolor=T.soft(icon[1], 0.15)),
            ft.Column([C.h2(head), C.body(text), *content], spacing=T.S2, expand=True),
        ], spacing=T.S4, vertical_alignment=ft.CrossAxisAlignment.START), padding=T.S5)

    def build(self) -> ft.Control:
        from ...tools import toolchain

        app = self.app
        tools = toolchain.status(include_optional=False)
        tools_done = all(s.installed for s in tools)
        tool_job = next((j for j in app.jobs.jobs if j.kind == "tools" and j.active), None)
        if not tools_done and not tool_job and not app.welcome_started:
            app.welcome_started = True
            tool_job = app.update_tools(quiet=True)
        t_state = "done" if tools_done else "busy" if tool_job else "error"
        t_text = (tr("Java, OVRPort and apksigner are ready.") if tools_done else
                  (tool_job.message or tool_job.stage or tr("Downloading…")) if tool_job else
                  tr("The download didn't finish. Check your internet connection."))
        f_state = {"connected": "done", "connecting": "busy"}.get(app.frame_state, "todo")
        f_text = (tr("Connected to {label}.").format(label=app.target.label) if app.frame_state == "connected" else
                  tr("Looking for your Frame…") if app.frame_state == "connecting" else
                  tr("FramePort installs games on the Frame over your network."))
        g_state = "done" if library.games() else "todo"
        return ft.Column([
            ft.Container(height=T.S5),
            ft.Row([C.logo(T.px(60)),
                    ft.Column([C.title(tr("Welcome to FramePort")),
                               C.body(tr("Play your Quest, Android and PC VR games on the Steam Frame. Three steps "
                                      "and you're set."))], spacing=T.px(2))], spacing=T.S4),
            ft.Container(height=T.S3),
            self.step(1, tr("Getting FramePort ready"), t_text, t_state,
                      *([C.progress_bar(tool_job.fraction)] if tool_job else []),
                      *([C.secondary(tr("Try again"), ft.Icons.REFRESH_ROUNDED, lambda e: app.update_tools())]
                        if t_state == "error" else [])),
            self.step(2, tr("Connect your Steam Frame"), f_text, f_state,
                      *([] if f_state == "done"
                        else [C.secondary(tr("Set up the Frame"), ft.Icons.ARROW_FORWARD_ROUNDED,
                                          lambda e: app.go("frame"))])),
            self.step(3, tr("Add your games"),
                      tr("A folder with Android games (APK + OBB, for example Quest games) or PC VR games."),
                      g_state, ft.Row([C.primary(tr("Scan a folder…"), ft.Icons.FOLDER_OPEN_ROUNDED, app.pick_folder),
                                       C.ghost(tr("Add an APK file…"), ft.Icons.ANDROID_ROUNDED, app.pick_apk)],
                                      spacing=T.S2)),
            ft.Row([ft.Container(expand=True),
                    C.primary(tr("Go to my library"), ft.Icons.ARROW_FORWARD_ROUNDED, lambda e: app.finish_welcome())
                    if g_state == "done" else C.ghost(tr("Skip for now"), on_click=lambda e: app.finish_welcome())]),
        ], spacing=T.S4, scroll=ft.ScrollMode.AUTO, expand=True, width=T.px(760))
