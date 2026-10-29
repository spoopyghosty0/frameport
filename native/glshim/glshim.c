// SPDX-License-Identifier: GPL-3.0-only
// GL extension filter for GLES VR games on Steam Frame: hides multiview / multisampled-render-to-texture
// extensions so engines take their plain per-eye render path. Loaded first via DT_NEEDED on the game lib.
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <GLES3/gl3.h>
#include <android/log.h>
#include <dlfcn.h>
#include <stdio.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>

#define LOG(...) __android_log_print(ANDROID_LOG_INFO, "GLShim", __VA_ARGS__)
#define EXPORT __attribute__((visibility("default")))
#ifndef GLSHIM_GLES_LIB  // (the host test points these at a stand-in GL library)
#define GLSHIM_GLES_LIB "libGLESv3.so"
#define GLSHIM_EGL_LIB "libEGL.so"
#endif
#include "glfix.h"

static const char *const hidden_multiview[] = {
    "GL_OVR_multiview", "GL_OVR_multiview2", "GL_OVR_multiview_multisampled_render_to_texture",
};
// multisampled render-to-texture: Unity renders a runtime MSAA eye buffer through it, which crashes Zink (SIGSEGV in
// libgallium_dri.so, e.g. The Room VR); hidden, Unity uses an ordinary MSAA renderbuffer + resolve
static const char *const hidden_msrtt[] = {
    "GL_EXT_multisampled_render_to_texture", "GL_EXT_multisampled_render_to_texture2",
    "GL_OVR_multiview_multisampled_render_to_texture",
};

static void *gles;
static const GLubyte *(*real_glGetString)(GLenum);
static const GLubyte *(*real_glGetStringi)(GLenum, GLuint);
static void (*real_glGetIntegerv)(GLenum, GLint *);
static __eglMustCastToProperFunctionPointerType (*real_eglGetProcAddress)(const char *);
static pthread_once_t once = PTHREAD_ONCE_INIT;

static int hide_multiview = -1;  // -1: not set (Unity and Unreal keep multiview, everything else hides it)
static int hide_msrtt = 1;
// Unreal (4.25) only turns on its mobile multiview when GL_OVR_multiview, GL_OVR_multiview2 AND
// GL_OVR_multiview_multisampled_render_to_texture are all there (FOpenGLES::ProcessExtensions). Hiding the last one
// with the MSRTT extensions switched Unreal's multiview off, so for Unreal it stays visible and its function draws
// single-sampled (glFramebufferTextureMultiviewOVR), like the EXT stand-ins below.
static int unreal;
// Per-game fixes (off unless a recipe sets them; see glfix.h): integer vertex attributes fed as floats are rebound as
// integers before each draw (gl_int_attribs), and vertex shaders containing every marker get highp calculations
// (gl_highp_markers).
static int int_attribs;
static char highp_markers[512];
static void forget_program(GLuint prog);
// gl_gamma_space=1: the Frame's runtime takes only sRGB eye-image formats, so FrameBridge (swapchain_fix) hands a game
// GL_SRGB8_ALPHA8 images where it asked for GL_RGBA8. A game that renders in gamma colour space (Unity's Gamma setting,
// e.g. PLAY'A VR) writes already gamma-encoded pixels and never calls glDisable(GL_FRAMEBUFFER_SRGB_EXT), since it
// believes the target is linear: the framebuffer encodes them a second time and the picture is washed out, grey instead
// of black. The shim then keeps GL_FRAMEBUFFER_SRGB_EXT (EXT_sRGB_write_control) off, so the pixels land unconverted in
// the sRGB image and the compositor decodes them once.
static int gamma_space;
static void (*real_glBindFramebuffer)(GLenum, GLuint);
static void (*real_glEnable)(GLenum);
static void (*real_glDisable)(GLenum);
static EGLBoolean (*real_eglMakeCurrent)(EGLDisplay, EGLSurface, EGLSurface, EGLContext);

static void read_conf_file(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[1024];
    while (fgets(line, sizeof(line), f)) {
        if (!strncmp(line, "gl_hide_multiview=", 18)) hide_multiview = atoi(line + 18);
        if (!strncmp(line, "gl_hide_msrtt=", 14)) hide_msrtt = atoi(line + 14);
        if (!strncmp(line, "gl_int_attribs=", 15)) int_attribs = atoi(line + 15);
        if (!strncmp(line, "gl_gamma_space=", 15)) gamma_space = atoi(line + 15);
        if (!strncmp(line, "gl_highp_markers=", 17)) {
            snprintf(highp_markers, sizeof(highp_markers), "%s", line + 17);
            highp_markers[strcspn(highp_markers, "\r\n")] = 0;
        }
    }
    fclose(f);
}

// The same settings sources as FrameBridge, later ones winning: the build's libframe_settings.so (next to this
// library), the game's framebridge.conf, then FRAMEBRIDGE_CONFIG (Lepton's per-game settings.conf).
static void read_conf(void) {
    Dl_info info;
    char path[600];
    if (dladdr((void *)read_conf, &info) && info.dli_fname) {
        const char *slash = strrchr(info.dli_fname, '/');
        if (slash && slash - info.dli_fname < 500) {
            snprintf(path, sizeof(path), "%.*s/libframe_settings.so", (int)(slash - info.dli_fname), info.dli_fname);
            read_conf_file(path);
        }
    }
    char pkg[256] = {0};
    FILE *f = fopen("/proc/self/cmdline", "r");
    if (f) {
        size_t n = fread(pkg, 1, sizeof(pkg) - 1, f);
        fclose(f);
        pkg[n] = 0;
    }
    char *colon = strchr(pkg, ':');
    if (colon) *colon = 0;
    if (*pkg && !strchr(pkg, '/')) {
        snprintf(path, sizeof(path), "/sdcard/Android/data/%s/files/framebridge.conf", pkg);
        read_conf_file(path);
    }
    const char *env = getenv("FRAMEBRIDGE_CONFIG");
    if (env && *env) read_conf_file(env);
}

// Logging wrappers for the framebuffer calls engines use for eye buffers.
typedef void (*PFN_FTMV)(GLenum, GLenum, GLuint, GLint, GLint, GLsizei);
typedef void (*PFN_FTMSMV)(GLenum, GLenum, GLuint, GLint, GLsizei, GLint, GLsizei);
typedef void (*PFN_FT2DMS)(GLenum, GLenum, GLenum, GLuint, GLint, GLsizei);
typedef GLenum (*PFN_CFS)(GLenum);
static PFN_FTMV real_ftmv;
static PFN_FTMSMV real_ftmsmv;
static PFN_FT2DMS real_ft2dms;
static PFN_CFS real_cfs;
static int fb_logs;
#define FBLOG(...) do { if (fb_logs < 60) { ++fb_logs; LOG(__VA_ARGS__); } } while (0)
static void w_ftmv(GLenum t, GLenum a, GLuint tex, GLint lvl, GLint base, GLsizei views) {
    FBLOG("glFramebufferTextureMultiviewOVR(att=0x%x tex=%u lvl=%d base=%d views=%d)", a, tex, lvl, base, views);
    real_ftmv(t, a, tex, lvl, base, views);
}
static void w_ftmsmv(GLenum t, GLenum a, GLuint tex, GLint lvl, GLsizei samples, GLint base, GLsizei views) {
    FBLOG("glFramebufferTextureMultisampleMultiviewOVR(att=0x%x tex=%u samples=%d base=%d views=%d)", a, tex, samples, base, views);
    real_ftmsmv(t, a, tex, lvl, samples, base, views);
}
static void w_ft2dms(GLenum t, GLenum a, GLenum tt, GLuint tex, GLint lvl, GLsizei samples) {
    FBLOG("glFramebufferTexture2DMultisampleEXT(att=0x%x target=0x%x tex=%u samples=%d)", a, tt, tex, samples);
    real_ft2dms(t, a, tt, tex, lvl, samples);
}
static GLenum w_cfs(GLenum t) {
    GLenum st = real_cfs(t);
    if (st != GL_FRAMEBUFFER_COMPLETE) FBLOG("glCheckFramebufferStatus -> 0x%x", st);
    return st;
}

