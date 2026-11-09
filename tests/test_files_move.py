"""Files tab, PC side (GitHub #532): /tmp and a game's saves as locations, Move to… — both ends checked before the
Frame is asked, then the real agent run end to end (a local process stands in for the SSH exec channel, a fake
`podman` for the namespace: `podman unshare` just runs the command)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from frameport.core.events import Cancelled, Reporter
from frameport.install import files

AGENT = Path(__file__).resolve().parents[1] / "agent" / "frameport_agent.py"
PKG = "com.example.game"


class _Chan:
    def __init__(self, proc):
        self.proc = proc

    def shutdown_write(self):
        self.proc.stdin.close()

    def recv_exit_status(self):
        return self.proc.wait()

    def close(self):
        self.proc.kill()


class _Stream:
    def __init__(self, f, chan):
        self.f, self.channel = f, chan

    def __getattr__(self, name):
        return getattr(self.f, name)


class _Client:
    def __init__(self, env):
        self.env = env
        self.commands = []

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        proc = subprocess.Popen(["sh", "-c", command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=self.env)
        chan = _Chan(proc)
        return _Stream(proc.stdin, chan), _Stream(proc.stdout, chan), _Stream(proc.stderr, chan)


class LocalFrame:
    """A Frame whose agent runs here (HOME = a temp folder)."""

    def __init__(self, env):
        self.env, self.client = env, _Client(env)

    def ensure_agent(self):
        return str(AGENT)

    def agent(self, command, **args):
        p = subprocess.run([sys.executable, str(AGENT), command], input=json.dumps(args), capture_output=True,
                           text=True, env=self.env)
        reply = json.loads(p.stdout.strip().splitlines()[-1])
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["result"]


@pytest.fixture
def frame(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text('#!/bin/sh\ncase "$1" in\n  unshare) shift; exec "$@";;\n  ps) printf "%s\\n" "$FAKE_RUNNING";;\n'
                      'esac\n')
    podman.chmod(0o755)
    python = bin_dir / "python3"  # the agent runs with this test's interpreter
    python.symlink_to(sys.executable)
    home = tmp_path / "home"
    anchor = home / "Applications/quest-frame" / PKG
    (anchor / "lepton-app").mkdir(parents=True)
    (anchor / "deployment.json").write_text(json.dumps({"package": PKG, "appid": 123, "title": "Example"}))
    internal = anchor / "lepton-data/internal" / PKG
    (internal / "files/SaveGames").mkdir(parents=True)
    (internal / "files/SaveGames/slot1.sav").write_bytes(b"\x01" * 100)
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_RUNNING": ""}
    env.pop("FRAMEPORT_SAVES_NS", None)
    f = LocalFrame(env)
    f.home, f.internal = home, internal
    return f


def saves_loc(frame):
    info = files.saves_info(frame, PKG)
    return {**files.saves_location(PKG), "path": info["root"]}, info


# ------------------------------------------------------------------ locations
def test_tmp_location_is_confined_to_tmp():
    loc = files.tmp_location()
    assert loc["path"] == "/tmp" and loc["tmp"]
    assert files.inside(loc["path"], "install/x.apk") == "/tmp/install/x.apk"
    for bad in ("../etc/passwd", "/etc", "/tmpfoo", "/tmp/../home"):
        with pytest.raises(ValueError):
            files.inside(loc["path"], bad)


def test_tmp_itself_can_be_neither_deleted_nor_renamed():
    loc = files.tmp_location()
    with pytest.raises(ValueError):
        files.loc_delete(None, loc, ["/tmp"])
    with pytest.raises(ValueError):
        files.loc_delete(None, loc, ["/tmp/"])
    with pytest.raises(ValueError):
        files.loc_rename(None, loc, "/tmp", "x")


def test_saves_location_listing_and_changes(frame, tmp_path):
    loc, info = saves_loc(frame)
    assert info["root"] == str(frame.internal) and info["android"] == f"/data/data/{PKG}" and not info["running"]
    entries = files.loc_list(frame, loc, loc["path"])
    assert [(e.name, e.is_dir) for e in entries] == [("files", True)]
    files.loc_make_dir(frame, loc, str(frame.internal / "files"), "Mods")
    assert [e.name for e in files.loc_list(frame, loc, str(frame.internal / "files"))] == ["Mods", "SaveGames"]
    files.loc_rename(frame, loc, str(frame.internal / "files/Mods"), "Mods2")
    files.loc_delete(frame, loc, [str(frame.internal / "files/Mods2")])
    assert not (frame.internal / "files/Mods2").exists()
    with pytest.raises(ValueError):  # checked here before the Frame is asked
        files.loc_list(frame, loc, "/etc")
    with pytest.raises(ValueError):
        files.loc_delete(frame, loc, [loc["path"]])


def test_saves_upload_and_download(frame, tmp_path):
    loc, _info = saves_loc(frame)
    src = tmp_path / "pc/Pack"
    (src / "empty").mkdir(parents=True)
    (src / "a.pak").write_bytes(b"p" * 3000)
    sent, _skipped, total = files.loc_upload(frame, loc, [src], str(frame.internal / "files"), Reporter())
    assert total == 3000 and len(sent) == 1
    assert (frame.internal / "files/Pack/a.pak").read_bytes() == b"p" * 3000
    assert (frame.internal / "files/Pack/empty").is_dir()
    out = tmp_path / "download"
    r = files.loc_download(frame, loc, [str(frame.internal / "files")], out, Reporter())
    assert r["files"] == 2
    assert (out / "files/SaveGames/slot1.sav").read_bytes() == b"\x01" * 100
    assert (out / "files/Pack/empty").is_dir()


def test_saves_changes_refused_while_the_game_runs(frame, tmp_path):
    frame.env["FAKE_RUNNING"] = "lepton-steamlaunch-123"
    loc, info = saves_loc(frame)
    assert info["running"]
    assert files.loc_list(frame, loc, loc["path"])  # looking is fine
    (tmp_path / "x.sav").write_text("x")
    with pytest.raises(files.FilesError, match="running"):
        files.loc_upload(frame, loc, [tmp_path / "x.sav"], loc["path"], Reporter())
    with pytest.raises(RuntimeError, match="running"):
        files.loc_delete(frame, loc, [str(frame.internal / "files")])
    assert not (frame.internal / "x.sav").exists() and (frame.internal / "files").is_dir()


# ------------------------------------------------------------------ Move to…
def test_check_move_both_ends():
    tmp, videos = files.tmp_location(), {"id": "videos", "path": "/home/x/Videos"}
    assert files.check_move(tmp, ["/tmp/a.mp4", "b"], videos, "/home/x/Videos/Trips") == \
        (["/tmp/a.mp4", "/tmp/b"], "/home/x/Videos/Trips")
    bad = [(["/etc/passwd"], "/home/x/Videos"),       # source outside /tmp
           (["/tmp/../etc/passwd"], "/home/x/Videos"),
           (["/tmp"], "/home/x/Videos"),              # /tmp itself
           ([], "/home/x/Videos"),
           (["/tmp/a"], "/home/x"),                   # destination outside its location
           (["/tmp/a"], "/home/x/Videos/../.ssh")]
    for items, folder in bad:
        with pytest.raises(ValueError):
            files.check_move(tmp, items, videos, folder)
    with pytest.raises(ValueError, match="into itself"):
        files.check_move(videos, ["/home/x/Videos/Trips"], videos, "/home/x/Videos/Trips/2026")
    with pytest.raises(ValueError, match="already in that folder"):
        files.check_move(videos, ["/home/x/Videos/a.mp4"], videos, "/home/x/Videos")
    with pytest.raises(ValueError, match="read-only"):
        files.check_move(tmp, ["/tmp/a"], {**videos, "read_only": True}, "/home/x/Videos")


def test_move_end_names_saves_by_package():
    assert files.move_end({"saves": PKG, "path": "/whatever"}) == {"saves": PKG}
    assert files.move_end({"path": "/tmp/"}) == {"root": "/tmp"}


def test_move_between_locations_end_to_end(frame):
    downloads, videos = frame.home / "Downloads", frame.home / "Videos"
    (downloads / "Pack").mkdir(parents=True)
    (downloads / "Pack/a.txt").write_text("a")
    (downloads / "movie.mp4").write_bytes(b"m" * 10)
    videos.mkdir()
    rep = Reporter()
    r = files.move(frame, {"path": str(downloads)}, [str(downloads / "Pack"), str(downloads / "movie.mp4")],
                   {"path": str(videos)}, str(videos), rep)
    assert r["moved"] == 2 and r["renamed"] == 2  # one file system: renames
    assert (videos / "Pack/a.txt").read_text() == "a" and not (downloads / "movie.mp4").exists()
    # into the game's saves (the namespace path) and back out
    loc, _ = saves_loc(frame)
    r = files.move(frame, {"path": str(videos)}, [str(videos / "movie.mp4")], loc,
                   str(frame.internal / "files/SaveGames"), rep)
    assert (frame.internal / "files/SaveGames/movie.mp4").exists()
    with pytest.raises(files.FilesError, match="already exists"):
        (videos / "slot1.sav").write_text("other")
        files.move(frame, loc, [str(frame.internal / "files/SaveGames/slot1.sav")], {"path": str(videos)},
                   str(videos), rep)
    assert (frame.internal / "files/SaveGames/slot1.sav").exists()


def test_move_cancel_closes_the_channel(frame, monkeypatch):
    tmp = frame.home / "t"
    (tmp / "a").mkdir(parents=True)
    rep = Reporter()
    rep.cancelled.set()
    with pytest.raises(Cancelled):  # Cancel is checked at each progress line (here: the first)
        files.move(frame, {"path": str(tmp)}, [str(tmp / "a")], {"path": str(frame.home)}, str(frame.home), rep)
