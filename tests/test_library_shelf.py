"""Library "On your Frame" shelf (which games, order, limit, when it hides) and the cover tint helpers."""
import colorsys

import pytest

from frameport.artwork.thumbs import dominant_rgb, tint_from_rgb
from frameport.ui.views.library import DEFAULT_FILTERS, filters_active, shelf_games


def g(pkg, played=0, sha=None, title=None):
    return {"package": pkg, "title": title or pkg, "last_played": played,
            "build": {"sha256": sha} if sha else {}}


FRAME = {"installed": [{"package": "a", "sha256": "1"}, {"package": "b", "sha256": "old"},
                       {"package": "c", "sha256": "1"}]}
GAMES = [g("a", 10, "1"), g("b", 30, "1"), g("c", 0, "1"), g("d", 99, "1")]  # d isn't on the Frame


def names(games):
    return [x["package"] for x in games]


def test_shelf_installed_most_recent_first():
    # outdated (b) counts as on the Frame; d isn't installed; never played (c) last
    assert names(shelf_games(GAMES, dict(DEFAULT_FILTERS), FRAME, True)) == ["b", "a", "c"]


def test_shelf_limit():
    games = [g(f"p{i}", i) for i in range(12)]
    frame = {"installed": [{"package": x["package"]} for x in games]}
    out = shelf_games(games, dict(DEFAULT_FILTERS), frame, True)
    assert len(out) == 8 and out[0]["package"] == "p11"
    assert len(shelf_games(games, dict(DEFAULT_FILTERS), frame, True, limit=3)) == 3


@pytest.mark.parametrize("change", [{"q": "x"}, {"where": "frame"}, {"platform": "pcvr"}, {"status": "works"},
                                    {"tags": ["Favorite"]}])
def test_shelf_hides_while_filtering(change):
    f = {**DEFAULT_FILTERS, **change}
    assert filters_active(f)
    assert shelf_games(GAMES, f, FRAME, True) == []


def test_shelf_ignores_sort_and_blank_search():
    f = {**DEFAULT_FILTERS, "sort": "size", "q": "  "}
    assert not filters_active(f)
    assert shelf_games(GAMES, f, FRAME, True)


def test_shelf_hides_without_frame_but_stays_in_select_mode():
    f = dict(DEFAULT_FILTERS)
    assert shelf_games(GAMES, f, FRAME, False) == []
    assert shelf_games(GAMES, f, None, False) == []
    # select mode keeps it: hiding it shifted the grid under a drag-select
    assert shelf_games(GAMES, f, FRAME, True, select_mode=True) == shelf_games(GAMES, f, FRAME, True)
    assert shelf_games(GAMES, f, {"installed": []}, True) == []


def hls(hex_):
    return colorsys.rgb_to_hls(*(int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)))


@pytest.mark.parametrize("rgb", [(255, 0, 0), (0, 0, 0), (255, 255, 255), (20, 200, 240), (128, 128, 128),
                                 (250, 240, 30), (10, 5, 40)])
def test_tint_is_clamped(rgb):
    out = tint_from_rgb(rgb)
    assert out.startswith("#") and len(out) == 7
    _, lum, s = hls(out)
    assert 0.35 - 0.01 <= lum <= 0.55 + 0.01
    assert s <= 0.55 + 0.01


def test_tint_keeps_hue_and_mild_colours():
    h0 = colorsys.rgb_to_hls(20 / 255, 200 / 255, 240 / 255)[0]
    assert abs(hls(tint_from_rgb((20, 200, 240)))[0] - h0) < 0.02
    assert tint_from_rgb((115, 89, 64)) == "#735940"  # already inside the range: unchanged


def test_dominant_rgb_prefers_colour():
    black, white, red, blue = (5, 5, 5), (250, 250, 250), (200, 30, 30), (30, 30, 200)
    assert dominant_rgb([(100, black), (80, white), (10, red), (20, blue)]) == blue
    assert dominant_rgb([(100, black), (5, (120, 120, 120))]) == black  # nothing colourful: the most common
    assert dominant_rgb([]) is None


def test_compute_tint_cached_next_to_the_art(tmp_path, monkeypatch):
    from PIL import Image

    from frameport.artwork import thumbs

    src = tmp_path / "portrait.png"
    im = Image.new("RGB", (60, 90), (8, 8, 8))
    im.paste((40, 90, 220), (0, 0, 60, 30))  # a third blue on black
    im.save(src)
    monkeypatch.setattr(thumbs, "pick", lambda pkg, kinds: src)
    monkeypatch.setattr(thumbs, "_tints", {})
    assert thumbs.cached_tint("x.tint") == (False, None)
    tint = thumbs.compute_tint("x.tint")
    h, lum, s = hls(tint)
    assert 0.55 < h < 0.7 and 0.35 <= lum <= 0.56 and s <= 0.56  # blue, clamped
    assert thumbs.cached_tint("x.tint") == (True, tint)
    cached = list(tmp_path.glob("t_tint_*.json"))
    assert len(cached) == 1
    cached[0].write_text('{"tint": "#123456"}', encoding="utf-8")  # the cached answer is used: no image work
    assert thumbs.compute_tint("x.tint") == "#123456"
