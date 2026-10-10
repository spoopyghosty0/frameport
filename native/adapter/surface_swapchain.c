// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c (after flip_vk.c and layer_emul_gl.c). Emulates XR_KHR_android_surface_swapchain,
// which the Frame's runtime lists but answers with XR_ERROR_FUNCTION_UNSUPPORTED. Games use it to play a video on a
// panel (OVROverlay "external surface", Android MediaPlayer/ExoPlayer drawing into a Surface): without it the panel has
// no image and is dropped, and a game that waits for its intro video shows black forever (I Am Monkey).
//
//  * xrCreateSwapchainAndroidSurfaceKHR: the runtime first; if it can't, an ordinary runtime swapchain of the same size
//    (sRGB RGBA8) is created and returned as the game's swapchain, and the game gets a real android.view.Surface from a
//    SurfaceTexture owned by a worker thread (own EGL context; JNI through the JavaVM of xrCreateInstance).
//  * The worker latches each new video frame (any size, YUV: SurfaceTexture samples it), draws it into an RGBA8
//    texture of the swapchain's size and reads it back (glReadPixels) into a shared buffer.
//  * xrEndFrame: a layer showing an emulated swapchain gets the newest frame uploaded first (Vulkan: staging buffer +
//    copy on the session's queue; GLES: glTexSubImage2D in the app's context), then is submitted as usual.
//  * Any failure: the call returns the runtime's own result (as before), so nothing gets worse.
#include <jni.h>
#include <android/hardware_buffer.h>
#include <android/native_window_jni.h>
#include <media/NdkImageReader.h>
#include <EGL/eglext.h>
#include <GLES2/gl2ext.h>
#include "surface_video.h"

#define SURF_NATIVE_SLOTS 4
typedef struct {
    AHardwareBuffer *buffer;
    VkImage image;
    VkImageView view;
    VkDeviceMemory memory;
    EGLImageKHR egl_image;
    GLuint texture;
    uint32_t width,height;
    int state; // 0 reusable, 1 worker writing, 2 published, 3 current/pending Vulkan sampler
    uint64_t seq;
    uint32_t source_width,source_height;
    int64_t source_time;
    surf_video_job video;
    int projected;
} surf_native_slot;

#define SURF_MAX 8

typedef void *(*PFN_ASurfaceTexture_fromSurfaceTexture)(JNIEnv *, jobject);
typedef int (*PFN_ASurfaceTexture_updateTexImage)(void *);
typedef void (*PFN_ASurfaceTexture_getTransformMatrix)(void *, float[16]);
typedef int64_t (*PFN_ASurfaceTexture_getTimestamp)(void *);
typedef void (*PFN_ASurfaceTexture_release)(void *);

#define surf_vm ((JavaVM *)android_vm)  // from XrInstanceCreateInfoAndroidKHR (frame_adapter.c)

typedef struct {
    int used;
    XrSwapchain handle;
    uint32_t width, height;
    // worker
    pthread_t thread;
    int stop, ready, failed;  // ready: surface handed out; failed: worker gave up
    jobject surface;          // global ref, returned to the game
    // newest frame (RGBA8, top row first), written by the worker
    uint8_t *pixels;
    uint64_t seq;             // bumps with every new frame
    int64_t frames;
    // upload side (app render thread)
    uint64_t uploaded_seq;
    int uploads, upload_failures, has_content;
    VkImage vk_images[MAX_IMAGES];
    uint32_t gl_images[MAX_IMAGES];
    uint32_t image_count;
    VkBuffer staging;
    VkDeviceMemory staging_memory;
    void *staging_map;
    VkCommandPool pool;  // own command buffer + fence: uploads never wait for the GPU in xrEndFrame
    VkCommandBuffer cmd;
    VkFence fence;
    int in_flight;      // a copy was submitted and its fence not yet seen signalled
    int native_mode, native_pending, native_current, native_project;
    surf_video_job video_job;
    surf_native_slot native_slots[SURF_NATIVE_SLOTS];
} surf_swapchain;

static pthread_mutex_t surf_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t surf_cond = PTHREAD_COND_INITIALIZER;
static surf_swapchain surfs[SURF_MAX];
static int surf_vulkan;  // the session is Vulkan (else GLES)

static surf_swapchain *surf_find(XrSwapchain handle) {
    for (int i = 0; i < SURF_MAX; ++i)
        if (surfs[i].used && surfs[i].handle == handle) return &surfs[i];
    return NULL;
}

// ---------------------------------------------------------------- worker: SurfaceTexture -> RGBA pixels
static const char *surf_vs = "#version 300 es\nout vec2 uv;\nvoid main() {\n"
    "  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));\n"
    "  uv = p; gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);\n}\n";
