// Exercise frame pose selection independently of the XR runtime.
#include <assert.h>
#include <string.h>
#include <stdio.h>
#include "surface_views.h"
static int valid_transform=1,locate_calls;
static XrResult locate_space(XrSpace space,XrSpace base,XrTime time,XrSpaceLocation *out){
    assert(space==(XrSpace)(uintptr_t)2 && base==(XrSpace)(uintptr_t)1 && time==123);
    locate_calls++;
    out->locationFlags=valid_transform?XR_SPACE_LOCATION_ORIENTATION_VALID_BIT|XR_SPACE_LOCATION_POSITION_VALID_BIT:0;
    out->pose=(XrPosef){{0,0,0,1},{10,20,30}};
    return XR_SUCCESS;
}
int main(void){
    XrSession session=(XrSession)(uintptr_t)1;
    XrSpace space=(XrSpace)(uintptr_t)1;
    XrView original[2]={{XR_TYPE_VIEW},{XR_TYPE_VIEW}},out[2]={{XR_TYPE_VIEW},{XR_TYPE_VIEW}};
    for(int i=0;i<2;i++){
        original[i].pose=(XrPosef){{0,0,0,1},{i?0.03f:-0.03f,1,2}};
        original[i].fov=(XrFovf){-.5f-i*.1f,.7f,.6f,-.8f};
    }
    XrViewLocateInfo info={XR_TYPE_VIEW_LOCATE_INFO,NULL,XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO,123,space};
    XrViewState state={XR_TYPE_VIEW_STATE,NULL,XR_VIEW_STATE_ORIENTATION_VALID_BIT|XR_VIEW_STATE_POSITION_VALID_BIT};
    XrFrameEndInfo frame={XR_TYPE_FRAME_END_INFO,NULL,123,XR_ENVIRONMENT_BLEND_MODE_OPAQUE,0,NULL};
    assert(!surf_views_select(session,&frame,space,locate_space,out));
    surf_views_record(session,&info,&state,2,original);
    assert(surf_views_select(session,&frame,space,locate_space,out)==2);
    assert(!memcmp(&out[0].pose,&original[0].pose,sizeof(XrPosef)));
    assert(!memcmp(&out[1].fov,&original[1].fov,sizeof(XrFovf)));
    frame.displayTime++;
    assert(!surf_views_select(session,&frame,space,locate_space,out));
    frame.displayTime=123;
    assert(!surf_views_select((XrSession)(uintptr_t)2,&frame,space,locate_space,out));
    assert(!surf_views_select(session,&frame,(XrSpace)(uintptr_t)3,locate_space,out));
    state.viewStateFlags=0;info.displayTime=124;
    surf_views_record(session,&info,&state,2,original);
    frame.displayTime=124;assert(!surf_views_select(session,&frame,space,locate_space,out));
    frame.displayTime=123;
    XrCompositionLayerProjectionView eyes[2]={{XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW},{XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW}};
    for(int i=0;i<2;i++){eyes[i].pose=original[i].pose;eyes[i].pose.position.y=3;eyes[i].fov=original[i].fov;}
    XrCompositionLayerProjection projection={XR_TYPE_COMPOSITION_LAYER_PROJECTION,NULL,0,space,2,eyes};
    const XrCompositionLayerBaseHeader *layers[]={(const void*)&projection};frame.layerCount=1;frame.layers=layers;
    assert(surf_views_select(session,&frame,space,locate_space,out)==1);
    assert(out[0].pose.position.y==3 && locate_calls==0);
    assert(!memcmp(&out[1].fov,&eyes[1].fov,sizeof(XrFovf)));
    projection.space=(XrSpace)(uintptr_t)2;
    assert(surf_views_select(session,&frame,space,locate_space,out)==1);
    assert(fabsf(out[0].pose.position.x-9.97f)<.00001f && out[1].pose.position.y==23 && out[1].pose.position.z==32);
    valid_transform=0;
    assert(surf_views_select(session,&frame,space,locate_space,out)==2);
    frame.displayTime=125;frame.layerCount=0;
    assert(!surf_views_select(session,&frame,space,locate_space,out));
    puts("PASS: submitted-view priority, stereo poses/FOV, space transforms and exact session/time cache.");
}
