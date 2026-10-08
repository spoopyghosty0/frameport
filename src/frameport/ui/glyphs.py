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


TRANSIT_REV = 1

# The logo's parts, whole (the logo shows the monitor's left half and the headset's right half): same 64-unit
# coordinates, strokes and radii as icons/logo.svg, each cropped to its own viewBox.
_MONITOR = ('<svg xmlns="http://www.w3.org/2000/svg" width="39" height="38" viewBox="3 11.5 39 38">'
            '<path d="M9 15h27a3 3 0 0 1 3 3v20a3 3 0 0 1-3 3H9a3 3 0 0 1-3-3V18a3 3 0 0 1 3-3z" fill="{body}" '
            'stroke="{line}" stroke-width="3" stroke-linejoin="round"/>'
            '<rect x="10.5" y="19.5" width="24" height="17" rx="1" fill="{screen}" fill-opacity="0.35"/>'
            '<path d="M22.5 41v6M15.5 47h14" fill="none" stroke="{line}" stroke-width="3" stroke-linecap="round"/>'
            '</svg>\n')
# the visor mirrored about its nose notch (x 44.25), lenses either side, a strap stub on both ends
_HEADSET = ('<svg xmlns="http://www.w3.org/2000/svg" width="34" height="28" viewBox="27.25 17.5 34 28">'
            '<path d="M30 31.5H32M56.5 31.5H58.5" stroke="{line}" stroke-width="3.4" stroke-linecap="round"/>'
            '<path d="M39.5 21H49a8 8 0 0 1 8 8v5a8 8 0 0 1-8 8H48.65a3 3 0 0 1-2.6-1.5l-1-1.7a.9.9 0 0 0-1.6 0l-1 1.7'
            'a3 3 0 0 1-2.6 1.5H39.5a8 8 0 0 1-8-8v-5a8 8 0 0 1 8-8z" fill="{body}" stroke="{line}" stroke-width="3" '
            'stroke-linejoin="round"/>'
            '<ellipse cx="39.5" cy="30.5" rx="4.2" ry="3.8" fill="{lens}"/>'
            '<ellipse cx="49" cy="30.5" rx="4.2" ry="3.8" fill="{lens}"/></svg>\n')
# the portal: dark hole with a keyline, a soft glow and the blue→orange rim, tilted 18°
_PORTAL = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="50" viewBox="20 7 24 50">'
           '<defs><linearGradient id="r" gradientUnits="userSpaceOnUse" x1="26" y1="0" x2="38" y2="0">'
           '<stop offset=".3" stop-color="{near}"/><stop offset=".7" stop-color="{far}"/></linearGradient></defs>'
           '<g transform="rotate(18 32 32)">'
           '<ellipse cx="32" cy="32" rx="4.5" ry="22" fill="{hole}" stroke="{hole}" stroke-width="5.8"/>'
           '<ellipse cx="32" cy="32" rx="4.5" ry="22" fill="none" stroke="url(#r)" stroke-opacity=".25" '
           'stroke-width="6.46"/>'
           '<ellipse cx="32" cy="32" rx="4.5" ry="22" fill="url(#r)" fill-opacity="0.14" stroke="url(#r)" '
           'stroke-width="3.4"/></g></svg>\n')
TRANSIT_SIZES = {"monitor": (39, 38), "headset": (34, 28), "portal": (24, 50)}  # viewBox sizes (aspect ratios)


def transit_parts(assets: Path, colors: dict) -> dict[str, Path]:
    """The install transit's monitor, portal and headset: the logo's own shapes, whole, in a theme's colours.
    colors: line (strokes), body (inside the outlines, the logo's tile), screen (the monitor's screen), lens (the
    visor's lenses), near/far (the portal rim's PC and Frame sides), hole (the portal's inside). Written once per
    colour set; returns {"monitor", "portal", "headset"} → file."""
    c = {k: str(v).upper() for k, v in colors.items()}
    out = {}
    for name, svg, keys in (("monitor", _MONITOR, ("line", "body", "screen")),
                            ("headset", _HEADSET, ("line", "body", "lens")),
                            ("portal", _PORTAL, ("near", "far", "hole"))):
        tag = "-".join(c[k].lstrip("#") for k in keys)
        dest = assets / FOLDER / f"transit{TRANSIT_REV}-{name}-{tag}.svg"  # REV: the drawing changed
        if not dest.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(svg.format(**{k: c[k] for k in keys}), encoding="utf-8")
        out[name] = dest
    return out


def path(name: str, assets: Path) -> Path:
    """The installed file for a glyph ("fp:frame") or a logo file name ("logo")."""
    return assets / FOLDER / f"{name.removeprefix(PREFIX)}.svg"
