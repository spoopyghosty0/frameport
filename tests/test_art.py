"""Artwork for Rift games (mocked network), thumbnails, Quest/Rift twin titles, uninstall."""
import io
import zipfile

import pytest

from frameport.artwork import fetch, sources, thumbs
from frameport.core import library


def png(w=800, h=1200, color=(40, 80, 160)) -> bytes:
    from PIL import Image

    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


class Resp:
    def __init__(self, content=b"", status=200, data=None):
        self.content, self.status_code, self._data = content, status, data

    def json(self):
        return self._data


@pytest.fixture
def net(monkeypatch):
    """Fake OculusDB / Steam / Meta art: search results by URL substring, images by URL."""
    db = {"rift": [], "all": [], "steam": [], "images": {}, "meta": {}}

    def cached_json(name, url, max_age=0, fallback=None):
        if "oculusdb" in url:
            term = url.split("/search/")[1].split("?")[0]
            rows = db["rift"] if "headsets=RIFT" in url else db["all"]
            return [r for r in rows if r.get("_term", "").lower() in term.lower().replace("%20", " ")]
        if "appdetails" in url:
            appid = url.split("appids=")[1].split("&")[0]
            return {appid: {"success": True, "data": {"steam_appid": int(appid)}}}
        if "storesearch" in url:
            return {"items": db["steam"]}
        return fallback

    def http_get(url, timeout=30):
        if "by_package" in url:
            pkg = url.split("package=")[1]
            if pkg in db["meta"]:
                return Resp(data={"status": "ok", "displayName": "Meta name", "images": [
                    {"image_type": "APP_IMG_COVER_PORTRAIT", "uri": __import__("base64").b64encode(png()).decode()}]})
            return Resp(data={"status": "not found"})
        if url in db["images"]:
            return Resp(db["images"][url])
        return Resp(b"", 404)
    monkeypatch.setattr(sources.cache, "cached_json", cached_json)
    monkeypatch.setattr(sources.cache, "http_get", http_get)
    monkeypatch.setattr(fetch.cache, "http_get", http_get)
    return db


def test_norm_and_terms():
    assert sources.norm("Asgard's Wrath") == sources.norm("Asgards Wrath")
    assert sources.norm("Lone Echo II") == sources.norm("Lone Echo 2")
    assert sources.norm("Vader Immortal: Episode III") == sources.norm("Vader Immortal - Episode III")
    assert "Asgard" in sources.search_terms("Asgards Wrath")


def test_quest_version_rules(net):
    net["all"] = [{"_term": "robo recall", "appName": "Robo Recall: Unplugged", "packageName": "com.YC.RoboRecall"},
                  {"_term": "the climb", "appName": "The Climb 2", "packageName": "com.crytek.climb2"},
                  {"_term": "chronos", "appName": "Chrono Strike", "packageName": "com.greensky.chronostrike"}]
    assert sources.match_quest("Robo Recall") == "com.YC.RoboRecall"
    assert sources.match_quest("The Climb") is None  # a sequel isn't the Quest version
    assert sources.match_quest("Chronos") is None


