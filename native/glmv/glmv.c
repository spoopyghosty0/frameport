// SPDX-License-Identifier: GPL-3.0-only
// libfpglmv.so: single-view draws of OVR_multiview programs (frame.gl_multiview_fbo, GitHub #77).
//
// OVR_multiview says a draw is INVALID_OPERATION when the number of views of the draw framebuffer differs from the
// num_views the program's vertex shader declares. Qualcomm's Quest driver tolerates it; Mesa (the Frame's GL, Zink)
// enforces it (src/mesa/main/draw_validate.c) and drops the draw silently. Engines that compile every shader with
// `layout(num_views=2) in;` and also draw into ordinary 2D framebuffers (Doom3Quest's HUD/PDA pool: glTexImage2D
// textures + renderbuffers) then get black panels.
//
// This library sits between such an engine and libGLESv3: the patch puts it first in the engine library's DT_NEEDED
// (direct gl* imports bind here) and points the engine's dlopen("libGLESv3.so") at it (same-length name, so its
// qgl* table from dlsym binds here too); every function it doesn't define resolves to libGLESv3 through its own
// dependency (linked --no-as-needed). When a multiview program draws into a framebuffer whose color attachment has no
// views (2D texture, texture layer, renderbuffer, FBO 0), the draw uses a lazily built single-view twin of the program
// (glmv_rewrite.h: num_views layout removed, gl_ViewID_OVR -> 0u), with the original's attribute locations, uniform
// block bindings and default-block uniform values (copied before every such draw), then the original is bound again.
//
// Logcat tag GLMV. Setting gl_mv_debug=1 (libframe_settings.so / framebridge.conf / FRAMEBRIDGE_CONFIG, as the GL
// shim): glGetError after every twin draw, and per-5-s counters.
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <GLES3/gl32.h>
#include <android/log.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "glmv_rewrite.h"

#define TAG "GLMV"
#define LOG(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)
#define EXPORT __attribute__((visibility("default")))
#ifndef GLMV_GLES_LIB  // (the host test points these at a stand-in GL library)
#define GLMV_GLES_LIB "libGLESv3.so"
#define GLMV_EGL_LIB "libEGL.so"
#endif
#ifndef GL_FRAMEBUFFER_ATTACHMENT_TEXTURE_NUM_VIEWS_OVR
#define GL_FRAMEBUFFER_ATTACHMENT_TEXTURE_NUM_VIEWS_OVR 0x9630
#endif

