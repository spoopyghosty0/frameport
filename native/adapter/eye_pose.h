// SPDX-License-Identifier: GPL-3.0-only
// eye_pose_fix math (eye_pose_fix.c): each eye's pose relative to the head, from the views xrLocateViews returned,
// and the rewrite of a projection layer whose two views carry one and the same (head) pose.
// Header-only, no Android dependencies: tests/test_framebridge_round2.py compiles it for the host.
#ifndef FRAMEBRIDGE_EYE_POSE_H
#define FRAMEBRIDGE_EYE_POSE_H
#include <math.h>
#include <openxr/openxr.h>
#include "layer_math.h"

// Same pose within 0.01 mm and ~0.2 degrees (an app that copies one pose into both views; real eyes are ~6 cm apart).
static inline int ep_same_pose(XrPosef a, XrPosef b) {
    float dp = fabsf(a.position.x - b.position.x) + fabsf(a.position.y - b.position.y) + fabsf(a.position.z - b.position.z);
    float dot = a.orientation.x * b.orientation.x + a.orientation.y * b.orientation.y + a.orientation.z * b.orientation.z +
                a.orientation.w * b.orientation.w;
    return dp < 1e-5f && fabsf(dot) > 0.999999f;
}

// The head between two eye poses: the midpoint, with the normalised mean of both orientations.
static inline XrPosef ep_head(XrPosef left, XrPosef right) {
    XrQuaternionf a = left.orientation, b = right.orientation;
    if (a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w < 0) b = (XrQuaternionf){-b.x, -b.y, -b.z, -b.w};
    XrQuaternionf q = {a.x + b.x, a.y + b.y, a.z + b.z, a.w + b.w};
    float len = sqrtf(q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w);
    if (len < 1e-6f) q = a; else q = (XrQuaternionf){q.x / len, q.y / len, q.z / len, q.w / len};
    return (XrPosef){q, {(left.position.x + right.position.x) / 2, (left.position.y + right.position.y) / 2,
                         (left.position.z + right.position.z) / 2}};
}

// offsets[i] = view i relative to the head between both views (works in any locate space, VIEW included).
// Returns 0 (offsets untouched) when the eyes are less than 1 mm or more than 20 cm apart.
static inline int ep_offsets(XrPosef left, XrPosef right, XrPosef offsets[2]) {
    XrVector3f d = {right.position.x - left.position.x, right.position.y - left.position.y,
                    right.position.z - left.position.z};
    float distance = lm_length(d);
    if (!(distance > 0.001f && distance < 0.2f)) return 0;
    XrPosef inv = lm_pose_inv(ep_head(left, right));
    offsets[0] = lm_pose_mul(inv, left);
    offsets[1] = lm_pose_mul(inv, right);
    return 1;
}

// A projection layer's two views both at `head`: each becomes head ∘ its eye's offset. Returns 1 when rewritten.
static inline int ep_rewrite(XrCompositionLayerProjectionView views[2], const XrPosef offsets[2]) {
    if (!ep_same_pose(views[0].pose, views[1].pose)) return 0;
    XrPosef head = views[0].pose;
    views[0].pose = lm_pose_mul(head, offsets[0]);
    views[1].pose = lm_pose_mul(head, offsets[1]);
    return 1;
}
#endif
