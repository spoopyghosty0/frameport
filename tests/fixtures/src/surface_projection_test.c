#define XR_USE_GRAPHICS_API_VULKAN
#include <vulkan/vulkan.h>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <assert.h>
#include "layer_math.h"
#include "surface_views.h"
#include "surface_video.h"
#define SURF_MAX 8
#define MAX_IMAGES 8
#define LOG(...) do {printf(__VA_ARGS__);puts("");} while(0)
#define VK_OK(call) assert((call)==VK_SUCCESS)
static struct {VkInstance instance;VkPhysicalDevice physical;VkDevice device;VkQueue queue;uint32_t family;
    PFN_vkGetDeviceProcAddr GetDeviceProcAddr;PFN_vkWaitForFences WaitForFences;PFN_vkResetFences ResetFences;
    PFN_vkResetCommandBuffer ResetCommandBuffer;PFN_vkBeginCommandBuffer BeginCommandBuffer;PFN_vkEndCommandBuffer EndCommandBuffer;
    PFN_vkQueueSubmit QueueSubmit;PFN_vkCmdPipelineBarrier CmdPipelineBarrier;} vk;
static struct {PFN_vkAllocateMemory AllocateMemory;PFN_vkGetPhysicalDeviceMemoryProperties GetPhysicalDeviceMemoryProperties;
    PFN_vkGetFenceStatus GetFenceStatus;PFN_vkCmdCopyBufferToImage CmdCopyBufferToImage;} svk;
// The CPU-delivery fixture also compiles the shared-image branch; it stays off.
#define SURF_NATIVE_SLOTS 4
typedef struct {VkImage image;VkImageView view;uint32_t width,height;int state;uint64_t seq;uint32_t source_width,source_height;surf_video_job video;int projected;} surf_native_slot;
typedef struct {XrSwapchain handle;uint32_t width,height;int in_flight,uploads,native_mode,native_pending,native_current,native_project;surf_video_job video_job;uint64_t seq;
    surf_native_slot native_slots[SURF_NATIVE_SLOTS];
    VkBuffer staging;void *staging_map;unsigned char *pixels;VkCommandBuffer cmd;VkFence fence;} surf_swapchain;
