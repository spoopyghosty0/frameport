// Stand-in OpenGL ES library for the host test of native/glmv/glmv.c (tests/test_gl_multiview_fbo.py). Just enough GL
// state for the interposer: shaders, programs (uniforms, attributes and blocks parsed from simple declarations),
// framebuffers with single-view or multiview color attachments, and OVR_multiview's draw rule as Mesa enforces it: a
// draw whose program declares a different number of views than the draw framebuffer's color attachment has is
// dropped with GL_INVALID_OPERATION. glmv_fake_state() lets the test look inside.
#include <GLES3/gl32.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define EXPORT __attribute__((visibility("default")))
#define MAXU 16

struct uniform {
    char name[64];
    GLenum type;
    int size, comps;
    GLuint value[4][16];  // per array element
};
struct shader {
    int used;
    GLenum type;
    char *src;
    int compiled;
};
struct program {
    int used, deleted, linked, views;
    GLuint shaders[4];
    int nshaders;
    char bound_names[8][64];
    GLint bound_locs[8];
    int nbound;
    char attribs[8][64];
    GLint attrib_locs[8];
    int nattribs;
    struct uniform u[MAXU];
    int nu;
    char blocks[4][64];
    GLint block_binding[4];
    int nblocks;
    char vs[4096];  // the vertex shader at link time
};
struct fb {
    int used;
    GLenum color_type;  // GL_NONE, GL_TEXTURE, GL_RENDERBUFFER
    int views;
};

EXPORT struct glmv_fake_state {
    unsigned accepted, rejected;
    GLuint last_program;  // program of the last accepted draw
    GLfloat last_color[4];  // its u_color
    GLint last_tex;  // its u_tex
    int live_programs;
} glmv_fake;

static struct shader shaders[64];
static struct program progs[64];
static struct fb fbs[16];
static GLuint current, draw_fb;
static GLenum error;

EXPORT struct glmv_fake_state *glmv_fake_state(void) { return &glmv_fake; }
EXPORT const char *glmv_fake_vertex_source(GLuint p) { return p < 64 ? progs[p].vs : ""; }
EXPORT GLint glmv_fake_attrib_location(GLuint p, const char *name) {
    for (int i = 0; p < 64 && i < progs[p].nattribs; ++i)
        if (!strcmp(progs[p].attribs[i], name)) return progs[p].attrib_locs[i];
    return -1;
}

static void set_error(GLenum e) { if (!error) error = e; }
EXPORT GLenum glGetError(void) { GLenum e = error; error = 0; return e; }

// ------------------------------------------------------------------ shaders
EXPORT GLuint glCreateShader(GLenum type) {
    for (GLuint i = 1; i < 64; ++i)
        if (!shaders[i].used) { shaders[i] = (struct shader){.used = 1, .type = type}; return i; }
    return 0;
}
EXPORT void glShaderSource(GLuint s, GLsizei n, const GLchar *const *strs, const GLint *lens) {
    size_t total = 0;
    for (GLsizei i = 0; i < n; ++i) total += lens && lens[i] >= 0 ? (size_t)lens[i] : strlen(strs[i]);
    free(shaders[s].src);
    shaders[s].src = calloc(total + 1, 1);
    for (GLsizei i = 0, off = 0; i < n; ++i) {
        size_t k = lens && lens[i] >= 0 ? (size_t)lens[i] : strlen(strs[i]);
        memcpy(shaders[s].src + off, strs[i], k);
        off += (GLsizei)k;
    }
}
EXPORT void glCompileShader(GLuint s) { shaders[s].compiled = !strstr(shaders[s].src, "COMPILE_ERROR"); }
EXPORT void glGetShaderiv(GLuint s, GLenum pname, GLint *v) {
    if (pname == GL_COMPILE_STATUS) *v = shaders[s].compiled;
    else if (pname == GL_SHADER_TYPE) *v = (GLint)shaders[s].type;
    else if (pname == GL_SHADER_SOURCE_LENGTH) *v = shaders[s].src ? (GLint)strlen(shaders[s].src) + 1 : 0;
}
EXPORT void glGetShaderSource(GLuint s, GLsizei max, GLsizei *len, GLchar *out) {
    GLsizei n = (GLsizei)snprintf(out, (size_t)max, "%s", shaders[s].src ? shaders[s].src : "");
    if (len) *len = n < max ? n : max - 1;
}
EXPORT void glGetShaderInfoLog(GLuint s, GLsizei max, GLsizei *len, GLchar *out) {
    (void)s;
    GLsizei n = (GLsizei)snprintf(out, (size_t)max, "0:1(1): error: fake");
    if (len) *len = n;
}
EXPORT void glDeleteShader(GLuint s) { (void)s; }

