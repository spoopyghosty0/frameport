// SPDX-License-Identifier: GPL-3.0-only
// Vulkan presentation for equirectangular Android video surfaces. The decoder's
// native GPU path projects directly from YUV on the surface worker, then copies
// the finished stereo view into a runtime swapchain. CPU-delivered/reference
// panoramas use a compute shader. No unsupported equirect layer reaches runtime.
#include "surface_projection_spv.h"
typedef struct {
    XrSwapchain output;
    VkImage images[MAX_IMAGES], source, rendered, video_cache;
    VkImageView rendered_view, source_view, video_cache_view;
    VkDeviceMemory memory, rendered_memory, video_cache_memory;
    VkSampler sampler;
    VkDescriptorPool descriptors;
    VkDescriptorSetLayout set_layout;
    VkDescriptorSet set;
    VkPipelineLayout layout;
    VkPipeline pipeline;
    uint32_t count, size, width, height, acquired_index;
    int acquired;
    XrTime presented_time;
    XrTime tracking_log_time;
    uint64_t source_seq;
    int source_ready, ready, view_source, occludes, cache_ready;
    XrCompositionLayerProjection layer;
    XrCompositionLayerProjectionView eyes[2];
    surf_video_job presented_job, cached_job;
} surf_projection;
static surf_projection surf_projections[SURF_MAX];
static struct {
#define SP_FN(name) PFN_vk##name name;
    SP_FN(CreateImage) SP_FN(DestroyImage) SP_FN(GetImageMemoryRequirements) SP_FN(BindImageMemory)
    SP_FN(CreateImageView) SP_FN(DestroyImageView) SP_FN(CreateSampler) SP_FN(DestroySampler)
    SP_FN(CreateDescriptorSetLayout) SP_FN(DestroyDescriptorSetLayout) SP_FN(CreateDescriptorPool)
    SP_FN(DestroyDescriptorPool) SP_FN(AllocateDescriptorSets) SP_FN(UpdateDescriptorSets)
    SP_FN(CreatePipelineLayout) SP_FN(DestroyPipelineLayout) SP_FN(CreateShaderModule) SP_FN(DestroyShaderModule)
    SP_FN(CreateRenderPass) SP_FN(DestroyRenderPass) SP_FN(CreateFramebuffer) SP_FN(DestroyFramebuffer)
    SP_FN(CreateGraphicsPipelines) SP_FN(CmdBeginRenderPass) SP_FN(CmdEndRenderPass)
    SP_FN(CmdSetViewport) SP_FN(CmdSetScissor) SP_FN(CmdDraw)
    SP_FN(CreateComputePipelines) SP_FN(DestroyPipeline) SP_FN(CmdBindPipeline) SP_FN(CmdBindDescriptorSets)
    SP_FN(CmdPushConstants) SP_FN(CmdDispatch) SP_FN(CmdCopyImage) SP_FN(FreeMemory)
#undef SP_FN
    int ready;
} spvk;
static int surf_projection_functions(void) {
    if(spvk.ready)return spvk.ready>0;
    spvk.ready=-1;
#define SP_LOAD(name) spvk.name=(PFN_vk##name)vk.GetDeviceProcAddr(vk.device,"vk" #name); if(!spvk.name)return 0;
    SP_LOAD(CreateImage) SP_LOAD(DestroyImage) SP_LOAD(GetImageMemoryRequirements) SP_LOAD(BindImageMemory)
    SP_LOAD(CreateImageView) SP_LOAD(DestroyImageView) SP_LOAD(CreateSampler) SP_LOAD(DestroySampler)
    SP_LOAD(CreateDescriptorSetLayout) SP_LOAD(DestroyDescriptorSetLayout) SP_LOAD(CreateDescriptorPool)
    SP_LOAD(DestroyDescriptorPool) SP_LOAD(AllocateDescriptorSets) SP_LOAD(UpdateDescriptorSets)
    SP_LOAD(CreatePipelineLayout) SP_LOAD(DestroyPipelineLayout) SP_LOAD(CreateShaderModule) SP_LOAD(DestroyShaderModule)
    SP_LOAD(CreateRenderPass) SP_LOAD(DestroyRenderPass) SP_LOAD(CreateFramebuffer) SP_LOAD(DestroyFramebuffer)
    SP_LOAD(CreateGraphicsPipelines) SP_LOAD(CmdBeginRenderPass) SP_LOAD(CmdEndRenderPass)
    SP_LOAD(CmdSetViewport) SP_LOAD(CmdSetScissor) SP_LOAD(CmdDraw)
    SP_LOAD(CreateComputePipelines) SP_LOAD(DestroyPipeline) SP_LOAD(CmdBindPipeline) SP_LOAD(CmdBindDescriptorSets)
    SP_LOAD(CmdPushConstants) SP_LOAD(CmdDispatch) SP_LOAD(CmdCopyImage) SP_LOAD(FreeMemory)
#undef SP_LOAD
    spvk.ready=1;return 1;
}
// All images belong to the session device; failures leave cleanup-safe handles.
static int surf_private_image(uint32_t width,uint32_t height,uint32_t layers,VkFormat format,
    VkImageUsageFlags usage,VkImage *image,VkDeviceMemory *memory,VkImageView *view) {
    VkImageCreateInfo ci={VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,NULL,0,VK_IMAGE_TYPE_2D,format,
        {width,height,1},1,layers,VK_SAMPLE_COUNT_1_BIT,VK_IMAGE_TILING_OPTIMAL,usage,VK_SHARING_MODE_EXCLUSIVE};
    if(spvk.CreateImage(vk.device,&ci,NULL,image)!=VK_SUCCESS)return 0;
    VkMemoryRequirements req;spvk.GetImageMemoryRequirements(vk.device,*image,&req);
    VkPhysicalDeviceMemoryProperties props;svk.GetPhysicalDeviceMemoryProperties(vk.physical,&props);
    uint32_t type=UINT32_MAX;
    for(uint32_t i=0;i<props.memoryTypeCount;i++)if((req.memoryTypeBits&(1u<<i)) &&
        (props.memoryTypes[i].propertyFlags&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)){type=i;break;}
    if(type==UINT32_MAX)return 0;
    VkMemoryAllocateInfo alloc={VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,NULL,req.size,type};
    if(svk.AllocateMemory(vk.device,&alloc,NULL,memory)!=VK_SUCCESS ||
       spvk.BindImageMemory(vk.device,*image,*memory,0)!=VK_SUCCESS)return 0;
    VkImageViewCreateInfo vi={VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,NULL,0,*image,
        layers>1?VK_IMAGE_VIEW_TYPE_2D_ARRAY:VK_IMAGE_VIEW_TYPE_2D,format,{0,0,0,0},
        {VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,layers}};
    return spvk.CreateImageView(vk.device,&vi,NULL,view)==VK_SUCCESS;
}
static void surf_composite_destroy(surf_projection *p);
static int surf_composite_active(surf_projection *p);
static int surf_composite_candidate(const XrFrameEndInfo *info,const XrCompositionLayerBaseHeader *video);
static int surf_composite_cached_only(const XrFrameEndInfo *info,surf_projection *p,const surf_video_job *rendered,const surf_video_job *current);
static void surf_projection_destroy(surf_swapchain *s) {
    surf_projection *p=&surf_projections[s-surfs];
    if(!spvk.ready || spvk.ready<0)return;
    if(s->in_flight) {
        vk.WaitForFences(vk.device,1,&s->fence,VK_TRUE,UINT64_MAX);
        vk.ResetFences(vk.device,1,&s->fence);s->in_flight=0;
    }
    surf_native_complete(s);
    surf_native_retire(s);
    surf_composite_destroy(p);
    if(p->video_cache_view)spvk.DestroyImageView(vk.device,p->video_cache_view,NULL);
    if(p->video_cache)spvk.DestroyImage(vk.device,p->video_cache,NULL);
    if(p->video_cache_memory)spvk.FreeMemory(vk.device,p->video_cache_memory,NULL);
    if(p->rendered_view)spvk.DestroyImageView(vk.device,p->rendered_view,NULL);
    if(p->rendered)spvk.DestroyImage(vk.device,p->rendered,NULL);
    if(p->rendered_memory)spvk.FreeMemory(vk.device,p->rendered_memory,NULL);
    if(p->pipeline)spvk.DestroyPipeline(vk.device,p->pipeline,NULL);
    if(p->layout)spvk.DestroyPipelineLayout(vk.device,p->layout,NULL);
    if(p->descriptors)spvk.DestroyDescriptorPool(vk.device,p->descriptors,NULL);
    if(p->set_layout)spvk.DestroyDescriptorSetLayout(vk.device,p->set_layout,NULL);
    if(p->sampler)spvk.DestroySampler(vk.device,p->sampler,NULL);
    if(p->source_view)spvk.DestroyImageView(vk.device,p->source_view,NULL);
    if(p->source)spvk.DestroyImage(vk.device,p->source,NULL);
    if(p->memory)spvk.FreeMemory(vk.device,p->memory,NULL);
    if(p->output) {
        PFN_xrDestroySwapchain destroy=(PFN_xrDestroySwapchain)lookup(active_instance,"xrDestroySwapchain");
        if(destroy)destroy(p->output);
    }
    memset(p,0,sizeof(*p));
}
static int surf_projection_setup(XrSession session,surf_swapchain *s,surf_projection *p,uint32_t width,uint32_t height) {
    if(p->pipeline || (s->native_project && p->output))return 1;
    if(!surf_vk_ready() || !surf_staging(s) || !surf_projection_functions())return 0;
    const char *stage="runtime swapchain";
    p->size=equirect_res>=512 && equirect_res<=4096?(uint32_t)equirect_res:1536;
    if(s->native_mode && !equirect_res_set)p->size=2560;
    p->width=width;p->height=height;
    XrSwapchainCreateInfo ci={XR_TYPE_SWAPCHAIN_CREATE_INFO,NULL,0,
        XR_SWAPCHAIN_USAGE_TRANSFER_DST_BIT|XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT|XR_SWAPCHAIN_USAGE_SAMPLED_BIT,
        VK_FORMAT_R8G8B8A8_SRGB,1,2*p->size,p->size,1,1,1};
    PFN_xrCreateSwapchain create=(PFN_xrCreateSwapchain)lookup(active_instance,"xrCreateSwapchain");
    PFN_xrEnumerateSwapchainImages enumerate=(PFN_xrEnumerateSwapchainImages)lookup(active_instance,"xrEnumerateSwapchainImages");
    if(!create || !enumerate)goto fail;
    XrResult xr_result=create(session,&ci,&p->output);
    if(XR_FAILED(xr_result)) {
        static int create_failures;
        if(create_failures++<5)LOG("surface_projection: swapchain creation result=%d",xr_result);
        goto fail;
    }
    stage="runtime image enumeration";
    XrSwapchainImageVulkanKHR images[MAX_IMAGES];
    for(int i=0;i<MAX_IMAGES;i++)images[i]=(XrSwapchainImageVulkanKHR){XR_TYPE_SWAPCHAIN_IMAGE_VULKAN_KHR,NULL,VK_NULL_HANDLE};
    uint32_t image_count=0;
    if(XR_FAILED(enumerate(p->output,MAX_IMAGES,&image_count,(XrSwapchainImageBaseHeader*)images)) ||
       !image_count || image_count>MAX_IMAGES)goto fail;
    p->count=image_count;
    for(uint32_t i=0;i<p->count;i++)p->images[i]=images[i].image;
    if(s->native_project){
        stage="video composition cache";
        if(!surf_private_image(2*p->size,p->size,1,VK_FORMAT_R8G8B8A8_SRGB,
            VK_IMAGE_USAGE_TRANSFER_DST_BIT|VK_IMAGE_USAGE_SAMPLED_BIT,
            &p->video_cache,&p->video_cache_memory,&p->video_cache_view))goto fail;
        LOG("surface_projection: fused GPU video view ready (%ux%u per eye)",p->size,p->size);
        return 1;
    }
    stage="source image";
    VkImageCreateInfo image={VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,NULL,0,VK_IMAGE_TYPE_2D,VK_FORMAT_R8G8B8A8_UNORM,
        {width,height,1},1,1,VK_SAMPLE_COUNT_1_BIT,VK_IMAGE_TILING_OPTIMAL,
        VK_IMAGE_USAGE_TRANSFER_DST_BIT|VK_IMAGE_USAGE_SAMPLED_BIT,VK_SHARING_MODE_EXCLUSIVE,0,NULL,VK_IMAGE_LAYOUT_UNDEFINED};
    VkMemoryRequirements req;
    VkPhysicalDeviceMemoryProperties props;svk.GetPhysicalDeviceMemoryProperties(vk.physical,&props);
    uint32_t type=UINT32_MAX;
    VkMemoryAllocateInfo memory;
    VkImageViewCreateInfo view={VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,NULL,0,p->source,VK_IMAGE_VIEW_TYPE_2D,
        VK_FORMAT_R8G8B8A8_UNORM,{0,0,0,0},{VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1}};
    if(!s->native_mode){
        if(spvk.CreateImage(vk.device,&image,NULL,&p->source)!=VK_SUCCESS)goto fail;
        spvk.GetImageMemoryRequirements(vk.device,p->source,&req);
        for(uint32_t i=0;i<props.memoryTypeCount;i++)if((req.memoryTypeBits&(1u<<i)) &&
            (props.memoryTypes[i].propertyFlags&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)){type=i;break;}
        if(type==UINT32_MAX)goto fail;
        memory=(VkMemoryAllocateInfo){VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,NULL,req.size,type};
        if(svk.AllocateMemory(vk.device,&memory,NULL,&p->memory)!=VK_SUCCESS ||
           spvk.BindImageMemory(vk.device,p->source,p->memory,0)!=VK_SUCCESS)goto fail;
        view.image=p->source;
        if(spvk.CreateImageView(vk.device,&view,NULL,&p->source_view)!=VK_SUCCESS)goto fail;
    }
    // The runtime supports sRGB swapchains, which cannot be storage images.
    // Compute into a private UNORM image and copy its encoded pixels unchanged.
    stage="private storage image";
    image.extent=(VkExtent3D){2*p->size,p->size,1};
    image.usage=VK_IMAGE_USAGE_STORAGE_BIT|VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    if(spvk.CreateImage(vk.device,&image,NULL,&p->rendered)!=VK_SUCCESS)goto fail;
    spvk.GetImageMemoryRequirements(vk.device,p->rendered,&req);
    type=UINT32_MAX;
    for(uint32_t i=0;i<props.memoryTypeCount;i++)if((req.memoryTypeBits&(1u<<i)) &&
        (props.memoryTypes[i].propertyFlags&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)){type=i;break;}
    if(type==UINT32_MAX)goto fail;
    memory=(VkMemoryAllocateInfo){VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,NULL,req.size,type};
    if(svk.AllocateMemory(vk.device,&memory,NULL,&p->rendered_memory)!=VK_SUCCESS ||
       spvk.BindImageMemory(vk.device,p->rendered,p->rendered_memory,0)!=VK_SUCCESS)goto fail;
    view.image=p->rendered;
    if(spvk.CreateImageView(vk.device,&view,NULL,&p->rendered_view)!=VK_SUCCESS)goto fail;
    stage="sampler and descriptors";
    VkSamplerCreateInfo sampler={VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO,NULL,0,VK_FILTER_LINEAR,VK_FILTER_LINEAR,
        VK_SAMPLER_MIPMAP_MODE_NEAREST,VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
        VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,0,VK_FALSE,1,VK_FALSE,VK_COMPARE_OP_ALWAYS,0,0,VK_BORDER_COLOR_FLOAT_TRANSPARENT_BLACK,VK_FALSE};
    if(spvk.CreateSampler(vk.device,&sampler,NULL,&p->sampler)!=VK_SUCCESS)goto fail;
    VkDescriptorSetLayoutBinding bindings[2]={{0,VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,1,VK_SHADER_STAGE_COMPUTE_BIT,NULL},
        {1,VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,1,VK_SHADER_STAGE_COMPUTE_BIT,NULL}};
    VkDescriptorSetLayoutCreateInfo set={VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,NULL,0,2,bindings};
    if(spvk.CreateDescriptorSetLayout(vk.device,&set,NULL,&p->set_layout)!=VK_SUCCESS)goto fail;
    VkDescriptorPoolSize sizes[2]={{VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,1},{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,1}};
    VkDescriptorPoolCreateInfo pool={VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,NULL,0,1,2,sizes};
    if(spvk.CreateDescriptorPool(vk.device,&pool,NULL,&p->descriptors)!=VK_SUCCESS)goto fail;
    VkDescriptorSetAllocateInfo alloc={VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,NULL,p->descriptors,1,&p->set_layout};
    if(spvk.AllocateDescriptorSets(vk.device,&alloc,&p->set)!=VK_SUCCESS)goto fail;
    stage="compute pipeline";
    VkPushConstantRange range={VK_SHADER_STAGE_COMPUTE_BIT,0,128};
    VkPipelineLayoutCreateInfo layout={VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,NULL,0,1,&p->set_layout,1,&range};
    if(spvk.CreatePipelineLayout(vk.device,&layout,NULL,&p->layout)!=VK_SUCCESS)goto fail;
    VkShaderModule module;
    VkShaderModuleCreateInfo shader={VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,NULL,0,sizeof(surface_projection_spv),surface_projection_spv};
    if(spvk.CreateShaderModule(vk.device,&shader,NULL,&module)!=VK_SUCCESS)goto fail;
    VkComputePipelineCreateInfo pipeline={VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,NULL,0,
        {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,NULL,0,VK_SHADER_STAGE_COMPUTE_BIT,module,"main",NULL},p->layout,VK_NULL_HANDLE,0};
    VkResult result=spvk.CreateComputePipelines(vk.device,VK_NULL_HANDLE,1,&pipeline,NULL,&p->pipeline);
    spvk.DestroyShaderModule(vk.device,module,NULL);
    if(result!=VK_SUCCESS)goto fail;
    LOG("surface_projection: Vulkan stereo panorama renderer ready (%ux%u per eye)",p->size,p->size);return 1;
fail:;
    static int failures;
    if(failures++<5)LOG("surface_projection: setup failed at %s; retaining original layer behavior",stage);
    surf_projection_destroy(s);return 0;
}
// Build both eyes at once. The worker carries this exact pose/space snapshot
// with its finished image rather than labelling an old image with a newer pose.
static int surf_video_request(XrSession session,const XrFrameEndInfo *info,
    const XrCompositionLayerBaseHeader *layer,surf_swapchain *s,surf_projection *p) {
    emul_common common=emul_common_of(layer);
    pthread_mutex_lock(&surf_lock);
    int same=s->video_job.seq && s->video_job.time==info->displayTime && s->video_job.space==common.space;
    pthread_mutex_unlock(&surf_lock);
    if(same)return 1;
    surf_video_job job={.time=info->displayTime,.space=common.space,.flags=layer->layerFlags,.size=p->size};
    job.views[0].type=job.views[1].type=XR_TYPE_VIEW;
    int source=surf_views_select(session,info,common.space,
        (PFN_xrLocateSpace)lookup(active_instance,"xrLocateSpace"),job.views);
    if(!source){
        PFN_xrLocateViews locate=(PFN_xrLocateViews)lookup(active_instance,"xrLocateViews");
        XrViewLocateInfo li={XR_TYPE_VIEW_LOCATE_INFO,NULL,XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO,info->displayTime,common.space};
        XrViewState state={XR_TYPE_VIEW_STATE};uint32_t count=0;
        if(!locate || XR_FAILED(locate(session,&li,&state,2,&count,job.views)) || count!=2 ||
            !(state.viewStateFlags&XR_VIEW_STATE_ORIENTATION_VALID_BIT))return 0;
        source=3;
    }
    if(p->view_source!=source){
        LOG("surface_projection: views from %s",source==1?"submitted projection":source==2?"application locate":"runtime locate");
        p->view_source=source;
    }
    for(int eye=0;eye<2;eye++){
        const XrCompositionLayerBaseHeader *selected=layer;
        for(uint32_t i=0;i<info->layerCount;i++)if(emul_is_equirect(info->layers[i])){
            emul_common c=emul_common_of(info->layers[i]);
            if(c.sub.swapchain==s->handle && c.space==common.space && (c.eye==XR_EYE_VISIBILITY_BOTH ||
                c.eye==(eye?XR_EYE_VISIBILITY_RIGHT:XR_EYE_VISIBILITY_LEFT))){selected=info->layers[i];break;}
        }
        emul_common c=emul_common_of(selected);XrView *view=&job.views[eye];
        job.panorama[eye]=c.pose;
        view->fov=surf_video_guard_fov(view->fov);
        XrQuaternionf rotation=lm_qmul(lm_qconj(c.pose.orientation),view->pose.orientation);
        XrVector3f origin=lm_rotate(lm_qconj(c.pose.orientation),(XrVector3f){view->pose.position.x-c.pose.position.x,
            view->pose.position.y-c.pose.position.y,view->pose.position.z-c.pose.position.z});
        float params[32]={rotation.x,rotation.y,rotation.z,rotation.w,origin.x,origin.y,origin.z,0,
            tanf(view->fov.angleLeft),tanf(view->fov.angleRight),tanf(view->fov.angleDown),tanf(view->fov.angleUp),
            c.sub.imageRect.offset.x/(float)s->width,c.sub.imageRect.offset.y/(float)s->height,
            c.sub.imageRect.extent.width/(float)s->width,c.sub.imageRect.extent.height/(float)s->height,
            0,0,0,(float)eye,1,1,0,0,1,1,1,1,0,0,0,0};
        if(selected->type==XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR){
            const XrCompositionLayerEquirect2KHR *e=(const void*)selected;
            params[7]=e->radius;params[16]=e->centralHorizontalAngle;params[17]=e->upperVerticalAngle;params[18]=e->lowerVerticalAngle;
        }else{
            const XrCompositionLayerEquirectKHR *e=(const void*)selected;
            params[7]=e->radius;params[20]=e->scale.x;params[21]=e->scale.y;params[22]=e->bias.x;params[23]=e->bias.y;
        }
        for(const XrBaseInStructure *n=selected->next;n;n=n->next)if(n->type==XR_TYPE_COMPOSITION_LAYER_COLOR_SCALE_BIAS_KHR){
            const XrCompositionLayerColorScaleBiasKHR *color=(const void*)n;
            memcpy(params+24,&color->colorScale,16);memcpy(params+28,&color->colorBias,16);
        }
        memcpy(job.params[eye],params,sizeof(params));
    }
    pthread_mutex_lock(&surf_lock);job.seq=s->video_job.seq+1;s->video_job=job;
    pthread_cond_broadcast(&surf_cond);pthread_mutex_unlock(&surf_lock);
    return 1;
}
static const XrCompositionLayerBaseHeader *surf_video_layer(surf_projection *p,const surf_video_job *current) {
    if(!p->ready)return NULL;
    int occludes=surf_video_occludes(&p->presented_job,current);
    if(occludes!=p->occludes && !surf_composite_active(p))LOG("surface_projection: opaque primary=%d (fades and uncovered views retain lower layers)",occludes);
    p->occludes=occludes;
    for(int eye=0;eye<2;eye++)p->eyes[eye].pose=occludes?
        surf_video_primary_pose(&p->presented_job,current,eye):surf_video_pose(&p->presented_job,current,eye);
    if(current->time-p->tracking_log_time>=5000000000ll){
        XrQuaternionf correction=lm_qmul(lm_qconj(p->eyes[0].pose.orientation),current->views[0].pose.orientation);
        XrQuaternionf layer_delta=lm_qmul(lm_qconj(p->presented_job.panorama[0].orientation),current->panorama[0].orientation);
        LOG("surface_projection: render age=%.2f ms, head correction=%.2f deg, layer rotation=%.2f deg, space=%p",
            (current->time-p->presented_job.time)/1e6,lm_angle(correction)*180/LM_PI,lm_angle(layer_delta)*180/LM_PI,
            (void*)(uintptr_t)current->space);
        p->tracking_log_time=current->time;
    }
    return (const void*)&p->layer;
}
static int surf_projection_occludes(const XrCompositionLayerBaseHeader *layer) {
    for(int i=0;i<SURF_MAX;i++)if(layer==(const void*)&surf_projections[i].layer)
        return surfs[i].native_project && surf_projections[i].ready && surf_projections[i].occludes;
    return 0;
}
static const XrCompositionLayerBaseHeader *surf_video_present(XrSession session,const XrFrameEndInfo *info,
    const XrCompositionLayerBaseHeader *layer,surf_swapchain *s,surf_projection *p) {
    if(!surf_projection_setup(session,s,p,s->width,s->height) || !surf_video_request(session,info,layer,s,p))return NULL;
    pthread_mutex_lock(&surf_lock);surf_video_job current=s->video_job;pthread_mutex_unlock(&surf_lock);
    // Request tracking updates even when our previous copy is still queued.
    if(s->in_flight){
        if(svk.GetFenceStatus(vk.device,s->fence)!=VK_SUCCESS)return surf_video_layer(p,&current);
        vk.ResetFences(vk.device,1,&s->fence);s->in_flight=0;surf_native_complete(s);
    }
    // A worker can publish between the two eye calls; present the pair only once.
    if(p->ready && p->presented_time==info->displayTime)return surf_video_layer(p,&current);
    int slot=-1;
    pthread_mutex_lock(&surf_lock);
    for(int i=0;i<SURF_NATIVE_SLOTS;i++)if(s->native_slots[i].state==2){slot=i;s->native_slots[i].state=3;break;}
    pthread_mutex_unlock(&surf_lock);
    if(slot<0)return surf_video_layer(p,&current);
    surf_native_slot *native=&s->native_slots[slot];
    if(!native->projected || native->width!=2*p->size || native->height!=p->size)goto unused;
    PFN_xrAcquireSwapchainImage acquire=(PFN_xrAcquireSwapchainImage)lookup(active_instance,"xrAcquireSwapchainImage");
    PFN_xrWaitSwapchainImage wait=(PFN_xrWaitSwapchainImage)lookup(active_instance,"xrWaitSwapchainImage");
    PFN_xrReleaseSwapchainImage release=(PFN_xrReleaseSwapchainImage)lookup(active_instance,"xrReleaseSwapchainImage");
    int compose=surf_composite_candidate(info,layer);
    int cached_only=compose && surf_composite_cached_only(info,p,&native->video,&current);
    XrSwapchainImageAcquireInfo ai={XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO};
    XrSwapchainImageWaitInfo wi={XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO,NULL,0};
    XrSwapchainImageReleaseInfo ri={XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO};uint32_t index;
    if(!acquire || !wait || !release)goto unused;
    if(!cached_only && !p->acquired){
        if(XR_FAILED(acquire(p->output,&ai,&p->acquired_index)))goto unused;
        p->acquired=1;
    }
    index=p->acquired_index;
    XrResult waited=cached_only?XR_SUCCESS:wait(p->output,&wi);
    // Timeout is a positive XrResult, but the image is not yet writable. Keep
    // it acquired and retry next frame rather than blocking the render thread.
    if(waited!=XR_SUCCESS)goto unused;
    if(!cached_only && index>=p->count){release(p->output,&ri);p->acquired=0;goto unused;}
    VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,NULL,VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    vk.ResetCommandBuffer(s->cmd,0);vk.BeginCommandBuffer(s->cmd,&begin);
    surf_native_ownership(s->cmd,native,1);
    if(!cached_only)surf_barrier(s->cmd,p->images[index],VK_IMAGE_LAYOUT_UNDEFINED,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,0,VK_ACCESS_TRANSFER_WRITE_BIT);
    VkImageCopy copy={{VK_IMAGE_ASPECT_COLOR_BIT,0,0,1},{0,0,0},{VK_IMAGE_ASPECT_COLOR_BIT,0,0,1},{0,0,0},{2*p->size,p->size,1}};
    if(!cached_only)spvk.CmdCopyImage(s->cmd,native->image,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,p->images[index],VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,1,&copy);
    if(compose){
        surf_barrier(s->cmd,p->video_cache,p->cache_ready?VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL:VK_IMAGE_LAYOUT_UNDEFINED,
        VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,p->cache_ready?VK_ACCESS_SHADER_READ_BIT:0,VK_ACCESS_TRANSFER_WRITE_BIT);
    spvk.CmdCopyImage(s->cmd,native->image,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,p->video_cache,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,1,&copy);
    surf_barrier(s->cmd,p->video_cache,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
        VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_SHADER_READ_BIT);
    }
    surf_native_ownership(s->cmd,native,0);
    if(!cached_only)surf_barrier(s->cmd,p->images[index],VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
        VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_COLOR_ATTACHMENT_READ_BIT|VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT);
    vk.EndCommandBuffer(s->cmd);
    VkSubmitInfo submit={VK_STRUCTURE_TYPE_SUBMIT_INFO,NULL,0,NULL,NULL,1,&s->cmd};
    int ok=vk.QueueSubmit(vk.queue,1,&submit,s->fence)==VK_SUCCESS;
    if(!cached_only){release(p->output,&ri);p->acquired=0;}if(!ok)goto unused;
    s->native_pending=slot;s->in_flight=1;s->uploads++;if(compose){p->cache_ready=1;p->cached_job=native->video;}
    p->width=native->source_width;p->height=native->source_height;p->source_seq=native->seq;p->source_ready=1;
    for(int eye=0;eye<2;eye++)p->eyes[eye]=(XrCompositionLayerProjectionView){XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW,NULL,
        native->video.views[eye].pose,native->video.views[eye].fov,
        {p->output,{{eye*(int32_t)p->size,0},{(int32_t)p->size,(int32_t)p->size}},0}};
    p->layer=(XrCompositionLayerProjection){XR_TYPE_COMPOSITION_LAYER_PROJECTION,NULL,native->video.flags,native->video.space,2,p->eyes};
    p->presented_job=native->video;
    p->ready=1;p->presented_time=info->displayTime;return surf_video_layer(p,&current);
unused:
    pthread_mutex_lock(&surf_lock);native->state=native->seq==s->seq?2:0;pthread_mutex_unlock(&surf_lock);
    return surf_video_layer(p,&current);
}
static const XrCompositionLayerBaseHeader *surf_projection_frame(XrSession session,const XrFrameEndInfo *info,
    const XrCompositionLayerBaseHeader *layer) {
    if(!surf_vulkan || !emul_is_equirect(layer))return NULL;
    emul_common common=emul_common_of(layer);
    surf_swapchain *s=surf_find(common.sub.swapchain);
    if(!s)return NULL;
    surf_projection *p=&surf_projections[s-surfs];
    if(s->native_project)return surf_video_present(session,info,layer,s,p);
    if(s->in_flight) {
        if(svk.GetFenceStatus(vk.device,s->fence)!=VK_SUCCESS)return p->ready?(const XrCompositionLayerBaseHeader*)&p->layer:NULL;
        vk.ResetFences(vk.device,1,&s->fence);s->in_flight=0;
        surf_native_complete(s);
    }
    int native_index=-1;
    if(s->native_mode){
        pthread_mutex_lock(&surf_lock);
        for(int i=0;i<SURF_NATIVE_SLOTS;i++)if(s->native_slots[i].state==2){native_index=i;s->native_slots[i].state=3;break;}
        pthread_mutex_unlock(&surf_lock);
        if(native_index<0 && !p->source_ready)return NULL;
    }
    surf_native_slot *native=native_index>=0?&s->native_slots[native_index]:NULL;
    uint32_t source_width=native?native->width:(s->native_mode?p->width:s->width);
    uint32_t source_height=native?native->height:(s->native_mode?p->height:s->height);
    if(p->pipeline && (p->width!=source_width || p->height!=source_height)){
        if(s->native_mode){p->width=source_width;p->height=source_height;}
        else surf_projection_destroy(s);
    }
    if(!surf_projection_setup(session,s,p,source_width,source_height)){
        if(native){pthread_mutex_lock(&surf_lock);native->state=native->seq==s->seq?2:0;pthread_mutex_unlock(&surf_lock);}return NULL;
    }
    XrView views[2]={{XR_TYPE_VIEW,NULL},{XR_TYPE_VIEW,NULL}};
    XrViewState state={XR_TYPE_VIEW_STATE,NULL,0};uint32_t count=0;
    XrViewLocateInfo locate={XR_TYPE_VIEW_LOCATE_INFO,NULL,XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO,info->displayTime,common.space};
    PFN_xrLocateViews locate_views=(PFN_xrLocateViews)lookup(active_instance,"xrLocateViews");
    int view_source=surf_views_select(session,info,common.space,
        (PFN_xrLocateSpace)lookup(active_instance,"xrLocateSpace"),views);
    if(!view_source){
        if(!locate_views || XR_FAILED(locate_views(session,&locate,&state,2,&count,views)) || count!=2 ||
            !(state.viewStateFlags&XR_VIEW_STATE_ORIENTATION_VALID_BIT))goto unused;
        view_source=3;
    }
    if(p->view_source!=view_source){
        LOG("surface_projection: views from %s, space=%p, frame time=%lld",view_source==1?"submitted projection":
            view_source==2?"application locate":"runtime locate",(void*)(uintptr_t)common.space,(long long)info->displayTime);
        p->view_source=view_source;
    }
    pthread_mutex_lock(&surf_lock);uint64_t seq=s->native_mode?(native?native->seq:p->source_seq):s->seq;
    if(!s->native_mode && seq!=p->source_seq)memcpy(s->staging_map,s->pixels,(size_t)s->width*s->height*4);
    pthread_mutex_unlock(&surf_lock);
    if(!seq)goto unused;
    PFN_xrAcquireSwapchainImage acquire=(PFN_xrAcquireSwapchainImage)lookup(active_instance,"xrAcquireSwapchainImage");
    PFN_xrWaitSwapchainImage wait=(PFN_xrWaitSwapchainImage)lookup(active_instance,"xrWaitSwapchainImage");
    PFN_xrReleaseSwapchainImage release=(PFN_xrReleaseSwapchainImage)lookup(active_instance,"xrReleaseSwapchainImage");
    XrSwapchainImageAcquireInfo ai={XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO,NULL};
    XrSwapchainImageWaitInfo wi={XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO,NULL,100000000};
    XrSwapchainImageReleaseInfo ri={XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO,NULL};uint32_t index;
    if(!acquire || !wait || !release || XR_FAILED(acquire(p->output,&ai,&index)))goto unused;
    if(index>=p->count || XR_FAILED(wait(p->output,&wi))) {release(p->output,&ri);goto unused;}
    int previous_native=s->native_current;
    surf_native_slot *sample=native?native:(s->native_mode && previous_native>=0?&s->native_slots[previous_native]:NULL);
    VkDescriptorImageInfo descriptors[2]={{VK_NULL_HANDLE,p->rendered_view,VK_IMAGE_LAYOUT_GENERAL},
        {p->sampler,sample?sample->view:p->source_view,VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL}};
    VkWriteDescriptorSet writes[2];
    for(int i=0;i<2;i++)writes[i]=(VkWriteDescriptorSet){VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,NULL,p->set,(uint32_t)i,0,1,
        i?VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER:VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,&descriptors[i],NULL,NULL};
    spvk.UpdateDescriptorSets(vk.device,2,writes,0,NULL);
    VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,NULL,VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT,NULL};
    vk.ResetCommandBuffer(s->cmd,0);vk.BeginCommandBuffer(s->cmd,&begin);
    if(native){
        if(previous_native>=0)surf_native_ownership(s->cmd,&s->native_slots[previous_native],0);
        surf_native_ownership(s->cmd,native,1);
    }else if(!s->native_mode && seq!=p->source_seq) {
        surf_barrier(s->cmd,p->source,p->source_ready?VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL:VK_IMAGE_LAYOUT_UNDEFINED,
            VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,p->source_ready?VK_ACCESS_SHADER_READ_BIT:0,VK_ACCESS_TRANSFER_WRITE_BIT);
        VkBufferImageCopy copy={0,0,0,{VK_IMAGE_ASPECT_COLOR_BIT,0,0,1},{0,0,0},{s->width,s->height,1}};
        svk.CmdCopyBufferToImage(s->cmd,s->staging,p->source,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,1,&copy);
        surf_barrier(s->cmd,p->source,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
            VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_SHADER_READ_BIT);
    }
    surf_barrier(s->cmd,p->rendered,VK_IMAGE_LAYOUT_UNDEFINED,VK_IMAGE_LAYOUT_GENERAL,0,VK_ACCESS_SHADER_WRITE_BIT);
    spvk.CmdBindPipeline(s->cmd,VK_PIPELINE_BIND_POINT_COMPUTE,p->pipeline);
    spvk.CmdBindDescriptorSets(s->cmd,VK_PIPELINE_BIND_POINT_COMPUTE,p->layout,0,1,&p->set,0,NULL);
    for(int eye=0;eye<2;eye++) {
        const XrCompositionLayerBaseHeader *selected=NULL;
        for(uint32_t i=0;i<info->layerCount;i++)if(emul_is_equirect(info->layers[i])) {
            emul_common c=emul_common_of(info->layers[i]);
            if(c.sub.swapchain==s->handle && c.space==common.space && (c.eye==XR_EYE_VISIBILITY_BOTH ||
                c.eye==(eye?XR_EYE_VISIBILITY_RIGHT:XR_EYE_VISIBILITY_LEFT))){selected=info->layers[i];break;}
        }
        if(!selected)selected=layer;
        emul_common c=emul_common_of(selected);
        XrQuaternionf rotation=lm_qmul(lm_qconj(c.pose.orientation),views[eye].pose.orientation);
        XrVector3f origin=lm_rotate(lm_qconj(c.pose.orientation),(XrVector3f){views[eye].pose.position.x-c.pose.position.x,
            views[eye].pose.position.y-c.pose.position.y,views[eye].pose.position.z-c.pose.position.z});
        float params[32]={rotation.x,rotation.y,rotation.z,rotation.w,origin.x,origin.y,origin.z,0,
            tanf(views[eye].fov.angleLeft),tanf(views[eye].fov.angleRight),tanf(views[eye].fov.angleDown),tanf(views[eye].fov.angleUp),
            c.sub.imageRect.offset.x/(float)s->width,c.sub.imageRect.offset.y/(float)s->height,
            c.sub.imageRect.extent.width/(float)s->width,c.sub.imageRect.extent.height/(float)s->height,
            0,0,0,(float)eye,1,1,0,0,1,1,1,1,0,0,0,0};
        if(selected->type==XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR) {
            const XrCompositionLayerEquirect2KHR *e=(const void*)selected;
            params[7]=e->radius;params[16]=e->centralHorizontalAngle;params[17]=e->upperVerticalAngle;params[18]=e->lowerVerticalAngle;
        } else {
            const XrCompositionLayerEquirectKHR *e=(const void*)selected;
            params[7]=e->radius;params[20]=e->scale.x;params[21]=e->scale.y;params[22]=e->bias.x;params[23]=e->bias.y;
        }
        for(const XrBaseInStructure *n=selected->next;n;n=n->next)if(n->type==XR_TYPE_COMPOSITION_LAYER_COLOR_SCALE_BIAS_KHR) {
            const XrCompositionLayerColorScaleBiasKHR *color=(const void*)n;
            memcpy(params+24,&color->colorScale,16);memcpy(params+28,&color->colorBias,16);
        }
        spvk.CmdPushConstants(s->cmd,p->layout,VK_SHADER_STAGE_COMPUTE_BIT,0,sizeof(params),params);
        spvk.CmdDispatch(s->cmd,(p->size+15)/16,(p->size+15)/16,1);
        p->eyes[eye]=(XrCompositionLayerProjectionView){XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW,NULL,
            views[eye].pose,views[eye].fov,{p->output,{{eye*(int32_t)p->size,0},{(int32_t)p->size,(int32_t)p->size}},0}};
    }
    surf_barrier(s->cmd,p->rendered,VK_IMAGE_LAYOUT_GENERAL,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
        VK_ACCESS_SHADER_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT);
    surf_barrier(s->cmd,p->images[index],VK_IMAGE_LAYOUT_UNDEFINED,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
        0,VK_ACCESS_TRANSFER_WRITE_BIT);
    VkImageCopy rendered_copy={{VK_IMAGE_ASPECT_COLOR_BIT,0,0,1},{0,0,0},
        {VK_IMAGE_ASPECT_COLOR_BIT,0,0,1},{0,0,0},{2*p->size,p->size,1}};
    spvk.CmdCopyImage(s->cmd,p->rendered,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
        p->images[index],VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,1,&rendered_copy);
    surf_barrier(s->cmd,p->images[index],VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
        VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_COLOR_ATTACHMENT_READ_BIT|VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT);
    vk.EndCommandBuffer(s->cmd);
    VkSubmitInfo submit={VK_STRUCTURE_TYPE_SUBMIT_INFO,NULL,0,NULL,NULL,1,&s->cmd,0,NULL};
    int ok=vk.QueueSubmit(vk.queue,1,&submit,s->fence)==VK_SUCCESS;
    release(p->output,&ri);
    if(!ok)goto unused;
    if(native){s->native_pending=previous_native;s->native_current=native_index;}
    s->in_flight=1;p->source_ready=1;p->source_seq=seq;s->uploads++;
    p->layer=(XrCompositionLayerProjection){XR_TYPE_COMPOSITION_LAYER_PROJECTION,NULL,layer->layerFlags,common.space,2,p->eyes};
    p->ready=1;
    return (const XrCompositionLayerBaseHeader*)&p->layer;
unused:
    if(native){pthread_mutex_lock(&surf_lock);native->state=native->seq==s->seq?2:0;pthread_mutex_unlock(&surf_lock);}
    return NULL;
}

#include "surface_composite.c"
