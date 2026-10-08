"""Design tokens for the FramePort GUI (dark only, the owner's choice): colour themes (an accent, a neutral surface
ramp, semantic state colours), an 8-pt spacing scale and a small type scale. Every view takes its colours and sizes
from here, reading them as `T.NAME` when it builds (never as a default argument or a module-level constant: those keep
the colours of the theme that was active at import; module-level maps re-fill themselves through on_change()).

Themes: the built-ins below plus theme files the user installs (<data>/themes/*.json, docs/THEMES.md)."""
from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import flet as ft

# Colour tokens of a theme:
#   surfaces: BG window, SIDEBAR, SURFACE cards, SURFACE_2 raised/hover, SURFACE_3 inputs/chips, BORDER(_STRONG)
#   text: TEXT, TEXT_2 secondary, TEXT_3 meta/disabled
#   ACCENT (+ ACCENT_SOFT tint, ON_ACCENT text on it): the main action and the current selection
#   SECONDARY (+ SECONDARY_SOFT): secondary actions, progress, switch tracks (with "dual" also the two-colour touches)
#   states OK/WARN/ERROR/INFO, PC = PC VR / "on this PC"
TOKENS = ("BG", "SIDEBAR", "SURFACE", "SURFACE_2", "SURFACE_3", "BORDER", "BORDER_STRONG", "TEXT", "TEXT_2", "TEXT_3",
          "ACCENT", "ACCENT_SOFT", "ON_ACCENT", "SECONDARY", "SECONDARY_SOFT", "OK", "WARN", "ERROR", "INFO", "PC")

# "portal" follows the logo: orange (the Frame's side) = actions and selection, blue (the PC's side) = secondary
# actions, progress and the PC; "dual" adds the blue→orange touches (sidebar edge, wordmark, selected tab).
# Status colours are picked so a warning never reads as the accent.
_PORTAL = {
    "BG": "#0D0E12", "SIDEBAR": "#111217", "SURFACE": "#16181E", "SURFACE_2": "#1D1F27", "SURFACE_3": "#252832",
    "BORDER": "#2A2D37", "BORDER_STRONG": "#393D49",
    "TEXT": "#EDEEF2", "TEXT_2": "#A7ABB7", "TEXT_3": "#717583",
    "ACCENT": "#FF8A1F", "ACCENT_SOFT": "#3A2512", "ON_ACCENT": "#160C03",
    "SECONDARY": "#3AA8FF", "SECONDARY_SOFT": "#0F2A40",
    # INFO is a cyan of its own: next to PC (the portal blue) it tells data series apart (Monitor: Steam vs
    # SteamVR processes, CPU vs memory) and stays clear of OK's green
    "OK": "#3DD68C", "WARN": "#F2C94C", "ERROR": "#FF6166", "INFO": "#5CD0E6", "PC": "#3AA8FF",
}
BUILTIN: dict[str, dict] = {
    "portal": {"name": "Portal", "dual": True, "colors": _PORTAL},
    "portal_oled": {"name": "Portal (OLED)", "dual": True, "colors": {
        **_PORTAL,  # true black behind everything; cards just lift off it
        "BG": "#000000", "SIDEBAR": "#000000", "SURFACE": "#0A0A0D", "SURFACE_2": "#121317", "SURFACE_3": "#1A1B21",
        "BORDER": "#1E1F26", "BORDER_STRONG": "#2B2D36", "ACCENT_SOFT": "#2B1A0A", "SECONDARY_SOFT": "#0A1F31"}},
    "original": {"name": "Original", "dual": False, "colors": {  # FramePort's colours before the Portal theme
        "BG": "#0E0F13", "SIDEBAR": "#121319", "SURFACE": "#171920", "SURFACE_2": "#1E2029", "SURFACE_3": "#262935",
        "BORDER": "#2C2F3B", "BORDER_STRONG": "#3A3E4D",
        "TEXT": "#ECEDF3", "TEXT_2": "#A9ADBD", "TEXT_3": "#737889",
        "ACCENT": "#8B7CFF", "ACCENT_SOFT": "#2A2550", "ON_ACCENT": "#0E0F13",
        "SECONDARY": "#38BDF8", "SECONDARY_SOFT": "#12303F",
        "OK": "#4ADE80", "WARN": "#FBBF24", "ERROR": "#F87171", "INFO": "#60A5FA", "PC": "#38BDF8"}},
}
ALIASES = {"classic": "original"}  # an earlier name
USER_PREFIX = "user:"
THEMES: dict[str, dict] = dict(BUILTIN)  # + installed theme files (load_user_themes)
DEFAULT_THEME = "portal"
THEME = DEFAULT_THEME
DUAL = True
_listeners: list[Callable[[], None]] = []

