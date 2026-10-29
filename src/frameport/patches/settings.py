"""FrameBridge adapter settings and device-side files, exposed as patches so the UI can toggle them.

Adapter settings end up in lib/<abi>/libframe_settings.so (build time) and in <install>/settings.conf +
Android/data/<pkg>/files/framebridge.conf on the Frame (can be changed later without re-patching).
The GL shim reads framebridge.conf too (gl_hide_multiview), as does the multiview interposer (gl_mv_debug).
"""
from __future__ import annotations

import math
import re

from .base import InstallContext, Param, Patch, Suggestion, register

# Always written (in this order) so every build carries explicit values.
BASE_SETTINGS = {"scale": 1.0, "foveation_fix": 1, "controller_fix": 1}

# key, kind, default, title, description
SETTINGS = [
    ("fixed_resolution", "int", 0, "Keep full resolution (no dynamic resolution)",
     "Keeps OpenXR layer recommendations at the full swapchain size. Prevents dynamic resolution from moving "
     "Valve foveation off-centre or leaving a coarse white edge line. May increase GPU load."),
    ("scale", "float", 1.0, "Resolution scale",
     "Multiplies the recommended eye-buffer width and height (0.5–2.0). 1.5 ≈ 2.25× pixels."),
    ("foveation_fix", "int", 1, "Hide Quest foveation", "Hides Quest foveation extensions the Frame runtime lacks."),
    ("snapshot", "int", 0, "Eye-image snapshots (diagnostics)",
     "Every N seconds, saves the left-eye image the game submits (a quarter of its size) as fb_snap_0-7.ppm in the "
     "game's files folder on the Frame. Launch tests run without anyone wearing the headset, so the headset view stays "
     "black; this shows what the game itself draws. Vulkan games: both eyes at half size (fb_snap_vk_N_vV.ppm), "
     "the whole swapchain image with the submitted rect and FOV in the log."),
    ("gl_error_clear", "int", 0, "Clear the game's GL errors (GLES)",
     "Clears OpenGL errors the game left pending before FrameBridge calls the Frame's runtime. The runtime's GL code "
     "fails on any pending error; after one such failure at the start it allocated GPU memory every frame without "
     "freeing it (e.g. Vader Immortal, ~20 MB a second; QuestZDoom slowed to a crawl after a few minutes)."),
    ("flip_dump", "int", 0, "Upside-down panel images (diagnostics)",
     "Saves up to N images of the UI panels FrameBridge turns upright (Vulkan games whose panels are stored upside "
     "down), exactly as the runtime gets them, as fb_flip_<n>.ppm in the game's files folder on the Frame (a new file "
     "whenever a panel's content changes). Shows what is displayed without anyone wearing the headset."),
    ("strip_color_bias", "int", 0, "Drop layer color fades",
     "Removes the color scale/bias (XR_KHR_composition_layer_color_scale_bias) from the game's layers, so the runtime "
     "doesn't apply it (diagnostics). Fades done this way no longer show."),
    ("fade_quad", "int", 0, "Draw fades over the picture",
     "Games that fade to black by dimming their picture with a layer color scale (XR_KHR_composition_layer_color_"
     "scale_bias, for example Batman: Arkham Shadow's loading screens) can show the level loading instead of black on "
     "the Frame. FrameBridge then draws the fade itself: a black quad in front of the eyes whose opacity follows the "
     "game's dimming. 2 also removes the game's own dimming (if both show, the fade is doubled)."),
    ("hide_fb_foveation", "int", 0, "Turn off Quest Vulkan foveation",
     "Hides Quest FB foveation extensions from the game and OVRPort, removes Vulkan foveation image/create chains, "
     "and refuses foveation profile updates. Diagnostic for first-frame failures with missing density images "
     "(for example Transformers Beyond Reality: Redux). Device verification pending; "
     "does not disable Valve foveation."),
    ("hide_space_warp", "int", 0, "Turn off space warp",
     "Hides XR_FB_space_warp, so the game renders every frame itself instead of half of them plus motion vectors "
     "(Application SpaceWarp). For games whose picture flickers or smears on the Frame (for example Unreal Engine 5 "
     "games such as Into The Radius 2)."),
    ("space_warp_emul", "int", 0, "Provide space warp",
     "For games that require Meta's Application SpaceWarp and quit without it (\"AppSW recommended buffers "
     "unavailable\", for example OutRun Quest VR, GitHub #336). FrameBridge offers XR_FB_space_warp, reports a "
     "motion-vector size (the runtime's, else a quarter of the eye size), serves the game's motion-vector and "
     "space-warp depth images itself (OpenGL ES textures or Vulkan images, which the Frame's runtime wouldn't take) "
     "and removes the space warp info from every frame: the game renders every frame normally and its motion "
     "vectors are not used. Overrides Turn off space warp."),
    ("controller_fix", "int", 1, "Report Touch controllers",
     "Reports Frame controllers as Oculus Touch and hides synthetic hand tracking. Set 0 for games that require hand "
     "tracking (for example Silhouette)."),
    ("quest_buttons", "int", 0, "Controller buttons",
     "Choose how the Frame's face buttons send Quest Touch inputs: 0 = SteamVR default, 1 = Mirror, "
     "2 = Quest pair. Used at the next launch; SteamVR custom bindings can override it. Requires the runtime's "
     "XR_VALVE_frame_controller_interaction extension. Mapping: "
     "https://github.com/spoopyghosty0/frameport/blob/main/docs/CONTROLLERS.md"),
    ("swapchain_fix", "int", 1, "Swapchain format fallback",
     "Retry rejected GLES formats/MSAA with sRGB, samples=1."),
    ("cube_quads", "int", 1, "Show cube layers",
     "Copies cube-map loading screens into six panels around the viewer (GLES and Vulkan). Mixed projection frames "
     "are skipped because the Frame draws panels above the scene. Requires cube_standin."),
    ("cube_standin", "int", 1, "Stand-in cube swapchains",
     "The Frame's runtime has no cube-map layers and refuses their swapchains; OVRPlugin then crashes when it submits "
     "the next frame (for example Budget Cuts Ultimate). FrameBridge serves such a swapchain itself on GLES and "
     "Vulkan; cube_quads displays its loading screens."),
    ("surface_native", "int", 0, "Native video surfaces (Batman renderer)",
     "Plays the game's Android video surface through GPU-shared buffers and composes it as a stereo panorama (Batman: "
     "Arkham Shadow's cutscenes, see docs/SURFACE_VIDEO.md). Specific to that game's renderer: set by its recipe, not "
     "shown in Game settings. Use with frame.hw_video_decode."),
    ("layer_fix", "int", 1, "Drop invalid layers",
     "Drop layers whose swapchain failed or whose extension isn't enabled."),
    ("passthrough_emul", "int", 1, "Emulate passthrough",
     "XR_FB_passthrough via ALPHA_BLEND (Frame grayscale cameras)."),
    ("alpha_blend_emul", "int", 1, "Emulate layer alpha blending",
     "With emulated passthrough, honours the eye layer's XR_FB_composition_layer_alpha_blend, which the Frame's "
     "runtime ignores: Unreal games keep inverted alpha and ask for inverse blending, so the room showed where the "
     "game drew and black where it should show the room (for example Star Wars: Beyond Victory). The alpha is "
     "inverted in the game's image before it is composited (Vulkan)."),
    ("flip_emul", "int", 1, "Emulate flipped quads",
     "Shows quads flagged XrCompositionLayerImageLayoutFB VERTICAL_FLIP as upside-down copies, refreshed every frame "
     "(Vulkan). Fixes UI panels and text that show upside down (for example Assassin's Creed Nexus)."),
    ("cylinder_strips", "int", 1, "Show curved panels",
     "Cylinder layers (curved menus and movie screens, for example 4XVR), which the Frame's runtime lacks, are shown "
     "as a few flat strips along the curve. 0 = drop them. 360° (equirect) layers can't be shown on the Frame."),
    ("surface_emul", "int", 1, "Show video panels",
     "Video panels that an Android media player draws into (XR_KHR_android_surface_swapchain, for example I Am "
     "Monkey's intro video), which the Frame's runtime refuses, get an Android Surface from FrameBridge: each video "
     "frame is copied into an ordinary swapchain the panel shows. Without it these panels are missing and a game "
     "waiting for its video stays black. 0 = leave them to the runtime."),
    ("swapchain_pool", "int", 0, "Share panel images (swapchain limit)",
     "Share panel images in runtime atlases to avoid the Frame's swapchain count limit (about 15 per session). "
     "Useful when opening extra menus closes them or leaves panels gray, or when a game stops drawing because a new "
     "set of eye images was refused. GLES and Vulkan apps; on Vulkan only sRGB panels are shared, eye images never."),
    ("equirect_emul", "int", 0, "Show 360° layers",
     "360° (equirect) layers, for example a video player's virtual theater or 360° videos (for example 4XVR), which "
     "the Frame's runtime lacks, are drawn by a background thread into a layer behind the game's own picture: the "
     "360° image is converted only when it changes (a theater once, a 360° video once per video frame) and the view "
     "is drawn for each frame's head pose. GLES games only; without it these layers are missing (black)."),
    ("equirect_face", "int", 1536, "360° detail",
     "Maximum size in pixels of each face of the cube the 360° image is converted to (256–2730). Higher is sharper "
     "but uses more GPU memory."),
    ("equirect_res", "int", 1536, "360° view resolution",
     "Size in pixels per eye of the 360° background, redrawn every frame on a background thread (512–4096). Higher is "
     "sharper but costs more GPU time per frame."),
    ("equirect_flip", "int", 0, "360° picture orientation",
     "Only if 360° pictures show wrongly: 1 = upside down, 2 = mirrored, 4 = turned around (add values to combine)."),
    ("equirect_fps", "float", 60.0, "360° redraw limit (per second)",
     "At most this many redraws per second of a 360° layer (0 = no limit). Video players need at least the video's "
     "frame rate."),
    ("equirect_stereo", "int", 0, "360° stereo",
     "Stereo (3D) 360° layers: 0 = as the game sends them, 2 = flat (the left image to both eyes)."),
    ("stable_local", "int", 0, "Keep the play space still",
     "Every 'local' play space the game creates lines up with the one at the start. For games whose menus or screens "
     "jump to where you look on the Frame."),
    ("focus_hold", "int", 1, "Ignore brief focus dips",
     "Hides the Frame's brief focus dips (up to focus_hold_ms) once the game has been focused for a second. On by "
     "default: the Frame's wear sensor flickers \"HMD off\" for 0.5-2 s while worn, and many games pause or recenter "
     "every time (for example Blade & Sorcery, Lucky's Tale)."),
    ("focus_hold_ms", "float", 5000.0, "Longest focus dip to ignore (ms)",
     "focus_hold hides focus dips up to this long (100-5000). Longer for a headset whose wear sensor flickers "
     "(\"HMD off\" for 0.5-2 s while worn, for example Blade & Sorcery pausing); taking the headset off or the system "
     "menu pauses the game only after this long."),
    ("sync_guard", "int", 0, "Guard controller input after focus",
     "Runs xrSyncActions one at a time with xrPollEvent and skips it for 250 ms after focus returns. For games that "
     "crash in the runtime's input code right after focus comes back (SIGSEGV in vrclient.so UpdateActionStateInternal "
     "/ xrSyncActions, for example Myst)."),
    ("profile_remap", "int", 1, "Newer Touch profiles as Touch",
     "Apps that suggest controller bindings only for Meta's newer profiles (Touch Plus, Touch Pro), which the Frame's "
     "runtime rejects (XR_ERROR_PATH_UNSUPPORTED), get the same bindings as oculus/touch_controller instead "
     "(components Touch lacks are dropped), and the runtime's answer for each hand is reported to them as their own "
     "profile (Unity's OpenXR input only connects controllers of a profile it knows). Meta's detached-controller paths "
     "(XR_META_detached_controllers, which the Frame lacks) are left out of the app's actions and bindings: one such "
     "path made Unity attach no controller input at all. Without it such apps get no controller input."),
    ("haptic_scale", "float", 1.0, "Vibration strength",
     "Scales every controller vibration the game asks for (0-1): plain vibrations, Meta's amplitude envelopes and "
     "PCM buffers. For games whose vibrations feel much stronger than on a Quest."),
    ("aim_pitch", "float", 0.0, "Pointer tilt (degrees)",
     "Tilts the controllers' pointing ray up (+) or down (−), for games whose pointer doesn't hit what you aim at."),
    ("aim_yaw", "float", 0.0, "Pointer turn (degrees)", "Turns the controllers' pointing ray left (+) or right (−)."),
    ("aim_forward", "float", 0.0, "Pointer origin forward (m)",
     "Moves where the pointing ray starts forward (+) or back (−)."),
    ("refresh_rate", "float", 0.0, "Refresh rate (Hz)",
     "Display refresh rate for this game (72, 80, 90, 96, 108, 120 or 144; 0 = the game's choice). Applied through "
     "SteamVR before the game starts. A video's frame "
     "rate that divides the refresh rate plays smoothest (for example 30 fps at 90 Hz, 24 fps at 72 Hz)."),
    ("layer_debug", "int", 0, "Extra diagnostics (log)",
     "Logs details for debugging: composition layers and swapchains, focus changes, play-space creation, pointer vs "
     "grip poses, refresh rates. No effect on the game."),
    ("eye_debug", "int", 0, "Per-eye diagnostics (log)",
     "For one eye looking wrong (for example a jittering right eye): logs each eye's submitted pose against a fresh "
     "one, the eye-to-eye relation and when each eye's image was released, also into framebridge.log in the game's "
     "storage (some games stop Android's log early). No effect on the game."),
    ("input_diag", "int", 0, "Controller input diagnostics (log)",
     "For buttons that do nothing or the wrong thing: logs each controller profile the game suggests bindings for and "
     "whether the runtime accepts it (with every path of a rejected one), which profile the runtime reports for each "
     "hand (a game without Steam Frame bindings gets SteamVR's Touch remap), OpenXR functions the game looks up that "
     "the runtime lacks, and failing input, vibration and performance calls. Each line once. No effect on the game."),
    ("release_wait", "int", 0, "Wait for the game's GPU before showing an image",
     "Before a swapchain image is handed to the Frame, wait until the game's GPU work on it has finished, for games "
     "that hand over images early (an eye flickers or jitters). Costs some frame time. 2 = only after the first 60 s "
     "(compare both in one session)."),
    ("scene_emul", "int", 0, "Emulate Meta scene (room)",
     "Serve Meta scene and spatial entity APIs from the saved room (walls, furniture and mesh). Without a saved "
     "room, use a simple play-area box (for example Demeter)."),
    ("controller_models", "int", 0, "Steam Frame controller models",
     "Games that ask the headset for its controller models (Meta's runtime controller models, XR_FB_render_model) get "
     "the Steam Frame controllers instead of Quest Touch controllers. The models come from the Frame's own SteamVR and "
     "are converted on the Frame at install time. Games that ship their own controller meshes aren't affected. Turning "
     "it on needs a rebuild (it adds a small library in front of OVRPort's loader)."),
    ("scene_scan_request", "int", 1, "Scan when the game asks for a room",
     "With scene_emul, let FramePort scan your room in the headset when the game requests a scan. "
     "If no scanner answers, use the saved room. Turn off to always use the saved room."),
    ("scene_hide_bounds", "int", 0, "Hide the play-area size from the game",
     "With scene_emul, the game's play-area (STAGE bounds) query answers \"no play area\". The Frame reports a 1x1 m "
     "area whose outline OVRPlugin can't give, and some Unity MR games then fail their guardian check (for example "
     "Demeter). Off by default: other games stalled without a play area."),
    ("scene_height", "float", 2.5, "Emulated room height (m)", "Ceiling height for scene_emul."),
    ("scene_width", "float", 0.0, "Emulated room width (m)",
     "Override the guardian width (0 = use guardian, min 1.5 m)."),
    ("scene_depth", "float", 0.0, "Emulated room depth (m)",
     "Override the guardian depth (0 = use guardian, min 1.5 m)."),
    ("pose_consistency", "int", 0, "Consistent head poses per frame",
     "Repeat xrLocateViews queries for the same display time get the first, fully tracked answer again. Games that "
     "ask several times per frame got slightly different poses and rendered parts of the frame with different heads: "
     "judder (for example I Am Cat; proposed by Klownicle, GitHub #8)."),
    ("pose_time_fix", "int", 0, "Locate poses at the right time",
     "Poses a game asks for at a CLOCK_MONOTONIC time, or more than 0.5 s before the frame's display time, are located "
     "at the same moment in the runtime's time (or now). Meta's OVRPlugin passes the monotonic clock on as the "
     "OpenXR time, which on the Frame runs seconds behind the runtime's clock, so its \"now\" poses (Unity's physics "
     "step, controller poses) came from the past: hands trailing the controllers (for example BattleSisters, Sniper "
     "Elite VR)."),
    ("proximity_emul", "int", 0, "Finger proximity from touch",
     "Meta's OVRPlugin reports a thumb (or index finger) resting near the controller through "
     "XR_FB_touch_controller_proximity, which the Frame's runtime lacks: \"near\" stays false and games that animate "
     "the hands from it show a thumb that never moves (for example Vader Immortal; found by Klownicle, GitHub #49). "
     "Binds the game's proximity actions to the capacitive touch inputs instead: 1 = thumb (thumbstick, face buttons "
     "or thumb rest touched), 2 = also the index finger (trigger touched). Only when the runtime lacks the extension."),
    ("pose_debug", "int", 0, "Pose time diagnostics (log)",
     "Every 5 s, logs per located space how far the requested times are from the frame's predicted display time, and "
     "the runtime clock's offset from the monotonic clock. No effect on the game."),
    ("haptic_fix", "int", 0, "Fix controller vibration freezes",
     "Routes OVRPlugin through FramePort's extension shim, which turns Meta's amplitude-envelope vibrations into "
     "plain ones: OVRPort's loader reads their nanosecond duration as seconds and allocates gigabytes, freezing the "
     "Frame when the game vibrates a controller (for example Lucky's Tale's save slots, GitHub #9). Needs a rebuild."),
    ("rect_clamp", "int", 1, "Clamp image rects",
     "Keeps every submitted image rect inside its swapchain. Unity can size the eye area a few pixels past the image "
     "on some Frames; SteamVR then rejects every frame (xrEndFrame -25, XR_ERROR_SWAPCHAIN_RECT_INVALID) and the game "
     "stops drawing (for example PowerWash Simulator stuck at \"Waiting\"). An empty rect (0x0, e.g. BattleTalent's "
     "first frames, GitHub #293) is rejected the same way and becomes the whole swapchain image."),
    ("fb_color_space", "int", 0, "Colour space extension (XR_FB_color_space)",
     "Offers Meta's colour-space extension when the Frame's runtime lacks it: the game is told the headset uses "
     "Rec.709 and may choose any colour space (no conversion is done). For games that quit before their VR "
     "session with \"Rec.709 color space unavailable\" (for example OutRun Quest VR, GitHub #336). Only reaches "
     "games that use their own OpenXR loader; OVRPort's dispatcher can't forward the new functions."),
    ("action_dedup", "int", 1, "Unique controller action names",
     "Gives a controller action whose name already exists in its action set a free name (\"turn\" -> \"turn_2\"). "
     "The Frame's runtime accepts duplicate names but then can't load the game's input manifest and treats the game "
     "as closed: black, then the home environment while the game keeps running with sound (for example Scared By "
     "Squares, whose Unreal project has an action and an axis both called Turn; GitHub #323)."),
    ("swap_eyes", "int", 0, "Swap eyes", "Swap left/right views (diagnostic)."),
    ("strip_depth", "int", 0, "Strip depth layers", "Remove XR_KHR_composition_layer_depth chains (diagnostic)."),
    ("mutable_fix", "int", 0, "Mutable swapchain fix", "Experimental Vulkan mutable-format workaround."),
    ("respace_kick", "int", 0, "Re-create reference space", "Recreate spaces after the first frames (diagnostic)."),
    ("flip_quads", "int", 0, "Rotate quads 180°",
     "Older workaround for upside-down quads: rotates them 180° (quads are single-sided; prefer flip_emul)."),
    ("vk_shader_fix", "str", "", "Vulkan shader fixes",
     "Vulkan shim (frame.vk_sanitize): SPIR-V modules matching size + SHA-256 get words inserted at a byte offset, "
     "for example stores that initialize locals a shader reads before writing (an undefined loop counter hung the GPU "
     "in VR4's campaign). Format: <size>:<sha256>:<byte offset>:<word>,<word>,...; several separated by ';'. Comes "
     "from a game's recipe."),
    ("zink_shader_fix", "str", "", "OpenGL ES shader fixes",
     "Shader-fix Vulkan layer (frame.zink_shader_fix) for OpenGL ES games, whose GLSL the Frame's GL driver (Zink) "
     "turns into SPIR-V: modules matching size + SHA-256 get words inserted at a byte offset, for example stores that "
     "set loop counters and accumulators a shader reads before writing (Vader Immortal's lightspeed jump hung the GPU; "
     "found by Klownicle, GitHub #49). Same format as vk_shader_fix. Comes from a game's recipe; the driver's "
     "output can change with a Frame update, then the fix no longer matches (logged)."),
    ("zink_shader_dump", "int", 0, "OpenGL ES shader dump (diagnostics)",
     "Shader-fix Vulkan layer (frame.zink_shader_fix): writes every distinct SPIR-V module the GL driver creates to "
     "Android/data/<package>/files/fp_spirv/<size>_<sha256>.spv, to capture a shader for a new zink_shader_fix. No "
     "effect on the game."),
    ("vk_shader_dump", "int", 0, "Vulkan shader dump (diagnostics)",
     "Vulkan shim (frame.vk_sanitize): writes every distinct SPIR-V module the game creates to "
     "Android/data/<package>/files/fp_vk_shaders/<size>_<sha256>.spv, with index.txt listing every creation in order "
     "with its time, to find the shader behind a GPU hang and capture it for a new vk_shader_fix. Diagnostics include "
     "the newest modules. No effect on the game."),
    ("vk_query_slots", "int", 0, "Vulkan shim: slots per occlusion query",
     "Vulkan shim (frame.vk_sanitize): on gives every occlusion query room for both eyes. In multiview passes the "
     "Frame's driver writes a zero result for the second eye into the next query, so engines that count on one slot "
     "cull visible objects (models popping in and out, for example Into The Radius 2). 0 = off, 1 = on (2 slots)."),
    ("vk_spec_fixes", "int", 0, "Vulkan shim: depth spec fixes",
     "Vulkan shim (frame.vk_sanitize): makes two Unreal habits valid Vulkan - depth images get "
     "VK_IMAGE_USAGE_TRANSFER_DST_BIT (they are cleared with vkCmdClearDepthStencilImage) and Qualcomm shader-resolve "
     "subpasses lose an invalid depth resolve. Found with the validation layer in Into The Radius 2 (flickering "
     "models, windows behind models). Also leaves out image barriers without an image and refuses image views without "
     "one, which crash the Frame's driver (for example Metro Awakening)."),
    ("vk_pipeline_log", "int", 0, "Vulkan shim: pipeline log (diagnostics)",
     "Vulkan shim (frame.vk_sanitize): logs every failing graphics/compute pipeline creation with its shader stages "
     "(named like the vk_shader_dump files), structure types and any sampler Y'CbCr conversion involved, to find the "
     "pipeline behind Unreal's \"Shader compilation failures are Fatal.\" No effect on the game."),
    ("vk_no_fossilize", "int", 0, "Vulkan shim: without Lepton's Fossilize layer",
     "Vulkan shim (frame.vk_sanitize): takes Lepton's Fossilize shader-cache layer out of the game's Vulkan layers "
     "(Android's debug layer list) when the engine loads its Vulkan library. With Fossilize loaded every graphics "
     "pipeline of Transformers Beyond Reality failed (VK_ERROR_OUT_OF_HOST_MEMORY), which Unreal treats as fatal "
     "(GitHub #349). Lepton has no switch for it; instances created before the engine loads the shim keep it."),
    ("vk_pipeline_fallback", "int", 0, "Vulkan shim: blank shaders for refused pipelines",
     "Vulkan shim (frame.vk_sanitize): a graphics pipeline the Frame's driver refuses is created again with a fragment "
     "shader that writes zero, then with a vertex shader that only sets the position as well, so the draw shows "
     "nothing instead of Unreal ending with \"Shader compilation failures are Fatal.\" (for example a few multiview "
     "vertex shaders in Transformers Beyond Reality, GitHub #349). Logged as \"retried with a blank\"."),
    ("vk_validation", "int", 0, "Vulkan shim: validation layer",
     "Diagnostics: the Vulkan shim (frame.vk_sanitize) adds Khronos' validation layer to the game's instance; its "
     "findings go to launch.log. The layer library (libVkLayer_khronos_validation.so) must be in the APK."),
    ("gl_mv_debug", "int", 0, "Multiview interposer: diagnostics",
     "Diagnostics for frame.gl_multiview_fbo: checks for GL errors after every single-view draw of a multiview "
     "shader and logs counters every 5 seconds (tag GLMV in launch.log). 2 (frameport settings) also checks every "
     "draw and logs the draw state, failed texture uploads and shader compiles and, for idTech 4 engines, the game "
     "clock."),
    ("gl_hide_msrtt", "int", 1, "GL shim: hide multisampled render-to-texture",
     "GL shim only: hide GL_EXT_multisampled_render_to_texture(2) (Zink crashes rendering Unity's runtime MSAA eye "
     "buffer through it, for example The Room VR)."),
    ("gl_gamma_space", "int", 0, "GL shim: gamma colour space",
     "GL shim (frame.gl_shim): keep sRGB write conversion (GL_FRAMEBUFFER_SRGB_EXT) off in every context the game "
     "draws through. The Frame takes only sRGB eye images, so swapchain_fix hands a game GL_SRGB8_ALPHA8 where it "
     "asked for GL_RGBA8; a game that renders in gamma colour space (Unity's Gamma setting, for example PLAY'A VR) "
     "writes already gamma-encoded colours, which the sRGB image encodes a second time: the picture is washed out, "
     "grey instead of black. Unity loads its GL functions with dlsym, so the shim acts on eglMakeCurrent, which Unity "
     "imports through it."),
    ("vk_hide_fdm", "int", 0, "Vulkan shim: hide fragment density maps",
     "Vulkan shim (frame.vk_sanitize): the game doesn't see VK_EXT_fragment_density_map(2). Unreal Engine 5 turns on "
     "fragment-density-map foveation when the driver offers it and expects the density map from the headset, which the "
     "Frame doesn't provide: image views and barriers for a missing image, then a crash in the Frame's Vulkan driver "
     "(for example Metro Awakening). Valve's own foveation layer isn't affected."),
    ("fdm_bin_w", "int", 0, "Foveation: bin width (Valve foveation)",
     "Width in pixels of the GMEM bins the Frame's driver (Turnip) renders the eye passes in, 0 = worked out "
     "automatically. The driver gives each bin the density Valve's map has at the bin's centre, so the full-resolution "
     "area is a block of whole bins; FramePort centres that block on each eye and works out the bin size the way the "
     "driver does. Set this and fdm_bin_h (both non-zero) only when that comes out wrong for a game: the sizes are "
     "measured with \"Darken coarse fragments (test)\" and snapshot=N (multiples of 96 wide, 32 high; GitHub #394)."),
    ("fdm_bin_h", "int", 0, "Foveation: bin height (Valve foveation)",
     "Height in pixels of the driver's GMEM bins for the eye passes, 0 = worked out automatically. See fdm_bin_w."),
    ("ovrp_begin_gate", "int", 0, "Unity frame loop: wait for skipped frames",
     "Unity's built-in Oculus frame loop (frame.unity_oculus_check): before each frame wait, wait up to 50 ms for the "
     "last waited frame to begin and skip the wait if it doesn't. Fixes a freeze at the first scene switch (for "
     "example Sniper Elite VR); in other games it can make loading screens stutter."),
    ("ovrp_hold_physics", "int", 0, "Unity frame loop: keep display-time poses",
     "Unity's built-in Oculus frame loop (frame.unity_oculus_check): don't pass Unity's physics-step pose update on, "
     "so the hands use the frame's display-time poses. Fixes hands trailing the controllers in some games (for "
     "example Sniper Elite VR); in others it makes the hands lag (for example BattleSisters)."),
    ("gl_hide_multiview", "int", 1, "GL shim: hide multiview",
     "GL shim only: hide GL_OVR_multiview so all passes use single-view shaders. For GLES games whose multiview "
     "shaders fail on single-view render targets (for example Path of the Warrior)."),
    ("gl_int_attribs", "int", 0, "GL shim: integer vertex inputs",
     "GL shim (frame.gl_shim): shader inputs declared as integers (int/uint, ivecN/uvecN) whose vertex data the game "
     "set up with glVertexAttribPointer (as floats) are set up again with glVertexAttribIPointer before each draw. "
     "Qualcomm's Quest driver read the raw integers anyway; the Frame's GL driver converts them to floats, so skinned "
     "models using bone indices vanish (for example BlazeRush's car bodies and controllers; Klownicle's guide, GitHub "
     "#57). Comes from a game's recipe."),
    ("gl_highp_markers", "str", "", "GL shim: high-precision vertex shaders",
     "GL shim (frame.gl_shim): vertex shaders whose source contains every '|'-separated text compute in highp: "
     "whole-word mediump becomes highp on every line without the words in, out or uniform (the shader's interface "
     "keeps its precision). For effects that draw streaks or flashes from mediump rounding on the Frame (for example "
     "BlazeRush's particles: in uvec2 inBoneIndices;|uniform InstanceData|void main()). Pair it with a new shader "
     "cache name if the game caches compiled programs. Comes from a game's recipe."),
]


