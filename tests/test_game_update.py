"""Update classification and the operations it must skip, including persisted live settings."""
from pathlib import Path

import pytest
from test_agent import load_agent

from frameport import pipeline
from frameport.core import library
from frameport.core.events import Reporter
from frameport.game_update import apk_recipe_fingerprint, configuration, configuration_fingerprint, plan_update
from frameport.ui.components import install_state

PKG = "com.example.update"


def game_and_dep(patches=None):
    recipe = {"package": PKG, "source": "user", "patches": patches or {"adapter.scale": {"value": 1.5}}}
    game = {"package": PKG, "title": "Example", "recipe": recipe, "analysis": {"version": "1"},
            "build": {"apk": "/missing/converted.apk", "sha256": "apk-a", "ok": True,
                      "source_version": "1", "apk_recipe_fp": apk_recipe_fingerprint(recipe, PKG)}}
    dep = {"package": PKG, "base": "/frame/game", "appid": 42, "sha256": "apk-a", "apk_present": True,
           "configuration_fp": configuration_fingerprint(recipe, PKG),
           "configuration": configuration(recipe, PKG), "data_manifest": {"main.obb": 4}}
    return game, dep


def test_custom_recipe_and_catalog_metadata_do_not_force_update():
    game, dep = game_and_dep()
    game["recipe"].update(catalog_rev="new", notes="changed", title="New", source="user")
    game["catalog_update"] = "newer-tested-recipe"
    assert not plan_update(game, dep).changed
    assert install_state(game, {"installed": [dep]}) == "installed"


def test_runtime_parameter_change_and_removal_do_not_rebuild():
    game, dep = game_and_dep()
    game["recipe"]["patches"]["adapter.scale"]["value"] = 1.2
    plan = plan_update(game, dep)
    assert plan.configure and not plan.rebuild and not plan.apk and not plan.launch_test
    game["recipe"]["patches"].clear()
    assert configuration(game["recipe"], PKG)["settings"]["scale"] == 1.0
    assert plan_update(game, dep).configure and not plan_update(game, dep).rebuild
    dep["configuration_fp"] = configuration_fingerprint(game["recipe"], PKG)
    assert not plan_update(game, dep).changed


def test_gl_hide_multiview_is_left_to_the_shim_unless_a_recipe_sets_it():
    """settings.conf carries every default; -1 lets the GL shim pick by engine (Unity/Unreal keep multiview), a
    recipe's explicit value still wins, and the build's settings library doesn't get the key (no rebuild)."""
    from frameport.patches.frame.adapter import settings_text

    game, dep = game_and_dep()
    assert configuration(game["recipe"], PKG)["settings"]["gl_hide_multiview"] == -1
    assert b"gl_hide_multiview" not in settings_text(game["recipe"]["patches"])
    for value in (0, 1):
        recipe = {**game["recipe"], "patches": {**game["recipe"]["patches"],
                                                 "adapter.gl_hide_multiview": {"value": value}}}
        assert configuration(recipe, PKG)["settings"]["gl_hide_multiview"] == value
        assert f"gl_hide_multiview={value}".encode() in settings_text(recipe["patches"])
        assert apk_recipe_fingerprint(recipe, PKG) == game["build"]["apk_recipe_fp"]  # settings.conf only
    blank = {**game["recipe"], "patches": {**game["recipe"]["patches"], "adapter.gl_hide_multiview": {}}}
    assert configuration(blank, PKG)["settings"]["gl_hide_multiview"] == -1


@pytest.mark.parametrize("patch", ["frame.unity_text_input", "device.foveation", "adapter.controller_models",
                                   "adapter.surface_native", "adapter.zink_shader_dump"])
def test_apk_support_changes_require_rebuild(patch):
    game, dep = game_and_dep()
    game["recipe"]["patches"][patch] = {}
    assert plan_update(game, dep).rebuild
    assert plan_update(game, dep).launch_test