// ---------------------------------------------------------------- real functions (only ever from libGLESv3's handle)
#define REAL_LIST(X) \
    X(void, UseProgram, (GLuint)) \
    X(void, LinkProgram, (GLuint)) \
    X(void, DeleteProgram, (GLuint)) \
    X(void, BindFramebuffer, (GLenum, GLuint)) \
    X(void, DeleteFramebuffers, (GLsizei, const GLuint *)) \
    X(void, FramebufferTexture2D, (GLenum, GLenum, GLenum, GLuint, GLint)) \
    X(void, FramebufferRenderbuffer, (GLenum, GLenum, GLenum, GLuint)) \
    X(void, FramebufferTextureLayer, (GLenum, GLenum, GLuint, GLint, GLint)) \
    X(void, FramebufferTexture, (GLenum, GLenum, GLuint, GLint)) \
    X(void, DrawBuffers, (GLsizei, const GLenum *)) \
    X(void, DrawArrays, (GLenum, GLint, GLsizei)) \
    X(void, DrawElements, (GLenum, GLsizei, GLenum, const void *)) \
    X(void, DrawArraysInstanced, (GLenum, GLint, GLsizei, GLsizei)) \
    X(void, DrawElementsInstanced, (GLenum, GLsizei, GLenum, const void *, GLsizei)) \
    X(void, DrawRangeElements, (GLenum, GLuint, GLuint, GLsizei, GLenum, const void *)) \
    X(void, DrawArraysIndirect, (GLenum, const void *)) \
    X(void, DrawElementsIndirect, (GLenum, GLenum, const void *)) \
    X(void, DrawElementsBaseVertex, (GLenum, GLsizei, GLenum, const void *, GLint)) \
    X(void, DrawRangeElementsBaseVertex, (GLenum, GLuint, GLuint, GLsizei, GLenum, const void *, GLint)) \
    X(void, DrawElementsInstancedBaseVertex, (GLenum, GLsizei, GLenum, const void *, GLsizei, GLint)) \
    X(GLenum, GetError, (void)) \
    X(void, GetIntegerv, (GLenum, GLint *)) \
    X(void, GetFramebufferAttachmentParameteriv, (GLenum, GLenum, GLenum, GLint *)) \
    X(void, GetProgramiv, (GLuint, GLenum, GLint *)) \
    X(void, GetProgramInfoLog, (GLuint, GLsizei, GLsizei *, GLchar *)) \
    X(void, GetShaderiv, (GLuint, GLenum, GLint *)) \
    X(void, GetShaderInfoLog, (GLuint, GLsizei, GLsizei *, GLchar *)) \
    X(void, GetShaderSource, (GLuint, GLsizei, GLsizei *, GLchar *)) \
    X(void, GetAttachedShaders, (GLuint, GLsizei, GLsizei *, GLuint *)) \
    X(GLuint, CreateShader, (GLenum)) \
    X(void, ShaderSource, (GLuint, GLsizei, const GLchar *const *, const GLint *)) \
    X(void, CompileShader, (GLuint)) \
    X(void, DeleteShader, (GLuint)) \
    X(GLuint, CreateProgram, (void)) \
    X(void, AttachShader, (GLuint, GLuint)) \
    X(void, BindAttribLocation, (GLuint, GLuint, const GLchar *)) \
    X(void, GetActiveAttrib, (GLuint, GLuint, GLsizei, GLsizei *, GLint *, GLenum *, GLchar *)) \
    X(GLint, GetAttribLocation, (GLuint, const GLchar *)) \
    X(void, GetActiveUniform, (GLuint, GLuint, GLsizei, GLsizei *, GLint *, GLenum *, GLchar *)) \
    X(void, GetActiveUniformsiv, (GLuint, GLsizei, const GLuint *, GLenum, GLint *)) \
    X(GLint, GetUniformLocation, (GLuint, const GLchar *)) \
    X(void, GetUniformfv, (GLuint, GLint, GLfloat *)) \
    X(void, GetUniformiv, (GLuint, GLint, GLint *)) \
    X(void, GetUniformuiv, (GLuint, GLint, GLuint *)) \
    X(void, GetActiveUniformBlockName, (GLuint, GLuint, GLsizei, GLsizei *, GLchar *)) \
    X(void, GetActiveUniformBlockiv, (GLuint, GLuint, GLenum, GLint *)) \
    X(GLuint, GetUniformBlockIndex, (GLuint, const GLchar *)) \
    X(void, UniformBlockBinding, (GLuint, GLuint, GLuint)) \
    X(void, Uniform1fv, (GLint, GLsizei, const GLfloat *)) \
    X(void, Uniform2fv, (GLint, GLsizei, const GLfloat *)) \
    X(void, Uniform3fv, (GLint, GLsizei, const GLfloat *)) \
    X(void, Uniform4fv, (GLint, GLsizei, const GLfloat *)) \
    X(void, Uniform1iv, (GLint, GLsizei, const GLint *)) \
    X(void, Uniform2iv, (GLint, GLsizei, const GLint *)) \
    X(void, Uniform3iv, (GLint, GLsizei, const GLint *)) \
    X(void, Uniform4iv, (GLint, GLsizei, const GLint *)) \
    X(void, Uniform1uiv, (GLint, GLsizei, const GLuint *)) \
    X(void, Uniform2uiv, (GLint, GLsizei, const GLuint *)) \
    X(void, Uniform3uiv, (GLint, GLsizei, const GLuint *)) \
    X(void, Uniform4uiv, (GLint, GLsizei, const GLuint *)) \
    X(void, UniformMatrix2fv, (GLint, GLsizei, GLboolean, const GLfloat *)) \
    X(void, UniformMatrix3fv, (GLint, GLsizei, GLboolean, const GLfloat *)) \
    X(void, UniformMatrix4fv, (GLint, GLsizei, GLboolean, const GLfloat *)) \
    X(void, UniformMatrix2x3fv, (GLint, GLsizei, GLboolean, const GLfloat *)) \
    X(void, UniformMatrix3x2fv, (GLint, GLsizei, GLboolean, const GLfloat *)) \
    X(void, UniformMatrix2x4fv, (GLint, GLsizei, GLboolean, const GLfloat *)) \
    X(void, UniformMatrix4x2fv, (GLint, GLsizei, GLboolean, const GLfloat *)) \
    X(void, UniformMatrix3x4fv, (GLint, GLsizei, GLboolean, const GLfloat *)) \
    X(void, UniformMatrix4x3fv, (GLint, GLsizei, GLboolean, const GLfloat *))

