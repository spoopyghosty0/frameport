// SPDX-License-Identifier: GPL-3.0-only
// Included after surface_projection.c. Snapshots scene images BEFORE release,
// then composes decoded video + scene into one primary projection on the GPU.
#include "surface_composite.h"
#include "surface_composite_spv.h"

#define SC_SCENES 4
typedef struct {
    XrSwapchain handle;
    VkImage image;
    VkImageView view;
    VkDeviceMemory memory;
    VkCommandBuffer commands[MAX_IMAGES];
    VkFence fences[MAX_IMAGES];
    int pending[MAX_IMAGES], ready;
    uint64_t serial,captured_release,releases;
    uint32_t width,height,layers;
} surf_scene_snapshot;
static surf_scene_snapshot sc_scenes[SC_SCENES];
static uint64_t sc_serial;
static int64_t sc_capture_until;

// Unity attaches both structures even when their transforms are identity.
// Apply supported transforms in our shader; reject other chains rather than
// silently discarding semantics when flattening the original scene.
static int sc_scene_supported(const XrCompositionLayerProjection *scene) {
    int reason=0;
    if(!scene || scene->viewCount!=2 || !scene->views)return 0;
    int count=0;
    for(const XrBaseInStructure *n=scene->next;n;n=n->next) {
        if(++count>16){reason=-1;break;}
        if(n->type==XR_TYPE_COMPOSITION_LAYER_COLOR_SCALE_BIAS_KHR)continue;
        if(n->type==XR_TYPE_COMPOSITION_LAYER_IMAGE_LAYOUT_FB) {
            const XrCompositionLayerImageLayoutFB *layout=(const void*)n;
            if(!(layout->flags&~XR_COMPOSITION_LAYER_IMAGE_LAYOUT_VERTICAL_FLIP_BIT_FB))continue;
        }
        reason=n->type;break;
    }
    for(int e=0;e<2 && !reason;e++)if(scene->views[e].next)
        reason=((const XrBaseInStructure*)scene->views[e].next)->type;
    static int previous;
    if(reason && reason!=previous)LOG("surface_composite: unsupported scene extension %d; retaining ordinary composition",reason);
    previous=reason;return !reason;
}

typedef struct {
    XrSwapchain output;
    VkImage images[MAX_IMAGES];
    VkImageView views[MAX_IMAGES];
    VkFramebuffer framebuffers[MAX_IMAGES];
    VkRenderPass render_pass;
    VkSampler sampler;
    VkDescriptorPool pool;
    VkDescriptorSetLayout set_layout;
    VkDescriptorSet sets[MAX_IMAGES][2];
    VkPipelineLayout layout;
    VkPipeline pipeline;
    VkCommandBuffer commands[MAX_IMAGES];
    VkFence fences[MAX_IMAGES];
    int pending[MAX_IMAGES], ready, acquired, failed;
    uint32_t count,index;
    XrTime time;
    XrCompositionLayerProjection layer;
    XrCompositionLayerProjectionView eyes[2];
    int64_t log_time;
    uint64_t frames,holds;
} surf_composite;
static surf_composite sc_outputs[SURF_MAX];

