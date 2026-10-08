# Architecture

```
            ┌──────── UI (Flet, ui/: app shell + views/) ─────┐   ┌── CLI (cli.py) ──┐
            └───────────────────────┬─────────────────────────┘   └────────┬─────────┘
                                    ▼                                      ▼
                          pipeline.py  (add → suggest → build → install → test)
      ┌──────────────┬──────────────┼───────────────┬───────────────┬──────────────┐
  sources/      analysis/      recommend/        build.py        targets/       validate/
  quest_dump    detect, elf    catalog, engine   overport →      base.Target    static, device,
  rift_dump     stubgen, rift  (+ catalog/*.yaml) patches(apk) →  frame_lepton   triage (+triage.yaml)
                                                  apk/sign        pc_revive
                                    │                │                 │
                              patches/ registry   tools/ toolchain   frame/ ssh, discovery, pairing
                              overport, frame/*,  (JRE, overport,    install/installer ──► agent (on Frame)
                              settings            apksigner)
```

## Data flow for one game
1. **Source** (`sources/quest_dump.py`): folder with an APK and optional `<package>/` or `obb/` data.
2. **Analysis** (`analysis/detect.py`): package/label/version (pyaxmlparser), ABIs, engine, XR API, graphics API,
   direct-VrApi, GLAD/eglGetProcAddress, Unity MSAA levels, Meta permissions, telemetry references.
3. **Recipe** (`recommend/engine.py`): every patch's `detect()` suggests itself with a reason; a catalog entry (exact
   known-good recipe) overrides heuristics. The UI shows toggles; the user confirms.
4. **Build** (`build.py`): overport CLI (OVRPort; defaults + extras) → apk-stage patches in `order` on an
   `ApkWorkspace` → apksigner (with the package's own keystore) → static validation. Optional alternate build (e.g.
   without Unreal ForceQuit).
5. **Install** (`install/installer.py` + `agent/frameport_agent.py`): `prepare` (paths, what's already there) → SFTP
   uploads with resume → `finalize` (move into place, settings.conf/framebridge.conf, device files, launch.sh,
   deployment.json, artwork) → `shortcuts` (detached systemd unit stops Steam, writes shortcuts.vdf + grid art,
   restarts Steam).
6. **Test** (`validate/device.py`): agent `launch_test` (systemd-run launch.sh, wait, stop) → fetch launch.log →
   `triage.py` (milestones + signatures → suggested patches) → UI offers "apply suggestions and rebuild".

## Extending
- **New fix**: `patches/frame/<name>.py` with a `Patch` subclass (`detect`, `apply`, optional `validate`), plus a
  signature in `catalog/triage.yaml` and a PLAYBOOK row. Stages: `overport` | `apk` | `install`.
- **Upstream fixed a bug we work around**: register an `UpstreamFix` (`patches/upstream.py`) in the workaround's
  module: a probe that finds the fix in OVRPort's output (True / False / None = can't tell). Each build runs the probes
  on the converted APK and leaves out the workarounds whose fix is there (log "not needed: …", `build.superseded`,
  left out of settings.conf at install; game page and `frameport show` say so). The recipe keeps asking for the fix,
  so builds made with an older runtime keep the workaround. `FRAMEPORT_KEEP_WORKAROUNDS=1` turns it off.
- **New heuristic**: put it in the patch's `detect()` (and `applies()` for visibility), with the evidence in the
  reason text; check `scripts/eval_heuristics.py` still reproduces the catalog and add a test in
  `tests/test_heuristics.py`.
- **New game recipe**: `catalog/games/<package>.yaml` (or "Save as known-good" in the GUI, which writes to the user
  catalog). Publish recipes by serving a folder with `index.json` and pointing `FRAMEPORT_CATALOG_URL` at it.
- **New target**: implement `targets/base.Target`; the pipeline and UI only use that interface.

## Oculus Rift (PC VR) games
`sources/rift_dump` finds Windows game folders; `analysis/rift` (own PE reader) detects exe, bitness, engine, LibOVR vs
OpenXR, D3D version and Oculus Platform SDK use; ids are `rift.<slug>`. Their only patches are the `pcvr` category
(`patches/pcvr.py`: Revive, OpenVR backend, crash reporter, Oculus detection, OpenXR layer, Proton
log/version/env); `base.for_game` keeps Quest and PC VR patches
apart. "Build" = `pipeline.prepare_rift` (checks + Revive). Revive is a portable tool (`tools/revive.py` unpacks
`ReviveInstaller.exe` in pure Python: NSIS header + deflate blocks). Targets:
- `targets/pc_revive.PcReviveTarget`: Windows/WSL (`core/winhost.py`), non-Steam shortcut running
  `ReviveInjector.exe /openxr <exe>` via the agent's VDF code, grid art, local records in `<user data>/pc/`.
