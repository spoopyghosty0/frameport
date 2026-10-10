// SPDX-License-Identifier: GPL-3.0-only
// Steam Frame OpenXR adapter for overport-patched Quest games.
//
// Installed as lib/arm64-v8a/libopenxr_loader_generic.so; overport's real generic
// loader is renamed to libopenxr_loader_original.so and loaded from the same dir.
//
// Fixes applied:
//  * adds XR_KHR_android_create_instance when the app chains
//    XrInstanceCreateInfoAndroidKHR but forgets to enable the extension;
//  * hides foveation extensions and strips XrSwapchainCreateInfoFoveationFB
//    (foveation_fix);
//  * reports Valve / generic controller profiles as Oculus Touch and suppresses
//    synthetic hand-tracking data (controller_fix);
//  * scales the recommended eye-buffer size (scale);
//  * drops requested instance extensions the runtime lacks (e.g. XR_FB_passthrough);
//  * retries rejected swapchains with 1 sample / a supported format (swapchain_fix, overport #71);
//  * drops composition layers that reference swapchains that failed to create (layer_fix);
//  * serves cube swapchains the runtime refuses as GL cube maps and drops their layers (cube_standin, cube_standin.c);
//  * optionally requests mutable-format swapchain images (mutable_fix, off by default);
//  * optionally swaps left/right images of stereo projection layers (swap_eyes, off by default);
//  * emulates XR_FB_passthrough with XR_ENVIRONMENT_BLEND_MODE_ALPHA_BLEND when the runtime lacks it
//    (passthrough_emul);
//  * optionally tells the app its reference spaces changed once the session is focused, so apps that
//    created them before tracking started recreate them (respace_kick);
//  * optionally flips quad layers vertically for apps whose UI panels show upside down (flip_quads);
//  * optionally serves Steam Frame controller models through XR_FB_render_model (controller_models,
//    render_model.c);
//  * per-game session fixes (session_fixes.c, all off by default): layer_debug diagnostics, stable_local,
//    focus_hold, aim pose correction (aim_pitch/aim_yaw/aim_forward), refresh_rate;
//  * optionally logs controller-input diagnostics: suggested interaction profiles and the runtime's answers, each
//    hand's current profile, functions the runtime lacks, failing input/haptics/perf calls (input_diag,
//    input_diag.c, off by default);
//  * optionally shows 360° (equirect/equirect2) layers as cube-face quads drawn by a worker thread with a shared
//    GLES context (equirect_emul, layer_emul_gl.c; GLES sessions only, off by default).
//
// Settings (key=value lines), later sources override earlier ones:
//   <libdir>/libframe_settings.so
//   /sdcard/Android/data/<package>/files/framebridge.conf
//   $FRAMEBRIDGE_CONFIG
#define _GNU_SOURCE
#define VK_USE_PLATFORM_ANDROID_KHR
#define XR_EXTENSION_PROTOTYPES
#include <openxr/openxr.h>
#include <android/log.h>
#include <dlfcn.h>
#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TAG "FrameBridge"
#include <stdarg.h>
#include <time.h>
// Everything also goes to <app external files>/framebridge.log when eye_debug is on: some games end Lepton's logcat
// mirror early (WiiCompiled), so a headset session's diagnostics would otherwise be lost.
static FILE *log_file;
static void fb_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void fb_log(const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    __android_log_vprint(ANDROID_LOG_INFO, TAG, fmt, args);
    va_end(args);
    if (!log_file) return;
    struct timespec now;
    clock_gettime(CLOCK_REALTIME, &now);
    flockfile(log_file);
    fprintf(log_file, "%lld.%03ld ", (long long)now.tv_sec, now.tv_nsec / 1000000);
    va_start(args, fmt);
    vfprintf(log_file, fmt, args);
    va_end(args);
    fputc('\n', log_file);
    fflush(log_file);
    funlockfile(log_file);
}
#define LOG(...) fb_log(__VA_ARGS__)
#include "audio_metadata.h"
#include "surface_views.h"

static void *loader;
static PFN_xrGetInstanceProcAddr next_gipa;
static pthread_once_t init_once = PTHREAD_ONCE_INIT;
static XrInstance active_instance = XR_NULL_HANDLE;
static void *android_vm;     // JavaVM from XrInstanceCreateInfoAndroidKHR (surface_swapchain.c)
static int surface_emul = 1;  // setting: emulate Android surface swapchains
static int surface_native;   // internal: GPU shared video buffers (panoramic video clients)
static int eye_debug;          // setting: per-eye diagnostics + file log (eye_debug.c)
static int release_wait;       // setting: wait for the app's GPU work before releasing images (2 = after 60 s: A/B)
static void surf_scene_destroy(XrSwapchain handle);
static void surf_on_destroy(XrSwapchain handle);

static float scale = 1.0f;
static int foveation_fix = 1;
// hide XR_FB_space_warp: games then render every frame themselves (UE5 Application SpaceWarp flickered on the Frame,
// e.g. Into The Radius 2; OVRPort's patch_disable_space_warp doesn't reach UE5's OpenXR plugin)
static int hide_space_warp = 0;
static int strip_color_bias = 0;
static int snapshot = 0;  // seconds between eye-image snapshots (snapshot_gl.c; 0 = off)
static char snap_dir[256];
static void snap_on_create(XrSwapchain handle, const XrSwapchainCreateInfo *info);
static void snap_on_destroy(XrSwapchain handle);
static void snap_on_enumerate(XrSwapchain handle, uint32_t count, const XrSwapchainImageBaseHeader *images);
static void snap_on_acquire(XrSwapchain handle, uint32_t index);
static void snap_end_frame(const XrFrameEndInfo *info);
static int frame_balance = 0;  // end a still-open frame before the next xrBeginFrame
static int frame_begins, frame_discarded, frame_ends, frame_balanced;  // per pacing period (layer_debug)
static int frame_open;
static int sc_acquires, sc_waits, sc_releases, sc_wait_fails;  // per pacing period (layer_debug)  // drop XrCompositionLayerColorScaleBiasKHR (the runtime's color pass)
static int controller_fix = 1;
static int swapchain_fix = 1;
static int cube_standin = 1;  // cube swapchains the runtime refuses: served by the adapter (cube_standin.c)
static int rect_clamp = 1;
static int pose_consistency = 0;  // repeat xrLocateViews queries for one display time get the same poses (GitHub #8)  // clamp submitted image rects to their swapchain (SteamVR rejects a 1 px overrun)
static int pose_time_fix;   // poses asked for at CLOCK_MONOTONIC "now" or far in the past: at XrTime "now" (pose_time.c)
static int pose_debug;      // diagnostics: requested pose times vs the predicted display time (pose_time.c)
static int layer_fix = 1;
static int mutable_fix = 0;
static int swap_eyes = 0;
static int passthrough_emul = 1;
static int respace_kick = 0;
static int strip_depth = 0;
static int flip_quads = 0;
static int flip_emul_setting = 1;
static int cylinder_strips_on = 1;  // show cylinder layers as flat quad strips (cylinder_strips)
static int scene_emul;  // scene emulation setting (per game)
static float scene_height = 2.5f;  // scene emulation ceiling height (m)
static float scene_width, scene_depth;  // optional room size override (m)
static int64_t xr_time_offset;  // XrTime minus CLOCK_MONOTONIC ns, measured at xrWaitFrame
static int xr_time_calibrated;
static int runtime_has_image_layout;  // runtime supports XR_FB_composition_layer_image_layout natively
// Layer types whose extension the runtime lacks (dropped at xrCreateInstance) are removed from frames.
static int no_equirect, no_equirect2, no_cylinder, no_cube;
static int runtime_has_fb_passthrough, emulate_passthrough, alpha_blend_failed;
static int controller_models;  // serve Frame controller models via XR_FB_render_model (render_model.c)
// Per-game settings, all off by default (session_fixes.c, layer_emul_gl.c).
static int sync_guard;       // xrSyncActions one at a time with xrPollEvent, paused briefly after focus returns
static int profile_remap = 1; // Meta's newer controller profiles (rejected by the Frame) -> oculus/touch_controller
static int proximity_emul;   // finger proximity from capacitive touch: 1 thumb, 2 thumb + index (session_fixes.c)
static int runtime_has_proximity;  // the runtime has XR_FB_touch_controller_proximity itself
static int layer_debug;      // diagnostics: layers, swapchains, session states, spaces, aim/grip, refresh rates
static int input_diag;       // diagnostics: controller profiles, bindings, missing functions, failing input calls
static int stable_local;     // keep every LOCAL space the app creates on the session-start origin
static int focus_hold = 1;   // hide brief focus dips once the session has been focused for a while
static float focus_hold_ms = 5000;  // longest focus dip focus_hold hides
static float haptic_scale = 1.0f;   // vibration strength (0-1), all vibration types (xrApplyHapticFeedback)
static float aim_pitch, aim_yaw, aim_forward;  // aim pose correction (degrees, degrees, metres)
static float refresh_rate;   // requested display refresh rate (Hz), 0 = the app's choice
static int equirect_emul;    // show 360 layers as cube faces (GLES)
static int equirect_face = 1536;  // max cube map face size (px)
static int equirect_res = 1536, equirect_res_set;   // projection image size per eye (px)
static int equirect_flip;    // source orientation fix: 1 upside down, 2 mirrored, 4 turned 180 degrees
static float equirect_fps = 60;   // max redraws per second per 360 layer (0 = no limit)
static int equirect_stereo;  // 0 auto (mono if the layer limit can't hold both eyes), 1 stereo, 2 mono
static XrTime last_predicted_time;
static void render_model_init(const char *package);

static void read_settings(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[256];
    float value;
    while (fgets(line, sizeof(line), f)) {
        if (sscanf(line, "scale=%f", &value) == 1 && value >= 0.5f && value <= 2.0f) scale = value;
        if (sscanf(line, "foveation_fix=%f", &value) == 1) foveation_fix = value != 0;
        if (sscanf(line, "hide_space_warp=%f", &value) == 1) hide_space_warp = value != 0;
        if (sscanf(line, "strip_color_bias=%f", &value) == 1) strip_color_bias = (int)value;
        if (sscanf(line, "frame_balance=%f", &value) == 1) frame_balance = value != 0;
        if (sscanf(line, "snapshot=%f", &value) == 1) snapshot = value > 0 ? (int)value : 0;
        if (sscanf(line, "controller_fix=%f", &value) == 1) controller_fix = value != 0;
        if (sscanf(line, "swapchain_fix=%f", &value) == 1) swapchain_fix = value != 0;
        if (sscanf(line, "cube_standin=%f", &value) == 1) cube_standin = value != 0;
        if (sscanf(line, "rect_clamp=%f", &value) == 1) rect_clamp = value != 0;
        if (sscanf(line, "pose_consistency=%f", &value) == 1) pose_consistency = value != 0;
        if (sscanf(line, "pose_debug=%f", &value) == 1) pose_debug = value != 0;
        if (sscanf(line, "pose_time_fix=%f", &value) == 1) pose_time_fix = value != 0;
        if (sscanf(line, "layer_fix=%f", &value) == 1) layer_fix = value != 0;
        if (sscanf(line, "mutable_fix=%f", &value) == 1) mutable_fix = value != 0;
        if (sscanf(line, "swap_eyes=%f", &value) == 1) swap_eyes = value != 0;
        if (sscanf(line, "passthrough_emul=%f", &value) == 1) passthrough_emul = value != 0;
        if (sscanf(line, "respace_kick=%f", &value) == 1) respace_kick = value != 0;
        if (sscanf(line, "strip_depth=%f", &value) == 1) strip_depth = value != 0;
        if (sscanf(line, "flip_quads=%f", &value) == 1) flip_quads = value != 0;
        if (sscanf(line, "flip_emul=%f", &value) == 1) flip_emul_setting = value != 0;
        if (sscanf(line, "cylinder_strips=%f", &value) == 1) cylinder_strips_on = value != 0;
        if (sscanf(line, "scene_emul=%f", &value) == 1) scene_emul = value != 0;
        if (sscanf(line, "scene_height=%f", &value) == 1 && value > 1.5f && value < 5.0f) scene_height = value;
        if (sscanf(line, "scene_width=%f", &value) == 1 && value > 0.5f && value < 20.0f) scene_width = value;
        if (sscanf(line, "scene_depth=%f", &value) == 1 && value > 0.5f && value < 20.0f) scene_depth = value;
        if (sscanf(line, "controller_models=%f", &value) == 1) controller_models = value != 0;
        if (sscanf(line, "layer_debug=%f", &value) == 1) layer_debug = value != 0;
        if (sscanf(line, "input_diag=%f", &value) == 1) input_diag = value != 0;
        if (sscanf(line, "surface_emul=%f", &value) == 1) surface_emul = value != 0;
        if (sscanf(line, "surface_native=%f", &value) == 1) surface_native = value != 0;
        if (sscanf(line, "eye_debug=%f", &value) == 1) eye_debug = value != 0;
        if (sscanf(line, "release_wait=%f", &value) == 1) release_wait = (int)value;
        if (sscanf(line, "profile_remap=%f", &value) == 1) profile_remap = value != 0;
        if (sscanf(line, "proximity_emul=%f", &value) == 1 && value >= 0 && value <= 2) proximity_emul = (int)value;
        if (sscanf(line, "sync_guard=%f", &value) == 1) sync_guard = value != 0;
        if (sscanf(line, "stable_local=%f", &value) == 1) stable_local = value != 0;
        if (sscanf(line, "focus_hold=%f", &value) == 1) focus_hold = value != 0;
        if (sscanf(line, "focus_hold_ms=%f", &value) == 1 && value >= 100 && value <= 5000) focus_hold_ms = value;
        if (sscanf(line, "haptic_scale=%f", &value) == 1 && value >= 0 && value <= 1) haptic_scale = value;
        if (sscanf(line, "aim_pitch=%f", &value) == 1 && fabsf(value) <= 90) aim_pitch = value;
        if (sscanf(line, "aim_yaw=%f", &value) == 1 && fabsf(value) <= 90) aim_yaw = value;
        if (sscanf(line, "aim_forward=%f", &value) == 1 && fabsf(value) <= 0.5f) aim_forward = value;
        if (sscanf(line, "refresh_rate=%f", &value) == 1 && (value == 0 || (value >= 60 && value <= 144))) refresh_rate = value;
        if (sscanf(line, "equirect_emul=%f", &value) == 1) equirect_emul = value != 0;
        if (sscanf(line, "equirect_face=%f", &value) == 1 && value >= 256 && value <= 2730) equirect_face = (int)value;
        if (sscanf(line, "equirect_res=%f", &value) == 1 && value >= 512 && value <= 4096) {equirect_res = (int)value;equirect_res_set=1;}
        if (sscanf(line, "equirect_flip=%f", &value) == 1 && value >= 0 && value <= 7) equirect_flip = (int)value;
        if (sscanf(line, "equirect_fps=%f", &value) == 1 && value >= 0 && value <= 144) equirect_fps = value;
        if (sscanf(line, "equirect_stereo=%f", &value) == 1 && value >= 0 && value <= 2) equirect_stereo = (int)value;
    }
    fclose(f);
    LOG("settings read from %s", path);
}

