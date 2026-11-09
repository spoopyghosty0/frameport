"""Finding Quest games and their data (OBB) folders on disk (sources/quest_dump.py)."""
import zipfile
from pathlib import Path

import pytest

from frameport.sources import quest_dump

IDS: dict[str, tuple[str, int]] = {}  # APK file name -> (package, versionCode) for names that don't say it


@pytest.fixture(autouse=True)
def package_from_file_name(monkeypatch):
    """com.x.game.apk -> (com.x.game, None); com.x.game_5.apk -> (com.x.game, 5); else IDS."""
    def ids(apk):
        name = Path(apk).name
        if name in IDS:
            return IDS[name]
        pkg, _, vc = Path(apk).stem.partition("_")
        return pkg, int(vc) if vc.isdigit() else None
    IDS.clear()
    monkeypatch.setattr(quest_dump, "_apk_ids", ids)


def apk(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", b"x")
    return path


def obb(folder: Path, name: str = "main.1.com.x.game.obb") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"o")
    return folder


def test_existing_layouts(tmp_path):
    a = tmp_path / "Game A v1 -TAG"
    apk(a / "com.x.game.apk")
    obb(a / "com.x.game")
    g = quest_dump.from_path(a)
    assert (g.name, g.data_dir, g.origin) == ("Game A v1", a / "com.x.game", a)
    b = tmp_path / "patched"
    apk(b / "com.x.game.apk")
    obb(b / "obb")
    assert quest_dump.from_path(b).data_dir == b / "obb"
    assert quest_dump.from_path(b / "com.x.game.apk").data_dir == b / "obb"  # a single APK file
    c = tmp_path / "no data"
    apk(c / "com.x.game.apk")
    assert quest_dump.from_path(c).data_dir is None


@pytest.mark.parametrize("data", ["obb/com.x.game", "OBB/com.x.game", "obbs/com.x.game", "Android/obb/com.x.game",
                                  "backup/com.x.game"])
def test_package_folder_below_the_apk(tmp_path, data):
    """The data folder is the one holding the .obb files (its contents go to Android/obb/<package>/)."""
    game = tmp_path / "Game"
    apk(game / "com.x.game.apk")
    obb(game / data)
    found = quest_dump.scan(tmp_path)
    assert len(found) == 1 and found[0].data_dir == game / data and found[0].name == "Game"


@pytest.mark.parametrize("apk_folder", ["apk", "APKs"])
def test_sidequest_style_backup_is_one_game(tmp_path, apk_folder):
    game = tmp_path / "Backups" / "Some Game"
    apk(game / apk_folder / "com.x.game.apk")
    obb(game / "obb" / "com.x.game")
    found = quest_dump.scan(tmp_path / "Backups")
    assert len(found) == 1
    g = found[0]
    assert (g.name, g.origin, g.data_dir) == ("Some Game", game, game / "obb" / "com.x.game")
    assert quest_dump.from_path(g.apk).data_dir == game / "obb" / "com.x.game"  # the APK file alone too


def test_other_games_obb_is_never_taken(tmp_path):
    """A neighbouring folder with its own APK is another game (e.g. another version): its data stays its own."""
    v1, v2 = tmp_path / "Game v1", tmp_path / "Game v2"
    apk(v1 / "com.x.game.apk")
    apk(v2 / "com.x.game.apk")
    obb(v2 / "data" / "com.x.game")
    found = {g.origin.name: g for g in quest_dump.scan(tmp_path)}
    assert found["Game v1"].data_dir is None
    assert found["Game v2"].data_dir == v2 / "data" / "com.x.game"


def test_package_folder_without_obb_files_is_ignored_when_searched(tmp_path):
    game = tmp_path / "Game"
    apk(game / "apk" / "com.x.game.apk")
    (game / "saves" / "com.x.game").mkdir(parents=True)
    (game / "saves" / "com.x.game" / "save.dat").write_bytes(b"s")
    assert quest_dump.from_path(game / "apk").data_dir is None


@pytest.mark.parametrize("apk_folder", ["apk", "apks", "APK"])
@pytest.mark.parametrize("data_folder", ["obb", "OBB", "obbs", "obb/com.coatsink.alone"])
def test_sidequest_raw_assets_without_expansion_files(tmp_path, apk_folder, data_folder):
    """GitHub #239: Aftermath's SideQuest backup has raw FMOD banks, no .obb file or package subfolder."""
    game = tmp_path / "com.coatsink.alone" / "2026-10-12T17-31-23-687Z"
    a = apk(game / apk_folder / "com.coatsink.alone.apk")
    data = game / data_folder
    data.mkdir(parents=True)
    (data / "Master.bank").write_bytes(b"bank")
    (data / "Master.strings.bank").write_bytes(b"strings")
    (game / "manifest.json").write_text("{}")
    found = quest_dump.scan(tmp_path)
    assert len(found) == 1
    g = found[0]
    assert (g.name, g.origin, g.apk, g.data_dir, g.data_files) == (
        "com.coatsink.alone", game, a, data, None)
    assert g.data_bytes() == 11
    assert quest_dump.from_path(a).data_dir == data


def test_raw_assets_in_a_neighbouring_backup_are_not_taken(tmp_path):
    game = tmp_path / "com.coatsink.alone"
    a = apk(game / "backup1" / "apk" / "com.coatsink.alone.apk")
    apk(game / "backup2" / "apk" / "com.coatsink.alone.apk")
    data = game / "backup2" / "obb"
    data.mkdir()
    (data / "Master.bank").write_bytes(b"bank")
    assert quest_dump.from_path(a).data_dir is None


@pytest.mark.parametrize("data_folder", ["obb", "obbs"])
def test_sibling_expansion_files_keep_package_and_version_selection(tmp_path, data_folder):
    from frameport import pipeline
    from frameport.core import library
    from frameport.install.installer import local_data_manifest

    a = apk(tmp_path / "apks" / "com.x.game_7.apk")
    data = tmp_path / data_folder
    for name in ("main.5.com.x.game.obb", "main.7.com.x.game.obb", "main.8.com.x.game.obb",
                 "patch.6.com.x.game.obb", "patch.8.com.x.game.obb", "main.7.com.y.other.obb"):
        obb(data, name)
    (data / "readme.txt").write_text("keep")
    g = quest_dump.from_path(a)
    assert g.data_dir == data
    assert g.data_files == ["main.7.com.x.game.obb", "patch.6.com.x.game.obb"]
    assert local_data_manifest(g.data_dir, g.data_files) == {
        "main.7.com.x.game.obb": 1, "patch.6.com.x.game.obb": 1}
    # The other game has not been added: deletion must protect its files through selection, not library sharing.
    library.upsert_game("com.x.game", apk=str(a), data_dir=str(g.data_dir), data_files=g.data_files)
    assert set(pipeline.local_game_files("com.x.game")) == {
        a, data / "main.7.com.x.game.obb", data / "patch.6.com.x.game.obb"}
    assert tmp_path.resolve() in data.resolve().parents
    pipeline.delete_local_files("com.x.game")
    assert not a.exists() and not (data / "main.7.com.x.game.obb").exists()
    assert not (data / "patch.6.com.x.game.obb").exists()
    assert {f.name for f in data.iterdir()} == {
        "main.5.com.x.game.obb", "main.8.com.x.game.obb", "patch.8.com.x.game.obb",
        "main.7.com.y.other.obb", "readme.txt"}


