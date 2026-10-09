"""Screenshots tab: the agent's listing (screenshots.vdf + folders, games matched by play session), its delete path
checks, the launchers' play log, and the PC-side cache/download helpers."""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time

import pytest
from test_agent import load_agent

VDF = """"screenshots"
{
\t"250820"
\t{
\t\t"0"
\t\t{
\t\t\t"type"\t\t"1"
\t\t\t"filename"\t\t"250820/screenshots/20260928184431_1.jpg"
\t\t\t"thumbnail"\t\t"250820/screenshots/thumbnails/20260928184431_1.jpg"
\t\t\t"imported"\t\t"1"
\t\t\t"externalfilename"\t\t"/run/user/1000/Screenshot_Mon_Sep_28_18-44-31_2026.png"
\t\t\t"width"\t\t"1920"
\t\t\t"height"\t\t"1080"
\t\t\t"gameid"\t\t"250820"
\t\t\t"creation"\t\t"1790635471"
\t\t}
\t\t"1"
\t\t{
\t\t\t"filename"\t\t"250820/screenshots/20260927100000_1.jpg"
\t\t\t"creation"\t\t"1790500000"
\t\t}
\t\t"2"
\t\t{
\t\t\t"filename"\t\t"250820/screenshots/20260101000000_1.jpg"
\t\t\t"creation"\t\t"1700000000"
\t\t}
\t\t"3"
\t\t{
\t\t\t"filename"\t\t"../../../../config/escape.jpg"
\t\t}
\t}
}
"""


def make_tree(a, home):
    user = home / ".local/share/Steam/userdata/123"
    shots = user / "760/remote/250820/screenshots"
    (shots / "thumbnails").mkdir(parents=True)
    (user / "760/screenshots.vdf").write_text(VDF)
    for name in ("20260928184431_1.jpg", "20260927100000_1.jpg"):
        (shots / name).write_bytes(b"\xff\xd8" + b"x" * 100)
    (shots / "thumbnails/20260928184431_1.jpg").write_bytes(b"\xff\xd8thumb")
    # 20260101000000_1.jpg is listed but deleted; one file isn't in the vdf yet (Steam writes it at exit)
    (shots / "20260929120000_1.jpg").write_bytes(b"\xff\xd8" + b"y" * 50)
    (user / "config").mkdir()
    # a FramePort game installed with its own Steam shortcut + plays.log sessions
    anchor = home / "Applications/quest-frame/com.x.game"
    anchor.mkdir(parents=True)
    (anchor / "deployment.json").write_text(json.dumps({"package": "com.x.game", "title": "X Game",
                                                        "appid": 3000000001, "base": str(anchor)}))
    (anchor / "plays.log").write_text("start 1790635000\nend 1790636000\nstart 1790499000\n")
    other = home / "Applications/quest-frame/com.y.other"
    other.mkdir(parents=True)
    (other / "deployment.json").write_text(json.dumps({"package": "com.y.other", "title": "Other", "appid": 5}))
    (other / "plays.log").write_text("start 1790499500\nend 1790500100\n")
    # a screenshot filed under a FramePort game's own shortcut appid
    own = user / "760/remote/3000000001/screenshots"
    own.mkdir(parents=True)
    (own / "20260930080000_1.jpg").write_bytes(b"\xff\xd8z")
    return user


