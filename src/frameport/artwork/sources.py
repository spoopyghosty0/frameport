"""Find artwork for games that the Meta art service (by Android package) doesn't cover — Oculus Rift games.

Order (all lookups cached for 30 days; only exact title matches are used, so no wrong art is picked):
  1. the Quest version's package (catalog, library, or OculusDB's packageName) → the full Meta art set (fetch.fetch)
  2. OculusDB (community database of the Oculus store): the Rift app's square cover
  3. the Steam store (games also sold on Steam): portrait, hero, landscape and logo
  4. the game's own program icon
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

from ..core import cache
from . import fetch

OCULUSDB = "https://oculusdb.rui2015.me"
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/?term={term}&cc=us&l=en"
STEAM_CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/{id}/{file}"
STEAM_FILES = {"portrait": "library_600x900.jpg", "hero": "library_hero.jpg", "landscape": "header.jpg",
               "logo": "logo.png"}
MAX_AGE = 30 * 86400


ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
QUEST_SUFFIXES = {"", "unplugged", "quest", "questedition", "forquest", "vr"}


def norm(s: str) -> str:
    """Comparable title: lower-case letters and digits only, roman numerals as digits ('Lone Echo II' = 'Lone Echo 2',
    "Asgard's Wrath" = 'Asgards Wrath')."""
    words = re.findall(r"[a-z0-9]+", (s or "").lower().replace("'", ""))
    return "".join(ROMAN.get(w, w) if i and w in ROMAN else w for i, w in enumerate(words))


def search_terms(title: str) -> list[str]:
    """The title, then shorter forms (OculusDB's search doesn't match "Asgards" to "Asgard's")."""
    words = re.sub(r"[^\w\s]", " ", title).split()
    terms = [title]
    for n in (3, 2, 1):
        if len(words) > n and len(" ".join(words[:n])) >= 4:
            terms.append(" ".join(words[:n]))
    if words and len(words[0]) >= 5 and words[0].lower().endswith("s"):
        terms.append(words[0][:-1])  # "Asgards" -> "Asgard", "Wilsons" -> "Wilson"
    return list(dict.fromkeys(terms))


def _key(term: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", term.lower())[:80]


def oculusdb_search(term: str, rift_only: bool = True) -> list[dict]:
    q = urllib.parse.quote(term)
    url = f"{OCULUSDB}/api/v1/search/{q}" + ("?headsets=RIFT" if rift_only else "")
    res = cache.cached_json(f"oculusdb-{'rift' if rift_only else 'all'}-{_key(term)}.json", url, max_age=MAX_AGE,
                            fallback=[])
    return [r for r in res or [] if isinstance(r, dict) and r.get("__OculusDBType", "Application") == "Application"]


def steam_search(term: str) -> list[dict]:
    res = cache.cached_json(f"steamsearch-{_key(term)}.json", STEAM_SEARCH.format(term=urllib.parse.quote(term)),
                            max_age=MAX_AGE, fallback={})
    return (res or {}).get("items") or []


def match_rift(title: str, canonical: str | None = None) -> dict | None:
    """The OculusDB Rift app for this game (exact normalized name or canonicalName match)."""
    want = norm(title)
    terms = ([canonical.replace("-", " ")] if canonical else []) + search_terms(title)
    for term in terms:
        for r in oculusdb_search(term):
            names = {norm(r.get("appName") or ""), norm(r.get("displayName") or "")}
            if (canonical and r.get("canonicalName") == canonical) or want in names:
                return r
    return None


def match_quest(title: str) -> str | None:
    """Package of the game's Quest version on the Meta store (same name, or the name plus a short suffix such as
    'Unplugged'), from OculusDB."""
    want = norm(title)
    if len(want) < 4:
        return None
    for term in search_terms(title)[:2]:
        for r in oculusdb_search(term, rift_only=False):
            pkg = r.get("packageName")
            n = norm(r.get("appName") or r.get("displayName") or "")
            # same name, or the name plus a platform word ("Robo Recall: Unplugged") — never a sequel/other game
            if pkg and n.startswith(want) and n[len(want):] in QUEST_SUFFIXES:
                return pkg
    return None


def match_steam(title: str) -> dict | None:
    want = norm(title)
    return next((i for i in steam_search(title) if norm(i.get("name") or "") == want), None)


def _save(package: str, kind: str, data: bytes) -> None:
    if not data or len(data) < 200:
        return
    ext = ".png" if data[:4] == b"\x89PNG" else ".webp" if data[8:12] == b"WEBP" else ".jpg"
    d = fetch.artwork_dir(package)
    for old in d.glob(f"{kind}.*"):
        old.unlink()
    (d / f"{kind}{ext}").write_bytes(data)


def _get(url: str) -> bytes | None:
    try:
        r = cache.http_get(url, timeout=30)
        return r.content if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def apply_oculusdb(package: str, app: dict) -> bool:
    link = app.get("imageLink") or (f"/cdn/images/{app['id']}" if app.get("id") else None)
    data = _get(OCULUSDB + link) if link else None
    if data:
        _save(package, "square", data)
    return bool(data)


def apply_steam(package: str, appid: int | str) -> bool:
    got = False
    for kind, file in STEAM_FILES.items():
        data = _get(STEAM_CDN.format(id=appid, file=file))
        if data:
            _save(package, kind, data)
            got = True
    return got


def has_art(package: str) -> bool:
    stems = {p.stem for p in fetch.files(package)}
    return bool(stems & {"portrait", "square", "landscape", "hero"})


def fetch_rift(package: str, title: str, canonical: str | None = None, quest_package: str | None = None,
               exe: Path | None = None, oculus: bool = True) -> dict:
    """Find and store artwork for a PC VR game. Returns what was found: {source, quest_package, oculus_app_id,
    canonical_name, steam_appid} (only the keys that apply). oculus=False (no Oculus code: a SteamVR/OpenXR game):
    Steam is asked first, the Oculus store only if Steam has nothing."""
    found: dict = {}
    if not oculus:
        try:
            st = match_steam(title)
        except Exception:  # noqa: BLE001 - offline
            st = None
        if st and apply_steam(package, st["id"]):
            return {"steam_appid": st["id"], "source": "steam"}
    app = None
    try:
        app = match_rift(title, canonical)
    except Exception:  # noqa: BLE001 - offline: fall through to local sources
        app = None
    if app:
        found.update(oculus_app_id=app.get("id"), canonical_name=app.get("canonicalName"),
                     title=app.get("displayName") or app.get("appName"))
    if not quest_package:
        try:
            quest_package = match_quest(found.get("title") or title)
        except Exception:  # noqa: BLE001
            quest_package = None
    if quest_package:
        found["quest_package"] = quest_package
        fetch.fetch(package, lookup=quest_package)
        if has_art(package):
            found["source"] = "meta"
    if app and not found.get("source") and apply_oculusdb(package, app):
        found["source"] = "oculusdb"
    if found.get("source") != "meta":
        try:
            st = match_steam(found.get("title") or title)
        except Exception:  # noqa: BLE001
            st = None
        if st and apply_steam(package, st["id"]):
            found["steam_appid"] = st["id"]
            found["source"] = found.get("source") or "steam"
    if not has_art(package) and exe is not None:
        from ..analysis.rift import exe_icon

        icon = exe_icon(exe)
        if icon:
            _save(package, "icon", icon)
            found["source"] = "icon"
    return found


def search(term: str) -> list[dict]:
    """Candidates for the artwork picker: [{source, name, preview (URL), id|package}]."""
    out = []
    try:
        for r in oculusdb_search(term, rift_only=False)[:12]:
            link = r.get("imageLink") or (f"/cdn/images/{r['id']}" if r.get("id") else None)
            if link:
                out.append({"source": "Meta (Quest)" if r.get("packageName") else "Oculus Rift",
                            "name": r.get("displayName") or r.get("appName"), "preview": OCULUSDB + link,
                            "id": r.get("id"), "package": r.get("packageName"), "app": r})
    except Exception:  # noqa: BLE001
        pass
    try:
        for i in steam_search(term)[:8]:
            out.append({"source": "Steam", "name": i.get("name"), "preview": STEAM_CDN.format(id=i["id"],
                        file="header.jpg"), "id": i["id"]})
    except Exception:  # noqa: BLE001
        pass
    return out


ART_STEMS = frozenset(fetch.KINDS.values()) | frozenset(fetch.EXTRA_KINDS)


def apply_choice(package: str, choice: dict) -> bool:
    """Store the artwork of a picker result, replacing the current art. It's downloaded into a staging folder first:
    when the result yields no cover (dead store image, no art for that package) the current art stays and this
    returns False. Screenshots and the stored title are kept either way."""
    import shutil

    tmp = f".pick.{package}"
    stage = fetch.artwork_dir(tmp)
    shutil.rmtree(stage, ignore_errors=True)
    stage = fetch.artwork_dir(tmp)
    try:
        if choice["source"] == "Steam":
            apply_steam(tmp, choice["id"])
        elif choice.get("package"):
            # the picture the picker showed (OculusDB) is the cover; the store service by package adds the logo
            # and icon. Its covers can differ from the picked picture (e.g. a "dogfooding" placeholder), so they're
            # dropped when the picked picture arrived: the Steam shapes are composed from it instead.
            picked = apply_oculusdb(tmp, choice["app"]) if choice.get("app") else False
            fetch.fetch(tmp, lookup=choice["package"], refresh=True)
            if picked:
                for f in stage.iterdir():
                    if f.stem in ("portrait", "landscape", "hero"):
                        f.unlink()
        else:
            apply_oculusdb(tmp, choice["app"])
        if not has_art(tmp):
            return False
        d = fetch.artwork_dir(package)
        for f in d.iterdir():  # the old art and its thumbnails (screenshot thumbnails are t_shot_*)
            if f.is_file() and (f.stem in ART_STEMS or f.name.startswith("t_") and not f.name.startswith("t_shot_")):
                f.unlink()
        for f in stage.iterdir():
            if f.is_file() and f.stem in ART_STEMS:
                f.replace(d / f.name)
        (d / fetch.PICKED).write_text(choice.get("source", ""), encoding="utf-8")  # keep it through installs
        return True
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# The user's own images (Artwork dialog → "Your own images"), for games whose art can't be found automatically.
CUSTOM_KINDS = ("portrait", "landscape", "hero", "logo", "icon")
CUSTOM_MAX_BYTES = 40 << 20
CUSTOM_MIN_PX = 64
CUSTOM_MAX_PX = 3840  # larger images are scaled down (Steam's biggest shape is 1920 px wide)


class ArtError(ValueError):
    """The chosen file can't be used as artwork (the message says why, for the GUI)."""


