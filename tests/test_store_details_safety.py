"""Store title provenance and sexual-content rejection (#377)."""
import pytest

from frameport import pipeline
from frameport.artwork import details, fetch, sources
from frameport.core import library


@pytest.mark.parametrize("ids,age,notes", [([1], 0, ""), ([3], "18", ""), ([4], 0, ""),
                                           ([5], 18, "Sexual content")])
def test_adult_steam_rejected(monkeypatch, ids, age, notes):
    app = {"steam_appid": 1, "required_age": age, "content_descriptors": {"ids": ids, "notes": notes},
           "screenshots": [{"path_full": "explicit"}], "about_the_game": "adult"}

    def cached(name, url, **kwargs):
        if "storesearch" in url:
            return {"items": [{"id": 1, "name": "Lollipop"}]}
        if "appdetails" in url:
            return {"1": {"success": True, "data": app}}
        return []

    monkeypatch.setattr(sources.cache, "cached_json", cached)
    monkeypatch.setattr(sources.cache, "http_get", lambda *a, **k: pytest.fail("downloaded adult image"))
    assert not details.fetch_details({"package": "com.game", "title": "Lollipop"}).get("description")
    assert not sources.apply_steam("com.game", 1)
    assert not sources.fetch_rift("rift.game", "Lollipop", oculus=False).get("source")
    assert not details.screenshot_files("com.game")


def test_general_mature_allowed():
    assert not sources.sexual_content({"required_age": 18, "content_descriptors": {"ids": [5]}})


@pytest.mark.parametrize("stored", [
    {"sources": ["steam"]},  # legacy migration
    {"sources": ["oculusdb"], "matches": {"oculusdb": {"by": "title", "key": "Old"}}},
    {"sources": ["oculusdb"]},  # legacy Rift lookup is conservatively name-dependent
])
@pytest.mark.parametrize("reset", [False, True])
def test_rename_clears_and_refetches(monkeypatch, stored, reset):
    pkg = "rift.game"
    library.upsert_game(pkg, title="Old", kind="rift", auto_title="Automatic", title_locked=True, details=stored)
    d = fetch.artwork_dir(pkg)
    for name in ("shot_1.jpg", "t_shot_1_480_abc.jpg", "portrait.jpg", ".picked"):
        (d / name).write_bytes(b"custom")
    seen = []

    def refetch(entry, **kwargs):
        assert "details" not in entry
        assert not list(d.glob("shot_*")) and not list(d.glob("t_shot_*"))
        seen.append(entry["title"])
        return {"sources": [], "fetched": 1}

    monkeypatch.setattr(details, "fetch_details", refetch)
    g = pipeline.rename_game(pkg, None if reset else "New")
    assert seen == ["Automatic" if reset else "New"]
    assert g["details"]["fetched"] == 1
    assert (d / ".picked").read_bytes() == (d / "portrait.jpg").read_bytes() == b"custom"


def test_package_details_kept(monkeypatch):
    stored = {"sources": ["oculusdb"], "matches": {"oculusdb": {"by": "package", "key": "com.game"}}}
    library.upsert_game("rift.game", kind="rift", title="Old", details=stored)
    monkeypatch.setattr(details, "fetch_details", lambda *a, **k: pytest.fail("unnecessary fetch"))
    assert pipeline.rename_game("rift.game", "New")["details"] == stored


def test_deferred_rename_does_no_network(monkeypatch):
    library.upsert_game("com.game", title="Old", details={"sources": ["steam"]})
    monkeypatch.setattr(details, "fetch_details", lambda *a, **k: pytest.fail("synchronous fetch"))
    assert "details" not in pipeline.rename_game("com.game", "New", refresh_details=False)


def test_match_provenance(monkeypatch):
    monkeypatch.setattr(sources, "oculusdb_search", lambda *a, **k: [{"packageName": "com.game", "id": 1}])
    monkeypatch.setattr(details, "steam_app", lambda title, store=None: {"steam_appid": 2})
    d = details.fetch_details({"package": "com.game", "title": "Game"})
    assert d["matches"] == {"oculusdb": {"by": "package", "key": "com.game"},
                            "steam": {"by": "title", "key": "Game"}}


def test_gui_rename_schedules_details(monkeypatch):
    from types import SimpleNamespace

    from frameport.ui.app import FramePortApp

    library.upsert_game("com.game", title="Old", details={"sources": ["steam"]})
    monkeypatch.setattr(details, "fetch_details", lambda *a, **k: pytest.fail("network in GUI handler"))
    queued = []
    app = SimpleNamespace(_title=lambda pkg: library.game(pkg)["title"], frame_state="disconnected",
                          pc_installs=lambda: {}, refresh_details=queued.append,
                          toast=lambda *a, **k: None, refresh_view=lambda: None)
    FramePortApp._apply_rename(app, "com.game", "New")
    assert queued == ["com.game"] and "details" not in library.game("com.game")


def test_cli_rename_and_reset_refetch(monkeypatch):
    from typer.testing import CliRunner

    from frameport.cli import app

    library.upsert_game("com.game", title="Old", details={"sources": ["steam"]})
    seen = []

    def refetch(entry, **kwargs):
        seen.append(entry["title"])
        return {"sources": ["steam"], "matches": {"steam": {"by": "title", "key": entry["title"]}}}

    monkeypatch.setattr(details, "fetch_details", refetch)
    for args in (["New"], ["--reset"]):
        result = CliRunner().invoke(app, ["rename", "com.game", *args, "--no-sync"])
        assert result.exit_code == 0, result.output
    assert seen == ["New", "Old"]