def test_kv_parse(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    d = a.kv_parse(VDF)
    assert d["screenshots"]["250820"]["0"]["creation"] == "1790635471"
    assert a.kv_parse('"a" { "b" "x\\"y" // c\n "c" { "d" "1"')["a"] == {"b": 'x"y', "c": {"d": "1"}}  # cut off
    assert a.kv_parse("") == {}


def test_list_screenshots_attributes_by_session(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    make_tree(a, tmp_path)
    r = a.cmd_list_screenshots({})
    names = [os.path.basename(s["path"]) for s in r["shots"]]
    assert names == ["20260930080000_1.jpg", "20260929120000_1.jpg", "20260928184431_1.jpg",
                     "20260927100000_1.jpg"]  # newest first; the deleted and the escaping entry are left out
    by = {os.path.basename(s["path"]): s for s in r["shots"]}
    # inside X Game's closed session
    assert by["20260928184431_1.jpg"]["package"] == "com.x.game"
    assert by["20260928184431_1.jpg"]["title"] == "X Game" and by["20260928184431_1.jpg"]["width"] == 1920
    assert by["20260928184431_1.jpg"]["thumb"].endswith("thumbnails/20260928184431_1.jpg")
    # X Game's open session (no end) was ended by Other's later start, so this one is Other's
    assert by["20260927100000_1.jpg"]["package"] == "com.y.other"
    # not in the vdf: timed by its name, outside every session -> SteamVR
    assert by["20260929120000_1.jpg"]["package"] is None and by["20260929120000_1.jpg"]["title"] == "SteamVR"
    assert by["20260929120000_1.jpg"]["time"] == int(time.mktime((2026, 9, 29, 12, 0, 0, 0, 0, -1)))
    assert by["20260929120000_1.jpg"]["thumb"] is None
    # filed under the game's own shortcut
    assert by["20260930080000_1.jpg"]["package"] == "com.x.game" and by["20260930080000_1.jpg"]["account"] == "123"
    assert r["total"] == 4
    assert {(g["package"], g["count"]) for g in r["games"]} == {("com.x.game", 2), ("com.y.other", 1), (None, 1)}
    page = a.cmd_list_screenshots({"package": "com.x.game", "offset": 1, "limit": 5})
    assert page["total"] == 2 and [s["path"] for s in page["shots"]] == [by["20260928184431_1.jpg"]["path"]]
    assert [s["title"] for s in a.cmd_list_screenshots({"package": ""})["shots"]] == ["SteamVR"]


def test_list_screenshots_without_steam(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    assert a.cmd_list_screenshots({}) == {"shots": [], "total": 0, "games": []}


def test_open_session_ends_after_a_while(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "Applications/quest-frame/com.x.game"
    anchor.mkdir(parents=True)
    (anchor / "plays.log").write_text("start 1000\ngarbage\nend x\n")
    sessions = a.play_sessions()
    assert sessions == [(1000, 1000 + a.OPEN_SESSION, "com.x.game")]
    assert a.session_game(1500, sessions) == "com.x.game" and a.session_game(999, sessions) is None
    assert a.session_game(1000 + a.OPEN_SESSION + 60, sessions) is None


def test_delete_screenshots_only_in_screenshot_folders(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    user = make_tree(a, tmp_path)
    shots = user / "760/remote/250820/screenshots"
    target = str(shots / "20260928184431_1.jpg")
    bad = ["/etc/passwd", str(user / "760/screenshots.vdf"), str(shots / "thumbnails/20260928184431_1.jpg"),
           str(shots / "../screenshots/20260928184431_1.jpg"), str(shots / "notashot.jpg"), "relative.jpg",
           str(shots / "20260101000000_1.jpg"), 42]
    (shots / "notashot.jpg").write_bytes(b"x")
    for p in bad:
        with pytest.raises(a.AgentError):
            a.cmd_delete_screenshots({"paths": [target, p]})
    assert os.path.exists(target)  # nothing deleted when any path is refused
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "20260101000000_1.jpg").write_bytes(b"s")
    link_dir = user / "760/remote/999/screenshots"
    link_dir.parent.mkdir(parents=True)
    link_dir.symlink_to(outside)
    with pytest.raises(a.AgentError):
        a.cmd_delete_screenshots({"paths": [str(link_dir / "20260101000000_1.jpg")]})
    assert (outside / "20260101000000_1.jpg").exists()
    (shots / "20260928184431_1_vr.jpg").write_bytes(b"stereo")  # Steam's stereo copy of a VR screenshot
    r = a.cmd_delete_screenshots({"paths": [target]})
    assert r == {"deleted": [target]}
    assert not os.path.exists(target) and not (shots / "thumbnails/20260928184431_1.jpg").exists()
    assert not (shots / "20260928184431_1_vr.jpg").exists()
    assert "20260928184431_1.jpg" in (user / "760/screenshots.vdf").read_text()  # Steam's index is left alone
    assert target not in [s["path"] for s in a.cmd_list_screenshots({})["shots"]]


@pytest.mark.skipif(sys.platform == "win32", reason="bash launcher")
def test_launcher_logs_play_sessions(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    base, anchor = tmp_path / "base", tmp_path / "anchor dir"
    (base / "lepton-app").mkdir(parents=True)
    anchor.mkdir()
    lepton = tmp_path / "lepton"
    lepton.write_text("#!/bin/bash\nexit 0\n")
    lepton.chmod(0o755)
    a.write_launcher(str(anchor), str(base), "com.x.y", "X", 123, str(lepton), {"FRAMEPORT_KEEP_DASHBOARD": "1"})
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    (tmp_path / "podman").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "podman").chmod(0o755)
    subprocess.run(["bash", str(anchor / "launch.sh")], env=env, timeout=30, capture_output=True)
    lines = (anchor / "plays.log").read_text().split("\n")
    assert lines[0].startswith("start ") and lines[1].startswith("end ") and lines[2] == ""
    assert int(lines[0].split()[1]) <= int(lines[1].split()[1])


def test_upgrade_launchers_adds_play_log(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    root = tmp_path / "Applications/quest-frame"
    quest, pcvr = root / "com.x.y", root / "rift.z"
    quest.mkdir(parents=True)
    pcvr.mkdir(parents=True)
    a.write_launcher(str(quest), "/b", "com.x.y", "X", 1, "/lepton", {})
    old = "\n".join(line for line in (quest / "launch.sh").read_text().split("\n") if "plays.log" not in line)
    (quest / "launch.sh").write_text(old)
    (pcvr / "launch.sh").write_text('#!/bin/bash\necho hi >"$base/launch.log"\nexec wine game.exe\n')
    assert sorted(a.upgrade_launchers()) == ["com.x.y", "rift.z"]
    q = (quest / "launch.sh").read_text()
    assert q.count("plays.log") == 2 and q.index('"start $(date') < q.index("\nsetsid ")
    assert '    echo "end $(date +%s)"' in q
    assert subprocess.run(["bash", "-n", str(quest / "launch.sh")]).returncode == 0
    p = (pcvr / "launch.sh").read_text()
    assert p.count("plays.log") == 1 and p.index("plays.log") < p.index("\nexec ")
    assert a.upgrade_launchers() == []


# ------------------------------------------------------------------ PC side
class FakeSFTP:
    def __init__(self):
        self.gets = []

    def get(self, remote, local, callback=None):
        self.gets.append(remote)
        shutil.copyfile(remote, local)
        if callback:
            callback(os.path.getsize(remote), os.path.getsize(remote))


class FakeFrame:
    def __init__(self, agent_mod):
        self.sftp = FakeSFTP()
        self.a = agent_mod
        self.target = type("T", (), {"host": "10.0.0.5", "name": ""})()

    def agent(self, command, **args):
        return self.a.COMMANDS[command](args)


@pytest.fixture
def pc(monkeypatch, tmp_path):
    from frameport.install import installer

    monkeypatch.setattr(installer, "transfer_link", lambda f, r, n: contextlib.nullcontext(f))
    frame_home = tmp_path / "frame"
    frame_home.mkdir()
    a = load_agent(monkeypatch, frame_home)
    make_tree(a, frame_home)
    monkeypatch.setenv("FRAMEPORT_HOME", str(tmp_path / "pc"))
    return FakeFrame(a)


def test_pc_cache_and_download(pc, tmp_path):
    from frameport.core.paths import user_data_dir
    from frameport.install import screenshots

    r = screenshots.list_shots(pc, package="com.x.game")
    assert r["total"] == 2
    shot = next(s for s in r["shots"] if s["thumb"])
    assert screenshots.cached(pc, shot) is None
    t = screenshots.thumb_path(pc, shot)
    assert t.read_bytes() == b"\xff\xd8thumb" and t.is_relative_to(user_data_dir() / "screenshots-cache" / "10.0.0.5")
    assert screenshots.thumb_path(pc, shot) == t and pc.sftp.gets == [shot["thumb"]]  # downloaded once
    assert screenshots.cached(pc, shot) == t
    full = screenshots.image_path(pc, shot)
    assert full.read_bytes() == open(shot["path"], "rb").read()
    # without a Steam thumbnail one is made from the full image (here: not an image -> the original is used)
    no_thumb = next(s for s in r["shots"] if not s["thumb"])
    assert screenshots.thumb_path(pc, no_thumb).exists()
    out = tmp_path / "out"
    got = screenshots.download(pc, r["shots"], out)
    assert got["files"] == 2 and (out / "X Game" / os.path.basename(shot["path"])).exists()
    assert screenshots.download(pc, r["shots"], out)["skipped"] == 2  # same size: not copied again
    assert screenshots.delete(pc, [shot]) == 1 and not os.path.exists(shot["path"])
    assert screenshots.cached(pc, shot) is None


def test_pc_download_cancel(pc, tmp_path):
    from frameport.core.events import Cancelled, Reporter
    from frameport.install import screenshots

    rep = Reporter()
    rep.cancelled.set()
    with pytest.raises(Cancelled):
        screenshots.download(pc, screenshots.list_shots(pc)["shots"], tmp_path / "out", rep)
    assert not list((tmp_path / "out").rglob("*.jpg"))


def test_group_by_day():
    from frameport.ui.views.screenshots import agent_filter, filter_key, group_by_day

    now = time.mktime((2026, 10, 4, 15, 0, 0, 0, 0, -1))
    shots = [{"time": now - 60}, {"time": now - 3600}, {"time": now - 86400}, {"time": now - 5 * 86400},
             {"time": None}]
    groups = group_by_day(shots, now)
    assert [(label, len(s)) for label, s in groups] == [("Today", 2), ("Yesterday", 1), ("2026-09-29", 1),
                                                        ("Unknown date", 1)]
    assert agent_filter(filter_key(None)) is None and agent_filter(filter_key("")) == ""
    assert agent_filter(filter_key("com.x")) == "com.x"
