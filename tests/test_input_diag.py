"""FrameBridge's controller-input diagnostics (native/adapter/input_diag.c), compiled for this host with a small C
harness that stands in for the adapter (lookup, path strings, result names, logging) and for the runtime.
Opt-in (FRAMEPORT_NATIVE_TESTS=1, -m native): needs the OpenXR headers in native/.cache (from native/build.py)."""
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

HARNESS = r"""
#define XR_EXTENSION_PROTOTYPES
#include <openxr/openxr.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define LOG(...) (fprintf(stderr, __VA_ARGS__), fputc('\n', stderr))
static XrInstance active_instance = (XrInstance)(uintptr_t)1;
static int input_diag;
static char paths[16][XR_MAX_PATH_LENGTH];
static int path_count;
static XRAPI_ATTR XrResult XRAPI_CALL fake_string_to_path(XrInstance i, const char *s, XrPath *out) {
    (void)i;
    for (int k = 0; k < path_count; ++k) if (!strcmp(paths[k], s)) { *out = (XrPath)(k + 1); return XR_SUCCESS; }
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
static XRAPI_ATTR XrResult XRAPI_CALL fake_result_to_string(XrInstance i, XrResult r, char *out) {
    (void)i;
    const char *name = r == XR_ERROR_PATH_UNSUPPORTED ? "XR_ERROR_PATH_UNSUPPORTED"
                     : r == XR_ERROR_NAME_INVALID ? "XR_ERROR_NAME_INVALID"
                     : r == XR_ERROR_FUNCTION_UNSUPPORTED ? "XR_ERROR_FUNCTION_UNSUPPORTED" : NULL;
    if (!name) return XR_ERROR_VALIDATION_FAILURE;
    snprintf(out, XR_MAX_RESULT_STRING_SIZE, "%s", name);
    return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL fake_create_action(XrActionSet set, const XrActionCreateInfo *info,
                                                         XrAction *action) {
    (void)set; *action = XR_NULL_HANDLE;
    return strchr(info->actionName, ' ') ? XR_ERROR_NAME_INVALID : XR_SUCCESS;
}
static PFN_xrVoidFunction lookup(XrInstance i, const char *name) {
    (void)i;
    if (!strcmp(name, "xrPathToString")) return (PFN_xrVoidFunction)fake_path_to_string;
    if (!strcmp(name, "xrResultToString")) return (PFN_xrVoidFunction)fake_result_to_string;
    if (!strcmp(name, "xrCreateAction")) return (PFN_xrVoidFunction)fake_create_action;
    return NULL;
}
#include "input_diag.c"
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAILED line %d: %s\n", __LINE__, #c); return 1; } } while (0)
int main(int argc, char **argv) {
    (void)argv;
    if (argc > 1) {  // more distinct findings than DIAG_MAX: the rest is dropped, with one line saying so
        input_diag = 1;
        char name[32];
        for (int i = 0; i < 600; ++i) {
            snprintf(name, sizeof(name), "xrMissing%d", i);
            input_diag_lookup(active_instance, name, XR_ERROR_FUNCTION_UNSUPPORTED, NULL);
            input_diag_lookup(active_instance, name, XR_ERROR_FUNCTION_UNSUPPORTED, NULL);
        }
        return 0;
    }
    XrPath touch, plus, left, right, proximity, a;
    fake_string_to_path(0, "/interaction_profiles/oculus/touch_controller", &touch);
    fake_string_to_path(0, "/interaction_profiles/meta/touch_controller_plus", &plus);
    fake_string_to_path(0, "/user/hand/left", &left);
    fake_string_to_path(0, "/user/hand/right", &right);
    fake_string_to_path(0, "/user/hand/left/input/thumb_resting_surfaces/proximity", &proximity);
    fake_string_to_path(0, "/user/hand/right/input/a/click", &a);
    XrActionSuggestedBinding bindings[2] = {{XR_NULL_HANDLE, proximity}, {XR_NULL_HANDLE, a}};
    XrInteractionProfileSuggestedBinding s = {XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING, NULL, plus, 2, bindings};

    input_diag_suggested(active_instance, &s, XR_ERROR_PATH_UNSUPPORTED, fake_path_to_string);  // off: nothing
    input_diag_lookup(active_instance, "xrCreateBodyTrackerFB", XR_ERROR_FUNCTION_UNSUPPORTED, NULL);
    input_diag = 1;
    input_diag_suggested(active_instance, &s, XR_ERROR_PATH_UNSUPPORTED, fake_path_to_string);
    input_diag_suggested(active_instance, &s, XR_ERROR_PATH_UNSUPPORTED, fake_path_to_string);  // once
    s.interactionProfile = touch;
    input_diag_suggested(active_instance, &s, XR_SUCCESS, fake_path_to_string);
    input_diag_current_profile(left, touch);
    input_diag_current_profile(left, touch);  // once
    input_diag_current_profile(right, XR_NULL_PATH);

    PFN_xrCreateAction create = (PFN_xrCreateAction)input_diag_hook("xrCreateAction");
    CHECK(create && input_diag_hook("xrStringToPath") && !input_diag_hook("xrEndFrame"));
    XrActionCreateInfo info = {XR_TYPE_ACTION_CREATE_INFO};
    XrAction action;
    snprintf(info.actionName, sizeof(info.actionName), "trigger pull");
    CHECK(create(XR_NULL_HANDLE, &info, &action) == XR_ERROR_NAME_INVALID);
    CHECK(create(XR_NULL_HANDLE, &info, &action) == XR_ERROR_NAME_INVALID);  // once
    snprintf(info.actionName, sizeof(info.actionName), "trigger_pull");
    CHECK(create(XR_NULL_HANDLE, &info, &action) == XR_SUCCESS);  // success: nothing

    input_diag_lookup(active_instance, "xrCreateBodyTrackerFB", XR_ERROR_FUNCTION_UNSUPPORTED, NULL);
    input_diag_lookup(active_instance, "xrCreateBodyTrackerFB", XR_ERROR_FUNCTION_UNSUPPORTED, NULL);  // once
    input_diag_lookup(XR_NULL_HANDLE, "xrCreateInstance", XR_ERROR_FUNCTION_UNSUPPORTED, NULL);  // no instance yet
    input_diag_lookup(active_instance, "xrSyncActions", XR_SUCCESS, (PFN_xrVoidFunction)fake_create_action);
    return 0;
}
"""

