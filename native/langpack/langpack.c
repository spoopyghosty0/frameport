// SPDX-License-Identifier: GPL-3.0-only
//
// FramePort language packs: answers the Meta Platform SDK's language-pack calls from `*.lang` files that already
// sit in the game's data (OBB / app files), without any download.
//
// Why: OVRPort's libovrplatformloader.so is a dispatcher in front of Meta's own loader (…_meta.so). It answers
// ovr_LanguagePack_GetCurrent / ovr_LanguagePack_SetCurrent with `return 0` (request id 0, no message ever comes
// back), so a game that asks for its language pack waits for an answer that never arrives.
//
// How it is wired (patch frame.langpacks):
//   * this library becomes the first DT_NEEDED of the dispatcher;
//   * the dispatcher's own exports of the functions defined here are marked STB_LOCAL in its .dynsym, so symbol
//     lookups (the game's imports and dlsym on the dispatcher's handle) continue to this library;
//   * everything we do not answer ourselves is forwarded to the dispatcher's original function, found at run time by
//     reading the dispatcher's in-memory .dynsym (the entries are still there, only no longer visible to lookups).
//
// What it implements (see https://developers.meta.com/vr/documentation/native/ps-language-packs/):
//   ovr_LanguagePack_GetCurrent / SetCurrent        answered here
//   ovr_AssetFile_GetList                           the dispatcher's list + our packs (type "language_pack")
//   ovr_AssetFile_StatusById                        answered here for our packs (always "installed")
//   ovr_PopMessage, ovr_FreeMessage, ovr_Message_*, ovr_AssetDetails*_*, ovr_LanguagePackInfo_*,
//   ovr_AssetFileDownloadResult_*, ovr_Error_*      wrappers: our messages/handles are served here, any other
//                                                   handle goes to the dispatcher unchanged
//
// Where the packs are looked for (first file per tag wins; depth <= 3):
//   $FRAMEPORT_LANGPACK_DIRS (':'-separated), otherwise
//   /sdcard/Android/obb/<pkg>, /sdcard/Android/data/<pkg>/files, the same under /storage/emulated/0, and
//   /data/steam_app/obb (where Lepton mounts the host's obb folder)
// A pack is a regular file named <BCP47 tag>.lang (de.lang -> tag "de").
//
// Which pack is "current": the one the game applied with SetCurrent; else $FRAMEPORT_LANGPACK (a tag); else the only
// pack if there is exactly one.
#define _GNU_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <elf.h>
#include <link.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#ifdef __ANDROID__
#include <android/log.h>
#endif

#define EXPORT __attribute__((visibility("default")))

typedef uint64_t u64;
typedef uint32_t u32;

