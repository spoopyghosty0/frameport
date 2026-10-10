// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c (after flip_vk.c). Per-game diagnostics for "one eye looks wrong" reports
// (eye_debug=1; everything also goes to framebridge.log in the app's external files folder):
//  * every frame's projection layer: each eye's submitted pose vs. a fresh xrLocateViews at the frame's display time
//    (game-side pose errors), the left/right eye relation (eye distance, relative rotation: must stay constant),
//    and how long before xrEndFrame each eye's image was released (images handed over too early/late);
//    summarised every 5 s, the first frames in full.
//  * release_wait: before an image is released, wait for the app's GPU work on the session's queue (Vulkan) or
//    glFinish (GLES), for games that release images whose rendering hasn't finished. 2 = only after the first 60 s
//    of frames, so one headset session compares both (log "release_wait now ON").

#define EYE_TRACKED 16

static struct { XrSwapchain handle; int64_t released_ns; } eye_released[EYE_TRACKED];
static pthread_mutex_t eye_lock = PTHREAD_MUTEX_INITIALIZER;
static int64_t eye_first_frame_ns;

static int release_wait_active(void) {
    if (release_wait == 1) return 1;
    if (release_wait != 2 || !eye_first_frame_ns) return 0;
    static int announced;
    int on = monotonic_ns() - eye_first_frame_ns > 60000000000ll;
    if (on && !announced++) LOG("eye_debug: release_wait now ON (A/B: the first 60 s ran without it)");
    return on;
}

static void eye_wait_for_gpu(void) {
    if (vk.device) {
        static PFN_vkQueueWaitIdle wait_idle;
        if (!wait_idle && vk_ready()) wait_idle = (PFN_vkQueueWaitIdle)vk.GetDeviceProcAddr(vk.device, "vkQueueWaitIdle");
        if (wait_idle && vk.queue) wait_idle(vk.queue);
    } else {
        static void (*finish)(void);
        if (!finish) finish = (void (*)(void))dlsym(RTLD_DEFAULT, "glFinish");
        if (finish) finish();
    }
}

static XRAPI_ATTR XrResult XRAPI_CALL eye_hook_xrReleaseSwapchainImage(XrSwapchain swapchain,
                                                                       const XrSwapchainImageReleaseInfo *info) {
    PFN_xrReleaseSwapchainImage fn = (PFN_xrReleaseSwapchainImage)lookup(active_instance, "xrReleaseSwapchainImage");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (is_standin(swapchain)) return XR_SUCCESS;
    if (release_wait_active()) eye_wait_for_gpu();
    XrResult result = fn(swapchain, info);
    if (eye_debug) {
        int64_t now = monotonic_ns();
        pthread_mutex_lock(&eye_lock);
        int slot = -1;
        for (int i = 0; i < EYE_TRACKED && slot < 0; ++i)
            if (eye_released[i].handle == swapchain) slot = i;
        for (int i = 0; i < EYE_TRACKED && slot < 0; ++i)
            if (!eye_released[i].handle) slot = i;
        if (slot < 0) slot = (int)(now % EYE_TRACKED);
        eye_released[slot].handle = swapchain;
        eye_released[slot].released_ns = now;
        pthread_mutex_unlock(&eye_lock);
    }
    return result;
}

static int64_t eye_release_age_ns(XrSwapchain swapchain, int64_t now) {
    int64_t age = -1;
    pthread_mutex_lock(&eye_lock);
    for (int i = 0; i < EYE_TRACKED; ++i)
        if (eye_released[i].handle == swapchain) age = now - eye_released[i].released_ns;
    pthread_mutex_unlock(&eye_lock);
    return age;
}

static float eye_angle_deg(XrQuaternionf a, XrQuaternionf b) {
    float d = fabsf(a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w);
    return d >= 1.0f ? 0.0f : 2.0f * acosf(d) * 57.29578f;
}

static float eye_dist_mm(XrVector3f a, XrVector3f b) {
    float x = a.x - b.x, y = a.y - b.y, z = a.z - b.z;
    return sqrtf(x * x + y * y + z * z) * 1000.0f;
}

