// SPDX-License-Identifier: GPL-3.0-only
// Haptics summary (logging only, GitHub #567): FrameBridge logs a game's first 8 vibration requests; a report like
// "vibrations fade after a song" needs the whole session. While vibrations happen, one line per HS_PERIOD_NS sums up
// each hand's requests: plain vibrations (count, amplitude and duration range as the game asked), stops (amplitude 0
// or xrStopHapticFeedback) and sample buffers (Meta's amplitude envelopes / PCM). The line for a period is written by
// the first request after it ends, so a quiet stretch costs nothing.
// Header-only, no Android dependencies: tests/test_framebridge_round2.py compiles it for the host.
#ifndef FRAMEBRIDGE_HAPTIC_SUMMARY_H
#define FRAMEBRIDGE_HAPTIC_SUMMARY_H
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define HS_PERIOD_NS 10000000000ll
enum { HS_LEFT, HS_RIGHT, HS_OTHER, HS_HANDS };
enum { HS_VIBRATION, HS_STOP, HS_BUFFER };

typedef struct {
    int vibrations, stops, buffers;
    float amp_min, amp_max;
    int64_t dur_min, dur_max;  // ns; XR_MIN_HAPTIC_DURATION (-1) = "shortest the device can do"
} HsHand;

typedef struct {
    int64_t start;  // first request of the current period (0 = none yet)
    long long total;
    HsHand hand[HS_HANDS];
} HsState;

// Writes the finished period's line into out (when one is due) and returns 1, then counts this request.
static inline int hs_note(HsState *s, int hand, int kind, float amplitude, int64_t duration, int64_t now,
                          char *out, size_t size) {
    int wrote = 0;
    if (s->start && now - s->start >= HS_PERIOD_NS) {
        static const char *const names[HS_HANDS] = {"left", "right", "other"};
        size_t used = (size_t)snprintf(out, size, "haptic summary: %.0f s, %lld requests this session;",
                                       (now - s->start) / 1e9, s->total);
        for (int h = 0; h < HS_HANDS && used < size; ++h) {
            const HsHand *x = &s->hand[h];
            if (!x->vibrations && !x->stops && !x->buffers) continue;
            used += (size_t)snprintf(out + used, size - used, " %s %d vibration(s)", names[h], x->vibrations);
            if (x->vibrations && used < size)
                used += (size_t)snprintf(out + used, size - used, " amplitude %.2f-%.2f duration %s%.0f-%.0f ms",
                                         x->amp_min, x->amp_max, x->dur_min < 0 ? "min/" : "",
                                         (x->dur_min < 0 ? 0 : x->dur_min) / 1e6, (x->dur_max < 0 ? 0 : x->dur_max) / 1e6);
            if (used < size)
                used += (size_t)snprintf(out + used, size - used, ", %d stop(s), %d buffer(s);", x->stops, x->buffers);
        }
        memset(s->hand, 0, sizeof s->hand);
        s->start = 0;
        wrote = 1;
    }
    if (!s->start) s->start = now ? now : 1;
    if (hand < 0 || hand >= HS_HANDS) hand = HS_OTHER;
    HsHand *x = &s->hand[hand];
    ++s->total;
    if (kind == HS_STOP) ++x->stops;
    else if (kind == HS_BUFFER) ++x->buffers;
    else {
        if (!x->vibrations || amplitude < x->amp_min) x->amp_min = amplitude;
        if (!x->vibrations || amplitude > x->amp_max) x->amp_max = amplitude;
        if (!x->vibrations || duration < x->dur_min) x->dur_min = duration;
        if (!x->vibrations || duration > x->dur_max) x->dur_max = duration;
        ++x->vibrations;
    }
    return wrote;
}
#endif
