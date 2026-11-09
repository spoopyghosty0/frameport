# FramePort — notes for Claude

FramePort ports Meta Quest standalone APKs to the **Valve Steam Frame** (SteamOS, aarch64). Games run in Valve's **Lepton**
(Waydroid-based Android container), one container per game, launched from a Steam library shortcut. Pipeline:
`scan → analyze → suggest recipe (catalog/heuristics) → user confirms → overport → Frame fixes → sign → static checks →
install over SSH (agent) → Steam shortcut → headless launch test + log triage`.

Read `docs/PLAYBOOK.md` (symptom → fix) before debugging a game, and `docs/FRAME_RUNTIME.md` for runtime facts.
Detailed device findings per game/issue: docs/DEVICE_NOTES.md (read before debugging a game).

## Layout
- `src/frameport/` — Python package. `pipeline.py` is the API the CLI (`cli.py`) and GUI (`ui/`, Flet 1.0) share.
  - `ui/` — `app.py` shell (sidebar with Frame connection + activity cards, routing, actions, 30 s connection poll),
    `theme.py` (design tokens; change colours/spacing only there. Dark-only colour themes (owner: no light mode):
    built-ins `portal` (default), `portal_oled` (true black) and `original` (the old violet; alias `classic`). Portal =
    the logo's dichotomy: orange ACCENT = main action + selection, blue SECONDARY = secondary (outlined) buttons,
    progress, switch tracks, PC; `dual` themes add the blue→orange sidebar edge, two-colour wordmark (`C.wordmark`) and
    selected-tab fade (`C.portal_gradient`). Installable theme files `<data>/themes/*.json` (docs/THEMES.md: name,
    base, dual, any subset of `T.TOKENS`; refused if light/unreadable, `T.ThemeError`), Settings → Appearance: cards
    switch live via `app.restyle()` (rebuilds the shell, drops cached views; setting `ui.theme`), "Install theme
    file…", "Copy this theme as a file", bin icon removes. Read tokens as `T.NAME` when building — never as a default argument or module-level constant (they
    keep the import-time theme; module maps refill through `T.on_change`, `tests/test_theme.py` checks defaults)),
    `glyphs.py` + `icons/` (the logo `logo.svg`/`logo-solid.svg` = a monitor that comes out of a tilted portal as the
    Frame, and 24 px line glyphs `fp:frame|port|patch|container|test|pc|sync|live|shot|keys|recipe|link`, copied into
    the assets dir and shown as tinted `ft.Image`s: pass `G.FRAME` … wherever an icon goes, `C.as_icon` / the button
    helpers handle both kinds; `src/assets/icon.png` = the bundles' app icon), `components.py` (pill, card, callout,
    checklist, art_fill, confirm, `update()` = safe update: in Flet 1.0 reading `.page` of an unmounted control
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
    pkg)`; Copy image (card hover button, right-click, viewer) = `core/clipboard.copy_image` (Windows/WSL: PowerShell
    bitmap + file via %TEMP%, macOS osascript PNG, Linux wl-copy/xclip; Flet's set_image only in web mode)); live view (`views/live.py` + `install/livestream.py`: the Frame's built-in SteamVR "headset view" webcam
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
    game; MON_CRITICAL needs force, MON_NEVER is refused)); room (`views/room.py`, persistent, "experimental" pill:
    scan with progress + Cancel = agent v109 `room_capture_stop`, play area, delete; cache `ui/room_state.py` shared
    with the Frame page's short card, mesh cached in `<data>/rooms-cache/<frame>/`; in-window 3D `ui/room3d.py`
    RoomCanvas over `mr/view3d.py` (pure-Python projection, mesh reduced to ≤4k/≤1.5k edges at rest/moving, ≤18 fps
    worker; each redraw sends NEW shape controls: changing a Points list made Flet diff it item by item, 0.3-1 s);
    "Open full 3D view" = `mr/room_viewer.py` token server on 127.0.0.1 + hand-written WebGL `mr/viewer_page.py`,
    stopped on disconnect/close; Test Quest MR tip = `mr.TESTER_*`; editing cards `views/room_edit.py` (agent v109):
    Clean-up = fp_roomscan's refinement settings, `install/rooms.REFINE_DEFAULTS` = the owner's defaults + slider
    metadata (agent `ROOM_REFINE_DEFAULTS` must match, a test checks), saved non-defaults in the Frame's
    `rooms/refine.json`, used by every build; Apply = job → agent `room_refine` (`--refine-only` on a copy of the room
    folder from `room-raw.txt`/`room-mesh-raw.bin`, scan.json → status `report`/`labels`/`suggestions`); Labels =
    the label rules' decisions + Add as door / Add as closet; Furniture & screens = box editor (click a box in the
    canvas: `view3d.pick`) + doors/windows on a wall (`mr.room.plane_on_wall`), Save = `rooms.put_room` → `room_put
    edited` (keeps raw files, `edits.txt` survives a new clean-up; "Back to the scan's boxes" = keep_edits false); no
    eraser in the GUI (`rooms.refine(erase=)` only)); settings; welcome; activity panel). Files, Screenshots and the Library share right-click menus
    (`app.popmenu`, filled on right-click; on one of several selected items they act on the whole
    selection, `C.menu_targets`) and click-and-drag multi-select (`C.DragSelect`: pan start/end on the area + item
    hover events, which Flutter also sends with the button held; Flet can't report item positions, so no rubber band).
    Selection bars sit below the list: above it, their appearing shifted the items mid-drag. Async handlers (they
    await a FilePicker) must be coroutine functions or go through `page.run_task`: Flet doesn't await a lambda's
    coroutine (the Files row Download button silently did nothing). `ui_smoke.py --fake-frame --gestures` drives real
    mouse drags/right-clicks.
    Live Frame data comes from one app-owned `app.monitor_hub` (`frame/monitor_hub.py`): subscribers name modules +
    interval, the hub runs one agent `_monitor` stream (union of modules, fastest interval; per-module collection
    needs agent v73), reconnects after a lost stream and stops it when nobody subscribes; `_poll` retries an "error".
    Games in a sample (agent v81): FramePort's own (launch.sh / container / Proton folder), any other game Steam's
    reaper runs (`steam:<appid>`, title from the appmanifest, e.g. Job Simulator from Steam's store; not SteamVR, not
    devkit entries; End game = Steam's Exit game), overlay apps flagged `overlay` and listed last; newest first. The
    UI takes `frame/monitor.main_game(sample)` (never an overlay app alone). The agent's own workers never count.
    Subscribers: "monitor" (the Monitor tab, everything, only while shown) and "card" = the sidebar's live Frame card
    (`ui/frame_card.py` helpers: battery ring, "now playing" row with fps + 2-min sparkline → click opens Monitor;
    games + battery every 5 s, paused while a job's stage is "Upload…", only while connected and setting
    `ui.live_frame_card` (Settings → Appearance, default on) is on; else the battery comes from the 30 s poll).
    ui_smoke injects `FakeMonitorSession` as the hub's `session_factory`.
    User tags live in library entries (`tags`), filters in library setting `ui.library`.
    Easter eggs (`ui/easter.py`, owner's wish; never mentioned in the UI or docs): seven quick clicks on the sidebar
    logo (the *wordmark's* portal glows + rings, a cover is tossed out of it and falls across the window on a random
    arc: `toss_path`, `page.overlay` at the computed window position `_wordmark_portal`; the logo's portal in
    themes without one), the Monitor's fps number has a mood tooltip (above the number, clear of the pointer) and
    2.5 minutes within 0.5 fps of target (`PERFECT_SECONDS`) make the game card glow and pulse blue/orange for 5 s,
    the number grow in the portal gradient and the sparkline take it; the "Perfect pacing" pill + line stay until the
    rate slips (`PerfectPacing.done`, `_show_perfect`, `Sparkline.portal`), holiday badges on the logo that dance on
    hover and shower the window on click (`HOLIDAY_MOTION`; `SHOWERS`/`shower_plan`: New Year confetti, winter snow,
    Valentine hearts, Pi digits, clovers, hopping eggs, bouncing pumpkins; `holiday`: Valentine's
    heart, Pi Day, St Patrick's clover, Easter Sat-Mon egg (computus), April 1 = the logo upside down until hovered,
    Halloween pumpkin, Dec 31-Jan 1 party popper, else Dec 20-Jan 2 snowflake; on those days the logo's box grows to
    hold the badge: Flutter only hit-tests inside a control's bounds; `FRAMEPORT_TODAY` pretends a date),
    Library search "frameport" (logo spins, "That's me!"), Type on Frame "hello" (keys still go to the Frame; a
    headset peeks up from the window's corner and waves), the battery ring (only the ring, with a glow behind
    it; not the percentage) empties at once and spins while its bar refills, then pulses, on reaching 100 % plugged (`charged_now`, `ring_pulse`, `ring_glow`), a long upload (>2.5 min, `HOP_AFTER`) makes the transit cover hop, the Live view
    tab shows a blinking ON AIR sign while streaming
    (`app.sync_on_air`), install milestones (confetti +
    message at the 1st/10th/100th *different* game: settings `fun.installed` = game ids, `fun.milestones` = shown;
    each milestone shows once ever, recorded before it's shown; updates/reinstalls never count; the first use seeds
    from games already on a Frame and marks passed milestones shown). Setting `ui.easter_eggs` (no UI) turns them
    off; the showcase demo library has them off (steps hook `easter_eggs` for egg demos). Reduce motion: no animation.
    **Performance rules** (the app froze before): never put image bytes in controls — artwork is served by URL from the
    GUI assets dir (= user data dir; `ft.run(assets_dir=…)`), as thumbnails (`artwork/thumbs.py`, Pillow); the
    library view is persistent, streams cards in batches from a background thread and filters by toggling visibility;
    job/connection events call `app.refresh_view()` (targeted), not `render()`; no I/O in render paths.
    **Never recreate clickable controls on progress ticks** (sidebar, activity tiles): update their properties —
    replacing them 5×/s swallowed clicks (couldn't leave the Library during an upload).
    Labelled switches: `C.switch(label, …)` (Material's default label colour is dark on our dark theme).
    Cover cards (Library grid + shelf): `library.card_hover`/`hover_motion` (lift, art zoom inside the clipped frame,
    radial `scrim`, accent-tinted shadow) and `C.CoverButton` (frosted round button, accent under the pointer; Play =
    blue then orange ring ripple + "Starting on Frame…" pill for STARTING_S; its `.status` layer must sit *below*
    the button in the Stack, else it takes the clicks); the game page's Play turns into "Starting on …" with a spinner
    (`GameView._starting`). Reduce motion: no lift/zoom/rings. Filled buttons use a glyph's `-solid` variant
    (`glyphs.solid`, e.g. `frame-solid.svg`: the outline glyph faded into the orange).
    Empty states (`C.empty_state(..., features=[(icon, heading, sentence)])`): with features the portal is bigger, a
    soft portal light (`C.portal_glow`) fills the screen and `C.feature_row` says what the screen will show (the
    not-connected Files/Screenshots/Live/Type/Monitor tabs and the empty Library); the idle Live view is a framed
    "screen" with its own Start button + `live_features()`, Type on Frame shows `keyboard_tips()` under the box.
    Setup checklists (Frame → Ready to play, Settings → Tools / This PC) = `C.checklist([C.Check(ok, title, detail,
    help, fix=(label, icon, handler), extra=control)])`: ok True/False/"warn"/None (= spinner); the fix button shows
    inline only while an item isn't ready — wire existing actions only. Motion: `app.body` holds a plain
    Container (`app.switcher`, `app._show_view`): a new route gets a new wrapper sent at opacity 0 and raised to 1
    (`_fade_in`, 150 ms), a same-route redraw swaps the wrapper's content (no animation; persistent views just
    remount). Never Flet's `AnimatedSwitcher` around a view: it keys its content with a new `UniqueKey()` at every
    rebuild, and any overlay change (menu, dialog, toast) rebuilds the page → the view was rebuilt from scratch and
    its lists jumped to the top (#411); the wordmark's portal (`wordmark().data`)
    pulses once when frame_state turns "connected"; both off with setting `ui.reduce_motion` (Settings → Appearance
    "Reduce motion", `app.reduce_motion`). Settings has a fixed index column (`settings.SECTIONS` order: most users'
    needs first (Updates, which also holds game recipe + install-link updates, Installing, Library folders,
    Appearance), niche ones later (Install links, Tools, This PC), Remove FramePort last): entries scroll the section column via `scroll_to(scroll_key=…)` (sections wrapped
    in Containers with `ft.ScrollKey`), the header stays; `app.settings_view.show_section(key)` (ui_smoke
    `settings-index`).
    Help hints: wording for non-obvious terms lives in `ui/help.py` (`HELP`); show it with `C.help_icon(key)` or the
    `help=` argument of `section`/`kv` (`C.Check` help key), tooltips via `C.tip()` (wraps). Context menus
    (Library, Files, Screenshots, Monitor) and the game page's "…" are FramePort's own (GitHub #414):
    `ui/popmenu.PopMenu` = `app.popmenu.show(entries, at=(x, y) | below=box)`, drawn in page.overlay (outside-click
    barrier + panels; hover submenus with open/close intent delays, ↑ ↓ → ← Enter Esc via `app._on_key` first, closes
    on outside click/Esc/choice/`app.go`; panels size to the estimated label width between MIN_W and MAX_W, longer
    labels wrap, flip/clamp into the window, submenus on the side with room; a solid grey left edge (owner: no portal gradient) and a solid soft-accent highlight; fade off with
    Reduce motion; hovering only repaints rows). Flet's stock ContextMenu/PopupMenu can't nest and cap at 280 px, so
    they're only used for button menus (`C.menu_button`, width scaled; `C.menu_label` wraps). Game menus come from
    `app.game_actions()` = `menus.menu_sections`: right-click = Play, Install/Update, Game settings | Open game page,
    Select | Update ▸ On the Frame ▸ Saves ▸ Name and artwork ▸ Recipe ▸ Troubleshoot ▸ | Uninstall…, Remove from
    library… (`menus.group`: a one-item group stays inline); the page's "…" (quick=False) is the same without what
    its hero has; on one of several selected games `menus.selection_menu` (`library.menu_actions`); the recipe card's
    buttons = `game_actions(recipe=True)`. Picking art (`sources.apply_choice`) downloads into a staging dir and keeps the old
    art if nothing came back; "Update Steam art on Frame" re-sends the art set to the game's anchor (agent ≥ 12).
    Patch descriptions/reasons describe the general case, naming games only as "e.g. …".
  - Installs: queued/cancelled/failed ones are remembered (library setting `ui.installs`) → Library "Resume" bar;
    uploads are interruptible (Cancel checked per MiB) and resumable (big files via SFTP `.part` append, small files
    streamed in tar batches; the agent counts files already in `incoming/`). Multi-select in the Library queues
    installs after asking every needed question up front. Failures end in one pop-up (Resume / Uninstall / log).
  - Library folders (GitHub discussion #351): `core/folders.py` (no Flet) owns setting `scan.roots` (remember/add/remove/status/rescan_roots; the origin-based fallback only while the setting was never set, so removing every folder doesn't guess them back), Settings → Library folders (`settings.library_folders`, status read via run_bg), CLI `frameport folders [--add/--remove/--json]` + `frameport rescan`. Scans relink moved games (`pipeline.ScanReport`: a library game found elsewhere whose own files are gone and the copy is the same build (Quest: source_sha256, else versionName; Rift: same program path + size from the fingerprint, matched by game id or, renamed, by program) → only paths change (`relink_quest`/`_relink_rift`), user fields/builds/installs stay; another version → Scan a folder… takes it via `replace_apk(found=)`/`replace_rift_game`, Rescan only reports it; the old copy still there → kept). `add_game` no longer overwrites a user recipe (refreshes `suggested` only). Linux apps aren't scanned. PC installs of a moved Rift game (Steam shortcut path) untested.
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
    game log + crash summaries from the Proton prefix; triage `unreal-crash`. Agent v67 adds Unity's logs (LocalLow/<Company>/<Product>/Player(-prev).log via `<Name>_Data/app.info`,
    `output_log.txt`, Temp/…/Crashes/*/error.log; `unity_logs`) to launch tests and diagnostics; triage `unity-vr-init` /
    `unity-crash`. A catalog recipe verified with another build (catalog `xr` ≠ the build's) keeps the build's own
    `pcvr.launch_args` (`engine._other_build`; GitHub #105: SUPERHOT VR's Oculus+OpenVR build needs `-vrmode OpenVR`,
    the catalog's is the OpenXR build); Electron launchers next to a game rank −30 (`rift.is_electron`). Lies Beneath via Proton without Revive
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
  - App bundles (discussion #482, `sources/bundles.py`, docs/DEVICE_NOTES.md#app-bundles-xapk--apks--apkm--apkx-discussion-482):
    .xapk/.apks/.apkm/.apkx files and folders of split APKs are unpacked + merged into ONE APK (Lepton: one APK per app;
    OVRPort: one APK) in `<data>/bundles/<pkg>/<key>/` (lib/assets splits in Python, resource/feature splits via
    APKEditor `tools/apkeditor.py`); arm64-v8a split only; library field `bundle`; merged APKs are unsigned (builds sign).
    FrameDrop itself takes no bundles; `.apkx` is no defined format (read as a zip of APKs); old encrypted .apkm: refused.
  - Scanning: SideQuest backups (`<package>/<time>/apk|obb|data`) and AXRB downloads (`AXRB/<app id>/<binary id>/base.apk`
    + OBBs/assets; its `patched/`/`*-axrb.apk` PC builds are skipped) are recognised (`sources/quest_dump.py`, FAQ).
  - Quest/Rift twins stay separate entries, shown and named in Steam "Title (Quest)"/"(Rift)" (`core/titles.py`).
  - Rename… (game menu "Name and artwork", Library right-click; CLI `frameport rename <pkg> "<name>"|--reset`):
    `pipeline.rename_game` sets `title` + `title_locked` (+ `auto_title` = the automatic name for reset; rescans, Rift
    store matches and links then only update `auto_title`; a locked name gets no twin suffix); `sync_title` → Frame
    agent v77 `rename` (deployment.json title, same shortcut appid: upsert matches by Exe) + art + one Steam restart, PC
    `PcReviveTarget.rename`; an unreachable Frame → `steam_name_stale` (game page "Update name on Frame", next install).
  - Recipe picker (GitHub #508: a repack's package id `ru.camouflaj.manta` never matched `com.camouflaj.manta`):
    Recipe ▸ / recipe card "Use a recipe from the catalog…" = `views/recipe_picker.py` (search pre-filled with the
    game's name; `catalog.search`: fuzzy title/package, same package core (`package_core`) first and preselected;
    preview = `pipeline.preview_catalog_recipe`: patches `drop_inapplicable` left out, `engine.pick_warnings` for
    engine/VR API/32-bit/kind), CLI `frameport recipe <pkg> --find "<name>"` / `--from <catalog pkg>`. Stored as a
    derived recipe with `Recipe.recipe_source` = the entry's package: `engine.suggest(a, pick=)` derives it, so
    `_follow_catalog`, rescans (`add_game`/`add_rift_game`) and re-analyses (`_store_analysis`) keep it and it follows
    that entry's revisions (edited by hand → the usual `catalog_update` offer from that entry); "Reset to suggested
    recipe" ends it; a picked entry gone from the catalog falls back to the normal suggestion. Status "unknown"
    (the entry's "unsupported" stays), the game keeps its own name.
  - Newer versions (GitHub #161): game menu "Update with a newer APK…" (`pipeline.replace_apk`, outdated via
    `build.source_version`) and, for Linux apps / PC VR games, "Update with a newer version…" (`replace_linux_app` /
    `replace_rift_game` via `add_linux_app(replace=)` / `add_rift_game(replace=)`, CLI `frameport new-version`): the
    entry keeps package, name, art, tags, switches, user recipe (a confirmed exe if the new folder has it); a new
    `source_id` goes into the deployment's `recipe` dict at install (no agent change) → `C.source_replaced` = outdated
    (Frame + `pc_outdated`); such an install re-sends same-size files changed after the last install (mtime/ctime,
    `installer.changed_since`); the old Rift folder is in `replaced_dirs` (rescans skip it). Untested on the device.
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
  - Save backups (discussion #352, agent v95; docs/FRAME_RUNTIME.md "Save backups"): agent `_backup_saves`/
    `_restore_saves '<json>'` stream a tar.gz of `lepton-data/internal/<pkg>` + `external/Android/data/<pkg>` (caches,
    IL2CPP/shader caches, FrameBridge files, videos/OBBs, files >64 MiB left out; manifest `frameport-saves.json` last)
    over the exec channel, file work in `podman unshare` (`_saves_ns`), launch lock held, refused while running;
    restore = untrusted input (staged + checked first), rollback copy `<base>/saves-before-restore.tar.gz` (undo =
    `rollback: true`). PC `install/saves.py` (`<data>/save-backups/<pkg>/<ts>.tar.gz` + `.json`, keep `saves.keep`
    = 10), GUI `ui/views/saves_dialog.py` (game menu section "Saves", Frame page "Back up all saves", uninstall's
    "back up before deleting"), CLI `frameport saves backup|list|restore|undo`. Agent v101: PC folder `saves.folder`
    (Settings → Data; old folders stay listed in `saves.previous_folders`), automatic snapshots `saves.auto` (default
    off; flag `~/.local/share/frameport/save-snapshots/enabled`, Quest launchers run `_snapshot_close` in the EXIT
    trap under the launch lock, newest 3 per game on the Frame; `install/saves.sync_snapshots` imports them as
    "automatic" once, ledger `<data>/save-snapshots-synced.json`). Device-checked 2026-10-17 with BlazeRush.
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
    chars; `*_links` = `IssueLinks(form, plain, body)`: body = the Markdown the form writes (`### <label>`, recipe in
    a ```yaml block; labels checked against the templates), plain = `?title=&body=` link. GitHub #167: some (Flatpak)
    browsers opened the form with empty fields → `app.open_issue` copies the body to the clipboard and offers "Form
    empty? Open a plain issue"; keep the templates valid YAML, quote `: ` in descriptions). "Share working config" →
    `working-config.yml` issue → maintainer label `catalog-accepted` (a plain issue needs `working-config` added by
    hand) → `catalog-from-issue.yml` workflow (`scripts/catalog_from_issue.py` validates) opens a catalog PR. App log:
    `core/applog.py` (`<data>/logs/app.log`, finished GUI jobs in `<data>/logs/jobs/`).
  - Announcements (2026-10-15, announcements/README.md; app only, the owner dropped the site pop-up): source of
    truth `announcements/current.md` on `main` (front matter id/title/date + optional image/image_alt/button+url/
    expires/active, Markdown body; ships `active: false`). `announcements.py` (no Flet; raw GitHub via
    `cache.cached_text`, 6 h) + `ui/announcer.py` (pop-up 20 s after start / every 6 h when no dialog is open and not
    on welcome; `C.dialog` with the portal edge, image downloaded + Pillow-checked into `<data>/announcements/`, shown
    by asset URL). Shown only if the user was around before `date` (setting `announce.since`, set once at the GUI's
    first start: 0 when `USED_BEFORE` settings exist, else now), once: any close (X, outside click, Escape, button)
    stores the id (`announce.dismissed`, last 50). New announcement = new id; same id = typo fix.
    `FRAMEPORT_NO_ANNOUNCEMENTS`; `ui_smoke.py --announcement` renders it.
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
    replaced files kept in `updates/<ver>/previous` because the zip has no top folder; nothing touched while a program file
    is in use, old-only files of app/Lib/DLLs/site-packages/data moved to `previous`, a failed copy deletes what it
    added (#476: the old rollback kept new modules → old version crashed at start); bundles carry
    `frameport/_bundle_files.txt` (package.py) → `core/bundle_files.check` at GUI start removes/ignores leftovers and
    reports a mixed install; macOS/Linux: mv to `.old` + swap,
    rollback, `xattr -dr` quarantine → relaunch; `<data>/logs/update.log`). `ui/updater.py`: background check (10 s,
    then 6 h), sidebar card (hidden control, toggled), Library bar (`library_bar`), changelog dialog, job
    `app-update` that restarts only when no other job is active, `apply_pending_at_start()` in `main()` for
    "Install updates automatically"; Settings → Updates. Changelog: `updates.fetch_changelog` (releases list,
    `app-releases.json`, same 6 h cache, offline → cached / []) → `ChangelogEntry` (`whats_new()` keeps only the tag
    message's "What's new" part: CI's intro, update hint and release-footer.md stripped); `between`/`changelog_for`
    (installed < v ≤ offered, dev entry only when on/choosing dev, skipped versions included), `recent_history`;
    the update/dev dialogs show one collapsible section per version (`_changes_column`, newest open); after an update
    `pending_news` (setting `update.last_seen_version`; first start only records it; offline → retried next start)
    → Library "was updated" bar → "See what's new…"; Settings → Updates "What's new…" = recent history. CLI: `--version`, `frameport update [--check (exit 10)]
    [--yes]`, once-a-day stderr hint read from the cache only (`refresh_cache()` in a daemon thread writes no
    settings, to avoid read-modify-write races). `scripts/update_smoke.py <archive>` runs the real extract + swap script
    (no relaunch) — CI runs it on all three OS with the archive it just built.
    Verified end to end (2026-10-02): real 0.3.3 bundles on Windows and Linux (Ubuntu 22.04/WSLg) found, staged and
    installed 0.3.4 ("Install updates automatically" path) and relaunched as 0.3.4 with settings kept. Not yet clicked
    by hand: the "Update now" button path (same apply(), called from the running app). macOS: CI smoke only.
- `agent/frameport_agent.py` — runs **on the Frame** (python3 stdlib only), JSON over SSH. Owns the install layout,
  launch.sh template, Steam shortcuts (binary VDF), launch tests. Bump `AGENT_VERSION` when changing it.
- `bootstrap/bootstrap.sh` — one-time Frame setup served by the pairing server (app key, podman fix, Developer Mode, Lepton).
  `bootstrap/setup.sh` — the setup URL (`curl -fsSL https://frameport.app/s | bash`, copied to the site by
  `site/scripts/sync-media.mjs`): finds the PC (stdlib mDNS on 5353 for `_frameport-pair._tcp`, which the pairing
  server announces while open; else the USB address and a /24 scan of `/ping`), `/hello` → the user clicks Allow (4
  digits on both sides, `ask_digits`) → `/wait` hands over the code → runs bootstrap.sh exactly like the typed line.
  Checked on the dev Frame 2026-10-10 (mDNS and scan paths) with a stub bootstrap; tests/test_setup_url.py.
  Auto-pairing (`frame/autopair.py`, `app._start_auto_pair`, setting `frame.auto_pair`, default on; off with
  FRAMEPORT_HOME/FRAMEPORT_NO_AUTO_PAIR): while no Frame is connected, Frames in Developer Mode (devkit mDNS, else a
  30 s scan of the /24s for :32000/login-name, deduped by host key) are connected if FramePort's key works, else
  offered the key via devkit.register every 4 s (refused at once until Pair new host is open; 45 s back-off after an
  unanswered request). This WSL dev PC never hears the Frame's mDNS (Hyper-V firewall): the scan finds it.
