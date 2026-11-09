// SPDX-License-Identifier: GPL-3.0-only
// Included by session_fixes.c: Meta's detached controllers (XR_META_detached_controllers: a controller put down while
// hand tracking runs) on a runtime without that extension. Uses profile_remap, runtime_has_detached_controllers,
// active_instance, lookup and LOG.
//
// Meta's Unity OpenXR features add "Detached ... Controller" devices for /user/detached_controller_meta/left|right.
// Unity merges same-named actions of every enabled device into one action, so the first action with such a subaction
// path (e.g. "thumbstick") fails with XR_ERROR_PATH_UNSUPPORTED on the Frame's runtime, Unity attaches no action set
// at all and neither Unity nor OVRPlugin get controller input: hands lying at the play-space origin on the floor, no
// buttons (Spatial Zombies, GitHub #328). With profile_remap on and the runtime lacking the extension, these paths are
// left out of the game's actions and suggested bindings, and xrGetCurrentInteractionProfile answers "no profile"
// for them (a detached controller that never shows up) instead of XR_ERROR_PATH_UNSUPPORTED.

#include <pthread.h>

#define DETACHED_PREFIX "/user/detached_controller_meta/"

static int detached_seen;  // a detached path was left out of an action or binding: syncs are checked from then on
static int detached_filter_on(void) { return profile_remap && !runtime_has_detached_controllers && active_instance; }

// 1 if `path` is a detached controller's top-level path or one of its input/output paths
static int detached_path(XrPath path) {
    PFN_xrPathToString to_string = (PFN_xrPathToString)lookup(active_instance, "xrPathToString");
    char name[XR_MAX_PATH_LENGTH];
    uint32_t size = 0;
    return path && to_string && XR_SUCCEEDED(to_string(active_instance, path, sizeof(name), &size, name)) &&
           !strncmp(name, DETACHED_PREFIX, strlen(DETACHED_PREFIX));
}

// xrCreateAction: `info` without the detached subaction paths, in *copy (paths in `kept`, room for all of them).
// Returns `info` itself when there is nothing to leave out.
static const XrActionCreateInfo *detached_strip_action(const XrActionCreateInfo *info, XrActionCreateInfo *copy,
                                                       XrPath *kept) {
    if (!info || !info->countSubactionPaths || !info->subactionPaths || !kept || !detached_filter_on()) return info;
    uint32_t n = 0;
    for (uint32_t i = 0; i < info->countSubactionPaths; ++i)
        if (!detached_path(info->subactionPaths[i])) kept[n++] = info->subactionPaths[i];
    if (n == info->countSubactionPaths) return info;
    detached_seen = 1;
    *copy = *info;
    copy->countSubactionPaths = n;
    copy->subactionPaths = kept;
    static int logged;
    if (logged++ < 3)
        LOG("profile_remap: action %s without %u detached-controller path(s) (the runtime has no "
            "XR_META_detached_controllers)", info->actionName, info->countSubactionPaths - n);
    return copy;
}

// xrSuggestInteractionProfileBindings: `suggested` without bindings of detached controllers, in *copy (bindings in
// `kept`, room for all of them). Returns `suggested` itself when there is nothing to leave out.
static const XrInteractionProfileSuggestedBinding *detached_strip_bindings(
        const XrInteractionProfileSuggestedBinding *suggested, XrInteractionProfileSuggestedBinding *copy,
        XrActionSuggestedBinding *kept) {
    if (!suggested || !suggested->countSuggestedBindings || !suggested->suggestedBindings || !kept ||
        !detached_filter_on())
        return suggested;
    uint32_t n = 0;
    for (uint32_t i = 0; i < suggested->countSuggestedBindings; ++i)
        if (!detached_path(suggested->suggestedBindings[i].binding)) kept[n++] = suggested->suggestedBindings[i];
    if (n == suggested->countSuggestedBindings) return suggested;
    detached_seen = 1;
    *copy = *suggested;
    copy->countSuggestedBindings = n;
    copy->suggestedBindings = kept;
    static int logged;
    if (logged++ < 8) {
        PFN_xrPathToString to_string = (PFN_xrPathToString)lookup(active_instance, "xrPathToString");
        char profile[XR_MAX_PATH_LENGTH] = "?";
        uint32_t size = 0;
        if (to_string) to_string(active_instance, suggested->interactionProfile, sizeof(profile), &size, profile);
        LOG("profile_remap: %s bindings without %u detached-controller path(s)", profile,
            suggested->countSuggestedBindings - n);
    }
    return copy;
}

// xrGetCurrentInteractionProfile: a detached controller's path the runtime doesn't know has no profile.
static XrResult detached_current_profile(XrPath user, XrInteractionProfileState *state, XrResult result) {
    if (result != XR_ERROR_PATH_UNSUPPORTED || !state || !detached_filter_on() || !detached_path(user)) return result;
    state->interactionProfile = XR_NULL_PATH;
    return XR_SUCCESS;
}

