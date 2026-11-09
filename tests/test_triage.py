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


def test_unreal_reservation_refused_while_loading():
    """GitHub #349: Unreal's out-of-memory handler crashes inside libUnreal.so's static constructors."""
    lib = "/data/app/~~x==/com.Meta4.TFBeyondReality-y==/lib/arm64/libUnreal.so (BuildId: 57d064dc)"
    crash = (
        "10-16 14:41:01.814  1185  1185 F libc    : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x10"
        " in tid 1185 (TFBeyondReality), pid 1185 (TFBeyondReality)\n"
        "10-16 14:41:01.858  1219  1219 F DEBUG   : Cause: null pointer dereference\n"
        "10-16 14:41:01.955  1219  1219 F DEBUG   : backtrace:\n"
        f"10-16 14:41:01.955  1219  1219 F DEBUG   :       #00 pc 00000000018bbe3c  {lib}\n"
        f"10-16 14:41:01.955  1219  1219 F DEBUG   :       #01 pc 00000000017ceccc  {lib}\n"
        "10-16 14:41:01.957  1219  1219 F DEBUG   :       #15 pc 000000000004b048  /apex/com.android.runtime/bin/"
        "linker64 (__dl__ZN6soinfo17call_constructorsEv+380)\n")
    r = triage("", "EXITED", crash=crash)
    ids = {f.id for f in r.findings}
    assert "unreal-reserve-refused" in ids and "native-crash" not in ids
    assert next(f for f in r.findings if f.id == "unreal-reserve-refused").suggest == ["frame.unreal_noreserve"]
    # the same fault address in a later render-thread crash isn't this
    later = crash.replace("__dl__ZN6soinfo17call_constructorsEv+380", "__pthread_start(void*)+64")
    assert "unreal-reserve-refused" not in {f.id for f in triage("", "EXITED", crash=later).findings}


def test_lua_pointer_error_and_cleanup_stack_are_distinguished():
    pointer = triage("PANIC: bad light userdata pointer", "EXITED")
    finding = next(f for f in pointer.findings if f.id == "lua-lightuserdata-pointer")
    assert finding.suggest == ["frame.luajit_pointers"] and finding.severity == "fatal"
    cleanup = ("FORTIFY: pthread_mutex_lock called on a destroyed mutex\n"
               "libc.so (__cxa_finalize+148)\nlibc.so (exit+24)\n"
               "libxlua.so (lj_err_throw+428)\nFatal signal 6 (SIGABRT)\n")
    result = triage(cleanup, "EXITED")
    finding = next(f for f in result.findings if f.id == "lua-startup-exit")
    assert finding.suggest == []  # unhandled Lua errors have other causes too
    assert "lua-lightuserdata-pointer" not in {f.id for f in result.findings}
    assert "lua-startup-exit" not in {f.id for f in triage("Unknown Platform\n" + LOG_OK, "RUNNING").findings}


def test_healthy_log():
    r = triage(LOG_OK, "RUNNING", "com.example.game")
    assert r.verdict == "pass"
    assert r.milestone == "Submitting frames"
    assert r.fps == 72.2


def test_vrapi_rejected_frames_fail_even_with_pacing_and_a_headless_end():
    """The Climb's old debug frame flag produced paced empty frames, so its launch test falsely passed (#238)."""
    log = (LOG_OK + "09-28 17:39:06.000  1147  1174 E OVRPortVrApi: "
           "Unsupported frame flags or swap interval\n"
           "09-28 17:39:35.000  1147  1174 I OVRPortVrApi: "
           "EnterVrMode: no tracked head pose before deadline\n"
           "09-28 17:39:36.000  1147  1174 F libc: Fatal signal 11 (SIGSEGV)\n")
    r = triage(log, "EXITED", "com.example.game", headless=True)
    assert r.fps == 72.2 and r.verdict == "fail"
    finding = next(f for f in r.findings if f.id == "vrapi-frame-rejected")
    assert finding.severity == "fatal" and finding.suggest == ["frame.vrapi_bridge"]


