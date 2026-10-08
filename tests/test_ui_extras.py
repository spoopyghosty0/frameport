"""Setup checklist items, the Settings index and the Reduce motion setting."""
import flet as ft

from frameport.core import library
from frameport.ui import components as C
from frameport.ui import theme as T
from frameport.ui.views import settings


def fix():
    return ("Install", ft.Icons.DOWNLOAD_ROUNDED, lambda: None)


def test_check_defaults_and_tuples():
    c = C.Check(True, "Steam")
    assert (c.detail, c.help, c.fix, c.extra) == ("", None, None, None)
    assert C.Check(*(False, "Java", "missing", "java")).help == "java"


def test_check_marks():
    assert C.check_look(True) == (ft.Icons.CHECK_CIRCLE_ROUNDED, T.OK)
    assert C.check_look(False) == (ft.Icons.ERROR_ROUNDED, T.ERROR)
    assert C.check_look("warn")[1] == T.WARN
    assert C.check_look(None)[0] is None  # pending: a spinner


def test_fix_only_while_not_ready():
    f = fix()
    assert C.check_fix(C.Check(False, "x", fix=f)) is f
    assert C.check_fix(C.Check("warn", "x", fix=f)) is f
    assert C.check_fix(C.Check(True, "x", fix=f)) is None
    assert C.check_fix(C.Check(None, "x", fix=f)) is None


def test_checklist_builds_rows_with_fix_and_extra():
    calls = []
    col = C.checklist([C.Check(False, "Lepton", "missing", None, ("Install", None, lambda: calls.append(1))),
                       (True, "Proton", "ready", "proton", None, C.secondary("Test")),
                       C.Check(None, "Checking…")])
    assert len(col.controls) == 3
    first = col.controls[0].content.controls
    button = first[-1]
    button.on_click(None)
    assert calls == [1]
    assert len(col.controls[1].content.controls) == 3  # mark, text, the extra button (no fix: ready)
    assert isinstance(col.controls[2].content.controls[0].content, ft.ProgressRing)


def test_settings_sections_order():
    s = settings.SECTIONS
    assert s[0] == "appearance" and s[-1] == "remove"
    assert len(set(s)) == len(s)
    assert set(settings.section_titles()) == set(s)
    assert s.index("tools") < s.index("pc") < s.index("data") < s.index("about")


def test_reduce_motion_default_off():
    from frameport.ui.app import REDUCE_MOTION, reduce_motion_setting

    assert reduce_motion_setting() is False
    library.set_setting(REDUCE_MOTION, True)
    assert reduce_motion_setting() is True


def test_callout_icon_alignment():
    """A bar (text + buttons in a Row) centres its icon; wrapping text keeps it at the first line."""
    bar = C.callout(ft.Row([C.body("Update available"), C.ghost("Not now")]), "info")
    assert bar.content.vertical_alignment == ft.CrossAxisAlignment.CENTER
    text = C.callout("A long note that wraps over several lines.", "warn")
    assert text.content.vertical_alignment == ft.CrossAxisAlignment.START
