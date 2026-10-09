"""Where the live view plays (ui/live_players): in the window, an mpv window, or the browser."""
import sys

import pytest

from frameport.ui import live_players as LP


def avail(**kw):
    base = dict(web=False, platform="win32", env={}, has_module=lambda n: True, find_library=lambda n: None)
    base.update(kw)
    return LP.embedded_available(**base)


def test_embedded_on_windows_and_macos_with_flet_video():
    assert avail() and avail(platform="darwin")
    assert not avail(has_module=lambda n: False)  # flet-video not installed (pyproject: not on Linux)
    assert not avail(web=True)  # web mode (screenshots): the browser itself
    assert not avail(env={"FRAMEPORT_NO_EMBEDDED_PLAYER": "1"})


def test_embedded_on_linux_only_with_the_full_client_and_libmpv():
    """Flet's default Linux client has no video plugin (an unknown control would show instead)."""
    assert not avail(platform="linux")
    assert not avail(platform="linux", env={"FLET_DESKTOP_FLAVOR": "full"})
    assert avail(platform="linux", env={"FLET_DESKTOP_FLAVOR": "full"}, find_library=lambda n: "libmpv.so.2")


def test_order_prefers_the_pick_and_ends_in_the_browser():
    assert LP.order("auto", True, True) == ["app", "mpv", "browser"]
    assert LP.order("auto", False, True) == ["mpv", "browser"]  # e.g. a Linux bundle with mpv installed
    assert LP.order("auto", False, False) == ["browser"]
    assert LP.order("browser", True, True) == ["browser", "app", "mpv"]
    assert LP.order("mpv", True, True) == ["mpv", "app", "browser"]
    assert LP.order("app", False, True) == ["mpv", "browser"]  # picked but not available here
    for choice in ("auto", "app", "mpv", "browser"):
        for e in (True, False):
            for m in (True, False):
                assert "browser" in LP.order(choice, e, m)


def test_find_mpv():
    assert LP.find_mpv("linux", which=lambda n: "/usr/bin/mpv" if n == "mpv" else None) == "/usr/bin/mpv"
    assert LP.find_mpv("win32", which=lambda n: "C:/mpv/mpv.exe" if n == "mpv.exe" else None) == "C:/mpv/mpv.exe"
    assert LP.find_mpv("linux", which=lambda n: None) is None
    # macOS apps don't get Homebrew on PATH
    assert LP.find_mpv("darwin", which=lambda n: None,
                       exists=lambda p: p == "/opt/homebrew/bin/mpv") == "/opt/homebrew/bin/mpv"


def test_stream_url_and_mpv_args():
    assert LP.stream_url("http://127.0.0.1:5123/") == "http://127.0.0.1:5123/stream.mp4"
    args = LP.mpv_args("mpv", "http://127.0.0.1:5123/stream.mp4", "FramePort live view")
    assert args[0] == "mpv" and args[-1].endswith("/stream.mp4") and "--profile=low-latency" in args
    assert LP.LOW_LATENCY["cache"] == "no" and "fflags=+nobuffer" in LP.LOW_LATENCY["demuxer-lavf-o-add"]


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell as a stand-in for mpv")
def test_mpv_window_that_quits_at_once_raises(tmp_path):
    fake = tmp_path / "mpv"
    fake.write_text("#!/bin/sh\necho 'Failed to open stream' >&2\nexit 2\n")
    fake.chmod(0o755)
    with pytest.raises(RuntimeError, match="Failed to open"):
        LP.MpvWindow(str(fake), "http://127.0.0.1:1/stream.mp4", "t").start(settle=2)


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell as a stand-in for mpv")
def test_mpv_window_that_keeps_running_starts_and_stops(tmp_path):
    fake = tmp_path / "mpv"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    w = LP.MpvWindow(str(fake), "http://127.0.0.1:1/stream.mp4", "t").start(settle=0.5)
    assert w.alive
    w.stop()
    assert not w.alive
