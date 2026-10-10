// SPDX-License-Identifier: GPL-3.0-only
// Android OMX integration for Iris stateful V4L2 video decoding. No OpenXR
// layer/pose manipulation, asset rewriting or package-specific behaviour.
#define LOG_TAG "FramePortVideo"
#include <media/stagefright/omx/SoftVideoDecoderOMXComponent.h>
#include <media/hardware/OMXPluginBase.h>
#include <media/openmax/OMX_IndexExt.h>
#include <media/hardware/HardwareAPI.h>
#include <media/stagefright/foundation/ColorUtils.h>
#include <android/hardware_buffer.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <linux/videodev2.h>
#include <utils/String8.h>
#include <arm_neon.h>
#include <cstring>
#include <cerrno>
#include <pthread.h>
#include <time.h>
#include <poll.h>
#include "yuv_copy.h"
#include "yuv_color.h"
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/opt.h>
extern const YuvConstants kYvuH709Constants, kYvuI601Constants;
extern const YuvConstants kYvuJPEGConstants;
void NV21ToARGBRow_Any_NEON(const uint8_t*,const uint8_t*,uint8_t*,const YuvConstants*,int);
void I422ToARGBRow_Any_NEON(const uint8_t*,const uint8_t*,const uint8_t*,uint8_t*,const YuvConstants*,int);
AHardwareBuffer *ANativeWindowBuffer_getHardwareBuffer(ANativeWindowBuffer*);
const native_handle_t *AHardwareBuffer_getNativeHandle(const AHardwareBuffer*);
int frameport_v4l2_export_frame(const AVFrame*);
}
#include "dma_copy.h"