- `catalog/games/<package>.yaml` (installed apps also fetch these from GitHub `main`, see "Catalog updates") — 38 recipes (34 verified 2026-09-28; Deadpool VR, 4XVR, NEX Player and AC Nexus's
  90 Hz default confirmed later by the owner); `catalog/triage.yaml` — log signatures → fixes.
- `native/` — sources of the prebuilt binaries in `artifacts/` (adapter, VrApi bridge patches, GL shim, stubs).
  `native/build.py` rebuilds them with NDK r27c (downloaded on demand into `native/.cache`, git-ignored; uses
  `-ffile-prefix-map` so no local paths get embedded; zip symlinks are restored as copies). Users never need the NDK.
  `native/a32translator/` (not an artifact, not shipped) = `a32run`, an AArch32 → AArch64 user-mode translator
  (interpreter + JIT, ARM/Thumb-2/VFP/NEON) for running 32-bit ARM Linux programs on the Frame; its README has the
  design, the qemu-arm differential fuzzer and program tests. `native/a32translator/bridge/` = the in-APK Android
  bridge (32-bit JNI/NativeActivity libraries in a 64-bit app: stub + liba32bridge.so + guest runtime liba32rt.so;
  harness with a fake JVM; test APK org.frameport.a32test passes in Lepton; GLES/EGL, OpenSL ES via AAudio, VrApi 1.x
  -> the 64-bit VrApi bridge, controller input as Gear VR touchpad/back events; `bridge/convert.py` converts an APK).
  First game: AFFECTED The Manor (Gear VR, Unity 5.6 + Mono) runs in the headset with picture and sound
  (2026-10-10); Spunky Dungeon (Unity 2019.2, OVRPlugin 1.46 on VrApi 1.31, Platform SDK) plays at 72 fps.
  In FramePort: patch `frame.a32_bridge` (`patches/frame/a32_bridge.py`: `convert()` is shared with the dev script
  `bridge/convert.py`; artifacts `liba32bridge.so`/`liba32rt.so`/`liba32stub.so` from `native/build.py --only a32`)
  is suggested for every 32-bit-only APK; the recipe then has `overport: False` (OVRPort is 64-bit only; the patch
  ships OVRPort's loader chain + its 32-bit platform dispatcher itself, `overport.ensure_runtime_lib`), only
  frame.a32_bridge/adapter/launcher of the frame patches, and `device.text_input_window` for Unity (`engine.a32_recipe`,
  also run by `pipeline.set_recipe` + builds, so a bridge turned on by hand drops OVRPort & the 64-bit fixes; which
  patches work there = `base.works_on_a32` (`Patch.a32`; overport/frame off unless `a32 = True`, adapter settings in
  `settings.XRSHIM_SETTINGS` off); `build.check_a32` refuses anything else, `engine.warnings(recipe, analysis)` says
  so; a catalog entry for a 32-bit game that neither names nor removes the bridge predates it: bridge kept, status
  unknown; parity gives bridge builds without a bridge known-good "new build (no baseline)"; library migration
  `a32_bridge_on` turned the bridge on once in every 32-bit recipe, user ones too, unless the catalog's frame_remove
  names it).
  Analysis `extra.headset` (ANALYSIS_VERSION 15): gearvr / go / quest → "Gear VR" / "Oculus Go" platform + Steam tag.
  Catalog: `headset:` field; com.fallen.affected, net.mobilemischief.spunkydungeon, com.ccpgames.gunjack (works, 2026-10-12). Per-game a32 findings (AFFECTED, Spunky Dungeon, Gunjack's signature check `fp_a32_signature.txt`, synthetic Gear VR input, Quest builds `fp_a32_headset.txt` / `quest_touch`, recenter, debugging aids `A32_BREAK` / `A32_STRACE` / `A32_PERFMAP`, translator profiling): docs/DEVICE_NOTES.md#a32-bridge-games-lessons-debugging-aids-profiling.
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
  managed tools. **The owner's real app data is the Windows copy's: `%LOCALAPPDATA%\FramePort`** (= `/mnt/c/Users/
  <user>/AppData/Local/FramePort`; tools, overport workspace + game keystores, library, artwork, frames.json, the app's
  SSH key which the dev Frame authorizes; moved from WSL's `~/.local/share/frameport` on 2026-10-11, which is now only
  a stale backup). The desktop shortcut "FramePort (dev)" runs the source checkout natively on Windows
  (`%LOCALAPPDATA%\FramePort-dev\launch-frameport-dev.vbs`: venv `FramePort-dev\venv` via `UV_PROJECT_ENVIRONMENT`,
  `uv sync --frozen`, log `FramePort\logs\dev-gui.log`). **WSL runs on that data need `FRAMEPORT_WINDOWS_DATA=1`**
  (set in `.claude/settings.local.json`; `paths.shares_windows_data`): the data folder becomes the Windows one, saved
  paths from the other side are translated on load (`paths.localize`: /mnt/d/X <-> D:\X in library.json, tools.json,
  PC deployment records), Java/Cpp2IL keep one copy per OS (tools.json `java-<os>-<arch>`), and the link handler isn't
  registered (it would point the Windows copy's links at WSL). Don't run a WSL GUI and the Windows GUI at once
  (the gui.lock flock/msvcrt locks don't see each other; only the gui.alive heartbeat does).
- Device checks: `frameport test <pkg>` / `frameport parity-device --results <parity.json> --baseline <launch.txt>
  [--test-only]`; the pre-FramePort baseline is `PATCHED/_known-good-2026-09-28/_frame-state/baseline-launch.txt`.
- Docs screenshots + videos (docs/SHOWCASE.md): never by hand. `uv run python scripts/showcase/render_docs.py`
  renders `docs/showcase/shots.yaml` into `docs/images/` (every shot twice, kept when both agree; only visibly changed
  files are rewritten; `--check` for CI) and `scripts/showcase/record_video.py [names] [--draft]` records the
  storyboards `docs/showcase/videos/<name>.yaml` (tour = FramePort in use + README teaser; install = the install
  tutorial: instruction cards for the download / the command on the Frame, the rest filmed from a first start) into
  `docs/media/frameport-<name>.mp4`. Both build the demo library first (`demo_home.py`, profile `demo` or `fresh`:
  catalog games from `demo-library.yaml` + `demo-analyses.json`, art/details cached in ~/.cache/frameport-showcase, a
  pretend Frame from `showcase-frame.json`) in a temp FRAMEPORT_HOME: no personal data, refuses the real data dir
  and placeholder art; discovery is off and the setup command shows placeholders for the address and code. Steps find elements
  by name through Flutter's semantics tree (`scripts/showcase/web.py`: text, tooltip; the innermost match wins) —
  give new icon-only controls a tooltip. The `showcase` workflow renders on UI pushes to main (+ the videos whose
  storyboard changed, or the ones named on dispatch) and opens PR `showcase/update`; releases attach every video as
  `FramePort-<name>.mp4`. `scripts/scrub_library.py` (the owner's own library) is only for private screenshots now.
  README rules (owner): says not every game runs (no longer "proof of concept" since 1.0), provides no piracy tools, credits the wrapped
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
- One GUI per data folder (`core/instance.py`, 2026-10-11): `<data>/gui.lock` (flock/msvcrt, pid inside); a second
  start asks the open window to come to the front (`gui.focus`, read by `_watch_links`) and exits; a lock held with no
  window (no `gui.alive`) and older than 30 s is a leftover FramePort process and is ended. `main()` ends the process
  (`os._exit`) once `ft.run` returns: background threads (re-analysis, session triage) used to keep a windowless
  process running. Session triage shows one pop-up for all games of a burst (`_announce_sessions`).
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
- No AArch32: 32-bit-only APKs fail with `INSTALL_FAILED_NO_MATCHING_ABIS` → `frame.a32_bridge` (the a32 translator inside the app, see native/a32translator below).
- GLES swapchains: only `GL_SRGB8_ALPHA8`/`GL_SRGB8` (35907/35905), no MSAA. Vulkan: format 43 (sRGB) but not 37 (UNORM).
- Missing: XR_FB_passthrough (emulate via ALPHA_BLEND), XR_FB_scene/spatial entities (emulated room from STAGE bounds),
  XR_KHR_composition_layer_equirect2/cylinder, XR_FB_composition_layer_image_layout (flip emulated by Vulkan blit).
- `xrConvertTimespecTimeToTimeKHR`/`xrConvertTimeToTimespecTimeKHR` return FUNCTION_UNSUPPORTED → adapter emulates
  (offset = predictedDisplayTime − period − CLOCK_MONOTONIC, sampled in xrWaitFrame).
- No XR_FB_display_refresh_rate (`xrGetDisplayRefreshRateFB` -7), yet OVRPort's dispatcher offers it and answered 0 Hz
  → FrameBridge always answers with the measured rate (1/predictedDisplayPeriod, else 72). PreyVR (#306, idTech 4:
  `USERCMD_MSEC = 1000 / GetRefresh()`) had 0 ms tics: async thread spinning, `com_frameTime` stuck at 0, menu black.
- Guardian STAGE bounds report 1×1 m (scene emulation uses ≥1.5 m). OVRPlugin treats that rect as a configured
  boundary but can't outline it ("ovrpBoundary_Outer is not supported"): Demeter's guardian check indexed an empty
  array → with `scene_emul` the app's STAGE `xrGetReferenceSpaceBoundsRect` returns XR_SPACE_BOUNDS_UNAVAILABLE.
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
scan redraw ran never closed: "dropped a patch for unknown control"); it also makes property writes (Flet's
`Prop.__set__`) take the same `app.FLET_LOCK`: a property set on a background thread while the loop packed a patch
changed the control's `_values` mid-iteration ("dictionary changed size during iteration", a sidebar click's page
change died while the live Frame card took a sample). Anything that serves the app (ui_smoke, showcase) calls it too.
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

**Upload speed:** home Wi-Fi caps uploads at ~11-18 MB/s whatever the SSH method; the Frame's hotspot (`wlanap`) ~83-97 MB/s; `Frame.fast_link()` (installer `transfer_link`, uploads ≥64 MiB) prefers usb0 > wlanap when the host key matches. → docs/DEVICE_NOTES.md#upload-speed
**Platform SDK detection** must include delay imports (`PEInfo.delay_imports`), modular Unreal's
`*-OnlineSubsystemOculus-*.dll` and Ready At Dawn's `pnsovr.dll`: Robo Recall, Lies Beneath and Lone Echo I/II all use
it; without the Oculus app they crash with 0xc06d007e (triage `delayload-missing`) — not fixable legitimately.
GUI: Frame → Installed → folder icon = file browser (agent `list_files`, flags files missing vs the install
manifest); Settings → About shows the bundled agent version and the Frame's.

**Revive under Proton arm64:** Revive's Detours hooks don't take effect (Wine's kernelbase is ARM64EC); a system32 `LibOVRRT64_1.dll` symlink gets past -3001 to -3021 = ovrError_LibSignCheck (Oculus-signed runtime required; not bypassed). → docs/DEVICE_NOTES.md#revive-under-proton-arm64-lone-echo
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
**OVRPort 1.2.5 (2026-10-01):** `patch` rejects unknown/duplicate args, unknown ids and an empty `--patches=`; it adds `libovrplatformcompat.so` itself (`frame.ovrplatformcompat` then skips). New patches `patch_ac_nexus_no_appsw_72/_90` (AC Nexus build 207706 only; `_90` = catalog default; STRICT patches only from a catalog recipe where `applies()` holds), `patch_disable_meta_xr_audio_telemetry`, `patch_vrapi_openxr` (OVRPort's VrApi translator fails on the Frame → keep `frame.vrapi_bridge`). The owner prefers OVRPort's fixes over ours where they work as well. `frameport install <pkg> --apk <file>` installs a test build. → docs/DEVICE_NOTES.md#ovrport-125
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

**Quest controller mode:** adapter `quest_buttons` (0-3, per game + library `input.quest_buttons`) suggests Frame Controller bindings translated from the game's Touch set (`suggest_frame`, mapping docs/input/quest_buttons.json = single source for C table, tests, diagram `scripts/controller_diagram.py`); runtime accepts them headless. Mode 3 "One hand" (#570) can't be bindings alone: `quest_onehand.c` attaches its own action set and answers the game's left X/Y + left-stick state queries from the right X/Y + left d-pad. → docs/DEVICE_NOTES.md#quest-controller-mode-quest_buttons-2026-10-17
**Steam Frame controller models:** adapter setting `controller_models` (`native/adapter/render_model.c`, emulates XR_FB_render_model) + agent `install_controller_models` (Frame's SteamVR models → glb at finalize); no catalog game uses runtime models; Valve's models are never committed or copied off the Frame. → docs/DEVICE_NOTES.md#steam-frame-controller-models
**overport's dispatcher (`libopenxr_loader.so`) only forwards functions in its own table** (`overportOXR: Unknown
proc addr: …`), so adapter emulations of functions it doesn't know are unreachable. For XR_FB_render_model,
`native/xrshim` fills the gap: OVRPlugin's `dlopen("libopenxr_loader.so")` string is rewritten to the shim (see
native/README). Verified on the device with Toy Master (2026-09-30): `extension shim: xrLoadRenderModelFB -> FrameBridge`.
Toy Master doesn't request a model after that (it uses its own), so the glb loading path is only unit-tested.

**Crash backtraces:** Lepton writes tombstones only to `lepton-logcats/steamlaunch-<appid>/logcat-crash.log` (launch tests return `crash_log`). Fossilize loads into every app (userdebug guest) → `frame.vk_sanitize` (`native/vkshim`); overport's dispatcher aborts on swapchains > 4096 px → `frame.swapchain_limit`; cylinder layers → quad strips (`cylinder_strips`). Drawing equirect layers ourselves and a focus debounce were tried and removed: don't retry without a new approach. → docs/DEVICE_NOTES.md#crash-backtraces-fossilize-swapchain-limit-cylinder-and-equirect-layers
**360° layers / 4XVR:** the Frame's Android runtime composites only quad + projection layers (quads always above projection layers). Per-game adapter settings (off by default): `equirect_emul` (GLES), `equirect_flip/face/res/fps/stereo`, `stable_local`, `focus_hold` (default on), `aim_pitch/aim_yaw/aim_forward`, `refresh_rate`, `layer_debug`; agent `link_media` / `app_media_dirs` put sent files into a player's own folder. → docs/DEVICE_NOTES.md#360-layers--4xvr
**Companion apps / intents:** a second APK can run inside a game's Lepton container, but Lepton turns every `android.intent.action.VIEW` with data into `steam://openurl` → "open in external player" hand-offs can't work; Lepton installs exactly one `*.apk` per app folder. → docs/DEVICE_NOTES.md#companion-apps--intents-stremio--4xvr
**Browser stand-in (#466):** Lepton's `ActivityStarter.execute` takes every VIEW intent before resolution → `steam://openurl` into `/lepton/steam.pipe`; apps that first query for a browser (Plex) find none → opt-in `frame.browser_handoff` (activity + `dex/browser-handoff.dex` in the app's own APK, analysis `browser_query`); a 2D app with an APK patch on is built, not as_is (`engine.flat_as_is`). → docs/DEVICE_NOTES.md#browser-stand-in-for-sign-ins-plex-github-466
**Text input:** Lepton's Android has no IME and VR apps run headless (no focused window); `lepton-show-flatscreen` keeps VR and gives focus + Steam's keyboard (`device.text_input_window`); `frame.unity_text_input` patches Unity's TMP_InputField/InputField at Cpp2IL offsets; agent `_keyboard` = uinput "Type on Frame". `pm install` of a second APK corrupts the main app (`cmd_real package install` skips the hook). Its window is a gamescope "desktop game" overlay: SteamVR auto-theaters it before the OpenXR session and opens the dashboard on it at session start (controllers = mouse on the Android screen, AC Nexus/#476/#477) → the launcher (`_theater_hold`, first thing) and dashboard worker keep `dashboard.autoShowGameTheater` off while such a game runs (agent v119, holds per launcher pid in `theater-holds/`); SteamOS 0.5.6 logs the dashboard button as `open_dashboard_action`. → docs/DEVICE_NOTES.md#text-input-stremio-vr, #typing-window-takes-the-controllers-steamvr-theater-mode-476-477-ac-nexus
**User reports #4-#10:** shortcuts go to the signed-in account (agent `library_users`), `launch` errors `NOT_IN_LIBRARY`; shared Cpp2IL run (`unity_text_input.Il2cppReturnPatch`) for `frame.unity_runtime_msaa_off` and `frame.unity_multipass`; VR4 → catalog `use_alt`. **Catalog changes reach existing games**: recipes store `catalog_rev`, `library._follow_catalog` re-derives non-user recipes. → docs/DEVICE_NOTES.md#user-reports-2026-10-04-issues-4-10
**Round 2 of reports / headset rounds 2-3 (2026-10-04):** vkshim `vk_shader_fix` (VR4); `Il2cppReturnPatch.field_loads` (I Am Cat); builds record `recipe_fp` (changed recipe/revised patch → "Update on Frame"); `Frame.relocate`; adapter `profile_remap` (Touch Plus/Pro); `sync_guard` (Myst); `focus_hold` tuning; ovrpshim for Unity 2017 (Accounting+); Steam library to every account, `shortcuts_lost` / `add_devkit_entry`; no clipboard service → `frame.sdl_clipboard`, `frame.godot_clipboard`; 2D apps keep non-VR patches (`needs_vr = False`); `frame/local.py` (FramePort on the Frame); after every app update `library._follow_catalog` re-derives non-user recipes once (`recipes.app_version`, `library.REFRESH_ON_UPDATE`). Analysis fields added later: **bump `analysis/detect.ANALYSIS_VERSION`** (re-analysis at GUI start + `pipeline.refresh_analyses`; `analysis_failed`). Roblox and BONELAB crash (unsupported). → docs/DEVICE_NOTES.md#round-2-of-reports-headset-rounds-2-3-2026-10-04
**Round 4:** XR_KHR_android_surface_swapchain is listed but unsupported → adapter `surface_emul` (default on, `native/adapter/surface_swapchain.c`); save folders without u/g+rwx → launch.sh `fix_perms`; Lucky's Tale OOM (no fix); `perf` works as steamos (gdb blocked); QuestCraft JRE, WiiCompiled import by hand (Lepton has no document picker). → docs/DEVICE_NOTES.md#round-4-2026-10-04-surface-swapchains-save-folder-permissions
**Steam library failures (GitHub #21/#30/#41/#42):** on some Frames Steam never loads FramePort's shortcuts.vdf entries (Play → AppError_9) → fallback: register through Steam's devkit interface (`devkit-1 … create-shortcut`, `~/devkit-game/<id>/`; `devkit_appid`, `devkit_appid_from_log`, `devkit_unregister`, `copy_grid_art`; ids: letter first, letters/digits/_ only); `collect_diag` adds `steam_library`. Cause still unknown. → docs/DEVICE_NOTES.md#steam-library-failures-and-the-devkit-fallback-github-21304142
**Exit game (GitHub #36):** `Apps.TerminateApp` (Steam CEF devtools on 127.0.0.1:8080) closes games; launch.sh ends the game when its parent reaper is gone (`upgrade_launchers` adds it). → docs/DEVICE_NOTES.md#exit-game-github-36
**SteamVR dashboard at game start:** launch.sh's `_dashboard_worker` hides the dashboard via `SteamClient.OpenVR.VROverlay` (agent `steam_js`) from the first FrameBridge `new layer:` line (120 s, ≤10 times, stops after `toggle_dashboard_action`); log `<base>/dashboard.log`, opt out `FRAMEPORT_KEEP_DASHBOARD=1`. → docs/DEVICE_NOTES.md#steamvr-dashboard-at-game-start
**Space warp / ITR2:** OVRPort's dispatcher offers XR_FB_space_warp itself (`patch_disable_space_warp` doesn't reach UE5) → FrameBridge `hide_space_warp` (per game, off; suggested for UE5 + OVRPlugin). Games that *require* space warp (OutRun, #336: "AppSW recommended buffers unavailable") → `space_warp_emul` (`native/adapter/space_warp.c`: MV size, FrameBridge-owned MV/depth images, info stripped; overrides hide_space_warp). `equirect_emul` switches itself off on Vulkan. → docs/DEVICE_NOTES.md#space-warp--itr2
**PR #34 (Lucas-Mathieu):** `native/adapter/audio_metadata.h` keeps Meta XR Audio Wwise metadata alive (Batman: Arkham Shadow 1.4.1 smoke-bomb crash). Outside PRs: review source, rebuild `artifacts/` ourselves, merge locally. → docs/DEVICE_NOTES.md#pr-34-meta-xr-audio-metadata-batman
**Heuristics eval (2026-10-04):** 19/44 recipes exact (superseded by the 2026-10-15 numbers under Heuristics). → docs/DEVICE_NOTES.md#heuristics-eval-2026-10-04
**Power (0.9, agent v47):** sidebar bar below the Frame card: Sleep / Restart / Shut down, each confirmed (`app.frame_power`, a running game asks again). logind answers CanSuspend/CanReboot/CanPowerOff = "challenge" for an SSH session but "yes" inside a user unit, so agent `power` runs `systemctl suspend|reboot|poweroff` from a transient `systemd-run --user --on-active=3` timer (the command answers first). Restart verified on the device (back in ~84 s, new boot id).
**Proton default:** the newest **stable** ARM64 Proton (`pick_proton`; owner's headset A/B); `pcvr.proton_tool` dropdown (Experimental = alias `proton-experimental`), `ensure_proton`, `proton_alternative_worth_trying`. → docs/DEVICE_NOTES.md#proton-default-stable-vs-experimental
**USB cable link:** `usb0` is a USB NCM gadget (Developer Mode only); Frame 10.86.200.233, PC **10.86.200.234** (`frame/usb.py`); ~37 MB/s; `scripts/usb_autotest.py`, `frameport frame usb-check`; discovery prefers USB > hotspot > network (`discovery.dedupe`). → docs/DEVICE_NOTES.md#usb-cable-link
**Catalog updates without a release (2026-10-05):** apps fetch `catalog/games/*.yaml` from GitHub `main` (`recommend/catalog.refresh_remote`: one API call `git/trees/main?recursive=1` + raw.githubusercontent downloads of changed blobs, cached as `catalog-gh-<sha>.yaml`, every 6 h; GUI at start + Settings → Data "Check now"; CLI background thread + `frameport catalog-update`). `catalog.load()` only reads the cache. A remote entry is skipped (`unusable_reason`) when it names a patch/adapter key this app doesn't register, has a field `CatalogEntry` doesn't know, or `min_app` > this version: add `min_app` (or rely on new patch ids) when a config needs an unreleased FramePort. Remote beats bundled unless the bundled entry has a later updated/verified date; the user's own entries still win as before. Off: setting `catalog.auto_update`, env FRAMEPORT_NO_CATALOG_UPDATE; FRAMEPORT_CATALOG_URL still overrides the source. So: **pushing a catalog change to main reaches users within 6 h** (their recipes follow via catalog_rev → Update on Frame). `catalog/triage.yaml` comes from the same tree listing (cached `triage-gh-<sha>.yaml`, index key `triage`; a new copy → `triage.reset()`; with FRAMEPORT_CATALOG_URL: that folder's optional `triage.yaml`): `validate/triage.database()` = bundled merged with remote per signature id (remote wins unless the bundled one has a later `updated:`; bundled-only ids stay), each remote signature dropped (bundled one kept) when malformed, it has an unknown field, suggests a patch/adapter key this app lacks, or `min_app` (per signature or top-level = whole file) > this version; `triage.skipped()`, `frameport catalog-update` lists them.
**Start activity / QuestCraft / ITR2 pop-in:** `frame.start_activity` (analysis `extra.vr_activity`, `axml.set_start_activity`; OVRPort gives every MAIN activity LAUNCHER, so take the VR activity from the original APK); Unreal reads user config from **internal** storage; Lepton passes `LEPTON_ENV_<NAME>` to the app as `<NAME>` (QuestCraft `LIBGL_NOERROR`); vkshim `vk_query_slots` left off. → docs/DEVICE_NOTES.md#start-activity--questcraft--itr2-pop-in
**Vulkan validation / ITR2 solved:** bundle Khronos' `libVkLayer_khronos_validation.so` into a test APK + vkshim `vk_validation=1` (logcat tag VALIDATION); `vk_spec_fixes=1` drops ITR2's invalid depth resolve; VALVE_rpo removed its fog (catalog `lepton_env: VK_INSTANCE_LAYERS`). Agent `not_started` waits 30 s for "Boot complete!". → docs/DEVICE_NOTES.md#vulkan-validation--itr2-solved
**GitHub #38/#39:** SteamVR rejects image rects past their swapchain (-25) → FrameBridge `rect_clamp` (default on), triage `swapchain-rect-invalid`; The Room VR MSAA crash in libgallium. → docs/DEVICE_NOTES.md#github-3839-powerwash-simulator-the-room-vr
**Headset round 2026-10-05:** FrameBridge `pose_consistency` (I Am Cat judder), xrshim `haptic_fix` (Lucky's Tale), **`focus_hold` on by default with 5 s** (wear-sensor dips, `logs/eyetracking.txt`), `unity_no_overlay_copy` (The Room VR), ovrpshim frame wait for Unity 2019 (`UnityOculusCheck.legacy_loop`), `frame.ovr_trace` (opt-in, `native/ovrtrace`, logcat `fp_ovrtrace`). → docs/DEVICE_NOTES.md#headset-round-2026-10-05-evening
**Unity built-in VR input:** probe builds `FRAMEPORT_INPUT_PROBE=1`; ovrpshim adds a `GetMouseButtonDown` icall click (Accounting+); libunity 2019 only polls Touch when the device model starts with "Oculus" → `unity_oculus_check` revision 4 `oculus_model_checks` (BattleSisters); `haptic_fix` suggested for every game with libOVRPlugin.so. → docs/DEVICE_NOTES.md#unity-built-in-vr-input-accounting-battlesisters
**Vader Immortal (GitHub #49):** stuck on its loading card and leaked GPU mappings (solved below: `gl_error_clear`); diagnostics FrameBridge `layer_debug`, `strip_color_bias`, `frame_balance`, `snapshot=N` (`snapshot_gl.c`: saves the submitted left-eye image as `files/fb_snap_*.ppm`). → docs/DEVICE_NOTES.md#vader-immortal-headless-investigation
**SteamVR overlay apps:** host OpenVR overlays composite over Lepton games; detection `analysis/vroverlay.py`, patch `pcvr.vr_overlay`, Linux fields `vr_overlay`/`vr_overlay_autostart`, agent `register_vr_overlay`/`unregister_vr_overlay`/`launch_vr_overlay`; SteamVR on the Frame reads only `binary_path_linux_arm`; FramePort's own autostart (`ensure_overlay_service`, `frameport-vr-overlays.service`). See also docs/FRAME_RUNTIME.md "Overlay apps". → docs/DEVICE_NOTES.md#steamvr-overlay-apps
**Vader's leak solved / AC Nexus flips:** stale GL errors broke SteamVR's GL interop → adapter `gl_error_clear` (GLES, catalog for Vader ep1-3); read `xrclient_<pkg>.txt` in Steam's logs for runtime errors; the runtime caps swapchains per session (~15) → flip emulation in place; adapter `flip_dump`. → docs/DEVICE_NOTES.md#vaders-leak-solved--ac-nexus-flips
**Mixed reality: scanned rooms (branch mr-scene):** agent `room_capture` records Valve's RoomView 3D depth mesh -> `fp_roomscan` -> `room.txt` + `room-mesh.bin` (docs/mr/ROOM_FORMAT.md; refined by default: the owner's cleanup passes + label rules in `native/roomscan/refine.c`/`rules.c`, unrefined output kept as `room-raw.txt` + `room-mesh-raw.bin`, head path/settings/report/labels/door suggestions in `scan.json`, `--refine-only <dir>` re-runs it, `--refine 0` = old output; docs/mr/scan.md "Refinement") in `~/.local/share/frameport/rooms/<universe>/`, copied next to settings.conf (`_room_sync`); FrameBridge (`scene_room.c/scene_meta.c/scene_anchors.c`, all through xrshim) serves walls, volumes, planes, the room mesh, META discovery/persistence, saved anchors and `XR_META_boundary_visibility`; `scene_emul` + `frame.meta_permissions` for every USE_SCENE/USE_ANCHOR_API game (migrations `scene_emul_all`, `scene_permissions`); per-game `scene_hide_bounds` (Demeter). → docs/DEVICE_NOTES.md#mixed-reality-scanned-rooms
**Unreal's self-restart:** UE 4.21+ restarts itself after the first shader precompile and Lepton ends the container → launch.sh RESTART_BLOCK starts Lepton once more (`upgrade_launchers`; `FRAMEPORT_NO_RESTART=1` off). → docs/DEVICE_NOTES.md#unreals-self-restart
**xrWaitFrame runs ahead:** the runtime sometimes returns a display time <1 refresh after the last submit and rejects the second submission (`VRCompositorError_AlreadySubmitted`) → the VrApi bridge re-waits (`pacing 2s:` counters). → docs/DEVICE_NOTES.md#xrwaitframe-runs-ahead-journey-of-the-gods
**Lepton's logcat mirror dies** (EOF, read failure, or silent) → launch.sh `_logcat_keeper` reads `podman exec <container> logcat` itself into launch.log (log `<base>/logcat-keeper.log`). → docs/DEVICE_NOTES.md#leptons-logcat-mirror-dies
**Graceful stop (#480, agent v120):** killing the container never paused the app (saves in `OnApplicationPause` lost: Just Dance VR) → launch.sh `pause_game` / agent `stop` / End game first `am start` the HOME intent in the container (`input keyevent` does nothing headless) and wait ≤3 s for STOPPED (`PAUSE_SCRIPT`, `<base>/stop-pause.log`). Steam's own Exit game signals the whole process tree (conmon is re-parented to the reaper) at once → agent v121: launch.sh's `_exit_hook` worker (`exit_hook_worker`, `EXIT_HOOK_JS`) wraps `SteamClient.Apps.TerminateApp` in SharedJSContext with a CDP binding of its own: Exit game for that game's ids waits for the worker's pause (cap 3 s, 0.8 s without an answer, lease 10 s), other ids pass straight through; End game calls `terminate_app_js` (past the hook). → docs/DEVICE_NOTES.md#graceful-stop-the-game-is-paused-before-it-ends-github-480-agent-v120
**Double launch:** launch.sh takes `flock` on `<base>/.launch.lock` (fd 9); a second launch exits 0 and logs to `<base>/launch-dup.log` (`upgrade_launchers` adds it). → docs/DEVICE_NOTES.md#double-launch
**Issue triage round 2026-10-06:** FrameBridge turns haptic amplitude 0 into xrStopHapticFeedback, per-game `haptic_scale`; `haptic_cap` (default on, #547, `haptic_cap.h`) cuts renewed-every-frame vibrations to a few frames so a stalled game stops buzzing (one-shots untouched); Lambda1VR → `frame.tbxr_vendor`; Jurassic World → `frame.vrapi_stub`; Metro Awakening / Pinball FX → `hide_space_warp`; `frame.ovr_trace` can't trace Unity games; the VrApi bridge's 30 s head-pose deadline ends headless VR mode. → docs/DEVICE_NOTES.md#issue-triage-round-2026-10-06
**Steam language, offline games, extra folder, power loss (agent v126, unchecked on device):** launchers pass Steam's language (registry.vdf → Android tag, `LOCALE_LINE`, #530; `device.language` "" = Steam's, `en-US` explicit); `device.offline` = wrapper `--network=none` (#505); `device.extra_folder` = /sdcard/<name> link + `STEAM_COMPAT_LIBRARY_PATHS` (#501); `boot_state.prev_reason` from `qcom-pon` (SMPL "SPML" = power loss → no game blamed, triage `frame-power-loss`, #598). → docs/DEVICE_NOTES.md#offline-games-and-an-extra-shared-folder-discussions-505-501-agent-v126
**Lepton storage:** each app's /sdcard has `Movies`/`Download`/`Documents` symlinked to the Frame's `~/Videos`/`~/Downloads`/`~/Documents` (agent `storage_targets`); the media index never works; Lepton grants all runtime permissions. → docs/DEVICE_NOTES.md#lepton-storage
**SteamVR per-app settings:** `native/vrsettings` = `fp_vrsettings.exe` sets `steam.app.<appid>` `preferredRefreshRate` / `motionSmoothingOverride` live; `pcvr.steamvr_tuning` (default on, PC only). → docs/DEVICE_NOTES.md#steamvr-per-app-settings
**Language packs:** `frame.langpacks` (opt-in, `native/langpack`, `elf.hide_exports`, logcat tag `fp_langpack`) serves `<tag>.lang` files; Unreal needs the pack's Metadata = the game's version (`with_metadata`, `FRAMEPORT_LANGPACK_META`) and the `/storage/emulated/0/...` path spelling; `FRAMEPORT_LANGPACK_SKIP`. → docs/DEVICE_NOTES.md#language-packs
**Cube swapchains:** the runtime refuses `faceCount=6` → FrameBridge `cube_standin` (default on, GLES only, `native/adapter/cube_standin.c`). → docs/DEVICE_NOTES.md#cube-swapchains
**Hardware video decoding:** `native/hevc` OMX plugin (`OMX.frameport.{avc,hevc,vp9}.decoder`), installed once per Frame (`ensure_video_codec`, agent `video_codec_status`/`install_video_codec`/`video_codec_switch`); **bump `revision` in native/hevc/build.py with every artifact change**; per game `frame.hw_video_decode` (analysis `media_codec`), Settings `video.hw_decode`, `FRAMEPORT_NO_HW_VIDEO=1`; Batman migration `batman_video_patches` + `surface_native`. → docs/DEVICE_NOTES.md#hardware-video-decoding
**Vulkan shader dump:** adapter `vk_shader_dump=1` (`native/vkshim/shader_dump.h`) writes SPIR-V modules + `index.txt` to `files/fp_vk_shaders/`; `collect_diag` returns the newest session's modules (`shader_dumps`); triage `gpu-hang`, `triage.graphics_api`. → docs/DEVICE_NOTES.md#vulkan-shader-dump
**Foveation centre (#394):** Valve's FDM layer centres its density map on the whole swapchain (not the eye's rect or optical axis) and Turnip picks density per GMEM bin (the sharp area snaps to bins, so its size shifts with render resolution: driver design); `VK_LAYER_FRAMEPORT_fdm_centre` (native/foveation/centre.h, below Valve's layer, views from FrameBridge `fdm_views.c`) rewrites the per-eye density-map offsets, centring the full-density *bins* (Turnip's bin size recomputed in `tu_bins.h`, Adreno 750 / Mesa 26.3; per-game override `fdm_bin_w`/`fdm_bin_h`; `FRAMEPORT_FDM_DIAG=1` logs it); FrameBridge `snapshot=N` reads Vulkan eyes back. → docs/DEVICE_NOTES.md#foveation-sharp-area-centre-and-size-394
**Expansion file names (#450/#481):** OBBs with a download's copy suffix (`main.203.com.Armature.VR4-003.obb`) or another case are installed under the game's name (`quest_dump.obb_names`/`frame_manifest`, PC files untouched), other kinds too (#415: Crisis Brigade 2's `office.222.<pkg>.obb`, kind spelling kept, never an "another app" warning); another app's name (#129), duplicates and unclear cases stay warnings. → docs/DEVICE_NOTES.md#expansion-file-names-450-481
**Unreal OBB check (#159):** `frame.unreal_skip_obb_check` (analysis `unreal_verify_obb`) turns off Epic's OBB CRC pass; triage `unreal-obb-check-stuck`. → docs/DEVICE_NOTES.md#unreal-obb-check
**Issue round 2026-10-12:** Lepton breaks on unquoted paths with spaces → launchers give Lepton a `~/.local/share/frameport/lepton-paths/<pkg>` link (`app_dir_lines`/`set_app_dir`/`launcher_base`); `frame.eos_keychain_loop`, `frame.unity_tls_key_pool` (`libfp_tkeys.so`, `elf.rename_imports`), `frame.vermillion_scene_canvas`; `tools/cpp2il.py` falls back to Cpp2IL's dev build for metadata v107; Linux `desktop_icon`; `scripts/upload_bench.py`. → docs/DEVICE_NOTES.md#issue-round-2026-10-12
**OpenSSH uploads (#324/#242):** `native_upload.quote` escapes only backslash and `"`, Win32 local paths go as `C:/…`; `Frame.auth` records the login that worked (app_key / user_key from `ssh.extra_keys`, `frame ssh-keys`, agent, password); strict IdentitiesOnly first, relaxed retry. → docs/DEVICE_NOTES.md#openssh-uploads
**Meta user id (#182):** OVRPort's platform loader makes up a new user id every start → `frame.ovr_stable_user` (default on) returns a stable per-game, per-user id. → docs/DEVICE_NOTES.md#meta-user-id
**Vivox API 31 (#101):** newer Vivox calls Android 12 AudioManager methods → `frame.vivox_audio_route` (analysis `vivox_api31`, `Dex.return_early`). → docs/DEVICE_NOTES.md#vivox-api-31
**Steam Input gamepad for 2D apps (#162):** opt-in `device.steam_gamepad` (deployment `steam_gamepad` → launcher `GAMEPAD_LINE` → `PODMAN_WRAPPER` / `podman_run_args` bind-mount Steam's virtual pads). → docs/DEVICE_NOTES.md#steam-input-gamepad-for-2d-apps
**Multiview programs on flat framebuffers (#77):** Mesa drops num_views=2 draws on single-view FBOs → `frame.gl_multiview_fbo` (`native/glmv` = `libfpglmv.so`, analysis `gl_multiview_libs`); headset-verified with Doom3Quest. → docs/DEVICE_NOTES.md#multiview-programs-on-flat-framebuffers-doom3quest
**BlazeRush (#57):** `frame.blazerush` (1.0.349 only), GL shim `gl_int_attribs` / `gl_highp_markers`, saved VrApi FOV (`files/framebridge-vrapi-fov.txt`), `device.config_sync` (catalog `config_sync`); launch tests treat an end after the headless deadline as a pass (triage `headless_end`). → docs/DEVICE_NOTES.md#blazerush
**Session triage:** real play sessions are triaged too: agent `last_play` / `session_log` (`<anchor>/plays.log`), `pipeline.sessions_due` / `triage_session` = `validate/session.analyze` (incl. `space-warp-used`, `gpu-hang`, `slow-frames`, `focus-dips`) → library `last_session` → game page "Last session"; `apply_suggestions_live`; CLI `frameport session <pkg> [--apply]`; pac_hints stays triage-only. → docs/DEVICE_NOTES.md#session-triage
**Unresolved (as of 2026-09-28):** Arcsmith (right-eye distortion) and Time Stall (both eyes) — swap, tracking, Valve
layers, depth, pacing ruled out. Sniper Elite VR (DEVICE LOST), Espire 1 (Mesa GL upload crash), HITMAN 3 (freedreno
crash): use PC versions.

**Install links / FrameDrop button protocol:** `deeplink.py` parses framedrop://, frameport:// and https install links (strict URL rules, manifest ≤256 KiB, downloads into `<data>/downloads/`), `pipeline.add_from_link`, `views/link_dialog.py`, CLI `frameport open-link`; `urlhandler.py` registers a script (bundles can't take URL arguments), `<data>/links/*.link` + `gui.alive` heartbeat; Settings → Install links (`links.framedrop`/`links.frameport`); `ui/dropped.py`, `pipeline.add_windows_exe`, `device.display_mode`; demo link `deeplink.DEMO_MANIFEST`. → docs/DEVICE_NOTES.md#install-links--framedrop-button-protocol
**Install link updates:** manifest `version` → library `link.version`; `pipeline.check_link_updates` (GUI start, 6 h, setting `links.check_updates` = Settings → Updates → Games) marks `link_update` → Library bar / game page → `link_dialog.update_from_link`; CLI `frameport link-updates`; the demo link is never checked. → docs/DEVICE_NOTES.md#install-links--framedrop-button-protocol

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

## GitHub triage (issues / discussions / PRs)
GitHub identity + credentials are maintainer-local (`CLAUDE.local.md`): check `gh api user --jq .login` before any
GitHub write. **The owner approves before posting, labelling or closing anything.** Reply style: "I"/neutral, never
we/our/us; never mention agent version numbers or internal tool versions; show the owner every draft in full with a
short recap of the thread so far; read **all** comments on a thread first (a later one often has the zip or the answer).
- **Every triage also sweeps for unanswered comments**, not just new threads: open AND recently closed issues, PRs
  (incl. merged/closed ones) and discussions (top-level comments and their replies) whose latest comment isn't the
  maintainer's, plus earlier user comments a later maintainer reply skipped. Users often ask follow-ups after a fix or
  after closing (#294: two questions after the reporter closed it; #249: a Q&A discussion with no reply for days;
  #161/#110: requests posted after the close). Quick check: `gh issue list --state all --search "updated:>=<date>"
  --json number,comments --jq '.[] | select(.comments[-1].author.login != "spoopyghosty0")'`; discussions via GraphQL
  (`discussions(orderBy:UPDATED_AT)` with `comments{replies}`).
- **Issue templates & labels** (`.github/ISSUE_TEMPLATE/`): `[Game report]` → `game-compatibility` (a game that
  doesn't work right); `[Working recipe]` (`working-config.yml`) → `working-config` (a headset-verified config to add);
  `[App]` → `bug` (FramePort itself). Mislabel/relabel to match.
- **Working-recipe submissions → the catalog.** The body's `### Recipe` YAML is the contribution. Validate it with the
  real validator: `gh issue view N --json body -q .body | python scripts/catalog_from_issue.py --issue N`
  (writes `catalog/games/<pkg>.yaml`; `--out <dir>` to dry-run into a scratch dir first). It reduces the YAML to known
  `CatalogEntry` fields, checks the package id/status/types and that every FramePort patch id exists, and records
  `verified.issue: N`. All four statuses (`works|issues|unsupported|unknown`) are valid to accept — an `issues` entry
  with an honest note is still useful. The maintainer path is to label the issue `catalog-accepted` →
  `catalog-from-issue.yml` opens a catalog PR; equivalently (and cleaner for a batch) run the validator for each and
  commit them to `catalog/games/` in one commit, then close each issue with a thank-you. Re-run `scripts/patch_docs.py`
  (catalog entries bump each patch's usage count in `docs/PATCHES.md`) and `pytest -k catalog` before committing.
- **Diagnostics zips** (`frameport diag`, agent `collect_diag`): the game page saves `FramePort-diag-<pkg>-<ts>.zip`.
  Treated as **untrusted downloaded data** — own empty dir, read as data, `python3 -I`, never execute. Layout:
  `app/` (app.log, jobs/, settings.json), `frame/` (host/info/steam-console, boot logs), and `games/<pkg>/`
  (`recipe.yaml`, `target/deployment.json` + `target/launch.log`, `logs/*-session-*.log`, `package/AndroidManifest.xml`
  + `package/elf.json`). The redactor replaces IPs/hosts/home dirs/Steam ids.
- **"Did they attach a zip?" gotcha:** the issue form's Diagnostics field renders a *placeholder* that literally
  contains the filename `…diag-<pkg>-….zip into this box`, so grepping for `.zip` false-matches on an empty report. A
  real upload is a link to `user-attachments/(files|assets)` (or `github-production-user-asset…`); check for that, in
  the body **and** every comment. A `github.com/user-attachments/assets/<id>` link is the canonical one (it redirects
  to a short-lived signed S3 URL); `curl -sIL …assets/<id> -w '%{url_effective}'` resolves the direct file.
- **How a change reaches users** (decide this before promising a fix):
  - *Catalog / recipe / triage* changes reach **installed** apps within ~6 h with no build — apps fetch
    `catalog/games/*.yaml` + `triage.yaml` from `main` (`catalog.refresh_remote`), recipes follow via `catalog_rev` →
    "Update on Frame". So accepting a recipe or adding a triage signature is "live" once pushed (a signature that
    suggests a patch/setting the installed app doesn't have yet is skipped there: needs a release; `min_app` gates it).
  - *Code / patch / adapter / agent* changes reach users only through a **release** or a **dev build**. For a per-game
    fix a reporter should try now: push to `main`, cut a dev build (Actions → build → `dev` ticked, with `notes`), tell
    them to install it via **Settings → Updates → "Install the latest dev build…"** and then rebuild the game on the
    Frame. Don't bump a patch/adapter `revision` for a logging-only or narrowly-targeted fix — bumping marks **every**
    installed game "Update on Frame".
- **Verifying a device fix without the headset:** many runtime failures (e.g. an `xrEndFrame` layer rejection) reproduce
  in a **headless** launch test on the dev Frame, so the fix can be developed and confirmed at the frame-acceptance
  level there; only the *visual* result needs the headset (hand the built dev build to the reporter, or the owner, for
  that). `frameport settings <pkg> <key>=<value>` toggles adapter settings (e.g. `layer_debug=1`) live, no rebuild.
  To reproduce a reporter's exact build, grab their APK from its upstream source (e.g. the linked GitHub release), scan
  → build → install → test in an isolated `FRAMEPORT_HOME` (see "Dev commands" / the Frame-busy rules).

## Heuristics (games not in the catalog)
Each patch's `detect()` suggests itself from the Analysis; `applies()` says whether it can matter at all (the UI/CLI
hide non-applicable patches; enabled ones are always shown). Rules learned from the 34 games:
MR-only (PASSTHROUGH required + BOUNDARYLESS_APP) → force_passthrough, + USE_SCENE → scene_emul + meta_permissions;
hand tracking required → controller_fix=0; OVRPlugin + ≥20 GiB → disable_space_warp; Unreal ≤4.21 or Unreal Meta XR
Audio → nodebug; Oculus-OS class referenced by the Unreal audio build or ≥2 Meta libs → oculusos; legacy-VrApi Unity
GLES with MSAA → unity_no_msaa; CryEngine → user.cfg r_variable_rate_shading=0; direct VrApi → bridge (+GL shim for
GLAD/GLES); Unreal → alternate no-ForceQuit build. Scoring (docs/HEURISTICS_EVAL.md, 2026-10-15): analyse the dumps
once (`scripts/heuristics_snapshot.py <out.json> "<VR CyberDeck Portable>" "<PATCHED>"`, ~30 min, keep out of git),
then `scripts/eval_heuristics.py --snapshot <out.json> --split dev|holdout|all [--fit-report]` (catalog-only truth,
per-patch precision/recall, a fixed 20% holdout to catch rules fitted to one game). Read the **dated** score: an
"extra" on an entry verified before the patch existed is a catalog gap, and adapter/device picks are inherited by
catalog recipes anyway. 2026-10-15: strict 5/65 exact, F1 73.8%; dated 39/65, F1 88.6%; remaining misses are
one-game runtime fixes. Don't key a rule on one game's code fingerprint (an Unreal splash/depth "detect" matching
only Star Wars Tales + FNAF HW was rejected: it would also have bumped ANALYSIS_VERSION for every user). Add a rule →
re-run dev + holdout + `pytest -m games`.

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
- New or changed patch, game setting, catalog recipe or triage signature → run `python scripts/patch_docs.py`
  (regenerates docs/PATCHES.md + site/src/data/patches.json, the site's id → anchor map; a test runs `--check`).
- Known-good backups of every working APK and the signing keys: `PATCHED/_known-good-2026-09-28/` (don't delete).
- Your development Frame: `frameport frame info` (remembered in `<user data>/frames.json`); SSH as `steamos@<frame>`.


Steam library scheduling (#305, agent v99): `install.steam_library` = now/later/off; agent `library_mode` persists it.
Automatic restarts also hold on a fresh local `frameport-app/gui.alive` (10 s; existing 12 h cap).
Later journals changes and installs steam.service ExecStartPre/ExecStartPost hooks to reapply after Steam's exit
writes and verify before clearing; preserve this ordering and avoid locking across systemctl's startup hooks.
Off Play (also pending Later Play) uses detached systemd-run launch.sh, with no shortcut/devkit registration;
Steam's per-game controller/overlay/Exit game integration is lost. Restart Steam now explicitly bypasses holds,
asks before ending a running game, and closes local FramePort/Desktop Mode. Purge removes the startup hook.
Hook hardening: both lines `-` + `/usr/bin/timeout -k 5 20`; ExecStartPost only `systemd-run --no-block`s the
verify (unit frameport-library-verify) so Steam's start never waits; drop-in removed once mode != later and nothing
pending (`drop_library_hooks_if_idle`). Steam = static user unit steam.service (Type=simple, Restart=always, 90 s).
Agent v124: a waiting `now` worker holds the shortcut lock (`shortcut_lock`) for up to 12 h, so nothing an SSH command
reaches blocks on it (an install's `library_mode` hung until the 600 s agent timeout): `library_mode` tries once and
skips the hook cleanup; `cmd_shortcuts` journals into the waiting worker (`pending_lock`, a short separate lock;
the worker writes "running" under it before merging the journal after stop_steam, one restart for all; the PC
shows the same "waiting" reason); `queue_library` waits ≤30 s, else journal only; runs drop what they applied
(`consume_pending`), never later entries.
