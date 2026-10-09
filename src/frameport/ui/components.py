"""Reusable GUI building blocks (all styling comes from ui.theme)."""
from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import NamedTuple

import flet as ft

from ..i18n import tr
from . import glyphs
from . import glyphs as G  # G.NAME at call sites, like the other modules
from . import theme as T
from .help import HELP

STATUS_STYLE: dict[str, tuple] = {}
# per-game state on a target: label, icon, color
INSTALL_STYLE: dict[str, tuple] = {}


def _fill_styles() -> None:
    """(Re)fill the style maps with the active theme's colours, in place: callers keep a reference to the dicts."""
    STATUS_STYLE.update({
        "works": (tr("Works"), T.OK),
        "issues": (tr("Works with issues"), T.WARN),
        "unsupported": (tr("Can't run"), T.ERROR),
        "unknown": (tr("Untested"), T.TEXT_3),
    })
    INSTALL_STYLE.update({
        "installed": (tr("On Frame"), ft.Icons.CHECK_CIRCLE_ROUNDED, T.OK),
        "outdated": (tr("Update ready"), ft.Icons.UPDATE_ROUNDED, T.WARN),
        "missing": (tr("Not installed"), ft.Icons.CLOUD_OFF_ROUNDED, T.TEXT_3),
        "on_pc": (tr("On this PC"), G.PC, T.PC),
    })


T.on_change(_fill_styles)


def _deployment(game: dict, frame_info: dict | None) -> dict | None:
    return next((d for d in (frame_info or {}).get("installed", []) if d.get("package") == game.get("package")), None)


def frame_drive(game: dict, frame_info: dict | None) -> dict | None:
    """The drive the game is installed on ({internal, path, label, missing}), None if not installed / old agent."""
    from ..install.drives import game_drive

    return game_drive(_deployment(game, frame_info))


def drive_note(drive: dict | None) -> str:
    """" · on SD Card" / " · SD Card not inserted" for a game on another drive, "" on internal storage."""
    if not drive or drive.get("internal"):
        return ""
    if drive.get("missing"):
        return tr(" · {label} not inserted").format(label=drive.get("label") or tr("its drive"))
    return tr(" · on {label}").format(label=drive.get("label") or "")


def settings_diff(game: dict, frame_info: dict | None) -> tuple[list[str], list[str]] | None:
    """(patches turned on, patches turned off) in the app since the Frame's copy was installed; None if the same (or
    unknown: installs from before FramePort recorded their recipe)."""
    dep = _deployment(game, frame_info)
    rec = (dep or {}).get("recipe")
    if not isinstance(rec, dict) or "patches" not in rec:
        return None
    installed, now = set(rec["patches"]), set((game.get("recipe") or {}).get("patches") or {})
    added, removed = sorted(now - installed), sorted(installed - now)
    return (added, removed) if added or removed else None


def platform(g: dict) -> tuple[str, str, str]:
    """(short label, long label, help key) for a game's platform: what it was made for, not how it runs here."""
    if g.get("kind") == "rift":
        extra = ((g.get("analysis") or {}).get("extra") or {})
        if extra.get("flat"):
            return tr("Windows"), tr("Windows game (Proton)"), "platform_windows"
        oculus = extra.get("needs_revive") or extra.get("libovr")
        return tr("PC VR"), tr("PC VR · Oculus") if oculus else tr("PC VR"), "platform_pcvr"
    if g.get("kind") == "linux":
        vr = (((g.get("analysis") or {}).get("extra") or {}).get("openxr"))
        return tr("Linux"), tr("Linux VR app · OpenXR") if vr else tr("Linux app (arm64)"), "platform_linux"
    kind = (((g.get("analysis") or {}).get("extra") or {}).get("vr_kind")) or "quest"
    if kind == "quest":
        return tr("Quest"), tr("Meta Quest"), "platform_quest"
    if kind == "none":
        return tr("Android"), tr("Android app"), "platform_android"
    return tr("Android VR"), {"openxr": "Android VR · OpenXR", "pico_sdk": "Pico", "wave": "HTC Vive (Wave)",
                          "android_xr": "Android XR"}.get(kind, "Android VR"), "platform_android_vr"


def platform_icon(g: dict) -> str:
    """The icon for a game's platform (card badge, artwork placeholder)."""
    return G.PC if g.get("kind") == "rift" else \
        ft.Icons.TERMINAL_ROUNDED if g.get("kind") == "linux" else G.FRAME


def missing_libraries(game: dict, frame_info: dict | None) -> list[str]:
    """A Linux app's shared libraries that the Frame lacks (the app won't start): from the Frame's install record when
    connected, else from the last install FramePort did. Empty when the connected Frame doesn't have the app."""
    dep = _deployment(game, frame_info)
    if frame_info is not None and dep is None:
        return []
    if dep is not None and "missing_libraries" in dep:
        return list(dep.get("missing_libraries") or [])
    installs = [i for i in (game.get("installs") or {}).values() if isinstance(i, dict) and
                isinstance(i.get("result"), dict)]
    last = max(installs, key=lambda i: i.get("time") or 0, default=None)
    return list((last or {}).get("result", {}).get("missing_libraries") or [])


def install_state(game: dict, frame_info: dict | None) -> str | None:
    """None when no Frame is connected; else 'installed', 'outdated' (the local build, or the patch settings, differ
    from what the Frame runs) or 'missing'."""
    if frame_info is None:
        return None
    dep = _deployment(game, frame_info)
    # no record, or the game files are gone (half uninstalled); a game on a microSD card that isn't inserted now is
    # still installed (GitHub #90)
    if not dep or (dep.get("apk_present") is False and not dep.get("drive_missing")):
        return "missing"
    b = game.get("build") or {}
    built = {b.get("sha256"), b.get("alt_sha256")} - {None}
    if built and dep.get("sha256") and dep["sha256"] not in built:
        return "outdated"
    if built and dep.get("sha256") in built and recipe_changed(game):
        return "outdated"  # the recipe changed since this build (e.g. a catalog fix): it needs a new build
    return "outdated" if settings_diff(game, frame_info) else "installed"


def recipe_changed(game: dict) -> bool:
    from ..patches.base import recipe_fingerprint, revised_since_unrecorded

    recipe, b = game.get("recipe") or {}, game.get("build") or {}
    if not recipe:
        return False
    if b.get("recipe_fp"):
        return b["recipe_fp"] != recipe_fingerprint(recipe)
    return revised_since_unrecorded(recipe)


def pc_outdated(g: dict, dep: dict) -> bool:
    """A PC install whose launch settings (pcvr patches + their parameters) differ from the game's recipe now."""
    installed = set((dep.get("recipe") or {}).get("patches") or [])
    want = set((g.get("recipe") or {}).get("patches") or {})
    from ..core.library import recipe_from_dict
    from ..patches.pcvr import game_args

    try:
        args = game_args(recipe_from_dict(g["recipe"]))
    except Exception:  # noqa: BLE001
        args = dep.get("game_args") or []
    return installed != want or list(dep.get("game_args") or []) != args


def one_choice():
    """Wraps the handlers of one dialog (its buttons and its on_dismiss) so that only the first of them acts: a second
    click while the dialog closes (easy in a slow window) ran the rest of a question chain twice — duplicate dialogs
    whose buttons no longer worked. Use: pick = one_choice(); on_click=pick(ok), on_dismiss=pick(closed)."""
    state = {"done": False}

    def wrap(fn):
        def handler(e=None):
            if not state["done"]:
                state["done"] = True
                return fn(e)
        return handler
    return wrap


def update(*controls: ft.Control) -> None:
    """Update controls that may not be on the page yet (Flet raises for those)."""
    for c in controls:
        try:
            c.update()
        except (RuntimeError, AssertionError):
            pass


# ------------------------------------------------------------------------------------------ text
def title(text: str, size: int | None = None) -> ft.Text:
    size = size or T.T_TITLE
    return ft.Text(text, size=size, weight=ft.FontWeight.W_700, color=T.TEXT)


def h2(text: str) -> ft.Text:
    return ft.Text(text, size=T.T_H2, weight=ft.FontWeight.W_600, color=T.TEXT)


def switch(label: str = "", wrap: bool = True, **kw) -> ft.Control:
    """A labelled switch in the theme's text colour (Material's default label is dark text). The label wraps (a
    Switch's own label is cut off when it's longer than the window); clicking it toggles the switch too.
    wrap=False keeps the built-in label, for a short label in a Row next to other controls."""
    kw.setdefault("active_color", T.ACCENT)
    if T.DUAL:  # orange thumb on a blue track
        kw.setdefault("active_track_color", T.soft(T.SECONDARY, 0.5))
    if not wrap or not label:
        return ft.Switch(label=label or None, label_text_style=ft.TextStyle(color=T.TEXT, size=T.px(14)), **kw)
    sw = ft.Switch(**kw)

    def click_label(e):
        if sw.disabled:
            return
        sw.value = not sw.value
        update(sw)
        if sw.on_change:
            sw.on_change(SimpleNamespace(control=sw))
    return ft.Row([sw, ft.Container(ft.Text(label, color=T.TEXT, size=T.px(14)), expand=True, on_click=click_label,
                                    ink=True, border_radius=T.RADIUS_XS)],
                  spacing=T.S2, vertical_alignment=ft.CrossAxisAlignment.CENTER, data="switch")


