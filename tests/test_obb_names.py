"""Expansion files with a changed name go to the Frame under the name the game looks for (GitHub #450/#481)."""
import contextlib
from types import SimpleNamespace

from frameport import pipeline
from frameport.core.events import Reporter
from frameport.core.models import Recipe
from frameport.game_update import plan_update
from frameport.install import installer
from frameport.sources import quest_dump

VR4 = "com.Armature.VR4"


def names(pkg, rels):
    return {o.name: (o.wanted, o.rename, o.reason) for o in quest_dump.obb_names(pkg, rels)}


def test_copy_suffixes_are_renamed():
    """#450: a download's -003/-001 suffixes; also Windows' " (1)" and " - Copy"."""
    assert names(VR4, ["main.203.com.Armature.VR4-003.obb", "patch.203.com.Armature.VR4-001.obb"]) == {
        "main.203.com.Armature.VR4-003.obb": ("main.203.com.Armature.VR4.obb", True, "copy"),
        "patch.203.com.Armature.VR4-001.obb": ("patch.203.com.Armature.VR4.obb", True, "copy")}
    assert names("com.x.y", ["main.5.com.x.y (1).obb"])["main.5.com.x.y (1).obb"][:2] == ("main.5.com.x.y.obb", True)
    assert names("com.x.y", ["main.5.com.x.y - Copy.obb"])["main.5.com.x.y - Copy.obb"][:2] == (
        "main.5.com.x.y.obb", True)


def test_case_differences_are_renamed_and_the_version_is_kept():
    assert names(VR4, ["Main.203.com.armature.vr4.obb"]) == {
        "Main.203.com.armature.vr4.obb": ("main.203.com.Armature.VR4.obb", True, "case")}
    # an older versionCode is normal (expansion files keep theirs): never changed
    assert names(VR4, ["main.200.com.Armature.VR4-1.obb"])["main.200.com.Armature.VR4-1.obb"][0] == \
        "main.200.com.Armature.VR4.obb"


def test_the_games_own_names_are_left_alone():
    """Exact names, Unreal chunk files (#471), split parts (Phantom), other data and subfolders."""
    assert names("com.Sanzaru.Wrath2", ["main.5513.com.Sanzaru.Wrath2.obb", "patch.0.com.Sanzaru.Wrath2.obb",
                                        "patch.pakchunk27_s1optional-Android_ASTC.com.Sanzaru.Wrath2.obb"]) == {}
    assert names("com.nDreams.PhantomQuest", [f"main.com.nDreams.PhantomQuest.part{i}.obb" for i in range(6)]) == {}
    assert names("com.crytek.climb2", ["main.com.crytek.climb2.obb", "Audio_en-US.obb", "AAP.obb",
                                       "chunk.1.com.crytek.climb2.obb", "sub/main.1.com.crytek.CLIMB2.obb"]) == {}


def test_unclear_cases_stay_warnings():
    # #129: a renamed build's OBB is another app's file: never renamed
    assert names("com.beatgames.beatsaber", ["main.1716.com.beatgames.beatsaber.lj369vr.obb"]) == {
        "main.1716.com.beatgames.beatsaber.lj369vr.obb": ("main.1716.com.beatgames.beatsaber.obb", False,
                                                          "another-app")}
    # the right name is there too: the copy is left as it is
    assert names(VR4, ["main.203.com.Armature.VR4.obb", "main.203.com.Armature.VR4-003.obb"]) == {
        "main.203.com.Armature.VR4-003.obb": ("main.203.com.Armature.VR4.obb", False, "duplicate")}
    # two candidates for one name
    got = names(VR4, ["main.203.com.Armature.VR4-1.obb", "main.203.com.Armature.VR4 (2).obb"])
    assert {v[1:] for v in got.values()} == {(False, "ambiguous")} and len(got) == 2
    # no versionCode: the number at the end may be the versionCode
    assert names(VR4, ["main.com.Armature.VR4-203.obb"])["main.com.Armature.VR4-203.obb"][1:] == (
        False, "unclear-version")


