// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c (before session_fixes.c). input_diag: controller-input diagnostics, off by default and
// hooked only when on; logs only, never changes what the game or the runtime sees. Each distinct line is logged once:
//  * every interaction profile the game suggests bindings for, with the runtime's answer; a rejected profile lists
//    all its binding paths ("unsupported: interaction profile ...");
//  * the profile the runtime reports for each hand (xrGetCurrentInteractionProfile, before controller_fix rewrites
//    the answer for the game): a game without a Steam Frame binding gets SteamVR's Touch remap, so this shows
//    whether the game's input runs through it;
//  * functions the game looks up that the runtime doesn't provide ("unsupported: function ...");
//  * failing input, haptics and performance calls with the result name and the argument that failed.

#define DIAG_MAX 512
#define DIAG_KEY (2 * XR_MAX_PATH_LENGTH + 32)
static uint64_t diag_seen[DIAG_MAX];  // FNV-1a hashes of the keys already logged
static int diag_count;
static pthread_mutex_t diag_lock = PTHREAD_MUTEX_INITIALIZER;

static int diag_first(const char *key) {  // 1 the first time `key` is seen; 0 for everything after DIAG_MAX keys
    uint64_t hash = 14695981039346656037ull;
    for (const unsigned char *c = (const unsigned char *)key; *c; ++c) hash = (hash ^ *c) * 1099511628211ull;
    pthread_mutex_lock(&diag_lock);
    int first = 1;
    for (int i = 0; i < diag_count && first; ++i) first = diag_seen[i] != hash;
    if (first && diag_count < DIAG_MAX) diag_seen[diag_count++] = hash;
    else if (first) {  // full: stop rather than repeat a per-frame failure
        static int full;
        if (!full) { full = 1; LOG("input_diag: %d distinct findings, logging no more", DIAG_MAX); }
        first = 0;
    }
    pthread_mutex_unlock(&diag_lock);
    return first;
}

static const char *diag_result_name(XrResult result, char *buf, size_t size) {
    PFN_xrResultToString fn =
        active_instance ? (PFN_xrResultToString)lookup(active_instance, "xrResultToString") : NULL;
    char name[XR_MAX_RESULT_STRING_SIZE];
    if (fn && XR_SUCCEEDED(fn(active_instance, result, name))) snprintf(buf, size, "%s", name);
    else snprintf(buf, size, "%d", result);
    return buf;
}

static void diag_call_failed(const char *fn, XrResult result, const char *detail) {
    char key[DIAG_KEY], name[XR_MAX_RESULT_STRING_SIZE];
    snprintf(key, sizeof(key), "%s:%d:%s", fn, result, detail ? detail : "");
    if (diag_first(key))
        LOG("input_diag: unsupported: %s -> %s%s%s", fn, diag_result_name(result, name, sizeof(name)),
            detail ? " " : "", detail ? detail : "");
}

// Called by hook_xrSuggestInteractionProfileBindings with the runtime's answer to the game's own suggestion (before
// profile_remap suggests it again for Touch).
static void input_diag_suggested(XrInstance instance, const XrInteractionProfileSuggestedBinding *suggested,
                                 XrResult result, PFN_xrPathToString to_string) {
    if (!input_diag || !suggested || !to_string) return;
    char profile[XR_MAX_PATH_LENGTH] = "?", key[DIAG_KEY], name[XR_MAX_RESULT_STRING_SIZE];
    uint32_t size = 0;
    to_string(instance, suggested->interactionProfile, sizeof(profile), &size, profile);
    snprintf(key, sizeof(key), "suggest:%s:%d", profile, result);
    if (!diag_first(key)) return;
    if (XR_SUCCEEDED(result)) {
        LOG("input_diag: bindings: %s accepted (%u paths)", profile, suggested->countSuggestedBindings);
        return;
    }
    LOG("input_diag: unsupported: interaction profile %s -> %s (%u bindings):", profile,
        diag_result_name(result, name, sizeof(name)), suggested->countSuggestedBindings);
    for (uint32_t i = 0; i < suggested->countSuggestedBindings; ++i) {
        char path[XR_MAX_PATH_LENGTH];
        if (XR_SUCCEEDED(to_string(instance, suggested->suggestedBindings[i].binding, sizeof(path), &size, path)))
            LOG("input_diag:   %s", path);
    }
}