def test_sibling_expansion_files_of_another_package_are_ignored(tmp_path):
    a = apk(tmp_path / "apk" / "com.x.game.apk")
    obb(tmp_path / "obb", "main.1.com.y.other.obb")
    assert quest_dump.from_path(a).data_dir is None


# ------------------------------------------------------------------ expansion files found by name (GitHub #85/#91)
def test_obbs_next_to_the_apk_are_sent_alone(tmp_path):
    """APK and OBBs in one folder: one game; only its own .obb files are its data (the APK, notes and other packages'
    OBBs stay on the PC)."""
    from frameport.install.installer import local_data_manifest

    game = tmp_path / "Games" / "TRIANGLE"
    apk(game / "com.x.game_7.apk")
    obb(game, "main.7.com.x.game.obb")
    obb(game, "patch.7.COM.X.GAME.obb")  # Windows copies change the case
    obb(game, "main.3.com.y.other.obb")
    (game / "readme.txt").write_text("hi")
    found = quest_dump.scan(tmp_path / "Games")
    assert len(found) == 1
    g = found[0]
    assert (g.name, g.data_dir, g.data_files) == ("TRIANGLE", game, ["main.7.com.x.game.obb", "patch.7.COM.X.GAME.obb"])
    assert g.data_bytes() == 2
    assert local_data_manifest(g.data_dir, g.data_files) == {"main.7.com.x.game.obb": 1, "patch.7.COM.X.GAME.obb": 1}
    assert quest_dump.from_path(g.apk).data_files == g.data_files  # the APK file alone too


def test_other_expansion_kinds_next_to_the_apk(tmp_path):
    """GitHub #415: Crisis Brigade 2 ships office.<versionCode>.<package>.obb next to main/patch; it wasn't sent."""
    game = tmp_path / "Games" / "CB2"
    apk(game / "com.x.game_7.apk")
    obb(game, "main.7.com.x.game.obb")
    obb(game, "office.5.com.x.game.obb")
    obb(game, "office.3.com.x.game.obb")  # an older one of the same kind isn't sent
    obb(game, "office.5.com.y.other.obb")  # another package's isn't either
    g = quest_dump.scan(tmp_path / "Games")[0]
    assert g.data_files == ["main.7.com.x.game.obb", "office.5.com.x.game.obb"]


def test_unreal_chunk_obbs_next_to_the_apk(tmp_path):
    """GitHub #471: Asgard's Wrath 2 has 81 patch.pakchunk<N>-Android_ASTC.<package>.obb files next to main/patch.0;
    only main and patch.0 were sent and the game hung at engine start-up."""
    game = tmp_path / "Games" / "AW2"
    apk(game / "com.x.game_7.apk")
    obb(game, "main.7.com.x.game.obb")
    obb(game, "patch.0.com.x.game.obb")
    obb(game, "patch.pakchunk0-Android_ASTC.com.x.game.obb")
    obb(game, "patch.pakchunk27_s1-Android_ASTC.com.x.game.obb")
    obb(game, "patch.pakchunk1-Android_ASTC.com.y.other.obb")  # another package's isn't sent
    obb(game, "patch.extra.y.com.x.game.obb")  # nor a name with a dot before the package
    g = quest_dump.scan(tmp_path / "Games")[0]
    assert sorted(g.data_files) == ["main.7.com.x.game.obb", "patch.0.com.x.game.obb",
                                    "patch.pakchunk0-Android_ASTC.com.x.game.obb",
                                    "patch.pakchunk27_s1-Android_ASTC.com.x.game.obb"]


def test_obb_folder_next_to_an_apk_folder_inside_a_package_folder(tmp_path):
    """<package>/apk/x.apk + <package>/obb/main.N.<package>.obb: the obb folder isn't named after the package."""
    root = tmp_path / "Quest"
    apk(root / "com.x.game" / "apk" / "game.apk")
    IDS["game.apk"] = ("com.x.game", 12)
    obb(root / "com.x.game" / "obb", "main.12.com.x.game.obb")
    found = quest_dump.scan(root)
    assert len(found) == 1
    g = found[0]
    assert (g.origin, g.data_dir, g.data_files) == (root / "com.x.game", root / "com.x.game" / "obb", None)


@pytest.mark.parametrize("obb_at", ["obb", "obbs", "Android/obb/com.crytek.climb", "."])
def test_sidequest_versionless_obb(tmp_path, obb_at):
    """GitHub #238: CryEngine also opens main.<package>.obb, without a versionCode."""
    backup = tmp_path / "com.crytek.climb" / "2026-10-12T16-44-24-860Z"
    a = apk(backup / "apk" / "com.crytek.climb.apk")
    IDS[a.name] = ("com.crytek.climb", 666)
    obb(backup / obb_at, "main.com.crytek.climb.obb")
    [g] = quest_dump.scan(tmp_path)
    assert (g.apk, g.data_dir) == (a, backup / obb_at)
    assert g.data_files == (["main.com.crytek.climb.obb"] if obb_at == "." else None)
    assert quest_dump.from_path(a).data_dir == g.data_dir


def test_versionless_obbs_select_only_the_package_and_prefer_usable_versions(tmp_path):
    apk(tmp_path / "com.x.game.apk")
    for name in ("main.COM.X.GAME.obb", "patch.com.x.game.obb", "main.5.com.x.game.obb",
                 "main.8.com.x.game.obb", "main.com.y.other.obb"):
        obb(tmp_path, name)
    assert quest_dump.find_data(tmp_path, "com.x.game", 6)[1] == ["main.5.com.x.game.obb", "patch.com.x.game.obb"]
    assert quest_dump.find_data(tmp_path, "com.x.game", 1)[1] == ["main.COM.X.GAME.obb", "patch.com.x.game.obb"]
    assert quest_dump.find_data(tmp_path, "com.x.game", None)[1] == ["main.8.com.x.game.obb", "patch.com.x.game.obb"]


