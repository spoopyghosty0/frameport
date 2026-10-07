# Steam Frame runtime reference (SteamOS 0.3.0, build 20260922)

## Lepton (Android container)
- Steam app **3029110 "Lepton"** (runtime) and **3056000 "Lepton Development"** (needs Developer Mode). The binary
  is `<Steam library>/steamapps/common/Lepton/lepton`; FramePort finds it via the appmanifests.
- Started per game with `lepton start` and these env vars: `SteamAppId`, `STEAM_COMPAT_INSTALL_PATH` (dir with
  `game.apk` + `obb/`), `STEAM_COMPAT_DATA_PATH` (container data/saves), `STEAM_COMPAT_SHADER_PATH`,
  `STEAM_COMPAT_LIBRARY_PATHS`, `IS_PARENT=true`, plus `LEPTON_ENV_<NAME>` to pass `<NAME>` into the container
  (FramePort passes `FRAMEBRIDGE_CONFIG`).
- Runs as podman container `lepton-steamlaunch-<appid>`; logcat is mirrored to stdout (→ `launch.log`) and to
  `~/.local/share/Steam/logs/lepton-logcats/steamlaunch-<appid>`. "Exited!" / "Early-exit" mark the end.
- Rootless podman creates a kernel session keyring per container start and never frees it; the per-user quota is
  200 keys, so ~200 launches after boot every container fails ("create keyring … Disk quota exceeded"). FramePort sets
  `keyring = false` in `~/.config/containers/containers.conf`; a reboot clears already-leaked keys.
- Picks the activity with category LAUNCHER (`APP_ACTIVITY`). Game data is visible at `/sdcard/Android/{obb,data}/<pkg>`.
- 64-bit only (no AArch32).
- `<install>/lepton-shaders/` holds Mesa's shader cache (Lepton sets `mesa.shader.cache.dir=/data/shaders`):
  `foz_cache.foz` + `foz_cache_idx.foz` per graphics driver (Turnip Vulkan, zink GL), typically 4–6 files and 7–19 MB
  per game, 2D apps included. It speeds up later starts; deleting it only costs one slower start.
- **2D apps** (verified 2026-10-02 with an open-source 2048 game): Lepton runs every app headless
  (`lepton.headless=true`; only OpenXR output reaches the headset) unless the app folder (`STEAM_COMPAT_INSTALL_PATH`)
  contains a file `lepton-show-flatscreen` (`liblepton/app_metadata.sh`): then Android (11, Waydroid) gets a Wayland
  window shown as a flat panel in the headset. FramePort creates the file for Android apps without VR. Android's
  back/home/recents bar is drawn over the app's own controls; `qemu.hw.mainkeys=1` removes it, but it's only read at
  boot and Lepton has no setting for extra properties, so patch `device.hide_navbar` (default on for apps without
  VR) passes it as a second line of a
  `LEPTON_GFXRECON_*` value (Lepton copies those into the boot properties unescaped). Runtime alternatives didn't
  work on Android 11: `policy_control` is gone, `cmd statusbar send-disable-flag home recents` has no `back` and
  moves the back button onto the app's controls, the `sysui_nav_bar` layout and disabling SystemUI had no effect.

## OpenXR runtime (as seen by games through overport's loader)
- Instance extensions present include KHR_android_create_instance (must be enabled; the adapter adds it),
  KHR_vulkan_enable(2), KHR_opengl_es_enable, KHR_composition_layer_depth, FB_display_refresh_rate, EXT_hand_tracking
  (synthesized from controllers), VALVE_frame_controller_interaction, META_recommended_layer_resolution,
  FB_swapchain_update_state, FB_space_warp, EXT_frame_synthesis, … (full list in any launch.log, tag overportOXR).
- Missing (emulated by the adapter): XR_FB_passthrough, XR_FB_scene / spatial_entity(_query/_storage/_container) /
  scene_capture, XR_FB_composition_layer_image_layout (flip), XR_KHR_convert_timespec_time (returns
  FUNCTION_UNSUPPORTED). XR_FB_render_model is presumably missing too (not checked on the device yet); the adapter
  emulates it only with `controller_models=1` and uses a native one if the runtime ever has it. Missing (dropped): XR_KHR_composition_layer_equirect2, XR_KHR_composition_layer_cylinder
  (the VrApi bridge converts cylinders to quads).