// window row 0 (glReadPixels' first row) must hold the image's top: sample t = 1 - y (SurfaceTexture t=0 is the bottom)
static const char *surf_fs = "#version 300 es\n#extension GL_OES_EGL_image_external_essl3 : require\n"
    "precision highp float;\nuniform samplerExternalOES tex;\nuniform mat4 st;\nin vec2 uv;\nout vec4 color;\n"
    "void main() { color = texture(tex, (st * vec4(uv.x, 1.0 - uv.y, 0.0, 1.0)).xy); }\n";

typedef void (*PFN_glReadPixels_)(GLint, GLint, GLsizei, GLsizei, GLenum, GLenum, void *);
typedef void (*PFN_glTexParameteri_)(GLenum, GLenum, GLint);
typedef void (*PFN_glUniformMatrix4fv_)(GLint, GLsizei, GLboolean, const GLfloat *);
typedef void (*PFN_glPixelStorei_)(GLenum, GLint);
typedef void (*PFN_glTexSubImage2D_)(GLenum, GLint, GLint, GLint, GLsizei, GLsizei, GLenum, GLenum, const void *);
typedef EGLDisplay (*PFN_eglGetDisplay_)(EGLNativeDisplayType);
typedef EGLBoolean (*PFN_eglInitialize_)(EGLDisplay, EGLint *, EGLint *);
static PFN_glReadPixels_ s_glReadPixels;
static PFN_glTexParameteri_ s_glTexParameteri;
static PFN_glUniformMatrix4fv_ s_glUniformMatrix4fv;
static PFN_glPixelStorei_ s_glPixelStorei;
static PFN_glTexSubImage2D_ s_glTexSubImage2D;
static PFN_eglGetDisplay_ s_eglGetDisplay;
static PFN_eglInitialize_ s_eglInitialize;
static PFN_ASurfaceTexture_fromSurfaceTexture s_ast_from;
static PFN_ASurfaceTexture_updateTexImage s_ast_update;
static PFN_ASurfaceTexture_getTransformMatrix s_ast_matrix;
static PFN_ASurfaceTexture_getTimestamp s_ast_timestamp;
static PFN_ASurfaceTexture_release s_ast_release;

static int surf_load(void) {
    static int state;  // 0 untried, 1 ok, -1 failed
    if (state) return state > 0;
    state = -1;
    if (!emul_load_gl()) { LOG("surface_emul: EGL/GLES not available"); return 0; }
    void *egl = dlopen("libEGL.so", RTLD_NOW | RTLD_LOCAL), *gles = dlopen("libGLESv3.so", RTLD_NOW | RTLD_LOCAL);
    void *android = dlopen("libandroid.so", RTLD_NOW | RTLD_LOCAL);
    if (!egl || !gles || !android) { LOG("surface_emul: libEGL/libGLESv3/libandroid missing"); return 0; }
    s_glReadPixels = (PFN_glReadPixels_)dlsym(gles, "glReadPixels");
    s_glTexParameteri = (PFN_glTexParameteri_)dlsym(gles, "glTexParameteri");
    s_glUniformMatrix4fv = (PFN_glUniformMatrix4fv_)dlsym(gles, "glUniformMatrix4fv");
    s_glPixelStorei = (PFN_glPixelStorei_)dlsym(gles, "glPixelStorei");
    s_glTexSubImage2D = (PFN_glTexSubImage2D_)dlsym(gles, "glTexSubImage2D");
    s_eglGetDisplay = (PFN_eglGetDisplay_)dlsym(egl, "eglGetDisplay");
    s_eglInitialize = (PFN_eglInitialize_)dlsym(egl, "eglInitialize");
    s_ast_from = (PFN_ASurfaceTexture_fromSurfaceTexture)dlsym(android, "ASurfaceTexture_fromSurfaceTexture");
    s_ast_update = (PFN_ASurfaceTexture_updateTexImage)dlsym(android, "ASurfaceTexture_updateTexImage");
    s_ast_matrix = (PFN_ASurfaceTexture_getTransformMatrix)dlsym(android, "ASurfaceTexture_getTransformMatrix");
    s_ast_timestamp = (PFN_ASurfaceTexture_getTimestamp)dlsym(android, "ASurfaceTexture_getTimestamp");
    s_ast_release = (PFN_ASurfaceTexture_release)dlsym(android, "ASurfaceTexture_release");
    if (!s_glReadPixels || !s_glTexParameteri || !s_glUniformMatrix4fv || !s_glPixelStorei || !s_glTexSubImage2D ||
        !s_eglGetDisplay || !s_eglInitialize || !s_ast_from || !s_ast_update || !s_ast_matrix || !s_ast_timestamp ||
        !s_ast_release) {
        LOG("surface_emul: a GL/EGL/ASurfaceTexture function is missing");
        return 0;
    }
    state = 1;
    return 1;
}

