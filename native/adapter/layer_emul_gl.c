// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c. equirect_emul (off by default, GLES sessions only): shows 360° layers
// (XR_KHR_composition_layer_equirect / equirect2, e.g. video players' theatres and 360° videos), which the Frame
// runtime lacks, as one adapter-owned projection layer in their place.
//
//  * Why a projection layer: the Frame's SteamVR composites quad layers like overlays, on top of every projection
//    layer whatever the submission order. A background made of quads covered the app's own projection layer (4XVR's
//    balcony, controllers and 360° videos). Projection layers keep their order, so the app's projection layer, which
//    it submits after (alpha-blended), stays on top.
//  * Two cached stages on a worker thread with its own EGL context, shared with the app's:
//      1. each 360° source image is resampled into a private GL cube map, only when it changes (a static theatre once,
//         a 360° video once per video frame, at most equirect_fps per second);
//      2. the projection image (both eyes) is drawn from the cube maps for every app frame, with exactly the views that
//         frame is shown with (the app's own projection views, else the runtime's views at the frame's display time):
//         xrEndFrame hands the job to the worker and waits only until the worker has submitted its (small) draw, never
//         for the GPU. The Frame does not reproject this layer from its own pose: images drawn for another moment (head
//         turns only; or drawn ahead from xrWaitFrame and sometimes late) made the background wobble.
//  * Nothing is drawn on the app's render thread. The app thread only adds a GL fence when it releases a source image
//    (so the worker waits for the app's GPU work) and, if it re-acquires an image the worker is still reading, waits
//    for that read to finish (bounded).
//  * Until the first projection image is ready the 360° layers are dropped exactly as without the setting. Any
//    EGL/GL/XR failure disables the emulation for the session (logged) and falls back to dropping them.
//  * Never Vulkan: in Vulkan sessions the setting does nothing.
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES3/gl3.h>

// Mirrors of XrGraphicsBindingOpenGLESAndroidKHR / XrSwapchainImageOpenGLESKHR (openxr_platform.h is already
// included for Vulkan by flip_vk.c, without the GLES types).
typedef struct { XrStructureType type; const void *next; EGLDisplay display; EGLConfig config; EGLContext context; } emul_gles_binding;
typedef struct { XrStructureType type; void *next; uint32_t image; } emul_gles_image;

#define EMUL_MAX_SOURCES 128
#define EMUL_MAX_LAYERS 8          // 360° sources with a cube map
#define EMUL_MAX_PER_EYE 4         // 360° layers drawn into one eye of the projection image
#define EMUL_MAX_IMAGES 8
#define EMUL_FRAME_WAIT_NS 6000000ll  // xrEndFrame waits at most this long for the worker's draw (else: previous image)
#define EMUL_IDLE_NS 500000000ll   // a 360° source not submitted for this long gives its cube map back (GPU memory)

// ---- GL / EGL entry points (resolved at run time: the adapter doesn't link GL, Vulkan games never load it)
#define EMUL_EGL_FUNCS(X) X(eglCreateContext) X(eglDestroyContext) X(eglMakeCurrent) X(eglQueryContext) \
    X(eglChooseConfig) X(eglCreatePbufferSurface) X(eglDestroySurface) X(eglGetCurrentContext) X(eglGetError) \
    X(eglQueryString)
#define EMUL_GL_FUNCS(X) X(glFenceSync) X(glDeleteSync) X(glWaitSync) X(glClientWaitSync) X(glFlush) \
    X(glGenFramebuffers) X(glDeleteFramebuffers) X(glBindFramebuffer) X(glFramebufferTexture2D) X(glCheckFramebufferStatus) X(glViewport) \
    X(glUseProgram) X(glCreateShader) X(glShaderSource) X(glCompileShader) X(glGetShaderiv) X(glGetShaderInfoLog) \
    X(glDeleteShader) X(glCreateProgram) X(glAttachShader) X(glLinkProgram) X(glGetProgramiv) X(glGetProgramInfoLog) \
    X(glGetUniformLocation) X(glUniform1i) X(glUniform1f) X(glUniform3f) X(glUniform4f) X(glUniformMatrix3fv) \
    X(glActiveTexture) X(glBindTexture) X(glGenSamplers) X(glSamplerParameteri) X(glBindSampler) X(glDrawArrays) \
    X(glGenVertexArrays) X(glBindVertexArray) X(glDisable) X(glEnable) X(glBlendFunc) X(glClearColor) X(glClear) \
    X(glGetError) X(glColorMask) X(glGenTextures) X(glDeleteTextures) X(glTexStorage2D) X(glGetIntegerv)
#define EMUL_DECLARE(fn) static __typeof__(&fn) p_##fn;
EMUL_EGL_FUNCS(EMUL_DECLARE)
EMUL_GL_FUNCS(EMUL_DECLARE)
#undef EMUL_DECLARE

static int emul_load_gl(void) {
    void *egl = dlopen("libEGL.so", RTLD_NOW | RTLD_LOCAL), *gles = dlopen("libGLESv3.so", RTLD_NOW | RTLD_LOCAL);
    if (!egl || !gles) return 0;
    int ok = 1;
#define EMUL_RESOLVE_EGL(fn) ok &= (p_##fn = (__typeof__(p_##fn))dlsym(egl, #fn)) != NULL;
#define EMUL_RESOLVE_GL(fn) ok &= (p_##fn = (__typeof__(p_##fn))dlsym(gles, #fn)) != NULL;
    EMUL_EGL_FUNCS(EMUL_RESOLVE_EGL)
    EMUL_GL_FUNCS(EMUL_RESOLVE_GL)
#undef EMUL_RESOLVE_EGL
#undef EMUL_RESOLVE_GL
    return ok;
}

// ---- state shared between the app thread (hooks, xrEndFrame) and the worker
typedef struct {
    XrSwapchain handle;
    uint32_t width, height, array_size, image_count;
    GLuint images[EMUL_MAX_IMAGES];
    uint32_t acquired[EMUL_MAX_IMAGES];  // acquired, not yet released (FIFO, as the runtime releases them)
    int acquired_count;
    int last_acquired, last_released;    // -1: none yet
    uint64_t generation;                 // bumps on every release
    GLsync fences[EMUL_MAX_IMAGES];      // app GPU work done for image i (owned here until the worker takes it)
    int watched;                         // shown through an emulated layer: fence its releases
    int reading;                         // image the worker is sampling, -1 = none
} emul_source;

// One 360° source (swapchain + sub-image) and the cube map it is resampled into (layer-local directions).
typedef struct {
    int used, retire;
    XrSwapchain src;
    XrRect2Di rect;
    uint32_t array_index;
    XrStructureType type;
    float central, upper, lower;  // equirect2
    XrVector2f scale, bias;       // equirect
    uint64_t params_gen;
    GLuint cube;                  // worker-owned cube map texture
    GLsync busy;                  // GPU still reading the source image for the last cube update
    int failed;                   // its cube map couldn't be made: this source is dropped (the others still show)
    uint32_t face;
    int ready;
    uint64_t rendered_src_gen, rendered_params_gen;
    int64_t rendered_at, last_used;
} emul_layer;

// What one eye of the projection image shows: 360° layers in submission order.
typedef struct {
    int slot;                     // emul_layers index
    XrQuaternionf orientation;    // layer pose in the projection layer's space (position ignored: infinite sphere)
    XrColor4f scale, bias;        // XrCompositionLayerColorScaleBiasKHR (dimming), identity if absent
} emul_entry;

typedef struct {
    XrSpace space;
    int count[2];
    emul_entry entries[2][EMUL_MAX_PER_EYE];
} emul_scene;

static pthread_mutex_t emul_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t emul_wake = PTHREAD_COND_INITIALIZER, emul_done = PTHREAD_COND_INITIALIZER;
static emul_source emul_sources[EMUL_MAX_SOURCES];
static emul_layer emul_layers[EMUL_MAX_LAYERS];
static emul_gles_binding emul_binding;
static XrSession emul_session = XR_NULL_HANDLE;
static XrViewConfigurationType emul_view_config = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
static XrSpace emul_view_space = XR_NULL_HANDLE;
static int emul_state;  // 0 idle, 1 worker running, -1 disabled for this session
static int emul_stop;
static pthread_t emul_thread;

// projection output (worker-owned swapchain; shared fields under emul_lock)
static struct {
    int job;                         // a frame to draw: job_scene with job_views for job_time
    emul_scene job_scene;
    XrView job_views[2];
    XrTime job_time, drawn_time;     // display time a job / the last image was drawn for
    emul_scene last_scene;           // the previous frame's 360 layers (for drawing early, from xrLocateViews)
    uint64_t early_seq;              // job posted from xrLocateViews for early_time
    XrTime early_time;
    uint64_t job_seq, done_seq;      // done_seq == job_seq: the worker finished (done_ok: drawn) the current job
    int done_ok;
    XrSwapchain swapchain;
    uint32_t size, count, pending_index;
    int pending_wait;                // acquired, wait not finished yet
    int preacquired;                 // acquired and waited ahead of the next frame (pending_index)
    GLuint images[EMUL_MAX_IMAGES];
    int ready;
    emul_scene drawn;                // what the last released image shows
    XrView views[2];                 // eye poses (in drawn.space) and FOV of the last image
    int late;                        // frames that got the previous image (stats)
    double wait_ms, wait_max;        // xrEndFrame's waits for the worker (stats)
    int waits, early_hits;
} emul_out;

