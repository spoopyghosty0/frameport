#!/usr/bin/env python3
"""Record the showcase videos (docs/showcase/videos/<name>.yaml): the real GUI on the demo library with a pretend
Frame, driven like a person would (a drawn pointer on eased paths), filmed with Chrome's screencast; then each scene
captioned, instruction cards rendered for what happens outside FramePort (a download, a command on the Frame), title
and end cards added and everything joined with crossfades.

    python scripts/showcase/record_video.py                    # every video
    python scripts/showcase/record_video.py tour install       # some
    python scripts/showcase/record_video.py install --draft    # quick 720p check of a storyboard edit (work dir only)
    python scripts/showcase/record_video.py /tmp/my-demo.yaml       # a storyboard file anywhere (its output may
                                                                         # be any .mp4 path: demos, not docs)
    python scripts/showcase/record_video.py --changed-since <git rev>   # the ones whose storyboard (or this code)
                                                                         # changed since then (CI)

Each video writes its `output` MP4 (+ .jpg poster) and, when it has `teaser` parts, its teaser WebP; a video.json
timeline goes next to the work files (--work). --names prints what can be clicked after each scene, --scenes records
only some scenes (with --draft). SHOWCASE_DEBUG=1 prints every step with its start time. See docs/SHOWCASE.md.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE.parent), str(REPO / "src")]

from showcase import demo_home  # noqa: E402

VIDEOS = REPO / "docs" / "showcase" / "videos"
VIDEO_KEYS = {"output", "teaser", "budget_mb", "poster_at", "start", "setup", "vars", "title", "end", "scenes"}
START_KEYS = {"profile", "frame", "stream", "route"}
SCENE_KEYS = {"name", "caption", "teaser", "hold", "steps", "card", "seconds"}
CARD_KEYS = {"eyebrow", "heading", "line", "steps", "code", "note"}
VIEWPORT = (1440, 810)  # the layout size; filmed at 4/3 device pixels = 1920x1080
TEASER_MB = 4.0


def video_names() -> list[str]:
    return sorted(p.stem for p in VIDEOS.glob("*.yaml"))


def load_video(name: str) -> dict:
    """A storyboard, checked (StepError for anything malformed: before a recording starts, and in the tests)."""
    import yaml

    from showcase import steps

    path = Path(name) if name.endswith(".yaml") else VIDEOS / f"{name}.yaml"  # (or a storyboard file anywhere)
    where = path.name if name.endswith(".yaml") else f"videos/{name}.yaml"
    data = yaml.safe_load(path.read_text())
    unknown = set(data) - VIDEO_KEYS
    if unknown:
        raise steps.StepError(f"{where}: unknown key(s) {sorted(unknown)}")
    if not str(data.get("output") or "").endswith(".mp4"):
        raise steps.StepError(f"{where}: output must be an .mp4 path")
    if data.get("teaser") is not None and not str(data["teaser"]).endswith(".webp"):
        raise steps.StepError(f"{where}: teaser must be a .webp path")
    start = data.get("start") or {}
    if set(start) - START_KEYS:
        raise steps.StepError(f"{where}: start: unknown key(s) {sorted(set(start) - START_KEYS)}")
    if start.get("profile", "demo") not in demo_home.PROFILES:
        raise steps.StepError(f"{where}: start.profile is one of {', '.join(demo_home.PROFILES)}")
    if start.get("frame", "connected") not in ("connected", "disconnected"):
        raise steps.StepError(f"{where}: start.frame is connected or disconnected")
    if start.get("stream", "live") not in ("live", "frozen"):
        raise steps.StepError(f"{where}: start.stream is live or frozen")
    steps.validate(data.get("setup") or [], f"{where} setup")
    seen = set()
    for sc in data.get("scenes") or []:
        name_ = sc.get("name")
        if not name_ or name_ in seen:
            raise steps.StepError(f"{where}: missing or repeated scene name {name_!r}")
        seen.add(name_)
        if set(sc) - SCENE_KEYS:
            raise steps.StepError(f"{where}: {name_}: unknown key(s) {sorted(set(sc) - SCENE_KEYS)}")
        if "card" in sc:  # an instruction card: rendered, not filmed
            card = sc["card"]
            if not isinstance(card, dict) or not card.get("heading") or set(card) - CARD_KEYS:
                raise steps.StepError(f"{where}: {name_}: card needs a heading (keys: {', '.join(sorted(CARD_KEYS))})")
            if "steps" in sc or "caption" in sc or "teaser" in sc:
                raise steps.StepError(f"{where}: {name_}: a card scene has no steps, caption or teaser")
            if not isinstance(sc.get("seconds", 5), (int, float)) or sc.get("seconds", 5) <= 0:
                raise steps.StepError(f"{where}: {name_}: seconds must be positive")
            continue
        cap = sc.get("caption")
        if cap is not None and not (isinstance(cap, list) and 1 <= len(cap) <= 2):
            raise steps.StepError(f"{where}: {name_}: caption is [title] or [title, line]")
        teaser = sc.get("teaser")
        if teaser is not None and not (isinstance(teaser, list) and len(teaser) == 2 and teaser[1] > 0):
            raise steps.StepError(f"{where}: {name_}: teaser is [from, seconds]")
        steps.validate(sc.get("steps") or [], f"{where} {name_}")
    if not seen:
        raise steps.StepError(f"{where}: no scenes")
    for card in ("title", "end"):
        if not (data.get(card) or {}).get("heading"):
            raise steps.StepError(f"{where}: {card} needs a heading")
    return data


def changed_videos(since: str) -> list[str]:
    """The videos to record again after the commits since `since`: all when the recorder or the step language
    changed, else the ones whose storyboard changed."""
    out = subprocess.run(["git", "diff", "--name-only", since, "HEAD"], cwd=REPO, capture_output=True, text=True)
    if out.returncode != 0:
        return video_names()  # (unknown base, e.g. a new branch): everything
    files = out.stdout.split()
    if any(f.startswith("scripts/showcase/") for f in files):
        return video_names()
    return sorted({Path(f).stem for f in files if f.startswith("docs/showcase/videos/") and f.endswith(".yaml")}
                  & set(video_names()))


def video_installs(session, pace: float = 0.5) -> None:
    """Installs in a video run the scripted job (every real stage, nothing built or sent) through the real queue,
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