// ------------------------------------------------------------------ programs
EXPORT GLuint glCreateProgram(void) {
    for (GLuint i = 1; i < 64; ++i)
        if (!progs[i].used) { progs[i] = (struct program){.used = 1}; ++glmv_fake.live_programs; return i; }
    return 0;
}
EXPORT void glAttachShader(GLuint p, GLuint s) { if (progs[p].nshaders < 4) progs[p].shaders[progs[p].nshaders++] = s; }
EXPORT void glGetAttachedShaders(GLuint p, GLsizei max, GLsizei *n, GLuint *out) {
    GLsizei k = 0;
    for (; k < progs[p].nshaders && k < max; ++k) out[k] = progs[p].shaders[k];
    if (n) *n = k;
}
EXPORT void glBindAttribLocation(GLuint p, GLuint loc, const GLchar *name) {
    struct program *pr = &progs[p];
    if (pr->nbound < 8) {
        snprintf(pr->bound_names[pr->nbound], 64, "%s", name);
        pr->bound_locs[pr->nbound++] = (GLint)loc;
    }
}

static int type_of(const char *t, GLenum *type) {
    static const struct { const char *name; GLenum type; int comps; } types[] = {
        {"float", GL_FLOAT, 1}, {"vec2", GL_FLOAT_VEC2, 2}, {"vec3", GL_FLOAT_VEC3, 3}, {"vec4", GL_FLOAT_VEC4, 4},
        {"mat4", GL_FLOAT_MAT4, 16}, {"int", GL_INT, 1}, {"uint", GL_UNSIGNED_INT, 1}, {"sampler2D", GL_SAMPLER_2D, 1},
    };
    for (size_t i = 0; i < sizeof(types) / sizeof(types[0]); ++i)
        if (!strcmp(t, types[i].name)) { *type = types[i].type; return types[i].comps; }
    return 0;
}

// "uniform [precision] type name[N];" / "in [precision] type name;" -> type, name, array size
static int declaration(const char *line, const char *kw, GLenum *type, int *comps, char *name, int *size) {
    char w[4][64] = {{0}};
    int n = sscanf(line, "%63s %63s %63s %63s", w[0], w[1], w[2], w[3]);
    if (n < 3 || strcmp(w[0], kw)) return 0;
    int t = !strcmp(w[1], "lowp") || !strcmp(w[1], "mediump") || !strcmp(w[1], "highp") ? 2 : 1;
    if (t + 1 >= n) return 0;
    if (!(*comps = type_of(w[t], type))) return 0;
    snprintf(name, 64, "%s", w[t + 1]);
    char *end = strpbrk(name, "[;");
    *size = 1;
    if (end && *end == '[') *size = end[1] >= '1' && end[1] <= '4' ? end[1] - '0' : 2;  // NUM_VIEWS -> 2
    if (end) *end = 0;
    return 1;
}

static void parse(struct program *pr, const char *src, int vertex) {
    const char *p = src;
    int in_block = 0;
    while (*p) {
        const char *e = strchr(p, '\n');
        size_t len = e ? (size_t)(e - p) : strlen(p);
        char line[256];
        snprintf(line, sizeof(line), "%.*s", (int)(len < 255 ? len : 255), p);
        char *l = line;
        while (*l == ' ' || *l == '\t') ++l;
        GLenum type;
        int comps, size;
        char name[64];
        if (in_block) {
            if (*l == '}') in_block = 0;
        } else if (!strncmp(l, "layout(shared) uniform ", 23) && !strchr(l, ';')) {
            if (pr->nblocks < 4) sscanf(l + 23, "%63s", pr->blocks[pr->nblocks++]);
            in_block = 1;
        } else if (declaration(l, "uniform", &type, &comps, name, &size)) {
            int dup = 0;
            for (int i = 0; i < pr->nu; ++i) dup |= !strcmp(pr->u[i].name, name);
            if (!dup && pr->nu < MAXU) {
                struct uniform *u = &pr->u[pr->nu++];
                memset(u, 0, sizeof(*u));
                snprintf(u->name, 64, "%s", name);
                u->type = type;
                u->size = size;
                u->comps = comps;
            }
        } else if (vertex && declaration(l, "in", &type, &comps, name, &size) && pr->nattribs < 8) {
            GLint loc = -1;
            for (int i = 0; i < pr->nbound; ++i)
                if (!strcmp(pr->bound_names[i], name)) loc = pr->bound_locs[i];
            if (loc < 0) loc = 10 + pr->nattribs;
            snprintf(pr->attribs[pr->nattribs], 64, "%s", name);
            pr->attrib_locs[pr->nattribs++] = loc;
        }
        p += len + (e ? 1 : 0);
    }
}

