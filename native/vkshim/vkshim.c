// SPDX-License-Identifier: GPL-3.0-only
// FramePort Vulkan shim: drops invalid pNext pointers from render-pass create infos before they reach the Vulkan
// loader. Some engines (e.g. Unreal builds such as Deadpool VR) leave the pNext of unused attachment references
// uninitialized. The Frame's driver never reads it, but Lepton always loads Steam's Fossilize layer, which records
// every render pass, follows the garbage pointer and crashes (SIGSEGV in libVkLayer_fossilize.so).
//
// The engine finds Vulkan with dlopen("libvulkan.so") + dlsym, so FramePort points that string in the engine library
// at this shim (patches/frame/vk_sanitize.py). dlsym on the shim finds the functions below first and every other
// symbol in its dependency, the real libvulkan.so. Valid pointers are kept; a pointer is dropped only when it isn't
// readable memory or doesn't point at a structure type the parent may chain.
// A depth/stencil resolve named in a subpass without a depth attachment is dropped as well: the Frame's driver
// (Turnip) reads the missing depth attachment and crashes in vkCreateRenderPass2 (Unreal 5, e.g. Metro Awakening).
//
// Shader fixes (adapter setting vk_shader_fix, from a game's recipe): a SPIR-V module whose size and SHA-256 match is
// copied with extra words inserted at a byte offset, e.g. stores that initialize locals a shader reads before writing
// (an undefined loop counter hung the GPU in VR4's campaign). Every other module passes through unchanged.
// Format: vk_shader_fix=<size>:<sha256 hex>:<byte offset>:<word>,<word>,...[;<next fix>]
// Shader dump (vk_shader_dump=1, diagnostics): every distinct module the game creates is written once to the app's
// files/fp_vk_shaders/ with an index of every creation (order, time), to find the shader behind a GPU hang
// (shader_dump.h).
//
// Query slots (adapter setting vk_query_slots=2, per game): with multiview, Vulkan counts a query that runs in a
// multiview render pass as one query per view (N consecutive indices). Mesa's drivers (the Frame's Turnip) write a
// zero result into the extra views' slots; Meta's Quest driver doesn't. Engines that allocate one slot per query
// (Into The Radius 2, Unreal 5) then have the next object's occlusion query overwritten with "0 samples" and cull it:
// models pop in and out. With the setting, occlusion pools get N slots per query; query i uses slot N*i, so the extra
// views land in N*i+1.., and results are read from slot N*i only (view 0's count; Turnip counts every view there).
//
// Spec fixes (vk_spec_fixes=1, per game; found with the validation layer in Into The Radius 2, Unreal 5):
//  * depth/stencil images created without VK_IMAGE_USAGE_TRANSFER_DST_BIT are cleared with vkCmdClearDepthStencilImage
//    (VUID-vkCmdClearDepthStencilImage-pRanges-02659); Mesa's Turnip picks the image's compression from its usage, so
//    the flag is added at vkCreateImage.
//  * subpasses with Qualcomm's shader resolve (VK_SUBPASS_DESCRIPTION_SHADER_RESOLVE_BIT_QCOM) also name a depth
//    resolve attachment for a single-sampled depth attachment (VUID-VkSubpassDescription2-flags-04908, -03179): the
//    resolve is dropped (there is nothing to resolve from one sample).
//  * image memory barriers whose image is VK_NULL_HANDLE (Unreal 5, e.g. Metro Awakening) are left out of
//    vkCmdPipelineBarrier(2): Quest's driver skips them, the Frame's Turnip crashes on them.
#define _GNU_SOURCE
#include <vulkan/vulkan.h>
#include <android/log.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define TAG "FrameBridge"
#define EXPORT __attribute__((visibility("default")))
#define LOG(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)

static void *real_vk;
static PFN_vkGetInstanceProcAddr real_gipa;
static PFN_vkGetDeviceProcAddr real_gdpa;
static PFN_vkCreateRenderPass2 real_rp2_trampoline;
static PFN_vkCreateShaderModule real_csm_trampoline;
static pthread_once_t once = PTHREAD_ONCE_INIT;

// ---------------------------------------------------------------- shader fixes (settings)

#define MAX_FIXES 16
#define MAX_WORDS 64
static struct {
    size_t size, offset;
    uint8_t sha[32];
    uint32_t words[MAX_WORDS];
    int nwords;
} fixes[MAX_FIXES];
static int nfixes;

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
        if (ok && nw && fixes[nfixes].size % 4 == 0 && fixes[nfixes].offset % 4 == 0 &&
            fixes[nfixes].offset >= 20 && fixes[nfixes].offset <= fixes[nfixes].size)
            nfixes++;
    }
}

static int query_slots = 1;  // vk_query_slots: slots per occlusion query (1 = untouched)
static int spec_fixes;       // vk_spec_fixes: make two Unreal habits valid Vulkan (depth clears, depth resolves)
#define VALIDATION_LAYER "VK_LAYER_KHRONOS_validation"
static int validation;       // vk_validation: add Khronos' validation layer (bundled in the APK) to the instance
static int hide_fdm;         // vk_hide_fdm: the game doesn't see VK_EXT_fragment_density_map(2)
static int want_dump;        // vk_shader_dump: write the game's SPIR-V modules to files/fp_vk_shaders/
static char app_files[400];  // /sdcard/Android/data/<package>/files

