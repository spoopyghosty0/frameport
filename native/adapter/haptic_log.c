// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c: the session-long haptics summary (haptic_summary.h, GitHub #567). Logging only: the
// requests themselves pass on unchanged. Uses lookup, active_instance and LOG.
#include "haptic_summary.h"

static pthread_mutex_t hs_lock = PTHREAD_MUTEX_INITIALIZER;
static HsState hs_state;
static XrInstance hs_instance;
static XrPath hs_left, hs_right;

static int hs_hand(XrPath subaction) {  // caller holds hs_lock
    if (!subaction) return HS_OTHER;
    if (hs_instance != active_instance) {
        PFN_xrStringToPath to_path = (PFN_xrStringToPath)lookup(active_instance, "xrStringToPath");
        hs_left = hs_right = XR_NULL_PATH;
        if (to_path) {
            to_path(active_instance, "/user/hand/left", &hs_left);
            to_path(active_instance, "/user/hand/right", &hs_right);
        }
        hs_instance = active_instance;
    }
    return subaction == hs_left ? HS_LEFT : subaction == hs_right ? HS_RIGHT : HS_OTHER;
}

static void haptic_summary_record(XrPath subaction, int kind, float amplitude, int64_t duration) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    char line[512];
    pthread_mutex_lock(&hs_lock);
    if (hs_note(&hs_state, hs_hand(subaction), kind, amplitude, duration,
                (int64_t)t.tv_sec * 1000000000ll + t.tv_nsec, line, sizeof line))
        LOG("%s", line);
    pthread_mutex_unlock(&hs_lock);
}

// xrApplyHapticFeedback, as the game asked (before haptic_scale)
static void haptic_summary_note(const XrHapticActionInfo *info, const XrHapticBaseHeader *feedback) {
    if (!feedback) return;
    XrPath subaction = info ? info->subactionPath : XR_NULL_PATH;
    if (feedback->type == XR_TYPE_HAPTIC_VIBRATION) {
        const XrHapticVibration *v = (const XrHapticVibration *)feedback;
        haptic_summary_record(subaction, v->amplitude <= 0.001f ? HS_STOP : HS_VIBRATION, v->amplitude, v->duration);
    } else if (feedback->type == (XrStructureType)1000173001 || feedback->type == (XrStructureType)1000209001) {
        haptic_summary_record(subaction, HS_BUFFER, 0, 0);  // amplitude envelope / PCM samples
    }
}

static XRAPI_ATTR XrResult XRAPI_CALL hook_stop_haptics(XrSession session, const XrHapticActionInfo *info) {
    PFN_xrStopHapticFeedback fn = (PFN_xrStopHapticFeedback)lookup(active_instance, "xrStopHapticFeedback");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    haptic_summary_record(info ? info->subactionPath : XR_NULL_PATH, HS_STOP, 0, 0);
    return fn(session, info);
}
