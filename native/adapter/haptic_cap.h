// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c (and the host test tests/test_haptic_cap.py): haptic_cap, GitHub #547.
//
// OVRPlugin games ask for ~2 s vibrations and renew them every frame while they want the buzz, then stop it with
// amplitude 0. When the game thread stalls (a loading screen: Five Nights at Freddy's: Help Wanted 2) neither the
// renewal nor the stop comes, and the last request buzzes for its full 2 s, or again and again across a long load.
// A game that renews every frame doesn't need long durations, so once a hand's vibration is clearly being renewed
// (HAPTIC_RENEW_STREAK calls in a row, each within HAPTIC_RENEW_NS of the last) each plain vibration is cut to a
// few renewal intervals (at least HAPTIC_CAP_MIN_NS): renewed every frame it feels the same, and a stalled game
// stops buzzing within that window. A vibration that isn't part of such a stream (a single 1-2 s rumble, two
// effects in a row) keeps its full duration: the streak starts over after any gap longer than HAPTIC_RENEW_NS.
// Every plain call counts toward the streak, amplitude 0 (a stop) too: a game calling every frame is renewing.
#define HAPTIC_CAP_SLOTS 8                 // action + subaction pairs tracked (least recently used replaced)
#define HAPTIC_RENEW_NS 250000000ll        // calls closer than this are renewals
#define HAPTIC_RENEW_STREAK 3              // renewals in a row before durations are capped
#define HAPTIC_CAP_MIN_NS 100000000ll      // a capped vibration lasts at least this long...
#define HAPTIC_CAP_INTERVALS 4             // ...and this many renewal intervals (a late frame doesn't cut the buzz)

typedef struct {
    XrAction action;
    XrPath subaction;
    int64_t last;      // monotonic ns of the last call, 0 = slot unused
    int64_t interval;  // smoothed gap between renewals (ns)
    int streak;        // renewals in a row
} haptic_stream;
static haptic_stream haptic_streams[HAPTIC_CAP_SLOTS];
static pthread_mutex_t haptic_cap_lock = PTHREAD_MUTEX_INITIALIZER;

// The duration to send for a plain vibration of `duration` ns on action/subaction at `now` (monotonic ns).
// XR_MIN_HAPTIC_DURATION (-1) and short durations pass unchanged; XR_INFINITE_DURATION is capped like any other.
static XrDuration haptic_cap_duration(XrAction action, XrPath subaction, XrDuration duration, int64_t now,
                                      int64_t *interval_out) {
    pthread_mutex_lock(&haptic_cap_lock);
    haptic_stream *s = NULL, *oldest = &haptic_streams[0];
    for (int i = 0; i < HAPTIC_CAP_SLOTS; ++i) {
        haptic_stream *c = &haptic_streams[i];
        if (c->last && c->action == action && c->subaction == subaction) { s = c; break; }
        if (c->last < oldest->last) oldest = c;
    }
    if (!s) {
        s = oldest;
        *s = (haptic_stream){action, subaction, 0, 0, 0};
    }
    int64_t gap = s->last ? now - s->last : -1;
    if (gap >= 0 && gap <= HAPTIC_RENEW_NS) {
        s->interval = s->streak ? (s->interval * 3 + gap) / 4 : gap;
        ++s->streak;
    } else s->streak = 0, s->interval = 0;
    s->last = now ? now : 1;
    XrDuration cap = HAPTIC_CAP_INTERVALS * s->interval;
    if (cap < HAPTIC_CAP_MIN_NS) cap = HAPTIC_CAP_MIN_NS;
    if (interval_out) *interval_out = s->interval;
    if (s->streak >= HAPTIC_RENEW_STREAK && duration > cap) duration = cap;
    pthread_mutex_unlock(&haptic_cap_lock);
    return duration;
}
