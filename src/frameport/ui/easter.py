"""Small surprises that give FramePort some character. None of them is announced anywhere in the UI, and none
gets in the way:

- Click the sidebar logo seven times quickly: its portal lights up and tosses a random game's cover out, which
  tumbles down across the window on its own random arc.
- Rest the pointer on the Monitor's frame rate: it says how the game is doing ("Smooth as butter"); a full minute
  right on target turns its chart into the portal's blue-to-orange for a moment ("Perfect pacing").
- Install milestones (the 1st, 10th and 100th game on the Frame): confetti in the portal's two colours and a
  thank-you.
- Holidays: the logo wears a little badge (a heart, a clover, a pumpkin, …) that does a small dance when the
  pointer touches it; on April 1st the logo stands on its head until you look at it.
- Searching the Library for "frameport": the logo spins round and says it's there.
- Typing "hello" on the Type on Frame tab (the keys still go to the Frame): a little headset waves back.
- The Frame reaching 100 % on the charger: its battery ring sparkles once.
- Long uploads (over five minutes): the cover riding the progress bar does a hop now and then.

Off with library setting `ui.easter_eggs` = False (the docs screenshots and videos turn them off; there's no
switch in Settings). Animations respect Reduce motion (the words stay, the motion goes). `FRAMEPORT_TODAY`
(YYYY-MM-DD) pretends another date (videos, tests). Pure helpers are tested in tests/test_easter.py.
"""
from __future__ import annotations

import datetime as _dt
import math
import os
import random
import threading
import time
from typing import TYPE_CHECKING

import flet as ft

from ..core import library
from ..i18n import tr
from . import components as C
from . import theme as T

if TYPE_CHECKING:
    from .app import FramePortApp

SETTING = "ui.easter_eggs"
INSTALLS = "fun.installed"  # the ids of the different games ever installed on a Frame (the milestones count them)
SHOWN = "fun.milestones"  # the milestones already celebrated (each one shows once, ever)
CLICKS, CLICK_WINDOW = 7, 3.0  # the logo: this many clicks within this many seconds
MILESTONES = (1, 10, 100)
PERFECT_SECONDS = 60.0  # the Monitor: this long right on target = perfect pacing
PERFECT_TOLERANCE = 0.5  # fps
PERFECT_SHOW = 5.0  # seconds the chart stays in the portal's colours
HOP_AFTER = 300.0  # an upload running longer than this: the transit's cover hops ...
HOP_EVERY = 9.0  # ... every this many seconds
HELLO = "hello"


def milestone_text(n: int) -> str | None:
    """(Literal tr() calls, so the strings reach the translation template.)"""
    return {1: tr("Your first game is on the Frame. Welcome aboard!"),
            10: tr("10 games on the Frame. It's getting cosy in there."),
            100: tr("100 games on the Frame. The Frame thanks you.")}.get(n)


def enabled() -> bool:
    return library.setting(SETTING, True) is not False


def today() -> _dt.date:
    """The date (FRAMEPORT_TODAY=YYYY-MM-DD pretends another one)."""
    value = os.environ.get("FRAMEPORT_TODAY", "")
    try:
        return _dt.date.fromisoformat(value) if value else _dt.date.today()
    except ValueError:
        return _dt.date.today()


# ------------------------------------------------------------------ holidays

def easter_sunday(year: int) -> _dt.date:
    """Western Easter Sunday (the anonymous Gregorian computus)."""
    a, (b, c) = year % 19, divmod(year, 100)
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return _dt.date(year, month, day + 1)


def holiday(day: _dt.date) -> str | None:
    """Which holiday `day` belongs to, or None: "valentine" (Feb 13-14), "pi" (Mar 14), "clover" (Mar 17), "easter"
    (Easter Saturday to Monday), "april" (Apr 1), "halloween" (Oct 24-31), "newyear" (Dec 31 - Jan 1), "winter"
    (Dec 20 - Jan 2, around the new year)."""
    md = (day.month, day.day)
    if md in ((12, 31), (1, 1)):
        return "newyear"
    if (day.month == 12 and day.day >= 20) or md == (1, 2):
        return "winter"
    if day.month == 10 and day.day >= 24:
        return "halloween"
    if abs((day - easter_sunday(day.year)).days) <= 1:
        return "easter"
    return {(2, 13): "valentine", (2, 14): "valentine", (3, 14): "pi", (3, 17): "clover",
            (4, 1): "april"}.get(md)


