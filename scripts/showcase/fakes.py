"""Pretend Frame for GUI renders (ui_smoke, docs screenshots, the demo tour): its files, screenshots, games, drives,
monitor stream and Type on Frame, with no network and no device. Shared by scripts/ui_smoke.py and scripts/showcase/.

The demo library (scripts/showcase/demo_home.py) writes `showcase-frame.json` into the data dir: which games the
pretend Frame has installed (and on which drive), the running game and its battery; without it the library's working
Quest builds are "installed" (the 8 largest).
"""
from __future__ import annotations

import json
import os
import random
import threading
import time

from frameport.core import library
from frameport.core.paths import user_data_dir
from frameport.ui.app import FramePortApp

FRAME_STATE = "showcase-frame.json"
SHOTS_AT = time.mktime((2026, 10, 4, 21, 40, 0, 0, 0, -1))  # the newest sample screenshot (local time)


def frame_state() -> dict:
    """The demo library's pretend Frame ({installed: [{package, drive, sha256?}], running, battery}), else {}."""
    try:
        return json.loads((user_data_dir() / FRAME_STATE).read_text())
    except (OSError, ValueError):
        return {}


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
        # fixed dates (the Files tab shows them): a few days before the newest screenshot, a different one each
        for i, (root, dirs, files) in enumerate(sorted(os.walk(self.home), reverse=True)):
            for j, name in enumerate(sorted(dirs + files)):
                t = SHOTS_AT - 86400 * (2 + (i + j) % 5) - 3600 * ((i * 7 + j * 3) % 11)
                os.utime(os.path.join(root, name), (t, t))
        self.shots = self._screenshots()

    def _screenshots(self) -> list[dict]:
        """Sample Steam screenshots for the Screenshots tab, two days, a few games + SteamVR. They are the games' own
        in-game store screenshots (shot_*.jpg from the store details), never cover art: games installed on the
        pretend Frame first, the running one leading (Take screenshot adds one of it); SteamVR's own shots (taken in
        a game FramePort didn't install) come from another game's. Only a library without any store screenshots falls
        back to hero/wide art, else colour gradients."""
        from frameport.artwork import fetch

        folder = os.path.join(self.home, "shots")
        os.makedirs(folder, exist_ok=True)
        state = frame_state()
        on_frame = {i["package"] for i in state.get("installed") or []}
        running = state.get("running")
        real, covers = [], []
        for g in library.games():
            if g.get("kind") == "rift":
                continue
            shots_art = sorted(fetch.artwork_dir(g["package"]).glob("shot_*.jpg"))
            if shots_art:
                real.append((g, shots_art))
            else:
                covers.append((g, [p for p in fetch.files(g["package"]) if p.stem in ("hero", "landscape")]))
        games = real or covers
        games.sort(key=lambda ga: (ga[0]["package"] != running, ga[0]["package"] not in on_frame, -len(ga[1]),
                                   ga[0]["package"]))
        self.owners = [(g["package"], g.get("title") or g["package"], arts) for g, arts in games[:3]]
        rest = [arts for g, arts in games[3:] if g["package"] not in on_frame] or [arts for _g, arts in games[3:4]]
        self.owners.append((None, "SteamVR", rest[0] if rest else []))
        self._used = [0] * len(self.owners)  # the next screenshot of each owner (no picture shown twice)
        # fixed dates (renders don't change from one day to the next): 6 shots on the first day, 4 on the one before
        # (--gestures drags across the first row)
        shots = []
        for i in range(10):
            t = SHOTS_AT - i * 2400 - (86400 if i >= 6 else 0)
            shots.append(self._shot(t, i, i % len(self.owners)))
        return shots

    def _shot(self, t: float, i: int, owner: int) -> dict:
        """One Steam screenshot: the owner's next store screenshot, whole (scaled to 1280x720)."""
        from PIL import Image, ImageOps

        pkg, title, arts = self.owners[owner]
        name = time.strftime("%Y%m%d%H%M%S", time.localtime(t)) + "_1.jpg"
        path = os.path.join(self.home, "shots", name)
        if arts:
            n = self._used[owner]
            self._used[owner] += 1
            with Image.open(arts[n % len(arts)]) as src:
                im = ImageOps.fit(src.convert("RGB"), (1280, 720))
        else:
            im = Image.linear_gradient("L").resize((640, 360)).convert("RGB")
            im = Image.merge("RGB", (im.getchannel(0).point(lambda v, i=i: (v + 40 * i) % 256),
                                     im.getchannel(1).point(lambda v: 255 - v), im.getchannel(2)))
        im.save(path, "JPEG", quality=85)
        return {"path": path, "thumb": None, "time": int(t), "width": 1920, "height": 1080,
                "size": os.path.getsize(path), "account": "1", "appid": "250820", "package": pkg, "title": title}

    def take_screenshot(self) -> dict:
        """Take screenshot: a new shot of the running game (its next store screenshot), newest of all."""
        time.sleep(1.2)  # SteamVR captures, Steam saves
        t = max(s["time"] for s in self.shots) + 600
        shot = self._shot(t, len(self.shots) + 1, 0)
        self.shots.insert(0, shot)
        return {"taken": True, "path": shot["path"], "reason": None, "hmd": "Normal"}

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
        if command == "take_screenshot":  # the Screenshots tab's button: a new shot of the running game
            return self.take_screenshot()
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
        state = frame_state()
        if state.get("installed"):  # the demo library says what's on the Frame
            sd_games = {i["package"] for i in state["installed"] if i.get("drive") == "sd"}
            self.games = [self._record(g, i.get("sha256")) for i in state["installed"]
                          if (g := library.game(i["package"]))]
        else:
            works = [g for g in library.games() if g.get("kind") != "rift" and (g.get("build") or {}).get("sha256")
                     and (g.get("recipe") or {}).get("status") in ("works", "issues")]
            works.sort(key=lambda g: -(g.get("data_bytes") or 0))
            self.games = [self._record(g) for g in works[:count]]
            sd_games = {self.games[1]["package"]} if len(self.games) > 1 else set()
        # --linux: the AppImage is on the Frame, but SteamOS lacks one of its libraries
        self.games += [{"package": g["package"], "kind": "linux", "title": g.get("title"), "apk_size": 2**26,
                        "apk_present": True, "recipe": None, "exe": g.get("exe"),
                        "missing_libraries": ["libwebkit2gtk-4.1.so.0"]}
                       for g in library.games() if g.get("kind") == "linux" and g["package"] == LINUX_APPIMAGE]
        # GitHub #90: most games on internal storage, some on a microSD card
        sd = {"internal": False, "path": "/run/media/steamos/SD Card", "label": "SD Card"}
        for g in self.games:
            g["drive"] = sd if g["package"] in sd_games else \
                {"internal": True, "path": "/home/steamos", "label": "Internal storage"}
            g["drive_missing"] = False

    @staticmethod
    def _record(g: dict, sha256: str | None = None) -> dict:
        """The agent's list_installed record of a library game (sha256: a different build = "Update on Frame")."""
        rift = g.get("kind") == "rift"
        return {"package": g["package"], "kind": "rift" if rift else "quest", "title": g.get("title"),
                "apk_size": (g.get("data_bytes") or 0) + (0 if rift else 2**28),
                "sha256": sha256 or (g.get("build") or {}).get("sha256"),
                "recipe": {"patches": list((g.get("recipe") or {}).get("patches", []))}}

    def launch(self, pkg: str) -> dict:
        """Play: Steam on the pretend Frame starts the game (the "Starting … put the headset on" toast)."""
        return {"via": "shortcut", "steam": {"result": "started", "lines": []}}

    def drives(self) -> list[dict]:
        sd_games = sum(1 for g in self.games if not g["drive"]["internal"])
        return [{"id": "internal", "path": "/home/steamos", "install_dir": "/home/steamos/Applications/quest-frame",
                 "label": "Internal storage", "fstype": "ext4", "internal": True, "removable": False,
                 "free_bytes": 312 * 2**30, "total_bytes": 460 * 2**30, "usable": True, "reason": "",
                 "games": len(self.games) - sd_games},
                {"id": "/run/media/steamos/SD Card", "path": "/run/media/steamos/SD Card",
                 "install_dir": "/run/media/steamos/SD Card/FramePort", "label": "SD Card", "fstype": "ext4",
                 "internal": False, "removable": True, "free_bytes": 187 * 2**30, "total_bytes": 238 * 2**30,
                 "usable": True, "reason": "", "games": sd_games},
                {"id": "/run/media/steamos/USB", "path": "/run/media/steamos/USB", "install_dir":
                 "/run/media/steamos/USB/FramePort", "label": "USB", "fstype": "exfat", "internal": False,
                 "removable": True, "free_bytes": 50 * 2**30, "total_bytes": 64 * 2**30, "usable": False,
                 "reason": "exfat can't hold game data (no Unix permissions or symlinks); format the drive in "
                           "SteamOS to install games on it", "games": 0}]

    def move(self, package, dest, reporter) -> dict:
        reporter.stage("Move game files")
        for i in range(5):
            reporter.progress(i / 4, f"Copying {i / 2:.1f}/2.0 GiB")
            time.sleep(0.5)
        return {"state": "done", "package": package}

    def set_desktop_entry(self, package, enabled) -> dict:
        for g in self.games:
            if g["package"] == package:
                g["desktop_entry"] = enabled
        return {"enabled": enabled}

    def describe(self) -> dict:
        return {"hostname": "steamframe", "os": "SteamOS", "os_version": "3.8", "build_id": "20260922",
                "free_bytes": 312 * 2**30, "installed": self.games, "lepton": True,
                "proton": {"ready": {"display_name": "Proton 11 (ARM64)"}, "openxr": {"name": "SteamVR"}},
                "kernel_keys": {"keys": 31, "max_keys": 200},
                "battery": {"percent": frame_state().get("battery", 95), "status": "Discharging"}}

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
    DIP: tuple[float, float] | None = None  # (until, fps): steps hook fps_dip
    PERFECT = 0.0  # until (time.time()): right on 72 fps (steps hook perfect_pacing)
    BATTERY: dict | None = None  # the battery from now on (steps hook battery)
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
        state = frame_state()
        idle = not self.GAME and "running" in state and state["running"] is None  # (a fresh Frame: no game)
        running = self.GAME or state.get("running")
        g = library.game(running) if running else (library.games() or [None])[0]
        game = g or {"package": "com.example.game", "title": "Example Game"}
        pkg = game["package"]
        procs = [{"pid": 570799 + i * 37, "ppid": 1, "name": pkg if n == "GAME" else n,
                  "group": f"game:{pkg}" if grp == "game" else grp, "game": pkg if grp == "game" else None,
                  "cpu": round(c * (0.85 + wave(0.3, 3, i)), 1), "gpu": round(gp * (0.9 + wave(0.2, 5, i)), 1),
                  "rss": r, "age": 1500 + t, "uid": 1000, "critical": grp in ("steamvr", "steam"), "locked": False,
                  "context": grp != "game"}
                 for i, (n, grp, c, gp, r) in enumerate(self.PROCS) if not (idle and grp == "game")]
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
            "battery": {"percent": frame_state().get("battery", 95), "status": "Discharging", "plugged": False,
                        "draining": True, "watts": -1.96, "empty_s": 22440},
            "net": {"wlan0": [int(2000 + wave(4000, 3)), 1000], "wlanap": [0, 0], "usb0": [0, 0]},
            "games": [{"package": pkg, "title": game.get("title") or pkg, "kind": "quest", "appid": 1,
                       "elapsed": 1500 + t, "cpu": 9.4, "gpu": round(44 + wave(6, 5), 1), "mem": 3_390_000_000,
                       "processes": 87, "fps": round(71.6 + wave(0.6, 2) - (9 if t % 41 == 30 else 0), 1),
                       "frame_ms": 0.0}] if not idle else [],
            "procs": procs, "filter": "game"}

    def _run(self) -> None:
        self.t = 0
        while not self.closed and self.t < 100000:
            self.on_sample(self._sample())
            if self.t >= self.BACKFILL:  # the first two minutes at once (full charts), then one per second
                time.sleep(1)
            self.t += 1

    def set_modules(self, m):
        # the hub asks for everything when the Monitor tab subscribes (the Frame card started the stream): send the
        # two minutes again so its charts are full in screenshots too
        if m == "all":
            self.t = 0
        return True

    def set_interval(self, s): pass  # noqa: E704
    def set_filter(self, w): pass  # noqa: E704
    def pause(self, p): pass  # noqa: E704

    def kill(self, pid, sig="TERM", force=False):
        return {"ended": True, "pid": pid}

    def end_game(self, package):
        return {"ended": True, "via": "steam"}

    def close(self):
        self.closed = True


