"""Rename… (a tester's request: Quest games were named like their APK): the library title is locked against every
automatic source of names, and installed copies get the new name in Steam without a new shortcut appid."""
import json
import zipfile
from types import SimpleNamespace

import pytest

from frameport import pipeline
from frameport.core import library
from frameport.core.titles import display_title, twins
from frameport.targets.base import PC_LABEL


def _apk(tmp_path, manifest, name="game.apk"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
    return path


class Rep:
    def __init__(self):
        self.checks = []

    def stage(self, *a):
        pass

    def log(self, *a):
        pass

    def progress(self, *a):
        pass

    def check(self, name, ok, msg=""):
        self.checks.append((name, ok, msg))


def test_rename_locks_the_title_and_reset_unlocks_it():
    library.upsert_game("com.v.two", title="VaderImmortal2", kind="quest")
    with pytest.raises(ValueError):
        pipeline.rename_game("com.not.there", "X")
    g = pipeline.rename_game("com.v.two", "  Vader Immortal:\n Episode II ")
    assert g["title"] == "Vader Immortal: Episode II" and g["title_locked"] and g["auto_title"] == "VaderImmortal2"
    assert pipeline.automatic_title(g) == "VaderImmortal2"
    assert pipeline.title_suggestions(g) == ["VaderImmortal2"]
    g = pipeline.rename_game("com.v.two", "Vader 2")  # a second rename keeps the automatic name
    assert g["auto_title"] == "VaderImmortal2"
    g = pipeline.rename_game("com.v.two", "VaderImmortal2")  # choosing the automatic name = reset
    assert g["title"] == "VaderImmortal2" and "title_locked" not in g and "auto_title" not in g
    pipeline.rename_game("com.v.two", "Mine")
    g = pipeline.rename_game("com.v.two", None)  # --reset
    assert g["title"] == "VaderImmortal2" and not g.get("title_locked")
    assert len(pipeline.rename_game("com.v.two", "x" * 500)["title"]) == pipeline.TITLE_MAX


def test_store_name_is_offered(tmp_path):
    from frameport.artwork.fetch import artwork_dir

    library.upsert_game("com.v.two", title="VaderImmortal2")
    (artwork_dir("com.v.two") / "title.txt").write_text("Vader Immortal: Episode II", encoding="utf-8")
    g = library.game("com.v.two")
    assert pipeline.store_title(g) == "Vader Immortal: Episode II"
    assert pipeline.title_suggestions(g) == ["Vader Immortal: Episode II"]  # (the current name is the automatic one)
    g = pipeline.rename_game("com.v.two", "Vader Immortal: Episode II")
    assert g["title_locked"] and pipeline.title_suggestions(g) == ["VaderImmortal2"]


def test_rescan_keeps_the_users_name(tmp_path, quest_manifest):
    apk = _apk(tmp_path, quest_manifest)
    [g] = pipeline.add_path(apk)
    pkg, auto = g["package"], g["title"]
    pipeline.rename_game(pkg, "My name")
    [g] = pipeline.add_path(apk)  # scanning the folder again (a newer APK) used to replace the title
    assert g["title"] == "My name" and g["title_locked"] and g["auto_title"] == auto
    pipeline.reanalyze(pkg)
    assert library.game(pkg)["title"] == "My name"


def test_rift_store_match_doesnt_overwrite_a_locked_name(monkeypatch, tmp_path):
    from frameport.artwork import sources

    monkeypatch.setattr(sources, "fetch_rift", lambda *a, **k: {"source": "oculusdb", "title": "Store Name"})
    monkeypatch.setattr("frameport.artwork.thumbs.prewarm", lambda pkg: None)
    entry = library.upsert_game("rift.x", kind="rift", title="mine", title_locked=True, auto_title="x",
                                game_dir=str(tmp_path), exe="x.exe", analysis={"extra": {}})
    pipeline._rift_art(entry)
    g = library.game("rift.x")
    assert g["title"] == "mine" and g["auto_title"] == "Store Name"
    entry = library.upsert_game("rift.y", kind="rift", title="y", game_dir=str(tmp_path), exe="y.exe",
                                analysis={"extra": {}})
    pipeline._rift_art(entry)
    assert library.game("rift.y")["title"] == "Store Name"  # not renamed: the store's name as before


def test_a_chosen_name_has_no_twin_suffix():
    games = [{"package": "com.rr", "title": "Robo Recall"},
             {"package": "rift.rr", "kind": "rift", "title": "Robo Recall", "quest_package": "com.rr"}]
    tw = twins(games)
    assert display_title(games[0], tw) == "Robo Recall (Quest)"
    games[0].update(title="Robo Recall Unplugged", title_locked=True)
    tw = twins(games)
    assert display_title(games[0], tw) == "Robo Recall Unplugged"
    assert display_title(games[1], tw) == "Robo Recall (Rift)"  # still linked by quest_package


def test_frame_name_stale_until_synced_or_installed():
    library.upsert_game("com.a", title="A", installs={"steamos@frame": {"time": 1}})
    library.upsert_game("rift.b", kind="rift", title="B", installs={PC_LABEL: {"time": 1}})
    assert pipeline.rename_game("com.a", "A2")["steam_name_stale"]
    assert not pipeline.rename_game("rift.b", "B2").get("steam_name_stale")  # only on this PC: synced right away
    pipeline._record_install("com.a", PC_LABEL, {"time": 2})
    assert library.game("com.a")["steam_name_stale"]
    pipeline._record_install("com.a", "steamos@frame", {"time": 2})  # the install named the shortcut
    assert "steam_name_stale" not in library.game("com.a")


def test_sync_title_frame_and_pc():
    library.upsert_game("com.a", title="A", title_locked=True, steam_name_stale=True, steam_art_stale=True)
    calls = []
    frame = SimpleNamespace(kind="frame", label="steamos@frame",
                            update_steam_art=lambda pkg, rep, title=None: calls.append(("frame", pkg, title)))
    pc = SimpleNamespace(kind="pc", label=PC_LABEL, rename=lambda pkg, title, rep: calls.append(("pc", pkg, title)))
    pipeline.sync_title("com.a", frame, Rep())
    pipeline.sync_title("com.a", pc, Rep())
    assert calls == [("frame", "com.a", "A"), ("pc", "com.a", "A")]
    g = library.game("com.a")
    assert "steam_name_stale" not in g and "steam_art_stale" not in g


def test_update_steam_art_renames_on_the_frame_first(monkeypatch):
    from frameport.install import installer

    calls = []

    class Frame:
        def agent(self, cmd, **kw):
            calls.append((cmd, kw))
            if cmd == "list_installed":
                return {"games": [{"package": "com.a", "title": "Old", "anchor": "/a"}]}
            if cmd == "rename":
                return {"renamed": True, "title": kw["title"], "appid": 123}
            return {}

    monkeypatch.setattr("frameport.artwork.steam.steam_set_for", lambda pkg: {})
    monkeypatch.setattr(installer, "add_to_steam", lambda frame, pkgs, rep: calls.append(("shortcuts", pkgs)))
    installer.update_steam_art(Frame(), "com.a", Rep(), title="New")
    assert calls[1] == ("rename", {"package": "com.a", "title": "New", "shortcuts": False})
    assert calls[-1] == ("shortcuts", ["com.a"])  # one Steam restart, after the rename
    calls.clear()
    with pytest.raises(RuntimeError, match="no artwork"):  # same name and no art: nothing to send
        installer.update_steam_art(Frame(), "com.a", Rep(), title="Old")
    assert [c[0] for c in calls] == ["list_installed"]


def test_pc_rename_keeps_the_shortcut_appid(monkeypatch):
    from frameport.targets import pc_revive

    d = pc_revive.pc_dir() / "rift.x"
    d.mkdir(parents=True)
    (d / "deployment.json").write_text(json.dumps({"package": "rift.x", "title": "Old", "appid": 77,
                                                   "exe_win": "C:\\G\\x.exe"}))
    t = object.__new__(pc_revive.PcReviveTarget)
    added = []
    monkeypatch.setattr(t, "add_to_library", lambda pkgs, rep: (added.append(pkgs), {"state": "done"})[1])
    t.rename("rift.x", "New", Rep())
    dep = json.loads((d / "deployment.json").read_text())
    assert dep["title"] == "New" and dep["appid"] == 77 and added == [["rift.x"]]
    assert t.rename("rift.x", "New", Rep())["unchanged"] and len(added) == 1


def test_cli_rename_and_reset(monkeypatch):
    from typer.testing import CliRunner

    from frameport.cli import app

    library.upsert_game("com.v.two", title="VaderImmortal2")
    r = CliRunner().invoke(app, ["rename", "com.v.two", "Vader Immortal: Episode II"])
    assert r.exit_code == 0, r.output
    assert "VaderImmortal2 -> Vader Immortal: Episode II" in r.output
    assert library.game("com.v.two")["title_locked"]
    r = CliRunner().invoke(app, ["rename", "com.v.two", "--reset"])
    assert r.exit_code == 0 and "(automatic name)" in r.output
    assert library.game("com.v.two")["title"] == "VaderImmortal2"
    assert CliRunner().invoke(app, ["rename", "com.v.two"]).exit_code == 2  # neither a name nor --reset
    # installed on a Frame: synced through the target, unless --no-sync
    library.upsert_game("com.v.two", installs={"steamos@frame": {"time": 1}})
    synced = []
    monkeypatch.setattr("frameport.cli._target", lambda frame, password=None, to="frame": to)
    monkeypatch.setattr(pipeline, "sync_title", lambda pkg, target, rep: synced.append((pkg, target)))
    r = CliRunner().invoke(app, ["rename", "Vader", "V2", "--no-sync"])
    assert r.exit_code == 0 and not synced and "keeps the old name" in r.output
    r = CliRunner().invoke(app, ["rename", "com.v.two", "V3"])
    assert r.exit_code == 0 and synced == [("com.v.two", "frame")]


def test_rename_dialog_wiring(monkeypatch):
    """The dialog: prefilled, Enter/Save store the typed name, a suggestion stores that one, and the installed copy on
    the connected Frame is renamed by a job."""
    from frameport.ui.app import FramePortApp

    library.upsert_game("com.v.two", title="VaderImmortal2")
    dialogs, toasts, synced = [], [], []
    app = object.__new__(FramePortApp)
    app.page = SimpleNamespace(show_dialog=dialogs.append, pop_dialog=lambda: None)
    app.toast = lambda msg, **k: toasts.append(msg)
    app.refresh_view = lambda: None
    app._title = lambda p: library.game(p)["title"]
    app.frame_state, app.frame_info = "connected", {"installed": [{"package": "com.v.two", "sha256": "a"}]}
    app.pc_installs = lambda: {}
    app.sync_title = lambda pkg, to="frame": synced.append((pkg, to))
    app.rename_game("com.v.two")
    field = dialogs[0].content.content.controls[0]
    assert field.value == "VaderImmortal2"
    field.value = "Vader Immortal: Episode II"
    save = dialogs[0].actions[-1]
    save.on_click(None)
    field.on_submit(None)  # Enter right after the click: acts once
    assert library.game("com.v.two")["title"] == "Vader Immortal: Episode II"
    assert synced == [("com.v.two", "frame")] and "Steam restarts once" in toasts[-1]
    app.rename_game("com.v.two")  # the automatic name is offered back
    suggestion = dialogs[1].content.content.controls[2]
    assert suggestion.content == "VaderImmortal2" or getattr(suggestion, "text", None) == "VaderImmortal2"
    suggestion.on_click(None)
    g = library.game("com.v.two")
    assert g["title"] == "VaderImmortal2" and not g.get("title_locked")
    app.rename_game("com.v.two")
    dialogs[2].actions[-1].on_click(None)  # unchanged: nothing to sync
    assert len(synced) == 2