# the badge each holiday puts on the logo (April 1st has none: the logo stands on its head instead)
HOLIDAY_ICON = {"valentine": "holiday-heart", "pi": "holiday-pi", "clover": "holiday-clover",
                "easter": "holiday-egg", "halloween": "holiday-pumpkin", "winter": "holiday-snowflake",
                "newyear": "holiday-popper"}
# what the badge does when the pointer touches it (keyframes in hover_frames)
HOLIDAY_MOTION = {"valentine": "beat", "pi": "roll", "clover": "spin", "easter": "hop", "halloween": "wobble",
                  "winter": "spin", "newyear": "pop"}


def holiday_tip(which: str) -> str:
    return {"valentine": tr("Happy Valentine's Day!"), "pi": tr("Happy Pi Day! 3.14159…"),
            "clover": tr("Happy St. Patrick's Day!"), "easter": tr("Happy Easter!"),
            "april": tr("Nothing to see here."), "halloween": tr("Happy Halloween!"),
            "newyear": tr("Happy New Year!")}.get(which, tr("Happy holidays!"))


def hover_frames(motion: str) -> list[tuple[dict, float]]:
    """A badge's little dance: (properties, seconds to hold them) steps, ending where it started. Rotations are
    relative turns (radians) the caller adds up, so a spin always goes forwards."""
    tau = 2 * math.pi
    return {
        "beat": [({"scale": 1.35}, 0.14), ({"scale": 1.0}, 0.14), ({"scale": 1.35}, 0.14), ({"scale": 1.0}, 0.2)],
        "spin": [({"turn": tau}, 0.9)],
        "roll": [({"turn": tau, "offset": (0.35, 0)}, 0.45), ({"offset": (0, 0)}, 0.45)],
        "hop": [({"offset": (0, -0.45), "turn": -0.25}, 0.18), ({"offset": (0, 0), "turn": 0.5}, 0.18),
                ({"offset": (0, -0.25), "turn": -0.25}, 0.16), ({"offset": (0, 0)}, 0.2)],
        "wobble": [({"turn": 0.35}, 0.12), ({"turn": -0.6}, 0.12), ({"turn": 0.45}, 0.12), ({"turn": -0.3}, 0.12),
                   ({"turn": 0.1}, 0.16)],
        "pop": [({"scale": 1.6, "turn": -0.35}, 0.16), ({"scale": 0.9, "turn": 0.5}, 0.16),
                ({"scale": 1.0, "turn": -0.15}, 0.2)],
    }.get(motion, [])


# ------------------------------------------------------------------ the Monitor

def fps_mood(fps: float | None, target: float | None) -> str | None:
    """What the Monitor's frame rate says about the game, or None (no reading)."""
    if not fps or not target:
        return None
    ratio = fps / target
    if ratio >= 0.97:
        return tr("Smooth as butter")
    if ratio >= 0.88:
        return tr("Pretty smooth")
    if ratio >= 0.7:
        return tr("It's doing its best")
    return tr("Hang in there, little Frame")


class PerfectPacing:
    """Watches the frame rate: True from feed() once it has stayed within PERFECT_TOLERANCE of the target for
    PERFECT_SECONDS (by the samples' own times). Once per streak: it has to slip before it can happen again."""

    def __init__(self):
        self.since: float | None = None
        self.done = False

    def feed(self, fps: float | None, target: float | None, t: float) -> bool:
        if not fps or not target or abs(fps - target) > PERFECT_TOLERANCE:
            self.since, self.done = None, False
            return False
        if self.since is None:
            self.since = t
        if not self.done and t - self.since >= PERFECT_SECONDS:
            self.done = True
            return True
        return False


# ------------------------------------------------------------------ installs

