"""Small cached thumbnails of the artwork, served to the GUI by URL (the GUI's assets folder is the user data dir).

Full-size store art is 0.3–2 MB per image; sending it as bytes on every render froze the app. Thumbnails are JPEGs named
after their source's content hash (t_<kind>_<width>_<sha8>.jpg), so the client cache stays correct when art changes.
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from ..core.paths import user_data_dir
from . import fetch

WIDTHS = {"portrait": 400, "square": 400, "cover": 400, "banner": 1280, "landscape": 1280, "hero": 1280, "icon": 96,
          "logo": 600}
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def _digest(path: Path) -> str:
    st = path.stat()
    return hashlib.sha1(f"{path.name}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:8]


def thumb(path: Path, width: int, kind: str | None = None) -> Path | None:
    """A JPEG (PNG for logos, which need transparency) at most `width` px wide; created once."""
    kind = kind or path.stem
    ext = ".png" if kind == "logo" else ".jpg"
    out = path.parent / f"t_{kind}_{width}_{_digest(path)}{ext}"
    if out.exists():
        return out
    with _lock(str(out)):
        if out.exists():
            return out
        try:
            from PIL import Image

            with Image.open(path) as im:
                im.load()
                if im.width > width:
                    im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
                for old in path.parent.glob(f"t_{kind}_{width}_*{ext}"):
                    old.unlink(missing_ok=True)
                tmp = out.with_suffix(out.suffix + ".tmp")
                if ext == ".png":
                    im.save(tmp, "PNG", optimize=True)
                else:
                    im.convert("RGB").save(tmp, "JPEG", quality=86, optimize=True, progressive=True)
                tmp.replace(out)
        except Exception:  # unreadable/odd image: use the original
            return path
    return out


def pick(package: str, kinds: tuple[str, ...]) -> Path | None:
    files = {p.stem: p for p in fetch.files(package)}
    return next((files[k] for k in kinds if k in files), None)


def url(package: str, kinds: tuple[str, ...], width: int | None = None, wait: bool = True) -> str | None:
    """Asset URL (for ft.Image / DecorationImage src) of the first available kind, as a thumbnail. wait=False (render
    paths): if the thumbnail doesn't exist yet it is made in the background and the original is used meanwhile."""
    src = pick(package, kinds)
    if not src:
        return None
    width = width or WIDTHS.get(src.stem, 600)
    if not wait:
        ready = ready_thumb(src, width, src.stem)
        if ready is None:
            threading.Thread(target=thumb, args=(src, width, src.stem), daemon=True).start()
            return asset_url(src)
        return asset_url(ready)
    return asset_url(thumb(src, width, src.stem))


def ready_thumb(path: Path, width: int, kind: str | None = None) -> Path | None:
    """The thumbnail if it already exists (no image work)."""
    kind = kind or path.stem
    out = path.parent / f"t_{kind}_{width}_{_digest(path)}{'.png' if kind == 'logo' else '.jpg'}"
    return out if out.exists() else None


_FILE_PATHS = False


def use_file_paths(on: bool = True) -> None:
    """Images by absolute file path: a packaged app (flet build) serves relative image paths from its own bundled
    assets, not from assets_dir, so artwork by URL stayed blank there (GitHub issue 16; the reporter confirmed file
    paths work in the Windows app). Source runs and the web view (ui_smoke) keep URLs."""
    global _FILE_PATHS
    _FILE_PATHS = on


def asset_url(path: Path) -> str:
    if _FILE_PATHS:
        return str(path.resolve())
    return "/" + path.relative_to(user_data_dir()).as_posix()


def prewarm(package: str) -> None:
    """Create the thumbnails the GUI uses (cards, hero, icons) ahead of time (called after fetching art)."""
    for kinds, width in ((("portrait", "square", "icon"), 400), (("hero", "landscape", "portrait", "square"), 1280),
                         (("icon", "square", "portrait"), 96)):
        src = pick(package, kinds)
        if src:
            thumb(src, width, src.stem)
    compute_tint(package)


# ------------------------------------------------------------------------------------------ cover tint
TINT_ART = ("portrait", "square", "cover", "icon")  # the library card's art (the cover people recognise)
_tints: dict[str, str | None] = {}  # package -> "#rrggbb" (None: no art / no usable colour)


def tint_from_rgb(rgb: tuple[int, int, int]) -> str:
    """A cover's dominant colour made to sit on the dark background: lightness 0.35-0.55, saturation at most 0.55."""
    import colorsys

    h, lum, s = colorsys.rgb_to_hls(*(max(0, min(255, c)) / 255 for c in rgb))
    r, g, b = colorsys.hls_to_rgb(h, min(0.55, max(0.35, lum)), min(0.55, s))
    return "#{:02x}{:02x}{:02x}".format(*(round(c * 255) for c in (r, g, b)))


def dominant_rgb(colors: list[tuple[int, tuple[int, int, int]]]) -> tuple[int, int, int] | None:
    """The most common clearly coloured entry of [(count, rgb)] (saturation ≥ 0.25, neither near black nor near
    white); else the most common one overall. None for an empty list."""
    import colorsys

    def colourful(rgb) -> bool:
        _, lum, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
        return s >= 0.25 and 0.12 <= lum <= 0.88
    ranked = [rgb for _, rgb in sorted(colors, key=lambda c: -c[0])]
    return next((rgb for rgb in ranked if colourful(rgb)), ranked[0] if ranked else None)


def compute_tint(package: str) -> str | None:
    """The cover's tint, cached next to the thumbnails (t_tint_<sha8>.json, named after its source like a thumbnail)
    and in memory. Image work: call from a background thread (render paths use cached_tint)."""
    import json

    src = pick(package, TINT_ART)
    value = None
    if src:
        out = src.parent / f"t_tint_{_digest(src)}.json"
        try:
            value = json.loads(out.read_text(encoding="utf-8")).get("tint")
        except (OSError, ValueError, AttributeError):
            try:
                from PIL import Image

                with Image.open(thumb(src, 400, src.stem)) as im:
                    small = im.convert("RGB").resize((48, 48))
                q = small.quantize(colors=8)
                pal = q.getpalette() or []
                rgb = dominant_rgb([(n, tuple(pal[i * 3:i * 3 + 3])) for n, i in (q.getcolors() or [])])
                value = tint_from_rgb(rgb) if rgb else None
                for old in src.parent.glob("t_tint_*.json"):
                    old.unlink(missing_ok=True)
                out.write_text(json.dumps({"tint": value}), encoding="utf-8")
            except Exception:  # noqa: BLE001  (unreadable image: no tint)
                value = None
    _tints[package] = value
    return value


def cached_tint(package: str) -> tuple[bool, str | None]:
    """(known, tint) from memory only (no I/O, for render paths); known=False: compute_tint hasn't run yet."""
    return (package in _tints, _tints.get(package))
