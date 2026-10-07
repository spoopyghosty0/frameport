"""Library: search, filters, tags and a grid of game cards with hover quick actions.

Performance: the view is created once and re-mounted; cards are built in a background thread and streamed in batches;
search and filters only toggle visibility / order of existing cards; images are small thumbnails by URL."""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import flet as ft

from ...artwork import thumbs
from ...core import library
from ...i18n import tr, tr_n
from .. import components as C
from .. import glyphs as G
from .. import theme as T
from ..help import HELP

if TYPE_CHECKING:
    from ..app import FramePortApp

DEFAULT_FILTERS = {"q": "", "where": "all", "platform": "all", "status": "all", "sort": "name", "tags": []}
STATUS_ORDER = {"works": 0, "issues": 1, "unknown": 2, "unsupported": 3}


def auto_tags(game: dict) -> list[str]:
    """Tags FramePort derives from the analysis (not stored): platform, engine, XR API, mixed reality."""
    a = game.get("analysis") or {}
    out = [C.platform(game)[0]]
    if a.get("engine") and a["engine"] not in ("Other", "?"):
        out.append(a["engine"])
    xr = a.get("xr") or ""
    out += [t for t in ("OpenXR", "VrApi", "LibOVR") if t in xr]  # API names (the same in every language)
    patches = (game.get("recipe") or {}).get("patches") or {}
    if "patch_force_passthrough" in patches or "adapter.scene_emul" in patches:
        out.append(tr("Mixed reality"))
    out += ((game.get("details") or {}).get("genres") or [])[:4]  # from the store
    return list(dict.fromkeys(out))  # (a Linux app's platform and "engine" are both "Linux")


def user_tags(game: dict) -> list[str]:
    return list(game.get("tags") or [])


def game_tags(game: dict) -> list[str]:
    seen, out = set(), []
    for t in user_tags(game) + auto_tags(game):
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def all_tags(games: list[dict]) -> list[str]:
    """Every tag in the library: the user's own first (alphabetical), then the automatic ones."""
    mine = sorted({t for g in games for t in user_tags(g)}, key=str.lower)
    auto = sorted({t for g in games for t in auto_tags(g)} - set(mine), key=str.lower)
    return mine + auto


def last_used(game: dict) -> float:
    """Latest play, install or launch test (seconds since epoch; 0 if never)."""
    times = [(game.get("last_test") or {}).get("time") or 0, game.get("last_played") or 0]
    times += [i.get("time") or 0 for i in (game.get("installs") or {}).values() if isinstance(i, dict)]
    return max(times)


def game_size(game: dict) -> int:
    if game.get("kind") == "rift":
        return ((game.get("analysis") or {}).get("extra") or {}).get("data_bytes") or 0
    return game.get("data_bytes") or 0


from ...core.titles import counterparts, display_title, twins  # noqa: E402,F401  (used by the views)


def normalize_tag(text: str) -> str:
    return " ".join((text or "").replace(",", " ").split())[:32]


# ------------------------------------------------------------------------------------------ pure helpers (tested)
def location(game: dict, frame_info: dict | None, pc_installs: set[str]) -> set[str]:
    st = C.install_state(game, frame_info)
    out = set()
    if st in ("installed", "outdated"):
        out.add("frame")
    if game["package"] in pc_installs:
        out.add("pc")
    return out


def platform_matches(game: dict, platform: str) -> bool:
    """The library's platform filter: all | quest (Android games and apps) | pcvr (PC VR and Windows) | linux."""
    kind = game.get("kind") or "quest"
    return platform == "all" or {"quest": kind not in ("rift", "linux"), "pcvr": kind == "rift",
                                 "linux": kind == "linux"}.get(platform, True)


def platform_filter_options(games: list[dict]) -> list[tuple[str, str]]:
    """The platform filter's choices: only when the library has more than Android games; one per kind present."""
    kinds = {g.get("kind") or "quest" for g in games}
    if not kinds & {"rift", "linux"}:
        return []
    out = [("all", tr("All")), ("quest", tr("Android"))]
    if "rift" in kinds:
        out.append(("pcvr", tr("PC VR")))
    if "linux" in kinds:
        out.append(("linux", tr("Linux")))
    return out


