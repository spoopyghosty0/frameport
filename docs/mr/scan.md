# Offline room scanner (scan stream)

`fp_roomscan` converts recorded FPRS v1 meshes to the unchanged [room contract](ROOM_FORMAT.md):
`room.txt` and `room-mesh.bin`. It does not capture RoomView queues, contact a headset, install rooms, or modify
the current-room symlink. This stream changes only `native/roomscan/`, its build target and artifact/checksum,
`scripts/mr/synth_scan.py`, `tests/test_roomscan.py`, and this note. No runtime, agent, patch, or analysis versions
were bumped.

## Build and run

Use the cached NDK; no downloads are needed for this target:

```sh
python native/build.py --only roomscan
```

The result is `artifacts/linux-arm64-bin/fp_roomscan`, a static aarch64 Linux ELF with its own `_start`, raw Linux
syscalls, and no interpreter, dynamic section, libc, libm, UAPI headers, generated tables, or new dependencies.
`platform.h` is the reconstruction code's entire I/O/allocation boundary. The same five C sources compile on the
host:

```sh
cc -std=c11 -O2 -Wall -Wextra -Werror -ffp-contract=off -DFP_HOSTED native/roomscan/*.c -o /tmp/fp_roomscan
python scripts/mr/synth_scan.py /tmp/room.fprs
/tmp/fp_roomscan --offline /tmp/room.fprs --out /tmp/scanned-room --universe 0 --json
```

The output directory's parent must exist; the scanner creates the final directory if needed. It always prints
exactly one JSON summary, whether `--json` is supplied or omitted. Success exits 0, errors exit 2 with
`{"ok":false,"error":"..."}`. `--help` prints usage and exits 0. Successful summaries contain:

```json
{"ok":true,"frames":30,"walls":4,"detected_walls":4,"volumes":2,"floor_y":-0.0023,
 "ceiling_y":2.5992,"coverage_percent":100.0,"hint_used":false,"triangles":70997,
 "accepted_triangles":118804,"rejected_triangles":704,"warnings":[],
 "timings_ms":{"read":0,"fusion":0,"model":0,"mesh":0,"total":0}}
```

Timing values above are placeholders. Heights are absolute STAGE coordinates. `walls` counts output polygon
edges including synthesized edges; `detected_walls` counts fitted physical planes. Coverage means the percentage
of the output perimeter supported by observed wall planes, **not camera coverage of all room surfaces**. Timings
include parsing, fusion, structural/furniture modelling, mesh generation, and total time including output.

All frame headers, finite timestamps/vertices/poses, approximately unit quaternions, counts, indices and EOF
boundaries are checked before publication. Frame counts are capped at 1 million vertices / 3 million indices;
oversized records fail rather than allocating from untrusted counts. A header-only dump or one without usable
triangles fails. Empty frames may accompany useful frames. Corrupt input preserves any existing output byte for
byte and creates no output directory. UUIDs come from `getrandom`, fresh per scan; mesh geometry is repeatable.

Both complete files are written to exclusive, randomly named temporary files, then individually renamed, with
the mesh first and `room.txt` last. A write failure removes the temporary files. These are atomic **file**
replacements, not a two-file filesystem transaction: the installer should scan into a fresh staging directory
and publish that directory/symlink after success. Do not run concurrent writers into the same output directory.
There is no fsync-based power-loss durability promise. Neither scan nor build writes application data outside
the worktree's `FRAMEPORT_HOME`; fixtures and test output use temporary directories.

## Reconstruction

1. The first streaming pass validates every frame, rotates its mesh by the supplied quaternion, and translates
   it into STAGE. It rejects degenerate triangles, excessively long edges, out-of-range points, and grazing
   surfaces. Winding is corrected toward the observing camera. A deterministic bounded reservoir of 32,768
   accepted triangle centroids determines median position and padded bounds. No frames are retained across reads.
2. The second pass rasterizes accepted triangles at 0.75 voxel spacing. Per-cell surfels average positions and
   inward normals, weighted by area and squared incidence. A capped accumulated weight lets later views correct
   earlier ones. The grid is sized from the sample bounds and capped at 8 x 8 x 4 metres about the data median.
   Unobserved cells have zero weight. This implementation uses a bounded dense backing array for the sparse
   observations, rather than a hash table; the 4.2-million-cell hard cap bounds all later work.
3. Horizontal normal/height histograms select the dominant floor within the STAGE floor band and a ceiling above
   the minimum ceiling height. Missing floor uses STAGE y=0 with a warning; missing ceiling uses floor plus
   `--ceiling-default`, warning exactly `ceiling not seen`.