def test_alternate_variant_can_reuse_an_existing_build():
    game, dep = game_and_dep()
    game["build"].update(alt_sha256="apk-b", alt_apk="/missing/alt.apk")
    game["recipe"]["use_alt"] = True
    plan = plan_update(game, dep)
    assert plan.apk and not plan.rebuild
    dep["sha256"] = "apk-b"
    assert not plan_update(game, dep).rebuild
    game["build"].pop("alt_sha256")
    assert plan_update(game, dep).rebuild


def test_data_and_artwork_have_separate_actions():
    game, dep = game_and_dep()
    plan = plan_update(game, dep, data_manifest={"main.obb": 5})
    assert plan.data and plan.launch_test and not plan.apk and not plan.rebuild
    assert not plan_update(game, dep, data_manifest={"main.obb": 5}, apk_only=True).data
    game["steam_art_stale"] = True
    plan = plan_update(game, dep)
    assert plan.artwork and plan.shortcut and not plan.launch_test


def test_legacy_and_source_changes_use_full_path():
    game, dep = game_and_dep()
    game["build"].pop("apk_recipe_fp")
    assert plan_update(game, dep).rebuild
    game, dep = game_and_dep()
    game["analysis"]["version"] = "2"
    assert plan_update(game, dep).rebuild


def test_superseded_setting_does_not_stay_pending():
    game, dep = game_and_dep({"adapter.haptic_fix": {"value": 1}})
    game["build"]["superseded"] = {"adapter.haptic_fix": "upstream"}
    game["build"]["apk_recipe_fp"] = apk_recipe_fingerprint(game["recipe"], PKG, game["build"]["superseded"])
    dep["configuration_fp"] = configuration_fingerprint(game["recipe"], PKG, game["build"]["superseded"])
    assert not plan_update(game, dep).changed
    assert configuration(game["recipe"], PKG, game["build"]["superseded"])["settings"]["haptic_fix"] == 0
    game["recipe"]["patches"].clear()  # removing an omitted workaround is also a no-op
    assert not plan_update(game, dep).changed


class Target:
    label = "test-frame"

    def __init__(self, dep):
        self.dep, self.calls = dep, []

    def installed(self):
        return [self.dep]

    def update_existing(self, entry, dep, plan, recipe, reporter, manifest=None):
        self.calls.append("configure" if not plan.data else "data")
        return {"base": dep["base"], "appid": dep["appid"]}

    def update_steam_art(self, package, reporter, title=None):
        self.calls.append("artwork")

    def install(self, *args, **kwargs):
        self.calls.append("install")
        return {"base": self.dep["base"], "appid": self.dep["appid"]}

    def add_to_library(self, *args):
        self.calls.append("shortcut")


def store_game(monkeypatch, game):
    monkeypatch.setattr(library, "_migrate", lambda data: False)
    library.upsert_game(PKG, **{k: v for k, v in game.items() if k != "package"}, apk="/missing/source.apk")
    monkeypatch.setattr(pipeline, "_find_late_data", lambda entry, rep: entry)
    monkeypatch.setattr(pipeline, "_build_inputs_changed", lambda entry: False)
    monkeypatch.setattr(pipeline, "build_game", lambda *a, **k: pytest.fail("unnecessary rebuild"))


def test_pipeline_configure_without_any_local_apk(monkeypatch):
    game, dep = game_and_dep()
    game["recipe"]["patches"]["adapter.scale"]["value"] = 1.2
    store_game(monkeypatch, game)
    target = Target(dep)
    result = pipeline.install_game(PKG, target, Reporter())
    assert target.calls == ["configure", "shortcut"]
    assert result["launch_test_required"] is False
    assert result["shortcut_required"] is True


