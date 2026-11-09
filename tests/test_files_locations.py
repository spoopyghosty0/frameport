"""Files location discovery and capabilities, without a device or GUI."""
from types import SimpleNamespace

import pytest

from frameport.install import files
from frameport.ui.views.files import FilesView, drive_locations, game_location


def test_drive_locations():
    assert drive_locations([]) == []
    locs = drive_locations([
        {"path": "/home/x", "internal": True},
        {"path": "/media/SD", "label": "SD card", "free_bytes": 123},
        {"path": "/media/USB", "read_only": True, "usable": False},
        {"path": "/games", "reason": "mounted read-only"},
        {"path": "/fat", "usable": False, "reason": "exfat can't hold game data"},
        {},
    ])
    assert [x["path"] for x in locs] == ["/media/SD", "/media/USB", "/games", "/fat"]
    assert locs[0]["label"] == "SD card" and locs[0]["free_bytes"] == 123
    assert [x["read_only"] for x in locs] == [False, True, True, False]
    for loc in locs:
        assert files.inside(loc["path"], "mods") == loc["path"] + "/mods"
        with pytest.raises(ValueError):
            files.inside(loc["path"], "../outside")


@pytest.mark.parametrize("kind", ["pcvr", "rift", "linux"])
def test_game_install_locations(kind):
    dep = {"package": "game", "kind": kind}
    loc = game_location(dep, "Game", "/home/x")
    assert loc["path"] == "/home/x/Applications/quest-frame/game"
    assert not loc["android"]
    dep["drive"] = {"path": "/media/SD", "internal": False}
    assert game_location(dep, "Game", "/home/x")["path"] == "/media/SD/FramePort/game"
    dep["base"] = "/custom/deployed/game"
    loc = game_location(dep, "Game", "/home/x")
    assert loc["path"] == dep["base"]
    with pytest.raises(ValueError):
        files.inside(loc["path"], "/custom/deployed/other")
    assert "path" not in game_location({"package": "q", "kind": "quest"}, "Quest", "/home/x")


def test_background_discovery_cached_and_game_open_uses_base(monkeypatch):
    calls = []
    frame = SimpleNamespace(home="/home/x")
    app = SimpleNamespace(target=SimpleNamespace(frame=frame, drives=lambda: calls.append("drives") or [
        {"path": "/media/SD", "read_only": True}]), frame_info={"installed": [
        {"package": "linux", "kind": "linux", "base": "/media/SD/FramePort/linux"}]},
        run_bg=lambda fn, *args: fn(*args))
    v = FilesView(app)
    monkeypatch.setattr(v, "_render_locations", lambda: None)
    monkeypatch.setattr(v, "load", lambda: None)
    monkeypatch.setattr(files, "storage_targets", lambda f, *a: calls.append("storage") or [
        {"id": "videos", "path": "/home/x/Videos", "android": "/sdcard/Movies"}])
    v._load_locations("linux")
    assert v.loc["path"] == "/media/SD/FramePort/linux"
    assert v.loc["read_only"] and all(c.disabled for c in v.toolbar.controls[:3])
    v._load_locations(None)
    assert calls == ["storage", "drives"]
    with pytest.raises(ValueError):
        v.cd("../other")


def test_read_only_actions_and_quest_link_protection():
    v = FilesView(SimpleNamespace())
    v.loc = {"id": "drive:/sd", "path": "/sd", "read_only": True}
    v.path = "/sd"
    e = files.Entry("link", "/sd/link", True, 0, 0, link=True)
    v.entries = [e]
    assert not v._protected(e) and v._selectable(e)
    labels = [a[0] for a in v.menu_actions(e) if a]
    assert "Download to this PC…" in labels
    assert "Rename…" not in labels and "Delete…" not in labels
    assert "Upload files…" not in [a[0] for a in v.menu_actions(None) if a]
    # Handler guards also cover OS drops and stale callbacks.
    v.upload([])
    v.new_folder()
    v.rename(e)
    v.delete([e])
    v.loc = {"id": "app:q", "path": "/sd", "android": "/sdcard"}
    assert v._protected(e)
    v.loc["android"] = ""
    assert not v._protected(e)


def test_installed_games_include_all_kinds_sorted(monkeypatch):
    from frameport.core import library

    monkeypatch.setattr(library, "game", lambda pkg: {"title": "Alpha"} if pkg == "pc" else None)
    v = FilesView(SimpleNamespace(frame_info={"installed": [
        {"package": "q", "kind": "quest", "title": "Zulu"},
        {"package": "linux", "kind": "linux", "title": "Beta"},
        {"package": "pc", "kind": "pcvr", "title": "Fallback"},
    ]}))
    assert v._installed_games() == [("pc", "Alpha"), ("linux", "Beta"), ("q", "Zulu")]


def test_empty_drives_cached(monkeypatch):
    calls = []
    v = FilesView(SimpleNamespace(target=SimpleNamespace(frame=SimpleNamespace(home="/home/x"),
                                                     drives=lambda: calls.append(1) or []),
                                 frame_info={"installed": []}))
    monkeypatch.setattr(v, "_render_locations", lambda: None)
    monkeypatch.setattr(files, "storage_targets", lambda f: [])
    v._load_locations(None)
    v._load_locations(None)
    assert v.drives == [] and len(calls) == 1


def test_installed_games_are_grouped_by_kind():
    from frameport.ui.views.files import game_section

    assert [game_section(k) for k in ("quest", None, "rift", "pcvr", "linux")] == \
        ["quest", "quest", "pcvr", "pcvr", "linux"]


