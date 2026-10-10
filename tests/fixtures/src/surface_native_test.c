#define VK_USE_PLATFORM_ANDROID_KHR
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
#include <time.h>
#include "layer_math.h"
#include "surface_views.h"
#include "surface_video.h"
#include <android/hardware_buffer.h>
#include <android/native_window_jni.h>
#include <media/NdkImageReader.h>
#include <media/NdkMediaCodec.h>
#include <media/NdkMediaExtractor.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES3/gl3.h>
#include <GLES2/gl2ext.h>
#include <dlfcn.h>
#define SURF_MAX 8
#define MAX_IMAGES 8
#define LOG(...) do {printf(__VA_ARGS__);puts("");fflush(stdout);} while(0)
#define VK_OK(call) assert((call)==VK_SUCCESS)
static int surface_native=1;
typedef struct {XrSwapchain handle;XrSwapchainCreateInfo info;VkImage images[MAX_IMAGES];uint32_t image_count;} tracked_swapchain;
static struct {VkInstance instance;VkPhysicalDevice physical;VkDevice device;VkQueue queue;VkCommandPool pool;uint32_t family;
    PFN_vkGetDeviceProcAddr GetDeviceProcAddr;PFN_vkWaitForFences WaitForFences;PFN_vkResetFences ResetFences;
    PFN_vkResetCommandBuffer ResetCommandBuffer;PFN_vkBeginCommandBuffer BeginCommandBuffer;PFN_vkEndCommandBuffer EndCommandBuffer;
    PFN_vkAllocateCommandBuffers AllocateCommandBuffers;PFN_vkCreateFence CreateFence;PFN_vkQueueSubmit QueueSubmit;PFN_vkCmdPipelineBarrier CmdPipelineBarrier;} vk;
static struct {PFN_vkAllocateMemory AllocateMemory;PFN_vkGetPhysicalDeviceMemoryProperties GetPhysicalDeviceMemoryProperties;
    PFN_vkGetFenceStatus GetFenceStatus;PFN_vkCmdCopyBufferToImage CmdCopyBufferToImage;} svk;
#define SURF_NATIVE_SLOTS 4
typedef struct {AHardwareBuffer *buffer;VkImage image;VkImageView view;VkDeviceMemory memory;EGLImageKHR egl_image;GLuint texture;uint32_t width,height;int state;uint64_t seq;uint32_t source_width,source_height;int64_t source_time;surf_video_job video;int projected;} surf_native_slot;
typedef struct {XrSwapchain handle;uint32_t width,height;int in_flight,uploads,native_mode,native_pending,native_current,native_project;surf_video_job video_job;uint64_t seq;
    int stop,ready,failed;int64_t frames;
    surf_native_slot native_slots[SURF_NATIVE_SLOTS];
    VkBuffer staging;void *staging_map;unsigned char *pixels;VkCommandBuffer cmd;VkFence fence;} surf_swapchain;
