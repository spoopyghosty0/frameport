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

static void read_conf_file(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        if (!strncmp(line, "gl_hide_multiview=", 18)) hide_multiview = atoi(line + 18);
        if (!strncmp(line, "gl_hide_msrtt=", 14)) hide_msrtt = atoi(line + 14);
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
    const GLchar *one = src;
    r_shaderSourceSet(sh, 1, &one, NULL);
    free(src);
}

__attribute__((constructor)) static void shim_ctor(void) {
    setenv("allow_glsl_extension_directive_midshader", "true", 0);
}

static void init(void) {
    read_conf();
    gles = dlopen("libGLESv3.so", RTLD_NOW | RTLD_LOCAL);
    void *egl = dlopen("libEGL.so", RTLD_NOW | RTLD_LOCAL);
    real_glGetString = gles ? dlsym(gles, "glGetString") : NULL;
    real_glGetStringi = gles ? dlsym(gles, "glGetStringi") : NULL;
    real_glGetIntegerv = gles ? dlsym(gles, "glGetIntegerv") : NULL;
    real_eglGetProcAddress = egl ? dlsym(egl, "eglGetProcAddress") : NULL;
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

__attribute__((visibility("default"))) __eglMustCastToProperFunctionPointerType eglGetProcAddress(const char *name) {
    pthread_once(&once, init);
    if (name && !strcmp(name, "glGetStringi")) return (__eglMustCastToProperFunctionPointerType)glGetStringi;
    if (name && !strcmp(name, "glGetString")) return (__eglMustCastToProperFunctionPointerType)glGetString;
    if (name && !strcmp(name, "glGetIntegerv")) return (__eglMustCastToProperFunctionPointerType)glGetIntegerv;
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