def body(text: str, color: str | None = None, size: int | None = None, **kw) -> ft.Text:
    size, color = size or T.T_BODY, color or T.TEXT_2
    return ft.Text(text, size=size, color=color, **kw)


def meta(text: str, color: str | None = None, **kw) -> ft.Text:
    return ft.Text(text, size=T.T_META, color=color or T.TEXT_3, **kw)


# ------------------------------------------------------------------------------------------ chips / badges
def pill(text: str, color: str | None = None, icon: str | None = None, solid: bool = False,
         tooltip: str | ft.Tooltip | None = None, overlay: bool = False) -> ft.Container:
    """overlay: for use on top of artwork (dark translucent backing so it reads on any image)."""
    color = color or T.TEXT_2
    fg = T.ON_ACCENT if solid else color
    items = ([as_icon(icon, T.px(13), fg)] if icon else []) + \
        [ft.Text(text, size=T.T_SMALL, color=fg, weight=ft.FontWeight.W_600)]
    return ft.Container(ft.Row(items, spacing=T.px(4), tight=True), tooltip=tooltip,
                        bgcolor=color if solid else T.soft("#000000", 0.62) if overlay else T.soft(color, 0.12),
                        border_radius=T.px(20),
                        padding=ft.Padding(T.px(8), T.px(3), T.px(9), T.px(3)))


def status_chip(status: str) -> ft.Container:
    label, color = STATUS_STYLE.get(status, STATUS_STYLE["unknown"])
    return pill(label, color, tooltip=tip(HELP["status"]))


def install_badge(state: str, solid: bool = True) -> ft.Container:
    label, icon, color = INSTALL_STYLE[state]
    return pill(label, color, icon, solid=solid, tooltip=tip(HELP["update_ready"]) if state == "outdated" else None)


def dot(color: str, size: int = 8) -> ft.Container:
    """A status dot; `size` in pixels at 100 % (scaled here)."""
    size = T.px(size)
    return ft.Container(width=size, height=size, border_radius=size, bgcolor=color)


# ------------------------------------------------------------------------------------------ help hints
def tip(text: str) -> ft.Tooltip:
    """A tooltip that wraps long help text (plain string tooltips run across the whole window)."""
    return ft.Tooltip(message=text, size_constraints=ft.BoxConstraints(max_width=T.px(340)),
                      padding=ft.Padding(T.px(12), T.px(8), T.px(12), T.px(8)), prefer_below=True)


def help_icon(key_or_text: str, size: int = 18) -> ft.Container:
    """A "?" that explains a non-obvious term on hover: a key of ui.help.HELP, or the text itself."""
    size = T.px(size)
    return ft.Container(ft.Icon(ft.Icons.HELP_OUTLINE_ROUNDED, size=size, color=T.TEXT_2),
                        tooltip=tip(HELP.get(key_or_text, key_or_text)), padding=T.px(3), border_radius=size)


def with_help(control: ft.Control, key: str | None) -> ft.Control:
    """`control` followed by a help icon (or just `control` when there's no key)."""
    if not key:
        return control
    if getattr(control, "data", None) == "switch":
        # the "?" goes right after the label (which takes only the width it needs and still wraps when long)
        label = control.controls[1]
        label.expand, label.expand_loose = True, True
        control.controls.append(help_icon(key))
        control.expand = True
        return control
    return ft.Row([control, help_icon(key)], spacing=T.px(4), tight=True,
                  vertical_alignment=ft.CrossAxisAlignment.CENTER)


# ------------------------------------------------------------------------------------------ icons
def _asset_src(name: str) -> str:
    from ..artwork.thumbs import asset_url
    from ..core.paths import user_data_dir

    return asset_url(glyphs.path(name, user_data_dir()))


def as_icon(name, size: float | None = None, color: str | None = None) -> ft.Control:
    """A Material icon (ft.Icons.…) or one of FramePort's glyphs (glyphs.FRAME …, tinted like an icon). Its colour can
    be changed later through .color either way."""
    size, color = size or T.px(20), color or T.TEXT_2
    if glyphs.is_glyph(name):
        return ft.Image(src=_asset_src(name), width=size, height=size, color=color,
                        color_blend_mode=ft.BlendMode.SRC_IN, fit=ft.BoxFit.CONTAIN)
    return ft.Icon(name, size=size, color=color)


def logo(size: float, solid: bool = False) -> ft.Image:
    """FramePort's logo (the monitor that comes out of a portal as the Frame); solid = the flat version for small
    sizes."""
    return ft.Image(src=_asset_src("logo-solid" if solid else "logo"), width=size, height=size, fit=ft.BoxFit.CONTAIN)


def _btn_icon(name, color: str, disabled: bool = False, size: float = 18):
    """A button's icon: Material icons stay as they are (the control colours them), glyphs become tinted images at
    the size Flutter gives icons in buttons (18; not scaled with the UI, like the Material icons next to them).
    Menu items size their icons themselves (menu_items)."""
    return as_icon(name, size, T.TEXT_3 if disabled else color) if glyphs.is_glyph(name) else name


def wordmark(size: float) -> ft.Control:
    """"FramePort": in dual themes "Frame" in the Frame's orange, a portal, then "Port" in the PC's blue."""
    if not T.DUAL:
        return ft.Text("FramePort", size=size, weight=ft.FontWeight.W_800, color=T.TEXT)
    from ..artwork.thumbs import asset_url
    from ..core.paths import user_data_dir

    mark = glyphs.portal_mark(user_data_dir(), T.ACCENT, T.SECONDARY, T.BG)
    word = lambda text, color: ft.Text(text, size=size, weight=ft.FontWeight.W_800, color=color)  # noqa: E731
    portal = ft.Image(src=asset_url(mark), width=size * 0.8, height=size * 1.4, fit=ft.BoxFit.CONTAIN, scale=1.0,
                      animate_scale=ft.Animation(220, ft.AnimationCurve.EASE_OUT))
    # .data = the portal (the app pulses it once when the Frame connects)
    return ft.Row([word("Frame", T.ACCENT), portal, word("Port", T.SECONDARY)], data=portal,
                  spacing=size * 0.08, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER)


def portal_gradient(vertical: bool = False, opacity: float = 1.0) -> ft.LinearGradient:
    """Blue (the PC's portal) into orange (the Frame's), as in the logo."""
    begin, end = (ft.Alignment.TOP_CENTER, ft.Alignment.BOTTOM_CENTER) if vertical else \
        (ft.Alignment.CENTER_LEFT, ft.Alignment.CENTER_RIGHT)
    return ft.LinearGradient(begin=begin, end=end,
                             colors=[T.soft(T.SECONDARY, opacity), T.soft(T.ACCENT, opacity)])


# ------------------------------------------------------------------------------------------ buttons
def button_shape(radius=None) -> ft.RoundedRectangleBorder:
    """The buttons' shape (RADIUS_SM unless given)."""
    return _shape(radius)


def _shape(radius=None):
    radius = T.RADIUS_SM if radius is None else radius
    return ft.RoundedRectangleBorder(radius=radius)


def primary(label: str, icon: str | None = None, on_click: Callable | None = None, disabled: bool = False,
            tooltip: str | None = None, big: bool = False) -> ft.FilledButton:
    padding = (ft.Padding(T.px(22), T.px(18), T.px(22), T.px(18)) if big
               else ft.Padding(T.px(16), T.px(12), T.px(16), T.px(12)))
    return ft.FilledButton(label, icon=_btn_icon(glyphs.solid(icon), T.ON_ACCENT, disabled), on_click=on_click,
                           disabled=disabled,
                           tooltip=tooltip,
                           style=ft.ButtonStyle(shape=_shape(), bgcolor={ft.ControlState.DEFAULT: T.ACCENT,
                                                                        ft.ControlState.DISABLED: T.SURFACE_3},
                                                color={ft.ControlState.DEFAULT: T.ON_ACCENT,
                                                       ft.ControlState.DISABLED: T.TEXT_3},
                                                padding=padding,
                                                text_style=ft.TextStyle(size=T.px(15) if big else T.px(13),
                                                                        weight=ft.FontWeight.W_600)))


