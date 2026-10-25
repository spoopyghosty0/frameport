# Steam Frame runtime reference

Facts about the Steam Frame's runtime that FramePort relies on, checked on the device from SteamOS 0.3.0 (build
20260922) onwards; each section says when and on which version where it matters. Symptoms and fixes per game are in
[PLAYBOOK.md](PLAYBOOK.md), longer investigations in [DEVICE_NOTES.md](DEVICE_NOTES.md).

## Setup from the project page (checked 2026-10-10, SteamOS build 20260922)

- Factory Frames have no browser; Steam's taskbar **+** offers Chromium as a Flatpak (`org.chromium.Chromium`,
  flathub is configured as a system remote). It is the default handler for https links once installed.
- Desktop Mode has `curl`, `wget`, `python3` 3.12, `konsole`, `kdialog`, `xdg-open`, `avahi-browse`, `systemd-run`;
  no `wl-copy`/`xclip`.
- avahi-daemon runs, but `avahi-browse` from an SSH session fails ("Daemon not running": no system D-Bus access
  there). A stdlib-Python mDNS query works when it listens on port 5353 in the 224.0.0.251 group (like avahi):
  replies to a random source port are dropped by the Frame's firewall. `bootstrap/setup.sh` does exactly that.
- The Frame reaches the PC's port 8765 over home Wi-Fi without a firewall change on the PC (tested with WSL in
  mirrored mode). The PC is also visible through the Frame's hotspot
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
- Ending a game: Lepton traps TERM/INT and tears down with `podman exec … reboot -p` + `podman stop -t 0`; neither
  pauses the app (no `onPause`, no Unity `OnApplicationPause(true)`). `input keyevent` HOME does nothing in the
  headless container; `am start -a android.intent.action.MAIN -c android.intent.category.HOME` (Launcher3) pauses the
  game at once and stops it ~0.3 s later. FramePort does that before its own stops (GitHub #480, DEVICE_NOTES
  "Graceful stop").
- Steam's Exit game (`Apps.TerminateApp`) signals the game's whole process tree at once. `conmon` is re-parented to
  Steam's `reaper` (a bare child subreaper), so the container's Android processes are in that tree although their
  cgroup is podman's `libpod-<id>.scope`; moving processes out of Steam's `app-steam-app<appid>-<pid>.scope` doesn't
  shield them. The container is gone ~0.7 s after the call; nothing in launch.sh can run before that. Every Exit
  game path in Steam's UI (the in-headset frame menu, Quick Access, "exit the running game?" dialogs) is
  `SteamClient.Apps.TerminateApp(gameid, false)` in SharedJSContext's code (the menus are popups without scripts of
  their own; each popup window has its own `SteamClient` object, unused), and `SteamClient.Apps.TerminateApp` is a
  writable property there. FramePort hooks it while a game runs (launch.sh's `_exit_hook`, DEVICE_NOTES "Graceful
  stop"). CDP `Runtime.addBinding` works on Steam's CEF (calls arrive as `Runtime.bindingCalled` on the connection
  that added it; the function stays on the page after that connection closes, calls then go nowhere).
- 64-bit only (no AArch32): 32-bit-only APKs run through FramePort's a32 bridge (`frame.a32_bridge`).
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

- **Per-game language** (verified 2026-10-17): `device.language` uses the same unescaped
  `LEPTON_GFXRECON_*` boot-property path: `LEPTON_GFXRECON_FP_LOCALE="0\npersist.sys.locale=<tag>"`.
  Android reports the selected locale to the game at startup; `ro.product.locale` stays `en-US`.
  Choose Game language under Customize and apply with Update on Frame. English (US) removes the override.
  Games need to include and use the requested language; text and voice still need headset verification.

## Where FramePort keeps games (internal storage, microSD)
- Every game has an **anchor** on internal storage: `~/Applications/quest-frame/<pkg>/` with `launch.sh`,
  `deployment.json`, `artwork/` and `plays.log`. Steam's shortcut points at the anchor's `launch.sh`, so it never
  changes when the files move.