def test_back_to_a_folder_restores_its_scroll_and_backspace_goes_up(monkeypatch):
    """GitHub #411: going back up jumped to the top of the folder's list; Backspace now goes to the folder above."""
    v = FilesView(SimpleNamespace())
    loaded = []
    monkeypatch.setattr(v, "load", lambda: loaded.append((v.path, v._restore)))
    v.loc, v.path = {"id": "drive:/sd", "path": "/sd"}, "/sd"
    v._remember_scroll(SimpleNamespace(pixels=640.0))
    v.cd("/sd/movies")
    v._remember_scroll(SimpleNamespace(pixels=80.0))
    v.cd("/sd/movies/2026")
    assert loaded[-1] == ("/sd/movies/2026", None)  # into a folder: from the top
    assert v.go_up() and loaded[-1] == ("/sd/movies", 80.0)
    v.cd("/sd")  # a crumb two levels up
    assert loaded[-1] == ("/sd", 640.0)
    assert not v.go_up()  # the location's top


def test_tmp_and_saves_locations_and_move_actions(monkeypatch):  # GitHub #532
    from frameport.core import library
    from frameport.ui.views.files_move import destinations

    monkeypatch.setattr(library, "game", lambda pkg: None)
    app = SimpleNamespace(target=SimpleNamespace(frame=SimpleNamespace(home="/home/x")), frame_info={"installed": [
        {"package": "q", "kind": "quest", "title": "Quest Game"}, {"package": "pc", "kind": "pcvr", "title": "PC"}]})
    v = FilesView(app)
    v.drives = [{"id": "drive:/ro", "label": "RO", "path": "/ro", "read_only": True}]
    groups = dict(v.location_groups())
    assert [x[0]["id"] for x in groups["Game storage"]] == ["app:q"]  # saves are inside it, not another row
    assert [x[0]["id"] for x in groups["PC VR games"]] == ["app:pc"]
    adv = [x[0] for x in groups["Advanced"]]
    assert adv[1]["id"] == "tmp" and adv[1]["path"] == "/tmp" and adv[1]["label"] == "Temporary files"
    assert "Drives" not in dict(destinations(v.location_groups()))  # read-only drives can't take a move
    v.loc, v.path = adv[1], "/tmp"
    e = files.Entry("a.apk", "/tmp/a.apk", False, 1, 0)
    v.entries = [e]
    assert "Move to…" in [a[0] for a in v.menu_actions(e) if a]
    v.loc = {"id": "app:q", "path": "/sd", "android": "/sdcard"}
    link = files.Entry("Movies", "/sd/Movies", True, 0, 0, link=True)
    v.entries = [link]
    assert "Move to…" not in [a[0] for a in v.menu_actions(link) if a]  # Lepton's link stays where it is


def test_saves_are_a_link_like_entry_inside_the_game_storage(monkeypatch):  # GitHub #532
    v = FilesView(SimpleNamespace(run_bg=lambda fn, *a: fn(*a)))
    storage = {"id": "app:q", "package": "q", "label": "Quest Game", "path": "/g/external", "android": "/sdcard"}
    assert v.has_saves_entry(storage, "/g/external")
    assert not v.has_saves_entry(storage, "/g/external/Android")  # only at the top
    assert not v.has_saves_entry({**storage, "id": "app:pc", "android": ""}, "/g/external")  # PC VR / Linux
    entry = v.saves_entry(storage)
    assert entry.saves == "q" and entry.is_dir and entry.link
    v.loc, v.path, v.entries = storage, storage["path"], [entry]
    assert v._protected(entry) and not v._selectable(entry)
    assert [a[0] for a in v.menu_actions(entry) if a] == ["Open"]  # no rename, delete, move or download
    opened = []
    monkeypatch.setattr(v, "open_location", lambda loc, path=None: opened.append((loc, path)))
    v.cd(entry.path)  # clicking it opens the saves location, its crumbs starting at the game's storage
    saves = opened[-1][0]
    assert saves["saves"] == "q" and saves["parent"] is storage and saves["title"] == "Quest Game"
    saves = {**saves, "path": "/g/internal/q"}
    assert [(label, c["id"], p) for label, c, p in v.nav_crumbs(saves, "/g/internal/q/files")] == [
        ("Quest Game", "app:q", "/g/external"), ("Saves (internal storage)", "saves:q", "/g/internal/q"),
        ("files", "saves:q", "/g/internal/q/files")]
    assert v.loc_name(saves) == "Quest Game › Saves (internal storage)"
    v.loc, v.path = saves, saves["path"]
    loaded = []
    monkeypatch.setattr(v, "load", lambda: loaded.append(v.path))
    v.cd("/g/internal/q/files")
    assert v.go_up() and loaded[-1] == "/g/internal/q"
    assert v.go_up() and opened[-1] == (storage, None)  # from the saves' top back to the game's storage


def test_saves_and_tmp_notices():
    shown = []
    v = FilesView(SimpleNamespace(backup_saves=shown.append))
    v.loc = {"id": "tmp", "path": "/tmp", "tmp": True}
    v._render_notice()
    assert v.notice.visible
    v.loc = {"id": "saves:q", "saves": "q", "title": "Quest Game", "path": "/x"}
    v.saves_running["q"] = False
    v._render_notice()
    row = v.notice.content.content.controls[1].content
    assert "back up the saves" in row.controls[0].content.value
    row.controls[1].on_click(None)
    assert shown == ["q"]
    v.saves_running["q"] = True
    v._render_notice()
    assert "is running" in v.notice.content.content.controls[1].content.controls[0].content.value
    v.loc = {"id": "videos", "path": "/home/x/Videos"}
    v._render_notice()
    assert not v.notice.visible
