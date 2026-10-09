"""Small surprises that give FramePort some character. None of them is announced anywhere in the UI, and none
gets in the way:

- Click the sidebar logo seven times quickly: a game's cover pops out of the logo's portal, flips over and dives
  back in (the portal pulses).
- Rest the pointer on the Monitor's frame rate: it says how the game is doing ("Smooth as butter").
- Install milestones (the 1st, 10th and 100th game on the Frame): confetti in the portal's two colours and a
  thank-you.
- Holidays: the logo wears a little pumpkin at the end of October and a snowflake around the new year.

Off with library setting `ui.easter_eggs` = False (the docs screenshots and videos turn them off; there's no
switch in Settings). Animations respect Reduce motion (the words stay, the motion goes). `FRAMEPORT_TODAY`
(YYYY-MM-DD) pretends another date (videos, tests). Pure helpers are tested in tests/test_easter.py.
"""
from __future__ import annotations

import datetime as _dt
import os
import random
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


def holiday(day: _dt.date) -> str | None:
    """"halloween" (Oct 24-31), "winter" (Dec 20 - Jan 2) or None."""
    if day.month == 10 and day.day >= 24:
        return "halloween"
    if (day.month == 12 and day.day >= 20) or (day.month == 1 and day.day <= 2):
        return "winter"
    return None


HOLIDAY_ICON = {"halloween": "holiday-pumpkin", "winter": "holiday-snowflake"}


def holiday_tip(which: str) -> str:
    return tr("Happy Halloween!") if which == "halloween" else tr("Happy holidays!")


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


# ------------------------------------------------------------------ the sidebar logo

class Logo:
    """The sidebar's logo with its surprises: a holiday badge, and the cover that flies out of its portal after
    seven quick clicks. `.control` goes into the sidebar."""

    def __init__(self, app: FramePortApp, size: float):
        self.app, self.size = app, size
        self.counter = ClickCounter()
        self.busy = False
        s = size
        self.traveller = ft.Container(width=s * 0.9, height=s * 1.2, left=s * 0.05, top=-s * 0.1,
                                      border_radius=T.px(3), scale=0, opacity=0,
                                      clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                                      shadow=ft.BoxShadow(blur_radius=8, color=T.soft("#000000", 0.6)),
                                      animate_offset=ft.Animation(420, ft.AnimationCurve.EASE_OUT),
                                      animate_scale=ft.Animation(420, ft.AnimationCurve.EASE_OUT),
                                      animate_rotation=ft.Animation(420, ft.AnimationCurve.EASE_IN_OUT),
                                      animate_opacity=ft.Animation(160))
        layers: list[ft.Control] = [C.logo(s)]
        which = holiday(today()) if enabled() else None
        if which:
            badge = s * 0.58
            layers.append(ft.Container(ft.Image(src=C._asset_src(HOLIDAY_ICON[which]), width=badge, height=badge,
                                                fit=ft.BoxFit.CONTAIN),
                                       left=s - badge * 0.7, top=s - badge * 0.75, tooltip=holiday_tip(which)))
        layers.append(self.traveller)
        self.control = ft.Container(ft.Stack(layers, width=s, height=s, clip_behavior=ft.ClipBehavior.NONE),
                                    width=s, height=s, on_click=self._click)

    def _click(self, e=None) -> None:
        if not enabled() or self.busy or not self.counter.click() or self.app.reduce_motion:
            return
        self.busy = True
        self.app.page.run_task(self._fly)

    async def _fly(self) -> None:
        """Out of the portal, a somersault over the wordmark (the logo sits in the window's top-left corner: the
        open space is to the right), back in through the portal's orange side."""
        import asyncio

        t = self.traveller
        try:
            cover = await asyncio.to_thread(_a_cover)  # (may make a thumbnail: not on the event loop)
            if cover is None:
                return
            t.content = ft.Image(src=cover, fit=ft.BoxFit.COVER, width=t.width, height=t.height)
            C.update(t)
            await asyncio.sleep(0.05)
            for offset, scale, rotate, wait in (
                    ((0.9, 0.05), 1.0, 0.25, 0.42),     # pops out to the right
                    ((2.6, 0.15), 1.2, 3.14, 0.45),     # a somersault over the wordmark
                    ((1.3, 0.55), 1.0, 5.9, 0.42),      # swings back
                    ((0.05, 0.1), 0.0, 6.6, 0.42)):     # and dives into the portal
                t.opacity, t.offset, t.scale, t.rotate = 1, ft.Offset(*offset), scale, rotate
                C.update(t)
                await asyncio.sleep(wait)
            t.opacity, t.offset, t.rotate = 0, ft.Offset(0, 0), 0
            C.update(t)
            self.app._pulse_portal()
        finally:
            self.busy = False


def _a_cover() -> str | None:
    """A random game's cover thumbnail (by URL), None when no game has art."""
    from ..artwork import thumbs

    games = library.games()
    random.shuffle(games)
    for g in games[:12]:
        try:
            url = thumbs.url(g["package"], ("portrait", "square", "landscape"), 160)
        except Exception:  # noqa: BLE001
            url = None
        if url:
            return url
    return None


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
