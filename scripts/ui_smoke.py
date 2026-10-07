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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import flet as ft  # noqa: E402

from frameport.core import library  # noqa: E402
from frameport.ui.app import FramePortApp  # noqa: E402

PORT = int(os.environ.get("FRAMEPORT_SMOKE_PORT", "8557"))  # two smoke runs at once need different ports
ERRORS: list[str] = []


class FakeFS:
    """--fake-frame's file system for the Files tab: a temp folder with sample files, served like SFTP."""

    def __init__(self):
        import tempfile

        self.home = tempfile.mkdtemp(prefix="fp-fake-frame-")
        samples = {"Videos": ["Earth from orbit (360).mp4", "Mars landing (VR180).mkv", "Concert 8K 3D.mp4"],
                   "Downloads": ["Mods/", "readme.txt"], "Documents": ["Saves/", "Notes.pdf"]}
        sizes = {".mp4": 2_400_000_000, ".mkv": 1_100_000_000}
        for folder, names in samples.items():
            base = os.path.join(self.home, folder)
            os.makedirs(os.path.join(base, "Trips"), exist_ok=True)
            for n in names:
                if n.endswith("/"):
                    os.makedirs(os.path.join(base, n), exist_ok=True)
                else:
                    with open(os.path.join(base, n), "wb") as f:
                        f.truncate(sizes.get(os.path.splitext(n)[1], 48_000))  # sparse: no real disk use
        self.shots = self._screenshots()

    def _screenshots(self) -> list[dict]:
        """Sample Steam screenshots for the Screenshots tab, two days, a few games + SteamVR: crops of the games' own
        store art (hero/landscape) when the library has some (docs screenshots), else colour gradients."""
        from PIL import Image, ImageOps

        from frameport.artwork import fetch

        folder = os.path.join(self.home, "shots")
        os.makedirs(folder, exist_ok=True)
        games = []
        for g in library.games():
            if g.get("kind") == "rift":
                continue
            # the store's own screenshots look like real captures; else hero/wide art
            shots_art = sorted(fetch.artwork_dir(g["package"]).glob("shot_*.jpg"))
            games.append((g, shots_art or [p for p in fetch.files(g["package"]) if p.stem in ("hero", "landscape")],
                          bool(shots_art)))
        games.sort(key=lambda ga: (not ga[2], -len(ga[1])))
        owners = [(g["package"], g.get("title") or g["package"], arts) for g, arts, _real in games[:3]]
        spare = games[3][1] if len(games) > 3 else []  # SteamVR's own shots (taken outside a FramePort game)
        owners.append((None, "SteamVR", spare))
        now, shots = time.time(), []
        for i in range(10):
            t = now - i * 2400 - (86400 if i >= 6 else 0)
            name = time.strftime("%Y%m%d%H%M%S", time.localtime(t)) + "_1.jpg"
            path = os.path.join(folder, name)
            pkg, title, arts = owners[i % len(owners)]
            if arts:  # a different crop of the game's art per shot, so they don't all look alike
                with Image.open(arts[i % len(arts)]) as src:
                    src = src.convert("RGB")
                    zoom = 1.0 + 0.15 * (i % 3) * (src.width < 2 * src.height)
                    w, h = int(src.width / zoom), int(src.height / zoom)
                    x, y = (src.width - w) * (i % 2), (src.height - h) // 2
                    im = ImageOps.fit(src.crop((x, y, x + w, y + h)), (1280, 720))
            else:
                im = Image.linear_gradient("L").resize((640, 360)).convert("RGB")
                im = Image.merge("RGB", (im.getchannel(0).point(lambda v, i=i: (v + 40 * i) % 256),
                                         im.getchannel(1).point(lambda v: 255 - v), im.getchannel(2)))
            im.save(path, "JPEG", quality=85)
            shots.append({"path": path, "thumb": None, "time": int(t), "width": 1920, "height": 1080,
                          "size": os.path.getsize(path), "account": "1", "appid": "250820", "package": pkg,
                          "title": title})
        return shots

    def agent(self, command, **args):
        if command == "storage_targets":
            return {"targets": [{"id": n.lower(), "path": os.path.join(self.home, n), "android": a, "shared": True}
                                for n, a in (("Videos", "/sdcard/Movies"), ("Downloads", "/sdcard/Download"),
                                             ("Documents", "/sdcard/Documents"))]}
        if command == "list_screenshots":
            want = args.get("package")
            shots = [s for s in self.shots if want is None or (s["package"] or "") == want]
            games = {}
            for s in self.shots:
                games.setdefault(s["package"] or "", {"package": s["package"], "title": s["title"], "count": 0})
                games[s["package"] or ""]["count"] += 1
            return {"shots": shots, "total": len(shots), "games": list(games.values())}
        raise RuntimeError(f"fake Frame: {command} not available")

    @property
    def sftp(self):
        import paramiko

        class SFTP:
            def listdir_attr(self, path):
                out = []
                for name in sorted(os.listdir(path)):
                    a = paramiko.SFTPAttributes.from_stat(os.lstat(os.path.join(path, name)))
                    a.filename = name
                    out.append(a)
                return out

            def stat(self, path):
                return paramiko.SFTPAttributes.from_stat(os.stat(path))

            def get(self, remote, local, callback=None):
                import shutil

                shutil.copyfile(remote, local)
        return SFTP()


