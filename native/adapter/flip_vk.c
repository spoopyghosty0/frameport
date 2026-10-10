// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c. Emulates XR_FB_composition_layer_image_layout's VERTICAL_FLIP for quad
// layers of Vulkan apps when the runtime lacks the extension (Frame): the app's image is blitted
// upside-down into a runtime-owned shadow swapchain, which is submitted instead.
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>
#define XR_USE_GRAPHICS_API_VULKAN
#define XR_USE_TIMESPEC
#include <time.h>
#include <openxr/openxr_platform.h>

#define XR_TYPE_COMPOSITION_LAYER_IMAGE_LAYOUT_FB_VALUE 1000040000
#define MAX_TRACKED_SWAPCHAINS 128
#define MAX_IMAGES 8

static int flip_emul = 1;             // setting


static struct {
    VkInstance instance;
    VkPhysicalDevice physical;
    VkDevice device;
    uint32_t family, index;
    VkQueue queue;
    VkCommandPool pool;
    VkCommandBuffer cmd;
    VkFence fence;
    int state;  // 0 = not initialized, 1 = ready, -1 = failed
    PFN_vkGetDeviceProcAddr GetDeviceProcAddr;
    PFN_vkGetDeviceQueue GetDeviceQueue;
    PFN_vkCreateCommandPool CreateCommandPool;
    PFN_vkAllocateCommandBuffers AllocateCommandBuffers;
    PFN_vkBeginCommandBuffer BeginCommandBuffer;
    PFN_vkEndCommandBuffer EndCommandBuffer;
    PFN_vkResetCommandBuffer ResetCommandBuffer;
    PFN_vkCmdPipelineBarrier CmdPipelineBarrier;
    PFN_vkCmdBlitImage CmdBlitImage;
    PFN_vkQueueSubmit QueueSubmit;
    PFN_vkCreateFence CreateFence;
    PFN_vkWaitForFences WaitForFences;
    PFN_vkResetFences ResetFences;
} vk;

typedef struct {
    XrSwapchain handle;
    XrSwapchainCreateInfo info;
    VkImage images[MAX_IMAGES];
    uint32_t image_count;
    uint32_t last_acquired;
    uint32_t acquired[MAX_IMAGES], acquired_count, waited_count;
    XrSwapchain shadow;
    XrRect2Di shadow_rect;
    VkImage shadow_images[MAX_IMAGES];
    uint32_t shadow_count;
} tracked_swapchain;

static pthread_mutex_t flip_lock = PTHREAD_MUTEX_INITIALIZER;
static tracked_swapchain tracked[MAX_TRACKED_SWAPCHAINS];

static tracked_swapchain *find_tracked(XrSwapchain handle, int create) {
    for (int i = 0; i < MAX_TRACKED_SWAPCHAINS; ++i)
        if (tracked[i].handle == handle) return &tracked[i];
    if (!create) return NULL;
    for (int i = 0; i < MAX_TRACKED_SWAPCHAINS; ++i)
        if (tracked[i].handle == XR_NULL_HANDLE) {
            memset(&tracked[i], 0, sizeof(tracked[i]));
            tracked[i].handle = handle;
            return &tracked[i];
        }
    return NULL;
}

static void flip_on_create_session(const XrSessionCreateInfo *info) {
    for (const XrBaseInStructure *p = info ? (const XrBaseInStructure *)info->next : NULL; p; p = p->next)
        if (p->type == XR_TYPE_GRAPHICS_BINDING_VULKAN_KHR) {
            const XrGraphicsBindingVulkanKHR *b = (const XrGraphicsBindingVulkanKHR *)p;
            vk.instance = b->instance;
            vk.physical = b->physicalDevice;
            vk.device = b->device;
            vk.family = b->queueFamilyIndex;
            vk.index = b->queueIndex;
            vk.state = 0;
            LOG("flip: Vulkan session (queue family %u index %u)", vk.family, vk.index);
        }
}

static void flip_on_create_swapchain(XrSwapchain handle, const XrSwapchainCreateInfo *info) {
    pthread_mutex_lock(&flip_lock);
    tracked_swapchain *t = find_tracked(handle, 1);
    if (t) { t->info = *info; t->info.next = NULL; }
    pthread_mutex_unlock(&flip_lock);
}

static void flip_on_enumerate_images(XrSwapchain handle, uint32_t count, const XrSwapchainImageBaseHeader *images) {
    if (!images || !count || images->type != XR_TYPE_SWAPCHAIN_IMAGE_VULKAN_KHR) return;
    pthread_mutex_lock(&flip_lock);
    tracked_swapchain *t = find_tracked(handle, 0);
    if (t) {
        const XrSwapchainImageVulkanKHR *v = (const XrSwapchainImageVulkanKHR *)images;
        t->image_count = count < MAX_IMAGES ? count : MAX_IMAGES;
        for (uint32_t i = 0; i < t->image_count; ++i) t->images[i] = v[i].image;
    }
    pthread_mutex_unlock(&flip_lock);
}

static void flip_on_acquire(XrSwapchain handle, uint32_t index) {
    pthread_mutex_lock(&flip_lock);
    tracked_swapchain *t = find_tracked(handle, 0);
    if (t) {
        t->last_acquired = index;
        if(t->acquired_count<MAX_IMAGES)t->acquired[t->acquired_count++]=index;
    }
    pthread_mutex_unlock(&flip_lock);
}

static void flip_on_destroy(XrSwapchain handle) {
    PFN_xrDestroySwapchain destroy = (PFN_xrDestroySwapchain)lookup(active_instance, "xrDestroySwapchain");
    pthread_mutex_lock(&flip_lock);
    tracked_swapchain *t = find_tracked(handle, 0);
    if (t) {
        if (t->shadow && destroy) destroy(t->shadow);
        memset(t, 0, sizeof(*t));
    }
    pthread_mutex_unlock(&flip_lock);
}

static int vk_ready(void) {
    if (vk.state) return vk.state > 0;
    vk.state = -1;
    if (!vk.device) return 0;
    void *lib = dlopen("libvulkan.so", RTLD_NOW | RTLD_LOCAL);
    PFN_vkGetInstanceProcAddr gipa = lib ? (PFN_vkGetInstanceProcAddr)dlsym(lib, "vkGetInstanceProcAddr") : NULL;
    if (!gipa) return 0;
    vk.GetDeviceProcAddr = (PFN_vkGetDeviceProcAddr)gipa(vk.instance, "vkGetDeviceProcAddr");
    if (!vk.GetDeviceProcAddr) return 0;
#define DEV(name) vk.name = (PFN_vk##name)vk.GetDeviceProcAddr(vk.device, "vk" #name); if (!vk.name) return 0;
    DEV(GetDeviceQueue) DEV(CreateCommandPool) DEV(AllocateCommandBuffers) DEV(BeginCommandBuffer)
    DEV(EndCommandBuffer) DEV(ResetCommandBuffer) DEV(CmdPipelineBarrier) DEV(CmdBlitImage) DEV(QueueSubmit)
    DEV(CreateFence) DEV(WaitForFences) DEV(ResetFences)
#undef DEV
    vk.GetDeviceQueue(vk.device, vk.family, vk.index, &vk.queue);
    VkCommandPoolCreateInfo pool = {VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, NULL,
                                    VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, vk.family};
    if (vk.CreateCommandPool(vk.device, &pool, NULL, &vk.pool) != VK_SUCCESS) return 0;
    VkCommandBufferAllocateInfo alloc = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, NULL, vk.pool,
                                         VK_COMMAND_BUFFER_LEVEL_PRIMARY, 1};
    if (vk.AllocateCommandBuffers(vk.device, &alloc, &vk.cmd) != VK_SUCCESS) return 0;
    VkFenceCreateInfo fence = {VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, NULL, 0};
    if (vk.CreateFence(vk.device, &fence, NULL, &vk.fence) != VK_SUCCESS) return 0;
    vk.state = 1;
    LOG("flip: Vulkan blitter ready");
    return 1;
}

