"""Files: browse and manage files on the Steam Frame (instead of a separate file-transfer app).

Locations are the folders Quest games see (Videos / Downloads / Documents, shared by every game as /sdcard/Movies,
/sdcard/Download, /sdcard/Documents), each installed game's own storage, and the Frame's home folder. Everything runs
over the app's SSH connection (SFTP); uploads and downloads are jobs in the Activity panel. Persistent like the
Library: the chosen location and folder survive switching tabs.
"""
from __future__ import annotations

import posixpath
import time
from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from ...errors import explain
from ...i18n import tr, tr_n
from .. import components as C
from .. import theme as T
from .files_dialog import human

if TYPE_CHECKING:
    from ..app import FramePortApp

SHARED_ICONS = {"videos": ft.Icons.MOVIE_ROUNDED, "downloads": ft.Icons.DOWNLOAD_ROUNDED,
                "documents": ft.Icons.DESCRIPTION_ROUNDED}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def dropzone_available() -> bool:
    """Drag-and-drop from the file manager needs flet-dropzone's Flutter code, which only a `flet build` app has (not
    `flet run`/the desktop client used from source, nor the PyInstaller fallback)."""
    import sys

    from ... import updates

    if getattr(sys, "frozen", False) or updates.install_kind() != "bundle":
        return False
    try:
        import flet_dropzone  # noqa: F401
    except ImportError:
        return False
    return True


def file_icon(name: str, is_dir: bool) -> str:
    if is_dir:
        return ft.Icons.FOLDER_ROUNDED
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXT:
        return ft.Icons.MOVIE_ROUNDED
    if ext in IMAGE_EXT:
        return ft.Icons.IMAGE_ROUNDED
    if ext in (".zip", ".7z", ".rar", ".tar", ".gz"):
        return ft.Icons.FOLDER_ZIP_ROUNDED
    return ft.Icons.INSERT_DRIVE_FILE_ROUNDED


def crumbs(root: str, path: str, root_label: str) -> list[tuple[str, str]]:
    """(label, path) for each level from the location down to `path`."""
    out = [(root_label, root)]
    rel = posixpath.relpath(path, root) if path != root else ""
    cur = root
    for part in [p for p in rel.split("/") if p and p != "."]:
        cur = posixpath.join(cur, part)
        out.append((part, cur))
    return out