FAKE_INSTALL_STAGES = ["Patching the game", "OVRPort (primary)", "Frame fixes (primary)", "sign (primary)",
                       "validate (primary)", "Prepare Frame", "Upload APK"]


def start_fake_install(app: FramePortApp, package: str) -> None:
    """A running install job of `package` that reports the real stage names up to the upload (62 %) and then waits
    (it never ends: the smoke run exits with it)."""
    from frameport.ui.jobs import Job

    hold, reached = threading.Event(), threading.Event()

    def run(job):
        for stage in FAKE_INSTALL_STAGES:
            job.reporter.stage(stage)
        time.sleep(0.3)  # past the job queue's notification throttle
        job.reporter.progress(0.62, "com.example.apk", speed="36.4 MB/s · ~1 min left")
        reached.set()
        hold.wait()

    app.jobs.submit(Job(f"Install {app._title(package)} on Frame", run, package, "install"))
    reached.wait(10)


def open_type_tab(app: FramePortApp) -> None:
    """The Type on Frame tab, with one key 'pressed' so the screenshot shows it working."""
    app.type_on_frame()
    time.sleep(1.5)
    app.keyboard_view.press("Enter")


def fake_live_stream(app: FramePortApp) -> None:
    """The Live view while streaming: a local ffmpeg test picture through the real relay (no Frame needed). Web mode
    has no in-window player, so it's shown as playing in the browser (the bar's streaming layout)."""
    import shutil
    import subprocess

    from frameport.install.livestream import LiveStream

    view = app.live_view
    if view is None or shutil.which("ffmpeg") is None:
        return
    src = ["ffmpeg", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-c:v", "libx264", "-preset", "ultrafast",
           "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mp4",
           "-movflags", "empty_moov+default_base_moof+frag_keyframe", "-frag_duration", "100000", "-"]
    proc = subprocess.Popen(src, stdout=subprocess.PIPE)
    view.live = LiveStream(lambda: proc.stdout, proc.terminate).start()
    view.mode = "browser"
    view._refresh()


