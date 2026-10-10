"""Plain explanations for the failures users actually meet (GUI and CLI share them).

`explain(exc)` turns an exception into one sentence that says what happened and what to do; anything it doesn't
recognise keeps its own message. `is_connection_error(exc)` tells whether the Frame dropped off the network (the
install queue then waits for it instead of failing every remaining job).
"""
from __future__ import annotations

import errno
import zipfile

from .i18n import tr


def _paramiko():
    try:
        import paramiko
        from paramiko.ssh_exception import NoValidConnectionsError
        return paramiko, NoValidConnectionsError
    except ImportError:  # pragma: no cover - paramiko is a dependency
        return None, None


def is_connection_error(exc: BaseException) -> bool:
    """The Frame can't be reached (off, asleep, out of Wi-Fi), as opposed to a command that failed on it."""
    paramiko, no_valid = _paramiko()
    if no_valid and isinstance(exc, no_valid):
        return True
    if isinstance(exc, (TimeoutError, EOFError, ConnectionResetError, ConnectionAbortedError,
                        ConnectionRefusedError, BrokenPipeError)):
        return True
    if paramiko and isinstance(exc, paramiko.SSHException) and not isinstance(exc, paramiko.AuthenticationException):
        msg = str(exc).lower()
        return any(s in msg for s in ("not active", "unable to connect", "error reading ssh protocol banner",
                                      "connection reset", "timed out", "socket is closed"))
    if isinstance(exc, OSError) and exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ECONNRESET,
                                                  errno.ETIMEDOUT, errno.ECONNREFUSED, errno.EPIPE):
        return True
    return isinstance(exc, ConnectionError) and "authentication" not in str(exc).lower()


def explain(exc: BaseException) -> str:
    """One sentence for the user: what went wrong and what to do (else the exception's own message)."""
    paramiko, _ = _paramiko()
    msg = " ".join(str(exc).split()).removeprefix("[Errno None] ")
    low = msg.lower()
    if paramiko and isinstance(exc, paramiko.ChannelException):
        return tr("The connection to the Frame is busy. Try again in a moment.")
    if "authentication failed" in low or "no authentication methods available" in low:
        if "password was refused" in low:
            return tr("The Frame refused that password. Check it, or use Steam Frame → Start setup instead.")
        return tr("This Frame isn't set up for FramePort yet. Go to Steam Frame → Start setup.")
    if is_connection_error(exc):
        return tr("Can't reach your Frame. Make sure it's on, awake and on the same network, then try again.")
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return tr("This PC's disk is full. Free some space and try again.")
    if "not enough space on the frame" in low:
        return tr("Not enough space on the Frame ({detail}). Uninstall games you don't play or free up space on "
                  "the Steam Frame page.").format(detail=msg.split(":", 1)[-1].strip())
    if "is running on the frame" in low or low in ("the game is running", "the game is already running") or \
            "a frameport game is running" in low:
        return tr("A game is running on the Frame. Close it in the headset first, then try again.")
    if "steam did not close" in low or "steam isn't running" in low:
        return tr("Steam on the Frame isn't responding. Restart the Frame and try again.")
    if isinstance(exc, zipfile.BadZipFile) or (isinstance(exc, KeyError) and "androidmanifest" in low):
        return tr("The APK file is damaged or incomplete. Get the game again and rescan the folder.")
    if "checksum mismatch" in low:
        return tr("The upload to the Frame was damaged. Try again, ideally over a cable or the Frame's "
                  "hotspot.")
    if msg:
        return msg
    return type(exc).__name__
