"""FramePort's own icons (no Flet): the logo and 24 px line glyphs for what Material has no icon for (the Frame, a
portal, launch tests …). They ship as SVG files in ui/icons and are copied into the GUI's assets folder (the user data
dir) so they load like artwork: by URL in source runs and the web view, by file path in packaged apps
(artwork.thumbs.asset_url). Glyphs are drawn in white and tinted by the GUI (components.icon)."""
from __future__ import annotations

from pathlib import Path

PREFIX = "fp:"
# glyph names usable wherever the GUI takes an icon (components.icon turns them into a tinted image)
FRAME = "fp:frame"          # the Steam Frame headset
PORT = "fp:port"            # portal + arrow: build / convert a game
PATCH = "fp:patch"
CONTAINER = "fp:container"  # Lepton's per-game container
TEST = "fp:test"            # launch test
PC = "fp:pc"                # on this PC / PC VR
SYNC = "fp:sync"            # update on Frame
LIVE = "fp:live"            # live view
SHOT = "fp:shot"            # screenshots
KEYS = "fp:keys"            # type on Frame
RECIPE = "fp:recipe"        # verified catalog recipe
LINK = "fp:link"            # Frame connection

SOURCE = Path(__file__).with_name("icons")
FOLDER = "ui-icons"         # inside the assets folder


def is_glyph(icon) -> bool:
    return isinstance(icon, str) and icon.startswith(PREFIX)


def install(assets: Path) -> Path:
    """Copy the bundled SVGs into <assets>/ui-icons (only files that changed). Returns that folder."""
    target = assets / FOLDER
    for src in [*SOURCE.glob("*.svg"), *(SOURCE / "glyphs").glob("*.svg")]:
        dest = target / src.name
        data = src.read_bytes()
        try:
            if dest.read_bytes() == data:
                continue
        except OSError:
            pass
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return target


PORTAL_REV = 2


def portal_mark(assets: Path, near: str, far: str, hole: str) -> Path:
    """The small tilted portal between "Frame" and "Port" in the wordmark, in a theme's colours: the rim runs from
    `near` (the side facing "Frame") to `far` (facing "Port"), around a dark hole. Written once per colour set."""
    near, far, hole = (c.upper() for c in (near, far, hole))
    dest = assets / FOLDER / f"portal{PORTAL_REV}-{near[1:]}-{far[1:]}-{hole[1:]}.svg"  # REV: the drawing changed
    if not dest.is_file():
        rim = (f'<linearGradient id="r" gradientUnits="userSpaceOnUse" x1="2" y1="0" x2="14" y2="0">'
               f'<stop offset=".3" stop-color="{near}"/><stop offset=".7" stop-color="{far}"/></linearGradient>')
        ring = '<ellipse cx="8" cy="14" rx="4.4" ry="10.6" transform="rotate(14 8 14)"'
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="28" viewBox="0 0 16 28">'
                        f'<defs>{rim}</defs>'
                        f'{ring} fill="none" stroke="url(#r)" stroke-opacity=".35" stroke-width="5"/>'
                        f'{ring} fill="{hole}" stroke="url(#r)" stroke-width="2.6"/></svg>\n', encoding="utf-8")
    return dest


def path(name: str, assets: Path) -> Path:
    """The installed file for a glyph ("fp:frame") or a logo file name ("logo")."""
    return assets / FOLDER / f"{name.removeprefix(PREFIX)}.svg"