def stop_fake_live(app: FramePortApp) -> None:
    if app.live_view is not None and app.live_view.live is not None:
        app.live_view.stop()


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

    base = user_data_dir() / "smoke-linux"
    app = base / "MatineeVR"
    (app / "lib").mkdir(parents=True, exist_ok=True)
    (base / "Venera-2.4.2-aarch64.AppImage").write_bytes(_elf(appimage=True, extra=b"\0" * 4096))
    (app / "MatineeVR").write_bytes(_elf(extra=b"\0" * 8192))
    (app / "crashpad_handler").write_bytes(_elf())
    (app / "lib" / "libopenxr_loader.so.1").write_bytes(_elf(extra=b"xrCreateInstance"))
    pipeline.add_linux_app(base / "Venera-2.4.2-aarch64.AppImage")
    pipeline.add_linux_app(app)


def install_fakes(game: str | None, monitor_session=None) -> None:
    """--fake-frame: never reach a real Frame (start-up auto-connect, discovery, the 30 s poll); Type on Frame and
    the Monitor stream are fakes. `game` = the Monitor's game card (with its artwork)."""
    from frameport.frame import keyboard, monitor

    keyboard.KeyboardSession = FakeKeyboardSession
    monitor.MonitorSession = monitor_session or FakeMonitorSession
    FakeMonitorSession.GAME = game
    FramePortApp.connect = lambda self, *a, **k: None
    FramePortApp.refresh_frame = lambda self, *a, **k: None
    install_first_run_fakes()


