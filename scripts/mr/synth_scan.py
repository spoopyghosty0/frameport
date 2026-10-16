#!/usr/bin/env python3
"""Render deterministic, noisy head-centred depth meshes into the FPRS v1 contract (stdlib only)."""
from __future__ import annotations

import argparse
import math
import random
import struct
from pathlib import Path

BOX_OUTLINE = [(-2, -1.5), (2, -1.5), (2, 1.5), (-2, 1.5)]
L_OUTLINE = [(-2, -2), (2, -2), (2, 0), (0, 0), (0, 2), (-2, 2)]
TABLE = ((-.6, 0, -.25), (1.2, .75, .7))
COUCH = ((.6, 0, .95), (1.6, .65, .65))
ROOMVIEW_WIDTH, ROOMVIEW_HEIGHT = 132, 128
ROOMVIEW_NEAR, ROOMVIEW_FAR = .3, 4.0


def fan(width, height, roomview):
    """RoomView camera axes are +Z forward / -Y image up; poses convert them to STAGE."""
    sx, sy = (1.96, 1.94) if roomview else (math.tan(math.radians(55)), math.tan(math.radians(38)))
    for row in range(height):
        for col in range(width):
            x = (2 * col / (width - 1) - 1) * sx
            y = (2 * row / (height - 1) - 1) * sy
            if not roomview:
                y = -y
            length = math.sqrt(x * x + y * y + 1)
            yield x / length, y / length, (1 if roomview else -1) / length


def smooth_depths(values, width, height, radius=3):
    """Separable box smoothing models soft stereo boundaries, without a NumPy/scipy dependency."""
    result = list(values)
    for vertical in (False, True):
        source, result = result, [0.0] * len(values)
        lines, length, step = (width, height, width) if vertical else (height, width, 1)
        for line in range(lines):
            start = line if vertical else line * width
            prefix = [0.0]
            for i in range(length):
                prefix.append(prefix[-1] + source[start + i * step])
            for i in range(length):
                lo, hi = max(0, i - radius), min(length, i + radius + 1)
                result[start + i * step] = (prefix[hi] - prefix[lo]) / (hi - lo)
    return result


def inside(x, z, polygon):
    odd = False
    for (ax, az), (bx, bz) in zip(polygon, polygon[1:] + polygon[:1], strict=False):
        if (az > z) != (bz > z) and x < (bx - ax) * (z - az) / (bz - az) + ax:
            odd = not odd
    return odd


def rotate(v, q):
    x, y, z = v
    qx, qy, qz, qw = q
    tx, ty, tz = 2 * (qy * z - qz * y), 2 * (qz * x - qx * z), 2 * (qx * y - qy * x)
    return (x + qw * tx + qy * tz - qz * ty,
            y + qw * ty + qz * tx - qx * tz,
            z + qw * tz + qx * ty - qy * tx)


def cast(eye, direction, polygon, ceiling, furniture, missing_wall):
    ex, ey, ez = eye
    dx, dy, dz = direction
    nearest = 5.0
    hit = False
    for height in (0, ceiling):
        if abs(dy) > 1e-8:
            t = (height - ey) / dy
            if .3 < t < nearest and inside(ex + t * dx, ez + t * dz, polygon):
                nearest, hit = t, True
    for index, ((ax, az), (bx, bz)) in enumerate(zip(polygon, polygon[1:] + polygon[:1], strict=False)):
        if index == missing_wall:
            continue
        sx, sz = bx - ax, bz - az
        det = dx * sz - dz * sx
        if abs(det) < 1e-8:
            continue
        t = ((ax - ex) * sz - (az - ez) * sx) / det
        u = ((ax - ex) * dz - (az - ez) * dx) / det
        if .3 < t < nearest and 0 <= u <= 1 and 0 <= ey + t * dy <= ceiling:
            nearest, hit = t, True
    for (cx, bottom, cz), (width, height, depth) in furniture:
        near, far = -1e20, 1e20
        for pos, ray, lo, hi in zip(eye, direction,
                                    (cx - width / 2, bottom, cz - depth / 2),
                                    (cx + width / 2, bottom + height, cz + depth / 2), strict=False):
            if abs(ray) < 1e-8:
                if not lo <= pos <= hi:
                    far = -1e20
                    break
            else:
                a, b = (lo - pos) / ray, (hi - pos) / ray
                near, far = max(near, min(a, b)), min(far, max(a, b))
        if .3 < near < nearest and near <= far:
            nearest, hit = near, True
    return nearest if hit else None


