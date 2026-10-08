"""Localisation: every GUI text goes through tr()/tr_n(), the translation template is current, translations load."""
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from frameport import i18n

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "frameport" / "ui"
# calls whose first argument is text shown to the user
TEXT_CALLS = {"body", "meta", "title", "h2", "primary", "secondary", "ghost", "pill", "callout", "toast", "switch",
              "section", "Text", "TextButton", "empty_state", "confirm", "kv"}


@pytest.fixture(autouse=True)
def english():
    yield
    i18n.set_language("en")


def test_template_is_up_to_date():
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "i18n_extract.py"), "--check"], capture_output=True,
                       text=True)
    assert p.returncode == 0, p.stderr


def test_no_untranslated_text_in_the_gui():
    """Literal text passed straight to a text control (instead of tr("…")) can't be translated."""
    bad = []
    for path in sorted(UI.rglob("*.py")):
        if path.name in ("help.py", "theme.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name not in TEXT_CALLS or not node.args:
                continue
            first = node.args[0]
            text = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else \
                "".join(v.value for v in first.values if isinstance(v, ast.Constant)) \
                if isinstance(first, ast.JoinedStr) else ""
            if any(c.isalpha() for c in text) and text.strip() not in ("FramePort",):
                bad.append(f"{path.relative_to(ROOT)}:{node.lineno}: {text[:50]!r}")
    assert not bad, "wrap in tr():\n" + "\n".join(bad)


def test_translation_and_plurals(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n, "LOCALES", tmp_path)
    (tmp_path / "xx.json").write_text(json.dumps({"Install on Frame": "Installieren",
                                                  "{n} game": ["{n} Spiel", "{n} Spiele"]}), encoding="utf-8")
    assert i18n.set_language("xx") == "xx" and "xx" in i18n.available()
    assert i18n.tr("Install on Frame") == "Installieren"
    assert i18n.tr("Not translated yet") == "Not translated yet"  # falls back to English
    assert i18n.tr_n("{n} game", "{n} games", 1) == "1 Spiel"
    assert i18n.tr_n("{n} game", "{n} games", 3) == "3 Spiele"
    assert i18n.set_language("zz") == "en"  # unknown language: English
    assert i18n.tr_n("{n} game", "{n} games", 2) == "2 games"


def test_help_lookups_translate_and_unknown_keys_fail():
    from frameport.ui.help import HELP

    with pytest.raises(KeyError):
        HELP["no_such_key"]
    assert HELP["status"].startswith("How well")


def test_sizes():
    assert i18n.fmt_size(3 * 2**30) == "3.0 GiB" and i18n.fmt_size(5 * 2**20) == "5 MiB"


def test_bundles_contain_the_translations():
    """Both packaging paths ship locales/*.json (flet build packages src/; PyInstaller needs --add-data)."""
    text = (ROOT / "scripts" / "package.py").read_text(encoding="utf-8")
    assert "src/frameport/locales" in text and "frameport/locales" in text


def test_tr_n_calls_dont_pass_n_twice():
    """tr_n formats with n itself: tr_n(..., n, n=n) raises TypeError at runtime (0.9.2 pre-release check)."""
    import ast
    from pathlib import Path

    bad = []
    for path in (Path(__file__).parents[1] / "src/frameport").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr_n" and \
                    any(k.arg == "n" for k in node.keywords):
                bad.append(f"{path.name}:{node.lineno}")
    assert not bad, bad