#define DECL(ret, name, args) static ret(*r_##name) args;
REAL_LIST(DECL)
#undef DECL
typedef void (*PFN_FTMV)(GLenum, GLenum, GLuint, GLint, GLint, GLsizei);
typedef void (*PFN_FTMSMV)(GLenum, GLenum, GLuint, GLint, GLsizei, GLint, GLsizei);
static PFN_FTMV r_ftmv;
static PFN_FTMSMV r_ftmsmv;
static __eglMustCastToProperFunctionPointerType (*r_eglGetProcAddress)(const char *);
static EGLBoolean (*r_eglMakeCurrent)(EGLDisplay, EGLSurface, EGLSurface, EGLContext);

static pthread_once_t once = PTHREAD_ONCE_INIT;
static int debug;

static void read_conf_file(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[256];
    while (fgets(line, sizeof(line), f))
        if (!strncmp(line, "gl_mv_debug=", 12)) debug = atoi(line + 12);
    fclose(f);
}

// The GL shim's settings sources, later ones winning: libframe_settings.so next to this library, the game's
// framebridge.conf, then FRAMEBRIDGE_CONFIG (Lepton's per-game settings.conf).
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

static void init(void) {
    read_conf();
    void *gles = dlopen(GLMV_GLES_LIB, RTLD_NOW | RTLD_LOCAL);
    void *egl = dlopen(GLMV_EGL_LIB, RTLD_NOW | RTLD_LOCAL);
    r_eglGetProcAddress = egl ? dlsym(egl, "eglGetProcAddress") : NULL;
    r_eglMakeCurrent = egl ? dlsym(egl, "eglMakeCurrent") : NULL;
    int missing = 0;
#define LOAD(ret, name, args)                                                                       \
    r_##name = gles ? (ret(*) args)dlsym(gles, "gl" #name) : NULL;                                  \
    if (!r_##name && r_eglGetProcAddress) r_##name = (ret(*) args)r_eglGetProcAddress("gl" #name); \
    missing += !r_##name;
    REAL_LIST(LOAD)
#undef LOAD
    if (r_eglGetProcAddress) {
        r_ftmv = (PFN_FTMV)r_eglGetProcAddress("glFramebufferTextureMultiviewOVR");
        r_ftmsmv = (PFN_FTMSMV)r_eglGetProcAddress("glFramebufferTextureMultisampleMultiviewOVR");
    }
    LOG("library active: single-view draws of multiview programs (gl_mv_debug=%d, %d GL functions missing)", debug,
        missing);
}
#define INIT() pthread_once(&once, init)

// ---------------------------------------------------------------- programs
struct uni {
    GLenum type;
    GLint orig, var;  // locations in the original and the twin
    GLuint shadow[16];  // last value given to the twin
    int valid;
};
struct block {
    GLuint orig_index, var_index;
    GLint binding;  // last binding given to the twin
};
struct prog {
    GLuint id;
    int multiview;  // the vertex shader declares num_views
    int nshaders;
    GLenum types[6];
    char *sources[6];  // stage sources at link time (only kept for multiview programs)
    GLuint twin;  // single-view twin, 0 = not built yet
    int twin_failed;
    struct uni *unis;
    int nunis;
    struct block *blocks;
    int nblocks;
};

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static struct prog **progs;  // indexed by program name (GL names are small integers)
static GLuint nprogs;

static struct prog *find_prog(GLuint id, int create) {
    struct prog *p = NULL;
    pthread_mutex_lock(&lock);
    if (id < nprogs) p = progs[id];
    if (!p && create && id < (1u << 24)) {
        if (id >= nprogs) {
            GLuint n = nprogs ? nprogs : 256;
            while (n <= id) n *= 2;
            struct prog **grown = realloc(progs, n * sizeof(*grown));
            if (grown) {
                memset(grown + nprogs, 0, (n - nprogs) * sizeof(*grown));
                progs = grown;
                nprogs = n;
            }
        }
        if (id < nprogs && (p = calloc(1, sizeof(*p)))) {
            p->id = id;
            progs[id] = p;
        }
    }
    pthread_mutex_unlock(&lock);
    return p;
}

static void drop_twin(struct prog *p) {
    if (p->twin && r_DeleteProgram) r_DeleteProgram(p->twin);
    p->twin = 0;
    p->twin_failed = 0;
    free(p->unis);
    p->unis = NULL;
    p->nunis = 0;
    free(p->blocks);
    p->blocks = NULL;
    p->nblocks = 0;
}

static void drop_sources(struct prog *p) {
    for (int i = 0; i < p->nshaders; ++i) free(p->sources[i]);
    p->nshaders = 0;
    p->multiview = 0;
}

static char *shader_source(GLuint sh) {
    GLint len = 0;
    r_GetShaderiv(sh, GL_SHADER_SOURCE_LENGTH, &len);
    if (len <= 0) return NULL;
    char *src = malloc((size_t)len + 1);
    if (!src) return NULL;
    GLsizei got = 0;
    r_GetShaderSource(sh, len, &got, src);
    src[got > 0 && got <= len ? got : 0] = 0;
    return src;
}

// Before the real link: note whether the program is multiview and keep its stage sources for a later twin.
static void note_program(GLuint id) {
    struct prog *p = find_prog(id, 1);
    if (!p) return;
    drop_twin(p);
    drop_sources(p);
    GLuint shaders[6];
    GLsizei n = 0;
    r_GetAttachedShaders(id, 6, &n, shaders);
    for (GLsizei i = 0; i < n && i < 6; ++i) {
        GLint type = 0;
        r_GetShaderiv(shaders[i], GL_SHADER_TYPE, &type);
        char *src = shader_source(shaders[i]);
        if (!src) continue;
        if (type == GL_VERTEX_SHADER && glmv_has_num_views(src)) p->multiview = 1;
        p->types[p->nshaders] = (GLenum)type;
        p->sources[p->nshaders++] = src;
    }
    if (!p->multiview) drop_sources(p);
}

// ---------------------------------------------------------------- the single-view twin
static int components(GLenum type, int *kind) {  // kind: 0 float, 1 int/bool/sampler, 2 uint, 3 matrix
    switch (type) {
    case GL_FLOAT: *kind = 0; return 1;
    case GL_FLOAT_VEC2: *kind = 0; return 2;
    case GL_FLOAT_VEC3: *kind = 0; return 3;
    case GL_FLOAT_VEC4: *kind = 0; return 4;
    case GL_INT: case GL_BOOL: *kind = 1; return 1;
    case GL_INT_VEC2: case GL_BOOL_VEC2: *kind = 1; return 2;
    case GL_INT_VEC3: case GL_BOOL_VEC3: *kind = 1; return 3;
    case GL_INT_VEC4: case GL_BOOL_VEC4: *kind = 1; return 4;
    case GL_UNSIGNED_INT: *kind = 2; return 1;
    case GL_UNSIGNED_INT_VEC2: *kind = 2; return 2;
    case GL_UNSIGNED_INT_VEC3: *kind = 2; return 3;
    case GL_UNSIGNED_INT_VEC4: *kind = 2; return 4;
    case GL_FLOAT_MAT2: *kind = 3; return 4;
    case GL_FLOAT_MAT3: *kind = 3; return 9;
    case GL_FLOAT_MAT4: *kind = 3; return 16;
    case GL_FLOAT_MAT2x3: case GL_FLOAT_MAT3x2: *kind = 3; return 6;
    case GL_FLOAT_MAT2x4: case GL_FLOAT_MAT4x2: *kind = 3; return 8;
    case GL_FLOAT_MAT3x4: case GL_FLOAT_MAT4x3: *kind = 3; return 12;
    default: *kind = 1; return 1;  // samplers and images: one int (the unit)
    }
}

static GLuint compile(GLenum type, const char *src, GLuint prog_id) {
    GLuint sh = r_CreateShader(type);
    if (!sh) return 0;
    r_ShaderSource(sh, 1, &src, NULL);
    r_CompileShader(sh);
    GLint ok = 0;
    r_GetShaderiv(sh, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[600] = {0};
        r_GetShaderInfoLog(sh, sizeof(log) - 1, NULL, log);
        LOG("twin of program %u: stage 0x%x failed to compile: %s", prog_id, type, log);
        r_DeleteShader(sh);
        return 0;
    }
    return sh;
}

static void copy_attrib_locations(GLuint orig, GLuint twin) {
    GLint n = 0;
    r_GetProgramiv(orig, GL_ACTIVE_ATTRIBUTES, &n);
    for (GLint i = 0; i < n; ++i) {
        char name[256];
        GLsizei len = 0;
        GLint size = 0;
        GLenum type = 0;
        r_GetActiveAttrib(orig, (GLuint)i, sizeof(name), &len, &size, &type, name);
        if (len <= 0 || !strncmp(name, "gl_", 3)) continue;
        GLint loc = r_GetAttribLocation(orig, name);
        if (loc >= 0) r_BindAttribLocation(twin, (GLuint)loc, name);
    }
}

static void map_uniforms(struct prog *p) {
    GLint n = 0;
    r_GetProgramiv(p->twin, GL_ACTIVE_UNIFORMS, &n);
    int cap = 0;
    for (GLint i = 0; i < n; ++i) {
        char name[256];
        GLsizei len = 0;
        GLint size = 0;
        GLenum type = 0;
        GLuint index = (GLuint)i;
        GLint block = -1;
        r_GetActiveUniform(p->twin, index, sizeof(name) - 16, &len, &size, &type, name);
        r_GetActiveUniformsiv(p->twin, 1, &index, GL_UNIFORM_BLOCK_INDEX, &block);
        if (len <= 0 || block != -1 || !strncmp(name, "gl_", 3)) continue;  // block members live in buffers
        char *bracket = len > 3 && !strcmp(name + len - 3, "[0]") ? name + len - 3 : NULL;
        if (bracket) *bracket = 0;
        for (GLint e = 0; e < (size > 0 ? size : 1); ++e) {
            char elem[300];
            if (bracket) snprintf(elem, sizeof(elem), "%s[%d]", name, e);
            else snprintf(elem, sizeof(elem), "%s", name);
            GLint lo = r_GetUniformLocation(p->id, elem), lt = r_GetUniformLocation(p->twin, elem);
            if (lo < 0 || lt < 0) continue;
            if (p->nunis == cap) {
                cap = cap ? cap * 2 : 16;
                struct uni *grown = realloc(p->unis, (size_t)cap * sizeof(*grown));
                if (!grown) return;
                p->unis = grown;
            }
            p->unis[p->nunis++] = (struct uni){.type = type, .orig = lo, .var = lt};
        }
    }
    GLint nb = 0;
    r_GetProgramiv(p->twin, GL_ACTIVE_UNIFORM_BLOCKS, &nb);
    p->blocks = nb > 0 ? calloc((size_t)nb, sizeof(*p->blocks)) : NULL;
    for (GLint i = 0; p->blocks && i < nb; ++i) {
        char name[256];
        GLsizei len = 0;
        r_GetActiveUniformBlockName(p->twin, (GLuint)i, sizeof(name), &len, name);
        GLuint oi = len > 0 ? r_GetUniformBlockIndex(p->id, name) : GL_INVALID_INDEX;
        if (oi == GL_INVALID_INDEX) continue;
        p->blocks[p->nblocks++] = (struct block){.orig_index = oi, .var_index = (GLuint)i, .binding = -1};
    }
}

static int build_twin(struct prog *p) {
    GLuint twin = r_CreateProgram(), shaders[6];
    int ns = 0, layouts = 0, ids = 0;
    for (int i = 0; twin && i < p->nshaders; ++i) {
        int l = 0, v = 0;
        char *src = glmv_rewrite(p->sources[i], &l, &v);
        GLuint sh = src ? compile(p->types[i], src, p->id) : 0;
        free(src);
        if (!sh) { ns = -1; break; }
        layouts += l;
        ids += v;
        r_AttachShader(twin, sh);
        shaders[ns++] = sh;
    }
    if (twin && ns > 0) {
        copy_attrib_locations(p->id, twin);
        r_LinkProgram(twin);
    }
    for (int i = 0; i < ns; ++i) r_DeleteShader(shaders[i]);  // flagged only; freed with the program
    GLint ok = 0;
    if (twin && ns > 0) r_GetProgramiv(twin, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[600] = {0};
        if (twin && ns > 0) r_GetProgramInfoLog(twin, sizeof(log) - 1, NULL, log);
        LOG("twin of program %u failed (%s): drawing it unchanged", p->id, ns > 0 ? log : "compile");
        if (twin) r_DeleteProgram(twin);
        p->twin_failed = 1;
        return 0;
    }
    p->twin = twin;
    map_uniforms(p);
    LOG("twin built: program %u -> %u (%d uniforms, %d blocks, %d num_views layouts and %d gl_ViewID_OVR replaced)",
        p->id, twin, p->nunis, p->nblocks, layouts, ids);
    return 1;
}

static unsigned long uniform_sets;  // twin uniforms changed (statistics)

static void sync_uniforms(struct prog *p) {
    for (int i = 0; i < p->nunis; ++i) {
        struct uni *u = &p->unis[i];
        int kind, n = components(u->type, &kind);
        GLuint v[16] = {0};
        if (kind == 0 || kind == 3) r_GetUniformfv(p->id, u->orig, (GLfloat *)v);
        else if (kind == 2) r_GetUniformuiv(p->id, u->orig, v);
        else r_GetUniformiv(p->id, u->orig, (GLint *)v);
        if (u->valid && !memcmp(v, u->shadow, (size_t)n * sizeof(GLuint))) continue;
        memcpy(u->shadow, v, sizeof(v));
        u->valid = 1;
        ++uniform_sets;
        const GLfloat *f = (const GLfloat *)v;
        const GLint *iv = (const GLint *)v;
        switch (u->type) {
        case GL_FLOAT: r_Uniform1fv(u->var, 1, f); break;
        case GL_FLOAT_VEC2: r_Uniform2fv(u->var, 1, f); break;
        case GL_FLOAT_VEC3: r_Uniform3fv(u->var, 1, f); break;
        case GL_FLOAT_VEC4: r_Uniform4fv(u->var, 1, f); break;
        case GL_UNSIGNED_INT: r_Uniform1uiv(u->var, 1, v); break;
        case GL_UNSIGNED_INT_VEC2: r_Uniform2uiv(u->var, 1, v); break;
        case GL_UNSIGNED_INT_VEC3: r_Uniform3uiv(u->var, 1, v); break;
        case GL_UNSIGNED_INT_VEC4: r_Uniform4uiv(u->var, 1, v); break;
        case GL_FLOAT_MAT2: r_UniformMatrix2fv(u->var, 1, GL_FALSE, f); break;
        case GL_FLOAT_MAT3: r_UniformMatrix3fv(u->var, 1, GL_FALSE, f); break;
        case GL_FLOAT_MAT4: r_UniformMatrix4fv(u->var, 1, GL_FALSE, f); break;
        case GL_FLOAT_MAT2x3: r_UniformMatrix2x3fv(u->var, 1, GL_FALSE, f); break;
        case GL_FLOAT_MAT3x2: r_UniformMatrix3x2fv(u->var, 1, GL_FALSE, f); break;
        case GL_FLOAT_MAT2x4: r_UniformMatrix2x4fv(u->var, 1, GL_FALSE, f); break;
        case GL_FLOAT_MAT4x2: r_UniformMatrix4x2fv(u->var, 1, GL_FALSE, f); break;
        case GL_FLOAT_MAT3x4: r_UniformMatrix3x4fv(u->var, 1, GL_FALSE, f); break;
        case GL_FLOAT_MAT4x3: r_UniformMatrix4x3fv(u->var, 1, GL_FALSE, f); break;
        case GL_INT_VEC2: case GL_BOOL_VEC2: r_Uniform2iv(u->var, 1, iv); break;
        case GL_INT_VEC3: case GL_BOOL_VEC3: r_Uniform3iv(u->var, 1, iv); break;
        case GL_INT_VEC4: case GL_BOOL_VEC4: r_Uniform4iv(u->var, 1, iv); break;
        default: r_Uniform1iv(u->var, 1, iv); break;  // int, bool, samplers
        }
    }
    for (int i = 0; i < p->nblocks; ++i) {
        struct block *b = &p->blocks[i];
        GLint binding = 0;
        r_GetActiveUniformBlockiv(p->id, b->orig_index, GL_UNIFORM_BLOCK_BINDING, &binding);
        if (binding != b->binding) {
            r_UniformBlockBinding(p->twin, b->var_index, (GLuint)binding);
            b->binding = binding;
        }
    }
}

// ---------------------------------------------------------------- per-thread state (a GL context is per thread)
#define FB_UNKNOWN (-1)
#define FB_NO_COLOR (-2)
static __thread struct prog *cur_prog;
static __thread int fb_views = FB_UNKNOWN;  // views of the draw framebuffer's first color attachment (cached)

static int draw_fb_views(void) {
    if (fb_views != FB_UNKNOWN) return fb_views;
    GLint fb = 0;
    r_GetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &fb);
    if (!fb) return fb_views = 0;  // the window surface
    GLint type = GL_NONE;
    r_GetFramebufferAttachmentParameteriv(GL_DRAW_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                                          GL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE, &type);
    if (type == GL_NONE) return fb_views = FB_NO_COLOR;  // depth-only: Mesa doesn't compare views
    if (type != GL_TEXTURE) return fb_views = 0;  // renderbuffer
    GLint views = 0;
    r_GetFramebufferAttachmentParameteriv(GL_DRAW_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                                          GL_FRAMEBUFFER_ATTACHMENT_TEXTURE_NUM_VIEWS_OVR, &views);
    return fb_views = views;
}

