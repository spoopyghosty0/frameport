// SPDX-License-Identifier: GPL-3.0-only
// Import linear decoder/Android DMA buffers. Ownership returns to the foreign
// producer/consumer before the completed fence permits either buffer to move.
#pragma once
#include <vulkan/vulkan.h>
#include <unistd.h>
#include <sys/stat.h>
#include <cstdio>
#include <cstring>
#include <vector>
class FramePortDmaCopy {
    VkInstance instance=VK_NULL_HANDLE;
    VkPhysicalDevice physical=VK_NULL_HANDLE;
    VkDevice device=VK_NULL_HANDLE;
    VkQueue queue=VK_NULL_HANDLE;
    VkCommandPool pool=VK_NULL_HANDLE;
    VkCommandBuffer command=VK_NULL_HANDLE;
    VkFence completed=VK_NULL_HANDLE;
    uint32_t family=0;
    PFN_vkGetMemoryFdPropertiesKHR properties=nullptr;
    struct Buffer {VkBuffer buffer=VK_NULL_HANDLE;VkDeviceMemory memory=VK_NULL_HANDLE;};
    struct Entry {dev_t device;ino_t inode;VkBufferUsageFlags usage;Buffer imported;};
    std::vector<Entry> cache;
    bool attempted=false,ready=false,unsafe=false;
    template<class T>static T info(VkStructureType type){T value={};value.sType=type;return value;}
    bool check(VkResult r,const char *where){if(r!=VK_SUCCESS)ALOGW("GPU video %s: %d",where,r);return r==VK_SUCCESS;}
    bool import(int fd,VkBufferUsageFlags usage,Buffer &out) {
        struct stat st={};if(fstat(fd,&st)||st.st_size<=0)return false;
        auto query=info<VkPhysicalDeviceExternalBufferInfo>(VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_BUFFER_INFO);
        query.usage=usage;query.handleType=VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
        auto caps=info<VkExternalBufferProperties>(VK_STRUCTURE_TYPE_EXTERNAL_BUFFER_PROPERTIES);
        vkGetPhysicalDeviceExternalBufferProperties(physical,&query,&caps);
        if(!(caps.externalMemoryProperties.externalMemoryFeatures&VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT))return false;
        auto external=info<VkExternalMemoryBufferCreateInfo>(VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO);
        external.handleTypes=query.handleType;
        auto create=info<VkBufferCreateInfo>(VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO);create.pNext=&external;
        create.size=st.st_size;create.usage=usage;create.sharingMode=VK_SHARING_MODE_EXCLUSIVE;
        if(!check(vkCreateBuffer(device,&create,nullptr,&out.buffer),"create buffer"))return false;
        VkMemoryRequirements needs;vkGetBufferMemoryRequirements(device,out.buffer,&needs);
        auto fdprops=info<VkMemoryFdPropertiesKHR>(VK_STRUCTURE_TYPE_MEMORY_FD_PROPERTIES_KHR);
        if(!check(properties(device,query.handleType,fd,&fdprops),"fd properties"))return false;
        uint32_t bits=needs.memoryTypeBits&fdprops.memoryTypeBits;
        if(!bits || needs.size>(VkDeviceSize)st.st_size){ALOGW("GPU import layout mismatch: size %llu/%lld bits %#x",
            (unsigned long long)needs.size,(long long)st.st_size,bits);return false;}
        auto imported=info<VkImportMemoryFdInfoKHR>(VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR);
        imported.handleType=query.handleType;imported.fd=dup(fd);if(imported.fd<0)return false;
        auto dedicated=info<VkMemoryDedicatedAllocateInfo>(VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO);
        dedicated.buffer=out.buffer;
        if(caps.externalMemoryProperties.externalMemoryFeatures&VK_EXTERNAL_MEMORY_FEATURE_DEDICATED_ONLY_BIT)imported.pNext=&dedicated;
        auto allocate=info<VkMemoryAllocateInfo>(VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO);allocate.pNext=&imported;
        allocate.allocationSize=st.st_size;allocate.memoryTypeIndex=__builtin_ctz(bits);
        if(!check(vkAllocateMemory(device,&allocate,nullptr,&out.memory),"import memory")){close(imported.fd);return false;}
        return check(vkBindBufferMemory(device,out.buffer,out.memory,0),"bind buffer");
    }
    void release(Buffer &b){if(b.buffer)vkDestroyBuffer(device,b.buffer,nullptr);if(b.memory)vkFreeMemory(device,b.memory,nullptr);b={};}
    bool cached(int fd,VkBufferUsageFlags usage,Buffer &out){
        struct stat st={};if(fstat(fd,&st))return false;
        for(auto &e:cache)if(e.device==st.st_dev&&e.inode==st.st_ino&&e.usage==usage){out=e.imported;return true;}
        Buffer b;bool ok=import(fd,usage,b);if(!ok){release(b);return false;}
        cache.push_back({st.st_dev,st.st_ino,usage,b});out=b;return true;
    }
public:
    void clearBuffers(){if(device){vkDeviceWaitIdle(device);for(auto &e:cache)release(e.imported);cache.clear();}}
    bool available(){if(!attempted){attempted=true;ready=init();}return ready;}
    bool failedAfterSubmit()const{return unsafe;}
    bool init(){
        auto app=info<VkApplicationInfo>(VK_STRUCTURE_TYPE_APPLICATION_INFO);app.apiVersion=VK_API_VERSION_1_1;
        auto create=info<VkInstanceCreateInfo>(VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO);create.pApplicationInfo=&app;
        if(!check(vkCreateInstance(&create,nullptr,&instance),"create instance"))return false;
        uint32_t n=0;vkEnumeratePhysicalDevices(instance,&n,nullptr);if(!n)return false;
        std::vector<VkPhysicalDevice> devices(n);vkEnumeratePhysicalDevices(instance,&n,devices.data());physical=devices[0];
        VkPhysicalDeviceProperties p;vkGetPhysicalDeviceProperties(physical,&p);
        if(p.apiVersion<VK_API_VERSION_1_1)return false;
        vkEnumerateDeviceExtensionProperties(physical,nullptr,&n,nullptr);std::vector<VkExtensionProperties> extensions(n);
        vkEnumerateDeviceExtensionProperties(physical,nullptr,&n,extensions.data());
        const char *required[]={VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME,VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME,VK_EXT_QUEUE_FAMILY_FOREIGN_EXTENSION_NAME};
        for(auto wanted:required){bool found=false;for(auto &e:extensions)if(!strcmp(e.extensionName,wanted))found=true;
            if(!found)return false;}
        vkGetPhysicalDeviceQueueFamilyProperties(physical,&n,nullptr);std::vector<VkQueueFamilyProperties> families(n);
        vkGetPhysicalDeviceQueueFamilyProperties(physical,&n,families.data());
        family=n;for(uint32_t i=0;i<n;i++)if(families[i].queueFlags&VK_QUEUE_TRANSFER_BIT){family=i;break;}
        if(family==n)return false;
        float priority=1;auto q=info<VkDeviceQueueCreateInfo>(VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO);
        q.queueFamilyIndex=family;q.queueCount=1;q.pQueuePriorities=&priority;
        auto d=info<VkDeviceCreateInfo>(VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO);d.queueCreateInfoCount=1;d.pQueueCreateInfos=&q;
        d.enabledExtensionCount=3;d.ppEnabledExtensionNames=required;
        if(!check(vkCreateDevice(physical,&d,nullptr,&device),"create device"))return false;
        vkGetDeviceQueue(device,family,0,&queue);
        properties=(PFN_vkGetMemoryFdPropertiesKHR)vkGetDeviceProcAddr(device,"vkGetMemoryFdPropertiesKHR");if(!properties)return false;
        auto cp=info<VkCommandPoolCreateInfo>(VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO);cp.queueFamilyIndex=family;
        cp.flags=VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
        if(!check(vkCreateCommandPool(device,&cp,nullptr,&pool),"create pool"))return false;
        auto a=info<VkCommandBufferAllocateInfo>(VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO);
        a.commandPool=pool;a.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY;a.commandBufferCount=1;
        auto f=info<VkFenceCreateInfo>(VK_STRUCTURE_TYPE_FENCE_CREATE_INFO);
        return check(vkAllocateCommandBuffers(device,&a,&command),"allocate command buffer")&&
            check(vkCreateFence(device,&f,nullptr,&completed),"create fence");
    }
    bool copy(int source,int dest,const VkBufferCopy *regions,unsigned count){
        unsafe=false;
        struct stat s={},d={};
        if(!count||fstat(source,&s)||fstat(dest,&d))return false;
        for(unsigned i=0;i<count;i++)if(!regions[i].size||(regions[i].size&3)||
            (regions[i].srcOffset&3)||(regions[i].dstOffset&3)||regions[i].srcOffset>(VkDeviceSize)s.st_size||
            regions[i].dstOffset>(VkDeviceSize)d.st_size||regions[i].size>(VkDeviceSize)s.st_size-regions[i].srcOffset||
            regions[i].size>(VkDeviceSize)d.st_size-regions[i].dstOffset)return false;
        // Make room for the pair before resolving either handle. Evicting
        // while importing the destination could destroy the source handle.
        if(cache.size()>30)clearBuffers();
        Buffer buffers[2];bool ok=cached(source,VK_BUFFER_USAGE_TRANSFER_SRC_BIT,buffers[0])&&
            cached(dest,VK_BUFFER_USAGE_TRANSFER_DST_BIT,buffers[1]);
        VkCommandBuffer cmd=command;VkFence fence=completed;
        if(ok)ok=check(vkResetCommandBuffer(cmd,0),"reset command buffer")&&check(vkResetFences(device,1,&fence),"reset fence");
        if(ok){
            auto b=info<VkCommandBufferBeginInfo>(VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO);b.flags=VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
            ok=check(vkBeginCommandBuffer(cmd,&b),"begin command buffer");
            VkBufferMemoryBarrier barriers[2]={};
            for(unsigned i=0;i<2;i++){
                barriers[i].sType=VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;barriers[i].dstAccessMask=i?VK_ACCESS_TRANSFER_WRITE_BIT:VK_ACCESS_TRANSFER_READ_BIT;
                barriers[i].srcQueueFamilyIndex=VK_QUEUE_FAMILY_FOREIGN_EXT;barriers[i].dstQueueFamilyIndex=family;
                barriers[i].buffer=buffers[i].buffer;barriers[i].size=VK_WHOLE_SIZE;
            }
            vkCmdPipelineBarrier(cmd,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,0,nullptr,2,barriers,0,nullptr);
            vkCmdCopyBuffer(cmd,buffers[0].buffer,buffers[1].buffer,count,regions);
            for(auto &barrier:barriers){barrier.srcAccessMask=barrier.dstAccessMask;barrier.dstAccessMask=0;
                barrier.srcQueueFamilyIndex=family;barrier.dstQueueFamilyIndex=VK_QUEUE_FAMILY_FOREIGN_EXT;}
            vkCmdPipelineBarrier(cmd,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,0,0,nullptr,2,barriers,0,nullptr);
            ok=ok&&check(vkEndCommandBuffer(cmd),"end command buffer");
        }
        if(ok){auto s=info<VkSubmitInfo>(VK_STRUCTURE_TYPE_SUBMIT_INFO);s.commandBufferCount=1;s.pCommandBuffers=&cmd;
            ok=check(vkQueueSubmit(queue,1,&s,fence),"submit");
            if(ok){ok=check(vkWaitForFences(device,1,&fence,VK_TRUE,10000000000ull),"wait fence");unsafe=!ok;}}
        if(!ok&&device)vkDeviceWaitIdle(device);
        return ok;
    }
    ~FramePortDmaCopy(){if(device){vkDeviceWaitIdle(device);for(auto &e:cache)release(e.imported);
        if(completed)vkDestroyFence(device,completed,nullptr);if(pool)vkDestroyCommandPool(device,pool,nullptr);
        vkDestroyDevice(device,nullptr);}if(instance)vkDestroyInstance(instance,nullptr);}
};
