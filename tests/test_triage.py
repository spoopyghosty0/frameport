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


def test_shader_fix_layer_findings():
    log = LOG_OK + ("09-28 17:39:06.000  1147  1174 I FrameBridge: shader fix layer: a 12016-byte module differs"
                    " from fix 1 (another build?)\n"
                    "09-28 17:39:06.000  1147  1147 I FrameBridge: shader fix layer: NOT active, Android's GraphicsEnv"
                    " functions not found (setDebugLayers)\n")
    r = triage(log, "RUNNING", "com.example.game")
    ids = {f.id for f in r.findings}
    assert {"zink-shader-fix-mismatch", "zink-shader-layer-inactive"} <= ids
    assert "adapter.zink_shader_dump" in r.suggestions()


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


def test_unreal_msrtt_crash():
    """Star Wars Pinball VR (GitHub #83): Zink jumps to 0x10000 on Unreal's RHIThread."""
    from frameport.validate import triage

    line = ("10-07 11:41:11.156  1127  1231 F libc    : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr "
            "0x10000 in tid 1231 (RHIThread), pid 1127 (MainThread-UE4)\n")
    r = triage.triage(line, "EXITED")
    hit = next(f for f in r.findings if f.id == "unreal-msrtt-crash")
    assert "frame.unreal_gl_shim" in hit.suggest


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


def test_slz_vulkan_hook_crash_is_recognised():
    """BONELAB 1.2974: SLZ's Vulkan plugin crashed Unity's Vulkan start-up (pc 0) before the first frame."""
    log = ("10-07 09:29:07.191  1131  1157 I SlzGfx  : SLZ Graphics plugin loading!\n"
           "10-07 09:29:09.434  1131  1157 I OVRPlugin: Preinitialize: xrDestroyInstance() succeeded\n"
           "10-07 09:29:09.445  1131  1157 E CRASH   :     sp  0000ffff189a2430  lr  0000fffdd93f6ed0  "
           "pc  0000000000000000\n"
           "10-07 09:29:09.500  1131  1157 E AndroidRuntime: FATAL EXCEPTION: UnityMain\n")
    r = triage(log, "EXITED", "com.StressLevelZero.BONELAB")
    f = next(f for f in r.findings if f.id == "slz-vulkan-hook-crash")
    assert f.suggest == ["frame.slz_vulkan_hooks"] and "java-crash" not in [x.id for x in r.findings]
    other = triage(log.replace("SLZ Graphics", "Other"), "EXITED", None)
    assert "slz-vulkan-hook-crash" not in [x.id for x in other.findings]


def test_lepton3_transient_not_running_context_is_not_a_failure():
    """Lepton 3.0.5 prints "is not a running context" while the container is still starting, then boots (VR4 ran
    at 72 fps); only a container that never boots is a failure."""
    from frameport.validate import triage

    err = "ERROR: 'steamlaunch-2272591617' is not a running context, use 'lepton ps' to list them, like this:\n"
    booted = triage.triage("Waiting for boot...\n" + err + "Boot complete!\n", "RUNNING")
    assert "container-not-started" not in [f.id for f in booted.findings]
    failed = triage.triage("Waiting for boot...\n" + err, "EXITED")
    assert "container-not-started" in [f.id for f in failed.findings]


def test_refused_cube_swapchain_explains_the_render_crash():
    """GitHub #107 (Budget Cuts Ultimate): the runtime refuses a cube swapchain, OVRPlugin crashes in EndFrame."""
    from frameport.validate.triage import triage as run

    log = ("10-09 02:34:43.131  1149  1219 I FrameBridge: xrCreateSwapchain 2048x2048 format=35907 samples=1 array=1 "
           "faces=6 usage=0x21 flags=0x0 result=-2\n"
           "10-09 02:34:43.131  1149  1219 D OVRPlugin: CompositorOpenXR_GLES::Layer::Initialize(): CreateSwapchain "
           "for eye 0: 0x0, 0 stages\n"
           "10-09 02:34:43.900  1149  1219 F libc    : Fatal signal 11 (SIGSEGV), code 2 (SEGV_ACCERR)\n"
           "10-09 02:34:44.134  1149  1223 E CRASH   :       #00 pc 0000000000a85a80  /vendor/lib64/dri/"
           "libgallium_dri.so (BuildId: 95)\n")
    r = run(log, "EXITED", "com.NeatCorporation.BudgetCutsUltimate")
    assert [f.id for f in r.findings] == ["cube-swapchain-refused"]
    assert r.findings[0].suggest == ["frame.adapter"]
    served = log + "I FrameBridge: cube_standin: runtime refused a 2048x2048 cube swapchain (result=-2): served\n"
    assert "cube-swapchain-refused" not in [f.id for f in run(served, "RUNNING", "x").findings]


