"""Colour themes (ui.theme: built-ins and installed theme files) and FramePort's own icons (ui.glyphs)."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import flet as ft
import pytest

from frameport.ui import components as C
from frameport.ui import glyphs
from frameport.ui import theme as T
from frameport.ui.views import activity

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "frameport" / "ui"


@pytest.fixture(autouse=True)
def default_theme():
    T.set_theme(T.DEFAULT_THEME)
    yield
    for tid in [t for t in T.THEMES if t.startswith(T.USER_PREFIX)]:
        del T.THEMES[tid]
    T.set_theme(T.DEFAULT_THEME)


def test_builtin_themes():
    assert list(T.BUILTIN) == ["portal", "portal_oled", "original"]
    for tid, theme in T.BUILTIN.items():
        assert set(theme["colors"]) == set(T.TOKENS), tid
        assert all(v.startswith("#") and len(v) == 7 for v in theme["colors"].values()), tid
        T.parse_theme({"base": tid, "colors": theme["colors"]})  # every built-in passes the theme-file checks
    assert T.BUILTIN["portal_oled"]["colors"]["BG"] == "#000000"
    assert T.BUILTIN["portal"]["dual"] and not T.BUILTIN["original"]["dual"]


def test_set_theme_switches_tokens_and_falls_back():
    T.set_theme("original")
    assert (T.THEME, T.ACCENT, T.DUAL) == ("original", T.BUILTIN["original"]["colors"]["ACCENT"], False)
    T.set_theme("classic")  # the earlier name
    assert T.THEME == "original"
    T.set_theme("no-such-theme")
    assert (T.THEME, T.ACCENT, T.DUAL) == (T.DEFAULT_THEME, T.BUILTIN[T.DEFAULT_THEME]["colors"]["ACCENT"], True)
    assert T.theme_from_setting(None) == T.DEFAULT_THEME


def test_module_level_maps_follow_the_theme():
    T.set_theme("original")
    assert C.STATUS_STYLE["works"][1] == T.BUILTIN["original"]["colors"]["OK"]
    assert C.INSTALL_STYLE["on_pc"][2] == T.BUILTIN["original"]["colors"]["PC"]
    assert activity.STATE_STYLE["running"][1] == T.BUILTIN["original"]["colors"]["ACCENT"]
    T.set_theme("portal")
    assert C.STATUS_STYLE["works"][1] == T.BUILTIN["portal"]["colors"]["OK"]
    assert activity.CHECK_ICON[False][1] == T.BUILTIN["portal"]["colors"]["ERROR"]


def test_no_theme_colour_is_frozen_in_a_default_argument():
    """A default like color=T.TEXT keeps the colour of the theme active at import (a theme switch wouldn't reach it)."""
    found = []
    for f in UI.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                    for sub in ast.walk(d):
                        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                                and sub.value.id == "T" and sub.attr in T.TOKENS:
                            found.append(f"{f.name}:{d.lineno} T.{sub.attr}")
    # updater._notes_style's inner text() is defined anew on every call: its default is current
    assert [x for x in found if not x.startswith("updater.py")] == []


def test_theme_file_partial_and_validated():
    t = T.parse_theme({"name": "Ember", "colors": {"accent": "#f80", "secondary": "#00A0FF"}})
    assert t["name"] == "Ember" and t["dual"] is True
    assert t["colors"]["ACCENT"] == "#FF8800" and t["colors"]["SECONDARY"] == "#00A0FF"
    assert t["colors"]["BG"] == T.BUILTIN["portal"]["colors"]["BG"]  # the rest comes from the base
    assert T.parse_theme({"base": "original", "colors": {}})["dual"] is False
    bad = [({"colors": {"ACCENT": "orange"}}, "not a color"),
           ({"colors": {"GLOW": "#FFFFFF"}}, "unknown color"),
           ({"colors": {"BG": "#F4F4F4", "TEXT": "#111111"}}, "too light"),
           ({"colors": {"TEXT": "#202020"}}, "hard to read"),
           ({"base": "nope"}, "isn't a built-in"),
           ({"colours": {}}, "unknown field"),
           ([1, 2], "isn't a theme")]
    for data, message in bad:
        with pytest.raises(T.ThemeError, match=message):
            T.parse_theme(data)


def test_install_load_and_remove_theme_files(tmp_path):
    src = tmp_path / "download.json"
    src.write_text(json.dumps({"name": "Ember Night", "colors": {"ACCENT": "#FFAA00"}}), encoding="utf-8")
    folder = tmp_path / "themes"
    tid = T.install_theme(src, folder)
    assert tid == "user:ember-night" and (folder / "ember-night.json").is_file()
    T.set_theme(tid)
    assert (T.THEME, T.ACCENT) == (tid, "#FFAA00")
    (folder / "broken.json").write_text("{not json", encoding="utf-8")
    problems = T.load_user_themes(folder)
    assert tid in T.THEMES and problems and problems[0].startswith("broken.json: not valid JSON")
    T.remove_theme(tid)
    assert tid not in T.THEMES and not (folder / "ember-night.json").exists()
    assert T.theme_from_setting(tid) == T.DEFAULT_THEME  # a removed theme falls back
    with pytest.raises(T.ThemeError):
        T.remove_theme("portal")


def test_exported_theme_reinstalls(tmp_path):
    f = tmp_path / "mine.json"
    f.write_text(T.theme_json("portal_oled"), encoding="utf-8")
    assert T.read_theme_file(f)["colors"] == T.BUILTIN["portal_oled"]["colors"]


def test_documented_example_theme_is_valid():
    t = T.read_theme_file(ROOT / "docs" / "themes" / "example-theme.json")
    assert t["name"] and t["dual"]


def test_glyphs_install_and_tint(tmp_path):
    folder = glyphs.install(tmp_path)
    names = {p.stem for p in folder.glob("*.svg")}
    assert {"logo", "logo-solid", "frame", "port", "test", "pc"} <= names
    assert glyphs.path(glyphs.FRAME, tmp_path) == folder / "frame.svg"
    before = (folder / "frame.svg").stat().st_mtime_ns
    glyphs.install(tmp_path)  # unchanged files aren't rewritten
    assert (folder / "frame.svg").stat().st_mtime_ns == before
    assert glyphs.is_glyph(glyphs.TEST) and not glyphs.is_glyph(ft.Icons.TUNE_ROUNDED)


def test_wordmark_portal_follows_the_theme(tmp_path):
    a = glyphs.portal_mark(tmp_path, "#ff8a1f", "#3AA8FF", "#0D0E12")
    svg = a.read_text(encoding="utf-8")
    assert "#FF8A1F" in svg and "#3AA8FF" in svg and "#0D0E12" in svg
    assert glyphs.portal_mark(tmp_path, "#FF8A1F", "#3AA8FF", "#0D0E12") == a  # cached per colour set
    assert glyphs.portal_mark(tmp_path, "#FF5A36", "#5CE1E6", "#0D0E12") != a  # an installed theme gets its own
    T.set_theme("original")
    assert isinstance(C.wordmark(17), ft.Text)  # single-accent themes keep the plain wordmark


def test_as_icon_turns_glyphs_into_tinted_images():
    img = C.as_icon(glyphs.FRAME, 20, T.ACCENT)
    assert isinstance(img, ft.Image) and img.color == T.ACCENT and img.src.endswith("frame.svg")
    assert isinstance(C.as_icon(ft.Icons.TUNE_ROUNDED, 20), ft.Icon)
    assert C._btn_icon(ft.Icons.TUNE_ROUNDED, T.TEXT) == ft.Icons.TUNE_ROUNDED  # buttons colour Material icons