def test_whole_log_unreal_signatures_only_search_from_the_start():
    import re

    from frameport.validate.triage import database

    traces = {
        "unreal-splash-frame-stall":
            "FlushRenderingCommands\nCSxrSession::WaitFrame\nFSplash::RenderFrame_RenderThread\n",
        "unreal-multiview-depth-size":
            "ViewLog: attachment 8d00 renderbuffer=1920x1016\n"
            "ViewLog: attachment 8ce0 renderbuffer=1728x1728\n",
    }
    for sig in database()["signatures"]:
        if sig["id"] not in traces:
            continue
        pattern = re.compile(sig["pattern"])
        log = "ordinary log line\n" * 6000 + traces[sig["id"]]
        assert pattern.search(log).start() == 0
        # Whole-log lookaheads must not be retried at every character when a marker is missing.
        assert pattern.search(log, 1) is None
        assert pattern.search("ordinary log line\n" * 6000) is None


def test_foveation_skipped_is_diagnostic_not_an_automatic_game_failure():
    log = (LOG_OK + "09-28 17:39:06.000  1147  1174 D com.valvesoftware.fdm: "
           "*** fdm_injection_CreateInstance: skipped ***\n")
    r = triage(log, "RUNNING", "com.example.game")
    finding = next(f for f in r.findings if f.id == "foveation-instance-skipped")
    assert finding.severity == "info"
    assert finding.suggest == []
    assert r.verdict == "pass"


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


def test_shader_fix_unmatched():
    for prefix in ("vk shim", "shader fix layer"):
        log = LOG_OK + (f"09-28 17:39:06.000  1147  1174 I FrameBridge: {prefix}: 2 shader fix(es) configured, "
                        "none matched after 812 modules in 300 s (game updated?)\n")
        r = triage(log, "RUNNING", "com.example.game")
        f = next(f for f in r.findings if f.id == "shader-fix-unmatched")
        assert f.severity == "info" and r.verdict != "fail"
    # a fix that matched logs its own line, not this one
    log = LOG_OK + "09-28 17:39:06.000  1147  1174 I FrameBridge: vk shim: fixed shader module (100 -> 108 bytes): 0\n"
    assert "shader-fix-unmatched" not in {f.id for f in triage(log, "RUNNING", "com.example.game").findings}


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


def test_tbxr_fdm_crash_is_identified_as_vr_teardown():
    log = "F libc: Fatal signal 11 (SIGSEGV), fault addr 0x0 in AppThreadFunction\n"
    crash = ("#00 libVkLayer_VALVE_fdm_injection.so (fdm_injection_xrDestroySwapchain+136)\n"
             "#01 libopenxr_loader_generic.so (xrDestroySwapchain+2600)\n"
             "#03 libxash.so (ovrRenderer_Destroy+44)\n"
             "#04 libxash.so (TBXR_LeaveVR+76)\n")
    r = triage(log, "EXITED", "com.drbeef.lambda1vr", crash=crash)
    assert [f.id for f in r.findings] == ["tbxr-fdm-shutdown-crash"]
    assert r.suggestions() == []  # no unverified graphics or foveation fix
    assert [f.id for f in triage(log, "EXITED", crash=crash.replace("TBXR_LeaveVR", "OtherCall")).findings] == \
        ["native-crash"]
    assert not triage(crash, "RUNNING").findings  # symbol names alone are not a crash


def test_ovrplugin_extension_probe_is_not_a_scene_game():
    probe = LOG_OK + "09-28 17:39:02.200  1147  1174 I OVRPlugin: Unavailable OpenXR extension: XR_FB_scene\n"
    assert "scene-missing" not in {f.id for f in triage(probe, "RUNNING", "com.example.game").findings}
    # extension listings (GitHub #404): OVRPlugin's enumeration, OVRPort's dispatcher, Unity's Meta XR feature
    for listing in ("I OVRPlugin:  Name=XR_FB_scene SpecVersion=1", "I overportOXR: - XR_FB_scene 1",
                    'I Unity   : Meta XR Feature: Extensions="XR_FB_passthrough XR_FB_scene XR_META_boundary"',
                    "I overportOXR: - XR_FB_scene_capture 1"):
        log = LOG_OK + f"09-28 17:39:02.200  1147  1174 {listing}\n"
        assert "scene-missing" not in {f.id for f in triage(log, "RUNNING", "com.example.game").findings}, listing
    wants = LOG_OK + "09-28 17:39:02.200  1147  1174 E Unity   : Required extension XR_FB_scene not available\n"
    assert "scene-missing" in {f.id for f in triage(wants, "RUNNING", "com.example.game").findings}
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