CB2 = "com.sumalab.CrisisBrigade2reloaded"
CB2_KINDS = ("office", "alleys", "challenges", "mansion", "ta1", "ta2", "ta3")


def test_other_kinds_with_the_package_in_another_case_are_renamed():
    """#415: Crisis Brigade 2's seven <kind>.222.<package>.obb files, the package part all lower case."""
    lower = [f"{k}.222.{CB2.lower()}.obb" for k in CB2_KINDS]
    assert names(CB2, lower) == {n: (f"{k}.222.{CB2}.obb", True, "case")
                                 for n, k in zip(lower, CB2_KINDS, strict=True)}
    assert names(CB2, [f"{k}.222.{CB2}.obb" for k in CB2_KINDS]) == {}  # the right names
    # the kind keeps its own spelling; a copy suffix goes as for main/patch
    assert names(CB2, ["Office.222.com.sumalab.crisisbrigade2reloaded-002.obb",
                       "ta1.222.com.sumalab.CrisisBrigade2reloaded (1).obb"]) == {
        "Office.222.com.sumalab.crisisbrigade2reloaded-002.obb": (f"Office.222.{CB2}.obb", True, "copy"),
        "ta1.222.com.sumalab.CrisisBrigade2reloaded (1).obb": (f"ta1.222.{CB2}.obb", True, "copy")}
    # the right name next to a copy: the copy stays as it is
    assert names(CB2, [f"office.222.{CB2}.obb", f"office.222.{CB2} - Copy.obb"])[f"office.222.{CB2} - Copy.obb"][1:] \
        == (False, "duplicate")


def test_other_kinds_of_another_package_are_not_warned_about():
    """Unlike main/patch, any other <word>.<n>.<x>.obb may be unrelated data: no "another app" warning."""
    assert names(CB2, ["office.222.com.sumalab.other.obb", "office.222.com.sumalab.CrisisBrigade2reloaded.extra.obb",
                       "data.1.something.obb"]) == {}
    entry = {"package": CB2, "data_manifest": {"office.222.com.y.other.obb": 1}}
    assert pipeline.misnamed_obbs(entry) == [] and pipeline.renamed_obbs(entry) == []


def test_other_kinds_go_to_the_frame_under_the_games_names():
    manifest = {f"{k}.222.{CB2.lower()}.obb": i for i, k in enumerate(CB2_KINDS, 1)}
    manifest[f"main.222.{CB2}.obb"] = 99
    assert quest_dump.frame_manifest(CB2, manifest) == {
        **{f"{k}.222.{CB2}.obb": i for i, k in enumerate(CB2_KINDS, 1)}, f"main.222.{CB2}.obb": 99}
    assert set(quest_dump.frame_names(CB2, manifest)) == {f"{k}.222.{CB2.lower()}.obb" for k in CB2_KINDS}


def test_lower_case_other_kinds_next_to_the_apk_are_found(tmp_path):
    (tmp_path / "game.apk").write_bytes(b"")
    files = [f"{k}.222.{CB2.lower()}.obb" for k in CB2_KINDS] + [f"office.222.{CB2.lower()}-001.obb"]
    for n in files:
        (tmp_path / n).write_bytes(b"x")
    data, got = quest_dump.find_data(tmp_path, CB2, 222)
    # the copy loses the tie against the plain lower-case name of its kind; everything else is sent
    assert data == tmp_path and sorted(got) == sorted(files[:-1])


def test_game_page_lists_renamed_files_as_info_and_the_rest_as_warnings():
    entry = {"package": VR4, "data_manifest": {"main.203.com.Armature.VR4-003.obb": 1,
                                               "patch.203.com.Armature.VR4-001.obb": 1}}
    assert [(o.name, o.wanted) for o in pipeline.renamed_obbs(entry)] == [
        ("main.203.com.Armature.VR4-003.obb", "main.203.com.Armature.VR4.obb"),
        ("patch.203.com.Armature.VR4-001.obb", "patch.203.com.Armature.VR4.obb")]
    assert pipeline.misnamed_obbs(entry) == []
    chunks = {"package": "com.Sanzaru.Wrath2", "data_manifest": {
        "patch.pakchunk27-Android_ASTC.com.Sanzaru.Wrath2.obb": 1, "main.5513.com.Sanzaru.Wrath2.obb": 1}}
    assert pipeline.misnamed_obbs(chunks) == [] and pipeline.renamed_obbs(chunks) == []