static void surf_fail(surf_swapchain *s, const char *why) {
    LOG("surface_emul: %s", why);
    pthread_mutex_lock(&surf_lock);
    s->failed = 1;
    pthread_cond_broadcast(&surf_cond);
    pthread_mutex_unlock(&surf_lock);
}

#include "surface_native.c"

static void *surf_worker(void *arg) {
    surf_swapchain *s = arg;
    JNIEnv *env = NULL;
    if ((*surf_vm)->AttachCurrentThread(surf_vm, &env, NULL) != JNI_OK || !env) { surf_fail(s, "no JNI env"); return NULL; }
    EGLDisplay dpy = s_eglGetDisplay(EGL_DEFAULT_DISPLAY);
    EGLint cfg_attribs[] = {EGL_RENDERABLE_TYPE, 0x40 /* EGL_OPENGL_ES3_BIT */, EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
                            EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE};
    EGLConfig config = NULL;
    EGLint n = 0, ctx_attribs[] = {EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE}, pb[] = {EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE};
    EGLContext ctx = EGL_NO_CONTEXT;
    EGLSurface pbuf = EGL_NO_SURFACE;
    void *ast = NULL;
    jobject st_local = NULL, st_global = NULL;
    GLuint ext_tex = 0, rgba_tex = 0, fbo = 0, vao = 0, program = 0;
    if (dpy == EGL_NO_DISPLAY || !s_eglInitialize(dpy, NULL, NULL) || !p_eglChooseConfig(dpy, cfg_attribs, &config, 1, &n) ||
        n < 1 || (ctx = p_eglCreateContext(dpy, config, EGL_NO_CONTEXT, ctx_attribs)) == EGL_NO_CONTEXT ||
        (pbuf = p_eglCreatePbufferSurface(dpy, config, pb)) == EGL_NO_SURFACE || !p_eglMakeCurrent(dpy, pbuf, pbuf, ctx)) {
        surf_fail(s, "EGL context failed");
        goto out;
    }
    if(s->native_mode){surf_native_worker(s,env,dpy);goto out;}
    // the SurfaceTexture's external texture, and the RGBA target the frames are drawn into
    p_glGenTextures(1, &ext_tex);
    p_glBindTexture(0x8D65 /* GL_TEXTURE_EXTERNAL_OES */, ext_tex);
    s_glTexParameteri(0x8D65, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    s_glTexParameteri(0x8D65, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    p_glGenTextures(1, &rgba_tex);
    p_glBindTexture(GL_TEXTURE_2D, rgba_tex);
    p_glTexStorage2D(GL_TEXTURE_2D, 1, GL_RGBA8, (GLsizei)s->width, (GLsizei)s->height);
    p_glGenFramebuffers(1, &fbo);
    p_glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    p_glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, rgba_tex, 0);
    if (p_glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) { surf_fail(s, "framebuffer incomplete"); goto out; }
    {
        GLuint v = emul_compile(GL_VERTEX_SHADER, "", surf_vs), f = emul_compile(GL_FRAGMENT_SHADER, "", surf_fs);
        if (!v || !f) { surf_fail(s, "shader compile failed"); goto out; }
        program = p_glCreateProgram();
        p_glAttachShader(program, v);
        p_glAttachShader(program, f);
        p_glLinkProgram(program);
        GLint ok = 0;
        p_glGetProgramiv(program, GL_LINK_STATUS, &ok);
        if (!ok) { surf_fail(s, "program link failed"); goto out; }
    }
    p_glGenVertexArrays(1, &vao);
    // android.graphics.SurfaceTexture(texName) + android.view.Surface(SurfaceTexture)
    {
        jclass st_class = (*env)->FindClass(env, "android/graphics/SurfaceTexture");
        jclass surface_class = (*env)->FindClass(env, "android/view/Surface");
        if (!st_class || !surface_class) { (*env)->ExceptionClear(env); surf_fail(s, "SurfaceTexture/Surface class missing"); goto out; }
        jmethodID st_init = (*env)->GetMethodID(env, st_class, "<init>", "(I)V");
        jmethodID st_size = (*env)->GetMethodID(env, st_class, "setDefaultBufferSize", "(II)V");
        jmethodID surface_init = (*env)->GetMethodID(env, surface_class, "<init>", "(Landroid/graphics/SurfaceTexture;)V");
        st_local = (*env)->NewObject(env, st_class, st_init, (jint)ext_tex);
        if (!st_local || (*env)->ExceptionCheck(env)) { (*env)->ExceptionClear(env); surf_fail(s, "new SurfaceTexture failed"); goto out; }
        (*env)->CallVoidMethod(env, st_local, st_size, (jint)s->width, (jint)s->height);
        jobject surface_local = (*env)->NewObject(env, surface_class, surface_init, st_local);
        if (!surface_local || (*env)->ExceptionCheck(env)) { (*env)->ExceptionClear(env); surf_fail(s, "new Surface failed"); goto out; }
        st_global = (*env)->NewGlobalRef(env, st_local);
        ast = s_ast_from(env, st_local);
        if (!ast) { surf_fail(s, "ASurfaceTexture_fromSurfaceTexture failed"); goto out; }
        pthread_mutex_lock(&surf_lock);
        s->surface = (*env)->NewGlobalRef(env, surface_local);
        s->ready = 1;
        pthread_cond_broadcast(&surf_cond);
        pthread_mutex_unlock(&surf_lock);
        (*env)->DeleteLocalRef(env, surface_local);
    }
    LOG("surface_emul: Surface ready for %ux%u swapchain %p", s->width, s->height, (void *)(uintptr_t)s->handle);
    {
        GLint u_tex = p_glGetUniformLocation(program, "tex"), u_st = p_glGetUniformLocation(program, "st");
        size_t bytes = (size_t)s->width * s->height * 4;
        uint8_t *buf = malloc(bytes);
        int64_t last_ts = -1, window_frames = 0, window_start = 0;
        int64_t latch_ns = 0, read_ns = 0, copy_ns = 0, latch_calls = 0;
        while (!s->stop && buf) {
            struct timespec pause = {0, 8000000};  // ~120 checks a second: well above video frame rates
            nanosleep(&pause, NULL);
            int64_t tick = monotonic_ns();
            int latch_result = s_ast_update(ast);
            latch_ns += monotonic_ns() - tick;
            latch_calls++;
            if (latch_result != 0) continue;
            int64_t ts = s_ast_timestamp(ast);
            if (ts == last_ts) continue;  // no new frame latched
            last_ts = ts;
            float m[16];
            s_ast_matrix(ast, m);
            p_glBindFramebuffer(GL_FRAMEBUFFER, fbo);
            p_glViewport(0, 0, (GLsizei)s->width, (GLsizei)s->height);
            p_glUseProgram(program);
            p_glActiveTexture(GL_TEXTURE0);
            p_glBindTexture(0x8D65, ext_tex);
            p_glUniform1i(u_tex, 0);
            s_glUniformMatrix4fv(u_st, 1, GL_FALSE, m);
            p_glBindVertexArray(vao);
            p_glDrawArrays(GL_TRIANGLES, 0, 3);
            s_glPixelStorei(GL_PACK_ALIGNMENT, 1);
            tick = monotonic_ns();
            s_glReadPixels(0, 0, (GLsizei)s->width, (GLsizei)s->height, GL_RGBA, GL_UNSIGNED_BYTE, buf);
            read_ns += monotonic_ns() - tick;
            GLenum err = p_glGetError();
            tick = monotonic_ns();
            pthread_mutex_lock(&surf_lock);
            if (!err && s->pixels) { memcpy(s->pixels, buf, bytes); s->seq++; s->frames++; }
            pthread_mutex_unlock(&surf_lock);
            copy_ns += monotonic_ns() - tick;
            if (err) { static int warned; if (warned++ < 5) LOG("surface_emul: GL error 0x%x drawing a frame", err); }
            int64_t now = monotonic_ns();
            if (!window_start) {
                window_start = now;
                LOG("surface_emul: first video frame (timestamp %lld)", (long long)ts);
            }
            if (++window_frames && now - window_start > 5000000000ll) {
                LOG("surface_emul: %.1f video frames/s, %d uploaded, %d upload failures", window_frames * 1e9 / (now - window_start),
                    s->uploads, s->upload_failures);
                LOG("surface_emul: timing latch %.2f ms/call (%lld calls), readback %.2f ms/frame, publish %.2f ms/frame, timestamp %lld",
                    latch_calls ? latch_ns / (1e6 * latch_calls) : 0.0, (long long)latch_calls,
                    read_ns / (1e6 * window_frames), copy_ns / (1e6 * window_frames), (long long)ts);
                window_frames = 0;
                latch_ns = read_ns = copy_ns = latch_calls = 0;
                window_start = now;
            }
        }
        free(buf);
    }
out:
    if (ast) s_ast_release(ast);
    if (st_global) (*env)->DeleteGlobalRef(env, st_global);
    if (ctx != EGL_NO_CONTEXT) { p_eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT); p_eglDestroyContext(dpy, ctx); }
    if (pbuf != EGL_NO_SURFACE) p_eglDestroySurface(dpy, pbuf);
    (*surf_vm)->DetachCurrentThread(surf_vm);
    return NULL;
}