4. RANSAC fits near-vertical planes using distance and normal agreement, then refines them by position covariance.
   Minimum height, span, and area reject furniture faces. Wall directions within the snap tolerance of the
   dominant wall's orthogonal directions are snapped, with offsets refitted. Directed wall segments are connected
   by nearest endpoints and adjacent line intersections. This preserves the L-shaped fixture's re-entrant corner.
   Large gaps close along observed endpoints. Too few or ambiguously ordered walls fall back to an observed-bounds
   rectangle, with warnings. Polygon checks reject crossing edges. The output follows the contract's positive
   shoelace ordering in (x,z), with left-facing inward normals.
5. Remaining above-floor surface cells form connected components in the voxel grid. Each box uses the dominant
   fourth harmonic of horizontal face directions for yaw (position PCA fallback), trimming the outer 2% of its
   horizontal bounds to suppress stereo halos. Heights extend down to the floor when supported near it;
   top-only objects conservatively extend to the furniture floor margin. Labels follow height, area, long-side,
   thinness and near-wall rules, in order BED, COUCH, TABLE, SCREEN, STORAGE, OTHER. Only volumes are emitted;
   optional table plane records are not required. Floor/ceiling/wall surfaces are excluded before clustering.
6. Surfel splats form an incidence/area-weighted signed distance field within three voxels of each surface. The
   sign is positive in observed free room space. Marching cubes traces zero crossings on cube faces, resolving
   ambiguous faces from their centre sign. It fans closed contours and orients triangles toward the positive
   gradient. Unknown corners suppress extraction rather than bridging unobserved space. If necessary, extraction
   is repeated on a coarser lattice (strides 2–4) until at most 200,000 triangles remain, with a decimation warning;
   excess beyond that fails. Vertices are unshared triangle vertices, so at most 600,000 vertices. Structural
   planes are semantic anchors, not substituted for the observed global mesh.

Peak allocation stays below 220 MiB, including maximum frame buffers, the grid, model worklists and mesh buffers
(the latter phases do not retain frame buffers). Default synthetic scenes use a much smaller grid. Thresholds
are CLI options below or named constants next to their algorithms; there is no device-specific calibration yet.

## Tuning

Distances are metres, areas m², volumes m³. `--universe` accepts the full unsigned 64-bit range; 0 means unknown.

| Option | Default | Accepted range / meaning |
| --- | ---: | --- |
| `--voxel` | 0.05 | 0.04–0.20, spatial resolution; lower values cost more memory/work |
| `--ceiling-default` | 2.5 | 1.8–3.8, height above floor when ceiling is unseen |
| `--truncation` | 3 | 1–5, TSDF band in voxel units |
| `--max-edge` | 0.6 | 0.05–1.5, reject longer triangle edges |
| `--grazing` | 0.12 | 0.01–0.9, minimum absolute camera/normal cosine |
| `--range` | 5.5 | 1–10, maximum camera-to-triangle distance |
| `--plane-distance` | 0.045 | 0.01–0.15, point-to-plane inlier tolerance |
| `--min-plane-area` | 0.45 | 0.1–4, minimum occupied plane area estimate |
| `--wall-height` | 1.5 | 0.5–3, minimum observed vertical wall span |
| `--snap-degrees` | 5 | 0–15, orthogonal snap tolerance |
| `--floor-band` | 0.35 | 0.05–1, floor search distance from STAGE y=0 |
| `--ceiling-min` | 1.8 | 1.5–3, minimum absolute ceiling y |
| `--min-volume` | 0.05 | 0.005–1, reject smaller furniture boxes |
| `--cluster-gap` | 0.10 | 0.04–0.20, voxel neighbourhood gap (rounded up to cells) |
| `--gap` | 0.65 | 0.1–1.5, maximum adjacent-wall endpoint extrapolation |
| `--horizontal` | 0.85 | 0.6–0.99, minimum floor/ceiling/top normal cosine |

Additional constants in `model.c` include RANSAC trials, normal agreement, wall span, furniture margins,
label height/area thresholds, box trimming and circle recognition. Fusion sampling/weight limits are in
`fusion.c`; mesh splat radii/decimation limits in `mesh.c`; hard resource limits in `scan.h`.

`--hint outline.txt` accepts one `x z` point per line, decimal/scientific notation, optional `#` comments, either
winding, at most 96 points / 8 KiB. At least eight points with radial coefficient of variation below 5% count as
the default circle and are ignored. A non-circular hint is used only when walls are incomplete **and every
observed wall has a compatible hint edge** within `--gap`. Complete scans are never constrained to chaperone.
Incompatible inset play boundaries are ignored with a warning. Hint-completed edges retain synthesized status.

