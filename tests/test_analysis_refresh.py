"""Library entries analysed by an older FramePort are analysed again (GitHub #104: no `sdl_java` → no
frame.sdl_clipboard), keeping the user's own choices."""
import zipfile

import pytest

from frameport import pipeline
from frameport.analysis import detect
from frameport.core import library
from frameport.core.models import Recipe
from frameport.patches import base
from frameport.recommend import engine

SDL = b"dex\n035\0...Lorg/libsdl/app/SDLClipboardHandler;..."


def _sdl_apk(path, manifest):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
        z.writestr("classes.dex", SDL)
    return path


def _old_entry(tmp_path, manifest, recipe_source="heuristics"):
    """An entry as an older FramePort stored it: analysed before `sdl_java` and ANALYSIS_VERSION existed."""
    base.load_all()
    apk = _sdl_apk(tmp_path / "game.apk", manifest)
    a = detect.analyze(apk)
    for k in ("sdl_java", "analysis_version", "min_sdk", "web_wrapper", "expects_obb"):
        a.extra.pop(k)
    recipe = engine.suggest(a)
    assert "frame.sdl_clipboard" not in recipe.patches  # the gap: the field is missing, the patch doesn't apply
    if recipe_source == "user":
        recipe = Recipe(package=a.package, patches={"frame.adapter": {}, "adapter.scale": {"value": 0.8}},
                        source="user", status="works")
    library.upsert_game(a.package, title="Dramatic Shape", title_locked=True, tags=["mine"], apk=str(apk),
                        analysis=a.to_dict(), recipe=library.recipe_to_dict(recipe),
                        suggested=library.recipe_to_dict(engine.suggest(a)), build={"apk": "x.apk"})
    return a.package


def test_old_entry_gets_new_fields_and_the_patch(tmp_path, quest_manifest):
    pkg = _old_entry(tmp_path, quest_manifest)
    assert pipeline.outdated_analyses() == [pkg]
    assert pipeline.refresh_analyses() == 1
    g = library.game(pkg)
    assert g["analysis"]["extra"]["sdl_java"] is True
    assert g["analysis"]["extra"]["analysis_version"] == detect.ANALYSIS_VERSION
    assert "min_sdk" in g["analysis"]["extra"] and "expects_obb" in g["analysis"]["extra"]
    assert "frame.sdl_clipboard" in g["recipe"]["patches"]  # derived recipe follows
    assert "frame.sdl_clipboard" in g["suggested"]["patches"]
    # everything else the user set stays
    assert g["title"] == "Dramatic Shape" and g["title_locked"] and g["tags"] == ["mine"]
    assert g["build"] == {"apk": "x.apk"}
    assert pipeline.outdated_analyses() == [] and pipeline.refresh_analyses() == 0  # once


def test_users_recipe_survives(tmp_path, quest_manifest):
    pkg = _old_entry(tmp_path, quest_manifest, recipe_source="user")
    before = library.game(pkg)["recipe"]  # (load-time migrations may have added default fixes)
    assert pipeline.refresh_analyses() == 1
    g = library.game(pkg)
    assert g["recipe"]["source"] == "user" and g["recipe"]["status"] == "works"
    assert g["recipe"] == before and g["recipe"]["patches"]["adapter.scale"] == {"value": 0.8}
    assert "frame.sdl_clipboard" not in g["recipe"]["patches"]
    assert "frame.sdl_clipboard" in g["suggested"]["patches"]  # offered, not forced
    assert g["analysis"]["extra"]["sdl_java"] is True


def test_missing_apk_is_skipped_and_tried_again_later(tmp_path, quest_manifest):
    pkg = _old_entry(tmp_path, quest_manifest)
    apk = tmp_path / "game.apk"
    apk.rename(tmp_path / "elsewhere.apk")
    before = library.game(pkg)["analysis"]
    assert pipeline.outdated_analyses() == [] and pipeline.refresh_analyses() == 0
    g = library.game(pkg)
    assert g["analysis"] == before and "analysis_failed" not in g
    (tmp_path / "elsewhere.apk").rename(apk)  # the drive is back
    assert pipeline.refresh_analyses() == 1


def test_unreadable_apk_is_marked_not_retried(tmp_path, quest_manifest, monkeypatch):
    pkg = _old_entry(tmp_path, quest_manifest)
    (tmp_path / "game.apk").write_bytes(b"not a zip")
    before = library.game(pkg)["analysis"]
    assert pipeline.refresh_analyses() == 0
    g = library.game(pkg)
    assert g["analysis"] == before and g["analysis_failed"] == detect.ANALYSIS_VERSION
    calls = []
    monkeypatch.setattr(pipeline, "analyze", lambda *a, **k: calls.append(a) or pytest.fail("read again"))
    assert pipeline.outdated_analyses() == [] and pipeline.refresh_analyses() == 0 and not calls
    # a newer analysis version tries again
    monkeypatch.setattr(detect, "ANALYSIS_VERSION", detect.ANALYSIS_VERSION + 1)
    assert pipeline.outdated_analyses() == [pkg]


def test_analyze_again_sets_the_version_and_clears_the_mark(tmp_path, quest_manifest):
    pkg = _old_entry(tmp_path, quest_manifest)
    library.update_game(pkg, lambda g: g.__setitem__("analysis_failed", detect.ANALYSIS_VERSION))
    pipeline.reanalyze(pkg)
    g = library.game(pkg)
    assert g["analysis"]["extra"]["analysis_version"] == detect.ANALYSIS_VERSION and "analysis_failed" not in g


def test_rift_and_linux_entries_are_left_alone(tmp_path):
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"MZ")
    library.upsert_game("rift.game", kind="rift", apk=None, game_dir=str(tmp_path),
                        analysis={"package": "rift.game", "extra": {"kind": "rift"}})
    library.upsert_game("linux.app", kind="linux", apk=str(exe), analysis={"package": "linux.app", "extra": {}})
    assert pipeline.outdated_analyses() == []


def test_build_analyses_an_outdated_entry_first(tmp_path, quest_manifest, monkeypatch):
    pkg = _old_entry(tmp_path, quest_manifest)
    seen = {}

    def build(src, a, recipe, out, reporter):
        seen["a"], seen["r"] = a, recipe
        raise RuntimeError("stop")
    monkeypatch.setattr(pipeline.builder, "build", build)
    from frameport.core.events import Reporter

    with pytest.raises(RuntimeError, match="stop"):
        pipeline.build_game(pkg, Reporter())
    assert seen["a"].extra["sdl_java"] is True and "frame.sdl_clipboard" in seen["r"].patches
