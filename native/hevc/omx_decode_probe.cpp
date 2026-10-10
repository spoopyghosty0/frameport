// Exercises the actual Android OMX component, including port renegotiation,
// output conversion, timestamps and normal state/buffer lifetimes.
#include <media/hardware/OMXPluginBase.h>
#include <media/hardware/HardwareAPI.h>
#include <media/stagefright/foundation/ColorUtils.h>
#include <media/openmax/OMX_Component.h>
#include <media/openmax/OMX_IndexExt.h>
#include <utils/String8.h>
#include <pthread.h>
#include <dlfcn.h>
#include <cstdio>
#include <cstring>
#include <cassert>
#include <cstdlib>
#include <unistd.h>
#include <android/hardware_buffer.h>
#include <cutils/native_handle.h>
#include <sys/stat.h>
extern "C" const native_handle_t *AHardwareBuffer_getNativeHandle(const AHardwareBuffer*);
namespace android { ANativeWindowBuffer *AHardwareBuffer_to_ANativeWindowBuffer(AHardwareBuffer*); }
extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/bsf.h>
#include <libavutil/time.h>
}
struct Test {
    pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER;
    pthread_cond_t changed=PTHREAD_COND_INITIALIZER;
    OMX_COMPONENTTYPE *component=nullptr;
    OMX_BUFFERHEADERTYPE *input_free[64],*output_free[64];
    unsigned in_count=0,out_count=0,frames=0;
    bool resize=false,eos=false,error=false;
    unsigned state=OMX_StateLoaded,port_event=0;
    OMX_COMMANDTYPE port_command=OMX_CommandStateSet;
    int64_t last_pts=-1;
    uint64_t first_hash=0;
    uint64_t pictures[120]={};
    bool replay=false;
    bool native=false;
    unsigned width=0,height=0;
    AHardwareBuffer *hardware[16]={};
};
static OMX_ERRORTYPE event(OMX_HANDLETYPE,OMX_PTR data,OMX_EVENTTYPE type,OMX_U32 a,OMX_U32 b,OMX_PTR) {
    auto &t=*(Test*)data;pthread_mutex_lock(&t.lock);
    if(type==OMX_EventError){fprintf(stderr,"OMX error: %x %x\n",a,b);t.error=true;}
    // As ACodec: only definition changes reallocate; crop/colour are updates.
    if(type==OMX_EventPortSettingsChanged && a==1 && (b==0 || b==OMX_IndexParamPortDefinition))t.resize=true;
    if(type==OMX_EventCmdComplete) {
        if(a==OMX_CommandStateSet)t.state=b;
        else {t.port_event=b==OMX_ALL?2:b+1;t.port_command=(OMX_COMMANDTYPE)a;}
    }
    pthread_cond_broadcast(&t.changed);pthread_mutex_unlock(&t.lock);return OMX_ErrorNone;
}
static OMX_ERRORTYPE empty(OMX_HANDLETYPE,OMX_PTR data,OMX_BUFFERHEADERTYPE *header) {
    auto &t=*(Test*)data;pthread_mutex_lock(&t.lock);assert(t.in_count<64);
    t.input_free[t.in_count++]=header;pthread_cond_broadcast(&t.changed);pthread_mutex_unlock(&t.lock);return OMX_ErrorNone;
}
static OMX_ERRORTYPE fill(OMX_HANDLETYPE,OMX_PTR data,OMX_BUFFERHEADERTYPE *header) {
    auto &t=*(Test*)data;pthread_mutex_lock(&t.lock);assert(t.out_count<64);
    if(header->nFilledLen) {
        AHardwareBuffer_Planes planes={};AHardwareBuffer *hardware=nullptr;
        if(t.native){
            auto *meta=reinterpret_cast<android::VideoNativeMetadata*>(header->pBuffer);
            for(auto *b:t.hardware)if(b && android::AHardwareBuffer_to_ANativeWindowBuffer(b)==meta->pBuffer)hardware=b;
            assert(hardware);
            assert(AHardwareBuffer_lockPlanes(hardware,AHARDWAREBUFFER_USAGE_CPU_READ_OFTEN,meta->nFenceFd,nullptr,&planes)==0);
            if(meta->nFenceFd>=0)close(meta->nFenceFd);meta->nFenceFd=-1;
        }
        auto byte=[&](unsigned i)->uint8_t{
            if(!t.native)return header->pBuffer[i];
            unsigned y=t.width*t.height,plane=i<y?0:i<y+y/4?1:2;
            unsigned index=plane?i-y-(plane==2?y/4:0):i,width=plane?t.width/2:t.width;
            return ((uint8_t*)planes.planes[plane].data)[(index/width)*planes.planes[plane].rowStride+
                (index%width)*planes.planes[plane].pixelStride];
        };
        if(t.frames<120){
            uint64_t hash=1469598103934665603ull;
            unsigned pixel_bytes=t.native?t.width*t.height*3/2:header->nFilledLen;
            unsigned step=pixel_bytes/1024;if(!step)step=1;
            for(unsigned i=0;i<pixel_bytes;i+=step){hash^=byte(i);hash*=1099511628211ull;}
            if(t.replay && t.pictures[t.frames]!=hash){
                fprintf(stderr,"Picture %u changed after seek/recovery\n",t.frames);t.error=true;
            }
            t.pictures[t.frames]=hash;
        }
        if(!t.frames){
            uint64_t hash=1469598103934665603ull;
            unsigned pixel_bytes=t.native?t.width*t.height*3/2:header->nFilledLen;
            for(unsigned i=0;i<pixel_bytes;i++){hash^=byte(i);hash*=1099511628211ull;}
            printf("First-picture hash: %016llx\n",(unsigned long long)hash);fflush(stdout);
            if(t.first_hash && t.first_hash!=hash){fprintf(stderr,"Seek did not reproduce the first picture\n");t.error=true;}
            t.first_hash=hash;
        }
        if(header->nTimeStamp<t.last_pts){fprintf(stderr,"PTS went backwards\n");t.error=true;}
        t.last_pts=header->nTimeStamp;t.frames++;
        if(hardware)assert(AHardwareBuffer_unlock(hardware,nullptr)==0);
    }
    if(header->nFlags&OMX_BUFFERFLAG_EOS)t.eos=true;
    t.output_free[t.out_count++]=header;
    pthread_cond_broadcast(&t.changed);pthread_mutex_unlock(&t.lock);return OMX_ErrorNone;
}
template<class T>static T parameter(unsigned port) {T p={};p.nSize=sizeof(p);p.nVersion.s.nVersionMajor=1;p.nPortIndex=port;return p;}
static void wait_state(Test &t,unsigned state) {
    pthread_mutex_lock(&t.lock);while(!t.error && t.state!=state)pthread_cond_wait(&t.changed,&t.lock);
    assert(!t.error);pthread_mutex_unlock(&t.lock);
}
static void wait_port(Test &t,OMX_COMMANDTYPE command) {
    pthread_mutex_lock(&t.lock);while(!t.error && !(t.port_event==2 && t.port_command==command))pthread_cond_wait(&t.changed,&t.lock);
    assert(!t.error);t.port_event=0;pthread_mutex_unlock(&t.lock);
}
static void allocate(Test &t,unsigned port,OMX_BUFFERHEADERTYPE **headers,unsigned &count) {
    auto def=parameter<OMX_PARAM_PORTDEFINITIONTYPE>(port);
    assert(t.component->GetParameter(t.component,OMX_IndexParamPortDefinition,&def)==OMX_ErrorNone);
    count=def.nBufferCountActual;assert(count<=16);
    printf("Allocate port %u: %ux%u, %u buffers x %u bytes\n",port,def.format.video.nFrameWidth,def.format.video.nFrameHeight,count,def.nBufferSize);fflush(stdout);
    for(unsigned i=0;i<count;i++)assert(t.component->AllocateBuffer(t.component,&headers[i],port,nullptr,def.nBufferSize)==OMX_ErrorNone);
    if(port==1 && t.native){
        t.width=def.format.video.nFrameWidth;t.height=def.format.video.nFrameHeight;
        for(unsigned i=0;i<count;i++){
            AHardwareBuffer_Desc d={};d.width=t.width;d.height=t.height;d.layers=1;d.format=AHARDWAREBUFFER_FORMAT_Y8Cb8Cr8_420;
            d.usage=AHARDWAREBUFFER_USAGE_CPU_READ_OFTEN|AHARDWAREBUFFER_USAGE_CPU_WRITE_OFTEN|AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE;
            assert(AHardwareBuffer_allocate(&d,&t.hardware[i])==0);
            if(!i){auto *handle=AHardwareBuffer_getNativeHandle(t.hardware[i]);
                printf("Native buffer handles: %d\n",handle->numFds);
                for(int fd=0;fd<handle->numFds;fd++){struct stat st={};assert(fstat(handle->data[fd],&st)==0);
                    printf("  fd %d: device=%llu inode=%llu bytes=%lld\n",fd,(unsigned long long)st.st_dev,
                        (unsigned long long)st.st_ino,(long long)st.st_size);}fflush(stdout);}
            auto *meta=reinterpret_cast<android::VideoNativeMetadata*>(headers[i]->pBuffer);
            meta->eType=android::kMetadataBufferTypeANWBuffer;meta->pBuffer=android::AHardwareBuffer_to_ANativeWindowBuffer(t.hardware[i]);
            meta->nFenceFd=-1;
        }
    }
}
static void queue_output(Test &t) {
    for(;;) {
        pthread_mutex_lock(&t.lock);
        if(!t.out_count || t.resize || t.eos){pthread_mutex_unlock(&t.lock);return;}
        auto *header=t.output_free[--t.out_count];pthread_mutex_unlock(&t.lock);
        header->nFilledLen=0;header->nFlags=0;
        assert(t.component->FillThisBuffer(t.component,header)==OMX_ErrorNone);
    }
}
int main(int argc,char **argv) {
    alarm(60);
    assert(argc==2 || argc==3);const char *path=getenv("FRAMEPORT_PROBE_CODEC");
    void *lib=dlopen(path?path:"/probe/libstagefrighthw.so",RTLD_NOW);
    if(!lib){fprintf(stderr,"%s\n",dlerror());return 1;}
    auto factory=(android::OMXPluginBase*(*)())dlsym(lib,"createOMXPlugin");assert(factory);
    auto *plugin=factory();Test t;OMX_CALLBACKTYPE callbacks={event,empty,fill};
    t.native=argc==3 && (!strcmp(argv[2],"native") || !strcmp(argv[2],"native_cpu") || !strcmp(argv[2],"native_importfail"));
    if(t.native && !strcmp(argv[2],"native_cpu")){
        auto deny=(void(*)())dlsym(RTLD_DEFAULT,"frameport_probe_deny_gpu");assert(deny);deny();
    }
    if(t.native && !strcmp(argv[2],"native_importfail")){
        auto deny=(void(*)())dlsym(RTLD_DEFAULT,"frameport_probe_deny_gpu_import");assert(deny);deny();
    }
    if(argc==3 && (!strcmp(argv[2],"software") || !strcmp(argv[2],"software_rgba"))){
        auto deny=(void(*)())dlsym(RTLD_DEFAULT,"frameport_probe_deny_device");assert(deny);deny();
    }
    if(argc==3 && !strcmp(argv[2],"recover")){
        auto corrupt=(void(*)())dlsym(RTLD_DEFAULT,"frameport_probe_corrupt_picture");assert(corrupt);corrupt();
    }
    if(argc==3 && !strcmp(argv[2],"restartfail")){
        auto deny=(void(*)())dlsym(RTLD_DEFAULT,"frameport_probe_deny_restart");assert(deny);deny();
    }
    AVFormatContext *format=nullptr;assert(avformat_open_input(&format,argv[1],nullptr,nullptr)>=0);
    unsigned video=0;while(video<format->nb_streams && format->streams[video]->codecpar->codec_type!=AVMEDIA_TYPE_VIDEO)video++;
    assert(video<format->nb_streams);auto *stream=format->streams[video];
    const char *name=nullptr,*filter=nullptr;
    switch(stream->codecpar->codec_id){
        case AV_CODEC_ID_HEVC:name="OMX.frameport.hevc.decoder";filter="hevc_mp4toannexb";break;
        case AV_CODEC_ID_H264:name="OMX.frameport.avc.decoder";filter="h264_mp4toannexb";break;
        case AV_CODEC_ID_VP9:name="OMX.frameport.vp9.decoder";break;
        default:assert(false);
    }
    printf("Testing %s\n",name);fflush(stdout);
    assert(plugin->makeComponentInstance(name,&callbacks,&t,&t.component)==OMX_ErrorNone);
    if(t.native)for(const char *name:{"OMX.google.android.index.enableAndroidNativeBuffers","OMX.google.android.index.storeANWBufferInMetadata"}){
        OMX_INDEXTYPE index;assert(t.component->GetExtensionIndex(t.component,(char*)name,&index)==OMX_ErrorNone);
        auto mode=parameter<android::EnableAndroidNativeBuffersParams>(1);mode.enable=OMX_TRUE;
        assert(t.component->SetParameter(t.component,index,&mode)==OMX_ErrorNone);
    }
    // ACodec supplies the container's colour metadata before decoding. Use the
    // same contract so full-range fixtures exercise the intended conversion.
    auto *par=stream->codecpar;
    if(par->color_primaries!=AVCOL_PRI_UNSPECIFIED || par->color_trc!=AVCOL_TRC_UNSPECIFIED ||
        par->color_space!=AVCOL_SPC_UNSPECIFIED || par->color_range!=AVCOL_RANGE_UNSPECIFIED) {
        OMX_INDEXTYPE index;
        char extension[]="OMX.google.android.index.describeColorAspects";
        assert(t.component->GetExtensionIndex(t.component,extension,&index)==OMX_ErrorNone);
        auto aspects=parameter<android::DescribeColorAspectsParams>(1);
        android::ColorUtils::convertIsoColorAspectsToCodecAspects(par->color_primaries,par->color_trc,
            par->color_space,par->color_range==AVCOL_RANGE_JPEG,aspects.sAspects);
        assert(t.component->SetConfig(t.component,index,&aspects)==OMX_ErrorNone);
    }
    if(argc==3 && (!strcmp(argv[2],"rgba") || !strcmp(argv[2],"software_rgba"))) {
        auto mode=parameter<OMX_PARAM_U32TYPE>(1);
        assert(t.component->GetParameter(t.component,(OMX_INDEXTYPE)OMX_IndexParamVideoAndroidRequiresSwRenderer,&mode)==OMX_ErrorNone);
        assert(mode.nU32==1);
    }
    AVBSFContext *bsf=nullptr;
    if(filter){assert(av_bsf_alloc(av_bsf_get_by_name(filter),&bsf)>=0);
        assert(avcodec_parameters_copy(bsf->par_in,stream->codecpar)>=0);bsf->time_base_in=stream->time_base;assert(av_bsf_init(bsf)>=0);}
    auto input_def=parameter<OMX_PARAM_PORTDEFINITIONTYPE>(0);
    assert(t.component->GetParameter(t.component,OMX_IndexParamPortDefinition,&input_def)==OMX_ErrorNone);
    input_def.format.video.nFrameWidth=stream->codecpar->width;input_def.format.video.nFrameHeight=stream->codecpar->height;
    assert(t.component->SetParameter(t.component,OMX_IndexParamPortDefinition,&input_def)==OMX_ErrorNone);
    OMX_BUFFERHEADERTYPE *inputs[16],*outputs[16];unsigned input_count,output_count;
    assert(t.component->SendCommand(t.component,OMX_CommandStateSet,OMX_StateIdle,nullptr)==OMX_ErrorNone);
    allocate(t,0,inputs,input_count);allocate(t,1,outputs,output_count);wait_state(t,OMX_StateIdle);
    for(unsigned i=0;i<input_count;i++)t.input_free[t.in_count++]=inputs[i];
    for(unsigned i=0;i<output_count;i++)t.output_free[t.out_count++]=outputs[i];
    assert(t.component->SendCommand(t.component,OMX_CommandStateSet,OMX_StateExecuting,nullptr)==OMX_ErrorNone);wait_state(t,OMX_StateExecuting);
    AVPacket *packet=av_packet_alloc();
    // midseek: seek mid-playback (no EOS first), as players usually do.
    bool midseek=argc==3 && !strcmp(argv[2],"midseek");
    for(unsigned phase=0;phase<2;phase++) {
    bool sent_config=false,sent_eos=false,resent_config=false;unsigned submitted=0;
    unsigned goal=phase?120:midseek?300:600;
    int64_t start=av_gettime_relative();
    for(;;) {
        queue_output(t);
        pthread_mutex_lock(&t.lock);
        while(!t.error && !t.resize && !t.eos && (sent_eos || !t.in_count) && !t.out_count)pthread_cond_wait(&t.changed,&t.lock);
        assert(!t.error);
        bool resized=t.resize;t.resize=false;bool finished=t.eos;
        pthread_mutex_unlock(&t.lock);
        if(finished)break;
        if(midseek && !phase && submitted>=goal) {
            pthread_mutex_lock(&t.lock);bool enough=t.frames>=200;pthread_mutex_unlock(&t.lock);
            if(enough)break;
            usleep(1000);
        }
        if(resized) {
            assert(t.component->SendCommand(t.component,OMX_CommandPortDisable,1,nullptr)==OMX_ErrorNone);
            pthread_mutex_lock(&t.lock);while(t.out_count<output_count && !t.error)pthread_cond_wait(&t.changed,&t.lock);assert(!t.error);t.out_count=0;pthread_mutex_unlock(&t.lock);
            for(unsigned i=0;i<output_count;i++)assert(t.component->FreeBuffer(t.component,1,outputs[i])==OMX_ErrorNone);
            if(t.native)for(auto &b:t.hardware)if(b){AHardwareBuffer_release(b);b=nullptr;}
            wait_port(t,OMX_CommandPortDisable);
            assert(t.component->SendCommand(t.component,OMX_CommandPortEnable,1,nullptr)==OMX_ErrorNone);
            allocate(t,1,outputs,output_count);wait_port(t,OMX_CommandPortEnable);
            pthread_mutex_lock(&t.lock);for(unsigned i=0;i<output_count;i++)t.output_free[t.out_count++]=outputs[i];pthread_mutex_unlock(&t.lock);
            continue;
        }
        if(sent_eos || (midseek && !phase && submitted>=goal))continue;
        pthread_mutex_lock(&t.lock);
        auto *header=t.in_count?t.input_free[--t.in_count]:nullptr;pthread_mutex_unlock(&t.lock);
        if(!header)continue;
        header->nOffset=0;header->nFilledLen=0;header->nTimeStamp=0;header->nFlags=0;
        // midconfig: parameter sets sent again mid-stream without a flush.
        bool midconfig=argc==3 && !strcmp(argv[2],"midconfig") && !phase && submitted==300 && !resent_config;
        if(!sent_config || midconfig) {
            const auto *params=bsf?bsf->par_out:stream->codecpar;
            if(midconfig){resent_config=true;puts("Resending codec configuration mid-stream.");fflush(stdout);}
            assert((unsigned)params->extradata_size<=header->nAllocLen);
            if(params->extradata_size)memcpy(header->pBuffer,params->extradata,params->extradata_size);
            header->nFilledLen=params->extradata_size;header->nFlags=OMX_BUFFERFLAG_CODECCONFIG;sent_config=true;
        } else if(submitted>=goal) {
            header->nFlags=OMX_BUFFERFLAG_EOS;header->nTimeStamp=goal*1000000/60;sent_eos=true;
        } else {
            int result;
            do{result=av_read_frame(format,packet);assert(result>=0);if(packet->stream_index!=(int)video)av_packet_unref(packet);}while(packet->stream_index!=(int)video);
            if(bsf){assert(av_bsf_send_packet(bsf,packet)>=0);assert(av_bsf_receive_packet(bsf,packet)>=0);}
            av_packet_rescale_ts(packet,stream->time_base,AVRational{1,1000000});
            assert((unsigned)packet->size<=header->nAllocLen);memcpy(header->pBuffer,packet->data,packet->size);header->nFilledLen=packet->size;
            header->nTimeStamp=packet->pts;header->nFlags=OMX_BUFFERFLAG_ENDOFFRAME;
            // Recovery runs model ACodec decoder input, which omits SYNCFRAME.
            if((packet->flags&AV_PKT_FLAG_KEY) && !(argc==3 && !strcmp(argv[2],"recover")))
                header->nFlags|=OMX_BUFFERFLAG_SYNCFRAME;
            if(submitted<2){printf("Input phase %u: pts=%lld key=%d bytes=%d\n",phase,(long long)packet->pts,
                !!(packet->flags&AV_PKT_FLAG_KEY),packet->size);fflush(stdout);}
            av_packet_unref(packet);submitted++;
        }
        assert(t.component->EmptyThisBuffer(t.component,header)==OMX_ErrorNone);
    }
    double seconds=(av_gettime_relative()-start)/1000000.0;
    printf("OMX decode + full-size delivery: %u frames in %.3fs = %.1ffps\n",t.frames,seconds,t.frames/seconds);fflush(stdout);
    assert(midseek && !phase?t.frames>=200:t.frames==goal);
    if(!phase) {
        assert(t.component->SendCommand(t.component,OMX_CommandFlush,OMX_ALL,nullptr)==OMX_ErrorNone);
        wait_port(t,OMX_CommandFlush);
        assert(av_seek_frame(format,video,0,AVSEEK_FLAG_BACKWARD)>=0);if(bsf)av_bsf_flush(bsf);
        pthread_mutex_lock(&t.lock);t.eos=false;t.frames=0;t.last_pts=-1;t.replay=true;pthread_mutex_unlock(&t.lock);
        puts("Replaying after a complete OMX flush and seek to the beginning.");
    }
    }
    assert(t.component->SendCommand(t.component,OMX_CommandStateSet,OMX_StateIdle,nullptr)==OMX_ErrorNone);wait_state(t,OMX_StateIdle);
    assert(t.component->SendCommand(t.component,OMX_CommandStateSet,OMX_StateLoaded,nullptr)==OMX_ErrorNone);
    for(unsigned i=0;i<input_count;i++)assert(t.component->FreeBuffer(t.component,0,inputs[i])==OMX_ErrorNone);
    for(unsigned i=0;i<output_count;i++)assert(t.component->FreeBuffer(t.component,1,outputs[i])==OMX_ErrorNone);
    if(t.native)for(auto &b:t.hardware)if(b){AHardwareBuffer_release(b);b=nullptr;}
    wait_state(t,OMX_StateLoaded);assert(plugin->destroyComponentInstance(t.component)==OMX_ErrorNone);
    delete plugin;dlclose(lib);av_packet_free(&packet);av_bsf_free(&bsf);avformat_close_input(&format);
    if(t.native){auto count=(unsigned(*)())dlsym(RTLD_DEFAULT,"frameport_probe_gpu_submissions");assert(count);
        printf("GPU submissions: %u\n",count());assert(!strcmp(argv[2],"native")?count()>0:count()==0);}
    puts("PASS: OMX decode, ordered timestamps, EOS, seek and shutdown.");
}