// Log line (logcat tag "fp_langpack" on Android, so it lands in the game's launch.log; on other systems stderr, only
// when $FRAMEPORT_LANGPACK_DEBUG is set).
static void fplog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void fplog(const char *fmt, ...) {
    char line[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
#ifdef __ANDROID__
    __android_log_print(ANDROID_LOG_INFO, "fp_langpack", "%s", line);
#else
    if (getenv("FRAMEPORT_LANGPACK_DEBUG")) fprintf(stderr, "fp_langpack: %s\n", line);
#endif
}

// accessor calls can repeat every frame: only the first FP_TRACE_MAX are logged
#define FP_TRACE_MAX 400
static int g_traced;
#define fptrace(...) \
    do { \
        if (__atomic_fetch_add(&g_traced, 1, __ATOMIC_RELAXED) < FP_TRACE_MAX) fplog(__VA_ARGS__); \
    } while (0)

// how long the dispatcher gets to answer AssetFile_GetList before we answer with our packs alone
#ifndef FP_LIST_TIMEOUT_MS
#define FP_LIST_TIMEOUT_MS 1500
#endif

#ifndef FP_LOADER_BASENAME
#define FP_LOADER_BASENAME "libovrplatformloader.so"
#endif

// ovrMessageType values (same table as native/platformcompat/message_type.cpp)
#define MSG_ASSETFILE_GETLIST 0x4AFC6F74u
#define MSG_ASSETFILE_STATUSBYID 0x5D955D38u
#define MSG_LANGUAGEPACK_GETCURRENT 0x1F90F0D5u
#define MSG_LANGUAGEPACK_SETCURRENT 0x5B4FBBE0u

#define MAXP 32
#define PATHLEN 1024
#define MAX_DEPTH 3

/* ---------------------------------------------------------------------------------------------------------------
 * Finding the dispatcher's original functions
 * ------------------------------------------------------------------------------------------------------------- */

typedef struct {
    const char *name;
    void *addr;
} find_t;

static uintptr_t fixptr(uintptr_t base, uintptr_t v) {
    // bionic leaves d_ptr as a link-time address, glibc adds the load bias in place
    return v < base ? base + v : v;
}

static size_t count_syms(uintptr_t base, const ElfW(Dyn) *dyn) {
    const uint32_t *gnu = NULL, *sysv = NULL;
    for (const ElfW(Dyn) *d = dyn; d->d_tag != DT_NULL; d++) {
        if (d->d_tag == DT_GNU_HASH) gnu = (const uint32_t *)fixptr(base, d->d_un.d_ptr);
        if (d->d_tag == DT_HASH) sysv = (const uint32_t *)fixptr(base, d->d_un.d_ptr);
    }
    if (sysv) return sysv[1];  // nchain
    if (!gnu) return 0;
    uint32_t nbuckets = gnu[0], symoffset = gnu[1], bloom_size = gnu[2];
    const uint32_t *buckets = gnu + 4 + bloom_size * (sizeof(ElfW(Addr)) / 4);
    const uint32_t *chains = buckets + nbuckets;
    uint32_t max = 0;
    for (uint32_t i = 0; i < nbuckets; i++)
        if (buckets[i] > max) max = buckets[i];
    if (max < symoffset) return symoffset;
    while (!(chains[max - symoffset] & 1)) max++;
    return (size_t)max + 1;
}

static int find_cb(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size;
    find_t *f = (find_t *)data;
    const char *n = info->dlpi_name ? info->dlpi_name : "";
    const char *slash = strrchr(n, '/');
    if (strcmp(slash ? slash + 1 : n, FP_LOADER_BASENAME) != 0) return 0;
    uintptr_t base = info->dlpi_addr;
    const ElfW(Dyn) *dyn = NULL;
    for (int i = 0; i < info->dlpi_phnum; i++)
        if (info->dlpi_phdr[i].p_type == PT_DYNAMIC) dyn = (const ElfW(Dyn) *)(base + info->dlpi_phdr[i].p_vaddr);
    if (!dyn) return 1;
    const ElfW(Sym) *symtab = NULL;
    const char *strtab = NULL;
    for (const ElfW(Dyn) *d = dyn; d->d_tag != DT_NULL; d++) {
        if (d->d_tag == DT_SYMTAB) symtab = (const ElfW(Sym) *)fixptr(base, d->d_un.d_ptr);
        if (d->d_tag == DT_STRTAB) strtab = (const char *)fixptr(base, d->d_un.d_ptr);
    }
    size_t n_syms = count_syms(base, dyn);
    if (!symtab || !strtab || !n_syms) return 1;
    for (size_t i = 1; i < n_syms; i++) {
        const ElfW(Sym) *s = &symtab[i];
        if (s->st_shndx == SHN_UNDEF || s->st_value == 0) continue;
        if (ELF64_ST_TYPE(s->st_info) != STT_FUNC) continue;
        if (strcmp(strtab + s->st_name, f->name) == 0) {
            f->addr = (void *)(base + s->st_value);
            break;
        }
    }
    return 1;
}

static void *orig_sym(const char *name) {
    find_t f = {name, NULL};
    dl_iterate_phdr(find_cb, &f);
    return f.addr;
}

// o_<Name>(...) calls the dispatcher's original ovr_<Name>; 0/NULL when it has none
#define DEFORIG(RET, NAME, PARAMS, ARGS)                       \
    static RET o_##NAME PARAMS {                               \
        typedef RET(*fn_t) PARAMS;                             \
        static fn_t f;                                         \
        static int tried;                                      \
        if (!tried) {                                          \
            f = (fn_t)orig_sym("ovr_" #NAME);                  \
            tried = 1;                                         \
        }                                                      \
        return f ? f ARGS : (RET)0;                            \
    }

DEFORIG(void *, PopMessage, (void), ())
DEFORIG(void, FreeMessage, (void *m), (m))
DEFORIG(u32, Message_GetType, (const void *m), (m))
DEFORIG(u64, Message_GetRequestID, (const void *m), (m))
DEFORIG(_Bool, Message_IsError, (const void *m), (m))
DEFORIG(void *, Message_GetError, (const void *m), (m))
DEFORIG(void *, Message_GetAssetDetails, (const void *m), (m))
DEFORIG(void *, Message_GetAssetDetailsArray, (const void *m), (m))
DEFORIG(void *, Message_GetAssetFileDownloadResult, (const void *m), (m))
DEFORIG(size_t, AssetDetailsArray_GetSize, (const void *a), (a))
DEFORIG(void *, AssetDetailsArray_GetElement, (const void *a, size_t i), (a, i))
DEFORIG(u64, AssetDetails_GetAssetId, (const void *d), (d))
DEFORIG(const char *, AssetDetails_GetAssetType, (const void *d), (d))
DEFORIG(const char *, AssetDetails_GetDownloadStatus, (const void *d), (d))
DEFORIG(const char *, AssetDetails_GetFilepath, (const void *d), (d))
DEFORIG(const char *, AssetDetails_GetIapStatus, (const void *d), (d))
DEFORIG(void *, AssetDetails_GetLanguage, (const void *d), (d))
DEFORIG(const char *, AssetDetails_GetMetadata, (const void *d), (d))
DEFORIG(const char *, LanguagePackInfo_GetTag, (const void *l), (l))
DEFORIG(const char *, LanguagePackInfo_GetEnglishName, (const void *l), (l))
DEFORIG(const char *, LanguagePackInfo_GetNativeName, (const void *l), (l))
DEFORIG(u64, AssetFileDownloadResult_GetAssetId, (const void *r), (r))
DEFORIG(const char *, AssetFileDownloadResult_GetFilepath, (const void *r), (r))
DEFORIG(u64, AssetFile_GetList, (void), ())
DEFORIG(u64, AssetFile_StatusById, (u64 id), (id))
DEFORIG(int, Error_GetCode, (const void *e), (e))
DEFORIG(int, Error_GetHttpCode, (const void *e), (e))
DEFORIG(const char *, Error_GetMessage, (const void *e), (e))
DEFORIG(const char *, Error_GetDisplayableMessage, (const void *e), (e))

/* ---------------------------------------------------------------------------------------------------------------
 * Finding the packs
 * ------------------------------------------------------------------------------------------------------------- */

typedef struct {
    char tag[48];
    char path[PATHLEN];
    u64 id;
} pack_t;

static const struct {
    const char *tag, *en, *native;
} NAMES[] = {
    {"en", "English", "English"},      {"de", "German", "Deutsch"},        {"fr", "French", "Français"},
    {"es", "Spanish", "Español"},      {"it", "Italian", "Italiano"},      {"pt", "Portuguese", "Português"},
    {"nl", "Dutch", "Nederlands"},     {"pl", "Polish", "Polski"},         {"ru", "Russian", "Русский"},
    {"tr", "Turkish", "Türkçe"},       {"ja", "Japanese", "日本語"},        {"ko", "Korean", "한국어"},
    {"zh", "Chinese", "中文"},          {"ar", "Arabic", "العربية"},        {"cs", "Czech", "Čeština"},
    {"sv", "Swedish", "Svenska"},      {"da", "Danish", "Dansk"},          {"fi", "Finnish", "Suomi"},
    {"nb", "Norwegian", "Norsk"},      {"hu", "Hungarian", "Magyar"},      {"uk", "Ukrainian", "Українська"},
    {"el", "Greek", "Ελληνικά"},       {"he", "Hebrew", "עברית"},          {"th", "Thai", "ไทย"},
    {"id", "Indonesian", "Indonesia"}, {"vi", "Vietnamese", "Tiếng Việt"}, {"ro", "Romanian", "Română"},
};

static u64 hash_tag(const char *tag) {
    u64 h = 1469598103934665603ULL;  // FNV-1a over the lower-case tag
    for (const char *p = tag; *p; p++) {
        h ^= (unsigned char)((*p >= 'A' && *p <= 'Z') ? *p + 32 : *p);
        h *= 1099511628211ULL;
    }
    return 0x4650000000000000ULL | (h & 0x0000FFFFFFFFFFFFULL);  // 'FP' prefix: never a store asset id
}

static int tag_eq(const char *a, const char *b) { return strcasecmp(a, b) == 0; }

static int base_len(const char *tag) {
    int n = 0;
    while (tag[n] && tag[n] != '-' && tag[n] != '_') n++;
    return n;
}

static int valid_tag(const char *t) {
    if (!*t) return 0;
    for (; *t; t++)
        if (!((*t >= 'a' && *t <= 'z') || (*t >= 'A' && *t <= 'Z') || (*t >= '0' && *t <= '9') || *t == '-' ||
              *t == '_'))
            return 0;
    return 1;
}

static void add_pack(pack_t *out, int *n, const char *tag, const char *path) {
    for (int i = 0; i < *n; i++)
        if (tag_eq(out[i].tag, tag)) return;  // first one wins
    pack_t *p = &out[(*n)++];
    snprintf(p->tag, sizeof p->tag, "%s", tag);
    snprintf(p->path, sizeof p->path, "%s", path);
    p->id = hash_tag(tag);
}

static int g_logged_dirs;

static void scan_dir(const char *dir, int depth, pack_t *out, int *n) {
    DIR *d = opendir(dir);
    if (!g_logged_dirs && depth == 0) fplog("looking in %s: %s", dir, d ? "found" : "not readable");
    if (!d) return;
    struct dirent *e;
    while (*n < MAXP && (e = readdir(d)) != NULL) {
        if (e->d_name[0] == '.') continue;
        char path[PATHLEN];
        if (snprintf(path, sizeof path, "%s/%s", dir, e->d_name) >= (int)sizeof path) continue;
        struct stat st;
        if (stat(path, &st) != 0) continue;
        if (S_ISDIR(st.st_mode)) {
            if (depth < MAX_DEPTH) scan_dir(path, depth + 1, out, n);
        } else if (S_ISREG(st.st_mode)) {
            size_t len = strlen(e->d_name);
            if (len > 5 && strcasecmp(e->d_name + len - 5, ".lang") == 0) {
                char tag[48];
                if (len - 5 >= sizeof tag) continue;
                memcpy(tag, e->d_name, len - 5);
                tag[len - 5] = 0;
                if (valid_tag(tag)) add_pack(out, n, tag, path);
            }
        }
    }
    closedir(d);
}

static void package_name(char *out, size_t cap) {
    out[0] = 0;
    FILE *f = fopen("/proc/self/cmdline", "r");
    if (!f) return;
    size_t n = fread(out, 1, cap - 1, f);
    fclose(f);
    out[n] = 0;  // argv[0] ends at the first NUL: the package (or package:process)
    char *colon = strchr(out, ':');
    if (colon) *colon = 0;
}

static int scan_dirs(pack_t *out);

static int scan(pack_t *out) {
    int n = scan_dirs(out);
    static int last = -1;
    if (n != last) {
        last = n;
        fplog("%d language pack(s) found", n);
        for (int i = 0; i < n; i++) fplog("  pack %s -> %s", out[i].tag, out[i].path);
    }
    g_logged_dirs = 1;
    return n;
}

static int scan_dirs(pack_t *out) {
    int n = 0;
    const char *dirs = getenv("FRAMEPORT_LANGPACK_DIRS");
    if (dirs && *dirs) {
        char buf[PATHLEN * 2];
        snprintf(buf, sizeof buf, "%s", dirs);
        for (char *save = NULL, *p = strtok_r(buf, ":", &save); p; p = strtok_r(NULL, ":", &save)) scan_dir(p, 0, out, &n);
        return n;
    }
    char pkg[256];
    package_name(pkg, sizeof pkg);
    if (!g_logged_dirs) fplog("package \"%s\"", pkg);
    if (!pkg[0] || strchr(pkg, '/')) return 0;
    static const char *const FORMATS[] = {"/sdcard/Android/obb/%s", "/sdcard/Android/data/%s/files",
                                          "/storage/emulated/0/Android/obb/%s",
                                          "/storage/emulated/0/Android/data/%s/files"};
    for (size_t i = 0; i < sizeof FORMATS / sizeof *FORMATS; i++) {
        char dir[PATHLEN];
        snprintf(dir, sizeof dir, FORMATS[i], pkg);
        scan_dir(dir, 0, out, &n);
    }
    // Lepton mounts the host's <app>/lepton-app/obb here (read-only) and only links /sdcard/Android/obb/<pkg> to it
    // at boot (liblepton/mounting.sh: podman_mount_entry "$LEPTON_MOUNT_OBB_DIR" "/$APP_MOUNT_TARGET/obb", default
    // APP_MOUNT_TARGET=data/steam_app), so look there too.
    scan_dir("/data/steam_app/obb", 0, out, &n);
    return n;
}

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static char g_current[48];  // tag the game applied with SetCurrent

static int find_tag(const pack_t *p, int n, const char *tag) {
    for (int i = 0; i < n; i++)
        if (tag_eq(p[i].tag, tag)) return i;
    int bl = base_len(tag);  // "de-DE" -> "de" (and the reverse: "de" asked, "de-de" present)
    for (int i = 0; i < n; i++)
        if (base_len(p[i].tag) == bl && strncasecmp(p[i].tag, tag, (size_t)bl) == 0) return i;
    return -1;
}

static int pick_current(const pack_t *p, int n) {
    char want[48] = "";
    pthread_mutex_lock(&g_lock);
    snprintf(want, sizeof want, "%s", g_current);
    pthread_mutex_unlock(&g_lock);
    if (want[0]) {
        int i = find_tag(p, n, want);
        if (i >= 0) return i;
    }
    const char *env = getenv("FRAMEPORT_LANGPACK");
    if (env && *env) {
        int i = find_tag(p, n, env);
        if (i >= 0) return i;
    }
    return n == 1 ? 0 : -1;
}

/* ---------------------------------------------------------------------------------------------------------------
 * Messages and handles. Every object we hand out lives inside one msg_t, so "is this handle ours" is an address
 * range check against the live messages.
 * ------------------------------------------------------------------------------------------------------------- */

// The asset's Metadata. Deadpool VR (Unreal) only accepts a language pack whose Metadata equals its own version string
// ("1.0.40.356975.Quest", built from five parts with "%s.%s.%s.%s.%s"), which Meta's store sets at upload. The
// patch writes the game's versionName behind the marker below (frame.langpacks, frameport.patches.frame.langpack);
// FRAMEPORT_LANGPACK_META overrides it. Without either the Metadata stays empty, as before.
#define META_LEN 96
#define META_MARK "@FPMETA@"
__attribute__((used)) static char g_meta_slot[sizeof META_MARK + META_LEN] = META_MARK;

static void pack_meta(char *out) {
    const char *env = getenv("FRAMEPORT_LANGPACK_META");
    const volatile char *slot = g_meta_slot + sizeof META_MARK;
    out[0] = 0;
    if (env && *env) snprintf(out, META_LEN, "%s", env);
    else {
        int i = 0;
        for (; i < META_LEN - 1 && slot[i]; i++) out[i] = slot[i];
        out[i] = 0;
    }
}

typedef struct {
    char tag[48], en[64], nat[64];
} lang_t;

typedef struct {
    u64 id;
    char type[16], status[16], iap[8], meta[META_LEN];
    char path[PATHLEN];
    lang_t *lang;
} details_t;

typedef struct {
    u64 id;
    char path[PATHLEN];
} result_t;

typedef struct {
    int code, http;
    char msg[128];
} err_t;

typedef struct msg {
    struct msg *all_next, *q_next;
    u32 type;
    u64 req;
    int is_error, has_det, has_res;
    err_t err;
    details_t det;
    lang_t lng;
    result_t res;
    u32 arr;  // its address is the handle of the details array
    int n;
    details_t dets[MAXP];
    lang_t langs[MAXP];
    void *orig_msg, *orig_arr;
    size_t orig_n;
} msg_t;

static msg_t *g_all, *g_qhead, *g_qtail;
static u64 g_next_req = 0x4650000000000001ULL;
#define MAX_PENDING 16
static u64 g_pending[MAX_PENDING];
static u64 g_pending_ms[MAX_PENDING];  // when each one was asked  // dispatcher requests whose AssetFile_GetList answer we extend

static msg_t *owner(const void *p) {
    msg_t *found = NULL;
    pthread_mutex_lock(&g_lock);
    for (msg_t *m = g_all; m; m = m->all_next)
        if ((const char *)p >= (const char *)m && (const char *)p < (const char *)m + sizeof *m) {
            found = m;
            break;
        }
    pthread_mutex_unlock(&g_lock);
    return found;
}

static msg_t *new_msg(u32 type, u64 req) {
    msg_t *m = (msg_t *)calloc(1, sizeof *m);
    if (!m) return NULL;
    m->type = type;
    m->req = req ? req : __atomic_fetch_add(&g_next_req, 1, __ATOMIC_RELAXED);
    pthread_mutex_lock(&g_lock);
    m->all_next = g_all;
    g_all = m;
    pthread_mutex_unlock(&g_lock);
    return m;
}

static void post(msg_t *m) {
    pthread_mutex_lock(&g_lock);
    m->q_next = NULL;
    if (g_qtail) g_qtail->q_next = m;
    else g_qhead = m;
    g_qtail = m;
    pthread_mutex_unlock(&g_lock);
}

static void fail(msg_t *m, int code, const char *text) {
    m->is_error = 1;
    m->err.code = code;
    snprintf(m->err.msg, sizeof m->err.msg, "%s", text);
}

static void fill_details(details_t *d, lang_t *l, const pack_t *p) {
    d->id = p->id;
    snprintf(d->type, sizeof d->type, "language_pack");
    snprintf(d->status, sizeof d->status, "installed");
    snprintf(d->iap, sizeof d->iap, "free");
    snprintf(d->path, sizeof d->path, "%s", p->path);
    pack_meta(d->meta);
    snprintf(l->tag, sizeof l->tag, "%s", p->tag);
    snprintf(l->en, sizeof l->en, "%s", p->tag);
    snprintf(l->nat, sizeof l->nat, "%s", p->tag);
    int bl = base_len(p->tag);
    for (size_t i = 0; i < sizeof NAMES / sizeof *NAMES; i++)
        if ((int)strlen(NAMES[i].tag) == bl && strncasecmp(NAMES[i].tag, p->tag, (size_t)bl) == 0) {
            snprintf(l->en, sizeof l->en, "%s", NAMES[i].en);
            snprintf(l->nat, sizeof l->nat, "%s", NAMES[i].native);
            break;
        }
    d->lang = l;
}

static u64 now_ms(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (u64)t.tv_sec * 1000u + (u64)t.tv_nsec / 1000000u;
}

static int take_pending(u64 req) {
    int hit = 0;
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < MAX_PENDING; i++)
        if (g_pending[i] == req) {
            g_pending[i] = 0;
            hit = 1;
            break;
        }
    pthread_mutex_unlock(&g_lock);
    return hit;
}