static int emul_active(void) { return equirect_emul && emul_session && emul_state >= 0; }

static emul_source *emul_find_source(XrSwapchain handle, int create) {
    for (int i = 0; i < EMUL_MAX_SOURCES; ++i)
        if (emul_sources[i].handle == handle) return &emul_sources[i];
    if (!create) return NULL;
    for (int i = 0; i < EMUL_MAX_SOURCES; ++i)
        if (!emul_sources[i].handle) {
            memset(&emul_sources[i], 0, sizeof(emul_sources[i]));
            emul_sources[i].handle = handle;
            emul_sources[i].last_acquired = emul_sources[i].last_released = emul_sources[i].reading = -1;
            return &emul_sources[i];
        }
    return NULL;
}

// ---- adapter-served swapchains: write-once (STATIC_IMAGE), 360°-shaped (2:1, >= 2048 px wide) swapchains, e.g.
// 4XVR's theatre pictures. The runtime never shows them (360° layers are drawn by the adapter), but each real one takes
// one of the runtime's few swapchain slots (k_nMaxSwapchains; 4XVR's right-eye video swapchain was refused with
// XR_ERROR_LIMIT_REACHED) and hundreds of MB. Served as a plain GL texture in the app's context instead. If the app ever
// used one in another layer type, that layer is dropped (layer_usable), never passed to the runtime.
#define EMUL_MAX_VIRTUAL 8
static struct { XrSwapchain handle; GLuint tex; } emul_virtuals[EMUL_MAX_VIRTUAL];

static int emul_is_virtual(XrSwapchain handle) {
    for (int i = 0; handle && i < EMUL_MAX_VIRTUAL; ++i)
        if (emul_virtuals[i].handle == handle) return 1;
    return 0;
}

static int emul_virtual_create(XrSession session, const XrSwapchainCreateInfo *info, XrSwapchain *out) {
    if (!equirect_emul || !info || !out || session != emul_session || emul_state < 0) return 0;
    if (!(info->createFlags & XR_SWAPCHAIN_CREATE_STATIC_IMAGE_BIT) || info->arraySize != 1 || info->faceCount != 1 ||
        info->sampleCount > 1 || info->mipCount > 1 || info->width < 2048 || info->width != 2 * info->height)
        return 0;
    if (!p_eglCreateContext && !emul_load_gl()) return 0;
    if (p_eglGetCurrentContext() == EGL_NO_CONTEXT) return 0;
    int slot = -1;
    for (int i = 0; i < EMUL_MAX_VIRTUAL && slot < 0; ++i)
        if (!emul_virtuals[i].handle) slot = i;
    if (slot < 0) return 0;
    for (int i = 0; i < 8 && p_glGetError() != GL_NO_ERROR; ++i) {}
    GLint previous = 0;
    p_glGetIntegerv(GL_TEXTURE_BINDING_2D, &previous);
    GLuint tex = 0;
    p_glGenTextures(1, &tex);
    p_glBindTexture(GL_TEXTURE_2D, tex);
    p_glTexStorage2D(GL_TEXTURE_2D, 1, (GLenum)info->format, (GLsizei)info->width, (GLsizei)info->height);
    GLenum err = p_glGetError();
    p_glBindTexture(GL_TEXTURE_2D, (GLuint)previous);  // the app's binding, untouched
    if (err != GL_NO_ERROR) {
        p_glDeleteTextures(1, &tex);
        LOG("equirect_emul: couldn't serve a %ux%u picture swapchain (GL 0x%x), the runtime makes it", info->width,
            info->height, err);
        return 0;
    }
    emul_virtuals[slot].handle = FAKE_HANDLE(XrSwapchain);
    emul_virtuals[slot].tex = tex;
    *out = emul_virtuals[slot].handle;
    LOG("equirect_emul: %ux%u write-once 360 picture swapchain served by the adapter (frees a runtime swapchain slot)",
        info->width, info->height);
    return 1;
}

static XrResult emul_virtual_enumerate(XrSwapchain handle, uint32_t capacity, uint32_t *count, XrSwapchainImageBaseHeader *images) {
    if (!count) return XR_ERROR_VALIDATION_FAILURE;
    *count = 1;
    if (!capacity) return XR_SUCCESS;
    if (!images) return XR_ERROR_VALIDATION_FAILURE;
    for (int i = 0; i < EMUL_MAX_VIRTUAL; ++i)
        if (emul_virtuals[i].handle == handle) ((emul_gles_image *)images)[0].image = emul_virtuals[i].tex;
    return XR_SUCCESS;
}

static void emul_virtual_destroy(XrSwapchain handle) {
    for (int i = 0; i < EMUL_MAX_VIRTUAL; ++i)
        if (emul_virtuals[i].handle == handle) {
            if (p_eglGetCurrentContext && p_eglGetCurrentContext() != EGL_NO_CONTEXT) p_glDeleteTextures(1, &emul_virtuals[i].tex);
            emul_virtuals[i].handle = XR_NULL_HANDLE;
            emul_virtuals[i].tex = 0;
        }
}

// ---- hooks (app thread)
static void emul_on_create_session(XrSession session, const XrSessionCreateInfo *info) {
    if (!equirect_emul || !info) return;
    const emul_gles_binding *binding = NULL;
    for (const XrBaseInStructure *p = (const XrBaseInStructure *)info->next; p; p = p->next)
        if (p->type == XR_TYPE_GRAPHICS_BINDING_OPENGL_ES_ANDROID_KHR) binding = (const emul_gles_binding *)p;
    if (!binding) { LOG("equirect_emul: not a GLES session, 360 layers stay dropped"); emul_state = -1; return; }
    emul_binding = *binding;
    emul_session = session;
    emul_state = 0;
    PFN_xrCreateReferenceSpace create = (PFN_xrCreateReferenceSpace)lookup(active_instance, "xrCreateReferenceSpace");
    XrReferenceSpaceCreateInfo ci = {XR_TYPE_REFERENCE_SPACE_CREATE_INFO, NULL, XR_REFERENCE_SPACE_TYPE_VIEW,
                                     {{0, 0, 0, 1}, {0, 0, 0}}};
    if (create) create(session, &ci, &emul_view_space);
    LOG("equirect_emul: GLES session, 360 layers will be drawn into a projection layer");
}

static void emul_on_begin_session(const XrSessionBeginInfo *info) {
    if (equirect_emul && info) emul_view_config = info->primaryViewConfigurationType;
}

static void emul_on_create_swapchain(XrSwapchain handle, const XrSwapchainCreateInfo *info) {
    if (!equirect_emul || !info) return;
    pthread_mutex_lock(&emul_lock);
    emul_source *s = emul_find_source(handle, 1);
    if (s) { s->width = info->width; s->height = info->height; s->array_size = info->arraySize; }
    pthread_mutex_unlock(&emul_lock);
}

static void emul_on_enumerate(XrSwapchain handle, uint32_t count, const XrSwapchainImageBaseHeader *images) {
    if (!equirect_emul || !images || images->type != XR_TYPE_SWAPCHAIN_IMAGE_OPENGL_ES_KHR) return;
    pthread_mutex_lock(&emul_lock);
    emul_source *s = emul_find_source(handle, 0);
    if (s) {
        s->image_count = count < EMUL_MAX_IMAGES ? count : EMUL_MAX_IMAGES;
        for (uint32_t i = 0; i < s->image_count; ++i) s->images[i] = ((const emul_gles_image *)images)[i].image;
    }
    pthread_mutex_unlock(&emul_lock);
}

static void emul_on_acquire(XrSwapchain handle, uint32_t index) {
    if (!equirect_emul) return;
    pthread_mutex_lock(&emul_lock);
    emul_source *s = emul_find_source(handle, 0);
    if (s && index < EMUL_MAX_IMAGES) {
        s->last_acquired = (int)index;
        if (s->acquired_count < EMUL_MAX_IMAGES) s->acquired[s->acquired_count++] = index;
    }
    pthread_mutex_unlock(&emul_lock);
}

