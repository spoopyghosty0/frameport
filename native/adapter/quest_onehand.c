// SPDX-License-Identifier: GPL-3.0-only
// Included by session_fixes.c: quest_buttons=3, "One hand" (GitHub discussion #570; docs/input/quest_buttons.json
// mode 3). The right Frame controller's A/B/X/Y send Quest A/B/X/Y, the left d-pad also moves the left thumbstick
// and the left View button is Menu.
//
// X and Y are left-hand Touch inputs, so binding suggestions alone can't move them to the right controller: a
// binding on /user/hand/right feeds the action's right-hand subaction, which games read as the right hand (Unity's
// primarybutton is X on the left and A on the right: Frame X would read as A), and the left subaction stays empty.
// Neither can a binding turn d-pad buttons into a thumbstick vector. So FrameBridge reads the right X/Y and the
// left d-pad through an action set of its own (created at the game's Touch suggestion, bound in the generated Frame
// suggestion, attached and synced together with the game's sets) and answers the game's state queries for actions
// bound to left X/Y or the left thumbstick: queried for the left hand (or without a subaction path), X/Y read as
// pressed/touched while the right X/Y are, and the stick reads the d-pad's direction while a d-pad button is held.
enum { OH_X_CLICK, OH_X_TOUCH, OH_Y_CLICK, OH_Y_TOUCH, OH_DPAD_UP, OH_DPAD_DOWN, OH_DPAD_LEFT, OH_DPAD_RIGHT,
       OH_SOURCES };
// Mirrors mode 3's other_hand and dpad_stick in docs/input/quest_buttons.json (the host test derives this order)
static const char *const OH_SOURCE_PATHS[OH_SOURCES] = {
    "/user/hand/right/input/x/click", "/user/hand/right/input/x/touch",
    "/user/hand/right/input/y/click", "/user/hand/right/input/y/touch",
    "/user/hand/left/input/dpad_up/click", "/user/hand/left/input/dpad_down/click",
    "/user/hand/left/input/dpad_left/click", "/user/hand/left/input/dpad_right/click"};
static const char *const OH_SOURCE_NAMES[OH_SOURCES] = {"right_x_click", "right_x_touch", "right_y_click",
    "right_y_touch", "left_dpad_up", "left_dpad_down", "left_dpad_left", "left_dpad_right"};

// What a game action is bound to on the left Touch controller
enum { OH_ROLE_X_CLICK, OH_ROLE_X_TOUCH, OH_ROLE_Y_CLICK, OH_ROLE_Y_TOUCH, OH_ROLE_STICK, OH_ROLE_STICK_X,
       OH_ROLE_STICK_Y };
#define OH_MAX_ROLES 32
static struct { XrAction action; int role; } oh_roles[OH_MAX_ROLES];
static int oh_role_count;           // written while the game suggests bindings (before attach), read by queries
static XrInstance oh_instance;      // the instance oh_set belongs to
static XrActionSet oh_set;
static XrAction oh_actions[OH_SOURCES];
static XrPath oh_left;              // /user/hand/left
static XrSession oh_session;        // the session oh_set is attached to
static pthread_mutex_t oh_lock = PTHREAD_MUTEX_INITIALIZER;  // oh_state, oh_sync, oh_tracks
static struct { int active, on; } oh_state[OH_SOURCES];     // as of the last xrSyncActions
static uint64_t oh_sync;            // syncs that included oh_set
#define OH_TRACKS 32
// changedSinceLastSync / lastChangeTime of answers FrameBridge changed (the runtime's own describe the real input)
static struct { XrAction action; XrPath sub; int kind; uint64_t sync, changed_sync; float v[2]; XrTime changed_at;
                int synth, prev_synth; } oh_tracks[OH_TRACKS];
static int oh_track_next;

static void oh_reset(void) {  // a new instance: handles of the old one are gone (bindings_lock held)
    oh_role_count = 0;
    oh_instance = XR_NULL_HANDLE;
    oh_set = XR_NULL_HANDLE;
    memset(oh_actions, 0, sizeof(oh_actions));
    oh_left = XR_NULL_PATH;
    pthread_mutex_lock(&oh_lock);
    oh_session = XR_NULL_HANDLE;
    memset(oh_state, 0, sizeof(oh_state));
    memset(oh_tracks, 0, sizeof(oh_tracks));
    oh_track_next = 0;
    pthread_mutex_unlock(&oh_lock);
}

