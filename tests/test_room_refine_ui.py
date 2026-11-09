"""Room scan clean-up and editing on the PC: install/rooms settings + refine/put_room, the mr/room editing helpers,
view3d's furniture overlay and picking, and the Room tab's Clean-up / Labels / Furniture cards (fake Frame)."""
import io
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from frameport.install import rooms
from frameport.mr import view3d
from frameport.mr.room import (
    Mesh,
    Volume,
    accept_suggestion,
    box_room,
    default_plane,
    dumps,
    nearest_wall,
    parse,
    plane_corners,
    plane_on_wall,
    plane_place,
    summary,
    write_mesh,
)


# ------------------------------------------------------------------------------------------------ settings
def test_refine_defaults_are_the_owners_and_normalize():
    d = rooms.refine_defaults()
    assert d == {"refine": 1, "walls": "panels", "walked": 0.4, "smooth": 8, "island": 0.2, "clip": 0.05,
                 "clip_open": 0.1, "band": 0.35, "band_in": 0.12, "floor_in": 0.08, "ceil_in": 0.25,
                 "top_strip": 0.5, "facing_deg": 60, "near": 0.03, "tubes": 3.0, "tube_thick": 0.15, "tops": 0.1,
                 "protect": 0, "label_rules": 1}
    for key, meta in rooms.REFINE_DEFAULTS.items():
        assert meta["flag"] == "--" + key.replace("_", "-") and meta["help"].startswith("refine_")
        if meta["kind"] == "number":
            assert meta["min"] <= meta["default"] <= meta["max"], key
    assert {k for k, m in rooms.REFINE_DEFAULTS.items() if m["key"]} == {"walls", "walked", "smooth", "island",
                                                                           "clip"}
    assert rooms.normalize_refine(d) == {}
    assert rooms.normalize_refine({"band": 0.351, "smooth": "4", "protect": "on", "walls": "snap", "clip": 7}) == \
        {"smooth": 4, "protect": 1, "walls": "snap", "clip": 1.0}  # 0.351 snaps to the default 0.35
    assert rooms.normalize_refine({"band": 0.356}) == {"band": 0.36}
    for bad in ({"walls": "flat"}, {"nope": 1}, {"band": "x"}):
        with pytest.raises(ValueError):
            rooms.normalize_refine(bad)
    assert rooms.current_refine({"refine": {"smooth": 2}})["smooth"] == 2
    assert rooms.current_refine(None) == d


class Frame:
    home = "/fake-frame"

    def __init__(self):
        self.files, self.calls, self.answer = {}, [], {"room_text": "x"}
        self.sftp = SimpleNamespace(open=lambda path, mode: io.BytesIO(self.files[path]))

    def run(self, cmd):
        return 0, "", ""

    def put(self, local, remote, **kwargs):
        self.files[remote] = Path(local).read_bytes()

    def agent(self, command, **args):
        self.calls.append((command, args))
        return self.answer


def test_refine_and_put_room_go_to_the_agent(monkeypatch):
    f = Frame()
    synced = []
    monkeypatch.setattr(rooms, "ensure_scanner", lambda frame: synced.append(1))
    rooms.refine(f, {"walls": "snap", "band": 0.35}, erase=[(0, 1, 0, 0.2)])
    assert synced and f.calls[-1] == ("room_refine", {"settings": {"walls": "snap"}, "keep_edits": True,
                                                      "erase": [[0.0, 1.0, 0.0, 0.2]]})
    rooms.refine(f, None, keep_edits=False)
    assert f.calls[-1] == ("room_refine", {"settings": None, "keep_edits": False})
    f.answer = {"error": "Room clean-up failed: boom"}
    with pytest.raises(RuntimeError, match="boom"):
        rooms.refine(f, {})
    # an edited room: the mesh comes from the Frame when not given, room_put says it's an edit
    f.answer = {"room": {}}
    r = box_room(4, 3)
    r.universe = 7
    m = write_mesh(Mesh(vertices=[(0, 0, 0), (1, 0, 0), (0, 0, 1)], indices=[0, 1, 2]))
    r.mesh = Mesh(vertex_count=3, index_count=3)
    f.files["/fake-frame/.local/share/frameport/rooms/7/room-mesh.bin"] = m
    r.volumes.append(Volume("c" * 32, "BED", 0, 0, 0.5, 0, 2, 1.4, 0.5))
    rooms.put_room(f, r)
    command, args = f.calls[-1]
    assert command == "room_put" and args["edited"] and args["path"].endswith("/room.txt")
    uploaded = {k.rsplit("/", 1)[1]: v for k, v in f.files.items() if "/.incoming-" in k}
    assert parse(uploaded["room.txt"].decode()).volumes[0].label == "BED" and uploaded["room-mesh.bin"] == m
    with pytest.raises(ValueError, match="changed"):
        rooms.put_room(f, r, write_mesh(Mesh(vertices=[(0, 0, 0)] * 4, indices=[0, 1, 2])))