- Layer types (checked 2026-09-30 in `/opt/steamvr/bin/androidarm64/vrclient.so`, the Android-side runtime Lepton
  mounts at `/data/steamvr/runtime`): its compositors (`CSxrCompositorPrism`, `CSxrCompositorOpenVR`) only have
  `ComposeLayerQuad` and `ComposeLayerProjection`; cube/cylinder/equirect/equirect2 exist only as enum names, so
  there is nothing to switch on. It has a layer limit ("Exceeded the layer limit"; the value is logged by
  `layer_debug` from `maxLayerCount`). The adapter emulates cylinders (strips) and, per game, equirect layers
  (`equirect_emul`, an adapter projection layer). Quad layers are drawn above every projection layer regardless of
  submission order (seen 2026-10-01: quads placed before 4XVR's projection layer covered it).
- Swapchain formats: GLES `GL_SRGB8_ALPHA8` (35907) / `GL_SRGB8` (35905) only, samples = 1. Vulkan: 43 (R8G8B8A8_SRGB)
  and 50, not 37/44 (UNORM).
- Environment blend: ALPHA_BLEND available (greyscale passthrough cameras).
- Reference spaces: STAGE bounds are reported as 1×1 m.
- Display 72 Hz by default in tests. Head pose is only tracked while the headset is worn; otherwise flags 0x3 and
  the session stays below FOCUSED.

## Graphics stack
- Vulkan: freedreno (Mesa Turnip); Valve injects `VK_LAYER_VALVE_rpo` and `VALVE_fdm_injection` via
  VK_INSTANCE_LAYERS (set `VK_INSTANCE_LAYERS=""` through `device.lepton_env` to test without them).
  The FDM layer follows each eye's gaze; one-eye jitter in some games (GitHub #69) is fixed per game with
  `device.foveation`: `fixed` = `FDM_DEBUG=disable_offsets`, `off` = `VK_INSTANCE_LAYERS=""`. Lepton passes `FDM`,
  `FDM_DEBUG`, `FOVE_LEVEL` and `FDM_SWAPCHAIN_SIZE` through to the container (liblepton/mounting.sh PASSTHROUGH_VARS);
  the layers are chosen on the host, so setting them inside the game does nothing.
- GL ES: Zink (Mesa GL on Vulkan). Strict GLSL (see PLAYBOOK) and occasional `DEVICE LOST` with MSAA render-to-texture.

## Proton / Windows games (surveyed 2026-09-29; running a Rift game under it not yet verified)
- Steam on the Frame registers ARM64 compat tools from the app **"Steam Frame ARM64 Compat List"** (appinfo
  `extended.compat_tools`, found dynamically by the agent): `proton_11-arm64` (4628740, needs
  `steamlinuxruntime_steamrt4-arm64` 4185400), `proton-experimental-arm64` (4427310), `fex` (3127680, FEX-Emu for
  Linux x86 binaries; `/usr/bin/FEXBash` shows the chain steam-launch-wrapper → reaper → fex-compat-tool →
  SteamLinuxRuntime_4). None are installed by default.
- `steam -ifrunning steam://install/<appid>` only opens a confirmation dialog in the headset. The agent's unattended
  mode writes an appmanifest stub (StateFlags 1026, installdir from appinfo) and restarts Steam, which then downloads it.
- The command Steam runs is built from each tool's `toolmanifest.vdf` (`commandline`, `require_tool_appid`):
  `<SLR4-arm64>/_v2-entry-point --verb=waitforexitandrun -- <Proton>/proton waitforexitandrun <exe>`.
- Proton sets up VR (vrclient/wineopenxr registry) only when `SteamGameId` is set (steam_helper `setup_vr_registry`).
- Host OpenXR runtime for Linux processes: `~/.config/openxr/1/active_runtime.json` → `/opt/steamvr/steamxr_linuxarm64.json`
  ("SteamVR", `bin/linuxarm64/vrclient.so`); implicit API layer `XrApiLayer_VALVE_fdm_injection`.
- The "Steam" VR games in the Frame's library (Pistol Whip, Job Simulator, SUPERHOT VR) are APKs run by Lepton, not
  Proton.
- Verified on the device (agent `proton_selftest`, `xr_layer_test`): FramePort installs Proton 11 (ARM64) + SLR4 by
  itself (~90 s); Proton runs x86 `cmd.exe` in ~3 s; with `SteamGameId` set it registers wineopenxr
  (`HKLM\Software\Khronos\OpenXR\1` → `C:\openxr\wineopenxr64.json`). Headless launches need the display session (`DISPLAY=:0`,
  `GAMESCOPE_WAYLAND_DISPLAY=gamescope-0`), else Wine can't create windows and the process hangs.
- The Linux SteamVR runtime (Proton's loader `files/lib/aarch64-linux-gnu/libopenxr_loader.so.1`) **does** support
  `XR_KHR_convert_timespec_time` (unlike the Android runtime Quest games see), so wineopenxr's
  `XR_KHR_win32_convert_performance_counter_time` works.
- **The Linux SteamVR runtime only accepts OpenXR 1.0 apps**: `xrCreateInstance` with `apiVersion` 1.1 returns
  `XR_ERROR_API_VERSION_UNSUPPORTED` (-4), with any extensions; 1.0 works (checked 2026-09-29, SteamOS 0.4.2). Proton
  11's VR helper (`proton_vrhelper`) requests 1.1, so without help VR never starts for Proton games: the log shows
  `LoaderInstance::CreateInstance chained CreateInstance call failed`, games run as a flat window (Rick and Morty) and
  Revive's OVRPlugin reports `ovr_Initialize failed: Unable to load LibOVRRT DLL` (Lies Beneath, then a crash).
  FramePort's OpenXR layer (`native/xrlayer`, patch `pcvr.xr_timefix`, default on for PC VR games) retries such a
  create as 1.0; enabled by `XR_ENABLE_API_LAYERS` from launch.sh. Proton's Steam Linux Runtime container drops
  `XR_API_LAYER_PATH` (it keeps `XR_ENABLE_API_LAYERS`), so the agent registers the layer as an explicit layer in
  `~/.local/share/openxr/1/api_layers/explicit.d/` (home is shared into the container) with an absolute library path.
- Oculus Store builds that delay-load `LibOVRPlatform64_1.dll` (Platform SDK, e.g. Lies Beneath) crash with
  `0xc06d007e` (delay-load module not found): that DLL comes with the Oculus app, which the Frame doesn't have. (No Oculus runtime DLLs or
  registry keys are needed: Revive's LibOVRRT hook works once OpenXR does.)
- Unreal's Oculus plugin (and LibOVR's `ovr_Detect`) first checks for the Windows event `OculusHMDConnected` (created
  by the Oculus service on a PC) and silently skips VR without it. Revive hooks `OpenEventW` for that — Lies Beneath
  got as far as OVRPlugin, so the hook evidently works there — but it depends on Detours patching Wine's kernelbase,
  which is ARM64EC code under Proton arm64. Patch `pcvr.oculus_unreal` (default for Unreal Rift games) makes it
  independent of that: launch.sh starts the injector through `helpers/fp_oculushmd.exe` (`native/oculushmd`), which
  creates the real event and keeps it until the game (tracked by a job object) has exited.

## Steam integration
- Non-Steam shortcut: binary `userdata/<id>/config/shortcuts.vdf`; appid = crc32(exe+title) | 0x80000000;
  artwork in `config/grid/<appid>p.*` (portrait), `<appid>.*` (landscape), `<appid>_hero.*`, `<appid>_logo.*`.
  Steam reads the file only at start → stop steam.service, edit, start (FramePort does it once per batch).
- Processes started from Steam (Konsole, SSH sessions?) share steam.service's cgroup: use `systemd-run --user`.
- SSH: `sshd` must be enabled (`sudo systemctl enable --now sshd`), which needs a user password (`passwd`).
- mDNS: avahi-daemon runs by default; hostname `frame` → `frame.local`.

## Video of the headset view (surveyed 2026-10-05; used by the Live view tab)
- `steamvr-v4l2cam.service` (user unit, part of gamescope-session.target, `Restart=always`) runs SteamVR's
  `/opt/steamvr/bin/linuxarm64/v4l2cam --output=99`: it reads the compositor's "Headset View" (IVRHeadsetView) and
  writes it to a v4l2loopback webcam named **"SteamVR"** (`/dev/video99`, 1920x1080 RGB24, advertised 30 fps, frames
  arrive at the display rate). Nothing on the Frame reads it by default; idle it costs nothing, read ~0.2 core.
  It shows what the wearer sees (SteamVR home, Steam's panels; a game's layers are expected but not yet seen in it).
  Black and ~1 fps (one frame per ~1.0 s) while the headset sleeps (standby): a 30 fps stream then repeats each
  frame in bursts, which looks like a stall in a player. Its size follows SteamVR's headset view (v4l2cam has no size
  option), so the live view only scales down (360p/480p/720p/1080p) or sends it as is ("full").
