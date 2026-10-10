// SPDX-License-Identifier: GPL-3.0-only
// GLSL source rewrite for libfpglmv.so (see glmv.c): turns an OVR_multiview shader into its single-view twin.
//   - every `layout(... num_views ...) in;` statement is blanked out (the program then declares no views, which is
//     what Mesa requires for a draw into a framebuffer whose attachments aren't multiview)
//   - every gl_ViewID_OVR token becomes (0u): view 0 (the left eye's matrices in Doom3Quest)
// #extension lines stay (the driver supports OVR_multiview; enabling it without a num_views layout is harmless).
// Columns and line numbers are kept (blanks pad the replacements), so compile logs point at the same places.
// Plain C, no GL: the host unit test (tests/fixtures/src/glmv_rewrite_test.c) compiles it too.
#pragma once
#include <stdlib.h>
#include <string.h>

static int glmv_ident_char(char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_';
}

// `word` at p as a whole token (not part of a longer identifier)
static int glmv_token_at(const char *start, const char *p, const char *word) {
    size_t n = strlen(word);
    if (strncmp(p, word, n) != 0) return 0;
    if (p > start && glmv_ident_char(p[-1])) return 0;
    return !glmv_ident_char(p[n]);
}

static const char *glmv_skip_ws(const char *p) {
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r' || *p == '\f' || *p == '\v') ++p;
    return p;
}

// If a `layout(...num_views...) in;` statement starts at p, returns a pointer to its ';', else NULL.
static const char *glmv_num_views_stmt(const char *start, const char *p) {
    if (!glmv_token_at(start, p, "layout")) return NULL;
    const char *q = glmv_skip_ws(p + 6);
    if (*q != '(') return NULL;
    const char *open = q;
    int depth = 0;
    for (; *q; ++q) {
        if (*q == '(') ++depth;
        else if (*q == ')' && --depth == 0) break;
    }
    if (!*q) return NULL;
    const char *close = q;
    int has_views = 0;
    for (const char *r = open + 1; r < close; ++r)
        if (glmv_token_at(start, r, "num_views")) { has_views = 1; break; }
    if (!has_views) return NULL;
    q = glmv_skip_ws(close + 1);
    if (!glmv_token_at(start, q, "in")) return NULL;
    q = glmv_skip_ws(q + 2);
    return *q == ';' ? q : NULL;
}

// 1 if the source declares multiview views (a vertex shader of an OVR_multiview program)
static int glmv_has_num_views(const char *src) {
    for (const char *p = src; (p = strstr(p, "layout")); p += 6)
        if (glmv_num_views_stmt(src, p)) return 1;
    return 0;
}

// The single-view twin of `src` (malloc'd, same length), or NULL when out of memory. Counts go to *layouts / *ids.
static char *glmv_rewrite(const char *src, int *layouts, int *ids) {
    static const char id_tok[] = "gl_ViewID_OVR";  // 13 characters
    static const char zero[] = "(0u)";
    size_t n = strlen(src);
    char *out = (char *)malloc(n + 1);
    if (!out) return NULL;
    memcpy(out, src, n + 1);
    int nl = 0, ni = 0;
    for (char *p = out; (p = strstr(p, "layout"));) {
        const char *semi = glmv_num_views_stmt(out, p);
        if (!semi) { p += 6; continue; }
        for (char *b = p; b <= semi; ++b)
            if (*b != '\n' && *b != '\r') *b = ' ';
        ++nl;
        p = (char *)semi + 1;
    }
    for (char *p = out; (p = strstr(p, id_tok));) {
        if (!glmv_token_at(out, p, id_tok)) { p += sizeof(id_tok) - 1; continue; }
        memcpy(p, zero, sizeof(zero) - 1);
        memset(p + sizeof(zero) - 1, ' ', sizeof(id_tok) - sizeof(zero));
        ++ni;
        p += sizeof(id_tok) - 1;
    }
    if (layouts) *layouts = nl;
    if (ids) *ids = ni;
    return out;
}
