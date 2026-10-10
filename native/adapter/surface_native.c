// SPDX-License-Identifier: GPL-3.0-only
// Decoder -> AImageReader -> fused GLES YUV conversion/projection -> shared RGBA
// eye views -> Vulkan copy to the runtime. The original decoded image is not
// downscaled or converted into a full-size intermediate. No picture readback or
// CPU colour conversion; no YCbCr feature forced onto the application's device.
// Published buffers are retained until the Vulkan fence completes. The full
// panorama import path remains available to the independent reference fixture.
static struct {
    media_status_t (*reader_new)(int32_t,int32_t,int32_t,uint64_t,int32_t,AImageReader**);
    media_status_t (*reader_window)(AImageReader*,ANativeWindow**);
    media_status_t (*reader_latest)(AImageReader*,AImage**);
    media_status_t (*reader_next)(AImageReader*,AImage**);
    media_status_t (*image_time)(const AImage*,int64_t*);
    void (*reader_delete)(AImageReader*);
    media_status_t (*image_buffer)(const AImage*,AHardwareBuffer**);
    void (*image_delete)(AImage*);
    int (*allocate)(const AHardwareBuffer_Desc*,AHardwareBuffer**);
    void (*describe)(const AHardwareBuffer*,AHardwareBuffer_Desc*);
    void (*release)(AHardwareBuffer*);
    jobject (*to_surface)(JNIEnv*,ANativeWindow*);
    PFNEGLGETNATIVECLIENTBUFFERANDROIDPROC client;
    PFNEGLCREATEIMAGEKHRPROC create;
    PFNEGLDESTROYIMAGEKHRPROC destroy;
    PFNGLEGLIMAGETARGETTEXTURE2DOESPROC bind;
    void (*finish)(void);
    PFN_vkGetAndroidHardwareBufferPropertiesANDROID properties;
    PFN_vkCreateImage create_image;
    PFN_vkDestroyImage destroy_image;
    PFN_vkAllocateMemory allocate_memory;
    PFN_vkFreeMemory free_memory;
    PFN_vkBindImageMemory bind_memory;
    PFN_vkCreateImageView create_view;
    PFN_vkDestroyImageView destroy_view;
} sn;
static int surf_native_load(void) {
    if(!vk.GetDeviceProcAddr)return 0;
    sn.properties=(PFN_vkGetAndroidHardwareBufferPropertiesANDROID)vk.GetDeviceProcAddr(vk.device,
        "vkGetAndroidHardwareBufferPropertiesANDROID");
    if(!sn.properties){LOG("surface_native: external-memory device extension unavailable");return 0;}
    void *media=dlopen("libmediandk.so",RTLD_NOW|RTLD_LOCAL);
    void *window=dlopen("libnativewindow.so",RTLD_NOW|RTLD_LOCAL);
    void *gles=dlopen("libGLESv3.so",RTLD_NOW|RTLD_LOCAL);
    if(!media || !window || !gles){LOG("surface_native: missing platform library: %s",dlerror());return 0;}
#define SN_SYM(field,lib,name) *(void**)&sn.field=dlsym(lib,name);if(!sn.field){LOG("surface_native: missing %s",name);return 0;}
    SN_SYM(reader_new,media,"AImageReader_newWithUsage")
    SN_SYM(reader_window,media,"AImageReader_getWindow")
    SN_SYM(reader_latest,media,"AImageReader_acquireLatestImage")
    SN_SYM(reader_next,media,"AImageReader_acquireNextImage")
    SN_SYM(image_time,media,"AImage_getTimestamp")
    SN_SYM(reader_delete,media,"AImageReader_delete")
    SN_SYM(image_buffer,media,"AImage_getHardwareBuffer") SN_SYM(image_delete,media,"AImage_delete")
    SN_SYM(allocate,window,"AHardwareBuffer_allocate") SN_SYM(describe,window,"AHardwareBuffer_describe")
    SN_SYM(release,window,"AHardwareBuffer_release")
#ifndef SURF_NATIVE_ALLOCATOR_TEST
    // A native GPU fixture has no Java VM; only the application needs this bridge.
    void *android=dlopen("libandroid.so",RTLD_NOW|RTLD_LOCAL);
    if(!android)return 0;
    SN_SYM(to_surface,android,"ANativeWindow_toSurface")
#endif
    SN_SYM(finish,gles,"glFinish")
#undef SN_SYM
#define SN_VK(field,name) sn.field=(PFN_vk##name)vk.GetDeviceProcAddr(vk.device,"vk" #name);if(!sn.field)return 0;
    SN_VK(create_image,CreateImage) SN_VK(destroy_image,DestroyImage) SN_VK(allocate_memory,AllocateMemory)
    SN_VK(free_memory,FreeMemory) SN_VK(bind_memory,BindImageMemory)
    SN_VK(create_view,CreateImageView) SN_VK(destroy_view,DestroyImageView)
#undef SN_VK
    void *egl=dlopen("libEGL.so",RTLD_NOW|RTLD_LOCAL);
    __eglMustCastToProperFunctionPointerType (*get)(const char*)=egl?
        (__eglMustCastToProperFunctionPointerType (*)(const char*))dlsym(egl,"eglGetProcAddress"):NULL;
    if(!get)return 0;
    sn.client=(PFNEGLGETNATIVECLIENTBUFFERANDROIDPROC)get("eglGetNativeClientBufferANDROID");
    sn.create=(PFNEGLCREATEIMAGEKHRPROC)get("eglCreateImageKHR");
    sn.destroy=(PFNEGLDESTROYIMAGEKHRPROC)get("eglDestroyImageKHR");
    sn.bind=(PFNGLEGLIMAGETARGETTEXTURE2DOESPROC)get("glEGLImageTargetTexture2DOES");
    return sn.client && sn.create && sn.destroy && sn.bind;
}
static void surf_native_slot_destroy(surf_native_slot *slot) {
    if(slot->view)sn.destroy_view(vk.device,slot->view,NULL);
    if(slot->image)sn.destroy_image(vk.device,slot->image,NULL);
    if(slot->memory)sn.free_memory(vk.device,slot->memory,NULL);
    if(slot->buffer)sn.release(slot->buffer);
    slot->view=VK_NULL_HANDLE;slot->image=VK_NULL_HANDLE;slot->memory=VK_NULL_HANDLE;slot->buffer=NULL;
}
static int surf_native_allocate(surf_native_slot *slot,EGLDisplay display,uint32_t width,uint32_t height) {
    AHardwareBuffer_Desc desc={.width=width,.height=height,.layers=1,
        .format=AHARDWAREBUFFER_FORMAT_R8G8B8A8_UNORM,
        .usage=AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE|AHARDWAREBUFFER_USAGE_GPU_COLOR_OUTPUT};
    if(sn.allocate(&desc,&slot->buffer))return 0;
    VkAndroidHardwareBufferFormatPropertiesANDROID format={VK_STRUCTURE_TYPE_ANDROID_HARDWARE_BUFFER_FORMAT_PROPERTIES_ANDROID};
    VkAndroidHardwareBufferPropertiesANDROID props={VK_STRUCTURE_TYPE_ANDROID_HARDWARE_BUFFER_PROPERTIES_ANDROID,&format};
    if(sn.properties(vk.device,slot->buffer,&props)!=VK_SUCCESS || format.format!=VK_FORMAT_R8G8B8A8_UNORM)goto fail;
    VkExternalMemoryImageCreateInfo external={VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,NULL,
        VK_EXTERNAL_MEMORY_HANDLE_TYPE_ANDROID_HARDWARE_BUFFER_BIT_ANDROID};
    VkImageCreateInfo image={VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,&external,0,VK_IMAGE_TYPE_2D,format.format,
        {width,height,1},1,1,VK_SAMPLE_COUNT_1_BIT,VK_IMAGE_TILING_OPTIMAL,
        VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT|VK_IMAGE_USAGE_TRANSFER_SRC_BIT,
        VK_SHARING_MODE_EXCLUSIVE,0,NULL,VK_IMAGE_LAYOUT_UNDEFINED};
    if(sn.create_image(vk.device,&image,NULL,&slot->image)!=VK_SUCCESS)goto fail;
    uint32_t type=0;while(type<32 && !(props.memoryTypeBits&(1u<<type)))type++;
    if(type==32)goto fail;
    VkImportAndroidHardwareBufferInfoANDROID imported={VK_STRUCTURE_TYPE_IMPORT_ANDROID_HARDWARE_BUFFER_INFO_ANDROID,NULL,slot->buffer};
    VkMemoryDedicatedAllocateInfo dedicated={VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO,&imported,slot->image,VK_NULL_HANDLE};
    VkMemoryAllocateInfo alloc={VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,&dedicated,props.allocationSize,type};
    if(sn.allocate_memory(vk.device,&alloc,NULL,&slot->memory)!=VK_SUCCESS ||
        sn.bind_memory(vk.device,slot->image,slot->memory,0)!=VK_SUCCESS)goto fail;
    VkImageViewCreateInfo view={VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,NULL,0,slot->image,VK_IMAGE_VIEW_TYPE_2D,
        format.format,{0,0,0,0},{VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1}};
    if(sn.create_view(vk.device,&view,NULL,&slot->view)!=VK_SUCCESS)goto fail;
    slot->egl_image=sn.create(display,EGL_NO_CONTEXT,EGL_NATIVE_BUFFER_ANDROID,sn.client(slot->buffer),NULL);
    if(slot->egl_image==EGL_NO_IMAGE_KHR)goto fail;
    p_glGenTextures(1,&slot->texture);p_glBindTexture(GL_TEXTURE_2D,slot->texture);
    sn.bind(GL_TEXTURE_2D,slot->egl_image);
    slot->width=width;slot->height=height;
    return p_glGetError()==GL_NO_ERROR;
fail:
    surf_native_slot_destroy(slot);return 0;
}
// Runs on the surface worker's EGL context. The pose snapshot is published
// together with the completed image, so the compositor can timewarp correctly.
static void surf_video_draw(GLuint program,const GLint uniforms[8],GLuint external,
    const surf_video_job *job) {
    p_glUseProgram(program);p_glActiveTexture(GL_TEXTURE0);p_glBindTexture(GL_TEXTURE_EXTERNAL_OES,external);
    for(int eye=0;eye<2;eye++){
        p_glViewport((GLint)(eye*job->size),0,(GLsizei)job->size,(GLsizei)job->size);
        for(int i=0;i<8;i++){
            const float *v=job->params[eye]+4*i;
            p_glUniform4f(uniforms[i],v[0],v[1],v[2],v[3]);
        }
        p_glDrawArrays(GL_TRIANGLES,0,3);
    }
}
#if !defined(SURF_NATIVE_ALLOCATOR_TEST) || defined(SURF_NATIVE_WORKER_TEST)
static void surf_native_worker(surf_swapchain *s,JNIEnv *env,EGLDisplay display) {
    AImageReader *reader=NULL;ANativeWindow *window=NULL;
    GLuint program=0,external=0,fbo=0,vao=0;
    enum {PENDING_IMAGES=4,ACQUIRED_IMAGES=5};
    AImage *held=NULL,*pending[PENDING_IMAGES]={0};int64_t pending_time[PENDING_IMAGES]={0};int pending_count=0;
    EGLImageKHR input=EGL_NO_IMAGE_KHR;
    AHardwareBuffer_Desc desc={0};uint64_t last_job=0;GLint uniforms[8]={0};
    int64_t source_time=0;
    surf_video_job rendered_job={0};int video_dirty=0;
#ifdef SURF_NATIVE_WORKER_TEST
    // A native fixture supplies the real reader/codec without a Java VM.
    (void)env;reader=surf_worker_test_reader;
#else
    if(sn.reader_new((int32_t)s->width,(int32_t)s->height,AIMAGE_FORMAT_PRIVATE,
        AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE,s->native_project?ACQUIRED_IMAGES:3,&reader)!=AMEDIA_OK ||
        sn.reader_window(reader,&window)!=AMEDIA_OK)goto fail;
    jobject local=sn.to_surface(env,window);
    if(!local || (*env)->ExceptionCheck(env)){(*env)->ExceptionClear(env);goto fail;}
    s->surface=(*env)->NewGlobalRef(env,local);(*env)->DeleteLocalRef(env,local);
    if(!s->surface)goto fail;
#endif
    const char *fragment="#version 300 es\n#extension GL_OES_EGL_image_external_essl3 : require\n"
        "precision highp float;uniform samplerExternalOES tex;in vec2 uv;out vec4 color;"
        "void main(){color=texture(tex,uv);}";
    GLuint vertex=emul_compile(GL_VERTEX_SHADER,"",surf_vs),pixel=emul_compile(GL_FRAGMENT_SHADER,"",
        s->native_project?surf_video_fragment:fragment);
    if(!vertex || !pixel)goto fail;
    program=p_glCreateProgram();p_glAttachShader(program,vertex);p_glAttachShader(program,pixel);p_glLinkProgram(program);
    GLint ok=0;p_glGetProgramiv(program,GL_LINK_STATUS,&ok);if(!ok)goto fail;
    if(s->native_project)for(int i=0;i<8;i++)uniforms[i]=p_glGetUniformLocation(program,surf_video_uniforms[i]);
    p_glGenTextures(1,&external);p_glBindTexture(GL_TEXTURE_EXTERNAL_OES,external);
    s_glTexParameteri(GL_TEXTURE_EXTERNAL_OES,GL_TEXTURE_MIN_FILTER,GL_LINEAR);
    s_glTexParameteri(GL_TEXTURE_EXTERNAL_OES,GL_TEXTURE_MAG_FILTER,GL_LINEAR);
    s_glTexParameteri(GL_TEXTURE_EXTERNAL_OES,GL_TEXTURE_WRAP_S,GL_CLAMP_TO_EDGE);
    s_glTexParameteri(GL_TEXTURE_EXTERNAL_OES,GL_TEXTURE_WRAP_T,GL_CLAMP_TO_EDGE);
    p_glGenFramebuffers(1,&fbo);p_glGenVertexArrays(1,&vao);
    pthread_mutex_lock(&surf_lock);s->ready=1;pthread_cond_broadcast(&surf_cond);pthread_mutex_unlock(&surf_lock);
    LOG("surface_native: shared GPU video surface ready (%s)",s->native_project?"fused visible-view conversion":"full panorama");
    int64_t start=0,frames=0,views=0,draw_ns=0,reused=0,held_views=0,anchored_views=0,recentred_views=0;
    while(!s->stop){
        int index=-1;
        surf_video_job job;
        pthread_mutex_lock(&surf_lock);
        if(s->video_job.seq==last_job && !s->stop){
            struct timespec until;clock_gettime(CLOCK_REALTIME,&until);until.tv_nsec+=1000000;
            if(until.tv_nsec>=1000000000){until.tv_sec++;until.tv_nsec-=1000000000;}
            pthread_cond_timedwait(&surf_cond,&surf_lock,&until);
        }
        job=s->video_job;
        for(int i=0;i<SURF_NATIVE_SLOTS;i++)if(s->native_slots[i].state==0){index=i;s->native_slots[i].state=1;break;}
        pthread_mutex_unlock(&surf_lock);
        if(index<0){struct timespec pause={0,250000};nanosleep(&pause,NULL);continue;}
        surf_native_slot *slot=&s->native_slots[index];
        AImage *image=NULL;
        if(s->native_project){
            // Acquire future pictures promptly: leaving them in a droppable
            // producer queue loses pictures before their presentation time.
            // Keep at most four future pictures plus the currently sampled one.
            int64_t now=monotonic_ns(),target=surf_video_target(now,job.time);
            while(pending_count && !surf_video_future(pending_time[0],now,target)){
                if(image)sn.image_delete(image);
                image=pending[0];pending_count--;
                memmove(pending,pending+1,pending_count*sizeof(*pending));
                memmove(pending_time,pending_time+1,pending_count*sizeof(*pending_time));
            }
            while(pending_count<PENDING_IMAGES && pending_count+(held!=NULL)+(image!=NULL)<ACQUIRED_IMAGES){
                AImage *next=NULL;if(sn.reader_next(reader,&next)!=AMEDIA_OK)break;
                int64_t timestamp=0;sn.image_time(next,&timestamp);
                if(surf_video_future(timestamp,now,target)){
                    pending[pending_count]=next;pending_time[pending_count++]=timestamp;
                }else{
                    if(image)sn.image_delete(image);image=next;
                }
            }
        }else sn.reader_latest(reader,&image);
        int new_image=image!=NULL;
        if(new_image){
            // Keep the decoder image acquired while GLES might sample it again
            // for head movement. Releasing it sooner lets the decoder overwrite it.
            if(input!=EGL_NO_IMAGE_KHR)sn.destroy(display,input);
            if(held)sn.image_delete(held);
            held=image;input=EGL_NO_IMAGE_KHR;
            sn.image_time(held,&source_time);
            AHardwareBuffer *buffer=NULL;
            int valid=sn.image_buffer(held,&buffer)==AMEDIA_OK;
            if(valid)sn.describe(buffer,&desc);
            if(!valid || !desc.width || !desc.height || desc.width>8192 || desc.height>8192)goto fail;
            input=sn.create(display,EGL_NO_CONTEXT,EGL_NATIVE_BUFFER_ANDROID,sn.client(buffer),NULL);
            if(input==EGL_NO_IMAGE_KHR)goto fail;
            p_glActiveTexture(GL_TEXTURE0);p_glBindTexture(GL_TEXTURE_EXTERNAL_OES,external);sn.bind(GL_TEXTURE_EXTERNAL_OES,input);
            frames++;video_dirty=1;
        }
        // Importing/acquiring a decoder buffer can take time. Pick the newest
        // request immediately before drawing instead of keeping the earlier pose.
        pthread_mutex_lock(&surf_lock);job=s->video_job;pthread_mutex_unlock(&surf_lock);
        // Coalesce video arrivals into the next application view request. Drawing
        // on both clocks would do up to video-fps + headset-fps work per second.
        if(!held || (s->native_project?(!job.size || job.seq==last_job):!new_image)){
            pthread_mutex_lock(&surf_lock);slot->state=0;pthread_mutex_unlock(&surf_lock);continue;
        }
        if(s->native_project && !video_dirty && surf_video_covers(&rendered_job,&job)){
            last_job=job.seq;reused++;
            pthread_mutex_lock(&surf_lock);slot->state=0;pthread_mutex_unlock(&surf_lock);continue;
        }
        uint32_t width=s->native_project?2*job.size:desc.width,height=s->native_project?job.size:desc.height;
        if(!slot->buffer || slot->width!=width || slot->height!=height){
            if(slot->texture)p_glDeleteTextures(1,&slot->texture);
            if(slot->egl_image)sn.destroy(display,slot->egl_image);
            slot->texture=0;slot->egl_image=EGL_NO_IMAGE_KHR;surf_native_slot_destroy(slot);
            if(!surf_native_allocate(slot,display,width,height))goto fail;
            LOG("surface_native: %ux%u decoded input -> %ux%u shared view",desc.width,desc.height,width,height);
        }
        p_glBindFramebuffer(GL_FRAMEBUFFER,fbo);
        p_glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,slot->texture,0);
        if(p_glCheckFramebufferStatus(GL_FRAMEBUFFER)!=GL_FRAMEBUFFER_COMPLETE)goto fail;
        p_glUseProgram(program);p_glUniform1i(p_glGetUniformLocation(program,"tex"),0);p_glBindVertexArray(vao);
        int64_t draw_start=monotonic_ns();
        surf_video_job draw=job;
        if(s->native_project){
            if(surf_video_anchor(&rendered_job,&job,&draw))anchored_views++;
            else recentred_views++;
            surf_video_draw(program,uniforms,external,&draw);
        }
        else {p_glViewport(0,0,(GLsizei)width,(GLsizei)height);p_glDrawArrays(GL_TRIANGLES,0,3);}
        sn.finish(); // external writes completed before the Vulkan ownership acquire
        if(!video_dirty)held_views++;
        draw_ns+=monotonic_ns()-draw_start;views++;last_job=job.seq;rendered_job=draw;video_dirty=0;
        GLenum error=p_glGetError();
        if(error)goto fail;
        pthread_mutex_lock(&surf_lock);
        for(int i=0;i<SURF_NATIVE_SLOTS;i++)if(s->native_slots[i].state==2)s->native_slots[i].state=0;
        slot->video=draw;slot->projected=s->native_project;slot->source_width=desc.width;slot->source_height=desc.height;
        slot->source_time=source_time;
        slot->seq=++s->seq;slot->state=2;s->frames++;
        pthread_mutex_unlock(&surf_lock);
        int64_t now=monotonic_ns();if(!start)start=now;
        if(now-start>5000000000ll){LOG("surface_native: %.1f video frames/s, %.1f views/s, visible-view GPU wait %.2f ms, no picture readback",
            frames*1e9/(now-start),views*1e9/(now-start),views?draw_ns/1e6/views:0);
            if(s->native_project)LOG("surface_native: held-picture redraws=%lld, compositor reuse=%lld, panorama radius=%.3f",
                (long long)held_views,(long long)reused,job.params[0][7]);
            if(s->native_project)LOG("surface_native: fixed sampling grid=%lld views, recentered=%lld views",
                (long long)anchored_views,(long long)recentred_views);
            frames=views=draw_ns=reused=held_views=anchored_views=recentred_views=0;start=now;}
    }
    goto done;