def test_cli_room_refine(monkeypatch):
    from typer.testing import CliRunner

    from frameport import cli

    f = Frame()
    f.answer = {"refine": {"smooth": 4}, "report": {"kept": 1}, "room_text": "x"}
    monkeypatch.setattr(cli, "_target", lambda frame: SimpleNamespace(frame=f))
    monkeypatch.setattr(rooms, "ensure_scanner", lambda frame: None)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["frame", "room", "refine", "--set", "walls=snap", "--set", "island=0.5"])
    assert result.exit_code == 0, result.output
    assert f.calls[-1] == ("room_refine", {"settings": {"smooth": 4, "walls": "snap", "island": 0.5},
                                           "keep_edits": True})
    assert '"kept": 1' in result.output
    result = runner.invoke(cli.app, ["frame", "room", "refine", "--reset", "--scan-boxes"])
    assert f.calls[-1] == ("room_refine", {"settings": {}, "keep_edits": False})
    result = runner.invoke(cli.app, ["frame", "room", "refine"])
    assert f.calls[-1][1]["settings"] is None
    result = runner.invoke(cli.app, ["frame", "room", "refine", "--set", "walls=flat"])
    assert result.exit_code != 0
    result = runner.invoke(cli.app, ["frame", "room", "refine", "--list"])
    assert result.exit_code == 0 and "facing_deg" in result.output and "Where you walked" in result.output


def test_report_follows_the_scanner_keys():
    from frameport.ui.views.room_edit import report_notes, report_rows

    report = {"triangles": 1000, "kept": 800, "structural": 300, "near_walls": 5, "outside": 40, "small_pieces": 30,
              "thin_pieces": 2, "erased": 0, "walked": 20, "tops": 9, "panel_triangles": 12, "truncated": 0,
              "walked_skipped": True, "floor_y": -0.01, "ceiling_y": 2.5, "boxes": 6, "relabelled": 2,
              "removed_boxes": 1, "suggestions": 1, "plane_shifts": [{"kind": "wall", "wall": 0, "shift": 0.02}]}
    rows = dict(report_rows(report))
    assert rows["Wall, floor and ceiling surfaces"] == "300" and rows["Cleared near walls"] == "5"
    assert rows["Small pieces"] == "30" and rows["Thin floating pieces"] == "2"
    assert rows["Flat panel triangles"] == "12" and rows["Boxes renamed"] == "2" and rows["Kept"] == "800"
    assert not {"Floor y", "Ceiling y", "Walked skipped", "Truncated", "Plane shifts"} & set(rows)
    notes, warning = report_notes(report)
    assert notes == ["Where you walked: skipped, this scan has no head path"] and warning == ""
    assert "1,234" in report_notes({"truncated": 1234})[1]
    summary = dict(report_rows({"triangles": 9, "refine": {"output_triangles": 7, "output_volumes": 3}}))
    assert summary["Triangles in the room"] == "7" and summary["Furniture boxes in the room"] == "3"