def test_frames_stopped_orders_replayed_old_lines_by_date():
    """BattleTalent (GitHub #293): logcat replays kernel lines dated days before the game's ahead of them; by time
    of day alone they looked like "past midnight" and a healthy run failed with frames-stopped (83261 s)."""
    from frameport.validate import triage

    log = ("10-16 00:16:59.468    18    18 I hwservicemanager: hwservicemanager is ready now.\n"
           "10-14 00:40:12.674    16    16 W auditd  : type=2000 audit(0.0:1): state=initialized\n"
           "10-14 18:25:54.684     0     0 I binder  : 418000 RLIMIT_NICE not set\n"
           "10-14 23:59:59.000     0     0 I binder  : 419099 RLIMIT_NICE not set\n"
           "10-15 02:10:00.000     0     0 I binder  : 419099 RLIMIT_NICE not set\n"
           "10-15 23:50:00.000     0     0 I binder  : 419099 RLIMIT_NICE not set\n"
           "10-16 00:17:33.725  1193  1274 I FrameBridge: pacing: 70.2 fps, displayTime vs predicted: avg 15 ms\n"
           "10-16 00:18:08.734  1193  1274 I FrameBridge: pacing: 72.2 fps, displayTime vs predicted: avg 13 ms\n"
           "10-16 00:18:13.837  1193  1274 I FrameBridge: layer_debug: frames: 361 xrBeginFrame\n")
    assert triage.frames_stopped(log.splitlines()) is None
    assert "frames-stopped" not in [f.id for f in triage.triage(log, "RUNNING").findings]
    # a real stop still counts, also across midnight and New Year
    assert triage.frames_stopped(["10-15 23:59:50.000 x FrameBridge: pacing: 72 fps", "10-16 00:00:30.000 y"]) == 40
    assert triage.frames_stopped(["12-31 23:59:50.000 x FrameBridge: pacing: 72 fps", "01-01 00:00:30.000 y"]) == 40
    assert triage.frames_stopped(["12-31 23:59:50.000 x FrameBridge: pacing: 72 fps", "01-01 00:00:05.000 y"]) is None


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


def test_cube_quads_setting_and_artifacts():
    from frameport.patches.frame import artifact
    from frameport.patches.frame.adapter import settings_text
    from frameport.patches.settings import SETTINGS, UI

    assert [s[2] for s in SETTINGS if s[0] == "cube_quads"] == [1]
    assert UI["cube_quads"]["control"] == ("switch",)
    assert b"cube_quads=1\n" in settings_text({"adapter.cube_quads": {}})
    assert b"cube_quads=0\n" in settings_text({"adapter.cube_quads": {"value": 0}})
    for abi in ("arm64-v8a", "armeabi-v7a"):
        data = artifact(abi, "libopenxr_loader_generic.so")
        assert b"cube_quads=%f" in data
        assert b"cube_quads: submitting cube as six quads" in data


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


def test_external_fmod_banks_missing_fails_even_with_frames():
    """GitHub #239: Aftermath renders its error screen at 72 fps with no data installed."""
    for root in ("/storage/emulated/0", "/sdcard"):
        log = LOG_OK + (
            f'10-12 17:41:17.092  1147  1174 E Unity   : Error loading FMOD bank "{root}/Android/obb/'
            'com.coatsink.alone/Master.bank": ERROR\n'
            f'10-12 17:41:17.189  1147  1174 E Unity   : Error loading FMOD bank "{root}/Android/obb/'
            'com.coatsink.alone/Master.strings.bank": ERROR\n')
        r = triage(log, "RUNNING", "com.example.game")
        f = next(f for f in r.findings if f.id == "fmod-bank-missing")
        assert f.severity == "fatal" and f.suggest == []
        assert "Master.bank" in f.evidence
        assert r.fps and r.verdict == "fail"
        assert sum(f.id == "fmod-bank-missing" for f in r.findings) == 1


def test_fmod_bank_diagnosis_requires_an_external_data_path():
    log = LOG_OK + 'E Unity : Error loading FMOD bank "Master.bank": ERROR\n'
    assert "fmod-bank-missing" not in {f.id for f in triage(log, "RUNNING").findings}


