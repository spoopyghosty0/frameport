#!/usr/bin/env python3
"""Record short clips of the real GUI's animations (web mode, a pretend Frame) for a showcase page: the install
transit, the live Frame card, navigation, selection and theme switching. One browser context per clip (Playwright's
video recorder), converted to MP4 + a poster JPG with ffmpeg.

    FRAMEPORT_HOME=<a test data dir> python scripts/demo_record.py --out /tmp/clips [--game <package>] [--only transit]

Reuses ui_smoke's fakes (FakeTarget, FakeMonitorSession, …): nothing reaches a real Frame. Mouse positions are for
the 1440x900 window at UI scale 1.0 (the same as ui_smoke --gestures).
"""
from __future__ import annotations

import argparse
import os
import queue
import random
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import flet as ft  # noqa: E402
import ui_smoke  # noqa: E402
from ui_smoke import FakeMonitorSession, attach_fake_frame, install_fakes  # noqa: E402

from frameport.core import library  # noqa: E402
from frameport.ui.app import FramePortApp  # noqa: E402

PORT = ui_smoke.PORT
W, H = 1440, 900
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
ERRORS: list[str] = []
APPS: queue.Queue = queue.Queue()  # one FramePortApp per browser session (= per clip)

# sidebar tabs (x, y) at 1440x900
TAB = {"library": (100, 110), "frame": (100, 152), "files": (100, 194), "screenshots": (100, 236),
       "live": (100, 278), "keyboard": (100, 320), "monitor": (100, 362), "settings": (100, 404)}
NOWHERE = (130, 520)  # the sidebar's empty part: nothing hovered