static void eye_debug_end_frame(XrSession session, const XrFrameEndInfo *info) {
    if (!info) return;
    const XrCompositionLayerProjection *proj = NULL;
    for (uint32_t i = 0; i < info->layerCount && !proj; ++i)
        if (info->layers[i] && info->layers[i]->type == XR_TYPE_COMPOSITION_LAYER_PROJECTION &&
            ((const XrCompositionLayerProjection *)info->layers[i])->viewCount == 2)
            proj = (const XrCompositionLayerProjection *)info->layers[i];
    if (!proj) return;
    int64_t now = monotonic_ns();
    if (!eye_first_frame_ns) eye_first_frame_ns = now;
    static struct {
        int frames, located;
        float pos_err[2], rot_err[2], pos_err_max[2], rot_err_max[2];  // submitted vs. fresh, per eye
        float ipd_min, ipd_max, rel_rot_max, step_max[2];               // eye relation; frame-to-frame jump per eye
        float age_min[2], age_max[2];                                   // ms from release to xrEndFrame
        int64_t window;
    } st;
    static XrPosef last[2];
    static int have_last;
    static int detailed;
    if (!st.window) { st.window = now; st.ipd_min = 1e9f; st.age_min[0] = st.age_min[1] = 1e9f; }
    // a fresh location of the same views at the frame's display time, in the layer's space
    XrView fresh[2] = {{XR_TYPE_VIEW, NULL}, {XR_TYPE_VIEW, NULL}};
    XrViewState vs = {XR_TYPE_VIEW_STATE, NULL};
    uint32_t n = 0;
    PFN_xrLocateViews locate = (PFN_xrLocateViews)lookup(active_instance, "xrLocateViews");
    XrViewLocateInfo li = {XR_TYPE_VIEW_LOCATE_INFO, NULL, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO, info->displayTime,
                           proj->space};
    int located = locate && XR_SUCCEEDED(locate(session, &li, &vs, 2, &n, fresh)) && n == 2 &&
                  (vs.viewStateFlags & XR_VIEW_STATE_POSITION_VALID_BIT);
    const XrCompositionLayerProjectionView *v = proj->views;
    float ipd = eye_dist_mm(v[0].pose.position, v[1].pose.position);
    float rel = eye_angle_deg(v[0].pose.orientation, v[1].pose.orientation);
    ++st.frames;
    if (ipd < st.ipd_min) st.ipd_min = ipd;
    if (ipd > st.ipd_max) st.ipd_max = ipd;
    if (rel > st.rel_rot_max) st.rel_rot_max = rel;
    for (int e = 0; e < 2; ++e) {
        int64_t age = eye_release_age_ns(v[e].subImage.swapchain, now);
        float ms = age < 0 ? -1.0f : age / 1e6f;
        if (ms < st.age_min[e]) st.age_min[e] = ms;
        if (ms > st.age_max[e]) st.age_max[e] = ms;
        if (located) {
            float pe = eye_dist_mm(v[e].pose.position, fresh[e].pose.position);
            float re = eye_angle_deg(v[e].pose.orientation, fresh[e].pose.orientation);
            st.pos_err[e] += pe;
            st.rot_err[e] += re;
            if (pe > st.pos_err_max[e]) st.pos_err_max[e] = pe;
            if (re > st.rot_err_max[e]) st.rot_err_max[e] = re;
        }
        if (have_last) {
            float step = eye_angle_deg(last[e].orientation, v[e].pose.orientation);
            if (step > st.step_max[e]) st.step_max[e] = step;
        }
        last[e] = v[e].pose;
    }
    have_last = 1;
    if (located) ++st.located;
    if (detailed < 4) {
        ++detailed;
        for (int e = 0; e < 2; ++e)
            LOG("eye_debug: eye %d swapchain=%p rect=%d,%d %dx%d array=%u pos=%.4f,%.4f,%.4f rot=%.4f,%.4f,%.4f,%.4f "
                "fov=%.3f/%.3f/%.3f/%.3f fresh pos=%.4f,%.4f,%.4f", e, (void *)(uintptr_t)v[e].subImage.swapchain,
                v[e].subImage.imageRect.offset.x, v[e].subImage.imageRect.offset.y, v[e].subImage.imageRect.extent.width,
                v[e].subImage.imageRect.extent.height, v[e].subImage.imageArrayIndex, v[e].pose.position.x,
                v[e].pose.position.y, v[e].pose.position.z, v[e].pose.orientation.x, v[e].pose.orientation.y,
                v[e].pose.orientation.z, v[e].pose.orientation.w, v[e].fov.angleLeft, v[e].fov.angleRight,
                v[e].fov.angleUp, v[e].fov.angleDown, fresh[e].pose.position.x, fresh[e].pose.position.y,
                fresh[e].pose.position.z);
    }
    if (now - st.window > 5000000000ll) {
        int l = st.located ? st.located : 1;
        LOG("eye_debug: %d frames | eye distance %.1f-%.1f mm, eye-to-eye rotation max %.2f deg | vs fresh pose: "
            "L avg %.1f mm/%.2f deg max %.1f mm/%.2f deg, R avg %.1f mm/%.2f deg max %.1f mm/%.2f deg (%d located) | "
            "largest frame step L %.2f R %.2f deg | release->end L %.1f-%.1f ms R %.1f-%.1f ms | release_wait %s",
            st.frames, st.ipd_min, st.ipd_max, st.rel_rot_max, st.pos_err[0] / l, st.rot_err[0] / l, st.pos_err_max[0],
            st.rot_err_max[0], st.pos_err[1] / l, st.rot_err[1] / l, st.pos_err_max[1], st.rot_err_max[1], st.located,
            st.step_max[0], st.step_max[1], st.age_min[0], st.age_max[0], st.age_min[1], st.age_max[1],
            release_wait_active() ? "on" : "off");
        memset(&st, 0, sizeof(st));
    }
}
