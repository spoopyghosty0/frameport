// SPDX-License-Identifier: GPL-3.0-only
// Parameters for composing a cached movie view into the game's scene camera.
#ifndef FRAMEBRIDGE_SURFACE_COMPOSITE_H
#define FRAMEBRIDGE_SURFACE_COMPOSITE_H
#include "surface_video.h"
typedef struct {
    float rotation[4], output_tans[4], video_tans[4];
    float scene_rect[4], options[4], scene_options[4], scene_scale[4], scene_bias[4];
} surf_composite_params;
static surf_composite_params surf_composite_parameters(const surf_video_job *rendered,
    const surf_video_job *current,int eye,XrRect2Di rect,uint32_t width,uint32_t height,uint32_t array_index) {
    surf_composite_params p={0};
    // The current view must already be transformed into the panorama space.
    XrPosef source=surf_video_pose(rendered,current,eye);
    XrQuaternionf relative=lm_qmul(lm_qconj(source.orientation),current->views[eye].pose.orientation);
    memcpy(p.rotation,&relative,sizeof(relative));
    for(int i=0;i<4;i++) {
        p.output_tans[i]=current->params[eye][8+i]/SURF_VIDEO_GUARD;
        p.video_tans[i]=rendered->params[eye][8+i];
    }
    p.scene_rect[0]=rect.offset.x/(float)width;p.scene_rect[1]=rect.offset.y/(float)height;
    p.scene_rect[2]=rect.extent.width/(float)width;p.scene_rect[3]=rect.extent.height/(float)height;
    for(int i=0;i<4;i++)p.scene_scale[i]=1;
    p.options[0]=(float)eye;p.options[1]=(float)array_index;
    p.options[2]=(rendered->flags&XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT)!=0;
    p.options[3]=(rendered->flags&XR_COMPOSITION_LAYER_UNPREMULTIPLIED_ALPHA_BIT)!=0;
    return p;
}
#endif
