// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c. eye_pose_fix (per game, off by default; GitHub #527).
//
// Lambda1VR (TBXR) renders each eye with its own offset but submits the head pose for both projection views (log:
// "projection view 0: ... pos=-0.390,1.091,-1.570" = view 1). A compositor reprojects each eye's image from the pose
// it was submitted with to where that eye really is, so both pictures were shifted inward by half the eye distance:
// wrong depth, eye strain. With the setting, a projection layer whose two views carry the identical pose gets, per
// view, that pose ∘ the eye's offset from the head, as the runtime's latest xrLocateViews answer has it (in whatever
// space the app located them; the head is the midpoint of the two views). The field of view stays as submitted.
// Uses eye_pose_fix, LOG; called from xrLocateViews and xrEndFrame.
#include "eye_pose.h"

static pthread_mutex_t ep_lock = PTHREAD_MUTEX_INITIALIZER;
static XrPosef ep_offset[2];
static int ep_have_offsets;

static void eye_pose_note_views(const XrViewState *state, uint32_t count, const XrView *views) {
    const XrViewStateFlags valid = XR_VIEW_STATE_POSITION_VALID_BIT | XR_VIEW_STATE_ORIENTATION_VALID_BIT;
    if (!eye_pose_fix || !state || !views || count != 2 || (state->viewStateFlags & valid) != valid) return;
    XrPosef offsets[2];
    if (!ep_offsets(views[0].pose, views[1].pose, offsets)) return;
    pthread_mutex_lock(&ep_lock);
    ep_offset[0] = offsets[0];
    ep_offset[1] = offsets[1];
    ep_have_offsets = 1;
    pthread_mutex_unlock(&ep_lock);
}

// `info` itself, or a copy (thread-local storage, valid until this thread's next frame) whose stereo projection layers
// with one pose for both views have per-eye poses.
static const XrFrameEndInfo *eye_pose_frame(const XrFrameEndInfo *info) {
    if (!eye_pose_fix || !info || !info->layers || !info->layerCount || info->layerCount > 64) return info;
    XrPosef offsets[2];
    pthread_mutex_lock(&ep_lock);
    int have = ep_have_offsets;
    offsets[0] = ep_offset[0];
    offsets[1] = ep_offset[1];
    pthread_mutex_unlock(&ep_lock);
    if (!have) return info;
    static __thread XrFrameEndInfo fixed;
    static __thread const XrCompositionLayerBaseHeader *layers[64];
    static __thread XrCompositionLayerProjection projections[64];
    static __thread XrCompositionLayerProjectionView views[64][2];
    int rewritten = 0;
    for (uint32_t i = 0; i < info->layerCount; ++i) {
        const XrCompositionLayerBaseHeader *l = info->layers[i];
        layers[i] = l;
        if (!l || l->type != XR_TYPE_COMPOSITION_LAYER_PROJECTION) continue;
        const XrCompositionLayerProjection *p = (const XrCompositionLayerProjection *)l;
        if (p->viewCount != 2 || !p->views) continue;
        views[i][0] = p->views[0];
        views[i][1] = p->views[1];
        if (!ep_rewrite(views[i], offsets)) continue;
        projections[i] = *p;
        projections[i].views = views[i];
        layers[i] = (const XrCompositionLayerBaseHeader *)&projections[i];
        ++rewritten;
    }
    if (!rewritten) return info;
    static int logged;
    if (!logged) {
        logged = 1;
        XrVector3f d = {offsets[1].position.x - offsets[0].position.x, offsets[1].position.y - offsets[0].position.y,
                        offsets[1].position.z - offsets[0].position.z};
        LOG("eye_pose_fix: both projection views had the same pose; each now gets its eye's offset from it "
            "(eyes %.1f mm apart, field of view as submitted)", lm_length(d) * 1000.0f);
    }
    fixed = *info;
    fixed.layers = layers;
    return &fixed;
}
