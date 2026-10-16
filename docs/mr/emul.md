# Scene emulation stream

Implemented against `ROOM_FORMAT.md` without changing the contract, patch revisions, agent version or analysis version.
No Frame access or external downloads were used. Headset behavior still needs maintainer verification.

## Implementation

- `scene_emu.c` owns entities, component APIs, pose transforms, bounded query snapshots and the event queue.
  `scene_room.c` parses room files and meshes; `scene_meta.c` implements META discovery and batch persistence;
  `scene_anchors.c` implements per-game saved anchors. All are included by the adapter, with no new dependencies.
- At session creation, read `dirname(FRAMEBRIDGE_CONFIG)/room.txt`. Invalid magic/version, unusable geometry or
  absent files retain the original guardian box, settings and seven deterministic UUIDs. Unknown records are
  ignored; malformed records are logged and skipped. Missing ceiling, outline and walls follow the contract.
- Floor/ceiling use the polygon area centroid and real local-XY polygons. Walls retain file order, with local +X
  along their segment, +Y up and +Z inward. Furniture poses use yaw then +Z up; volume bounds extend downwards.
  The room container includes every other room-file entity, excluding independently created application anchors.
- Geometry exposes BOUNDED_2D, BOUNDED_3D, semantic labels and TRIANGLE_MESH as appropriate. OpenXR has no separate
  `BOUNDARY_2D` component enum: `xrGetSpaceBoundary2DFB` belongs to BOUNDED_2D. Labels preserve comma-separated strings.
  All variable-sized outputs support count-only and insufficient-capacity calls, including independent mesh arrays.
- Limits: 2,048 entities including application anchors, 1,024 outline points, 255 label bytes, 64 KiB record lines,
  one global mesh, 600,000 vertices/indices (200,000 triangles) and 16 MiB mesh payload. Sidecar filenames cannot
  escape the config directory. Magic, version, counts, exact byte size, finite coordinates and indices are checked.
- FB queries and META discovery snapshot results under the mutation lock, before queuing available/complete events.
  META component/UUID filters intersect; unrecognized filters impose no restriction. FB max-result limits are honored.
  There are 64 retained query snapshots and 63 queued events; calls that cannot fit their events return
  `XR_ERROR_LIMIT_REACHED`. Geometry readers share the mutation lock with persistence.
- Application anchors use kernel randomness (`getrandom`, then `/dev/urandom`), never index-derived UUIDs.
  Saved poses are in STAGE space. FB/META saves write a private temporary file, flush/fsync, then rename to
  `/sdcard/Android/data/<package>/files/framebridge-anchors.txt`. A process suffix such as `:game` is stripped.
  Erase removes application anchors from disk and the discoverable in-memory set. Failed writes report failure
  through completion events and restore memory. Room-file entities remain owned by the scan: save/erase are no-ops
  for those entities. Storage is local; there is no cloud synchronization.
- A new session reloads the room and saved anchors. Fake handles fit both adapter ABIs and include a session
  generation; erased/stale fake spaces are never passed to SteamVR. Session generations wrap after 256 sessions.
- Capture completes immediately and logs exactly `scene: capture requested -> scanned room` for a usable room file,
  or `scene: capture requested -> guardian box` for the fallback. The former covers all file sources, including manual
  and boundary files. The load summary preserves the actual `source` and `universe`.

The cached `openxr-f2448a8797` headers already contain discovery, persistence and mesh declarations and enum values.
No local structure/enum substitutes were necessary. Both META extensions are added to the existing conditional
extension advertisement/instance filtering. Their system capability structures report support when emulation is active.
Passthrough and unrelated adapter settings are unchanged.

## Routing and integration

All of the following use `scene_emulation` through **both** the adapter's normal `xrGetInstanceProcAddr` and
`framebridge_extension_proc`, reached through xrshim's existing name-agnostic lookup:

```
xrQuerySpacesFB                         xrRetrieveSpaceQueryResultsFB
xrGetSpaceUuidFB                        xrEnumerateSpaceSupportedComponentsFB
xrSetSpaceComponentStatusFB             xrGetSpaceComponentStatusFB
xrGetSpaceContainerFB                   xrGetSpaceRoomLayoutFB
xrGetSpaceBoundingBox2DFB               xrGetSpaceBoundingBox3DFB
xrGetSpaceBoundary2DFB                  xrGetSpaceSemanticLabelsFB
xrRequestSceneCaptureFB                xrCreateSpatialAnchorFB
xrSaveSpaceFB                          xrEraseSpaceFB
xrGetSpaceTriangleMeshMETA              xrDiscoverSpacesMETA
xrRetrieveSpaceDiscoveryResultsMETA     xrSaveSpacesMETA
xrEraseSpacesMETA
```

