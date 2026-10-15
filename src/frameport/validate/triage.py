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
    headless_end: str | None = None  # launch test: the session ended for lack of a worn headset (label), see triage()

    @property
    def verdict(self) -> str:
        fatal = [f for f in self.findings if f.severity == "fatal"]
        if not fatal and any(f.id == "frames-stopped" for f in self.findings):
            return "unknown"
        if (self.state == "RUNNING" or self.headless_end) and not fatal:
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
    # system_server's lines about the game's activities (which one was started / shown), e.g. Unreal's
    # DownloaderActivity (unreal-obb-check-stuck)
    own_activity = re.compile(r"(Displayed |START u0 .*cmp=)" + re.escape(package) + "/")
    out = []
    for ln in lines:
        f = ln.split()
        # logcat threadtime: date time pid tid level tag: msg; everything else is Lepton's own output (e.g. its short
        # "Boot complete!", which the container-not-started signature checks for: it used to be dropped here)
        if not (len(f) > 3 and LOGCAT_LINE.match(ln) and f[2].isdigit()):
            out.append(ln)
        elif (f[2] in pids or "lepton" in ln.lower() or "APP_ACTIVITY" in ln or " FramePortVideo" in ln
              or own_activity.search(ln)):
            # FramePortVideo: the hardware codec plugin logs from Android's media service, not the game's process
            out.append(ln)
    return out


# suggestions that only work with one graphics API: the Vulkan shim's settings for Vulkan games, the shader-fix layer
# under Zink for OpenGL ES games
API_ONLY = {"adapter.vk_shader_dump": "vulkan", "adapter.vk_shader_fix": "vulkan",
            "adapter.zink_shader_dump": "gles", "adapter.zink_shader_fix": "gles"}
SWAPCHAIN_FORMAT = re.compile(r"xrCreateSwapchain \d+x\d+ format=(\d+) .*result=0\b")


def graphics_api(text: str) -> str | None:
    """"vulkan" or "gles" from the swapchain formats FrameBridge logged: OpenXR passes the API's own format numbers,
    Vulkan's VkFormat values are small (e.g. 43 = sRGB), GL's internal formats are 0x8000+ (35907 = GL_SRGB8_ALPHA8).
    None when the log has no (or both kinds of) swapchains."""
    kinds = {"gles" if int(m) >= 0x1000 else "vulkan" for m in SWAPCHAIN_FORMAT.findall(text)}
    return kinds.pop() if len(kinds) == 1 else None


def split_suggestion(s: str) -> tuple[str, str | None]:
    """A suggestion is a patch id, or a value for an adapter setting ("adapter.focus_hold_ms=2500")."""
    pid, eq, value = s.partition("=")
    return pid.strip(), (value.strip() if eq else None)


def triage(log: str, state: str = "UNKNOWN", package: str | None = None, crash: str = "",
           kernel: str = "", headless: bool = False, kind: str | None = None) -> TriageResult:
    """`crash` = the container's crash logcat (tombstones come from crash_dump's pid, so it isn't pid-filtered);
    `kernel` = kernel log lines of a play session ("kernel: …", GPU hangs; agent session_log); `headless` = a launch
    test (no headset worn): what happens after a `headless_end` line doesn't count against the game. `kind`: which
    signatures apply ("quest" Lepton logcat, "pcvr" Proton/Revive, "axrb" a Quest game on this PC); default from
    the package."""
    db = database()
    kind = kind or ("pcvr" if package and package.startswith("rift.") else "quest")
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
    api = graphics_api(text)
    if api:  # a setting for the other graphics API can't help (e.g. the GPU-hang shader dumps)
        for f in res.findings:
            f.suggest = [s for s in f.suggest if API_ONLY.get(split_suggestion(s)[0], api) == api]
    # a root-cause finding hides the generic crash findings it explains
    hidden = {h for sig in db["signatures"] if any(f.id == sig["id"] for f in res.findings)
              for h in sig.get("supersedes") or []}
    res.findings = [f for f in res.findings if f.id not in hidden]
    if headless:
        _forgive_headless_end(res, lines, db.get("headless_end") or [])
    fps = re.findall(r"pacing: ([0-9.]+) fps", text)
    if fps:
        res.fps = float(fps[-1])
    stopped = frames_stopped(lines)
    has_pacing = any(re.search(r"FrameBridge\s*: pacing:", ln) for ln in lines)
    if (stopped and has_pacing and state == "RUNNING" and not res.headless_end
            and not any(f.severity == "fatal" for f in res.findings)):
        res.findings.append(Finding(
            "frames-stopped", "fatal",
            f"The game stopped sending frames {stopped:.0f} s before the test ended although its process kept "
            "running (a crashed or stuck render thread: a frozen picture in the headset).", [],
            "last FrameBridge pacing line"))
    elif (stopped and not has_pacing and state in ("RUNNING", "UNKNOWN") and not res.headless_end
          and not any(f.severity == "fatal" for f in res.findings)
          and not any(re.search(m["pattern"], text) for m in db.get("headless_end") or [])):
        res.findings.append(Finding(
            "frames-stopped", "info",
            "The game submitted projection frames but produced no FrameBridge pacing summary for at least "
            f"{stopped:.0f} s while its process appeared alive. It may have stopped drawing; collect diagnostics "
            "while the picture is stuck to investigate.", [],
            "FrameBridge projection frames without a pacing summary", report=True))
    return res


