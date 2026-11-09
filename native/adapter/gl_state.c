// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c next to clear_gl_errors: gl_state_guard (GLES sessions, default on; GitHub #441/#502).
//
// SteamVR's GL interop runs GL in the app's context inside the runtime's calls and leaves its own objects bound:
// inside JKXR's first xrEndFrame it created six 16x16 GL_SRGB8_ALPHA8 textures ("Created remote GL texture ... 16 x
// 16, format 35907" in xrclient's log) and left one bound. The game's GL_Bind cache and gl4es's binding cache still
// believed the cinematic texture was bound, so every later glTexSubImage2D(512 wide) hit the runtime's texture
// (GL_INVALID_VALUE, 433 times, one per cinematic frame) and the cinematics stayed black. Around every runtime call
// that may run GL in the app's context (xrEndFrame, xrBeginFrame, xrCreateSwapchain, xrAcquire/Wait/ReleaseSwapchain-
// Image) FrameBridge saves the bindings an app's state cache tracks and restores the ones that changed: the active
// texture unit, the 2D / 2D-array / external texture on that unit and on unit 0, draw and read framebuffers, the
// program, the vertex array + array buffer, the pixel-unpack buffer and the unpack alignment. About 12 glGet calls
// before and after each guarded call, only with a current context; restores only for values that changed.
// GL comes from the system's libGLESv3/libGLESv2 (RTLD_NOLOAD), never RTLD_DEFAULT: in gl4es games that resolves to
// gl4es's own entry points (its caches, not the driver's state). Uses gl_state_guard, gles_session and LOG.

#define GLS_TEXTURE_2D 0x0DE1
#define GLS_TEXTURE_2D_ARRAY 0x8C1A
#define GLS_TEXTURE_EXTERNAL_OES 0x8D65
#define GLS_TEXTURE0 0x84C0

typedef void (*gls_get_t)(unsigned pname, int *out);
typedef void (*gls_uint_t)(unsigned);
typedef void (*gls_bind_t)(unsigned target, unsigned name);
typedef void (*gls_store_t)(unsigned pname, int value);
typedef const unsigned char *(*gls_string_t)(unsigned name);

static struct {
    int ready, ok, external;
    gls_get_t get;
    gls_uint_t active_texture, use_program, bind_vertex_array;
    gls_bind_t bind_texture, bind_framebuffer, bind_buffer;
    gls_store_t pixel_store;
    void *(*current_context)(void);
} gls;

typedef struct {
    void *context;
    int active, units[2];  // texture units saved: the active one and unit 0 (the same one twice when it is unit 0)
    int tex[2][3];         // per unit: 2D, 2D array, external OES (-1: not queried)
    int draw_fb, read_fb, program, vao, array_buffer, unpack_buffer, unpack_alignment;
} GlState;

static void gls_resolve(void) {
    gls.ready = 1;
    void *gl = dlopen("libGLESv3.so", RTLD_NOW | RTLD_NOLOAD);
    if (!gl) gl = dlopen("libGLESv2.so", RTLD_NOW | RTLD_NOLOAD);
    void *egl = dlopen("libEGL.so", RTLD_NOW | RTLD_NOLOAD);
    if (!gl || !egl) { LOG("gl_state_guard: off (libGLESv2/v3 or libEGL not loaded)"); return; }
#define GLS_SYM(field, type, name) gls.field = (type)dlsym(gl, name)
    GLS_SYM(get, gls_get_t, "glGetIntegerv");
    GLS_SYM(active_texture, gls_uint_t, "glActiveTexture");
    GLS_SYM(use_program, gls_uint_t, "glUseProgram");
    GLS_SYM(bind_vertex_array, gls_uint_t, "glBindVertexArray");
    GLS_SYM(bind_texture, gls_bind_t, "glBindTexture");
    GLS_SYM(bind_framebuffer, gls_bind_t, "glBindFramebuffer");
    GLS_SYM(bind_buffer, gls_bind_t, "glBindBuffer");
    GLS_SYM(pixel_store, gls_store_t, "glPixelStorei");
#undef GLS_SYM
    gls.current_context = (void *(*)(void))dlsym(egl, "eglGetCurrentContext");
    gls.ok = gls.get && gls.active_texture && gls.use_program && gls.bind_vertex_array && gls.bind_texture &&
             gls.bind_framebuffer && gls.bind_buffer && gls.pixel_store && gls.current_context;
    LOG("gl_state_guard: %s", gls.ok ? "on (app GL state kept across the runtime's calls)" : "off (GL entry points missing)");
}

static int gls_value(unsigned pname) {
    int v = 0;
    gls.get(pname, &v);
    return v;
}

