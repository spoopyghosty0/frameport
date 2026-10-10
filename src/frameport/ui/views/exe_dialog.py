"""'Which program starts <game>?' — pick the executable of a Rift game when FramePort isn't sure (or to change it)."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from ... import pipeline
from ...core import library
from ...errors import explain
from ...i18n import fmt_size, tr
from .. import components as C
from .. import theme as T

if TYPE_CHECKING:
    from ..app import FramePortApp


def _size(n: int) -> str:
    return fmt_size(n)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def show_exe_dialog(app: FramePortApp, package: str, remaining: int = 0,
                    on_done: Callable[[], None] | None = None) -> None:
    g = library.game(package)
    extra = (g.get("analysis") or {}).get("extra") or {}
    linux = g.get("kind") == "linux"
    if linux:  # a Linux app: its arm64 programs, best guess first
        root = Path(g.get("game_dir") or ".")
        cands = [{"path": c, "score": 0, "reasons": [], "size": _file_size(root / c)}
                 for c in extra.get("candidates") or [g.get("exe")]]
    else:
        cands = extra.get("exe_candidates") or [{"path": g.get("exe"), "score": 0, "reasons": [], "size": 0}]
    current = g.get("exe") or cands[0]["path"]
    group = ft.RadioGroup(value=current, content=ft.Column(spacing=T.S2))
    rows: dict[str, ft.Container] = {}

    def paint(e=None):
        for path, row in rows.items():
            C.selected_style(row, path == group.value, idle_bg=T.SURFACE)
        if e is not None:
            C.update(group)

    def choose(path):
        group.value = path
        paint(True)
    group.on_change = paint
    for i, c in enumerate(cands):
        tags = [C.pill(r, T.OK if r in ("Unreal game build", "Unity game (next to its data)", "name matches the game",
                                        "named in the Oculus manifest") else
                       T.WARN if r in ("Steam build", "Unreal launcher (starts the real game build)") else T.TEXT_2)
                for r in c.get("reasons", [])]
        if i == 0:
            tags.insert(0, C.pill(tr("Best guess"), T.ACCENT, ft.Icons.AUTO_AWESOME_ROUNDED))
        folder, _, name = c["path"].rpartition("/")
        rows[c["path"]] = row = C.hoverable(ft.Container(ft.Row([
            ft.Radio(value=c["path"], active_color=T.ACCENT),
            ft.Column([
                ft.Row([C.body(name, T.TEXT, weight=ft.FontWeight.W_600), C.meta(_size(c.get("size") or 0))],
                       spacing=T.S2),
                C.meta(folder or tr("(top folder)"), selectable=True),
                ft.Row(tags, spacing=T.px(6), wrap=True) if tags else ft.Container(),
            ], spacing=T.px(4), expand=True),
        ], vertical_alignment=ft.CrossAxisAlignment.START), padding=ft.Padding(T.S2, T.S2, T.S3, T.S2),
            border_radius=T.RADIUS_SM, bgcolor=T.SURFACE, on_click=lambda e, p=c["path"]: choose(p)))
        group.content.controls.append(row)
    paint()

    def use(e):
        choice = group.value
        app.page.pop_dialog()

        def work():
            try:
                pipeline.set_exe(package, choice)
                app.toast(tr("{get} starts with {value}").format(get=g.get('title'), value=choice.rsplit('/', 1)[-1]))
            except Exception as exc:  # noqa: BLE001
                app.toast(tr("Couldn't use {choice}: {exc}").format(choice=choice, exc=explain(exc)), error=True)
            app.refresh_view()
            if on_done:
                on_done()
        app.run_bg(work)

    def later(e):
        app.page.pop_dialog()
        if on_done:
            on_done()

    pick = C.one_choice()
    title = tr("Which program starts {get}?").format(get=g.get('title'))
    lead = (tr("FramePort found more than one program in this app. Pick the one that starts it, not a helper "
               "like a crash reporter or updater.") if linux else
            tr("Pick the program you'd double-click to play. Oculus builds usually work better than Steam "
               "builds."))
    app.page.show_dialog(C.dialog(
        title, ft.Column([C.body(lead), group], spacing=T.S4, scroll=ft.ScrollMode.AUTO, tight=True),
        title_actions=[C.meta(tr("{remaining} more after this").format(remaining=remaining))] if remaining else None,
        height=T.px(min(120 + 96 * len(cands), 520)),
        modal=True, on_dismiss=pick(lambda e: on_done() if on_done else None),  # closed (Esc) = decide later
        actions=[C.ghost(tr("Decide later"), on_click=pick(later)),
                 C.primary(tr("Use this program"), on_click=pick(use))]))
