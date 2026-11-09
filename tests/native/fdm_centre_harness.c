// Host harness for native/foveation/centre.h (GitHub #394): a fake next layer records the density-map offsets the
// centring layer hands down for the render passes Valve's foveation layer would record.
#define _POSIX_C_SOURCE 200809L
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>
#define CHECK(c)                                                                                                     \
    do {                                                                                                               \
        if (!(c)) {                                                                                                    \
            fprintf(stderr, "line %d: %s\n", __LINE__, #c);                                                            \
            return 1;                                                                                                  \
        }                                                                                                              \
    } while (0)
#include <stdarg.h>
static char last_log[1024];
static void harness_log(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(last_log, sizeof(last_log), fmt, ap);
    va_end(ap);
}
#define LOG(...) harness_log(__VA_ARGS__)
#define EXPORT
#define MAX_HANDLES 16
static void take_lock(int *p) { while (__atomic_exchange_n(p, 1, __ATOMIC_ACQUIRE)) {} }
static void drop_lock(int *p) { __atomic_store_n(p, 0, __ATOMIC_RELEASE); }
static void *key_of(const void *handle) { return handle ? *(void *const *)handle : NULL; }
static PFN_vkVoidFunction own(const char *name) { (void)name; return NULL; }
#include "centre.h"

// ---- fake next layer
static void *dispatch_key = (void *)0x1234;
static struct { void *key; } device_obj = {(void *)0x1234}, cmd_obj = {(void *)0x1234};
static uintptr_t next_handle = 0x1000;
static VkImageCreateFlags last_image_flags;
static int ends, image_changed;
static uint32_t got_count;
static VkOffset2D got[4];

static VKAPI_ATTR VkResult VKAPI_CALL n_CreateImage(VkDevice d, const VkImageCreateInfo *ci, const VkAllocationCallbacks *a,
                                                    VkImage *img) {
    (void)d, (void)a;
    last_image_flags = ci->flags;
    *img = (VkImage)(next_handle += 8);
    return VK_SUCCESS;
}
static VKAPI_ATTR VkResult VKAPI_CALL n_CreateImageView(VkDevice d, const VkImageViewCreateInfo *ci,
                                                        const VkAllocationCallbacks *a, VkImageView *v) {
    (void)d, (void)ci, (void)a;
    *v = (VkImageView)(next_handle += 8);
    return VK_SUCCESS;
}
static VKAPI_ATTR VkResult VKAPI_CALL n_CreateRenderPass2(VkDevice d, const VkRenderPassCreateInfo2 *ci,
                                                          const VkAllocationCallbacks *a, VkRenderPass *rp) {
    (void)d, (void)ci, (void)a;
    *rp = (VkRenderPass)(next_handle += 8);
    return VK_SUCCESS;
}
static VKAPI_ATTR VkResult VKAPI_CALL n_CreateFramebuffer(VkDevice d, const VkFramebufferCreateInfo *ci,
                                                          const VkAllocationCallbacks *a, VkFramebuffer *fb) {
    (void)d, (void)ci, (void)a;
    *fb = (VkFramebuffer)(next_handle += 8);
    return VK_SUCCESS;
}
static VKAPI_ATTR VkResult VKAPI_CALL n_CreateGraphicsPipelines(VkDevice d, VkPipelineCache c, uint32_t n,
                                                                const VkGraphicsPipelineCreateInfo *ci,
                                                                const VkAllocationCallbacks *a, VkPipeline *p) {
    (void)d, (void)c, (void)ci, (void)a;
    for (uint32_t i = 0; i < n; ++i) p[i] = (VkPipeline)(next_handle += 8);
    return VK_SUCCESS;
}
static int binds_seen;
static VKAPI_ATTR void VKAPI_CALL n_CmdBindPipeline(VkCommandBuffer c, VkPipelineBindPoint b, VkPipeline p) {
    (void)c, (void)b, (void)p;
    ++binds_seen;
}
static VkPipeline bind_in_pass;  // FRAMEPORT_FDM_DIAG tests: bound between begin and end
static VkRenderPass last_rp;
static VKAPI_ATTR void VKAPI_CALL n_CmdBeginRenderPass2(VkCommandBuffer c, const VkRenderPassBeginInfo *b,
                                                        const VkSubpassBeginInfo *s) {
    (void)c, (void)b, (void)s;
}
static void record(const void *next) {
    ++ends;
    got_count = 0;
    const c_offset_end *o = (const void *)c_chain(next, ST_FDM_OFFSET_END);
    if (o) {
        got_count = o->count;
        memcpy(got, o->offsets, o->count * sizeof(VkOffset2D));
    }
}
static VKAPI_ATTR void VKAPI_CALL n_CmdEndRenderPass2(VkCommandBuffer c, const VkSubpassEndInfo *e) {
    (void)c;
    record(e ? e->pNext : NULL);
}
static VKAPI_ATTR void VKAPI_CALL n_CmdEndRenderPass(VkCommandBuffer c) {
    (void)c;
    record(NULL);
}

// One pass into a w x h swapchain with Valve's ceil(w/32) x ceil(h/32) two-layer map: multiview (view_mask 3, the
// whole map) or a one-eye pass (view_mask 0, the map view of `eye`'s layer, as Valve attaches it); ends with `valve`
// offsets (NULL: none). `flagged`: the map has the offset flag (Valve's offsets on).
static VkExtent2D render_area;  // pass_ex's render area; 0: the whole framebuffer
static void pass_ex(uint32_t w, uint32_t h, const VkOffset2D *valve, uint32_t count, uint32_t view_mask, uint32_t eye,
                    int flagged) {
    VkDevice dev = (VkDevice)&device_obj;
    VkCommandBuffer cmd = (VkCommandBuffer)&cmd_obj;
    VkImageCreateInfo ici = {VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ici.imageType = VK_IMAGE_TYPE_2D;
    ici.extent = (VkExtent3D){(w + 31) / 32, (h + 31) / 32, 1};
    ici.arrayLayers = 2;
    ici.usage = FDM_USAGE;
    ici.flags = flagged ? FDM_OFFSET_FLAG : 0;
    VkImage map;
    c_CreateImage(dev, &ici, NULL, &map);
    if ((last_image_flags & FDM_OFFSET_FLAG) != (ici.flags & FDM_OFFSET_FLAG)) image_changed = 1;
    VkImageViewCreateInfo vci = {VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    vci.image = map;
    vci.subresourceRange.baseArrayLayer = view_mask ? 0 : eye;
    vci.subresourceRange.layerCount = view_mask ? 2 : 1;
    VkImageView view;
    c_CreateImageView(dev, &vci, NULL, &view);
    VkRenderPassFragmentDensityMapCreateInfoEXT fdm = {ST_FDM_RP_INFO, NULL, {1, VK_IMAGE_LAYOUT_GENERAL}};
    VkSubpassDescription2 sub = {VK_STRUCTURE_TYPE_SUBPASS_DESCRIPTION_2};
    sub.viewMask = view_mask;
    VkRenderPassCreateInfo2 rci = {VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO_2, &fdm};
    rci.subpassCount = 1, rci.pSubpasses = &sub;
    VkRenderPass rp;
    c_CreateRenderPass2(dev, &rci, NULL, &rp);
    VkImageView atts[2] = {(VkImageView)0x99, view};
    VkFramebufferCreateInfo fci = {VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO};
    fci.renderPass = rp, fci.attachmentCount = 2, fci.pAttachments = atts, fci.width = w, fci.height = h, fci.layers = 1;
    VkFramebuffer fb;
    c_CreateFramebuffer(dev, &fci, NULL, &fb);
    VkRenderPassBeginInfo bi = {VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO};
    bi.renderPass = rp, bi.framebuffer = fb, bi.renderArea.extent = render_area.width ? render_area : (VkExtent2D){w, h};
    c_CmdBeginRenderPass2(cmd, &bi, NULL);
    last_rp = rp;
    if (bind_in_pass) c_CmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, bind_in_pass);
    if (valve) {
        c_offset_end o = {ST_FDM_OFFSET_END, NULL, count, valve};
        VkSubpassEndInfo e = {VK_STRUCTURE_TYPE_SUBPASS_END_INFO, &o};
        c_CmdEndRenderPass2(cmd, &e);
    } else {
        c_CmdEndRenderPass(cmd);
    }
}
static void pass(uint32_t w, uint32_t h, const VkOffset2D *valve, uint32_t count) {
    pass_ex(w, h, valve, count, 3, 0, 1);
}

static int near(int got_value, float want, int tolerance) { return fabsf((float)got_value - want) <= (float)tolerance; }

// A two-view pass like In Death's eye pass: colour + depth/stencil (samples each) and Valve's density map, into a
// w x h framebuffer whose depth image has the density-map-offset flag (Valve flags it). color: the colour format.
static void eye_pass(uint32_t w, uint32_t h, VkFormat color, VkSampleCountFlagBits samples, const VkOffset2D *valve,
                     uint32_t count) {
    VkDevice dev = (VkDevice)&device_obj;
    VkCommandBuffer cmd = (VkCommandBuffer)&cmd_obj;
    VkImageCreateInfo ici = {VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ici.imageType = VK_IMAGE_TYPE_2D;
    ici.extent = (VkExtent3D){(w + 31) / 32, (h + 31) / 32, 1};
    ici.arrayLayers = 2, ici.usage = FDM_USAGE, ici.flags = FDM_OFFSET_FLAG, ici.samples = VK_SAMPLE_COUNT_1_BIT;
    VkImage map, col, depth;
    c_CreateImage(dev, &ici, NULL, &map);
    ici.extent = (VkExtent3D){w, h, 1};
    ici.samples = samples, ici.format = color, ici.usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    c_CreateImage(dev, &ici, NULL, &col);
    ici.format = VK_FORMAT_D24_UNORM_S8_UINT, ici.usage = VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;
    c_CreateImage(dev, &ici, NULL, &depth);
    VkImageViewCreateInfo vci = {VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    vci.subresourceRange.layerCount = 2;
    VkImageView views[3];
    vci.image = col, vci.format = color;
    c_CreateImageView(dev, &vci, NULL, &views[0]);
    vci.image = depth, vci.format = VK_FORMAT_D24_UNORM_S8_UINT;
    c_CreateImageView(dev, &vci, NULL, &views[1]);
    vci.image = map, vci.format = VK_FORMAT_R8G8_UNORM;
    c_CreateImageView(dev, &vci, NULL, &views[2]);
    VkAttachmentDescription2 atts[3] = {{VK_STRUCTURE_TYPE_ATTACHMENT_DESCRIPTION_2}, {VK_STRUCTURE_TYPE_ATTACHMENT_DESCRIPTION_2},
                                        {VK_STRUCTURE_TYPE_ATTACHMENT_DESCRIPTION_2}};
    atts[0].format = color, atts[0].samples = samples;
    atts[1].format = VK_FORMAT_D24_UNORM_S8_UINT, atts[1].samples = samples;
    atts[2].format = VK_FORMAT_R8G8_UNORM, atts[2].samples = VK_SAMPLE_COUNT_1_BIT;
    VkAttachmentReference2 cref = {VK_STRUCTURE_TYPE_ATTACHMENT_REFERENCE_2, NULL, 0}, dref = {VK_STRUCTURE_TYPE_ATTACHMENT_REFERENCE_2, NULL, 1};
    VkSubpassDescription2 sub = {VK_STRUCTURE_TYPE_SUBPASS_DESCRIPTION_2};
    sub.viewMask = 3, sub.colorAttachmentCount = 1, sub.pColorAttachments = &cref, sub.pDepthStencilAttachment = &dref;
    VkRenderPassFragmentDensityMapCreateInfoEXT fdm = {ST_FDM_RP_INFO, NULL, {2, VK_IMAGE_LAYOUT_GENERAL}};
    VkRenderPassCreateInfo2 rci = {VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO_2, &fdm};
    rci.attachmentCount = 3, rci.pAttachments = atts, rci.subpassCount = 1, rci.pSubpasses = &sub;
    VkRenderPass rp;
    c_CreateRenderPass2(dev, &rci, NULL, &rp);
    VkFramebufferCreateInfo fci = {VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO};
    fci.renderPass = rp, fci.attachmentCount = 3, fci.pAttachments = views, fci.width = w, fci.height = h, fci.layers = 1;
    VkFramebuffer fb;
    c_CreateFramebuffer(dev, &fci, NULL, &fb);
    VkRenderPassBeginInfo bi = {VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO};
    bi.renderPass = rp, bi.framebuffer = fb, bi.renderArea.extent = (VkExtent2D){w, h};
    c_CmdBeginRenderPass2(cmd, &bi, NULL);
    c_offset_end o = {ST_FDM_OFFSET_END, NULL, count, valve};
    VkSubpassEndInfo e = {VK_STRUCTURE_TYPE_SUBPASS_END_INFO, &o};
    c_CmdEndRenderPass2(cmd, &e);
}

int main(void) {
    (void)dispatch_key;
    c_device d;
    memset(&d, 0, sizeof(d));
    d.key = device_obj.key, d.device = (VkDevice)&device_obj, d.gdpa = (PFN_vkGetDeviceProcAddr)1, d.gran = (VkExtent2D){8, 8};
    d.CreateImage = n_CreateImage, d.CreateImageView = n_CreateImageView, d.CreateRenderPass2 = n_CreateRenderPass2;
    d.CreateFramebuffer = n_CreateFramebuffer, d.CmdBeginRenderPass2 = n_CmdBeginRenderPass2;
    d.CmdEndRenderPass2 = n_CmdEndRenderPass2, d.CmdEndRenderPass = n_CmdEndRenderPass;
    d.CreateGraphicsPipelines = n_CreateGraphicsPipelines, d.CmdBindPipeline = n_CmdBindPipeline;
    c_devices[0] = d;

    // Valve's lo centre on a 2016x1728 swapchain (63x54 map, 32 px texels): texels 23-40 and 20-34
    CHECK(c_valve_centre(2016, 63, 0) == 1024.0f && c_valve_centre(1728, 54, 1) == 880.0f);
    c_level = 3;
    CHECK(c_valve_centre(2016, 63, 0) == 1024.0f);  // hi: texels 24-39
    c_level = 1;

    // AC Nexus' asymmetric eyes: rect=0,0 / 287,0 1726x1728 inside 2016x1728, optical centres from the FOV
    fp_fdm_view views[2] = {{2016, 1728, 0, 0, 0, 1726, 1728, 1002, 696}, {2016, 1728, 1, 287, 0, 1726, 1728, 1001, 709}};
    fp_fdm_set_views(views, 2);

    // Eye-tracked with no gaze (all 0): straight ahead = the optical centre. Both offsets Turnip reads are written.
    c_fixed = 0;
    VkOffset2D zero[2] = {{0, 0}, {0, 0}};
    pass(2016, 1728, zero, 2);
    CHECK(ends == 1 && got_count == 2);
    CHECK(near(got[0].x, 1002 - 1024, 4) && near(got[0].y, 696 - 880, 4));
    CHECK(near(got[1].x, 1001 - 1024, 4) && near(got[1].y, 709 - 880, 4));
    CHECK(got[0].x % 8 == 0 && got[0].y % 8 == 0);
    CHECK(!image_changed);  // images are never re-flagged

    // Valve declares 1 offset for a two-view pass: Turnip still reads 2, so the layer hands down 2
    pass(2016, 1728, zero, 1);
    CHECK(got_count == 2 && near(got[1].x, 1001 - 1024, 4) && near(got[1].y, 709 - 880, 4));

    // A one-eye pass (no multiview) with the right eye's map layer: its single offset is the right eye's
    pass_ex(2016, 1728, zero, 1, 0, 1, 1);
    CHECK(got_count == 1 && near(got[0].x, 1001 - 1024, 4) && near(got[0].y, 709 - 880, 4));

    // Eye-tracked gaze: Valve's offset (gaze NDC * half the framebuffer, from the swapchain centre) is re-based on
    // the eye's rect: gaze right by a quarter of the framebuffer = a quarter of the rect from the rect's centre
    VkOffset2D gaze[2] = {{504, 0}, {-504, 432}};
    pass(2016, 1728, gaze, 2);
    CHECK(near(got[0].x, 863 + 504 * 1726.0f / 2016 - 1024, 4) && near(got[0].y, 864 - 880, 4));
    CHECK(near(got[1].x, 287 + 863 - 504 * 1726.0f / 2016 - 1024, 4) && near(got[1].y, 864 + 432 - 880, 4));

    // Valve computes both eyes' gaze offsets but declares count 1 (seen on the Frame): each eye uses its own
    pass(2016, 1728, gaze, 1);
    CHECK(got_count == 2 && near(got[1].x, 287 + 863 - 504 * 1726.0f / 2016 - 1024, 4));

    // In Death (Unreal, #394/#520): the eye pass renders only the rect (render area 1730 of a 2768 framebuffer) and
    // Valve scales its gaze by that render area: its straight-ahead gaze 128,-176 is the left eye's optical centre
    // 996,689 (1730/2 + 128, 1730/2 - 176), not 945,755 as with the framebuffer's scale
    fp_fdm_view id_eyes[2] = {{2768, 2768, 0, 0, 0, 1730, 1730, 996, 689}, {2768, 2768, 1, 0, 0, 1730, 1730, 725, 702}};
    fp_fdm_set_views(id_eyes, 2);
    render_area = (VkExtent2D){1730, 1730};
    VkOffset2D ahead[2] = {{128, -176}, {-136, -160}};
    pass(2768, 2768, ahead, 1);
    CHECK(got_count == 2);
    CHECK(near(got[0].x, 993 - c_valve_centre(2768, 87, 0), 8) && near(got[0].y, 689 - c_valve_centre(2768, 87, 1), 8));
    CHECK(near(got[1].x, 729 - c_valve_centre(2768, 87, 0), 8) && near(got[1].y, 705 - c_valve_centre(2768, 87, 1), 8));
    render_area = (VkExtent2D){0, 0};
    fp_fdm_set_views(views, 2);

    // Fixed (Valve's offsets on underneath): its gaze is ignored, the optical centre wins
    c_fixed = 1;
    pass(2016, 1728, gaze, 2);
    CHECK(got_count == 2 && near(got[0].x, -22, 4) && near(got[1].y, -171, 4));

    // A map without the offset flag (Valve's offsets off, e.g. it read FDM_DEBUG before this layer changed it): left
    // alone, nothing added
    pass_ex(2016, 1728, NULL, 0, 3, 0, 0);
    CHECK(got_count == 0);

    // A pass of another size (no reported view): Valve's offsets go through unchanged
    pass(1024, 1024, gaze, 2);
    CHECK(got_count == 2 && got[0].x == 504 && got[1].y == 432);

    // Both eyes side by side in one layer: one map can't serve both, left alone
    fp_fdm_view wide[2] = {{3456, 1728, 0, 0, 0, 1728, 1728, 1000, 700}, {3456, 1728, 0, 1728, 0, 1728, 1728, 2450, 700}};
    fp_fdm_set_views(wide, 2);
    pass(3456, 1728, gaze, 2);
    CHECK(got_count == 2 && got[0].x == 504);

    // Off (FRAMEPORT_FDM_CENTRE=0): nothing changes
    fp_fdm_set_views(views, 2);
    c_disabled = 1;
    pass(2016, 1728, gaze, 2);
    CHECK(got_count == 2 && got[0].x == 504 && got[1].x == -504);
    c_disabled = 0;

    // Fixed keeps Valve's offsets on: disable_offsets is dropped from FDM_DEBUG, other flags stay
    setenv("FDM_DEBUG", "enable,disable_offsets,debug", 1);
    c_fixed = 1;
    c_fixed_with_offsets();
    CHECK(!strcmp(getenv("FDM_DEBUG"), "enable,debug"));

    // configuration as Valve reads it: FOVE_LEVEL beats FDM_DEBUG's lo/med/hi
    setenv("FDM_DEBUG", "enable,disable_offsets,med", 1);
    unsetenv("FOVE_LEVEL");
    unsetenv("FRAMEPORT_FDM_CENTRE");
    c_read_config();
    CHECK(c_fixed && c_level == 2 && !c_disabled);
    setenv("FOVE_LEVEL", "hi", 1);
    setenv("FDM_DEBUG", "enable", 1);
    setenv("FRAMEPORT_FDM_CENTRE", "0", 1);
    c_read_config();
    CHECK(!c_fixed && c_level == 3 && c_disabled);
    CHECK(c_has_token("enable,disable_offsets", "disable_offsets") && !c_has_token("enable", "disable_offsets"));
    CHECK(c_has_token("enable,hi", "hi") && !c_has_token("enable,high", "hi"));

    // FRAMEPORT_FDM_DIAG: SPIR-V outputs. A vertex shader with a standalone Output ViewportIndex variable, one with
    // Layer in an output block, and a fragment shader reading Layer as an input (not counted).
    const uint32_t vs_vp[] = {0x07230203, 0x10000, 0, 20, 0,
                              (4u << 16) | 15, 0, 1, 0,          // OpEntryPoint Vertex %1 "" (name truncated)
                              (4u << 16) | 71, 5, 11, 10,        // OpDecorate %5 BuiltIn ViewportIndex
                              (4u << 16) | 32, 4, 3, 2,          // %4 = OpTypePointer Output %2
                              (4u << 16) | 59, 4, 5, 3};         // %5 = OpVariable %4 Output
    CHECK(c_spirv_outputs(vs_vp, sizeof(vs_vp) / 4) == (1u | 1u << 8));
    const uint32_t vs_block[] = {0x07230203, 0x10000, 0, 20, 0,
                                 (5u << 16) | 72, 7, 1, 11, 9,   // OpMemberDecorate %7 1 BuiltIn Layer
                                 (4u << 16) | 32, 8, 3, 7,       // %8 = OpTypePointer Output %7
                                 (4u << 16) | 59, 8, 9, 3};      // %9 = OpVariable %8 Output
    CHECK(c_spirv_outputs(vs_block, sizeof(vs_block) / 4) == 2u);
    const uint32_t fs_in[] = {0x07230203, 0x10000, 0, 20, 0,
                              (4u << 16) | 15, 4, 1, 0,          // OpEntryPoint Fragment
                              (4u << 16) | 71, 5, 11, 9,         // OpDecorate %5 BuiltIn Layer
                              (4u << 16) | 59, 4, 5, 1};         // %5 = OpVariable %4 Input
    CHECK(c_spirv_outputs(fs_in, sizeof(fs_in) / 4) == 1u << 12);
    CHECK(c_spirv_outputs(NULL, 0) == 0);

    // FRAMEPORT_FDM_DIAG: a pipeline made for a render pass without a density map, bound inside a foveated pass,
    // makes Turnip share one viewport scale; one made for the foveated pass doesn't
    c_diag = 1, c_fixed = 0, c_level = 1, c_disabled = 0;
    fp_fdm_set_views(views, 2);
    pass(2016, 1728, zero, 2);  // creates a foveated render pass (last_rp)
    VkPipelineShaderStageCreateInfo stages[2] = {{VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO},
                                                 {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO}};
    stages[0].stage = VK_SHADER_STAGE_VERTEX_BIT, stages[1].stage = VK_SHADER_STAGE_FRAGMENT_BIT;
    VkGraphicsPipelineCreateInfo gci = {VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO};
    gci.stageCount = 2, gci.pStages = stages, gci.renderPass = (VkRenderPass)0x7777;  // no density map
    VkPipeline plain, foveated;
    CHECK(c_CreateGraphicsPipelines((VkDevice)&device_obj, VK_NULL_HANDLE, 1, &gci, NULL, &plain) == VK_SUCCESS);
    CHECK(strstr(last_log, "NO density map"));
    gci.renderPass = last_rp;
    snprintf(last_log, sizeof(last_log), "-");
    CHECK(c_CreateGraphicsPipelines((VkDevice)&device_obj, VK_NULL_HANDLE, 1, &gci, NULL, &foveated) == VK_SUCCESS);
    CHECK(!strcmp(last_log, "-"));  // not logged: nothing wrong with it
    bind_in_pass = foveated;
    pass(2016, 1728, zero, 2);
    CHECK(strstr(last_log, "per-view scale") && strstr(last_log, "1 pipeline bind(s)") && binds_seen == 1);
    bind_in_pass = plain;
    pass(2016, 1728, zero, 2);
    CHECK(strstr(last_log, "SHARED VIEWPORT") && binds_seen == 2);
    CHECK(strstr(last_log, "pipelines created 2, without density map 1, writing ViewportIndex 0"));
    CHECK(strstr(last_log, "handed down 2: v0 -24,-184"));
    bind_in_pass = VK_NULL_HANDLE;
    c_diag = 0;

    // Turnip's bins (tu_bins.h, Adreno 750). In Death's eye pass: 4x RGBA8 + 4x D24S8, two views, 2768x2768.
    // Measured on the Frame (snapshots, 2026-10-25): 192x224 bins.
    tb_att idatts[3];
    CHECK(tb_att_of(VK_FORMAT_R8G8B8A8_SRGB, 4, &idatts[0]) && tb_att_of(VK_FORMAT_D24_UNORM_S8_UINT, 4, &idatts[1]));
    CHECK(!tb_att_of(VK_FORMAT_ASTC_4x4_UNORM_BLOCK, 1, &idatts[2]));  // not modelled
    tb_att_of(VK_FORMAT_R8G8_UNORM, 1, &idatts[2]);
    idatts[0].gmem = idatts[1].gmem = 1;  // the density map isn't in GMEM
    tb_pass tp;
    CHECK(tb_pass_config(&TB_ADRENO_750, idatts, 3, &tp) && tp.gmem_pixels == 86016 && tp.tile_align_w == 96);
    uint32_t tw = 0, th = 0, cw, ch;
    tb_lrz_constraint(&TB_ADRENO_750, 2768, 2768, 4, 2, &cw, &ch);  // 4x: LRZ fast clear can't cover it anyway
    CHECK(cw == 2016 && ch == 2032);
    CHECK(tb_tile_size(&TB_ADRENO_750, &tp, 2768, 2768, 2, cw, ch, &tw, &th) && tw == 192 && th == 224);
    // a 1x depth image of that size does get a limit (a multiple of the GMEM alignment, at least 192)
    tb_lrz_constraint(&TB_ADRENO_750, 2768, 2768, 1, 2, &cw, &ch);
    CHECK(cw < 2016 && cw >= 192 && cw % 16 == 0 && ch % 4 == 0);
    // the bins whose centre lies in Valve's sharp texels 1024-1824 (lo preset, 87-texel map): 960-1728 / 1120-1792
    CHECK(tb_bins_centre(1024, 1824, 192) == 1344.0f && tb_bins_centre(1024, 1824, 224) == 1456.0f);
    CHECK(tb_bins_centre(1024, 1100, 400) < 0);  // no bin centre inside: undecided
    CHECK(c_sharp_centre(2768, 87, 0, 192) == 1344.0f && c_sharp_centre(2768, 87, 0, 0) == 1424.0f);

    // End to end: with the GPU known, the layer computes 192x224 for that pass and centres those bins on each eye
    fp_fdm_view indeath[2] = {{2768, 2768, 0, 0, 0, 1730, 1730, 1004, 697}, {2768, 2768, 1, 0, 0, 1730, 1730, 715, 709}};
    fp_fdm_set_views(indeath, 2);
    c_fixed = 1, c_level = 1;
    eye_pass(2768, 2768, VK_FORMAT_R8G8B8A8_SRGB, VK_SAMPLE_COUNT_4_BIT, zero, 2);
    CHECK(got_count == 2 && near(got[0].x, 1004 - 1424, 4) && near(got[0].y, 697 - 1424, 4));  // GPU unknown: texels
    c_gpu = &TB_ADRENO_750;
    eye_pass(2768, 2768, VK_FORMAT_R8G8B8A8_SRGB, VK_SAMPLE_COUNT_4_BIT, zero, 2);
    CHECK(got_count == 2 && near(got[0].x, 1004 - 1344, 4) && near(got[0].y, 697 - 1456, 4));
    CHECK(near(got[1].x, 715 - 1344, 4) && near(got[1].y, 709 - 1456, 4));
    // a colour format tu_bins.h doesn't model: undecided, the texels' centre as before
    eye_pass(2768, 2768, VK_FORMAT_ASTC_4x4_UNORM_BLOCK, VK_SAMPLE_COUNT_4_BIT, zero, 2);
    CHECK(near(got[0].x, 1004 - 1424, 4) && near(got[0].y, 697 - 1424, 4));
    // fdm_bin_w=256, fdm_bin_h=256 (FrameBridge -> fp_fdm_set_bin) wins over the computed size: bins 1024-1792 -> centre 1408
    fp_fdm_set_bin(256, 256);
    eye_pass(2768, 2768, VK_FORMAT_R8G8B8A8_SRGB, VK_SAMPLE_COUNT_4_BIT, zero, 2);
    CHECK(near(got[0].x, 1004 - 1408, 4) && near(got[0].y, 697 - 1408, 4));
    fp_fdm_set_bin(0, 0);
    c_gpu = NULL;
    puts("ok");
    return 0;
}