def apply_custom(package: str, kind: str, path: str | Path) -> Path:
    """Store an image file the user chose as one kind of artwork, replacing that kind only. It is checked and
    re-encoded (PNG when it has transparency, else JPEG; logos and icons always PNG), marked as the user's pick so
    automatic fetches never replace it, and the old thumbnails go. FramePort's generated cover/banner (games without
    store art) are dropped once a real cover shape exists. Returns the stored file."""
    from PIL import Image, UnidentifiedImageError

    if kind not in CUSTOM_KINDS:
        raise ArtError(f"unknown artwork kind {kind!r}")
    path = Path(path)
    try:
        if path.stat().st_size > CUSTOM_MAX_BYTES:
            raise ArtError(f"{path.name} is larger than {CUSTOM_MAX_BYTES >> 20} MB")
        with Image.open(path) as im:
            im.load()
            im = im.copy()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise ArtError(f"{path.name} isn't an image FramePort can read (use PNG, JPEG or WebP)") from None
    if min(im.size) < CUSTOM_MIN_PX:
        raise ArtError(f"{path.name} is too small ({im.width}×{im.height}); use at least {CUSTOM_MIN_PX} px")
    if max(im.size) > CUSTOM_MAX_PX:
        im.thumbnail((CUSTOM_MAX_PX, CUSTOM_MAX_PX), Image.LANCZOS)
    alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    png = alpha or kind in ("logo", "icon")
    d = fetch.artwork_dir(package)
    tmp = d / f".custom-{kind}.tmp"
    if png:
        im.convert("RGBA" if alpha else "RGB").save(tmp, "PNG", optimize=True)
    else:
        im.convert("RGB").save(tmp, "JPEG", quality=92, optimize=True)
    _drop_kind(d, kind)
    out = d / f"{kind}{'.png' if png else '.jpg'}"
    tmp.replace(out)
    if kind in ("portrait", "landscape", "hero"):
        for stem in ("cover", "banner"):
            _drop_kind(d, stem)
    (d / fetch.PICKED).write_text("custom", encoding="utf-8")  # automatic fetches leave the art alone
    return out


