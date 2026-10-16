"""Room storage and offline scanner operations on a connected Frame. No Flet."""
from __future__ import annotations

import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..core.paths import artifacts_dir, user_data_dir
from ..frame.connection import sh_quote
from ..mr.room import dumps, parse, preview_svg, read_mesh
from .livestream import sync_helper


def status(frame) -> dict:
    return frame.agent("room_status")


def delete(frame, universe: int | None = None) -> dict:
    return frame.agent("room_delete", **({"universe": universe} if universe is not None else {}))


def from_boundary(frame, height: float = 2.5) -> dict:
    return frame.agent("room_from_boundary", height=height)


def capture(frame, seconds: float = 90, wait: float = 180) -> dict:
    """Start a scan in the headset (the agent records the passthrough mesh, then builds the room)."""
    ensure_scanner(frame)
    ensure_prompts(frame)
    result = frame.agent("room_capture", seconds=seconds, wait=wait)
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result


def capture_status(frame) -> dict:
    return frame.agent("room_capture_status")


SCAN_DONE = ("done", "failed", "none")


def scan(frame, seconds: float = 90, wait: float = 180, progress=None, poll: float = 2.0,
         sleep=time.sleep) -> dict:
    """capture() and wait for the result. progress(status) gets every status the agent reports (state waiting ->
    recording -> building -> done|failed; directions seen, looked_up). Returns the final status; raises on failure."""
    capture(frame, seconds, wait)
    deadline = time.monotonic() + wait + seconds + 600  # + building time
    while True:
        st = capture_status(frame)
        if progress:
            progress(st)
        if st.get("state") in SCAN_DONE:
            break
        if time.monotonic() > deadline:
            raise RuntimeError("The room scan didn't finish")
        sleep(poll)
    if st.get("state") != "done":
        raise RuntimeError(st.get("error") or "The room scan didn't finish")
    return st


def ensure_prompts(frame) -> str:
    """The in-headset scan prompts (artifacts/roomscan/*.png) next to the scanner, synced by digest in one go."""
    import hashlib

    local = artifacts_dir() / "roomscan"
    remote = f"{frame.home}/.local/share/frameport/bin/roomscan-prompts"
    files = sorted(local.glob("*.png"))
    if not files:
        return remote
    want = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    _, out, _ = frame.run(f"mkdir -p {sh_quote(remote)} && cd {sh_quote(remote)} && sha256sum -- *.png 2>/dev/null")
    have = {}
    for line in out.splitlines():
        digest, _, name = line.partition("  ")
        have[name.strip()] = digest
    for f in files:
        if have.get(f.name) != want[f.name]:
            frame.put(f, f"{remote}/{f.name}", resume=False)
    return remote


@contextmanager
def _upload_dir(frame):
    remote = f"{frame.home}/.local/share/frameport/rooms/.incoming-{uuid.uuid4().hex}"
    code, _, err = frame.run(f"mkdir -p {sh_quote(remote)}")
    if code:
        raise RuntimeError(err.strip())
    try:
        yield remote
    finally:
        frame.run(f"rm -rf -- {sh_quote(remote)}")


def put(frame, path: Path) -> dict:
    path = Path(path)
    room = parse(path.read_text(encoding="utf-8"))
    mesh_path = None
    if room.mesh:
        mesh_path = path.parent / room.mesh.filename
        mesh = read_mesh(mesh_path.read_bytes())
        if (mesh.vertex_count, mesh.index_count) != (room.mesh.vertex_count, room.mesh.index_count):
            raise ValueError("room mesh counts do not match room.txt")
    staging = user_data_dir() / "rooms"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="import-", dir=staging) as local, _upload_dir(frame) as remote:
        canonical = Path(local) / "room.txt"
        canonical.write_text(dumps(room), encoding="utf-8")
        frame.put(canonical, f"{remote}/room.txt", resume=False)
        if mesh_path:
            frame.put(mesh_path, f"{remote}/{room.mesh.filename}", resume=False)
        return frame.agent("room_put", path=f"{remote}/room.txt")


def export(frame, directory: Path) -> Path:
    st = status(frame)
    if not st.get("room_text"):
        raise ValueError("No room is stored on the Frame")
    room = parse(st["room_text"])
    directory = Path(directory)
    mesh_data = None
    if room.mesh:
        remote = f"{frame.home}/.local/share/frameport/rooms/{room.universe}/{room.mesh.filename}"
        with frame.sftp.open(remote, "rb") as f:
            mesh_data = f.read(64 * 1024 * 1024)
        mesh = read_mesh(mesh_data)
        if (mesh.vertex_count, mesh.index_count) != (room.mesh.vertex_count, room.mesh.index_count):
            raise ValueError("room changed during export; try again")
    directory.mkdir(parents=True, exist_ok=True)
    if mesh_data is not None:
        (directory / room.mesh.filename).write_bytes(mesh_data)
    path = directory / "room.txt"
    path.write_text(dumps(room), encoding="utf-8")
    return path


def ensure_scanner(frame) -> str:
    local = artifacts_dir() / "linux-arm64-bin" / "fp_roomscan"
    if not local.is_file():
        raise RuntimeError("This build does not include the room scanner yet")
    return sync_helper(frame, local, "fp_roomscan")


def build(frame, dump: Path, universe: int | None = None) -> dict:
    dump = Path(dump)
    with dump.open("rb") as f:
        if f.read(8) != b"FPRS\x01\0\0\0":
            raise ValueError("expected FPRS version 1 scan dump")
    ensure_scanner(frame)
    with _upload_dir(frame) as remote:
        frame.put(dump, f"{remote}/scan.fprs", resume=False)
        return frame.agent("room_build", dump=f"{remote}/scan.fprs",
                           **({"universe": universe} if universe is not None else {}))


def show(frame, path: Path) -> dict:
    from ..mr.room import summary

    st = status(frame)
    if not st.get("room_text"):
        raise ValueError("No room is stored on the Frame")
    room = parse(st["room_text"])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(preview_svg(room), encoding="utf-8")
    return summary(room)
