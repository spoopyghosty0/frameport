"""The hosted build runs exactly the reconstruction sources shipped in fp_roomscan."""
from __future__ import annotations

import importlib.util
import json
import math
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("synth_scan", ROOT / "scripts/mr/synth_scan.py")
synth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(synth)


@pytest.fixture(scope="module")
def scanner(tmp_path_factory):
    compiler = shutil.which("cc") or shutil.which("clang")
    if not compiler:
        pytest.skip("roomscan tests need a host C compiler")
    binary = tmp_path_factory.mktemp("roomscan-build") / "fp_roomscan"
    subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-ffp-contract=off",
                    "-fno-math-errno", "-DFP_HOSTED",
                    *map(str, sorted((ROOT / "native/roomscan").glob("*.c"))), "-o", str(binary)],
                   check=True, capture_output=True, text=True)
    return binary


@pytest.fixture(scope="module")
def dumps(tmp_path_factory):
    directory = tmp_path_factory.mktemp("depth-meshes")
    cases = {"box": {}, "l": {"shape": "l"}, "down": {"no_ceiling": True}, "one": {"frames": 1},
             "rotated": {"yaw": math.radians(27), "offset": (.3, .1, -.2)},
             "missing": {"missing_wall": 1, "objects": False}}
    result = {}
    for name, options in cases.items():
        result[name] = directory / f"{name}.fprs"
        synth.render(result[name], profile="legacy", **options)
    return result


def run(scanner, dump, directory, *args, success=True):
    proc = subprocess.run([str(scanner), "--offline", str(dump), "--out", str(directory), "--json", *args],
                          text=True, capture_output=True, timeout=15)
    assert proc.returncode == (0 if success else 2), proc.stdout + proc.stderr
    assert len(proc.stdout.splitlines()) == 1
    report = json.loads(proc.stdout)
    assert report["ok"] is success
    return report


def room(directory):
    records = {}
    lines = (directory / "room.txt").read_text().splitlines()
    assert lines[0] == "frameport-room 1"
    for line in lines:
        if not line or line.startswith("#"):
            continue
        kind, *values = line.split()
        records.setdefault(kind, []).append(values)
    return records


def outline(records):
    n, *values = records["outline"][0]
    assert len(values) == 2 * int(n)
    values = list(map(float, values))
    return list(zip(values[::2], values[1::2], strict=False))


def area(polygon):
    return sum(ax * bz - bx * az for (ax, az), (bx, bz) in zip(polygon, polygon[1:] + polygon[:1], strict=False)) / 2


def assert_walls(polygon, expected):
    assert len(polygon) == len(expected)
    assert area(polygon) > 0
    # Corner positions and segment orientation are checked independently.
    for x, z in expected:
        assert min(math.hypot(x - a, z - b) for a, b in polygon) < .05
    expected_angles = [math.atan2(bz - az, bx - ax)
                       for (ax, az), (bx, bz) in zip(expected, expected[1:] + expected[:1], strict=False)]
    for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=False):
        heading = math.atan2(b[1] - a[1], b[0] - a[0])
        assert min(abs(math.atan2(math.sin(heading - e), math.cos(heading - e)))
                   for e in expected_angles) < math.radians(2)
        dx, dz = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dz)
        assert synth.inside((a[0] + b[0]) / 2 - .04 * dz / length,
                            (a[1] + b[1]) / 2 + .04 * dx / length, polygon)


def mesh(directory):
    data = (directory / "room-mesh.bin").read_bytes()
    magic, version, nv, ni = struct.unpack_from("<4sIII", data)
    assert (magic, version) == (b"FPRM", 1)
    assert 0 < ni // 3 <= 200_000 and ni % 3 == 0
    assert len(data) == 16 + nv * 12 + ni * 4
    vertices = list(struct.iter_unpack("<fff", data[16:16 + nv * 12]))
    indices = struct.unpack_from(f"<{ni}I", data, 16 + nv * 12)
    assert all(math.isfinite(c) for vertex in vertices for c in vertex)
    assert max(indices) < nv
    return vertices, indices


