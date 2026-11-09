"""Store artwork for the Steam library (portrait/landscape/hero/logo/icon).

Fetched live from OVRPort's image service (Meta store art by package name), cached per package. When the Meta services
are down (GitHub #510), the game's exact name finds art on the Steam store, SideQuest and (with the user's key)
SteamGridDB (artwork/stores.py). Falls back to the APK's launcher icon when nothing has art (e.g. sideload-only titles).
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
SOURCE = ".source"  # where the automatic art came from (meta, oculusdb, steam, sidequest, steamgriddb; comma-separated)
COVER_KINDS = frozenset({"portrait", "landscape", "hero", "square"})
ART_STEMS = frozenset(KINDS.values()) | frozenset(EXTRA_KINDS)


def artwork_dir(package: str) -> Path:
    path = user_data_dir() / "artwork" / package
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ext(data: bytes) -> str:
    return ".png" if data[:4] == b"\x89PNG" else ".jpg"


def fetch(package: str, apk: Path | None = None, refresh: bool = False,
          lookup: str | None = None, title: str | None = None, fallbacks: bool = True) -> tuple[Path, str | None]:
    """Returns (folder with <kind>.<ext> files, store display name or None). `lookup` is the package whose store art
    to use (a Rift game borrows its Quest version's art); Rift ids without one have no store art.
    Without `refresh` it only fills in missing kinds and never touches art the user picked (PICKED marker): a pick
    often lacks a kind (Steam has no icon, OculusDB only a square cover), and re-fetching the store art for that
    replaced the pick at the next install. With `refresh` ("Find automatically") everything is looked up again into a
    staging folder; the current art is replaced only when that found a cover.

    Sources, in order (GitHub #510: the Meta services can be down for weeks): OVRPort's image service by package
    (asked every time, so it is used again as soon as it's back), OculusDB's cover of exactly this package, then, for
    Quest games, by the game's exact name (`title`, else the library/catalog name; off with `fallbacks=False`): the
    Steam store (a VR app or one by the same publisher), SideQuest's store banner, SteamGridDB (with the user's API
    key); finally the APK's icon."""
    out = artwork_dir(package)
    if not refresh:
        return out, _fill(package, out, apk, lookup, title, fallbacks)
    import shutil

    (out / PICKED).unlink(missing_ok=True)
    stage = artwork_dir(f".refresh.{package}")
    shutil.rmtree(stage, ignore_errors=True)
    stage = artwork_dir(f".refresh.{package}")
    try:
        found = _fill(package, stage, apk, lookup, title, fallbacks)
        art = [f for f in stage.iterdir() if f.is_file() and (f.stem in ART_STEMS or f.name in ("title.txt", SOURCE))]
        if {f.stem for f in art} & COVER_KINDS:  # a new cover: the new set replaces the old one (and its thumbnails)
            for f in out.iterdir():
                if f.is_file() and (f.stem in ART_STEMS or f.name == ".cover"
                                    or f.name.startswith("t_") and not f.name.startswith("t_shot_")):
                    f.unlink()
        have = {p.stem for p in out.iterdir() if p.is_file()}
        if not (stage / SOURCE).exists():
            (out / SOURCE).unlink(missing_ok=True)  # this look found nothing (fetch_art says so)
        for f in art:
            if f.stem not in have or f.name in ("title.txt", SOURCE):
                f.replace(out / f.name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if found is None and (out / "title.txt").exists():
        found = (out / "title.txt").read_text(encoding="utf-8")
    return out, found


def source(package: str) -> str:
    """Where the game's automatic art came from at the last fetch that found some ("" when unknown)."""
    try:
        return (artwork_dir(package) / SOURCE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _fill(package: str, out: Path, apk: Path | None, lookup: str | None, title: str | None,
          fallbacks: bool) -> str | None:
    """Fill the missing kinds of `out` (see fetch). Returns the image service's display name, or the stored one."""
    have = {p.stem for p in out.iterdir()}
    store_title = None
    quest = not package.startswith(("rift.", "."))
    lookup = lookup or (package if quest else None)
    picked = (out / PICKED).exists()
    used = []
    if lookup and not picked and not {"portrait", "landscape", "hero", "icon"} <= have:
        store_title = _image_service(out, lookup)
        if {p.stem for p in out.iterdir()} - have - {"title"}:
            used.append("meta")
    if lookup and not picked and not ({p.stem for p in out.iterdir()} & COVER_KINDS) and _oculusdb_cover(out, lookup):
        used.append("oculusdb")
    if fallbacks and quest and not picked:
        used += by_name(out, package, title, store_title)
    if used:
        (out / SOURCE).write_text(",".join(used), encoding="utf-8")
    if not any(out.glob("icon.*")) and apk:
        icon = apk_icon(apk)
        if icon:
            (out / f"icon{_ext(icon)}").write_bytes(icon)
    if store_title is None and (out / "title.txt").exists():
        store_title = (out / "title.txt").read_text(encoding="utf-8")
    return store_title


def _image_service(out: Path, package: str) -> str | None:
    """OVRPort's image service (Meta's store art by package). A 404, an HTML page or images that aren't images count
    as "nothing" and aren't remembered: the next fetch asks again, so the art comes once the service is back. Fills
    only missing kinds."""
    try:
        data = cache.http_get(API.format(package=package), timeout=30).json()
    except Exception:  # noqa: BLE001 - 404 (answered for every package at times), offline, not JSON
        return None
    if not isinstance(data, dict) or data.get("status") != "ok":
        return None
    from . import stores

    for img in data.get("images") or []:
        kind = KINDS.get((img or {}).get("image_type")) if isinstance(img, dict) else None
        if not kind or any(out.glob(f"{kind}.*")):
            continue
        try:
            raw = base64.b64decode(img["uri"])
        except (KeyError, TypeError, ValueError):
            continue
        stores.save(out, kind, raw)
    title = data.get("displayName")
    if title and {p.stem for p in out.iterdir()} & set(KINDS.values()):
        (out / "title.txt").write_text(title, encoding="utf-8")
        return title
    return None


def names(package: str, title: str | None = None, store_title: str | None = None) -> list[str]:
    """The names to look a game up by (exact matches only): the given title, the library's automatic name, its
    current name (one the user typed), the catalog's, the store name the image service gave. Empty for an Android app
    without VR (it isn't on a VR store; a name match would be another product)."""
    from ..core import library
    from .sources import norm

    try:
        g = library.game(package) or {}
    except Exception:  # noqa: BLE001
        g = {}
    if ((g.get("analysis") or {}).get("extra") or {}).get("vr_kind") == "none":
        return []
    found = [title, g.get("auto_title"), g.get("title")]
    try:
        from ..recommend import catalog

        entry = catalog.lookup(package)
        found.append(entry.title if entry else None)
    except Exception:  # noqa: BLE001
        pass
    found.append(store_title)
    out, seen = [], set()
    for n in found:
        key = norm(n or "")
        if len(key) >= 3 and key not in seen and n != package:
            seen.add(key)
            out.append(n.strip())
    return out


def by_name(out: Path, package: str, title: str | None = None, store_title: str | None = None) -> list[str]:
    """Art by the game's exact name when the Meta services gave no cover: the Steam store (cover, banner, header,
    logo), else SideQuest's store banner; then SteamGridDB fills what's still missing when the user set a key. Only
    missing kinds are stored. Returns the sources used."""
    from . import stores

    used: list[str] = []
    look = names(package, title, store_title)
    if not look:
        return used

    def stems() -> set[str]:
        return {p.stem for p in out.iterdir()}
    if not stems() & COVER_KINDS:
        from . import details, sources

        for name in look:
            try:
                app = details.steam_app(name)  # (a VR app: a common name on Steam is often another game, #524)
            except Exception:  # noqa: BLE001 - offline
                app = None
            if app and app.get("steam_appid") and sources.apply_steam(package, app["steam_appid"], folder=out,
                                                                      replace=False, check=False):
                used.append("steam")
                break
    if not stems() & COVER_KINDS:
        try:
            app = stores.match_sidequest(look, package)
        except Exception:  # noqa: BLE001 - offline
            app = None
        banner = stores.sidequest_banner(app) if app else None
        if banner and stores.save(out, "landscape", stores.download(banner).data):
            used.append("sidequest")
    key = stores.sgdb_key()
    if key and not set(stores.SGDB_KINDS) <= stems():
        try:
            game = stores.match_sgdb(look, key)
        except Exception:  # noqa: BLE001 - offline, rate limit, a rejected key
            game = None
        if game and stores.apply_sgdb(out, game["id"], stores.SGDB_KINDS, key):
            used.append("steamgriddb")
    if used:
        drop_generated(out)
        cover_from_wide(out)
    return used


def drop_generated(out: Path) -> None:
    """FramePort's generated cover/banner (no store art) go once store art exists."""
    for f in list(out.glob("cover.*")) + list(out.glob("banner.*")) + list(out.glob(".cover")):
        f.unlink(missing_ok=True)


def cover_from_wide(out: Path) -> Path | None:
    """Only wide store art (SideQuest's banner): a tall cover for the Library's cards, the banner over a blurred copy
    of itself. It is FramePort's own `cover` (never sent as store art; Steam's shapes are composed from the banner)."""
    stems = {p.stem for p in out.iterdir()}
    if stems & {"portrait", "square"}:
        return None
    src = next((p for k in ("landscape", "hero") for p in sorted(out.glob(f"{k}.*"))), None)
    if src is None:
        return None
    from PIL import Image, ImageEnhance, ImageFilter

    from .steam import _fill

    w, h = 600, 900
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            bg = ImageEnhance.Brightness(_fill(im, (w, h)).filter(ImageFilter.GaussianBlur(radius=28))).enhance(0.6)
            # the banner across the whole width, a little above the middle (the card's title sits at the bottom)
            fg = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
            bg.paste(fg, (0, max(0, round(h * 0.42 - fg.height / 2))))
    except Exception:  # noqa: BLE001 - unreadable: the card shows the icon
        return None
    path = out / "cover.jpg"
    bg.save(path, "JPEG", quality=88, optimize=True)
    (out / ".cover").unlink(missing_ok=True)
    return path


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
    drop_generated(out)
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
