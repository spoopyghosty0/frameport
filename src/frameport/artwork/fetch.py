"""Store artwork for the Steam library (portrait/landscape/hero/logo/icon).

Fetched live from OVRPort's image service (Meta store art by package name), cached per package. Falls back to
the APK's launcher icon when the store has nothing (e.g. sideload-only titles).
"""
from __future__ import annotations

import base64
import zipfile
from pathlib import Path

from ..core import cache
from ..core.paths import user_data_dir

API = "https://ovrp.crx.moe/images/by_package?package={package}"
KINDS = {
    "APP_IMG_COVER_PORTRAIT": "portrait",
    "APP_IMG_COVER_LANDSCAPE": "landscape",
    "APP_IMG_HERO": "hero",
    "APP_IMG_LOGO_TRANSPARENT": "logo",
    "APP_IMG_ICON": "icon",
}
# OculusDB's square cover (Rift games); FramePort's own cover/banner (no store art)
EXTRA_KINDS = ("square", "cover", "banner")
PICKED = ".picked"  # marker: the art in this folder was chosen by the user (Find artwork); automatic fetches skip it


def artwork_dir(package: str) -> Path:
    path = user_data_dir() / "artwork" / package
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ext(data: bytes) -> str:
    return ".png" if data[:4] == b"\x89PNG" else ".jpg"


def fetch(package: str, apk: Path | None = None, refresh: bool = False,
          lookup: str | None = None) -> tuple[Path, str | None]:
    """Returns (folder with <kind>.<ext> files, store display name or None). `lookup` is the package whose store art
    to use (a Rift game borrows its Quest version's art); Rift ids without one have no store art.
    Without `refresh` it only fills in missing kinds and never touches art the user picked (PICKED marker): a pick
    often lacks a kind (Steam has no icon, OculusDB only a square cover), and re-fetching the store art for that
    replaced the pick at the next install."""
    out = artwork_dir(package)
    if refresh:
        (out / PICKED).unlink(missing_ok=True)
    have = {p.stem for p in out.iterdir()}
    title = None
    lookup = lookup or (None if package.startswith("rift.") else package)
    picked = (out / PICKED).exists()
    if lookup and not picked and (refresh or not {"portrait", "landscape", "hero", "icon"} <= have):
        try:
            data = cache.http_get(API.format(package=lookup), timeout=30).json()
        except Exception:
            data = {}
        if data.get("status") == "ok":
            title = data.get("displayName")
            for img in data.get("images", []):
                kind = KINDS.get(img.get("image_type"))
                if not kind:
                    continue
                if not refresh and kind in have:
                    continue  # keep what's there; only fill gaps
                raw = base64.b64decode(img["uri"])
                for old in out.glob(f"{kind}.*"):
                    old.unlink()
                (out / f"{kind}{_ext(raw)}").write_bytes(raw)
            if title:
                (out / "title.txt").write_text(title, encoding="utf-8")
    if lookup and not picked and not ({p.stem for p in out.iterdir()} & COVER_KINDS):
        _oculusdb_cover(out, lookup)
    if not any(out.glob("icon.*")) and apk:
        icon = apk_icon(apk)
        if icon:
            (out / f"icon{_ext(icon)}").write_bytes(icon)
    if title is None and (out / "title.txt").exists():
        title = (out / "title.txt").read_text(encoding="utf-8")
    return out, title


COVER_KINDS = frozenset({"portrait", "landscape", "hero", "square"})


def _oculusdb_cover(out: Path, package: str) -> bool:
    """No cover from the image service (it answers 404 for every package at times, GitHub #510: Assassin's Creed
    Nexus got no art while its picker result was there): the store cover of the app with exactly this package from
    OculusDB, saved as the square cover (the Steam shapes are composed from it). FramePort's generated cover/banner
    go, like with a picked cover."""
    from . import sources

    try:
        app = next((r for r in sources.oculusdb_search(package, rift_only=False) if r.get("packageName") == package),
                   None)
        if not app or not sources.apply_oculusdb(package, app, folder=out):
            return False
    except Exception:  # noqa: BLE001 - offline: the APK icon (and a generated cover) stay
        return False
    for f in list(out.glob("cover.*")) + list(out.glob("banner.*")) + list(out.glob(".cover")):
        f.unlink(missing_ok=True)
    return True


def apk_icon(apk: Path | str | None) -> bytes | None:
    """The app's launcher icon from the APK as PNG bytes: the icon the manifest names (pyaxmlparser), else the
    largest ic_launcher bitmap. None for vector/adaptive-only icons (no bitmap to use)."""
    if not apk or not Path(apk).is_file():
        return None
    import io

    from PIL import Image

    dens = ("xxxhdpi", "xxhdpi", "xhdpi", "hdpi", "mdpi")
    try:
        with zipfile.ZipFile(apk) as z:
            listed = set(z.namelist())
            names = []
            try:
                from pyaxmlparser import APK

                named = APK(str(apk)).get_app_icon()
                if named:
                    names.append(named)
            except Exception:  # noqa: BLE001 - unreadable resources: fall back to the usual file names
                pass
            names += sorted((n for n in listed if n.startswith("res/") and "ic_launcher" in n
                             and n.endswith((".png", ".webp")) and "foreground" not in n and "background" not in n),
                            key=lambda n: next((i for i, d in enumerate(dens) if d in n), len(dens)))
            for n in names:
                if n not in listed or not n.endswith((".png", ".webp")):
                    continue
                with Image.open(io.BytesIO(z.read(n))) as im:
                    if im.width < 48:
                        continue
                    out = io.BytesIO()
                    im.convert("RGBA").save(out, "PNG")
                    return out.getvalue()
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    return None


def files(package: str) -> list[Path]:
    return sorted(p for p in artwork_dir(package).iterdir() if p.stem in KINDS.values() or p.stem in EXTRA_KINDS)
