"""FrameBridge fixes of November 2026: performance-level requests (#336/#400), plain RGBA8 formats (#400), per-eye
poses for games that submit the head pose twice (#527), detached controllers in xrSyncActions (#476), the haptics
summary (#567) and the OVRPlugin controller battery (#572).

The C parts are compiled for this host: the header-only helpers with any C compiler, the parts that need the OpenXR
headers only with FRAMEPORT_NATIVE_TESTS=1 (native/.cache from native/build.py), and the battery wrapper end to end
on Linux against a stand-in libOVRPlugin.so that went through the real ELF patch."""
import ctypes as C
import os
import platform
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

from frameport.analysis import elf
from frameport.apk.workspace import ApkWorkspace
from frameport.core.events import Reporter
from frameport.core.models import Analysis
from frameport.patches import base
from frameport.patches.frame import ovrp_battery
from frameport.patches.settings import UI, adapter_settings

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "native/adapter"
HOST_INCLUDE = ROOT / "tests/fixtures/src/host_include"
INC = next(iter((ROOT / "native/.cache").glob("openxr-*")), None)
CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
needs_cc = pytest.mark.skipif(not CC or os.name == "nt", reason="needs a POSIX host C compiler")
needs_openxr = [pytest.mark.native, pytest.mark.skipif(
    not os.environ.get("FRAMEPORT_NATIVE_TESTS") or not INC or not CC,
    reason="set FRAMEPORT_NATIVE_TESTS=1 (needs the OpenXR headers from native/build.py and a host C compiler)")]