# settings that need a new build, not only new settings files: the Vulkan shim learned vk_shader_fix in 0.6.4 and
# vk_query_slots and vk_spec_fixes in 0.10.0. haptic_fix stays at 2 although the shim also converts PCM vibrations
# since 0.12.1: no game is known to need that, and a bump would mark every OVRPlugin game's build outdated
REVISIONS = {"vk_shader_fix": 2, "vk_query_slots": 3, "vk_spec_fixes": 2, "haptic_fix": 2, "pose_consistency": 2,
             "gl_int_attribs": 2, "gl_highp_markers": 2, "surface_native": 4}

# Settings a game's recipe sets that the Game settings dialog doesn't show (engine-specific, not user choices)
HIDDEN = {"surface_native", "gl_int_attribs"}

# How the "Game settings" dialog shows each setting to non-technical users: group, level (common settings are always
# shown; advanced ones only under "Show advanced settings"), a plain label and one-line help, the control, and the
# setting it depends on (only shown while that one is on). The technical title/description above stay for the CLI.
GROUPS = [("picture", "Picture"), ("controllers", "Controllers"), ("screens", "Menus and screens"),
          ("room", "Mixed reality"), ("video360", "360° pictures and video"), ("troubleshooting", "Troubleshooting")]
REFRESH_CHOICES = [(0.0, "Game's choice"), *((float(hz), f"{hz} Hz") for hz in (72, 80, 90, 96, 108, 120, 144))]
FOVEATION_UI = {
    "mode": dict(group="picture", level="common", label="Foveated rendering (Valve)",
                 help="Reduces shading away from the full-resolution area. Changes apply at the next game start."),
    "level": dict(group="picture", level="common", label="Full-resolution area",
                   help="Larger areas retain more detail but save less GPU work."),
    "test": dict(group="picture", level="advanced", label="Darken coarse fragments (test)",
                 help="Diagnostic only; do not use for benchmarking."),
}

