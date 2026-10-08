"""Install transit: job stage names → step + overall progress (ui/transit.py, no Flet)."""
import pytest

from frameport.ui.transit import STEPS, step_of, transit_state

QUEST = ["Patching the game", "OVRPort (primary)", "Frame fixes (primary)", "sign (primary)", "validate (primary)"]
ALT = ["OVRPort (alt)", "Frame fixes (alt)", "sign (alt)", "validate (alt)"]
INSTALL = ["Prepare Frame", "Upload APK", "Upload data (12 files)", "Artwork for the Steam library",
           "Finalize install", "Add to Steam library"]


def run(stages, **kw):
    return transit_state(stage=stages[-1] if stages else "", stages=stages, **kw)


@pytest.mark.parametrize("stage,step", [
    ("Patching the game", 0), ("Checking the game (installing it as it is)", 0), ("Check game files", 0),
    ("OVRPort (primary)", 1), ("Frame fixes (alt)", 1), ("Revive", 1), ("sign (primary)", 2), ("validate (alt)", 2),
    ("Prepare Frame", 3), ("Upload APK", 3), ("Upload data (3 files)", 3), ("Upload game (40 files, 3.1 GiB)", 3),
    ("Artwork for the Steam library", 4), ("Finalize install", 4), ("Add to Steam library", 4),
    ("Launch test (headless)", 5), ("Launch test: passed", 5), ("Installed", len(STEPS)), ("Something new", None)])
def test_step_of(stage, step):
    assert step_of(stage) == step


def test_quest_primary_and_alt_never_go_back():
    s = run(QUEST)
    assert (s.step, s.label) == (2, "sign")
    s = run(QUEST + ALT[:1])  # the alternate build's OVRPort after the primary's validate
    assert s.step == 2
    assert s.fraction == pytest.approx(2 / 6)


def test_upload_fraction_and_detail():
    s = run(QUEST + ALT + INSTALL[:2], fraction=0.5, speed="36.4 MB/s · ~1 min left")
    assert s.step == 3 and s.label == "upload"
    assert s.fraction == pytest.approx(3.5 / 6)
    assert s.detail == "50% · 36.4 MB/s · ~1 min left"
    assert s.target == "frame" and not s.waiting and not s.failed


def test_install_and_test_steps():
    assert run(QUEST + INSTALL).step == 4
    s = run(QUEST + INSTALL + ["Launch test (headless)"])
    assert (s.step, s.label) == (5, "test")
    tested = QUEST + INSTALL + ["Launch test (headless)", "Launch test: passed"]
    s = transit_state("done", "Launch test: passed", tested)
    assert s.fraction == 1.0 and s.label == "done"


def test_launch_test_off_ends_at_installed():
    s = run(QUEST + INSTALL + ["Installed"])
    assert s.fraction == 1.0 and s.label == "done"


def test_rift_and_as_is():
    rift = ["Checking the game", "Check game files", "Revive", "Prepare Frame", "Upload game (40 files, 3.1 GiB)"]
    s = run(rift, fraction=0.25)
    assert s.step == 3 and s.fraction == pytest.approx(3.25 / 6)
    s = run(["Checking the game", "Checking the game (installing it as it is)"])
    assert s.step == 0 and s.fraction == 0.0


def test_test_only_job():
    s = transit_state(stage="Launch test (headless)", stages=["Launch test (headless)"], kind="test")
    assert s.step == 5
    s = transit_state(stage="", stages=[], kind="test")
    assert s.step == 5  # starting: nothing before the test to do


def test_pc_install():
    s = run(["Checking the game", "Check game files", "Revive", "Prepare this PC", "Add to Steam library (this PC)"],
            to="pc")
    assert s.target == "pc" and s.step == 4
    s = transit_state("done", "Installed", ["Checking the game", "Prepare this PC", "Installed"], to="pc")
    assert s.fraction == 1.0


def test_waiting_keeps_the_step():
    stages = QUEST + ["Prepare Frame", "Upload APK"]
    s = transit_state("queued", "Waiting for the Frame", stages, kind="install")
    assert s.waiting and s.step == 3 and s.fraction == pytest.approx(3 / 6)
    # resumed: the job runs again from the start
    s = run(stages + ["Patching the game"])
    assert s.step == 0 and not s.waiting


def test_failed_keeps_the_step():
    s = transit_state("failed", "Upload APK", QUEST + ["Prepare Frame", "Upload APK"], fraction=0.3)
    assert s.failed and s.step == 3 and s.fraction < 1.0


def test_unknown_stage_keeps_the_previous_step():
    s = run(QUEST + INSTALL[:1] + ["Ensuring Proton"])
    assert s.step == 3


def test_upload_never_goes_back_between_apk_and_data():
    from frameport.ui.transit import Monotonic

    mono = Monotonic()
    base = QUEST + INSTALL[:2]
    seen = [mono.follow("j", run(base, fraction=f)).fraction for f in (0.0, 0.1, 0.2)]
    # "Upload data" starts: the job's fraction is cleared until the first tick, then continues over all files
    seen += [mono.follow("j", run(QUEST + INSTALL[:3], fraction=f)).fraction for f in (None, 0.2, 0.6, 1.0)]
    seen.append(mono.follow("j", run(QUEST + INSTALL[:4])).fraction)
    assert seen == sorted(seen)
    assert seen[3] == pytest.approx(3.2 / 6) and seen[-2] == pytest.approx(4 / 6)


def test_monotonic_resets_for_another_job_or_a_new_run():
    from frameport.ui.transit import Monotonic

    mono = Monotonic()
    assert mono.follow("a", run(QUEST + INSTALL[:3], fraction=0.9)).fraction == pytest.approx(3.9 / 6)
    assert mono.follow("a", run(["Patching the game"])).fraction == 0.0  # started over
    mono.follow("a", run(QUEST + INSTALL[:3], fraction=0.9))
    assert mono.follow("b", run(QUEST[:1])).fraction == 0.0


def test_position_portal_at_the_pc_frame_boundary():
    from frameport.ui.transit import PORTAL_AT, position

    assert PORTAL_AT == 0.5
    # Analyze/Patch/Sign (also halfway through Sign): before the portal
    for stages, f in ((QUEST[:1], None), (QUEST[:2], None), (QUEST, None), (QUEST + ALT, None)):
        assert position(run(stages, fraction=f).fraction) < 0.5
    assert position(0.0) == 0.0
    # the upload crosses the portal: just before it at 0 %, at it halfway, just after it at 100 %
    up = [position(run(QUEST + INSTALL[:2], fraction=f).fraction) for f in (0.0, 0.5, 1.0)]
    assert up[0] < 0.5 < up[2]
    assert up[1] == pytest.approx(0.5)
    # Install/Test on the Frame: after the portal; done: at the headset
    assert position(run(QUEST + INSTALL).fraction) > 0.5
    assert 0.5 < position(run(QUEST + INSTALL + ["Launch test (headless)"]).fraction) < 1.0
    assert position(transit_state("done", "Installed", QUEST + INSTALL + ["Installed"]).fraction) == 1.0
    # a PC install: same track (Prepare this PC = the crossing's start)
    pc = run(["Checking the game", "Revive", "Prepare this PC", "Add to Steam library (this PC)"], to="pc")
    assert position(pc.fraction) > 0.5


def test_position_is_monotonic():
    from frameport.ui.transit import position

    xs = [i / 600 for i in range(601)]
    ps = [position(x) for x in xs]
    assert ps == sorted(ps) and ps[-1] == 1.0