static void emul_on_destroy_swapchain(XrSwapchain handle) {
    if (!equirect_emul) return;
    pthread_mutex_lock(&emul_lock);
    emul_source *s = emul_find_source(handle, 0);
    if (s) {
        struct timespec until;
        clock_gettime(CLOCK_REALTIME, &until);
        until.tv_sec += 1;
        while (s->reading >= 0 && pthread_cond_timedwait(&emul_done, &emul_lock, &until) == 0) {}
        for (int i = 0; i < EMUL_MAX_LAYERS; ++i)
            if (emul_layers[i].used && emul_layers[i].src == handle) emul_layers[i].retire = 1;
        // Fences still owned here would need a current GL context to delete; a few syncs per destroyed swapchain.
        memset(s, 0, sizeof(*s));
        pthread_cond_broadcast(&emul_wake);
    }
    pthread_mutex_unlock(&emul_lock);
}

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrReleaseSwapchainImage(XrSwapchain swapchain, const XrSwapchainImageReleaseInfo *info) {
    PFN_xrReleaseSwapchainImage fn = (PFN_xrReleaseSwapchainImage)lookup(active_instance, "xrReleaseSwapchainImage");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (is_standin(swapchain)) return XR_SUCCESS;
    pthread_mutex_lock(&emul_lock);
    emul_source *s = emul_active() ? emul_find_source(swapchain, 0) : NULL;
    int index = s && s->acquired_count ? (int)s->acquired[0] : -1;
    GLsync fence = NULL, old = NULL;
    if (s && s->watched && index >= 0 && p_glFenceSync && p_eglGetCurrentContext && p_eglGetCurrentContext() != EGL_NO_CONTEXT) {
        fence = p_glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
        if (fence) p_glFlush();
    }
    pthread_mutex_unlock(&emul_lock);
    XrResult result = emul_is_virtual(swapchain) ? XR_SUCCESS : fn(swapchain, info);
    pthread_mutex_lock(&emul_lock);
    s = emul_active() ? emul_find_source(swapchain, 0) : NULL;
    if (s && XR_SUCCEEDED(result) && s->acquired_count) {
        index = (int)s->acquired[0];
        memmove(&s->acquired[0], &s->acquired[1], (size_t)--s->acquired_count * sizeof(s->acquired[0]));
        old = s->fences[index];
        s->fences[index] = fence;
        fence = NULL;
        s->last_released = index;
        ++s->generation;
        if (s->watched) pthread_cond_broadcast(&emul_wake);
    }
    pthread_mutex_unlock(&emul_lock);
    if (old && p_glDeleteSync) p_glDeleteSync(old);
    if (fence && p_glDeleteSync) p_glDeleteSync(fence);
    return result;
}

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrWaitSwapchainImage(XrSwapchain swapchain, const XrSwapchainImageWaitInfo *info) {
    PFN_xrWaitSwapchainImage fn = (PFN_xrWaitSwapchainImage)lookup(active_instance, "xrWaitSwapchainImage");
    if (!fn) return XR_ERROR_FUNCTION_UNSUPPORTED;
    if (is_standin(swapchain)) return XR_SUCCESS;
    XrResult result = emul_is_virtual(swapchain) ? XR_SUCCESS : fn(swapchain, info);
    pthread_mutex_lock(&emul_lock);
    emul_source *s = emul_active() ? emul_find_source(swapchain, 0) : NULL;
    if (s && s->reading >= 0 && s->reading == s->last_acquired) {
        // The app is about to draw into the image the worker is reading: let the read finish first (bounded).
        struct timespec until;
        clock_gettime(CLOCK_REALTIME, &until);
        until.tv_nsec += 50000000;
        if (until.tv_nsec >= 1000000000) { until.tv_sec += 1; until.tv_nsec -= 1000000000; }
        int64_t start = monotonic_ns();
        while (s->reading >= 0 && s->reading == s->last_acquired &&
               pthread_cond_timedwait(&emul_done, &emul_lock, &until) == 0) {}
        static int logged;
        if (logged++ < 5) LOG("equirect_emul: app waited %.1f ms for the worker's read", (monotonic_ns() - start) / 1e6);
    }
    pthread_mutex_unlock(&emul_lock);
    return result;
}

// ---- worker: shaders
static const char *emul_vs =
    "#version 300 es\n"
    "out vec2 ndc;\n"
    "void main() {\n"
    "    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));\n"
    "    ndc = p * 2.0 - 1.0;\n"
    "    gl_Position = vec4(ndc, 0.0, 1.0);\n"
    "}\n";

// Stage 1: one face of a GL cube map (GL's face orientation) from the app's equirect image.
static const char *emul_cube_fs =
    "precision highp float;\n"
    "precision highp int;\n"
    "#ifdef ARRAY\n"
    "uniform highp sampler2DArray src;\n"
    "#else\n"
    "uniform highp sampler2D src;\n"
    "#endif\n"
    "uniform float array_layer;\n"
    "uniform int face;\n"         // GL_TEXTURE_CUBE_MAP_POSITIVE_X + face
    "uniform float texel;\n"      // one cube texel in ndc units
    "uniform int mode;\n"         // 0 = equirect2, 1 = equirect
    "uniform vec3 angles;\n"      // equirect2: central, upper, lower
    "uniform vec4 scale_bias;\n"  // equirect: scale.xy, bias.xy
    "uniform vec4 rect;\n"        // sub-image in normalized texture coordinates: x, y, w, h
    "uniform int flip;\n"         // 1 = upside down, 2 = mirrored, 4 = turned 180 degrees
    "in vec2 ndc;\n"
    "out vec4 color;\n"
    "const float PI = 3.14159265358979;\n"
    "vec3 cube_dir(vec2 p) {\n"   // direction of cube texel (s, t) = (p + 1) / 2 on this face (GL convention)
    "    if (face == 0) return vec3(1.0, -p.y, -p.x);\n"
    "    if (face == 1) return vec3(-1.0, -p.y, p.x);\n"
    "    if (face == 2) return vec3(p.x, 1.0, p.y);\n"
    "    if (face == 3) return vec3(p.x, -1.0, -p.y);\n"
    "    if (face == 4) return vec3(p.x, -p.y, 1.0);\n"
    "    return vec3(-p.x, -p.y, -1.0);\n"
    "}\n"
    "vec4 fetch(vec2 p) {\n"
    "    vec3 d = normalize(cube_dir(p));\n"
    "    if ((flip & 4) != 0) d = vec3(-d.x, d.y, -d.z);\n"
    "    float lon = atan(d.x, -d.z);\n"
    "    float lat = asin(clamp(d.y, -1.0, 1.0));\n"
    "    vec2 uv;\n"
    "    if (mode == 0) uv = vec2(lon / angles.x + 0.5, (lat - angles.z) / (angles.y - angles.z));\n"
    "    else uv = vec2(lon / (2.0 * PI) + 0.5, lat / PI + 0.5) * scale_bias.xy + scale_bias.zw;\n"
    "    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) return vec4(0.0);\n"
    "    if ((flip & 1) != 0) uv.y = 1.0 - uv.y;\n"
    "    if ((flip & 2) != 0) uv.x = 1.0 - uv.x;\n"
    "    vec2 tc = rect.xy + uv * rect.zw;\n"
    "#ifdef ARRAY\n"
    "    return textureLod(src, vec3(tc, array_layer), 0.0);\n"
    "#else\n"
    "    return textureLod(src, tc, 0.0);\n"
    "#endif\n"
    "}\n"
    "void main() {\n"  // 2x2 supersampling: the source usually has more texels per degree than the cube
    "    float q = texel * 0.25;\n"
    "    color = 0.25 * (fetch(ndc + vec2(-q, -q)) + fetch(ndc + vec2(q, -q)) + fetch(ndc + vec2(-q, q)) +\n"
    "                    fetch(ndc + vec2(q, q)));\n"
    "}\n";

// Stage 2: one eye of the projection image from a cube map.
static const char *emul_view_fs =
    "#version 300 es\n"
    "precision highp float;\n"
    "uniform highp samplerCube cube;\n"
    "uniform mat3 view_to_layer;\n"  // eye orientation -> the 360 layer's local frame
    "uniform vec4 tans;\n"           // tan of the FOV angles: left, right, down, up
    "uniform vec4 color_scale;\n"
    "uniform vec4 color_bias;\n"
    "in vec2 ndc;\n"
    "out vec4 color;\n"
    "void main() {\n"
    "    vec2 t = vec2(mix(tans.x, tans.y, ndc.x * 0.5 + 0.5), mix(tans.z, tans.w, ndc.y * 0.5 + 0.5));\n"
    "    vec3 d = view_to_layer * vec3(t, -1.0);\n"
    "    color = clamp(texture(cube, d) * color_scale + color_bias, 0.0, 1.0);\n"
    "}\n";

typedef struct {
    GLuint program;
    GLint src, array_layer, face, texel, mode, angles, scale_bias, rect, flip;  // cube stage
    GLint cube, view_to_layer, tans, color_scale, color_bias;                    // view stage
} emul_program;

static GLuint emul_compile(GLenum kind, const char *prefix, const char *body) {
    GLuint shader = p_glCreateShader(kind);
    const char *parts[2] = {prefix, body};
    p_glShaderSource(shader, 2, parts, NULL);
    p_glCompileShader(shader);
    GLint ok = 0;
    p_glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[1024] = "";
        p_glGetShaderInfoLog(shader, sizeof(log), NULL, log);
        LOG("equirect_emul: shader compile failed: %s", log);
        p_glDeleteShader(shader);
        return 0;
    }
    return shader;
}

static int emul_build_program(emul_program *out, const char *fs_prefix, const char *fs) {
    GLuint v = emul_compile(GL_VERTEX_SHADER, "", emul_vs), f = emul_compile(GL_FRAGMENT_SHADER, fs_prefix, fs);
    if (!v || !f) return 0;
    GLuint program = p_glCreateProgram();
    p_glAttachShader(program, v);
    p_glAttachShader(program, f);
    p_glLinkProgram(program);
    p_glDeleteShader(v);
    p_glDeleteShader(f);
    GLint ok = 0;
    p_glGetProgramiv(program, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[1024] = "";
        p_glGetProgramInfoLog(program, sizeof(log), NULL, log);
        LOG("equirect_emul: program link failed: %s", log);
        return 0;
    }
    out->program = program;
#define EMUL_UNIFORM(name) out->name = p_glGetUniformLocation(program, #name);
    EMUL_UNIFORM(src) EMUL_UNIFORM(array_layer) EMUL_UNIFORM(face) EMUL_UNIFORM(texel) EMUL_UNIFORM(mode)
    EMUL_UNIFORM(angles) EMUL_UNIFORM(scale_bias) EMUL_UNIFORM(rect) EMUL_UNIFORM(flip) EMUL_UNIFORM(cube)
    EMUL_UNIFORM(view_to_layer) EMUL_UNIFORM(tans) EMUL_UNIFORM(color_scale) EMUL_UNIFORM(color_bias)
#undef EMUL_UNIFORM
    return 1;
}

