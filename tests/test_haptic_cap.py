"""FrameBridge haptic_cap (GitHub #547): renewed-every-frame vibrations are shortened, one-shot rumbles are not.
Opt-in (FRAMEPORT_NATIVE_TESTS=1, -m native): cached OpenXR headers and a host C compiler only, no device.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INC = next(iter((ROOT / "native/.cache").glob("openxr-*")), None)
CC = shutil.which("cc") or shutil.which("clang")
pytestmark = [pytest.mark.native, pytest.mark.skipif(
    not os.environ.get("FRAMEPORT_NATIVE_TESTS") or not INC or not CC,
    reason="set FRAMEPORT_NATIVE_TESTS=1 (needs the OpenXR headers from native/build.py and a host C compiler)")]

HARNESS = r"""
#include <openxr/openxr.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include "haptic_cap.h"
int main(void) {
    unsigned action, sub;
    long long duration, ms;
    // lines: <action> <subaction> <duration ns> <time ms>  ->  <duration sent>
    while (scanf("%u %u %lld %lld", &action, &sub, &duration, &ms) == 4)
        printf("%lld\n", (long long)haptic_cap_duration((XrAction)(uintptr_t)action, (XrPath)sub, duration,
                                                        ms * 1000000ll, NULL));
    return 0;
}
"""
S = 1_000_000_000
MS = 1_000_000


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("haptic_cap")
    (d / "harness.c").write_text(HARNESS)
    subprocess.run([CC, "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread", "-I", str(INC),
                    "-I", str(ROOT / "native/adapter"), str(d / "harness.c"), "-o", str(d / "harness")], check=True)
    return d / "harness"


def run(harness, calls):
    """calls: (action, subaction, duration ns, time ms)"""
    p = subprocess.run([str(harness)], input="".join(f"{a} {s} {d} {t}\n" for a, s, d, t in calls),
                       capture_output=True, text=True, timeout=5, check=True)
    return [int(line) for line in p.stdout.split()]


def test_renewed_every_frame_is_capped_after_the_streak(harness):
    # OVRPlugin: a 2 s vibration renewed every 14 ms (72 Hz)
    out = run(harness, [(1, 7, 2 * S, 1000 + 14 * i) for i in range(10)])
    assert out[:3] == [2 * S] * 3          # the first call and two renewals: no streak yet
    assert out[3:] == [100 * MS] * 7       # then a few frames' worth (at least 100 ms)


def test_slow_renewals_get_several_intervals(harness):
    out = run(harness, [(1, 7, 2 * S, 1000 + 200 * i) for i in range(6)])
    assert out[-1] == 800 * MS             # 4 x 200 ms: a late renewal doesn't cut the buzz


def test_one_shot_rumbles_keep_their_duration(harness):
    # single long rumbles, and two effects back to back, now and then
    calls = [(1, 7, 2 * S, 1000), (1, 7, 1500 * MS, 4000), (1, 7, 1 * S, 4050), (1, 7, 2 * S, 9000)]
    assert run(harness, calls) == [2 * S, 1500 * MS, 1 * S, 2 * S]


def test_gap_restarts_the_streak(harness):
    stream = [(1, 7, 2 * S, 1000 + 14 * i) for i in range(6)]
    after_stall = [(1, 7, 2 * S, 5000), (1, 7, 2 * S, 5014)]   # a loading screen, then the game buzzes again
    out = run(harness, stream + after_stall)
    assert out[-3] == 100 * MS and out[-2:] == [2 * S, 2 * S]


def test_hands_are_separate_and_stops_count_as_renewals(harness):
    left = [(1, 7, 2 * S, 1000 + 14 * i) for i in range(5)]
    right = [(1, 8, 2 * S, 1100)]
    stops = [(2, 7, 2 * S, 2000 + 14 * i) for i in range(4)] + [(2, 7, 2 * S, 2056)]
    out = run(harness, left + right + stops)
    assert out[4] == 100 * MS and out[5] == 2 * S   # the other hand's first call is not capped
    assert out[-1] == 100 * MS                       # amplitude-0 calls every frame are a renewal stream too


def test_short_and_minimum_durations_pass(harness):
    stream = [(1, 7, 2 * S, 1000 + 14 * i) for i in range(5)]
    out = run(harness, stream + [(1, 7, 50 * MS, 1070), (1, 7, -1, 1084), (1, 7, 0x7FFFFFFFFFFFFFFF, 1098)])
    assert out[-3:] == [50 * MS, -1, 100 * MS]


def test_many_streams_reuse_the_oldest_slot(harness):
    calls = [(a, 7, 2 * S, 1000 + 14 * i) for a in range(1, 11) for i in range(5)]
    out = run(harness, calls)
    assert out[4::5] == [100 * MS] * 10


def test_settings_and_wiring():
    source = (ROOT / "native/adapter/frame_adapter.c").read_text()
    settings = source[source.index("static void read_settings("):source.index("static void initialize(")]
    assert 'sscanf(line, "haptic_cap=%f", &value)' in settings
    assert "static int haptic_cap = 1;" in source
    apply = source[source.index("static XrResult apply_haptics("):]
    apply = apply[:apply.index("\n}\n")]
    assert "haptic_cap_duration(info->action, info->subactionPath, v.duration, monotonic_ns()" in apply
    # capped before the amplitude-0 stop, so stops count toward the renewal streak
    assert apply.index("haptic_cap_duration") < apply.index("xrStopHapticFeedback")