fail:
    surf_fail(s,"shared GPU video initialization/delivery failed");
done:
    sn.finish();
    if(input!=EGL_NO_IMAGE_KHR)sn.destroy(display,input);
    if(held)sn.image_delete(held);
    for(int i=0;i<pending_count;i++)sn.image_delete(pending[i]);
    for(int i=0;i<SURF_NATIVE_SLOTS;i++){
        surf_native_slot *slot=&s->native_slots[i];
        if(slot->texture)p_glDeleteTextures(1,&slot->texture);
        if(slot->egl_image)sn.destroy(display,slot->egl_image);
        slot->texture=0;slot->egl_image=EGL_NO_IMAGE_KHR;
    }
    if(external)p_glDeleteTextures(1,&external);if(fbo)p_glDeleteFramebuffers(1,&fbo);
    if(reader)sn.reader_delete(reader);
}
#endif
static void surf_native_complete(surf_swapchain *s) {
    pthread_mutex_lock(&surf_lock);
    if(s->native_pending>=0)s->native_slots[s->native_pending].state=0;
    s->native_pending=-1;pthread_mutex_unlock(&surf_lock);
}
static void surf_native_ownership(VkCommandBuffer command,surf_native_slot *slot,int acquire) {
    VkAccessFlags access=slot->projected?VK_ACCESS_TRANSFER_READ_BIT:VK_ACCESS_SHADER_READ_BIT;
    VkImageLayout layout=slot->projected?VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL:VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    VkImageMemoryBarrier barrier={VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,NULL,
        acquire?0:access,acquire?access:0,
        acquire?VK_IMAGE_LAYOUT_GENERAL:layout,
        acquire?layout:VK_IMAGE_LAYOUT_GENERAL,
        acquire?VK_QUEUE_FAMILY_FOREIGN_EXT:vk.family,acquire?vk.family:VK_QUEUE_FAMILY_FOREIGN_EXT,
        slot->image,{VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1}};
    vk.CmdPipelineBarrier(command,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
        0,0,NULL,0,NULL,1,&barrier);
}
static void surf_native_retire(surf_swapchain *s) {
    if(!s->native_mode || s->native_current<0)return;
    // Teardown is the only blocking handoff. The worker is stopped before a
    // swapchain is destroyed, so it cannot rewrite an image being retired.
    VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,NULL,
        VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT,NULL};
    vk.ResetCommandBuffer(s->cmd,0);vk.BeginCommandBuffer(s->cmd,&begin);
    surf_native_ownership(s->cmd,&s->native_slots[s->native_current],0);
    vk.EndCommandBuffer(s->cmd);
    VkSubmitInfo submit={VK_STRUCTURE_TYPE_SUBMIT_INFO,NULL,0,NULL,NULL,1,&s->cmd,0,NULL};
    if(vk.QueueSubmit(vk.queue,1,&submit,s->fence)==VK_SUCCESS){
        vk.WaitForFences(vk.device,1,&s->fence,VK_TRUE,UINT64_MAX);vk.ResetFences(vk.device,1,&s->fence);
    }
    pthread_mutex_lock(&surf_lock);s->native_slots[s->native_current].state=0;
    s->native_current=-1;pthread_mutex_unlock(&surf_lock);
}
static void surf_native_destroy(surf_swapchain *s) {
    if(!s->native_mode)return;
    for(int i=0;i<SURF_NATIVE_SLOTS;i++)surf_native_slot_destroy(&s->native_slots[i]);
}