static void add_pending(u64 req) {
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < MAX_PENDING; i++)
        if (!g_pending[i]) {
            g_pending[i] = req;
            g_pending_ms[i] = now_ms();
            break;
        }
    pthread_mutex_unlock(&g_lock);
}

static void fill_list(msg_t *m, const pack_t *p, int n) {
    for (int i = 0; i < n && i < MAXP; i++) fill_details(&m->dets[i], &m->langs[i], &p[i]);
    m->n = n < MAXP ? n : MAXP;
}

// The dispatcher's AssetFile_GetList answer arrived: ours + theirs in one array.
static void *wrap_list(void *orig) {
    pack_t packs[MAXP];
    int n = scan(packs);
    if (n == 0) return orig;
    msg_t *m = new_msg(MSG_ASSETFILE_GETLIST, o_Message_GetRequestID(orig));
    if (!m) return orig;
    fill_list(m, packs, n);
    m->orig_msg = orig;
    if (!o_Message_IsError(orig)) {
        m->orig_arr = o_Message_GetAssetDetailsArray(orig);
        if (m->orig_arr) m->orig_n = o_AssetDetailsArray_GetSize(m->orig_arr);
    }
    return m;
}

// A GetList the dispatcher has not answered within FP_LIST_TIMEOUT_MS: answer with our packs alone (request id kept).
// Returns NULL if nothing is overdue or no pack exists. A late answer from the dispatcher is passed on unchanged.
static msg_t *expire_pending(void) {
    u64 req = 0, now = now_ms();
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < MAX_PENDING; i++)
        if (g_pending[i] && now - g_pending_ms[i] >= FP_LIST_TIMEOUT_MS) {
            req = g_pending[i];
            g_pending[i] = 0;
            break;
        }
    pthread_mutex_unlock(&g_lock);
    if (!req) return NULL;
    pack_t packs[MAXP];
    int n = scan(packs);
    fplog("AssetFile_GetList %llu: no answer after %d ms, answering with %d pack(s)", (unsigned long long)req,
          FP_LIST_TIMEOUT_MS, n);
    if (n == 0) return NULL;
    msg_t *m = new_msg(MSG_ASSETFILE_GETLIST, req);
    if (!m) return NULL;
    fill_list(m, packs, n);
    return m;
}

