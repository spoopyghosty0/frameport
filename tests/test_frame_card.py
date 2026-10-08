"""The sidebar's live Frame card: display values from a monitor sample (ui/frame_card.py, no Flet)."""
from types import SimpleNamespace

from frameport.ui import frame_card as FC


def sample(**game):
    g = {"package": "com.example.game", "title": "Example Game", "kind": "quest", "elapsed": 27 * 60 + 10,
         "fps": 71.6}
    g.update(game)
    return {"t": 1000.0, "games": [g], "battery": {"percent": 76, "plugged": False, "draining": True}}


def test_no_game_no_row():
    assert FC.now_playing(None) is None
    assert FC.now_playing({"games": []}) is None
    assert FC.now_playing({"battery": {"percent": 50}}) is None


def test_now_playing_values():
    np = FC.now_playing(sample(), [72.0] * 10)
    assert np["package"] == "com.example.game" and np["title"] == "Example Game"
    assert np["minutes"] == 27 and "27 min" in np["line"]
    assert np["fps_text"] == "72" and np["level"] == "ok" and np["target"] == 72


def test_fps_levels_against_the_refresh_rate():
    history = [90.0] * 20  # aiming for 90 Hz
    assert FC.now_playing(sample(fps=78.0), history)["level"] == "warn"   # < 90 %
    assert FC.now_playing(sample(fps=60.0), history)["level"] == "error"  # < 75 %
    assert FC.now_playing(sample(fps=89.5), history)["level"] == "ok"


def test_missing_fps_and_title():
    np = FC.now_playing(sample(fps=None, title=None, elapsed=None))
    assert np["fps_text"] == "–" and np["level"] == "none" and np["target"] is None
    assert np["title"] == "com.example.game" and np["minutes"] is None and np["line"]


def test_battery_ring():
    assert FC.battery_ring(None) is None and FC.battery_ring({}) is None
    r = FC.battery_ring({"percent": 76, "plugged": False, "draining": True})
    assert r == {"value": 0.76, "text": "76", "level": "ok"}
    assert FC.battery_ring({"percent": 12, "plugged": False, "draining": True})["level"] == "warn"
    assert FC.battery_ring({"percent": 40, "plugged": True, "draining": False})["level"] == "charging"
    assert FC.battery_ring({"percent": 140})["value"] == 1.0


def test_fps_history_covers_two_minutes():
    h = FC.fps_history()
    for i in range(300):  # one sample a second for 5 minutes
        h.add("fps", 72.0, 1000.0 + i)
    assert len(h.get("fps")) == FC.FPS_POINTS and FC.FPS_POINTS * FC.FPS_BUCKET == 120


def test_uploading():
    assert FC.uploading(SimpleNamespace(stage="Upload game (3 files, 1.2 GiB)"))
    assert not FC.uploading(SimpleNamespace(stage="Finalizing"))
    assert not FC.uploading(None)