static void sc_barrier(VkCommandBuffer cmd,VkImage image,uint32_t layers,VkImageLayout from,VkImageLayout to,
    VkAccessFlags src,VkAccessFlags dst) {
    VkImageMemoryBarrier b={VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,NULL,src,dst,from,to,
        VK_QUEUE_FAMILY_IGNORED,VK_QUEUE_FAMILY_IGNORED,image,{VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,layers}};
    vk.CmdPipelineBarrier(cmd,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
        0,0,NULL,0,NULL,1,&b);
}
static int sc_command(VkCommandBuffer *cmd,VkFence *fence) {
    VkCommandBufferAllocateInfo ai={VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,NULL,vk.pool,VK_COMMAND_BUFFER_LEVEL_PRIMARY,1};
    VkFenceCreateInfo fi={VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    if(vk.AllocateCommandBuffers(vk.device,&ai,cmd)!=VK_SUCCESS)return 0;
    if(vk.CreateFence(vk.device,&fi,NULL,fence)==VK_SUCCESS)return 1;
    PFN_vkFreeCommandBuffers free_cmd=(PFN_vkFreeCommandBuffers)vk.GetDeviceProcAddr(vk.device,"vkFreeCommandBuffers");
    free_cmd(vk.device,vk.pool,1,cmd);*cmd=VK_NULL_HANDLE;return 0;
}
static int sc_reuse(VkFence fence,int *pending) {
    if(!*pending)return 1;
    if(svk.GetFenceStatus(vk.device,fence)!=VK_SUCCESS)return 0;
    if(vk.ResetFences(vk.device,1,&fence)!=VK_SUCCESS)return 0;
    *pending=0;return 1;
}
static void sc_retire(VkCommandBuffer *commands,VkFence *fences,int *pending,uint32_t count) {
    PFN_vkDestroyFence destroy=(PFN_vkDestroyFence)vk.GetDeviceProcAddr(vk.device,"vkDestroyFence");
    PFN_vkFreeCommandBuffers free_cmd=(PFN_vkFreeCommandBuffers)vk.GetDeviceProcAddr(vk.device,"vkFreeCommandBuffers");
    for(uint32_t i=0;i<count;i++) {
        if(pending[i])vk.WaitForFences(vk.device,1,&fences[i],VK_TRUE,UINT64_MAX);
        if(fences[i])destroy(vk.device,fences[i],NULL);
        if(commands[i])free_cmd(vk.device,vk.pool,1,&commands[i]);
    }
}
static void surf_scene_destroy(XrSwapchain handle) {
    if(!handle || spvk.ready<=0)return;
    // A compositor command can still be sampling this mirror after its
    // snapshot copy completes. Retire those readers before destroying a view.
    for(int k=0;k<SURF_MAX;k++)for(uint32_t j=0;j<sc_outputs[k].count;j++)
        if(sc_outputs[k].pending[j])vk.WaitForFences(vk.device,1,&sc_outputs[k].fences[j],VK_TRUE,UINT64_MAX);
    for(int i=0;i<SC_SCENES;i++)if(sc_scenes[i].handle==handle) {
        surf_scene_snapshot *s=&sc_scenes[i];
        sc_retire(s->commands,s->fences,s->pending,MAX_IMAGES);
        if(s->view)spvk.DestroyImageView(vk.device,s->view,NULL);
        if(s->image)spvk.DestroyImage(vk.device,s->image,NULL);
        if(s->memory)spvk.FreeMemory(vk.device,s->memory,NULL);
        memset(s,0,sizeof(*s));
    }
}
static void surf_composite_destroy(surf_projection *p) {
    surf_composite *c=&sc_outputs[p-surf_projections];
    sc_retire(c->commands,c->fences,c->pending,c->count);
    if(c->pipeline)spvk.DestroyPipeline(vk.device,c->pipeline,NULL);
    if(c->layout)spvk.DestroyPipelineLayout(vk.device,c->layout,NULL);
    if(c->pool)spvk.DestroyDescriptorPool(vk.device,c->pool,NULL);
    if(c->set_layout)spvk.DestroyDescriptorSetLayout(vk.device,c->set_layout,NULL);
    if(c->sampler)spvk.DestroySampler(vk.device,c->sampler,NULL);
    for(uint32_t i=0;i<c->count;i++) {
        if(c->framebuffers[i])spvk.DestroyFramebuffer(vk.device,c->framebuffers[i],NULL);
        if(c->views[i])spvk.DestroyImageView(vk.device,c->views[i],NULL);
    }
    if(c->render_pass)spvk.DestroyRenderPass(vk.device,c->render_pass,NULL);
    if(c->output) {
        PFN_xrDestroySwapchain fn=(PFN_xrDestroySwapchain)lookup(active_instance,"xrDestroySwapchain");
        if(c->acquired) {
            PFN_xrWaitSwapchainImage wait=(PFN_xrWaitSwapchainImage)lookup(active_instance,"xrWaitSwapchainImage");
            PFN_xrReleaseSwapchainImage release=(PFN_xrReleaseSwapchainImage)lookup(active_instance,"xrReleaseSwapchainImage");
            XrSwapchainImageWaitInfo wi={XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO,NULL,XR_INFINITE_DURATION};
            XrSwapchainImageReleaseInfo ri={XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO};
            if(wait(c->output,&wi)==XR_SUCCESS)release(c->output,&ri);
        }
        if(fn)fn(c->output);
    }
    memset(c,0,sizeof(*c));
}

// Called while the application still owns this successfully waited image.
// Queue ordering includes the application's rendering and this snapshot copy;
// no CPU readback, post-release access, or deferred OpenXR release is needed.
static void surf_scene_capture(tracked_swapchain *t,uint32_t index) {
    if(!surface_native || monotonic_ns()>sc_capture_until || !t || index>=t->image_count ||
        spvk.ready<=0 || t->info.sampleCount!=1 || t->info.arraySize!=2 ||
        !(t->info.usageFlags&XR_SWAPCHAIN_USAGE_TRANSFER_SRC_BIT) ||
        !(t->info.usageFlags&XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT))return;
    surf_scene_snapshot *s=NULL;
    for(int i=0;i<SC_SCENES;i++)if(sc_scenes[i].handle==t->handle){s=&sc_scenes[i];break;}
    if(!s)for(int i=0;i<SC_SCENES;i++)if(!sc_scenes[i].handle){s=&sc_scenes[i];s->handle=t->handle;break;}
    if(!s)return;
    if(!s->view) {
        s->width=t->info.width;s->height=t->info.height;s->layers=t->info.arraySize;
        if(!surf_private_image(s->width,s->height,s->layers,(VkFormat)t->info.format,
            VK_IMAGE_USAGE_TRANSFER_DST_BIT|VK_IMAGE_USAGE_SAMPLED_BIT,&s->image,&s->memory,&s->view)) {
            surf_scene_destroy(t->handle);return;
        }
    }
    if(!s->commands[index] && !sc_command(&s->commands[index],&s->fences[index]))return;
    if(!sc_reuse(s->fences[index],&s->pending[index]))return;
    VkCommandBuffer cmd=s->commands[index];
    VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,NULL,VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    if(vk.ResetCommandBuffer(cmd,0)!=VK_SUCCESS || vk.BeginCommandBuffer(cmd,&begin)!=VK_SUCCESS)return;
    sc_barrier(cmd,t->images[index],s->layers,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
        VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT);
    sc_barrier(cmd,s->image,s->layers,s->ready?VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL:VK_IMAGE_LAYOUT_UNDEFINED,
        VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,s->ready?VK_ACCESS_SHADER_READ_BIT:0,VK_ACCESS_TRANSFER_WRITE_BIT);
    VkImageCopy copy={{VK_IMAGE_ASPECT_COLOR_BIT,0,0,s->layers},{0,0,0},
        {VK_IMAGE_ASPECT_COLOR_BIT,0,0,s->layers},{0,0,0},{s->width,s->height,1}};
    spvk.CmdCopyImage(cmd,t->images[index],VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,s->image,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,1,&copy);
    sc_barrier(cmd,s->image,s->layers,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
        VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_SHADER_READ_BIT);
    sc_barrier(cmd,t->images[index],s->layers,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
        VK_ACCESS_TRANSFER_READ_BIT,VK_ACCESS_COLOR_ATTACHMENT_READ_BIT|VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT);
    if(vk.EndCommandBuffer(cmd)!=VK_SUCCESS)return;
    VkSubmitInfo submit={VK_STRUCTURE_TYPE_SUBMIT_INFO,NULL,0,NULL,NULL,1,&cmd};
    if(vk.QueueSubmit(vk.queue,1,&submit,s->fences[index])!=VK_SUCCESS)return;
    s->pending[index]=1;s->ready=1;s->serial=sc_serial+1;s->captured_release=s->releases+1;
}

static int sc_setup(XrSession session,surf_projection *p,surf_composite *c) {
    if(c->pipeline)return 1;
    if(c->failed)return 0;
    PFN_xrCreateSwapchain create=(PFN_xrCreateSwapchain)lookup(active_instance,"xrCreateSwapchain");
    PFN_xrEnumerateSwapchainImages enumerate=(PFN_xrEnumerateSwapchainImages)lookup(active_instance,"xrEnumerateSwapchainImages");
    XrSwapchainCreateInfo ci={XR_TYPE_SWAPCHAIN_CREATE_INFO,NULL,0,
        XR_SWAPCHAIN_USAGE_TRANSFER_DST_BIT|XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT,
        VK_FORMAT_R8G8B8A8_SRGB,1,2*p->size,p->size,1,1,1};
    if(!create || !enumerate || XR_FAILED(create(session,&ci,&c->output)))goto fail;
    XrSwapchainImageVulkanKHR images[MAX_IMAGES];
    for(int i=0;i<MAX_IMAGES;i++)images[i]=(XrSwapchainImageVulkanKHR){XR_TYPE_SWAPCHAIN_IMAGE_VULKAN_KHR};
    if(XR_FAILED(enumerate(c->output,MAX_IMAGES,&c->count,(void*)images)) || !c->count || c->count>MAX_IMAGES) {
        c->count=0;goto fail;
    }
    for(uint32_t i=0;i<c->count;i++) {
        c->images[i]=images[i].image;
        if(!sc_command(&c->commands[i],&c->fences[i]))goto fail;
    }
    VkAttachmentDescription attachment={0,VK_FORMAT_R8G8B8A8_SRGB,VK_SAMPLE_COUNT_1_BIT,
        VK_ATTACHMENT_LOAD_OP_DONT_CARE,VK_ATTACHMENT_STORE_OP_STORE,VK_ATTACHMENT_LOAD_OP_DONT_CARE,
        VK_ATTACHMENT_STORE_OP_DONT_CARE,VK_IMAGE_LAYOUT_UNDEFINED,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkAttachmentReference reference={0,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkSubpassDescription subpass={0,VK_PIPELINE_BIND_POINT_GRAPHICS,0,NULL,1,&reference};
    VkSubpassDependency dependency={VK_SUBPASS_EXTERNAL,0,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
        VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT|VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
        VK_ACCESS_MEMORY_WRITE_BIT|VK_ACCESS_MEMORY_READ_BIT,VK_ACCESS_SHADER_READ_BIT|VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT};
    VkRenderPassCreateInfo rpi={VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO,NULL,0,1,&attachment,1,&subpass,1,&dependency};
    if(spvk.CreateRenderPass(vk.device,&rpi,NULL,&c->render_pass)!=VK_SUCCESS)goto fail;
    for(uint32_t i=0;i<c->count;i++) {
        VkImageViewCreateInfo vi={VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,NULL,0,c->images[i],VK_IMAGE_VIEW_TYPE_2D,
            VK_FORMAT_R8G8B8A8_SRGB,{0,0,0,0},{VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1}};
        if(spvk.CreateImageView(vk.device,&vi,NULL,&c->views[i])!=VK_SUCCESS)goto fail;
        VkFramebufferCreateInfo fi={VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO,NULL,0,c->render_pass,1,&c->views[i],2*p->size,p->size,1};
        if(spvk.CreateFramebuffer(vk.device,&fi,NULL,&c->framebuffers[i])!=VK_SUCCESS)goto fail;
    }
    VkSamplerCreateInfo si={VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO,NULL,0,VK_FILTER_LINEAR,VK_FILTER_LINEAR,
        VK_SAMPLER_MIPMAP_MODE_NEAREST,VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
        VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE};
    if(spvk.CreateSampler(vk.device,&si,NULL,&c->sampler)!=VK_SUCCESS)goto fail;
    VkDescriptorSetLayoutBinding bindings[2]={{0,VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,1,VK_SHADER_STAGE_FRAGMENT_BIT},
        {1,VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,1,VK_SHADER_STAGE_FRAGMENT_BIT}};
    VkDescriptorSetLayoutCreateInfo li={VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,NULL,0,2,bindings};
    if(spvk.CreateDescriptorSetLayout(vk.device,&li,NULL,&c->set_layout)!=VK_SUCCESS)goto fail;
    VkDescriptorPoolSize sizes[1]={{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,4*c->count}};
    VkDescriptorPoolCreateInfo pi={VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,NULL,0,2*c->count,1,sizes};
    if(spvk.CreateDescriptorPool(vk.device,&pi,NULL,&c->pool)!=VK_SUCCESS)goto fail;
    for(uint32_t i=0;i<c->count;i++)for(int e=0;e<2;e++) {
        VkDescriptorSetAllocateInfo ai={VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,NULL,c->pool,1,&c->set_layout};
        if(spvk.AllocateDescriptorSets(vk.device,&ai,&c->sets[i][e])!=VK_SUCCESS)goto fail;
    }
    VkPushConstantRange range={VK_SHADER_STAGE_FRAGMENT_BIT,0,sizeof(surf_composite_params)};
    VkPipelineLayoutCreateInfo pli={VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,NULL,0,1,&c->set_layout,1,&range};
    if(spvk.CreatePipelineLayout(vk.device,&pli,NULL,&c->layout)!=VK_SUCCESS)goto fail;
    VkShaderModule modules[2]={0};
    VkShaderModuleCreateInfo mi={VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,NULL,0,sizeof(surface_composite_vert_spv),surface_composite_vert_spv};
    if(spvk.CreateShaderModule(vk.device,&mi,NULL,&modules[0])!=VK_SUCCESS)goto fail;
    mi.codeSize=sizeof(surface_composite_frag_spv);mi.pCode=surface_composite_frag_spv;
    if(spvk.CreateShaderModule(vk.device,&mi,NULL,&modules[1])!=VK_SUCCESS) {
        spvk.DestroyShaderModule(vk.device,modules[0],NULL);goto fail;
    }
    VkPipelineShaderStageCreateInfo stages[2]={
        {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,NULL,0,VK_SHADER_STAGE_VERTEX_BIT,modules[0],"main"},
        {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,NULL,0,VK_SHADER_STAGE_FRAGMENT_BIT,modules[1],"main"}};
    VkPipelineVertexInputStateCreateInfo vertices={VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO};
    VkPipelineInputAssemblyStateCreateInfo assembly={VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO,NULL,0,VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST};
    VkPipelineViewportStateCreateInfo viewport={VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO,NULL,0,1,NULL,1,NULL};
    VkPipelineRasterizationStateCreateInfo raster={VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO,NULL,0,VK_FALSE,VK_FALSE,
        VK_POLYGON_MODE_FILL,VK_CULL_MODE_NONE,VK_FRONT_FACE_COUNTER_CLOCKWISE,VK_FALSE,0,0,0,1};
    VkPipelineMultisampleStateCreateInfo samples={VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO,NULL,0,VK_SAMPLE_COUNT_1_BIT};
    VkPipelineColorBlendAttachmentState blend={.colorWriteMask=VK_COLOR_COMPONENT_R_BIT|VK_COLOR_COMPONENT_G_BIT|VK_COLOR_COMPONENT_B_BIT|VK_COLOR_COMPONENT_A_BIT};
    VkPipelineColorBlendStateCreateInfo blends={VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO,NULL,0,VK_FALSE,VK_LOGIC_OP_COPY,1,&blend};
    VkDynamicState dynamic_states[2]={VK_DYNAMIC_STATE_VIEWPORT,VK_DYNAMIC_STATE_SCISSOR};
    VkPipelineDynamicStateCreateInfo dynamic={VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO,NULL,0,2,dynamic_states};
    VkGraphicsPipelineCreateInfo pci={VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO,NULL,0,2,stages,&vertices,&assembly,NULL,&viewport,
        &raster,&samples,NULL,&blends,&dynamic,c->layout,c->render_pass,0,VK_NULL_HANDLE,-1};
    VkResult result=spvk.CreateGraphicsPipelines(vk.device,VK_NULL_HANDLE,1,&pci,NULL,&c->pipeline);
    for(int k=0;k<2;k++)spvk.DestroyShaderModule(vk.device,modules[k],NULL);
    if(result!=VK_SUCCESS)goto fail;
    LOG("surface_composite: one primary stereo scene, %ux%u per eye",p->size,p->size);return 1;
fail:
    surf_composite_destroy(p);c->failed=1;
    LOG("surface_composite: setup failed; retaining ordinary composition");return 0;
}

static const XrCompositionLayerBaseHeader *surf_composite_frame(XrSession session,const XrFrameEndInfo *info,
    const XrCompositionLayerProjection *scene,const XrCompositionLayerBaseHeader *video) {
    surf_projection *p=NULL;
    for(int i=0;i<SURF_MAX;i++)if(video==(const void*)&surf_projections[i].layer && surfs[i].native_project) {
        p=&surf_projections[i];break;
    }
    if(!p || !p->cache_ready || !sc_scene_supported(scene))return NULL;
    if(p->presented_job.params[0][7]!=0 || p->presented_job.params[1][7]!=0)return NULL;
    sc_capture_until=monotonic_ns()+500000000ll;
    surf_composite *c=&sc_outputs[p-surf_projections];
    surf_scene_snapshot *sources[2]={0};
    for(int e=0;e<2;e++)for(int i=0;i<SC_SCENES;i++)if(sc_scenes[i].handle==scene->views[e].subImage.swapchain) {
        sources[e]=&sc_scenes[i];break;
    }
    if(p->cached_job.space!=surfs[p-surf_projections].video_job.space)return NULL;
    if(!sources[0] || !sources[1] || sources[0]->captured_release!=sources[0]->releases || sources[1]->captured_release!=sources[1]->releases)goto held;
    for(int e=0;e<2;e++)if(scene->views[e].next || scene->views[e].subImage.imageArrayIndex>=sources[e]->layers)goto held;
    if(!sc_setup(session,p,c))return NULL;
    PFN_xrAcquireSwapchainImage acquire=(PFN_xrAcquireSwapchainImage)lookup(active_instance,"xrAcquireSwapchainImage");
    PFN_xrWaitSwapchainImage wait=(PFN_xrWaitSwapchainImage)lookup(active_instance,"xrWaitSwapchainImage");
    PFN_xrReleaseSwapchainImage release=(PFN_xrReleaseSwapchainImage)lookup(active_instance,"xrReleaseSwapchainImage");
    XrSwapchainImageAcquireInfo ai={XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO};
    XrSwapchainImageWaitInfo wi={XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO,NULL,0};
    XrSwapchainImageReleaseInfo ri={XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO};
    if(!c->acquired) {
        if(XR_FAILED(acquire(c->output,&ai,&c->index)))goto held;
        c->acquired=1;
    }
    if(wait(c->output,&wi)!=XR_SUCCESS)goto held;
    if(c->index>=c->count) {release(c->output,&ri);c->acquired=0;goto held;}
    uint32_t index=c->index;
    if(!sc_reuse(c->fences[index],&c->pending[index]))goto held;
    surf_swapchain *s=&surfs[p-surf_projections];
    pthread_mutex_lock(&surf_lock);surf_video_job current=s->video_job;pthread_mutex_unlock(&surf_lock);
    VkCommandBuffer cmd=c->commands[index];
    VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,NULL,VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    if(vk.ResetCommandBuffer(cmd,0)!=VK_SUCCESS || vk.BeginCommandBuffer(cmd,&begin)!=VK_SUCCESS)goto held;
    VkRenderPassBeginInfo rbi={VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO,NULL,c->render_pass,c->framebuffers[index],
        {{0,0},{2*p->size,p->size}},0,NULL};
    spvk.CmdBeginRenderPass(cmd,&rbi,VK_SUBPASS_CONTENTS_INLINE);
    spvk.CmdBindPipeline(cmd,VK_PIPELINE_BIND_POINT_GRAPHICS,c->pipeline);
    for(int e=0;e<2;e++) {
        VkDescriptorImageInfo di[2]={{c->sampler,p->video_cache_view,VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL},
            {c->sampler,sources[e]->view,VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL}};
        VkWriteDescriptorSet writes[2];
        for(int k=0;k<2;k++)writes[k]=(VkWriteDescriptorSet){VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,NULL,c->sets[index][e],k,0,1,
            VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,&di[k]};
        spvk.UpdateDescriptorSets(vk.device,2,writes,0,NULL);
        spvk.CmdBindDescriptorSets(cmd,VK_PIPELINE_BIND_POINT_GRAPHICS,c->layout,0,1,&c->sets[index][e],0,NULL);
        surf_composite_params params=surf_composite_parameters(&p->cached_job,&current,e,
            scene->views[e].subImage.imageRect,sources[e]->width,sources[e]->height,scene->views[e].subImage.imageArrayIndex);
        params.scene_options[0]=(scene->layerFlags&XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT)!=0;
        params.scene_options[1]=(scene->layerFlags&XR_COMPOSITION_LAYER_UNPREMULTIPLIED_ALPHA_BIT)!=0;
        for(const XrBaseInStructure *n=scene->next;n;n=n->next) {
            if(n->type==XR_TYPE_COMPOSITION_LAYER_COLOR_SCALE_BIAS_KHR) {
                const XrCompositionLayerColorScaleBiasKHR *color=(const void*)n;
                memcpy(params.scene_scale,&color->colorScale,16);memcpy(params.scene_bias,&color->colorBias,16);
            }
            if(n->type==XR_TYPE_COMPOSITION_LAYER_IMAGE_LAYOUT_FB)
                params.scene_options[2]=(((const XrCompositionLayerImageLayoutFB*)n)->flags&XR_COMPOSITION_LAYER_IMAGE_LAYOUT_VERTICAL_FLIP_BIT_FB)!=0;
        }
        VkViewport viewport={(float)(e*p->size),0,(float)p->size,(float)p->size,0,1};
        VkRect2D scissor={{e*(int32_t)p->size,0},{p->size,p->size}};
        spvk.CmdSetViewport(cmd,0,1,&viewport);spvk.CmdSetScissor(cmd,0,1,&scissor);
        spvk.CmdPushConstants(cmd,c->layout,VK_SHADER_STAGE_FRAGMENT_BIT,0,sizeof(params),&params);
        spvk.CmdDraw(cmd,3,1,0,0);
    }
    spvk.CmdEndRenderPass(cmd);
    if(vk.EndCommandBuffer(cmd)!=VK_SUCCESS)goto held;
    VkSubmitInfo submit={VK_STRUCTURE_TYPE_SUBMIT_INFO,NULL,0,NULL,NULL,1,&cmd};
    if(vk.QueueSubmit(vk.queue,1,&submit,c->fences[index])!=VK_SUCCESS)goto held;
    c->pending[index]=1;
    if(XR_FAILED(release(c->output,&ri)))goto held;
    c->acquired=0;
    for(int e=0;e<2;e++) {
        c->eyes[e]=scene->views[e];c->eyes[e].next=NULL;
        c->eyes[e].subImage=(XrSwapchainSubImage){c->output,{{e*(int32_t)p->size,0},{(int32_t)p->size,(int32_t)p->size}},0};
    }
    c->layer=*scene;c->layer.layerFlags &= ~XR_COMPOSITION_LAYER_UNPREMULTIPLIED_ALPHA_BIT;c->layer.next=NULL;c->layer.views=c->eyes;c->ready=1;c->time=info->displayTime;c->frames++;
    if(monotonic_ns()-c->log_time>5000000000ll) {
        LOG("surface_composite: %llu new scene frames, %llu held; no scene/overlay promotion",(unsigned long long)c->frames,(unsigned long long)c->holds);
        c->log_time=monotonic_ns();
    }
    return (const void*)&c->layer;
held:
    c->holds++;
    return c->ready && c->layer.space==scene->space?(const void*)&c->layer:NULL;
}

static int surf_composite_candidate(const XrFrameEndInfo *info,const XrCompositionLayerBaseHeader *video) {
    if(!info || !info->layerCount || !info->layers[0] || info->layers[0]->type!=XR_TYPE_COMPOSITION_LAYER_PROJECTION ||
        !emul_is_equirect(video))return 0;
    const XrCompositionLayerProjection *scene=(const void*)info->layers[0];
    if(!sc_scene_supported(scene))return 0;
    emul_common target=emul_common_of(video);
    for(uint32_t i=1;i<info->layerCount;i++) {
        const XrCompositionLayerBaseHeader *layer=info->layers[i];
        if(layer==video)return 1;
        if(!layer)continue;
        if(!emul_is_equirect(layer))return 0;
        emul_common other=emul_common_of(layer);
        if(other.sub.swapchain!=target.sub.swapchain || other.space!=target.space)return 0;
    }
    return 0;
}
static void surf_scene_release_commit(XrSwapchain handle) {
    for(int i=0;i<SC_SCENES;i++)if(sc_scenes[i].handle==handle){sc_scenes[i].releases++;break;}
}
static int surf_composite_cached_only(const XrFrameEndInfo *info,surf_projection *p,
    const surf_video_job *rendered,const surf_video_job *current) {
    surf_composite *c=&sc_outputs[p-surf_projections];
    const XrCompositionLayerProjection *scene=(const void*)info->layers[0];
    return c->ready && !p->acquired && c->layer.space==scene->space && rendered->space==current->space &&
        rendered->params[0][7]==0 && rendered->params[1][7]==0 && !c->failed;
}

static int surf_composite_active(surf_projection *p) {return sc_outputs[p-surf_projections].ready;}
