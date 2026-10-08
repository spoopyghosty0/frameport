"""Install progress as a transit (no Flet): a job's stage names → one of six steps and an overall fraction, so the GUI
can show the game crossing the portal from the PC to the Frame (components.Transit).

The stage names come from the install job (app._submit_install), build.py, pipeline.py and the installers; they are
intentionally not translated so they can be mapped here."""
from __future__ import annotations

from dataclasses import dataclass, replace

# step keys (stable) and their labels (translated when shown)
STEPS = ("analyze", "patch", "sign", "upload", "install", "test")
LABELS = {"analyze": "Analyze", "patch": "Patch", "sign": "Sign", "upload": "Upload", "install": "Install",
          "test": "Test"}

# a run starts over with these (a job that lost the Frame runs again from the beginning)
_START = ("patching the game", "checking the game")
# lower-case stage prefix → step index; the first match wins
_PREFIXES = (
    ("patching", 0), ("checking", 0), ("check game files", 0),
    ("ovrport", 1), ("frame fixes", 1), ("revive", 1),
    ("sign", 2), ("validate", 2),
    ("prepare", 3), ("upload", 3),
    ("artwork", 4), ("finalize", 4), ("add to steam", 4),
    ("launch test", 5), ("starting steamvr", 5),
)
_DONE = ("installed",)
WAITING = "Waiting for the Frame"

# the track: the portal stands still at the PC/Frame boundary. Analyze/Patch/Sign happen on the PC (the cover
# approaches the portal), Upload is the crossing (the cover passes under the portal), Install/Test on the Frame (or
# this PC's Steam) carry it on to the destination. Positions are fractions of the track.
PORTAL_AT = 0.5
CROSSING = 0.06  # half the crossing's share of the track, either side of the portal
_LOCAL, _UPLOAD = 3, 4  # steps before the upload; the upload's own end
# the steps row's column weights, so the Upload column sits under the portal
STEP_WEIGHTS = (139, 139, 139, 166, 208, 208)


@dataclass(frozen=True)
class TransitState:
    step: int            # index into STEPS (len(STEPS) = everything done)
    fraction: float      # overall progress 0..1
    label: str           # the step's key ("upload" …), "done" when finished
    detail: str          # what happens right now: the stage, or the upload's % and speed
    waiting: bool        # the queue waits for the Frame
    failed: bool
    target: str          # "frame" | "pc"


def step_of(stage: str) -> int | None:
    """The step a stage name belongs to (None: unknown, keeps the previous step)."""
    low = (stage or "").strip().lower()
    if low.startswith(_DONE):
        return len(STEPS)
    for prefix, index in _PREFIXES:
        if low.startswith(prefix):
            return index
    return None


def _is_waiting(stage: str) -> bool:
    from ..i18n import tr

    return bool(stage) and stage in (WAITING, tr(WAITING))


def transit_state(state: str = "running", stage: str = "", stages: list[str] | None = None,
                  fraction: float | None = None, speed: str = "", message: str = "", kind: str = "install",
                  to: str = "frame") -> TransitState:
    """Where a job is on its way: the furthest step of the current run (a second build variant's OVRPort after the
    first one's validate doesn't move it back), the upload's own fraction inside the Upload step, and 1.0 when the
    job is done (also when the launch test is off and it ends at "Installed")."""
    stages = list(stages or [])
    waiting = _is_waiting(stage)
    step = 0
    for s in stages:
        if s.strip().lower().startswith(_START):
            step = 0  # a new run
        index = step_of(s)
        if index is not None:
            step = max(step, index)
    if kind == "test" and not any(step_of(s) is not None for s in stages):
        step = 5  # a launch test on its own: everything before it is done
    current = None if waiting else step_of(stage)
    within = 0.0
    if fraction is not None and current == step and step < len(STEPS):
        within = min(1.0, max(0.0, float(fraction)))
    done = state == "done" or step >= len(STEPS)
    if done:
        step, within = len(STEPS), 0.0
    overall = 1.0 if done else (step + within) / len(STEPS)
    if waiting:
        detail = WAITING
    elif current == 3 and fraction is not None:
        detail = " · ".join(x for x in (f"{fraction:.0%}", speed) if x)
    else:
        detail = stage or message
    return TransitState(step=step, fraction=overall, label="done" if done else STEPS[step], detail=detail,
                        waiting=waiting, failed=state == "failed", target="pc" if to == "pc" else "frame")


def position(fraction: float) -> float:
    """Where the cover rides on the track (0..1) for an overall fraction (TransitState.fraction): the local steps
    bring it from the start to just before the portal (PORTAL_AT − CROSSING), the upload carries it across, Install
    and Test from just after the portal to the end; done = 1. Monotonic in `fraction`."""
    x = min(1.0, max(0.0, float(fraction))) * len(STEPS)  # steps done, with the current one's share
    before, after = PORTAL_AT - CROSSING, PORTAL_AT + CROSSING
    if x <= _LOCAL:
        return x / _LOCAL * before
    if x <= _UPLOAD:
        return before + (x - _LOCAL) * (after - before)
    return after + (x - _UPLOAD) / (len(STEPS) - _UPLOAD) * (1.0 - after)


def job_state(job) -> TransitState:
    """transit_state() for a ui.jobs.Job."""
    return transit_state(job.state, job.stage, job.stages, job.fraction, job.speed, job.message, job.kind,
                         getattr(job, "to", "frame"))


class Monotonic:
    """Keeps one place's bar from moving back within a run: the installer reports the upload's fraction over all its
    files (APK, then data), but a new stage clears the job's fraction until the first tick ("Upload data" started at
    the Upload step's 0 % after the APK had filled it). The fraction never decreases for the same job unless the step
    goes back (the job started over, e.g. after the Frame was lost)."""

    def __init__(self):
        self.key = None
        self.step = -1
        self.best = 0.0

    def follow(self, key, state: TransitState) -> TransitState:
        if key != self.key or state.step < self.step:
            self.key, self.best = key, 0.0
        self.step = state.step
        self.best = max(self.best, state.fraction)
        return state if state.fraction == self.best else replace(state, fraction=self.best)
