// SPDX-License-Identifier: GPL-3.0-only
// VK_LAYER_FRAMEPORT_fdm_centre (GitHub #394): puts the full-density area of Valve's foveation on each eye.
//
// Valve's VALVE_fdm_injection layer gives every eye swapchain a fragment density map sized to the WHOLE swapchain
// (ceil(w/32) x ceil(h/32) texels, one array layer per eye) and draws its preset rectangles centred on that map,
// i.e. on the swapchain's centre. The eye's view is the imageRect the game submits, often smaller than the swapchain
// and placed off-centre (dynamic resolution: a corner-anchored rect; asymmetric eyes: rect=0,0 / rect=287,0 inside a
// wider swapchain), and the eye's optical axis isn't the rect's centre either (asymmetric FOV, e.g. -1.03/+0.88 rad).
// So the sharp area sat towards the swapchain centre: off to one side or the bottom right of what the eye sees.
//
// This layer runs BELOW Valve's (inserted right after it in Android's debug-layer list), so it sees the render passes
// with Valve's density map attached and the per-view density-map offsets Valve passes at the end of the pass
// (VK_EXT/QCOM_fragment_density_map_offset: the map is shifted by the offset). It replaces each view's offset with
// one that puts the map's sharp centre on that eye's optical centre (Fixed: no gaze; Valve passes no offsets then and
// this layer adds them) or on the gaze point inside the eye's rect (Eye-tracked: Valve's gaze offset is measured from
// the swapchain centre and scaled by the render area; it is re-based on the rect). FrameBridge reports the views
// (fp_fdm_set_views, every xrEndFrame): swapchain size, array layer, imageRect and the optical centre, all in the
// Vulkan image's pixel rows. Passes are matched to views by framebuffer size and layer, like Valve matches maps.
// Off with FRAMEPORT_FDM_CENTRE=0 (developers: compare against Valve's placement).
// FRAMEPORT_FDM_DIAG=1 (as LEPTON_ENV_, off by default) logs, under the same tag, every view's offset as handed down
// and which pipelines bound inside a foveated pass would make Turnip fall back to one shared viewport scale for all
// views (pipeline without the density-map flag, or a pre-rasterization shader writing ViewportIndex): Turnip then
// gives every view the finest fragment area of all views in each bin (tu_calc_frag_area, rp.shared_viewport), so
// each eye gets the union of both eyes' sharp areas (GitHub #394, In Death / The 7th Guest).
//
// Bins (#394, second round): Turnip renders each GMEM bin at the density the map has at the bin's centre, on a grid
// anchored in map space, so the full-density area is the bins whose centre falls in Valve's sharp texels; their centre
// is off the texels' centre by up to half a bin (In Death: -80,+32 px). The layer computes Turnip's bin size for each
// foveated pass the way Mesa does (tu_bins.h; Adreno 750 only) and centres those bins on the eye; the per-game setting
// fdm_bin_w/fdm_bin_h (FrameBridge -> fp_fdm_set_bin) override it. Undecided (another GPU, an unmodelled pass): the texels'
// centre as before.
#define CENTRE_NAME "VK_LAYER_FRAMEPORT_fdm_centre"
#define FP_FDM_MAX_VIEWS 4
#include "tu_bins.h"

typedef struct {
    uint32_t width, height, layer;   // swapchain image size, array layer
    int32_t x, y, w, h;              // imageRect in Vulkan image rows (top-down)
    float cx, cy;                    // optical centre (straight-ahead direction) in the same pixels
} fp_fdm_view;

static fp_fdm_view c_views[FP_FDM_MAX_VIEWS];
static uint32_t c_view_count;
static int c_lock;
static int c_level = 1;      // Valve's preset: 1 lo, 2 med, 3 hi (FOVE_LEVEL / FDM_DEBUG, as Valve reads them)
static int c_fixed;          // FramePort's Fixed mode (FDM_DEBUG had disable_offsets): ignore Valve's gaze
static int c_disabled;       // FRAMEPORT_FDM_CENTRE=0
static int c_diag;           // FRAMEPORT_FDM_DIAG=1: offsets and shared-viewport causes in the log
static const tb_gpu *c_gpu;  // Turnip on a GPU tu_bins.h knows: bin sizes can be computed
static uint32_t c_bin_w, c_bin_h;  // fdm_bin_w/fdm_bin_h (per game, from FrameBridge): the bin size, not computed

EXPORT void fp_fdm_set_bin(uint32_t width, uint32_t height) {
    c_bin_w = width <= 4096 ? width : 0, c_bin_h = height <= 4096 ? height : 0;
}

EXPORT void fp_fdm_set_views(const fp_fdm_view *views, uint32_t count) {
    if (count > FP_FDM_MAX_VIEWS) count = FP_FDM_MAX_VIEWS;
    take_lock(&c_lock);
    if (views && count) memcpy(c_views, views, count * sizeof(*views));
    c_view_count = views ? count : 0;
    drop_lock(&c_lock);
}

// Valve's full-density rectangle per preset (x, y, w, h in map-normalized coordinates; texel i is sampled at
// i / (texels - 1)), from libVkLayer_VALVE_fdm_injection.so (SteamOS 0.5.x, 2026-10).
static const float c_preset[4][4] = {{0, 0, 0, 0}, {.364f, .367f, .295f, .286f}, {.386f, .388f, .25f, .245f},
                                     {.386f, .404f, .246f, .211f}};

// Where Turnip puts the centre of Valve's sharp texels, in framebuffer pixels: Turnip samples the map with a texel
// size of 2^ceil(log2(framebuffer / map)) pixels (at least 32), see tu_fragment_density_map_sample().
// Valve's sharp texels in framebuffer pixels: [lo, hi)
static void c_sharp_range(uint32_t fb, uint32_t texels, int axis, uint32_t *lo_px, uint32_t *hi_px) {
    uint32_t ratio = fb / texels, shift = 0;
    while ((1u << shift) < ratio) ++shift;
    if (shift < 5) shift = 5;
    if (shift > 10) shift = 10;
    float lo = c_preset[c_level][axis], hi = lo + c_preset[c_level][axis + 2];
    float n = (float)(texels > 1 ? texels - 1 : 1);
    int first = (int)ceilf(lo * n - 1e-4f), last = (int)floorf(hi * n + 1e-4f);
    if (last < first) last = first;
    *lo_px = (uint32_t)first << shift, *hi_px = (uint32_t)(last + 1) << shift;
}
static float c_valve_centre(uint32_t fb, uint32_t texels, int axis) {
    if (!texels) return fb / 2.0f;
    uint32_t lo, hi;
    c_sharp_range(fb, texels, axis, &lo, &hi);
    return (float)(lo + hi) / 2.0f;
}
// The centre of the bins Turnip renders at full density (bin: Turnip's bin size along the axis, 0 = unknown); the
// texels' centre when unknown or no bin centre falls inside the sharp texels.
static float c_sharp_centre(uint32_t fb, uint32_t texels, int axis, uint32_t bin) {
    if (!texels || !bin) return c_valve_centre(fb, texels, axis);
    uint32_t lo, hi;
    c_sharp_range(fb, texels, axis, &lo, &hi);
    float c = tb_bins_centre(lo, hi, bin);
    return c >= 0 ? c : (float)(lo + hi) / 2.0f;
}

// ---------------------------------------------------------------- object tracking (handles below Valve's layer)

#define C_SLOTS 2048
typedef struct { uint64_t key; uint64_t a, b, c, d, e; } c_slot;  // a-e meaning per table
static c_slot c_images[C_SLOTS], c_ivs[C_SLOTS], c_rps[C_SLOTS], c_fbs[C_SLOTS], c_cmds[C_SLOTS];
static int c_obj_lock;
// Not the darkening layer's lock: it holds that one while asking the next layer (Valve's, then this one) for
// functions in vkCreateDevice.
static int c_dev_lock;

static c_slot *c_find_n(c_slot *t, uint32_t slots, uint64_t key, int create) {
    if (!key) return NULL;
    uint32_t h = (uint32_t)((key * 0x9E3779B97F4A7C15ull) >> 40) & (slots - 1);
    c_slot *free_slot = NULL;
    for (uint32_t i = 0; i < slots; ++i) {
        c_slot *s = &t[(h + i) & (slots - 1)];
        if (s->key == key) return s;
        if (s->key == 0) { if (!free_slot) free_slot = s; break; }
        if (s->key == ~0ull && !free_slot) free_slot = s;
    }
    if (!create || !free_slot) return NULL;
    memset(free_slot, 0, sizeof(*free_slot));
    free_slot->key = key;
    return free_slot;
}
static c_slot *c_find(c_slot *t, uint64_t key, int create) { return c_find_n(t, C_SLOTS, key, create); }
static void c_forget(c_slot *t, uint64_t key) {
    take_lock(&c_obj_lock);
    c_slot *s = c_find(t, key, 0);
    if (s) s->key = ~0ull;  // tombstone
    drop_lock(&c_obj_lock);
}
#define H(x) ((uint64_t)(uintptr_t)(x))

typedef void (VKAPI_PTR *PFN_fp_EndRendering2)(VkCommandBuffer, const void *);
typedef struct {
    void *key;
    VkDevice device;
    PFN_vkGetDeviceProcAddr gdpa;
    VkExtent2D gran;
    PFN_vkCreateImage CreateImage;
    PFN_vkDestroyImage DestroyImage;
    PFN_vkCreateImageView CreateImageView;
    PFN_vkDestroyImageView DestroyImageView;
    PFN_vkCreateRenderPass CreateRenderPass;
    PFN_vkCreateRenderPass2 CreateRenderPass2, CreateRenderPass2KHR;
    PFN_vkDestroyRenderPass DestroyRenderPass;
    PFN_vkCreateFramebuffer CreateFramebuffer;
    PFN_vkDestroyFramebuffer DestroyFramebuffer;
    PFN_vkCmdBeginRenderPass CmdBeginRenderPass;
    PFN_vkCmdBeginRenderPass2 CmdBeginRenderPass2, CmdBeginRenderPass2KHR;
    PFN_vkCmdEndRenderPass CmdEndRenderPass;
    PFN_vkCmdEndRenderPass2 CmdEndRenderPass2, CmdEndRenderPass2KHR;
    PFN_vkCmdBeginRendering CmdBeginRendering, CmdBeginRenderingKHR;
    PFN_vkCmdEndRendering CmdEndRendering, CmdEndRenderingKHR;
    PFN_fp_EndRendering2 CmdEndRendering2EXT, CmdEndRendering2KHR;
    // FRAMEPORT_FDM_DIAG only
    PFN_vkCreateShaderModule CreateShaderModule;
    PFN_vkDestroyShaderModule DestroyShaderModule;
    PFN_vkCreateGraphicsPipelines CreateGraphicsPipelines;
    PFN_vkDestroyPipeline DestroyPipeline;
    PFN_vkCmdBindPipeline CmdBindPipeline;
    PFN_vkBeginCommandBuffer BeginCommandBuffer;
    PFN_vkCmdExecuteCommands CmdExecuteCommands;
} c_device;
static struct { void *key; VkInstance instance; PFN_vkGetInstanceProcAddr gipa; } c_instances[MAX_HANDLES];
static c_device c_devices[MAX_HANDLES];

static c_device *c_dev(const void *handle) {
    void *key = key_of(handle);
    c_device *d = NULL;
    take_lock(&c_dev_lock);
    for (int i = 0; i < MAX_HANDLES && !d; i++)
        if (c_devices[i].gdpa && c_devices[i].key == key) d = &c_devices[i];
    drop_lock(&c_dev_lock);
    return d;
}

static const VkBaseInStructure *c_chain(const void *next, VkStructureType type) {
    for (const VkBaseInStructure *b = next; b; b = b->pNext)
        if (b->sType == type) return b;
    return NULL;
}

#define ST_FDM_RP_INFO ((VkStructureType)1000218002)        // VkRenderPassFragmentDensityMapCreateInfoEXT
#define ST_FDM_RENDERING_INFO ((VkStructureType)1000044007) // VkRenderingFragmentDensityMapAttachmentInfoEXT
#define ST_FDM_OFFSET_END ((VkStructureType)1000425002)     // VkRenderPassFragmentDensityMapOffsetEndInfoEXT
#define ST_RENDERING_END ((VkStructureType)1000619003)      // VkRenderingEndInfoKHR
#define ST_RP_ATTACHMENT_BEGIN ((VkStructureType)1000108003)
#define ST_FDM_OFFSET_PROPS ((VkStructureType)1000425001)
#define FDM_USAGE 0x200u
#define FDM_OFFSET_FLAG 0x8000u
typedef struct { VkStructureType sType; const void *pNext; uint32_t count; const VkOffset2D *offsets; } c_offset_end;
typedef struct { VkStructureType sType; const void *pNext; } c_rendering_end;

// Render targets (colour/depth images and their views), for Turnip's bin size (tu_bins.h): image a = width | height << 32,
// b = samples | layers << 16 | format << 32, c = flags | usage << 32; view: the same with the view's format.
#define C_RT_SLOTS 8192
static c_slot c_rtimgs[C_RT_SLOTS], c_rtviews[C_RT_SLOTS];
#define RT_USAGE (VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT)

static int c_is_depth(VkFormat f) {
    return f == VK_FORMAT_D16_UNORM || f == VK_FORMAT_X8_D24_UNORM_PACK32 || f == VK_FORMAT_D32_SFLOAT ||
           f == VK_FORMAT_D16_UNORM_S8_UINT || f == VK_FORMAT_D24_UNORM_S8_UINT || f == VK_FORMAT_D32_SFLOAT_S8_UINT;
}

// The tile-size limit a depth image with the density-map-offset flag sets (Turnip's max_tile_*_constraint_fdm)
static void c_image_constraint(const c_slot *v, uint32_t *max_w, uint32_t *max_h) {
    uint32_t flags = (uint32_t)v->c, usage = (uint32_t)(v->c >> 32);
    VkFormat format = (VkFormat)(uint32_t)(v->b >> 32);
    if (!c_gpu || !(flags & FDM_OFFSET_FLAG) || !(usage & VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT) ||
        !c_is_depth(format))
        return;
    uint32_t w, h;
    tb_lrz_constraint(c_gpu, (uint32_t)v->a, (uint32_t)(v->a >> 32), (uint32_t)v->b & 0xffff,
                      (uint32_t)(v->b >> 16) & 0xffff, &w, &h);
    if (w < *max_w) *max_w = w;
    if (h < *max_h) *max_h = h;
}

// Images are never changed here: Valve flags them for offsets itself (all but external-memory ones) when its offsets
// are on. Flagging more (imported Zink/AHardwareBuffer images, as a first version did for Fixed) crashed Turnip.
static VKAPI_ATTR VkResult VKAPI_CALL c_CreateImage(VkDevice device, const VkImageCreateInfo *ci,
                                                    const VkAllocationCallbacks *a, VkImage *image) {
    VkResult r = c_dev(device)->CreateImage(device, ci, a, image);
    if (r == VK_SUCCESS && (ci->usage & FDM_USAGE)) {
        take_lock(&c_obj_lock);
        c_slot *s = c_find(c_images, H(*image), 1);
        if (s) s->a = ci->extent.width, s->b = ci->extent.height, s->c = ci->flags;
        drop_lock(&c_obj_lock);
        static int logged;
        if (logged++ < 4)
            LOG("fdm_centre: density map %ux%u[%u] flags 0x%x", ci->extent.width, ci->extent.height, ci->arrayLayers,
                ci->flags);
    }
    if (r == VK_SUCCESS && c_gpu && (ci->usage & RT_USAGE)) {
        take_lock(&c_obj_lock);
        c_slot *s = c_find_n(c_rtimgs, C_RT_SLOTS, H(*image), 1);
        if (s)
            s->a = ci->extent.width | (uint64_t)ci->extent.height << 32,
            s->b = (ci->samples & 0xffff) | (uint64_t)(ci->arrayLayers & 0xffff) << 16 | (uint64_t)ci->format << 32,
            s->c = ci->flags | (uint64_t)ci->usage << 32;
        drop_lock(&c_obj_lock);
    }
    return r;
}
static VKAPI_ATTR void VKAPI_CALL c_DestroyImage(VkDevice device, VkImage image, const VkAllocationCallbacks *a) {
    c_forget(c_images, H(image));
    if (c_gpu) {
        take_lock(&c_obj_lock);
        c_slot *s = c_find_n(c_rtimgs, C_RT_SLOTS, H(image), 0);
        if (s) s->key = ~0ull;
        drop_lock(&c_obj_lock);
    }
    c_dev(device)->DestroyImage(device, image, a);
}
static VKAPI_ATTR VkResult VKAPI_CALL c_CreateImageView(VkDevice device, const VkImageViewCreateInfo *ci,
                                                        const VkAllocationCallbacks *a, VkImageView *view) {
    VkResult r = c_dev(device)->CreateImageView(device, ci, a, view);
    if (r == VK_SUCCESS) {
        take_lock(&c_obj_lock);
        c_slot *img = c_find(c_images, H(ci->image), 0);
        c_slot *s = img ? c_find(c_ivs, H(*view), 1) : NULL;
        // d = first array layer | layer count << 32: Valve gives a one-eye pass the map layer of that eye
        if (s) s->a = img->a, s->b = img->b, s->c = img->c,
               s->d = ci->subresourceRange.baseArrayLayer | (uint64_t)ci->subresourceRange.layerCount << 32;
        c_slot *rt = c_gpu ? c_find_n(c_rtimgs, C_RT_SLOTS, H(ci->image), 0) : NULL;
        c_slot *v = rt ? c_find_n(c_rtviews, C_RT_SLOTS, H(*view), 1) : NULL;
        if (v) v->a = rt->a, v->b = (rt->b & 0xffffffffull) | (uint64_t)ci->format << 32, v->c = rt->c, v->d = 0;
        drop_lock(&c_obj_lock);
    }
    return r;
}
static VKAPI_ATTR void VKAPI_CALL c_DestroyImageView(VkDevice device, VkImageView view, const VkAllocationCallbacks *a) {
    c_forget(c_ivs, H(view));
    if (c_gpu) {
        take_lock(&c_obj_lock);
        c_slot *s = c_find_n(c_rtviews, C_RT_SLOTS, H(view), 0);
        if (s) s->key = ~0ull;
        drop_lock(&c_obj_lock);
    }
    c_dev(device)->DestroyImageView(device, view, a);
}

// A render pass as Turnip's GMEM setup sees it (tu_render_pass_gmem_config); 0: not modelled (no bin size then)
typedef struct {
    tb_att atts[TB_MAX_ATTS];
    int known[TB_MAX_ATTS];  // format modelled
    uint32_t n, depth_mask;  // depth_mask: attachments with a depth format
    int bad;
} c_rp_model;
static void c_model_init(c_rp_model *m, uint32_t n) {
    memset(m, 0, sizeof(*m));
    m->n = n;
    if (n > TB_MAX_ATTS) m->bad = 1;
}
static void c_model_att(c_rp_model *m, uint32_t i, VkFormat format, VkSampleCountFlagBits samples) {
    if (i >= m->n || m->bad) return;
    m->known[i] = tb_att_of(format, (uint32_t)samples, &m->atts[i]);
    m->atts[i].first = ~0u, m->atts[i].last = 0;
    if (c_is_depth(format)) m->depth_mask |= 1u << i;
}
static void c_model_range(c_rp_model *m, uint32_t a, uint32_t subpass, int gmem) {
    if (a == VK_ATTACHMENT_UNUSED || m->bad) return;
    if (a >= m->n) { m->bad = 1; return; }
    tb_att *t = &m->atts[a];
    if (subpass < t->first) t->first = subpass;
    if (subpass > t->last) t->last = subpass;
    if (gmem) {
        t->gmem = 1;
        if (!m->known[a]) m->bad = 1;  // an attachment in GMEM with a format tu_bins.h doesn't know
    }
}
#define ST_MSRTSS ((VkStructureType)1000376002)  // VkMultisampledRenderToSingleSampledInfoEXT
#define ST_DS_RESOLVE ((VkStructureType)1000199001)  // VkSubpassDescriptionDepthStencilResolve
static int c_msrtss(const void *next) {
    const VkBaseInStructure *b = c_chain(next, ST_MSRTSS);
    return b && ((const struct { VkStructureType t; const void *n; VkBool32 on; } *)b)->on;
}
static void c_model2(const VkRenderPassCreateInfo2 *ci, c_rp_model *m) {
    c_model_init(m, ci->attachmentCount);
    for (uint32_t i = 0; i < ci->attachmentCount && !m->bad; ++i)
        c_model_att(m, i, ci->pAttachments[i].format, ci->pAttachments[i].samples);
    for (uint32_t i = 0; i < ci->subpassCount && !m->bad; ++i) {
        const VkSubpassDescription2 *sp = &ci->pSubpasses[i];
        if (c_msrtss(sp->pNext)) m->bad = 1;
        for (uint32_t j = 0; j < sp->inputAttachmentCount; ++j) c_model_range(m, sp->pInputAttachments[j].attachment, i, 0);
        for (uint32_t j = 0; j < sp->colorAttachmentCount; ++j) {
            c_model_range(m, sp->pColorAttachments[j].attachment, i, 1);
            if (sp->pResolveAttachments && sp->pResolveAttachments[j].attachment != VK_ATTACHMENT_UNUSED &&
                sp->pColorAttachments[j].attachment != VK_ATTACHMENT_UNUSED)
                c_model_range(m, sp->pResolveAttachments[j].attachment, i, 0);
        }
        if (sp->pDepthStencilAttachment) c_model_range(m, sp->pDepthStencilAttachment->attachment, i, 1);
        const VkSubpassDescriptionDepthStencilResolve *dr = (const void *)c_chain(sp->pNext, ST_DS_RESOLVE);
        if (dr && dr->pDepthStencilResolveAttachment && sp->pDepthStencilAttachment &&
            sp->pDepthStencilAttachment->attachment != VK_ATTACHMENT_UNUSED)
            c_model_range(m, dr->pDepthStencilResolveAttachment->attachment, i, 0);
    }
}
static void c_model1(const VkRenderPassCreateInfo *ci, c_rp_model *m) {
    c_model_init(m, ci->attachmentCount);
    for (uint32_t i = 0; i < ci->attachmentCount && !m->bad; ++i)
        c_model_att(m, i, ci->pAttachments[i].format, ci->pAttachments[i].samples);
    for (uint32_t i = 0; i < ci->subpassCount && !m->bad; ++i) {
        const VkSubpassDescription *sp = &ci->pSubpasses[i];
        for (uint32_t j = 0; j < sp->inputAttachmentCount; ++j) c_model_range(m, sp->pInputAttachments[j].attachment, i, 0);
        for (uint32_t j = 0; j < sp->colorAttachmentCount; ++j) {
            c_model_range(m, sp->pColorAttachments[j].attachment, i, 1);
            if (sp->pResolveAttachments && sp->pResolveAttachments[j].attachment != VK_ATTACHMENT_UNUSED &&
                sp->pColorAttachments[j].attachment != VK_ATTACHMENT_UNUSED)
                c_model_range(m, sp->pResolveAttachments[j].attachment, i, 0);
        }
        if (sp->pDepthStencilAttachment) c_model_range(m, sp->pDepthStencilAttachment->attachment, i, 1);
    }
}

// render pass: a = map attachment index, b = the subpasses' view masks ORed (multiview), c = Turnip's GMEM budget
// (gmem_pixels | tile_align_w << 32 | min_cpp << 48, 0: not modelled), d = depth-format attachments | samples of
// attachment 0 << 32 (Turnip takes those for an imageless framebuffer's depth constraint)
static void c_note_rp(const void *next, VkRenderPass rp, uint32_t view_mask, const c_rp_model *m,
                      VkSampleCountFlagBits samples0) {
    const VkRenderPassFragmentDensityMapCreateInfoEXT *f = (const void *)c_chain(next, ST_FDM_RP_INFO);
    if (!f || f->fragmentDensityMapAttachment.attachment == VK_ATTACHMENT_UNUSED) return;
    tb_pass p = {0, 0, 0};
    int ok = c_gpu && m && !m->bad && tb_pass_config(c_gpu, m->atts, m->n, &p);
    take_lock(&c_obj_lock);
    c_slot *s = c_find(c_rps, H(rp), 1);
    if (s) {
        s->a = f->fragmentDensityMapAttachment.attachment, s->b = view_mask;
        s->c = ok ? (p.gmem_pixels | (uint64_t)(p.tile_align_w & 0xffff) << 32 |
                     (uint64_t)(p.min_cpp > 0xffff ? 0 : p.min_cpp) << 48) : 0;
        s->d = (m ? m->depth_mask : 0) | (uint64_t)samples0 << 32;
    }
    drop_lock(&c_obj_lock);
    static int logged;
    if (c_gpu && logged++ < 8) {
        char atts[160] = "";
        for (uint32_t i = 0; m && i < m->n && i < 8; ++i)
            snprintf(atts + strlen(atts), sizeof(atts) - strlen(atts), "%s%u%s", i ? "," : "", m->atts[i].cpp,
                     m->atts[i].gmem ? "" : "-");
        LOG("fdm_centre: foveated render pass: attachments cpp %s (- = not in GMEM) -> %sgmem_pixels %u, tile "
            "align %u", atts, ok ? "" : "NOT MODELLED, ", p.gmem_pixels, p.tile_align_w);
    }
}
static uint32_t c_masks2(const VkRenderPassCreateInfo2 *ci) {
    uint32_t m = 0;
    for (uint32_t i = 0; ci->pSubpasses && i < ci->subpassCount; ++i) m |= ci->pSubpasses[i].viewMask;
    return m;
}
static uint32_t c_masks1(const VkRenderPassCreateInfo *ci) {
    const VkRenderPassMultiviewCreateInfo *mv = (const void *)c_chain(ci->pNext, VK_STRUCTURE_TYPE_RENDER_PASS_MULTIVIEW_CREATE_INFO);
    uint32_t m = 0;
    for (uint32_t i = 0; mv && mv->pViewMasks && i < mv->subpassCount; ++i) m |= mv->pViewMasks[i];
    return m;
}
static void c_note_rp2(const VkRenderPassCreateInfo2 *ci, VkRenderPass rp) {
    if (!c_chain(ci->pNext, ST_FDM_RP_INFO)) return;
    c_rp_model m;
    c_model2(ci, &m);
    c_note_rp(ci->pNext, rp, c_masks2(ci), &m, ci->attachmentCount ? ci->pAttachments[0].samples : VK_SAMPLE_COUNT_1_BIT);
}
static VKAPI_ATTR VkResult VKAPI_CALL c_CreateRenderPass(VkDevice device, const VkRenderPassCreateInfo *ci,
                                                         const VkAllocationCallbacks *a, VkRenderPass *rp) {
    VkResult r = c_dev(device)->CreateRenderPass(device, ci, a, rp);
    if (r == VK_SUCCESS && c_chain(ci->pNext, ST_FDM_RP_INFO)) {
        c_rp_model m;
        c_model1(ci, &m);
        c_note_rp(ci->pNext, *rp, c_masks1(ci), &m, ci->attachmentCount ? ci->pAttachments[0].samples : VK_SAMPLE_COUNT_1_BIT);
    }
    return r;
}
static VKAPI_ATTR VkResult VKAPI_CALL c_CreateRenderPass2(VkDevice device, const VkRenderPassCreateInfo2 *ci,
                                                          const VkAllocationCallbacks *a, VkRenderPass *rp) {
    VkResult r = c_dev(device)->CreateRenderPass2(device, ci, a, rp);
    if (r == VK_SUCCESS) c_note_rp2(ci, *rp);
    return r;
}
static VKAPI_ATTR VkResult VKAPI_CALL c_CreateRenderPass2KHR(VkDevice device, const VkRenderPassCreateInfo2 *ci,
                                                             const VkAllocationCallbacks *a, VkRenderPass *rp) {
    VkResult r = c_dev(device)->CreateRenderPass2KHR(device, ci, a, rp);
    if (r == VK_SUCCESS) c_note_rp2(ci, *rp);
    return r;
}
static VKAPI_ATTR void VKAPI_CALL c_DestroyRenderPass(VkDevice device, VkRenderPass rp, const VkAllocationCallbacks *a) {
    c_forget(c_rps, H(rp));
    c_dev(device)->DestroyRenderPass(device, rp, a);
}

static uint32_t c_last_bit(uint32_t mask) {
    uint32_t n = 0;
    while (mask >> n) ++n;
    return n;
}
// Turnip's bin size for a framebuffer of a modelled pass: width | height << 16, 0 when undecided
static uint32_t c_bin_size(uint64_t pass_c, uint32_t fb_w, uint32_t fb_h, uint32_t layers, uint32_t max_w,
                           uint32_t max_h) {
    if (!c_gpu || !pass_c) return 0;
    tb_pass p = {(uint32_t)pass_c, (uint32_t)(pass_c >> 32) & 0xffff, (uint32_t)(pass_c >> 48)};
    if (!p.min_cpp) p.min_cpp = UINT32_MAX;
    uint32_t w = 0, h = 0;
    if (!tb_tile_size(c_gpu, &p, fb_w, fb_h, layers, max_w, max_h, &w, &h) || w > 0xffff || h > 0xffff) return 0;
    return w | h << 16;
}

#define ST_FB_ATTACHMENTS ((VkStructureType)1000108001)  // VkFramebufferAttachmentsCreateInfo
// framebuffer: a = width | height << 32, b = density map view (0: imageless), c = map attachment index + 1 | Turnip's
// bin size << 32 (width | height << 16, 0: undecided), d = layers | render pass view mask << 32
static VKAPI_ATTR VkResult VKAPI_CALL c_CreateFramebuffer(VkDevice device, const VkFramebufferCreateInfo *ci,
                                                          const VkAllocationCallbacks *a, VkFramebuffer *fb) {
    VkResult r = c_dev(device)->CreateFramebuffer(device, ci, a, fb);
    if (r == VK_SUCCESS) {
        take_lock(&c_obj_lock);
        c_slot *rp = c_find(c_rps, H(ci->renderPass), 0);
        if (rp) {
            uint32_t bin = 0;
            if (rp->c) {
                // the first depth attachment with the offset flag limits the tile size (tu_CreateFramebuffer)
                uint32_t max_w = ~0u, max_h = ~0u;
                int known = 1;
                if (ci->flags & VK_FRAMEBUFFER_CREATE_IMAGELESS_BIT) {
                    const VkFramebufferAttachmentsCreateInfo *fa = (const void *)c_chain(ci->pNext, ST_FB_ATTACHMENTS);
                    for (uint32_t i = 0; fa && i < fa->attachmentImageInfoCount; ++i) {
                        const VkFramebufferAttachmentImageInfo *ii = &fa->pAttachmentImageInfos[i];
                        if ((ii->flags & FDM_OFFSET_FLAG) && (ii->usage & VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT)) {
                            // Turnip takes the pass's first attachment's sample count here
                            tb_lrz_constraint(c_gpu, ii->width, ii->height, (uint32_t)(rp->d >> 32), ii->layerCount,
                                              &max_w, &max_h);
                            break;
                        }
                    }
                } else {
                    for (uint32_t i = 0; ci->pAttachments && i < ci->attachmentCount; ++i) {
                        c_slot *v = c_find_n(c_rtviews, C_RT_SLOTS, H(ci->pAttachments[i]), 0);
                        if (!v) {
                            if (i < 32 && (rp->d >> i & 1)) known = 0;  // a depth attachment not seen created
                            continue;
                        }
                        if (((uint32_t)v->c & FDM_OFFSET_FLAG) &&
                            ((uint32_t)(v->c >> 32) & VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT)) {
                            c_image_constraint(v, &max_w, &max_h);
                            break;
                        }
                    }
                }
                uint32_t layers = ci->layers, views = c_last_bit((uint32_t)rp->b);
                if (views > layers) layers = views;
                if (known) bin = c_bin_size(rp->c, ci->width, ci->height, layers, max_w, max_h);
            }
            c_slot *s = c_find(c_fbs, H(*fb), 1);
            if (s) {
                s->a = ci->width | (uint64_t)ci->height << 32;
                s->b = (!(ci->flags & VK_FRAMEBUFFER_CREATE_IMAGELESS_BIT) && ci->pAttachments &&
                        rp->a < ci->attachmentCount) ? H(ci->pAttachments[rp->a]) : 0;
                s->c = (rp->a + 1) | (uint64_t)bin << 32;
                s->d = ci->layers | rp->b << 32;
            }
        }
        drop_lock(&c_obj_lock);
    }
    return r;
}
static VKAPI_ATTR void VKAPI_CALL c_DestroyFramebuffer(VkDevice device, VkFramebuffer fb, const VkAllocationCallbacks *a) {
    c_forget(c_fbs, H(fb));
    c_dev(device)->DestroyFramebuffer(device, fb, a);
}

// Dynamic rendering: Turnip builds the pass per vkCmdBeginRendering (tu_setup_dynamic_render_pass/_framebuffer):
// colour attachments, then depth/stencil in GMEM; the framebuffer ends at the render area's end.
static uint32_t c_dynamic_bin(const VkRenderingInfo *info) {
    if (!c_gpu || c_msrtss(info->pNext)) return 0;
    tb_att atts[TB_MAX_ATTS];
    uint32_t n = 0;
    uint32_t max_w = ~0u, max_h = ~0u;
    take_lock(&c_obj_lock);
    int ok = info->colorAttachmentCount < TB_MAX_ATTS;
    for (uint32_t i = 0; ok && i < info->colorAttachmentCount; ++i) {
        VkImageView view = info->pColorAttachments[i].imageView;
        if (!view) continue;
        c_slot *v = c_find_n(c_rtviews, C_RT_SLOTS, H(view), 0);
        ok = v && tb_att_of((VkFormat)(uint32_t)(v->b >> 32), (uint32_t)v->b & 0xffff, &atts[n]);
        if (ok) atts[n].gmem = 1, atts[n].first = atts[n].last = 0, ++n;
    }
    const VkRenderingAttachmentInfo *ds = info->pDepthAttachment && info->pDepthAttachment->imageView ?
        info->pDepthAttachment : info->pStencilAttachment && info->pStencilAttachment->imageView ?
        info->pStencilAttachment : NULL;
    if (ok && ds) {
        c_slot *v = c_find_n(c_rtviews, C_RT_SLOTS, H(ds->imageView), 0);
        ok = v && tb_att_of((VkFormat)(uint32_t)(v->b >> 32), (uint32_t)v->b & 0xffff, &atts[n]);
        if (ok) atts[n].gmem = 1, atts[n].first = atts[n].last = 0, ++n;
        if (ok && info->pDepthAttachment && info->pDepthAttachment->imageView) {
            c_slot *dv = c_find_n(c_rtviews, C_RT_SLOTS, H(info->pDepthAttachment->imageView), 0);
            if (dv) c_image_constraint(dv, &max_w, &max_h);
        }
    }
    drop_lock(&c_obj_lock);
    tb_pass p;
    if (!ok || !tb_pass_config(c_gpu, atts, n, &p)) return 0;
    uint32_t layers = info->viewMask ? 1 : info->layerCount, views = c_last_bit(info->viewMask);
    if (views > layers) layers = views;
    uint64_t pass_c = p.gmem_pixels | (uint64_t)(p.tile_align_w & 0xffff) << 32 |
                      (uint64_t)(p.min_cpp > 0xffff ? 0 : p.min_cpp) << 48;
    return c_bin_size(pass_c, (uint32_t)(info->renderArea.offset.x + (int32_t)info->renderArea.extent.width),
                      (uint32_t)(info->renderArea.offset.y + (int32_t)info->renderArea.extent.height), layers, max_w,
                      max_h);
}

// ---------------------------------------------------------------- diagnostics (FRAMEPORT_FDM_DIAG=1)
//
// Turnip scales each view's viewport by that view's fragment area only when the bound pipeline has
// program.per_view_viewport: the pre-rasterization stage doesn't write ViewportIndex, and the fragment shader was
// compiled for a density map (render pass with a density-map attachment at pipeline creation, or dynamic rendering
// with VK_PIPELINE_CREATE_RENDERING_FRAGMENT_DENSITY_MAP_ATTACHMENT_BIT_EXT), unless the app enabled
// multiviewPerViewViewports. One draw with another pipeline sets rp.shared_viewport for the whole pass: the bins
// aren't shifted by the offsets any more, each view's density is sampled at bin centre - its offset, and every view
// gets the smallest fragment area of all views (tu_calc_frag_area). These counters say whether that happens.

#define C_PIPE_SLOTS 16384
#define CP_FDM 1u       // the driver sees a density map for this pipeline (fs.has_fdm)
#define CP_VP 2u        // a pre-rasterization shader writes ViewportIndex
#define CP_LAYER 4u     // ... writes Layer
#define CP_DYN 8u       // dynamic rendering
#define CP_LIB 16u      // pipeline library or linked from libraries
#define CP_NOFS 32u     // no fragment shader in this (library) pipeline
#define CP_SCANNED 64u  // the shader modules were seen
#define CP_RISKY(b) (!((b) & CP_FDM) || ((b) & CP_VP))
static c_slot c_mods[C_SLOTS], c_pdiag[C_SLOTS], c_pipes[C_PIPE_SLOTS];
static int c_app_per_view_viewports;  // the app enabled multiviewPerViewViewports (QCOM)
static uint64_t c_n_pipes, c_n_pipes_nofdm, c_n_pipes_vp, c_n_passes, c_n_shared, c_n_logged_pipes;

// Output ViewportIndex (bit 0) / Layer (bit 1) of a SPIR-V module, plus 1 << (8 + execution model) per entry point.
static uint32_t c_spirv_outputs(const uint32_t *code, size_t words) {
    if (!code || words < 5 || code[0] != 0x07230203u) return 0;
    enum { MAXID = 32 };
    uint32_t ids[MAXID], idbits[MAXID], nids = 0;          // variables decorated BuiltIn ViewportIndex/Layer
    uint32_t structs[MAXID], sbits[MAXID], nstructs = 0;   // block types with such a member
    uint32_t ptrs[MAXID], pbits[MAXID], nptrs = 0;         // Output pointers to those blocks
    uint32_t out = 0;
    for (size_t i = 5; i < words;) {
        uint32_t op = code[i] & 0xffff, wc = code[i] >> 16;
        if (!wc || i + wc > words) break;
        const uint32_t *w = code + i;
        if (op == 15 && wc >= 2 && w[1] < 16) out |= 1u << (8 + w[1]);  // OpEntryPoint
        uint32_t bit = 0;
        if (op == 71 && wc >= 4 && w[2] == 11) bit = w[3] == 10 ? 1 : w[3] == 9 ? 2 : 0;  // OpDecorate BuiltIn
        if (op == 72 && wc >= 5 && w[3] == 11) bit = w[4] == 10 ? 1 : w[4] == 9 ? 2 : 0;  // OpMemberDecorate
        if (bit && op == 71 && nids < MAXID) ids[nids] = w[1], idbits[nids++] = bit;
        if (bit && op == 72) {
            uint32_t k = 0;
            while (k < nstructs && structs[k] != w[1]) ++k;
            if (k == nstructs && nstructs < MAXID) structs[nstructs] = w[1], sbits[nstructs++] = 0;
            if (k < nstructs) sbits[k] |= bit;
        }
        if (op == 32 && wc >= 4 && w[2] == 3)  // OpTypePointer Output
            for (uint32_t k = 0; k < nstructs; ++k)
                if (structs[k] == w[3] && nptrs < MAXID) ptrs[nptrs] = w[1], pbits[nptrs++] = sbits[k];
        if (op == 59 && wc >= 4 && w[3] == 3) {  // OpVariable Output
            for (uint32_t k = 0; k < nids; ++k)
                if (ids[k] == w[2]) out |= idbits[k];
            for (uint32_t k = 0; k < nptrs; ++k)
                if (ptrs[k] == w[1]) out |= pbits[k];
        }
        i += wc;
    }
    return out;
}

static VKAPI_ATTR VkResult VKAPI_CALL c_CreateShaderModule(VkDevice device, const VkShaderModuleCreateInfo *ci,
                                                           const VkAllocationCallbacks *a, VkShaderModule *module) {
    VkResult r = c_dev(device)->CreateShaderModule(device, ci, a, module);
    if (r == VK_SUCCESS) {
        uint32_t bits = c_spirv_outputs(ci->pCode, ci->codeSize / 4);
        take_lock(&c_obj_lock);
        c_slot *s = c_find(c_mods, H(*module), 1);
        if (s) s->a = bits | 0x80000000u;  // high bit: seen
        drop_lock(&c_obj_lock);
    }
    return r;
}
static VKAPI_ATTR void VKAPI_CALL c_DestroyShaderModule(VkDevice device, VkShaderModule m, const VkAllocationCallbacks *a) {
    c_forget(c_mods, H(m));
    c_dev(device)->DestroyShaderModule(device, m, a);
}

#define ST_PIPELINE_FLAGS2 ((VkStructureType)1000470005)     // VkPipelineCreateFlags2CreateInfo
#define ST_PIPELINE_RENDERING ((VkStructureType)1000044002)  // VkPipelineRenderingCreateInfo
#define ST_PIPELINE_LIBRARY ((VkStructureType)1000290000)    // VkPipelineLibraryCreateInfoKHR
#define PIPE_FDM_FLAG 0x400000ull  // VK_PIPELINE_CREATE(_2)_RENDERING_FRAGMENT_DENSITY_MAP_ATTACHMENT_BIT_EXT
#define PIPE_LIBRARY_FLAG 0x800ull
typedef struct { VkStructureType sType; const void *pNext; uint64_t flags; } c_flags2;
typedef struct { VkStructureType sType; const void *pNext; uint32_t count; const VkPipeline *libs; } c_libinfo;

// What Turnip will decide for this pipeline: bits CP_*, view mask << 32
static uint64_t c_pipeline_bits(const VkGraphicsPipelineCreateInfo *ci) {
    uint64_t flags = ci->flags;
    const c_flags2 *f2 = (const void *)c_chain(ci->pNext, ST_PIPELINE_FLAGS2);
    if (f2) flags = f2->flags;
    uint32_t bits = 0, mask = 0, fs = 0;
    take_lock(&c_obj_lock);
    if (ci->renderPass) {
        c_slot *rp = c_find(c_rps, H(ci->renderPass), 0);  // only passes with a density-map attachment are noted
        if (rp) bits |= CP_FDM, mask = (uint32_t)rp->b;
    } else {
        bits |= CP_DYN;
        if (flags & PIPE_FDM_FLAG) bits |= CP_FDM;
        const VkPipelineRenderingCreateInfo *pr = (const void *)c_chain(ci->pNext, ST_PIPELINE_RENDERING);
        if (pr) mask = pr->viewMask;
    }
    for (uint32_t i = 0; ci->pStages && i < ci->stageCount; ++i) {
        const VkPipelineShaderStageCreateInfo *st = &ci->pStages[i];
        uint32_t out = 0;
        if (st->module) {
            c_slot *m = c_find(c_mods, H(st->module), 0);
            if (m) out = (uint32_t)m->a, bits |= CP_SCANNED;
        } else {
            const VkShaderModuleCreateInfo *inl =
                (const void *)c_chain(st->pNext, VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO);
            if (inl) out = c_spirv_outputs(inl->pCode, inl->codeSize / 4), bits |= CP_SCANNED;
        }
        if (st->stage == VK_SHADER_STAGE_FRAGMENT_BIT) fs = 1;
        else bits |= (out & 1 ? CP_VP : 0) | (out & 2 ? CP_LAYER : 0);
    }
    const c_libinfo *libs = (const void *)c_chain(ci->pNext, ST_PIPELINE_LIBRARY);
    for (uint32_t i = 0; libs && libs->libs && i < libs->count; ++i) {
        c_slot *l = c_find_n(c_pipes, C_PIPE_SLOTS, H(libs->libs[i]), 0);
        if (l) {
            bits |= (uint32_t)l->a & (CP_FDM | CP_VP | CP_LAYER | CP_SCANNED);
            if (!((uint32_t)l->a & CP_NOFS)) fs = 1;
            if (!mask) mask = (uint32_t)(l->a >> 32);
        }
    }
    drop_lock(&c_obj_lock);
    if (libs || (flags & PIPE_LIBRARY_FLAG)) bits |= CP_LIB;
    if (!fs) bits |= CP_NOFS;
    return bits | (uint64_t)mask << 32;
}

static VKAPI_ATTR VkResult VKAPI_CALL c_CreateGraphicsPipelines(VkDevice device, VkPipelineCache cache, uint32_t count,
                                                                const VkGraphicsPipelineCreateInfo *ci,
                                                                const VkAllocationCallbacks *a, VkPipeline *pipes) {
    VkResult r = c_dev(device)->CreateGraphicsPipelines(device, cache, count, ci, a, pipes);
    for (uint32_t i = 0; r >= 0 && i < count; ++i) {
        if (!pipes[i]) continue;
        uint64_t b = c_pipeline_bits(&ci[i]);
        uint32_t bits = (uint32_t)b;
        take_lock(&c_obj_lock);
        c_slot *s = c_find_n(c_pipes, C_PIPE_SLOTS, H(pipes[i]), 1);
        if (s) s->a = b, s->b = H(ci[i].renderPass);
        ++c_n_pipes;
        if (!(bits & CP_FDM)) ++c_n_pipes_nofdm;
        if (bits & CP_VP) ++c_n_pipes_vp;
        int log_it = CP_RISKY(bits) && !(bits & CP_NOFS) && c_n_logged_pipes < 24;
        if (log_it) ++c_n_logged_pipes;
        unsigned long long n = c_n_pipes, nofdm = c_n_pipes_nofdm, vp = c_n_pipes_vp;
        drop_lock(&c_obj_lock);
        if (log_it || !s)
            LOG("fdm_diag: pipeline %p (%s, render pass %p subpass %u, view mask 0x%x, flags 0x%x): %s%s%s%s%s%s "
                "[pipelines so far %llu, without density map %llu, writing ViewportIndex %llu]", (void *)pipes[i],
                bits & CP_DYN ? "dynamic rendering" : "render pass", (void *)ci[i].renderPass, ci[i].subpass,
                (uint32_t)(b >> 32), (uint32_t)ci[i].flags, bits & CP_FDM ? "density map" : "NO density map",
                bits & CP_VP ? ", writes ViewportIndex" : "", bits & CP_LAYER ? ", writes Layer" : "",
                bits & CP_LIB ? ", library" : "", bits & CP_SCANNED ? "" : ", shaders not seen",
                s ? "" : ", NOT TRACKED (table full)", n, nofdm, vp);
    }
    return r;
}
static VKAPI_ATTR void VKAPI_CALL c_DestroyPipeline(VkDevice device, VkPipeline p, const VkAllocationCallbacks *a) {
    take_lock(&c_obj_lock);
    c_slot *s = c_find_n(c_pipes, C_PIPE_SLOTS, H(p), 0);
    if (s) s->key = ~0ull;
    drop_lock(&c_obj_lock);
    c_dev(device)->DestroyPipeline(device, p, a);
}

// pass diagnostics per command buffer: a = graphics binds, b = binds Turnip shares one viewport scale for, c = binds
// writing ViewportIndex, d = bits of the first such pipeline | 1 << 31 (low half); 1 << 63: a secondary begun inside
// a foveated render pass, 1 << 62: the pass executed secondaries
static void c_pdiag_start(VkCommandBuffer cmd, int secondary) {
    if (!c_diag) return;
    take_lock(&c_obj_lock);
    c_slot *s = c_find(c_pdiag, H(cmd), 1);
    if (s) s->a = s->b = s->c = 0, s->d = secondary ? 1ull << 63 : 0;
    drop_lock(&c_obj_lock);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdBindPipeline(VkCommandBuffer cmd, VkPipelineBindPoint point, VkPipeline p) {
    if (point == VK_PIPELINE_BIND_POINT_GRAPHICS) {
        take_lock(&c_obj_lock);
        c_slot *s = c_find(c_pdiag, H(cmd), 0);
        if (s) {
            c_slot *pp = c_find_n(c_pipes, C_PIPE_SLOTS, H(p), 0);
            uint32_t bits = pp ? (uint32_t)pp->a : 0;
            ++s->a;
            if (pp && CP_RISKY(bits) && !c_app_per_view_viewports) {
                ++s->b;
                if (!(s->d & 0xffffffffull)) s->d |= bits | 0x80000000u;
            }
            if (bits & CP_VP) ++s->c;
        }
        drop_lock(&c_obj_lock);
    }
    c_dev(cmd)->CmdBindPipeline(cmd, point, p);
}
static VKAPI_ATTR VkResult VKAPI_CALL c_BeginCommandBuffer(VkCommandBuffer cmd, const VkCommandBufferBeginInfo *info) {
    c_forget(c_pdiag, H(cmd));
    const VkCommandBufferInheritanceInfo *inh = info ? info->pInheritanceInfo : NULL;
    if (inh && inh->renderPass && (info->flags & VK_COMMAND_BUFFER_USAGE_RENDER_PASS_CONTINUE_BIT)) {
        take_lock(&c_obj_lock);
        int fdm = c_find(c_rps, H(inh->renderPass), 0) != NULL;
        drop_lock(&c_obj_lock);
        if (fdm) c_pdiag_start(cmd, 1);
    }
    return c_dev(cmd)->BeginCommandBuffer(cmd, info);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdExecuteCommands(VkCommandBuffer cmd, uint32_t n, const VkCommandBuffer *subs) {
    take_lock(&c_obj_lock);
    c_slot *s = c_find(c_pdiag, H(cmd), 0);
    for (uint32_t i = 0; s && subs && i < n; ++i) {
        c_slot *sub = c_find(c_pdiag, H(subs[i]), 0);
        if (!sub) continue;
        s->a += sub->a, s->b += sub->b, s->c += sub->c;
        if (!(s->d & 0xffffffffull)) s->d |= sub->d & 0xffffffffull;
        s->d |= 1ull << 62;
    }
    drop_lock(&c_obj_lock);
    c_dev(cmd)->CmdExecuteCommands(cmd, n, subs);
}

// The pass's summary at its end: the offsets as handed down and whether Turnip shares one viewport scale
typedef struct {
    uint32_t fbw, fbh, mw, mh, first, map_layers, n, flagged;
    int32_t eye[FP_FDM_MAX_VIEWS];  // index into views[] per offset, -1: no single matching view
    fp_fdm_view views[FP_FDM_MAX_VIEWS];
    float vx, vy;               // where the full-density area is centred without offsets
    uint32_t bin_w, bin_h;      // Turnip's bins (0: unknown)
    int bin_source;             // 0 unknown, 1 computed, 2 fdm_bin_w/fdm_bin_h
    uint32_t raw, rah;          // the pass's render area (Valve scales its gaze offsets by it)
} c_pass_info;
static void c_diag_pass(VkCommandBuffer cmd, const c_pass_info *pi, uint32_t in_count, const VkOffset2D *in,
                        const VkOffset2D *out, uint32_t handed) {
    take_lock(&c_obj_lock);
    c_slot *s = c_find(c_pdiag, H(cmd), 0);
    uint64_t binds = s ? s->a : 0, shared = s ? s->b : 0, vp = s ? s->c : 0, d = s ? s->d : 0;
    if (s) s->key = ~0ull;
    uint64_t pass_no = ++c_n_passes;
    if (shared) ++c_n_shared;
    unsigned long long total_shared = c_n_shared, pipes = c_n_pipes, nofdm = c_n_pipes_nofdm, vpw = c_n_pipes_vp;
    drop_lock(&c_obj_lock);
    if (!(pass_no <= 40 || pass_no % 1000 == 0 || (shared && total_shared <= 20))) return;
    char text[640];
    int len = snprintf(text, sizeof(text), "fdm_diag: pass #%llu %ux%u (render area %ux%u) map %ux%u layer %u+%u%s, bins %ux%u, sharp centre %.0f,%.0f: "
                       "%llu pipeline bind(s)%s, %llu Turnip shares one viewport for%s (first: 0x%x), %llu writing "
                       "ViewportIndex -> %s [%llu of %llu passes shared; pipelines created %llu, without density map "
                       "%llu, writing ViewportIndex %llu]; offsets in %u:",
                       (unsigned long long)pass_no, pi->fbw, pi->fbh, pi->raw, pi->rah, pi->mw, pi->mh, pi->first, pi->map_layers,
                       pi->flagged ? "" : " (map without offset flag)", pi->bin_w, pi->bin_h, pi->vx, pi->vy,
                       (unsigned long long)binds,
                       d >> 62 ? " incl. secondaries" : "", (unsigned long long)shared,
                       c_app_per_view_viewports ? " (app enabled per-view viewports)" : "", (uint32_t)d & 0x7fffffffu,
                       (unsigned long long)vp, shared ? "SHARED VIEWPORT (each view gets all views' sharp bins)"
                                                      : "per-view scale", total_shared,
                       (unsigned long long)pass_no, pipes, nofdm, vpw, in_count);
    for (uint32_t v = 0; in && v < pi->n && v < FP_FDM_MAX_VIEWS && len > 0 && len < (int)sizeof(text); ++v)
        len += snprintf(text + len, sizeof(text) - (size_t)len, " %d,%d", in[v].x, in[v].y);
    if (len > 0 && len < (int)sizeof(text))
        len += snprintf(text + len, sizeof(text) - (size_t)len, "; handed down %u:", handed);
    for (uint32_t v = 0; v < handed && v < FP_FDM_MAX_VIEWS && len > 0 && len < (int)sizeof(text); ++v) {
        const fp_fdm_view *e = pi->eye[v] >= 0 ? &pi->views[pi->eye[v]] : NULL;
        if (e)
            len += snprintf(text + len, sizeof(text) - (size_t)len,
                            " v%u %d,%d (layer %u rect %d,%d %dx%d centre %.0f,%.0f -> sharp at %.0f,%.0f)", v,
                            out[v].x, out[v].y, e->layer, e->x, e->y, e->w, e->h, e->cx, e->cy, pi->vx + out[v].x,
                            pi->vy + out[v].y);
        else
            len += snprintf(text + len, sizeof(text) - (size_t)len, " v%u %d,%d (no view)", v, out[v].x, out[v].y);
    }
    LOG("%s", text);
}

// The number of density-map offsets Turnip reads at the end of a pass, whatever count it is given
// (tu_fdm_num_layers): the multiview view count, else the framebuffer's layers (per-layer maps) or 1.
static uint32_t c_pass_views(uint32_t view_mask, uint32_t layers) {
    if (view_mask) {
        uint32_t n = 0;
        while (view_mask >> n) ++n;
        return n;
    }
    return layers ? layers : 1;
}

// command buffer state: a = framebuffer width | height << 32, b = map width | height << 32 (0: no map), c = map flags
// | Turnip's bin size << 32 (width | height << 16, 0: undecided), d = offsets Turnip reads | first map layer << 16 |
// map view's layer count << 32, e = render area width | height << 32 (Valve's gaze offsets are NDC x half of it)
static void c_begin(VkCommandBuffer cmd, uint32_t fbw, uint32_t fbh, VkImageView map, uint32_t views, uint32_t bin,
                    VkExtent2D area) {
    take_lock(&c_obj_lock);
    c_slot *iv = map ? c_find(c_ivs, H(map), 0) : NULL;
    c_slot *s = c_find(c_cmds, H(cmd), iv != NULL);
    if (s) {
        s->a = fbw | (uint64_t)fbh << 32;
        s->b = iv ? (iv->a | iv->b << 32) : 0;
        s->c = iv ? ((iv->c & 0xffffffffull) | (uint64_t)bin << 32) : 0;
        s->d = iv ? ((views & 0xffff) | (iv->d & 0xffff) << 16 | (iv->d >> 32) << 32) : 0;
        s->e = area.width | (uint64_t)area.height << 32;
    }
    drop_lock(&c_obj_lock);
    if (iv) c_pdiag_start(cmd, 0);
}
static void c_begin_rp(VkCommandBuffer cmd, const VkRenderPassBeginInfo *info) {
    if (c_disabled || !info) return;
    take_lock(&c_obj_lock);
    c_slot *fb = c_find(c_fbs, H(info->framebuffer), 0);
    uint64_t size = fb ? fb->a : 0, view = fb ? fb->b : 0, index = fb ? fb->c : 0, lm = fb ? fb->d : 0;
    drop_lock(&c_obj_lock);
    if (fb && !view && (uint32_t)index) {  // imageless framebuffer: the views come with the begin
        const VkRenderPassAttachmentBeginInfo *ab = (const void *)c_chain(info->pNext, ST_RP_ATTACHMENT_BEGIN);
        if (ab && (uint32_t)index - 1 < ab->attachmentCount) view = H(ab->pAttachments[(uint32_t)index - 1]);
    }
    c_begin(cmd, (uint32_t)size, (uint32_t)(size >> 32), (VkImageView)(uintptr_t)view,
            c_pass_views((uint32_t)(lm >> 32), (uint32_t)lm), (uint32_t)(index >> 32), info->renderArea.extent);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdBeginRenderPass(VkCommandBuffer cmd, const VkRenderPassBeginInfo *info,
                                                       VkSubpassContents contents) {
    c_begin_rp(cmd, info);
    c_dev(cmd)->CmdBeginRenderPass(cmd, info, contents);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdBeginRenderPass2(VkCommandBuffer cmd, const VkRenderPassBeginInfo *info,
                                                        const VkSubpassBeginInfo *sub) {
    c_begin_rp(cmd, info);
    c_dev(cmd)->CmdBeginRenderPass2(cmd, info, sub);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdBeginRenderPass2KHR(VkCommandBuffer cmd, const VkRenderPassBeginInfo *info,
                                                           const VkSubpassBeginInfo *sub) {
    c_begin_rp(cmd, info);
    c_dev(cmd)->CmdBeginRenderPass2KHR(cmd, info, sub);
}
static void c_begin_rendering(VkCommandBuffer cmd, const VkRenderingInfo *info) {
    if (c_disabled || !info) return;
    const VkRenderingFragmentDensityMapAttachmentInfoEXT *f = (const void *)c_chain(info->pNext, ST_FDM_RENDERING_INFO);
    // Turnip: a dynamic-rendering framebuffer ends where the render area ends
    c_begin(cmd, (uint32_t)(info->renderArea.offset.x + (int32_t)info->renderArea.extent.width),
            (uint32_t)(info->renderArea.offset.y + (int32_t)info->renderArea.extent.height), f ? f->imageView : VK_NULL_HANDLE,
            c_pass_views(info->viewMask, info->layerCount), f && f->imageView ? c_dynamic_bin(info) : 0,
            info->renderArea.extent);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdBeginRendering(VkCommandBuffer cmd, const VkRenderingInfo *info) {
    c_begin_rendering(cmd, info);
    c_dev(cmd)->CmdBeginRendering(cmd, info);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdBeginRenderingKHR(VkCommandBuffer cmd, const VkRenderingInfo *info) {
    c_begin_rendering(cmd, info);
    c_dev(cmd)->CmdBeginRenderingKHR(cmd, info);
}

static int c_round(float v, uint32_t gran) {
    if (!gran) gran = 1;
    return (int)lroundf(v / (float)gran) * (int)gran;
}

// The density-map offsets this pass ends with: in = Valve's array (NULL: none). Writes the n offsets Turnip will read
// to out and returns n (0: leave the pass as it is). Turnip reads one offset per view (or per-layer map layer) whatever
// fragmentDensityOffsetCount says. Valve's layer computes one per view into its array but declares count 1 for a
// two-view pass, so with Valve alone Turnip reads the right eye's offset beyond the count: in[v] is read for every
// view here too (the same memory the driver reads), and every entry handed down is written.
static uint32_t c_offsets_for(VkCommandBuffer cmd, c_device *d, uint32_t in_count, const VkOffset2D *in,
                              VkOffset2D *out, c_pass_info *pi) {
    take_lock(&c_obj_lock);
    c_slot *s = c_find(c_cmds, H(cmd), 0);
    uint64_t size = s ? s->a : 0, map = s ? s->b : 0, flags = s ? s->c : 0, shape = s ? s->d : 0, area = s ? s->e : 0;
    if (s) s->key = ~0ull;  // one pass at a time per command buffer
    drop_lock(&c_obj_lock);
    uint32_t n = (uint32_t)(shape & 0xffff), first = (uint32_t)((shape >> 16) & 0xffff);
    uint32_t map_layers = (uint32_t)(shape >> 32);
    uint32_t fbw = (uint32_t)size, fbh = (uint32_t)(size >> 32), mw = (uint32_t)map, mh = (uint32_t)(map >> 32);
    // Turnip's bins: the per-game setting, else computed for this pass (0: undecided -> the texels' centre)
    uint32_t bin = (uint32_t)(flags >> 32), bin_w = bin & 0xffff, bin_h = bin >> 16;
    int bin_source = bin ? 1 : 0;
    if (c_bin_w && c_bin_h) bin_w = c_bin_w, bin_h = c_bin_h, bin_source = 2;
    float vx = c_sharp_centre(fbw, mw, 0, bin_w), vy = c_sharp_centre(fbh, mh, 1, bin_h);
    // Valve's render area (its gaze offsets are gaze NDC x half of it); the framebuffer when none was recorded
    uint32_t raw = (uint32_t)area ? (uint32_t)area : fbw, rah = (uint32_t)(area >> 32) ? (uint32_t)(area >> 32) : fbh;
    *pi = (c_pass_info){fbw, fbh, mw, mh, first, map_layers, map ? n : 0, (flags & FDM_OFFSET_FLAG) != 0,
                        {-1, -1, -1, -1}, {{0}}, vx, vy, bin_w, bin_h, bin_source, raw, rah};
    fp_fdm_view *views = pi->views;
    take_lock(&c_lock);
    uint32_t nviews = c_view_count;
    memcpy(views, c_views, sizeof(c_views));
    drop_lock(&c_lock);
    // A map Valve made without the offset flag (its offsets off): Turnip ignores offsets, change nothing.
    if (!map || !(flags & FDM_OFFSET_FLAG)) return 0;
    if (!n || n > FP_FDM_MAX_VIEWS) return 0;
    if (!in_count) in = NULL;  // no offsets from Valve at all
    // Valve's gaze (Eye-tracked); all zero when it has none (no eye tracking): straight ahead then
    int gaze = 0;
    for (uint32_t i = 0; !c_fixed && in && i < n; ++i) gaze |= in[i].x || in[i].y;
    int matched = 0;
    for (uint32_t v = 0; v < n; ++v) {
        VkOffset2D g = in ? in[v] : (VkOffset2D){0, 0};  // that eye's gaze offset
        out[v] = g;
        // the eye: the map layer this offset applies to (a one-eye pass gets the one-layer map view of its eye)
        uint32_t layer = first + (map_layers > 1 ? v : 0);
        const fp_fdm_view *e = NULL;
        int candidates = 0;
        for (uint32_t i = 0; i < nviews; ++i)
            if (views[i].width == fbw && views[i].height == fbh && views[i].layer == layer) e = &views[i], ++candidates;
        if (candidates != 1) continue;  // e.g. both eyes side by side in one layer: one map can't serve both
        pi->eye[v] = (int32_t)(e - views);
        float dx = e->cx, dy = e->cy;
        if (gaze) {
            // Valve: gaze NDC * half the pass's render area (not the framebuffer: Unreal-style passes render only
            // the eye's rect, In Death 1730 of 2768, #394/#520); the gaze NDC is in the eye's view = its rect
            dx = (float)e->x + (float)e->w / 2.0f + (float)g.x * (float)e->w / (float)raw;
            dy = (float)e->y + (float)e->h / 2.0f + (float)g.y * (float)e->h / (float)rah;
        }
        out[v].x = c_round(dx - vx, d->gran.width);
        out[v].y = c_round(dy - vy, d->gran.height);
        ++matched;
    }
    static int logged;
    if (matched && logged++ < 8)
        LOG("fdm_centre: pass %ux%u (render area %ux%u) map %ux%u layer %u+%u (preset %d, bins %ux%u %s, sharp centre "
            "%.0f,%.0f, texel centre %.0f,%.0f, %s): %u view(s), %u offset(s) from Valve %d,%d -> %d,%d%s", fbw, fbh,
            raw, rah, mw, mh, first,
            map_layers, c_level, bin_w, bin_h, bin_source == 2 ? "from fdm_bin_w/_h" : bin_source ? "computed" : "unknown",
            vx, vy, c_valve_centre(fbw, mw, 0), c_valve_centre(fbh, mh, 1), c_fixed ? "Fixed" : gaze ? "gaze" : "no gaze",
            n, in_count, in_count ? in[0].x : 0, in_count ? in[0].y : 0, out[0].x, out[0].y,
            n > 1 ? " (+ more views)" : "");
    return matched ? n : 0;
}
static uint32_t c_offsets(VkCommandBuffer cmd, c_device *d, uint32_t in_count, const VkOffset2D *in, VkOffset2D *out) {
    c_pass_info pi;
    memset(&pi, 0, sizeof(pi));
    uint32_t n = c_offsets_for(cmd, d, in_count, in, out, &pi);
    if (c_diag && pi.n) c_diag_pass(cmd, &pi, in_count, in_count ? in : NULL, n ? out : in, n ? n : in_count);
    return n;
}

static VKAPI_ATTR void VKAPI_CALL c_end_rp2(VkCommandBuffer cmd, const VkSubpassEndInfo *info, int khr) {
    c_device *d = c_dev(cmd);
    PFN_vkCmdEndRenderPass2 next = khr && d->CmdEndRenderPass2KHR ? d->CmdEndRenderPass2KHR : d->CmdEndRenderPass2;
    const c_offset_end *valve = info ? (const void *)c_chain(info->pNext, ST_FDM_OFFSET_END) : NULL;
    VkOffset2D offsets[FP_FDM_MAX_VIEWS];
    uint32_t n = c_disabled ? 0 : c_offsets(cmd, d, valve ? valve->count : 0, valve ? valve->offsets : NULL, offsets);
    if (!n || (valve && info->pNext != (const void *)valve)) {  // nothing to change, or not first in the chain
        next(cmd, info);
        return;
    }
    c_offset_end mine = {ST_FDM_OFFSET_END, valve ? valve->pNext : info->pNext, n, offsets};
    VkSubpassEndInfo copy = *info;
    copy.pNext = &mine;
    next(cmd, &copy);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdEndRenderPass2(VkCommandBuffer cmd, const VkSubpassEndInfo *info) {
    c_end_rp2(cmd, info, 0);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdEndRenderPass2KHR(VkCommandBuffer cmd, const VkSubpassEndInfo *info) {
    c_end_rp2(cmd, info, 1);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdEndRenderPass(VkCommandBuffer cmd) {
    c_device *d = c_dev(cmd);
    VkOffset2D offsets[FP_FDM_MAX_VIEWS];
    uint32_t n = c_disabled || !d->CmdEndRenderPass2 ? 0 : c_offsets(cmd, d, 0, NULL, offsets);
    if (!n) {
        d->CmdEndRenderPass(cmd);
        return;
    }
    c_offset_end mine = {ST_FDM_OFFSET_END, NULL, n, offsets};
    VkSubpassEndInfo info = {VK_STRUCTURE_TYPE_SUBPASS_END_INFO, &mine};
    d->CmdEndRenderPass2(cmd, &info);
}
static void c_end_rendering2(VkCommandBuffer cmd, const c_rendering_end *info, PFN_fp_EndRendering2 next) {
    c_device *d = c_dev(cmd);
    const c_offset_end *valve = info ? (const void *)c_chain(info->pNext, ST_FDM_OFFSET_END) : NULL;
    VkOffset2D offsets[FP_FDM_MAX_VIEWS];
    uint32_t n = c_disabled ? 0 : c_offsets(cmd, d, valve ? valve->count : 0, valve ? valve->offsets : NULL, offsets);
    if (!n || (valve && info->pNext != (const void *)valve)) {
        next(cmd, info);
        return;
    }
    c_offset_end mine = {ST_FDM_OFFSET_END, valve ? valve->pNext : (info ? info->pNext : NULL), n, offsets};
    c_rendering_end copy = {ST_RENDERING_END, &mine};
    next(cmd, &copy);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdEndRendering2EXT(VkCommandBuffer cmd, const void *info) {
    c_end_rendering2(cmd, info, c_dev(cmd)->CmdEndRendering2EXT);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdEndRendering2KHR(VkCommandBuffer cmd, const void *info) {
    c_end_rendering2(cmd, info, c_dev(cmd)->CmdEndRendering2KHR);
}
static void c_end_rendering(VkCommandBuffer cmd, PFN_vkCmdEndRendering plain) {
    c_device *d = c_dev(cmd);
    PFN_fp_EndRendering2 next = d->CmdEndRendering2EXT ? d->CmdEndRendering2EXT : d->CmdEndRendering2KHR;
    VkOffset2D offsets[FP_FDM_MAX_VIEWS];
    uint32_t n = c_disabled || !next ? 0 : c_offsets(cmd, d, 0, NULL, offsets);
    if (!n) {
        plain(cmd);
        return;
    }
    c_offset_end mine = {ST_FDM_OFFSET_END, NULL, n, offsets};
    c_rendering_end info = {ST_RENDERING_END, &mine};
    next(cmd, &info);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdEndRendering(VkCommandBuffer cmd) {
    c_end_rendering(cmd, c_dev(cmd)->CmdEndRendering);
}
static VKAPI_ATTR void VKAPI_CALL c_CmdEndRenderingKHR(VkCommandBuffer cmd) {
    c_end_rendering(cmd, c_dev(cmd)->CmdEndRenderingKHR);
}

// ---------------------------------------------------------------- layer plumbing

static PFN_vkGetInstanceProcAddr c_instance_gipa(const void *handle, VkInstance *instance) {
    void *key = key_of(handle);
    PFN_vkGetInstanceProcAddr found = NULL, last = NULL;
    take_lock(&c_dev_lock);
    for (int i = 0; i < MAX_HANDLES; i++) {
        if (!c_instances[i].gipa) continue;
        last = c_instances[i].gipa;
        if (instance) *instance = c_instances[i].instance;
        if (c_instances[i].key == key) { found = c_instances[i].gipa; break; }
    }
    drop_lock(&c_dev_lock);
    return found ? found : last;
}

static VKAPI_ATTR VkResult VKAPI_CALL c_CreateInstance(const VkInstanceCreateInfo *ci, const VkAllocationCallbacks *a,
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
    take_lock(&c_dev_lock);
    int slot = 0;
    for (int i = 0; i < MAX_HANDLES; i++)
        if (!c_instances[i].gipa || c_instances[i].key == key_of(*instance)) { slot = i; break; }
    c_instances[slot].key = key_of(*instance);
    c_instances[slot].instance = *instance;
    c_instances[slot].gipa = next;
    drop_lock(&c_dev_lock);
    return r;
}

static VKAPI_ATTR VkResult VKAPI_CALL c_CreateDevice(VkPhysicalDevice pd, const VkDeviceCreateInfo *ci,
                                                     const VkAllocationCallbacks *a, VkDevice *device) {
    VkLayerDeviceCreateInfo *link = (VkLayerDeviceCreateInfo *)ci->pNext;
    while (link && !(link->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO && link->function == VK_LAYER_LINK_INFO))
        link = (VkLayerDeviceCreateInfo *)link->pNext;
    if (!link || !link->u.pLayerInfo) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr next_gipa = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr next_gdpa = link->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;
    VkInstance instance = VK_NULL_HANDLE;
    c_instance_gipa(pd, &instance);
    PFN_vkCreateDevice create = (PFN_vkCreateDevice)next_gipa(instance, "vkCreateDevice");
    if (!create) return VK_ERROR_INITIALIZATION_FAILED;
    VkResult r = create(pd, ci, a, device);
    if (r != VK_SUCCESS) return r;
    c_device d;
    memset(&d, 0, sizeof(d));
    d.key = key_of(*device), d.device = *device, d.gdpa = next_gdpa, d.gran = (VkExtent2D){1, 1};
#define C_GET(field, name) d.field = (__typeof__(d.field))next_gdpa(*device, name);
    C_GET(CreateImage, "vkCreateImage") C_GET(DestroyImage, "vkDestroyImage")
    C_GET(CreateImageView, "vkCreateImageView") C_GET(DestroyImageView, "vkDestroyImageView")
    C_GET(CreateRenderPass, "vkCreateRenderPass") C_GET(CreateRenderPass2, "vkCreateRenderPass2")
    C_GET(CreateRenderPass2KHR, "vkCreateRenderPass2KHR") C_GET(DestroyRenderPass, "vkDestroyRenderPass")
    C_GET(CreateFramebuffer, "vkCreateFramebuffer") C_GET(DestroyFramebuffer, "vkDestroyFramebuffer")
    C_GET(CmdBeginRenderPass, "vkCmdBeginRenderPass") C_GET(CmdBeginRenderPass2, "vkCmdBeginRenderPass2")
    C_GET(CmdBeginRenderPass2KHR, "vkCmdBeginRenderPass2KHR") C_GET(CmdEndRenderPass, "vkCmdEndRenderPass")
    C_GET(CmdEndRenderPass2, "vkCmdEndRenderPass2") C_GET(CmdEndRenderPass2KHR, "vkCmdEndRenderPass2KHR")
    C_GET(CmdBeginRendering, "vkCmdBeginRendering") C_GET(CmdBeginRenderingKHR, "vkCmdBeginRenderingKHR")
    C_GET(CmdEndRendering, "vkCmdEndRendering") C_GET(CmdEndRenderingKHR, "vkCmdEndRenderingKHR")
    C_GET(CmdEndRendering2EXT, "vkCmdEndRendering2EXT") C_GET(CmdEndRendering2KHR, "vkCmdEndRendering2KHR")
    C_GET(CreateShaderModule, "vkCreateShaderModule") C_GET(DestroyShaderModule, "vkDestroyShaderModule")
    C_GET(CreateGraphicsPipelines, "vkCreateGraphicsPipelines") C_GET(DestroyPipeline, "vkDestroyPipeline")
    C_GET(CmdBindPipeline, "vkCmdBindPipeline") C_GET(BeginCommandBuffer, "vkBeginCommandBuffer")
    C_GET(CmdExecuteCommands, "vkCmdExecuteCommands")
#undef C_GET
    if (c_diag) {
        // VkPhysicalDeviceMultiviewPerViewViewportsFeaturesQCOM: with it Turnip keeps per-view viewports for every
        // pipeline that doesn't write ViewportIndex
        const VkBaseInStructure *pv = c_chain(ci->pNext, (VkStructureType)1000488000);
        c_app_per_view_viewports = pv && ((const struct { VkStructureType t; const void *n; VkBool32 on; } *)pv)->on;
        uint32_t fdm_feature = 0;
        const VkBaseInStructure *fd = c_chain(ci->pNext, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FRAGMENT_DENSITY_MAP_FEATURES_EXT);
        if (fd) fdm_feature = ((const VkPhysicalDeviceFragmentDensityMapFeaturesEXT *)fd)->fragmentDensityMap;
        int multiview = 0;
        const VkBaseInStructure *mv = c_chain(ci->pNext, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MULTIVIEW_FEATURES);
        if (mv) multiview = ((const VkPhysicalDeviceMultiviewFeatures *)mv)->multiview;
        const VkBaseInStructure *v11 = c_chain(ci->pNext, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_1_FEATURES);
        if (v11) multiview |= ((const VkPhysicalDeviceVulkan11Features *)v11)->multiview;
        char exts[512] = "";
        for (uint32_t i = 0; i < ci->enabledExtensionCount; ++i)
            if (strstr(ci->ppEnabledExtensionNames[i], "density") || strstr(ci->ppEnabledExtensionNames[i], "multiview") ||
                strstr(ci->ppEnabledExtensionNames[i], "dynamic_rendering") || strstr(ci->ppEnabledExtensionNames[i], "library"))
                snprintf(exts + strlen(exts), sizeof(exts) - strlen(exts), " %s", ci->ppEnabledExtensionNames[i]);
        LOG("fdm_diag: device %p: multiviewPerViewViewports %s, fragmentDensityMap feature %u, multiview %d, "
            "extensions:%s", (void *)*device, c_app_per_view_viewports ? "ENABLED by the app" : "off", fdm_feature,
            multiview, exts);
    }
    PFN_vkGetPhysicalDeviceProperties2 props2 =
        (PFN_vkGetPhysicalDeviceProperties2)next_gipa(instance, "vkGetPhysicalDeviceProperties2");
    VkPhysicalDeviceDriverProperties driver;
    memset(&driver, 0, sizeof(driver));
    driver.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES;
    struct { VkStructureType sType; void *pNext; VkExtent2D gran; } offset_props = {ST_FDM_OFFSET_PROPS, &driver, {0, 0}};
    VkPhysicalDeviceProperties2 p2;
    memset(&p2, 0, sizeof(p2));
    p2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, p2.pNext = &offset_props;
    if (props2) props2(pd, &p2);
    if (offset_props.gran.width && offset_props.gran.height) d.gran = offset_props.gran;
    // Turnip's bin size is only modelled for the Frame's GPU (tu_bins.h); anything else keeps the texels' centre
    const tb_gpu *gpu = driver.driverID == VK_DRIVER_ID_MESA_TURNIP &&
                        strstr(p2.properties.deviceName, "Adreno (TM) 750") ? &TB_ADRENO_750 : NULL;
    if (gpu) c_gpu = gpu;
    take_lock(&c_dev_lock);
    int slot = 0;
    for (int i = 0; i < MAX_HANDLES; i++)
        if (!c_devices[i].gdpa || c_devices[i].key == key_of(*device)) { slot = i; break; }
    c_devices[slot] = d;
    drop_lock(&c_dev_lock);
    static int logged;
    if (logged++ < 4)
        LOG("fdm_centre: active on device %p (preset %d, %s, granularity %ux%u; %s, %s %s: bins %s)", (void *)*device,
            c_level, c_fixed ? "Fixed: optical centre" : "gaze", d.gran.width, d.gran.height,
            p2.properties.deviceName, driver.driverName, driver.driverInfo,
            gpu ? "computed as Turnip does" : "unknown (texel centre)");
    return r;
}

static PFN_vkVoidFunction c_own(const char *name, const c_device *d);

static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL c_GetDeviceProcAddr(VkDevice device, const char *name) {
    if (name && !strcmp(name, "vkGetDeviceProcAddr")) return (PFN_vkVoidFunction)c_GetDeviceProcAddr;
    c_device *d = c_dev(device);
    PFN_vkVoidFunction fn = c_own(name, d);
    if (fn) return fn;
    return d ? d->gdpa(device, name) : NULL;
}
static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL c_GetInstanceProcAddr(VkInstance instance, const char *name) {
    if (name && !strcmp(name, "vkCreateInstance")) return (PFN_vkVoidFunction)c_CreateInstance;
    if (name && !strcmp(name, "vkCreateDevice")) return (PFN_vkVoidFunction)c_CreateDevice;
    if (name && !strcmp(name, "vkGetInstanceProcAddr")) return (PFN_vkVoidFunction)c_GetInstanceProcAddr;
    if (name && !strcmp(name, "vkGetDeviceProcAddr")) return (PFN_vkVoidFunction)c_GetDeviceProcAddr;
    if (name && !strncmp(name, "vkEnumerate", 11)) {
        PFN_vkVoidFunction fn = own(name);  // the library's layer enumeration
        if (fn) return fn;
    }
    PFN_vkVoidFunction fn = c_own(name, NULL);
    if (fn) return fn;
    PFN_vkGetInstanceProcAddr next = instance ? c_instance_gipa(instance, NULL) : NULL;
    return next ? next(instance, name) : NULL;
}

// Device functions are only ours where the next layer has them (a NULL stays NULL for the app).
static PFN_vkVoidFunction c_own(const char *name, const c_device *d) {
    if (!name) return NULL;
#define C_OWN(fn, impl, field) if (!strcmp(name, fn)) return (!d || d->field) ? (PFN_vkVoidFunction)impl : NULL;
    C_OWN("vkCreateImage", c_CreateImage, CreateImage) C_OWN("vkDestroyImage", c_DestroyImage, DestroyImage)
    C_OWN("vkCreateImageView", c_CreateImageView, CreateImageView)
    C_OWN("vkDestroyImageView", c_DestroyImageView, DestroyImageView)
    C_OWN("vkCreateRenderPass", c_CreateRenderPass, CreateRenderPass)
    C_OWN("vkCreateRenderPass2", c_CreateRenderPass2, CreateRenderPass2)
    C_OWN("vkCreateRenderPass2KHR", c_CreateRenderPass2KHR, CreateRenderPass2KHR)
    C_OWN("vkDestroyRenderPass", c_DestroyRenderPass, DestroyRenderPass)
    C_OWN("vkCreateFramebuffer", c_CreateFramebuffer, CreateFramebuffer)
    C_OWN("vkDestroyFramebuffer", c_DestroyFramebuffer, DestroyFramebuffer)
    C_OWN("vkCmdBeginRenderPass", c_CmdBeginRenderPass, CmdBeginRenderPass)
    C_OWN("vkCmdBeginRenderPass2", c_CmdBeginRenderPass2, CmdBeginRenderPass2)
    C_OWN("vkCmdBeginRenderPass2KHR", c_CmdBeginRenderPass2KHR, CmdBeginRenderPass2KHR)
    C_OWN("vkCmdEndRenderPass", c_CmdEndRenderPass, CmdEndRenderPass)
    C_OWN("vkCmdEndRenderPass2", c_CmdEndRenderPass2, CmdEndRenderPass2)
    C_OWN("vkCmdEndRenderPass2KHR", c_CmdEndRenderPass2KHR, CmdEndRenderPass2KHR)
    C_OWN("vkCmdBeginRendering", c_CmdBeginRendering, CmdBeginRendering)
    C_OWN("vkCmdBeginRenderingKHR", c_CmdBeginRenderingKHR, CmdBeginRenderingKHR)
    C_OWN("vkCmdEndRendering", c_CmdEndRendering, CmdEndRendering)
    C_OWN("vkCmdEndRenderingKHR", c_CmdEndRenderingKHR, CmdEndRenderingKHR)
    C_OWN("vkCmdEndRendering2EXT", c_CmdEndRendering2EXT, CmdEndRendering2EXT)
    C_OWN("vkCmdEndRendering2KHR", c_CmdEndRendering2KHR, CmdEndRendering2KHR)
    if (c_diag) {
        C_OWN("vkCreateShaderModule", c_CreateShaderModule, CreateShaderModule)
        C_OWN("vkDestroyShaderModule", c_DestroyShaderModule, DestroyShaderModule)
        C_OWN("vkCreateGraphicsPipelines", c_CreateGraphicsPipelines, CreateGraphicsPipelines)
        C_OWN("vkDestroyPipeline", c_DestroyPipeline, DestroyPipeline)
        C_OWN("vkCmdBindPipeline", c_CmdBindPipeline, CmdBindPipeline)
        C_OWN("vkBeginCommandBuffer", c_BeginCommandBuffer, BeginCommandBuffer)
        C_OWN("vkCmdExecuteCommands", c_CmdExecuteCommands, CmdExecuteCommands)
    }
#undef C_OWN
    return NULL;
}

EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL VK_LAYER_FRAMEPORT_fdm_centreGetInstanceProcAddr(VkInstance i, const char *n) {
    return c_GetInstanceProcAddr(i, n);
}
EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL VK_LAYER_FRAMEPORT_fdm_centreGetDeviceProcAddr(VkDevice d, const char *n) {
    return c_GetDeviceProcAddr(d, n);
}

// Valve's preset and offset choice, read the way its layer reads them (FOVE_LEVEL wins over FDM_DEBUG lo/med/hi).
static int c_has_token(const char *list, const char *token) {
    size_t n = strlen(token);
    for (const char *p = list; p && *p;) {
        while (*p && !((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9') || *p == '_')) ++p;
        const char *start = p;
        while ((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9') || *p == '_') ++p;
        if ((size_t)(p - start) == n && !strncmp(start, token, n)) return 1;
    }
    return 0;
}
static void c_read_config(void) {
    const char *flags = getenv("FDM_DEBUG"), *level = getenv("FOVE_LEVEL"), *off = getenv("FRAMEPORT_FDM_CENTRE");
    c_fixed = c_has_token(flags, "disable_offsets");
    c_level = c_has_token(flags, "hi") ? 3 : c_has_token(flags, "med") ? 2 : 1;
    int explicit = c_has_token(level, "hi") ? 3 : c_has_token(level, "med") ? 2 : c_has_token(level, "lo") ? 1 : 0;
    if (explicit) c_level = explicit;
    c_disabled = off && !strcmp(off, "0");
    const char *diag = getenv("FRAMEPORT_FDM_DIAG");
    c_diag = diag && !strcmp(diag, "1");
}

// Fixed mode: let Valve run with its offsets on (it then flags the images it should, and passes per-view offsets
// this layer overrides with the optical centre) instead of disable_offsets, which leaves maps without the offset flag
// so no offset can move them. Done in this process before Valve's layer reads FDM_DEBUG (Mesa caches options at the
// first read); if it already read it, its maps carry no offset flag and every pass is left alone (Valve's Fixed).
static void c_fixed_with_offsets(void) {
    const char *flags = getenv("FDM_DEBUG");
    if (!c_fixed || !flags || strlen(flags) >= 512) return;
    char out[512] = "";
    for (const char *p = flags; *p;) {
        const char *start = p;
        while (*p && *p != ',') ++p;
        size_t n = (size_t)(p - start);
        if (n && !(n == 15 && !strncmp(start, "disable_offsets", 15)))
            snprintf(out + strlen(out), sizeof(out) - strlen(out), "%s%.*s", *out ? "," : "", (int)n, start);
        if (*p) ++p;
    }
    setenv("FDM_DEBUG", out, 1);
    LOG("fdm_centre: Fixed: FDM_DEBUG \"%s\" -> \"%s\" (Valve's offsets on, overridden with each eye's centre)", flags,
        out);
}
