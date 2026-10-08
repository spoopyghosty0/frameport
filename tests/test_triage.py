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


def test_vrapi_called_before_init_suggests_the_stub_not_the_bridge():
    log = ("10-06 02:04:56.040  1154  1180 I OVRPlugin: OVRPlugin 1.89.1 initialized\n"
           "10-06 02:04:56.324  1154  1180 F VrApiLoader: vrapi_SetPropertyInt was called before vrapi_Initialize()!\n")
    r = triage(log, "EXITED", "com.coatsink.alone")
    ids = {f.id for f in r.findings}
    assert "vrapi-before-init" in ids and "direct-vrapi" not in ids  # Jurassic World Aftermath (GitHub #62)
    assert r.suggestions() == ["frame.vrapi_stub"]


def test_missing_vrapi_function():
    log = ('10-06 00:53:00.662  1124  1124 E AndroidRuntime: java.lang.UnsatisfiedLinkError: Unable to load native '
           'library "/data/app/x/lib/arm64-v8a/libtargemapp.so": dlopen failed: cannot locate symbol '
           '"vrapi_PollEvent" referenced by "libtargemapp.so"\n')
    ids = {f.id for f in triage(log, "EXITED", "ru.targem.blazerush").findings}
    assert "vrapi-symbol-missing" in ids and "java-crash" not in ids


def test_unreal_vulkan_driver_crash_and_missing_fdm():
    """Metro Awakening (Unreal 5.2): Turnip crashed on Unreal's render passes, null-image barriers and FDM views."""
    tomb = ("10-06 08:04:22.702  1236  1236 F DEBUG   :       #00 pc 0000000000ad74d0  "
            "/vendor/lib64/hw/vulkan.freedreno.so (BuildId: 052b)\n"
            "10-06 08:04:22.703  1236  1236 F DEBUG   :       #03 pc 0000000004822ab4  "
            "/data/app/x/lib/arm64/libUnreal.so (BuildId: 7a70)\n")
    r = triage(LOG_OK, "EXITED", "com.example.game", crash=tomb)
    f = next(f for f in r.findings if f.id == "unreal-vulkan-driver-crash")
    assert "adapter.vk_hide_fdm" in f.suggest and not any(f.id == "native-crash" for f in r.findings)
    fdm = ("10-06 08:10:51.264  1147  1241 I FrameBridge: vk shim: left out 1 image barrier(s) without an image "
           "(first: layout 0 -> 1000218000, aspect 0x1, layers 4294967295)\n")
    assert "unreal-fdm-missing" in [f.id for f in triage(LOG_OK + fdm, "RUNNING", "com.example.game").findings]
    hidden = ("10-06 08:10:47.924  1147  1165 I FrameBridge: vk shim: fragment density map extensions hidden from "
              "the game\n")
    found = triage(LOG_OK + hidden + fdm, "RUNNING", "com.example.game").findings
    assert "unreal-fdm-missing" not in [f.id for f in found]


def test_session_without_graphics_requirements():
    log = "10-06 18:13:02.714  1115  1157 E TBXR    : Failed to create XR session: -50.\n"
    assert "graphics-requirements-missing" in {f.id for f in triage(log, "EXITED", "com.drbeef.lambda1vr").findings}


def test_avatar_driver_missing():
    log = ("10-06 18:42:51.349  1130  1158 I OVRAvatar-Loader: ovrAvatar_Initialize: Failed to load AvatarSDK "
           "driver (-1)!\n10-06 18:42:51.351  1130  1158 F OVRAvatar-Loader: DisplayErrorAndExit: Failed to launch "
           "SystemActivities\n")
    assert triage(log, "EXITED", "ru.targem.blazerush").suggestions() == ["frame.avatar_stub"]


def test_x86_linux_program_without_fex():
    from frameport.validate import triage

    log = "launch.sh: line 20: /home/steamos/Applications/quest-frame/linux.x/app/x: cannot execute binary file: " \
          "Exec format error\n"
    assert "linux-x86-no-fex" in [f.id for f in triage.triage(log).findings]


def test_android_too_new_signatures():
    """GitHub #71 (XTADIUM) and #72 (NBA): minSdk 34 apps on Lepton's Android 11."""
    for line in (
        "10-06 20:11:02.123  4211  4211 E AndroidRuntime: java.lang.NoClassDefFoundError: Failed resolution of: "
        "Landroid/window/OnBackInvokedCallback;",
        "10-06 20:11:02.123  4211  4230 E AndroidRuntime: java.lang.NoSuchMethodError: No static method "
        "storeStoreFence()V in class Ljava/lang/invoke/VarHandle; or its super classes",
    ):
        r = triage(line + "\n", "EXITED")
        assert [f.id for f in r.findings] == ["android-too-new"] and r.verdict == "fail"
    assert not triage("E AndroidRuntime: java.lang.NoClassDefFoundError: Lcom/example/Foo;\n", "EXITED").findings


def test_web_wrapper_signature():
    """GitHub #86 (Mahjong Table VR): a TWA looking for Meta's browser."""
    log = ("10-06 18:02:11.100  3301  3301 D TWALauncherActivity: Using URL from Manifest "
           "(https://mahjong-vr.pages.dev/).\n"
           "10-06 18:02:11.120  3301  3301 D TwaLauncher: Creating TwaLauncher for com.oculus.browser\n"
           "10-06 18:02:11.130  3301  3301 W PackageIdentity: android.content.pm.PackageManager$"
           "NameNotFoundException: com.oculus.browser\n")
    r = triage(log, "EXITED")
    assert {f.id for f in r.findings} == {"web-wrapper"} and r.verdict == "fail"
