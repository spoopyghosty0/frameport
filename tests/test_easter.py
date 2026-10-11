"""The GUI's easter eggs (ui/easter.py): the pure parts (dates, moods, the click counter, milestones)."""
import datetime as dt

import pytest

from frameport.core import library
from frameport.ui import easter


@pytest.mark.parametrize("day, expected", [
    ("2026-10-23", None), ("2026-10-24", "halloween"), ("2026-10-31", "halloween"), ("2026-11-01", None),
    ("2026-12-19", None), ("2026-12-20", "winter"), ("2026-12-30", "winter"), ("2026-12-31", "newyear"),
    ("2027-01-01", "newyear"), ("2027-01-02", "winter"), ("2027-01-03", None), ("2026-07-04", None),
    ("2027-02-13", "valentine"), ("2027-02-14", "valentine"), ("2027-02-15", None), ("2027-03-14", "pi"),
    ("2027-03-17", "clover"), ("2027-04-01", "april"), ("2027-03-27", "easter"), ("2027-03-28", "easter"),
    ("2027-03-29", "easter"), ("2027-03-30", None),
])
def test_holidays(day, expected):
    assert easter.holiday(dt.date.fromisoformat(day)) == expected


def test_today_can_be_pretended(monkeypatch):
    monkeypatch.setenv("FRAMEPORT_TODAY", "2026-10-31")
    assert easter.today() == dt.date(2026, 10, 31)
    monkeypatch.setenv("FRAMEPORT_TODAY", "not a date")
    assert easter.today() == dt.date.today()


@pytest.mark.parametrize("fps, target, mood", [
    (72, 72, "Smooth as butter"), (70, 72, "Smooth as butter"), (66, 72, "Pretty smooth"),
    (57, 72, "It's doing its best"), (40, 72, "Hang in there, little Frame"), (None, 72, None), (60, None, None),
])
def test_fps_moods(fps, target, mood):
    assert easter.fps_mood(fps, target) == mood


def test_click_counter_needs_seven_quick_clicks():
    now = [0.0]
    counter = easter.ClickCounter(clicks=7, window=3.0, clock=lambda: now[0])
    hits = []
    for _ in range(7):
        hits.append(counter.click())
        now[0] += 0.3
    assert hits == [False] * 6 + [True]
    for _ in range(6):  # slow clicks never get there
        now[0] += 1.0
        assert not counter.click()
    assert counter.times  # (the window keeps only recent clicks)
    assert len(counter.times) <= 4


def test_install_milestones_count_different_games():
    messages = [easter.record_install(f"game.{i}") for i in range(12)]
    assert messages[0] and "first" in messages[0].lower()
    assert messages[9] and messages[9].startswith("10 games")
    assert [m for i, m in enumerate(messages) if i not in (0, 9)] == [None] * 10
    for i in range(12, 100):
        easter.record_install(f"game.{i}")
    assert library.setting(easter.SHOWN) == [1, 10, 100]
    assert len(library.setting(easter.INSTALLS)) == 100


def test_milestones_show_once_ever():
    """Updates, rebuilds and reinstalls of a game never count again; a shown milestone never comes back."""
    assert easter.record_install("game.a")  # the first one
    for _ in range(5):  # updating / reinstalling the same game (or a second window handling the same job)
        assert easter.record_install("game.a") is None
    for i in range(8):
        easter.record_install(f"game.{i}")
    assert easter.record_install("game.ten")  # the 10th different game
    # however the set got smaller later (a reset, a bug), a milestone that was shown stays shown
    library.set_setting(easter.INSTALLS, ["game.a"])
    assert [easter.record_install(f"again.{i}") for i in range(12)] == [None] * 12
    assert easter.record_install(None) is None


def test_existing_installs_dont_celebrate_after_an_update():
    """FramePort updated to a version with milestones: games already on the Frame count, and the milestones they
    passed are taken as shown (no "your first game" for someone with 12 games)."""
    for i in range(12):
        library.upsert_game(f"old.{i}", title=f"Old {i}", installs={"frame": {"result": {"ok": True}, "time": 1}})
    library.upsert_game("pc.only", title="PC only", installs={"pc": {"time": 1}})
    assert easter.record_install("new.game") is None  # the 13th: no milestone
    assert library.setting(easter.SHOWN) == [1, 10]
    assert len(library.setting(easter.INSTALLS)) == 13 and "pc.only" not in library.setting(easter.INSTALLS)


def test_milestone_seed_with_none_installed():
    library.upsert_game("first.one", title="First", installs={"frame": {"result": {"ok": True}, "time": 1}})
    # (the installed game is the one being recorded: the seed leaves it out, so it still is the first)
    assert easter.record_install("first.one").startswith("Your first game")


def test_easter_eggs_can_be_turned_off():
    assert easter.enabled()  # (on unless the setting says otherwise)
    library.set_setting(easter.SETTING, False)
    assert not easter.enabled()


def test_holiday_badges_exist():
    from pathlib import Path

    icons = Path(easter.__file__).parent / "icons"
    for name in easter.HOLIDAY_ICON.values():
        assert (icons / f"{name}.svg").is_file()


@pytest.mark.parametrize("year, day", [(2026, "2026-04-05"), (2027, "2027-03-28"), (2029, "2029-04-01"),
                                       (2038, "2038-04-25"), (2285, "2285-03-22")])