def record(name: str, args) -> int:
    """Record one video in this process."""
    video = load_video(name)
    start = video.get("start") or {}
    scenes = video["scenes"]
    if args.scenes:
        want = args.scenes.split(",")
        scenes = [s for s in scenes if s["name"] in want]
    stem = Path(name).stem
    work = (args.work / stem) if args.work else Path(tempfile.mkdtemp(prefix=f"fp-video-{stem}-"))
    work.mkdir(parents=True, exist_ok=True)
    home = (args.home / stem) if args.home else Path(tempfile.mkdtemp(prefix="fp-video-home-"))
    demo_home.build_for_render(home, offline=args.offline, allow_missing_art=args.allow_missing_art,
                               profile=start.get("profile", "demo"))

    from showcase import postprod, steps
    from showcase.fakes import FrozenMonitorSession, LiveMonitorSession
    from showcase.web import Screencast, Server

    variables = video.get("vars") or {}
    started = time.monotonic()
    step_log = (lambda msg: print(f"{time.monotonic() - started:7.1f} {msg}", flush=True)) \
        if os.environ.get("SHOWCASE_DEBUG") else None

    def script(server) -> int:
        scale = 1.0 if args.draft else 4 / 3
        s = server.session(VIEWPORT, cursor=True, device_scale=scale)
        video_installs(s)
        if start.get("frame") == "disconnected":
            steps.HOOKS["disconnect"](s)
        steps.run(s, video.get("setup") or [], variables, log=step_log)  # before filming: a failure stops here
        s.app.go(start.get("route", "library"))
        s.sleep(2.5)
        s.park()
        cast = Screencast(s, work / "frames", quality=80 if args.draft else 92)
        cast.start()
        s.sleep(0.8)
        timeline = []
        for sc in scenes:
            if "card" in sc:  # rendered later: nothing to film
                timeline.append({"name": sc["name"], "card": True, "seconds": sc.get("seconds", 5)})
                continue
            t0 = time.time()
            print(f"SCENE {sc['name']}", flush=True)
            try:
                steps.run(s, sc.get("steps") or [], variables, log=step_log)
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
        filmed = [t for t in timeline if not t.get("card")]
        raw = work / "raw.mp4"
        first = postprod.frames_to_video(work / "frames", raw, end=filmed[-1]["end"], size=size) if filmed else 0
        colors = postprod.theme_colors("portal")
        logo = (REPO / "src" / "frameport" / "ui" / "icons" / "logo.svg").read_text()
        cards = work / "cards"
        cards.mkdir(exist_ok=True)

        def end_card(which: str) -> tuple[Path, float]:
            c = video[which]
            png = postprod.render_card(server.playwright, postprod.card_html(
                c["heading"], c.get("line", ""), c.get("footer", ""), logo, colors), cards / f"{which}.png")
            clip = work / f"{which}.mp4"
            postprod.card_clip(png, c.get("seconds", 3.0), clip)
            return clip, c.get("seconds", 3.0)

        clip, secs = end_card("title")
        clips, durations, scene_clips = [clip], [secs], {}
        by_name = {sc["name"]: sc for sc in scenes}
        for item in timeline:
            sc = by_name[item["name"]]
            if item.get("card"):
                png = postprod.render_card(server.playwright, postprod.step_card_html(sc["card"], colors),
                                           cards / f"card-{sc['name']}.png")
                clip = work / f"card-{sc['name']}.mp4"
                postprod.card_clip(png, item["seconds"], clip, zoom=0.015)
                clips.append(clip)
                durations.append(item["seconds"])
                continue
            start_s, length = item["start"] - first, item["end"] - item["start"]
            cap = None
            if sc.get("caption"):
                title, line = (sc["caption"] + [""])[:2]
                cap = postprod.render_card(server.playwright, postprod.caption_html(title, line, colors),
                                           cards / f"cap-{sc['name']}.png", transparent=True)
            clip = work / f"scene-{sc['name']}.mp4"
            postprod.cut_scene(raw, max(0.0, start_s), length, cap, clip, size=size)
            clips.append(clip)
            durations.append(length)
            scene_clips[sc["name"]] = (clip, length)
            item.update(clip_start=round(start_s, 3), seconds=round(length, 3))
        clip, secs = end_card("end")
        clips.append(clip)
        durations.append(secs)
        transition = 0.25 if args.draft else 0.5
        if args.draft:
            out = work / f"{stem}-draft.mp4"
            postprod.join(clips, durations, out, transition=transition, crf=28, size=size)
            crf = 28
        else:
            out = REPO / video["output"]
            out.parent.mkdir(parents=True, exist_ok=True)
            crf = postprod.encode_within(clips, durations, out, float(video.get("budget_mb", 12)))
            postprod.poster(out, durations[0] + float(video.get("poster_at", 1.5)), out.with_suffix(".jpg"))
            parts, lengths = [], []
            for sc in scenes:  # the teaser: each scene's `teaser` part (with its caption)
                if sc.get("teaser") and sc["name"] in scene_clips:
                    frm, secs = sc["teaser"]
                    part = work / f"teaser-{sc['name']}.mp4"
                    postprod.run(["-ss", f"{frm:.2f}", "-t", f"{secs:.2f}", "-i", scene_clips[sc["name"]][0],
                                  "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", part])
                    parts.append(part)
                    lengths.append(secs)
            if parts and video.get("teaser"):
                teaser = REPO / video["teaser"]
                teaser.parent.mkdir(parents=True, exist_ok=True)
                q = postprod.teaser_webp(parts, lengths, teaser, budget_mb=TEASER_MB)
                print(f"TEASER {teaser} ({teaser.stat().st_size / 1e6:.2f} MB, quality {q})", flush=True)
        # where each scene starts in the finished video (chapters)
        at = durations[0] - transition
        for item, d in zip(timeline, durations[1:-1], strict=True):
            item["at"] = round(at, 2)
            at += d - transition
        total = postprod.joined_length(durations, transition)
        (work / "video.json").write_text(json.dumps({"video": name, "scenes": timeline, "seconds": round(total, 2),
                                                     "crf": crf, "output": str(out)}, indent=1))
        print(f"VIDEO {name}: {out} ({out.stat().st_size / 1e6:.2f} MB, {total:.1f} s, crf {crf}); work: {work}",
              flush=True)
        return 0

    stream = FrozenMonitorSession if start.get("stream") == "frozen" else LiveMonitorSession
    return Server(monitor_session=stream).run(script)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("videos", nargs="*", help=f"video names (default: all of {', '.join(video_names())})")
    ap.add_argument("--changed-since", default=None, help="only videos whose storyboard (or the recorder) changed "
                    "since this git revision")
    ap.add_argument("--scenes", default=None, help="comma-separated scene names (one video)")
    ap.add_argument("--draft", action="store_true", help="720p, quick, no teaser; nothing written to docs/")
    ap.add_argument("--names", action="store_true", help="print the clickable names after each scene")
    ap.add_argument("--work", type=Path, default=None, help="folder for frames and clips (<work>/<video>)")
    ap.add_argument("--home", type=Path, default=None, help="demo data dirs (<home>/<video>; default: temp)")
    ap.add_argument("--offline", action="store_true", help="use only cached art")
    ap.add_argument("--allow-missing-art", action="store_true", help="render even when a game got no art")
    args = ap.parse_args()
    names = args.videos or (changed_videos(args.changed_since) if args.changed_since else video_names())
    unknown = {n for n in names if not (n.endswith(".yaml") and Path(n).is_file())} - set(video_names())
    if unknown:
        print(f"unknown video(s): {sorted(unknown)} (known: {', '.join(video_names())})", file=sys.stderr)
        return 2
    for name in names:
        load_video(name)  # every storyboard checked before anything is recorded
    if not names:
        print("no video to record", flush=True)
        return 0
    if len(names) == 1:
        return record(names[0], args)
    # one process per video: each builds its own demo data dir (the app reads it per process)
    failed = []
    passed = [*(["--draft"] if args.draft else []), *(["--names"] if args.names else []),
              *(["--offline"] if args.offline else []), *(["--allow-missing-art"] if args.allow_missing_art else []),
              *(["--work", str(args.work)] if args.work else []), *(["--home", str(args.home)] if args.home else [])]
    for name in names:
        cmd = [sys.executable, __file__, name, *passed]
        print(f"RECORD {name}", flush=True)
        if subprocess.run(cmd).returncode != 0:
            failed.append(name)
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