def test_rift_art_chain(net):
    # 1. OculusDB square cover, exact (apostrophe-insensitive) match found through a shorter search term
    net["rift"] = [{"_term": "asgard", "appName": "Asgard's Wrath", "id": "111", "imageLink": "/cdn/images/111",
                    "canonicalName": "sanzaru-asgards-wrath"}]
    net["images"][sources.OCULUSDB + "/cdn/images/111"] = png(720, 720)
    found = sources.fetch_rift("rift.asgards_wrath", "Asgards Wrath")
    assert found["source"] == "oculusdb" and found["title"] == "Asgard's Wrath"
    assert {p.stem for p in fetch.files("rift.asgards_wrath")} == {"square"}
    # 2. the Quest version's full Meta art wins when there is one
    net["all"] = [{"_term": "robo recall", "appName": "Robo Recall: Unplugged", "packageName": "com.YC.RoboRecall"}]
    net["meta"]["com.YC.RoboRecall"] = True
    found = sources.fetch_rift("rift.robo_recall", "Robo Recall")
    assert found["source"] == "meta" and found["quest_package"] == "com.YC.RoboRecall"
    # 3. Steam, exact names only ("Stormlander" isn't "Stormland")
    net["steam"] = [{"id": 1, "name": "Stormlander"}, {"id": 469610, "name": "Rick and Morty: Virtual Rick-ality"}]
    for f in sources.STEAM_FILES.values():
        net["images"][sources.STEAM_CDN.format(id=469610, file=f)] = png()
    assert sources.fetch_rift("rift.stormland", "Stormland").get("source") is None
    found = sources.fetch_rift("rift.rm", "Rick and Morty - Virtual Rick-ality")
    assert found["source"] == "steam" and {"portrait", "hero", "landscape", "logo"} <= \
        {p.stem for p in fetch.files("rift.rm")}


def test_exe_icon_fallback(net, tmp_path, monkeypatch):
    exe = tmp_path / "g.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr("frameport.analysis.rift.exe_icon", lambda p: png(64, 64))
    assert sources.fetch_rift("rift.none", "Totally Unknown Game", exe=exe)["source"] == "icon"


def test_thumbnails_and_urls():
    d = fetch.artwork_dir("com.x")
    (d / "portrait.png").write_bytes(png(1000, 1500))
    url = thumbs.url("com.x", ("portrait",), 400)
    assert url.startswith("/artwork/com.x/t_portrait_400_") and url.endswith(".jpg")
    from PIL import Image

    t = next(d.glob("t_portrait_400_*.jpg"))
    assert Image.open(t).size == (400, 600)
    assert thumbs.url("com.x", ("portrait",), 400) == url  # cached
    assert "t_portrait" not in "".join(p.name for p in fetch.files("com.x"))  # thumbnails aren't artwork kinds


def test_twin_titles():
    from frameport.core.titles import counterparts, display_title, twins

    games = [{"package": "com.YC.RoboRecall", "title": "Robo Recall"},
             {"package": "rift.robo_recall", "kind": "rift", "title": "Robo Recall", "quest_package": None},
             {"package": "rift.lone_echo", "kind": "rift", "title": "Lone Echo"},
             {"package": "com.x.vader", "title": "Vader Immortal: Episode I"},
             {"package": "rift.vader1", "kind": "rift", "title": "Vader Immortal - Episode I",
              "quest_package": "com.x.vader"}]
    tw = twins(games)
    assert tw == {"com.YC.RoboRecall", "rift.robo_recall", "com.x.vader", "rift.vader1"}
    assert display_title(games[0], tw) == "Robo Recall (Quest)" and display_title(games[1], tw) == "Robo Recall (Rift)"
    assert display_title(games[2], tw) == "Lone Echo"
    assert [g["package"] for g in counterparts(games[3], games)] == ["rift.vader1"]


def test_steam_title_uses_twin_suffix():
    from frameport import pipeline

    library.upsert_game("com.YC.RoboRecall", title="Robo Recall")
    library.upsert_game("rift.robo_recall", kind="rift", title="Robo Recall")
    assert pipeline.steam_title(library.game("rift.robo_recall")) == "Robo Recall (Rift)"


