"""Game language follows Steam's (#530), games without network (device.offline, Discussion #505) and an extra shared
Frame folder (device.extra_folder, Discussion #501): the agent's launcher lines and Podman wrapper, and the patches."""
import json
import os
import subprocess

import pytest
from test_steam_gamepad import LEPTON, NEEDS_POSIX, RUN, agent_module, anchor

from frameport.core.models import Recipe
from frameport.install.installer import install_context
from frameport.patches import base
from frameport.patches.settings import Language, extra_folder_value
from frameport.recommend import engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = """"Registry"
{
\t"HKLM" { "Software" { "Valve" { "Steam" { "language"\t\t"english" } } } }
\t"HKCU"
\t{
\t\t"Software"
\t\t{
\t\t\t"Valve"
\t\t\t{
\t\t\t\t"Steam"
\t\t\t\t{
\t\t\t\t\t"AutoLoginUser"\t\t"someone"
\t\t\t\t\t"Language"\t\t"%s"
\t\t\t\t\t"Apps" { "480" { "language" "french" } }
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
"""


def _env(patches):
    base.load_all()
    return install_context(Recipe(package="com.example.game", patches=patches)).env


# ------------------------------------------------------------------------------------------- language (#530)
@pytest.mark.parametrize("name,tag", [("german", "de-DE"), ("schinese", "zh-CN"), ("brazilian", "pt-BR"),
                                      ("latam", "es-MX"), ("koreana", "ko-KR"), ("english", ""), ("klingon", "")])
def test_steam_language_maps_to_android_tags(monkeypatch, tmp_path, name, tag):
    agent = agent_module(monkeypatch, tmp_path)
    reg = tmp_path / "registry.vdf"
    reg.write_text(REGISTRY % name)
    assert agent.steam_language([str(reg)]) == name  # HKCU's, not HKLM's or an app's
    assert agent.steam_locale([str(reg)]) == tag