@pytest.mark.parametrize("apk_folder,obb_folder", [("apk", "obb"), ("APKs", "OBBs")])
def test_sidequest_backup_keeps_asset_files_and_language_packs(tmp_path, apk_folder, obb_folder):
    from frameport import pipeline
    from frameport.analysis import langpacks
    from frameport.install.installer import local_data_manifest

    backup = tmp_path / "com.x.game" / "2026-10-12T16-35-27-760Z"
    a = apk(backup / apk_folder / "com.x.game_7.apk")
    data = obb(backup / obb_folder, "main.7.com.x.game.obb")
    (data / "Season1Taco-Android_ASTC.pak").write_bytes(b"content")
    (data / "WwiseBanksCoreTaco-Android_ASTC.pak").write_bytes(b"audio")
    (data / "de.lang").write_bytes(b"language")
    for g in (quest_dump.from_path(a), *quest_dump.scan(tmp_path)):
        assert g.data_dir == data and g.data_files is None
        assert g.data_bytes() == 21
        assert local_data_manifest(g.data_dir, g.data_files) == {
            "main.7.com.x.game.obb": 1, "Season1Taco-Android_ASTC.pak": 7,
            "WwiseBanksCoreTaco-Android_ASTC.pak": 5, "de.lang": 8,
        }
        assert langpacks.find_content_files(pipeline._data_folder(g)) == [
            "Season1Taco-Android_ASTC.pak", "WwiseBanksCoreTaco-Android_ASTC.pak",
        ]
        assert langpacks.find_tags(pipeline._data_folder(g)) == ["de"]


@pytest.mark.parametrize("other_folder", [".", "com.y.other", "nested/com.y.other"])
def test_shared_sibling_obb_folder_keeps_other_games_assets_out(tmp_path, other_folder):
    from frameport.install.installer import local_data_manifest

    a = apk(tmp_path / "apk" / "com.x.game_7.apk")
    data = obb(tmp_path / "obb", "main.7.com.x.game.obb")
    other = obb(data / other_folder, "main.3.com.y.other.obb")
    (other / "other.pak").write_bytes(b"other game's content")
    g = quest_dump.from_path(a)
    assert g.data_dir == data and g.data_files == ["main.7.com.x.game.obb"]
    assert local_data_manifest(g.data_dir, g.data_files) == {"main.7.com.x.game.obb": 1}


@pytest.mark.parametrize("nested_obb", ["main.6.com.x.game.obb", "main.7.com.x.game.obb"])
def test_sibling_obb_folder_does_not_include_nested_expansion_files(tmp_path, nested_obb):
    a = apk(tmp_path / "apk" / "com.x.game_7.apk")
    data = obb(tmp_path / "obb", "main.7.com.x.game.obb")
    obb(data / "old", nested_obb)
    assert quest_dump.from_path(a).data_files == ["main.7.com.x.game.obb"]


def test_sidequest_backup_keeps_nested_assets(tmp_path):
    from frameport.install.installer import local_data_manifest

    a = apk(tmp_path / "apk" / "com.x.game_7.apk")
    data = obb(tmp_path / "obb", "main.7.com.x.game.obb")
    (data / "content").mkdir()
    (data / "content" / "extra.pak").write_bytes(b"content")
    g = quest_dump.from_path(a)
    assert g.data_files is None
    assert local_data_manifest(g.data_dir, g.data_files) == {"main.7.com.x.game.obb": 1, "content/extra.pak": 7}


def test_app_update_recovers_assets_for_an_old_sidequest_entry(tmp_path):
    from frameport.core import library

    a = apk(tmp_path / "apk" / "com.x.game_7.apk")
    data = obb(tmp_path / "obb", "main.7.com.x.game.obb")
    (data / "Season1Taco-Android_ASTC.pak").write_bytes(b"content")
    g = {"apk": str(a), "data_dir": str(data), "data_files": ["main.7.com.x.game.obb"], "data_bytes": 1,
         "recipe": {"source": "user", "patches": {"adapter.scale": {"value": 0.8}}}}
    analysis = {"package": "com.x.game", "extra": {"lang_packs": [], "asset_files": []}}
    assert library._refresh_data_fields(g, analysis)
    assert g["data_files"] is None and g["data_bytes"] == 8
    assert analysis["extra"]["data_bytes"] == 8
    assert analysis["extra"]["asset_files"] == ["Season1Taco-Android_ASTC.pak"]
    assert g["recipe"] == {"source": "user", "patches": {"adapter.scale": {"value": 0.8}}}
    assert not library._refresh_data_fields(g, analysis)


@pytest.mark.parametrize("other_folder", [".", "com.y.other"])
def test_app_update_does_not_expand_a_shared_or_manual_data_selection(tmp_path, other_folder):
    from frameport.core import library

    a = apk(tmp_path / "apk" / "com.x.game_7.apk")
    data = obb(tmp_path / "obb", "main.7.com.x.game.obb")
    obb(data / other_folder, "main.3.com.y.other.obb")
    g = {"apk": str(a), "data_dir": str(data), "data_files": ["main.7.com.x.game.obb"]}
    analysis = {"package": "com.x.game", "extra": {"lang_packs": [], "asset_files": []}}
    assert not library._refresh_data_fields(g, analysis)
    assert g["data_files"] == ["main.7.com.x.game.obb"]
    other = obb(tmp_path / "manual", "main.7.com.x.game.obb")
    g["data_dir"] = str(other)
    assert not library._refresh_data_fields(g, analysis)
    assert g["data_files"] == ["main.7.com.x.game.obb"]