def record_install(package: str | None) -> str | None:
    """A game was installed on a Frame. Returns a milestone's message the one time the number of *different* games
    ever installed reaches 1, 10 or 100, else None. Each milestone shows once, ever:

    - what counts is the set of game ids (INSTALLS): updating, rebuilding or reinstalling a game adds nothing, nor
      does an app update;
    - a milestone is stored as shown (SHOWN) in the same locked write that finds it, before it's shown: a crash or a
      second window can't show it again;
    - the first time this runs, the set starts from the library's games already installed on a Frame, and the
      milestones they passed count as shown (someone with 20 games doesn't get "your first game" after updating
      FramePort)."""
    if not package:
        return None
    with library.edit() as data:
        s = data.setdefault("settings", {})
        if not isinstance(s.get(INSTALLS), list):  # first use: what's on a Frame already, celebrated or not
            before = sorted(pkg for pkg, g in (data.get("games") or {}).items()
                            if pkg != package and ((g.get("installs") or {}).get("frame")))
            s[INSTALLS] = before
            s[SHOWN] = [m for m in MILESTONES if m <= len(before)]
        games = set(s[INSTALLS])
        if package in games:
            return None
        games.add(package)
        s[INSTALLS] = sorted(games)
        shown = set(s.get(SHOWN) or [])
        due = len(games) if len(games) in MILESTONES and len(games) not in shown else None
        if due:
            s[SHOWN] = sorted(shown | {due})
    return milestone_text(due) if due else None


# ------------------------------------------------------------------ small pure helpers

class ClickCounter:
    """Counts quick clicks: True on the CLICKS-th within CLICK_WINDOW seconds (then starts over)."""

    def __init__(self, clicks: int = CLICKS, window: float = CLICK_WINDOW, clock=time.monotonic):
        self.clicks, self.window, self.clock = clicks, window, clock
        self.times: list[float] = []

    def click(self) -> bool:
        now = self.clock()
        self.times = [t for t in self.times if now - t <= self.window] + [now]
        if len(self.times) >= self.clicks:
            self.times = []
            return True
        return False


def is_self_search(query: str | None) -> bool:
    """The Library's search asks for FramePort itself."""
    return (query or "").strip().lower().replace(" ", "") == "frameport"


class Typed:
    """The last few characters typed on the Type on Frame tab (key names as Flet reports them): True from key()
    when they just spelled HELLO. Only watches; the keys go to the Frame as always."""

    def __init__(self):
        self.text = ""

    def key(self, name: str) -> bool:
        if name in ("Backspace",):
            self.text = self.text[:-1]
        elif name == "Space" or name == " ":
            self.text += " "
        elif len(name) == 1:
            self.text += name.lower()
        else:
            return False  # Shift, arrows, …: nothing typed
        self.text = self.text[-16:]
        return self.text.endswith(HELLO)


def says_hello(text: str | None) -> bool:
    return HELLO in (text or "").lower()


def toss_path(x0: float, y0: float, width: float, height: float, rnd: random.Random | None = None,
              dt: float = 0.06) -> list[tuple[float, float, float]]:
    """A cover tossed out of the portal at (x0, y0): a random throw (always into the window: the logo sits in its
    top-left corner) under gravity until it has left the window at the bottom (or a few seconds). (x, y, turn)
    points dt apart; turn = the cover's rotation in radians."""
    rnd = rnd or random.Random()
    vx = rnd.uniform(0.14, 0.42) * width  # px/s: the right half of the window, or less
    vy = -rnd.uniform(0.3, 0.7) * height  # px/s, upwards first
    gravity = height * rnd.uniform(1.0, 1.4)
    spin = rnd.choice((-1, 1)) * rnd.uniform(2.0, 5.0)  # rad/s
    out, t = [], 0.0
    while t < 4.0:
        t += dt
        x, y = x0 + vx * t, y0 + vy * t + gravity * t * t / 2
        out.append((x, y, spin * t))
        if y > height + 80 or x > width + 80:
            break
    return out


# ------------------------------------------------------------------ the sidebar logo

def _logo_origin(size: float) -> tuple[float, float]:
    """The logo's top-left corner in the window (app._build_shell: the sidebar's padding, then the logo row's)."""
    return T.S4 + T.S1, T.S4 + T.S2