static surf_swapchain surfs[SURF_MAX];static pthread_mutex_t surf_lock=PTHREAD_MUTEX_INITIALIZER;
static int equirect_res=1536,equirect_res_set;
static void surf_native_complete(surf_swapchain *s){(void)s;}
static void surf_native_retire(surf_swapchain *s){(void)s;}
static void surf_native_ownership(VkCommandBuffer cmd,surf_native_slot *slot,int acquire){(void)cmd;(void)slot;(void)acquire;assert(0);}
static XrInstance active_instance;static int surf_vulkan=1;static VkImage destination;static VkDeviceMemory destination_memory;
static uint32_t memory_type(uint32_t mask,VkMemoryPropertyFlags flags) {
    VkPhysicalDeviceMemoryProperties props;vkGetPhysicalDeviceMemoryProperties(vk.physical,&props);
    for(uint32_t i=0;i<props.memoryTypeCount;i++)if((mask&(1u<<i)) && (props.memoryTypes[i].propertyFlags&flags)==flags)return i;
    assert(0);return 0;
}
static XrResult fake_create(XrSession session,const XrSwapchainCreateInfo *ci,XrSwapchain *out) {
    (void)session;
    VkImageCreateInfo image={VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,NULL,0,VK_IMAGE_TYPE_2D,(VkFormat)ci->format,
        {ci->width,ci->height,1},1,1,VK_SAMPLE_COUNT_1_BIT,VK_IMAGE_TILING_OPTIMAL,
        VK_IMAGE_USAGE_TRANSFER_DST_BIT|VK_IMAGE_USAGE_TRANSFER_SRC_BIT|VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT,VK_SHARING_MODE_EXCLUSIVE,0,NULL,0};
    VK_OK(vkCreateImage(vk.device,&image,NULL,&destination));VkMemoryRequirements req;vkGetImageMemoryRequirements(vk.device,destination,&req);
    VkMemoryAllocateInfo alloc={VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,NULL,req.size,memory_type(req.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)};
    VK_OK(vkAllocateMemory(vk.device,&alloc,NULL,&destination_memory));VK_OK(vkBindImageMemory(vk.device,destination,destination_memory,0));
    assert(ci->format==VK_FORMAT_R8G8B8A8_SRGB);
    assert(!(ci->usageFlags&XR_SWAPCHAIN_USAGE_UNORDERED_ACCESS_BIT));
    *out=(XrSwapchain)(uintptr_t)2;return XR_SUCCESS;
}
static XrResult fake_enumerate(XrSwapchain sc,uint32_t cap,uint32_t *count,XrSwapchainImageBaseHeader *images) {
    (void)sc;(void)cap;*count=1;((XrSwapchainImageVulkanKHR*)images)->image=destination;return XR_SUCCESS;
}
static XrResult fake_destroy(XrSwapchain sc){(void)sc;vkDestroyImage(vk.device,destination,NULL);vkFreeMemory(vk.device,destination_memory,NULL);return XR_SUCCESS;}
static XrResult fake_acquire(XrSwapchain sc,const XrSwapchainImageAcquireInfo *info,uint32_t *index){(void)sc;(void)info;*index=0;return XR_SUCCESS;}
static XrResult fake_wait(XrSwapchain sc,const XrSwapchainImageWaitInfo *info){(void)sc;(void)info;return XR_SUCCESS;}
static XrResult fake_release(XrSwapchain sc,const XrSwapchainImageReleaseInfo *info){(void)sc;(void)info;return XR_SUCCESS;}
static XrResult fake_locate(XrSession session,const XrViewLocateInfo *info,XrViewState *state,uint32_t cap,uint32_t *count,XrView *views) {
    (void)session;(void)info;(void)cap;*count=2;state->viewStateFlags=XR_VIEW_STATE_ORIENTATION_VALID_BIT|XR_VIEW_STATE_POSITION_VALID_BIT;
    for(int i=0;i<2;i++){views[i].pose=(XrPosef){{0,0,0,1},{0,0,0}};views[i].fov=(XrFovf){-.6,.6,.6,-.6};}return XR_SUCCESS;
}
static PFN_xrVoidFunction lookup(XrInstance instance,const char *name) {
    (void)instance;
#define FN(n,f) if(!strcmp(name,n))return (PFN_xrVoidFunction)f;
    FN("xrCreateSwapchain",fake_create) FN("xrEnumerateSwapchainImages",fake_enumerate) FN("xrDestroySwapchain",fake_destroy)
    FN("xrAcquireSwapchainImage",fake_acquire) FN("xrWaitSwapchainImage",fake_wait) FN("xrReleaseSwapchainImage",fake_release) FN("xrLocateViews",fake_locate)
    return NULL;
}
static surf_swapchain *surf_find(XrSwapchain handle){return handle==surfs[0].handle?surfs:NULL;}
static void surf_barrier(VkCommandBuffer cmd,VkImage image,VkImageLayout from,VkImageLayout to,VkAccessFlags src,VkAccessFlags dst) {
    VkImageMemoryBarrier barrier={VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,NULL,src,dst,from,to,VK_QUEUE_FAMILY_IGNORED,VK_QUEUE_FAMILY_IGNORED,image,{VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1}};
    vkCmdPipelineBarrier(cmd,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,0,0,NULL,0,NULL,1,&barrier);
}
typedef struct {XrSwapchainSubImage sub;XrPosef pose;XrSpace space;XrEyeVisibility eye;} emul_common;
static int emul_is_equirect(const XrCompositionLayerBaseHeader *layer){return layer && layer->type==XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR;}
static emul_common emul_common_of(const XrCompositionLayerBaseHeader *layer) {
    const XrCompositionLayerEquirect2KHR *e=(const void*)layer;return (emul_common){e->subImage,e->pose,e->space,e->eyeVisibility};
}
static int surf_vk_ready(void) {return vk.GetDeviceProcAddr && svk.AllocateMemory;}
static int surf_staging(surf_swapchain *s) {return s->staging_map && s->staging && s->cmd && s->fence;}
#include "surface_projection.c"
int main(void) {
    VkInstanceCreateInfo instance={VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};VK_OK(vkCreateInstance(&instance,NULL,&vk.instance));
    uint32_t count=1;VK_OK(vkEnumeratePhysicalDevices(vk.instance,&count,&vk.physical));
    uint32_t families=0;vkGetPhysicalDeviceQueueFamilyProperties(vk.physical,&families,NULL);
    VkQueueFamilyProperties *properties=calloc(families,sizeof(*properties));vkGetPhysicalDeviceQueueFamilyProperties(vk.physical,&families,properties);
    for(vk.family=0;vk.family<families;vk.family++)if(properties[vk.family].queueFlags&VK_QUEUE_COMPUTE_BIT)break;
    assert(vk.family<families);float priority=1;
    VkDeviceQueueCreateInfo queue={VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,NULL,0,vk.family,1,&priority};
    VkDeviceCreateInfo device={VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,NULL,0,1,&queue};VK_OK(vkCreateDevice(vk.physical,&device,NULL,&vk.device));
    vkGetDeviceQueue(vk.device,vk.family,0,&vk.queue);vk.GetDeviceProcAddr=vkGetDeviceProcAddr;vk.WaitForFences=vkWaitForFences;
    vk.ResetFences=vkResetFences;vk.ResetCommandBuffer=vkResetCommandBuffer;vk.BeginCommandBuffer=vkBeginCommandBuffer;
    vk.EndCommandBuffer=vkEndCommandBuffer;vk.QueueSubmit=vkQueueSubmit;vk.CmdPipelineBarrier=vkCmdPipelineBarrier;svk.AllocateMemory=vkAllocateMemory;
    svk.GetPhysicalDeviceMemoryProperties=vkGetPhysicalDeviceMemoryProperties;svk.GetFenceStatus=vkGetFenceStatus;svk.CmdCopyBufferToImage=vkCmdCopyBufferToImage;
    surf_swapchain *s=surfs;s->handle=(XrSwapchain)(uintptr_t)1;s->width=16;s->height=8;s->seq=1;s->pixels=calloc(16*8,4);
    for(int y=0;y<8;y++)for(int x=0;x<16;x++){s->pixels[(y*16+x)*4+(x<8?0:1)]=255;s->pixels[(y*16+x)*4+3]=255;}
    VkCommandPool pool;VkCommandPoolCreateInfo pci={VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,NULL,VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,vk.family};VK_OK(vkCreateCommandPool(vk.device,&pci,NULL,&pool));
    VkCommandBufferAllocateInfo cai={VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,NULL,pool,VK_COMMAND_BUFFER_LEVEL_PRIMARY,1};VK_OK(vkAllocateCommandBuffers(vk.device,&cai,&s->cmd));
    VkFenceCreateInfo fi={VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};VK_OK(vkCreateFence(vk.device,&fi,NULL,&s->fence));
    VkBufferCreateInfo bi={VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,NULL,0,2*1536*1536*4,VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,VK_SHARING_MODE_EXCLUSIVE};
    VK_OK(vkCreateBuffer(vk.device,&bi,NULL,&s->staging));VkMemoryRequirements req;vkGetBufferMemoryRequirements(vk.device,s->staging,&req);
    VkDeviceMemory buffer_memory;VkMemoryAllocateInfo mai={VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,NULL,req.size,memory_type(req.memoryTypeBits,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)};
    VK_OK(vkAllocateMemory(vk.device,&mai,NULL,&buffer_memory));VK_OK(vkBindBufferMemory(vk.device,s->staging,buffer_memory,0));VK_OK(vkMapMemory(vk.device,buffer_memory,0,VK_WHOLE_SIZE,0,&s->staging_map));
    XrCompositionLayerEquirect2KHR layers[2];const XrCompositionLayerBaseHeader *list[2];
    for(int i=0;i<2;i++){layers[i]=(XrCompositionLayerEquirect2KHR){XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR,NULL,0,(XrSpace)(uintptr_t)1,
        i?XR_EYE_VISIBILITY_RIGHT:XR_EYE_VISIBILITY_LEFT,{s->handle,{{i*8,0},{8,8}},0},{{0,0,0,1},{0,0,0}},0,2*LM_PI,LM_PI/2,-LM_PI/2};list[i]=(const void*)&layers[i];}
    XrFrameEndInfo frame={XR_TYPE_FRAME_END_INFO,NULL,1,XR_ENVIRONMENT_BLEND_MODE_OPAQUE,2,list};
    const XrCompositionLayerBaseHeader *result=surf_projection_frame((XrSession)(uintptr_t)1,&frame,list[0]);assert(result && result->type==XR_TYPE_COMPOSITION_LAYER_PROJECTION);
    VK_OK(vkWaitForFences(vk.device,1,&s->fence,VK_TRUE,UINT64_MAX));
    VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};VK_OK(vkResetCommandBuffer(s->cmd,0));VK_OK(vkBeginCommandBuffer(s->cmd,&begin));
    surf_barrier(s->cmd,destination,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT);
    VkBufferImageCopy copy={0,0,0,{VK_IMAGE_ASPECT_COLOR_BIT,0,0,1},{0,0,0},{3072,1536,1}};vkCmdCopyImageToBuffer(s->cmd,destination,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,s->staging,1,&copy);
    VK_OK(vkEndCommandBuffer(s->cmd));VkSubmitInfo submit={VK_STRUCTURE_TYPE_SUBMIT_INFO,NULL,0,NULL,NULL,1,&s->cmd};VK_OK(vkQueueSubmit(vk.queue,1,&submit,VK_NULL_HANDLE));VK_OK(vkQueueWaitIdle(vk.queue));
    unsigned char *pixels=s->staging_map;
    for(int eye=0;eye<2;eye++)for(int y=128;y<1536;y+=128)for(int x=128;x<1536;x+=128){unsigned char *pixel=pixels+4*(y*3072+eye*1536+x);assert(pixel[eye]==255 && pixel[1-eye]==0 && pixel[3]==255);}
    puts("PASS: production Vulkan projection path renders distinct stereo source rectangles on the headset GPU.");
    surf_projection_destroy(s);vkDestroyFence(vk.device,s->fence,NULL);vkDestroyCommandPool(vk.device,pool,NULL);
    vkUnmapMemory(vk.device,buffer_memory);vkDestroyBuffer(vk.device,s->staging,NULL);vkFreeMemory(vk.device,buffer_memory,NULL);
    vkDestroyDevice(vk.device,NULL);vkDestroyInstance(vk.instance,NULL);free(properties);free(s->pixels);
}