// ---- render tracing: shader failures, eye-buffer attachments, draws/clears per framebuffer
#include <time.h>
typedef void (*PFN_V_U)(GLuint);
typedef void (*PFN_GSIV)(GLuint, GLenum, GLint *);
typedef void (*PFN_GLOG)(GLuint, GLsizei, GLsizei *, char *);
typedef void (*PFN_BFB)(GLenum, GLuint);
typedef void (*PFN_FT2D)(GLenum, GLenum, GLenum, GLuint, GLint);
typedef void (*PFN_FTL)(GLenum, GLenum, GLuint, GLint, GLint);
typedef void (*PFN_DA)(GLenum, GLint, GLsizei);
typedef void (*PFN_DE)(GLenum, GLsizei, GLenum, const void *);
typedef void (*PFN_DAI)(GLenum, GLint, GLsizei, GLsizei);
typedef void (*PFN_DEI)(GLenum, GLsizei, GLenum, const void *, GLsizei);
typedef void (*PFN_DRE)(GLenum, GLuint, GLuint, GLsizei, GLenum, const void *);
typedef void (*PFN_CLR)(GLbitfield);
typedef void (*PFN_BLIT)(GLint, GLint, GLint, GLint, GLint, GLint, GLint, GLint, GLbitfield, GLenum);
typedef GLenum (*PFN_GE)(void);
typedef void (*PFN_GIV)(GLenum, GLint *);
static PFN_V_U r_compile, r_link;
static PFN_GSIV r_getShaderiv, r_getProgramiv;
static PFN_GLOG r_shaderLog, r_programLog, r_shaderSource;
static PFN_BFB r_bindFb;
static PFN_FT2D r_ft2d;
static PFN_FTL r_ftl;
static PFN_DA r_da; static PFN_DE r_de; static PFN_DAI r_dai; static PFN_DEI r_dei; static PFN_DRE r_dre;
static PFN_CLR r_clear;
static PFN_BLIT r_blit;
static PFN_GE r_getError;
static PFN_GIV r_getIv;
static int shader_logs, attach_logs, blit_logs;
static __thread GLuint cur_draw_fb;
#define NFB 64
static struct { GLuint fb; unsigned draws, clears, binds; } fbstats[NFB];
static struct timespec last_dump;
static pthread_mutex_t stats_lock = PTHREAD_MUTEX_INITIALIZER;