# ------------------------------------------------------------------ first run (the install tutorial)
# The setup command shows placeholders for the address and code (each PC has its own; never this PC's real address).
PAIR_HOST, PAIR_PORT, PAIR_CODE = "<your-PC-address>", 8765, "<one-time-code>"
TOOLS = {"ready": True}  # False: the managed tools look missing until the (pretend) download finishes


class FakePairingServer:
    """The Frame page's setup command without a server: nothing listens and no firewall rule is touched. The
    `pairing_done` step hook plays the Frame running the command (on_paired → the pretend Frame connects)."""

    def __init__(self, on_paired=None, host: str = "", on_ask=None, **_kw):
        # host stays as given ("" = over the network; the Frame page words a set host as the USB-cable setup)
        self.on_paired, self.host, self.on_ask = on_paired, host, on_ask
        self.port, self.code = PAIR_PORT, PAIR_CODE
        self.requests, self.failures, self.paired, self.hint = 1, 0, [], ""  # (requests: no firewall hint)
        self.asks: list = []  # the setup URL: the `frame_asks` step hook plays a Frame asking (Allow / Deny)
        self.running = False

    def decide(self, ask_id: str, allow: bool) -> None:
        for a in self.asks:
            if a.id == ask_id and a.state == "open":
                a.state = "allowed" if allow else "denied"
        # (the Frame then runs the setup: the storyboard's `pairing_done` hook connects the pretend Frame)

    @property
    def url(self) -> str:
        return f"http://{self.host or PAIR_HOST}:{self.port}"

    @property
    def one_liner(self) -> str:
        return f"curl -fsS {self.host or PAIR_HOST}:{self.port}/{self.code} | bash"

    def start(self):
        self.running = True
        return self

    def stop_soon(self) -> None:
        self.running = False

    def stop(self) -> None:
        self.running = False


