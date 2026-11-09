"""FrameBridge Quest bindings, checked against the shared JSON contract with a fake OpenXR runtime.
Opt-in (FRAMEPORT_NATIVE_TESTS=1, -m native): cached OpenXR headers and a host C compiler only, no device.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INC = next(iter((ROOT / "native/.cache").glob("openxr-*")), None)
CC = shutil.which("cc") or shutil.which("clang")
pytestmark = [pytest.mark.native, pytest.mark.skipif(
    not os.environ.get("FRAMEPORT_NATIVE_TESTS") or not INC or not CC,
    reason="set FRAMEPORT_NATIVE_TESTS=1 (needs the OpenXR headers from native/build.py and a host C compiler)")]
CONTRACT = json.loads((ROOT / "docs/input/quest_buttons.json").read_text())
TOUCH = CONTRACT["touch_profile"]
FRAME = CONTRACT["frame_profile"]
PRO = "/interaction_profiles/facebook/touch_controller_pro"

HARNESS = r"""
#define XR_EXTENSION_PROTOTYPES
#include <openxr/openxr.h>
#include <math.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#define LOG(...) (fprintf(stderr, __VA_ARGS__), fputc('\n', stderr))
static XrInstance active_instance = (XrInstance)(uintptr_t)1;
static int input_diag, controller_fix, profile_remap, runtime_has_detached_controllers;
static int stable_local, layer_debug, sync_guard, action_dedup, proximity_emul, runtime_has_proximity;
static float aim_pitch, aim_yaw, aim_forward, refresh_rate, focus_hold_ms;
static XrTime last_predicted_time;
static XrDuration last_display_period;
static void emul_on_begin_session(const XrSessionBeginInfo *info) { (void)info; }
static int hide_fb_foveation_vulkan, hide_fb_foveation, hide_space_warp, scene_emul, emulate_scene, scene_app_mesh;
static int controller_models, render_models_available, emulate_render_model, passthrough_emul, emulate_passthrough;
static int foveation_eye_enabled, foveation_eye, foveation_fix, surface_emul, runtime_has_image_layout;
static int no_equirect, no_equirect2, no_cylinder, no_cube, fb_color_space, emulate_color_space;
static int space_warp_emul, sw_enabled;
static void *android_vm;
static void mx_audio_initialize(void) {}
static void mx_batman_geometry_initialize(void) {}
static int is_scene_extension(const char *name) { (void)name; return 0; }
#include "hide_fb_foveation.h"
#include "quest_buttons.c"

