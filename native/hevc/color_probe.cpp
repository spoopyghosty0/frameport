// SPDX-License-Identifier: GPL-3.0-only
// Compare the actual platform NEON row functions with the full-range BT.709
// equations, including shadows/highlights and RGBA channel order in both paths.
#include "yuv_color.h"
#include <cassert>
#include <cstdio>
#include <cmath>
#include <cstring>
#include <initializer_list>
extern "C" {
void NV21ToARGBRow_Any_NEON(const uint8_t*,const uint8_t*,uint8_t*,const YuvConstants*,int);
void I422ToARGBRow_Any_NEON(const uint8_t*,const uint8_t*,const uint8_t*,uint8_t*,const YuvConstants*,int);
}
static int clipped(double v) {int n=(int)std::lround(v);return n<0?0:n>255?255:n;}
int main() {
    unsigned checked=0;
    for(unsigned width : {2u,8u,18u})
    for(int y : {0,8,16,64,128,235,247,255})
    for(int u : {16,64,128,192,240})
    for(int v : {16,64,128,192,240}) {
        uint8_t yy[32],uu[16],vv[16],uv[32],nv[128],planar[128];
        memset(yy,y,sizeof yy);memset(uu,u,sizeof uu);memset(vv,v,sizeof vv);
        for(unsigned i=0;i<width/2;i++){uv[2*i]=u;uv[2*i+1]=v;}
        NV21ToARGBRow_Any_NEON(yy,uv,nv,&frameport_full709_rgba,width);
        I422ToARGBRow_Any_NEON(yy,vv,uu,planar,&frameport_full709_rgba,width);
        int expected[]={clipped(y+1.5748*(v-128)),clipped(y-0.187324*(u-128)-0.468124*(v-128)),
                        clipped(y+1.8556*(u-128)),255};
        for(unsigned x=0;x<width;x++)for(unsigned c=0;c<4;c++) {
            assert(nv[4*x+c]==planar[4*x+c]);
            assert(std::abs((int)nv[4*x+c]-expected[c])<=2);
            if(u==128 && v==128 && c<3)assert(nv[4*x+c]==y);
        }
        checked++;
    }
    printf("PASS: %u full-range BT.709 colour/width combinations, planar and NV12 RGBA\n",checked);
}