/* ---------------------------------------------------------------------------------------------------------------
 * Requests
 * ------------------------------------------------------------------------------------------------------------- */

EXPORT u64 ovr_LanguagePack_GetCurrent(void) {
    pack_t packs[MAXP];
    int n = scan(packs), i = pick_current(packs, n);
    fplog("LanguagePack_GetCurrent -> %s", i >= 0 ? packs[i].tag : "none applied");
    msg_t *m = new_msg(MSG_LANGUAGEPACK_GETCURRENT, 0);
    if (!m) return 0;
    u64 req = m->req;
    if (i < 0) {
        fail(m, 404, "no language pack is applied");
    } else {
        fill_details(&m->det, &m->lng, &packs[i]);
        m->has_det = 1;
    }
    post(m);
    return req;
}

EXPORT u64 ovr_LanguagePack_SetCurrent(const char *tag) {
    pack_t packs[MAXP];
    int n = scan(packs), i = (tag && *tag) ? find_tag(packs, n, tag) : -1;
    fplog("LanguagePack_SetCurrent(\"%s\") -> %s", tag ? tag : "(null)", i >= 0 ? packs[i].path : "not available");
    msg_t *m = new_msg(MSG_LANGUAGEPACK_SETCURRENT, 0);
    if (!m) return 0;
    u64 req = m->req;
    if (i < 0) {
        fail(m, 404, "language pack not available on this device");
    } else {
        pthread_mutex_lock(&g_lock);
        snprintf(g_current, sizeof g_current, "%s", packs[i].tag);
        pthread_mutex_unlock(&g_lock);
        m->res.id = packs[i].id;
        snprintf(m->res.path, sizeof m->res.path, "%s", packs[i].path);
        m->has_res = 1;
    }
    post(m);
    return req;
}

