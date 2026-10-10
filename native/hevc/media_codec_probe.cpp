#include <media/NdkMediaCodec.h>
#include <media/NdkMediaExtractor.h>
#include <media/NdkImageReader.h>
#include <android/native_window.h>
#include <android/hardware_buffer.h>
#include <cstdio>
#include <cassert>
#include <cstring>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include <time.h>
static double now(){timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec*1e-9;}
int main(int argc,char **argv){
 bool fallback=argc>2 && !strcmp(argv[2],"fallback");
 assert(argc>=2);int fd=open(argv[1],O_RDONLY);assert(fd>=0);struct stat st;assert(!fstat(fd,&st));
 auto *extractor=AMediaExtractor_new();assert(AMediaExtractor_setDataSourceFd(extractor,fd,0,st.st_size)==AMEDIA_OK);
 AMediaFormat *format=nullptr;int width=0,height=0;const char *video_mime=nullptr;
 for(size_t i=0;i<AMediaExtractor_getTrackCount(extractor);i++){
  auto *candidate=AMediaExtractor_getTrackFormat(extractor,i);const char *mime=nullptr;
  if(AMediaFormat_getString(candidate,AMEDIAFORMAT_KEY_MIME,&mime)&&
     (!strcmp(mime,"video/hevc") || !strcmp(mime,"video/avc") || !strcmp(mime,"video/x-vnd.on2.vp9"))){
   format=candidate;video_mime=mime;assert(AMediaExtractor_selectTrack(extractor,i)==AMEDIA_OK);break;
  }AMediaFormat_delete(candidate);
 }assert(format);
 assert(AMediaFormat_getInt32(format,AMEDIAFORMAT_KEY_WIDTH,&width));assert(AMediaFormat_getInt32(format,AMEDIAFORMAT_KEY_HEIGHT,&height));
 const char *expected=!strcmp(video_mime,"video/hevc")?"OMX.frameport.hevc.decoder":
   !strcmp(video_mime,"video/avc")?"OMX.frameport.avc.decoder":"OMX.frameport.vp9.decoder";
 const char *stock=!strcmp(video_mime,"video/hevc")?"OMX.google.hevc.decoder":
   !strcmp(video_mime,"video/avc")?"OMX.google.h264.decoder":"OMX.google.vp9.decoder";
 auto *codec=AMediaCodec_createDecoderByType(video_mime);assert(codec);char *name=nullptr;
 assert(AMediaCodec_getName(codec,&name)==AMEDIA_OK);printf("Selected codec: %s\n",name);fflush(stdout);
 if(argc>2 && !strcmp(argv[2],"query")){
  assert(!strcmp(name,expected) || !strcmp(name,stock));
  AMediaCodec_releaseName(codec,name);AMediaCodec_delete(codec);
  AMediaFormat_delete(format);AMediaExtractor_delete(extractor);close(fd);
  puts("PASS default HEVC selection");return 0;
 }
 assert(!strcmp(name,fallback?stock:expected));AMediaCodec_releaseName(codec,name);
 AImageReader *reader=nullptr;ANativeWindow *window=nullptr;
 if(argc>2){assert(AImageReader_newWithUsage(width,height,AIMAGE_FORMAT_PRIVATE,AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE,3,&reader)==AMEDIA_OK);assert(AImageReader_getWindow(reader,&window)==AMEDIA_OK);AMediaFormat_setInt32(format,"allow-frame-drop",0);}
 printf("Input format: %s\n",AMediaFormat_toString(format));
 const char *keys[]={"csd-0","csd-1"};for(const char *key:keys){void *p=nullptr;size_t n=0;if(AMediaFormat_getBuffer(format,key,&p,&n)){auto *b=(unsigned char*)p;printf("%s: %zu bytes %02x %02x %02x %02x\n",key,n,b[0],b[1],b[2],b[3]);}}
 assert(AMediaCodec_configure(codec,format,window,nullptr,0)==AMEDIA_OK);assert(AMediaCodec_start(codec)==AMEDIA_OK);
 for(unsigned phase=0;phase<2;phase++){
 unsigned goal=fallback?(phase?2:6):(phase?120:600),sent=0,received=0,images=0;bool input_eos=false,output_eos=false;double start=now();
 while(!output_eos && now()-start<45){
  if(!input_eos){ssize_t id=AMediaCodec_dequeueInputBuffer(codec,1000);if(id>=0){
   size_t capacity=0;auto *data=AMediaCodec_getInputBuffer(codec,id,&capacity);assert(data);
   if(sent==goal){assert(AMediaCodec_queueInputBuffer(codec,id,0,0,sent*1000000/60,AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM)==AMEDIA_OK);input_eos=true;}
   else {ssize_t size=AMediaExtractor_readSampleData(extractor,data,capacity);assert(size>0);int64_t pts=AMediaExtractor_getSampleTime(extractor);
    if(sent<2)printf("Input packet: %zd bytes %02x %02x %02x %02x\n",size,data[0],data[1],data[2],data[3]);
    assert(AMediaCodec_queueInputBuffer(codec,id,0,size,pts,AMediaExtractor_getSampleFlags(extractor)&1?AMEDIACODEC_BUFFER_FLAG_KEY_FRAME:0)==AMEDIA_OK);
    assert(AMediaExtractor_advance(extractor));sent++;
   }
  }}
  AMediaCodecBufferInfo info={};ssize_t id=AMediaCodec_dequeueOutputBuffer(codec,&info,1000);
  if(id>=0){if(received<2 || (info.flags&AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM))printf("Buffer id=%zd size=%d flags=%u pts=%lld\n",id,info.size,info.flags,(long long)info.presentationTimeUs);if(info.size)received++;output_eos=info.flags&AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM;
   assert(AMediaCodec_releaseOutputBuffer(codec,id,window!=nullptr)==AMEDIA_OK);
   // Surface delivery is asynchronous. Drain each rendered picture before
   // submitting another, so this unpaced fixture does not test frame dropping.
   if(reader && info.size){double deadline=now()+1;unsigned before=images;
    do{AImage *image=nullptr;if(AImageReader_acquireNextImage(reader,&image)==AMEDIA_OK){
     int32_t w=0,h=0;assert(AImage_getWidth(image,&w)==AMEDIA_OK && AImage_getHeight(image,&h)==AMEDIA_OK);
     assert(w==width && h==height);images++;AImage_delete(image);
    }else usleep(100);}while(images==before && now()<deadline);
    assert(images>before);
   }
  }else if(id==AMEDIACODEC_INFO_OUTPUT_FORMAT_CHANGED){auto *out=AMediaCodec_getOutputFormat(codec);printf("Output: %s\n",AMediaFormat_toString(out));AMediaFormat_delete(out);fflush(stdout);}
  if(reader){AImage *image=nullptr;while(AImageReader_acquireNextImage(reader,&image)==AMEDIA_OK){images++;AImage_delete(image);}}
 }
 printf("Phase %u: %u decoded frames, %u surface images, %.3f seconds\n",phase,received,images,now()-start);fflush(stdout);
 assert(output_eos && received==goal);if(reader)assert(images>=goal-2);
 if(!phase){assert(AMediaCodec_flush(codec)==AMEDIA_OK);assert(AMediaExtractor_seekTo(extractor,0,AMEDIAEXTRACTOR_SEEK_PREVIOUS_SYNC)==AMEDIA_OK);}
 }
 assert(AMediaCodec_stop(codec)==AMEDIA_OK);AMediaCodec_delete(codec);if(reader)AImageReader_delete(reader);
 AMediaFormat_delete(format);AMediaExtractor_delete(extractor);close(fd);puts(fallback?"PASS stock fallback playback and seeking":"PASS MediaCodec default selection, full-resolution playback and seeking");
}