def test_cube_standin_is_a_default_on_adapter_setting():
    from frameport.patches.settings import SETTINGS

    assert [s[2] for s in SETTINGS if s[0] == "cube_standin"] == [1]


def test_unity_pcvr_signatures():
    """Unity's Player.log in a PC VR launch log (agent v67): VR start-up failures and Unity's crash handler."""
    log = ("===== unity log compatdata/pfx/drive_c/users/steamuser/AppData/LocalLow/SUPERHOT Team/SUPERHOT VR/"
           "Player.log\n"
           "XR: OpenVR Error! OpenVR failed initialization with error code VRInitError_Init_HmdNotFound\n")
    r = triage(log, "EXITED", "rift.superhot_vr")
    assert [f.id for f in r.findings] == ["unity-vr-init"] and "pcvr.launch_args" in r.suggestions()
    assert not triage("VRInitError_None\n", "RUNNING", "rift.x").findings
    crash = triage("Crash!!!\nwine: Unhandled page fault\nBacktrace:\n", "EXITED", "rift.x")
    assert [f.id for f in crash.findings] == ["unity-crash"]
    assert not triage(log, "EXITED", "com.example.game").findings  # PC VR signatures only for rift games


def test_hw_video_decoder_busy_from_the_media_service():
    """The codec plugin logs from Android's media service (another pid than the game's): still triaged."""
    for line in ("W FramePortVideo: Iris video/hevc unavailable; keeping Android's stock decoder",
                 "W FramePortVideo: Iris video/hevc initialization failed (-12); using software decoding"):
        log = LOG_OK + f"09-28 17:39:01.500   412   412 {line}\n"
        r = triage(log, "RUNNING", "com.example.game")
        assert [f.id for f in r.findings] == ["hw-video-decoder-busy"] and r.verdict == "pass"
    log = LOG_OK + "09-28 17:39:01.500   412   412 I FramePortVideo: Iris hardware video/hevc decoder active\n"
    assert not triage(log, "RUNNING", "com.example.game").findings


def test_lepton_lines_survive_the_game_filter():
    """With the package known, triage keeps only the game's logcat lines plus Lepton's own: the short "Boot complete!"
    used to be dropped, so Lepton 3's transient "is not a running context" failed every launch test in the GUI."""
    from frameport.validate import triage

    log = ("Waiting for boot...\n"
           "ERROR: 'steamlaunch-1' is not a running context, use 'lepton ps' to list them, like this:\n"
           "Boot complete!\n"
           "10-09 15:25:52.000   100   100 I ActivityManager: Start proc 1206:com.x.y/u0a12 for top-activity\n"
           "10-09 15:25:52.416  1206  1230 I FrameBridge: settings read\n"
           "10-09 15:25:52.500   999   999 I Other: unrelated\n")
    lines = triage.game_lines(log, "com.x.y")
    assert "Boot complete!" in lines and not any("Other: unrelated" in ln for ln in lines)
    assert any("FrameBridge" in ln for ln in lines)
    res = triage.triage(log, "RUNNING", package="com.x.y")
    assert "container-not-started" not in [f.id for f in res.findings]


def test_unity_data_missing_fails_the_launch_test():
    """GitHub #155: Batman's launch test passed while Unity was stuck on a bundle missing from the data folder."""
    from frameport.validate import triage

    log = ("10-10 00:51:10.000  1  2 E Unity   : Unable to open archive file: /sdcard/Android/obb/com.camouflaj.manta/"
           "localization-assets-french(france)(fr-fr)_assets_all.bundle\n"
           "10-10 00:51:20.000  1  2 I FrameBridge: pacing: 72.0 fps\n")
    r = triage.triage(log, "RUNNING")
    assert "unity-data-missing" in [f.id for f in r.findings] and r.verdict == "fail"