// ---------------------------------------------------------------- statistics (per 5 s)
static unsigned long twin_draws, mv_draws;
static struct timespec last_stats;
static void stats(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    if (!last_stats.tv_sec) last_stats = now;
    if (now.tv_sec - last_stats.tv_sec < 5) return;
    last_stats = now;
    if (twin_draws || debug)
        LOG("5s: %lu single-view draws of multiview programs, %lu multiview draws, %lu twin uniform updates",
            twin_draws, mv_draws, uniform_sets);
    twin_draws = mv_draws = uniform_sets = 0;
}

// Before a draw: bind the twin when the program is multiview and the framebuffer has no views. 1 = twin bound.
static int begin_draw(void) {
    struct prog *p = cur_prog;
    if (!p || !p->multiview || p->twin_failed) return 0;
    int views = draw_fb_views();
    if (views != 0) {
        ++mv_draws;
        if (debug) stats();
        return 0;
    }
    if (!p->twin && !build_twin(p)) return 0;
    r_UseProgram(p->twin);
    sync_uniforms(p);
    if (debug) r_GetError();  // (debug only: this consumes an earlier error the game hasn't read)
    return 1;
}

static void end_draw(const char *what) {
    struct prog *p = cur_prog;
    if (debug) {
        static int logs;
        GLenum err = r_GetError();
        if (err && logs < 20) {
            ++logs;
            LOG("%s with the twin of program %u: GL error 0x%x", what, p ? p->id : 0, err);
        }
    }
    r_UseProgram(p ? p->id : 0);
    ++twin_draws;
    stats();
}

