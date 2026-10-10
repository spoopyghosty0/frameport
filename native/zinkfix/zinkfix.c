// SPDX-License-Identifier: GPL-3.0-only
// FramePort shader-fix Vulkan layer for OpenGL ES games (libVkLayer_fp_shaderfix.so, layer VK_LAYER_FP_shader_fix).
//
// On the Frame, OpenGL ES runs on Zink (Mesa's GL on Vulkan): the game's GLSL becomes SPIR-V inside the driver, which
// the Vulkan shim (vkshim, only in front of an engine's own Vulkan) never sees. Some of those modules read locals
// before writing them (e.g. two lightspeed shaders of Vader Immortal: loop counters and accumulators undefined, the
// GPU hangs; captured and fixed by Klownicle, GitHub #49). This layer sits between Zink and the driver and inserts
// words into modules whose size and SHA-256 match a fix, exactly like vkshim's vk_shader_fix; every other module
// passes through unchanged.
// Settings (same sources as FrameBridge: libframe_settings.so next to this library, the game's framebridge.conf,
// FRAMEBRIDGE_CONFIG):
//   zink_shader_fix=<size>:<sha256 hex>:<byte offset>:<word>,<word>,...[;<next fix>]
//   zink_shader_dump=1  writes every distinct module to Android/data/<pkg>/files/fp_spirv/<size>_<sha>.spv (capture
//                       new variants after a driver update; diagnostics)
//
// Activation: Android's Vulkan loader only loads the layers GraphicsEnv names (Lepton fills that list from its own
// layer set for the running app), from the app's native library directory among others. The engine library loads this
// library (DT_NEEDED) before it starts EGL; its constructor puts this layer in front of GraphicsEnv's debug layer list
// for this process only, through GraphicsEnv's (private, Android 11) functions found via libvulkan.so's dependencies.
// Missing functions are logged and nothing changes.
#define _GNU_SOURCE
#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>
#include <android/log.h>
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define TAG "FrameBridge"
#define EXPORT __attribute__((visibility("default")))
#define LOG(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)
#define LAYER_NAME "VK_LAYER_FP_shader_fix"

// ---------------------------------------------------------------- settings

#define MAX_FIXES 16
#define MAX_WORDS 64
static struct {
    size_t size, offset;
    uint8_t sha[32];
    uint32_t words[MAX_WORDS];
    int nwords, applied;
} fixes[MAX_FIXES];
static int nfixes, dump;
static char process[256];

static int hexval(char c) {
    return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
}

static void parse_fixes(char *v) {
    for (char *save = NULL, *item = strtok_r(v, ";", &save); item && nfixes < MAX_FIXES; item = strtok_r(NULL, ";", &save)) {
        char *f[4] = {0}, *save2 = NULL;
        int n = 0;
        for (char *t = strtok_r(item, ":", &save2); t && n < 4; t = strtok_r(NULL, ":", &save2)) f[n++] = t;
        if (n != 4 || strlen(f[1]) != 64) continue;
        int ok = 1;
        for (int i = 0; i < 32 && ok; i++) {
            int hi = hexval(f[1][2 * i]), lo = hexval(f[1][2 * i + 1]);
            if (hi < 0 || lo < 0) ok = 0; else fixes[nfixes].sha[i] = (uint8_t)(hi << 4 | lo);
        }
        fixes[nfixes].size = strtoul(f[0], NULL, 0);
        fixes[nfixes].offset = strtoul(f[2], NULL, 0);
        int nw = 0;
        for (char *save3 = NULL, *w = strtok_r(f[3], ",", &save3); w && nw < MAX_WORDS; w = strtok_r(NULL, ",", &save3))
            fixes[nfixes].words[nw++] = (uint32_t)strtoul(w, NULL, 0);
        fixes[nfixes].nwords = nw;
        fixes[nfixes].applied = 0;
        if (ok && nw && fixes[nfixes].size % 4 == 0 && fixes[nfixes].offset % 4 == 0 &&
            fixes[nfixes].offset >= 20 && fixes[nfixes].offset <= fixes[nfixes].size)
            nfixes++;
    }
}

static void read_settings(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[8192];
    while (fgets(line, sizeof line, f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (!strncmp(line, "zink_shader_dump=", 17)) dump = atoi(line + 17) != 0;
        if (!strncmp(line, "zink_shader_fix=", 16)) {
            nfixes = 0;  // later sources override earlier ones (as for the adapter's settings)
            parse_fixes(line + 16);
        }
    }
    fclose(f);
}

static void read_all_settings(void) {
    char path[600];
    Dl_info info;
    if (dladdr((void *)read_all_settings, &info) && info.dli_fname) {
        const char *slash = strrchr(info.dli_fname, '/');
        if (slash && slash - info.dli_fname < 500) {
            snprintf(path, sizeof path, "%.*s/libframe_settings.so", (int)(slash - info.dli_fname), info.dli_fname);
            read_settings(path);
        }
    }
    FILE *f = fopen("/proc/self/cmdline", "r");
    if (f) {
        size_t n = fread(process, 1, sizeof process - 1, f);
        process[n] = 0;
        fclose(f);
    }
    char *colon = strchr(process, ':');
    if (colon) *colon = 0;
    if (*process && !strchr(process, '/')) {
        snprintf(path, sizeof path, "/sdcard/Android/data/%s/files/framebridge.conf", process);
        read_settings(path);
    }
    const char *env = getenv("FRAMEBRIDGE_CONFIG");
    if (env && *env) read_settings(env);
}

// ---------------------------------------------------------------- SHA-256 (FIPS 180-4)

static const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01,
    0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08,
    0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha256_block(uint32_t h[8], const uint8_t *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++) w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 | (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t t1 = hh + (ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i];
        uint32_t t2 = (ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
}

static void sha256(const uint8_t *data, size_t len, uint8_t out[32]) {
    uint32_t h[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    size_t i = 0;
    for (; i + 64 <= len; i += 64) sha256_block(h, data + i);
    uint8_t tail[128] = {0};
    size_t rest = len - i;
    memcpy(tail, data + i, rest);
    tail[rest] = 0x80;
    size_t blocks = rest + 9 > 64 ? 2 : 1;
    uint64_t bits = (uint64_t)len * 8;
    for (int b = 0; b < 8; b++) tail[blocks * 64 - 1 - b] = (uint8_t)(bits >> (8 * b));
    for (size_t b = 0; b < blocks; b++) sha256_block(h, tail + 64 * b);
    for (int b = 0; b < 8; b++) {
        out[4 * b] = h[b] >> 24; out[4 * b + 1] = h[b] >> 16; out[4 * b + 2] = h[b] >> 8; out[4 * b + 3] = h[b];
    }
}

// ---------------------------------------------------------------- the fix and the dump

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
#define MAX_DUMPED 4096
static uint8_t dumped[MAX_DUMPED][8];
static int ndumped, dump_dir_ok = -1;

static void dump_module(const uint32_t *code, size_t size, const uint8_t digest[32]) {
    pthread_mutex_lock(&lock);
    int seen = 0;
    for (int i = 0; i < ndumped && !seen; i++) seen = !memcmp(dumped[i], digest, 8);
    if (!seen && ndumped < MAX_DUMPED) memcpy(dumped[ndumped++], digest, 8);
    pthread_mutex_unlock(&lock);
    if (seen || !*process || strchr(process, '/')) return;
    char path[512];
    snprintf(path, sizeof path, "/sdcard/Android/data/%s/files/fp_spirv", process);
    if (dump_dir_ok < 0) {
        dump_dir_ok = mkdir(path, 0775) == 0 || errno == EEXIST;
        LOG("shader fix layer: dumping SPIR-V modules to %s (%s)", path, dump_dir_ok ? "ok" : strerror(errno));
    }
    if (!dump_dir_ok) return;
    char hex[65];
    for (int i = 0; i < 32; i++) snprintf(hex + 2 * i, 3, "%02x", digest[i]);
    snprintf(path + strlen(path), sizeof path - strlen(path), "/%zu_%s.spv", size, hex);
    FILE *f = fopen(path, "wb");
    if (!f) return;
    fwrite(code, 1, size, f);
    fclose(f);
}

// The fixed copy of `code` (caller frees) and its size, or NULL when no fix matches.
static uint32_t *fixed_shader(const uint32_t *code, size_t size, size_t *out_size) {
    uint8_t digest[32];
    int hashed = 0;
    if (dump) {
        sha256((const uint8_t *)code, size, digest);
        hashed = 1;
        dump_module(code, size, digest);
    }
    for (int i = 0; i < nfixes; i++) {
        if (fixes[i].size != size) continue;
        if (!hashed) sha256((const uint8_t *)code, size, digest), hashed = 1;
        if (memcmp(digest, fixes[i].sha, 32)) {
            static int near_logged;
            if (near_logged++ < 8) LOG("shader fix layer: a %zu-byte module differs from fix %d (another build?)", size, i + 1);
            continue;
        }
        size_t extra = (size_t)fixes[i].nwords * 4;
        uint32_t *copy = malloc(size + extra);
        if (!copy) return NULL;
        memcpy(copy, code, fixes[i].offset);
        memcpy((uint8_t *)copy + fixes[i].offset, fixes[i].words, extra);
        memcpy((uint8_t *)copy + fixes[i].offset + extra, (const uint8_t *)code + fixes[i].offset, size - fixes[i].offset);
        *out_size = size + extra;
        if (fixes[i].applied++ < 3)
            LOG("shader fix layer: fixed shader module %d (%zu -> %zu bytes)", i + 1, size, size + extra);
        return copy;
    }
    return NULL;
}

// ---------------------------------------------------------------- dispatch (per instance / device, by dispatch key)

#define MAX_HANDLES 16
static struct { void *key; VkInstance instance; PFN_vkGetInstanceProcAddr gipa; } instances[MAX_HANDLES];
static struct { void *key; PFN_vkGetDeviceProcAddr gdpa; PFN_vkCreateShaderModule create_shader; } devices[MAX_HANDLES];

static void *key_of(const void *handle) { return handle ? *(void *const *)handle : NULL; }

static PFN_vkGetInstanceProcAddr instance_gipa(const void *handle, VkInstance *instance) {
    void *key = key_of(handle);
    PFN_vkGetInstanceProcAddr found = NULL, last = NULL;
    pthread_mutex_lock(&lock);
    for (int i = 0; i < MAX_HANDLES; i++) {
        if (!instances[i].gipa) continue;
        last = instances[i].gipa;
        if (instance) *instance = instances[i].instance;
        if (instances[i].key == key) { found = instances[i].gipa; break; }
    }
    pthread_mutex_unlock(&lock);
    return found ? found : last;  // a physical device: its instance (or the only one there is)
}

static VKAPI_ATTR VkResult VKAPI_CALL layer_CreateInstance(const VkInstanceCreateInfo *ci, const VkAllocationCallbacks *a,
                                                           VkInstance *instance) {
    VkLayerInstanceCreateInfo *link = (VkLayerInstanceCreateInfo *)ci->pNext;
    while (link && !(link->sType == VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO && link->function == VK_LAYER_LINK_INFO))
        link = (VkLayerInstanceCreateInfo *)link->pNext;
    if (!link || !link->u.pLayerInfo) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr next = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;
    PFN_vkCreateInstance create = (PFN_vkCreateInstance)next(VK_NULL_HANDLE, "vkCreateInstance");
    if (!create) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = create(ci, a, instance);
    if (r != VK_SUCCESS) return r;
    pthread_mutex_lock(&lock);
    int slot = 0;
    for (int i = 0; i < MAX_HANDLES; i++)
        if (!instances[i].gipa || instances[i].key == key_of(*instance)) { slot = i; break; }
    instances[slot].key = key_of(*instance);
    instances[slot].instance = *instance;
    instances[slot].gipa = next;
    pthread_mutex_unlock(&lock);
    static int logged;
    if (logged++ < 4) LOG("shader fix layer: active on instance %p (%d fix(es))", (void *)*instance, nfixes);
    return r;
}

static VKAPI_ATTR VkResult VKAPI_CALL layer_CreateDevice(VkPhysicalDevice pd, const VkDeviceCreateInfo *ci,
                                                         const VkAllocationCallbacks *a, VkDevice *device) {
    VkLayerDeviceCreateInfo *link = (VkLayerDeviceCreateInfo *)ci->pNext;
    while (link && !(link->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO && link->function == VK_LAYER_LINK_INFO))
        link = (VkLayerDeviceCreateInfo *)link->pNext;
    if (!link || !link->u.pLayerInfo) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr next_gipa = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr next_gdpa = link->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;
    VkInstance instance = VK_NULL_HANDLE;
    instance_gipa(pd, &instance);
    PFN_vkCreateDevice create = (PFN_vkCreateDevice)next_gipa(instance, "vkCreateDevice");
    if (!create) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = create(pd, ci, a, device);
    if (r != VK_SUCCESS) return r;
    pthread_mutex_lock(&lock);
    int slot = 0;
    for (int i = 0; i < MAX_HANDLES; i++)
        if (!devices[i].gdpa || devices[i].key == key_of(*device)) { slot = i; break; }
    devices[slot].key = key_of(*device);
    devices[slot].gdpa = next_gdpa;
    devices[slot].create_shader = (PFN_vkCreateShaderModule)next_gdpa(*device, "vkCreateShaderModule");
    pthread_mutex_unlock(&lock);
    return r;
}

static void *device_entry(VkDevice device, int shader) {
    void *key = key_of(device), *fn = NULL;
    pthread_mutex_lock(&lock);
    for (int i = 0; i < MAX_HANDLES && !fn; i++)
        if (devices[i].gdpa && devices[i].key == key) fn = shader ? (void *)devices[i].create_shader : (void *)devices[i].gdpa;
    pthread_mutex_unlock(&lock);
    return fn;
}

static VKAPI_ATTR VkResult VKAPI_CALL layer_CreateShaderModule(VkDevice device, const VkShaderModuleCreateInfo *ci,
                                                               const VkAllocationCallbacks *a, VkShaderModule *module) {
    PFN_vkCreateShaderModule next = (PFN_vkCreateShaderModule)device_entry(device, 1);
    if (!next) return VK_ERROR_INITIALIZATION_FAILED;
    size_t size = 0;
    uint32_t *copy = ci && ci->pCode && ci->codeSize >= 20 ? fixed_shader(ci->pCode, ci->codeSize, &size) : NULL;
    if (!copy) return next(device, ci, a, module);
    VkShaderModuleCreateInfo fixed = *ci;
    fixed.codeSize = size;
    fixed.pCode = copy;
    VkResult r = next(device, &fixed, a, module);
    free(copy);
    return r;
}

static PFN_vkVoidFunction own(const char *name);

static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL layer_GetDeviceProcAddr(VkDevice device, const char *name) {
    PFN_vkVoidFunction fn = name && (!strcmp(name, "vkCreateShaderModule") || !strcmp(name, "vkGetDeviceProcAddr"))
                                ? own(name) : NULL;
    if (fn) return fn;
    PFN_vkGetDeviceProcAddr next = (PFN_vkGetDeviceProcAddr)device_entry(device, 0);
    return next ? next(device, name) : NULL;
}

static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL layer_GetInstanceProcAddr(VkInstance instance, const char *name) {
    PFN_vkVoidFunction fn = own(name);
    if (fn) return fn;
    PFN_vkGetInstanceProcAddr next = instance ? instance_gipa(instance, NULL) : NULL;
    return next ? next(instance, name) : NULL;
}

// ---------------------------------------------------------------- layer enumeration (what the loader asks for)

static const VkLayerProperties properties = {LAYER_NAME, VK_MAKE_VERSION(1, 1, 0), 1, "FramePort shader fixes"};

EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkEnumerateInstanceLayerProperties(uint32_t *count, VkLayerProperties *props) {
    if (!props) { *count = 1; return VK_SUCCESS; }
    if (*count < 1) return VK_INCOMPLETE;
    props[0] = properties;
    *count = 1;
    return VK_SUCCESS;
}

EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkEnumerateInstanceExtensionProperties(const char *layer, uint32_t *count,
                                                                             VkExtensionProperties *props) {
    (void)props;
    if (!layer || strcmp(layer, LAYER_NAME)) return VK_ERROR_LAYER_NOT_PRESENT;
    *count = 0;
    return VK_SUCCESS;
}

EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkEnumerateDeviceLayerProperties(VkPhysicalDevice pd, uint32_t *count,
                                                                       VkLayerProperties *props) {
    (void)pd;
    return vkEnumerateInstanceLayerProperties(count, props);
}

EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkEnumerateDeviceExtensionProperties(VkPhysicalDevice pd, const char *layer,
                                                                           uint32_t *count, VkExtensionProperties *props) {
    (void)pd;
    return vkEnumerateInstanceExtensionProperties(layer, count, props);
}

EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL VK_LAYER_FP_shader_fixGetInstanceProcAddr(VkInstance i, const char *n) {
    return layer_GetInstanceProcAddr(i, n);
}

EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL VK_LAYER_FP_shader_fixGetDeviceProcAddr(VkDevice d, const char *n) {
    return layer_GetDeviceProcAddr(d, n);
}

static PFN_vkVoidFunction own(const char *name) {
    if (!name) return NULL;
#define OWN(fn, impl) if (!strcmp(name, fn)) return (PFN_vkVoidFunction)impl;
    OWN("vkGetInstanceProcAddr", layer_GetInstanceProcAddr)
    OWN("vkGetDeviceProcAddr", layer_GetDeviceProcAddr)
    OWN("vkCreateInstance", layer_CreateInstance)
    OWN("vkCreateDevice", layer_CreateDevice)
    OWN("vkCreateShaderModule", layer_CreateShaderModule)
    OWN("vkEnumerateInstanceLayerProperties", vkEnumerateInstanceLayerProperties)
    OWN("vkEnumerateInstanceExtensionProperties", vkEnumerateInstanceExtensionProperties)
    OWN("vkEnumerateDeviceLayerProperties", vkEnumerateDeviceLayerProperties)
#undef OWN
    return NULL;
}

// ---------------------------------------------------------------- activation (GraphicsEnv debug layers)

// libc++ std::string as Android 11's platform libc++ lays it out (little endian, short-string flag in bit 0)
struct cxx_string { size_t cap; size_t size; char *data; };

static const char *cxx_chars(const void *s, size_t *len) {
    const unsigned char *b = s;
    if (b[0] & 1) {
        const struct cxx_string *l = s;
        *len = l->size;
        return l->data;
    }
    *len = b[0] >> 1;
    return (const char *)b + 1;
}

#define GET_INSTANCE "_ZN7android11GraphicsEnv11getInstanceEv"
#define GET_LAYERS "_ZN7android11GraphicsEnv14getDebugLayersEv"
#define SET_LAYERS "_ZN7android11GraphicsEnv14setDebugLayersENSt3__112basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEE"

__attribute__((constructor)) static void activate(void) {
    read_all_settings();
    if (!nfixes && !dump) return;
    void *vk = dlopen("libvulkan.so", RTLD_NOW | RTLD_LOCAL);
    void *(*get_instance)(void) = vk ? (void *(*)(void))dlsym(vk, GET_INSTANCE) : NULL;
    const void *(*get_layers)(void *) = vk ? (const void *(*)(void *))dlsym(vk, GET_LAYERS) : NULL;
    void (*set_layers)(void *, struct cxx_string *) = vk ? (void (*)(void *, struct cxx_string *))dlsym(vk, SET_LAYERS) : NULL;
    if (!get_instance || !get_layers || !set_layers) {
        LOG("shader fix layer: NOT active, Android's GraphicsEnv functions not found (%s%s%s)", get_instance ? "" : "getInstance ",
            get_layers ? "" : "getDebugLayers ", set_layers ? "" : "setDebugLayers");
        return;
    }
    void *env = get_instance();
    size_t len = 0;
    const char *current = env ? cxx_chars(get_layers(env), &len) : NULL;
    if (!env || (len && !current) || len > 4096) {
        LOG("shader fix layer: NOT active, unexpected GraphicsEnv state");
        return;
    }
    char old[4097];
    memcpy(old, current ? current : "", len);
    old[len] = 0;
    if (strstr(old, LAYER_NAME)) return;  // loaded twice (the loader's own dlopen): already in the list
    size_t n = strlen(LAYER_NAME) + (len ? 1 + len : 0);
    size_t cap = (n + 16) & ~(size_t)15;
    char *data = malloc(cap);
    if (!data) return;
    snprintf(data, cap, "%s%s%s", LAYER_NAME, len ? ":" : "", old);
    // passed by value: the callee gets a pointer to our temporary and copies it; freed below unless it was moved
    struct cxx_string tmp = {cap | 1, n, data};
    set_layers(env, &tmp);
    if ((tmp.cap & 1) && tmp.data == data) free(data);
    const char *now = cxx_chars(get_layers(env), &len);
    LOG("shader fix layer: debug layers now \"%.*s\" (%d fix(es)%s)", (int)len, now ? now : "", nfixes,
        dump ? ", dumping modules" : "");
}