UI: dict[str, dict] = {
    "fixed_resolution": dict(group="picture", level="advanced", label="Keep full resolution (no dynamic resolution)",
                help="Prevents dynamic resolution from moving Valve foveation off-centre or leaving a white edge line. "
                     "May increase GPU load. Used at the next launch.", control=("switch",)),
    "scale": dict(group="picture", level="common", label="Render resolution (supersampling)",
                  help="Changes the game's render resolution. Higher is sharper but slower; 1.0× is the game's own "
                       "resolution.",
                  control=("slider", 0.5, 2.0, 0.1, "{:.1f}×")),
    "refresh_rate": dict(group="picture", level="common", label="Refresh rate",
                         help="How often the picture updates. Higher is smoother if the game can keep up.",
                         control=("choice", REFRESH_CHOICES)),
    "controller_fix": dict(group="controllers", level="common", label="Use controllers",
                           help="Turn off only for games you play with your hands instead of controllers.",
                           control=("switch",)),
    "quest_buttons": dict(group="controllers", level="common", label="Controller buttons",
                          help="Choose which Quest Touch buttons the Frame's face buttons send. "
                               "Takes effect at the next launch.",
                          control=("choice", [(0, "SteamVR default"), (1, "Mirror"), (2, "Quest pair")])),
    "focus_hold_ms": dict(group="screens", level="advanced", label="Ignore focus dips up to", depends="focus_hold",
                          help="Longer for headsets that briefly think they're off your head.",
                          control=("slider", 500.0, 3000.0, 100.0, "{:.0f} ms")),
    "fade_quad": dict(group="picture", level="advanced", label="Draw fades over the picture",
                      help="For games whose fades to black don't show, so you see the level loading (for example "
                           "Batman: Arkham Shadow).", control=("switch",)),
    "pose_consistency": dict(group="picture", level="advanced", label="Steadier head tracking",
                             help="For games that judder or shimmer while you hold your head still (for "
                                  "example I Am Cat).",
                             control=("switch",)),
    "pose_time_fix": dict(group="controllers", level="advanced", label="Fix hands lagging behind controllers",
                          help="For games whose hands trail the controllers when you move them (for example "
                               "BattleSisters).",
                          control=("switch",)),
    "proximity_emul": dict(group="controllers", level="advanced", label="Animate fingers from touch",
                           help="For games whose hands' thumbs never move when you touch the buttons or thumbstick "
                                "(for example Vader Immortal).",
                           control=("choice", [(0, "Off"), (1, "Thumbs"), (2, "Thumbs and index fingers")])),
    "haptic_fix": dict(group="controllers", level="advanced", label="Fix freezes when controllers vibrate",
                       help="For games that freeze the Frame when a controller vibrates (for example Lucky's Tale). "
                            "Needs a rebuild.",
                       control=("switch",)),
    "vk_spec_fixes": dict(group="troubleshooting", level="advanced", label="Fix depth flicker (Unreal Vulkan)",
                          help="For Unreal games whose models flicker or show through walls and windows on the Frame "
                               "(for example Into The Radius 2). Needs a rebuild.",
                          control=("switch",)),
    "vk_query_slots": dict(group="troubleshooting", level="advanced", label="Stop objects popping in and out",
                           help="For Vulkan games whose models flicker or vanish while you look at them (for example "
                                "Into The Radius 2). Needs a rebuild.",
                           control=("switch",)),
    "sync_guard": dict(group="troubleshooting", level="advanced", label="Steady controller input after focus",
                       help="For games that crash right after you return to them (for example after the Steam menu).",
                       control=("switch",)),
    "profile_remap": dict(group="controllers", level="advanced", label="Treat newer Quest controllers as Touch",
                          help="For apps that only know Quest 3/Pro controllers: without it they get no buttons.",
                          control=("switch",)),
    "haptic_scale": dict(group="controllers", level="common", label="Vibration strength",
                         help="Turn it down if controller vibrations are too strong.",
                         control=("slider", 0.0, 1.0, 0.05, "{:.0%}")),
    "aim_pitch": dict(group="controllers", level="common", label="Pointer angle",
                      help="If the pointer doesn't hit what you aim at, tilt it up or down.",
                      control=("slider", -30.0, 30.0, 1.0, "{:+.0f}°")),
    "aim_yaw": dict(group="controllers", level="advanced", label="Pointer turn",
                    help="Turns the pointer left or right.", control=("slider", -30.0, 30.0, 1.0, "{:+.0f}°")),
    "aim_forward": dict(group="controllers", level="advanced", label="Pointer start",
                        help="Moves where the pointer starts, forward or back.",
                        control=("slider", -0.3, 0.3, 0.01, "{:+.2f} m")),
    "controller_models": dict(group="controllers", level="common", label="Show Steam Frame controllers",
                              help="For games that show the headset's own controllers. Takes effect after a "
                                   "reinstall.", control=("switch",), reinstall=True),
    "cylinder_strips": dict(group="screens", level="common", label="Show curved menus and screens",
                            help="The Frame can't show curved panels; this shows them as gently bent strips.",
                            control=("switch",)),
    "surface_emul": dict(group="screens", level="common", label="Show video panels",
                         help="Shows videos that games play on a flat panel (for example an intro video).",
                         control=("switch",)),
    "flip_emul": dict(group="screens", level="common", label="Fix upside-down menus",
                      help="Turns menus and text the right way up.", control=("switch",)),
    "stable_local": dict(group="screens", level="common", label="Keep menus in place",
                         help="For games whose menus or screens jump to wherever you look.", control=("switch",)),
    "focus_hold": dict(group="screens", level="common", label="Don't pause on short interruptions",
                       help="For games that pause or recenter when the headset briefly loses focus.",
                       control=("switch",)),
    "passthrough_emul": dict(group="room", level="common", label="See your room",
                             help="Shows the Frame's cameras where the game expects passthrough.",
                             control=("switch",)),
    "alpha_blend_emul": dict(group="room", level="advanced", label="Fix inside-out mixed reality",
                             depends="passthrough_emul",
                             help="For games that show your room on their objects and darkness where your room "
                                  "should be.", control=("switch",)),
    "scene_emul": dict(group="room", level="common", label="Use room layout",
                       help="Scene games get your saved room. Without one, they get a simple room from the play area.",
                       control=("switch",)),
    "scene_scan_request": dict(group="room", level="advanced", label="Scan when requested", depends="scene_emul",
                               help="Scan your room in the headset when the game asks for a room scan.",
                               control=("switch",)),
    "scene_hide_bounds": dict(group="room", level="advanced", label="Hide play-area size", depends="scene_emul",
                              help="For games that reject the room because of the play area.", control=("switch",)),
    "scene_height": dict(group="room", level="advanced", label="Room height", depends="scene_emul",
                         help="Ceiling height of the pretend room.", control=("slider", 2.0, 4.0, 0.1, "{:.1f} m")),
    "scene_width": dict(group="room", level="advanced", label="Room width", depends="scene_emul",
                        help="0 = the size of your play area.", control=("slider", 0.0, 10.0, 0.5, "{:.1f} m")),
    "scene_depth": dict(group="room", level="advanced", label="Room depth", depends="scene_emul",
                        help="0 = the size of your play area.", control=("slider", 0.0, 10.0, 0.5, "{:.1f} m")),
    "swapchain_pool": dict(group="troubleshooting", level="advanced",
                           label="Share panel images (swapchain limit)",
                           help="Keeps extra menus, panels and new eye images working when the Frame reaches its "
                                "image count limit.", control=("switch",)),
    "equirect_emul": dict(group="video360", level="common", label="Show 360° backgrounds and videos",
                          help="Video players' virtual theaters and 360° videos would otherwise stay black.",
                          control=("switch",)),
    "equirect_res": dict(group="video360", level="advanced", label="360° sharpness", depends="equirect_emul",
                         help="Higher is sharper but needs more power.",
                         control=("choice", [(1024, "Low"), (1536, "Normal"), (2048, "High"), (3072, "Very high")])),
    "equirect_face": dict(group="video360", level="advanced", label="360° detail", depends="equirect_emul",
                          help="Higher keeps more detail but uses more memory.",
                          control=("choice", [(1024, "Low"), (1536, "Normal"), (2048, "High"), (2730, "Maximum")])),
    "equirect_fps": dict(group="video360", level="advanced", label="360° video frame rate limit",
                         depends="equirect_emul", help="Must be at least the video's own frame rate.",
                         control=("choice", [(0.0, "No limit"), (24.0, "24"), (30.0, "30"), (60.0, "60"),
                                             (90.0, "90")])),
    "equirect_flip": dict(group="video360", level="advanced", label="360° picture orientation", depends="equirect_emul",
                          help="Only if 360° pictures show wrongly.",
                          control=("choice", [(0, "Normal"), (1, "Upside down"), (2, "Mirrored"),
                                              (3, "Upside down and mirrored"), (4, "Turned around")])),
    "equirect_stereo": dict(group="video360", level="advanced", label="360° 3D", depends="equirect_emul",
                            help="Shows 3D 360° pictures flat if they look doubled.",
                            control=("choice", [(0, "As the game sends it"), (2, "Flat")])),
    "foveation_fix": dict(group="troubleshooting", level="advanced", label="Hide Quest foveation",
                help="Hides a Quest-only way of saving GPU power that the Frame lacks. Leave it on.",
                control=("switch",)),
    "hide_fb_foveation": dict(group="troubleshooting", level="advanced", label="Turn off Quest Vulkan foveation",
                help="Diagnostic for Vulkan games that fail on their first frames with missing Quest foveation images.",
                control=("switch",)),
    "hide_space_warp": dict(group="troubleshooting", level="advanced", label="Turn off space warp",
                help="For games whose picture flickers or smears (for example Into The Radius 2).",
                control=("switch",)),
    "space_warp_emul": dict(group="troubleshooting", level="advanced", label="Provide space warp",
                help="For games that stop at start because they require Meta's space warp (for example OutRun "
                     "Quest VR). The game then renders every frame normally.", control=("switch",)),
    "swapchain_fix": dict(group="troubleshooting", level="advanced", label="Retry refused picture formats",
                help="Lets OpenGL ES games start when the Frame refuses their picture format. Leave it on.",
                control=("switch",)),
    "layer_fix": dict(group="troubleshooting", level="advanced", label="Drop broken layers",
                help="Skips picture layers the Frame would refuse. Leave it on.", control=("switch",)),
    "gl_hide_multiview": dict(group="troubleshooting", level="advanced", label="Draw each eye separately (OpenGL ES)",
                help="For older games whose picture stays black on the Frame (for example Path of the "
                     "Warrior).", control=("switch",)),
    "mutable_fix": dict(group="troubleshooting", level="advanced", label="Vulkan format workaround",
                help="Experimental. Leave it off unless asked to test it.", control=("switch",)),
    "flip_quads": dict(group="troubleshooting", level="advanced", label="Rotate panels 180°",
                help="Older way to turn upside-down panels; prefer Fix upside-down menus.", control=("switch",)),
    "swap_eyes": dict(group="troubleshooting", level="advanced", label="Swap eyes",
                help="Shows each eye's picture to the other eye. For testing.", control=("switch",)),
    "vk_pipeline_log": dict(group="troubleshooting", level="advanced", label="Vulkan pipeline failures (log)",
                help="Logs graphics pipelines the Frame's driver refuses. No effect on the game.", control=("switch",)),
    "vk_no_fossilize": dict(group="troubleshooting", level="advanced", label="Without Steam's shader cache layer",
                help="Some games can't create any graphics with it (for example Transformers Beyond Reality).",
                control=("switch",)),
    "vk_pipeline_fallback": dict(group="troubleshooting", level="advanced", label="Skip refused graphics",
                help="When the Frame's driver refuses one of the game's shaders, that object is left out instead of "
                     "the game ending.", control=("switch",)),
    "vk_validation": dict(group="troubleshooting", level="advanced", label="Vulkan error checks (log)",
                help="Logs Vulkan mistakes the game makes. Needs a special test build.", control=("switch",)),
    "fb_color_space": dict(group="troubleshooting", level="advanced", label="Colour space support",
                help="For games that stop at start because the headset reports no colour space "
                     "(for example OutRun Quest VR).", control=("switch",)),
    "rect_clamp": dict(group="troubleshooting", level="advanced", label="Keep pictures in bounds",
                help="Stops games from hanging when their eye picture is a little too big or empty (for example "
                     "PowerWash Simulator). Leave it on.", control=("switch",)),
    "action_dedup": dict(group="troubleshooting", level="advanced", label="Unique controller action names",
                help="Stops games from dropping back to the home environment at start because two of their "
                     "controller actions share a name (for example Scared By Squares). Leave it on.",
                control=("switch",)),
    "gl_hide_msrtt": dict(group="troubleshooting", level="advanced", label="Hide fast anti-aliasing (OpenGL ES)",
                help="For Unity games that crash when they turn on anti-aliasing (for example The Room "
                     "VR).", control=("switch",)),
    "gl_gamma_space": dict(group="troubleshooting", level="advanced", label="Gamma colour space (OpenGL ES)",
                help="For games whose picture is washed out, grey instead of black (for example PLAY'A VR): their "
                     "colours were converted to sRGB a second time.", control=("switch",)),
    "strip_color_bias": dict(group="troubleshooting", level="advanced", label="Drop layer color fades",
                help="Diagnostics: removes layer color fades. Some fades no longer show.", control=("switch",)),
    "snapshot": dict(group="troubleshooting", level="advanced", label="Eye-image snapshots (diagnostics)",
                help="Saves what the game draws every few seconds, for testing without the headset.",
                control=("switch",)),
    "gl_error_clear": dict(group="troubleshooting", level="advanced", label="Clear the game's GL errors (GLES)",
                help="For OpenGL ES games whose memory use keeps growing (e.g. Vader Immortal).", control=("switch",)),
    "flip_dump": dict(group="troubleshooting", level="advanced", label="Upside-down panel images (diagnostics)",
                help="Saves a few UI panels before and after FrameBridge turns them upright, for testing without the "
                     "headset.", control=("switch",)),
    "strip_depth": dict(group="troubleshooting", level="advanced", label="Strip depth layers",
                help="Removes depth information from the game's layers. For testing.", control=("switch",)),
    "respace_kick": dict(group="troubleshooting", level="advanced", label="Re-create play space",
                help="Re-creates the game's play space after the first frames. For testing.", control=("switch",)),
    "layer_debug": dict(group="troubleshooting", level="advanced", label="Extra diagnostics (log)",
                help="Logs details for troubleshooting. No effect on the game.", control=("switch",)),
    "eye_debug": dict(group="troubleshooting", level="advanced", label="Per-eye diagnostics (log)",
                help="Logs details when one eye looks wrong. No effect on the game.", control=("switch",)),
    "input_diag": dict(group="troubleshooting", level="advanced", label="Controller input diagnostics (log)",
                help="Logs details when buttons do nothing or the wrong thing. No effect on the game.",
                control=("switch",)),
    "release_wait": dict(group="troubleshooting", level="advanced", label="Wait for the game's GPU",
                help="For games where one eye flickers or jitters. Costs some speed.", control=("switch",)),
    "vk_hide_fdm": dict(group="troubleshooting", level="advanced", label="Hide Vulkan foveation",
                help="For Unreal Engine 5 games that crash on the Frame (for example Metro Awakening).",
                control=("switch",)),
    "fdm_bin_w": dict(group="troubleshooting", level="advanced", label="Foveation bin width",
                help="Only if the full-resolution area of Valve's foveation sits off-centre in this game. 0 = "
                     "automatic.", control=("slider", 0, 2016, 96, "{:.0f} px")),
    "fdm_bin_h": dict(group="troubleshooting", level="advanced", label="Foveation bin height",
                help="Set together with the width. 0 = automatic.", control=("slider", 0, 2048, 32, "{:.0f} px")),
    "ovrp_begin_gate": dict(group="troubleshooting", level="advanced",
                            label="Unity frame loop: wait for skipped frames",
                help="For older Unity games that freeze at the first scene change (for example Sniper "
                     "Elite VR). Can make loading screens stutter.", control=("switch",)),
    "ovrp_hold_physics": dict(group="troubleshooting", level="advanced", label="Steadier hands (older Unity)",
                help="For older Unity games whose hands trail the controllers (for example Sniper Elite "
                     "VR). In others the hands lag.", control=("switch",)),
    "cube_quads": dict(group="troubleshooting", level="advanced", label="Show cube loading screens",
                help="Shows 360° cube loading screens as six panels around you. Requires stand-in cube layers. "
                     "Skipped while a 3D scene is submitted.", control=("switch",)),
    "cube_standin": dict(group="troubleshooting", level="advanced", label="Stand in for cube layers",
                help="Keeps games running that use a 360° cube layer the Frame lacks (for example Budget Cuts "
                     "Ultimate). Leave it on.", control=("switch",)),
    "pose_debug": dict(group="troubleshooting", level="advanced", label="Pose timing diagnostics (log)",
                help="Logs how game poses line up with the Frame's clock. No effect on the game.", control=("switch",)),
    "zink_shader_dump": dict(group="troubleshooting", level="advanced", label="OpenGL ES shader dump (diagnostics)",
                help="Saves the game's shaders to capture one for a fix. No effect on the game.", control=("switch",)),
    "vk_shader_dump": dict(group="troubleshooting", level="advanced", label="Vulkan shader dump (diagnostics)",
                help="Saves the game's shaders to find one that hangs the GPU. No effect on the game.",
                control=("switch",)),
    "gl_mv_debug": dict(group="troubleshooting", level="advanced", label="Flat panel diagnostics (log)",
                help="Logs details when flat panels stay black. No effect on the game.", control=("switch",)),
}