EXPORT u64 ovr_AssetFile_GetList(void) {
    u64 req = o_AssetFile_GetList();
    fplog("AssetFile_GetList (dispatcher request %llu)", (unsigned long long)req);
    if (req) {
        add_pending(req);
        return req;
    }
    // no dispatcher function behind us: answer with our packs alone
    pack_t packs[MAXP];
    int n = scan(packs);
    msg_t *m = new_msg(MSG_ASSETFILE_GETLIST, 0);
    if (!m) return 0;
    req = m->req;
    fill_list(m, packs, n);
    post(m);
    return req;
}

EXPORT u64 ovr_AssetFile_StatusById(u64 id) {
    fplog("AssetFile_StatusById(%llu)%s", (unsigned long long)id, (id >> 48) == 0x4650 ? " (ours)" : "");
    if ((id >> 48) == 0x4650) {
        pack_t packs[MAXP];
        int n = scan(packs);
        for (int i = 0; i < n; i++)
            if (packs[i].id == id) {
                msg_t *m = new_msg(MSG_ASSETFILE_STATUSBYID, 0);
                if (!m) return 0;
                u64 req = m->req;
                fill_details(&m->det, &m->lng, &packs[i]);
                m->has_det = 1;
                post(m);
                return req;
            }
        msg_t *m = new_msg(MSG_ASSETFILE_STATUSBYID, 0);
        if (!m) return 0;
        u64 req = m->req;
        fail(m, 404, "asset file not found");
        post(m);
        return req;
    }
    return o_AssetFile_StatusById(id);
}