def test_pipeline_noop_and_artwork_skip_build_and_install(monkeypatch):
    game, dep = game_and_dep()
    store_game(monkeypatch, game)
    target = Target(dep)
    result = pipeline.install_game(PKG, target, Reporter())
    # nothing to send, but Steam may have lost the shortcut since: the agent checks it (no restart if present)
    assert target.calls == ["shortcut"] and result["launch_test_required"] is False
    target.calls.clear()
    library.update_game(PKG, lambda entry: entry.update(steam_art_stale=True))
    pipeline.install_game(PKG, target, Reporter())
    assert target.calls == ["artwork", "shortcut"]


def test_pipeline_artwork_that_rewrote_the_shortcut_is_not_added_twice(monkeypatch):
    game, dep = game_and_dep()
    store_game(monkeypatch, game)
    target = Target(dep)
    target.update_steam_art = lambda *a, **k: (target.calls.append("artwork"), {"library": True})[1]
    library.update_game(PKG, lambda entry: entry.update(steam_art_stale=True))
    result = pipeline.install_game(PKG, target, Reporter())
    assert target.calls == ["artwork"] and result["shortcut_required"] is False


def test_pipeline_cli_mode_asks_for_the_shortcut_check(monkeypatch):
    game, dep = game_and_dep()
    store_game(monkeypatch, game)
    target = Target(dep)
    result = pipeline.install_game(PKG, target, Reporter(), add_to_library=False)
    assert target.calls == [] and result["shortcut_required"] is True


def test_pipeline_changed_data_without_local_apk(monkeypatch, tmp_path):
    game, dep = game_and_dep()
    data = tmp_path / "data"
    data.mkdir()
    (data / "main.obb").write_bytes(b"12345")
    game["data_dir"] = str(data)
    store_game(monkeypatch, game)
    target = Target(dep)
    result = pipeline.install_game(PKG, target, Reporter())
    assert target.calls == ["data", "shortcut"] and result["launch_test_required"] is True
    target.calls.clear()
    pipeline.install_game(PKG, target, Reporter(), apk_only=True)
    assert target.calls == ["shortcut"]


def installed_agent(monkeypatch, tmp_path):
    agent = load_agent(monkeypatch, tmp_path)
    base = tmp_path / "Applications/quest-frame" / PKG
    (base / "lepton-app").mkdir(parents=True)
    (base / "lepton-app/game.apk").write_bytes(b"apk")
    dep = {"package": PKG, "title": "Example", "base": str(base), "appid": 42, "sha256": "apk-a",
           "tags": ["Favorite"], "apk": "original.apk", "apk_size": 3, "data_manifest": {"modded.obb": 4}}
    dep["apk_stat"] = {"size": 3, "mtime_ns": (base / "lepton-app/game.apk").stat().st_mtime_ns}
    agent.write_deployment(PKG, dep)
    monkeypatch.setattr(agent, "new_base", lambda args, pkg, deployment: (deployment["base"], 10 << 30))
    monkeypatch.setattr(agent, "container_running", lambda appid: False)
    monkeypatch.setattr(agent, "lepton_path", lambda: ("/lepton", None))
    monkeypatch.setattr(agent, "install_controller_models", lambda *a: {"ok": True})
    monkeypatch.setattr(agent, "write_launcher", lambda *a: None)
    monkeypatch.setattr(agent, "sync_game_config", lambda *a: {})
    return agent, base, dep