static struct {
    EGLContext context;
    EGLSurface surface;
    emul_program cube_plain, cube_array, view;
    GLuint fbo, vao, sampler_clamp, sampler_wrap, sampler_cube;
    int cube_draws, view_draws, why_cube;
    double cube_ms, view_ms, cube_ms_max, view_ms_max;
    int64_t stats_since;
} emul_gl;

static int emul_gl_init(void) {
    EGLDisplay dpy = emul_binding.display;
    EGLConfig config = emul_binding.config;
    if (!config) {  // EGL_NO_CONFIG_KHR: use the app context's config
        EGLint id = 0, n = 0;
        p_eglQueryContext(dpy, emul_binding.context, EGL_CONFIG_ID, &id);
        EGLint attribs[] = {EGL_CONFIG_ID, id, EGL_NONE};
        if (!p_eglChooseConfig(dpy, attribs, &config, 1, &n) || n < 1) config = NULL;
    }
    // Low priority where the driver supports it (EGL_IMG_context_priority), so the app's own rendering goes first.
    EGLint low[] = {EGL_CONTEXT_CLIENT_VERSION, 3, 0x3100 /* EGL_CONTEXT_PRIORITY_LEVEL_IMG */, 0x3103 /* LOW */, EGL_NONE};
    EGLint normal[] = {EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE};
    const char *extensions = p_eglQueryString(dpy, EGL_EXTENSIONS);
    int has_priority = extensions && strstr(extensions, "EGL_IMG_context_priority");
    emul_gl.context = p_eglCreateContext(dpy, config, emul_binding.context, has_priority ? low : normal);
    if (emul_gl.context == EGL_NO_CONTEXT && has_priority)
        emul_gl.context = p_eglCreateContext(dpy, config, emul_binding.context, normal);
    if (emul_gl.context == EGL_NO_CONTEXT) { LOG("equirect_emul: eglCreateContext failed 0x%x", p_eglGetError()); return 0; }
    emul_gl.surface = EGL_NO_SURFACE;
    if (!p_eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, emul_gl.context)) {  // surfaceless where supported
        EGLint pb[] = {EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE};
        emul_gl.surface = config ? p_eglCreatePbufferSurface(dpy, config, pb) : EGL_NO_SURFACE;
        if (emul_gl.surface == EGL_NO_SURFACE || !p_eglMakeCurrent(dpy, emul_gl.surface, emul_gl.surface, emul_gl.context)) {
            LOG("equirect_emul: eglMakeCurrent failed 0x%x", p_eglGetError());
            return 0;
        }
    }
    if (!emul_build_program(&emul_gl.cube_plain, "#version 300 es\n", emul_cube_fs) ||
        !emul_build_program(&emul_gl.cube_array, "#version 300 es\n#define ARRAY\n", emul_cube_fs) ||
        !emul_build_program(&emul_gl.view, "", emul_view_fs))
        return 0;
    p_glGenFramebuffers(1, &emul_gl.fbo);
    p_glGenVertexArrays(1, &emul_gl.vao);
    GLuint samplers[3];
    p_glGenSamplers(3, samplers);
    for (int i = 0; i < 3; ++i) {
        p_glSamplerParameteri(samplers[i], GL_TEXTURE_MIN_FILTER, GL_LINEAR);
        p_glSamplerParameteri(samplers[i], GL_TEXTURE_MAG_FILTER, GL_LINEAR);
        p_glSamplerParameteri(samplers[i], GL_TEXTURE_WRAP_S, i == 1 ? GL_REPEAT : GL_CLAMP_TO_EDGE);
        p_glSamplerParameteri(samplers[i], GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    }
    emul_gl.sampler_clamp = samplers[0];
    emul_gl.sampler_wrap = samplers[1];
    emul_gl.sampler_cube = samplers[2];  // GLES 3 cube maps filter seamlessly across faces
    LOG("equirect_emul: worker context ready (%s, priority %s)", emul_gl.surface ? "pbuffer" : "surfaceless",
        has_priority ? "low" : "default");
    return 1;
}