# settings that need the xrshim (frame/adapter.py: OVRPlugin's own arm64 OpenXR lookups routed to the adapter), so
# they don't work for 32-bit games on the bridge. surface_native: Batman's native video renderer hooks Vulkan
# functions OVRPort's dispatcher doesn't know. scene_emul: the dispatcher refuses every scene/anchor function
# ("Unknown proc addr: xrQuerySpacesFB", Pinball FX VR 2026-10-16), so the emulated room only reaches the game through
# the shim (Demeter/NOPE only got it because haptic_fix happened to be on)
XRSHIM_SETTINGS = ("fixed_resolution", "hide_fb_foveation", "controller_models", "haptic_fix", "surface_native",
                   "scene_emul")
# shown with an "experimental" pill (Game settings, game page, CLI): not proven in the headset on many games yet
EXPERIMENTAL_SETTINGS = ("quest_buttons",)


class AdapterSetting(Patch):
    category = "adapter"
    stage = "install"

    def __init__(self, key, kind, default, title, description):
        self.id = f"adapter.{key}"
        self.key = key
        self.title = UI.get(key, {}).get("label") or title  # the plain label the Game settings dialog uses
        self.description = description
        self.params = [Param("value", kind, default, description)]
        self.default = default
        self.revision = REVISIONS.get(key, 1)
        self.a32 = key not in XRSHIM_SETTINGS
        self.experimental = key in EXPERIMENTAL_SETTINGS

    @property
    def summary(self) -> str:
        """The Game settings dialog's plain help line (UI), else patches/summaries.py."""
        return UI.get(self.key, {}).get("help") or super().summary

    def detect(self, a):
        from .applicability import uses_equirect_layers, uses_render_models, uses_scene

        if self.key == "controller_fix" and a.extra.get("hand_tracking_only"):
            return Suggestion(True, "Hand tracking is required by the game: pass hands through instead of reporting "
                                    "Touch controllers (for example Silhouette).", {"value": 0})
        if self.key == "controller_models" and uses_render_models(a):
            return Suggestion(True, "The game asks the headset for its controller models: show Steam Frame controllers "
                                    "instead of Quest Touch controllers.", {"value": 1})
        if self.key == "haptic_fix" and "libOVRPlugin.so" in a.libs:
            return Suggestion(True, "Meta's OVRPlugin vibrates controllers through OVRPort's loader, which turns some "
                                    "vibrations into gigabyte allocations that freeze the Frame (for example Lucky's "
                                    "Tale, BattleSisters): vibrations are converted before they reach it.",
                              {"value": 1})
        if (self.key == "pose_time_fix" and a.engine == "Unity" and "libOVRPlugin.so" in a.libs
                # Absence of the Oculus XR Plugin isn't proof of Unity's built-in VR: Unity OpenXR games can
                # bundle OVRPlugin too. The system-app check in libunity is positive evidence of built-in Oculus
                # (analysis/detect.py, frame/unity_oculus_check.py). Older analyses lack this signal; converted
                # inputs may have had the check rewritten to "android", so keep the legacy fallback for those.
                and (a.extra.get("unity_oculus_check", True) or a.is_overport_output)
                and not a.extra.get("oculus_xr_plugin") and "libOculusXRPlugin.so" not in a.libs):
            return Suggestion(True, "Unity's built-in Oculus support reads controller poses through OVRPlugin at its "
                                    "monotonic \"now\", seconds in the past on the Frame: they are located at the "
                                    "right time (for example BattleSisters, Sniper Elite VR).", {"value": 1})
        if self.key == "pose_time_fix" and a.engine == "Unreal" and (a.extra or {}).get("unreal_thumb_touch"):
            return Suggestion(True, "Unreal 4's Oculus input of this OVRPlugin generation asks for controller poses "
                                    "at OVRPlugin's own clock, which is behind the Frame's: they are located at the "
                                    "right time (for example Vader Immortal).", {"value": 1})
        if self.key == "equirect_emul" and uses_equirect_layers(a):
            return Suggestion(True, "The game draws 360° layers (for example a video player's theater or 360° "
                                    "videos), which the Frame's runtime can't show: show them as panels around you.",
                              {"value": 1})
        if self.key == "hide_space_warp" and "libUnreal.so" in a.libs and "libOVRPlugin.so" in a.libs:
            # Unreal Engine 5 + Meta's plugin: Application SpaceWarp is on by default (Into The Radius 2 flickered;
            # OVRPort's patch_disable_space_warp doesn't reach UE5). Off until confirmed in more games.
            return Suggestion(False, "Unreal Engine 5 game with Meta's space warp: turn on if the picture flickers or "
                                     "smears (for example Into The Radius 2).", {"value": 1})
        if self.key == "gl_error_clear" and (a.extra or {}).get("tbxr_libs"):
            # QuestZDoom (#237) leaves GL_INVALID_ENUM pending every frame; the runtime's GL check failed on it at
            # the first swapchain and its GL interop then leaked GPU objects (~100 a second) until the Frame slowed
            # to a crawl and, minutes later, the GPU driver crashed. Harmless for ports that leave no error.
            return Suggestion(True, "Team Beef port: these leave OpenGL errors pending, which made the Frame's "
                                    "runtime leak GPU memory every frame until the game slowed to a crawl (for "
                                    "example QuestZDoom): they are cleared first.", {"value": 1})
        if self.key == "scene_emul" and uses_scene(a):
            return Suggestion(True, "The game uses Meta's scene or spatial anchor APIs: serve the saved room on "
                                    "the Frame, or a simple play-area room until one is scanned.", {"value": 1})
        return None

    def applies(self, a):
        from . import applicability as ap

        if a.only_32bit and not self.a32:
            return False  # a 32-bit game on the bridge: no xrshim (also hides it in the Game settings dialog)
        rules = {
            "scene_emul": lambda a: ap.uses_scene(a) or a.extra.get("mr_only"),
            "scene_height": lambda a: ap.uses_scene(a) or a.extra.get("mr_only"),
            "scene_scan_request": lambda a: ap.uses_scene(a) or a.extra.get("mr_only"),
            "scene_hide_bounds": lambda a: ap.uses_scene(a) or a.extra.get("mr_only"),
            "scene_width": lambda a: ap.uses_scene(a) or a.extra.get("mr_only"),
            "scene_depth": lambda a: ap.uses_scene(a) or a.extra.get("mr_only"),
            "passthrough_emul": lambda a: (a.extra.get("mr_only")
                                           or "com.oculus.feature.PASSTHROUGH" in (a.extra.get("features") or {})),
            "alpha_blend_emul": lambda a: (a.extra.get("mr_only")
                                           or "com.oculus.feature.PASSTHROUGH" in (a.extra.get("features") or {})),
            "fixed_resolution": lambda a: "libOVRPlugin.so" in a.libs or a.xr == "OpenXR",
            "hide_fb_foveation": ap.is_vulkan,
            "flip_emul": ap.is_vulkan,
            "flip_quads": ap.is_vulkan,
            "mutable_fix": ap.is_vulkan,
            "swapchain_fix": ap.is_gles,
            "gl_hide_multiview": lambda a: a.direct_vrapi and ap.is_gles(a),
            "gl_hide_msrtt": ap.is_gles,
            "gl_gamma_space": ap.is_gles,
            "gl_int_attribs": ap.is_gles,
            "gl_highp_markers": ap.is_gles,
            "gl_mv_debug": lambda a: bool((a.extra or {}).get("gl_multiview_libs")) and ap.is_gles(a),
            "ovrp_begin_gate": lambda a: a.engine == "Unity" and "libOVRPlugin.so" in a.libs,
            "ovrp_hold_physics": lambda a: a.engine == "Unity" and "libOVRPlugin.so" in a.libs,
            "vk_shader_fix": lambda a: a.engine == "Unreal",  # read by the Vulkan shim, which only Unreal games get
            "vk_hide_fdm": lambda a: a.engine == "Unreal",  # Unreal's Vulkan isn't always detected (Metro Awakening)
            # read by the foveation centring layer, which every arm64 FrameBridge build carries
            "fdm_bin_w": lambda a: "arm64-v8a" in a.abis,
            "fdm_bin_h": lambda a: "arm64-v8a" in a.abis,
            "vk_query_slots": lambda a: a.engine == "Unreal" and ap.is_vulkan(a),
            "vk_validation": lambda a: a.engine == "Unreal",
            "vk_pipeline_log": lambda a: a.engine in ("Unreal", "Other") and "arm64-v8a" in a.abis,
            "fb_color_space": lambda a: a.xr == "OpenXR",
            "space_warp_emul": lambda a: a.xr == "OpenXR",
            "vk_no_fossilize": lambda a: a.engine in ("Unreal", "Other") and "arm64-v8a" in a.abis,
            "vk_pipeline_fallback": lambda a: a.engine in ("Unreal", "Other") and "arm64-v8a" in a.abis,
            # wherever the Vulkan shim can be (vk_sanitize); Unreal's Vulkan builds are rarely detected as such
            "vk_shader_dump": lambda a: a.engine in ("Unreal", "Other") and "arm64-v8a" in a.abis,
            "zink_shader_fix": ap.may_be_gles,
            "zink_shader_dump": ap.may_be_gles,
            "haptic_fix": lambda a: "libOVRPlugin.so" in a.libs,
            "surface_native": lambda a: "arm64-v8a" in a.abis,  # the xrshim it needs is arm64 only (also above)
            "pose_time_fix": lambda a: "libOVRPlugin.so" in a.libs,
            "proximity_emul": lambda a: "libOVRPlugin.so" in a.libs,
            "vk_spec_fixes": lambda a: a.engine == "Unreal" and ap.is_vulkan(a),
            "controller_models": ap.may_use_render_models,
            "swapchain_pool": lambda a: ap.is_gles(a) or ap.is_vulkan(a),
            **{k: ap.is_gles for k in ("equirect_emul", "equirect_face", "equirect_res",
                                       "equirect_flip", "equirect_fps", "equirect_stereo")},
        }
        rule = rules.get(self.key)
        return bool(rule(a)) if rule else True

    def install(self, ctx: InstallContext) -> None:
        pass  # collected by adapter_settings()


