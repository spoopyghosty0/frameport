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


# type to search: Flet sends Flutter's key labels ("A" for a and A, "1", " ", "-", "Arrow Left", "Numpad 1", "F5"),
# not the typed character, so shifted symbols follow the US layout (what the label alone can tell)
_SHIFTED = dict(zip("1234567890-=[];'`\\,./", "!@#$%^&*()_+{}:\"~|<>?", strict=True))
_NAMED_CHARS = {"space": " ", **{f"numpad{d}": d for d in "0123456789"}, "numpadadd": "+",
                "numpadsubtract": "-", "numpadmultiply": "*", "numpaddivide": "/", "numpaddecimal": "."}


def typed_char(key: str, shift: bool = False, ctrl: bool = False, alt: bool = False,
               meta: bool = False) -> str | None:
    """The character a key press types (for the Library's type-to-search), None for shortcuts (Ctrl/Alt/Meta held),
    modifiers, navigation, function and other named keys."""
    if not key or ctrl or alt or meta:
        return None
    if len(key) == 1:
        if key.isalpha():
            return key.upper() if shift else key.lower()
        if shift and key in _SHIFTED:
            return _SHIFTED[key]
        return key if key.isprintable() else None
    return _NAMED_CHARS.get(key.lower().replace(" ", "").replace("_", ""))


def should_capture(route: str, dialog_open: bool, field_focused: bool, char: str | None, query: str = "") -> bool:
    """Whether a typed character goes to the Library's search: only on the Library itself, with no dialog open and
    the search not already focused (it types there itself); a leading space is ignored (Space on a focused button)."""
    return (route == "library" and not dialog_open and not field_focused and char is not None
            and not (char == " " and not query))


SHELF_MAX = 8


def filters_active(f: dict) -> bool:
    """A search or any filter other than the defaults (the sort order isn't a filter)."""
    return bool((f.get("q") or "").strip() or f.get("tags")) or any(
        f.get(k, DEFAULT_FILTERS[k]) != DEFAULT_FILTERS[k] for k in ("where", "platform", "status"))


