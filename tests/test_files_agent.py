"""Files tab on the Frame's side (GitHub #532): a game's internal storage as a location (saves, reached through the
agent like save backups), and Move to… (rename on one file system, else copy + delete). podman unshare is switched
off here (the files are ours)."""
import importlib.util
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1] / "agent" / "frameport_agent.py"
PKG = "com.example.game"


def load_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FRAMEPORT_SAVES_NS", raising=False)
    spec = importlib.util.spec_from_file_location("frameport_agent_files", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "as_owner", lambda: [])
    monkeypatch.setattr(mod, "container_running", lambda appid: False)
    return mod


def write(path: Path, data="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


@pytest.fixture
def game(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = Path(a.ANCHORS) / PKG
    (anchor / "lepton-app").mkdir(parents=True)
    (anchor / "deployment.json").write_text(json.dumps({"package": PKG, "appid": 123, "title": "Example"}))
    internal = anchor / "lepton-data/internal" / PKG
    external = anchor / "lepton-data/external"
    write(internal / "files/Saved/SaveGames/slot1.sav", b"\x01" * 100)
    write(internal / "shared_prefs/prefs.xml", "<map/>")
    (tmp_path / "Videos").mkdir()
    external.mkdir(parents=True)
    os.symlink(tmp_path / "Videos", external / "Movies")
    os.symlink("/etc", internal / "escape")
    return a, internal, external, tmp_path


def op(a, **args):
    return a.cmd_saves_files({"package": PKG, **args})


# ------------------------------------------------------------------ the saves location
def test_saves_location_lists_and_changes_inside_the_internal_storage(game):
    a, internal, _external, _home = game
    info = op(a, op="info")
    assert info == {"root": str(internal), "android": f"/data/data/{PKG}", "exists": True, "running": False}
    names = {e["name"]: e for e in op(a, op="list")["entries"]}
    assert set(names) == {"files", "shared_prefs", "escape"}
    assert names["escape"]["link"] and names["files"]["is_dir"]
    deep = op(a, op="list", path=str(internal / "files/Saved/SaveGames"))["entries"]
    assert [(e["name"], e["size"]) for e in deep] == [("slot1.sav", 100)]
    made = op(a, op="mkdir", parent=str(internal / "files"), name="Mods")["path"]
    assert os.path.isdir(made)
    renamed = op(a, op="rename", path=made, name="Mods2")["path"]
    assert renamed.endswith("/files/Mods2") and os.path.isdir(renamed)
    assert op(a, op="delete", paths=[renamed, str(internal / "escape")])["deleted"] == 2
    assert not os.path.lexists(internal / "escape") and os.path.isdir("/etc")  # the link, never its target


@pytest.mark.parametrize("bad", [dict(op="list", path="/etc"), dict(op="list", path="../../external"),
                                 dict(op="list", path="escape"),  # a link that leads out
                                 dict(op="mkdir", parent="escape", name="x"),
                                 dict(op="rename", path="files", name="../x"),
                                 dict(op="delete", paths=["/home"]), dict(op="delete", paths=[""]),
                                 dict(op="rename", path="", name="other"), dict(op="nope")])
def test_saves_location_never_leaves_its_root(game, bad):
    a, *_ = game
    with pytest.raises(a.AgentError):
        op(a, **bad)


def test_saves_changes_are_refused_while_the_game_runs(game, monkeypatch):
    a, internal, *_ = game
    monkeypatch.setattr(a, "container_running", lambda appid: True)
    assert op(a, op="info")["running"] is True
    assert op(a, op="list")["entries"]  # looking is fine
    with pytest.raises(a.AgentError, match="running"):
        op(a, op="delete", paths=[str(internal / "files")])
    assert (internal / "files").is_dir()


def test_saves_not_created_yet(game):
    a, internal, *_ = game
    import shutil

    shutil.rmtree(internal)
    assert op(a, op="info")["exists"] is False
    with pytest.raises(a.AgentError, match="start it once"):
        op(a, op="list")


def tar_bytes(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members:
            ti = tarfile.TarInfo(name)
            if data is None:
                ti.type = tarfile.DIRTYPE
                tar.addfile(ti)
            elif isinstance(data, str) and data.startswith("->"):
                ti.type, ti.linkname = tarfile.SYMTYPE, data[2:]
                tar.addfile(ti)
            else:
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def put(a, dest, members):
    out = io.StringIO()
    code = a.saves_put_session({"package": PKG, "dest": str(dest)}, io.BytesIO(tar_bytes(members)), out)
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def test_saves_upload_and_download_round_trip(game):
    a, internal, *_ = game
    code, reply = put(a, internal / "files", [("Mods", None), ("Mods/empty", None), ("Mods/a.pak", b"pak"),
                                              ("Saved/SaveGames/slot1.sav", b"new"), ("link", "->/etc")])
    assert code == 0 and reply["result"] == {"files": 2, "bytes": 6, "folders": 2}
    assert (internal / "files/Mods/a.pak").read_bytes() == b"pak"
    assert (internal / "files/Mods/empty").is_dir()
    assert (internal / "files/Saved/SaveGames/slot1.sav").read_bytes() == b"new"  # replaced
    assert not (internal / "files/link").exists()  # links are never created
    out, err = io.BytesIO(), io.StringIO()
    assert a.saves_get_session({"package": PKG, "paths": [str(internal / "files/Mods"),
                                                         str(internal / "shared_prefs/prefs.xml"),
                                                         str(internal / "escape")]}, out, err) == 0
    assert json.loads(err.getvalue())["result"] == {"files": 2, "bytes": 9}
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
        assert sorted(tar.getnames()) == ["Mods", "Mods/a.pak", "Mods/empty", "prefs.xml"]


@pytest.mark.parametrize("members", [[("../outside.txt", b"x")], [("/abs.txt", b"x")],
                                     [("escape/passwd", b"x")]])  # through the link to /etc
def test_saves_upload_refuses_paths_outside(game, members):
    a, internal, *_ = game
    code, reply = put(a, internal, members)
    assert code == 1 and not reply["ok"]


def test_saves_upload_refused_while_running_and_outside_dest(game, monkeypatch):
    a, internal, *_ = game
    code, reply = put(a, "/tmp", [("x", b"x")])
    assert code == 1 and "outside" in reply["error"]
    monkeypatch.setattr(a, "container_running", lambda appid: True)
    code, reply = put(a, internal, [("x", b"x")])
    assert code == 1 and "running" in reply["error"] and not (internal / "x").exists()


# ------------------------------------------------------------------ Move to…
def move(a, items, folder, src, dest, emit=None):
    lines = []
    result = a.files_move_here({"items": [str(p) for p in items], "folder": str(folder), "src": src, "dest": dest},
                               emit or lines.append)
    return result, lines


def test_move_on_one_file_system_is_a_rename(game, tmp_path):
    a, *_ = game
    home = {"root": str(tmp_path)}
    write(tmp_path / "Downloads/movie.mp4", b"m" * 10)
    write(tmp_path / "Downloads/Pack/a.txt", "a")
    (tmp_path / "Videos/Trips").mkdir()
    r, _lines = move(a, [tmp_path / "Downloads/movie.mp4", tmp_path / "Downloads/Pack"], tmp_path / "Videos/Trips",
                     home, home)
    assert r == {"moved": 2, "renamed": 2, "copied": 0, "bytes": 0}
    assert (tmp_path / "Videos/Trips/movie.mp4").read_bytes() == b"m" * 10
    assert (tmp_path / "Videos/Trips/Pack/a.txt").read_text() == "a"
    assert not (tmp_path / "Downloads/movie.mp4").exists()


def test_move_across_file_systems_copies_then_deletes(game, tmp_path, monkeypatch):
    a, *_ = game
    monkeypatch.setattr(a, "_files_same_fs", lambda src, folder: False)
    monkeypatch.setattr(a, "FILES_CHUNK", 4)
    write(tmp_path / "tmp/Pack/sub/a.bin", b"a" * 10)
    write(tmp_path / "tmp/Pack/b.bin", b"b" * 6)
    os.symlink("sub/a.bin", tmp_path / "tmp/Pack/link")
    (tmp_path / "tmp/Pack/empty").mkdir()
    (tmp_path / "Documents").mkdir()
    r, lines = move(a, [tmp_path / "tmp/Pack"], tmp_path / "Documents", {"root": str(tmp_path / "tmp")},
                    {"root": str(tmp_path / "Documents")})
    assert r == {"moved": 1, "renamed": 0, "copied": 1, "bytes": 16}
    dest = tmp_path / "Documents/Pack"
    assert (dest / "sub/a.bin").read_bytes() == b"a" * 10 and (dest / "b.bin").read_bytes() == b"b" * 6
    assert os.readlink(dest / "link") == "sub/a.bin" and (dest / "empty").is_dir()
    assert not (tmp_path / "tmp/Pack").exists()
    assert lines[-1] == {"progress": 16, "total": 16, "item": "Pack"}


def test_cancelled_copy_keeps_the_original_and_removes_the_partial_copy(game, tmp_path, monkeypatch):
    a, *_ = game
    monkeypatch.setattr(a, "_files_same_fs", lambda src, folder: False)
    monkeypatch.setattr(a, "FILES_CHUNK", 4)
    monkeypatch.setattr(a.time, "monotonic", iter(range(0, 1000, 1)).__next__)  # every chunk reports
    write(tmp_path / "tmp/big.bin", b"z" * 40)
    (tmp_path / "Videos/x").mkdir()

    def emit(msg):
        if msg["progress"] >= 8:
            raise BrokenPipeError
    with pytest.raises(BrokenPipeError):
        move(a, [tmp_path / "tmp/big.bin"], tmp_path / "Videos/x", {"root": str(tmp_path / "tmp")},
             {"root": str(tmp_path / "Videos")}, emit=emit)
    assert (tmp_path / "tmp/big.bin").read_bytes() == b"z" * 40
    assert not (tmp_path / "Videos/x/big.bin").exists()


def test_move_checks_both_ends(game, tmp_path):
    a, internal, external, _home = game
    tmp = {"root": str(tmp_path / "tmp")}
    write(tmp_path / "tmp/a.txt", "a")
    write(tmp_path / "Videos/a.txt", "other")
    write(tmp_path / "secret/s.txt", "s")
    (tmp_path / "tmp/dir/inner").mkdir(parents=True)
    os.symlink(tmp_path / "secret", tmp_path / "tmp/out")
    videos = {"root": str(tmp_path / "Videos")}
    bad = [([tmp_path / "secret/s.txt"], tmp_path / "Videos", tmp, videos),           # source outside its location
           ([tmp_path / "tmp/a.txt"], tmp_path / "secret", tmp, videos),              # folder outside its location
           ([tmp_path / "tmp/a.txt"], tmp_path / "Videos", tmp, videos),              # a.txt exists there
           ([tmp_path / "tmp/dir"], tmp_path / "tmp/dir/inner", tmp, tmp),            # into itself
           ([tmp_path / "tmp/a.txt"], tmp_path / "tmp", tmp, tmp),                    # already there
           ([tmp_path / "tmp"], tmp_path / "Videos", tmp, videos),                    # the location itself
           ([tmp_path / "tmp/out/s.txt"], tmp_path / "Videos", tmp, videos),          # through a link
           ([tmp_path / "tmp/a.txt"], tmp_path / "tmp/out", tmp, tmp),                # into a link that leads out
           ([external / "Movies"], tmp_path / "Videos", {"root": str(external)}, videos),  # Lepton's link
           ([tmp_path / "tmp/a.txt"], tmp_path, tmp, {"root": "/"}),                  # / is no location
           ([tmp_path / "tmp/a.txt"], internal / "escape", tmp, {"saves": PKG})]      # saves: out through a link
    reasons = ["outside", "outside", "already exists", "into itself", "already in that folder", "location itself",
               "through a link", "through a link", "Lepton's links", "bad location", "through a link"]
    for (items, folder, src, dest), reason in zip(bad, reasons, strict=True):
        with pytest.raises(a.AgentError, match=reason):
            move(a, items, folder, src, dest)
    assert (tmp_path / "tmp/a.txt").read_text() == "a" and (tmp_path / "secret/s.txt").exists()
    assert os.path.islink(external / "Movies")


def test_move_into_and_out_of_saves(game, tmp_path):
    a, internal, *_ = game
    write(tmp_path / "Downloads/slot2.sav", "two")
    out = io.StringIO()
    assert a.files_move_session({"items": [str(tmp_path / "Downloads/slot2.sav")],
                                 "folder": str(internal / "files/Saved/SaveGames"),
                                 "src": {"root": str(tmp_path / "Downloads")}, "dest": {"saves": PKG}}, out) == 0
    assert json.loads(out.getvalue().strip().splitlines()[-1])["result"]["moved"] == 1
    assert (internal / "files/Saved/SaveGames/slot2.sav").read_text() == "two"
    out = io.StringIO()
    assert a.files_move_session({"items": [str(internal / "files/Saved/SaveGames/slot1.sav")],
                                 "folder": str(tmp_path / "Downloads"),
                                 "src": {"saves": PKG}, "dest": {"root": str(tmp_path / "Downloads")}}, out) == 0
    assert (tmp_path / "Downloads/slot1.sav").read_bytes() == b"\x01" * 100


def test_move_with_saves_refused_while_the_game_runs(game, tmp_path, monkeypatch):
    a, internal, *_ = game
    monkeypatch.setattr(a, "container_running", lambda appid: True)
    write(tmp_path / "Downloads/slot2.sav", "two")
    out = io.StringIO()
    assert a.files_move_session({"items": [str(tmp_path / "Downloads/slot2.sav")], "folder": str(internal),
                                 "src": {"root": str(tmp_path / "Downloads")}, "dest": {"saves": PKG}}, out) == 1
    assert "running" in json.loads(out.getvalue().strip())["error"]
    assert (tmp_path / "Downloads/slot2.sav").exists()