# A Linux app's own icon (GitHub #99: from its .desktop file / AppImage), applied automatically: no PICKED marker, and
# APP_ICON records which icon.* it is, so a later pick (store art, the user's own) is told apart from it.
APP_ICON = ".app-icon"
APP_ICON_MIN_PX = 16


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def icon_source(package: str) -> str:
    """What the game's icon artwork is: "custom" (the user's pick, store art or an install link's icon), "app" (the
    Linux app's own icon, applied automatically) or "generated" (no icon: Steam's is composed or a placeholder). The
    Frame's agent puts the app's own icon first unless this says "custom"."""
    d = fetch.artwork_dir(package)
    icon = next(iter(sorted(d.glob("icon.*"))), None)
    if icon is None:
        return "generated"
    marker = d / APP_ICON
    try:
        if marker.read_text(encoding="utf-8").strip() == _sha256(icon):
            return "app"
    except OSError:
        pass
    return "custom"


def apply_app_icon(package: str, data: bytes) -> Path | None:
    """Store a Linux app's own icon (PNG/JPEG bytes) as the game's icon when it has none and nothing was picked (the
    user's choice and store art always stay). Not marked as the user's pick. Returns the stored file, or None when
    nothing changed."""
    import io

    from PIL import Image, UnidentifiedImageError

    d = fetch.artwork_dir(package)
    if (d / fetch.PICKED).exists() or icon_source(package) == "custom":
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            im = im.copy()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None
    if min(im.size) < APP_ICON_MIN_PX:
        return None
    if max(im.size) > 1024:
        im.thumbnail((1024, 1024), Image.LANCZOS)
    tmp = d / ".app-icon.tmp"
    im.convert("RGBA").save(tmp, "PNG", optimize=True)
    out = d / "icon.png"
    if out.exists() and out.read_bytes() == tmp.read_bytes():
        tmp.unlink()
        return None
    _drop_kind(d, "icon")
    tmp.replace(out)
    (d / APP_ICON).write_text(_sha256(out), encoding="utf-8")
    return out


def remove_custom(package: str, kind: str) -> None:
    """Remove one kind of artwork (the Steam set then composes it from the other kinds, or uses a placeholder)."""
    if kind not in CUSTOM_KINDS:
        raise ArtError(f"unknown artwork kind {kind!r}")
    d = fetch.artwork_dir(package)
    _drop_kind(d, kind)
    (d / fetch.PICKED).write_text("custom", encoding="utf-8")  # "Find automatically" brings store art back


def _drop_kind(d: Path, kind: str) -> None:
    for f in list(d.glob(f"{kind}.*")) + list(d.glob(f"t_{kind}_*")):
        f.unlink(missing_ok=True)