@pytest.mark.parametrize("unavailable", ["apk", "data", "both"])
@pytest.mark.parametrize("recipe_source", ["user", "heuristics"])
def test_app_update_retries_asset_recovery_after_backup_returns(tmp_path, monkeypatch, unavailable, recipe_source):
    from copy import deepcopy

    from frameport import __version__
    from frameport.core import library
    from frameport.recommend import catalog

    monkeypatch.setattr(library, "REFRESH_ON_UPDATE", True)
    monkeypatch.setattr(catalog, "lookup", lambda _: None)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(library.time, "time", lambda: clock[0])
    monkeypatch.setattr(library, "_last_data_retry", 0.0)
    a = tmp_path / "apk" / "com.x.game_7.apk"
    data = tmp_path / "obb"
    if unavailable == "data":
        apk(a)
    if unavailable == "apk":
        obb(data, "main.7.com.x.game.obb")
    recipe = {"package": "com.x.game", "source": recipe_source,
              "patches": {"adapter.scale": {"value": 0.8}}}
    g = {"apk": str(a), "data_dir": str(data), "data_files": ["main.7.com.x.game.obb"], "data_bytes": 1,
         "recipe": recipe, "analysis": {"package": "com.x.game", "engine": "Unreal", "abis": ["arm64-v8a"],
                                        "libs": ["libovrplatformloader.so"],
                                        "extra": {"lang_packs": [], "asset_files": [], "asset_file_api": True}}}
    stored = {"settings": {"recipes.app_version": "0.0.1"}, "games": {"com.x.game": g}}
    assert library._follow_catalog(stored)
    assert stored["settings"]["recipes.app_version"] == __version__
    assert g["data_refresh_pending"] and g["data_files"] == ["main.7.com.x.game.obb"]
    # Persist the pending state, as an update followed by another app start would.
    library.save(stored)
    stored = library.load()
    g = stored["games"]["com.x.game"]
    assert g["data_refresh_pending"]
    recipe_before_retry = deepcopy(g["recipe"])
    apk(a)
    obb(data, "main.7.com.x.game.obb")
    (data / "content.pak").write_bytes(b"content")
    (data / "de.lang").write_bytes(b"language")
    assert not library._follow_catalog(stored)  # frequent library reads don't repeatedly probe the drive
    clock[0] += 601
    assert library._follow_catalog(stored)
    assert g["data_files"] is None and g["data_bytes"] == 16
    assert g["analysis"]["extra"]["asset_files"] == ["content.pak"]
    assert g["analysis"]["extra"]["lang_packs"] == ["de"]
    assert "data_refresh_pending" not in g
    if recipe_source == "user":
        assert g["recipe"] == recipe_before_retry
        assert g["recipe"]["patches"]["adapter.scale"] == {"value": 0.8}
    else:
        assert "frame.asset_files" in g["recipe"]["patches"]
    clock[0] += 601
    assert not library._follow_catalog(stored)  # completed recovery is not repeated


@pytest.mark.parametrize("pending", [False, True])
def test_asset_recovery_retries_a_directory_discovery_failure(tmp_path, monkeypatch, pending):
    from frameport.core import library

    a = apk(tmp_path / "apk" / "com.x.game_7.apk")
    data = obb(tmp_path / "obb", "main.7.com.x.game.obb")
    (data / "content.pak").write_bytes(b"content")
    (data / "de.lang").write_bytes(b"language")
    g = {"apk": str(a), "data_dir": str(data), "data_files": ["main.7.com.x.game.obb"]}
    if pending:
        g["data_refresh_pending"] = True
    analysis = {"package": "com.x.game", "extra": {"lang_packs": [], "asset_files": []}}
    scandir = quest_dump.os.scandir

    def unreadable(path):
        if Path(path) == data:
            raise PermissionError("backup directory temporarily unreadable")
        return scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(quest_dump.os, "scandir", unreadable)
        assert library._refresh_data_fields(g, analysis) is (not pending)
        assert g["data_refresh_pending"] and g["data_files"] == ["main.7.com.x.game.obb"]
        assert analysis["extra"] == {"lang_packs": [], "asset_files": []}
        assert not library._refresh_data_fields(g, analysis)
        # Ordinary scans stay tolerant of unreadable backup folders.
        assert quest_dump.find_data(a.parent, "com.x.game", 7) == (None, None)
    assert library._refresh_data_fields(g, analysis)
    assert g["data_files"] is None and "data_refresh_pending" not in g
    assert g["data_bytes"] == 16
    assert analysis["extra"]["asset_files"] == ["content.pak"]
    assert analysis["extra"]["lang_packs"] == ["de"]


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_asset_recovery_retries_an_unreadable_nested_folder(tmp_path, monkeypatch, shared, pending):
    from frameport.core import library

    a = apk(tmp_path / "apk" / "com.x.game_7.apk")
    data = obb(tmp_path / "obb", "main.7.com.x.game.obb")
    # Below the bounded OBB discovery search: the whole-folder check must still read this directory.
    nested = data / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "content.pak").write_bytes(b"content")
    if shared:
        obb(nested, "main.3.com.y.other.obb")
    files = ["main.7.com.x.game.obb"]
    g = {"apk": str(a), "data_dir": str(data), "data_files": files, "data_bytes": 1}
    if pending:
        g["data_refresh_pending"] = True
    analysis = {"package": "com.x.game", "extra": {"lang_packs": [], "asset_files": []}}
    scandir = quest_dump.os.scandir

    def unreadable(path):
        if Path(path) == nested:
            raise PermissionError("nested backup directory temporarily unreadable")
        return scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(quest_dump.os, "scandir", unreadable)
        with pytest.raises(PermissionError):
            quest_dump.find_data(a.parent, "com.x.game", 7, strict=True)
        assert quest_dump.find_data(a.parent, "com.x.game", 7) == (data, files)
        assert library._refresh_data_fields(g, analysis) is (not pending)
        assert g["data_refresh_pending"] and g["data_files"] == files and g["data_bytes"] == 1
        assert analysis["extra"] == {"lang_packs": [], "asset_files": []}
        assert not library._refresh_data_fields(g, analysis)
    assert library._refresh_data_fields(g, analysis)
    assert "data_refresh_pending" not in g
    assert g["data_files"] == (files if shared else None)
    assert g["data_bytes"] == (1 if shared else 8)
    assert analysis["extra"]["asset_files"] == ([] if shared else ["content.pak"])


def test_asset_recovery_retries_a_transient_read_failure(tmp_path, monkeypatch):
    from frameport.core import library

    a = apk(tmp_path / "apk" / "com.x.game_7.apk")
    data = obb(tmp_path / "obb", "main.7.com.x.game.obb")
    (data / "content.pak").write_bytes(b"content")
    g = {"apk": str(a), "data_dir": str(data), "data_files": ["main.7.com.x.game.obb"]}
    analysis = {"package": "com.x.game", "extra": {"lang_packs": [], "asset_files": []}}

    def unreadable(_):
        raise PermissionError("backup temporarily unreadable")

    with monkeypatch.context() as patch:
        patch.setattr(library, "data_manifest", unreadable)
        assert library._refresh_data_fields(g, analysis)
        assert g["data_refresh_pending"] and g["data_files"] == ["main.7.com.x.game.obb"]
        assert not library._refresh_data_fields(g, analysis)
    assert library._refresh_data_fields(g, analysis)
    assert g["data_files"] is None and "data_refresh_pending" not in g
    assert analysis["extra"]["asset_files"] == ["content.pak"]


