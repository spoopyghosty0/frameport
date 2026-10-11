"""Experimental: Oculus Rift games that need Meta's PC runtime, on the Steam Frame alone.

x86_64 GE-Proton under FEX runs Meta's runtime, Revive and the game in one Wine prefix on the Frame (research and
measurements: native/fexwine/README.md, CLAUDE.md "Rift via x86_64 Wine under FEX"). CLI only, behind
FRAMEPORT_EXPERIMENTAL_FEX=1 (`frameport fex ...`); verified game: Oculus First Contact.

Steps: setup (uploads agent/fexrift.py + artifacts/fexrift + a patched Revive 3.2.0, then the Frame downloads
GE-Proton and Meta's runtime itself) -> import-login (the Meta login and the games Meta's app downloaded, from a
signed-in Wine prefix on this PC) -> install (per-game fixes, launcher, Steam shortcut).
The login archive holds sign-in tokens: it is written owner-only, sent over SSH and deleted on both sides.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import time
from pathlib import Path

from .core.paths import agent_dir, artifacts_dir, user_data_dir
from .core.events import Reporter

EXPERIMENT_ENV = "FRAMEPORT_EXPERIMENTAL_FEX"

# Revive 3.2.0's LibRevive64.dll: the visibility fixes (IsVisible / HmdMounted / HasInputFocus always true: no pauses
# on the Frame's brief focus dips) and eye textures at 67 % of the recommended size (ovr_GetFovTextureSize divides by
# 1.5: the Frame's GPU can't fill the full size at 72 Hz). (file offset, original bytes, new bytes)
REVIVE_VERSION = "3.2.0"
REVIVE_DLL_SHA256 = "ef9ffd5342bb4afad9b1c0f4fee1ca3bf32f9d3159d49dd841cc9e22f02663e4"
REVIVE_PATCHES = [
    (0x1008c, "740d", "9090"),
    (0x122af, "15", "f1"),
    (0x122b1, "04", "03"),
    (0x151f8, "ff9030010000", "b00190909090"),
    (0x1563a, "0f94c0", "b00190"),
    (0x15807, "ff9230010000", "b00190909090"),
]

# games verified with this mode: Meta app name (folder under Software/Software) -> FramePort package + title
GAMES = {"oculus-first-contact": {"package": "rift.oculus_first_contact", "title": "Oculus First Contact"}}

# what a signed-in prefix contributes (relative to drive_c), tar top-level name -> path
META = "Program Files/Meta Horizon"


def enabled() -> bool:
    return os.environ.get(EXPERIMENT_ENV) == "1"


def require_enabled() -> None:
    if not enabled():
        raise RuntimeError(f"Rift games with Meta's runtime on the Frame are experimental: set {EXPERIMENT_ENV}=1")


def artifact_files() -> list[tuple[Path, str]]:
    """(local file, path under the Frame's artifacts/) for everything fexrift.py needs, except Revive."""
    root = artifacts_dir() / "fexrift"
    out = [(p, p.relative_to(root).as_posix()) for p in sorted(root.rglob("*")) if p.is_file()]
    helper = agent_dir() / "fexrift.py"   # release bundles carry a .txt copy (their .py files are compiled away)
    out.append((helper if helper.exists() else agent_dir() / "fexrift.py.txt", "fexrift.py"))
    return out


def patch_revive(src: Path, dest: Path) -> Path:
    """Copy Revive's runtime files to dest with REVIVE_PATCHES applied to LibRevive64.dll (Revive 3.2.0 only)."""
    from .tools import revive as revive_tool

    dll = src / "LibRevive64.dll"
    if not dll.is_file() or hashlib.sha256(dll.read_bytes()).hexdigest() != REVIVE_DLL_SHA256:
        raise RuntimeError(f"this mode needs Revive {REVIVE_VERSION} (its LibRevive64.dll is patched byte by byte); "
                           f"found another build in {src}")
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    for f in revive_tool.runtime_files(src):
        target = tmp / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
    data = bytearray((tmp / "LibRevive64.dll").read_bytes())
    for off, old, new in REVIVE_PATCHES:
        o, n = bytes.fromhex(old), bytes.fromhex(new)
        if bytes(data[off:off + len(o)]) != o:
            raise RuntimeError(f"Revive {REVIVE_VERSION}: unexpected bytes at {off:#x}")
        data[off:off + len(n)] = n
    (tmp / "LibRevive64.dll").write_bytes(bytes(data))
    shutil.rmtree(dest, ignore_errors=True)
    tmp.replace(dest)
    return dest


def setup(frame, reporter: Reporter, wait: float = 3600) -> dict:
    """Upload fexrift.py, its binaries and the patched Revive, then run the setup on the Frame and follow it."""
    from .install import installer
    from .tools import revive as revive_tool

    require_enabled()
    reporter.stage("Prepare the Frame")
    prep = frame.agent("fex_prepare")
    if not prep.get("fex"):
        raise RuntimeError("FEX isn't installed on the Frame (Steam app 3127680, installed with Proton for x86 games)")
    if prep["free_bytes"] < 12 << 30:
        raise RuntimeError(f"needs about 12 GiB free on the Frame, have {prep['free_bytes'] / 2**30:.1f} GiB")
    src = revive_tool.revive_dir() or revive_tool.install()
    revive = patch_revive(src, user_data_dir() / "fexrift" / "revive")
    items = [(p, rel, p.stat().st_size) for p, rel in artifact_files()]
    items += [(p, "revive/" + p.relative_to(revive).as_posix(), p.stat().st_size)
              for p in sorted(revive.rglob("*")) if p.is_file()]
    total = sum(i[2] for i in items)
    reporter.stage(f"Upload FramePort's files ({len(items)} files, {total / 2**20:.0f} MiB)")
    installer.upload_files(frame, items, prep["artifacts"], reporter, max(total, 1))
    reporter.stage("Set up GE-Proton, FEX and Meta's runtime on the Frame (10-20 minutes)")
    frame.agent("fex_setup")
    end = time.time() + wait
    last = None
    while time.time() < end:
        time.sleep(10)
        try:
            st = frame.agent("fex_status", timeout=90)
        except Exception:   # noqa: BLE001 - SSH hiccups while the Frame works hard
            continue
        setup_st = st.get("setup") or {}
        if setup_st.get("step") != last and setup_st.get("step"):
            last = setup_st["step"]
            reporter.log(f"setup step: {last}")
        if setup_st.get("state") == "failed":
            raise RuntimeError(f"setup failed: {setup_st.get('error')}")
        if setup_st.get("state") == "done":
            for d in setup_st.get("done", []):
                reporter.check(f"setup: {d['step']}", True, str(d.get("result")))
            return st
    raise RuntimeError("the setup on the Frame didn't finish in time; check again with `frameport fex status`")


def find_user_dir(prefix: Path) -> Path:
    users = prefix / "drive_c" / "users"
    for u in sorted(users.iterdir()) if users.is_dir() else []:
        if (u / "AppData" / "Roaming" / "Oculus" / "sessions").is_dir():
            return u
    raise RuntimeError(f"no Meta login in {prefix} (sign in with Meta's app in that prefix first)")


def login_archive(prefix: Path, apps: list[str], out: Path) -> dict:
    """tar of the login (sessions/, CoreData/), the app manifests and the given apps, from a signed-in prefix."""
    user = find_user_dir(prefix)
    meta = prefix / "drive_c" / META
    trees = {"sessions": user / "AppData" / "Roaming" / "Oculus" / "sessions", "CoreData": meta / "CoreData"}
    for name, path in trees.items():
        if not path.is_dir() or not any(path.iterdir()):
            raise RuntimeError(f"{name} missing in {prefix}: sign in with Meta's app there first")
    out.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    apps_found = []
    try:
        with tarfile.open(out, "w") as t:
            for name, path in trees.items():
                t.add(path, arcname=name)
            manifests = meta / "Software" / "Manifests"
            for app in apps:
                for f in sorted(manifests.glob(f"{app}.json*")) if manifests.is_dir() else []:
                    t.add(f, arcname=f"Manifests/{f.name}")
                game = meta / "Software" / "Software" / app
                if game.is_dir():
                    t.add(game, arcname=f"Software/{app}")
                    apps_found.append(app)
    finally:
        os.umask(old_umask)
    return {"archive": out, "apps": apps_found, "bytes": out.stat().st_size}


def import_login(frame, prefix: Path, reporter: Reporter, apps: list[str] | None = None) -> dict:
    require_enabled()
    apps = apps or list(GAMES)
    tmp = user_data_dir() / "fexrift" / "login.tar"
    try:
        reporter.stage("Pack the Meta login")
        info = login_archive(prefix, apps, tmp)
        missing = [a for a in apps if a not in info["apps"]]
        if missing:
            reporter.check("Games from the prefix", None, f"not downloaded there: {', '.join(missing)}")
        prep = frame.agent("fex_prepare")
        reporter.stage(f"Send the login ({info['bytes'] / 2**20:.0f} MiB)")
        frame.put(tmp, prep["incoming"] + "/login.tar", None)
        res = frame.agent("fex_import_login", timeout=1200)
    finally:
        tmp.unlink(missing_ok=True)
    reporter.check("Meta login on the Frame", bool((res.get("status") or {}).get("login")))
    return res


def install(frame, reporter: Reporter, app: str = "oculus-first-contact", msaa: int | None = None,
            add_to_steam: bool = True) -> dict:
    from .install import installer

    require_enabled()
    if app not in GAMES:
        raise RuntimeError(f"{app} isn't verified for this mode (verified: {', '.join(GAMES)})")
    g = GAMES[app]
    reporter.stage(f"Install {g['title']}")
    res = frame.agent("fex_install", package=g["package"], app=app, title=g["title"], msaa=msaa)
    for k, v in (res.get("fixes") or {}).items():
        if v is not None:
            reporter.check(f"fix: {k}", v not in ("unknown build",), str(v))
    if add_to_steam:
        installer.add_to_steam(frame, [g["package"]], reporter)
    return res