class FakeTarget:
    """--fake-frame: a pretend Steam Frame with the library's working Quest builds installed (no network, no device),
    for documentation screenshots."""

    def __init__(self, count: int = 8):
        from frameport.frame.connection import parse_target

        self.label = "steamframe"
        self.frame = FakeFS()
        self.target = parse_target("steamos@steamframe.local")
        works = [g for g in library.games() if g.get("kind") != "rift" and (g.get("build") or {}).get("sha256")
                 and (g.get("recipe") or {}).get("status") in ("works", "issues")]
        works.sort(key=lambda g: -(g.get("data_bytes") or 0))
        self.games = [{"package": g["package"], "kind": "quest", "title": g.get("title"),
                       "apk_size": (g.get("data_bytes") or 0) + 2**28, "sha256": g["build"]["sha256"],
                       "recipe": {"patches": (g.get("recipe") or {}).get("patches", [])}} for g in works[:count]]
        # --linux: the AppImage is on the Frame, but SteamOS lacks one of its libraries
        self.games += [{"package": g["package"], "kind": "linux", "title": g.get("title"), "apk_size": 2**26,
                        "apk_present": True, "recipe": None, "exe": g.get("exe"),
                        "missing_libraries": ["libwebkit2gtk-4.1.so.0"]}
                       for g in library.games() if g.get("kind") == "linux" and g["package"] == LINUX_APPIMAGE]

    def describe(self) -> dict:
        return {"hostname": "steamframe", "os": "SteamOS", "os_version": "3.8", "build_id": "20260922",
                "free_bytes": 312 * 2**30, "installed": self.games, "lepton": True,
                "proton": {"ready": {"display_name": "Proton 11 (ARM64)"}, "openxr": {"name": "SteamVR"}},
                "kernel_keys": {"keys": 31, "max_keys": 200}}

    def installed(self) -> list[dict]:
        return self.games

    def close(self) -> None:
        pass


class FakeKeyboardSession:
    """--fake-frame: Type on Frame connects at once (nothing is sent anywhere)."""

    def __init__(self, frame):
        self.closed = False

    def key(self, name, action="down"):
        return True

    def text(self, text):
        return ""

    def close(self):
        self.closed = True


