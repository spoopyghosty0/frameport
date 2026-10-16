"""Room commands and launcher synchronization, entirely on a temporary fake Frame."""
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent import load_agent

from frameport.mr.room import Mesh, box_room, dumps, parse, write_mesh


def test_universe_log(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    assert a.room_universe_from_log("noise") is None
    assert a.room_universe_from_log("chaperone universe 123\nSwitch to standing universe 456") == 456
    assert a.room_universe_from_log("Switch to universe 7\nchaperone universe 424242") == 0
    assert a.room_universe_from_log("Switch to universe: 9876543210987654321") == 9876543210987654321


def chaperone(a, points, universe=7):
    quads = [[[x, 0, z], [b, 0, c], [b, 2.43, c], [x, 2.43, z]]
             for (x, z), (b, c) in zip(points, points[1:] + points[:1], strict=True)]
    path = Path(a.HOME) / ".config/openvr/config/chaperone_info.vrchap"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"universes": [{"universeID": str(universe), "collision_bounds": quads}]}))


def test_boundary_circle_and_status(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    circle = [(math.cos(i*math.pi/10), math.sin(i*math.pi/10)) for i in range(20)]
    chaperone(a, circle)
    assert a.cmd_room_status({})["default_circle"]
    with pytest.raises(a.AgentError, match="default circle"):
        a.cmd_room_from_boundary({})
    chaperone(a, [(-2, -1), (2, -1), (2, 1), (-2, 1)])
    status = a.cmd_room_from_boundary({})
    assert status["aligned"] and status["universe"] == 7 and not status["default_circle"]
    assert status["room"]["summary"]["floor_area"] == 8
    assert parse(status["room_text"]).source == "boundary"
    assert Path(a.ROOMS_DIR, "current").readlink() == Path("7")
    logs = Path(a.STEAM, "logs/vrserver.txt")
    logs.parent.mkdir(parents=True)
    logs.write_text("chaperone universe 8")
    assert not a.cmd_room_status({})["aligned"]
    assert not a.cmd_room_status({})["scanner_installed"]
    assert not a.cmd_room_delete({})["rooms"]
    assert a.cmd_room_capture({}) == {"error": "not implemented yet"}


def test_store_mesh_validation_and_fallback(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    r = box_room(3, 4)
    r.universe = 7
    m = Mesh(vertices=[(0, 0, 0), (1, 0, 0), (0, 0, 1)], indices=[0, 1, 2])
    r.mesh = Mesh(vertex_count=3, index_count=3)
    with pytest.raises(a.AgentError, match="missing"):
        a.room_store(dumps(r))
    a.room_store(dumps(r), write_mesh(m))
    r.universe = 8
    a.room_store(dumps(r), write_mesh(m))
    assert a.room_selected(7).endswith("/7")
    assert a.room_selected(9).endswith("/8")
    with pytest.raises(a.AgentError):
        a.room_store(dumps(r), b"FPRM")
    assert a.cmd_room_status({})["room"]["universe"] == 8
    assert a.cmd_room_delete({"universe": 8})["room"]["universe"] == 7
    with pytest.raises(a.AgentError):
        a.cmd_room_delete({"universe": "../other"})
    with pytest.raises(a.AgentError):
        a.cmd_room_put({"path": str(tmp_path / "room.txt")})
    folder = Path(a.ROOMS_DIR) / ".incoming"
    folder.mkdir()
    (folder / "room.txt").write_text(dumps(r))
    (folder / "room-mesh.bin").write_bytes(write_mesh(m))
    assert a.cmd_room_put({"path": str(folder / "room.txt")})["room"]["universe"] == 8


def test_launcher_sync_and_upgrade(monkeypatch, tmp_path, capsys):
    a = load_agent(monkeypatch, tmp_path)
    pkg = "com.test.scene"
    anchor = Path(a.ANCHORS, pkg)
    anchor.mkdir(parents=True)
    base = tmp_path / "drive with space" / pkg
    (base / "lepton-app").mkdir(parents=True)
    dep = dict(package=pkg, base=str(base), appid=123, scene_room=True)
    (anchor / "deployment.json").write_text(json.dumps(dep))
    a.write_launcher(str(anchor), str(base), pkg, "Scene", 123, "/lepton", {}, scene_room=True)
    text = (anchor / "launch.sh").read_text()
    assert text.count("# frameport-room-sync") == 1
    assert text.index("_room_sync") < text.index("\nsetsid ")
    assert a.set_room_sync_line(text, pkg, True) == text
    old = "\n".join(line for line in text.splitlines() if "# frameport-room-sync" not in line) + "\n"
    (anchor / "launch.sh").write_text(old)
    a.upgrade_launchers()
    assert (anchor / "launch.sh").read_text().count("# frameport-room-sync") == 1
    r = box_room(4, 3)
    r.universe = 7
    a.room_store(dumps(r))
    a.room_sync_worker(pkg)
    assert parse((base / "room.txt").read_text()).universe == 7
    (base / "room-mesh.bin").write_bytes(b"stale")
    a.room_sync_worker(pkg)
    assert not (base / "room-mesh.bin").exists()
    # Live settings take precedence, including disabling after a previous start.
    (base / "settings.conf").write_text("scene_emul=0\n")
    a.room_sync_worker(pkg)
    assert not (base / "room.txt").exists()
    (base / "settings.conf").write_text("scene_emul=1\n")
    a.room_sync_worker(pkg)
    assert (base / "room.txt").exists()
    a.cmd_room_delete({})
    a.room_sync_worker(pkg)
    assert not (base / "room.txt").exists()


def test_build_offline_scanner(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    Path(a.ROOMS_DIR).mkdir(parents=True)
    scanner = Path(a.ROOM_SCANNER)
    scanner.parent.mkdir(parents=True)
    scanner.touch(mode=0o755)
    dump = Path(a.ROOMS_DIR, "scan.fprs")
    dump.write_bytes(b"FPRS")
    def run(argv, **kwargs):
        assert argv[:3] == [a.ROOM_SCANNER, "--offline", str(dump)]
        r = box_room(4, 3)
        r.universe = int(argv[argv.index("--universe") + 1])
        Path(argv[argv.index("--out") + 1], "room.txt").write_text(dumps(r))
        return SimpleNamespace(returncode=0, stdout='{"triangles": 20}', stderr="scan completed")
    monkeypatch.setattr(a.subprocess, "run", run)
    result = a.cmd_room_build({"dump": str(dump), "universe": 8})
    assert result["report"] == {"triangles": 20}
    assert result["room"]["universe"] == 8
    assert Path(a.ROOMS_DIR, "8/scan.log").read_text() == "scan completed"


def test_live_scene_setting_updates_only_the_quest_launcher(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    pkg = "com.test.live"
    anchor = Path(a.ANCHORS, pkg)
    anchor.mkdir(parents=True)
    dep = dict(package=pkg, base=str(anchor), appid=123, scene_room=False)
    (anchor / "deployment.json").write_text(json.dumps(dep))
    a.write_launcher(str(anchor), str(anchor), pkg, "Live", 123, "/lepton", {})
    path = anchor / "launch.sh"
    assert "# frameport-room-sync" not in path.read_text()
    (anchor / "settings.conf").write_text("scene_emul=1\n")
    a.room_setting_changed(pkg, dep, {"scene_emul": 1})
    assert "# frameport-room-sync" in path.read_text()
    assert a.deployment(pkg)["scene_room"]
    (anchor / "room.txt").write_text("stale")
    (anchor / "settings.conf").write_text("scene_emul=0\n")
    a.room_setting_changed(pkg, dep, {"scene_emul": 0})
    assert "# frameport-room-sync" not in path.read_text()
    assert not (anchor / "room.txt").exists()
    assert not a.deployment(pkg)["scene_room"]


def test_mesh_sync_renames_sidecar_and_changes_current_room(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    pkg = "com.test.mesh"
    anchor = Path(a.ANCHORS, pkg)
    anchor.mkdir(parents=True)
    (anchor / "deployment.json").write_text(json.dumps(dict(package=pkg, base=str(anchor), scene_room=True)))
    r = box_room(3, 4)
    r.universe = 7
    r.mesh = Mesh(filename="custom.bin", vertices=[(0, 0, 0), (1, 0, 0), (0, 0, 1)], indices=[0, 1, 2])
    data = write_mesh(r.mesh)
    a.room_store(dumps(r), data)
    result = a.room_sync_worker(pkg)
    assert result["room"]["universe"] == 7
    got = parse((anchor / "room.txt").read_text())
    assert got.mesh.filename == "room-mesh.bin"
    assert (anchor / "room-mesh.bin").read_bytes() == data
    assert not (anchor / "custom.bin").exists()


@pytest.mark.parametrize("settings, recipe, enabled", [
    ({"scene_emul": 1}, {}, True),
    ({}, {"adapter": {"scene_emul": 1}}, True),
    ({}, {"patches": ["adapter.scene_emul"]}, True),
    ({"scene_emul": 0}, {"patches": ["adapter.scene_emul"]}, False),
])
def test_finalize_records_resolved_scene_setting(monkeypatch, tmp_path, settings, recipe, enabled):
    from test_game_update import PKG, installed_agent

    a, base, _ = installed_agent(monkeypatch, tmp_path)
    a.configure_install(dict(package=PKG, title="Example", settings=settings, recipe=recipe),
                        dict(base=str(base), anchor=str(base), appid=42, lepton="/lepton"), 0)
    assert a.deployment(PKG)["scene_room"] is enabled