def _run_c(tmp_path, source, *flags):
    src = tmp_path / "test.c"
    src.write_text(source)
    binary = tmp_path / "test"
    built = subprocess.run([CC, "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function", "-I",
                            str(ADAPTER), *flags, str(src), "-o", str(binary), "-lm", "-lpthread"],
                           capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    return run


HEADERS_HARNESS = r"""
#include <stdio.h>
#include "swapchain_formats.h"
#include "haptic_summary.h"
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAILED line %d: %s\n", __LINE__, #c); return 1; } } while (0)
int main(void) {
    // GLES: the runtime's list keeps its order (sRGB first), GL_RGBA8 comes after it, once
    int64_t gl[8] = {0x8C41, 0x8C43, 0x81A5};  // GL_SRGB8, GL_SRGB8_ALPHA8, depth
    CHECK(sf_append_unorm(gl, 3, 1) == 4 && gl[0] == 0x8C41 && gl[1] == 0x8C43 && gl[3] == 0x8058);
    CHECK(sf_append_unorm(gl, 4, 1) == 4);  // already listed: nothing added
    int64_t gl_depth[8] = {0x81A5};
    CHECK(sf_append_unorm(gl_depth, 1, 1) == 1);  // no sRGB colour format: no twin
    // Vulkan: R8G8B8A8_UNORM after R8G8B8A8_SRGB, B8G8R8A8_UNORM only if B8G8R8A8_SRGB is listed
    int64_t vk[8] = {43, 126};
    CHECK(sf_append_unorm(vk, 2, 0) == 3 && vk[0] == 43 && vk[2] == 37);
    int64_t vk2[8] = {50, 43};
    CHECK(sf_append_unorm(vk2, 2, 0) == 4 && vk2[2] == 37 && vk2[3] == 44);

    // haptics summary: nothing until a period has passed, then one line for the whole period, per hand
    HsState s = {0};
    char line[512];
    const long long S = 1000000000ll;
    CHECK(!hs_note(&s, HS_LEFT, HS_VIBRATION, 0.5f, 20000000, 1 * S, line, sizeof line));
    CHECK(!hs_note(&s, HS_LEFT, HS_VIBRATION, 0.2f, 5000000, 2 * S, line, sizeof line));
    CHECK(!hs_note(&s, HS_LEFT, HS_STOP, 0, 0, 3 * S, line, sizeof line));
    CHECK(!hs_note(&s, HS_RIGHT, HS_BUFFER, 0, 0, 4 * S, line, sizeof line));
    CHECK(hs_note(&s, HS_RIGHT, HS_VIBRATION, 1.0f, -1, 12 * S, line, sizeof line));
    printf("%s\n", line);
    CHECK(strstr(line, "4 requests this session"));
    CHECK(strstr(line, "left 2 vibration(s) amplitude 0.20-0.50 duration 5-20 ms, 1 stop(s), 0 buffer(s);"));
    CHECK(strstr(line, "right 0 vibration(s), 0 stop(s), 1 buffer(s);"));
    CHECK(!strstr(line, "other"));
    // the request that closed the period starts the next one: min duration (-1) is shown as such
    CHECK(hs_note(&s, HS_LEFT, HS_VIBRATION, 0.3f, 10000000, 30 * S, line, sizeof line));
    CHECK(strstr(line, "right 1 vibration(s) amplitude 1.00-1.00 duration min/0-0 ms"));
    CHECK(strstr(line, "6 requests") == NULL && strstr(line, "5 requests this session"));
    return 0;
}
"""


@needs_cc
def test_rgba8_formats_and_haptic_summary(tmp_path):
    out = _run_c(tmp_path, HEADERS_HARNESS)
    assert "haptic summary:" in out.stdout


OPENXR_HARNESS = r"""
#define XR_EXTENSION_PROTOTYPES
#include <openxr/openxr.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "eye_pose.h"
#define LOG(...) (fprintf(stderr, __VA_ARGS__), fputc('\n', stderr))
static XrInstance active_instance = (XrInstance)(uintptr_t)1;
static int profile_remap = 1, runtime_has_detached_controllers;
static const char *paths[] = {"", "/user/hand/left", "/user/hand/right", "/user/detached_controller_meta/left",
                              "/user/detached_controller_meta/right"};
static int path_lookups;
static XRAPI_ATTR XrResult XRAPI_CALL fake_path_to_string(XrInstance i, XrPath p, uint32_t cap, uint32_t *n, char *out) {
    (void)i;
    ++path_lookups;
    if (!p || p > 4) return XR_ERROR_PATH_INVALID;
    *n = (uint32_t)snprintf(out, cap, "%s", paths[p]) + 1;
    return XR_SUCCESS;
}
static PFN_xrVoidFunction lookup(XrInstance i, const char *name) {
    (void)i;
    return !strcmp(name, "xrPathToString") ? (PFN_xrVoidFunction)fake_path_to_string : NULL;
}
#include "detached_controllers.c"

// the runtime: refuses detached paths (mode 0) or any subaction path of set 1 (mode 1); records the last call
static int mode, syncs;
static XrActiveActionSet last[64];
static uint32_t last_count;
static XRAPI_ATTR XrResult XRAPI_CALL fake_sync(XrSession s, const XrActionsSyncInfo *info) {
    (void)s;
    ++syncs;
    last_count = info->countActiveActionSets;
    memcpy(last, info->activeActionSets, last_count * sizeof *last);
    for (uint32_t i = 0; i < last_count; ++i) {
        if (last[i].subactionPath >= 3) return XR_ERROR_PATH_UNSUPPORTED;
        if (mode == 1 && last[i].actionSet == (XrActionSet)(uintptr_t)1 && last[i].subactionPath)
            return XR_ERROR_PATH_UNSUPPORTED;
    }
    return XR_SUCCESS;
}

#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAILED line %d: %s\n", __LINE__, #c); return 1; } } while (0)
#define NEAR(a, b) (fabsf((a) - (b)) < 1e-4f)
int main(void) {
    // ---- eye_pose_fix: offsets from views in any space, applied to a layer with one pose for both eyes
    XrQuaternionf yaw = lm_axis_angle(0, 1, 0, 0.5f);
    XrPosef head = {yaw, {1, 1.6f, -2}};
    XrPosef eye_l = lm_pose_mul(head, (XrPosef){{0, 0, 0, 1}, {-0.032f, 0, 0}});
    XrPosef eye_r = lm_pose_mul(head, (XrPosef){{0, 0, 0, 1}, {0.032f, 0, 0}});
    XrPosef off[2];
    CHECK(ep_offsets(eye_l, eye_r, off));
    CHECK(NEAR(off[0].position.x, -0.032f) && NEAR(off[0].position.y, 0) && NEAR(off[0].position.z, 0));
    CHECK(NEAR(off[1].position.x, 0.032f) && NEAR(off[1].orientation.w, 1));
    // VIEW space answers (Lambda1VR locates its views relative to the head) give the same offsets
    XrPosef view_off[2];
    CHECK(ep_offsets((XrPosef){{0, 0, 0, 1}, {-0.032f, 0, 0}}, (XrPosef){{0, 0, 0, 1}, {0.032f, 0, 0}}, view_off));
    CHECK(NEAR(view_off[0].position.x, -0.032f) && NEAR(view_off[1].position.x, 0.032f));
    CHECK(!ep_offsets(eye_l, eye_l, view_off));  // no eye distance: nothing learnt
    XrCompositionLayerProjectionView v[2] = {{XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW}, {XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW}};
    v[0].pose = v[1].pose = head;
    v[0].fov = (XrFovf){-0.97f, 0.80f, 0.9f, -0.9f};
    v[1].fov = (XrFovf){-0.80f, 0.97f, 0.9f, -0.9f};
    CHECK(ep_rewrite(v, off));
    CHECK(NEAR(v[0].pose.position.x, eye_l.position.x) && NEAR(v[0].pose.position.z, eye_l.position.z));
    CHECK(NEAR(v[1].pose.position.x, eye_r.position.x) && NEAR(v[1].pose.orientation.y, yaw.y));
    CHECK(NEAR(v[0].fov.angleLeft, -0.97f) && NEAR(v[1].fov.angleRight, 0.97f));  // fov as submitted
    CHECK(!ep_rewrite(v, off));  // views with their own poses stay as they are

    // ---- detached controllers in xrSyncActions
    detached_sync_next = fake_sync;
    XrActiveActionSet sets[3] = {{(XrActionSet)(uintptr_t)1, 1}, {(XrActionSet)(uintptr_t)1, 3}, {(XrActionSet)(uintptr_t)2, 0}};
    XrActionsSyncInfo info = {XR_TYPE_ACTIONS_SYNC_INFO, NULL, 3, sets};
    CHECK(detached_hook_xrSyncActions(XR_NULL_HANDLE, &info) == XR_ERROR_PATH_UNSUPPORTED);  // nothing stripped yet
    CHECK(last_count == 3);
    XrPath subs[4] = {1, 2, 3, 4}, kept[4];
    XrActionCreateInfo action = {XR_TYPE_ACTION_CREATE_INFO, NULL, "thumbstick", XR_ACTION_TYPE_VECTOR2F_INPUT, 4, subs, "Thumbstick"};
    XrActionCreateInfo copy;
    CHECK(detached_strip_action(&action, &copy, kept) == &copy && copy.countSubactionPaths == 2);
    CHECK(detached_hook_xrSyncActions(XR_NULL_HANDLE, &info) == XR_SUCCESS);  // the detached entry is left out
    CHECK(last_count == 2 && last[0].subactionPath == 1 && last[1].actionSet == (XrActionSet)(uintptr_t)2);
    int lookups = path_lookups;
    CHECK(detached_hook_xrSyncActions(XR_NULL_HANDLE, &info) == XR_SUCCESS && path_lookups == lookups);  // cached
    // the runtime refuses a path for another reason: every set synced once for all its paths from then on
    mode = 1;
    CHECK(detached_hook_xrSyncActions(XR_NULL_HANDLE, &info) == XR_SUCCESS);
    CHECK(last_count == 2 && last[0].subactionPath == 0 && last[1].subactionPath == 0);
    int before = syncs;
    CHECK(detached_hook_xrSyncActions(XR_NULL_HANDLE, &info) == XR_SUCCESS && syncs == before + 1);
    // a runtime with the extension gets the game's sync unchanged
    runtime_has_detached_controllers = 1;
    mode = 0;
    CHECK(detached_hook_xrSyncActions(XR_NULL_HANDLE, &info) == XR_ERROR_PATH_UNSUPPORTED && last_count == 3);
    puts("ok");
    return 0;
}
"""


@pytest.mark.native
@pytest.mark.skipif(not os.environ.get("FRAMEPORT_NATIVE_TESTS") or not INC or not CC,
                    reason="set FRAMEPORT_NATIVE_TESTS=1 (needs the OpenXR headers from native/build.py and a host "
                           "C compiler)")
def test_eye_poses_and_detached_sync(tmp_path):
    out = _run_c(tmp_path, OPENXR_HARNESS, "-I", str(INC), "-Wno-missing-field-initializers")
    assert out.stdout.strip() == "ok"
    assert "xrSyncActions with every set for all its subaction paths: result 0 (kept from now on)" in out.stderr
    assert "subaction /user/hand/left" in out.stderr  # the refused sync's active sets are logged


WAIT_HARNESS = r"""
#define XR_EXTENSION_PROTOTYPES
#include <openxr/openxr.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#define LOG(...) (fprintf(stderr, __VA_ARGS__), fputc('\n', stderr))
static XrInstance active_instance = (XrInstance)(uintptr_t)1;
static int wait_balance = 1, frame_open;
static int begins, ends;
static XrTime end_time;
static uint32_t end_layers = 99;
static XRAPI_ATTR XrResult XRAPI_CALL fake_begin(XrSession s, const XrFrameBeginInfo *i) { (void)s; (void)i; ++begins; return XR_SUCCESS; }
static XRAPI_ATTR XrResult XRAPI_CALL fake_end(XrSession s, const XrFrameEndInfo *i) {
    (void)s; ++ends; end_time = i->displayTime; end_layers = i->layerCount; return XR_SUCCESS;
}
static PFN_xrVoidFunction lookup(XrInstance i, const char *name) {
    (void)i;
    if (!strcmp(name, "xrBeginFrame")) return (PFN_xrVoidFunction)fake_begin;
    if (!strcmp(name, "xrEndFrame")) return (PFN_xrVoidFunction)fake_end;
    return NULL;
}
#define WB_GRACE_NS 20000000ll
#include "wait_balance.c"
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAILED line %d: %s\n", __LINE__, #c); return 1; } } while (0)
static XrSession S = (XrSession)(uintptr_t)7;
static void *late_begin(void *arg) {  // another thread begins the waited frame within the grace time
    (void)arg;
    struct timespec t = {0, 5000000};
    nanosleep(&t, NULL);
    wb_after_begin(S, XR_SUCCESS);
    return NULL;
}
int main(void) {
    // pipelined engine: the next wait comes before the render thread began the last frame, which it then does
    wb_after_wait(S, XR_SUCCESS, 1000);
    pthread_t t;
    pthread_create(&t, NULL, late_begin, NULL);
    wb_before_wait(S);
    pthread_join(t, NULL);
    CHECK(begins == 0 && ends == 0);
    CHECK(!wb_begin_swallow(S));
    // FNaF's splash: a second wait on the thread that would begin the frame: closed without layers, then answered
    wb_after_wait(S, XR_SUCCESS, 2000);
    wb_before_wait(S);
    CHECK(begins == 1 && ends == 1 && end_time == 2000 && end_layers == 0);
    wb_after_wait(S, XR_SUCCESS, 3000);
    CHECK(!wb_begin_swallow(S));        // the next begin finds the frame waited just now
    wb_after_begin(S, XR_SUCCESS);
    CHECK(wb_begin_swallow(S));         // a further begin (the closed frame's late one) is answered here...
    CHECK(wb_end_swallow());            // ...and so is its end
    CHECK(!wb_end_swallow() && !wb_begin_swallow(S));
    // an open frame (begun, not ended) is frame_balance's business; the setting off changes nothing
    wb_after_wait(S, XR_SUCCESS, 4000);
    frame_open = 1;
    wb_before_wait(S);
    frame_open = 0;
    wait_balance = 0;
    wb_before_wait(S);
    CHECK(begins == 1 && ends == 1);
    puts("ok");
    return 0;
}
"""


@pytest.mark.native
@pytest.mark.skipif(not os.environ.get("FRAMEPORT_NATIVE_TESTS") or not INC or not CC,
                    reason="set FRAMEPORT_NATIVE_TESTS=1 (needs the OpenXR headers from native/build.py and a host "
                           "C compiler)")
def test_wait_balance_closes_a_frame_nobody_begins(tmp_path):
    out = _run_c(tmp_path, WAIT_HARNESS, "-I", str(INC), "-Wno-missing-field-initializers")
    assert out.stdout.strip() == "ok"
    assert out.stderr.count("wait_balance: xrWaitFrame while the frame waited before was never begun") == 1


def test_wait_balance_is_wired():
    wait = SRC.split("XRAPI_ATTR XrResult XRAPI_CALL xrWaitFrame(", 1)[1].split("\n}\n", 1)[0]
    assert wait.index("wb_before_wait(session)") < wait.index("fn(session, info, state)") < wait.index("wb_after_wait(")
    begin = SRC.split("XRAPI_ATTR XrResult XRAPI_CALL xrBeginFrame(", 1)[1].split("\n}\n", 1)[0]
    assert "if (wb_begin_swallow(session)) return XR_SUCCESS;" in begin and "wb_after_begin(session, result)" in begin
    end = SRC.split("XRAPI_ATTR XrResult XRAPI_CALL xrEndFrame(", 1)[1]
    assert end.index("wb_end_swallow()") < end.index("eye_pose_frame(info)")
    assert "static int wait_balance = 1;" in SRC


# ---------------------------------------------------------------- GL state guard (#441/#502)
FAKE_GLES = r"""
#include <string.h>
#define E __attribute__((visibility("default")))
E int unit = 0x84C0, tex[3][32], draw_fb, read_fb, program, vao, array_buffer, unpack_buffer, align = 4, real_binds;
static int slot(unsigned target) { return target == 0x0DE1 ? 0 : target == 0x8C1A ? 1 : 2; }
E void glActiveTexture(unsigned u) { unit = (int)u; }
E void glBindTexture(unsigned target, unsigned name) { tex[slot(target)][unit - 0x84C0] = (int)name; ++real_binds; }
E void glBindFramebuffer(unsigned target, unsigned name) {
    if (target == 0x8CA9 || target == 0x8D40) draw_fb = (int)name;
    if (target == 0x8CA8 || target == 0x8D40) read_fb = (int)name;
}
E void glUseProgram(unsigned p) { program = (int)p; }
E void glBindVertexArray(unsigned v) { vao = (int)v; }
E void glBindBuffer(unsigned target, unsigned name) { if (target == 0x8892) array_buffer = (int)name; else unpack_buffer = (int)name; }
E void glPixelStorei(unsigned pname, int v) { if (pname == 0x0CF5) align = v; }
E const unsigned char *glGetString(unsigned name) { return (const unsigned char *)(name == 0x1F03 ? "GL_OES_EGL_image_external GL_EXT_x" : "x"); }
E void glGetIntegerv(unsigned pname, int *out) {
    int u = unit - 0x84C0;
    switch (pname) {
    case 0x84E0: *out = unit; break;
    case 0x8069: *out = tex[0][u]; break;
    case 0x8C1D: *out = tex[1][u]; break;
    case 0x8D67: *out = tex[2][u]; break;
    case 0x8CA6: *out = draw_fb; break;
    case 0x8CAA: *out = read_fb; break;
    case 0x8B8D: *out = program; break;
    case 0x85B5: *out = vao; break;
    case 0x8894: *out = array_buffer; break;
    case 0x88EF: *out = unpack_buffer; break;
    case 0x0CF5: *out = align; break;
    default: *out = -99;
    }
}
"""
FAKE_EGL = r"""
__attribute__((visibility("default"))) void *context = (void *)0x1234;
__attribute__((visibility("default"))) void *eglGetCurrentContext(void) { return context; }
"""
GL_STATE_HARNESS = r"""
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
typedef int XrResult;
#define LOG(...) (fprintf(stderr, __VA_ARGS__), fputc('\n', stderr))
static int gl_state_guard = 1, gles_session = 1;
#include "gl_state.c"
extern int unit, tex[3][32], draw_fb, read_fb, program, vao, array_buffer, unpack_buffer, align, real_binds;
extern void *context;
// gl4es stand-in: the game's own GL entry point, which a global lookup (RTLD_DEFAULT) would find first
int gl4es_binds;
__attribute__((visibility("default"))) void glBindTexture(unsigned target, unsigned name) { (void)target; (void)name; ++gl4es_binds; }
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAILED line %d: %s\n", __LINE__, #c); return 1; } } while (0)
static XrResult runtime_end_frame(void) {  // SteamVR's interop: its own texture on unit 0, its FBO, program, alignment
    unit = 0x84C0; tex[0][0] = 99; draw_fb = read_fb = 7; program = 5; vao = 0; unpack_buffer = 0; align = 1;
    return 0;
}
int main(void) {
    // the app: unit 2 active with its cinematic texture, another one on unit 0, its own FBO/program/VAO/buffers
    unit = 0x84C2; tex[0][2] = 42; tex[2][2] = 8; tex[0][0] = 41; draw_fb = 3; read_fb = 4; program = 9; vao = 6;
    array_buffer = 11; unpack_buffer = 12; align = 2;
    XrResult r = GL_GUARDED("xrEndFrame", runtime_end_frame());
    CHECK(r == 0);
    CHECK(unit == 0x84C2 && tex[0][2] == 42 && tex[2][2] == 8 && tex[0][0] == 41);
    CHECK(draw_fb == 3 && read_fb == 4 && program == 9 && vao == 6 && array_buffer == 11 && unpack_buffer == 12);
    CHECK(align == 2);
    CHECK(gl4es_binds == 0 && real_binds > 0);  // the system's GL, never the game's own entry points
    // nothing changed: nothing rebound
    int binds = real_binds;
    CHECK(GL_GUARDED("xrBeginFrame", 0) == 0 && real_binds == binds);
    // no current context, or the setting off: nothing saved or restored
    context = NULL;
    CHECK(GL_GUARDED("xrEndFrame", runtime_end_frame()) == 0 && tex[0][0] == 99 && program == 5);
    context = (void *)0x1234;
    gl_state_guard = 0;
    program = 9;
    CHECK(GL_GUARDED("xrEndFrame", runtime_end_frame()) == 0 && program == 5);
    puts("ok");
    return 0;
}
"""


@pytest.mark.skipif(not CC or platform.system() != "Linux", reason="needs a C compiler on Linux")
def test_gl_state_guard_restores_the_apps_bindings(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    for name, src in (("libGLESv3.so", FAKE_GLES), ("libEGL.so", FAKE_EGL)):
        (tmp_path / f"{name}.c").write_text(src)
        subprocess.run([CC, "-shared", "-fPIC", f"-Wl,-soname,{name}", str(tmp_path / f"{name}.c"), "-o",
                        str(lib / name)], check=True)
    out = _run_c(tmp_path, GL_STATE_HARNESS, "-rdynamic", "-L", str(lib), f"-Wl,-rpath,{lib}",
                 "-Wl,--no-as-needed", "-l:libGLESv3.so", "-l:libEGL.so", "-ldl")
    assert out.stdout.strip() == "ok"
    assert "gl_state_guard: xrEndFrame changed" in out.stderr


def test_gl_state_guard_is_wired():
    base.load_all()
    assert base.REGISTRY["adapter.gl_state_guard"].default == 1
    assert 'sscanf(line, "gl_state_guard=%f", &value) == 1' in SRC and "static int gl_state_guard = 1;" in SRC
    for where in ("xrEndFrame", "xrBeginFrame", "xrCreateSwapchain", "xrAcquireSwapchainImage",
                  "xrWaitSwapchainImage", "xrReleaseSwapchainImage"):
        assert f'GL_GUARDED("{where}", ' in SRC, where
    end = SRC.split("XRAPI_ATTR XrResult XRAPI_CALL xrEndFrame(", 1)[1].split("\n}\n", 1)[0]
    assert "return fn(session, info);" not in end  # every runtime call in it is guarded
    clear = SRC.split("static void clear_gl_errors(const char *where) {", 1)[1].split("\n}\n", 1)[0]
    assert clear.index('dlopen("libGLESv3.so"') < clear.index("dlsym(RTLD_DEFAULT")  # the system's GL first


# ---------------------------------------------------------------- controller battery (#572)
FAKE_PLUGIN = r"""
#include <string.h>
typedef struct { unsigned int connected, buttons, touches, near; float f[12]; unsigned char battery[2], recenter[2];
                 unsigned char reserved[28]; } State4;
int calls;
static int fill(unsigned int mask, State4 *s) {
    ++calls;
    memset(s, 0, sizeof *s);
    s->connected = mask & 0x3;
    s->buttons = 0x100;
    s->battery[1] = 42;  // a real level stays
    return 0;
}
__attribute__((visibility("default"))) int ovrp_GetControllerState4(unsigned int m, State4 *s) { return fill(m, s); }
__attribute__((visibility("default"))) int ovrp_GetControllerState5(unsigned int m, State4 *s) { return fill(m, s); }
__attribute__((visibility("default"))) int ovrp_GetControllerState6(unsigned int m, State4 *s) { return fill(m, s); }
__attribute__((visibility("default"))) int ovrp_GetVersion(void) { return 7; }
"""


class State(C.Structure):
    _fields_ = [("connected", C.c_uint), ("buttons", C.c_uint), ("touches", C.c_uint), ("near", C.c_uint),
                ("f", C.c_float * 12), ("battery", C.c_ubyte * 2), ("recenter", C.c_ubyte * 2),
                ("reserved", C.c_ubyte * 28)]


def test_battery_exports_match_the_c_source():
    src = (ROOT / "native/ovrpbattery/ovrpbattery.c").read_text()
    assert tuple(f"ovrp_GetControllerState{n}" for n in re.findall(r"^WRAP\((\d)\)$", src, re.M)) == \
        ovrp_battery.EXPORTS
    assert "#define BATTERY_OFFSET 64" in src and C.sizeof(State) == 96  # ovrpControllerState4 is 96 bytes


@pytest.mark.skipif(not CC or platform.system() != "Linux", reason="needs a C compiler on Linux")
def test_battery_wrapper_through_the_patched_plugin(tmp_path):
    d = tmp_path / "lib"
    d.mkdir()
    (tmp_path / "plugin.c").write_text(FAKE_PLUGIN)
    common = ["-shared", "-fPIC", "-O1", "-Wall", "-Wextra", "-Werror"]
    subprocess.run([CC, *common, str(tmp_path / "plugin.c"), "-o", str(tmp_path / "plugin.so")], check=True)
    subprocess.run([CC, *common, "-fvisibility=hidden", "-I", str(HOST_INCLUDE), "-Wl,-soname,libfp_ovrpbattery.so",
                    str(ROOT / "native/ovrpbattery/ovrpbattery.c"), "-o", str(d / "libfp_ovrpbattery.so"), "-ldl"],
                   check=True)
    patched, hidden = elf.hide_exports((tmp_path / "plugin.so").read_bytes(), ovrp_battery.EXPORTS)
    assert hidden == sorted(ovrp_battery.EXPORTS)
    (d / "libOVRPlugin.so").write_bytes(elf.add_needed(patched, ovrp_battery.LIB, first=False))
    wrapper = C.CDLL(str(d / "libfp_ovrpbattery.so"))  # loaded first: the plugin's DT_NEEDED finds it by soname
    plugin = C.CDLL(str(d / "libOVRPlugin.so"))
    libdl = C.CDLL(None)
    libdl.dlsym.restype = C.c_void_p
    libdl.dlsym.argtypes = [C.c_void_p, C.c_char_p]
    for n in (4, 5, 6):  # a lookup on the plugin's handle (Unreal, Unity's P/Invoke) reaches the wrapper
        name = f"ovrp_GetControllerState{n}".encode()
        assert libdl.dlsym(plugin._handle, name) == libdl.dlsym(wrapper._handle, name)
    assert plugin.ovrp_GetVersion() == 7  # everything else is the plugin's own
    state = State()
    assert plugin.ovrp_GetControllerState4(0x3, C.byref(state)) == 0
    assert list(state.battery) == [100, 42] and state.buttons == 0x100
    assert C.c_int.in_dll(plugin, "calls").value == 1  # the plugin's original was called once
    state = State()
    assert plugin.ovrp_GetControllerState6(0x2, C.byref(state)) == 0
    assert list(state.battery) == [0, 42]  # no left controller asked for: its level stays 0


def _analysis(**kw):
    d = dict(package="com.example.questgame", version="1.0", label="Quest Game", abis=["arm64-v8a"], engine="Unreal",
             xr="OpenXR", graphics="Vulkan (declared in manifest)", direct_vrapi=False, libs=["libOVRPlugin.so"],
             launcher_activity=None, has_info_category=True, meta_permissions=[], uses_glad_gl=False,
             unity_msaa_levels=0, oculus_os_classes=False, is_overport_output=True, debuggable=True, extra={"size": 1})
    d.update(kw)
    return Analysis(**d)


@pytest.mark.skipif(not CC or os.name == "nt", reason="needs a host C compiler")
def test_battery_patch_links_the_wrapper(tmp_path, monkeypatch):
    (tmp_path / "plugin.c").write_text(FAKE_PLUGIN)
    subprocess.run([CC, "-shared", "-fPIC", "-O1", str(tmp_path / "plugin.c"), "-o", str(tmp_path / "plugin.so")],
                   check=True)
    apk = tmp_path / "in.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("AndroidManifest.xml", b"manifest")
        z.writestr("classes.dex", b"dex\n035\0")
        z.writestr("lib/arm64-v8a/libOVRPlugin.so", (tmp_path / "plugin.so").read_bytes())
    monkeypatch.setattr(ovrp_battery, "artifact", lambda abi, name: b"\x7fELF-battery")
    base.load_all()
    patch = base.get("frame.ovrp_controller_battery")
    a = _analysis()
    assert patch.applies(a) and not patch.applies(_analysis(libs=[]))
    assert patch.detect(a) is None and not patch.default_on  # opt-in: cosmetic, and it changes libOVRPlugin.so
    with ApkWorkspace(apk) as ws:
        ctx = base.ApkContext(ws, a, {}, Reporter(), {})
        assert not all(ok for _, ok, _ in patch.validate(ctx))
        assert patch.apply(ctx)
        plugin = ws.read_lib("libOVRPlugin.so")
        assert elf.needed(plugin)[-1] == ovrp_battery.LIB
        assert ws.read_lib(ovrp_battery.LIB) == b"\x7fELF-battery"
        assert all(ok for _, ok, _ in patch.validate(ctx))
        assert not patch.apply(base.ApkContext(ws, a, {}, Reporter(), {}))  # applied once is enough


# ---------------------------------------------------------------- settings, wiring, catalog, triage
SRC = (ADAPTER / "frame_adapter.c").read_text()


def test_new_settings_are_registered_with_their_defaults():
    base.load_all()
    assert base.REGISTRY["adapter.perf_settings_emul"].default == 1  # harmless: on for every game
    assert base.REGISTRY["adapter.unorm_formats"].default == 0
    assert base.REGISTRY["adapter.eye_pose_fix"].default == 0
    assert base.REGISTRY["adapter.wait_balance"].default == 1  # only acts in a state that would otherwise hang
    for key in ("perf_settings_emul", "unorm_formats", "eye_pose_fix", "wait_balance"):
        assert UI[key]["control"] == ("switch",)
        assert f'sscanf(line, "{key}=%f", &value) == 1' in SRC
        assert base.REGISTRY[f"adapter.{key}"].revision == 1  # read at run time from settings.conf: no rebuild
    assert adapter_settings({"adapter.eye_pose_fix": {"value": 1}})["eye_pose_fix"] == 1
    assert "static int perf_settings_emul = 1;" in SRC


def test_hooks_are_wired():
    assert "if (perf_settings_emul) HOOK_AS(xrPerfSettingsSetPerformanceLevelEXT, hook_perf_settings_level)" in SRC
    assert "if (unorm_formats && swapchain_fix) HOOK(xrEnumerateSwapchainFormats)" in SRC
    assert "info = eye_pose_frame(info);" in SRC and "eye_pose_note_views(state" in SRC
    assert "detached_sync_next = (PFN_xrSyncActions)inner;" in SRC
    assert "haptic_summary_note(info, feedback);" in SRC and "hook_stop_haptics" in SRC


def test_lambda1vr_catalog_turns_the_eye_fix_on():
    entry = yaml.safe_load((ROOT / "catalog/games/com.drbeef.lambda1vr.yaml").read_text())
    assert entry["adapter"]["eye_pose_fix"] == 1


TRIAGE_LINES = {
    "gl-upload-into-runtime-texture": [
        "11-09 00:27:33.665  1161  1201 E Mesa    : GL_INVALID_VALUE in glTexSubImage2D(xoffset 0 + width 512 > 16)"],
    "wait-frame-stale": [
        "11-08 23:27:49.680  1006  1153 I OVRPlugin: Warning : CompositorOpenXR::Update called for frame 7 outside of "
        "frame bounds"],
    "perf-settings-unsupported": [
        "10-26 01:17:42.116  1259  1310 E OutRunQuest: CPU performance: -7",
        "E flycast : rend/vr/xr_host.cpp:181 E[RENDERER]: XR: setPerformanceLevel(session, "
        "XR_PERF_SETTINGS_DOMAIN_CPU_EXT, XR_PERF_SETTINGS_LEVEL_SUSTAINED_HIGH_EXT) failed: -7 "
        "XR_ERROR_FUNCTION_UNSUPPORTED"],
    "no-rgba8-swapchain-format": [
        "E flycast : 00:02:450 rend/vr/xr_host.cpp:368 E[RENDERER]: XR: no RGBA8 swapchain format"],
    "projection-views-same-pose": [
        "I FrameBridge: projection view 0: space=0xfffe8400ca20 swapchain=0xfffe8428c680 rect=0,0 2246x2246 array=0 "
        "pos=-0.390,1.091,-1.570 fov=-0.97/0.94\n"
        "I FrameBridge: projection view 1: space=0xfffe8400ca20 swapchain=0xfffe7bbf6480 rect=0,0 2246x2246 array=0 "
        "pos=-0.390,1.091,-1.570 fov=-0.97/0.94"],
    "sync-actions-path-unsupported": [
        "E OVRPlugin: [XRCMD][failure] [XR_ERROR_PATH_UNSUPPORTED]: xrSyncActions(session, &newSyncInfo), "
        "arvr\\projects\\integrations\\OVRPlugin\\Src\\Util\\CompositorOpenXR.cpp:17853"],
}
NOT_MATCHED = {
    "perf-settings-unsupported": ["I AudioStreamBuilder: device = 0, sessionId = -1, perfMode = 12, callback: ON",
                                  "I FrameBridge: perf_settings: CPU level 3 accepted without effect"],
    "projection-views-same-pose": [
        "I FrameBridge: projection view 0: space=0x1 swapchain=0x2 rect=0,0 1x1 array=0 pos=-0.032,1.600,0.000 fov=a\n"
        "I FrameBridge: projection view 1: space=0x1 swapchain=0x2 rect=0,0 1x1 array=1 pos=0.032,1.600,0.000 fov=a"],
}


def test_triage_signatures():
    sigs = {s["id"]: s for s in yaml.safe_load((ROOT / "catalog/triage.yaml").read_text())["signatures"]}
    for sid, lines in TRIAGE_LINES.items():
        for line in lines:
            assert re.search(sigs[sid]["pattern"], line), (sid, line)
    for sid, lines in NOT_MATCHED.items():
        for line in lines:
            assert not re.search(sigs[sid]["pattern"], line), (sid, line)
    assert sigs["perf-settings-unsupported"]["suggest"] == ["adapter.perf_settings_emul"]
    assert sigs["no-rgba8-swapchain-format"]["suggest"] == ["adapter.unorm_formats"]
    assert sigs["projection-views-same-pose"]["suggest"] == ["adapter.eye_pose_fix"]
