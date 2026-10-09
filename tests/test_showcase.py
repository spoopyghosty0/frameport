"""The showcase (scripts/showcase): demo library, manifests, step language, element finding, the image diff and the
video graph builders. No browser and no ffmpeg run here (render_docs / record_tour do that)."""
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from showcase import demo_home, postprod, record_tour, render_docs, steps, web  # noqa: E402

TOOL_VARS = ("FRAMEPORT_NO_UPDATE_CHECK", "FRAMEPORT_NO_CATALOG_UPDATE", "FRAMEPORT_NO_LINK_HANDLER",
             "FRAMEPORT_JAVA", "FRAMEPORT_OVERPORT_JAR", "FRAMEPORT_APKSIGNER_JAR")


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """The demo library built offline with an empty art cache (every game falls back to placeholders)."""
    for var in TOOL_VARS:  # build() sets these: set + delete first, so monkeypatch removes them again afterwards
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    monkeypatch.setattr(demo_home, "CACHE", tmp_path / "cache")
    home = tmp_path / "demo"
    result = demo_home.build(home, offline=True, log=lambda *_: None)
    return home, result


def test_fixture_and_analyses_match():
    fixture = demo_home.load_fixture()
    tech = json.loads(demo_home.ANALYSES.read_text())
    assert {g["package"] for g in fixture["games"]} <= set(tech)
    assert fixture["frame"]["running"] in {g["package"] for g in fixture["games"]}
    text = demo_home.ANALYSES.read_text()
    assert not re.search(r'"[A-Za-z]:[\\/]|/home/|/Users/|\\\\', text)  # exported without paths


def test_fixture_rejects_unknown_fields(tmp_path):
    bad = tmp_path / "lib.yaml"
    bad.write_text("games:\n  - package: a.b\n    frmae: internal\n")
    with pytest.raises(ValueError, match="unknown field"):
        demo_home.load_fixture(bad)


def test_demo_library_builds_offline(demo):
    from frameport.core import library

    home, result = demo
    games = {g["package"]: g for g in library.games()}
    fixture = demo_home.load_fixture()
    assert result["games"] == len(fixture["games"]) == len(games)
    assert result["art"] == {"none": [g["package"] for g in fixture["games"]]}
    manta = games["com.camouflaj.manta"]
    assert manta["recipe"]["source"].startswith("catalog") and manta["status"] == "works"
    assert manta["apk"].startswith("D:/Games/Quest/")
    rift = games["rift.rick_and_morty_virtual_rick_ality"]
    assert rift["kind"] == "rift" and rift["game_dir"].startswith("D:/Games/PC VR/")
    state = json.loads((home / "showcase-frame.json").read_text())
    by_pkg = {i["package"]: i for i in state["installed"]}
    assert by_pkg["com.Sanzaru.Wrath2"]["drive"] == "sd"
    assert by_pkg["com.vertigogames.ImpactDevelopment"]["sha256"] != \
        games["com.vertigogames.ImpactDevelopment"]["build"]["sha256"]  # "outdated": an older build on the Frame
    assert library.setting("ui.welcome_done") is True
    assert render_docs.privacy_problems(home) == []


def test_demo_library_installed_state_in_ui(demo):
    """The UI's own install_state sees the pretend Frame's games as installed / outdated / missing."""
    from showcase import fakes

    from frameport.core import library
    from frameport.ui import components as C

    target = fakes.FakeTarget()
    info = target.describe()
    assert C.install_state(library.game("com.camouflaj.manta"), info) == "installed"
    assert C.install_state(library.game("com.vertigogames.ImpactDevelopment"), info) == "outdated"
    assert C.install_state(library.game("com.CyanWorlds.Riven"), info) == "missing"
    drives = {g["package"]: g["drive"]["internal"] for g in target.games}
    assert drives["com.Sanzaru.Wrath2"] is False and drives["com.camouflaj.manta"] is True


def test_build_refuses_the_real_data_dir(monkeypatch):
    monkeypatch.setattr(demo_home, "real_data_dir", lambda: Path("/somewhere/frameport").resolve())
    with pytest.raises(SystemExit, match="refusing"):
        demo_home.build(Path("/somewhere/frameport/demo"))