def test_steam_language_missing(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    assert agent.steam_locale([str(tmp_path / "none.vdf")]) == ""
    (tmp_path / "cut.vdf").write_text('"Registry" { "HKCU" { "Software"')
    assert agent.steam_locale([str(tmp_path / "cut.vdf")]) == ""
    assert all(agent.LOCALE_TAG_RE.fullmatch(t) for t in agent.STEAM_LANGUAGES.values())


def test_patch_default_is_steams_language_and_english_is_a_choice():
    assert Language.CHOICES[0] == ("", "Steam's language") and ("en-US", "English (US)") in Language.CHOICES
    assert "LEPTON_GFXRECON_FP_LOCALE" not in _env({"device.language": {"tag": ""}})
    assert "LEPTON_GFXRECON_FP_LOCALE" not in _env({"device.language": {"tag": "steam"}})
    assert _env({"device.language": {"tag": "en-US"}})["LEPTON_GFXRECON_FP_LOCALE"] == "0\npersist.sys.locale=en-US"


@NEEDS_POSIX
@pytest.mark.parametrize("preset,steam,expected", [("", "german", "0\npersist.sys.locale=de-DE"),
                                                   ("0\npersist.sys.locale=en-US", "german",
                                                    "0\npersist.sys.locale=en-US"),
                                                   ("", "english", "")])
def test_locale_line_in_bash(tmp_path, preset, steam, expected):
    """The launcher line itself: Steam's language unless the recipe exported one; English changes nothing."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("locale_agent", os.path.join(ROOT, "agent/frameport_agent.py"))
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    (tmp_path / ".steam").mkdir()
    (tmp_path / ".steam/registry.vdf").write_text(REGISTRY % steam)
    line = agent.LOCALE_LINE.format(agent_q=agent.shlex.quote(os.path.join(ROOT, "agent/frameport_agent.py")))
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    if preset:
        env["LEPTON_GFXRECON_FP_LOCALE"] = preset
    script = f"set -euo pipefail\n{line}\nprintf '<%s>' \"${{LEPTON_GFXRECON_FP_LOCALE:-}}\""
    out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.endswith(f"<{expected}>")


def test_lepton_launchers_get_the_locale_line_once(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    directory = anchor(tmp_path, "com.game", LEPTON.format(lines=""))
    proton = anchor(tmp_path, "rift.game", "#!/bin/bash\nchild=''\nexec proton\n")
    assert "com.game" in agent.upgrade_launchers()
    text = (directory / "launch.sh").read_text()
    assert text.count(" _steam_locale ") == 1 and text.index("child=''") < text.index(" _steam_locale ")
    assert " _steam_locale " not in (proton / "launch.sh").read_text()
    assert agent.upgrade_launchers() == [] and agent.set_locale_line(text) == text


# ------------------------------------------------------------------------------------------- offline (#505)
def test_offline_patch_sets_its_variable():
    assert _env({"device.offline": {}})["FRAMEPORT_OFFLINE"] == "1"
    patch = base.get("device.offline")
    assert "online" in patch.description.lower() and patch.category == "device" and not patch.needs_vr


@NEEDS_POSIX
def test_offline_container_gets_no_network(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    env = {"FRAMEPORT_OFFLINE": "1", "SteamAppId": "123"}
    lepton = ["run", "--network", "slirp4netns", "--dns=1.1.1.1", "--add-host", "a:1.2.3.4", "--name",
              "lepton-steamlaunch-123", "--network-alias=x", "--rootfs", "/lepton/rootfs:O", "/init", "-p"]
    out = agent.podman_run_args(lepton, env, str(tmp_path / "sys"), str(tmp_path / "dev"))
    assert out == ["run", "--network=none", "--name", "lepton-steamlaunch-123", "--rootfs", "/lepton/rootfs:O",
                   "/init", "-p"]
    assert agent.podman_run_args(RUN, env, "/x", "/x") == ["run", "--network=none", *RUN[1:]]
    assert agent.podman_run_args(RUN, dict(env, SteamAppId="9"), "/x", "/x") == RUN  # another container
    assert agent.podman_run_args(RUN, dict(env, FRAMEPORT_OFFLINE=""), "/x", "/x") == RUN
    assert agent.podman_run_args(["ps", "--network", "x"], env, "/x", "/x") == ["ps", "--network", "x"]


def test_offline_launcher_puts_the_wrapper_on_path(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    plain = LEPTON.format(lines="export FRAMEPORT_OFFLINE=1\n")
    directory = anchor(tmp_path, "com.game", plain)
    old = anchor(tmp_path, "com.old", LEPTON.format(lines="export FRAMEPORT_FEATURES=a.b\n"
                                                     + agent.OLD_FEATURES_LINES[0] + "\n"))
    agent.upgrade_launchers()
    assert (directory / "launch.sh").read_text().count(agent.FEATURES_LINE) == 1
    text = (old / "launch.sh").read_text()
    assert text.count(agent.FEATURES_LINE) == 1 and agent.OLD_FEATURES_LINES[0] not in text
    source = open(os.path.join(ROOT, "agent/frameport_agent.py")).read()
    assert 'get("FRAMEPORT_OFFLINE", "")) == "1"  # device.offline' in source


# ------------------------------------------------------------------------------------------- extra folder (#501)
@pytest.mark.parametrize("value,expected", [("ROMs", "ROMs"), ("~/Games/ROMs/", "Games/ROMs"),
                                            ("/home/steamos/ROMs", "ROMs"), ("Games\\ROMs", "Games/ROMs")])
def test_folder_values(value, expected):
    assert extra_folder_value(value) == expected
    assert _env({"device.extra_folder": {"folder": value}})["FRAMEPORT_FOLDER"] == expected


@pytest.mark.parametrize("value", ["../etc", ".ssh", "a/.config", "/etc", "a:b", "x\ny", "Download", "Android",
                                   "~/", "/home/steamos"])
def test_folder_values_refused(value):
    with pytest.raises(ValueError):
        extra_folder_value(value)
    warn = engine.warnings(Recipe(package="com.x", patches={"device.extra_folder": {"folder": value}}))
    assert value.strip("~/") == "" or warn  # the game page says so before an install


def test_empty_folder_sets_nothing():
    assert "FRAMEPORT_FOLDER" not in _env({"device.extra_folder": {"folder": ""}})


@NEEDS_POSIX
def test_extra_folder_links_into_sdcard(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    home, app = tmp_path / "home", tmp_path / "base"
    (home / "Games/ROMs").mkdir(parents=True)
    (app / "lepton-data/external/ROMs").mkdir(parents=True)
    (app / "lepton-data/external/ROMs/own.sav").write_text("x")  # the game's own folder of that name is kept
    mount = agent.extra_folder(str(app), "com.x", "Games/ROMs", home=str(home))
    real = os.path.realpath(home / "Games/ROMs")
    assert mount == real and os.readlink(app / "lepton-data/external/ROMs") == real
    assert (app / "lepton-data/external/ROMs.frameport-own/own.sav").exists()
    assert json.loads((app / "extra-folder.json").read_text())["link"] == "ROMs"
    # the setting goes: the link goes and the game's own folder comes back
    agent.extra_folder_cleanup(str(app), None, home=str(home))
    assert (app / "lepton-data/external/ROMs/own.sav").exists() and not (app / "extra-folder.json").exists()


@NEEDS_POSIX
def test_extra_folder_with_spaces_goes_through_a_link(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    home, app = tmp_path / "home", tmp_path / "base"
    (home / "My ROMs").mkdir(parents=True)
    mount = agent.extra_folder(str(app), "com.x", "My ROMs", home=str(home), links=str(tmp_path / "links"))
    assert mount == str(tmp_path / "links/com.x.folder") and " " not in os.path.basename(mount)
    assert os.path.realpath(mount) == os.path.realpath(home / "My ROMs")
    assert os.readlink(app / "lepton-data/external/My ROMs") == mount


@NEEDS_POSIX
def test_extra_folder_refuses_escapes(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    home, app, outside = tmp_path / "home", tmp_path / "base", tmp_path / "outside"
    (home / ".ssh").mkdir(parents=True)
    outside.mkdir()
    (home / "out").symlink_to(outside)
    (home / "keys").symlink_to(home / ".ssh")
    (home / "file").write_text("x")
    for value in ("out", "keys", "file", "missing", ".ssh", "../outside", "/etc", "Documents"):
        (home / "Documents").mkdir(exist_ok=True)
        assert agent.extra_folder(str(app), "com.x", value, home=str(home)) == "", value
    assert not (app / "lepton-data/external").exists() or not os.listdir(app / "lepton-data/external")


@NEEDS_POSIX
def test_folder_lines_in_bash(monkeypatch, tmp_path):
    """The launcher line: the folder joins STEAM_COMPAT_LIBRARY_PATHS; a bad one leaves it unchanged."""
    agent = agent_module(monkeypatch, tmp_path)
    (tmp_path / "ROMs").mkdir()
    (tmp_path / "base").mkdir()
    line = agent.FOLDER_LINES.format(agent_q=agent.shlex.quote(os.path.join(ROOT, "agent/frameport_agent.py")),
                                     pkg_q="com.x")
    script = f"set -euo pipefail\napp_dir={tmp_path}/base\nexport STEAM_COMPAT_LIBRARY_PATHS=$app_dir\n{line}\n" \
             "printf '<%s>' \"$STEAM_COMPAT_LIBRARY_PATHS\""
    for folder, extra in (("ROMs", f":{os.path.realpath(tmp_path / 'ROMs')}"), ("nope", "")):
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                             env={"HOME": str(tmp_path), "PATH": os.environ["PATH"], "FRAMEPORT_FOLDER": folder})
        assert out.returncode == 0, out.stderr
        assert out.stdout == f"<{tmp_path}/base{extra}>"
    assert "isn't a folder" in (tmp_path / "base/extra-folder.log").read_text()


def test_folder_lines_follow_the_env(monkeypatch, tmp_path):
    agent = agent_module(monkeypatch, tmp_path)
    directory = tmp_path / "apps/game"
    directory.mkdir(parents=True)
    agent.write_launcher(str(directory), str(tmp_path / "base"), "com.example.game", "Game", 123, "/lepton/lepton",
                         {"FRAMEPORT_FOLDER": "ROMs"})
    text = (directory / "launch.sh").read_text()
    assert text.count(" _extra_folder ") == 1 and "export FRAMEPORT_FOLDER=ROMs\n" in text
    assert text.index("export FRAMEPORT_FOLDER") < text.index(" _extra_folder ") < text.index("setsid ")
    agent.write_launcher(str(directory), str(tmp_path / "base"), "com.example.game", "Game", 123, "/lepton/lepton", {})
    assert " _extra_folder " not in (directory / "launch.sh").read_text()
