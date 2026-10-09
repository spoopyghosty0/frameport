"""The GUI's easter eggs (ui/easter.py): the pure parts (dates, moods, the click counter, milestones)."""
import datetime as dt

import pytest

from frameport.core import library
from frameport.ui import easter


@pytest.mark.parametrize("day, expected", [
    ("2026-10-23", None), ("2026-10-24", "halloween"), ("2026-10-31", "halloween"), ("2026-11-01", None),
    ("2026-12-19", None), ("2026-12-20", "winter"), ("2026-12-31", "winter"), ("2027-01-02", "winter"),
    ("2027-01-03", None), ("2026-07-04", None),
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
