"""More artwork sources for Quest games, used when the Meta art services are down (GitHub #510: OVRPort's image
service answered 404 for every package and OculusDB redirected to an outage page).

  - SideQuest (`api.sidequestvr.com`, no key): a name search. Store titles are synced from Meta's store (one
    landscape banner, screenshots, description, tags); their `packagename` is a made-up `com.autogen.*` id, so only an
    exact normalized-name match counts, and a listing with a real package name of another app never does.
  - SteamGridDB (`www.steamgriddb.com/api/v2`): a full set (covers, banners, logos, icons) for games it lists. It needs
    a free API key per user (their terms: each user brings their own), set in Settings → Artwork (setting
    `art.steamgriddb_key`). The key is a secret: sent only in SteamGridDB's Authorization header, never logged, never
    in a cache file name or a diagnostics bundle (diag/redact.py).

Every lookup is cached (cache dir, JSON only); images are checked with Pillow before they're stored, and WebP is
re-encoded (Steam's library takes PNG/JPEG).
"""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.parse
from pathlib import Path

from ..core import cache
from ..core.paths import write_atomic

SIDEQUEST = "https://api.sidequestvr.com/v2"
SIDEQUEST_DEFAULT_BANNER = "/assets/"  # SideQuest's placeholder banners are site assets, not the app's art
SGDB = "https://www.steamgriddb.com/api/v2"
SGDB_KEY_SETTING = "art.steamgriddb_key"
SGDB_KEY_URL = "https://www.steamgriddb.com/profile/preferences/api"
MAX_AGE = 7 * 86400
# SteamGridDB asks per kind: the dimensions Steam shows (grids) and only static, safe-for-work, serious images
SGDB_KINDS = {
    "portrait": "grids/game/{id}?dimensions=600x900&types=static&nsfw=false&humor=false",
    "landscape": "grids/game/{id}?dimensions=920x430,460x215&types=static&nsfw=false&humor=false",
    "hero": "heroes/game/{id}?types=static&nsfw=false&humor=false",
    "logo": "logos/game/{id}?types=static&nsfw=false&humor=false",
    "icon": "icons/game/{id}?types=static&nsfw=false&humor=false",
}


def _norm(s: str) -> str:
    from .sources import norm

    return norm(s)


def _key(term: str) -> str:
    from .sources import _key as key

    return key(term)


# ---- images


def image(data: bytes | None, kind: str = "") -> tuple[bytes, str] | None:
    """(bytes, extension) of a usable image, or None for anything else (an HTML outage page, an error text, a broken
    file). PNG and JPEG are kept as they are; other formats (WebP) become PNG (with transparency, and always for logos
    and icons) or JPEG."""
    if not data or data[:64].lstrip()[:1] == b"<":
        return None
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            fmt = im.format
            if fmt == "PNG":
                return data, ".png"
            if fmt == "JPEG":
                return data, ".jpg"
            alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            out = io.BytesIO()
            if alpha or kind in ("logo", "icon"):
                im.convert("RGBA" if alpha else "RGB").save(out, "PNG")
                return out.getvalue(), ".png"
            im.convert("RGB").save(out, "JPEG", quality=92)
            return out.getvalue(), ".jpg"
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError):
        return None


class Fetched:
    """A download's outcome, for messages that say what went wrong ("the server answered 404")."""

    def __init__(self, data: bytes | None = None, status: int | None = None, error: str = ""):
        self.data, self.status, self.error = data, status, error

    @property
    def reason(self) -> str:
        """'http' (an error status), 'offline' (no answer), 'not_image' (an HTML page/garbage) or '' (fine)."""
        if self.status and self.status != 200:
            return "http"
        if self.data is None:
            return "offline"
        return "" if image(self.data) else "not_image"


