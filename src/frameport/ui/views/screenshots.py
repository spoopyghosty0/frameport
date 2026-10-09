"""Screenshots: the Steam screenshots taken on the Frame, newest first, grouped by day, filterable by game.

The Frame files every headset screenshot under SteamVR; the agent matches them to FramePort games by play session
(install/screenshots.py). Persistent like the Library: the filter survives switching tabs. Thumbnails are downloaded
once into the cache and shown by asset URL; cards stream in batches from a background thread. Downloads are jobs.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from ...artwork import thumbs
from ...errors import explain
from ...i18n import tr, tr_n
from .. import components as C
from .. import glyphs as G
from .. import theme as T
from .files_dialog import human

if TYPE_CHECKING:
    from ..app import FramePortApp

BATCH = 12
ALL = "__all__"
OTHER = "__other__"  # shots of no FramePort game (SteamVR home, Steam games)


def day_label(t: float | None, now: float | None = None) -> str:
    if not t:
        return tr("Unknown date")
    now = now or time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(t))
    if day == time.strftime("%Y-%m-%d", time.localtime(now)):
        return tr("Today")
    if day == time.strftime("%Y-%m-%d", time.localtime(now - 86400)):
        return tr("Yesterday")
    return day


def group_by_day(shots: list[dict], now: float | None = None) -> list[tuple[str, list[dict]]]:
    """[(day label, shots)] in the order given (newest first)."""
    out: list[tuple[str, list[dict]]] = []
    for s in shots:
        label = day_label(s.get("time"), now)
        if out and out[-1][0] == label:
            out[-1][1].append(s)
        else:
            out.append((label, [s]))
    return out


def filter_key(package: str | None) -> str:
    return ALL if package is None else (package or OTHER)


def agent_filter(key: str) -> str | None:
    """The agent's `package` argument for a dropdown key."""
    return None if key == ALL else "" if key == OTHER else key