static void *slot(GLuint fb) {
    for (int i = 0; i < NFB; ++i)
        if (fbstats[i].fb == fb && (fb || fbstats[i].binds || fbstats[i].draws || fbstats[i].clears)) return &fbstats[i];
    for (int i = 0; i < NFB; ++i)
        if (!fbstats[i].fb && !fbstats[i].binds && !fbstats[i].draws && !fbstats[i].clears) { fbstats[i].fb = fb; return &fbstats[i]; }
    return NULL;
}
static void maybe_dump(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    if (now.tv_sec - last_dump.tv_sec < 5) return;
    last_dump = now;
    char buf[1024]; int n = 0;
    for (int i = 0; i < NFB && n < (int)sizeof(buf) - 48; ++i)
        if (fbstats[i].binds || fbstats[i].draws || fbstats[i].clears)
            n += snprintf(buf + n, sizeof(buf) - n, " fb%u:b%u/d%u/c%u", fbstats[i].fb, fbstats[i].binds, fbstats[i].draws, fbstats[i].clears);
    GLenum err = r_getError ? r_getError() : 0;
    LOG("5s stats (binds/draws/clears):%s  glGetError=0x%x", n ? buf : " none", err);
    memset(fbstats, 0, sizeof(fbstats));
}
static void check_draw(const char *what, GLenum mode, GLsizei count) {
    static int logs;
    GLenum err = r_getError ? r_getError() : 0;
    if (err == GL_NO_ERROR || logs >= 30) return;
    ++logs;
    GLint prog = 0;
    if (r_getIv) r_getIv(GL_CURRENT_PROGRAM, &prog);
    LOG("%s(mode=0x%x count=%d) error 0x%x fb=%u program=%d", what, mode, count, err, cur_draw_fb, prog);
}
static void count_draw(void) {
    pthread_mutex_lock(&stats_lock);
    struct { GLuint fb; unsigned draws, clears, binds; } *e = slot(cur_draw_fb);
    if (e) e->draws++;
    maybe_dump();
    pthread_mutex_unlock(&stats_lock);
}
static void w_compile(GLuint sh) {
    r_compile(sh);
    GLint ok = 1;
    r_getShaderiv(sh, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        static int failures;
        ++failures;
        if (shader_logs >= 40) { if (failures % 50 == 0) LOG("%d shader compile failures so far", failures); return; }
        ++shader_logs;
        char log[1024] = {0};
        r_shaderLog(sh, sizeof(log) - 1, NULL, log);
        LOG("SHADER COMPILE FAILED %u: %.600s", sh, log);
        GLint len = 0;
        r_getShaderiv(sh, GL_SHADER_SOURCE_LENGTH, &len);
        char *src = len > 0 ? malloc((size_t)len + 1) : NULL;
        if (!src || !r_shaderSource) { free(src); return; }
        r_shaderSource(sh, len, NULL, src);
        // Mesa reports "0:LINE(COL): error"; print each referenced line once.
        int printed[8], np = 0;
        for (const char *p = log; (p = strstr(p, "0:")) && np < 8; p += 2) {
            int line = atoi(p + 2), seen = 0;
            for (int k = 0; k < np; ++k) seen |= printed[k] == line;
            if (line <= 0 || seen) continue;
            printed[np++] = line;
            const char *q = src;
            for (int l = 1; l < line && q; ++l) { q = strchr(q, '\n'); if (q) ++q; }
            if (!q) continue;
            const char *e = strchr(q, '\n');
            LOG("  line %d: %.*s", line, (int)(e ? e - q : (long)strlen(q)), q);
        }
        free(src);
    }
}
static void w_link(GLuint prog) {
    r_link(prog);
    forget_program(prog);
    GLint ok = 1;
    r_getProgramiv(prog, GL_LINK_STATUS, &ok);
    if (!ok && shader_logs < 40) {
        ++shader_logs;
        char log[768] = {0};
        r_programLog(prog, sizeof(log) - 1, NULL, log);
        LOG("PROGRAM LINK FAILED %u: %s", prog, log);
    }
}
static void w_bindFb(GLenum t, GLuint fb) {
    r_bindFb(t, fb);
    if (t == GL_FRAMEBUFFER || t == GL_DRAW_FRAMEBUFFER) {
        cur_draw_fb = fb;
        pthread_mutex_lock(&stats_lock);
        struct { GLuint fb; unsigned draws, clears, binds; } *e = slot(fb);
        if (e) e->binds++;
        pthread_mutex_unlock(&stats_lock);
    }
}
static void w_ft2d(GLenum t, GLenum a, GLenum tt, GLuint tex, GLint lvl) {
    r_ft2d(t, a, tt, tex, lvl);
    if (attach_logs < 80) { ++attach_logs; LOG("glFramebufferTexture2D(fb=%u att=0x%x target=0x%x tex=%u lvl=%d) err=0x%x", cur_draw_fb, a, tt, tex, lvl, r_getError()); }
}
static void w_ftl(GLenum t, GLenum a, GLuint tex, GLint lvl, GLint layer) {
    r_ftl(t, a, tex, lvl, layer);
    if (attach_logs < 80) { ++attach_logs; LOG("glFramebufferTextureLayer(fb=%u att=0x%x tex=%u lvl=%d layer=%d) err=0x%x", cur_draw_fb, a, tex, lvl, layer, r_getError()); }
}
static void w_da(GLenum m, GLint f, GLsizei c) { if (r_getError) r_getError(); r_da(m, f, c); check_draw("glDrawArrays", m, c); count_draw(); }
static void w_de(GLenum m, GLsizei c, GLenum t, const void *i) { if (r_getError) r_getError(); r_de(m, c, t, i); check_draw("glDrawElements", m, c); count_draw(); }
static void w_dai(GLenum m, GLint f, GLsizei c, GLsizei n) { if (r_getError) r_getError(); r_dai(m, f, c, n); check_draw("glDrawArraysInstanced", m, c); count_draw(); }
static void w_dei(GLenum m, GLsizei c, GLenum t, const void *i, GLsizei n) { if (r_getError) r_getError(); r_dei(m, c, t, i, n); check_draw("glDrawElementsInstanced", m, c); count_draw(); }
static void w_dre(GLenum m, GLuint s, GLuint e2, GLsizei c, GLenum t, const void *i) { if (r_getError) r_getError(); r_dre(m, s, e2, c, t, i); check_draw("glDrawRangeElements", m, c); count_draw(); }
static void w_clear(GLbitfield mask) {
    r_clear(mask);
    pthread_mutex_lock(&stats_lock);
    struct { GLuint fb; unsigned draws, clears, binds; } *e = slot(cur_draw_fb);
    if (e) e->clears++;
    maybe_dump();
    pthread_mutex_unlock(&stats_lock);
}
static void w_blit(GLint a, GLint b, GLint c, GLint d, GLint e, GLint f, GLint g, GLint h, GLbitfield m, GLenum fl) {
    r_blit(a, b, c, d, e, f, g, h, m, fl);
    if (blit_logs < 30) {
        ++blit_logs;
        GLint rfb = 0;
        r_getIv(GL_READ_FRAMEBUFFER_BINDING, &rfb);
        LOG("glBlitFramebuffer read=%d draw=%u src %d,%d-%d,%d dst %d,%d-%d,%d mask=0x%x err=0x%x", rfb, cur_draw_fb, a, b, c, d, e, f, g, h, m, r_getError());
    }
}

// ---- per-game fixes: the real functions (from libGLESv3 / libEGL, whatever the game imports or looks up)
static struct {
    void (*ShaderSource)(GLuint, GLsizei, const GLchar *const *, const GLint *);
    void (*GetShaderiv)(GLuint, GLenum, GLint *);
    void (*LinkProgram)(GLuint);
    void (*ProgramBinary)(GLuint, GLenum, const void *, GLsizei);
    void (*DeleteProgram)(GLuint);
    void (*GetProgramiv)(GLuint, GLenum, GLint *);
    void (*GetActiveAttrib)(GLuint, GLuint, GLsizei, GLsizei *, GLint *, GLenum *, GLchar *);
    GLint (*GetAttribLocation)(GLuint, const GLchar *);
    void (*GetVertexAttribiv)(GLuint, GLenum, GLint *);
    void (*GetVertexAttribPointerv)(GLuint, GLenum, void **);
    void (*VertexAttribPointer)(GLuint, GLint, GLenum, GLboolean, GLsizei, const void *);
    void (*VertexAttribIPointer)(GLuint, GLint, GLenum, GLsizei, const void *);
    void (*VertexAttribI4i)(GLuint, GLint, GLint, GLint, GLint);
    void (*VertexAttribI4ui)(GLuint, GLuint, GLuint, GLuint, GLuint);
    void (*BindBuffer)(GLenum, GLuint);
    void (*DrawArrays)(GLenum, GLint, GLsizei);
    void (*DrawElements)(GLenum, GLsizei, GLenum, const void *);
    void (*DrawArraysInstanced)(GLenum, GLint, GLsizei, GLsizei);
    void (*DrawElementsInstanced)(GLenum, GLsizei, GLenum, const void *, GLsizei);
    void (*DrawRangeElements)(GLenum, GLuint, GLuint, GLsizei, GLenum, const void *);
    EGLContext (*GetCurrentContext)(void);
} d;

// gl_highp_markers: a copy of a matching vertex shader's source with highp calculations, else NULL
static int highp_logs;
static char *precision_fix(GLuint sh, const char *src) {
    if (!highp_markers[0] || !glfix_markers_match(src, highp_markers)) return NULL;
    GLint type = 0;
    if (d.GetShaderiv) d.GetShaderiv(sh, GL_SHADER_TYPE, &type);
    if (type != GL_VERTEX_SHADER) return NULL;
    int n = 0;
    char *out = glfix_promote_highp(src, &n);
    if (out && highp_logs < 16) { ++highp_logs; LOG("highp: vertex shader %u: %d mediump -> highp", sh, n); }
    return out;
}

static char *concat_sources(GLsizei count, const GLchar *const *strs, const GLint *lens) {
    size_t total = 0;
    for (GLsizei i = 0; i < count; ++i) total += lens && lens[i] >= 0 ? (size_t)lens[i] : strlen(strs[i]);
    char *src = malloc(total + 1);
    if (!src) return NULL;
    size_t off = 0;
    for (GLsizei i = 0; i < count; ++i) {
        size_t n = lens && lens[i] >= 0 ? (size_t)lens[i] : strlen(strs[i]);
        memcpy(src + off, strs[i], n);
        off += n;
    }
    src[off] = 0;
    return src;
}