static void emul_gl_shutdown(void) {
    if (!emul_gl.context) return;
    for (int i = 0; i < EMUL_MAX_LAYERS; ++i)
        if (emul_layers[i].cube) { p_glDeleteTextures(1, &emul_layers[i].cube); emul_layers[i].cube = 0; emul_layers[i].ready = 0; }
    p_eglMakeCurrent(emul_binding.display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    if (emul_gl.surface) p_eglDestroySurface(emul_binding.display, emul_gl.surface);
    p_eglDestroyContext(emul_binding.display, emul_gl.context);
    memset(&emul_gl, 0, sizeof(emul_gl));
}

// Cube face size: the source's texels per 90° (no detail lost), capped by equirect_face.
static uint32_t emul_face_size(const emul_layer *l) {
    float per_turn = (float)l->rect.extent.width;
    if (l->type == XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR && l->central > 0) per_turn *= 2 * LM_PI / l->central;
    else if (l->scale.x > 0) per_turn /= l->scale.x;
    uint32_t face = (uint32_t)(per_turn / 4);
    uint32_t cap = equirect_face >= 256 ? (uint32_t)equirect_face : 1536;
    if (face > cap) face = cap;
    if (face < 256) face = 256;
    return face & ~3u;
}

static void emul_stats(void) {
    int64_t now = monotonic_ns();
    if (now - emul_gl.stats_since < 5000000000ll) return;
    if (emul_gl.cube_draws || emul_gl.view_draws)
        LOG("equirect_emul: in %.1f s: %d 360 image update(s) avg %.1f ms (max %.1f), %d view redraw(s) avg %.1f ms (max %.1f)",
            (now - emul_gl.stats_since) / 1e9, emul_gl.cube_draws,
            emul_gl.cube_draws ? emul_gl.cube_ms / emul_gl.cube_draws : 0.0, emul_gl.cube_ms_max, emul_gl.view_draws,
            emul_gl.view_draws ? emul_gl.view_ms / emul_gl.view_draws : 0.0, emul_gl.view_ms_max);
    if (emul_gl.view_draws && emul_out.late)
        LOG("equirect_emul: %d frame(s) showed the previous 360 view (worker late)", emul_out.late);
    if (emul_out.waits)
        LOG("equirect_emul: xrEndFrame waited avg %.2f ms (max %.2f) for the 360 view; %d of %d frames drawn early",
            emul_out.wait_ms / emul_out.waits, emul_out.wait_max, emul_out.early_hits, emul_out.waits);
    emul_out.late = emul_out.waits = emul_out.early_hits = 0;
    emul_out.wait_ms = emul_out.wait_max = 0;
    emul_gl.cube_draws = emul_gl.view_draws = 0;
    emul_gl.cube_ms = emul_gl.view_ms = emul_gl.cube_ms_max = emul_gl.view_ms_max = 0;
    emul_gl.stats_since = now;
}

// Frees the cube maps of 360 layers the last frame didn't show (emul_lock held; worker thread: its GL context).
static void emul_free_unshown_cubes(const emul_layer *keep) {
    int freed = 0;
    for (int i = 0; i < EMUL_MAX_LAYERS; ++i) {
        emul_layer *l = &emul_layers[i];
        if (l == keep || !l->cube || l->busy) continue;
        int shown = 0;
        for (int e = 0; e < 2; ++e)
            for (int k = 0; k < emul_out.last_scene.count[e]; ++k) shown |= emul_out.last_scene.entries[e][k].slot == i;
        if (shown) continue;
        p_glDeleteTextures(1, &l->cube);
        l->cube = 0;
        l->ready = 0;
        ++freed;
    }
    if (freed) LOG("equirect_emul: freed %d cube map(s) not on screen", freed);
}

// Stage 1 (worker, emul_lock NOT held): resample one source image into the layer's cube map.
static int emul_draw_cube(emul_layer *l, emul_layer snapshot, emul_source src, int index, GLsync fence) {
    int64_t start = monotonic_ns();
    if (fence) { p_glWaitSync(fence, 0, GL_TIMEOUT_IGNORED); p_glDeleteSync(fence); }
    // The computed size, else smaller ones. A cube map the driver can't render into (seen with 4XVR's 5120x2560 videos:
    // created without error, framebuffer incomplete) is most likely GPU memory running out: cube maps of 360 layers not
    // on screen are freed before the next try.
    static const uint32_t smaller[] = {1024, 768};
    if (!l->cube) {  // a new 360 source (e.g. a video starting): first give back what isn't on screen (the theatre)
        pthread_mutex_lock(&emul_lock);
        emul_free_unshown_cubes(l);
        pthread_mutex_unlock(&emul_lock);
    }
    for (int attempt = 0; !l->cube && attempt < 3; ++attempt) {
        uint32_t want = attempt ? smaller[attempt - 1] : emul_face_size(&snapshot);
        if (attempt && want >= l->face) continue;
        l->face = want;
        for (int i = 0; i < 8 && p_glGetError() != GL_NO_ERROR; ++i) {}
        p_glGenTextures(1, &l->cube);
        p_glBindTexture(GL_TEXTURE_CUBE_MAP, l->cube);
        p_glTexStorage2D(GL_TEXTURE_CUBE_MAP, 1, GL_SRGB8_ALPHA8, (GLsizei)l->face, (GLsizei)l->face);
        p_glBindTexture(GL_TEXTURE_CUBE_MAP, 0);
        GLenum err = p_glGetError(), status = GL_FRAMEBUFFER_COMPLETE;
        if (err == GL_NO_ERROR) {
            p_glBindFramebuffer(GL_FRAMEBUFFER, emul_gl.fbo);
            for (int f = 0; f < 6 && status == GL_FRAMEBUFFER_COMPLETE; ++f) {
                p_glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_CUBE_MAP_POSITIVE_X + f, l->cube, 0);
                status = p_glCheckFramebufferStatus(GL_FRAMEBUFFER);
            }
            p_glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, 0, 0);
            p_glBindFramebuffer(GL_FRAMEBUFFER, 0);
            for (int i = 0; i < 8 && p_glGetError() != GL_NO_ERROR; ++i) {}
        }
        int ok = err == GL_NO_ERROR && status == GL_FRAMEBUFFER_COMPLETE;
        LOG("equirect_emul: cube map %ux%u x6 for a %ux%u source%s (GL 0x%x, framebuffer 0x%x)", l->face, l->face,
            snapshot.rect.extent.width, snapshot.rect.extent.height, ok ? "" : " FAILED", err, status);
        if (!ok) {
            p_glDeleteTextures(1, &l->cube);
            l->cube = 0;
            pthread_mutex_lock(&emul_lock);
            emul_free_unshown_cubes(l);
            pthread_mutex_unlock(&emul_lock);
        }
    }
    if (!l->cube) return 0;
    int array = src.array_size > 1;
    emul_program *pr = array ? &emul_gl.cube_array : &emul_gl.cube_plain;
    p_glBindFramebuffer(GL_FRAMEBUFFER, emul_gl.fbo);
    p_glDisable(GL_BLEND); p_glDisable(GL_DEPTH_TEST); p_glDisable(GL_SCISSOR_TEST); p_glDisable(GL_CULL_FACE);
    p_glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    p_glUseProgram(pr->program);
    p_glBindVertexArray(emul_gl.vao);
    p_glActiveTexture(GL_TEXTURE0);
    p_glBindTexture(array ? GL_TEXTURE_2D_ARRAY : GL_TEXTURE_2D, src.images[index]);
    GLenum bind_err = p_glGetError();
    if (bind_err != GL_NO_ERROR) {
        LOG("equirect_emul: binding the app's %ux%u image %d (texture %u, %s) failed: GL 0x%x", src.width, src.height,
            index, src.images[index], array ? "2D array" : "2D", bind_err);
        p_glBindFramebuffer(GL_FRAMEBUFFER, 0);
        return 0;
    }
    int full_width = snapshot.rect.offset.x == 0 && (uint32_t)snapshot.rect.extent.width == src.width;
    p_glBindSampler(0, full_width ? emul_gl.sampler_wrap : emul_gl.sampler_clamp);
    p_glUniform1i(pr->src, 0);
    p_glUniform1f(pr->array_layer, (float)snapshot.array_index);
    p_glUniform1f(pr->texel, 2.0f / (float)l->face);
    p_glUniform1i(pr->mode, snapshot.type == XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR ? 0 : 1);
    p_glUniform3f(pr->angles, snapshot.central, snapshot.upper, snapshot.lower);
    p_glUniform4f(pr->scale_bias, snapshot.scale.x, snapshot.scale.y, snapshot.bias.x, snapshot.bias.y);
    p_glUniform4f(pr->rect, (float)snapshot.rect.offset.x / src.width, (float)snapshot.rect.offset.y / src.height,
                  (float)snapshot.rect.extent.width / src.width, (float)snapshot.rect.extent.height / src.height);
    p_glUniform1i(pr->flip, equirect_flip);
    p_glViewport(0, 0, (GLsizei)l->face, (GLsizei)l->face);
    for (int f = 0; f < 6; ++f) {
        p_glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_CUBE_MAP_POSITIVE_X + f, l->cube, 0);
        if (f == 0) {
            GLenum status = p_glCheckFramebufferStatus(GL_FRAMEBUFFER);
            if (status != GL_FRAMEBUFFER_COMPLETE) LOG("equirect_emul: cube framebuffer incomplete 0x%x", status);
        }
        p_glUniform1i(pr->face, f);
        p_glDrawArrays(GL_TRIANGLES, 0, 3);
        if (f == 0) {
            GLenum draw_err = p_glGetError();
            if (draw_err != GL_NO_ERROR) {
                LOG("equirect_emul: drawing from the app's %ux%u image failed: GL 0x%x", src.width, src.height, draw_err);
                p_glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, 0, 0);
                p_glBindFramebuffer(GL_FRAMEBUFFER, 0);
                return 0;
            }
        }
    }
    p_glBindTexture(array ? GL_TEXTURE_2D_ARRAY : GL_TEXTURE_2D, 0);
    p_glBindSampler(0, 0);
    p_glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, 0, 0);
    p_glBindFramebuffer(GL_FRAMEBUFFER, 0);
    l->busy = p_glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);  // the app may reuse the source image after it
    p_glFlush();
    GLenum err = p_glGetError();
    if (err != GL_NO_ERROR) LOG("equirect_emul: GL error 0x%x while updating a cube map", err);
    if (err != GL_NO_ERROR && l->cube) { p_glDeleteTextures(1, &l->cube); l->cube = 0; }
    double ms = (monotonic_ns() - start) / 1e6;
    emul_gl.cube_draws++;
    emul_gl.cube_ms += ms;
    if (ms > emul_gl.cube_ms_max) emul_gl.cube_ms_max = ms;
    return err == GL_NO_ERROR;
}

static void emul_mat3(XrQuaternionf q, float m[9]) {  // column-major rotation matrix
    XrVector3f x = lm_rotate(q, (XrVector3f){1, 0, 0}), y = lm_rotate(q, (XrVector3f){0, 1, 0}),
               z = lm_rotate(q, (XrVector3f){0, 0, 1});
    float v[9] = {x.x, x.y, x.z, y.x, y.y, y.z, z.x, z.y, z.z};
    memcpy(m, v, sizeof(v));
}

static int emul_create_output(void) {
    PFN_xrCreateSwapchain create = (PFN_xrCreateSwapchain)lookup(active_instance, "xrCreateSwapchain");
    PFN_xrEnumerateSwapchainImages enumerate =
        (PFN_xrEnumerateSwapchainImages)lookup(active_instance, "xrEnumerateSwapchainImages");
    if (!create || !enumerate) return 0;
    uint32_t size = equirect_res >= 512 ? (uint32_t)equirect_res : 1536;
    XrSwapchainCreateInfo ci = {XR_TYPE_SWAPCHAIN_CREATE_INFO, NULL, 0,
        XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT | XR_SWAPCHAIN_USAGE_SAMPLED_BIT, 0x8C43 /* GL_SRGB8_ALPHA8 */,
        1, 2 * size, size, 1, 1, 1};
    XrSwapchain swapchain = XR_NULL_HANDLE;
    XrResult r = create(emul_session, &ci, &swapchain);
    if (XR_FAILED(r)) { LOG("equirect_emul: projection swapchain %ux%u failed: %d", 2 * size, size, r); return 0; }
    emul_gles_image images[EMUL_MAX_IMAGES];
    for (int i = 0; i < EMUL_MAX_IMAGES; ++i) images[i] = (emul_gles_image){XR_TYPE_SWAPCHAIN_IMAGE_OPENGL_ES_KHR, NULL, 0};
    uint32_t n = 0;
    if (XR_FAILED(enumerate(swapchain, EMUL_MAX_IMAGES, &n, (XrSwapchainImageBaseHeader *)images))) return 0;
    for (uint32_t i = 0; i < n; ++i) emul_out.images[i] = images[i].image;
    pthread_mutex_lock(&emul_lock);
    emul_out.count = n;
    emul_out.size = size;
    emul_out.swapchain = swapchain;
    pthread_mutex_unlock(&emul_lock);
    LOG("equirect_emul: projection swapchain %ux%u (%u images)", 2 * size, size, n);
    return 1;
}