static void barrier(VkImage image, uint32_t layer, VkImageLayout from, VkImageLayout to, VkAccessFlags src_access,
                    VkAccessFlags dst_access) {
    VkImageMemoryBarrier b = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER, NULL, src_access, dst_access, from, to,
                              VK_QUEUE_FAMILY_IGNORED, VK_QUEUE_FAMILY_IGNORED, image,
                              {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, layer, 1}};
    vk.CmdPipelineBarrier(vk.cmd, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, 0, 0, NULL,
                          0, NULL, 1, &b);
}

static int wants_vertical_flip(const XrCompositionLayerQuad *q) {
    for (const XrBaseInStructure *n = (const XrBaseInStructure *)q->next; n; n = n->next)
        if (n->type == XR_TYPE_COMPOSITION_LAYER_IMAGE_LAYOUT_FB_VALUE)
            return (((const XrCompositionLayerImageLayoutFB *)n)->flags & XR_COMPOSITION_LAYER_IMAGE_LAYOUT_VERTICAL_FLIP_BIT_FB) != 0;
    return 0;
}

// Returns a replacement quad referencing a flipped copy, or NULL to keep the original.
static const XrCompositionLayerQuad *flip_quad(XrSession session, const XrCompositionLayerQuad *q,
                                               XrCompositionLayerQuad *out) {
    if (!flip_emul || runtime_has_image_layout || !wants_vertical_flip(q)) return NULL;
    if (!vk_ready()) { static int warned; if (!warned++) LOG("flip: needed but Vulkan blitter unavailable (GLES app?)"); return NULL; }
    PFN_xrCreateSwapchain create = (PFN_xrCreateSwapchain)lookup(active_instance, "xrCreateSwapchain");
    PFN_xrEnumerateSwapchainImages enumerate =
        (PFN_xrEnumerateSwapchainImages)lookup(active_instance, "xrEnumerateSwapchainImages");
    PFN_xrAcquireSwapchainImage acquire = (PFN_xrAcquireSwapchainImage)lookup(active_instance, "xrAcquireSwapchainImage");
    PFN_xrWaitSwapchainImage wait = (PFN_xrWaitSwapchainImage)lookup(active_instance, "xrWaitSwapchainImage");
    PFN_xrReleaseSwapchainImage release = (PFN_xrReleaseSwapchainImage)lookup(active_instance, "xrReleaseSwapchainImage");
    if (!create || !enumerate || !acquire || !wait || !release) return NULL;

    pthread_mutex_lock(&flip_lock);
    tracked_swapchain *t = find_tracked(q->subImage.swapchain, 0);
    const XrCompositionLayerQuad *result = NULL;
    const XrRect2Di rect = q->subImage.imageRect;
    if (!t || !t->image_count || t->last_acquired >= t->image_count || rect.extent.width <= 0 || rect.extent.height <= 0) {
        static int skipped;
        if (skipped++ < 10)
            LOG("flip: skipped quad swapchain=%p tracked=%d images=%u last=%u rect=%dx%d", (void *)(uintptr_t)q->subImage.swapchain,
                t != NULL, t ? t->image_count : 0, t ? t->last_acquired : 0, rect.extent.width, rect.extent.height);
        goto done;
    }
    if (t->shadow && (t->shadow_rect.extent.width != rect.extent.width || t->shadow_rect.extent.height != rect.extent.height)) {
        // The app changed the sub-rectangle it shows: rebuild the shadow at the new size.
        PFN_xrDestroySwapchain destroy = (PFN_xrDestroySwapchain)lookup(active_instance, "xrDestroySwapchain");
        forget_swapchain(t->shadow);
        if (destroy) destroy(t->shadow);
        t->shadow = XR_NULL_HANDLE;
    }

    if (!t->shadow) {
        XrSwapchainCreateInfo ci = t->info;
        ci.createFlags = 0;
        ci.usageFlags |= XR_SWAPCHAIN_USAGE_TRANSFER_DST_BIT | XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT;
        ci.width = (uint32_t)rect.extent.width;
        ci.height = (uint32_t)rect.extent.height;
        ci.arraySize = 1;
        ci.mipCount = 1;
        ci.faceCount = 1;
        if (XR_FAILED(create(session, &ci, &t->shadow))) { t->shadow = XR_NULL_HANDLE; goto done; }
        remember_swapchain(t->shadow);
        XrSwapchainImageVulkanKHR images[MAX_IMAGES];
        for (int i = 0; i < MAX_IMAGES; ++i) images[i] = (XrSwapchainImageVulkanKHR){XR_TYPE_SWAPCHAIN_IMAGE_VULKAN_KHR, NULL, VK_NULL_HANDLE};
        uint32_t n = 0;
        if (XR_FAILED(enumerate(t->shadow, MAX_IMAGES, &n, (XrSwapchainImageBaseHeader *)images))) goto done;
        t->shadow_count = n;
        for (uint32_t i = 0; i < n; ++i) t->shadow_images[i] = images[i].image;
        t->shadow_rect = rect;
        LOG("flip: created %dx%d shadow swapchain for upside-down quad", rect.extent.width, rect.extent.height);
    }

    uint32_t index = 0;
    XrSwapchainImageAcquireInfo ai = {XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO, NULL};
    XrSwapchainImageWaitInfo wi = {XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO, NULL, 100000000};
    XrSwapchainImageReleaseInfo ri = {XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO, NULL};
    if (XR_FAILED(acquire(t->shadow, &ai, &index)) || index >= t->shadow_count) goto done;
    if (XR_FAILED(wait(t->shadow, &wi))) { release(t->shadow, &ri); goto done; }

    VkImage src = t->images[t->last_acquired], dst = t->shadow_images[index];
    uint32_t layer = q->subImage.imageArrayIndex;
    VkCommandBufferBeginInfo begin = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, NULL,
                                      VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT, NULL};
    vk.ResetCommandBuffer(vk.cmd, 0);
    vk.BeginCommandBuffer(vk.cmd, &begin);
    barrier(src, layer, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
    barrier(dst, 0, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0, VK_ACCESS_TRANSFER_WRITE_BIT);
    VkImageBlit blit = {
        {VK_IMAGE_ASPECT_COLOR_BIT, 0, layer, 1},
        {{rect.offset.x, rect.offset.y, 0}, {rect.offset.x + rect.extent.width, rect.offset.y + rect.extent.height, 1}},
        {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
        {{0, rect.extent.height, 0}, {rect.extent.width, 0, 1}},  // rows reversed
    };
    vk.CmdBlitImage(vk.cmd, src, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, dst, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1,
                    &blit, VK_FILTER_NEAREST);
    barrier(src, layer, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
            VK_ACCESS_TRANSFER_READ_BIT, VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT);
    barrier(dst, 0, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
            VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT);
    vk.EndCommandBuffer(vk.cmd);
    VkSubmitInfo submit = {VK_STRUCTURE_TYPE_SUBMIT_INFO, NULL, 0, NULL, NULL, 1, &vk.cmd, 0, NULL};
    int ok = vk.QueueSubmit(vk.queue, 1, &submit, vk.fence) == VK_SUCCESS &&
             vk.WaitForFences(vk.device, 1, &vk.fence, VK_TRUE, 100000000ull) == VK_SUCCESS;
    vk.ResetFences(vk.device, 1, &vk.fence);
    release(t->shadow, &ri);
    if (!ok) goto done;

    *out = *q;
    out->subImage.swapchain = t->shadow;
    out->subImage.imageArrayIndex = 0;
    out->subImage.imageRect = (XrRect2Di){{0, 0}, rect.extent};
    result = out;
    static int logged;
    if (!logged++) LOG("flip: presenting vertically flipped quad");
done:
    pthread_mutex_unlock(&flip_lock);
    return result;
}
