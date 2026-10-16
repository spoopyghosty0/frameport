# FramePort room files (mixed reality)

Quest MR games read a room model (floor, ceiling, walls, furniture, a global mesh) through Meta's scene APIs. The
Steam Frame has no such data for apps (see docs/FRAME_RUNTIME.md "Room data"), so FramePort scans the room itself
(`fp_roomscan`) and FrameBridge (`native/adapter/scene_emu.c`) serves it to games. This file is the contract between
the scanner, the Frame agent, the PC app and FrameBridge.

## Coordinates
- Everything is in **STAGE space** = SteamVR's standing universe = OpenXR `XR_REFERENCE_SPACE_TYPE_STAGE`:
  metres, right-handed, +Y up, floor at y = 0, -Z = the universe's forward.
- A room belongs to one chaperone universe (`universe` line). The Frame sometimes starts a new universe without
  relocalizing; then the stored room may be misaligned (the agent reports `aligned: false`, nothing breaks).

## room.txt (UTF-8 text, one record per line, `#` starts a comment, fields separated by spaces)
```
frameport-room 1                    # magic + format version (required, first line)
universe <decimal u64>              # chaperone universe id the room was scanned in (0 = unknown)
source scan|boundary|box|manual     # how it was made
time <unix seconds>
room <uuid>                         # the room anchor
floor <uuid> <y>                    # floor height (normally 0)
ceiling <uuid> <y>                  # ceiling height
outline <n> x0 z0 x1 z1 ... x(n-1) z(n-1)
                                    # floor polygon, n >= 3, counter-clockwise seen from above (+Y), not closed
wall <uuid> x0 z0 x1 z1 <y0> <y1> [LABEL]
                                    # one wall face from (x0,z0) to (x1,z1), bottom y0, top y1.
                                    # Its normal points INTO the room: walls follow the outline counter-clockwise,
                                    # so inward = left of the direction (x0,z0) -> (x1,z1) seen from above.
                                    # LABEL (optional, default WALL_FACE): INVISIBLE_WALL_FACE for an edge the
                                    # scanner closed without seeing a wall (open side of a room, doorway gap).
volume <uuid> <LABEL> cx cz top_y yaw_deg w d h
                                    # furniture box: top-face centre (cx, top_y, cz), yaw about +Y in degrees
                                    # (0 = local X along world X), width w along local X, depth d along the
                                    # horizontal local axis perpendicular to it, height h downwards from the top
plane <uuid> <LABEL> px py pz qx qy qz qw w h
                                    # extra 2D plane (DOOR_FRAME, WINDOW_FRAME, WALL_ART, ...): pose of its centre,
                                    # Meta convention (bounds in local XY, local +Z = the plane's normal)
mesh <uuid> <file name> <vertex count> <index count>
                                    # global mesh sidecar in the same folder (GLOBAL_MESH entity)
```
- `<uuid>` = 32 lowercase hex digits (no dashes). New ones per scan; FrameBridge uses them as the XrUuidEXT bytes.
- Labels are Meta's semantic label strings: FLOOR, CEILING, WALL_FACE, INVISIBLE_WALL_FACE, TABLE, COUCH, BED,
  STORAGE, SCREEN, LAMP, PLANT, DOOR_FRAME, WINDOW_FRAME, WALL_ART, OTHER, GLOBAL_MESH.
- Readers ignore unknown record types (forward compatibility) and reject a file whose first line isn't
  `frameport-room <version>` with a version they know. Missing ceiling -> 2.5 m above the floor. Missing walls ->
  walls from the outline edges. Missing outline -> the bounding rectangle of the walls.

### How FrameBridge maps records to Meta entities (scene_emu.c)
- ROOM: ROOM_LAYOUT (floor, ceiling, walls in file order) + SPACE_CONTAINER (every other entity) + LOCATABLE/STORABLE.
- floor/ceiling: pose at the outline centroid at height y; local +Z = +Y (floor) / -Y (ceiling);
  BOUNDED_2D = the outline's bounding rect in local XY; BOUNDARY_2D = the outline polygon in local XY.
- wall: pose at the segment midpoint, height (y0+y1)/2; local +X along the wall (x0,z0)->(x1,z1), local +Y up,
  local +Z = inward normal; BOUNDED_2D = length x (y1-y0) centred.
- volume: pose at the top-face centre, rotation = yaw about +Y then local +Z pointing up (+Y); BOUNDED_3D offset
  (-w/2, -d/2, -h), extent (w, d, h) (Meta: volumes hang below their anchor); BOUNDED_2D = the top face (w x d).
- plane: pose as given; BOUNDED_2D = w x h centred.
- mesh: entity at the identity pose labelled GLOBAL_MESH with TRIANGLE_MESH (XR_META_spatial_entity_mesh).

## room-mesh.bin (little-endian)
```
char[4] "FPRM", u32 version = 1, u32 vertex_count, u32 index_count,
float32 xyz[vertex_count], u32 indices[index_count]   (triangles; counter-clockwise seen from the room's inside)
```
Coordinates in STAGE space. Kept below 200k triangles (decimated).

## Where the files live
- Frame: `~/.local/share/frameport/rooms/<universe>/room.txt` + `room-mesh.bin` (+ `scan.log`).
  `~/.local/share/frameport/rooms/current` = symlink to the newest room's folder.
- Per game: launch.sh copies the current universe's room (else `current`) to `<app_dir>/room.txt` (+ mesh) before
  Lepton starts, next to `settings.conf`; FrameBridge reads `dirname($FRAMEBRIDGE_CONFIG)/room.txt`. No room ->
  the old guardian box.
- A game's own saved anchors: `/sdcard/Android/data/<pkg>/files/framebridge-anchors.txt`
  (`anchor <uuid> px py pz qx qy qz qw` per line, STAGE space), written by FrameBridge on save/erase.

## Scan dumps (*.fprs) — recorded RoomView 3D meshes, input of `fp_roomscan --offline`
```
char[4] "FPRS", u32 version = 1, then frames until EOF:
  f64 time_s, float32 pose[7] (px py pz qx qy qz qw: mesh -> STAGE), u32 vertex_count, u32 index_count,
  float32 xyz[vertex_count] (mesh space), u32 indices[index_count]
```