def download(url: str) -> Fetched:
    import requests

    try:
        r = cache.http_get(url, timeout=30)
    except requests.HTTPError as exc:
        return Fetched(status=getattr(exc.response, "status_code", None) or 0, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - offline / timeout
        return Fetched(error=type(exc).__name__)
    return Fetched(r.content, getattr(r, "status_code", 200) or 200)


def save(folder: Path, kind: str, data: bytes | None, replace: bool = False) -> bool:
    """Store `data` as <kind>.<ext> in `folder` when it is an image (and, without `replace`, only when that kind is
    missing). Returns whether it was stored."""
    if not replace and any(folder.glob(f"{kind}.*")):
        return False
    img = image(data, kind)
    if not img:
        return False
    raw, ext = img
    for old in folder.glob(f"{kind}.*"):
        old.unlink()
    (folder / f"{kind}{ext}").write_bytes(raw)
    return True


# ---- SideQuest


def sidequest_search(term: str) -> list[dict]:
    url = f"{SIDEQUEST}/apps?search={urllib.parse.quote(term)}&limit=30"
    res = cache.cached_json(f"sidequest-search-{_key(term)}.json", url, max_age=MAX_AGE, fallback=[])
    return [r for r in res or [] if isinstance(r, dict)]


def sidequest_app(apps_id) -> dict | None:
    try:
        apps_id = int(apps_id)
    except (TypeError, ValueError):
        return None
    res = cache.cached_json(f"sidequest-app-{apps_id}.json", f"{SIDEQUEST}/apps/{apps_id}", max_age=MAX_AGE,
                            fallback={})
    return res if isinstance(res, dict) and res.get("apps_id") else None


def _real_package(pkg: str | None) -> bool:
    return bool(pkg) and not str(pkg).startswith("com.autogen.")


def match_sidequest(names: list[str], package: str | None = None) -> dict | None:
    """The SideQuest app record (full, with screenshots) whose name is exactly one of `names` (normalized), for a
    Quest app. Several listings can share a name (a store title + a fan's mod): the store-synced one wins, then the
    most downloaded. A listing whose own package name is real and isn't `package` is another app."""
    for name in names:
        want = _norm(name)
        if len(want) < 3:
            continue
        hits = [r for r in sidequest_search(name)
                if _norm(r.get("name") or "") == want and r.get("supports_quest", True)]
        hits.sort(key=lambda r: (not r.get("labrador_url"), -int(r.get("downloads") or 0)))
        for hit in hits:
            app = sidequest_app(hit.get("apps_id"))
            if not app:
                continue
            pkg = app.get("packagename")
            if package and _real_package(pkg) and pkg != package:
                continue  # same name, another app (its own package name says so)
            return app
    return None


def sidequest_banner(app: dict) -> str | None:
    for field in ("app_banner", "image_url", "title_image"):
        url = app.get(field) or ""
        if url.startswith("https://") and SIDEQUEST_DEFAULT_BANNER not in url:
            return url
    return None


def sidequest_screenshots(app: dict) -> list[str]:
    return [s.get("image_url") for s in app.get("screenshots") or []
            if isinstance(s, dict) and str(s.get("image_url") or "").startswith("https://")]


def sidequest_tags(app: dict) -> list[str]:
    return [t.strip().title() for t in str(app.get("search_tags") or "").split(",") if t.strip()][:6]


# ---- SteamGridDB


def sgdb_key() -> str:
    from ..core import library

    try:
        return str(library.setting(SGDB_KEY_SETTING) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


_sgdb_lock = threading.Lock()
_sgdb_state = {"until": 0.0, "last": 0.0, "rejected": ""}
SGDB_MIN_INTERVAL = 0.35  # a few requests a second at most; a 429 pauses SteamGridDB for Retry-After (or 60 s)


class SgdbError(RuntimeError):
    """'rejected' (the key), 'limited' (rate limit) or 'offline'."""


def _sgdb_get(path: str, key: str, cache_name: str | None = None) -> dict | None:
    """GET an API path with the user's key. Answers are cached under `cache_name` (never the key); a 429 pauses all
    SteamGridDB requests, a 401/403 stops them for that key until it changes."""
    import requests

    if not key:
        return None
    if cache_name:
        p = cache.cache_dir() / cache_name
        if p.exists() and time.time() - p.stat().st_mtime < MAX_AGE:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    with _sgdb_lock:
        if _sgdb_state["rejected"] == key:
            raise SgdbError("rejected")
        now = time.time()
        if now < _sgdb_state["until"]:
            raise SgdbError("limited")
        wait = _sgdb_state["last"] + SGDB_MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _sgdb_state["last"] = time.time()
    try:
        r = cache.http_get(f"{SGDB}/{path}", timeout=20, headers={"Authorization": f"Bearer {key}"})
        data = r.json()
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", 0)
        if status in (401, 403):
            _sgdb_state["rejected"] = key
            raise SgdbError("rejected") from None
        if status == 429:
            try:
                after = float(exc.response.headers.get("Retry-After") or 60)
            except (TypeError, ValueError):
                after = 60.0
            _sgdb_state["until"] = time.time() + min(max(after, 1.0), 900.0)
            raise SgdbError("limited") from None
        if status == 404:
            data = {"success": False, "data": []}  # no such game: cached like an empty answer
        else:
            raise SgdbError("offline") from None
    except (ValueError, requests.RequestException):
        raise SgdbError("offline") from None
    if cache_name and isinstance(data, dict):
        write_atomic(cache.cache_dir() / cache_name, json.dumps(data))
    return data if isinstance(data, dict) else None


def check_sgdb_key(key: str) -> str:
    """'ok', 'rejected' or 'offline' (Settings → Artwork → Check). Never cached."""
    key = (key or "").strip()
    if not key:
        return "rejected"
    _sgdb_state["rejected"] = "" if _sgdb_state["rejected"] == key else _sgdb_state["rejected"]
    try:
        data = _sgdb_get("search/autocomplete/" + urllib.parse.quote("Beat Saber"), key)
    except SgdbError as exc:
        return "rejected" if str(exc) == "rejected" else "offline"
    return "ok" if data and data.get("success") else "offline"


def sgdb_search(term: str, key: str | None = None) -> list[dict]:
    key = sgdb_key() if key is None else key
    if not key or not term.strip():
        return []
    data = _sgdb_get("search/autocomplete/" + urllib.parse.quote(term.strip(), safe=""), key,
                     f"sgdb-search-{_key(term)}.json")
    return [g for g in (data or {}).get("data") or [] if isinstance(g, dict) and g.get("id")]


def match_sgdb(names: list[str], key: str | None = None) -> dict | None:
    """The SteamGridDB game whose name is exactly one of `names` (normalized)."""
    for name in names:
        want = _norm(name)
        if len(want) < 3:
            continue
        hit = next((g for g in sgdb_search(name, key) if _norm(g.get("name") or "") == want), None)
        if hit:
            return hit
    return None


def sgdb_images(game_id, kind: str, key: str | None = None) -> list[dict]:
    """SteamGridDB's images of one kind for a game, best first (their score order)."""
    key = sgdb_key() if key is None else key
    path = SGDB_KINDS[kind].format(id=int(game_id))
    data = _sgdb_get(path, key, f"sgdb-{kind}-{int(game_id)}.json")
    return [i for i in (data or {}).get("data") or []
            if isinstance(i, dict) and str(i.get("url") or "").startswith("https://") and not i.get("nsfw")]


def apply_sgdb(folder: Path, game_id, kinds, key: str | None = None, replace: bool = False) -> list[str]:
    """Store SteamGridDB's best image of each of `kinds` (only missing ones unless `replace`). Returns the kinds
    stored. Stops quietly at a rate limit or a rejected key (what's stored stays)."""
    key = sgdb_key() if key is None else key
    got = []
    for kind in kinds:
        if not replace and any(folder.glob(f"{kind}.*")):
            continue
        try:
            imgs = sgdb_images(game_id, kind, key)
        except SgdbError:
            break
        for img in imgs[:3]:
            if save(folder, kind, download(img["url"]).data, replace=replace):
                got.append(kind)
                break
    return got