def test_cloud_save_exception_during_startup_fails_with_frames():
    log = LOG_OK + (
        '10-12 19:25:05.325  1147  1174 E Unity : UnauthorizedAccessException: Access to the path '
        '"/sdcard/Android/data/com.coatsink.alone/files/cloud/data" is denied.\n\n'
        '10-12 19:25:05.325  1147  1174 E Unity : at System.IO.Directory.CreateDirectoriesInternal (String path)\n\n'
        '10-12 19:25:05.325  1147  1174 E Unity : at SaveLoad.GetSlotPaths (String dataPath)\n\n'
        '10-12 19:25:05.325  1147  1174 E Unity : at StartupLoading+<Start>d__11.MoveNext ()\n')
    r = triage(log, "RUNNING", "com.example.game")
    assert r.fps and r.verdict == "fail"
    assert "startup-save-permission" in {f.id for f in r.findings}
    assert "save-folder-permission" not in {f.id for f in r.findings}
    # A save error later in gameplay remains an error, rather than claiming that startup stopped.
    later = log.replace("StartupLoading+<Start>d__11", "SaveLoad+<Save>d__12")
    r = triage(later, "RUNNING", "com.example.game")
    assert "startup-save-permission" not in {f.id for f in r.findings}
    assert next(f for f in r.findings if f.id == "save-folder-permission").severity == "error"


def test_webview_helper_exit_is_not_a_force_quit():
    """GitHub #179: Android's WebView helper exits at every container boot; the game itself never started."""
    log = """
10-11 05:09:26.499   220   249 I ActivityManager: Start proc 367:WebViewLoader-arm64-v8a/1037
10-11 05:09:26.546   220   249 I ActivityManager: Start proc 397:com.android.networkstack.process/1073 for service
10-11 05:09:26.579   367   367 I WebViewLoader-: System.exit called, status: 0
LeptonProcessObserver: Target app (com.example.game) did not start.
"""
    res = triage(log, "EXITED", package="com.example.game")
    ids = [f.id for f in res.findings]
    assert "force-quit" not in ids and "app-not-started" in ids
    assert "App process started" not in res.milestones
    # a real System.exit from the game still counts
    real = "09-28 17:39:01.000  1000  1000 I ActivityManager: Start proc 1150:com.example.game/u0a55 for activity\n" \
           "09-28 17:39:05.000  1150  1150 I GameActivity: System.exit called, status: 0\n"
    assert "force-quit" in [f.id for f in triage(real, "EXITED", package="com.example.game").findings]


HEADLESS_END = """
10-11 16:35:09.900  1000  1000 I ActivityManager: Start proc 1192:ru.targem.blazerush/u0a14 for activity
10-11 16:35:12.282  1192  1222 I FrameBridge: xrCreateInstance result=0
10-11 16:35:18.219  1192  1222 I FrameBridge: pacing: 68.7 fps, displayTime vs predicted: avg 0.00 ms, max 0.00 ms
10-11 16:35:38.316  1192  1222 I FrameBridge: pacing: 72.2 fps, displayTime vs predicted: avg 0.00 ms, max 0.00 ms
10-11 16:35:43.150  1192  1222 I OVRPortVrApi: EnterVrMode: no tracked head pose before deadline
10-11 16:35:52.086  1192  1248 E MESA    : zink: DEVICE LOST!
10-11 16:35:52.087  1192  1248 F libc    : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x1a08
"""


def test_crash_after_the_headless_tracking_deadline_passes_a_launch_test():
    """BlazeRush (GitHub #57): its launch test failed on the GPU crash that follows the VrApi bridge ending the session
    for lack of a worn headset; that never happens in the headset."""
    r = triage(HEADLESS_END, "EXITED", "ru.targem.blazerush", headless=True)
    assert r.verdict == "pass" and r.headless_end
    assert [f.id for f in r.findings] == ["headless-end"] and r.findings[0].severity == "info"
    assert "zink-device-lost" in r.findings[0].diagnosis
    # a play session (not headless) keeps the crash
    assert triage(HEADLESS_END, "EXITED", "ru.targem.blazerush").verdict == "fail"
    # a crash before the deadline still fails
    early = HEADLESS_END.replace("16:35:52.08", "16:35:40.08")
    early_lines = early.strip().splitlines()
    early = "\n".join(early_lines[:4] + early_lines[5:] + early_lines[4:5])
    r = triage(early, "EXITED", "ru.targem.blazerush", headless=True)
    assert r.verdict == "fail" and {"zink-device-lost", "native-crash"} <= {f.id for f in r.findings}
    # no frames before the deadline: the game never ran
    no_frames = "\n".join(ln for ln in HEADLESS_END.splitlines() if "pacing:" not in ln)
    assert triage(no_frames, "EXITED", "ru.targem.blazerush", headless=True).verdict == "fail"