/* ---------------------------------------------------------------------------------------------------------------
 * Message loop
 * ------------------------------------------------------------------------------------------------------------- */

EXPORT void *ovr_PopMessage(void) {
    pthread_mutex_lock(&g_lock);
    msg_t *m = g_qhead;
    if (m) {
        g_qhead = m->q_next;
        if (!g_qhead) g_qtail = NULL;
        m->q_next = NULL;
    }
    pthread_mutex_unlock(&g_lock);
    if (m) return m;
    void *o = o_PopMessage();
    if (o && o_Message_GetType(o) == MSG_ASSETFILE_GETLIST) {
        u64 r = o_Message_GetRequestID(o);
        if (take_pending(r)) {
            fplog("AssetFile_GetList %llu answered by the dispatcher", (unsigned long long)r);
            return wrap_list(o);
        }
    }
    if (o) return o;
    return expire_pending();
}

EXPORT void ovr_FreeMessage(void *h) {
    msg_t *m = h ? owner(h) : NULL;
    if (!m || (void *)m != h) {
        o_FreeMessage(h);
        return;
    }
    pthread_mutex_lock(&g_lock);
    for (msg_t **pp = &g_all; *pp; pp = &(*pp)->all_next)
        if (*pp == m) {
            *pp = m->all_next;
            break;
        }
    for (msg_t **pp = &g_qhead; *pp; pp = &(*pp)->q_next)  // freed unpopped: leave the queue too
        if (*pp == m) {
            *pp = m->q_next;
            break;
        }
    g_qtail = NULL;
    for (msg_t *q = g_qhead; q; q = q->q_next) g_qtail = q;
    pthread_mutex_unlock(&g_lock);
    if (m->orig_msg) o_FreeMessage(m->orig_msg);
    free(m);
}