namespace android {
static const CodecProfileLevel hevc_profiles[]={{OMX_VIDEO_HEVCProfileMain,OMX_VIDEO_HEVCMainTierLevel62}};
// Android reports constrained AVC streams under their own profile values;
// stock decoders list them so format-aware clients can match them.
static const CodecProfileLevel avc_profiles[]={
    {(OMX_U32)OMX_VIDEO_AVCProfileConstrainedBaseline,OMX_VIDEO_AVCLevel62},
    {OMX_VIDEO_AVCProfileBaseline,OMX_VIDEO_AVCLevel62},
    {(OMX_U32)OMX_VIDEO_AVCProfileConstrainedHigh,OMX_VIDEO_AVCLevel62},
    {OMX_VIDEO_AVCProfileMain,OMX_VIDEO_AVCLevel62},
    {OMX_VIDEO_AVCProfileHigh,OMX_VIDEO_AVCLevel62}};
static const CodecProfileLevel vp9_profiles[]={{OMX_VIDEO_VP9Profile0,OMX_VIDEO_VP9Level62}};
struct DecoderKind {
    const char *name,*role,*mime,*hardware,*software;
    OMX_VIDEO_CODINGTYPE coding;
    uint32_t fourcc;
    const CodecProfileLevel *profiles;
    unsigned profile_count;
};
static const DecoderKind kinds[]={
    {"OMX.frameport.hevc.decoder","video_decoder.hevc","video/hevc","hevc_v4l2m2m","hevc",
        OMX_VIDEO_CodingHEVC,V4L2_PIX_FMT_HEVC,hevc_profiles,1},
    {"OMX.frameport.avc.decoder","video_decoder.avc","video/avc","h264_v4l2m2m","h264",
        OMX_VIDEO_CodingAVC,V4L2_PIX_FMT_H264,avc_profiles,5},
    {"OMX.frameport.vp9.decoder","video_decoder.vp9","video/x-vnd.on2.vp9","vp9_v4l2m2m","vp9",
        OMX_VIDEO_CodingVP9,V4L2_PIX_FMT_VP9,vp9_profiles,1}};
static constexpr unsigned kind_count=sizeof(kinds)/sizeof(kinds[0]);
class ColorWorkers {
    struct Worker {ColorWorkers *owner;unsigned band,seen;pthread_t thread;};
    Worker workers[3];unsigned count=0,generation=0,complete=0;
    bool started=false,stopping=false;
    pthread_mutex_t mutex=PTHREAD_MUTEX_INITIALIZER;
    pthread_cond_t work=PTHREAD_COND_INITIALIZER,done=PTHREAD_COND_INITIALIZER;
    AVFrame *frame=nullptr;uint8_t *output=nullptr;size_t stride=0;
    const YuvConstants *matrix=nullptr;
    FramePortYuvPlane planes[3]={};bool native_yuv=false;
    void rows(unsigned band) {
        unsigned bands=count+1;
        if(frame->format==AV_PIX_FMT_YUV420P || frame->format==AV_PIX_FMT_YUVJ420P){
            unsigned begin=(frame->height/2)*band/bands,end=(frame->height/2)*(band+1)/bands;
            if(native_yuv){
                for(unsigned plane=0;plane<3;plane++){
                    unsigned first=plane?begin:2*begin,last=plane?end:2*end,width=plane?frame->width/2:frame->width;
                    for(unsigned row=first;row<last;row++){
                        const uint8_t *src=frame->data[plane]+row*frame->linesize[plane];
                        uint8_t *dst=planes[plane].data+row*planes[plane].row_stride;
                        if(planes[plane].pixel_stride==1)memcpy(dst,src,width);
                        else for(unsigned x=0;x<width;x++)dst[x*planes[plane].pixel_stride]=src[x];
                    }
                }
            }else for(unsigned row=2*begin;row<2*end;row++)
                I422ToARGBRow_Any_NEON(frame->data[0]+row*frame->linesize[0],
                    frame->data[2]+(row/2)*frame->linesize[2],frame->data[1]+(row/2)*frame->linesize[1],
                    output+row*stride*4,matrix,frame->width);
            return;
        }
        if(native_yuv){
            frameport_yuv_copy_band(frame->data[0],frame->data[1],frame->width,frame->height,
                frame->linesize[0],frame->linesize[1],planes,band,bands);return;
        }
        int begin=frame->height*band/bands,end=frame->height*(band+1)/bands;
        for(int row=begin;row<end;row++)
            NV21ToARGBRow_Any_NEON(frame->data[0]+row*frame->linesize[0],
                frame->data[1]+(row/2)*frame->linesize[1],output+row*stride*4,matrix,frame->width);
    }
    static void *thread(void *arg) {
        auto &worker=*static_cast<Worker*>(arg);auto &pool=*worker.owner;unsigned seen=worker.seen;
        pthread_mutex_lock(&pool.mutex);
        for(;;) {
            while(!pool.stopping && seen==pool.generation)pthread_cond_wait(&pool.work,&pool.mutex);
            if(pool.stopping)break;
            seen=pool.generation;pthread_mutex_unlock(&pool.mutex);
            pool.rows(worker.band);
            pthread_mutex_lock(&pool.mutex);
            if(++pool.complete==pool.count)pthread_cond_signal(&pool.done);
        }
        pthread_mutex_unlock(&pool.mutex);return nullptr;
    }
public:
    void convert(AVFrame *source,uint8_t *destination,size_t pitch,const YuvConstants *constants,
        const AHardwareBuffer_Planes *native=nullptr) {
        // Start helpers when a large picture first appears, including after
        // an adaptive stream begins at a lower resolution.
        if(!started && source->width*source->height>=4096*2048) {
            started=true;
            for(unsigned i=0;i<3;i++) {
                // Only this thread advances the generation, so a helper
                // created now waits for the next picture, never a stale one.
                workers[i]={this,i+1,generation,{}};
                if(pthread_create(&workers[i].thread,nullptr,thread,&workers[i]))break;
                count++;
            }
        }
        pthread_mutex_lock(&mutex);
        frame=source;output=destination;stride=pitch;matrix=constants;complete=0;generation++;
        native_yuv=native!=nullptr;
        if(native)for(unsigned i=0;i<3;i++)planes[i]={static_cast<uint8_t*>(native->planes[i].data),
            native->planes[i].rowStride,native->planes[i].pixelStride};
        pthread_cond_broadcast(&work);pthread_mutex_unlock(&mutex);
        rows(0);
        pthread_mutex_lock(&mutex);
        while(complete<count)pthread_cond_wait(&done,&mutex);
        pthread_mutex_unlock(&mutex);
    }
    ~ColorWorkers() {
        pthread_mutex_lock(&mutex);stopping=true;pthread_cond_broadcast(&work);pthread_mutex_unlock(&mutex);
        for(unsigned i=0;i<count;i++)pthread_join(workers[i].thread,nullptr);
        pthread_cond_destroy(&work);pthread_cond_destroy(&done);pthread_mutex_destroy(&mutex);
    }
};
class FramePortVideo final : public SoftVideoDecoderOMXComponent {
    const DecoderKind &kind;
    ColorWorkers color;
    FramePortDmaCopy gpu;
    bool gpu_allowed=true;
    AVCodecContext *codec=nullptr;
    AVCodecParserContext *parser=nullptr;
    AVFrame *frame=av_frame_alloc();
    uint8_t *config=nullptr;
    size_t config_size=0;
    bool failed=false,held=false,drained=false,replace_config=false,surface=false;
    bool native_enabled=false,metadata=false;
    bool hardware_active=false,prefer_software=false,eos_submitted=false,replay_eos=false;
    bool prepend_config=false;
    Vector<AVPacket*> history;
    size_t history_bytes=0;
    ssize_t replay_next=-1;
    int64_t last_output_pts=AV_NOPTS_VALUE,discard_until=AV_NOPTS_VALUE;
    static constexpr OMX_INDEXTYPE native_index=(OMX_INDEXTYPE)0x7f010001;
    static constexpr OMX_INDEXTYPE metadata_index=(OMX_INDEXTYPE)0x7f010002;
    static constexpr OMX_INDEXTYPE usage_index=(OMX_INDEXTYPE)0x7f010003;
    int64_t eos_pts=0;
    static int64_t clockNs() {timespec ts;clock_gettime(CLOCK_MONOTONIC,&ts);return ts.tv_sec*1000000000ll+ts.tv_nsec;}
    int64_t stats_start=0, stats_lock=0, stats_convert=0, stats_unlock=0;
    unsigned stats_frames=0,stats_gpu=0;
    bool gpuCopy(AHardwareBuffer *hardware,const AHardwareBuffer_Planes &planes) {
        if(planes.planes[1].pixelStride!=2 || planes.planes[2].pixelStride!=2 ||
            planes.planes[1].rowStride!=planes.planes[2].rowStride ||
            (uintptr_t)planes.planes[2].data!=(uintptr_t)planes.planes[1].data+1 ||
            (uintptr_t)planes.planes[1].data<(uintptr_t)planes.planes[0].data)return false;
        const auto *handle=AHardwareBuffer_getNativeHandle(hardware);
        // CrOS gralloc includes duplicate plane fds and a metadata fd. The
        // first fd holds the linear pixels; copy() bounds-checks both planes.
        if(!handle || handle->numFds<1)return false;
        int source=frameport_v4l2_export_frame(frame);if(source<0)return false;
        uint64_t src_uv=(uintptr_t)frame->data[1]-(uintptr_t)frame->data[0];
        uint64_t dst_uv=(uintptr_t)planes.planes[1].data-(uintptr_t)planes.planes[0].data;
        std::vector<VkBufferCopy> regions;
        auto append=[&](VkDeviceSize src,VkDeviceSize dst,VkDeviceSize bytes){
            if(!regions.empty() && regions.back().srcOffset+regions.back().size==src &&
                regions.back().dstOffset+regions.back().size==dst)regions.back().size+=bytes;
            else regions.push_back({src,dst,bytes});
        };
        for(int row=0;row<frame->height;row++)
            append((uint64_t)row*frame->linesize[0],(uint64_t)row*planes.planes[0].rowStride,frame->width);
        for(int row=0;row<frame->height/2;row++)
            append(src_uv+(uint64_t)row*frame->linesize[1],dst_uv+(uint64_t)row*planes.planes[1].rowStride,frame->width);
        bool result=gpu.copy(source,handle->data[0],regions.data(),regions.size());close(source);return result;
    }
    void clearHistory() {
        for(size_t i=0;i<history.size();i++){AVPacket *packet=history[i];av_packet_free(&packet);}
        history.clear();history_bytes=0;replay_next=-1;
    }
    void rememberInput(const AVPacket *packet) {
        if(!hardware_active)return;
        if(packet->flags&AV_PKT_FLAG_KEY){
            // Keep the preceding GOP too: the new keyframe can be submitted
            // before older reordered pictures have finished coming out.
            ssize_t previous=-1;
            for(size_t i=0;i<history.size();i++)if(history[i]->flags&AV_PKT_FLAG_KEY)previous=i;
            if(previous>0){
                for(ssize_t i=0;i<previous;i++){
                    AVPacket *old=history[i];history_bytes-=old->size;av_packet_free(&old);
                }
                history.removeItemsAt(0,previous);
            }
        }
        // References to compressed input, not copies of decoded panoramas.
        // Bound memory for unusually long GOPs or malformed streams.
        if(history_bytes+(size_t)packet->size>64*1024*1024 || history.size()>=512)clearHistory();
        if(history.empty() && !(packet->flags&AV_PKT_FLAG_KEY))return;
        AVPacket *copy=av_packet_clone(packet);
        if(!copy)return;
        if(history.add(copy)<0){av_packet_free(&copy);return;}
        history_bytes+=copy->size;
    }
    bool annexB() const {
        // H.264/HEVC parameter sets with start codes can go in-band. VP9's
        // configuration is container metadata, never bitstream data.
        return kind.coding!=OMX_VIDEO_CodingVP9 && config_size>=4 && !config[0] && !config[1] &&
            (config[2]==1 || (!config[2] && config[3]==1));
    }
    void fail(int result,const char *operation) {
        char message[128];av_strerror(result,message,sizeof(message));
        ALOGE("%s failed: %s",operation,message);
        failed=true;notify(OMX_EventError,OMX_ErrorHardware,0,nullptr);
    }
    static unsigned captureBuffers(const DecoderKind &kind,uint64_t pixels) {
        // FFmpeg's general-purpose capture budget (20) suits ordinary sizes.
        // Reference pictures shrink as pictures grow (HEVC level 6.x DPB
        // limits; H.264 level 6.2 allows fewer), so 20 panorama-sized buffers
        // would hold about 1 GB at 8K. VP9 can keep 8 references at any size.
        // Unknown sizes keep the full budget.
        const uint64_t max_luma=35651584;
        unsigned references=pixels<=max_luma/4?16:pixels<=max_luma/2?12:pixels<=max_luma*3/4?8:6;
        if(kind.coding==OMX_VIDEO_CodingVP9 && references<9)references=9;
        // Room for the picture being decoded, one held for output and queueing.
        return references+4<20?references+4:20;
    }
    int openCodec(const char *name,bool hardware) {
        const AVCodec *implementation=avcodec_find_decoder_by_name(name);
        if(!implementation || !frame)return AVERROR(ENOMEM);
        codec=avcodec_alloc_context3(implementation);
        if(!codec)return AVERROR(ENOMEM);
        const auto &input=editPortInfo(kInputPortIndex)->mDef.format.video;
        codec->width=codec->coded_width=input.nFrameWidth;
        codec->height=codec->coded_height=input.nFrameHeight;
        codec->pkt_timebase=AVRational{1,1000000};
        codec->pix_fmt=AV_PIX_FMT_NV12;
        if(config_size) {
            codec->extradata=(uint8_t*)av_mallocz(config_size+AV_INPUT_BUFFER_PADDING_SIZE);
            if(!codec->extradata)return AVERROR(ENOMEM);
            memcpy(codec->extradata,config,config_size);codec->extradata_size=(int)config_size;
        }
        AVDictionary *options=nullptr;
        if(hardware){
            av_dict_set(&options,"num_output_buffers","2",0);
            // Leave room for reference/reordered pictures: a four-buffer
            // budget stalled a valid H.264 stream on Iris.
            av_dict_set_int(&options,"num_capture_buffers",
                captureBuffers(kind,(uint64_t)input.nFrameWidth*input.nFrameHeight),0);
        }else{codec->thread_count=4;codec->thread_type=FF_THREAD_FRAME|FF_THREAD_SLICE;}
        int result=avcodec_open2(codec,implementation,&options);av_dict_free(&options);
        return result;
    }
    bool openDecoder() {
        if(codec)return true;
        int result=prefer_software?AVERROR(ENODEV):openCodec(kind.hardware,true);
        hardware_active=result>=0;
        if(result<0){
            // Availability can change after enumeration (another app/Steam
            // opens Iris). Native MediaCodec clients do not all retry a stock
            // component, so retain playback within this selected component.
            if(prefer_software)ALOGI("%s: continuing in software for this session",kind.mime);
            else ALOGW("Iris %s initialization failed (%d); using software decoding",kind.mime,result);
            gpu.clearBuffers();avcodec_free_context(&codec);
            result=openCodec(kind.software,false);
            if(result<0){gpu.clearBuffers();avcodec_free_context(&codec);fail(result,"software fallback initialization");return false;}
        }else ALOGI("Iris hardware %s decoder active: %dx%d",kind.mime,codec->coded_width,codec->coded_height);
        prepend_config=hardware_active && annexB();
        parser=av_parser_init(codec->codec_id);
        if(parser)parser->flags|=PARSER_FLAG_COMPLETE_FRAMES;
        return true;
    }
    bool recoverHardware(const char *operation) {
        if(!hardware_active || history.empty() || !(history[0]->flags&AV_PKT_FLAG_KEY)){
            fail(AVERROR_INVALIDDATA,operation);return false;
        }
        ALOGW("Iris %s failed during %s; replaying %zu compressed packets in software, last pts=%lld",kind.mime,operation,history.size(),(long long)last_output_pts);
        av_frame_unref(frame);held=false;av_parser_close(parser);parser=nullptr;gpu.clearBuffers();avcodec_free_context(&codec);
        hardware_active=false;prefer_software=true;
        int result=openCodec(kind.software,false);
        if(result<0){fail(result,"software recovery initialization");return false;}
        replay_next=0;replay_eos=eos_submitted;eos_submitted=false;discard_until=last_output_pts;
        return true;
    }
    void consumeInput(BufferInfo *info) {
        OMX_BUFFERHEADERTYPE *header=info->mHeader;
        getPortQueue(kInputPortIndex).erase(getPortQueue(kInputPortIndex).begin());
        header->nOffset=0;header->nFilledLen=0;info->mOwnedByUs=false;
        notifyEmptyBufferDone(header);
    }
    void reportBitstreamColor() {
        // Like Android's stock decoders, pass signalled colour information
        // to ACodec, which uses it for the surface dataspace. Container
        // values still take precedence (getColorAspectPreference).
        if(frame->color_primaries==AVCOL_PRI_UNSPECIFIED && frame->color_trc==AVCOL_TRC_UNSPECIFIED &&
            frame->colorspace==AVCOL_SPC_UNSPECIFIED && frame->color_range==AVCOL_RANGE_UNSPECIFIED)return;
        // FFmpeg's colour enums use the ISO/IEC 23091-2 code points.
        ColorAspects aspects;
        ColorUtils::convertIsoColorAspectsToCodecAspects(frame->color_primaries,frame->color_trc,
            frame->colorspace,frame->color_range==AVCOL_RANGE_JPEG,aspects);
        if(colorAspectsDiffer(aspects,mBitstreamColorAspects)){
            ALOGI("%s bitstream colour: primaries %d, transfer %d, matrix %d, range %d",kind.mime,
                frame->color_primaries,frame->color_trc,frame->colorspace,frame->color_range);
            mBitstreamColorAspects=aspects;handleColorAspectsChange();
        }
    }
    const YuvConstants *rgbaMatrix() {
        ColorAspects aspects;
        {Mutex::Autolock lock(mColorAspectsLock);aspects=mFinalColorAspects;}
        // Unsignalled HD video is conventionally BT.709, as Android assumes.
        bool bt709=aspects.mMatrixCoeffs==ColorAspects::MatrixBT709_5 ||
            (aspects.mMatrixCoeffs==ColorAspects::MatrixUnspecified && (frame->width>=1280 || frame->height>=720));
        if(aspects.mRange==ColorAspects::RangeFull)return bt709?&frameport_full709_rgba:&kYvuJPEGConstants;
        return bt709?&kYvuH709Constants:&kYvuI601Constants;
    }
    bool outputFrame() {
        // Hardware errors are recovered in software. Software decoders
        // conceal damage themselves; output their picture as stock decoders do.
        if(frame->decode_error_flags && hardware_active)return recoverHardware("decoded picture");
        reportBitstreamColor();
        bool reset=false;
        handlePortSettingsChange(&reset,frame->width,frame->height,
            native_enabled?(OMX_COLOR_FORMATTYPE)AHARDWAREBUFFER_FORMAT_Y8Cb8Cr8_420:
                surface?OMX_COLOR_Format32BitRGBA8888:OMX_COLOR_FormatYUV420Planar);
        rgbaPort();
        if(reset)return false;
        auto &queue=getPortQueue(kOutputPortIndex);
        if(queue.empty())return false;
        BufferInfo *info=*queue.begin();OMX_BUFFERHEADERTYPE *header=info->mHeader;
        size_t stride=outputBufferWidth(),height=outputBufferHeight();
        size_t bytes=native_enabled?sizeof(VideoNativeMetadata):surface?stride*height*4:stride*height*3/2;
        bool planar=frame->format==AV_PIX_FMT_YUV420P || frame->format==AV_PIX_FMT_YUVJ420P;
        if(bytes>header->nAllocLen || (frame->format!=AV_PIX_FMT_NV12 && !planar) ||
           frame->width<1 || frame->height<1 || (frame->width&1) || (frame->height&1) ||
           (size_t)frame->width>stride || (size_t)frame->height>height) {
            fail(AVERROR(EINVAL),"output buffer format");return false;
        }
        // Native surfaces keep YUV planes for GPU colour conversion. Legacy
        // software surfaces use parallel NEON RGBA conversion, bypassing
        // Lepton's slow scalar RGB565 renderer.
        if(surface || native_enabled) {
            int64_t before=clockNs(),locked=before,converted=before;
            const YuvConstants *matrix=native_enabled?nullptr:rgbaMatrix();
            uint8_t *destination=header->pBuffer;
            AHardwareBuffer *hardware=nullptr;VideoNativeMetadata *native=nullptr;
            AHardwareBuffer_Planes planes={};
            bool used_gpu=false;
            if(native_enabled) {
                native=reinterpret_cast<VideoNativeMetadata*>(header->pBuffer);
                if(!metadata || native->eType!=kMetadataBufferTypeANWBuffer || !native->pBuffer) {
                    fail(AVERROR(EINVAL),"native output metadata");return false;
                }
                hardware=ANativeWindowBuffer_getHardwareBuffer(native->pBuffer);
                if(!hardware){fail(AVERROR(EINVAL),"native hardware buffer");return false;}
                AHardwareBuffer_Desc desc={};AHardwareBuffer_describe(hardware,&desc);
                if(desc.format!=AHARDWAREBUFFER_FORMAT_Y8Cb8Cr8_420 || desc.width<(unsigned)frame->width ||
                    desc.height<(unsigned)frame->height) {
                    fail(AVERROR(EINVAL),"native output geometry");return false;
                }
                int result=AHardwareBuffer_lockPlanes(hardware,AHARDWAREBUFFER_USAGE_CPU_WRITE_OFTEN,
                    native->nFenceFd,nullptr,&planes);
                if(native->nFenceFd>=0)close(native->nFenceFd);
                native->nFenceFd=-1;
                if(result){fail(AVERROR(EIO),"native output lock");return false;}
                if(planes.planeCount!=3 || planes.planes[0].pixelStride!=1 ||
                    planes.planes[0].rowStride<(unsigned)frame->width ||
                    !planes.planes[0].data || !planes.planes[1].data || !planes.planes[2].data ||
                    !planes.planes[1].pixelStride || !planes.planes[2].pixelStride ||
                    planes.planes[1].rowStride<(frame->width/2-1)*planes.planes[1].pixelStride+1 ||
                    planes.planes[2].rowStride<(frame->width/2-1)*planes.planes[2].pixelStride+1) {
                    AHardwareBuffer_unlock(hardware,&native->nFenceFd);
                    fail(AVERROR(EINVAL),"native YUV planes");return false;
                }
                locked=clockNs();
                if(gpu_allowed && hardware_active && !planar && !(frame->width&3) &&
                    (uint64_t)frame->width*frame->height>=4096*2048 && gpu.available()) {
                    if(AHardwareBuffer_unlock(hardware,&native->nFenceFd)){
                        fail(AVERROR(EIO),"native layout unlock");return false;
                    }
                    if(native->nFenceFd>=0){
                        pollfd fence={native->nFenceFd,POLLIN,0};int waited;
                        do{waited=poll(&fence,1,10000);}while(waited<0 && errno==EINTR);
                        close(native->nFenceFd);native->nFenceFd=-1;
                        if(waited<=0 || !(fence.revents&POLLIN)){fail(AVERROR(EIO),"native layout fence");return false;}
                    }
                    used_gpu=gpuCopy(hardware,planes);
                    if(!used_gpu){
                        gpu_allowed=false;
                        if(gpu.failedAfterSubmit()){fail(AVERROR(EIO),"GPU video transfer");return false;}
                        if(AHardwareBuffer_lockPlanes(hardware,AHARDWAREBUFFER_USAGE_CPU_WRITE_OFTEN,-1,nullptr,&planes)){
                            fail(AVERROR(EIO),"native CPU fallback lock");return false;
                        }
                        ALOGW("GPU video buffer sharing unavailable; retaining CPU copy");
                    }
                }
            }
            if(!native_enabled)locked=clockNs();
            if(native_enabled && !used_gpu)color.convert(frame,nullptr,0,nullptr,&planes);
            else if(!native_enabled)color.convert(frame,destination,stride,matrix);
            converted=clockNs();
            if(hardware && !used_gpu && AHardwareBuffer_unlock(hardware,&native->nFenceFd)) {
                fail(AVERROR(EIO),"native output unlock");return false;
            }
            int64_t after=clockNs();
            stats_lock+=locked-before;stats_convert+=converted-locked;stats_unlock+=after-converted;
            if(!stats_start)stats_start=before;
            stats_frames++;
            if(used_gpu)stats_gpu++;
            if(after-stats_start>=5000000000ll) {
                ALOGI("output %.1f fps, native=%d, gpu=%u/%u, lock %.2f ms, copy/convert %.2f ms, unlock %.2f ms, pts=%lld",
                    stats_frames*1e9/(after-stats_start),native_enabled,stats_gpu,stats_frames,
                    stats_lock/(1e6*stats_frames),stats_convert/(1e6*stats_frames),stats_unlock/(1e6*stats_frames),
                    (long long)frame->pts);
                stats_start=after;stats_frames=stats_gpu=0;stats_lock=stats_convert=stats_unlock=0;
            }
        } else {
            // Byte buffers hold planar YUV; the same worker bands copy the
            // planes (in parallel for panorama sizes).
            uint8_t *y=header->pBuffer,*u=y+stride*height,*v=u+stride*height/4;
            AHardwareBuffer_Planes planes={};planes.planeCount=3;
            planes.planes[0]={y,1,(uint32_t)stride};
            planes.planes[1]={u,1,(uint32_t)(stride/2)};
            planes.planes[2]={v,1,(uint32_t)(stride/2)};
            color.convert(frame,nullptr,0,nullptr,&planes);
        }
        header->nOffset=0;header->nFilledLen=(OMX_U32)bytes;
        header->nTimeStamp=frame->pts==AV_NOPTS_VALUE?frame->best_effort_timestamp:frame->pts;
        last_output_pts=header->nTimeStamp;
        header->nFlags=OMX_BUFFERFLAG_ENDOFFRAME;
        queue.erase(queue.begin());info->mOwnedByUs=false;notifyFillBufferDone(header);
        av_frame_unref(frame);held=false;return true;
    }
protected:
    void rgbaPort() {
        if(native_enabled) {
            auto &def=editPortInfo(kOutputPortIndex)->mDef;
            def.format.video.eColorFormat=(OMX_COLOR_FORMATTYPE)AHARDWAREBUFFER_FORMAT_Y8Cb8Cr8_420;
            def.nBufferSize=sizeof(VideoNativeMetadata);return;
        }
        if(mOutputFormat!=OMX_COLOR_Format32BitRGBA8888)return;
        auto &def=editPortInfo(kOutputPortIndex)->mDef;
        def.format.video.eColorFormat=OMX_COLOR_Format32BitRGBA8888;
        def.nBufferSize=outputBufferWidth()*outputBufferHeight()*4;
    }
    OMX_ERRORTYPE internalGetParameter(OMX_INDEXTYPE index, OMX_PTR params) override {
        if(index==(OMX_INDEXTYPE)OMX_IndexParamVideoAndroidRequiresSwRenderer) {
            auto *value=static_cast<OMX_PARAM_U32TYPE*>(params);
            if(!value || value->nSize<sizeof(*value) || value->nPortIndex!=kOutputPortIndex)
                return OMX_ErrorBadParameter;
            // Decoding happens on Iris; Android's Surface renderer consumes
            // our ordinary planar buffers rather than vendor ANW metadata.
            surface=!native_enabled;value->nU32=native_enabled?0:1;return OMX_ErrorNone;
        }
        if(index==usage_index) {
            auto *value=static_cast<GetAndroidNativeBufferUsageParams*>(params);
            if(!value || value->nSize<sizeof(*value) || value->nPortIndex!=kOutputPortIndex)return OMX_ErrorBadParameter;
            value->nUsage=AHARDWAREBUFFER_USAGE_CPU_WRITE_OFTEN|AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE;
            return OMX_ErrorNone;
        }
        if(index==OMX_IndexParamPortDefinition)rgbaPort();
        OMX_ERRORTYPE result=SoftVideoDecoderOMXComponent::internalGetParameter(index,params);
        if(result==OMX_ErrorNone && index==OMX_IndexParamVideoPortFormat) {
            auto *value=static_cast<OMX_VIDEO_PARAM_PORTFORMATTYPE*>(params);
            if(value->nPortIndex==kOutputPortIndex)value->eColorFormat=native_enabled?
                (OMX_COLOR_FORMATTYPE)AHARDWAREBUFFER_FORMAT_Y8Cb8Cr8_420:mOutputFormat;
        }
        return result;
    }
    OMX_ERRORTYPE getExtensionIndex(const char *name,OMX_INDEXTYPE *index) override {
        if(!strcmp(name,"OMX.google.android.index.enableAndroidNativeBuffers"))*index=native_index;
        else if(!strcmp(name,"OMX.google.android.index.storeANWBufferInMetadata"))*index=metadata_index;
        else if(!strcmp(name,"OMX.google.android.index.getAndroidNativeBufferUsage"))*index=usage_index;
        else return SoftVideoDecoderOMXComponent::getExtensionIndex(name,index);
        return OMX_ErrorNone;
    }
    OMX_ERRORTYPE internalSetParameter(OMX_INDEXTYPE index,OMX_PTR params) override {
        if(index==native_index || index==metadata_index) {
            auto *value=static_cast<EnableAndroidNativeBuffersParams*>(params);
            if(!value || value->nSize<sizeof(*value) || value->nPortIndex!=kOutputPortIndex)return OMX_ErrorBadParameter;
            if(index==native_index)native_enabled=value->enable;
            else metadata=value->enable;
            rgbaPort();return OMX_ErrorNone;
        }
        if(index==OMX_IndexParamVideoPortFormat && native_enabled) {
            auto *value=static_cast<OMX_VIDEO_PARAM_PORTFORMATTYPE*>(params);
            if(value && value->nSize>=sizeof(*value) && value->nPortIndex==kOutputPortIndex) {
                auto copy=*value;copy.eColorFormat=OMX_COLOR_FormatYUV420Planar;
                return SoftVideoDecoderOMXComponent::internalSetParameter(index,&copy);
            }
        }
        OMX_ERRORTYPE result=SoftVideoDecoderOMXComponent::internalSetParameter(index,params);
        rgbaPort();return result;
    }
    int getColorAspectPreference() override {return kPreferContainer;}
    void onQueueFilled(OMX_U32) override {
        if(failed || mOutputPortSettingsChange!=NONE || drained)return;
        auto &inputs=getPortQueue(kInputPortIndex);auto &outputs=getPortQueue(kOutputPortIndex);
        while(!outputs.empty()) {
            if(held){if(!outputFrame())return;continue;}
            if(codec) {
                int result=avcodec_receive_frame(codec,frame);
                if(result==0){
                    int64_t pts=frame->pts==AV_NOPTS_VALUE?frame->best_effort_timestamp:frame->pts;
                    if(discard_until!=AV_NOPTS_VALUE && pts!=AV_NOPTS_VALUE && pts<=discard_until){av_frame_unref(frame);continue;}
                    discard_until=AV_NOPTS_VALUE;held=true;continue;
                }
                if(result==AVERROR_EOF) {
                    BufferInfo *info=*outputs.begin();auto *header=info->mHeader;
                    header->nOffset=0;header->nFilledLen=0;header->nTimeStamp=eos_pts;
                    header->nFlags=OMX_BUFFERFLAG_EOS;outputs.erase(outputs.begin());
                    info->mOwnedByUs=false;notifyFillBufferDone(header);drained=true;return;
                }
                if(result!=AVERROR(EAGAIN)){
                    if(hardware_active && recoverHardware("frame retrieval"))continue;
                    if(!failed)fail(result,"frame retrieval");return;
                }
            }
            if(replay_next>=0){
                if((size_t)replay_next<history.size()){
                    int result=avcodec_send_packet(codec,history[replay_next]);
                    if(result<0){fail(result,"software recovery packet");return;}
                    replay_next++;continue;
                }
                clearHistory();
                if(replay_eos){
                    int result=avcodec_send_packet(codec,nullptr);
                    if(result<0){fail(result,"software recovery drain");return;}
                    replay_eos=false;eos_submitted=true;continue;
                }
            }
            if(inputs.empty())return;
            BufferInfo *info=*inputs.begin();auto *header=info->mHeader;
            if(header->nOffset>header->nAllocLen || header->nFilledLen>header->nAllocLen-header->nOffset) {
                fail(AVERROR(EINVAL),"input buffer bounds");return;
            }
            if(header->nFlags&OMX_BUFFERFLAG_CODECCONFIG) {
                if(replace_config){av_freep(&config);config_size=0;replace_config=false;}
                if(config_size+header->nFilledLen>1024*1024) {
                    fail(AVERROR(EINVAL),"oversized codec configuration");return;
                }
                if(header->nFilledLen) {
                    void *grown=av_realloc(config,config_size+header->nFilledLen);
                    if(!grown){fail(AVERROR(ENOMEM),"codec configuration storage");return;}
                    config=(uint8_t*)grown;
                    memcpy(config+config_size,header->pBuffer+header->nOffset,header->nFilledLen);
                    config_size+=header->nFilledLen;
                }
                // Streaming apps and restarted encoders can send parameter
                // sets again mid-stream. Stock decoders accept them; submit
                // them in-band before the next access unit.
                if(codec)prepend_config=annexB();
                consumeInput(info);continue;
            }
            if(!openDecoder())return;
            if(header->nFilledLen) {
                AVPacket *packet=av_packet_alloc();
                if(!packet){fail(AVERROR(ENOMEM),"input packet");return;}
                // Android delivers parameter sets in separate CODECCONFIG
                // buffers. V4L2 expects Annex B parameter sets in its input
                // stream; FFmpeg's V4L2 wrapper does not submit extradata.
                size_t prefix=prepend_config?config_size:0;
                int result=av_new_packet(packet,(int)(prefix+header->nFilledLen));
                if(result>=0) {
                    if(prefix)memcpy(packet->data,config,prefix);
                    memcpy(packet->data+prefix,header->pBuffer+header->nOffset,header->nFilledLen);
                    packet->pts=packet->dts=header->nTimeStamp;
                    if(header->nFlags&OMX_BUFFERFLAG_SYNCFRAME)packet->flags|=AV_PKT_FLAG_KEY;
                    // ACodec does not preserve MediaCodec's KEY_FRAME flag
                    // on decoder input. Parse complete access units so driver
                    // recovery also works for ordinary Android clients.
                    if(parser){
                        uint8_t *parsed=nullptr;int parsed_size=0;
                        av_parser_parse2(parser,codec,&parsed,&parsed_size,packet->data,packet->size,
                            packet->pts,packet->dts,AV_NOPTS_VALUE);
                        if(parser->key_frame==1)packet->flags|=AV_PKT_FLAG_KEY;
                    }
                    result=avcodec_send_packet(codec,packet);
                    if(result>=0){prepend_config=false;rememberInput(packet);}
                }
                av_packet_free(&packet);
                if(result==AVERROR(EAGAIN))return;
                if(result<0){
                    if(hardware_active && recoverHardware("packet submission"))continue;
                    if(!failed)fail(result,"packet submission");return;
                }
                header->nFilledLen=0;
            }
            if(header->nFlags&OMX_BUFFERFLAG_EOS) {
                eos_pts=header->nTimeStamp;
                int result=avcodec_send_packet(codec,nullptr);
                if(result==AVERROR(EAGAIN))return;
                if(result<0){fail(result,"stream drain");return;}
                eos_submitted=true;
            }
            replace_config=true;  // a later configuration replaces this one
            consumeInput(info);
        }
    }
    void onPortFlushCompleted(OMX_U32 port) override {
        if(port==kInputPortIndex) {
            gpu.clearBuffers();
            // Release the held picture first (its V4L2 buffer returns to the driver).
            av_frame_unref(frame);av_parser_close(parser);parser=nullptr;clearHistory();
            // Software decoders flush in place. A hardware session restarts in
            // place only after a completed drain (replay/loop after EOS):
            // restarting a running Iris session's queues failed its admission
            // check and left buffers unreturned, so other seeks reopen it.
            bool usable=codec && !failed && (!hardware_active || drained);
            eos_submitted=false;replay_eos=false;
            last_output_pts=discard_until=AV_NOPTS_VALUE;
            held=false;drained=false;failed=false;replace_config=true;
            if(usable) {
                // Keeps the buffers and the Iris session (no reallocation).
                avcodec_flush_buffers(codec);
                int64_t restart_error=0;
                if(hardware_active && (av_opt_get_int(codec->priv_data,"frameport_flush_error",0,&restart_error)<0 ||
                    restart_error<0)) {
                    ALOGW("Iris %s replay restart failed (%lld); continuing in software",kind.mime,(long long)restart_error);
                    gpu.clearBuffers();avcodec_free_context(&codec);hardware_active=false;prefer_software=true;
                } else {
                    parser=av_parser_init(codec->codec_id);
                    if(parser)parser->flags|=PARSER_FLAG_COMPLETE_FRAMES;
                    prepend_config=annexB();
                }
            } else {
                if(hardware_active && kind.coding==OMX_VIDEO_CodingVP9){
                    // Reopening Iris VP9 sessions after a seek damaged pictures and
                    // faulted the kernel on this Frame: continue in software.
                    prefer_software=true;
                    ALOGW("VP9 seek: continuing in software rather than reopening Iris");
                }
                gpu.clearBuffers();avcodec_free_context(&codec);hardware_active=false;
            }
        }
    }
    void onReset() override {
        gpu_allowed=true;
        av_frame_unref(frame);av_parser_close(parser);parser=nullptr;gpu.clearBuffers();avcodec_free_context(&codec);av_freep(&config);config_size=0;clearHistory();
        hardware_active=false;prefer_software=false;eos_submitted=false;replay_eos=false;
        last_output_pts=discard_until=AV_NOPTS_VALUE;
        held=false;drained=false;failed=false;replace_config=false;
        SoftVideoDecoderOMXComponent::onReset();
        surface=false;native_enabled=false;metadata=false;mOutputFormat=OMX_COLOR_FormatYUV420Planar;
        updatePortDefinitions(true,false);
    }
    ~FramePortVideo() override {av_frame_free(&frame);av_parser_close(parser);gpu.clearBuffers();avcodec_free_context(&codec);av_freep(&config);clearHistory();}
public:
    FramePortVideo(const DecoderKind &type,const OMX_CALLBACKTYPE *callbacks,OMX_PTR appData,OMX_COMPONENTTYPE **component)
        :SoftVideoDecoderOMXComponent(type.name,type.role,type.coding,
            type.profiles,type.profile_count,320,240,callbacks,appData,component),kind(type) {
        initPorts(8,2*1024*1024,4,type.mime,1);
    }
};
class FramePortOMXPlugin final : public OMXPluginBase {
    bool available[kind_count]={};
    static bool probeCapacity(const DecoderKind &kind) {
        // Check each coded format separately, at ordinary video geometry.
        // A rejected 8K session must not hide hardware from a 1080p app.
        int fd=open("/dev/video-dec0",O_RDWR|O_NONBLOCK);
        if(fd<0)return false;
        bool supported=false;
        for(unsigned i=0;;i++){
            v4l2_fmtdesc desc={};desc.index=i;desc.type=V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
            if(ioctl(fd,VIDIOC_ENUM_FMT,&desc)<0)break;
            if(desc.pixelformat==kind.fourcc)supported=true;
        }
        if(!supported){close(fd);return false;}
        v4l2_format format={};format.type=V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
        format.fmt.pix_mp.width=1920;format.fmt.pix_mp.height=1080;
        format.fmt.pix_mp.pixelformat=kind.fourcc;format.fmt.pix_mp.num_planes=1;
        bool ok=ioctl(fd,VIDIOC_S_FMT,&format)==0 && format.fmt.pix_mp.pixelformat==kind.fourcc;
        format.type=V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;format.fmt.pix_mp.pixelformat=V4L2_PIX_FMT_NV12;
        if(ok)ok=ioctl(fd,VIDIOC_S_FMT,&format)==0 && format.fmt.pix_mp.pixelformat==V4L2_PIX_FMT_NV12;
        v4l2_requestbuffers buffers={};buffers.count=2;
        buffers.type=V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;buffers.memory=V4L2_MEMORY_MMAP;
        if(ok)ok=ioctl(fd,VIDIOC_REQBUFS,&buffers)==0;
        close(fd);return ok;
    }
public:
    FramePortOMXPlugin() {
        for(unsigned i=0;i<kind_count;i++){
            available[i]=probeCapacity(kinds[i]);
            if(!available[i])ALOGW("Iris %s unavailable; keeping Android's stock decoder",kinds[i].mime);
        }
    }
    OMX_ERRORTYPE makeComponentInstance(const char *name,const OMX_CALLBACKTYPE *callbacks,
        OMX_PTR appData,OMX_COMPONENTTYPE **component) override {
        for(unsigned i=0;i<kind_count;i++)if(available[i] && !strcmp(name,kinds[i].name)){
            auto *decoder=new FramePortVideo(kinds[i],callbacks,appData,component);
            decoder->incStrong(this);return decoder->initCheck();
        }
        return OMX_ErrorInvalidComponentName;
    }
    OMX_ERRORTYPE destroyComponentInstance(OMX_COMPONENTTYPE *component) override {
        auto *decoder=static_cast<SoftOMXComponent*>(component->pComponentPrivate);
        decoder->prepareForDestruction();decoder->decStrong(this);return OMX_ErrorNone;
    }
    OMX_ERRORTYPE enumerateComponents(OMX_STRING name,size_t size,OMX_U32 index) override {
        for(unsigned i=0;i<kind_count;i++)if(available[i]){
            if(index){--index;continue;}
            if(size<=strlen(kinds[i].name))return OMX_ErrorBadParameter;
            strcpy(name,kinds[i].name);return OMX_ErrorNone;
        }
        return OMX_ErrorNoMore;
    }
    OMX_ERRORTYPE getRolesOfComponent(const char *name,Vector<String8> *roles) override {
        for(unsigned i=0;i<kind_count;i++)if(available[i] && !strcmp(name,kinds[i].name)){
            roles->clear();roles->push(String8(kinds[i].role));return OMX_ErrorNone;
        }
        return OMX_ErrorInvalidComponentName;
    }
};
}
android::OMXPluginBase *createOMXPlugin(){return new android::FramePortOMXPlugin;}
// Android loaders support both historical factory ABI spellings.
extern "C" android::OMXPluginBase *createFramePortOMXPlugin() __asm__("createOMXPlugin");
extern "C" android::OMXPluginBase *createFramePortOMXPlugin(){return createOMXPlugin();}
extern "C" void destroyOMXPlugin(android::OMXPluginBase *plugin){delete plugin;}