def test_pixelripped_asset_status_error():
    log = "E Unity : |||||||||| Asset status error  (code 100)\nE Unity : DLC_Manager:AssetStatusCallback(Message`1)\n"
    result = triage(log, "RUNNING")
    assert "meta-asset-status-error" in {f.id for f in result.findings}
    assert "frame.asset_files_stub" in result.suggestions()
    assert "meta-asset-status-error" not in {f.id for f in triage("Asset status OK\n", "RUNNING").findings}


def test_projection_frames_without_pacing_are_informational():
    for projection in ("new layer: type=35 swapchain=0x1 usable=1", "projection view 0: space=0x1"):
        log = (f"10-15 12:00:01.000  1  2 I FrameBridge: {projection}\n"
               "10-15 12:00:26.000  1  2 I Game: still alive\n")
        r = triage(log, "RUNNING")
        f = next(f for f in r.findings if f.id == "frames-stopped")
        assert f.severity == "info" and f.report and r.verdict == "unknown"
        for state in ("EXITED", "NEVER_STARTED"):
            assert "frames-stopped" not in [f.id for f in triage(log, state).findings]
        for extra in ("FrameBridge: pacing: 72 fps", "OVRPortVrApi: EnterVrMode: no tracked head pose before deadline"):
            normal = log + f"10-15 12:00:27.000  1  2 I {extra}\n"
            assert "frames-stopped" not in [f.id for f in triage(normal, "RUNNING", headless=True).findings]
        short = log.replace("12:00:26", "12:00:10")
        assert "frames-stopped" not in [f.id for f in triage(short, "RUNNING").findings]


def test_obb_and_cube_advice_describes_limits():
    obb = triage("Displayed com.example.game/.DownloaderActivity", "RUNNING")
    advice = next(f for f in obb.findings if f.id == "unreal-obb-check-stuck")
    assert "only when the APK enables bVerifyOBBOnStartUp" in advice.diagnosis
    assert "frame.unreal_skip_obb_delivery" in advice.diagnosis
    assert "neither option is a guaranteed fix" in advice.diagnosis
    cube = triage("FrameBridge: xrCreateSwapchain 512x512 format=43 faces=6 result=-9", "RUNNING")
    advice = next(f for f in cube.findings if f.id == "cube-swapchain-refused")
    assert "GLES" in advice.diagnosis
    assert "For a Vulkan game, the current build cannot stand in" in advice.diagnosis


# Deadly Delivery 1.0.30 (GitHub #322): renders, then its lobby's Meta entitlement check fails four times and it quits
LOG_ENTITLEMENT = """
10-15 12:45:23.964   212   240 I ActivityManager: Start proc 1214:com.FlatHeadStudio.DeadlyDelivery/u0a14 for \
pre-top-activity {com.FlatHeadStudio.DeadlyDelivery/com.unity3d.player.UnityPlayerActivity}
10-15 12:45:38.349  1214  1238 E Unity   : [LobbyManager]: Entitlement check failed: .
10-15 12:45:38.349  1214  1238 E Unity   : \
FlatHeads.<>c__DisplayClass105_0:<CheckEntitlementAndConfigureAsync>b__0(Message)
10-15 12:45:38.352  1214  1238 I Unity   : [LobbyManager]: Retrying oculus authentication in 2 seconds.
10-15 12:45:43.372  1214  1286 I FrameBridge: pacing: 71.6 fps, displayTime vs predicted: avg 13.97 ms, max 83.33 ms
10-15 12:45:46.349  1214  1238 I Unity   : stopping xr plugin
10-15 12:45:47.424  1214  1238 I Unity   : [Photon] Disconnected from Photon (None)
10-15 12:45:48.857  1214  1214 I Process : Sending signal. PID: 1214 SIG: 9
"""


def test_meta_entitlement_quest():
    r = triage(LOG_ENTITLEMENT, "EXITED", "com.FlatHeadStudio.DeadlyDelivery")
    f = next(f for f in r.findings if f.id == "meta-entitlement")
    assert f.severity == "error" and f.suggest == []
    assert "Entitlement check failed" in f.evidence
    assert "oculus-entitlement" not in {x.id for x in r.findings}  # the Rift signature stays PC VR only
    assert r.verdict == "fail"
    assert "meta-entitlement" in {x.id for x in triage("E Unity: ovrPlatformInitialize_NotEntitled", "EXITED").findings}
    # routine Platform SDK lines of games that work don't match; a game that logs it and keeps running still passes
    for ok in ("I Unity   : CheckEntitlementAndConfigureAsync", "I Unity   : Entitlement check passed",
               "I Unity   : ovr_Entitlement_GetIsViewerEntitled", "I Unity   : User is entitled"):
        assert "meta-entitlement" not in {x.id for x in triage(ok + "\n" + LOG_OK, "RUNNING").findings}
    assert triage("E Unity   : Entitlement check failed\n" + LOG_OK, "RUNNING").verdict == "pass"