EXPORT u32 ovr_Message_GetType(const void *h) {
    msg_t *m = owner(h);
    u32 t = m ? m->type : o_Message_GetType(h);
    if (m) fptrace("Message_GetType(ours) = 0x%08X", t);
    return t;
}

EXPORT u64 ovr_Message_GetRequestID(const void *h) {
    msg_t *m = owner(h);
    return m ? m->req : o_Message_GetRequestID(h);
}

EXPORT _Bool ovr_Message_IsError(const void *h) {
    msg_t *m = owner(h);
    return m ? (_Bool)m->is_error : o_Message_IsError(h);
}

EXPORT void *ovr_Message_GetError(const void *h) {
    msg_t *m = owner(h);
    if (!m) return o_Message_GetError(h);
    return m->is_error ? (void *)&m->err : NULL;
}

EXPORT void *ovr_Message_GetAssetDetails(const void *h) {
    msg_t *m = owner(h);
    if (!m) return o_Message_GetAssetDetails(h);
    return m->has_det ? (void *)&m->det : NULL;
}

EXPORT void *ovr_Message_GetAssetFileDownloadResult(const void *h) {
    msg_t *m = owner(h);
    if (!m) return o_Message_GetAssetFileDownloadResult(h);
    return m->has_res ? (void *)&m->res : NULL;
}

EXPORT void *ovr_Message_GetAssetDetailsArray(const void *h) {
    msg_t *m = owner(h);
    if (!m) return o_Message_GetAssetDetailsArray(h);
    fptrace("Message_GetAssetDetailsArray(ours)");
    return m->type == MSG_ASSETFILE_GETLIST && !m->is_error ? (void *)&m->arr : NULL;
}

EXPORT size_t ovr_AssetDetailsArray_GetSize(const void *h) {
    msg_t *m = owner(h);
    size_t n = m ? (size_t)m->n + m->orig_n : o_AssetDetailsArray_GetSize(h);
    if (m) fptrace("AssetDetailsArray_GetSize = %zu (%d ours + %zu dispatcher)", n, m->n, (size_t)m->orig_n);
    return n;
}

EXPORT void *ovr_AssetDetailsArray_GetElement(const void *h, size_t i) {
    msg_t *m = owner(h);
    if (!m) return o_AssetDetailsArray_GetElement(h, i);
    if (i < (size_t)m->n) {
        fptrace("AssetDetailsArray_GetElement(%zu) = our pack %s", i, m->dets[i].lang ? m->dets[i].lang->tag : "?");
        return &m->dets[i];
    }
    fptrace("AssetDetailsArray_GetElement(%zu) = dispatcher entry", i);
    return m->orig_arr ? o_AssetDetailsArray_GetElement(m->orig_arr, i - (size_t)m->n) : NULL;
}