# ------------------------------------------------------------------------------------------------ editing helpers
def test_planes_on_walls_follow_the_cleanup_page():
    room = box_room(4, 3)  # walls run counter-clockwise; their inward normal is (-uz, ux)
    w = room.walls[0]
    p = plane_on_wall(room, 0, 0.5, 0.0, 0.9, 2.05)
    ux, uz = (w.x1 - w.x0) / 4, (w.z1 - w.z0) / 4
    assert p.label == "DOOR_FRAME" and math.isclose(p.py, 2.05 / 2)
    mid = ((w.x0 + w.x1) / 2, (w.z0 + w.z1) / 2)
    assert math.isclose(p.px, mid[0] - uz * 0.01, abs_tol=1e-9) and math.isclose(p.pz, mid[1] + ux * 0.01, abs_tol=1e-9)
    # the quaternion turns +X onto the wall and +Z into the room
    th = 2 * math.atan2(p.qy, p.qw)
    assert math.isclose(math.cos(th), ux, abs_tol=1e-9) and math.isclose(-math.sin(th), uz, abs_tol=1e-9)
    c = plane_corners(p)
    assert math.isclose(math.dist(c[0], c[1]), 0.9) and math.isclose(c[3][1] - c[0][1], 2.05)
    edge = plane_on_wall(room, 0, 0.0, 0, 0.9, 2)  # kept fully on the wall
    assert math.isclose(math.dist((edge.px, edge.pz), (w.x0, w.z0)), math.hypot(0.45, 0.01), rel_tol=1e-6)
    place = plane_place(room, p)
    assert place["wall"] == 0 and math.isclose(place["along"], 0.5, abs_tol=0.01) and math.isclose(place["bottom"], 0)
    window = default_plane(room, "WINDOW_FRAME")
    assert window.label == "WINDOW_FRAME" and math.isclose(plane_place(room, window)["bottom"], 0.9)
    near = nearest_wall(room, 0, -1.4)
    assert near["d"] < 0.11
    room.planes.append(p)
    assert parse(dumps(room)).planes[0].label == "DOOR_FRAME"


def test_suggestions_become_doors_or_closets():
    room = box_room(4, 3)
    sug = {"kind": "door", "box": {"cx": 1.95, "cz": 0, "top_y": 2.0, "yaw_deg": 90, "w": 1.0, "d": 0.1, "h": 2.0},
           "wall": 1, "along": 0.5}
    door = accept_suggestion(room, sug, "door")
    assert room.planes == [door] and door.label == "DOOR_FRAME" and door.w == 1.0 and math.isclose(door.h, 2.0)
    assert plane_place(room, door)["wall"] == 1
    closet = accept_suggestion(room, dict(sug, wall=None), "closet")
    assert room.volumes == [closet] and closet.label == "STORAGE" and closet.d == 0.6 and closet.h == 2.0
    corners = view3d.box_corners(closet.cx, closet.cz, closet.yaw_deg, closet.w, closet.d)
    assert max(x for x, _ in corners) <= 2.0 + 1e-6  # against the wall, inside the room
    assert parse(dumps(room)).volumes[0].label == "STORAGE"


def test_with_furniture_and_pick():
    room = box_room(4, 3)
    room.volumes.append(Volume("1" * 32, "TABLE", 0, 0, 0.75, 0, 1, 1, 0.75))
    scene = view3d.prepare(room)
    cam = view3d.Camera(width=800, height=480)
    cam.fit(scene)
    x, y = cam.project([(0, 0.75, 0)])[0]
    assert view3d.pick(scene, cam, x, y) == ("box", 0)
    assert view3d.pick(scene, cam, 2, 2) is None
    edited = box_room(4, 3)
    edited.volumes = [Volume("1" * 32, "BED", 1, 0.5, 0.5, 30, 2, 1.4, 0.5)]
    edited.planes = [plane_on_wall(edited, 0, 0.5, 0, 0.9, 2.05)]
    sc = view3d.with_furniture(scene, edited, ("plane", 0))
    assert sc is not scene and len([f for f in scene.faces if f.kind == "box"]) == 6  # the cache isn't touched
    assert [f.index for f in sc.faces if f.kind == "box"] == [0] * 6 and len(sc.planes) == 1
    assert len(sc.selected) == 4
    frame = view3d.render(sc, cam)
    assert frame.planes and frame.selected
    px, py = cam.project([(edited.planes[0].px, edited.planes[0].py, edited.planes[0].pz)])[0]
    assert view3d.pick(sc, cam, px, py) == ("plane", 0)