static char paths[1024][XR_MAX_PATH_LENGTH];
static int path_count, offered, enum_calls, create_calls;
static XrResult reject_touch_once;
static XrResult touch_result, frame_result, create_result;
static XrPath runtime_profile;
static XRAPI_ATTR XrResult XRAPI_CALL fake_string_to_path(XrInstance i, const char *s, XrPath *out) {
    (void)i;
    for (int k = 0; k < path_count; ++k) if (!strcmp(paths[k], s)) { *out = (XrPath)(k + 1); return XR_SUCCESS; }
    if (path_count >= 1024) abort();
    snprintf(paths[path_count], XR_MAX_PATH_LENGTH, "%s", s);
    *out = (XrPath)++path_count;
    return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL fake_path_to_string(XrInstance i, XrPath p, uint32_t cap, uint32_t *n,
                                                        char *out) {
    (void)i;
    if (!p || p > (XrPath)path_count) return XR_ERROR_PATH_INVALID;
    *n = (uint32_t)snprintf(out, cap, "%s", paths[p - 1]) + 1;
    return XR_SUCCESS;
}
static XrPath P(const char *s) { XrPath p; fake_string_to_path(0, s, &p); return p; }
static XRAPI_ATTR XrResult XRAPI_CALL fake_suggest(XrInstance i, const XrInteractionProfileSuggestedBinding *s) {
    (void)i;
    const char *profile = paths[s->interactionProfile - 1];
    printf("CALL %s %u\n", profile, s->countSuggestedBindings);
    for (uint32_t k = 0; k < s->countSuggestedBindings; ++k)
        printf("%llu %s\n", (unsigned long long)(uintptr_t)s->suggestedBindings[k].action,
               paths[s->suggestedBindings[k].binding - 1]);
    if (!strcmp(profile, FRAME_PROFILE)) return frame_result;
    if (!strcmp(profile, "/interaction_profiles/oculus/touch_controller")) {
        if (reject_touch_once) { XrResult r = reject_touch_once; reject_touch_once = 0; return r; }
        return touch_result;
    }
    return XR_ERROR_PATH_UNSUPPORTED;
}
static XRAPI_ATTR XrResult XRAPI_CALL fake_enumerate(const char *layer, uint32_t cap, uint32_t *n,
                                                    XrExtensionProperties *out) {
    (void)layer;
    ++enum_calls;
    printf("ENUM %u\n", cap);
    if (offered == 5 || (offered == 4 && cap)) return XR_ERROR_RUNTIME_FAILURE;
    *n = offered == 2 ? 0 : (offered == 1 ? 2 : 1);
    if (cap && *n) {
        snprintf(out[0].extensionName, sizeof(out[0].extensionName), "XR_KHR_vulkan_enable");
        if (*n > 1) snprintf(out[1].extensionName, sizeof(out[1].extensionName), "%s", FRAME_EXTENSION);
    }
    return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL fake_create(const XrInstanceCreateInfo *info, XrInstance *instance) {
    printf("CREATE %u\n", info->enabledExtensionCount);
    for (uint32_t k = 0; k < info->enabledExtensionCount; ++k) printf("EXT %s\n", info->enabledExtensionNames[k]);
    if (XR_SUCCEEDED(create_result)) *instance = (XrInstance)(uintptr_t)++create_calls;
    return create_result;
}
static XRAPI_ATTR XrResult XRAPI_CALL fake_current(XrSession s, XrPath hand, XrInteractionProfileState *out) {
    (void)s; (void)hand;
    out->interactionProfile = runtime_profile;
    return XR_SUCCESS;
}
// One hand: FrameBridge's own action set and the runtime's action states (indexed by action handle)
static int created_actions, attach_result, refuse_set;
static struct { int active; float x, y; } fake_state[2048];
static XRAPI_ATTR XrResult XRAPI_CALL fake_create_set(XrInstance i, const XrActionSetCreateInfo *info,
                                                     XrActionSet *out) {
    (void)i;
    printf("ACTIONSET %s\n", info->actionSetName);
    if (refuse_set) return XR_ERROR_NAME_DUPLICATED;
    *out = (XrActionSet)(uintptr_t)900;
    return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL fake_create_action(XrActionSet set, const XrActionCreateInfo *info,
                                                        XrAction *out) {
    printf("ACTION %llu %s %d %u\n", (unsigned long long)(uintptr_t)set, info->actionName, (int)info->actionType,
           info->countSubactionPaths);
    *out = (XrAction)(uintptr_t)(1001 + created_actions++);
    return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL fake_attach(XrSession s, const XrSessionActionSetsAttachInfo *info) {
    printf("ATTACH %llu", (unsigned long long)(uintptr_t)s);
    for (uint32_t k = 0; k < info->countActionSets; ++k)
        printf(" %llu", (unsigned long long)(uintptr_t)info->actionSets[k]);
    printf("\n");
    if (attach_result && info->countActionSets > 1) return (XrResult)attach_result;
    return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL fake_sync(XrSession s, const XrActionsSyncInfo *info) {
    printf("SYNC %llu", (unsigned long long)(uintptr_t)s);
    for (uint32_t k = 0; k < info->countActiveActionSets; ++k)
        printf(" %llu", (unsigned long long)(uintptr_t)info->activeActionSets[k].actionSet);
    printf("\n");
    return XR_SUCCESS;
}
static int known_action(XrAction a) { return (uintptr_t)a < 2048; }
static XRAPI_ATTR XrResult XRAPI_CALL fake_get_bool(XrSession s, const XrActionStateGetInfo *info,
                                                   XrActionStateBoolean *out) {
    (void)s;
    if (!known_action(info->action)) return XR_ERROR_HANDLE_INVALID;
    out->isActive = fake_state[(uintptr_t)info->action].active;
    out->currentState = fake_state[(uintptr_t)info->action].x != 0;
    out->changedSinceLastSync = 0;
    out->lastChangeTime = 5;
    return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL fake_get_float(XrSession s, const XrActionStateGetInfo *info,
                                                    XrActionStateFloat *out) {
    (void)s;
    if (!known_action(info->action)) return XR_ERROR_HANDLE_INVALID;
    out->isActive = fake_state[(uintptr_t)info->action].active;
    out->currentState = fake_state[(uintptr_t)info->action].x;
    out->changedSinceLastSync = 0;
    out->lastChangeTime = 5;
    return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL fake_get_vec2(XrSession s, const XrActionStateGetInfo *info,
                                                   XrActionStateVector2f *out) {
    (void)s;
    if (!known_action(info->action)) return XR_ERROR_HANDLE_INVALID;
    out->isActive = fake_state[(uintptr_t)info->action].active;
    out->currentState.x = fake_state[(uintptr_t)info->action].x;
    out->currentState.y = fake_state[(uintptr_t)info->action].y;
    out->changedSinceLastSync = 0;
    out->lastChangeTime = 5;
    return XR_SUCCESS;
}
static PFN_xrVoidFunction lookup(XrInstance i, const char *name) {
    (void)i;
    if (!strcmp(name, "xrCreateActionSet")) return (PFN_xrVoidFunction)fake_create_set;
    if (!strcmp(name, "xrCreateAction")) return (PFN_xrVoidFunction)fake_create_action;
    if (!strcmp(name, "xrAttachSessionActionSets")) return (PFN_xrVoidFunction)fake_attach;
    if (!strcmp(name, "xrSyncActions")) return (PFN_xrVoidFunction)fake_sync;
    if (!strcmp(name, "xrGetActionStateBoolean")) return (PFN_xrVoidFunction)fake_get_bool;
    if (!strcmp(name, "xrGetActionStateFloat")) return (PFN_xrVoidFunction)fake_get_float;
    if (!strcmp(name, "xrGetActionStateVector2f")) return (PFN_xrVoidFunction)fake_get_vec2;
    if (!strcmp(name, "xrStringToPath")) return (PFN_xrVoidFunction)fake_string_to_path;
    if (!strcmp(name, "xrPathToString")) return (PFN_xrVoidFunction)fake_path_to_string;
    if (!strcmp(name, "xrSuggestInteractionProfileBindings")) return (PFN_xrVoidFunction)fake_suggest;
    if (!strcmp(name, "xrEnumerateInstanceExtensionProperties"))
        return offered == 3 ? NULL : (PFN_xrVoidFunction)fake_enumerate;
    if (!strcmp(name, "xrCreateInstance")) return (PFN_xrVoidFunction)fake_create;
    if (!strcmp(name, "xrGetCurrentInteractionProfile")) return (PFN_xrVoidFunction)fake_current;
    return NULL;
}
#include "input_diag.c"
#include "session_fixes.c"
// The two exported adapter functions below are copied verbatim into this host translation unit by the fixture;
// the rest of frame_adapter.c needs Android graphics headers and is covered by the NDK build.
@ADAPTER_FUNCTIONS@

int main(int argc, char **argv) {
    if (argc != 9) return 2;
    char setting[256];
    snprintf(setting, sizeof(setting), "quest_buttons=%s\n", argv[1]);
    quest_buttons_setting(setting);
    printf("MODE %d\n", quest_buttons);
    offered = atoi(argv[2]);
    input_diag = atoi(argv[4]);
    profile_remap = atoi(argv[5]);
    controller_fix = atoi(argv[6]);
    create_result = (XrResult)atoi(argv[7]);
    proximity_emul = atoi(argv[8]);
    const char *extensions[2] = {"XR_KHR_vulkan_enable", FRAME_EXTENSION};
    XrInstanceCreateInfo ci = {XR_TYPE_INSTANCE_CREATE_INFO};
    ci.enabledExtensionCount = atoi(argv[3]) ? 2 : 1;
    ci.enabledExtensionNames = extensions;
    XrInstance instance;
    xrCreateInstance(&ci, &instance);
    printf("ENABLED %d\n", quest_buttons_enabled);
    char command[32], profile[XR_MAX_PATH_LENGTH];
    while (scanf("%31s", command) == 1) {
        if (!strcmp(command, "create")) {
            int result;
            if (scanf("%d %d", &offered, &result) != 2) return 2;
            create_result = (XrResult)result;
            xrCreateInstance(&ci, &instance);
            printf("ENABLED %d\nACTIVE %llu\n", quest_buttons_enabled, (unsigned long long)(uintptr_t)active_instance);
        } else if (!strcmp(command, "proximity")) {
            unsigned thumb, trigger;
            if (scanf("%u %u", &thumb, &trigger) != 2) return 2;
            thumb_proximity_action = (XrAction)(uintptr_t)thumb;
            trigger_proximity_action = (XrAction)(uintptr_t)trigger;
        } else if (!strcmp(command, "state")) {  // state <action> <active> <x> <y>: the runtime's answer
            unsigned action;
            int active;
            float x, y;
            if (scanf("%u %d %f %f", &action, &active, &x, &y) != 4 || action >= 2048) return 2;
            fake_state[action].active = active;
            fake_state[action].x = x;
            fake_state[action].y = y;
        } else if (!strcmp(command, "refuse_set")) {
            refuse_set = 1;
        } else if (!strcmp(command, "attach_result")) {
            if (scanf("%d", &attach_result) != 1) return 2;
        } else if (!strcmp(command, "attach")) {  // attach <session>: the game attaches its set 77
            unsigned session;
            if (scanf("%u", &session) != 1) return 2;
            XrActionSet set = (XrActionSet)(uintptr_t)77;
            XrSessionActionSetsAttachInfo a = {XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO, NULL, 1, &set};
            printf("RESULT %d\n", hook_xrAttachSessionActionSets((XrSession)(uintptr_t)session, &a));
        } else if (!strcmp(command, "sync")) {  // sync <session> <predicted time>
            unsigned session;
            long long t;
            if (scanf("%u %lld", &session, &t) != 2) return 2;
            last_predicted_time = (XrTime)t;
            XrActiveActionSet set = {(XrActionSet)(uintptr_t)77, XR_NULL_PATH};
            XrActionsSyncInfo y = {XR_TYPE_ACTIONS_SYNC_INFO, NULL, 1, &set};
            printf("RESULT %d\n", hook_xrSyncActions((XrSession)(uintptr_t)session, &y));
        } else if (!strcmp(command, "get")) {  // get <bool|float|vec2> <session> <action> <subaction path|none>
            char kind[8], sub[XR_MAX_PATH_LENGTH];
            unsigned session, action;
            if (scanf("%7s %u %u %255s", kind, &session, &action, sub) != 4) return 2;
            XrActionStateGetInfo g = {XR_TYPE_ACTION_STATE_GET_INFO, NULL, (XrAction)(uintptr_t)action,
                                      strcmp(sub, "none") ? P(sub) : XR_NULL_PATH};
            XrSession s = (XrSession)(uintptr_t)session;
            if (!strcmp(kind, "bool")) {
                XrActionStateBoolean b = {XR_TYPE_ACTION_STATE_BOOLEAN};
                XrResult r = hook_xrGetActionStateBoolean(s, &g, &b);
                printf("STATE %d %d %d %d %lld\n", r, b.isActive, b.currentState, b.changedSinceLastSync,
                       (long long)b.lastChangeTime);
            } else if (!strcmp(kind, "float")) {
                XrActionStateFloat f = {XR_TYPE_ACTION_STATE_FLOAT};
                XrResult r = hook_xrGetActionStateFloat(s, &g, &f);
                printf("STATE %d %d %.3f %d %lld\n", r, f.isActive, f.currentState, f.changedSinceLastSync,
                       (long long)f.lastChangeTime);
            } else {
                XrActionStateVector2f v = {XR_TYPE_ACTION_STATE_VECTOR2F};
                XrResult r = hook_xrGetActionStateVector2f(s, &g, &v);
                printf("STATE %d %d %.3f,%.3f %d %lld\n", r, v.isActive, v.currentState.x, v.currentState.y,
                       v.changedSinceLastSync, (long long)v.lastChangeTime);
            }
        } else if (!strcmp(command, "reject_touch_once")) {
            int result;
            if (scanf("%d", &result) != 1) return 2;
            reject_touch_once = (XrResult)result;
        } else if (!strcmp(command, "profile")) {
            if (scanf("%255s", profile) != 1) return 2;
            runtime_profile = !strcmp(profile, "none") ? XR_NULL_PATH : P(profile);
            XrInteractionProfileState state = {XR_TYPE_INTERACTION_PROFILE_STATE};
            xrGetCurrentInteractionProfile(XR_NULL_HANDLE, P("/user/hand/left"), &state);
            printf("PROFILE %s\n", state.interactionProfile ? paths[state.interactionProfile - 1] : "none");
        } else if (!strcmp(command, "suggest")) {
            unsigned n;
            int tr, fr;
            if (scanf("%255s %u %d %d", profile, &n, &tr, &fr) != 4 || n > 128) return 2;
            touch_result = (XrResult)tr;
            frame_result = (XrResult)fr;
            XrActionSuggestedBinding b[128];
            for (unsigned k = 0; k < n; ++k) {
                unsigned action;
                char path[XR_MAX_PATH_LENGTH];
                if (scanf("%u %255s", &action, path) != 2) return 2;
                b[k] = (XrActionSuggestedBinding){(XrAction)(uintptr_t)action, P(path)};
            }
            XrInteractionProfileSuggestedBinding s = {XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING, NULL,
                                                      P(profile), n, b};
            printf("RESULT %d\n", hook_xrSuggestInteractionProfileBindings(instance, &s));
        } else return 2;
    }
    return 0;
}
"""


def adapter_function(source, name):
    start = source.index(f"XRAPI_ATTR XrResult XRAPI_CALL {name}(")
    return source[start:source.index("\n}\n", start) + 3]


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("quest_buttons")
    source = (ROOT / "native/adapter/frame_adapter.c").read_text()
    functions = "\n".join(adapter_function(source, name) for name in
                          ("xrCreateInstance", "xrGetCurrentInteractionProfile"))
    (d / "harness.c").write_text(HARNESS.replace("@ADAPTER_FUNCTIONS@", functions))
    subprocess.run([CC, "-std=c11", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                    "-Wno-missing-field-initializers", "-pthread", "-I", str(INC),
                    "-I", str(ROOT / "native/adapter"), str(d / "harness.c"), "-lm", "-o", str(d / "harness")],
                   check=True)
    return d / "harness"


def run(harness, mode=1, commands="", *, offered=1, requested=False, diag=False, remap=False,
        controller_fix=False, create_result=0, proximity=0):
    p = subprocess.run([str(harness), str(mode), str(offered), str(int(requested)), str(int(diag)),
                        str(int(remap)), str(int(controller_fix)), str(create_result), str(proximity)],
                       input=commands, capture_output=True, text=True, timeout=5)
    assert p.returncode == 0, p.stderr
    return p


def suggest(bindings, profile=TOUCH, result=0, frame_result=0):
    return (f"suggest {profile} {len(bindings)} {result} {frame_result}\n" +
            "".join(f"{action} {path}\n" for action, path in bindings))


def calls(p):
    lines = iter(p.stdout.splitlines())
    out = []
    for line in lines:
        if line.startswith("CALL "):
            _, profile, count = line.split()
            bindings = []
            for _ in range(int(count)):
                action, path = next(lines).split()
                bindings.append((int(action), path))
            out.append((profile, bindings))
    return out


ONE_HAND_ROLES = {"input/x", "input/x/click", "input/x/touch", "input/y", "input/y/click", "input/y/touch",
                  "input/thumbstick", "input/thumbstick/x", "input/thumbstick/y"}


def one_hand_sources(mode):
    """FrameBridge's own bindings in a mode, in creation order (action handles 1001, 1002, ... in the harness)."""
    spec = CONTRACT["modes"][str(mode)]
    out = []
    for hand, buttons in spec.get("other_hand", {}).items():
        other = "right" if hand == "left" else "left"
        for _, frames in buttons.items():
            out += [f"/user/hand/{other}/input/{f}/{c}" for f in frames for c in ("click", "touch")]
    for hand, pads in spec.get("dpad_stick", {}).items():
        out += [f"/user/hand/{hand}/input/{pad}/click" for pad in pads]
    return out


def translate(bindings, mode):
    """Independent oracle: use only the shared JSON, never the C table."""
    if not mode:
        return []
    out = []
    spec = CONTRACT["modes"][str(mode)]
    extras = [(1001 + i, path) for i, path in enumerate(one_hand_sources(mode))]
    served = {f"/user/hand/{hand}/{role}" for hand in {*spec.get("other_hand", {}), *spec.get("dpad_stick", {})}
              for role in ONE_HAND_ROLES}
    for action, path in bindings:
        for hand, buttons in CONTRACT["buttons"].items():
            prefix = f"/user/hand/{hand}/"
            if not path.startswith(prefix):
                continue
            kind, _, component = path[len(prefix):].partition("/")
            targets = []
            if kind == "output":
                targets = CONTRACT["outputs"].get(component, [])
            elif kind == "input" and component not in CONTRACT["dropped"]:
                targets = CONTRACT["common"].get(component, [])
                button, _, suffix = component.partition("/")
                if ((button in buttons and suffix in ("", "click", "touch")) or
                        (button == "menu" and suffix in ("", "click"))):
                    targets = [f"{target}/{suffix}" if suffix else target for target in
                               CONTRACT["modes"][str(mode)][hand].get(button, [])]
            out.extend((action, f"{prefix}{kind}/{target}") for target in targets)
    if any(path in served for _, path in bindings):
        out.extend(extras)
    return out


def full_bindings():
    paths = []
    for hand, buttons in CONTRACT["buttons"].items():
        parts = [f"input/{p}" for p in CONTRACT["common"]]
        parts += [f"output/{p}" for p in CONTRACT["outputs"]]
        parts += [f"input/{button}/{component}" for button in buttons for component in ("click", "touch")]
        parts += [f"input/{button}" for button in buttons] + ["input/menu"]
        parts += [f"input/{p}" for p in CONTRACT["dropped"]]
        parts += ["input/menu/click", "input/menu/touch", "input/unknown/click", "input/trigger/click",
                  "input/squeeze/click", "input/thumb_fb/proximity_fb", "output/unknown", "input/a/value"]
        paths.extend(f"/user/hand/{hand}/{part}" for part in parts)
    paths += ["/user/head/input/aim/pose", "/user/hand/leftover/input/x/click"]
    return list(enumerate(paths, 1))


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_json_mapping_all_components(harness, mode):
    bindings = full_bindings()
    p = run(harness, mode, suggest(bindings), diag=True)
    expected = translate(bindings, mode)
    assert calls(p) == [(TOUCH, bindings)] + ([(FRAME, expected)] if mode else [])
    assert p.stdout.endswith("RESULT 0\n")
    logs = [line for line in p.stderr.splitlines() if line.startswith("input_diag: quest_buttons ")]
    if mode:
        assert len(logs) == len(expected)
        for action, target in expected:
            source = dict(bindings).get(action, "FrameBridge")
            assert f"input_diag: quest_buttons {source} -> {target}" in logs
        dropped = sum(not [t for t in translate([binding], mode) if t[0] < 1001] for binding in bindings)
        assert (f"quest_buttons: frame_controller bindings (mode {mode}, {len(expected)} bindings, "
                f"{dropped} dropped): 0") in p.stderr
    else:
        assert not logs and "quest_buttons:" not in p.stderr


@pytest.mark.parametrize("mode", [1, 2, 3])
def test_vrapi_bridge_shaped_bindings(harness, mode):
    # Same action handles shared between hands, as in native/vrapi-bridge/input.cpp.
    bindings = []
    for hand, buttons in CONTRACT["buttons"].items():
        common = ["input/grip/pose", "input/trigger/value", "input/squeeze/value", "input/thumbstick",
                  "input/thumbstick/click", "input/trigger/touch", "input/thumbstick/touch", "output/haptic"]
        bindings.extend((i, f"/user/hand/{hand}/{path}") for i, path in enumerate(common, 1))
        for i, button in enumerate(buttons):
            for offset, component in ((9, "click"), (11, "touch")):
                bindings.append((i + offset, f"/user/hand/{hand}/input/{button}/{component}"))
    bindings.append((13, "/user/hand/left/input/menu/click"))
    p = run(harness, mode, suggest(bindings))
    assert calls(p) == [(TOUCH, bindings), (FRAME, translate(bindings, mode))]


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_touch_pro_fallback_and_current_profile(harness, mode):
    bindings = [(1, "/user/hand/left/input/x/click"), (1, "/user/hand/right/input/a/touch"),
                (2, "/user/hand/left/input/thumb_fb/proximity_fb")]
    touch = bindings[:2]
    p = run(harness, mode, suggest(bindings, PRO) + f"profile {FRAME}\nprofile {TOUCH}\n", remap=True)
    assert calls(p) == [(PRO, bindings), (TOUCH, touch)] + ([(FRAME, translate(touch, mode))] if mode else [])
    assert "RESULT 0\n" in p.stdout
    profiles = [line for line in p.stdout.splitlines() if line.startswith("PROFILE ")]
    assert profiles == [f"PROFILE {PRO if mode else FRAME}", f"PROFILE {PRO}"]


@pytest.mark.parametrize("frame_result", [0, -2, -22])
def test_frame_result_never_changes_game_result_and_later_touch_replaces(harness, frame_result):
    first = [(4, "/user/hand/right/input/a/click")]
    later = [(8, "/user/hand/left/input/y/touch")]
    p = run(harness, commands=suggest(first, frame_result=frame_result) + suggest(later))
    assert calls(p) == [(TOUCH, first), (FRAME, translate(first, 1)),
                        (TOUCH, later), (FRAME, translate(later, 1))]
    assert p.stdout.count("RESULT 0\n") == 2
    assert f"0 dropped): {frame_result}" in p.stderr


def test_failed_touch_does_not_suggest_frame(harness):
    bindings = [(1, "/user/hand/right/input/a/click")]
    p = run(harness, commands=suggest(bindings, result=-22))
    assert calls(p) == [(TOUCH, bindings)]
    assert p.stdout.endswith("RESULT -22\n")
    assert "frame_controller bindings" not in p.stderr


def test_nonzero_touch_success_is_preserved(harness):
    bindings = [(1, "/user/hand/right/input/a/click")]
    p = run(harness, commands=suggest(bindings, result=1, frame_result=-2))
    assert calls(p) == [(TOUCH, bindings), (FRAME, translate(bindings, 1))]
    assert p.stdout.endswith("RESULT 1\n")
    assert "input_diag: quest_buttons" not in p.stderr


def test_touch_pro_success_ignores_frame_failure(harness):
    bindings = [(1, "/user/hand/left/input/x/click")]
    p = run(harness, commands=suggest(bindings, PRO, frame_result=-22), remap=True)
    assert calls(p) == [(PRO, bindings), (TOUCH, bindings), (FRAME, translate(bindings, 1))]
    assert p.stdout.endswith("RESULT 0\n")


@pytest.mark.parametrize("mode", [1, 2, 3])
@pytest.mark.parametrize("previous", [False, True])
def test_all_dropped_bindings_are_counted(harness, mode, previous):
    first = [(1, "/user/hand/right/input/a/click")]
    dropped = [(1, "/user/hand/left/input/thumbrest/touch")]
    commands = (suggest(first) if previous else "") + suggest(dropped) + f"profile {FRAME}\n"
    p = run(harness, mode, commands=commands)
    assert calls(p) == ([(TOUCH, first), (FRAME, translate(first, mode))] if previous else []) + [(TOUCH, dropped)]
    assert (f"quest_buttons: frame_controller bindings (mode {mode}, 0 bindings, 1 dropped): not suggested") in p.stderr
    assert p.stdout.endswith(f"PROFILE {TOUCH if previous else FRAME}\n")


@pytest.mark.parametrize("first", [False, True])
def test_games_own_frame_suggestion_wins(harness, first):
    bindings = [(1, "/user/hand/right/input/a/click")]
    own = [(2, "/user/hand/right/input/y/click")]
    commands = suggest(own, FRAME) if first else suggest(bindings)
    if not first:
        commands += suggest(own, FRAME)
    commands += suggest(bindings)
    p = run(harness, commands=commands)
    expected = [(FRAME, own), (TOUCH, bindings)] if first else [
        (TOUCH, bindings), (FRAME, translate(bindings, 1)), (FRAME, own), (TOUCH, bindings)]
    assert calls(p) == expected


def test_rejected_games_frame_binding_does_not_block_translation(harness):
    bindings = [(1, "/user/hand/right/input/a/click")]
    p = run(harness, commands=suggest(bindings, FRAME, frame_result=-22) + suggest(bindings))
    assert calls(p) == [(FRAME, bindings), (TOUCH, bindings), (FRAME, translate(bindings, 1))]


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
@pytest.mark.parametrize("offered", [0, 1, 2, 3, 4, 5])
@pytest.mark.parametrize("requested", [False, True])
def test_extension_enabled_only_when_offered_and_never_twice(harness, mode, offered, requested):
    bindings = [(1, "/user/hand/right/input/a/click")]
    p = run(harness, mode, suggest(bindings), offered=offered, requested=requested)
    enabled = bool(mode and offered == 1)
    assert f"ENABLED {int(enabled)}\n" in p.stdout
    exts = [line.removeprefix("EXT ") for line in p.stdout.splitlines() if line.startswith("EXT ")]
    # Existing pass-through policy retains app-requested extensions when enumeration is unavailable/fails.
    assert exts.count(CONTRACT["extension"]) == int(enabled or (requested and offered in (1, 2, 3, 4, 5)))
    assert calls(p) == [(TOUCH, bindings)] + ([(FRAME, translate(bindings, mode))] if enabled else [])
    if mode:
        message = "enabled" if enabled else "not offered by the runtime"
        assert f"quest_buttons: extension {CONTRACT['extension']} {message}" in p.stderr
    else:
        assert "quest_buttons:" not in p.stderr


@pytest.mark.parametrize("value", ["0", "1", "2", "3", "-1", "4", "1.5", "nan", "inf", "Mirror", "1junk", ""])
def test_setting_numbers_only_invalid_values_disable(harness, value):
    p = run(harness, value)
    valid = [str(n) for n in range(len(CONTRACT["modes"]))]
    expected = int(value) if value in valid else 0
    assert p.stdout.startswith(f"MODE {expected}\n")
    assert ("quest_buttons: invalid setting; using 0" in p.stderr) == (value not in valid)


@pytest.mark.parametrize("offered", [0, 1])
def test_current_profile_controller_fix_off(harness, offered):
    bindings = [(1, "/user/hand/right/input/a/click")]
    p = run(harness, commands=suggest(bindings) + f"profile {FRAME}\nprofile {TOUCH}\nprofile none\n"
            "profile /interaction_profiles/khr/generic_controller\n", offered=offered)
    profiles = [line.removeprefix("PROFILE ") for line in p.stdout.splitlines() if line.startswith("PROFILE ")]
    assert profiles == [TOUCH if offered else FRAME, TOUCH, "none", "/interaction_profiles/khr/generic_controller"]


def test_failed_instance_disables_feature(harness):
    bindings = [(1, "/user/hand/right/input/a/click")]
    p = run(harness, commands=suggest(bindings) + f"profile {FRAME}\n", create_result=-2)
    assert "ENABLED 0\n" in p.stdout
    assert calls(p) == [(TOUCH, bindings)]
    assert p.stdout.endswith(f"PROFILE {FRAME}\n")


def test_off_preserves_runtime_call_bytes(harness):
    bindings = [(9, "/user/hand/left/input/x/click"), (10, "/user/hand/right/output/haptic")]
    p = run(harness, 0, suggest(bindings) + suggest(bindings, PRO), remap=True)
    serialized = "".join(f"{a} {path}\n" for a, path in bindings)
    assert p.stdout == ("MODE 0\nENUM 0\nENUM 2\nCREATE 1\nEXT XR_KHR_vulkan_enable\nENABLED 0\n" +
                        f"CALL {TOUCH} 2\n{serialized}RESULT 0\n" +
                        f"CALL {PRO} 2\n{serialized}RESULT -22\n")
    # Without a game's own Touch suggestion, profile_remap still makes exactly its original two calls.
    p = run(harness, 0, suggest(bindings, PRO), remap=True)
    assert calls(p) == [(PRO, bindings), (TOUCH, bindings)]
    assert "quest_buttons:" not in p.stderr


@pytest.mark.parametrize("mode", [1, 2, 3])
@pytest.mark.parametrize("profile", [TOUCH, PRO])
def test_proximity_expansion_becomes_frame_bindings(harness, mode, profile):
    bindings = [(1, "/user/hand/left/input/thumb_fb/proximity_fb"),
                (2, "/user/hand/right/input/trigger/value")]
    # Pro's unsupported proximity path is filtered out during remap, as before; known action handles still expand.
    p = run(harness, mode, commands="proximity 1 3\n" + suggest(bindings, profile), proximity=2, remap=True)
    recorded = calls(p)
    touch = recorded[-2]
    assert touch[0] == TOUCH and len(touch[1]) > len(bindings)
    assert recorded[-1] == (FRAME, translate(touch[1], mode))
    frame_actions = {action for action, _ in recorded[-1][1]}
    assert {1, 2, 3} <= frame_actions


@pytest.mark.parametrize("mode", [1, 2, 3])
def test_bare_pose_parents_are_translated(harness, mode):
    bindings = [(i, f"/user/hand/{hand}/input/{pose}") for i, (hand, pose) in
                enumerate(((h, p) for h in ("left", "right") for p in ("grip", "aim")), 1)]
    assert all(CONTRACT["common"][pose] == [pose] for pose in ("grip", "aim"))
    p = run(harness, mode, suggest(bindings))
    assert calls(p) == [(TOUCH, bindings), (FRAME, translate(bindings, mode))]


@pytest.mark.parametrize("scenario,expected", [
    ("none", FRAME), ("rejected", FRAME), ("generated", TOUCH),
    ("own", FRAME), ("generated_then_own", FRAME), ("generated_then_rejected", TOUCH),
])
def test_profile_rewrite_requires_generated_binding(harness, scenario, expected):
    bindings = [(1, "/user/hand/right/input/a/click")]
    commands = ""
    if scenario.startswith("generated"):
        commands += suggest(bindings)
    elif scenario == "rejected":
        commands += suggest(bindings, frame_result=-22)
    if scenario.endswith("own"):
        commands += suggest(bindings, FRAME)
    elif scenario == "generated_then_rejected":
        commands += suggest(bindings, FRAME, frame_result=-22)
    p = run(harness, commands=commands + f"profile {FRAME}\n")
    assert p.stdout.endswith(f"PROFILE {expected}\n")


@pytest.mark.parametrize("controller_fix", [False, True])
def test_other_valve_profiles_keep_existing_controller_fix_behavior(harness, controller_fix):
    other = "/interaction_profiles/valve/index_controller"
    bindings = [(1, "/user/hand/right/input/a/click")]
    p = run(harness, commands=suggest(bindings) + f"profile {other}\n", controller_fix=controller_fix)
    assert p.stdout.endswith(f"PROFILE {TOUCH if controller_fix else other}\n")


@pytest.mark.parametrize("offered", [0, 1])
@pytest.mark.parametrize("owner", ["generated", "app"])
def test_failed_second_instance_preserves_live_binding_state(harness, offered, owner):
    bindings = [(1, "/user/hand/right/input/a/click")]
    commands = suggest(bindings, FRAME if owner == "app" else TOUCH)
    commands += f"create {offered} -2\nprofile {FRAME}\n" + suggest(bindings)
    p = run(harness, commands=commands)
    assert "ENABLED 1\nACTIVE 1\n" in p.stdout
    assert f"PROFILE {FRAME if owner == 'app' else TOUCH}\n" in p.stdout
    expected = [(FRAME, bindings), (TOUCH, bindings)] if owner == "app" else [
        (TOUCH, bindings), (FRAME, translate(bindings, 1)), (TOUCH, bindings), (FRAME, translate(bindings, 1))]
    assert calls(p) == expected


@pytest.mark.parametrize("owner", ["generated", "app"])
def test_successful_second_instance_resets_binding_ownership(harness, owner):
    bindings = [(1, "/user/hand/right/input/a/click")]
    commands = suggest(bindings, FRAME if owner == "app" else TOUCH)
    commands += f"create 1 0\nprofile {FRAME}\n" + suggest(bindings)
    p = run(harness, commands=commands)
    assert "ENABLED 1\nACTIVE 2\n" in p.stdout
    assert f"PROFILE {FRAME}\n" in p.stdout
    initial = [(FRAME, bindings)] if owner == "app" else [(TOUCH, bindings), (FRAME, translate(bindings, 1))]
    assert calls(p) == initial + [(TOUCH, bindings), (FRAME, translate(bindings, 1))]


@pytest.mark.parametrize("profile", [TOUCH, PRO])
def test_nested_proximity_fallback_does_not_deadlock_and_translates_accepted_set(harness, profile):
    bindings = [(1, "/user/hand/left/input/x/click")]
    commands = "proximity 3 4\nreject_touch_once -22\n" + suggest(bindings, profile) + suggest(bindings)
    p = run(harness, commands=commands, remap=True, proximity=2)
    recorded = calls(p)
    offset = int(profile == PRO)
    assert recorded[offset][0] == TOUCH and len(recorded[offset][1]) > len(bindings)
    assert recorded[offset + 1:offset + 3] == [(TOUCH, bindings), (FRAME, translate(bindings, 1))]
    # A second hook transaction also completes: all fallback paths released the mutex.
    assert recorded[-1] == (FRAME, translate(recorded[-2][1], 1))


def test_settings_and_hook_wiring():
    source = (ROOT / "native/adapter/frame_adapter.c").read_text()
    settings = source[source.index("static void read_settings("):source.index("static void initialize(")]
    assert "quest_buttons_setting(line);" in settings
    hook = source[source.index("if (layer_debug || aim_correction_on() || profile_remap"):]
    assert "|| quest_buttons)\n        HOOK_AS(xrSuggestInteractionProfileBindings" in hook


# ---------------------------------------------------------------- One hand (mode 3): answers from FrameBridge's set
LEFT, RIGHT = "/user/hand/left", "/user/hand/right"
# Unity-shaped: one action per button pair, read per hand through subaction paths
UNITY = [(5, f"{LEFT}/input/x/click"), (5, f"{RIGHT}/input/a/click"), (6, f"{LEFT}/input/y/click"),
         (6, f"{RIGHT}/input/b/click"), (7, f"{LEFT}/input/thumbstick"), (7, f"{RIGHT}/input/thumbstick"),
         (8, f"{LEFT}/input/thumbstick/x"), (9, f"{LEFT}/input/x/touch"), (10, f"{LEFT}/input/menu/click")]
SOURCES = {path: 1001 + i for i, path in enumerate(one_hand_sources(3))}


def states(p):
    return [line.removeprefix("STATE ") for line in p.stdout.splitlines() if line.startswith("STATE ")]


def test_one_hand_frame_suggestion_and_own_action_set(harness):
    p = run(harness, 3, suggest(UNITY) + "attach 1\nsync 1 100\n")
    out = p.stdout.splitlines()
    assert "ACTIONSET framebridge_one_hand" in out
    # FrameBridge's actions: boolean inputs without subaction paths, in its own set
    assert [line for line in out if line.startswith("ACTION ")] == [
        f"ACTION 900 {name} 1 0" for name in ("right_x_click", "right_x_touch", "right_y_click", "right_y_touch",
                                              "left_dpad_up", "left_dpad_down", "left_dpad_left", "left_dpad_right")]
    frame = calls(p)[-1]
    assert frame == (FRAME, translate(UNITY, 3))
    # left X/Y get no Frame binding of their own; right A/B stay on A/B; View is Menu
    assert (5, f"{RIGHT}/input/a/click") in frame[1] and (6, f"{RIGHT}/input/b/click") in frame[1]
    assert not [b for b in frame[1] if b[0] in (5, 6, 9) and b[1].startswith(LEFT)]
    assert (10, f"{LEFT}/input/view/click") in frame[1]
    assert "ATTACH 1 77 900" in out and "SYNC 1 77 900" in out
    assert "quest_buttons: one hand: FrameBridge's action set attached" in p.stderr


def test_one_hand_x_y_from_the_right_controller(harness):
    x, xt = SOURCES[f"{RIGHT}/input/x/click"], SOURCES[f"{RIGHT}/input/x/touch"]
    y = SOURCES[f"{RIGHT}/input/y/click"]
    commands = suggest(UNITY) + "attach 1\nstate 5 1 0 0\nstate 6 1 0 0\nstate 9 0 0 0\n"
    commands += "sync 1 100\n" + f"get bool 1 5 {LEFT}\n"                       # nothing pressed
    commands += f"state {x} 1 1 0\nstate {xt} 1 1 0\nsync 1 200\n"                # right X pressed
    commands += f"get bool 1 5 {LEFT}\nget bool 1 5 {RIGHT}\nget bool 1 5 none\nget bool 1 6 {LEFT}\n"
    commands += f"get bool 1 9 {LEFT}\nget float 1 5 {LEFT}\nget bool 2 5 {LEFT}\n"
    commands += f"sync 1 300\nget bool 1 5 {LEFT}\n"                              # held: not changed
    commands += f"state {x} 1 0 0\nstate {y} 1 1 0\nsync 1 400\nget bool 1 5 {LEFT}\nget bool 1 6 {LEFT}\n"
    commands += f"sync 1 500\nget bool 1 5 {LEFT}\n"
    p = run(harness, 3, commands)
    assert states(p) == [
        "0 1 0 0 5",
        "0 1 1 1 200",      # left X reads the right X, changed at this sync
        "0 1 0 0 5",        # the right hand's subaction (Quest A) is untouched
        "0 1 1 1 200",      # no subaction path: any hand
        "0 1 0 0 5",        # Y not pressed
        "0 1 1 1 200",      # x/touch from the right X's touch, active although the runtime said inactive
        "0 1 1.000 1 200",  # a float action on a button
        "0 1 0 0 5",        # another session: FrameBridge's set isn't attached there
        "0 1 1 0 200",      # still held
        "0 1 0 1 400",      # released: changed
        "0 1 1 1 400",      # Y pressed
        "0 1 0 0 5",        # a sync after the release: the runtime's own values again
    ]


def test_one_hand_dpad_moves_the_left_stick(harness):
    up, down = SOURCES[f"{LEFT}/input/dpad_up/click"], SOURCES[f"{LEFT}/input/dpad_down/click"]
    right = SOURCES[f"{LEFT}/input/dpad_right/click"]
    commands = suggest(UNITY) + "attach 1\nstate 7 1 0.25 -0.5\nstate 8 1 0.25 0\nsync 1 100\n"
    commands += f"get vec2 1 7 {LEFT}\n"
    commands += f"state {up} 1 1 0\nstate {right} 1 1 0\nsync 1 200\n"
    commands += f"get vec2 1 7 {LEFT}\nget float 1 8 {LEFT}\nget vec2 1 7 {RIGHT}\n"
    commands += f"state {right} 1 0 0\nstate {up} 1 0 0\nstate {down} 1 1 0\nsync 1 300\nget vec2 1 7 {LEFT}\n"
    commands += f"get float 1 8 {LEFT}\nstate {down} 1 0 0\nsync 1 400\nget vec2 1 7 {LEFT}\n"
    p = run(harness, 3, commands)
    assert states(p) == [
        "0 1 0.250,-0.500 0 5",    # the real stick
        "0 1 0.707,0.707 1 200",   # up + right: a full diagonal push
        "0 1 0.707 1 200",
        "0 1 0.250,-0.500 0 5",    # the right stick is never touched
        "0 1 0.000,-1.000 1 300",
        "0 1 0.000 1 300",
        "0 1 0.250,-0.500 1 400",  # released: the real stick again, reported as a change
    ]


@pytest.mark.parametrize("case", ["attach_fails", "set_refused", "no_left_roles", "game_frame_bindings"])
def test_one_hand_falls_back_to_plain_bindings(harness, case):
    bindings = UNITY if case != "no_left_roles" else [(5, f"{RIGHT}/input/a/click")]
    commands = {"attach_fails": "attach_result -1\n", "set_refused": "refuse_set\n"}.get(case, "")
    commands += suggest(bindings)
    if case == "game_frame_bindings":
        commands += suggest([(5, f"{RIGHT}/input/x/click")], FRAME)
    x = SOURCES[f"{RIGHT}/input/x/click"]
    commands += f"attach 1\nstate 5 1 0 0\nstate {x} 1 1 0\nsync 1 100\nget bool 1 5 {LEFT}\n"
    p = run(harness, 3, commands)
    out = p.stdout.splitlines()
    assert out[-1] == "STATE 0 1 0 0 5"   # the runtime's answer, unchanged
    assert "SYNC 1 77" in out
    if case == "attach_fails":
        assert out.count("ATTACH 1 77 900") == 1 and "ATTACH 1 77" in out
        assert "attaching FrameBridge's action set failed (-1)" in p.stderr
    else:
        assert "ATTACH 1 77" in out and "ATTACH 1 77 900" not in out
    if case in ("set_refused", "no_left_roles"):
        assert calls(p)[-1] == (FRAME, [b for b in translate(bindings, 3) if b[0] < 1001])


def test_other_modes_never_answer_for_the_game(harness):
    x = SOURCES[f"{RIGHT}/input/x/click"]
    commands = suggest(UNITY) + f"attach 1\nstate {x} 1 1 0\nsync 1 100\nget bool 1 5 {LEFT}\n"
    p = run(harness, 1, commands)
    assert "ACTIONSET" not in p.stdout
    assert "ATTACH 1 77\n" in p.stdout and "SYNC 1 77\n" in p.stdout
    assert states(p) == ["0 0 0 0 5"]


def test_one_hand_hook_wiring():
    source = (ROOT / "native/adapter/frame_adapter.c").read_text()
    hook = source[source.index("if (sync_guard || layer_debug || input_diag"):]
    assert hook.startswith("if (sync_guard || layer_debug || input_diag || quest_buttons == 3) HOOK_AS(xrSyncActions")
    block = hook[:hook.index("#undef HOOK_AS")]
    for name in ("xrAttachSessionActionSets", "xrGetActionStateFloat", "xrGetActionStateVector2f",
                 "xrGetActionStateBoolean"):
        assert f"HOOK_AS({name}, hook_{name})" in block
