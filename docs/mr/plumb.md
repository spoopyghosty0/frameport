# Room plumbing stream

Implemented on `mr/plumb`; no Frame access or device verification. The shared `ROOM_FORMAT.md`, agent version,
analysis version and patch revisions were left unchanged. No native binaries belong to this stream.

## What changed

- `frameport.mr.room`: room/entity dataclasses, v1 text and binary-mesh validation, canonical serialization,
  missing ceiling/wall/outline defaults, clockwise outline/wall repair, summaries and top-down SVG previews.
  Mesh counts, indices, finite coordinates, UUIDs and sidecar basenames are checked before import.
- Standalone stdlib agent room commands live in one marked block. Rooms are staged under
  `~/.local/share/frameport/rooms/<universe>/`; a `current` symlink selects the latest saved room. Uploads validate
  before replacing a room. Deleting the current room selects the newest remaining room.
- `room_status` returns `rooms`, current `universe`, selected `room`, `room_text`, `aligned`, `default_circle`
  (null when no usable boundary exists) and `scanner_installed`. Summaries include wall/plane counts, furniture
  counts by label, floor area and ceiling height. Status text lets the UI preview a room without another transfer.
- Universe detection takes the last matching vrserver log line, then the newest chaperone record if no log match
  exists. Dummy universe 424242 means unknown. Boundary conversion chains collision quads' bottom edges and refuses
  the default 20-point, roughly 1 m circle. Boundary ceilings default to 2.5 m; SteamVR's 2.43 m wall extrusion
  does not measure the real ceiling.
- Finalize records `scene_room` from adapter settings. A marked `_room_sync` launcher line follows the existing
  config-sync pattern, before Lepton starts. Launcher upgrades also handle old recipes listing
  `adapter.scene_emul`. Matching universe wins over `current`; absent rooms remove stale sidecars. Live setting
  changes update the launcher and clear copies when disabled. Non-scene launchers keep their existing layout.
- PC room helpers implement status/import/export/delete/boundary/build/show/capture. Scanner upload reuses the
  SHA-256 helper extracted from live view; `fp_venc` retains its soft-failure behavior. Import uploads canonical
  text. Export checks mesh counts; build uploads FPRS dumps, runs the scanner and cleans its staging directory.
- CLI: `frameport frame room status|from-boundary|import|export|delete|build|show`, with `--frame` on each command.
  `show --output <path.svg>` writes a preview (default below `FRAMEPORT_HOME/rooms`). Build accepts `--universe`.
- Frame page Room card: checklist, asset-URL preview, boundary/delete/capture actions, refresh and recoverable errors.
  SSH and preview writes run in a background worker; state is cached per connection. Scene-game pages link to Room
  setup when the connected Frame has no room. `ui_smoke.py --fake-frame` supplies local room-command answers.
- Every USE_SCENE/USE_ANCHOR_API game gets the scene-emulation suggestion. Migration `scene_emul_all` enables it once
  in automatic recipes, preserving user recipes. NOPE and Pinball FX keep their other recipe fields and now request
  scene emulation. Triage explains guardian fallback and missing discovery; patch docs and translation template
  were regenerated.

## Integration questions and limits

1. Headset capture is intentionally unfinished: `room_capture` returns `{"error":"not implemented yet"}`. The
   `capture_roomview(seconds, out_path)` hook documents the pending RoomView queue work. The GUI explains this and
   offers the boundary path. Do not present the capture button as a completed scan workflow yet.
2. This branch has no `fp_roomscan` artifact; merge the scan stream before using `frame room build`. The exact scanner
   invocation is `fp_roomscan --offline <dump> --out <temporary directory> --universe U --json`. Stdout must contain
   one JSON value, stderr is saved as `scan.log`, and output must include a valid `room.txt` with the requested U.
3. Merge the emulation/shim stream before expecting games to use saved rooms, meshes or new Meta entry points.
   Runtime log spelling is owned there; triage accepts both existing `using guardian room`/dimension logs and the
   brief's `guardian box`/`room from guardian` messages. Reconcile new success logs at integration.
4. Confirm the chaperone JSON path, exact universe-switch logs and whether collision quads are already in STAGE on
   the device. This implementation follows the brief's path and coordinate contract; it does not apply a second
   standing-origin transform. A universe match alone cannot detect Valve changing an origin without changing its ID.
