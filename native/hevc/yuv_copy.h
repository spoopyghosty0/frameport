// SPDX-License-Identifier: GPL-3.0-only
// Copy unchanged NV12 pixels into an Android YUV layout, in disjoint row bands.
#ifndef FRAMEPORT_YUV_COPY_H
#define FRAMEPORT_YUV_COPY_H
#include <stdint.h>
#include <string.h>
#include <arm_neon.h>
typedef struct {uint8_t *data;unsigned row_stride,pixel_stride;} FramePortYuvPlane;
static void frameport_yuv_copy_band(const uint8_t *y,const uint8_t *uv,
    unsigned width,unsigned height,unsigned y_stride,unsigned uv_stride,
    const FramePortYuvPlane planes[3],unsigned band,unsigned bands) {
    unsigned begin=(height/2)*band/bands,end=(height/2)*(band+1)/bands;
    for(unsigned row=2*begin;row<2*end;row++)
        memcpy(planes[0].data+row*planes[0].row_stride,y+row*y_stride,width);
    for(unsigned row=begin;row<end;row++){
        const uint8_t *src=uv+row*uv_stride;
        uint8_t *u=planes[1].data+row*planes[1].row_stride;
        uint8_t *v=planes[2].data+row*planes[2].row_stride;
        if(planes[1].pixel_stride==2 && planes[2].pixel_stride==2 &&
            planes[1].row_stride==planes[2].row_stride && v==u+1){
            memcpy(u,src,width);continue;
        }
        unsigned x=0;
        if(planes[1].pixel_stride==1 && planes[2].pixel_stride==1)
            for(;x+16<=width/2;x+=16){
                uint8x16x2_t pair=vld2q_u8(src+2*x);
                vst1q_u8(u+x,pair.val[0]);vst1q_u8(v+x,pair.val[1]);
            }
        for(;x<width/2;x++){
            u[x*planes[1].pixel_stride]=src[2*x];v[x*planes[2].pixel_stride]=src[2*x+1];
        }
    }
}
#endif