// gl_int_attribs: the shader declares an integer input (int/uint/ivecN/uvecN), the game set its array with
// glVertexAttribPointer (floats; GL_VERTEX_ATTRIB_ARRAY_INTEGER 0). Qualcomm's Quest driver read the raw integers
// anyway; Mesa (Zink) converts them to floats, so e.g. BlazeRush's bone indices were garbage and its car bodies and
// controller models invisible (Klownicle's guide, GitHub #57). Before each draw, such an array is set again with
// glVertexAttribIPointer (same buffer, size, component type, stride and offset; the game's GL_ARRAY_BUFFER binding is
// restored), and set back to floats (with the game's normalization) when a program with a float input at that
// location draws from the same vertex array state. A disabled integer input gets the integer default (0, 0, 0, 1).
// The active-attribute reflection is cached per EGL context and program, and dropped when a program is linked,
// loaded from a binary or deleted (program names are reused).
#define MAXLOC 32
#define NPROG 1024
#define PROBE 16
#define NCONV 256
#define NCTX 16
struct proginfo { EGLContext ctx; GLuint prog; uint32_t int_mask, unsigned_mask, float_mask; };
struct convrec { EGLContext ctx; GLuint vao, buffer; int loc, used; GLint size, stride; GLenum type; const void *ptr;
                 GLboolean normalized; };
struct ctxstate { EGLContext ctx; uint32_t converted; };
static struct proginfo progs[NPROG];
static struct convrec convs[NCONV];
static struct ctxstate ctxs[NCTX];
static unsigned conv_next, ctx_next;
static pthread_mutex_t attrib_lock = PTHREAD_MUTEX_INITIALIZER;
static int attrib_logs, reflect_logs, skip_logs;

static unsigned prog_slot(EGLContext ctx, GLuint prog) {
    uint64_t h = (uint64_t)(uintptr_t)ctx * 0x9E3779B97F4A7C15ull ^ (uint64_t)prog * 0x85EBCA6Bu;
    return (unsigned)((h ^ (h >> 29)) % NPROG);
}

static void forget_program(GLuint prog) {  // every context's entry: shared contexts see the same program name
    if (!prog || !int_attribs) return;
    pthread_mutex_lock(&attrib_lock);
    for (unsigned i = 0; i < NPROG; ++i)
        if (progs[i].prog == prog) progs[i] = (struct proginfo){0};
    pthread_mutex_unlock(&attrib_lock);
}

static void reflect(struct proginfo *pi, GLuint prog) {
    GLint linked = 0, n = 0;
    d.GetProgramiv(prog, GL_LINK_STATUS, &linked);
    if (!linked) return;
    d.GetProgramiv(prog, GL_ACTIVE_ATTRIBUTES, &n);
    for (GLint i = 0; i < n; ++i) {
        char name[128] = {0};
        GLsizei len = 0;
        GLint size = 0;
        GLenum type = 0;
        d.GetActiveAttrib(prog, (GLuint)i, sizeof(name), &len, &size, &type, name);
        GLint loc = name[0] ? d.GetAttribLocation(prog, name) : -1;
        if (loc < 0) continue;  // built-ins (gl_VertexID ...)
        int kind = glfix_int_attrib_kind(type), cols = glfix_attrib_columns(type);
        for (int k = 0; k < (size > 0 ? size : 1) * cols && loc + k < MAXLOC; ++k) {
            uint32_t bit = 1u << (loc + k);
            if (kind) pi->int_mask |= bit; else pi->float_mask |= bit;
            if (kind == 2) pi->unsigned_mask |= bit;
        }
        if (kind && reflect_logs < 16) {
            ++reflect_logs;
            LOG("int attribs: program %u has integer input %s (%s) at location %d", prog, name,
                kind == 2 ? "unsigned" : "signed", loc);
        }
    }
}

static struct proginfo *program_info(EGLContext ctx, GLuint prog) {
    unsigned h = prog_slot(ctx, prog), free_slot = NPROG;
    for (unsigned k = 0; k < PROBE; ++k) {
        struct proginfo *e = &progs[(h + k) % NPROG];
        if (e->prog == prog && e->ctx == ctx) return e;
        if (!e->prog && free_slot == NPROG) free_slot = (h + k) % NPROG;
    }
    struct proginfo *e = &progs[free_slot < NPROG ? free_slot : h];
    *e = (struct proginfo){.ctx = ctx, .prog = prog};
    reflect(e, prog);
    return e;
}

static struct ctxstate *ctx_state(EGLContext ctx) {
    for (unsigned i = 0; i < NCTX; ++i)
        if (ctxs[i].ctx == ctx) return &ctxs[i];
    struct ctxstate *c = &ctxs[ctx_next++ % NCTX];
    *c = (struct ctxstate){.ctx = ctx};
    return c;
}

static struct convrec *find_conv(EGLContext ctx, GLuint vao, int loc) {
    for (unsigned i = 0; i < NCONV; ++i)
        if (convs[i].used && convs[i].ctx == ctx && convs[i].vao == vao && convs[i].loc == loc) return &convs[i];
    return NULL;
}

static void rebind(GLuint loc, GLuint buffer, GLint size, GLenum type, GLboolean normalized, GLsizei stride,
                   const void *ptr, int integer) {
    GLint previous = 0;
    real_glGetIntegerv(GL_ARRAY_BUFFER_BINDING, &previous);
    d.BindBuffer(GL_ARRAY_BUFFER, buffer);
    if (integer) d.VertexAttribIPointer(loc, size, type, stride, ptr);
    else d.VertexAttribPointer(loc, size, type, normalized, stride, ptr);
    d.BindBuffer(GL_ARRAY_BUFFER, (GLuint)previous);
}

