"""Artwork fallbacks when the Meta services are down (GitHub #510): the image service's 404/HTML answers, then the
Steam store, SideQuest and SteamGridDB by exact name (mocked HTTP), the picker's candidates and failure messages,
SideQuest store details, and the SteamGridDB key as a secret."""
import base64
import io
import json
import zipfile

import pytest
import requests

from frameport.artwork import details, fetch, sources, stores
from frameport.core import cache, library


def img(w=600, h=900, color=(40, 80, 160), fmt="PNG") -> bytes:
    from PIL import Image

    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, fmt)
    return b.getvalue()


HTML = b"<!DOCTYPE html><html><body>502 Bad Gateway</body></html>" + b" " * 300


class Resp:
    def __init__(self, content=b"", status=200, data=None, headers=None):
        self.content, self.status_code, self._data, self.headers = content, status, data, headers or {}
        self.text = content.decode("latin-1") if isinstance(content, bytes) else str(content)

    def json(self):
        if self._data is None:
            return json.loads(self.content)
        return self._data


def _http_error(status, headers=None):
    resp = requests.Response()
    resp.status_code = status
    resp.headers.update(headers or {})
    return requests.HTTPError(f"{status} Error", response=resp)


@pytest.fixture
def web(monkeypatch):
    """A fake internet: `routes` maps URL substrings to a Resp, bytes, dict/list (JSON) or an int (an error status).
    Every request is recorded in `calls` as (url, headers)."""
    state = {"routes": {}, "calls": []}

    def http_get(url, timeout=20, **kw):
        state["calls"].append((url, dict(kw.get("headers") or {})))
        for part, answer in sorted(state["routes"].items(), key=lambda kv: -len(kv[0])):
            if part in url:
                if callable(answer):
                    answer = answer(url, kw)
                if isinstance(answer, int):
                    raise _http_error(answer)
                if isinstance(answer, Exception):
                    raise answer
                if isinstance(answer, Resp):
                    return answer
                if isinstance(answer, bytes):
                    return Resp(answer)
                return Resp(json.dumps(answer).encode(), data=answer)
        raise _http_error(404)
    monkeypatch.setattr(cache, "http_get", http_get)
    monkeypatch.setattr(stores, "SGDB_MIN_INTERVAL", 0)
    stores._sgdb_state.update(until=0.0, last=0.0, rejected="")
    return state


def add_game(pkg, title, **extra):
    library.upsert_game(pkg, title=title, **extra)


def stems(pkg):
    return {p.stem for p in fetch.files(pkg)}


def urls(web, part):
    return [u for u, _ in web["calls"] if part in u]


STEAM_VR = {"success": True, "data": {"steam_appid": 77, "name": "Moon Rider",
                                      "categories": [{"id": 54, "description": "VR Only"}]}}


def steam_routes(web, name="Moon Rider", vr=True):
    web["routes"]["storesearch"] = {"items": [{"id": 77, "name": name}]}
    app = json.loads(json.dumps(STEAM_VR))
    if not vr:
        app["data"]["categories"] = [{"id": 2, "description": "Single-player"}]
    web["routes"]["appdetails?appids=77"] = {"77": app}
    for kind, file in sources.STEAM_FILES.items():
        web["routes"][f"/apps/77/{file}"] = img(*((600, 900) if kind == "portrait" else (920, 430)))


def sidequest_routes(web, name="Moon Rider", package="com.autogen.1617", banner="https://cdn.sidequestvr.com/f/b.webp"):
    for old in cache.cache_dir().glob("sidequest-*"):  # (answers are cached for days)
        old.unlink()
    web["routes"]["api.sidequestvr.com/v2/apps?search="] = [
        {"apps_id": 5, "name": name, "app_banner": banner, "labrador_url": "https://www.oculus.com/experiences/quest/42/",
         "supports_quest": True, "downloads": 10},
        {"apps_id": 6, "name": f"{name} 2", "app_banner": "https://cdn.sidequestvr.com/f/other.webp"}]
    web["routes"]["api.sidequestvr.com/v2/apps/5"] = {
        "apps_id": 5, "name": name, "packagename": package, "app_banner": banner,
        "description": "Ride the moon.\n\nWith friends.", "search_tags": "rhythm,music",
        "oculus_auto_generated_creator": "Moon Studio", "website": "https://moon.example",
        "labrador_url": "https://www.oculus.com/experiences/quest/42/?forced_locale=en_US",
        "screenshots": [{"image_url": "https://cdn.sidequestvr.com/f/s1.webp"}]}
    web["routes"][banner] = img(1280, 720, fmt="WEBP")
    web["routes"]["/f/s1.webp"] = img(1280, 720, (200, 10, 10), fmt="WEBP")


