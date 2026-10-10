# Quest games on this PC (AXRB)

FramePort's PC target ("This PC") already runs Oculus Rift games through Revive. For Quest/Android games it uses
**AXRB** (Android XR Bridge, github.com/Android-XR-Bridge/AXRB): an Android 16 emulator (x86_64 Google APIs image,
WHPX) that runs arm64 games through Android's ARM translation (`libndk_translation`), with AXRB's own OpenXR runtime
inside Android and a Windows host process (`axrb-host-bridge.exe`) that opens a real OpenXR session on the PC's
runtime (SteamVR, Virtual Desktop, ...). FramePort only drives it: it downloads AXRB's official release when a user
first installs a Quest game on the PC, never bundles or modifies it (AXRB's host/clock code is under the AXRB
Source-Available License 1.0, which allows running it and interoperating through its public interfaces).

Status: **experimental**. AXRB is young (v1.0.4, 2026-10-06) and many games still fail in it; Steam Frame
controllers don't work in AXRB yet (AXRB issue #20).

## Spike results (2026-10-10, Windows 11 26200, i7-14700K, RTX 4080 SUPER, 64 GB; headless)

| Step | Result |
|---|---|
| AXRB install | `AXRB-Setup-<v>.exe /S` (electron-builder one-click NSIS, per user, no UAC) → `%LOCALAPPDATA%\Programs\axrb-launcher`, 462 MB. Release assets: Setup exe + `SHA256SUMS-<v>-setup.txt` + source zip (no portable zip since 1.0.4). |
| Runtime setup | Not scriptable through AXRB (it lives in the Electron launcher's JS), so FramePort reproduces it: 4 pinned `dl.google.com` zips from AXRB's `core/components.json` (emulator 36.5.11, platform-tools r37.0.1, build-tools 36.1, `sys-img/google_apis/x86_64-36_r07`), hash-checked, into `%LOCALAPPDATA%\AXRB Runtime\sdk`, AVD `axrb-managed-api36` written as AXRB writes it, `ready.json` / `license-acceptance.json` receipts so AXRB's own launcher sees the same runtime as set up. 2.4 GB download, ~3 min on a fast line; 26 GB on disk after one 7 GB game. |
| Emulator boot | AXRB's `scripts\emulator\windows_android_emulator.ps1 -Action Start` (headless QEMU, AXRB clock adapter + GPU layer). First cold boot ~6 min (TSC-corrected clock cold-boots every time). ABIs `x86_64,arm64-v8a`. QEMU (`Start-Process`) and the adb server inherit the caller's handles: a pipe, a file through WSL interop, even `/dev/null` through interop (its relay stays open) all left the FramePort call hanging after the script had ended → FramePort starts AXRB scripts through WMI (`Win32_Process.Create`, hidden, nothing inherited), polls the PID and reads an `FP_EXIT <code>` line from the log. Windows PowerShell 5.1's `*>>` writes UTF-16. |
| AXRB runtime | `adb install --no-incremental --force-queryable -r axrb-openxr-runtime-debug.apk` (0.3 s); found by the OpenXR loader through the system runtime broker. |
| Data | `adb push` of OBB/pak files: 25-35 MB/s (7.2 GB Pinball FX VR in 5.5 min). A full uninstall (or a signature change: `INSTALL_FAILED_UPDATE_INCOMPATIBLE`) deletes the OBB folder and the saves → FramePort's stable per-game keys matter here too. |
| **Original APK** (Pinball FX VR) | No launcher activity (Meta's VR category only), started explicitly: **SIGABRT 0.3 s after loading Meta's `libovrplatformloader.so`**. Unconverted Quest APKs don't run. |
| **OVRPort-only build** (Lucky's Tale, Unity 2019 GLES) | OVRPort's loader → AXRB runtime → instance + session, frames submitted (2 s each without a host: the image send times out), no crash in 40 s. |
| **OVRPort-only build** (Pinball FX VR, UE5 Vulkan) | OVRPlugin pre-init OK (AXRB reports "Oculus 67.522.0"), then exits (ForceQuit) / with the no-ForceQuit build SIGSEGV in `gfxstream vkAllocateMemory` called from AXRB's runtime Vulkan layer. Not decided headless (no host attached, see below). |
| Host bridge | Needs an OpenXR runtime with a D3D11 adapter. Headless options all failed: SteamVR's null driver (`failed to find D3D11 adapter requested by OpenXR runtime`), `vrlink` without a headset (`xrCreateInstance` -2), Meta XR Simulator (binary download needs a Meta developer login). → frames-to-SteamVR need a real headset. |

**Build decision: FramePort hands AXRB an OVRPort-only build** (the recipe's OVRPort patches, no Steam Frame fixes,
signed with the game's FramePort key). The original APK crashes; FramePort's Frame fixes (FrameBridge adapter, Lepton
and Frame-runtime workarounds) target the Frame. The recipe's alternate (no-ForceQuit) build is used when the recipe
says so.

### FramePort end to end (same day, headless, this branch)

`frameport install com.playful.LuckysTale --to pc --no-library` (isolated `FRAMEPORT_HOME`): PC build (19 patches:
OVRPort's + `frame.ovrplatformcompat`/`frame.ovrstubs`/`frame.swapchain_limit`; static checks pass without the
FrameBridge check) → AXRB found, requirements met → emulator → AXRB runtime → `adb install` → 3.7 GB OBB in ~95 s
(39 MB/s) → `deployment.json` + launcher script. `frameport test com.playful.LuckysTale --to pc`: launcher → AXRB run
script → host bridge → triage `axrb-no-headset` (expected without a headset). The Steam shortcut write (needs a Steam
restart) is unit-tested only.

## How FramePort drives AXRB

- `tools/axrb.py`: find (env `FRAMEPORT_AXRB_DIR` > installed AXRB) / install (Setup exe, SHA256SUMS checked, `/S`) /
  runtime setup (above) / requirements (WHPX via AXRB's `check_windows.ps1`, ≥ 12 GB RAM, x64).
- Environment for every AXRB script (the installed build sets none of it itself): `AXRB_DATA_HOME=<Runtime>\output`,
  `ANDROID_AVD_HOME=<Runtime>\avd`, `ANDROID_USER_HOME=<Runtime>\android`, `ANDROID_HOME`/`ANDROID_SDK_ROOT=<Runtime>\sdk`,
  `ANDROID_ADB_SERVER_PORT=5038`, `ADB_USB_LEGACY=1`, `ADB_LOCAL_TRANSPORT_MAX_PORT=5683`, AXRB's embedded Python
  (`resources\runtime\tools\python`) first on PATH; arguments `-Sdk <Runtime>\sdk -Avd axrb-managed-api36 -Port 5584`.
- Install: start the emulator if needed (and stop it again if FramePort started it), install the AXRB runtime APK,
  `adb install -r` the build, push OBB/data to `/sdcard/Android/obb|data/<pkg>/`, record `<data>/pc/<pkg>/deployment.json`
  (`kind: "android"`).
- Play: a Steam shortcut runs FramePort's small launcher script (`%LOCALAPPDATA%\FramePort\axrb\fp-axrb-run.ps1`),
  which sets the environment above and calls AXRB's `scripts\run\run_windows_game.ps1` (boots the emulator when
  needed and then owns it, registers a SteamVR app identity, starts the host bridge, `am start`, waits; exit codes
  0 ok, 1 error, 3 game lost, 4 host lost; `logs\game\session.json`, `host.err`).
- Logs for diagnostics: `<Runtime>\output\logs\game\{host.log,host.err,session.json,guest.log}`,
  `logs\emulator\emulator.std{out,err}.log`.

## Open (needs the owner + a headset)

- A game in the headset through SteamVR (picture, audio, controllers; Steam Frame controllers: AXRB #20).
- Pinball FX VR's Vulkan crash with the host attached (AXRB's demo game: likely an AXRB/emulator issue if it repeats).
- Play from the PC's Steam library (shortcut → launcher script → AXRB).