5. Agent and desktop perform independent validation because the deployed agent cannot import the desktop package.
   The desktop canonicalizes outlines and fills defaults; the agent preserves scanner output and validates it.
   The format contract needed no changes.
6. Rooms are shared across games; per-game saved anchors are not deleted or rewritten by these commands. Replacing
   a room creates new entity UUIDs, so a game may need its own room-setup reset. No automatic game reset was added.

## Offline validation

All commands used `FRAMEPORT_HOME` in this worktree (tests use temporary isolated homes), an existing dependency
venv and offline/no-sync uv settings. No dependencies or native toolchains were downloaded.

- Room, PC/CLI, UI, heuristics, migration, MR triage and translation checks passed: 63 tests. The related
  agent/launcher/screenshots/codec/gamepad regression group passed 170 tests. The final deployment/configuration
  regression check passed 48 tests, including all four recipe/settings precedence cases.
- Ruff over changed Python files and `scripts/patch_docs.py --check` passed. The UI detector reported no findings.
  `scripts/i18n_extract.py` regenerated `src/frameport/locales/template.json`.
- `pytest -m games`: 3 skipped (no game dumps). Dev/holdout evaluation used only the checked-in demo analysis
  snapshot, 21 catalog matches: dev dated 9/17 exact, F1 81.1%; holdout dated 1/4 exact, F1 73.3%. These are limited
  fixture results, not a new score for the owner's complete collection; the evaluator exits 1 for recipe differences.
- Final full suite: **1790 passed, 40 skipped, 11 failed, 16 errors** in 87.35 s. All 27 failures/errors are
  `PermissionError: [Errno 1] Operation not permitted` when existing tests create sockets. The whole-suite green
  requirement cannot be met in this sandbox; rerun `uv run pytest -q` on a host permitting loopback sockets.
  Browser UI smoke also requires a loopback server, so it
  was not run. New UI tests construct the real Flet controls and exercise deferred preview/error/cache states.

## Exact device checks (maintainer only)

Run these after integrating emul + scan. Substitute the paired Frame address below; keep the current worktree's
`FRAMEPORT_HOME=.home` and use explicit `--frame` so the test does not open another app data directory. The source
connection syncs the agent by digest even without a version bump, unless the Frame has a newer agent version.
The maintainer must perform integration version bumps before testing against a newer installed agent.

```sh
export FRAME='steamos@<frame>'
export PKG='com.gritGames.Nope'  # repeat with com.zenstudios.PFXVRQuest and a known scene game
uv run frameport frame info --frame "$FRAME"
uv run frameport frame room status --frame "$FRAME" > .home/mr-status.json
# Back up ALL rooms on the Frame before destructive checks; use an unused backup name.
ssh "$FRAME" 'test ! -e ~/.local/share/frameport/rooms.before-mr-test && { test ! -d ~/.local/share/frameport/rooms || cp -a ~/.local/share/frameport/rooms ~/.local/share/frameport/rooms.before-mr-test; }'
```

1. Inspect status: no room gives `room: null`, `aligned: false`. With Valve's default circle, `default_circle: true`;
   `uv run frameport frame room from-boundary --frame "$FRAME"` must fail with the default-circle explanation.
   Set a real play area in SteamVR, repeat `from-boundary`: expect source `boundary`, one wall per boundary edge,
   correct floor area and `aligned: true`. Open Frame → Room: visible preview, warning for the unscanned room,
   aligned row ready; the capture button should show the friendly unavailable message without crashing.
2. Exercise import/export and preview with a manual fixture in the current universe:

