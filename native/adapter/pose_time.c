// Included by frame_adapter.c: the times games locate poses at (pose_time_fix, pose_debug).
//
// pose_debug (diagnostics): every ~5 s, per located space (xrLocateSpace/xrLocateSpaces) and for xrLocateViews, the
// number of calls and the min/max of (requested time - the last xrWaitFrame's predictedDisplayTime); for
// xrConvertTimespecTimeToTimeKHR the same for its answers, plus the spread of the XrTime-vs-CLOCK_MONOTONIC offset
// measured at xrWaitFrame.

static XrDuration last_display_period;

#define PT_SLOTS 12
typedef struct {
    const char *kind;
    uint64_t space, base;
    int count;
    long long min, max;  // ns relative to the last predicted display time
} pt_stat;
static pt_stat pt_stats[PT_SLOTS];
static int pt_overflow;
static long long pt_offset_min, pt_offset_max;
static int pt_offset_count;
static pthread_mutex_t pt_lock = PTHREAD_MUTEX_INITIALIZER;

static void pose_time_note(const char *kind, uint64_t space, uint64_t base, XrTime time) {
    if (!pose_debug || !last_predicted_time) return;
    long long d = (long long)(time - last_predicted_time);
    pthread_mutex_lock(&pt_lock);
    int i = 0;
    while (i < PT_SLOTS && pt_stats[i].count &&
           !(pt_stats[i].kind == kind && pt_stats[i].space == space && pt_stats[i].base == base))
        ++i;
    if (i == PT_SLOTS) {
        ++pt_overflow;
    } else {
        pt_stat *s = &pt_stats[i];
        if (!s->count) s->kind = kind, s->space = space, s->base = base, s->min = s->max = d;
        if (d < s->min) s->min = d;
        if (d > s->max) s->max = d;
        ++s->count;
    }
    pthread_mutex_unlock(&pt_lock);
}

// The offset sampled at xrWaitFrame dips (by up to 2.5 s) whenever xrWaitFrame returns late after a hitch, which put
// corrected times up to 2 s in the future (headless survey, 2026-10-09). The steady value is the largest recent sample:
// pose_time_fix uses the maximum of the last PT_RING samples (~2 s at 72 Hz; the offset drifts only ~0.14 s per hour).
#define PT_RING 144
static int64_t pt_ring[PT_RING];
static int pt_ring_n, pt_ring_i;

static void pose_time_sample(int64_t offset) {  // XrTime "now" minus CLOCK_MONOTONIC, from xrWaitFrame
    pthread_mutex_lock(&pt_lock);
    pt_ring[pt_ring_i] = offset;
    pt_ring_i = (pt_ring_i + 1) % PT_RING;
    if (pt_ring_n < PT_RING) ++pt_ring_n;
    pthread_mutex_unlock(&pt_lock);
}

// The last few predicted display times (xrWaitFrame): a pose asked for at one of them is the game rendering a frame
// and is never moved. On a Frame whose XrTime runs only ~70 ms ahead of the monotonic clock (GitHub #49), such a
// request could look nearer the monotonic "now" than XrTime's and was moved 68 ms into the future.
#define PT_DISPLAY_RING 6
#define PT_DISPLAY_MATCH 1000000ll  // 1 ms
static XrTime pt_display[PT_DISPLAY_RING];
static int pt_display_i;

static void pose_time_display(XrTime predicted) {  // from xrWaitFrame
    pthread_mutex_lock(&pt_lock);
    pt_display[pt_display_i] = predicted;
    pt_display_i = (pt_display_i + 1) % PT_DISPLAY_RING;
    pthread_mutex_unlock(&pt_lock);
}

static int pose_time_is_display(XrTime time) {
    int found = 0;
    pthread_mutex_lock(&pt_lock);
    for (int i = 0; i < PT_DISPLAY_RING && !found; i++) {
        long long d = (long long)(time - pt_display[i]);
        found = pt_display[i] && d <= PT_DISPLAY_MATCH && d >= -PT_DISPLAY_MATCH;
    }
    pthread_mutex_unlock(&pt_lock);
    return found;
}

static int64_t pose_time_offset(void) {
    pthread_mutex_lock(&pt_lock);
    int64_t best = pt_ring_n ? pt_ring[0] : 0;
    for (int i = 1; i < pt_ring_n; i++)
        if (pt_ring[i] > best) best = pt_ring[i];
    pthread_mutex_unlock(&pt_lock);
    return best;
}

static void pose_time_note_offset(long long offset) {
    if (!pose_debug) return;
    pthread_mutex_lock(&pt_lock);
    if (!pt_offset_count || offset < pt_offset_min) pt_offset_min = offset;
    if (!pt_offset_count || offset > pt_offset_max) pt_offset_max = offset;
    ++pt_offset_count;
    pthread_mutex_unlock(&pt_lock);
}