# ------------------------------------------------------------------------------------------ uninstall
def test_uninstall_backs_up_keys_and_removes_data(tmp_path, monkeypatch):
    from frameport import uninstall
    from frameport.core.events import Reporter
    from frameport.core.paths import user_data_dir

    data = user_data_dir()
    ks = data / "overport-workspace" / "signatures"
    ks.mkdir(parents=True)
    (ks / "com.x.keystore").write_bytes(b"key")
    (data / "ssh").mkdir()
    (data / "ssh" / "id_ed25519").write_text("secret")
    library.upsert_game("com.x", title="X")
    monkeypatch.setattr(uninstall, "wsl_revive_copy", lambda: None)
    pl = uninstall.plan()
    assert pl.keys and pl.items[0].path == str(data)
    out = uninstall.run(Reporter(), backup_dir=tmp_path / "backup")
    z = zipfile.ZipFile(out["backup"])
    assert {"overport-workspace/signatures/com.x.keystore", "ssh/id_ed25519", "README.txt"} <= set(z.namelist())
    assert not data.exists()
    # what still runs after the uninstall (the job's log, a late setting, the poll) must not bring the folder back
    from frameport.core import applog

    applog.save_job_log("uninstall-app", None, "done", "log")
    library.set_setting("ui.scale", 1.0)
    user_data_dir()
    assert not data.exists()


def test_uninstall_keeps_foreign_files_in_a_custom_data_folder(tmp_path):
    from frameport import uninstall
    from frameport.core.paths import user_data_dir

    data = user_data_dir()  # tests run with FRAMEPORT_HOME: a folder the user chose, maybe shared
    library.upsert_game("com.x", title="X")
    (data / "tools").mkdir()
    (data / "my-notes.txt").write_text("not FramePort's")
    assert uninstall.remove_data_dir(data) == ["my-notes.txt"]
    assert (data / "my-notes.txt").exists() and not (data / "library.json").exists()


# ------------------------------------------------------------------------------------------ details / Steam art
def test_details_from_oculusdb_and_steam(net, monkeypatch):
    from frameport import pipeline
    from frameport.artwork import details

    net["all"] = [{"_term": "com.x.game", "appName": "Game", "packageName": "com.x.game", "id": "9",
                   "display_long_description": "Fight <b>robots</b>.<br/>Save the day.", "genre_names": ["Action"],
                   "publisher_name": "Pub", "website_url": "https://game.example"}]
    net["steam"] = [{"id": 5, "name": "Game"}]
    steam = {"5": {"success": True, "data": {"steam_appid": 5, "short_description": "Robots!",
                                             "genres": [{"description": "Action"}, {"description": "Indie"}],
                                             "developers": ["Dev"], "publishers": ["Pub"],
                                             "release_date": {"date": "Apr 20, 2017"},
                                             "screenshots": [{"path_full": "https://img/ss1.jpg"},
                                                             {"path_full": "https://img/ss2.jpg"}]}}}
    real = sources.cache.cached_json
    monkeypatch.setattr(details.cache, "cached_json",
                        lambda name, url, **k: steam if "appdetails" in url else real(name, url, **k))
    net["images"]["https://img/ss1.jpg"] = png(1920, 1080)
    net["images"]["https://img/ss2.jpg"] = png(1920, 1080)
    monkeypatch.setattr(details.cache, "http_get", sources.cache.http_get)
    library.upsert_game("com.x.game", title="Game")
    d = pipeline.fetch_details("com.x.game")
    assert d["description"] == "Fight robots.\nSave the day." and d["genres"] == ["Action", "Indie"]
    assert (d["developer"], d["publisher"], d["release_date"]) == ("Dev", "Pub", "Apr 20, 2017")
    assert d["screenshots"] == ["shot_1.jpg", "shot_2.jpg"] and sorted(d["sources"]) == ["oculusdb", "steam"]
    assert [x[0] for x in details.store_links(d)] == ["Meta store", "Steam store", "game.example"]
    from frameport.ui.views.library import auto_tags

    assert "Indie" in auto_tags(library.game("com.x.game"))


def test_steam_set_from_square_cover_and_screenshot():
    from PIL import Image

    from frameport.artwork import steam

    d = fetch.artwork_dir("rift.sq")
    (d / "square.webp").write_bytes(png(720, 720))
    (d / "shot_1.jpg").write_bytes(png(1920, 1080))
    art = steam.steam_set("rift.sq")
    assert set(art) == {"portrait", "landscape", "hero", "icon"}
    assert Image.open(art["portrait"]).size == (600, 900) and Image.open(art["hero"]).size == (1920, 620)
    assert Image.open(art["landscape"]).size == (920, 430) and Image.open(art["icon"]).size == (256, 256)
    assert steam.steam_set("rift.sq") == art  # cached