```sh
uv run frameport frame room status --frame "$FRAME" > .home/mr-status.json
uv run python - <<'PY'
import json
from pathlib import Path
from frameport.mr.room import Mesh, Volume, box_room, dumps, write_mesh
p = Path('.home/mr-check'); p.mkdir(parents=True, exist_ok=True)
r = box_room(4, 3, 2.5)
r.source = 'manual'
r.universe = json.loads(Path('.home/mr-status.json').read_text())['universe']
r.volumes.append(Volume('a'*32, 'TABLE', .5, 0, .8, 20, 1.2, .7, .8))
r.mesh = Mesh(vertices=[(-2, 0, -1.5), (2, 0, -1.5), (2, 0, 1.5)], indices=[0, 1, 2])
(p/'room.txt').write_text(dumps(r))
(p/'room-mesh.bin').write_bytes(write_mesh(r.mesh))
PY
uv run frameport frame room import .home/mr-check/room.txt --frame "$FRAME"
uv run frameport frame room export .home/mr-export --frame "$FRAME"
uv run frameport frame room show --output .home/mr-check/preview.svg --frame "$FRAME"
cmp .home/mr-check/room-mesh.bin .home/mr-export/room-mesh.bin
```

   Expect four walls, one TABLE, area 12 m² and height 2.5 m. The preview must label TABLE and the game must show it
   hanging below its top-face anchor. Check the corresponding universe folder and `rooms/current` on the Frame.
3. Use a recorded dump from the same standing universe (do not invent one). Copy it to `.home/mr-check/scan.fprs`,
   then run the command below. Expect the scanner JSON under `report`, source `scan`, aligned room data, `scan.log`
   on the Frame and an installed executable. Run twice: an unchanged scanner must not upload again.

```sh
uv run frameport frame room build .home/mr-check/scan.fprs --frame "$FRAME"
uv run frameport frame room status --frame "$FRAME"
```

   If the recording is from another universe, pass its original `--universe <id>`; do not relabel its coordinates.
4. Prepare the game's source and its existing backed-up signing key in the isolated test library, then force a
   rebuild/reinstall after integration: `uv run frameport install "$PKG" --frame "$FRAME" --force --apk-only`.
   Repeat for both catalog games. In each deployment, expect `scene_room: true`. Run `frame info` twice to upgrade
   old launchers and check one marker only:

```sh
uv run frameport frame info --frame "$FRAME"
uv run frameport frame info --frame "$FRAME"
ssh "$FRAME" "grep -c '# frameport-room-sync' ~/Applications/quest-frame/$PKG/launch.sh"
uv run frameport settings "$PKG" scene_emul=1 --frame "$FRAME"
```

   Expected marker count: 1. Start the game with Steam's Play button in the headset, enter its MR mode, then inspect
   `<deployment.base>/room-sync.log`: JSON has the selected universe and alignment. The two room files must be
   beside `<deployment.base>/settings.conf`. FrameBridge must load that room; `xrDiscoverSpacesMETA` must resolve
   through the extension shim, with no `Unknown proc addr`/MRUK `NoRoomsFound`. Exact new native success log strings
   should be taken from the emul stream. Verify walls/table placement physically; never infer alignment from FPS.
5. Exit the game. Switch `scene_emul=0` using Game settings (or the settings command above with 0): marker and game
   sidecars disappear. Switch it back to 1: one marker returns. Delete the room and launch again: stale copies are
   removed and existing FrameBridge guardian fallback logs trigger informational `mr-no-room` triage.
6. To check a universe mismatch, back up/export the room, change its `universe` record to another valid ID and import
   it, then delete the original universe's room with `frame room delete --universe <original-id>`. Status must select
   `current` but report `aligned: false`; the Room card warns. Restore the original file before playing physically.
   The dummy universe 424242 should report current universe 0 rather than claim alignment.

## Undo device checks

Close all games. Restore the backed-up store (or remove only the fixture universes if there was no previous store):

```sh
ssh "$FRAME" 'if test -d ~/.local/share/frameport/rooms.before-mr-test; then rm -rf ~/.local/share/frameport/rooms; mv ~/.local/share/frameport/rooms.before-mr-test ~/.local/share/frameport/rooms; fi'
```

Set each tested game's `scene_emul` back to its recorded original value. To remove a test room without a backup:
`uv run frameport frame room delete --universe <fixture-universe> --frame "$FRAME"`. Start a scene game once to
refresh/remove its copies. The scanner can be removed with
`ssh "$FRAME" 'rm -f ~/.local/share/frameport/bin/fp_roomscan'`; the next build reinstalls it. Game saves and
`framebridge-anchors.txt` are untouched. Restore the previous app/agent/native artifacts through the maintainer's
normal integration rollback if testing the combined native implementation fails.