// Stage 2 (worker, emul_lock NOT held): draw both eyes of the projection image for `scene` and `views`.
// Returns 1 = drawn, 0 = failed (disable), -1 = no image this time.
static int emul_draw_view(const emul_scene *scene, GLuint cubes[2][EMUL_MAX_PER_EYE], const XrView views[2]) {
    PFN_xrAcquireSwapchainImage acquire = (PFN_xrAcquireSwapchainImage)lookup(active_instance, "xrAcquireSwapchainImage");
    PFN_xrWaitSwapchainImage wait = (PFN_xrWaitSwapchainImage)lookup(active_instance, "xrWaitSwapchainImage");
    PFN_xrReleaseSwapchainImage release = (PFN_xrReleaseSwapchainImage)lookup(active_instance, "xrReleaseSwapchainImage");
    if (!acquire || !wait || !release) return 0;
    if (!emul_out.swapchain && !emul_create_output()) return 0;
    uint32_t out = 0;
    XrResult r = XR_SUCCESS;
    XrSwapchainImageAcquireInfo ai = {XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO, NULL};
    XrSwapchainImageWaitInfo wi = {XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO, NULL, 4000000};
    XrSwapchainImageReleaseInfo ri = {XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO, NULL};
    if (emul_out.pending_wait || emul_out.preacquired) out = emul_out.pending_index;
    else r = acquire(emul_out.swapchain, &ai, &out);
    if (XR_FAILED(r) || out >= emul_out.count) { LOG("equirect_emul: projection acquire failed %d", r); return 0; }
    if (!emul_out.preacquired) r = wait(emul_out.swapchain, &wi);
    if (r != XR_SUCCESS) {  // e.g. XR_TIMEOUT_EXPIRED: keep the acquired image and wait again next time
        emul_out.pending_wait = 1;
        emul_out.pending_index = out;
        return XR_FAILED(r) ? 0 : -1;
    }
    emul_out.pending_wait = 0;
    emul_out.preacquired = 0;
    int64_t start = monotonic_ns();
    uint32_t W = emul_out.size;
    p_glBindFramebuffer(GL_FRAMEBUFFER, emul_gl.fbo);
    p_glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, emul_out.images[out], 0);
    p_glDisable(GL_DEPTH_TEST); p_glDisable(GL_SCISSOR_TEST); p_glDisable(GL_CULL_FACE); p_glDisable(GL_BLEND);
    p_glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
    p_glViewport(0, 0, (GLsizei)(2 * W), (GLsizei)W);
    p_glClearColor(0, 0, 0, 1);
    p_glClear(GL_COLOR_BUFFER_BIT);
    p_glUseProgram(emul_gl.view.program);
    p_glBindVertexArray(emul_gl.vao);
    p_glActiveTexture(GL_TEXTURE0);
    p_glBindSampler(0, emul_gl.sampler_cube);
    p_glUniform1i(emul_gl.view.cube, 0);
    for (int e = 0; e < 2; ++e) {
        XrFovf fov = views[e].fov;
        p_glViewport((GLint)(e * W), 0, (GLsizei)W, (GLsizei)W);
        p_glUniform4f(emul_gl.view.tans, tanf(fov.angleLeft), tanf(fov.angleRight), tanf(fov.angleDown), tanf(fov.angleUp));
        for (int k = 0; k < scene->count[e]; ++k) {
            const emul_entry *en = &scene->entries[e][k];
            if (!cubes[e][k]) continue;
            if (k == 0) p_glDisable(GL_BLEND);
            else { p_glEnable(GL_BLEND); p_glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA); }
            float m[9];  // eye frame -> layer frame = conj(layer) * eye
            emul_mat3(lm_qmul(lm_qconj(en->orientation), views[e].pose.orientation), m);
            p_glUniformMatrix3fv(emul_gl.view.view_to_layer, 1, GL_FALSE, m);
            p_glUniform4f(emul_gl.view.color_scale, en->scale.r, en->scale.g, en->scale.b, en->scale.a);
            p_glUniform4f(emul_gl.view.color_bias, en->bias.r, en->bias.g, en->bias.b, en->bias.a);
            p_glBindTexture(GL_TEXTURE_CUBE_MAP, cubes[e][k]);
            p_glDrawArrays(GL_TRIANGLES, 0, 3);
        }
    }
    p_glDisable(GL_BLEND);
    p_glBindTexture(GL_TEXTURE_CUBE_MAP, 0);
    p_glBindSampler(0, 0);
    p_glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, 0, 0);
    p_glBindFramebuffer(GL_FRAMEBUFFER, 0);
    p_glFlush();
    r = release(emul_out.swapchain, &ri);  // the runtime waits for the GPU work itself
    GLenum err = p_glGetError();
    if (err != GL_NO_ERROR) LOG("equirect_emul: GL error 0x%x while drawing the view", err);
    double ms = (monotonic_ns() - start) / 1e6;
    emul_gl.view_draws++;
    emul_gl.view_ms += ms;
    if (ms > emul_gl.view_ms_max) emul_gl.view_ms_max = ms;
    return XR_SUCCEEDED(r) && err == GL_NO_ERROR;
}

// After a frame: acquire and wait for the next projection image now, so the next frame's job is only the draw
// (worker, emul_lock NOT held).
static void emul_preacquire(void) {
    PFN_xrAcquireSwapchainImage acquire = (PFN_xrAcquireSwapchainImage)lookup(active_instance, "xrAcquireSwapchainImage");
    PFN_xrWaitSwapchainImage wait = (PFN_xrWaitSwapchainImage)lookup(active_instance, "xrWaitSwapchainImage");
    if (!acquire || !wait || !emul_out.swapchain || emul_out.preacquired || emul_out.pending_wait) return;
    XrSwapchainImageAcquireInfo ai = {XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO, NULL};
    XrSwapchainImageWaitInfo wi = {XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO, NULL, 100000000};
    uint32_t out = 0;
    if (XR_FAILED(acquire(emul_out.swapchain, &ai, &out)) || out >= emul_out.count) return;
    emul_out.pending_index = out;
    XrResult r = wait(emul_out.swapchain, &wi);
    if (r == XR_SUCCESS) emul_out.preacquired = 1;
    else emul_out.pending_wait = 1;  // the frame job waits again
}

static void emul_disable(const char *why) {  // emul_lock held
    LOG("equirect_emul: %s; disabled for this session, 360 layers stay dropped", why);
    emul_out.ready = 0;
    emul_state = -1;
}

