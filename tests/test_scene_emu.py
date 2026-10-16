"""Offline host tests of the real FrameBridge scene implementation and on-disk room contract."""
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def uuid(n):
    return f"{n:032x}"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc or os.name == "nt" or not Path("/proc/self/cmdline").exists():
        pytest.skip("needs a POSIX host C compiler and /proc")
    inc = next((p for p in sorted((ROOT / "native/.cache").glob("openxr-*"))
                if "#define XR_META_spatial_entity_discovery 1" in (p / "openxr/openxr.h").read_text()), None)
    if inc is None:
        pytest.skip("needs the cached OpenXR headers with META discovery (native/build.py)")
    d = tmp_path_factory.mktemp("scene-host")
    binary = d / "scene-test"
    external = d / "external"
    external.mkdir()
    subprocess.run([cc, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                    f'-DSCENE_EXTERNAL_ROOT="{external}"', "-I", str(inc), "-I", str(ROOT / "native/adapter"),
                    str(ROOT / "tests/native/scene_emu_test.c"), "-pthread", "-lm", "-o", str(binary)], check=True)
    return binary, external


def run(harness, folder, mode, *args, process="org.frameport.test"):
    binary, _ = harness
    env = {**os.environ, "FRAMEBRIDGE_CONFIG": str(folder / "settings.conf")}
    result = subprocess.run([process, mode, *map(str, args)], executable=binary, capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stderr


def room(folder, *, generated_walls=False):
    points = [(0, 0), (4, 0), (4, 3), (2, 4), (0, 3)]
    lines = ["frameport-room 1 # format", "source scan", "universe 1234", f"room {uuid(1)}",
             f"floor {uuid(2)} 0", f"ceiling {uuid(3)} 2.5",
             "outline 5 " + " ".join(f"{x} {z}" for x, z in points),
             # Interleave furniture and walls to test layout ordering and handle assignment.
             f"volume {uuid(40)} TABLE,OTHER 2 1 0.75 90 2 1 0.75",
             f"plane {uuid(41)} WINDOW_FRAME 1 1.5 0 0 0 0 1 0.8 1.2"]
    if not generated_walls:
        for i, ((x, z), (xx, zz)) in enumerate(zip(points, points[1:] + points[:1])):
            lines.append(f"wall {uuid(10 + i)} {x} {z} {xx} {zz} 0 2.5")
    lines += [f"mesh {uuid(42)} room-mesh.bin 4 6", "future-data ignored"]
    (folder / "room.txt").write_text("\n".join(lines) + "\n")
    verts = [(0, 0, 0), (4, 0, 0), (4, 0, 3), (0, 0, 3)]
    mesh = b"FPRM" + struct.pack("<III", 1, 4, 6)
    mesh += b"".join(struct.pack("<fff", *v) for v in verts) + struct.pack("<6I", 0, 1, 2, 0, 2, 3)
    (folder / "room-mesh.bin").write_bytes(mesh)


def test_room_geometry_components_queries_and_mesh(harness, tmp_path):
    room(tmp_path)
    log = run(harness, tmp_path, "room", 5, 1)
    assert "scene: room from scan file (5 walls, 1 volumes, 1 planes, 2 mesh triangles, universe 1234)" in log
    assert "scene: capture requested -> scanned room" in log


def test_no_global_mesh_without_the_mesh_extension(harness, tmp_path, monkeypatch):
    # an app that didn't enable XR_META_spatial_entity_mesh can't place a GLOBAL_MESH anchor (older SDKs then failed
    # the whole scene load): the room comes without it
    room(tmp_path)
    monkeypatch.setenv("SCENE_NO_MESH_EXT", "1")
    log = run(harness, tmp_path, "room", 5, 0)
    assert "scene: room from scan file (5 walls, 1 volumes, 1 planes, 0 mesh triangles, universe 1234)" in log


def test_wall_label_field(harness, tmp_path):
    # optional 8th field: INVISIBLE_WALL_FACE for edges the scanner closed itself; other labels are refused
    room(tmp_path)
    text = (tmp_path / "room.txt").read_text()
    last = f"wall {uuid(14)} 0 3 0 0 0 2.5"
    assert last in text
    (tmp_path / "room.txt").write_text(text.replace(last, last + " INVISIBLE_WALL_FACE")
                                       + f"wall {uuid(70)} 0 0 1 0 0 2 TABLE\n")
    log = run(harness, tmp_path, "room", 5, 1)
    assert "scene: room from scan file (5 walls" in log and log.count("bad room line") == 1


def test_walls_derived_from_outline(harness, tmp_path):
    room(tmp_path, generated_walls=True)
    run(harness, tmp_path, "room", 5, 1)


def test_bad_lines_are_skipped_without_losing_valid_geometry(harness, tmp_path):
    room(tmp_path)
    with (tmp_path / "room.txt").open("a") as f:
        f.write(f"volume {uuid(60)} TABLE nan 0 1 0 1 1 1\n")
        f.write(f"wall {uuid(61)} 0 0 0 0 0 3\n")
        f.write(f"plane {uuid(62)} WALL_ART 0 0 0 0 0 0 0 1 1\n")
        f.write("outline 5 99 99 invalid 0 0 0 0 0 0 0\n")
        f.write("universe -1\n" + "outline " + "x" * 70000 + "\n")
        f.write(f"volume {uuid(40)} DUPLICATE 0 0 1 0 1 1 1\n")
    log = run(harness, tmp_path, "room", 5, 1)
    assert log.count("bad room line") == 7


@pytest.mark.parametrize("damage", ["magic", "version", "truncated", "counts", "index", "nonfinite", "trailing"])
def test_rejects_corrupt_mesh_without_losing_room(harness, tmp_path, damage):
    room(tmp_path)
    p = tmp_path / "room-mesh.bin"
    data = bytearray(p.read_bytes())
    if damage == "magic":
        data[:4] = b"NOPE"
    elif damage == "version":
        data[4:8] = struct.pack("<I", 2)
    elif damage == "truncated":
        data = data[:-1]
    elif damage == "counts":
        data[8:12] = struct.pack("<I", 0xFFFFFFFF)
    elif damage == "index":
        data[-4:] = struct.pack("<I", 4)
    elif damage == "nonfinite":
        data[16:20] = struct.pack("<f", float("nan"))
    else:
        data += b"junk"
    p.write_bytes(data)
    log = run(harness, tmp_path, "room", 5, 0)
    assert "bad room line" in log and "0 mesh triangles" in log


@pytest.mark.parametrize("record", [f"mesh {uuid(80)} ../escape.bin 4 6", f"mesh {uuid(80)} room-mesh.bin 600001 600000",
                                     f"mesh {uuid(80)} room-mesh.bin 4 7"])
def test_mesh_caps_and_path_confinement(harness, tmp_path, record):
    room(tmp_path)
    p = tmp_path / "room.txt"
    p.write_text("\n".join(record if x.startswith("mesh ") else x for x in p.read_text().splitlines()) + "\n")
    run(harness, tmp_path, "room", 5, 0)


@pytest.mark.parametrize("text", [None, "", "frameport-room 2\n", "bad 1\n", "# comment before magic\nframeport-room 1\n",
                                  "frameport-room 1\noutline 3 0 0 1 1 2 2\n"])
def test_box_fallback_unchanged(harness, tmp_path, text):
    if text is not None:
        (tmp_path / "room.txt").write_text(text)
    log = run(harness, tmp_path, "box")
    assert "scene: room 1.50 x 1.50 x 2.50 m (from guardian)" in log
    assert "scene: capture requested -> guardian box" in log


def test_box_size_settings(harness, tmp_path):
    assert "scene: room 3.00 x 4.00 x 2.70 m" in run(harness, tmp_path, "override")


def test_fb_and_meta_anchors_persist_across_sessions_and_processes(harness, tmp_path):
    _, external = harness
    process = "org.frameport.persist"
    folder = external / process / "files"
    folder.mkdir(parents=True)
    run(harness, tmp_path, "save", process=process + ":game")
    p = folder / "framebridge-anchors.txt"
    data = p.read_text()
    lines = data.splitlines()
    assert len(lines) == 2 and len({x.split()[1] for x in lines}) == 2
    assert all(len(x.split()) == 9 and x.startswith("anchor ") for x in lines)
    assert not list(folder.glob("*.tmp-*"))
    assert "loaded 2 saved anchors" in run(harness, tmp_path, "reload", process=process)
    assert p.read_text() == data
    run(harness, tmp_path, "erase-failure", process=process)
    assert p.read_text() == data and not list(folder.glob("*.tmp-*"))
    run(harness, tmp_path, "erase", process=process)
    assert p.read_text() == ""
    run(harness, tmp_path, "empty", process=process)


def test_failed_anchor_write_is_reported_and_rolled_back(harness, tmp_path):
    # No external files directory: the accepted async call reports IO failure in its completion event.
    run(harness, tmp_path, "save-failure", process="org.frameport.missing")


def test_missing_outline_and_ceiling_are_derived(harness, tmp_path):
    points = [(0, 0), (4, 0), (4, 3), (0, 3)]
    lines = ["frameport-room 1", f"room {uuid(1)}", f"floor {uuid(2)} 1"]
    for i, ((x, z), (xx, zz)) in enumerate(zip(points, points[1:] + points[:1])):
        lines.append(f"wall {uuid(10 + i)} {x} {z} {xx} {zz} 1 4")
    (tmp_path / "room.txt").write_text("\n".join(lines) + "\n")
    run(harness, tmp_path, "derived")


def test_hundreds_of_walls_have_distinct_default_uuids(harness, tmp_path):
    # Missing walls are generated from all outline edges, including more than the old 64-entity cap.
    import math

    points = [(5 * math.cos(i * math.tau / 300), 5 * math.sin(i * math.tau / 300)) for i in range(300)]
    text = "frameport-room 1\noutline 300 " + " ".join(f"{x} {z}" for x, z in points) + "\n"
    (tmp_path / "room.txt").write_text(text)
    run(harness, tmp_path, "many")


def test_real_shim_bypasses_a_dispatcher_that_refuses_scene_calls(harness, tmp_path):
    import re

    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    inc = next(p for p in (ROOT / "native/.cache").glob("openxr-*")
               if "#define XR_META_spatial_entity_discovery 1" in (p / "openxr/openxr.h").read_text())
    native = ROOT / "tests/native"
    common = [cc, "-std=gnu11", "-O2", "-I", str(inc)]
    subprocess.run([*common, "-shared", "-fPIC", "-fvisibility=hidden", "-DSCENE_SHIM_TEST",
                    "-I", str(ROOT / "native/adapter"), str(native / "scene_emu_test.c"), "-pthread", "-lm",
                    "-o", str(tmp_path / "libopenxr_loader_generic.so")], check=True)
    subprocess.run([*common, "-shared", "-fPIC", "-DFAKE_DISPATCH", str(native / "scene_shim_test.c"),
                    "-o", str(tmp_path / "libopenxr_loader.so")], check=True)
    shim = tmp_path / "libframe_xrshim.so"
    subprocess.run([*common, "-shared", "-fPIC", "-fvisibility=hidden", "-I", str(ROOT / "tests/fixtures/src/host_include"),
                    str(ROOT / "native/xrshim/xrshim.c"), "-ldl", "-pthread", "-lm", "-o", str(shim)], check=True)
    binary = tmp_path / "test-shim"
    subprocess.run([*common, str(native / "scene_shim_test.c"), "-ldl", "-o", str(binary)], check=True)
    # Exercise every name the emulation serves, including future additions to its dispatch table.
    names = re.findall(r'\{"(xr\w+)",', (ROOT / "native/adapter/scene_emu.c").read_text())
    result = subprocess.run([str(binary), str(shim), *names], capture_output=True, text=True,
                            env={**os.environ, "LD_LIBRARY_PATH": str(tmp_path),
                                 "FRAMEBRIDGE_CONFIG": str(tmp_path / "settings.conf")})
    assert result.returncode == 0, result.stdout + result.stderr
    for name in names:
        assert f"extension shim: {name} -> FrameBridge" in result.stderr
