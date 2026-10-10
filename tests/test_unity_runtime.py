"""Unity XR settings fixed in libil2cpp.so: OVRManager's runtime MSAA and the Oculus XR Plugin's multiview."""
from pathlib import Path

from test_patches import _analysis

from frameport.analysis import il2cpp
from frameport.patches import base
from frameport.patches.frame import unity_runtime as R
from frameport.patches.frame import unity_text_input as U
from frameport.validate.triage import triage


def _lib() -> bytes:
    return sorted(Path(__file__).parent.joinpath("fixtures").glob("libfake*_arm64.so"))[0].read_bytes()


def test_all_il2cpp_patches_share_one_cpp2il_lookup():
    base.load_all()
    for p in (U.UnityTextInput, R.UnityRuntimeMsaa, R.UnityMultiPass):
        for cls, methods in p.targets.items():
            assert set(methods) <= set(U.ALL_TARGETS[cls])
    # keyed by the build's metadata: an earlier patch's bytes in libil2cpp.so don't force another Cpp2IL run
    assert il2cpp._cache_file(b"a" * 10, b"meta") == il2cpp._cache_file(b"b" * 10, b"meta")
    assert il2cpp._cache_file(b"a" * 10, b"meta") != il2cpp._cache_file(b"a" * 10, b"other")


def test_runtime_patches_write_zero_returns():
    lib = _lib()
    lo, _hi = U._executable(lib)[0]
    a, b = (lo + 0x40) & ~3, (lo + 0x80) & ~3
    found = {"Assembly-CSharp/OVRDisplay.cs": {"get_recommendedMSAALevel": (a, 0x5C)},
             "Unity.XR.Oculus/Unity/XR/Oculus/OculusSettings.cs": {"GetStereoRenderingMode": (b, 8)}}
    msaa, notes = U.patch_methods(lib, found, R.UnityRuntimeMsaa.targets)
    assert msaa[a:a + 8] == R.RET_ZERO and msaa[b:b + 8] == lib[b:b + 8] and "OVRDisplay" in notes[0]
    multi, _ = U.patch_methods(lib, found, R.UnityMultiPass.targets)
    assert multi[b:b + 8] == R.RET_ZERO and multi[a:a + 8] == lib[a:a + 8]


def test_runtime_msaa_suggested_for_gles_ovr_games_only():
    base.load_all()
    p = base.get("frame.unity_runtime_msaa_off")
    gles = _analysis(libs=["libil2cpp.so", "libunity.so"], graphics="GLES or unknown (no Vulkan declaration)",
                     extra={"ovr_runtime_msaa": True})
    assert p.detect(gles).recommended
    assert p.detect(_analysis(libs=["libil2cpp.so"], extra={"ovr_runtime_msaa": True})) is None  # Vulkan
    gles.extra["ovr_runtime_msaa"] = False
    assert p.detect(gles) is None and p.applies(gles)
    assert base.get("frame.unity_multipass").detect(gles) is None  # opt-in: only for the symptom
    assert not base.get("frame.unity_multipass").applies(_analysis(libs=["libunity.so"]))  # Mono: no Cpp2IL


def test_triage_points_runtime_msaa_to_the_patch():
    log = ("09-28 17:39:01.000  1000  1000 I ActivityManager: Start proc 1147:com.example.game/u0a55 for activity\n"
           "09-28 17:39:04.000  1147  1174 I Unity   : The current MSAA level is 0, but the recommended MSAA level is "
           "4. Switching to the recommended level.\n")
    f = {x.id: x for x in triage(log, "RUNNING", "com.example.game").findings}
    assert "frame.unity_runtime_msaa_off" in f["unity-runtime-msaa"].suggest



