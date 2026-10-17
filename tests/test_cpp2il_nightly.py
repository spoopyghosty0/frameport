"""Cpp2IL's development build for Unity metadata newer than its release reads (Unity 6.5: v107, GitHub #198)."""
import json
import zipfile
from pathlib import Path

import pytest

from frameport.analysis import il2cpp
from frameport.tools import cpp2il

ERROR = "---> System.FormatException: Unsupported metadata version found! We support 23-106, got 107"
WANTED = {"Assembly-CSharp/X.cs": ["Run"]}


def test_unsupported_metadata_and_runtime_ids(monkeypatch):
    assert cpp2il.unsupported_metadata("Cpp2IL couldn't read this game's code: " + ERROR)
    assert not cpp2il.unsupported_metadata("Cpp2IL couldn't read this game's code: Unity 2017 isn't supported")
    monkeypatch.setattr(cpp2il.platform, "machine", lambda: "aarch64")
    for plat, rid in (("linux", "linux-arm64"), ("darwin", "osx-arm64"), ("win32", "win-x64")):
        monkeypatch.setattr(cpp2il.sys, "platform", plat)
        assert cpp2il.nightly_rid() == rid
    monkeypatch.setattr(cpp2il.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(cpp2il.sys, "platform", "linux")
    assert cpp2il.nightly_rid() == "linux-x64"


def test_release_path_ignores_the_development_build(monkeypatch, tmp_path):
    monkeypatch.setattr(cpp2il, "tools_dir", lambda: tmp_path)
    monkeypatch.delenv("FRAMEPORT_CPP2IL", raising=False)
    monkeypatch.setattr(cpp2il.sys, "platform", "linux")
    for d in ("cpp2il-2022.1.0-pre-release.21", cpp2il.NIGHTLY_DIR):
        (tmp_path / d).mkdir()
        (tmp_path / d / "Cpp2IL").write_text("")
    assert cpp2il.path() == tmp_path / "cpp2il-2022.1.0-pre-release.21" / "Cpp2IL"


def test_ensure_nightly_unpacks_the_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(cpp2il, "tools_dir", lambda: tmp_path)
    monkeypatch.setattr(cpp2il.sys, "platform", "linux")
    urls = []

    def download(url, dest, progress=None):
        urls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w") as z:
            z.writestr("Cpp2IL", b"\x7fELF fake")
        return dest

    monkeypatch.setattr(cpp2il.cache, "download", download)
    exe = cpp2il.ensure_nightly()
    assert exe == tmp_path / cpp2il.NIGHTLY_DIR / "Cpp2IL" and exe.read_bytes() == b"\x7fELF fake"
    assert urls[0].startswith("https://nightly.link/SamboyCoding/Cpp2IL/") and urls[0].endswith(".zip")
    assert not (tmp_path / cpp2il.NIGHTLY_DIR / "cpp2il.zip").exists()
    assert cpp2il.ensure_nightly() == exe and len(urls) == 1  # downloaded once


@pytest.fixture
def tools(monkeypatch, tmp_path):
    """Release + development build stand-ins: the release can't read metadata v107."""
    monkeypatch.setattr(il2cpp, "cache_dir", lambda: tmp_path)
    release, nightly = Path("release/Cpp2IL"), Path("nightly/Cpp2IL")
    monkeypatch.setattr(cpp2il, "ensure", lambda progress=None: release)
    monkeypatch.setattr(cpp2il, "ensure_nightly", lambda progress=None: nightly)
    runs = []

    def run(exe, lib, metadata, unity_version, wanted, timeout):
        runs.append(exe)
        if exe == release:
            raise il2cpp._Unreadable(ERROR)
        return {"Assembly-CSharp/X.cs": {"Run": (0x1000, 0x20)}}, []

    monkeypatch.setattr(il2cpp, "_run", run)
    return release, nightly, runs


def test_find_methods_falls_back_to_the_development_build(tools, tmp_path):
    release, nightly, runs = tools
    found = il2cpp.find_methods(b"lib", b"meta", "6000.5.6f1", WANTED)
    assert found == {"Assembly-CSharp/X.cs": {"Run": (0x1000, 0x20)}} and runs == [release, nightly]
    assert il2cpp.find_methods(b"lib", b"meta", "6000.5.6f1", WANTED) == found and len(runs) == 2  # cached


def test_cached_release_error_is_retried_once_with_the_development_build(tools, tmp_path):
    release, nightly, runs = tools
    cache = il2cpp._cache_file(b"lib", b"meta")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"unity_version": "6000.5.6f1", "error": "Cpp2IL couldn't read this game's code: "
                                                                         + ERROR}))
    assert il2cpp.find_methods(b"lib", b"meta", "6000.5.6f1", WANTED)
    assert runs == [nightly]
    cache.write_text(json.dumps({"unity_version": "6000.5.6f1", "error": "x: " + ERROR, "nightly": True}))
    with pytest.raises(RuntimeError):  # the development build couldn't either: not run again
        il2cpp.find_methods(b"lib", b"meta", "6000.5.6f1", WANTED)
    assert runs == [nightly]


def test_other_errors_dont_fetch_the_development_build(tools, monkeypatch):
    release, nightly, runs = tools

    def run(exe, *a):
        runs.append(exe)
        raise il2cpp._Unreadable("System.NullReferenceException: Object reference not set")

    monkeypatch.setattr(il2cpp, "_run", run)
    with pytest.raises(RuntimeError):
        il2cpp.find_methods(b"lib", b"meta", "2022.3.1f1", WANTED)
    assert runs == [release]


ICU = ("Process terminated. Couldn't find a valid ICU package installed on the system. Please install libicu\n"
       "   at System.Environment.FailFast(System.String)\n   at Cpp2IL.Program.Main(System.String[])")


def test_dotnet_runtime_failure_is_named_and_not_cached(tools, monkeypatch):
    release, nightly, runs = tools
    assert il2cpp._last_error(ICU).startswith("Process terminated.")

    def run(exe, *a):
        runs.append(exe)
        raise il2cpp._Unreadable(il2cpp._last_error(ICU))

    monkeypatch.setattr(il2cpp, "_run", run)
    with pytest.raises(RuntimeError, match="couldn't start on this PC"):
        il2cpp.find_methods(b"lib", b"meta", "2022.3.9f1", WANTED)
    assert not il2cpp._cache_file(b"lib", b"meta").exists() and runs == [release]


def test_cached_runtime_failure_from_older_versions_is_retried(tools):
    release, nightly, runs = tools
    cache = il2cpp._cache_file(b"lib", b"meta")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"unity_version": "2022.3.9f1", "nightly": False, "error":
                                 "Cpp2IL couldn't read this game's code: at Cpp2IL.Program.Main(System.String[])"}))
    runs.clear()
    # the fixture's release fails on the metadata version -> the development build: both run, nothing cached wrong
    assert il2cpp.find_methods(b"lib", b"meta", "2022.3.9f1", WANTED) and runs == [release, nightly]


def test_cpp2il_runs_with_invariant_globalization(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw.get("env") or {})
        raise il2cpp._Unreadable("stop")

    monkeypatch.setattr(il2cpp.subprocess, "run", fake_run)
    with pytest.raises(il2cpp._Unreadable):
        il2cpp._run(Path("Cpp2IL"), b"lib", b"meta", "2022.3.9f1", WANTED, 5)
    assert seen.get("DOTNET_SYSTEM_GLOBALIZATION_INVARIANT") == "1"