- Sound: `pactl get-default-sink` (`alsa_loopback_device.stereo.alsa_output.platform-sound.HiFi__Speaker__sink`) and its
  `.monitor` source carry what the headset plays; the Frame's ffmpeg has the `pulse` input and `aac`. Timestamps: pulse
  uses the wall clock, v4l2 CLOCK_MONOTONIC → `-ts mono2abs` on the v4l2 input. Don't force
  `-use_wallclock_as_timestamps` on the pulse input: it stamped bursts of AAC packets with one time.
- Steam's own game recording / Remote Play / broadcast capture the **gamescope** PipeWire node (`CDesktopCapturePipeWire:
  ... node path: gamescope`): with gamescope's `--backend openvr` that's only the flat Steam UI, not VR. Steam's arm64
  `libvideo.so` encodes with x264 (vaapi/nvenc paths don't apply). No cast/spectator feature for the Frame's own VR view
  exists in Steam's UI; the SteamVR web server (27062) has no mirror route.
- Encoding: the Qualcomm encoder (`/dev/video23` qcom-iris-encoder, V4L2 M2M) doesn't work with the stock tools (ffmpeg
  `h264_v4l2m2m` hangs, gst `v4l2h264enc` not-negotiated). ffmpeg + libx264 works: FramePort's live view
  (`install/livestream.py`: `fps=30` before the scale, ultrafast/zerolatency, 3 threads, nice 10, fragmented MP4 on
  stdout, + AAC 128k) measured 0.34 core at 720p / 0.56 at 1080p with a quiet picture (video only); expect ~1-1.4
  cores with a busy scene.

## Text input

- Lepton's Android has no on-screen keyboard (IME) and VR apps run headless, so no Android window has keyboard focus
  and key presses (Steam's keyboard, USB/Bluetooth keyboards, `input text`) don't reach VR apps. The app folder's
  `lepton-show-flatscreen` marker shows the window (VR keeps working) and gives it focus; Steam's on-screen keyboard
  then opens for its text fields (FramePort patch `device.text_input_window`).
- `/dev/uinput` is writable by the steamos user (ACL for Steam Input): a uinput virtual keyboard works like a real
  one everywhere (FramePort's "Type on Frame", agent `_keyboard`).
- Unity text fields close without a system keyboard; see `frame.unity_text_input` in PLAYBOOK.md.


## Monitoring sources (probed 2026-10-07, SteamOS 0.4.3, kernel 6.18; used by the Monitor tab, agent `_monitor`)

All readable by the steamos user without root; the agent reads them directly (no programs started per sample).

| Metric | Source | Notes |
|---|---|---|
| CPU | `/proc/stat` deltas; `cpufreq/policy{0,2,5,7}` | 8 cores in 4 clusters: 0-1 / 2-4 / 5-6 / 7 (max 2.27 / 3.15 / 2.96 / 3.05 GHz) |
| GPU busy | Sum of `drm-engine-gpu` (ns) deltas in `/proc/<pid>/fdinfo/<fd>` over every render-node fd (msm DRM) | Gives GPU % per process too. A process can hold several render fds: add them all. Lepton games see the node as `/dev/kgsl-3d0` (bind mount of `/dev/dri/renderD128`). `drm-total-memory` reads 0 |
| GPU clock | `/sys/class/devfreq/3d00000.gpu/{cur,max}_freq` | 231–903 MHz in 12 steps |
| Memory / pressure | `/proc/meminfo`; `/proc/pressure/{cpu,memory,io}` (`some avg10`) | 16 GB RAM, 8 GB swap |
| Temperatures | `/sys/class/thermal/thermal_zone*/{type,temp}` | 48 zones: `cpu*`/`cpuss*`, `gpuss-*`, `ddr`, `nsph*` (NPU), `pm8550*`/`pm8010*` (power ICs), `modem*`, `camera*`, `video`, `max1720x_bat*` |
| Fan | hwmon `slg4ax46073v` `fan1_input` | ~8200 rpm idle |
| Power | hwmon `max34417_*` `power{1-4}_{label,input}` (µW) | `vph` = whole system (~3.6 W idle), `gfx` = GPU, `apc0/1/2` = CPU clusters, `nsp1/2` = NPU; each read is an I2C transfer (~0.7 ms wall), so only these are read |
| Battery | `power_supply/max1720x_bat_7-36` | `current_now` (µA, negative = draining) × `voltage_now` (µV) = watts; `time_to_empty_now`/`time_to_full_now` (s), `cycle_count`, `health`, `temp` (0.1 °C) |
| Game fps | `<base>/launch.log` lines `FrameBridge: pacing: N fps …` (every ~5 s) | Quest games only; SteamVR writes PC VR frame stats only as an end-of-session summary in `vrcompositor.txt` |
| Game container | conmon `-n lepton-steamlaunch-<appid>`; its child's `/proc/<pid>/cgroup` → `cpu.stat`, `memory.current` | Android processes in the container run under subuid-mapped uids (still the user's: the user owns the user namespace) |

Cost: a naive sample (fds of ~520 processes scanned) took 43 ms CPU. With kernel threads skipped after their first
sighting, command lines checked once per process and render fds cached (rescanned every 60 s, every 4 s for young game
processes without one), a steady process scan is ~2 ms (every 2 s) and the sensors ~4 ms per tick: ~0.6 % of one core.
