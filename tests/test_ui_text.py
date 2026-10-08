"""GUI wording rules: every user-visible literal goes through tr(), one vocabulary, "…" only as a real ellipsis."""
import ast
import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "src" / "frameport" / "ui"

# helpers whose arguments at these positions are shown to the user
TEXT_ARGS = {"Text": (0,), "body": (0,), "meta": (0,), "title": (0,), "h2": (0,), "primary": (0,),
             "secondary": (0,), "ghost": (0,), "danger": (0,), "pill": (0,), "callout": (0,), "section": (0,),
             "kv": (0,), "switch": (0,), "empty_state": (1, 2), "Check": (1, 2), "icon_btn": (1,),
             "confirm": (1, 2, 3), "toast": (0,), "tip": (0,), "submit": (0,), "dialog": (0,), "primary_menu": (0,)}
TEXT_KWARGS = {"tooltip", "hint_text", "label", "dialog_title", "action", "subtitle", "text"}
# literals that aren't words to translate: the product name and the wordmark's halves
ALLOWED = {"FramePort", "Frame", "Port"}


def _ui_files():
    return sorted(UI.rglob("*.py"))


def _name(func: ast.AST) -> str:
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _is_word_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):  # f"…" with words in its literal parts
        return any(isinstance(v, ast.Constant) and re.search(r"[A-Za-z]{2}", v.value) for v in node.values)
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in ALLOWED \
        and not node.value.startswith(("http://", "https://")) and bool(re.search(r"[A-Za-z]{2}", node.value))


def _tr_texts():
    """(file, line or help key, text) for every tr()/tr_n() literal in ui/ and every help text."""
    for path in _ui_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and _name(node.func) in ("tr", "tr_n"):
                for a in node.args[:2 if _name(node.func) == "tr_n" else 1]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        yield path, node.lineno, a.value
    from frameport.ui.help import HELP

    for key, text in HELP.items():
        yield UI / "help.py", key, text


def test_visible_literals_are_translated():
    found = []
    for path in _ui_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = _name(node.func)
            pos = TEXT_ARGS.get(name, ())
            if name == "Text" and not (isinstance(node.func, ast.Attribute) and _name(node.func.value) == "ft"):
                pos = ()
            args = [node.args[i] for i in pos if i < len(node.args)]
            args += [k.value for k in node.keywords if k.arg in TEXT_KWARGS]
            found += [f"{path.relative_to(UI)}:{node.lineno}" for a in args if _is_word_literal(a)]
    assert not found, "user-visible text not wrapped in tr(): " + ", ".join(found)


def test_no_three_dots_or_eg_in_ui_text():
    texts = list(_tr_texts())
    dots = [f"{p.name}:{line}" for p, line, text in texts if "..." in text]
    eg = [f"{p.name}:{line}" for p, line, text in texts if re.search(r"\be\.g\.", text)]
    assert not dots, "use … (one character) instead of ...: " + ", ".join(dots)
    assert not eg, "write \"for example\" in user text: " + ", ".join(eg)


def test_one_vocabulary():
    rules = {r"(?i)\bthis computer\b": "this PC", r"(?i)\bconfigs?\b": "recipe", r"(?i)\bexecutable\b": "program",
             r"(?i)\bforce kill\b": "Force quit", r"\d %": "no space before %",
             r"(?i)colour|cancelled": "US spelling", r"Steam ids": "Steam IDs", r"ARM64": "arm64"}
    bad = [f"{p.name}:{line} ({fix})" for p, line, text in _tr_texts()
           for rule, fix in rules.items() if re.search(rule, text)]
    assert not bad, "vocabulary: " + ", ".join(bad)


def test_switch_frame_opens_a_picker_not_disconnect():
    tree = ast.parse((UI / "views" / "frame.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node.func) == "ghost" and node.args and \
                isinstance(node.args[0], ast.Call) and node.args[0].args and \
                getattr(node.args[0].args[0], "value", None) == "Switch Frame…":
            handler = ast.unparse(node.args[2])
            assert "disconnect" not in handler and "switch_frame" in handler
            return
    raise AssertionError("no Switch Frame… button found")
