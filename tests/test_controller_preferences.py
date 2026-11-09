"""Quest button preferences: inheritance, deployment payloads and generated mapping assets."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import flet as ft
import pytest
from test_game_update import game_and_dep, installed_agent
from test_patches import _analysis

from frameport import pipeline
from frameport.core import controllers, library, paths
from frameport.core.events import Cancelled, Reporter
from frameport.core.models import Recipe
from frameport.game_update import configuration, configuration_fingerprint, plan_update
from frameport.install.installer import configuration_args, install_context
from frameport.patches import base
from frameport.patches.frame.adapter import settings_text
from frameport.patches.settings import SETTINGS, UI, XRSHIM_SETTINGS, adapter_settings
from frameport.ui.views import adapter_dialog as ad
from frameport.ui.views.settings import SettingsView


def store(patches=None):
    recipe = Recipe("test.quest", patches=patches or {"frame.adapter": {}})
    library.upsert_game(recipe.package, title="Quest", recipe=library.recipe_to_dict(recipe))
    return library.game(recipe.package)


def walk(control):
    yield control
    for child in getattr(control, "controls", []) or []:
        yield from walk(child)
    for attr in ("content", "title"):
        child = getattr(control, attr, None)
        if child is not None and not isinstance(child, str):
            yield from walk(child)
    for child in getattr(control, "actions", []) or []:
        yield from walk(child)


def test_setting_metadata_and_no_suggestion():
    spec = next(s for s in SETTINGS if s[0] == "quest_buttons")
    assert spec[1:3] == ("int", 0)
    assert "docs/CONTROLLERS.md" in spec[4]
    assert UI["quest_buttons"]["control"] == ("choice", list(controllers.CHOICES))
    assert UI["quest_buttons"]["group"] == "controllers" and UI["quest_buttons"]["level"] == "common"
    patch = base.get(controllers.QUEST_BUTTONS)
    assert patch.detect(_analysis()) is None
    assert patch.a32 and "quest_buttons" not in XRSHIM_SETTINGS
    assert patch.applies(_analysis(abis=["armeabi-v7a"]))


@pytest.mark.parametrize("global_mode", [0, 1, 2])
@pytest.mark.parametrize("override", [None, 0, 1, 2])
def test_effective_value_reaches_every_runtime_payload(global_mode, override):
    library.set_setting("input.quest_buttons", global_mode)
    patches = {controllers.QUEST_BUTTONS: {"value": override}} if override is not None else {}
    game = store(patches or {"frame.adapter": {}})
    recipe = library.recipe_from_dict(game["recipe"])
    expected = global_mode if override is None else override
    assert controllers.effective_adapter_settings(recipe.patches)["quest_buttons"] == expected
    assert install_context(recipe).adapter_settings["quest_buttons"] == expected
    args = configuration_args(game, recipe)
    assert args["settings"]["quest_buttons"] == expected
    assert f"quest_buttons={expected}\n".encode() in settings_text(recipe.patches)
    assert ad.recipe_values(game)["quest_buttons"] == expected
    # Fingerprinting is a pure operation on the recipe, not the active library preference.
    assert args["configuration"]["settings"]["quest_buttons"] == (override or 0)


def test_pure_defaults_and_global_changes_do_not_mark_games_outdated(monkeypatch):
    def forbidden(*args):
        raise AssertionError("pure settings helper performed I/O")
    with monkeypatch.context() as m:
        m.setattr(library, "setting", forbidden)
        assert adapter_settings({}, defaults={"quest_buttons": 2})["quest_buttons"] == 2
        assert adapter_settings({controllers.QUEST_BUTTONS: {"value": 0}},
                                defaults={"quest_buttons": 2})["quest_buttons"] == 0
    game, dep = game_and_dep()
    before = base.recipe_fingerprint(game["recipe"])
    config = configuration_fingerprint(game["recipe"])
    for mode in (1, 2, 3, 0):
        library.set_setting("input.quest_buttons", mode)
        assert base.recipe_fingerprint(game["recipe"]) == before
        assert configuration_fingerprint(game["recipe"]) == config
        assert not plan_update(game, dep).changed


def test_saving_game_keeps_inheritance_and_explicit_zero():
    library.set_setting("input.quest_buttons", 2)
    game = store()
    ad.save_to_recipe(game["package"], ad.recipe_values(game))
    assert library.game(game["package"])["recipe"] == game["recipe"]
    values = ad.recipe_values(game)
    values["quest_buttons"] = 0
    ad.save_to_recipe(game["package"], values, inherit_quest_buttons=False)
    assert library.game(game["package"])["recipe"]["patches"][controllers.QUEST_BUTTONS] == {"value": 0}
    ad.save_to_recipe(game["package"], values, inherit_quest_buttons=True)
    assert controllers.QUEST_BUTTONS not in library.game(game["package"])["recipe"]["patches"]
    assert ad.recipe_values(library.game(game["package"]))["quest_buttons"] == 2


def test_agent_live_default_write_does_not_mark_inherited_game_outdated(monkeypatch, tmp_path):
    game, _ = game_and_dep()
    package = game["package"]
    agent, _, record = installed_agent(monkeypatch, tmp_path)
    record.update(configuration=configuration(game["recipe"], package),
                  configuration_fp=configuration_fingerprint(game["recipe"], package))
    agent.write_deployment(package, record)
    for mode in (1, 2, 3, 0):
        agent.cmd_set_settings({"package": package, "settings": {"quest_buttons": mode}})
        deployed = agent.deployment(package)
        assert not plan_update(game, deployed).changed
    agent.cmd_set_settings({"package": package, "settings": {"quest_buttons": 1}})
    deployed = agent.deployment(package)
    # Explicit overrides still need to match, and unrelated runtime changes are never ignored.
    game["recipe"]["patches"][controllers.QUEST_BUTTONS] = {"value": 0}
    assert plan_update(game, deployed).configure
    game["recipe"]["patches"].pop(controllers.QUEST_BUTTONS)
    game["recipe"]["patches"]["adapter.scale"] = {"value": 1.8}
    assert plan_update(game, deployed).configure


def test_dialog_global_option_and_diagram_follow_choice():
    library.set_setting("input.quest_buttons", 1)
    game = store()
    dialogs = []
    app = SimpleNamespace(page=SimpleNamespace(show_dialog=dialogs.append, pop_dialog=lambda: None),
                          target=None, toast=lambda *a, **k: None, refresh_view=lambda: None,
                          _title=lambda p: "Quest")
    ad._open(app, game["package"], game, ad.recipe_values(game))
    controls = list(walk(dialogs[-1]))
    choice = next(c for c in controls if isinstance(c, ft.Dropdown) and any(o.key == "global" for o in c.options))
    image = next(c for c in controls if isinstance(c, ft.Image) and "quest-buttons" in c.src)
    assert choice.value == "global" and "Mirror" in choice.options[0].text
    assert image.src.endswith("quest-buttons-1.svg")
    choice.on_select(SimpleNamespace(control=SimpleNamespace(value="0")))
    assert image.src.endswith("quest-buttons-0.svg")
    dialogs[-1].actions[-1].on_click(None)
    assert library.game(game["package"])["recipe"]["patches"][controllers.QUEST_BUTTONS] == {"value": 0}


def test_dialog_reset_restores_global_value_without_pinning_it():
    library.set_setting("input.quest_buttons", 2)
    game = store({"frame.adapter": {}, controllers.QUEST_BUTTONS: {"value": 1}})
    dialogs = []
    app = SimpleNamespace(page=SimpleNamespace(show_dialog=dialogs.append, pop_dialog=lambda: None),
                          target=None, toast=lambda *a, **k: None, refresh_view=lambda: None,
                          _title=lambda p: "Quest")
    ad._open(app, game["package"], game, ad.recipe_values(game))
    dialogs[-1].actions[0].on_click(None)
    assert next(c for c in walk(dialogs[-1]) if isinstance(c, ft.Image) and "quest-buttons" in c.src
                ).src.endswith("quest-buttons-2.svg")
    dialogs[-1].actions[-1].on_click(None)
    assert controllers.QUEST_BUTTONS not in library.game(game["package"])["recipe"]["patches"]


class Target:
    def __init__(self, installed):
        self.games, self.calls = installed, []

    def installed(self):
        return self.games

    def set_settings(self, package, values):
        self.calls.append((package, values))


def test_apply_default_skips_overrides_and_non_quest_games():
    library.set_setting("input.quest_buttons", 2)
    store({controllers.QUEST_BUTTONS: {"value": 0}})
    library.upsert_game("flat", analysis={"extra": {"vr_kind": "none"}})
    target = Target([{"package": "test.quest"}, {"package": "inherited"}, {"package": "flat"},
                     {"package": "rift", "kind": "pcvr"}, {"package": "linux", "kind": "linux"},
                     {"package": "remote-override", "recipe": {"patches": [controllers.QUEST_BUTTONS]}}])
    assert controllers.apply_default(target, Reporter()) == {"applied": [], "needs_update": ["inherited"]}
    assert target.calls == [("inherited", {"quest_buttons": 2})]
    reporter = Reporter()
    reporter.cancelled.set()
    with pytest.raises(Cancelled):
        controllers.apply_default(target, reporter)
    assert len(target.calls) == 1


@pytest.fixture
def adapter_manifest(monkeypatch, tmp_path):
    manifest = tmp_path / "artifacts" / "SHA256SUMS"
    manifest.parent.mkdir()
    manifest.write_text("\n".join(f"{'a' * 64}  ./{abi}/libopenxr_loader_generic.so"
                                  for abi in ("arm64-v8a", "armeabi-v7a")) + "\n")
    monkeypatch.setattr(paths, "artifacts_dir", lambda: manifest.parent)
    return manifest


@pytest.mark.parametrize("abi", ["arm64-v8a", "armeabi-v7a"])
@pytest.mark.parametrize(("recorded", "installed_sha", "ready"), [
    ("a" * 64, "apk", True),
    ("a" * 64, "alternate", True),
    ("b" * 64, "apk", False),
    (None, "apk", False),
    ("a" * 64, "older-apk", False),
    ("a" * 64, None, False),
])
def test_apply_reports_current_old_and_unknown_adapters(adapter_manifest, abi, recorded, installed_sha, ready):
    game = store()
    native = {f"{abi}/libopenxr_loader_generic.so": recorded, f"{abi}/unrelated.so": "old"}
    library.set_fields(game["package"], build={"sha256": "apk", "alt_sha256": "alternate",
                                             "build_inputs": {"native": native}})
    target = Target([{"package": game["package"], "sha256": installed_sha}])
    result = controllers.apply_default(target, Reporter())
    assert result == {"applied": [game["package"]] if ready else [],
                      "needs_update": [] if ready else [game["package"]]}
    assert target.calls == [(game["package"], {"quest_buttons": 0})]


def test_apply_does_not_trust_unknown_adapter_or_manifest(adapter_manifest):
    game = store()
    library.set_fields(game["package"], build={"sha256": "apk", "build_inputs": {"native": {}}})
    target = Target([{"package": game["package"], "sha256": "apk"}])
    assert controllers.apply_default(target, Reporter())["needs_update"] == [game["package"]]
    library.set_fields(game["package"], build={"sha256": "apk", "build_inputs": {"native": {
        "arm64-v8a/libopenxr_loader_generic.so": "a" * 64}}})
    adapter_manifest.unlink()
    assert controllers.apply_default(target, Reporter())["needs_update"] == [game["package"]]


def test_live_suggestion_push_uses_inherited_value(monkeypatch):
    library.set_setting("input.quest_buttons", 2)
    monkeypatch.setattr(pipeline, "apply_suggestions", lambda *a: Recipe("test.quest"))
    target = Target([])
    assert pipeline.apply_suggestions_live("test.quest", ["adapter.scale=1"], target)
    assert target.calls[0][1]["quest_buttons"] == 2


def test_settings_apply_uses_frame_job_queue_and_selected_default(adapter_manifest):
    game = store()
    library.set_fields(game["package"], build={"sha256": "apk", "build_inputs": {"native": {
        "arm64-v8a/libopenxr_loader_generic.so": "a" * 64}}})
    target, jobs = Target([{"package": game["package"], "sha256": "apk"}, {"package": "old"}]), []
    app = SimpleNamespace(frame_state="connected", target=target,
                          submit=lambda *a, **k: jobs.append((a, k)), toast=lambda *a: None)
    controls = list(walk(SettingsView(app).controllers()))
    choice = next(c for c in controls if isinstance(c, ft.Dropdown))
    choice.on_select(SimpleNamespace(control=SimpleNamespace(value="1")))
    apply = next(c for c in controls if getattr(c, "content", None) == "Apply to installed games now")
    apply.on_click(None)
    args, kwargs = jobs[0]
    assert kwargs["kind"] == "tool-frame" and not target.calls
    message = args[1](SimpleNamespace(reporter=Reporter()))
    assert "Applied to 1 game." in message and "1 game needs Update on Frame first." in message
    assert target.calls == [(game["package"], {"quest_buttons": 1}), ("old", {"quest_buttons": 1})]


def test_controller_section_reads_default_once_and_updates_preview(monkeypatch):
    reads = []
    def snapshot():
        reads.append(True)
        assert len(reads) == 1
        return 1
    monkeypatch.setattr(controllers, "quest_buttons_default", snapshot)
    controls = list(walk(SettingsView(SimpleNamespace(frame_state="none")).controllers()))
    choice = next(c for c in controls if isinstance(c, ft.Dropdown))
    preview = next(c for c in controls if isinstance(c, ft.Image))
    assert choice.value == "1" and preview.src.endswith("quest-buttons-1.svg")
    choice.on_select(SimpleNamespace(control=SimpleNamespace(value="2")))
    assert preview.src.endswith("quest-buttons-2.svg") and len(reads) == 1
    assert library.setting("input.quest_buttons") == 2


def test_generated_controller_assets_are_current():
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, str(root / "scripts/controller_diagram.py"), "--check"], check=True)