// A new Touch suggestion replaces the last one: forget the roles of the old set (bindings_lock held).
static void oh_begin_suggestion(void) { __atomic_store_n(&oh_role_count, 0, __ATOMIC_RELEASE); }

// Records the role of one of the game's Touch bindings, `relative` after /user/hand/<hand>/. 1 = X/Y (left out
// of the Frame suggestion and answered from the right controller), 0 = anything else (bindings_lock held).
static int oh_note_binding(XrAction action, const char *hand, const char *relative) {
    if (quest_buttons != 3 || strcmp(hand, "left")) return 0;
    static const struct { const char *path; int role; } ROLES[] = {
        {"input/x", OH_ROLE_X_CLICK}, {"input/x/click", OH_ROLE_X_CLICK}, {"input/x/touch", OH_ROLE_X_TOUCH},
        {"input/y", OH_ROLE_Y_CLICK}, {"input/y/click", OH_ROLE_Y_CLICK}, {"input/y/touch", OH_ROLE_Y_TOUCH},
        {"input/thumbstick", OH_ROLE_STICK}, {"input/thumbstick/x", OH_ROLE_STICK_X},
        {"input/thumbstick/y", OH_ROLE_STICK_Y}};
    for (size_t i = 0; i < sizeof(ROLES) / sizeof(*ROLES); ++i) {
        if (strcmp(relative, ROLES[i].path)) continue;
        int n = oh_role_count;
        int duplicate = 0;
        for (int k = 0; k < n; ++k) duplicate |= oh_roles[k].action == action && oh_roles[k].role == ROLES[i].role;
        if (!duplicate && n < OH_MAX_ROLES) {
            oh_roles[n].action = action;
            oh_roles[n].role = ROLES[i].role;
            __atomic_store_n(&oh_role_count, n + 1, __ATOMIC_RELEASE);
        }
        return ROLES[i].role <= OH_ROLE_Y_TOUCH;
    }
    return 0;
}

// Creates FrameBridge's own action set once per instance (bindings_lock held). 0 if the runtime refused it.
static int oh_create(XrInstance instance, PFN_xrStringToPath to_path) {
    if (oh_set && oh_instance == instance) return 1;
    PFN_xrCreateActionSet create_set = (PFN_xrCreateActionSet)lookup(instance, "xrCreateActionSet");
    PFN_xrCreateAction create_action = (PFN_xrCreateAction)lookup(instance, "xrCreateAction");
    PFN_xrDestroyActionSet destroy_set = (PFN_xrDestroyActionSet)lookup(instance, "xrDestroyActionSet");
    if (!create_set || !create_action || XR_FAILED(to_path(instance, "/user/hand/left", &oh_left))) return 0;
    XrActionSetCreateInfo set_info = {XR_TYPE_ACTION_SET_CREATE_INFO};
    snprintf(set_info.actionSetName, sizeof(set_info.actionSetName), "framebridge_one_hand");
    snprintf(set_info.localizedActionSetName, sizeof(set_info.localizedActionSetName), "FrameBridge one hand");
    XrActionSet set;
    XrResult r = create_set(instance, &set_info, &set);
    if (XR_FAILED(r)) {
        LOG("quest_buttons: one hand: action set not created: %d", r);
        return 0;
    }
    XrAction actions[OH_SOURCES];
    for (int i = 0; i < OH_SOURCES; ++i) {
        XrActionCreateInfo info = {XR_TYPE_ACTION_CREATE_INFO};
        info.actionType = XR_ACTION_TYPE_BOOLEAN_INPUT;
        snprintf(info.actionName, sizeof(info.actionName), "%s", OH_SOURCE_NAMES[i]);
        snprintf(info.localizedActionName, sizeof(info.localizedActionName), "%s", OH_SOURCE_NAMES[i]);
        if (XR_FAILED(r = create_action(set, &info, &actions[i]))) {
            LOG("quest_buttons: one hand: action %s not created: %d", OH_SOURCE_NAMES[i], r);
            if (destroy_set) destroy_set(set);
            return 0;
        }
    }
    memcpy(oh_actions, actions, sizeof(actions));
    oh_set = set;
    oh_instance = instance;
    return 1;
}

