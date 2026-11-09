"""Using another game's catalog recipe by hand (GitHub #508: a repack with its own package id)."""
import pytest

from frameport.core import library
from frameport.core.models import Analysis
from frameport.recommend import catalog, engine
from frameport.recommend.catalog import CatalogEntry


def _analysis(**kw):
    d = dict(package="ru.example.shadow", version="1.0", label="Shadow Knight", abis=["arm64-v8a"], engine="Unreal",
             xr="OpenXR", graphics="Vulkan (declared in manifest)", direct_vrapi=False, libs=[], launcher_activity=None,
             has_info_category=False, meta_permissions=[], uses_glad_gl=False, unity_msaa_levels=0,
             oculus_os_classes=False, is_overport_output=False, debuggable=False, extra={"size": 1})
    d.update(kw)
    return Analysis(**d)


SHADOW = CatalogEntry(package="com.example.shadow", title="Shadow Knight VR", status="works", engine="Unreal",
                      xr="OpenXR", tested_version="1.4.1", frame=["frame.nodebug", "frame.unreal_noreserve"],
                      adapter={"fixed_resolution": 1}, notes="Big data set.", verified={"date": "2026-10-01"})
UNITY = CatalogEntry(package="com.other.cubes", title="Cube Smasher", status="issues", engine="Unity", xr="VrApi",
                     frame=["frame.unity_text_input"], verified={"date": "2026-09-01"})
RIFT = CatalogEntry(package="rift.shadow-knight", title="Shadow Knight", kind="rift", pcvr=["pcvr.xr_timefix"])


@pytest.fixture
def entries(monkeypatch):
    cat = {e.package: e for e in (SHADOW, UNITY, RIFT)}
    monkeypatch.setattr(catalog, "_cache", cat)
    return cat


def _add(a: Analysis) -> dict:
    r = engine.suggest(a)
    library.upsert_game(a.package, title=a.label, analysis=a.to_dict(), recipe=library.recipe_to_dict(r),
                        suggested=library.recipe_to_dict(r), status=r.status)
    return library.game(a.package)


# ------------------------------------------------------------------------------------------------ fuzzy search
def test_search_ranks_close_titles_and_packages_first(entries):
    titles = [e.title for _, e in catalog.search("shadow knight", entries=entries)]
    assert titles[0] == "Shadow Knight VR" and "Shadow Knight" not in titles  # the PC VR entry is another kind
    assert [e.package for _, e in catalog.search("shadw", entries=entries)][0] == "com.example.shadow"  # a typo
    assert [e.package for _, e in catalog.search("cube", entries=entries)] == ["com.other.cubes"]
    assert catalog.search("zzzz qqqq", entries=entries) == []
    # a package id typed in, and the game's own package id (a repack: same id core), rank that entry first
    assert catalog.search("ru.example.shadow", entries=entries)[0][1] is SHADOW
    ranked = catalog.search("cube smasher", package="ru.example.shadow", entries=entries)
    assert ranked[0][1] is SHADOW and ranked[1][1] is UNITY
    # an empty query lists every entry of the kind, the same-core one first
    assert [e.package for _, e in catalog.search("", package="ru.example.shadow", entries=entries)] == \
        ["com.example.shadow", "com.other.cubes"]
    assert [e.package for _, e in catalog.search("shadow", kind="rift", entries=entries)] == ["rift.shadow-knight"]


def test_search_real_catalog_finds_batman_for_the_repack():
    catalog._cache = None
    top = catalog.search("Batman Arkham Shadow", package="ru.camouflaj.manta")[0][1]
    assert top.package == "com.camouflaj.manta"
    assert catalog.search("arkam shadow")[0][1].package == "com.camouflaj.manta"


# ------------------------------------------------------------------------------------------------ applying
def test_pick_copies_the_recipe_and_follows_its_entry(entries, monkeypatch):
    from frameport import pipeline

    _add(_analysis())
    assert library.game("ru.example.shadow")["recipe"]["source"] == "heuristics"
    assert pipeline.catalog_choices("ru.example.shadow")[0][1] is SHADOW  # pre-filled with the game's name

    pick = pipeline.apply_catalog_recipe("ru.example.shadow", "com.example.shadow")
    r = library.recipe_from_dict(library.game("ru.example.shadow")["recipe"])
    assert r.recipe_source == "com.example.shadow" and r.source.startswith("catalog pick")
    assert {"frame.nodebug", "frame.unreal_noreserve", "adapter.fixed_resolution"} <= set(r.patches)
    assert r.catalog_rev == SHADOW.rev() and r.title == "Shadow Knight"  # the game keeps its own name
    assert r.status == "unknown" and "Shadow Knight VR" in r.notes and "works" in r.notes
    assert pick.dropped == [] and pick.warnings == []

    # the catalog follow (every library read) keeps the pick ...
    data = library.load()
    library._follow_catalog(data)
    assert data["games"]["ru.example.shadow"]["recipe"]["recipe_source"] == "com.example.shadow"
    # ... and takes the entry's later changes
    changed = CatalogEntry(**{**SHADOW.__dict__, "frame": ["frame.nodebug"], "adapter": {}})
    monkeypatch.setitem(entries, "com.example.shadow", changed)
    assert library._follow_catalog(data)
    r = library.recipe_from_dict(data["games"]["ru.example.shadow"]["recipe"])
    assert r.recipe_source == "com.example.shadow" and r.catalog_rev == changed.rev()
    assert "frame.unreal_noreserve" not in r.patches and "frame.nodebug" in r.patches

    # the entry leaves the catalog: back to the game's own suggestion
    monkeypatch.delitem(entries, "com.example.shadow")
    assert library._follow_catalog(data)
    r = library.recipe_from_dict(data["games"]["ru.example.shadow"]["recipe"])
    assert r.recipe_source == "" and r.source == "heuristics"


