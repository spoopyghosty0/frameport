// SPDX-License-Identifier: GPL-3.0-only
// pose_time_fix (native/adapter/pose_time.c) with synthetic clocks: which located times are moved, and where to.
#define _GNU_SOURCE
#include <assert.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#define LOG(...) ((void)0)
typedef int64_t XrTime;
typedef int64_t XrDuration;
static XrTime last_predicted_time;
static int64_t xr_time_offset;
static int pose_time_fix = 1, pose_debug = 1;
#include "pose_time.c"

#define MS 1000000ll

static long long mono_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000000ll + ts.tv_nsec;
}

// XrTime runs `offset` ahead of the monotonic clock; the frame being prepared shows two periods from now
static void frame(long long offset) {
    xr_time_offset = offset;
    pt_ring_n = pt_ring_i = 0;  // a new clock: fill the offset window with steady samples
    for (int i = 0; i < 10; i++) pose_time_sample(offset);
    last_display_period = 13888889ll;  // 72 Hz
    last_predicted_time = mono_now() + offset + 21 * MS;
    pose_time_display(last_predicted_time);
}

static void near(XrTime got, long long want) {
    long long d = (long long)got - want;
    if (d < -5 * MS || d > 5 * MS) {
        fprintf(stderr, "got %lld, want %lld (%+lld ms)\n", (long long)got, want, d / MS);
        assert(0);
    }
}

int main(void) {
    // SteamOS 0.4.5; a reporter's Frame (GitHub #49); the range seen on older builds
    const long long offsets[] = {2564 * MS, 68 * MS, 900 * MS};
    for (unsigned i = 0; i < sizeof(offsets) / sizeof(*offsets); ++i) {
        long long offset = offsets[i];
        frame(offset);
        XrTime display = last_predicted_time;
        assert(pose_time_fixed(display) == display);                    // the frame's display time
        assert(pose_time_fixed(display + 30 * MS) == display + 30 * MS);  // predicted further ahead
        long long mono = mono_now();
        near(pose_time_fixed(mono), mono + offset);                     // OVRPlugin's monotonic "now"
        near(pose_time_fixed(mono - 8 * MS), mono - 8 * MS + offset);   // a monotonic time just before
        assert(pose_time_fixed(100 * MS) == display);                   // nonsense far in the past: the display time
        pose_time_sample(offset - 1500 * MS);                            // a hitch: xrWaitFrame returned late
        near(pose_time_fixed(mono), mono + offset);                     // the steady offset still wins
    }
    frame(2564 * MS);
    assert(pose_time_fixed(last_predicted_time - 100 * MS) == last_predicted_time - 100 * MS);  // recent past: kept
    assert(pt_fixed_mono == 9 && pt_fixed_past == 3);
    // GitHub #49: XrTime 68 ms ahead and the game asks for its frame's display time 60 ms after xrWaitFrame (a hitch):
    // nearer the monotonic "now" than XrTime's, but it is the display time and stays (was moved +67.7 ms)
    frame(68 * MS);
    last_predicted_time -= 60 * MS;
    pose_time_display(last_predicted_time);
    assert(pose_time_fixed(last_predicted_time) == last_predicted_time);
    assert(pose_time_fixed(last_predicted_time + 2 * MS) == last_predicted_time + 2 * MS);  // not clearly monotonic
    XrTime previous = last_predicted_time;
    last_predicted_time += 14 * MS;  // the next frame: the previous display time is still the game's
    pose_time_display(last_predicted_time);
    assert(pose_time_fixed(previous) == previous);
    frame(50 * MS);  // clocks less than 4 display periods apart: monotonic times aren't told apart
    long long m50 = mono_now();
    assert(pose_time_fixed(m50) == m50 && pose_time_fixed(last_predicted_time) == last_predicted_time);
    assert(pose_time_fixed(100 * MS) == last_predicted_time);  // far past still handled
    assert(pt_fixed_mono == 9 && pt_fixed_past == 4);
    frame(2564 * MS);
    pt_ring_n = 3;  // too few samples yet: nothing is moved
    assert(pose_time_fixed(100 * MS) == 100 * MS);
    pt_ring_n = PT_RING < 10 ? PT_RING : 10;
    pt_fixed_mono = 9;
    last_display_period = 10 * MS;  // the diagnostics count per space and start over after each report
    pose_time_note_offset(2564 * MS);
    pose_time_note("xrLocateSpace", 1, 2, last_predicted_time - 2564 * MS);
    pose_time_note("xrLocateSpace", 1, 2, last_predicted_time);
    assert(pt_stats[0].count == 2 && pt_stats[0].min == -2564 * MS && pt_stats[0].max == 0);
    pose_time_report();
    assert(!pt_stats[0].count && !pt_fixed_mono && !pt_offset_count);
    frame(2 * MS);  // XrTime is the monotonic clock (a Quest): nothing to tell apart, nothing moved
    assert(pose_time_fixed(100 * MS) == 100 * MS);
    long long mono = mono_now();
    assert(pose_time_fixed(mono) == mono);
    frame(2564 * MS);
    pose_time_fix = 0;
    assert(pose_time_fixed(mono) == mono && pose_time_fixed(100 * MS) == 100 * MS);
    puts("ok");
    return 0;
}