// ---------------------------------------------------------------- uploads (app render thread)
static struct {
    PFN_vkCreateBuffer CreateBuffer;
    PFN_vkGetBufferMemoryRequirements GetBufferMemoryRequirements;
    PFN_vkAllocateMemory AllocateMemory;
    PFN_vkBindBufferMemory BindBufferMemory;
    PFN_vkMapMemory MapMemory;
    PFN_vkCmdCopyBufferToImage CmdCopyBufferToImage;
    PFN_vkCmdClearColorImage CmdClearColorImage;
    PFN_vkGetPhysicalDeviceMemoryProperties GetPhysicalDeviceMemoryProperties;
    PFN_vkGetFenceStatus GetFenceStatus;
    int state;
} svk;

static int surf_vk_ready(void) {
    if (svk.state) return svk.state > 0;
    svk.state = -1;
    if (!vk_ready()) return 0;
#define SDEV(name) svk.name = (PFN_vk##name)vk.GetDeviceProcAddr(vk.device, "vk" #name); if (!svk.name) return 0;
    SDEV(CreateBuffer) SDEV(GetBufferMemoryRequirements) SDEV(AllocateMemory) SDEV(BindBufferMemory) SDEV(MapMemory)
    SDEV(CmdCopyBufferToImage) SDEV(CmdClearColorImage) SDEV(GetFenceStatus)
#undef SDEV
    void *lib = dlopen("libvulkan.so", RTLD_NOW | RTLD_LOCAL);
    PFN_vkGetInstanceProcAddr gipa = lib ? (PFN_vkGetInstanceProcAddr)dlsym(lib, "vkGetInstanceProcAddr") : NULL;
    svk.GetPhysicalDeviceMemoryProperties = gipa ? (PFN_vkGetPhysicalDeviceMemoryProperties)gipa(vk.instance,
        "vkGetPhysicalDeviceMemoryProperties") : NULL;
    if (!svk.GetPhysicalDeviceMemoryProperties) return 0;
    svk.state = 1;
    return 1;
}

