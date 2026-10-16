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


def render(path: Path, *, shape="box", frames=30, width=56, height=40, noise=.004,
           holes=.025, smear=.012, floor_bias=.008, no_ceiling=False, seed=7,
           yaw=0.0, offset=(0.0, 0.0, 0.0), missing_wall=-1, objects=True):
    """Each frame has its own moving pose; no ground-truth room data is put in the dump."""
    rng = random.Random(seed)
    polygon = BOX_OUTLINE if shape == "box" else L_OUTLINE
    ceiling = 2.6
    furniture = [TABLE, COUCH] if objects and shape == "box" else []
    rotation = (0, math.sin(yaw / 2), 0, math.cos(yaw / 2))
    rays = []
    for row in range(height):
        for col in range(width):
            x = (2 * col / (width - 1) - 1) * math.tan(math.radians(55))
            y = (1 - 2 * row / (height - 1)) * math.tan(math.radians(38))
            length = math.sqrt(x * x + y * y + 1)
            rays.append((x / length, y / length, -1 / length))
    with path.open("wb") as out:
        out.write(struct.pack("<4sI", b"FPRS", 1))
        for frame in range(frames):
            heading = 2 * math.pi * frame / max(frames, 1)
            pitch = -.85 if no_ceiling else (-.60, 0, .70)[frame % 3]
            # The L's two arms need viewpoints on both sides of the re-entrant corner.
            centre = (-.65, -.65) if shape == "l" else (0, 0)
            eye = (centre[0] + .35 * math.cos(heading), 1.6 + .04 * math.sin(heading * 2),
                   centre[1] + .35 * math.sin(heading))
            sy, cy = math.sin(heading / 2), math.cos(heading / 2)
            sp, cp = math.sin(pitch / 2), math.cos(pitch / 2)
            q = (cy * sp, sy * cp, -sy * sp, cy * cp)
            depths = [cast(eye, rotate(ray, q), polygon, ceiling, furniture, missing_wall) for ray in rays]
            vertices, valid = [], []
            for index, (ray, depth) in enumerate(zip(rays, depths, strict=False)):
                valid.append(depth is not None and rng.random() >= holes)
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
            out.write(struct.pack("<d7fII", frame / 15, *pose_eye, *pose_q, len(rays), len(indices)))
            out.write(struct.pack(f"<{len(vertices)}f", *vertices))
            out.write(struct.pack(f"<{len(indices)}I", *indices))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", type=Path)
    ap.add_argument("--shape", choices=("box", "l"), default="box")
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("--width", type=int, default=56)
    ap.add_argument("--height", type=int, default=40)
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
    if args.frames < 1 or args.width < 3 or args.height < 3:
        ap.error("positive frame count and a grid of at least 3 x 3 required")
    render(args.out, shape=args.shape, frames=args.frames, width=args.width, height=args.height,
           noise=args.noise, holes=args.holes, smear=args.smear, floor_bias=args.floor_bias,
           no_ceiling=args.no_ceiling, seed=args.seed, yaw=math.radians(args.yaw),
           missing_wall=args.missing_wall, objects=not args.no_objects)


if __name__ == "__main__":
    main()