def test_loose_copy_named_obbs_next_to_the_apk_are_found(tmp_path, monkeypatch):
    """A folder with the APK and a download's -003 OBBs: found by name, the right-named one wins a tie."""
    (tmp_path / "game.apk").write_bytes(b"")
    for n in ("main.203.com.Armature.VR4-003.obb", "patch.203.com.Armature.VR4-001.obb", "notes.txt"):
        (tmp_path / n).write_bytes(b"x")
    data, files = quest_dump.find_data(tmp_path, VR4, 203)
    assert data == tmp_path and files == ["main.203.com.Armature.VR4-003.obb", "patch.203.com.Armature.VR4-001.obb"]
    (tmp_path / "main.203.com.Armature.VR4.obb").write_bytes(b"x")
    assert quest_dump.find_data(tmp_path, VR4, 203)[1] == [
        "main.203.com.Armature.VR4.obb", "patch.203.com.Armature.VR4-001.obb"]


def test_plan_update_compares_the_frames_names():
    """No endless "Update on Frame": the deployment records the renamed file under its Frame name."""
    local = {"main.203.com.Armature.VR4-003.obb": 7}
    game = {"package": VR4, "data_manifest": local}
    assert not plan_update(game, {"data_manifest": {"main.203.com.Armature.VR4.obb": 7}}).data
    assert plan_update(game, {"data_manifest": {"main.203.com.Armature.VR4-003.obb": 7}}).data  # installed before
    import hashlib
    import json
    fp = hashlib.sha256(json.dumps({"main.203.com.Armature.VR4.obb": 7}, sort_keys=True).encode()).hexdigest()[:32]
    assert not plan_update(game, {"data_fp": fp}).data  # list_installed without details: the fingerprint


class FakeFrame:
    def __init__(self, existing):
        self.existing, self.calls, self.renamed = existing, {}, []
        self.target = SimpleNamespace(host="10.0.0.5")
        self.sftp = SimpleNamespace(rename=lambda a, b: self.renamed.append((a, b)))

    def agent(self, command, **args):
        self.calls[command] = args
        if command == "prepare":
            return {"base": "/g", "incoming": "/g/incoming", "existing_obb": self.existing, "same_apk": True,
                    "free_bytes": 1 << 40, "lepton": "/l", "installed": True}
        return {"base": "/g", "appid": 1}


def _install(tmp_path, monkeypatch, existing):
    data = tmp_path / "Resident Evil 4" / VR4
    data.mkdir(parents=True)
    (data / "main.203.com.Armature.VR4-003.obb").write_bytes(b"m" * 5)
    (data / "patch.203.com.Armature.VR4-001.obb").write_bytes(b"p" * 3)
    apk = tmp_path / "game.apk"
    apk.write_bytes(b"apk")
    sent = []
    monkeypatch.setattr(installer, "upload_files", lambda xfer, items, *a, **k: sent.extend(items))
    monkeypatch.setattr(installer, "transfer_link", lambda frame, reporter, need: contextlib.nullcontext(frame))
    monkeypatch.setattr(installer, "_tags", lambda pkg: [])
    frame = FakeFrame(existing)
    installer.install(frame, installer.InstallPlan(VR4, "VR4", apk, data, Recipe(VR4)), Reporter())
    return frame, sent, data


def test_install_sends_renamed_files_under_the_games_names(tmp_path, monkeypatch):
    frame, sent, data = _install(tmp_path, monkeypatch, {})
    assert sorted((p.name, rel) for p, rel, _ in sent) == [
        ("main.203.com.Armature.VR4-003.obb", "obb/main.203.com.Armature.VR4.obb"),
        ("patch.203.com.Armature.VR4-001.obb", "obb/patch.203.com.Armature.VR4.obb")]
    assert frame.calls["finalize"]["obb_manifest"] == {"main.203.com.Armature.VR4.obb": 5,
                                                       "patch.203.com.Armature.VR4.obb": 3}
    assert sorted(p.name for p in data.iterdir()) == ["main.203.com.Armature.VR4-003.obb",
                                                       "patch.203.com.Armature.VR4-001.obb"]  # PC files untouched