class LiveMonitorSession(FakeMonitorSession):
    """The fake stream, livelier for the live-card clip: a sample every 0.6 s and a frame rate with a few-second
    stutter now and then (also in the two minutes sent first), so the card's sparkline (5 s points) has a shape and
    visibly moves."""

    def _sample(self) -> dict:
        s = super()._sample()
        t = self.t
        fps = 72 - abs(random.gauss(0, 0.8))
        if (t // 8) % 3 == 1:  # 8 samples of stutter in every 24
            fps = 56 + 6 * abs(((t % 8) - 4) / 4) + random.uniform(-1.5, 1.5)
        for g in s["games"]:
            g["fps"] = round(fps, 1)
        return s

    def _run(self) -> None:
        self.t = 0
        while not self.closed and self.t < 100000:
            self.on_sample(self._sample())
            if self.t >= self.BACKFILL:
                time.sleep(0.6)
            self.t += 1


def ui(app: FramePortApp, fn, *args) -> None:
    """Run an app action, logging (not raising) errors so one broken step doesn't end the recording."""
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        ERRORS.append(traceback.format_exc())


# ------------------------------------------------------------------ scenarios
# each: (setup(app) before the clip starts, play(app, page) = the clip, poster time in s)

def transit_setup(game):
    def run(app):
        app.open_game(game)
        time.sleep(1)
        app.show_activity(True)
    return run


def transit_play(game):
    """A pretend install through the real JobManager: every stage the real one reports, with an upload ticking."""
    def run(job):
        rep = job.reporter

        def stage(name, secs, logs=()):
            rep.stage(name)
            for line in logs:
                rep.log(line)
            time.sleep(secs)
        stage("Patching the game", 0.9)
        stage("OVRPort (primary)", 1.2, ["overport patch --version=latest"])
        stage("Frame fixes (primary)", 1.0, ["frame.ovrstubs", "frame.vk_sanitize"])
        stage("sign (primary)", 0.7)
        rep.stage("validate (primary)")
        for name in ("APK signature", "64-bit libraries", "OpenXR loader"):
            rep.check(name, True)
            time.sleep(0.25)
        time.sleep(0.2)
        stage("Prepare Frame", 0.7)
        total = 1.42e9
        rep.stage("Upload APK")
        apk_part = 0.42
        steps = 14
        for i in range(1, steps + 1):
            f = apk_part * i / steps
            speed = 34.0 + random.uniform(0, 4)
            left = (1 - f) * total / (speed * 1e6)
            rep.progress(f, "com.BithellGames.Arcsmith.apk", speed=f"{speed:.1f} MB/s · ~{left:.0f} s left")
            time.sleep(0.22)
        rep.stage("Upload data (3 files)")
        for i in range(1, steps + 1):
            f = apk_part + (1 - apk_part) * i / steps
            speed = 34.0 + random.uniform(0, 4)
            left = (1 - f) * total / (speed * 1e6)
            rep.progress(f, f"main.{120 + i // 5}.com.BithellGames.Arcsmith.obb",
                         speed=f"{speed:.1f} MB/s · ~{left:.0f} s left" if f < 1 else f"{speed:.1f} MB/s")
            time.sleep(0.22)
        stage("Artwork for the Steam library", 0.8)
        stage("Finalize install", 0.9)
        stage("Add to Steam library", 0.8)
        stage("Launch test (headless)", 1.8, ["process alive", "OpenXR session created", "frames paced: 72 fps"])
        job.summary = {"verdict": "pass", "findings": []}
        rep.stage("Launch test: passed")
        time.sleep(0.5)
        return "Arcsmith is ready on your Frame: put the headset on and launch it from your Steam library"

    def play(app, page):
        time.sleep(1.0)
        app.submit(f"Install {app._title(game)} on Frame", run, game, "install", to="frame")
        deadline = time.time() + 40
        while time.time() < deadline and any(j.active for j in app.jobs.jobs):
            time.sleep(0.3)
        time.sleep(1.8)  # done: the cover arrives, the toast
    return play


def live_setup(app):
    # disconnected-looking: no target, state "none" (the fake isn't attached yet)
    app.target, app.frame_info, app.frame_state = None, None, "none"
    app.go("library")


def live_play(app, page):
    time.sleep(1.2)
    app.frame_state = "connecting"
    app._refresh_sidebar()
    time.sleep(1.0)
    attach_fake_frame(app, LiveMonitorSession)
    app.refresh_view()
    app._refresh_sidebar()
    time.sleep(14)


def navigation_setup(app):
    app.go("library")


def navigation_play(app, page):
    m = page.mouse
    time.sleep(0.8)
    for tab in ("frame", "files", "monitor", "settings", "library"):
        m.move(*TAB[tab], steps=8)
        time.sleep(0.25)
        m.click(*TAB[tab])
        time.sleep(1.3)
    # type to search: an empty part of the header, then the keyboard
    m.move(480, 60, steps=6)
    m.click(480, 60)
    time.sleep(0.4)
    page.keyboard.type("arc", delay=160)
    time.sleep(1.6)
    page.keyboard.press("Escape")
    time.sleep(1.2)
    # the "On your Frame" shelf's first card: lift + Play
    m.move(403, 268, steps=12)
    time.sleep(1.6)
    # a grid card's menu
    m.move(360, 540, steps=10)
    time.sleep(0.6)
    m.click(360, 540, button="right")
    time.sleep(0.9)
    for dy in (20, 60, 100, 140, 180):
        m.move(400, 540 + dy, steps=5)
        time.sleep(0.35)
    page.keyboard.press("Escape")
    time.sleep(0.4)
    m.move(*NOWHERE, steps=8)
    time.sleep(1.0)


def selection_setup(app):
    app.go("files")


def selection_play(app, page):
    m = page.mouse
    time.sleep(0.8)
    m.move(900, 320, steps=10)  # a row: its checkbox and actions fade in
    time.sleep(1.4)
    m.move(900, 268, steps=6)
    time.sleep(0.5)
    m.down()
    for pt in ((900, 320), (900, 372)):
        m.move(*pt, steps=20)
        time.sleep(0.4)
    m.up()
    time.sleep(1.2)
    m.move(900, 320, steps=5)
    time.sleep(0.3)
    m.click(900, 320, button="right")
    time.sleep(0.8)
    for dy in (20, 68, 116):
        m.move(960, 320 + dy, steps=5)
        time.sleep(0.4)
    page.keyboard.press("Escape")
    time.sleep(0.6)
    m.move(*TAB["screenshots"], steps=10)
    m.click(*TAB["screenshots"])
    time.sleep(2.2)
    for x in (410, 680, 946):  # hover the first row
        m.move(x, 275, steps=10)
        time.sleep(0.6)
    for x in (305, 573):  # select two
        m.move(x, 227, steps=10)
        time.sleep(0.4)
        m.click(x, 227)
        time.sleep(0.8)
    m.move(700, 760, steps=10)
    time.sleep(1.5)


def themes_setup(app):
    app.go("settings")


def themes_play(app, page):
    m = page.mouse
    time.sleep(1.0)
    for x, y in ((838, 268), (1090, 268), (587, 420), (587, 275)):  # Portal (OLED), Original, Ember & Ice, Portal
        m.move(x, y, steps=12)
        time.sleep(0.3)
        m.click(x, y)
        time.sleep(2.4)
    m.move(*TAB["library"], steps=12)
    m.click(*TAB["library"])
    time.sleep(3.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--game", default=None)
    ap.add_argument("--only", default=None, help="comma-separated clip names (transit,live-card,navigation,…)")
    ap.add_argument("--crf", type=int, default=26)
    ap.add_argument("--width", type=int, default=W, help="scale the MP4 to this width (e.g. 1280)")
    ap.add_argument("--keep-raw", action="store_true", help="keep Playwright's .webm recordings in <out>/raw")
    args = ap.parse_args()
    game = args.game or (library.games()[0]["package"] if library.games() else None)
    if not game:
        print("the library is empty: nothing to show", file=sys.stderr)
        return 2
    library.set_setting("ui.theme", "portal")
    from frameport.ui import theme as T
    from frameport.ui.app import assets_dir, themes_dir

    T.set_scale(1.0)
    T.load_user_themes(themes_dir())
    install_fakes(game)
    # the transit last: the job queue is shared by every session, and its finished install would show in the
    # sidebar's activity card of the clips after it
    scenarios = [("live-card", live_setup, live_play, 4.0),
                 ("navigation", navigation_setup, navigation_play, 0.5),
                 ("selection", selection_setup, selection_play, 3.5),
                 ("themes", themes_setup, themes_play, 3.0),
                 ("transit", transit_setup(game), transit_play(game), 9.0)]
    if args.only:
        keep = args.only.split(",")
        scenarios = [s for s in scenarios if s[0] in keep]
    out, raw = args.out, args.out / "raw"
    out.mkdir(parents=True, exist_ok=True)
    raw.mkdir(exist_ok=True)

    def app_main(page: ft.Page):
        try:
            app = FramePortApp(page)
            attach_fake_frame(app)
        except Exception:  # noqa: BLE001
            ERRORS.append("startup: " + traceback.format_exc())
            return
        APPS.put(app)

    def convert(webm: Path, name: str, start: float, length: float, poster_at: float) -> None:
        mp4, jpg = out / f"{name}.mp4", out / f"{name}.jpg"
        scale = [] if args.width == W else ["-vf", f"scale={args.width}:-2:flags=lanczos"]
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(webm),
                        "-t", f"{length:.2f}", *scale, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf",
                        str(args.crf), "-preset", "slow", "-movflags", "+faststart", "-an", str(mp4)], check=True)
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{min(poster_at, length - 0.2):.2f}", "-i",
                        str(mp4), "-frames:v", "1", "-q:v", "3", str(jpg)], check=True)
        print(f"CLIP {name}: {length:.1f} s, {mp4.stat().st_size / 1e6:.2f} MB", flush=True)

    def recorder():
        import socket

        from playwright.sync_api import sync_playwright

        for _ in range(90):
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
                break
            except OSError:
                time.sleep(1)
        time.sleep(2)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
                for name, setup, play, poster in scenarios:
                    while not APPS.empty():  # sessions from elsewhere (a browser the web mode opened)
                        APPS.get_nowait()
                    ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(raw),
                                              record_video_size={"width": W, "height": H})
                    page = ctx.new_page()
                    t0 = time.monotonic()
                    page.goto(f"http://127.0.0.1:{PORT}", wait_until="networkidle", timeout=120_000)
                    app = APPS.get(timeout=120)
                    time.sleep(2)  # the first screen + web fonts
                    ui(app, setup, app)
                    time.sleep(3.5)  # the scenario's first view painted, cards loaded
                    page.mouse.move(*NOWHERE)
                    start = time.monotonic() - t0
                    try:
                        play(app, page)
                    except Exception:  # noqa: BLE001
                        ERRORS.append(f"{name}: " + traceback.format_exc())
                    length = time.monotonic() - t0 - start
                    video = page.video
                    ctx.close()  # flushes the video
                    webm = Path(video.path())
                    target = raw / f"{name}.webm"
                    webm.replace(target)
                    convert(target, name, start + 0.2, length, poster)
                    library.set_setting("ui.theme", "portal")  # the next session starts in Portal again
                browser.close()
        except Exception:  # noqa: BLE001
            ERRORS.append("browser: " + traceback.format_exc())
        if not args.keep_raw:
            shutil.rmtree(raw, ignore_errors=True)
        for e in ERRORS:
            print("ERROR", e, flush=True)
        os._exit(1 if ERRORS else 0)

    threading.Thread(target=recorder, daemon=True).start()
    ft.run(app_main, view=ft.AppView.WEB_BROWSER, port=PORT, assets_dir=assets_dir())
    return 1


if __name__ == "__main__":
    sys.exit(main())
