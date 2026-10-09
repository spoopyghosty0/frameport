"""The step language shared by the docs screenshots (docs/showcase/shots.yaml) and the demo tour (tour.yaml).

A step is a one-key mapping. App steps call the app the way its own handlers do; pointer steps move a real (drawn)
mouse to things found by name (see web.Session.find); hooks run the pretend Frame's scripted events.

    - go: monitor                      # a sidebar route (library, frame, files, screenshots, live, keyboard, …)
    - nav: "Monitor"                   # click that tab like a person (checked: the page really changed)
    - open_game: ${game}               # or {package: …, advanced: true} = with Customize open
    - call: {fn: screenshots_view.viewer, args: [0]}   # any app method (dotted path from the app)
    - pop_dialog: true                 # close the top dialog
    - theme: original                  # switch the colour theme live (what Settings → Appearance does)
    - hover: "Batman"                  # pointer: a name, {name, nth, dx, dy, exact} or [x, y]
    - click: "Monitor"                 # also right_click, double_click
    - drag: ["Videos", "Documents"]    # press on the first target, move through the others, release
    - type: "arc"                      # keyboard; press: Escape
    - scroll: {dy: 600, at: "Patches"}
    - wait: 1.5                        # seconds
    - wait_for: "Starting on Frame"    # until something with that name shows (timeout 15 s)
    - settle: true                     # until the picture stops changing
    - park: true                       # pointer to a place that hovers nothing
    - hook: fake_install               # or {name: fake_install, args: [${game}]}: see HOOKS

`${name}` in any string is replaced from the file's `vars:`.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time

from showcase import fakes

POINTER = {"hover", "move", "click", "right_click", "double_click"}
ACTIONS = POINTER | {"nav", "go", "open_game", "call", "pop_dialog", "theme", "drag", "type", "press", "scroll", "wait",
                     "wait_for", "settle", "park", "hook"}


class StepError(ValueError):
    pass


def substitute(value, variables: dict):
    """${name} → variables[name] in every string of a step (a whole-string reference keeps the value's type)."""
    if isinstance(value, str):
        m = re.fullmatch(r"\$\{(\w+)\}", value)
        if m:
            if m.group(1) not in variables:
                raise StepError(f"unknown variable ${{{m.group(1)}}}")
            return variables[m.group(1)]

        def one(m):
            if m.group(1) not in variables:
                raise StepError(f"unknown variable ${{{m.group(1)}}}")
            return str(variables[m.group(1)])
        return re.sub(r"\$\{(\w+)\}", one, value)
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    return value


def validate(steps, where: str = "steps") -> None:
    """Raises StepError for a malformed step list (checked before a render starts, and by the tests)."""
    if not isinstance(steps, list):
        raise StepError(f"{where}: a list of steps expected")
    for i, step in enumerate(steps):
        at = f"{where}[{i}]"
        if not isinstance(step, dict) or len(step) != 1:
            raise StepError(f"{at}: a step is a mapping with exactly one action, got {step!r}")
        (action, value), = step.items()
        if action not in ACTIONS:
            raise StepError(f"{at}: unknown action {action!r} (known: {', '.join(sorted(ACTIONS))})")
        if action == "hook":
            name = value.get("name") if isinstance(value, dict) else value
            if name not in HOOKS:
                raise StepError(f"{at}: unknown hook {name!r} (known: {', '.join(sorted(HOOKS))})")
        if action == "call" and not (isinstance(value, str) or isinstance(value, dict) and "fn" in value):
            raise StepError(f"{at}: call needs a method name or {{fn, args}}")
        if action == "drag" and not (isinstance(value, list) and len(value) >= 2):
            raise StepError(f"{at}: drag needs two or more targets")
        if action in ("wait",) and not isinstance(value, (int, float)):
            raise StepError(f"{at}: wait needs seconds")


def _resolve(app, dotted: str):
    obj = app
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def run(session, steps: list, variables: dict | None = None, log=None) -> None:
    """Run the steps on a web.Session. App calls run in this thread (the app serialises its own updates)."""
    app = session.app
    for step in substitute(steps, variables or {}):
        (action, v), = step.items()
        if log:
            log(f"  {action}: {v}")
        if action == "go":
            app.go(v)
        elif action == "open_game":
            if isinstance(v, dict):
                app.open_game(v["package"], advanced=bool(v.get("advanced")))
            else:
                app.open_game(v)
        elif action == "call":
            fn, args = (v, []) if isinstance(v, str) else (v["fn"], v.get("args") or [])
            method = _resolve(app, fn)
            if asyncio.iscoroutinefunction(method):  # (e.g. a FilePicker flow): the page's loop runs it
                app.page.run_task(method, *args)
            else:
                method(*args)
        elif action == "nav":
            _nav(session, v, log)
        elif action == "pop_dialog":
            app.page.pop_dialog()
        elif action == "theme":
            from frameport.core import library

            library.set_setting("ui.theme", v)
            app.restyle(v)
        elif action in POINTER:
            if action in ("hover", "move"):
                session.move(v)
            elif action == "click":
                session.click(v)
            elif action == "right_click":
                session.click(v, button="right")
            else:
                session.click(v)
                session.sleep(0.08)
                session.page.mouse.down()
                session.page.mouse.up()
        elif action == "drag":
            session.drag(v)
        elif action == "type":
            session.type(v)
        elif action == "press":
            session.press(v)
        elif action == "scroll":
            session.scroll(v.get("dy", 300), v.get("at"))
        elif action == "wait":
            session.sleep(float(v))
        elif action == "wait_for":
            session.find(v, timeout=15)
        elif action == "settle":
            session.settle(**({} if v is True else {k: w for k, w in v.items() if k in ("timeout",)}))
        elif action == "park":
            session.park()
        elif action == "hook":
            name, args = (v, []) if isinstance(v, str) else (v["name"], v.get("args") or [])
            HOOKS[name](session, *args)


def _nav(session, label: str, log=None) -> None:
    """Click a sidebar tab like a person, then make sure the page really changed: its heading shows in the content
    area. A click whose handler died would otherwise leave the old page in the picture for the rest of a recording
    (seen on CI before property writes took FLET_LOCK; kept as a safety net): then the route is opened directly and
    a WARN line printed."""
    from frameport.ui import app as app_module
    from showcase.web import Box, NotFound

    keys = {str(lbl).casefold(): key for key, lbl, _icon in app_module.NAV}
    key = keys.get(label.casefold())
    if key is None:
        raise StepError(f"nav: {label!r} is not a sidebar tab ({', '.join(sorted(keys))})")
    session.click(label)
    vw, _vh = session.viewport
    content = Box(260, 0, vw - 260, 160)  # right of the sidebar, the page's title row
    for _ in range(2):
        try:
            session.find(label, within=content, timeout=2.5)
            return
        except NotFound:
            print(f"WARN nav {label}: the page didn't change after the click, opening it directly", flush=True)
            session.app.go(key)
    session.find(label, within=content, timeout=2.5)


# ------------------------------------------------------------------ hooks: the pretend Frame's scripted events

def _fake_install(session, package: str, pace: float = 1.0, wait: bool = False) -> None:
    """Install `package` on the pretend Frame through the real job queue (every real stage, an upload, a passing
    launch test). wait: until it's done."""
    app = session.app
    app.submit(f"Install {app._title(package)} on Frame", fakes.install_job(package, pace), package, "install",
               to="frame")
    if wait:
        _wait_jobs(session)


def _wait_jobs(session, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    session.sleep(0.5)
    while time.time() < deadline and any(j.active for j in session.app.jobs.jobs):
        session.sleep(0.3)


def _held_install(session, package: str, fraction: float = 0.62) -> None:
    """An install held mid-upload: the transit in the sidebar, the game page and Activity. It stays there until the
    window closes (Session.close cancels it)."""
    from frameport.ui.jobs import Job

    app, reached = session.app, threading.Event()

    def run(job):
        rep = job.reporter
        for stage in fakes.FAKE_INSTALL_STAGES:
            rep.stage(stage)
        time.sleep(0.3)  # past the job queue's notification throttle
        rep.progress(fraction, f"{package}.apk", speed="36.4 MB/s · ~1 min left")
        reached.set()
        while True:
            rep.check_cancel()
            time.sleep(0.2)

    app.jobs.submit(Job(f"Install {app._title(package)} on Frame", run, package, "install"))
    reached.wait(10)


def _live_stream(session) -> None:
    fakes.fake_live_stream(session.app)


def _stop_live(session) -> None:
    fakes.stop_fake_live(session.app)


def _monitor_details(session) -> None:
    fakes.open_monitor_details(session.app)


def _type_tab(session) -> None:
    fakes.open_type_tab(session.app)


def _select_files(session, first: int = 1, count: int = 2) -> None:
    v = session.app.files_view
    for e in v.entries[first:first + count]:
        v._toggle(e.path, True)


def _disconnect(session) -> None:
    """The sidebar as before connecting (the tour connects in front of the camera)."""
    app = session.app
    app.target, app.frame_info, app.frame_state = None, None, "none"
    app._refresh_sidebar()
    app.refresh_view()


def _connect(session, delay: float = 1.0) -> None:
    """Connecting… then connected to the pretend Frame (the live card starts streaming)."""
    app = session.app
    app.frame_state = "connecting"
    app._refresh_sidebar()
    session.sleep(delay)
    fakes.attach_fake_frame(app, session.server.monitor_session)
    app.refresh_view()
    app._refresh_sidebar()


def _settings_section(session, key: str) -> None:
    """Settings, scrolled to one section (its index entry)."""
    app = session.app
    app.go("settings")
    session.sleep(1.0)  # mounted first: scrolling an unmounted column does nothing
    app.page.run_task(app.settings_view.show_section, key)


def _first_run(session, folder: str = "D:/Games", pace: float = 1.0, tools_missing: bool = True,
               tools_delay: float = 4.0) -> None:
    """FramePort's first start (the "fresh" demo profile): the welcome screen's tool download is the pretend one
    (fakes.tools_job; it waits `tools_delay` s so filming catches it from the start), and "Scan a folder…" scans
    `folder` at once instead of opening the native folder picker (a script can't drive it): the demo games appear
    one by one."""
    import json

    from frameport.core.paths import user_data_dir

    app = session.app
    fakes.TOOLS["ready"] = not tools_missing
    app.update_tools = lambda update=False, quiet=False: fakes.tools_job(app, pace, tools_delay)
    entries = json.loads((user_data_dir() / "showcase-scan.json").read_text())
    app.pick_folder = lambda e=None: fakes.scan_job(app, folder, entries, pace)
    app.welcome_started = False
    app.library_view = None  # (a Library built already would keep the real folder picker)


def _pairing_done(session, delay: float = 1.5) -> None:
    """The Frame ran the setup command: FramePort hears from it and connects (the pretend Frame)."""
    app = session.app
    if app.pairing is not None:
        app.pairing.stop()
    _connect(session, delay)


HOOKS = {"fake_install": _fake_install, "wait_jobs": _wait_jobs, "held_install": _held_install,
         "live_stream": _live_stream, "stop_live": _stop_live, "monitor_details": _monitor_details,
         "type_tab": _type_tab, "select_files": _select_files, "disconnect": _disconnect, "connect": _connect,
         "settings_section": _settings_section, "first_run": _first_run, "pairing_done": _pairing_done}