/* ---------------------------------------------------------------------------------------------------------------
 * Accessors on our own objects (anything else goes to the dispatcher)
 * ------------------------------------------------------------------------------------------------------------- */

#define IS_OURS(h) (owner(h) != NULL)

EXPORT u64 ovr_AssetDetails_GetAssetId(const void *h) {
    if (!IS_OURS(h)) return o_AssetDetails_GetAssetId(h);
    fptrace("AssetDetails_GetAssetId(ours) = %llu", (unsigned long long)((const details_t *)h)->id);
    return ((const details_t *)h)->id;
}
EXPORT const char *ovr_AssetDetails_GetAssetType(const void *h) {
    if (!IS_OURS(h)) return o_AssetDetails_GetAssetType(h);
    fptrace("AssetDetails_GetAssetType(ours) = \"%s\"", ((const details_t *)h)->type);
    return ((const details_t *)h)->type;
}
EXPORT const char *ovr_AssetDetails_GetDownloadStatus(const void *h) {
    if (!IS_OURS(h)) return o_AssetDetails_GetDownloadStatus(h);
    fptrace("AssetDetails_GetDownloadStatus(ours) = \"%s\"", ((const details_t *)h)->status);
    return ((const details_t *)h)->status;
}
EXPORT const char *ovr_AssetDetails_GetFilepath(const void *h) {
    if (!IS_OURS(h)) return o_AssetDetails_GetFilepath(h);
    fptrace("AssetDetails_GetFilepath(ours) = \"%s\"", ((const details_t *)h)->path);
    return ((const details_t *)h)->path;
}
EXPORT const char *ovr_AssetDetails_GetIapStatus(const void *h) {
    if (!IS_OURS(h)) return o_AssetDetails_GetIapStatus(h);
    fptrace("AssetDetails_GetIapStatus(ours) = \"%s\"", ((const details_t *)h)->iap);
    return ((const details_t *)h)->iap;
}
EXPORT void *ovr_AssetDetails_GetLanguage(const void *h) {
    if (!IS_OURS(h)) return o_AssetDetails_GetLanguage(h);
    fptrace("AssetDetails_GetLanguage(ours)");
    return (void *)((const details_t *)h)->lang;
}
EXPORT const char *ovr_AssetDetails_GetMetadata(const void *h) {
    if (!IS_OURS(h)) return o_AssetDetails_GetMetadata(h);
    fptrace("AssetDetails_GetMetadata(ours) = \"%s\"", ((const details_t *)h)->meta);
    return ((const details_t *)h)->meta;
}

EXPORT const char *ovr_LanguagePackInfo_GetTag(const void *h) {
    if (!IS_OURS(h)) return o_LanguagePackInfo_GetTag(h);
    fptrace("LanguagePackInfo_GetTag(ours) = \"%s\"", ((const lang_t *)h)->tag);
    return ((const lang_t *)h)->tag;
}
EXPORT const char *ovr_LanguagePackInfo_GetEnglishName(const void *h) {
    if (!IS_OURS(h)) return o_LanguagePackInfo_GetEnglishName(h);
    fptrace("LanguagePackInfo_GetEnglishName(ours) = \"%s\"", ((const lang_t *)h)->en);
    return ((const lang_t *)h)->en;
}
EXPORT const char *ovr_LanguagePackInfo_GetNativeName(const void *h) {
    if (!IS_OURS(h)) return o_LanguagePackInfo_GetNativeName(h);
    fptrace("LanguagePackInfo_GetNativeName(ours) = \"%s\"", ((const lang_t *)h)->nat);
    return ((const lang_t *)h)->nat;
}

EXPORT u64 ovr_AssetFileDownloadResult_GetAssetId(const void *h) {
    return IS_OURS(h) ? ((const result_t *)h)->id : o_AssetFileDownloadResult_GetAssetId(h);
}
EXPORT const char *ovr_AssetFileDownloadResult_GetFilepath(const void *h) {
    return IS_OURS(h) ? ((const result_t *)h)->path : o_AssetFileDownloadResult_GetFilepath(h);
}

EXPORT int ovr_Error_GetCode(const void *h) { return IS_OURS(h) ? ((const err_t *)h)->code : o_Error_GetCode(h); }
EXPORT int ovr_Error_GetHttpCode(const void *h) { return IS_OURS(h) ? ((const err_t *)h)->http : o_Error_GetHttpCode(h); }
EXPORT const char *ovr_Error_GetMessage(const void *h) {
    return IS_OURS(h) ? ((const err_t *)h)->msg : o_Error_GetMessage(h);
}
EXPORT const char *ovr_Error_GetDisplayableMessage(const void *h) {
    return IS_OURS(h) ? ((const err_t *)h)->msg : o_Error_GetDisplayableMessage(h);
}