def test_library_recipes_follow_their_catalog_entry(tmp_path, monkeypatch):
    import json

    from frameport.core import library
    from frameport.recommend import catalog, engine

    monkeypatch.setattr(library, "_path", lambda: tmp_path / "library.json")
    entry = catalog.CatalogEntry.from_dict({"package": "com.x.game", "title": "X", "status": "issues",
                                            "frame": ["frame.unity_multipass"], "updated": "2026-10-04"}, "bundled")
    monkeypatch.setattr(catalog, "lookup", lambda pkg: entry if pkg in ("com.x.game", "com.x.mine") else None)
    an = {"package": "com.x.game", "engine": "Unity", "libs": ["libil2cpp.so", "libunity.so"], "abis": ["arm64-v8a"]}
    games = {"com.x.game": {"analysis": an, "recipe": {"package": "com.x.game", "source": "heuristics"}},
             "com.x.mine": {"analysis": dict(an, package="com.x.mine"),
                            "recipe": {"package": "com.x.mine", "source": "user", "patches": {"frame.adapter": {}}}}}
    (tmp_path / "library.json").write_text(json.dumps({"games": games, "settings": {}}))
    got = library.load()["games"]
    r = got["com.x.game"]["recipe"]
    assert "frame.unity_multipass" in r["patches"] and r["status"] == "issues" and r["catalog_rev"] == entry.rev()
    mine = got["com.x.mine"]["recipe"]  # the user's own choice stays (one-time migrations aside)
    assert "frame.adapter" in mine["patches"] and "frame.unity_multipass" not in mine["patches"]
    assert not library._follow_catalog(library.load())  # nothing changed since: no re-derive on every load
    assert engine.suggest(library.analysis_from_dict(an)).catalog_rev == entry.rev()


def test_maintained_entry_verified_later_beats_the_users_shared_one():
    from frameport.recommend import catalog

    def e(origin, **kw):
        return catalog.CatalogEntry.from_dict({"package": "p.q", "title": "P", **kw}, origin)
    user = e("user", verified={"date": "2026-10-03"})
    fixed, old = e("bundled", updated="2026-10-04"), e("bundled", verified={"date": "2026-09-28"})
    assert catalog._newer(fixed, user) and not catalog._newer(old, user) and not catalog._newer(None, user)


def test_inlined_getter_field_read_is_rewritten():
    lib = bytearray(_lib())
    lo, _hi = U._executable(bytes(lib))[0]
    m = (lo + 0x40) & ~3
    lib[m:m + 4] = (0xB9401C00 | 20 << 5 | 22).to_bytes(4, "little")  # ldr w22, [x20, #0x1c]
    lib[m + 4:m + 8] = (0x39408000 | 20 << 5 | 8).to_bytes(4, "little")  # ldrb w8, [x20, #0x20]: another field
    out, notes = U.patch_field_loads(bytes(lib), (m, 16), 0x1C, 0, "Initialize")
    assert out[m:m + 4] == (0x52800000 | 22).to_bytes(4, "little") and out[m + 4:m + 8] == lib[m + 4:m + 8]
    assert U.patch_field_loads(out, (m, 16), 0x1C, 0, "Initialize")[0] is None  # idempotent
    import pytest

    with pytest.raises(RuntimeError, match="no load"):
        U.patch_field_loads(bytes(_lib()), (m, 16), 0x1C, 0, "Initialize")


def test_cpp2il_field_offsets_are_parsed():
    cs = ("public class OculusSettings : ScriptableObject\n{\n\t[SerializeField]\n"
          "\tpublic StereoRenderingModeAndroid m_StereoRenderingModeAndroid; //Field offset: 0x1C\n}\n")
    assert il2cpp.parse_methods(cs, ["field:m_StereoRenderingModeAndroid", "Missing"]) == {
        "field:m_StereoRenderingModeAndroid": (0x1C, 0)}
    assert "field:m_StereoRenderingModeAndroid" in U.ALL_TARGETS[R.UnityMultiPass.SETTINGS]


