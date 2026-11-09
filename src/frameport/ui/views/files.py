"""Files: browse and manage files on the Steam Frame (instead of a separate file-transfer app).

Locations are the folders Quest games see (Videos / Downloads / Documents, shared by every game as /sdcard/Movies,
/sdcard/Download, /sdcard/Documents), each installed Quest game's own storage (with its saves = internal storage
as a link-like "Saves (internal storage)" entry at the top, through the agent in podman's namespace), PC VR/Linux
install folders, extra drives, the Frame's home folder and /tmp. Everything runs over the app's SSH connection
(SFTP, the agent for saves and moves); uploads, downloads and moves are jobs in the Activity panel. Persistent like
the Library: the chosen location and folder survive switching tabs.
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

SHARED_ICONS = {"videos": ft.Icons.MOVIE_OUTLINED, "downloads": ft.Icons.DOWNLOAD_ROUNDED,
                "documents": ft.Icons.DESCRIPTION_OUTLINED}
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
        return ft.Icons.FOLDER_OUTLINED
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXT:
        return ft.Icons.MOVIE_OUTLINED
    if ext in IMAGE_EXT:
        return ft.Icons.IMAGE_OUTLINED
    if ext in (".zip", ".7z", ".rar", ".tar", ".gz"):
        return ft.Icons.FOLDER_ZIP_OUTLINED
    return ft.Icons.INSERT_DRIVE_FILE_OUTLINED


def crumbs(root: str, path: str, root_label: str) -> list[tuple[str, str]]:
    """(label, path) for each level from the location down to `path`."""
    out = [(root_label, root)]
    rel = posixpath.relpath(path, root) if path != root else ""
    cur = root
    for part in [p for p in rel.split("/") if p and p != "."]:
        cur = posixpath.join(cur, part)
        out.append((part, cur))
    return out


def drive_locations(listed: list[dict]) -> list[dict]:
    """Files roots are mounts, including drives unsuitable for installing games."""
    return [{"id": f"drive:{d['path']}", "label": d.get("label") or d["path"], "path": d["path"],
             "android": "", "shared": False,
             "read_only": bool(d.get("read_only") or d.get("readonly") or d.get("reason") == "mounted read-only"),
             "free_bytes": d.get("free_bytes")}
            for d in listed if not d.get("internal") and d.get("path", "").startswith("/")]


def game_section(kind: str | None) -> str:
    """Which Files section an installed game is listed in: its Android storage (Quest, the default), or the install
    folder of a PC VR game / Linux app."""
    return "pcvr" if kind in ("rift", "pcvr") else "linux" if kind == "linux" else "quest"


def game_location(dep: dict, title: str, home: str) -> dict:
    """Quest storage resolves lazily; other games expose their deployment's base."""
    pkg = dep["package"]
    loc = {"id": f"app:{pkg}", "label": title, "package": pkg}
    if dep.get("kind") in ("pcvr", "rift", "linux"):
        drive = dep.get("drive") or {}
        fallback = (posixpath.join(drive["path"], "FramePort", pkg)
                    if drive.get("path") and not drive.get("internal")
                    else posixpath.join(home, "Applications/quest-frame", pkg))
        base = dep.get("base") or fallback
        loc.update(path=base, android="", shared=False)
    return loc