static void fix_attribs(void) {
    if (!int_attribs || !real_glGetIntegerv || !d.GetVertexAttribiv || !d.GetVertexAttribPointerv || !d.BindBuffer ||
        !d.VertexAttribIPointer || !d.VertexAttribPointer || !d.GetProgramiv || !d.GetActiveAttrib ||
        !d.GetAttribLocation)
        return;
    GLint prog = 0;
    real_glGetIntegerv(GL_CURRENT_PROGRAM, &prog);
    if (prog <= 0) return;
    EGLContext ctx = d.GetCurrentContext ? d.GetCurrentContext() : NULL;
    pthread_mutex_lock(&attrib_lock);
    struct proginfo *pi = program_info(ctx, (GLuint)prog);
    struct ctxstate *cs = ctx_state(ctx);
    uint32_t check = pi->int_mask | (pi->float_mask & cs->converted);
    if (!check) { pthread_mutex_unlock(&attrib_lock); return; }
    GLint vao = 0;
    real_glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &vao);
    for (int loc = 0; loc < MAXLOC; ++loc) {
        uint32_t bit = 1u << loc;
        if (!(check & bit)) continue;
        int want_int = (pi->int_mask & bit) != 0;
        GLint enabled = 0, integer = 0;
        d.GetVertexAttribiv((GLuint)loc, GL_VERTEX_ATTRIB_ARRAY_ENABLED, &enabled);
        if (!enabled) {
            if (want_int && (pi->unsigned_mask & bit) && d.VertexAttribI4ui) d.VertexAttribI4ui((GLuint)loc, 0, 0, 0, 1);
            else if (want_int && d.VertexAttribI4i) d.VertexAttribI4i((GLuint)loc, 0, 0, 0, 1);
            continue;
        }
        d.GetVertexAttribiv((GLuint)loc, GL_VERTEX_ATTRIB_ARRAY_INTEGER, &integer);
        if (want_int == (integer != 0)) continue;
        GLint buffer = 0, size = 0, type = 0, stride = 0, normalized = 0;
        void *ptr = NULL;
        d.GetVertexAttribiv((GLuint)loc, GL_VERTEX_ATTRIB_ARRAY_BUFFER_BINDING, &buffer);
        d.GetVertexAttribiv((GLuint)loc, GL_VERTEX_ATTRIB_ARRAY_SIZE, &size);
        d.GetVertexAttribiv((GLuint)loc, GL_VERTEX_ATTRIB_ARRAY_TYPE, &type);
        d.GetVertexAttribiv((GLuint)loc, GL_VERTEX_ATTRIB_ARRAY_STRIDE, &stride);
        d.GetVertexAttribiv((GLuint)loc, GL_VERTEX_ATTRIB_ARRAY_NORMALIZED, &normalized);
        d.GetVertexAttribPointerv((GLuint)loc, GL_VERTEX_ATTRIB_ARRAY_POINTER, &ptr);
        if (vao && !buffer) continue;  // client-side arrays only exist on the default vertex array
        if (want_int) {
            if (!glfix_int_component_type((unsigned)type)) {
                if (skip_logs < 8) {
                    ++skip_logs;
                    LOG("int attribs: program %d location %d has data of type 0x%x, left as it is", prog, loc, type);
                }
                continue;
            }
            rebind((GLuint)loc, (GLuint)buffer, size, (GLenum)type, (GLboolean)normalized, stride, ptr, 1);
            struct convrec *r = find_conv(ctx, (GLuint)vao, loc);
            if (!r) r = &convs[conv_next++ % NCONV];
            *r = (struct convrec){ctx, (GLuint)vao, (GLuint)buffer, loc, 1, size, stride, (GLenum)type, ptr,
                                  (GLboolean)normalized};
            cs->converted |= bit;
            if (attrib_logs < 24) {
                ++attrib_logs;
                LOG("int attribs: program %d location %d: floats -> glVertexAttribIPointer (vao %d buffer %d size %d "
                    "type 0x%x stride %d offset %p)", prog, loc, vao, buffer, size, type, stride, ptr);
            }
        } else {
            struct convrec *r = find_conv(ctx, (GLuint)vao, loc);
            if (!r || r->buffer != (GLuint)buffer || r->size != size || r->type != (GLenum)type || r->stride != stride ||
                r->ptr != ptr)
                continue;  // the game set it up as integers itself
            rebind((GLuint)loc, (GLuint)buffer, size, (GLenum)type, r->normalized, stride, ptr, 0);
            r->used = 0;
            if (attrib_logs < 24) { ++attrib_logs; LOG("int attribs: program %d location %d: back to floats", prog, loc); }
        }
    }
    pthread_mutex_unlock(&attrib_lock);
}

// Mesa's GLSL preprocessor rejects "#extension" after "#pragma" lines ("not allowed in the middle of a shader");
// Quest drivers accept it. Neutralise #pragma lines (optimize/debug hints only) as sources are uploaded.
typedef void (*PFN_SS)(GLuint, GLsizei, const GLchar *const *, const GLint *);
static PFN_SS r_shaderSourceSet;
static int pragma_logs;
static int have_implicit_conv = -1;
static int ext_supported(const char *want) {
    if (!real_glGetIntegerv || !real_glGetStringi) return 0;
    GLint total = 0;
    real_glGetIntegerv(GL_NUM_EXTENSIONS, &total);
    for (GLint i = 0; i < total; ++i) {
        const char *name = (const char *)real_glGetStringi(GL_EXTENSIONS, (GLuint)i);
        if (name && !strcmp(name, want)) return 1;
    }
    return 0;
}
static void w_shaderSource(GLuint sh, GLsizei count, const GLchar *const *strs, const GLint *lens) {
    size_t total = 0;
    for (GLsizei i = 0; i < count; ++i) total += lens && lens[i] >= 0 ? (size_t)lens[i] : strlen(strs[i]);
    char *src = malloc(total + 1);
    if (!src) { r_shaderSourceSet(sh, count, strs, lens); return; }
    size_t off = 0;
    for (GLsizei i = 0; i < count; ++i) {
        size_t n = lens && lens[i] >= 0 ? (size_t)lens[i] : strlen(strs[i]);
        memcpy(src + off, strs[i], n);
        off += n;
    }
    src[off] = 0;
    int fixed = 0;
    for (char *p = src; (p = strstr(p, "#pragma")); p += 7) {
        char *q = p;
        while (q > src && (q[-1] == ' ' || q[-1] == '\t')) --q;
        if (q == src || q[-1] == '\n') { p[0] = '/'; p[1] = '/'; ++fixed; }
    }
    if (fixed && pragma_logs < 3) { ++pragma_logs; LOG("shader %u: neutralised %d #pragma line(s)", sh, fixed); }
    if (hide_multiview) {  // the compiler still predefines GL_OVR_multiview*; make shaders take the single-view path
        int n = 0;
        for (char *p = src; (p = strstr(p, "GL_OVR_multiview")); p += 16) { p[0] = 'N'; p[1] = 'O'; ++n; }
        if (n && pragma_logs < 6) { ++pragma_logs; LOG("shader %u: disabled %d multiview reference(s)", sh, n); }
    }
    if (have_implicit_conv < 0) {
        have_implicit_conv = ext_supported("GL_EXT_shader_implicit_conversions");
        LOG("GL_EXT_shader_implicit_conversions=%d GL_EXT_gpu_shader5=%d", have_implicit_conv,
            ext_supported("GL_EXT_gpu_shader5"));
    }
    if (have_implicit_conv && !strstr(src, "GL_EXT_shader_implicit_conversions")) {
        char *nl = strncmp(src, "#version", 8) == 0 ? strchr(src, '\n') : NULL;
        if (nl) {
            static const char ext[] = "#extension GL_EXT_shader_implicit_conversions : enable\n";
            size_t head = (size_t)(nl + 1 - src), rest = strlen(nl + 1);
            char *out = malloc(head + sizeof(ext) + rest + 1);
            if (out) {
                memcpy(out, src, head);
                memcpy(out + head, ext, sizeof(ext) - 1);
                memcpy(out + head + sizeof(ext) - 1, nl + 1, rest + 1);
                free(src);
                src = out;
            }
        }
    }
    char *highp = precision_fix(sh, src);
    if (highp) { free(src); src = highp; }
    const GLchar *one = src;
    r_shaderSourceSet(sh, 1, &one, NULL);
    free(src);
}

__attribute__((constructor)) static void shim_ctor(void) {
    setenv("allow_glsl_extension_directive_midshader", "true", 0);
}