static void initialize(void) {
    Dl_info self;
    char path[4096];
    if (!dladdr((void *)&initialize, &self) || !self.dli_fname) return;
    snprintf(path, sizeof(path), "%s", self.dli_fname);
    char *slash = strrchr(path, '/');
    if (!slash) return;
    size_t room = sizeof(path) - (size_t)(slash + 1 - path);

    snprintf(slash + 1, room, "libframe_settings.so");
    read_settings(path);

    snprintf(slash + 1, room, "libopenxr_loader_original.so");
    loader = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (loader) next_gipa = (PFN_xrGetInstanceProcAddr)dlsym(loader, "xrGetInstanceProcAddr");

    char process[256] = {0};
    FILE *f = fopen("/proc/self/cmdline", "r");
    if (f) {
        size_t n = fread(process, 1, sizeof(process) - 1, f);
        process[n] = 0;
        fclose(f);
    }
    char *colon = strchr(process, ':');
    if (colon) *colon = 0;
    if (*process && !strchr(process, '/')) {
        snprintf(path, sizeof(path), "/sdcard/Android/data/%s/files/framebridge.conf", process);
        read_settings(path);
    }
    const char *env = getenv("FRAMEBRIDGE_CONFIG");
    if (env && *env) read_settings(env);
    if (snapshot && *process && !strchr(process, '/')) {
        snprintf(snap_dir, sizeof(snap_dir), "/sdcard/Android/data/%s/files", process);
        LOG("per-game: snapshot every %d s to %s/fb_snap_N.ppm", snapshot, snap_dir);
    }
    if (eye_debug && *process && !strchr(process, '/')) {
        snprintf(path, sizeof(path), "/sdcard/Android/data/%s/files/framebridge.log", process);
        log_file = fopen(path, "a");
    }
    if (input_diag) LOG("per-game: input_diag=1 (controller-input diagnostics)");
    if (proximity_emul) LOG("per-game: proximity_emul=%d (finger proximity from capacitive touch: %s)", proximity_emul,
                            proximity_emul > 1 ? "thumb + index" : "thumb");
    if (hide_space_warp) LOG("per-game: hide_space_warp=1 (XR_FB_space_warp hidden, space warp info removed)");
    if (strip_color_bias) LOG("per-game: strip_color_bias=%d (layer color scale/bias removed)", strip_color_bias);
    if (frame_balance) LOG("per-game: frame_balance=1 (an open frame is ended before the next one begins)");
    if (eye_debug || release_wait) LOG("per-game: eye_debug=%d release_wait=%d log_file=%s", eye_debug, release_wait,
                                       log_file ? path : "none");
    if (controller_models && *process && !strchr(process, '/')) render_model_init(process);

    LOG("scale=%.2f foveation_fix=%d controller_fix=%d swapchain_fix=%d layer_fix=%d mutable_fix=%d swap_eyes=%d passthrough_emul=%d respace_kick=%d flip_quads=%d controller_models=%d loader=%s",
        scale, foveation_fix, controller_fix, swapchain_fix, layer_fix, mutable_fix, swap_eyes, passthrough_emul,
        respace_kick, flip_quads, controller_models, next_gipa ? "OK" : (dlerror() ?: "FAILED"));
    if (layer_debug || stable_local || focus_hold || aim_pitch || aim_yaw || aim_forward || refresh_rate || equirect_emul)
        LOG("per-game: layer_debug=%d stable_local=%d focus_hold=%d aim=%.1f/%.1f/%.3f refresh_rate=%.0f equirect_emul=%d "
            "(face %d, res %d, flip %d, fps %.0f, stereo %d)", layer_debug, stable_local, focus_hold, aim_pitch, aim_yaw,
            aim_forward, refresh_rate, equirect_emul, equirect_face, equirect_res, equirect_flip, equirect_fps,
            equirect_stereo);
}

static PFN_xrVoidFunction lookup(XrInstance instance, const char *name) {
    pthread_once(&init_once, initialize);
    PFN_xrVoidFunction fn = NULL;
    if (next_gipa) next_gipa(instance, name, &fn);
    if (!fn && loader) fn = (PFN_xrVoidFunction)dlsym(loader, name);
    return fn;
}

#include "scene_emu.c"
#include "render_model.c"

XRAPI_ATTR XrResult XRAPI_CALL xrCreateInstance(const XrInstanceCreateInfo *info, XrInstance *instance) {
    PFN_xrCreateInstance fn = (PFN_xrCreateInstance)lookup(XR_NULL_HANDLE, "xrCreateInstance");
    if (!fn) return XR_ERROR_INITIALIZATION_FAILED;
    if (!info) return XR_ERROR_VALIDATION_FAILURE;

    int chained = 0, enabled = 0;
    for (const XrBaseInStructure *p = (const XrBaseInStructure *)info->next; p; p = p->next)
        if (p->type == XR_TYPE_INSTANCE_CREATE_INFO_ANDROID_KHR) {
            chained = 1;
            android_vm = ((const struct { XrStructureType type; const void *next; void *vm; void *activity; } *)p)->vm;
        }
    for (uint32_t i = 0; i < info->enabledExtensionCount; ++i)
        if (!strcmp(info->enabledExtensionNames[i], "XR_KHR_android_create_instance")) enabled = 1;

    // Runtime's real extension list (bypassing our own filtered hook), used to drop requested
    // extensions the Frame runtime lacks (e.g. XR_FB_passthrough) instead of failing outright.
    PFN_xrEnumerateInstanceExtensionProperties enum_ext = (PFN_xrEnumerateInstanceExtensionProperties)lookup(
        XR_NULL_HANDLE, "xrEnumerateInstanceExtensionProperties");
    uint32_t available_count = 0;
    XrExtensionProperties *available = NULL;
    if (enum_ext && XR_SUCCEEDED(enum_ext(NULL, 0, &available_count, NULL)) && available_count) {
        available = calloc(available_count, sizeof(*available));
        if (available) {
            for (uint32_t i = 0; i < available_count; ++i) available[i].type = XR_TYPE_EXTENSION_PROPERTIES;
            if (XR_FAILED(enum_ext(NULL, available_count, &available_count, available))) {
                free(available);
                available = NULL;
            }
        }
    }

    XrInstanceCreateInfo fixed = *info;
    const char **names = calloc(info->enabledExtensionCount + 3, sizeof(*names));
    if (!names) { free(available); return XR_ERROR_OUT_OF_MEMORY; }
    uint32_t kept = 0;
    for (uint32_t i = 0; i < info->enabledExtensionCount; ++i) {
        const char *ext = info->enabledExtensionNames[i];
        int supported = !available;  // if we could not enumerate, pass everything through
        for (uint32_t j = 0; available && j < available_count && !supported; ++j)
            supported = !strcmp(available[j].extensionName, ext);
        if (hide_space_warp && !strcmp(ext, "XR_FB_space_warp")) {
            LOG("hide_space_warp: XR_FB_space_warp not enabled");
            continue;
        }
        if (supported) names[kept++] = ext;
        else if (scene_emul && is_scene_extension(ext)) {
            if (!emulate_scene) LOG("emulating Meta scene/spatial-entity extensions from the guardian bounds");
            emulate_scene = 1;
        } else if (controller_models && render_models_available && !strcmp(ext, XR_FB_RENDER_MODEL_EXTENSION_NAME)) {
            emulate_render_model = 1;
            LOG("emulating XR_FB_render_model with Steam Frame controller models");
        } else if (passthrough_emul && !strcmp(ext, "XR_FB_passthrough")) {
            emulate_passthrough = 1;
            LOG("emulating XR_FB_passthrough with alpha-blended environment");
        } else LOG("dropped unsupported extension %s", ext);
    }
    if (chained && !enabled) {
        names[kept++] = "XR_KHR_android_create_instance";
        LOG("added %s", "XR_KHR_android_create_instance");
    }
    {   // no graphics API extension at all: Meta's runtime still accepts a GLES session, the Frame's doesn't
        // (Lambda1VR's TBXR enables only XR_EXT_local_floor). Add GLES, which every such app uses, if offered.
        int graphics = 0, gles_offered = 0;
        for (uint32_t i = 0; i < kept; ++i)
            graphics |= !strcmp(names[i], "XR_KHR_opengl_es_enable") || !strcmp(names[i], "XR_KHR_vulkan_enable") ||
                        !strcmp(names[i], "XR_KHR_vulkan_enable2");
        for (uint32_t j = 0; available && j < available_count; ++j)
            gles_offered |= !strcmp(available[j].extensionName, "XR_KHR_opengl_es_enable");
        if (!graphics && gles_offered) {
            names[kept++] = "XR_KHR_opengl_es_enable";
            LOG("added XR_KHR_opengl_es_enable (the app enabled no graphics API extension)");
        }
    }
    if (refresh_rate > 0) {  // the refresh_rate setting needs XR_FB_display_refresh_rate even if the app doesn't use it
        int requested = 0, offered = 0;
        for (uint32_t i = 0; i < kept; ++i) requested |= !strcmp(names[i], "XR_FB_display_refresh_rate");
        for (uint32_t j = 0; available && j < available_count; ++j) offered |= !strcmp(available[j].extensionName, "XR_FB_display_refresh_rate");
        if (!requested && offered) { names[kept++] = "XR_FB_display_refresh_rate"; LOG("added XR_FB_display_refresh_rate (refresh_rate)"); }
    }
    fixed.enabledExtensionNames = names;
    fixed.enabledExtensionCount = kept;
    {   // what the runtime gets (diagnostics: e.g. a graphics extension the app forgot)
        char list[1024] = "";
        for (uint32_t i = 0; i < kept; ++i) {
            size_t len = strlen(list);
            snprintf(list + len, sizeof list - len, "%s%s", i ? " " : "", names[i] + (strncmp(names[i], "XR_", 3) ? 0 : 3));
        }
        LOG("xrCreateInstance with %u extension(s): %s", kept, list);
    }
    for (uint32_t j = 0; available && j < available_count; ++j)
        if (!strcmp(available[j].extensionName, "XR_FB_composition_layer_image_layout")) runtime_has_image_layout = 1;
        else if (!strcmp(available[j].extensionName, "XR_FB_touch_controller_proximity")) runtime_has_proximity = 1;
    // Layer types are only valid if their extension ends up enabled; some apps submit them regardless.
    int has_equirect = 0, has_equirect2 = 0, has_cylinder = 0, has_cube = 0;
    for (uint32_t i = 0; i < kept; ++i) {
        has_equirect |= !strcmp(names[i], "XR_KHR_composition_layer_equirect");
        has_equirect2 |= !strcmp(names[i], "XR_KHR_composition_layer_equirect2");
        has_cylinder |= !strcmp(names[i], "XR_KHR_composition_layer_cylinder");
        has_cube |= !strcmp(names[i], "XR_KHR_composition_layer_cube");
    }
    no_equirect = !has_equirect; no_equirect2 = !has_equirect2; no_cylinder = !has_cylinder; no_cube = !has_cube;
    free(available);
    mx_audio_initialize();
    XrResult result = fn(&fixed, instance);
    free(names);
    LOG("xrCreateInstance result=%d", result);
    if (XR_SUCCEEDED(result)) { active_instance = *instance; mx_audio_initialize(); }
    return result;
}

XRAPI_ATTR XrResult XRAPI_CALL xrEnumerateViewConfigurationViews(XrInstance instance, XrSystemId system,
        XrViewConfigurationType type, uint32_t capacity, uint32_t *count, XrViewConfigurationView *views) {
    PFN_xrEnumerateViewConfigurationViews fn =
        (PFN_xrEnumerateViewConfigurationViews)lookup(instance, "xrEnumerateViewConfigurationViews");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrResult result = fn(instance, system, type, capacity, count, views);
    if (XR_SUCCEEDED(result) && views && count && scale != 1.0f) {
        for (uint32_t i = 0; i < *count && i < capacity; ++i) {
            uint32_t w = (uint32_t)(views[i].recommendedImageRectWidth * scale + 0.5f);
            uint32_t h = (uint32_t)(views[i].recommendedImageRectHeight * scale + 0.5f);
            if (w > views[i].maxImageRectWidth) w = views[i].maxImageRectWidth;
            if (h > views[i].maxImageRectHeight) h = views[i].maxImageRectHeight;
            views[i].recommendedImageRectWidth = w ? w : 1;
            views[i].recommendedImageRectHeight = h ? h : 1;
            LOG("view %u recommended %ux%u", i, views[i].recommendedImageRectWidth, views[i].recommendedImageRectHeight);
        }
    }
    return result;
}

// Closest runtime-supported swapchain format for one it rejected; 0 if none.
static int64_t pick_format(XrSession session, int64_t wanted) {
    PFN_xrEnumerateSwapchainFormats fn =
        (PFN_xrEnumerateSwapchainFormats)lookup(active_instance, "xrEnumerateSwapchainFormats");
    uint32_t count = 0;
    if (!fn || XR_FAILED(fn(session, 0, &count, NULL)) || !count) return 0;
    int64_t *formats = calloc(count, sizeof(*formats));
    if (!formats || XR_FAILED(fn(session, count, &count, formats))) { free(formats); return 0; }
    static int logged;
    if (!logged++)
        for (uint32_t i = 0; i < count; ++i) LOG("runtime swapchain format[%u]=%lld", i, (long long)formats[i]);

    // Same channel layout, preferring the sRGB variant the runtime accepts.
    static const int64_t equivalents[][2] = {
        {0x8058, 0x8C43},  // GL_RGBA8 -> GL_SRGB8_ALPHA8
        {0x8051, 0x8C43},  // GL_RGB8 -> GL_SRGB8_ALPHA8
        {0x8C41, 0x8C43},  // GL_SRGB8 -> GL_SRGB8_ALPHA8
        {37, 43},          // VK_FORMAT_R8G8B8A8_UNORM -> _SRGB
        {44, 50},          // VK_FORMAT_B8G8R8A8_UNORM -> _SRGB
        {50, 43},          // VK_FORMAT_B8G8R8A8_SRGB -> R8G8B8A8_SRGB
    };
    int64_t chosen = 0;
    for (size_t e = 0; e < sizeof(equivalents) / sizeof(equivalents[0]) && !chosen; ++e)
        if (equivalents[e][0] == wanted)
            for (uint32_t i = 0; i < count && !chosen; ++i)
                if (formats[i] == equivalents[e][1]) chosen = formats[i];
    if (!chosen) chosen = formats[0];  // runtime lists its preferred format first
    free(formats);
    return chosen;
}

// Swapchains the runtime actually created; layers referencing anything else are dropped.
static pthread_mutex_t swapchains_lock = PTHREAD_MUTEX_INITIALIZER;
static XrSwapchain *swapchains;
static size_t swapchain_count, swapchain_capacity;

static void remember_swapchain(XrSwapchain handle) {
    pthread_mutex_lock(&swapchains_lock);
    if (swapchain_count == swapchain_capacity) {
        size_t capacity = swapchain_capacity ? swapchain_capacity * 2 : 64;
        XrSwapchain *grown = realloc(swapchains, capacity * sizeof(*grown));
        if (grown) { swapchains = grown; swapchain_capacity = capacity; }
    }
    if (swapchain_count < swapchain_capacity) swapchains[swapchain_count++] = handle;
    pthread_mutex_unlock(&swapchains_lock);
}

// Swapchain sizes, to keep submitted image rects inside them (rect_clamp).
#define MAX_SIZES 256
static struct { XrSwapchain handle; uint32_t w, h; } sizes[MAX_SIZES];
static pthread_mutex_t sizes_lock = PTHREAD_MUTEX_INITIALIZER;

static void note_swapchain_size(XrSwapchain handle, uint32_t w, uint32_t h) {
    pthread_mutex_lock(&sizes_lock);
    int slot = -1;
    for (int i = 0; i < MAX_SIZES; ++i) {
        if (sizes[i].handle == handle) { slot = i; break; }
        if (slot < 0 && sizes[i].handle == XR_NULL_HANDLE) slot = i;
    }
    if (slot >= 0) { sizes[slot].handle = handle; sizes[slot].w = w; sizes[slot].h = h; }
    pthread_mutex_unlock(&sizes_lock);
}

static int swapchain_size(XrSwapchain handle, uint32_t *w, uint32_t *h) {
    int found = 0;
    pthread_mutex_lock(&sizes_lock);
    for (int i = 0; i < MAX_SIZES && !found; ++i)
        if (sizes[i].handle == handle && handle != XR_NULL_HANDLE) { *w = sizes[i].w; *h = sizes[i].h; found = 1; }
    pthread_mutex_unlock(&sizes_lock);
    return found;
}

// Clamp `r` to a w x h image; returns whether it changed.
static int clamp_rect(XrRect2Di *r, uint32_t w, uint32_t h) {
    XrRect2Di c = *r;
    if (c.offset.x < 0) { c.extent.width += c.offset.x; c.offset.x = 0; }
    if (c.offset.y < 0) { c.extent.height += c.offset.y; c.offset.y = 0; }
    if ((uint32_t)c.offset.x >= w || (uint32_t)c.offset.y >= h) return 0;  // nothing sensible to keep: leave it
    if ((int64_t)c.offset.x + c.extent.width > (int64_t)w) c.extent.width = (int32_t)(w - (uint32_t)c.offset.x);
    if ((int64_t)c.offset.y + c.extent.height > (int64_t)h) c.extent.height = (int32_t)(h - (uint32_t)c.offset.y);
    if (c.extent.width <= 0 || c.extent.height <= 0) return 0;
    int changed = memcmp(&c, r, sizeof c) != 0;
    *r = c;
    return changed;
}

static void forget_swapchain(XrSwapchain handle) {
    pthread_mutex_lock(&swapchains_lock);
    for (size_t i = 0; i < swapchain_count; ++i)
        if (swapchains[i] == handle) { swapchains[i] = swapchains[--swapchain_count]; break; }
    pthread_mutex_unlock(&swapchains_lock);
}