static void surf_barrier(VkCommandBuffer cmd, VkImage image, VkImageLayout from, VkImageLayout to,
                         VkAccessFlags src_access, VkAccessFlags dst_access) {
    VkImageMemoryBarrier b = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER, NULL, src_access, dst_access, from, to,
                              VK_QUEUE_FAMILY_IGNORED, VK_QUEUE_FAMILY_IGNORED, image,
                              {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    vk.CmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, 0, 0, NULL, 0,
                          NULL, 1, &b);
}

static int surf_staging(surf_swapchain *s) {
    if (s->staging_map) return 1;
    VkCommandPoolCreateInfo pool = {VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, NULL,
                                    VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, vk.family};
    if (vk.CreateCommandPool(vk.device, &pool, NULL, &s->pool) != VK_SUCCESS) return 0;
    VkCommandBufferAllocateInfo alloc = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, NULL, s->pool,
                                         VK_COMMAND_BUFFER_LEVEL_PRIMARY, 1};
    if (vk.AllocateCommandBuffers(vk.device, &alloc, &s->cmd) != VK_SUCCESS) return 0;
    VkFenceCreateInfo fence = {VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, NULL, 0};
    if (vk.CreateFence(vk.device, &fence, NULL, &s->fence) != VK_SUCCESS) return 0;
    VkBufferCreateInfo bi = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, NULL, 0, (VkDeviceSize)s->width * s->height * 4,
                             VK_BUFFER_USAGE_TRANSFER_SRC_BIT, VK_SHARING_MODE_EXCLUSIVE, 0, NULL};
    if (svk.CreateBuffer(vk.device, &bi, NULL, &s->staging) != VK_SUCCESS) return 0;
    VkMemoryRequirements req;
    svk.GetBufferMemoryRequirements(vk.device, s->staging, &req);
    VkPhysicalDeviceMemoryProperties props;
    svk.GetPhysicalDeviceMemoryProperties(vk.physical, &props);
    uint32_t want = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT, type = UINT32_MAX;
    for (uint32_t i = 0; i < props.memoryTypeCount && type == UINT32_MAX; ++i)
        if ((req.memoryTypeBits & (1u << i)) && (props.memoryTypes[i].propertyFlags & want) == want) type = i;
    if (type == UINT32_MAX) return 0;
    VkMemoryAllocateInfo ai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL, req.size, type};
    if (svk.AllocateMemory(vk.device, &ai, NULL, &s->staging_memory) != VK_SUCCESS) return 0;
    if (svk.BindBufferMemory(vk.device, s->staging, s->staging_memory, 0) != VK_SUCCESS) return 0;
    return svk.MapMemory(vk.device, s->staging_memory, 0, VK_WHOLE_SIZE, 0, &s->staging_map) == VK_SUCCESS;
}