static void init(void) {
    read_conf();
    gles = dlopen(GLSHIM_GLES_LIB, RTLD_NOW | RTLD_LOCAL);
    void *egl = dlopen(GLSHIM_EGL_LIB, RTLD_NOW | RTLD_LOCAL);
    real_glGetString = gles ? dlsym(gles, "glGetString") : NULL;
    real_glGetStringi = gles ? dlsym(gles, "glGetStringi") : NULL;
    real_glGetIntegerv = gles ? dlsym(gles, "glGetIntegerv") : NULL;
    real_eglGetProcAddress = egl ? dlsym(egl, "eglGetProcAddress") : NULL;
    real_glBindFramebuffer = gles ? dlsym(gles, "glBindFramebuffer") : NULL;
    real_glEnable = gles ? dlsym(gles, "glEnable") : NULL;
    real_glDisable = gles ? dlsym(gles, "glDisable") : NULL;
    real_eglMakeCurrent = egl ? dlsym(egl, "eglMakeCurrent") : NULL;
#define REAL(field, sym) d.field = gles ? dlsym(gles, sym) : NULL;
    REAL(ShaderSource, "glShaderSource") REAL(GetShaderiv, "glGetShaderiv") REAL(LinkProgram, "glLinkProgram")
    REAL(ProgramBinary, "glProgramBinary") REAL(DeleteProgram, "glDeleteProgram") REAL(GetProgramiv, "glGetProgramiv")
    REAL(GetActiveAttrib, "glGetActiveAttrib") REAL(GetAttribLocation, "glGetAttribLocation")
    REAL(GetVertexAttribiv, "glGetVertexAttribiv") REAL(GetVertexAttribPointerv, "glGetVertexAttribPointerv")
    REAL(VertexAttribPointer, "glVertexAttribPointer") REAL(VertexAttribIPointer, "glVertexAttribIPointer")
    REAL(VertexAttribI4i, "glVertexAttribI4i") REAL(VertexAttribI4ui, "glVertexAttribI4ui")
    REAL(BindBuffer, "glBindBuffer") REAL(DrawArrays, "glDrawArrays") REAL(DrawElements, "glDrawElements")
    REAL(DrawArraysInstanced, "glDrawArraysInstanced") REAL(DrawElementsInstanced, "glDrawElementsInstanced")
    REAL(DrawRangeElements, "glDrawRangeElements")
#undef REAL
    d.GetCurrentContext = egl ? dlsym(egl, "eglGetCurrentContext") : NULL;
    static const char *const engines[] = {"libunity.so", "libUE4.so", "libUnreal.so"};
    int engine_found = 0;
    for (unsigned i = 0; i < sizeof engines / sizeof *engines && !engine_found; i++) {
        void *engine = dlopen(engines[i], RTLD_NOW | RTLD_NOLOAD);
        if (engine) {
            engine_found = 1;
            unreal = i > 0;
            dlclose(engine);
        }
    }
    // Unity and Unreal render multiview themselves and only need MSRTT hidden
    if (hide_multiview < 0) hide_multiview = !engine_found;
    unreal = unreal && hide_msrtt && !hide_multiview;  // only matters while MSRTT is hidden and multiview kept
    LOG("GL shim active: hide_multiview=%d hide_msrtt=%d%s", hide_multiview, hide_msrtt,
        unreal ? " (Unreal: multiview MSRTT kept, drawn single-sampled)" : "");
    if (int_attribs || highp_markers[0] || gamma_space)
        LOG("GL shim per-game fixes: gl_int_attribs=%d gl_highp_markers=%s gl_gamma_space=%d", int_attribs,
            highp_markers[0] ? highp_markers : "(none)", gamma_space);
}

// gl_gamma_space: GL_FRAMEBUFFER_SRGB_EXT is context state that defaults to enabled, so it is switched off whenever
// a context is made current (Unity imports eglMakeCurrent through this library, but loads its GL core functions with
// dlsym on libGLESv2.so, so glBindFramebuffer below only reaches engines that import it) and after every framebuffer
// bind that does come through here; the game's own glEnable of it is dropped. OVRPlugin's copies into the eye images
// run on Unity's render thread, in the same context.
#define GL_FRAMEBUFFER_SRGB_EXT 0x8DB9
static int srgb_logged;
static void srgb_conversion_off(void) {
    if (real_glDisable) real_glDisable(GL_FRAMEBUFFER_SRGB_EXT);
    if (!srgb_logged++) LOG("GL shim: no sRGB write conversion (gl_gamma_space=1)");
}
EXPORT void glBindFramebuffer(GLenum target, GLuint framebuffer) {
    pthread_once(&once, init);
    if (real_glBindFramebuffer) real_glBindFramebuffer(target, framebuffer);
    if (gamma_space) srgb_conversion_off();
}
EXPORT void glEnable(GLenum cap) {
    pthread_once(&once, init);
    if (gamma_space && cap == GL_FRAMEBUFFER_SRGB_EXT) return;
    if (real_glEnable) real_glEnable(cap);
}
EXPORT EGLBoolean eglMakeCurrent(EGLDisplay dpy, EGLSurface draw, EGLSurface read, EGLContext ctx) {
    pthread_once(&once, init);
    EGLBoolean ok = real_eglMakeCurrent ? real_eglMakeCurrent(dpy, draw, read, ctx) : EGL_FALSE;
    if (ok && ctx != EGL_NO_CONTEXT && gamma_space) srgb_conversion_off();
    return ok;
}

static int is_hidden(const char *name) {
    if (hide_multiview)
        for (size_t i = 0; i < sizeof(hidden_multiview) / sizeof(hidden_multiview[0]); ++i)
            if (!strcmp(name, hidden_multiview[i])) return 1;
    if (hide_msrtt)
        for (size_t i = 0; i < sizeof(hidden_msrtt) / sizeof(hidden_msrtt[0]); ++i)
            if (!strcmp(name, hidden_msrtt[i]))
                return !(unreal && !strcmp(name, "GL_OVR_multiview_multisampled_render_to_texture"));
    return 0;
}

// Filtered index table for glGetStringi (built lazily, per process).
static GLuint *visible;
static GLint visible_count = -1;
static void build_table(void) {
    if (visible_count >= 0 || !real_glGetIntegerv || !real_glGetStringi) return;
    GLint total = 0;
    real_glGetIntegerv(GL_NUM_EXTENSIONS, &total);
    visible = calloc(total > 0 ? (size_t)total : 1, sizeof(*visible));
    visible_count = 0;
    for (GLint i = 0; i < total; ++i) {
        const char *name = (const char *)real_glGetStringi(GL_EXTENSIONS, (GLuint)i);
        if (name && !is_hidden(name)) visible[visible_count++] = (GLuint)i;
    }
    LOG("extensions: %d of %d visible to the game", visible_count, total);
}

__attribute__((visibility("default"))) void glGetIntegerv(GLenum pname, GLint *data) {
    pthread_once(&once, init);
    if (pname == GL_NUM_EXTENSIONS && data) { build_table(); if (visible_count >= 0) { *data = visible_count; return; } }
    if (real_glGetIntegerv) real_glGetIntegerv(pname, data);
}