// ---- xrSyncActions (GitHub #476, PadelVR Training: Unity 6 + OVRPlugin 1.207). With the detached paths left out of
// the actions, an active action set that names one as its subaction path is refused: every xrSyncActions failed with
// XR_ERROR_PATH_UNSUPPORTED from the moment the game had focus, and the game got no input at all. Such entries are
// left out of the sync (the controllers they ask for never exist here). Should the runtime still refuse the sync for
// a path, the active sets are logged once and synced for all their subaction paths (XR_NULL_PATH, once per set) from
// then on. The hook wraps whichever xrSyncActions hook (or the runtime) comes next (detached_sync_next).
static PFN_xrSyncActions detached_sync_next;
static pthread_mutex_t detached_cache_lock = PTHREAD_MUTEX_INITIALIZER;
static struct { XrPath path; int detached; } detached_cache[64];
static int detached_cache_count;

static int detached_path_cached(XrPath path) {
    if (!path) return 0;
    pthread_mutex_lock(&detached_cache_lock);
    for (int i = 0; i < detached_cache_count; ++i)
        if (detached_cache[i].path == path) {
            int d = detached_cache[i].detached;
            pthread_mutex_unlock(&detached_cache_lock);
            return d;
        }
    pthread_mutex_unlock(&detached_cache_lock);
    int d = detached_path(path);
    pthread_mutex_lock(&detached_cache_lock);
    if (detached_cache_count < 64) {
        detached_cache[detached_cache_count].path = path;
        detached_cache[detached_cache_count++].detached = d;
    }
    pthread_mutex_unlock(&detached_cache_lock);
    return d;
}

static void detached_log_sync(const XrActionsSyncInfo *info, XrResult result) {
    PFN_xrPathToString to_string = (PFN_xrPathToString)lookup(active_instance, "xrPathToString");
    LOG("profile_remap: xrSyncActions failed %d with %u active action set(s):", result,
        info ? info->countActiveActionSets : 0);
    for (uint32_t i = 0; info && info->activeActionSets && i < info->countActiveActionSets && i < 32; ++i) {
        char path[XR_MAX_PATH_LENGTH] = "(all)";
        uint32_t size = 0;
        XrPath sub = info->activeActionSets[i].subactionPath;
        if (sub && (!to_string || XR_FAILED(to_string(active_instance, sub, sizeof(path), &size, path))))
            snprintf(path, sizeof(path), "path %llu", (unsigned long long)sub);
        LOG("  set %p subaction %s", (void *)(uintptr_t)info->activeActionSets[i].actionSet, path);
    }
}

// `sets` has room for countActiveActionSets entries; returns the number kept. all_paths: XR_NULL_PATH, once per set.
static uint32_t detached_sync_sets(const XrActionsSyncInfo *info, XrActiveActionSet *sets, int all_paths,
                                   uint32_t *dropped) {
    uint32_t n = 0;
    *dropped = 0;
    for (uint32_t i = 0; i < info->countActiveActionSets; ++i) {
        XrActiveActionSet s = info->activeActionSets[i];
        if (detached_path_cached(s.subactionPath)) { ++*dropped; continue; }
        if (all_paths) {
            int dup = 0;
            for (uint32_t k = 0; k < n && !dup; ++k) dup = sets[k].actionSet == s.actionSet;
            if (dup) continue;
            s.subactionPath = XR_NULL_PATH;
        }
        sets[n++] = s;
    }
    return n;
}

static XRAPI_ATTR XrResult XRAPI_CALL detached_hook_xrSyncActions(XrSession session, const XrActionsSyncInfo *info) {
    PFN_xrSyncActions next = detached_sync_next;
    if (!next) return XR_ERROR_FUNCTION_UNSUPPORTED;
    static int all_paths, fallback_tried, logged;
    if (!detached_seen || !info || !info->countActiveActionSets || !info->activeActionSets ||
        info->countActiveActionSets > 64 || !detached_filter_on())
        return next(session, info);
    XrActiveActionSet sets[64];
    uint32_t dropped = 0, n = detached_sync_sets(info, sets, all_paths, &dropped);
    if (dropped && logged < 1) {
        logged = 1;
        LOG("profile_remap: xrSyncActions without %u active set entry(ies) for detached controllers", dropped);
    }
    XrActionsSyncInfo copy = *info;
    copy.countActiveActionSets = n;
    copy.activeActionSets = sets;
    const XrActionsSyncInfo *use = dropped || all_paths ? &copy : info;
    XrResult r = next(session, use);
    if (r == XR_ERROR_PATH_UNSUPPORTED && !fallback_tried) {
        fallback_tried = 1;
        detached_log_sync(info, r);
        n = detached_sync_sets(info, sets, 1, &dropped);
        copy.countActiveActionSets = n;
        r = next(session, &copy);
        LOG("profile_remap: xrSyncActions with every set for all its subaction paths: result %d%s", r,
            XR_SUCCEEDED(r) ? " (kept from now on)" : "");
        if (XR_SUCCEEDED(r)) all_paths = 1;
    } else if (XR_FAILED(r) && r != XR_ERROR_SESSION_LOST && logged < 2) {
        logged = 2;
        detached_log_sync(use, r);
    }
    return r;
}