class FilesView:
    def __init__(self, app: FramePortApp):
        self.app = app
        self.loc: dict | None = None      # {"id", "label", "path", "android", "shared", "package"}
        self.path = ""
        self.entries = []
        self.hidden = False
        self.game_locs: dict[str, dict] = {}  # package -> resolved location (agent storage_targets, cached)
        self.shared: list[dict] = []
        self.drives: list[dict] | None = None
        self.locations = ft.Column(spacing=T.px(2), scroll=ft.ScrollMode.AUTO, expand=True)
        self.crumb_row = ft.Row(spacing=T.px(2), wrap=True)
        self.where = C.meta("")
        self.notice = ft.Container(visible=False)  # what's special about the location (/tmp, a game's saves)
        self.saves_running: dict[str, bool] = {}  # package -> the game ran when its saves were opened
        self.listing = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True, on_scroll=self._remember_scroll,
                                 scroll_interval=100)
        self.scrolled: dict[str, float] = {}  # folder -> scroll offset, restored when going back to it (GitHub #411)
        self._restore: float | None = None
        self.status = C.meta("")
        self.toolbar = ft.Row([
            C.primary(tr("Upload files…"), ft.Icons.UPLOAD_FILE_OUTLINED, self.upload_files),
            C.secondary(tr("Upload folder…"), ft.Icons.DRIVE_FOLDER_UPLOAD_OUTLINED, self.upload_folder),
            C.ghost(tr("New folder…"), ft.Icons.CREATE_NEW_FOLDER_OUTLINED, lambda e: self.new_folder()),
            C.icon_btn(ft.Icons.REFRESH_ROUNDED, tr("Refresh"), lambda e: self.load()),
        ], spacing=T.S2, wrap=True, run_spacing=T.S2)
        self.hidden_switch = C.switch(tr("Show hidden files"), wrap=False, value=False, on_change=self._toggle_hidden)
        self.selected: set[str] = set()  # paths of checked entries in the current folder
        self.checks: dict[str, ft.Checkbox] = {}
        self.rows: dict[str, tuple] = {}  # path → (row, quiet checkbox, quiet actions)
        self.hot: set[str] = set()  # rows under the mouse or with keyboard focus inside
        self._select_mode = False
        self.select_all = ft.Checkbox(value=False, active_color=T.ACCENT, check_color=T.ON_ACCENT,
                                      tooltip=tr("Select all"), on_change=self._toggle_all)
        self.sel_label = C.body("", T.TEXT, weight=ft.FontWeight.W_500)
        self.sel_bar = ft.Container(ft.Row([
            self.sel_label, ft.Container(expand=True),
            C.secondary(tr("Download…"), ft.Icons.DOWNLOAD_ROUNDED, self._download_selected),
            C.ghost(tr("Move to…"), ft.Icons.DRIVE_FILE_MOVE_OUTLINED, lambda e: self._move_selected()),
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
        self.menu = ft.Container(ft.GestureDetector(
            content=C.card(self.listing, padding=T.px(4), expand=True), expand=True,
            on_secondary_tap_down=lambda e: self.open_menu(None, e.global_position),
            on_pan_start=self.drag.start, on_pan_end=self.drag.end), expand=True)
        self.root = None

    # ---------------------------------------------------------------- building
    def mount(self, package: str | None = None) -> ft.Control:
        app = self.app
        if not (app.target and app.frame_state == "connected"):
            return ft.Column([
                app.top_bar(tr("Files"), tr("Videos, documents, mods and saves on your Steam Frame")),
                C.empty_state(ft.Icons.FOLDER_OPEN_ROUNDED, tr("Your Frame's files, from this PC"),
                              tr("Connect to your Steam Frame to browse its videos, downloads and each game's "
                                 "storage."),
                              C.primary(tr("Connect"), ft.Icons.LINK_ROUNDED, lambda e: app.go("frame")),
                              preview=C.ghost_preview("files"))], expand=True)
        if self.root is None:
            self.root = ft.Column([
                app.top_bar(tr("Files"), tr("Videos, documents, mods and saves on your Steam Frame")),
                ft.Row([
                    C.card(ft.Column([C.meta(tr("Locations").upper()), self.locations], spacing=T.S2, expand=True),
                           padding=T.S3, width=T.px(260), expand=False),
                    ft.Column([
                        self.notice,
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
        selected = self.loc is not None and loc["id"] in (self.loc["id"], (self.loc.get("parent") or {}).get("id"))
        ic = ft.Icon(icon, size=T.px(18))
        label = C.body(loc["label"], weight=ft.FontWeight.W_500)
        box = ft.Container(
            ft.Row([ic, ft.Column([label, *([C.meta(sub)] if sub else [])], spacing=0, expand=True)], spacing=T.S2),
            padding=ft.Padding(T.S2, T.px(6), T.S2, T.px(6)), border_radius=T.RADIUS_SM, ink=True,
            on_click=lambda e, loc_=loc: self.open_location(loc_))
        C.selected_style(box, selected, ic, label)  # like the sidebar: the portal fade with a white icon
        return box

    def location_groups(self) -> list[tuple[str | None, list[tuple[dict, str, str]]]]:
        """[(heading, [(location, icon, subtitle)])]: the Locations column, and the Move to… picker's list."""
        groups = [(None, [(loc, SHARED_ICONS.get(loc["id"], ft.Icons.FOLDER_OUTLINED), loc["android"])
                          for loc in self.shared])]
        if self.drives:
            from ...install.drives import free_text

            groups.append((tr("Drives"), [(loc, ft.Icons.STORAGE_OUTLINED,
                                           " · ".join(filter(None, [free_text(loc), tr("Read-only")
                                                                    if loc.get("read_only") else ""])))
                                          for loc in self.drives]))
        kinds = {d["package"]: d.get("kind") for d in (self.app.frame_info or {}).get("installed", [])}
        installed = self._installed_games()
        for section, heading, icon in (("quest", tr("Game storage"), ft.Icons.SPORTS_ESPORTS_OUTLINED),
                                       ("pcvr", tr("PC VR games"), ft.Icons.DESKTOP_WINDOWS_ROUNDED),
                                       ("linux", tr("Linux apps"), ft.Icons.TERMINAL_ROUNDED)):
            games = [g for g in installed if game_section(kinds.get(g[0])) == section]
            if not games:
                continue
            groups.append((heading, [(self.game_locs.get(pkg) or self._game_location(pkg, title), icon, "")
                                     for pkg, title in games]))
        home = {"id": "home", "label": tr("Home folder"), "path": self.app.target.frame.home, "android": "",
                "shared": False}
        groups.append((tr("Advanced"), [
            (home, ft.Icons.HOME_OUTLINED, tr("everything in ~ (not seen by games)")),
            (self._tmp_location(), ft.Icons.HOURGLASS_EMPTY_ROUNDED, tr("/tmp · emptied at every restart"))]))
        return groups

    def _render_locations(self) -> None:
        rows = []
        for heading, entries in self.location_groups():
            if heading:
                rows.append(ft.Container(C.meta(heading.upper()), padding=ft.Padding(T.S2, T.S3, 0, T.px(2))))
            rows.extend(self._location_row(loc, icon, sub) for loc, icon, sub in entries)
        self.locations.controls = rows
        C.update(self.locations)

    @staticmethod
    def _tmp_location() -> dict:
        from ...install.files import tmp_location

        return {**tmp_location(), "label": tr("Temporary files")}

    def saves_location(self, storage: dict) -> dict:
        """A Quest game's internal storage (its saves), entered from its storage's "Saves (internal storage)" entry:
        a location of its own (the agent serves it) whose crumbs start at the game's storage (`parent`)."""
        from ...install.files import saves_location

        pkg = storage["package"]
        return {**saves_location(pkg), **(self.game_locs.get(f"saves:{pkg}") or {}),
                "label": tr("Saves (internal storage)"), "title": storage.get("label") or pkg, "parent": storage}

    @staticmethod
    def has_saves_entry(loc: dict | None, path: str) -> bool:
        """The top of a Quest game's storage (its /sdcard) lists its saves as a link-like entry."""
        return bool(loc) and loc["id"].startswith("app:") and loc.get("android") == "/sdcard" \
            and path == loc.get("path")

    def saves_entry(self, storage: dict):
        from ...install.files import Entry

        return Entry(tr("Saves (internal storage)"), f"saves://{storage['package']}", True, 0, 0.0, link=True,
                     saves=storage["package"])

    def nav_crumbs(self, loc: dict, path: str) -> list[tuple[str, dict, str]]:
        """(label, location, path) from the location's top to `path`; inside a game's saves: from its storage."""
        parent = loc.get("parent")
        out = [(label, parent, p) for label, p in crumbs(parent["path"], parent["path"], parent["label"])] \
            if parent else []
        return out + [(label, loc, p) for label, p in crumbs(loc["path"], path, loc["label"])]

    def _installed_games(self) -> list[tuple[str, str]]:
        from ...core import library

        out = []
        for d in (self.app.frame_info or {}).get("installed", []):
            g = library.game(d["package"]) or {}
            out.append((d["package"], g.get("title") or d.get("title") or d["package"]))
        return sorted(out, key=lambda x: x[1].lower())

    def _game_location(self, package: str, title: str) -> dict:
        dep = next((d for d in (self.app.frame_info or {}).get("installed", [])
                    if d["package"] == package), {"package": package})
        loc = game_location(dep, title, self.app.target.frame.home)
        if loc.get("path"):
            from ...install.files import inside

            for drive in self.drives or []:
                try:
                    inside(drive["path"], loc["path"])
                except ValueError:
                    continue
                loc["read_only"] = drive["read_only"]
        return loc

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
        if self.drives is None:
            try:  # the Frame target's list (as the Storage section and Move to… use), kept for the space check too
                listed = self.app.target.drives()
                self.app.frame_drives = listed
                self.drives = drive_locations(listed)
            except Exception:  # older agents / temporarily unavailable; retry on the next mount
                pass
        self._render_locations()
        if package:
            self.open_location(self._game_location(package, dict(self._installed_games()).get(package, package)))
        elif self.loc is None and self.shared:
            self.open_location(self.shared[0])
        elif self.loc is not None:
            self.load()

    def resolve(self, loc: dict) -> dict:
        """The location with its folder on the Frame (a game's storage and saves are asked for when first opened;
        blocking: call from a background thread). Raises when the Frame can't say."""
        from ...install import files

        frame = self.app.target.frame
        if loc.get("saves"):  # asked every time: whether the game runs now
            info = files.saves_info(frame, loc["saves"])
            self.saves_running[loc["saves"]] = bool(info.get("running"))
            loc = {**loc, "path": info["root"], "android": info.get("android") or "", "shared": False}
            self.game_locs[loc["id"]] = loc
            return loc
        if loc.get("path"):
            return loc
        t = {x["id"]: x for x in files.storage_targets(frame, loc["package"])}["app"]
        loc = {**loc, "path": t["path"], "android": t["android"], "shared": False}
        self.game_locs[loc["package"]] = loc
        return loc

    def open_location(self, loc: dict, path: str | None = None) -> None:
        def work():
            nonlocal loc
            try:
                loc = self.resolve(loc)
            except Exception as exc:  # noqa: BLE001
                self.app.toast((tr("Couldn't open the game's saves: {exc}") if loc.get("saves") else
                                tr("Couldn't open the game's storage: {exc}")).format(exc=explain(exc)), error=True)
                return
            self.loc, self.path = loc, path or loc["path"]
            self.selected.clear()
            for control in self.toolbar.controls[:3]:
                control.disabled = not self._writable()
            for control in self.sel_bar.content.controls[3:5]:  # Move to…, Delete…
                control.disabled = not self._writable()
            C.update(self.toolbar)
            self._render_notice()
            self._render_locations()
            self.load()
        self.app.run_bg(work)

    def _render_notice(self) -> None:
        """A callout above the folder for locations with rules of their own: /tmp is emptied at every restart; a
        game's saves can't change while it runs, and a backup first is a good idea."""
        loc = self.loc or {}
        content = None
        if loc.get("tmp"):
            content = C.callout(tr("The Frame empties this folder at every restart. Files that belong to other "
                                   "users can't be opened or changed here."))
        elif loc.get("saves"):
            pkg, title = loc["saves"], loc.get("title") or loc["saves"]
            text = (tr("{title} is running: close it to change its saves.").format(title=title)
                    if self.saves_running.get(pkg) else
                    tr("{title} keeps its saves and settings here. Changes are refused while the game runs; back "
                       "up the saves before you edit them.").format(title=title))
            content = C.callout(ft.Row([ft.Container(C.body(text, T.TEXT), expand=True),
                                        C.secondary(tr("Back up saves"), ft.Icons.SAVE_ALT_ROUNDED,
                                                    lambda e: self.app.backup_saves(pkg))],
                                       spacing=T.S3, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                kind="warn")
        self.notice.content = content
        self.notice.visible = content is not None
        C.update(self.notice)

    def load(self) -> None:
        if not self.loc:
            return
        loc, path = self.loc, self.path
        self.status.value = tr("Loading…")
        C.update(self.status)

        def work():
            from ...install import files

            try:
                entries = files.loc_list(self.app.target.frame, loc, path, hidden=self.hidden)
            except Exception as exc:  # noqa: BLE001
                self.status.value = tr("Couldn't list {path}: {exc}").format(path=path, exc=explain(exc))
                C.update(self.status)
                return
            if (self.loc, self.path) != (loc, path):
                return  # the user moved on meanwhile
            if self.has_saves_entry(loc, path):  # the game's saves, shown like Lepton's links
                entries = [self.saves_entry(loc), *entries]
            self.entries = entries
            self.selected &= {e.path for e in entries}
            self._render_listing()
        self.app.run_bg(work)

    # ---------------------------------------------------------------- listing
    def _render_listing(self) -> None:
        loc = self.loc
        self.crumb_row.controls = []
        for i, (label, cloc, p) in enumerate(self.nav_crumbs(loc, self.path)):
            if i:
                self.crumb_row.controls.append(ft.Icon(ft.Icons.CHEVRON_RIGHT_ROUNDED, size=T.px(16), color=T.TEXT_3))
            self.crumb_row.controls.append(C.ghost(label, on_click=lambda e, c=cloc, p=p: self._crumb(c, p),
                                                   color=T.TEXT if cloc is loc and p == self.path else T.TEXT_2))
        android = loc.get("android")
        rel = posixpath.relpath(self.path, loc["path"]) if self.path != loc["path"] else ""
        self.where.value = (tr("Games see this folder as {value}")
                            .format(value=posixpath.join(android, rel) if rel else android)
                            if android else self.path)
        rows = []
        self.checks, self.rows, self.hot = {}, {}, set()
        if self.path != loc["path"] or loc.get("parent"):
            rows.append(self._row(None))
        rows += [self._row(e) for e in self.entries]
        if not self.entries:
            rows.append(ft.Container(C.meta(tr("This folder is empty.")),
                                     padding=T.S4))
        self.listing.controls = rows
        n_dirs = sum(e.is_dir for e in self.entries)
        size = sum(e.size for e in self.entries if not e.is_dir)
        self.status.value = ", ".join([tr_n("{n} folder", "{n} folders", n_dirs),
                                       tr_n("{n} file", "{n} files", len(self.entries) - n_dirs), human(size)])
        self._update_selection(render=False)
        for c in (self.crumb_row, self.where, self.listing, self.status):
            C.update(c)
        offset, self._restore = self._restore, None
        if offset:
            try:
                self.app.page.run_task(self.listing.scroll_to, offset=offset, duration=0)
            except Exception:  # noqa: BLE001 - the view left the page meanwhile
                pass

    def _row(self, e) -> ft.Control:
        if e is None:  # ".."
            return ft.Container(ft.Row([ft.Icon(ft.Icons.ARROW_UPWARD_ROUNDED, size=T.px(20), color=T.TEXT_2),
                                        C.body("..", T.TEXT_2)], spacing=T.S3),
                                padding=ft.Padding(T.S3, T.px(8), T.S3, T.px(8)), border_radius=T.RADIUS_SM, ink=True,
                                on_click=lambda ev: self.go_up())
        if e.saves:  # the game's internal storage: opens like a folder, can't be changed itself (like Lepton's links)
            return self._saves_row(e)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.mtime)) if e.mtime else ""
        info = ((tr("folder") if e.is_dir else human(e.size)) + (tr(" · link") if e.link else "")
                + (f" · {when}" if when else ""))
        actions = [C.icon_btn(ft.Icons.DOWNLOAD_ROUNDED, tr("Download to this PC…"),
                              lambda ev, x=e: self._download_later([x]))]
        if self._writable() and not self._protected(e):
            actions += [C.icon_btn(ft.Icons.DRIVE_FILE_RENAME_OUTLINE_OUTLINED, tr("Rename…"),
                                   lambda ev, x=e: self.rename(x)),
                        C.icon_btn(ft.Icons.DRIVE_FILE_MOVE_OUTLINED, tr("Move to…"),
                                   lambda ev, x=e: self.move_to([x])),
                        C.icon_btn(ft.Icons.DELETE_OUTLINE_ROUNDED, tr("Delete…"), lambda ev, x=e: self.delete([x]))]
        check = ft.Checkbox(value=e.path in self.selected, active_color=T.ACCENT, check_color=T.ON_ACCENT,
                            on_change=lambda ev, p=e.path: self._toggle(p, ev.control.value),
                            disabled=self._protected(e))
        self.checks[e.path] = check
        # quiet rows: the checkbox and the actions show on hover, keyboard focus or when something is selected
        for c in (check, *actions):
            c.on_focus = lambda ev, p=e.path: self._focus(p, True)
            c.on_blur = lambda ev, p=e.path: self._focus(p, False)
        quiet_check, quiet_actions = C.quiet(check), C.quiet(*actions)
        row = ft.Container(
            ft.Row([quiet_check,
                    ft.Icon(file_icon(e.name, e.is_dir), size=T.px(20), color=T.ACCENT if e.is_dir else T.TEXT_2),
                    ft.Column([C.body(e.name, T.TEXT, weight=ft.FontWeight.W_500, max_lines=1,
                                      overflow=ft.TextOverflow.ELLIPSIS), C.meta(info)], spacing=0, expand=True),
                    quiet_actions], spacing=T.S3),
            padding=ft.Padding(T.S3, T.px(6), T.S2, T.px(6)), border_radius=T.RADIUS_SM, ink=e.is_dir,
            on_click=(lambda ev, p=e.path: self.cd(p)) if e.is_dir else None,
            on_hover=lambda ev, p=e.path: self._hover(p, ev.data in (True, "true")))
        self.rows[e.path] = (row, quiet_check, quiet_actions)
        self._paint_row(e.path)
        return ft.GestureDetector(content=row, on_secondary_tap_down=lambda ev, x=e: self.open_menu(x,
                                                                                                    ev.global_position))

    def _saves_row(self, e) -> ft.Control:
        row = ft.Container(
            ft.Row([ft.Container(width=T.px(40)),  # where other rows have their checkbox
                    ft.Icon(ft.Icons.SAVE_OUTLINED, size=T.px(20), color=T.ACCENT),
                    ft.Column([C.body(e.name, T.TEXT, weight=ft.FontWeight.W_500, max_lines=1,
                                      overflow=ft.TextOverflow.ELLIPSIS),
                               C.meta(tr("link → the game's internal storage (/data/data/{package}): saves and "
                                         "settings").format(package=e.saves))], spacing=0, expand=True)],
                   spacing=T.S3),
            padding=ft.Padding(T.S3, T.px(6), T.S2, T.px(6)), border_radius=T.RADIUS_SM, ink=True,
            tooltip=tr("Open the game's saves"), on_click=lambda ev: self.open_saves())
        return ft.GestureDetector(content=row, on_secondary_tap_down=lambda ev, x=e: self.open_menu(x,
                                                                                                    ev.global_position))

    def open_saves(self) -> None:
        """Into the game's internal storage (from its storage's "Saves (internal storage)" entry)."""
        if self.loc and self.loc["id"].startswith("app:"):
            self.open_location(self.saves_location(self.loc))

    def _crumb(self, loc: dict, path: str) -> None:
        if loc is self.loc:
            self.cd(path)
        else:  # from inside a game's saves up to its storage
            self.open_location(loc, path)

    @staticmethod
    def loc_name(loc: dict) -> str:
        """A location's name for job titles: "<game> › Saves (internal storage)" inside a game's saves."""
        return f"{loc['title']} › {loc['label']}" if loc.get("parent") else loc.get("label") or loc.get("path", "")

    # ---------------------------------------------------------------- right-click menu
    def menu_actions(self, e) -> list[tuple | None]:
        """[(label, icon, handler) | None] for a right-click on entry `e` (None = on the folder's empty space). With
        several entries selected and `e` among them, the actions act on the whole selection."""
        if e is None:
            return [*([(tr("Upload files…"), ft.Icons.UPLOAD_FILE_OUTLINED, self.upload_files),
                    (tr("Upload folder…"), ft.Icons.DRIVE_FOLDER_UPLOAD_OUTLINED, self.upload_folder),
                    (tr("New folder…"), ft.Icons.CREATE_NEW_FOLDER_OUTLINED, lambda ev: self.new_folder())]
                   if self._writable() else []),
                    None,
                    *([(tr("Select all"), ft.Icons.SELECT_ALL_ROUNDED, lambda ev: self._select_all())]
                      if any(self._selectable(x) for x in self.entries) else []),
                    (tr("Copy folder path"), ft.Icons.CONTENT_COPY_ROUNDED,
                     lambda ev: self.app.copy(self._shown_path(self.path))),
                    (tr("Refresh"), ft.Icons.REFRESH_ROUNDED, lambda ev: self.load())]
        if e.saves:
            return [(tr("Open"), ft.Icons.FOLDER_OPEN_ROUNDED, lambda ev: self.open_saves())]
        by_path = {x.path: x for x in self.entries}
        items = [by_path[p] for p in C.menu_targets(e.path, [x.path for x in self.entries if x.path in self.selected])]
        several = len(items) > 1
        deletable = [x for x in items if self._writable() and not self._protected(x)]
        out: list[tuple | None] = []
        if not several and e.is_dir:
            out.append((tr("Open"), ft.Icons.FOLDER_OPEN_ROUNDED, lambda ev: self.cd(e.path)))
        out.append((tr("Download {n} items to this PC…").format(n=len(items)) if several
                    else tr("Download to this PC…"), ft.Icons.DOWNLOAD_ROUNDED,
                    lambda ev: self._download_later(items)))
        if not several and self._writable() and not self._protected(e):
            out.append((tr("Rename…"), ft.Icons.DRIVE_FILE_RENAME_OUTLINE_OUTLINED, lambda ev: self.rename(e)))
        if not several:
            out.append((tr("Copy path"), ft.Icons.CONTENT_COPY_ROUNDED, lambda ev: self.app.copy(self._shown_path(
                e.path))))
        out.append(None)
        if several:
            out.append((tr("Clear selection"), ft.Icons.CLOSE_ROUNDED, lambda ev: self._clear_selection()))
        elif self._selectable(e):
            on = e.path in self.selected
            out.append((tr("Deselect") if on else tr("Select"),
                        ft.Icons.CHECK_BOX_OUTLINE_BLANK_ROUNDED if on else ft.Icons.CHECK_BOX_OUTLINED,
                        lambda ev: self._toggle(e.path, not on)))
        if deletable:
            out += [None, (tr("Move {n} items to…").format(n=len(deletable)) if len(deletable) > 1 else tr("Move to…"),
                           ft.Icons.DRIVE_FILE_MOVE_OUTLINED, lambda ev: self.move_to(deletable)),
                    (tr("Delete {n} items…").format(n=len(deletable)) if len(deletable) > 1 else tr("Delete…"),
                     ft.Icons.DELETE_OUTLINE_ROUNDED, lambda ev: self.delete(deletable))]
        return out

    def open_menu(self, e, position=None) -> None:
        if not self.loc:
            return
        self.app.popmenu.show(self.menu_actions(e), at=position)

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
        from ...install.files import inside

        if path.startswith("saves://"):
            self.open_saves()
            return
        new = inside(self.loc["path"], path)
        # going back up (the ".." row, a crumb, Backspace) returns to where that folder's list was scrolled
        going_up = new != self.path and (self.path + "/").startswith(new.rstrip("/") + "/")
        self._restore = self.scrolled.get(new) if going_up else None
        if not going_up:
            self.scrolled.pop(new, None)
        self.path = new
        self.selected.clear()
        self.load()

    def go_up(self) -> bool:
        """Backspace: the folder above, inside the location. False at the location's top."""
        if not self.loc:
            return False
        if self.path == self.loc["path"]:
            if not self.loc.get("parent"):
                return False
            self.open_location(self.loc["parent"])  # from a game's saves back to its storage
            return True
        self.cd(posixpath.dirname(self.path))
        return True

    def _remember_scroll(self, e) -> None:
        if self.path:
            self.scrolled[self.path] = e.pixels

    def _toggle(self, path: str, on: bool) -> None:
        (self.selected.add if on else self.selected.discard)(path)
        check = self.checks.get(path)
        if check is not None and check.value != on:
            check.value = on
            C.update(check)
        if self._paint_row(path):
            C.update(self.rows[path][0])
        self._update_selection()

    # ---------------------------------------------------------------- quiet rows
    def _hover(self, path: str, inside: bool) -> None:
        self.drag.hover(path, inside)
        self._set_hot(path, inside)

    def _focus(self, path: str, on: bool) -> None:
        self._set_hot(path, on)

    def _set_hot(self, path: str, on: bool) -> None:
        (self.hot.add if on else self.hot.discard)(path)
        if path in self.rows and self._paint_row(path):
            C.update(self.rows[path][0])

    def _paint_row(self, path: str) -> bool:
        """Properties only: the selected look and whether the checkbox/actions show. True when something changed."""
        row, quiet_check, quiet_actions = self.rows[path]
        on = path in self.selected
        before = (row.bgcolor, row.gradient is not None)
        C.selected_style(row, on)
        changed = before != (row.bgcolor, row.gradient is not None)
        show = on or path in self.hot or bool(self.selected)
        changed |= C.reveal(quiet_check, show)
        changed |= C.reveal(quiet_actions, on or path in self.hot)
        return changed

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

    def _writable(self) -> bool:
        return bool(self.loc) and not self.loc.get("read_only", False)

    def _protected(self, e) -> bool:
        """Lepton's links at the top of a game's storage (Movies → ~/Videos, …): renaming/deleting them breaks them;
        the game's saves entry isn't a real file at all."""
        if e.saves:
            return True
        return (self.loc["id"].startswith("app:") and self.loc.get("android") == "/sdcard"
                and e.link and posixpath.dirname(e.path) == posixpath.normpath(self.loc["path"]))

    def _update_selection(self, render: bool = True) -> None:
        n = len(self.selected)
        size = sum(x.size for x in self.entries if x.path in self.selected and not x.is_dir)
        self.sel_label.value = (tr("{n} selected").format(n=n)
                                + (tr(" · {human} in files").format(human=human(size)) if size else ""))
        self.sel_bar.visible = bool(n)
        if bool(n) != self._select_mode:  # select mode starts/ends: every row's checkbox shows/hides
            self._select_mode = bool(n)
            changed = [path for path in self.rows if self._paint_row(path)]
            if render and changed:
                C.update(self.listing)
        self.select_all.value = bool(self.entries) and n == len([x for x in self.entries if self._selectable(x)])
        if render:
            for c in (self.sel_bar, self.select_all):
                C.update(c)

    def _chosen(self) -> list:
        return [x for x in self.entries if x.path in self.selected]

    async def _download_selected(self, e=None):
        if self.selected:
            await self.download(self._chosen())

    def _move_selected(self) -> None:
        items = [x for x in self._chosen() if not self._protected(x)]
        if items:
            self.move_to(items)

    def _delete_selected(self) -> None:
        items = [x for x in self._chosen() if not self._protected(x)]
        if items:
            self.delete(items)

    def _toggle_hidden(self, e) -> None:
        self.hidden = bool(e.control.value)
        self.load()

    # ---------------------------------------------------------------- actions
    async def upload_files(self, e=None):
        paths = [Path(p) for p in await C.pick_files(self.app.page, multiple=True)]
        if paths:
            self.upload(paths)

    async def upload_folder(self, e=None):
        path = await C.pick_dir(self.app.page, dialog_title=tr("Folder to upload to the Frame"))
        if path:
            self.upload([Path(path)])

    def upload(self, paths: list[Path]) -> None:
        if not self._writable():
            return
        from ...install.files import inside

        app, loc, dest = self.app, self.loc, inside(self.loc["path"], self.path)

        def run(job):
            from ...install import files

            sent, skipped, total = files.loc_upload(app.target.frame, loc, paths, dest, job.reporter)
            return (tr_n("Uploaded {n} file", "Uploaded {n} files", len(sent))
                    + (tr_n(", {n} already there", ", {n} already there", len(skipped)) if skipped else ""))
        app.submit(tr("Upload to {label}").format(label=self.loc_name(loc)), run, loc.get("package"), kind="tool-frame",
                   open_panel=True)

    async def download(self, items: list) -> None:
        folder = await C.pick_dir(self.app.page, dialog_title=tr("Download to which folder on this PC?"))
        if not folder:
            return
        app, loc = self.app, self.loc
        expected = sum(x.size for x in items if not x.is_dir)

        def run(job):
            from ...install import files

            r = files.loc_download(app.target.frame, loc, [x.path for x in items], Path(folder), job.reporter,
                                   expected=expected)
            return tr_n("Downloaded {n} file to {folder}", "Downloaded {n} files to {folder}", r["files"],
                        folder=r["folder"])
        more = tr_n(" and {n} more", " and {n} more", len(items) - 1) if len(items) > 1 else ""
        app.submit(tr("Download {name}").format(name=items[0].name) + more, run,
                   None, kind="tool-frame", open_panel=True)

    def new_folder(self) -> None:
        if not self._writable():
            return
        self._ask_name(tr("New folder"), tr("Folder name"), "", tr("Create"), lambda name: self._fs(
            lambda files, frame: files.loc_make_dir(frame, self.loc, self.path, name)))

    def rename(self, e) -> None:
        if not self._writable():
            return
        self._ask_name(tr("Rename {name}").format(name=e.name), tr("New name"), e.name, tr("Rename"),
                       lambda name: self._fs(
                           lambda files, frame: files.loc_rename(frame, self.loc, e.path, name)))

    def delete(self, items: list) -> None:
        if not self._writable():
            return
        names = ", ".join(x.name for x in items[:3]) + (" …" if len(items) > 3 else "")
        what = tr("folder and everything in it") if any(x.is_dir for x in items) else tr("file")
        C.confirm(self.app.page, tr("Delete {names}?").format(names=names),
                  tr("This deletes the {what} on the Frame. It can't be undone.").format(what=what),
                  tr("Delete"), lambda: (self.selected.difference_update(x.path for x in items),
                                     self._fs(lambda files, frame: files.loc_delete(frame, self.loc,
                                                                                    [x.path for x in items]))),
                  danger=True)

    def move_to(self, items: list) -> None:
        """Move to…: pick a folder in any location (views/files_move.py); the move is a job (it may copy)."""
        items = [x for x in items if not self._protected(x)]
        if not self._writable() or not items:
            return
        from .files_move import MovePicker

        MovePicker(self, self.loc, items).show()

    def start_move(self, src: dict, items: list, dest: dict, folder: str) -> None:
        app = self.app

        def run(job):
            from ...install import files

            r = files.move(app.target.frame, src, [x.path for x in items], dest, folder, job.reporter)
            self.selected.difference_update(x.path for x in items)
            self.load()
            return tr_n("Moved {n} item to {label}", "Moved {n} items to {label}", r["moved"],
                        label=self.loc_name(dest) or folder)
        more = tr_n(" and {n} more", " and {n} more", len(items) - 1) if len(items) > 1 else ""
        app.submit(tr("Move {name}").format(name=items[0].name) + more, run,
                   src.get("package") or dest.get("package"), kind="tool-frame", open_panel=True)

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