class FakeMonitorSession:
    """--fake-frame: the Monitor tab's stream as synthetic samples, one per second, shaped like a real Quest game on
    the dev Frame (2026-10-07): 72 fps, the game's Android container, SteamVR/Steam as context. Two minutes are sent
    at once first, so the charts are full in screenshots. GAME = --game (else the library's first game)."""

    GAME: str | None = None
    BACKFILL = 120
    # (name, group, CPU %, GPU %, memory); "GAME" = the game's package
    PROCS = [("GAME", "game", 9.8, 44.0, 1_350_000_000), ("vrcompositor", "steamvr", 1.4, 9.0, 80_000_000),
             ("system_server", "game", 1.1, 0.0, 296_000_000), ("XRServiceLoopTh", "steamvr", 0.5, 0.0, 433_000_000),
             ("steamwebhelper", "steam", 0.4, 0.0, 350_000_000), ("com.android.systemui", "game", 0.1, 0.0,
                                                                   229_000_000),
             ("surfaceflinger", "game", 0.1, 0.4, 96_000_000), ("audioserver", "game", 0.1, 0.0, 31_000_000),
             ("com.android.providers.media.module", "game", 0.1, 0.0, 130_000_000),
             ("com.android.launcher3", "game", 0.0, 0.0, 194_000_000), ("main", "game", 0.0, 0.0, 178_000_000),
             ("com.android.settings", "game", 0.0, 0.0, 172_000_000),
             ("com.android.networkstack.process", "game", 0.0, 0.0, 130_000_000),
             ("android.ext.services", "game", 0.0, 0.0, 111_000_000)] + \
            [("vrwebhelper", "steamvr", 0.3, 0.0, 160_000_000 + i * 9_000_000) for i in range(4)]

    def __init__(self, frame, on_sample, on_end=None):
        import math

        self.math, self.closed, self.on_sample = math, False, on_sample
        self.static = {"agent": 62, "cores": 8, "gpu_max_mhz": 903, "mem_total": 16 * 1024 ** 3, "fan": True,
                       "clusters": [{"cpus": [0, 1], "max_mhz": 2265}, {"cpus": [2, 3, 4], "max_mhz": 3148},
                                    {"cpus": [5, 6], "max_mhz": 2956}, {"cpus": [7], "max_mhz": 3052}],
                       "temp_groups": ["CPU", "GPU", "Memory", "Battery", "NPU", "Power ICs", "Modem", "Camera"]}
        self.t = 0
        threading.Thread(target=self._run, daemon=True).start()

    def _sample(self) -> dict:
        m, t = self.math, self.t
        wave = lambda a, p, o=0: a * (1 + m.sin(t / p + o)) / 2  # noqa: E731
        g = library.game(self.GAME) if self.GAME else (library.games() or [None])[0]
        game = g or {"package": "com.example.game", "title": "Example Game"}
        pkg = game["package"]
        procs = [{"pid": 570799 + i * 37, "ppid": 1, "name": pkg if n == "GAME" else n,
                  "group": f"game:{pkg}" if grp == "game" else grp, "game": pkg if grp == "game" else None,
                  "cpu": round(c * (0.85 + wave(0.3, 3, i)), 1), "gpu": round(gp * (0.9 + wave(0.2, 5, i)), 1),
                  "rss": r, "age": 1500 + t, "uid": 1000, "critical": grp in ("steamvr", "steam"), "locked": False,
                  "context": grp != "game"}
                 for i, (n, grp, c, gp, r) in enumerate(self.PROCS)]
        return {
            "t": time.time() - max(0, self.BACKFILL - t), "dt": 1.0, "self_ms": 11.5,
            "cpu": {"total": round(15 + wave(4, 4), 1), "cores": [round(4 + wave(30, 3 + i, i), 1) for i in range(8)],
                    "mhz": [2265, 1996, 1920, 518]},
            "gpu": {"busy": round(52 + wave(8, 5), 1), "mhz": 903},
            "mem": {"total": 15.3 * 1024 ** 3, "avail": int((8.3 - wave(0.15, 9)) * 1024 ** 3),
                    "swap_total": 7.6 * 1024 ** 3, "swap_free": 7.4 * 1024 ** 3},
            "psi": {"cpu": 1.0, "memory": 0.0, "io": 0.1},
            "temps": {"CPU": round(48 + wave(1.5, 11), 1), "GPU": round(46.5 + wave(1, 13), 1), "Memory": 47.1,
                      "Battery": 27.7, "NPU": 47.3, "Power ICs": 37.0, "Modem": 44.2, "Camera": 46.3},
            "zones": {"CPU": {"cpu7-top": 48.4, "cpu0": 44.0}, "GPU": {"gpuss-0": 46.5}},
            "fan": 8500 + int(wave(300, 7)),
            "power": {"system": round(7.3 + wave(0.6, 4), 2), "cpu": 0.85, "gpu": round(1.0 + wave(0.3, 5), 2),
                      "npu": 0.1},
            "battery": {"percent": 95, "status": "Discharging", "plugged": False, "draining": True, "watts": -1.96,
                        "empty_s": 22440},
            "net": {"wlan0": [int(2000 + wave(4000, 3)), 1000], "wlanap": [0, 0], "usb0": [0, 0]},
            "games": [{"package": pkg, "title": game.get("title") or pkg, "kind": "quest", "appid": 1,
                       "elapsed": 1500 + t, "cpu": 9.4, "gpu": round(44 + wave(6, 5), 1), "mem": 3_390_000_000,
                       "processes": 87, "fps": round(71.6 + wave(0.6, 2) - (9 if t % 41 == 30 else 0), 1),
                       "frame_ms": 0.0}],
            "procs": procs, "filter": "game"}

    def _run(self) -> None:
        for self.t in range(100000):
            if self.closed:
                return
            self.on_sample(self._sample())
            if self.t >= self.BACKFILL:  # the first two minutes at once (full charts), then one per second
                time.sleep(1)

    def set_interval(self, s): pass  # noqa: E704
    def set_filter(self, w): pass  # noqa: E704
    def pause(self, p): pass  # noqa: E704

    def kill(self, pid, sig="TERM", force=False):
        return {"ended": True, "pid": pid}

    def end_game(self, package):
        return {"ended": True, "via": "steam"}

    def close(self):
        self.closed = True


