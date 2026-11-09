// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c: wait_balance (default on; GitHub #135, Five Nights at Freddy's: Help Wanted).
//
// OpenXR lets xrWaitFrame block until the previously waited frame has begun. Meta's runtime never held a game up
// there; the Frame's (SteamVR) does, forever. Unreal's Oculus splash screen (OculusHMD::FSplash::RenderFrame_RenderThread
// -> ovrp_WaitToBeginFrame) waits for a new frame on the render thread while the frame waited before it was never
// begun ("OVRPlugin: CompositorOpenXR::Update called for frame 7 outside of frame bounds"): the render thread, the only
// one that would begin it, then sits in vrclient's CSxrSession::WaitFrame and the game hangs on its logo.
//
// When xrWaitFrame is called while a waited frame of the same session is still not begun, FrameBridge gives another
// thread WB_GRACE_NS to begin it (engines that wait for the next frame on one thread and begin on another do that
// within a frame). If none does, it closes the stale frame itself: xrBeginFrame + an xrEndFrame without layers at the
// stale wait's predicted display time, straight at the runtime, and the wait goes on. FrameBridge then owes the game
// one frame: a later xrBeginFrame that finds no waited frame (the late begin of the frame it closed) is answered
// XR_SUCCESS without the runtime, and so is its xrEndFrame, so waits and begins stay paired at the runtime.
// Uses wait_balance, frame_open, active_instance, lookup and LOG.

#ifndef WB_GRACE_NS
#define WB_GRACE_NS 100000000ll  // 100 ms
#endif

static pthread_mutex_t wb_lock = PTHREAD_MUTEX_INITIALIZER;
static XrSession wb_session;
static int wb_pending;         // waited frames of wb_session not begun yet
static int wb_debt;            // frames FrameBridge closed for the game: its late xrBeginFrame is answered here
static int wb_swallowed_open;  // such a begin was answered: its xrEndFrame is too
static XrTime wb_stale_time;   // the waited frame's predicted display time
static int wb_closed;          // frames closed so far (log)

static int64_t wb_now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000000000ll + t.tv_nsec;
}

static void wb_before_wait(XrSession session) {
    if (!wait_balance) return;
    pthread_mutex_lock(&wb_lock);
    int stale = session == wb_session && wb_pending > 0 && !frame_open;
    pthread_mutex_unlock(&wb_lock);
    if (!stale) return;
    int64_t deadline = wb_now_ns() + WB_GRACE_NS;
    struct timespec step = {0, 1000000};
    for (;;) {  // another thread may still begin it
        pthread_mutex_lock(&wb_lock);
        stale = session == wb_session && wb_pending > 0 && !frame_open;
        pthread_mutex_unlock(&wb_lock);
        if (!stale) return;
        if (wb_now_ns() >= deadline) break;
        nanosleep(&step, NULL);
    }
    PFN_xrBeginFrame begin = (PFN_xrBeginFrame)lookup(active_instance, "xrBeginFrame");
    PFN_xrEndFrame end = (PFN_xrEndFrame)lookup(active_instance, "xrEndFrame");
    if (!begin || !end) return;
    pthread_mutex_lock(&wb_lock);
    XrFrameBeginInfo begin_info = {XR_TYPE_FRAME_BEGIN_INFO, NULL};
    XrResult rb = begin(session, &begin_info), re = XR_ERROR_CALL_ORDER_INVALID;
    if (XR_SUCCEEDED(rb)) {
        XrFrameEndInfo empty = {XR_TYPE_FRAME_END_INFO, NULL, wb_stale_time, XR_ENVIRONMENT_BLEND_MODE_OPAQUE, 0, NULL};
        re = end(session, &empty);
        --wb_pending;
        ++wb_debt;
    }
    int n = ++wb_closed;
    pthread_mutex_unlock(&wb_lock);
    if (n <= 5 || n % 100 == 0)
        LOG("wait_balance: xrWaitFrame while the frame waited before was never begun (%.0f ms): closed it without "
            "layers (begin %d, end %d; %d so far)", WB_GRACE_NS / 1e6, rb, re, n);
}

static void wb_after_wait(XrSession session, XrResult result, XrTime predicted) {
    if (!wait_balance || !XR_SUCCEEDED(result)) return;
    pthread_mutex_lock(&wb_lock);
    if (session != wb_session) {
        wb_session = session;
        wb_pending = wb_debt = wb_swallowed_open = 0;
    }
    ++wb_pending;
    wb_stale_time = predicted;
    pthread_mutex_unlock(&wb_lock);
}

// 1: the game's xrBeginFrame belongs to a frame FrameBridge already closed: answer it without the runtime
static int wb_begin_swallow(XrSession session) {
    if (!wait_balance) return 0;
    pthread_mutex_lock(&wb_lock);
    int swallow = session == wb_session && wb_debt > 0 && wb_pending == 0;
    if (swallow) {
        --wb_debt;
        wb_swallowed_open = 1;
    }
    pthread_mutex_unlock(&wb_lock);
    return swallow;
}

static void wb_after_begin(XrSession session, XrResult result) {
    if (!wait_balance || !XR_SUCCEEDED(result)) return;
    pthread_mutex_lock(&wb_lock);
    if (session == wb_session && wb_pending > 0) --wb_pending;
    pthread_mutex_unlock(&wb_lock);
}

// 1: the game's xrEndFrame ends a frame whose begin FrameBridge answered: answer it without the runtime
static int wb_end_swallow(void) {
    if (!wait_balance) return 0;
    pthread_mutex_lock(&wb_lock);
    int swallow = wb_swallowed_open;
    wb_swallowed_open = 0;
    pthread_mutex_unlock(&wb_lock);
    return swallow;
}