class ScreenshotsView:
    def __init__(self, app: FramePortApp):
        self.app = app
        self.filter = ALL
        self.shots: list[dict] = []
        self.games: list[dict] = []
        self.selected: set[str] = set()  # paths
        self.checks: dict[str, ft.Checkbox] = {}
        self.tiles: dict[str, tuple] = {}  # path → (tile, quiet checkbox, selection overlay)
        self.copy_btns: dict[str, ft.Control] = {}  # path → the card's "Copy image" button (shown on hover)
        self.hot: set[str] = set()  # tiles under the mouse or with keyboard focus
        self._gen = 0
        self._loaded = False
        self._lock = threading.Lock()
        self.dropdown = C.dropdown(label=tr("Game"), value=ALL, width=T.px(300),
                                    options=[ft.DropdownOption(key=ALL, text=tr("All games"))],
                                    on_select=lambda e: self.set_filter(e.control.value))
        self.select_all = ft.Checkbox(value=False, active_color=T.ACCENT, check_color=T.ON_ACCENT,
                                      label=tr("Select all"), label_style=ft.TextStyle(color=T.TEXT_2, size=T.T_BODY),
                                      on_change=self._toggle_all)
        # a headset screenshot now (SteamVR's own, like the dashboard's button): appears in the grid when Steam saved it
        self.take_btn = C.primary(tr("Take screenshot"), G.SHOT, self._take)
        self.toolbar = ft.Row([
            self.dropdown, self.select_all, ft.Container(expand=True),
            self.take_btn,
            C.secondary(tr("Download all…"), ft.Icons.DOWNLOAD_ROUNDED, self._download_all),
            C.icon_btn(ft.Icons.REFRESH_ROUNDED, tr("Refresh"), lambda e: self.load()),
        ], spacing=T.S2, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self.sel_label = C.body("", T.TEXT, weight=ft.FontWeight.W_500)
        self.sel_bar = ft.Container(ft.Row([
            self.sel_label, ft.Container(expand=True),
            C.secondary(tr("Download selected…"), ft.Icons.DOWNLOAD_ROUNDED, self._download_selected),
            C.ghost(tr("Delete…"), ft.Icons.DELETE_OUTLINE_ROUNDED, lambda e: self.delete(self._chosen())),
            C.ghost(tr("Clear"), ft.Icons.CLOSE_ROUNDED, lambda e: self._clear_selection()),
        ], spacing=T.S2), padding=ft.Padding(T.S3, T.px(6), T.S2, T.px(6)), border_radius=T.RADIUS_SM,
            bgcolor=T.ACCENT_SOFT, visible=False)
        self.grid = ft.Column(spacing=T.S3, scroll=ft.ScrollMode.AUTO, expand=True)
        self.status = C.meta("")
        # one right-click menu for the grid: filled with the clicked screenshot's actions (or the tab's) when it opens
        # click and drag across screenshots to select them (C.DragSelect)
        self.drag = C.DragSelect(lambda p: p in self.selected, self._toggle)
        self.menu = ft.ContextMenu(content=ft.GestureDetector(
            content=C.card(self.grid, padding=T.S3, expand=True), expand=True,
            on_secondary_tap_down=lambda e: self.open_menu(None, e.global_position),
            on_pan_start=self.drag.start, on_pan_end=self.drag.end),
            secondary_trigger=None, tertiary_trigger=None, expand=True)
        self.root = None

    # ---------------------------------------------------------------- building
    def mount(self, package: str | None = None) -> ft.Control:
        app = self.app
        heading, sub = tr("Screenshots"), tr("Pictures you took in the headset (Steam screenshots on the Frame)")
        if not (app.target and app.frame_state == "connected"):
            self._loaded = False  # load again once connected
            return ft.Column([
                app.top_bar(heading, sub),
                C.empty_state(G.SHOT, tr("Every shot you take in the headset"),
                              tr("Connect to your Steam Frame to see its screenshots here."),
                              C.primary(tr("Connect"), ft.Icons.LINK_ROUNDED, lambda e: app.go("frame")),
                              features=[
                                  (G.SHOT, tr("Sorted by day"),
                                   tr("All of Steam's headset screenshots, newest first.")),
                                  (ft.Icons.VIDEOGAME_ASSET_OUTLINED, tr("Matched to your games"),
                                   tr("Each shot is labelled with the game you were playing.")),
                                  (ft.Icons.DOWNLOAD_ROUNDED, tr("Save them to this PC"),
                                   tr("Download one, a selection or all of them.")),
                              ])], expand=True)
        if self.root is None:
            # the selection bar below the grid (as in the Library): above it, it pushed the cards down mid-drag
            self.root = ft.Column([app.top_bar(heading, sub), self.toolbar, self.menu, self.sel_bar, self.status],
                                  spacing=T.S3, expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        if package is not None and filter_key(package) != self.filter:
            self.filter = filter_key(package)
            self.dropdown.value = self.filter
            self.selected.clear()
            self._loaded = False
        if not self._loaded:  # later visits keep what was shown (Refresh reloads)
            self._loaded = True
            self.load()
        return self.root

    def set_filter(self, key: str | None) -> None:
        self.filter = key or ALL
        self.selected.clear()
        self.load()

    # ---------------------------------------------------------------- loading (background thread)
    def load(self) -> None:
        self._gen += 1
        self.status.value = tr("Loading…")
        C.update(self.status)
        self.app.run_bg(self._load, self._gen, self.filter)

    def _load(self, gen: int, key: str) -> None:
        from ...install import screenshots

        frame = self.app.target.frame
        with self._lock:
            if gen != self._gen:
                return
            try:
                r = screenshots.list_shots(frame, package=agent_filter(key))
            except Exception as exc:  # noqa: BLE001
                self.status.value = tr("Couldn't list the screenshots: {exc}").format(exc=explain(exc))
                C.update(self.status)
                return
            if gen != self._gen:
                return
            self.shots, self.games = r.get("shots") or [], r.get("games") or []
            self.selected &= {s["path"] for s in self.shots}
            self._fill_dropdown()
            self.checks, self.tiles, self.hot = {}, {}, set()
            self.grid.controls = []
            if not self.shots:
                empty = C.empty_state(
                    ft.Icons.PHOTO_CAMERA_OUTLINED, tr("No screenshots yet"),
                    tr("Take one in the headset with Steam's screenshot shortcut; it shows up here. Screenshots "
                       "are matched to the FramePort game that was running."))
                empty.expand, empty.padding = False, T.px(48)  # (expand inside a scrolling column: invalid layout)
                self.grid.controls = [empty]
            self._update_selection(render=False)
            self.status.value = tr_n("{n} screenshot", "{n} screenshots", len(self.shots)) + \
                ((" · " + human(sum(s.get("size") or 0 for s in self.shots))) if self.shots else "")
            for c in (self.grid, self.status, self.dropdown):
                C.update(c)
            sections: dict[str, ft.Row] = {}
            index = {s["path"]: i for i, s in enumerate(self.shots)}
            for start in range(0, len(self.shots), BATCH):
                if gen != self._gen:
                    return  # the filter changed or a refresh started: that load takes over
                batch = self.shots[start:start + BATCH]
                for s in batch:
                    try:
                        screenshots.thumb_path(frame, s)
                    except Exception:  # noqa: BLE001  (the card shows a placeholder)
                        pass
                for label, shots in group_by_day(batch):
                    row = sections.get(label)
                    if row is None:
                        row = sections[label] = ft.Row(spacing=T.S3, run_spacing=T.S3, wrap=True)
                        self.grid.controls += [C.h2(label), row]
                    row.controls += [self.card(s, index[s["path"]]) for s in shots]
                C.update(self.grid)

    def _fill_dropdown(self) -> None:
        opts = [ft.DropdownOption(key=ALL, text=tr("All games"))]
        for g in self.games:
            key = g.get("package") or OTHER
            title = g.get("title") if g.get("package") else tr("Not from a FramePort game")
            opts.append(ft.DropdownOption(key=key, text=f"{title} ({g.get('count', 0)})"))
        if self.filter not in {o.key for o in opts}:  # a game without screenshots (opened from its page)
            from ...core import library

            pkg = agent_filter(self.filter) or ""
            opts.append(ft.DropdownOption(key=self.filter, text=(library.game(pkg) or {}).get("title") or pkg))
        self.dropdown.options = opts  # the agent counts `games` before filtering: the same list for every filter
        self.dropdown.value = self.filter

    # ---------------------------------------------------------------- cards
    def _thumb_url(self, s: dict) -> str | None:
        from ...install import screenshots

        p = screenshots.cached(self.app.target.frame, s, "thumb")
        return thumbs.asset_url(p) if p else None

    def card(self, s: dict, i: int) -> ft.Control:
        when = time.strftime("%H:%M", time.localtime(s["time"])) if s.get("time") else ""
        check = ft.Checkbox(value=s["path"] in self.selected, active_color=T.ACCENT, check_color=T.ON_ACCENT,
                            on_change=lambda e, p=s["path"]: self._toggle(p, e.control.value))
        self.checks[s["path"]] = check
        caption = ft.Container(ft.Row([C.meta(s.get("title") or "", T.TEXT, expand=True, max_lines=1,
                                              overflow=ft.TextOverflow.ELLIPSIS), C.meta(when, T.TEXT_2)],
                                      spacing=T.S2),
                               left=0, right=0, bottom=0, padding=ft.Padding(T.S2, T.px(14), T.S2, T.px(6)),
                               gradient=ft.LinearGradient(begin=ft.Alignment.TOP_CENTER,
                                                          end=ft.Alignment.BOTTOM_CENTER,
                                                          colors=[ft.Colors.TRANSPARENT, T.soft("#000000", 0.8)]),
                               border_radius=ft.BorderRadius(0, 0, T.RADIUS_SM, T.RADIUS_SM))
        check.on_focus = lambda e, p=s["path"]: self._set_hot(p, True)
        check.on_blur = lambda e, p=s["path"]: self._set_hot(p, False)
        quiet_check = C.quiet(check)
        quiet_check.left, quiet_check.top = 0, 0
        # (hidden, not just transparent, off hover: a click on that corner then opens the viewer as everywhere else)
        copy_btn = ft.Container(
            ft.Icon(ft.Icons.CONTENT_COPY_ROUNDED, size=T.px(16), color=T.TEXT), width=T.px(30), height=T.px(30),
            alignment=ft.Alignment.CENTER, border_radius=T.px(15), bgcolor=T.soft("#000000", 0.55), ink=True,
            tooltip=tr("Copy image"), visible=False, on_click=lambda e, s=s: self.copy(s))
        copy_btn.right, copy_btn.top = T.px(6), T.px(6)
        self.copy_btns[s["path"]] = copy_btn
        overlay = ft.Container(left=0, right=0, top=0, bottom=0, border_radius=T.RADIUS_SM)
        tile = ft.Container(
            ft.Stack([C.art_fill(self._thumb_url(s), radius=T.RADIUS_SM, placeholder_icon=ft.Icons.IMAGE_OUTLINED,
                                 left=0, right=0, top=0, bottom=0),
                      overlay, caption, quiet_check, copy_btn]),
            width=T.px(256), height=T.px(144), border_radius=T.RADIUS_SM, ink=True,
            border=ft.Border.all(2, ft.Colors.TRANSPARENT),
            tooltip=f"{s.get('title') or ''} · {day_label(s.get('time'))} {when}",
            on_click=lambda e, i=i: self.viewer(i),
            on_hover=lambda e, p=s["path"]: self._hover(p, e.data in (True, "true")))
        self.tiles[s["path"]] = (tile, quiet_check, overlay)
        self._paint(s["path"])
        return ft.GestureDetector(content=tile, on_secondary_tap_down=lambda e, s=s: self.open_menu(s,
                                                                                                  e.global_position))

    # ---------------------------------------------------------------- right-click menu
    def menu_actions(self, s: dict | None) -> list[tuple | None]:
        """[(label, icon, handler) | None] for a right-click on screenshot `s` (None = on the grid's empty space). With
        several screenshots selected and `s` among them, the actions act on the whole selection."""
        if s is None:
            if not self.shots:
                return [(tr("Refresh"), ft.Icons.REFRESH_ROUNDED, lambda e: self.load())]
            return [(tr("Download all ({n})…").format(n=len(self.shots)), ft.Icons.DOWNLOAD_ROUNDED,
                     lambda e: self._later(self.download, list(self.shots))),
                    (tr("Select all"), ft.Icons.SELECT_ALL_ROUNDED, lambda e: self._select_all()),
                    *([(tr("Clear selection"), ft.Icons.CLOSE_ROUNDED, lambda e: self._clear_selection())]
                      if self.selected else []),
                    None, (tr("Refresh"), ft.Icons.REFRESH_ROUNDED, lambda e: self.load())]
        by_path = {x["path"]: x for x in self.shots}
        shots = [by_path[p] for p in C.menu_targets(s["path"], [x["path"] for x in self.shots
                                                                if x["path"] in self.selected])]
        several = len(shots) > 1
        out: list[tuple | None] = []
        if not several:
            out.append((tr("View"), ft.Icons.OPEN_IN_FULL_ROUNDED, lambda e: self.viewer(self.shots.index(s))))
            out.append((tr("Copy image"), ft.Icons.CONTENT_COPY_ROUNDED, lambda e: self.copy(s)))
        out.append((tr("Download {n} screenshots…").format(n=len(shots)) if several else tr("Download…"),
                    ft.Icons.DOWNLOAD_ROUNDED, lambda e: self._later(self.download, shots)))
        key = filter_key(s.get("package") or "")
        if not several and self.filter == ALL and key in {o.key for o in self.dropdown.options}:
            out.append((tr("Show only this game's screenshots") if s.get("package")
                        else tr("Show only screenshots not from a FramePort game"),
                        ft.Icons.FILTER_ALT_OUTLINED, lambda e: self._filter_to(key)))
        out.append(None)
        if several:
            out.append((tr("Clear selection"), ft.Icons.CLOSE_ROUNDED, lambda e: self._clear_selection()))
        else:
            on = s["path"] in self.selected
            out.append((tr("Deselect") if on else tr("Select"),
                        ft.Icons.CHECK_BOX_OUTLINE_BLANK_ROUNDED if on else ft.Icons.CHECK_BOX_OUTLINED,
                        lambda e: self._toggle(s["path"], not on)))
        out += [None, (tr("Delete {n} screenshots…").format(n=len(shots)) if several else tr("Delete…"),
                       ft.Icons.DELETE_OUTLINE_ROUNDED, lambda e: self.delete(shots))]
        return out

    def open_menu(self, s: dict | None, position=None) -> None:
        self.menu.items = C.menu_items(self.menu_actions(s))
        C.update(self.menu)
        self.app.page.run_task(self.menu.open, global_position=position)

    def _later(self, coro_fn, *args) -> None:
        """Run an async action (it asks for a folder) from a plain click handler: Flet only awaits handlers that
        are coroutine functions themselves."""
        self.app.page.run_task(coro_fn, *args)

    def _filter_to(self, key: str) -> None:
        self.dropdown.value = key
        C.update(self.dropdown)
        self.set_filter(key)

    # ---------------------------------------------------------------- selection
    def _toggle(self, path: str, on: bool) -> None:
        (self.selected.add if on else self.selected.discard)(path)
        self._update_selection()

    def _toggle_all(self, e) -> None:
        self.selected = {s["path"] for s in self.shots} if e.control.value else set()
        self._update_selection()

    def _select_all(self) -> None:
        self.selected = {s["path"] for s in self.shots}
        self._update_selection()

    def _clear_selection(self) -> None:
        self.selected.clear()
        self._update_selection()

    # quiet tiles: the checkbox shows on hover, keyboard focus or while anything is selected
    def _hover(self, path: str, inside: bool) -> None:
        self.drag.hover(path, inside)
        self._set_hot(path, inside)

    def _set_hot(self, path: str, on: bool) -> None:
        (self.hot.add if on else self.hot.discard)(path)
        if path in self.tiles and self._paint(path):
            C.update(self.tiles[path][0])

    def _paint(self, path: str) -> bool:
        """Properties only: checkbox value + visibility, the accent border and the selection fade. True = changed."""
        tile, quiet_check, overlay = self.tiles[path]
        on = path in self.selected
        check = self.checks[path]
        copy_btn = self.copy_btns.get(path)
        before = (check.value, quiet_check.opacity, overlay.bgcolor, overlay.gradient is not None,
                  copy_btn.visible if copy_btn else None)
        check.value = on
        C.reveal(quiet_check, on or path in self.hot or bool(self.selected))
        if copy_btn is not None:
            copy_btn.visible = path in self.hot
        C.selected_style(overlay, on, subtle=True)
        tile.border = ft.Border.all(2, T.ACCENT if on else ft.Colors.TRANSPARENT)
        return before != (check.value, quiet_check.opacity, overlay.bgcolor, overlay.gradient is not None,
                          copy_btn.visible if copy_btn else None)

    def _update_selection(self, render: bool = True) -> None:
        n = len(self.selected)
        size = sum(s.get("size") or 0 for s in self._chosen())
        self.sel_label.value = tr("{n} selected").format(n=n) + (f" · {human(size)}" if size else "")
        self.sel_bar.visible = bool(n)
        self.select_all.value = bool(self.shots) and n == len(self.shots)
        changed = [path for path in self.tiles if self._paint(path)]
        if render:
            for c in (self.sel_bar, self.select_all):
                C.update(c)
            for path in changed:
                C.update(self.tiles[path][0])

    def _chosen(self) -> list[dict]:
        return [s for s in self.shots if s["path"] in self.selected]

    # ---------------------------------------------------------------- viewer
    def viewer(self, index: int) -> None:
        if not self.shots:
            return
        page, shots = self.app.page, list(self.shots)
        state = {"i": index}
        img = ft.Image(src=self._thumb_url(shots[index]) or "", fit=ft.BoxFit.CONTAIN, width=T.px(1100),
                       height=T.px(620), border_radius=T.RADIUS_SM)
        caption = C.body("", T.TEXT, weight=ft.FontWeight.W_500)
        details = C.meta("")

        def show(delta: int = 0) -> None:
            state["i"] = (state["i"] + delta) % len(shots)
            s = shots[state["i"]]
            img.src = self._thumb_url(s) or img.src
            caption.value = s.get("title") or ""
            dims = f"{s['width']}×{s['height']} · " if s.get("width") and s.get("height") else ""
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["time"])) if s.get("time") else ""
            details.value = f"{state['i'] + 1} / {len(shots)} · {when} · {dims}{human(s.get('size') or 0)}"
            C.update(img, caption, details)
            self.app.run_bg(full, state["i"])

        def full(i: int) -> None:
            from ...install import screenshots

            try:
                p = screenshots.image_path(self.app.target.frame, shots[i])
            except Exception as exc:  # noqa: BLE001
                details.value += " · " + tr("couldn't load the full image: {exc}").format(exc=explain(exc))
                C.update(details)
                return
            if state["i"] == i:
                img.src = thumbs.asset_url(p)
                C.update(img)

        async def download(e):
            await self.download([shots[state["i"]]])

        def delete(e):
            page.pop_dialog()
            self.delete([shots[state["i"]]])
        page.show_dialog(C.viewer(
            ft.Container(ft.Column([img, ft.Row([
                C.icon_btn(ft.Icons.CHEVRON_LEFT_ROUNDED, tr("Previous"), lambda e: show(-1)),
                ft.Column([caption, details], spacing=0, expand=True),
                C.icon_btn(ft.Icons.CHEVRON_RIGHT_ROUNDED, tr("Next"), lambda e: show(1)),
                C.secondary(tr("Copy"), ft.Icons.CONTENT_COPY_ROUNDED, lambda e: self.copy(shots[state["i"]])),
                C.secondary(tr("Download…"), ft.Icons.DOWNLOAD_ROUNDED, download),
                C.icon_btn(ft.Icons.DELETE_OUTLINE_ROUNDED, tr("Delete…"), delete),
                C.ghost(tr("Close"), on_click=lambda e: page.pop_dialog())], spacing=T.S2,
                vertical_alignment=ft.CrossAxisAlignment.CENTER)], spacing=T.S2, tight=True), width=T.px(1100))))
        show()

    # ---------------------------------------------------------------- actions
    async def _download_selected(self, e=None):
        if self.selected:
            await self.download(self._chosen())

    def _take(self, e=None) -> None:
        """Take screenshot: the button waits while the Frame saves it (a few seconds), then the grid reloads."""
        if self.take_btn.disabled:
            return
        self.take_btn.disabled = True
        C.update(self.take_btn)
        self.app.run_bg(self._take_bg)

    def _take_bg(self) -> None:
        from ...install import screenshots

        app = self.app
        try:
            r = screenshots.take(app.target.frame)
            if r.get("taken"):
                app.toast(tr("Screenshot taken"))
                self.load()
            elif r.get("reason") == "steamvr":
                app.toast(tr("SteamVR isn't running on the Frame: put the headset on, then take the screenshot."),
                          error=True)
            elif r.get("reason") == "capture" or r.get("hmd") == "Standby":
                app.toast(tr("The headset is asleep: put it on, then take the screenshot."), error=True)
            else:
                app.toast(tr("The Frame didn't save a screenshot ({why}). Try again.").format(
                    why=r.get("reason") or tr("no reason given")), error=True)
        except Exception as exc:  # noqa: BLE001
            app.toast(tr("Couldn't take a screenshot: {error}").format(error=explain(exc)), error=True)
        finally:
            self.take_btn.disabled = False
            C.update(self.take_btn)

    def copy(self, shot: dict) -> None:
        """The full-size screenshot onto this PC's clipboard, to paste it anywhere (no download needed)."""
        app = self.app

        def work():
            from ...core import clipboard
            from ...install import screenshots

            try:
                path = screenshots.image_path(app.target.frame, shot)
                if app.page.web:  # (a browser session: the browser's clipboard, through Flet)
                    app.page.run_task(self._copy_web, path)
                    return
                clipboard.copy_image(path)
            except Exception as exc:  # noqa: BLE001
                app.toast(tr("Couldn't copy the screenshot: {error}").format(error=explain(exc)), error=True)
                return
            app.toast(tr("Screenshot copied: paste it anywhere"))
        app.run_bg(work)

    async def _copy_web(self, path: Path) -> None:
        try:
            await ft.Clipboard().set_image(path.read_bytes())
        except Exception as exc:  # noqa: BLE001
            self.app.toast(tr("Couldn't copy the screenshot: {error}").format(error=explain(exc)), error=True)
            return
        self.app.toast(tr("Screenshot copied: paste it anywhere"))

    async def _download_all(self, e=None):
        if self.shots:
            await self.download(list(self.shots))

    async def download(self, shots: list[dict]) -> None:
        folder = await ft.FilePicker().get_directory_path(
            dialog_title=tr("Save the screenshots to which folder on this PC?"))
        if not folder:
            return
        app = self.app

        def run(job):
            from ...install import screenshots

            r = screenshots.download(app.target.frame, shots, Path(folder), job.reporter)
            return tr_n("Downloaded {n} screenshot to {folder}", "Downloaded {n} screenshots to {folder}",
                        r["files"], folder=r["folder"])
        app.submit(tr_n("Download {n} screenshot", "Download {n} screenshots", len(shots)), run, None,
                   kind="tool-frame", open_panel=True)

    def delete(self, shots: list[dict]) -> None:
        if not shots:
            return

        def go():
            def work():
                from ...install import screenshots

                try:
                    n = screenshots.delete(self.app.target.frame, shots)
                    self.app.toast(tr_n("Deleted {n} screenshot", "Deleted {n} screenshots", n))
                except Exception as exc:  # noqa: BLE001
                    self.app.toast(explain(exc), error=True)
                self.selected.difference_update(s["path"] for s in shots)
                self.load()
            self.app.run_bg(work)
        C.confirm(self.app.page, tr_n("Delete {n} screenshot?", "Delete {n} screenshots?", len(shots)),
                  tr("They are deleted on the Frame and can't be restored. Steam's own screenshot list may still "
                     "show them until Steam restarts."), tr("Delete"), go, danger=True)
