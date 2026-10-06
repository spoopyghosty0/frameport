"""FrameBridge adapter settings and device-side files, exposed as patches so the UI can toggle them.

Adapter settings end up in lib/<abi>/libframe_settings.so (build time) and in <install>/settings.conf +
Android/data/<pkg>/files/framebridge.conf on the Frame (can be changed later without re-patching).
The GL shim reads framebridge.conf too (gl_hide_multiview).
"""
from __future__ import annotations

from .base import InstallContext, Param, Patch, Suggestion, register

# Always written (in this order) so every build carries explicit values.
BASE_SETTINGS = {"scale": 1.0, "foveation_fix": 1, "controller_fix": 1}

# key, kind, default, title, description
SETTINGS = [
    ("scale", "float", 1.0, "Resolution scale",
     "Multiplies the recommended eye-buffer width and height (0.5–2.0). 1.5 ≈ 2.25× pixels."),
    ("foveation_fix", "int", 1, "Hide Quest foveation", "Hides Quest foveation extensions the Frame runtime lacks."),
    ("snapshot", "int", 0, "Eye-image snapshots (diagnostics)",
     "Every N seconds, saves the left-eye image the game submits (a quarter of its size) as fb_snap_0-7.ppm in the "
     "game's files folder on the Frame. Launch tests run without anyone wearing the headset, so the headset view stays "
     "black; this shows what the game itself draws. OpenGL ES games only."),
    ("strip_color_bias", "int", 0, "Drop layer color fades",
     "Removes the color scale/bias (XR_KHR_composition_layer_color_scale_bias) from the game's layers, so the runtime "
     "doesn't apply it. The Frame's runtime allocates GPU memory for it every frame without freeing it, e.g. Vader "
     "Immortal leaked ~20 MB/s on its loading screen. Fades done this way no longer show."),
    ("hide_space_warp", "int", 0, "Turn off space warp",
     "Hides XR_FB_space_warp, so the game renders every frame itself instead of half of them plus motion vectors "
     "(Application SpaceWarp). For games whose picture flickers or smears on the Frame (e.g. Unreal Engine 5 games "
     "such as Into The Radius 2)."),
    ("controller_fix", "int", 1, "Report Touch controllers",
     "Reports Frame controllers as Oculus Touch and hides synthetic hand tracking. Set 0 for games that require hand "
     "tracking (e.g. Silhouette)."),
    ("swapchain_fix", "int", 1, "Swapchain format fallback",
     "Retry rejected GLES formats/MSAA with sRGB, samples=1."),
    ("layer_fix", "int", 1, "Drop invalid layers",
     "Drop layers whose swapchain failed or whose extension isn't enabled."),
    ("passthrough_emul", "int", 1, "Emulate passthrough",
     "XR_FB_passthrough via ALPHA_BLEND (Frame greyscale cameras)."),
    ("flip_emul", "int", 1, "Emulate flipped quads",
     "Blit quads flagged XrCompositionLayerImageLayoutFB VERTICAL_FLIP upside down (Vulkan). Fixes UI panels and "
     "text that show upside down (e.g. Assassin's Creed Nexus)."),
    ("cylinder_strips", "int", 1, "Show curved panels",
     "Cylinder layers (curved menus and movie screens, e.g. 4XVR), which the Frame's runtime lacks, are shown as a "
     "few flat strips along the curve. 0 = drop them. 360° (equirect) layers can't be shown on the Frame."),
    ("surface_emul", "int", 1, "Show video panels",
     "Video panels that an Android media player draws into (XR_KHR_android_surface_swapchain, e.g. I Am Monkey's "
     "intro video), which the Frame's runtime refuses, get an Android Surface from FrameBridge: each video frame is "
     "copied into an ordinary swapchain the panel shows. Without it these panels are missing and a game waiting for "
     "its video stays black. 0 = leave them to the runtime."),
    ("equirect_emul", "int", 0, "Show 360° layers",
     "360° (equirect) layers, e.g. a video player's virtual theatre or 360° videos (e.g. 4XVR), which the Frame's "
     "runtime lacks, are drawn by a background thread into a layer behind the game's own picture: the 360° image is "
     "converted only when it changes (a theatre once, a 360° video once per video frame) and the view is drawn for "
     "each frame's head pose. GLES games only; without it these layers are missing (black)."),
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
     "default: the Frame's wear sensor flickers \"HMD off\" for 0.5-2 s while worn, and many games pause or recentre "
     "every time (e.g. Blade & Sorcery, Lucky's Tale)."),
    ("focus_hold_ms", "float", 5000.0, "Longest focus dip to ignore (ms)",
     "focus_hold hides focus dips up to this long (100-5000). Longer for a headset whose wear sensor flickers "
     "(\"HMD off\" for 0.5-2 s while worn, e.g. Blade & Sorcery pausing); taking the headset off or the system menu "
     "pauses the game only after this long."),
    ("sync_guard", "int", 0, "Guard controller input after focus",
     "Runs xrSyncActions one at a time with xrPollEvent and skips it for 250 ms after focus returns. For games that "
     "crash in the runtime's input code right after focus comes back (SIGSEGV in vrclient.so UpdateActionStateInternal "
     "/ xrSyncActions, e.g. Myst)."),
    ("profile_remap", "int", 1, "Newer Touch profiles as Touch",
     "Apps that suggest controller bindings only for Meta's newer profiles (Touch Plus, Touch Pro), which the Frame's "
     "runtime rejects (XR_ERROR_PATH_UNSUPPORTED), get the same bindings as oculus/touch_controller instead "
     "(components Touch lacks are dropped). Without it such apps get no controller input."),
    ("aim_pitch", "float", 0.0, "Pointer tilt (degrees)",
     "Tilts the controllers' pointing ray up (+) or down (−), for games whose pointer doesn't hit what you aim at."),
    ("aim_yaw", "float", 0.0, "Pointer turn (degrees)", "Turns the controllers' pointing ray left (+) or right (−)."),
    ("aim_forward", "float", 0.0, "Pointer origin forward (m)",
     "Moves where the pointing ray starts forward (+) or back (−)."),
    ("refresh_rate", "float", 0.0, "Refresh rate (Hz)",
     "Display refresh rate for this game (72, 80, 90, 96, 108, 120 or 144; 0 = the game's choice). A video's frame "
     "rate that divides the refresh rate plays smoothest (e.g. 30 fps at 90 Hz, 24 fps at 72 Hz)."),
    ("layer_debug", "int", 0, "Extra diagnostics (log)",
     "Logs details for debugging: composition layers and swapchains, focus changes, play-space creation, pointer vs "
     "grip poses, refresh rates. No effect on the game."),
    ("eye_debug", "int", 0, "Per-eye diagnostics (log)",
     "For one eye looking wrong (e.g. a jittering right eye): logs each eye's submitted pose against a fresh one, the "
     "eye-to-eye relation and when each eye's image was released, also into framebridge.log in the game's storage "
     "(some games stop Android's log early). No effect on the game."),
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
     "Fake XR_FB_scene/spatial entities: a guardian-sized room with floor, ceiling and four walls, for mixed-reality "
     "games that build their level from the room (e.g. Demeter)."),
    ("controller_models", "int", 0, "Steam Frame controller models",
     "Games that ask the headset for its controller models (Meta's runtime controller models, XR_FB_render_model) get "
     "the Steam Frame controllers instead of Quest Touch controllers. The models come from the Frame's own SteamVR and "
     "are converted on the Frame at install time. Games that ship their own controller meshes aren't affected. Turning "
     "it on needs a rebuild (it adds a small library in front of OVRPort's loader)."),
    ("scene_height", "float", 2.5, "Emulated room height (m)", "Ceiling height for scene_emul."),
    ("scene_width", "float", 0.0, "Emulated room width (m)",
     "Override the guardian width (0 = use guardian, min 1.5 m)."),
    ("scene_depth", "float", 0.0, "Emulated room depth (m)",
     "Override the guardian depth (0 = use guardian, min 1.5 m)."),
    ("pose_consistency", "int", 0, "Consistent head poses per frame",
     "Repeat xrLocateViews queries for the same display time get the first, fully tracked answer again. Games that "
     "ask several times per frame got slightly different poses and rendered parts of the frame with different heads: "
     "judder (e.g. I Am Cat; proposed by Klownicle, GitHub #8)."),
    ("haptic_fix", "int", 0, "Fix controller vibration freezes",
     "Routes OVRPlugin through FramePort's extension shim, which turns Meta's amplitude-envelope vibrations into "
     "plain ones: OVRPort's loader reads their nanosecond duration as seconds and allocates gigabytes, freezing the "
     "Frame when the game vibrates a controller (e.g. Lucky's Tale's save slots, GitHub #9). Needs a rebuild."),
    ("rect_clamp", "int", 1, "Clamp image rects",
     "Keeps every submitted image rect inside its swapchain. Unity can size the eye area a few pixels past the image "
     "on some Frames; SteamVR then rejects every frame (xrEndFrame -25, XR_ERROR_SWAPCHAIN_RECT_INVALID) and the game "
     "stops drawing (e.g. PowerWash Simulator stuck at \"Waiting\")."),
    ("swap_eyes", "int", 0, "Swap eyes", "Swap left/right views (diagnostic)."),
    ("strip_depth", "int", 0, "Strip depth layers", "Remove XR_KHR_composition_layer_depth chains (diagnostic)."),
    ("mutable_fix", "int", 0, "Mutable swapchain fix", "Experimental Vulkan mutable-format workaround."),
    ("respace_kick", "int", 0, "Re-create reference space", "Recreate spaces after the first frames (diagnostic)."),
    ("flip_quads", "int", 0, "Rotate quads 180°",
     "Older workaround for upside-down quads: rotates them 180° (quads are single-sided; prefer flip_emul)."),
    ("vk_shader_fix", "str", "", "Vulkan shader fixes",
     "Vulkan shim (frame.vk_sanitize): SPIR-V modules matching size + SHA-256 get words inserted at a byte offset, "
     "e.g. stores that initialize locals a shader reads before writing (an undefined loop counter hung the GPU in "
     "VR4's campaign). Format: <size>:<sha256>:<byte offset>:<word>,<word>,...; several separated by ';'. Comes from "
     "a game's recipe."),
    ("vk_query_slots", "int", 0, "Vulkan shim: slots per occlusion query",
     "Vulkan shim (frame.vk_sanitize): on gives every occlusion query room for both eyes. In multiview passes the "
     "Frame's driver writes a zero result for the second eye into the next query, so engines that count on one slot "
     "cull visible objects (models popping in and out, e.g. Into The Radius 2). 0 = off, 1 = on (2 slots)."),
    ("vk_spec_fixes", "int", 0, "Vulkan shim: depth spec fixes",
     "Vulkan shim (frame.vk_sanitize): makes two Unreal habits valid Vulkan - depth images get "
     "VK_IMAGE_USAGE_TRANSFER_DST_BIT (they are cleared with vkCmdClearDepthStencilImage) and Qualcomm shader-resolve "
     "subpasses lose an invalid depth resolve. Found with the validation layer in Into The Radius 2 (flickering "
     "models, windows behind models)."),
    ("vk_validation", "int", 0, "Vulkan shim: validation layer",
     "Diagnostics: the Vulkan shim (frame.vk_sanitize) adds Khronos' validation layer to the game's instance; its "
     "findings go to launch.log. The layer library (libVkLayer_khronos_validation.so) must be in the APK."),
    ("gl_hide_msrtt", "int", 1, "GL shim: hide multisampled render-to-texture",
     "GL shim only: hide GL_EXT_multisampled_render_to_texture(2) (Zink crashes rendering Unity's runtime MSAA eye "
     "buffer through it, e.g. The Room VR)."),
    ("gl_hide_multiview", "int", 1, "GL shim: hide multiview",
     "GL shim only: hide GL_OVR_multiview so all passes use single-view shaders. For GLES games whose multiview "
     "shaders fail on single-view render targets (e.g. Path of the Warrior)."),
]


