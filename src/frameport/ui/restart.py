"""Did the Frame crash because of a game? (no Flet) From the agent's boot state: a new boot whose predecessor didn't
shut down cleanly, with a FramePort game started shortly before it. A boot the Frame's power chip started after a
sudden power loss (agent `prev_reason` "power_loss": PON trigger SMPL/UVLO, GitHub #598) wasn't caused by a game."""
from __future__ import annotations

SOON = 3 * 3600  # a game started up to 3 h before the restart may have caused it


def _game_before(boot: dict, previous_boot_id: str | None) -> dict | None:
    """The FramePort game started shortly before a new, uncleanly ended boot, or None."""
    if not previous_boot_id or boot.get("boot_id") == previous_boot_id or boot.get("prev_clean") is not False:
        return None
    last, boot_time = boot.get("last_launch"), boot.get("boot_time")
    if not last or not boot_time or not 0 <= boot_time - last.get("time", 0) <= SOON:
        return None
    return last


def crashed_game(boot: dict, previous_boot_id: str | None) -> dict | None:
    """The game to blame ({"package", "title", "time"}) or None. Only for a boot FramePort hasn't seen before whose
    previous boot ended without a clean shutdown (a battery shutdown or a normal power-off is clean), and not when the
    Frame lost power (power_loss_game)."""
    if boot.get("prev_reason") == "power_loss":
        return None
    return _game_before(boot, previous_boot_id)


def power_loss_game(boot: dict, previous_boot_id: str | None) -> dict | None:
    """The game that was running when the Frame lost power (the restart crashed_game would otherwise blame on it),
    or None."""
    if boot.get("prev_reason") != "power_loss":
        return None
    return _game_before(boot, previous_boot_id)