def test_pick_survives_reanalysis_and_reset_ends_it(entries):
    from frameport import pipeline

    a = _analysis()
    _add(a)
    pipeline.apply_catalog_recipe("ru.example.shadow", "com.example.shadow")
    pipeline._store_analysis("ru.example.shadow", a)  # Analyze again / a rescan
    g = library.game("ru.example.shadow")
    assert library.recipe_pick(g) == "com.example.shadow"
    assert g["suggested"]["recipe_source"] == ""  # the plain suggestion stays the plain suggestion
    assert library.recipe_pick(library.game("ru.example.shadow")) == "com.example.shadow"
    pipeline.reset_recipe("ru.example.shadow")
    assert library.recipe_pick(library.game("ru.example.shadow")) == ""


def test_edited_pick_is_offered_its_entry_updates(entries, monkeypatch):
    from frameport import pipeline

    _add(_analysis())
    pipeline.apply_catalog_recipe("ru.example.shadow", "com.example.shadow")
    r = library.recipe_from_dict(library.game("ru.example.shadow")["recipe"])
    r.patches["adapter.layer_debug"] = {"value": 1}
    r.source = "user"
    pipeline.set_recipe("ru.example.shadow", r)
    changed = CatalogEntry(**{**SHADOW.__dict__, "frame": ["frame.nodebug"]})
    monkeypatch.setitem(entries, "com.example.shadow", changed)
    data = library.load()  # (follows the catalog)
    g = data["games"]["ru.example.shadow"]
    assert g["recipe"]["source"] == "user" and g["catalog_update"] == changed.rev()
    assert "frame.unreal_noreserve" in g["recipe"]["patches"]  # not replaced
    library.save(data)
    new = pipeline.apply_catalog_update("ru.example.shadow")
    assert new.recipe_source == "com.example.shadow" and "frame.unreal_noreserve" not in new.patches
    assert new.patches["adapter.layer_debug"] == {"value": 1}  # the user's own setting stays


def test_pick_drops_patches_for_another_engine_and_warns(entries):
    from frameport import pipeline

    _add(_analysis(package="org.example.cubes", label="Cubes", engine="Unity", xr="OpenXR"))
    pick = pipeline.preview_catalog_recipe("org.example.cubes", "com.example.shadow")
    assert "frame.nodebug" in pick.dropped and "frame.unreal_noreserve" in pick.dropped
    assert not {"frame.nodebug", "frame.unreal_noreserve"} & set(pick.recipe.patches)
    assert any("engine" in w and "Unity" in w and "Unreal" in w for w in pick.warnings)
    # previewing saves nothing
    assert library.recipe_pick(library.game("org.example.cubes")) == ""
    other_api = pipeline.preview_catalog_recipe("org.example.cubes", "com.other.cubes")
    assert any("VR interface" in w for w in other_api.warnings)
    # a PC VR recipe can't be used for a Quest game
    blocked = pipeline.preview_catalog_recipe("org.example.cubes", "rift.shadow-knight")
    assert blocked.blocked and blocked.warnings
    with pytest.raises(ValueError):
        pipeline.apply_catalog_recipe("org.example.cubes", "rift.shadow-knight")
    with pytest.raises(ValueError):
        pipeline.apply_catalog_recipe("org.example.cubes", "com.not.there")


def test_picking_the_games_own_entry_is_a_reset(entries):
    from frameport import pipeline

    _add(_analysis(package="com.example.shadow"))
    pipeline.apply_catalog_recipe("com.example.shadow", "com.example.shadow")
    r = library.game("com.example.shadow")["recipe"]
    assert r["recipe_source"] == "" and r["source"] == "catalog (bundled)"


def test_pick_marks_an_installed_build_outdated(entries):
    from frameport import pipeline

    g = _add(_analysis())
    before = pipeline.recipe_fingerprint(g["recipe"], g["package"])
    pipeline.apply_catalog_recipe("ru.example.shadow", "com.example.shadow")
    assert pipeline.recipe_fingerprint(library.game("ru.example.shadow")["recipe"], "ru.example.shadow") != before


# ------------------------------------------------------------------------------------------------ CLI
def test_cli_recipe_find_and_from(entries):
    from typer.testing import CliRunner

    from frameport import cli

    _add(_analysis())
    found = CliRunner().invoke(cli.app, ["recipe", "ru.example.shadow", "--find", ""])
    assert found.exit_code == 0, found.output
    assert found.output.splitlines()[0].startswith("com.example.shadow")
    res = CliRunner().invoke(cli.app, ["recipe", "ru.example.shadow", "--from", "com.example.shadow"])
    assert res.exit_code == 0, res.output
    assert library.recipe_pick(library.game("ru.example.shadow")) == "com.example.shadow"
    assert "frame.nodebug" in library.game("ru.example.shadow")["recipe"]["patches"]
    bad = CliRunner().invoke(cli.app, ["recipe", "ru.example.shadow", "--from", "com.not.there"])
    assert bad.exit_code != 0


def test_picker_rows_say_how_an_entry_relates_to_the_game():
    from frameport.ui.views.recipe_picker import facts, relation

    assert relation(SHADOW, "ru.example.shadow", "") == "same"
    assert relation(SHADOW, "ru.example.shadow", "com.example.shadow") == "current"
    assert relation(SHADOW, "com.example.shadow", "") == "own"
    assert relation(UNITY, "ru.example.shadow", "") is None
    assert facts(SHADOW) == "com.example.shadow · Unreal · OpenXR · tested with 1.4.1"
