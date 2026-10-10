// Included by surface_native_test.c; executes the production compositor shader.
static void fixture_upload_image_pattern(surf_swapchain *s,VkImage image,uint32_t width,uint32_t height,uint32_t layers,
    VkImageLayout old,VkImageLayout final,const unsigned char colors[2][4],int rows) {
    unsigned char *pixels=s->staging_map;
    for(uint32_t layer=0;layer<layers;layer++)for(uint32_t y=0;y<height;y++)for(uint32_t x=0;x<width;x++)
        memcpy(pixels+4*((size_t)layer*width*height+y*width+x),colors[rows?(y>=height/2):(layers>1?layer:(x>=width/2))],4);
    VK_OK(vkResetCommandBuffer(s->cmd,0));
    VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};VK_OK(vkBeginCommandBuffer(s->cmd,&begin));
    sc_barrier(s->cmd,image,layers,old,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
        old==VK_IMAGE_LAYOUT_UNDEFINED?0:VK_ACCESS_MEMORY_READ_BIT|VK_ACCESS_MEMORY_WRITE_BIT,VK_ACCESS_TRANSFER_WRITE_BIT);
    VkBufferImageCopy copy={0,0,0,{VK_IMAGE_ASPECT_COLOR_BIT,0,0,layers},{0,0,0},{width,height,1}};
    vkCmdCopyBufferToImage(s->cmd,s->staging,image,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,1,&copy);
    sc_barrier(s->cmd,image,layers,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,final,VK_ACCESS_TRANSFER_WRITE_BIT,
        final==VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL?VK_ACCESS_SHADER_READ_BIT:VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT);
    VK_OK(vkEndCommandBuffer(s->cmd));VkSubmitInfo submit={VK_STRUCTURE_TYPE_SUBMIT_INFO,NULL,0,NULL,NULL,1,&s->cmd};
    VK_OK(vkQueueSubmit(vk.queue,1,&submit,VK_NULL_HANDLE));VK_OK(vkQueueWaitIdle(vk.queue));
}
static void fixture_upload_image(surf_swapchain *s,VkImage image,uint32_t width,uint32_t height,uint32_t layers,
    VkImageLayout old,VkImageLayout final,const unsigned char colors[2][4]) {
    fixture_upload_image_pattern(s,image,width,height,layers,old,final,colors,0);
}
static void fixture_composite_pixels(surf_swapchain *s,surf_composite *c,uint32_t size) {
    VK_OK(vkResetCommandBuffer(s->cmd,0));
    VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};VK_OK(vkBeginCommandBuffer(s->cmd,&begin));
    sc_barrier(s->cmd,c->images[c->index],1,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
        VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT);
    VkBufferImageCopy copies[2];
    for(int e=0;e<2;e++)copies[e]=(VkBufferImageCopy){(VkDeviceSize)(4*e),0,0,{VK_IMAGE_ASPECT_COLOR_BIT,0,0,1},
        {e*(int32_t)size+(int32_t)size/2,(int32_t)size/4,0},{1,1,1}};
    vkCmdCopyImageToBuffer(s->cmd,c->images[c->index],VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,s->staging,2,copies);
    sc_barrier(s->cmd,c->images[c->index],1,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
        VK_ACCESS_TRANSFER_READ_BIT,VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT);
    VK_OK(vkEndCommandBuffer(s->cmd));VkSubmitInfo submit={VK_STRUCTURE_TYPE_SUBMIT_INFO,NULL,0,NULL,NULL,1,&s->cmd};
    VK_OK(vkQueueSubmit(vk.queue,1,&submit,VK_NULL_HANDLE));VK_OK(vkQueueWaitIdle(vk.queue));
}
static void test_composite(surf_swapchain *base) {
    surf_swapchain *s=&surfs[1];memset(s,0,sizeof(*s));
    s->staging=base->staging;s->staging_map=base->staging_map;s->cmd=base->cmd;s->fence=base->fence;
    s->native_project=1;s->native_mode=1;s->native_current=s->native_pending=-1;s->width=s->height=64;
    surf_projection *p=&surf_projections[1];
    equirect_res_set=1;equirect_res=2560;
    assert(surf_projection_setup((XrSession)(uintptr_t)1,s,p,64,64));
    tracked_swapchain game={.info={XR_TYPE_SWAPCHAIN_CREATE_INFO,NULL,0,
        XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT|XR_SWAPCHAIN_USAGE_TRANSFER_SRC_BIT,
        VK_FORMAT_R8G8B8A8_SRGB,1,64,64,1,2,1},.image_count=1};
    assert(fake_create((XrSession)(uintptr_t)1,&game.info,&game.handle)==XR_SUCCESS);
    game.images[0]=runtime_images[fixture_image_slot(game.handle)].image;
    const unsigned char scene_colors[2][4]={{255,0,0,255},{0,0,255,255}};
    fixture_upload_image(s,game.images[0],64,64,2,VK_IMAGE_LAYOUT_UNDEFINED,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,scene_colors);
    surf_video_job job={.seq=1,.space=(XrSpace)(uintptr_t)1,.flags=7,.size=p->size};
    for(int e=0;e<2;e++) {
        job.views[e]=(XrView){.type=XR_TYPE_VIEW,.pose={{0,0,0,1},{e?.03f:-.03f,0,0}},
            .fov={atanf(-SURF_VIDEO_GUARD),atanf(SURF_VIDEO_GUARD),atanf(SURF_VIDEO_GUARD),atanf(-SURF_VIDEO_GUARD)}};
        job.panorama[e].orientation.w=1;job.params[e][3]=1;
        job.params[e][8]=job.params[e][10]=-SURF_VIDEO_GUARD;job.params[e][9]=job.params[e][11]=SURF_VIDEO_GUARD;
        job.params[e][12]=e*.5f;job.params[e][14]=.5f;job.params[e][15]=1;
        job.params[e][16]=LM_PI;job.params[e][17]=LM_PI*.5f;job.params[e][18]=-LM_PI*.5f;
        job.params[e][27]=1;
    }
    p->presented_job=job;p->cached_job=job;p->layer=(XrCompositionLayerProjection){.type=XR_TYPE_COMPOSITION_LAYER_PROJECTION};
    p->ready=p->cache_ready=1;s->video_job=job;
    XrCompositionLayerProjectionView views[2];
    for(int e=0;e<2;e++)views[e]=(XrCompositionLayerProjectionView){.type=XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW,
        .pose=job.views[e].pose,.fov={-LM_PI*.25f,LM_PI*.25f,LM_PI*.25f,-LM_PI*.25f},
        .subImage={game.handle,{{0,0},{64,64}},e}};
    XrCompositionLayerProjection scene={.type=XR_TYPE_COMPOSITION_LAYER_PROJECTION,.space=job.space,.viewCount=2,.views=views};
    XrCompositionLayerImageLayoutFB layout={XR_TYPE_COMPOSITION_LAYER_IMAGE_LAYOUT_FB,NULL,0};
    XrCompositionLayerColorScaleBiasKHR color={XR_TYPE_COMPOSITION_LAYER_COLOR_SCALE_BIAS_KHR,&layout,{1,1,1,1},{0,0,0,0}};
    scene.next=&color;
    XrCompositionLayerEquirect2KHR movie={.type=XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR,.space=job.space};
    const XrCompositionLayerBaseHeader *layers[]={(const void*)&scene,(const void*)&movie};
    XrFrameEndInfo frame={.type=XR_TYPE_FRAME_END_INFO,.displayTime=1,.layerCount=2,.layers=layers};
    assert(sizeof(surf_composite_params)==128);
    assert(surf_composite_candidate(&frame,(const void*)&movie));
    XrBaseInStructure unknown={XR_TYPE_COMPOSITION_LAYER_DEPTH_INFO_KHR,NULL};
    layout.next=&unknown;assert(!surf_composite_candidate(&frame,(const void*)&movie));layout.next=NULL;
    views[0].next=&unknown;assert(!surf_composite_candidate(&frame,(const void*)&movie));views[0].next=NULL;
    layout.flags=2;assert(!surf_composite_candidate(&frame,(const void*)&movie));layout.flags=0;
    assert(surf_composite_candidate(&frame,(const void*)&movie));
    surf_composite *c=&sc_outputs[1];
    const unsigned char video_colors[][2][4]={{{0,255,0,255},{255,255,0,255}},
        {{0,255,0,128},{255,255,0,128}},{{0,0,0,0},{0,0,0,0}},{{0,255,0,255},{255,255,0,255}}};
    const unsigned char expected[][2][3]={{{0,255,0},{255,255,0}},{{187,188,0},{188,188,187}},
        {{255,0,0},{0,0,255}},{{255,0,0},{0,0,255}}};
    for(int phase=0;phase<4;phase++) {
        fixture_upload_image(s,p->video_cache,2*p->size,p->size,1,phase?VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL:VK_IMAGE_LAYOUT_UNDEFINED,
            VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,video_colors[phase]);
        s->video_job=job;
        if(phase==3)for(int e=0;e<2;e++) {
            s->video_job.views[e].pose.orientation=lm_axis_angle(0,1,0,1.2f);
            memcpy(s->video_job.params[e],&s->video_job.views[e].pose.orientation,16);
            views[e].pose=s->video_job.views[e].pose;
        }
        sc_capture_until=monotonic_ns()+1000000000ll;surf_scene_capture(&game,0);surf_scene_release_commit(game.handle);++sc_serial;
        const XrCompositionLayerBaseHeader *out=surf_composite_frame((XrSession)(uintptr_t)1,&frame,&scene,(const void*)&p->layer);
        assert(out==(const void*)&c->layer && c->ready && c->frames==(uint64_t)phase+1);
        VK_OK(vkQueueWaitIdle(vk.queue));fixture_composite_pixels(s,c,p->size);
        const unsigned char *pixels=s->staging_map;
        for(int e=0;e<2;e++)for(int channel=0;channel<3;channel++)
            if(abs(pixels[4*e+channel]-expected[phase][e][channel])>2) {
                printf("FAIL: composition phase %d eye %d channel %d: %d != %d\n",phase,e,channel,pixels[4*e+channel],expected[phase][e][channel]);assert(0);
            }
        assert(pixels[3]==255 && pixels[7]==255);
    }
    puts("PASS: production Vulkan composition: stereo arrays, opaque video, linear-light fade, transparent hemisphere and uncovered view.");
    // An unavailable fresh scene must retain the exact old combined camera,
    // rather than returning to projected overlays or relabelling old pixels.
    XrPosef old=c->eyes[0].pose;surf_scene_release_commit(game.handle);++sc_serial;
    assert(surf_composite_frame((XrSession)(uintptr_t)1,&frame,&scene,(const void*)&p->layer)==(const void*)&c->layer);
    assert(!memcmp(&old,&c->eyes[0].pose,sizeof(old)) && c->holds==1);
    wait_timeouts=1;sc_capture_until=monotonic_ns()+1000000000ll;surf_scene_capture(&game,0);surf_scene_release_commit(game.handle);++sc_serial;
    assert(surf_composite_frame((XrSession)(uintptr_t)1,&frame,&scene,(const void*)&p->layer)==(const void*)&c->layer && c->acquired);
    assert(surf_composite_frame((XrSession)(uintptr_t)1,&frame,&scene,(const void*)&p->layer)==(const void*)&c->layer && !c->acquired);
    VK_OK(vkQueueWaitIdle(vk.queue));
    double total=0;
    for(int i=0;i<120;i++) {
        for(int e=0;e<2;e++) {
            s->video_job.views[e].pose.orientation=lm_axis_angle(0,1,0,.9f*sinf(i*.1f));
            memcpy(s->video_job.params[e],&s->video_job.views[e].pose.orientation,16);views[e].pose=s->video_job.views[e].pose;
        }
        sc_capture_until=monotonic_ns()+1000000000ll;surf_scene_capture(&game,0);surf_scene_release_commit(game.handle);++sc_serial;
        int64_t start=monotonic_ns();
        assert(surf_composite_frame((XrSession)(uintptr_t)1,&frame,&scene,(const void*)&p->layer)==(const void*)&c->layer);
        VK_OK(vkQueueWaitIdle(vk.queue));total+=(monotonic_ns()-start)/1e6;
    }
    assert(c->frames==125);
    printf("PASS: 120 moving-head composition frames retained a single primary projection; GPU submission+finish %.2f ms/frame.\n",total/120);
    // Exercise Batman's real chained scene settings rather than a bare layer.
    fixture_upload_image(s,p->video_cache,2*p->size,p->size,1,VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
        VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,video_colors[2]);
    s->video_job=job;for(int e=0;e<2;e++)views[e].pose=job.views[e].pose;
    fixture_upload_image_pattern(s,game.images[0],64,64,2,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
        VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,scene_colors,1);
    for(int flip=0;flip<2;flip++) {
        layout.flags=flip?XR_COMPOSITION_LAYER_IMAGE_LAYOUT_VERTICAL_FLIP_BIT_FB:0;
        sc_capture_until=monotonic_ns()+1000000000ll;surf_scene_capture(&game,0);surf_scene_release_commit(game.handle);
        assert(surf_composite_frame((XrSession)(uintptr_t)1,&frame,&scene,(const void*)&p->layer)==(const void*)&c->layer);
        fixture_composite_pixels(s,c,p->size);
        const unsigned char *pixels=s->staging_map;
        for(int e=0;e<2;e++)for(int k=0;k<3;k++)assert(abs(pixels[e*4+k]-scene_colors[flip][k])<=2);
    }
    color.colorScale=(XrColor4f){.25f,.5f,.75f,.5f};color.colorBias=(XrColor4f){.25f,.25f,.25f,.25f};
    sc_capture_until=monotonic_ns()+1000000000ll;surf_scene_capture(&game,0);surf_scene_release_commit(game.handle);
    assert(surf_composite_frame((XrSession)(uintptr_t)1,&frame,&scene,(const void*)&p->layer)==(const void*)&c->layer);
    fixture_composite_pixels(s,c,p->size);
    const unsigned char *transformed=s->staging_map;
    for(int e=0;e<2;e++)for(int k=0;k<3;k++) {
        float linear=(k==2?1.f:.25f)*.75f;
        int expected=(int)lroundf(255*(1.055f*powf(linear,1.f/2.4f)-.055f));
        assert(abs(transformed[e*4+k]-expected)<=2);
    }
    assert(abs(transformed[3]-191)<=1 && abs(transformed[7]-191)<=1);
    puts("PASS: Batman scene colour/layout chain, vertical orientation, RGB/alpha scale and bias; unsupported chains retain fallback.");
    surf_scene_destroy(game.handle);fake_destroy(game.handle);surf_projection_destroy(s);
    assert(!c->ready && !c->output);memset(s,0,sizeof(*s));
    puts("PASS: composition timeout retry, held-camera metadata and GPU-reader teardown.");
}