def adapter_settings(recipe_patches: dict, *, defaults: dict | None = None) -> dict:
    """Ordered settings for libframe_settings.so / settings.conf: base keys first, then enabled overrides in
    selection order. Callers supply preference defaults; this helper never reads the library."""
    out = dict(BASE_SETTINGS)
    out.update(defaults or {})
    for pid, params in recipe_patches.items():
        if pid.startswith("adapter."):
            key = pid.split(".", 1)[1]
            spec = next((s for s in SETTINGS if s[0] == key), None)
            value = (params or {}).get("value", spec[2] if spec else 1)
            if spec and spec[1] == "float":
                value = float(value)
            elif spec and spec[1] == "int":
                value = int(value)
            out[key] = value
    if (recipe_patches.get("device.foveation") or {}).get("mode") in ("tracked", "fixed"):
        out["foveation_fix"] = 1  # shared injection must not compete with the game's native foveation
        # ...and must render into ordinary images: Valve's FDM leaves a SUBSAMPLED swapchain image's coarse areas
        # unscaled, which the compositor shows as garbage around the sharp area (GitHub #303, Medieval Dynasty)
        out["foveation_storage_fix"] = 1
    return out


class DeviceFiles(Patch):
    id = "device.files"
    title = "Settings files for the game"
    description = ("Writes config files into the game's Android/data/<package>/files/ on the Frame, to turn off engine "
                   "features the Frame doesn't support (for example a CryEngine user.cfg with r_variable_rate_shading "
                   "= 0, as The Climb 2 needs).")
    category = "device"
    needs_vr = False
    stage = "install"
    params = [Param("files", "text", {}, "path relative to files/ -> content")]

    def detect(self, a):
        if (a.engine == "CryEngine" and a.graphics.startswith("Vulkan")
                or (a.engine == "CryEngine" and "libCryRenderVulkan.so" in a.libs)):
            cvar = "r_vr_foveated" if (a.extra or {}).get("cryengine_vr_foveated") else "r_variable_rate_shading"
            return Suggestion(True, "CryEngine: variable-rate shading isn't supported on the Frame, so user.cfg turns "
                                    f"it off ({cvar} = 0; The Climb / The Climb 2).",
                              {"files": {"user.cfg": f"{cvar} = 0\n"}})
        return None

    def install(self, ctx: InstallContext) -> None:
        for rel, content in (ctx.params.get("files") or {}).items():
            ctx.files[rel] = content.encode() if isinstance(content, str) else content