static void read_settings(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[4096];
    while (fgets(line, sizeof line, f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (!strncmp(line, "vk_validation=", 14)) validation = atoi(line + 14) != 0;
        if (!strncmp(line, "vk_hide_fdm=", 12)) hide_fdm = atoi(line + 12) != 0;
        if (!strncmp(line, "vk_spec_fixes=", 14)) spec_fixes = atoi(line + 14) != 0;
        if (!strncmp(line, "vk_shader_dump=", 15)) want_dump = atoi(line + 15) != 0;
        if (!strncmp(line, "vk_query_slots=", 15)) {
            int v = atoi(line + 15);
            query_slots = v == 1 ? 2 : v >= 2 && v <= 4 ? v : 1;  // 1 = on (the settings dialog's switch) = 2 slots
        }
        if (!strncmp(line, "vk_shader_fix=", 14)) {
            nfixes = 0;  // later sources override earlier ones (as for the adapter's settings)
            parse_fixes(line + 14);
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
    char pkg[256] = {0};
    FILE *f = fopen("/proc/self/cmdline", "r");
    if (f) {
        size_t n = fread(pkg, 1, sizeof pkg - 1, f);
        pkg[n] = 0;
        fclose(f);
    }
    char *colon = strchr(pkg, ':');
    if (colon) *colon = 0;
    if (*pkg && !strchr(pkg, '/') && strlen(pkg) < 300) {
        snprintf(app_files, sizeof app_files, "/sdcard/Android/data/%s/files", pkg);
        snprintf(path, sizeof path, "%s/framebridge.conf", app_files);
        read_settings(path);
    }
    const char *env = getenv("FRAMEBRIDGE_CONFIG");
    if (env && *env) read_settings(env);
}

#include "sha256.h"  // only run for modules whose size matches a fix, or every module with vk_shader_dump
#include "shader_dump.h"

// The fixed copy of `code` (caller frees) and its size, or NULL when no fix matches.
static uint32_t *fixed_shader(const uint32_t *code, size_t size, size_t *out_size) {
    for (int i = 0; i < nfixes; i++) {
        if (fixes[i].size != size) continue;
        uint8_t digest[32];
        sha256((const uint8_t *)code, size, digest);
        if (memcmp(digest, fixes[i].sha, 32)) continue;
        size_t extra = (size_t)fixes[i].nwords * 4;
        uint32_t *copy = malloc(size + extra);
        if (!copy) return NULL;
        memcpy(copy, code, fixes[i].offset);
        memcpy((uint8_t *)copy + fixes[i].offset, fixes[i].words, extra);
        memcpy((uint8_t *)copy + fixes[i].offset + extra, (const uint8_t *)code + fixes[i].offset, size - fixes[i].offset);
        *out_size = size + extra;
        return copy;
    }
    return NULL;
}

static void init(void) {
    read_all_settings();
    if (nfixes) LOG("vk shim: %d shader fix(es) configured", nfixes);
    if (query_slots > 1) LOG("vk shim: %d slots per occlusion query", query_slots);
    if (validation) LOG("vk shim: adding %s to the instance", VALIDATION_LAYER);
    if (hide_fdm) LOG("vk shim: fragment density map extensions hidden from the game");
    if (want_dump && !dump_init(app_files)) LOG("vk shim: shader dump not possible (no package name or files dir)");
    if (spec_fixes) LOG("vk shim: Vulkan spec fixes on (depth images can be cleared, no depth resolve in shader-resolve "
                        "subpasses)");
    real_vk = dlopen("libvulkan.so", RTLD_NOW | RTLD_LOCAL);
    if (real_vk) {
        real_gipa = (PFN_vkGetInstanceProcAddr)dlsym(real_vk, "vkGetInstanceProcAddr");
        real_gdpa = (PFN_vkGetDeviceProcAddr)dlsym(real_vk, "vkGetDeviceProcAddr");
        real_rp2_trampoline = (PFN_vkCreateRenderPass2)dlsym(real_vk, "vkCreateRenderPass2");
        real_csm_trampoline = (PFN_vkCreateShaderModule)dlsym(real_vk, "vkCreateShaderModule");
    }
    LOG("vk shim: libvulkan.so %s", real_gipa && real_gdpa ? "OK" : "MISSING");
}

// ---------------------------------------------------------------- pointer checks

static pthread_mutex_t probe_lock = PTHREAD_MUTEX_INITIALIZER;
static int probe_fd[2] = {-1, -1};

// readable(p, n): the kernel copies from p without faulting (write to a pipe fails with EFAULT otherwise)
static int readable(const void *p, size_t n) {
    if (!p || ((uintptr_t)p & 7)) return 0;
    pthread_mutex_lock(&probe_lock);
    if (probe_fd[0] < 0 && pipe2(probe_fd, O_CLOEXEC | O_NONBLOCK)) {
        pthread_mutex_unlock(&probe_lock);
        return 1;  // can't check: trust the pointer (the previous behaviour)
    }
    ssize_t w = write(probe_fd[1], p, n);
    int ok = w == (ssize_t)n;
    char sink[64];
    while (w > 0) {
        ssize_t r = read(probe_fd[0], sink, w < (ssize_t)sizeof sink ? (size_t)w : sizeof sink);
        if (r <= 0) break;
        w -= r;
    }
    pthread_mutex_unlock(&probe_lock);
    return ok;
}

static int valid_next(const void *p, const VkStructureType *allowed, int n) {
    if (!readable(p, sizeof(VkBaseInStructure))) return 0;
    VkStructureType s = ((const VkBaseInStructure *)p)->sType;
    for (int i = 0; i < n; i++)
        if (allowed[i] == s) return 1;
    return 0;
}

// ---------------------------------------------------------------- scratch memory for the cleaned copies

typedef struct { void *blocks[64]; int n; } Arena;

static void *arena_dup(Arena *a, const void *src, size_t size) {
    if (a->n >= 64) return NULL;
    void *p = malloc(size ? size : 1);
    if (!p) return NULL;
    memcpy(p, src, size);
    a->blocks[a->n++] = p;
    return p;
}

static void arena_free(Arena *a) {
    for (int i = 0; i < a->n; i++) free(a->blocks[i]);
    a->n = 0;
}

static int fixes_logged;

static const VkStructureType REF_NEXT[] = {VK_STRUCTURE_TYPE_ATTACHMENT_REFERENCE_STENCIL_LAYOUT};
static const VkStructureType DESC_NEXT[] = {VK_STRUCTURE_TYPE_ATTACHMENT_DESCRIPTION_STENCIL_LAYOUT};
static const VkStructureType DEP_NEXT[] = {VK_STRUCTURE_TYPE_MEMORY_BARRIER_2};

// A copy of `refs` whose pNext pointers are valid (or NULL); the original when nothing needed fixing.
static const VkAttachmentReference2 *clean_refs(Arena *a, const VkAttachmentReference2 *refs, uint32_t count, int *fixes) {
    if (!refs || !count) return refs;
    VkAttachmentReference2 *copy = NULL;
    for (uint32_t i = 0; i < count; i++) {
        if (refs[i].pNext && !valid_next(refs[i].pNext, REF_NEXT, 1)) {
            if (!copy && !(copy = arena_dup(a, refs, sizeof *refs * count))) return refs;
            copy[i].pNext = NULL;
            (*fixes)++;
        }
    }
    return copy ? copy : refs;
}

// Structures that may be chained to a subpass or to the create info, with their size (so a chain can be cut after
// them) and whether they hold an attachment reference to clean.
static size_t chain_size(VkStructureType s) {
    switch (s) {
    case VK_STRUCTURE_TYPE_SUBPASS_DESCRIPTION_DEPTH_STENCIL_RESOLVE: return sizeof(VkSubpassDescriptionDepthStencilResolve);
    case VK_STRUCTURE_TYPE_FRAGMENT_SHADING_RATE_ATTACHMENT_INFO_KHR: return sizeof(VkFragmentShadingRateAttachmentInfoKHR);
    case VK_STRUCTURE_TYPE_MULTISAMPLED_RENDER_TO_SINGLE_SAMPLED_INFO_EXT: return sizeof(VkMultisampledRenderToSingleSampledInfoEXT);
    case VK_STRUCTURE_TYPE_RENDER_PASS_FRAGMENT_DENSITY_MAP_CREATE_INFO_EXT: return sizeof(VkRenderPassFragmentDensityMapCreateInfoEXT);
    case VK_STRUCTURE_TYPE_RENDER_PASS_MULTIVIEW_CREATE_INFO: return sizeof(VkRenderPassMultiviewCreateInfo);
    case VK_STRUCTURE_TYPE_RENDER_PASS_INPUT_ATTACHMENT_ASPECT_CREATE_INFO: return sizeof(VkRenderPassInputAttachmentAspectCreateInfo);
    case VK_STRUCTURE_TYPE_RENDER_PASS_CREATION_CONTROL_EXT: return sizeof(VkRenderPassCreationControlEXT);
    case VK_STRUCTURE_TYPE_RENDER_PASS_CREATION_FEEDBACK_CREATE_INFO_EXT: return sizeof(VkRenderPassCreationFeedbackCreateInfoEXT);
    case VK_STRUCTURE_TYPE_RENDER_PASS_SUBPASS_FEEDBACK_CREATE_INFO_EXT: return sizeof(VkRenderPassSubpassFeedbackCreateInfoEXT);
    case VK_STRUCTURE_TYPE_RENDER_PASS_STRIPE_BEGIN_INFO_ARM: return 0;  // not a create-info chain member
    default: return 0;
    }
}

// Walk a pNext chain from `head` (the pNext value of a structure we own a copy of); returns the new head. Known
// members are copied so a bad link after them can be cut; an unknown but valid member ends the checks (it can't be
// copied without knowing its size, so what follows it stays as is).
static const void *clean_chain(Arena *a, const void *head, int *fixes) {
    const void *first = head;
    VkBaseOutStructure *prev = NULL;  // our copy of the previous member
    const void *cur = head;
    while (cur) {
        if (!readable(cur, sizeof(VkBaseInStructure))) {
            if (prev) prev->pNext = NULL; else first = NULL;
            (*fixes)++;
            break;
        }
        VkStructureType s = ((const VkBaseInStructure *)cur)->sType;
        size_t size = chain_size(s);
        if (!size || !readable(cur, size)) {
            if (!size && s < 1100000000u && (s < 1000u || s >= 1000000000u)) break;  // plausible unknown member: keep
            if (prev) prev->pNext = NULL; else first = NULL;
            (*fixes)++;
            break;
        }
        VkBaseOutStructure *copy = arena_dup(a, cur, size);
        if (!copy) break;
        if (s == VK_STRUCTURE_TYPE_SUBPASS_DESCRIPTION_DEPTH_STENCIL_RESOLVE) {
            VkSubpassDescriptionDepthStencilResolve *r = (void *)copy;
            r->pDepthStencilResolveAttachment = clean_refs(a, r->pDepthStencilResolveAttachment, 1, fixes);
        } else if (s == VK_STRUCTURE_TYPE_FRAGMENT_SHADING_RATE_ATTACHMENT_INFO_KHR) {
            VkFragmentShadingRateAttachmentInfoKHR *r = (void *)copy;
            r->pFragmentShadingRateAttachment = clean_refs(a, r->pFragmentShadingRateAttachment, 1, fixes);
        }
        if (prev) prev->pNext = copy; else first = copy;
        prev = copy;
        cur = copy->pNext;
    }
    return first;
}

static int resolves_logged, sourceless_logged;

// A subpass without a depth/stencil attachment that still names a depth/stencil resolve attachment (Unreal 5, e.g.
// Metro Awakening; valid Vulkan only while that resolve is VK_ATTACHMENT_UNUSED): the Frame's Turnip reads the
// subpass's depth attachment whenever a resolve attachment is named and crashes on the NULL pointer in
// vkCreateRenderPass2. With nothing to resolve from, no resolve attachment means the same thing.
// `sp` is our copy; its known pNext members are copies too (clean_chain), the first unknown one ends the walk.
static void drop_sourceless_resolve(VkSubpassDescription2 *sp, uint32_t index, int *fixes) {
    for (VkBaseOutStructure *n = (VkBaseOutStructure *)sp->pNext; n && chain_size(n->sType); n = n->pNext) {
        if (n->sType != VK_STRUCTURE_TYPE_SUBPASS_DESCRIPTION_DEPTH_STENCIL_RESOLVE) continue;
        VkSubpassDescriptionDepthStencilResolve *r = (void *)n;
        if (!r->pDepthStencilResolveAttachment) continue;
        r->pDepthStencilResolveAttachment = NULL;
        (*fixes)++;
        if (sourceless_logged++ < 5) LOG("vk shim: dropped the depth resolve of subpass %u (no depth attachment)", index);
    }
}

static VkRenderPassCreateInfo2 clean_create_info(Arena *a, const VkRenderPassCreateInfo2 *ci, int *fixes) {
    VkRenderPassCreateInfo2 out = *ci;
    out.pNext = clean_chain(a, ci->pNext, fixes);
    if (ci->pAttachments && ci->attachmentCount) {
        VkAttachmentDescription2 *d = arena_dup(a, ci->pAttachments, sizeof *d * ci->attachmentCount);
        if (d) {
            for (uint32_t i = 0; i < ci->attachmentCount; i++)
                if (d[i].pNext && !valid_next(d[i].pNext, DESC_NEXT, 1)) { d[i].pNext = NULL; (*fixes)++; }
            out.pAttachments = d;
        }
    }
    if (ci->pSubpasses && ci->subpassCount) {
        VkSubpassDescription2 *sp = arena_dup(a, ci->pSubpasses, sizeof *sp * ci->subpassCount);
        if (sp) {
            for (uint32_t i = 0; i < ci->subpassCount; i++) {
                sp[i].pNext = clean_chain(a, sp[i].pNext, fixes);
                sp[i].pInputAttachments = clean_refs(a, sp[i].pInputAttachments, sp[i].inputAttachmentCount, fixes);
                sp[i].pColorAttachments = clean_refs(a, sp[i].pColorAttachments, sp[i].colorAttachmentCount, fixes);
                sp[i].pResolveAttachments = clean_refs(a, sp[i].pResolveAttachments, sp[i].colorAttachmentCount, fixes);
                sp[i].pDepthStencilAttachment = clean_refs(a, sp[i].pDepthStencilAttachment, 1, fixes);
                if (!sp[i].pDepthStencilAttachment) drop_sourceless_resolve(&sp[i], i, fixes);
            }
            out.pSubpasses = sp;
        }
    }
    if (spec_fixes && out.pSubpasses) {
        VkSubpassDescription2 *sp = (VkSubpassDescription2 *)out.pSubpasses;
        for (uint32_t i = 0; i < ci->subpassCount; i++) {
            if (!(sp[i].flags & 0x8 /* VK_SUBPASS_DESCRIPTION_SHADER_RESOLVE_BIT_QCOM */) || !sp[i].pDepthStencilAttachment)
                continue;
            uint32_t att = sp[i].pDepthStencilAttachment->attachment;
            if (att == VK_ATTACHMENT_UNUSED || att >= ci->attachmentCount || !ci->pAttachments ||
                ci->pAttachments[att].samples != VK_SAMPLE_COUNT_1_BIT)
                continue;
            for (VkBaseOutStructure *n = (VkBaseOutStructure *)sp[i].pNext; n; n = n->pNext) {
                if (n->sType != VK_STRUCTURE_TYPE_SUBPASS_DESCRIPTION_DEPTH_STENCIL_RESOLVE) continue;
                VkSubpassDescriptionDepthStencilResolve *r = (void *)n;
                if (r->pDepthStencilResolveAttachment &&
                    r->pDepthStencilResolveAttachment->attachment != VK_ATTACHMENT_UNUSED) {
                    r->pDepthStencilResolveAttachment = NULL;
                    (*fixes)++;
                    if (resolves_logged++ < 5) LOG("vk shim: dropped the depth resolve of shader-resolve subpass %u", i);
                }
            }
        }
    }
    if (ci->pDependencies && ci->dependencyCount) {
        VkSubpassDependency2 *dep = arena_dup(a, ci->pDependencies, sizeof *dep * ci->dependencyCount);
        if (dep) {
            for (uint32_t i = 0; i < ci->dependencyCount; i++)
                if (dep[i].pNext && !valid_next(dep[i].pNext, DEP_NEXT, 1)) { dep[i].pNext = NULL; (*fixes)++; }
            out.pDependencies = dep;
        }
    }
    return out;
}

// ---------------------------------------------------------------- entry points

#define MAX_DEVICES 8
enum { FN_RP2, FN_RP2KHR, FN_CSM, FN_CIMG, FN_CQP, FN_DQP, FN_BQ, FN_EQ, FN_CRQP, FN_RQP, FN_RQPEXT, FN_GQPR, FN_CCQPR,
      FN_PB, FN_PB2, FN_PB2KHR, FN_EDEP, FN_CDEV, FN_CIV, FN_COUNT };
static const char *const FN_NAMES[FN_COUNT] = {
    "vkCreateRenderPass2", "vkCreateRenderPass2KHR", "vkCreateShaderModule", "vkCreateImage", "vkCreateQueryPool", "vkDestroyQueryPool",
    "vkCmdBeginQuery", "vkCmdEndQuery", "vkCmdResetQueryPool", "vkResetQueryPool", "vkResetQueryPoolEXT",
    "vkGetQueryPoolResults", "vkCmdCopyQueryPoolResults", "vkCmdPipelineBarrier", "vkCmdPipelineBarrier2",
    "vkCmdPipelineBarrier2KHR", "vkEnumerateDeviceExtensionProperties", "vkCreateDevice", "vkCreateImageView"};
static struct { VkDevice device; PFN_vkVoidFunction fn[FN_COUNT]; } devices[MAX_DEVICES];
static PFN_vkCreateRenderPass2 fallback_rp2khr;  // from vkGetInstanceProcAddr
static pthread_mutex_t dev_lock = PTHREAD_MUTEX_INITIALIZER;

// The next implementation of a wrapped device function: the one vkGetDeviceProcAddr returned for this device, else
// a fresh lookup, else the loader's trampoline.
static PFN_vkVoidFunction device_fn(VkDevice device, int which) {
    PFN_vkVoidFunction fn = NULL;
    pthread_mutex_lock(&dev_lock);
    for (int i = 0; i < MAX_DEVICES; i++)
        if (devices[i].device == device) { fn = devices[i].fn[which]; break; }
    pthread_mutex_unlock(&dev_lock);
    if (!fn && real_gdpa) fn = real_gdpa(device, FN_NAMES[which]);
    return fn;
}

static PFN_vkCreateRenderPass2 real_for(VkDevice device, int khr) {
    PFN_vkCreateRenderPass2 fn = (PFN_vkCreateRenderPass2)device_fn(device, khr ? FN_RP2KHR : FN_RP2);
    if (!fn) fn = khr ? fallback_rp2khr : real_rp2_trampoline;
    return fn;
}

static VkResult create_render_pass2(VkDevice device, const VkRenderPassCreateInfo2 *ci, const VkAllocationCallbacks *alloc,
                                    VkRenderPass *rp, int khr) {
    PFN_vkCreateRenderPass2 real = real_for(device, khr);
    if (!real) return VK_ERROR_INITIALIZATION_FAILED;
    if (!ci) return real(device, ci, alloc, rp);
    Arena a = {0};
    int fixes = 0;
    VkRenderPassCreateInfo2 clean = clean_create_info(&a, ci, &fixes);
    if (fixes && fixes_logged < 5) {
        fixes_logged++;
        LOG("vk shim: dropped %d invalid pNext pointer(s) in vkCreateRenderPass2%s", fixes, khr ? "KHR" : "");
    }
    VkResult r = real(device, fixes ? &clean : ci, alloc, rp);
    arena_free(&a);
    return r;
}

EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkCreateRenderPass2(VkDevice device, const VkRenderPassCreateInfo2 *ci,
                                                          const VkAllocationCallbacks *alloc, VkRenderPass *rp) {
    pthread_once(&once, init);
    return create_render_pass2(device, ci, alloc, rp, 0);
}

static VKAPI_ATTR VkResult VKAPI_CALL create_render_pass2_khr(VkDevice device, const VkRenderPassCreateInfo2 *ci,
                                                             const VkAllocationCallbacks *alloc, VkRenderPass *rp) {
    return create_render_pass2(device, ci, alloc, rp, 1);
}

static int fixes_applied;

EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkCreateShaderModule(VkDevice device, const VkShaderModuleCreateInfo *ci,
                                                           const VkAllocationCallbacks *alloc, VkShaderModule *module) {
    pthread_once(&once, init);
    PFN_vkCreateShaderModule real = (PFN_vkCreateShaderModule)device_fn(device, FN_CSM);
    if (!real) real = real_csm_trampoline;
    if (!real) return VK_ERROR_INITIALIZATION_FAILED;
    if (dump_on && ci) dump_module(ci->pCode, ci->codeSize);  // the game's own module, before any fix
    size_t size = 0;
    uint32_t *code = nfixes && ci && ci->pCode ? fixed_shader(ci->pCode, ci->codeSize, &size) : NULL;
    if (!code) return real(device, ci, alloc, module);
    VkShaderModuleCreateInfo copy = *ci;  // the caller's create info and code stay untouched
    copy.codeSize = size;
    copy.pCode = code;
    VkResult r = real(device, &copy, alloc, module);
    free(code);
    if (fixes_applied++ < 20) LOG("vk shim: fixed shader module (%zu -> %zu bytes): %d", ci->codeSize, size, r);
    return r;
}

// ---------------------------------------------------------------- query slots

// Command buffer functions carry no VkDevice: their next implementation is the one the (single) game device got.
static PFN_vkVoidFunction cmd_fn[FN_COUNT];
static PFN_vkVoidFunction any_fn(VkDevice device, int which) {
    PFN_vkVoidFunction fn = device ? device_fn(device, which) : NULL;
    if (!fn) fn = cmd_fn[which];
    if (!fn && real_vk) fn = (PFN_vkVoidFunction)dlsym(real_vk, FN_NAMES[which]);
    return fn;
}

#define MAX_POOLS 4096
static uint64_t occlusion_pools[MAX_POOLS];  // open addressing; 0 = empty, 1 = deleted
static pthread_mutex_t pool_lock = PTHREAD_MUTEX_INITIALIZER;
static int pools_logged;

static unsigned pool_hash(uint64_t h) { return (unsigned)((h * 0x9E3779B97F4A7C15ull) >> 52) % MAX_POOLS; }

static void pool_add(VkQueryPool pool) {
    uint64_t h = (uint64_t)pool;
    pthread_mutex_lock(&pool_lock);
    for (unsigned i = pool_hash(h), n = 0; n < MAX_POOLS; i = (i + 1) % MAX_POOLS, n++)
        if (occlusion_pools[i] <= 1) { occlusion_pools[i] = h; break; }
    pthread_mutex_unlock(&pool_lock);
}

static int pool_slot(VkQueryPool pool, int remove) {
    uint64_t h = (uint64_t)pool;
    if (h <= 1) return 0;
    int found = 0;
    pthread_mutex_lock(&pool_lock);
    for (unsigned i = pool_hash(h), n = 0; n < MAX_POOLS && occlusion_pools[i]; i = (i + 1) % MAX_POOLS, n++)
        if (occlusion_pools[i] == h) {
            found = 1;
            if (remove) occlusion_pools[i] = 1;
            break;
        }
    pthread_mutex_unlock(&pool_lock);
    return found;
}

// the remapped first query of `pool` (unchanged for pools that aren't remapped)
static uint32_t q(VkQueryPool pool, uint32_t query) { return pool_slot(pool, 0) ? query * (uint32_t)query_slots : query; }

static VKAPI_ATTR VkResult VKAPI_CALL create_query_pool(VkDevice device, const VkQueryPoolCreateInfo *ci,
                                                       const VkAllocationCallbacks *alloc, VkQueryPool *pool) {
    PFN_vkCreateQueryPool real = (PFN_vkCreateQueryPool)any_fn(device, FN_CQP);
    if (!real) return VK_ERROR_INITIALIZATION_FAILED;
    if (!ci || ci->queryType != VK_QUERY_TYPE_OCCLUSION || query_slots < 2) return real(device, ci, alloc, pool);
    VkQueryPoolCreateInfo copy = *ci;
    copy.queryCount = ci->queryCount * (uint32_t)query_slots;
    VkResult r = real(device, &copy, alloc, pool);
    if (r == VK_SUCCESS) {
        pool_add(*pool);
        if (pools_logged++ < 5)
            LOG("vk shim: occlusion query pool %u -> %u slots", ci->queryCount, copy.queryCount);
    }
    return r;
}

static VKAPI_ATTR void VKAPI_CALL destroy_query_pool(VkDevice device, VkQueryPool pool, const VkAllocationCallbacks *alloc) {
    PFN_vkDestroyQueryPool real = (PFN_vkDestroyQueryPool)any_fn(device, FN_DQP);
    pool_slot(pool, 1);
    if (real) real(device, pool, alloc);
}

static VKAPI_ATTR void VKAPI_CALL cmd_begin_query(VkCommandBuffer cb, VkQueryPool pool, uint32_t query,
                                                 VkQueryControlFlags flags) {
    PFN_vkCmdBeginQuery real = (PFN_vkCmdBeginQuery)any_fn(NULL, FN_BQ);
    if (real) real(cb, pool, q(pool, query), flags);
}

static VKAPI_ATTR void VKAPI_CALL cmd_end_query(VkCommandBuffer cb, VkQueryPool pool, uint32_t query) {
    PFN_vkCmdEndQuery real = (PFN_vkCmdEndQuery)any_fn(NULL, FN_EQ);
    if (real) real(cb, pool, q(pool, query));
}

static uint32_t span(VkQueryPool pool, uint32_t count) { return pool_slot(pool, 0) ? count * (uint32_t)query_slots : count; }

static VKAPI_ATTR void VKAPI_CALL cmd_reset_query_pool(VkCommandBuffer cb, VkQueryPool pool, uint32_t first, uint32_t count) {
    PFN_vkCmdResetQueryPool real = (PFN_vkCmdResetQueryPool)any_fn(NULL, FN_CRQP);
    if (real) real(cb, pool, q(pool, first), span(pool, count));
}

static void reset_query_pool(VkDevice device, VkQueryPool pool, uint32_t first, uint32_t count, int ext) {
    PFN_vkResetQueryPool real = (PFN_vkResetQueryPool)any_fn(device, ext ? FN_RQPEXT : FN_RQP);
    if (real) real(device, pool, q(pool, first), span(pool, count));
}
static VKAPI_ATTR void VKAPI_CALL host_reset_query_pool(VkDevice d, VkQueryPool p, uint32_t f, uint32_t c) { reset_query_pool(d, p, f, c, 0); }
static VKAPI_ATTR void VKAPI_CALL host_reset_query_pool_ext(VkDevice d, VkQueryPool p, uint32_t f, uint32_t c) { reset_query_pool(d, p, f, c, 1); }

// Results come from slot N*i of each query, one query at a time: the extra slots are only written in multiview passes,
// so reading them (with VK_QUERY_RESULT_WAIT_BIT) could wait forever.
static VKAPI_ATTR VkResult VKAPI_CALL get_query_pool_results(VkDevice device, VkQueryPool pool, uint32_t first, uint32_t count,
                                                            size_t size, void *data, VkDeviceSize stride,
                                                            VkQueryResultFlags flags) {
    PFN_vkGetQueryPoolResults real = (PFN_vkGetQueryPoolResults)any_fn(device, FN_GQPR);
    if (!real) return VK_ERROR_INITIALIZATION_FAILED;
    if (!pool_slot(pool, 0) || !count) return real(device, pool, first, count, size, data, stride, flags);
    VkResult result = VK_SUCCESS;
    for (uint32_t i = 0; i < count; i++) {
        size_t offset = (size_t)(i * stride);
        if (offset >= size) break;
        VkResult r = real(device, pool, (first + i) * (uint32_t)query_slots, 1, size - offset, (uint8_t *)data + offset,
                          stride, flags);
        if (r < 0) return r;
        if (r == VK_NOT_READY) result = VK_NOT_READY;
    }
    return result;
}

static VKAPI_ATTR void VKAPI_CALL cmd_copy_query_pool_results(VkCommandBuffer cb, VkQueryPool pool, uint32_t first,
                                                             uint32_t count, VkBuffer buffer, VkDeviceSize offset,
                                                             VkDeviceSize stride, VkQueryResultFlags flags) {
    PFN_vkCmdCopyQueryPoolResults real = (PFN_vkCmdCopyQueryPoolResults)any_fn(NULL, FN_CCQPR);
    if (!real) return;
    if (!pool_slot(pool, 0)) { real(cb, pool, first, count, buffer, offset, stride, flags); return; }
    for (uint32_t i = 0; i < count; i++)
        real(cb, pool, (first + i) * (uint32_t)query_slots, 1, buffer, offset + i * stride, stride, flags);
}

// ---------------------------------------------------------------- spec fixes: depth images

static int is_depth_format(VkFormat f) {
    return f == VK_FORMAT_D16_UNORM || f == VK_FORMAT_X8_D24_UNORM_PACK32 || f == VK_FORMAT_D32_SFLOAT ||
           f == VK_FORMAT_S8_UINT || f == VK_FORMAT_D16_UNORM_S8_UINT || f == VK_FORMAT_D24_UNORM_S8_UINT ||
           f == VK_FORMAT_D32_SFLOAT_S8_UINT;
}
static int images_logged;

static VKAPI_ATTR VkResult VKAPI_CALL create_image(VkDevice device, const VkImageCreateInfo *ci,
                                                  const VkAllocationCallbacks *alloc, VkImage *image) {
    PFN_vkCreateImage real = (PFN_vkCreateImage)any_fn(device, FN_CIMG);
    if (!real) return VK_ERROR_INITIALIZATION_FAILED;
    if (!spec_fixes || !ci || !is_depth_format(ci->format) || !(ci->usage & VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT) ||
        (ci->usage & (VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSIENT_ATTACHMENT_BIT)))  // transient: no
        return real(device, ci, alloc, image);
    VkImageCreateInfo copy = *ci;
    copy.usage |= VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    VkResult r = real(device, &copy, alloc, image);
    if (images_logged++ < 5) LOG("vk shim: depth image %ux%u format %d can be cleared now: %d", ci->extent.width,
                                 ci->extent.height, ci->format, r);
    if (r != VK_SUCCESS) r = real(device, ci, alloc, image);  // the flag isn't allowed here: as the game asked
    return r;
}

EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkCreateImage(VkDevice d, const VkImageCreateInfo *ci, const VkAllocationCallbacks *a,
                                                    VkImage *img) {
    pthread_once(&once, init);
    return create_image(d, ci, a, img);
}

// ---------------------------------------------------------------- validation (diagnostics)

static PFN_vkCreateInstance real_ci;

static VKAPI_ATTR VkResult VKAPI_CALL create_instance(const VkInstanceCreateInfo *ci, const VkAllocationCallbacks *a,
                                                    VkInstance *inst) {
    if (!real_ci && real_vk) real_ci = (PFN_vkCreateInstance)dlsym(real_vk, "vkCreateInstance");
    if (!real_ci) return VK_ERROR_INITIALIZATION_FAILED;
    if (!validation || !ci) return real_ci(ci, a, inst);
    for (uint32_t i = 0; i < ci->enabledLayerCount; i++)
        if (!strcmp(ci->ppEnabledLayerNames[i], VALIDATION_LAYER)) return real_ci(ci, a, inst);
    const char **names = malloc(sizeof(char *) * (ci->enabledLayerCount + 1));
    if (!names) return real_ci(ci, a, inst);
    for (uint32_t i = 0; i < ci->enabledLayerCount; i++) names[i] = ci->ppEnabledLayerNames[i];
    names[ci->enabledLayerCount] = VALIDATION_LAYER;
    VkInstanceCreateInfo copy = *ci;
    copy.enabledLayerCount = ci->enabledLayerCount + 1;
    copy.ppEnabledLayerNames = names;
    VkResult r = real_ci(&copy, a, inst);
    LOG("vk shim: vkCreateInstance with %s: %d", VALIDATION_LAYER, r);
    if (r == VK_ERROR_LAYER_NOT_PRESENT) r = real_ci(ci, a, inst);  // not bundled: start without it
    free(names);
    return r;
}

EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkCreateInstance(const VkInstanceCreateInfo *ci, const VkAllocationCallbacks *a,
                                                       VkInstance *inst) {
    pthread_once(&once, init);
    return create_instance(ci, a, inst);
}

// The same entry points for engines that dlsym them from the shim instead of asking vkGet*ProcAddr
EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkCreateQueryPool(VkDevice d, const VkQueryPoolCreateInfo *ci,
                                                        const VkAllocationCallbacks *a, VkQueryPool *p) {
    pthread_once(&once, init);
    return create_query_pool(d, ci, a, p);
}
EXPORT VKAPI_ATTR void VKAPI_CALL vkDestroyQueryPool(VkDevice d, VkQueryPool p, const VkAllocationCallbacks *a) {
    pthread_once(&once, init);
    destroy_query_pool(d, p, a);
}
EXPORT VKAPI_ATTR void VKAPI_CALL vkCmdBeginQuery(VkCommandBuffer cb, VkQueryPool p, uint32_t i, VkQueryControlFlags f) {
    pthread_once(&once, init);
    cmd_begin_query(cb, p, i, f);
}
EXPORT VKAPI_ATTR void VKAPI_CALL vkCmdEndQuery(VkCommandBuffer cb, VkQueryPool p, uint32_t i) {
    pthread_once(&once, init);
    cmd_end_query(cb, p, i);
}
EXPORT VKAPI_ATTR void VKAPI_CALL vkCmdResetQueryPool(VkCommandBuffer cb, VkQueryPool p, uint32_t f, uint32_t c) {
    pthread_once(&once, init);
    cmd_reset_query_pool(cb, p, f, c);
}
EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkGetQueryPoolResults(VkDevice d, VkQueryPool p, uint32_t f, uint32_t c, size_t s,
                                                            void *data, VkDeviceSize st, VkQueryResultFlags fl) {
    pthread_once(&once, init);
    return get_query_pool_results(d, p, f, c, s, data, st, fl);
}
EXPORT VKAPI_ATTR void VKAPI_CALL vkCmdCopyQueryPoolResults(VkCommandBuffer cb, VkQueryPool p, uint32_t f, uint32_t c,
                                                           VkBuffer b, VkDeviceSize o, VkDeviceSize st,
                                                           VkQueryResultFlags fl) {
    pthread_once(&once, init);
    cmd_copy_query_pool_results(cb, p, f, c, b, o, st, fl);
}

// ---------------------------------------------------------------- fragment density map (vk_hide_fdm)

// Unreal 5 turns on fragment-density-map foveation when the driver offers VK_EXT_fragment_density_map (the Frame's
// Turnip does) and expects the density map from the headset's runtime, which the Frame doesn't provide (e.g. Metro
// Awakening: image views and barriers for a VK_NULL_HANDLE image, then a crash). Hidden, the game renders without it;
// Valve's own FDM layer sits below this shim and isn't affected.
static int is_fdm_extension(const char *name) { return strstr(name, "fragment_density_map") != NULL; }

EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkEnumerateDeviceExtensionProperties(VkPhysicalDevice pd, const char *layer,
                                                                          uint32_t *count, VkExtensionProperties *props) {
    pthread_once(&once, init);
    PFN_vkEnumerateDeviceExtensionProperties real = (PFN_vkEnumerateDeviceExtensionProperties)any_fn(NULL, FN_EDEP);
    if (!real) return VK_ERROR_INITIALIZATION_FAILED;
    if (!hide_fdm || layer || !count) return real(pd, layer, count, props);
    uint32_t n = 0;
    VkResult r = real(pd, NULL, &n, NULL);
    if (r != VK_SUCCESS) return r;
    VkExtensionProperties *all = calloc(n ? n : 1, sizeof *all);
    if (!all) return VK_ERROR_OUT_OF_HOST_MEMORY;
    r = real(pd, NULL, &n, all);
    if (r < 0) { free(all); return r; }
    uint32_t kept = 0;
    for (uint32_t i = 0; i < n; i++)
        if (!is_fdm_extension(all[i].extensionName)) all[kept++] = all[i];
    if (!props) {
        *count = kept;
        free(all);
        return VK_SUCCESS;
    }
    uint32_t c = *count < kept ? *count : kept;
    memcpy(props, all, c * sizeof *props);
    *count = c;
    free(all);
    return c < kept ? VK_INCOMPLETE : VK_SUCCESS;
}

EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkCreateDevice(VkPhysicalDevice pd, const VkDeviceCreateInfo *ci,
                                                    const VkAllocationCallbacks *alloc, VkDevice *device) {
    pthread_once(&once, init);
    PFN_vkCreateDevice real = (PFN_vkCreateDevice)any_fn(NULL, FN_CDEV);
    if (!real) return VK_ERROR_INITIALIZATION_FAILED;
    if (!ci || (!hide_fdm && !spec_fixes)) return real(pd, ci, alloc, device);
    char names[1024] = "";
    VkDeviceCreateInfo copy = *ci;
    const char **kept = calloc(ci->enabledExtensionCount + 1, sizeof *kept);
    if (!kept) return VK_ERROR_OUT_OF_HOST_MEMORY;
    copy.enabledExtensionCount = 0;
    for (uint32_t i = 0; i < ci->enabledExtensionCount; i++) {
        const char *e = ci->ppEnabledExtensionNames[i];
        size_t len = strlen(names);
        snprintf(names + len, sizeof names - len, "%s%s", len ? " " : "", e + (strncmp(e, "VK_", 3) ? 0 : 3));
        if (hide_fdm && is_fdm_extension(e)) continue;
        kept[copy.enabledExtensionCount++] = e;
    }
    copy.ppEnabledExtensionNames = kept;
    LOG("vk shim: vkCreateDevice with %u extension(s): %s", ci->enabledExtensionCount, names);
    VkResult r = real(pd, &copy, alloc, device);
    free(kept);
    return r;
}

static int null_views_logged;

// An image view of VK_NULL_HANDLE crashes the Frame's Turnip (it reads the image); with vk_spec_fixes it fails with
// an error instead, which names the call in the game's own log.
static VKAPI_ATTR VkResult VKAPI_CALL create_image_view(VkDevice device, const VkImageViewCreateInfo *ci,
                                                       const VkAllocationCallbacks *alloc, VkImageView *view) {
    PFN_vkCreateImageView real = (PFN_vkCreateImageView)any_fn(device, FN_CIV);
    if (!real) return VK_ERROR_INITIALIZATION_FAILED;
    if (!ci || ci->image) return real(device, ci, alloc, view);
    if (null_views_logged++ < 10)
        LOG("vk shim: refused an image view without an image (format %d, view type %d, aspect 0x%x, layers %u)",
            ci->format, ci->viewType, ci->subresourceRange.aspectMask, ci->subresourceRange.layerCount);
    if (view) *view = VK_NULL_HANDLE;
    return VK_ERROR_INITIALIZATION_FAILED;
}

// ---------------------------------------------------------------- image barriers without an image (vk_spec_fixes)

// Unreal 5 (e.g. Metro Awakening) records image memory barriers whose image is VK_NULL_HANDLE (invalid Vulkan,
// VUID-VkImageMemoryBarrier-image-parameter). Quest's driver skips them; the Frame's Turnip reads the image and
// crashes in vkCmdPipelineBarrier. They change nothing, so they are left out.
static int null_barriers_logged;

// The barriers without the ones naming no image: `src` itself when there are none, else a copy in `buf` (or a malloc'd
// one in *heap, which the caller frees); *count is updated.
static const void *without_null_images(const void *src, uint32_t *count, size_t size, size_t image_offset, void *buf,
                                       size_t buf_size, void **heap) {
    uint32_t n = *count, kept = 0;
    for (uint32_t i = 0; i < n; i++)
        if (*(const uint64_t *)((const char *)src + i * size + image_offset)) kept++;
    if (kept == n) return src;
    char *out = n * size <= buf_size ? buf : (*heap = malloc(n * size));
    if (!out) return src;
    for (uint32_t i = 0, j = 0; i < n; i++)
        if (*(const uint64_t *)((const char *)src + i * size + image_offset)) memcpy(out + j++ * size, (const char *)src + i * size, size);
    if (null_barriers_logged++ < 5) {
        // in both barrier versions oldLayout/newLayout sit 16 bytes before the image, the subresource range 8 after it
        const char *first = NULL;
        for (uint32_t i = 0; i < n && !first; i++)
            if (!*(const uint64_t *)((const char *)src + i * size + image_offset)) first = (const char *)src + i * size;
        const uint32_t *layouts = (const uint32_t *)(first + image_offset - 16);  // oldLayout, newLayout, srcQFI, dstQFI
        const uint32_t *range = (const uint32_t *)(first + image_offset + 8);     // aspect, mip, levels, layer, layers
        LOG("vk shim: left out %u image barrier(s) without an image (first: layout %u -> %u, aspect 0x%x, layers %u)",
            n - kept, layouts[0], layouts[1], range[0], range[4]);
    }
    *count = kept;
    return out;
}

static VKAPI_ATTR void VKAPI_CALL cmd_pipeline_barrier(VkCommandBuffer cb, VkPipelineStageFlags src, VkPipelineStageFlags dst,
                                                       VkDependencyFlags flags, uint32_t memory_count,
                                                       const VkMemoryBarrier *memory, uint32_t buffer_count,
                                                       const VkBufferMemoryBarrier *buffers, uint32_t image_count,
                                                       const VkImageMemoryBarrier *images) {
    PFN_vkCmdPipelineBarrier real = (PFN_vkCmdPipelineBarrier)any_fn(NULL, FN_PB);
    if (!real) return;
    VkImageMemoryBarrier buf[16];
    void *heap = NULL;
    const VkImageMemoryBarrier *kept = images && image_count
        ? without_null_images(images, &image_count, sizeof *images, offsetof(VkImageMemoryBarrier, image), buf, sizeof buf, &heap)
        : images;
    real(cb, src, dst, flags, memory_count, memory, buffer_count, buffers, image_count, kept);
    free(heap);
}

static void pipeline_barrier2(VkCommandBuffer cb, const VkDependencyInfo *info, int which) {
    PFN_vkCmdPipelineBarrier2 real = (PFN_vkCmdPipelineBarrier2)any_fn(NULL, which);
    if (!real) return;
    if (!info || !info->pImageMemoryBarriers || !info->imageMemoryBarrierCount) {
        real(cb, info);
        return;
    }
    VkImageMemoryBarrier2 buf[16];
    void *heap = NULL;
    VkDependencyInfo copy = *info;
    copy.pImageMemoryBarriers = without_null_images(info->pImageMemoryBarriers, &copy.imageMemoryBarrierCount,
                                                    sizeof *buf, offsetof(VkImageMemoryBarrier2, image), buf, sizeof buf, &heap);
    real(cb, &copy);
    free(heap);
}

static VKAPI_ATTR void VKAPI_CALL cmd_pipeline_barrier2(VkCommandBuffer cb, const VkDependencyInfo *info) {
    pipeline_barrier2(cb, info, FN_PB2);
}

static VKAPI_ATTR void VKAPI_CALL cmd_pipeline_barrier2_khr(VkCommandBuffer cb, const VkDependencyInfo *info) {
    pipeline_barrier2(cb, info, FN_PB2KHR);
}

static int wrapped_index(const char *name) {
    for (int i = 0; i < FN_COUNT; i++)
        if (!strcmp(name, FN_NAMES[i])) return i;
    return -1;
}

static PFN_vkVoidFunction wrap(const char *name) {
    switch (wrapped_index(name)) {
    case FN_RP2: return (PFN_vkVoidFunction)vkCreateRenderPass2;
    case FN_RP2KHR: return (PFN_vkVoidFunction)create_render_pass2_khr;
    case FN_CSM: return nfixes || dump_on ? (PFN_vkVoidFunction)vkCreateShaderModule : NULL;  // else no detour
    case FN_CIMG: return spec_fixes ? (PFN_vkVoidFunction)create_image : NULL;
    case FN_PB: return spec_fixes ? (PFN_vkVoidFunction)cmd_pipeline_barrier : NULL;
    case FN_PB2: return spec_fixes ? (PFN_vkVoidFunction)cmd_pipeline_barrier2 : NULL;
    case FN_PB2KHR: return spec_fixes ? (PFN_vkVoidFunction)cmd_pipeline_barrier2_khr : NULL;
    case FN_EDEP: return hide_fdm ? (PFN_vkVoidFunction)vkEnumerateDeviceExtensionProperties : NULL;
    case FN_CDEV: return hide_fdm || spec_fixes ? (PFN_vkVoidFunction)vkCreateDevice : NULL;
    case FN_CIV: return spec_fixes ? (PFN_vkVoidFunction)create_image_view : NULL;
    default: break;
    }
    if (query_slots < 2) return NULL;  // no query remapping: no detours
    switch (wrapped_index(name)) {
    case FN_CQP: return (PFN_vkVoidFunction)create_query_pool;
    case FN_DQP: return (PFN_vkVoidFunction)destroy_query_pool;
    case FN_BQ: return (PFN_vkVoidFunction)cmd_begin_query;
    case FN_EQ: return (PFN_vkVoidFunction)cmd_end_query;
    case FN_CRQP: return (PFN_vkVoidFunction)cmd_reset_query_pool;
    case FN_RQP: return (PFN_vkVoidFunction)host_reset_query_pool;
    case FN_RQPEXT: return (PFN_vkVoidFunction)host_reset_query_pool_ext;
    case FN_GQPR: return (PFN_vkVoidFunction)get_query_pool_results;
    case FN_CCQPR: return (PFN_vkVoidFunction)cmd_copy_query_pool_results;
    default: return NULL;
    }
}

EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(VkDevice device, const char *name) {
    pthread_once(&once, init);
    if (!real_gdpa || !name) return NULL;
    PFN_vkVoidFunction fn = real_gdpa(device, name);
    PFN_vkVoidFunction w = fn ? wrap(name) : NULL;
    if (!w) return fn;
    int which = wrapped_index(name);
    pthread_mutex_lock(&dev_lock);
    int slot = -1;
    for (int i = 0; i < MAX_DEVICES; i++) {
        if (devices[i].device == device) { slot = i; break; }
        if (slot < 0 && !devices[i].device) slot = i;
    }
    if (slot >= 0) {
        devices[slot].device = device;
        devices[slot].fn[which] = fn;
    }
    cmd_fn[which] = fn;
    pthread_mutex_unlock(&dev_lock);
    return w;
}

EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(VkInstance instance, const char *name) {
    pthread_once(&once, init);
    if (!real_gipa || !name) return NULL;
    if (!strcmp(name, "vkGetDeviceProcAddr")) return (PFN_vkVoidFunction)vkGetDeviceProcAddr;
    if (!strcmp(name, "vkGetInstanceProcAddr")) return (PFN_vkVoidFunction)vkGetInstanceProcAddr;
    if (!strcmp(name, "vkCreateInstance") && validation) return (PFN_vkVoidFunction)vkCreateInstance;
    PFN_vkVoidFunction fn = real_gipa(instance, name);
    PFN_vkVoidFunction w = fn ? wrap(name) : NULL;
    if (!w) return fn;
    if (!strcmp(name, "vkCreateRenderPass2KHR")) fallback_rp2khr = (PFN_vkCreateRenderPass2)fn;
    int which = wrapped_index(name);
    if (which >= FN_CQP && !cmd_fn[which]) cmd_fn[which] = fn;
    return w;
}