def test_agent_configuration_preserves_apk_data_tags_and_ignores_staging(monkeypatch, tmp_path):
    agent, base, _ = installed_agent(monkeypatch, tmp_path)
    pending = base / "incoming"
    pending.mkdir()
    (pending / "game.apk").write_bytes(b"pending different apk")
    recipe = {"patches": {"adapter.scale": {"value": 1.5}}}
    applied = configuration(recipe, PKG)
    result = agent.cmd_configure({"package": PKG, "apk_sha256": "apk-a", "settings": applied["settings"],
                                  "configuration": applied, "configuration_fp": configuration_fingerprint(recipe, PKG),
                                  "recipe": {"patches": ["adapter.scale"]}})
    assert "data_manifest" not in result["deployment"]
    dep = agent.deployment(PKG)
    assert dep["tags"] == ["Favorite"] and dep["apk"] == "original.apk" and dep["sha256"] == "apk-a"
    assert dep["data_manifest"] == {"modded.obb": 4}
    assert (base / "lepton-app/game.apk").read_bytes() == b"apk"
    assert (pending / "game.apk").exists()
    # Live writes update the applied baseline, including resetting a deleted recipe override.
    agent.cmd_set_settings({"package": PKG, "settings": {"scale": 1.0}})
    assert agent.deployment(PKG)["configuration_fp"] == configuration_fingerprint({"patches": {}}, PKG)
    settings_conf = (base / "settings.conf").read_text()
    for bad in (".", "1.2.3", "-", "abc"):  # refused before anything is written
        with pytest.raises(agent.AgentError):
            agent.cmd_set_settings({"package": PKG, "settings": {"scale": bad}})
    assert (base / "settings.conf").read_text() == settings_conf


@pytest.mark.parametrize("failure", ["running", "missing", "different", "drive"])
def test_agent_refuses_unsafe_configurations(monkeypatch, tmp_path, failure):
    agent, base, original = installed_agent(monkeypatch, tmp_path)
    sha = "apk-a"
    if failure == "running":
        monkeypatch.setattr(agent, "container_running", lambda appid: True)
    elif failure == "missing":
        (base / "lepton-app/game.apk").unlink()
    elif failure == "different":
        sha = "other-apk"
    else:
        def unavailable(*args):
            raise agent.AgentError("drive missing")
        monkeypatch.setattr(agent, "new_base", unavailable)
    with pytest.raises(agent.AgentError):
        agent.cmd_configure({"package": PKG, "apk_sha256": sha, "settings": {"scale": 1.5}})
    assert agent.deployment(PKG) == original
    assert not (base / "settings.conf").exists()


def test_generated_config_removal_restores_original_and_preserves_user_changes(monkeypatch, tmp_path):
    agent, base, _ = installed_agent(monkeypatch, tmp_path)
    files = Path(agent.data_files_dir(str(base), PKG))
    files.mkdir(parents=True)
    (files / "user.cfg").write_text("original")
    args = {"package": PKG, "apk_sha256": "apk-a", "files": {"user.cfg": "managed"}}
    agent.cmd_configure(args)
    agent.cmd_configure({**args, "files": {}})
    assert (files / "user.cfg").read_text() == "original"
    agent.cmd_configure(args)
    (files / "user.cfg").write_text("user mod")
    agent.cmd_configure({**args, "files": {}})
    assert (files / "user.cfg").read_text() == "user mod"


def test_failed_configuration_does_not_record_success(monkeypatch, tmp_path):
    agent, base, original = installed_agent(monkeypatch, tmp_path)
    def fail(*args):
        raise RuntimeError("configuration sync failed")
    monkeypatch.setattr(agent, "sync_game_config", fail)
    with pytest.raises(RuntimeError):
        agent.cmd_configure({"package": PKG, "apk_sha256": "apk-a", "settings": {"scale": 1.5},
                             "configuration_fp": "new"})
    assert agent.deployment(PKG) == original


def test_generated_paths_checked_before_writing_settings(monkeypatch, tmp_path):
    agent, base, original = installed_agent(monkeypatch, tmp_path)
    with pytest.raises(agent.AgentError):
        agent.cmd_configure({"package": PKG, "apk_sha256": "apk-a", "settings": {"scale": 1.5},
                             "files": {"../../escape": "bad"}})
    assert agent.deployment(PKG) == original
    assert not (base / "settings.conf").exists()


