// SPDX-License-Identifier: GPL-3.0-only
// Included by frame_adapter.c. XR_EXT_performance_settings on a runtime without it (setting perf_settings_emul,
// default on).
//
// OVRPort's dispatcher offers XR_EXT_performance_settings to every game and looks xrPerfSettingsSetPerformanceLevelEXT
// up through FrameBridge; the Frame's runtime has no such function, so the dispatcher answered every call with
// XR_ERROR_FUNCTION_UNSUPPORTED. Most games ignore that, some give up: OutRun Quest VR destroys its instance and quits
// ("CPU performance: -7", GitHub #336), flycast logs it for both domains (GitHub #400). A performance level is only a
// hint (the Frame manages its clocks itself), so FrameBridge answers XR_SUCCESS without doing anything; a runtime that
// has the function gets the call unchanged. Uses perf_settings_emul, lookup, active_instance and LOG.

static XRAPI_ATTR XrResult XRAPI_CALL hook_perf_settings_level(XrSession session, XrPerfSettingsDomainEXT domain,
                                                               XrPerfSettingsLevelEXT level) {
    PFN_xrPerfSettingsSetPerformanceLevelEXT fn =
        (PFN_xrPerfSettingsSetPerformanceLevelEXT)lookup(active_instance, "xrPerfSettingsSetPerformanceLevelEXT");
    XrResult result = fn ? fn(session, domain, level) : XR_ERROR_FUNCTION_UNSUPPORTED;
    if (result != XR_ERROR_FUNCTION_UNSUPPORTED) return result;
    static int logs;
    if (logs < 4) {
        ++logs;
        LOG("perf_settings: %s level %d accepted without effect (the runtime has no XR_EXT_performance_settings)",
            domain == XR_PERF_SETTINGS_DOMAIN_CPU_EXT ? "CPU" : domain == XR_PERF_SETTINGS_DOMAIN_GPU_EXT ? "GPU" : "?",
            (int)level);
    }
    return XR_SUCCESS;
}
