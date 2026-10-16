import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from frameport.install import rooms
from frameport.mr.room import Mesh, box_room, dumps, write_mesh


class Frame:
    home = "/fake-frame"

    def __init__(self):
        self.files = {}
        self.calls = []
        self.commands = []
        self.answer = {}
        self.sftp = SimpleNamespace(open=lambda path, mode: io.BytesIO(self.files[path]))

    def run(self, cmd):
        self.commands.append(cmd)
        return 0, "", ""

    def put(self, local, remote, **kwargs):
        self.files[remote] = Path(local).read_bytes()

    def agent(self, command, **args):
        self.calls.append((command, args))
        return self.answer


def test_import_export_and_preview(tmp_path):
    f = Frame()
    r = box_room(4, 3)
    m = Mesh(vertices=[(0, 0, 0), (1, 0, 0), (0, 0, 1)], indices=[0, 1, 2])
    r.mesh = Mesh(vertex_count=3, index_count=3)
    path = tmp_path / "room.txt"
    path.write_text(dumps(r))
    (tmp_path / "room-mesh.bin").write_bytes(write_mesh(m))
    rooms.put(f, path)
    assert f.calls[0][0] == "room_put"
    assert f.commands[-1].startswith("rm -rf -- ")
    assert len(f.files) == 2
    f.answer = {"room_text": dumps(r)}
    f.files[f"{f.home}/.local/share/frameport/rooms/0/room-mesh.bin"] = write_mesh(m)
    out = rooms.export(f, tmp_path / "export")
    assert out.read_text() == dumps(r)
    assert (out.parent / "room-mesh.bin").read_bytes() == write_mesh(m)
    assert rooms.show(f, tmp_path / "preview.svg")["floor_area"] == 12
    assert "<svg" in (tmp_path / "preview.svg").read_text()
    (tmp_path / "room-mesh.bin").write_bytes(b"wrong")
    with pytest.raises(ValueError):
        rooms.put(f, path)
    f.answer = {}
    with pytest.raises(ValueError, match="No room"):
        rooms.export(f, tmp_path / "empty")


def test_helper_sync_shared_with_live_view(tmp_path, monkeypatch):
    from frameport.install.livestream import sync_helper

    f = Frame()
    local = tmp_path / "fp_roomscan"
    local.write_bytes(b"scanner")
    remote = sync_helper(f, local, "fp_roomscan")
    assert remote.endswith("/bin/fp_roomscan")
    assert list(f.files.values()) == [b"scanner"]
    f.files.clear()
    monkeypatch.setattr(f, "run", lambda cmd: (0, hashlib.sha256(b"scanner").hexdigest()+"  path", ""))
    assert sync_helper(f, local, "fp_roomscan") == remote
    assert not f.files


def test_build_and_capture(tmp_path, monkeypatch):
    f = Frame()
    monkeypatch.setattr(rooms, "ensure_scanner", lambda frame: "scanner")
    dump = tmp_path / "scan.fprs"
    dump.write_bytes(b"FPRS\x01\0\0\0")
    rooms.build(f, dump, 7)
    assert f.calls[-1][0] == "room_build" and f.calls[-1][1]["universe"] == 7
    dump.write_bytes(b"wrong")
    with pytest.raises(ValueError, match="FPRS"):
        rooms.build(f, dump)
    f.answer = {"error": "not implemented yet"}
    with pytest.raises(RuntimeError, match="not implemented yet"):
        rooms.capture(f)


def test_cli_room_commands(tmp_path, monkeypatch):
    from frameport import cli

    f = Frame()
    monkeypatch.setattr(cli, "_target", lambda frame: SimpleNamespace(frame=f))
    runner = CliRunner()
    for command, expected in [("status", "room_status"), ("from-boundary", "room_from_boundary"),
                               ("delete", "room_delete")]:
        result = runner.invoke(cli.app, ["frame", "room", command])
        assert result.exit_code == 0, result.output
        assert f.calls[-1][0] == expected
    r = box_room(3, 4)
    f.answer = {"room_text": dumps(r)}
    result = runner.invoke(cli.app, ["frame", "room", "show", "--output", str(tmp_path / "preview.svg")])
    assert result.exit_code == 0, result.exception
    assert (tmp_path / "preview.svg").exists()
    result = runner.invoke(cli.app, ["frame", "room", "export", str(tmp_path / "out")])
    assert result.exit_code == 0, result.exception