def test_box_room_geometry_furniture_and_wire_format(scanner, dumps, tmp_path):
    report = run(scanner, dumps["box"], tmp_path, "--universe", "18446744073709551615")
    assert report["walls"] == report["detected_walls"] == 4
    assert all(w == "out-of-range depth vertices skipped" for w in report["warnings"])
    assert report["coverage_percent"] == 100
    assert abs(report["floor_y"]) < .05
    assert abs(report["ceiling_y"] - 2.6) < .05
    records = room(tmp_path)
    assert records["universe"] == [["18446744073709551615"]]
    assert records["source"] == [["scan"]]
    assert_walls(outline(records), synth.BOX_OUTLINE)
    tables = [v for v in records["volume"] if v[1] == "TABLE"]
    assert len(tables) == 1
    _, _, x, z, top, yaw, width, depth, height = tables[0]
    assert abs(float(x) + .6) < .07 and abs(float(z) + .25) < .07
    assert abs(float(top) - .75) < .05
    assert abs(float(width) - 1.2) < .08 and abs(float(depth) - .7) < .08
    assert abs(float(height) - .75) < .08
    assert abs(float(yaw)) < 2
    assert any(v[1] == "COUCH" for v in records["volume"])
    ids = [row[0] for kind in ("room", "floor", "ceiling", "wall", "volume", "mesh") for row in records[kind]]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch("[0-9a-f]{32}", value) for value in ids)
    vertices, indices = mesh(tmp_path)
    assert records["mesh"][0][1:] == ["room-mesh.bin", str(len(vertices)), str(len(indices))]
    # Normals on the structural surfaces must face the room interior.
    correct = checked = 0
    for i in range(0, len(indices), 3):
        a, b, c = [vertices[j] for j in indices[i:i + 3]]
        centre = [sum(coords) / 3 for coords in zip(a, b, c, strict=False)]
        ab, ac = [v - u for u, v in zip(a, b, strict=False)], [v - u for u, v in zip(a, c, strict=False)]
        normal = (ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2],
                  ab[0] * ac[1] - ab[1] * ac[0])
        x, y, z = centre
        component = None
        if abs(y) < .03:
            component = normal[1]
        elif abs(y - 2.6) < .03:
            component = -normal[1]
        elif abs(abs(x) - 2) < .02:
            component = -x * normal[0]
        elif abs(abs(z) - 1.5) < .02:
            component = -z * normal[2]
        if component is not None:
            checked += 1
            correct += component > 0
    assert checked > 5000
    assert correct / checked > .99
    assert all(value >= 0 for value in report["timings_ms"].values())


def test_l_shape_preserves_reentrant_corner(scanner, dumps, tmp_path):
    report = run(scanner, dumps["l"], tmp_path)
    assert report["detected_walls"] == 6
    polygon = outline(room(tmp_path))
    assert_walls(polygon, synth.L_OUTLINE)
    assert abs(area(polygon) - 12) < .15
    assert not synth.inside(1, 1, polygon)


def test_pose_transform_and_non_axis_aligned_walls(scanner, dumps, tmp_path):
    report = run(scanner, dumps["rotated"], tmp_path)
    yaw = math.radians(27)
    expected = [(math.cos(yaw) * x + math.sin(yaw) * z + .3,
                 -math.sin(yaw) * x + math.cos(yaw) * z - .2) for x, z in synth.BOX_OUTLINE]
    assert_walls(outline(room(tmp_path)), expected)
    assert abs(report["floor_y"] - .1) < .05
    assert abs(report["ceiling_y"] - 2.7) < .05


def test_unseen_ceiling_uses_relative_default(scanner, dumps, tmp_path):
    report = run(scanner, dumps["down"], tmp_path, "--ceiling-default", "2.7")
    assert "ceiling not seen" in report["warnings"]
    assert abs(report["ceiling_y"] - report["floor_y"] - 2.7) < .001


def test_one_frame_warns_and_still_emits_finite_room(scanner, dumps, tmp_path):
    report = run(scanner, dumps["one"], tmp_path)
    assert report["frames"] == 1
    assert any("few frames" in warning for warning in report["warnings"])
    assert 0 <= report["coverage_percent"] <= 100
    assert len(outline(room(tmp_path))) >= 3
    mesh(tmp_path)


def test_missing_wall_is_closed_and_circle_hint_is_ignored(scanner, dumps, tmp_path):
    hint = tmp_path / "circle.txt"
    hint.write_text("".join(f"{math.cos(i * math.tau / 32)} {math.sin(i * math.tau / 32)}\n" for i in range(32)))
    out = tmp_path / "out"
    report = run(scanner, dumps["missing"], out, "--hint", str(hint))
    assert not report["hint_used"]
    assert "default circle hint ignored" in report["warnings"]
    assert report["coverage_percent"] < 100
    assert "INVISIBLE_WALL_FACE" in (out / "room.txt").read_text()
    labels = [row[7] if len(row) == 8 else "WALL_FACE" for row in room(out)["wall"]]
    assert "INVISIBLE_WALL_FACE" in labels and "WALL_FACE" in labels
    assert area(outline(room(out))) > 8


def test_matching_hint_fills_missing_wall(scanner, dumps, tmp_path):
    hint = tmp_path / "outline.txt"
    hint.write_text("# clockwise hint is also accepted\n" + "".join(f"{x} {z}\n" for x, z in synth.BOX_OUTLINE[::-1]))
    report = run(scanner, dumps["missing"], tmp_path / "out", "--hint", str(hint))
    assert report["hint_used"]
    assert report["coverage_percent"] < 100
    assert_walls(outline(room(tmp_path / "out")), synth.BOX_OUTLINE)