class ConfigSync(Patch):
    id = "device.config_sync"
    title = "Keep the game's saved resolution in step"
    description = ("Some games save their render resolution in a settings file and load it after creating the "
                   "headset's eye buffers; after a resolution scale change the saved size no longer matches and the "
                   "picture is displaced or stretched, with black borders (for example BlazeRush's user_config.xml). "
                   "FramePort writes the values its recipe names into the game's file while the game isn't running "
                   "(after an install and after a Game settings change), from the Frame's own eye-buffer size times "
                   "the resolution scale: w and h = the eye buffer's width and height, for example h*16/9. Only "
                   "existing entries change; the previous file is kept as <file>.frameport-backup. The size is "
                   "learned from the game's last start, so a file the game wrote before is corrected from the next "
                   "install or settings change on.")
    category = "device"
    needs_vr = True
    stage = "install"
    params = [Param("files", "text", {}, "path relative to files/ -> {setting name: w | h | expression}")]

    def install(self, ctx: InstallContext) -> None:
        rules = {}
        for rel, values in (ctx.params.get("files") or {}).items():
            if isinstance(rel, str) and isinstance(values, dict) and values:
                rules[rel] = {str(k): str(v) for k, v in values.items()}
        ctx.config_sync.update(rules)


class LeptonEnv(Patch):
    id = "device.lepton_env"
    title = "Extra Lepton settings"
    description = ("Environment variables exported by the launcher (for example VK_INSTANCE_LAYERS=\"\" to test "
                   "without Valve's layers).")
    category = "device"
    needs_vr = False
    stage = "install"
    params = [Param("env", "text", {}, "NAME -> value")]

    def install(self, ctx: InstallContext) -> None:
        ctx.env.update({k: str(v) for k, v in (ctx.params.get("env") or {}).items()})


