"""Self-update (frameport.updates): version logic, release parsing, checks, staging and the swap script."""
import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from frameport import __version__, updates
from frameport.core import cache, library


def release(tag="v9.9.9", assets=("FramePort-windows-x64.zip", "FramePort-macos-arm64.zip",
                                  "FramePort-linux-x64.tar.gz", "SHA256SUMS.txt", "frameport-9.9.9-py3-none-any.whl"),
            **kw):
    return {"tag_name": tag, "body": "What's new: things", "html_url": f"https://example.invalid/releases/{tag}",
            "draft": False, "prerelease": False, "published_at": "2026-10-02T00:00:00Z",
            "assets": [{"name": n, "browser_download_url": f"https://example.invalid/dl/{n}"} for n in assets], **kw}


def test_versions():
    assert updates.parse_version("v0.10.1") > updates.parse_version("0.9.9")
    assert updates.parse_version("1.0.0") > updates.parse_version("1.0.0-rc1")
    assert updates.is_newer("0.2.1", "0.2.0") and not updates.is_newer("0.2.0", "0.2.0")
    assert not updates.is_newer("garbage", "0.1.0")


def test_release_parsing_picks_this_platform_and_skips_drafts():
    up = updates.update_from_release(release(), "FramePort-linux-x64.tar.gz")
    assert (up.version == "9.9.9" and up.asset_url.endswith("linux-x64.tar.gz")
            and up.sums_url.endswith("SHA256SUMS.txt"))
    assert up.wheel_url.endswith(".whl") and "What's new" in up.notes
    assert updates.update_from_release(release(draft=True)) is None
    assert updates.update_from_release(release(prerelease=True)) is None
    assert updates.update_from_release(release(assets=("SHA256SUMS.txt",)), "FramePort-linux-x64.tar.gz").asset is None


def test_check_skip_and_cached_hint(monkeypatch):
    calls = []
    monkeypatch.setattr(cache, "http_get", lambda url, **kw: calls.append(url) or type(
        "R", (), {"text": json.dumps(release()), "raise_for_status": lambda self: None})())
    up = updates.check(force=True)
    assert up and up.version == "9.9.9" and calls == [updates.LATEST_API]
    assert library.setting("update.last_check")
    assert updates.cached_update().version == "9.9.9"  # read from the cache, no network
    updates.skip("9.9.9")
    assert updates.check() is None and updates.cached_update() is None  # "Skip this version" hides it
    assert updates.check(force=True).version == "9.9.9"                 # "Check now" still finds it
    monkeypatch.setattr(cache, "http_get", lambda url, **kw: (_ for _ in ()).throw(OSError("offline")))
    (cache.cache_dir() / "app-release.json").unlink()
    assert updates.check(force=True) is None                            # offline: no update, no error


def test_snooze_hides_a_version_until_it_runs_out(monkeypatch):
    monkeypatch.setattr(cache, "cached_json", lambda *a, **k: release())
    cache.cache_dir().mkdir(parents=True, exist_ok=True)
    (cache.cache_dir() / "app-release.json").write_text(json.dumps(release()), encoding="utf-8")
    now = [1_000_000.0]
    monkeypatch.setattr(updates.time, "time", lambda: now[0])
    updates.snooze("9.9.9", hours=24)
    assert updates.check() is None and updates.cached_update() is None   # "Later": hidden for a day
    assert updates.check(force=True).version == "9.9.9"                  # "Check now" still finds it
    now[0] += 23 * 3600
    assert updates.check() is None
    now[0] += 2 * 3600                                                   # a day later it is offered again
    assert updates.check().version == "9.9.9" and updates.cached_update().version == "9.9.9"
    updates.snooze("9.9.8")                                              # a snooze for another version doesn't hide it
    assert updates.check().version == "9.9.9"
    assert not updates.hidden("9.9.9")


def test_older_release_is_no_update(monkeypatch):
    monkeypatch.setattr(cache, "cached_json", lambda *a, **k: release(tag="v0.0.1"))
    assert updates.check(force=True) is None


def test_bundle_layouts():
    assert updates.bundle_root(Path("C:/Games/FramePort/FramePort.exe"), "win32") == Path("C:/Games/FramePort")
    assert updates.bundle_root(Path("/Applications/FramePort.app/Contents/MacOS/FramePort"), "darwin") == \
        Path("/Applications/FramePort.app")
    assert updates.bundle_root(Path("/home/u/FramePort/FramePort"), "linux") == Path("/home/u/FramePort")
    assert updates.bundle_root(Path("/usr/bin/python3"), "linux") is None
    assert updates.bundle_root(Path("/usr/bin/python3.12"), "darwin") is None


def test_install_kind_from_source_checkout():
    # the test suite runs from the repo: a git checkout
    assert updates.install_kind() == "source"
    cmds = updates.upgrade_commands(updates.update_from_release(release(), None), "source")
    assert cmds[0][:2] == ["git", "-C"] and cmds[0][-2:] == ["pull", "--ff-only"] and cmds[1][:2] == ["uv", "sync"]