EXPORT void glLinkProgram(GLuint p) {
    struct program *pr = &progs[p];
    pr->nu = pr->nattribs = pr->nblocks = 0;
    pr->views = 0;
    pr->linked = 1;
    pr->vs[0] = 0;
    for (int i = 0; i < pr->nshaders; ++i) {
        struct shader *s = &shaders[pr->shaders[i]];
        if (!s->compiled || strstr(s->src, "LINK_ERROR")) pr->linked = 0;
        int vertex = s->type == GL_VERTEX_SHADER;
        if (vertex) {
            snprintf(pr->vs, sizeof(pr->vs), "%s", s->src);
            if (strstr(s->src, "layout(num_views")) pr->views = 2;
        }
        parse(pr, s->src, vertex);
    }
}
EXPORT void glGetProgramiv(GLuint p, GLenum pname, GLint *v) {
    struct program *pr = &progs[p];
    if (pname == GL_LINK_STATUS) *v = pr->linked;
    else if (pname == GL_ACTIVE_UNIFORMS) *v = pr->nu;
    else if (pname == GL_ACTIVE_ATTRIBUTES) *v = pr->nattribs;
    else if (pname == GL_ACTIVE_UNIFORM_BLOCKS) *v = pr->nblocks;
}
EXPORT void glGetProgramInfoLog(GLuint p, GLsizei max, GLsizei *len, GLchar *out) { glGetShaderInfoLog(p, max, len, out); }
EXPORT void glUseProgram(GLuint p) { current = p; }
EXPORT void glDeleteProgram(GLuint p) {
    if (p && progs[p].used && !progs[p].deleted) { progs[p].deleted = 1; progs[p].used = 0; --glmv_fake.live_programs; }
}
EXPORT void glGetActiveAttrib(GLuint p, GLuint i, GLsizei max, GLsizei *len, GLint *size, GLenum *type, GLchar *name) {
    GLsizei n = (GLsizei)snprintf(name, (size_t)max, "%s", progs[p].attribs[i]);
    if (len) *len = n;
    *size = 1;
    *type = GL_FLOAT_VEC4;
}
EXPORT GLint glGetAttribLocation(GLuint p, const GLchar *name) { return glmv_fake_attrib_location(p, name); }
EXPORT void glGetActiveUniform(GLuint p, GLuint i, GLsizei max, GLsizei *len, GLint *size, GLenum *type, GLchar *name) {
    struct uniform *u = &progs[p].u[i];
    GLsizei n = (GLsizei)snprintf(name, (size_t)max, u->size > 1 ? "%s[0]" : "%s", u->name);
    if (len) *len = n;
    *size = u->size;
    *type = u->type;
}
EXPORT void glGetActiveUniformsiv(GLuint p, GLsizei n, const GLuint *idx, GLenum pname, GLint *out) {
    (void)p;
    (void)idx;
    for (GLsizei i = 0; i < n; ++i) out[i] = pname == GL_UNIFORM_BLOCK_INDEX ? -1 : 0;
}
EXPORT GLint glGetUniformLocation(GLuint p, const GLchar *name) {
    for (int i = 0; i < progs[p].nu; ++i) {
        struct uniform *u = &progs[p].u[i];
        size_t n = strlen(u->name);
        if (strncmp(name, u->name, n)) continue;
        if (!name[n]) return i * 4;
        if (name[n] == '[' && name[n + 1] >= '0' && name[n + 1] < '0' + u->size && name[n + 2] == ']' && !name[n + 3])
            return i * 4 + (name[n + 1] - '0');
    }
    return -1;
}
static GLuint *value(GLuint p, GLint loc, int *comps) {
    if (!p || loc < 0 || loc / 4 >= progs[p].nu) { set_error(GL_INVALID_OPERATION); return NULL; }
    struct uniform *u = &progs[p].u[loc / 4];
    *comps = u->comps;
    return u->value[loc % 4];
}
EXPORT void glGetUniformfv(GLuint p, GLint loc, GLfloat *out) {
    int c;
    GLuint *v = value(p, loc, &c);
    if (v) memcpy(out, v, (size_t)c * 4);
}
EXPORT void glGetUniformiv(GLuint p, GLint loc, GLint *out) { glGetUniformfv(p, loc, (GLfloat *)out); }
EXPORT void glGetUniformuiv(GLuint p, GLint loc, GLuint *out) { glGetUniformfv(p, loc, (GLfloat *)out); }
static void set_uniform(GLint loc, const void *data, int comps) {
    int c;
    GLuint *v = value(current, loc, &c);
    if (v && c == comps) memcpy(v, data, (size_t)c * 4);
    else if (v) set_error(GL_INVALID_OPERATION);
}
EXPORT void glUniform1fv(GLint l, GLsizei n, const GLfloat *v) { (void)n; set_uniform(l, v, 1); }
EXPORT void glUniform2fv(GLint l, GLsizei n, const GLfloat *v) { (void)n; set_uniform(l, v, 2); }
EXPORT void glUniform3fv(GLint l, GLsizei n, const GLfloat *v) { (void)n; set_uniform(l, v, 3); }
EXPORT void glUniform4fv(GLint l, GLsizei n, const GLfloat *v) { (void)n; set_uniform(l, v, 4); }
EXPORT void glUniform1iv(GLint l, GLsizei n, const GLint *v) { (void)n; set_uniform(l, v, 1); }
EXPORT void glUniform1i(GLint l, GLint v) { set_uniform(l, &v, 1); }
EXPORT void glUniform1uiv(GLint l, GLsizei n, const GLuint *v) { (void)n; set_uniform(l, v, 1); }
EXPORT void glUniformMatrix4fv(GLint l, GLsizei n, GLboolean t, const GLfloat *v) { (void)n; (void)t; set_uniform(l, v, 16); }
EXPORT void glGetActiveUniformBlockName(GLuint p, GLuint i, GLsizei max, GLsizei *len, GLchar *name) {
    GLsizei n = (GLsizei)snprintf(name, (size_t)max, "%s", progs[p].blocks[i]);
    if (len) *len = n;
}
EXPORT void glGetActiveUniformBlockiv(GLuint p, GLuint i, GLenum pname, GLint *v) {
    if (pname == GL_UNIFORM_BLOCK_BINDING) *v = progs[p].block_binding[i];
}
EXPORT GLuint glGetUniformBlockIndex(GLuint p, const GLchar *name) {
    for (int i = 0; i < progs[p].nblocks; ++i)
        if (!strcmp(progs[p].blocks[i], name)) return (GLuint)i;
    return GL_INVALID_INDEX;
}
EXPORT void glUniformBlockBinding(GLuint p, GLuint i, GLuint b) { progs[p].block_binding[i] = (GLint)b; }