# the active theme's tokens as module globals (set_theme() replaces them); declared for linters and readers
BG = SIDEBAR = SURFACE = SURFACE_2 = SURFACE_3 = BORDER = BORDER_STRONG = ""
TEXT = TEXT_2 = TEXT_3 = ACCENT = ACCENT_SOFT = ON_ACCENT = SECONDARY = SECONDARY_SOFT = ""
OK = WARN = ERROR = INFO = PC = ""


def theme_from_setting(value) -> str:
    """Library setting ui.theme → a known theme id (unknown/missing/removed → the default)."""
    value = ALIASES.get(value, value)
    return value if value in THEMES else DEFAULT_THEME


def on_change(callback: Callable[[], None]) -> None:
    """Call `callback` after every set_theme() (and once now): for module-level maps that hold colours."""
    _listeners.append(callback)
    callback()


def set_theme(name: str) -> None:
    """Switch the colour tokens. Views built afterwards use the new colours (app.restyle() rebuilds the open ones)."""
    global THEME, DUAL
    THEME = theme_from_setting(name)
    DUAL = bool(THEMES[THEME]["dual"])
    globals().update(THEMES[THEME]["colors"])
    for callback in list(_listeners):
        callback()


# ------------------------------------------------------------------------------------------ theme files
class ThemeError(ValueError):
    """A theme file FramePort can't use; the message says why, in plain words."""


_HEX = re.compile(r"#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})")


def _hex(value, key: str) -> str:
    m = _HEX.fullmatch(str(value).strip()) if isinstance(value, str) else None
    if not m:
        raise ThemeError(f"{key}: \"{value}\" is not a color like #FF8A1F")
    h = m.group(1)
    return "#" + (h if len(h) == 6 else "".join(c * 2 for c in h)).upper()


def _luminance(color: str) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def parse_theme(data: dict, fallback_name: str = "Custom theme") -> dict:
    """A theme file's content → a theme ({"name", "dual", "colors"}); raises ThemeError. Colours it leaves out come
    from its "base" built-in (default Portal); keys may be any case."""
    if not isinstance(data, dict):
        raise ThemeError("the file isn't a theme (expected a JSON object with \"colors\")")
    unknown = set(data) - {"name", "base", "dual", "colors", "description", "author", "version"}
    if unknown:
        raise ThemeError(f"unknown field(s): {', '.join(sorted(unknown))}")
    base_id = ALIASES.get(data.get("base") or DEFAULT_THEME, data.get("base") or DEFAULT_THEME)
    if base_id not in BUILTIN:
        raise ThemeError(f"base \"{data.get('base')}\" isn't a built-in theme ({', '.join(BUILTIN)})")
    base = BUILTIN[base_id]
    raw = data.get("colors") or {}
    if not isinstance(raw, dict):
        raise ThemeError("\"colors\" must be an object of TOKEN: \"#RRGGBB\"")
    colors = dict(base["colors"])
    for key, value in raw.items():
        token = str(key).upper()
        if token not in TOKENS:
            raise ThemeError(f"unknown color \"{key}\" (known: {', '.join(TOKENS)})")
        colors[token] = _hex(value, token)
    # FramePort is dark only: a light window would leave Material's own parts (menus, inputs) unreadable
    if _luminance(colors["BG"]) > 0.05:
        raise ThemeError(f"BG {colors['BG']} is too light: FramePort themes are dark")
    if contrast(colors["TEXT"], colors["SURFACE"]) < 4.5:
        raise ThemeError(f"TEXT {colors['TEXT']} is hard to read on SURFACE {colors['SURFACE']}")
    name = str(data.get("name") or fallback_name).strip()[:40] or fallback_name
    dual = data.get("dual", base["dual"])
    if not isinstance(dual, bool):
        raise ThemeError("\"dual\" must be true or false")
    return {"name": name, "dual": dual, "colors": colors}