__attribute__((visibility("default"))) const GLubyte *glGetStringi(GLenum name, GLuint index) {
    pthread_once(&once, init);
    if (name == GL_EXTENSIONS) {
        build_table();
        if (visible_count >= 0) return index < (GLuint)visible_count ? real_glGetStringi(name, visible[index]) : NULL;
    }
    return real_glGetStringi ? real_glGetStringi(name, index) : NULL;
}

__attribute__((visibility("default"))) const GLubyte *glGetString(GLenum name) {
    pthread_once(&once, init);
    const GLubyte *s = real_glGetString ? real_glGetString(name) : NULL;
    if (name != GL_EXTENSIONS || !s) return s;
    static char *filtered;
    if (!filtered) {
        filtered = calloc(strlen((const char *)s) + 1, 1);
        char *copy = strdup((const char *)s), *save = NULL;
        for (char *tok = strtok_r(copy, " ", &save); tok; tok = strtok_r(NULL, " ", &save))
            if (!is_hidden(tok)) { strcat(filtered, tok); strcat(filtered, " "); }
        free(copy);
    }
    return (const GLubyte *)filtered;
}

// With GL_EXT_multisampled_render_to_texture hidden (gl_hide_msrtt), an app that calls its functions anyway (Team
// Beef's TBXR, e.g. Lambda1VR: color via glFramebufferTexture2DMultisampleEXT, depth via
// glRenderbufferStorageMultisampleEXT) gets plain single-sampled versions, so both attachments match: Zink called
// the mix GL_FRAMEBUFFER_INCOMPLETE_MULTISAMPLE and the eyes stayed black.
typedef void (*PFN_FT2D)(GLenum, GLenum, GLenum, GLuint, GLint);
typedef void (*PFN_RBS)(GLenum, GLenum, GLsizei, GLsizei);
static PFN_FT2D r_plain_ft2d;
static PFN_RBS r_plain_rbs;
static int msrtt_logged;
static void plain_ft2dms(GLenum target, GLenum attachment, GLenum textarget, GLuint texture, GLint level, GLsizei samples) {
    if (!msrtt_logged++) LOG("GL shim: multisampled render-to-texture (%d samples) drawn single-sampled", (int)samples);
    if (r_plain_ft2d) r_plain_ft2d(target, attachment, textarget, texture, level);
}
static void plain_rbsms(GLenum target, GLsizei samples, GLenum format, GLsizei width, GLsizei height) {
    (void)samples;
    if (r_plain_rbs) r_plain_rbs(target, format, width, height);
}
static PFN_FTMV r_plain_ftmv;
static int mv_msrtt_logged;
static void plain_ftmsmv(GLenum target, GLenum attachment, GLuint texture, GLint level, GLsizei samples,
                         GLint base, GLsizei views) {
    if (!mv_msrtt_logged++)
        LOG("GL shim: multiview multisampled render-to-texture (%d samples) drawn single-sampled", (int)samples);
    if (r_plain_ftmv) r_plain_ftmv(target, attachment, texture, level, base, views);
}

// gl_int_attribs: draws looked up through eglGetProcAddress (wrapped only when the setting is on)
typedef void (*PFN_PB)(GLuint, GLenum, const void *, GLsizei);
typedef void (*PFN_DEBV)(GLenum, GLsizei, GLenum, const void *, GLint);
typedef void (*PFN_DREBV)(GLenum, GLuint, GLuint, GLsizei, GLenum, const void *, GLint);
typedef void (*PFN_DEIBV)(GLenum, GLsizei, GLenum, const void *, GLsizei, GLint);
static PFN_PB e_pb, e_pboes;
static PFN_V_U e_dp;
static PFN_DA e_da;
static PFN_DE e_de;
static PFN_DAI e_dai;
static PFN_DEI e_dei;
static PFN_DRE e_dre;
static PFN_DEBV e_debv0, e_debv1, e_debv2;
static PFN_DREBV e_drebv0, e_drebv1, e_drebv2;
static PFN_DEIBV e_deibv0, e_deibv1, e_deibv2;
static void x_pb(GLuint p, GLenum f, const void *b, GLsizei n) { e_pb(p, f, b, n); forget_program(p); }
static void x_pboes(GLuint p, GLenum f, const void *b, GLsizei n) { e_pboes(p, f, b, n); forget_program(p); }
static void x_dp(GLuint p) { e_dp(p); forget_program(p); }
static void x_da(GLenum m, GLint f, GLsizei c) { fix_attribs(); e_da(m, f, c); }
static void x_de(GLenum m, GLsizei c, GLenum t, const void *i) { fix_attribs(); e_de(m, c, t, i); }
static void x_dai(GLenum m, GLint f, GLsizei c, GLsizei n) { fix_attribs(); e_dai(m, f, c, n); }
static void x_dei(GLenum m, GLsizei c, GLenum t, const void *i, GLsizei n) { fix_attribs(); e_dei(m, c, t, i, n); }
static void x_dre(GLenum m, GLuint s, GLuint e, GLsizei c, GLenum t, const void *i) {
    fix_attribs();
    e_dre(m, s, e, c, t, i);
}
#define BV(n) \
    static void x_debv##n(GLenum m, GLsizei c, GLenum t, const void *i, GLint b) { fix_attribs(); e_debv##n(m, c, t, i, b); } \
    static void x_drebv##n(GLenum m, GLuint s, GLuint e, GLsizei c, GLenum t, const void *i, GLint b) { \
        fix_attribs(); e_drebv##n(m, s, e, c, t, i, b); } \
    static void x_deibv##n(GLenum m, GLsizei c, GLenum t, const void *i, GLsizei k, GLint b) { \
        fix_attribs(); e_deibv##n(m, c, t, i, k, b); }
BV(0) BV(1) BV(2)
#undef BV

// Direct imports of a game library that loads the shim first (DT_NEEDED): these come before libGLESv3's. Plain
// pass-through unless a per-game fix is on.
EXPORT void glShaderSource(GLuint sh, GLsizei count, const GLchar *const *strs, const GLint *lens) {
    pthread_once(&once, init);
    if (!d.ShaderSource) return;
    if (highp_markers[0] && count > 0 && strs) {
        char *src = concat_sources(count, strs, lens);
        char *highp = src ? precision_fix(sh, src) : NULL;
        free(src);
        if (highp) {
            const GLchar *one = highp;
            d.ShaderSource(sh, 1, &one, NULL);
            free(highp);
            return;
        }
    }
    d.ShaderSource(sh, count, strs, lens);
}
EXPORT void glLinkProgram(GLuint p) {
    pthread_once(&once, init);
    if (d.LinkProgram) d.LinkProgram(p);
    forget_program(p);
}
EXPORT void glProgramBinary(GLuint p, GLenum f, const void *b, GLsizei n) {
    pthread_once(&once, init);
    if (d.ProgramBinary) d.ProgramBinary(p, f, b, n);
    forget_program(p);
}
EXPORT void glDeleteProgram(GLuint p) {
    pthread_once(&once, init);
    if (d.DeleteProgram) d.DeleteProgram(p);
    forget_program(p);
}
EXPORT void glDrawArrays(GLenum m, GLint f, GLsizei c) {
    pthread_once(&once, init);
    fix_attribs();
    if (d.DrawArrays) d.DrawArrays(m, f, c);
}
EXPORT void glDrawElements(GLenum m, GLsizei c, GLenum t, const void *i) {
    pthread_once(&once, init);
    fix_attribs();
    if (d.DrawElements) d.DrawElements(m, c, t, i);
}
EXPORT void glDrawArraysInstanced(GLenum m, GLint f, GLsizei c, GLsizei n) {
    pthread_once(&once, init);
    fix_attribs();
    if (d.DrawArraysInstanced) d.DrawArraysInstanced(m, f, c, n);
}
EXPORT void glDrawElementsInstanced(GLenum m, GLsizei c, GLenum t, const void *i, GLsizei n) {
    pthread_once(&once, init);
    fix_attribs();
    if (d.DrawElementsInstanced) d.DrawElementsInstanced(m, c, t, i, n);
}
EXPORT void glDrawRangeElements(GLenum m, GLuint s, GLuint e, GLsizei c, GLenum t, const void *i) {
    pthread_once(&once, init);
    fix_attribs();
    if (d.DrawRangeElements) d.DrawRangeElements(m, s, e, c, t, i);
}