LOG_META_REQUEST = """
E Unity   : An unknown error occurred. Request failed.
E Unity   : LogHandler:LogFormat(LogType, Object, String, Object[])
E Unity   : Oculus.Platform.Request`1:OnComplete(Callback)
E Unity   : Port3R.Logistics.<Init>d__18:MoveNext()
"""


def test_meta_request_failed():
    r = triage(LOG_META_REQUEST + LOG_OK, "RUNNING", "com.rrrgames.ThiefSimVRQuest")
    f = next(f for f in r.findings if f.id == "meta-request-failed")
    assert f.severity == "warning" and f.suggest == [] and f.report
    assert r.verdict == "pass"  # the game keeps running: a hint, not a failure
    # the same words from something other than Meta's platform wrapper don't match
    other = "E Unity   : An unknown error occurred. Request failed.\nE Unity   : UnityWebRequest:SendWebRequest()\n"
    assert "meta-request-failed" not in {x.id for x in triage(other + LOG_OK, "RUNNING").findings}


def test_meta_org_scoped_id_wait():
    log = ("10-23 18:31:41.484  1253  1277 I Unity   : [LOAD EVENT]:[META.FUN] in meta platform "
           "HandlePlatformInitialization\n"
           "10-23 18:31:41.488    47   712 W resolv  : Failed to tag socket: Bad file descriptor\n"
           "10-23 18:31:41.746  1253  1277 E Unity   : An unknown error occurred. Request failed.\n"
           "10-23 18:31:41.746  1253  1277 E Unity   : Oculus.Platform.Request`1:OnComplete(Callback)\n")
    r = triage(log + LOG_OK, "RUNNING", "quest.eleven.forfunlabs")
    ids = {x.id for x in r.findings}
    assert "meta-org-scoped-id-wait" in ids and "meta-request-failed" not in ids
    assert next(f for f in r.findings if f.id == "meta-org-scoped-id-wait").suggest == ["frame.ovr_org_scoped_id"]
    # another request's failure stays the generic hint
    assert "meta-org-scoped-id-wait" not in {x.id for x in triage(LOG_META_REQUEST + LOG_OK, "RUNNING").findings}


def test_meta_app_version_wait():
    # C-Smash VRS (GitHub #503) with Unity's log on: the version check is the frame after OnComplete
    log = ("11-09 16:47:25.158  1202  1228 E Unity   : An unknown error occurred. Request failed.\n"
           "11-09 16:47:25.158  1202  1228 E Unity   : Oculus.Platform.Request`1:OnComplete(Callback)\n"
           "11-09 16:47:25.158  1202  1228 E Unity   : OculusNetworkManager:OnCoreServicesInitalised()\n"
           "11-09 16:47:25.158  1202  1228 E Unity   : Oculus.Platform.Callback:HandleMessage(Message)\n")
    r = triage(log + LOG_OK, "RUNNING", "com.wolfandwood.csmashvs")
    ids = {x.id for x in r.findings}
    assert "meta-app-version-wait" in ids and "meta-request-failed" not in ids
    assert next(f for f in r.findings if f.id == "meta-app-version-wait").suggest == ["frame.ovr_app_version"]
    other = log.replace("OculusNetworkManager:OnCoreServicesInitalised", "Game.Store:CheckLatestVersion")
    assert "meta-app-version-wait" in {x.id for x in triage(other + LOG_OK, "RUNNING").findings}
    # another request's failure stays the generic hint
    assert "meta-app-version-wait" not in {x.id for x in triage(LOG_META_REQUEST + LOG_OK, "RUNNING").findings}