// Appends FrameBridge's own bindings to the generated Frame suggestion when the game has an action they serve.
// `bindings` has room for OH_SOURCES more (bindings_lock held).
static void oh_append_bindings(XrInstance instance, PFN_xrStringToPath to_path, XrActionSuggestedBinding *bindings,
                               uint32_t *kept) {
    if (quest_buttons != 3 || !oh_role_count || !oh_create(instance, to_path)) return;
    for (int i = 0; i < OH_SOURCES; ++i) {
        XrPath p;
        if (XR_FAILED(to_path(instance, OH_SOURCE_PATHS[i], &p))) continue;
        bindings[(*kept)++] = (XrActionSuggestedBinding){oh_actions[i], p};
        input_diag_quest_binding("FrameBridge", OH_SOURCE_PATHS[i]);
    }
    LOG("quest_buttons: one hand: %d game actions answered from the right X/Y and the left d-pad", oh_role_count);
}

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrAttachSessionActionSets(XrSession session,
        const XrSessionActionSetsAttachInfo *info) {
    PFN_xrAttachSessionActionSets fn = (PFN_xrAttachSessionActionSets)lookup(active_instance,
                                                                             "xrAttachSessionActionSets");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    pthread_mutex_lock(&bindings_lock);
    XrActionSet set = quest_buttons == 3 && generated_frame_bindings && !app_suggested_frame && oh_role_count
        ? oh_set : XR_NULL_HANDLE;
    pthread_mutex_unlock(&bindings_lock);
    if (!set || !info || info->countActionSets > 63) return fn(session, info);
    XrActionSet sets[64];
    if (info->countActionSets) memcpy(sets, info->actionSets, info->countActionSets * sizeof(*sets));
    sets[info->countActionSets] = set;
    XrSessionActionSetsAttachInfo copy = *info;
    copy.countActionSets = info->countActionSets + 1;
    copy.actionSets = sets;
    XrResult r = fn(session, &copy);
    if (XR_SUCCEEDED(r)) {
        pthread_mutex_lock(&oh_lock);
        oh_session = session;
        memset(oh_state, 0, sizeof(oh_state));
        pthread_mutex_unlock(&oh_lock);
        LOG("quest_buttons: one hand: FrameBridge's action set attached");
        return r;
    }
    LOG("quest_buttons: one hand: attaching FrameBridge's action set failed (%d); the game's sets alone", r);
    return fn(session, info);
}

// The game's sync info with FrameBridge's set added (`sets` has room for 64). The original when it doesn't apply.
static const XrActionsSyncInfo *oh_sync_info(XrSession session, const XrActionsSyncInfo *info, XrActionsSyncInfo *copy,
                                             XrActiveActionSet *sets) {
    if (quest_buttons != 3 || !info || info->countActiveActionSets > 63) return info;
    pthread_mutex_lock(&oh_lock);
    int ours = oh_session && oh_session == session;
    pthread_mutex_unlock(&oh_lock);
    if (!ours) return info;
    if (info->countActiveActionSets)
        memcpy(sets, info->activeActionSets, info->countActiveActionSets * sizeof(*sets));
    sets[info->countActiveActionSets] = (XrActiveActionSet){oh_set, XR_NULL_PATH};
    *copy = *info;
    copy->countActiveActionSets = info->countActiveActionSets + 1;
    copy->activeActionSets = sets;
    return copy;
}

// After the game's xrSyncActions: read FrameBridge's own actions once for every query until the next sync.
static void oh_after_sync(XrSession session, const XrActionsSyncInfo *used, const XrActionsSyncInfo *info,
                          XrResult r) {
    if (used == info) return;
    PFN_xrGetActionStateBoolean get = (PFN_xrGetActionStateBoolean)lookup(active_instance, "xrGetActionStateBoolean");
    int active[OH_SOURCES] = {0}, on[OH_SOURCES] = {0};
    for (int i = 0; get && r == XR_SUCCESS && i < OH_SOURCES; ++i) {
        XrActionStateGetInfo q = {XR_TYPE_ACTION_STATE_GET_INFO, NULL, oh_actions[i], XR_NULL_PATH};
        XrActionStateBoolean s = {XR_TYPE_ACTION_STATE_BOOLEAN};
        if (XR_SUCCEEDED(get(session, &q, &s))) { active[i] = s.isActive; on[i] = s.isActive && s.currentState; }
    }
    pthread_mutex_lock(&oh_lock);
    for (int i = 0; i < OH_SOURCES; ++i) { oh_state[i].active = active[i]; oh_state[i].on = on[i]; }
    ++oh_sync;
    pthread_mutex_unlock(&oh_lock);
}