class HideNavBar(Patch):
    id = "device.hide_navbar"
    title = "Hide Android's navigation bar"
    description = ("Removes Android's back/home/recents buttons from a 2D app's window, where they cover the app's own "
                   "controls. Turn it off for an app that needs the on-screen back button.")
    category = "device"
    needs_vr = False
    stage = "install"
    # qemu.hw.mainkeys=1 ("this device has hardware keys") is only read at boot and Lepton has no setting for extra
    # Android properties; it copies LEPTON_GFXRECON_* values into the boot properties unescaped, so a second line
    # rides along (see docs/FRAME_RUNTIME.md). Harmless if a Lepton update changes that: the bar comes back.
    ENV = {"LEPTON_GFXRECON_FP_PROPS": "0\nqemu.hw.mainkeys=1"}

    def applies(self, a):
        return a.vr_kind == "none"

    def detect(self, a):
        if a.vr_kind == "none":
            return Suggestion(True, "2D app: Android's navigation buttons would cover the app's own controls.")
        return None

    def install(self, ctx: InstallContext) -> None:
        ctx.env.update(self.ENV)


LANGUAGE_TAG = re.compile(r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-[A-Z]{2}|-[0-9]{3})?$")


class Language(Patch):
    id = "device.language"
    title = "Game language"
    description = ("The language Android reports to the game. Lepton's Android is set to English (US), and games that "
                   "follow the system language (most Unity and Unreal games) start in English. A language the game "
                   "doesn't have still shows English. Applies with Update on Frame (the APK isn't changed).")
    category = "device"
    needs_vr = False
    stage = "install"
    params = [Param("tag", "str", "", "Android language tag, e.g. de-DE (empty = English, US)")]
    CHOICES = (("", "English (US)"), ("en-GB", "English (UK)"), ("de-DE", "Deutsch"), ("fr-FR", "Français"),
               ("es-ES", "Español"), ("es-MX", "Español (Latinoamérica)"), ("it-IT", "Italiano"),
               ("pt-BR", "Português (Brasil)"), ("pt-PT", "Português (Portugal)"), ("nl-NL", "Nederlands"),
               ("pl-PL", "Polski"), ("cs-CZ", "Čeština"), ("sv-SE", "Svenska"), ("da-DK", "Dansk"),
               ("nb-NO", "Norsk"), ("fi-FI", "Suomi"), ("tr-TR", "Türkçe"), ("ru-RU", "Русский"),
               ("uk-UA", "Українська"), ("ja-JP", "日本語"), ("ko-KR", "한국어"), ("zh-CN", "中文（简体）"),
               ("zh-TW", "中文（繁體）"))
    # persist.sys.locale is Android's system language, read when the container boots. Lepton has no setting for it;
    # like device.hide_navbar it rides as a second line of a LEPTON_GFXRECON_* value (copied into the boot
    # properties unescaped, see docs/FRAME_RUNTIME.md), in its own variable so both can be on.
    ENV_KEY = "LEPTON_GFXRECON_FP_LOCALE"

    def applies(self, a):
        return True

    def install(self, ctx: InstallContext) -> None:
        tag = str(ctx.params.get("tag") or "").strip()
        if not tag:
            return
        if not LANGUAGE_TAG.match(tag):
            raise ValueError(f"Not a language tag: {tag!r} (for example de-DE)")
        ctx.env[self.ENV_KEY] = f"0\npersist.sys.locale={tag}"


SHARE_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
SHARE_FOLDER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


class SharedFolder(Patch):
    id = "device.shared_folder"
    title = "Share a folder with another game"
    description = ("On a Quest every app sees the same /sdcard; on the Frame each game has its own. This makes a "
                   "folder of this game's /sdcard the same folder as in another game's, e.g. a companion app that "
                   "manages its game's mods and start options (JKXR's companion app and /sdcard/JKXR, GitHub #440). "
                   "The folder stays in the other game's storage; this game's own copy of it, if it had one, is kept "
                   "beside it. Both games must be installed on the Frame. Applied with Update on Frame.")
    category = "device"
    needs_vr = False
    stage = "install"
    params = [Param("game", "str", "", "The other game's package, e.g. com.drbeef.jkxr"),
              Param("folder", "str", "", "The folder in /sdcard to share, e.g. JKXR")]
    ENV_KEY = "FRAMEPORT_SHARE"

    def applies(self, a):
        return True

    def install(self, ctx: InstallContext) -> None:
        game, folder = str(ctx.params.get("game") or "").strip(), str(ctx.params.get("folder") or "").strip()
        if not game and not folder:
            return
        if not SHARE_PACKAGE.match(game):
            raise ValueError(f"Not a package name: {game!r} (for example com.drbeef.jkxr)")
        if not SHARE_FOLDER.match(folder) or folder in (".", ".."):
            raise ValueError(f"Not a folder name in /sdcard: {folder!r} (for example JKXR)")
        ctx.env[self.ENV_KEY] = f"{game}:{folder}"


class SteamGamepad(Patch):
    id = "device.steam_gamepad"
    title = "Controllers as a gamepad (Steam Input)"
    description = ("Lepton's Android only gets a keyboard, a pointer and touch from the Frame, so a 2D game never sees "
                   "a controller (the controllers only move the pointer; SDL games show their touch controls). This "
                   "passes Steam Input's virtual gamepad (\"Microsoft X-Box 360 pad\", made by Steam for the Frame's "
                   "controllers and paired Bluetooth pads) into this app's container with an Xbox 360 key layout, and "
                   "tells SDL games to use it (SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD). Steam Input must be "
                   "on for the game's shortcut; the controllers must be on when the game starts (a pad Steam makes "
                   "later isn't seen until the next start). The APK isn't changed. Off for one start: "
                   "FRAMEPORT_NO_GAMEPAD=1 in the game's launch options. Steam Input's own per-game remapping may "
                   "not reach the app (GitHub #162); the options below change the buttons inside the app instead "
                   "(applied with Update on Frame).")
    category = "device"
    needs_vr = False
    stage = "install"  # the agent reads it from the recipe at finalize (deployment.json steam_gamepad)
    params = [
        Param("swap_sticks", "bool", False, "Swap the sticks: the right stick moves through menus like the left one "
                                            "(for using one controller)"),
        Param("swap_ab", "bool", False, "Swap A and B"),
        Param("keys", "str", "", "Own key layout lines (Android .kl), separated by ';', e.g. 'key 304 DPAD_DOWN' "
                                 "(A presses D-pad down) or 'key 305 BACK'"),
    ]
    KL_LINE = re.compile(r"(key [0-9]{1,4} [A-Z][A-Z0-9_]{0,40}( (FUNCTION|WAKE))?"
                         r"|axis 0x[0-9a-f]{1,2} (invert )?[A-Z][A-Z0-9_]{0,40}( flat [0-9]{1,5})?)")

    def applies(self, a):
        return a.vr_kind == "none"

    def detect(self, a):
        if a.vr_kind == "none" and (a.extra or {}).get("gamepad"):
            return Suggestion(True, "2D app that declares gamepad support (android.hardware.gamepad or Android TV's "
                                    "launcher): pass the controllers to it as a gamepad.")
        return None

    def install(self, ctx: InstallContext) -> None:
        # the launcher line (agent: GAMEPAD_LINE) exports SDL's hint and puts the Podman wrapper on PATH; the wrapper
        # builds the app's key layout from these (agent: gamepad_kl)
        layout = ",".join(k for k in ("swap_sticks", "swap_ab") if ctx.params.get(k))
        lines = [" ".join(ln.split()) for ln in str(ctx.params.get("keys") or "").split(";") if ln.strip()]
        bad = [ln for ln in lines if not self.KL_LINE.fullmatch(ln)]
        if bad:
            raise ValueError(f"Not a key layout line: {bad[0]!r} (for example 'key 304 DPAD_DOWN')")
        if layout:
            ctx.env["FRAMEPORT_GAMEPAD_LAYOUT"] = layout
        if lines:
            ctx.env["FRAMEPORT_GAMEPAD_KEYS"] = "; ".join(lines)


class HybridApp(Patch):
    id = "device.hybrid_app"
    title = "Hybrid app: allow the switch into VR"
    description = ("Horizon OS \"hybrid apps\" start as a 2D panel and switch into VR for some content (e.g. YouTube's "
                   "360° videos and full screen). Before the switch the app checks that the system has the feature "
                   "oculus.software.vr.app.hybrid; the Frame's Android doesn't, so the app stays in its panel "
                   "(\"Missing hybrid app system feature\" in its log). This declares the feature in the app's "
                   "container (a permissions file mounted by FramePort's Podman wrapper; the APK isn't changed). "
                   "Whether the app's VR part then works on the Frame depends on the app (GitHub #484). Applied with "
                   "Update on Frame.")
    category = "device"
    needs_vr = False
    stage = "install"  # the launcher exports FRAMEPORT_FEATURES; the agent's Podman wrapper mounts the file
    FEATURE = "oculus.software.vr.app.hybrid"
    ENV_KEY = "FRAMEPORT_FEATURES"

    def applies(self, a):
        return self.FEATURE in ((a.extra or {}).get("features") or {})

    def install(self, ctx: InstallContext) -> None:
        ctx.env[self.ENV_KEY] = self.FEATURE