def read_theme_file(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ThemeError(f"can't read the file ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise ThemeError(f"not valid JSON (line {exc.lineno}: {exc.msg})") from exc
    return parse_theme(data, Path(path).stem.replace("_", " ").replace("-", " ").title())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "theme"


def load_user_themes(folder: Path) -> list[str]:
    """(Re)load the installed theme files into THEMES. Returns problems with files that were skipped."""
    for key in [k for k in THEMES if k.startswith(USER_PREFIX)]:
        del THEMES[key]
    problems = []
    for f in sorted(Path(folder).glob("*.json")) if Path(folder).is_dir() else []:
        try:
            THEMES[USER_PREFIX + f.stem] = {**read_theme_file(f), "path": str(f)}
        except ThemeError as exc:
            problems.append(f"{f.name}: {exc}")
    return problems


def install_theme(source: Path, folder: Path) -> str:
    """Check a theme file and copy it into the themes folder (named after the theme). Returns its theme id."""
    theme = read_theme_file(source)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{_slug(theme['name'])}.json"
    if Path(source).resolve() != dest.resolve():
        shutil.copyfile(source, dest)
    tid = USER_PREFIX + dest.stem
    THEMES[tid] = {**theme, "path": str(dest)}
    return tid


def remove_theme(theme_id: str) -> None:
    """Delete an installed theme file (built-ins can't be removed)."""
    theme = THEMES.get(theme_id)
    if not theme_id.startswith(USER_PREFIX) or not theme:
        raise ThemeError("only installed themes can be removed")
    Path(theme["path"]).unlink(missing_ok=True)
    del THEMES[theme_id]


def theme_json(theme_id: str) -> str:
    """A theme as a theme file (a starting point for your own)."""
    t = THEMES[theme_id]
    base = theme_id if theme_id in BUILTIN else DEFAULT_THEME
    return json.dumps({"name": f"My {t['name']}", "base": base, "dual": t["dual"], "colors": t["colors"]}, indent=2)


set_theme(DEFAULT_THEME)

# spacing / shape / type (at 100 %; set_scale() multiplies them)
# RADIUS cards/dialogs, RADIUS_SM buttons/inputs/thumbnails, RADIUS_XS small chips/tooltips; ICON_S/M/L icon sizes;
# DIALOG_S/M/L dialog content widths; T_DISPLAY big headings (empty states, setup)
_BASE = {"S1": 4, "S2": 8, "S3": 12, "S4": 16, "S5": 24, "S6": 32, "RADIUS": 12, "RADIUS_SM": 8, "RADIUS_XS": 6,
         "ICON_S": 16, "ICON_M": 20, "ICON_L": 24, "DIALOG_S": 440, "DIALOG_M": 600, "DIALOG_L": 880,
         "T_DISPLAY": 22, "T_TITLE": 28, "T_H2": 16, "T_BODY": 13, "T_META": 12, "T_SMALL": 11}
S1, S2, S3, S4, S5, S6 = 4, 8, 12, 16, 24, 32
RADIUS = 12
RADIUS_SM = 8
RADIUS_XS = 6
ICON_S, ICON_M, ICON_L = 16, 20, 24
DIALOG_S, DIALOG_M, DIALOG_L = 440, 600, 880
T_DISPLAY, T_TITLE, T_H2, T_BODY, T_META, T_SMALL = 22, 28, 16, 13, 12, 11
SCALE = 1.0


def set_scale(factor: float) -> None:
    """UI scale (1.0 = 100 %): every size token and every px() value. Call before the views are built."""
    global SCALE
    SCALE = max(0.75, min(3.0, float(factor or 1.0)))
    for name, value in _BASE.items():
        globals()[name] = round(value * SCALE)


SCALE_CHOICES = (1.0, 1.25, 1.5, 1.75, 2.0)


def detect_scale() -> float:
    """The display scaling the GUI should use by itself. Native Windows/macOS: 1.0 (Flutter applies the OS scaling).
    Under WSL the Linux window gets none of Windows' scaling, so follow Windows' setting halfway (AppliedDPI 144 =
    150 % -> 125 %); plain Linux: GDK_SCALE if set."""
    import os

    from ..core import winhost

    if winhost.is_wsl():
        try:
            dpi = int(winhost.reg_query(r"HKCU\Control Panel\Desktop\WindowMetrics", "AppliedDPI") or 0, 0)
        except (ValueError, TypeError, OSError):
            dpi = 0
        # half of Windows' extra scaling (150 % -> 125 %): the full factor made the app too large
        return 1.0 + (dpi / 96 - 1.0) / 2 if dpi > 96 else 1.0
    if not winhost.is_windows() and os.environ.get("GDK_SCALE", "").isdigit():
        return 1.0  # GTK already scales the window by GDK_SCALE
    return 1.0


def scale_from_setting(value) -> float:
    """Library setting ui.scale: "auto" (or missing) → detect_scale(), else a factor like 1.5."""
    if value in (None, "", "auto"):
        return detect_scale()
    try:
        return float(value)
    except (TypeError, ValueError):
        return detect_scale()


def px(value: float) -> float:
    """A size in logical pixels at 100 %, scaled to the current UI scale."""
    return round(value * SCALE) if isinstance(value, int) else value * SCALE


def soft(color: str, opacity: float = 0.14) -> str:
    return ft.Colors.with_opacity(opacity, color)


def apply(page: ft.Page) -> None:
    scheme = ft.ColorScheme(
        primary=ACCENT, on_primary=ON_ACCENT, primary_container=ACCENT_SOFT, on_primary_container=TEXT,
        secondary=SECONDARY, on_secondary=ON_ACCENT, surface=SURFACE, on_surface=TEXT, on_surface_variant=TEXT_2,
        surface_container_lowest=BG, surface_container_low=SIDEBAR, surface_container=SURFACE,
        surface_container_high=SURFACE_2, surface_container_highest=SURFACE_3, outline=BORDER_STRONG,
        outline_variant=BORDER, error=ERROR, on_error=ON_ACCENT,
    )
    # Material's default type scale (buttons = label_large, inputs/menus = body_large …) at the UI scale, for every
    # control that doesn't set its own size
    sizes = {"display_large": 57, "display_medium": 45, "display_small": 36, "headline_large": 32,
             "headline_medium": 28, "headline_small": 24, "title_large": 22, "title_medium": 16, "title_small": 14,
             "body_large": 16, "body_medium": 14, "body_small": 12, "label_large": 14, "label_medium": 12,
             "label_small": 11}
    # with a colour: without one Flutter draws these styles dark (e.g. radio/checkbox labels, dialog titles)
    text_theme = ft.TextTheme(**{k: ft.TextStyle(size=px(v), color=TEXT) for k, v in sizes.items()})
    theme = ft.Theme(color_scheme=scheme, visual_density=ft.VisualDensity.COMFORTABLE, use_material3=True,
                     text_theme=text_theme,
                     divider_theme=ft.DividerTheme(color=BORDER, thickness=1, space=1),
                     tooltip_theme=ft.TooltipTheme(
                         decoration=ft.BoxDecoration(bgcolor=SURFACE_3, border_radius=RADIUS_XS,
                                                     border=ft.Border.all(1, BORDER)),
                         text_style=ft.TextStyle(color=TEXT, size=px(12)), wait_duration=400),
                     expansion_tile_theme=ft.ExpansionTileTheme(
                         shape=ft.RoundedRectangleBorder(radius=RADIUS), collapsed_shape=ft.RoundedRectangleBorder(
                             radius=RADIUS), icon_color=TEXT_2, collapsed_icon_color=TEXT_3, text_color=TEXT,
                         collapsed_text_color=TEXT),
                     scaffold_bgcolor=BG, card_bgcolor=SURFACE, canvas_color=SURFACE_2)
    page.theme = page.dark_theme = theme
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG
