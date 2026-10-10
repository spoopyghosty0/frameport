// SPDX-License-Identifier: GPL-3.0-only
// Immutable geometry/pose snapshot carried with a GPU-produced video view.
#ifndef FRAMEBRIDGE_SURFACE_VIDEO_H
#define FRAMEBRIDGE_SURFACE_VIDEO_H
#include <math.h>
#include <string.h>
#include "layer_math.h"
#define SURF_VIDEO_GUARD 1.12f
// MediaCodec's timed surface releases carry CLOCK_MONOTONIC presentation times.
// Untimed releases carry media PTS instead; those must not be treated as a clock.
static int64_t surf_video_target(int64_t now,XrTime display_time) {
    return display_time>now-100000000ll && display_time<now+100000000ll?display_time:now;
}
static int surf_video_future(int64_t timestamp,int64_t now,int64_t target) {
    return timestamp>now-60000000000ll && timestamp<now+60000000000ll && timestamp>target;
}
// A wider frustum gives runtime timewarp real pixels beyond the visible view.
// Keep the expanded FOV on the submitted image, not just on the shader rays.
static XrFovf surf_video_guard_fov(XrFovf fov) {
    return (XrFovf){atanf(tanf(fov.angleLeft)*SURF_VIDEO_GUARD),
        atanf(tanf(fov.angleRight)*SURF_VIDEO_GUARD),
        atanf(tanf(fov.angleUp)*SURF_VIDEO_GUARD),
        atanf(tanf(fov.angleDown)*SURF_VIDEO_GUARD)};
}
typedef struct {
    uint64_t seq;
    XrTime time;
    XrSpace space;
    XrCompositionLayerFlags flags;
    uint32_t size;
    XrView views[2];
    XrPosef panorama[2];
    float params[2][32];
} surf_video_job;
// Reorient a projected infinite panorama with its layer, retaining the actual
// camera rotation used to render it. Centre it on today's eye position because
// an infinite panorama has no translational parallax. This needs no pixel copy.
static XrPosef surf_video_pose(const surf_video_job *rendered,const surf_video_job *current,int eye) {
    XrPosef pose=rendered->views[eye].pose;
    if(rendered->space!=current->space || rendered->params[eye][7]!=0 || current->params[eye][7]!=0)return pose;
    if(memcmp(&rendered->panorama[eye].orientation,&current->panorama[eye].orientation,sizeof(XrQuaternionf))){
        XrQuaternionf delta=lm_qmul(current->panorama[eye].orientation,lm_qconj(rendered->panorama[eye].orientation));
        pose.orientation=lm_qmul(delta,pose.orientation);
    }
    pose.position=current->views[eye].pose.position;
    return pose;
}
// Reuse a held picture only while the next visible frustum fits inside it.
// The compositor still timewarps at headset cadence using the actual old pose.
// Changed source frames, layer geometry or colour bypass this reuse policy.
static int surf_video_frustum_covers(const surf_video_job *old,const surf_video_job *next,float margin) {
    if(!old->seq || old->size!=next->size || old->space!=next->space)return 0;
    for(int eye=0;eye<2;eye++){
        const float *a=old->params[eye],*b=next->params[eye];
        XrQuaternionf relative=lm_qmul(lm_qconj((XrQuaternionf){a[0],a[1],a[2],a[3]}),
            (XrQuaternionf){b[0],b[1],b[2],b[3]});
        for(int y=0;y<2;y++)for(int x=0;x<2;x++){
            XrVector3f ray=lm_rotate(relative,(XrVector3f){b[8+x]/SURF_VIDEO_GUARD,b[10+y]/SURF_VIDEO_GUARD,-1});
            if(ray.z>=-0.00001f)return 0;
            float u=ray.x/-ray.z,v=ray.y/-ray.z;
            if(u<a[8]*margin || u>a[9]*margin || v<a[10]*margin || v>a[11]*margin)return 0;
        }
    }
    return 1;
}
static int surf_video_covers_margin(const surf_video_job *old,const surf_video_job *next,float margin) {
    if(old->flags!=next->flags)return 0;
    for(int eye=0;eye<2;eye++){
        const float *a=old->params[eye],*b=next->params[eye];
        if(a[7]>0 && memcmp(&old->panorama[eye],&next->panorama[eye],sizeof(XrPosef)))return 0;
        if(a[7]!=b[7] || memcmp(a+12,b+12,20*sizeof(float)) ||
           (a[7]>0 && memcmp(a+4,b+4,3*sizeof(float))))return 0;
    }
    return surf_video_frustum_covers(old,next,margin);
}
static int surf_video_covers(const surf_video_job *old,const surf_video_job *next) {
    // Request a new sampling camera before the visible view reaches the outer
    // guard. The worker/copy handoff needs reserve while that update is in flight.
    return surf_video_covers_margin(old,next,.94f);
}
// Opaque decoded YUV can occlude earlier layers only when every pixel of the
// rendered, guarded stereo frustum lies in its 180-degree hemisphere. Keep
// ordinary composition during fades, outside coverage, and for other shapes.
static int surf_video_occludes(const surf_video_job *rendered,const surf_video_job *current) {
    // Occlusion uses actual image coverage, not the worker's earlier redraw
    // threshold. Otherwise each asynchronous camera update switches primary
    // layers briefly even though the completed image still covers both eyes.
    if(rendered->flags!=current->flags || !surf_video_frustum_covers(rendered,current,1.0f))return 0;
    for(int eye=0;eye<2;eye++){
        const float *p=rendered->params[eye];
        const float *q=current->params[eye];
        // RGB/source changes require fresh pixels, not a different composition
        // path. Require both the submitted picture and requested fade opaque.
        if(q[7]!=0 || !isfinite(q[27]+q[31]) || q[27]+q[31]<1.0f)return 0;
        if(p[7]!=0 || fabsf(p[16]-LM_PI)>.00001f ||
           fabsf(p[17]-LM_PI*.5f)>.00001f || fabsf(p[18]+LM_PI*.5f)>.00001f ||
           !isfinite(p[27]+p[31]) || p[27]+p[31]<1.0f ||
           p[12]<0 || p[13]<0 || p[14]<=0 || p[15]<=0 ||
           p[12]+p[14]>1.0f || p[13]+p[15]>1.0f)return 0;
        XrQuaternionf rotation={p[0],p[1],p[2],p[3]};
        for(int y=0;y<2;y++)for(int x=0;x<2;x++){
            XrVector3f ray=lm_rotate(rotation,(XrVector3f){p[8+x],p[10+y],-1});
            if(!isfinite(ray.z) || ray.z>=-.01f)return 0;
        }
    }
    return 1;
}
// A primary projection must describe one rigid stereo camera. Retarget the
// infinite panorama's centre to the current head position, but rotate the eye
// offsets with the actual camera orientation carried by its rendered pixels.
static XrPosef surf_video_primary_pose(const surf_video_job *rendered,const surf_video_job *current,int eye) {
    XrPosef pose=surf_video_pose(rendered,current,eye);
    XrVector3f centre={
        (current->views[0].pose.position.x+current->views[1].pose.position.x)*.5f,
        (current->views[0].pose.position.y+current->views[1].pose.position.y)*.5f,
        (current->views[0].pose.position.z+current->views[1].pose.position.z)*.5f};
    XrVector3f offset={current->views[eye].pose.position.x-centre.x,
        current->views[eye].pose.position.y-centre.y,current->views[eye].pose.position.z-centre.z};
    offset=lm_rotate(lm_qmul(pose.orientation,lm_qconj(current->views[eye].pose.orientation)),offset);
    pose.position=(XrVector3f){centre.x+offset.x,centre.y+offset.y,centre.z+offset.z};
    return pose;
}
// A fresh movie picture does not require a fresh camera direction. While the
// visible view fits, keep its panorama-local sampling grid stable across movie
// frames. This avoids encoding tracking motion into the video textures at the
// decoder's cadence. Tracking remains the compositor's responsibility. Carry
// the real anchored camera/FOV with the pixels; never relabel them as a new view.
// Re-anchor at the newest requested view when either eye leaves the guard band
// or the reference space changes. Source/colour updates keep this camera but
// are still rendered with the current mapping/colour. Finite spheres unchanged.
static int surf_video_anchor(const surf_video_job *old,const surf_video_job *current,surf_video_job *draw) {
    *draw=*current;
    if(old->params[0][7]!=0 || old->params[1][7]!=0 || current->params[0][7]!=0 || current->params[1][7]!=0 ||
        !surf_video_frustum_covers(old,current,.94f))return 0;
    for(int eye=0;eye<2;eye++){
        draw->views[eye]=old->views[eye];
        const float *p=old->params[eye];
        // Reconstruct from the fixed sampling quaternion, not a succession of
        // layer deltas, so long cutscenes cannot accumulate pose-rounding drift.
        draw->views[eye].pose.orientation=lm_qmul(current->panorama[eye].orientation,(XrQuaternionf){p[0],p[1],p[2],p[3]});
        draw->views[eye].pose.position=current->views[eye].pose.position;
        memcpy(draw->params[eye],old->params[eye],12*sizeof(float));
    }
    return 1;
}
// Same ray/sphere mapping as the Vulkan reference shader, sampled directly
// from the decoder's external YUV texture. No full-size RGB intermediate.
static const char *surf_video_fragment=
    "#version 300 es\n#extension GL_OES_EGL_image_external_essl3 : require\n"
    "precision highp float;uniform samplerExternalOES tex;in vec2 uv;out vec4 color;"
    "uniform vec4 rotation,originRadius,tans,rect,anglesEye,scaleBias,colorScale,colorBias;"
    "vec3 rotate(vec4 q,vec3 v){return v+2.0*cross(q.xyz,cross(q.xyz,v)+q.w*v);}"
    "void main(){vec3 d=normalize(rotate(rotation,vec3(mix(tans.x,tans.y,uv.x),mix(tans.w,tans.z,uv.y),-1.0)));"
    "if(originRadius.w>0.0){vec3 o=originRadius.xyz;float b=dot(o,d),disc=b*b+originRadius.w*originRadius.w-dot(o,o);"
    "if(disc<0.0){color=vec4(0);return;}float distance=-b+sqrt(disc);"
    "if(distance<=0.0){color=vec4(0);return;}d=normalize(o+distance*d);}"
    "float lon=atan(d.x,-d.z),lat=asin(clamp(d.y,-1.0,1.0));vec2 eq;"
    "if(anglesEye.x>0.0)eq=vec2(lon/anglesEye.x+0.5,(lat-anglesEye.z)/(anglesEye.y-anglesEye.z));"
    "else eq=vec2(lon/6.28318530718+0.5,lat/3.14159265359+0.5)*scaleBias.xy+scaleBias.zw;"
    "color=vec4(0);if(all(greaterThanEqual(eq,vec2(0)))&&all(lessThanEqual(eq,vec2(1))))"
    "color=clamp(texture(tex,rect.xy+vec2(eq.x,1.0-eq.y)*rect.zw)*colorScale+colorBias,0.0,1.0);}"
    ;
static const char *surf_video_uniforms[8]={"rotation","originRadius","tans","rect",
    "anglesEye","scaleBias","colorScale","colorBias"};
#endif
