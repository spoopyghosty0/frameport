"""The sidebar's live Frame card (no Flet): what one monitor sample shows there — the running game (title, how long it
has been playing, its frame rate and that rate's level) and the battery ring. The app subscribes the card to the
shared MonitorHub (frame/monitor_hub.py) with only the "games" and "battery" modules, every CARD_INTERVAL seconds,
while the setting `ui.live_frame_card` is on."""
from __future__ import annotations

from ..frame import monitor as M
from ..i18n import tr
from . import battery as B

SETTING = "ui.live_frame_card"
CARD_INTERVAL = 5.0          # seconds between samples (the agent collects only games + battery for it)
CARD_MODULES = ("games", "battery")
FPS_WINDOW = 120.0           # the sparkline covers the last 2 minutes
FPS_BUCKET = 5.0             # one point per CARD_INTERVAL (faster samples, e.g. with the Monitor open, are averaged)
FPS_POINTS = int(FPS_WINDOW // FPS_BUCKET)


def fps_history() -> M.History:
    return M.History(size=FPS_POINTS, bucket=FPS_BUCKET)


def now_playing(sample: dict | None, fps_series: list | None = None) -> dict | None:
    """Display values for the card's "now playing" row, or None when no game runs: package, title, line ("playing ·
    27 min"), fps text ("72" / "–"), level (ok | warn | error by the fps against its presumed refresh rate) and the
    fps target (refresh rate) for the sparkline's guide."""
    games = (sample or {}).get("games") or []
    if not games:
        return None
    g = games[0]
    pkg = g.get("package") or ""
    elapsed = g.get("elapsed")
    line = tr("playing · {t}").format(t=M.fmt_duration(elapsed)) if elapsed is not None else tr("playing")
    fps = g.get("fps")
    series = list(fps_series or [])
    target = M.fps_target(series + ([fps] if fps is not None else []))
    lvl = M.level("fps_ratio", fps / target if fps is not None and target else None)
    return {"package": pkg, "title": g.get("title") or pkg, "line": line, "minutes": int(elapsed // 60)
            if elapsed is not None else None, "fps": fps, "fps_text": f"{fps:.0f}" if fps is not None else "–",
            "level": lvl if fps is not None else "none", "target": target}


def battery_ring(bat: dict | None) -> dict | None:
    """The battery ring: value (0..1), the "76" text and its level (warn when low on battery power, charging when it
    gains charge, ok otherwise). None without a reading."""
    if not bat or bat.get("percent") is None:
        return None
    pct = max(0, min(100, int(bat.get("percent") or 0)))
    level = "warn" if B.low(bat) else "charging" if B.charging(bat) else "ok"
    return {"value": pct / 100.0, "text": f"{pct}", "level": level}


def uploading(job) -> bool:
    """A job is sending files to the Frame (installer/files stages "Upload …"): the card pauses its samples meanwhile
    (the link is busy)."""
    return bool(job) and (getattr(job, "stage", "") or "").startswith("Upload")