def shelf_divider() -> ft.Control:
    """The line between the "On your Frame" shelf and the grid of every game: a label and a 2 px rule (the portal's
    blue→orange in dual themes), so the shelf doesn't read as the grid's first row. Part of the shelf: it hides
    with it."""
    rule = ft.Container(height=T.px(2), expand=True, border_radius=T.px(1),
                        gradient=C.portal_gradient(opacity=0.55) if T.DUAL else None,
                        bgcolor=None if T.DUAL else T.BORDER_STRONG)
    label = ft.Text(tr("All games").upper(), size=T.T_SMALL, weight=ft.FontWeight.W_600, color=T.TEXT_2,
                    style=ft.TextStyle(letter_spacing=1.2))
    return ft.Container(ft.Row([label, rule], spacing=T.S3, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        padding=ft.Padding(0, T.S2, T.S2, 0))


def shelf_games(games: list[dict], f: dict, frame_info: dict | None, connected: bool, select_mode: bool = False,
                limit: int = SHELF_MAX) -> list[dict]:
    """The "On your Frame" shelf: games installed on the connected Frame (outdated ones too), most recently used
    first, at most `limit`. Empty (the shelf hides) without a connected Frame and while searching or filtering. It
    stays in select mode (`select_mode` is ignored): hiding it when a drag-select starts moved the grid up under the
    mouse, and the drag selected the wrong cards."""
    if not connected or filters_active(f):
        return []
    on = [g for g in games if C.install_state(g, frame_info) in ("installed", "outdated")]
    on.sort(key=lambda g: (-last_used(g), (g.get("title") or g["package"]).lower()))
    return on[:limit]


def load_filters() -> dict:
    saved = library.setting("ui.library") or {}
    return {**DEFAULT_FILTERS, "tags": [], **{k: v for k, v in saved.items() if k in DEFAULT_FILTERS and k != "q"}}


def save_filters(f: dict) -> None:
    library.set_setting("ui.library", {k: v for k, v in f.items() if k != "q"})


# ------------------------------------------------------------------------------------------ view
BATCH = 8
CARD_ART = ("portrait", "square", "cover", "icon")  # store art first; cover = FramePort's own (no store art)
SHELF_ART = ("landscape", "hero", "banner", "portrait", "square", "cover", "icon")  # wide first
def card_shadow(hover: bool = False) -> ft.BoxShadow | list[ft.BoxShadow]:
    """Library cards float on the dark background; hovering lifts them further, with a faint accent glow."""
    if hover:
        return [ft.BoxShadow(blur_radius=36, spread_radius=2, color=T.soft("#000000", 0.75), offset=ft.Offset(0, 16)),
                ft.BoxShadow(blur_radius=30, spread_radius=-2, color=T.soft(T.ACCENT, 0.22), offset=ft.Offset(0, 4))]
    return ft.BoxShadow(blur_radius=22, spread_radius=1, color=T.soft("#000000", 0.55), offset=ft.Offset(0, 8))


def card_hover(tile: ft.Container, art: ft.Control, scrim: ft.Control | None, on: bool, reduce: bool) -> None:
    """A cover card under the pointer: lifts, its art zooms in a little inside the frame and darkens behind the round
    button (scrim), the shadow deepens. Reduce motion: only the shadow and scrim change."""
    if not reduce:
        tile.scale = 1.02 if on else 1.0
        tile.offset = ft.Offset(0, -0.015 if on else 0)
        art.scale = 1.07 if on else 1.0
    if scrim is not None:
        scrim.opacity = 1 if on else 0
    tile.shadow = card_shadow(on)


def hover_motion(tile: ft.Container, art: ft.Control) -> None:
    """The animations card_hover relies on (set once when the card is built)."""
    tile.scale, tile.offset = 1.0, ft.Offset(0, 0)
    tile.animate_scale = ft.Animation(220, ft.AnimationCurve.EASE_OUT)
    tile.animate_offset = ft.Animation(220, ft.AnimationCurve.EASE_OUT)
    tile.clip_behavior = ft.ClipBehavior.ANTI_ALIAS  # the zoomed art stays inside the rounded frame
    art.scale = 1.0
    art.animate_scale = ft.Animation(600, ft.AnimationCurve.EASE_OUT)


def art_frame(cover: ft.Control) -> ft.Container:
    """The cover in its own rounded clip, filling the card: the hover zoom (card_hover) stays inside the frame. The
    card's own clip didn't hold the scaled art, and a strip of it showed below the bottom fade."""
    return ft.Container(cover, left=0, right=0, top=0, bottom=0, border_radius=T.RADIUS,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS)


def scrim() -> ft.Container:
    """The darkening behind a card's round button while hovered (a soft radial dim, strongest in the middle)."""
    return ft.Container(left=0, right=0, top=0, bottom=0, opacity=0,
                        animate_opacity=ft.Animation(220, ft.AnimationCurve.EASE_OUT),
                        gradient=ft.RadialGradient(colors=[T.soft("#000000", 0.5), T.soft("#000000", 0.15)],
                                                   radius=0.8))


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
        self.marks: dict[str, tuple] = {}  # package -> (card tile, selection fade overlay, hovered?)
        self.selected: set[str] = set()
        self.select_mode = False
        self.sel_bar = ft.Container(visible=False)
        self.resume_bar = ft.Container(visible=False)
        self.update_bar = ft.Container(visible=False)  # "FramePort x.y is available" (ui/updater.py)
        self.games: list[dict] = []
        self.tw: set[str] = set()
        # "On your Frame": a row of wide cards above the grid (shelf_games); cards are kept per game and only rebuilt
        # when what they show changes
        self.shelf_cards: dict[str, tuple[tuple, ft.Control]] = {}
        self.shelf_row = ft.Row(spacing=T.S4, scroll=ft.ScrollMode.AUTO)
        self.shelf = ft.Container(ft.Column([
            C.h2(tr("On your Frame")),
            ft.Container(self.shelf_row, padding=ft.Padding(0, 0, 0, T.S2)),
            shelf_divider(),
        ], spacing=T.S3, tight=True), visible=False)
        self._lock = threading.Lock()
        self._search_timer: threading.Timer | None = None
        self._gen = 0
        self.grid = ft.GridView(expand=True, max_extent=T.px(196), child_aspect_ratio=0.62, spacing=T.S4,
                                run_spacing=T.S4, padding=ft.Padding(0, T.S2, T.S2, T.S5))
        self.count = C.meta("")
        self.subtitle = C.body("", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        # typing anywhere on the Library goes here (app._on_key); search_focused tracks where keys already land
        self.search_focused = False
        self.search = C.search(value=self.f["q"], hint_text=tr("Type to search games and tags"), width=T.px(260),
                               on_change=self._on_search, on_focus=lambda e: self._focus(True),
                               on_blur=lambda e: self._focus(False))
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
        self.menu = ft.ContextMenu(content=ft.Column([
            self.shelf,
            ft.GestureDetector(content=self.grid, expand=True, on_pan_start=self.drag.start, on_pan_end=self.drag.end),
        ], spacing=T.S4, expand=True), secondary_trigger=None, tertiary_trigger=None, expand=True)
        self.body = ft.Container(self.menu, expand=True)
        add = C.primary_menu(tr("Add games"), ft.Icons.ADD_ROUNDED, C.menu_items([
            (tr("Scan a folder…"), ft.Icons.FOLDER_OPEN_ROUNDED, app.pick_folder),
            (tr("Add a PC game folder…"), G.PC, app.pick_game_folder),
            (tr("Add an APK…"), ft.Icons.ANDROID_ROUNDED, app.pick_apk),
            (tr("Add a Windows program…"), ft.Icons.WEB_ASSET_ROUNDED, app.pick_windows_exe),
            (tr("Add from a link…"), ft.Icons.LINK_ROUNDED, app.pick_link),
            None,  # native Linux apps (GitHub #31)
            (tr("Add a Linux app…"), ft.Icons.TERMINAL_ROUNDED, app.pick_linux_app),
            (tr("Add a Linux app folder…"), ft.Icons.FOLDER_OUTLINED, app.pick_linux_folder),
            None,
            (tr("Rescan folders"), ft.Icons.REFRESH_ROUNDED, app.rescan)]))
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
            tw = self.tw = twins(games)
            self._update_header(games, pc)
            self.update_update_bar()
            self.update_resume_bar()  # also after Dismiss / Resume and when installs finish or fail
            if not games:
                self.body.content = C.empty_state(
                    ft.Icons.LIBRARY_ADD_OUTLINED, tr("Add your games"),
                    tr("Choose a folder with Quest, Android or PC VR games. FramePort finds them and gets them "
                       "ready."),
                    C.primary(tr("Scan a folder…"), ft.Icons.FOLDER_OPEN_ROUNDED, self.app.pick_folder, big=True),
                    C.secondary(tr("Add an APK…"), ft.Icons.ANDROID_ROUNDED, self.app.pick_apk),
                    features=[
                        (G.FRAME, tr("Quest games"),
                         tr("Patched for the Steam Frame and installed over Wi-Fi or USB.")),
                        (G.PC, tr("PC VR games"),
                         tr("Windows VR games, on the Frame or on this PC.")),
                        (ft.Icons.APPS_ROUNDED, tr("Android and Linux apps"),
                         tr("They run on the Frame too.")),
                    ])
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
        for pkg in self.marks:
            self._paint_card(pkg)
        self._update_sel_bar()
        self._update_shelf()
        C.update(self.grid, self.sel_bar, self.select_btn, self.shelf)

    def toggle_selected(self, pkg: str) -> None:
        self.selected.symmetric_difference_update({pkg})
        chk = self.checks.get(pkg)
        if chk:
            chk.content.value = pkg in self.selected
            C.update(chk)
        if pkg in self.marks:
            self._paint_card(pkg)
            C.update(self.marks[pkg][0])
        self._update_sel_bar()

    def _paint_card(self, pkg: str) -> None:
        """A card's border + selection fade (properties only): selected = 2 px accent + the portal fade, hovered =
        1 px accent, else the plain border."""
        tile, overlay, hovered = self.marks[pkg]
        on = self.select_mode and pkg in self.selected
        C.selected_style(overlay, on, subtle=True)
        tile.border = ft.Border.all(2 if on else 1, T.ACCENT if on or hovered else T.BORDER)

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
                      tooltip=None if app.frame_state == "connected" else tr("Connect your Frame first.")),
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
                C.body(tr_n("{n} install didn't finish: {names}. Resume continues where it stopped.",
                            "{n} installs didn't finish: {names}. Resume continues where it stopped.",
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

    def _focus(self, on: bool) -> None:
        self.search_focused = on

    def type_key(self, char: str | None, backspace: bool = False) -> None:
        """A key typed while the search isn't focused (app._on_key): add it (or remove the last character), filter
        like typing in the field, and focus the field so the next keys go there directly."""
        value = self.search.value or ""
        self.search.value = value[:-1] if backspace else value + (char or "")
        self.set_query(self.search.value)
        C.update(self.search)
        try:
            self.app.page.run_task(self._focus_at_end)
        except Exception:  # noqa: BLE001
            pass

    async def _focus_at_end(self) -> None:
        await self.search.focus()
        n = len(self.search.value or "")
        try:  # Flutter selects nothing on a programmatic focus; put the cursor after the typed text
            self.search.selection = ft.TextSelection(base_offset=n, extent_offset=n)
            C.update(self.search)
        except Exception:  # noqa: BLE001  (older Flet: no selection property; the cursor goes to the end anyway)
            pass

    def _on_search(self, e):
        self.set_query(e.control.value)

    def set_query(self, value: str) -> None:
        """The search text changed: show/hide the clear X, filter after a short pause (debounced)."""
        from .. import easter

        egg = getattr(self.app, "logo_egg", None)
        if egg and easter.is_self_search(value) and not easter.is_self_search(self.f.get("q")):
            egg.hello_search()  # (easter egg: searching for FramePort itself; the search goes on as usual)
        self.f["q"] = value
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
        self._update_shelf()
        if update:
            C.update(self.grid, self.count, self.shelf)

    # ---------------------------------------------------------------- "On your Frame" shelf
    def _update_shelf(self) -> None:
        """Show the shelf's games (properties + reused cards; a card is rebuilt only when what it shows changed)."""
        app = self.app
        games = shelf_games(self.games, self.f, app.frame_info, app.frame_state == "connected", self.select_mode)
        controls = []
        for g in games:
            pkg = g["package"]
            key = (display_title(g, self.tw), C.install_state(g, app.frame_info), bool(app.jobs.busy_with(pkg)),
                   thumbs.url(pkg, SHELF_ART, T.px(640), wait=False))
            old = self.shelf_cards.get(pkg)
            if not old or old[0] != key:
                old = self.shelf_cards[pkg] = (key, self.shelf_card(g, *key))
            controls.append(old[1])
        for pkg in set(self.shelf_cards) - {g["package"] for g in games}:
            self.shelf_cards.pop(pkg, None)
        self.shelf_row.controls = controls
        self.shelf.visible = bool(controls)

    def shelf_card(self, g: dict, title: str, state: str | None, busy: bool, art: str | None) -> ft.Control:
        """A wide cover: title, "Update ready" when the Frame runs an older build, Play on hover; click opens the
        game, right-click shows its menu (the same as the grid's)."""
        app = self.app
        pkg = g["package"]
        w, h = T.px(264), T.px(148)
        button = C.CoverButton(ft.Icons.PLAY_ARROW_ROUNDED, tr("Play on Frame"), lambda e: app.play(pkg, "frame"),
                               T.px(48), launch=tr("Starting on Frame…"), reduce_motion=app.reduce_motion)
        quick = ft.Container(button.control, left=0, right=0, top=0, bottom=T.px(36), alignment=ft.Alignment.CENTER,
                             opacity=0, animate_opacity=ft.Animation(180, ft.AnimationCurve.EASE_OUT), visible=not busy)
        badges = [C.install_badge("outdated")] if state == "outdated" else []
        if busy:
            badges = [C.pill(tr("Working…"), T.ACCENT, ft.Icons.SYNC_ROUNDED, solid=True)]
        cover = C.art_fill(art, placeholder_icon=C.platform_icon(g))
        shade = scrim()
        tile = ft.Container(
            ft.Stack([
                art_frame(cover),
                shade,
                ft.Container(C.bottom_fade(None, 0.92), left=0, right=0, bottom=0, top=T.px(48)),
                *([ft.Container(ft.Row(badges, spacing=T.px(4)), left=T.px(10), top=T.px(10))] if badges else []),
                ft.Container(button.status, left=0, right=0, top=0, bottom=T.px(36)),  # below the button (clicks)
                quick,
                ft.Container(ft.Text(title, size=T.px(14), weight=ft.FontWeight.W_700, color=T.TEXT, max_lines=1,
                                     overflow=ft.TextOverflow.ELLIPSIS),
                             left=T.px(12), right=T.px(12), bottom=T.px(10)),
            ]),
            width=w, height=h, border_radius=T.RADIUS, bgcolor=T.SURFACE, border=ft.Border.all(1, T.BORDER),
            shadow=card_shadow(),
            tooltip=ft.Tooltip(message=tr("Click to open · right-click for quick actions"), wait_duration=1500),
            on_click=lambda e: app.open_game(pkg))
        hover_motion(tile, cover)

        def hover(e):
            on = e.data in (True, "true")
            card_hover(tile, cover, shade, on, app.reduce_motion)
            tile.border = ft.Border.all(1, T.ACCENT if on else T.BORDER)
            quick.opacity = 1 if on else 0
            button.show(on)
            tile.update()
        tile.on_hover = hover
        # (padding: room for the hover lift and shadow inside the scrolling row, which clips)
        return ft.Container(ft.GestureDetector(content=tile,
                                               on_secondary_tap_down=lambda e: self.open_menu(pkg, e.global_position)),
                            padding=ft.Padding(T.px(2), T.px(6), T.px(2), T.px(8)))

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
        return C.segmented(options, self.f.get(key), lambda v: self._set(key, v))

    def _menu_chip(self, key: str, label: str, options: list[tuple[str, str]]) -> ft.Control:
        current = dict(options).get(self.f.get(key), options[0][1])
        return C.menu_button(
            content=ft.Container(ft.Row([C.meta(label + ":"), C.body(current, T.TEXT, size=T.T_META),
                                         ft.Icon(ft.Icons.EXPAND_MORE_ROUNDED, size=T.px(16), color=T.TEXT_2)],
                                        spacing=T.px(4), tight=True),
                                 padding=ft.Padding(T.px(12), T.px(7), T.px(8), T.px(7)), border_radius=T.px(20),
                                 border=ft.Border.all(1, T.BORDER)),
            items=[C.check_item(text, self.f.get(key) == value, lambda e, v=value: self._set(key, v))
                   for value, text in options])

    def _toggle_tag(self, tag: str):
        tags = list(self.f.get("tags") or [])
        tags.remove(tag) if tag in tags else tags.append(tag)
        self._set("tags", tags)

    def _tag_menu(self, games: list[dict]) -> ft.Control:
        chosen = self.f.get("tags") or []
        label = ", ".join(chosen) if chosen else tr("Any")
        items = [C.check_item(t, t in chosen, lambda e, t=t: self._toggle_tag(t)) for t in all_tags(games)]
        if chosen:
            items += C.menu_items([None, (tr("Clear tags"), ft.Icons.CLOSE_ROUNDED, lambda e: self._set("tags", []))])
        return C.menu_button(
            content=ft.Container(ft.Row([ft.Container(ft.Icon(ft.Icons.SELL_OUTLINED, size=T.px(14), color=T.TEXT_2),
                                                      width=T.px(16), height=T.px(16),
                                                      alignment=ft.Alignment.CENTER),  # sized: overlapped the label
                                         C.meta(tr("Tags:")),
                                         C.body(label if len(label) < 28
                                                else tr("{len} selected").format(len=len(chosen)), T.TEXT,
                                                size=T.T_META),
                                         ft.Icon(ft.Icons.EXPAND_MORE_ROUNDED, size=T.px(16), color=T.TEXT_2)],
                                        spacing=T.px(4), tight=True),
                                 padding=ft.Padding(T.px(12), T.px(7), T.px(8), T.px(7)), border_radius=T.px(20),
                                 border=ft.Border.all(1, T.ACCENT if chosen else T.BORDER)),
            items=items, tooltip=C.tip(HELP["tags"]))

    # ---------------------------------------------------------------- cards
    def _key(self, g: dict, pc: set[str], tw: set[str]) -> tuple:
        pkg = g["package"]
        job = self.app.jobs.busy_with(pkg)
        return (display_title(g, tw), (g.get("recipe") or {}).get("status"), C.install_state(g, self.app.frame_info),
                pkg in pc, bool(job), self.app.quick_action(g)[0], thumbs.url(pkg, CARD_ART),
                self.app.frame_state)

    def starting_text(self, g: dict) -> str:
        """What a cover's Play button says while the game starts (Play goes to the Frame first, as play_options)."""
        app = self.app
        on_frame = app.frame_state == "connected" and C.install_state(g, app.frame_info) in ("installed", "outdated")
        return tr("Starting on Frame…") if on_frame else tr("Starting on this PC…")

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
            badges.append(C.pill(tr("Check program"), T.WARN, ft.Icons.HELP_OUTLINE_ROUNDED, overlay=True,
                                 tooltip=C.tip(HELP["check_exe"])))
        label, _, help_key = C.platform(g)
        platform = C.pill(label, T.PC if rift else T.TEXT, C.platform_icon(g), overlay=True,
                          tooltip=C.tip(HELP[help_key]))
        check = ft.Container(ft.Checkbox(value=pkg in self.selected, active_color=T.ACCENT, check_color=T.ON_ACCENT,
                                         on_change=lambda e: self.toggle_selected(pkg)),
                             bgcolor=T.soft("#000000", 0.6), border_radius=T.RADIUS_SM, left=T.px(6), top=T.px(40),
                             visible=self.select_mode)
        self.checks[pkg] = check
        overlay = ft.Container(left=0, right=0, top=0, bottom=0, border_radius=T.RADIUS)  # the selected fade
        quick_label, quick_kind = app.quick_action(g)
        # one round Play / Install button in the middle of the cover, shown on hover
        button = C.CoverButton(quick_icon(quick_kind), quick_label, lambda e: app.primary_action(pkg), T.px(60),
                               launch=self.starting_text(g) if quick_kind == "play" else None,
                               reduce_motion=app.reduce_motion) if quick_label else None
        quick = ft.Container(button.control, left=0, right=0, top=0, bottom=T.px(56), alignment=ft.Alignment.CENTER,
                             opacity=0, animate_opacity=ft.Animation(180, ft.AnimationCurve.EASE_OUT)) \
            if button else None
        dim = state == "missing" and not on_pc
        cover = C.art_fill(art, opacity=0.5 if dim else 1.0,
                           placeholder_icon=C.platform_icon(g))
        shade = scrim() if button else None
        tile = ft.Container(
            ft.Stack([
                art_frame(cover),
                *([shade] if shade else []),
                ft.Container(C.bottom_fade(None, 0.92), left=0, right=0, bottom=0, top=T.px(90)),
                # platform + state badges in one row that wraps: on a narrow card "On Frame" covered "Android"
                ft.Container(ft.Row([platform, *badges], spacing=T.px(4), run_spacing=T.px(4), wrap=True),
                             left=T.px(10), right=T.px(10), top=T.px(10)),
                overlay,
                # the pill's layer below the button's: above it, the (empty) layer took the button's clicks
                *([ft.Container(button.status, left=0, right=0, top=0, bottom=T.px(56)), quick] if quick else []),
                check,
                ft.Container(ft.Column([
                    ft.Text(display_title(g, tw), size=T.px(14), weight=ft.FontWeight.W_700, color=T.TEXT, max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Container(ft.Row([C.dot(s_color, 7), C.meta(s_label, T.TEXT_2)], spacing=T.px(6), tight=True),
                                 tooltip=C.tip(HELP["status"])),
                ], spacing=T.px(4)), left=T.px(12), right=T.px(12), bottom=T.px(12)),
            ], expand=True),
            border_radius=T.RADIUS, bgcolor=T.SURFACE, border=ft.Border.all(1, T.BORDER), expand=True,
            shadow=card_shadow(),
            tooltip=ft.Tooltip(message=tr("Click to open · right-click for quick actions · drag across cards to "
                                          "select several"), wait_duration=1500),
            on_click=lambda e: self.toggle_selected(pkg) if self.select_mode else app.open_game(pkg))
        hover_motion(tile, cover)

        def hover(e):
            on = e.data in (True, "true")
            self.drag.hover(pkg, on)
            self.marks[pkg] = (tile, overlay, on)
            self._paint_card(pkg)
            show = on and not self.select_mode
            card_hover(tile, cover, shade, on, app.reduce_motion)
            if shade:
                shade.opacity = 1 if show else 0
            if quick:
                quick.opacity = 1 if show else 0
                button.show(show)
            tile.update()
        tile.on_hover = hover
        self.marks[pkg] = (tile, overlay, False)
        self._paint_card(pkg)
        # no key=: Flet freezes keyed controls, and cards change in place (hover, state); the grid only ever holds
        # the matching cards in order (see _apply), which is what fixed search and sorting
        return ft.GestureDetector(content=tile, expand=True,
                                  on_secondary_tap_down=lambda e: self.open_menu(pkg, e.global_position))

    def open_menu(self, pkg: str, position=None) -> None:
        """Right-click on a card: that game's quick actions (built now, so they match the game's current state)."""
        self.menu.items = C.menu_items(self.app.game_actions(pkg))
        C.update(self.menu)
        self.app.page.run_task(self.menu.open, global_position=position)
