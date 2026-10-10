// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c. Per-game session fixes, all off by default (enabled in a game's recipe):
//  * layer_debug: diagnostics only — layer/swapchain details, session-state timing, reference-space creation (and how
//    far each new LOCAL space is from the session's first one), aim vs grip poses, refresh-rate calls;
//  * stable_local: every LOCAL reference space the app creates coincides with the one at session start (for runtimes
//    that place each new LOCAL space at the current head yaw, which makes menus jump to where you look);
//  * focus_hold: hides brief FOCUSED -> VISIBLE -> (SYNCHRONIZED) dips once the session has been focused for a while
//    (apps that recentre on regaining focus);
//  * aim_pitch / aim_yaw / aim_forward: correct the aim pose of controllers (pointer rays) by a fixed offset;
//  * refresh_rate: display refresh rate requested at session start (and instead of the app's own requests).
#include "layer_math.h"

static void emul_on_begin_session(const XrSessionBeginInfo *info);  // layer_emul_gl.c

static int64_t monotonic_ns(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (int64_t)now.tv_sec * 1000000000ll + now.tv_nsec;
}

// ---------------------------------------------------------------- stable_local
static XrSpace local_anchor = XR_NULL_HANDLE;  // private LOCAL space created right after xrCreateSession
static int local_anchor_stale;                 // runtime announced a LOCAL change (user recentre): re-anchor
static XrSpace debug_first_local = XR_NULL_HANDLE;  // layer_debug: first LOCAL space, to measure later ones against

static XrSpace create_local(XrSession session) {
    PFN_xrCreateReferenceSpace create = (PFN_xrCreateReferenceSpace)lookup(active_instance, "xrCreateReferenceSpace");
    XrReferenceSpaceCreateInfo ci = {XR_TYPE_REFERENCE_SPACE_CREATE_INFO, NULL, XR_REFERENCE_SPACE_TYPE_LOCAL,
                                     {{0, 0, 0, 1}, {0, 0, 0}}};
    XrSpace space = XR_NULL_HANDLE;
    if (!create || XR_FAILED(create(session, &ci, &space))) return XR_NULL_HANDLE;
    return space;
}

static void destroy_space(XrSpace space) {
    PFN_xrDestroySpace destroy = (PFN_xrDestroySpace)lookup(active_instance, "xrDestroySpace");
    if (destroy && space) destroy(space);
}

// Pose of `space` in `base` at the latest predicted display time; 0 if not (yet) known.
static int locate_pose(XrSpace space, XrSpace base, XrPosef *pose) {
    PFN_xrLocateSpace locate = (PFN_xrLocateSpace)lookup(active_instance, "xrLocateSpace");
    if (!locate || !space || !base || !last_predicted_time) return 0;
    XrSpaceLocation loc = {XR_TYPE_SPACE_LOCATION, NULL, 0, {{0, 0, 0, 1}, {0, 0, 0}}};
    if (XR_FAILED(locate(space, base, last_predicted_time, &loc))) return 0;
    const XrSpaceLocationFlags valid = XR_SPACE_LOCATION_ORIENTATION_VALID_BIT | XR_SPACE_LOCATION_POSITION_VALID_BIT;
    if ((loc.locationFlags & valid) != valid) return 0;
    *pose = loc.pose;
    return 1;
}

static void session_fixes_on_create_session(XrSession session) {
    if (stable_local) {
        local_anchor = create_local(session);
        LOG("stable_local: session anchor %s", local_anchor ? "created" : "FAILED");
    }
}

// The pose to create an app LOCAL space with, so that it coincides with the session-start LOCAL space.
static XrPosef stable_local_pose(XrSession session, XrPosef app_pose) {
    if (!local_anchor) return app_pose;
    XrSpace fresh = create_local(session);
    if (!fresh) return app_pose;
    XrPosef anchor_in_fresh;
    int known = locate_pose(local_anchor, fresh, &anchor_in_fresh);
    if (known && local_anchor_stale) {  // the user recentred: from now on the new LOCAL origin is the anchor
        destroy_space(local_anchor);
        local_anchor = fresh;
        local_anchor_stale = 0;
        LOG("stable_local: re-anchored after a runtime recentre");
        return app_pose;
    }
    destroy_space(fresh);
    if (!known) return app_pose;
    float moved = lm_length(anchor_in_fresh.position), turned = lm_angle(anchor_in_fresh.orientation);
    if (moved < 0.01f && turned < 0.01f) return app_pose;  // runtime kept LOCAL stable: nothing to correct
    LOG("stable_local: new LOCAL space is %.3f m / %.1f deg from the session's, compensating", moved,
        turned * 180 / LM_PI);
    return lm_pose_mul(anchor_in_fresh, app_pose);
}