static void flip_on_create_swapchain(XrSwapchain handle, const XrSwapchainCreateInfo *info);
static void flip_on_destroy(XrSwapchain handle);
static void emul_on_create_swapchain(XrSwapchain handle, const XrSwapchainCreateInfo *info);
static void emul_on_destroy_swapchain(XrSwapchain handle);
static int emul_virtual_create(XrSession session, const XrSwapchainCreateInfo *info, XrSwapchain *out);
static int emul_is_virtual(XrSwapchain handle);
static void emul_virtual_destroy(XrSwapchain handle);
static XrResult emul_virtual_enumerate(XrSwapchain handle, uint32_t capacity, uint32_t *count, XrSwapchainImageBaseHeader *images);
static void emul_on_acquire(XrSwapchain handle, uint32_t index);
static XrResult standin_after_failure(const XrSwapchainCreateInfo *info, XrSwapchain *out, XrResult result);
static int is_standin(XrSwapchain handle);
static void standin_destroy(XrSwapchain handle);
static XrResult standin_enumerate(XrSwapchain handle, uint32_t capacity, uint32_t *count, XrSwapchainImageBaseHeader *images);

static int known_swapchain(XrSwapchain handle) {
    int found = 0;
    pthread_mutex_lock(&swapchains_lock);
    for (size_t i = 0; i < swapchain_count && !found; ++i) found = swapchains[i] == handle;
    pthread_mutex_unlock(&swapchains_lock);
    return found;
}

XRAPI_ATTR XrResult XRAPI_CALL xrCreateSwapchain(XrSession session, const XrSwapchainCreateInfo *info,
        XrSwapchain *swapchain) {
    PFN_xrCreateSwapchain fn = (PFN_xrCreateSwapchain)lookup(active_instance, "xrCreateSwapchain");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (!info) return XR_ERROR_VALIDATION_FAILURE;
    XrSwapchainCreateInfo fixed = *info;
    if (foveation_fix) {
        // Drop leading foveation structs; the chain is const so only a prefix can be skipped.
        const XrBaseInStructure *p = (const XrBaseInStructure *)fixed.next;
        while (p && p->type == XR_TYPE_SWAPCHAIN_CREATE_INFO_FOVEATION_FB) p = p->next;
        fixed.next = p;
    }
    if (mutable_fix) fixed.usageFlags |= XR_SWAPCHAIN_USAGE_MUTABLE_FORMAT_BIT;
    // Internal native-video composition snapshots the owned stereo scene before
    // release. Other games and UI/depth/MSAA swapchains keep their original usage.
    if(surface_native && fixed.arraySize==2 && fixed.sampleCount==1 &&
        (fixed.usageFlags&XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT))
        fixed.usageFlags |= XR_SWAPCHAIN_USAGE_TRANSFER_SRC_BIT;
    if (equirect_emul && swapchain && emul_virtual_create(session, &fixed, swapchain)) {
        remember_swapchain(*swapchain);
        emul_on_create_swapchain(*swapchain, &fixed);
        snap_on_create(*swapchain, &fixed);
        return XR_SUCCESS;
    }
    XrResult result = fn(session, &fixed, swapchain);
    LOG("xrCreateSwapchain %ux%u format=%lld samples=%u array=%u faces=%u usage=0x%llx flags=0x%llx result=%d",
        fixed.width, fixed.height, (long long)fixed.format, fixed.sampleCount, fixed.arraySize, fixed.faceCount,
        (unsigned long long)fixed.usageFlags, (unsigned long long)fixed.createFlags, result);
    if (XR_SUCCEEDED(result)) {
        remember_swapchain(*swapchain);
        note_swapchain_size(*swapchain, fixed.width, fixed.height);
        flip_on_create_swapchain(*swapchain, &fixed);
        emul_on_create_swapchain(*swapchain, &fixed);
        snap_on_create(*swapchain, &fixed);
        return result;
    }
    if (!swapchain_fix) return standin_after_failure(&fixed, swapchain, result);

    // Frame's runtime rejects some GLES formats (GL_RGBA8) and MSAA swapchains (overport #71).
    if (fixed.sampleCount > 1) {
        fixed.sampleCount = 1;
        result = fn(session, &fixed, swapchain);
        LOG("  retry samples=1 result=%d", result);
    }
    if (result == XR_ERROR_SWAPCHAIN_FORMAT_UNSUPPORTED) {
        int64_t replacement = pick_format(session, fixed.format);
        if (replacement && replacement != fixed.format) {
            fixed.format = replacement;
            result = fn(session, &fixed, swapchain);
            LOG("  retry format=%lld result=%d", (long long)replacement, result);
        }
    }
    if (XR_SUCCEEDED(result)) {
        remember_swapchain(*swapchain);
        note_swapchain_size(*swapchain, fixed.width, fixed.height);
        flip_on_create_swapchain(*swapchain, &fixed);
        emul_on_create_swapchain(*swapchain, &fixed);
        snap_on_create(*swapchain, &fixed);
    }
    return standin_after_failure(&fixed, swapchain, result);
}

XRAPI_ATTR XrResult XRAPI_CALL xrDestroySwapchain(XrSwapchain swapchain) {
    PFN_xrDestroySwapchain fn = (PFN_xrDestroySwapchain)lookup(active_instance, "xrDestroySwapchain");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (layer_debug) {
        static int logged;
        if (logged++ < 200) LOG("layer_debug: xrDestroySwapchain %p", (void *)swapchain);
    }
    if (is_standin(swapchain)) { standin_destroy(swapchain); return XR_SUCCESS; }
    surf_scene_destroy(swapchain);
    forget_swapchain(swapchain);
    surf_on_destroy(swapchain);
    flip_on_destroy(swapchain);
    emul_on_destroy_swapchain(swapchain);
    snap_on_destroy(swapchain);
    if (equirect_emul && emul_is_virtual(swapchain)) { emul_virtual_destroy(swapchain); return XR_SUCCESS; }
    return fn(swapchain);
}

// ---- cylinder layers (XR_KHR_composition_layer_cylinder), which the Frame runtime lacks: shown as a few flat quads
// (chords of the arc), each showing its share of the image. The runtime composites them from the app's own image at
// full resolution, and they stay within centimetres of where the app aims its pointer at the curved surface.
static XrVector3f strip_rotate(XrQuaternionf q, XrVector3f v) {  // v' = v + 2w(u x v) + 2 u x (u x v)
    XrVector3f u = {q.x, q.y, q.z}, t = {2 * (u.y * v.z - u.z * v.y), 2 * (u.z * v.x - u.x * v.z), 2 * (u.x * v.y - u.y * v.x)};
    return (XrVector3f){v.x + q.w * t.x + (u.y * t.z - u.z * t.y), v.y + q.w * t.y + (u.z * t.x - u.x * t.z),
                        v.z + q.w * t.z + (u.x * t.y - u.y * t.x)};
}

static XrQuaternionf strip_mul(XrQuaternionf a, XrQuaternionf b) {
    return (XrQuaternionf){a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y, a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
                           a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w, a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z};
}

// Returns the number of quads written to out (0 = not possible). budget = layers still allowed in the frame.
static int cylinder_strips(const XrCompositionLayerCylinderKHR *c, XrCompositionLayerQuad *out, int budget) {
    if (c->centralAngle <= 0 || c->aspectRatio <= 0) return 0;
    float r = c->radius > 0 && c->radius < 1e4f ? c->radius : 3.0f;
    int n = (int)ceilf(c->centralAngle / 0.26f);  // ~15 degrees per strip
    if (n > 8) n = 8;
    if (n > budget) n = budget;
    if (n < 1) return 0;
    float a = c->centralAngle / (float)n, height = r * c->centralAngle / c->aspectRatio;
    const XrRect2Di rect = c->subImage.imageRect;
    for (int i = 0; i < n; ++i) {
        float phi = -c->centralAngle / 2 + (i + 0.5f) * a;  // + = towards +X (the image's right)
        XrQuaternionf yaw = {0, sinf(-phi / 2), 0, cosf(-phi / 2)};
        XrPosef pose;
        pose.orientation = strip_mul(c->pose.orientation, yaw);
        XrVector3f d = strip_rotate(pose.orientation, (XrVector3f){0, 0, -r * cosf(a / 2)});
        pose.position = (XrVector3f){c->pose.position.x + d.x, c->pose.position.y + d.y, c->pose.position.z + d.z};
        int32_t x0 = rect.offset.x + (int32_t)((int64_t)rect.extent.width * i / n);
        int32_t x1 = rect.offset.x + (int32_t)((int64_t)rect.extent.width * (i + 1) / n);
        XrSwapchainSubImage sub = c->subImage;
        sub.imageRect = (XrRect2Di){{x0, rect.offset.y}, {x1 - x0, rect.extent.height}};
        out[i] = (XrCompositionLayerQuad){XR_TYPE_COMPOSITION_LAYER_QUAD, c->next, c->layerFlags, c->space,
                                          c->eyeVisibility, sub, pose, {2 * r * sinf(a / 2), height}};
    }
    static int logged;
    if (logged++ < 3) LOG("cylinder layer -> %d quad strips (r=%.2f angle=%.2f)", n, r, c->centralAngle);
    return n;
}

// A layer is usable if every swapchain it references was created successfully.
// Swapchains the runtime can show: created by it (adapter-served 360 pictures only feed the adapter's own drawing).
static int runtime_swapchain(XrSwapchain handle) {
    return known_swapchain(handle) && !(equirect_emul && emul_is_virtual(handle));
}

static int layer_usable(const XrCompositionLayerBaseHeader *layer) {
    switch (layer->type) {
    case XR_TYPE_COMPOSITION_LAYER_PROJECTION: {
        const XrCompositionLayerProjection *p = (const XrCompositionLayerProjection *)layer;
        for (uint32_t v = 0; v < p->viewCount; ++v)
            if (!runtime_swapchain(p->views[v].subImage.swapchain)) return 0;
        return 1;
    }
    case XR_TYPE_COMPOSITION_LAYER_QUAD:
        return runtime_swapchain(((const XrCompositionLayerQuad *)layer)->subImage.swapchain);
    case XR_TYPE_COMPOSITION_LAYER_CYLINDER_KHR:
        if (no_cylinder) return 0;
        return runtime_swapchain(((const XrCompositionLayerCylinderKHR *)layer)->subImage.swapchain);
    case XR_TYPE_COMPOSITION_LAYER_EQUIRECT_KHR:
        if (no_equirect) return 0;
        return known_swapchain(((const XrCompositionLayerEquirectKHR *)layer)->subImage.swapchain);
    case XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR:
        if (no_equirect2) return 0;
        return known_swapchain(((const XrCompositionLayerEquirect2KHR *)layer)->subImage.swapchain);
    case XR_TYPE_COMPOSITION_LAYER_CUBE_KHR:
        if (no_cube) return 0;
        return runtime_swapchain(((const XrCompositionLayerCubeKHR *)layer)->swapchain);
    default:
        return 1;  // layer types without swapchains (e.g. passthrough) or unknown: pass through
    }
}

// ---------------------------------------------------------------- XR_FB_passthrough emulation
static uint64_t next_fake_handle = 0x7f000001;
#define FAKE_HANDLE(type) ((type)(uintptr_t)__atomic_fetch_add(&next_fake_handle, 1, __ATOMIC_RELAXED))

