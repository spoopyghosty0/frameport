"""fexrift.py's extension modules (fexrift_<area>.py) and per-game data (fexrift_games.json)."""
import importlib.util
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_copy(monkeypatch, tmp_path, ext_source=None):
    """fexrift.py copied into its own folder (HERE), optionally with one extension module next to it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "artifacts"
    d.mkdir()
    shutil.copy2(ROOT / "agent" / "fexrift.py", d / "fexrift.py")
    shutil.copy2(ROOT / "agent" / "fexrift_games.json", d / "fexrift_games.json")
    if ext_source:
        (d / "fexrift_perf.py").write_text(ext_source)
    spec = importlib.util.spec_from_file_location("fexrift", d / "fexrift.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_games_json_loaded():
    data = json.loads((ROOT / "agent" / "fexrift_games.json").read_text())
    assert "oculus-first-contact" in data


def test_games_rva_hex(monkeypatch, tmp_path):
    fx = load_copy(monkeypatch, tmp_path)
    fc = fx.GAMES["oculus-first-contact"]
    assert fc["ime_patch"]["rva"] == 0x22a605
    assert not any(k.startswith("_") for k in fc) and "_comment" not in fx.GAMES


def test_extension_registers_command_and_hooks(monkeypatch, tmp_path):
    src = (
        "def register(core):\n"
        "    core.COMMANDS['hello'] = lambda args: {'root': core.ROOT}\n"
        "    core.LAUNCHER_ENV_HOOKS.append(lambda app, info: ['export FP_TEST=1'])\n"
    )
    fx = load_copy(monkeypatch, tmp_path, src)
    fx.load_extensions()
    assert fx.LOADED_EXTENSIONS == ["perf"]
    monkeypatch.setattr(fx, "ROOT", "/patched")
    assert fx.COMMANDS["hello"]({}) == {"root": "/patched"}
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    fx.write_launcher("oculus-first-contact", fx.GAMES["oculus-first-contact"], str(anchor))
    assert "export FP_TEST=1\n" in (anchor / "launch.sh").read_text()


def test_no_extensions_is_fine(monkeypatch, tmp_path):
    fx = load_copy(monkeypatch, tmp_path)
    fx.load_extensions()
    assert fx.LOADED_EXTENSIONS == []


def test_artifact_files_include_extensions_and_data():
    from frameport import fexrift

    rels = {rel for _, rel in fexrift.artifact_files()}
    assert {"fexrift.py", "fexrift_games.json"} <= rels


def test_signed_in_needs_a_user_id_folder(monkeypatch, tmp_path):
    fx = load_copy(monkeypatch, tmp_path)
    sessions = tmp_path / "sessions"
    assert not fx.signed_in(str(sessions))
    (sessions / "_oaf").mkdir(parents=True)
    (sessions / "_oaf" / "data.sqlite").write_text("x")          # what the runtime alone creates
    assert not fx.signed_in(str(sessions))
    (sessions / "x1368").mkdir()
    assert not fx.signed_in(str(sessions))
    (sessions / "1368740236322643-1275611841").mkdir()                  # the real shape: <user id>-<n>
    assert fx.signed_in(str(sessions))


def test_ime_patch_off_in_profile(monkeypatch, tmp_path):
    fx = load_copy(monkeypatch, tmp_path)
    assert fx.GAMES["chronos"]["ime_patch"] == "off"