@pytest.mark.parametrize("cut", [0, 3, 7, 8, 9, 51, 52, 60, -1])
def test_truncated_dump_has_no_outputs(scanner, dumps, tmp_path, cut):
    data = dumps["one"].read_bytes()
    dump = tmp_path / "broken.fprs"
    dump.write_bytes(data[:cut])
    out = tmp_path / "out"
    report = run(scanner, dump, out, success=False)
    assert report["error"]
    assert not out.exists()


@pytest.mark.parametrize(("offset", "replacement"), [
    (0, b"NOPE"), (4, struct.pack("<I", 2)), (12, struct.pack("<I", 0x7ff00000)),
    (16, struct.pack("<f", float("nan"))), (40, struct.pack("<f", 50)),
    (44, struct.pack("<I", 0xffffffff)), (48, struct.pack("<I", 4)),
    (52, struct.pack("<f", float("inf"))),
])
def test_corrupt_dump_is_rejected_before_publishing(scanner, dumps, tmp_path, offset, replacement):
    data = bytearray(dumps["one"].read_bytes())
    data[offset:offset + len(replacement)] = replacement
    dump = tmp_path / "bad.fprs"
    dump.write_bytes(data)
    out = tmp_path / "out"
    out.mkdir()
    # An invalid replacement must preserve a previously installed room, byte for byte.
    for name in ("room.txt", "room-mesh.bin"):
        (out / name).write_bytes(b"previous room")
    run(scanner, dump, out, success=False)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        "room.txt": b"previous room", "room-mesh.bin": b"previous room"}


def test_bad_index_and_trailing_partial_frame(scanner, dumps, tmp_path):
    data = bytearray(dumps["one"].read_bytes())
    nv = struct.unpack_from("<I", data, 44)[0]
    struct.pack_into("<I", data, 52 + nv * 12, nv)
    bad = tmp_path / "bad-index.fprs"
    bad.write_bytes(data)
    assert "index" in run(scanner, bad, tmp_path / "out", success=False)["error"]
    bad.write_bytes(dumps["one"].read_bytes() + b"\0")
    assert "truncated" in run(scanner, bad, tmp_path / "out", success=False)["error"]


@pytest.mark.parametrize("args", [("--voxel", "0"), ("--voxel", ".01"), ("--grazing", "nan"),
                                  ("--universe", "18446744073709551616"), ("--surprise", "1")])
def test_invalid_options_are_json_errors(scanner, dumps, tmp_path, args):
    run(scanner, dumps["one"], tmp_path / "out", *args, success=False)