class Logo:
    """The sidebar's logo with its surprises: a holiday badge that dances when touched, the April 1st headstand,
    the portal toss after seven quick clicks and the spin when you search for FramePort. `.control` goes into the
    sidebar."""

    def __init__(self, app: FramePortApp, size: float):
        self.app, self.size = app, size
        self.counter = ClickCounter()
        self.busy = False
        self._turn = 0.0  # the badge's rotation so far (spins add up)
        self._dancing = False
        s = size
        self.image = C.logo(s)
        self.image.rotate = 0
        self.image.animate_rotation = ft.Animation(700, ft.AnimationCurve.EASE_IN_OUT)
        self.image.animate_scale = ft.Animation(220, ft.AnimationCurve.EASE_OUT)
        layers: list[ft.Control] = [self.image]
        self.which = holiday(today()) if enabled() else None
        self.badge = None
        if self.which in HOLIDAY_ICON:
            b = s * 0.58
            pic = ft.Image(src=C._asset_src(HOLIDAY_ICON[self.which]), width=b, height=b, fit=ft.BoxFit.CONTAIN)
            self.badge = ft.Container(pic, width=b, height=b, rotate=0, scale=1, offset=ft.Offset(0, 0),
                                      animate_rotation=ft.Animation(260, ft.AnimationCurve.EASE_IN_OUT),
                                      animate_scale=ft.Animation(140, ft.AnimationCurve.EASE_OUT),
                                      animate_offset=ft.Animation(180, ft.AnimationCurve.EASE_OUT))
            layers.append(ft.Container(self.badge, left=s - b * 0.7, top=s - b * 0.75,
                                       tooltip=holiday_tip(self.which), on_hover=self._badge_hover))
        if self.which == "april":  # upside down all day; looking at it puts it right for a moment
            self.image.rotate = math.pi
        self.control = ft.Container(ft.Stack(layers, width=s, height=s, clip_behavior=ft.ClipBehavior.NONE),
                                    width=s, height=s, on_click=self._click,
                                    tooltip=holiday_tip("april") if self.which == "april" else None,
                                    on_hover=self._logo_hover if self.which == "april" else None)

    # ---------------------------------------------------------------- holidays
    def _logo_hover(self, e) -> None:
        if self.app.reduce_motion:
            return
        self.image.rotate = 0 if e.data in (True, "true") else math.pi
        C.update(self.image)

    def _badge_hover(self, e) -> None:
        if e.data not in (True, "true") or self._dancing or self.app.reduce_motion or self.badge is None:
            return
        self._dancing = True
        self.app.page.run_task(self._dance)

    async def _dance(self) -> None:
        import asyncio

        b = self.badge
        try:
            for props, hold in hover_frames(HOLIDAY_MOTION.get(self.which, "")):
                if "turn" in props:
                    self._turn += props["turn"]
                    b.animate_rotation = ft.Animation(int(hold * 1000), ft.AnimationCurve.EASE_IN_OUT)
                    b.rotate = self._turn
                if "scale" in props:
                    b.scale = props["scale"]
                if "offset" in props:
                    b.offset = ft.Offset(*props["offset"])
                C.update(b)
                await asyncio.sleep(hold)
            # back to upright without spinning back (spins are whole turns)
            self._turn = round(self._turn / (2 * math.pi)) * 2 * math.pi
        finally:
            self._dancing = False

    # ---------------------------------------------------------------- clicks: the portal toss
    def _click(self, e=None) -> None:
        if not enabled() or self.busy or not self.counter.click() or self.app.reduce_motion:
            return
        self.busy = True
        self.app.page.run_task(self._toss)

    async def _toss(self) -> None:
        """The portal lights up (a blue-and-orange glow swells around the logo), then a game's cover comes out of
        it small, grows to full size and falls down across the window on a random arc, tumbling; the glow fades."""
        import asyncio

        page = self.app.page
        try:
            cover_url = await asyncio.to_thread(_a_cover)  # (may make a thumbnail: not on the event loop)
            if cover_url is None:
                return
            width = float(getattr(page, "width", None) or 1440)
            height = float(getattr(page, "height", None) or 900)
            s = self.size
            ox, oy = _logo_origin(s)
            cx, cy = ox + s / 2, oy + s / 2
            g = s * 4.2
            glow = ft.Container(width=g, height=g, left=cx - g / 2, top=cy - g / 2, shape=ft.BoxShape.CIRCLE,
                                gradient=ft.RadialGradient(colors=[T.soft("#FFFFFF", 0.85), T.soft(T.SECONDARY, 0.75),
                                                                   T.soft(T.ACCENT, 0.45), T.soft(T.ACCENT, 0.0)],
                                                           stops=[0.0, 0.18, 0.45, 1.0]),
                                opacity=0, scale=0.3,
                                animate_opacity=ft.Animation(260, ft.AnimationCurve.EASE_OUT),
                                animate_scale=ft.Animation(420, ft.AnimationCurve.EASE_OUT))

            def ring(color: str) -> ft.Container:  # a shock ring that runs out of the portal and fades
                r = s * 1.2
                return ft.Container(width=r, height=r, left=cx - r / 2, top=cy - r / 2, shape=ft.BoxShape.CIRCLE,
                                    border=ft.Border.all(T.px(3), color), opacity=1, scale=0.4, visible=False,
                                    animate_opacity=ft.Animation(650, ft.AnimationCurve.EASE_IN),
                                    animate_scale=ft.Animation(650, ft.AnimationCurve.EASE_OUT))
            rings = [ring(T.SECONDARY), ring(T.ACCENT)]
            cw, ch = T.px(72), T.px(96)
            dt = 0.06
            cover = ft.Container(width=cw, height=ch, left=cx - cw / 2, top=cy - ch / 2, scale=0.08, opacity=0,
                                 rotate=0, border_radius=T.px(5), clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                                 image=ft.DecorationImage(src=cover_url, fit=ft.BoxFit.COVER),
                                 border=ft.Border.all(1, T.soft("#FFFFFF", 0.25)),
                                 shadow=ft.BoxShadow(blur_radius=14, spread_radius=1, color=T.soft("#000000", 0.55),
                                                     offset=ft.Offset(0, 4)),
                                 animate_position=ft.Animation(int(dt * 1000), ft.AnimationCurve.LINEAR),
                                 animate_rotation=ft.Animation(int(dt * 1000), ft.AnimationCurve.LINEAR),
                                 animate_scale=ft.Animation(380, ft.AnimationCurve.EASE_OUT_BACK),
                                 animate_opacity=ft.Animation(120))
            layer = ft.TransparentPointer(ft.Stack([glow, *rings, cover], width=width, height=height), expand=True)
            page.overlay.append(layer)
            page.update()
            await asyncio.sleep(0.05)
            # the portal lights up and sends out two rings (blue, then orange)
            glow.opacity, glow.scale = 1, 1.0
            self.image.scale = 1.15
            C.update(glow, self.image)
            for r in rings:
                r.visible = True
                C.update(r)
                await asyncio.sleep(0.05)
                r.opacity, r.scale = 0, 2.6
                C.update(r)
                await asyncio.sleep(0.22)
            await asyncio.sleep(0.25)
            # out it comes
            cover.opacity, cover.scale = 1, 1.0
            C.update(cover)
            for i, (x, y, turn) in enumerate(toss_path(cx, cy, width, height, dt=dt)):
                cover.left, cover.top, cover.rotate = x - cw / 2, y - ch / 2, turn
                if i == 6:  # the glow dies down once the cover is clear of the portal
                    glow.opacity, glow.scale = 0, 0.6
                    glow.animate_opacity = ft.Animation(700, ft.AnimationCurve.EASE_IN)
                    self.image.scale = 1.0
                    C.update(glow, self.image)
                C.update(cover)
                await asyncio.sleep(dt)
            await asyncio.sleep(0.3)
            if layer in page.overlay:
                page.overlay.remove(layer)
                page.update()
        finally:
            self.busy = False

    # ---------------------------------------------------------------- searching for itself
    def hello_search(self) -> None:
        """The Library's search asked for "frameport": the logo spins round once and says it's there."""
        if not enabled() or self.busy:
            return
        self.busy = True
        self.app.page.run_task(self._spin_and_say)

    async def _spin_and_say(self) -> None:
        import asyncio

        page = self.app.page
        try:
            s = self.size
            ox, oy = _logo_origin(s)
            bubble = ft.Container(
                ft.Text(tr("That's me!"), size=T.T_META, weight=ft.FontWeight.W_700, color=T.BG),
                left=ox + s * 0.55, top=oy + s * 1.05, padding=ft.Padding(T.S2, T.px(5), T.S2, T.px(5)),
                bgcolor=T.ACCENT, border_radius=ft.BorderRadius(T.px(3), T.px(12), T.px(12), T.px(12)),
                shadow=ft.BoxShadow(blur_radius=10, color=T.soft("#000000", 0.45)),
                opacity=0, scale=0.4, animate_opacity=ft.Animation(180),
                animate_scale=ft.Animation(260, ft.AnimationCurve.EASE_OUT_BACK))
            layer = ft.TransparentPointer(ft.Stack([bubble], expand=True), expand=True)
            page.overlay.append(layer)
            page.update()
            if not self.app.reduce_motion:
                self.image.rotate = (self.image.rotate or 0) + 2 * math.pi
                C.update(self.image)
            await asyncio.sleep(0.35)
            bubble.opacity, bubble.scale = 1, 1.0
            C.update(bubble)
            await asyncio.sleep(2.6)
            bubble.opacity, bubble.scale = 0, 0.6
            C.update(bubble)
            await asyncio.sleep(0.3)
            if layer in page.overlay:
                page.overlay.remove(layer)
                page.update()
        finally:
            self.busy = False


