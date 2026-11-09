"""Store details for the game page (and Steam tags): description, genres, developer/publisher, release date, website
and screenshots.

Sources (cached 30 days; exact title matches only, like the artwork):
  - OculusDB (Meta/Oculus store data): long description, genres, publisher, website — found by Quest package or by
    the Rift app match; covers Quest and Rift games, including Oculus exclusives
  - the Steam store (games also sold there): short description, genres, developers, publishers, release date and
    screenshots
Meta's own store pages refuse automated requests, so Oculus-exclusive games get no screenshots.
Screenshots are saved as artwork/<pkg>/shot_<n>.jpg; the rest goes into the library entry's `details`.
"""
from __future__ import annotations

import html
import re
import time
import urllib.parse

from ..core import cache
from . import fetch, sources

MAX_SHOTS = 6


def _text(s: str | None) -> str:
    """Store HTML/markdown-ish text → plain paragraphs."""
    if not s:
        return ""
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def oculusdb_app(entry: dict) -> dict | None:
    """The OculusDB record of this game: by Quest package, else by the Rift match."""
    pkg = entry.get("quest_package") if entry.get("kind") == "rift" else entry["package"]
    if pkg and "." in pkg and not pkg.startswith("rift."):
        for r in sources.oculusdb_search(pkg, rift_only=False):
            if r.get("packageName") == pkg:
                return r
    if entry.get("kind") == "rift":
        extra = (entry.get("analysis") or {}).get("extra") or {}
        return sources.match_rift(entry.get("title") or "", extra.get("canonical_name"))
    return None


# Steam's VR categories: VR Support (31), VR Supported (53), VR Only (54)
STEAM_VR_CATEGORIES = frozenset({31, 53, 54})


def _steam_vr(app: dict) -> bool:
    return any(c.get("id") in STEAM_VR_CATEGORIES for c in app.get("categories") or [] if isinstance(c, dict))


def _same_company(app: dict, store: dict | None) -> bool:
    """The Steam app's developer or publisher is the Meta store app's publisher ("Crytek" / "Crytek GmbH")."""
    want = sources.norm((store or {}).get("publisher_name") or "")
    if len(want) < 3:
        return False
    names = [sources.norm(n) for n in (app.get("developers") or []) + (app.get("publishers") or []) if n]
    return any(n and (want in n or n in want) for n in names if len(n) >= 3)


def steam_app(title: str, store: dict | None = None) -> dict | None:
    """The Steam app with exactly this name, if it is the same game: a VR app, or one by the Meta store app's
    publisher (`store`, OculusDB). Common names are taken on Steam by other games (GitHub #524: "The Climb" there is
    another studio's flat game, Crytek's isn't on Steam)."""
    hit = sources.match_steam(title)
    if not hit:
        return None
    app = sources.steam_details(hit["id"])
    if app and not (_steam_vr(app) or _same_company(app, store)):
        return None
    return app


def fetch_details(entry: dict, screenshots: bool = True) -> dict:
    """Collect details for a library entry (doesn't store them; see pipeline.fetch_details)."""
    title = entry.get("title") or entry["package"]
    out: dict = {"sources": [], "fetched": time.time()}
    if ((entry.get("analysis") or {}).get("extra") or {}).get("vr_kind") == "none":
        return out  # an Android app without VR isn't on a VR store; a title match would be another product
    try:
        app = oculusdb_app(entry)
    except Exception:  # noqa: BLE001 - offline
        app = None
    if app:
        pkg = entry.get("quest_package") if entry.get("kind") == "rift" else entry["package"]
        out.setdefault("matches", {})["oculusdb"] = {
            "by": "package" if pkg and app.get("packageName") == pkg else "title",
            "key": pkg if pkg and app.get("packageName") == pkg else title,
        }
        out["sources"].append("oculusdb")
        out["description"] = _text(app.get("display_long_description"))
        out["genres"] = [g for g in app.get("genre_names") or [] if g]
        if app.get("publisher_name"):
            out["publisher"] = app["publisher_name"]
        if app.get("website_url"):
            out["website"] = app["website_url"]
        if app.get("id"):
            out["store_url"] = f"https://www.meta.com/experiences/{app['id']}/"
    try:
        st = steam_app(title, app)
    except Exception:  # noqa: BLE001
        st = None
    if st:
        out.setdefault("matches", {})["steam"] = {"by": "title", "key": title}
        out["sources"].append("steam")
        out["steam_appid"] = st.get("steam_appid")
        out["short"] = _text(st.get("short_description"))
        if not out.get("description"):
            out["description"] = _text(st.get("about_the_game") or st.get("detailed_description"))
        out["genres"] = list(dict.fromkeys((out.get("genres") or []) +
                                           [g["description"] for g in st.get("genres") or []]))
        out.setdefault("developer", ", ".join(st.get("developers") or []) or None)
        out.setdefault("publisher", ", ".join(st.get("publishers") or []) or None)
        rd = (st.get("release_date") or {}).get("date")
        if rd:
            out["release_date"] = rd
        if st.get("website"):
            out.setdefault("website", st["website"])
        out["store_url_steam"] = f"https://store.steampowered.com/app/{st.get('steam_appid')}/"
        if screenshots:
            out["screenshots"] = save_screenshots(entry["package"], [s.get("path_full") for s in
                                                                     st.get("screenshots") or []])
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def title_matched(entry: dict) -> bool:
    """Legacy Steam and Rift name matches must be refreshed after a rename."""
    details = entry.get("details") or {}
    matches = details.get("matches") or {}
    for source in details.get("sources") or []:
        if source in matches:
            if matches[source].get("by") == "title":
                return True
        elif source == "steam" or source == "oculusdb" and entry.get("kind") == "rift":
            return True
    return False


def clear_screenshots(package: str) -> None:
    d = fetch.artwork_dir(package)
    for pattern in ("shot_*.jpg", "t_shot_*"):
        for path in d.glob(pattern):
            path.unlink(missing_ok=True)


def save_screenshots(package: str, urls: list[str]) -> list[str]:
    d = fetch.artwork_dir(package)
    saved = []
    for i, url in enumerate([u for u in urls if u][:MAX_SHOTS], 1):
        path = d / f"shot_{i}.jpg"
        if not path.exists():
            try:
                r = cache.http_get(url, timeout=30)
                if r.status_code != 200 or len(r.content) < 1000:
                    continue
                path.write_bytes(r.content)
            except Exception:  # noqa: BLE001
                continue
        saved.append(path.name)
    for old in d.glob("shot_*.jpg"):
        if old.name not in saved:
            old.unlink()
    return saved


def screenshot_files(package: str) -> list:
    return sorted(fetch.artwork_dir(package).glob("shot_*.jpg"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))


def plain_description(text: str) -> str:
    """Store descriptions as plain text: OculusDB's carry Markdown and "[media]" placeholders for embedded videos."""
    import re

    text = re.sub(r"\[media\]", "", text or "")
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)        # headings
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)                  # bold
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", text)  # links
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def store_links(details: dict) -> list[tuple[str, str]]:
    out = []
    if details.get("store_url"):
        out.append(("Meta store", details["store_url"]))
    if details.get("store_url_steam"):
        out.append(("Steam store", details["store_url_steam"]))
    if details.get("website"):
        out.append((urllib.parse.urlparse(details["website"]).netloc or "Website", details["website"]))
    return out