// Roles of `action` that apply to a query for `subaction` (bit per role), 0 when nothing is answered here.
static int oh_roles_for(XrSession session, const XrActionStateGetInfo *info) {
    if (quest_buttons != 3 || !info) return 0;
    int n = __atomic_load_n(&oh_role_count, __ATOMIC_ACQUIRE), mask = 0;
    for (int i = 0; i < n; ++i)
        if (oh_roles[i].action == info->action) mask |= 1 << oh_roles[i].role;
    if (!mask || (info->subactionPath != XR_NULL_PATH && info->subactionPath != oh_left)) return 0;
    pthread_mutex_lock(&oh_lock);
    int ours = oh_session && oh_session == session;
    pthread_mutex_unlock(&oh_lock);
    return ours ? mask : 0;
}

// Change tracking for answers FrameBridge changes (oh_lock held): while FrameBridge's input contributes to an
// answer, or did at the sync before, changedSinceLastSync/lastChangeTime describe the answer given (compared with
// the answer after the previous sync); otherwise the runtime's values stay.
static void oh_track(const XrActionStateGetInfo *info, int kind, const float v[2], int synth, XrBool32 *changed,
                     XrTime *time) {
    int slot = -1;
    for (int i = 0; i < OH_TRACKS; ++i)
        if (oh_tracks[i].sync && oh_tracks[i].action == info->action && oh_tracks[i].sub == info->subactionPath &&
            oh_tracks[i].kind == kind) { slot = i; break; }
    if (slot < 0) {
        slot = oh_track_next;
        oh_track_next = (oh_track_next + 1) % OH_TRACKS;
        oh_tracks[slot].action = info->action;
        oh_tracks[slot].sub = info->subactionPath;
        oh_tracks[slot].kind = kind;
        oh_tracks[slot].sync = oh_sync + 1;  // never 0: 0 marks a free slot
        oh_tracks[slot].changed_sync = synth ? oh_sync + 1 : 0;
        oh_tracks[slot].v[0] = v[0];
        oh_tracks[slot].v[1] = v[1];
        oh_tracks[slot].changed_at = last_predicted_time;
        oh_tracks[slot].synth = synth;
        oh_tracks[slot].prev_synth = 0;
    } else if (oh_tracks[slot].sync != oh_sync + 1) {  // the first query since this sync
        if (oh_tracks[slot].v[0] != v[0] || oh_tracks[slot].v[1] != v[1]) {
            oh_tracks[slot].changed_sync = oh_sync + 1;
            oh_tracks[slot].changed_at = last_predicted_time;
        }
        oh_tracks[slot].v[0] = v[0];
        oh_tracks[slot].v[1] = v[1];
        oh_tracks[slot].sync = oh_sync + 1;
        oh_tracks[slot].prev_synth = oh_tracks[slot].synth;
        oh_tracks[slot].synth = synth;
    }
    if (!oh_tracks[slot].synth && !oh_tracks[slot].prev_synth) return;
    *changed = oh_tracks[slot].changed_sync == oh_sync + 1;
    if (oh_tracks[slot].changed_at) *time = oh_tracks[slot].changed_at;
}

// The right X/Y state for a boolean/float role mask (oh_lock held): bit 0 = some source active, bit 1 = on.
static int oh_button(int mask) {
    static const int SOURCE[4] = {OH_X_CLICK, OH_X_TOUCH, OH_Y_CLICK, OH_Y_TOUCH};
    int out = 0;
    for (int role = OH_ROLE_X_CLICK; role <= OH_ROLE_Y_TOUCH; ++role)
        if (mask & (1 << role)) {
            out |= oh_state[SOURCE[role]].active ? 1 : 0;
            out |= oh_state[SOURCE[role]].on ? 2 : 0;
        }
    return out;
}