__attribute__((visibility("default"))) __eglMustCastToProperFunctionPointerType eglGetProcAddress(const char *name) {
    pthread_once(&once, init);
    if (name && !strcmp(name, "glGetStringi")) return (__eglMustCastToProperFunctionPointerType)glGetStringi;
    if (name && !strcmp(name, "glGetString")) return (__eglMustCastToProperFunctionPointerType)glGetString;
    if (name && !strcmp(name, "glGetIntegerv")) return (__eglMustCastToProperFunctionPointerType)glGetIntegerv;
    if (gamma_space && name && !strcmp(name, "glBindFramebuffer"))
        return (__eglMustCastToProperFunctionPointerType)glBindFramebuffer;
    if (gamma_space && name && !strcmp(name, "glEnable")) return (__eglMustCastToProperFunctionPointerType)glEnable;
    __eglMustCastToProperFunctionPointerType fn = real_eglGetProcAddress ? real_eglGetProcAddress(name) : NULL;
    if (!fn || !name) return fn;
#define WRAP(sym, real, w) if (!strcmp(name, sym)) { real = (void *)fn; return (__eglMustCastToProperFunctionPointerType)w; }
    if (hide_msrtt && (!strcmp(name, "glFramebufferTexture2DMultisampleEXT") ||
                       !strcmp(name, "glRenderbufferStorageMultisampleEXT"))) {
        if (!r_plain_ft2d) r_plain_ft2d = (PFN_FT2D)real_eglGetProcAddress("glFramebufferTexture2D");
        if (!r_plain_rbs) r_plain_rbs = (PFN_RBS)real_eglGetProcAddress("glRenderbufferStorage");
        return name[2] == 'F' ? (__eglMustCastToProperFunctionPointerType)plain_ft2dms
                              : (__eglMustCastToProperFunctionPointerType)plain_rbsms;
    }
    if (unreal && !strcmp(name, "glFramebufferTextureMultisampleMultiviewOVR")) {
        if (!r_plain_ftmv) r_plain_ftmv = (PFN_FTMV)real_eglGetProcAddress("glFramebufferTextureMultiviewOVR");
        return (__eglMustCastToProperFunctionPointerType)plain_ftmsmv;
    }
#ifdef GLSHIM_TRACE  // build with -DGLSHIM_TRACE for eye-buffer / draw / error tracing
    WRAP("glFramebufferTextureMultiviewOVR", real_ftmv, w_ftmv)
    WRAP("glFramebufferTextureMultisampleMultiviewOVR", real_ftmsmv, w_ftmsmv)
    WRAP("glFramebufferTexture2DMultisampleEXT", real_ft2dms, w_ft2dms)
    WRAP("glCheckFramebufferStatus", real_cfs, w_cfs)
#endif
    if (!r_getError) {
        r_getError = (PFN_GE)real_eglGetProcAddress("glGetError");
        r_getIv = (PFN_GIV)real_eglGetProcAddress("glGetIntegerv");
        r_getShaderiv = (PFN_GSIV)real_eglGetProcAddress("glGetShaderiv");
        r_getProgramiv = (PFN_GSIV)real_eglGetProcAddress("glGetProgramiv");
        r_shaderLog = (PFN_GLOG)real_eglGetProcAddress("glGetShaderInfoLog");
        r_programLog = (PFN_GLOG)real_eglGetProcAddress("glGetProgramInfoLog");
        r_shaderSource = (PFN_GLOG)real_eglGetProcAddress("glGetShaderSource");
    }
    WRAP("glShaderSource", r_shaderSourceSet, w_shaderSource)
    WRAP("glCompileShader", r_compile, w_compile)
    WRAP("glLinkProgram", r_link, w_link)
    if (int_attribs) {
        WRAP("glProgramBinary", e_pb, x_pb)
        WRAP("glProgramBinaryOES", e_pboes, x_pboes)
        WRAP("glDeleteProgram", e_dp, x_dp)
        WRAP("glDrawArrays", e_da, x_da)
        WRAP("glDrawElements", e_de, x_de)
        WRAP("glDrawArraysInstanced", e_dai, x_dai)
        WRAP("glDrawElementsInstanced", e_dei, x_dei)
        WRAP("glDrawRangeElements", e_dre, x_dre)
        WRAP("glDrawElementsBaseVertex", e_debv0, x_debv0)
        WRAP("glDrawElementsBaseVertexEXT", e_debv1, x_debv1)
        WRAP("glDrawElementsBaseVertexOES", e_debv2, x_debv2)
        WRAP("glDrawRangeElementsBaseVertex", e_drebv0, x_drebv0)
        WRAP("glDrawRangeElementsBaseVertexEXT", e_drebv1, x_drebv1)
        WRAP("glDrawRangeElementsBaseVertexOES", e_drebv2, x_drebv2)
        WRAP("glDrawElementsInstancedBaseVertex", e_deibv0, x_deibv0)
        WRAP("glDrawElementsInstancedBaseVertexEXT", e_deibv1, x_deibv1)
        WRAP("glDrawElementsInstancedBaseVertexOES", e_deibv2, x_deibv2)
    }
#ifdef GLSHIM_TRACE
    WRAP("glBindFramebuffer", r_bindFb, w_bindFb)
    WRAP("glFramebufferTexture2D", r_ft2d, w_ft2d)
    WRAP("glFramebufferTextureLayer", r_ftl, w_ftl)
    WRAP("glDrawArrays", r_da, w_da)
    WRAP("glDrawElements", r_de, w_de)
    WRAP("glDrawArraysInstanced", r_dai, w_dai)
    WRAP("glDrawElementsInstanced", r_dei, w_dei)
    WRAP("glDrawRangeElements", r_dre, w_dre)
    WRAP("glClear", r_clear, w_clear)
    WRAP("glBlitFramebuffer", r_blit, w_blit)
#endif
    return fn;
}