def test_steam_tags():
    from frameport.artwork.steam import steam_tags

    rift = {"kind": "rift", "details": {"genres": ["Action", "Shooter"]}, "tags": ["Favorite"],
            "analysis": {"extra": {"needs_revive": True}}}
    assert steam_tags(rift) == ["PC VR on Frame", "Oculus Rift", "Action", "Shooter", "Favorite"]
    assert steam_tags(rift, "pc")[:2] == ["FramePort PC VR", "Oculus Rift"]
    assert steam_tags({"kind": "rift"})[:2] == ["PC VR on Frame", "PC VR"]  # an OpenXR/SteamVR game
    assert steam_tags({"package": "com.x"})[:2] == ["Quest on Frame", "Meta Quest"]
    assert steam_tags({"package": "org.flat", "analysis": {"extra": {"vr_kind": "none"}}})[:2] == \
        ["Android on Frame", "Android"]


def test_plain_description_drops_store_markup():
    from frameport.artwork.details import plain_description

    text = "[media]\n\n# Become The Knight.\n\nIt's **bold** and [a link](https://x.invalid).\n\n\n\n[media]\n\n**Hard**"
    assert plain_description(text) == "Become The Knight.\n\nIt's bold and a link.\n\nHard"


def test_steam_placeholder_for_apps_without_artwork(tmp_path):
    """No store art: a name-on-colour set (with the APK's launcher icon), so Steam shows the name, not a blank tile."""
    import zipfile

    from PIL import Image

    from frameport.artwork import fetch, steam

    pkg = "org.example.flat"
    assert steam.steam_set(pkg) == {}  # no art and no title: nothing to make
    icon = tmp_path / "icon.png"
    Image.new("RGBA", (96, 96), (200, 30, 30, 255)).save(icon)
    apk = tmp_path / "app.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.write(icon, "res/mipmap-xxxhdpi-v4/ic_launcher.png")
    art = steam.steam_set(pkg, title="A Flat Little App", apk=apk)
    assert set(art) == {"portrait", "landscape", "hero", "logo", "icon"}
    with Image.open(art["portrait"]) as im:
        assert im.size == (600, 900)
    with Image.open(art["icon"]) as im:  # the APK's icon is used
        assert im.convert("RGB").getpixel((128, 128)) == (200, 30, 30)
    with Image.open(art["logo"]) as im:
        assert im.mode == "RGBA" and im.getbbox()
    assert steam.steam_set(pkg, title="A Flat Little App", apk=apk) == art  # cached
    (fetch.artwork_dir(pkg) / "portrait.png").write_bytes(icon.read_bytes())  # real art arrives: no placeholder
    assert "logo" not in steam.steam_set(pkg, title="A Flat Little App", apk=apk)


def test_no_store_details_for_android_apps_without_vr(monkeypatch):
    from frameport.artwork import details

    monkeypatch.setattr(details, "steam_app",
                        lambda title, store=None: (_ for _ in ()).throw(AssertionError("looked up")))
    entry = {"package": "org.example.flat", "title": "2048", "analysis": {"extra": {"vr_kind": "none"}}}
    assert details.fetch_details(entry)["sources"] == []


def test_picked_art_survives_installs(monkeypatch):
    """A pick that lacks a kind (Steam: no icon) used to be overwritten by the store art at the next install."""
    import base64
    import io
    from types import SimpleNamespace

    from PIL import Image

    from frameport.artwork import fetch

    def png(color):
        b = io.BytesIO()
        Image.new("RGB", (8, 8), color).save(b, "PNG")
        return b.getvalue()

    store = {"status": "ok", "displayName": "G", "images": [
        {"image_type": t, "uri": base64.b64encode(png("red")).decode()}
        for t in ("APP_IMG_COVER_PORTRAIT", "APP_IMG_COVER_LANDSCAPE", "APP_IMG_HERO", "APP_IMG_ICON")]}
    monkeypatch.setattr(fetch.cache, "http_get", lambda url, timeout=30: SimpleNamespace(json=lambda: store))
    d = fetch.artwork_dir("com.picked")
    (d / "portrait.png").write_bytes(png("blue"))  # the user's pick (no icon)
    (d / fetch.PICKED).write_text("Steam")
    fetch.fetch("com.picked")  # what an install does
    assert (d / "portrait.png").read_bytes() == png("blue") and not (d / "icon.png").exists()
    (d / fetch.PICKED).unlink()  # without a pick: only missing kinds are filled, nothing is replaced
    fetch.fetch("com.picked")
    assert (d / "portrait.png").read_bytes() == png("blue") and (d / "icon.png").exists()
    fetch.fetch("com.picked", refresh=True)  # "Find automatically": the store art replaces everything
    assert (d / "portrait.png").read_bytes() == png("red")



def test_generated_cover_still_gets_a_steam_placeholder():
    """The library's generated cover/banner (apps without store art, e.g. WiiCompiled) aren't store art: Steam still
    gets the placeholder set (it used to get nothing at all, a blank tile in the Frame's library)."""
    from PIL import Image

    from frameport.artwork import fetch, steam

    pkg = "org.example.generated"
    d = fetch.artwork_dir(pkg)
    d.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (600, 900)).save(d / "cover.jpg")
    Image.new("RGB", (920, 430)).save(d / "banner.jpg")
    art = steam.steam_set(pkg, title="Generated Cover App")
    assert {"portrait", "landscape", "hero"} <= set(art)

def test_apps_without_store_art_get_a_cover(tmp_path):
    """FramePort's library shows a cover (name + APK icon) for apps no store knows; store art always wins."""
    import zipfile

    from PIL import Image

    from frameport.artwork import fetch, steam, thumbs
    from frameport.core import library

    icon = tmp_path / "icon.png"
    Image.new("RGBA", (96, 96), (10, 200, 30, 255)).save(icon)
    apk = tmp_path / "app.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.write(icon, "res/mipmap-xxxhdpi-v4/ic_launcher.png")
    library.upsert_game("org.example.flat", title="Flat App", apk=str(apk))
    cover = steam.ensure_cover("org.example.flat")
    assert cover and cover.name == "cover.jpg" and (fetch.artwork_dir("org.example.flat") / "icon.png").exists()
    assert thumbs.pick("org.example.flat", ("portrait", "square", "cover", "icon")).name == "cover.jpg"
    Image.new("RGB", (60, 90)).save(fetch.artwork_dir("org.example.flat") / "portrait.jpg")  # store art arrives
    assert steam.ensure_cover("org.example.flat") is None
    assert thumbs.pick("org.example.flat", ("portrait", "square", "cover", "icon")).name == "portrait.jpg"


def test_converted_copies_are_removed_after_installing(tmp_path, monkeypatch):
    """Converted APKs live in FramePort's output folder only until they're on the Frame; the user's files stay."""
    from frameport import pipeline
    from frameport.core import library
    from frameport.core.paths import output_dir

    own = tmp_path / "mine.apk"
    own.write_bytes(b"x")
    out = output_dir() / "Game"
    out.mkdir(parents=True)
    (out / "com.g.apk").write_bytes(b"y" * 10)
    (out / "com.g.alt-noforcequit.apk").write_bytes(b"z" * 5)
    library.upsert_game("com.g", build={"apk": str(out / "com.g.apk"),
                                        "alt_apk": str(out / "com.g.alt-noforcequit.apk")})
    library.upsert_game("com.mine", build={"apk": str(own)})  # "install as is": the build is the user's own file
    assert pipeline.remove_converted_copies("com.mine") == 0 and own.exists()
    library.set_setting("build.keep_copies", True)
    assert pipeline.remove_converted_copies("com.g") == 0
    library.set_setting("build.keep_copies", False)
    assert pipeline.remove_converted_copies("com.g") == 15 and not out.exists()


