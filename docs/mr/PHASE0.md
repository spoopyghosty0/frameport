# Phase 0: reading Valve's RoomView 3D mesh (dev Frame, SteamOS 0.4.5 build 20261007, 2026-10-16)

Result: **a third-party OpenVR client can read the passthrough depth mesh, and it carries enough of the room for
a scan** (floor, table-height surfaces, orthogonal walls; the ceiling only weakly).

## Access
- Background OpenVR app (`VR_InitInternal2(..., VRApplication_Background)`, never starts SteamVR) through
  `/opt/steamvr/bin/linuxarm64/libopenvr_api.so`.
- `FnTable:IVRBlockQueue_005`: `Connect("/xrservice/roomview3d/meshData")` and `.../meshLayout` work (created by
  Valve's cv driver; no exclusivity seen next to the compositor's own reader). Read with
  `WaitAndAcquireReadOnlyBlock(queue, &block, &buffer, EBlockQueueReadType_New, timeout_ms)` + `ReleaseReadOnlyBlock`.
- Metadata through the C++ interface `IVRPaths_002` (vtable slot 0 `ReadPathBatch(root, PathRead_t*, n)`, slot 2
  `StringToHandle(&handle, path)`; `FnTable:IVRPaths_001` doesn't exist), root = the **block** handle, except
  `/meshData/meshIsUpToDate` which is a property of the **queue** handle:

| path | tag | meaning |
|---|---|---|
| `/meshLayout/layoutId` | 3 (u64) | id of the index layout (static while the mesh resolution stays) |
| `/meshLayout/vertexCount` | 2 (i32) | 16896 = 132 x 128 |
| `/meshLayout/indexCount` | 2 | 99822 (u32 triangle indices into the grid, row width 132) |
| `/meshData/meshId` | 3 | frame counter (~30 per second while passthrough runs) |
| `/meshData/layoutId` | 3 | matches the layout block |
| `/meshData/elementCount` | 2 | floats in the data block (vertexCount x 3) |
| `/meshData/rawFromMeshPose` | 20 (HmdMatrix34) | mesh -> SteamVR raw tracking space |
| `/meshData/posePredictionBaseTime` | 7 (f64) | seconds (system uptime clock) |
| `/meshData/meshIsUpToDate` (queue) | 4 (bool) | false in standby / headset off |

- STAGE (= standing universe) = `IVRSystem_023` vtable slot 13 `GetRawZeroPoseToStandingAbsoluteTrackingPose()` x
  rawFromMeshPose. Check: the mesh origin is 6.7 cm from `GetDeviceToAbsoluteTrackingPose(Standing)`'s head
  position in every frame (camera vs head centre). **The raw->standing transform changes during a session** (its
  height went 1.60 -> 1.07 m while recording: Valve's floor estimate) -> capture raw poses and convert all frames
  with the transform at the end of the capture.

## The mesh
- A fixed fan of view directions, +Z forward, rows top->bottom (row 0 = up in the image, -Y), ~126 deg across the
  corners (corner directions ~(+-0.67, +-0.66, 0.34)); one depth per grid point. Not head-locked in world terms:
  its pose is the passthrough camera pose at capture.
- Range: reliable ~0.3-4 m; far values are inpainted (up to ~10 m) -> drop > 4 m. Strongly smoothed (soft edges,
  small objects lost); locally planar: plane-fit residual of 12x12 patches median 5-17 mm.
- First frames after tracking starts can have NaN poses -> skip non-finite frames.
- Fused normals (60 s of looking around, ~3 frames/s used): floor clearly at y ~= 0, a table-height surface at
  0.7 m, a lower one at 0.5 m; vertical surfaces in two orientation clusters ~85 deg apart (orthogonal walls), wall
  offsets readable; ceiling: few down-facing points (people rarely look straight up; prompt for it).
- Data rate: 16896 x 12 B vertices + 99822 x 4 B indices per frame (the dump repeats the indices per frame).

## Consequences for the design
- Capture = stdlib Python on the Frame (ctypes), as above; the C scanner works offline on the dump.
- Scanner input quality: smooth, noisy at depth discontinuities; walls/floor via normals + RANSAC are robust;
  furniture only as coarse boxes; ceiling needs a guided "look up" step, else the default height.
