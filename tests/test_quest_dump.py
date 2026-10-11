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


def test_the_folder_with_the_apks_own_version_wins(tmp_path):
    game = tmp_path / "Game"
    apk(game / "apk" / "com.x.game_2.apk")
    obb(game / "old", "main.1.com.x.game.obb")
    obb(game / "new", "main.2.com.x.game.obb")
    g = quest_dump.from_path(game / "apk")
    assert (g.data_dir, g.data_files) == (game / "new", None)


def test_obbs_by_name_never_come_from_another_games_folder(tmp_path):
    v1, v2 = tmp_path / "Game v1", tmp_path / "Game v2"
    apk(v1 / "com.x.game_1.apk")
    apk(v2 / "com.x.game_2.apk")
    obb(v2, "main.2.com.x.game.obb")
    found = {g.origin.name: g for g in quest_dump.scan(tmp_path)}
    assert found["Game v1"].data_dir is None
    assert (found["Game v2"].data_dir, found["Game v2"].data_files) == (v2, ["main.2.com.x.game.obb"])


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