# settings that need a new build, not only new settings files: the Vulkan shim learned vk_shader_fix in 0.6.4 and
# vk_query_slots and vk_spec_fixes in 0.10.0
REVISIONS = {"vk_shader_fix": 2, "vk_query_slots": 3, "vk_spec_fixes": 2, "haptic_fix": 2, "pose_consistency": 2}

# How the "Game settings" dialog shows each setting to non-technical users: group, level (common settings are always
# shown; advanced ones only under "Show advanced settings"), a plain label and one-line help, the control, and the
# setting it depends on (only shown while that one is on). The technical title/description above stay for the CLI.
GROUPS = [("picture", "Picture"), ("controllers", "Controllers"), ("screens", "Menus and screens"),
          ("room", "Mixed reality"), ("video360", "360° pictures and video"), ("troubleshooting", "Troubleshooting")]
REFRESH_CHOICES = [(0.0, "Game's choice"), *((float(hz), f"{hz} Hz") for hz in (72, 80, 90, 96, 108, 120, 144))]
UI: dict[str, dict] = {
    "scale": dict(group="picture", level="common", label="Sharpness",
                  help="Higher looks sharper but needs more power; lower runs smoother. 1.0× is the game's own.",
                  control=("slider", 0.5, 2.0, 0.1, "{:.1f}×")),
    "refresh_rate": dict(group="picture", level="common", label="Refresh rate",
                         help="How often the picture updates. Higher is smoother if the game can keep up.",
                         control=("choice", REFRESH_CHOICES)),
    "controller_fix": dict(group="controllers", level="common", label="Use controllers",
                           help="Turn off only for games you play with your hands instead of controllers.",
                           control=("switch",)),
    "focus_hold_ms": dict(group="screens", level="advanced", label="Ignore focus dips up to", depends="focus_hold",
                          help="Longer for headsets that briefly think they're off your head.",
                          control=("slider", 500.0, 3000.0, 100.0, "{:.0f} ms")),
    "pose_consistency": dict(group="picture", level="advanced", label="Steadier head tracking",
                             help="For games that judder or shimmer while you hold your head still (e.g. I Am Cat).",
                             control=("switch",)),
    "haptic_fix": dict(group="controllers", level="advanced", label="Fix freezes when controllers vibrate",
                       help="For games that freeze the Frame when a controller vibrates (e.g. Lucky's Tale). Needs a "
                            "rebuild.",
                       control=("switch",)),
    "vk_spec_fixes": dict(group="troubleshooting", level="advanced", label="Fix depth flicker (Unreal Vulkan)",
                          help="For Unreal games whose models flicker or show through walls and windows on the Frame "
                               "(e.g. Into The Radius 2). Needs a rebuild.",
                          control=("switch",)),
    "vk_query_slots": dict(group="troubleshooting", level="advanced", label="Stop objects popping in and out",
                           help="For Vulkan games whose models flicker or vanish while you look at them (e.g. Into "
                                "The Radius 2). Needs a rebuild.",
                           control=("switch",)),
    "sync_guard": dict(group="troubleshooting", level="advanced", label="Steady controller input after focus",
                       help="For games that crash right after you return to them (e.g. after the Steam menu).",
                       control=("switch",)),
    "profile_remap": dict(group="controllers", level="advanced", label="Treat newer Quest controllers as Touch",
                          help="For apps that only know Quest 3/Pro controllers: without it they get no buttons.",
                          control=("switch",)),
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
                         help="Shows videos that games play on a flat panel (e.g. an intro video).",
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
    "scene_emul": dict(group="room", level="common", label="Pretend room layout",
                       help="Mixed-reality games that build their level from your room get a simple room with walls.",
                       control=("switch",)),
    "scene_height": dict(group="room", level="advanced", label="Room height", depends="scene_emul",
                         help="Ceiling height of the pretend room.", control=("slider", 2.0, 4.0, 0.1, "{:.1f} m")),
    "scene_width": dict(group="room", level="advanced", label="Room width", depends="scene_emul",
                        help="0 = the size of your play area.", control=("slider", 0.0, 10.0, 0.5, "{:.1f} m")),
    "scene_depth": dict(group="room", level="advanced", label="Room depth", depends="scene_emul",
                        help="0 = the size of your play area.", control=("slider", 0.0, 10.0, 0.5, "{:.1f} m")),
    "equirect_emul": dict(group="video360", level="common", label="Show 360° backgrounds and videos",
                          help="Video players' virtual theatres and 360° videos would otherwise stay black.",
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
    **{key: dict(group="troubleshooting", level="advanced", control=("switch",)) for key in (
        "foveation_fix", "hide_space_warp", "swapchain_fix", "layer_fix", "gl_hide_multiview", "mutable_fix",
        "flip_quads", "swap_eyes", "vk_validation", "rect_clamp", "gl_hide_msrtt", "strip_color_bias", "snapshot",
        "strip_depth", "respace_kick", "layer_debug", "eye_debug", "input_diag", "release_wait")},
}


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

    def detect(self, a):
        from .applicability import needs_scene, uses_equirect_layers, uses_render_models

        if self.key == "controller_fix" and a.extra.get("hand_tracking_only"):
            return Suggestion(True, "Hand tracking is required by the game: pass hands through instead of reporting "
                                    "Touch controllers (e.g. Silhouette).", {"value": 0})
        if self.key == "controller_models" and uses_render_models(a):
            return Suggestion(True, "The game asks the headset for its controller models: show Steam Frame controllers "
                                    "instead of Quest Touch controllers.", {"value": 1})
        if self.key == "haptic_fix" and "libOVRPlugin.so" in a.libs:
            return Suggestion(True, "Meta's OVRPlugin vibrates controllers through OVRPort's loader, which turns some "
                                    "vibrations into gigabyte allocations that freeze the Frame (e.g. Lucky's Tale, "
                                    "BattleSisters): vibrations are converted before they reach it.", {"value": 1})
        if self.key == "equirect_emul" and uses_equirect_layers(a):
            return Suggestion(True, "The game draws 360° layers (e.g. a video player's theatre or 360° videos), which "
                                    "the Frame's runtime can't show: show them as panels around you.", {"value": 1})
        if self.key == "hide_space_warp" and "libUnreal.so" in a.libs and "libOVRPlugin.so" in a.libs:
            # Unreal Engine 5 + Meta's plugin: Application SpaceWarp is on by default (Into The Radius 2 flickered;
            # OVRPort's patch_disable_space_warp doesn't reach UE5). Off until confirmed in more games.
            return Suggestion(False, "Unreal Engine 5 game with Meta's space warp: turn on if the picture flickers or "
                                     "smears (e.g. Into The Radius 2).", {"value": 1})
        if self.key == "scene_emul" and needs_scene(a):
            return Suggestion(True, "Mixed-reality game that builds its level from the room model: emulate a "
                                    "guardian-sized room (e.g. Demeter).", {"value": 1})
        return None

    def applies(self, a):
        from . import applicability as ap

        rules = {
            "scene_emul": lambda a: ap.uses_scene(a) or a.extra.get("mr_only"),
            "scene_height": lambda a: ap.uses_scene(a) or a.extra.get("mr_only"),
            "scene_width": lambda a: ap.uses_scene(a) or a.extra.get("mr_only"),
            "scene_depth": lambda a: ap.uses_scene(a) or a.extra.get("mr_only"),
            "passthrough_emul": lambda a: (a.extra.get("mr_only")
                                           or "com.oculus.feature.PASSTHROUGH" in (a.extra.get("features") or {})),
            "flip_emul": ap.is_vulkan,
            "flip_quads": ap.is_vulkan,
            "mutable_fix": ap.is_vulkan,
            "swapchain_fix": ap.is_gles,
            "gl_hide_multiview": lambda a: a.direct_vrapi and ap.is_gles(a),
            "gl_hide_msrtt": ap.is_gles,
            "vk_shader_fix": lambda a: a.engine == "Unreal",  # read by the Vulkan shim, which only Unreal games get
            "vk_query_slots": lambda a: a.engine == "Unreal" and ap.is_vulkan(a),
            "vk_validation": lambda a: a.engine == "Unreal",
            "haptic_fix": lambda a: "libOVRPlugin.so" in a.libs,
            "vk_spec_fixes": lambda a: a.engine == "Unreal" and ap.is_vulkan(a),
            "controller_models": ap.may_use_render_models,
            **{k: ap.is_gles for k in ("equirect_emul", "equirect_face", "equirect_res", "equirect_flip",
                                       "equirect_fps", "equirect_stereo")},
        }
        rule = rules.get(self.key)
        return bool(rule(a)) if rule else True

    def install(self, ctx: InstallContext) -> None:
        pass  # collected by adapter_settings()


def adapter_settings(recipe_patches: dict) -> dict:
    """Ordered settings for libframe_settings.so / settings.conf: base keys first, then enabled overrides in
    selection order."""
    out = dict(BASE_SETTINGS)
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
    return out


class DeviceFiles(Patch):
    id = "device.files"
    title = "Game config files"
    description = ("Writes config files into the game's Android/data/<package>/files/ on the Frame, to turn off engine "
                   "features the Frame doesn't support (e.g. a CryEngine user.cfg with r_variable_rate_shading = 0, "
                   "as The Climb 2 needs).")
    category = "device"
    needs_vr = False
    stage = "install"
    params = [Param("files", "text", {}, "path relative to files/ -> content")]

    def detect(self, a):
        if (a.engine == "CryEngine" and a.graphics.startswith("Vulkan")
                or (a.engine == "CryEngine" and "libCryRenderVulkan.so" in a.libs)):
            return Suggestion(True, "CryEngine: variable-rate shading isn't supported on the Frame, so user.cfg turns "
                                    "it off (r_variable_rate_shading = 0; e.g. The Climb 2).",
                              {"files": {"user.cfg": "r_variable_rate_shading = 0\n"}})
        return None

    def install(self, ctx: InstallContext) -> None:
        for rel, content in (ctx.params.get("files") or {}).items():
            ctx.files[rel] = content.encode() if isinstance(content, str) else content


class LeptonEnv(Patch):
    id = "device.lepton_env"
    title = "Extra Lepton environment"
    description = ("Environment variables exported by the launcher (e.g. VK_INSTANCE_LAYERS=\"\" to test without "
                   "Valve's layers).")
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


class TextInputWindow(Patch):
    id = "device.text_input_window"
    title = "Show the app's Android window (for typing)"
    description = ("Lepton runs VR apps headless: their Android window is never shown, so it never gets keyboard "
                   "focus and no key press (Steam's on-screen keyboard, a USB/Bluetooth keyboard, Type on Frame) "
                   "reaches the app's text fields. With Lepton's lepton-show-flatscreen marker the window is shown "
                   "(behind the VR view, which keeps working) and Steam's keyboard opens for a selected text field. "
                   "Steam lists the shown window as \"Gamescope\" (the Frame's compositor). Pairs with \"Make Unity "
                   "text fields work without a system keyboard\".")
    category = "device"
    stage = "install"

    def applies(self, a):
        return a.vr_kind != "none" and bool((a.extra or {}).get("text_fields"))

    def detect(self, a):
        if self.applies(a):
            return Suggestion(True, "App with text fields: key presses only reach a shown Android window.")
        return None

    def install(self, ctx: InstallContext) -> None:
        ctx.flatscreen = True


for _spec in SETTINGS:
    register(AdapterSetting(*_spec))
register(DeviceFiles)
register(LeptonEnv)
register(HideNavBar)
register(TextInputWindow)
