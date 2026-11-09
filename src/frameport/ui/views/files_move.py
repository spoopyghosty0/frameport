"""Files → Move to… (GitHub #532): pick a folder in any of the Files tab's locations (the same list, the same
browsing, folders only) and move the chosen files and folders there. The move itself is a job (`FilesView.start_move`
→ `install/files.move`): a rename on one file system, else a copy on the Frame and the originals deleted."""
from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING

import flet as ft

from ...errors import explain
from ...i18n import tr, tr_n
from .. import components as C
from .. import theme as T

if TYPE_CHECKING:
    from .files import FilesView


def destinations(groups: list[tuple[str | None, list[tuple[dict, str, str]]]]) -> list[tuple[str | None, list]]:
    """The locations a move can go to: everything but read-only drives (and empty headings)."""
    out = []
    for heading, entries in groups:
        keep = [x for x in entries if not x[0].get("read_only")]
        if keep:
            out.append((heading, keep))
    return out


class MovePicker:
    def __init__(self, view: FilesView, src: dict, items: list):
        self.view, self.app, self.src, self.items = view, view.app, src, items
        self.loc: dict | None = None
        self.path = ""
        self.locations = ft.Column(spacing=T.px(2), scroll=ft.ScrollMode.AUTO, expand=True)
        self.crumb_row = ft.Row(spacing=T.px(2), wrap=True)
        self.folders = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)
        self.hint = C.meta("")
        self.go = C.primary(tr("Move here"), ft.Icons.DRIVE_FILE_MOVE_ROUNDED, lambda e: self._move())
        self.go.disabled = True

    def show(self) -> None:
        page = self.app.page
        self.view.move_picker = self  # (ui_smoke picks a destination through it)
        n = len(self.items)
        heading = (tr("Move {name} to…").format(name=self.items[0].name) if n == 1
                   else tr("Move {n} items to…").format(n=n))
        content = ft.Column([
            ft.Row([
                C.card(self.locations, padding=T.px(6), width=T.px(240), expand=False),
                ft.Column([self.crumb_row, C.card(self.folders, padding=T.px(4), expand=True)], spacing=T.S2,
                          expand=True),
            ], spacing=T.S3, expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH),
            self.hint,
        ], spacing=T.S2, expand=True)
        page.show_dialog(C.dialog(heading, content, [C.ghost(tr("Cancel"), on_click=lambda e: page.pop_dialog()),
                                                     self.go], size="l", height=T.px(440)))
        self._render_locations()
        self.open(self.src, self.view.path)  # start where the files are (a sibling folder is one click away)

    # ---------------------------------------------------------------- locations
    def _render_locations(self) -> None:
        rows = []
        for heading, entries in destinations(self.view.location_groups()):
            if heading:
                rows.append(ft.Container(C.meta(heading.upper()), padding=ft.Padding(T.S2, T.S2, 0, T.px(2))))
            for loc, icon, _sub in entries:
                selected = self.loc is not None and loc["id"] in (self.loc["id"],
                                                                  (self.loc.get("parent") or {}).get("id"))
                ic = ft.Icon(icon, size=T.px(16))
                label = C.body(loc["label"], weight=ft.FontWeight.W_500, max_lines=1,
                               overflow=ft.TextOverflow.ELLIPSIS)
                box = ft.Container(ft.Row([ic, ft.Container(label, expand=True)], spacing=T.S2),
                                   padding=ft.Padding(T.S2, T.px(5), T.S2, T.px(5)), border_radius=T.RADIUS_SM,
                                   ink=True, tooltip=loc["label"], on_click=lambda e, x=loc: self.open(x))
                C.selected_style(box, selected, ic, label)
                rows.append(box)
        self.locations.controls = rows
        C.update(self.locations)

    def open(self, loc: dict, path: str | None = None) -> None:
        def work():
            try:
                resolved = self.view.resolve(loc)
            except Exception as exc:  # noqa: BLE001
                self.hint.value = tr("Couldn't open {label}: {exc}").format(label=loc.get("label"), exc=explain(exc))
                C.update(self.hint)
                return
            self.loc = resolved
            self._render_locations()
            self.cd(path or resolved["path"])
        self.app.run_bg(work)

    # ---------------------------------------------------------------- folders
    def cd(self, path: str) -> None:
        from ...install import files

        loc = self.loc
        path = files.inside(loc["path"], path)
        self.path = path
        self.go.disabled = True
        C.update(self.go)

        def work():
            try:
                entries = files.loc_list(self.app.target.frame, loc, path)
            except Exception as exc:  # noqa: BLE001
                self.hint.value = tr("Couldn't list {path}: {exc}").format(path=path, exc=explain(exc))
                C.update(self.hint)
                return
            if (self.loc, self.path) != (loc, path):
                return
            self._render_folders([e for e in entries if e.is_dir])
        self.app.run_bg(work)

    def _render_folders(self, folders: list) -> None:
        loc, path = self.loc, self.path
        self.crumb_row.controls = []
        for i, (label, cloc, p) in enumerate(self.view.nav_crumbs(loc, path)):
            if i:
                self.crumb_row.controls.append(ft.Icon(ft.Icons.CHEVRON_RIGHT_ROUNDED, size=T.px(16), color=T.TEXT_3))
            self.crumb_row.controls.append(C.ghost(label, on_click=lambda e, c=cloc, p=p: (
                self.cd(p) if c is self.loc else self.open(c, p)), color=T.TEXT if cloc is loc and p == path
                else T.TEXT_2))
        moving = {x.path for x in self.items}
        rows = []
        if path != loc["path"]:
            rows.append(self._folder_row("..", ft.Icons.ARROW_UPWARD_ROUNDED, posixpath.dirname(path)))
        elif loc.get("parent"):  # from a game's saves back to its storage
            rows.append(self._folder_row("..", ft.Icons.ARROW_UPWARD_ROUNDED, "",
                                         on_click=lambda e, c=loc["parent"]: self.open(c)))
        if self.view.has_saves_entry(loc, path):  # into the game's saves, like the Files list
            rows.append(self._folder_row(tr("Saves (internal storage)"), ft.Icons.SAVE_ROUNDED, "", link=True,
                                         on_click=lambda e: self.open(self.view.saves_location(loc))))
        for e in folders:
            if e.path in moving:  # never into itself
                continue
            rows.append(self._folder_row(e.name, ft.Icons.FOLDER_OUTLINED, e.path, link=e.link))
        if not folders and not self.view.has_saves_entry(loc, path):
            rows.append(ft.Container(C.meta(tr("No folders here: the files go into this folder.")), padding=T.S3))
        self.folders.controls = rows
        problem = self._problem()
        self.go.disabled = problem is not None
        n = len(self.items)
        name = self.view.loc_name(loc)
        where = posixpath.join(name, posixpath.relpath(path, loc["path"])) if path != loc["path"] else name
        self.hint.value = problem or tr_n("{n} item goes into {where}", "{n} items go into {where}", n, where=where)
        for c in (self.crumb_row, self.folders, self.go, self.hint):
            C.update(c)

    def _folder_row(self, name: str, icon: str, path: str, link: bool = False, on_click=None) -> ft.Control:
        return ft.Container(ft.Row([ft.Icon(icon, size=T.px(18), color=T.ACCENT if name != ".." else T.TEXT_2),
                                    C.body(name, T.TEXT if name != ".." else T.TEXT_2, max_lines=1,
                                           overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                                    *([C.meta(tr("link"))] if link else [])], spacing=T.S3),
                            padding=ft.Padding(T.S3, T.px(7), T.S3, T.px(7)), border_radius=T.RADIUS_SM, ink=True,
                            on_click=on_click or (lambda e: self.cd(path)))

    def _problem(self) -> str | None:
        """Why this folder can't take the items (None = it can)."""
        from ...install import files

        try:
            files.check_move(self.src, [x.path for x in self.items], self.loc, self.path)
        except ValueError as exc:
            return str(exc)
        return None

    def _move(self) -> None:
        if self.loc is None or self._problem():
            return
        self.app.page.pop_dialog()
        self.view.start_move(self.src, self.items, self.loc, self.path)