// Called by xrGetCurrentInteractionProfile with the runtime's answer.
static void input_diag_current_profile(XrPath user, XrPath profile) {
    PFN_xrPathToString to_string =
        active_instance ? (PFN_xrPathToString)lookup(active_instance, "xrPathToString") : NULL;
    if (!input_diag || !to_string) return;
    char u[XR_MAX_PATH_LENGTH] = "?", p[XR_MAX_PATH_LENGTH] = "none", key[DIAG_KEY];
    uint32_t size = 0;
    to_string(active_instance, user, sizeof(u), &size, u);
    if (profile) to_string(active_instance, profile, sizeof(p), &size, p);
    snprintf(key, sizeof(key), "current:%s:%s", u, p);
    if (diag_first(key)) LOG("input_diag: bindings: %s uses %s", u, p);
}

// ---------------------------------------------------------------- failing calls
// xrSyncActions and xrSuggestInteractionProfileBindings are reported from session_fixes.c's hooks.
#define DIAG_CHECKED(name, params, args, detail)                                                                    \
    static XRAPI_ATTR XrResult XRAPI_CALL diag_##name params {                                                     \
        PFN_##name fn = (PFN_##name)lookup(active_instance, #name);                                                \
        if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;                                                             \
        XrResult result = fn args;                                                                                 \
        if (XR_FAILED(result)) diag_call_failed(#name, result, detail);                                            \
        return result;                                                                                             \
    }
static const char *diag_name_detail(const char *name) {
    static __thread char buf[XR_MAX_PATH_LENGTH + 8];
    snprintf(buf, sizeof(buf), "(%s)", name ? name : "null");
    return buf;
}
static const char *diag_perf_detail(XrPerfSettingsDomainEXT domain, XrPerfSettingsLevelEXT level) {
    static __thread char buf[64];
    snprintf(buf, sizeof(buf), "(domain %d level %d)", (int)domain, (int)level);
    return buf;
}
DIAG_CHECKED(xrStringToPath, (XrInstance instance, const char *path, XrPath *out), (instance, path, out),
             diag_name_detail(path))
DIAG_CHECKED(xrCreateAction, (XrActionSet set, const XrActionCreateInfo *info, XrAction *action), (set, info, action),
             diag_name_detail(info ? info->actionName : NULL))
DIAG_CHECKED(xrAttachSessionActionSets, (XrSession session, const XrSessionActionSetsAttachInfo *info),
             (session, info), NULL)
DIAG_CHECKED(xrApplyHapticFeedback,
             (XrSession session, const XrHapticActionInfo *info, const XrHapticBaseHeader *haptic),
             (session, info, haptic),
             haptic && haptic->type != XR_TYPE_HAPTIC_VIBRATION ? "(not XrHapticVibration)" : NULL)
DIAG_CHECKED(xrPerfSettingsSetPerformanceLevelEXT,
             (XrSession session, XrPerfSettingsDomainEXT domain, XrPerfSettingsLevelEXT level),
             (session, domain, level), diag_perf_detail(domain, level))
#undef DIAG_CHECKED

// xrGetInstanceProcAddr: our wrapper for `name`, NULL if it isn't one of ours.
static PFN_xrVoidFunction input_diag_hook(const char *name) {
#define DIAG(fn) if (!strcmp(name, #fn)) return (PFN_xrVoidFunction)diag_##fn;
    DIAG(xrStringToPath)
    DIAG(xrCreateAction)
    DIAG(xrAttachSessionActionSets)
    DIAG(xrApplyHapticFeedback)
    DIAG(xrPerfSettingsSetPerformanceLevelEXT)
#undef DIAG
    return NULL;
}

// After the runtime's own lookup: a function the game asked for that the runtime doesn't provide.
static void input_diag_lookup(XrInstance instance, const char *name, XrResult result, PFN_xrVoidFunction fn) {
    if (!input_diag || instance == XR_NULL_HANDLE || (XR_SUCCEEDED(result) && fn)) return;
    char key[DIAG_KEY], rname[XR_MAX_RESULT_STRING_SIZE];
    snprintf(key, sizeof(key), "fn:%s", name);
    if (diag_first(key))
        LOG("input_diag: unsupported: function %s not provided by the runtime (%s)", name,
            diag_result_name(result, rname, sizeof(rname)));
}