// Copy the newest frame (or black, the first time) into the next image of the runtime swapchain. Returns 1 if the
// swapchain now has an image to show, -1 if the previous copy is still running (try next frame), 0 on failure.
static int surf_upload(surf_swapchain *s, int black) {
    PFN_xrAcquireSwapchainImage acquire = (PFN_xrAcquireSwapchainImage)lookup(active_instance, "xrAcquireSwapchainImage");
    PFN_xrWaitSwapchainImage wait = (PFN_xrWaitSwapchainImage)lookup(active_instance, "xrWaitSwapchainImage");
    PFN_xrReleaseSwapchainImage release = (PFN_xrReleaseSwapchainImage)lookup(active_instance, "xrReleaseSwapchainImage");
    if (!acquire || !wait || !release || !s->image_count) return 0;
    uint32_t index = 0;
    XrSwapchainImageAcquireInfo ai = {XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO, NULL};
    XrSwapchainImageWaitInfo wi = {XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO, NULL, 100000000};
    XrSwapchainImageReleaseInfo ri = {XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO, NULL};
    if (surf_vulkan && s->in_flight) {  // the previous copy still reads the staging buffer: try again next frame
        if (svk.GetFenceStatus(vk.device, s->fence) != VK_SUCCESS) return -1;
        vk.ResetFences(vk.device, 1, &s->fence);
        s->in_flight = 0;
    }
    if (XR_FAILED(acquire(s->handle, &ai, &index)) || index >= s->image_count) return 0;
    if (XR_FAILED(wait(s->handle, &wi))) { release(s->handle, &ri); return 0; }
    int ok = 0;
    size_t bytes = (size_t)s->width * s->height * 4;
    if (surf_vulkan) {
        if (!black) {
            pthread_mutex_lock(&surf_lock);
            memcpy(s->staging_map, s->pixels, bytes);
            pthread_mutex_unlock(&surf_lock);
        }
        VkImage img = s->vk_images[index];
        VkCommandBufferBeginInfo begin = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, NULL,
                                          VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT, NULL};
        VkCommandBuffer cmd = s->cmd;
        vk.ResetCommandBuffer(cmd, 0);
        vk.BeginCommandBuffer(cmd, &begin);
        surf_barrier(cmd, img, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0, VK_ACCESS_TRANSFER_WRITE_BIT);
        if (black) {
            VkClearColorValue c = {{0, 0, 0, 1}};
            VkImageSubresourceRange r = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
            svk.CmdClearColorImage(cmd, img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &c, 1, &r);
        } else {
            VkBufferImageCopy copy = {0, 0, 0, {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, {0, 0, 0}, {s->width, s->height, 1}};
            svk.CmdCopyBufferToImage(cmd, s->staging, img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &copy);
        }
        surf_barrier(cmd, img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
                VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT);
        vk.EndCommandBuffer(cmd);
        VkSubmitInfo submit = {VK_STRUCTURE_TYPE_SUBMIT_INFO, NULL, 0, NULL, NULL, 1, &cmd, 0, NULL};
        // released right after the submit: the runtime's own work on this queue runs after the copy
        ok = vk.QueueSubmit(vk.queue, 1, &submit, s->fence) == VK_SUCCESS;
        s->in_flight = ok;
    } else if (!black) {  // GLES: the app's context is current on its render thread (xrEndFrame)
        GLint prev_tex = 0, prev_align = 4, prev_unpack = 0;
        p_glGetIntegerv(GL_TEXTURE_BINDING_2D, &prev_tex);
        p_glGetIntegerv(GL_UNPACK_ALIGNMENT, &prev_align);
        p_glGetIntegerv(0x88EF /* GL_PIXEL_UNPACK_BUFFER_BINDING */, &prev_unpack);
        if (prev_unpack) { typedef void (*bb)(GLenum, GLuint); static bb bind; if (!bind) bind = (bb)dlsym(RTLD_DEFAULT, "glBindBuffer"); if (bind) bind(0x88EC, 0); }
        p_glBindTexture(GL_TEXTURE_2D, s->gl_images[index]);
        s_glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
        pthread_mutex_lock(&surf_lock);
        s_glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, (GLsizei)s->width, (GLsizei)s->height, GL_RGBA, GL_UNSIGNED_BYTE, s->pixels);
        pthread_mutex_unlock(&surf_lock);
        ok = p_glGetError() == GL_NO_ERROR;
        s_glPixelStorei(GL_UNPACK_ALIGNMENT, prev_align);
        p_glBindTexture(GL_TEXTURE_2D, (GLuint)prev_tex);
        if (prev_unpack) { typedef void (*bb)(GLenum, GLuint); static bb bind; if (!bind) bind = (bb)dlsym(RTLD_DEFAULT, "glBindBuffer"); if (bind) bind(0x88EC, (GLuint)prev_unpack); }
    }
    release(s->handle, &ri);
    return ok;
}