static void *emul_worker(void *arg) {
    (void)arg;
    if (!emul_gl_init()) {
        pthread_mutex_lock(&emul_lock);
        emul_disable("no shared GL context");
        pthread_mutex_unlock(&emul_lock);
        emul_gl_shutdown();
        return NULL;
    }
    emul_gl.stats_since = monotonic_ns();
    pthread_mutex_lock(&emul_lock);
    while (!emul_stop) {
        int64_t now = monotonic_ns();
        int64_t wait_ns = 200000000;
        // source images whose cube update the GPU has finished reading: the app may draw into them again
        for (int i = 0; i < EMUL_MAX_LAYERS; ++i) {
            emul_layer *l = &emul_layers[i];
            if (!l->busy) continue;
            if (p_glClientWaitSync(l->busy, 0, 0) == GL_TIMEOUT_EXPIRED) { wait_ns = 2000000; continue; }
            p_glDeleteSync(l->busy);
            l->busy = NULL;
            emul_source *s = emul_find_source(l->src, 0);
            if (s) s->reading = -1;
            pthread_cond_broadcast(&emul_done);
        }
        // 1. the current frame (xrEndFrame is waiting for it)
        if (emul_out.job) {
            emul_scene scene = emul_out.job_scene;
            XrView views[2] = {emul_out.job_views[0], emul_out.job_views[1]};
            uint64_t seq = emul_out.job_seq;
            XrTime time = emul_out.job_time;
            emul_out.job = 0;
            GLuint cubes[2][EMUL_MAX_PER_EYE] = {{0}};
            int drawable = scene.count[0] || scene.count[1];
            for (int e = 0; e < 2 && drawable; ++e)
                for (int k = 0; k < scene.count[e]; ++k) {
                    emul_layer *l = &emul_layers[scene.entries[e][k].slot];
                    if (l->failed) continue;  // skipped; the other layers are still drawn
                    if (!l->used || !l->ready || !l->cube) drawable = 0;
                    else cubes[e][k] = l->cube;
                }
            int ok = -1;
            if (drawable) {
                pthread_mutex_unlock(&emul_lock);
                ok = emul_draw_view(&scene, cubes, views);
                pthread_mutex_lock(&emul_lock);
                if (!ok) {
                    emul_disable("drawing the 360 view failed");
                    emul_out.done_seq = seq;
                    pthread_cond_broadcast(&emul_done);
                    break;
                }
                if (ok > 0) {
                    if (!emul_out.ready) LOG("equirect_emul: first 360 view ready");
                    emul_out.drawn = scene;
                    emul_out.views[0] = views[0];
                    emul_out.views[1] = views[1];
                    emul_out.drawn_time = time;
                    emul_out.ready = 1;
                }
            }
            emul_out.done_seq = seq;
            emul_out.done_ok = ok > 0;
            pthread_cond_broadcast(&emul_done);
            emul_stats();
            if (ok > 0) {  // get the next image ready while the app works on its next frame
                pthread_mutex_unlock(&emul_lock);
                emul_preacquire();
                pthread_mutex_lock(&emul_lock);
            }
            continue;
        }
        // 2. a source image that changed: update its cube map (the next frame shows it)
        int64_t min_interval = equirect_fps > 0 ? (int64_t)(1e9 / equirect_fps) : 0;
        emul_layer *job = NULL;
        for (int i = 0; i < EMUL_MAX_LAYERS && !job; ++i) {
            emul_layer *l = &emul_layers[i];
            if (!l->used) continue;
            if (!l->retire && l->last_used && now - l->last_used > EMUL_IDLE_NS) l->retire = 1;
            if (l->retire && !l->busy) {  // its source is gone or unused: free the cube map
                if (l->cube) p_glDeleteTextures(1, &l->cube);
                memset(l, 0, sizeof(*l));
                continue;
            }
            if (l->retire || l->busy || l->failed) continue;
            emul_source *s = emul_find_source(l->src, 0);
            if (!s || !s->image_count) continue;
            int index = s->last_released;  // only finished pictures (a new video's swapchain starts empty)
            if (index < 0 || (uint32_t)index >= s->image_count) continue;
            if (l->ready && s->generation == l->rendered_src_gen && l->params_gen == l->rendered_params_gen) continue;
            if (l->ready && now - l->rendered_at < min_interval) {
                if (min_interval - (now - l->rendered_at) < wait_ns) wait_ns = min_interval - (now - l->rendered_at);
                continue;
            }
            job = l;
        }
        if (job) {
            emul_source *s = emul_find_source(job->src, 0);
            int index = s->last_released;
            GLsync fence = s->fences[index];
            s->fences[index] = NULL;  // the worker owns (waits on, deletes) it now
            s->reading = index;       // until the GPU has read it (busy fence above)
            emul_layer snapshot = *job;
            emul_source src = *s;
            uint64_t gen = s->generation, params_gen = job->params_gen;
            pthread_mutex_unlock(&emul_lock);
            int ok = emul_draw_cube(job, snapshot, src, index, fence);
            pthread_mutex_lock(&emul_lock);
            if (!ok) {  // only this source is dropped; the source image is free again
                LOG("equirect_emul: a %ux%u 360 image can't be shown (cube map failed); other 360 layers still show",
                    snapshot.rect.extent.width, snapshot.rect.extent.height);
                job->failed = 1;
                if (s) s->reading = -1;
                pthread_cond_broadcast(&emul_done);
                // (Rebuilding the GL context here froze 4XVR on the Frame's driver: never do that.)
                continue;
            }
            if (!job->ready) LOG("equirect_emul: first 360 image converted");
            job->ready = 1;
            job->rendered_src_gen = gen;
            job->rendered_params_gen = params_gen;
            job->rendered_at = monotonic_ns();
            emul_gl.why_cube++;
            continue;
        }
        emul_stats();
        struct timespec until;
        clock_gettime(CLOCK_REALTIME, &until);
        until.tv_nsec += (long)wait_ns;
        while (until.tv_nsec >= 1000000000) { until.tv_sec += 1; until.tv_nsec -= 1000000000; }
        pthread_cond_timedwait(&emul_wake, &emul_lock, &until);
    }
    for (int i = 0; i < EMUL_MAX_LAYERS; ++i)  // nothing may wait on these any more
        if (emul_layers[i].busy) { p_glDeleteSync(emul_layers[i].busy); emul_layers[i].busy = NULL; }
    for (int i = 0; i < EMUL_MAX_SOURCES; ++i) emul_sources[i].reading = -1;
    pthread_cond_broadcast(&emul_done);
    pthread_mutex_unlock(&emul_lock);
    emul_gl_shutdown();  // the projection swapchain is destroyed by emul_on_destroy_session
    return NULL;
}

static void emul_on_destroy_session(XrSession session) {
    if (!equirect_emul || session != emul_session) return;
    pthread_mutex_lock(&emul_lock);
    emul_stop = 1;
    emul_out.ready = 0;
    pthread_cond_broadcast(&emul_wake);
    pthread_mutex_unlock(&emul_lock);
    if (emul_thread) pthread_join(emul_thread, NULL);
    emul_thread = 0;
    PFN_xrDestroySwapchain destroy_swapchain = (PFN_xrDestroySwapchain)lookup(active_instance, "xrDestroySwapchain");
    if (emul_out.swapchain && destroy_swapchain) destroy_swapchain(emul_out.swapchain);
    PFN_xrDestroySpace destroy_space = (PFN_xrDestroySpace)lookup(active_instance, "xrDestroySpace");
    if (emul_view_space && destroy_space) destroy_space(emul_view_space);
    pthread_mutex_lock(&emul_lock);
    memset(emul_layers, 0, sizeof(emul_layers));
    memset(&emul_out, 0, sizeof(emul_out));
    for (int i = 0; i < EMUL_MAX_SOURCES; ++i) emul_sources[i].watched = 0, emul_sources[i].reading = -1;
    emul_session = XR_NULL_HANDLE;
    emul_view_space = XR_NULL_HANDLE;
    emul_state = 0;
    emul_stop = 0;
    pthread_mutex_unlock(&emul_lock);
}

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrDestroySession(XrSession session) {
    emul_on_destroy_session(session);
    PFN_xrDestroySession fn = (PFN_xrDestroySession)lookup(active_instance, "xrDestroySession");
    return fn ? fn(session) : XR_ERROR_FUNCTION_UNSUPPORTED;
}

// ---- xrEndFrame side (app thread)
static int emul_is_equirect(const XrCompositionLayerBaseHeader *layer) {
    return layer && (layer->type == XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR || layer->type == XR_TYPE_COMPOSITION_LAYER_EQUIRECT_KHR);
}

typedef struct {
    XrSwapchainSubImage sub;
    XrPosef pose;
    XrSpace space;
    XrEyeVisibility eye;
} emul_common;

static emul_common emul_common_of(const XrCompositionLayerBaseHeader *layer) {
    if (layer->type == XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR) {
        const XrCompositionLayerEquirect2KHR *e = (const XrCompositionLayerEquirect2KHR *)layer;
        return (emul_common){e->subImage, e->pose, e->space, e->eyeVisibility};
    }
    const XrCompositionLayerEquirectKHR *e = (const XrCompositionLayerEquirectKHR *)layer;
    return (emul_common){e->subImage, e->pose, e->space, e->eyeVisibility};
}

// Finds/creates the cube-map slot of this source and updates its mapping (emul_lock held). -1 if none is free.
static int emul_track(const XrCompositionLayerBaseHeader *layer) {
    emul_common c = emul_common_of(layer);
    emul_source *s = emul_find_source(c.sub.swapchain, 0);
    if (!s) return -1;
    s->watched = 1;
    emul_layer *slot = NULL, *free_slot = NULL;
    for (int i = 0; i < EMUL_MAX_LAYERS && !slot; ++i) {
        emul_layer *l = &emul_layers[i];
        if (!l->used) { if (!free_slot) free_slot = l; continue; }
        if (!l->retire && l->src == c.sub.swapchain && l->array_index == c.sub.imageArrayIndex &&
            !memcmp(&l->rect, &c.sub.imageRect, sizeof(l->rect)))
            slot = l;
    }
    int64_t now = monotonic_ns();
    if (!slot) {
        if (!free_slot) {  // all slots taken: give back the one unused longest (freed by the worker)
            emul_layer *oldest = NULL;
            for (int i = 0; i < EMUL_MAX_LAYERS; ++i)
                if (!emul_layers[i].retire && (!oldest || emul_layers[i].last_used < oldest->last_used)) oldest = &emul_layers[i];
            if (oldest && now - oldest->last_used > 1000000000ll) { oldest->retire = 1; pthread_cond_broadcast(&emul_wake); }
            return -1;
        }
        slot = free_slot;
        memset(slot, 0, sizeof(*slot));
        slot->used = 1;
        slot->src = c.sub.swapchain;
        slot->rect = c.sub.imageRect;
        slot->array_index = c.sub.imageArrayIndex;
        slot->type = layer->type;
        slot->params_gen = 1;
    }
    slot->last_used = now;
    float central = 0, upper = 0, lower = 0;
    XrVector2f scale = {1, 1}, bias = {0, 0};
    if (layer->type == XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR) {
        const XrCompositionLayerEquirect2KHR *e = (const XrCompositionLayerEquirect2KHR *)layer;
        central = e->centralHorizontalAngle; upper = e->upperVerticalAngle; lower = e->lowerVerticalAngle;
    } else {
        const XrCompositionLayerEquirectKHR *e = (const XrCompositionLayerEquirectKHR *)layer;
        scale = e->scale; bias = e->bias;
    }
    if (slot->type != layer->type || slot->central != central || slot->upper != upper || slot->lower != lower ||
        memcmp(&slot->scale, &scale, sizeof(scale)) || memcmp(&slot->bias, &bias, sizeof(bias))) {
        slot->type = layer->type;
        slot->central = central; slot->upper = upper; slot->lower = lower;
        slot->scale = scale; slot->bias = bias;
        ++slot->params_gen;
    }
    if (!slot->ready || s->generation != slot->rendered_src_gen || slot->params_gen != slot->rendered_params_gen)
        pthread_cond_broadcast(&emul_wake);
    return (int)(slot - emul_layers);
}

static void emul_start_worker(void) {  // emul_lock held
    if (emul_state != 0) return;
    if (!p_eglCreateContext && !emul_load_gl()) {
        LOG("equirect_emul: libEGL/libGLESv3 entry points missing, disabled");
        emul_state = -1;
        return;
    }
    emul_stop = 0;
    if (pthread_create(&emul_thread, NULL, emul_worker, NULL)) { emul_state = -1; emul_thread = 0; return; }
    emul_state = 1;
}