def test_recipe_changes_and_revised_patches_mark_builds_outdated():
    from frameport.patches.base import recipe_fingerprint, revised_since_unrecorded
    from frameport.ui.components import recipe_changed

    base.load_all()
    r = {"patches": {"frame.unity_text_input": {}}, "use_alt": False}
    game = {"recipe": r, "build": {"sha256": "a", "recipe_fp": recipe_fingerprint(r)}}
    assert not recipe_changed(game)
    r["use_alt"] = True  # e.g. a catalog fix switched to the alternate build
    assert recipe_changed(game)
    assert not revised_since_unrecorded({"patches": {"frame.unity_text_input": {}}})
    assert revised_since_unrecorded({"patches": {"frame.unity_multipass": {}}})  # fixed after 0.6.3
    assert revised_since_unrecorded({"patches": {"adapter.vk_shader_fix": {"value": "x"}}})  # needs the new shim


def test_packaged_app_shows_artwork_by_file_path(tmp_path, monkeypatch):
    from frameport.artwork import thumbs

    monkeypatch.setattr(thumbs, "user_data_dir", lambda: tmp_path)
    art = tmp_path / "artwork" / "p.q" / "cover.jpg"
    assert thumbs.asset_url(art) == "/artwork/p.q/cover.jpg"  # web view / source run: URL under assets_dir
    thumbs.use_file_paths(True)
    try:
        assert thumbs.asset_url(art) == str(art.resolve())
    finally:
        thumbs.use_file_paths(False)


def test_unity_oculus_check_for_unity_with_the_check():
    base.load_all()
    p = base.get("frame.unity_oculus_check")
    old = _analysis(libs=["libunity.so", "libOVRPlugin.so"],
                    extra={"unity_version": "2017.4.23f1", "unity_oculus_check": True})
    assert p.applies(old) and p.detect(old).recommended
    new = _analysis(libs=["libunity.so", "libOVRPlugin.so"],
                    extra={"unity_version": "2019.4.35f1", "unity_oculus_check": True})
    # 2019 too (BattleSisters, Unity 2019.4, stayed a 2D app without it)
    assert p.applies(new) and p.detect(new).recommended
    # the frame-wait shim: Unity's built-in VR loop (2017-2018, 2019 without the Oculus XR Plugin), not XR Plugin games
    assert p.legacy_loop(old) and p.legacy_loop(new)
    assert not p.legacy_loop(_analysis(libs=["libunity.so", "libOVRPlugin.so", "libOculusXRPlugin.so"],
                                       extra={"unity_version": "2019.4.35f1"}))
    assert not p.legacy_loop(_analysis(libs=["libunity.so"], extra={"unity_version": "2022.3.19f1"}))
    none = _analysis(libs=["libunity.so", "libOVRPlugin.so"], extra={"unity_version": "2019.4.35f1"})
    assert not p.applies(none) and p.detect(none) is None  # no check in libunity.so: nothing to change
    log = "10-04 15:08:01.000  1213  1235 I Unity   : [NewtonVR] Critical Error: Oculus / SteamVR not setup properly"
    assert "frame.unity_oculus_check" in {s for f in triage(log, "RUNNING", None).findings for s in f.suggest}


def test_app_update_refreshes_derived_recipes_once(tmp_path, monkeypatch):
    import json

    from frameport.core import library

    monkeypatch.setattr(library, "_path", lambda: tmp_path / "library.json")
    monkeypatch.setattr(library, "REFRESH_ON_UPDATE", True)
    an = {"package": "com.own.engine", "engine": "Other", "libs": ["libgame.so"], "abis": ["arm64-v8a"]}
    custom = {"frame.adapter": {}, "adapter.scale": {"value": 1.5}}
    games = {"com.own.engine": {"analysis": an, "recipe": {"package": "com.own.engine", "source": "heuristics",
                                                          "patches": dict(custom)}},
             "com.mine": {"analysis": dict(an, package="com.mine"),
                          "recipe": {"package": "com.mine", "source": "user", "patches": dict(custom)}}}
    (tmp_path / "library.json").write_text(json.dumps({"games": games, "settings": {"recipes.app_version": "0.0.1"}}))
    got = library.load()["games"]
    assert "frame.vk_sanitize" in got["com.own.engine"]["recipe"]["patches"]  # a newer automatic fix reaches it
    assert "adapter.scale" not in got["com.own.engine"]["recipe"]["patches"]  # derived again from scratch
    assert got["com.mine"]["recipe"]["patches"]["adapter.scale"] == {"value": 1.5}  # the user's own recipe stays
    assert not library._follow_catalog(library.load())  # once per app version