The shim additionally routes `xrPollEvent`, `xrGetSystemProperties`, `xrLocateSpace`, `xrLocateSpaces`,
`xrLocateSpacesKHR` and `xrDestroySpace` to the adapter while scene emulation is active. Other shim names continue to
OVRPort, preserving existing haptics/render-model handling. The real shim is host-tested against a dispatcher that
refuses every scene function; every emulated scene lookup succeeds and an actual discovery/retrieve round trip runs.

**Plumb-stream requirement:** ensure `scene_emul=1` also installs xrshim and rewrites OVRPlugin's loader string to it.
The pre-existing adapter patch only did this for other shim settings. These files are outside this stream's ownership.
Delivery of room files before Lepton starts and detection of universe misalignment also belong to plumb. FrameBridge
serves the supplied coordinates; it does not silently transform a room belonging to another universe.

No contract changes are needed. Whether each MRUK version accepts a room without further permissions, how it chooses
volume versus mesh geometry, and whether its visible placement matches the scan need headset tests. The existing
single-active-session architecture is retained; simultaneous independent sessions are not implemented.

## Offline verification

- 25 host tests passed, compiling the actual emulation and shim. Coverage includes malformed records, unknown
  records, mesh corruption/path/cap checks, derived geometry, 300 walls, poses/normals, all buffer sizing idioms,
  discovery event order/filters/backpressure, UUID query/discovery of saved anchors, new sessions/processes,
  FB/META save/erase and atomic-write failure rollback.
- ASan/UBSan: scanned room, generated walls and box cases passed. LeakSanitizer was disabled because this sandbox
  prohibits ptrace; its default exit failed with that environmental limitation.
- `python native/build.py --only adapter,xrshim` succeeded from cached tools. Two final builds were byte-identical,
  including `SHA256SUMS`. The shim was rebuilt and is byte-identical to its previous artifact (the routing lives in
  the adapter). Artifact content changes are the two adapter binaries and two checksum lines.
- Full pytest was attempted: **1,771 passed, 40 skipped, 11 failed, 16 errors**. All 27 failing/erroring tests create
  local HTTP sockets, which the sandbox prohibits (`PermissionError: [Errno 1] Operation not permitted`). They are
  in `test_deeplink`, `test_devkit`, `test_install_links_extra`, `test_livestream`, `test_setup_url` and `test_ui_state`.
  With exactly those 27 tests deselected, and the additional real-shim test included: **1,772 passed, 40 skipped,
  27 deselected**. An unrestricted maintainer run of the whole suite is still required.
- The worktree had no usable venv. Verification reused the existing local dependencies through an ignored venv
  `.pth`, with `PYTHONPATH="$PWD/src"`, `UV_CACHE_DIR=/tmp/frameport-uv-cache`, `UV_OFFLINE=true` and
  `uv run --no-sync pytest`. No dependencies were downloaded. The build cache was made a local directory with
  symlinks to cached tools/headers so generated shader files stayed inside this writable worktree.

Adapter SHA-256:

```
arm64-v8a:   a90c8d58904857bc198a48fd7a411b7807f68f14fa58fd268c57e2c2ca87e389
armeabi-v7a: 92daecaf5b86e1857ee8a61b76598e5645f8b427e5f373067c1f87bf8ff99d07
```

## Maintainer device tests

These commands are a handoff, not commands run by this agent. Use an isolated populated `FRAMEPORT_HOME`, cached
build tools, a paired Frame and legally obtained game sources. Stop games before changing their files. Back up the
existing recipe, APK, room files and saved anchors first. Integrate plumb before testing normal room delivery.

On the PC, from the integrated checkout (replace `frame.local` if necessary):

```sh
export FRAMEPORT_HOME="$PWD/.home"
export FRAMEPORT_NO_UPDATE_CHECK=1 FRAMEPORT_NO_CATALOG_UPDATE=1
FRAME=steamos@frame.local
PKG=com.Novelab.Demeter
uv run frameport recipe "$PKG" --set scene_emul=1
uv run frameport install "$PKG" --frame "$FRAME" --force
uv run frameport test "$PKG" --frame "$FRAME" --seconds 45
ssh "$FRAME"
```

In that Frame shell, find the deployment's actual base (also supports a game moved to another drive):

```sh
PKG=com.Novelab.Demeter
BASE=$(python3 -c 'import json,os,sys; p=os.path.expanduser("~/Applications/quest-frame/"+sys.argv[1]+"/deployment.json"); print(json.load(open(p)).get("base",os.path.dirname(p)))' "$PKG")
grep -E 'scene:|extension shim:.*(Spaces|TriangleMesh|BoundingBox3D|PollEvent)' "$BASE/launch.log"
```

With a current scan delivered beside `settings.conf`, expect:

```
scene: room from scan file (N walls, M volumes, P planes, K mesh triangles, universe U)
scene: capture requested -> scanned room
```