def filter_games(games: list[dict], f: dict, frame_info: dict | None = None,
                 pc_installs: set[str] | frozenset = frozenset()) -> list[dict]:
    q = (f.get("q") or "").strip().lower()
    out = []
    for g in games:
        tags = [t.lower() for t in game_tags(g)]
        if q and q not in f"{g.get('title') or ''} {g['package']} {' '.join(tags)}".lower():
            continue
        if any(t.lower() not in tags for t in f.get("tags") or []):
            continue
        if not platform_matches(g, f.get("platform", "all")):
            continue
        if f.get("status", "all") != "all" and (g.get("recipe") or {}).get("status", "unknown") != f["status"]:
            continue
        where = f.get("where", "all")
        loc = location(g, frame_info, set(pc_installs))
        if where == "frame" and "frame" not in loc or where == "pc" and "pc" not in loc or \
                where == "none" and loc:
            continue
        out.append(g)
    key = {
        "name": lambda g: (g.get("title") or g["package"]).lower(),
        "recent": lambda g: -(g.get("added") or 0),
        "played": lambda g: -last_used(g),
        "size": lambda g: -game_size(g),
        "status": lambda g: (STATUS_ORDER.get((g.get("recipe") or {}).get("status", "unknown"), 9),
                             (g.get("title") or g["package"]).lower()),
    }.get(f.get("sort", "name"))
    return sorted(out, key=key)


def load_filters() -> dict:
    saved = library.setting("ui.library") or {}
    return {**DEFAULT_FILTERS, "tags": [], **{k: v for k, v in saved.items() if k in DEFAULT_FILTERS and k != "q"}}


def save_filters(f: dict) -> None:
    library.set_setting("ui.library", {k: v for k, v in f.items() if k != "q"})


# ------------------------------------------------------------------------------------------ view
BATCH = 8
CARD_ART = ("portrait", "square", "cover", "icon")  # store art first; cover = FramePort's own (no store art)
def card_shadow(hover: bool = False) -> ft.BoxShadow:
    """Library cards float on the dark background; hovering lifts them further."""
    if hover:
        return ft.BoxShadow(blur_radius=36, spread_radius=2, color=T.soft("#000000", 0.75), offset=ft.Offset(0, 14))
    return ft.BoxShadow(blur_radius=22, spread_radius=1, color=T.soft("#000000", 0.55), offset=ft.Offset(0, 8))


def quick_icon(kind: str) -> str:
    """The symbol on a card's round quick button (app.quick_action's kind)."""
    return {"play": ft.Icons.PLAY_ARROW_ROUNDED, "update": ft.Icons.UPGRADE_ROUNDED,
            "reinstall": ft.Icons.REFRESH_ROUNDED}.get(kind, ft.Icons.DOWNLOAD_ROUNDED)


