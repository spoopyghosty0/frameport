// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c (after layer_emul_gl.c, whose GL entry points it shares). cube_standin (default on):
// the Frame's runtime has no cube layers (XR_KHR_composition_layer_cube) and refuses cube swapchains (faceCount 6)
// with XR_ERROR_RUNTIME_FAILURE. OVRPlugin doesn't check: it goes on with "CreateSwapchain for eye 0: 0x0, 0 stages"
// and writes past its empty image list in the next ovrp_EndFrame4 (Unity's render thread SIGSEGV in memset, e.g.
// Budget Cuts Ultimate's cube-map overlay, GitHub #107). Only when the runtime refused one, a cube swapchain is served
// by the adapter instead: one GL cube-map texture (the format, size and mip levels asked for) in the app's current
// GLES context, acquire/wait/release succeed at once. The app renders or copies into it as usual; layers that use it
// are dropped in xrEndFrame (the runtime couldn't show them anyway). Vulkan sessions (no current EGL context) keep
// the runtime's error.
#define STANDIN_MAX 8
static struct { XrSwapchain handle; GLuint tex; } standins[STANDIN_MAX];
static pthread_mutex_t standin_lock = PTHREAD_MUTEX_INITIALIZER;

static int standin_find(XrSwapchain handle) {
    if (!handle) return -1;
    int slot = -1;
    pthread_mutex_lock(&standin_lock);
    for (int i = 0; i < STANDIN_MAX && slot < 0; ++i)
        if (standins[i].handle == handle) slot = i;
    pthread_mutex_unlock(&standin_lock);
    return slot;
}

static int is_standin(XrSwapchain handle) { return cube_standin && standin_find(handle) >= 0; }

// After the runtime refused a swapchain: serve a cube stand-in if it was a cube swapchain in a GLES context.
static XrResult standin_after_failure(const XrSwapchainCreateInfo *info, XrSwapchain *out, XrResult result) {
    if (XR_SUCCEEDED(result) || !cube_standin || !info || !out || info->faceCount != 6) return result;
    if (info->width != info->height || !info->width || info->arraySize > 1) return result;
    if (!p_eglCreateContext && !emul_load_gl()) { LOG("cube_standin: no GLES, the runtime's error stays"); return result; }
    if (p_eglGetCurrentContext() == EGL_NO_CONTEXT) {
        LOG("cube_standin: no current GLES context (Vulkan?), the runtime's error stays");
        return result;
    }
    int slot = -1;
    pthread_mutex_lock(&standin_lock);
    for (int i = 0; i < STANDIN_MAX && slot < 0; ++i)
        if (!standins[i].handle) slot = i;
    if (slot >= 0) standins[slot].handle = (XrSwapchain)1;  // reserved
    pthread_mutex_unlock(&standin_lock);
    if (slot < 0) { LOG("cube_standin: too many cube swapchains, the runtime's error stays"); return result; }
    GLsizei levels = info->mipCount > 1 ? (GLsizei)info->mipCount : 1;
    for (int i = 0; i < 8 && p_glGetError() != GL_NO_ERROR; ++i) {}
    GLint previous = 0;
    p_glGetIntegerv(GL_TEXTURE_BINDING_CUBE_MAP, &previous);
    GLuint tex = 0;
    p_glGenTextures(1, &tex);
    p_glBindTexture(GL_TEXTURE_CUBE_MAP, tex);
    p_glTexStorage2D(GL_TEXTURE_CUBE_MAP, levels, (GLenum)info->format, (GLsizei)info->width, (GLsizei)info->height);
    GLenum err = p_glGetError();
    p_glBindTexture(GL_TEXTURE_CUBE_MAP, (GLuint)previous);  // the app's binding, untouched
    if (err != GL_NO_ERROR) {
        p_glDeleteTextures(1, &tex);
        pthread_mutex_lock(&standin_lock);
        standins[slot].handle = XR_NULL_HANDLE;
        pthread_mutex_unlock(&standin_lock);
        LOG("cube_standin: couldn't make a %ux%u cube map (GL 0x%x), the runtime's error stays", info->width,
            info->height, err);
        return result;
    }
    XrSwapchain handle = FAKE_HANDLE(XrSwapchain);
    pthread_mutex_lock(&standin_lock);
    standins[slot].handle = handle;
    standins[slot].tex = tex;
    pthread_mutex_unlock(&standin_lock);
    *out = handle;
    LOG("cube_standin: runtime refused a %ux%u cube swapchain (result=%d): served by FrameBridge (GL cube map %u, "
        "%d levels); its cube layers are dropped", info->width, info->height, result, tex, (int)levels);
    return XR_SUCCESS;
}