def secondary(label: str, icon: str | None = None, on_click: Callable | None = None, disabled: bool = False,
              tooltip: str | None = None) -> ft.OutlinedButton:
    # dual themes: secondary actions are the blue portal next to the orange primary one
    edge, mark = (T.soft(T.SECONDARY, 0.55), T.SECONDARY) if T.DUAL else (T.BORDER_STRONG, T.TEXT)
    return ft.OutlinedButton(label, icon=_btn_icon(icon, mark, disabled), on_click=on_click, disabled=disabled,
                             tooltip=tooltip,
                             style=ft.ButtonStyle(shape=_shape(), side=ft.BorderSide(1, edge),
                                                  color={ft.ControlState.DEFAULT: T.TEXT,
                                                         ft.ControlState.DISABLED: T.TEXT_3},
                                                  icon_color={ft.ControlState.DEFAULT: mark,
                                                              ft.ControlState.DISABLED: T.TEXT_3},
                                                  overlay_color=T.soft(T.SECONDARY, 0.08) if T.DUAL else None,
                                                  padding=ft.Padding(T.px(14), T.px(12), T.px(14), T.px(12))))


def ghost(label: str, icon: str | None = None, on_click: Callable | None = None, disabled: bool = False,
          tooltip: str | None = None, color: str | None = None, url: str | None = None) -> ft.TextButton:
    """A quiet action (Close, Cancel, links); `color` e.g. T.ACCENT for a link, `url` opens in the browser."""
    return ft.TextButton(label, icon=_btn_icon(icon, color or T.TEXT_2, disabled), on_click=on_click,
                         disabled=disabled, tooltip=tooltip, url=url,
                         style=ft.ButtonStyle(shape=_shape(), color={ft.ControlState.DEFAULT: color or T.TEXT_2,
                                                                     ft.ControlState.DISABLED: T.TEXT_3}))


def icon_btn(icon: str, tooltip: str, on_click: Callable | None = None, disabled: bool = False,
             color: str | None = None) -> ft.IconButton:
    return ft.IconButton(_btn_icon(icon, color or T.TEXT_2, disabled), tooltip=tooltip, on_click=on_click,
                         disabled=disabled, icon_color=color or T.TEXT_2,
                         icon_size=T.px(20), style=ft.ButtonStyle(shape=_shape()))


def danger(label: str, icon: str | None = None, on_click: Callable | None = None, disabled: bool = False,
           tooltip: str | None = None, outline: bool = False) -> ft.FilledButton | ft.OutlinedButton:
    """A destructive action (uninstall, delete): C.primary's shape and size on the error colour. outline = C.secondary's
    look in the error colour, for an entry that asks first (the filled one is the dialog's final button)."""
    if outline:
        btn = secondary(label, icon, on_click, disabled, tooltip)
        btn.style.side = ft.BorderSide(1, T.soft(T.ERROR, 0.6))
        btn.style.color = {ft.ControlState.DEFAULT: T.ERROR, ft.ControlState.DISABLED: T.TEXT_3}
        btn.style.icon_color = {ft.ControlState.DEFAULT: T.ERROR, ft.ControlState.DISABLED: T.TEXT_3}
        btn.style.overlay_color = T.soft(T.ERROR, 0.08)
        if glyphs.is_glyph(icon):
            btn.icon = _btn_icon(icon, T.ERROR, disabled)
        return btn
    btn = primary(label, icon, on_click, disabled, tooltip)
    btn.style.bgcolor = {ft.ControlState.DEFAULT: T.ERROR, ft.ControlState.DISABLED: T.SURFACE_3}
    return btn


_danger = danger  # for functions with a `danger` argument


def primary_menu(label: str, icon: str | None, items: list[ft.PopupMenuItem], tooltip: str | None = None
                 ) -> ft.PopupMenuButton:
    """A menu that opens from a button looking exactly like C.primary (e.g. Library → Add games). The button is
    drawn disabled (in the enabled colours) so it doesn't take the click: the menu button around it does."""
    btn = primary(label, icon, disabled=True)
    btn.style.bgcolor = T.ACCENT
    btn.style.color = T.ON_ACCENT
    btn.style.icon_color = T.ON_ACCENT
    btn.style.mouse_cursor = ft.MouseCursor.CLICK
    return menu_button(items, content=btn, tooltip=tooltip or "")


# ------------------------------------------------------------------------------------------ inputs
def _input_style(kw: dict) -> dict:
    """The one input look: filled SURFACE_3, no border until focused (then the accent), small radius."""
    kw.setdefault("dense", True)
    kw.setdefault("border_radius", T.RADIUS_SM)
    kw.setdefault("border_color", ft.Colors.TRANSPARENT)
    kw.setdefault("focused_border_color", T.ACCENT)
    kw.setdefault("text_size", T.T_BODY)
    kw.setdefault("content_padding", ft.Padding(T.px(12), T.px(10), T.px(12), T.px(10)))
    return kw


def field(**kw) -> ft.TextField:
    """A text field in the app's input style (all ft.TextField arguments work)."""
    kw = _input_style(kw)
    kw.setdefault("bgcolor", T.SURFACE_3)  # (filled=True would add Material's extra height)
    kw.setdefault("cursor_color", T.ACCENT)
    if kw.get("label") is not None:
        kw.setdefault("label_style", ft.TextStyle(color=T.TEXT_2, size=T.T_BODY))
    return ft.TextField(**kw)


def search(**kw) -> ft.TextField:
    """A search field: C.field with a magnifier."""
    kw.setdefault("prefix_icon", ft.Icons.SEARCH_ROUNDED)
    kw.setdefault("content_padding", ft.Padding(T.px(12), T.px(8), T.px(12), T.px(8)))
    return field(**kw)


def dropdown(**kw) -> ft.Dropdown:
    """A dropdown in the app's input style (all ft.Dropdown arguments work)."""
    kw.setdefault("content_padding", ft.Padding(T.px(12), T.px(6), T.px(8), T.px(6)))
    kw = _input_style(kw)
    kw.setdefault("filled", True)
    kw.setdefault("fill_color", T.SURFACE_3)
    if kw.get("label") is not None:
        kw.setdefault("label_style", ft.TextStyle(color=T.TEXT_2, size=T.T_BODY))
    return ft.Dropdown(**kw)


# ------------------------------------------------------------------------------------------ selection
def selected_style(box: ft.Container, on: bool, icon: ft.Control | None = None, text: ft.Control | None = None,
                   idle_bg: str | None = None, subtle: bool = False) -> None:
    """The app-wide "this one is selected" look, set on an existing container (call again when it changes): dual
    themes = the portal fade with a white icon and label (an orange icon on the fade clashed: owner), else the accent
    tint with an accent icon. `idle_bg` = the background when not selected; subtle = a lighter fade (an overlay on
    a picture, e.g. a selected screenshot)."""
    if T.DUAL:
        box.bgcolor, box.gradient = (None, portal_gradient(opacity=0.14 if subtle else 0.22)) if on else (idle_bg, None)
    else:
        box.bgcolor, box.gradient = (T.soft(T.ACCENT, 0.12) if subtle else T.ACCENT_SOFT) if on else idle_bg, None
    if icon is not None:
        icon.color = (T.TEXT if T.DUAL else T.ACCENT) if on else T.TEXT_2
    if text is not None:
        text.color = T.TEXT if on else T.TEXT_2


def quiet(*controls: ft.Control, shown: bool = False) -> ft.Container:
    """Row/tile actions that only show on hover, keyboard focus or selection ("quiet rows"): opacity 0 keeps their
    space, so the layout doesn't jump when they appear. Toggle with reveal()."""
    content = controls[0] if len(controls) == 1 else ft.Row(list(controls), spacing=0, tight=True)
    return ft.Container(content, opacity=1.0 if shown else 0.0, animate_opacity=ft.Animation(120))


def reveal(box: ft.Container, on: bool) -> bool:
    """Show/hide a quiet() box; True when it changed (the caller updates it)."""
    value = 1.0 if on else 0.0
    if box.opacity == value:
        return False
    box.opacity = value
    return True


def segmented(options: list[tuple[str, str]], value: str | None, on_change: Callable[[str], None]) -> ft.Container:
    """A pill track of choices (one selected): the selected one is the portal fade in dual themes, else SURFACE_3.
    Clicking restyles in place, then calls on_change(value)."""
    items: dict[str, ft.Container] = {}

    def paint(current):
        for v, box in items.items():
            on = v == current
            box.content.color = T.TEXT if on else T.TEXT_2
            if T.DUAL:
                box.bgcolor, box.gradient = None, portal_gradient(opacity=0.22) if on else None
            else:
                box.bgcolor, box.gradient = T.SURFACE_3 if on else None, None

    def click(v):
        def handler(e):
            if track.data == v:
                return
            track.data = v
            paint(v)
            update(track)
            on_change(v)
        return handler

    for v, label in options:
        items[v] = ft.Container(ft.Text(label, size=T.T_META, weight=ft.FontWeight.W_600),
                                padding=ft.Padding(T.px(12), T.px(6), T.px(12), T.px(6)), border_radius=T.px(20),
                                on_click=click(v), ink=True)
    track = ft.Container(ft.Row(list(items.values()), spacing=T.px(2), tight=True), padding=T.px(3),
                         border_radius=T.px(22), border=ft.Border.all(1, T.BORDER), data=value)
    paint(value)
    return track


def hoverable(box: ft.Container) -> ft.Container:
    """A clickable container: ink (the hover highlight and the click ripple, drawn over its background or the
    selected fade) like the buttons around it."""
    box.ink = True
    return box


