"""Classify a launch log: which milestones were reached, which known failure signatures appear, what to try."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from ..core.paths import catalog_dir

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class Finding:
    id: str
    severity: str
    diagnosis: str
    suggest: list[str]
    evidence: str
    use_alt: bool = False
    question: str = ""  # a symptom only the player sees: its fix is offered as "Did … ? Yes = apply", never on its own
    report: bool = False  # nothing to switch on: diagnostics (a problem report) are what helps


@dataclass
class TriageResult:
    state: str  # RUNNING | EXITED | NEVER_STARTED | UNKNOWN
    milestone: str | None
    milestones: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    fps: float | None = None

    @property
    def verdict(self) -> str:
        fatal = [f for f in self.findings if f.severity == "fatal"]
        if self.state == "RUNNING" and not fatal:
            return "pass"
        return "fail" if fatal or self.state in ("EXITED", "NEVER_STARTED") else "unknown"

    def suggestions(self) -> list[str]:
        """Fixes to offer without asking (findings with a symptom question are left to the player)."""
        out = []
        for f in self.findings:
            if f.question:
                continue
            for s in f.suggest:
                if s not in out:
                    out.append(s)
        return out


_db = None


def database() -> dict:
    global _db
    if _db is None:
        _db = yaml.safe_load((catalog_dir() / "triage.yaml").read_text(encoding="utf-8"))
    return _db


LOGCAT_LINE = re.compile(r"\d\d-\d\d \d\d:\d\d:\d\d\.\d+ ")


def game_lines(log: str, package: str | None = None) -> list[str]:
    """Strip colour codes; when possible keep only lines of the game's process (plus Lepton's own lines)."""
    lines = [ANSI.sub("", ln) for ln in log.splitlines()]
    if not package:
        return lines
    pids = set()
    for ln in lines:
        m = re.search(r"Start proc (\d+):" + re.escape(package), ln)
        if m:
            pids.add(m[1])
    if not pids:
        return lines
    out = []
    for ln in lines:
        f = ln.split()
        # logcat threadtime: date time pid tid level tag: msg; everything else is Lepton's own output (e.g. its short
        # "Boot complete!", which the container-not-started signature checks for: it used to be dropped here)
        if not (len(f) > 3 and LOGCAT_LINE.match(ln) and f[2].isdigit()):
            out.append(ln)
        elif f[2] in pids or "lepton" in ln.lower() or "APP_ACTIVITY" in ln or " FramePortVideo" in ln:
            # FramePortVideo: the hardware codec plugin logs from Android's media service, not the game's process
            out.append(ln)
    return out


def split_suggestion(s: str) -> tuple[str, str | None]:
    """A suggestion is a patch id, or a value for an adapter setting ("adapter.focus_hold_ms=2500")."""
    pid, eq, value = s.partition("=")
    return pid.strip(), (value.strip() if eq else None)


def triage(log: str, state: str = "UNKNOWN", package: str | None = None, crash: str = "",
           kernel: str = "") -> TriageResult:
    """`crash` = the container's crash logcat (tombstones come from crash_dump's pid, so it isn't pid-filtered);
    `kernel` = kernel log lines of a play session ("kernel: …", GPU hangs; agent session_log)."""
    db = database()
    kind = "pcvr" if package and package.startswith("rift.") else "quest"  # Proton/Revive logs vs Lepton logcat
    lines = game_lines(log, package) if kind == "quest" else [ANSI.sub("", ln) for ln in log.splitlines()]
    lines += [ANSI.sub("", ln) for ln in crash.splitlines()]
    lines += [ANSI.sub("", ln) for ln in kernel.splitlines()]
    text = "\n".join(lines)
    res = TriageResult(state, None)
    for m in db["milestones"]:
        if m.get("kind", "quest") != kind:
            continue
        if re.search(m["pattern"], text):
            res.milestones.append(m["label"])
            res.milestone = m["label"]
    for sig in db["signatures"]:
        if sig.get("kind", "quest") != kind:
            continue
        hit = re.search(sig["pattern"], text)
        if hit and sig.get("unless") and re.search(sig["unless"], text):
            hit = None  # e.g. a rejected swapchain the adapter retried successfully
        if hit:
            line = next((ln for ln in lines if re.search(sig["pattern"], ln)), hit.group(0))
            res.findings.append(Finding(sig["id"], sig["severity"], sig["diagnosis"], list(sig.get("suggest") or []),
                                        line.strip()[:300], bool(sig.get("use_alt")), sig.get("question") or "",
                                        bool(sig.get("report"))))
    # a root-cause finding hides the generic crash findings it explains
    hidden = {h for sig in db["signatures"] if any(f.id == sig["id"] for f in res.findings)
              for h in sig.get("supersedes") or []}
    res.findings = [f for f in res.findings if f.id not in hidden]
    fps = re.findall(r"pacing: ([0-9.]+) fps", text)
    if fps:
        res.fps = float(fps[-1])
    stopped = frames_stopped(lines)
    if stopped and state == "RUNNING" and not any(f.severity == "fatal" for f in res.findings):
        res.findings.append(Finding(
            "frames-stopped", "fatal",
            f"The game stopped sending frames {stopped:.0f} s before the test ended although its process kept "
            "running (a crashed or stuck render thread: a frozen picture in the headset).", [],
            "last FrameBridge pacing line"))
    return res


MISSING_OBB = ("The game keeps its content in a data file (.obb) and none was installed: FramePort found no .obb "
               "files next to its APK. Unreal games then stop right after start without an error (GitHub #85). Put "
               "the .obb files in a folder named like the package (or obb/) next to the APK, add the folder again "
               "and reinstall.")


def add_missing_obb(res: TriageResult) -> TriageResult:
    """A launch test of a game whose APK expects an OBB that wasn't installed (from the library, not the log: the
    game logs nothing about it). Only when it sent no frames: an OBB from an earlier install may still be there."""
    if res.fps is None and not any(f.id == "missing-obb" for f in res.findings):
        res.findings.insert(0, Finding("missing-obb", "fatal", MISSING_OBB, [],
                                       "analysis: the APK expects an OBB (Unreal bHasOBBFiles); no data folder"))
    return res


LOGCAT_TIME = re.compile(r"^\d\d-\d\d (\d\d):(\d\d):(\d\d)\.(\d{3})")


def frames_stopped(lines: list[str], gap: float = 20.0) -> float | None:
    """Seconds between FrameBridge's last pacing line and the end of the log when that is over `gap` (the frame loop
    died while the process lived on, e.g. The Room VR's render thread crash, PowerWash Simulator's rejected frames)."""
    def secs(line):
        m = LOGCAT_TIME.match(line)
        return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000 if m else None
    last_pacing = last = None
    for ln in lines:  # logcat lines aren't strictly in time order: the latest time counts, not the last line
        t = secs(ln)
        if t is None:
            continue
        if last is not None and t < last - 43200:  # past midnight
            t += 86400
        last = t if last is None else max(last, t)
        if "FrameBridge: pacing:" in ln:
            last_pacing = t
    if last_pacing is None or last is None:
        return None
    delta = last - last_pacing
    return delta if delta > gap else None