def _a_cover() -> str | None:
    """A random game's cover thumbnail (by URL), None when no game has art."""
    from ..artwork import thumbs

    games = library.games()
    random.shuffle(games)
    for g in games[:12]:
        try:
            url = thumbs.url(g["package"], ("portrait", "square", "landscape"), 240)
        except Exception:  # noqa: BLE001
            url = None
        if url:
            return url
    return None


# ------------------------------------------------------------------ Type on Frame: hello

def wave_control() -> tuple[ft.Control, callable]:
    """A little headset with a hand that waves (hidden until wave() is called; Type on Frame shows it next to the
    last key). Returns (control, wave)."""
    parts = C.transit_parts()
    h = T.px(46)
    from . import glyphs

    w = h * glyphs.TRANSIT_SIZES["headset"][0] / glyphs.TRANSIT_SIZES["headset"][1]
    headset = ft.Image(src=parts["headset"], width=w, height=h, fit=ft.BoxFit.CONTAIN)
    hand = ft.Container(ft.Icon(ft.Icons.WAVING_HAND_ROUNDED, size=T.px(34), color=T.ACCENT), rotate=0,
                        animate_rotation=ft.Animation(160, ft.AnimationCurve.EASE_IN_OUT))
    box = ft.Container(ft.Row([headset, hand], spacing=T.px(4), tight=True,
                              vertical_alignment=ft.CrossAxisAlignment.CENTER),
                       opacity=0, scale=0.5, animate_opacity=ft.Animation(200),
                       animate_scale=ft.Animation(280, ft.AnimationCurve.EASE_OUT_BACK),
                       tooltip=tr("Hello to you too!"))
    state = {"busy": False}

    def wave(reduce: bool = False) -> None:
        if state["busy"]:
            return
        state["busy"] = True

        def run():
            try:
                box.opacity, box.scale = 1, 1.0
                C.update(box)
                time.sleep(0.3)
                for turn in ([] if reduce else [0.5, -0.3, 0.5, -0.3, 0.5, -0.3, 0.0]):
                    hand.rotate = turn
                    C.update(hand)
                    time.sleep(0.17)
                time.sleep(1.6)
                box.opacity, box.scale = 0, 0.7
                C.update(box)
                time.sleep(0.3)
            finally:
                state["busy"] = False
        threading.Thread(target=run, daemon=True).start()
    return box, wave


