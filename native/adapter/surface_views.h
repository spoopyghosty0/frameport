// SPDX-License-Identifier: GPL-3.0-only
// Use the views belonging to the submitted frame before sampling tracking again.
// Cached application views are eligible only for the exact session/display time.
#ifndef FRAMEBRIDGE_SURFACE_VIEWS_H
#define FRAMEBRIDGE_SURFACE_VIEWS_H
#include <pthread.h>
#include "layer_math.h"
static pthread_mutex_t surf_views_lock=PTHREAD_MUTEX_INITIALIZER;
static struct {
    XrSession session;
    XrSpace space;
    XrTime time;
    XrView views[2];
} surf_view_cache[4];
static void surf_views_record(XrSession session,const XrViewLocateInfo *info,const XrViewState *state,
    uint32_t count,const XrView *views) {
    if(!info || !state || !views || count!=2 || !info->displayTime ||
        !(state->viewStateFlags&XR_VIEW_STATE_ORIENTATION_VALID_BIT))return;
    pthread_mutex_lock(&surf_views_lock);
    int slot=0;
    for(int i=0;i<4;i++){
        if(surf_view_cache[i].session==session && surf_view_cache[i].space==info->space){slot=i;break;}
        if(surf_view_cache[i].time<surf_view_cache[slot].time)slot=i;
    }
    surf_view_cache[slot].session=session;surf_view_cache[slot].space=info->space;
    surf_view_cache[slot].time=info->displayTime;
    surf_view_cache[slot].views[0]=views[0];surf_view_cache[slot].views[1]=views[1];
    pthread_mutex_unlock(&surf_views_lock);
}
// 1 = submitted projection views, 2 = exact application locate, 0 = not available.
static int surf_views_select(XrSession session,const XrFrameEndInfo *frame,XrSpace space,
    PFN_xrLocateSpace locate,XrView views[2]) {
    if(!frame)return 0;
    for(uint32_t i=0;i<frame->layerCount;i++){
        const XrCompositionLayerBaseHeader *layer=frame->layers[i];
        if(!layer || layer->type!=XR_TYPE_COMPOSITION_LAYER_PROJECTION)continue;
        const XrCompositionLayerProjection *projection=(const void*)layer;
        if(projection->viewCount!=2 || !projection->views)continue;
        XrPosef transform={{0,0,0,1},{0,0,0}};
        if(projection->space!=space){
            XrSpaceLocation location={XR_TYPE_SPACE_LOCATION,NULL,0,{{0,0,0,1},{0,0,0}}};
            XrSpaceLocationFlags valid=XR_SPACE_LOCATION_ORIENTATION_VALID_BIT|XR_SPACE_LOCATION_POSITION_VALID_BIT;
            if(!locate || XR_FAILED(locate(projection->space,space,frame->displayTime,&location)) ||
                (location.locationFlags&valid)!=valid)continue;
            transform=location.pose;
        }
        for(int eye=0;eye<2;eye++){
            views[eye].pose=lm_pose_mul(transform,projection->views[eye].pose);
            views[eye].fov=projection->views[eye].fov;
        }
        return 1;
    }
    int found=0;
    pthread_mutex_lock(&surf_views_lock);
    for(int i=0;i<4;i++)if(surf_view_cache[i].session==session && surf_view_cache[i].space==space &&
        surf_view_cache[i].time==frame->displayTime){
        views[0]=surf_view_cache[i].views[0];views[1]=surf_view_cache[i].views[1];found=2;break;
    }
    pthread_mutex_unlock(&surf_views_lock);return found;
}
#endif