def test_coarser_voxels_and_repeatable_geometry(scanner, dumps, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    a = run(scanner, dumps["box"], first, "--voxel", ".08")
    b = run(scanner, dumps["box"], second, "--voxel", ".08")
    assert a["walls"] == b["walls"] == 4
    assert (first / "room-mesh.bin").read_bytes() == (second / "room-mesh.bin").read_bytes()
    assert room(first)["room"] != room(second)["room"]  # fresh OS-random anchors on every scan
    mesh(first)


@pytest.mark.parametrize("offset", [12, 16, 28, 52])
def test_nonfinite_frame_is_skipped_before_and_after_valid_frame(scanner, dumps, tmp_path, offset):
    data = dumps["one"].read_bytes()
    bad = bytearray(data[8:])
    # High word of timestamp, pose position/quaternion, or a vertex; body offsets exclude FPRS header.
    struct.pack_into("<I", bad, offset - 8, 0x7fffffff)
    dump = tmp_path / "tracking-start.fprs"
    dump.write_bytes(data[:8] + bad + data[8:] + bad)
    report = run(scanner, dump, tmp_path / "out")
    assert report["frames"] == 3 and report["skipped_frames"] == 2
    assert "non-finite or invalid frames skipped" in report["warnings"]
    assert len(outline(room(tmp_path / "out"))) >= 3


def test_far_vertices_are_dropped_per_vertex(scanner, dumps, tmp_path):
    data = bytearray(dumps["one"].read_bytes())
    # A single inpainted vertex must remove every incident triangle, without losing its whole frame.
    struct.pack_into("<3f", data, 52, 0, 0, 9)
    dump = tmp_path / "far.fprs"
    dump.write_bytes(data)
    report = run(scanner, dump, tmp_path / "out")
    assert report["skipped_frames"] == 0 and report["far_vertices"] >= 1
    assert report["rejected_triangles"] >= 1


@pytest.fixture(scope="module")
def roomview_dump(tmp_path_factory):
    path = tmp_path_factory.mktemp("roomview-fan") / "synthetic.fprs"
    synth.render(path)
    return path


def frames(path):
    with path.open("rb") as stream:
        assert stream.read(8) == b"FPRS\x01\0\0\0"
        while header := stream.read(44):
            timestamp, *values = struct.unpack("<d7fII", header)
            pose, nv, ni = values[:7], values[7], values[8]
            vertices = list(struct.iter_unpack("<3f", stream.read(nv * 12)))
            indices = struct.unpack(f"<{ni}I", stream.read(ni * 4))
            yield timestamp, pose, vertices, indices


def test_roomview_renderer_grid_camera_axes_drift_and_inpainting(roomview_dump):
    heights, far = [], 0
    for i, (timestamp, pose, vertices, indices) in enumerate(frames(roomview_dump)):
        assert abs(timestamp - i / 3) < 1e-8
        assert len(vertices) == 132 * 128 and len(indices) == 99822
        assert indices[:6] == (0, 132, 1, 1, 132, 133)
        assert vertices[0][0] < 0 and vertices[0][1] < 0 and vertices[0][2] > 0
        assert vertices[-1][0] > 0 and vertices[-1][1] > 0 and vertices[-1][2] > 0
        if i < 2:
            assert not all(map(math.isfinite, pose))
        else:
            assert all(map(math.isfinite, pose))
            heights.append(pose[1])
        far += sum(sum(c * c for c in vertex) > 16 for vertex in vertices)
    assert .89 <= min(heights) <= .92 and 1.68 <= max(heights) <= 1.71
    assert far > 1000


def test_depth_smoothing_reduces_local_noise_without_changing_grid():
    depth = [1 + ((x * 31 + y * 17) % 7 - 3) * .04 for y in range(128) for x in range(132)]
    filtered = synth.smooth_depths(depth, 132, 128)
    assert len(filtered) == len(depth)
    assert sum((v - 1) ** 2 for v in filtered) < sum((v - 1) ** 2 for v in depth) * .1
    assert synth.smooth_depths([2.0] * (132 * 128), 132, 128) == [2.0] * (132 * 128)


def test_roomview_style_scan_retains_room_and_furniture(scanner, roomview_dump, tmp_path):
    report = run(scanner, roomview_dump, tmp_path)
    assert report["frames"] == 30 and report["skipped_frames"] == 2
    assert report["far_vertices"] > 1000
    assert report["detected_walls"] == 4
    assert abs(report["floor_y"]) < .05 and abs(report["ceiling_y"] - 2.6) < .05
    records = room(tmp_path)
    assert_walls(outline(records), synth.BOX_OUTLINE)
    assert any(row[1] == "TABLE" and abs(float(row[4]) - .75) < .05 for row in records["volume"])
    assert any(row[1] == "COUCH" for row in records["volume"])
    mesh(tmp_path)


def test_depth_discontinuity_is_rejected_below_absolute_edge_limit(scanner, dumps, tmp_path):
    data = dumps["one"].read_bytes()
    vertices = (0, 0, 1, .1, 0, 1, 0, .1, 1.5)
    frame = struct.pack("<d7fII", 0, 0, 1, 0, 0, 0, 0, 1, 3, 3)
    frame += struct.pack("<9f3I", *vertices, 0, 1, 2)
    path = tmp_path / "discontinuity.fprs"
    path.write_bytes(data[:8] + frame + data[8:])
    report = run(scanner, path, tmp_path / "out")
    assert report["discontinuity_triangles"] >= 1


def test_small_ceiling_patch_does_not_override_default(scanner, dumps, tmp_path):
    # A lone down-facing patch at shelf height (1.8 m, below a 2 m ceiling) is no ceiling. A patch at ceiling height
    # is accepted on purpose: the owner's real ceiling (2026-10-16) was only ~0.1 m^2 of fused cells after looking
    # up, less evidence than a 0.7 m patch seen ten times, so area or a share of the floor can't tell them apart.
    data = dumps["down"].read_bytes()
    width = 15
    vertices = [(x * .05 - .35, .3, z * .05 - .35) for z in range(width) for x in range(width)]
    indices = []
    for z in range(width - 1):
        for x in range(width - 1):
            a = z * width + x
            indices.extend((a, a + width, a + 1, a + 1, a + width, a + width + 1))
    frame = struct.pack("<d7fII", 0, 0, 1.5, 0, 0, 0, 0, 1, len(vertices), len(indices))
    frame += b"".join(struct.pack("<3f", *v) for v in vertices)
    frame += struct.pack(f"<{len(indices)}I", *indices)
    path = tmp_path / "weak-ceiling.fprs"
    path.write_bytes(data + frame * 10)
    report = run(scanner, path, tmp_path / "out", "--ceiling-default", "2.7")
    assert "ceiling not seen" in report["warnings"]
    assert abs(report["ceiling_y"] - report["floor_y"] - 2.7) < .001
