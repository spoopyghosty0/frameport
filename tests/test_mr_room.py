import struct
import xml.etree.ElementTree as ET

import pytest

from frameport.mr.room import (
    Mesh,
    Plane,
    Volume,
    box_room,
    dumps,
    from_boundary,
    parse,
    preview_svg,
    read_mesh,
    summary,
    write_mesh,
)


def test_roundtrip():
    r = box_room(4, 3, 2.6)
    r.universe = 2**64 - 1
    r.volumes.append(Volume("a"*32, "TABLE", 1, 0, .8, 25, 1, .6, .8))
    r.planes.append(Plane("b"*32, "WINDOW_FRAME", 0, 1, -1.5, 0, 0, 0, 1, 1, 1))
    r.mesh = Mesh("c"*32, vertex_count=3, index_count=3)
    assert dumps(parse(dumps(r))) == dumps(r)
    assert summary(r) == dict(walls=4, volumes={"TABLE": 1}, planes=1, floor_area=12, ceiling_height=2.6)
    root = ET.fromstring(preview_svg(r))
    assert root.tag.endswith("svg")
    assert any(e.text == "TABLE" for e in root.iter())


def test_tolerant_and_defaults():
    r = parse("# comment\n\nframeport-room 1 # format\nfloor " + "1"*32 + " 1\n"
              "outline 4 0 0 0 3 4 3 4 0\nfuture whatever\n")
    assert r.ceiling == 3.5
    assert summary(r)["floor_area"] == 12
    assert len(r.walls) == 4
    # Clockwise input is repaired, as are explicitly supplied wall directions.
    text = dumps(r).replace("outline 4 4 0 4 3 0 3 0 0", "outline 4 0 0 0 3 4 3 4 0")
    assert dumps(parse(text)) == dumps(r)
    assert parse("frameport-room 1\n" + "\n".join(line for line in dumps(r).splitlines()
                                                 if line.startswith("wall "))).outline


@pytest.mark.parametrize("text", ["", "frameport-room 2", "frameport-room 1\noutline 3 0 0 1 1",
    "frameport-room 1\noutline 3 0 0 1 1 nan 2", "frameport-room 1\noutline 3 0 0 1 1 2 2"])
def test_invalid(text):
    with pytest.raises(ValueError):
        parse(text)


@pytest.mark.parametrize("record", ["universe -1", "universe 18446744073709551616", "source unknown",
    "mesh " + "a"*32 + " ../oops 3 3", "volume " + "a"*32 + " TABLE 0 0 1 0 -1 1 1",
    "wall " + "a"*32 + " 0 0 1 0 1 0", "room bad", "floor " + "a"*32 + " inf"])
def test_invalid_record(record):
    with pytest.raises(ValueError):
        parse(dumps(box_room(3, 4)) + record + "\n")


def test_mesh_roundtrip_and_validation():
    m = Mesh(vertices=[(0, 0, 0), (1, 0, 0), (0, 0, 1)], indices=[0, 1, 2])
    data = write_mesh(m)
    got = read_mesh(data)
    assert got.vertices == m.vertices and got.indices == m.indices
    for bad in [data[:10], data[:-1], b"oops" + data[4:], data + b"x",
                data[:-4] + struct.pack("<I", 3)]:
        with pytest.raises(ValueError):
            read_mesh(bad)


def test_boundary_and_preview():
    r = from_boundary([(0, 0), (0, 3), (4, 3), (4, 0)])
    assert r.source == "boundary"
    assert [(w.x0, w.z0) for w in r.walls] == r.outline
    r.play_area = [(1, 1), (2, 1), (2, 2)]
    assert "stroke-dasharray" in preview_svg(r)
    for dims in [(0, 1, 1), (1, -1, 1), (1, 1, float("nan"))]:
        with pytest.raises(ValueError):
            box_room(*dims)


def test_clockwise_outline_and_wall_directions_are_repaired():
    r = box_room(4, 3)
    r.outline.reverse()
    for w in r.walls:
        w.x0, w.x1 = w.x1, w.x0
        w.z0, w.z1 = w.z1, w.z0
    got = parse(dumps(r))
    edges = list(zip(got.outline, got.outline[1:] + got.outline[:1], strict=True))
    assert all(((w.x0, w.z0), (w.x1, w.z1)) in edges for w in got.walls)
