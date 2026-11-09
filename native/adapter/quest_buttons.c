// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c: opt-in Frame controller bindings need the runtime's extension enabled first.
static int quest_buttons;
// Published instance/binding state is guarded by bindings_lock, including current-profile reporting.
static int quest_buttons_enabled;
static int app_suggested_frame;
static int generated_frame_bindings;
static pthread_mutex_t bindings_lock = PTHREAD_MUTEX_INITIALIZER;
static void reset_bindings(int frame_enabled);  // session_fixes.c, called with bindings_lock held
static const char *const FRAME_PROFILE = "/interaction_profiles/valve/frame_controller_valve";
static const char *const FRAME_EXTENSION = "XR_VALVE_frame_controller_interaction";

static void quest_buttons_setting(const char *line) {
    if (strncmp(line, "quest_buttons=", 14)) return;
    float value;
    char extra;
    if (sscanf(line, "quest_buttons=%f %c", &value, &extra) == 1 &&
        (value == 0 || value == 1 || value == 2 || value == 3)) quest_buttons = (int)value;
    else {
        quest_buttons = 0;
        LOG("quest_buttons: invalid setting; using 0");
    }
}

// `names` has room for one more extension. Use only the next layer's list, never an emulated extension list.
static int quest_buttons_extensions(const XrExtensionProperties *available, uint32_t count,
                                     const char **names, uint32_t *kept) {
    // This is a proposed instance's extension list; do not change the live instance until creation succeeds.
    if (!quest_buttons) return 0;
    int offered = 0, requested = 0;
    for (uint32_t i = 0; available && i < count; ++i)
        offered |= !strcmp(available[i].extensionName, FRAME_EXTENSION);
    for (uint32_t i = 0; i < *kept; ++i) requested |= !strcmp(names[i], FRAME_EXTENSION);
    if (offered) {
        if (!requested) names[(*kept)++] = FRAME_EXTENSION;
        LOG("quest_buttons: extension %s enabled", FRAME_EXTENSION);
    } else LOG("quest_buttons: extension %s not offered by the runtime", FRAME_EXTENSION);
    return offered;
}