def test_custom_art_replaces_one_kind_and_is_kept(tmp_path):
    from PIL import Image

    pkg = "com.custom.art"
    d = fetch.artwork_dir(pkg)
    (d / "portrait.jpg").write_bytes(png(600, 900))
    (d / "icon.png").write_bytes(png(128, 128))
    (d / "cover.jpg").write_bytes(png(600, 900))  # FramePort's generated cover (no store art)
    src = tmp_path / "mine.png"
    Image.new("RGB", (1000, 1500), (200, 10, 10)).save(src)
    out = sources.apply_custom(pkg, "portrait", src)
    assert out.name == "portrait.jpg" and Image.open(out).size == (1000, 1500)  # no alpha → JPEG
    assert (d / "icon.png").exists() and not (d / "cover.jpg").exists()  # only that kind is replaced
    assert (d / fetch.PICKED).read_text() == "custom"  # automatic fetches skip it

    logo = tmp_path / "logo.webp"
    Image.new("RGBA", (5000, 1000), (0, 0, 0, 0)).save(logo)
    out = sources.apply_custom(pkg, "logo", logo)
    assert out.name == "logo.png" and max(Image.open(out).size) == sources.CUSTOM_MAX_PX  # scaled down, PNG

    thumbs.thumb(d / "icon.png", 96)
    sources.remove_custom(pkg, "icon")
    assert not list(d.glob("icon.*")) and not list(d.glob("t_icon_*"))


@pytest.mark.parametrize("make, why", [
    (lambda p: p.write_bytes(b"not an image"), "isn't an image"),
    (lambda p: __import__("PIL.Image").Image.new("RGB", (20, 20)).save(p, "PNG"), "too small"),
])
def test_custom_art_rejects_bad_files(tmp_path, make, why):
    bad = tmp_path / "bad.png"
    make(bad)
    with pytest.raises(sources.ArtError, match=why):
        sources.apply_custom("com.custom.bad", "portrait", bad)
    with pytest.raises(sources.ArtError):
        sources.apply_custom("com.custom.bad", "screenshot", bad)


def test_quest_cover_from_oculusdb_when_the_image_service_has_none(net):
    """GitHub #510: the image service had nothing for Assassin's Creed Nexus (it answers 404 for every package at
    times); OculusDB's app with exactly that package gives the cover. Another package's app never does."""
    pkg = "com.Ubisoft.ACNexusVR"
    net["all"] = [{"_term": pkg, "appName": "Assassin's Creed Nexus VR", "packageName": pkg, "id": "58",
                   "imageLink": "/cdn/images/58"},
                  {"_term": pkg, "appName": "Something else", "packageName": "com.other", "id": "59",
                   "imageLink": "/cdn/images/59"}]
    net["images"][sources.OCULUSDB + "/cdn/images/58"] = png(720, 720)
    d = fetch.artwork_dir(pkg)
    (d / "cover.jpg").write_bytes(png(600, 900))  # FramePort's generated cover goes once store art exists
    fetch.fetch(pkg)
    assert {p.stem for p in fetch.files(pkg)} == {"square"} and sources.has_art(pkg)
    # the image service has art: no OculusDB lookup
    net["meta"]["com.y.game"] = True
    net["all"] = [{"_term": "com.y.game", "packageName": "com.y.game", "id": "60", "imageLink": "/cdn/images/60"}]
    net["images"][sources.OCULUSDB + "/cdn/images/60"] = png(720, 720)
    fetch.fetch("com.y.game")
    assert {p.stem for p in fetch.files("com.y.game")} == {"portrait"}
    # no exact package match: nothing
    fetch.fetch("com.other.game")
    assert not sources.has_art("com.other.game")