class FilesView:
    def __init__(self, app: FramePortApp):
        self.app = app
        self.loc: dict | None = None      # {"id", "label", "path", "android", "shared", "package"}
        self.path = ""
        self.entries = []
        self.hidden = False
        self.game_locs: dict[str, dict] = {}  # package -> resolved location (agent storage_targets, cached)
        self.shared: list[dict] = []
        self.locations = ft.Column(spacing=T.px(2), scroll=ft.ScrollMode.AUTO, expand=True)
        self.crumb_row = ft.Row(spacing=T.px(2), wrap=True)
        self.where = C.meta("")
        self.listing = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)
        self.status = C.meta("")
        self.toolbar = ft.Row([
            C.primary(tr("Upload files…"), ft.Icons.UPLOAD_FILE_ROUNDED, self.upload_files),
            C.secondary(tr("Upload folder…"), ft.Icons.DRIVE_FOLDER_UPLOAD_ROUNDED, self.upload_folder),
            C.ghost(tr("New folder…"), ft.Icons.CREATE_NEW_FOLDER_ROUNDED, lambda e: self.new_folder()),
            C.icon_btn(ft.Icons.REFRESH_ROUNDED, tr("Refresh"), lambda e: self.load()),
        ], spacing=T.S2, wrap=True, run_spacing=T.S2)
        self.hidden_switch = C.switch(tr("Show hidden files"), wrap=False, value=False, on_change=self._toggle_hidden)
        self.selected: set[str] = set()  # paths of checked entries in the current folder
        self.checks: dict[str, ft.Checkbox] = {}
        self.select_all = ft.Checkbox(value=False, active_color=T.ACCENT, check_color=T.ON_ACCENT,
                                      tooltip=tr("Select all"), on_change=self._toggle_all)
        self.sel_label = C.body("", T.TEXT, weight=ft.FontWeight.W_500)
        self.sel_bar = ft.Container(ft.Row([
            self.sel_label, ft.Container(expand=True),
            C.secondary(tr("Download…"), ft.Icons.DOWNLOAD_ROUNDED, self._download_selected),
            C.ghost(tr("Delete…"), ft.Icons.DELETE_OUTLINE_ROUNDED, lambda e: self._delete_selected()),
            C.ghost(tr("Clear"), ft.Icons.CLOSE_ROUNDED, lambda e: self._clear_selection()),
        ], spacing=T.S2), padding=ft.Padding(T.S3, T.px(6), T.S2, T.px(6)), border_radius=T.RADIUS_SM,
            bgcolor=T.ACCENT_SOFT, visible=False)
        self.drop_hint = ft.Container(
            ft.Column([ft.Icon(ft.Icons.UPLOAD_ROUNDED, size=T.px(48), color=T.ACCENT), C.h2(tr("Drop to upload")),
                       C.meta("")], horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            left=0, right=0, top=0, bottom=0, alignment=ft.Alignment.CENTER, bgcolor=T.soft("#000000", 0.7),
            border_radius=T.RADIUS, border=ft.Border.all(2, T.ACCENT), visible=False)
        # click and drag across rows to select them (C.DragSelect)
        self.drag = C.DragSelect(lambda p: p in self.selected, self._toggle,
                                 can_select=lambda p: any(x.path == p and self._selectable(x) for x in self.entries))
        # one right-click menu for the listing: filled with the clicked entry's actions (or the folder's) when it opens
        self.menu = ft.ContextMenu(content=ft.GestureDetector(
            content=C.card(self.listing, padding=T.px(4), expand=True), expand=True,
            on_secondary_tap_down=lambda e: self.open_menu(None, e.global_position),
            on_pan_start=self.drag.start, on_pan_end=self.drag.end),
            secondary_trigger=None, tertiary_trigger=None, expand=True)
        self.root = None

    # ---------------------------------------------------------------- building
    def mount(self, package: str | None = None) -> ft.Control:
        app = self.app
        if not (app.target and app.frame_state == "connected"):
            return ft.Column([
                app.top_bar(tr("Files"), tr("Videos, documents, mods and saves on your Steam Frame")),
                C.empty_state(ft.Icons.FOLDER_OFF_ROUNDED, tr("Connect your Frame first"),
                              tr("Files on the Frame can be browsed once FramePort is connected to it."),
                              C.primary(tr("Connect"), ft.Icons.LINK_ROUNDED, lambda e: app.go("frame")))], expand=True)
        if self.root is None:
            self.root = ft.Column([
                app.top_bar(tr("Files"), tr("Videos, documents, mods and saves on your Steam Frame")),
                ft.Row([
                    C.card(ft.Column([C.meta(tr("Locations").upper()), self.locations], spacing=T.S2, expand=True),
                           padding=T.S3, width=T.px(260), expand=False),
                    ft.Column([
                        # the folder path on its own line: next to the buttons a narrow window squeezed it
                        self.crumb_row,
                        self.toolbar,
                        ft.Row([self.select_all, ft.Container(self.where, expand=True), self.hidden_switch],
                               vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        self._drop_area(ft.Stack([self.menu, self.drop_hint], expand=True)),
                        # below the list (as in the Library): above it, its appearing pushed the rows down in the
                        # middle of a drag-select
                        self.sel_bar,
                        self.status,
                    ], spacing=T.S2, expand=True),
                ], spacing=T.S4, expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH),
            ], spacing=T.S3, expand=True)
        self.app.run_bg(self._load_locations, package)
        return self.root

    def _drop_area(self, content: ft.Control) -> ft.Control:
        """Files and folders dragged in from the computer's file manager are uploaded to the open folder. Needs the
        flet-dropzone extension, which only the packaged app contains (flet build); elsewhere: no drop area."""
        if not dropzone_available():
            return content
        import flet_dropzone as ftd

        def entered(e):
            self.drop_hint.content.controls[2].value = tr("into {value}").format(value=self.where.value or self.path)
            self.drop_hint.visible = True
            C.update(self.drop_hint)

        def exited(e):
            self.drop_hint.visible = False
            C.update(self.drop_hint)

        def dropped(e):
            exited(e)
            paths = [Path(f.path) for f in e.files if f.path and not f.path.startswith("blob:")]
            if paths:
                self.upload(paths)
        return ftd.Dropzone(content=content, expand=True, on_entered=entered, on_exited=exited, on_dropped=dropped)

    def _location_row(self, loc: dict, icon: str, sub: str = "") -> ft.Control:
        selected = self.loc is not None and self.loc["id"] == loc["id"]
        ic = ft.Icon(icon, size=T.px(18))
        label = C.body(loc["label"], weight=ft.FontWeight.W_500)
        box = ft.Container(
            ft.Row([ic, ft.Column([label, *([C.meta(sub)] if sub else [])], spacing=0, expand=True)], spacing=T.S2),
            padding=ft.Padding(T.S2, T.px(6), T.S2, T.px(6)), border_radius=T.RADIUS_SM, ink=True,
            on_click=lambda e, loc_=loc: self.open_location(loc_))
        C.selected_style(box, selected, ic, label)  # like the sidebar: the portal fade with a white icon
        return box

    def _render_locations(self) -> None:
        rows = [self._location_row(loc_, SHARED_ICONS.get(loc_["id"], ft.Icons.FOLDER_ROUNDED), loc_["android"])
                for loc_ in self.shared]
        games = self._installed_games()
        if games:
            rows.append(ft.Container(C.meta(tr("Game storage").upper()), padding=ft.Padding(T.S2, T.S3, 0, T.px(2))))
            for pkg, title in games:
                loc = self.game_locs.get(pkg) or {"id": f"app:{pkg}", "label": title, "package": pkg}
                rows.append(self._location_row(loc, ft.Icons.SPORTS_ESPORTS_ROUNDED))
        rows.append(ft.Container(C.meta(tr("Advanced").upper()), padding=ft.Padding(T.S2, T.S3, 0, T.px(2))))
        home = {"id": "home", "label": tr("Home folder"), "path": self.app.target.frame.home, "android": "",
                "shared": False}
        rows.append(self._location_row(home, ft.Icons.HOME_ROUNDED, tr("everything in ~ (not seen by games)")))
        self.locations.controls = rows
        C.update(self.locations)

    def _installed_games(self) -> list[tuple[str, str]]:
        from ...core import library

        out = []
        for d in (self.app.frame_info or {}).get("installed", []):
            if d.get("kind") in ("pcvr", "linux"):  # no Android storage
                continue
            g = library.game(d["package"]) or {}
            out.append((d["package"], g.get("title") or d.get("title") or d["package"]))
        return sorted(out, key=lambda x: x[1].lower())

    # ---------------------------------------------------------------- loading (background threads)
    def _load_locations(self, package: str | None) -> None:
        from ...install import files

        frame = self.app.target.frame
        try:
            if not self.shared:
                names = {"videos": tr("Videos"), "downloads": tr("Downloads"), "documents": tr("Documents")}
                self.shared = [{**t, "label": names.get(t["id"], t["id"]), "package": None}
                               for t in files.storage_targets(frame)]
        except Exception as exc:  # noqa: BLE001
            self.status.value = tr("Couldn't read the Frame's folders: {exc}").format(exc=explain(exc))
            C.update(self.status)
        self._render_locations()
        if package:
            self.open_location({"id": f"app:{package}", "package": package,
                                "label": dict(self._installed_games()).get(package, package)})
        elif self.loc is None and self.shared:
            self.open_location(self.shared[0])
        elif self.loc is not None:
            self.load()

    def open_location(self, loc: dict) -> None:
        def work():
            nonlocal loc
            if loc.get("package") and not loc.get("path"):
                from ...install import files

                try:
                    t = {x["id"]: x for x in files.storage_targets(self.app.target.frame, loc["package"])}["app"]
                except Exception as exc:  # noqa: BLE001
                    self.app.toast(tr("Couldn't open the game's storage: {exc}").format(exc=explain(exc)), error=True)
                    return
                loc = {**loc, "path": t["path"], "android": t["android"], "shared": False}
                self.game_locs[loc["package"]] = loc
            self.loc, self.path = loc, loc["path"]
            self._render_locations()
            self.load()
        self.app.run_bg(work)

    def load(self) -> None:
        if not self.loc:
            return
        loc, path = self.loc, self.path
        self.status.value = tr("Loading…")
        C.update(self.status)

        def work():
            from ...install import files

            try:
                entries = files.list_dir(self.app.target.frame, loc["path"], path, hidden=self.hidden)
            except Exception as exc:  # noqa: BLE001
                self.status.value = tr("Couldn't list {path}: {exc}").format(path=path, exc=explain(exc))
                C.update(self.status)
                return
            if (self.loc, self.path) != (loc, path):
                return  # the user moved on meanwhile
            self.entries = entries
            self.selected &= {e.path for e in entries}
            self._render_listing()
        self.app.run_bg(work)

    # ---------------------------------------------------------------- listing
    def _render_listing(self) -> None:
        loc = self.loc
        self.crumb_row.controls = []
        for i, (label, p) in enumerate(crumbs(loc["path"], self.path, loc["label"])):
            if i:
                self.crumb_row.controls.append(ft.Icon(ft.Icons.CHEVRON_RIGHT_ROUNDED, size=T.px(16), color=T.TEXT_3))
            self.crumb_row.controls.append(C.ghost(label, on_click=lambda e, p=p: self.cd(p),
                                                   color=T.TEXT if p == self.path else T.TEXT_2))
        android = loc.get("android")
        rel = posixpath.relpath(self.path, loc["path"]) if self.path != loc["path"] else ""
        self.where.value = (tr("Games see this folder as {value}")
                            .format(value=posixpath.join(android, rel) if rel else android)
                            if android else self.path)
        rows = []
        self.checks = {}
        if self.path != loc["path"]:
            rows.append(self._row(None))
        rows += [self._row(e) for e in self.entries]
        if not self.entries:
            rows.append(ft.Container(C.meta(tr("This folder is empty. Upload files with the buttons above.")),
                                     padding=T.S4))
        self.listing.controls = rows
        n_dirs = sum(e.is_dir for e in self.entries)
        size = sum(e.size for e in self.entries if not e.is_dir)
        self.status.value = ", ".join([tr_n("{n} folder", "{n} folders", n_dirs),
                                       tr_n("{n} file", "{n} files", len(self.entries) - n_dirs), human(size)])
        self._update_selection(render=False)
        for c in (self.crumb_row, self.where, self.listing, self.status):
            C.update(c)

    def _row(self, e) -> ft.Control:
        if e is None:  # ".."
            return ft.Container(ft.Row([ft.Icon(ft.Icons.ARROW_UPWARD_ROUNDED, size=T.px(20), color=T.TEXT_2),
                                        C.body("..", T.TEXT_2)], spacing=T.S3),
                                padding=ft.Padding(T.S3, T.px(8), T.S3, T.px(8)), border_radius=T.RADIUS_SM, ink=True,
                                on_click=lambda ev: self.cd(posixpath.dirname(self.path)))
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.mtime)) if e.mtime else ""
        info = ((tr("folder") if e.is_dir else human(e.size)) + (tr(" · link") if e.link else "")
                + (f" · {when}" if when else ""))
        actions = [C.icon_btn(ft.Icons.DOWNLOAD_ROUNDED, tr("Download to this PC…"),
                              lambda ev, x=e: self._download_later([x]))]
        if not self._protected(e):
            actions += [C.icon_btn(ft.Icons.DRIVE_FILE_RENAME_OUTLINE_ROUNDED, tr("Rename…"),
                                   lambda ev, x=e: self.rename(x)),
                        C.icon_btn(ft.Icons.DELETE_OUTLINE_ROUNDED, tr("Delete…"), lambda ev, x=e: self.delete([x]))]
        check = ft.Checkbox(value=e.path in self.selected, active_color=T.ACCENT, check_color=T.ON_ACCENT,
                            on_change=lambda ev, p=e.path: self._toggle(p, ev.control.value),
                            disabled=self._protected(e))
        self.checks[e.path] = check
        row = ft.Container(
            ft.Row([check,
                    ft.Icon(file_icon(e.name, e.is_dir), size=T.px(20), color=T.ACCENT if e.is_dir else T.TEXT_2),
                    ft.Column([C.body(e.name, T.TEXT, weight=ft.FontWeight.W_500, max_lines=1,
                                      overflow=ft.TextOverflow.ELLIPSIS), C.meta(info)], spacing=0, expand=True),
                    *actions], spacing=T.S3),
            padding=ft.Padding(T.S3, T.px(6), T.S2, T.px(6)), border_radius=T.RADIUS_SM, ink=e.is_dir,
            on_click=(lambda ev, p=e.path: self.cd(p)) if e.is_dir else None,
            on_hover=lambda ev, p=e.path: self.drag.hover(p, ev.data in (True, "true")))
        return ft.GestureDetector(content=row, on_secondary_tap_down=lambda ev, x=e: self.open_menu(x,
                                                                                                    ev.global_position))

    # ---------------------------------------------------------------- right-click menu
    def menu_actions(self, e) -> list[tuple | None]:
        """[(label, icon, handler) | None] for a right-click on entry `e` (None = on the folder's empty space). With
        several entries selected and `e` among them, the actions act on the whole selection."""
        if e is None:
            return [(tr("Upload files…"), ft.Icons.UPLOAD_FILE_ROUNDED, self.upload_files),
                    (tr("Upload folder…"), ft.Icons.DRIVE_FOLDER_UPLOAD_ROUNDED, self.upload_folder),
                    (tr("New folder…"), ft.Icons.CREATE_NEW_FOLDER_ROUNDED, lambda ev: self.new_folder()),
                    None,
                    *([(tr("Select all"), ft.Icons.SELECT_ALL_ROUNDED, lambda ev: self._select_all())]
                      if any(self._selectable(x) for x in self.entries) else []),
                    (tr("Copy folder path"), ft.Icons.CONTENT_COPY_ROUNDED,
                     lambda ev: self.app.copy(self._shown_path(self.path))),
                    (tr("Refresh"), ft.Icons.REFRESH_ROUNDED, lambda ev: self.load())]
        by_path = {x.path: x for x in self.entries}
        items = [by_path[p] for p in C.menu_targets(e.path, [x.path for x in self.entries if x.path in self.selected])]
        several = len(items) > 1
        deletable = [x for x in items if not self._protected(x)]
        out: list[tuple | None] = []
        if not several and e.is_dir:
            out.append((tr("Open"), ft.Icons.FOLDER_OPEN_ROUNDED, lambda ev: self.cd(e.path)))
        out.append((tr("Download {n} items to this PC…").format(n=len(items)) if several
                    else tr("Download to this PC…"), ft.Icons.DOWNLOAD_ROUNDED,
                    lambda ev: self._download_later(items)))
        if not several and not self._protected(e):
            out.append((tr("Rename…"), ft.Icons.DRIVE_FILE_RENAME_OUTLINE_ROUNDED, lambda ev: self.rename(e)))
        if not several:
            out.append((tr("Copy path"), ft.Icons.CONTENT_COPY_ROUNDED, lambda ev: self.app.copy(self._shown_path(
                e.path))))
        out.append(None)
        if several:
            out.append((tr("Clear selection"), ft.Icons.CLOSE_ROUNDED, lambda ev: self._clear_selection()))
        elif self._selectable(e):
            on = e.path in self.selected
            out.append((tr("Deselect") if on else tr("Select"),
                        ft.Icons.CHECK_BOX_OUTLINE_BLANK_ROUNDED if on else ft.Icons.CHECK_BOX_ROUNDED,
                        lambda ev: self._toggle(e.path, not on)))
        if deletable:
            out += [None, (tr("Delete {n} items…").format(n=len(deletable)) if len(deletable) > 1 else tr("Delete…"),
                           ft.Icons.DELETE_OUTLINE_ROUNDED, lambda ev: self.delete(deletable))]
        return out

    def open_menu(self, e, position=None) -> None:
        if not self.loc:
            return
        self.menu.items = C.menu_items(self.menu_actions(e))
        C.update(self.menu)
        self.app.page.run_task(self.menu.open, global_position=position)

    def _shown_path(self, path: str) -> str:
        """The path as games see it (/sdcard/…) where the location has one, else the Frame's own path."""
        android = (self.loc or {}).get("android")
        if not android:
            return path
        rel = posixpath.relpath(path, self.loc["path"])
        return android if rel == "." else posixpath.join(android, rel)

    def _download_later(self, items: list) -> None:
        """Start the (async: it asks for a folder) download from a plain click handler: Flet only awaits handlers
        that are coroutine functions themselves, not lambdas returning a coroutine."""
        self.app.page.run_task(self.download, items)

    def cd(self, path: str) -> None:
        self.path = path
        self.selected.clear()
        self.load()

    def _toggle(self, path: str, on: bool) -> None:
        (self.selected.add if on else self.selected.discard)(path)
        check = self.checks.get(path)
        if check is not None and check.value != on:
            check.value = on
            C.update(check)
        self._update_selection()

    def _toggle_all(self, e) -> None:
        self.selected = {x.path for x in self.entries if self._selectable(x)} if e.control.value else set()
        self._render_listing()

    def _clear_selection(self) -> None:
        self.selected.clear()
        self._render_listing()

    def _select_all(self) -> None:
        self.selected = {x.path for x in self.entries if self._selectable(x)}
        self._render_listing()

    def _selectable(self, e) -> bool:
        return not self._protected(e)

    def _protected(self, e) -> bool:
        """Lepton's links at the top of a game's storage (Movies → ~/Videos, …): renaming/deleting them breaks them."""
        return e.link and posixpath.dirname(e.path) == posixpath.normpath(self.loc["path"])

    def _update_selection(self, render: bool = True) -> None:
        n = len(self.selected)
        size = sum(x.size for x in self.entries if x.path in self.selected and not x.is_dir)
        self.sel_label.value = (tr("{n} selected").format(n=n)
                                + (tr(" · {human} in files").format(human=human(size)) if size else ""))
        self.sel_bar.visible = bool(n)
        self.select_all.value = bool(self.entries) and n == len([x for x in self.entries if self._selectable(x)])
        if render:
            for c in (self.sel_bar, self.select_all):
                C.update(c)

    def _chosen(self) -> list:
        return [x for x in self.entries if x.path in self.selected]

    async def _download_selected(self, e=None):
        if self.selected:
            await self.download(self._chosen())

    def _delete_selected(self) -> None:
        items = [x for x in self._chosen() if not self._protected(x)]
        if items:
            self.delete(items)

    def _toggle_hidden(self, e) -> None:
        self.hidden = bool(e.control.value)
        self.load()

    # ---------------------------------------------------------------- actions
    async def upload_files(self, e=None):
        files = await ft.FilePicker().pick_files(allow_multiple=True)
        paths = [Path(f.path) for f in files or [] if f.path]
        if paths:
            self.upload(paths)

    async def upload_folder(self, e=None):
        path = await ft.FilePicker().get_directory_path(dialog_title=tr("Folder to upload to the Frame"))
        if path:
            self.upload([Path(path)])

    def upload(self, paths: list[Path]) -> None:
        if not self.loc:
            return
        app, loc, dest = self.app, self.loc, self.path

        def run(job):
            from ...install import files

            sent, skipped, total = files.upload(app.target.frame, paths, dest, job.reporter)
            return (tr_n("Uploaded {n} file", "Uploaded {n} files", len(sent))
                    + (tr_n(", {n} already there", ", {n} already there", len(skipped)) if skipped else ""))
        app.submit(tr("Upload to {label}").format(label=loc['label']), run, loc.get("package"), kind="tool-frame",
                   open_panel=True)

    async def download(self, items: list) -> None:
        folder = await ft.FilePicker().get_directory_path(dialog_title=tr("Download to which folder on this PC?"))
        if not folder:
            return
        app, root = self.app, self.loc["path"]

        def run(job):
            from ...install import files

            r = files.download(app.target.frame, root, [x.path for x in items], Path(folder), job.reporter)
            return tr_n("Downloaded {n} file to {folder}", "Downloaded {n} files to {folder}", r["files"],
                        folder=r["folder"])
        more = tr_n(" and {n} more", " and {n} more", len(items) - 1) if len(items) > 1 else ""
        app.submit(tr("Download {name}").format(name=items[0].name) + more, run,
                   None, kind="tool-frame", open_panel=True)

    def new_folder(self) -> None:
        self._ask_name(tr("New folder"), tr("Folder name"), "", tr("Create"), lambda name: self._fs(
            lambda files, frame: files.make_dir(frame, self.loc["path"], self.path, name)))

    def rename(self, e) -> None:
        self._ask_name(tr("Rename {name}").format(name=e.name), tr("New name"), e.name, tr("Rename"),
                       lambda name: self._fs(
                           lambda files, frame: files.rename(frame, self.loc["path"], e.path, name)))

    def delete(self, items: list) -> None:
        names = ", ".join(x.name for x in items[:3]) + (" …" if len(items) > 3 else "")
        what = tr("folder and everything in it") if any(x.is_dir for x in items) else tr("file")
        C.confirm(self.app.page, tr("Delete {names}?").format(names=names),
                  tr("This deletes the {what} on the Frame. It can't be undone.").format(what=what),
                  tr("Delete"), lambda: (self.selected.difference_update(x.path for x in items),
                                     self._fs(lambda files, frame: files.delete(frame, self.loc["path"],
                                                                                [x.path for x in items]))),
                  danger=True)

    def _fs(self, op) -> None:
        """A quick file operation in the background, then reload the folder (errors become a toast)."""
        def work():
            from ...install import files

            try:
                op(files, self.app.target.frame)
            except Exception as exc:  # noqa: BLE001
                self.app.toast(explain(exc), error=True)
            self.load()
        self.app.run_bg(work)

    def _ask_name(self, heading: str, label: str, value: str, ok: str, on_ok) -> None:
        page = self.app.page
        field = C.field(label=label, value=value, autofocus=True, width=T.DIALOG_S)

        def go(e=None):
            page.pop_dialog()
            if (field.value or "").strip():
                on_ok(field.value.strip())
        field.on_submit = go
        page.show_dialog(C.dialog(heading, field, [C.ghost(tr("Cancel"), on_click=lambda e: page.pop_dialog()),
                                                   C.primary(ok, on_click=go)], size="s"))