# ------------------------------------------------------------------------------------------ the image service


def test_image_service_404_and_html_count_as_nothing_and_are_asked_again(web):
    add_game("com.moon.rider", "Moon Rider")
    web["routes"]["by_package"] = 404
    fetch.fetch("com.moon.rider")
    assert not sources.has_art("com.moon.rider")
    web["routes"]["by_package"] = Resp(HTML)  # an outage page
    fetch.fetch("com.moon.rider")
    # images that aren't images (an HTML page base64-encoded) aren't stored either
    web["routes"]["by_package"] = {"status": "ok", "displayName": "Moon Rider", "images": [
        {"image_type": "APP_IMG_COVER_PORTRAIT", "uri": base64.b64encode(HTML).decode()}]}
    fetch.fetch("com.moon.rider")
    assert not sources.has_art("com.moon.rider") and not fetch.source("com.moon.rider")
    # the service is back: the next fetch uses it (nothing was remembered as "no art")
    web["routes"]["by_package"] = {"status": "ok", "displayName": "Moon Rider", "images": [
        {"image_type": "APP_IMG_COVER_PORTRAIT", "uri": base64.b64encode(img()).decode()}]}
    fetch.fetch("com.moon.rider")
    assert "portrait" in stems("com.moon.rider") and fetch.source("com.moon.rider") == "meta"
    assert len(urls(web, "by_package")) == 4


# ------------------------------------------------------------------------------------------ by name


def test_steam_by_exact_name_when_meta_is_down(web):
    add_game("com.moon.rider", "Moon Rider")
    steam_routes(web)
    sidequest_routes(web)
    d = fetch.artwork_dir("com.moon.rider")
    (d / "cover.jpg").write_bytes(img())  # FramePort's generated cover goes once store art exists
    fetch.fetch("com.moon.rider")
    assert stems("com.moon.rider") == {"portrait", "hero", "landscape", "logo"}
    assert fetch.source("com.moon.rider") == "steam"
    assert not urls(web, "sidequest")  # Steam had a cover: SideQuest isn't asked


def test_steam_needs_a_vr_app_and_the_exact_name(web):
    """GitHub #524: a common name on Steam is often another (flat) game."""
    add_game("com.moon.rider", "Moon Rider")
    steam_routes(web, vr=False)
    fetch.fetch("com.moon.rider")
    assert not sources.has_art("com.moon.rider")
    steam_routes(web, name="Moon Rider 2")  # a sequel is another game
    add_game("com.moon.rider2", "Moon Rider")
    fetch.fetch("com.moon.rider2")
    assert not sources.has_art("com.moon.rider2")


def test_sidequest_banner_by_exact_name(web):
    add_game("com.moon.rider", "Moon Rider")
    sidequest_routes(web)
    d = fetch.artwork_dir("com.moon.rider")
    (d / "banner.jpg").write_bytes(img(1600, 600))
    fetch.fetch("com.moon.rider")
    files = {p.name for p in fetch.files("com.moon.rider")}
    assert "landscape.jpg" in files  # WebP re-encoded (Steam takes PNG/JPEG)
    assert "cover.jpg" in files and "banner.jpg" not in files  # a tall card cover made from the banner
    assert fetch.source("com.moon.rider") == "sidequest"
    assert not urls(web, "/f/other.webp")  # "Moon Rider 2" is another game
    from PIL import Image

    with Image.open(d / "cover.jpg") as im:
        assert im.size == (600, 900)


def test_sidequest_never_takes_another_apps_listing(web):
    """A listing whose own (real, not com.autogen.*) package name is another app's is another app with that name."""
    add_game("com.moon.rider", "Moon Rider")
    sidequest_routes(web, package="com.someone.else")
    fetch.fetch("com.moon.rider")
    assert not sources.has_art("com.moon.rider")
    sidequest_routes(web, package="com.moon.rider")  # its own package: fine
    fetch.fetch("com.moon.rider")
    assert "landscape" in stems("com.moon.rider")


def test_sidequest_placeholder_banner_is_not_art(web):
    add_game("com.moon.rider", "Moon Rider")
    sidequest_routes(web, banner="/assets/images/Discord-Banner.jpg")
    fetch.fetch("com.moon.rider")
    assert not sources.has_art("com.moon.rider")


def test_picked_art_and_2d_apps_get_no_lookups(web):
    add_game("com.moon.rider", "Moon Rider")
    steam_routes(web)
    sidequest_routes(web)
    d = fetch.artwork_dir("com.moon.rider")
    (d / "icon.png").write_bytes(img(256, 256))
    (d / fetch.PICKED).write_text("custom")
    fetch.fetch("com.moon.rider")
    assert stems("com.moon.rider") == {"icon"} and not web["calls"]
    add_game("com.flat.app", "Moon Rider", analysis={"extra": {"vr_kind": "none"}})
    fetch.fetch("com.flat.app")
    assert not sources.has_art("com.flat.app") and not urls(web, "storesearch") and not urls(web, "sidequest")


def test_only_missing_kinds_are_filled(web):
    add_game("com.moon.rider", "Moon Rider")
    web["routes"]["by_package"] = {"status": "ok", "displayName": "Moon Rider", "images": [
        {"image_type": "APP_IMG_ICON", "uri": base64.b64encode(img(64, 64, (1, 2, 3))).decode()}]}
    steam_routes(web)
    fetch.fetch("com.moon.rider")
    d = fetch.artwork_dir("com.moon.rider")
    assert stems("com.moon.rider") == {"portrait", "hero", "landscape", "logo", "icon"}
    assert fetch.source("com.moon.rider") == "meta,steam"
    before = (d / "portrait.png").read_bytes()
    web["routes"][f"/apps/77/{sources.STEAM_FILES['portrait']}"] = img(600, 900, (9, 9, 9))
    fetch.fetch("com.moon.rider")
    assert (d / "portrait.png").read_bytes() == before


def test_refresh_keeps_the_old_art_when_nothing_is_found(web):
    add_game("com.moon.rider", "Moon Rider")
    d = fetch.artwork_dir("com.moon.rider")
    (d / "portrait.jpg").write_bytes(img(fmt="JPEG"))
    (d / "t_portrait_400_abcd.jpg").write_bytes(b"thumb")
    fetch.fetch("com.moon.rider", refresh=True)
    assert (d / "portrait.jpg").exists() and (d / "t_portrait_400_abcd.jpg").exists()
    sidequest_routes(web)  # now there's art: it replaces the old set (and its thumbnails)
    fetch.fetch("com.moon.rider", refresh=True)
    assert stems("com.moon.rider") == {"landscape", "cover"}
    assert not (d / "t_portrait_400_abcd.jpg").exists()
    assert not any(p.name.startswith(".refresh") for p in d.parent.iterdir())


# ------------------------------------------------------------------------------------------ SteamGridDB


def sgdb_routes(web, name="Moon Rider"):
    web["routes"]["steamgriddb.com/api/v2/search/autocomplete/"] = {
        "success": True, "data": [{"id": 9, "name": name}, {"id": 10, "name": f"{name}: Deluxe"}]}
    for kind in stores.SGDB_KINDS:
        web["routes"]["/api/v2/" + stores.SGDB_KINDS[kind].format(id=9)] = {"success": True, "data": [
            {"id": 1, "url": f"https://cdn2.steamgriddb.com/{kind}.png", "thumb": f"https://cdn2.steamgriddb.com/t_{kind}.png"}]}
        web["routes"][f"cdn2.steamgriddb.com/{kind}.png"] = img(512, 512)


def test_steamgriddb_needs_a_key(web):
    add_game("com.moon.rider", "Moon Rider")
    sgdb_routes(web)
    fetch.fetch("com.moon.rider")
    assert not urls(web, "steamgriddb") and not sources.has_art("com.moon.rider")


def test_steamgriddb_fills_missing_kinds_with_the_users_key(web):
    key = "0123456789abcdef0123456789abcdef"
    library.set_setting(stores.SGDB_KEY_SETTING, key)
    add_game("com.moon.rider", "Moon Rider")
    sgdb_routes(web)
    fetch.fetch("com.moon.rider")
    assert stems("com.moon.rider") == {"portrait", "landscape", "hero", "logo", "icon"}
    assert fetch.source("com.moon.rider") == "steamgriddb"
    api = [(u, h) for u, h in web["calls"] if "/api/v2/" in u]
    assert api and all(h == {"Authorization": f"Bearer {key}"} for _, h in api)
    assert not any(key in u for u, _ in web["calls"])  # never in a URL
    assert not any(key in p.name for p in cache.cache_dir().iterdir())  # nor in a cache file name
    assert all(not h for u, h in web["calls"] if "/api/v2/" not in u)  # images: no key sent to the CDN
    assert not urls(web, "/10")  # "Moon Rider: Deluxe" isn't the exact name


def test_steamgriddb_rate_limit_and_rejected_key(web, monkeypatch):
    library.set_setting(stores.SGDB_KEY_SETTING, "badkey")
    add_game("com.moon.rider", "Moon Rider")
    web["routes"]["/api/v2/"] = 401
    fetch.fetch("com.moon.rider")
    n = len(urls(web, "/api/v2/"))
    assert n == 1  # a rejected key stops SteamGridDB for that key
    fetch.fetch("com.moon.rider")
    assert len(urls(web, "/api/v2/")) == n
    assert stores.check_sgdb_key("badkey") == "rejected"
    # a new key: asked again; a 429 pauses every request until Retry-After
    library.set_setting(stores.SGDB_KEY_SETTING, "otherkey")

    def limited(url, kw):
        raise _http_error(429, {"Retry-After": "120"})
    web["routes"]["/api/v2/"] = limited
    fetch.fetch("com.moon.rider")
    n = len(urls(web, "/api/v2/"))
    fetch.fetch("com.moon.rider")
    assert len(urls(web, "/api/v2/")) == n and stores._sgdb_state["until"] > 0


def test_check_key(web):
    web["routes"]["/api/v2/search/autocomplete/"] = {"success": True, "data": []}
    assert stores.check_sgdb_key("goodkey") == "ok"
    web["routes"]["/api/v2/search/autocomplete/"] = requests.ConnectionError("down")
    assert stores.check_sgdb_key("goodkey2") == "offline"
    assert stores.check_sgdb_key("") == "rejected"


# ------------------------------------------------------------------------------------------ the picker


def test_picker_offers_steam_sidequest_and_steamgriddb(web, monkeypatch):
    monkeypatch.setattr(sources, "oculusdb_search", lambda *a, **k: [])
    web["routes"]["storesearch"] = {"items": [{"id": 77, "name": "Moon Rider"}]}
    sidequest_routes(web)
    found = sources.search("Moon Rider")
    assert [r["source"] for r in found] == ["Steam", "SideQuest", "SideQuest"]  # no key: no SteamGridDB
    library.set_setting(stores.SGDB_KEY_SETTING, "k")
    sgdb_routes(web)
    found = sources.search("Moon Rider")
    sg = [r for r in found if r["source"] == "SteamGridDB"]
    assert sg[0]["id"] == 9 and sg[0]["preview"].endswith("t_portrait.png")


def test_picking_a_sidequest_result(web):
    sidequest_routes(web)
    d = fetch.artwork_dir("com.x")
    (d / "portrait.jpg").write_bytes(b"old")
    choice = next(r for r in sources.search("Moon Rider") if r["source"] == "SideQuest")
    assert sources.pick("com.x", choice) is None
    assert {p.stem for p in fetch.files("com.x")} == {"landscape", "cover"}
    assert (d / fetch.PICKED).read_text() == "SideQuest"


def test_picking_a_steamgriddb_result(web):
    library.set_setting(stores.SGDB_KEY_SETTING, "k")
    sgdb_routes(web)
    assert sources.pick("com.x", {"source": "SteamGridDB", "id": 9, "name": "Moon Rider"}) is None
    assert {p.stem for p in fetch.files("com.x")} == {"portrait", "landscape", "hero", "logo", "icon"}


def test_a_failed_pick_says_why_and_names_the_source(web):
    """The TMNT case: the picked result's image was gone; the message said only "couldn't download"."""
    from frameport.ui.views.art_dialog import pick_failure_text

    d = fetch.artwork_dir("com.x")
    (d / "portrait.jpg").write_bytes(b"old")
    web["routes"]["/apps/88/"] = 404
    web["routes"]["appdetails?appids=88"] = {"88": STEAM_VR}
    choice = {"source": "Steam", "id": 88, "name": "TMNT",
              "preview": sources.STEAM_CDN.format(id=88, file="header.jpg")}
    failed = sources.pick("com.x", choice)
    assert (failed.source, failed.reason, failed.status) == ("Steam", "http", 404)
    assert (d / "portrait.jpg").read_bytes() == b"old" and not sources.apply_choice("com.x", choice)
    text = pick_failure_text(failed)
    assert "Steam" in text and "404" in text
    web["routes"]["/apps/88/"] = Resp(HTML)
    failed = sources.pick("com.x", choice)
    assert failed.reason == "not_image" and "web page" in pick_failure_text(failed)
    web["routes"]["/apps/88/"] = requests.ConnectionError("offline")
    assert sources.pick("com.x", choice).reason == "offline"


# ------------------------------------------------------------------------------------------ store details


def test_sidequest_details_when_oculusdb_has_nothing(web, monkeypatch):
    monkeypatch.setattr(sources, "oculusdb_search", lambda *a, **k: [])
    monkeypatch.setattr(details, "steam_app", lambda title, store=None: None)
    sidequest_routes(web)
    d = details.fetch_details({"package": "com.moon.rider", "title": "Moon Rider"})
    assert d["sources"] == ["sidequest"] and d["description"].startswith("Ride the moon.")
    assert d["genres"] == ["Rhythm", "Music"] and d["publisher"] == "Moon Studio"
    assert d["store_url"] == "https://www.meta.com/experiences/42/"
    assert d["screenshots"] == ["shot_1.jpg"]
    assert (fetch.artwork_dir("com.moon.rider") / "shot_1.jpg").read_bytes()[:3] == b"\xff\xd8\xff"  # WebP → JPEG
    assert ("SideQuest", "https://sidequestvr.com/app/5") in details.store_links(d)
    # another name (a sequel) or another app's listing: nothing
    assert not details.fetch_details({"package": "com.moon.rider", "title": "Moon Rider 3"}).get("sources")
    sidequest_routes(web, package="com.other.app")
    assert not details.fetch_details({"package": "com.moon.rider", "title": "Moon Rider"}).get("sources")


def test_no_sidequest_details_when_oculusdb_answers_or_for_pc_games(web, monkeypatch):
    monkeypatch.setattr(details, "steam_app", lambda title, store=None: None)
    sidequest_routes(web)
    monkeypatch.setattr(sources, "oculusdb_search", lambda *a, **k: [
        {"packageName": "com.moon.rider", "appName": "Moon Rider", "display_long_description": "From OculusDB."}])
    assert details.fetch_details({"package": "com.moon.rider", "title": "Moon Rider"})["sources"] == ["oculusdb"]
    monkeypatch.setattr(sources, "oculusdb_search", lambda *a, **k: [])
    monkeypatch.setattr(sources, "match_rift", lambda *a, **k: None)
    assert not details.fetch_details({"package": "rift.moon", "kind": "rift", "title": "Moon Rider"}).get("sources")
    assert not urls(web, "sidequest")


# ------------------------------------------------------------------------------------------ the key is a secret


def test_steamgriddb_key_is_redacted(tmp_path):
    from frameport.diag import bundle, redact

    key = "fedcba9876543210fedcba9876543210"
    library.set_setting(stores.SGDB_KEY_SETTING, key)
    red = redact.default()
    assert key not in red.text(f"GET with Bearer {key} failed") and "<secret>" in red.text(key)
    assert redact.scrub_settings({stores.SGDB_KEY_SETTING: key, "ui.theme": "portal"}) == {
        stores.SGDB_KEY_SETTING: "<secret>", "ui.theme": "portal"}
    assert redact.scrub_settings({stores.SGDB_KEY_SETTING: ""})[stores.SGDB_KEY_SETTING] == ""
    out = bundle.collect(None, None, dest=tmp_path / "d.zip")
    with zipfile.ZipFile(out) as z:
        everything = b"".join(z.read(n) for n in z.namelist())
        settings = json.loads(z.read("app/settings.json"))["settings"]
    assert key.encode() not in everything and settings[stores.SGDB_KEY_SETTING] == "<secret>"
