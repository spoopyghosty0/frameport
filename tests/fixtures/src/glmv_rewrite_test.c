// Host test of native/glmv/glmv_rewrite.h (frame.gl_multiview_fbo): built and run by tests/test_gl_multiview_fbo.py.
// Arguments: an output directory for the rewritten shaders, then Doom3Quest's shader files (C++ raw strings R"(...)").
// Every vertex shader must lose its num_views layout and every gl_ViewID_OVR; everything else stays byte for byte
// (apart from the blanks that keep columns and line numbers).
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "glmv_rewrite.h"

static int failures;
#define CHECK(cond, ...)                                    \
    do {                                                    \
        if (!(cond)) {                                      \
            ++failures;                                     \
            fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__); \
            fprintf(stderr, __VA_ARGS__);                   \
            fputc('\n', stderr);                            \
        }                                                   \
    } while (0)

static char *read_file(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = malloc((size_t)n + 1);
    if (buf && fread(buf, 1, (size_t)n, f) != (size_t)n) { free(buf); buf = NULL; }
    if (buf) buf[n] = 0;
    fclose(f);
    return buf;
}

static int count_token(const char *s, const char *word) {
    int n = 0;
    for (const char *p = s; (p = strstr(p, word)); p += strlen(word)) n += glmv_token_at(s, p, word);
    return n;
}

static int count_char(const char *s, char c) {
    int n = 0;
    for (; *s; ++s) n += *s == c;
    return n;
}

// The rewrite only blanks the layout statement and swaps gl_ViewID_OVR for (0u): with both removed from the original
// too (the same way, by hand), the texts must be equal.
static void check_rest_unchanged(const char *name, const char *orig, const char *out) {
    size_t n = strlen(orig);
    for (size_t i = 0; i < n; ++i) {
        if (orig[i] == out[i]) continue;
        // inside a replaced gl_ViewID_OVR or a blanked layout statement
        int ok = 0;
        for (size_t back = 0; back <= i && back < 64 && !ok; ++back) {
            const char *p = orig + i - back;
            if (!strncmp(p, "gl_ViewID_OVR", 13) && back < 13) ok = 1;
            const char *semi = !strncmp(p, "layout", 6) ? glmv_num_views_stmt(orig, p) : NULL;
            if (semi && orig + i <= semi && out[i] == ' ') ok = 1;
        }
        CHECK(ok, "%s: unexpected change at offset %zu", name, i);
        if (!ok) return;
    }
}

static void check_shader(const char *outdir, const char *path) {
    char *file = read_file(path);
    CHECK(file != NULL, "can't read %s", path);
    if (!file) return;
    char *start = strstr(file, "R\"(");
    char *end = strstr(file, ")\";");
    CHECK(start && end && end > start, "%s: no raw string", path);
    if (!start || !end) { free(file); return; }
    start += 3;
    *end = 0;
    const char *name = strrchr(path, '/') ? strrchr(path, '/') + 1 : path;
    int vertex = strstr(name, "VP.") != NULL;

    CHECK(glmv_has_num_views(start) == vertex, "%s: num_views layout %s", name, vertex ? "not found" : "found");
    int ids_before = count_token(start, "gl_ViewID_OVR");
    int layouts = -1, ids = -1;
    char *out = glmv_rewrite(start, &layouts, &ids);
    CHECK(out != NULL, "%s: out of memory", name);
    if (!out) { free(file); return; }
    CHECK(!glmv_has_num_views(out), "%s: num_views layout left", name);
    CHECK(count_token(out, "gl_ViewID_OVR") == 0, "%s: gl_ViewID_OVR left", name);
    CHECK(!strstr(out, "num_views"), "%s: num_views text left", name);
    CHECK(layouts == (vertex ? 1 : 0), "%s: %d layouts removed", name, layouts);
    CHECK(ids == ids_before, "%s: %d of %d gl_ViewID_OVR replaced", name, ids, ids_before);
    CHECK(!vertex || ids >= 1, "%s: vertex shader without gl_ViewID_OVR", name);
    CHECK(strlen(out) == strlen(start), "%s: length changed", name);
    CHECK(count_char(out, '\n') == count_char(start, '\n'), "%s: line count changed", name);
    CHECK(strstr(out, "#version 300 es") != NULL, "%s: #version lost", name);
    CHECK(count_token(out, "GL_OVR_multiview2") == count_token(start, "GL_OVR_multiview2"),
          "%s: #extension lines changed", name);
    check_rest_unchanged(name, start, out);

    char dest[1024];
    snprintf(dest, sizeof(dest), "%s/%.*s.glsl", outdir, (int)(strrchr(name, '.') ? strrchr(name, '.') - name
                                                                          : (long)strlen(name)), name);
    FILE *f = fopen(dest, "wb");
    CHECK(f != NULL, "can't write %s", dest);
    if (f) { fputs(out, f); fclose(f); }
    free(out);
    free(file);
}

static void check_cases(void) {
    struct { const char *src, *want; int layouts, ids; } cases[] = {
        // spacing, newlines inside the statement (kept), other qualifiers before num_views
        {"layout ( num_views = 2 )\n in ;\nvoid main(){}", "                        \n     \nvoid main(){}", 1, 0},
        {"layout(location = 0, num_views=2) in;", "                                     ", 1, 0},
        // not a num_views statement: untouched
        {"layout(location = 0) in vec4 a;", "layout(location = 0) in vec4 a;", 0, 0},
        {"layout(std140) uniform B { mat4 m[2]; };", "layout(std140) uniform B { mat4 m[2]; };", 0, 0},
        {"mylayout(num_views=2) in;", "mylayout(num_views=2) in;", 0, 0},
        {"layout(num_views=2) in vec4 x;", "layout(num_views=2) in vec4 x;", 0, 0},
        // whole tokens only
        {"x = m[gl_ViewID_OVR];", "x = m[(0u)         ];", 0, 1},
        {"int my_gl_ViewID_OVR = gl_ViewID_OVRx;", "int my_gl_ViewID_OVR = gl_ViewID_OVRx;", 0, 0},
        {"#define VIEW_ID gl_ViewID_OVR\nuint v = gl_ViewID_OVR+1u;",
         "#define VIEW_ID (0u)         \nuint v = (0u)         +1u;", 0, 2},
        // unterminated: untouched, no overrun
        {"layout(num_views=2", "layout(num_views=2", 0, 0},
        {"layout(num_views=2) in", "layout(num_views=2) in", 0, 0},
    };
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        int l = -1, v = -1;
        char *out = glmv_rewrite(cases[i].src, &l, &v);
        CHECK(out && !strcmp(out, cases[i].want), "case %zu: got \"%s\"", i, out ? out : "(null)");
        CHECK(l == cases[i].layouts && v == cases[i].ids, "case %zu: counts %d/%d", i, l, v);
        CHECK(glmv_has_num_views(cases[i].src) == (cases[i].layouts > 0), "case %zu: has_num_views", i);
        free(out);
    }
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s OUTDIR SHADER.cpp...\n", argv[0]);
        return 2;
    }
    check_cases();
    for (int i = 2; i < argc; ++i) check_shader(argv[1], argv[i]);
    if (failures) {
        fprintf(stderr, "%d failure(s)\n", failures);
        return 1;
    }
    printf("ok: %d shaders\n", argc - 2);
    return 0;
}
