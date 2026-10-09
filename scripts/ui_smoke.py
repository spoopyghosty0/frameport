#!/usr/bin/env python3
"""GUI smoke test: serve the Flet app on the web, drive its screens from Python, screenshot each with headless
Chromium (Playwright). Any exception in a view shows up on stdout; screenshots land in --out.

    FRAMEPORT_HOME=<a test data dir> python scripts/ui_smoke.py --out /tmp/shots [--game <package>]

--linux adds two pretend arm64 Linux apps (an AppImage and a VR folder app, tiny fake ELF files in FRAMEPORT_HOME)
and renders their pages; with --fake-frame the AppImage is "installed" with a missing library.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import flet as ft  # noqa: E402
from showcase.fakes import (  # noqa: E402,F401
    LINUX_APPIMAGE,
    LINUX_FOLDER,
    add_fake_linux_apps,
    attach_fake_frame,
    fake_live_stream,
    install_fakes,
    open_monitor_details,
    open_type_tab,
    start_fake_install,
    stop_fake_live,
)

from frameport.core import library  # noqa: E402
from frameport.ui.app import FramePortApp  # noqa: E402

PORT = int(os.environ.get("FRAMEPORT_SMOKE_PORT", "8557"))  # two smoke runs at once need different ports
ERRORS: list[str] = []


PAINT_SECONDS = 3  # after a step, before its screenshot: Flutter paints (and a view's content loads)
STEP_TIMEOUT = 240  # the longest a step may wait for its screenshot (a crashed browser mustn't hang the run)


def driver(app: FramePortApp, steps: list[tuple[str, callable]], ready: threading.Event, shot_done: threading.Event,
           done_step: list):
    """Runs one step, hands it to the shooter ("ready") and waits until its screenshot and mouse actions are done
    ("shot_done") before the next one: so a screenshot always shows its own step, however long the shooter takes."""
    for name, action in steps:
        try:
            action(app)
        except Exception:  # noqa: BLE001
            ERRORS.append(f"{name}: {traceback.format_exc()}")
        done_step.append(name)
        shot_done.clear()
        ready.set()
        if not shot_done.wait(STEP_TIMEOUT):
            ERRORS.append(f"{name}: no screenshot within {STEP_TIMEOUT} s, stopping")
            return


def find_button(root, text: str):
    """The first button under `root` whose label is `text` (walks Flet's control tree)."""
    seen, stack = set(), [root]
    while stack:
        c = stack.pop()
        if c is None or id(c) in seen:
            continue
        seen.add(id(c))
        label = getattr(c, "content", None)
        if (label == text or getattr(c, "text", None) == text) and getattr(c, "on_click", None):
            return c
        for attr in ("controls", "content", "actions"):
            v = getattr(c, attr, None)
            if isinstance(v, list):
                stack.extend(v)
            elif v is not None and not isinstance(v, str):
                stack.append(v)
    return None


def click_usb_setup(app) -> None:
    app.navigate(1)
    time.sleep(2)
    button = find_button(app.body, "Set up with a USB cable")
    print(f"usb button found: {button is not None}", flush=True)
    if button:
        button.on_click(None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--game", default=None)
    ap.add_argument("--frame", default=None, help="steamos@host: also connect and run a launch-test job for --game")
    ap.add_argument("--no-test", action="store_true", help="with --frame: skip the launch-test job")
    ap.add_argument("--scale", type=float, default=1.0, help="UI scale to render at (e.g. 1.5)")
    ap.add_argument("--viewport", default="1280x820", help="browser size, e.g. 2560x1440")
    ap.add_argument("--update", action="store_true", help="pretend a new FramePort release exists (update UI: "
                    "changelog dialog, what's new bar + dialog, Settings changelog)")
    ap.add_argument("--fake-frame", action="store_true", help="pretend a Steam Frame is connected (no device needed)")
    ap.add_argument("--hover", default=None, help="x,y: move the mouse there before the Library screenshot")
    ap.add_argument("--install-questions", nargs="+", metavar="PKG", default=None,
                    help="open the install questions for these games, then click each dialog's main button twice "
                         "(a double click must not duplicate dialogs); nothing is installed")
    ap.add_argument("--usb-setup", action="store_true", help="click 'Set up with a USB cable' on the connect page "
                    "(a Frame cabled to this PC) and screenshot what follows")
    ap.add_argument("--docs", action="store_true", help="only the screens used in the docs (Library, --game, Frame)")
    ap.add_argument("--gestures", action="store_true", help="with --fake-frame: real mouse drags (drag-select) and "
                    "right-clicks (menus) in Files, Screenshots and the Library; prints what got selected "
                    "(positions for --viewport 1440x900)")
    ap.add_argument("--only", default=None, help="comma-separated step names to keep (e.g. monitor,monitor-details)")
    ap.add_argument("--linux", action="store_true", help="add two pretend arm64 Linux apps and render their pages")
    ap.add_argument("--links", action="store_true", help="install links: the Add games menu, the paste dialog, the "
                    "confirmation for a pretend FrameDrop manifest, Settings → Install links and (with --game) the "
                    "VR / flat window choice")
    ap.add_argument("--themes", action="store_true", help="Settings' theme picker, then switch every theme while "
                    "running (Library and --game in each)")
    args = ap.parse_args()
    if args.linux:
        add_fake_linux_apps()
    from frameport.ui import theme

    theme.set_scale(args.scale)
    vw, vh = (int(x) for x in args.viewport.split("x"))
    args.out.mkdir(parents=True, exist_ok=True)
    game = args.game or (library.games()[0]["package"] if library.games() else None)
    steps = [("library", lambda a: a.navigate(0)), ("frame", lambda a: a.navigate(1)),
             ("files", lambda a: a.go("files")), ("screenshots", lambda a: a.go("screenshots")),
             ("live", lambda a: a.go("live")), ("live-streaming", fake_live_stream),
             ("keyboard", lambda a: (stop_fake_live(a), a.go("keyboard"))),
             ("monitor", lambda a: a.go("monitor")), ("monitor-details", open_monitor_details),
             ("tools", lambda a: a.go("settings")),
             # the index on the left: "This PC" scrolls the page there
             ("settings-index", lambda a: a.page.run_task(a.settings_view.show_section, "pc"))]
    if game:
        steps.insert(1, ("game", lambda a: a.open_game(game)))
        steps.insert(2, ("game-customize", lambda a: a.open_game(game, advanced=True)))
        steps += [("share-dialog", lambda a: a.share_config_dialog(game)),
                  ("report-dialog", lambda a: (a.page.pop_dialog(), a.report_problem_dialog(game))),
                  ("custom-art-dialog", lambda a: (a.page.pop_dialog(), a.custom_artwork(game)))]
        # a pretend install of --game, mid-upload: the transit in the sidebar, the game page and Activity
        steps += [("transit-game", lambda a: (a.page.pop_dialog(), start_fake_install(a, game), a.open_game(game))),
                  ("transit-activity", lambda a: a.show_activity(True))]
        if args.fake_frame:  # GitHub #90: the Move to… dialog (drives load in the background)
            steps.append(("move-dialog", lambda a: (a.page.pop_dialog(), a.move_game(game))))
        # last: the right-click menu stays open over whatever comes next
        steps.append(("library-menu", lambda a: (a.show_activity(False), a.navigate(0), time.sleep(3),
                                                 a.library_view.open_menu(game))))
    if args.docs:
        steps = [("library", lambda a: a.navigate(0))]
        if game:
            steps += [("game", lambda a: a.open_game(game)),
                      ("game-customize", lambda a: a.open_game(game, advanced=True))]
        steps.append(("frame", lambda a: a.navigate(1)))
        steps.append(("files", lambda a: a.go("files")))
        steps.append(("files-select", lambda a: [a.files_view._toggle(e.path, True)
                                                 for e in a.files_view.entries[1:3]]))
        steps.append(("screenshots", lambda a: a.go("screenshots")))
        steps.append(("screenshot-viewer", lambda a: a.screenshots_view.viewer(0)))
        steps.append(("type-on-frame", lambda a: (a.page.pop_dialog(), open_type_tab(a))))
        steps.append(("monitor", lambda a: a.go("monitor")))
        steps.append(("power-confirm", lambda a: (a.page.pop_dialog(), a.frame_power("restart"))))
    # step name -> mouse actions (Playwright page) after its screenshot, then a "-mouse" screenshot unless the
    # actions return True (they saved their own picture, e.g. an opened menu)
    mouse: dict[str, callable] = {}
    hover: dict[str, tuple[float, float]] = {}  # step name -> where the mouse rests for its screenshot
    if args.hover:
        hover["library"] = tuple(float(v) for v in args.hover.split(","))
    if not args.docs and args.viewport == "1440x900":  # menus that open from buttons (positions at this size)
        def open_and_shoot(x, y, name):
            def run(page):
                page.mouse.click(x, y)
                time.sleep(2)
                page.screenshot(path=str(args.out / f"{name}.png"))
                page.mouse.click(1420, 880)  # outside the menu (Flutter's popup ignores Escape)
                return True
            return run
        mouse["library"] = open_and_shoot(1354, 54, "library-add-menu")
        # type to search: click an empty part of the header, then type (the "-mouse" shot shows "arc" + the matches)
        steps.insert(1, ("library-typed", lambda a: a.library_view.clear_search()))
        def typed(page):
            page.mouse.click(480, 60)
            time.sleep(0.5)
            page.keyboard.type("arc", delay=120)
            if args.fake_frame:  # the matches' cover buttons: Install (Arcsmith, not installed), Update (Pinball FX)
                time.sleep(PAINT_SECONDS)
                for name, pt in (("library-install-hover", (742, 215)), ("library-install-button", (742, 276)),
                                 ("library-update-button", (357, 276))):
                    page.mouse.move(*pt, steps=8)
                    time.sleep(1.2)
                    page.screenshot(path=str(args.out / f"{name}.png"))
                page.mouse.move(1300, 700)
        mouse["library-typed"] = typed
        steps.insert(2, ("library-cleared", lambda a: a.library_view.clear_search()))
        if args.fake_frame:  # the "On your Frame" shelf's first card, hovered: its Play button shows
            steps.insert(3, ("library-shelf-hover", lambda a: None))
            hover["library-shelf-hover"] = (403, 268)
            # the grid's first card: hovered (lift, zoom, the glass button), the pointer on its button, then Play:
            # the portal rings mid-ripple and the "Starting on Frame…" pill
            steps.insert(4, ("library-card-hover", lambda a: None))
            hover["library-card-hover"] = (357, 420)
            steps.insert(5, ("library-card-button", lambda a: None))
            hover["library-card-button"] = (357, 497)

            def launch(page):
                page.mouse.move(357, 497)
                time.sleep(1.2)
                page.mouse.click(357, 497)
                for i in range(6):  # the launch, frame by frame (rings, then the pill)
                    time.sleep(0.15)
                    page.screenshot(path=str(args.out / f"library-play-launch-{i}.png"))
                time.sleep(1.0)
                page.screenshot(path=str(args.out / "library-play-starting.png"))
                page.mouse.move(130, 520)
                return True
            mouse["library-card-button"] = launch
        if game:
            mouse["game"] = open_and_shoot(491, 306, "game-more-menu")  # the hero's "…"
    if args.gestures:  # the mouse actions run after each step's screenshot, before the next step
        def drag(*points):
            def run(page):
                page.mouse.move(*points[0])
                time.sleep(0.5)
                page.mouse.down()
                for pt in points[1:]:
                    page.mouse.move(*pt, steps=25)
                    time.sleep(0.4)
                page.mouse.up()
            return run

        def right_click(x, y):
            return lambda page: (page.mouse.move(x, y), time.sleep(0.5), page.mouse.click(x, y, button="right"))

        def report(label, view, extra=""):
            def run(a):
                v = getattr(a, view)
                print(f"GESTURE {label}: selected={sorted(v.selected)} {extra and eval(extra)}", flush=True)
            return run

        def escape(page):
            page.keyboard.press("Escape")
        steps = [("files", lambda a: a.go("files")),
                 ("files-dragged", report("files drag", "files_view")),
                 ("files-menu", lambda a: None),
                 ("files-space-menu", lambda a: None),
                 ("screenshots", lambda a: a.go("screenshots")),
                 ("screenshots-dragged", report("screenshots drag", "screenshots_view")),
                 ("screenshots-menu", lambda a: None),
                 ("library", lambda a: a.navigate(0)),
                 ("library-dragged", report("library drag", "library_view", "a.library_view.select_mode")),
                 ("library-undrag", report("library drag back", "library_view"))]
        mouse = {"files": drag((900, 268), (900, 320), (900, 372)),          # the folder + 2 files
                 "files-dragged": right_click(900, 320),                      # a selected one: "… 3 items"
                 "files-menu": lambda page: (escape(page), time.sleep(0.5), right_click(900, 600)(page)),
                 "files-space-menu": escape,
                 "screenshots": drag((400, 300), (680, 300), (950, 300)),     # 3 shots (the first row)
                 "screenshots-dragged": right_click(950, 300),
                 "screenshots-menu": escape,
                 # the grid's first row (below the "On your Frame" shelf)
                 "library": drag((360, 560), (560, 560), (760, 560)),        # 3 cards, select mode on
                 "library-dragged": drag((560, 560), (760, 560))}             # from a selected card: deselects 2
    if args.linux:
        def linux_filter(a, value):
            a.navigate(0)
            time.sleep(2)
            a.library_view._set("platform", value)
        steps = [("library", lambda a: a.navigate(0)), ("library-linux", lambda a: linux_filter(a, "linux")),
                 ("linux-appimage", lambda a: (a.library_view._set("platform", "all"), a.open_game(LINUX_APPIMAGE))),
                 ("linux-folder", lambda a: a.open_game(LINUX_FOLDER)),
                 ("linux-folder-customize", lambda a: a.open_game(LINUX_FOLDER, advanced=True)),
                 ("linux-change-program", lambda a: a.choose_exe(LINUX_FOLDER)),
                 ("frame", lambda a: (a.page.pop_dialog(), a.navigate(1))),
                 ("linux-move", lambda a: a.move_game(LINUX_APPIMAGE) if args.fake_frame else None),
                 ("linux-menu", lambda a: (a.page.pop_dialog(), a.navigate(0), time.sleep(3),
                                           a.library_view.open_menu(LINUX_APPIMAGE)))]
    if args.links:
        from frameport import deeplink
        from frameport.ui.views import link_dialog

        fake = deeplink.Manifest("Example Game", [
            deeplink.ManifestFile("https://cdn.example.com/example-game-arm64.apk", "ab" * 32),
            deeplink.ManifestFile("https://cdn.example.com/main.1.com.example.game.obb")],
            "https://example.com/example-game.framedrop.json",
            description="A puzzle adventure across floating islands: build bridges, bend light and find your way home. "
                        "Room-scale or seated, with smooth or snap turning.",
            icon="https://example.com/icon.png")
        sizes = {fake.files[0].url: 412_000_000, fake.files[1].url: 1_900_000_000}
        from PIL import Image, ImageDraw

        from frameport.core.paths import user_data_dir

        icon = user_data_dir() / "downloads" / "icons" / "smoke-example.png"  # a stand-in for the downloaded icon
        icon.parent.mkdir(parents=True, exist_ok=True)
        im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        ImageDraw.Draw(im).rounded_rectangle((8, 8, 248, 248), 48, fill=(124, 92, 255, 255))
        ImageDraw.Draw(im).ellipse((72, 72, 184, 184), fill=(255, 214, 102, 255))
        im.save(icon)

        steps = [("library", lambda a: a.navigate(0)),
                 ("links-paste", lambda a: a.pick_link()),
                 ("links-confirm", lambda a: (a.page.pop_dialog(), link_dialog._confirm(a, fake, sizes, False, icon))),
                 ("links-settings", lambda a: (a.page.pop_dialog(), a.go("settings")))]
        if game:
            steps.append(("links-display-mode", lambda a: a.open_game(game, advanced=True)))
        def add_menu_open(page):
            page.mouse.click(1194, 94)  # Add games (1280-wide viewport)
            time.sleep(2)
            page.screenshot(path=str(args.out / "library-add-menu.png"))
            page.mouse.click(120, 520)  # outside the menu (Flutter's popup ignores Escape)
            return True
        # tall pages: run with e.g. --viewport 1280x3200 (at 1440x900 the tour's own Add games click is kept)
        mouse.setdefault("library", add_menu_open)
    if args.themes:
        from frameport.ui import theme as T
        from frameport.ui.app import themes_dir

        T.load_user_themes(themes_dir())  # installed theme files are switched through too

        def switch(name):
            def run(a):
                library.set_setting("ui.theme", name)  # what the picker does
                a.restyle(name)
            return run

        def appearance(a):
            """Settings: Appearance is its first section."""
            a.go("settings")
        steps = [("appearance", appearance)]
        for name in [n for n in T.THEMES if n != T.THEME] + [T.THEME]:
            steps.append((f"{name}-switched", switch(name)))
            steps.append((f"{name}-appearance", appearance))
            steps.append((f"{name}-library", lambda a: a.navigate(0)))
            if game:
                steps.append((f"{name}-game", lambda a: a.open_game(game)))
    if args.usb_setup:
        def continue_usb(a):
            dialog = [d for d in a.page._dialogs.controls if d.open and type(d).__name__ == "AlertDialog"][-1]
            button = find_button(dialog, "Done: look for the cable") or dialog.actions[-1]  # (label inside a Row)
            print(f"dialog button found: {button is not None}", flush=True)
            button.on_click(None)
        steps = [("usb-explain", click_usb_setup), ("usb-continue", continue_usb),
                 ("usb-wait", lambda a: time.sleep(8)), ("usb-after", lambda a: time.sleep(10))]
    if args.install_questions:
        queued: list[str] = []

        def open_questions(a):
            a._submit_install = lambda p, to: queued.append(p)
            a.install_many(args.install_questions, "frame")

        def double_click(a):
            dialogs = [d for d in a.page._dialogs.controls if d.open]
            print(f"open dialogs: {len(dialogs)}", flush=True)
            button = dialogs[-1].actions[-1]
            button.on_click(None)
            button.on_click(None)

        def report(a):
            print(f"open dialogs: {len([d for d in a.page._dialogs.controls if d.open])}, queued: {queued}", flush=True)
        steps = [("library", lambda a: a.navigate(0)), ("questions", open_questions),
                 ("after-1st-double", double_click), ("after-2nd-double", double_click), ("end", report)]
    if args.update:
        from frameport import __version__, updates

        notes = ("FramePort v9.9.9: Windows (x64), macOS (Apple Silicon) and Linux (x64, ARM64).\n\n## What's new\n\n"
                 "- Self-update test release: the update dialog shows every version since yours\n"
                 "- **Bold** and `code` in notes, and a [link](https://example.invalid)\n- A third bullet\n\n"
                 "Already have FramePort? It offers this update itself (Library → **Update now**).\n\n## Install\n\n"
                 "- footer that the changelog leaves out\n")
        fake = updates.Update(version="9.9.9", tag="v9.9.9", page="https://example.invalid", asset=None, asset_url=None,
                              sums_url=None, wheel_url=None, notes=notes, published="2026-10-08T10:00:00Z")
        major, minor, _patch = updates.parse_version(__version__)[:3]
        older = f"{major}.{max(minor - 1, 0)}"

        def entry(version, date, body):
            return updates.ChangelogEntry(version=version, tag=f"v{version}", date=date, title=f"FramePort v{version}",
                                          body=body, page="https://example.invalid",
                                          dev=updates.is_dev_version(version))
        fake_log = [updates.entry_from_update(fake),
                    entry("9.9.8", "2026-10-06", "- Install links from FrameDrop buttons\n- Faster uploads over USB"),
                    entry("9.9.7", "2026-10-04", "- Monitor tab with live Frame stats"),
                    entry(__version__, "2026-10-02", "- The version you have: **Live view** with sound\n"
                                                     "- Files tab: drag and drop\n- Smaller fixes"),
                    entry(f"{older}.3", "2026-09-30", "- Proton defaults to the newest stable build"),
                    entry(f"{older}.2", "2026-09-28", "- Language packs")]
        updates.fetch_changelog = lambda force=False, need=None: list(fake_log)  # no GitHub in screenshots

        def news(a):
            a.updater._set(None)
            a.updater.news = updates.recent_history(fake_log, n=3)
            a.navigate(0)
            a.refresh_view()

        def settings_updates(a):
            a.go("settings")
            # once the page is mounted: scrolling an unmounted column does nothing
            threading.Timer(1.0, lambda: a.page.run_task(a.settings_view.show_section, "updates")).start()
        steps += [("update-banner", lambda a: (a.updater._set(fake), a.navigate(0))),
                  ("update-dialog", lambda a: a.updater.show_dialog()),
                  ("update-settings", lambda a: (a.page.pop_dialog(), settings_updates(a))),
                  ("update-whats-new-bar", lambda a: news(a)),
                  ("update-whats-new-dialog", lambda a: a.updater.show_news()),
                  ("update-settings-changelog", lambda a: (a.page.pop_dialog(), settings_updates(a),
                                                           a.updater.show_history()))]
    if args.frame:
        from frameport.frame.connection import parse_target

        def connect(a):
            a.connect(parse_target(args.frame))
            for _ in range(60):
                if a.target:
                    break
                time.sleep(1)
            a.navigate(1)

        def job(a):
            a.start_job(game, build=False, install=False, test=True)
            time.sleep(5)
            while a.job_running:
                time.sleep(2)

        steps += [("frame-connected", connect), ("library-connected", lambda a: a.navigate(0))]
        if game:
            steps.append(("game-connected", lambda a: a.open_game(game)))
        if not args.no_test:
            steps.append(("launch-test-job", job))
    if args.only:  # in the order given (e.g. frame-connected,monitor)
        by_name = dict(steps)
        steps = [(n, by_name[n]) for n in args.only.split(",") if n in by_name]
    # a first screen while the web fonts load: shot before that, text measured with a fallback font wrapped or
    # overlapped ("Q / uest", a check mark over its pill's text); its picture is 00-warmup.png. Settings, not the
    # Library: opening the Library twice restarts its card loading and the next shot caught it empty
    steps.insert(0, ("warmup", lambda a: a.go("settings")))
    ready, shot_done, done = threading.Event(), threading.Event(), []

    if args.fake_frame:  # never reach a real Frame (start-up auto-connect, discovery, the 30 s poll)
        install_fakes(game)

    def app_main(page: ft.Page):
        try:
            app = FramePortApp(page)
            if args.fake_frame:
                attach_fake_frame(app)
        except Exception:  # noqa: BLE001
            ERRORS.append("startup: " + traceback.format_exc())
            return
        threading.Thread(target=driver, args=(app, steps, ready, shot_done, done), daemon=True).start()

    def shooter():
        # wait until the app's web server listens (a fixed 6 s was too short with another smoke run on the machine)
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
                page = browser.new_page(viewport={"width": vw, "height": vh})
                page.goto(f"http://127.0.0.1:{PORT}", wait_until="networkidle", timeout=120_000)
                shot = handled = 0
                while handled < len(steps):  # one hand-off per step (see driver)
                    if not ready.wait(STEP_TIMEOUT):
                        ERRORS.append(f"browser: no step after {done[-1] if done else 'start'} "
                                      f"within {STEP_TIMEOUT} s")
                        break
                    ready.clear()
                    name = done[-1]
                    try:
                        time.sleep(PAINT_SECONDS)
                        if name in hover:
                            page.mouse.move(*hover[name])
                            time.sleep(1.5)
                        page.screenshot(path=str(args.out / f"{shot:02d}-{name}.png"))
                        shot += 1
                        if name in hover:
                            page.mouse.move(130, 520)  # the sidebar's empty part: nothing left hovered
                        if name in mouse and mouse[name](page) is not True:
                            time.sleep(PAINT_SECONDS)  # e.g. typed search: the matching cards fade in
                            page.screenshot(path=str(args.out / f"{shot:02d}-{name}-mouse.png"))
                            shot += 1
                    finally:
                        handled += 1
                        shot_done.set()
                browser.close()
        except Exception:  # noqa: BLE001
            ERRORS.append("browser: " + traceback.format_exc())
        for e in ERRORS:
            print("ERROR", e, flush=True)
        print(f"screens: {[p.name for p in sorted(args.out.glob('*.png'))]}", flush=True)
        os._exit(1 if ERRORS else 0)

    threading.Thread(target=shooter, daemon=True).start()
    from frameport.ui.app import assets_dir

    ft.run(app_main, view=ft.AppView.WEB_BROWSER, port=PORT, assets_dir=assets_dir())
    return 1


if __name__ == "__main__":
    sys.exit(main())