@pytest.mark.parametrize("obb_at", ["obbs", "obb/2026-10-07T21-11-04.426Z", "Android/obb/com.x.game", "."])
def test_sidequest_backup_with_timestamped_apks(tmp_path, obb_at):
    """SideQuest backups name APKs <timestamp>_<versionCode>.apk; where the OBB sits varies."""
    root = tmp_path / "backups" / "com.x.game"
    a = apk(root / "apks" / "2026-10-07T21-11-04.426Z_3244131.apk")
    IDS[a.name] = ("com.x.game", 3244131)
    obb(root / obb_at, "main.3244131.com.x.game.obb")
    [g] = quest_dump.scan(tmp_path / "backups")
    assert (g.apk, g.data_dir) == (a, root / obb_at)
    # next to the apks folder: only the file
    assert g.data_files == (["main.3244131.com.x.game.obb"] if obb_at == "." else None)


def test_two_versions_side_by_side(tmp_path):
    folder = tmp_path / "apks"
    apk(folder / "com.x.game_5.apk")
    apk(folder / "com.x.game_6.apk")
    for name in ("main.5.com.x.game.obb", "main.6.com.x.game.obb", "patch.6.com.x.game.obb"):
        obb(folder, name)
    assert quest_dump.from_path(folder / "com.x.game_6.apk").data_files == ["main.6.com.x.game.obb",
                                                                             "patch.6.com.x.game.obb"]
    assert quest_dump.from_path(folder / "com.x.game_5.apk").data_files == ["main.5.com.x.game.obb"]
    # a version without its own files: the newest older ones (expansion files keep their first versionCode)
    assert quest_dump.find_data(folder, "com.x.game", 9)[1] == ["main.6.com.x.game.obb", "patch.6.com.x.game.obb"]
    assert quest_dump.find_data(folder, "com.x.game", 1)[1] == ["main.6.com.x.game.obb", "patch.6.com.x.game.obb"]
    assert quest_dump.find_data(folder, "com.x.game", None)[1] == ["main.6.com.x.game.obb", "patch.6.com.x.game.obb"]


@pytest.mark.parametrize("older_folder", ["old", "obb", "OBBs"])
def test_the_folder_with_the_apks_own_version_wins(tmp_path, older_folder):
    game = tmp_path / "Game"
    apk(game / "apk" / "com.x.game_2.apk")
    old = obb(game / older_folder, "main.1.com.x.game.obb")
    (old / "old.pak").write_bytes(b"old content")
    obb(game / "new", "main.2.com.x.game.obb")
    g = quest_dump.from_path(game / "apk")
    assert (g.data_dir, g.data_files) == (game / "new", None)
    assert quest_dump.from_path(g.apk).data_dir == game / "new"


def test_selected_sibling_obb_folder_keeps_assets_without_unselected_versions(tmp_path):
    a = apk(tmp_path / "apk" / "com.x.game_2.apk")
    obb(tmp_path / "old", "main.1.com.x.game.obb")
    data = obb(tmp_path / "obb", "main.2.com.x.game.obb")
    (data / "content.pak").write_bytes(b"content")
    g = quest_dump.from_path(a)
    assert (g.data_dir, g.data_files) == (data, None)
    # Retaining the entire folder must not reinclude expansion files for a different version.
    obb(data, "main.3.com.x.game.obb")
    g = quest_dump.from_path(a)
    assert (g.data_dir, g.data_files) == (data, ["main.2.com.x.game.obb"])


def test_app_update_does_not_expand_an_outdated_sibling_obb_folder(tmp_path):
    from frameport.core import library

    a = apk(tmp_path / "apk" / "com.x.game_2.apk")
    data = obb(tmp_path / "obb", "main.1.com.x.game.obb")
    (data / "old.pak").write_bytes(b"old content")
    obb(tmp_path / "new", "main.2.com.x.game.obb")
    g = {"apk": str(a), "data_dir": str(data), "data_files": ["main.1.com.x.game.obb"]}
    analysis = {"package": "com.x.game", "extra": {"lang_packs": [], "asset_files": []}}
    assert not library._refresh_data_fields(g, analysis)
    assert (g["data_dir"], g["data_files"]) == (str(data), ["main.1.com.x.game.obb"])


@pytest.mark.parametrize("name", ["main.2.com.x.game.obb", "main.com.x.game.obb"])
def test_obbs_by_name_never_come_from_another_games_folder(tmp_path, name):
    v1, v2 = tmp_path / "Game v1", tmp_path / "Game v2"
    apk(v1 / "com.x.game_1.apk")
    apk(v2 / "com.x.game_2.apk")
    obb(v2, name)
    found = {g.origin.name: g for g in quest_dump.scan(tmp_path)}
    assert found["Game v1"].data_dir is None
    assert (found["Game v2"].data_dir, found["Game v2"].data_files) == (v2, [name])


def test_deleting_local_files_takes_only_the_games_own_obbs(tmp_path):
    """Uninstall → "also delete the files on this PC": a folder holding several games' APKs and OBBs stays."""
    from frameport import pipeline
    from frameport.core import library

    shared = tmp_path / "Downloads"
    for pkg in ("com.x.game", "com.y.other"):
        apk(shared / f"{pkg}.apk")
        obb(shared, f"main.1.{pkg}.obb")
    for pkg in ("com.x.game", "com.y.other"):
        g = quest_dump.from_path(shared / f"{pkg}.apk")
        library.upsert_game(pkg, apk=str(g.apk), data_dir=str(g.data_dir), data_files=g.data_files)
    assert set(pipeline.local_game_files("com.x.game")) == {shared / "com.x.game.apk", shared / "main.1.com.x.game.obb"}
    assert pipeline.source_of(library.game("com.x.game")).data_files == ["main.1.com.x.game.obb"]


# ------------------------------------------------------------------ a game that expects an OBB (GitHub #85)
def test_expects_obb_from_unreals_manifest_flag():
    from frameport.analysis.detect import expects_obb

    assert expects_obb({"com.epicgames.ue4.GameActivity.bHasOBBFiles": True})
    assert expects_obb({"com.epicgames.unreal.GameActivity.bHasOBBFiles": "true"})  # UE5's name
    assert not expects_obb({"com.epicgames.ue4.GameActivity.bHasOBBFiles": False})
    assert not expects_obb({"com.epicgames.ue4.GameActivity.bVerifyOBBOnStartUp": True})


def test_analysis_reads_the_obb_flag(tmp_path):
    from conftest import build_axml

    from frameport.analysis.detect import analyze

    manifest = build_axml([
        ("start", "manifest", [("package", "str", "com.x.game")]),
        ("start", "application", []),
        ("start", "meta-data", [("name", "str", "com.epicgames.ue4.GameActivity.bHasOBBFiles"),
                                ("value", "bool", True)]),
        ("end", "meta-data"),
        ("end", "application"),
        ("end", "manifest"),
    ])
    p = tmp_path / "game.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
    assert analyze(p).extra["expects_obb"] is True


