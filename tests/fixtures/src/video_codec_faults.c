// SPDX-License-Identifier: GPL-3.0-only
// Test-only fault injection: deny Iris opens AFTER plugin enumeration, to
// exercise software fallback without altering the host's decoder or devices.
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <string.h>
#include <sys/ioctl.h>
#include <linux/videodev2.h>
static int denied;
static int corrupt_after=-1;
static int deny_restart;
void frameport_probe_deny_device(void) { denied=1; }
void frameport_probe_corrupt_picture(void) { corrupt_after=30; }
void frameport_probe_deny_restart(void) { deny_restart=1; }
int ioctl(int fd,int request,...) {
    static int (*original)(int,int,...);
    if(!original) *(void**)&original=dlsym(RTLD_NEXT,"ioctl");
    va_list args;va_start(args,request);void *data=va_arg(args,void*);va_end(args);
    if(deny_restart && (unsigned)request==VIDIOC_DECODER_CMD &&
       ((struct v4l2_decoder_cmd*)data)->cmd==V4L2_DEC_CMD_START){errno=EIO;return -1;}
    int result=original(fd,request,data);
    if(result==0 && (unsigned)request==VIDIOC_DQBUF && corrupt_after>=0){
        struct v4l2_buffer *buffer=data;
        if(buffer->type==V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE && buffer->m.planes[0].bytesused){
            if(!corrupt_after--){buffer->flags|=V4L2_BUF_FLAG_ERROR;corrupt_after=-1;}
        }
    }
    return result;
}
int open(const char *path,int flags,...) {
    static int (*original)(const char*,int,...);
    if(!original) *(void**)&original=dlsym(RTLD_NEXT,"open");
    if(denied && !strncmp(path,"/dev/video",10)){errno=ENODEV;return -1;}
    mode_t mode=0;
    if(flags&O_CREAT){va_list args;va_start(args,flags);mode=va_arg(args,int);va_end(args);}
    return original(path,flags,mode);
}
int open64(const char *path,int flags,...) {
    mode_t mode=0;
    if(flags&O_CREAT){va_list args;va_start(args,flags);mode=va_arg(args,int);va_end(args);}
    return open(path,flags,mode);
}
#include <vulkan/vulkan.h>
static unsigned gpu_submissions;
static int deny_gpu;
static int deny_gpu_import;
static void *vulkan_symbol(const char *name) {
    // The plugin is RTLD_LOCAL in the OMX probe, so RTLD_NEXT cannot see its
    // Vulkan dependency. Resolve the actual library, not this preload shim.
    static void *library;
    if(!library)library=dlopen("libvulkan.so",RTLD_NOW|RTLD_LOCAL);
    return library?dlsym(library,name):0;
}
void frameport_probe_deny_gpu(void) { deny_gpu=1; }
void frameport_probe_deny_gpu_import(void) { deny_gpu_import=1; }
unsigned frameport_probe_gpu_submissions(void) { return __atomic_load_n(&gpu_submissions,__ATOMIC_RELAXED); }
VKAPI_ATTR VkResult VKAPI_CALL vkCreateInstance(const VkInstanceCreateInfo *info,const VkAllocationCallbacks *allocator,VkInstance *instance) {
    if(deny_gpu)return VK_ERROR_FEATURE_NOT_PRESENT;
    PFN_vkCreateInstance real=(PFN_vkCreateInstance)vulkan_symbol("vkCreateInstance");
    if(!real)return VK_ERROR_INITIALIZATION_FAILED;
    return real(info,allocator,instance);
}
VKAPI_ATTR VkResult VKAPI_CALL vkQueueSubmit(VkQueue queue,uint32_t count,const VkSubmitInfo *submits,VkFence fence) {
    PFN_vkQueueSubmit real=(PFN_vkQueueSubmit)vulkan_symbol("vkQueueSubmit");
    if(!real)return VK_ERROR_INITIALIZATION_FAILED;
    VkResult result=real(queue,count,submits,fence);
    if(result==VK_SUCCESS)__atomic_add_fetch(&gpu_submissions,1,__ATOMIC_RELAXED);
    return result;
}
VKAPI_ATTR VkResult VKAPI_CALL vkAllocateMemory(VkDevice device,const VkMemoryAllocateInfo *info,const VkAllocationCallbacks *allocator,VkDeviceMemory *memory) {
    if(deny_gpu_import)return VK_ERROR_OUT_OF_DEVICE_MEMORY;
    PFN_vkAllocateMemory real=(PFN_vkAllocateMemory)vulkan_symbol("vkAllocateMemory");
    return real?real(device,info,allocator,memory):VK_ERROR_INITIALIZATION_FAILED;
}
