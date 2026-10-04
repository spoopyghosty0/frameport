// Test fixture for tests/test_langpack.py: a stand-in for OVRPort's platform loader (libovrplatformloader.so).
// Like the real one it answers ovr_LanguagePack_GetCurrent/SetCurrent with `return 0` and keeps its own message queue;
// AssetFile_GetList/StatusById queue one message that carries one "store" asset (id 7).
// Build: cc -shared -fPIC -O1 fakeloader.c -o libovrplatformloader.so
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef uint64_t u64;
typedef struct { uint32_t type; u64 req; int err; } fmsg;
typedef struct { u64 id; char type[16]; char path[32]; } fdet;

static fmsg *q[16];
static int qn, freed;
static u64 next_req = 100;
static fdet store = {7, "store", "/store/a"};
static int arr_handle;

static u64 post(uint32_t type, int err) {
    fmsg *m = calloc(1, sizeof *m);
    m->type = type;
    m->req = next_req++;
    m->err = err;
    q[qn++] = m;
    return m->req;
}

u64 ovr_AssetFile_GetList(void) {
    if (getenv("FAKE_SILENT_LIST")) return next_req++;  // asks, never answers (the case the timeout fallback is for)
    return post(0x4AFC6F74u, 0);
}
u64 ovr_AssetFile_StatusById(u64 id) { return post(0x5D955D38u, id != 7); }
u64 ovr_LanguagePack_GetCurrent(void) { return 0; }
u64 ovr_LanguagePack_SetCurrent(const char *tag) { (void)tag; return 0; }

void *ovr_PopMessage(void) {
    if (!qn) return NULL;
    fmsg *m = q[0];
    memmove(q, q + 1, sizeof(q[0]) * (size_t)--qn);
    return m;
}
void ovr_FreeMessage(void *m) { freed++; free(m); }
uint32_t ovr_Message_GetType(const void *m) { return ((const fmsg *)m)->type; }
u64 ovr_Message_GetRequestID(const void *m) { return ((const fmsg *)m)->req; }
_Bool ovr_Message_IsError(const void *m) { return ((const fmsg *)m)->err != 0; }
void *ovr_Message_GetError(const void *m) { (void)m; return NULL; }
void *ovr_Message_GetAssetDetails(const void *m) { return ((const fmsg *)m)->err ? NULL : &store; }
void *ovr_Message_GetAssetDetailsArray(const void *m) { (void)m; return &arr_handle; }
size_t ovr_AssetDetailsArray_GetSize(const void *a) { (void)a; return 1; }
void *ovr_AssetDetailsArray_GetElement(const void *a, size_t i) { (void)a; return i == 0 ? &store : NULL; }
u64 ovr_AssetDetails_GetAssetId(const void *d) { return ((const fdet *)d)->id; }
const char *ovr_AssetDetails_GetAssetType(const void *d) { return ((const fdet *)d)->type; }
const char *ovr_AssetDetails_GetDownloadStatus(const void *d) { (void)d; return "available"; }
const char *ovr_AssetDetails_GetFilepath(const void *d) { return ((const fdet *)d)->path; }
const char *ovr_AssetDetails_GetIapStatus(const void *d) { (void)d; return "free"; }
void *ovr_AssetDetails_GetLanguage(const void *d) { (void)d; return NULL; }
const char *ovr_AssetDetails_GetMetadata(const void *d) { (void)d; return ""; }

// not part of the SDK: lets the test see that our wrapper frees the dispatcher's own message
int fake_freed(void) { return freed; }