static void debug_reference_space(XrSession session, const XrReferenceSpaceCreateInfo *info, XrSpace space) {
    const XrPosef *p = &info->poseInReferenceSpace;
    LOG("layer_debug: space created type=%d space=%p pose=(%.3f,%.3f,%.3f | %.3f,%.3f,%.3f,%.3f) t=%.3f",
        info->referenceSpaceType, (void *)(uintptr_t)space, p->position.x, p->position.y, p->position.z,
        p->orientation.x, p->orientation.y, p->orientation.z, p->orientation.w, monotonic_ns() / 1e9);
    if (info->referenceSpaceType != XR_REFERENCE_SPACE_TYPE_LOCAL) return;
    if (!debug_first_local) { debug_first_local = create_local(session); return; }
    XrPosef rel;
    if (locate_pose(space, debug_first_local, &rel))
        LOG("layer_debug: that LOCAL space sits at %.3f m / %.1f deg (yaw %.1f) from the session's first LOCAL space",
            lm_length(rel.position), lm_angle(rel.orientation) * 180 / LM_PI,
            2 * atan2f(rel.orientation.y, rel.orientation.w) * 180 / LM_PI);
    else
        LOG("layer_debug: that LOCAL space can't be located yet (no tracking)");
}

// ---------------------------------------------------------------- sync_guard
// The Frame's runtime crashed in xrSyncActions right after focus returned (SIGSEGV in vrclient.so
// CVRInputLatest::UpdateActionStateInternal, Myst; a race inside the runtime, not every launch). sync_guard runs
// xrSyncActions one at a time with xrPollEvent and skips it for a moment after FOCUSED arrives (XR_SESSION_NOT_FOCUSED
// is a success code every app handles: no input for those few frames).
#define SYNC_GUARD_PAUSE_NS 250000000ll
static pthread_mutex_t sync_lock = PTHREAD_MUTEX_INITIALIZER;
static int64_t sync_resume_at;