def test_sdl_clipboard_patch_for_2d_sdl_apps():
    from frameport.recommend import engine

    base.load_all()
    a = _analysis(package="org.love2d.android", engine="Other", libs=["liblove.so"], is_overport_output=False,
                  extra={"sdl_java": True, "vr_kind": "none"})
    r = engine.suggest(a)
    assert "frame.sdl_clipboard" in r.patches and not r.as_is  # an APK edit: not installed unchanged
    a.extra["sdl_java"] = False
    assert "frame.sdl_clipboard" not in engine.suggest(a).patches


def test_uninstall_can_delete_the_games_files_on_this_pc(tmp_path):
    from frameport import pipeline
    from frameport.core import library
    from frameport.core.paths import output_dir, user_data_dir

    game = tmp_path / "Game v1.2"
    (game / "com.x.game").mkdir(parents=True)
    (game / "com.x.game.apk").write_bytes(b"PK" * 10)
    (game / "com.x.game/main.obb").write_bytes(b"o" * 30)
    shared = tmp_path / "Shared"
    (shared / "data").mkdir(parents=True)
    (shared / "b.apk").write_bytes(b"PK")
    conv = output_dir() / "Game v1.2"
    conv.mkdir(parents=True)
    (conv / "com.x.game.apk").write_bytes(b"c")
    key = user_data_dir() / "overport-workspace/signatures/com.x.game.keystore"
    key.parent.mkdir(parents=True)
    key.write_bytes(b"k")
    library.upsert_game("com.x.game", apk=str(game / "com.x.game.apk"), data_dir=str(game / "com.x.game"),
                        build={"apk": str(conv / "com.x.game.apk")})
    library.upsert_game("com.y.other", apk=str(shared / "b.apk"), data_dir=str(shared / "data"))
    library.upsert_game("com.z.third", apk=str(shared / "c.apk"), data_dir=str(shared / "data"))
    assert set(pipeline.local_game_files("com.x.game")) == {game / "com.x.game.apk", game / "com.x.game",
                                                            conv / "com.x.game.apk"}
    assert pipeline.local_game_files("com.y.other") == [shared / "b.apk"]  # its data folder is another game's too
    done, freed = pipeline.delete_local_files("com.x.game")
    assert not game.exists() and freed == 51 and library.game("com.x.game") is None
    assert key.exists() and shared.exists()  # signing keys and other games' files stay


def test_deleting_a_games_files_takes_its_download_manifest(tmp_path):
    from frameport import pipeline
    from frameport.core import library

    def release(name, extra=""):
        d = tmp_path / name
        (d / "com.x.game").mkdir(parents=True)
        (d / "com.x.game.apk").write_bytes(b"PK")
        (d / "com.x.game/main.1.com.x.game.obb").write_bytes(b"o")
        (d / "release.manifest").write_text(
            "﻿#VRPRELEASEMANIFEST 1.0\nGame Name;Release Name\nGame;Game v1\n\n#filelist\ntype;name;size\n"
            "f;./com.x.game.apk;2\nd;./com.x.game;0\nf;./com.x.game/main.1.com.x.game.obb;1\n" + extra,
            encoding="utf-8")
        return d

    game = release("Game v1")
    library.upsert_game("com.x.game", apk=str(game / "com.x.game.apk"), data_dir=str(game / "com.x.game"))
    assert game / "release.manifest" in pipeline.local_game_files("com.x.game")
    pipeline.delete_local_files("com.x.game")
    assert not game.exists()  # the folder went too: a download manager no longer lists the game

    other = release("Game v2", extra="f;./notes.txt;5\n")  # lists a file that isn't the game's: stays
    library.upsert_game("com.x.game", apk=str(other / "com.x.game.apk"), data_dir=str(other / "com.x.game"))
    assert other / "release.manifest" not in pipeline.local_game_files("com.x.game")