// ------------------------------------------------------------------ framebuffers
EXPORT void glGenFramebuffers(GLsizei n, GLuint *out) {
    for (GLsizei k = 0; k < n; ++k)
        for (GLuint i = 1; i < 16; ++i)
            if (!fbs[i].used) { fbs[i] = (struct fb){.used = 1}; out[k] = i; break; }
}
EXPORT void glBindFramebuffer(GLenum target, GLuint fb) { if (target != GL_READ_FRAMEBUFFER) draw_fb = fb; }
EXPORT void glDeleteFramebuffers(GLsizei n, const GLuint *ids) {
    for (GLsizei k = 0; k < n; ++k) {
        fbs[ids[k]].used = 0;
        if (draw_fb == ids[k]) draw_fb = 0;
    }
}
static void attach(GLenum attachment, GLenum type, int views) {
    if (attachment == GL_COLOR_ATTACHMENT0 && draw_fb) { fbs[draw_fb].color_type = type; fbs[draw_fb].views = views; }
}
EXPORT void glFramebufferTexture2D(GLenum t, GLenum a, GLenum tt, GLuint tex, GLint l) {
    (void)t; (void)tt; (void)l;
    attach(a, tex ? GL_TEXTURE : GL_NONE, 0);
}
EXPORT void glFramebufferTextureLayer(GLenum t, GLenum a, GLuint tex, GLint l, GLint layer) {
    (void)t; (void)l; (void)layer;
    attach(a, tex ? GL_TEXTURE : GL_NONE, 0);
}
EXPORT void glFramebufferTexture(GLenum t, GLenum a, GLuint tex, GLint l) { (void)t; (void)l; attach(a, tex ? GL_TEXTURE : GL_NONE, 0); }
EXPORT void glFramebufferRenderbuffer(GLenum t, GLenum a, GLenum rt, GLuint rb) {
    (void)t; (void)rt;
    attach(a, rb ? GL_RENDERBUFFER : GL_NONE, 0);
}
static void ftmv(GLenum t, GLenum a, GLuint tex, GLint l, GLint base, GLsizei views) {
    (void)t; (void)l; (void)base;
    attach(a, tex ? GL_TEXTURE : GL_NONE, views);
}
EXPORT void glDrawBuffers(GLsizei n, const GLenum *b) { (void)n; (void)b; }
EXPORT void glGetIntegerv(GLenum pname, GLint *v) {
    if (pname == GL_DRAW_FRAMEBUFFER_BINDING) *v = (GLint)draw_fb;
    else if (pname == GL_CURRENT_PROGRAM) *v = (GLint)current;
}
EXPORT void glGetFramebufferAttachmentParameteriv(GLenum t, GLenum a, GLenum pname, GLint *v) {
    (void)t;
    if (!draw_fb || a != GL_COLOR_ATTACHMENT0) { set_error(GL_INVALID_OPERATION); return; }
    struct fb *f = &fbs[draw_fb];
    if (pname == GL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE) *v = (GLint)f->color_type;
    else if (pname == 0x9630 && f->color_type == GL_TEXTURE) *v = f->views;  // ..._TEXTURE_NUM_VIEWS_OVR
    else set_error(GL_INVALID_ENUM);
}