def spinner(size: str = "s", color: str | None = None) -> ft.ProgressRing:
    """Something is running: "s" (16, next to text) or "m" (24); blue in dual themes (progress), else the accent.
    `color` only for a spinner on a coloured fill (e.g. ON_ACCENT inside a primary button)."""
    px = T.ICON_S if size == "s" else T.ICON_L
    return ft.ProgressRing(width=px, height=px, stroke_width=T.px(2),
                           color=color or (T.SECONDARY if T.DUAL else T.ACCENT))


def gauge(value: float = 0.0, size: int = 40, color: str | None = None, stroke: float = 3.5) -> ft.ProgressRing:
    """A level as a ring (e.g. the Frame card's battery): value 0..1 on a SURFACE_3 track; `size` px at 100 %."""
    px = T.px(size)
    return ft.ProgressRing(value=value, width=px, height=px, stroke_width=T.px(stroke), color=color or T.OK,
                           bgcolor=T.SURFACE_3)


# ------------------------------------------------------------------------------------------ containers
def card(content: ft.Control, padding: int | None = None, bgcolor: str | None = None, **kw) -> ft.Container:
    padding = T.S4 if padding is None else padding
    return ft.Container(content, padding=padding, bgcolor=bgcolor or T.SURFACE, border_radius=T.RADIUS,
                        border=ft.Border.all(1, T.BORDER), **kw)


def section(heading: str, *controls: ft.Control, subtitle: str | None = None,
            action: ft.Control | None = None, help: str | None = None) -> ft.Column:
    head = ft.Row([ft.Column([with_help(h2(heading), help)] + ([meta(subtitle)] if subtitle else []), spacing=T.px(2),
                             expand=True)]
                  + ([action] if action else []), vertical_alignment=ft.CrossAxisAlignment.CENTER)
    return ft.Column([head, *controls], spacing=T.S3)


def callout(text: str | ft.Control, kind: str = "info", icon: str | None = None) -> ft.Container:
    color = {"info": T.INFO, "warn": T.WARN, "error": T.ERROR, "ok": T.OK, "pc": T.PC}[kind]
    icon = icon or {"info": ft.Icons.INFO_OUTLINE_ROUNDED, "warn": ft.Icons.WARNING_AMBER_ROUNDED,
                    "error": ft.Icons.ERROR_OUTLINE_ROUNDED, "ok": ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED,
                    "pc": G.PC}[kind]
    content = body(text, T.TEXT) if isinstance(text, str) else text
    # a bar (text + buttons in one Row) is taller than a line of text: centre the icon on it; text that wraps keeps
    # the icon at its first line
    align = ft.CrossAxisAlignment.CENTER if isinstance(content, ft.Row) else ft.CrossAxisAlignment.START
    return ft.Container(ft.Row([as_icon(icon, T.px(18), color), ft.Container(content, expand=True)],
                               spacing=T.S3, vertical_alignment=align),
                        bgcolor=T.soft(color, 0.08), border=ft.Border.all(1, T.soft(color, 0.35)),
                        border_radius=T.RADIUS_SM, padding=ft.Padding(T.px(14), T.px(12), T.px(14), T.px(12)))


def portal_image(src: str, ring_h: float) -> ft.Image:
    """The whole portal (transit_parts()["portal"]) with its ring `ring_h` tall (the picture adds its shadow)."""
    w, h = (n * ring_h / glyphs.PORTAL_RING[1] for n in glyphs.TRANSIT_SIZES["portal"])
    return ft.Image(src=src, width=w, height=h, fit=ft.BoxFit.CONTAIN)


