"""The showcase (scripts/showcase): demo library, manifests, step language, element finding, the image diff and the
video graph builders. No browser and no ffmpeg run here (render_docs / record_video do that)."""
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from showcase import demo_home, postprod, record_video, render_docs, steps, web  # noqa: E402

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


def test_fake_screenshots_are_in_game_pictures(demo):
    """The Screenshots tab's samples are store screenshots (in-game pictures), never cover art, the running game's
    first, no picture twice; Take screenshot adds the running game's next one."""
    from PIL import Image
    from showcase import fakes

    from frameport.artwork import fetch

    home, _ = demo
    with_shots = ["com.StressLevelZero.BONELAB", "com.CyanWorlds.Myst", "com.playful.LuckysTale",
                  "com.CyanWorlds.Riven"]
    for n, pkg in enumerate(with_shots):
        d = fetch.artwork_dir(pkg)
        for i in range(1, 6):
            Image.new("RGB", (1280, 720), (n * 50, i * 40, 90)).save(d / f"shot_{i}.jpg")
        Image.new("RGB", (600, 900), (255, 0, 0)).save(d / "portrait.jpg")  # cover art: must not be used
    fs = fakes.FakeFS()
    assert fs.owners[0][0] == "com.StressLevelZero.BONELAB"  # the running game (demo-library.yaml)
    assert fs.owners[-1][1] == "SteamVR" and fs.owners[-1][2]
    assert {o[0] for o in fs.owners[:3]} <= set(with_shots[:3])  # installed games first, Riven isn't
    for _pkg, _title, arts in fs.owners:
        assert arts and all(p.name.startswith("shot_") for p in arts)
    pixels = {Image.open(s["path"]).convert("RGB").getpixel((5, 5)) for s in fs.shots}
    assert len(pixels) == len(fs.shots)  # every sample a different picture
    taken = fs.take_screenshot()
    assert taken["taken"] and fs.shots[0]["package"] == "com.StressLevelZero.BONELAB"


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
    for shot in shots["shots"]:  # every vars reference resolves
        steps.substitute(shot["steps"], shots["vars"])
    assert "library" in names
    assert set(record_video.video_names()) >= {"tour", "install"}
    outputs = set()
    for name in record_video.video_names():
        video = record_video.load_video(name)
        assert video["output"] not in outputs  # one file per video
        outputs.add(video["output"])
        assert video["output"].startswith("docs/media/frameport-")  # (build.yml names release assets from it)
        filmed = [sc for sc in video["scenes"] if "card" not in sc]
        assert filmed and all(sc.get("caption") for sc in filmed)
        for sc in filmed:
            steps.substitute(sc["steps"], video.get("vars") or {})
        steps.substitute(video.get("setup") or [], video.get("vars") or {})
    tour = record_video.load_video("tour")
    assert any(sc.get("teaser") for sc in tour["scenes"]) and tour["teaser"] == "docs/images/tour-teaser.webp"
    install = record_video.load_video("install")
    assert install["start"]["profile"] == "fresh" and any("card" in sc for sc in install["scenes"])


@pytest.mark.parametrize("change, message", [
    ({"scenes": [{"name": "a", "card": {"heading": "H"}, "steps": [{"wait": 1}]}]}, "no steps, caption or teaser"),
    ({"scenes": [{"name": "a", "card": {"line": "no heading"}}]}, "card needs a heading"),
    ({"scenes": [{"name": "a", "card": {"heading": "H", "colour": "red"}}]}, "card needs a heading"),
    ({"start": {"profile": "empty"}}, "start.profile"),
    ({"start": {"frame": "maybe"}}, "start.frame"),
    ({"output": "docs/media/x.gif"}, "output must be an .mp4"),
    ({"scenes": []}, "no scenes"),
    ({"extra": 1}, "unknown key"),
])
def test_bad_storyboards_are_refused(tmp_path, monkeypatch, change, message):
    import yaml

    good = {"output": "docs/media/frameport-x.mp4", "title": {"heading": "T"}, "end": {"heading": "E"},
            "scenes": [{"name": "s", "caption": ["c"], "steps": [{"go": "library"}]}]}
    (tmp_path / "x.yaml").write_text(yaml.safe_dump({**good, **change}))
    monkeypatch.setattr(record_video, "VIDEOS", tmp_path)
    with pytest.raises(steps.StepError, match=message):
        record_video.load_video("x")


def test_changed_videos(monkeypatch):
    import subprocess

    def fake_diff(files, code=0):
        return lambda *a, **k: subprocess.CompletedProcess(a, code, "\n".join(files), "")
    monkeypatch.setattr(subprocess, "run", fake_diff(["docs/showcase/videos/install.yaml", "README.md"]))
    assert record_video.changed_videos("abc") == ["install"]
    monkeypatch.setattr(subprocess, "run", fake_diff(["scripts/showcase/web.py"]))
    assert record_video.changed_videos("abc") == record_video.video_names()  # the recorder changed: all
    monkeypatch.setattr(subprocess, "run", fake_diff(["src/frameport/ui/app.py"]))
    assert record_video.changed_videos("abc") == []
    monkeypatch.setattr(subprocess, "run", fake_diff([], code=128))
    assert record_video.changed_videos("unknown") == record_video.video_names()


def test_fresh_profile_is_a_first_start(tmp_path, monkeypatch):
    for var in TOOL_VARS:
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    monkeypatch.setattr(demo_home, "CACHE", tmp_path / "cache")
    home = tmp_path / "fresh"
    demo_home.build(home, offline=True, log=lambda *_: None, profile="fresh")
    from frameport.core import library

    assert library.games() == [] and library.setting("ui.welcome_done") is False
    found = json.loads((home / demo_home.SCAN_FILE).read_text())
    assert len(found) == len(demo_home.load_fixture()["games"])
    assert not any(k in g for g in found for k in ("installs", "last_played", "last_test", "build"))
    state = json.loads((home / "showcase-frame.json").read_text())
    assert state["installed"] == [] and state["running"] is None


def test_first_run_fakes_never_show_this_pcs_address(monkeypatch):
    from showcase import fakes

    from frameport.frame import discovery, pairing
    from frameport.tools import toolchain

    for module, attr in ((discovery, "browse"), (pairing, "PairingServer"), (pairing, "ensure_reachable"),
                         (toolchain, "status")):  # (restored after the test: the fakes patch modules)
        monkeypatch.setattr(module, attr, getattr(module, attr))
    fakes.install_first_run_fakes()
    server = pairing.PairingServer(on_paired=None).start()
    assert server.one_liner == "curl -fsS 192.168.1.20:8765/1a2b3c4d | bash"  # the docs' example
    assert server.requests and server.running  # (no firewall hint, nothing listening)
    assert discovery.browse(4) == []  # no real Frame on this network shows up in a recording
    assert pairing.ensure_reachable(server) == "ok"


def test_step_cards():
    colors = {k: "#123456" for k in ("BG", "SURFACE", "SURFACE_2", "BORDER", "TEXT", "TEXT_2", "TEXT_3", "ACCENT",
                                     "SECONDARY")}
    html = postprod.step_card_html({"eyebrow": "Step 1", "heading": "Run <it>", "steps": ["Open **Konsole** & type"],
                                    "code": "curl x | bash", "note": "No **password**"}, colors)
    assert "Run &lt;it&gt;" in html and "<b>Konsole</b> &amp; type" in html
    assert "curl x | bash" in html and "<b>password</b>" in html and ">1<" in html


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