// Saves the state; 0 when there is nothing to guard (not a GLES session, no current context, setting off).
static int gl_state_save(GlState *s) {
    if (!gl_state_guard || !gles_session) return 0;
    if (!gls.ready) {
        gls_resolve();
        if (gls.ok && gls.current_context()) {  // GL_TEXTURE_BINDING_EXTERNAL_OES is an error without the extension
            gls_string_t string = NULL;
            void *gl = dlopen("libGLESv3.so", RTLD_NOW | RTLD_NOLOAD);
            if (!gl) gl = dlopen("libGLESv2.so", RTLD_NOW | RTLD_NOLOAD);
            if (gl) string = (gls_string_t)dlsym(gl, "glGetString");
            const unsigned char *ext = string ? string(0x1F03) : NULL;  // GL_EXTENSIONS
            gls.external = ext && strstr((const char *)ext, "GL_OES_EGL_image_external") != NULL;
        }
    }
    if (!gls.ok || !(s->context = gls.current_context())) return 0;
    s->active = gls_value(0x84E0);  // GL_ACTIVE_TEXTURE
    s->units[0] = s->active;
    s->units[1] = GLS_TEXTURE0;
    for (int u = 0; u < 2; ++u) {
        if (u == 1 && s->units[1] == s->units[0]) { memcpy(s->tex[1], s->tex[0], sizeof s->tex[0]); break; }
        if (u == 1) gls.active_texture(GLS_TEXTURE0);
        s->tex[u][0] = gls_value(0x8069);                          // GL_TEXTURE_BINDING_2D
        s->tex[u][1] = gls_value(0x8C1D);                          // GL_TEXTURE_BINDING_2D_ARRAY
        s->tex[u][2] = gls.external ? gls_value(0x8D67) : -1;      // GL_TEXTURE_BINDING_EXTERNAL_OES
        if (u == 1) gls.active_texture((unsigned)s->active);
    }
    s->draw_fb = gls_value(0x8CA6);          // GL_DRAW_FRAMEBUFFER_BINDING
    s->read_fb = gls_value(0x8CAA);          // GL_READ_FRAMEBUFFER_BINDING
    s->program = gls_value(0x8B8D);          // GL_CURRENT_PROGRAM
    s->vao = gls_value(0x85B5);              // GL_VERTEX_ARRAY_BINDING
    s->array_buffer = gls_value(0x8894);     // GL_ARRAY_BUFFER_BINDING
    s->unpack_buffer = gls_value(0x88EF);    // GL_PIXEL_UNPACK_BUFFER_BINDING
    s->unpack_alignment = gls_value(0x0CF5); // GL_UNPACK_ALIGNMENT
    return 1;
}

static void gl_state_restore(const GlState *s, const char *where) {
    if (gls.current_context() != s->context) return;  // the runtime left another context current: not ours to fix
    static const unsigned targets[3] = {GLS_TEXTURE_2D, GLS_TEXTURE_2D_ARRAY, GLS_TEXTURE_EXTERNAL_OES};
    static const unsigned queries[3] = {0x8069, 0x8C1D, 0x8D67};
    int changed = 0;
    for (int u = 1; u >= 0; --u) {  // unit 0 first, the app's active unit last (it stays active)
        if (u == 1 && s->units[1] == s->units[0]) continue;
        gls.active_texture((unsigned)s->units[u]);
        for (int t = 0; t < 3; ++t) {
            if (s->tex[u][t] < 0) continue;
            if (gls_value(queries[t]) != s->tex[u][t]) { gls.bind_texture(targets[t], (unsigned)s->tex[u][t]); ++changed; }
        }
    }
    if (gls_value(0x84E0) != s->active) gls.active_texture((unsigned)s->active);
    if (gls_value(0x8CA6) != s->draw_fb) { gls.bind_framebuffer(0x8CA9, (unsigned)s->draw_fb); ++changed; }  // DRAW
    if (gls_value(0x8CAA) != s->read_fb) { gls.bind_framebuffer(0x8CA8, (unsigned)s->read_fb); ++changed; }  // READ
    if (gls_value(0x8B8D) != s->program) { gls.use_program((unsigned)s->program); ++changed; }
    if (gls_value(0x85B5) != s->vao) { gls.bind_vertex_array((unsigned)s->vao); ++changed; }
    if (gls_value(0x8894) != s->array_buffer) { gls.bind_buffer(0x8892, (unsigned)s->array_buffer); ++changed; }
    if (gls_value(0x88EF) != s->unpack_buffer) { gls.bind_buffer(0x88EC, (unsigned)s->unpack_buffer); ++changed; }
    if (gls_value(0x0CF5) != s->unpack_alignment) { gls.pixel_store(0x0CF5, s->unpack_alignment); ++changed; }
    static int logged;
    if (changed && logged < 5) {
        ++logged;
        LOG("gl_state_guard: %s changed %d of the app's GL bindings: restored", where, changed);
    }
}

// expr = a runtime call that may run GL in the app's context
#define GL_GUARDED(where, expr) __extension__({ \
    GlState gls_saved_; \
    int gls_on_ = gl_state_save(&gls_saved_); \
    XrResult gls_r_ = (expr); \
    if (gls_on_) gl_state_restore(&gls_saved_, where); \
    gls_r_; })