def portal_scene(icon: str, height: float | None = None) -> ft.Control:
    """The transit's portal with what the page is about (`icon`) coming out of it, on a faint glow: the empty states'
    illustration (the Live view's idle picture uses the same portal + headset)."""
    h = height or T.px(72)
    portal = transit_parts()["portal"]
    return ft.Container(
        ft.Row([portal_image(portal, h),
                as_icon(icon, h * 0.5, T.TEXT_2)],
               spacing=T.S3, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        # the glow fades out at the box's top and bottom edges (radius = half the shortest side): no visible edge
        padding=ft.Padding(T.S6, T.S3, T.S6, T.S3),
        gradient=ft.RadialGradient(colors=[T.soft(T.ACCENT, 0.13), T.soft(T.ACCENT, 0.0)], radius=0.5))


def feature_card(icon: str, heading: str, text: str) -> ft.Container:
    """One thing a screen does, for empty states and tips: a tinted round icon, a short heading, one sentence."""
    tint = T.SECONDARY if T.DUAL else T.ACCENT
    return ft.Container(ft.Column([
        ft.Container(as_icon(icon, T.px(20), tint), width=T.px(40), height=T.px(40), border_radius=T.px(20),
                     bgcolor=T.soft(tint, 0.13), alignment=ft.Alignment.CENTER),
        ft.Text(heading, size=T.px(14), weight=ft.FontWeight.W_600, color=T.TEXT),
        body(text, T.TEXT_2),
    ], spacing=T.S2, tight=True), width=T.px(236), padding=T.S4, bgcolor=T.soft(T.SURFACE, 0.75),
        border=ft.Border.all(1, T.BORDER), border_radius=T.RADIUS)


def feature_row(features: list[tuple[str, str, str]]) -> ft.Row:
    """feature_card()s side by side, wrapping on narrow windows."""
    return ft.Row([feature_card(*f) for f in features], wrap=True, spacing=T.S3, run_spacing=T.S3,
                  alignment=ft.MainAxisAlignment.CENTER, vertical_alignment=ft.CrossAxisAlignment.START)


def portal_glow() -> ft.RadialGradient:
    """The faint light behind an empty state: the portal's blue and orange (the accent alone in single themes)."""
    if T.DUAL:
        colors, stops = [T.soft(T.SECONDARY, 0.10), T.soft(T.ACCENT, 0.05), T.soft(T.BG, 0.0)], [0.0, 0.45, 1.0]
    else:
        colors, stops = [T.soft(T.ACCENT, 0.09), T.soft(T.BG, 0.0)], [0.0, 1.0]
    return ft.RadialGradient(colors=colors, stops=stops, center=ft.Alignment(0, -0.25), radius=0.85)


def empty_state(icon: str, heading: str, text: str, *actions: ft.Control,
                features: list[tuple[str, str, str]] | None = None) -> ft.Container:
    """A screen with nothing to show yet: the portal with what the screen is about coming out of it, a heading, one
    sentence and the action that fills it. `features` (icon, heading, sentence) say what the screen will show, so a
    first look isn't an empty page; with them the portal is bigger and a soft portal light fills the background."""
    rich = bool(features)
    return ft.Container(ft.Column([
        portal_scene(icon, T.px(104) if rich else None),
        ft.Container(height=T.S2),
        title(heading, T.T_DISPLAY),
        ft.Container(body(text, text_align=ft.TextAlign.CENTER), width=T.px(500)),
        ft.Container(height=T.S2),
        ft.Row(list(actions), alignment=ft.MainAxisAlignment.CENTER, spacing=T.S3),
        *([ft.Container(height=T.S6), ft.Container(feature_row(features), width=T.px(780))] if rich else []),
    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=T.S2, tight=not rich,
        # rich: scrolls in small windows, centred otherwise (a scrolling Column takes the full height, so the
        # Container's alignment no longer centres it)
        scroll=ft.ScrollMode.AUTO if rich else None, expand=rich,
        alignment=ft.MainAxisAlignment.CENTER if rich else ft.MainAxisAlignment.START),
        alignment=ft.Alignment.CENTER, expand=True, gradient=portal_glow() if rich else None,
        border_radius=T.RADIUS)


def kv(label: str, value: str | ft.Control, help: str | None = None) -> ft.Row:
    return ft.Row([ft.Container(with_help(meta(label), help), width=T.px(130)),
                   ft.Container(value if isinstance(value, ft.Control) else body(value, T.TEXT, selectable=True),
                                expand=True)], vertical_alignment=ft.CrossAxisAlignment.START)


class Check(NamedTuple):
    """One line of a setup checklist (checklist()). ok: True = ready, False = a problem, "warn" = optional/not
    needed yet, None = still checking (spinner). fix = (label, icon, handler) shown on the right while the item isn't
    ready (handler() takes no arguments); extra = a control shown on the right in every state (e.g. "Test")."""
    ok: bool | str | None
    title: str
    detail: str = ""
    help: str | None = None
    fix: tuple[str, str | None, Callable[[], object]] | None = None
    extra: ft.Control | None = None


def check_look(ok: bool | str | None) -> tuple[str | None, str]:
    """(icon, colour) of a checklist mark; icon None = a spinner (still checking)."""
    return {True: (ft.Icons.CHECK_CIRCLE_ROUNDED, T.OK), False: (ft.Icons.ERROR_ROUNDED, T.ERROR),
            "warn": (ft.Icons.RADIO_BUTTON_UNCHECKED_ROUNDED, T.WARN),
            None: (None, T.SECONDARY if T.DUAL else T.ACCENT)}[ok]


def check_fix(item: Check) -> tuple[str, str | None, Callable[[], object]] | None:
    """The fix button an item shows: only while it's a problem or not set up (not while ready or still checking)."""
    return item.fix if item.ok in (False, "warn") else None


def checklist(items: list[Check | tuple]) -> ft.Column:
    """The setup checklists (Frame → Ready to play, Settings → Tools and This PC): a mark, the title (+ help) and
    detail, and the item's fix button inline on the right. Put it in a card(padding=Padding(S4, S2, S4, S2))."""
    rows = []
    for raw in items:
        item = raw if isinstance(raw, Check) else Check(*raw)
        icon, color = check_look(item.ok)
        mark = ft.Container(spinner() if icon is None else as_icon(icon, T.px(20), color), width=T.px(20),
                            alignment=ft.Alignment.CENTER)
        right: list[ft.Control] = []
        fix = check_fix(item)
        if fix:
            label, fix_icon, handler = fix
            right.append(secondary(label, fix_icon, lambda e, h=handler: h()))
        if item.extra is not None:
            right.append(item.extra)
        rows.append(ft.Container(ft.Row([
            mark,
            ft.Column([with_help(body(item.title, T.TEXT, weight=ft.FontWeight.W_500), item.help)]
                      + ([meta(item.detail)] if item.detail else []), spacing=1, expand=True),
            *right,
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=T.S3), padding=ft.Padding(0, T.px(6), 0, T.px(6))))
    return ft.Column(rows, spacing=0)


def art_fill(src: str | None, radius: int | None = None, placeholder_icon: str = ft.Icons.VIDEOGAME_ASSET_OUTLINED,
             hero: bool = False, **kw) -> ft.Container:
    """Artwork that covers its box (cards, heroes), by asset URL (artwork.thumbs.url — never image bytes: those are
    re-sent on every update); an accent gradient with a large faint icon when there's none."""
    radius = T.RADIUS if radius is None else radius
    if src:
        return ft.Container(image=ft.DecorationImage(src=src, fit=ft.BoxFit.COVER), border_radius=radius,
                            bgcolor=T.SURFACE_2, **kw)
    icon = as_icon(placeholder_icon, T.px(220) if hero else T.px(56), T.soft(T.ACCENT, 0.22 if hero else 0.45))
    return ft.Container(ft.Container(icon, padding=ft.Padding(0, 0, T.px(60), 0) if hero else 0), border_radius=radius,
                        alignment=ft.Alignment.CENTER_RIGHT if hero else ft.Alignment.CENTER,
                        gradient=ft.LinearGradient(begin=ft.Alignment.TOP_LEFT, end=ft.Alignment.BOTTOM_RIGHT,
                                                   colors=[T.SECONDARY_SOFT, T.SURFACE, T.ACCENT_SOFT] if T.DUAL
                                                   else [T.ACCENT_SOFT, T.SURFACE, T.BG]), **kw)


def bottom_fade(height: int | None = None, strength: float = 0.85) -> ft.Container:
    return ft.Container(height=height, border_radius=T.RADIUS,
                        gradient=ft.LinearGradient(begin=ft.Alignment.TOP_CENTER, end=ft.Alignment.BOTTOM_CENTER,
                                                   colors=[ft.Colors.TRANSPARENT, T.soft("#000000", strength)]))


class CoverButton:
    """The round button on a cover (Library cards, the On your Frame shelf): frosted glass with a thin light ring that
    springs in while the card is hovered (`show`) and fills with the accent under the pointer. A Play button
    (`launch` = the "Starting on …" text) launches with two portal rings rippling out of it, blue then orange, and
    swaps itself for a "Starting on Frame…" pill for STARTING_S seconds. `.control` goes where the button sits (in the
    card's hover layer); `.status` (the pill) goes in the card's Stack outside that layer, so it stays when the pointer
    leaves. Only properties change (no controls are created after construction)."""

    STARTING_S = 8

    def __init__(self, icon: str, tooltip: str, on_click: Callable, size: float, launch: str | None = None,
                 reduce_motion: bool = False):
        self.on_click, self.launch, self.reduce = on_click, launch, reduce_motion
        self.hovered = self.starting = False
        motion = (lambda ms, curve=ft.AnimationCurve.EASE_OUT: None if reduce_motion else ft.Animation(ms, curve))
        # (Material's play arrow is already drawn right of its box's centre for optical balance: no extra nudge — an
        # extra one put the triangle visibly off centre)
        self.icon = ft.Icon(icon, size=size * 0.52, color="#FFFFFF")
        self.circle = ft.Container(
            self.icon,
            width=size, height=size, border_radius=size / 2, alignment=ft.Alignment.CENTER,
            bgcolor=T.soft("#0B0C10", 0.45), border=ft.Border.all(1.5, T.soft("#FFFFFF", 0.6)),
            blur=ft.Blur(10, 10), shadow=self._shadow(False),
            animate=motion(180), scale=0.72, animate_scale=motion(320, ft.AnimationCurve.EASE_OUT_BACK),
            tooltip=ft.Tooltip(message=tooltip, wait_duration=800), on_hover=self._hover_button, on_click=self._click)
        self.rings = [ft.Container(width=size, height=size, border_radius=size / 2,
                                   border=ft.Border.all(2.5, color), opacity=0, scale=1,
                                   animate_opacity=ft.Animation(90), animate_scale=ft.Animation(1))
                      for color in (T.SECONDARY if T.DUAL else T.ACCENT, T.ACCENT)]
        self.control = ft.Stack([*self.rings, self.circle], width=size, height=size, alignment=ft.Alignment.CENTER,
                                clip_behavior=ft.ClipBehavior.NONE)
        self.label = ft.Text(launch or "", size=T.px(12), weight=ft.FontWeight.W_600, color="#FFFFFF")
        self.status = ft.Container(
            ft.Container(ft.Row([spinner("s", "#FFFFFF"), self.label], spacing=T.S2, tight=True),
                         bgcolor=T.soft("#0B0C10", 0.6), border=ft.Border.all(1, T.soft(T.ACCENT, 0.7)),
                         border_radius=T.px(999), blur=ft.Blur(10, 10),
                         padding=ft.Padding(T.px(12), T.px(7), T.px(14), T.px(7))),
            alignment=ft.Alignment.CENTER, opacity=0, animate_opacity=motion(220), visible=False)

    @staticmethod
    def _shadow(hot: bool):
        if hot:  # the accent glows under the pointer
            return [ft.BoxShadow(blur_radius=26, spread_radius=1, color=T.soft(T.ACCENT, 0.55)),
                    ft.BoxShadow(blur_radius=18, color=T.soft("#000000", 0.5), offset=ft.Offset(0, 6))]
        return ft.BoxShadow(blur_radius=22, color=T.soft("#000000", 0.55), offset=ft.Offset(0, 6))

    def _paint(self, hot: bool) -> None:
        c = self.circle
        c.bgcolor = T.ACCENT if hot else T.soft("#0B0C10", 0.45)
        c.border = ft.Border.all(1.5, T.ACCENT if hot else T.soft("#FFFFFF", 0.6))
        c.shadow = self._shadow(hot)
        self.icon.color = T.ON_ACCENT if hot else "#FFFFFF"

    def show(self, on: bool) -> None:
        """The card is (not) hovered: spring in / shrink back (the card fades its hover layer itself)."""
        self.hovered = on
        self.circle.scale = 0 if self.starting else (1.0 if on else 0.72)
        if not on:
            self._paint(False)

    def _hover_button(self, e) -> None:
        hot = e.data in (True, "true") and not self.starting
        self._paint(hot)
        self.circle.scale = 0 if self.starting else (1.08 if hot else 1.0)
        update(self.circle)

    def _click(self, e) -> None:
        if self.starting:
            return
        self.on_click(e)
        try:
            page = e.control.page
        except Exception:  # noqa: BLE001 - unmounted meanwhile (Flet 1.0 raises): no animation then
            page = None
        if page is not None:
            page.run_task(self._launch if self.launch else self._pop)

    async def _pop(self) -> None:
        import asyncio

        self.circle.scale = 0.9
        update(self.circle)
        await asyncio.sleep(0.12)
        self.circle.scale = 1.0 if self.hovered else 0.72
        update(self.circle)

    async def _launch(self) -> None:
        """Rings out (blue, then orange), the button shrinks away, "Starting on …" for STARTING_S seconds."""
        import asyncio

        self.starting = True
        self._paint(False)
        self.circle.scale = 0
        self.status.visible = True
        if self.reduce:
            self.status.opacity = 1
        else:
            for r in self.rings:
                r.opacity = 0.9
        update(self.control, self.status)
        if not self.reduce:
            await asyncio.sleep(0.09)
            for i, r in enumerate(self.rings):
                r.animate_scale = ft.Animation(720, ft.AnimationCurve.EASE_OUT)
                r.animate_opacity = ft.Animation(720, ft.AnimationCurve.EASE_IN)
                r.scale, r.opacity = 2.5, 0
                update(r)
                if i == 0:
                    await asyncio.sleep(0.16)
            await asyncio.sleep(0.2)  # the button is gone, the rings are out: then the pill
            self.status.opacity = 1
            update(self.status)
            await asyncio.sleep(0.6)
            for r in self.rings:  # back in place, unseen
                r.animate_scale, r.animate_opacity, r.scale = ft.Animation(1), ft.Animation(90), 1
            update(*self.rings)
        await asyncio.sleep(self.STARTING_S)
        self.starting = False
        self.status.opacity = 0
        self.circle.scale = 1.0 if self.hovered else 0.72
        update(self.status, self.circle)
        await asyncio.sleep(0.25)
        self.status.visible = False
        update(self.status)


DIALOG_TITLE_SIZE = 20  # px at 100 %


def dialog(title: str | ft.Control | None, content: ft.Control | None, actions: list[ft.Control] | None = None,
           size: str = "m", modal: bool = False, on_dismiss: Callable | None = None,
           title_actions: list[ft.Control] | None = None, width: float | None = None, height: float | None = None,
           **kw) -> ft.AlertDialog:
    """The one dialog look: SURFACE_2, RADIUS corners, a semibold title, content `size` wide ("s"/"m"/"l" =
    DIALOG_S/M/L, or `width`; `height` fixes it), actions at the end (Cancel/Close first, then the action).
    title_actions go at the title's right (e.g. a counter or a close button)."""
    if isinstance(title, str):
        title = ft.Text(title, size=T.px(DIALOG_TITLE_SIZE), weight=ft.FontWeight.W_600, color=T.TEXT)
    if title is not None and title_actions:
        title = ft.Row([ft.Container(title, expand=True), *title_actions],
                       vertical_alignment=ft.CrossAxisAlignment.CENTER)
    width = width or {"s": T.DIALOG_S, "m": T.DIALOG_M, "l": T.DIALOG_L}[size]
    box = ft.Container(content, width=width, height=height) if content is not None else None
    return ft.AlertDialog(title=title, content=box,
                          actions=actions or [], modal=modal, on_dismiss=on_dismiss, bgcolor=T.SURFACE_2,
                          shape=_shape(T.RADIUS), actions_alignment=ft.MainAxisAlignment.END, **kw)


def dialog_open(page: ft.Page | None) -> bool:
    """Whether a dialog (or viewer/bottom sheet) is open: Flet 1.0's page.show_dialog keeps every shown dialog in
    page._dialogs until Flutter reports it dismissed, and pop_dialog sets open=False at once. Snack bars (toasts)
    travel the same way but don't take the keyboard, so they don't count. Without that list: assume none."""
    try:
        shown = page._dialogs.controls  # noqa: SLF001  (Flet has no public "is a dialog open")
    except Exception:  # noqa: BLE001
        return False
    return any(d.open and not isinstance(d, ft.SnackBar) for d in shown)


def viewer(content: ft.Control, on_dismiss: Callable | None = None, **kw) -> ft.AlertDialog:
    """A full-size image/screenshot viewer: no title, a dark backdrop around the picture."""
    return ft.AlertDialog(content=content, bgcolor=T.BG, content_padding=T.S3, shape=_shape(T.RADIUS),
                          on_dismiss=on_dismiss, **kw)


def confirm(page: ft.Page, heading: str, text: str, ok_label: str, on_ok: Callable[[], None],
            danger: bool = False, extra: ft.Control | None = None) -> None:
    """A yes/no dialog; `extra` (e.g. an option checkbox) goes below the text and is read by on_ok."""
    def go(e):
        page.pop_dialog()
        on_ok()
    content = body(text) if extra is None else ft.Column([body(text), extra], spacing=T.S3, tight=True)
    page.show_dialog(dialog(heading, content, [ghost(tr("Cancel"), on_click=lambda e: page.pop_dialog()),
                                               (_danger if danger else primary)(ok_label, on_click=go)],
                            size="s"))


def progress_bar(value: float | None = None, color: str | None = None) -> ft.ProgressBar:
    """Progress: blue in dual themes (something on its way through the portal), else the accent."""
    return ft.ProgressBar(value=value, color=color or (T.SECONDARY if T.DUAL else T.ACCENT), bgcolor=T.SURFACE_3,
                          border_radius=T.px(4), bar_height=T.px(6))


def _is_header(a) -> bool:
    from .menus import Header

    return isinstance(a, Header)


def menu_entries(actions: list) -> list:
    """The entries menu_items shows: a header is dropped when nothing follows it before the next divider/header,
    dividers are never first, last or doubled."""
    kept = []
    for i, a in enumerate(actions):
        if _is_header(a) and (i + 1 >= len(actions) or actions[i + 1] is None or _is_header(actions[i + 1])):
            continue
        kept.append(a)
    out: list = []
    for a in kept:
        if a is None and (not out or out[-1] is None):
            continue
        out.append(a)
    while out and out[-1] is None:
        out.pop()
    return out


def menu_items(actions: list) -> list[ft.PopupMenuItem]:
    """[(label, icon, handler) | menus.Header | None] → compact popup menu items (32 px rows). None becomes a divider
    (never first, last or doubled), a Header a small caps group heading (dropped when its group is empty); an item
    without a handler is shown disabled (e.g. "Protected by SteamOS")."""
    out: list[ft.PopupMenuItem] = []
    for a in menu_entries(actions):
        if a is None:
            out.append(ft.PopupMenuItem())
        elif _is_header(a):
            out.append(ft.PopupMenuItem(
                content=ft.Text(a.label.upper(), size=T.T_SMALL, color=T.TEXT_3, weight=ft.FontWeight.W_600,
                                font_family="monospace", style=ft.TextStyle(letter_spacing=T.px(1))),
                height=T.px(24), padding=ft.Padding(T.px(14), T.px(6), T.px(16), 0), disabled=True,
                mouse_cursor=ft.MouseCursor.BASIC))
        else:
            label, icon, handler = a
            color = T.TEXT if handler else T.TEXT_3
            out.append(ft.PopupMenuItem(
                content=ft.Text(label, size=T.T_BODY, color=color),
                icon=as_icon(icon, T.ICON_S, T.TEXT_2 if handler else T.TEXT_3) if icon else None,
                height=T.px(32), padding=ft.Padding(T.px(14), 0, T.px(18), 0), on_click=handler,
                disabled=handler is None))
    return out


def check_item(label: str, checked: bool, on_click: Callable) -> ft.PopupMenuItem:
    """A compact menu row with a check mark (filter/sort chips)."""
    return ft.PopupMenuItem(content=ft.Text(label, size=T.T_BODY, color=T.TEXT), checked=checked, on_click=on_click,
                            height=T.px(32), padding=ft.Padding(T.px(14), 0, T.px(18), 0))


def menu_button(items: list[ft.PopupMenuItem], **kw) -> ft.PopupMenuButton:
    """A PopupMenuButton with the shared menu look (border, shadow, SURFACE_2, small radius; also the theme's
    popup_menu_theme, which right-click ContextMenus use)."""
    kw.setdefault("tooltip", "")
    return ft.PopupMenuButton(items=items, bgcolor=T.SURFACE_2, elevation=8, shadow_color="#000000",
                              shape=menu_shape(), menu_padding=ft.Padding(0, T.px(4), 0, T.px(4)), **kw)


def menu_shape() -> ft.RoundedRectangleBorder:
    return ft.RoundedRectangleBorder(radius=T.RADIUS_SM, side=ft.BorderSide(1, T.BORDER_STRONG))


def menu_targets(clicked, selected) -> list:
    """What a right-click acts on: the whole selection when the clicked item is part of a selection of several (as in
    a file manager), else just that item. `selected` keeps its order."""
    selected = list(selected)
    return selected if clicked in selected and len(selected) > 1 else [clicked]


class DragSelect:
    """Click-and-drag multi-select ("paint" selection) for a grid or list, without knowing where items are on screen.

    The area's GestureDetector calls start() on pan start and end() on pan end; each item calls hover(key, inside)
    from its hover (enter/exit) event, which Flutter also sends while the mouse button is held. A drag selects every
    item it passes over, or deselects them when it started on a selected item. No Flet in here (tested directly)."""

    def __init__(self, is_selected, set_selected, can_select=lambda key: True, on_start=None):
        self.is_selected, self.set_selected, self.can_select = is_selected, set_selected, can_select
        self.on_start = on_start
        self.under = None      # the item the pointer is over
        self.active = False
        self.mode = True       # select (True) or deselect (False) during this drag
        self.touched: set = set()

    def hover(self, key, inside: bool) -> None:
        if inside:
            self.under = key
            if self.active:
                self._apply(key)
        elif self.under == key:
            self.under = None

    def start(self, e=None) -> None:
        if self.on_start:
            self.on_start()
        under = self.under if self.under is not None and self.can_select(self.under) else None
        self.mode = not self.is_selected(under) if under is not None else True
        self.active, self.touched = True, set()
        if under is not None:
            self._apply(under)

    def end(self, e=None) -> None:
        self.active = False

    def _apply(self, key) -> None:
        if key in self.touched or not self.can_select(key):
            return
        self.touched.add(key)
        if self.is_selected(key) != self.mode:
            self.set_selected(key, self.mode)


def is_media_player(g: dict) -> bool:
    """A video player (360° layers in its recipe or analysis, e.g. 4XVR): its page offers "Add videos" up front."""
    patches = ((g.get("recipe") or {}).get("patches") or {})
    exts = ((g.get("analysis") or {}).get("extra") or {}).get("xr_layer_exts") or []
    return "adapter.equirect_emul" in patches or any(e.startswith("XR_KHR_composition_layer_equirect") for e in exts)


# ------------------------------------------------------------------------------------------ charts
def spark_points(values: list, width: float, height: float, lo: float = 0.0, hi: float | None = None,
                 slots: int | None = None, pad: float = 2.0) -> list[list[tuple[float, float]]]:
    """Runs of (x, y) points for a sparkline: the newest value at the right edge, one slot per value (`slots` fixes
    the spacing so a young series grows from the right), None = a gap. y grows downwards (canvas coordinates)."""
    n = slots or len(values)
    if n < 2 or width <= 0 or height <= 0:
        return []
    real = [v for v in values if v is not None]
    if hi is None:
        hi = max(real, default=lo + 1.0)
    span = (hi - lo) or 1.0
    step = width / (n - 1)
    x0 = width - step * (len(values) - 1)
    runs, run = [], []
    for i, v in enumerate(values):
        if v is None:
            if run:
                runs.append(run)
            run = []
            continue
        frac = min(1.0, max(0.0, (v - lo) / span))
        run.append((x0 + i * step, pad + (height - 2 * pad) * (1.0 - frac)))
    if run:
        runs.append(run)
    return runs


class Sparkline:
    """A small line chart with a soft area fill (Flet canvas, built into Flet: no charts extension). Created once;
    set() replaces only the paths' elements, so a tick sends a small patch. Fills its width (on_resize)."""

    def __init__(self, color: str | None = None, height: int = 36, slots: int = 120, lo: float = 0.0,
                 hi: float | None = None, min_slots: int = 30, fit: float | None = None):
        import flet.canvas as cv

        color = color or T.ACCENT
        self.cv, self.slots, self.min_slots, self.lo, self.hi = cv, slots, min_slots, lo, hi
        self.fit = fit  # zoom to the data: a scale of at least this span around it (temperatures, fan speed)
        self.width, self.height = 0.0, T.px(height)
        self.values: list = []
        self.target: float | None = None
        self.area = cv.Path([], paint=ft.Paint(color=T.soft(color, 0.16), style=ft.PaintingStyle.FILL))
        self.line = cv.Path([], paint=ft.Paint(color=color, stroke_width=T.px(1.6), style=ft.PaintingStyle.STROKE,
                                               stroke_join=ft.StrokeJoin.ROUND, stroke_cap=ft.StrokeCap.ROUND))
        self.guide = cv.Path([], paint=ft.Paint(color=T.soft(T.TEXT_3, 0.6), stroke_width=1,
                                                style=ft.PaintingStyle.STROKE, stroke_dash_pattern=[3, 3]))
        self.control = cv.Canvas([self.area, self.guide, self.line], height=self.height, expand=True,
                                 on_resize=self._resized)

    def _resized(self, e) -> None:
        self.width, self.height = e.width, e.height or self.height
        self._draw()
        update(self.control)

    def set_color(self, color: str) -> None:
        self.line.paint.color = color
        self.area.paint.color = T.soft(color, 0.16)

    def set(self, values: list, hi: float | None = None, target: float | None = None) -> None:
        """New values (oldest first); hi overrides the top of the scale; target draws a dashed guide (e.g. 72 fps)."""
        self.values, self.target = list(values)[-self.slots:], target
        if hi is not None:
            self.hi = hi
        self._draw()

    def _draw(self) -> None:
        cv, w, h = self.cv, self.width, self.height
        lo, hi = self.lo, self.hi
        real = [v for v in self.values if v is not None]
        if self.fit is not None and real:
            mid, span = (max(real) + min(real)) / 2, max(self.fit, (max(real) - min(real)) * 1.3)
            lo, hi = mid - span / 2, mid + span / 2
        elif hi is None:
            hi = max(real + [self.target or 0, self.lo + 1.0]) * 1.1
        # the time window starts at min_slots points and widens to `slots` as data comes in (a new chart isn't a
        # sliver at the right edge for its first minute)
        runs = spark_points(self.values, w, h, lo, hi, max(self.min_slots, min(self.slots, len(self.values))))
        line, area = [], []
        for run in runs:
            line.append(cv.Path.MoveTo(*run[0]))
            line += [cv.Path.LineTo(x, y) for x, y in run[1:]]
            area += [cv.Path.MoveTo(run[0][0], h), *[cv.Path.LineTo(x, y) for x, y in run],
                     cv.Path.LineTo(run[-1][0], h), cv.Path.Close()]
        self.line.elements, self.area.elements = line, area
        guide = []
        if self.target is not None and w > 0:
            y = spark_points([self.target, self.target], w, h, lo, hi)[0][0][1]
            guide = [cv.Path.MoveTo(0, y), cv.Path.LineTo(w, y)]
        self.guide.elements = guide


def _darker(color: str, factor: float = 0.6) -> str:
    c = color.lstrip("#")
    return "#" + "".join(f"{round(int(c[i:i + 2], 16) * factor):02X}" for i in (0, 2, 4))


def transit_parts() -> dict[str, str]:
    """The logo's monitor, portal and headset in the active theme's colours (asset URLs; glyphs.transit_parts)."""
    from ..artwork.thumbs import asset_url
    from ..core.paths import user_data_dir

    parts = glyphs.transit_parts(user_data_dir(), {
        "line": T.TEXT, "body": T.BG, "screen": T.SECONDARY, "lens": T.ACCENT,
        "near": T.SECONDARY if T.DUAL else T.ACCENT, "far": T.ACCENT, "hole": _darker(T.BG)})
    return {k: asset_url(v) for k, v in parts.items()}


class Transit:
    """Install progress as a transit: the PC's monitor · a track with the portal standing at the PC/Frame boundary
    (transit.PORTAL_AT): the game's cover approaches it while the game is prepared on the PC, passes through it
    (over the rim's PC half, under its Frame half) during the upload and rides on to the Frame headset (a monitor
    again for installs on this PC) while it installs and is tested. Below: "Title → Frame" with what happens now
    (heading=False: without "Title → Frame"), and the six steps (not when compact). Created once per place; set()
    changes only properties, so it can follow every progress tick."""

    def __init__(self, compact: bool = False, heading: bool = True):
        from .transit import STEP_WEIGHTS, STEPS

        self.compact = compact
        heading = heading and not compact
        self.width = 0.0
        self.frac = 0.0
        self.done = False
        self._job_id = None
        self._mono = None
        icon_h = T.px(22 if compact else 30)
        self.track_h = T.px(32 if compact else 48)  # = the portal's height: taller than the cover going through it
        self.bar_h = T.px(4 if compact else 6)
        self.cover_s = T.px(22 if compact else 36)
        self.portal_w = self.track_h * glyphs.PORTAL_RING[0] / glyphs.PORTAL_RING[1]  # the ring's width
        self.parts = transit_parts()
        size = lambda name, h: (h * glyphs.TRANSIT_SIZES[name][0] / glyphs.TRANSIT_SIZES[name][1], h)  # noqa: E731
        mw, mh = size("monitor", icon_h)
        self._dest_sizes = {"frame": size("headset", icon_h * 0.86), "pc": (mw, mh)}
        self.source = ft.Image(src=self.parts["monitor"], width=mw, height=mh, fit=ft.BoxFit.CONTAIN)
        hw, hh = self._dest_sizes["frame"]
        self.dest = ft.Image(src=self.parts["headset"], width=hw, height=hh, fit=ft.BoxFit.CONTAIN)
        anim = ft.Animation(250, ft.AnimationCurve.EASE_OUT)
        top = (self.track_h - self.bar_h) / 2
        self.fill = ft.Container(left=0, top=top, height=self.bar_h, width=0, border_radius=self.bar_h,
                                 gradient=portal_gradient() if T.DUAL else None,
                                 bgcolor=None if T.DUAL else T.ACCENT, animate_size=anim)
        self.cover = ft.Container(left=0, top=(self.track_h - self.cover_s) / 2, width=self.cover_s,
                                  height=self.cover_s, border_radius=T.px(4), bgcolor=T.SURFACE_3,
                                  border=ft.Border.all(1, T.BORDER_STRONG), alignment=ft.Alignment.CENTER,
                                  clip_behavior=ft.ClipBehavior.ANTI_ALIAS, animate_position=anim)
        # the portal in two layers around the cover, so the cover goes through it: the back (shadow, hole, the rim's
        # PC/blue half) under the cover, the rim's Frame/orange half over it. Same viewBox: they line up exactly; the
        # pictures are a bit larger than the ring (its shadow reaches a little below the track)
        img_w, img_h = (n * self.track_h / glyphs.PORTAL_RING[1] for n in glyphs.TRANSIT_SIZES["portal"])
        self.portal_layers = [ft.Image(src=self.parts[k], left=0, top=0, width=img_w, height=img_h,
                                       fit=ft.BoxFit.CONTAIN, animate_opacity=anim)
                              for k in ("portal_back", "portal_front")]
        base = ft.Container(left=0, right=0, top=top, height=self.bar_h, border_radius=self.bar_h,
                            bgcolor=T.SURFACE_3)
        back, front = self.portal_layers
        self.track = ft.Container(ft.Stack([base, self.fill, back, self.cover, front], height=self.track_h,
                                           clip_behavior=ft.ClipBehavior.NONE),
                                  expand=True, height=self.track_h, on_size_change=self._resized)
        row = ft.Row([self.source, self.track, self.dest], spacing=T.px(6 if compact else 10),
                     vertical_alignment=ft.CrossAxisAlignment.CENTER)
        self.heading = ft.Text("", size=T.T_META, weight=ft.FontWeight.W_600, color=T.TEXT, max_lines=1,
                               overflow=ft.TextOverflow.ELLIPSIS, expand=True)
        self.detail = ft.Text("", size=T.T_META, color=T.TEXT_2, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                              expand=not heading, text_align=ft.TextAlign.RIGHT if heading else ft.TextAlign.LEFT)
        info = ft.Row([self.heading, self.detail] if heading else [self.detail], spacing=T.S2,
                      vertical_alignment=ft.CrossAxisAlignment.CENTER)
        labels = {"analyze": tr("Analyze"), "patch": tr("Patch"), "sign": tr("Sign"), "upload": tr("Upload"),
                  "install": tr("Install"), "test": tr("Test")}
        self.steps = [ft.Container(ft.Text(labels[k], size=T.T_SMALL, color=T.TEXT_3, max_lines=1, no_wrap=True,
                                            text_align=ft.TextAlign.CENTER),
                                    expand=w, alignment=ft.Alignment.CENTER,
                                    padding=ft.Padding(0, 0, 0, T.px(3)),
                                    border=ft.Border(bottom=ft.BorderSide(2, T.SURFACE_3)))
                      for k, w in zip(STEPS, STEP_WEIGHTS, strict=True)]
        steps_row = ft.Row(self.steps, spacing=T.px(3), visible=not compact)
        self.control = ft.Column([row, info, steps_row], spacing=T.px(4 if compact else 6), tight=True)

    # ------------------------------------------------------------------ layout
    def _resized(self, e) -> None:
        if e.width and abs(e.width - self.width) > 0.5:
            self.width = e.width
            self._place()
            update(self.track)

    def _place(self) -> None:
        """The portal stands at PORTAL_AT; the cover's centre follows transit.position(): from the start to just
        before the portal (Analyze/Patch/Sign), through it (Upload), just after it to the end (Install/Test).
        The fill reaches the cover's centre."""
        from .transit import CROSSING, PORTAL_AT, position

        w, pw, cs, gap = self.width, self.portal_w, self.cover_s, T.px(2)
        if w <= 0:
            return
        mid = PORTAL_AT * w
        for layer in self.portal_layers:  # the ring's box: its left edge is the picture's
            layer.left = mid - pw / 2
        # pixel anchors of the cover's centre: start · touching the portal's near side · its far side · end
        start, end = cs / 2, w - cs / 2
        near, far = max(start, mid - pw / 2 - gap - cs / 2), min(end, mid + pw / 2 + gap + cs / 2)
        p = 1.0 if self.done else position(self.frac)
        before, after = PORTAL_AT - CROSSING, PORTAL_AT + CROSSING
        if p <= before:
            x = start + p / before * (near - start)
        elif p <= after:
            x = near + (p - before) / (after - before) * (far - near)
        else:
            x = far + (p - after) / (1.0 - after) * (end - far)
        self.cover.left = x - cs / 2
        self.fill.width = w if self.done else (x if p > 0 else 0.0)

    # ------------------------------------------------------------------ state
    def set(self, job, title: str = "", dest: str = "", art: str | None = None, icon=None, extra: str = "") -> None:
        """Show `job` (ui.jobs.Job). title/dest: "Title → Frame"; art: the cover's asset URL (icon when none);
        extra: appended to the detail (e.g. " · 2 more queued")."""
        from .transit import WAITING, Monotonic, job_state

        self._mono = self._mono or Monotonic()
        s = self._mono.follow(job.id, job_state(job))
        if job.id != self._job_id:  # another job: its cover and destination
            self._job_id = job.id
            self.cover.image = ft.DecorationImage(src=art, fit=ft.BoxFit.COVER) if art else None
            self.cover.content = None if art else as_icon(icon or ft.Icons.VIDEOGAME_ASSET_OUTLINED,
                                                          self.cover_s * 0.66, T.TEXT_2)
            self.dest.src = self.parts["monitor" if s.target == "pc" else "headset"]
            self.dest.width, self.dest.height = self._dest_sizes[s.target]
        self.frac, self.done = s.fraction, s.label == "done"
        self.heading.value = f"{title} → {dest}" if dest else title
        if s.waiting:
            text, color = tr(WAITING), T.WARN
        elif s.failed:
            text, color = (job.error or tr("Failed")), T.ERROR
        elif job.state == "queued":
            text, color = tr("Queued"), T.TEXT_3
        else:
            text, color = (s.detail or tr("Starting…")), T.TEXT_2
        self.detail.value, self.detail.color = text + extra, color
        for layer in self.portal_layers:
            layer.opacity = 0.35 if s.waiting else 1.0
        if s.failed:
            self.fill.gradient, self.fill.bgcolor = None, T.ERROR
        else:
            self.fill.gradient = portal_gradient() if T.DUAL else None
            self.fill.bgcolor = None if T.DUAL else T.ACCENT
        for i, box in enumerate(self.steps):
            text = box.content
            current = i == s.step and not self.done
            finished = i < s.step or self.done
            text.color = T.ACCENT if current else T.TEXT_2 if finished else T.TEXT_3
            text.weight = ft.FontWeight.W_600 if current else ft.FontWeight.W_400
            box.border = ft.Border(bottom=ft.BorderSide(
                2, (T.ERROR if s.failed else T.WARN if s.waiting else T.ACCENT) if current
                else (T.SECONDARY if T.DUAL else T.ACCENT) if finished else T.SURFACE_3))
        self._place()


class MeterBar:
    """A thin horizontal bar of coloured segments (per-core load, power split); widths are fractions of the bar."""

    def __init__(self, colors: list[str], height: int = 6):
        self.segments = [ft.Container(bgcolor=c, expand=0, height=T.px(height)) for c in colors]
        self.rest = ft.Container(expand=1000, height=T.px(height))
        self.control = ft.Container(ft.Row([*self.segments, self.rest], spacing=0), bgcolor=T.SURFACE_3,
                                    border_radius=T.px(height), height=T.px(height),
                                    clip_behavior=ft.ClipBehavior.HARD_EDGE)

    def set(self, fractions: list[float]) -> None:
        """Fractions (0..1) per segment; the rest of the bar stays empty. Expand weights are integers (per mille)."""
        total = 0
        for seg, f in zip(self.segments, fractions, strict=False):
            seg.expand = max(0, int(round(min(1.0, max(0.0, f)) * 1000)))
            seg.visible = seg.expand > 0
            total += seg.expand
        self.rest.expand = max(1, 1000 - total)


class LoopUpdater:
    """update() for controls changed on a background thread that Flet doesn't know (e.g. an SSH reader): the patch is
    sent from Flet's event loop. Flet 1.0's desktop connection queues outgoing messages on an asyncio.Queue with
    put_nowait, which from a foreign thread doesn't wake the loop: the window only showed such updates after the next
    UI event (the Monitor stayed empty until a click, its charts jumped in bursts). Updates requested while one is
    pending are merged into it, so a fast stream can't pile up work on the loop."""

    def __init__(self, page: ft.Page):
        import threading

        self.page = page
        self._lock = threading.Lock()
        self._pending: dict[int, ft.Control] = {}
        self._scheduled = False

    def __call__(self, *controls: ft.Control) -> None:
        with self._lock:
            for c in controls:
                self._pending[id(c)] = c
            if self._scheduled:
                return
            self._scheduled = True
        try:
            self.page.run_task(self._flush)
        except Exception:  # noqa: BLE001 - no event loop (tests, shutting down): update right here
            self._flush_now()

    async def _flush(self) -> None:
        self._flush_now()

    def _flush_now(self) -> None:
        with self._lock:
            items = list(self._pending.values())
            self._pending.clear()
            self._scheduled = False
        update(*items)
