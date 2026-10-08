"""GUI consistency guard: dialogs, spinners and inputs come from the shared helpers in ui/components.py (C.dialog,
C.viewer, C.spinner, C.field/C.search/C.dropdown), icons are from one family (ROUNDED), and radii are tokens."""
import ast
from pathlib import Path

import flet as ft

UI = Path(__file__).resolve().parents[1] / "src" / "frameport" / "ui"
COMPONENTS = UI / "components.py"
# controls only components.py may create (everything else uses its helper)
SHARED = {"AlertDialog": "C.dialog / C.viewer", "ProgressRing": "C.spinner", "TextField": "C.field / C.search",
          "Dropdown": "C.dropdown", "SegmentedButton": "C.segmented"}
# justified exceptions: (file name, control) -> why
ALLOWED: dict[tuple[str, str], str] = {}


def _files():
    return sorted(UI.rglob("*.py"))


def _ft_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                isinstance(node.func.value, ast.Name) and node.func.value.id == "ft":
            yield node.func.attr, node


def test_shared_controls_only_from_components():
    bad = []
    for path in _files():
        if path == COMPONENTS:
            continue
        for name, node in _ft_calls(ast.parse(path.read_text(encoding="utf-8"))):
            if name in SHARED and (path.name, name) not in ALLOWED:
                bad.append(f"{path.relative_to(UI)}:{node.lineno}: ft.{name}( -> use {SHARED[name]}")
    assert not bad, "\n".join(bad)


def test_rounded_icons_where_flet_has_them():
    names = {m.name for m in ft.Icons}
    bad = []
    for path in _files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and \
                    node.value.attr == "Icons" and node.attr.endswith("_OUTLINED"):
                twin = node.attr.removesuffix("_OUTLINED") + "_ROUNDED"
                if twin in names:
                    bad.append(f"{path.relative_to(UI)}:{node.lineno}: Icons.{node.attr} -> Icons.{twin}")
    assert not bad, "\n".join(bad)


def test_radii_are_tokens():
    """radius=/border_radius= never a bare number in ui/ (T.RADIUS, T.RADIUS_SM, T.RADIUS_XS or T.px(...))."""
    bad = []
    for path in _files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg in ("radius", "border_radius") and \
                    isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
                bad.append(f"{path.relative_to(UI)}:{node.value.lineno}: {node.arg}={node.value.value}")
    assert not bad, "\n".join(bad)


def test_title_sizes_are_tokens():
    """C.title(text, size): the size is a token (T.T_DISPLAY …), never a raw number (it wouldn't scale)."""
    bad = []
    for path in _files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and (isinstance(node.func, ast.Attribute) and node.func.attr == "title"
                                               or isinstance(node.func, ast.Name) and node.func.id == "title"):
                sizes = node.args[1:2] + [k.value for k in node.keywords if k.arg == "size"]
                if any(isinstance(s, ast.Constant) and isinstance(s.value, (int, float)) for s in sizes):
                    bad.append(f"{path.relative_to(UI)}:{node.lineno}")
    assert not bad, "\n".join(bad)


def test_dialog_helper_style():
    """C.dialog: the shared look (12 px corners, semibold title, SURFACE_2, actions at the end, size tokens)."""
    from frameport.ui import components as C
    from frameport.ui import theme as T

    d = C.dialog("Title", ft.Text("body"), [C.ghost("Cancel"), C.primary("OK")], size="s")
    assert d.shape.radius == T.RADIUS and d.bgcolor == T.SURFACE_2
    assert d.title.weight == ft.FontWeight.W_600
    assert d.content.width == T.DIALOG_S
    assert d.actions_alignment == ft.MainAxisAlignment.END
    assert C.dialog("T", ft.Text("b"), size="l").content.width == T.DIALOG_L
    assert C.dialog("T", ft.Text("b"), title_actions=[ft.Text("1 more")]).title.controls[-1].value == "1 more"


def test_danger_matches_primary_metrics():
    from frameport.ui import components as C
    from frameport.ui import theme as T

    p, d = C.primary("Go"), C.danger("Delete")
    assert d.style.padding == p.style.padding and d.style.text_style.size == p.style.text_style.size
    assert d.style.bgcolor[ft.ControlState.DEFAULT] == T.ERROR
    menu = C.primary_menu("Add", ft.Icons.ADD_ROUNDED, [])
    assert isinstance(menu.content, ft.FilledButton) and menu.content.style.padding == p.style.padding


def test_segmented_and_selected_style():
    from frameport.ui import components as C
    from frameport.ui import theme as T

    seen = []
    seg = C.segmented([("a", "A"), ("b", "B")], "a", seen.append)
    first, second = seg.content.controls
    assert seg.data == "a" and first.content.color == T.TEXT and second.content.color == T.TEXT_2
    second.on_click(None)
    assert seen == ["b"] and seg.data == "b" and second.content.color == T.TEXT
    box, icon, text = ft.Container(), ft.Icon(ft.Icons.FOLDER_ROUNDED), ft.Text("x")
    C.selected_style(box, True, icon, text)
    assert text.color == T.TEXT and (box.gradient is not None if T.DUAL else box.bgcolor == T.ACCENT_SOFT)
    C.selected_style(box, False, icon, text)
    assert box.gradient is None and box.bgcolor is None and icon.color == T.TEXT_2
