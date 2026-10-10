"""Triage of a real play session (agent session_log, v70): the launch-test signatures (catalog/triage.yaml) plus what
only shows while someone plays - the frame rate over the whole session (FrameBridge's 5 s `pacing:` lines) and the
runtime's focus dips (FrameBridge's always-on `focus: back after N ms` lines). No Flet, no I/O: pipeline.triage_session
fetches the log and stores the result as the game's `last_session`."""
from __future__ import annotations

import math
import re
import statistics

from .triage import Finding, game_lines, triage

PACING = re.compile(r"FrameBridge\s*: pacing: ([0-9.]+) fps, displayTime vs predicted: avg ([0-9.]+) ms, "
                    r"max ([0-9.]+) ms")
FOCUS_BACK = re.compile(r"FrameBridge\s*: focus: back after ([0-9]+) ms")
REFRESH_RATES = (72, 80, 90, 96, 108, 120, 144)  # the Frame's display modes
SKIP_WINDOWS = 2  # the first pacing windows (loading) don't count
MIN_WINDOWS = 12  # ~1 min of frames before the frame rate is judged
SLOW = 0.9  # a 5 s window below this share of the refresh rate is "slow"
SLOW_SHARE = 0.3  # ... and more than this share of the session's windows were slow: suggest a lighter picture
SCALE_STEP = 0.85  # one step down in resolution scale
MIN_SCALE = 0.5
FOCUS_HOLD_MAX_MS = 5000  # FrameBridge's limit for focus_hold_ms (and its default)
MIN_DIPS = 3


def pacing_stats(lines: list[str]) -> dict | None:
    """{median, target, slow_share, worst_ms, windows} from FrameBridge's pacing lines, or None without any."""
    windows = [(float(m[1]), float(m[3])) for ln in lines if (m := PACING.search(ln))]
    if not windows:
        return None
    counted = windows[SKIP_WINDOWS:] if len(windows) > SKIP_WINDOWS + 3 else windows
    fps = [w[0] for w in counted]
    p90 = sorted(fps)[min(len(fps) - 1, int(len(fps) * 0.9))]
    target = next((r for r in REFRESH_RATES if r >= p90 * 0.97), REFRESH_RATES[-1])
    slow = sum(1 for f in fps if f < SLOW * target)
    return {"median": round(statistics.median(fps), 1), "target": target, "slow_share": round(slow / len(fps), 2),
            "worst_ms": round(max(w[1] for w in counted), 1), "windows": len(windows)}


def focus_dips(lines: list[str]) -> list[int]:
    """Length (ms) of every time the runtime took focus away and gave it back."""
    return [int(m[1]) for ln in lines if (m := FOCUS_BACK.search(ln))]


