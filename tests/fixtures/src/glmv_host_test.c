// Host test of native/glmv/glmv.c against a stand-in GL (glmv_fake_gles.c), driven like Doom3Quest drives GL: every
// function from dlsym on the library's handle (its qgl* table), multiview attachments through eglGetProcAddress.
// Usage: glmv_host_test <path of the interposer built for the host>
#define GL_GLES_PROTOTYPES 0  // every function is a pointer from dlsym below
#include <GLES3/gl32.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>

struct glmv_fake_state {
    unsigned accepted, rejected;
    GLuint last_program;
    GLfloat last_color[4];
    GLint last_tex;
    int live_programs;
};

static int failures;
#define CHECK(cond, ...)                                         \
    do {                                                         \
        if (!(cond)) {                                           \
            ++failures;                                          \
            fprintf(stderr, "FAIL line %d: ", __LINE__);         \
            fprintf(stderr, __VA_ARGS__);                        \
            fputc('\n', stderr);                                 \
        }                                                        \
    } while (0)

static const char *VS =
    "#version 300 es\n"
    "// Multiview\n"
    "#define NUM_VIEWS 2\n"
    "#extension GL_OVR_multiview2 : enable\n"
    "layout(num_views=NUM_VIEWS) in;\n"
    "precision mediump float;\n"
    "in highp vec4 attr_Vertex;\n"
    "in lowp vec4 attr_Color;\n"
    "layout(shared) uniform ViewMatrices\n"
    "{\n"
    "    uniform highp mat4 u_viewMatrices[NUM_VIEWS];\n"
    "};\n"
    "uniform highp mat4 u_modelMatrix;\n"
    "uniform lowp vec4 u_color;\n"
    "out lowp vec4 var_Color;\n"
    "void main() {\n"
    "  var_Color = attr_Color * u_color;\n"
    "  gl_Position = u_viewMatrices[gl_ViewID_OVR] * (u_modelMatrix * attr_Vertex);\n"
    "}\n";
static const char *FS =
    "#version 300 es\n"
    "precision mediump float;\n"
    "uniform sampler2D u_tex;\n"
    "uniform lowp vec4 u_color;\n"
    "in lowp vec4 var_Color;\n"
    "out vec4 fragColor;\n"
    "void main() { fragColor = texture(u_tex, vec2(0.5)) * var_Color * u_color; }\n";
static const char *FLAT_VS =
    "#version 300 es\n"
    "in highp vec4 attr_Vertex;\n"
    "uniform lowp vec4 u_color;\n"
    "void main() { gl_Position = attr_Vertex * u_color; }\n";

#define FN(ret, name, args) static ret(*name) args;
FN(GLuint, glCreateShader, (GLenum))
FN(void, glShaderSource, (GLuint, GLsizei, const GLchar *const *, const GLint *))
FN(void, glCompileShader, (GLuint))
FN(GLuint, glCreateProgram, (void))
FN(void, glAttachShader, (GLuint, GLuint))
FN(void, glBindAttribLocation, (GLuint, GLuint, const GLchar *))
FN(void, glLinkProgram, (GLuint))
FN(void, glUseProgram, (GLuint))
FN(void, glDeleteProgram, (GLuint))
FN(GLint, glGetUniformLocation, (GLuint, const GLchar *))
FN(void, glUniform4fv, (GLint, GLsizei, const GLfloat *))
FN(void, glUniform1i, (GLint, GLint))
FN(GLuint, glGetUniformBlockIndex, (GLuint, const GLchar *))
FN(void, glUniformBlockBinding, (GLuint, GLuint, GLuint))
FN(void, glGetActiveUniformBlockiv, (GLuint, GLuint, GLenum, GLint *))
FN(void, glGenFramebuffers, (GLsizei, GLuint *))
FN(void, glBindFramebuffer, (GLenum, GLuint))
FN(void, glFramebufferTexture2D, (GLenum, GLenum, GLenum, GLuint, GLint))
FN(void, glFramebufferRenderbuffer, (GLenum, GLenum, GLenum, GLuint))
FN(void, glDrawElements, (GLenum, GLsizei, GLenum, const void *))
FN(void, glDrawArrays, (GLenum, GLint, GLsizei))
FN(GLenum, glGetError, (void))
FN(void, glGetIntegerv, (GLenum, GLint *))
FN(void *, eglGetProcAddress, (const char *))
FN(unsigned, eglMakeCurrent, (void *, void *, void *, void *))
FN(struct glmv_fake_state *, glmv_fake_state, (void))
FN(const char *, glmv_fake_vertex_source, (GLuint))
FN(GLint, glmv_fake_attrib_location, (GLuint, const char *))
static void (*glFramebufferTextureMultiviewOVR)(GLenum, GLenum, GLuint, GLint, GLint, GLsizei);

