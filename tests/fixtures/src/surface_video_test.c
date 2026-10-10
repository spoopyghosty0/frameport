// Reprojection coverage: reuse static pictures without hiding content changes.
#include <assert.h>
#include <stdio.h>
#include "surface_video.h"
int main(void){
    int64_t clock=90000000000000ll;
    assert(surf_video_target(clock,clock+10000000ll)==clock+10000000ll);
    assert(surf_video_target(clock,1)==clock);
    assert(surf_video_future(clock+30000000ll,clock,clock+10000000ll));
    assert(!surf_video_future(clock-10000000ll,clock,clock+10000000ll));
    assert(!surf_video_future(1000000000ll,clock,clock)); // untimed media PTS
    surf_video_job old={.seq=1,.size=2560,.space=(XrSpace)(uintptr_t)1},next;
    XrFovf input={-.6f,.65f,.7f,-.55f},wide=surf_video_guard_fov(input);
    assert(wide.angleLeft<input.angleLeft && wide.angleRight>input.angleRight);
    assert(wide.angleDown<input.angleDown && wide.angleUp>input.angleUp);
    for(int eye=0;eye<2;eye++){
        float *p=old.params[eye];p[3]=1;
        old.views[eye].pose.orientation.w=old.panorama[eye].orientation.w=1;
        p[8]=tanf(wide.angleLeft);p[9]=tanf(wide.angleRight);
        p[10]=tanf(wide.angleDown);p[11]=tanf(wide.angleUp);
    }
    next=old;next.seq++;next.time++;
    assert(surf_video_covers(&old,&next));
    // Small tracking changes remain covered, in yaw, pitch, roll and both signs.
    for(int axis=0;axis<3;axis++)for(int sign=-1;sign<=1;sign+=2){
        next=old;for(int eye=0;eye<2;eye++){
            next.params[eye][axis]=sinf(sign*.005f);next.params[eye][3]=cosf(.005f);
        }
        assert(surf_video_covers(&old,&next));
    }
    next=old;for(int eye=0;eye<2;eye++){
        next.params[eye][1]=sinf(.15f);next.params[eye][3]=cosf(.15f);
    }
    assert(!surf_video_covers(&old,&next));
    next=old;next.params[1][25]=.5f;assert(!surf_video_covers(&old,&next));
    next=old;next.params[0][12]=.25f;assert(!surf_video_covers(&old,&next));
    next=old;next.panorama[0].orientation=lm_axis_angle(0,1,0,.2f);
    next.params[0][1]=-sinf(.1f);next.params[0][3]=cosf(.1f);
    assert(!surf_video_covers(&old,&next));
    next=old;next.space=(XrSpace)(uintptr_t)2;assert(!surf_video_covers(&old,&next));
    next=old;next.size=2048;assert(!surf_video_covers(&old,&next));
    next=old;next.params[0][4]=.001f;assert(surf_video_covers(&old,&next));
    next.panorama[0].position.x=.01f;assert(surf_video_covers(&old,&next));
    next.panorama[1].position.y=-.02f;assert(surf_video_covers(&old,&next));
    old.params[0][7]=next.params[0][7]=1;assert(!surf_video_covers(&old,&next));
    old.params[0][7]=0;
    for(int eye=0;eye<2;eye++){
        old.panorama[eye].orientation=lm_axis_angle(0,1,0,.35f);
        old.views[eye].pose.orientation=lm_qmul(lm_axis_angle(1,0,0,.17f),lm_axis_angle(0,0,1,-.23f));
        XrQuaternionf local=lm_qmul(lm_qconj(old.panorama[eye].orientation),old.views[eye].pose.orientation);
        memcpy(old.params[eye],&local,sizeof(local));
    }
    next=old;
    XrQuaternionf delta=lm_axis_angle(1,0,0,.4f);
    for(int eye=0;eye<2;eye++){
        next.panorama[eye].orientation=lm_qmul(delta,old.panorama[eye].orientation);
        next.views[eye].pose.orientation=lm_qmul(delta,old.views[eye].pose.orientation);
        next.views[eye].pose.position=(XrVector3f){.2f,.03f,-.1f};
        // Head and layer moved together: local mapping stays identical.
    }
    assert(surf_video_covers(&old,&next));
    for(int eye=0;eye<2;eye++){
        XrPosef pose=surf_video_pose(&old,&next,eye);
        assert(!memcmp(&pose.position,&next.views[eye].pose.position,sizeof(XrVector3f)));
        XrQuaternionf local=lm_qmul(lm_qconj(next.panorama[eye].orientation),pose.orientation);
        for(int y=-1;y<=1;y++)for(int x=-1;x<=1;x++){
            XrVector3f ray={(float)x,(float)y,-1};
            XrVector3f a=lm_rotate((XrQuaternionf){old.params[eye][0],old.params[eye][1],old.params[eye][2],old.params[eye][3]},ray);
            XrVector3f b=lm_rotate(local,ray);
            assert(fabsf(a.x-b.x)<.000001f && fabsf(a.y-b.y)<.000001f && fabsf(a.z-b.z)<.000001f);
        }
        old.params[eye][7]=1;
        pose=surf_video_pose(&old,&next,eye);
        assert(!memcmp(&pose,&old.views[eye].pose,sizeof(pose)));
        old.params[eye][7]=0;
    }
    next.space=(XrSpace)(uintptr_t)2;
    XrPosef pose=surf_video_pose(&old,&next,0);
    assert(!memcmp(&pose,&old.views[0].pose,sizeof(pose)));
    // Keep fresh movie pictures on a steady sampling grid while tracking
    // moves within coverage. Every frame must carry that actual grid/pose.
    surf_video_job baseline=old,previous=old,draw;
    for(int eye=0;eye<2;eye++)baseline.views[eye].fov=previous.views[eye].fov=wide;
    int stable=0;
    for(int frame=0;frame<1000;frame++){
        next=baseline;next.seq=frame+2;next.time=frame+100;
        XrQuaternionf layer_delta=lm_axis_angle(0,1,0,.05f*sinf(frame*.017f));
        XrQuaternionf head_delta=lm_axis_angle(1,0,0,.008f*sinf(frame*.11f));
        for(int eye=0;eye<2;eye++){
            next.panorama[eye].orientation=lm_qmul(layer_delta,baseline.panorama[eye].orientation);
            next.views[eye].pose.orientation=lm_qmul(layer_delta,lm_qmul(baseline.views[eye].pose.orientation,head_delta));
            next.views[eye].pose.position=(XrVector3f){.03f*sinf(frame*.1f),.01f,-.04f};
            XrQuaternionf local=lm_qmul(lm_qconj(next.panorama[eye].orientation),next.views[eye].pose.orientation);
            memcpy(next.params[eye],&local,sizeof(local));
        }
        assert(surf_video_anchor(&previous,&next,&draw));stable++;
        assert(draw.seq==next.seq && draw.time==next.time && draw.space==next.space);
        for(int eye=0;eye<2;eye++){
            assert(!memcmp(draw.params[eye],baseline.params[eye],12*sizeof(float)));
            assert(!memcmp(&draw.views[eye].fov,&wide,sizeof(wide)));
            assert(!memcmp(&draw.views[eye].pose.position,&next.views[eye].pose.position,sizeof(XrVector3f)));
            XrQuaternionf local=lm_qmul(lm_qconj(draw.panorama[eye].orientation),draw.views[eye].pose.orientation);
            XrVector3f actual=lm_rotate(local,(XrVector3f){.3f,.2f,-1});
            const float *p=draw.params[eye];
            XrVector3f expected=lm_rotate((XrQuaternionf){p[0],p[1],p[2],p[3]},(XrVector3f){.3f,.2f,-1});
            assert(fabsf(actual.x-expected.x)<.000001f && fabsf(actual.y-expected.y)<.000001f && fabsf(actual.z-expected.z)<.000001f);
        }
        previous=draw;
    }
    next=baseline;next.params[0][7]=1;
    assert(!surf_video_anchor(&previous,&next,&draw) && !memcmp(&draw,&next,sizeof(draw)));
    // Occlusion is exact for an opaque decoded hemisphere, including its
    // overscan. Never hide underlying layers during a fade or outside it.
    surf_video_job opaque={.seq=1,.size=2560,.space=(XrSpace)(uintptr_t)1};
    for(int eye=0;eye<2;eye++){
        float *p=opaque.params[eye];p[3]=1;p[8]=-1;p[9]=1;p[10]=-1;p[11]=1;
        p[12]=eye*.5f;p[14]=.5f;p[15]=1;p[16]=LM_PI;p[17]=LM_PI*.5f;p[18]=-LM_PI*.5f;p[27]=1;
        opaque.panorama[eye].orientation.w=opaque.views[eye].pose.orientation.w=1;
        opaque.views[eye].pose.position.x=eye?.032f:-.032f;
    }
    next=opaque;next.seq++;
    assert(surf_video_occludes(&opaque,&next));
    for(int eye=0;eye<2;eye++){
        surf_video_job transparent=opaque;transparent.params[eye][27]=.99f;
        assert(!surf_video_occludes(&transparent,&transparent));
        transparent=opaque;transparent.params[eye][31]=-.01f;
        assert(!surf_video_occludes(&transparent,&transparent));
        transparent=opaque;transparent.params[eye][7]=1;
        assert(!surf_video_occludes(&transparent,&transparent));
        transparent=opaque;transparent.params[eye][17]=.5f;
        assert(!surf_video_occludes(&transparent,&transparent));
        transparent=opaque;transparent.params[eye][12]=1;
        assert(!surf_video_occludes(&transparent,&transparent));
        transparent=opaque;
        XrQuaternionf edge=lm_axis_angle(0,1,0,.8f);memcpy(transparent.params[eye],&edge,sizeof(edge));
        assert(!surf_video_occludes(&transparent,&transparent));
    }
    next=opaque;next.flags=1;assert(!surf_video_occludes(&opaque,&next));
    next=opaque;next.params[0][27]=.5f;assert(!surf_video_occludes(&opaque,&next));
    next=opaque;next.params[0][24]=.5f;next.params[1][29]=.01f;
    assert(!surf_video_covers(&opaque,&next)); // pixels must update
    assert(surf_video_occludes(&opaque,&next)); // opaque RGB animation never changes the primary path
    next=opaque;next.space=(XrSpace)(uintptr_t)2;assert(!surf_video_occludes(&opaque,&next));
    next=opaque;
    for(int eye=0;eye<2;eye++){
        XrQuaternionf pending=lm_axis_angle(0,1,0,.045f);memcpy(next.params[eye],&pending,sizeof(pending));
    }
    assert(!surf_video_covers(&opaque,&next)); // worker must already request a new camera
    assert(surf_video_occludes(&opaque,&next)); // old image still covers while it finishes
    // The primary path's two old camera orientations must reconstruct a
    // single head centre even when the tracked head moved/turned meanwhile.
    for(int frame=0;frame<1000;frame++){
        next=opaque;XrQuaternionf head=lm_axis_angle(0,1,0,.035f*sinf(frame*.01f));
        for(int eye=0;eye<2;eye++){
            next.views[eye].pose.orientation=head;
            XrVector3f offset=lm_rotate(head,(XrVector3f){eye?.032f:-.032f,0,0});
            next.views[eye].pose.position=(XrVector3f){.1f+offset.x,.2f+offset.y,.3f+offset.z};
            memcpy(next.params[eye],&head,sizeof(head));
        }
        for(int eye=0;eye<2;eye++){
            XrPosef primary=surf_video_primary_pose(&opaque,&next,eye);
            assert(fabsf(primary.position.x-(.1f+(eye?.032f:-.032f)))<.000001f);
            assert(fabsf(primary.position.y-.2f)<.000001f && fabsf(primary.position.z-.3f)<.000001f);
            assert(!memcmp(&primary.orientation,&opaque.views[eye].pose.orientation,sizeof(primary.orientation)));
        }
        assert(surf_video_occludes(&opaque,&next));
    }
    puts("PASS: guarded opaque stereo occlusion, fade/source/edge/space exclusions, rigid primary stereo poses.");
    next=baseline;next.params[1][24]=.5f;
    assert(surf_video_anchor(&previous,&next,&draw));
    assert(!memcmp(draw.params[1],baseline.params[1],12*sizeof(float)) && draw.params[1][24]==.5f);
    next=baseline;next.params[0][12]=.25f;
    assert(surf_video_anchor(&previous,&next,&draw) && draw.params[0][12]==.25f);
    // Continuous colour and fade animation, with held or fresh movie pictures,
    // updates current pixels while retaining the sampling camera in both eyes.
    previous=opaque;
    for(int frame=0;frame<1000;frame++){
        next=opaque;next.seq=frame+2;next.time=frame+100;
        for(int eye=0;eye<2;eye++){
            XrQuaternionf head=lm_axis_angle(0,1,0,.02f*sinf(frame*.17f));
            memcpy(next.params[eye],&head,sizeof(head));next.views[eye].pose.orientation=head;
            next.params[eye][24]=.4f+.3f*sinf(frame*.02f);
            next.params[eye][28]=.02f*cosf(frame*.03f);
            next.params[eye][27]=frame<500?1.0f:.5f+.25f*sinf(frame*.02f);
        }
        assert(!surf_video_covers(&previous,&next));
        assert(surf_video_anchor(&previous,&next,&draw));
        for(int eye=0;eye<2;eye++){
            assert(!memcmp(draw.params[eye],opaque.params[eye],12*sizeof(float)));
            assert(!memcmp(draw.params[eye]+12,next.params[eye]+12,20*sizeof(float)));
        }
        assert(surf_video_occludes(&draw,&next)==(frame<500));
        previous=draw;
    }
    puts("PASS: 1000 animated colour/fade updates retain sampling cameras, refresh pixels, and preserve opaque/translucent composition.");
    next=baseline;next.space=(XrSpace)(uintptr_t)3;
    assert(!surf_video_anchor(&previous,&next,&draw));
    next=baseline;next.views[0].pose.orientation=lm_qmul(baseline.views[0].pose.orientation,lm_axis_angle(0,1,0,.4f));
    XrQuaternionf local=lm_qmul(lm_qconj(next.panorama[0].orientation),next.views[0].pose.orientation);
    memcpy(next.params[0],&local,sizeof(local));
    assert(!surf_video_anchor(&previous,&next,&draw) && !memcmp(&draw,&next,sizeof(draw)));
    printf("PASS: %d new-picture anchored views, moving head/layer/eyes, unchanged pixels/FOV and truthful poses; fast turns re-anchor.\n",stable);
    puts("PASS: infinite panorama retargeting, unchanged local sample rays and eye positions; finite/space exclusions.");
    puts("PASS: expanded FOV, small rotations, fast recentering, stereo geometry, colour and finite-sphere changes.");
}
