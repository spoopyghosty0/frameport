# Steam Frame runtime reference (SteamOS 0.3.0, build 20260922)

## Setup from the project page (checked 2026-10-10, dev Frame, SteamOS build 20260922)

- Factory Frames have no browser; Steam's taskbar **+** offers Chromium as a Flatpak (`org.chromium.Chromium`,
  flathub is configured as a system remote). It is the default handler for https links once installed.
- Desktop Mode has `curl`, `wget`, `python3` 3.12, `konsole`, `kdialog`, `xdg-open`, `avahi-browse`, `systemd-run`;
  no `wl-copy`/`xclip`.
- avahi-daemon runs, but `avahi-browse` from an SSH session fails ("Daemon not running": no system D-Bus access
  there). A stdlib-Python mDNS query works when it listens on port 5353 in the 224.0.0.251 group (like avahi):
  replies to a random source port are dropped by the Frame's firewall. `bootstrap/setup.sh` does exactly that.
- The Frame reaches the PC's port 8765 over the home Wi-Fi without any firewall change on the PC side here (WSL in
  mirrored mode with the earlier FramePort rule state). The PC is also visible through the Frame's hotspot
  (`wlanap`, 10.35.78.x): the script lists one PC once, by name and words.
- Dolphin's "executable scripts" setting is the default (ask); `.sh` files open with a `bash.desktop` handler that
  runs them in a terminal. Not used by the setup (a pasted line is simpler and needs no download prompt).

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

## Where FramePort keeps games (internal storage, microSD)
- Every game has an **anchor** on internal storage: `~/Applications/quest-frame/<pkg>/` with `launch.sh`,
  `deployment.json`, `artwork/` and `plays.log`. Steam's shortcut points at the anchor's `launch.sh`, so it never
  changes when the files move.