static XRAPI_ATTR XrResult XRAPI_CALL emu_create_passthrough(XrSession s, const XrPassthroughCreateInfoFB *i, XrPassthroughFB *out) {
    (void)s; (void)i; if (!out) return XR_ERROR_VALIDATION_FAILURE; *out = FAKE_HANDLE(XrPassthroughFB); return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_passthrough_handle(XrPassthroughFB p) { (void)p; return XR_SUCCESS; }
static XRAPI_ATTR XrResult XRAPI_CALL emu_create_passthrough_layer(XrSession s, const XrPassthroughLayerCreateInfoFB *i,
        XrPassthroughLayerFB *out) {
    (void)s; (void)i; if (!out) return XR_ERROR_VALIDATION_FAILURE; *out = FAKE_HANDLE(XrPassthroughLayerFB); return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_passthrough_layer_handle(XrPassthroughLayerFB l) { (void)l; return XR_SUCCESS; }
static XRAPI_ATTR XrResult XRAPI_CALL emu_passthrough_layer_style(XrPassthroughLayerFB l, const XrPassthroughStyleFB *st) {
    (void)l; (void)st; return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_create_geometry(XrSession s, const XrGeometryInstanceCreateInfoFB *i, XrGeometryInstanceFB *out) {
    (void)s; (void)i; if (!out) return XR_ERROR_VALIDATION_FAILURE; *out = FAKE_HANDLE(XrGeometryInstanceFB); return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_geometry_handle(XrGeometryInstanceFB g) { (void)g; return XR_SUCCESS; }
static XRAPI_ATTR XrResult XRAPI_CALL emu_geometry_transform(XrGeometryInstanceFB g, const XrGeometryInstanceTransformFB *t) {
    (void)g; (void)t; return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL emu_create_mesh(XrSession s, const XrTriangleMeshCreateInfoFB *i, XrTriangleMeshFB *out) {
    (void)s; (void)i; if (!out) return XR_ERROR_VALIDATION_FAILURE; *out = FAKE_HANDLE(XrTriangleMeshFB); return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_mesh_handle(XrTriangleMeshFB m) { (void)m; return XR_SUCCESS; }
static XRAPI_ATTR XrResult XRAPI_CALL emu_create_lut(XrPassthroughFB p, const XrPassthroughColorLutCreateInfoMETA *i,
        XrPassthroughColorLutMETA *out) {
    (void)p; (void)i; if (!out) return XR_ERROR_VALIDATION_FAILURE; *out = FAKE_HANDLE(XrPassthroughColorLutMETA); return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_lut_handle(XrPassthroughColorLutMETA l) { (void)l; return XR_SUCCESS; }
static XRAPI_ATTR XrResult XRAPI_CALL emu_update_lut(XrPassthroughColorLutMETA l, const XrPassthroughColorLutUpdateInfoMETA *u) {
    (void)l; (void)u; return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_preferences(XrSession s, XrPassthroughPreferencesMETA *prefs) {
    (void)s; if (!prefs) return XR_ERROR_VALIDATION_FAILURE; prefs->flags = 0; return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_keyboard_hands(XrPassthroughLayerFB l, const XrPassthroughKeyboardHandsIntensityFB *i) {
    (void)l; (void)i; return XR_SUCCESS;
}

static PFN_xrVoidFunction passthrough_emulation(const char *name) {
    if (!emulate_passthrough) return NULL;
    static const struct { const char *name; PFN_xrVoidFunction fn; } table[] = {
        {"xrCreatePassthroughFB", (PFN_xrVoidFunction)emu_create_passthrough},
        {"xrDestroyPassthroughFB", (PFN_xrVoidFunction)emu_passthrough_handle},
        {"xrPassthroughStartFB", (PFN_xrVoidFunction)emu_passthrough_handle},
        {"xrPassthroughPauseFB", (PFN_xrVoidFunction)emu_passthrough_handle},
        {"xrCreatePassthroughLayerFB", (PFN_xrVoidFunction)emu_create_passthrough_layer},
        {"xrDestroyPassthroughLayerFB", (PFN_xrVoidFunction)emu_passthrough_layer_handle},
        {"xrPassthroughLayerPauseFB", (PFN_xrVoidFunction)emu_passthrough_layer_handle},
        {"xrPassthroughLayerResumeFB", (PFN_xrVoidFunction)emu_passthrough_layer_handle},
        {"xrPassthroughLayerSetStyleFB", (PFN_xrVoidFunction)emu_passthrough_layer_style},
        {"xrCreateGeometryInstanceFB", (PFN_xrVoidFunction)emu_create_geometry},
        {"xrDestroyGeometryInstanceFB", (PFN_xrVoidFunction)emu_geometry_handle},
        {"xrGeometryInstanceSetTransformFB", (PFN_xrVoidFunction)emu_geometry_transform},
        // Companion passthrough entry points OVRPlugin's Insight MR init requires.
        {"xrCreateTriangleMeshFB", (PFN_xrVoidFunction)emu_create_mesh},
        {"xrDestroyTriangleMeshFB", (PFN_xrVoidFunction)emu_mesh_handle},
        {"xrCreatePassthroughColorLutMETA", (PFN_xrVoidFunction)emu_create_lut},
        {"xrDestroyPassthroughColorLutMETA", (PFN_xrVoidFunction)emu_lut_handle},
        {"xrUpdatePassthroughColorLutMETA", (PFN_xrVoidFunction)emu_update_lut},
        {"xrGetPassthroughPreferencesMETA", (PFN_xrVoidFunction)emu_preferences},
        {"xrPassthroughLayerSetKeyboardHandsIntensityFB", (PFN_xrVoidFunction)emu_keyboard_hands},
    };
    for (size_t i = 0; i < sizeof(table) / sizeof(table[0]); ++i)
        if (!strcmp(name, table[i].name)) return table[i].fn;
    return NULL;
}

XRAPI_ATTR XrResult XRAPI_CALL xrGetSystemProperties(XrInstance instance, XrSystemId system,
        XrSystemProperties *properties) {
    PFN_xrGetSystemProperties fn = (PFN_xrGetSystemProperties)lookup(instance, "xrGetSystemProperties");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrResult result = fn(instance, system, properties);
    if (XR_SUCCEEDED(result) && properties && layer_debug)
        LOG("layer_debug: system '%s' maxLayerCount=%u max swapchain %ux%u", properties->systemName,
            properties->graphicsProperties.maxLayerCount, properties->graphicsProperties.maxSwapchainImageWidth,
            properties->graphicsProperties.maxSwapchainImageHeight);
    if (XR_SUCCEEDED(result) && (emulate_passthrough || emulate_scene || emulate_render_model) && properties)
        for (XrBaseOutStructure *p = (XrBaseOutStructure *)properties->next; p; p = p->next) {
            if (p->type == XR_TYPE_SYSTEM_PASSTHROUGH_PROPERTIES_FB)
                ((XrSystemPassthroughPropertiesFB *)p)->supportsPassthrough = XR_TRUE;
            if (p->type == XR_TYPE_SYSTEM_SPATIAL_ENTITY_PROPERTIES_FB && emulate_scene)
                ((XrSystemSpatialEntityPropertiesFB *)p)->supportsSpatialEntity = XR_TRUE;
            if (p->type == XR_TYPE_SYSTEM_PASSTHROUGH_PROPERTIES2_FB)
                ((XrSystemPassthroughProperties2FB *)p)->capabilities = XR_PASSTHROUGH_CAPABILITY_BIT_FB;
            if (p->type == XR_TYPE_SYSTEM_RENDER_MODEL_PROPERTIES_FB && emulate_render_model)
                ((XrSystemRenderModelPropertiesFB *)p)->supportsRenderModelLoading = XR_TRUE;
        }
    return result;
}

#include "flip_vk.c"
#include "pose_time.c"

// XR_KHR_convert_timespec_time for runtimes that lack it (Frame): XrTime is derived from the monotonic clock
// using the offset measured at the last xrWaitFrame. The runtime's own implementation is preferred.
static PFN_xrConvertTimespecTimeToTimeKHR runtime_timespec_to_time;
static PFN_xrConvertTimeToTimespecTimeKHR runtime_time_to_timespec;

static XRAPI_ATTR XrResult XRAPI_CALL emu_timespec_to_time(XrInstance instance, const struct timespec *ts, XrTime *time) {
    if (runtime_timespec_to_time) {
        XrResult r = runtime_timespec_to_time(instance, ts, time);
        if (r != XR_ERROR_FUNCTION_UNSUPPORTED) {
            if (XR_SUCCEEDED(r) && time) pose_time_note("xrConvertTimespecTimeToTimeKHR (runtime)", 0, 0, *time);
            return r;
        }
    }
    if (!ts || !time) return XR_ERROR_VALIDATION_FAILURE;
    *time = (XrTime)((int64_t)ts->tv_sec * 1000000000ll + ts->tv_nsec + (xr_time_calibrated ? xr_time_offset : 0));
    pose_time_note("xrConvertTimespecTimeToTimeKHR", 0, 0, *time);
    return XR_SUCCESS;
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_time_to_timespec(XrInstance instance, XrTime time, struct timespec *ts) {
    if (runtime_time_to_timespec) {
        XrResult r = runtime_time_to_timespec(instance, time, ts);
        if (r != XR_ERROR_FUNCTION_UNSUPPORTED) return r;
    }
    if (!ts) return XR_ERROR_VALIDATION_FAILURE;
    int64_t mono = (int64_t)time - (xr_time_calibrated ? xr_time_offset : 0);
    ts->tv_sec = (time_t)(mono / 1000000000ll);
    ts->tv_nsec = (long)(mono % 1000000000ll);
    return XR_SUCCESS;
}

XRAPI_ATTR XrResult XRAPI_CALL xrLocateSpace(XrSpace space, XrSpace baseSpace, XrTime time, XrSpaceLocation *location) {
    time = pose_time_fixed(time);
    pose_time_note("xrLocateSpace", (uint64_t)(uintptr_t)space, (uint64_t)(uintptr_t)baseSpace, time);
    if (emulate_scene && location) {
        XrPosef pose; XrSpaceLocationFlags flags;
        if (locate_fake(space, baseSpace, time, &pose, &flags)) {
            location->pose = pose;
            location->locationFlags = flags;
            return XR_SUCCESS;
        }
    }
    PFN_xrLocateSpace fn = (PFN_xrLocateSpace)lookup(active_instance, "xrLocateSpace");
    return fn ? fn(space, baseSpace, time, location) : XR_ERROR_FUNCTION_UNSUPPORTED;
}

static XrResult locate_spaces_common(const char *name, XrSession session, const XrSpacesLocateInfo *info,
                                     XrSpaceLocations *locations) {
    int any_fake = 0;
    XrSpacesLocateInfo moved;
    if (info && pose_time_fix) {
        moved = *info;
        moved.time = pose_time_fixed(info->time);
        info = &moved;
    }
    if (info) pose_time_note(name, info->spaceCount ? (uint64_t)(uintptr_t)info->spaces[0] : 0,
                             (uint64_t)(uintptr_t)info->baseSpace, info->time);
    if (emulate_scene && info && locations) {
        any_fake = fake_space_index(info->baseSpace) >= 0;
        for (uint32_t i = 0; i < info->spaceCount && !any_fake; ++i) any_fake = fake_space_index(info->spaces[i]) >= 0;
    }
    if (!any_fake) {
        PFN_xrLocateSpaces fn = (PFN_xrLocateSpaces)lookup(active_instance, name);
        return fn ? fn(session, info, locations) : XR_ERROR_FUNCTION_UNSUPPORTED;
    }
    for (uint32_t i = 0; i < info->spaceCount && i < locations->locationCount; ++i) {
        XrSpaceLocation one = {XR_TYPE_SPACE_LOCATION, NULL, 0, {{0, 0, 0, 1}, {0, 0, 0}}};
        xrLocateSpace(info->spaces[i], info->baseSpace, info->time, &one);
        locations->locations[i].locationFlags = one.locationFlags;
        locations->locations[i].pose = one.pose;
    }
    return XR_SUCCESS;
}
XRAPI_ATTR XrResult XRAPI_CALL xrLocateSpaces(XrSession session, const XrSpacesLocateInfo *info, XrSpaceLocations *locations) {
    return locate_spaces_common("xrLocateSpaces", session, info, locations);
}
static XRAPI_ATTR XrResult XRAPI_CALL emu_locate_spaces_khr(XrSession session, const XrSpacesLocateInfo *info,
                                                            XrSpaceLocations *locations) {
    return locate_spaces_common("xrLocateSpacesKHR", session, info, locations);
}

XRAPI_ATTR XrResult XRAPI_CALL xrDestroySpace(XrSpace space) {
    if (fake_space_index(space) >= 0) return XR_SUCCESS;  // entity spaces live for the whole session
    PFN_xrDestroySpace fn = (PFN_xrDestroySpace)lookup(active_instance, "xrDestroySpace");
    return fn ? fn(space) : XR_ERROR_FUNCTION_UNSUPPORTED;
}

#include "input_diag.c"
#include "session_fixes.c"
#include "layer_emul_gl.c"
#include "cube_standin.c"
#include "snapshot_gl.c"
#include "surface_swapchain.c"

// GPU video buffers need explicit external-memory support on the app's device.
// Preserve the runtime's extension list and both Vulkan-enable entry points.
XRAPI_ATTR XrResult XRAPI_CALL xrCreateVulkanInstanceKHR(XrInstance instance,const XrVulkanInstanceCreateInfoKHR *info,
        VkInstance *created,VkResult *vulkanResult) {
    PFN_xrCreateVulkanInstanceKHR fn=(PFN_xrCreateVulkanInstanceKHR)lookup(instance,"xrCreateVulkanInstanceKHR");
    if(!fn)return XR_ERROR_FUNCTION_UNSUPPORTED;
    if(!surface_native)return fn(instance,info,created,vulkanResult);
    XrResult result=fn(instance,info,created,vulkanResult);
    if(XR_SUCCEEDED(result) && created && vulkanResult && *vulkanResult==VK_SUCCESS)vk.instance=*created;
    return result;
}
static const char *surf_device_exts[] = {"VK_ANDROID_external_memory_android_hardware_buffer",
    "VK_KHR_external_memory", "VK_KHR_get_memory_requirements2", "VK_KHR_dedicated_allocation",
    "VK_EXT_queue_family_foreign", "VK_KHR_sampler_ycbcr_conversion", "VK_KHR_bind_memory2", "VK_KHR_maintenance1"};
static int surf_extension_present(const char *text,const char *name) {
    size_t length=strlen(name);
    for(const char *p=text;(p=strstr(p,name));p++)
        if((p==text || p[-1]==' ') && (p[length]==' ' || p[length]=='\0'))return 1;
    return 0;
}
static int surf_device_supported(PFN_vkGetInstanceProcAddr gipa, VkInstance instance, VkPhysicalDevice physical) {
    if (!surface_native || !gipa) return 0;
    if(!physical){
        // Unity can ask for extension strings before it creates its instance.
        // Query a temporary instance instead of depending on that call order.
        PFN_vkCreateInstance create=(PFN_vkCreateInstance)gipa(VK_NULL_HANDLE,"vkCreateInstance");
        VkApplicationInfo app={VK_STRUCTURE_TYPE_APPLICATION_INFO,NULL,"FrameBridge video capabilities",0,NULL,0,VK_API_VERSION_1_1};
        VkInstanceCreateInfo ci={VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,NULL,0,&app};
        VkInstance probe=VK_NULL_HANDLE;if(!create || create(&ci,NULL,&probe)!=VK_SUCCESS)return 0;
        PFN_vkEnumeratePhysicalDevices devices=(PFN_vkEnumeratePhysicalDevices)gipa(probe,"vkEnumeratePhysicalDevices");
        PFN_vkDestroyInstance destroy=(PFN_vkDestroyInstance)gipa(probe,"vkDestroyInstance");
        uint32_t count=0;VkPhysicalDevice gpu=VK_NULL_HANDLE;
        int ok=devices && devices(probe,&count,NULL)==VK_SUCCESS && count==1 &&
            devices(probe,&count,&gpu)==VK_SUCCESS && surf_device_supported(gipa,probe,gpu);
        if(destroy)destroy(probe,NULL);return ok;
    }
    PFN_vkEnumerateDeviceExtensionProperties enumerate=(PFN_vkEnumerateDeviceExtensionProperties)
        gipa(instance,"vkEnumerateDeviceExtensionProperties");
    uint32_t count=0;
    if(!enumerate || enumerate(physical,NULL,&count,NULL)!=VK_SUCCESS || count>2048)return 0;
    VkExtensionProperties *exts=calloc(count,sizeof(*exts));
    if(!exts)return 0;
    int ok=enumerate(physical,NULL,&count,exts)==VK_SUCCESS;
    for(unsigned j=0;j<sizeof(surf_device_exts)/sizeof(*surf_device_exts);j++){
        int found=0;for(uint32_t i=0;i<count;i++)found|=!strcmp(exts[i].extensionName,surf_device_exts[j]);
        ok &= found;
    }
    free(exts);return ok;
}
XRAPI_ATTR XrResult XRAPI_CALL xrGetVulkanGraphicsDeviceKHR(XrInstance instance,XrSystemId system,
        VkInstance vulkanInstance,VkPhysicalDevice *physical) {
    PFN_xrGetVulkanGraphicsDeviceKHR fn=(PFN_xrGetVulkanGraphicsDeviceKHR)lookup(instance,"xrGetVulkanGraphicsDeviceKHR");
    if(!surface_native)return fn?fn(instance,system,vulkanInstance,physical):XR_ERROR_FUNCTION_UNSUPPORTED;
    XrResult result=fn?fn(instance,system,vulkanInstance,physical):XR_ERROR_FUNCTION_UNSUPPORTED;
    if(XR_SUCCEEDED(result) && physical){vk.instance=vulkanInstance;vk.physical=*physical;}
    return result;
}
XRAPI_ATTR XrResult XRAPI_CALL xrGetVulkanDeviceExtensionsKHR(XrInstance instance,XrSystemId system,
        uint32_t capacity,uint32_t *count,char *buffer) {
    PFN_xrGetVulkanDeviceExtensionsKHR fn=(PFN_xrGetVulkanDeviceExtensionsKHR)lookup(instance,"xrGetVulkanDeviceExtensionsKHR");
    if(!fn)return XR_ERROR_FUNCTION_UNSUPPORTED;
    if(!surface_native)return fn(instance,system,capacity,count,buffer);
    void *lib=dlopen("libvulkan.so",RTLD_NOW|RTLD_LOCAL);
    PFN_vkGetInstanceProcAddr gipa=lib?(PFN_vkGetInstanceProcAddr)dlsym(lib,"vkGetInstanceProcAddr"):NULL;
    if(!surf_device_supported(gipa,vk.instance,vk.physical))return fn(instance,system,capacity,count,buffer);
    uint32_t original=0;XrResult result=fn(instance,system,0,&original,NULL);
    if(XR_FAILED(result) || !count || original>65536)return result;
    char *text=calloc(original+1024,1);if(!text)return XR_ERROR_OUT_OF_MEMORY;
    result=fn(instance,system,original,&original,text);
    if(XR_SUCCEEDED(result)){
        for(unsigned i=0;i<sizeof(surf_device_exts)/sizeof(*surf_device_exts);i++){
            if(!surf_extension_present(text,surf_device_exts[i])){if(*text)strcat(text," ");strcat(text,surf_device_exts[i]);}
        }
        *count=(uint32_t)strlen(text)+1;
        if(capacity && (capacity<*count || !buffer))result=XR_ERROR_SIZE_INSUFFICIENT;
        else if(capacity)memcpy(buffer,text,*count);
    }
    free(text);return result;
}
XRAPI_ATTR XrResult XRAPI_CALL xrCreateVulkanDeviceKHR(XrInstance instance,const XrVulkanDeviceCreateInfoKHR *info,
        VkDevice *device,VkResult *vulkanResult) {
    PFN_xrCreateVulkanDeviceKHR fn=(PFN_xrCreateVulkanDeviceKHR)lookup(instance,"xrCreateVulkanDeviceKHR");
    if(!fn)return XR_ERROR_FUNCTION_UNSUPPORTED;
    if(!surface_native)return fn(instance,info,device,vulkanResult);
    if(!info || !info->vulkanCreateInfo || !surf_device_supported(info->pfnGetInstanceProcAddr,
        vk.instance,info->vulkanPhysicalDevice))return fn(instance,info,device,vulkanResult);
    VkDeviceCreateInfo ci=*info->vulkanCreateInfo;
    const char **names=calloc(ci.enabledExtensionCount+8,sizeof(*names));
    if(!names)return XR_ERROR_OUT_OF_MEMORY;
    for(uint32_t i=0;i<ci.enabledExtensionCount;i++)names[i]=ci.ppEnabledExtensionNames[i];
    for(unsigned j=0;j<8;j++){
        int found=0;for(uint32_t i=0;i<ci.enabledExtensionCount;i++)found|=!strcmp(names[i],surf_device_exts[j]);
        if(!found)names[ci.enabledExtensionCount++]=surf_device_exts[j];
    }
    ci.ppEnabledExtensionNames=names;
    XrVulkanDeviceCreateInfoKHR fixed=*info;fixed.vulkanCreateInfo=&ci;
    XrResult result=fn(instance,&fixed,device,vulkanResult);free(names);return result;
}
#include "eye_debug.c"

// OpenXR requires xrGet*GraphicsRequirementsKHR before xrCreateSession; Meta's runtime doesn't enforce it, the Frame's
// does (XR_ERROR_GRAPHICS_REQUIREMENTS_CALL_MISSING, e.g. Lambda1VR's TBXR). Ask on the app's behalf, once, and retry.
static int ask_graphics_requirements(XrInstance instance, const XrSessionCreateInfo *info) {
    const XrBaseInStructure *binding = info ? (const XrBaseInStructure *)info->next : NULL;
    for (; binding; binding = binding->next) {
        const char *name = NULL;
        XrStructureType type = 0;
        if (binding->type == (XrStructureType)1000024001) {  // XR_TYPE_GRAPHICS_BINDING_OPENGL_ES_ANDROID_KHR
            name = "xrGetOpenGLESGraphicsRequirementsKHR";
            type = (XrStructureType)1000024003;               // XR_TYPE_GRAPHICS_REQUIREMENTS_OPENGL_ES_KHR
        } else if (binding->type == (XrStructureType)1000025000) {  // XR_TYPE_GRAPHICS_BINDING_VULKAN_KHR
            name = "xrGetVulkanGraphicsRequirementsKHR";
            type = (XrStructureType)1000025002;               // XR_TYPE_GRAPHICS_REQUIREMENTS_VULKAN_KHR
        }
        if (!name) continue;
        // XrGraphicsRequirementsOpenGLESKHR and ...VulkanKHR share this layout
        struct { XrStructureType type; void *next; XrVersion min, max; } req = {type, NULL, 0, 0};
        typedef XrResult (XRAPI_PTR *PFN_requirements)(XrInstance, XrSystemId, void *);
        PFN_requirements get = (PFN_requirements)lookup(instance, name);
        if (!get && type == (XrStructureType)1000025002)  // XR_KHR_vulkan_enable2 apps: same struct and call
            get = (PFN_requirements)lookup(instance, "xrGetVulkanGraphicsRequirements2KHR");
        XrResult r = get ? get(instance, info->systemId, &req) : XR_ERROR_FUNCTION_UNSUPPORTED;
        LOG("xrCreateSession: the app skipped %s; asked for it (result=%d)", name, (int)r);
        return XR_SUCCEEDED(r);
    }
    return 0;
}

XRAPI_ATTR XrResult XRAPI_CALL xrCreateSession(XrInstance instance, const XrSessionCreateInfo *info, XrSession *session) {
    PFN_xrCreateSession fn = (PFN_xrCreateSession)lookup(instance, "xrCreateSession");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrResult result = fn(instance, info, session);
    if (result == XR_ERROR_GRAPHICS_REQUIREMENTS_CALL_MISSING && ask_graphics_requirements(instance, info))
        result = fn(instance, info, session);
    if (XR_SUCCEEDED(result)) {
        flip_emul = flip_emul_setting;
        flip_on_create_session(info);
        scene_on_create_session(*session);
        session_fixes_on_create_session(*session);
        emul_on_create_session(*session, info);
    }
    return result;
}

// Vibrations: logged (the first few, to see what games ask for) and scaled by haptic_scale. Plain vibrations, Meta's
// amplitude envelopes and PCM buffers all pass here: games' own calls, OVRPort's VrApi bridge and the xrshim alike.
static int haptics_logged;
static XrResult apply_haptics(XrSession session, const XrHapticActionInfo *info, const XrHapticBaseHeader *feedback) {
    PFN_xrApplyHapticFeedback fn = (PFN_xrApplyHapticFeedback)lookup(active_instance, "xrApplyHapticFeedback");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (!feedback) return fn(session, info, feedback);
    if (feedback->type == XR_TYPE_HAPTIC_VIBRATION) {
        XrHapticVibration v = *(const XrHapticVibration *)feedback;
        if (haptics_logged++ < 8)
            LOG("haptic: vibration amplitude=%.2f duration=%lld ns frequency=%.0f (scale %.2f)", v.amplitude,
                (long long)v.duration, v.frequency, haptic_scale);
        v.amplitude *= haptic_scale;
        if (v.amplitude <= 0.001f) {
            // OVRPlugin stops a vibration by sending amplitude 0 with its usual 2 s duration (Jurassic World, The Boys
            // VR): on a Quest that stops the motor; OpenXR's stop is xrStopHapticFeedback, and without it the last
            // buzz ran its full 2 s
            PFN_xrStopHapticFeedback stop = (PFN_xrStopHapticFeedback)lookup(active_instance, "xrStopHapticFeedback");
            if (stop) return stop(session, info);
        }
        return fn(session, info, (const XrHapticBaseHeader *)&v);
    }
    if (feedback->type == (XrStructureType)1000173001 || feedback->type == (XrStructureType)1000209001) {
        // XR_TYPE_HAPTIC_AMPLITUDE_ENVELOPE_VIBRATION_FB / XR_TYPE_HAPTIC_PCM_VIBRATION_FB: a sample buffer
        typedef struct { XrStructureType type; const void *next; XrDuration duration; uint32_t count; const float *s; } env_t;
        typedef struct { XrStructureType type; const void *next; uint32_t count; const float *s; float rate; XrBool32 append;
                         uint32_t *consumed; } pcm_t;
        int envelope = feedback->type == (XrStructureType)1000173001;
        uint32_t n = envelope ? ((const env_t *)feedback)->count : ((const pcm_t *)feedback)->count;
        const float *in = envelope ? ((const env_t *)feedback)->s : ((const pcm_t *)feedback)->s;
        if (haptics_logged++ < 8) {
            float peak = 0;
            for (uint32_t i = 0; in && i < n; ++i) peak = fmaxf(peak, in[i]);
            LOG("haptic: %s %u samples peak=%.2f (scale %.2f)", envelope ? "envelope" : "pcm", n, peak, haptic_scale);
        }
        if (haptic_scale >= 0.999f || !in || !n || n > 1u << 20) return fn(session, info, feedback);
        float *scaled = malloc(n * sizeof *scaled);
        if (!scaled) return fn(session, info, feedback);
        for (uint32_t i = 0; i < n; ++i) scaled[i] = in[i] * haptic_scale;
        XrResult r;
        if (envelope) { env_t e = *(const env_t *)feedback; e.s = scaled; r = fn(session, info, (const XrHapticBaseHeader *)&e); }
        else { pcm_t c = *(const pcm_t *)feedback; c.s = scaled; r = fn(session, info, (const XrHapticBaseHeader *)&c); }
        free(scaled);
        return r;
    }
    return fn(session, info, feedback);
}

XRAPI_ATTR XrResult XRAPI_CALL xrApplyHapticFeedback(XrSession session, const XrHapticActionInfo *info,
                                                    const XrHapticBaseHeader *feedback) {
    XrResult result = apply_haptics(session, info, feedback);
    if (input_diag && XR_FAILED(result))  // always hooked (haptic_scale), so input_diag reports failures here
        diag_call_failed("xrApplyHapticFeedback", result,
                         feedback && feedback->type != XR_TYPE_HAPTIC_VIBRATION ? "(not XrHapticVibration)" : NULL);
    return result;
}

XRAPI_ATTR XrResult XRAPI_CALL xrEnumerateSwapchainImages(XrSwapchain swapchain, uint32_t capacity, uint32_t *count,
        XrSwapchainImageBaseHeader *images) {
    PFN_xrEnumerateSwapchainImages fn = (PFN_xrEnumerateSwapchainImages)lookup(active_instance, "xrEnumerateSwapchainImages");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (is_standin(swapchain)) return standin_enumerate(swapchain, capacity, count, images);
    XrResult result = equirect_emul && emul_is_virtual(swapchain) ? emul_virtual_enumerate(swapchain, capacity, count, images)
                                                                  : fn(swapchain, capacity, count, images);
    if (XR_SUCCEEDED(result) && images && capacity && count) {
        flip_on_enumerate_images(swapchain, *count, images);
        emul_on_enumerate(swapchain, *count < capacity ? *count : capacity, images);
        snap_on_enumerate(swapchain, *count < capacity ? *count : capacity, images);
    }
    return result;
}

// counted for layer_debug (an app that skips the wait or release keeps runtime resources alive)
XRAPI_ATTR XrResult XRAPI_CALL xrWaitSwapchainImage(XrSwapchain swapchain, const XrSwapchainImageWaitInfo *info) {
    PFN_xrWaitSwapchainImage fn = (PFN_xrWaitSwapchainImage)lookup(active_instance, "xrWaitSwapchainImage");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (is_standin(swapchain)) return XR_SUCCESS;
    XrResult result = fn(swapchain, info);
    __atomic_add_fetch(&sc_waits, 1, __ATOMIC_RELAXED);
    if (result != XR_SUCCESS) __atomic_add_fetch(&sc_wait_fails, 1, __ATOMIC_RELAXED);
    return result;
}

XRAPI_ATTR XrResult XRAPI_CALL xrReleaseSwapchainImage(XrSwapchain swapchain, const XrSwapchainImageReleaseInfo *info) {
    PFN_xrReleaseSwapchainImage fn = (PFN_xrReleaseSwapchainImage)lookup(active_instance, "xrReleaseSwapchainImage");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (is_standin(swapchain)) return XR_SUCCESS;
    __atomic_add_fetch(&sc_releases, 1, __ATOMIC_RELAXED);
    return fn(swapchain, info);
}

XRAPI_ATTR XrResult XRAPI_CALL xrAcquireSwapchainImage(XrSwapchain swapchain, const XrSwapchainImageAcquireInfo *info,
        uint32_t *index) {
    PFN_xrAcquireSwapchainImage fn = (PFN_xrAcquireSwapchainImage)lookup(active_instance, "xrAcquireSwapchainImage");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (is_standin(swapchain)) {  // adapter-served cube stand-in: one image
        if (!index) return XR_ERROR_VALIDATION_FAILURE;
        *index = 0;
        return XR_SUCCESS;
    }
    if (equirect_emul && emul_is_virtual(swapchain)) {  // adapter-served write-once picture: one image
        if (!index) return XR_ERROR_VALIDATION_FAILURE;
        *index = 0;
        emul_on_acquire(swapchain, 0);
        return XR_SUCCESS;
    }
    XrResult result = fn(swapchain, info, index);
    __atomic_add_fetch(&sc_acquires, 1, __ATOMIC_RELAXED);
    if (XR_SUCCEEDED(result) && index) {
        flip_on_acquire(swapchain, *index);
        emul_on_acquire(swapchain, *index);
        snap_on_acquire(swapchain, *index);
    }
    return result;
}

// Copy scene content only while the oldest acquired image is successfully waited
// and still application-owned. Release itself is forwarded immediately.
static XRAPI_ATTR XrResult XRAPI_CALL surf_composite_release(XrSwapchain handle,const XrSwapchainImageReleaseInfo *info) {
    pthread_mutex_lock(&flip_lock);
    tracked_swapchain *t=find_tracked(handle,0);
    if(t && t->acquired_count && t->waited_count)surf_scene_capture(t,t->acquired[0]);
    pthread_mutex_unlock(&flip_lock);
    XrResult result=equirect_emul?hook_xrReleaseSwapchainImage(handle,info):
        ((PFN_xrReleaseSwapchainImage)lookup(active_instance,"xrReleaseSwapchainImage"))(handle,info);
    if(XR_SUCCEEDED(result)) {
        surf_scene_release_commit(handle);
        pthread_mutex_lock(&flip_lock);t=find_tracked(handle,0);
        if(t && t->acquired_count) {
            memmove(t->acquired,t->acquired+1,(--t->acquired_count)*sizeof(t->acquired[0]));
            if(t->waited_count)t->waited_count--;
        }
        pthread_mutex_unlock(&flip_lock);
    }
    return result;
}
static XRAPI_ATTR XrResult XRAPI_CALL surf_composite_wait(XrSwapchain handle,const XrSwapchainImageWaitInfo *info) {
    XrResult result=equirect_emul?hook_xrWaitSwapchainImage(handle,info):
        ((PFN_xrWaitSwapchainImage)lookup(active_instance,"xrWaitSwapchainImage"))(handle,info);
    if(result==XR_SUCCESS) {
        pthread_mutex_lock(&flip_lock);tracked_swapchain *t=find_tracked(handle,0);
        if(t && t->waited_count<t->acquired_count)t->waited_count++;
        pthread_mutex_unlock(&flip_lock);
    }
    return result;
}

// ---------------------------------------------------------------- tracking diagnostics
static XrSpace view_space_handle = XR_NULL_HANDLE;

// Frame's runtime returns untracked poses from reference spaces created before tracking starts.
// With respace_kick, once the session reaches FOCUSED we deliver synthetic "reference space change
// pending" events so the app (OVRPlugin) recreates its spaces.
static XrSession focused_session = XR_NULL_HANDLE;
static int kicks_pending = -1;  // -1: not yet armed

XRAPI_ATTR XrResult XRAPI_CALL xrPollEvent(XrInstance instance, XrEventDataBuffer *event) {
    mx_audio_initialize();
    PFN_xrPollEvent fn = (PFN_xrPollEvent)lookup(instance, "xrPollEvent");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (emulate_scene && event) {
        pthread_mutex_lock(&scene_lock);
        int got = pop_event(event);
        pthread_mutex_unlock(&scene_lock);
        if (got) return XR_SUCCESS;
    }
    if (sync_guard) pthread_mutex_lock(&sync_lock);
    focus_log_next = fn;  // the runtime's own focus changes are logged, before focus_hold hides any
    XrResult result = focus_hold && event ? focus_hold_poll(focus_log_poll, instance, event)
                                          : focus_log_poll(instance, event);
    if (sync_guard) {
        if (result == XR_SUCCESS && event && session_state_of(event) == XR_SESSION_STATE_FOCUSED)
            sync_resume_at = monotonic_ns() + SYNC_GUARD_PAUSE_NS;
        pthread_mutex_unlock(&sync_lock);
    }
    if (result == XR_SUCCESS && event && (layer_debug || stable_local)) {
        if (event->type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED && layer_debug)
            LOG("layer_debug: session state %d at %.3f", ((const XrEventDataSessionStateChanged *)event)->state,
                monotonic_ns() / 1e9);
        if (event->type == XR_TYPE_EVENT_DATA_REFERENCE_SPACE_CHANGE_PENDING) {
            const XrEventDataReferenceSpaceChangePending *e = (const XrEventDataReferenceSpaceChangePending *)event;
            if (layer_debug) LOG("layer_debug: runtime reference space change pending type=%d", e->referenceSpaceType);
            if (e->referenceSpaceType == XR_REFERENCE_SPACE_TYPE_LOCAL) local_anchor_stale = 1;
        }
    }
    if (!respace_kick || !event) return result;
    if (result == XR_SUCCESS && event->type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
        const XrEventDataSessionStateChanged *e = (const XrEventDataSessionStateChanged *)event;
        if (e->state == XR_SESSION_STATE_FOCUSED && kicks_pending < 0) {
            focused_session = e->session;
            kicks_pending = 3;  // LOCAL, LOCAL_FLOOR, STAGE
        }
        return result;
    }
    if (result == XR_EVENT_UNAVAILABLE && kicks_pending > 0) {
        static const XrReferenceSpaceType types[] = {XR_REFERENCE_SPACE_TYPE_STAGE,
            (XrReferenceSpaceType)1000426000 /* LOCAL_FLOOR_EXT */, XR_REFERENCE_SPACE_TYPE_LOCAL};
        XrEventDataReferenceSpaceChangePending *e = (XrEventDataReferenceSpaceChangePending *)event;
        memset(e, 0, sizeof(*e));
        e->type = XR_TYPE_EVENT_DATA_REFERENCE_SPACE_CHANGE_PENDING;
        e->session = focused_session;
        e->referenceSpaceType = types[--kicks_pending];
        e->changeTime = 0;
        e->poseValid = XR_FALSE;
        e->poseInPreviousSpace.orientation.w = 1.0f;
        LOG("respace_kick: delivered reference space change for type %d", e->referenceSpaceType);
        return XR_SUCCESS;
    }
    return result;
}

XRAPI_ATTR XrResult XRAPI_CALL xrCreateReferenceSpace(XrSession session, const XrReferenceSpaceCreateInfo *info,
        XrSpace *space) {
    PFN_xrCreateReferenceSpace fn = (PFN_xrCreateReferenceSpace)lookup(active_instance, "xrCreateReferenceSpace");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrReferenceSpaceCreateInfo fixed;
    if (stable_local && info && info->referenceSpaceType == XR_REFERENCE_SPACE_TYPE_LOCAL) {
        fixed = *info;
        fixed.poseInReferenceSpace = stable_local_pose(session, info->poseInReferenceSpace);
        info = &fixed;
    }
    XrResult result = fn(session, info, space);
    if (info && space) {
        LOG("xrCreateReferenceSpace type=%d result=%d space=%p", info->referenceSpaceType, result,
            (void *)(uintptr_t)*space);
        if (layer_debug && XR_SUCCEEDED(result)) debug_reference_space(session, info, *space);
        if (XR_SUCCEEDED(result) && info->referenceSpaceType == XR_REFERENCE_SPACE_TYPE_VIEW) view_space_handle = *space;
    }
    return result;
}

XRAPI_ATTR XrResult XRAPI_CALL xrWaitFrame(XrSession session, const XrFrameWaitInfo *info, XrFrameState *state) {
    PFN_xrWaitFrame fn = (PFN_xrWaitFrame)lookup(active_instance, "xrWaitFrame");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrResult result = fn(session, info, state);
    if (XR_SUCCEEDED(result) && state) {
        last_predicted_time = state->predictedDisplayTime;
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        int64_t mono = (int64_t)now.tv_sec * 1000000000ll + now.tv_nsec;
        // predictedDisplayTime is about one display period ahead of "now".
        xr_time_offset = (int64_t)(state->predictedDisplayTime - state->predictedDisplayPeriod) - mono;
        xr_time_calibrated = 1;
        last_display_period = state->predictedDisplayPeriod;
        pose_time_sample(xr_time_offset);
        pose_time_display(state->predictedDisplayTime);
        pose_time_note_offset((long long)(state->predictedDisplayTime - mono));
        if (layer_debug) debug_aim_vs_grip(state->predictedDisplayTime);
    }
    return result;
}

// pose_consistency (GitHub #8, proposed by Klownicle for I Am Cat): the game asks xrLocateViews ~3 times per frame
// for the same display time and got slightly different poses each time (tenths of a degree), so parts of its pipeline
// rendered with different heads: judder. The first fully tracked answer for (session, space, view configuration,
// display time) is kept and repeated; a new display time, another space or a failed/untracked answer is never cached.
#define POSE_CACHE 8
static struct {
    XrSession session; XrSpace space; XrViewConfigurationType config; XrTime time;
    XrViewStateFlags flags; uint32_t count; XrPosef pose[2]; XrFovf fov[2];
} pose_cache[POSE_CACHE];
static int pose_cache_next, pose_cache_hits;
static pthread_mutex_t pose_cache_lock = PTHREAD_MUTEX_INITIALIZER;
#define POSE_TRACKED (XR_VIEW_STATE_ORIENTATION_VALID_BIT | XR_VIEW_STATE_POSITION_VALID_BIT | \
                      XR_VIEW_STATE_ORIENTATION_TRACKED_BIT | XR_VIEW_STATE_POSITION_TRACKED_BIT)

static int pose_cache_lookup(XrSession session, const XrViewLocateInfo *info, XrViewState *state, uint32_t capacity,
                             uint32_t *count, XrView *views) {
    int hit = 0;
    pthread_mutex_lock(&pose_cache_lock);
    for (int i = 0; i < POSE_CACHE && !hit; ++i) {
        if (pose_cache[i].session != session || pose_cache[i].space != info->space ||
            pose_cache[i].config != info->viewConfigurationType || pose_cache[i].time != info->displayTime ||
            !pose_cache[i].count || capacity < pose_cache[i].count)
            continue;
        state->viewStateFlags = pose_cache[i].flags;
        *count = pose_cache[i].count;
        for (uint32_t v = 0; v < pose_cache[i].count; ++v) {  // only the values: the caller's type/next stay
            views[v].pose = pose_cache[i].pose[v];
            views[v].fov = pose_cache[i].fov[v];
        }
        hit = 1;
    }
    if (hit && pose_cache_hits++ == 0) LOG("pose_consistency: repeated query answered with the frame's first poses");
    pthread_mutex_unlock(&pose_cache_lock);
    return hit;
}

static void pose_cache_store(XrSession session, const XrViewLocateInfo *info, const XrViewState *state,
                             uint32_t count, const XrView *views) {
    if (count == 0 || count > 2 || (state->viewStateFlags & POSE_TRACKED) != POSE_TRACKED) return;
    pthread_mutex_lock(&pose_cache_lock);
    int slot = pose_cache_next;
    pose_cache_next = (pose_cache_next + 1) % POSE_CACHE;
    pose_cache[slot].session = session;
    pose_cache[slot].space = info->space;
    pose_cache[slot].config = info->viewConfigurationType;
    pose_cache[slot].time = info->displayTime;
    pose_cache[slot].flags = state->viewStateFlags;
    pose_cache[slot].count = count;
    for (uint32_t v = 0; v < count; ++v) {
        pose_cache[slot].pose[v] = views[v].pose;
        pose_cache[slot].fov[v] = views[v].fov;
    }
    pthread_mutex_unlock(&pose_cache_lock);
}

XRAPI_ATTR XrResult XRAPI_CALL xrLocateViews(XrSession session, const XrViewLocateInfo *info, XrViewState *state,
        uint32_t capacity, uint32_t *count, XrView *views) {
    PFN_xrLocateViews fn = (PFN_xrLocateViews)lookup(active_instance, "xrLocateViews");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrViewLocateInfo moved;
    if (info && pose_time_fix) {
        moved = *info;
        moved.displayTime = pose_time_fixed(info->displayTime);
        info = &moved;
    }
    if (info) pose_time_note("xrLocateViews", (uint64_t)(uintptr_t)info->space, 0, info->displayTime);
    int cacheable = pose_consistency && info && state && count && views && capacity > 0;
    if (cacheable && pose_cache_lookup(session, info, state, capacity, count, views)) return XR_SUCCESS;
    XrResult result = fn(session, info, state, capacity, count, views);
    if (cacheable && result == XR_SUCCESS) pose_cache_store(session, info, state, *count, views);
    if(surface_native && surface_emul && XR_SUCCEEDED(result) && count)
        surf_views_record(session,info,state,*count<capacity?*count:capacity,views);
    if (equirect_emul && XR_SUCCEEDED(result) && info && count && views && state &&
        (state->viewStateFlags & XR_VIEW_STATE_ORIENTATION_VALID_BIT))
        emul_on_locate_views(info->space, info->displayTime, *count < capacity ? *count : capacity, views);
    // respace_kick trigger: ~90 consecutive untracked head poses in a non-VIEW space.
    static int untracked;
    if (respace_kick && XR_SUCCEEDED(result) && state && info && info->space != view_space_handle) {
        int tracked = (state->viewStateFlags & (XR_VIEW_STATE_ORIENTATION_TRACKED_BIT | XR_VIEW_STATE_POSITION_TRACKED_BIT)) != 0;
        untracked = tracked ? 0 : untracked + 1;
        if (untracked == 90 && kicks_pending <= 0) {
            focused_session = session;
            kicks_pending = 3;
            LOG("respace_kick: head pose untracked for 90 frames, asking app to recreate its spaces");
            // Diagnostic: which freshly created space types give tracked poses right now?
            PFN_xrCreateReferenceSpace create = (PFN_xrCreateReferenceSpace)lookup(active_instance, "xrCreateReferenceSpace");
            static const XrReferenceSpaceType probe[] = {XR_REFERENCE_SPACE_TYPE_LOCAL, XR_REFERENCE_SPACE_TYPE_STAGE,
                                                         (XrReferenceSpaceType)1000426000};
            for (size_t k = 0; create && k < 3; ++k) {
                XrReferenceSpaceCreateInfo ci = {XR_TYPE_REFERENCE_SPACE_CREATE_INFO, NULL, probe[k], {{0, 0, 0, 1}, {0, 0, 0}}};
                XrSpace fresh = XR_NULL_HANDLE;
                if (XR_FAILED(create(session, &ci, &fresh))) continue;
                XrViewLocateInfo li = *info;
                li.space = fresh;
                XrViewState st = {XR_TYPE_VIEW_STATE, NULL, 0};
                XrView tmp[2] = {{XR_TYPE_VIEW, NULL, {{0, 0, 0, 1}, {0, 0, 0}}, {0, 0, 0, 0}},
                                 {XR_TYPE_VIEW, NULL, {{0, 0, 0, 1}, {0, 0, 0}}, {0, 0, 0, 0}}};
                uint32_t n = 0;
                XrResult r = fn(session, &li, &st, 2, &n, tmp);
                LOG("probe fresh space type=%d locate=%d flags=0x%llx pos=%.3f,%.3f,%.3f", probe[k], r,
                    (unsigned long long)st.viewStateFlags, tmp[0].pose.position.x, tmp[0].pose.position.y,
                    tmp[0].pose.position.z);
            }
        }
    }
    static int logged;
    if (views && count && *count && logged < 400 && (logged++ % 100) == 0)
        LOG("xrLocateViews space=%p%s result=%d flags=0x%llx view0 pos=%.3f,%.3f,%.3f time=%lld predicted=%lld",
            (void *)(uintptr_t)info->space, info->space == view_space_handle ? "(VIEW)" : "", result,
            (unsigned long long)state->viewStateFlags, views[0].pose.position.x, views[0].pose.position.y,
            views[0].pose.position.z, (long long)info->displayTime, (long long)last_predicted_time);
    return result;
}

// xrBeginFrame: counted for layer_debug. A game that begins a new frame while the previous one is still open
// (never ended) makes the runtime discard it; the Frame's runtime then never recycles that frame's GPU timing
// command buffer (Vader Immortal: ~430 GPU mappings / 20 MB a second). frame_balance ends the open frame first,
// with no layers, as the runtime expects.
static XrTime last_begin_display_time;
XRAPI_ATTR XrResult XRAPI_CALL xrBeginFrame(XrSession session, const XrFrameBeginInfo *info) {
    PFN_xrBeginFrame fn = (PFN_xrBeginFrame)lookup(active_instance, "xrBeginFrame");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (frame_open && frame_balance) {
        PFN_xrEndFrame end = (PFN_xrEndFrame)lookup(active_instance, "xrEndFrame");
        XrFrameEndInfo empty = {XR_TYPE_FRAME_END_INFO, NULL, last_begin_display_time ? last_begin_display_time
                                                                                        : last_predicted_time,
                                XR_ENVIRONMENT_BLEND_MODE_OPAQUE, 0, NULL};
        XrResult r = end ? end(session, &empty) : XR_ERROR_FUNCTION_UNSUPPORTED;
        static int logged;
        if (logged++ < 3) LOG("frame_balance: ended an open frame before xrBeginFrame (result %d)", r);
        __atomic_add_fetch(&frame_balanced, 1, __ATOMIC_RELAXED);
    }
    XrResult result = fn(session, info);
    __atomic_add_fetch(&frame_begins, 1, __ATOMIC_RELAXED);
    if (result == XR_FRAME_DISCARDED) __atomic_add_fetch(&frame_discarded, 1, __ATOMIC_RELAXED);
    if (XR_SUCCEEDED(result)) {
        frame_open = 1;
        last_begin_display_time = last_predicted_time;
    }
    return result;
}

XRAPI_ATTR XrResult XRAPI_CALL xrEndFrame(XrSession session, const XrFrameEndInfo *info) {
    PFN_xrEndFrame fn = (PFN_xrEndFrame)lookup(active_instance, "xrEndFrame");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    frame_open = 0;
    __atomic_add_fetch(&frame_ends, 1, __ATOMIC_RELAXED);
    snap_end_frame(info);
    if(surface_native)++sc_serial;
    if (eye_debug) eye_debug_end_frame(session, info);
    {   // frame pacing statistics every ~5 s: fps and submitted-vs-predicted display time
        static struct timespec start;
        static int frames;
        static long long drift_sum, drift_max;
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (!frames) start = now;
        ++frames;
        long long drift = info ? (long long)(info->displayTime - last_predicted_time) : 0;
        drift_sum += drift < 0 ? -drift : drift;
        if ((drift < 0 ? -drift : drift) > drift_max) drift_max = drift < 0 ? -drift : drift;
        double elapsed = (now.tv_sec - start.tv_sec) + (now.tv_nsec - start.tv_nsec) * 1e-9;
        if (elapsed >= 5.0) {
            LOG("pacing: %.1f fps, displayTime vs predicted: avg %.2f ms, max %.2f ms", frames / elapsed,
                drift_sum / (double)frames / 1e6, drift_max / 1e6);
            if (layer_debug)
                LOG("layer_debug: swapchain images: %d acquired, %d waited (%d not ok), %d released",
                    __atomic_exchange_n(&sc_acquires, 0, __ATOMIC_RELAXED),
                    __atomic_exchange_n(&sc_waits, 0, __ATOMIC_RELAXED),
                    __atomic_exchange_n(&sc_wait_fails, 0, __ATOMIC_RELAXED),
                    __atomic_exchange_n(&sc_releases, 0, __ATOMIC_RELAXED));
            if (layer_debug)
                LOG("layer_debug: frames: %d xrBeginFrame (%d discarded), %d xrEndFrame, %d balanced",
                    __atomic_exchange_n(&frame_begins, 0, __ATOMIC_RELAXED),
                    __atomic_exchange_n(&frame_discarded, 0, __ATOMIC_RELAXED),
                    __atomic_exchange_n(&frame_ends, 0, __ATOMIC_RELAXED),
                    __atomic_exchange_n(&frame_balanced, 0, __ATOMIC_RELAXED));
            if (layer_debug)
                LOG("layer_debug: input: %d xrSyncActions (%d ok, last %d), %d bool reads, %d pressed",
                    __atomic_exchange_n(&input_syncs, 0, __ATOMIC_RELAXED),
                    __atomic_exchange_n(&input_sync_ok, 0, __ATOMIC_RELAXED), input_last_sync_result,
                    __atomic_exchange_n(&input_bool_reads, 0, __ATOMIC_RELAXED),
                    __atomic_exchange_n(&input_bool_true, 0, __ATOMIC_RELAXED));
            pose_time_report();
            frames = 0; drift_sum = drift_max = 0;
        }
    }
    if ((!layer_fix && !swap_eyes && !emulate_passthrough && !flip_quads && !flip_emul && !strip_depth) || !info || !info->layerCount ||
        info->layerCount > 64)
        return fn(session, info);
    if (layer_debug) {  // the submitted layer list (order, flags, eyes, chained structs) whenever its shape changes
        static char last[1024];
        char now[1024];
        size_t used = 0;
        for (uint32_t i = 0; i < info->layerCount && used < sizeof(now) - 64; ++i) {
            const XrCompositionLayerBaseHeader *l = info->layers[i];
            if (!l) continue;
            int eye = -1;
            if (l->type == XR_TYPE_COMPOSITION_LAYER_QUAD) eye = ((const XrCompositionLayerQuad *)l)->eyeVisibility;
            if (l->type == XR_TYPE_COMPOSITION_LAYER_CYLINDER_KHR) eye = ((const XrCompositionLayerCylinderKHR *)l)->eyeVisibility;
            if (l->type == XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR) eye = ((const XrCompositionLayerEquirect2KHR *)l)->eyeVisibility;
            used += (size_t)snprintf(now + used, sizeof(now) - used, " [%d f=0x%llx e=%d", l->type,
                                     (unsigned long long)l->layerFlags, eye);
            if (l->type == XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR) {  // mapping + source size: 360 vs 180, SBS halves
                const XrCompositionLayerEquirect2KHR *q = (const XrCompositionLayerEquirect2KHR *)l;
                used += (size_t)snprintf(now + used, sizeof(now) - used, " h=%.2f u=%.2f l=%.2f r=%.1f rect=%d,%d %dx%d",
                                         q->centralHorizontalAngle, q->upperVerticalAngle, q->lowerVerticalAngle,
                                         q->radius, q->subImage.imageRect.offset.x, q->subImage.imageRect.offset.y,
                                         q->subImage.imageRect.extent.width, q->subImage.imageRect.extent.height);
            }
            for (const XrBaseInStructure *n = (const XrBaseInStructure *)l->next; n && used < sizeof(now) - 32; n = n->next)
                used += (size_t)snprintf(now + used, sizeof(now) - used, " n=%d", n->type);
            used += (size_t)snprintf(now + used, sizeof(now) - used, "]");
        }
        if (strcmp(now, last)) {
            LOG("layer_debug: frame layers blend=%d:%s", info->environmentBlendMode, now);
            snprintf(last, sizeof(last), "%s", now);
        }
    }
    // equirect_emul: the frame's 360 layers are replaced by one emulated projection layer (at the first one's place)
    const XrCompositionLayerBaseHeader *emul_layer_out = NULL;
    int emul_first = layer_fix && emul_active() ? emul_prepare_frame(info, &emul_layer_out) : -1;
    const XrCompositionLayerBaseHeader *kept[64];
    XrCompositionLayerProjection projections[64];
    XrCompositionLayerProjectionView views[64][2];
    XrCompositionLayerQuad quads[64];
    uint32_t count = 0, dropped = 0, swapped = 0, passthrough = 0;
    const XrCompositionLayerBaseHeader *surface_projections[SURF_MAX];
    int surface_projection_count = 0;
    for (uint32_t i = 0; i < info->layerCount; ++i) {
        const XrCompositionLayerBaseHeader *layer = info->layers[i];
        if (!layer) { ++dropped; continue; }
        if (emul_is_equirect(layer)) {
            const XrCompositionLayerBaseHeader *replacement = surface_native ? surf_projection_frame(session, info, layer) : NULL;
            if (replacement) {
                int already = 0;
                for (int k = 0; k < surface_projection_count; ++k) already |= surface_projections[k] == replacement;
                if (!already && surface_projection_count < SURF_MAX) {
                    surface_projections[surface_projection_count++] = replacement;
                    // An opaque native hemisphere covering the guarded stereo
                    // view hides every earlier layer. Cull only that hidden
                    // prefix: video becomes the primary projection, while
                    // later subtitles/menus keep their original order. Fades
                    // and uncovered views use the ordinary layered path.
                    const XrCompositionLayerBaseHeader *combined=NULL;
                    if(surface_native && count==1 && kept[0]->type==XR_TYPE_COMPOSITION_LAYER_PROJECTION && surf_composite_candidate(info,layer))
                        combined=surf_composite_frame(session,info,(const void*)kept[0],replacement);
                    if(combined) {kept[0]=combined;++swapped;}
                    else if (surf_projection_occludes(replacement)) {
                        dropped += count;
                        count = 0;
                        passthrough = 0;
                    }
                    if(!combined)kept[count++] = replacement;
                }
                ++swapped;
                continue;
            }
        }
        if (emulate_passthrough && layer->type == XR_TYPE_COMPOSITION_LAYER_PASSTHROUGH_FB) {
            ++passthrough;  // replaced by the runtime's camera environment below
            continue;
        }
        {
            static struct { int type; const void *key; } seen[48];
            static int nseen;
            const void *key = NULL;
            if (layer->type == XR_TYPE_COMPOSITION_LAYER_QUAD) key = (const void *)(uintptr_t)((const XrCompositionLayerQuad *)layer)->subImage.swapchain;
            if (layer->type == XR_TYPE_COMPOSITION_LAYER_CYLINDER_KHR) key = (const void *)(uintptr_t)((const XrCompositionLayerCylinderKHR *)layer)->subImage.swapchain;
            if (layer->type == XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR) key = (const void *)(uintptr_t)((const XrCompositionLayerEquirect2KHR *)layer)->subImage.swapchain;
            int known = 0;
            for (int k = 0; k < nseen; ++k) known |= seen[k].type == (int)layer->type && seen[k].key == key;
            if (!known && nseen < 48) {
                seen[nseen].type = layer->type; seen[nseen++].key = key;
                int tagged = 0;
                for (const XrBaseInStructure *n = (const XrBaseInStructure *)layer->next; n; n = n->next)
                    tagged |= n->type == 1000040000 && (((const XrCompositionLayerImageLayoutFB *)n)->flags & 1);
                LOG("new layer: type=%d swapchain=%p flip_tag=%d usable=%d", layer->type, key, tagged, layer_usable(layer));
            }
        }
        if (emul_first >= 0 && emul_layer_out && emul_is_equirect(layer)) {
            if ((int)i == emul_first) kept[count++] = emul_layer_out;  // all 360 layers are drawn into it
            ++swapped;
            continue;
        }
        if (layer_fix && cylinder_strips_on && no_cylinder && layer->type == XR_TYPE_COMPOSITION_LAYER_CYLINDER_KHR &&
            runtime_swapchain(((const XrCompositionLayerCylinderKHR *)layer)->subImage.swapchain)) {
            // keep the whole frame within the runtime's layer limit: every later layer still needs a slot
            int budget = 16 - (int)count - (int)(info->layerCount - i - 1);
            int n = cylinder_strips((const XrCompositionLayerCylinderKHR *)layer, &quads[count], budget < 64 - (int)count ? budget : 64 - (int)count);
            if (n > 0) {
                for (int k = 0; k < n; ++k) kept[count + k] = (const XrCompositionLayerBaseHeader *)&quads[count + k];
                count += (uint32_t)n;
                ++swapped;
                continue;
            }
        }
        if (layer && (layer->type == XR_TYPE_COMPOSITION_LAYER_QUAD || layer->type == XR_TYPE_COMPOSITION_LAYER_CYLINDER_KHR) &&
            !surf_prepare_layer(layer->type == XR_TYPE_COMPOSITION_LAYER_QUAD
                                    ? ((const XrCompositionLayerQuad *)layer)->subImage.swapchain
                                    : ((const XrCompositionLayerCylinderKHR *)layer)->subImage.swapchain)) {
            ++dropped;  // an emulated video surface without a frame yet
            continue;
        }
        if (layer_uses_standin(layer)) {  // a cube layer whose swapchain the runtime refused (cube_standin.c)
            static int logged;
            if (logged++ < 3) LOG("cube_standin: dropped a layer (type=%d) that shows a stand-in swapchain", layer->type);
            ++dropped;
            continue;
        }
        if (!layer || (layer_fix && !layer_usable(layer))) { ++dropped; continue; }
        if (layer->type == XR_TYPE_COMPOSITION_LAYER_QUAD) {
            const XrCompositionLayerQuad *flipped = flip_quad(session, (const XrCompositionLayerQuad *)layer, &quads[count]);
            if (flipped) { layer = (const XrCompositionLayerBaseHeader *)flipped; ++swapped; }
        }
        if (flip_quads && layer->type == XR_TYPE_COMPOSITION_LAYER_QUAD) {
            // Rotate 180 degrees about the quad's local X axis: shows the image upright without mirroring it.
            quads[count] = *(const XrCompositionLayerQuad *)layer;
            XrQuaternionf q = quads[count].pose.orientation;           // q * (1,0,0,0)
            quads[count].pose.orientation = (XrQuaternionf){q.w, q.z, -q.y, -q.x};  // (x,y,z,w) of q * (1,0,0 | 0)
            layer = (const XrCompositionLayerBaseHeader *)&quads[count];
            ++swapped;
        }
        int blend_alpha = passthrough && !alpha_blend_failed && layer->type == XR_TYPE_COMPOSITION_LAYER_PROJECTION;
        if (blend_alpha && !(swap_eyes && ((const XrCompositionLayerProjection *)layer)->viewCount == 2)) {
            // Let the camera show through where the app rendered transparent pixels.
            projections[count] = *(const XrCompositionLayerProjection *)layer;
            projections[count].layerFlags |= XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT |
                                             XR_COMPOSITION_LAYER_UNPREMULTIPLIED_ALPHA_BIT;
            layer = (const XrCompositionLayerBaseHeader *)&projections[count];
        }
        if (strip_depth && layer->type == XR_TYPE_COMPOSITION_LAYER_PROJECTION &&
            ((const XrCompositionLayerProjection *)layer)->viewCount == 2) {
            // Drop XrCompositionLayerDepthInfoKHR: bad app depth makes positional reprojection warp the image.
            const XrCompositionLayerProjection *src = (const XrCompositionLayerProjection *)layer;
            if (layer != (const XrCompositionLayerBaseHeader *)&projections[count]) projections[count] = *src;
            views[count][0] = src->views[0];
            views[count][1] = src->views[1];
            views[count][0].next = NULL;
            views[count][1].next = NULL;
            projections[count].views = views[count];
            layer = (const XrCompositionLayerBaseHeader *)&projections[count];
            static int logged;
            if (!logged++) LOG("strip_depth: removed depth info from projection layer");
            ++swapped;
        }
        {
            // XR_KHR_composition_layer_color_scale_bias (1000034000) on the layer itself: logged when its values
            // change (layer_debug); with strip_color_bias the runtime never gets it (Vader Immortal: the Frame's
            // runtime allocates GPU memory for it every frame and never frees it)
            const XrBaseInStructure *head = (const XrBaseInStructure *)layer->next;
            const XrCompositionLayerColorScaleBiasKHR *cb = NULL;
            for (const XrBaseInStructure *n = head; n; n = n->next)
                if (n->type == XR_TYPE_COMPOSITION_LAYER_COLOR_SCALE_BIAS_KHR) cb = (const XrCompositionLayerColorScaleBiasKHR *)n;
            if (cb && layer_debug) {
                static float last[8] = {-1};
                float now[8] = {cb->colorScale.r, cb->colorScale.g, cb->colorScale.b, cb->colorScale.a,
                                cb->colorBias.r, cb->colorBias.g, cb->colorBias.b, cb->colorBias.a};
                static int logged;
                if (memcmp(now, last, sizeof(now)) && logged++ < 100) {
                    LOG("layer_debug: layer type=%d color scale %.2f %.2f %.2f %.2f bias %.2f %.2f %.2f %.2f", layer->type,
                        now[0], now[1], now[2], now[3], now[4], now[5], now[6], now[7]);
                    memcpy(last, now, sizeof(now));
                }
            }
            if (cb && strip_color_bias && (layer->type == XR_TYPE_COMPOSITION_LAYER_PROJECTION ||
                                           layer->type == XR_TYPE_COMPOSITION_LAYER_QUAD)) {
                const XrBaseInStructure *rest = head;  // skip leading color structs; one further down drops the chain
                while (rest && rest->type == XR_TYPE_COMPOSITION_LAYER_COLOR_SCALE_BIAS_KHR) rest = rest->next;
                for (const XrBaseInStructure *n = rest; n; n = n->next)
                    if (n->type == XR_TYPE_COMPOSITION_LAYER_COLOR_SCALE_BIAS_KHR) rest = NULL;
                if (strip_color_bias > 1) rest = NULL;  // 2: the layer's whole extension chain (diagnostics)
                if (layer->type == XR_TYPE_COMPOSITION_LAYER_PROJECTION) {
                    if (layer != (const XrCompositionLayerBaseHeader *)&projections[count])
                        projections[count] = *(const XrCompositionLayerProjection *)layer;
                    projections[count].next = rest;
                    layer = (const XrCompositionLayerBaseHeader *)&projections[count];
                } else {
                    if (layer != (const XrCompositionLayerBaseHeader *)&quads[count])
                        quads[count] = *(const XrCompositionLayerQuad *)layer;
                    quads[count].next = rest;
                    layer = (const XrCompositionLayerBaseHeader *)&quads[count];
                }
                static int logged;
                if (!logged++) LOG("strip_color_bias: removed the color scale/bias from a layer");
                ++swapped;
            }
        }
        if (hide_space_warp && layer->type == XR_TYPE_COMPOSITION_LAYER_PROJECTION &&
            ((const XrCompositionLayerProjection *)layer)->viewCount == 2) {
            // The game may still attach XrCompositionLayerSpaceWarpInfoFB (1000171000) to its views: OVRPort's
            // dispatcher offers XR_FB_space_warp itself even when hidden here. Unlink it (keeping what follows when
            // it heads the chain, else the whole chain) so the runtime never uses the motion vectors.
            const XrCompositionLayerProjection *src = (const XrCompositionLayerProjection *)layer;
            int found = 0;
            for (uint32_t v = 0; v < 2; ++v)
                for (const XrBaseInStructure *n = (const XrBaseInStructure *)src->views[v].next; n; n = n->next)
                    found |= n->type == 1000171000;
            if (found) {
                if (layer != (const XrCompositionLayerBaseHeader *)&projections[count]) projections[count] = *src;
                for (uint32_t v = 0; v < 2; ++v) {
                    views[count][v] = src->views[v];
                    const XrBaseInStructure *head = (const XrBaseInStructure *)src->views[v].next;
                    if (head && head->type == 1000171000) views[count][v].next = head->next;
                    else if (head) views[count][v].next = NULL;
                }
                projections[count].views = views[count];
                layer = (const XrCompositionLayerBaseHeader *)&projections[count];
                static int logged;
                if (!logged++) LOG("hide_space_warp: removed space warp info from the eye images");
                ++swapped;
            }
        }
        if (swap_eyes && layer->type == XR_TYPE_COMPOSITION_LAYER_PROJECTION &&
            ((const XrCompositionLayerProjection *)layer)->viewCount == 2) {
            // Show each eye the image the game rendered for it: keep poses/FOVs, swap sub-images.
            const XrCompositionLayerProjection *cur = (const XrCompositionLayerProjection *)layer;
            XrCompositionLayerProjectionView v0 = cur->views[0], v1 = cur->views[1];
            if (layer != (const XrCompositionLayerBaseHeader *)&projections[count]) projections[count] = *cur;
            views[count][0] = v0;
            views[count][1] = v1;
            views[count][0].subImage = v1.subImage;
            views[count][1].subImage = v0.subImage;
            projections[count].views = views[count];
            if (blend_alpha)
                projections[count].layerFlags |= XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT |
                                                 XR_COMPOSITION_LAYER_UNPREMULTIPLIED_ALPHA_BIT;
            layer = (const XrCompositionLayerBaseHeader *)&projections[count];
            ++swapped;
        }
        if (rect_clamp) {
            // Keep every image rect inside its swapchain: Unity/OVRPlugin can size the eye area a few pixels past
            // the image on some Frames (PowerWash Simulator: rect 268+1656 on a 1920 wide swapchain) and SteamVR
            // then rejects every frame with XR_ERROR_SWAPCHAIN_RECT_INVALID (-25) (GitHub #39).
            uint32_t w, h;
            if (layer->type == XR_TYPE_COMPOSITION_LAYER_PROJECTION &&
                ((const XrCompositionLayerProjection *)layer)->viewCount <= 2) {
                const XrCompositionLayerProjection *cur = (const XrCompositionLayerProjection *)layer;
                int over = 0;
                for (uint32_t v = 0; v < cur->viewCount; ++v) {
                    XrRect2Di r = cur->views[v].subImage.imageRect;
                    if (swapchain_size(cur->views[v].subImage.swapchain, &w, &h) && clamp_rect(&r, w, h)) over = 1;
                }
                if (over) {
                    if (layer != (const XrCompositionLayerBaseHeader *)&projections[count]) projections[count] = *cur;
                    if (projections[count].views != views[count])
                        for (uint32_t v = 0; v < cur->viewCount; ++v) views[count][v] = cur->views[v];
                    for (uint32_t v = 0; v < cur->viewCount; ++v)
                        if (swapchain_size(views[count][v].subImage.swapchain, &w, &h) &&
                            clamp_rect(&views[count][v].subImage.imageRect, w, h)) {
                            static int logged;
                            if (logged++ < 3)
                                LOG("rect_clamp: view %u image rect clamped to %dx%d at %d,%d (swapchain %ux%u)", v,
                                    views[count][v].subImage.imageRect.extent.width,
                                    views[count][v].subImage.imageRect.extent.height,
                                    views[count][v].subImage.imageRect.offset.x,
                                    views[count][v].subImage.imageRect.offset.y, w, h);
                        }
                    projections[count].views = views[count];
                    layer = (const XrCompositionLayerBaseHeader *)&projections[count];
                    ++swapped;
                }
            } else if (layer->type == XR_TYPE_COMPOSITION_LAYER_QUAD) {
                const XrCompositionLayerQuad *q = (const XrCompositionLayerQuad *)layer;
                XrRect2Di r = q->subImage.imageRect;
                if (swapchain_size(q->subImage.swapchain, &w, &h) && clamp_rect(&r, w, h)) {
                    if (layer != (const XrCompositionLayerBaseHeader *)&quads[count]) quads[count] = *q;
                    quads[count].subImage.imageRect = r;
                    layer = (const XrCompositionLayerBaseHeader *)&quads[count];
                    ++swapped;
                    static int logged;
                    if (!logged++) LOG("rect_clamp: quad image rect clamped (swapchain %ux%u)", w, h);
                }
            }
        }
        kept[count++] = layer;
    }
    XrFrameEndInfo fixed = *info;
    fixed.layers = kept;
    fixed.layerCount = count;
    if (passthrough && !alpha_blend_failed) fixed.environmentBlendMode = XR_ENVIRONMENT_BLEND_MODE_ALPHA_BLEND;
    static int warned, failures, passthrough_logged;
    if (dropped && warned++ < 5) LOG("xrEndFrame: dropped %u unusable layer(s)", dropped);
    if (passthrough && !passthrough_logged++) LOG("xrEndFrame: passthrough layer -> ALPHA_BLEND environment");
    static int views_logged;
    if (views_logged < 3) {  // describe the stereo layer layout a few times to debug eye/stereo problems
        for (uint32_t i = 0; i < info->layerCount; ++i) {
            const XrCompositionLayerBaseHeader *l = info->layers[i];
            if (l && l->type == XR_TYPE_COMPOSITION_LAYER_QUAD) {
                const XrCompositionLayerQuad *qd = (const XrCompositionLayerQuad *)l;
                char chain[160] = "";
                size_t used = 0;
                for (const XrBaseInStructure *n = (const XrBaseInStructure *)qd->next; n && used < sizeof(chain) - 16; n = n->next) {
                    int w = snprintf(chain + used, sizeof(chain) - used, " %d", n->type);
                    if (n->type == 1000040000)  // XrCompositionLayerImageLayoutFB
                        w += snprintf(chain + used + w, sizeof(chain) - used - w, "(flags=0x%llx)",
                                      (unsigned long long)((const XrCompositionLayerImageLayoutFB *)n)->flags);
                    used += w;
                }
                LOG("quad layer: flags=0x%llx size=%.2fx%.2f next:%s", (unsigned long long)qd->layerFlags,
                    qd->size.width, qd->size.height, chain);
            }
            if (!l || l->type != XR_TYPE_COMPOSITION_LAYER_PROJECTION) continue;
            const XrCompositionLayerProjection *p = (const XrCompositionLayerProjection *)l;
            for (uint32_t v = 0; v < p->viewCount; ++v) {
                const XrCompositionLayerProjectionView *pv = &p->views[v];
                if (v == 0 && pv->next) LOG("projection view 0 chains struct type %d", ((const XrBaseInStructure *)pv->next)->type);
                LOG("projection view %u: space=%p%s swapchain=%p rect=%d,%d %dx%d array=%u pos=%.3f,%.3f,%.3f fov=%.2f/%.2f",
                    v, (void *)(uintptr_t)p->space, p->space == view_space_handle ? "(VIEW)" : "", (void *)(uintptr_t)pv->subImage.swapchain, pv->subImage.imageRect.offset.x,
                    pv->subImage.imageRect.offset.y, pv->subImage.imageRect.extent.width,
                    pv->subImage.imageRect.extent.height, pv->subImage.imageArrayIndex, pv->pose.position.x,
                    pv->pose.position.y, pv->pose.position.z, pv->fov.angleLeft, pv->fov.angleRight);
            }
            ++views_logged;
        }
    }
    XrResult result = fn(session, (dropped || swapped || passthrough) ? &fixed : info);
    if (result == XR_ERROR_ENVIRONMENT_BLEND_MODE_UNSUPPORTED && passthrough && !alpha_blend_failed) {
        alpha_blend_failed = 1;  // runtime refuses camera blending: resubmit as a normal opaque frame
        LOG("xrEndFrame: ALPHA_BLEND unsupported, passthrough falls back to opaque");
        return xrEndFrame(session, info);
    }
    if (XR_FAILED(result) && failures++ < 3) {  // describe rejected frames to aid debugging
        LOG("xrEndFrame failed %d: blend=%d layers=%u", result, fixed.environmentBlendMode, fixed.layerCount);
        for (uint32_t i = 0; i < fixed.layerCount; ++i)
            LOG("  layer %u type=%d flags=0x%llx", i, fixed.layers[i]->type,
                (unsigned long long)fixed.layers[i]->layerFlags);
    }
    return result;
}

XRAPI_ATTR XrResult XRAPI_CALL xrLocateHandJointsEXT(XrHandTrackerEXT tracker,
        const XrHandJointsLocateInfoEXT *info, XrHandJointLocationsEXT *locations) {
    if (!controller_fix) {
        PFN_xrLocateHandJointsEXT fn = (PFN_xrLocateHandJointsEXT)lookup(active_instance, "xrLocateHandJointsEXT");
        return fn ? fn(tracker, info, locations) : XR_ERROR_FUNCTION_UNSUPPORTED;
    }
    if (!locations) return XR_ERROR_VALIDATION_FAILURE;
    locations->isActive = XR_FALSE;
    for (XrBaseOutStructure *p = locations->next; p; p = p->next)
        if (p->type == XR_TYPE_HAND_TRACKING_AIM_STATE_FB) ((XrHandTrackingAimStateFB *)p)->status = 0;
    return XR_SUCCESS;
}

XRAPI_ATTR XrResult XRAPI_CALL xrGetCurrentInteractionProfile(XrSession session, XrPath user,
        XrInteractionProfileState *state) {
    PFN_xrGetCurrentInteractionProfile fn =
        (PFN_xrGetCurrentInteractionProfile)lookup(active_instance, "xrGetCurrentInteractionProfile");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    XrResult result = fn(session, user, state);
    if (input_diag && XR_SUCCEEDED(result) && state) input_diag_current_profile(user, state->interactionProfile);
    if (layer_debug && active_instance) {  // which device each hand has, as the runtime reports it
        PFN_xrPathToString str = (PFN_xrPathToString)lookup(active_instance, "xrPathToString");
        char who[XR_MAX_PATH_LENGTH] = "?", what[XR_MAX_PATH_LENGTH] = "(none)";
        uint32_t n = 0;
        if (str) {
            str(active_instance, user, sizeof(who), &n, who);
            if (XR_SUCCEEDED(result) && state && state->interactionProfile)
                str(active_instance, state->interactionProfile, sizeof(what), &n, what);
        }
        static int logged;
        if (logged++ < 40) LOG("layer_debug: interaction profile of %s: %s (result %d)", who, what, result);
    }
    if (!controller_fix || XR_FAILED(result) || !state || !state->interactionProfile || !active_instance)
        return result;
    PFN_xrPathToString to_string = (PFN_xrPathToString)lookup(active_instance, "xrPathToString");
    PFN_xrStringToPath to_path = (PFN_xrStringToPath)lookup(active_instance, "xrStringToPath");
    char name[XR_MAX_PATH_LENGTH];
    uint32_t size = 0;
    if (to_string && to_path &&
        XR_SUCCEEDED(to_string(active_instance, state->interactionProfile, sizeof(name), &size, name)) &&
        (strstr(name, "/valve/") || strstr(name, "/khr/generic_controller")))
        to_path(active_instance, "/interaction_profiles/oculus/touch_controller", &state->interactionProfile);
    return result;
}

XRAPI_ATTR XrResult XRAPI_CALL xrEnumerateInstanceExtensionProperties(const char *layer, uint32_t capacity,
        uint32_t *count, XrExtensionProperties *properties) {
    PFN_xrEnumerateInstanceExtensionProperties fn = (PFN_xrEnumerateInstanceExtensionProperties)lookup(
        XR_NULL_HANDLE, "xrEnumerateInstanceExtensionProperties");
    if (!fn) return XR_ERROR_INITIALIZATION_FAILED;
    if (!count) return XR_ERROR_VALIDATION_FAILURE;
    if ((!foveation_fix && !hide_space_warp && !passthrough_emul && !scene_emul && !controller_models) || layer)
        return fn(layer, capacity, count, properties);

    uint32_t total = 0;
    XrResult result = fn(layer, 0, &total, NULL);
    if (XR_FAILED(result)) return result;
    XrExtensionProperties *all = calloc(total + 2 + SCENE_EXTENSION_COUNT, sizeof(*all));
    if (!all) return XR_ERROR_OUT_OF_MEMORY;
    for (uint32_t i = 0; i < total; ++i) all[i].type = XR_TYPE_EXTENSION_PROPERTIES;
    result = fn(layer, total, &total, all);
    if (XR_FAILED(result)) { free(all); return result; }

    uint32_t kept = 0;
    for (uint32_t i = 0; i < total; ++i) {
        if (!strcmp(all[i].extensionName, "XR_FB_passthrough")) runtime_has_fb_passthrough = 1;
        if (!strcmp(all[i].extensionName, XR_FB_RENDER_MODEL_EXTENSION_NAME)) runtime_has_render_model = 1;
        if (foveation_fix && strstr(all[i].extensionName, "foveation")) continue;
        if (hide_space_warp && !strcmp(all[i].extensionName, "XR_FB_space_warp")) continue;
        if (properties && kept < capacity) properties[kept] = all[i];
        ++kept;
    }
    if (scene_emul) {
        for (size_t e = 0; e < SCENE_EXTENSION_COUNT; ++e) {
            int present = 0;
            for (uint32_t i = 0; i < total; ++i) present |= !strcmp(all[i].extensionName, scene_extensions[e]);
            if (present) continue;
            XrExtensionProperties fake = {XR_TYPE_EXTENSION_PROPERTIES, NULL, "", 1};
            snprintf(fake.extensionName, sizeof(fake.extensionName), "%s", scene_extensions[e]);
            if (properties && kept < capacity) properties[kept] = fake;
            ++kept;
        }
    }
    if (passthrough_emul && !runtime_has_fb_passthrough) {
        XrExtensionProperties fake = {XR_TYPE_EXTENSION_PROPERTIES, NULL, "XR_FB_passthrough", 4};
        if (properties && kept < capacity) properties[kept] = fake;
        ++kept;
    }
    if (controller_models && render_models_available && !runtime_has_render_model) {
        XrExtensionProperties fake = {XR_TYPE_EXTENSION_PROPERTIES, NULL, XR_FB_RENDER_MODEL_EXTENSION_NAME,
                                      XR_FB_render_model_SPEC_VERSION};
        if (properties && kept < capacity) properties[kept] = fake;
        ++kept;
    }
    free(all);
    *count = kept;
    return (capacity && capacity < kept) ? XR_ERROR_SIZE_INSUFFICIENT : XR_SUCCESS;
}

XRAPI_ATTR XrResult XRAPI_CALL xrEnumerateApiLayerProperties(uint32_t capacity, uint32_t *count,
        XrApiLayerProperties *properties) {
    PFN_xrEnumerateApiLayerProperties fn =
        (PFN_xrEnumerateApiLayerProperties)lookup(XR_NULL_HANDLE, "xrEnumerateApiLayerProperties");
    return fn ? fn(capacity, count, properties) : XR_ERROR_INITIALIZATION_FAILED;
}

// For native/xrshim: emulated functions that overport's dispatcher doesn't know (it never asks us for them).
__attribute__((visibility("default"))) PFN_xrVoidFunction framebridge_extension_proc(const char *name) {
    pthread_once(&init_once, initialize);
    if(surface_native && name){
#define NATIVE_PROC(fn) if(!strcmp(name,#fn))return (PFN_xrVoidFunction)fn;
        NATIVE_PROC(xrGetVulkanGraphicsDeviceKHR)
        NATIVE_PROC(xrGetVulkanDeviceExtensionsKHR)
        NATIVE_PROC(xrCreateVulkanDeviceKHR)
        NATIVE_PROC(xrCreateVulkanInstanceKHR)
#undef NATIVE_PROC
    }
    return name ? render_model_emulation(name) : NULL;
}

XRAPI_ATTR XrResult XRAPI_CALL xrGetInstanceProcAddr(XrInstance instance, const char *name,
        PFN_xrVoidFunction *function) {
    pthread_once(&init_once, initialize);
    if (!next_gipa) return XR_ERROR_INITIALIZATION_FAILED;
    if (!name || !function) return XR_ERROR_VALIDATION_FAILURE;
#define HOOK(fn) if (!strcmp(name, #fn)) { *function = (PFN_xrVoidFunction)fn; return XR_SUCCESS; }
    if (!strcmp(name, "xrConvertTimespecTimeToTimeKHR") || !strcmp(name, "xrConvertTimeToTimespecTimeKHR")) {
        PFN_xrVoidFunction real = NULL;
        next_gipa(instance, name, &real);
        if (!strcmp(name, "xrConvertTimespecTimeToTimeKHR")) {
            runtime_timespec_to_time = (PFN_xrConvertTimespecTimeToTimeKHR)real;
            *function = (PFN_xrVoidFunction)emu_timespec_to_time;
        } else {
            runtime_time_to_timespec = (PFN_xrConvertTimeToTimespecTimeKHR)real;
            *function = (PFN_xrVoidFunction)emu_time_to_timespec;
        }
        return XR_SUCCESS;
    }
    PFN_xrVoidFunction emulated = passthrough_emulation(name);
    if (!emulated) emulated = scene_emulation(name);
    if (!emulated) emulated = render_model_emulation(name);
    if (!emulated && emulate_scene && !strcmp(name, "xrLocateSpacesKHR")) emulated = (PFN_xrVoidFunction)emu_locate_spaces_khr;
    if (emulated) { *function = emulated; return XR_SUCCESS; }
    HOOK(xrGetSystemProperties)
    HOOK(xrApplyHapticFeedback)
    HOOK(xrCreateReferenceSpace)
    HOOK(xrLocateViews)
    HOOK(xrPollEvent)
    HOOK(xrWaitFrame)
    HOOK(xrCreateSession)
    HOOK(xrEnumerateSwapchainImages)
    HOOK(xrAcquireSwapchainImage)
    HOOK(xrLocateSpace)
    HOOK(xrLocateSpaces)
    HOOK(xrDestroySpace)
    HOOK(xrCreateInstance)
    HOOK(xrEnumerateViewConfigurationViews)
    HOOK(xrCreateSwapchain)
    HOOK(xrDestroySwapchain)
    HOOK(xrEndFrame)
    HOOK(xrBeginFrame)
    HOOK(xrLocateHandJointsEXT)
    HOOK(xrGetCurrentInteractionProfile)
    HOOK(xrEnumerateInstanceExtensionProperties)
    // Per-game hooks: only installed when their setting is on, so other games run through exactly the same calls.
#define HOOK_AS(fn, impl) if (!strcmp(name, #fn)) { *function = (PFN_xrVoidFunction)impl; return XR_SUCCESS; }
    if (surface_native) {
        HOOK(xrGetVulkanGraphicsDeviceKHR)
        HOOK(xrGetVulkanDeviceExtensionsKHR)
        HOOK(xrCreateVulkanDeviceKHR)
        HOOK(xrCreateVulkanInstanceKHR)
        HOOK_AS(xrReleaseSwapchainImage, surf_composite_release)
        HOOK_AS(xrWaitSwapchainImage, surf_composite_wait)
    }
    if (equirect_emul) {
        HOOK_AS(xrReleaseSwapchainImage, hook_xrReleaseSwapchainImage)
        HOOK_AS(xrWaitSwapchainImage, hook_xrWaitSwapchainImage)
        HOOK_AS(xrDestroySession, hook_xrDestroySession)
    }
    if (layer_debug || refresh_rate > 0 || equirect_emul) HOOK_AS(xrBeginSession, hook_xrBeginSession)
    if (layer_debug || refresh_rate > 0) {
        HOOK_AS(xrRequestDisplayRefreshRateFB, hook_request_refresh_rate)
    }
    if (surface_emul) HOOK_AS(xrCreateSwapchainAndroidSurfaceKHR, hook_xrCreateSwapchainAndroidSurfaceKHR)
    if (eye_debug || release_wait) HOOK_AS(xrReleaseSwapchainImage, eye_hook_xrReleaseSwapchainImage)
    if (layer_debug || cube_standin) {  // counters + cube stand-ins; every other wait/release hook above takes precedence
        HOOK(xrWaitSwapchainImage)
        HOOK(xrReleaseSwapchainImage)
    }
    if (cube_standin && (!strcmp(name, "xrUpdateSwapchainFB") || !strcmp(name, "xrGetSwapchainStateFB"))) {
        XrResult result = next_gipa(instance, name, function);  // only wrapped where the runtime has it
        if (XR_FAILED(result) || !*function) return result;
        if (!strcmp(name, "xrUpdateSwapchainFB")) {
            real_update_swapchain = (PFN_xrUpdateSwapchainFB)*function;
            *function = (PFN_xrVoidFunction)standin_update_swapchain;
        } else {
            real_get_swapchain_state = (PFN_xrGetSwapchainStateFB)*function;
            *function = (PFN_xrVoidFunction)standin_get_swapchain_state;
        }
        return result;
    }
    if (sync_guard || layer_debug || input_diag) HOOK_AS(xrSyncActions, hook_xrSyncActions)
    if (layer_debug) HOOK_AS(xrGetActionStateBoolean, hook_xrGetActionStateBoolean)
    if (layer_debug || aim_correction_on() || profile_remap || input_diag || proximity_emul)
        HOOK_AS(xrSuggestInteractionProfileBindings, hook_xrSuggestInteractionProfileBindings)
    if (proximity_emul) HOOK_AS(xrCreateAction, hook_xrCreateAction)
    if (layer_debug || aim_correction_on()) {
        HOOK_AS(xrCreateActionSpace, hook_xrCreateActionSpace)
    }
#undef HOOK_AS
#undef HOOK
    if (input_diag) {  // logs only: wraps a few input/haptics/perf calls, notes lookups the runtime can't answer
        PFN_xrVoidFunction diag = input_diag_hook(name);
        XrResult result = next_gipa(instance, name, function);
        input_diag_lookup(instance, name, result, *function);
        if (diag && XR_SUCCEEDED(result) && *function) *function = diag;
        return result;
    }
    return next_gipa(instance, name, function);
}

// ---------------------------------------------------------------------------
// Every other symbol exported by overport's generic loader is forwarded
// unchanged (see forwarders.S), so callers that dlsym() entry points directly
// keep working. Unresolved entries return XR_ERROR_FUNCTION_UNSUPPORTED.
__attribute__((visibility("hidden"))) XrResult frame_unsupported(void) { return XR_ERROR_FUNCTION_UNSUPPORTED; }

#define FORWARD(fn) extern void *fwd_##fn;
#include "forwarders.inc"
#undef FORWARD

__attribute__((constructor)) static void resolve_forwarders(void) {
    pthread_once(&init_once, initialize);
    if (!loader) return;
    void *p;
#define FORWARD(fn) if ((p = dlsym(loader, #fn))) fwd_##fn = p;
#include "forwarders.inc"
#undef FORWARD
}
