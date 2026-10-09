"""Serve the real GUI in web mode with a pretend Frame and drive it with headless Chromium (Playwright), for docs
screenshots and the demo tour.

`Server(...).run(script)` owns the main thread (Flet's `ft.run` must) and runs `script(server)` in a thread; the
script opens browser sessions with `server.session(...)`: one Playwright context + page and the FramePortApp that
page started (one app per browser session). Elements are found by name through Flutter's semantics tree (the
accessibility DOM Flutter web builds on request): visible text, a tooltip or a semantics label; the pointer events
still go to Flutter's canvas, so hover effects and gestures behave as with a real mouse.
"""
from __future__ import annotations

import json
import math
import os
import queue
import socket
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

import flet as ft

from frameport.ui.app import FramePortApp
from showcase import fakes

PORT = int(os.environ.get("FRAMEPORT_SHOWCASE_PORT", "8571"))
CHROMIUM_ARGS = ["--use-gl=swiftshader", "--enable-unsafe-swiftshader", "--font-render-hinting=none"]
REPO = Path(__file__).resolve().parents[2]

# Flutter builds its semantics DOM only once something asks for it (a screen reader): the placeholder button does.
# The semantics nodes then sit above the canvas; with pointer-events off they don't take the mouse.
_ENABLE_SEMANTICS = """() => {
  const p = document.querySelector('flt-semantics-placeholder');
  if (p) p.click();
  if (!document.getElementById('fp-sem-style')) {
    const s = document.createElement('style');
    s.id = 'fp-sem-style';
    s.textContent = 'flt-semantics-host, flt-semantics, flt-semantics * { pointer-events: none !important; }';
    document.head.appendChild(s);
  }
  return !!p;
}"""

# every named node: its own text, aria-label or the text of its children, with its box on screen
_NODES = """() => [...document.querySelectorAll('flt-semantics')].map(e => {
  const r = e.getBoundingClientRect();
  const own = [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim();
  return {label: (e.getAttribute('aria-label') || '').trim(), text: own, all: (e.innerText || '').trim(),
          role: e.getAttribute('role') || '', tappable: e.hasAttribute('flt-tappable'),
          x: r.x, y: r.y, w: r.width, h: r.height};
}).filter(n => n.w > 0 && n.h > 0)"""

# the visible pointer for recordings (headless Chromium draws none): an arrow that follows the mouse, a ring on press
CURSOR_JS = """(() => {
  if (window.__fpCursor) return;
  window.__fpCursor = true;
  const add = () => {
    const c = document.createElement('div');
    c.id = 'fp-cursor';
    c.innerHTML = '<svg width="28" height="28" viewBox="0 0 28 28"><path d="M4 2 L4 22 L9.5 17 L13 25 L16.5 23.5 ' +
      'L13 15.8 L20.5 15.8 Z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    Object.assign(c.style, {position: 'fixed', left: '-40px', top: '-40px', width: '28px', height: '28px',
      pointerEvents: 'none', zIndex: 2147483647, filter: 'drop-shadow(0 2px 3px rgba(0,0,0,.45))',
      transform: 'translate(-3px,-2px)'});
    document.documentElement.appendChild(c);
    const ring = (x, y) => {
      const r = document.createElement('div');
      Object.assign(r.style, {position: 'fixed', left: (x - 18) + 'px', top: (y - 18) + 'px', width: '36px',
        height: '36px', borderRadius: '50%', border: '3px solid rgba(255,140,40,.95)', pointerEvents: 'none',
        zIndex: 2147483646, transition: 'transform .45s ease-out, opacity .45s ease-out', transform: 'scale(.4)',
        opacity: '1'});
      document.documentElement.appendChild(r);
      requestAnimationFrame(() => { r.style.transform = 'scale(1.5)'; r.style.opacity = '0'; });
      setTimeout(() => r.remove(), 600);
    };
    const move = e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; };
    window.addEventListener('pointermove', move, true);
    window.addEventListener('mousemove', move, true);
    window.addEventListener('pointerdown', e => ring(e.clientX, e.clientY), true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add();
})();"""


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2


class NotFound(LookupError):
    pass


def names_of(node: dict) -> list[str]:
    """What a semantics node is called: its label (tooltip), its own text and each line of its children's text
    (a card reads "Click to open…\\nQuest\\nBatman: Arkham Shadow\\nWorks")."""
    out = [node.get("label") or "", node.get("text") or ""]
    for key in ("label", "text", "all"):
        out += (node.get(key) or "").splitlines()
    return [s.strip().casefold() for s in out if s and s.strip()]