def test_meta_system_activities():
    log = ("W System.err: \tat android.content.ContextWrapper.startActivity(ContextWrapper.java:403)\n"
           "F OVRPlatform-Loader: DisplayErrorAndExit: Failed to launch SystemActivities\n"
           "F libc    : Fatal signal 6 (SIGABRT), code -1 (SI_QUEUE) in tid 1335 (UnityMain), pid 1221\n"
           "F DEBUG   : Abort message: 'DisplayErrorAndExit: Failed to launch SystemActivities'\n")
    r = triage(log, "EXITED", "com.TriangleFactory.Breachers")
    ids = {x.id for x in r.findings}
    assert "meta-system-activities" in ids and "native-crash" not in ids
    assert next(f for f in r.findings if f.id == "meta-system-activities").severity == "fatal"
    assert r.verdict == "fail"


def test_missing_quest_foveation_image_is_diagnostic():
    message = "OVRPlugin: foveationImage = 0x0, width = 0, height = 0"
    result = triage(message, "EXITED")
    finding = next(f for f in result.findings if f.id == "quest-foveation-image-missing")
    assert finding.severity == "info" and finding.question
    assert finding.suggest == ["adapter.hide_fb_foveation"]
    assert "adapter.hide_fb_foveation" not in result.suggestions()
    result = triage(message + "\nFrameBridge: hide_fb_foveation: filtering dispatcher extensions", "EXITED")
    assert "quest-foveation-image-missing" not in {f.id for f in result.findings}


def test_swapchain_count_limit_signature():
    line = ("10-18 12:00:00 I FrameBridge: xrCreateSwapchain 256x128 format=35907 samples=1 array=1 "
            "faces=1 usage=0x1 flags=0x0 result=-10 handle=0x0")
    findings = triage(line, "RUNNING").findings
    finding = next(f for f in findings if f.id == "swapchain-limit-reached")
    assert finding.suggest == [] and "count limit" in finding.diagnosis
    for other in (line.replace("result=-10", "result=0"), line.replace("result=-10", "result=-100"),
                  line.replace("FrameBridge:", "Other:"), "FrameBridge: xrEndFrame result=-10"):
        assert "swapchain-limit-reached" not in {f.id for f in triage(other, "RUNNING").findings}


def test_cryengine_init_failure_is_named():
    # GitHub #432/#449: The Climb 2 on FramePort 1.3.0, then SIGSEGV in CSystem::ShutDown
    log = ("10-23 16:21:18.880  1170  1198 I Cry     : Messagebox: cap: Engine initialization failed!  "
           "text:CrySystem initialization failed!\n"
           "10-23 16:21:18.886  1170  1198 F libc    : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), "
           "fault addr 0x0\n")
    assert "cryengine-init-failed" in [f.id for f in triage(log, package="com.crytek.climb2").findings]


def test_vrapi_bridge_without_framebridge_is_named():
    # GitHub #432: The Climb 1 built without the layer FrameBridge is linked against
    log = ("10-24 22:34:39.134  1198  1223 E OVRPortVrApi: xrInitializeLoaderKHR failed: OpenXR result -7\n"
           "10-24 22:34:39.134  1198  1223 F libc    : Fatal signal 5 (SIGTRAP), code -6 (SI_TKILL) in tid 1223\n")
    assert "vrapi-bridge-no-loader" in [f.id for f in triage(log, package="com.crytek.climb").findings]


def test_decoder_held_by_steam_is_named():
    """GitHub #437: Steam's web helper kept the Iris decoder; Batman's 8K cutscenes went to software."""
    log = "FramePort: hardware video decoder /dev/video22 in use by: steamwebhelper(2953) \n"
    assert "video-decoder-held-by-steam" in [f.id for f in triage(log, package="com.camouflaj.manta").findings]
    assert "video-decoder-held-by-steam" not in [f.id for f in triage(log.replace("steamwebhelper(2953)", ""),
                                                                      package="com.camouflaj.manta").findings]


def test_sdl_second_activity_exit():
    """GitHub #475 (Xelee): the VR activity, moved into the launcher's process, starts after SDL main finished."""
    log = ("10-25 19:07:34.039  1220  1252 V SDL     : Finished main function\n"
           "10-25 19:07:34.069  1220  1220 V SDL     : onCreate()\n"
           "10-25 19:07:34.069  1220  1220 V SDL     : SDL main() finished\n"
           "10-25 19:07:34.069  1220  1220 V SDL     : activity finished\n"
           "10-25 19:07:34.069  1220  1220 I dev.melee.game: System.exit called, status: 0\n")
    ids = [f.id for f in triage(log, "EXITED", "dev.melee.game").findings]
    assert "sdl-second-activity-exit" in ids and "force-quit" not in ids