def test_install_sends_other_kinds_under_the_games_names(tmp_path, monkeypatch):
    """#415: lower-case Crisis Brigade 2 files go to the Frame under the game's spelling; the PC copies keep theirs."""
    data = tmp_path / "CB2"
    data.mkdir()
    for k in CB2_KINDS:
        (data / f"{k}.222.{CB2.lower()}.obb").write_bytes(b"o" * 2)
    apk = tmp_path / "game.apk"
    apk.write_bytes(b"apk")
    sent = []
    monkeypatch.setattr(installer, "upload_files", lambda xfer, items, *a, **k: sent.extend(items))
    monkeypatch.setattr(installer, "transfer_link", lambda frame, reporter, need: contextlib.nullcontext(frame))
    monkeypatch.setattr(installer, "_tags", lambda pkg: [])
    frame = FakeFrame({})
    installer.install(frame, installer.InstallPlan(CB2, "CB2", apk, data, Recipe(CB2)), Reporter())
    assert sorted(rel for _, rel, _ in sent) == sorted(f"obb/{k}.222.{CB2}.obb" for k in CB2_KINDS)
    assert frame.calls["finalize"]["obb_manifest"] == {f"{k}.222.{CB2}.obb": 2 for k in CB2_KINDS}
    assert sorted(p.name for p in data.iterdir()) == sorted(f"{k}.222.{CB2.lower()}.obb" for k in CB2_KINDS)


def test_install_renames_an_earlier_copy_on_the_frame_instead_of_sending_it(tmp_path, monkeypatch):
    """Installed before this fix under the old name: moved into place on the Frame, nothing uploaded twice."""
    frame, sent, _ = _install(tmp_path, monkeypatch, {"main.203.com.Armature.VR4-003.obb": 5,
                                                      "patch.203.com.Armature.VR4.obb": 3})
    assert sent == [] and frame.renamed == [("/g/lepton-app/obb/main.203.com.Armature.VR4-003.obb",
                                             "/g/incoming/obb/main.203.com.Armature.VR4.obb")]


def test_relink_on_the_frame_uses_the_pcs_name(tmp_path, monkeypatch):
    """#433: an earlier copy of a renamed file becomes a link to the original under the Frame's name."""
    import os

    src = tmp_path / "src"
    src.mkdir()
    (src / "main.1.com.x-003.obb").write_bytes(b"x" * 4)
    root = tmp_path / "obb"
    root.mkdir()
    (root / "main.1.com.x.obb").write_bytes(b"x" * 4)
    monkeypatch.setattr("frameport.frame.local.on_frame", lambda: True)
    frame = SimpleNamespace(target=SimpleNamespace(host="127.0.0.1"))
    assert installer._relink_installed(frame, src, {"main.1.com.x.obb": 4}, str(root), Reporter(),
                                       {"main.1.com.x.obb": "main.1.com.x-003.obb"}) == 4
    assert os.path.samefile(root / "main.1.com.x.obb", src / "main.1.com.x-003.obb")


def test_numbered_patch_files_are_the_games_own():
    """#159: Medal of Honor's patch.<n>.<versionCode>.<package>.obb files aren't another app's; case is fixed."""
    pkg = "com.Virtuos.R6DLM"
    assert names(pkg, ["main.12297.com.Virtuos.R6DLM.obb", "patch.0.12297.com.Virtuos.R6DLM.obb",
                       "patch.20.12297.com.Virtuos.R6DLM.obb"]) == {}
    assert names(pkg, ["patch.3.12297.com.virtuos.r6dlm.obb"]) == {
        "patch.3.12297.com.virtuos.r6dlm.obb": ("patch.3.12297.com.Virtuos.R6DLM.obb", True, "case")}