def _contains(outer: dict, inner: dict) -> bool:
    """outer's box holds inner's centre and is bigger: a container of inner (a list around its row)."""
    cx, cy = inner["x"] + inner["w"] / 2, inner["y"] + inner["h"] / 2
    return (outer["x"] <= cx <= outer["x"] + outer["w"] and outer["y"] <= cy <= outer["y"] + outer["h"]
            and outer["w"] * outer["h"] > inner["w"] * inner["h"] * 1.2)


def innermost(nodes: list[dict]) -> list[dict]:
    """The nodes that don't contain another one of them (a list's text includes its rows': the row wins)."""
    return [n for n in nodes if not any(m is not n and _contains(n, m) for m in nodes)]


def pick(nodes: list[dict], name: str, nth: int = 0, exact: bool | None = None) -> dict | None:
    """The nth node called `name`: exact names first, then names starting with it, then containing it (unless
    exact). Tappable nodes (buttons, cards) win, top to bottom and left to right; else the plain text itself, i.e.
    the smallest node (the containers around it carry its text too)."""
    want = name.strip().casefold()
    tests = [lambda s: s == want]
    if not exact:
        tests += [lambda s: s.startswith(want), lambda s: want in s]
    for test in tests:
        hits = [n for n in nodes if any(test(s) for s in names_of(n))]
        tappable = innermost([n for n in hits if n.get("tappable")])
        if tappable:
            tappable.sort(key=lambda n: (round(n["y"]), round(n["x"]), n["w"] * n["h"]))
            return tappable[nth] if nth < len(tappable) else None
        if hits:
            hits.sort(key=lambda n: (n["w"] * n["h"], round(n["y"]), round(n["x"])))
            return hits[nth] if nth < len(hits) else None
    return None


def ease(t: float) -> float:
    """Ease-in-out (smoothstep of a smoothstep): slow start, fast middle, gentle landing."""
    t = max(0.0, min(1.0, t))
    t = t * t * (3 - 2 * t)
    return t * t * (3 - 2 * t)