static surf_swapchain surfs[SURF_MAX];static pthread_mutex_t surf_lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t surf_cond=PTHREAD_COND_INITIALIZER;
static AImageReader *surf_worker_test_reader;
static int64_t monotonic_ns(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (int64_t)t.tv_sec*1000000000ll+t.tv_nsec;}
static void surf_fail(surf_swapchain *s,const char *message){LOG("FAIL: %s",message);s->failed=1;}
static GLuint fixture_shader(GLenum type,const char *source);
static const char *surf_vs="#version 300 es\nout vec2 uv;void main(){vec2 p=vec2(float((gl_VertexID<<1)&2),float(gl_VertexID&2));uv=p;gl_Position=vec4(p*2.-1.,0.,1.);}";
static int equirect_res=2048,equirect_res_set=1;
#define SURF_NATIVE_ALLOCATOR_TEST
#define SURF_NATIVE_WORKER_TEST
#define emul_compile(type,prefix,body) fixture_shader(type,body)
#define p_glGenTextures glGenTextures
#define p_glBindTexture glBindTexture
#define p_glGetError glGetError
#define p_glUseProgram glUseProgram
#define p_glActiveTexture glActiveTexture
#define p_glViewport glViewport
#define p_glUniform4f glUniform4f
#define p_glDrawArrays glDrawArrays
#define p_glCreateProgram glCreateProgram
#define p_glAttachShader glAttachShader
#define p_glLinkProgram glLinkProgram
#define p_glGetProgramiv glGetProgramiv
#define p_glGetUniformLocation glGetUniformLocation
#define p_glUniform1i glUniform1i
#define p_glGenFramebuffers glGenFramebuffers
#define p_glGenVertexArrays glGenVertexArrays
#define p_glDeleteTextures glDeleteTextures
#define p_glDeleteFramebuffers glDeleteFramebuffers
#define p_glBindFramebuffer glBindFramebuffer
#define p_glFramebufferTexture2D glFramebufferTexture2D
#define p_glCheckFramebufferStatus glCheckFramebufferStatus
#define p_glBindVertexArray glBindVertexArray
#define s_glTexParameteri glTexParameteri
#include "surface_native.c"
static XrInstance active_instance;static int surf_vulkan=1;static VkImage destination;static VkDeviceMemory destination_memory;
static XrQuaternionf test_rotation={0,0,0,1};
static XrVector3f test_position;
static int test_reference_guard,wait_timeouts,acquires;
static GLuint fixture_shader(GLenum type,const char *source){
    GLuint shader=glCreateShader(type);glShaderSource(shader,1,&source,NULL);glCompileShader(shader);
    GLint ok;glGetShaderiv(shader,GL_COMPILE_STATUS,&ok);
    if(!ok){char log[4096];glGetShaderInfoLog(shader,sizeof(log),NULL,log);puts(log);}assert(ok);return shader;
}
static uint32_t memory_type(uint32_t mask,VkMemoryPropertyFlags flags) {
    VkPhysicalDeviceMemoryProperties props;vkGetPhysicalDeviceMemoryProperties(vk.physical,&props);
    for(uint32_t i=0;i<props.memoryTypeCount;i++)if((mask&(1u<<i)) && (props.memoryTypes[i].propertyFlags&flags)==flags)return i;
    assert(0);return 0;
}
static struct {VkImage image;VkDeviceMemory memory;int used;} runtime_images[16];
static int fixture_image_slot(XrSwapchain sc){int n=(int)(uintptr_t)sc-2;assert(n>=0&&n<16&&runtime_images[n].used);return n;}
static XrResult fake_create(XrSession session,const XrSwapchainCreateInfo *ci,XrSwapchain *out) {
    (void)session;
    int slot=0;while(slot<16&&runtime_images[slot].used)slot++;assert(slot<16);
    VkImageCreateInfo image={VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,NULL,0,VK_IMAGE_TYPE_2D,(VkFormat)ci->format,
        {ci->width,ci->height,1},1,ci->arraySize,VK_SAMPLE_COUNT_1_BIT,VK_IMAGE_TILING_OPTIMAL,
        VK_IMAGE_USAGE_TRANSFER_DST_BIT|VK_IMAGE_USAGE_TRANSFER_SRC_BIT|VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT|VK_IMAGE_USAGE_SAMPLED_BIT,VK_SHARING_MODE_EXCLUSIVE,0,NULL,0};
    VK_OK(vkCreateImage(vk.device,&image,NULL,&destination));VkMemoryRequirements req;vkGetImageMemoryRequirements(vk.device,destination,&req);
    VkMemoryAllocateInfo alloc={VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,NULL,req.size,memory_type(req.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)};
    VK_OK(vkAllocateMemory(vk.device,&alloc,NULL,&destination_memory));VK_OK(vkBindImageMemory(vk.device,destination,destination_memory,0));
    assert(ci->format==VK_FORMAT_R8G8B8A8_SRGB);
    assert(!(ci->usageFlags&XR_SWAPCHAIN_USAGE_UNORDERED_ACCESS_BIT));
    runtime_images[slot].image=destination;runtime_images[slot].memory=destination_memory;runtime_images[slot].used=1;
    *out=(XrSwapchain)(uintptr_t)(slot+2);return XR_SUCCESS;
}
static XrResult fake_enumerate(XrSwapchain sc,uint32_t cap,uint32_t *count,XrSwapchainImageBaseHeader *images) {
    (void)sc;(void)cap;*count=1;((XrSwapchainImageVulkanKHR*)images)->image=runtime_images[fixture_image_slot(sc)].image;return XR_SUCCESS;
}
static XrResult fake_destroy(XrSwapchain sc){int slot=fixture_image_slot(sc);vkDestroyImage(vk.device,runtime_images[slot].image,NULL);vkFreeMemory(vk.device,runtime_images[slot].memory,NULL);runtime_images[slot].used=0;return XR_SUCCESS;}
static XrResult fake_acquire(XrSwapchain sc,const XrSwapchainImageAcquireInfo *info,uint32_t *index){(void)sc;(void)info;*index=0;acquires++;return XR_SUCCESS;}
static XrResult fake_wait(XrSwapchain sc,const XrSwapchainImageWaitInfo *info){(void)sc;(void)info;if(wait_timeouts){assert(info->timeout==0);wait_timeouts--;return XR_TIMEOUT_EXPIRED;}return XR_SUCCESS;}
static XrResult fake_release(XrSwapchain sc,const XrSwapchainImageReleaseInfo *info){(void)sc;(void)info;return XR_SUCCESS;}
static XrResult fake_locate(XrSession session,const XrViewLocateInfo *info,XrViewState *state,uint32_t cap,uint32_t *count,XrView *views) {
    (void)session;(void)info;(void)cap;*count=2;state->viewStateFlags=XR_VIEW_STATE_ORIENTATION_VALID_BIT|XR_VIEW_STATE_POSITION_VALID_BIT;
    for(int i=0;i<2;i++){views[i].pose=(XrPosef){test_rotation,test_position};views[i].fov=(XrFovf){-.6,.6,.6,-.6};
        if(test_reference_guard)views[i].fov=surf_video_guard_fov(views[i].fov);}return XR_SUCCESS;
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
static void read_destination(surf_swapchain *s){
    VK_OK(vkWaitForFences(vk.device,1,&s->fence,VK_TRUE,UINT64_MAX));
    VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    VK_OK(vkResetCommandBuffer(s->cmd,0));VK_OK(vkBeginCommandBuffer(s->cmd,&begin));
    surf_barrier(s->cmd,destination,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT);
    VkBufferImageCopy copy={0,0,0,{VK_IMAGE_ASPECT_COLOR_BIT,0,0,1},{0,0,0},{4096,2048,1}};
    vkCmdCopyImageToBuffer(s->cmd,destination,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,s->staging,1,&copy);
    VK_OK(vkEndCommandBuffer(s->cmd));VkSubmitInfo submit={VK_STRUCTURE_TYPE_SUBMIT_INFO,NULL,0,NULL,NULL,1,&s->cmd};
    VK_OK(vkQueueSubmit(vk.queue,1,&submit,VK_NULL_HANDLE));VK_OK(vkQueueWaitIdle(vk.queue));
}
static void *worker_thread(void *data){
    EGLDisplay display=eglGetDisplay(EGL_DEFAULT_DISPLAY);
    EGLint attrs[]={EGL_RENDERABLE_TYPE,EGL_OPENGL_ES3_BIT,EGL_SURFACE_TYPE,EGL_PBUFFER_BIT,EGL_NONE};
    EGLConfig config;EGLint nc;assert(eglChooseConfig(display,attrs,&config,1,&nc)&&nc);
    EGLint ca[]={EGL_CONTEXT_CLIENT_VERSION,3,EGL_NONE},pa[]={EGL_WIDTH,16,EGL_HEIGHT,16,EGL_NONE};
    EGLContext context=eglCreateContext(display,config,EGL_NO_CONTEXT,ca);
    EGLSurface surface=eglCreatePbufferSurface(display,config,pa);assert(eglMakeCurrent(display,surface,surface,context));
    surf_native_worker(data,NULL,display);
    eglMakeCurrent(display,EGL_NO_SURFACE,EGL_NO_SURFACE,EGL_NO_CONTEXT);eglDestroySurface(display,surface);eglDestroyContext(display,context);
    return NULL;
}
#include "surface_composite_test.inc"
static void test_real_worker(surf_swapchain *s,XrFrameEndInfo *frame,const XrCompositionLayerBaseHeader *layer,const char *path){
    int fd=open(path,O_RDONLY);assert(fd>=0);struct stat st;assert(!fstat(fd,&st));
    AMediaExtractor *extractor=AMediaExtractor_new();assert(AMediaExtractor_setDataSourceFd(extractor,fd,0,st.st_size)==AMEDIA_OK);
    AMediaFormat *format=NULL;
    for(size_t i=0;i<AMediaExtractor_getTrackCount(extractor);i++){
        AMediaFormat *f=AMediaExtractor_getTrackFormat(extractor,i);const char *mime=NULL;
        if(AMediaFormat_getString(f,AMEDIAFORMAT_KEY_MIME,&mime)&&!strcmp(mime,"video/hevc")){format=f;assert(AMediaExtractor_selectTrack(extractor,i)==AMEDIA_OK);break;}
        AMediaFormat_delete(f);
    }
    assert(format);int32_t width,height;assert(AMediaFormat_getInt32(format,AMEDIAFORMAT_KEY_WIDTH,&width));assert(AMediaFormat_getInt32(format,AMEDIAFORMAT_KEY_HEIGHT,&height));
    assert(width==8192 && height==4096);
    // Match the game's logical external surface; MediaCodec overrides its
    // buffers with the untouched 8192x4096 decoded pictures.
    ANativeWindow *window;assert(AImageReader_newWithUsage(4096,2048,AIMAGE_FORMAT_PRIVATE,AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE,5,&surf_worker_test_reader)==AMEDIA_OK);
    assert(AImageReader_getWindow(surf_worker_test_reader,&window)==AMEDIA_OK);
    s->stop=s->ready=s->failed=0;s->frames=0;memset(&s->video_job,0,sizeof(s->video_job));
    pthread_t worker;assert(!pthread_create(&worker,NULL,worker_thread,s));
    AMediaCodec *codec=AMediaCodec_createDecoderByType("video/hevc");assert(codec);
    char *name;assert(AMediaCodec_getName(codec,&name)==AMEDIA_OK);assert(!strcmp(name,"OMX.frameport.hevc.decoder"));AMediaCodec_releaseName(codec,name);
    assert(AMediaCodec_configure(codec,format,window,NULL,0)==AMEDIA_OK);assert(AMediaCodec_start(codec)==AMEDIA_OK);
    tracked_swapchain game={.info={XR_TYPE_SWAPCHAIN_CREATE_INFO,NULL,0,
        XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT|XR_SWAPCHAIN_USAGE_TRANSFER_SRC_BIT,
        VK_FORMAT_R8G8B8A8_SRGB,1,64,64,1,2,1},.image_count=1};
    assert(fake_create((XrSession)(uintptr_t)1,&game.info,&game.handle)==XR_SUCCESS);
    game.images[0]=runtime_images[fixture_image_slot(game.handle)].image;
    const unsigned char scene_colors[2][4]={{255,0,0,255},{0,0,255,255}};
    fixture_upload_image(s,game.images[0],64,64,2,VK_IMAGE_LAYOUT_UNDEFINED,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,scene_colors);
    XrCompositionLayerProjectionView scene_views[2];
    for(int e=0;e<2;e++)scene_views[e]=(XrCompositionLayerProjectionView){.type=XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW,
        .pose={{0,0,0,1},{e?.03f:-.03f,0,0}},.fov={-.6f,.6f,.6f,-.6f},
        .subImage={game.handle,{{0,0},{64,64}},e}};
    XrCompositionLayerProjection scene={.type=XR_TYPE_COMPOSITION_LAYER_PROJECTION,.space=(XrSpace)(uintptr_t)1,.viewCount=2,.views=scene_views};
    XrCompositionLayerImageLayoutFB scene_layout={XR_TYPE_COMPOSITION_LAYER_IMAGE_LAYOUT_FB,NULL,0};
    XrCompositionLayerColorScaleBiasKHR scene_color={XR_TYPE_COMPOSITION_LAYER_COLOR_SCALE_BIAS_KHR,&scene_layout,{1,1,1,1},{0,0,0,0}};
    scene.next=&scene_color;
    const XrCompositionLayerBaseHeader *composed_layers[3]={(const void*)&scene,frame->layers[0],frame->layers[1]};
    XrFrameEndInfo composed_frame=*frame;composed_frame.layerCount=3;composed_frame.layers=composed_layers;
    int combined_frames=0;
    int submissions=0;int64_t next_view=0;
    XrCompositionLayerColorScaleBiasKHR animated[2];const void *saved_next[2];
    assert(frame->layerCount==2);
    for(int eye=0;eye<2;eye++){
        XrCompositionLayerEquirect2KHR *panorama=(void*)frame->layers[eye];saved_next[eye]=panorama->next;
        animated[eye]=(XrCompositionLayerColorScaleBiasKHR){XR_TYPE_COMPOSITION_LAYER_COLOR_SCALE_BIAS_KHR,
            saved_next[eye],{1,1,1,1},{0,0,0,0}};panorama->next=&animated[eye];
    }
    for(int phase=0;phase<2;phase++){
        int goal=phase?120:600,sent=0,received=0,input_eos=0,output_eos=0,paused=0;
        int phase_views=(int)s->frames,phase_requests=submissions;
        int distinct_pictures=0,stable_grid_pairs=0;int64_t last_picture=0;
        surf_video_job previous_picture={0};
        int primary_frames=0,presented_frames=0,primary_transitions=0,last_primary=-1;
        ssize_t waiting=-1;AMediaCodecBufferInfo out={0};
        int64_t start=monotonic_ns(),pause_until=0,next_input=0,time_shift=0;
        while((!output_eos || monotonic_ns()<start+goal*16666667ll+time_shift+100000000ll) && monotonic_ns()-start<45000000000ll){
            int64_t now=monotonic_ns();
            if(!paused && received>=120){paused=1;pause_until=now+300000000ll;time_shift=300000000ll;}
            if(!input_eos && now>=pause_until && now>=next_input){ssize_t id=AMediaCodec_dequeueInputBuffer(codec,1000);if(id>=0){
                size_t capacity;uint8_t *data=AMediaCodec_getInputBuffer(codec,id,&capacity);assert(data);
                if(sent==goal){assert(AMediaCodec_queueInputBuffer(codec,id,0,0,sent*1000000ll/60,AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM)==AMEDIA_OK);input_eos=1;}
                else{
                    ssize_t size=AMediaExtractor_readSampleData(extractor,data,capacity);assert(size>0);
                    assert(AMediaCodec_queueInputBuffer(codec,id,0,size,AMediaExtractor_getSampleTime(extractor),AMediaExtractor_getSampleFlags(extractor)&1)==AMEDIA_OK);
                    assert(AMediaExtractor_advance(extractor));sent++;
                    // Feed ahead, as ExoPlayer does, while scheduling pictures
                    // at 60 Hz. Reading "latest" must not skip future pictures.
                    next_input=now+8333333ll;
                }
            }}
            if(waiting<0)waiting=AMediaCodec_dequeueOutputBuffer(codec,&out,1000);
            int64_t scheduled=start+out.presentationTimeUs*1000+time_shift;
            // ExoPlayer queues surface pictures at most ~50 ms early.
            if(waiting>=0 && scheduled<=now+50000000ll){
                if(out.size)received++;output_eos=out.flags&AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM;
                assert(AMediaCodec_releaseOutputBufferAtTime(codec,waiting,scheduled)==AMEDIA_OK);waiting=-1;
            }
            if(now>=next_view){
                pthread_mutex_lock(&surf_lock);
                for(int slot=0;slot<SURF_NATIVE_SLOTS;slot++)if(s->native_slots[slot].state==2){
                    int64_t picture=s->native_slots[slot].source_time;
                    assert(picture<=monotonic_ns()); // never display a future picture
                    if(picture && picture!=last_picture){
                        assert(picture>last_picture);last_picture=picture;distinct_pictures++;
                        surf_video_job *rendered=&s->native_slots[slot].video;
                        if(previous_picture.seq &&
                            !memcmp(previous_picture.params[0],rendered->params[0],4*sizeof(float)) &&
                            !memcmp(previous_picture.params[1],rendered->params[1],4*sizeof(float)))stable_grid_pairs++;
                        previous_picture=*rendered;
                    }
                }
                pthread_mutex_unlock(&surf_lock);
                frame->displayTime++;float angle=(phase?.8f:.12f)*sinf(submissions*(phase?.06f:.02f));
                test_rotation=(XrQuaternionf){0,sinf(angle/2),0,cosf(angle/2)};
                test_position=(XrVector3f){.005f*sinf(submissions*.03f),.002f*sinf(submissions*.017f),0};
                // Tracking can move an infinite panorama's origin every frame.
                // This must not defeat compositor reuse of unchanged pictures.
                for(unsigned eye=0;eye<frame->layerCount;eye++){
                    XrCompositionLayerEquirect2KHR *panorama=(void*)frame->layers[eye];
                    panorama->pose.position.x=.003f*sinf(submissions*.03f);
                    panorama->pose.orientation=lm_axis_angle(0,1,0,.02f*sinf(submissions*.04f));
                    animated[eye].colorScale.r=.7f+.2f*sinf(submissions*.037f);
                    animated[eye].colorBias.g=.01f+.01f*cosf(submissions*.029f);
                }
                for(int e=0;e<2;e++)scene_views[e].pose=lm_pose_mul((XrPosef){test_rotation,test_position},(XrPosef){{0,0,0,1},{e?.03f:-.03f,0,0}});
                sc_capture_until=monotonic_ns()+1000000000ll;surf_scene_capture(&game,0);surf_scene_release_commit(game.handle);++sc_serial;
                composed_frame.displayTime=frame->displayTime;
                const XrCompositionLayerBaseHeader *presented=surf_projection_frame((XrSession)(uintptr_t)1,&composed_frame,layer);
                if(presented) {
                    const XrCompositionLayerBaseHeader *combined=surf_composite_frame((XrSession)(uintptr_t)1,&composed_frame,&scene,presented);
                    if(combined==(const void*)&sc_outputs[0].layer)combined_frames++;
                }
                if(presented){
                    int primary=surf_projection_occludes(presented);presented_frames++;primary_frames+=primary;
                    if(last_primary>=0 && primary!=last_primary)primary_transitions++;
                    last_primary=primary;
                }
                submissions++;next_view=now+10416667ll;
            }
            assert(!s->failed);
        }
        assert(output_eos && received==goal);
        assert(distinct_pictures>=goal-30);
        if(!phase)assert(stable_grid_pairs>goal/2);
        if(!phase){assert(primary_frames>presented_frames*.9f);assert(primary_transitions<10);}
        printf("PASS: animated opaque colour: primary %d/%d presented requests, %d path transitions.\n",
            primary_frames,presented_frames,primary_transitions);
        printf("PASS: %d consecutive new-picture pairs retained the fixed stereo sampling grid.\n",stable_grid_pairs);
        printf("PASS: %d/%d distinct scheduled pictures, none early.\n",distinct_pictures,goal);fflush(stdout);
        printf("PASS: %s movement: %lld views for %d requests, %d original video frames.\n",phase?"rapid":"gentle",
            (long long)s->frames-phase_views,submissions-phase_requests,received);fflush(stdout);
        printf("PASS: real production worker phase %d: %d original 8K frames, pause/resume and head-view requests.\n",phase,received);fflush(stdout);
        if(!phase){assert(AMediaCodec_flush(codec)==AMEDIA_OK);assert(AMediaExtractor_seekTo(extractor,0,AMEDIAEXTRACTOR_SEEK_PREVIOUS_SYNC)==AMEDIA_OK);}
    }
    s->stop=1;pthread_join(worker,NULL);
    assert(!s->failed && s->frames>0 && (uint64_t)s->frames<=s->video_job.seq);
    printf("PASS: worker produced %lld views for %llu requests; video and view arrivals were coalesced.\n",(long long)s->frames,(unsigned long long)s->video_job.seq);
    for(int eye=0;eye<2;eye++)((XrCompositionLayerEquirect2KHR*)frame->layers[eye])->next=saved_next[eye];
    assert(combined_frames>submissions*.95f);
    printf("PASS: original 8K decoding + production scene composition: %d/%d requests on one primary, %llu scene frames, %llu held.\n",
        combined_frames,submissions,(unsigned long long)sc_outputs[0].frames,(unsigned long long)sc_outputs[0].holds);
    surf_projection_destroy(s);surf_scene_destroy(game.handle);fake_destroy(game.handle);surf_native_destroy(s);
    assert(AMediaCodec_stop(codec)==AMEDIA_OK);AMediaCodec_delete(codec);AMediaFormat_delete(format);AMediaExtractor_delete(extractor);close(fd);
}
int main(int argc,char **argv) {
    VkApplicationInfo api={VK_STRUCTURE_TYPE_APPLICATION_INFO,NULL,"shared-video-test",0,NULL,0,VK_API_VERSION_1_1};
    VkInstanceCreateInfo instance={VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};instance.pApplicationInfo=&api;VK_OK(vkCreateInstance(&instance,NULL,&vk.instance));
    uint32_t count=1;VK_OK(vkEnumeratePhysicalDevices(vk.instance,&count,&vk.physical));
    assert(count && vk.physical);puts("fixture: Vulkan device available");fflush(stdout);
    uint32_t families=0;vkGetPhysicalDeviceQueueFamilyProperties(vk.physical,&families,NULL);
    VkQueueFamilyProperties *properties=calloc(families,sizeof(*properties));vkGetPhysicalDeviceQueueFamilyProperties(vk.physical,&families,properties);
    for(vk.family=0;vk.family<families;vk.family++)if(properties[vk.family].queueFlags&VK_QUEUE_COMPUTE_BIT)break;
    assert(vk.family<families);float priority=1;
    VkDeviceQueueCreateInfo queue={VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,NULL,0,vk.family,1,&priority};
    VkDeviceCreateInfo device={VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,NULL,0,1,&queue};const char *extensions[]={"VK_ANDROID_external_memory_android_hardware_buffer","VK_EXT_queue_family_foreign"};
    device.enabledExtensionCount=2;device.ppEnabledExtensionNames=extensions;
    VK_OK(vkCreateDevice(vk.physical,&device,NULL,&vk.device));
    vkGetDeviceQueue(vk.device,vk.family,0,&vk.queue);vk.GetDeviceProcAddr=vkGetDeviceProcAddr;vk.WaitForFences=vkWaitForFences;
    vk.ResetFences=vkResetFences;vk.ResetCommandBuffer=vkResetCommandBuffer;vk.BeginCommandBuffer=vkBeginCommandBuffer;
    vk.AllocateCommandBuffers=vkAllocateCommandBuffers;vk.CreateFence=vkCreateFence;vk.EndCommandBuffer=vkEndCommandBuffer;vk.QueueSubmit=vkQueueSubmit;vk.CmdPipelineBarrier=vkCmdPipelineBarrier;svk.AllocateMemory=vkAllocateMemory;
    svk.GetPhysicalDeviceMemoryProperties=vkGetPhysicalDeviceMemoryProperties;svk.GetFenceStatus=vkGetFenceStatus;svk.CmdCopyBufferToImage=vkCmdCopyBufferToImage;
    surf_swapchain *s=surfs;s->handle=(XrSwapchain)(uintptr_t)1;s->width=16;s->height=8;s->seq=1;s->pixels=calloc(16*8,4);
    s->native_mode=1;s->native_pending=-1;s->native_current=-1;free(s->pixels);s->pixels=NULL;
    EGLDisplay display=eglGetDisplay(EGL_DEFAULT_DISPLAY);assert(eglInitialize(display,NULL,NULL));
    EGLint attrs[]={EGL_RENDERABLE_TYPE,EGL_OPENGL_ES3_BIT,EGL_SURFACE_TYPE,EGL_PBUFFER_BIT,EGL_NONE};
    EGLConfig config;EGLint nc;assert(eglChooseConfig(display,attrs,&config,1,&nc)&&nc);
    EGLint ca[]={EGL_CONTEXT_CLIENT_VERSION,3,EGL_NONE},pa[]={EGL_WIDTH,16,EGL_HEIGHT,16,EGL_NONE};
    EGLContext context=eglCreateContext(display,config,EGL_NO_CONTEXT,ca);assert(context!=EGL_NO_CONTEXT);
    EGLSurface surface=eglCreatePbufferSurface(display,config,pa);assert(eglMakeCurrent(display,surface,surface,context));
    assert(surf_native_load());GLuint framebuffer;glGenFramebuffers(1,&framebuffer);glBindFramebuffer(GL_FRAMEBUFFER,framebuffer);
    puts("fixture: EGL/native bridge ready");fflush(stdout);
    for(int i=0;i<SURF_NATIVE_SLOTS;i++){
        assert(surf_native_allocate(&s->native_slots[i],display,8192,4096));
        glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,s->native_slots[i].texture,0);
        assert(glCheckFramebufferStatus(GL_FRAMEBUFFER)==GL_FRAMEBUFFER_COMPLETE);
        glEnable(GL_SCISSOR_TEST);glScissor(0,0,4096,4096);glClearColor(1,0,0,1);glClear(GL_COLOR_BUFFER_BIT);
        glScissor(4096,0,4096,4096);glClearColor(0,1,0,1);glClear(GL_COLOR_BUFFER_BIT);glDisable(GL_SCISSOR_TEST);glFinish();
    }
    puts("fixture: full-size native buffers ready");fflush(stdout);
    VkCommandPool pool;VkCommandPoolCreateInfo pci={VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,NULL,VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,vk.family};VK_OK(vkCreateCommandPool(vk.device,&pci,NULL,&pool));vk.pool=pool;
    VkCommandBufferAllocateInfo cai={VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,NULL,pool,VK_COMMAND_BUFFER_LEVEL_PRIMARY,1};VK_OK(vkAllocateCommandBuffers(vk.device,&cai,&s->cmd));
    VkFenceCreateInfo fi={VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};VK_OK(vkCreateFence(vk.device,&fi,NULL,&s->fence));
    VkBufferCreateInfo bi={VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,NULL,0,2*2560*2560*4,VK_BUFFER_USAGE_TRANSFER_SRC_BIT|VK_BUFFER_USAGE_TRANSFER_DST_BIT,VK_SHARING_MODE_EXCLUSIVE};
    VK_OK(vkCreateBuffer(vk.device,&bi,NULL,&s->staging));VkMemoryRequirements req;vkGetBufferMemoryRequirements(vk.device,s->staging,&req);
    VkDeviceMemory buffer_memory;VkMemoryAllocateInfo mai={VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,NULL,req.size,memory_type(req.memoryTypeBits,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)};
    VK_OK(vkAllocateMemory(vk.device,&mai,NULL,&buffer_memory));VK_OK(vkBindBufferMemory(vk.device,s->staging,buffer_memory,0));VK_OK(vkMapMemory(vk.device,buffer_memory,0,VK_WHOLE_SIZE,0,&s->staging_map));
    XrCompositionLayerEquirect2KHR layers[2];const XrCompositionLayerBaseHeader *list[2];
    for(int i=0;i<2;i++){layers[i]=(XrCompositionLayerEquirect2KHR){XR_TYPE_COMPOSITION_LAYER_EQUIRECT2_KHR,NULL,0,(XrSpace)(uintptr_t)1,
        i?XR_EYE_VISIBILITY_RIGHT:XR_EYE_VISIBILITY_LEFT,{s->handle,{{i*8,0},{8,8}},0},{{0,0,0,1},{0,0,0}},0,2*LM_PI,LM_PI/2,-LM_PI/2};list[i]=(const void*)&layers[i];}
    XrFrameEndInfo frame={XR_TYPE_FRAME_END_INFO,NULL,1,XR_ENVIRONMENT_BLEND_MODE_OPAQUE,2,list};
    for(int iteration=0;iteration<120;iteration++){
    if(s->in_flight){VK_OK(vkWaitForFences(vk.device,1,&s->fence,VK_TRUE,UINT64_MAX));vkResetFences(vk.device,1,&s->fence);s->in_flight=0;surf_native_complete(s);}
    int previous=s->native_current;
    if(previous>=0)assert(s->native_slots[previous].state==3);
    int slot=-1;
    // Every third submission reprojects the held picture without a new video.
    if(iteration%3!=2){
        for(int i=0;i<SURF_NATIVE_SLOTS;i++)if(s->native_slots[i].state==0){slot=i;break;}
        assert(slot>=0);
        uint32_t width=iteration<60?8192:4096,height=width/2;
        if(s->native_slots[slot].width!=width){
            glDeleteTextures(1,&s->native_slots[slot].texture);
            sn.destroy(display,s->native_slots[slot].egl_image);
            surf_native_slot_destroy(&s->native_slots[slot]);
            assert(surf_native_allocate(&s->native_slots[slot],display,width,height));
        }
        // Emulate the producer writing only after Vulkan returns ownership.
        glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,s->native_slots[slot].texture,0);
        glEnable(GL_SCISSOR_TEST);glScissor(0,0,width/2,height);glClearColor(1,0,0,1);glClear(GL_COLOR_BUFFER_BIT);
        glScissor(width/2,0,width/2,height);glClearColor(0,1,0,1);glClear(GL_COLOR_BUFFER_BIT);glDisable(GL_SCISSOR_TEST);glFinish();
        s->native_slots[slot].seq=++s->seq;s->native_slots[slot].state=2;
    }
    const XrCompositionLayerBaseHeader *result=surf_projection_frame((XrSession)(uintptr_t)1,&frame,list[0]);assert(result && result->type==XR_TYPE_COMPOSITION_LAYER_PROJECTION);
    assert(surf_projections[0].width==(iteration<60?8192:4096) && surf_projections[0].height==(iteration<60?4096:2048));
    assert(!surf_projections[0].source && !surf_projections[0].source_view && !surf_projections[0].memory);
    assert(s->native_current==(slot>=0?slot:previous));
    assert(s->native_slots[s->native_current].state==3);
    assert(s->native_pending==(slot>=0?previous:-1));
    }
    VK_OK(vkWaitForFences(vk.device,1,&s->fence,VK_TRUE,UINT64_MAX));
    VkCommandBufferBeginInfo begin={VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};VK_OK(vkResetCommandBuffer(s->cmd,0));VK_OK(vkBeginCommandBuffer(s->cmd,&begin));
    surf_barrier(s->cmd,destination,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT);
    VkBufferImageCopy copy={0,0,0,{VK_IMAGE_ASPECT_COLOR_BIT,0,0,1},{0,0,0},{4096,2048,1}};vkCmdCopyImageToBuffer(s->cmd,destination,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,s->staging,1,&copy);
    VK_OK(vkEndCommandBuffer(s->cmd));VkSubmitInfo submit={VK_STRUCTURE_TYPE_SUBMIT_INFO,NULL,0,NULL,NULL,1,&s->cmd};VK_OK(vkQueueSubmit(vk.queue,1,&submit,VK_NULL_HANDLE));VK_OK(vkQueueWaitIdle(vk.queue));
    unsigned char *pixels=s->staging_map;
    for(int eye=0;eye<2;eye++)for(int y=128;y<2048;y+=128)for(int x=128;x<2048;x+=128){unsigned char *pixel=pixels+4*(y*4096+eye*2048+x);assert(pixel[eye]==255 && pixel[1-eye]==0 && pixel[3]==255);}
    surf_projection_destroy(s);
    assert(s->native_current==-1 && s->native_pending==-1);
    for(int i=0;i<SURF_NATIVE_SLOTS;i++)assert(s->native_slots[i].state==0);
    puts("PASS: direct shared-image 8K Vulkan sampling, stereo pixels, 120 frame/reprojection iterations, resolution change and ownership teardown.");
    // Fused path: GLES samples an external decoder image at the final eye
    // resolution and Vulkan only copies that finished view. Exercise an older
    // worker pose arriving during a newer app frame, plus repeated stereo calls.
    surf_native_slot input={0};assert(surf_native_allocate(&input,display,8192,4096));
    const char *vertex="#version 300 es\nout vec2 uv;void main(){vec2 p=vec2(float((gl_VertexID<<1)&2),float(gl_VertexID&2));uv=p;gl_Position=vec4(p*2.-1.,0.,1.);}";
    glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,input.texture,0);
    GLuint gradient=glCreateProgram();glAttachShader(gradient,fixture_shader(GL_VERTEX_SHADER,vertex));
    glAttachShader(gradient,fixture_shader(GL_FRAGMENT_SHADER,"#version 300 es\nprecision highp float;in vec2 uv;out vec4 color;void main(){color=vec4(uv,0.5+0.25*mod(floor(uv.x*64.)+floor(uv.y*32.),2.),1.);}"));
    glLinkProgram(gradient);glUseProgram(gradient);glViewport(0,0,8192,4096);glDrawArrays(GL_TRIANGLES,0,3);glFinish();
    // Capture the Vulkan reference's coordinate/orientation/colour mapping.
    surf_native_slot *first=&s->native_slots[0];glDeleteTextures(1,&first->texture);sn.destroy(display,first->egl_image);surf_native_slot_destroy(first);
    *first=input;first->seq=++s->seq;first->state=2;
    test_reference_guard=1;frame.displayTime++;
    surf_projection_frame((XrSession)(uintptr_t)1,&frame,list[0]);read_destination(s);
    unsigned char reference[2][15][15][4];
    for(int eye=0;eye<2;eye++)for(int y=0;y<15;y++)for(int x=0;x<15;x++)memcpy(reference[eye][y][x],pixels+4*((128+y*128)*4096+eye*2048+128+x*128),4);
    surf_projection_destroy(s);test_reference_guard=0;input=*first;memset(first,0,sizeof(*first));
    GLuint external;glGenTextures(1,&external);glBindTexture(GL_TEXTURE_EXTERNAL_OES,external);sn.bind(GL_TEXTURE_EXTERNAL_OES,input.egl_image);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES,GL_TEXTURE_MIN_FILTER,GL_LINEAR);glTexParameteri(GL_TEXTURE_EXTERNAL_OES,GL_TEXTURE_MAG_FILTER,GL_LINEAR);
    GLuint program=glCreateProgram();glAttachShader(program,fixture_shader(GL_VERTEX_SHADER,vertex));
    glAttachShader(program,fixture_shader(GL_FRAGMENT_SHADER,surf_video_fragment));glLinkProgram(program);
    GLint linked;glGetProgramiv(program,GL_LINK_STATUS,&linked);assert(linked);
    GLint uniforms[8];for(int i=0;i<8;i++)uniforms[i]=glGetUniformLocation(program,surf_video_uniforms[i]);
    for(int i=0;i<SURF_NATIVE_SLOTS;i++){
        surf_native_slot *slot=&s->native_slots[i];if(slot->texture)glDeleteTextures(1,&slot->texture);if(slot->egl_image)sn.destroy(display,slot->egl_image);surf_native_slot_destroy(slot);
        assert(surf_native_allocate(slot,display,4096,2048));
    }
    s->native_project=1;double milliseconds=0;
    for(int i=0;i<120;i++){
        if(s->in_flight){VK_OK(vkWaitForFences(vk.device,1,&s->fence,VK_TRUE,UINT64_MAX));vkResetFences(vk.device,1,&s->fence);s->in_flight=0;surf_native_complete(s);}
        frame.displayTime++;
        float angle=(i%12)*.2f;test_rotation=(XrQuaternionf){0,sinf(angle/2),0,cosf(angle/2)};
        surf_projection_frame((XrSession)(uintptr_t)1,&frame,list[0]);
        surf_video_job job=s->video_job;assert(job.size==2048);
        int slot=-1;for(int j=0;j<SURF_NATIVE_SLOTS;j++)if(s->native_slots[j].state==0){slot=j;break;}assert(slot>=0);
        surf_native_slot *native=&s->native_slots[slot];
        glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,native->texture,0);
        struct timespec start,end;clock_gettime(CLOCK_MONOTONIC,&start);
        surf_video_draw(program,uniforms,external,&job);glFinish();clock_gettime(CLOCK_MONOTONIC,&end);
        if(i==0){puts("fixture: fused draw finished");fflush(stdout);}
        milliseconds+=(end.tv_sec-start.tv_sec)*1000+(end.tv_nsec-start.tv_nsec)/1e6;
        assert(glGetError()==GL_NO_ERROR);
        native->video=job;native->projected=1;native->source_width=8192;native->source_height=4096;
        native->seq=++s->seq;native->state=2;
        // A completed image must keep its old pose when a new frame arrives.
        if(i%2){frame.displayTime++;test_rotation=(XrQuaternionf){0,0,0,1};}
        int uploads=s->uploads;
        if(i==0){
            int before=acquires;wait_timeouts=1;
            assert(!surf_projection_frame((XrSession)(uintptr_t)1,&frame,list[0]));
            assert(surf_projections[0].acquired && acquires==before+1 && !s->in_flight);
            frame.displayTime++;
        }
        const XrCompositionLayerProjection *result=(const void*)surf_projection_frame((XrSession)(uintptr_t)1,&frame,list[0]);
        assert(result && !memcmp(&result->views[0].pose,&job.views[0].pose,sizeof(XrPosef)));
        assert(result->space==job.space && s->native_pending==slot && native->state==3 && s->native_current==-1);
        assert(!memcmp(&result->views[0].fov,&job.views[0].fov,sizeof(XrFovf)));
        assert(job.views[0].fov.angleRight>.6f);
        assert(s->uploads==uploads+1);
        if(i==0){puts("fixture: timeout retry/presentation finished");fflush(stdout);}
        if(i==0){
            read_destination(s);int maximum=0;
            puts("fixture: fused reference readback finished");fflush(stdout);
            for(int eye=0;eye<2;eye++)for(int y=0;y<15;y++)for(int x=0;x<15;x++)for(int c=0;c<4;c++){
                int diff=abs((int)pixels[4*((128+y*128)*4096+eye*2048+128+x*128)+c]-reference[eye][y][x][c]);
                if(diff>maximum)maximum=diff;assert(diff<=2);
            }
            printf("PASS: fused orientation/colour mapping agrees with Vulkan reference (maximum channel difference %d/255).\n",maximum);
        }
        VK_OK(vkWaitForFences(vk.device,1,&s->fence,VK_TRUE,UINT64_MAX));
        int extra=-1;for(int j=0;j<SURF_NATIVE_SLOTS;j++)if(s->native_slots[j].state==0){extra=j;break;}
        assert(extra>=0);s->native_slots[extra].state=2;s->native_slots[extra].seq=++s->seq;
        surf_projection_frame((XrSession)(uintptr_t)1,&frame,list[1]);assert(s->uploads==uploads+1);
        s->native_slots[extra].state=0;
        assert(!surf_projections[0].pipeline && !surf_projections[0].rendered && !surf_projections[0].source);
    }
    // A held panorama follows layer rotation and eye translation using pose
    // metadata alone, including successive frames with no new GPU picture.
    int previous_uploads=s->uploads;
    for(int iteration=0;iteration<20;iteration++){
        frame.displayTime++;test_position=(XrVector3f){.01f*iteration,.002f*iteration,-.003f*iteration};
        for(int eye=0;eye<2;eye++)layers[eye].pose.orientation=lm_axis_angle(0,1,0,.005f*iteration);
        const XrCompositionLayerProjection *held=(const void*)surf_projection_frame((XrSession)(uintptr_t)1,&frame,list[0]);
        assert(held && s->uploads==previous_uploads);
        for(int eye=0;eye<2;eye++){
            assert(!memcmp(&held->views[eye].pose.position,&test_position,sizeof(test_position)));
            XrQuaternionf local=lm_qmul(lm_qconj(layers[eye].pose.orientation),held->views[eye].pose.orientation);
            XrVector3f a=lm_rotate(local,(XrVector3f){.3f,.2f,-1});
            const float *params=surf_projections[0].presented_job.params[eye];
            XrVector3f b=lm_rotate((XrQuaternionf){params[0],params[1],params[2],params[3]},(XrVector3f){.3f,.2f,-1});
            assert(fabsf(a.x-b.x)<.000001f && fabsf(a.y-b.y)<.000001f && fabsf(a.z-b.z)<.000001f);
        }
        XrPosef first=held->views[0].pose;
        held=(const void*)surf_projection_frame((XrSession)(uintptr_t)1,&frame,list[1]);
        assert(!memcmp(&first,&held->views[0].pose,sizeof(first)) && s->uploads==previous_uploads);
    }
    test_position=(XrVector3f){0};
    for(int eye=0;eye<2;eye++)layers[eye].pose.orientation=(XrQuaternionf){0,0,0,1};
    puts("PASS: 20 held-picture pose updates, panorama-local rays, translated eyes, no pose accumulation or GPU copies.");
    if(s->in_flight)VK_OK(vkWaitForFences(vk.device,1,&s->fence,VK_TRUE,UINT64_MAX));
    VK_OK(vkResetCommandBuffer(s->cmd,0));VK_OK(vkBeginCommandBuffer(s->cmd,&begin));
    surf_barrier(s->cmd,destination,VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,VK_ACCESS_TRANSFER_WRITE_BIT,VK_ACCESS_TRANSFER_READ_BIT);
    vkCmdCopyImageToBuffer(s->cmd,destination,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,s->staging,1,&copy);
    VK_OK(vkEndCommandBuffer(s->cmd));VK_OK(vkQueueSubmit(vk.queue,1,&submit,VK_NULL_HANDLE));VK_OK(vkQueueWaitIdle(vk.queue));
    for(int eye=0;eye<2;eye++)for(int y=128;y<2048;y+=128)for(int x=128;x<2048;x+=128){unsigned char *pixel=pixels+4*(y*4096+eye*2048+x);assert(pixel[2]>=127 && pixel[3]==255);}
    surf_projection_destroy(s);
    for(int i=0;i<SURF_NATIVE_SLOTS;i++)assert(s->native_slots[i].state==0);
    printf("PASS: fused external-image stereo projection, moving/older poses, duplicate-eye calls and ownership; GPU draw+finish %.2f ms/view.\n",milliseconds/120);
    glDeleteTextures(1,&external);glDeleteTextures(1,&input.texture);sn.destroy(display,input.egl_image);surf_native_slot_destroy(&input);
    for(int i=0;i<SURF_NATIVE_SLOTS;i++){glDeleteTextures(1,&s->native_slots[i].texture);sn.destroy(display,s->native_slots[i].egl_image);}
    surf_native_destroy(s);memset(s->native_slots,0,sizeof(s->native_slots));
    if(argc>1){
        equirect_res_set=0;
        // Batman's original cutscene maps each source eye over 180 degrees.
        layers[0].centralHorizontalAngle=layers[1].centralHorizontalAngle=LM_PI;
        test_real_worker(s,&frame,list[0],argv[1]);
    }
    test_composite(s);
    vkDestroyFence(vk.device,s->fence,NULL);vkDestroyCommandPool(vk.device,pool,NULL);
    vkUnmapMemory(vk.device,buffer_memory);vkDestroyBuffer(vk.device,s->staging,NULL);vkFreeMemory(vk.device,buffer_memory,NULL);
    vkDestroyDevice(vk.device,NULL);vkDestroyInstance(vk.instance,NULL);free(properties);free(s->pixels);
}