def _forgive_headless_end(res: TriageResult, lines: list[str], markers: list[dict]) -> None:
    """A launch test whose game sent paced frames until a `headless_end` line (the VR session ended because nobody
    wears the headset, e.g. the VrApi bridge's tracking deadline): an exit or crash after it is an artifact of the
    test (BlazeRush: zink DEVICE LOST + SIGSEGV in the GPU driver thread as the renderer tore down, GitHub #57).
    Findings first seen after that line become one info finding; the verdict then passes (TriageResult.verdict)."""
    hits = ((m, next((i for i, ln in enumerate(lines) if re.search(m["pattern"], ln)), None)) for m in markers)
    marker, at = next(((m, i) for m, i in hits if i is not None), (None, None))
    if at is None or not any("FrameBridge: pacing:" in ln for ln in lines[:at]):
        return  # no such line, or no frames before it: the game never really ran
    when = _secs(lines[at])

    def after(f: Finding) -> bool:
        i = next((i for i, ln in enumerate(lines) if f.evidence and f.evidence in ln.strip()), None)
        if i is None:
            return False
        t = _secs(lines[i])
        return t >= when if t is not None and when is not None else i > at

    late = [f for f in res.findings if after(f)]
    res.findings = [f for f in res.findings if f not in late]
    res.headless_end = marker["label"]
    what = ", ".join(f.id for f in late) or "the game ended"
    res.findings.append(Finding(
        "headless-end", "info",
        f"The game ran (paced frames) until the VR session ended for lack of a worn headset ({marker['label']}). What "
        f"came after ({what}) is a side effect of this headless test and doesn't happen in the headset.", [],
        lines[at].strip()[:300]))


def _secs(line: str) -> float | None:
    m = LOGCAT_TIME.match(line)
    return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000 if m else None


MISSING_OBB = ("The game keeps its content in a data file (.obb) and none was installed: FramePort found no .obb "
               "files next to its APK. Games can stop silently (Unreal) or crash during engine initialization "
               "(CryEngine, for example The Climb). Put "
               "the .obb files in a folder named like the package (or obb/) next to the APK, add the folder again "
               "and reinstall.")


def add_missing_obb(res: TriageResult) -> TriageResult:
    """A launch test of a game whose APK expects an OBB that wasn't installed (from the library, not the log: the
    game logs nothing about it). Only when it sent no frames: an OBB from an earlier install may still be there."""
    if res.fps is None and not any(f.id == "missing-obb" for f in res.findings):
        res.findings.insert(0, Finding("missing-obb", "fatal", MISSING_OBB, [],
                                       "analysis: the APK expects an OBB; no data folder"))
    return res


LOGCAT_TIME = re.compile(r"^\d\d-\d\d (\d\d):(\d\d):(\d\d)\.(\d{3})")


def frames_stopped(lines: list[str], gap: float = 20.0) -> float | None:
    """Seconds since the last pacing summary (or initial projection submission if none), when over `gap`.
    Twenty seconds allows several of FrameBridge's 5 s pacing windows before suspecting a stalled frame loop."""
    def secs(line):
        m = LOGCAT_TIME.match(line)
        return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000 if m else None
    last_projection = last_pacing = last = None
    for ln in lines:  # logcat lines aren't strictly in time order: the latest time counts, not the last line
        t = secs(ln)
        if t is None:
            continue
        if last is not None and t < last - 43200:  # past midnight
            t += 86400
        last = t if last is None else max(last, t)
        if re.search(r"FrameBridge\s*: pacing:", ln):
            last_pacing = t if last_pacing is None else max(last_pacing, t)
        if re.search(r"FrameBridge\s*: (?:new layer: type=35\b|projection view \d+:)", ln):
            last_projection = t if last_projection is None else max(last_projection, t)
    if last_pacing is None:
        last_pacing = last_projection
    if last_pacing is None or last is None:
        return None
    delta = last - last_pacing
    return delta if delta > gap else None