// layer_debug input counters, logged with the pacing line: were actions synced, did any button read as pressed?
static int input_syncs, input_sync_ok, input_bool_reads, input_bool_true, input_last_sync_result;

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrSyncActions(XrSession session, const XrActionsSyncInfo *info) {
    PFN_xrSyncActions fn = (PFN_xrSyncActions)lookup(active_instance, "xrSyncActions");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (!sync_guard) {  // installed for layer_debug / input_diag only: count, report failures
        XrResult r = fn(session, info);
        __atomic_add_fetch(&input_syncs, 1, __ATOMIC_RELAXED);
        if (r == XR_SUCCESS) __atomic_add_fetch(&input_sync_ok, 1, __ATOMIC_RELAXED);
        input_last_sync_result = r;
        if (input_diag && XR_FAILED(r)) diag_call_failed("xrSyncActions", r, NULL);
        return r;
    }
    pthread_mutex_lock(&sync_lock);
    int64_t wait = sync_resume_at - monotonic_ns();
    XrResult r = wait > 0 ? XR_SESSION_NOT_FOCUSED : fn(session, info);
    pthread_mutex_unlock(&sync_lock);
    static int logged;
    if (wait > 0 && logged++ < 5) LOG("sync_guard: input paused for %.0f ms after focus returned", wait / 1e6);
    if (input_diag && XR_FAILED(r)) diag_call_failed("xrSyncActions", r, NULL);
    return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrGetActionStateBoolean(XrSession session, const XrActionStateGetInfo *info,
        XrActionStateBoolean *state) {
    PFN_xrGetActionStateBoolean fn = (PFN_xrGetActionStateBoolean)lookup(active_instance, "xrGetActionStateBoolean");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrResult r = fn(session, info, state);
    __atomic_add_fetch(&input_bool_reads, 1, __ATOMIC_RELAXED);
    if (r == XR_SUCCESS && state && state->isActive && state->currentState)
        __atomic_add_fetch(&input_bool_true, 1, __ATOMIC_RELAXED);
    return r;
}

// ---------------------------------------------------------------- focus_hold
#define FOCUS_HOLD_MIN_FOCUSED_NS 1000000000ll  // only after this long in FOCUSED (start-up transitions untouched;
                                                // 3 s let a 27 ms dip through in Blade & Sorcery)
// dips longer than focus_hold_ms (setting, default 5000) are delivered (late, in order): taking the headset off or the
// system menu must still pause the game. A longer limit is for headsets whose wear sensor flickers ("HMD off" for
// 0.5-2 s while worn; Blade & Sorcery pauses on each)
#define FOCUS_HOLD_MAX_DIP_NS ((int64_t)(focus_hold_ms * 1000000.0f))
static struct {
    XrEventDataBuffer events[6];
    int count, holding, delivering;
    int64_t since;
} held;
static int is_focused;
static int64_t focused_since;

static int session_state_of(const XrEventDataBuffer *event) {
    return event->type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED ? (int)((const XrEventDataSessionStateChanged *)event)->state : -1;
}

static void focus_hold_note(int state) {
    if (state == XR_SESSION_STATE_FOCUSED && !is_focused) { is_focused = 1; focused_since = monotonic_ns(); }
    else if (state >= 0 && state != XR_SESSION_STATE_FOCUSED) is_focused = 0;
}

// ---------------------------------------------------------------- focus log (always on: rare events, cheap)
// Every time the runtime takes focus away and gives it back, as the runtime reports it (before focus_hold hides a dip
// from the app): FramePort's session triage counts these dips after a play session and suggests a longer
// focus_hold_ms when they keep reaching the game.
static PFN_xrPollEvent focus_log_next;
static int focus_log_focused;
static int64_t focus_log_lost_at;
static int focus_log_lost_state;

static XRAPI_ATTR XrResult XRAPI_CALL focus_log_poll(XrInstance instance, XrEventDataBuffer *event) {
    XrResult r = focus_log_next(instance, event);
    if (r != XR_SUCCESS || !event) return r;
    int state = session_state_of(event);
    if (state < 0) return r;
    if (state == XR_SESSION_STATE_FOCUSED) {
        if (focus_log_lost_at)
            LOG("focus: back after %.0f ms (lost to state %d)", (monotonic_ns() - focus_log_lost_at) / 1e6,
                focus_log_lost_state);
        focus_log_focused = 1;
        focus_log_lost_at = 0;
    } else if (focus_log_focused && (state == XR_SESSION_STATE_VISIBLE || state == XR_SESSION_STATE_SYNCHRONIZED)) {
        focus_log_focused = 0;
        focus_log_lost_at = monotonic_ns();
        focus_log_lost_state = state;
        LOG("focus: lost (state %d)", state);
    } else if (state != XR_SESSION_STATE_VISIBLE && state != XR_SESSION_STATE_SYNCHRONIZED) {
        focus_log_focused = 0;  // stopping, loss pending, exiting: the session ends, not a dip
        if (focus_log_lost_at) LOG("focus: session ending (state %d)", state);
        focus_log_lost_at = 0;
    }
    return r;
}

static XrResult focus_hold_poll(PFN_xrPollEvent fn, XrInstance instance, XrEventDataBuffer *event) {
    for (;;) {
        if (held.delivering) {  // hand held events to the app, oldest first
            if (held.count) {
                *event = held.events[0];
                memmove(&held.events[0], &held.events[1], (size_t)--held.count * sizeof(held.events[0]));
                focus_hold_note(session_state_of(event));
                return XR_SUCCESS;
            }
            held.delivering = 0;
        }
        XrResult result = fn(instance, event);
        int64_t now = monotonic_ns();
        if (result != XR_SUCCESS) {
            if (held.holding && now - held.since > FOCUS_HOLD_MAX_DIP_NS) {
                LOG("focus_hold: focus lost for > %lld ms, delivering %d state change(s)",
                    (long long)(FOCUS_HOLD_MAX_DIP_NS / 1000000), held.count);
                held.holding = 0; held.delivering = 1;
                continue;
            }
            return result;
        }
        int state = session_state_of(event);
        if (state < 0) return result;  // other events pass straight through
        if (held.holding) {
            if (state == XR_SESSION_STATE_FOCUSED) {
                LOG("focus_hold: hid a %.0f ms focus dip (%d state change(s))", (now - held.since) / 1e6, held.count);
                held.count = 0; held.holding = 0;
                continue;  // the app never saw it leave FOCUSED
            }
            if (held.count < 6) held.events[held.count++] = *event;
            if (state != XR_SESSION_STATE_VISIBLE && state != XR_SESSION_STATE_SYNCHRONIZED) {
                held.holding = 0; held.delivering = 1;  // stopping, loss pending, exiting: deliver everything now
            }
            continue;
        }
        if (state == XR_SESSION_STATE_VISIBLE && is_focused && now - focused_since >= FOCUS_HOLD_MIN_FOCUSED_NS) {
            held.holding = 1; held.since = now; held.count = 0;
            held.events[held.count++] = *event;
            continue;
        }
        focus_hold_note(state);
        return result;
    }
}

// ---------------------------------------------------------------- aim pose correction + aim/grip diagnostics
#define MAX_POSE_ACTIONS 64
static struct { XrAction action; int aim; } pose_actions[MAX_POSE_ACTIONS];
static int pose_action_count;
static struct { XrSpace space; XrPath subaction; int aim; } pose_spaces[MAX_POSE_ACTIONS];
static int pose_space_count;
static pthread_mutex_t pose_lock = PTHREAD_MUTEX_INITIALIZER;

static int aim_correction_on(void) { return aim_pitch != 0 || aim_yaw != 0 || aim_forward != 0; }

static void note_pose_binding(XrAction action, const char *path) {
    size_t n = strlen(path);
    int aim = n >= 9 && !strcmp(path + n - 9, "/aim/pose");
    int grip = n >= 10 && !strcmp(path + n - 10, "/grip/pose");
    if (!aim && !grip) return;
    pthread_mutex_lock(&pose_lock);
    int known = 0;
    for (int i = 0; i < pose_action_count; ++i) known |= pose_actions[i].action == action;
    if (!known && pose_action_count < MAX_POSE_ACTIONS) {
        pose_actions[pose_action_count].action = action;
        pose_actions[pose_action_count++].aim = aim;
    }
    pthread_mutex_unlock(&pose_lock);
}

static int pose_action_kind(XrAction action) {  // 1 aim, 0 grip, -1 unknown
    int kind = -1;
    pthread_mutex_lock(&pose_lock);
    for (int i = 0; i < pose_action_count && kind < 0; ++i)
        if (pose_actions[i].action == action) kind = pose_actions[i].aim;
    pthread_mutex_unlock(&pose_lock);
    return kind;
}

// Meta's newer controller profiles, which the Frame's runtime doesn't know (it has oculus/touch_controller). An app
// that suggests bindings only for these gets XR_ERROR_PATH_UNSUPPORTED and no input (e.g. a setup screen that can't be
// passed); its bindings are suggested again for Touch, keeping the components Touch controllers have.
static const char *const NEWER_TOUCH[] = {"/interaction_profiles/meta/touch_controller_plus",
                                          "/interaction_profiles/meta/touch_plus_controller",
                                          "/interaction_profiles/facebook/touch_controller_pro",
                                          "/interaction_profiles/meta/touch_pro_controller",
                                          "/interaction_profiles/meta/touch_controller_rift_cv1",
                                          "/interaction_profiles/meta/touch_controller_quest_1_rift_s",
                                          "/interaction_profiles/meta/touch_controller_quest_2"};
static const char *const TOUCH_PROFILE = "/interaction_profiles/oculus/touch_controller";
// oculus/touch_controller components (OpenXR spec), after /user/hand/<left|right>/
static const char *const TOUCH_COMMON[] = {"input/squeeze/value", "input/trigger/value", "input/trigger/touch",
    "input/thumbstick", "input/thumbstick/x", "input/thumbstick/y", "input/thumbstick/click",
    "input/thumbstick/touch", "input/thumbrest/touch", "input/grip/pose", "input/aim/pose", "output/haptic"};
static const char *const TOUCH_LEFT[] = {"input/x/click", "input/x/touch", "input/y/click", "input/y/touch",
    "input/menu/click"};
static const char *const TOUCH_RIGHT[] = {"input/a/click", "input/a/touch", "input/b/click", "input/b/touch",
    "input/system/click"};
static int app_suggested_touch;

static int in_list(const char *s, const char *const *list, size_t n) {
    for (size_t i = 0; i < n; ++i)
        if (!strcmp(s, list[i])) return 1;
    return 0;
}
#define IN(s, list) in_list(s, list, sizeof(list) / sizeof(*list))

// 1 if the binding path exists on oculus/touch_controller
static int touch_has(const char *path) {
    const char *left = "/user/hand/left/", *right = "/user/hand/right/";
    if (!strncmp(path, left, strlen(left))) {
        path += strlen(left);
        return IN(path, TOUCH_COMMON) || IN(path, TOUCH_LEFT);
    }
    if (!strncmp(path, right, strlen(right))) {
        path += strlen(right);
        return IN(path, TOUCH_COMMON) || IN(path, TOUCH_RIGHT);
    }
    return 0;
}

// ---------------------------------------------------------------- finger proximity from touch (proximity_emul)
// Meta's OVRPlugin reports a thumb or index finger resting near the controller through XR_FB_touch_controller_proximity
// (.../thumb_fb/proximity_fb, .../trigger/proximity_fb). The Frame's runtime doesn't have it; OVRPort's loader offers
// it to the game anyway, and the proximity actions never get a working binding: "near" stays false, so games that
// animate the hands from it show a thumb that never moves (Unreal's ThumbUp axis is "no thumb proximity", e.g. Vader
// Immortal; found by Klownicle, GitHub #49). With proximity_emul those actions are bound to the capacitive touch inputs
// instead (OpenXR combines several bindings of one boolean action with OR): thumb = thumbstick, face buttons or thumb
// rest touched; index (proximity_emul=2) = trigger touched. Only when the runtime lacks the extension itself.
static XrAction thumb_proximity_action, trigger_proximity_action;

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrCreateAction(XrActionSet set, const XrActionCreateInfo *info,
        XrAction *action) {
    PFN_xrCreateAction fn = (PFN_xrCreateAction)lookup(active_instance, "xrCreateAction");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrResult r = fn(set, info, action);
    if (XR_SUCCEEDED(r) && info && action) {  // OVRPlugin's action names
        if (!strcmp(info->actionName, "hand_thumb_proximity")) thumb_proximity_action = *action;
        else if (!strcmp(info->actionName, "hand_trigger_proximity")) trigger_proximity_action = *action;
        else return r;
        LOG("proximity_emul: game action %s found", info->actionName);
    }
    return r;
}

static const char *const THUMB_TOUCH_LEFT[] = {"x/touch", "y/touch", "thumbstick/touch", "thumbrest/touch"};
static const char *const THUMB_TOUCH_RIGHT[] = {"a/touch", "b/touch", "thumbstick/touch", "thumbrest/touch"};

static int add_binding(XrInstance instance, PFN_xrStringToPath to_path, XrActionSuggestedBinding *b, uint32_t *n,
                       XrAction action, const char *hand, const char *input) {
    char path[XR_MAX_PATH_LENGTH];
    snprintf(path, sizeof(path), "/user/hand/%s/input/%s", hand, input);
    XrPath p;
    if (XR_FAILED(to_path(instance, path, &p))) return 0;
    for (uint32_t i = 0; i < *n; ++i)
        if (b[i].action == action && b[i].binding == p) return 0;
    b[*n].action = action;
    b[(*n)++].binding = p;
    return 1;
}

// Suggests Touch bindings with finger proximity bound to the touch inputs (proximity_emul), falling back to the
// bindings as given when the runtime refuses them.
static XrResult suggest_touch(XrInstance instance, PFN_xrSuggestInteractionProfileBindings fn,
                              PFN_xrPathToString to_string, const XrInteractionProfileSuggestedBinding *suggested) {
    PFN_xrStringToPath to_path = (PFN_xrStringToPath)lookup(instance, "xrStringToPath");
    if (!proximity_emul || runtime_has_proximity || !to_path || !to_string) return fn(instance, suggested);
    XrAction thumb = thumb_proximity_action, index = proximity_emul > 1 ? trigger_proximity_action : XR_NULL_HANDLE;
    uint32_t n = suggested->countSuggestedBindings, kept = 0, dropped = 0, added = 0;
    XrActionSuggestedBinding *b = calloc(n + 2 * 5, sizeof(*b));
    if (!b) return fn(instance, suggested);
    for (uint32_t i = 0; i < n; ++i) {
        char path[XR_MAX_PATH_LENGTH];
        uint32_t size = 0;
        const XrActionSuggestedBinding *s = &suggested->suggestedBindings[i];
        if (XR_SUCCEEDED(to_string(instance, s->binding, sizeof(path), &size, path)) && strstr(path, "/proximity_")) {
            // the game's own proximity binding, which the runtime doesn't know: its action is bound to touch below
            if (strstr(path, "/thumb")) { if (!thumb) thumb = s->action; }
            else if (proximity_emul > 1 && !index) index = s->action;
            ++dropped;
            continue;
        }
        b[kept++] = *s;
    }
    for (int hand = 0; hand < 2; ++hand) {
        const char *name = hand ? "right" : "left";
        const char *const *thumb_inputs = hand ? THUMB_TOUCH_RIGHT : THUMB_TOUCH_LEFT;
        for (int c = 0; thumb && c < 4; ++c) added += add_binding(instance, to_path, b, &kept, thumb, name, thumb_inputs[c]);
        if (index) added += add_binding(instance, to_path, b, &kept, index, name, "trigger/touch");
    }
    XrResult r;
    if (!added && !dropped) r = fn(instance, suggested);
    else {
        XrInteractionProfileSuggestedBinding copy = *suggested;
        copy.countSuggestedBindings = kept;
        copy.suggestedBindings = b;
        r = fn(instance, &copy);
        LOG("proximity_emul: finger proximity bound to touch (%u binding(s) added, %u proximity path(s) replaced): %d",
            added, dropped, r);
        if (XR_FAILED(r)) r = fn(instance, suggested);
    }
    free(b);
    return r;
}

static XrResult remap_to_touch(XrInstance instance, PFN_xrSuggestInteractionProfileBindings fn,
                               PFN_xrPathToString to_string, const XrInteractionProfileSuggestedBinding *suggested,
                               const char *profile) {
    PFN_xrStringToPath to_path = (PFN_xrStringToPath)lookup(instance, "xrStringToPath");
    XrPath touch;
    if (!to_path || XR_FAILED(to_path(instance, TOUCH_PROFILE, &touch))) return XR_ERROR_PATH_UNSUPPORTED;
    uint32_t n = suggested->countSuggestedBindings, kept = 0;
    XrActionSuggestedBinding *b = calloc(n ? n : 1, sizeof(*b));
    if (!b) return XR_ERROR_OUT_OF_MEMORY;
    for (uint32_t i = 0; i < n; ++i) {
        char path[XR_MAX_PATH_LENGTH];
        uint32_t size = 0;
        if (XR_SUCCEEDED(to_string(instance, suggested->suggestedBindings[i].binding, sizeof(path), &size, path))
                && touch_has(path))
            b[kept++] = suggested->suggestedBindings[i];
    }
    XrInteractionProfileSuggestedBinding copy = *suggested;
    copy.interactionProfile = touch;
    copy.countSuggestedBindings = kept;
    copy.suggestedBindings = b;
    XrResult r = kept ? suggest_touch(instance, fn, to_string, &copy) : XR_ERROR_PATH_UNSUPPORTED;
    free(b);
    LOG("controller profile %s -> oculus/touch_controller (%u bindings, %u dropped): %d", profile + 22, kept,
        n - kept, r);
    return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrSuggestInteractionProfileBindings(XrInstance instance,
        const XrInteractionProfileSuggestedBinding *suggested) {
    PFN_xrSuggestInteractionProfileBindings fn =
        (PFN_xrSuggestInteractionProfileBindings)lookup(instance, "xrSuggestInteractionProfileBindings");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    PFN_xrPathToString to_string = (PFN_xrPathToString)lookup(instance, "xrPathToString");
    if (suggested && to_string && (layer_debug || aim_correction_on()))
        for (uint32_t i = 0; i < suggested->countSuggestedBindings; ++i) {
            char path[XR_MAX_PATH_LENGTH];
            uint32_t size = 0;
            if (XR_SUCCEEDED(to_string(instance, suggested->suggestedBindings[i].binding, sizeof(path), &size, path)))
                note_pose_binding(suggested->suggestedBindings[i].action, path);
        }
    char profile[XR_MAX_PATH_LENGTH] = {0};
    uint32_t size = 0;
    int known = suggested && to_string &&
                XR_SUCCEEDED(to_string(instance, suggested->interactionProfile, sizeof(profile), &size, profile));
    XrResult r = known && !strcmp(profile, TOUCH_PROFILE) ? suggest_touch(instance, fn, to_string, suggested)
                                                         : fn(instance, suggested);
    input_diag_suggested(instance, suggested, r, to_string);  // the runtime's answer to the game's own suggestion
    if (!profile_remap || !known) return r;
    if (!strcmp(profile, TOUCH_PROFILE)) {
        if (XR_SUCCEEDED(r)) app_suggested_touch = 1;  // the app's own Touch bindings win over a remap
        return r;
    }
    if (r != XR_ERROR_PATH_UNSUPPORTED || app_suggested_touch || !IN(profile, NEWER_TOUCH)) return r;
    XrResult remapped = remap_to_touch(instance, fn, to_string, suggested, profile);
    return XR_SUCCEEDED(remapped) ? XR_SUCCESS : r;
}

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrCreateActionSpace(XrSession session, const XrActionSpaceCreateInfo *info,
        XrSpace *space) {
    PFN_xrCreateActionSpace fn = (PFN_xrCreateActionSpace)lookup(active_instance, "xrCreateActionSpace");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (!info) return fn(session, info, space);
    int kind = pose_action_kind(info->action);
    XrActionSpaceCreateInfo fixed = *info;
    if (kind == 1 && aim_correction_on()) {
        XrQuaternionf q = lm_qmul(lm_axis_angle(0, 1, 0, aim_yaw * LM_PI / 180), lm_axis_angle(1, 0, 0, aim_pitch * LM_PI / 180));
        XrPosef correction = {q, {0, 0, -aim_forward}};
        fixed.poseInActionSpace = lm_pose_mul(correction, info->poseInActionSpace);
        static int logged;
        if (logged++ < 4) LOG("aim correction: pitch %.1f deg, yaw %.1f deg, forward %.3f m", aim_pitch, aim_yaw, aim_forward);
    }
    XrResult result = fn(session, &fixed, space);
    if (layer_debug && XR_SUCCEEDED(result) && space && kind >= 0) {
        pthread_mutex_lock(&pose_lock);
        if (pose_space_count < MAX_POSE_ACTIONS) {
            pose_spaces[pose_space_count].space = *space;
            pose_spaces[pose_space_count].subaction = info->subactionPath;
            pose_spaces[pose_space_count++].aim = kind;
        }
        pthread_mutex_unlock(&pose_lock);
        LOG("layer_debug: %s action space %p (subaction %llu)", kind ? "aim" : "grip", (void *)(uintptr_t)*space,
            (unsigned long long)info->subactionPath);
    }
    return result;
}

// layer_debug: about once a second, the aim pose of each hand expressed in its grip pose.
static void debug_aim_vs_grip(XrTime time) {
    static int64_t last;
    int64_t now = monotonic_ns();
    if (!layer_debug || !time || now - last < 1000000000ll) return;
    last = now;
    PFN_xrLocateSpace locate = (PFN_xrLocateSpace)lookup(active_instance, "xrLocateSpace");
    if (!locate) return;
    pthread_mutex_lock(&pose_lock);
    for (int a = 0; a < pose_space_count; ++a) {
        if (!pose_spaces[a].aim) continue;
        for (int g = 0; g < pose_space_count; ++g) {
            if (pose_spaces[g].aim || pose_spaces[g].subaction != pose_spaces[a].subaction) continue;
            XrSpaceLocation loc = {XR_TYPE_SPACE_LOCATION, NULL, 0, {{0, 0, 0, 1}, {0, 0, 0}}};
            if (XR_SUCCEEDED(locate(pose_spaces[a].space, pose_spaces[g].space, time, &loc)) &&
                (loc.locationFlags & XR_SPACE_LOCATION_ORIENTATION_VALID_BIT)) {
                XrQuaternionf q = loc.pose.orientation;
                float pitch = asinf(fmaxf(-1, fminf(1, 2 * (q.w * q.x - q.y * q.z)))) * 180 / LM_PI;
                float yaw = atan2f(2 * (q.w * q.y + q.x * q.z), 1 - 2 * (q.x * q.x + q.y * q.y)) * 180 / LM_PI;
                LOG("layer_debug: aim in grip (subaction %llu): pos %.3f,%.3f,%.3f pitch %.1f yaw %.1f flags 0x%llx",
                    (unsigned long long)pose_spaces[a].subaction, loc.pose.position.x, loc.pose.position.y,
                    loc.pose.position.z, pitch, yaw, (unsigned long long)loc.locationFlags);
            }
            break;
        }
    }
    pthread_mutex_unlock(&pose_lock);
}

// ---------------------------------------------------------------- refresh rate
static void debug_refresh_rates(XrSession session) {
    PFN_xrEnumerateDisplayRefreshRatesFB enumerate =
        (PFN_xrEnumerateDisplayRefreshRatesFB)lookup(active_instance, "xrEnumerateDisplayRefreshRatesFB");
    PFN_xrGetDisplayRefreshRateFB get = (PFN_xrGetDisplayRefreshRateFB)lookup(active_instance, "xrGetDisplayRefreshRateFB");
    float rates[32], current = 0;
    uint32_t n = 0;
    char text[256] = "";
    if (enumerate && XR_SUCCEEDED(enumerate(session, 32, &n, rates)))
        for (uint32_t i = 0, used = 0; i < n && i < 32 && used < sizeof(text) - 8; ++i)
            used += (uint32_t)snprintf(text + used, sizeof(text) - used, " %.0f", rates[i]);
    if (get) get(session, &current);
    LOG("layer_debug: refresh rates offered:%s; current %.1f Hz", *text ? text : " (unknown)", current);
}

static void refresh_on_begin_session(XrSession session) {
    if (layer_debug) debug_refresh_rates(session);
    if (refresh_rate <= 0) return;
    PFN_xrRequestDisplayRefreshRateFB request =
        (PFN_xrRequestDisplayRefreshRateFB)lookup(active_instance, "xrRequestDisplayRefreshRateFB");
    XrResult result = request ? request(session, refresh_rate) : XR_ERROR_FUNCTION_UNSUPPORTED;
    LOG("refresh_rate: requested %.1f Hz at session start: result %d", refresh_rate, result);
}

static XRAPI_ATTR XrResult XRAPI_CALL hook_request_refresh_rate(XrSession session, float rate) {
    PFN_xrRequestDisplayRefreshRateFB fn =
        (PFN_xrRequestDisplayRefreshRateFB)lookup(active_instance, "xrRequestDisplayRefreshRateFB");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    float wanted = refresh_rate > 0 ? refresh_rate : rate;
    XrResult result = fn(session, wanted);
    LOG("refresh rate: app asked for %.1f Hz, requested %.1f Hz: result %d", rate, wanted, result);
    // A refused override keeps the current rate (as frame-control's compat layer does) instead of failing the app.
    return XR_FAILED(result) && refresh_rate > 0 ? XR_SUCCESS : result;
}

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrBeginSession(XrSession session, const XrSessionBeginInfo *info) {
    PFN_xrBeginSession fn = (PFN_xrBeginSession)lookup(active_instance, "xrBeginSession");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrResult result = fn(session, info);
    if (XR_SUCCEEDED(result)) { emul_on_begin_session(info); refresh_on_begin_session(session); }
    return result;
}