// ---------------------------------------------------------------- exported GL functions
EXPORT void glUseProgram(GLuint program) {
    INIT();
    r_UseProgram(program);
    cur_prog = program ? find_prog(program, 0) : NULL;
}

EXPORT void glLinkProgram(GLuint program) {
    INIT();
    note_program(program);
    r_LinkProgram(program);
}

EXPORT void glDeleteProgram(GLuint program) {
    INIT();
    struct prog *p = program ? find_prog(program, 0) : NULL;
    if (p) {  // the record stays (pointers to it may be cached); a new program with this name relinks it
        drop_twin(p);
        drop_sources(p);
    }
    r_DeleteProgram(program);
}

EXPORT void glBindFramebuffer(GLenum target, GLuint framebuffer) {
    INIT();
    r_BindFramebuffer(target, framebuffer);
    if (target != GL_READ_FRAMEBUFFER) fb_views = FB_UNKNOWN;
}

EXPORT void glDeleteFramebuffers(GLsizei n, const GLuint *framebuffers) {
    INIT();
    r_DeleteFramebuffers(n, framebuffers);
    fb_views = FB_UNKNOWN;
}

EXPORT void glFramebufferTexture2D(GLenum target, GLenum attachment, GLenum textarget, GLuint texture, GLint level) {
    INIT();
    r_FramebufferTexture2D(target, attachment, textarget, texture, level);
    fb_views = FB_UNKNOWN;
}

