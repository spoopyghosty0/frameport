"""Colour themes (ui.theme) and FramePort's own icons (ui.glyphs)."""
from __future__ import annotations

import ast
from pathlib import Path

import flet as ft
import pytest

from frameport.ui import components as C
from frameport.ui import glyphs
from frameport.ui import theme as T
from frameport.ui.views import activity

UI = Path(__file__).resolve().parents[1] / "src" / "frameport" / "ui"


@pytest.fixture(autouse=True)
def default_theme():
    T.set_theme(T.DEFAULT_THEME)
    yield
    T.set_theme(T.DEFAULT_THEME)


def test_themes_define_every_token():
    keys = set(T.THEMES[T.DEFAULT_THEME])
    for name, tokens in T.THEMES.items():
        assert set(tokens) == keys, name
        assert all(v.startswith("#") and len(v) == 7 for v in tokens.values()), name


def test_set_theme_switches_tokens_and_falls_back():
    T.set_theme("classic")
    assert (T.THEME, T.ACCENT) == ("classic", T.THEMES["classic"]["ACCENT"])
    T.set_theme("no-such-theme")
    assert (T.THEME, T.ACCENT) == (T.DEFAULT_THEME, T.THEMES[T.DEFAULT_THEME]["ACCENT"])
    assert T.theme_from_setting(None) == T.DEFAULT_THEME


def test_module_level_maps_follow_the_theme():
    T.set_theme("classic")
    assert C.STATUS_STYLE["works"][1] == T.THEMES["classic"]["OK"]
    assert C.INSTALL_STYLE["on_pc"][2] == T.THEMES["classic"]["PC"]
    assert activity.STATE_STYLE["running"][1] == T.THEMES["classic"]["ACCENT"]
    T.set_theme("portal")
    assert C.STATUS_STYLE["works"][1] == T.THEMES["portal"]["OK"]
    assert activity.CHECK_ICON[False][1] == T.THEMES["portal"]["ERROR"]


def test_no_theme_colour_is_frozen_in_a_default_argument():
    """A default like color=T.TEXT keeps the colour of the theme active at import (a theme switch wouldn't reach it)."""
    found = []
    for f in UI.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                    for sub in ast.walk(d):
                        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                                and sub.value.id == "T" and sub.attr in T.THEMES[T.DEFAULT_THEME]:
                            found.append(f"{f.name}:{d.lineno} T.{sub.attr}")
    # updater._notes_style's inner text() is defined anew on every call: its default is current
    assert [x for x in found if not x.startswith("updater.py")] == []


def test_glyphs_install_and_tint(tmp_path):
    folder = glyphs.install(tmp_path)
    names = {p.stem for p in folder.glob("*.svg")}
    assert {"logo", "logo-solid", "frame", "port", "test", "pc"} <= names
    assert glyphs.path(glyphs.FRAME, tmp_path) == folder / "frame.svg"
    before = (folder / "frame.svg").stat().st_mtime_ns
    glyphs.install(tmp_path)  # unchanged files aren't rewritten
    assert (folder / "frame.svg").stat().st_mtime_ns == before
    assert glyphs.is_glyph(glyphs.TEST) and not glyphs.is_glyph(ft.Icons.TUNE_ROUNDED)


def test_as_icon_turns_glyphs_into_tinted_images():
    img = C.as_icon(glyphs.FRAME, 20, T.ACCENT)
    assert isinstance(img, ft.Image) and img.color == T.ACCENT and img.src.endswith("frame.svg")
    assert isinstance(C.as_icon(ft.Icons.TUNE_ROUNDED, 20), ft.Icon)
    assert C._btn_icon(ft.Icons.TUNE_ROUNDED, T.TEXT) == ft.Icons.TUNE_ROUNDED  # buttons colour Material icons