// a render thread that takes over the context (Doom3Quest's GLimp_ActivateContext) and draws the HUD at once
static void *render_thread(void *arg) {
    (void)arg;
    eglMakeCurrent((void *)1, NULL, NULL, (void *)1);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    return NULL;
}

static GLuint program(const char *vs, const char *fs) {
    GLuint v = glCreateShader(GL_VERTEX_SHADER), f = glCreateShader(GL_FRAGMENT_SHADER), p = glCreateProgram();
    glShaderSource(v, 1, &vs, NULL);
    glShaderSource(f, 1, &fs, NULL);
    glCompileShader(v);
    glCompileShader(f);
    glAttachShader(p, v);
    glAttachShader(p, f);
    glBindAttribLocation(p, 3, "attr_Vertex");  // like Doom3Quest's ATTR_* slots
    glBindAttribLocation(p, 5, "attr_Color");
    glLinkProgram(p);
    return p;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    void *gl = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!gl) { fprintf(stderr, "%s\n", dlerror()); return 2; }
#define LOAD(name) *(void **)&name = dlsym(gl, #name); if (!name) { fprintf(stderr, "no %s\n", #name); return 2; }
    LOAD(glCreateShader) LOAD(glShaderSource) LOAD(glCompileShader) LOAD(glCreateProgram) LOAD(glAttachShader)
    LOAD(glBindAttribLocation) LOAD(glLinkProgram) LOAD(glUseProgram) LOAD(glDeleteProgram) LOAD(glGetUniformLocation)
    LOAD(glUniform4fv) LOAD(glUniform1i) LOAD(glGetUniformBlockIndex) LOAD(glUniformBlockBinding)
    LOAD(glGetActiveUniformBlockiv) LOAD(glGenFramebuffers) LOAD(glBindFramebuffer) LOAD(glFramebufferTexture2D)
    LOAD(glFramebufferRenderbuffer) LOAD(glDrawElements) LOAD(glDrawArrays) LOAD(glGetError) LOAD(glGetIntegerv)
    LOAD(eglGetProcAddress) LOAD(eglMakeCurrent) LOAD(glmv_fake_state) LOAD(glmv_fake_vertex_source) LOAD(glmv_fake_attrib_location)
    *(void **)&glFramebufferTextureMultiviewOVR = eglGetProcAddress("glFramebufferTextureMultiviewOVR");
    CHECK(glFramebufferTextureMultiviewOVR != NULL, "no glFramebufferTextureMultiviewOVR");
    if (!glFramebufferTextureMultiviewOVR) return 1;
    // the interposer's own functions, also through eglGetProcAddress (SDL-style loaders)
    CHECK(eglGetProcAddress("glUseProgram") == (void *)glUseProgram, "eglGetProcAddress(glUseProgram) not wrapped");
    struct glmv_fake_state *st = glmv_fake_state();

    GLuint prog = program(VS, FS);
    glUseProgram(prog);
    const GLfloat red[4] = {1, 0.5f, 0.25f, 1}, blue[4] = {0, 0, 1, 1};
    glUniform4fv(glGetUniformLocation(prog, "u_color"), 1, red);
    glUniform1i(glGetUniformLocation(prog, "u_tex"), 2);
    glUniformBlockBinding(prog, glGetUniformBlockIndex(prog, "ViewMatrices"), 1);
    int live = st->live_programs;

    GLuint fb[3];
    glGenFramebuffers(3, fb);
    // eye buffer: two-view multiview texture -> drawn by the program itself
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, fb[0]);
    glFramebufferTextureMultiviewOVR(GL_DRAW_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, 7, 0, 0, 2);
    glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_SHORT, NULL);
    CHECK(st->accepted == 1 && st->rejected == 0 && st->last_program == prog, "eye-buffer draw: %u/%u prog %u",
          st->accepted, st->rejected, st->last_program);
    CHECK(st->live_programs == live, "a twin was built for an eye-buffer draw");

    // HUD pool: a 2D texture -> the single-view twin draws, with the original's uniforms
    glBindFramebuffer(GL_FRAMEBUFFER, fb[1]);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, 8, 0);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, 9);
    glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_SHORT, NULL);
    GLuint twin = st->last_program;
    CHECK(st->accepted == 2 && st->rejected == 0, "HUD draw dropped: %u/%u", st->accepted, st->rejected);
    CHECK(twin && twin != prog && st->live_programs == live + 1, "HUD draw not by a twin (program %u)", twin);
    CHECK(!memcmp(st->last_color, red, sizeof(red)) && st->last_tex == 2, "twin uniforms not copied");
    const char *tvs = glmv_fake_vertex_source(twin);
    CHECK(!strstr(tvs, "num_views") && !strstr(tvs, "gl_ViewID_OVR") && strstr(tvs, "u_viewMatrices[(0u)"),
          "twin vertex shader not rewritten:\n%s", tvs);
    CHECK(glmv_fake_attrib_location(twin, "attr_Vertex") == 3 && glmv_fake_attrib_location(twin, "attr_Color") == 5,
          "attribute locations not copied");
    GLint binding = -1;
    glGetActiveUniformBlockiv(twin, glGetUniformBlockIndex(twin, "ViewMatrices"), GL_UNIFORM_BLOCK_BINDING, &binding);
    CHECK(binding == 1, "uniform block binding not copied (%d)", binding);
    GLint cur = 0;
    glGetIntegerv(GL_CURRENT_PROGRAM, &cur);
    CHECK((GLuint)cur == prog, "the original program isn't bound again after the draw (%d)", cur);
    CHECK(glGetError() == GL_NO_ERROR, "GL error after the twin draw");

    // the game changes a uniform (on the original): the next twin draw sees it; the twin is reused
    glUniform4fv(glGetUniformLocation(prog, "u_color"), 1, blue);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    CHECK(st->last_program == twin && !memcmp(st->last_color, blue, sizeof(blue)), "changed uniform not copied");
    CHECK(st->live_programs == live + 1, "twin rebuilt");

    // the context moves to another thread, which draws without binding anything first
    pthread_t thread;
    unsigned rejected = st->rejected;
    pthread_create(&thread, NULL, render_thread, NULL);
    pthread_join(thread, NULL);
    CHECK(st->rejected == rejected && st->last_program == twin, "draw after eglMakeCurrent on another thread: %u",
          st->last_program);

    // the window (FBO 0) has no views either
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    CHECK(st->last_program == twin && st->rejected == 0, "window draw: program %u", st->last_program);

    // back to the eye buffer (cached view count invalidated by the bind)
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER, fb[0]);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    CHECK(st->last_program == prog && st->rejected == 0, "eye draw after HUD: program %u", st->last_program);

    // a depth-only framebuffer: Mesa doesn't compare views without a color attachment
    glBindFramebuffer(GL_FRAMEBUFFER, fb[2]);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, 10);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    CHECK(st->last_program == prog && st->rejected == 0, "depth-only draw: program %u", st->last_program);
    CHECK(glGetError() == GL_NO_ERROR, "the interposer's framebuffer queries left a GL error");

    // a single-view program on the HUD: untouched
    GLuint flat = program(FLAT_VS, FS);
    glUseProgram(flat);
    glBindFramebuffer(GL_FRAMEBUFFER, fb[1]);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    CHECK(st->last_program == flat && st->live_programs == live + 2, "single-view program: %u", st->last_program);

    // deleting the original deletes its twin
    glDeleteProgram(prog);
    CHECK(st->live_programs == live, "twin left behind (%d programs)", st->live_programs);

    // without the interposer the stand-in drops the HUD draw, as Mesa does
    void *fake = dlopen("libglmv_fake_gles.so", RTLD_NOW | RTLD_NOLOAD);
    void (*real_use)(GLuint) = fake ? (void (*)(GLuint))dlsym(fake, "glUseProgram") : NULL;
    void (*real_draw)(GLenum, GLint, GLsizei) = fake ? (void (*)(GLenum, GLint, GLsizei))dlsym(fake, "glDrawArrays") : NULL;
    GLuint again = program(VS, FS);
    if (real_use && real_draw) {
        real_use(again);
        unsigned rejected = st->rejected;
        real_draw(GL_TRIANGLES, 0, 3);
        CHECK(st->rejected == rejected + 1, "the stand-in accepted a multiview draw into a 2D framebuffer");
    }

    if (failures) {
        fprintf(stderr, "%d failure(s)\n", failures);
        return 1;
    }
    printf("ok\n");
    return 0;
}