def test_build_inputs_ignore_unrelated_native_assets_and_detect_source_changes(monkeypatch, tmp_path):
    import zipfile

    from frameport.core import paths
    from frameport.tools import toolchain

    game, _ = game_and_dep()
    source = tmp_path / "source.apk"
    source.write_bytes(b"source")
    game["apk"] = str(source)
    native = tmp_path / "artifacts"
    (native / "arm64-v8a").mkdir(parents=True)
    (native / "arm64-v8a/chosen.so").write_bytes(b"native")
    manifest = native / "SHA256SUMS"
    manifest.write_text("a" * 64 + "  ./arm64-v8a/chosen.so\n")
    output = tmp_path / "output.apk"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("lib/arm64-v8a/chosen.so", b"native")
    monkeypatch.setattr(paths, "artifacts_dir", lambda: native)
    monkeypatch.setattr(toolchain, "overport_jar", lambda: None)
    game["build"].update(pipeline._build_record(game, output))
    assert not pipeline._build_inputs_changed(game)
    manifest.write_text(manifest.read_text() + "b" * 64 + "  ./arm64-v8a/unrelated.so\n")
    assert not pipeline._build_inputs_changed(game)
    manifest.write_text("c" * 64 + "  ./arm64-v8a/chosen.so\n")
    assert pipeline._build_inputs_changed(game)
    manifest.write_text("a" * 64 + "  ./arm64-v8a/chosen.so\n")
    source.write_bytes(b"different source, same version")
    assert pipeline._build_inputs_changed(game)


def test_stage_timings_include_the_final_stage(monkeypatch):
    from frameport.core import events

    now = [10.0]
    monkeypatch.setattr(events.time, "monotonic", lambda: now[0])
    reporter = Reporter()
    reporter.stage("Build")
    now[0] += 3
    reporter.stage("Upload")
    now[0] += 2
    assert reporter.stage_timings() == {"Build": 3, "Upload": 2}
    assert reporter.stage_timings() == {"Build": 3, "Upload": 2}  # snapshots do not double-count


def test_artwork_update_transfers_only_changed_slot(monkeypatch, tmp_path):
    from frameport.artwork import sources, steam
    from frameport.install import installer

    portrait, hero = tmp_path / "portrait.png", tmp_path / "hero.png"
    portrait.write_bytes(b"portrait")
    hero.write_bytes(b"old hero")
    art = {"portrait": portrait, "hero": hero}
    monkeypatch.setattr(steam, "steam_set_for", lambda package: art)
    monkeypatch.setattr(sources, "icon_source", lambda package: "custom")
    manifest = installer.art_manifest(PKG, art)
    hero.write_bytes(b"new hero")
    calls = []
    dep = {"package": PKG, "title": "Example", "anchor": "/frame/anchor", "artwork_manifest": manifest}

    class Frame:
        def agent(self, command, **args):
            calls.append(command)
            return {"games": [dep]} if command == "list_installed" else {"verified": True}

        def run(self, command):
            calls.append("mkdir")

        def put(self, local, remote, **kwargs):
            calls.append(local.name)
    monkeypatch.setattr(installer, "add_to_steam", lambda *a: calls.append("shortcut"))
    installer.update_steam_art(Frame(), PKG, Reporter(), title="Example")
    assert "hero.png" in calls and "portrait.png" not in calls
    assert "record_artwork" in calls and "shortcut" in calls
    calls.clear()
    dep["artwork_manifest"] = installer.art_manifest(PKG, art)
    installer.update_steam_art(Frame(), PKG, Reporter(), title="Example")
    assert calls == ["list_installed"]


def test_artwork_update_without_a_recorded_manifest_starts_clean(monkeypatch, tmp_path):
    """Installs before agent v86 have no artwork_manifest: an old portrait.jpg must not stay next to portrait.png."""
    from frameport.artwork import sources, steam
    from frameport.install import installer

    portrait = tmp_path / "portrait.png"
    portrait.write_bytes(b"portrait")
    monkeypatch.setattr(steam, "steam_set_for", lambda package: {"portrait": portrait})
    monkeypatch.setattr(sources, "icon_source", lambda package: "custom")
    monkeypatch.setattr(installer, "send_icon_source", lambda *a: None)
    monkeypatch.setattr(installer, "add_to_steam", lambda *a: {"state": "done"})
    dep = {"package": PKG, "title": "Example", "anchor": "/frame/anchor"}
    commands, puts = [], []

    class Frame:
        def agent(self, command, **args):
            return {"games": [dep]} if command == "list_installed" else {"verified": True}

        def run(self, command):
            commands.append(command)

        def put(self, local, remote, **kwargs):
            puts.append(remote)

    result = installer.update_steam_art(Frame(), PKG, Reporter(), title="Example")
    assert commands[0] == "rm -rf -- '/frame/anchor/artwork'" and commands[1].startswith("mkdir -p")
    assert puts == ["/frame/anchor/artwork/portrait.png"] and result["library"] is True