- The game's files (`lepton-app/`, `lepton-data/` = saves, `lepton-shaders/`, `settings.conf`, `launch.log`; PC VR:
  `game/`, `revive/`, `compatdata/` = Proton prefix; Linux: `app/`) live in `deployment.json["base"]`: the anchor
  itself on internal storage, or `<mount>/FramePort/<pkg>` on another drive (GitHub #90, agent v63).
- SteamOS mounts removable drives (microSD) under `/run/media/<user>/<label or uuid>`; the agent's `drives` reads
  `/proc/mounts` (plus the drives of Steam library folders) and refuses vfat/exfat/ntfs (Lepton's data and Proton
  prefixes need Unix owners, permissions and symlinks) and read-only mounts. A drive that isn't mounted is an error for
  new installs (never a silent fallback to internal storage); `list_installed` marks games on it `drive_missing` and
  their launchers stop with "storage not mounted?".
- `move` copies with `cp -a` inside `podman unshare` (files Lepton's containers own belong to subordinate user ids),
  compares file count + bytes, retargets absolute symlinks into the old folder (the LibOVRRT → Revive redirect), points
  `launch.sh` at the new folder and only then deletes the old copy; on the same filesystem it renames instead.
  Moving a game whose Lepton data holds absolute paths, or a PC VR prefix, is not yet verified on the device.

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
- Environment blend: ALPHA_BLEND available (grayscale passthrough cameras).
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
- How Lepton loads them: `liblepton/vulkan_layers.sh` mounts the chosen layers (only those in the OS image's
  `/usr/share/guestos/android/vendor/vulkan_layers`) into the app's lib dir and writes their names to Android's
  `settings global gpu_debug_layers` for `gpu_debug_app` = the game; Android's loader reads that list from GraphicsEnv
  at each vkCreateInstance and searches the app's lib dir (`/data/app/…/lib/arm64`). A layer bundled in the APK is
  found there too; FramePort's shader-fix layer (`frame.zink_shader_fix`) adds its own name to GraphicsEnv's list from
  inside the process (`android::GraphicsEnv::setDebugLayers`, exported by the guest's libgraphicsenv.so, Lepton 3.0.5).
  That is the only way to reach the Vulkan side of OpenGL ES games (Zink creates the instance inside Mesa).
- GL ES: Zink (Mesa GL on Vulkan). Strict GLSL (see PLAYBOOK) and occasional `DEVICE LOST` with MSAA render-to-texture.

## Proton / Windows games (surveyed 2026-09-29; running a Rift game under it not yet verified)
- Steam on the Frame registers ARM64 compat tools from the app **"Steam Frame ARM64 Compat List"** (appinfo
  `extended.compat_tools`, found dynamically by the agent): `proton_11-arm64` (4628740, needs
  `steamlinuxruntime_steamrt4-arm64` 4185400), `proton-experimental-arm64` (4427310), `fex` (3127680, FEX-Emu for
  Linux x86 binaries; `/usr/bin/FEXBash` shows the chain steam-launch-wrapper → reaper → fex-compat-tool →
  SteamLinuxRuntime_4). None are installed by default.
- `steam -ifrunning steam://install/<appid>` only opens a confirmation dialog in the headset. The agent's unattended
  mode writes an appmanifest stub (StateFlags 1026, installdir from appinfo) and restarts Steam, which then downloads it.
- **x86_64 Linux apps (agent v61; verified on the device 2026-10-07: an x86_64 glibc test program installed + launch test RUNNING, "machine=x86_64 glibc=2.41"):**
  FEX is Steam app 3127680 (`fex`, installs as `common/FEX-Emu`, ~6 MB, 21 s via the appmanifest-stub path:
  `install_proton` / `proton_status` with `kind: linux_x86`). Its toolmanifest commandline is
  `/fex-compat-tool %verb% --`, **no** require_tool_appid: it does not use the Steam Linux Runtime. fex-compat-tool
  (Python) runs `<FEX-Emu>/usr/bin/FEX` with RootFS `/usr/share/guestos/fex-mesa` (part of the SteamOS image: an
  x86 Arch-style root with glibc 2.41, Mesa, graphics_provider.json for x86_64 + i386), emulates x86_64 and i386
  (emulator.json), honors `STEAM_FEX_TSOENABLED`, `STEAM_FEX_MULTIBLOCK`, `STEAM_COMPAT_FEX_CONFIG`, sets
  `tu_override_uncached_as_cache_coherent=true` and logs to `/tmp/fex-compat-tool-<pid>.log`. It **exits 1 ("No compat
  data path?") without `STEAM_COMPAT_DATA_PATH`** (keeps Config.json/AppConfig/Server/Telemetry in `<it>/fex-emu/`):
  the Linux launcher exports `<base>/compatdata`. Version seen: FEX-2607-76-g37265b1. ldd can't read x86 programs,
  so the missing-library check is skipped for them; an x86_64 AppImage is extracted through the same chain.
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
- Oculus Store builds that delay-load `LibOVRPlatform64_1.dll` (Platform SDK, for example Lies Beneath) crash with
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
- Desktop Mode menu entries (GitHub #84, agent v63): Linux apps get `~/.local/share/applications/frameport-<slug>.desktop`
  (+ an executable copy in `~/Desktop` when that folder exists; Plasma starts executable `.desktop` files there
  without a trust prompt), `Exec=env FRAMEPORT_DESKTOP=1 "<anchor>/launch.sh"`, marked `X-FramePort-Package=<pkg>`.
  Plasma's launcher exits right after starting the program, so with `FRAMEPORT_DESKTOP=1` the Linux launcher skips
  its "Steam parent gone → end the app" watchdog and keeps the desktop's DISPLAY/WAYLAND_DISPLAY instead of taking
  gamescope's from Steam. Not yet tried from the Frame's Desktop Mode.
- Linux apps' own icons (GitHub #99, agent v64): finalize_linux (and every refresh of the menu entries) copies the
  app's icon to `<anchor>/artwork/app-icon.{png,svg}`: an AppImage's `squashfs-root/.DirIcon` (usually a symlink),
  else the `Icon=` of its top-level `.desktop` file (a folder app: the first `.desktop` within 3 levels) looked up
  next to it, in `(usr/)share/icons/hicolor/*/apps/` and `(usr/)share/pixmaps/`; the biggest PNG ≥128 px, else an
  SVG, else the biggest PNG. Symlinks are resolved and must stay inside the app folder; absolute and `../` names
  are ignored. Precedence for the menu entry's `Icon=` and the Steam shortcut's icon: the user's chosen/store icon
  (`artwork/.icon-source` = `custom`, written by the PC with every art upload) > the app's own (Steam: PNG only) >
  FramePort's placeholder (`artwork/icon.*`). `StartupWMClass=` is copied from the app's `.desktop` file (Plasma's
  task bar matches the window to the entry). A PNG ≤1 MiB goes back to the PC (finalize result `app_icon.png`,
  base64) and becomes the library's icon when the game has none and nothing was picked (`.app-icon` marker = its
  sha256, no `.picked`); folder apps get it on the PC at add time (`analysis/linux.find_icon`). Not yet seen in
  Desktop Mode on the device.

## Overlay apps (SteamVR / OpenVR overlays, checked 2026-10-10 on the dev Frame, SteamOS 0.4.5)
OpenVR overlay applications (`VRApplication_Overlay`: fpsVR, wrist watches, ...) work on the Frame itself, also
over Quest games.
- The Frame's host SteamVR (`/opt/steamvr/bin/linuxarm64`, always running in the VR session) serves `IVROverlay`
  010-028 / `IVRApplications_007` to native Linux arm64 clients through its own `libopenvr_api.so` (ctypes is
  enough: `native/vroverlay_probe/vroverlay_probe.py`). Overlay apps connect from SSH; vrserver logs
  `New Connect message from … (VRApplication_Overlay)`, `vrcmd --overlays` (`/opt/steamvr/bin/linuxarm64/vrcmd`,
  LD_LIBRARY_PATH=that dir) lists them `visible`, Steam's VR UI logs `[Overlays] Created: <key>` and loads a
  dashboard overlay's thumbnail.
- **Composited over Lepton (Quest) games**: with 4XVR running headless (FrameBridge 72 fps), the probe's head-locked
  overlay appeared in the headset view (`/dev/video99`, see "Video of the headset view") on top of 4XVR's theatre.
  The Android SteamVR runtime inside Lepton submits to the same host compositor, which draws host overlays on top.
- Seeing pixels without a worn headset: the compositor pauses in standby (headset view = black) and fades to a
  solid colour without tracking. For a few seconds after setting `power/pauseCompositorOnStandby` and
  `steamvr/forceFadeOnBadTracking` to false (IVRSettings; neither key is in the user's steamvr.vrsettings, so
  `RemoveKeyInSection` restores the default) the headset view shows the real composition (2 fps grabs: frames 7-10
  of 16 had the picture, the rest the fade colour). Restore both keys afterwards.
- **Windows overlay apps under Proton (ARM64)**: Temporal Reality's Windows build (Python/pyopenvr, x64) under
  Proton 11 (SteamGameId set, FramePort's timefix layer) created its overlays on the Frame's SteamVR (`vrcmd
  --overlays`: `temporalreality.watch`, `.settings` dashboard + 512x512 thumbnail), so Proton's vrclient bridges
  `VRApplication_Overlay` too. Not seen as pixels (that watch only shows on a tracked left controller). fpsVR (.NET,
  wine-mono) ran 60 s under Proton (SteamAPI ok: "Game process added: AppID 908520") but never loaded
  openvr_api.dll and quit by itself (presumably it waits for a Windows SteamVR process). A freestanding CRT-less x64
  exe (`native/vroverlay_probe/fp_vroverlay_probe.c`) died at its first kernel32 call (`c000001d` in the x64
  emulation thunk); a normal MSVC/MinGW build should be used for Windows probes.
- **Registration** (what SteamVR honours on the Frame): `IVRApplications::AddApplicationManifest(path, false)` from
  a utility client, live; the path is kept in `~/.config/openvr/config/appconfig.json` `manifest_paths`.
  `SetApplicationAutoLaunch` is accepted (GetApplicationAutoLaunch → true; vrserver has
  `CAppInfoManager::StartAutolaunchOverlays`) but on the dev Frame it never reached `steamvr.vrsettings` (no
  autolaunch entry for `temporalreality.overlay` there, appconfig.json holds only manifest_paths) and the owner's
  watch didn't start by itself, so FramePort doesn't rely on it (see "Autostart" below). The linuxarm64 vrserver only reads
  **`binary_path_linux_arm`**: a manifest with `binary_path_linux` alone is skipped ("must specify binary_path for
  launch_type binary. Skipping"; Steam's own steamapps.vrmanifest entries are skipped the same way), so an app's
  own Linux manifest/`--install` that only writes binary_path_linux can't be launched by SteamVR on the Frame.
  `LaunchApplication(key)` then starts the binary (Temporal Reality's launch.sh: process up, overlays created).
- A Steam shortcut of an overlay app and a game shortcut run at the same time (Temporal Reality, then 4XVR, both
  through `steam://rungameid`: both "Game process added", both kept running).
- FramePort (agent v75): `register_vr_overlay` writes `<anchor>/frameport-overlay.vrmanifest` (the app's own key,
  name and image from its bundled manifest, binary = launch.sh in `binary_path_linux_arm` + `binary_path_linux`,
  absolute paths) and registers it; `unregister_vr_overlay`, uninstall and purge remove it; `ensure_host_fixes`
  registers ones SteamVR missed (it wasn't running). Overlay apps skip launch tests, the Linux launcher's
  Steam-parent watchdog (`FRAMEPORT_OVERLAY=1`) and don't count as a running game.
- **Autostart (agent v76)**: the deployment's `overlay.autostart` is the source of truth (SetApplicationAutoLaunch
  is still set for SteamVR builds that honour it). While at least one installed overlay app has it on,
  `ensure_host_fixes` keeps the user service `frameport-vr-overlays.service` (`~/.config/systemd/user`, enabled for
  default.target, Restart=always) = `frameport_agent.py _vr_overlay_watch`: every 5 s it checks the vrserver it knows
  (`/proc/<pid>/stat`: name + start time; a full /proc scan only when that one is gone); a new vrserver (boot,
  SteamVR restart, or the first look after the service starts) gets one round in a child process
  (`_vr_overlay_round`, timeout): wait ≤180 s until IVRApplications answers, 15 s for SteamVR's own auto-launch,
  then `launch_vr_overlay` for each autostart app that isn't running (a process with its install folder/anchor in
  the command line, or GetApplicationProcessId ≠ 0: never a second copy). The handled vrserver is kept in
  `~/.cache/frameport-vr-overlays.json`, so an agent update (the watcher exits when its file changes, systemd starts
  the new one) doesn't restart an app the user closed. Log: `~/.local/share/frameport/vr-overlays.log` (128 KB, one
  `.1`). Removed when no overlay app has autostart (game page switch, uninstall), by purge, and by the kill switch
  `~/.local/share/frameport/vr-overlays.disabled` (or `FRAMEPORT_NO_OVERLAY_AUTOSTART=1` in the agent's environment).
  Cost: one idle python3 (~30-40 MB RSS, no CPU between polls). Dev Frame 2026-10-10 (vrserver up since boot,
  Temporal Reality already started from its Steam shortcut): `new SteamVR (vrserver 2297)` →
  `linux.temporalreality: running` (not started again). Start after a real SteamVR start/boot: not yet seen.

## Video of the headset view (surveyed 2026-10-05; used by the Live view tab)
- `steamvr-v4l2cam.service` (user unit, part of gamescope-session.target, `Restart=always`) runs SteamVR's
  `/opt/steamvr/bin/linuxarm64/v4l2cam --output=99`: it reads the compositor's "Headset View" (IVRHeadsetView) and
  writes it to a v4l2loopback webcam named **"SteamVR"** (`/dev/video99`, 1920x1080 RGB24, advertised 30 fps, frames
  arrive at the display rate). Nothing on the Frame reads it by default; idle it costs nothing, read ~0.2 core.
  It shows what the wearer sees (SteamVR home, Steam's panels; a game's layers are expected but not yet seen in it).
  Black and ~1 fps (one frame per ~1.0 s) while the Frame sleeps (standby): a 30 fps stream then repeats each
  frame in bursts, which looks like a stall in a player. Its size follows SteamVR's headset view (v4l2cam has no size
  option), so the live view only scales down (360p/480p/720p/1080p) or sends it as is ("full").
- Sound: `pactl get-default-sink` (`alsa_loopback_device.stereo.alsa_output.platform-sound.HiFi__Speaker__sink`) and its
  `.monitor` source carry what the Frame plays; the Frame's ffmpeg has the `pulse` input and `aac`. Timestamps: pulse
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
  cores with a busy scene. That is now only the fallback.
- Hardware encoding (2026-10-07): the iris encoder works when driven directly through the V4L2 stateful encoder
  interface. That is FramePort's `fp_venc` (`native/venc`, spec + every measured value in `native/venc/SPEC.md`).
  Facts:
  - Device: `/dev/video23` (`/dev/video-enc0` links to it), driver `iris_driver`, M2M multiplanar.
  - Formats: input NV12/NV21/AB24(RGBA)/QC24/Q08C; output H264/HEVC; sizes 128..8192.
  - NV12 layout (S_FMT answers):
    - stride is a multiple of 128;
    - the returned height is padded to a multiple of 32;
    - CbCr starts at stride × padded height;
    - sizeimage is rounded up to 4 KiB;
    - the default crop is the requested size.
  - Controls: CBR, FORCE_KEY_FRAME, PREPEND_SPSPPS_TO_IDR, HEADER_MODE joined, FRAME_SKIP_MODE, H.264 profiles
    Baseline..Constrained High, levels up to 6.0.
  - The `steamos` user can open it (group video).
  - The panel's current refresh rate can be read without privileges through DRM: `/dev/dri/card0` is mode 0666, and
    GETCRTC reports for example `2*2160x2160_96` (clock 1402720 kHz / 4448 × 3285 = 96 Hz) even while the Frame sleeps.
    The panel offers 72/80/90/96/108/120/144 Hz.
  - `/dev/video99` (v4l2loopback) has `max_buffers=2`: a reader asking for more gets 2.
  - Mid-stream keyframe requests (FORCE_KEY_FRAME) take effect on the next frame, and the GOP restarts from there.
  - Measured 2026-10-07 with the Frame asleep (still picture):
    - `fp_venc` alone at 32/36 fps: 1% of a core at 1080p, 2.5% at 720p.
    - Live view end to end: `fp_venc` 2.6% + ffmpeg 8.4% (AAC encoding + muxing).
  - Conversion cost per new picture (self-test, NEON, 2026-10-07):
    - 1080p (no scaling): 0.6–1.0 ms.
    - 720p (exact 3:2 fast path): 1.3–1.65 ms, down from 5.6 ms with the generic box loop (~11 ms at idle clocks).
    - 360p (3:1 fast path): 0.7 ms.
    - 480p: 2.8 ms; its 852-px width doesn't repeat cleanly, so it uses the generic path.
    - At 36 fps with live content that is roughly 2–6% of a core.
  - ffmpeg with raw H.264 on a pipe:
    - `-framerate` is ignored.
    - `-fflags nobuffer` loses the first seconds of tiny frames.
    - Numbering frames from 0 (`setts`) with no input timestamps holds all output ~7 s next to pulse's audio.
    - Arrival stamps alone (`-use_wallclock_as_timestamps 1`) bunch frames that are read together: gaps of 0–10 ms
      and 45+ ms, seen in the headset test as dropped frames.
    - What works: `-probesize 32 -analyzeduration 0 -use_wallclock_as_timestamps 1` on the input (ffmpeg reads it in
      step with the audio), then `-bsf:v setts=ts=N*(1/fps)/TB` on the output (exactly even frames; audio still
      0–N s alongside), plus `frag_keyframe` so that a requested keyframe starts a fragment.
    - With sound, ffmpeg paces its inputs against each other and pulse's audio arrives later than the wall clock
      (more while something plays). The video input then counts as ahead, and ffmpeg stops reading it for up to
      0.7 s, so the writer blocks. That was the cause of 1080p dropping frames: busy 1080p replayed with sound took
      34–47 s for 20 s of video. Video alone was fine; `nice`, `-raw_packet_size` and the input queue size didn't
      matter. Shifting the video input back fixes it, but too far makes ffmpeg hold the video for interleaving and
      release it in clumps: -1 s gave output gaps of up to 550 ms, a longer and stuttering delay in the browser.
      -0.25 s is the measured sweet spot (no stalled writes at busy 1080p with sound, steady 50–150 ms output; -0.5 s
      already clumps). Output audio/video spans stay equal because setts sets the output times.
    - Browser delay (headless Chromium, 720p, 2026-10-07): muted about 0.2–0.35 s behind the newest data; with
      sound about 1 s. Chrome keeps about 0.6 s of audio ahead and stalls below that, whatever the player does:
      1.1× catch-up gave 8 stalls per 30 s, no speed-up 1–2, same average lag; 50 ms fragments didn't help. So the
      player doesn't speed up while sound is on.
  - Quality (owner's headset test, 2026-10-07): 3 Mbit/s CBR at 720p36 showed heavy compression artifacts. The
    hardware path now uses VBR with a 1.5× peak (the encoder accepts BITRATE_MODE VBR + BITRATE_PEAK) and higher
    targets: 360p 1.5, 480p 2.5, 720p 5, 1080p 8, Full 10 Mbit/s. The x264 fallback keeps its rates.
  - The default sink is SUSPENDED while nothing plays; its monitor still delivers (silent) audio.

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
| Game container | conmon `-n lepton-steamlaunch-<appid>`; its child's `/proc/<pid>/cgroup` → `cpu.stat`, `memory.current` | The container's ~90 Android processes show as uid 1000 on the host and can be signaled (4XVR, 2026-10-07); Android names them after the package's last 15 characters (`lus4xvrplayerov`), the full name is in `cmdline` |

Cost: a naive sample (fds of ~520 processes scanned) took 43 ms CPU. With kernel threads skipped after their first
sighting, command lines checked once per process, render fds cached (rescanned every 60 s, every 4 s for busy young
game processes without one), temperatures every 2 s, the CPU-cluster power rails every 5 s and the battery gauge every
2-10 s, the stream measured ~10-13 ms CPU per 1 s tick on the device (idle clocks; idle or with 4XVR running) = about
1 % of one core, ~0.15 % of the whole CPU. The I2C sensors (power monitors, battery gauge) are the slowest reads.
