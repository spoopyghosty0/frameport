"""Where the live view plays: inside FramePort's window, in an mpv window, or in the web browser.

- "app": flet-video's Video control (media_kit = libmpv) plays the relay's /stream.mp4 in the Live view. Windows and
  macOS (bundles and source runs: Flet's desktop clients there contain it). Not in Linux bundles: the plugin links
  libmpv.so.1, and a bundle built with it wouldn't start on systems without that exact library (pyproject installs
  flet-video only off Linux); a Linux source run can use it with Flet's "full" desktop client + libmpv.
- "mpv": the mpv program, when installed, in its own low-latency window (Linux's way to a native player).
- "browser": the MSE player page (install/live_player.py) in the default browser — always available, and where the
  others fall back to when they can't start or fail.
The pure parts (availability, order, mpv settings) are unit-tested; the Video control is built only when available.
"""
from __future__ import annotations

import ctypes.util
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable

MODES = ("app", "mpv", "browser")
SETTING = "ui.live_player"  # "auto" (default) or one of MODES: the user's pick in the Live view

# mpv's low-latency profile, as properties (media_kit sets them before opening; `profile` itself is applied by the
# command line, not reliably as a property): a live stream should show the newest picture, not buffer
LOW_LATENCY = {
    "cache": "no",
    "cache-pause": "no",
    "audio-buffer": 0.05,
    "vd-lavc-threads": 1,
    "demuxer-lavf-o-add": "fflags=+nobuffer",
    "demuxer-lavf-probe-info": "nostreams",
    "demuxer-lavf-analyzeduration": 0.1,
    "video-sync": "audio",
    "interpolation": "no",
    "video-latency-hacks": "yes",
    "stream-buffer-size": "4k",
}


def stream_url(page_url: str) -> str:
    """The relay's raw fragmented MP4 (what native players open) next to its player page."""
    return page_url.rstrip("/") + "/stream.mp4"


def embedded_available(web: bool = False, platform: str | None = None, env: dict | None = None,
                       has_module: Callable[[str], bool] | None = None,
                       find_library: Callable[[str], str | None] | None = None) -> bool:
    """Whether the Video control can play inside the window here (arguments: for tests)."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    has_module = has_module or (lambda name: importlib.util.find_spec(name) is not None)
    find_library = find_library or ctypes.util.find_library
    if web or env.get("FRAMEPORT_NO_EMBEDDED_PLAYER") or not has_module("flet_video"):
        return False
    if platform.startswith("linux"):
        # Flet's default Linux client is "light" (no video plugin): an unknown control would show instead
        return env.get("FLET_DESKTOP_FLAVOR") == "full" and bool(find_library("mpv"))
    return platform in ("win32", "darwin")


def find_mpv(platform: str | None = None, which: Callable[[str], str | None] = shutil.which,
             exists: Callable[[str], bool] = os.path.isfile) -> str | None:
    """The mpv program, if installed (macOS apps don't get Homebrew's folders on PATH: looked at directly)."""
    platform = platform or sys.platform
    found = which("mpv") or (which("mpv.exe") if platform == "win32" else None)
    if found:
        return found
    if platform == "darwin":
        for p in ("/opt/homebrew/bin/mpv", "/usr/local/bin/mpv", "/Applications/mpv.app/Contents/MacOS/mpv"):
            if exists(p):
                return p
    return None


def order(choice: str, embedded: bool, mpv: bool) -> list[str]:
    """The players to try, best first: the user's pick (if available here) first, then app → mpv → browser. The
    browser is always last, so a failing player ends up there."""
    avail = [m for m, ok in (("app", embedded), ("mpv", mpv), ("browser", True)) if ok]
    if choice in avail:
        avail.remove(choice)
        avail.insert(0, choice)
    return avail


def mpv_args(exe: str, url: str, title: str) -> list[str]:
    return [exe, "--profile=low-latency", "--cache=no", "--force-window=immediate", f"--title={title}",
            "--keep-open=no", "--autofit=1280x720", "--no-terminal", url]


class MpvWindow:
    """The stream in an mpv window. `start()` raises when mpv quits at once (it couldn't open the stream)."""

    def __init__(self, exe: str, url: str, title: str):
        self.exe, self.url, self.title = exe, url, title
        self.proc: subprocess.Popen | None = None

    def start(self, settle: float = 2.5) -> MpvWindow:
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW: no console next to mpv's window
        self.proc = subprocess.Popen(mpv_args(self.exe, self.url, self.title), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags)
        deadline = time.time() + settle
        while time.time() < deadline:
            code = self.proc.poll()
            if code is not None:
                err = (self.proc.stderr.read() or b"").decode(errors="replace").strip() if self.proc.stderr else ""
                raise RuntimeError(f"mpv exited ({code}){': ' + err.splitlines()[-1] if err else ''}")
            time.sleep(0.1)
        return self

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.alive:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def video_control(url: str, on_error: Callable, on_complete: Callable, muted: bool = False):
    """The Video control for the stream (only when embedded_available()): no built-in controls (the Live view's bar
    has Stop, Sound and Open in browser), black around the picture, low-latency mpv settings."""
    import flet as ft
    import flet_video as ftv

    return ftv.Video(playlist=[ftv.VideoMedia(url)], autoplay=True, controls=None, expand=True,
                     fill_color=ft.Colors.BLACK, muted=muted, wakelock=False, title="FramePort live view",
                     configuration=ftv.VideoConfiguration(mpv_properties=dict(LOW_LATENCY)),
                     on_error=on_error, on_complete=on_complete)