# ------------------------------------------------------------------------------------------------ the Room tab
class RefineFrame:
    """A Frame with a scanned room, its clean-up results, room_refine / room_put and the mesh over SFTP."""

    def __init__(self, tmp_path):
        self.home = str(tmp_path / "frame")
        self.room = box_room(4, 3)
        self.room.source, self.room.universe = "scan", 7
        self.room.volumes = [Volume("1" * 32, "TABLE", 0, 0, 0.75, 0, 1.2, 0.6, 0.75),
                             Volume("2" * 32, "SCREEN", 0, -1.4, 1.4, 0, 1.2, 0.1, 0.7)]
        self.mesh = write_mesh(Mesh(vertices=[(-1, 0, -1), (1, 0, -1), (1, 0, 1)], indices=[0, 2, 1]))
        self.room.mesh = Mesh(vertex_count=3, index_count=3)
        self.folder = tmp_path / "frame/.local/share/frameport/rooms/7"
        self.folder.mkdir(parents=True)
        (self.folder / "room-mesh.bin").write_bytes(self.mesh)
        self.saved, self.calls, self.files = {}, [], {}
        self.report = {"triangles": 1200, "kept": 900, "walked": 40}

    def agent(self, command, **kw):
        self.calls.append((command, kw))
        if command == "room_refine":
            if kw.get("settings") is not None:
                self.saved = kw["settings"]
            self.room.time += 1
            self.report = {"triangles": 1200, "kept": 850, "walked": 60}
        if command == "room_put":
            self.room = parse(Path(kw["path"]).read_text())
            self.edited = True
        return dict(room_text=dumps(self.room), room=dict(source=self.room.source, summary=summary(self.room)),
                    aligned=True, scanner_installed=True, default_circle=False, refine=dict(self.saved),
                    report=self.report, edited=getattr(self, "edited", False),
                    labels=[{"from": "COUCH", "to": "TABLE", "reason": "top at 0.75 m: a table",
                             "box": {"w": 1.2, "d": 0.6, "h": 0.75}},
                            {"from": "TABLE", "to": None, "reason": "behind a wall", "box": {}}],
                    suggestions=[{"kind": "closet", "box": {"cx": 1.9, "cz": 0.5, "top_y": 2.0, "yaw_deg": 90,
                                                            "w": 1.0, "d": 0.3, "h": 2.0}, "wall": 1,
                                  "along": 0.6}])

    def run(self, cmd):
        return 0, "", ""

    def put(self, local, remote, **kw):  # uploads land in a local folder (the agent reads room.txt from there)
        Path(remote).parent.mkdir(parents=True, exist_ok=True)
        Path(remote).write_bytes(Path(local).read_bytes())

    @property
    def sftp(self):
        class SFTP:
            def open(self, path, mode="rb"):
                return open(path, mode)  # noqa: SIM115
        return SFTP()


def refine_app(frame):
    from test_ui_rooms import sync_app

    app = sync_app(frame)
    jobs = []

    def submit(title, run, *a, **kw):
        job = SimpleNamespace(title=title, reporter=SimpleNamespace(stage=lambda *a: None))
        jobs.append(title)
        job.result = run(job)
        return job
    app.submit, app.jobs_run = submit, jobs
    return app


