"""core/clipboard.py: the PowerShell it runs, PNG conversion, the Linux tools it needs."""
from pathlib import Path

import pytest
from PIL import Image

from frameport.core import clipboard


def test_windows_script_quotes_the_path():
    script = clipboard.windows_script(r"C:\Users\O'Brien\shot.jpg")
    assert r"'C:\Users\O''Brien\shot.jpg'" in script
    assert "SetImage" in script and "SetFileDropList" in script and "SetDataObject($data, $true)" in script


def test_as_png_converts_jpeg(tmp_path):
    src = tmp_path / "shot.jpg"
    Image.new("RGB", (8, 6), "orange").save(src)
    out = clipboard.as_png(src, tmp_path / "out")
    with Image.open(out) as im:
        assert im.format == "PNG" and im.size == (8, 6)


def test_missing_file(tmp_path):
    with pytest.raises(clipboard.ClipboardError):
        clipboard.copy_image(tmp_path / "nope.png")


def test_linux_without_tools_says_what_to_install(tmp_path, monkeypatch):
    src = tmp_path / "shot.png"
    Image.new("RGB", (4, 4)).save(src)
    monkeypatch.setattr(clipboard.winhost, "available", lambda: False)
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    monkeypatch.setattr(clipboard.tempfile, "gettempdir", lambda: str(tmp_path))
    with pytest.raises(clipboard.ClipboardError, match="xclip"):
        clipboard.copy_image(Path(src))


def test_linux_uses_wl_copy_on_wayland(tmp_path, monkeypatch):
    src = tmp_path / "shot.png"
    Image.new("RGB", (4, 4)).save(src)
    ran = []
    monkeypatch.setattr(clipboard.winhost, "available", lambda: False)
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(clipboard.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(clipboard.subprocess, "Popen", lambda cmd, **kw: ran.append(cmd))
    clipboard.copy_image(Path(src))
    assert ran == [["wl-copy", "--type", "image/png"]]