def test_an_outage_page_is_not_a_cover(net):
    net["images"][sources.OCULUSDB + "/cdn/images/7"] = b"<html>" + b"x" * 400 + b"</html>"
    assert not sources.apply_oculusdb("com.z", {"id": "7"})
    assert not sources.has_art("com.z")


def test_cached_json_keeps_the_last_good_answer(monkeypatch):
    """GitHub #510: OculusDB's outage redirects to an HTML page answering 200; it was cached for 30 days in place of
    the search results (and lookups found nothing until it expired)."""
    from frameport.core import cache

    answers = ["<html>outage</html>", '[{"id": 1}]', "<html>outage</html>"]
    calls = []

    class R:
        def __init__(self, text):
            self.text = text

    def get(url, timeout=20):
        calls.append(url)
        return R(answers[len(calls) - 1])

    monkeypatch.setattr(cache, "http_get", get)
    assert cache.cached_json("t.json", "u", max_age=3600, fallback=[]) == []
    assert not (cache.cache_dir() / "t.json").exists()
    assert cache.cached_json("t.json", "u", max_age=3600, fallback=[]) == [{"id": 1}]
    assert cache.cached_json("t.json", "u", max_age=3600, fallback=[]) == [{"id": 1}] and len(calls) == 2  # fresh
    assert cache.cached_json("t.json", "u", max_age=0, fallback=[]) == [{"id": 1}]  # stale, outage: the last copy
    (cache.cache_dir() / "p.json").write_text("<html>cached by an older version</html>", encoding="utf-8")
    answers.append('{"ok": true}')
    assert cache.cached_json("p.json", "u", max_age=3600) == {"ok": True}  # not JSON: fetched again


def _steam_match(monkeypatch, app):
    monkeypatch.setattr(sources, "match_steam", lambda title: {"id": 1, "name": title})
    monkeypatch.setattr(sources, "steam_details", lambda appid: app)


def test_steam_details_need_a_vr_app_or_the_same_company(monkeypatch):
    """GitHub #524: Steam's "The Climb" is another studio's flat game (Crytek's isn't on Steam): its description and
    developer showed on The Climb's About tab."""
    from frameport.artwork import details

    crytek = {"packageName": "com.crytek.climb", "appName": "The Climb", "publisher_name": "Crytek", "id": "23",
              "display_long_description": "Climb mountains."}
    monkeypatch.setattr(sources, "oculusdb_search", lambda *a, **k: [crytek])
    other = {"steam_appid": 1, "developers": ["PiggyBanx Inc."], "publishers": ["PiggyBanx Inc."],
             "categories": [{"id": 2, "description": "Single-player"}], "about_the_game": "Another game."}
    _steam_match(monkeypatch, other)
    d = details.fetch_details({"package": "com.crytek.climb", "title": "The Climb"})
    assert d["sources"] == ["oculusdb"] and d["description"] == "Climb mountains." and "developer" not in d
    # the same game on Steam: a VR app, or (flat) by the store app's publisher
    _steam_match(monkeypatch, {**other, "categories": [{"id": 54, "description": "VR Only"}]})
    assert details.fetch_details({"package": "com.crytek.climb", "title": "The Climb"})["sources"] == [
        "oculusdb", "steam"]
    _steam_match(monkeypatch, {**other, "developers": ["Crytek GmbH"], "publishers": ["Crytek"]})
    assert "steam" in details.fetch_details({"package": "com.crytek.climb", "title": "The Climb"})["sources"]
    # OculusDB unreachable (and SideQuest has nothing): only a VR app on Steam is taken
    from frameport.artwork import stores

    monkeypatch.setattr(sources, "oculusdb_search", lambda *a, **k: [])
    monkeypatch.setattr(stores, "sidequest_search", lambda term: [])
    _steam_match(monkeypatch, other)
    assert not details.fetch_details({"package": "com.crytek.climb", "title": "The Climb"}).get("sources")