// pose_time_fix: Meta's OVRPlugin takes "now" from CLOCK_MONOTONIC and passes it on as an XrTime unchanged (no
// conversion reaches FrameBridge: OVRPort's dispatcher answers XR_KHR_convert_timespec_time itself, 1:1, since the
// Frame's runtime lacks it). On a Quest XrTime is the monotonic clock; on the Frame it runs ahead (2.56 s measured on
// SteamOS 0.4.5, 0.05-0.9 s seen on older builds), so every "now" pose (Unity's physics step, OVRInput's controller
// poses) was located that far in the past and the hands trailed the controllers (BattleSisters, Sniper Elite VR).
// A located time nearer the monotonic clock than XrTime's "now" is taken as a monotonic timestamp and moved to the
// same moment in XrTime (offset measured at xrWaitFrame); a time more than PT_PAST_LIMIT before the predicted display
// time that isn't one (BattleSisters asks for its head at XrTime 0.1 s; Robo Recall, Phantom, Vader, Time Stall and
// The Room VR at ~0 every frame) is located at the predicted display time, the time the game renders with.
// A monotonic timestamp is only told apart when the clocks are more than PT_MONO_PERIODS display periods apart, and the
// time must be clearly nearer the monotonic "now" (at most a third of its distance to XrTime's "now"); a time equal to
// a recent predicted display time is never moved (GitHub #49: XrTime only ~68 ms ahead, display-time requests moved).
#define PT_PAST_LIMIT 500000000ll  // 0.5 s: no runtime keeps a longer pose history; real past queries are far shorter
#define PT_MIN_OFFSET 5000000ll    // XrTime and the monotonic clock this close: nothing to tell apart or to fix
#define PT_MONO_PERIODS 4
#define PT_DEFAULT_PERIOD 13888889ll  // 72 Hz, before xrWaitFrame reported a period
static int pt_fixed_mono, pt_fixed_past;
static long long pt_fixed_min;

static XrTime pose_time_fixed(XrTime time) {
    if (!pose_time_fix || !last_predicted_time || pt_ring_n < 8) return time;  // a few frames of offset samples first
    int64_t offset = pose_time_offset();  // XrTime "now" minus the monotonic clock (robust, see above)
    if (offset < PT_MIN_OFFSET && offset > -PT_MIN_OFFSET) return time;
    if (pose_time_is_display(time)) return time;  // the frame (or a recent one) the game renders
    long long period = last_display_period > 0 ? (long long)last_display_period : PT_DEFAULT_PERIOD;
    long long abs_offset = offset < 0 ? -offset : offset;
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    long long mono = (long long)ts.tv_sec * 1000000000ll + ts.tv_nsec;
    long long d_mono = (long long)time - mono, d_xr = (long long)time - (mono + offset);
    long long abs_mono = d_mono < 0 ? -d_mono : d_mono, abs_xr = d_xr < 0 ? -d_xr : d_xr;
    XrTime fixed;
    int kind;
    if (abs_offset > PT_MONO_PERIODS * period && abs_mono < PT_PAST_LIMIT && 3 * abs_mono < abs_xr) {
        fixed = (XrTime)(time + offset), kind = 0;  // a monotonic timestamp
    } else if ((long long)(time - last_predicted_time) < -PT_PAST_LIMIT) {
        fixed = last_predicted_time, kind = 1;  // far in the past: the frame's display time
    } else {
        return time;
    }
    if (pose_debug) {
        long long d = (long long)(time - last_predicted_time);
        pthread_mutex_lock(&pt_lock);
        if (!(pt_fixed_mono + pt_fixed_past) || d < pt_fixed_min) pt_fixed_min = d;
        ++*(kind ? &pt_fixed_past : &pt_fixed_mono);
        pthread_mutex_unlock(&pt_lock);
    }
    static int logged[2];
    if (logged[kind]++ < 3)
        LOG("pose_time_fix: pose asked for at %.1f ms from the predicted display time (%s) located at %.1f ms",
            (long long)(time - last_predicted_time) / 1e6, kind ? "far in the past" : "a monotonic-clock time",
            (long long)(fixed - last_predicted_time) / 1e6);
    return fixed;
}

// called from the pacing statistics in xrEndFrame
static void pose_time_report(void) {
    if (!pose_debug) return;
    pthread_mutex_lock(&pt_lock);
    LOG("pose_debug: period %.2f ms; XrTime - monotonic at xrWaitFrame (%d): %.2f..%.2f ms", last_display_period / 1e6,
        pt_offset_count, pt_offset_min / 1e6, pt_offset_max / 1e6);
    for (int i = 0; i < PT_SLOTS && pt_stats[i].count; ++i)
        LOG("pose_debug: %s space=0x%llx base=0x%llx: %d calls, time - predicted display time %.2f..%.2f ms",
            pt_stats[i].kind, (unsigned long long)pt_stats[i].space, (unsigned long long)pt_stats[i].base,
            pt_stats[i].count, pt_stats[i].min / 1e6, pt_stats[i].max / 1e6);
    if (pt_overflow) LOG("pose_debug: %d calls not counted (more than %d spaces)", pt_overflow, PT_SLOTS);
    if (pt_fixed_mono + pt_fixed_past)
        LOG("pose_debug: pose_time_fix moved %d monotonic-clock times and %d far-past times (earliest asked %.2f ms)",
            pt_fixed_mono, pt_fixed_past, pt_fixed_min / 1e6);
    memset(pt_stats, 0, sizeof(pt_stats));
    pt_overflow = pt_offset_count = pt_fixed_mono = pt_fixed_past = 0;
    pthread_mutex_unlock(&pt_lock);
}
