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
    fallback, which would show an unknown control); settings; welcome; activity panel).
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
- `catalog/games/<package>.yaml` — 38 recipes (34 verified 2026-09-28; Deadpool VR, 4XVR, NEX Player and AC Nexus's
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
`aim_pitch/aim_yaw/aim_forward`, `refresh_rate`, `layer_debug` (diagnostics). 4XVR re-creates LOCAL spaces every 2–4 s
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
changed; a maintained entry beats the user's shared copy when its `updated`/verified date is later (`catalog._newer`). Unity `boot.config` "vulkan" substring mislabels GLES games as Vulkan
(I Am Cat ran GLES); OVRPlugin's "Unavailable OpenXR extension: XR_FB_scene" is routine (no longer triaged).
**Lepton storage (2026-09-30):** each app's /sdcard (= /storage/emulated/0 → `<base>/lepton-data/external`) has `Movies`/`Download`/`Documents` symlinked to the Frame's `~/Videos`/`~/Downloads`/`~/Documents` (liblepton/mounting.sh, only if they exist at start); agent v24 `storage_targets` reads that mapping. Android's MediaProvider canonicalises paths to /home/steamos/... and rejects every file ("doesn't appear under [/system/media...]"), `sm list-volumes` is empty: the media index never works, apps must browse folders. Lepton installs with `adb install -g` (runtime permissions granted, MANAGE_EXTERNAL_STORAGE too). Files: `install/files.py`, `frameport frame send|storage`, GUI Files tab (formerly Frame → Send files).
**SteamVR per-app settings (2026-09-30):** editing steamvr.vrsettings while SteamVR runs is lost; the web API (127.0.0.1:27062 /app/setsettings) needs `x-steamvr-secret`. `native/vrsettings` = `fp_vrsettings.exe` (freestanding, OpenVR `FnTable:IVRSettings_003` as a Utility app, loads SteamVR's bin/win64/openvr_api.dll) sets them live and SteamVR persists them: section `steam.app.<shortcut appid>`, keys `preferredRefreshRate` (float) and `motionSmoothingOverride` (0 global, 1 on, 2 off, 3 always). Steam Link (vrlink) lists the Frame's rates 72/80/90/96/108/120/144 in vrserver.txt and follows the per-app preference ("host preferred N Hz"; whether the key is honoured is unverified in-headset yet). Judder metric: vrcompositor.txt session summary dropped + "Timed out. N total" (Stormland: 0 dropped but 313 timeouts in 2 min); fpsVR (`%LOCALAPPDATA%\fpsVR\*.json`, 0.1 ms histograms) gives p99 CPU/GPU ms. `pcvr.steamvr_tuning` (default on, PC only) applies on Play: highest rate whose budget ≥ p99×1.05, at least one step down, smoothing on.

**Language packs (2026-10-04, host-tested only):** overport's `libovrplatformloader.so` is a dispatcher that `dlopen`s Meta's own loader (`libovrplatformloader_meta.so` / `_meta_q1.so`, also `libpxrplatformloader.so`) and keeps its own message queue; `ovr_LanguagePack_GetCurrent/SetCurrent` are 8-byte `return 0` stubs in it, `ovr_AssetFile_GetList` forwards to Meta's loader. `frame.langpacks` (opt-in, `native/langpack`) serves `<tag>.lang` files from the game's data; `elf.hide_exports` marks the loader's exports STB_LOCAL (bionic and glibc only match GLOBAL/WEAK; verified for glibc, bionic's `is_symbol_global_and_defined` is from memory). The artifact `libfp_langpack.so` is **not committed**: build it with `python native/build.py --only langpack` (NDK), commit with SHA256SUMS, then try a game with a `*.lang` in the headset. The library logs to logcat under the tag `fp_langpack` (dirs looked in, packs found, every language-pack call), so it shows up in the game's `launch.log`; an `ovr_AssetFile_GetList` the dispatcher leaves unanswered for 1.5 s is answered with our packs alone. Deadpool VR (Unreal) accepts a pack only when its `Metadata` equals the game's own version string (`ULanguagePacksSubsystem` compares it with `%s.%s.%s.%s.%s` built from the build info, e.g. `1.0.40.356975.Quest` = versionName; found by disassembling `libUE4.so`, **not yet confirmed in the headset**): the patch writes the APK's versionName into the library (`@FPMETA@` slot, `with_metadata`), `FRAMEPORT_LANGPACK_META` overrides it.

**Unresolved (as of 2026-09-28):** Arcsmith (right-eye distortion) and Time Stall (both eyes) — swap, tracking, Valve
layers, depth, pacing ruled out. Sniper Elite VR (DEVICE LOST), Espire 1 (Mesa GL upload crash), HITMAN 3 (freedreno
crash): use PC versions.

## Releases, CI, GitHub
Maintainer-only notes (accounts, credentials, key locations) live in the git-ignored `CLAUDE.local.md`.
Public repo `github.com/spoopyghosty0/frameport` (branch `main`). Push a `v*` tag → CI (`.github/workflows/build.yml`)
tests, builds Windows x64 / macOS arm64 / Linux x64 bundles, signs, attests and publishes a GitHub Release
(`FramePort-*.zip/.tar.gz`, the CLI wheel `frameport-<ver>-py3-none-any.whl`, `SHA256SUMS.txt`,
`FramePort-selfsigned.cer`; notes = "What's new" from the annotated tag message + the short `packaging/release-footer.md`; owner: keep release
notes short — a few "What's new" bullets, nothing long after them).
Installed apps find the release themselves (self-update), so the notes are what users see in the update dialog.
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