The capture line appears only if the game requests capture. A source made from a boundary/manual file names that
source in the summary. The shim logs each requested function, for example
`extension shim: xrDiscoverSpacesMETA -> FrameBridge`. There must be no `Unknown proc addr` for a scene function
that the game obtains through the shim. Repeat the rebuild/install/headset check for `com.gritGames.Nope` and
`com.zenstudios.PFXVRQuest` with `scene_emul=1`; these exercise newer MRUK discovery/persistence paths.

Wear the headset and launch from Steam. Enter each game's MR mode and request room setup if offered. Verify the
floor polygon, every wall, ceiling and furniture match the scan; tables sit below their top-face anchors; doors and
windows have the correct orientation. Headless startup alone cannot prove tracking or visual placement.

Fallback/undo test, while stopped: preserve `$BASE/room.txt` and `$BASE/room-mesh.bin` under a separate backup
folder, then disable plumb's room delivery for this run or move the source room out of `rooms/current` as well.
Without that precaution launch.sh will restore the file. Launch again; expect the original box summary and
`scene: capture requested -> guardian box`. Test `scene_width=3 scene_depth=4 scene_height=2.7` with the existing
`frameport settings` command and expect `scene: room 3.00 x 4.00 x 2.70 m`. Restore the saved files, recipe/settings
and normal room delivery afterward. Do not delete a real game's saved anchors to test the fallback.

### Android persistence probe (independent of a game's UI)

This probe uses the **real scene code with the fake runtime from the host harness**. It checks Android storage,
process-name handling and both persistence APIs; the headset tests above check the real SteamVR integration.
The following cross-compilation command was verified offline:

```sh
native/.cache/ndk-27.2.12479018/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android29-clang \
  -std=gnu11 -O2 -Wall -Wextra -Werror -Wno-unused-function \
  -I native/.cache/openxr-f2448a8797 -I native/adapter tests/native/scene_emu_test.c \
  -lm -o /tmp/frameport-scene-test-android
scp /tmp/frameport-scene-test-android "$FRAME":/tmp/
```

Keep a freshly installed game running so its Lepton container exists, then run in the Frame shell:

```sh
APPID=$(python3 -c 'import json,os,sys; print(json.load(open(os.path.expanduser("~/Applications/quest-frame/"+sys.argv[1]+"/deployment.json")))["appid"])' "$PKG")
CONTAINER=lepton-steamlaunch-$APPID
podman cp /tmp/frameport-scene-test-android "$CONTAINER":/data/local/tmp/frameport-scene-test
podman exec "$CONTAINER" test ! -e /sdcard/Android/data/org.frameport.scenetest
# Continue only if that test succeeds: use a fresh test-only package directory.
podman exec "$CONTAINER" mkdir -p /sdcard/Android/data/org.frameport.scenetest/files /data/local/tmp/frameport-scene-probe
for mode in save reload erase-failure reload erase empty; do
  podman exec "$CONTAINER" sh -c "FRAMEBRIDGE_CONFIG=/data/local/tmp/frameport-scene-probe/settings.conf exec -a org.frameport.scenetest /data/local/tmp/frameport-scene-test $mode" || break
done
podman exec "$CONTAINER" wc -l /sdcard/Android/data/org.frameport.scenetest/files/framebridge-anchors.txt
```

Every invocation must exit 0 without `FAIL`; logs show `anchors saved`, `loaded 2 saved anchors`, a deliberate
`anchor write failed` during `erase-failure` (then the following reload still finds two), and `anchors erased`.
The final file has zero lines. `save` also recreates a session internally, while separate invocations validate
new-process reload. Undo, before stopping this container:

```sh
podman exec "$CONTAINER" rm -rf /sdcard/Android/data/org.frameport.scenetest /data/local/tmp/frameport-scene-probe
podman exec "$CONTAINER" rm -f /data/local/tmp/frameport-scene-test
rm -f /tmp/frameport-scene-test-android
```

Remove the PC's temporary probe binary too. Restore backed-up game files/settings and install the previous APK if
rolling the feature back. No probe command changes real application anchors.

## Commit handoff under the sandbox

This worktree's real Git metadata is on a read-only mount. Commits therefore live on `mr/emul` in
`/tmp/frameport-mr-emul.git`, with a portable bundle at `/tmp/frameport-mr-emul.bundle`. The actual worktree contains
all final source/artifact edits but its original branch metadata cannot be advanced here. No local instruction,
app-data or credential files were staged. The base is `4bf48d3`.

After reviewing the worktree and verifying it contains exactly these changes, the maintainer can import the commits
and align the existing worktree index without discarding its files:

```sh
git bundle verify /tmp/frameport-mr-emul.bundle
git fetch /tmp/frameport-mr-emul.bundle mr/emul
git log --oneline 4bf48d3..FETCH_HEAD
git reset --mixed FETCH_HEAD
```

Run the unrestricted whole suite before merging. The bundle preserves the ordered implementation/test/build commits.
