"""Quest games on this Windows PC through AXRB (tools/axrb.py): the "This PC" target's Android half.

An install sets AXRB up when needed (its installer + its Android runtime, downloaded only now), starts the emulator,
installs the game's build for this PC (OVRPort's conversion without the Steam Frame fixes, signed with the game's key)
and its OBB data, and records <data>/pc/<pkg>/deployment.json with kind "android". Play goes through a Steam shortcut
that runs FramePort's launcher script, which hands the game to AXRB's own run script (emulator, SteamVR identity,
host bridge). See docs/PC_ANDROID.md.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from ..core import winhost
from ..core.events import Reporter
from ..core.models import Recipe, data_manifest
from ..tools import axrb

KIND = "android"
OBB_ROOT = "/sdcard/Android/obb"


def powershell_win() -> str:
    return axrb.powershell_win()


def shortcut_fields(dep: dict) -> tuple[str, str, str]:
    """(Exe, StartDir, LaunchOptions) of the Steam shortcut: powershell.exe running FramePort's AXRB launcher."""
    launcher = dep["launcher_win"]
    start = '"' + launcher.rsplit("\\", 1)[0] + '\\"'
    return f'"{dep["powershell_win"]}"', start, axrb.launcher_args(launcher, dep["package"], dep["activity"],
                                                                     dep["title"])


def _adb_ok(r: subprocess.CompletedProcess, what: str) -> str:
    text = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "Failure" in text or "Error" in text and "Success" not in text:
        if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in text:
            raise RuntimeError(f"{what}: a copy of this game signed with another key is installed in AXRB. "
                               "Uninstall it there first (that deletes its Android saves), then install again.")
        raise RuntimeError(f"{what}: {text[-400:]}")
    return text


def resolve_activity(package: str) -> str:
    r = axrb.adb(["shell", "cmd", "package", "resolve-activity", "--brief", package], timeout=60)
    last = (r.stdout.strip().splitlines() or [""])[-1].strip()
    if not re.fullmatch(r"[\w.]+/[\w.$]+", last):
        raise RuntimeError(f"{package} has no launcher activity in Android (is it an OVRPort build?)")
    return last


def remote_sizes(package: str) -> dict[str, int]:
    """Sizes of the files already in the game's OBB folder in AXRB's Android (a re-install sends only the rest)."""
    r = axrb.adb(["shell", f"cd {OBB_ROOT}/{package} 2>/dev/null && find . -type f -exec stat -c '%s %n' {{}} +"],
                 timeout=120)
    out = {}
    for line in r.stdout.splitlines():
        size, _, name = line.strip().partition(" ")
        if size.isdigit() and name.startswith("./"):
            out[name[2:]] = int(size)
    return out


def push_data(package: str, data_dir: Path, files: list[str] | None, reporter: Reporter) -> int:
    manifest = data_manifest(data_dir, files)
    if not manifest:
        return 0
    have = remote_sizes(package)
    todo = {rel: size for rel, size in manifest.items() if have.get(rel) != size}
    total, done = sum(todo.values()) or 1, 0
    reporter.log(f"game data: {len(todo)} of {len(manifest)} files to copy ({total / 2**30:.1f} GiB)")
    for rel, size in todo.items():
        reporter.check_cancel()
        reporter.progress(done / total, f"copying game data: {rel}")
        r = axrb.adb(["push", winhost.to_windows(data_dir / rel), f"{OBB_ROOT}/{package}/{rel}"],
                     timeout=600 + size / 4e6)
        _adb_ok(r, f"copying {rel}")
        done += size
    reporter.progress(1.0, "game data copied")
    return len(todo)


def ensure_axrb(reporter: Reporter) -> Path:
    """AXRB and its Android runtime, installed/set up on first use. Returns the AXRB app folder."""
    app = axrb.app_dir()
    if not app:
        reporter.stage("Download AXRB (Android XR Bridge)")
        app = axrb.install(lambda f: reporter.progress(f, "downloading AXRB"), reporter.check_cancel)
    reporter.check("AXRB", True, f"{axrb.installed_version(app) or '?'} at {winhost.to_windows(app)}")
    info = axrb.requirements(app)
    problems = axrb.requirement_problems(info)
    if problems:
        raise RuntimeError("This PC can't run Quest games through AXRB: " + "; ".join(problems))
    reporter.check("PC requirements (AXRB)", True, f"{info.get('gpu')}, {info.get('memoryGB')} GB")
    state = axrb.setup_state(app=app)
    if state["components"] or not state["avd"]:
        reporter.stage(f"Set up AXRB's Android ({state['download_bytes'] / 2**30:.1f} GiB download)")
        axrb.setup_files(lambda f, name: reporter.progress(f, f"downloading {name}"), reporter.check_cancel, app=app)
    return app


