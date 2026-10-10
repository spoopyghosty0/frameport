"""On-device validation: headless launch on the Frame + log triage."""
from __future__ import annotations

from ..core.events import Reporter
from ..frame.connection import Frame
from .triage import TriageResult, triage


def launch_test(frame: Frame, package: str, reporter: Reporter, seconds: int = 45) -> tuple[TriageResult, str]:
    reporter.stage("Launch test (headless)")
    reporter.log(f"starting {package} for {seconds}s; without the headset worn the game can't reach FOCUSED, "
                 "so this checks startup, not the picture")
    res = frame.agent("launch_test", timeout=seconds + 120, package=package, seconds=seconds)
    if res.get("skipped"):  # agent >= 75: SteamVR overlay apps show nothing without a game
        reporter.check("Launch test", None, f"skipped ({res['skipped']})")
        return TriageResult("UNKNOWN", None), ""
    log = frame.get_text(res["log"]) if res.get("log_size") else ""
    crash = frame.get_text(res["crash_log"]) if res.get("crash_log") else ""  # tombstones (backtraces), agent >= 23
    result = triage(log, res["state"], package, crash=crash[-256 * 1024:])
    reporter.check("Process", res["state"] == "RUNNING", f"{res['state']} after {res['elapsed']}s")
    for m in result.milestones:
        reporter.check(m, True)
    if result.fps:
        reporter.check("Frame pacing", result.fps > 30, f"{result.fps:.0f} fps")
    for f in result.findings:
        ok = None if f.severity in ("warning", "info") else False
        reporter.check(f.id, ok, f"{f.diagnosis} [{f.evidence[:160]}]")
    return result, log + ("\n--------- crash logcat\n" + crash if crash else "")