def test_wheel_commands(monkeypatch):
    up = updates.update_from_release(release(), None)
    monkeypatch.setattr(sys, "prefix", "/home/u/.local/share/uv/tools/frameport")
    assert updates.upgrade_commands(up, "wheel") == [["uv", "tool", "install", "--force", up.wheel_url]]
    monkeypatch.setattr(sys, "prefix", "/home/u/venv")
    assert updates.upgrade_commands(up, "wheel")[0][1:4] == ["-m", "pip", "install"]


def test_sums_parsing():
    sums = updates.parse_sums("a" * 64 + "  FramePort-linux-x64.tar.gz\n"
                              + "B" * 64 + " *FramePort-windows-x64.zip\njunk\n")
    assert sums == {"FramePort-linux-x64.tar.gz": "a" * 64, "FramePort-windows-x64.zip": "b" * 64}


def _fake_downloads(monkeypatch, files: dict[str, bytes], sums: dict[str, str] | None = None):
    sums = sums or {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    text = "".join(f"{h}  {n}\n" for n, h in sums.items())
    monkeypatch.setattr(cache, "http_get", lambda url, **kw: type("R", (), {"text": text})())

    def download(url, dest, progress=None, expected_sha256=None, expected_sha1=None):
        data = files[url.rsplit("/", 1)[1]]
        if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError("checksum mismatch")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest
    monkeypatch.setattr(cache, "download", download)


def _linux_archive(version: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data, mode in (("FramePort/FramePort", b"#!/bin/sh\necho " + version.encode() + b"\n", 0o755),
                                 ("FramePort/data/version.txt", version.encode(), 0o644)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_prepare_verifies_and_unpacks(monkeypatch):
    up = updates.update_from_release(release(), "FramePort-linux-x64.tar.gz")
    _fake_downloads(monkeypatch, {"FramePort-linux-x64.tar.gz": _linux_archive("9.9.9")})
    app = updates.prepare(up, platform="linux")
    assert (app / "FramePort").is_file() and (app / "data/version.txt").read_text() == "9.9.9"
    assert os.access(app / "FramePort", os.X_OK) or sys.platform == "win32"
    assert updates.ready_update() == ("9.9.9", app)


def test_prepare_rejects_a_bad_checksum(monkeypatch):
    up = updates.update_from_release(release(), "FramePort-linux-x64.tar.gz")
    _fake_downloads(monkeypatch, {"FramePort-linux-x64.tar.gz": _linux_archive("9.9.9")},
                    sums={"FramePort-linux-x64.tar.gz": "0" * 64})
    with pytest.raises(ValueError):
        updates.prepare(up, platform="linux")
    assert updates.ready_update() is None


def test_prepare_rejects_wrong_layout(monkeypatch):
    up = updates.update_from_release(release(), "FramePort-windows-x64.zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("NotFramePort.exe", b"x")
    _fake_downloads(monkeypatch, {"FramePort-windows-x64.zip": buf.getvalue()})
    with pytest.raises(updates.UpdateError):
        updates.prepare(up, platform="win32", current=Path("/nonexistent"))


def test_release_without_sums_is_refused():
    up = updates.update_from_release(release(assets=("FramePort-linux-x64.tar.gz",)), "FramePort-linux-x64.tar.gz")
    with pytest.raises(updates.UpdateError):
        updates.prepare(up, platform="linux")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swap script")
def test_swap_script_replaces_the_installed_folder(monkeypatch, tmp_path):
    up = updates.update_from_release(release(), "FramePort-linux-x64.tar.gz")
    _fake_downloads(monkeypatch, {"FramePort-linux-x64.tar.gz": _linux_archive("9.9.9")})
    app = updates.prepare(up, platform="linux")
    installed = tmp_path / "apps/FramePort"
    installed.mkdir(parents=True)
    (installed / "FramePort").write_text("old")
    (installed / "stale.txt").write_text("old file")
    script = updates.apply(app, installed, relaunch=False, pid=2 ** 22 + 1, platform="linux", wait=True)
    assert script.exists()
    assert (installed / "data/version.txt").read_text() == "9.9.9" and not (installed / "stale.txt").exists()
    assert not (tmp_path / "apps/FramePort.old").exists()
    log = (updates.user_data_dir() / "logs/update.log").read_text()
    assert "update: installed" in log and "not relaunching" in log


def test_windows_script_backs_up_and_relaunches():
    text = updates.swap_script(Path("C:/data/updates/9.9.9/staged"), Path("C:/Games/FramePort"), 1234, "win32", True,
                               Path("C:/data/logs/update.log"))
    assert "Get-Process -Id 1234" in text and "Copy-Item -Path (Join-Path $src '*') -Destination $dst" in text
    assert "previous" in text and "Start-Process -FilePath 'C:" in text and "FramePort.exe" in text


def test_macos_script_clears_quarantine_and_opens():
    text = updates.swap_script(Path("/d/staged/FramePort.app"), Path("/Applications/FramePort.app"), 7, "darwin",
                               True, Path("/d/update.log"))
    assert "xattr -dr com.apple.quarantine '/Applications/FramePort.app'" in text
    assert "open '/Applications/FramePort.app'" in text


def test_version_is_single_sourced():
    text = (Path(__file__).resolve().parents[1] / "src/frameport/_version.py").read_text()
    assert f'"{__version__}"' in text
    assert 'dynamic = ["version"]' in (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()


def test_cli_version_and_update_check(monkeypatch):
    from typer.testing import CliRunner

    from frameport.cli import app

    runner = CliRunner()
    assert runner.invoke(app, ["--version"]).output.strip() == f"FramePort {__version__}"
    monkeypatch.setattr(cache, "cached_json", lambda *a, **k: release())
    res = runner.invoke(app, ["update", "--check"])
    assert res.exit_code == 10 and "FramePort 9.9.9 is available" in res.output and "What's new" in res.output
    monkeypatch.setattr(cache, "cached_json", lambda *a, **k: release(tag="v0.0.1"))
    res = runner.invoke(app, ["update", "--check"])
    assert res.exit_code == 0 and "is the latest version" in res.output


def test_cli_hint_once_a_day_without_waiting(monkeypatch):
    from typer.testing import CliRunner

    from frameport.cli import app

    (cache.cache_dir() / "app-release.json").write_text(json.dumps(release()))
    monkeypatch.setattr(updates, "refresh_cache", lambda: None)  # the cache is fresh anyway: no network
    runner = CliRunner()
    first = runner.invoke(app, ["list"])
    assert "FramePort 9.9.9 is available" in first.output
    assert "FramePort 9.9.9 is available" not in runner.invoke(app, ["list"]).output  # once a day
    monkeypatch.setenv("FRAMEPORT_NO_UPDATE_CHECK", "1")
    library.set_setting("update.cli_hint", None)
    assert "is available" not in runner.invoke(app, ["list"]).output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swap script")
def test_apply_detached_checks_the_script_started(monkeypatch, tmp_path):
    import time

    up = updates.update_from_release(release(), "FramePort-linux-x64.tar.gz")
    _fake_downloads(monkeypatch, {"FramePort-linux-x64.tar.gz": _linux_archive("9.9.9")})
    app = updates.prepare(up, platform="linux")
    installed = tmp_path / "apps/FramePort"
    installed.mkdir(parents=True)
    (installed / "FramePort").write_text("old")
    updates.apply(app, installed, relaunch=False, pid=2 ** 22 + 2, platform="linux")  # returns once the script runs
    log = updates.user_data_dir() / "logs/update.log"
    for _ in range(100):
        if "not relaunching" in log.read_text():
            break
        time.sleep(0.1)
    assert (installed / "data/version.txt").read_text() == "9.9.9"


def test_apply_reports_a_script_that_never_starts(monkeypatch, tmp_path):
    installed = tmp_path / "FramePort"
    installed.mkdir()
    staged = tmp_path / "staged"
    staged.mkdir()
    monkeypatch.setattr(updates, "START_TIMEOUT", 0.5)
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: type("P", (), {"poll": lambda self: None})())
    with pytest.raises(updates.UpdateError):
        updates.apply(staged, installed, relaunch=False, pid=1, platform="linux")


def test_platform_asset_picks_the_arm64_linux_bundle(monkeypatch):
    monkeypatch.setattr(updates.sys, "platform", "linux")
    monkeypatch.setattr(updates.platform, "machine", lambda: "aarch64")
    assert updates.platform_asset() == "FramePort-linux-arm64.tar.gz"
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
    assert updates.platform_asset() == "FramePort-linux-x64.tar.gz"


def test_dev_build_versions_sort_between_releases():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("dev_version", Path(__file__).parents[1] / "scripts/dev_version.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.dev_version("0.9.0", 57) == "0.9.1.dev57"
    pv = updates.parse_version
    assert pv("0.9.0") < pv("0.9.1.dev57") < pv("0.9.1.dev58") < pv("0.9.1")
    assert updates.is_newer("0.9.1", "0.9.1.dev58") and not updates.is_newer("0.9.0", "0.9.1.dev58")


def test_dev_release_is_only_read_on_request():
    release = {"tag_name": "dev", "prerelease": True, "html_url": "https://example/dev", "body": "try X",
               "assets": [{"name": "frameport-0.9.1.dev57-py3-none-any.whl", "browser_download_url": "w"},
                          {"name": "FramePort-windows-x64.zip", "browser_download_url": "z"},
                          {"name": "SHA256SUMS.txt", "browser_download_url": "s"}]}
    assert updates.update_from_release(release, "FramePort-windows-x64.zip") is None  # automatic checks skip it
    up = updates.update_from_release(release, "FramePort-windows-x64.zip", dev=True)
    assert up.version == "0.9.1.dev57" and up.asset_url == "z" and up.sums_url == "s" and up.notes == "try X"
    assert updates.update_from_release({**release, "assets": []}, dev=True) is None  # no wheel: no version