def open_type_tab(app: FramePortApp) -> None:
    """The Type on Frame tab, with one key 'pressed' so the screenshot shows it working."""
    app.type_on_frame()
    time.sleep(1.5)
    app.keyboard_view.press("Enter")


def open_monitor_details(app: FramePortApp) -> None:
    """The Monitor with its details row open."""
    app.go("monitor")
    time.sleep(2)
    if not app.monitor_view.details.visible:
        app.monitor_view.toggle_details()


LINUX_APPIMAGE = "linux.venera"  # --linux's packages
LINUX_FOLDER = "linux.matineevr"


def _elf(machine: int = 183, appimage: bool = False, extra: bytes = b"") -> bytes:
    """A minimal ELF header (183 = aarch64): enough for FramePort's Linux app detection."""
    head = bytearray(64)
    head[:4], head[4], head[5] = b"\x7fELF", 2, 1
    if appimage:
        head[8:11] = b"AI\x02"
    head[18:20] = machine.to_bytes(2, "little")
    return bytes(head) + extra


def add_fake_linux_apps() -> None:
    """--linux: an AppImage and a VR app folder (with a helper program, so "Change program…" shows) in the library."""
    from frameport import pipeline
    from frameport.core.paths import user_data_dir

    base = user_data_dir() / "smoke-linux"
    app = base / "MatineeVR"
    (app / "lib").mkdir(parents=True, exist_ok=True)
    (base / "Venera-2.4.2-aarch64.AppImage").write_bytes(_elf(appimage=True, extra=b"\0" * 4096))
    (app / "MatineeVR").write_bytes(_elf(extra=b"\0" * 8192))
    (app / "crashpad_handler").write_bytes(_elf())
    (app / "lib" / "libopenxr_loader.so.1").write_bytes(_elf(extra=b"xrCreateInstance"))
    pipeline.add_linux_app(base / "Venera-2.4.2-aarch64.AppImage")
    pipeline.add_linux_app(app)


STEP_SECONDS = 4  # per screen (the screenshot is taken ~3 s in)