def test_privacy_scan_finds_personal_data(tmp_path):
    (tmp_path / "library.json").write_text(json.dumps({"apk": str(Path.home() / "games" / "x.apk"),
                                                       "host": "192.168.1.23", "version": "1.4.0.0"}))
    problems = render_docs.privacy_problems(tmp_path)
    assert any("contains" in p and str(Path.home()) in p for p in problems)
    assert any("192.168.1.23" in p for p in problems)
    assert not any("1.4.0.0" in p for p in problems)  # version numbers aren't addresses


def test_image_change_threshold(tmp_path):
    from PIL import Image, ImageDraw

    a = Image.new("RGB", (400, 300), (20, 22, 30))
    ImageDraw.Draw(a).rectangle((40, 40, 200, 120), fill=(240, 140, 40))
    a.save(tmp_path / "a.png")
    a.save(tmp_path / "same.png")
    noisy = a.copy()
    noisy.putpixel((10, 10), (25, 26, 33))  # anti-aliasing-sized noise
    noisy.putpixel((300, 200), (30, 22, 30))
    noisy.save(tmp_path / "noisy.png")
    moved = Image.new("RGB", (400, 300), (20, 22, 30))
    ImageDraw.Draw(moved).rectangle((60, 40, 220, 120), fill=(240, 140, 40))
    moved.save(tmp_path / "moved.png")
    Image.new("RGB", (401, 300)).save(tmp_path / "bigger.png")
    old = tmp_path / "a.png"
    assert not render_docs.changed(old, tmp_path / "same.png")
    assert not render_docs.changed(old, tmp_path / "noisy.png")
    assert render_docs.changed(old, tmp_path / "moved.png")
    assert render_docs.changed(old, tmp_path / "bigger.png")
    assert render_docs.changed(tmp_path / "missing.png", tmp_path / "same.png")


def test_manifests_are_valid():
    shots = render_docs.load_manifest()
    names = {s["name"] for s in shots["shots"]}
    tour = record_tour.load_tour()
    assert len(tour["scenes"]) >= 5 and all(sc.get("caption") for sc in tour["scenes"])
    assert any(sc.get("teaser") for sc in tour["scenes"])
    # every vars reference resolves
    for shot in shots["shots"]:
        steps.substitute(shot["steps"], shots["vars"])
    for sc in tour["scenes"]:
        steps.substitute(sc["steps"], tour["vars"])
    assert "library" in names


def test_every_docs_image_has_a_shot():
    """A screenshot the docs show must come from the renderer (else it silently goes stale)."""
    shots = {s["name"]: s for s in render_docs.load_manifest()["shots"]}
    docs = [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]
    used = {}
    for doc in docs:
        for m in re.finditer(r"!\[[^\]]*\]\((?:docs/)?images/([a-z0-9-]+)\.png\)", doc.read_text()):
            used.setdefault(m.group(1), []).append(doc.relative_to(REPO).as_posix())
    assert used, "no docs images found"
    missing = sorted(set(used) - set(shots))
    assert not missing, f"docs images without a shots.yaml entry: {missing}"
    for name, where in used.items():
        assert set(where) <= set(shots[name].get("docs") or []), f"{name}: docs list is out of date ({where})"


@pytest.mark.parametrize("bad, message", [
    ([{"clik": "Library"}], "unknown action"),
    ([{"hook": "nope"}], "unknown hook"),
    ([{"go": "library", "wait": 1}], "exactly one action"),
    ([{"drag": ["A"]}], "two or more"),
    ([{"wait": "soon"}], "seconds"),
    ({"go": "library"}, "list of steps"),
])
def test_step_validation(bad, message):
    with pytest.raises(steps.StepError, match=message):
        steps.validate(bad)


def test_substitute_keeps_types():
    out = steps.substitute([{"call": {"fn": "x", "args": ["${n}", "id ${g}"]}}], {"n": 3, "g": "pkg"})
    assert out == [{"call": {"fn": "x", "args": [3, "id pkg"]}}]
    with pytest.raises(steps.StepError):
        steps.substitute("${missing}", {})


def node(text="", label="", all_text=None, x=0, y=0, w=100, h=40, tappable=True):
    return {"label": label, "text": text, "all": all_text if all_text is not None else text, "tappable": tappable,
            "x": x, "y": y, "w": w, "h": h, "role": ""}


