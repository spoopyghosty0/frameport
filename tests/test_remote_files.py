"""Files tab backend (install/files.py): browsing and managing a location over SFTP, never outside it."""
import contextlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from frameport.install import files, installer


class LocalSFTP:
    """paramiko.SFTPClient's calls used by files.py, on the local file system."""
    def listdir_attr(self, path):
        out = []
        for name in sorted(os.listdir(path)):
            a = paramiko_attr(os.lstat(os.path.join(path, name)))
            a.filename = name
            out.append(a)
        return out

    def stat(self, path):
        return paramiko_attr(os.stat(path))

    def lstat(self, path):
        return paramiko_attr(os.lstat(path))

    def mkdir(self, path):
        os.mkdir(path)

    def rename(self, a, b):
        os.rename(a, b)

    def get(self, remote, local, callback=None):
        shutil.copyfile(remote, local)
        if callback:
            callback(os.path.getsize(remote), os.path.getsize(remote))


def paramiko_attr(st):
    import paramiko

    return paramiko.SFTPAttributes.from_stat(st)


class LocalFrame:
    sftp = LocalSFTP()

    def mkdirs(self, dirs):
        for d in dirs:
            os.makedirs(d, exist_ok=True)

    def run(self, command, stdin=None, timeout=None):
        p = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr


@pytest.fixture
def frame(monkeypatch):
    monkeypatch.setattr(installer, "transfer_link", lambda f, r, n: contextlib.nullcontext(f))
    return LocalFrame()


def test_browse_and_manage(frame, tmp_path):
    root = tmp_path / "Videos"
    (root / "Trips").mkdir(parents=True)
    (root / "Trips/a.mp4").write_bytes(b"x" * 10)
    (root / ".hidden").write_text("h")
    (root / "b.txt").write_text("b")
    entries = files.list_dir(frame, str(root), str(root))
    assert [(e.name, e.is_dir) for e in entries] == [("Trips", True), ("b.txt", False)]  # folders first, no hidden
    assert ".hidden" in [e.name for e in files.list_dir(frame, str(root), str(root), hidden=True)]
    new = files.make_dir(frame, str(root), str(root), "New")
    assert Path(new).is_dir()
    files.rename(frame, str(root), str(root / "b.txt"), "c.txt")
    assert (root / "c.txt").exists()
    out = tmp_path / "pc"
    r = files.download(frame, str(root), [str(root / "Trips"), str(root / "c.txt")], out)
    assert r["files"] == 2 and (out / "Trips/a.mp4").read_bytes() == b"x" * 10 and (out / "c.txt").exists()
    assert files.delete(frame, str(root), [str(root / "Trips"), new]) == 2
    assert not (root / "Trips").exists() and not Path(new).exists()


def test_nothing_leaves_the_location(frame, tmp_path):
    root = tmp_path / "Videos"
    root.mkdir()
    (tmp_path / "secret").write_text("s")
    for bad in (str(tmp_path / "secret"), str(root / ".." / "secret"), "../secret"):
        with pytest.raises(ValueError):
            files.delete(frame, str(root), [bad])
    with pytest.raises(ValueError):
        files.delete(frame, str(root), [str(root)])  # not the location itself
    for name in ("../x", "a/b", "", ".."):
        with pytest.raises(ValueError):
            files.make_dir(frame, str(root), str(root), name)
    assert (tmp_path / "secret").exists()


def test_links_are_shown_as_folders_and_deleting_one_keeps_the_target(frame, tmp_path):
    shared = tmp_path / "Videos"
    shared.mkdir()
    (shared / "keep.mp4").write_text("v")
    game = tmp_path / "game/external"
    game.mkdir(parents=True)
    os.symlink(shared, game / "Movies")
    [e] = files.list_dir(frame, str(game), str(game))
    assert e.is_dir and e.link
    files.delete(frame, str(game), [str(game / "Movies")])
    assert (shared / "keep.mp4").exists()


def test_crumbs():
    from frameport.ui.views.files import crumbs

    assert crumbs("/h/Videos", "/h/Videos/a/b", "Videos") == [("Videos", "/h/Videos"), ("a", "/h/Videos/a"),
                                                              ("b", "/h/Videos/a/b")]
    assert crumbs("/h/Videos", "/h/Videos", "Videos") == [("Videos", "/h/Videos")]


def test_upload_keeps_empty_folders(frame, tmp_path, monkeypatch):  # GitHub #532
    from frameport.core.events import Reporter

    sent = []
    monkeypatch.setattr(installer, "upload_files", lambda xfer, items, remote, rep, total: sent.extend(items))
    src = tmp_path / "pc/Mod"
    (src / "saves/slot1").mkdir(parents=True)
    (src / "maps").mkdir()
    (src / "maps/a.map").write_text("m")
    root = tmp_path / "Documents"
    root.mkdir()
    items, have, _ = files.upload(frame, [src], str(root), Reporter())
    assert [i[1] for i in items] == ["Mod/maps/a.map"] and not have
    assert (root / "Mod/saves/slot1").is_dir()
    files.upload(frame, [tmp_path / "pc"], str(root / "Mod"), Reporter())  # an empty tree at the top too
    assert (root / "Mod/pc/Mod/saves/slot1").is_dir()
