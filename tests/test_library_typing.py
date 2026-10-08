"""Library type-to-search: which key presses type a character and when they go to the search field."""
import pytest

from frameport.ui.views.library import should_capture, typed_char


@pytest.mark.parametrize("key,shift,want", [
    ("A", False, "a"), ("A", True, "A"), ("z", False, "z"),
    ("1", False, "1"), ("1", True, "!"), ("0", False, "0"),
    (" ", False, " "), ("Space", False, " "),
    ("-", False, "-"), ("-", True, "_"), (".", False, "."), ("'", False, "'"), (",", False, ","),
    ("&", False, "&"), ("Numpad 7", False, "7"), ("Numpad Decimal", False, "."),
])
def test_typed_char_printable(key, shift, want):
    assert typed_char(key, shift) == want


@pytest.mark.parametrize("key", ["Shift Left", "Control Left", "Alt", "Meta Left", "Caps Lock", "Arrow Left",
                                 "Arrow Down", "Home", "Page Up", "F1", "F12", "Escape", "Enter", "Tab",
                                 "Backspace", "Delete", "", "\t"])
def test_typed_char_named_keys(key):
    assert typed_char(key) is None


@pytest.mark.parametrize("mods", [dict(ctrl=True), dict(alt=True), dict(meta=True), dict(ctrl=True, shift=True)])
def test_typed_char_shortcuts(mods):
    assert typed_char("F", **mods) is None
    assert typed_char("1", **mods) is None


def test_should_capture():
    assert should_capture("library", False, False, "a")
    assert not should_capture("game", False, False, "a")  # the game page has no search
    assert not should_capture("files", False, False, "a")
    assert not should_capture("library", True, False, "a")  # a dialog has the keyboard
    assert not should_capture("library", False, True, "a")  # the field types it itself
    assert not should_capture("library", False, False, None)
    assert not should_capture("library", False, False, " ")  # Space on a focused button, not a search
    assert should_capture("library", False, False, " ", "arc")
