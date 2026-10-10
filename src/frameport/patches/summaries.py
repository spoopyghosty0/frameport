"""One plain sentence per patch: what it does for the player, no jargon. The game page shows these by default; the
technical description (patch.description) is shown with "Show technical details" and in the CLI.
Game settings (adapter.*) use their Game settings help line (patches/settings.UI) instead; entries here are the
fallback for settings that dialog doesn't show."""

SUMMARIES = {
    # OVRPort (the Quest → OpenXR conversion)
    "patch_copy_libraries": "Adds the parts that let a Quest game run on the Steam Frame. Needed for every Quest "
                            "game.",
    "patch_copy_ovrplugin_vrapi": "Gives older Quest games a newer version of Meta's VR plugin that can be converted.",
    "patch_disable_controller_offset": "Removes a controller position adjustment, for games whose hands look "
                                       "misplaced.",
    "patch_disable_meta_xr_audio_telemetry": "Stops Meta's audio library from reporting to Meta (only matters on PC "
                                             "emulators).",
    "patch_disable_space_warp": "Turns off Meta's frame-rate trick, which causes glitches or hangs outside Quest "
                                "headsets.",
    "patch_fix_min_android_sdk": "Raises the minimum Android version the game declares, so the new parts load.",
    "patch_fix_unreal_crash": "Prevents a start-up crash in some Unreal games.",
    "patch_force_passthrough": "Lets mixed-reality games start even though they insist on a camera view of your room.",
    "patch_generate_config": "Writes the conversion's settings into the game.",
    "patch_launcher_entry": "Makes sure the game has an entry that can be started.",
    "patch_mark_allow_backup": "Allows the game's data to be backed up.",
    "patch_mark_as_debuggable": "Allows reading the game's logs, which helps find problems.",
    "patch_meta_xr_audio": "Makes Meta's 3D audio library work outside Quest headsets.",
    "patch_oculus_unity": "Lets Unity games accept a headset that isn't a Quest.",
    "patch_oculus_unreal": "Lets Unreal games accept a headset that isn't a Quest.",
    "patch_remove_localized_names": "Keeps one name for the game, so it shows the same everywhere.",
    "patch_remove_unreal_force_quit": "Stops some Unreal games from closing themselves right after starting.",
    "patch_remove_uses_library": "Removes requirements for Quest-only system parts.",
    "patch_remove_vrapi": "Removes Meta's old VR library when the game doesn't need it.",
    "patch_replace_icon_label": "Uses the store's name and icon for the game.",
    "patch_vr_metadata": "Marks the game as a VR app so headsets show it as one.",
    "patch_vrapi_openxr": "OVRPort's own converter for games that use Meta's oldest VR interface (experimental).",
    "patch_ac_nexus_no_appsw_72": "Assassin's Creed Nexus: smoother picture at 72 Hz (turns off Meta's frame-rate "
                                  "trick).",
    "patch_ac_nexus_no_appsw_90": "Assassin's Creed Nexus: smoother picture at 90 Hz (turns off Meta's frame-rate "
                                  "trick).",
    "patch_clean_up_frida": "Removes debugging leftovers some game files contain.",
    # Steam Frame patches
    "frame.adapter": "FramePort's layer between the game and the Steam Frame: fills in what the Frame lacks "
                     "(controllers, room, passthrough). Needed for every converted game.",
    "frame.launcher": "Makes the game start from the Frame's Steam library.",
    "frame.start_activity": "Opens the app straight in VR, skipping its flat launcher screen. Turn off to use the "
                            "launcher, for example to import new content.",
    "frame.ltw_depth": "Stops a black picture in Minecraft launchers (for example QuestCraft).",
    "frame.unity_gl_shim": "Stops a gray or frozen screen in Unity games that turn on anti-aliasing while running "
                           "(for example The Room VR).",
    "frame.unreal_gl_shim": "Stops a crash a few seconds after start in some Unreal games with anti-aliasing (for "
                            "example Star Wars Pinball VR).",
    "frame.unreal_ovrp_entrypoints": "Lets older Unreal games start VR on the Frame (for example Star Wars Pinball "
                                     "VR).",
    "frame.pac_hints": "Stops a crash on the Frame's processor in games whose code the Quest runs more leniently "
                       "(for example Star Wars Pinball VR).",
    "frame.unity_no_overlay_copy": "Stops a gray screen crash in some Unity games by skipping their overlay layers "
                                   "(for example The Room VR).",
    "frame.nodebug": "Stops strict debugging checks that make some games quit.",
    "frame.meta_permissions": "Declares Meta's permissions some mixed-reality games ask for, so they don't stop.",
    "frame.ovrplatformcompat": "Adds a small piece of Meta's platform library that some games need to start.",
    "frame.ovrstubs": "Provides stand-ins for Meta store functions the game expects, so it doesn't crash at start.",
    "frame.langpacks": "Lets the game use language files already in its data instead of waiting for a download that "
                       "never comes.",
    "frame.ovr_trace": "For troubleshooting: records the game's requests to Meta's online services.",
    "frame.slz_vulkan_hooks": "Stops a start-up crash in Stress Level Zero games (for example BONELAB).",
    "frame.unity_user_presence": "Counts the headset as worn, so games that only move the player while it's worn "
                                 "get head tracking and controls (for example BONELAB).",
    "frame.unity_no_msaa": "Turns off a smoothing setting that crashes some older Unity games on the Frame.",
    "frame.unity_runtime_msaa_off": "Keeps a smoothing setting off that the game turns on itself and that can freeze "
                                    "the Frame.",
    "frame.sdl_clipboard": "Lets SDL and LÖVE games start on the Frame (they'd crash looking for a clipboard).",
    "frame.unity_oculus_check": "Lets older Unity games start VR without Meta's system apps (they'd stay on a 2D "
                                "screen).",
    "frame.unity_multipass": "Draws each eye separately. Try it when one eye shows a gray or broken picture.",
    "frame.swapchain_limit": "Allows very large pictures (8K video, theaters) instead of quitting.",
    "frame.vrapi_stub": "Stops Meta's old VR library from closing the game at start (for example Jurassic World "
                        "Aftermath).",
    "frame.tbxr_vendor": "Lets Team Beef ports (for example Lambda1VR) start on the Frame.",
    "frame.asset_files": "Lets the game find extra content it keeps next to its data (for example Star Wars Tales' "
                         "seasons).",
    "frame.ovr_microphone": "Stops a crash a few seconds after the logo in games with voice chat.",
    "frame.avatar_stub": "Lets games start that would stop at Meta's avatars (for example BlazeRush). No Meta "
                         "avatars.",
    "frame.vrapi_bridge": "Lets games built on Meta's oldest VR interface run on the Frame.",
    "frame.gl_shim": "Corrects graphics code the Frame's drivers reject (black screen with sound).",
    "frame.metaxr_telemetry": "Skips a Quest-only reporting step in Meta's audio library that crashes some games.",
    "frame.oculusos": "Provides stand-ins for Quest system calls some games make at start.",
    "frame.unity_text_input": "Lets you type into this app's text fields on the Frame (they'd close at once).",
    "frame.vk_sanitize": "Cleans up graphics data that crashes some Unreal games on the Frame.",
    # Files and environment on the Frame
    "device.files": "Puts settings files next to the game on the Frame, for example to turn off an unsupported "
                    "effect.",
    "device.hide_navbar": "Hides Android's back, home and recents buttons, which cover the app's own buttons.",
    "device.text_input_window": "Lets typing reach this app from Steam's on-screen keyboard or your PC's keyboard. VR "
                                "keeps working.",
    "device.display_mode": "Choose Flat window if a phone or tablet app shows nothing in the headset, VR if a VR app "
                           "opens as a flat window.",
    "device.lepton_env": "Extra settings for the Android container (for testing).",
    "device.foveation": "Try Fixed or Off if one eye shimmers or jitters while menus look fine.",
    # PC VR
    "pcvr.repack_launcher": "Starts the game with the launcher that came with your copy.",
    "pcvr.launch_args": "Starts the game with options, for example to choose SteamVR or OpenXR.",
    "pcvr.revive": "Lets games made for Oculus PCs run on SteamVR.",
    "pcvr.libovr_redirect": "Points Oculus games at Revive on the Frame.",
    "pcvr.no_crash_reporter": "Stops Unreal's crash reporter, which can block the game from starting.",
    "pcvr.oculus_unreal": "Lets Unreal games accept a headset that isn't an Oculus Rift.",
    "pcvr.revive_openvr": "On this PC, runs Revive through SteamVR (works with more headsets).",
    "pcvr.xr_timefix": "Lets games written for newer OpenXR versions run on the Frame.",
    "pcvr.proton_log": "Writes a detailed Proton log for troubleshooting.",
    "pcvr.proton_tool": "Which Proton runs the game on the Frame. Stable is smoother; try Experimental if the game "
                        "doesn't start or runs badly.",
    "pcvr.steamvr_tuning": "Picks a refresh rate and motion smoothing that suit the game on this PC.",
    "pcvr.proton_env": "Extra settings for Proton (for testing).",
    # Game settings the Game settings dialog doesn't show (the others use their help line there)
    "adapter.vk_shader_fix": "Corrects specific shaders that hang the game on the Frame. Comes from a game's recipe.",
}
