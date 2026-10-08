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


TRANSIT_REV = 2

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
# the portal: dark hole with a keyline, a soft glow and the blue→orange rim, tilted 18°, on a soft drop shadow. In
# the transit the game's cover passes through it: "portal_back" (shadow, hole, the rim's near = PC/blue half) is drawn
# under the cover, "portal_front" (the rim's far = Frame/orange half) over it; "portal" is both in one picture (empty
# states). The halves are arcs of the same ellipse; the near one runs a few degrees past the split under the far
# one, so no seam shows where they meet.
_P_CX, _P_CY, _P_RX, _P_RY = 32, 32, 4.5, 22
_P_VIEW = "20 7 26 54"  # the ring (+ glow) spans x 21.3-42.7, y 7.9-56.1; the shadow reaches x 45.2, y 60


def _portal_arc(t0: float, t1: float) -> str:
    """Path along the portal's ellipse (unrotated) from parameter angle t0 to t1 (degrees; y points down, so a
    growing angle runs clockwise), in arcs of at most 90° (one 180° arc is ambiguous)."""
    import math

    def pt(t: float) -> str:
        r = math.radians(t)
        return f"{_P_CX + _P_RX * math.cos(r):.3f} {_P_CY + _P_RY * math.sin(r):.3f}"

    steps = max(1, math.ceil((t1 - t0) / 90 - 1e-9))
    return f"M{pt(t0)}" + "".join(f"A{_P_RX} {_P_RY} 0 0 1 {pt(t0 + (t1 - t0) * i / steps)}"
                                  for i in range(1, steps + 1))


def _portal_svg(back: bool, front: bool) -> str:
    """The portal's back (shadow, hole, near half), front (far half) or both; colours as {near}/{far}/{hole}."""
    rot = f'transform="rotate(18 {_P_CX} {_P_CY})"'
    ell = f'cx="{_P_CX}" cy="{_P_CY}" rx="{_P_RX}" ry="{_P_RY}"'
    near, far = _portal_arc(86, 274), _portal_arc(-90, 90)  # near = x < cx, 4° longer at both ends

    def half(d: str) -> str:  # keyline, glow and rim along one half
        return (f'<path d="{d}" fill="none" stroke="{{hole}}" stroke-width="5.8"/>'
                f'<path d="{d}" fill="none" stroke="url(#r)" stroke-opacity=".25" stroke-width="6.46"/>'
                f'<path d="{d}" fill="none" stroke="url(#r)" stroke-width="3.4"/>')

    out = ['<defs><linearGradient id="r" gradientUnits="userSpaceOnUse" x1="26" y1="0" x2="38" y2="0">'
           '<stop offset=".3" stop-color="{near}"/><stop offset=".7" stop-color="{far}"/></linearGradient></defs>']
    if back:
        # the drop shadow: the ring a little lower and to the right, blurred by hand (flutter_svg ignores <filter>):
        # stacked translucent outlines, each wider and fainter
        out.append(f'<g transform="translate(.7 2) rotate(18 {_P_CX} {_P_CY})">')
        out += [f'<ellipse {ell} fill="#000" fill-opacity="{a}" stroke="#000" stroke-opacity="{a}" '
                f'stroke-width="{w}"/>' for w, a in ((6.8, .22), (8.6, .13), (10.4, .07))]
        out.append(f'</g><g {rot}><ellipse {ell} fill="{{hole}}"/>{half(near)}'
                   f'<ellipse {ell} fill="url(#r)" fill-opacity="0.14"/></g>')
    if front:
        out.append(f'<g {rot}>{half(far)}</g>')
    w, h = _P_VIEW.split()[2:]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="{_P_VIEW}">'
            + "".join(out) + "</svg>\n")


_PORTAL, _PORTAL_BACK, _PORTAL_FRONT = _portal_svg(True, True), _portal_svg(True, False), _portal_svg(False, True)
PORTAL_RING = (24, 50)  # the ring's box in viewBox units, from the portal viewBox's top left (the track = 50 units)
TRANSIT_SIZES = {"monitor": (39, 38), "headset": (34, 28), "portal": (26, 54)}  # viewBox sizes (aspect ratios)


def transit_parts(assets: Path, colors: dict) -> dict[str, Path]:
    """The install transit's monitor, portal and headset: the logo's own shapes, whole, in a theme's colours.
    colors: line (strokes), body (inside the outlines, the logo's tile), screen (the monitor's screen), lens (the
    visor's lenses), near/far (the portal rim's PC and Frame sides), hole (the portal's inside). Written once per
    colour set; returns {"monitor", "portal", "portal_back", "portal_front", "headset"} → file (the three portal
    pictures share one viewBox, TRANSIT_SIZES["portal"]; the ring sits in its top-left PORTAL_RING box)."""
    c = {k: str(v).upper() for k, v in colors.items()}
    portal = ("near", "far", "hole")
    out = {}
    for name, svg, keys in (("monitor", _MONITOR, ("line", "body", "screen")),
                            ("headset", _HEADSET, ("line", "body", "lens")),
                            ("portal", _PORTAL, portal), ("portal_back", _PORTAL_BACK, portal),
                            ("portal_front", _PORTAL_FRONT, portal)):
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