def driver(app: FramePortApp, steps: list[tuple[str, callable]], ready: threading.Event, done_step: list):
    for name, action in steps:
        try:
            action(app)
        except Exception:  # noqa: BLE001
            ERRORS.append(f"{name}: {traceback.format_exc()}")
        done_step.append(name)
        ready.set()
        time.sleep(STEP_SECONDS)


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
    ap.add_argument("--update", action="store_true", help="pretend a new FramePort release exists (update UI)")
    ap.add_argument("--fake-frame", action="store_true", help="pretend a Steam Frame is connected (no device needed)")
    ap.add_argument("--hover", default=None, help="x,y: move the mouse there before the Library screenshot")
    ap.add_argument("--install-questions", nargs="+", metavar="PKG", default=None,
                    help="open the install questions for these games, then click each dialog's main button twice "
                         "(a double click must not duplicate dialogs); nothing is installed")
    ap.add_argument("--usb-setup", action="store_true", help="click 'Set up with a USB cable' on the connect page "
                    "(a Frame cabled to this PC) and screenshot what follows")
    ap.add_argument("--docs", action="store_true", help="only the screens used in the docs (Library, --game, Frame)")
    ap.add_argument("--gestures", action="store_true", help="with --fake-frame: real mouse drags (drag-select) and "
                    "right-clicks (menus) in Files, Screenshots and the Library; prints what got selected")
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
    global STEP_SECONDS
    if args.hover:
        STEP_SECONDS = 7
    from frameport.ui import theme

    theme.set_scale(args.scale)
    vw, vh = (int(x) for x in args.viewport.split("x"))
    args.out.mkdir(parents=True, exist_ok=True)
    game = args.game or (library.games()[0]["package"] if library.games() else None)
    steps = [("library", lambda a: a.navigate(0)), ("frame", lambda a: a.navigate(1)),
             ("files", lambda a: a.go("files")), ("screenshots", lambda a: a.go("screenshots")),
             ("live", lambda a: a.go("live")), ("keyboard", lambda a: a.go("keyboard")),
             ("monitor", lambda a: a.go("monitor")), ("monitor-details", open_monitor_details),
             ("tools", lambda a: a.go("settings"))]
    if game:
        steps.insert(1, ("game", lambda a: a.open_game(game)))
        steps.insert(2, ("game-customize", lambda a: a.open_game(game, advanced=True)))
        steps += [("share-dialog", lambda a: a.share_config_dialog(game)),
                  ("report-dialog", lambda a: (a.page.pop_dialog(), a.report_problem_dialog(game))),
                  ("custom-art-dialog", lambda a: (a.page.pop_dialog(), a.custom_artwork(game)))]
        # last: the right-click menu stays open over whatever comes next
        steps.append(("library-menu", lambda a: (a.page.pop_dialog(), a.navigate(0), time.sleep(3),
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
    mouse: dict[str, callable] = {}  # step name -> mouse actions (Playwright page) after its screenshot
    if args.gestures:
        STEP_SECONDS = 14  # the mouse actions run after each step's screenshot, before the next step

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
                 "screenshots": drag((680, 280), (950, 280), (950, 430)),     # 3 shots
                 "screenshots-dragged": right_click(950, 430),
                 "screenshots-menu": escape,
                 "library": drag((360, 300), (560, 300), (760, 300)),        # 3 cards, select mode on
                 "library-dragged": drag((560, 300), (760, 300))}             # from a selected card: deselects 2
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
                 ("linux-menu", lambda a: (a.navigate(0), time.sleep(3), a.library_view.open_menu(LINUX_APPIMAGE)))]
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

        def add_menu(a):
            a.navigate(0)
            time.sleep(2)
        steps = [("library", add_menu),
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
        mouse["library"] = add_menu_open  # tall pages: run with e.g. --viewport 1280x3200
    if args.themes:
        from frameport.ui import theme as T

        def switch(name):
            def run(a):
                library.set_setting("ui.theme", name)  # what the picker does
                a.restyle(name)
            return run

        def appearance(a):
            """Settings' Appearance section on its own (it is near the end of the page)."""
            from frameport.ui import components as C
            from frameport.ui.views.settings import SettingsView

            a.go("settings")
            a.body.content = ft.Column([a.top_bar("Settings", "Appearance"),
                                        C.section("Appearance", C.card(SettingsView(a).appearance()))])
            a.page.update()
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
        from frameport import updates

        fake = updates.Update(version="9.9.9", tag="v9.9.9", page="https://example.invalid", asset=None, asset_url=None,
                              sums_url=None, wheel_url=None,
                              notes="## What's new\n- Self-update test release\n- **Bold** and `code` in notes")
        steps += [("update-banner", lambda a: (a.updater._set(fake), a.navigate(0))),
                  ("update-dialog", lambda a: a.updater.show_dialog()),
                  ("update-settings", lambda a: (a.page.pop_dialog(), a.go("settings")))]
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
    ready, done = threading.Event(), []

    if args.fake_frame:  # never reach a real Frame (start-up auto-connect, discovery, the 30 s poll)
        from frameport.frame import keyboard, monitor

        keyboard.KeyboardSession = FakeKeyboardSession
        monitor.MonitorSession = FakeMonitorSession
        FakeMonitorSession.GAME = game  # the game card shows --game (with its artwork)
        FramePortApp.connect = lambda self, *a, **k: None
        FramePortApp.refresh_frame = lambda self, *a, **k: None

    def app_main(page: ft.Page):
        try:
            app = FramePortApp(page)
            if args.fake_frame:
                app.target = FakeTarget()
                app.frame_info, app.frame_state = app.target.describe(), "connected"
        except Exception:  # noqa: BLE001
            ERRORS.append("startup: " + traceback.format_exc())
            return
        threading.Thread(target=driver, args=(app, steps, ready, done), daemon=True).start()

    def shooter():
        from playwright.sync_api import sync_playwright

        time.sleep(6)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
                page = browser.new_page(viewport={"width": vw, "height": vh})
                page.goto(f"http://127.0.0.1:{PORT}", wait_until="networkidle", timeout=120_000)
                shot = 0
                deadline = time.time() + 240 + 8 * len(steps)
                while len(done) < len(steps) and time.time() < deadline:
                    if ready.wait(1):
                        ready.clear()
                        name = done[-1]
                        time.sleep(3)  # let Flutter paint
                        if args.hover and name == "library":
                            hx, hy = (float(v) for v in args.hover.split(","))
                            page.mouse.move(hx, hy)
                            time.sleep(1.5)
                        page.screenshot(path=str(args.out / f"{shot:02d}-{name}.png"))
                        shot += 1
                        if name in mouse:
                            mouse[name](page)
                            time.sleep(2)
                            page.screenshot(path=str(args.out / f"{shot:02d}-{name}-mouse.png"))
                            shot += 1
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