def install_first_run_fakes() -> None:
    """Fakes for a first start: no Frame discovery on the real network (a recording must not show a real device),
    the pretend setup command, and the managed tools' state (TOOLS)."""
    from frameport.frame import discovery, pairing
    from frameport.tools import toolchain

    discovery.browse = lambda *a, **k: []
    pairing.PairingServer = FakePairingServer
    pairing.ensure_reachable = lambda server: "ok"
    if not getattr(toolchain.status, "_showcase", False):
        real_status = toolchain.status

        def status(*a, **k):
            out = real_status(*a, **k)
            if not TOOLS["ready"]:
                for s in out:
                    s.installed, s.version = False, None
            return out
        status._showcase = True
        toolchain.status = status


def tools_job(app, pace: float = 1.0, delay: float = 0.0):
    """The welcome screen's "Getting FramePort ready": a pretend download of Java, OVRPort and apksigner through the
    real job queue (app.update_tools while TOOLS["ready"] is False). `delay`: seconds at 0 % first."""
    from frameport.ui.jobs import Job

    def run(job):
        rep = job.reporter
        rep.stage("Downloading tools")
        time.sleep(delay)
        parts = [("Java runtime (Temurin 21)", 48.0), ("OVRPort CLI", 21.0), ("apksigner", 1.6)]
        total = sum(mb for _n, mb in parts)
        done = 0.0
        for name, mb in parts:
            rep.stage(f"Downloading {name}")
            steps = max(3, int(mb / 4))
            for i in range(1, steps + 1):
                got = done + mb * i / steps
                rep.progress(got / total, f"{name}: {mb * i / steps:.1f} of {mb:.1f} MB")
                time.sleep(0.12 * pace)
            done += mb
        TOOLS["ready"] = True
        return "FramePort is ready"
    job = app.jobs.submit(Job("Download tools", run, None, "tools"))
    return job


def scan_job(app, folder: str, entries: list[dict], pace: float = 1.0):
    """Scan a folder: the demo games appear one by one through the real job queue (the folder picker is a native
    dialog a script can't drive; `entries` = the demo library saved aside by demo_home's "fresh" profile)."""
    from frameport.artwork import thumbs

    def run(job):
        rep = job.reporter
        rep.stage("Looking for games")
        for i, e in enumerate(entries, 1):
            rep.progress(i / len(entries), e.get("title") or e["package"])
            library.upsert_game(e["package"], **{k: v for k, v in e.items() if k != "package"})
            try:
                thumbs.prewarm(e["package"])
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.12 * pace)
        library.set_setting("scan.roots", [folder])
        if app.route[0] == "welcome":
            library.set_setting("ui.welcome_done", True)
        return f"{len(entries)} games found in {folder}"
    return app.submit(f"Scan {folder.rstrip('/').rsplit('/', 1)[-1]}", run, None, "scan")


def attach_fake_frame(app: FramePortApp, monitor_session=None) -> None:
    """Connect `app` to a FakeTarget (the shared monitor stream, used by the Monitor tab and the Frame card, too)."""
    app.monitor_hub.session_factory = monitor_session or FakeMonitorSession
    app.target = FakeTarget()
    app.frame_info, app.frame_state = app.target.describe(), "connected"