def test_agent_artwork_manifest_requires_matching_uploaded_content(monkeypatch, tmp_path):
    agent, base, _ = installed_agent(monkeypatch, tmp_path)
    art = base / "artwork"
    art.mkdir()
    (art / "hero.png").write_bytes(b"hero")
    manifest = {"hero.png": agent.sha256_file(str(art / "hero.png"))}
    assert agent.cmd_record_artwork({"package": PKG, "manifest": manifest})["verified"]
    with pytest.raises(agent.AgentError):
        agent.cmd_record_artwork({"package": PKG, "manifest": {"hero.png": "f" * 64}})
    assert agent.deployment(PKG)["artwork_manifest"] == manifest


def test_apk_only_finalize_does_not_apply_staged_data_or_artwork(monkeypatch, tmp_path):
    agent, base, _ = installed_agent(monkeypatch, tmp_path)
    (base / "lepton-app/obb").mkdir()
    (base / "lepton-app/obb/main.obb").write_bytes(b"user mod")
    incoming = base / "incoming"
    (incoming / "obb").mkdir(parents=True)
    (incoming / "obb/main.obb").write_bytes(b"old staged copy")
    (incoming / "game.apk").write_bytes(b"new apk")
    (base / "incoming-artwork").mkdir()
    (base / "incoming-artwork/hero.png").write_bytes(b"old staged art")
    monkeypatch.setattr(agent, "cmd_prepare", lambda args: {"base": str(base), "anchor": str(base), "appid": 42,
                                                          "incoming": str(incoming), "lepton": "/lepton"})
    result = agent.cmd_finalize({"package": PKG, "title": "Example", "preserve_data": True,
                                 "consume_artwork": False,
                                 "apk_sha256": agent.sha256_file(str(incoming / "game.apk"))})
    assert result["moved_data_files"] == 0
    assert (base / "lepton-app/obb/main.obb").read_bytes() == b"user mod"
    assert (incoming / "obb/main.obb").read_bytes() == b"old staged copy"
    assert not (base / "artwork").exists()
    assert (base / "incoming-artwork/hero.png").exists()


def test_data_digest_fast_path_and_subset_manifest():
    from frameport.game_update import fingerprint

    game, dep = game_and_dep()
    dep.pop("data_manifest")
    dep["data_fp"] = fingerprint({"main.obb": 4})
    assert not plan_update(game, dep, data_manifest={"main.obb": 4}).data
    assert plan_update(game, dep, data_manifest={"main.obb": 5}).data
    dep["data_manifest"] = {"main.obb": 4, "unused.obb": 10}
    assert not plan_update(game, dep, data_manifest={"main.obb": 4}).data


def test_routine_deployment_summary_excludes_large_manifests():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "agent/frameport_agent.py"
    spec = importlib.util.spec_from_file_location("frameport_agent_summary", path)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    full = {"package": PKG, "data_fp": "digest", "configuration_fp": "runtime", "sha256": "apk",
            "data_manifest": {"main.obb": 123}, "configuration": {"settings": {}}, "managed_config_files": {}}
    summary = agent.deployment_summary(full)
    assert summary == {"package": PKG, "data_fp": "digest", "configuration_fp": "runtime", "sha256": "apk"}
    assert "data_manifest" in full  # summarizing does not mutate the durable record


