"""The Frame-side agent (stdlib only) — pieces that don't need a Frame."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

AGENT = Path(__file__).resolve().parents[1] / "agent" / "frameport_agent.py"


def load_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("frameport_agent", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_vdf_roundtrip_and_upsert(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    vdf = tmp_path / "shortcuts.vdf"
    exe = '"/home/steamos/Applications/quest-frame/com.x.y/launch.sh"'
    appid = a.upsert_shortcut(str(vdf), exe, "My Game", "/start", "/icon.png")
    assert appid == a.shortcut_appid(exe, "My Game") and appid & 0x80000000
    root = a.vdf_decode(vdf.read_bytes())
    entry = root["shortcuts"]["0"]
    assert entry["appname"] == "My Game" and entry["Exe"] == exe and entry["OpenVR"] == 1
    # upsert keeps the id and doesn't duplicate
    assert a.upsert_shortcut(str(vdf), exe, "Renamed", "/start") == appid
    assert len(a.vdf_decode(vdf.read_bytes())["shortcuts"]) == 1
    assert a.vdf_encode(a.vdf_decode(vdf.read_bytes())) == vdf.read_bytes()
    assert a.remove_shortcut(str(vdf), exe)
    assert a.vdf_decode(vdf.read_bytes())["shortcuts"] == {}


def test_launcher_template(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    a.write_launcher(str(anchor), "/data/game dir/pkg", "com.x.y", "Game's Title", 123, "/lepton/lepton",
                     {"VK_INSTANCE_LAYERS": "", "bad-name": "x"})
    text = (anchor / "launch.sh").read_text()
    assert "export SteamAppId=123" in text and "app_dir='/data/game dir/pkg'" in text
    assert "export VK_INSTANCE_LAYERS=''" in text and "bad-name" not in text
    assert subprocess.run(["bash", "-n", str(anchor / "launch.sh")]).returncode == 0


def test_launcher_repairs_save_folder_permissions(monkeypatch, tmp_path):
    """SUPERHOT creates its cloud save folder with mode 1700 and quits when it can't write there (Lepton's app
    writes through the folder's group): the launcher gives every storage folder owner and group write permission."""
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    a.write_launcher(str(anchor), str(tmp_path / "game"), "com.x.y", "T", 1, "/lepton/lepton", {})
    fix = next(line for line in (anchor / "launch.sh").read_text().splitlines() if line.startswith("fix_perms()"))
    saves = tmp_path / "game" / "lepton-data" / "external" / "Android" / "data" / "com.x.y" / "files" / "cloud"
    saves.mkdir(parents=True)
    os.chmod(saves, 0o1700)
    if os.stat(saves).st_mode & 0o777 != 0o700:
        pytest.skip("filesystem doesn't keep modes")
    subprocess.run(["bash", "-c", f"app_dir={tmp_path / 'game'}\n{fix}\nfix_perms"], check=True)
    assert os.stat(saves).st_mode & 0o770 == 0o770


