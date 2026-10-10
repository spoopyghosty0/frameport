// Exact native YUV copies: padded planar/interleaved layouts and uneven bands.
#include <assert.h>
#include <stdio.h>
#include "yuv_copy.h"
int main(void){
    enum {W=70,H=22,S=96};
    uint8_t y[S*H],uv[S*H/2],out[3][4*S*H];
    for(unsigned i=0;i<sizeof(y);i++)y[i]=(uint8_t)(i*17+5);
    for(unsigned i=0;i<sizeof(uv);i++)uv[i]=(uint8_t)(i*23+11);
    for(unsigned layout=0;layout<4;layout++)for(unsigned bands=1;bands<=4;bands++){
        memset(out,0xcd,sizeof(out));
        FramePortYuvPlane p[3]={{out[0],S,1},{out[1],2*S,layout==0?1:layout==3?3:2},{out[2],2*S,layout==0?1:layout==3?3:2}};
        if(layout==1)p[2].data=p[1].data+1;
        if(layout==2)p[1].data=p[2].data+1; // VU layout: U is not first.
        for(unsigned b=0;b<bands;b++)frameport_yuv_copy_band(y,uv,W,H,S,S,p,b,bands);
        for(unsigned row=0;row<H;row++)for(unsigned x=0;x<S;x++)
            assert(out[0][row*S+x]==(x<W?y[row*S+x]:0xcd));
        uint8_t expected[2][4*S*H];memset(expected,0xcd,sizeof(expected));
        for(unsigned row=0;row<H/2;row++)for(unsigned x=0;x<W/2;x++){
            unsigned ui=layout==2?1:0;
            unsigned vi=layout==1?1:0;
            expected[layout==2?1:0][ui+row*p[1].row_stride+x*p[1].pixel_stride]=uv[row*S+2*x];
            expected[layout==1?0:1][vi+row*p[2].row_stride+x*p[2].pixel_stride]=uv[row*S+2*x+1];
        }
        assert(!memcmp(out[1],expected[0],sizeof(out[1])));
        assert(!memcmp(out[2],expected[1],sizeof(out[2])));
    }
    puts("PASS: exact NV12 pixels, planar/UV/VU/sparse layouts, row padding and 1-4 uneven bands.");
}