## Contract gaps and limits for integration

- **`INVISIBLE_WALL_FACE` has no encoding in the current wall record.** `ROOM_FORMAT.md` lists the semantic label
  but specifies only `wall uuid x0 z0 x1 z1 y0 y1`. It must not change in this stream. The scanner writes a
  `# INVISIBLE_WALL_FACE: synthesized edge follows` comment before each synthesized wall, plus JSON warnings and
  reduced coverage. Existing readers can parse every record, but cannot distinguish that wall's semantic label.
  The maintainer must agree a future encoding/reader change with the emulation stream to expose the label.
- Real RoomView recordings are not available to this stream. Stereo noise, voxel size, normal thresholds and
  furniture rules need calibration against actual dumps. The ray-cast fixtures are useful regressions, not proof
  of headset accuracy. Furniture labels are geometric heuristics; adjacent/touching objects can merge, occluded
  objects may split, and top-only clusters have inferred depth. Large disconnected coplanar wall segments are
  currently fitted together; complicated rooms can fall back to a warned rectangle.
- Cropped or sparse captures can produce an incomplete mesh and synthesized outline. Surfaces are not filled
  through unseen regions. Furniture is not an accessibility/safety boundary. Rooms beyond the grid cap are
  cropped with a warning. Default-circle recognition may also ignore an intentionally circular boundary.
- Output paths are fixed by the contract. The plumbing stream should stage a new directory and switch `current`
  after scanner success, preserving the previous room for rollback. Assign the recording's actual universe id;
  this standalone scanner cannot query SteamVR or validate universe alignment.

## Offline validation

`tests/test_roomscan.py` builds with the host compiler (skips only if none exists). Its 31 tests cover the noisy
4 x 3 x 2.6 m room with table/couch, six-wall L, moving and rotated/translated mesh poses, ceiling fallback, a single
frame, missing walls, circular/matching hints, correct UUIDs/wire layout, interior mesh winding, triangle budget,
repeatable mesh bytes with fresh UUIDs, truncated/corrupt dumps, invalid counts/indices/poses and invalid CLI values.
The test module takes about seven seconds, including synthetic rendering and compilation (under the 60 s budget).
The renderer is deterministic stdlib Python: a 110° x 76° cone, moving head around 1.6 m, pitched views, triangulated
depth grid, Gaussian noise, holes, discontinuity smear, and floor-biased depth.

```sh
UV_OFFLINE=1 UV_CACHE_DIR=/tmp/frameport-uv-cache uv run pytest -q tests/test_roomscan.py
UV_OFFLINE=1 UV_CACHE_DIR=/tmp/frameport-uv-cache uv run ruff check scripts/mr/synth_scan.py tests/test_roomscan.py
```

The synthetic box also passes AddressSanitizer + UndefinedBehaviorSanitizer. A 4 cm voxel run peaks at 34,376 KiB
RSS on the host (0.82 s; device speed remains untested). Two consecutive final native builds produce identical
SHA-256 `da56b149eef89e0cca6d5ca395dff5cff5987328eaad346b2012fa093244ddcf` (43,880 bytes), matching
`artifacts/SHA256SUMS`. ELF inspection confirms aarch64, little-endian, no PT_INTERP / PT_DYNAMIC, and no local build
paths embedded.

The required full `uv run pytest -q` run completed with **1,778 passed, 40 skipped, 11 failed, 16 errors** in 74 s.
All 27 failures/errors are the sandbox's `PermissionError: [Errno 1] Operation not permitted` on socket creation
in existing HTTP/pairing/live-stream tests (`test_deeplink`, `test_devkit`, `test_install_links_extra`,
`test_livestream`, `test_setup_url`, `test_ui_state`). This environment forbids sockets, including loopback.
The unchanged baseline had the identical 27 failures/errors before the scanner tests were added (1,747 passed).
Consequently the whole-suite-pass requirement cannot be satisfied here; rerun it outside that restriction at
integration. No tests were changed/skipped to hide the restriction and no network/device access was attempted.
No patch/settings/catalog/triage files were touched, so the conditional patch documentation regeneration is
unnecessary.

## Exact device checks (maintainer only; none executed by this stream)

1. On the development PC, build, then render the canonical fixtures:

   ```sh
   python native/build.py --only roomscan
   python scripts/mr/synth_scan.py /tmp/box.fprs
   python scripts/mr/synth_scan.py /tmp/l.fprs --shape l
   python scripts/mr/synth_scan.py /tmp/down.fprs --no-ceiling
   python scripts/mr/synth_scan.py /tmp/one.fprs --frames 1
   scp artifacts/linux-arm64-bin/fp_roomscan /tmp/box.fprs /tmp/l.fprs /tmp/down.fprs /tmp/one.fprs steamos@<FRAME>:~/Downloads/
   ```

