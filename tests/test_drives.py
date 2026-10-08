"""Agent: games on another drive (microSD, GitHub #90) and Desktop Mode entries for Linux apps (GitHub #84)."""
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1] / "agent" / "frameport_agent.py"


def load(monkeypatch, tmp_path, extra_mounts=()):
    """The agent with HOME = <tmp>/home, removable drives under <tmp>/media and a fake /proc/mounts."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    spec = importlib.util.spec_from_file_location("frameport_agent", AGENT)
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    media = tmp_path / "media"
    (media / "steamos" / "SD Card").mkdir(parents=True, exist_ok=True)
    (media / "steamos" / "STICK").mkdir(parents=True, exist_ok=True)
    (media / "steamos" / "RO").mkdir(parents=True, exist_ok=True)
    sd = str(media / "steamos" / "SD Card").replace(" ", "\\040")
    table = tmp_path / "mounts"
    table.write_text("/dev/nvme0n1p8 / btrfs rw,relatime 0 0\n"
                     f"/dev/mmcblk0p1 {sd} ext4 rw,nosuid,nodev,relatime 0 0\n"
                     f"/dev/sda1 {media}/steamos/STICK exfat rw,nosuid,nodev 0 0\n"
                     f"/dev/sdb1 {media}/steamos/RO ext4 ro,nosuid 0 0\n"
                     "proc /proc proc rw 0 0\n" + "".join(extra_mounts))
    monkeypatch.setattr(a, "PROC_MOUNTS", str(table))
    monkeypatch.setattr(a, "MEDIA_ROOT", str(media))
    monkeypatch.setattr(a, "container_running", lambda appid: False)
    monkeypatch.setattr(a, "pcvr_pids", lambda base: [])
    monkeypatch.setattr(a, "as_owner", lambda: [])
    return a


def sd_dir(tmp_path):
    return tmp_path / "media" / "steamos" / "SD Card"


def quest_install(a, pkg="com.x.y", dest=None):
    """A Quest game as finalize leaves it (no Lepton needed: written by hand like the agent would)."""
    prep = a.cmd_prepare({"package": pkg, "title": "Game", "dest": dest})
    base = Path(prep["base"])
    (base / "lepton-app" / "obb").mkdir(parents=True)
    (base / "lepton-app" / "game.apk").write_bytes(b"apk" * 100)
    (base / "lepton-app" / "obb" / "main.obb").write_bytes(b"o" * 5000)
    saves = base / "lepton-data" / "external" / "Android" / "data" / pkg / "files"
    saves.mkdir(parents=True)
    (saves / "save.dat").write_text("progress")
    shutil.rmtree(base / "incoming")
    anchor = Path(prep["anchor"])
    anchor.mkdir(parents=True, exist_ok=True)
    (anchor / "artwork").mkdir(exist_ok=True)
    (anchor / "artwork" / "icon.png").write_bytes(b"png")
    a.write_launcher(str(anchor), str(base), pkg, "Game", prep["appid"], "/lepton/lepton", {"X": "1"})
    (anchor / "deployment.json").write_text(json.dumps({"package": pkg, "appid": int(prep["appid"]),
                                                        "base": str(base), "title": "Game"}))
    return prep


def test_drives_lists_internal_and_removable_drives(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    drives = {d["label"]: d for d in a.cmd_drives({})["drives"]}
    assert set(drives) == {"Internal storage", "SD Card", "STICK", "RO"}
    internal = drives["Internal storage"]
    assert internal["internal"] and internal["install_dir"] == a.ANCHORS and internal["usable"]
    assert internal["fstype"] == "btrfs" and internal["free_bytes"] > 0
    sd = drives["SD Card"]
    assert sd["usable"] and sd["removable"] and sd["fstype"] == "ext4"
    assert sd["path"] == str(sd_dir(tmp_path)) and sd["install_dir"] == str(sd_dir(tmp_path) / "FramePort")
    assert not drives["STICK"]["usable"] and "exfat" in drives["STICK"]["reason"]
    assert not drives["RO"]["usable"] and "read-only" in drives["RO"]["reason"]


def test_steam_library_on_another_drive_is_listed(monkeypatch, tmp_path):
    lib = tmp_path / "games"
    (lib / "steamapps").mkdir(parents=True)
    a = load(monkeypatch, tmp_path, [f"/dev/sdc1 {lib} ext4 rw 0 0\n"])
    steamapps = Path(a.STEAM) / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "libraryfolders.vdf").write_text(f'"libraryfolders" {{ "1" {{ "path" "{lib}" }} }}')
    drives = {d["path"]: d for d in a.list_drives()}
    assert drives[str(lib)]["steam_library"] and drives[str(lib)]["install_dir"] == str(lib / "FramePort")


def test_resolve_dest_never_falls_back_to_internal_storage(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    for internal in (None, "", "internal", a.ANCHORS):
        assert a.resolve_dest(internal) == a.ANCHORS
    assert a.resolve_dest(str(sd_dir(tmp_path))) == str(sd_dir(tmp_path) / "FramePort")  # the mount point
    assert (sd_dir(tmp_path) / "FramePort").is_dir()
    with pytest.raises(a.AgentError, match="exfat"):
        a.resolve_dest(str(tmp_path / "media/steamos/STICK/FramePort"))
    with pytest.raises(a.AgentError, match="read-only"):
        a.resolve_dest(str(tmp_path / "media/steamos/RO"))
    with pytest.raises(a.AgentError, match="isn't inserted"):  # an SD card that was taken out
        a.resolve_dest(str(tmp_path / "media/steamos/OLDCARD/FramePort"))
    assert not (tmp_path / "media/steamos/OLDCARD").exists()


def test_new_games_go_to_the_chosen_drive_and_keep_their_base(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    dest = str(sd_dir(tmp_path) / "FramePort")
    prep = quest_install(a, dest=dest)
    assert prep["base"] == os.path.join(dest, "com.x.y") and prep["anchor"].startswith(a.ANCHORS)
    assert prep["free_bytes"] > 0
    # a reinstall keeps the game where it is, whatever the default is now
    again = a.cmd_prepare({"package": "com.x.y", "title": "Game", "dest": None})
    assert again["base"] == prep["base"] and again["installed"]
    games = a.cmd_list_installed({})["games"]
    assert games[0]["drive"] == {"internal": False, "path": str(sd_dir(tmp_path)), "label": "SD Card"}
    assert games[0]["drive_missing"] is False and games[0]["apk_present"]
    assert {d["label"]: d["games"] for d in a.cmd_drives({})["drives"]}["SD Card"] == 1
    # Linux apps and PC VR games take dest too
    p = a.cmd_prepare_linux({"package": "linux.tool", "title": "Tool", "dest": dest})
    assert p["base"] == os.path.join(dest, "linux.tool")


def test_missing_sd_card(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    prep = quest_install(a, dest=str(sd_dir(tmp_path) / "FramePort"))
    (tmp_path / "mounts").write_text("/dev/nvme0n1p8 / btrfs rw 0 0\n")  # card taken out
    game = a.cmd_list_installed({})["games"][0]
    assert game["drive_missing"] is True and game["drive"]["label"] == "SD Card"
    with pytest.raises(a.AgentError, match="isn't inserted"):
        a.cmd_prepare({"package": "com.x.y", "title": "Game"})
    with pytest.raises(a.AgentError, match="isn't inserted"):
        a.cmd_launch({"package": "com.x.y"})
    with pytest.raises(a.AgentError, match="isn't inserted"):
        a.cmd_move({"package": "com.x.y", "dest": "internal", "detach": False})
    assert Path(prep["base"]).exists()


@pytest.mark.parametrize("same_drive", [True, False])
def test_move_quest_game_to_sd_and_back(monkeypatch, tmp_path, same_drive):
    a = load(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "same_device", lambda x, y: same_drive)
    prep = quest_install(a)
    anchor = Path(prep["anchor"])
    assert prep["base"] == str(anchor)  # internal storage: the files live next to the launcher
    os.symlink("/nonexistent/elsewhere", anchor / "lepton-app" / "outside-link")
    dest = str(sd_dir(tmp_path))
    r = a.cmd_move({"package": "com.x.y", "dest": dest, "detach": False})
    assert r["state"] == "done", r
    new = sd_dir(tmp_path) / "FramePort" / "com.x.y"
    assert (new / "lepton-app" / "obb" / "main.obb").stat().st_size == 5000
    assert (new / "lepton-data/external/Android/data/com.x.y/files/save.dat").read_text() == "progress"
    assert os.readlink(new / "lepton-app" / "outside-link") == "/nonexistent/elsewhere"
    # the anchor keeps its own files only
    assert sorted(os.listdir(anchor)) == ["artwork", "deployment.json", "launch.sh"]
    dep = a.deployment("com.x.y")
    assert dep["base"] == str(new) and dep["moved"]["from"] == str(anchor)
    text = (anchor / "launch.sh").read_text()
    assert f"app_dir='{new}'" in text and "export X=1" in text
    assert subprocess.run(["bash", "-n", str(anchor / "launch.sh")]).returncode == 0
    assert a.cmd_move_status({})["state"] == "done"
    with pytest.raises(a.AgentError, match="already there"):
        a.cmd_move({"package": "com.x.y", "dest": dest, "detach": False})
    # and back to internal storage: the drive's empty FramePort folder goes
    r = a.cmd_move({"package": "com.x.y", "dest": "internal", "detach": False})
    assert r["state"] == "done", r
    assert (anchor / "lepton-app" / "game.apk").exists() and not (sd_dir(tmp_path) / "FramePort").exists()
    assert a.deployment("com.x.y")["base"] == str(anchor)
    assert f"app_dir={anchor}\n" in (anchor / "launch.sh").read_text()


def test_move_refusals(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    quest_install(a)
    with pytest.raises(a.AgentError, match="exfat"):
        a.cmd_move({"package": "com.x.y", "dest": str(tmp_path / "media/steamos/STICK"), "detach": False})
    monkeypatch.setattr(a, "container_running", lambda appid: True)
    with pytest.raises(a.AgentError, match="running"):
        a.cmd_move({"package": "com.x.y", "dest": str(sd_dir(tmp_path)), "detach": False})
    monkeypatch.setattr(a, "container_running", lambda appid: False)
    monkeypatch.setattr(a, "same_device", lambda x, y: False)
    monkeypatch.setattr(a, "_space", lambda path: (100 << 20, 1 << 40))  # 100 MiB free < headroom
    with pytest.raises(a.AgentError, match="not enough space"):
        a.cmd_move({"package": "com.x.y", "dest": str(sd_dir(tmp_path)), "detach": False})
    with pytest.raises(a.AgentError, match="not installed"):
        a.cmd_move({"package": "com.not.there", "dest": "internal", "detach": False})
    assert a.deployment("com.x.y")["base"] == os.path.join(a.ANCHORS, "com.x.y")


def test_failed_copy_leaves_the_game_where_it_was(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "same_device", lambda x, y: False)
    prep = quest_install(a)
    real = a.tree_stats
    monkeypatch.setattr(a, "tree_stats", lambda root, names: (real(root, names)[0], 1) if "SD" in root
                        else real(root, names))  # the copy comes up short
    r = a.cmd_move({"package": "com.x.y", "dest": str(sd_dir(tmp_path)), "detach": False})
    assert r["state"] == "failed" and "doesn't match" in r["error"]
    assert a.deployment("com.x.y")["base"] == prep["base"]
    assert (Path(prep["base"]) / "lepton-app" / "game.apk").exists()
    assert not (sd_dir(tmp_path) / "FramePort" / "com.x.y").exists()
    assert f"app_dir={prep['base']}\n" in (Path(prep["anchor"]) / "launch.sh").read_text()


def test_move_detaches_through_systemd_run(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    quest_install(a)
    calls = []
    monkeypatch.setattr(a, "run", lambda cmd, **kw: calls.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, "", "") if cmd[0] == "systemd-run" else
                        subprocess.run(cmd, capture_output=True, text=True))
    r = a.cmd_move({"package": "com.x.y", "dest": str(sd_dir(tmp_path))})
    assert r["started"] and r["to"].endswith("FramePort/com.x.y")
    cmd = next(c for c in calls if c[0] == "systemd-run")
    assert "_move_worker" in cmd and json.loads(cmd[-1])["package"] == "com.x.y"
    assert a.cmd_move_status({})["state"] == "running"


def test_replace_path_keeps_shell_quoting_valid(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    old, new = "/home/steamos/Applications/quest-frame/rift.x", "/run/media/steamos/SD/FramePort/rift.x"
    text = (f"base={old}\ncd {old}/game\nexec proton run {old}/revive/ReviveInjector.exe /openxr "
            f"'Z:\\home\\steamos\\Applications\\quest-frame\\rift.x\\game\\G.exe' >>\"$base/launch.log\"\n"
            f"other={old}2/x\n")
    out = a.replace_path(text, old, new)
    assert f"base={new}\n" in out and f"cd {new}/game" in out and f"{new}/revive/ReviveInjector.exe" in out
    assert "'Z:\\run\\media\\steamos\\SD\\FramePort\\rift.x\\game\\G.exe'" in out
    assert f"other={old}2/x" in out  # another folder that only starts the same
    with pytest.raises(a.AgentError):  # an unquoted path can't become one that needs quotes
        a.replace_path(text, old, "/run/media/steamos/SD Card/FramePort/rift.x")
    spaced = "/run/media/steamos/SD Card/FramePort/rift.x"
    assert a.replace_path(f"base='{spaced}'\n", spaced, old) == f"base='{old}'\n"


def test_symlinks_into_the_old_folder_follow_the_move(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    old, new = tmp_path / "old", tmp_path / "new"
    (new / "game").mkdir(parents=True)
    os.symlink(str(old / "revive" / "LibReviveXR64.dll"), new / "game" / "LibOVRRT64_1.dll")
    os.symlink("../drive_c", new / "game" / "relative")
    assert a.retarget_symlinks(str(new), ["game"], str(old), str(new)) == 1
    assert os.readlink(new / "game" / "LibOVRRT64_1.dll") == str(new / "revive" / "LibReviveXR64.dll")
    assert os.readlink(new / "game" / "relative") == "../drive_c"


def linux_install(a, tmp_path, pkg="linux.true", title="True Tool", tags=None, dest=None, **extra):
    prep = a.cmd_prepare_linux({"package": pkg, "title": title, "dest": dest})
    app_in = os.path.join(prep["incoming"], "app", "bin")
    os.makedirs(app_in)
    shutil.copy("/bin/true", os.path.join(app_in, "true"))
    size = os.path.getsize(os.path.join(app_in, "true"))
    os.makedirs(os.path.join(prep["base"], "incoming-artwork"))
    Path(prep["base"], "incoming-artwork", "icon.png").write_bytes(b"png")
    res = a.cmd_finalize_linux({"package": pkg, "title": title, "exe": "bin/true", "tags": tags or [],
                                "manifests": {"app": {"bin/true": size}}, "env": {"APP_MODE": "1"}, "dest": dest,
                                **extra})
    return prep, res


def test_linux_app_moves_with_its_launcher(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "same_device", lambda x, y: False)
    prep, _ = linux_install(a, tmp_path)
    r = a.cmd_move({"package": "linux.true", "dest": str(sd_dir(tmp_path)), "detach": False})
    assert r["state"] == "done", r
    new = sd_dir(tmp_path) / "FramePort" / "linux.true"
    text = (Path(prep["anchor"]) / "launch.sh").read_text()
    assert f"'{new}/app/bin/true'" in text and "export APP_MODE=1" in text and prep["base"] not in text
    assert subprocess.run(["bash", "-n", str(Path(prep["anchor"]) / "launch.sh")]).returncode == 0
    assert (new / "app" / "bin" / "true").exists() and not (Path(prep["anchor"]) / "app").exists()


def test_pc_vr_game_moves_with_its_prefix_and_launcher(monkeypatch, tmp_path):
    """PC VR: the launcher is written again from the install record (env + game args kept since agent v63), the
    Proton prefix moves with the game and the LibOVRRT redirect follows."""
    from test_agent import fake_steam_tools

    a = load(monkeypatch, tmp_path)
    fake_steam_tools(a, Path(a.HOME))
    monkeypatch.setattr(a, "same_device", lambda x, y: False)
    prep = a.cmd_prepare_pcvr({"package": "rift.game", "title": "Rift Game"})
    inc = Path(prep["incoming"])
    (inc / "game/UEGame.exe").write_bytes(b"MZexe")
    (inc / "revive/ReviveInjector.exe").write_bytes(b"MZ")
    (inc / "revive/LibReviveXR64.dll").write_bytes(b"MZdll")
    a.cmd_finalize_pcvr({"package": "rift.game", "title": "Rift Game", "exe": "UEGame.exe", "libovr_redirect": True,
                         "manifests": {"game": {"UEGame.exe": 5},
                                       "revive": {"ReviveInjector.exe": 2, "LibReviveXR64.dll": 5}},
                         "env": {"FOO": "bar"}, "game_args": ["-vrmode"]})
    base = Path(prep["base"])
    (base / "compatdata/pfx/drive_c").mkdir(parents=True)
    (base / "compatdata/pfx/drive_c/save.sav").write_text("saved")
    assert os.path.islink(base / "game/LibOVRRT64_1.dll")
    r = a.cmd_move({"package": "rift.game", "dest": str(sd_dir(tmp_path)), "detach": False})
    assert r["state"] == "done", r
    new = sd_dir(tmp_path) / "FramePort" / "rift.game"
    assert (new / "compatdata/pfx/drive_c/save.sav").read_text() == "saved"
    assert os.readlink(new / "game/LibOVRRT64_1.dll") == str(new / "revive/LibReviveXR64.dll")
    text = (Path(prep["anchor"]) / "launch.sh").read_text()
    assert f"{base}/game" not in text and f"{base}/revive" not in text  # (the play log stays in the anchor)
    assert "export FOO=bar" in text and "-vrmode" in text
    assert f"base='{new}'" in text
    assert subprocess.run(["bash", "-n", str(Path(prep["anchor"]) / "launch.sh")]).returncode == 0


def test_cleanup_and_purge_on_a_drive(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "stop_steam", lambda: True)
    monkeypatch.setattr(a, "start_steam", lambda s: None)
    fp = sd_dir(tmp_path) / "FramePort"
    prep = quest_install(a, dest=str(fp))
    (fp / "com.old.game").mkdir()  # left by an earlier install
    (fp / "com.old.game" / "x").write_bytes(b"1234")
    assert a.cmd_cleanup({"paths": [str(fp / "com.old.game")]})["freed_bytes"] == 4
    for bad in (prep["base"], str(fp), str(fp / "com.x.y" / "lepton-data"), str(sd_dir(tmp_path))):
        with pytest.raises(a.AgentError):
            a.cmd_cleanup({"paths": [bad]})
    a.purge_worker(json.dumps({"keep_saves": False, "status": str(tmp_path / "st.json")}))
    assert json.loads((tmp_path / "st.json").read_text())["state"] == "done"
    assert not fp.exists() and sd_dir(tmp_path).exists()


# ------------------------------------------------------------------------------------------ Desktop Mode (#84)
def test_linux_app_gets_a_desktop_mode_entry(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    (Path(a.HOME) / "Desktop").mkdir()
    prep, res = linux_install(a, tmp_path, title='Odd "Name" 100%', tags=["Linux", "Puzzle"])
    menu = Path(a.HOME) / ".local/share/applications/frameport-true.desktop"
    desk = Path(a.HOME) / "Desktop/frameport-true.desktop"
    assert res["desktop_entry"]["menu"] == str(menu) and res["desktop_entry"]["desktop"] == str(desk)
    text = menu.read_text()
    assert text == desk.read_text() and os.access(desk, os.X_OK)
    lines = dict(line.split("=", 1) for line in text.splitlines()[1:])
    assert lines["Name"] == 'Odd "Name" 100%' and lines["Categories"] == "Game;"
    assert lines["Exec"] == f'env FRAMEPORT_DESKTOP=1 "{prep["anchor"]}/launch.sh"'
    assert lines["Icon"] == f"{prep['anchor']}/artwork/icon.png" and lines["X-FramePort-Package"] == "linux.true"
    assert lines["Terminal"] == "false"
    # nothing changes when it is refreshed again; an app without game tags is a utility
    assert a.refresh_desktop_entries() == []
    linux_install(a, tmp_path, pkg="linux.editor", title="Editor")
    assert "Categories=Utility;" in (Path(a.HOME) / ".local/share/applications/frameport-editor.desktop").read_text()
    # uninstall removes both files
    a.cmd_uninstall({"package": "linux.true", "keep_data": False})
    assert not menu.exists() and not desk.exists()


def test_exec_quoting():
    spec = importlib.util.spec_from_file_location("frameport_agent", AGENT)
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    assert a.desktop_exec_arg('/a b/$x`"\\%') == '"/a b/\\\\$x\\\\`\\\\"\\\\\\\\%%"'
    assert a.desktop_value("two\nlines\\") == "two lines\\\\"
    assert a.desktop_file_name("linux.My App!") == "frameport-My-App.desktop"


def test_desktop_entry_switch_and_refresh(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    linux_install(a, tmp_path, desktop_entry=False)
    menu = Path(a.HOME) / ".local/share/applications/frameport-true.desktop"
    assert not menu.exists() and a.deployment("linux.true")["desktop_entry"] is False
    assert a.refresh_desktop_entries() == []  # off stays off
    r = a.cmd_desktop_entry({"package": "linux.true", "enabled": True})
    assert r["enabled"] and menu.exists()
    a.cmd_desktop_entry({"package": "linux.true", "enabled": False})
    assert not menu.exists()
    # an install from before agent v63 (no desktop_entry field) gets one with the host fixes; stale ones go
    dep_path = Path(a.ANCHORS) / "linux.true" / "deployment.json"
    raw = json.loads(dep_path.read_text())
    raw.pop("desktop_entry")
    dep_path.write_text(json.dumps(raw))
    stale = menu.parent / "frameport-gone.desktop"
    stale.write_text("[Desktop Entry]\nX-FramePort-Package=linux.gone\n")
    (menu.parent / "frameport-links.desktop").write_text("[Desktop Entry]\nName=not an app entry\n")
    assert "Desktop Mode entries (2)" in a.ensure_host_fixes()
    assert menu.exists() and not stale.exists() and (menu.parent / "frameport-links.desktop").exists()
    with pytest.raises(a.AgentError):
        a.cmd_desktop_entry({"package": "com.not.linux", "enabled": True})


def test_linux_launcher_from_desktop_mode(monkeypatch, tmp_path):
    """Started from Desktop Mode (FRAMEPORT_DESKTOP=1) the launcher's parent exits at once: the app must keep running,
    and the desktop's display is used instead of Steam's."""
    a = load(monkeypatch, tmp_path)
    prep, _ = linux_install(a, tmp_path)
    text = (Path(prep["anchor"]) / "launch.sh").read_text()
    assert '[[ -n "${FRAMEPORT_DESKTOP:-}" ]] && parent=1' in text
    assert 'if [[ -z "${FRAMEPORT_DESKTOP:-}" && -z "${DISPLAY:-}" ]]; then' in text
    # older launchers are upgraded in place
    old = text.replace('[[ -n "${FRAMEPORT_DESKTOP:-}" ]] && parent=1\n', "").replace(
        'if [[ -z "${FRAMEPORT_DESKTOP:-}" && -z "${DISPLAY:-}" ]]; then', 'if [[ -z "${DISPLAY:-}" ]]; then')
    old = old.replace("# Started from Desktop Mode's menu (FRAMEPORT_DESKTOP=1, GitHub #84) the desktop's own session "
                      "is used.\n", "")
    assert "FRAMEPORT_DESKTOP" not in old
    (Path(prep["anchor"]) / "launch.sh").write_text(old)
    assert "linux.true" in a.upgrade_launchers()
    new = (Path(prep["anchor"]) / "launch.sh").read_text()
    assert '[[ -n "${FRAMEPORT_DESKTOP:-}" ]] && parent=1' in new and "FRAMEPORT_DESKTOP:-}\" && -z" in new
    assert subprocess.run(["bash", "-n", str(Path(prep["anchor"]) / "launch.sh")]).returncode == 0
    # the watchdog: a parent that exits ends the app, unless started from the desktop
    script = tmp_path / "app.sh"
    script.write_text("#!/bin/sh\nsleep 6\necho finished >" + str(tmp_path / "out") + "\n")
    script.chmod(0o755)
    program = os.path.join(prep["base"], "app", "bin", "true")
    assert program in new
    body = new.replace(program, str(script))
    launcher = tmp_path / "l.sh"
    launcher.write_text(body)
    launcher.chmod(0o755)
    env = dict(os.environ, FRAMEPORT_DESKTOP="1", DISPLAY=":9")
    subprocess.run(["bash", "-c", f"{launcher} & exit 0"], env=env, timeout=20)
    import time

    deadline = time.time() + 15
    while time.time() < deadline and not (tmp_path / "out").exists():
        time.sleep(0.5)
    assert (tmp_path / "out").read_text().strip() == "finished"


# ------------------------------------------------------------------------------------------ Linux apps' icons (#99)
def png(width: int) -> bytes:
    """Enough of a PNG for the agent (signature + IHDR size)."""
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") * 2 + b"\x08\x06\x00\x00\x00" + b"x" * 40


SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"></svg>\n'


def appimage_install(a, tmp_path, tree):
    """An "AppImage" whose --appimage-extract copies the folder `tree` (symlinks kept) as squashfs-root."""
    prep = a.cmd_prepare_linux({"package": "linux.tool", "title": "Tool"})
    exe = Path(prep["incoming"]) / "app" / "Tool-aarch64.AppImage"
    exe.write_text(f"#!/bin/sh\ncp -a '{tree}' squashfs-root\nprintf '#!/bin/sh\\n' > squashfs-root/AppRun\n"
                   "chmod +x squashfs-root/AppRun\n")
    os.makedirs(os.path.join(prep["base"], "incoming-artwork"))
    Path(prep["base"], "incoming-artwork", "icon.png").write_bytes(b"placeholder")
    res = a.cmd_finalize_linux({"package": "linux.tool", "title": "Tool", "exe": exe.name, "appimage": True,
                                "manifests": {"app": {exe.name: exe.stat().st_size}}})
    return prep, res


def entry_lines(a):
    text = (Path(a.HOME) / ".local/share/applications/frameport-tool.desktop").read_text()
    return dict(line.split("=", 1) for line in text.splitlines()[1:])


def test_appimage_icon_from_its_diricon_symlink(monkeypatch, tmp_path):
    import base64

    a = load(monkeypatch, tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "tool.desktop").write_text("[Desktop Entry]\nName=Tool\nIcon=tool\nStartupWMClass=ToolWindow\n")
    (tree / "tool.png").write_bytes(png(256))
    (tree / ".DirIcon").symlink_to("tool.png")
    prep, res = appimage_install(a, tmp_path, tree)
    anchor = Path(prep["anchor"])
    icon = anchor / "artwork" / "app-icon.png"
    assert icon.read_bytes() == png(256) and not icon.is_symlink()
    lines = entry_lines(a)
    assert lines["Icon"] == str(icon) and lines["StartupWMClass"] == "ToolWindow"
    assert base64.b64decode(res["app_icon"]["png"]) == png(256)
    assert a.shortcut_args("linux.tool")[3] == str(icon)  # the Steam shortcut too
    # the user's own icon (PC: "custom") wins over the app's
    (anchor / "artwork" / ".icon-source").write_text("custom")
    a.refresh_desktop_entries()
    assert entry_lines(a)["Icon"] == str(anchor / "artwork" / "icon.png")
    assert a.shortcut_args("linux.tool")[3] == str(anchor / "artwork" / "icon.png")
    # an art update replaces the artwork folder: the app's icon comes back
    (anchor / "artwork" / ".icon-source").write_text("generated")
    icon.unlink()
    a.refresh_desktop_entries()
    assert icon.exists() and entry_lines(a)["Icon"] == str(icon)


def test_appimage_icon_from_hicolor_and_svg(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    tree = tmp_path / "tree"
    (tree / "usr/share/icons/hicolor/48x48/apps").mkdir(parents=True)
    (tree / "usr/share/icons/hicolor/scalable/apps").mkdir(parents=True)
    (tree / "org.tool.App.desktop").write_text("[Desktop Entry]\nName=Tool\nIcon=org.tool.App\n")
    (tree / "usr/share/icons/hicolor/scalable/apps/org.tool.App.svg").write_bytes(SVG)
    (tree / "usr/share/icons/hicolor/48x48/apps/org.tool.App.png").write_bytes(b"not a png")
    prep, res = appimage_install(a, tmp_path, tree)
    icon = Path(prep["anchor"]) / "artwork" / "app-icon.svg"
    assert icon.exists() and entry_lines(a)["Icon"] == str(icon) and "StartupWMClass" not in entry_lines(a)
    assert res["app_icon"]["file"] == "app-icon.svg" and "png" not in res["app_icon"]  # the PC can't draw SVG
    # Steam takes PNGs only: the shortcut keeps the art set's icon
    assert a.shortcut_args("linux.tool")[3] == str(Path(prep["anchor"]) / "artwork" / "icon.png")


def test_app_icon_prefers_big_pngs_and_stays_inside_the_app(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    root = tmp_path / "root"
    for size in (32, 512):
        d = root / "usr/share/icons/hicolor" / f"{size}x{size}" / "apps"
        d.mkdir(parents=True)
        (d / "tool.png").write_bytes(png(size))
    (root / "usr/share/icons/hicolor/scalable/apps").mkdir(parents=True)
    (root / "usr/share/icons/hicolor/scalable/apps/tool.svg").write_bytes(SVG)
    (root / "tool.desktop").write_text("[Desktop Entry]\nIcon=tool\n[Desktop Action x]\nIcon=other\n")
    assert a.find_app_icon(str(root), True)["icon"].endswith("512x512/apps/tool.png")
    (root / "usr/share/icons/hicolor/512x512/apps/tool.png").unlink()
    assert a.find_app_icon(str(root), True)["icon"].endswith("scalable/apps/tool.svg")  # SVG beats a 32 px PNG
    # a symlink out of the app (or an absolute / ../ Icon=) is never followed
    secret = tmp_path / "secret.png"
    secret.write_bytes(png(1024))
    for p in list(root.rglob("tool.*")):
        if p.suffix != ".desktop":
            p.unlink()
    (root / ".DirIcon").symlink_to(secret)
    (root / "usr/share/pixmaps").mkdir(parents=True)
    (root / "usr/share/pixmaps/tool.png").symlink_to(secret)
    assert a.find_app_icon(str(root), True)["icon"] is None
    for name in (str(secret), "../secret.png"):
        (root / "tool.desktop").write_text(f"[Desktop Entry]\nIcon={name}\n")
        assert a.find_app_icon(str(root), True)["icon"] is None


def test_folder_app_icon_next_to_its_desktop_file(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    prep = a.cmd_prepare_linux({"package": "linux.tool", "title": "Tool"})
    app = Path(prep["incoming"]) / "app"
    (app / "Tool/share/pixmaps").mkdir(parents=True)
    (app / "Tool/share/applications").mkdir(parents=True)
    shutil.copy("/bin/true", app / "Tool" / "tool")
    (app / "Tool/share/applications/tool.desktop").write_text("[Desktop Entry]\nIcon=tool.png\n")
    (app / "Tool/share/pixmaps/tool.png").write_bytes(png(64))
    manifest = {rel: (app / rel).stat().st_size for rel in
                ("Tool/tool", "Tool/share/applications/tool.desktop", "Tool/share/pixmaps/tool.png")}
    a.cmd_finalize_linux({"package": "linux.tool", "title": "Tool", "exe": "Tool/tool", "manifests": {"app": manifest}})
    icon = Path(prep["anchor"]) / "artwork" / "app-icon.png"
    assert icon.read_bytes() == png(64) and entry_lines(a)["Icon"] == str(icon)
    # a new version without an icon: the old copy goes
    (Path(prep["base"]) / "app/Tool/share/pixmaps/tool.png").unlink()
    a.ensure_app_icon(a.deployment("linux.tool"), prep["anchor"], refresh=True)
    assert not icon.exists()
