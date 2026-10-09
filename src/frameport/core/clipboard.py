"""Put an image on the system clipboard (no Flet: Flet's own Clipboard.set_image only works in web and mobile apps).

- Windows (and WSL, where the user's clipboard is Windows'): Windows PowerShell + System.Windows.Forms, as a
  bitmap (pastes into chats, Paint, documents) plus the file (pastes into Explorer and file uploads). From WSL the
  image is copied to the Windows user's %TEMP% first: .NET reads that reliably, a \\\\wsl$ path not always.
- macOS: osascript with the image as PNG.
- Linux: wl-copy (Wayland) or xclip (X11), as PNG.

copy_image() raises ClipboardError with a sentence for the user when nothing on this system can do it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import winhost


class ClipboardError(RuntimeError):
    pass


def as_png(path: Path, dest_dir: Path) -> Path:
    """The image as a PNG in dest_dir (PNG input is copied as it is)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / (path.stem + ".png")
    if path.suffix.lower() == ".png":
        shutil.copyfile(path, out)
    else:
        from PIL import Image

        with Image.open(path) as im:
            im.save(out, "PNG")
    return out


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def windows_script(win_path: str) -> str:
    """The PowerShell that puts the image at win_path on the clipboard (bitmap + file)."""
    p = _ps_quote(win_path)
    return ("Add-Type -AssemblyName System.Windows.Forms, System.Drawing; "
            f"$bytes = [IO.File]::ReadAllBytes({p}); "
            "$img = [Drawing.Image]::FromStream((New-Object IO.MemoryStream(,$bytes))); "
            "$data = New-Object Windows.Forms.DataObject; $data.SetImage($img); "
            "$files = New-Object Collections.Specialized.StringCollection; "
            f"[void]$files.Add({p}); $data.SetFileDropList($files); "
            "[Windows.Forms.Clipboard]::SetDataObject($data, $true)")


def _windows(path: Path) -> None:
    src = path
    if winhost.is_wsl():
        temp = winhost.env_path("TEMP")
        if temp is None:
            raise ClipboardError("Windows' temporary folder isn't reachable from WSL")
        folder = temp / "FramePort-clipboard"
        folder.mkdir(parents=True, exist_ok=True)
        src = folder / path.name
        shutil.copyfile(path, src)
    r = winhost.powershell(windows_script(winhost.to_windows(src)), timeout=30)
    if r.returncode:
        raise ClipboardError((r.stderr or r.stdout or "PowerShell failed").strip().splitlines()[-1])


def _mac(path: Path) -> None:
    png = as_png(path, Path(tempfile.gettempdir()) / "frameport-clipboard")
    script = f'set the clipboard to (read (POSIX file "{png}") as «class PNGf»)'
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise ClipboardError(r.stderr.strip() or "osascript failed")


def _linux(path: Path) -> None:
    png = as_png(path, Path(tempfile.gettempdir()) / "frameport-clipboard")
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        cmd = ["wl-copy", "--type", "image/png"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard", "-t", "image/png"]
    else:
        raise ClipboardError("copying pictures needs wl-copy (Wayland) or xclip (X11): install one of them")
    # (both stay running to serve the clipboard: don't wait for them)
    with png.open("rb") as f:
        subprocess.Popen(cmd, stdin=f, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def copy_image(path: Path) -> None:
    """The image file at `path` onto the system clipboard."""
    path = Path(path)
    if not path.is_file():
        raise ClipboardError(f"{path.name} isn't there")
    if winhost.available():
        _windows(path)
    elif sys.platform == "darwin":
        _mac(path)
    else:
        _linux(path)
