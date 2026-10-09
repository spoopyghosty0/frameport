#!/usr/bin/env python3
"""Render the docs screenshots (docs/showcase/shots.yaml) from the demo library with a pretend Frame, and write each
into docs/images/ only when the picture really changed (anti-aliasing noise doesn't count).

    python scripts/showcase/render_docs.py [--only library,game] [--check] [--out docs/images] [--offline]

--check renders into a temp folder and exits 1 if any docs image is out of date (CI). Needs the dev extras
(flet-web, playwright + `playwright install chromium`). See docs/SHOWCASE.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE.parent), str(REPO / "src")]

from showcase import demo_home  # noqa: E402

MANIFEST = REPO / "docs" / "showcase" / "shots.yaml"
IMAGES = REPO / "docs" / "images"
SHOT_KEYS = {"name", "docs", "steps", "viewport", "scale", "crop"}
# a picture counts as changed when the mean difference per channel exceeds MEAN_LIMIT (0-255 scale) or more than
# PIXEL_SHARE of its pixels differ by more than PIXEL_LIMIT in some channel. Measured: run-to-run noise (the live
# card's sparkline) stays under 0.007 % of the pixels; the smallest real change seen (the sidebar card showing
# another game) is 0.12 %
MEAN_LIMIT, PIXEL_LIMIT, PIXEL_SHARE = 0.5, 24, 0.0004
ATTEMPTS = 2  # a shot whose steps fail (e.g. the window never connected) is tried once more in a fresh window


def load_manifest(path: Path = MANIFEST) -> dict:
    import yaml

    from showcase import steps

    data = yaml.safe_load(path.read_text())
    names = set()
    for shot in data.get("shots") or []:
        name = shot.get("name")
        if not name or not re.fullmatch(r"[a-z0-9-]+", name):
            raise steps.StepError(f"shots.yaml: bad shot name {name!r}")
        if name in names:
            raise steps.StepError(f"shots.yaml: {name} is listed twice")
        names.add(name)
        unknown = set(shot) - SHOT_KEYS
        if unknown:
            raise steps.StepError(f"shots.yaml: {name}: unknown key(s) {sorted(unknown)}")
        steps.validate(shot.get("steps") or [], f"shots.yaml {name}")
        crop = shot.get("crop")
        placed = "anchor" in (crop or {}) or {"x", "y"} <= set(crop or {})
        if crop and crop.get("dialog"):
            continue
        if crop is not None and not ({"width", "height"} <= set(crop) and placed):
            raise steps.StepError(f"shots.yaml: {name}: crop needs width, height and an anchor or x/y")
    return data


def changed(old: Path, new: Path) -> bool:
    """Whether `new` differs visibly from `old` (a missing or differently sized old picture counts as changed)."""
    from PIL import Image, ImageChops, ImageStat

    if not old.exists():
        return True
    with Image.open(old) as a, Image.open(new) as b:
        if a.size != b.size:
            return True
        diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    if max(ImageStat.Stat(diff).mean) > MEAN_LIMIT:
        return True
    strong = diff.convert("L").point(lambda v: 255 if v > PIXEL_LIMIT else 0)
    share = ImageStat.Stat(strong).mean[0] / 255
    return share > PIXEL_SHARE


def optimise(path: Path) -> None:
    from PIL import Image

    with Image.open(path) as im:
        im.load()
        im.convert("RGB").save(path, "PNG", optimize=True)


def privacy_problems(home: Path) -> list[str]:
    """Anything in the demo data dir that looks like someone's own data: this machine's home folder or user name, the
    real data dir, IP addresses (version numbers like 1.4.0.0 aside: only private/public ranges with a non-zero last
    part that aren't a known version are reported)."""
    needles = {str(Path.home()), str(demo_home.real_data_dir())}
    user = os.environ.get("USER") or os.environ.get("USERNAME")
    found = []
    ip = re.compile(r"(?<![\d.])(?:10|172|192|100)\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])")
    for f in home.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in (".json", ".txt", ".yaml", ".yml", ".conf", ".log", ""):
            continue
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        rel = f.relative_to(home)
        for n in needles:
            if n and n in text.replace(str(home), ""):
                found.append(f"{rel}: contains {n}")
        if user and len(user) > 2 and re.search(rf"\b{re.escape(user)}\b", text.replace(str(home), "")):
            found.append(f"{rel}: contains the user name {user!r}")
        for m in ip.findall(text):
            found.append(f"{rel}: IP address {m}")
    return found


def crop_box(session, crop: dict):
    from showcase.web import Box

    if crop.get("dialog"):
        d, pad = session.dialog(), crop.get("pad", 0)
        return Box(d.x - pad, d.y - pad, d.w + 2 * pad, d.h + 2 * pad)
    if "anchor" in crop:
        a = session.find(crop["anchor"], timeout=10)
        return Box(a.x + crop.get("left", 0), a.y + crop.get("top", 0), crop["width"], crop["height"])
    return Box(crop["x"], crop["y"], crop["width"], crop["height"])


def render(shots: list[dict], variables: dict, defaults: dict, out: Path, log=print, verify: bool = True):
    """A script for web.Server.run: renders every shot into out/<name>.png."""
    from showcase import steps

    def one(server, shot: dict, path: Path) -> None:
        """Render one shot. Any failing step raises: no picture is taken of a page that didn't get where the steps
        lead (a window that never connected once produced an empty, unconnected Library)."""
        vw, vh = shot.get("viewport") or defaults.get("viewport") or (1280, 820)
        s = server.session((vw, vh))
        try:
            steps.run(s, shot.get("steps") or [], variables)
            s.park(away=True)
            box = crop_box(s, steps.substitute(shot["crop"], variables)) if shot.get("crop") else None
            s.shot(path, clip=box)
        finally:
            s.close()

    def attempt(server, shot: dict, path: Path) -> bool:
        """One render into `path` (a failing step: one more try in a fresh window). False when both failed."""
        for n in range(1, ATTEMPTS + 1):
            try:
                one(server, shot, path)
                return True
            except Exception as exc:  # noqa: BLE001
                path.unlink(missing_ok=True)
                if n < ATTEMPTS:
                    log(f"RETRY {shot['name']}: {exc!r}")
                else:
                    server.errors.append(f"{shot['name']}: {exc!r}")
        return False

    def script(server) -> int:
        """Every shot is rendered twice, each in a fresh window, and kept only when both agree: the headless GPU
        now and then draws a glyph wrong ("7.θ W" for "7.30 W"), a fault the settling can't see. Disagreeing
        renders get a third: the one two of them match is kept."""
        failed = 0
        for shot in shots:
            t0 = time.monotonic()
            final = out / f"{shot['name']}.png"
            takes = []
            for k in range(3 if verify else 1):
                path = out / f".take{k}-{shot['name']}.png"
                if not attempt(server, shot, path):
                    break
                takes.append(path)
                if not verify:
                    path.replace(final)
                    break
                agreed = next((a for i, a in enumerate(takes) for b in takes[i + 1:] if not changed(a, b)), None)
                if agreed is not None:
                    agreed.replace(final)
                    break
            else:
                server.errors.append(f"{shot['name']}: three renders, no two alike (an unstable page?)")
            for p in takes:
                p.unlink(missing_ok=True)
            if final.exists():
                log(f"SHOT {shot['name']} ({time.monotonic() - t0:.1f} s, {len(takes)} renders)")
            else:
                failed += 1
        return 1 if failed else 0
    return script


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default=None, help="comma-separated shot names")
    ap.add_argument("--out", type=Path, default=IMAGES, help="where the images go (default docs/images)")
    ap.add_argument("--check", action="store_true", help="only report out-of-date images (exit 1 if any)")
    ap.add_argument("--home", type=Path, default=None, help="demo data dir (default: a temp folder)")
    ap.add_argument("--offline", action="store_true", help="use only cached art")
    ap.add_argument("--allow-missing-art", action="store_true", help="render even when a game got no art")
    ap.add_argument("--keep", type=Path, default=None, help="also keep every raw render in this folder")
    ap.add_argument("--fast", action="store_true", help="render each shot once (no second render to catch a "
                    "misdrawn glyph); for trying out a manifest edit")
    args = ap.parse_args()

    manifest = load_manifest()
    shots = manifest["shots"]
    if args.only:
        want = args.only.split(",")
        unknown = set(want) - {s["name"] for s in shots}
        if unknown:
            print(f"unknown shot(s): {sorted(unknown)}", file=sys.stderr)
            return 2
        shots = [s for s in shots if s["name"] in want]
    home = args.home or Path(tempfile.mkdtemp(prefix="fp-showcase-home-"))
    demo_home.build_for_render(home, offline=args.offline, allow_missing_art=args.allow_missing_art)
    problems = privacy_problems(home)
    if problems:
        print("refusing to render, the demo data contains personal-looking data:\n  " + "\n  ".join(problems),
              file=sys.stderr)
        return 3
    renders = Path(tempfile.mkdtemp(prefix="fp-showcase-shots-"))
    status_file = renders / "status.json"

    def finish(server_code: int) -> int:
        """After rendering (still in the server's script thread): compare, write, report."""
        report = {"written": [], "unchanged": [], "missing": [], "changed": []}
        for shot in shots:
            new = renders / f"{shot['name']}.png"
            if not new.exists():
                report["missing"].append(shot["name"])
                continue
            optimise(new)
            old = args.out / new.name
            if changed(old, new):
                report["changed"].append(shot["name"])
                if not args.check:
                    args.out.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(new, old)
                    report["written"].append(shot["name"])
            else:
                report["unchanged"].append(shot["name"])
        if args.keep:
            args.keep.mkdir(parents=True, exist_ok=True)
            for f in renders.glob("*.png"):
                shutil.copyfile(f, args.keep / f.name)
        status_file.write_text(json.dumps(report))
        print("RESULT " + json.dumps(report), flush=True)
        if report["missing"] or server_code:
            return 1
        return 1 if args.check and report["changed"] else 0

    from showcase.web import Server

    script = render(shots, manifest.get("vars") or {}, manifest.get("defaults") or {}, renders, verify=not args.fast)
    from showcase.fakes import FrozenMonitorSession

    return Server(monitor_session=FrozenMonitorSession).run(lambda server: finish(script(server)))


if __name__ == "__main__":
    sys.exit(main())