def human_path(start: tuple[float, float], end: tuple[float, float], steps: int, bend: float = 0.12
               ) -> list[tuple[float, float]]:
    """Points from start to end along a slight arc (a quadratic Bezier whose control point sits `bend` of the
    distance off the straight line), spaced by `ease`: the path a hand takes, not a ruler line."""
    (x0, y0), (x1, y1) = start, end
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    cx, cy = (x0 + x1) / 2 - dy * bend, (y0 + y1) / 2 + dx * bend  # perpendicular offset
    if dist < 1:
        return [end] * max(1, steps)
    out = []
    for i in range(1, steps + 1):
        t = ease(i / steps)
        out.append(((1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t * t * x1,
                    (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t * t * y1))
    return out


def move_duration(dist: float) -> float:
    """Seconds for a pointer hop of `dist` px: ~0.45 s for short hops, up to ~0.9 s across the window."""
    return max(0.35, min(0.9, 0.3 + dist / 1800))


class Session:
    """One browser page showing one FramePortApp."""

    def __init__(self, server: Server, ctx, page, app: FramePortApp, cursor: bool):
        self.server, self.ctx, self.page, self.app = server, ctx, page, app
        self.mouse_at = (0.0, 0.0)
        self.cursor = cursor
        self.semantics = False
        self.browser = None
        self.park_at: tuple[float, float] | None = None

    def sleep(self, seconds: float) -> None:
        """Wait while the browser keeps going (Playwright's event loop runs, so a screencast keeps its frames)."""
        if seconds > 0:
            self.page.wait_for_timeout(seconds * 1000)

    # ------------------------------------------------------------ finding things
    def enable_semantics(self) -> None:
        if not self.semantics:
            self.semantics = bool(self.page.evaluate(_ENABLE_SEMANTICS))
            self.sleep(0.5)

    def nodes(self) -> list[dict]:
        self.enable_semantics()
        return self.page.evaluate(_NODES)

    def find(self, name: str, within: Box | None = None, nth: int = 0, exact: bool | None = None,
             timeout: float = 8.0) -> Box:
        """The box of the element called `name` (its text, tooltip or label; exact match first, then the start of
        the text, then anywhere in it), optionally inside `within`; the smallest tappable one wins ties. Waits up
        to `timeout` s for it to appear."""
        deadline = time.monotonic() + timeout
        while True:
            vw, vh = self.viewport
            nodes = [n for n in self.nodes() if 0 <= n["x"] + n["w"] / 2 <= vw and 0 <= n["y"] + n["h"] / 2 <= vh]
            if within:
                nodes = [n for n in nodes if within.x <= n["x"] + n["w"] / 2 <= within.x + within.w
                         and within.y <= n["y"] + n["h"] / 2 <= within.y + within.h]
            hit = pick(nodes, name, nth, exact)
            if hit:
                return Box(hit["x"], hit["y"], hit["w"], hit["h"])
            if time.monotonic() > deadline:
                raise NotFound(f"no element named {name!r} on screen")
            self.sleep(0.3)

    def dialog(self, timeout: float = 8.0) -> Box:
        """The box of the topmost open dialog (Flutter labels a dialog's surface "Alert")."""
        deadline = time.monotonic() + timeout
        while True:
            boxes = [n for n in self.nodes() if n["label"] == "Alert"]
            if boxes:
                n = boxes[-1]
                return Box(n["x"], n["y"], n["w"], n["h"])
            if time.monotonic() > deadline:
                raise NotFound("no dialog open")
            self.sleep(0.3)

    def exists(self, name: str, timeout: float = 0.0) -> bool:
        try:
            self.find(name, timeout=timeout)
            return True
        except NotFound:
            return False

    @property
    def viewport(self) -> tuple[int, int]:
        v = self.page.viewport_size
        return v["width"], v["height"]

    # ------------------------------------------------------------ the pointer
    def point(self, target) -> tuple[float, float]:
        """A target: a name (see find), a Box, an (x, y) pair, or {"name": …, "dx": …, "dy": …, "nth": …}
        (dx/dy: offset from the box's centre in px, or as a fraction of its size when |d| < 1)."""
        if isinstance(target, (tuple, list)) and len(target) == 2 and all(isinstance(v, (int, float)) for v in target):
            return float(target[0]), float(target[1])
        if isinstance(target, Box):
            return target.center
        if isinstance(target, str):
            return self.find(target).center
        if isinstance(target, dict):
            box = self.find(target["name"], nth=target.get("nth", 0), exact=target.get("exact"))
            cx, cy = box.center
            dx, dy = target.get("dx", 0), target.get("dy", 0)
            dx = dx * box.w if abs(dx) < 1 else dx
            dy = dy * box.h if abs(dy) < 1 else dy
            return cx + dx, cy + dy
        raise TypeError(f"not a target: {target!r}")

    def move(self, target, duration: float | None = None, smooth: bool = True) -> tuple[float, float]:
        x, y = self.point(target)
        if not smooth:
            self.page.mouse.move(x, y)
        else:
            dist = math.hypot(x - self.mouse_at[0], y - self.mouse_at[1])
            duration = move_duration(dist) if duration is None else duration
            steps = max(2, int(duration * 60))
            t0 = time.monotonic()
            for i, (px, py) in enumerate(human_path(self.mouse_at, (x, y), steps)):
                self.page.mouse.move(px, py)
                wait = t0 + duration * (i + 1) / steps - time.monotonic()
                if wait > 0:
                    self.sleep(wait)
        self.mouse_at = (x, y)
        return x, y

    def click(self, target, button: str = "left", smooth: bool = True, pause: float = 0.15) -> None:
        self.move(target, smooth=smooth)
        if isinstance(target, (str, dict)):  # the layout moved while the pointer travelled (a list reloaded): follow
            x, y = self.point(target)
            if math.hypot(x - self.mouse_at[0], y - self.mouse_at[1]) > 6:
                self.move((x, y), duration=0.25)
        self.sleep(pause)
        self.page.mouse.down(button=button)
        self.sleep(0.07)
        self.page.mouse.up(button=button)

    def drag(self, points: list, smooth: bool = True) -> None:
        self.move(points[0], smooth=smooth)
        self.sleep(0.2)
        self.page.mouse.down()
        for p in points[1:]:
            self.move(p, smooth=smooth, duration=0.6)
            self.sleep(0.2)
        self.page.mouse.up()

    def type(self, text: str, delay: float = 0.11) -> None:
        self.page.keyboard.type(text, delay=int(delay * 1000))

    def press(self, key: str) -> None:
        self.page.keyboard.press(key)

    def scroll(self, dy: float, at=None) -> None:
        if at is not None:
            self.move(at)
        for _ in range(max(1, int(abs(dy) // 100))):
            self.page.mouse.wheel(0, 100 if dy > 0 else -100)
            self.sleep(0.05)

    def park(self, away: bool = False) -> None:
        """The pointer somewhere it hovers nothing: the sidebar's empty part (videos: the drawn cursor stays in the
        picture), or with `away` off the page (still pictures: nothing hovered, whatever the layout)."""
        if away:
            self.page.mouse.move(-40, -40)
            self.mouse_at = (-40.0, -40.0)
            return
        if self.park_at is None:  # just below the sidebar's last tab (Settings): nothing there takes the pointer
            try:
                tab = self.find("Settings", timeout=3)
                self.park_at = (tab.x + tab.w * 0.6, tab.y + tab.h + 44)
            except NotFound:
                self.park_at = self.server.park_point(self.viewport)
        self.page.mouse.move(*self.park_at)
        self.mouse_at = self.park_at

    # ------------------------------------------------------------ pictures
    def settle(self, timeout: float = 8.0, interval: float = 0.15, quiet: int = 4, mask: list[Box] = ()) -> bool:
        """Wait until the page stops changing: `quiet` screenshots in a row (interval apart) are identical (masked
        boxes, e.g. a live chart, ignored). False when it was still changing at `timeout`."""
        deadline, last, same = time.monotonic() + timeout, None, 0
        while time.monotonic() < deadline:
            png = self._raw(mask)
            same = same + 1 if png == last else 0
            if same >= quiet:
                return True
            last = png
            self.sleep(interval)
        return False

    def _raw(self, mask: list[Box] = ()) -> bytes:
        if not mask:
            return self.page.screenshot(animations="disabled", caret="hide")
        from io import BytesIO

        from PIL import Image, ImageDraw

        im = Image.open(BytesIO(self.page.screenshot(caret="hide")))
        d = ImageDraw.Draw(im)
        for b in mask:
            d.rectangle((b.x, b.y, b.x + b.w, b.y + b.h), fill=(0, 0, 0))
        return im.tobytes()

    def shot(self, path: Path, clip: Box | None = None, settle: bool = True) -> Path:
        if settle:
            self.settle()
        path.parent.mkdir(parents=True, exist_ok=True)
        kw = {"clip": {"x": clip.x, "y": clip.y, "width": clip.w, "height": clip.h}} if clip else {}
        self.page.screenshot(path=str(path), caret="hide", **kw)
        return path

    def close(self) -> None:
        """Close the window and leave nothing behind for the next one: the job queue is shared by every session
        (a pretend install would show in the next window's Activity), the live stream's ffmpeg and the monitor
        stream would run on, and a closed page must not get job events."""
        app = self.app
        try:
            app.jobs.unsubscribe(app._on_job)
            for job in list(app.jobs.jobs):
                if job.active:
                    app.jobs.cancel(job)
            deadline = time.monotonic() + 5
            while any(j.active for j in app.jobs.jobs) and time.monotonic() < deadline:
                self.sleep(0.1)
            app.jobs.clear_finished()
            fakes.stop_fake_live(app)
            app.stop_monitor()
        except Exception:  # noqa: BLE001
            pass
        for thing in (self.ctx, self.browser):
            try:
                if thing is not None:
                    thing.close()
            except Exception:  # noqa: BLE001
                pass


class Server:
    """The GUI on 127.0.0.1:PORT (web mode) with fakes installed; `run(script)` serves until the script returns."""

    def __init__(self, port: int = PORT, fake_frame: bool = True, monitor_session=None, game: str | None = None,
                 scale: float = 1.0):
        self.port, self.fake_frame, self.scale = port, fake_frame, scale
        self.monitor_session = monitor_session or fakes.FakeMonitorSession
        self.apps: queue.Queue = queue.Queue()
        self.errors: list[str] = []
        self.playwright = None
        self.game = game

    def park_point(self, viewport: tuple[int, int]) -> tuple[float, float]:
        return 130 * self.scale, viewport[1] * 0.62

    def _app_main(self, page: ft.Page) -> None:
        try:
            app = FramePortApp(page)
            if self.fake_frame:
                fakes.attach_fake_frame(app, self.monitor_session)
        except Exception:  # noqa: BLE001
            self.errors.append("startup: " + traceback.format_exc())
            return
        self.apps.put(app)

    def session(self, viewport: tuple[int, int], cursor: bool = False, video_dir: Path | None = None,
                device_scale: float = 1.0) -> Session:
        """A new browser context + page (fresh app). `video_dir`: Playwright's own recorder (fallback capture)."""
        while not self.apps.empty():  # apps from sessions already closed
            self.apps.get_nowait()
        kw = {}
        if video_dir is not None:
            kw = {"record_video_dir": str(video_dir), "record_video_size": {"width": viewport[0],
                                                                           "height": viewport[1]}}
        # a browser of its own per window: windows sharing one browser (its GPU process) sometimes drew text with
        # glyphs from the wrong place ("/sdcard/ʍvies")
        browser = self.playwright.chromium.launch(args=CHROMIUM_ARGS)
        ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]},
                                  device_scale_factor=device_scale, **kw)
        if cursor:
            ctx.add_init_script(CURSOR_JS)
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{self.port}", wait_until="networkidle", timeout=120_000)
        app = self.apps.get(timeout=120)
        s = Session(self, ctx, page, app, cursor)
        s.browser = browser
        # web fonts: text measured before they load wraps or overlaps ("Q / uest"): a text-heavy first screen (the
        # Settings page) takes them before the steps start
        s.sleep(1.0)
        app.go("settings")
        s.sleep(2.0)
        s.enable_semantics()
        s.park()
        return s

    def run(self, script) -> int:
        """Serve; `script(server) -> exit code` runs in a thread with the browser ready; the process ends with it."""
        from frameport.ui.app import serialize_flet_updates

        serialize_flet_updates()  # as the app's main() does: patches and property writes don't interleave
        if self.fake_frame:
            fakes.install_fakes(self.game, self.monitor_session)

        def runner():
            from playwright.sync_api import sync_playwright

            for _ in range(90):
                try:
                    socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                    break
                except OSError:
                    time.sleep(1)
            time.sleep(1.5)
            code = 1
            try:
                with sync_playwright() as p:
                    self.playwright = p
                    code = script(self) or 0
            except Exception:  # noqa: BLE001
                self.errors.append("script: " + traceback.format_exc())
            for e in self.errors:
                print("ERROR", e, flush=True)
            os._exit(1 if self.errors else code)

        from frameport.ui.app import assets_dir

        threading.Thread(target=runner, daemon=True).start()
        ft.run(self._app_main, view=ft.AppView.WEB_BROWSER, port=self.port, assets_dir=assets_dir())
        return 1


class Screencast:
    """The page's frames as Chrome paints them (CDP Page.startScreencast: JPEGs with timestamps), written to a folder.
    Sharper than Playwright's own recorder (VP8 at a low bit rate). Chrome sends a frame only when the page changed,
    so the frames come with their times and are turned into a constant frame rate later (postprod.frames_to_video).
    Frames only arrive while Playwright runs (Session.sleep, mouse moves), never during time.sleep."""

    def __init__(self, session: Session, folder: Path, quality: int = 92):
        import base64

        self._b64 = base64.b64decode
        self.session, self.folder, self.quality = session, Path(folder), quality
        self.folder.mkdir(parents=True, exist_ok=True)
        self.frames: list[tuple[float, str]] = []  # (wall-clock time, file name)
        self.cdp = session.ctx.new_cdp_session(session.page)
        self.cdp.on("Page.screencastFrame", self._frame)
        self.running = False

    def _frame(self, params: dict) -> None:
        name = f"{len(self.frames):06d}.jpg"
        (self.folder / name).write_bytes(self._b64(params["data"]))
        self.frames.append((float(params["metadata"].get("timestamp") or time.time()), name))
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:  # noqa: BLE001  (stopping)
            pass

    def start(self) -> None:
        vw, vh = self.session.viewport
        scale = self.session.page.evaluate("window.devicePixelRatio") or 1
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": self.quality,
                                               "maxWidth": int(vw * scale), "maxHeight": int(vh * scale),
                                               "everyNthFrame": 1})
        self.running = True

    def stop(self) -> None:
        if self.running:
            self.session.sleep(0.3)
            self.cdp.send("Page.stopScreencast")
            self.running = False

    def write_index(self) -> Path:
        """frames.json: [[time, file], …] for postprod."""
        path = self.folder / "frames.json"
        path.write_text(json.dumps(self.frames))
        return path


def call(session: Session, fn, *args, **kwargs):
    """Run an app action from the script thread, logging (not raising) its errors so one broken step doesn't end a
    whole render."""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        session.server.errors.append(f"{getattr(fn, '__name__', fn)}: " + traceback.format_exc())
        return None
