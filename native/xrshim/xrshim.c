// SPDX-License-Identifier: GPL-3.0-only
// FrameBridge extension shim: lets engine plugins reach OpenXR functions that FrameBridge emulates but overport's
// dispatcher (libopenxr_loader.so) doesn't know. overport answers xrGetInstanceProcAddr from a fixed table and returns
// "Unknown proc addr" for anything else (e.g. the XR_FB_render_model functions), so the adapter underneath never sees
// the request.
//
// Meta's OVRPlugin finds xrGetInstanceProcAddr with dlopen("libopenxr_loader.so") + dlsym, so FramePort points that
// string in libOVRPlugin.so at this library instead (patches/frame/adapter.py). Names the adapter emulates are served
// by the adapter (framebridge_extension_proc); everything else goes to overport's xrGetInstanceProcAddr unchanged.
//
// Haptics (GitHub #9, found by Klownicle in Lucky's Tale): overport's dispatcher converts an
// XrHapticAmplitudeEnvelopeVibrationFB with its nanosecond duration taken as seconds, so one vibration allocates an
// enormous sample buffer and the Frame runs out of memory and freezes. The shim hands OVRPlugin its own
// xrApplyHapticFeedback, which turns such an envelope into a plain XrHapticVibration (same duration, its RMS
// amplitude) before the dispatcher sees it; PCM vibrations (XrHapticPcmVibrationFB) are converted the same way.
// Everything else passes through unchanged.
// Upstream: ovrport/app#73; tracked in FramePort GitHub #74.
#include <openxr/openxr.h>
#include <android/log.h>
#include <dlfcn.h>
#include <pthread.h>
#include <math.h>
#include <stddef.h>
#include <string.h>

#define TAG "FrameBridge"
#define EXPORT __attribute__((visibility("default")))

typedef PFN_xrVoidFunction (*PFN_extension_proc)(const char *name);

static PFN_xrGetInstanceProcAddr overport_gipa;
static pthread_once_t once = PTHREAD_ONCE_INIT;

static void resolve_overport(void) {
    void *h = dlopen("libopenxr_loader.so", RTLD_NOW | RTLD_NOLOAD);
    if (!h) h = dlopen("libopenxr_loader.so", RTLD_NOW);
    if (h) overport_gipa = (PFN_xrGetInstanceProcAddr)dlsym(h, "xrGetInstanceProcAddr");
    __android_log_print(ANDROID_LOG_INFO, TAG, "extension shim: overport xrGetInstanceProcAddr %s",
                        overport_gipa ? "OK" : "MISSING");
}

static PFN_xrVoidFunction adapter_proc(const char *name) {
    static PFN_extension_proc fn;
    if (!fn) {
        // Loaded by overport's dispatcher once an instance exists; NOLOAD so the shim never loads it by itself.
        void *h = dlopen("libopenxr_loader_generic.so", RTLD_NOW | RTLD_NOLOAD);
        if (h) fn = (PFN_extension_proc)dlsym(h, "framebridge_extension_proc");
        if (!fn) return NULL;
    }
    return fn(name);
}

static PFN_xrApplyHapticFeedback overport_haptics;
static int haptics_logged;

static XRAPI_ATTR XrResult XRAPI_CALL apply_haptic_feedback(XrSession session, const XrHapticActionInfo *info,
                                                           const XrHapticBaseHeader *feedback) {
    if (feedback && feedback->type == XR_TYPE_HAPTIC_AMPLITUDE_ENVELOPE_VIBRATION_FB) {
        const XrHapticAmplitudeEnvelopeVibrationFB *env = (const XrHapticAmplitudeEnvelopeVibrationFB *)feedback;
        // the envelope's energy (RMS), not its peak: a short fading pulse held at its peak for the whole duration
        // felt far stronger than on a Quest (The Boys VR, Jurassic World, BONELAB)
        double sum = 0.0;
        for (uint32_t i = 0; env->amplitudes && i < env->amplitudeCount; ++i)
            sum += (double)env->amplitudes[i] * env->amplitudes[i];
        float rms = env->amplitudeCount ? (float)sqrt(sum / env->amplitudeCount) : 0.0f;
        XrHapticVibration plain = {XR_TYPE_HAPTIC_VIBRATION, NULL, env->duration, XR_FREQUENCY_UNSPECIFIED,
                                   rms > 1.0f ? 1.0f : rms};
        if (haptics_logged++ < 3)
            __android_log_print(ANDROID_LOG_INFO, TAG, "extension shim: haptic envelope (%u samples, %lld ns) -> "
                                "vibration %.2f", env->amplitudeCount, (long long)env->duration, plain.amplitude);
        return overport_haptics(session, info, (const XrHapticBaseHeader *)&plain);
    }
    if (feedback && feedback->type == XR_TYPE_HAPTIC_PCM_VIBRATION_FB) {
        // the other buffered form goes through the same conversion: a plain vibration as long as the samples (all
        // consumed), with their RMS
        const XrHapticPcmVibrationFB *pcm = (const XrHapticPcmVibrationFB *)feedback;
        double sum = 0.0;
        for (uint32_t i = 0; pcm->buffer && i < pcm->bufferSize; ++i)
            sum += (double)pcm->buffer[i] * pcm->buffer[i];
        float rms = pcm->bufferSize ? (float)sqrt(sum / pcm->bufferSize) : 0.0f;
        XrDuration duration = pcm->sampleRate > 0 ? (XrDuration)(pcm->bufferSize / pcm->sampleRate * 1e9) : 0;
        XrHapticVibration plain = {XR_TYPE_HAPTIC_VIBRATION, NULL, duration > 0 ? duration : XR_MIN_HAPTIC_DURATION,
                                   XR_FREQUENCY_UNSPECIFIED, rms > 1.0f ? 1.0f : rms};
        if (pcm->samplesConsumed) *pcm->samplesConsumed = pcm->bufferSize;
        if (haptics_logged++ < 3)
            __android_log_print(ANDROID_LOG_INFO, TAG, "extension shim: haptic PCM (%u samples at %.0f Hz) -> "
                                "vibration %.2f", pcm->bufferSize, pcm->sampleRate, plain.amplitude);
        return overport_haptics(session, info, (const XrHapticBaseHeader *)&plain);
    }
    return overport_haptics(session, info, feedback);
}

EXPORT XRAPI_ATTR XrResult XRAPI_CALL xrGetInstanceProcAddr(XrInstance instance, const char *name,
                                                            PFN_xrVoidFunction *function) {
    pthread_once(&once, resolve_overport);
    if (name && function && instance != XR_NULL_HANDLE && overport_gipa && !strcmp(name, "xrApplyHapticFeedback")) {
        PFN_xrVoidFunction real = NULL;
        XrResult r = overport_gipa(instance, name, &real);
        if (XR_SUCCEEDED(r) && real) {
            overport_haptics = (PFN_xrApplyHapticFeedback)real;
            *function = (PFN_xrVoidFunction)apply_haptic_feedback;
        }
        return r;
    }
    if (name && function && instance != XR_NULL_HANDLE) {
        PFN_xrVoidFunction emulated = adapter_proc(name);
        if (emulated) {
            __android_log_print(ANDROID_LOG_INFO, TAG, "extension shim: %s -> FrameBridge", name);
            *function = emulated;
            return XR_SUCCESS;
        }
    }
    if (!overport_gipa) return XR_ERROR_INITIALIZATION_FAILED;
    return overport_gipa(instance, name, function);
}