EXPECTED = [
    "input_diag: unsupported: interaction profile /interaction_profiles/meta/touch_controller_plus -> "
    "XR_ERROR_PATH_UNSUPPORTED (2 bindings):",
    "input_diag:   /user/hand/left/input/thumb_resting_surfaces/proximity",
    "input_diag:   /user/hand/right/input/a/click",
    "input_diag: bindings: /interaction_profiles/oculus/touch_controller accepted (2 paths)",
    "input_diag: bindings: /user/hand/left uses /interaction_profiles/oculus/touch_controller",
    "input_diag: bindings: /user/hand/right uses none",
    "input_diag: unsupported: xrCreateAction -> XR_ERROR_NAME_INVALID (trigger pull)",
    "input_diag: unsupported: function xrCreateBodyTrackerFB not provided by the runtime "
    "(XR_ERROR_FUNCTION_UNSUPPORTED)",
]


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("input_diag")
    (d / "harness.c").write_text(HARNESS)
    subprocess.run([CC, "-std=c11", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                    "-Wno-missing-field-initializers", "-pthread", "-I", str(INC),
                    "-I", str(ROOT / "native/adapter"),
                    str(d / "harness.c"), "-o", str(d / "harness")], check=True)
    return d / "harness"


def test_logs_each_finding_once_and_nothing_when_off(harness):
    p = subprocess.run([str(harness)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert p.stderr.splitlines() == EXPECTED


def test_stops_after_512_distinct_findings(harness):
    p = subprocess.run([str(harness), "flood"], capture_output=True, text=True)
    lines = p.stderr.splitlines()
    assert p.returncode == 0 and len(lines) == 513
    assert lines[511].startswith("input_diag: unsupported: function xrMissing511 ")
    assert lines[512] == "input_diag: 512 distinct findings, logging no more"
