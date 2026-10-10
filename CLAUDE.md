# FramePort — notes for Claude

FramePort ports Meta Quest standalone APKs to the **Valve Steam Frame** (SteamOS, aarch64). Games run in Valve's **Lepton**
(Waydroid-based Android container), one container per game, launched from a Steam library shortcut. Pipeline:
`scan → analyze → suggest recipe (catalog/heuristics) → user confirms → overport → Frame fixes → sign → static checks →
install over SSH (agent) → Steam shortcut → headless launch test + log triage`.

Read `docs/PLAYBOOK.md` (symptom → fix) before debugging a game, and `docs/FRAME_RUNTIME.md` for runtime facts.

## Layout
- `src/frameport/` — Python package. `pipeline.py` is the API the CLI (`cli.py`) and GUI (`ui/`, Flet 1.0) share.
  - `ui/` — `app.py` shell (sidebar with Frame connection + activity cards, routing, actions, 30 s connection poll),
    `theme.py` (dark design tokens; change colours/spacing only there), `components.py` (pill, card, callout,
    status_row, art_fill, confirm, `update()` = safe update: in Flet 1.0 reading `.page` of an unmounted control
    raises), `jobs.py` (background FIFO job queue, one at a time, cancel via Reporter; no Flet), `views/`
    (library: search/filters/tags/sort as pure tested helpers; game: hero + one-click install, patches under
    "Customize"; frame: device + readiness + installed, or connect wizard; files: the Frame's file manager (persistent
    like the library; locations = Videos/Downloads/Documents from agent `storage_targets`, each installed Quest game's
    storage, the home folder; SFTP via `install/files.py` list_dir/upload/download/make_dir/rename/delete, all
    confined to the location by `files.inside`; Lepton's links in a game's storage can't be renamed/deleted; replaced
    the "Send files" dialog, game menu → "Add videos & files…" = `go("files", pkg)`; multi-select bar (download/
    delete); drag-and-drop from the OS via the `flet-dropzone` extension (Apache-2.0, Flutter `desktop_drop`), which
    only a `flet build` bundle contains: `files.dropzone_available()` keeps it out of source runs and the PyInstaller
    fallback, which would show an unknown control); screenshots (Steam screenshots on the Frame, `install/screenshots.py`
    + agent v46 `list_screenshots`/`delete_screenshots`: the Frame files every headset shot under SteamVR 250820, so
    launch.sh logs `start/end <unix>` to `<anchor>/plays.log` (upgrade_launchers adds it; Proton launchers exec → start
    only) and shots are matched by time; thumbnails cached in `<data>/screenshots-cache/<frame>/`, shown by asset URL;
    delete leaves screenshots.vdf alone (Steam rewrites it at exit); game menu → "Screenshots" = `go("screenshots",
    pkg)`); live view (`views/live.py` + `install/livestream.py`: the Frame's built-in SteamVR "headset view" webcam
    (`steamvr-v4l2cam.service` → v4l2loopback "SteamVR" /dev/video99, see docs/FRAME_RUNTIME.md) + the default
    output's pulse monitor (sound) → `fp_venc` (`native/venc`, own clean-room V4L2 driver of the Frame's iris
    hardware encoder: RGB24→NV12 box downscale with NEON, H.264 CBR, one frame per slot of an even fraction of the
    panel rate read from DRM, e.g. 96 Hz → 32 fps; stdin `k` = keyframe, EOF = stop; `--probe` / `--selftest`;
    artifact `linux-arm64-bin/`, synced to `~/.local/share/frameport/bin` by sha256 at stream start, no agent change)
    piped into ffmpeg (`-c:v copy` + AAC), else the old ffmpeg/x264 30 fps path (qualities scale down only; "full" =
    SteamVR's size; stderr `live: encoder=… fps=…` → status line) →
    fragmented MP4 on an SSH exec channel's stdout (stdin EOF stops it) → relay on 127.0.0.1 (a new viewer asks
    the hardware encoder for a keyframe and waits for it, else gets init + fragments since the last keyframe of the
    *video* track) → player page
    `install/live_player.py` (MSE; starts muted as browsers require, "Sound on" button; 0.3 s cushion, catches up at
    1.1x, seeks only when >2 s behind: seeking to the very edge starved it) opened
    in the user's default browser: Flet can't show video outside `flet build` bundles; the stream outlives the tab and
    stops on disconnect/window close); monitor (`views/monitor.py` + `frame/monitor.py` + agent v62 `_monitor`: one
    JSON sample per tick over an SSH exec channel while the tab is shown (stopped in go/disconnect/on_close, the
    agent ends at EOF); sources and costs in docs/FRAME_RUNTIME.md "Monitoring sources"; game card (fps from
    FrameBridge pacing; End game = Steam's Exit game, then cmd_stop), tiles with `C.Sparkline` (Flet canvas, no charts
    extension), details, a pooled process table (Game / Steam & SteamVR / All; right-click: end / force kill / end
    game; MON_CRITICAL needs force, MON_NEVER is refused)); settings; welcome; activity panel). Files, Screenshots and the Library share right-click menus
    (one `ft.ContextMenu` per view, filled on right-click; on one of several selected items they act on the whole
    selection, `C.menu_targets`) and click-and-drag multi-select (`C.DragSelect`: pan start/end on the area + item
    hover events, which Flutter also sends with the button held; Flet can't report item positions, so no rubber band).
    Selection bars sit below the list: above it, their appearing shifted the items mid-drag. Async handlers (they
    await a FilePicker) must be coroutine functions or go through `page.run_task`: Flet doesn't await a lambda's
    coroutine (the Files row Download button silently did nothing). `ui_smoke.py --fake-frame --gestures` drives real
    mouse drags/right-clicks.
    User tags live in library entries (`tags`), filters in library setting `ui.library`.
    **Performance rules** (the app froze before): never put image bytes in controls — artwork is served by URL from the
    GUI assets dir (= user data dir; `ft.run(assets_dir=…)`), as thumbnails (`artwork/thumbs.py`, Pillow); the
    library view is persistent, streams cards in batches from a background thread and filters by toggling visibility;
    job/connection events call `app.refresh_view()` (targeted), not `render()`; no I/O in render paths.
    **Never recreate clickable controls on progress ticks** (sidebar, activity tiles): update their properties —
    replacing them 5×/s swallowed clicks (couldn't leave the Library during an upload).
    Labelled switches: `C.switch(label, …)` (Material's default label colour is dark on our dark theme).
    Help hints: wording for non-obvious terms lives in `ui/help.py` (`HELP`); show it with `C.help_icon(key)` or the
    `help=` argument of `section`/`status_row`/`kv`, tooltips via `C.tip()` (wraps). Game actions for the Library
    right-click menu (one `ft.ContextMenu` around the grid, filled on right-click) and the game page's "…" menu come
    from `app.game_actions()`. Picking art (`sources.apply_choice`) downloads into a staging dir and keeps the old
    art if nothing came back; "Update Steam art on Frame" re-sends the art set to the game's anchor (agent ≥ 12).
    Patch descriptions/reasons describe the general case, naming games only as "e.g. …".
  - Installs: queued/cancelled/failed ones are remembered (library setting `ui.installs`) → Library "Resume" bar;
    uploads are interruptible (Cancel checked per MiB) and resumable (big files via SFTP `.part` append, small files
    streamed in tar batches; the agent counts files already in `incoming/`). Multi-select in the Library queues
    installs after asking every needed question up front. Failures end in one pop-up (Resume / Uninstall / log).
  - Other drives (GitHub #90, agent v63): anchors stay in ~/Applications/quest-frame, a game's files may live in
    `<mount>/FramePort/<pkg>` (`deployment.json` base). Agent `drives` (/proc/mounts, /run/media + Steam library
    drives; vfat/exfat/ntfs/read-only refused), prepare*/finalize_linux `dest` (unmounted = error, never a fallback;
    installed games keep their base), `move` (detached systemd-run + `move_status`; cp -a under podman unshare,
    count+bytes check, symlink retarget, launch.sh: Quest app_dir line / Linux+PC VR rewritten from the record's
    `launcher` field, else text swap), list_installed `drive`/`drive_missing` (install_state keeps such games
    "installed"). PC: `install/drives.py`, library setting `install.drive` (Frame page → Storage), game menu
    "Move to…" (job kind tool-frame), CLI `frame drives`/`frame move`/`install --dest`. Untested on the device.
  - **PC VR repacks are pre-patched to run directly** (proven: Rick and Morty, Vader Immortal run when the exe is
    launched directly; Revive breaks them). So Rift games default to `as_is` = install the copy unchanged and launch
    the exe directly (`pcvr.xr_timefix` for the Frame OpenXR-1.1→1.0 fix, `pcvr.no_crash_reporter` for Unreal).
    **Revive is only suggested for games with Oculus (LibOVR) code** (`pcvr.revive`, and `pcvr.oculus_unreal`; repacks
    with a bundled Revive and SteamVR/OpenXR games run directly): needed by an un-cracked Oculus game that fails at
    "Initializing OVR session". Those (Lone Echo, Robo Recall, Lies Beneath: crack .7z not extracted /
    Platform SDK) hit Revive's Oculus-runtime **signature check** under Proton-arm64 — Revive's LoadLibrary/WinVerifyTrust
    hooks don't install (ARM64EC), and the game's Oculus SDK shim rejects the unsigned Revive runtime (wintrust +
    crypt32 signer "Oculus VR") — so they don't run on the Frame without extracting the repack's crack (which FramePort
    doesn't do). `VD.bat` is Virtual Desktop's launcher: ignore it except as an exe-location hint. Library migration
    `rift_run_direct` resets existing recipes.
    Auto launch-test is skipped on PC installs (it would start the game on the user's desktop). Launch tests collect the Unreal
    game log + crash summaries from the Proton prefix; triage `unreal-crash`. Lies Beneath via Proton without Revive
    crashed (UE 4.23 "Unhandled exception").
  - Rift scanning: `sources/rift_dump.scan` = the scanned folder's subfolders are games (one per folder; a folder is a
    game if all candidate exes sit under one child), recursing into collections; `analysis/rift.py` walks once,
    filters helpers + non-GUI PEs, `rank_exes` (Unreal *-Shipping beats its launcher, Steam builds −10, ambiguous →
    `exe_confirmed=False` → GUI exe dialog), `clean_title`, fingerprint (unchanged folders aren't re-analyzed),
    modular-Unreal Oculus plugin DLLs + UTF-16 markers count as LibOVR, `revive_bundled` → as-is.
  - Rift VR-API routing (`analysis/rift.py`): `openvr`/`openxr`/`libovr` detected from imports + bundled DLLs (NOT the
    universal-in-Unreal `IVRSystem`/`VR_InitInternal` strings). `frame_native = (openxr or openvr) and not libovr` =
    confidently runs on the Frame via wineopenxr (SteamVR), no Revive. Any LibOVR game → `needs_revive` → **PC only**
    on the Frame (Revive's ARM64EC hooks don't work under Proton-arm64, and FramePort does **not** defeat the Oculus
    runtime Authenticode signature check). Patch `pcvr.libovr_redirect` (Frame, default on for Revive games, agent v19 `set_libovr_redirect`) symlinks Revive's
runtime as `LibOVRRT{64,32}_1.dll` in the game's exe dir **and next to every OVRPlugin.dll** (Unreal's OVR shim
searches its own module dir, not the exe dir) — the LoadLibrary redirect (pure runtime substitution), which
does NOT touch the game's runtime signature check (a checking build still fails at `-3021`; only non-checking builds
run). PoC verified on-device (2026-09-30): with the redirect, Lone Echo's Oculus SDK now loads Revive's runtime and
reaches Oculus API init (`-3021`) instead of failing to load a runtime at all — i.e. the substitution works; `-3021`
is the downstream signature/runtime-init stage the redirect doesn't touch. (OVRPlugin/Unreal builds use a more
restrictive LibOVRRT search; the next-to-OVRPlugin placement covers the common case.)
Rick and Morty runs on the Frame via its catalog recipe (OpenVR, no Revive),
    not via static detection. PC installs default to Revive's **OpenVR** backend (`pcvr.revive_openvr` on) and
    auto-start SteamVR on Play (`winhost.start_steamvr`); the Frame launcher always uses `/openxr`. UI (`game.py`
    where()) states per-game where it runs; installing an Oculus game on the Frame shows a warning. Migration
    `rift_frame_native` re-analyzes + re-derives existing entries.
  - Own artwork (0.8.0): game menu → "Use your own artwork…" (also from the Find artwork dialog) =
    `views/art_dialog.show_custom_art_dialog`: one slot per kind (portrait/landscape/hero/logo/icon), FilePicker →
    `sources.apply_custom` (Pillow check, ≥64 px, ≤40 MB, scaled to ≤3840 px, PNG if alpha/logo/icon else JPEG,
    replaces only that kind + its thumbnails, drops the generated cover/banner, writes `.picked` = "custom") /
    `remove_custom`; Steam shapes are still composed from what exists (`steam.py` PREFER).
  - Art for Rift games: `artwork/sources.py` — Quest version package (OculusDB packageName, exact name or +
    "Unplugged"-type suffix, never sequels) → Meta art; OculusDB square cover; Steam (exact names only); exe icon.
  - Store details (`artwork/details.py`, entry `details`): OculusDB (description, genres, publisher, website; by Quest
    package or Rift match) + Steam appdetails (exact title: developer, release date, up to 6 screenshots → artwork
    `shot_N.jpg`). Meta store pages reject scraping, so Oculus exclusives have no screenshots. Genres become automatic
    tags. Steam shortcuts get a complete composed art set (`artwork/steam.py`: 600×900 / 920×430 / 1920×620 / logo /
    256 icon, blurred-backdrop compositing for square-only covers; no store art (at most the APK icon) → a placeholder
    set: the name on a colour from the title hash + the APK icon, `steam_set_for`; 2D Android apps get no store lookups) and tags: how it runs, the original platform
    (Meta Quest / Oculus Rift), genres, user tags — merged with tags set in Steam (non-Steam shortcuts can't hold a
    description).
  - Linux apps (GitHub #31, library kind `linux`, `linux.<slug>`; x86_64 builds run through FEX (app 3127680, no SLR:
    RootFS /usr/share/guestos/fex-mesa from the OS image; needs `STEAM_COMPAT_DATA_PATH`, see docs/FRAME_RUNTIME.md):
    agent v61 `linux_x86_tools`/`pick_tool`, `install_proton`/`proton_status` `kind: linux_x86`, `finalize_linux
    x86_64`, `installer.ensure_proton(kind=)`; an arm64 program wins over an x86_64 one): GUI = Add
    games → "Add a Linux app…" /
    "…folder…" (`app.add_linux` job → `pipeline.add_linux_app`); game page `linux_summary` (program + Change…, AppImage,
    OpenXR, source) instead of recipe/patches, `C.missing_libraries` callout (Frame deployment, else last install);
    no Analyze/Rebuild/recipe/share/Game settings actions, Frame only; platform "Linux" badge + library filter; Steam
    tags "Linux app on Frame"/"Linux"; `_follow_catalog` skips them; local files of a lone AppImage = the file only.
    Desktop Mode entries (GitHub #84, agent v63): finalize_linux writes `frameport-<slug>.desktop` to
    ~/.local/share/applications (+ ~/Desktop if it exists; `X-FramePort-Package` marks ours), launch.sh with
    `FRAMEPORT_DESKTOP=1` skips the Steam-parent watchdog and Steam's display; ensure_host_fixes refreshes entries
    (older installs, stale ones removed), uninstall/purge remove them. Per app: library entry field `desktop_entry`
    (default on; patches don't apply to Linux apps) → game page switch → agent `desktop_entry`. Untested on device.
  - Quest/Rift twins stay separate entries, shown and named in Steam "Title (Quest)"/"(Rift)" (`core/titles.py`).
  - `Recipe.as_is` = install unchanged (pre-patched libraries): `pipeline.prepare_as_is`; auto for APKs that already
    contain FrameBridge (`frame_patched`). For Rift it changes nothing (the dump is never modified; the Frame copy
    still gets launch fixes like the crash-reporter rename — `-nocrashreports` alone doesn't stop UE 4.23) — it does
    **not** turn Revive off: a repack's patches (cracks, `VD.bat` = Virtual Desktop's Oculus
    runtime) give a LibOVR game no Oculus runtime on the Frame; Revive is that runtime (Lone Echo without it: EXITED
    in seconds). SteamVR builds (Rick and Morty) turn Revive off via their recipe (`pcvr_remove`, catalog `as_is`).
  - Play: Library hover button / right-click / game page / Frame rows → agent `launch` = `steam://rungameid/<appid<<32
    | 0x02000000>` through the Frame's Steam (in-headset session); PC: Windows Steam. Launch tests use a
    flask icon (SCIENCE_OUTLINED) so they aren't confused with Play.
  - `uninstall.py` + `frameport uninstall-app` + Settings: removes the data dir (after an optional key backup zip),
    PC Steam shortcuts, the WSL Revive copy, and via agent `purge` FramePort's games/files on the Frame.
  - `patches/` — **the unit of modularity**. `base.py` (Patch interface, registry), `overport.py` (overport CLI patch ids,
    discovered dynamically via `overport patches`), `frame/*.py` (one module per Frame fix), `settings.py` (FrameBridge
    adapter keys + device files as patches). Add a patch = add a module that calls `register(...)`.
    `upstream.py`: upstream fixes that replace a workaround per build (a probe finds the fix in OVRPort's output →
    the build leaves the workaround out, `build.superseded`; recipes unchanged). Registered: `ovrport.haptic_envelope`
    (→ `adapter.haptic_fix`, `frame/haptic_envelope.py`) and `ovrport.microphone_stream` (→ `frame.ovr_microphone`),
    both fixed in OVRPort runtime 3.4.3-aa54c3f (ovrport/app#73; haptics owner-verified with Lucky's Tale 2026-10-07).
  - `analysis/` — APK/ELF inspection (`detect.py`), `elf.py` (pyelftools reads; own DT_NEEDED writer), `stubgen.py`
    (generates the ovr_* stub .so without a compiler).
  - `apk/` — `axml.py` (binary manifest editor), `workspace.py` (staged zip edits), `sign.py` (apksigner; it aligns too).
  - `recommend/` — `catalog.py` (known-good recipes: user > remote `FRAMEPORT_CATALOG_URL` > bundled), `engine.py`.
  - `tools/` — portable toolchain (Temurin JRE, overport jar, apksigner) downloaded dynamically into the user data dir.
    The overport CLI comes from the downstream fork **Android-XR-Bridge/OVRPort** (stable `vX.Y.Z` releases,
    `OVRPort-<ver>-stable-cli.jar`; fallback ovrport/app `cli-jar.zip`), see "overport" below.
  - `frame/` — SSH (paramiko), mDNS discovery, pairing server; `install/installer.py`; `validate/` (static, device, triage).
  - `targets/` — `Target` interface; `frame_lepton.py` (Quest via Lepton + Rift via Proton), `pc_revive.py` (Rift games
    on this Windows/WSL PC via Revive + local Steam shortcut; `core/winhost.py` = Windows/WSL helpers).
  - Oculus Rift (PC VR): `sources/rift_dump.py`, `analysis/rift.py` (own PE reader), `patches/pcvr.py` (category
    `pcvr`, shown as patches; `base.for_game` separates Quest/PC VR patches), `tools/revive.py` (Revive: FRAMEPORT_REVIVE_DIR >
    the user's installed Revive (C:\Program Files\Revive, else registry HKLM/HKCU\Software\Revive; version = GitHub
    release matching the DLL build date, since Revive's version resources are stale) > portable copy unpacked from
    ReviveInstaller.exe in pure Python; never replaces the user's install). Library ids `rift.<slug>`, entries have `kind: rift`.
  - `parity.py` — rebuild catalog games from dumps and classify every APK entry difference vs known-good builds.
  - `diag/` — user feedback without tokens (docs/DIAGNOSTICS.md): `redact.py` (every file/issue text: IPs, hosts,
    home dirs, Steam ids, dump folders → placeholders), `bundle.py` (redacted diagnostics zip; agent v21
    `collect_diag`; `frameport diag collect|inspect|report`), `issue.py` (prefilled GitHub issue-form links, ≤7.5k
    chars). "Share working config" → `working-config.yml` issue → maintainer label `catalog-accepted` →
    `catalog-from-issue.yml` workflow (`scripts/catalog_from_issue.py` validates) opens a catalog PR. App log:
    `core/applog.py` (`<data>/logs/app.log`, finished GUI jobs in `<data>/logs/jobs/`).
  - Self-update (docs/ARCHITECTURE.md "Self-update", docs/INSTALL.md "Updating"): `_version.py` = the only version
    (`frameport.__version__`; pyproject reads it via hatch `dynamic`; app log, diagnostics, User-Agent, Settings use
    it — never `importlib.metadata`, bundles have no dist-info). `updates.py` (no Flet): `check()` = GitHub
    releases/latest via `cached_json("app-release.json")`, 6 h, skips drafts/prereleases and the user's skipped version
    (settings `update.last_check`/`skipped`/`auto_check`/`auto_install`/`cli_hint`; env `FRAMEPORT_NO_UPDATE_CHECK`);
    `install_kind()` = `bundle` (psutil exe path → FramePort.exe's folder / FramePort.app / FramePort/FramePort),
    `source` (`.git` checkout: `git pull --ff-only` + `uv sync`, refused on a dirty tree), `wheel` (`uv tool`/pipx/pip
    reinstall of the release's `.whl`); `prepare()` → `<data>/updates/<ver>/` (SHA256SUMS.txt required + checked,
    layout checked, Windows: the new FramePort.exe must have the running exe's Authenticode signer when that one is
    signed, `ready.json`); `apply()` writes + starts detached `apply.ps1`/`apply.sh` (wait for pid → Windows: copy over,
    replaced files kept in `updates/<ver>/previous` because the zip has no top folder; macOS/Linux: mv to `.old` + swap,
    rollback, `xattr -dr` quarantine → relaunch; `<data>/logs/update.log`). `ui/updater.py`: background check (10 s,
    then 6 h), sidebar card (hidden control, toggled), Library bar (`library_bar`), notes dialog (`ft.Markdown`), job
    `app-update` that restarts only when no other job is active, `apply_pending_at_start()` in `main()` for
    "Install updates automatically"; Settings → Updates. CLI: `--version`, `frameport update [--check (exit 10)]
    [--yes]`, once-a-day stderr hint read from the cache only (`refresh_cache()` in a daemon thread writes no
    settings, to avoid read-modify-write races). `scripts/update_smoke.py <archive>` runs the real extract + swap script
    (no relaunch) — CI runs it on all three OS with the archive it just built.
    Verified end to end (2026-10-02): real 0.3.3 bundles on Windows and Linux (Ubuntu 22.04/WSLg) found, staged and
    installed 0.3.4 ("Install updates automatically" path) and relaunched as 0.3.4 with settings kept. Not yet clicked
    by hand: the "Update now" button path (same apply(), called from the running app). macOS: CI smoke only.
- `agent/frameport_agent.py` — runs **on the Frame** (python3 stdlib only), JSON over SSH. Owns the install layout,
  launch.sh template, Steam shortcuts (binary VDF), launch tests. Bump `AGENT_VERSION` when changing it.
- `bootstrap/bootstrap.sh` — one-time Frame setup served by the pairing server (sshd, app key, avahi service, Lepton).
- `catalog/games/<package>.yaml` (installed apps also fetch these from GitHub `main`, see "Catalog updates") — 38 recipes (34 verified 2026-09-28; Deadpool VR, 4XVR, NEX Player and AC Nexus's
  90 Hz default confirmed later by the owner); `catalog/triage.yaml` — log signatures → fixes.
- `native/` — sources of the prebuilt binaries in `artifacts/` (adapter, VrApi bridge patches, GL shim, stubs).
  `native/build.py` rebuilds them with NDK r27c (downloaded on demand into `native/.cache`, git-ignored; uses
  `-ffile-prefix-map` so no local paths get embedded; zip symlinks are restored as copies). Users never need the NDK.
- `scripts/` — `package.py` (flet build/pack), `eval_heuristics.py` (score heuristics), `ui_smoke.py` (GUI screenshots).
- `docs/` — PLAYBOOK, FRAME_RUNTIME, ARCHITECTURE, INSTALL (end users), parity reports (offline + device).

## Dev commands
```
# the dev venv is .venv in the repo root (git-ignored); on the NTFS drive uv needs UV_LINK_MODE=copy
UV_LINK_MODE=copy uv sync --extra dev   # creates/updates ./.venv
uv run pytest                # unit tests (no device, no game files)
uv run frameport --help      # CLI;  uv run frameport-gui  for the GUI
uv run frameport parity --known-good <PATCHED/_known-good-*> --sources "<folder with the game dumps>"
```
Games tests: `pytest -m games` (FRAMEPORT_GAMES=<downloads dir>); on-device checks are CLI commands (below);
native layer test: `FRAMEPORT_NATIVE_TESTS=1 pytest -m native` (compiles with the NDK, ~2 min on NTFS).
Repo is on an NTFS drive (`core.fileMode=false`); line endings are LF (`.gitattributes`).
- `FRAMEPORT_HOME=<dir>` isolates all app data (tests use it); `FRAMEPORT_JAVA/_OVERPORT_JAR/_APKSIGNER_JAR` override
  managed tools. Real app data (WSL): `~/.local/share/frameport` (tools, overport workspace + game keystores, library,
  artwork, frames.json, the app's SSH key which the dev Frame authorizes).
- Device checks: `frameport test <pkg>` / `frameport parity-device --results <parity.json> --baseline <launch.txt>
  [--test-only]`; the pre-FramePort baseline is `PATCHED/_known-good-2026-09-28/_frame-state/baseline-launch.txt`.
- Docs screenshots (`docs/images/`): `python scripts/scrub_library.py ~/.local/share/frameport <dir> --status works,issues` (copies
  library + artwork only; titles replace folder names, local paths → `D:/Games/...`, sort by size) then
  `FRAMEPORT_HOME=<dir> python scripts/ui_smoke.py --out <shots> --docs --fake-frame --game <pkg>` (set
  FRAMEPORT_JAVA/_OVERPORT_JAR/_APKSIGNER_JAR so no tool download toast appears; `--viewport 1280x2600` + crop for the
  patch list). Check every PNG for paths, IPs, user names and repack/scene names before committing.
  README rules (owner): states the project is a proof of concept, provides no piracy tools, credits the wrapped
  projects (most functionality is theirs); neutral technical wording; no Quest2Frame mentions anywhere.
- GUI smoke test: `uv pip install flet-web playwright && playwright install chromium`, then
  `FRAMEPORT_HOME=<test dir> python scripts/ui_smoke.py --out <dir> [--game <pkg>] [--frame steamos@<host>] [--update]` (`--update` = fake release: banner, dialog, Settings → Updates) and look
  at the PNGs. Flet 1.0 notes: `ft.run` must own the main thread; background work via `page.run_thread`; FilePicker is
  awaited (`await ft.FilePicker().get_directory_path()`); dialogs via `page.show_dialog/pop_dialog`; running from
  source needs `flet-desktop` (declared) and web mode needs `flet-web`.
- GUI scale: `theme.set_scale()` (library setting `ui.scale`, "auto" = halfway to Windows' AppliedDPI/96 under WSL (150 % → 125 %; full was too large), where the
  Linux window gets no Windows scaling; Settings → Appearance; applied at start). Sizes go through tokens or
  `T.px(n)`, Material defaults through the theme's text_theme; never hardcode a bare pixel number in ui/.
  `scripts/ui_smoke.py --scale 1.5 --viewport 2560x1440` renders it. Flet draws a grey box for invalid layouts (e.g.
  an `expand` child in a `wrap=True` Row).
- Stopping the GUI: `pkill -f` patterns match your own shell — use `pgrep -f "[b]in/frameport-gui|[f]let-desktop-light"`.

## Round-2 polish (2026-10-02)
- `errors.py`: `explain(exc)` (plain sentence for GUI + CLI), `is_connection_error`. Install/test jobs for the Frame
  (`Job.needs_frame`) that lose the connection go back to the queue front and the queue pauses (`jobs.paused`);
  `app._poll` retries every 10 s and resumes (owner-verified on the device 2026-10-02: the queue resumed after the
  connection was lost). Wake lock: agent v30 `keep_awake` (systemd-inhibit idle:sleep, idle-only
  fallback because polkit `inhibit-block-sleep` is auth_admin for non-local sessions), held while Frame jobs exist.
- **Already converted inputs** (`analysis.is_overport_output`, e.g. the owner's library points at `PATCHED/` copies)
  are not converted again: a second OVRPort run replaced libovrplatformloader.so and dropped its DT_NEEDED on
  libovrstubs.so (Wallace & Gromit / Espire 2 crashed: cannot locate symbol ovr_…). Alt builds come from the saved
  `<pkg>.alt-noforcequit.apk`, else one OVRPort run (frame.ovrstubs relinks). `missing_ovr_symbols` only counts
  linked stub/compat libs. Converted copies in `output/` are removed after a Frame install (`build.keep_copies`).
- Art: `.picked` marker = user's pick, automatic fetches never overwrite it and only fill missing kinds; a Meta
  result's picture is the OculusDB image shown in the picker (the ovrp image service served "dogfooding" placeholder
  covers for some packages, e.g. Asgard's Wrath 2). `steam_art_stale` → game page reminder. No store art → generated
  `cover.jpg`/`banner.jpg` (APK icon via `fetch.apk_icon`).
- Game settings dialog (`ui/views/adapter_dialog.py`, metadata `patches/settings.UI`); plain patch summaries
  (`patches/summaries.py`, `Patch.summary`, "Show technical details" = setting `ui.patch_details`); share nudge
  (`views/game.should_ask_to_share`); saving a recipe keeps its status (was always "works").

## Hard-won facts (don't re-learn these)
**Frame runtime (SteamOS 0.3.0, build 20260922):**
- No AArch32: 32-bit-only APKs fail with `INSTALL_FAILED_NO_MATCHING_ABIS`. Unfixable; point to Rift + Revive.
- GLES swapchains: only `GL_SRGB8_ALPHA8`/`GL_SRGB8` (35907/35905), no MSAA. Vulkan: format 43 (sRGB) but not 37 (UNORM).
- Missing: XR_FB_passthrough (emulate via ALPHA_BLEND), XR_FB_scene/spatial entities (emulated room from STAGE bounds),
  XR_KHR_composition_layer_equirect2/cylinder, XR_FB_composition_layer_image_layout (flip emulated by Vulkan blit).
- `xrConvertTimespecTimeToTimeKHR`/`xrConvertTimeToTimespecTimeKHR` return FUNCTION_UNSUPPORTED → adapter emulates
  (offset = predictedDisplayTime − period − CLOCK_MONOTONIC, sampled in xrWaitFrame).
- Guardian STAGE bounds report 1×1 m (scene emulation uses ≥1.5 m).
- GL goes through **Zink** (Mesa GL on Vulkan). Mesa GLSL is strict: `#pragma` before `#extension` fails, implicit
  int/float conversions fail (enable `GL_EXT_shader_implicit_conversions`), num_views=2 shaders on single-view FBOs
  give GL_INVALID_OPERATION. Some GLES games hit `zink: DEVICE LOST` (Unity MSAA RTT; Sniper Elite VR even without).
- Valve injects `VALVE_rpo`/`VALVE_fdm_injection` Vulkan layers (via VK_INSTANCE_LAYERS).
- **Tracking only works with the headset worn.** SSH/headless launches never reach VISIBLE/FOCUSED and poses have
  flags 0x3. So automated tests prove startup (process alive, instance/session created, frames paced), never visuals.

**Setup / pairing (2026-10-02, verified on the device):** Developer Mode = `"DevModeEnabled"` in
`~/.local/share/Steam/config/config.vdf` (InstallConfigStore/developer), applied by Valve's
`/usr/bin/steamos-polkit-helpers/steamos-devkit-mode --enable|--disable` (polkit allow_any: no password; enables/
disables sshd, xrdp, steamos-devkit-service, debug port forwards; sentinel `/etc/steamos-devkit-enabled`). Steam
re-asserts the config value at every start and rewrites config.vdf on exit → stop Steam, edit, helper, start.
**Desktop Mode is a nested Plasma inside steam.service** (own XDG_RUNTIME_DIR `/run/user/1000/nested_plasma` + private
D-Bus): `systemd-run --user` from Konsole fails ("Failed to connect to user scope bus") unless XDG_RUNTIME_DIR/
DBUS_SESSION_BUS_ADDRESS point at `/run/user/$UID`, and stopping Steam ends the desktop and everything started in
it → bootstrap.sh runs the Dev Mode job + Lepton request + `/paired` as a user unit (log `~/.cache/frameport-setup.log`);
no sudo/password anywhere (verified: naive Frame → connected, password never set). Valve's devkit pairing (fallback,
PC → Frame only): `POST :32000/register` with an **ssh-rsa** key (`connection.devkit_key`, `frame/devkit.py`) works only
while Steam is in pairing mode (Settings → Developer → Pair new host), else 403 at once; approve hook waits 30 s.
The Frame runs firewalld (22 and 32000 open). PC side: the setup server needs inbound TCP 8765–8767 — WSL's Hyper-V
firewall blocks it silently (`DefaultInboundAction Block`): `pairing.ensure_reachable` adds a temporary rule via one
UAC prompt, removed when the server stops (flag file in %TEMP%, max 35 min); hints per OS after 45 s without a request
(`pairing.firewall_hint`). Flet 1.0 patches aren't thread-safe → `app.serialize_flet_updates()` (a dialog shown while a
scan redraw ran never closed: "dropped a patch for unknown control").
**Lepton:** needs an activity with category **LAUNCHER** (Quest apps often only have INFO → "APP_ACTIVITY is empty").
**2D apps:** Lepton runs every app headless (`lepton.headless=true`, only OpenXR output reaches the headset) unless the app folder (`<base>/lepton-app/`) has a `lepton-show-flatscreen` file (liblepton/app_metadata.sh) → Waydroid window on gamescope; agent v28 `set_flatscreen` at finalize for `vr_kind == "none"`. Android 11's navbar covered the
app's controls → patch `device.hide_navbar` (default on for vr_kind none, migration `flat_hide_navbar`) exports
`qemu.hw.mainkeys=1` as a second line of `LEPTON_GFXRECON_FP_PROPS` (runtime alternatives failed, see
docs/FRAME_RUNTIME.md). Verified in the headset 2026-10-02 (2048).
Lepton = Steam app 3029110 (+ "Lepton Development" 3056000, needs Developer Mode). Per-game env: STEAM_COMPAT_INSTALL_PATH
/DATA_PATH/SHADER_PATH, SteamAppId. Logs: `<base>/launch.log` and `~/.local/share/Steam/logs/lepton-logcats/steamlaunch-<appid>`.
Containers are podman `lepton-steamlaunch-<appid>`. Some Unreal games create save dirs without u+rwx → launcher repairs every 2 s.
**Rootless podman leaks one kernel session keyring per container start** (200-key quota per user): after ~200 launches
since boot every game fails with `crun: create keyring …: Disk quota exceeded` / `is not a running context`. Fix:
`keyring = false` in `~/.config/containers/containers.conf` (agent `ensure_host_fixes`, bootstrap); leaked keys only
go away with a reboot. Check usage: `grep "^ *1000:" /proc/key-users` (agent `info` → kernel_keys).

**Proton on the Frame (2026-09-29):** Steam registers ARM64 tools from app "Steam Frame ARM64 Compat List"
(`proton_11-arm64` 4628740 needs SLR4-arm64 4185400; `proton-experimental-arm64` 4427310; `fex` 3127680) — not installed
by default; `steam://install/<id>` only opens a dialog in the headset (agent `install_proton` mode `unattended` =
appmanifest stubs + Steam restart; untested on device). Proton only sets up VR/wineopenxr when `SteamGameId` is set.
Host OpenXR = SteamVR (`~/.config/openxr/1/active_runtime.json`). Steam VR games on the Frame (Pistol Whip etc.) are
APKs via Lepton. Verified on the device: the app installs Proton itself (`installer.ensure_proton`: appmanifest stubs +
Steam restart, ~90 s); Proton runs x86 code; wineopenxr gets registered; headless launches need DISPLAY/
GAMESCOPE_WAYLAND_DISPLAY from Steam's environment (launch.sh imports them). The **Linux** SteamVR runtime supports
XR_KHR_convert_timespec_time (only the Android runtime lacks it). **But it only accepts OpenXR 1.0 apps**: apiVersion
1.1 → XR_ERROR_API_VERSION_UNSUPPORTED, and Proton 11's vrhelper asks for 1.1 → no VR at all (flat window; Revive:
"Unable to load LibOVRRT DLL"). FramePort's layer (`native/xrlayer`, `artifacts/linux-arm64`, patch `pcvr.xr_timefix`,
default on + one-time library migration in `core/library._migrate`) retries as 1.0. Proton's container (pressure-vessel)
**drops `XR_API_LAYER_PATH`**, so the agent registers the layer as an explicit layer in
`~/.local/share/openxr/1/api_layers/explicit.d/` (library: shared copy in `~/.local/share/frameport/xrlayer/`); it
only loads when `XR_ENABLE_API_LAYERS` names it. Unreal PC VR games get
`pcvr.no_crash_reporter` by default (`-nocrashreports` + CrashReportClient.exe renamed `.disabled` in the Frame copy)
and `pcvr.oculus_unreal` (PC VR counterpart of overport's `patch_oculus_unreal`: UE's OculusHMD needs the Windows event
`OculusHMDConnected`; launch.sh wraps the injector in `helpers/fp_oculushmd.exe` = `native/oculushmd`,
`artifacts/win-x64`, which provides it until the game's job is empty; untested on the device yet).
Rift games tried on the Frame: Rick and Morty (SteamVR build, no Revive: "OpenVR initialized!" with the layer; flat
before — confirm in the headset), Lies Beneath (Oculus Store UE 4.23 build: delay-loads `LibOVRPlatform64_1.dll` =
Platform SDK → crash 0xc06d007e; needs the Oculus app, FramePort doesn't replace it; also OVRPlugin's LibOVR shim
reports "Unable to load LibOVRRT DLL" before any OpenXR call — likely needs a real Oculus runtime install, unverified).
Launch tests ignore log files older than the launch (stale Proton/game logs used to be triaged). Revive injector CLI: `ReviveInjector.exe [/openxr] <exe path>`
(joins args; logs to `%LOCALAPPDATA%\Revive\ReviveInjector.txt`). On WSL, run Windows programs from Windows paths
(`\\wsl$` is unreliable), so PC mode copies Revive to `%LOCALAPPDATA%\FramePort`.
FramePort never bypasses Oculus entitlement checks (Platform SDK games: flagged, PC mode with the Oculus app only).

**Upload speed (measured 2026-09-29):** via the home router (PC Wi-Fi → router → Frame wlan0) only 15-18 MB/s, the
same for paramiko, OpenSSH and 4 parallel streams, so the network path is the limit, not the SSH code. Direct to the
Frame's hotspot (`wlanap` 10.35.78.1; the owner's PC has Valve's USB Wi-Fi dongle on it) 83 MB/s OpenSSH / 97 MB/s
paramiko; reading dumps from D: via WSL drvfs is then the cap (~82 MB/s). `Frame.fast_link()` (installer
`transfer_link`, uploads ≥64 MiB) uses usb0 > wlanap when reachable **and** the host key matches the paired Frame.
**Platform SDK detection** must include delay imports (`PEInfo.delay_imports`), modular Unreal's
`*-OnlineSubsystemOculus-*.dll` and Ready At Dawn's `pnsovr.dll`: Robo Recall, Lies Beneath and Lone Echo I/II all use
it; without the Oculus app they crash with 0xc06d007e (triage `delayload-missing`) — not fixable legitimately.
GUI: Frame → Installed → folder icon = file browser (agent `list_files`, flags files missing vs the install
manifest); Settings → About shows the bundled agent version and the Frame's.

**Revive under Proton arm64 (Lone Echo, 2026-09-29):** Revive's Detours hooks (LoadLibraryW/ExW, OpenEventW, the
signature check) don't take effect: Wine's kernelbase is ARM64EC. The LibOVR shim in the game then does a plain
`LoadLibrary("LibOVRRT64_1.dll")` search (exe dir, cwd, system32, windows, PATH — no registry, `OculusBase` or
`LIBOVR_DLL_DIR` used by this build) → `Failed to initialize Oculus API (-3001)` (= Lies Beneath's "Unable to load
LibOVRRT DLL"). A symlink `pfx/drive_c/windows/system32/LibOVRRT64_1.dll -> <base>/revive/LibReviveXR64.dll` (Wine
maps a symlink to the already-loaded module) gets past it to **-3021 = ovrError_LibSignCheck**: the shim only accepts
an Oculus-signed runtime. Next step (not done): make the runtime check pass without Detours, e.g. an injected helper
that patches the game's import table (IAT) for LoadLibrary*/WinVerifyTrust, or a prefix-level override. This is
runtime interop (what Revive does), not the Platform SDK licence check (which FramePort never touches).
**Rift via x86_64 Wine under FEX (research, 2026-10-08, headless on the dev Frame, SteamOS 0.4.5; no product code):**
why Revive failed is confirmed upstream: x64 Detours returns ERROR_NOT_SUPPORTED on ARM64EC targets (microsoft/Detours
PR #388; only an ARM64EC build of Detours can hook them). A fully x86_64 Wine avoids it: GE-Proton11-7 **x86_64** runs
under the FEX tool (`fex-compat-tool run -- <x86 binary>`; RootFS /usr/share/guestos/fex-mesa, no FreeType there)
— prefix boot 35 s, kernelbase.dll is x86-64. Proton's script needs help without the x86_64 SLR (app 4183110, not
installed): its Python runs natively (the RootFS has none), so `files/bin/wine{,server}` and the two direct
`wine-preloader` argv calls must go through a FEX wrapper that also unsets `FEX_APP_CONFIG(_LOCATION)` (Proton sets
them on aarch64 for its own ARM64EC FEX → "RootFS path set to ''"); GE's game drive needs STEAM_COMPAT_INSTALL_PATH
unset. SteamVR now ships `bin/linux64/vrclient.so` + `bin/vrclient.so` (i386) + `vrclient_x64.dll` (Oct 6 build; not
there on 09-30): Proton's x86_64 vrclient bridge reaches the arm64 vrserver. **Rick and Morty ran as the scene app**
("OpenVR initialized!", controller tracking, 9176 presents / 6 dropped, CPU 11.6 / GPU 17.9 ms, target 72).
**ReviveInjector: "Succesfully injected!"**, LibRevive64.dll loaded next to OVRPlugin.dll in Lies Beneath, which then
died on 0xc06d007e = delay-loaded `libovrplatform64_1.dll` (Platform SDK) before connecting to SteamVR — so Revive's
signature hook and Revive→SteamVR are still unproven. Every Rift game in the library except Rick and Morty uses the
Platform SDK → the Meta (Link) app in the prefix, logged in with the user's own account, is the real blocker; the
only public attempt (github.com/michauMiau/oculus-wine-linux, 2026-10) never got OculusSetup.exe (32-bit .NET)
past its HTTPS config fetch. Scratch files on the Frame: `~/frameport-rifttest/` (scripts t1-t4.sh).
Meta runtime in the same Wine (2026-10-09): Platform SDK games reach OVRServer's IPC on the Frame. The apparent
intermittent handshake hang was **OVRLibrarian.exe** (.NET, Wine Mono): under FEX it finishes its work but never exits,
its AppTracker entry stays, and the runtime then tracks no later client (Meta app, game), so the game waits forever
for the reply event the runtime only sets after identifying it (found with wineserver `+server` traces PC vs Frame).
Ending OVRLibrarian before the game → game tracked, "Missing entitlement" exactly as on the PC (needs a login).
Logged in (2026-10-09, owner's own account, browser sign-in; the `oculus://` answer had to be forwarded into the Wine
app's `\\.\pipe\oculus`), the runtime also needed a WinRT `Windows.Devices.WiFi.WiFiAdapter` stand-in (Air Link code
fail-fasts without it), Wine's DeviceWatcher add/remove_Updated/Removed implemented, and the C: volume name
(`\\?\Volume{...0043}`) linked in dosdevices. Copying the logged-in `sessions/` + CoreData to the Frame prefix kept the
login. **Oculus First Contact then ran in VR on the Frame** (Revive in OpenVR mode; `/openxr` failed in wineopenxr):
scene app at 72 Hz target, but 92 % of frames "timed out" (game CPU-bound under FEX) → stutter/flicker/lag.
Performance (2026-10-10, headless with patched Revive visibility + SteamVR `pauseCompositorOnStandby=false`, numbers from
`IVRCompositor::GetFrameTimings`): the frame loop waited on sync round trips through the FEX-run x86_64 wineserver.
`native/fexwine/`: a **native aarch64 wineserver** built from GE's exact source (protocol 938) + **ntsync** (works once
OVRLibrarian is ended) took hitches 17 → 0-4 per 20 s. The rest were Unreal's large-block allocator committing,
filling (one `rep stos`) and freeing 514 MB every 1-8 s: ~130k 4 KB page faults each under FEX. `fp_mem.so`
(`FP_BIGCACHE_MB=256`, guest preload via `FEX_ENV=LD_PRELOAD=…`: fex-compat-tool deletes LD_PRELOAD) keeps that block
→ 0 hitches, steady 36 fps + motion smoothing, ~5.5 % timed out. 72 fps would need ~30 % less GPU (game ~55 % busy
at 36 fps); THP (fragmentation), MSAA off, Turnip gmem don't help. Also needed: Unreal's
`Slate.DeferWindowsMessageProcessing` = 0 (exe patch; the default deadlocks Wine's IME window ~1/3 of starts).
Uninstall (Quest, saves kept) used to leave `deployment.json` → still "installed"; fixed (agent v16).

**Discovery/network:** Developer-Mode SteamOS devices announce `_steamos-devkit._tcp` (TXT `login=steamos`) — use it;
they don't publish `_ssh._tcp`. The Frame has several links: `wlan0` (home Wi-Fi), `wlanap` = its own hotspot at
10.35.78.1/24 (a PC can join it directly), `usb0` = USB gadget network 10.86.200.233/29 (up when cabled to a PC).
The dev PC runs WSL2 in mirrored networking mode (mDNS works). Beware `pkill -f <pattern>` killing your own shell.

**Steam:** shortcut appid = crc32('"<anchor>/launch.sh"' + title) | 0x80000000; shortcuts.vdf is only read at Steam
start, so Steam must be stopped while writing it. Terminals/SSH started from Steam live in steam.service's cgroup —
stopping Steam kills them → always run that work via `systemd-run --user` (the agent does). Artwork goes to
`userdata/<id>/config/grid/<appid>{p,,_hero,_logo}.<ext>`.

**overport:** always `--version=latest`; `--workspace` holds runtimes and **per-package keystores (password
"password", alias "key") — never lose them**: updates must be signed with the same key or saves are lost on reinstall.
Output is deterministic (same input + runtime → same bytes), which is what makes parity testing possible.
**OVRPort 1.2.5 (2026-10-01, the fork's first release; CLI-only):** same commands (`patches [--json]`, `patch`, `help`,
`install`), but `patch` rejects unknown/duplicate args, unknown patch ids and an empty `--patches=`
(`tools/overport.patch` refuses empty lists). After patching it adds `libovrplatformcompat.so` itself when the platform
loader lacks `ovrMessageType_ToString` (our `frame.ovrplatformcompat` then skips: same library). New patches (all off;
`patches/overport.py`): `patch_ac_nexus_no_appsw_72/_90` (AC Nexus build 207706 only, exclusive),
`patch_disable_meta_xr_audio_telemetry` (x86_64 emulators: hidden), `patch_vrapi_openxr` = OVRPort's VrApi adapter =
the **unpatched** upstream of our `frame.vrapi_bridge` (`native/vrapi` unchanged since our 5e7df52), only usable with an
experimental CLI built with `-PwithVrApi=true` (stable jars list it but fail). The owner prefers OVRPort's fixes over
ours where they work as well (less to maintain): compare in the headset before switching a default.
`frameport install <pkg> --apk <file>` installs a specific (test) build signed with the game's key.
Headset results (2026-10-02, `PATCHED/_test-ovrport-1.2.5/TESTING.md`): OVRPort's own VrApi translator fails on the
Frame (Climb 2: requests VkFormat 37 → crash; POTW: no GLES path, missing `vrapi_GetTextureSwapChainHandle`) → keep
`frame.vrapi_bridge`; those two changes are upstream candidates. 1.2.5 builds (its platform compat, new permissions)
work. AC Nexus: `patch_ac_nexus_no_appsw_90` is the catalog default (owner preferred 90 Hz); `STRICT` patches are only
taken from a catalog recipe where `applies()` holds (the AC Nexus ones need build MAIN.450412.207706.final, else the
whole overport run fails).

**Patching gotchas:**
- UnityPy re-serialization breaks scene loading → patch QualitySettings ints in place.
- LIEF's DT_NEEDED injection shifts segments; our `elf.add_needed` appends a new PT_LOAD (reuses PT_NOTE, else moves the
  phdr table + adds PT_PHDR) and leaves existing bytes untouched. Bionic requires section headers and matching .dynamic.
- apksigner 37 aligns (4 B / .so 16 KiB) itself — no zipalign needed.
- Titles from aapt with apostrophes got truncated once; we read labels with pyaxmlparser and store titles from the
  overport image API (`https://ovrp.crx.moe/images/by_package?package=`), which also serves Steam artwork.
- GLAD engines fetch all GL via eglGetProcAddress → wrap it (GL shim) to fix/trace shaders; that's how POTW was solved.
- VrApi-direct engines (CryEngine Climb 2, POTW) need the VrApi bridge; the bridge drops whole frames on unknown layer
  types (→ black screen with audio).

**Steam Frame controller models (2026-09-30, not yet seen in a game):** adapter setting `controller_models`
(`native/adapter/render_model.c`) emulates XR_FB_render_model and serves `files/framebridge/controller_{left,right}.glb`;
agent v20 (`install_controller_models`, command `controller_models`) converts the Frame's SteamVR render models
(folder name matching "frame" + left/right, OBJ+PNG, `openxr_grip` component → grip space) at finalize/set_settings.
Valve's models are never committed or copied off the Frame. Only games that use Meta's runtime controller models
benefit (manifest `RENDER_MODEL` permission/feature → suggested); **none of the 34 catalog games do** (they ship their
own meshes: that would need per-game asset replacement). Model discovery and conversion
checked on the device 2026-09-30: `/opt/steamvr/drivers/frame_controller/resources/rendermodels/frame_controller_{left,right}`
(component OBJs in model space = the whole `<name>.obj`, one `_color.png` 2048² near-black, `openxr_grip` rotates about
X only; hidden-by-default components like `status` are skipped) → ~2 MB glb each, ~1 s. Not yet seen in a game (none
of the installed builds has the new adapter, and none requests runtime models), so whether Meta's SDK attaches runtime models at the grip pose is unverified.
**overport's dispatcher (`libopenxr_loader.so`) only forwards functions in its own table** (`overportOXR: Unknown
proc addr: …`), so adapter emulations of functions it doesn't know are unreachable. For XR_FB_render_model,
`native/xrshim` fills the gap: OVRPlugin's `dlopen("libopenxr_loader.so")` string is rewritten to the shim (see
native/README). Verified on the device with Toy Master (2026-09-30): `extension shim: xrLoadRenderModelFB -> FrameBridge`.
Toy Master doesn't request a model after that (it uses its own), so the glb loading path is only unit-tested.

**Crash backtraces (2026-09-30):** Lepton writes tombstones only to `lepton-logcats/steamlaunch-<appid>/logcat-crash.log` (after "Dumping logcat"), not launch.log; agent v23 launch tests wait for it and return `crash_log`, triage gets it via `triage(..., crash=)` (not pid-filtered: tombstones come from crash_dump's pid). The guest is `userdebug` (`ro.debuggable=1`), so Lepton's Fossilize layer loads into every app whatever the APK's debuggable flag; it has no off switch. Fossilize crashed Deadpool VR on uninitialized attachment-reference pNexts (several, in FVulkanRenderPass and the render-target layout) → `frame.vk_sanitize` = `native/vkshim` (engine's `dlopen("libvulkan.so")` string → `libfp_vk.so`, wraps vkCreateRenderPass2/KHR, drops unreadable/wrong-sType pNexts; verified: game runs). overport's dispatcher aborts on swapchains > 4096 px (`cmp wN,#4096` in xrCreateSwapchain) → `frame.swapchain_limit` (verified with 4XVR, 7680×3840 accepted by the runtime). Both are default-on (parity classifies their rewrites as
"expected"; library migration `quest_binary_fixes_v2`). The adapter shows cylinder layers (4XVR's movie screen) as ~15° quad strips (`cylinder_strips`, setting
`cylinder_strips`); the runtime composites them at full resolution. Equirect (360°) layers are dropped: drawing
them ourselves (GLES renderer in xrEndFrame: stutter, never showed 4XVR's VR video; Vulkan renderer: hung the
Frame's GPU in AC Nexus; reading the newest *released* image broke 4XVR's theatre) was removed again on the owner's
request — don't retry without a new approach. Converted layers must count as "swapped" or the original layer list
is submitted (runtime returns -2 for every frame). A focus debounce (hiding the Frame's brief
FOCUSED→VISIBLE→SYNCHRONIZED dips, which make 4XVR recenter) was also removed: AC Nexus stayed black with it (it
hid 521 ms focus changes at start). The new approach (below) is per-game, off the app's render thread, GLES-only.

**360° layers / 4XVR (2026-09-30):** the Frame's Android SteamVR runtime (`/opt/steamvr/bin/androidarm64/vrclient.so`,
mounted in Lepton as `/data/steamvr/runtime`) only composites quad + projection layers (cube/cylinder/equirect(2)
are enum names only) — nothing to unlock. Per-game adapter settings (off by default, hooks installed only when on):
`equirect_emul` (GLES only: a worker thread with a shared EGL context converts each 360° image to a cube map when it
changes and draws one adapter projection layer from it for every frame with exactly that frame's views (the app's own
projection views, else xrLocateViews at displayTime); xrEndFrame waits ≤6 ms for the worker's CPU submit, never the
GPU. Learned in the headset: quads can't be a background (the Frame draws quad layers above all projection layers
whatever the order: hid 4XVR's balcony/controllers), and the Frame doesn't reproject a projection layer from its own
pose (images drawn only after head turns, or ahead from xrWaitFrame and sometimes late, wobbled);
`equirect_flip/face/res/fps/stereo`), `stable_local`, `focus_hold` (only after 3 s FOCUSED, dips <600 ms),
`aim_pitch/aim_yaw/aim_forward`, `refresh_rate`, `layer_debug` (diagnostics); focus_hold is default-on since 2026-10-05. 4XVR re-creates LOCAL spaces every 2–4 s
(menu recentring suspect); its theatres are baked 7680×3840 equirect2 images (`assets/100.png` …). Test 360° videos
(NASA, public domain) are in the Frame's ~/Videos; copies in `~/Downloads/frameport-360`. 4XVR's "Internal Storage"
lists its own `/sdcard/4XPlayer`, not Movies: agent v25 `link_media` hard-links sent files into an app's own top-level
folder (`app_media_dirs`), `frameport frame send --app <pkg>`, GUI game page "Add videos" (players) / "Add videos &
files…". A 4XVR webm stereo swapchain was refused with XR_ERROR_LIMIT_REACHED (-10) → right eye grey; our projection
swapchain was halved (1536/eye) in case memory is the limit (unverified). Frame data snapshot
(SteamVR runtimes, Lepton scripts, logs; never commit Valve binaries): `~/frameport-research/frame-data-2026-09-30/`.
Not yet verified in the headset.

**Companion apps / intents (2026-10-03, tested with Stremio + 4XVR):** a second APK can be installed into a running
Lepton container (`podman exec -i lepton-steamlaunch-<appid> pm install -g -S <size> < apk`) and runs there, VR
included (4XVR in Stremio's flatscreen container: FrameBridge 72 fps, settings via LEPTON_ENV_FRAMEBRIDGE_CONFIG). But
Lepton's services.jar (`ActivityStarter.execute`) intercepts **every** `android.intent.action.VIEW` with data (any
scheme, explicit component or not, no property to disable): it writes `steam://openurl/<uri>` to `/lepton/steam.pipe`
(= the host Steam client's `~/.steam/steam.pipe`) and starts nothing. So "open in external player" hand-offs (Stremio
→ 4XVR) can't work inside Lepton; don't retry without Valve changing it. Lepton installs exactly one `*.apk` per app
folder (two break `get_apk_path`).
**Text input (2026-10-03, verified in the headset with Stremio VR):** Lepton's Android has no IME (`ime list` empty)
and runs VR apps headless (`lepton.headless=true`): no Android window has input focus (`dumpsys input` FocusedWindows
empty), so neither `input text`, a USB keyboard nor Steam's keyboard reach the app. `lepton-show-flatscreen` on a VR
app keeps VR working (FrameBridge 72 fps) and gives the window focus; Steam's on-screen keyboard then opens for text
fields. Unity's TMP_InputField on non-Quest Android waits for the system keyboard (`TouchScreenKeyboardShouldBeUsed`)
and deselects a frame later unless `isKeyboardUsingEvents` (Android: `InPlaceEditing() && m_HideSoftKeyboard`);
uGUI InputField's LateUpdate keeps the field when `InPlaceEditing()`. Patch `frame.unity_text_input` rewrites them
(`mov w0,#0|#1; ret`) at Cpp2IL's **Offset** (= file offset; RVA differs by 0x4000 in Stremio's lib), Cpp2IL
2022.1 pre-release (Unity 6 / metadata v31; Il2CppDumper can't), cached per libil2cpp sha. `/dev/uinput` has an ACL
for steamos (Steam Input) → agent v33 `_keyboard` uinput keyboard ("Type on Frame") reaches everything with focus.
Installing a second APK into a container with `pm install` re-runs Lepton's post-install hook on the **main** app
(`lepton.active_app_id`; moves its files to /data/steam_app) and corrupts it on the next start (fix: touch the APK →
re-bake); `cmd_real package install` skips the hook.
**User reports (2026-10-04, issues #4-#10):** Steam shortcuts go to the signed-in account (`loginusers.vdf`
MostRecent) else every account (agent v35 `library_users`; it used to refuse with >1 account → Play gave Steam's "Game
configuration unavailable"); `launch` uses the shortcut's own appid and errors `NOT_IN_LIBRARY`, the app then adds it
and plays. Unity IL2CPP fixes share one Cpp2IL run (`unity_text_input.Il2cppReturnPatch`, `ALL_TARGETS`, cache keyed by
global-metadata.dat): `frame.unity_runtime_msaa_off` (OVRManager raises MSAA to 4x at runtime: "Switching to the
recommended level" → Lucky's Tale restarted the headset; `OVRDisplay.get_recommendedMSAALevel` → 0) and
`frame.unity_multipass` (Oculus XR Plugin multiview → MultiPass via `OculusSettings.GetStereoRenderingMode`; I Am Cat's
right eye grey). Both checked against Toy Master's Cpp2IL output, not yet in a headset. VR4 quits itself (System.exit
after the intro movie) → catalog `use_alt`. **Catalog changes reach existing games** without hardcoded migrations:
recipes store `catalog_rev` (= `CatalogEntry.rev()`), `library._follow_catalog` re-derives non-user recipes whose entry
changed; a maintained entry beats the user's shared copy when its `updated`/verified date is later (`catalog._newer`).
**Round 2 of reports (2026-10-04):** VR4's campaign hang = one fragment shader reading an uninitialized loop counter
(reporter's capture): vkshim `vk_shader_fix` setting (`<size>:<sha256>:<offset>:<words>`, from the recipe's `adapter:`;
matched by size + SHA-256, words inserted into a copy) — on the device the module is fixed at load ("fixed shader
module (6488 -> 6512 bytes)"); campaign itself not yet seen in a headset by us. Text settings (kind "str") stay out of
the Game settings dialog (saving it dropped them). I Am Cat: `GetStereoRenderingMode` is inlined (no call sites) →
`Il2cppReturnPatch.field_loads` rewrites the field read in `OculusLoader.Initialize` (Cpp2IL gives the field offset,
`field:<name>`); verified with Toy Master: eye swapchains array=1 ×2 instead of array=2. Builds record `recipe_fp`
(`patches/base.recipe_fingerprint`, `Patch.revision`): a changed recipe or revised patch shows "Update on Frame".
Remembered Frames are found again after an address change (`Frame.relocate`: scan, same SSH host key, before
login). Packaged apps show artwork by file path (`thumbs.use_file_paths`, #16). Diagnostics: this boot's kernel log.
Device tests of requested games (2026-10-04, headless): I Am Cat, Myst 3.3.0, BattleGlide, Blade & Sorcery: Nomad
start (RUNNING, frames). Roblox: SIGSEGV in je_free from libroblox.so ~3 s in (likely its anti-tamper vs the re-signed
APK). BONELAB (Unity 2021.3 Vulkan, OVRPlugin 1.94): SIGSEGV with pc == fault addr in vkCreateInstance, called from
libSLZQuestNative.so's Vulkan hooks into an unmapped (unloaded) library right after OVRPlugin's pre-init
xrDestroyInstance; pinning openxr/vrclient libraries (RTLD_NODELETE) did NOT help — find the unloaded library with the
linker's dlopen/dlclose logging next. Accounting+ (Unity 2017.4 built-in Oculus, libOVRPlugin + libvrapi): Unity
never starts VR on the Frame (no OVRPlugin/OpenXR lines; runs as a 2D app), which is why its "controller setup"
screen can't be passed — Unity 2017's Oculus device check, not the controller profiles. Cpp2IL can't read Unity 2017.
The Frame's Android runtime (vrclient.so) knows oculus/touch_controller but not Meta's Touch Plus/Pro profiles
(OVRPlugin suggests those too, routinely) → adapter `profile_remap`.
Headset round 2 (2026-10-04): the Frame drops focus often (ms to 7 s; only some are presence/standby) → games pause
or recentre (B&S Player.OnVRPresence → Teleport, BattleGlide/Unreal pauses); focus_hold now 1 s focused / 1 s max dip
(longer must still pause). Myst crash = vrclient xrSyncActions race after FOCUSED → `sync_guard` (confirmed); its
object glitches: app space warp suspected (FB_space_warp is advertised and used) → recipe `patch_disable_space_warp`.
I Am Cat multipass: poses/times consistent, but every frame is submitted after the next xrWaitFrame (one period late;
Zink + doubled draws) and the game clamps its physics step to 10 ms → judder while still; testing scale 0.8. Roblox's
newest crash is on Fossilize's recording thread → vk_sanitize extended to own-engine libs (engine "Other").
Accounting+: unity_oculus_check + native/ovrpshim (libfp_ovrp.so waits via ovrp_WaitToBeginFrame before Unity
2017's ovrp_Update2) → VR in the headset at ~72 fps (2026-10-04), but stuck at "press any button" although input
reaches its OVRInput cleanly (probe: both Touch connected 0x63, individual buttons, input focus 1) → works with issues.
"outside of frame bounds" warnings stay (~2/frame) and are harmless.
Steam library (GitHub #4/#21/#27, agent v38): shortcuts go to every Steam account (signed-in first; MostRecent isn't
always the account on the Frame); stop_steam also waits for Steam's helpers; after the restart the agent re-reads
shortcuts.vdf and writes once more if Steam put its old copy back (`shortcuts_lost`), else reports it. 0.6.3's Play
auto-repair (`_add_then_play`) crashed (its job got a Job, not a reporter). Uploads resume on a transient OSError.
Lepton's Android 11 has no clipboard service (134 services; checked with `podman exec … service check clipboard`):
SDL/LÖVE apps crashed at start → `frame.sdl_clipboard` (`apk/dex.py`: in-place dex edit, nops one invoke, fixes
the header checksum/signature; verified with LÖVE for Android 11.5, GitHub #24 Dramatic Shape). 2D apps (vr_kind none)
keep every suggested patch that isn't about VR (`needs_vr = False`), not a fixed list. FramePort on the Frame:
`frame/local.py` (127.0.0.1, own key authorized; "This Frame (experimental)"; app data in
~/.local/share/frameport-app because ~/.local/share/frameport is the agent's) and Steam library changes wait while
Desktop Mode is open. Verified on the device 2026-10-04 (install + launch test from the Frame). Library updates on the
Frame run one at a time (agent v40 flock): two quick uninstalls used to bring a removed shortcut back (Roblox).
Headset round 3 (owner's verdicts, 2026-10-04): Blade & Sorcery works — its freezes were the headset's wear sensor
flickering off while worn (vrserver.txt "HMD off/on" 0.5-2 s) → per-game `focus_hold_ms=2500` (only this game reacts;
not a default). BattleGlide works (focus_hold). Myst works with issues (object glitches; space warp ruled out, its
recipe keeps it on). I Am Cat works with issues (judder; scale 0.8 no help). Roblox unsupported (je_free in
libroblox, also with the Vulkan shim). Installs skip the Steam restart when the shortcut is unchanged and wait while a
game runs (agent v37). New default fixes in FramePort's code reach games already in a library: after every app update
`library._follow_catalog` re-derives each non-user recipe once (setting `recipes.app_version`; tests switch it off via
`library.REFRESH_ON_UPDATE`). Analysis fields added later: **bump `analysis/detect.ANALYSIS_VERSION`** (stored as `analysis.extra.analysis_version`); older entries whose APK is still there are analysed again at the GUI's start (background thread, not a job: ~8 s per 900 MB APK) and before a build (`pipeline.refresh_analyses`, GitHub #104); only `analysis`/`suggested` change, user recipes stay; an unreadable APK gets `analysis_failed` = the version (not retried until the next bump). Troubleshooting techniques: docs/PLAYBOOK.md "Debugging techniques". Unity `boot.config` "vulkan" substring mislabels GLES games as Vulkan
(I Am Cat ran GLES); OVRPlugin's "Unavailable OpenXR extension: XR_FB_scene" is routine (no longer triaged).
**Round 4 (2026-10-04):** XR_KHR_android_surface_swapchain is listed by the Frame's runtime but returns
FUNCTION_UNSUPPORTED → adapter `surface_emul` (default on, `native/adapter/surface_swapchain.c`): an ordinary runtime
swapchain is returned, the game gets a Surface from a SurfaceTexture on a worker thread (own EGL context, JavaVM from
XrInstanceCreateInfoAndroidKHR), frames are read back (glReadPixels) and uploaded in xrEndFrame (Vulkan: own command
buffer + fence per panel, never waits; GLES: glTexSubImage2D). I Am Monkey's intro works (owner-confirmed). SUPERHOT
(Quest) quit at start: its cloud save folder was mode 1700 and Lepton's app writes through the folder's group →
launch.sh `fix_perms` adds u+rwx,g+rwx (agent v41). Lucky's Tale: Unity's Loading.PreloadManager thread grows the
native heap ~1 GB/s until the OOM killer (Frame freezes); no fix. `perf` works as steamos (paranoid 2; ptrace_scope 1
blocks gdb/eu-stack/debuggerd) — see PLAYBOOK "Debugging techniques". QuestCraft downloads its JRE at first run into
`files/runtimes/JRE` (Pojlib Installer); on the Frame it was missing (unpacked by hand, unverified). WiiCompiled: its launcher shows
(lepton-show-flatscreen) but imports .wcgame files through Android's document picker (OPEN_DOCUMENT), which Lepton
lacks (ActivityNotFoundException) → imported by hand: libmain.so + game.json → internal files/game/<profile>,
DATA/ → external files/WiiCompiledOpenXRVR/DATA, MOD/ → .../RetroRewind6 (decompiled with jadx). Lepton has no
picker at all: a FramePort fix would be an injected picker activity. Generated cover/banner art used to suppress the
Steam placeholder set (no Steam art at all) → fixed in artwork/steam.py.
**Steam library failures (GitHub #21/#30):** on some Frames Steam never loads FramePort's shortcuts.vdf entries (same SteamOS build and arm64 beta client as the dev Frame; IDs correct; file written before Steam starts): Play → console_log `GameAction [AppID <id>] … RequestingLicense → UpdatingAppInfo → LaunchApp failed with AppError_9` (= "Game configuration unavailable", Steam treats the id as a store app). Cause unknown; agent v42/43 diagnostics: `launch` returns `steam` (started / error + code / silent from console_log), `collect_diag` adds `steam_library` (real Steam dir, beta, Steam start vs vdf write time, entries per account, devkit games) + `steam-console.txt` (incl. Steam's `logs/shortcuts.previous.txt` if present; steamclient.so has "LoadShortcuts: rejecting attempt to load shortcuts: invalid account ID"). **Fallback (agent v43):** when Play gets AppError_9 the agent registers the game through Steam's devkit interface (what Valve's Devkit Management Tool does, MIT: `devkit-1 steam://devkit-1/<~/.steam/steam.token>/create-shortcut?response=<file>&gameid=<id>&directory=~/devkit-game` written to ~/.steam/steam.pipe; answer file / `.error`; `~/devkit-game/<id>/launch.sh` = link to the game's launch.sh, `<id>-argv.json` ["launch.sh"] (relative to the folder), `<id>-settings.json` {steam_play 0, compat_tool ""}). Steam adds it live (no restart), picks its own appid (read back from shortcuts.vdf by DevkitGameID), shows it as a normal non-Steam game named "Devkit Game: <id>" (OpenVR 0, but VR works: owner-verified 2026-10-04), art copied to grid/<appid>*. Ids: letter first, then letters/digits/_ only (spaces, '-', leading digit → "missing/invalid arguments"). Later Plays use the devkit entry; uninstall (`devkit_unregister`, `delete-shortcut` live) and purge remove it. Verified on the dev Frame: register/launch/unregister; the AppError_9 trigger itself only in unit tests. GitHub #42 (two Steam accounts, neither MostRecent): Steam rewrote the active account's shortcuts.vdf without FramePort's entries right after starting, and added the devkit entry live without saving it, so `devkit_appid` (vdf only) failed ("Steam added the devkit entry but didn't save it"); agent v55 `devkit_appid_from_log` reads Steam's `sanitize shortcut app id "~/devkit-game/<id>/launch.sh": replacing 0 with N` console line. GitHub #41 (one account): same Steam behaviour, and every install restarts Steam, which forgets the never-saved devkit entry. Agent v56: the log fallback reads only the current session's console_log.txt; Play re-registers a devkit entry that gets AppError_9; art updates also write the devkit entry's grid art (`copy_grid_art`). Open: why some Frames' Steam never loads or saves shortcuts.vdf (single account too).
**Exit game (GitHub #36, 2026-10-04):** `Apps.TerminateApp` (what Exit game calls; reachable for tests through Steam's CEF devtools on 127.0.0.1:8080, SharedJSContext, `Runtime.evaluate` — a stdlib websocket client is enough) closed both a normal shortcut and a devkit entry on the dev Frame within 1-3 s. SIGTERM to Steam's `reaper` alone left launch.sh + Lepton (setsid) + the container running (an earlier headset launch ran on for 87 min); SIGTERM to launch.sh cleans up in ~4 s. Agent v44: launch.sh's 2 s loop ends the game when its parent (the reaper) is gone; `upgrade_launchers` (from ensure_host_fixes) adds that to existing launchers in place. Which in-headset exit path fails is still unknown. Screenshots on the Frame: all under SteamVR's appid 250820 (`760/remote/250820/screenshots`, screenshots.vdf has creation time, no game).
**SteamVR dashboard at game start (2026-10-04):** the dashboard (Resume game / controller / VR options) was open whenever a FramePort game started (also over Lepton's 2D launcher). Steam's UI exposes `SteamClient.OpenVR.VROverlay.{IsDashboardVisible,HideDashboard,ShowDashboard}` (CEF devtools, `steam_js` in the agent). Agent v44: launch.sh starts `_dashboard_worker` (waits for FrameBridge's first `pacing:` line in launch.log, then hides a visible dashboard for 25 s, at most 3 times; log `<base>/dashboard.log`; opt out with env FRAMEPORT_KEEP_DASHBOARD=1). Verified on the dev Frame (dashboard shown → 4XVR launched via Steam → hidden ~5 s after the first frames); not yet seen in the headset. **Headset (ITR2, 2026-10-05): too late** — Steam shows its frame menu (`valve.steam.gamepadui.frame.menu`, vrwebhelper_systemui.txt `[PooledPopups] Showing`) ~0.3 s after the game's first submitted frame (FrameBridge `new layer:`), but the first `pacing:` summary comes ~8 s later; the owner had pressed Resume (`[HideDashboard] return_to_game`) before the worker looked. Agent v52: watches from the first `new layer:` line (incremental log reads, 0.5 s polls, 30 s window). Agent v59: watches 120 s and hides up to 10 times (the menu came back after 30 s in the owner's sessions). Agent v53: stops for good once the player opens the dashboard with the controller (`toggle_dashboard_action` in vrwebhelper_systemui.txt since the worker started): v52 closed it 60 ms after each press, and the game, paused for it, stayed paused.
**Space warp / ITR2 (2026-10-04):** Into The Radius 2 (UE5, `libUnreal.so` + OVRPlugin, Vulkan) uses Application SpaceWarp (extra swapchains `376x376 format=97` motion vectors + `format=129` depth); #35 reports flickering textures. OVRPort's `patch_disable_space_warp` has no effect on UE5, and hiding XR_FB_space_warp in FrameBridge's enumerate doesn't reach the game either: **OVRPort's dispatcher offers/enables XR_FB_space_warp itself**. FrameBridge `hide_space_warp` (per game, off) hides it and strips `XrCompositionLayerSpaceWarpInfoFB` (1000171000) from the projection views (log `hide_space_warp: removed space warp info`); the game still renders its MV swapchains. Test build installed on the dev Frame; flicker not yet checked in a headset. Heuristic: UE5 + OVRPlugin shows it as an option (off). `equirect_emul` is suggested for Unreal games whose graphics API isn't detected ("GLES or unknown"); on Vulkan it switches itself off (Myst/Riven/ITR2): harmless, but Unreal Vulkan detection is a known gap.
**PR #34 (merged 2026-10-04, Lucas-Mathieu):** `native/adapter/audio_metadata.h` patches Meta XR Audio Wwise (only build ID e1619e7f…, Batman: Arkham Shadow 1.4.1) so queued audio metadata isn't freed while the current audio frame references it (smoke-bomb crash). Verified headless: installs ("metadata reclamation follows …"), 72 fps; we added a once-per-second scan limit. Outside PRs: review source, rebuild `artifacts/` ourselves, merge locally (contributor's commit kept), push main.
**Heuristics eval (2026-10-04):** `scripts/eval_heuristics.py "<VR CyberDeck Portable>"` → 19/44 recipes exact, 322/352 fields (most diffs: `frame.unity_text_input` suggested where older catalog recipes lack it; the rest are runtime-only findings: use_alt, vk_shader_fix, focus_hold, sync_guard, multipass). `PATCHED/` copies aren't scored (already converted).
**Power (0.9, agent v47):** sidebar bar below the Frame card: Sleep / Restart / Shut down, each confirmed (`app.frame_power`, a running game asks again). logind answers CanSuspend/CanReboot/CanPowerOff = "challenge" for an SSH session but "yes" inside a user unit, so agent `power` runs `systemctl suspend|reboot|poweroff` from a transient `systemd-run --user --on-active=3` timer (the command answers first). Restart verified on the device (back in ~84 s, new boot id).
**Proton default (agent v51, owner's choice 2026-10-05):** the newest **stable** ARM64 Proton is the default for PC VR games (`pick_proton`: asked-for name/alias, else newest installed stable, else any stable). Agent v49-50 defaulted to Experimental; the owner's headset A/B with Rick and Morty (Experimental 69 % reprojected frames, stable felt much smoother) reverted it. The game page's Customize shows `pcvr.proton_tool` as a dropdown (Stable (default) / Experimental = alias `proton-experimental`); `ensure_proton` installs a chosen one (it asks once more for the runtime Steam only names after Proton is installed; Experimental installed unattended on the device in 53 s). A PC VR launch test that fails on stable suggests Experimental (`pipeline.PROTON_TOOL`, `proton_alternative_worth_trying`, button "Try Proton Experimental and reinstall"). Remaining Rick and Morty stutter on head turns = the game rendering below the refresh rate (reprojection), not Proton.
**USB cable link (verified 2026-10-05, cabled to the dev PC):** `usb0` is a USB **NCM** gadget (configfs g1: `ncm.usb0` + `ffs.adb`, Valve 28de:2460) set up by `usb-ncm-gadget@usb0.service`, which only runs with `ConditionPathExists=|/etc/systemd/system/adbd.service.d/steamos-devkit-enabled` = **Developer Mode only**. `usb-ncm-dnsmasq@usb0` serves DHCP with fixed `dhcp-host` entries: Frame 10.86.200.233, PC **10.86.200.234** (`frame/usb.py` FRAME_USB_IP/PC_USB_IP). Windows 11 binds its built-in "UsbNcm Host Device" (no driver); WSL mirrored sees eth 10.86.200.234. USB is high-speed (USB 2.0): upload 36.9 MB/s vs 11.3 MB/s home Wi-Fi. Frame→PC over the cable worked without a firewall change. `scripts/usb_autotest.py` (Windows via powershell.exe from WSL), `frameport frame usb-check`. Wizard "Set up with a USB cable" (`PairingServer(host=PC_USB_IP)`; the real bootstrap ran end to end over USB). Discovery dedupes by SSH host key and prefers USB > Frame hotspot > network (`discovery.dedupe`); a lost address is re-found by key (`Frame.relocate`, tested with a dead saved address).
**Catalog updates without a release (2026-10-05):** apps fetch `catalog/games/*.yaml` from GitHub `main` (`recommend/catalog.refresh_remote`: one API call `git/trees/main?recursive=1` + raw.githubusercontent downloads of changed blobs, cached as `catalog-gh-<sha>.yaml`, every 6 h; GUI at start + Settings → Data "Check now"; CLI background thread + `frameport catalog-update`). `catalog.load()` only reads the cache. A remote entry is skipped (`unusable_reason`) when it names a patch/adapter key this app doesn't register, has a field `CatalogEntry` doesn't know, or `min_app` > this version: add `min_app` (or rely on new patch ids) when a config needs an unreleased FramePort. Remote beats bundled unless the bundled entry has a later updated/verified date; the user's own entries still win as before. Off: setting `catalog.auto_update`, env FRAMEPORT_NO_CATALOG_UPDATE; FRAMEPORT_CATALOG_URL still overrides the source. So: **pushing a catalog change to main reaches users within 6 h** (their recipes follow via catalog_rev → Update on Frame).
**Start activity / QuestCraft / ITR2 pop-in (2026-10-05):** WiiCompiled's VR part (`QuestActivity`, SDL, process `:game`) stays behind Lepton's flat window when the launcher is shown (`device.text_input_window`). `frame.start_activity` (opt-in; analysis `extra.vr_activity` = the original APK's activity with Meta's VR category when it isn't the launcher) gives that activity's filter LAUNCHER and every other LAUNCHER becomes INFO, per element (`axml.set_start_activity`); Lepton's own `apk-info-extractor --print-activity-name` (on the Frame under Lepton/liblepton/apk_extractor) then answers QuestActivity. **OVRPort gives every MAIN activity LAUNCHER + VR categories**, so the VR activity must come from the original APK's analysis, not the converted manifest. Lepton has no activity override (`APP_ACTIVITY` is reset when app_metadata.sh is sourced). QuestCraft reaches Minecraft 1.21.5 (Fabric, Vivecraft, Sodium; LTW "Large Thin Wrapper" GL 3.0 on the Frame's Zink) and stops at `OpenGL error 1282` in `WindowFramebuffer` createTexture (Minecraft's crash report in `files/instances/1.21.5/crash-reports/`, Pojlib's `files/latestlog.txt`); the vrclient SIGSEGV / "pthread_mutex_lock called on a destroyed mutex" afterwards is only the shutdown (Unity's render thread still in vrclient while the instance is destroyed). ITR2 "models popping in and out" in game: Unreal reads user config from **internal** storage (`lepton-data/internal/<pkg>/files/UnrealGame/<Project>/<Project>/Saved/Config/Android/Engine.ini`, not external); `r.AllowOcclusionQueries=0` there broke its menus (removed again). Vulkan shim `vk_query_slots` (two slots per occlusion query) never saw ITR2 create an occlusion query pool, and the run with it crashed (Unreal RenderThread SIGSEGV, caught by sentry-native: no tombstone; the backtrace is in `cache/sentry/*/.sentry-native/*.run/*.envelope` with module offsets) - left off. Next suspect: Unreal 5 mobile HZB occlusion reading depth stored with Valve's FDM injection layer; test = launcher `VK_INSTANCE_LAYERS=''` (Lepton then loads neither VALVE_rpo nor VALVE_fdm_injection, Fossilize still). QuestCraft: LTW overrides glGetError and returns 0 with `LIBGL_NOERROR` set; Lepton passes `LEPTON_ENV_<NAME>` to the app as `<NAME>`, so catalog `lepton_env: {LEPTON_ENV_LIBGL_NOERROR: '1'}` gets Minecraft past the GL error 1282; then black picture (see the catalog entry). FrameBridge logs `new layer` once per layer type per process and projection views only for the first frames, so a second session in the same process (Vivecraft after Unity) needs `layer_debug=1`.
**Vulkan validation / ITR2 solved (2026-10-05):** Khronos' validation layer can't come from Lepton (its layer dir is in the read-only guest image; the Linux copies on the Frame are glibc) and a global `debug.vulkan.layers` property (via the LEPTON_GFXRECON_FP_PROPS trick) kills the container at boot. Working recipe: bundle `libVkLayer_khronos_validation.so` (Khronos android-binaries release, arm64-v8a, stored uncompressed) into a test APK re-signed with the game's key (`apk.sign.sign`), install with `frameport install <pkg> --apk`, and set the Vulkan shim's `vk_validation=1` (it adds the layer at vkCreateInstance; findings under the logcat tag VALIDATION in launch.log, each message ID capped at 10). ITR2's flicker + windows behind models = Unreal's Qualcomm shader-resolve subpasses with a depth resolve of a single-sampled depth attachment (VUID-04908/-03179); `vk_spec_fixes=1` drops that resolve (its TRANSFER_DST half never applies to ITR2: the main depth is transient). Valve's VALVE_rpo layer removed ITR2's fog (catalog `lepton_env: VK_INSTANCE_LAYERS: VK_LAYER_VALVE_fdm_injection`). `r.ViewDistanceScale=2` in the internal Saved/Config/Android/Engine.ini only exists on the owner's Frame (the catalog can't write internal storage yet). Launch tests: Lepton prints a transient "is not a running context" on the first start after an APK change (agent v54 `not_started` waits 30 s for "Boot complete!").
**GitHub #38/#39 (2026-10-05):** PowerWash Simulator stuck at "Waiting.." on a reporter's Frame = Unity sized the right-eye image rect past its swapchain (rect 268+1656 on a 1920-wide swapchain; the runtime's recommended size differs per Frame) and SteamVR rejected every frame (`xrEndFrame failed -25`, XR_ERROR_SWAPCHAIN_RECT_INVALID), after which the game stopped calling xrEndFrame. FrameBridge `rect_clamp` (default on; sizes noted in xrCreateSwapchain; clamped layers count as swapped) keeps projection/quad rects inside their swapchain; triage `swapchain-rect-invalid`. Not reproducible on the dev Frame with `scale` (Unity's eye sizing isn't just the recommended width); no regression there (72 fps). The adapter's revision was NOT bumped (that would mark every installed game outdated): affected users rebuild. The Room VR (#38) = Unity's render thread SIGSEGV in libgallium_dri.so ~30 ms after the game recreates its eye swapchain with samples=4 (FrameBridge retries samples=1); the game's own code turns MSAA on (not OVRManager); candidate fix: hide GL_EXT_multisampled_render_to_texture (GL shim) - needs the APK to test. The launch test passed both (render thread dead / no frames): triage gap.
**Headset round 2026-10-05 (evening):** I Am Cat's judder = repeated xrLocateViews of one display time returning
slightly different poses → FrameBridge `pose_consistency` (owner-verified, works). Lucky's Tale freeze on touching a
save slot = OVRPort's OpenXR dispatcher converting XR_FB_haptic_amplitude_envelope with ns read as s (GitHub #9) →
xrshim `haptic_fix` (owner-verified); its remaining short pauses were the wear sensor ("HMD off/on" 0.5-1 s in
`logs/eyetracking.txt`, not vrserver.txt) → **`focus_hold` is on by default with 5 s** (owner's choice; C defaults
too). The Room VR works (`unity_no_overlay_copy`). BattleSisters (Unity 2019.4 built-in VR, no libOculusXRPlugin.so)
flooded "outside of frame bounds" ~2400/s and hung the GPU (kernel hangcheck, zink DEVICE LOST) → the ovrpshim frame
wait now also applies to Unity 2019 without the Oculus XR Plugin (`UnityOculusCheck.legacy_loop`). Vader Immortal
(UE4, stuck on the loading image after the intro, GitHub #49): VRP repacks carry a Frida gadget (`libfrda.so`,
config `hijack_responses`), but OVRPort's `patch_clean_up_frida` removes its loadLibrary call, so it never runs on
the Frame; `frame.ovr_trace` (opt-in, `native/ovrtrace`: 1138 exported ovr_* stubs → real loader functions, logcat
tag `fp_ovrtrace`: calls, PopMessage answers, unanswered requests every 10 s) is the next diagnostic.
**Unity built-in VR input (2026-10-05):** probe builds (`FRAMEPORT_INPUT_PROBE=1 frameport build …`; it logs
trigger/grip crossings too) showed OVRPlugin returns the full Touch state for every controller mask (also Go masks);
presses reach the games' OVRInput. Accounting+ (Il2CppDumper v6.7.46 reads its Unity 2017 metadata v24, run on Windows)
passes stance selection (NewtonVR grip/trigger) but its motion warning waits for `Input.GetMouseButtonDown(0/1)`,
which Lepton never delivers (no focused Android window) → ovrpshim registers its own
`UnityEngine.Input::GetMouseButtonDown(System.Int32)` icall (il2cpp_add_internal_call, after Unity's: resolve first)
that adds a click in the frame a Touch trigger or A/B/X/Y is newly pressed (`unity_oculus_check` revision 3).
BattleSisters (Unity 2019 InputSystem/XR InputDevices via `VrHandInput`): libunity's Oculus module (OVRPlugin
function table: a global pointer at 0x16b57c0 in this build, slots filled by name, e.g. +0x160 GetControllerState,
+0x168 State2, +0xe8 GetNodePresent) only reports controllers when `strncmp(deviceModel, "Oculus", 6) == 0` (next to
the Go check `deviceModel == "Oculus Pacific"`); Lepton's model is "Valve Lepton" → Go/unknown → only Go masks polled,
buttons dead. `unity_oculus_check` revision 4 (`oculus_model_checks`, Unity 2019 only) turns the 17 compares' length
into 0 (`orr w2, wzr, #6` → `mov w2, #0` after the adrp/add of the "Oculus" literal; the string stays: it is also
Unity's VR device name). Device result: libunity now polls `ovrp_GetControllerState(0x3)` (Touch); owner-confirmed
2026-10-06: buttons work. Its first controller vibration then hit OVRPort's haptic-envelope bug (11 GB, OOM kill) →
`haptic_fix` is now suggested for every game with libOVRPlugin.so (settings detect), BattleSisters works. Ruled out before: ProductName, GetNodePresent, the device-model string itself, exports.
Accounting+ works (owner, 2026-10-06).
**Vader Immortal (UE4, GitHub #49, 2026-10-06, headless):** stuck after the intro on an in-game image (the splash
quad ends ~6 s in; then the game's own projection frames, 72 fps, balanced xrBeginFrame/xrEndFrame). Not the Platform
SDK (`frame.ovr_trace`: only user + entitlement, both answered) and not the repack's Frida gadget (OVRPort's
`patch_clean_up_frida` removes its loadLibrary). It **leaks ~430 GPU mappings (/dev/dri/renderD128) and ~20 MB a
second** (6.5 GB + swap after 5 min, then 26 fps): page-fault stacks (perf -e page-faults, offsets resolved with the
process maps + vrclient.so's own symbols; its text segment is at file offset + 0x4000) end in SteamVR's runtime:
`xrBeginFrame → CSxrCompositorOpenVR::BeginFrame → SubmitExplicitTimingData → CVRCompositorSharedTextures::
BeginGPUTimingCommandBuffer` and `xrEndFrame → CVRCompositorClient::SubmitWithArrayIndexAndTime`. Ruled out: the
layer color scale/bias + image layout structs (FrameBridge `strip_color_bias` 1/2, diagnostic), Valve's Vulkan
layers (VK_INSTANCE_LAYERS=""), array swapchains in general (Lucky's Tale/I Am Cat flat). Kernel tracepoints aren't
allowed for steamos. FrameBridge: `layer_debug` logs xrDestroySwapchain and per-5 s xrBeginFrame/xrEndFrame counts;
`frame_balance` (ends an open frame before the next begin) exists but Vader never leaves one open. Next (built,
not yet run: the Frame slept): `layer_debug` also counts xrAcquire/Wait/ReleaseSwapchainImage per 5 s (hooked only
with layer_debug, after the other conditional hooks) to see whether Vader skips a wait or release: it doesn't (361/361/361 per 5 s).
FrameBridge `snapshot=N` (`snapshot_gl.c`, GLES): every N s the left-eye image the game submits is read back on its
own context and saved as `files/fb_snap_0-7.ppm` (quarter size) — headless launches show a black headset view, this
shows what the game draws. Vader: Lucasfilm logo (an OBB mp4: video works), then its loading card (portrait + segmented
bar) that never advances; the async loader thread sleeps and OBB reads stop (~168 MB of a 2.7 GB pak).
**Double launch (agent v57, 2026-10-06):** a second Play while Lepton still boots (~10 s with nothing to see) made
the second Lepton stop the first one's container ("Waiting for steamlaunch-<appid> (PID …) to exit", exit 137
"(starting)", "Clearing baked app data due to early exit") and both died (Vader, BattleSisters). launch.sh now takes
`flock` on `<base>/.launch.lock` (fd 9, inherited by Lepton; the watchdog and dashboard helpers close it with
`9>&-`); a second launch exits 0 and logs to `<base>/launch-dup.log`. `upgrade_launchers` adds it to existing
launchers. Verified on the device (second launch during boot: ignored, the first kept running). Vader on its loading
card ignores input too (owner pressed/held every button: presses reach the runtime, the card never changes).
**Issue triage round (2026-10-06, owner's headset):** vibration in OVRPlugin games = 2 s vibrations updated per frame and
stopped with amplitude 0; FrameBridge turns amplitude 0 into xrStopHapticFeedback (else each buzz ran 2 s), haptic_fix
uses the envelope RMS (was its peak), per-game `haptic_scale` → Creed/The Boys/Jurassic World fine. Lambda1VR (TBXR):
loader named by Build.MANUFACTURER (`frame.tbxr_vendor`), no graphics extension enabled (FrameBridge adds
XR_KHR_opengl_es_enable + asks for the requirements on -50), always multisampled render-to-texture (GL shim gives
single-sampled stand-ins when GL_EXT_multisampled_render_to_texture is hidden) + its xash/ data → works. Jurassic World:
`frame.vrapi_stub`. Metro Awakening: contributor's vkshim fixes + hide_space_warp + no VALVE_rpo (jumping polygons like
ITR2). Pinball FX VR: hide_space_warp (stutters). Eleven: a Meta online request fails after platform init → needs Meta
services. Star Wars Tales: top half black + freeze after loading; ruled out: asset-file paks (never requested), Valve
foveation (off: no change), GL errors (MESA_DEBUG=1: none), EGL_BAD_ACCESS once in OVRPlugin init (harmless); the game's
eye image reads back black. BlazeRush: VrApi bridge exports + ovr*_ToString stubs + avatar stub get it to the menu room
at 72 fps, full input reaches it (diagnostics `input 5s`: Touch type only, sticks 1.0, buttons, poses 0x8f), but the room
draws no controllers and ignores input; avatar loader forced to its "Failed" path (no logged-in user) changed nothing.
`frame.ovr_trace` can't trace Unity games (P/Invoke dlsym; nothing imports ovr_*). Headless: the VrApi bridge's 30 s
head-pose deadline ends VR mode without a worn headset (not a game bug).
**Lepton storage (2026-09-30):** each app's /sdcard (= /storage/emulated/0 → `<base>/lepton-data/external`) has `Movies`/`Download`/`Documents` symlinked to the Frame's `~/Videos`/`~/Downloads`/`~/Documents` (liblepton/mounting.sh, only if they exist at start); agent v24 `storage_targets` reads that mapping. Android's MediaProvider canonicalises paths to /home/steamos/... and rejects every file ("doesn't appear under [/system/media...]"), `sm list-volumes` is empty: the media index never works, apps must browse folders. Lepton installs with `adb install -g` (runtime permissions granted, MANAGE_EXTERNAL_STORAGE too). Files: `install/files.py`, `frameport frame send|storage`, GUI Files tab (formerly Frame → Send files).
**SteamVR per-app settings (2026-09-30):** editing steamvr.vrsettings while SteamVR runs is lost; the web API (127.0.0.1:27062 /app/setsettings) needs `x-steamvr-secret`. `native/vrsettings` = `fp_vrsettings.exe` (freestanding, OpenVR `FnTable:IVRSettings_003` as a Utility app, loads SteamVR's bin/win64/openvr_api.dll) sets them live and SteamVR persists them: section `steam.app.<shortcut appid>`, keys `preferredRefreshRate` (float) and `motionSmoothingOverride` (0 global, 1 on, 2 off, 3 always). Steam Link (vrlink) lists the Frame's rates 72/80/90/96/108/120/144 in vrserver.txt and follows the per-app preference ("host preferred N Hz"; whether the key is honoured is unverified in-headset yet). Judder metric: vrcompositor.txt session summary dropped + "Timed out. N total" (Stormland: 0 dropped but 313 timeouts in 2 min); fpsVR (`%LOCALAPPDATA%\fpsVR\*.json`, 0.1 ms histograms) gives p99 CPU/GPU ms. `pcvr.steamvr_tuning` (default on, PC only) applies on Play: highest rate whose budget ≥ p99×1.05, at least one step down, smoothing on.

**Language packs (merged from PR #20, 2026-10-04):** overport's `libovrplatformloader.so` is a dispatcher that `dlopen`s Meta's own loader (`libovrplatformloader_meta.so` / `_meta_q1.so`, also `libpxrplatformloader.so`) and keeps its own message queue; `ovr_LanguagePack_GetCurrent/SetCurrent` are 8-byte `return 0` stubs in it, `ovr_AssetFile_GetList` forwards to Meta's loader. `frame.langpacks` (opt-in, `native/langpack`) serves `<tag>.lang` files from the game's data; `elf.hide_exports` marks the loader's exports STB_LOCAL (bionic and glibc only match GLOBAL/WEAK; verified for glibc, bionic's `is_symbol_global_and_defined` is from memory). `libfp_langpack.so` is built here (`python native/build.py --only langpack`) and committed. Owner-verified 2026-10-04: Deadpool VR with only `en.lang` and the patch on plays English dialogue and runs normally (headless launch tests show 2-6 fps while it loads: not a regression sign). A dispatcher answer that arrives after we answered the timed-out GetList is dropped (one answer per request); games already in a library get `lang_packs` filled after an app update (`library._refresh_data_fields`). The library logs to logcat under the tag `fp_langpack` (dirs looked in, packs found, every language-pack call), so it shows up in the game's `launch.log`; an `ovr_AssetFile_GetList` the dispatcher leaves unanswered for 1.5 s is answered with our packs alone. Deadpool VR (Unreal) accepts a pack only when its `Metadata` equals the game's own version string (`ULanguagePacksSubsystem` compares it with `%s.%s.%s.%s.%s` built from the build info, e.g. `1.0.40.356975.Quest` = versionName; found by disassembling `libUE4.so`): the patch writes the APK's versionName into the library (`@FPMETA@` slot, `with_metadata`), `FRAMEPORT_LANGPACK_META` overrides it. Deadpool VR (2026-10-04, headset): German became selectable with that Metadata, but dialogue stayed silent (even English once reported as a pack) while the path was spelled `/sdcard/Android/obb/<pkg>/x.lang`; with the `/storage/emulated/0/Android/obb/<pkg>/x.lang` spelling (Unreal's own, now listed first in `scan_dirs`) German dialogue plays. `FRAMEPORT_LANGPACK_SKIP=<tags>` or a file `fp_langpack_skip` in the obb folder leaves packs out of the list (experiments).

**Unresolved (as of 2026-09-28):** Arcsmith (right-eye distortion) and Time Stall (both eyes) — swap, tracking, Valve
layers, depth, pacing ruled out. Sniper Elite VR (DEVICE LOST), Espire 1 (Mesa GL upload crash), HITMAN 3 (freedreno
crash): use PC versions.

**Install links / FrameDrop button protocol (2026-10-07, not yet clicked end to end from a browser):** FrameDrop
(framedropvr.com, a closed-source Windows sideloader) defines "Install with FrameDrop" buttons:
`https://framedropvr.com/install?manifest=<url>|url=<file>` → that page opens `framedrop://install?…` (1.6 s, else its
home page); manifest `{"schema":"framedrop.install/v1","name","files":[{"url","sha256"}]}` (name = Steam title; .apk
or Linux .zip). `deeplink.py` (no Flet) parses framedrop://, frameport:// and the pasted https link, enforces its
rules (https; http only on loopback; no credentials; no LAN/loopback/link-local IPs, also after DNS and redirects; URL
ends in a file name), caps manifests at 256 KiB, ignores non-hex sha256 (FrameDrop's own example has a placeholder),
downloads into `<data>/downloads/<slug>-<hash>/` (OBBs → `obb/` next to the APK, `.part` removed on cancel/mismatch);
`pipeline.add_from_link` routes APK / Linux / exe and sets `title` + `title_locked` + `link` (a bare file link keeps
FramePort's title). GUI: `views/link_dialog.py` (always asks first), Add games → "Install from a link…", CLI
`frameport open-link [--yes --no-install --gui]`. **A `flet build` bundle can't take a URL argument** (the Flutter
host treats any argv as a developer page URL), so `urlhandler.py` registers a script, not FramePort.exe: Windows
HKCU `Software\Classes\{framedrop,frameport}` → hidden PowerShell `frameport-link-handler.ps1`; WSL (source runs)
the same keys → `wsl.exe -d <distro> -e sh frameport-link-handler.sh`; Linux `frameport-links.desktop` +
`xdg-mime`; macOS unsupported (Apple Events, paste instead). The script drops the link into `<data>/links/*.link`
and starts FramePort unless `<data>/gui.alive` is < 10 s old (`gui.starting` stops double starts); the GUI's
`_watch_links` thread touches the heartbeat and opens links (newest window session). Settings → Install links: one
switch per scheme (`links.framedrop`, `links.frameport`, default on); a scheme another program owns (FrameDrop) is
only taken with "Use FramePort for these links" (`register([s], force=True)`); off removes only FramePort's own
registration (`MARK` in the command). Never registered with FRAMEPORT_HOME/FRAMEPORT_NO_LINK_HANDLER (tests,
screenshots). Same round: files dropped on the Library (`ui/dropped.py`, bundles only like the Files tab), "Add a
Windows program (.exe)…" (`pipeline.add_windows_exe`: exe in Downloads/home/drive root copied alone into
`<data>/windows-apps/<slug>/`), patch `device.display_mode` (Automatic / VR / Flat window → `InstallContext.display`,
`installer.show_window`). FramePort-only manifest extension `"frameport": {"description", "icon"}` (bad values ignored; icon: same URL rules,
≤2 MiB, Pillow-checked, ≥32 px, saved as PNG in `<data>/downloads/icons/`, shown in the question by asset URL, then
`sources.apply_custom(pkg, "icon")` unless `.picked` exists; description fills `details.description` only when
empty). Bare file links get a title guessed from the file name (`title_from_filename`: version/arch dropped, package
names → last part). Windows test of 0.12.1.dev191 (2026-10-07): a button click opened the dialog; but a click right
after closing FramePort did nothing (the closed window's heartbeat was < 10 s old) → the handler scripts now wait
up to 4 s for the link file to be taken before trusting the heartbeat, the window's CLOSE event / atexit delete
`gui.alive`, and the Windows command runs under `conhost.exe --headless` (plain `-WindowStyle Hidden` flashed a
console). Screens: `scripts/ui_smoke.py --links --fake-frame --game <pkg>` (tall pages: `--viewport
1280x7000`; Flutter's popup menu ignores Escape).

## Releases, CI, GitHub
Maintainer-only notes (accounts, credentials, key locations) live in the git-ignored `CLAUDE.local.md`.
Public repo `github.com/spoopyghosty0/frameport` (branch `main`). Push a `v*` tag → CI (`.github/workflows/build.yml`)
tests, builds Windows x64 / macOS arm64 / Linux x64 bundles, signs, attests and publishes a GitHub Release
(`FramePort-*.zip/.tar.gz`, the CLI wheel `frameport-<ver>-py3-none-any.whl`, `SHA256SUMS.txt`,
`FramePort-selfsigned.cer`; notes = "What's new" from the annotated tag message + the short `packaging/release-footer.md`; owner: keep release
notes short — a few "What's new" bullets, nothing long after them).
Installed apps find the release themselves (self-update), so the notes are what users see in the update dialog.
- **Dev builds (for testers, no release):** Actions → build → Run workflow (main), "dev" ticked, optional "notes"
  (what to test). The bundles get version `<next patch>.dev<run number>` (`scripts/dev_version.py`, not committed) and
  replace the rolling `dev` pre-release (`dev-release` job; same assets + SHA256SUMS). Automatic update checks ignore
  pre-releases; testers use Settings → Updates → "Install the latest dev build…" (`updates.check_dev`,
  `Updater.install_dev`). `parse_version` sorts 0.9.0 < 0.9.1.devN < 0.9.1, so testers get the next release normally.
- **Upstream trackers:** issues labelled `upstream` (#73 microphone, #74 haptics, #75 VrApi bridge) list our workarounds for OVRPort bugs and how to drop them; check them against OVRPort's latest release before each release.
- **Release checklist:** bump `src/frameport/_version.py` (the only version; `scripts/package.py` fails a tag build
  whose tag ≠ `v<_version>`), commit, `git tag -a vX.Y.Z -m "FramePort X.Y.Z" -m "<What's new, Markdown bullets>"`,
  push the commit and the tag. Never publish a release without its `SHA256SUMS.txt` (the updater refuses it) and keep
  the asset names (`updates.ASSETS`) — renaming them breaks updating for every installed copy.
- Signing is **free/self-signed by the owner's choice** (no paid certs, no SignPath): Windows binaries are signed with
  a self-signed "FramePort (self-signed)" code-signing cert (RSA 3072, valid to 2031, SHA-256
  `4E:12:98:91:62:C0:E4:50:FB:65:1D:34:BB:73:00:09:7B:78:BE:88:5C:A7:6C:42:23:46:9B:92:A1:59:A7:6E`); secrets
  `WINDOWS_CODESIGN_PFX` (base64) + `WINDOWS_CODESIGN_PASSWORD`. The private key is never committed (its
  location is in `CLAUDE.local.md`). macOS is ad-hoc signed only (Gatekeeper
  needs right-click → Open; notarization would need the paid Apple program). Users still see SmartScreen unless they
  import the .cer into Trusted Root.
- CI gotchas: `flet build` needs `--yes --no-rich-output` (it prompts to install Flutter; rich output crashes the
  Windows console) plus PYTHONUTF8; macOS builds need `--python-version 3.12 --arch arm64` (cryptography has no wheels
  for flet's default Python / x86_64 cross-build), with a PyInstaller fallback step; `astral-sh/setup-uv` has no
  floating major tags after v7 → pin the exact version; force-moving a tag starts duplicate runs (cancel one).
- `jni` (2026-10-06): jni_flutter 1.0.4/1.0.4+1 generate bindings that require jni ^1.1.0 while Flet's build template
  pins jni 1.0.0 → every `flet build` bundle failed (`JniVersionCheck`, "generated bindings expect package:jni
  ^1.1.0"). pyproject `[tool.flet.flutter.pubspec.dependency_overrides] jni = "1.1.0"` fixes it (pinning
  jni_flutter 1.0.4 does not); drop it once Flet's template moves to jni 1.1. A failed tag build publishes nothing:
  delete and re-push the tag on the fixed commit.
- macOS runner (2026-10-02): `macos-latest` jobs went unassigned (cancelled after 15 min, no steps); `macos-15`'s Xcode
  16.4 fails a Flutter plugin (`NWPath has no member`); `macos-26` (Xcode 26) builds with `flet build`. The PyInstaller
  fallback (`package.py --pyinstaller`) passes `--yes` so a failed `flet build`'s folder doesn't stop it at a prompt.
- Windows PowerShell calls from Python (`updates._powershell()`): use `%SystemRoot%\System32\WindowsPowerShell\v1.0\
  powershell.exe` with `PSModulePath` removed from the environment — started under PowerShell 7 (CI's default shell,
  or a user's pwsh terminal) it couldn't run Get-AuthenticodeSignature (v0.3.0's Windows update smoke failed on it).
  `v0.3.0` is a tag without a release (that failed build); the updater shipped first in v0.3.1.
- **Never start PowerShell with `DETACHED_PROCESS` from the packaged app**: it exits 0 without running anything (found
  in the 0.3.1→0.3.2 end-to-end test: the app quit, nothing updated). `updates.spawn_hidden` = CREATE_NEW_CONSOLE +
  hidden window; `apply()` waits until the script has logged that it runs and raises otherwise (the app then stays
  open). Windows installs of 0.3.1/0.3.2 can't update themselves: they need one manual download.
- The Linux bundle is built on ubuntu-22.04: a 24.04-built Flutter bundle needs GLib 2.80 (`undefined symbol:
  g_once_init_enter_pointer` on 22.04).
- **The repo is public: never commit personal data** — the Frame's IP address, the Steam user id, the owner's email,
  local home paths (native builds use `-ffile-prefix-map`). History was rewritten once to remove them.

## Heuristics (games not in the catalog)
Each patch's `detect()` suggests itself from the Analysis; `applies()` says whether it can matter at all (the UI/CLI
hide non-applicable patches; enabled ones are always shown). Rules learned from the 34 games:
MR-only (PASSTHROUGH required + BOUNDARYLESS_APP) → force_passthrough, + USE_SCENE → scene_emul + meta_permissions;
hand tracking required → controller_fix=0; OVRPlugin + ≥20 GiB → disable_space_warp; Unreal ≤4.21 or Unreal Meta XR
Audio → nodebug; Oculus-OS class referenced by the Unreal audio build or ≥2 Meta libs → oculusos; legacy-VrApi Unity
GLES with MSAA → unity_no_msaa; CryEngine → user.cfg r_variable_rate_shading=0; direct VrApi → bridge (+GL shim for
GLAD/GLES); Unreal → alternate no-ForceQuit build. Score changes with
`python scripts/eval_heuristics.py "<dumps>"` (catalog off vs verified recipes; currently 33/34 exact — Phantom's
"use the no-ForceQuit build" is only detectable at runtime via triage). Add a rule → re-run the eval + `pytest -m games`.

## Frame operations
- Find/connect: `frameport frame discover` / `frame info` (the remembered Frame is in `<user data>/frames.json`).
- Power off over SSH: plain `systemctl poweroff` is refused by polkit for remote sessions; this works:
  `ssh steamos@<frame> 'systemd-run --user --wait --pipe --quiet systemctl poweroff'`. `sudo` needs the user's password
  (not known to Claude). A reboot clears leaked kernel keyrings.
- Reinstalls keep one rollback copy (`<base>/previous-game.apk`, `settings.conf.previous`); `frameport frame cleanup`
  removes them (and `--path ~/X` extra folders under home).

## Project status (2026-10-02)
Self-update, the Files tab, OVRPort 1.2.5, non-Quest Android apps (vr_kind) and GUI localisation (tr(), 0.3.x) are
in; see the sections above. Earlier state (2026-09-29):
34 Quest games ported; the owner confirmed in the headset that all FramePort-rebuilt games work: 23 work, 5 work with
issues (Arcsmith/Time Stall eye distortion, AC Nexus some flipped launch text, Phantom DLC button, Silhouette hands),
6 can't run (Sniper Elite VR, Espire 1, HITMAN, and the 32-bit Journey of the Gods / Shadow Point / Sports Scramble).
Parity: all 34 rebuilt from the dumps match the known-good builds (`docs/parity-report.md`, generated locally and
git-ignored; 34/34 again with OVRPort 1.2.5 on 2026-10-02) and were reinstalled +
launch-tested with 0 regressions (`docs/parity-device-report.md`). `PATCHED/` holds exactly the installed builds.
Owner preferences: manual installs (no third-party installer apps), Python + Flet, dynamic data over hardcoding,
free tooling only, public repo scrubbed of personal data, keep the known-good backups.
Rift/PC VR support (2026-09-29): implemented + unit-tested, not yet tried with a real Rift game on the PC or Frame
(needs a Rift dump, and Proton installed on the Frame). Open ideas: exe-icon artwork for Rift games, macOS x86_64 bundle, USB-cable connection (Frame `usb0`, untested),
the unresolved eye distortion, and testing the release bundles on real Windows/macOS machines (never launched yet).

## Conventions
- Dynamic first: fetch live data (tool versions, overport patch list/titles, artwork, catalog) with cache + bundled
  fallback (`core/cache.py`). Don't hardcode what can be discovered (e.g. Lepton path via appmanifests).
- Keep APK edits minimal and byte-stable (parity depends on it). Every new fix: a patch module + a triage signature +
  a PLAYBOOK row + a unit test.
- Known-good backups of every working APK and the signing keys: `PATCHED/_known-good-2026-09-28/` (don't delete).
- Your development Frame: `frameport frame info` (remembered in `<user data>/frames.json`); SSH as `steamos@<frame>`.