2. In a terminal **on the Frame**, use an isolated directory; no game or current room is changed:

   ```sh
   mkdir -p "$HOME/.local/share/frameport/roomscan-test"
   cd "$HOME/.local/share/frameport/roomscan-test"
   cp "$HOME/Downloads/fp_roomscan" ./fp_roomscan
   chmod 700 ./fp_roomscan
   ./fp_roomscan --help
   ./fp_roomscan --offline "$HOME/Downloads/box.fprs" --out box --universe 0 --json > box.json
   cat box.json
   ./fp_roomscan --offline "$HOME/Downloads/l.fprs" --out l --json > l.json
   ./fp_roomscan --offline "$HOME/Downloads/down.fprs" --out down --ceiling-default 2.7 --json > down.json
   ./fp_roomscan --offline "$HOME/Downloads/one.fprs" --out one --json > one.json
   ```

   Expected JSON: box has `ok:true`, `walls:4`, `detected_walls:4`, `volumes:2`, floor within 5 cm of 0,
   ceiling within 5 cm of 2.6, coverage 100, no warnings; `box/room.txt` includes TABLE and COUCH volume records.
   L has six walls and the concave corner near (0,0), rather than a rectangle. Down includes exactly the warning
   string `ceiling not seen` (other warnings are expected), with ceiling minus floor 2.7. One includes
   `few frames; scan more viewpoints`. Every mesh should have at most 200,000 triangles. JSON is the scanner log;
   there are no expected stderr status lines.

3. Verify sidecars and the corrupt-input exit status on the Frame:

   ```sh
   python3 - <<'PY'
   import json, pathlib, struct
   for name in ('box', 'l', 'down', 'one'):
       report = json.loads(pathlib.Path(name + '.json').read_text())
       data = pathlib.Path(name, 'room-mesh.bin').read_bytes()
       magic, version, nv, ni = struct.unpack_from('<4sIII', data)
       assert report['ok'] and magic == b'FPRM' and version == 1
       assert ni % 3 == 0 and 0 < ni // 3 <= 200000
       assert len(data) == 16 + nv * 12 + ni * 4
       print(name, nv, ni // 3, report['warnings'])
   pathlib.Path('broken.fprs').write_bytes(b'FPRS\x01\0\0\0\0')
   PY
   ./fp_roomscan --offline broken.fprs --out broken --json
   echo "$?"
   test ! -e broken/room.txt
   test ! -e broken/room-mesh.bin
   ```

   Expected: `{"ok":false,"error":"truncated frame header"}`, exit status 2, no `broken` outputs.

4. Record actual RoomView dumps with the maintainer's recorder (FPRS v1, mesh-to-STAGE poses, stable universe).
   Copy the recording to `~/Downloads/capture.fprs`. Look around slowly for about a minute, including floor,
   ceiling, every wall, and furniture from several directions. With the **recorded universe id** assigned below:

   ```sh
   scan_universe=0  # replace 0 with the recorder's actual decimal universe id
   /usr/bin/time -v ./fp_roomscan --offline "$HOME/Downloads/capture.fprs" --out real --universe "$scan_universe" --json > real.json 2> real-timing.log
   cat real.json real-timing.log
   cat real/room.txt
   ```

   Measure wall offsets/yaws, ceiling and table top physically and compare to the records (target 5 cm / 2°).
   Check resident memory below 300 MB, sensible coverage/warnings, tabletop labels and no false wall/furniture
   blobs from smeared edges. Repeat with `--max-edge 0.35 --grazing 0.2` into `real-stricter` if edges smear;
   try `--voxel 0.08 --plane-distance 0.07` into `real-coarser` if depth is noisy. Keep both JSON logs and raw
   dumps **outside git** for threshold calibration. These are proposed tuning probes, not established device fixes.

5. After merging plumbing/emulation, publish this staged room through that stream's room-install path and try
   Demeter, then an MRUK discovery/mesh game. Check room loading, furniture alignment, and warning/fallback
   behaviour in-headset; the scanner alone cannot verify the Meta API path. Do not replace `rooms/current` by hand
   during these standalone checks.

Undo standalone checks: remove only the isolated `~/.local/share/frameport/roomscan-test` directory and the five
test files copied to `~/Downloads` (plus `capture.fprs` if no longer needed). No Steam settings, games, agent,
room universe folder, or current-room symlink were changed by these steps.