class FrozenMonitorSession(FakeMonitorSession):
    """The fake stream for still pictures: the two minutes of history, then nothing new (every render of a page shows
    the same numbers, whenever the screenshot is taken)."""

    def _run(self) -> None:
        self.t = 0
        while not self.closed:
            if self.t <= self.BACKFILL:
                self.on_sample(self._sample())
                self.t += 1
            else:
                time.sleep(0.2)


class LiveMonitorSession(FakeMonitorSession):
    """The fake stream, livelier for videos: a sample every 0.6 s and a steady frame rate with a short, shallow dip
    now and then (also in the two minutes sent first), so the card's sparkline (5 s points) has a shape and visibly
    moves without the game looking like it struggles. Seeded: every recording gets the same curve."""

    def _sample(self) -> dict:
        s = super()._sample()
        t = self.t
        rnd = random.Random(t)
        fps = 72 - abs(rnd.gauss(0, 0.6))
        if t % 45 in (20, 21, 22):  # a short, shallow dip now and then
            fps = 66 + 2 * abs(t % 45 - 21) + rnd.uniform(-0.8, 0.8)
        if self.DIP and time.time() < self.DIP[0]:  # (the step hook fps_dip: a stutter on cue)
            fps = self.DIP[1] + rnd.uniform(-0.8, 0.8)
        if time.time() < self.PERFECT:  # (the step hook perfect_pacing: right on target)
            fps = 72 - rnd.uniform(0, 0.2)
        for g in s["games"]:
            g["fps"] = round(fps, 1)
        if self.BATTERY:
            s["battery"] = dict(self.BATTERY)
        return s

    def _run(self) -> None:
        self.t = 0
        while not self.closed and self.t < 100000:
            self.on_sample(self._sample())
            if self.t >= self.BACKFILL:
                time.sleep(0.6)
            self.t += 1

    def set_modules(self, m):
        """Opening the Monitor tab adds modules: on film the stream just goes on, as the real one does (the tab's
        charts start there and grow live). Replaying the two minutes, as the stills' stream does, showed the charts
        racing to catch up."""
        return True


def install_job(package: str, pace: float = 1.0):
    """A pretend install of `package` for the real JobManager (app.submit): every stage the real one reports, an
    upload ticking at a plausible speed, the launch test passing. pace < 1 = faster."""
    def run(job):
        rep = job.reporter
        g = library.game(package) or {}
        title = g.get("title") or package
        rnd = random.Random(7)

        def stage(name, secs, logs=()):
            rep.stage(name)
            for line in logs:
                rep.log(line)
            time.sleep(secs * pace)
        if g.get("kind") == "rift":
            stage("Prepare the game", 1.0)
        else:
            stage("Patching the game", 0.9)
            stage("OVRPort (primary)", 1.2, ["overport patch --version=latest"])
            stage("Frame fixes (primary)", 1.0, list((g.get("recipe") or {}).get("patches") or [])[-3:])
            stage("sign (primary)", 0.7)
            rep.stage("validate (primary)")
            for name in ("APK signature", "64-bit libraries", "OpenXR loader"):
                rep.check(name, True)
                time.sleep(0.25 * pace)
        stage("Prepare Frame", 0.7)
        total = max(g.get("data_bytes") or 0, 1.4e9)
        first, steps = 0.42, 14
        for name, file, lo, hi in (("Upload APK", f"{package}.apk", 0.0, first),
                                   ("Upload data (3 files)", f"main.1.{package}.obb", first, 1.0)):
            rep.stage(name)
            for i in range(1, steps + 1):
                f = lo + (hi - lo) * i / steps
                speed = 34.0 + rnd.uniform(0, 4)
                left = (1 - f) * total / (speed * 1e6)
                rep.progress(f, file, speed=f"{speed:.1f} MB/s · ~{left:.0f} s left" if f < 1 else f"{speed:.1f} MB/s")
                time.sleep(0.22 * pace)
        stage("Artwork for the Steam library", 0.8)
        stage("Finalize install", 0.9)
        stage("Add to Steam library", 0.8)
        stage("Launch test (headless)", 1.8, ["process alive", "OpenXR session created", "frames paced: 72 fps"])
        job.summary = {"verdict": "pass", "findings": []}
        rep.stage("Launch test: passed")
        time.sleep(0.5 * pace)
        return f"{title} is ready on your Frame: put the headset on and launch it from your Steam library"
    return run

