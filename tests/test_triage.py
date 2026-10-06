from frameport.validate.triage import triage

LOG_OK = """
09-28 17:39:01.000  1000  1000 I ActivityManager: Start proc 1147:com.example.game/u0a55 for activity
09-28 17:39:02.000  1147  1174 I FrameBridge: scale=1.00 foveation_fix=1 controller_fix=1
09-28 17:39:02.100  1147  1174 I FrameBridge: xrCreateInstance result=0
09-28 17:39:03.000  1147  1174 I OVRPortVrApi: Created OpenXR session with the application's GLES context
09-28 17:39:05.000  1147  1174 I FrameBridge: pacing: 72.2 fps, displayTime vs predicted: avg 0.00 ms
"""

LOG_BAD = ("""
09-28 17:39:01.000  1000  1000 I ActivityManager: Start proc 1150:com.example.game/u0a55 for activity
09-28 17:39:02.000  1150  1170 E AndroidRuntime: java.lang.UnsatisfiedLinkError: dlopen failed: cannot locate symbol"""
""" "ovr_User_GetLoggedInUser" referenced by "libgame.so"
09-28 17:39:02.000  1150  1170 I GLShim  : SHADER COMPILE FAILED 1: 0:9(1): error: #extension directive is not"""
""" allowed in the middle of a shader
""")


def test_healthy_log():
    r = triage(LOG_OK, "RUNNING", "com.example.game")
    assert r.verdict == "pass"
    assert r.milestone == "Submitting frames"
    assert r.fps == 72.2


def test_failures_map_to_patches():
    r = triage(LOG_BAD, "EXITED", "com.example.game")
    ids = {f.id for f in r.findings}
    assert {"missing-ovr-symbol", "gl-shader-failed"} <= ids
    assert "frame.ovrstubs" in r.suggestions() and "frame.gl_shim" in r.suggestions()
    assert r.verdict == "fail"


def test_launcher_signature():
    r = triage("lepton: APP_ACTIVITY is empty\n", "NEVER_STARTED")
    assert r.suggestions() == ["frame.launcher"]


def test_missing_platform_dll_hides_generic_crash():
    log = ("LogOnline:Display: Oculus: FOnlineSubsystemOculus::InitWithWindowsPlatform()\n"
           "LogWindows:Warning: CreateProc failed (2) ../../../Engine/Binaries/Win64/CrashReportClient.exe\n"
           "LogWindows:Error: Unhandled Exception: 0xc06d007e\n")
    r = triage(log, "EXITED", "rift.robo_recall")
    assert [f.id for f in r.findings] == ["delayload-missing"] and r.suggestions() == []


def test_crash_logcat_signatures():
    crash = ("F DEBUG   :       #00 pc 0000000000074cf0  /data/app/x/lib/arm64/libVkLayer_fossilize.so (BuildId: 8c)\n"
             "F DEBUG   :       #01 pc 000000000007af58  /data/app/x/lib/arm64/libVkLayer_fossilize.so (BuildId: 8c)\n"
             "F DEBUG   :       #04 pc 000000000adb35a0  /data/app/x/lib/arm64/libUE4.so (FVulkanRenderPass::"
             "FVulkanRenderPass(FVulkanDevice&, FVulkanRenderTargetLayout const&)+1372)\n")
    log = "09-30 19:31:36.149  1126  1265 F libc    : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR)\n"
    r = triage(log, "EXITED", "com.example.game", crash=crash)
    assert [f.id for f in r.findings] == ["fossilize-renderpass"]  # supersedes the generic native-crash
    assert r.findings[0].suggest == ["frame.vk_sanitize"]
    assert [f.id for f in triage(log, "EXITED", "com.example.game").findings] == ["native-crash"]
    abort = ("F DEBUG   :       #00 pc 00000000000898b4  /apex/com.android.runtime/lib64/bionic/libc.so (abort+168)\n"
             "F DEBUG   :       #01 pc 0000000000018cf4  /data/app/x/lib/arm64/libopenxr_loader.so "
             "(xrCreateSwapchain+424)\n")
    r = triage(log.replace("11 (SIGSEGV)", "6 (SIGABRT)"), "EXITED", "com.example.game", crash=abort)
    assert [(f.id, f.suggest) for f in r.findings] == [("swapchain-size-abort", ["frame.swapchain_limit"])]


def test_ovrplugin_extension_probe_is_not_a_scene_game():
    probe = LOG_OK + "09-28 17:39:02.200  1147  1174 I OVRPlugin: Unavailable OpenXR extension: XR_FB_scene\n"
    assert "scene-missing" not in {f.id for f in triage(probe, "RUNNING", "com.example.game").findings}
    used = LOG_OK + "09-28 17:39:04.000  1147  1174 E Unity   : xrQuerySpacesFB failed: -12\n"
    assert "scene-missing" in {f.id for f in triage(used, "RUNNING", "com.example.game").findings}


def test_rejected_controller_profile_is_information_only():
    log = LOG_OK + ("09-28 17:39:02.300  1147  1174 E OVRPlugin: OpenXR error: XR_ERROR_PATH_UNSUPPORTED, cmd "
                    "xrSuggestInteractionProfileBindings(m_xrInstance, &plusSuggestedBindings)\n")
    f = {x.id: x for x in triage(log, "RUNNING", "com.example.game").findings}
    assert f["controller-profile-rejected"].severity == "info"
    assert triage(log, "RUNNING", "com.example.game").verdict == "pass"


def test_failed_input_call_logged_by_input_diag():
    log = LOG_OK + ("09-28 17:39:02.400  1147  1174 I FrameBridge: input_diag: unsupported: xrCreateAction -> "
                    "XR_ERROR_NAME_INVALID (trigger pull)\n")
    r = triage(log, "RUNNING", "com.example.game")
    f = {x.id: x for x in r.findings}
    assert f["input-call-failed"].severity == "warning" and r.verdict == "pass"
    accepted = LOG_OK + ("09-28 17:39:02.400  1147  1174 I FrameBridge: input_diag: bindings: "
                         "/interaction_profiles/oculus/touch_controller accepted (30 paths)\n")
    assert "input-call-failed" not in {x.id for x in triage(accepted, "RUNNING", "com.example.game").findings}


def test_swapchain_rect_invalid_is_recognised():
    """GitHub #39: SteamVR rejected PowerWash Simulator's frames (rect a few px past the swapchain)."""
    from frameport.validate import triage

    log = "10-05 08:13:02.100  1149  1312 I FrameBridge: xrEndFrame failed -25\n"
    assert "swapchain-rect-invalid" in [f.id for f in triage.triage(log).findings]


def test_frames_stopped_and_unity_render_crash():
    """The Room VR passed its launch test although Unity's render thread had crashed in libgallium (GitHub #38)."""
    from frameport.validate import triage

    log = ("10-05 23:16:30.000  1  2 I FrameBridge: pacing: 59.0 fps\n"
           "10-05 23:16:33.137  1  3 E CRASH   : \t#01  pc 0000000000c3fe18  /vendor/lib64/libgallium_dri.so ()\n"
           "10-05 23:16:59.000  1  4 I OvrAudio: ovrAudio_Enable\n")
    r = triage.triage(log, "RUNNING")
    assert [f.id for f in r.findings] == ["unity-render-crash"] and r.verdict == "fail"
    alone = triage.triage(log.replace("libgallium_dri", "libother"), "RUNNING")
    assert "frames-stopped" in [f.id for f in alone.findings]
    assert triage.frames_stopped(["10-05 23:16:30.000 x FrameBridge: pacing: 72 fps", "10-05 23:16:40.000 y"]) is None