// ------------------------------------------------------------------ draws (OVR_multiview's rule, as in Mesa)
static void draw(void) {
    struct program *pr = &progs[current];
    int has_color = !draw_fb || fbs[draw_fb].color_type != GL_NONE;
    int fb_views = draw_fb ? fbs[draw_fb].views : 0;
    if (!current || !pr->linked || (has_color && pr->views != fb_views)) {
        ++glmv_fake.rejected;
        set_error(GL_INVALID_OPERATION);
        return;
    }
    ++glmv_fake.accepted;
    glmv_fake.last_program = current;
    GLint lc = glGetUniformLocation(current, "u_color"), lt = glGetUniformLocation(current, "u_tex");
    if (lc >= 0) glGetUniformfv(current, lc, glmv_fake.last_color);
    if (lt >= 0) glGetUniformiv(current, lt, &glmv_fake.last_tex);
}
EXPORT void glDrawArrays(GLenum m, GLint f, GLsizei c) { (void)m; (void)f; (void)c; draw(); }
EXPORT void glDrawElements(GLenum m, GLsizei c, GLenum t, const void *i) { (void)m; (void)c; (void)t; (void)i; draw(); }

// one context, current everywhere: eglMakeCurrent only has to exist
EXPORT unsigned glmv_fake_make_current_calls;
EXPORT unsigned eglMakeCurrent(void *dpy, void *draw, void *read, void *ctx) {
    (void)dpy; (void)draw; (void)read; (void)ctx;
    ++glmv_fake_make_current_calls;
    return 1;
}

// eglGetProcAddress: the stand-in's own functions (looked up in this library only, never the interposer's)
typedef void (*fnptr)(void);
EXPORT fnptr eglGetProcAddress(const char *name) {
    if (!strcmp(name, "glFramebufferTextureMultiviewOVR")) return (fnptr)ftmv;
    static void *self;
    if (!self) self = dlopen("libglmv_fake_gles.so", RTLD_NOW | RTLD_NOLOAD);
    return self ? (fnptr)dlsym(self, name) : NULL;
}