EXPORT void glFramebufferRenderbuffer(GLenum target, GLenum attachment, GLenum rbtarget, GLuint renderbuffer) {
    INIT();
    r_FramebufferRenderbuffer(target, attachment, rbtarget, renderbuffer);
    fb_views = FB_UNKNOWN;
}

EXPORT void glFramebufferTextureLayer(GLenum target, GLenum attachment, GLuint texture, GLint level, GLint layer) {
    INIT();
    r_FramebufferTextureLayer(target, attachment, texture, level, layer);
    fb_views = FB_UNKNOWN;
}

EXPORT void glFramebufferTexture(GLenum target, GLenum attachment, GLuint texture, GLint level) {
    INIT();
    if (r_FramebufferTexture) r_FramebufferTexture(target, attachment, texture, level);
    fb_views = FB_UNKNOWN;
}

EXPORT void glDrawBuffers(GLsizei n, const GLenum *bufs) {
    INIT();
    r_DrawBuffers(n, bufs);
    fb_views = FB_UNKNOWN;
}

static void w_ftmv(GLenum target, GLenum attachment, GLuint texture, GLint level, GLint base, GLsizei views) {
    r_ftmv(target, attachment, texture, level, base, views);
    fb_views = FB_UNKNOWN;
}

static void w_ftmsmv(GLenum target, GLenum attachment, GLuint texture, GLint level, GLsizei samples, GLint base,
                     GLsizei views) {
    r_ftmsmv(target, attachment, texture, level, samples, base, views);
    fb_views = FB_UNKNOWN;
}