def render(path: Path, *, shape="box", frames=30, width=None, height=None, noise=.004,
           holes=.025, smear=.012, floor_bias=.008, no_ceiling=False, seed=7,
           yaw=0.0, offset=(0.0, 0.0, 0.0), missing_wall=-1, objects=True, profile="roomview",
           invalid_start=None, smoothing=3):
    """Each frame has its own moving pose; no ground-truth room data is put in the dump."""
    rng = random.Random(seed)
    roomview = profile == "roomview"
    width = width or (ROOMVIEW_WIDTH if roomview else 56)
    height = height or (ROOMVIEW_HEIGHT if roomview else 40)
    invalid_start = (2 if roomview else 0) if invalid_start is None else invalid_start
    polygon = BOX_OUTLINE if shape == "box" else L_OUTLINE
    ceiling = 2.6
    furniture = [TABLE, COUCH] if objects and shape == "box" else []
    rotation = (0, math.sin(yaw / 2), 0, math.cos(yaw / 2))
    rays = list(fan(width, height, roomview))
    with path.open("wb") as out:
        out.write(struct.pack("<4sI", b"FPRS", 1))
        for frame in range(frames):
            heading = 2 * math.pi * frame / max(frames, 1)
            pitch = -.85 if no_ceiling else (-.60, 0, .70)[frame % 3]
            if roomview and no_ceiling:
                pitch = -1.2
            # The L's two arms need viewpoints on both sides of the re-entrant corner.
            centre = (-.65, -.65) if shape == "l" else (0, 0)
            eye = (centre[0] + .35 * math.cos(heading), 1.6 + .04 * math.sin(heading * 2),
                   centre[1] + .35 * math.sin(heading))
            if roomview:
                eye = eye[0], 1.3 + .4 * math.sin(heading), eye[2]
            sy, cy = math.sin(heading / 2), math.cos(heading / 2)
            sp, cp = math.sin(pitch / 2), math.cos(pitch / 2)
            q = (cy * sp, sy * cp, -sy * sp, cy * cp)
            if roomview:
                q = q[3], q[2], -q[1], -q[0]  # head rotation composed with camera's 180 degree X rotation
            depths = [cast(eye, rotate(ray, q), polygon, ceiling, furniture, missing_wall) for ray in rays]
            if roomview:
                # Smooth both valid depth and inpainted far pixels. Low-frequency noise survives the filter,
                # while isolated high-frequency speckle is small, as in a stereo patch fit.
                coarse_width = (width + 11) // 12
                coarse = [rng.gauss(0, .012) for _ in range(coarse_width * ((height + 11) // 12))]
                depths = [(depth if depth is not None else 8 + rng.random() * 2)
                          + coarse[(i // width // 12) * coarse_width + (i % width // 12)]
                          for i, depth in enumerate(depths)]
                depths = smooth_depths(depths, width, height, smoothing)
            vertices, valid = [], []
            for index, (ray, depth) in enumerate(zip(rays, depths, strict=False)):
                keep = depth is not None and rng.random() >= holes
                valid.append(keep or roomview)
                if roomview and not keep:
                    depth = 8 + rng.random() * 2
                if depth is None:
                    depth = 0
                else:
                    # Stereo edges blend two depths, producing stretched grazing triangles.
                    other = depths[index + 1] if (index + 1) % width and index + 1 < len(depths) else depth
                    if other is not None and abs(other - depth) > .3 and rng.random() < smear:
                        depth = (depth + other) / 2
                    world_ray = rotate(ray, q)
                    if eye[1] + depth * world_ray[1] < .15:
                        depth += floor_bias * rng.random()
                    depth += rng.gauss(0, noise)
                vertices.extend(c * depth for c in ray)
            indices = []
            for row in range(height - 1):
                for col in range(width - 1):
                    a = row * width + col
                    for triangle in ((a, a + width, a + 1), (a + 1, a + width, a + width + 1)):
                        if all(valid[i] for i in triangle):
                            indices.extend(triangle)
            # Compose the global room transform with each independent mesh -> room pose.
            pose_eye = tuple(a + b for a, b in zip(rotate(eye, rotation), offset, strict=False))
            ax, ay, az, aw = rotation
            bx, by, bz, bw = q
            pose_q = (aw * bx + ax * bw + ay * bz - az * by,
                      aw * by - ax * bz + ay * bw + az * bx,
                      aw * bz + ax * by - ay * bx + az * bw,
                      aw * bw - ax * bx - ay * by - az * bz)
            if frame < invalid_start:
                pose_eye = (float("nan"),) * 3
            out.write(struct.pack("<d7fII", frame / (3 if roomview else 15),
                                  *pose_eye, *pose_q, len(rays), len(indices)))
            out.write(struct.pack(f"<{len(vertices)}f", *vertices))
            out.write(struct.pack(f"<{len(indices)}I", *indices))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", type=Path)
    ap.add_argument("--shape", choices=("box", "l"), default="box")
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("--profile", choices=("roomview", "legacy"), default="roomview")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--invalid-start", type=int, help="untracked initial frames (RoomView default: 2)")
    ap.add_argument("--smoothing", type=int, default=3, help="RoomView depth filter radius in pixels")
    ap.add_argument("--noise", type=float, default=.004)
    ap.add_argument("--holes", type=float, default=.025)
    ap.add_argument("--smear", type=float, default=.012)
    ap.add_argument("--floor-bias", type=float, default=.008)
    ap.add_argument("--no-ceiling", action="store_true")
    ap.add_argument("--no-objects", action="store_true")
    ap.add_argument("--missing-wall", type=int, default=-1)
    ap.add_argument("--yaw", type=float, default=0, help="global room yaw in degrees")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    if args.frames < 1 or (args.width is not None and args.width < 3) or (args.height is not None and args.height < 3):
        ap.error("positive frame count and a grid of at least 3 x 3 required")
    render(args.out, shape=args.shape, frames=args.frames, width=args.width, height=args.height,
           noise=args.noise, holes=args.holes, smear=args.smear, floor_bias=args.floor_bias,
           no_ceiling=args.no_ceiling, seed=args.seed, yaw=math.radians(args.yaw),
           missing_wall=args.missing_wall, objects=not args.no_objects, profile=args.profile,
           invalid_start=args.invalid_start, smoothing=args.smoothing)


if __name__ == "__main__":
    main()