def foveation_options(params):
    mode = params.get("mode") or ""
    if mode not in ("", "tracked", "fixed", "off"):
        raise ValueError("Unknown foveation mode")
    level = params.get("level", "")
    if "level" not in params and params.get("radius"):
        # Old radius exports never reached the layer. Preserve intent with a supported
        # preset; unknown legacy values restore Default. No numeric FOVE_LEVEL values.
        try:
            radius = float(params["radius"])
        except (ValueError, TypeError):
            radius = 0
        if math.isfinite(radius) and 0.05 <= radius <= 0.45:
            level = min(((.30, "lo"), (.25, "med"), (.20, "hi")),
                        key=lambda pair: abs(pair[0] - radius))[1]
    if level not in ("", "lo", "med", "hi"):
        raise ValueError("Unknown foveation profile")
    test = params.get("test", False)
    if not isinstance(test, bool):
        raise ValueError("Foveation test mode must be a boolean")
    if (test or level) and mode not in ("tracked", "fixed"):
        raise ValueError("Choose Eye-tracked or Fixed before configuring foveation size or test mode")
    return mode, level, test


class Foveation(Patch):
    id = "device.foveation"
    title = "Foveated rendering (Valve)"
    description = ("Reduces shading away from the full-resolution area, following each eye's gaze. "
                   "Default keeps the runtime profile unchanged. Low has the largest full-resolution center; "
                   "medium and high use smaller centers and stronger peripheral detail reduction. Larger areas "
                   "retain more detail but save less GPU work. Fixed stops the area following your eyes. "
                   "The optional test darkens actual coarse-shaded fragments. Runtime choices are in Game settings. "
                   "Tested on RE4, Deadpool and Asgard's Wrath 2. "
                   "The Climb 2 gets a version-guarded private bridge at 1.0 scale; Batman gets its "
                   "hash-verified allocation fix (unsupported OVRPlugin versions are refused). Other games may "
                   "need additional fixes; turn this off if artifacts appear.")
    category = "device"
    # This feature has both APK support (allocation fix / optional diagnostic) and launcher settings.
    stage = "apk"
    revision = 3
    order = 95  # after renderer/bridge replacements
    experimental = True
    params = [Param("support", "bool", False, "Internal APK compatibility support"),
              Param("mode", "str", "", "Catalog default: tracked, fixed or off (empty = Valve's default)"),
              Param("level", "str", "", "Runtime profile: lo, med, hi (empty = Default)"),
              Param("test", "bool", False, "Darken lower-resolution regions (diagnostic)")]
    CHOICES = (("", "Default"), ("tracked", "Eye-tracked"),
               ("fixed", "Fixed (doesn't follow the eyes)"), ("off", "Off"))
    # Valve's layer starts at lo when FOVE_LEVEL is unset, so Default is the large area
    LEVEL_CHOICES = (("", "Default (large)"), ("lo", "Large"), ("med", "Medium (saves more)"),
                     ("hi", "Small (saves most)"))

    def apply(self, ctx):
        from .frame.foveation import apply
        return apply(ctx)

    def validate(self, ctx):
        from .frame.foveation import validate
        return validate(ctx)

    def install(self, ctx: InstallContext) -> None:
        # runs after device.lepton_env (install_context), so this choice wins over a recipe's VK_INSTANCE_LAYERS
        mode, level, test = foveation_options(ctx.params)
        ctx.env.pop("FOVE_INNER", None)
        ctx.env.pop("FOVE_OUTER", None)
        if not mode:
            return
        for key in ("FDM_DEBUG", "ENABLE_VULKAN_FDM_INJECTION_LAYER",
                    "LEPTON_ENV_DISABLE_VULKAN_FDM_INJECTION_LAYER", "FOVE_INNER", "FOVE_OUTER", "FOVE_LEVEL",
                    "LEPTON_ENV_FRAMEPORT_FOVEATION_TEST", "LEPTON_ENV_FRAMEPORT_CLIMB_STEREO_OFFSETS",
                    "LEPTON_ENV_FRAMEPORT_VRAPI_FDM_ISOLATE_MONO",
                    "LEPTON_ENV_FRAMEPORT_VRAPI_KEEP_FOCUSED_IMAGE"):
            ctx.env.pop(key, None)
        if mode == "off":
            ctx.env.update(VK_INSTANCE_LAYERS="", LEPTON_ENV_DISABLE_VULKAN_FDM_INJECTION_LAYER="1")
            return
        if ctx.env.get("VK_INSTANCE_LAYERS") == "":
            ctx.env.pop("VK_INSTANCE_LAYERS")
        ctx.env.update(ENABLE_VULKAN_FDM_INJECTION_LAYER="1",
                       FDM_DEBUG="enable,disable_offsets" if mode == "fixed" else "enable")
        if ctx.package == "com.crytek.climb2":
            if float(ctx.adapter_settings.get("scale", 1)) != 1:
                raise ValueError("The Climb 2 foveation is verified only at 1.0 resolution scale")
            ctx.env["FDM_DEBUG"] += ",yflip"
            ctx.env.update(LEPTON_ENV_FRAMEPORT_CLIMB_STEREO_OFFSETS="1",
                           LEPTON_ENV_FRAMEPORT_VRAPI_FDM_ISOLATE_MONO="1",
                           LEPTON_ENV_FRAMEPORT_VRAPI_KEEP_FOCUSED_IMAGE="1")
        if level:
            ctx.env["FOVE_LEVEL"] = level
        if test:
            ctx.env["LEPTON_ENV_FRAMEPORT_FOVEATION_TEST"] = "1"


class DisplayMode(Patch):
    id = "device.display_mode"
    title = "Show as VR or as a flat window"
    description = ("How Lepton runs the app: VR (headless Android, only what the app draws through OpenXR reaches the "
                   "headset) or a flat window (Lepton's lepton-show-flatscreen marker: the Android window on a screen "
                   "in the headset, like a phone or tablet app). Automatic uses a flat window for apps without VR "
                   "code. "
                   "Choose \"Flat window\" for a 2D app FramePort took for VR (it shows nothing in the headset), "
                   "\"VR\" for a VR app FramePort took for 2D.")
    category = "device"
    needs_vr = False
    stage = "install"
    params = [Param("mode", "str", "", "vr or flat (empty = automatic)")]
    CHOICES = (("", "Automatic"), ("vr", "VR"), ("flat", "Flat window"))

    def install(self, ctx: InstallContext) -> None:
        ctx.display = ctx.params.get("mode") if ctx.params.get("mode") in ("vr", "flat") else ""


class TextInputWindow(Patch):
    id = "device.text_input_window"
    title = "Show the app's Android window (for typing)"
    description = ("Lepton runs VR apps headless: their Android window is never shown, so it never gets keyboard "
                   "focus and no key press (Steam's on-screen keyboard, a USB/Bluetooth keyboard, Type on Frame) "
                   "reaches the app's text fields. With Lepton's lepton-show-flatscreen marker the window is shown "
                   "(behind the VR view, which keeps working) and Steam's keyboard opens for a selected text field. "
                   "Steam lists the shown window as \"Gamescope\" (the Frame's compositor). While the game runs, "
                   "FramePort turns off SteamVR's automatic theater mode for game windows: it put the Android window "
                   "in front of the player with the controllers as a mouse pointer, and the game got no controller "
                   "input. The window stays a page of the game in the dashboard. Pairs with \"Make Unity text fields "
                   "work without a system keyboard\". Apps that open a flat launcher before their VR part (for "
                   "example Xelee or WiiCompiled, whose launchers pick the game) need the window too: headless, the "
                   "launcher is never seen and the VR part never starts. For those, theater mode stays on, so the "
                   "launcher is in front of you; once the VR part runs, close the dashboard with the controller's "
                   "button if it opened on the window.")
    category = "device"
    stage = "install"

    def applies(self, a):
        extra = a.extra or {}
        return a.vr_kind != "none" and bool(extra.get("text_fields") or extra.get("vr_activity"))

    def detect(self, a):
        if not self.applies(a):
            return None
        vr = (a.extra or {}).get("vr_activity")
        if vr:  # GitHub #475 (Xelee): headless, its launcher was never seen and never started the VR part
            return Suggestion(True, "The app opens a flat launcher that starts its VR part "
                                    f"({vr.rsplit('.', 1)[-1]}): Lepton runs VR apps headless, so the launcher "
                                    "is only seen in a shown Android window.")
        return Suggestion(True, "App with text fields: key presses only reach a shown Android window.")

    def install(self, ctx: InstallContext) -> None:
        ctx.flatscreen = True


for _spec in SETTINGS:
    register(AdapterSetting(*_spec))
register(DeviceFiles)
register(ConfigSync)
register(LeptonEnv)
register(HideNavBar)
register(Language)
register(SteamGamepad)
register(HybridApp)
register(SharedFolder)
register(TextInputWindow)
register(DisplayMode)
register(Foveation)


FOVEATION_ENV_KEYS = ("FDM_DEBUG", "ENABLE_VULKAN_FDM_INJECTION_LAYER",
    "LEPTON_ENV_DISABLE_VULKAN_FDM_INJECTION_LAYER", "VK_INSTANCE_LAYERS", "FOVE_INNER", "FOVE_OUTER", "FOVE_LEVEL",
    "LEPTON_ENV_FRAMEPORT_FOVEATION_TEST", "LEPTON_ENV_FRAMEPORT_CLIMB_STEREO_OFFSETS",
    "LEPTON_ENV_FRAMEPORT_VRAPI_FDM_ISOLATE_MONO", "LEPTON_ENV_FRAMEPORT_VRAPI_KEEP_FOCUSED_IMAGE")


def foveation_preference(entry):
    params = entry.get("foveation") if "foveation" in entry else (
        ((entry.get("recipe") or {}).get("patches") or {}).get("device.foveation") or {})
    pref = {k: v for k, v in params.items() if k in ("mode", "level", "radius", "test")}
    if "radius" in pref:
        _, level, _ = foveation_options(pref)
        pref.pop("radius")
        pref["level"] = level
    return pref


def foveation_env(package, params, settings=None, baseline=None):
    """Complete launcher override; None removes an obsolete export, including on Default."""
    from .base import InstallContext
    ctx = InstallContext(package, params, {}, dict(baseline or {}), dict(settings or {}))
    Foveation().install(ctx)
    return {key: ctx.env.get(key) for key in FOVEATION_ENV_KEYS}