// xrEndFrame: before a layer showing an emulated swapchain is submitted. Returns 0 if it has nothing to show yet.
static int surf_prepare_layer(XrSwapchain handle) {
    pthread_mutex_lock(&surf_lock);
    surf_swapchain *s = surf_find(handle);
    uint64_t seq = s ? s->seq : 0;
    pthread_mutex_unlock(&surf_lock);
    if (!s) return 1;  // not ours
    if (seq != s->uploaded_seq) {
        int r = surf_upload(s, 0);
        if (r > 0) { s->uploaded_seq = seq; s->uploads++; s->has_content = 1; }
        else if (r == 0 && s->upload_failures++ < 5) LOG("surface_emul: upload failed (swapchain %p)", (void *)(uintptr_t)handle);
    }
    return s->has_content;
}

// ---------------------------------------------------------------- the emulated entry points
typedef XrResult (XRAPI_PTR *PFN_xrCreateSwapchainAndroidSurfaceKHR_)(XrSession, const XrSwapchainCreateInfo *,
                                                                      XrSwapchain *, jobject *);

static XRAPI_ATTR XrResult XRAPI_CALL hook_xrCreateSwapchainAndroidSurfaceKHR(XrSession session,
        const XrSwapchainCreateInfo *info, XrSwapchain *swapchain, jobject *surface) {
    PFN_xrCreateSwapchainAndroidSurfaceKHR_ fn =
        (PFN_xrCreateSwapchainAndroidSurfaceKHR_)lookup(active_instance, "xrCreateSwapchainAndroidSurfaceKHR");
    XrResult result = fn ? fn(session, info, swapchain, surface) : XR_ERROR_FUNCTION_UNSUPPORTED;
    if (result != XR_ERROR_FUNCTION_UNSUPPORTED || !info || !swapchain || !surface) return result;
    LOG("surface_emul: runtime has no Android surface swapchains: emulating %ux%u (vm %s, %s session)", info->width,
        info->height, surf_vm ? "ok" : "MISSING", vk.device ? "Vulkan" : "GLES");
    if (!surf_vm || !info->width || !info->height || !surf_load()) return result;
    surf_vulkan = vk.device != VK_NULL_HANDLE;
    if (surf_vulkan && !surf_vk_ready()) { LOG("surface_emul: Vulkan upload path unavailable"); return result; }
    pthread_mutex_lock(&surf_lock);
    surf_swapchain *s = NULL;
    for (int i = 0; i < SURF_MAX && !s; ++i)
        if (!surfs[i].used) s = &surfs[i];
    if (s) { memset(s, 0, sizeof(*s)); s->used = 1; }
    pthread_mutex_unlock(&surf_lock);
    if (!s) { LOG("surface_emul: too many surface swapchains"); return result; }
    // the runtime swapchain the game gets instead (what its layer will show)
    PFN_xrCreateSwapchain create = (PFN_xrCreateSwapchain)lookup(active_instance, "xrCreateSwapchain");
    PFN_xrEnumerateSwapchainImages enumerate =
        (PFN_xrEnumerateSwapchainImages)lookup(active_instance, "xrEnumerateSwapchainImages");
    PFN_xrDestroySwapchain destroy = (PFN_xrDestroySwapchain)lookup(active_instance, "xrDestroySwapchain");
    XrSwapchainCreateInfo ci = {XR_TYPE_SWAPCHAIN_CREATE_INFO, NULL, 0,
                                XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT | XR_SWAPCHAIN_USAGE_TRANSFER_DST_BIT |
                                XR_SWAPCHAIN_USAGE_SAMPLED_BIT,
                                surf_vulkan ? 43 /* VK_FORMAT_R8G8B8A8_SRGB */ : 0x8C43 /* GL_SRGB8_ALPHA8 */,
                                1, info->width, info->height, 1, 1, 1};
    XrSwapchain handle = XR_NULL_HANDLE;
    XrResult cr = create && enumerate ? create(session, &ci, &handle) : XR_ERROR_FUNCTION_UNSUPPORTED;
    if (XR_FAILED(cr)) {
        LOG("surface_emul: xrCreateSwapchain failed %d", cr);
        pthread_mutex_lock(&surf_lock); s->used = 0; pthread_mutex_unlock(&surf_lock);
        return result;
    }
    s->handle = handle;
    s->width = info->width;
    s->height = info->height;
    s->native_pending=-1;
    s->native_current=-1;
    s->native_mode=surface_native && surf_vulkan && surf_native_load();
    s->native_project=s->native_mode;
    s->pixels = calloc((size_t)s->width * s->height, 4);
    uint32_t n = 0;
    if (surf_vulkan) {
        XrSwapchainImageVulkanKHR images[MAX_IMAGES];
        for (int i = 0; i < MAX_IMAGES; ++i) images[i] = (XrSwapchainImageVulkanKHR){XR_TYPE_SWAPCHAIN_IMAGE_VULKAN_KHR, NULL, VK_NULL_HANDLE};
        if (XR_SUCCEEDED(enumerate(handle, MAX_IMAGES, &n, (XrSwapchainImageBaseHeader *)images)))
            for (uint32_t i = 0; i < n && i < MAX_IMAGES; ++i) s->vk_images[i] = images[i].image;
    } else {
        emul_gles_image images[MAX_IMAGES];
        for (int i = 0; i < MAX_IMAGES; ++i) images[i] = (emul_gles_image){XR_TYPE_SWAPCHAIN_IMAGE_OPENGL_ES_KHR, NULL, 0};
        if (XR_SUCCEEDED(enumerate(handle, MAX_IMAGES, &n, (XrSwapchainImageBaseHeader *)images)))
            for (uint32_t i = 0; i < n && i < MAX_IMAGES; ++i) s->gl_images[i] = images[i].image;
    }
    s->image_count = n < MAX_IMAGES ? n : MAX_IMAGES;
    int setup_ok = s->pixels && s->image_count && (!surf_vulkan || surf_staging(s));
    if (setup_ok) {
        pthread_mutex_lock(&surf_lock);
        setup_ok = pthread_create(&s->thread, NULL, surf_worker, s) == 0;
        struct timespec until;
        clock_gettime(CLOCK_REALTIME, &until);
        until.tv_sec += 3;
        while (setup_ok && !s->ready && !s->failed && pthread_cond_timedwait(&surf_cond, &surf_lock, &until) == 0) {}
        setup_ok = setup_ok && s->ready;
        pthread_mutex_unlock(&surf_lock);
    }
    if (!setup_ok) {
        LOG("surface_emul: setup failed (images %u, pixels %d): the panel stays empty", s->image_count, s->pixels != NULL);
        if (s->thread) { s->stop = 1; pthread_join(s->thread, NULL); }
        surf_native_destroy(s);
        if (destroy) destroy(handle);
        free(s->pixels);
        pthread_mutex_lock(&surf_lock); s->used = 0; pthread_mutex_unlock(&surf_lock);
        return result;
    }
    // something valid to show from the first frame on (Vulkan: a black image; GLES: the layer waits for a frame)
    if (surf_vulkan && surf_upload(s, 1) > 0) s->has_content = 1;
    remember_swapchain(handle);
    *swapchain = handle;
    *surface = s->surface;
    LOG("surface_emul: emulated Android surface swapchain %p (%u images)", (void *)(uintptr_t)handle, s->image_count);
    return XR_SUCCESS;
}