# ------------------------------------------------------------------ Unity split-binary builds (GitHub #92)
def _unity_apk(path: Path, files: dict[str, bytes]) -> Path:
    from conftest import build_axml

    manifest = build_axml([("start", "manifest", [("package", "str", "com.x.game")]), ("end", "manifest")])
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
        z.writestr("lib/arm64-v8a/libunity.so", b"\x7fELF")
        for name, data in files.items():
            z.writestr(name, data)
    return path


D = "assets/bin/Data/"


@pytest.mark.parametrize("files, split", [
    ({"lib/arm64-v8a/libOculusXRPlugin.so": b"", D + "level0": b""}, True),  # XR plugin, UnitySubsystems in the OBB
    ({"lib/arm64-v8a/libUnityOpenXR.so": b"", D + "level0": b""}, True),
    ({"lib/arm64-v8a/libOculusXRPlugin.so": b"", D + "UnitySubsystems/OculusXRPlugin/UnitySubsystemsManifest.json":
      b"{}", D + "level0": b""}, False),  # a full XR-plugin build
    ({D + "level0": b""}, False),  # built-in VR, one scene (The Room VR): no sign
])
def test_unity_split_build_from_xr_plugin_without_subsystems(tmp_path, files, split):
    from frameport.analysis.detect import analyze

    extra = analyze(_unity_apk(tmp_path / "g.apk", files), deep=False).extra
    assert (extra["unity_split"], extra["expects_obb"]) == (split, split)


def test_unity_split_build_from_more_scenes_than_levels(tmp_path, monkeypatch):
    from frameport.analysis import unity_split

    monkeypatch.setattr(unity_split, "build_scene_count", lambda ggm: 3 if ggm == b"GGM" else None)
    for levels, split in ((1, True), (3, False)):
        files = {D + "globalgamemanagers": b"GGM", **{f"{D}level{i}": b"" for i in range(levels)},
                 D + "level0.resS": b"", D + "level0.split1": b""}  # parts of one level count once
        p = _unity_apk(tmp_path / f"loose{levels}.apk", files)
        with zipfile.ZipFile(p) as z:
            assert unity_split.split_build(z, z.namelist()) is split
            assert unity_split.split_build(z, z.namelist(), deep=False) is False
    # packed into data.unity3d: only the bundle's head and the blocks up to globalgamemanagers are read
    for levels, split in ((["level0"], True), (["level0", "level1", "level2"], False)):
        p = _unity_apk(tmp_path / f"packed{len(levels)}.apk", {D + "data.unity3d": _unityfs(
            {"globalgamemanagers": b"GGM", **{n: b"x" * 10 for n in levels}})})
        with zipfile.ZipFile(p) as z:
            assert unity_split.split_build(z, z.namelist()) is split


def _unityfs(nodes: dict[str, bytes]) -> bytes:
    """An uncompressed UnityFS bundle (format 7) with one data block."""
    import struct

    data = b"".join(nodes.values())
    info = b"\0" * 16 + struct.pack(">i", 1) + struct.pack(">IIH", len(data), len(data), 0)
    info += struct.pack(">i", len(nodes))
    off = 0
    for name, body in nodes.items():
        info += struct.pack(">qqI", off, len(body), 4) + name.encode() + b"\0"
        off += len(body)
    head = b"UnityFS\0" + struct.pack(">I", 7) + b"5.x.x\0" + b"2021.3.1f1\0"
    head += struct.pack(">qIII", 0, len(info), len(info), 0)
    head += b"\0" * (-len(head) % 16)
    return head + info + data


def test_unityfs_reader_stops_after_the_node(tmp_path):
    import io

    from frameport.analysis.unity_split import bundle_node

    names, ggm = bundle_node(io.BytesIO(_unityfs({"globalgamemanagers": b"GGM", "level0": b"L"})))
    assert (names, ggm) == (["globalgamemanagers", "level0"], b"GGM")
    assert bundle_node(io.BytesIO(b"UnityWeb\0..."))[1] is None


def _entry(**kw):
    e = {"package": "com.x.game", "analysis": {"extra": {"expects_obb": True}}, "data_dir": None, "data_bytes": 0}
    e.update(kw)
    return e


def test_missing_obb_is_flagged_before_install_and_in_launch_tests():
    from frameport import pipeline
    from frameport.validate.triage import TriageResult, add_missing_obb

    assert pipeline.missing_obb(_entry())
    assert not pipeline.missing_obb(_entry(data_dir="/games/x/com.x.game", data_bytes=10))
    assert not pipeline.missing_obb(_entry(data_dir="/games/x/com.x.game", data_bytes=None))  # older entries
    assert not pipeline.missing_obb(_entry(analysis={"extra": {"expects_obb": False}}))
    assert not pipeline.missing_obb(_entry(kind="rift"))
    from test_patches import _analysis

    analysis = _analysis(package="com.x.game", engine="Unreal", extra={"size": 1, "expects_obb": True}).to_dict()
    [note] = pipeline.analysis_warnings(_entry(analysis=analysis))
    assert "folder named com.x.game" in note and "hangs at start" in note
    # the launch test: the game logs nothing about it, so the finding comes from the library
    r = add_missing_obb(TriageResult("RUNNING", "Lepton started"))
    assert [f.id for f in r.findings] == ["missing-obb"] and r.verdict == "fail"
    running = TriageResult("RUNNING", "Submitting frames", fps=72.0)  # an OBB from an earlier install is there
    assert not add_missing_obb(running).findings and running.verdict == "pass"


