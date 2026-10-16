"""RoomView capture (agent) and the PC's scan flow, without SteamVR: pose math, the final raw->STAGE rewrite of a
dump, prompt choice, the worker's status file, and rooms.scan's polling."""
import json
import math
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent import load_agent

from frameport.install import rooms


def rot_y(deg, t=(0.0, 0.0, 0.0)):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [c, 0, s, t[0], 0, 1, 0, t[1], -s, 0, c, t[2]]


def test_m34_mul_and_quat(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    m = a._m34_mul(rot_y(90, (0, 1.6, 0)), rot_y(0, (1, 0, 0)))
    assert m[3] == pytest.approx(0, abs=1e-6) and m[7] == pytest.approx(1.6) and m[11] == pytest.approx(-1)
    x, y, z, w = a._m34_quat(rot_y(90))
    assert (x, z) == (pytest.approx(0), pytest.approx(0))
    assert y == pytest.approx(math.sin(math.radians(45))) and w == pytest.approx(math.cos(math.radians(45)))
    for deg in (0, 179.9, -120):  # every branch of the matrix -> quaternion conversion stays a unit rotation
        q = a._m34_quat(rot_y(deg))
        assert sum(v * v for v in q) == pytest.approx(1)


def test_prompt_choice(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    assert a._prompt_for(None, set(), {"up": 0, "down": 0}) == "start"
    assert a._prompt_for(1.0, {0, 1, 2}, {"up": 0, "down": 0}) == "look_03"
    assert a._prompt_for(1.0, set(range(12)), {"up": 0, "down": 0}) == "ceiling"
    assert a._prompt_for(1.0, set(range(12)), {"up": 9, "down": 0}) == "floor"
    assert a._prompt_for(1.0, set(range(11)), {"up": 9, "down": 9}) == "look_11"


def test_scan_completes_early_only_when_everything_was_seen(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    full, seen = set(range(12)), {"up": 9, "down": 9}
    assert a._scan_complete(0.0, 61.0, full, seen)
    assert not a._scan_complete(0.0, 50.0, full, seen)  # too short
    assert not a._scan_complete(0.0, 61.0, set(range(11)), seen)
    assert not a._scan_complete(0.0, 61.0, full, {"up": 9, "down": 1})
    assert not a._scan_complete(None, 61.0, full, seen)


def test_view_coverage_bins(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    bins, pitch = set(), {"up": 0, "down": 0}
    for deg in range(15, 360, 30):  # mid-bin: exact multiples of 30 sit on bin edges
        a._view_coverage(rot_y(deg), bins, pitch)
    assert len(bins) == 12 and pitch == {"up": 0, "down": 0}
    up = [1, 0, 0, 0, 0, 0, 1, 0, 0, -1, 0, 0]  # mesh +Z (third column) pointing up
    a._view_coverage(up, bins, pitch)
    assert pitch["up"] == 1


class FakeReader:
    """A stream of six fresh mesh blocks (alternately facing 0 / 90 deg), then nothing; raw->standing moves the
    floor by +0.5 m at the end. The first two only make the stream count as "passthrough on"."""

    def __init__(self, app_type=None, blocks=6):
        import ctypes

        self.ct = ctypes
        self.blocks = [(i, rot_y(90 * (i % 2 == 0), (0, 1.0, 0))) for i in range(1, blocks + 1)]
        self.verts = ctypes.create_string_buffer(struct.pack("<9f", *range(9)))
        self.idx = ctypes.create_string_buffer(struct.pack("<3I", 0, 1, 2))
        self.data, self.layout = "data", "layout"
        self.current = None

    def up_to_date(self):
        return True

    def acquire(self, queue, read_type, timeout_ms):
        if queue == "layout":
            return SimpleNamespace(value="layout"), self.ct.addressof(self.idx)
        if not self.blocks:
            return None, None
        self.current = self.blocks.pop(0)
        return SimpleNamespace(value="data"), self.ct.addressof(self.verts)

    def prop(self, root, path, fmt):
        return {"/meshData/meshId": self.current[0] if self.current else None, "/meshData/layoutId": 5,
                "/meshData/elementCount": 9, "/meshData/rawFromMeshPose": tuple(self.current[1]),
                "/meshLayout/indexCount": 3, "/meshLayout/layoutId": 5}.get(path)

    def release(self, queue, blk):
        pass

    def raw_to_standing(self):
        return rot_y(0, (0, 0.5, 0))

    def close(self):
        pass


def test_capture_rewrites_poses_with_final_transform(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "_RoomViewReader", FakeReader)
    out = tmp_path / "scan.fprs"
    clock = iter(i * 0.05 for i in range(10000))
    monkeypatch.setattr(a.time, "time", lambda: next(clock))
    res = a.capture_roomview(3, str(out), max_hz=100)
    assert res["frames"] == 3  # blocks 4-6: three meshes had to stream before the first recorded one
    data = out.read_bytes()
    assert data[:8] == b"FPRS\x01\0\0\0"
    hdr = struct.calcsize(a.FPRS_FRAME)
    t, px, py, pz, qx, qy, qz, qw, vc, ic = struct.unpack_from(a.FPRS_FRAME, data, 8)
    assert (vc, ic) == (3, 3) and py == pytest.approx(1.5)  # raw y 1.0 + the final floor shift 0.5
    assert qy == pytest.approx(math.sin(math.radians(45)))  # block 4 faces 90 deg
    second = 8 + hdr + 3 * 12 + 3 * 4
    _, _, py2, _, qx2, qy2, qz2, qw2, _, _ = struct.unpack_from(a.FPRS_FRAME, data, second)
    assert py2 == pytest.approx(1.5) and qy2 == pytest.approx(0)  # block 5 faces 0 deg
    assert "ceiling not seen: look up" in res["warnings"]


def test_capture_ignores_a_leftover_block(monkeypatch, tmp_path):
    # passthrough off: the queue still hands out its last block once; that is no scan (owner's run, 2026-10-16)
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "_RoomViewReader", lambda app_type=None: FakeReader(app_type, blocks=1))
    clock = iter(i * 0.05 for i in range(10000))
    monkeypatch.setattr(a.time, "time", lambda: next(clock))
    res = a.capture_roomview(3, str(tmp_path / "scan.fprs"), max_hz=100)
    assert res["frames"] == 0 and res["warnings"][0].startswith("no passthrough")


def test_prompt_asks_for_passthrough_whenever_the_stream_stops(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    assert a._prompt_for(5.0, {0, 1}, {"up": 0, "down": 0}, streaming=False) == "start"


def test_capture_worker_status(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "capture_roomview", lambda *args, **kw: {"frames": 0, "warnings": []})
    a.room_capture_worker(json.dumps({"seconds": 10, "wait": 0}))
    st = a.cmd_room_capture_status({})
    assert st["state"] == "failed" and "passthrough" in st["error"]
    monkeypatch.setattr(a, "capture_roomview", lambda *args, **kw: {"frames": 12, "warnings": []})
    a.room_capture_worker(json.dumps({"seconds": 10, "wait": 0}))  # a scrap of a scan never replaces a room
    assert a.cmd_room_capture_status({})["state"] == "failed"

    built = {"report": {"walls": 4}, "room": {"universe": 7}}
    monkeypatch.setattr(a, "capture_roomview", lambda *args, **kw: {"frames": 40, "warnings": []})
    monkeypatch.setattr(a, "cmd_room_build", lambda args: built)
    a.room_capture_worker(json.dumps({"seconds": 10, "wait": 0}))
    st = a.cmd_room_capture_status({})
    assert st["state"] == "done" and st["report"] == {"walls": 4}


class ScanFrame:
    home = "/fake-frame"

    def __init__(self, states):
        self.states = list(states)
        self.calls = []
        self.puts = []
        self.sums = ""

    def agent(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd == "room_capture_status":
            return self.states.pop(0)
        return {"started": True}

    def run(self, cmd):
        return 0, self.sums, ""

    def put(self, local, remote, resume=True):
        self.puts.append((Path(local).name, remote))


def test_scan_polls_until_done(monkeypatch):
    monkeypatch.setattr(rooms, "ensure_scanner", lambda frame: "x")
    monkeypatch.setattr(rooms, "ensure_prompts", lambda frame: "x")
    frame = ScanFrame([{"state": "waiting"}, {"state": "recording", "directions": 4}, {"state": "done", "room": {}}])
    seen = []
    st = rooms.scan(frame, seconds=30, wait=60, progress=seen.append, sleep=lambda s: None)
    assert st["state"] == "done" and [s["state"] for s in seen] == ["waiting", "recording", "done"]
    assert frame.calls[0] == ("room_capture", {"seconds": 30, "wait": 60})

    frame = ScanFrame([{"state": "failed", "error": "No passthrough view arrived"}])
    with pytest.raises(RuntimeError, match="No passthrough"):
        rooms.scan(frame, sleep=lambda s: None)


def test_ensure_prompts_uploads_only_changed(monkeypatch, tmp_path):
    import hashlib

    (tmp_path / "roomscan").mkdir()
    for name in ("a.png", "b.png"):
        (tmp_path / "roomscan" / name).write_bytes(name.encode())
    monkeypatch.setattr(rooms, "artifacts_dir", lambda: tmp_path)
    frame = ScanFrame([])
    frame.sums = hashlib.sha256(b"a.png").hexdigest() + "  a.png\n" + "0" * 64 + "  b.png\n"
    rooms.ensure_prompts(frame)
    assert [p[0] for p in frame.puts] == ["b.png"]