def _value(patches: dict, key: str, default):
    v = (patches.get(f"adapter.{key}") or {}).get("value", default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt(v: float) -> str:
    return f"{v:g}"


def performance_finding(stats: dict | None, entry: dict) -> Finding | None:
    """The game ran below the display's refresh rate for a good part of the session: suggest a lower resolution
    scale, then the engine's usual extra cost (Unity's runtime MSAA, Unreal 5's space warp)."""
    if not stats or stats["windows"] < MIN_WINDOWS or stats["slow_share"] <= SLOW_SHARE:
        return None
    patches = (entry.get("recipe") or {}).get("patches") or {}
    analysis = entry.get("analysis") or {}
    libs = set(analysis.get("libs") or [])
    suggest = []
    scale = _value(patches, "scale", 1.0)
    lower = max(MIN_SCALE, math.floor(scale * SCALE_STEP * 20) / 20)  # in steps of 0.05
    if lower < scale:
        suggest.append(f"adapter.scale={_fmt(lower)}")
    if analysis.get("engine") == "Unity":
        suggest.append("frame.unity_runtime_msaa_off")
    if "libUnreal.so" in libs and "libOVRPlugin.so" in libs:
        suggest.append("adapter.hide_space_warp")
    return Finding(
        "slow-frames", "warning",
        f"The game ran below the display's {stats['target']} Hz for {stats['slow_share']:.0%} of the session "
        f"(median {stats['median']:g} fps): the runtime fills the gaps (judder, smearing on head turns). A lower "
        "resolution scale lightens each frame; the game's own heavy features can be turned off too.",
        suggest,
        f"FrameBridge pacing: median {stats['median']:g} fps, worst displayTime offset {stats['worst_ms']:g} ms")


def focus_finding(dips: list[int], entry: dict) -> Finding | None:
    """Several focus dips reached the game (it pauses or recentres on each): hide them (focus_hold) or longer ones
    (focus_hold_ms), up to FrameBridge's limit. Longer dips are taking the headset off or the system menu."""
    patches = (entry.get("recipe") or {}).get("patches") or {}
    hold = _value(patches, "focus_hold", 1) != 0
    hold_ms = _value(patches, "focus_hold_ms", FOCUS_HOLD_MAX_MS)
    reached = [d for d in dips if (d > hold_ms if hold else True) and d <= FOCUS_HOLD_MAX_MS]
    if len(reached) < MIN_DIPS:
        return None
    if not hold:
        suggest = ["adapter.focus_hold=1"]
    else:
        want = min(FOCUS_HOLD_MAX_MS, int(math.ceil((max(reached) + 250) / 500.0)) * 500)
        if want <= hold_ms:
            return None
        suggest = [f"adapter.focus_hold_ms={want}"]
    return Finding(
        "focus-dips", "warning",
        f"The Frame took focus away from the game {len(reached)} times for a moment "
        f"(up to {max(reached) / 1000:.1f} s; "
        "usually its wear sensor briefly thinking the headset is off), and the game saw each one: games pause or "
        "recentre when that happens. FrameBridge can hide such short dips from the game.",
        suggest, f"{len(dips)} focus dips, {len(reached)} reached the game")


def analyze(log: str, package: str, entry: dict, crash: str = "", kernel: str = "") -> dict:
    """{fps, focus_dips, findings: [finding dicts]} for one play session. The findings' suggestions aren't filtered
    against the recipe yet (pipeline.useful_suggestions)."""
    res = triage(log, "UNKNOWN", package, crash=crash, kernel=kernel)
    lines = game_lines(log, package)
    stats = pacing_stats(lines)
    dips = focus_dips(lines)
    findings = list(res.findings)
    for f in (performance_finding(stats, entry), focus_finding(dips, entry)):
        if f:
            findings.append(f)
    out = []
    for f in findings:
        d = dict(f.__dict__)
        d["suggest"] = applicable(entry, d["suggest"])
        out.append(d)
    return {"fps": stats, "focus_dips": len(dips), "milestone": res.milestone, "findings": out}


def applicable(entry: dict, suggestions: list[str]) -> list[str]:
    """Suggestions whose patch can matter for this game (Patch.applies on its analysis), as the game page lists."""
    from ..core import library
    from ..patches.base import REGISTRY, load_all
    from .triage import split_suggestion

    if not entry.get("analysis"):
        return list(suggestions)
    load_all()
    try:
        a = library.analysis_from_dict(entry["analysis"])
    except Exception:  # noqa: BLE001 (an old or partial entry: keep them all)
        return list(suggestions)
    out = []
    for s in suggestions:
        p = REGISTRY.get(split_suggestion(s)[0])
        try:
            ok = p is None or p.applies(a)
        except Exception:  # noqa: BLE001
            ok = True
        if ok:
            out.append(s)
    return out


def shown_findings(summary: dict) -> list[dict]:
    """The findings the game page shows: something to do (a fix, a question, a report) or a crash."""
    return [f for f in summary.get("findings") or []
            if f.get("suggest") or f.get("report") or f.get("severity") in ("fatal", "error")]