@pytest.mark.parametrize("stray_pkg", ["com.Armature.VR4", "com.epicgames.ue4"])
def test_sidequest_desktop_backups_folder(tmp_path, stray_pkg):
    """SideQuest's "SideQuest Backups" folder, scanned whole: <package>/<timestamp>/apk/<package>.apk +
    obb/(main|patch).N.<package>.obb + data/ (the app's private files) + icon.png + manifest.json. VR4's backup also
    holds a second APK in its obb folder (Unreal's VR4-Android-Shipping-arm64.apk), which must not become the game
    or a second game (reddit, 2026-10-11)."""
    root = tmp_path / "SideQuest Backups"
    cabin = root / "com.pixeltoys.cabin" / "2026-10-08T12-08-41-153Z"
    apk(cabin / "apk" / "com.pixeltoys.cabin.apk")
    IDS["com.pixeltoys.cabin.apk"] = ("com.pixeltoys.cabin", 11479)
    obb(cabin / "obb", "main.11479.com.pixeltoys.cabin.obb")
    for d in ("cloud", "il2cpp", "Unity"):
        (cabin / "data" / d).mkdir(parents=True)
    (cabin / "icon.png").write_bytes(b"p")
    (cabin / "manifest.json").write_text("{}")
    vr4 = root / "com.Armature.VR4" / "2026-10-07T02-10-19-171Z"
    apk(vr4 / "apk" / "com.Armature.VR4.apk")
    IDS["com.Armature.VR4.apk"] = ("com.Armature.VR4", 203)
    obb(vr4 / "obb", "main.203.com.Armature.VR4.obb")
    obb(vr4 / "obb", "patch.203.com.Armature.VR4.obb")
    apk(vr4 / "obb" / "VR4-Android-Shipping-arm64.apk")
    IDS["VR4-Android-Shipping-arm64.apk"] = (stray_pkg, 1)
    dungeon = root / "de.erthu.ancientdungeonfull" / "2026-10-06T22-31-00-000Z"
    apk(dungeon / "apk" / "de.erthu.ancientdungeonfull.apk")  # no obb folder at all
    found = {g.apk.name: g for g in quest_dump.scan(root)}
    assert set(found) == {"com.pixeltoys.cabin.apk", "com.Armature.VR4.apk", "de.erthu.ancientdungeonfull.apk"}
    assert found["com.pixeltoys.cabin.apk"].data_dir == cabin / "obb"
    assert found["com.pixeltoys.cabin.apk"].name == "com.pixeltoys.cabin"  # not the backup's time stamp
    v = found["com.Armature.VR4.apk"]
    assert v.data_dir == vr4 / "obb"
    assert sorted(v.data_files or [p.name for p in (vr4 / "obb").glob("*.obb")]) == [
        "main.203.com.Armature.VR4.obb", "patch.203.com.Armature.VR4.obb"]
    assert found["de.erthu.ancientdungeonfull.apk"].data_dir is None


def test_axrb_download_folder(tmp_path):
    """AXRB's launcher downloads to <Downloads>/AXRB/<app id>/<binary id>/: base.apk + the OBBs by their store names
    (+ other asset files); its own patched builds go to AXRB/patched/<package>/<name>-axrb.apk (reddit, 2026-10-11)."""
    root = tmp_path / "AXRB"
    d = root / "1234567890" / "987654321"
    a = apk(d / "base.apk")
    IDS["base.apk"] = ("com.Armature.VR4", 203)
    obb(d, "main.203.com.Armature.VR4.obb")
    obb(d, "patch.203.com.Armature.VR4.obb")
    (d / "extra_asset.dat").write_bytes(b"a")
    p = apk(root / "patched" / "com.Armature.VR4" / "base-axrb.apk")
    IDS["base-axrb.apk"] = ("com.Armature.VR4", 203)
    found = quest_dump.scan(root)
    games = {g.apk: g for g in found}
    # the OBBs and AXRB's other store files (DLC, assets) go to Android/obb/<package>; the APK stays
    assert games[a].data_dir == d and games[a].data_files == ["extra_asset.dat", "main.203.com.Armature.VR4.obb",
                                                              "patch.203.com.Armature.VR4.obb"]
    assert p not in games and len(games) == 1  # AXRB's PC-patched copy isn't a second game
    assert games[a].name == "com.Armature.VR4"  # not the binary id


def test_rescan_replaces_an_entry_made_from_axrbs_patched_copy(tmp_path, monkeypatch):
    """Older FramePort scanned AXRB/patched/<package>/*-axrb.apk (no OBBs) over the original download; a rescan of
    new games only must still add the original for that package."""
    from frameport import pipeline
    from frameport.core import library

    root = tmp_path / "AXRB"
    d = root / "1" / "2"
    apk(d / "base.apk")
    IDS["base.apk"] = ("com.x.game", 5)
    obb(d, "main.5.com.x.game.obb")
    old = tmp_path / "AXRB" / "patched" / "com.x.game" / "base-axrb.apk"
    games = [{"package": "com.x.game", "apk": str(old)}, {"package": "com.y.other", "apk": str(tmp_path / "y.apk")}]
    monkeypatch.setattr(library, "games", lambda: games)
    monkeypatch.setattr(quest_dump, "_package_of", lambda a: quest_dump._apk_ids(a)[0])
    added = []
    monkeypatch.setattr(pipeline, "add_game", lambda src, rep=None: added.append(src) or {"package": "com.x.game"})
    monkeypatch.setattr(pipeline.rift_dump, "scan", lambda *a, **k: [])
    pipeline.add_path(root, only_new=True)
    assert [s.apk.name for s in added] == ["base.apk"] and added[0].data_files == ["main.5.com.x.game.obb"]


def test_install_looks_for_data_copied_in_after_the_apk_was_added(tmp_path, monkeypatch):
    """GitHub #156: the APK was added alone; the OBB was copied next to it later. Installing finds and sends it."""
    from frameport import pipeline
    from frameport.core import library

    a = apk(tmp_path / "BnS" / "BladeAndSorcery.apk")
    IDS[a.name] = ("com.Warpfrog.BladeAndSorcery", 260730005)
    obb(tmp_path / "BnS", "main.260730005.com.Warpfrog.BladeAndSorcery.obb")
    stored = {}
    monkeypatch.setattr(library, "upsert_game", lambda pkg, **kw: stored.update(kw) or {"package": pkg, **kw})
    out = pipeline._find_late_data({"package": "com.Warpfrog.BladeAndSorcery", "apk": str(a), "data_dir": None})
    assert out["data_dir"] == str(tmp_path / "BnS") and stored["data_files"] == [
        "main.260730005.com.Warpfrog.BladeAndSorcery.obb"]
    assert pipeline._find_late_data({"package": "x", "apk": str(tmp_path / "gone.apk")})["apk"].endswith("gone.apk")


def test_install_recovers_versionless_obb_skipped_by_older_scan(tmp_path, monkeypatch):
    """An already-added Climb backup gets its missing data on Update on Frame, without needing a rescan."""
    from frameport import pipeline
    from frameport.core import library

    backup = tmp_path / "com.crytek.climb" / "2026-10-12T16-44-24-860Z"
    a = apk(backup / "apk" / "com.crytek.climb.apk")
    obb(backup / "obb", "main.com.crytek.climb.obb")
    saved = {}
    monkeypatch.setattr(library, "upsert_game", lambda pkg, **kw: saved.update(kw) or {"package": pkg, **kw})
    out = pipeline._find_late_data({"package": "com.crytek.climb", "apk": str(a), "data_dir": None})
    assert out["data_dir"] == str(backup / "obb")
    assert saved["data_files"] is None and saved["data_bytes"] == 1