- The game's files (`lepton-app/`, `lepton-data/` = saves, `lepton-shaders/`, `settings.conf`, `launch.log`; PC VR:
  `game/`, `revive/`, `compatdata/` = Proton prefix; Linux: `app/`) live in `deployment.json["base"]`: the anchor
  itself on internal storage, or `<mount>/FramePort/<pkg>` on another drive (GitHub #90).
- SteamOS mounts removable drives (microSD) under `/run/media/<user>/<label or uuid>`; the agent's `drives` reads
  `/proc/mounts` (plus the drives of Steam library folders) and refuses vfat/exfat/ntfs (Lepton's data and Proton
  prefixes need Unix owners, permissions and symlinks) and read-only mounts. A drive that isn't mounted is an error for
  new installs (never a silent fallback to internal storage); `list_installed` marks games on it `drive_missing` and
  their launchers stop with "storage not mounted?".
- `move` copies with `cp -a` inside `podman unshare` (files Lepton's containers own belong to subordinate user ids),
  compares file count + bytes, retargets absolute symlinks into the old folder (the LibOVRRT → Revive redirect), points
  `launch.sh` at the new folder and only then deletes the old copy; on the same filesystem it renames instead.
  Moving a game whose Lepton data holds absolute paths, or a PC VR prefix, is not yet verified on the device.

## Room data

- SteamVR on the Frame has no Meta scene/anchor extensions. Its chaperone supplies only a 2D outline raised to
  2.43 m, without furniture, ceiling or a room mesh. Valve's default boundary is a 20-point circle of roughly 1 m
  radius; FramePort refuses to treat it as a room.
- FrameBridge's `scene_emul=1` serves saved room data, with a guardian box when no room is available. OVRPort's
  loader only dispatches functions in its own table: newer Meta entry points go through FramePort's extension shim.
  MRUK uses discovery (`xrDiscoverSpacesMETA`, older versions `xrQuerySpacesFB`) and requests Space Setup when
  discovery returns no rooms. Volumes hang below their top-face anchor in Meta's coordinate convention.
- Room files use STAGE coordinates (metres, +Y up, floor y=0, -Z forward). The format contract is
  [mr/ROOM_FORMAT.md](mr/ROOM_FORMAT.md). On the Frame: `~/.local/share/frameport/rooms/<universe>/room.txt`,
  optional `room-mesh.bin` and `scan.log`; `rooms/current` points to the most recently saved room.
- A room belongs to its chaperone universe. The agent reads the last universe switch from `vrserver.txt`, falling
  back to the newest universe in `~/.config/openvr/config/chaperone_info.vrchap`; dummy 424242 means none.
  A matching room is selected first, else `current`; the app reports `aligned: false` after a universe change.
- Quest launchers copy that room beside `settings.conf` before Lepton starts; FrameBridge reads
  `dirname($FRAMEBRIDGE_CONFIG)/room.txt`. Stale copies are removed if no room exists or emulation is disabled.
  Each game's persistent anchors stay in `/sdcard/Android/data/<pkg>/files/framebridge-anchors.txt`.
- With `scene_emul`, launch.sh starts `_scan_watch <pkg>` after Lepton, closing fd 9; its exit trap stops the
  watcher, which also exits if the launcher disappears. It polls once a second and logs to `<base>/scan-watch.log`.
  In `<base>/lepton-data/external/Android/data/<pkg>/files/`, FrameBridge writes `framebridge-scan-request`
  (`request <id> <unix_seconds>`); the watcher clears stale request/ack/result files at startup, writes
  `framebridge-scan-ack` (`ack <id>`), reuses the headset capture/build worker, stores the current room and copies
  it beside settings.conf before atomically writing `framebridge-scan-result` (`ok <id>` or `failed <id> <reason>`).
  A global `rooms/.scan.lock` excludes concurrent captures, including manual scans; `rooms/.lock` still protects
  room publication. Launcher exit cancels the scan; a 350-second deadline leaves time for the six-minute protocol timeout.
- The Room tab and `frameport frame room` manage rooms. `from-boundary` creates walls without furniture;
  `import` / `export` transfer room files; `show` writes an SVG preview; `build <dump.fprs>` syncs the bundled
  `fp_roomscan` by SHA-256 and runs it on the Frame with `--offline`. `scan` = agent `room_capture`: records the
  RoomView meshes (`/xrservice/roomview3d/meshData` + `/meshLayout`, `IVRBlockQueue_005`) while in-headset prompts
  guide the player, then builds the room from that recording.

### Scan preview

Live floor coverage is shown by default for manual and game-requested scans; the capture
payload accepts `preview: false`, and `frameport frame room scan --no-preview` disables it. A world-locked,
upward-facing 6 m square at floor height follows the current raw-to-STAGE calibration and stays centred on the
scan's first recorded camera position. Every 16th depth vertex contributes to raw-space 10 cm voxels; at most once
per second, newly occupied cells redraw a 256×256 RGBA map in one colour (FramePort's secondary blue), each cell a
filled 10 cm square, only the opacity telling floor (<0.3 m, faint), raised surfaces (0.3–1.5 m) and tall surfaces
(>1.5 m, strongest, the room's outline) apart; unseen transparent. Overlays have no depth (they draw over hands and
passthrough), so the map stays faint; three colours in 3x3 blotches clashed in the first headset test. It shows
coverage, not a validated room boundary. IVROverlay_028 slots 1/3/20/22/33/43/62 (OpenVR 2.15.6 header)
create/destroy/sort order (1, below the scan prompts' 10)/set width/set absolute STAGE transform/show/upload raw
pixels. Each upload is 256 KiB (at most 256 KiB/s); timing for the first
20 uploads and every tenth thereafter, plus failures, goes to `rooms/scans/scan-*.fprs.log` and the built room’s `scan.log`. Overlay errors or an
upload taking over 50 ms disable the preview while capture continues; headset appearance and upload cost still
need device validation.

## Save backups (Quest games, discussion #352)
- A Lepton game's saves are two folders of `<base>/lepton-data`: `internal/<pkg>` (= `/data/data/<pkg>`:
  `shared_prefs/`, `files/`, `databases/`; Unreal's `files/UE4Game|UnrealGame/.../Saved` often lives here) and
  `external/Android/data/<pkg>` (= `/sdcard/Android/data/<pkg>`: Unity's `files/`, Unreal's external `Saved`). On the
  dev Frame (2026-10-16) every file in them belongs to `steamos` (Lepton's `keep-id:uid=0`: the container's root and
  app = steamos), mostly mode 600/660 in 700/771/775/2771 folders; Lepton's links (`Android/obb/<pkg>`, `Movies`,
  `Download`, `Documents`) sit outside these two folders.
- Agent `_backup_saves '<json>'` streams a tar.gz of both (members `internal/…`, `external/…`, PAX, numeric owners and
  modes; last member `frameport-saves.json` = manifest with size + sha256 per file and what was left out) on the SSH
  exec channel's stdout, the result JSON on stderr. Left out: folders named cache/code_cache/no_backup/il2cpp/
  UnityShaderCache/(Vulkan)ProgramBinaryCache/GPUCache/… at any depth, Unreal `Saved/Logs|Crashes`, FrameBridge's
  `fb_snap_*`/`framebridge*` files, `.obb/.apk/.foz/.mp4/.mkv/.webm/.mov`, symlinks and special files, and files over
  64 MiB (big downloaded content; for example BattleTalent's `files/Video`). More than 4 GiB of saves is refused.
- `_restore_saves '<json>'` reads the archive from stdin and treats it as untrusted: only regular files and folders
  under `internal/`/`external/` (no `..`, absolute paths, links, devices, duplicates), ≤1 GiB per file / 8 GiB in all,
  each file's sha256 must match the manifest, the manifest's package must be the game. Everything is staged in
  `<base>/.saves-restore-*` first, a path that would pass through a link on the Frame is refused, then the current
  saves are written to `<base>/saves-before-restore.tar.gz` (one copy; `rollback: true` restores it and keeps the
  replaced saves as the new copy) and each file is renamed into place. Existing files keep their owner and mode, new
  ones get their folder's owner and the backup's mode (at least u+rw); save files the backup doesn't have are removed
  (left-out files stay). A failure while swapping puts the rollback copy back.
- Both run their file work in `podman unshare` (the agent re-runs itself as `_saves_ns <op>` with stdio inherited) so
  files owned by mapped ids are readable and keep their ids, take the game's launch lock (`<base>/.launch.lock`, a
  Play meanwhile is ignored as a double launch) and refuse while its container runs. `saves_info` sizes a backup and
  reports the rollback copy, `saves_rollback_delete` removes it.
- PC destination: `saves.folder` (empty = `<data>/save-backups`); Settings → Data has an awaited folder picker,
  a default reset and a write probe. Old destinations (`saves.previous_folders`) stay listed; switching never moves
  or deletes files. `saves.keep` defaults to 10 and prunes only the selected destination.
- `saves.auto` defaults off. Connection and 30 s poll hooks sync its value using `save_snapshot_settings` (flag
  `~/.local/share/frameport/save-snapshots/enabled`). Updated and newly written Quest launchers call `_snapshot_close`
  after stopping the container, inside the EXIT trap, with the launch lock still held on fd 9. The worker verifies
  that fd against `<base>/.launch.lock` and passes it through `podman unshare`; another launch cannot run meanwhile.
  `timeout -k 5 90` bounds the whole worker. PC VR/Linux launchers have no snapshot step.
- Snapshots use the same archive builder, exclusions and 64 MiB/file, 4 GiB/total limits as manual backups.
  File paths/sizes/content hashes form a fingerprint; unchanged saves skip compression. Complete snapshots are
  atomically published under `~/.local/share/frameport/save-snapshots/<pkg>/<ns>-<sha256>.tar.gz`, with fingerprint
  sidecars. The newest three per game are retained; interrupted `.part` files are overwritten on the next attempt.
- `save_snapshots` lists immutable snapshots oldest first per game; `_fetch_save_snapshot '<json>'` streams one
  (archive stdout, result JSON stderr). PC `install/saves.sync_snapshots` runs in the background, verifies the archive
  hash and package, imports it as a normal backup marked `automatic`, and applies `saves.keep`. A local
  `<data>/save-snapshots-synced.json` digest ledger prevents reimporting snapshots after pruning or deleting backups.
  Failures leave snapshots on the Frame for the next poll. This path does no disk/SSH work in rendering.

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
  (`equirect_emul`, an adapter projection layer). Refused cube swapchains use GLES/Vulkan stand-ins;
  `cube_quads` (default on) copies their faces into six quads for frames without a projection
  (see DEVICE_NOTES "Cube swapchains"). Quad layers are drawn above every projection layer regardless of
  submission order (seen 2026-10-01: quads placed before 4XVR's projection layer covered it).
- Swapchain formats: GLES `GL_SRGB8_ALPHA8` (35907) / `GL_SRGB8` (35905) only, samples = 1. Vulkan: 43 (R8G8B8A8_SRGB)
  and 50, not 37/44 (UNORM).
- Environment blend: ALPHA_BLEND available (grayscale passthrough cameras).
- XR_FB_composition_layer_alpha_blend and XR_KHR_composition_layer_color_scale_bias are not the runtime's (OVRPort's
  dispatcher lists alpha_blend): their structs are ignored, so FrameBridge emulates inverse alpha (`alpha_blend_emul`).
- Reference spaces: STAGE bounds are reported as 1×1 m.
- Display 72 Hz by default in tests. Head pose is only tracked while the headset is worn; otherwise flags 0x3 and
  the session stays below FOCUSED.

### Frame Controller input

The runtime offers `XR_VALVE_frame_controller_interaction`, with interaction profile
`/interaction_profiles/valve/frame_controller_valve`. [Valve's input reference](https://partner.steamgames.com/doc/steamhardware/steamframe/input)
describes two gamepad halves. Under `/user/hand/<hand>/input/`:

- Left: `dpad_up`, `dpad_down`, `dpad_left`, `dpad_right`, `view` (click and touch).
- Right: `a`, `b`, `x`, `y`, `menu` (click and touch). The face diamond is A bottom, B right, X left, Y top.
- Both: `system`, `bumper`; `squeeze` and `trigger` (click, touch, value); `thumbstick` (vector, x, y, click,
  touch); `grip/pose`, `aim/pose`. Haptics use `/user/hand/<hand>/output/haptic`.

SteamVR prefers a game's Frame Controller binding. Without one, its Touch remap sends left d-pad down as Touch X
and left/up/right as Touch Y; right A as Touch A and X/Y/B as Touch B. Left View sends Touch Menu.
FrameBridge's `quest_buttons` preference (`0` default, `1` Mirror, `2` Quest pair) enables the extension when offered
and suggests a Frame binding translated from the game's Touch suggestions. Common pressure/trigger/thumbstick,
pose and haptic paths keep their Touch counterparts; thumbrest touch and system click are dropped, bumpers stay
unbound. The player's SteamVR custom bindings can override suggestions. This works on both bridge ABIs, without
the arm64-only extension shim. See [Controller buttons](CONTROLLERS.md) and the [mapping contract](input/QUEST_BUTTONS.md).
The runtime accepts the suggested bindings in headless launches; the modes haven't been tried in the headset on
many games yet.

## Lua native pointer compatibility

Frame's Android processes can map libraries above the lowest 2^47 bytes of address space. The verified xLua API 105
engine in BattleTalent 4.7 rejects those otherwise valid pointers in `lua_pushlightuserdata`. This was isolated using
the unchanged library from the source APK: a real allocation at `0x10000000` round-trips, but its own native `xlua_tag`
pointer above 47 bits raises `bad light userdata pointer` and exits. With `frame.luajit_pointers`, both round-trip
exactly. No game scripts or account checks are changed. This validates an address-layout compatibility requirement;
the APK was not run on an actual Quest during this investigation.

The patch is restricted to SHA-256 `6d5860e4a4f1cc2b05c600c63297ed467369739666edca79413fff4374ff2684`.
It redirects only the Lua engine's public API to a pinned full-pointer LuaJIT, keeping the original xLua bindings,
LuaSocket and game helpers. The patched build survives a 60 s headless launch and creates OpenXR swapchains.
Early frame-rectangle errors and backend login errors remain in the capture; this does not verify rendering or play.
An arbitrary `lj_err_throw` followed by Unity destructor failures does not prove this pointer issue: first capture the
Lua error text. `Unknown Platform` also appears after this crash is fixed, so it was not the fatal cause here.

## Graphics stack

- Explicit foveation injection probe (SteamOS 0.4.5, 20261007.6125817, Lepton 3.0.5, 2026-10-11):
  Batman's normal Steam environment requested the FDM Vulkan layer but supplied no `FDM_DEBUG` flag.
  Appending `debug` while preserving inherited flags showed `fdm_injection_CreateInstance: skipped`.
  A temporary `FDM_DEBUG=enable,debug` launch created the Unity/Manta Vulkan instance and device,
  detected a 54x54 two-layer density map for 1728x1728 eye buffers, and logged render passes with
  `fdm 1`, `already_has_fdm=1`, and changing offsets for both eyes. This verifies the injection path,
  not visual quality, gaze calibration, sustained gameplay stability, or an FPS/power improvement.
  `device.foveation` mode `tracked` now explicitly loads the layer and sets `FDM_DEBUG=enable` without
  debug spam. It is opt-in; do not enable it globally or assume all GLES/Zink and Vulkan renderers work.
  Follow-up wearer test on 2026-10-11 FAILED in Batman: large rainbow glitches outside the gaze region
  and jitter even in clear regions. Restoring the original launcher and restarting removed BOTH symptoms,
  confirmed by the wearer. Do not recommend forced injection for this Batman build. Reaching foveated
  render passes was only a technical probe, not a working visual implementation. Eye-tracking logs also
  showed left-eye filter resets, but they do not establish the rendering bug's root cause.
  Keep `adapter.foveation_fix` separate: hiding the game's Quest extensions does not prevent the
  independent Valve injection path. A swapchain sample count of 1 also does not prove the absence of
  internal MSAA: the enabled Batman render-pass trace reported `samples 2`.
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

## Proton / Windows games (surveyed 2026-09-29)
- Steam on the Frame registers ARM64 compat tools from the app **"Steam Frame ARM64 Compat List"** (appinfo
  `extended.compat_tools`, found dynamically by the agent): `proton_11-arm64` (4628740, needs
  `steamlinuxruntime_steamrt4-arm64` 4185400), `proton-experimental-arm64` (4427310), `fex` (3127680, FEX-Emu for
  Linux x86 binaries; `/usr/bin/FEXBash` shows the chain steam-launch-wrapper → reaper → fex-compat-tool →
  SteamLinuxRuntime_4). None are installed by default.
- `steam -ifrunning steam://install/<appid>` only opens a confirmation dialog in the headset. The agent's unattended
  mode writes an appmanifest stub (StateFlags 1026, installdir from appinfo) and restarts Steam, which then downloads it.
- **x86_64 Linux apps (verified on the device 2026-10-07: an x86_64 glibc test program installed + launch test
  RUNNING, "machine=x86_64 glibc=2.41"):**
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
  `0xc06d007e` (delay-load module not found): that DLL comes with the Oculus app, which the Frame doesn't have.
- Revive under Proton arm64: Revive's Detours hooks (LoadLibrary, WinVerifyTrust) don't take effect because Wine's
  kernelbase is ARM64EC code. `pcvr.libovr_redirect` puts Revive's runtime where the game looks for `LibOVRRT*_1.dll`
  (gets past `-3001`), but games that check the runtime's Oculus signature then stop at `-3021`
  (`ovrError_LibSignCheck`), which FramePort doesn't bypass. So LibOVR games run only on the PC; OpenVR/OpenXR builds
  (for example Rick and Morty, Vader Immortal) run directly under Proton. DEVICE_NOTES "Revive under Proton arm64".
- Unreal's Oculus plugin (and LibOVR's `ovr_Detect`) first checks for the Windows event `OculusHMDConnected` (created
  by the Oculus service on a PC) and silently skips VR without it. Revive hooks `OpenEventW` for that — Lies Beneath
  got as far as OVRPlugin, so the hook evidently works there — but it depends on Detours patching Wine's kernelbase,
  which is ARM64EC code under Proton arm64. Patch `pcvr.oculus_unreal` (default for Unreal Rift games) makes it
  independent of that: launch.sh starts the injector through `helpers/fp_oculushmd.exe` (`native/oculushmd`), which
  creates the real event and keeps it until the game (tracked by a job object) has exited.

## Steam integration
- Non-Steam shortcut: binary `userdata/<id>/config/shortcuts.vdf`; appid = crc32(exe+title) | 0x80000000;
  artwork in `config/grid/<appid>p.*` (portrait), `<appid>.*` (landscape), `<appid>_hero.*`, `<appid>_logo.*`.
  KeyValues names are case-insensitive: Steam may save `Shortcuts` / `AppName` after a devkit registration
  (GitHub #263). The agent reads and updates keys case-insensitively, keeping Steam's spelling and existing appids.
  Steam reads the file only at start → stop steam.service, edit, start (FramePort does it once per batch).
- Processes started from Steam (Konsole, and SSH sessions started from it) share steam.service's cgroup and end with
  Steam: run such work through `systemd-run --user`.
- SSH: Developer Mode enables `sshd`; FramePort logs in with its own key (the setup adds it), so no user password is
  needed.
- mDNS: avahi-daemon runs by default; hostname `frame` → `frame.local`.
- Desktop Mode menu entries (GitHub #84): Linux apps get `~/.local/share/applications/frameport-<slug>.desktop`
  (+ an executable copy in `~/Desktop` when that folder exists; Plasma starts executable `.desktop` files there
  without a trust prompt), `Exec=env FRAMEPORT_DESKTOP=1 "<anchor>/launch.sh"`, marked `X-FramePort-Package=<pkg>`.
  Plasma's launcher exits right after starting the program, so with `FRAMEPORT_DESKTOP=1` the Linux launcher skips
  its "Steam parent gone → end the app" watchdog and keeps the desktop's DISPLAY/WAYLAND_DISPLAY instead of taking
  gamescope's from Steam. Not yet tried from the Frame's Desktop Mode.
- Linux apps' own icons (GitHub #99): finalize_linux (and every refresh of the menu entries) copies the
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
  autolaunch entry for `temporalreality.overlay` there, appconfig.json holds only manifest_paths) and the
  watch didn't start by itself, so FramePort doesn't rely on it (see "Autostart" below). The linuxarm64 vrserver only reads
  **`binary_path_linux_arm`**: a manifest with `binary_path_linux` alone is skipped ("must specify binary_path for
  launch_type binary. Skipping"; Steam's own steamapps.vrmanifest entries are skipped the same way), so an app's
  own Linux manifest/`--install` that only writes binary_path_linux can't be launched by SteamVR on the Frame.
  `LaunchApplication(key)` then starts the binary (Temporal Reality's launch.sh: process up, overlays created).
- A Steam shortcut of an overlay app and a game shortcut run at the same time (Temporal Reality, then 4XVR, both
  through `steam://rungameid`: both "Game process added", both kept running).
- FramePort: `register_vr_overlay` writes `<anchor>/frameport-overlay.vrmanifest` (the app's own key,
  name and image from its bundled manifest, binary = launch.sh in `binary_path_linux_arm` + `binary_path_linux`,
  absolute paths) and registers it; `unregister_vr_overlay`, uninstall and purge remove it; `ensure_host_fixes`
  registers ones SteamVR missed (it wasn't running). Overlay apps skip launch tests, the Linux launcher's
  Steam-parent watchdog (`FRAMEPORT_OVERLAY=1`) and don't count as a running game.
- **Autostart**: the deployment's `overlay.autostart` is the source of truth (SetApplicationAutoLaunch
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
  It shows what the wearer sees: SteamVR home, Steam's panels and games (4XVR's theatre with an overlay on top,
  2026-10-10).
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
  - Quality (headset test, 2026-10-07): 3 Mbit/s CBR at 720p36 showed heavy compression artifacts. The
    hardware path now uses VBR with a 1.5× peak (the encoder accepts BITRATE_MODE VBR + BITRATE_PEAK) and higher
    targets: 360p 1.5, 480p 2.5, 720p 5, 1080p 8, Full 10 Mbit/s. The x264 fallback keeps its rates.
  - The default sink is SUSPENDED while nothing plays; its monitor still delivers (silent) audio.

## Text input

- Lepton's Android has no on-screen keyboard (IME) and VR apps run headless, so no Android window has keyboard focus
  and key presses (Steam's keyboard, USB/Bluetooth keyboards, `input text`) don't reach VR apps. The app folder's
  `lepton-show-flatscreen` marker shows the window (VR keeps working) and gives it focus; Steam's on-screen keyboard
  then opens for its text fields (FramePort patch `device.text_input_window`).
- That window is a gamescope client: gamescope (`--backend openvr`, `--virtual-connector-strategy PerAppId`) shows a
  Steam app's windows as the SteamVR dashboard overlay `valve.steam.desktopgame.<appid>` with mouse input. SteamVR
  puts a game's window into theater mode while the game isn't a VR scene app yet (setting
  `dashboard.autoShowGameTheater`) and opens the dashboard on it when the OpenXR session starts: the controllers then
  drive a mouse pointer on the Android screen. FramePort turns the setting off while such a game runs (see
  DEVICE_NOTES.md "Typing window takes the controllers").
- `/dev/uinput` is writable by the steamos user (ACL for Steam Input): a uinput virtual keyboard works like a real
  one everywhere (FramePort's "Type on Frame", agent `_keyboard`).
- Unity text fields close without a system keyboard; see `frame.unity_text_input` in PLAYBOOK.md.


## Shared foveation controls (tested 2026-10-12)

Under a Quest game's **Game settings → Picture**, choose **Foveated rendering → Eye-tracked**, then **Save**.
Changes apply at the next game start.
The area selector now uses Valve's supported `FOVE_LEVEL` presets (GitHub #394; binary analysis
2026-10-17). Default leaves the runtime choice alone; on the shared injection path this is `lo`.
The old `FOVE_INNER`/`FOVE_OUTER` radius exports had no effect: Lepton does not pass them through,
and the layer does not read them. The previously documented 0.30/0.60, 0.25/0.50, 0.20/0.40
radius pairs do **not** describe the generated shared-injection density map.

The inspected arm64 `libVkLayer_VALVE_fdm_injection.so` has these semantics (virtual addresses):
- `init_fdm_fove_level` at `0x1cdf4` calls the cached option reader, then
  `debug_parse_flags_option` (`0x21d74`) with table `0x27e70`: named tokens `lo` → 1,
  `med` → 2, `hi` → 3. It does not call atoi/strtol. Unset, empty, unknown and numeric
  strings parse to zero and retain the current profile (initialized to 1 at `0x1ce74`).
  Tokens are case-sensitive and separated by non-alphanumeric/non-underscore characters;
  matching values are ORed, so combinations can alias another profile. `all` also yields 3;
  `help` prints the table and returns zero. Use a single preset token.
- Profile selection at `0x1918c–0x19364` uses normalized **rectangles**, not circular radii.
  `lo` has a full-density center rectangle `(x=.364, y=.367, width=.295, height=.286)`
  (constant at `0xe440`), then density byte `0x76` outside. `med` uses rectangles from
  `0xe988`: sizes `.250×.245`, `.295×.286`, `.386×.367`, with density bytes
  `0xff`, `0xa8`, `0x76` and `0x50` outside. `hi` uses `0xe9b8`: sizes
  `.246×.211`, `.281×.246`, `.351×.316`, `.386×.351`, with bytes
  `0xff`, `0xa8`, `0x76`, `0x50`, then `0x38` outside. Each byte is written to both
  R8G8 density channels. All three retain a full-density center (`0xff`); medium/high make it smaller
  and reduce peripheral density more. Coordinates are sampled on the density-map grid (`0x19378` onward).
- `FDM_DEBUG` flags `lo`/`med`/`hi` also select presets (high takes precedence), but an
  explicit recognized `FOVE_LEVEL` overrides them (`0x1ce14`). `enable` gates injection;
  `disable_offsets` fixes the gaze position (`0x1b6c8`), and `yflip` flips the vertical
  offset (`0x1b9b8`). These three flags do not resize the profile.
- `FDM_SWAPCHAIN_SIZE` is parsed by `atoi` (`0x1c518`), clamped to nonnegative, and
  seeds a known swapchain size (`0x1ccf8`). It is a size/matching hint, not a radius.
  The synthetic map (Zink/GLES dynamic-rendering path, `CmdBeginRendering`) is square with side
  `ceil(size/32)`, size = the render area width (`0x19048–0x19088`); image association also matches
  this tile size (`0x16c78`).
- Where the sharp area lands (device-checked 2026-10-24 with AC Nexus, GitHub #394; details in
  DEVICE_NOTES.md "Foveation: sharp area centre and size"): for Vulkan games the layer's
  `xrCreateSwapchain` creates a map of `ceil(w/32) x ceil(h/32)` texels per eye swapchain (one array
  layer per eye) and fills the preset rectangles in map-normalized coordinates, so the sharp area is
  centred on the **whole swapchain** (all presets centre at about 0.51, 0.51). It knows each view's
  imageRect (`xrEndFrame` stores them) but doesn't use it for the map. Eye-tracked offsets
  (`calc_offset`, `0x1b848`) are `gaze NDC x 0.5 x render-area size`, rounded to the offset
  granularity (8), measured from that same swapchain centre; `disable_offsets` (Fixed) also leaves
  every image without `VK_IMAGE_CREATE_FRAGMENT_DENSITY_MAP_OFFSET_BIT_EXT` (added at `0x16afc`
  only when offsets are on). A game that submits a smaller or shifted rect (dynamic resolution,
  asymmetric eyes: AC Nexus rect=0,0 / 287,0 1726x1728 in 2016x1728, Asgard's Wrath 2 0,0 / 347,0
  2237x2256 in 2592x2256) gets the sharp area off its view's centre, and the eye's optical axis
  (asymmetric FOV, for example -1.03/+0.88 rad) isn't the rect centre either. FramePort's
  `VK_LAYER_FRAMEPORT_fdm_centre` (native/foveation/centre.h, below Valve's layer) rewrites the
  per-view offsets so the area sits on each eye.
- Turnip (freedreno) applies a density map per GMEM **bin**: it reads the map on the CPU at the
  bin's centre (`tu_calc_frag_area`, texel size `2^ceil(log2(fb/map))` >= 32 px) and renders the
  whole bin at that density. The visible sharp area is therefore the set of bins whose centre falls
  in Valve's rectangle: its size snaps to the bin grid (bins are ~150-200 px here and get relatively
  smaller as render resolution grows), so the same preset looks bigger or smaller and changes
  shape between render scales. That part is the driver's design, not a setting; FramePort can't
  pick the bins.

Per-game library preferences now store `level`. Legacy radius choices are translated on read
(and written as `level` on Save): nearest old nominal .30 → lo, .25 → med, .20 → hi,
with larger values clamped by nearest choice; zero or unknown/invalid values → Default.
This preserves approximate intent, not a claim that these presets implement those radii.
Explicit `level` wins. Obsolete radius exports are removed from managed launchers.

For device verification, hold game scene, render resolution and gaze constant; compare explicit
`lo`, `med`, `hi` across fresh game starts (Default should match lo absent another runtime override).
First use Fixed for a stable comparison, then Eye-tracked to check gaze following. Enable
**Darken coarse fragments (test)** and adapter `snapshot=N` to capture submitted eye images headlessly
every N seconds (GLES: left eye at quarter size; Vulkan: both eyes at half size, the whole swapchain layer, with
imageRect and FOV in the log); compare the undarkened center and peripheral bands. Existing adapter
builds need diagnostic support installed. Turn the test and snapshots off before benchmarking.
Agent `FOVEATION_ENV_KEYS` must include `"FOVE_LEVEL"` for launcher updates via `set_settings`.

`device.foveation` enables Valve's shared Vulkan injection and suppresses competing native foveation through
`foveation_fix=1`. Game settings stores mode, profile and test selection as a per-game library preference, outside shared recipes. This remains
experimental: complete images were confirmed in RE4, Batman, Deadpool, The Climb 2 and Asgard's Wrath 2,
not all Quest games or renderers. RE4 and Batman checks include title/calibration scenes; these are not
equivalent to extended gameplay coverage. Asgard gameplay was visually confirmed, without an on/off benchmark.
Batman additionally receives the verified ordinary shared-allocation patch, guarded by the full original
OVRPlugin hash; unknown Batman versions fail explicitly rather than receiving unchecked binary offsets.

The Climb 2 uses a private `libvrapi_foveation.so` bridge, installed only into that game's APK after the
standard bridge patch. It retains images during held-focus visibility dips, separates mono overlay output
storage from stereo density-map storage, and rate-limits its view-cache writes. Its process-local Valve layer
correction supplies both eye offsets and avoids the invalid subpass mask-table reads seen during Alps loading;
`yflip` corrects vertical gaze direction. The renderer SHA-256 and 1.0 resolution scale are checked before
installation. The runtime correction additionally checks the exact Valve library SHA-256 and all four original
instructions before touching executable memory; unsupported libraries request injection be disabled and log
the reason. No system library is changed. A SteamOS update requires revalidation of this compatibility path.
Both-eye gaze following was confirmed in Climb's darkening diagnostic, but long-session stability remains
experimental. Off disables injection through launcher exports; the private APK compatibility support remains installed.

**Darken coarse fragments (test)** enables the dormant Vulkan shader diagnostic. It dims RGB where the
actual `FragSizeEXT` footprint exceeds one pixel; alpha is unchanged. Deadpool's shaded regions were confirmed
to follow gaze in the headset. Unsupported shader output interfaces are skipped, and multiple passes may
compound darkening, so this is a diagnostic rather than a precise composited mask. Do not benchmark with it on.
Uncheck it and save Game settings to remove the launcher opt-in. The library stays installed and dormant;
in Climb the same library also carries the compatibility correction.

High-resolution Deadpool samples crossed from about 45 to 90 FPS with injection enabled, but temperatures and
power rose as it rendered twice as many frames. Foveation does not guarantee lower temperature at uncapped FPS.
Preset export/migration has configuration coverage. Headless device check of the presets (2026-10-24, AC Nexus,
Fixed, darkening test + FrameBridge `snapshot=N`, which also reads Vulkan eye images back): DEVICE_NOTES.md
"Foveation: sharp area centre and size".

### User preferences (discussion #365)

Game settings → Picture provides Default / Eye-tracked / Fixed / Off, a runtime profile, and an advanced diagnostic switch.
`set_settings` atomically rewrites just the managed launcher exports and records them in deployment.json;
no APK rebuild, reinstall or live device intervention is needed. Changes apply at the next start. Default restores
recipe/catalog launcher defaults; explicit preferences win. One-time library migration extracts old recipe choices
and leaves internal APK compatibility support. Catalog defaults (including Climb 2 and Batman) still apply to new
entries. Shared working configs omit `device.foveation` entirely.

APK-side: adapter builds automatically prepare Batman's hash-verified allocation fix and Climb 2's
renderer-guarded private bridge for verified builds, independently of the runtime choice. Unknown versions skip
automatic compatibility edits; explicit catalog requests still fail validation rather than guessing offsets. FrameBridge
builds include the dormant diagnostic layer (activated only by `FRAMEPORT_FOVEATION_TEST=1`), so future test toggles
need only a launcher update. Existing builds need one normal update to acquire that support. Render resolution
(supersampling) changes the game's render resolution: higher is sharper but slower. Climb 2 foveation still requires
1.0 scale.

## Monitoring sources (probed 2026-10-07, SteamOS 0.4.3, kernel 6.18; used by the Monitor tab, agent `_monitor`)

All readable by the steamos user without root; the agent reads them directly (no programs started per sample).

| Metric | Source | Notes |
|---|---|---|
| CPU | `/proc/stat` deltas; `cpufreq/policy{0,2,5,7}` | 8 cores in 4 clusters: 0-1 / 2-4 / 5-6 / 7 (max 2.27 / 3.15 / 2.96 / 3.05 GHz) |
| GPU busy | Sum of `drm-engine-gpu` (ns) deltas in `/proc/<pid>/fdinfo/<fd>` over every render-node fd (msm DRM) | Gives GPU % per process too. A process can hold several render fds: add them all. Lepton games see the node as `/dev/kgsl-3d0` (bind mount of `/dev/dri/renderD128`). `drm-total-memory` reads 0. msm can report garbage engine time (SteamVR's vrcompositor: near 2^64, rising ~1000x faster than real time; idle Frame at 100 %, 2026-10-18): a per-fd delta longer than the interval falls back to `drm-cycles-gpu` / GPU clock (agent `gpu_busy_ns`) |
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

## Steam library scheduling

The agent holds automatic shortcut restarts while the local app's
`~/.local/share/frameport-app/gui.alive` heartbeat is under ten seconds old, as well as while a game or Desktop Mode
is open. Closed/crashed GUIs expire; the existing twelve-hour hold limit still applies.

`library_mode` accepts `now` (default), `later`, and `off`; FramePort stores its preference as
`install.steam_library`. Later writes shortcuts and journals packages/removals in
`~/.local/share/frameport/library-pending.json`. Steam rewrites shortcuts.vdf on exit, so a user drop-in
`~/.config/systemd/user/steam.service.d/frameport-library.conf` reapplies the journal in ExecStartPre, after the
previous session has exited. ExecStartPost verifies the entries and uses the existing live devkit fallback if Steam
removed them. The journal is cleared only after success, and survives agent/app exit and reboot. The hooks share the
shortcut worker lock; an active worker performs its own write/verification instead. Both hook lines start with
`-` and run under `/usr/bin/timeout -k 5 20` (present on SteamOS; `-` ignores exit codes, including 127 for a missing
interpreter or agent, but not a hook that hangs until the unit's 90 s start timeout); ExecStartPre also has its own
SIGALRM deadline. ExecStartPost only starts the verification as the transient unit `frameport-library-verify`
(`--no-block`, RuntimeMaxSec 300), so Steam's start never waits for its Steam/devkit waits. The pre hook leaves
shortcuts.vdf alone when nothing is pending. The drop-in is removed (and the user manager reloaded) as soon as the
mode isn't `later` and nothing is pending, and by purge; an otherwise empty `steam.service.d` goes with it.
SteamOS 0.3.0 facts (checked 2026-10-16): Steam is the static user unit `/usr/lib/systemd/user/steam.service`
(Type=simple, Restart=always, TimeoutStartSec 90 s, its own `-` ExecStartPre lines; `extra_args.conf` drop-ins are
Valve's documented way to extend it), systemd 257; no user drop-ins exist by default.
This automatic application targets SteamOS's `steam.service`; if Steam is started outside that service, use
FramePort's Restart Steam now action to apply the journal safely.

Off skips new shortcuts (existing entries remain). Play in Off, and Play for a pending Later entry, starts launch.sh
in a detached user systemd unit, just as launch tests do, without adding a devkit entry. Steam/SteamVR must already
be running. This loses Steam's per-game overlay/controller setup and Exit game integration; use FramePort Monitor
to stop the game. This direct path has unit coverage; headset input/visual behaviour needs device validation.