// xrDestroySwapchain: an emulated swapchain's worker and surface go with it
static void surf_projection_destroy(surf_swapchain *s);
static void surf_on_destroy(XrSwapchain handle) {
    pthread_mutex_lock(&surf_lock);
    surf_swapchain *s = surf_find(handle);
    pthread_mutex_unlock(&surf_lock);
    if (!s) return;
    s->stop = 1;
    surf_projection_destroy(s);
    if (s->thread) pthread_join(s->thread, NULL);
    surf_native_destroy(s);
    if (surf_vm && s->surface) {
        JNIEnv *env = NULL;
        int attached = 0;
        if ((*surf_vm)->GetEnv(surf_vm, (void **)&env, JNI_VERSION_1_6) != JNI_OK) {
            attached = (*surf_vm)->AttachCurrentThread(surf_vm, &env, NULL) == JNI_OK;
        }
        if (env) (*env)->DeleteGlobalRef(env, s->surface);
        if (attached) (*surf_vm)->DetachCurrentThread(surf_vm);
    }
    pthread_mutex_lock(&surf_lock);
    free(s->pixels);
    s->pixels = NULL;
    s->used = 0;
    pthread_mutex_unlock(&surf_lock);
    LOG("surface_emul: destroyed %p", (void *)(uintptr_t)handle);
}

#include "surface_projection.c"