def test_pick_prefers_exact_innermost_and_order():
    rows = node(all_text="Trips\nConcert 8K 3D.mp4\n2.2 GiB\nEarth from orbit (360).mp4", x=0, y=0, w=800, h=500)
    row = node(text="Earth from orbit (360).mp4\n2.2 GiB · 2026-09-30", x=5, y=100, w=790, h=50)
    assert web.pick([rows, row], "Earth from orbit (360).mp4") is row  # the row, not the list around it
    a, b = node(text="Riven", y=300), node(text="Riven", y=100)
    assert web.pick([a, b], "Riven") is b and web.pick([a, b], "Riven", nth=1) is a
    assert web.pick([a, b], "Riven", nth=2) is None
    title = node(text="Riven: The Sequel", y=50)
    assert web.pick([title, a], "Riven") is a  # exact beats prefix
    assert web.pick([title], "Riven") is title and web.pick([title], "Riven", exact=True) is None
    text = node(text="", all_text="What FramePort will do", w=170, h=20, tappable=False)
    page = node(text="", all_text="Library\nWhat FramePort will do\nPatches", w=1200, h=2000, tappable=False)
    assert web.pick([page, text], "What FramePort will do") is text  # plain text: the smallest node
    tooltip = node(label="Settings", text="")
    assert web.pick([tooltip], "settings") is tooltip


def test_pointer_paths():
    path = web.human_path((0, 0), (300, 200), 40)
    assert len(path) == 40 and path[-1] == pytest.approx((300, 200))
    xs = [p[0] for p in path]
    assert xs == sorted(xs)  # no going back
    assert web.ease(0) == 0 and web.ease(1) == 1 and web.ease(0.5) == pytest.approx(0.5)
    steps_ = [web.ease(i / 10) for i in range(11)]
    assert steps_ == sorted(steps_)
    assert web.move_duration(10) < web.move_duration(1500) <= 0.9


def test_concat_list_holds_each_frame_until_the_next():
    text = postprod.concat_list([(10.0, "a.jpg"), (10.5, "b.jpg"), (12.0, "c.jpg")], end=13.0)
    lines = text.splitlines()
    assert lines[0] == "ffconcat version 1.0"
    assert lines[1:7] == ["file 'a.jpg'", "duration 0.500000", "file 'b.jpg'", "duration 1.500000",
                          "file 'c.jpg'", "duration 1.000000"]
    assert lines[-1] == "file 'c.jpg'"


def test_xfade_graph():
    assert postprod.xfade_offsets([3.0, 10.0, 8.0, 4.0], 0.5) == [2.5, 12.0, 19.5]
    assert postprod.joined_length([3.0, 10.0, 8.0, 4.0], 0.5) == pytest.approx(23.5)
    graph, label = postprod.xfade_graph([3.0, 10.0, 4.0], 0.5, (1280, 720))
    assert label == "[vout]"
    assert graph.count("xfade=") == 2 and "offset=2.5" in graph and "offset=12.0" in graph
    assert all(f"[{k}:v]fps=30,scale=1280:720" in graph for k in range(3))
    single, _ = postprod.xfade_graph([5.0], 0.5)
    assert "xfade" not in single


def test_caption_window_fits_the_scene():
    for duration in (3.0, 6.0, 15.0):
        start, end = postprod.caption_window(duration)
        assert 0 <= start < end <= max(duration - 0.6, start + 0.9)
    assert postprod.caption_window(15.0)[1] - postprod.caption_window(15.0)[0] == pytest.approx(4.2)
    f = postprod.scene_filter(10.0, size=(1280, 720))
    assert "scale=1280:720" in f and "fade=t=in" in f and "fade=t=out" in f and "overlay" in f


def test_cards_escape_text():
    colors = {k: "#123456" for k in ("BG", "SURFACE", "SURFACE_2", "BORDER", "TEXT", "TEXT_2", "TEXT_3", "ACCENT",
                                     "SECONDARY")}
    html = postprod.caption_html("Fast <b>", "A & B", colors)
    assert "Fast &lt;b&gt;" in html and "A &amp; B" in html
    card = postprod.card_html("FramePort", "line", "footer", "<svg/>", colors)
    assert ">Frame</span>" in card and ">Port</span>" in card and "<svg/>" in card