def install(package: str, title: str, apk: Path, data_dir: Path | None, recipe: Recipe, reporter: Reporter,
            apk_only: bool = False, data_files: list[str] | None = None, record_dir: Path | None = None,
            appid=None) -> dict:
    app = ensure_axrb(reporter)
    reporter.stage("Start Android (AXRB; the first start takes several minutes)")
    started = axrb.start_emulator(app=app)
    try:
        axrb.ensure_runtime_apk(app=app)
        reporter.stage("Install the game into AXRB's Android")
        _adb_ok(axrb.adb(["install", "--no-incremental", "-r", winhost.to_windows(apk)], timeout=900),
                "installing the game")
        activity = resolve_activity(package)
        sent = 0 if apk_only or not data_dir else push_data(package, Path(data_dir), data_files, reporter)
        axrb.adb(["shell", "sync"], timeout=120)
    finally:
        if started:
            reporter.stage("Stop Android")
            axrb.stop_emulator()
    launcher = axrb.write_launcher(app=app)
    from ..build import sha256
    from ..patches.base import pc_selection

    dep = {"package": package, "kind": KIND, "title": title, "activity": activity, "apk": str(apk),
           "sha256": sha256(apk), "launcher_win": winhost.to_windows(launcher), "powershell_win": powershell_win(),
           "axrb_version": axrb.installed_version(app),
           "recipe": {"patches": sorted(pc_selection(recipe.patches)), "source": recipe.source}, "time": time.time()}
    dep["appid"] = appid(shortcut_fields(dep)[0], title) if appid else None
    if record_dir:
        record_dir.mkdir(parents=True, exist_ok=True)
        (record_dir / "deployment.json").write_text(json.dumps(dep, indent=2), encoding="utf-8")
    reporter.log(f"installed in AXRB ({activity}); {sent} data files copied")
    return {"ok": True, "appid": dep["appid"], "activity": activity}


def game_logs() -> dict[str, str]:
    """AXRB's session logs (newest session) for triage and diagnostics."""
    root = axrb.runtime_root()
    out = {}
    if not root:
        return out
    logs = axrb.log_dir(root)
    for name, sub, limit in (("session.json", "game/session.json", 1 << 16), ("host.err", "game/host.err", 1 << 20),
                             ("host.log", "game/host.log", 1 << 20), ("guest.log", "game/guest.log", 2 << 20),
                             ("emulator.stderr.log", "emulator/emulator.stderr.log", 1 << 18),
                             ("frameport-emulator-start.log", "frameport-emulator-start.log", 1 << 18)):
        p = logs / sub
        if p.is_file():
            out[name] = axrb.read_log(p, limit)
    return out


def launch_test(dep: dict, reporter: Reporter, seconds: int = 45, triage=None):
    """Start the game through FramePort's launcher (= AXRB's run script), watch its Android process, stop it again."""
    reporter.stage("Launch test (this PC, AXRB)")
    package = dep["package"]
    if axrb.emulator_online() and _pid(package):
        raise RuntimeError(f"{dep['title']} is already running")
    log = axrb.log_dir() / "frameport-launch-test.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.unlink(missing_ok=True)
    before = time.time()
    q = axrb._ps_quote
    pid = axrb.spawn_detached(axrb.logged(
        f"& {q(dep['launcher_win'])} -Package {q(package)} -Activity {q(dep['activity'])} "
        f"-GameName {q(dep['title'])} -CaptureGuestLog", winhost.to_windows(log)))
    seen, state, boot_deadline = False, "NEVER_STARTED", time.time() + 900  # the emulator boot comes first
    watch_end = None
    while time.time() < boot_deadline and axrb.pid_running(pid):
        reporter.check_cancel()
        time.sleep(3)
        running = axrb.emulator_online() and bool(_pid(package))
        if running and not seen:
            seen, watch_end = True, time.time() + seconds
        if seen and not running:
            state = "EXITED"
            break
        if watch_end and time.time() >= watch_end:
            state = "RUNNING"
            break
    if seen and state == "NEVER_STARTED":
        state = "EXITED"
    if axrb.pid_running(pid):
        try:  # AXRB's script sees the game stop, cleans up (and shuts Android down if it started it)
            axrb.adb(["shell", "am", "force-stop", package], timeout=30)
        except (axrb.AxrbError, OSError, subprocess.TimeoutExpired):
            pass
        end = time.time() + 300
        while time.time() < end and axrb.pid_running(pid):
            time.sleep(3)
    logs = game_logs()
    text = "\n".join(f"===== {name}\n{body}" for name, body in logs.items()
                     if name != "frameport-emulator-start.log")
    text += "\n===== launcher\n" + axrb.read_log(log, 1 << 16)
    result = triage(text, state, package, kind="axrb") if triage else None
    reporter.check("Game process", state == "RUNNING", f"{state} ({round(time.time() - before)}s incl. Android start)")
    if result:
        for m in result.milestones:
            reporter.check(m, True)
        for f in result.findings:
            reporter.check(f.id, None if f.severity in ("warning", "info") else False,
                           f"{f.diagnosis} [{f.evidence[:160]}]")
    return result, text


def _pid(package: str) -> str:
    try:
        return axrb.adb(["shell", "pidof", package], timeout=20).stdout.strip()
    except (axrb.AxrbError, OSError, subprocess.TimeoutExpired):
        return ""


def uninstall(dep: dict, keep_data: bool = True) -> bool:
    """Remove the game from AXRB's Android (saves kept unless keep_data is False). False if AXRB isn't there."""
    if not axrb.app_dir() or not axrb.adb_exe():
        return False
    started = axrb.start_emulator()
    try:
        args = ["shell", "pm", "uninstall", "-k", dep["package"]] if keep_data else ["uninstall", dep["package"]]
        r = axrb.adb(args, timeout=240)
        return "Success" in r.stdout
    finally:
        if started:
            axrb.stop_emulator()