def test_easter_sunday(year, day):
    assert easter.easter_sunday(year) == dt.date.fromisoformat(day)


def test_easter_beats_april_fools():
    assert easter.holiday(dt.date(2029, 4, 1)) == "easter"  # (Easter Sunday that year)


def test_every_badge_has_a_dance_that_ends_where_it_started():
    import math

    for which, motion in easter.HOLIDAY_MOTION.items():
        assert which in easter.HOLIDAY_ICON
        frames = easter.hover_frames(motion)
        assert frames, motion
        turn = sum(p.get("turn", 0) for p, _ in frames)
        assert abs(turn / (2 * math.pi) - round(turn / (2 * math.pi))) < 1e-9, motion  # whole turns
        last = {}
        for props, _ in frames:
            last.update(props)
        assert last.get("scale", 1.0) == 1.0 and tuple(last.get("offset", (0, 0))) == (0, 0), motion


def test_perfect_pacing_needs_the_full_time_once_per_streak():
    need = easter.PERFECT_SECONDS
    assert 120 <= need <= 180  # (owner: two to three minutes)
    p = easter.PerfectPacing()
    assert not any(p.feed(72.0, 72, t) for t in range(0, int(need)))
    assert p.feed(71.8, 72, need)  # the whole time within half a frame
    assert p.done and not p.feed(72.0, 72, need + 1)  # once per streak; .done stays while it holds (the pill)
    assert not p.feed(68.0, 72, need + 2) and not p.done  # it slips ...
    assert not any(p.feed(72.0, 72, need + 3 + t) for t in range(int(need) - 1))
    assert p.feed(72.0, 72, 2 * need + 3)  # ... and a new streak counts again
    assert not easter.PerfectPacing().feed(None, 72, 0)


def test_self_search():
    assert easter.is_self_search("FramePort") and easter.is_self_search(" frame port ")
    assert not easter.is_self_search("frame") and not easter.is_self_search(None)


def test_hello_is_noticed_wherever_it_is_typed():
    typed = easter.Typed()
    said = [typed.key(k) for k in ("H", "E", "L", "L", "O")]
    assert said == [False] * 4 + [True]
    typed = easter.Typed()
    assert not any(typed.key(k) for k in ("H", "E", "L", "Shift", "L", "P", "Backspace"))
    assert typed.key("O")  # (Shift doesn't count, Backspace takes the P back)
    assert easter.says_hello("Hello Frame!") and not easter.says_hello("help")


def test_toss_path_falls_out_of_the_window():
    import random

    path = easter.toss_path(40, 40, 1440, 900, random.Random(3))
    xs, ys = [p[0] for p in path], [p[1] for p in path]
    assert xs == sorted(xs) and xs[0] > 40  # always into the window, never back
    assert min(ys) < 40  # thrown upwards first ...
    assert ys[-1] > 900 or xs[-1] > 1440  # ... then it falls out of the window
    assert len(path) < 70


def test_charged_now():
    assert easter.charged_now({"percent": 99}, {"percent": 100, "plugged": True})
    assert not easter.charged_now(None, {"percent": 100, "plugged": True})  # a first reading isn't "reaching" it
    assert not easter.charged_now({"percent": 100}, {"percent": 100, "plugged": True})
    assert not easter.charged_now({"percent": 99}, {"percent": 100, "plugged": False})


@pytest.mark.parametrize("which", sorted(easter.SHOWERS))
def test_holiday_showers(which):
    import random

    kind, n = easter.SHOWERS[which]
    assert which in easter.HOLIDAY_ICON  # (a badge to click)
    if kind == "confetti":
        return
    plan = easter.shower_plan(kind, n, 1440, 900, random.Random(5))
    assert len(plan) == n
    for p in plan:
        assert 0 <= p["start"]["left"] <= 1440
        assert p["phases"] and all(secs > 0 for _, secs, _ in p["phases"])
        end = dict(p["start"])
        for props, _, curve in p["phases"]:
            assert curve in easter._CURVES
            end.update(props)
        assert end["top"] > 900 or end["top"] < 0 or end.get("opacity") == 0 or kind == "pumpkins"  # gone at the end
        if kind == "pumpkins":
            assert end["opacity"] == 0


def test_ring_pulse_leaves_the_percentage_alone():
    import inspect

    assert "scale" in inspect.getsource(easter.ring_pulse)
    assert "ring_pulse(self._conn_ring, self._conn_glow)" in \
        __import__("pathlib").Path(easter.__file__).with_name("app.py").read_text()


def test_charge_ring_empties_at_once_then_refills():
    steps = easter.ring_fill_steps(fill=0.4, dt=0.025)
    assert steps[0] == 0.0 and steps[-1] == 1.0  # empty at once (no draining), full at the end
    assert steps == sorted(steps) and len(steps) == 17  # then only filling, in 0.4 s


@pytest.mark.parametrize("eggs, reduce, finale", [
    (True, False, "toss"), (True, True, "words"), (False, False, "plain"), (False, True, "plain"),
])
def test_demo_link_finale(eggs, reduce, finale):
    assert easter.demo_finale(eggs, reduce) == finale
    assert "nothing" in easter.demo_message(finale)
    assert ("Nice, it works" in easter.demo_message(finale)) == (finale == "plain")


def test_demo_link_is_listed_in_the_docstring():
    assert 'example "Install with FramePort"' in easter.__doc__
    assert "show_demo" in easter.__doc__