static XrResult standin_enumerate(XrSwapchain handle, uint32_t capacity, uint32_t *count, XrSwapchainImageBaseHeader *images) {
    if (!count) return XR_ERROR_VALIDATION_FAILURE;
    *count = 1;
    if (!capacity) return XR_SUCCESS;
    if (!images) return XR_ERROR_VALIDATION_FAILURE;
    if (images->type != XR_TYPE_SWAPCHAIN_IMAGE_OPENGL_ES_KHR) return XR_ERROR_VALIDATION_FAILURE;
    int slot = standin_find(handle);
    ((emul_gles_image *)images)[0].image = slot >= 0 ? standins[slot].tex : 0;
    return XR_SUCCESS;
}

static void standin_destroy(XrSwapchain handle) {
    pthread_mutex_lock(&standin_lock);
    for (int i = 0; i < STANDIN_MAX; ++i)
        if (standins[i].handle == handle) {
            if (p_eglGetCurrentContext && p_eglGetCurrentContext() != EGL_NO_CONTEXT) p_glDeleteTextures(1, &standins[i].tex);
            standins[i].handle = XR_NULL_HANDLE;
            standins[i].tex = 0;
        }
    pthread_mutex_unlock(&standin_lock);
}

// A layer that shows a stand-in: never passed to the runtime (whatever layer_fix says).
static int layer_uses_standin(const XrCompositionLayerBaseHeader *layer) {
    if (!cube_standin || !layer) return 0;
    switch (layer->type) {
    case XR_TYPE_COMPOSITION_LAYER_CUBE_KHR:
        return is_standin(((const XrCompositionLayerCubeKHR *)layer)->swapchain);
    case XR_TYPE_COMPOSITION_LAYER_QUAD:
        return is_standin(((const XrCompositionLayerQuad *)layer)->subImage.swapchain);
    case XR_TYPE_COMPOSITION_LAYER_CYLINDER_KHR:
        return is_standin(((const XrCompositionLayerCylinderKHR *)layer)->subImage.swapchain);
    case XR_TYPE_COMPOSITION_LAYER_EQUIRECT_KHR:
        return is_standin(((const XrCompositionLayerEquirectKHR *)layer)->subImage.swapchain);
    case XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR:
        return is_standin(((const XrCompositionLayerEquirect2KHR *)layer)->subImage.swapchain);
    case XR_TYPE_COMPOSITION_LAYER_PROJECTION: {
        const XrCompositionLayerProjection *p = (const XrCompositionLayerProjection *)layer;
        for (uint32_t v = 0; v < p->viewCount; ++v)
            if (is_standin(p->views[v].subImage.swapchain)) return 1;
        return 0;
    }
    default:
        return 0;
    }
}

// XR_FB_swapchain_update_state on a stand-in: nothing to update (only hooked when the runtime has the function).
static PFN_xrUpdateSwapchainFB real_update_swapchain;
static PFN_xrGetSwapchainStateFB real_get_swapchain_state;
static XRAPI_ATTR XrResult XRAPI_CALL standin_update_swapchain(XrSwapchain swapchain, const XrSwapchainStateBaseHeaderFB *state) {
    if (is_standin(swapchain)) return XR_SUCCESS;
    return real_update_swapchain ? real_update_swapchain(swapchain, state) : XR_ERROR_FUNCTION_UNSUPPORTED;
}
static XRAPI_ATTR XrResult XRAPI_CALL standin_get_swapchain_state(XrSwapchain swapchain, XrSwapchainStateBaseHeaderFB *state) {
    if (is_standin(swapchain)) return XR_ERROR_VALIDATION_FAILURE;  // no state to report (apps treat it as "unsupported")
    return real_get_swapchain_state ? real_get_swapchain_state(swapchain, state) : XR_ERROR_FUNCTION_UNSUPPORTED;
}
