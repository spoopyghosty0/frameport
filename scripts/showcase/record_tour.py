#!/usr/bin/env python3
"""Record the demo tour (docs/showcase/tour.yaml): the real GUI on the demo library with a pretend Frame, driven
like a person would (a drawn pointer on eased paths), filmed with Chrome's screencast, then captioned, given title
and end cards and joined with crossfades.

    python scripts/showcase/record_tour.py [--scenes library,install] [--draft] [--names]

Writes docs/media/frameport-tour.mp4 (+ .jpg poster) and docs/images/tour-teaser.webp, and a tour.json timeline
next to the work files (--work, default a temp folder). --draft: 720p, quick encode, no teaser, into --work only
(for checking a storyboard edit). --names prints what can be clicked after each scene (writing steps).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE.parent), str(REPO / "src")]

from showcase import demo_home  # noqa: E402

TOUR = REPO / "docs" / "showcase" / "tour.yaml"
MEDIA = REPO / "docs" / "media"
TEASER = REPO / "docs" / "images" / "tour-teaser.webp"
SCENE_KEYS = {"name", "caption", "teaser", "hold", "steps"}
VIEWPORT = (1440, 810)  # the layout size; filmed at 4/3 device pixels = 1920x1080
BUDGET_MB, TEASER_MB = 12.0, 4.0


def load_tour(path: Path = TOUR) -> dict:
    import yaml

    from showcase import steps

    data = yaml.safe_load(path.read_text())
    seen = set()
    for sc in data.get("scenes") or []:
        name = sc.get("name")
        if not name or name in seen:
            raise steps.StepError(f"tour.yaml: missing or repeated scene name {name!r}")
        seen.add(name)
        unknown = set(sc) - SCENE_KEYS
        if unknown:
            raise steps.StepError(f"tour.yaml: {name}: unknown key(s) {sorted(unknown)}")
        cap = sc.get("caption")
        if cap is not None and not (isinstance(cap, list) and 1 <= len(cap) <= 2):
            raise steps.StepError(f"tour.yaml: {name}: caption is [title] or [title, line]")
        teaser = sc.get("teaser")
        if teaser is not None and not (isinstance(teaser, list) and len(teaser) == 2 and teaser[1] > 0):
            raise steps.StepError(f"tour.yaml: {name}: teaser is [from, seconds]")
        steps.validate(sc.get("steps") or [], f"tour.yaml {name}")
    for card in ("title", "end"):
        if not (data.get(card) or {}).get("heading"):
            raise steps.StepError(f"tour.yaml: {card} needs a heading")
    return data


def tour_installs(session, pace: float = 0.5) -> None:
    """Installs in the tour run the scripted job (every real stage, nothing built or sent) through the real queue,
    and leave the game installed on the pretend Frame."""
    from frameport.core import library
    from showcase import fakes

    app = session.app

    def submit(pkg: str, to: str = "frame"):
        script = fakes.install_job(pkg, pace)

        def run(job):
            message = script(job)
            g = library.game(pkg)
            if app.target is not None and g:
                app.target.games.append(fakes.FakeTarget._record(g))
                app.frame_info = app.target.describe()
            now = time.time()
            library.upsert_game(pkg, installs={"frame": {"result": {"ok": True}, "time": now}},
                                last_test={"state": "RUNNING", "verdict": "pass", "milestone": "Submitting frames",
                                           "fps": 72.0, "findings": [], "suggestions": [], "time": now,
                                           "target": "frame"})
            return message
        return app.submit(f"Install {app._title(pkg)} on Frame", run, pkg, "install", to="frame")
    app._submit_install = submit


def print_names(session, scene: str) -> None:
    names = sorted({(n["label"] or n["text"] or n["all"].splitlines()[-1] if n["all"] else "")[:60]
                    for n in session.nodes() if n["tappable"]} - {""})
    print(f"NAMES after {scene}: {names}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenes", default=None, help="comma-separated scene names (default: all)")
    ap.add_argument("--draft", action="store_true", help="720p, quick, no teaser; nothing written to docs/")
    ap.add_argument("--names", action="store_true", help="print the clickable names after each scene")
    ap.add_argument("--work", type=Path, default=None, help="folder for frames and clips (default: temp)")
    ap.add_argument("--home", type=Path, default=None, help="demo data dir (default: temp)")
    ap.add_argument("--offline", action="store_true", help="use only cached art")
    ap.add_argument("--out", type=Path, default=MEDIA, help="where frameport-tour.mp4 goes")
    ap.add_argument("--teaser", type=Path, default=TEASER, help="where the teaser WebP goes")
    args = ap.parse_args()

    tour = load_tour()
    scenes = tour["scenes"]
    if args.scenes:
        want = args.scenes.split(",")
        scenes = [s for s in scenes if s["name"] in want]
    work = args.work or Path(tempfile.mkdtemp(prefix="fp-tour-"))
    work.mkdir(parents=True, exist_ok=True)
    home = args.home or Path(tempfile.mkdtemp(prefix="fp-tour-home-"))
    demo_home.build(home, offline=args.offline)

    from showcase import postprod, steps
    from showcase.fakes import LiveMonitorSession
    from showcase.web import Screencast, Server

    variables = tour.get("vars") or {}

    def script(server) -> int:
        scale = 1.0 if args.draft else 4 / 3
        s = server.session(VIEWPORT, cursor=True, device_scale=scale)
        tour_installs(s)
        cast = Screencast(s, work / "frames", quality=80 if args.draft else 92)
        s.app.go("library")
        s.sleep(2.5)
        s.park()
        cast.start()
        s.sleep(0.8)
        timeline = []
        for sc in scenes:
            t0 = time.time()
            print(f"SCENE {sc['name']}", flush=True)
            try:
                steps.run(s, sc.get("steps") or [], variables)
            except Exception as exc:  # noqa: BLE001
                server.errors.append(f"scene {sc['name']}: {exc!r}")
            s.sleep(sc.get("hold", 0.6))
            timeline.append({"name": sc["name"], "start": t0, "end": time.time()})
            if args.names:
                print_names(s, sc["name"])
        cast.stop()
        cast.write_index()
        s.close()
        return produce(server, timeline)

    def produce(server, timeline) -> int:
        size = (1280, 720) if args.draft else postprod.SIZE
        raw = work / "raw.mp4"
        first = postprod.frames_to_video(work / "frames", raw, end=timeline[-1]["end"], size=size)
        colors = postprod.theme_colors("portal")
        logo = (REPO / "src" / "frameport" / "ui" / "icons" / "logo.svg").read_text()
        cards = work / "cards"
        cards.mkdir(exist_ok=True)
        clips, durations, scene_clips = [], [], {}
        for which in ("title",):
            c = tour[which]
            png = postprod.render_card(server.playwright, postprod.card_html(
                c["heading"], c.get("line", ""), c.get("footer", ""), logo, colors), cards / f"{which}.png")
            clip = work / f"{which}.mp4"
            postprod.card_clip(png, c.get("seconds", 3.0), clip)
            clips.append(clip)
            durations.append(c.get("seconds", 3.0))
        by_name = {sc["name"]: sc for sc in scenes}
        for item in timeline:
            sc = by_name[item["name"]]
            start, length = item["start"] - first, item["end"] - item["start"]
            cap = None
            if sc.get("caption"):
                title, line = (sc["caption"] + [""])[:2]
                cap = postprod.render_card(server.playwright, postprod.caption_html(title, line, colors),
                                           cards / f"cap-{sc['name']}.png", transparent=True)
            clip = work / f"scene-{sc['name']}.mp4"
            postprod.cut_scene(raw, max(0.0, start), length, cap, clip, size=size)
            clips.append(clip)
            durations.append(length)
            scene_clips[sc["name"]] = (clip, length)
            item.update(clip_start=round(start, 3), seconds=round(length, 3))
        c = tour["end"]
        png = postprod.render_card(server.playwright, postprod.card_html(
            c["heading"], c.get("line", ""), c.get("footer", ""), logo, colors), cards / "end.png")
        postprod.card_clip(png, c.get("seconds", 3.0), work / "end.mp4")
        clips.append(work / "end.mp4")
        durations.append(c.get("seconds", 3.0))
        # the final cut
        if args.draft:
            out = work / "tour-draft.mp4"
            postprod.join(clips, durations, out, transition=0.25, crf=28, size=size)
            crf = 28
        else:
            args.out.mkdir(parents=True, exist_ok=True)
            out = args.out / "frameport-tour.mp4"
            crf = postprod.encode_within(clips, durations, out, BUDGET_MB)
            postprod.poster(out, durations[0] + 1.5, args.out / "frameport-tour.jpg")
            # the teaser: each scene's `teaser` part (with its caption)
            parts, lengths = [], []
            for sc in scenes:
                if sc.get("teaser") and sc["name"] in scene_clips:
                    frm, secs = sc["teaser"]
                    part = work / f"teaser-{sc['name']}.mp4"
                    postprod.run(["-ss", f"{frm:.2f}", "-t", f"{secs:.2f}", "-i", scene_clips[sc["name"]][0],
                                  "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", part])
                    parts.append(part)
                    lengths.append(secs)
            if parts:
                args.teaser.parent.mkdir(parents=True, exist_ok=True)
                q = postprod.teaser_webp(parts, lengths, args.teaser, budget_mb=TEASER_MB)
                print(f"TEASER {args.teaser} ({args.teaser.stat().st_size / 1e6:.2f} MB, quality {q})", flush=True)
        total = postprod.joined_length(durations, 0.25 if args.draft else 0.5)
        (work / "tour.json").write_text(json.dumps({"scenes": timeline, "seconds": round(total, 2), "crf": crf,
                                                    "video": str(out)}, indent=1))
        print(f"TOUR {out} ({out.stat().st_size / 1e6:.2f} MB, {total:.1f} s, crf {crf}); work: {work}", flush=True)
        return 0

    return Server(monitor_session=LiveMonitorSession).run(script)


if __name__ == "__main__":
    sys.exit(main())