#define DRAW(name, call)                \
    INIT();                             \
    if (!r_##name) return;              \
    if (begin_draw()) {                 \
        r_##name call;                  \
        end_draw("gl" #name);           \
    } else {                            \
        r_##name call;                  \
    }

EXPORT void glDrawArrays(GLenum mode, GLint first, GLsizei count) { DRAW(DrawArrays, (mode, first, count)) }
EXPORT void glDrawElements(GLenum mode, GLsizei count, GLenum type, const void *indices) {
    DRAW(DrawElements, (mode, count, type, indices))
}
EXPORT void glDrawArraysInstanced(GLenum mode, GLint first, GLsizei count, GLsizei instances) {
    DRAW(DrawArraysInstanced, (mode, first, count, instances))
}
EXPORT void glDrawElementsInstanced(GLenum mode, GLsizei count, GLenum type, const void *indices, GLsizei instances) {
    DRAW(DrawElementsInstanced, (mode, count, type, indices, instances))
}
EXPORT void glDrawRangeElements(GLenum mode, GLuint start, GLuint end, GLsizei count, GLenum type,
                                const void *indices) {
    DRAW(DrawRangeElements, (mode, start, end, count, type, indices))
}
EXPORT void glDrawArraysIndirect(GLenum mode, const void *indirect) { DRAW(DrawArraysIndirect, (mode, indirect)) }
EXPORT void glDrawElementsIndirect(GLenum mode, GLenum type, const void *indirect) {
    DRAW(DrawElementsIndirect, (mode, type, indirect))
}
EXPORT void glDrawElementsBaseVertex(GLenum mode, GLsizei count, GLenum type, const void *indices, GLint base) {
    DRAW(DrawElementsBaseVertex, (mode, count, type, indices, base))
}
EXPORT void glDrawRangeElementsBaseVertex(GLenum mode, GLuint start, GLuint end, GLsizei count, GLenum type,
                                          const void *indices, GLint base) {
    DRAW(DrawRangeElementsBaseVertex, (mode, start, end, count, type, indices, base))
}
EXPORT void glDrawElementsInstancedBaseVertex(GLenum mode, GLsizei count, GLenum type, const void *indices,
                                              GLsizei instances, GLint base) {
    DRAW(DrawElementsInstancedBaseVertex, (mode, count, type, indices, instances, base))
}

// A context made current on this thread (engines with a render thread move theirs between threads, e.g. Doom3Quest's
// GLimp_ActivateContext): what this thread knew about the bound program and framebuffer may be another context's.
EXPORT EGLBoolean eglMakeCurrent(EGLDisplay dpy, EGLSurface draw, EGLSurface read, EGLContext ctx) {
    INIT();
    if (!r_eglMakeCurrent) return EGL_FALSE;
    EGLBoolean ok = r_eglMakeCurrent(dpy, draw, read, ctx);
    fb_views = FB_UNKNOWN;
    cur_prog = NULL;
    if (ok && ctx != EGL_NO_CONTEXT && r_GetIntegerv) {
        GLint program = 0;
        r_GetIntegerv(GL_CURRENT_PROGRAM, &program);
        cur_prog = program > 0 ? find_prog((GLuint)program, 0) : NULL;
    }
    return ok;
}

// Engines fetch the OVR multiview functions (and SDL-style loaders every function) through eglGetProcAddress.
EXPORT __eglMustCastToProperFunctionPointerType eglGetProcAddress(const char *name) {
    INIT();
    __eglMustCastToProperFunctionPointerType fn = r_eglGetProcAddress ? r_eglGetProcAddress(name) : NULL;
    if (!fn || !name) return fn;
    if (!strcmp(name, "glFramebufferTextureMultiviewOVR")) {
        if (!r_ftmv) r_ftmv = (PFN_FTMV)fn;
        return (__eglMustCastToProperFunctionPointerType)w_ftmv;
    }
    if (!strcmp(name, "glFramebufferTextureMultisampleMultiviewOVR")) {
        if (!r_ftmsmv) r_ftmsmv = (PFN_FTMSMV)fn;
        return (__eglMustCastToProperFunctionPointerType)w_ftmsmv;
    }
#define OWN(sym) if (!strcmp(name, #sym)) return (__eglMustCastToProperFunctionPointerType)sym;
    OWN(glUseProgram) OWN(glLinkProgram) OWN(glDeleteProgram) OWN(glBindFramebuffer) OWN(glDeleteFramebuffers)
    OWN(glFramebufferTexture2D) OWN(glFramebufferRenderbuffer) OWN(glFramebufferTextureLayer)
    OWN(glFramebufferTexture) OWN(glDrawBuffers) OWN(glDrawArrays) OWN(glDrawElements) OWN(glDrawArraysInstanced)
    OWN(glDrawElementsInstanced) OWN(glDrawRangeElements) OWN(glDrawArraysIndirect) OWN(glDrawElementsIndirect)
    OWN(glDrawElementsBaseVertex) OWN(glDrawRangeElementsBaseVertex) OWN(glDrawElementsInstancedBaseVertex)
    OWN(eglMakeCurrent)
#undef OWN
    return fn;
}
