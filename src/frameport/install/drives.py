"""Games on another drive of the Frame (a microSD card, GitHub #90): which drives there are, where new games go
(library setting `install.drive`) and moving an installed game. No Flet here (the CLI and GUI share it).

The agent keeps each game's anchor (launch.sh, deployment.json, artwork) on internal storage and its files in
`<drive>/FramePort/<package>`; a drive is given by its install dir (or mount point), internal storage by "internal"."""
from __future__ import annotations

import time

from ..core import library
from ..core.events import Reporter
from ..frame.connection import AgentFailed, Frame

SETTING = "install.drive"
INTERNAL = "internal"


def list_drives(frame: Frame) -> list[dict]:
    """The agent's `drives`: [{id, path, install_dir, label, fstype, internal, removable, free_bytes, total_bytes,
    usable, reason, games}]."""
    return frame.agent("drives")["drives"]


def install_dest() -> str | None:
    """Where new games go (None = internal storage)."""
    value = library.setting(SETTING) or INTERNAL
    return None if value == INTERNAL else value


def set_install_dest(install_dir: str | None) -> None:
    library.set_setting(SETTING, install_dir or INTERNAL)


def free_text(d: dict) -> str:
    """"112.4 GiB free" ("" when unknown)."""
    from ..i18n import tr

    free = d.get("free_bytes")
    return tr("{size:.1f} GiB free").format(size=free / 2**30) if isinstance(free, (int, float)) else ""


def drive_text(d: dict) -> str:
    """"SD Card · 112.4 GiB free" (labels for menus and dropdowns)."""
    return " · ".join(filter(None, [d.get("label") or d.get("path"), free_text(d)]))


def game_drive(dep: dict | None) -> dict | None:
    """{"internal", "path", "label", "missing"} of an installed game (the agent's list_installed entry), or None for
    agents before v63."""
    if not dep or "drive" not in dep:
        return None
    return {**dep["drive"], "missing": bool(dep.get("drive_missing"))}


def is_on(dep: dict | None, d: dict) -> bool:
    """The game (list_installed entry) is on drive d (a `drives` entry)."""
    g = game_drive(dep)
    if not g:
        return d.get("internal", False)
    return bool(g["internal"]) if d.get("internal") else (not g["internal"] and g["path"] == d["path"])


def move(frame: Frame, package: str, dest: str, reporter: Reporter, poll: float = 2.0,
         timeout: float = 6 * 3600, sleep=time.sleep) -> dict:
    """Move an installed game to `dest` (a drive's install dir or "internal"). The copy runs on the Frame by itself;
    this follows its progress (a reconnect picks a running move of the same game up again). Cancel works until the
    copy has started."""
    reporter.stage("Move game files")
    st = frame.agent("move_status")
    if not (st.get("state") == "running" and st.get("package") == package):
        reporter.check_cancel()
        try:
            st = frame.agent("move", package=package, dest=dest)
        except AgentFailed as exc:
            # a job that lost the Frame runs again: the move it started may have finished meanwhile
            if "already there" in str(exc):
                reporter.check("Game files moved", True, "already there")
                return {"state": "done", "package": package}
            raise
        reporter.log(f"moving {st['from']} → {st['to']} ({st['total_bytes'] / 2**30:.1f} GiB"
                     + (", same drive" if st.get("same_drive") else "") + ")")
    end = time.time() + timeout
    while time.time() < end:
        st = frame.agent("move_status")
        if st.get("state") in ("done", "failed"):
            break
        total, done = st.get("total_bytes") or 0, st.get("done_bytes") or 0
        phase = {"copying": "Copying", "verifying": "Checking the copy", "removing": "Removing the old copy"}.get(
            st.get("phase") or "", "Starting")
        reporter.progress(done / total if total else 0.0,
                          f"{phase} {done / 2**30:.1f}/{total / 2**30:.1f} GiB" if total else phase)
        sleep(poll)
    else:
        raise RuntimeError("the move didn't finish in time; it may still be running on the Frame")
    if st.get("state") != "done":
        raise RuntimeError(f"moving failed: {st.get('error') or 'unknown error'} (the game stays where it was)")
    reporter.check("Game files moved", True, st.get("base") or st.get("to") or "")
    return st