class LibraryView:
    """Created once per app; `mount()` returns the same control tree, `reload()` refreshes it in the background."""

    def __init__(self, app: FramePortApp):
        self.app = app
        self.f = app.lib_filters
        self.cards: dict[str, tuple[tuple, ft.Control]] = {}  # package -> (state key, card)
        self.checks: dict[str, ft.Control] = {}  # package -> selection checkbox overlay
        self.selected: set[str] = set()
        self.select_mode = False
        self.sel_bar = ft.Container(visible=False)
        self.resume_bar = ft.Container(visible=False)
        self.update_bar = ft.Container(visible=False)  # "FramePort x.y is available" (ui/updater.py)
        self.games: list[dict] = []
        self._lock = threading.Lock()
        self._search_timer: threading.Timer | None = None
        self._gen = 0
        self.grid = ft.GridView(expand=True, max_extent=T.px(196), child_aspect_ratio=0.62, spacing=T.S4,
                                run_spacing=T.S4, padding=ft.Padding(0, T.S2, T.S2, T.S5))
        self.count = C.meta("")
        self.subtitle = C.body("", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self.search = ft.TextField(
            value=self.f["q"], hint_text=tr("Search games and tags"), prefix_icon=ft.Icons.SEARCH_ROUNDED, dense=True,
            width=T.px(260), border_radius=T.RADIUS_SM, bgcolor=T.SURFACE_3, border_color=ft.Colors.TRANSPARENT,
            focused_border_color=T.ACCENT, content_padding=ft.Padding(T.px(12), T.px(8), T.px(12), T.px(8)),
            text_size=T.T_BODY,
            on_change=self._on_search)
        # an X that clears the search at once (shown only while there is text)
        # a small clickable icon, not an IconButton: its 40 px minimum size made the field taller and pushed the
        # text off-centre
        self.search.suffix = ft.Container(ft.Icon(ft.Icons.CLOSE_ROUNDED, size=T.px(16), color=T.TEXT_2),
                                          tooltip=tr("Clear search"), on_click=lambda e: self.clear_search(),
                                          visible=bool(self.f["q"]), border_radius=T.px(10), ink=True,
                                          padding=T.px(2))
        app.search_field = self.search
        self.filters = ft.Container()
        self.hint = ft.Container(visible=False)
        # one right-click menu for every card: filled with that game's actions when it opens (open_menu)
        # click and drag across cards to select them (turns on select mode; C.DragSelect)
        self.drag = C.DragSelect(lambda pkg: pkg in self.selected,
                                 lambda pkg, on: self.toggle_selected(pkg) if (pkg in self.selected) != on else None,
                                 can_select=lambda pkg: pkg in self.cards,
                                 on_start=lambda: None if self.select_mode else self.set_select_mode(True))
        self.menu = ft.ContextMenu(content=ft.GestureDetector(content=self.grid, expand=True,
                                                              on_pan_start=self.drag.start, on_pan_end=self.drag.end),
                                   secondary_trigger=None, tertiary_trigger=None, expand=True)
        self.body = ft.Container(self.menu, expand=True)
        add = ft.PopupMenuButton(
            content=ft.Container(ft.Row([ft.Icon(ft.Icons.ADD_ROUNDED, color=T.ON_ACCENT, size=T.px(18)),
                                         ft.Text(tr("Add games"), color=T.ON_ACCENT, weight=ft.FontWeight.W_600,
                                                 size=T.px(13))],
                                        spacing=T.px(6), tight=True),
                                 bgcolor=T.ACCENT, border_radius=T.RADIUS_SM,
                                 padding=ft.Padding(T.px(14), T.px(9), T.px(16), T.px(9))),
            items=[ft.PopupMenuItem(content=ft.Text(tr("Scan a folder…")), icon=ft.Icons.FOLDER_OPEN_ROUNDED,
                                    on_click=app.pick_folder),
                   ft.PopupMenuItem(content=ft.Text(tr("Add one game folder…")),
                                    icon=ft.Icons.CREATE_NEW_FOLDER_ROUNDED, on_click=app.pick_game_folder),
                   ft.PopupMenuItem(content=ft.Text(tr("Add an APK file…")), icon=ft.Icons.ANDROID_ROUNDED,
                                    on_click=app.pick_apk),
                   ft.PopupMenuItem(content=ft.Text(tr("Add a Windows program (.exe)…")),
                                    icon=ft.Icons.DESKTOP_WINDOWS_ROUNDED, on_click=app.pick_windows_exe),
                   ft.PopupMenuItem(content=ft.Text(tr("Install from a link…")), icon=ft.Icons.LINK_ROUNDED,
                                    on_click=app.pick_link),
                   ft.PopupMenuItem(),  # divider: native Linux apps (GitHub #31)
                   ft.PopupMenuItem(content=ft.Text(tr("Add a Linux app…")), icon=ft.Icons.TERMINAL_ROUNDED,
                                    on_click=app.pick_linux_app),
                   ft.PopupMenuItem(content=ft.Text(tr("Add a Linux app folder…")), icon=ft.Icons.FOLDER_ROUNDED,
                                    on_click=app.pick_linux_folder)],
            bgcolor=T.SURFACE_2, tooltip="")
        self.rescan_btn = C.secondary(tr("Rescan folders"), ft.Icons.REFRESH_ROUNDED, app.rescan,
                                      tooltip=C.tip(HELP["rescan"]))
        self.update_all_btn = C.secondary(tr("Update all"), ft.Icons.SYSTEM_UPDATE_ALT_ROUNDED,
                                          lambda e: app.update_all(), tooltip=C.tip(HELP["update_all"]))
        self.update_all_btn.visible = False
        self.select_btn = C.secondary(tr("Select"), ft.Icons.CHECKLIST_ROUNDED, lambda e: self.set_select_mode(True),
                                      tooltip=C.tip(HELP["select"]))
        self.root = ft.Column([
            # the actions wrap (right-aligned) in a narrow window instead of squeezing the heading away
            ft.Row([ft.Column([ft.Text(tr("Library"), size=T.T_TITLE, weight=ft.FontWeight.W_700, color=T.TEXT,
                                       no_wrap=True), self.subtitle], spacing=T.px(2), expand=1),
                    ft.Container(ft.Row([self.search, self.update_all_btn, self.rescan_btn, self.select_btn, add],
                                        spacing=T.S3, run_spacing=T.S2, wrap=True,
                                        alignment=ft.MainAxisAlignment.END,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER), expand=3)],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=T.S3),
            self.update_bar, self.resume_bar, self.hint, self.filters, self._drop_area(self.body), self.sel_bar,
        ], expand=True, spacing=T.S4)
        # skeleton cards until the first batch arrives
        n = min(len(library.games()), 15)
        self.grid.controls = [ft.Container(bgcolor=T.SURFACE, border_radius=T.RADIUS, border=ft.Border.all(1, T.BORDER),
                                           opacity=0.6) for _ in range(n)]

    def _drop_area(self, content: ft.Control) -> ft.Control:
        """APKs, Linux builds, Windows programs, folders and FrameDrop manifests dragged in from the file manager are
        added (packaged app only: flet-dropzone, see files.dropzone_available)."""
        from .files import dropzone_available

        if not dropzone_available():
            return content
        import flet_dropzone as ftd

        def dropped(e):
            paths = [f.path for f in e.files if f.path and not f.path.startswith("blob:")]
            if paths:
                self.app.add_dropped(paths)
        return ftd.Dropzone(content=content, expand=True, on_dropped=dropped)

    # ---------------------------------------------------------------- public
    def mount(self) -> ft.Control:
        self._update_hint()
        self.update_resume_bar()
        self.update_update_bar()
        self._update_sel_bar()
        return self.root

    def reload(self) -> None:
        """Rebuild changed cards (streamed in batches), then apply filters. Call from a background thread."""
        from ...targets.pc_revive import local_installs

        self._gen += 1  # before waiting for the lock: an older reload still running sees it and stops early
        gen = self._gen
        with self._lock:
            if gen != self._gen:
                return  # an even newer reload is queued behind this one
            games = library.games()
            self.games = games
            pc = set(local_installs())
            tw = twins(games)
            self._update_header(games, pc)
            self.update_update_bar()
            self.update_resume_bar()  # also after Dismiss / Resume and when installs finish or fail
            if not games:
                self.body.content = C.empty_state(
                    ft.Icons.LIBRARY_ADD_ROUNDED, tr("Add your games"),
                    tr("Point FramePort at a folder with Android games (APK + OBB, e.g. Quest games) or PC VR games "
                       "(one folder per game, or a folder of them). It finds them, works out what each needs and "
                       "fetches artwork."),
                    C.primary(tr("Scan a folder"), ft.Icons.FOLDER_OPEN_ROUNDED, self.app.pick_folder, big=True),
                    C.secondary(tr("Add an APK file"), ft.Icons.ANDROID_ROUNDED, self.app.pick_apk))
                self.cards.clear()
                C.update(self.root)
                return
            if self.body.content is not self.menu:
                self.body.content = self.menu
            first = not self.cards
            if first:
                C.update(self.root)
            pending = []
            for g in games:
                key = self._key(g, pc, tw)
                old = self.cards.get(g["package"])
                if old and old[0] == key:
                    continue
                pending.append((g, key))
            for i in range(0, len(pending), BATCH):
                if gen != self._gen:
                    return  # a newer reload took over
                for g, key in pending[i:i + BATCH]:
                    self.cards[g["package"]] = (key, self.card(g, pc, tw))
                if first or i + BATCH >= len(pending):
                    self._apply(update=True)
            gone = set(self.cards) - {g["package"] for g in games}
            for pkg in gone:
                self.cards.pop(pkg, None)
            if gone or not pending:
                self._apply(update=True)

    # ---------------------------------------------------------------- selection (queue several installs)
    def set_select_mode(self, on: bool) -> None:
        self.select_mode = on
        if not on:
            self.selected.clear()
        for pkg, chk in self.checks.items():
            chk.visible = on
            chk.content.value = pkg in self.selected
        self._update_sel_bar()
        C.update(self.grid, self.sel_bar, self.select_btn)

    def toggle_selected(self, pkg: str) -> None:
        self.selected.symmetric_difference_update({pkg})
        chk = self.checks.get(pkg)
        if chk:
            chk.content.value = pkg in self.selected
            C.update(chk)
        self._update_sel_bar()

    def select_visible(self) -> None:
        shown = filter_games(self.games, self.f, self.app.frame_info, set(self.app.pc_installs()))
        self.selected |= {g["package"] for g in shown if g["package"] in self.cards}
        self.set_select_mode(True)

    def _update_sel_bar(self) -> None:
        app = self.app
        n = len(self.selected)
        self.select_btn.visible = not self.select_mode
        rift = [p for p in self.selected if p.startswith("rift.")]
        from ...core import winhost

        self.sel_bar.visible = self.select_mode
        self.sel_bar.content = ft.Container(ft.Row([
            ft.Icon(ft.Icons.CHECKLIST_ROUNDED, color=T.ACCENT),
            C.body(tr("{n} selected").format(n=n) if n else tr("Select games to install"), T.TEXT,
                   weight=ft.FontWeight.W_600),
            ft.Container(expand=True),
            C.ghost(tr("Select all shown"), on_click=lambda e: self.select_visible()),
            C.ghost(tr("Clear"), on_click=lambda e: (self.selected.clear(), self.set_select_mode(True))),
            *([C.secondary(tr("Install {len} on this PC").format(len=len(rift)), G.PC,
                           lambda e: app.install_many(sorted(rift), "pc"), disabled=not winhost.available())]
              if rift else []),
            C.primary(tr("Install {n} on Frame").format(n=n) if n else tr("Install on Frame"),
                      G.FRAME,
                      lambda e: app.install_many(sorted(self.selected), "frame"),
                      disabled=not n or app.frame_state != "connected",
                      tooltip=None if app.frame_state == "connected" else tr("Connect your Frame first")),
            C.ghost(tr("Done"), on_click=lambda e: self.set_select_mode(False)),
        ], spacing=T.S2), bgcolor=T.SURFACE_2, border_radius=T.RADIUS, padding=ft.Padding(T.S4, T.S2, T.S2, T.S2),
            border=ft.Border.all(1, T.ACCENT))
        C.update(self.sel_bar, self.select_btn)

    def update_update_bar(self) -> None:
        from ..updater import library_bar

        bar = library_bar(self.app)
        self.update_bar.visible = bar is not None
        self.update_bar.content = bar
        C.update(self.update_bar)

    def update_resume_bar(self) -> None:
        """Installs that didn't finish (cancelled, failed, or the app closed): resume them from here."""
        app = self.app
        pending = app.unfinished_installs()
        self.resume_bar.visible = bool(pending)
        if pending:
            names = ", ".join(app._title(p) for p in list(pending)[:3]) + ("…" if len(pending) > 3 else "")
            self.resume_bar.content = C.callout(ft.Row([
                C.body(tr_n("{n} install didn't finish: {names}. What was already copied is kept, so resuming "
                            "continues where it stopped.",
                            "{n} installs didn't finish: {names}. What was already copied is kept, so resuming "
                            "continues where it stopped.",
                            len(pending), names=names), T.TEXT, expand=True),
                C.primary(tr("Resume"), ft.Icons.PLAY_ARROW_ROUNDED, lambda e: app.resume_installs()),
                C.ghost(tr("Dismiss"), on_click=lambda e: app.forget_installs()),
            ], spacing=T.S3), "warn", ft.Icons.PAUSE_CIRCLE_OUTLINE_ROUNDED)
        C.update(self.resume_bar)

    def refresh_async(self) -> None:
        self.app.page.run_thread(self.reload)

    # ---------------------------------------------------------------- filtering (no rebuilds)
    def clear_search(self) -> None:
        self.search.value = self.f["q"] = ""
        self.search.suffix.visible = False
        C.update(self.search)
        self._apply(update=True)

    def _on_search(self, e):
        self.f["q"] = e.control.value
        if self.search.suffix.visible != bool(self.f["q"]):
            self.search.suffix.visible = bool(self.f["q"])
            C.update(self.search)
        if self._search_timer:
            self._search_timer.cancel()
        self._search_timer = threading.Timer(0.18, lambda: self._apply(update=True))
        self._search_timer.daemon = True
        self._search_timer.start()

    def _set(self, key, value):
        self.f[key] = value
        save_filters(self.f)
        self._update_header(self.games, set(self.app.pc_installs()))
        self._apply(update=True)
        C.update(self.filters)

    def _apply(self, update: bool = False) -> None:
        pc = set(self.app.pc_installs())
        shown = filter_games(self.games, self.f, self.app.frame_info, pc)
        # only the matching cards, in order (hidden cards in between confused the grid's item matching)
        self.grid.controls = [self.cards[g["package"]][1] for g in shown if g["package"] in self.cards]
        n = len(self.games)
        self.count.value = tr("Showing {len} of {n}").format(len=len(shown), n=n) if len(shown) != n else ""
        if update:
            C.update(self.grid, self.count)

    # ---------------------------------------------------------------- header / filter bar
    def _update_hint(self):
        app = self.app
        if app.frame_state != "connected":
            self.hint.content = C.callout(ft.Row([
                C.body(tr("Connect your Steam Frame to install games and see what's on it."), T.TEXT, expand=True),
                C.ghost(tr("Connect"), ft.Icons.ARROW_FORWARD_ROUNDED, lambda e: app.go("frame"), color=T.ACCENT)]),
                "info", G.FRAME)
            self.hint.visible = True
        else:
            self.hint.visible = False

    def _update_header(self, games: list[dict], pc: set[str]) -> None:
        n = len(games)
        on = sum(1 for g in games if C.install_state(g, self.app.frame_info) in ("installed", "outdated"))
        n_pc = len(pc & {g["package"] for g in games})
        parts = [tr_n("{n} game", "{n} games", n)]
        if self.app.frame_info is not None:
            parts.append(tr("{on} on your Frame").format(on=on))
        if n_pc:
            parts.append(tr("{n_pc} on this PC").format(n_pc=n_pc))
        self.subtitle.value = " · ".join(parts)
        updates = len(self.app.updatable())
        self.update_all_btn.visible = updates > 0
        self.update_all_btn.content = (tr("Update all ({updates})").format(updates=updates) if updates
                                       else tr("Update all"))
        C.update(self.update_all_btn)
        self._update_hint()
        has_rift = any(g.get("kind") == "rift" for g in games)
        platforms = platform_filter_options(games)
        where = [("all", tr("All")), ("frame", tr("On Frame")), ("none", tr("Not installed"))]
        if has_rift:
            where.insert(2, ("pc", tr("On this PC")))
        # the filters wrap onto a second line in a narrow window; count and sort stay on the right
        left = ft.Row([
            self._seg("where", where),
            *([self._seg("platform", platforms)] if platforms else []),
            C.with_help(self._menu_chip("status", tr("Status"), [("all", tr("Any")), ("works", tr("Works")),
                                                                 ("issues", tr("Works with issues")),
                                                                 ("unknown", tr("Untested")),
                                                                 ("unsupported", tr("Can't run"))]), "status"),
            self._tag_menu(games),
        ], spacing=T.S2, run_spacing=T.S2, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self.filters.content = ft.Row([
            ft.Container(left, expand=True),
            self.count,
            self._menu_chip("sort", tr("Sort"), [("name", tr("Name")), ("recent", tr("Recently added")),
                                                 ("played", tr("Recently used")),
                                                 ("status", tr("Status")), ("size", tr("Size"))]),
        ], spacing=T.S2, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self.filters.visible = bool(games)
        C.update(self.subtitle, self.hint, self.filters)

    def _seg(self, key: str, options: list[tuple[str, str]]) -> ft.Control:
        items = []
        for value, label in options:
            on = self.f.get(key) == value
            items.append(ft.Container(
                ft.Text(label, size=T.T_META, weight=ft.FontWeight.W_600, color=T.TEXT if on else T.TEXT_2),
                padding=ft.Padding(T.px(12), T.px(6), T.px(12), T.px(6)), border_radius=T.px(20),
                bgcolor=T.SURFACE_3 if on else None,
                on_click=lambda e, v=value: self._set(key, v), ink=True))
        return ft.Container(ft.Row(items, spacing=T.px(2), tight=True), padding=T.px(3), border_radius=T.px(22),
                            border=ft.Border.all(1, T.BORDER))

    def _menu_chip(self, key: str, label: str, options: list[tuple[str, str]]) -> ft.Control:
        current = dict(options).get(self.f.get(key), options[0][1])
        return ft.PopupMenuButton(
            content=ft.Container(ft.Row([C.meta(label + ":"), C.body(current, T.TEXT, size=T.T_META),
                                         ft.Icon(ft.Icons.EXPAND_MORE_ROUNDED, size=T.px(16), color=T.TEXT_2)],
                                        spacing=T.px(4), tight=True),
                                 padding=ft.Padding(T.px(12), T.px(7), T.px(8), T.px(7)), border_radius=T.px(20),
                                 border=ft.Border.all(1, T.BORDER)),
            items=[ft.PopupMenuItem(content=ft.Text(text), checked=self.f.get(key) == value,
                                    on_click=lambda e, v=value: self._set(key, v)) for value, text in options],
            bgcolor=T.SURFACE_2, tooltip="")

    def _toggle_tag(self, tag: str):
        tags = list(self.f.get("tags") or [])
        tags.remove(tag) if tag in tags else tags.append(tag)
        self._set("tags", tags)

    def _tag_menu(self, games: list[dict]) -> ft.Control:
        chosen = self.f.get("tags") or []
        label = ", ".join(chosen) if chosen else tr("Any")
        items = [ft.PopupMenuItem(content=ft.Text(t), checked=t in chosen, on_click=lambda e, t=t: self._toggle_tag(t))
                 for t in all_tags(games)]
        if chosen:
            items.append(ft.PopupMenuItem(content=ft.Text(tr("Clear tags")), icon=ft.Icons.CLEAR_ROUNDED,
                                          on_click=lambda e: self._set("tags", [])))
        return ft.PopupMenuButton(
            content=ft.Container(ft.Row([ft.Icon(ft.Icons.SELL_OUTLINED, size=T.px(14), color=T.TEXT_2),
                                         C.meta(tr("Tags:")),
                                         C.body(label if len(label) < 28
                                                else tr("{len} selected").format(len=len(chosen)), T.TEXT,
                                                size=T.T_META),
                                         ft.Icon(ft.Icons.EXPAND_MORE_ROUNDED, size=T.px(16), color=T.TEXT_2)],
                                        spacing=T.px(4), tight=True),
                                 padding=ft.Padding(T.px(12), T.px(7), T.px(8), T.px(7)), border_radius=T.px(20),
                                 border=ft.Border.all(1, T.ACCENT if chosen else T.BORDER)),
            items=items, bgcolor=T.SURFACE_2, tooltip=C.tip(HELP["tags"]))

    # ---------------------------------------------------------------- cards
    def _key(self, g: dict, pc: set[str], tw: set[str]) -> tuple:
        pkg = g["package"]
        job = self.app.jobs.busy_with(pkg)
        return (display_title(g, tw), (g.get("recipe") or {}).get("status"), C.install_state(g, self.app.frame_info),
                pkg in pc, bool(job), self.app.quick_action(g)[0], thumbs.url(pkg, CARD_ART),
                self.app.frame_state)

    def card(self, g: dict, pc: set[str], tw: set[str]) -> ft.Control:
        app = self.app
        pkg = g["package"]
        rift = g.get("kind") == "rift"
        art = thumbs.url(pkg, CARD_ART)
        state = C.install_state(g, app.frame_info)
        on_pc = pkg in pc
        status = (g.get("recipe") or {}).get("status", "unknown")
        s_label, s_color = C.STATUS_STYLE.get(status, C.STATUS_STYLE["unknown"])
        job = app.jobs.busy_with(pkg)
        badges = []
        if job:
            badges.append(C.pill(tr("Working…"), T.ACCENT, ft.Icons.SYNC_ROUNDED, solid=True))
        elif state in ("installed", "outdated"):
            badges.append(C.install_badge(state))
        if on_pc:
            badges.append(C.install_badge("on_pc"))
        if rift and g.get("exe_confirmed") is False:
            badges.append(C.pill(tr("Check exe"), T.WARN, ft.Icons.HELP_OUTLINE_ROUNDED, overlay=True,
                                 tooltip=C.tip(HELP["check_exe"])))
        label, _, help_key = C.platform(g)
        platform = C.pill(label, T.PC if rift else T.TEXT, C.platform_icon(g), overlay=True,
                          tooltip=C.tip(HELP[help_key]))
        check = ft.Container(ft.Checkbox(value=pkg in self.selected, active_color=T.ACCENT, check_color=T.ON_ACCENT,
                                         on_change=lambda e: self.toggle_selected(pkg)),
                             bgcolor=T.soft("#000000", 0.6), border_radius=T.px(8), left=T.px(6), top=T.px(40),
                             visible=self.select_mode)
        self.checks[pkg] = check
        quick_label, quick_kind = app.quick_action(g)
        circle = ft.Container(
            ft.Icon(quick_icon(quick_kind), size=T.px(56), color=T.ON_ACCENT),
            width=T.px(96), height=T.px(96), border_radius=T.px(48), bgcolor=T.ACCENT, alignment=ft.Alignment.CENTER,
            shadow=ft.BoxShadow(blur_radius=28, spread_radius=2, color=T.soft("#000000", 0.6), offset=ft.Offset(0, 6)),
            tooltip=ft.Tooltip(message=quick_label, wait_duration=800), ink=True,
            on_click=lambda e: app.primary_action(pkg),
            scale=0.85, animate_scale=ft.Animation(160, ft.AnimationCurve.EASE_OUT)) if quick_label else None
        # one big round Play / Install button in the middle of the cover, shown on hover
        quick = ft.Container(circle, left=0, right=0, top=0, bottom=T.px(56), alignment=ft.Alignment.CENTER,
                             opacity=0, animate_opacity=ft.Animation(160, ft.AnimationCurve.EASE_OUT)) \
            if circle else None
        dim = state == "missing" and not on_pc
        tile = ft.Container(
            ft.Stack([
                C.art_fill(art, left=0, right=0, top=0, bottom=0, opacity=0.5 if dim else 1.0,
                           placeholder_icon=C.platform_icon(g)),
                ft.Container(C.bottom_fade(None, 0.92), left=0, right=0, bottom=0, top=T.px(90)),
                # platform + state badges in one row that wraps: on a narrow card "On Frame" covered "Android"
                ft.Container(ft.Row([platform, *badges], spacing=T.px(4), run_spacing=T.px(4), wrap=True),
                             left=T.px(10), right=T.px(10), top=T.px(10)),
                *([quick] if quick else []),
                check,
                ft.Container(ft.Column([
                    ft.Text(display_title(g, tw), size=T.px(14), weight=ft.FontWeight.W_700, color=T.TEXT, max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Container(ft.Row([C.dot(s_color, 7), C.meta(s_label, T.TEXT_2)], spacing=T.px(6), tight=True),
                                 tooltip=C.tip(HELP["status"])),
                ], spacing=T.px(4)), left=T.px(12), right=T.px(12), bottom=T.px(12)),
            ], expand=True),
            border_radius=T.RADIUS, bgcolor=T.SURFACE, border=ft.Border.all(1, T.BORDER), expand=True,
            scale=1.0, animate_scale=ft.Animation(140, ft.AnimationCurve.EASE_OUT),
            shadow=card_shadow(),
            tooltip=ft.Tooltip(message=tr("Click to open · right-click for quick actions · drag across cards to "
                                          "select several"), wait_duration=1500),
            on_click=lambda e: self.toggle_selected(pkg) if self.select_mode else app.open_game(pkg))

        def hover(e):
            on = e.data in (True, "true")
            self.drag.hover(pkg, on)
            tile.scale = 1.03 if on else 1.0
            tile.border = ft.Border.all(1, T.ACCENT if on else T.BORDER)
            tile.shadow = card_shadow(on)
            if quick:
                quick.opacity = 1 if on and not self.select_mode else 0
                circle.scale = 1.0 if on else 0.85
            tile.update()
        tile.on_hover = hover
        # no key=: Flet freezes keyed controls, and cards change in place (hover, state); the grid only ever holds
        # the matching cards in order (see _apply), which is what fixed search and sorting
        return ft.GestureDetector(content=tile, expand=True,
                                  on_secondary_tap_down=lambda e: self.open_menu(pkg, e.global_position))

    def open_menu(self, pkg: str, position=None) -> None:
        """Right-click on a card: that game's quick actions (built now, so they match the game's current state)."""
        self.menu.items = C.menu_items(self.app.game_actions(pkg))
        C.update(self.menu)
        self.app.page.run_task(self.menu.open, global_position=position)
