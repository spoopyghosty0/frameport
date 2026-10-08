from frameport.ui import battery as B


def bat(pct, plugged=False):
    return {"percent": pct, "status": "Charging" if plugged else "Discharging", "plugged": plugged}


def test_warn_once_then_pause_then_resume_when_plugged_in():
    assert B.advice(bat(80), True, None, False) is None
    assert B.advice(bat(30), True, None, False) == "warn"
    assert B.advice(bat(28), True, None, True) is None  # warned already
    assert B.advice(bat(15), True, None, True) == "pause"
    assert B.advice(bat(14), True, "battery", True) is None  # still low and unplugged: stay paused
    assert B.advice(bat(14, plugged=True), True, "battery", True) == "resume"
    assert B.advice(bat(25), True, "battery", True) == "resume"


def test_no_action_without_frame_work_or_while_charging_or_paused_for_another_reason():
    assert B.advice(bat(5), False, None, False) is None
    assert B.advice(bat(5, plugged=True), True, None, False) is None
    assert B.advice(bat(5), True, "frame", False) is None  # waiting for the Frame to come back: not ours
    assert B.advice(bat(5), False, "battery", True) == "resume"  # queue emptied (cancelled): don't stay paused
    assert B.advice(None, True, "battery", True) == "resume"  # no reading any more (old agent): don't block forever
    assert B.advice(None, True, None, False) is None


def test_plugged_in_but_draining_counts_as_not_charging():
    weak = {"percent": 12, "status": "Discharging", "plugged": True, "draining": True}
    assert B.advice(weak, True, None, True) == "pause" and B.low(weak) and B.label(weak) == "12%"


def test_label():
    assert B.label(bat(76)) == "76%" and B.label(bat(76, True)) == "76% ⚡" and B.label(None) == ""
    assert B.low(bat(20)) and not B.low(bat(20, True)) and not B.low(bat(60))


def test_icon_follows_level_and_charging():
    assert B.icon(bat(7, True)) == "BATTERY_CHARGING_FULL_ROUNDED"
    assert B.icon(bat(7)) == "BATTERY_ALERT_ROUNDED" and B.icon(bat(100)) == "BATTERY_FULL_ROUNDED"
    assert B.icon(bat(50)) == "BATTERY_3_BAR_ROUNDED" and B.icon(bat(90)) == "BATTERY_6_BAR_ROUNDED"