def test_obb_named_for_another_package_is_flagged():
    """GitHub #129: a renamed build's OBB next to the original APK; the game looks for its own name."""
    from frameport import pipeline

    entry = {"package": "com.beatgames.beatsaber", "data_manifest": {
        "main.1716.com.beatgames.beatsaber.lj369vr.obb": 1, "patch.1716.com.beatgames.beatsaber.obb": 1,
        "extra.bundle": 1}}
    assert [(o.name, o.wanted, o.rename) for o in pipeline.misnamed_obbs(entry)] == [
        ("main.1716.com.beatgames.beatsaber.lj369vr.obb", "main.1716.com.beatgames.beatsaber.obb", False)]
    assert pipeline.renamed_obbs(entry) == []  # another app's file: a warning, never renamed (#481)
    assert pipeline.misnamed_obbs({"package": "com.x", "kind": "rift", "data_manifest": entry["data_manifest"]}) == []


def test_install_picks_up_chunk_obbs_an_older_scan_skipped(tmp_path, monkeypatch):
    """GitHub #471: library entries scanned before chunk OBBs were recognised get them at the next install."""
    from frameport import pipeline

    game = tmp_path / "AW2"
    a = apk(game / "com.x.game_7.apk")
    obb(game, "main.7.com.x.game.obb")
    obb(game, "patch.pakchunk0-Android_ASTC.com.x.game.obb")
    monkeypatch.setattr(pipeline.library, "upsert_game", lambda pkg, **kw: {**entry, **kw})
    entry = {"package": "com.x.game", "apk": str(a), "data_dir": str(game), "data_files": ["main.7.com.x.game.obb"]}
    out = pipeline._find_more_data(entry)
    assert sorted(out["data_files"]) == ["main.7.com.x.game.obb", "patch.pakchunk0-Android_ASTC.com.x.game.obb"]
    assert pipeline._find_more_data({**entry, "data_files": out["data_files"]})["data_files"] == out["data_files"]
    assert pipeline._find_more_data({**entry, "data_files": None}) is not None  # whole-folder data: unchanged
    assert out["data_manifest"] == {f: (game / f).stat().st_size for f in out["data_files"]}  # → "Update on Frame"
    gone = {**entry, "package": "com.y", "apk": str(game / "gone.apk")}
    monkeypatch.setattr(pipeline.library, "games", lambda: [entry, gone])
    assert pipeline.refresh_data_files() == 1


@pytest.mark.parametrize("apk_folder,obb_folder", [("apk", "obb"), ("APKs", "OBBs")])
def test_sidequest_backup_with_numbered_patch_files(tmp_path, apk_folder, obb_folder):
    """GitHub #159: Medal of Honor's SideQuest backup holds main.12297.<package>.obb and patch.0 … patch.20.12297.
    <package>.obb (~41 GB); only the main file was sent. Every file of the obb folder goes to the Frame."""
    from frameport.install.installer import local_data_manifest

    pkg = "com.Virtuos.R6DLM"
    backup = tmp_path / "SideQuest Backups" / pkg / "2026-10-14T19-17-27-675Z"
    a = apk(backup / apk_folder / f"{pkg}_12297.apk")
    data = obb(backup / obb_folder, f"main.12297.{pkg}.obb")
    patches = [f"patch.{n}.12297.{pkg}.obb" for n in range(21)]
    for name in patches:
        obb(data, name)
    (data / "extra.pak").write_bytes(b"content")
    for g in (quest_dump.from_path(a), *quest_dump.scan(tmp_path / "SideQuest Backups")):
        assert g.data_dir == data and g.data_files is None
        assert sorted(local_data_manifest(g.data_dir, g.data_files)) == sorted(
            [f"main.12297.{pkg}.obb", *patches, "extra.pak"])


def test_numbered_patch_files_next_to_the_apk(tmp_path):
    """Numbered patch files picked by name: each number is a file of its own, chosen by its versionCode."""
    pkg = "com.Virtuos.R6DLM"
    game = tmp_path / "Medal of Honor"
    apk(game / f"{pkg}_12297.apk")
    for name in (f"main.12297.{pkg}.obb", f"patch.0.12297.{pkg}.obb", f"patch.1.12297.{pkg}.obb",
                 f"patch.1.13000.{pkg}.obb", "patch.0.12297.com.y.other.obb"):
        obb(game, name)
    [g] = quest_dump.scan(tmp_path)
    assert g.data_dir == game and sorted(g.data_files) == [
        f"main.12297.{pkg}.obb", f"patch.0.12297.{pkg}.obb", f"patch.1.12297.{pkg}.obb"]


def test_loose_asset_bundles_next_to_the_apk(tmp_path):
    """GitHub #444: Ghost Town's store asset files (Unity *_assets_all.bundle / *_scenes_all.bundle) lie next to
    base.apk without an OBB; the game reads them from Android/obb/<package>/. Other files there stay on the PC."""
    from frameport.install.installer import local_data_manifest

    pkg = "com.FireproofStudios.GhostTown"
    game = tmp_path / "Ghost Town"
    a = apk(game / "base.apk")
    IDS["base.apk"] = (pkg, 101)
    bundles = ["flt_scenes_all.bundle", "localisation-translations_assets_all.bundle",
               "localization-assets-english(en)_assets_all.bundle",
               "localization-assets-japanese(japan)(ja-jp)_assets_all.bundle", "shared_assets_all.bundle"]
    for name in bundles:
        (game / name).write_bytes(b"b")
    (game / "readme.txt").write_bytes(b"r")
    (game / "cover.jpg").write_bytes(b"c")
    for g in (quest_dump.from_path(a), quest_dump.from_path(game), *quest_dump.scan(tmp_path)):
        assert g.apk == a and g.data_dir == game and sorted(g.data_files) == sorted(bundles)
        assert sorted(local_data_manifest(g.data_dir, g.data_files)) == sorted(bundles)
    # with an OBB next to them: both
    obb(game, f"main.101.{pkg}.obb")
    assert sorted(quest_dump.from_path(a).data_files) == sorted([f"main.101.{pkg}.obb", *bundles])
    # a package folder made by hand (the workaround from the thread) still wins
    obb(game / pkg, "localisation-translations_assets_all.bundle")
    assert quest_dump.from_path(a).data_dir == game / pkg


def test_loose_bundles_in_a_folder_of_several_apks_are_nobodys(tmp_path):
    """A collection of APKs: whose asset files lie next to them is unknown, so none are sent."""
    d = tmp_path / "collection" / "games"
    apk(d / "com.x.game.apk")
    apk(d / "com.y.other.apk")
    (d / "shared_assets_all.bundle").write_bytes(b"b")
    assert quest_dump.from_path(d / "com.x.game.apk").data_dir is None