# ------------------------------------------------------------------ the battery ring: fully charged

def sparkle_layer(ring: float) -> tuple[list[ft.Control], callable]:
    """Little stars around the sidebar's battery ring (for its Stack, which must not clip) and sparkle(), which
    lets them twinkle once (the Frame just reached 100 % on the charger)."""
    spots = [(-0.12, 0.05, 0.42), (0.78, -0.12, 0.34), (0.95, 0.62, 0.4), (0.1, 0.86, 0.3), (0.42, -0.2, 0.26),
             (-0.18, 0.55, 0.26)]
    stars = []
    for x, y, k in spots:
        size = ring * k
        stars.append(ft.Container(ft.Icon(ft.Icons.AUTO_AWESOME, size=size, color="#FFE7A8"), left=ring * x,
                                  top=ring * y, scale=0, opacity=0, rotate=0,
                                  animate_scale=ft.Animation(300, ft.AnimationCurve.EASE_OUT_BACK),
                                  animate_opacity=ft.Animation(250),
                                  animate_rotation=ft.Animation(600, ft.AnimationCurve.EASE_OUT)))
    state = {"busy": False}

    def sparkle() -> None:
        if state["busy"]:
            return
        state["busy"] = True

        def run():
            try:
                for _ in range(2):  # the stars twinkle on one after another, twice
                    for star in stars:
                        star.scale, star.opacity, star.rotate = 1.0, 1, (star.rotate or 0) + 0.8
                        C.update(star)
                        time.sleep(0.08)
                    time.sleep(0.35)
                    for star in stars:
                        star.scale, star.opacity = 0, 0
                    C.update(*stars)
                    time.sleep(0.3)
            finally:
                state["busy"] = False
        threading.Thread(target=run, daemon=True).start()
    return stars, sparkle