@pytest.mark.parametrize("changed_data, force, preserve", [(False, False, True), (True, False, False),
                                                           (False, True, False)])
def test_apk_recipe_update_preserves_unchanged_source_data(monkeypatch, tmp_path, changed_data, force, preserve):
    game, dep = game_and_dep()
    data = tmp_path / "data"
    data.mkdir()
    (data / "main.obb").write_bytes(b"12345" if changed_data else b"1234")
    game["data_dir"] = str(data)
    game["recipe"]["patches"]["frame.unity_text_input"] = {}
    store_game(monkeypatch, game)
    output = tmp_path / "built.apk"
    output.write_bytes(b"checked new build")
    def build(package, reporter):
        current = library.game(package)
        library.update_game(package, lambda entry: entry["build"].update(
            apk=str(output), sha256=pipeline.builder.sha256(output),
            apk_recipe_fp=apk_recipe_fingerprint(current["recipe"], package)))
        return {"ok": True}
    monkeypatch.setattr(pipeline, "build_game", build)
    target = Target(dep)
    installed = []
    def install(*args, **kwargs):
        installed.append(args[6])
        return {"base": dep["base"], "appid": dep["appid"]}
    target.install = install
    result = pipeline.install_game(PKG, target, Reporter(), force=force)
    assert installed == [preserve]
    assert result["launch_test_required"] is True


def test_agent_drops_records_an_older_app_did_not_send(monkeypatch, tmp_path):
    """An app before agent v86 installs without fingerprints: a newer PC mustn't skip work on the old record."""
    agent, base, dep = installed_agent(monkeypatch, tmp_path)
    agent.write_deployment(PKG, {**dep, "configuration_fp": "old", "apk_recipe_fp": "old",
                                 "artwork_manifest": {"portrait.jpg": "a" * 64}})
    anchor = tmp_path / "Applications/quest-frame" / PKG
    prep = {"base": str(base), "anchor": str(anchor), "appid": 42, "lepton": "/lepton"}
    # no new art: the record of the art in place stays
    agent.configure_install({"package": PKG, "title": "Example", "settings": {}}, prep, 0)
    record = agent.deployment(PKG)
    assert "configuration_fp" not in record and "apk_recipe_fp" not in record
    assert record["artwork_manifest"] == {"portrait.jpg": "a" * 64}
    # new art moved in without a manifest: its record is dropped too
    (base / "incoming-artwork").mkdir()
    (base / "incoming-artwork/portrait.png").write_bytes(b"new")
    agent.configure_install({"package": PKG, "title": "Example", "settings": {}, "consume_artwork": True}, prep, 0)
    assert "artwork_manifest" not in agent.deployment(PKG)
    assert (anchor / "artwork/portrait.png").exists()


def test_agent_config_written_by_an_older_frameport_is_not_kept_as_the_users(monkeypatch, tmp_path):
    agent, base, dep = installed_agent(monkeypatch, tmp_path)
    files = base / "lepton-data/external/Android/data" / PKG / "files"
    files.mkdir(parents=True)
    (files / "user.cfg").write_text("r_x=0\n")  # FramePort's own, from before agent v86 kept records
    (files / "mine.cfg").write_text("users own\n")
    prep = {"base": str(base), "anchor": str(tmp_path / "Applications/quest-frame" / PKG), "appid": 42,
            "lepton": "/lepton"}
    agent.configure_install({"package": PKG, "title": "Example", "settings": {},
                             "files": {"user.cfg": "r_x=0\n", "mine.cfg": "r_y=1\n"}}, prep, 0)
    managed = agent.deployment(PKG)["managed_config_files"]
    assert managed["user.cfg"]["backup"] is False and managed["mine.cfg"]["backup"] is True
    # the recipe drops both: FramePort's file goes away, the user's own comes back
    agent.configure_install({"package": PKG, "title": "Example", "settings": {}, "files": {}}, prep, 0)
    assert not (files / "user.cfg").exists() and (files / "mine.cfg").read_text() == "users own\n"