// The d-pad as a stick direction (oh_lock held): 1 while a d-pad button is held
static int oh_dpad(float *x, float *y) {
    float dx = (float)(oh_state[OH_DPAD_RIGHT].on - oh_state[OH_DPAD_LEFT].on);
    float dy = (float)(oh_state[OH_DPAD_UP].on - oh_state[OH_DPAD_DOWN].on);
    int held = oh_state[OH_DPAD_UP].on || oh_state[OH_DPAD_DOWN].on || oh_state[OH_DPAD_LEFT].on ||
               oh_state[OH_DPAD_RIGHT].on;
    if (dx && dy) { dx *= 0.70710678f; dy *= 0.70710678f; }  // a diagonal is still a full push, not 1.41
    *x = dx;
    *y = dy;
    return held;
}

static void oh_answer_boolean(XrSession session, const XrActionStateGetInfo *info, XrActionStateBoolean *state) {
    int mask = oh_roles_for(session, info) & 0xf;
    if (!mask) return;
    pthread_mutex_lock(&oh_lock);
    int b = oh_button(mask), synth = (b & 2) && !state->currentState;
    if (b & 1) state->isActive = XR_TRUE;
    if (b & 2) state->currentState = XR_TRUE;
    float v[2] = {state->currentState ? 1.0f : 0.0f, 0};
    oh_track(info, 0, v, synth, &state->changedSinceLastSync, &state->lastChangeTime);
    pthread_mutex_unlock(&oh_lock);
}

static void oh_answer_float(XrSession session, const XrActionStateGetInfo *info, XrActionStateFloat *state) {
    int mask = oh_roles_for(session, info);
    if (!mask) return;
    pthread_mutex_lock(&oh_lock);
    int synth = 0;
    int b = oh_button(mask & 0xf);
    if (b & 1) state->isActive = XR_TRUE;
    if ((b & 2) && state->currentState < 1.0f) { state->currentState = 1.0f; synth = 1; }
    float x, y;
    if ((mask & (1 << OH_ROLE_STICK_X | 1 << OH_ROLE_STICK_Y)) && oh_dpad(&x, &y)) {
        float value = mask & (1 << OH_ROLE_STICK_X) ? x : y;
        state->isActive = XR_TRUE;
        synth |= state->currentState != value;
        state->currentState = value;
    }
    float v[2] = {state->currentState, 0};
    oh_track(info, 1, v, synth, &state->changedSinceLastSync, &state->lastChangeTime);
    pthread_mutex_unlock(&oh_lock);
}

static void oh_answer_vector2f(XrSession session, const XrActionStateGetInfo *info, XrActionStateVector2f *state) {
    if (!(oh_roles_for(session, info) & (1 << OH_ROLE_STICK))) return;
    pthread_mutex_lock(&oh_lock);
    float x, y;
    int synth = 0;
    if (oh_dpad(&x, &y)) {
        synth = state->currentState.x != x || state->currentState.y != y;
        state->isActive = XR_TRUE;
        state->currentState.x = x;
        state->currentState.y = y;
    }
    float v[2] = {state->currentState.x, state->currentState.y};
    oh_track(info, 2, v, synth, &state->changedSinceLastSync, &state->lastChangeTime);
    pthread_mutex_unlock(&oh_lock);
}

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrGetActionStateFloat(XrSession session, const XrActionStateGetInfo *info,
        XrActionStateFloat *state) {
    PFN_xrGetActionStateFloat fn = (PFN_xrGetActionStateFloat)lookup(active_instance, "xrGetActionStateFloat");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrResult r = fn(session, info, state);
    if (r == XR_SUCCESS && state) oh_answer_float(session, info, state);
    return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrGetActionStateVector2f(XrSession session,
        const XrActionStateGetInfo *info, XrActionStateVector2f *state) {
    PFN_xrGetActionStateVector2f fn = (PFN_xrGetActionStateVector2f)lookup(active_instance, "xrGetActionStateVector2f");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrResult r = fn(session, info, state);
    if (r == XR_SUCCESS && state) oh_answer_vector2f(session, info, state);
    return r;
}