- `FrameLeptonTarget.install_pcvr` → `installer.install_pcvr` → agent `prepare_pcvr`/`finalize_pcvr`: upload
  `game/` + `revive/` (+ `xrlayer/` for `pcvr.xr_timefix`, `helpers/` for `pcvr.oculus_unreal`; size-manifest dedupe,
  stale files removed), `launch.sh` running the ARM64 Proton chain (built from toolmanifest.vdf; with
  `pcvr.oculus_unreal` the injector runs through `helpers/fp_oculushmd.exe`, which provides Unreal's
  `OculusHMDConnected` event); `proton_status`/`install_proton` manage Proton from Valve's ARM64 compat list.
- **Native binaries** (`artifacts/`): edit `native/…`, run `python native/build.py`, commit the new artifacts + SHA256SUMS,
  run `frameport parity` to see which games change.

## Self-update (`updates.py`, `ui/updater.py`, `cli.py update`)
```
check()  GitHub releases/latest (cached 6 h, drafts/prereleases skipped, "skipped" version remembered)
   │     → Update(version, notes = release body incl. "What's new" from the annotated tag, asset for this OS, sums, wheel)
   ▼
install_kind()  bundle (running exe inside FramePort.exe's folder / FramePort.app / FramePort/FramePort)
   │            source (git checkout) → git pull --ff-only + uv sync     wheel (uv tool / pipx / pip) → reinstall wheel
   ▼ bundle
prepare()  <data>/updates/<ver>/: download → SHA256SUMS.txt check → extract to staged/ (ditto on macOS) → layout check
   │       → Windows: same Authenticode signer as the running exe → ready.json
   ▼
apply()   writes <data>/updates/apply.{ps1,sh}, starts it detached; FramePort quits. The script waits for the pid,
          Windows: backs up the files it replaces (updates/<ver>/previous) and copies over the folder (the zip has
          no folder of its own); macOS/Linux: mv target → .old, staged → target, rollback on failure, clears the
          quarantine; then relaunches. Log: <data>/logs/update.log.
```
GUI: background check 10 s after start + every 6 h → sidebar card + Library bar → dialog with notes → job
`app-update` (waits for other jobs) → restart. "Install updates automatically": prepare in the background, apply in
`main()` before the window opens. CLI: once-a-day hint from the cached result (a daemon thread refreshes it), `frameport
update [--check] [--yes]`. CI runs `scripts/update_smoke.py` on every OS with the archive it just built.

## Dynamic data (fetched live, cached, bundled fallback)
overport CLI release (Android-XR-Bridge/OVRPort, fallback ovrport/app) + patch list + titles, Temurin JRE (Adoptium API), apksigner (Google repository index),
store artwork/titles (overport image API), catalog (optional remote), Lepton location/appid (Frame appmanifests),
Steam user (Frame userdata).

## Built on

FramePort is a front end for other projects; most of the functionality comes from them:

| Project | Used for |
|---|---|
| [OVRPort](https://github.com/Android-XR-Bridge/OVRPort) (overport, originally [ovrport/app](https://github.com/ovrport/app)) | Converts Quest games to OpenXR: its CLI applies the game patches and supplies the OpenXR loader; FramePort also includes its VrApi→OpenXR adapter |
| Valve Lepton, Proton and SteamVR | Run Android games, Windows games and OpenXR on the Frame |
| [Revive](https://github.com/LibreVR/Revive) (LibreVR) | Runs Oculus Rift games on OpenXR / SteamVR |
| Mesa (Zink) | OpenGL ES on Vulkan on the Frame |
| [Khronos OpenXR SDK](https://github.com/KhronosGroup/OpenXR-SDK) | OpenXR headers for the native layers |
| Eclipse Temurin, Android apksigner, Android NDK | Java runtime, APK signing, building the native layers |
| [Flet](https://flet.dev) | The desktop app |
| OculusDB, Steam store | Game descriptions, genres and artwork |

FramePort's own parts: game detection and recipes, the Steam Frame OpenXR adapter (FrameBridge) and the other native
fixes in `native/`, the installer agent that runs on the Frame, and the desktop/command-line app.