def test_cli_protocol(tmp_path):
    p = subprocess.run([sys.executable, str(AGENT), "nope"], capture_output=True, text=True)
    assert p.returncode == 2 and json.loads(p.stdout)["ok"] is False
    p = subprocess.run([sys.executable, str(AGENT), "set_settings"], input=json.dumps({"package": "bad name"}),
                       capture_output=True, text=True, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert json.loads(p.stdout) == {"ok": False, "error": "bad package name 'bad name'"}


def test_host_fix_keyring(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    assert a.ensure_host_fixes() == ["podman keyring=false"]
    text = (tmp_path / ".config/containers/containers.conf").read_text()
    assert "[containers]" in text and "keyring = false" in text
    assert a.ensure_host_fixes() == []  # idempotent
    # an existing [containers] section gets the key inserted, not a second section
    conf = tmp_path / ".config/containers/containers.conf"
    conf.write_text("[engine]\nfoo = 1\n[containers]\nlog_size_max = 10\n")
    assert a.ensure_host_fixes() == ["podman keyring=false"]
    assert conf.read_text().count("[containers]") == 1 and "keyring = false" in conf.read_text()


def test_cleanup_refuses_outside_paths(monkeypatch, tmp_path):
    import pytest

    a = load_agent(monkeypatch, tmp_path)
    old = tmp_path / "PATCHED"
    (old / "x").mkdir(parents=True)
    (old / "x" / "f.apk").write_bytes(b"1234")
    r = a.cmd_cleanup({"paths": ["~/PATCHED"]})
    assert r["freed_bytes"] == 4 and not old.exists()
    for bad in ("/etc", "~/Applications/quest-frame", "~/Applications/quest-frame/x", "~/..", "~/.ssh", "~/.steam",
                "~/.local/share", "~"):
        with pytest.raises(a.AgentError):
            a.cmd_cleanup({"paths": [bad]})


# ------------------------------------------------------------------------------------------ PC VR under Proton
def fake_steam_tools(a, tmp_path, experimental=False):
    """A Steam library with an ARM64 Proton (needing a runtime) installed, like the Frame's; experimental=True also
    installs Proton Experimental (chosen per game; the default is the stable one)."""
    apps = tmp_path / ".local/share/Steam/steamapps"
    if experimental:
        (apps / "common/Proton - Experimental (ARM64)").mkdir(parents=True)
        (apps / "common/Proton - Experimental (ARM64)/toolmanifest.vdf").write_text(
            '"manifest"\n{\n  "commandline" "/proton %verb%"\n  "require_tool_appid" "4185400"\n}\n')
        (apps / "appmanifest_4427310.acf").write_text(
            '"AppState"\n{\n\t"appid"\t\t"4427310"\n\t"name"\t\t"Proton Experimental (ARM64)"\n'
            '\t"StateFlags"\t\t"4"\n\t"installdir"\t\t"Proton - Experimental (ARM64)"\n}\n')
    (apps / "common/Proton 11.0 (ARM64)").mkdir(parents=True)
    (apps / "common/SteamLinuxRuntime_4-arm64").mkdir(parents=True)
    (apps / "common/Proton 11.0 (ARM64)/toolmanifest.vdf").write_text(
        '"manifest"\n{\n  "version" "2"\n  "commandline" "/proton %verb%"\n  "require_tool_appid" "4185400"\n}\n')
    (apps / "common/SteamLinuxRuntime_4-arm64/toolmanifest.vdf").write_text(
        '"manifest"\n{\n  "commandline" "/_v2-entry-point --verb=%verb% --"\n}\n')
    for appid, name, d in ((4628740, "Proton 11.0 (ARM64)", "Proton 11.0 (ARM64)"),
                           (4185400, "Steam Linux Runtime 4.0 - Arm64", "SteamLinuxRuntime_4-arm64")):
        (apps / f"appmanifest_{appid}.acf").write_text(
            f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n\t"StateFlags"\t\t"4"\n'
            f'\t"installdir"\t\t"{d}"\n}}\n')
    a.arm64_compat_tools = lambda: {
        "proton_11-arm64": {"appid": 4628740, "display_name": "Proton 11.0-2 (ARM64)", "from_oslist": "windows",
                            "require_tool_appid": 4185400, "aliases": "proton-stable-arm64"},
        "proton-experimental-arm64": {"appid": 4427310, "display_name": "Proton Experimental (ARM64)",
                                      "from_oslist": "windows", "aliases": "proton-experimental"},
        "steamlinuxruntime_steamrt4-arm64": {"appid": 4185400, "from_oslist": "linux"},
    }
    return apps


def test_proton_status_and_command(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    apps = fake_steam_tools(a, tmp_path)
    st = a.cmd_proton_status({})
    assert st["ready"]["name"] == "proton_11-arm64" and st["suggested"]["name"] == "proton_11-arm64"  # stable
    st = a.cmd_proton_status({"tool": "proton-stable-arm64"})
    assert st["ready"]["name"] == "proton_11-arm64"
    assert [t["name"] for t in st["tools"]] == ["proton_11-arm64", "proton-experimental-arm64"]  # stable first
    cmd = a.compat_command(st["ready"]["dir"])
    assert cmd == [str(apps / "common/SteamLinuxRuntime_4-arm64/_v2-entry-point"), "--verb=waitforexitandrun", "--",
                   str(apps / "common/Proton 11.0 (ARM64)/proton"), "waitforexitandrun"]
    assert a.pick_proton(st["tools"], "proton-experimental")["installed"] is False


def test_install_proton_request_mode(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    fake_steam_tools(a, tmp_path)
    calls = []
    monkeypatch.setattr(a.subprocess, "Popen", lambda args, **k: calls.append(args))
    r = a.cmd_install_proton({"tool": "proton-experimental-arm64"})
    assert r["requested"] == [4427310] and calls == [["steam", "-ifrunning", "steam://install/4427310"]]
    assert a.cmd_install_proton({"tool": "proton-stable-arm64"})["installed"] is True
    assert a.cmd_install_proton({})["installed"] is True  # the default: the stable one, already installed


def test_stub_manifest(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    lib = tmp_path / "lib"
    lib.mkdir()
    assert a.write_stub_manifest(4427310, "Proton Experimental (ARM64)", "Proton Experimental (ARM64)", str(lib))
    text = (lib / "appmanifest_4427310.acf").read_text()
    assert '"StateFlags"\t\t"1026"' in text and '"installdir"\t\t"Proton Experimental (ARM64)"' in text
    assert not a.write_stub_manifest(4427310, "x", "x", str(lib))  # never overwrites a real manifest


def test_pcvr_install_flow(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    fake_steam_tools(a, tmp_path, experimental=True)
    monkeypatch.setattr(a, "pcvr_pids", lambda base: [])
    prep = a.cmd_prepare_pcvr({"package": "rift.space_game", "title": "Space Game"})
    inc = Path(prep["incoming"])
    (inc / "game/Space Game_Data").mkdir(parents=True)
    (inc / "game/Space Game.exe").write_bytes(b"MZexe")
    (inc / "game/Space Game_Data/level0").write_bytes(b"1234")
    (inc / "revive").mkdir(exist_ok=True)
    (inc / "revive/ReviveInjector.exe").write_bytes(b"MZ")
    (inc / "xrlayer").mkdir(exist_ok=True)
    layer_json = ('{"api_layer": {"name": "XR_APILAYER_FRAMEPORT_timefix", '
                  '"library_path": "./libxr_frameport_timefix.so"}}')
    (inc / "xrlayer/XR_APILAYER_FRAMEPORT_timefix.json").write_text(layer_json)
    (inc / "xrlayer/libxr_frameport_timefix.so").write_bytes(b"ELF")
    manifests = {"game": {"Space Game.exe": 5, "Space Game_Data/level0": 4}, "revive": {"ReviveInjector.exe": 2},
                 "xrlayer": {"XR_APILAYER_FRAMEPORT_timefix.json": len(layer_json), "libxr_frameport_timefix.so": 3}}
    r = a.cmd_finalize_pcvr({"package": "rift.space_game", "title": "Space Game", "exe": "Space Game.exe",
                             "manifests": manifests, "env": {"PROTON_LOG": "1", "bad key": "x"}, "xr_layer": True})
    assert r["ok"] and r["proton"] == "proton_11-arm64"  # the default: stable, also with Experimental installed
    launch = Path(prep["anchor"]) / "launch.sh"
    text = launch.read_text()
    assert subprocess.run(["bash", "-n", str(launch)]).returncode == 0
    assert "SteamGameId=" in text and "export PROTON_LOG=1" in text and "bad key" not in text
    assert "ReviveInjector.exe /openxr 'Z:" in text and "Space Game.exe'" in text
    assert 'XR_ENABLE_API_LAYERS="XR_APILAYER_FRAMEPORT_timefix' in text and 'XR_API_LAYER_PATH="$base/xrlayer' in text
    env = subprocess.run(["bash", "-c", text.split("export XDG_RUNTIME_DIR")[0].replace("set -euo pipefail", "set -eu")
                          .split("[[ -d")[0] + 'base=/b\n' + text.split("export PROTON_LOG_DIR=\"$base\"\n")[1]
                          .split("export XDG_RUNTIME_DIR")[0] + 'echo "$XR_API_LAYER_PATH|$XR_ENABLE_API_LAYERS"'],
                         capture_output=True, text=True)
    assert env.stdout.strip() == "/b/xrlayer|XR_APILAYER_FRAMEPORT_timefix", env.stderr
    # the layer is registered as an explicit layer in the user's XDG data dir (Proton's container drops
    # XR_API_LAYER_PATH), pointing at a shared absolute copy
    reg = json.loads((tmp_path / ".local/share/openxr/1/api_layers/explicit.d/XR_APILAYER_FRAMEPORT_timefix.json")
                     .read_text())
    lib = Path(reg["api_layer"]["library_path"])
    assert lib.is_absolute() and lib.read_bytes() == b"ELF"
    dep = a.deployment("rift.space_game")
    assert dep["kind"] == "pcvr" and dep["files"]["game"] == manifests["game"]
    listed = a.cmd_list_installed({})["games"]
    assert listed[0]["kind"] == "pcvr" and listed[0]["apk_present"] and "files" not in listed[0]
    # an update that drops a file removes it, and a new prepare sees what's there
    prep2 = a.cmd_prepare_pcvr({"package": "rift.space_game", "title": "Space Game"})
    assert prep2["existing"]["game"] == manifests["game"] and prep2["appid"] == prep["appid"]
    a.cmd_finalize_pcvr({"package": "rift.space_game", "title": "Space Game", "exe": "Space Game.exe",
                         "manifests": {"game": {"Space Game.exe": 5}, "revive": manifests["revive"]}})
    assert not (Path(prep["base"]) / "game/Space Game_Data/level0").exists()
    # uninstall keeps the Proton prefix (saves)
    (Path(prep["base"]) / "compatdata/pfx").mkdir(parents=True)
    assert a.cmd_uninstall({"package": "rift.space_game", "keep_data": True})["removed"]
    assert (Path(prep["base"]) / "compatdata/pfx").is_dir() and not (Path(prep["base"]) / "game").exists()


def test_pcvr_finalize_rejects_bad_exe(monkeypatch, tmp_path):
    import pytest

    a = load_agent(monkeypatch, tmp_path)
    fake_steam_tools(a, tmp_path)
    monkeypatch.setattr(a, "pcvr_pids", lambda base: [])
    with pytest.raises(a.AgentError):
        a.cmd_finalize_pcvr({"package": "rift.x", "title": "X", "exe": "../../etc/passwd", "manifests": {}})


def test_shortcut_tag_and_launch_options(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    vdf = tmp_path / "shortcuts.vdf"
    a.upsert_shortcut(str(vdf), '"C:\\Revive\\ReviveInjector.exe"', "Game", '"D:\\G\\"', "", "Rift via Revive",
                      '/openxr "D:\\G\\Game.exe"')
    e = a.vdf_decode(vdf.read_bytes())["shortcuts"]["0"]
    assert e["tags"] == {"0": "Rift via Revive"} and e["LaunchOptions"] == '/openxr "D:\\G\\Game.exe"'


def test_appinfo_parser(monkeypatch, tmp_path):
    """appinfo.vdf v29: header, one app (binary KV with string-table keys), string table."""
    import struct

    a = load_agent(monkeypatch, tmp_path)
    keys = ["appinfo", "appid", "common", "name", "extended", "compat_tools", "proton-x-arm64", "from_oslist"]
    k = {n: i for i, n in enumerate(keys)}

    def s(key, val):
        return b"\x01" + struct.pack("<I", k[key]) + val.encode() + b"\0"

    def m(key, body):
        return b"\x00" + struct.pack("<I", k[key]) + body + b"\x08"
    kv = m("appinfo", b"\x02" + struct.pack("<Ii", k["appid"], 7) + m("common", s("name", "Compat List")) +
           m("extended", m("compat_tools", m("proton-x-arm64", s("from_oslist", "windows") +
                                                         b"\x02" + struct.pack("<Ii", k["appid"], 99))))) + b"\x08"
    app = struct.pack("<I", 7) + struct.pack("<I", 60 + len(kv)) + b"\0" * 60 + kv
    body = app + struct.pack("<I", 0)
    str_off = 16 + len(body)
    table = struct.pack("<I", len(keys)) + b"".join(n.encode() + b"\0" for n in keys)
    data = struct.pack("<II", 0x07564429, 1) + struct.pack("<q", str_off) + body + table
    f = tmp_path / "appinfo.vdf"
    f.write_bytes(data)
    out = a.appinfo_entries(path=str(f))
    assert out[7]["common"]["name"] == "Compat List"
    assert out[7]["extended"]["compat_tools"]["proton-x-arm64"] == {"from_oslist": "windows", "appid": 99}


def test_run_tree_kills_the_whole_group(monkeypatch, tmp_path):
    """Wine leaves children holding the output open; a timeout must still return and kill them."""
    import time

    a = load_agent(monkeypatch, tmp_path)
    start = time.time()
    out, code = a.run_tree(["bash", "-c", "echo started; (sleep 60 &) ; sleep 60"], dict(a.os.environ), str(tmp_path),
                           str(tmp_path / "log"), 2)
    assert code is None and "started" in out and time.time() - start < 20
    out, code = a.run_tree(["bash", "-c", "echo ok; exit 3"], dict(a.os.environ), str(tmp_path), str(tmp_path / "l2"),
                           10)
    assert (out.strip(), code) == ("ok", 3)


def test_purge_removes_frameport_and_keeps_saves(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    fake_steam_tools(a, tmp_path)
    monkeypatch.setattr(a, "pcvr_pids", lambda base: [])
    monkeypatch.setattr(a, "stop_steam", lambda: True)
    monkeypatch.setattr(a, "start_steam", lambda s: None)
    users = tmp_path / ".local/share/Steam/userdata/42/config"
    (users / "grid").mkdir(parents=True)
    # a Quest-style install (base = anchor, saves in lepton-data) and a PC VR one (saves in compatdata)
    q = Path(a.ANCHORS) / "com.x.y"
    (q / "lepton-app").mkdir(parents=True)
    (q / "lepton-data" / "external").mkdir(parents=True)
    (q / "launch.sh").write_text("#!/bin/sh")
    (q / "deployment.json").write_text(json.dumps({"package": "com.x.y", "appid": 7, "base": str(q), "title": "Q"}))
    a.upsert_shortcut(str(users / "shortcuts.vdf"), f'"{q}/launch.sh"', "Q", str(q))
    (users / "grid" / "7p.png").write_bytes(b"x")
    agent_home = Path(a.AGENT_HOME)
    (agent_home / "agent").mkdir(parents=True)
    a.purge_worker(json.dumps({"keep_saves": True, "status": str(tmp_path / "st.json")}))
    st = json.loads((tmp_path / "st.json").read_text())
    assert st["state"] == "done", st
    assert (q / "lepton-data").is_dir() and not (q / "lepton-app").exists() and not (q / "launch.sh").exists()
    assert a.vdf_decode((users / "shortcuts.vdf").read_bytes())["shortcuts"] == {}
    assert not (users / "grid" / "7p.png").exists() and not agent_home.exists()
    # without keeping saves everything goes
    a.purge_worker(json.dumps({"keep_saves": False, "status": str(tmp_path / "st.json")}))
    assert not Path(a.ANCHORS).exists()


def test_shortcut_tags_merge(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    vdf = tmp_path / "shortcuts.vdf"
    exe = '"/x/launch.sh"'
    a.upsert_shortcut(str(vdf), exe, "G", "/x", tags=["Meta Quest", "Action"])
    root = a.vdf_decode(vdf.read_bytes())
    root["shortcuts"]["0"]["tags"]["9"] = "My collection"  # a tag the user set in Steam
    vdf.write_bytes(a.vdf_encode(root))
    a.upsert_shortcut(str(vdf), exe, "G", "/x", tags=["Meta Quest", "Action", "Indie"])
    tags = list(a.vdf_decode(vdf.read_bytes())["shortcuts"]["0"]["tags"].values())
    assert tags == ["Quest on Frame", "Meta Quest", "Action", "My collection", "Indie"]


def test_pcvr_launcher_oculus_hmd_helper(monkeypatch, tmp_path):
    """pcvr.oculus_unreal: launch.sh runs Revive's injector (or the game) through fp_oculushmd.exe."""
    import pytest

    a = load_agent(monkeypatch, tmp_path)
    fake_steam_tools(a, tmp_path, experimental=True)
    monkeypatch.setattr(a, "pcvr_pids", lambda base: [])

    def install(pkg, helper=True, revive=True, oculus_hmd=True):
        prep = a.cmd_prepare_pcvr({"package": pkg, "title": "UE Game"})
        inc = Path(prep["incoming"])
        (inc / "game").mkdir(parents=True, exist_ok=True)
        (inc / "game/UEGame.exe").write_bytes(b"MZexe")
        manifests = {"game": {"UEGame.exe": 5}}
        if revive:
            (inc / "revive").mkdir(exist_ok=True)
            (inc / "revive/ReviveInjector.exe").write_bytes(b"MZ")
            manifests["revive"] = {"ReviveInjector.exe": 2}
        if helper:
            (inc / "helpers").mkdir(exist_ok=True)
            (inc / "helpers/fp_oculushmd.exe").write_bytes(b"MZhelp")
            manifests["helpers"] = {"fp_oculushmd.exe": 6}
        a.cmd_finalize_pcvr({"package": pkg, "title": "UE Game", "exe": "UEGame.exe", "manifests": manifests,
                             "revive": revive, "oculus_hmd": oculus_hmd, "game_args": ["-nocrashreports"]})
        launch = Path(prep["anchor"]) / "launch.sh"
        assert subprocess.run(["bash", "-n", str(launch)]).returncode == 0
        return launch.read_text().splitlines()[-1], prep["base"]

    cmd, base = install("rift.ue_game")
    helper = f"{base}/helpers/fp_oculushmd.exe"
    assert helper in cmd and cmd.index(helper) < cmd.index("ReviveInjector.exe")
    # everything after the helper is a Windows command line: the injector by its Z: path, then the game + args
    assert f"'Z:{base}/revive/ReviveInjector.exe'".replace("/", "\\") in cmd
    assert (cmd.index("ReviveInjector.exe") < cmd.index("/openxr") < cmd.index("UEGame.exe")
            < cmd.index("-nocrashreports"))
    assert a.deployment("rift.ue_game")["oculus_hmd"] is True
    cmd, base = install("rift.ue_direct", revive=False)
    assert f"{base}/helpers/fp_oculushmd.exe" in cmd and "ReviveInjector" not in cmd and "'Z:" in cmd
    cmd, base = install("rift.ue_plain", helper=False, oculus_hmd=False)
    assert "fp_oculushmd" not in cmd and f"{base}/revive/ReviveInjector.exe /openxr" in cmd
    with pytest.raises(a.AgentError, match="fp_oculushmd.exe missing"):
        install("rift.ue_nohelper", helper=False)


def test_list_files(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    base = tmp_path / "Applications/quest-frame/rift.g"
    (base / "game/Bin").mkdir(parents=True)
    (base / "game/Bin/Game.exe").write_bytes(b"MZ12")
    (base / "game/Bin/CrashReportClient.exe.disabled").write_bytes(b"MZ")
    (base / "launch.sh").write_text("#!/bin/sh\n")
    (base / "deployment.json").write_text(json.dumps({
        "package": "rift.g", "kind": "pcvr", "base": str(base),
        "files": {"game": {"Bin/Game.exe": 4, "Bin/Data.pak": 10, "Bin/CrashReportClient.exe": 2}}}))
    r = a.cmd_list_files({"package": "rift.g"})
    assert len(r["roots"]) == 1 and r["roots"][0]["name"] == "Install folder"  # the anchor is the same folder
    files = dict(map(tuple, r["roots"][0]["files"]))
    assert files["game/Bin/Game.exe"] == 4 and "launch.sh" in files
    assert r["missing"] == [["game/Bin/Data.pak", 10, None]]  # the renamed crash reporter isn't "missing"
    assert not r["truncated"]


def test_launch_uses_steam_shortcut(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    anchor.mkdir(parents=True)
    deployment = {"package": "com.x.y", "appid": 2546384938, "base": str(anchor)}
    (anchor / "deployment.json").write_text(json.dumps(deployment))
    calls = []
    monkeypatch.setattr(a, "run", lambda cmd, **k: (calls.append(cmd), SimpleNamespace(returncode=0, stdout=""))[1])
    cfg = tmp_path / ".local/share/Steam/userdata/42/config"
    cfg.mkdir(parents=True)
    with pytest.raises(a.AgentError, match="not in the Frame's Steam library"):
        a.cmd_launch({"package": "com.x.y"})
    exe = f'"{anchor}/launch.sh"'
    a.upsert_shortcut(str(cfg / "shortcuts.vdf"), exe, "Old title", str(anchor))  # keeps its first id
    r = a.cmd_launch({"package": "com.x.y"})
    assert r["gameid"] == (a.shortcut_appid(exe, "Old title") << 32) | 0x02000000
    assert calls[-1][-1] == f"steam://rungameid/{r['gameid']}" and calls[-1][0] == "systemd-run"


def test_library_users_prefers_the_signed_in_account(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    steam = tmp_path / ".local/share/Steam"
    with pytest.raises(a.AgentError, match="signed-in"):
        a.library_users()
    for u in ("12345", "999"):
        (steam / "userdata" / u / "config").mkdir(parents=True)
    assert a.library_users() == ["12345", "999"]  # unknown: every account (used to fail with 2 accounts)
    (steam / "config").mkdir()
    (steam / "config/loginusers.vdf").write_text(
        '"users"\n{\n\t"76561197960278073"\n\t{\n\t\t"MostRecent"\t\t"1"\n\t}\n'
        '\t"76561197960266727"\n\t{\n\t\t"MostRecent"\t\t"0"\n\t}\n}\n')
    assert a.library_users() == ["12345", "999"]  # every account, the signed-in one first


def test_uninstall_quest_keeping_saves_drops_the_install_record(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    (anchor / "lepton-app").mkdir(parents=True)
    (anchor / "lepton-app/game.apk").write_bytes(b"PK")
    (anchor / "lepton-data/saves").mkdir(parents=True)
    (anchor / "lepton-data/saves/slot1").write_text("progress")
    (anchor / "launch.sh").write_text("#!/bin/sh\n")
    (anchor / "deployment.json").write_text(json.dumps({"package": "com.x.y", "base": str(anchor), "appid": 1}))
    monkeypatch.setattr(a, "container_running", lambda appid: False)
    assert a.cmd_uninstall({"package": "com.x.y"})["removed"]
    assert not a.cmd_list_installed({})["games"]  # no longer reported as installed
    assert (anchor / "lepton-data/saves/slot1").read_text() == "progress"  # saves kept



def test_uninstall_quest_deleting_data_removes_saves_and_mods(monkeypatch, tmp_path):
    """GitHub #130: the Uninstall dialog's "Also delete its saves…" box (keep_data False) removes lepton-data too."""
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    (anchor / "lepton-app").mkdir(parents=True)
    (anchor / "lepton-data/external/ModData/songs").mkdir(parents=True)
    (anchor / "lepton-data/external/ModData/songs/old.zip").write_bytes(b"x")
    (anchor / "deployment.json").write_text(json.dumps({"package": "com.x.y", "base": str(anchor), "appid": 1}))
    monkeypatch.setattr(a, "container_running", lambda appid: False)
    assert a.cmd_uninstall({"package": "com.x.y", "keep_data": False})["removed"]
    assert not (anchor / "lepton-data").exists()

def test_prune_shortcuts_on_relaunch_change(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    vdf = str(tmp_path / "shortcuts.vdf")
    old = a.upsert_shortcut(vdf, '"C:\\Revive\\ReviveInjector.exe"', "Vader", "/d", tag="Rift via Revive",
                            tags=["Rift via Revive"])
    a.upsert_shortcut(vdf, '"C:\\Other.exe"', "Other game", "/d", tag="Rift via Revive", tags=["Rift via Revive"])
    removed = a.prune_shortcuts(vdf, "Vader", '"C:\\game\\WKND.exe"', "Rift via Revive")  # new direct launch
    assert removed == [old]
    root = a.vdf_decode(open(vdf, "rb").read())
    names = {v.get("appname") for v in root["shortcuts"].values() if isinstance(v, dict)}
    assert names == {"Other game"}  # the stale Vader entry is gone, the unrelated one stays
    # re-adding Vader directly, then pruning again, is a no-op for the kept entry
    a.upsert_shortcut(vdf, '"C:\\game\\WKND.exe"', "Vader", "/d", tag="Rift via Revive", tags=["Rift via Revive"])
    assert a.prune_shortcuts(vdf, "Vader", '"C:\\game\\WKND.exe"', "Rift via Revive") == []


def test_libovr_redirect_symlink(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    base = tmp_path / "b"
    (base / "game/Bin/Win64").mkdir(parents=True)
    (base / "game/Bin/Win64/Game.exe").write_bytes(b"MZ")
    (base / "revive").mkdir()
    (base / "revive/LibReviveXR64.dll").write_bytes(b"REVIVE")
    # an Unreal-style OVRPlugin.dll in its own dir -> the redirect must land there too
    (base / "game/Engine/Plug/OVRPlugin/Win64").mkdir(parents=True)
    (base / "game/Engine/Plug/OVRPlugin/Win64/OVRPlugin.dll").write_bytes(b"MZ")
    exe_rel = "Bin/Win64/Game.exe"
    link = base / "game/Bin/Win64/LibOVRRT64_1.dll"
    plugin_link = base / "game/Engine/Plug/OVRPlugin/Win64/LibOVRRT64_1.dll"
    a.set_libovr_redirect(str(base), exe_rel, enabled=True)
    assert link.is_symlink() and link.read_bytes() == b"REVIVE"  # redirect to Revive's runtime (exe dir)
    assert plugin_link.is_symlink() and plugin_link.read_bytes() == b"REVIVE"  # and next to OVRPlugin.dll
    assert not (base / "game/Bin/Win64/LibOVRRT32_1.dll").exists()  # no 32-bit Revive dll -> not created
    # disabling removes our symlink
    a.set_libovr_redirect(str(base), exe_rel, enabled=False)
    assert not link.exists()
    # never clobber a real game-shipped LibOVRRT
    real = base / "game/Bin/Win64/LibOVRRT64_1.dll"
    real.write_bytes(b"REAL")
    a.set_libovr_redirect(str(base), exe_rel, enabled=True)
    assert not real.is_symlink() and real.read_bytes() == b"REAL"


def test_libovr_redirect_to_bundled_revive(monkeypatch, tmp_path):
    """A repack's own LibRevive64.dll (no FramePort Revive) becomes the LibOVRRT the game loads on the Frame."""
    a = load_agent(monkeypatch, tmp_path)
    base = tmp_path / "b"
    (base / "game/G").mkdir(parents=True)
    (base / "game/G/Game.exe").write_bytes(b"MZ")
    (base / "game/G/LibRevive64.dll").write_bytes(b"BUNDLED")
    (base / "revive").mkdir()
    (base / "revive/LibReviveXR64.dll").write_bytes(b"REVIVE")
    link = base / "game/G/LibOVRRT64_1.dll"
    a.set_libovr_redirect(str(base), "G/Game.exe", enabled=True)
    assert link.read_bytes() == b"REVIVE"
    a.set_libovr_redirect(str(base), "G/Game.exe", enabled=True, bundled=True)  # switches target
    assert link.is_symlink() and link.read_bytes() == b"BUNDLED"
    a.set_libovr_redirect(str(base), "G/Game.exe", enabled=False, bundled=True)
    assert not link.exists() and (base / "game/G/LibRevive64.dll").read_bytes() == b"BUNDLED"


def test_proton_launcher_game_args(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "compat_command", lambda d: ["/proton", "waitforexitandrun"])
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    a.write_proton_launcher(str(anchor), "/b", "rift.x", "X", 1, {"dir": "/p", "name": "proton"}, "G/X.exe", False,
                            {"WINEDLLOVERRIDES": "xinput1_3=n,b"}, game_args=["-vrmode", "OpenVR", "-hmd=OpenXR",
                                                                              "$(rm -rf /)"])
    text = (anchor / "launch.sh").read_text()
    assert "/b/game/G/X.exe -vrmode OpenVR -hmd=OpenXR >>" in text and "rm -rf" not in text
    assert "export WINEDLLOVERRIDES=xinput1_3=n,b" in text


PNG_1PX = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63f8"
                        "cfc0f01f0005000201a5d6f1c80000000049454e44ae426082")


def _fake_render_model(root, name, grip_origin=(0.0, 0.0, 0.0), grip_rot=(0, 0, 0)):
    d = root / "resources/rendermodels" / name
    d.mkdir(parents=True)
    (d / "diffuse.png").write_bytes(PNG_1PX)
    (d / "body.mtl").write_text("newmtl skin\nmap_Kd diffuse.png\n")
    # a quad (fan-triangulated) with v/vt/vn corners, plus a negative-index face
    (d / "body.obj").write_text("mtllib body.mtl\nusemtl skin\n"
                                "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nvn 0 0 1\n"
                                "f 1/1/1 2/2/1 3/3/1 4/4/1\nf -4/-4/-1 -3/-3/-1 -1/-1/-1\n")
    (d / "status.obj").write_text("v 5 5 5\nv 6 5 5\nv 5 6 5\nf 1 2 3\n")
    (d / f"{name}.json").write_text(json.dumps({"components": {
        "body": {"filename": "body.obj"}, "status": {"filename": "status.obj"},
        "openxr_grip": {"component_local": {"origin": list(grip_origin), "rotate_xyz": list(grip_rot)}}}}))
    return d


def _read_glb(data):
    import struct as st
    magic, version, total = st.unpack_from("<III", data)
    assert magic == 0x46546C67 and version == 2 and total == len(data)
    jlen, jtype = st.unpack_from("<I4s", data, 12)
    assert jtype == b"JSON" and jlen % 4 == 0
    gltf = json.loads(data[20:20 + jlen])
    blen, btype = st.unpack_from("<I4s", data, 20 + jlen)
    assert btype == b"BIN\x00" and blen == gltf["buffers"][0]["byteLength"]
    return gltf, data[28 + jlen:28 + jlen + blen]


def test_controller_models_pick_and_convert(monkeypatch, tmp_path):
    import struct as st
    a = load_agent(monkeypatch, tmp_path)
    root = tmp_path / "steamvr"
    _fake_render_model(root, "valve_frame_controller_left", grip_origin=(1.0, 0.0, 0.0))
    _fake_render_model(root, "valve_frame_controller_right", grip_rot=(0, 0, 90))
    _fake_render_model(root, "vr_controller_vive_1_5")
    dirs = a.render_model_dirs([str(root)])
    assert set(dirs) == {"valve_frame_controller_left", "valve_frame_controller_right", "vr_controller_vive_1_5"}
    picked = a.pick_controller_models(dirs)
    assert picked == {"left": dirs["valve_frame_controller_left"], "right": dirs["valve_frame_controller_right"]}
    assert a.pick_controller_models({"frame_left": "/x"}) == {}  # needs both sides
    assert a.model_side("controller_r") == "right" and a.model_side("hmd") is None

    gltf, blob = _read_glb(a.controller_glb(picked["left"]))
    prim = gltf["meshes"][0]["primitives"]
    assert len(prim) == 1  # status component skipped, one texture
    acc = gltf["accessors"]
    pos = acc[prim[0]["attributes"]["POSITION"]]
    assert pos["count"] == 4 and acc[prim[0]["indices"]]["count"] == 9  # 4 unique corners, 3 triangles
    # grip origin (1, 0, 0) -> vertices shifted by -1 on x
    assert pos["min"] == [-1.0, 0.0, 0.0] and pos["max"] == [0.0, 1.0, 0.0]
    assert gltf["images"][0]["mimeType"] == "image/png"
    view = gltf["bufferViews"][gltf["images"][0]["bufferView"]]
    assert blob[view["byteOffset"]:view["byteOffset"] + view["byteLength"]] == PNG_1PX
    uv_view = gltf["bufferViews"][acc[prim[0]["attributes"]["TEXCOORD_0"]]["bufferView"]]
    uvs = st.unpack_from("<8f", blob, uv_view["byteOffset"])
    assert uvs[:2] == (0.0, 1.0)  # OBJ (0, 0) -> glTF (0, 1)

    # 90° about z: raw +x becomes grip -y (R^T applied)
    gltf, blob = _read_glb(a.controller_glb(picked["right"]))
    pos = gltf["accessors"][gltf["meshes"][0]["primitives"][0]["attributes"]["POSITION"]]
    x = st.unpack_from("<6f", blob, gltf["bufferViews"][pos["bufferView"]]["byteOffset"])
    assert abs(x[3]) < 1e-6 and abs(x[4] + 1.0) < 1e-6  # vertex (1, 0, 0)


def test_controller_models_install(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    root = tmp_path / "steamvr"
    _fake_render_model(root, "frame_controller_left")
    _fake_render_model(root, "frame_controller_right")
    monkeypatch.setattr(a, "steamvr_roots", lambda: [str(root)])
    files = tmp_path / "files"
    result = a.install_controller_models(str(files), True)
    assert result["ok"] and set(result["sources"]) == {"left", "right"}
    left = files / "framebridge/controller_left.glb"
    assert left.read_bytes()[:4] == b"glTF" and (files / "framebridge/controller_right.glb").exists()
    assert len(list((tmp_path / ".local/share/frameport/controller-models").glob("*.glb"))) == 2  # cached
    assert a.install_controller_models(str(files), False) is None and not left.exists()
    monkeypatch.setattr(a, "steamvr_roots", lambda: [])
    missing = a.install_controller_models(str(files), True)
    assert missing["ok"] is False and "no Steam Frame controller render models" in missing["error"]
    assert a.cmd_controller_models({})["picked"] == {}


def test_storage_targets_follow_lepton(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    lepton = tmp_path / "Lepton"
    (lepton / "liblepton").mkdir(parents=True)
    (lepton / "lepton").write_text("#!/bin/sh\n")
    (lepton / "liblepton/mounting.sh").write_text(
        'ln -s "${HOME}/Videos" "${TARGET_PATH}/Movies"\n'
        'ln -s "${HOME}/Downloads" "${TARGET_PATH}/Download"\n')
    monkeypatch.setattr(a, "lepton_path", lambda: (str(lepton / "lepton"), None))
    base = tmp_path / "Applications/quest-frame/com.x.y"
    base.mkdir(parents=True)
    (base / "deployment.json").write_text(json.dumps({"package": "com.x.y", "base": str(base)}))
    t = {x["id"]: x for x in a.cmd_storage_targets({"package": "com.x.y"})["targets"]}
    assert t["videos"]["android"] == "/sdcard/Movies" and t["videos"]["shared"]
    assert t["downloads"]["android"] == "/sdcard/Download" and "documents" not in t  # read from Lepton, not assumed
    assert (tmp_path / "Videos").is_dir()  # Lepton only links folders that exist
    assert t["app"]["path"].endswith("lepton-data/external") and t["app"]["android"] == "/sdcard"
    assert t["app-files"]["android"] == "/sdcard/Android/data/com.x.y/files"
    # without Lepton's script: the known defaults
    monkeypatch.setattr(a, "lepton_path", lambda: (None, None))
    ids = {x["id"] for x in a.cmd_storage_targets({})["targets"]}
    assert ids == {"documents", "downloads", "videos"}


def test_link_media_into_app_own_folder(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "lepton_path", lambda: (None, None))  # default shared folders
    base = tmp_path / "Applications/quest-frame/cn.player"
    ext = base / "lepton-data/external"
    for d in ("4XPlayer", "Android", "DCIM", "Music", ".hidden"):
        (ext / d).mkdir(parents=True)
    (tmp_path / "Videos").mkdir()
    (ext / "Movies").symlink_to(tmp_path / "Videos")  # Lepton's link to the shared folder: not the app's own
    (base / "deployment.json").write_text(json.dumps({"package": "cn.player", "base": str(base)}))
    assert a.app_media_dirs(str(ext)) == ["4XPlayer"]
    t = {x["id"]: x for x in a.cmd_storage_targets({"package": "cn.player"})["targets"]}
    assert t["app-media"]["android"] == "/sdcard/4XPlayer"
    video = tmp_path / "Videos/a_360.mp4"
    video.write_bytes(b"x" * 10)
    r = a.cmd_link_media({"package": "cn.player", "files": [str(video), str(tmp_path / "Videos/gone.mp4")]})
    assert r["folder"] == "4XPlayer" and r["linked"] == ["a_360.mp4"] and r["missing"]
    assert (ext / "4XPlayer/a_360.mp4").stat().st_ino == video.stat().st_ino  # a hard link, not a copy
    assert a.cmd_link_media({"package": "cn.player", "files": [str(video)]})["existing"] == ["a_360.mp4"]
    with pytest.raises(a.AgentError):  # only files from the shared folders
        a.cmd_link_media({"package": "cn.player", "files": [str(base / "deployment.json")]})


def test_grid_files_match_exact_appids(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    grid = tmp_path / "grid"
    grid.mkdir()
    for n in ("123p.jpg", "123.png", "123_hero.jpg", "123_logo.png", "1234p.jpg", "12345_hero.jpg", "notes.txt"):
        (grid / n).write_text("x")
    assert sorted(p.rsplit("/", 1)[1] for p in a.grid_files(str(grid), 123)) == \
        ["123.png", "123_hero.jpg", "123_logo.png", "123p.jpg"]


def test_vdf_backups_are_capped(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    vdf = tmp_path / "shortcuts.vdf"
    vdf.write_bytes(b"x")
    for i in range(9):
        (tmp_path / f"shortcuts.vdf.backup-2026010{i}-000000").write_bytes(b"old")
    a.backup_vdf(str(vdf))
    assert len(list(tmp_path.glob("shortcuts.vdf.backup-*"))) == a.VDF_BACKUPS


def test_finalize_checks_the_data_before_replacing_the_game(monkeypatch, tmp_path):
    """A failed data check must leave the installed APK and data as they were."""
    a = load_agent(monkeypatch, tmp_path)
    base = tmp_path / "Applications/quest-frame/com.x.y"
    app = base / "lepton-app"
    (app / "obb").mkdir(parents=True)
    (app / "game.apk").write_bytes(b"OLD")
    inc = base / "incoming"
    (inc / "obb").mkdir(parents=True)
    (inc / "game.apk").write_bytes(b"NEW")
    monkeypatch.setattr(a, "cmd_prepare", lambda args: {"base": str(base), "anchor": str(base), "appid": 1,
                                                         "incoming": str(inc), "lepton": "/lepton"})
    with pytest.raises(a.AgentError):
        a.cmd_finalize({"package": "com.x.y", "title": "X", "obb_manifest": {"main.obb": 10}})
    assert (app / "game.apk").read_bytes() == b"OLD" and (inc / "game.apk").exists()


def test_uninstall_removes_the_shortcut_with_steam_closed(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    anchor.mkdir(parents=True)
    (anchor / "deployment.json").write_text(json.dumps({"package": "com.x.y", "base": str(anchor), "appid": 7}))
    monkeypatch.setattr(a, "container_running", lambda appid: False)
    monkeypatch.setattr(a, "steam_users", lambda: ["1"])
    started = []
    monkeypatch.setattr(a, "run", lambda cmd, *k, **kw: started.append(cmd) or SimpleNamespace(returncode=0, stdout=""))
    r = a.cmd_uninstall({"package": "com.x.y", "remove_shortcut": True})
    assert r["shortcut_removed"] and started and started[0][0] == "systemd-run"  # the detached worker, not a live edit
    payload = json.loads(started[0][-1])
    assert payload["remove"] == [{"exe": f'"{anchor}/launch.sh"', "appid": 7}]


def test_prune_also_matches_the_earlier_tag_name(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    vdf = str(tmp_path / "shortcuts.vdf")
    old = a.upsert_shortcut(vdf, '"C:\\Revive\\ReviveInjector.exe"', "Vader", "/d", tag="Rift via Revive")
    assert a.prune_shortcuts(vdf, "Vader", '"C:\\game\\WKND.exe"', ("FramePort PC VR", "Rift via Revive")) == [old]


def test_flatscreen_marker_for_android_apps_without_vr(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    a.set_flatscreen(str(tmp_path), True)
    assert (tmp_path / "lepton-show-flatscreen").exists()
    a.set_flatscreen(str(tmp_path), True)  # idempotent
    a.set_flatscreen(str(tmp_path), False)
    assert not (tmp_path / "lepton-show-flatscreen").exists()
    a.set_flatscreen(str(tmp_path), False)



def test_launcher_exports_multiline_env_values(monkeypatch, tmp_path):
    """device.hide_navbar's value has a newline (a second Android property line): it must survive launch.sh."""
    import subprocess

    from frameport.patches.settings import HideNavBar

    a = load_agent(monkeypatch, tmp_path)
    a.write_launcher(str(tmp_path), "/b", "p", "T", 1, "/l", HideNavBar.ENV)
    script = (tmp_path / "launch.sh").read_text()
    line = next(x for x in script.split("export ") if x.startswith("LEPTON_GFXRECON_FP_PROPS="))
    line = line[:line.index("\n", line.index("mainkeys"))]
    out = subprocess.run(["bash", "-c", f'export {line}\nprintf %s "$LEPTON_GFXRECON_FP_PROPS"'],
                         capture_output=True, text=True, check=True).stdout
    assert out == "0\nqemu.hw.mainkeys=1"


def test_keep_awake_falls_back_to_idle_when_sleep_is_refused(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    calls, started = [], []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "systemd-run":
            started.append(next(c for c in cmd if c.startswith("--what=")))
        active = cmd[:3] == ["systemctl", "--user", "is-active"] and started and started[-1] == "--what=idle"
        return SimpleNamespace(returncode=0, stdout="active\n" if active else "failed\n", stderr="")

    monkeypatch.setattr(a, "run", fake_run)
    monkeypatch.setattr(a.time, "sleep", lambda s: None)
    r = a.cmd_keep_awake({"on": True, "minutes": 30})
    assert r == {"awake": True, "what": "idle", "seconds": 1800} and started == ["--what=idle:sleep", "--what=idle"]
    run_cmd = next(c for c in calls if c[0] == "systemd-run")
    assert "systemd-inhibit" in run_cmd and run_cmd[-2:] == ["sleep", "1800"]
    calls.clear()
    assert a.cmd_keep_awake({"on": False}) == {"awake": False}
    assert calls and all(c[0] == "systemctl" for c in calls)  # only stops the unit


def test_purge_without_saves_removes_container_workdirs_and_stale_shortcuts(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    fake_steam_tools(a, tmp_path)
    monkeypatch.setattr(a, "pcvr_pids", lambda base: [])
    monkeypatch.setattr(a, "stop_steam", lambda: True)
    monkeypatch.setattr(a, "start_steam", lambda s: None)
    users = tmp_path / ".local/share/Steam/userdata/42/config"
    (users / "grid").mkdir(parents=True)
    q = Path(a.ANCHORS) / "com.x.y"
    work = q / "lepton-data" / "baked" / "data_workdir" / "work"  # overlayfs leaves these with mode 000
    (work / "inner").mkdir(parents=True)
    (work / "inner" / "f").write_text("x")
    (q / "launch.sh").write_text("#!/bin/sh")
    (q / "deployment.json").write_text(json.dumps({"package": "com.x.y", "appid": 7, "base": str(q), "title": "Q"}))
    os.chmod(work / "inner", 0)
    os.chmod(work, 0)
    vdf = str(users / "shortcuts.vdf")
    a.upsert_shortcut(vdf, f'"{q}/launch.sh"', "Q", str(q))
    stale_exe = f'"{a.ANCHORS}/com.gone/launch.sh"'  # no install record any more
    stale = a.upsert_shortcut(vdf, stale_exe, "Gone", "/x")
    a.upsert_shortcut(vdf, '"/usr/bin/other"', "Not ours", "/x")
    (users / "grid" / f"{stale}p.jpg").write_bytes(b"x")
    a.purge_worker(json.dumps({"keep_saves": False, "status": str(tmp_path / "st.json")}))
    st = json.loads((tmp_path / "st.json").read_text())
    assert st["state"] == "done" and not st["errors"], st
    assert not Path(a.ANCHORS).exists()
    names = [s["appname"] for s in a.vdf_decode((users / "shortcuts.vdf").read_bytes())["shortcuts"].values()]
    assert names == ["Not ours"] and not (users / "grid" / f"{stale}p.jpg").exists()


def test_battery_state(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    ps = tmp_path / "power_supply"

    def supply(name, **files):
        (ps / name).mkdir(parents=True)
        for k, v in files.items():
            (ps / name / k).write_text(v + "\n")
    monkeypatch.setattr(a, "POWER_SUPPLY", str(ps))
    assert a.battery_state() is None  # no power_supply folder (or no battery): nothing to show
    supply("battery", type="Battery", capacity="42", status="Discharging")
    supply("usb", type="USB", online="0")
    assert a.battery_state() == {"percent": 42, "status": "Discharging", "plugged": False, "draining": True}
    (ps / "usb" / "online").write_text("1\n")  # cable in, battery not charging yet ("Not charging"): still plugged
    assert a.battery_state()["plugged"] is True
    assert a.cmd_battery({})["battery"]["percent"] == 42


def test_virtual_keyboard_events_text_and_release(monkeypatch, tmp_path):
    import io
    import struct as st

    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a.time, "sleep", lambda s: None)
    written = []
    kb = a.VirtualKeyboard(fd="fake", write=lambda fd, data: written.append(st.unpack("llHHi", data)[2:]))
    kb.key(30, 1)  # 'a' down: EV_KEY then SYN_REPORT
    assert written == [(1, 30, 1), (0, 0, 0)] and kb.held == {30}
    written.clear()
    assert kb.type_text("A!\N{SNOWMAN}") == "\N{SNOWMAN}"  # US layout; what it can't type is reported
    keys = [(c, v) for t, c, v in written if t == 1]
    assert keys == [(42, 1), (30, 1), (30, 0), (42, 0), (42, 1), (2, 1), (2, 0), (42, 0)]
    kb.key(57, 1)  # space held down when the PC goes away
    written.clear()
    kb.close()  # released before the device goes away
    assert (1, 57, 0) in written and kb.held == set()
    # session protocol: ready line, keys and text from JSON lines, keyboard closed at EOF
    out = io.StringIO()
    kb2 = a.VirtualKeyboard(fd="fake", write=lambda fd, data: None)
    closed = []
    kb2.close = lambda: closed.append(1)
    lines = io.StringIO('{"k": 28, "v": 1}\n{"k": 28, "v": 0}\nnot json\n{"text": "hi"}\n')
    assert a.keyboard_session(lines, out, keyboard=kb2) == 0
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert replies[0] == {"ready": True} and replies[1] == {"typed": True, "skipped": ""} and closed


def test_keyboard_session_reports_a_refused_device(monkeypatch, tmp_path):
    import io

    a = load_agent(monkeypatch, tmp_path)

    def refuse(*args, **kw):
        raise PermissionError(13, "Permission denied", "/dev/uinput")
    monkeypatch.setattr(a, "VirtualKeyboard", refuse)
    out = io.StringIO()
    assert a.keyboard_session(io.StringIO(""), out) == 1
    assert json.loads(out.getvalue())["ready"] is False


def test_boot_state_finds_unclean_previous_boot_and_last_launch(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    base = tmp_path / "game"
    base.mkdir()
    (base / "launch.log").write_text("x")
    os.utime(base / "launch.log", (1000, 1000))
    monkeypatch.setattr(a, "cmd_list_installed", lambda args: {"games": [{"package": "com.x", "title": "X",
                                                                           "base": str(base)}]})
    real_open = open

    def fake_open(path, *args, **kw):
        if path == "/proc/sys/kernel/random/boot_id":
            from io import StringIO
            return StringIO("abc\n")
        if path == "/proc/stat":
            from io import StringIO
            return StringIO("cpu 1 2 3\nbtime 2000\n")
        return real_open(path, *args, **kw)
    monkeypatch.setattr(a, "open", fake_open, raising=False)
    monkeypatch.setattr(a, "run", lambda cmd, **kw: SimpleNamespace(stdout="kernel: msm_dpu ... fault\n"))
    st = a.boot_state()
    assert st == {"boot_id": "abc", "boot_time": 2000, "prev_clean": False,
                  "last_launch": {"package": "com.x", "title": "X", "time": 1000.0}}
    monkeypatch.setattr(a, "run", lambda cmd, **kw: SimpleNamespace(stdout="Reached target System Power Off\n"))
    assert a.boot_state()["prev_clean"] is False  # cached for this boot: worked out once


def test_flat_windows_game_launcher_and_shortcut(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "compat_command", lambda d: ["/proton", "waitforexitandrun"])
    anchor, base = tmp_path / "anchor", tmp_path / "base"
    anchor.mkdir()
    tool = {"dir": "/tools/proton", "name": "Proton 11"}
    a.write_proton_launcher(str(anchor), str(base), "rift.game", "Game", 123, tool, "Game.exe", False, {}, vr=False)
    flat = (anchor / "launch.sh").read_text()
    a.write_proton_launcher(str(anchor), str(base), "rift.game", "Game", 123, tool, "Game.exe", False, {})
    vr = (anchor / "launch.sh").read_text()
    assert "SteamGameId" not in flat and "export SteamGameId=123" in vr  # Proton sets up VR only with it
    assert "STEAM_COMPAT_APP_ID=123" in flat
    vdf = str(tmp_path / "shortcuts.vdf")
    a.upsert_shortcut(vdf, '"/x/launch.sh"', "Game", "/x", tag="Windows game on Frame", openvr=False)
    entry = a.vdf_decode(open(vdf, "rb").read())["shortcuts"]["0"]
    assert entry["OpenVR"] == 0 and entry["tags"]["0"] == "Windows game on Frame"


def test_reinstall_with_unchanged_shortcut_does_not_restart_steam(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    (anchor / "artwork").mkdir(parents=True)
    (anchor / "artwork/portrait.jpg").write_bytes(b"art")
    (anchor / "deployment.json").write_text(json.dumps({"package": "com.x.y", "appid": 1, "title": "X", "base": "b"}))
    cfg = tmp_path / ".local/share/Steam/userdata/42/config"
    cfg.mkdir(parents=True)
    exe, title, start, icon, tag, tags, openvr = a.shortcut_args("com.x.y")
    assert a.library_changes(["42"], ["com.x.y"])  # not in the library yet
    a.upsert_shortcut(str(cfg / "shortcuts.vdf"), exe, title, start, icon, tag, tags=tags, openvr=openvr)
    assert not a.library_changes(["42"], ["com.x.y"])  # a reinstall: nothing to change
    stops = []
    monkeypatch.setattr(a, "stop_steam", lambda: stops.append(1) or True)
    monkeypatch.setattr(a, "start_steam", lambda s: stops.append(2))
    a.shortcuts_worker(json.dumps({"packages": ["com.x.y"], "remove": []}))
    status = json.load(open(a.STATUS_FILE))
    assert not stops and status["state"] == "done" and status["unchanged"]
    assert status["added"][0]["package"] == "com.x.y"
    assert (cfg / "grid").is_dir() and any(p.name.endswith("p.jpg") for p in (cfg / "grid").iterdir())



def test_devkit_games_get_art_without_a_steam_restart(monkeypatch, tmp_path):
    # GitHub #41: on Frames whose Steam drops FramePort's shortcuts.vdf entries, every install/art update restarted
    # Steam, which also dropped the live devkit entry: the next Play then restarted Steam once more
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    (anchor / "artwork").mkdir(parents=True)
    (anchor / "artwork/portrait.jpg").write_bytes(b"art")
    (anchor / "deployment.json").write_text(json.dumps({"package": "com.x.y", "appid": 1, "title": "X", "base": "b"}))
    (tmp_path / ".local/share/Steam/userdata/42/config").mkdir(parents=True)
    monkeypatch.setattr(a, "devkit_gameid", lambda pkg: "X")
    monkeypatch.setattr(a, "devkit_appid", lambda gid: 777)
    art = []
    monkeypatch.setattr(a, "copy_grid_art", lambda pkg, appid, users=None: art.append((pkg, appid)))
    stops = []
    monkeypatch.setattr(a, "stop_steam", lambda: stops.append(1) or True)
    monkeypatch.setattr(a, "start_steam", lambda s: stops.append(2))
    a.shortcuts_worker(json.dumps({"packages": ["com.x.y"], "remove": []}))
    status = json.load(open(a.STATUS_FILE))
    assert not stops and art == [("com.x.y", 777)] and status["devkit"] == ["com.x.y"] and status["state"] == "done"


def test_launch_re_adds_a_forgotten_devkit_entry_live(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    anchor.mkdir(parents=True)
    (anchor / "deployment.json").write_text(json.dumps({"package": "com.x.y", "appid": 111, "title": "X",
                                                        "base": str(anchor)}))
    monkeypatch.setattr(a, "run", lambda cmd, **k: SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(a, "shortcut_appid_for", lambda exe: None)  # Steam has no shortcut either
    monkeypatch.setattr(a, "devkit_gameid", lambda pkg: "X")
    monkeypatch.setattr(a, "devkit_appid", lambda gid: None)  # Steam restarted and forgot the devkit entry
    monkeypatch.setattr(a, "devkit_register", lambda pkg: 333)
    monkeypatch.setattr(a, "steam_launch", lambda appid, wait=10: {"result": "started", "appid": appid})
    got = a.cmd_launch({"package": "com.x.y"})  # no NOT_IN_LIBRARY (whose repair restarts Steam)
    assert got["via"] == "devkit" and got["steam"]["appid"] == 333

def test_shortcuts_lost_after_steam_restart(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    anchor.mkdir(parents=True)
    (anchor / "deployment.json").write_text(json.dumps({"package": "com.x.y", "appid": 1, "title": "X", "base": "b"}))
    cfg = tmp_path / ".local/share/Steam/userdata/42/config"
    cfg.mkdir(parents=True)
    monkeypatch.setattr(a, "run", lambda cmd, **k: SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(a.time, "sleep", lambda s: None)
    assert a.shortcuts_lost(["42"], ["com.x.y"]) == ["com.x.y"]  # Steam put its old shortcuts.vdf back
    exe, title, start, icon, tag, tags, openvr = a.shortcut_args("com.x.y")
    a.upsert_shortcut(str(cfg / "shortcuts.vdf"), exe, title, start, icon, tag, tags=tags, openvr=openvr)
    assert a.shortcuts_lost(["42"], ["com.x.y"]) == []


def test_steam_launch_result_reads_steams_log(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a.time, "sleep", lambda s: None)
    log = tmp_path / "console_log.txt"
    old = "[2026-10-04 12:00:00] SteamUI: WARNING: LaunchGameAction: launch error 9 3554078061 LaunchApp AppError_18\n"
    log.write_text(old)
    start = log.stat().st_size  # lines from before this launch don't count
    assert a.steam_launch_result(str(log), start, 3554078061, wait=0)["result"] == "silent"
    with open(log, "a") as f:  # GitHub #21/#30: Steam's "Game configuration unavailable"
        f.write("[2026-10-04 12:09:52] SteamUI: WARNING: LaunchGameAction: launch error 9 3554078061 LaunchApp "
                "AppError_18 \n")
    got = a.steam_launch_result(str(log), start, 3554078061, wait=0)
    assert got["result"] == "error" and got["code"] == 9 and got["detail"] == "LaunchApp AppError_18"
    log.write_text(old + "[2026-10-04 19:12:16] GameAction [AppID 2369265159, ActionID 4] : LaunchApp changed task "
                         "to CreatingProcess with \"\"\n")
    assert a.steam_launch_result(str(log), start, 2369265159, wait=0)["result"] == "started"


def test_steam_library_report(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    anchor.mkdir(parents=True)
    (anchor / "deployment.json").write_text(json.dumps({"package": "com.x.y", "appid": 1, "title": "X", "base": "b"}))
    cfg = tmp_path / ".local/share/Steam/userdata/42/config"
    cfg.mkdir(parents=True)
    exe, title, start, icon, tag, tags, openvr = a.shortcut_args("com.x.y")
    appid = a.upsert_shortcut(str(cfg / "shortcuts.vdf"), exe, title, start, icon, tag, tags=tags, openvr=openvr)
    a.upsert_shortcut(str(cfg / "shortcuts.vdf"), '"/usr/bin/other"', "Other", "/", "")
    logs = tmp_path / ".local/share/Steam/logs"
    logs.mkdir()
    (logs / "console_log.txt").write_text(f"noise\n[x] GameAction [AppID {appid}, ActionID 1] : LaunchApp changed "
                                          "task to Completed\n    path_shortcut: \"/a.desktop\"\n")
    monkeypatch.setattr(a, "run", lambda cmd, **k: SimpleNamespace(returncode=1, stdout=""))
    rep, console = a.steam_library_report()
    acc = rep["accounts"][0]
    assert acc["shortcuts"] == 2 and acc["exe_dupes"] == 0 and acc["shortcuts_vdf_written"]
    assert acc["frameport"] == [{"package": "com.x.y", "appid": appid, "title": "X", "start_dir": start,
                                 "openvr": acc["frameport"][0]["openvr"], "options": acc["frameport"][0]["options"]}]
    assert f"[AppID {appid}" in console and "noise" not in console and "path_shortcut" not in console


def test_steam_launch_result_app_error(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a.time, "sleep", lambda s: None)
    log = tmp_path / "console_log.txt"
    log.write_text('[x] GameAction [AppID 3554078061, ActionID 3] : LaunchApp changed task to RequestingLicense\n'
                   '[x] GameAction [AppID 3554078061, ActionID 3] : LaunchApp failed with AppError_9 with ""\n'
                   '[x] GameAction [AppID 1234, ActionID 1] : LaunchApp failed with AppError_18 with ""\n')
    got = a.steam_launch_result(str(log), 0, 3554078061, wait=0)  # GitHub #21: Steam didn't know the shortcut
    assert got["result"] == "error" and got["code"] == 9
    assert a.steam_launch_result(str(log), 0, 35540780, wait=0)["result"] == "silent"  # no prefix matches


def _fake_steam(a, monkeypatch, tmp_path, appid=2772269798):
    """Steam's devkit side: answers create/delete requests and writes/removes the shortcut like Steam does."""
    sent = []
    vdf = tmp_path / ".local/share/Steam/userdata/42/config/shortcuts.vdf"
    vdf.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".steam").mkdir(exist_ok=True)
    (tmp_path / ".steam/steam.token").write_text("tok")
    pipe = tmp_path / ".steam/steam.pipe"
    pipe.write_text("")

    real_open = open

    def fake_open(path, mode="r", *args, **kw):
        if str(path) == str(pipe) and "w" in mode:
            class P:
                def write(self, data):
                    line = data.decode().strip()
                    sent.append(line)
                    from urllib.parse import parse_qs, urlparse
                    url = urlparse(line.split(" ", 1)[1])
                    q = parse_qs(url.query)
                    cmd = url.path.rsplit("/", 1)[-1]
                    if cmd == "create-shortcut":
                        gid = q["gameid"][0]
                        a.upsert_shortcut(str(vdf), f'"{a.DEVKIT_GAMES}/{gid}/launch.sh"', f"Devkit Game: {gid}",
                                          f"{a.DEVKIT_GAMES}/{gid}", "")
                        root = a.vdf_decode(vdf.read_bytes())
                        for v in root["shortcuts"].values():
                            if v["Exe"].endswith(f'/{gid}/launch.sh"'):
                                v.update(appid=appid, Devkit=1, DevkitGameID=gid)
                        vdf.write_bytes(a.vdf_encode(root))
                    elif cmd == "delete-shortcut":
                        a.remove_shortcut(str(vdf), f'"{a.DEVKIT_GAMES}/{q["gameid"][0]}/launch.sh"')
                    real_open(q["response"][0], "w").write("ok")

                def __enter__(self):
                    return self

                def __exit__(self, *e):
                    return False
            return P()
        return real_open(path, mode, *args, **kw)
    monkeypatch.setattr(a, "open", fake_open, raising=False)
    monkeypatch.setattr("builtins.open", fake_open)
    return sent, vdf


def test_devkit_fallback_register_and_unregister(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.camouflaj.manta"
    (anchor / "artwork").mkdir(parents=True)
    (anchor / "launch.sh").write_text("#!/bin/sh\n")
    (anchor / "artwork/portrait.jpg").write_bytes(b"jpg")
    (anchor / "deployment.json").write_text(json.dumps({"package": "com.camouflaj.manta", "appid": 3554078061,
                                                        "title": "Batman: Arkham Shadow", "base": str(anchor)}))
    sent, vdf = _fake_steam(a, monkeypatch, tmp_path)
    monkeypatch.setattr(a.time, "sleep", lambda s: None)
    appid = a.devkit_register("com.camouflaj.manta")
    assert appid == 2772269798 and a.devkit_gameid("com.camouflaj.manta") == "Batman_Arkham_Shadow"
    assert "create-shortcut" in sent[0] and "gameid=Batman_Arkham_Shadow" in sent[0]  # no spaces in the id
    dk = tmp_path / "devkit-game"
    assert json.loads((dk / "Batman_Arkham_Shadow-argv.json").read_text()) == ["launch.sh"]
    assert os.readlink(dk / "Batman_Arkham_Shadow/launch.sh") == str(anchor / "launch.sh")
    grid = vdf.parent / "grid"
    assert (grid / "2772269798p.jpg").exists()  # its art under Steam's new appid
    assert a.devkit_register("com.camouflaj.manta") == appid  # registering again reuses the same id
    assert a.devkit_unregister("com.camouflaj.manta")
    assert not (dk / "Batman_Arkham_Shadow").exists() and not list(grid.iterdir())
    assert a.devkit_appid("Batman_Arkham_Shadow") is None and not a.devkit_unregister("com.camouflaj.manta")
    # Steam rejects ids that don't start with a letter (e.g. "4XVR_Video_Player")
    dep = json.loads((anchor / "deployment.json").read_text())
    (anchor / "deployment.json").write_text(json.dumps({**dep, "title": "4XVR Video-Player"}))
    a.devkit_register("com.camouflaj.manta")
    assert a.devkit_gameid("com.camouflaj.manta") == "Game_4XVR_Video_Player"


def test_launch_falls_back_to_devkit_entry(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    anchor.mkdir(parents=True)
    (anchor / "deployment.json").write_text(json.dumps({"package": "com.x.y", "appid": 111, "title": "X",
                                                        "base": str(anchor)}))
    monkeypatch.setattr(a, "run", lambda cmd, **k: SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(a, "shortcut_appid_for", lambda exe: 111)
    tried, registered = [], {}

    def launch(appid, wait=10):
        tried.append(appid)
        return {"result": "error", "code": 9} if appid == 111 else {"result": "started"}

    def register(pkg):
        registered["id"] = 222
        return 222
    monkeypatch.setattr(a, "steam_launch", launch)
    monkeypatch.setattr(a, "devkit_register", register)
    monkeypatch.setattr(a, "devkit_gameid", lambda pkg: "X" if registered else None)
    monkeypatch.setattr(a, "devkit_appid", lambda gid: registered.get("id"))
    got = a.cmd_launch({"package": "com.x.y"})
    assert tried == [111, 222] and got["via"] == "devkit" and got["steam"]["result"] == "started"
    assert got["first_try"]["code"] == 9
    got = a.cmd_launch({"package": "com.x.y"})  # next time the devkit entry is used right away
    assert tried[-1] == 222 and got["via"] == "devkit" and "first_try" not in got


@pytest.mark.skipif(sys.platform == "win32", reason="bash launcher")
def test_launcher_ends_game_when_its_parent_is_gone(monkeypatch, tmp_path):
    """GitHub #36: Steam stopping only its reaper left launch.sh + Lepton running; the watchdog stops them."""
    import signal
    import time

    a = load_agent(monkeypatch, tmp_path)
    base, anchor = tmp_path / "base", tmp_path / "anchor"
    (base / "lepton-app").mkdir(parents=True)
    anchor.mkdir()
    marker = tmp_path / "lepton-pid"
    lepton = tmp_path / "lepton"
    lepton.write_text(f"#!/bin/bash\necho $$ > {marker}\nexec sleep 300\n")
    lepton.chmod(0o755)
    a.write_launcher(str(anchor), str(base), "com.x.y", "X", 123, str(lepton), {})
    # the reaper stand-in: starts the launcher and waits; then it alone is killed
    parent = subprocess.Popen(["bash", "-c", f"bash {anchor / 'launch.sh'} & wait"], start_new_session=True)
    for _ in range(50):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.1)
    game = int(marker.read_text())
    os.kill(parent.pid, signal.SIGKILL)
    parent.wait()
    for _ in range(80):  # the watchdog checks every 2 s
        try:
            os.kill(game, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.killpg(game, signal.SIGKILL)
        pytest.fail("the game kept running after its parent was gone")


def test_upgrade_launchers_adds_watchdog(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    anchor.mkdir(parents=True)
    old = f"#!/bin/bash\nfix_perms\n{a.OLD_WATCHDOG}\nexport SteamAppId=1\n"
    (anchor / "launch.sh").write_text(old)
    (anchor / "launch.sh").chmod(0o755)
    assert a.upgrade_launchers() == ["com.x.y"]
    text = (anchor / "launch.sh").read_text()
    assert "parent=$PPID" in text and a.OLD_WATCHDOG not in text and os.access(anchor / "launch.sh", os.X_OK)
    assert a.upgrade_launchers() == []  # once only


def test_dashboard_worker_hides_dashboard_after_first_frames(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a.time, "sleep", lambda s: None)
    log = tmp_path / "launch.log"
    log.write_text("Lepton starting\n")
    calls, visible = [], [True, True, False, True]

    def js(expr, timeout=5):
        calls.append(expr)
        if expr.endswith("IsDashboardVisible()"):
            if len(calls) == 1:  # the first frames arrive while it waits
                pass
            return visible.pop(0) if visible else False
        return None
    monkeypatch.setattr(a, "steam_js", js)
    clock = iter(range(0, 10000))
    monkeypatch.setattr(a.time, "time", lambda: next(clock))
    log.write_text("Lepton starting\nI FrameBridge: pacing: 72.0 fps\n")
    a.dashboard_worker(str(log), os.getpid(), wait_start=10, window=8, max_hides=3)
    assert calls.count("SteamClient.OpenVR.VROverlay.HideDashboard()") == 3  # at most max_hides times


def test_dashboard_worker_starts_at_the_first_submitted_frame(monkeypatch, tmp_path):
    """Steam's "Resume game" menu opens at the game's first frame (ITR2: 0.3 s after FrameBridge's "new layer:"); the
    first "pacing:" summary comes ~8 s later, after the player had pressed Resume. The log grows while it waits."""
    a = load_agent(monkeypatch, tmp_path)
    log = tmp_path / "launch.log"
    log.write_text("Lepton starting\nI FrameBridge: xrCreateSwapchain 2016x1728\nI FrameBridge: new la")
    pieces = ["yer: type=35 swapchain=0x0\n"]  # the marker split across two reads

    def sleep(s):
        if pieces:
            with open(log, "a") as f:
                f.write(pieces.pop(0))
    monkeypatch.setattr(a.time, "sleep", sleep)
    clock = iter(range(0, 10000))
    monkeypatch.setattr(a.time, "time", lambda: next(clock))
    calls = []
    monkeypatch.setattr(a, "steam_js", lambda expr, timeout=5: calls.append(expr) or expr.endswith("Visible()"))
    a.dashboard_worker(str(log), os.getpid(), wait_start=10, window=4)
    assert "SteamClient.OpenVR.VROverlay.HideDashboard()" in calls


def test_dashboard_worker_leaves_a_dashboard_the_player_opened(monkeypatch, tmp_path):
    """ITR2 (agent 52): the player's controller button opened the dashboard and the worker closed it 60 ms later; the
    game had paused for it and stayed paused. After a toggle_dashboard_action the worker must stop for good."""
    a = load_agent(monkeypatch, tmp_path)
    log, ui = tmp_path / "launch.log", tmp_path / "vrwebhelper_systemui.txt"
    log.write_text("I FrameBridge: new layer: type=35\n")
    ui.write_text("| [Dashboard] [ToggleDashboard] toggle_dashboard_action (an earlier session)\n")
    visible = iter([True, False, False, True, True])
    calls = []

    def js(expr, timeout=5):
        calls.append(expr)
        return next(visible, True) if expr.endswith("Visible()") else None
    monkeypatch.setattr(a, "steam_js", js)
    steps = iter(range(10000))

    def sleep(s):
        if next(steps) == 2:  # the player presses the dashboard button
            with open(ui, "a") as f:
                f.write("| [Dashboard] [ToggleDashboard] toggle_dashboard_action bSourceIsVRLinkRemote false\n")
    monkeypatch.setattr(a.time, "sleep", sleep)
    clock = iter(range(0, 10000))
    monkeypatch.setattr(a.time, "time", lambda: next(clock))
    a.dashboard_worker(str(log), os.getpid(), wait_start=10, window=50, ui_log=str(ui))
    assert calls.count("SteamClient.OpenVR.VROverlay.HideDashboard()") == 1  # Steam's start-up menu only


def test_dashboard_worker_waits_for_vr_frames(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a.time, "sleep", lambda s: None)
    clock = iter(range(0, 10000))
    monkeypatch.setattr(a.time, "time", lambda: next(clock))
    monkeypatch.setattr(a, "steam_js", lambda *x, **k: pytest.fail("no frames yet: must not touch Steam"))
    log = tmp_path / "launch.log"
    log.write_text("2D app, no FrameBridge\n")
    a.dashboard_worker(str(log), os.getpid(), wait_start=5)


def test_upgrade_launchers_adds_dashboard_helper(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    anchor.mkdir(parents=True)
    (anchor / "launch.sh").write_text(f'{a.OLD_WATCHDOG}\nsetsid lepton start &\nchild=$!\nwait "$child"\n')
    assert a.upgrade_launchers() == ["com.x.y"]
    text = (anchor / "launch.sh").read_text()
    assert "_dashboard_worker" in text and text.index("_dashboard_worker") < text.index('wait "$child"')


def test_linux_app_finalize_launcher_and_libraries(monkeypatch, tmp_path):
    """GitHub #31: an arm64 Linux app (here: a copy of a host ELF) goes to <base>/app with a launcher; the library
    check runs on the program, uninstall removes it."""
    import shutil

    a = load_agent(monkeypatch, tmp_path)
    prep = a.cmd_prepare_linux({"package": "linux.true", "title": "True"})
    app_in = os.path.join(prep["incoming"], "app", "bin")
    os.makedirs(app_in)
    shutil.copy("/bin/true", os.path.join(app_in, "true"))
    size = os.path.getsize(os.path.join(app_in, "true"))
    res = a.cmd_finalize_linux({"package": "linux.true", "title": "True", "exe": "bin/true",
                                "manifests": {"app": {"bin/true": size}}, "openxr": False})
    assert res["missing_libraries"] == [] and res["moved_files"] == 1
    dep = a.deployment("linux.true")
    assert dep["kind"] == "linux" and dep["vr"] is False
    text = open(os.path.join(prep["anchor"], "launch.sh")).read()
    assert os.path.join(prep["base"], "app", "bin", "true") in text and "kill -0 $parent" in text
    assert a.shortcut_args("linux.true")[4] == "Linux app on Frame" and a.shortcut_args("linux.true")[6] is False
    listed = [g for g in a.cmd_list_installed({})["games"] if g["package"] == "linux.true"]
    assert listed and listed[0]["apk_present"]
    monkeypatch.setattr(a, "pcvr_pids", lambda base: [])
    assert a.cmd_uninstall({"package": "linux.true", "keep_data": False})["removed"]
    assert not os.path.exists(os.path.join(prep["base"], "app"))
    with pytest.raises(a.AgentError):
        a.cmd_finalize_linux({"package": "linux.true", "title": "True", "exe": "../etc/passwd"})


def test_appimage_programs_and_missing_libraries(monkeypatch, tmp_path):
    import shutil

    a = load_agent(monkeypatch, tmp_path)
    root = tmp_path / "squashfs-root"
    (root / "usr/bin").mkdir(parents=True)
    shutil.copy("/bin/true", root / "usr/bin/tool")
    (root / "AppRun").write_text("#!/bin/sh\n")
    (root / "AppRun").chmod(0o755)
    (root / "libfoo.so").write_bytes(b"\x7fELF")
    assert a.appimage_programs(str(root)) == [str(root / "usr/bin/tool")]  # scripts and libraries skipped
    assert a.missing_libraries(str(root), [str(root / "usr/bin/tool")]) == []


def test_power_schedules_systemctl_in_a_user_timer(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(a, "run", lambda cmd, **k: calls.append(cmd) or SimpleNamespace(returncode=0, stdout="",
                                                                                         stderr=""))
    monkeypatch.setattr(a, "game_running", lambda: False)
    assert a.cmd_power({"action": "restart"}) == {"action": "restart", "in_seconds": 3}
    assert calls[-1][:2] == ["systemd-run", "--user"] and calls[-1][-2:] == ["systemctl", "reboot"]
    assert "--on-active=3" in calls[-1]
    monkeypatch.setattr(a, "game_running", lambda: True)
    with pytest.raises(a.AgentError, match="game is running"):
        a.cmd_power({"action": "sleep"})
    assert a.cmd_power({"action": "sleep", "force": True})["action"] == "sleep"
    with pytest.raises(a.AgentError):
        a.cmd_power({"action": "format"})


def test_incomplete_deployment_records_dont_break_listing(monkeypatch, tmp_path):
    """GitHub #40: a deployment.json without "base" made every connection fail with KeyError: 'base'."""
    a = load_agent(monkeypatch, tmp_path)
    good = tmp_path / "Applications/quest-frame/com.x.ok"
    good.mkdir(parents=True)
    (good / "deployment.json").write_text(json.dumps({"package": "com.x.ok", "appid": 5}))  # no base / title
    junk = tmp_path / "Applications/quest-frame/other"
    junk.mkdir(parents=True)
    (junk / "deployment.json").write_text(json.dumps({"name": "something else"}))
    games = a.cmd_list_installed({})["games"]
    assert [g["package"] for g in games] == ["com.x.ok"] and games[0]["base"] == str(good)
    assert a.deployment("com.x.ok")["base"] == str(good)


def test_proton_defaults_to_stable(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    stable = {"name": "proton_11-arm64", "display_name": "Proton 11.0-2 (ARM64)", "aliases": "proton-stable-arm64,"
              "proton-stable", "experimental": False, "installed": True, "require_installed": True}
    exp = {"name": "proton-experimental-arm64", "display_name": "Proton Experimental (ARM64)",
           "aliases": "proton-experimental", "experimental": True, "installed": False, "require_installed": True}
    assert a.pick_proton([stable, exp])["name"] == "proton_11-arm64"
    assert a.pick_proton([exp, stable])["name"] == "proton_11-arm64"  # whatever the order
    assert a.pick_proton([stable, exp], "proton-experimental")["name"] == "proton-experimental-arm64"  # by alias
    assert a.pick_proton([exp])["name"] == "proton-experimental-arm64"  # only Experimental offered


def test_launch_test_waits_for_a_first_boot(monkeypatch, tmp_path):
    """The first start after an APK change: Lepton says "is not a running context" while it waits, then boots."""
    a = load_agent(monkeypatch, tmp_path)
    first = "Waiting for boot...\nERROR: 'steamlaunch-1' is not a running context, use 'lepton ps'\n"
    assert not a.not_started(first, 5)  # still within the grace period
    assert a.not_started(first, 31)  # no boot: the container really failed
    assert not a.not_started(first + "Boot complete!\nInstalling game.apk...\n", 120)
    assert not a.not_started("Boot complete!\n", 120)


def test_devkit_appid_from_steams_console_log(monkeypatch, tmp_path):
    """GitHub #42: Steam added the devkit entry live but never saved shortcuts.vdf; its console log names the appid."""
    a = load_agent(monkeypatch, tmp_path)
    logs = Path(a.STEAM) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    exe = f"{a.DEVKIT_GAMES}/I_Am_Cat/launch.sh"
    why = "reason: k_unAppIdInvalid"
    (logs / "console_log.txt").write_text(
        f'[2026-10-05 14:45:37] sanitize shortcut app id "{exe}": replacing 0 with 3849978641, {why}\n'
        f'[2026-10-05 14:45:38] sanitize shortcut app id "{a.DEVKIT_GAMES}/Other/launch.sh": replacing 0 with 5\n'
        f'[2026-10-05 15:29:14] sanitize shortcut app id "{exe}": replacing 0 with 3849978642, {why}\n')
    assert a.devkit_appid("I_Am_Cat") == 3849978642  # nothing in shortcuts.vdf: the newest log line
    assert a.devkit_appid("Missing") is None


def test_devkit_appid_ignores_the_previous_steam_session(monkeypatch, tmp_path):
    """Live devkit entries die with Steam (GitHub #41): an appid from console_log.previous.txt is stale."""
    a = load_agent(monkeypatch, tmp_path)
    logs = Path(a.STEAM) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    exe = f"{a.DEVKIT_GAMES}/Batman/launch.sh"
    (logs / "console_log.previous.txt").write_text(f'sanitize shortcut app id "{exe}": replacing 0 with 77, x\n')
    (logs / "console_log.txt").write_text("Steam started\n")
    assert a.devkit_appid("Batman") is None


def test_launch_registers_a_lost_devkit_entry_again(monkeypatch, tmp_path):
    """After a Steam restart Steam forgot the (never saved) devkit entry: Play gets AppError_9 and registers it
    again."""
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "check_pkg", lambda p: p)
    monkeypatch.setattr(a, "deployment", lambda p: {"appid": 111, "title": "Batman"})
    monkeypatch.setattr(a, "run", lambda *x, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(a, "devkit_gameid", lambda p: "Batman")
    monkeypatch.setattr(a, "devkit_appid", lambda g: 222)  # the stale entry from before the restart
    registered = []
    monkeypatch.setattr(a, "devkit_register", lambda p: registered.append(p) or 333)
    launches = []

    def launch(appid, wait=10):
        launches.append(appid)
        return {"result": "error", "code": 9} if appid == 222 else {"result": "started"}
    monkeypatch.setattr(a, "steam_launch", launch)
    out = a.cmd_launch({"package": "com.camouflaj.manta"})
    assert registered == ["com.camouflaj.manta"] and launches == [222, 333]
    assert out["via"] == "devkit" and out["steam"]["result"] == "started"


@pytest.mark.skipif(sys.platform == "win32" or not __import__("shutil").which("flock"), reason="bash + flock")
def test_second_launch_while_starting_is_ignored(monkeypatch, tmp_path):
    """Play pressed again while Lepton still boots made the second Lepton stop the first one's container (both died:
    Vader Immortal, BattleSisters). The launcher holds a lock for the game's lifetime; a second launch leaves it."""
    import time

    a = load_agent(monkeypatch, tmp_path)
    base = tmp_path / "game"
    (base / "lepton-app").mkdir(parents=True)
    lepton = tmp_path / "lepton"
    lepton.write_text(f"#!/bin/bash\necho started >>{tmp_path}/starts\nsleep ${{FAKE_RUN:-30}}\n")
    lepton.chmod(0o755)
    text = a.LAUNCH_SH.format(title="T", pkg="com.x.y", base_q=str(base), appid=1, lepton_q=str(lepton), extra_env="",
                              watchdog=a.WATCHDOG, dashboard="true", logcat="true", single=a.SINGLE_LINE,
                              plays_start="true", plays_end="true", video_codec="")
    launcher = tmp_path / "launch.sh"
    launcher.write_text(text)
    launcher.chmod(0o755)
    first = subprocess.Popen([str(launcher)], start_new_session=True)
    try:
        for _ in range(100):
            if (tmp_path / "starts").exists():
                break
            time.sleep(0.05)
        second = subprocess.run([str(launcher)], timeout=10)
        assert second.returncode == 0 and first.poll() is None  # ignored, the first keeps running
        assert (tmp_path / "starts").read_text().count("started") == 1
        assert "second launch ignored" in (base / "launch-dup.log").read_text()
    finally:
        os.killpg(first.pid, 15)
        first.wait(timeout=10)
    third = subprocess.run([str(launcher)], timeout=20, env={**os.environ, "FAKE_RUN": "0"})
    assert third.returncode == 0 and (tmp_path / "starts").read_text().count("started") == 2  # free again


def test_upgrade_launchers_adds_the_single_launch_lock(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    anchor.mkdir(parents=True)
    old = ('#!/bin/bash\napp_dir=/x\n[[ -d "$app_dir/lepton-app" ]] || { exit 1; }\nfix_perms\nparent=$PPID\n'
           '( while sleep 2; do fix_perms; if true; then kill -TERM $$; fi; done ) & permfix=$!\nexport SteamAppId=1\n')
    (anchor / "launch.sh").write_text(old)
    a.upgrade_launchers()
    text = (anchor / "launch.sh").read_text()
    assert text.index(".launch.lock") < text.index("fix_perms") and "done ) 9>&- & permfix=$!" in text
    assert text.count(".launch.lock") == 1
    a.upgrade_launchers()
    assert (anchor / "launch.sh").read_text().count(".launch.lock") == 1


def test_dashboard_worker_keeps_watching_for_two_minutes(monkeypatch, tmp_path):
    """Agent 59: Steam's menu came back after the first 30 s in headset sessions (The Boys, BONELAB)."""
    a = load_agent(monkeypatch, tmp_path)
    import inspect

    sig = inspect.signature(a.dashboard_worker)
    assert sig.parameters["window"].default == 120 and sig.parameters["max_hides"].default == 10


def test_linux_x86_apps_run_through_fex(monkeypatch, tmp_path):
    """x86_64 Linux apps: FEX is found in the compat list like Proton, and the launcher starts the program through
    the tool's command chain with STEAM_COMPAT_DATA_PATH set; no ldd check (it can't read x86 programs)."""
    import shutil

    a = load_agent(monkeypatch, tmp_path)
    apps = fake_steam_tools(a, tmp_path)
    tools = a.arm64_compat_tools()
    tools["fex"] = {"appid": 3127680, "display_name": "FEX-Emu", "from_oslist": "linux", "require_tool_appid": 4185400}
    a.arm64_compat_tools = lambda: tools
    assert [t["name"] for t in a.linux_x86_tools()] == ["fex"]  # the runtimes aren't translators
    st = a.cmd_proton_status({"kind": "linux_x86"})
    assert st["ready"] is None and st["suggested"]["name"] == "fex"
    assert a.cmd_proton_status({})["ready"]["name"] == "proton_11-arm64"  # Proton unchanged
    (apps / "common/FEX").mkdir(parents=True)
    (apps / "common/FEX/toolmanifest.vdf").write_text(
        '"manifest"\n{\n  "commandline" "/fex-compat-tool %verb%"\n  "require_tool_appid" "4185400"\n}\n')
    (apps / "appmanifest_3127680.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"3127680"\n\t"name"\t\t"FEX"\n\t"StateFlags"\t\t"4"\n'
        '\t"installdir"\t\t"FEX"\n}\n')
    assert a.cmd_proton_status({"kind": "linux_x86"})["ready"]["name"] == "fex"
    prep = a.cmd_prepare_linux({"package": "linux.thing", "title": "Thing"})
    shutil.copy("/bin/true", os.path.join(prep["incoming"], "app", "thing"))
    monkeypatch.setattr(a, "missing_libraries", lambda *x: pytest.fail("ldd on an x86 program"))
    res = a.cmd_finalize_linux({"package": "linux.thing", "title": "Thing", "exe": "thing", "x86_64": True})
    assert res["missing_libraries"] == [] and a.deployment("linux.thing")["x86_64"] is True
    text = open(os.path.join(prep["anchor"], "launch.sh")).read()
    line = next(x for x in text.splitlines() if "launch.log" in x and "_v2-entry-point" in x)
    assert line.index("_v2-entry-point") < line.index("fex-compat-tool") < line.index("app/thing")
    data = os.path.join(prep["base"], "compatdata")
    assert f"export STEAM_COMPAT_DATA_PATH={data}" in text and f"mkdir -p {data}" in text  # FEX exits without it
    assert text.index("STEAM_COMPAT_DATA_PATH") < text.index("fex-compat-tool")


def test_hmd_state_from_vrserver_log(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    logs = Path(a.STEAM) / "logs"
    logs.mkdir(parents=True)
    assert a.hmd_state() is None
    (logs / "vrserver.txt").write_text("x cv: [CCVTrackedHmdDriver] in StateActive\n"
                                       "y cv:  [CCVTrackedHmdDriver] in StateStandby \n")
    assert a.hmd_state() == "Standby"


def test_take_screenshot_waits_for_steams_file(monkeypatch, tmp_path):
    """take_screenshot asks SteamVR (vr_screenshot) and reports the shot Steam saved, or why none came."""
    a = load_agent(monkeypatch, tmp_path)
    shots = Path(a.STEAM) / "userdata" / "123" / "760" / "remote" / a.STEAMVR_APPID / "screenshots"
    shots.mkdir(parents=True)
    (shots / "20261008120000_1.jpg").write_bytes(b"old")

    def submitted():
        (shots / "20261008221922_1.jpg").write_bytes(b"new")  # Steam saves the flat shot...
        (shots / "20261008221922_1_vr.jpg").write_bytes(b"stereo")  # ...and the stereo one (not listed)
    monkeypatch.setattr(a, "vr_screenshot", submitted)
    r = a.cmd_take_screenshot({"wait": 2})
    assert r["taken"] and r["path"].endswith("20261008221922_1.jpg") and r["reason"] is None
    monkeypatch.setattr(a, "vr_screenshot", lambda: "capture")  # the headset sleeps: nothing captured
    r = a.cmd_take_screenshot({"wait": 0.5})
    assert r["taken"] is False and r["reason"] == "capture"


def test_vr_screenshot_without_steamvr(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "OPENVR_LIBS", (str(tmp_path / "missing.so"),))
    assert a.vr_screenshot() == "steamvr"


def test_game_logs_collect_unity_player_log(monkeypatch, tmp_path):
    """PC VR launch tests and diagnostics include Unity's Player.log (+ prev, crash error.log) from the Proton prefix
    (GitHub #105: SUPERHOT VR quit without a trace in the Proton logs)."""
    a = load_agent(monkeypatch, tmp_path)
    base = tmp_path / "rift.superhot_vr"
    data = base / "game/SUPERHOTVR_Data"
    data.mkdir(parents=True)
    (data / "app.info").write_text("SUPERHOT Team\nSUPERHOT VR")
    low = base / a.LOCAL_LOW
    mine = low / "SUPERHOT Team/SUPERHOT VR"
    mine.mkdir(parents=True)
    (mine / "Player.log").write_text("Mono path[0]\n" + "".join(f"line {i}\n" for i in range(3000))
                                     + "XR: OpenVR Error! OpenVR failed initialization with error code "
                                       "VRInitError_Init_HmdNotFound\n")
    (mine / "Player-prev.log").write_text("previous run\n")
    other = low / "Other/Game"
    other.mkdir(parents=True)
    (other / "Player.log").write_text("not this game\n")
    crash = base / a.LOCAL_APPDATA / "Temp/SUPERHOT Team/SUPERHOT VR/Crashes/Crash_2026-10-09_1"
    crash.mkdir(parents=True)
    (crash / "error.log").write_text("SUPERHOTVR.exe caused an Access Violation (0xc0000005)\n")
    parts = a.game_logs(str(base))
    text = "\n".join(parts)
    assert "===== unity log " in text and "Mono path[0]" in text and "VRInitError_Init_HmdNotFound" in text
    assert "lines left out" in text and "previous run" in text and "not this game" not in text
    assert "Access Violation" in text
    # a launch test only takes logs written since it started
    old = 1_000_000
    os.utime(mine / "Player-prev.log", (old, old))
    os.utime(crash / "error.log", (old, old))
    recent = "\n".join(a.game_logs(str(base), since=old + 10))
    assert "VRInitError" in recent and "previous run" not in recent and "Access Violation" not in recent
    # no app.info: every Player log in LocalLow; old Unity: <Name>_Data/output_log.txt
    (data / "app.info").unlink()
    (data / "output_log.txt").write_text("old unity\n")
    text = "\n".join(a.game_logs(str(base)))
    assert "not this game" in text and "old unity" in text


def test_logcat_keeper_reads_the_container_logcat_after_leptons_mirror_died(monkeypatch, tmp_path):
    """Lepton's logcat mirror died right after Vader Immortal started ("logcat: Unexpected EOF!"): launch.log stayed
    empty, so the dashboard was never closed. The keeper reads the container's logcat itself."""
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a.time, "sleep", lambda s: None)
    log = tmp_path / "launch.log"
    log.write_text("Boot complete!\nWaiting for app com.x.y to exit...\nlogcat: Unexpected EOF!\n")
    calls, alive = [], iter([True, True, False])
    monkeypatch.setattr(a.os, "kill", lambda pid, sig: None if next(alive) else (_ for _ in ()).throw(OSError()))

    class Proc:
        def __init__(self, argv, stdout, **kw):
            calls.append(argv)
            stdout.write(b"10-09 10:01:00.000 1 2 I FrameBridge: new layer: type=35\n")

        def poll(self):
            return None

        def terminate(self):
            calls.append("terminated")

    a.logcat_keeper(str(log), "123", os.getpid(), popen=Proc)
    assert calls[0][:4] == ["podman", "exec", "lepton-steamlaunch-123", "logcat"] and calls[-1] == "terminated"
    text = log.read_text()
    assert "reading lepton-steamlaunch-123's logcat again" in text and "new layer: type=35" in text


def test_logcat_keeper_does_nothing_while_leptons_mirror_works(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a.time, "sleep", lambda s: None)
    log = tmp_path / "launch.log"
    log.write_text("Waiting for app com.x.y to exit...\n10-09 I FrameBridge: pacing: 72 fps\n")
    alive = iter([True, True, False])
    monkeypatch.setattr(a.os, "kill", lambda pid, sig: None if next(alive) else (_ for _ in ()).throw(OSError()))
    a.logcat_keeper(str(log), "123", os.getpid(), popen=lambda *x, **k: pytest.fail("must not start logcat"))


def test_upgrade_launchers_adds_logcat_keeper(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    anchor.mkdir(parents=True)
    (anchor / "launch.sh").write_text(f'{a.OLD_WATCHDOG}\nsetsid lepton start &\nchild=$!\nwait "$child"\n')
    a.upgrade_launchers()
    text = (anchor / "launch.sh").read_text()
    assert "_logcat_keeper" in text and text.index("_logcat_keeper") < text.index('wait "$child"')


def test_launch_test_window_starts_when_the_app_starts(monkeypatch, tmp_path):
    """The first start after an APK change spends ~90 s booting Lepton and installing the app; a 45 s window counted
    from the launcher stopped the container mid-install and left a broken APK copy ("base.apk is not zip", VR HOT)."""
    a = load_agent(monkeypatch, tmp_path)
    base = tmp_path / "base"
    base.mkdir()
    log = base / "launch.log"
    log.write_text("Waiting for boot...\n")
    clock = {"t": 1000.0}
    monkeypatch.setattr(a.time, "time", lambda: clock["t"])

    def sleep(s):
        clock["t"] += s
        if clock["t"] >= 1090 and "Waiting for app" not in log.read_text():
            log.write_text("Waiting for boot...\nBoot complete!\nInstalling game.apk...\nSuccess\n"
                           "Waiting for app com.x.y to exit...\n")
    monkeypatch.setattr(a.time, "sleep", sleep)
    monkeypatch.setattr(a, "deployment", lambda pkg: {"base": str(base), "appid": 1})
    monkeypatch.setattr(a, "container_running", lambda appid: False)
    monkeypatch.setattr(a, "ensure_host_fixes", lambda: None)
    monkeypatch.setattr(a, "key_usage", lambda: None)
    monkeypatch.setattr(a, "run", lambda *x, **k: type("R", (), {"returncode": 0, "stderr": ""})())
    res = a.cmd_launch_test({"package": "com.x.y", "seconds": 45})
    assert res["state"] == "RUNNING" and res["elapsed"] >= 90 + 45


def test_rename_keeps_the_shortcut_appid(monkeypatch, tmp_path):
    """Renaming an installed game (agent v77) changes deployment.json's title and the shortcut's name; the shortcut
    keeps its appid (Play, grid art and the game's container depend on it)."""
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.y"
    anchor.mkdir(parents=True)
    exe = f'"{anchor}/launch.sh"'
    appid = a.shortcut_appid(exe, "com.x.y")
    (anchor / "deployment.json").write_text(json.dumps(
        {"package": "com.x.y", "appid": appid, "base": str(anchor), "title": "com.x.y", "kind": "quest"}))
    cfg = tmp_path / ".local/share/Steam/userdata/42/config"
    cfg.mkdir(parents=True)
    vdf = cfg / "shortcuts.vdf"
    assert a.upsert_shortcut(str(vdf), exe, "com.x.y", str(anchor)) == appid
    started = []
    monkeypatch.setattr(a, "cmd_shortcuts", lambda args: (started.append(args), {"started": True})[1])
    with pytest.raises(a.AgentError, match="empty"):
        a.cmd_rename({"package": "com.x.y", "title": " \n\t "})
    with pytest.raises(a.AgentError, match="not installed"):
        a.cmd_rename({"package": "com.other", "title": "X"})
    r = a.cmd_rename({"package": "com.x.y", "title": "  Vader Immortal:\nEpisode II  "})
    assert r["renamed"] and r["title"] == "Vader Immortal: Episode II" and r["appid"] == appid
    assert started == [{"packages": ["com.x.y"], "restart": True}]
    dep = json.loads((anchor / "deployment.json").read_text())
    assert dep["title"] == "Vader Immortal: Episode II" and dep["appid"] == appid
    # what the shortcuts worker then does with Steam closed: same appid, new name, one entry
    monkeypatch.setattr(a, "devkit_gameid", lambda pkg: None)
    result = {"added": [], "errors": []}
    a.update_library("42", ["com.x.y"], result)
    assert not result["errors"] and result["added"][0]["appid"] == appid
    entries = list(a.vdf_decode(vdf.read_bytes())["shortcuts"].values())
    assert len(entries) == 1 and entries[0]["appname"] == "Vader Immortal: Episode II"
    assert entries[0]["appid"] & 0xFFFFFFFF == appid
    # the same name again: nothing to do, no Steam restart
    started.clear()
    r = a.cmd_rename({"package": "com.x.y", "title": "Vader Immortal: Episode II"})
    assert not r["renamed"] and not started
    assert len(a.cmd_rename({"package": "com.x.y", "title": "x" * 300, "shortcuts": False})["title"]) == a.TITLE_MAX


def test_two_revive_games_keep_their_own_shortcuts(monkeypatch, tmp_path):
    """Every Revive game on a PC starts ReviveInjector.exe: shortcuts are told apart by the game in the launch
    options (one overwrote the other before); a changed injector flag still updates the same shortcut."""
    a = load_agent(monkeypatch, tmp_path)
    vdf = tmp_path / "shortcuts.vdf"
    exe = '"C:\\\\Revive\\\\ReviveInjector.exe"'
    one = a.upsert_shortcut(str(vdf), exe, "Game One", '"D:\\\\G1\\\\"', "", "Rift via Revive",
                            launch_options='/openxr "D:\\\\G1\\\\One.exe"')
    two = a.upsert_shortcut(str(vdf), exe, "Game Two", '"D:\\\\G2\\\\"', "", "Rift via Revive",
                            launch_options='"D:\\\\G2\\\\Two.exe"')
    assert one != two
    again = a.upsert_shortcut(str(vdf), exe, "Game One", '"D:\\\\G1\\\\"', "", "Rift via Revive",
                              launch_options='"D:\\\\G1\\\\One.exe"')
    assert again == one
    names = sorted(v["appname"] for v in a.vdf_decode(vdf.read_bytes())["shortcuts"].values())
    assert names == ["Game One", "Game Two"]