def test_room_tab_cleanup_labels_and_editor(tmp_path, monkeypatch):
    from frameport.ui.views.room import RoomView

    monkeypatch.setattr(rooms, "ensure_scanner", lambda frame: None)
    monkeypatch.setattr(rooms, "_upload_dir", lambda frame: _local_dir(tmp_path))
    frame = RefineFrame(tmp_path)
    app = refine_app(frame)
    view = RoomView(app)
    view.mount()
    cleanup, labels, editor = view.cleanup, view.labels, view.editor
    # clean-up: the defaults, the key settings in sight, the rest under More settings
    assert cleanup.preset.data == "recommended" and cleanup.walls.data == "panels" and not cleanup.more.visible
    assert cleanup.readouts["walked"].value == "40 cm" and cleanup.readouts["island"].value == "0.20 m²"
    assert cleanup.readouts["smooth"].value == "8×" and not cleanup.apply_btn.disabled
    assert cleanup.result_box.visible and any("1,200" in r.controls[1].value for r in cleanup.result.controls)
    cleanup.toggle_more()
    assert cleanup.more.visible and set(cleanup.sliders) == {k for k, m in rooms.REFINE_DEFAULTS.items()
                                                              if m["kind"] == "number"}
    cleanup._slid("smooth", 4.2)
    cleanup.set("walls", "snap")
    assert cleanup.preset.data == "mine" and cleanup.sliders["near"].disabled
    cleanup._preset("recommended")
    assert cleanup.values == rooms.refine_defaults()
    cleanup._preset("mine")
    assert cleanup.values["smooth"] == 4 and cleanup.values["walls"] == "snap"
    cleanup.apply()
    command, args = [c for c in frame.calls if c[0] == "room_refine"][-1]
    assert command == "room_refine" and args["settings"] == {"smooth": 4, "walls": "snap"}
    assert app.jobs_run == ["Clean up the room scan"] and not app._room_state["working"]
    view.refresh(update=False)
    assert cleanup.values["smooth"] == 4 and any("850" in r.controls[1].value for r in cleanup.result.controls)
    # labels: the scanner's label -> the rule's, the reason, and a suggestion to take
    assert len(labels.rows.controls) == 2 and len(labels.suggestions.controls) == 1 and not labels.empty.visible
    texts = [t.value for t in _walk(labels.rows) if isinstance(getattr(t, "value", None), str)]
    assert "Couch" in texts and "→ Table" in texts and "removed" in texts and "behind a wall" in texts
    # the box editor: select, change, add a door, take the closet suggestion, save
    assert len(editor.box_list.controls) == 2 and not editor.box_panel.visible
    editor.select(("box", 0))
    assert editor.box_panel.visible and editor.box_type.value == "TABLE"
    assert editor.box_sliders["cx"].min == -3 and editor.box_sliders["cx"].max == 3
    editor._box_value("w", 1.6)
    editor._box_label("COUCH")
    assert editor.dirty and editor.room.volumes[0].w == 1.6 and editor.room.volumes[0].label == "COUCH"
    assert view.canvas.scene.selected  # the canvas shows the edited, selected box
    editor._add_plane("DOOR_FRAME")
    assert editor.plane_panel.visible and editor.plane_wall.value == "0" and len(editor.plane_list.controls) == 1
    editor._plane_value("along", 0.25)
    editor._plane_wall("2")
    p = editor.room.planes[0]
    assert plane_place(editor.room, p)["wall"] == 2 and math.isclose(plane_place(editor.room, p)["along"], 0.25,
                                                                      abs_tol=0.02)
    labels.refresh(app._room_state, app._room_state["status"], app._room_state["status"]["room"])
    editor.accept(frame.agent("room_status")["suggestions"][0], "closet")
    assert editor.room.volumes[-1].label == "STORAGE" and editor.sel == ("box", 2)
    editor._delete()
    assert len(editor.room.volumes) == 2 and editor.sel is None
    assert not editor.save_btn.disabled
    editor.save()
    command, args = [c for c in frame.calls if c[0] == "room_put"][-1]
    assert command == "room_put" and args["edited"]
    saved = frame.room
    assert [v.label for v in saved.volumes] == ["COUCH", "SCREEN"] and saved.planes[0].label == "DOOR_FRAME"
    view.refresh(update=False)
    assert not editor.dirty and editor.room.volumes[0].label == "COUCH"  # reloaded from the saved room
    assert labels.reset_btn.visible  # an edited room can go back to the scan's boxes
    # a click on the canvas selects what's under it
    view.canvas.on_pick(("plane", 0))
    assert editor.sel == ("plane", 0)


def test_cleanup_waits_for_a_scanned_room(tmp_path):
    from frameport.ui.views.room import RoomView

    frame = RefineFrame(tmp_path)
    frame.room.source = "boundary"
    app = refine_app(frame)
    view = RoomView(app)
    view.mount()
    assert view.cleanup.apply_btn.disabled and "scanned" in view.cleanup.state_text.value


def _walk(control):
    yield control
    kids = list(getattr(control, "controls", None) or [])
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        kids.append(content)
    for child in kids:
        yield from _walk(child)


class _local_dir:
    def __init__(self, tmp_path):
        self.path = tmp_path / "frame-upload"

    def __enter__(self):
        self.path.mkdir(exist_ok=True)
        return str(self.path)

    def __exit__(self, *a):
        return False


def test_box_sizes_keep_thin_sizes_readable():
    from frameport.ui.views.room_edit import metres, size_text

    assert metres(0.03) == "0.03" and metres(0.07) == "0.07" and metres(0.5) == "0.5" and metres(2.05) == "2.0"
    assert size_text(1.6, 1.1, 0.03) == "1.6×1.1×0.03 m"


def test_slider_range_keeps_its_value_inside():  # GitHub #559
    import flet as ft

    from frameport.ui.views.room_edit import set_range

    s = ft.Slider(min=-5, max=5, value=0)
    set_range(s, 0.333, 4.2, 0.01)  # a room whose outline starts past 0 m
    assert (s.min, s.max, s.value) == (0.33, 4.2, 0.33) and s.divisions == 387
    set_range(s, -3, -1, 0.01)
    assert s.value == -1
    s.value = None
    set_range(s, 1, 2, 0.01)
    assert s.value == 1
