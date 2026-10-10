"""The Frame's battery during installs (no Flet): when to warn, pause the queue and continue. A long queue on battery
power can drain the Frame until it shuts down mid-upload (owner, 2026-10-03: the charging cable came out at night)."""
from __future__ import annotations

from ..i18n import tr

WARN_AT = 30     # % on battery power: warn once while Frame work is queued
PAUSE_AT = 15    # % on battery power: pause the queue before the Frame shuts itself down
RESUME_AT = 25   # % on battery power at which a paused queue continues by itself (or as soon as it's plugged in)


def charging(battery: dict) -> bool:
    """Gaining charge: a charger is connected and the battery isn't draining anyway (a weak charger, e.g. a PC port,
    can supply less than the Frame uses while it installs)."""
    return bool(battery.get("plugged")) and not battery.get("draining")


def advice(battery: dict | None, frame_work: bool, paused: str | None, warned: bool) -> str | None:
    """"pause", "resume", "warn" or None. `paused` is the queue's pause reason ("battery" = paused by us)."""
    if not battery:
        return "resume" if paused == "battery" else None
    pct, plugged = battery.get("percent", 100), charging(battery)
    if paused == "battery":
        return "resume" if plugged or pct >= RESUME_AT or not frame_work else None
    if not frame_work or plugged or paused:
        return None
    if pct <= PAUSE_AT:
        return "pause"
    if pct <= WARN_AT and not warned:
        return "warn"
    return None


def label(battery: dict | None) -> str:
    """ "76%" / "76% ⚡" (charging or plugged in); "" without a battery reading."""
    if not battery:
        return ""
    return f"{battery.get('percent', 0)}%" + (" ⚡" if charging(battery) else "")


def icon(battery: dict) -> str:
    """Material battery icon name for the level (bars like a phone's status bar)."""
    if charging(battery):
        return "BATTERY_CHARGING_FULL_ROUNDED"
    pct = battery.get("percent", 0)
    if pct <= PAUSE_AT:
        return "BATTERY_ALERT_ROUNDED"
    return "BATTERY_FULL_ROUNDED" if pct >= 95 else f"BATTERY_{min(6, pct * 7 // 100)}_BAR_ROUNDED"


def low(battery: dict | None) -> bool:
    return bool(battery) and not charging(battery) and battery.get("percent", 100) <= WARN_AT


def message(kind: str, battery: dict) -> str:
    pct = battery.get("percent", 0)
    if kind == "pause":
        return tr("The Frame's battery is at {pct}%, so installs are paused. Plug it in and they "
                  "continue.").format(pct=pct)
    if kind == "warn":
        return tr("The Frame's battery is at {pct}% and not charging. Plug it into a strong charger — "
                  "installs pause at {pause}%.").format(
            pct=pct, pause=PAUSE_AT)
    return tr("The Frame is charging — installs continue.")