def test_scan_finds_games_in_download_manager_layouts(tmp_path, monkeypatch):
    import zipfile

    from frameport.sources import quest_dump

    monkeypatch.setattr(quest_dump, "_apk_ids", lambda apk: (Path(apk).stem, None))

    def apk(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("AndroidManifest.xml", b"x")
    root = tmp_path / "VR"
    apk(root / "stray.app.apk")  # a loose APK next to game folders: not "one game"
    apk(root / "Manager/Game A v1/com.a.game.apk")
    (root / "Manager/Game A v1/com.a.game").mkdir()
    (root / "Manager/Game A v1/com.a.game/main.obb").write_bytes(b"o")
    apk(root / "Manager/data/downloads/Game B v2/com.b.game.apk")  # 4 levels down
    (root / "PC Game/Binaries").mkdir(parents=True)
    (root / "PC Game/game.exe").write_bytes(b"MZ")
    apk(root / "PC Game/Binaries/never.apk")  # PC program folders aren't searched
    found = {g.apk.name: g for g in quest_dump.scan(root)}
    assert set(found) == {"stray.app.apk", "com.a.game.apk", "com.b.game.apk"}
    assert found["com.a.game.apk"].data_dir == root / "Manager/Game A v1/com.a.game"


def test_user_recipes_get_a_catalog_update_offer(tmp_path, monkeypatch):
    """GitHub #10: a recipe whose Game settings were saved once ("user") never followed a catalog fix again; now
    the game is flagged, and taking the offer re-derives it keeping the user's own FrameBridge settings."""
    from frameport import pipeline
    from frameport.core import library
    from frameport.recommend import catalog

    monkeypatch.setattr(library, "_path", lambda: tmp_path / "library.json")
    entry = catalog.CatalogEntry(package="com.x.vr4", title="VR4", adapter={"vk_shader_fix": "1:2:3:4"})
    monkeypatch.setattr(catalog, "lookup", lambda pkg: entry if pkg == "com.x.vr4" else None)
    an = {"package": "com.x.vr4", "engine": "Unreal", "libs": ["libUE4.so"], "abis": ["arm64-v8a"]}
    recipe = {"package": "com.x.vr4", "patches": {"adapter.scale": {"value": 0.9}}, "source": "user",
              "catalog_rev": "old"}
    (tmp_path / "library.json").write_text(__import__("json").dumps(
        {"games": {"com.x.vr4": {"package": "com.x.vr4", "analysis": an, "recipe": recipe}}, "settings": {}}))
    assert library.game("com.x.vr4")["catalog_update"] == entry.rev()
    pipeline.apply_catalog_update("com.x.vr4")
    g = library.game("com.x.vr4")
    assert "catalog_update" not in g and g["recipe"]["patches"]["adapter.scale"] == {"value": 0.9}
    assert g["recipe"].get("source") != "user" and "adapter.vk_shader_fix" in g["recipe"]["patches"]



def test_oculus_model_checks_accept_any_device_but_the_go():
    import struct

    from frameport.patches.frame import unity_oculus_check as C

    # .text: adrp x1, page(0x1000); add x1, x1, #0x10; orr w2, wzr, #6; bl ...  -> strncmp(model, "Oculus", 6)
    code = struct.pack("<4I", 0xB0000001, 0x91004021, C.ORR_W2_6, 0x94000000)
    other = struct.pack("<4I", 0xB0000001, 0x91008021, C.ORR_W2_6, 0x94000000)  # a different string: untouched
    data = bytearray(0x2000)
    data[0:16], data[16:32] = code, other
    data[0x100F:0x1017] = b"\0Oculus\0"  # the literal at 0x1010
    data[0x1020:0x1028] = b"OculusX\0"
    out, n = C.oculus_model_checks(bytes(data))
    assert n == 1
    assert struct.unpack_from("<I", out, 8)[0] == C.MOV_W2_0 and struct.unpack_from("<I", out, 24)[0] == C.ORR_W2_6
    assert out[0x1010:0x1016] == b"Oculus"  # the string itself stays (Unity's VR device name)


def test_install_builds_a_game_that_was_never_built(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from frameport import pipeline
    from frameport.core import library
    from frameport.core.models import Recipe

    library.upsert_game("com.x.fresh", apk=str(tmp_path / "in.apk"),
                        recipe=library.recipe_to_dict(Recipe("com.x.fresh")))
    out = tmp_path / "built.apk"
    out.write_bytes(b"PK")

    def build(package, reporter, pc=False):
        library.upsert_game(package, build={"apk": str(out)})
    monkeypatch.setattr(pipeline, "build_game", build)
    monkeypatch.setattr(pipeline, "remove_converted_copies", lambda package: 0)
    installed = []
    target = SimpleNamespace(label="frame", install=lambda *a, **k: installed.append(a[2]) or {"ok": True},
                             add_to_library=lambda pkgs, rep: None)
    pipeline.install_game("com.x.fresh", target, None)  # used to fail with KeyError 'apk'
    assert installed == [out]


def test_frame_wait_follows_unitys_frame_begins():
    """Sniper Elite VR deadlocked at a scene switch: the shim's xrWaitFrame waited for a frame Unity never began. The
    begin/end lookups in libunity.so go to the shim too (same-length, in-place renames) and the shim exports
    them."""
    import io

    from elftools.elf.elffile import ELFFile

    from frameport.patches.frame import artifact
    from frameport.patches.frame import unity_oculus_check as C

    for old, new in ((C.UPDATE, C.SHIM_UPDATE), (C.BEGIN, C.SHIM_BEGIN), (C.END, C.SHIM_END)):
        assert len(old) == len(new)
    shim = ELFFile(io.BytesIO(artifact("arm64-v8a", C.SHIM)))
    exported = {s.name for s in shim.get_section_by_name(".dynsym").iter_symbols() if s["st_shndx"] != "SHN_UNDEF"}
    assert {C.SHIM_UPDATE, C.SHIM_BEGIN, C.SHIM_END} <= exported
    log = ("10-06 15:01:12.256  1232  1255 I FrameBridge: ovrp frame loop shim: frame 2175: the last waited frame "
           "wasn't begun within 50 ms, not waiting (2 times)")
    assert "unity-frame-not-begun" in {f.id for f in triage(log, "RUNNING", None).findings}


def test_frame_loop_fixes_are_per_game_settings():
    """The frame-begin gate and the held-back physics update (Sniper Elite VR) are off by default: BattleSisters'
    hands lagged and its loading screen stuttered with them; Sniper's recipe turns them on."""
    from pathlib import Path

    import yaml

    from frameport.patches import settings

    src = Path(settings.__file__).read_text()
    assert '("ovrp_begin_gate", "int", 0,' in src and '("ovrp_hold_physics", "int", 0,' in src
    shim = (Path(__file__).parents[1] / "native/ovrpshim/ovrpshim.c").read_text()
    assert "if (!begin_gate) return 1;" in shim and "if (hold_physics && step == STEP_PHYSICS" in shim
    recipe = yaml.safe_load((Path(__file__).parents[1] / "catalog/games/com.JustAddWater.SniperEliteVR.yaml")
                            .read_text())
    assert recipe["adapter"]["ovrp_begin_gate"] == 1 and recipe["adapter"]["ovrp_hold_physics"] == 1