// Same 360 layers as `b`, ignoring the per-frame pose jitter apps have (4XVR: ~1e-4) and tiny dimming steps?
static int emul_scene_same(const emul_scene *a, const emul_scene *b) {
    if (a->space != b->space || a->count[0] != b->count[0] || a->count[1] != b->count[1]) return 0;
    for (int e = 0; e < 2; ++e)
        for (int k = 0; k < a->count[e]; ++k) {
            const emul_entry *x = &a->entries[e][k], *y = &b->entries[e][k];
            if (x->slot != y->slot || lm_angle(lm_qmul(x->orientation, lm_qconj(y->orientation))) > 0.0035f) return 0;
            const float *p = &x->scale.r, *q = &y->scale.r;  // scale and bias: 8 consecutive floats
            for (int c = 0; c < 8; ++c)
                if (fabsf(p[c] - q[c]) > 1.0f / 256) return 0;
        }
    return 1;
}

// xrLocateViews (app thread): the app asks where the eyes will be for its next frame. Draw the background for exactly
// those views right away, so its GPU work runs before the app's own rendering instead of after it (late work delays
// the whole frame). xrEndFrame uses it if the frame's 360 layers and display time match.
static void emul_on_locate_views(XrSpace space, XrTime time, uint32_t count, const XrView *views) {
    if (!emul_active() || count < 2 || !views || !time) return;
    pthread_mutex_lock(&emul_lock);
    if (emul_state == 1 && emul_out.ready && !emul_out.job && emul_out.last_scene.space == space &&
        emul_out.early_time != time && (emul_out.last_scene.count[0] || emul_out.last_scene.count[1])) {
        emul_out.job = 1;
        emul_out.job_scene = emul_out.last_scene;
        emul_out.job_views[0] = views[0];
        emul_out.job_views[1] = views[1];
        emul_out.job_time = time;
        emul_out.early_seq = ++emul_out.job_seq;
        emul_out.early_time = time;
        pthread_cond_broadcast(&emul_wake);
    }
    pthread_mutex_unlock(&emul_lock);
}

static XrCompositionLayerProjection emul_projection;
static XrCompositionLayerProjectionView emul_projection_views[2];

// Called once per frame (app thread). Draws this frame's 360° layers (via the worker) for exactly the views this frame
// is shown with, and returns the index of the first 360° layer, to be replaced by *layer_out (the emulated projection
// layer; NULL while nothing is ready: the 360° layers are then dropped as without the setting). -1: the frame has no
// 360° layers or the emulation is off.
static int emul_prepare_frame(const XrFrameEndInfo *info, const XrCompositionLayerBaseHeader **layer_out) {
    *layer_out = NULL;
    int first = -1;
    for (uint32_t i = 0; i < info->layerCount && first < 0; ++i)
        if (emul_is_equirect(info->layers[i])) first = (int)i;
    if (first < 0) return -1;
    emul_scene scene;
    memset(&scene, 0, sizeof(scene));
    scene.space = emul_common_of(info->layers[first]).space;
    // the views: the app's own projection layer in the same space if there is one, else the runtime's for this frame
    XrView views[2] = {{XR_TYPE_VIEW, NULL, {{0, 0, 0, 1}, {0, 0, 0}}, {0, 0, 0, 0}},
                       {XR_TYPE_VIEW, NULL, {{0, 0, 0, 1}, {0, 0, 0}}, {0, 0, 0, 0}}};
    int have_views = 0;
    for (uint32_t i = 0; i < info->layerCount && !have_views; ++i) {
        const XrCompositionLayerBaseHeader *l = info->layers[i];
        if (!l || l->type != XR_TYPE_COMPOSITION_LAYER_PROJECTION) continue;
        const XrCompositionLayerProjection *p = (const XrCompositionLayerProjection *)l;
        if (p->space != scene.space || p->viewCount != 2) continue;
        for (int e = 0; e < 2; ++e) { views[e].pose = p->views[e].pose; views[e].fov = p->views[e].fov; }
        have_views = 1;
    }
    if (!have_views) {
        PFN_xrLocateViews locate_views = (PFN_xrLocateViews)lookup(active_instance, "xrLocateViews");
        XrViewLocateInfo li = {XR_TYPE_VIEW_LOCATE_INFO, NULL, emul_view_config, info->displayTime, scene.space};
        XrViewState vs = {XR_TYPE_VIEW_STATE, NULL, 0};
        uint32_t n = 0;
        have_views = locate_views && XR_SUCCEEDED(locate_views(emul_session, &li, &vs, 2, &n, views)) && n == 2 &&
                     (vs.viewStateFlags & XR_VIEW_STATE_ORIENTATION_VALID_BIT);
    }
    pthread_mutex_lock(&emul_lock);
    emul_start_worker();
    if (emul_state < 0) { pthread_mutex_unlock(&emul_lock); return -1; }
    static int warned_space;
    for (uint32_t i = (uint32_t)first; i < info->layerCount; ++i) {
        const XrCompositionLayerBaseHeader *layer = info->layers[i];
        if (!emul_is_equirect(layer) || !known_swapchain(emul_common_of(layer).sub.swapchain)) continue;
        emul_common c = emul_common_of(layer);
        if (c.space != scene.space) {
            if (!warned_space++) LOG("equirect_emul: 360 layers in different spaces; only the first space's are shown");
            continue;
        }
        int slot = emul_track(layer);
        if (slot < 0) continue;
        emul_entry en = {slot, c.pose.orientation, {1, 1, 1, 1}, {0, 0, 0, 0}};
        for (const XrBaseInStructure *n = (const XrBaseInStructure *)layer->next; n; n = n->next)
            if (n->type == XR_TYPE_COMPOSITION_LAYER_COLOR_SCALE_BIAS_KHR) {
                en.scale = ((const XrCompositionLayerColorScaleBiasKHR *)n)->colorScale;
                en.bias = ((const XrCompositionLayerColorScaleBiasKHR *)n)->colorBias;
            }
        for (int e = 0; e < 2; ++e) {
            int shows = c.eye == XR_EYE_VISIBILITY_BOTH || (int)c.eye == e + 1;
            if (equirect_stereo == 2 && c.eye != XR_EYE_VISIBILITY_BOTH) shows = c.eye == XR_EYE_VISIBILITY_LEFT;  // mono
            if (shows && scene.count[e] < EMUL_MAX_PER_EYE) scene.entries[e][scene.count[e]++] = en;
        }
    }
    emul_out.last_scene = scene;
    if (!have_views || (!scene.count[0] && !scene.count[1])) { pthread_mutex_unlock(&emul_lock); return first; }
    uint64_t seq;
    if (emul_out.early_seq && emul_out.early_time == info->displayTime && emul_scene_same(&scene, &emul_out.job_scene)) {
        seq = emul_out.early_seq;  // drawn (or being drawn) since xrLocateViews for this very frame
        emul_out.early_hits++;
    } else {  // hand this frame to the worker now and wait (bounded) until it has submitted the draw
        if (emul_out.job) emul_out.job = 0;  // a stale early job: replaced
        emul_out.job = 1;
        emul_out.job_scene = scene;
        emul_out.job_views[0] = views[0];
        emul_out.job_views[1] = views[1];
        emul_out.job_time = info->displayTime;
        seq = ++emul_out.job_seq;
        pthread_cond_broadcast(&emul_wake);
    }
    struct timespec until;
    clock_gettime(CLOCK_REALTIME, &until);
    until.tv_nsec += EMUL_FRAME_WAIT_NS;
    if (until.tv_nsec >= 1000000000) { until.tv_sec += 1; until.tv_nsec -= 1000000000; }
    int64_t wait_start = monotonic_ns();
    while (emul_out.done_seq < seq && emul_state >= 0 && pthread_cond_timedwait(&emul_done, &emul_lock, &until) == 0) {}
    double waited = (monotonic_ns() - wait_start) / 1e6;
    emul_out.wait_ms += waited;
    emul_out.waits++;
    if (waited > emul_out.wait_max) emul_out.wait_max = waited;
    int fresh = emul_out.done_seq >= seq && emul_out.done_ok && emul_out.drawn_time == info->displayTime;
    int ready = emul_out.ready && emul_out.drawn.space == scene.space;
    if (!fresh && ready) emul_out.late++;
    XrView shown[2] = {emul_out.views[0], emul_out.views[1]};
    XrSwapchain swapchain = emul_out.swapchain;
    uint32_t W = emul_out.size;
    pthread_mutex_unlock(&emul_lock);
    if (!ready) return first;
    for (int e = 0; e < 2; ++e)
        emul_projection_views[e] = (XrCompositionLayerProjectionView){XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW, NULL,
            shown[e].pose, shown[e].fov, {swapchain, {{(int32_t)(e * W), 0}, {(int32_t)W, (int32_t)W}}, 0}};
    emul_projection = (XrCompositionLayerProjection){XR_TYPE_COMPOSITION_LAYER_PROJECTION, NULL, 0, scene.space, 2,
                                                     emul_projection_views};
    *layer_out = (const XrCompositionLayerBaseHeader *)&emul_projection;
    return first;
}
