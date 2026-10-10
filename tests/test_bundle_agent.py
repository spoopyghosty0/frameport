"""Issue #2: `flet build` compiles every .py of the app to .pyc, bundled data included, so release bundles had no Frame
agent source to upload. Bundles carry it as frameport_agent.py.txt too; everything reads it through paths.agent_file."""
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from frameport.core import paths

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bundle_like(tmp_path, monkeypatch):
    """A data folder like the one in a release bundle: the agent only compiled, plus the .txt copy."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "frameport_agent.pyc").write_bytes(b"compiled")
    shutil.copy(ROOT / "agent/frameport_agent.py", tmp_path / "agent" / paths.AGENT_SOURCE_COPY)
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    return tmp_path


def test_agent_file_falls_back_to_the_txt_copy(bundle_like):
    assert paths.agent_file().name == "frameport_agent.py.txt"
    from frameport.frame.connection import bundled_agent_version

    assert bundled_agent_version() and bundled_agent_version() > 30


def test_pc_shortcut_code_loads_from_the_txt_copy(bundle_like):
    from frameport.targets import pc_revive

    assert callable(pc_revive._vdf().vdf_decode)


def test_source_checkout_uses_the_py():
    assert paths.agent_file() == paths.agent_dir() / "frameport_agent.py"


def test_package_script_checks_the_bundle(tmp_path):
    spec = importlib.util.spec_from_file_location("package", ROOT / "scripts/package.py")
    package = importlib.util.module_from_spec(spec)
    sys.modules["package"] = package
    spec.loader.exec_module(package)
    data = tmp_path / "app/frameport/_data/agent"
    data.mkdir(parents=True)
    (data / "frameport_agent.pyc").write_bytes(b"x")
    with pytest.raises(SystemExit, match="agent's source is missing"):
        package.check_bundle(tmp_path)
    (data / "frameport_agent.py.txt").write_text("x")
    package.check_bundle(tmp_path)


def test_pc_shortcut_code_loads_without_fcntl(monkeypatch):
    """GitHub #131: on Windows "Install on this PC" loads the agent for its VDF code; its `import fcntl` (POSIX only)
    made every PC install fail."""
    from frameport.targets import pc_revive

    monkeypatch.setitem(sys.modules, "fcntl", None)  # import fails as on Windows
    assert callable(pc_revive._vdf().vdf_decode)