def charged_now(before: dict | None, now: dict | None) -> bool:
    """The battery just reached 100 % on the charger (from a reading below 100; a first reading of 100 isn't
    "reaching" it)."""
    if not before or not now or before.get("percent") is None or now.get("percent") is None:
        return False
    return int(before["percent"]) < 100 <= int(now["percent"]) and bool(now.get("plugged"))


# ------------------------------------------------------------------ confetti

CONFETTI = 46


def celebrate(app: FramePortApp, message: str) -> None:
    """A milestone: the message as a toast, and (motion allowed) confetti in the portal's colours falling over the
    window for about two seconds."""
    app.toast(message)
    if app.reduce_motion:
        return
    app.page.run_task(_confetti, app)


async def _confetti(app: FramePortApp) -> None:
    import asyncio

    page = app.page
    width = float(getattr(page, "width", None) or 1440)
    height = float(getattr(page, "height", None) or 900)
    rnd = random.Random()
    colors = [T.ACCENT, T.SECONDARY, T.ACCENT, T.SECONDARY, "#FFFFFF"]
    pieces = []
    for i in range(CONFETTI):
        w = rnd.uniform(6, 11)
        piece = ft.Container(width=w, height=w * rnd.uniform(1.3, 2.0), bgcolor=colors[i % len(colors)],
                             border_radius=T.px(2), left=rnd.uniform(0.05, 0.95) * width, top=-30 - rnd.uniform(0, 160),
                             rotate=rnd.uniform(0, 6.3), opacity=1,
                             animate_position=ft.Animation(int(rnd.uniform(1500, 2300)), ft.AnimationCurve.EASE_IN),
                             animate_rotation=ft.Animation(2000, ft.AnimationCurve.LINEAR),
                             animate_opacity=ft.Animation(600))
        pieces.append(piece)
    layer = ft.TransparentPointer(ft.Stack(pieces, width=width, height=height), expand=True)
    page.overlay.append(layer)
    page.update()
    await asyncio.sleep(0.05)
    for p in pieces:
        p.top = height * rnd.uniform(0.75, 1.05)
        p.left = p.left + rnd.uniform(-120, 120)
        p.rotate = p.rotate + rnd.uniform(4, 12)
    page.update()
    await asyncio.sleep(1.9)
    for p in pieces:
        p.opacity = 0
    page.update()
    await asyncio.sleep(0.7)
    if layer in page.overlay:
        page.overlay.remove(layer)
        page.update()
