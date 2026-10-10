from frameport.ui.app import install_state


def game(sha=None, alt=None):
    return {"package": "com.x", "build": {"sha256": sha, "alt_sha256": alt} if sha else {}}


def frame(*deps):
    return {"installed": list(deps)}


def test_not_connected():
    assert install_state(game("a"), None) is None


def test_missing():
    assert install_state(game("a"), frame({"package": "com.other", "sha256": "a"})) == "missing"


def test_installed_same_build_or_alt():
    assert install_state(game("a", "b"), frame({"package": "com.x", "sha256": "a"})) == "installed"
    assert install_state(game("a", "b"), frame({"package": "com.x", "sha256": "b"})) == "installed"


def test_installed_without_local_build():
    assert install_state(game(), frame({"package": "com.x", "sha256": "z"})) == "installed"


def test_outdated():
    assert install_state(game("a"), frame({"package": "com.x", "sha256": "old"})) == "outdated"



def test_outdated_after_a_newer_apk_was_added():
    """GitHub #161: re-adding a newer APK of an installed game shows "Update on Frame", not "Reinstall"."""
    g = game("a")
    g["build"]["source_version"] = "1.0"
    g["analysis"] = {"version": "1.0"}
    assert install_state(g, frame({"package": "com.x", "sha256": "a"})) == "installed"
    g["analysis"]["version"] = "1.1"
    assert install_state(g, frame({"package": "com.x", "sha256": "a"})) == "outdated"

# ------------------------------------------------------------------------------------------ library filters / tags
def lib_games():
    return [
        {"package": "com.a", "title": "Alpha", "added": 1, "recipe": {"status": "works", "patches": {}},
         "analysis": {"engine": "Unity", "xr": "OpenXR"}, "tags": ["Favorite"], "data_bytes": 5},
        {"package": "rift.b", "kind": "rift", "title": "Bravo", "added": 3, "recipe": {"status": "unknown"},
         "analysis": {"engine": "Unreal", "xr": "LibOVR", "extra": {"data_bytes": 50}},
         "last_test": {"time": 100}},
        {"package": "com.c", "title": "charlie", "added": 2, "recipe": {"status": "issues",
                                                                        "patches": {"patch_force_passthrough": {}}},
         "analysis": {"engine": "Unreal", "xr": "VrApi"}, "installs": {"frame": {"time": 200}}, "data_bytes": 20},
    ]


def test_filters_and_sort():
    from frameport.ui.views.library import DEFAULT_FILTERS, filter_games

    games = lib_games()
    f = dict(DEFAULT_FILTERS)
    names = lambda fl, **kw: [g["title"] for g in filter_games(games, {**f, **fl}, **kw)]  # noqa: E731
    assert names({}) == ["Alpha", "Bravo", "charlie"]
    assert names({"platform": "pcvr"}) == ["Bravo"] and names({"platform": "quest"}) == ["Alpha", "charlie"]
    assert names({"status": "issues"}) == ["charlie"]
    assert names({"q": "unreal"}) == ["Bravo", "charlie"]  # search covers tags
    assert names({"tags": ["favorite"]}) == ["Alpha"] and names({"tags": ["Unreal", "Mixed reality"]}) == ["charlie"]
    assert names({"sort": "recent"}) == ["Bravo", "charlie", "Alpha"]
    assert names({"sort": "played"}) == ["charlie", "Bravo", "Alpha"]
    assert names({"sort": "size"}) == ["Bravo", "charlie", "Alpha"]
    assert names({"sort": "status"}) == ["Alpha", "charlie", "Bravo"]
    frame = {"installed": [{"package": "com.c"}]}
    assert names({"where": "frame"}, frame_info=frame) == ["charlie"]
    assert names({"where": "pc"}, pc_installs={"rift.b"}) == ["Bravo"]
    assert names({"where": "none"}, frame_info=frame, pc_installs={"rift.b"}) == ["Alpha"]


def test_tags():
    from frameport.ui.views.library import all_tags, auto_tags, game_tags, normalize_tag

    a, b, c = lib_games()
    assert auto_tags(b) == ["PC VR", "Unreal", "LibOVR"]
    assert "Mixed reality" in auto_tags(c)
    assert game_tags(a)[0] == "Favorite"
    assert all_tags([a, b, c])[0] == "Favorite"  # the user's own tags first
    assert normalize_tag("  my,  tag ") == "my tag"


# ------------------------------------------------------------------------------------------ job queue
def test_jobs_run_in_order_one_at_a_time():
    import threading
    import time

    from frameport.ui.jobs import Job, JobManager

    events, running = [], []
    lock = threading.Lock()

    def work(name):
        def run(job):
            with lock:
                running.append(name)
                assert len(running) == 1, "two jobs ran at once"
            job.reporter.stage(f"{name} stage")
            job.reporter.check("thing", True, "ok")
            time.sleep(0.05)
            with lock:
                running.remove(name)
            events.append(name)
            return name
        return run
    m = JobManager(throttle=0)
    jobs = [m.submit(Job(n, work(n), package="p" if n == "a" else None)) for n in "abc"]
    assert m.wait_idle(5)
    assert events == ["a", "b", "c"] and [j.state for j in jobs] == ["done"] * 3
    assert jobs[0].stages == ["a stage"] and jobs[0].checks[0]["ok"] is True and jobs[0].result == "a"
    assert m.busy_with("p") is None


def test_job_cancel_and_failure():
    import time

    from frameport.ui.jobs import Job, JobManager

    def slow(job):
        for _ in range(100):
            job.reporter.check_cancel()
            time.sleep(0.02)

    def boom(job):
        raise RuntimeError("no Frame")
    m = JobManager(throttle=0)
    first = m.submit(Job("slow", slow, package="x"))
    queued = m.submit(Job("later", slow))
    failing = m.submit(Job("boom", boom))
    time.sleep(0.1)
    assert m.busy_with("x") is first and first.state == "running"
    m.cancel(queued)  # still queued: never runs
    m.cancel(first)
    assert m.wait_idle(5)
    assert (first.state, queued.state, failing.state) == ("cancelled", "cancelled", "failed")
    assert failing.error == "no Frame"
    m.clear_finished()
    assert m.jobs == []


def test_files_tree():
    from frameport.ui.views import files_dialog as fd

    files = [["game/Bin/Game.exe", 4], ["game/Bin/Data.pak", 10], ["launch.sh", 1], ["game/Readme.txt", 2]]
    root = fd.build_tree("Install folder", files)
    assert (root.size, root.files) == (17, 4)
    game = root.children["game"]
    assert (game.size, game.files) == (16, 3)
    # folders first, then files; only expanded folders show their children
    assert [(d, n.name) for d, n in fd.visible_rows(root, set())] == [(0, "game"), (0, "launch.sh")]
    rows = fd.visible_rows(root, {"game", "game/Bin"})
    assert [(d, n.name) for d, n in rows] == [(0, "game"), (1, "Bin"), (2, "Data.pak"), (2, "Game.exe"),
                                              (1, "Readme.txt"), (0, "launch.sh")]
    assert fd.matches(files, ".PAK") == [["game/Bin/Data.pak", 10]]
    assert fd.human(3 * 2**30) == "3.0 GiB" and fd.human(512) == "512 B"


def test_files_tree_caps_children():
    from frameport.ui.views import files_dialog as fd

    root = fd.build_tree("x", [[f"f{i:04}", 1] for i in range(fd.MAX_CHILDREN + 5)])
    rows = fd.visible_rows(root, set())
    assert len(rows) == fd.MAX_CHILDREN + 1 and rows[-1] == (0, 5)


def test_install_state_tracks_patch_settings():
    from frameport.ui import components as C

    g = {"package": "rift.g", "kind": "rift", "recipe": {"patches": {"pcvr.revive": {}, "pcvr.xr_timefix": {}}}}
    fi = {"installed": [{"package": "rift.g", "recipe": {"patches": ["pcvr.revive", "pcvr.xr_timefix"]}}]}
    assert C.install_state(g, fi) == "installed" and C.settings_diff(g, fi) is None
    g["recipe"]["patches"].pop("pcvr.revive")
    g["recipe"]["patches"]["pcvr.no_crash_reporter"] = {}
    assert C.install_state(g, fi) == "outdated"
    assert C.settings_diff(g, fi) == (["pcvr.no_crash_reporter"], ["pcvr.revive"])
    old = {"installed": [{"package": "rift.g"}]}  # installed before recipes were recorded: no false alarm
    assert C.install_state(g, old) == "installed"
    assert C.install_state(g, {"installed": []}) == "missing"


def test_game_on_a_missing_sd_card_is_still_installed():
    """GitHub #90: a game whose microSD card is out has no files to see, but it is installed (not "Not installed")."""
    from frameport.ui import components as C

    sd = {"internal": False, "path": "/run/media/steamos/SD", "label": "SD"}
    gone = frame({"package": "com.x", "sha256": "a", "apk_present": False, "drive": sd, "drive_missing": True})
    assert install_state(game("a"), gone) == "installed"
    assert C.drive_note(C.frame_drive(game("a"), gone)) == " · SD not inserted"
    here = frame({"package": "com.x", "sha256": "a", "apk_present": True, "drive": sd, "drive_missing": False})
    assert C.drive_note(C.frame_drive(game("a"), here)) == " · on SD"
    internal = frame({"package": "com.x", "sha256": "a", "drive": {"internal": True, "path": "/home/steamos",
                                                                     "label": "Internal storage"}})
    assert C.drive_note(C.frame_drive(game("a"), internal)) == ""
    assert C.frame_drive(game("a"), frame({"package": "com.x"})) is None  # an agent before v63
    assert install_state(game("a"), frame({"package": "com.x", "sha256": "a", "apk_present": False})) == "missing"


def test_library_writes_from_many_threads_are_not_lost():
    """Every thread's change survives (library.json read-modify-write used to race between jobs and the UI)."""
    import threading

    from frameport.core import library

    def work(i):
        for j in range(20):
            library.upsert_game(f"pkg.{i}", n=j)
            library.update_setting("counter", lambda v: (v or 0) + 1)
    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {g["package"] for g in library.games() if g["package"].startswith("pkg.")} == {f"pkg.{i}" for i in range(6)}
    assert library.setting("counter") == 120


def test_refresh_keeps_a_working_connection_when_a_status_query_fails():
    """An agent error during the background refresh must not close the connection a running job is using."""
    from types import SimpleNamespace

    from frameport.frame.connection import AgentFailed
    from frameport.ui.app import FramePortApp

    closed = []

    class Target:
        def __init__(self, alive):
            self.frame = SimpleNamespace(alive=lambda: alive)

        def describe(self):
            raise AgentFailed("agent info: boom")

        def close(self):
            closed.append(self)

    def app_with(target):
        app = object.__new__(FramePortApp)
        app.target, app.frame_state, app.frame_info = target, "connected", {"installed": []}
        app.route = ("settings",)
        app._refresh_sidebar = lambda *a, **k: None
        app.toast = lambda *a, **k: None
        return app

    up = app_with(Target(alive=True))
    up.refresh_frame(quiet=True, background=False)
    assert up.frame_state == "connected" and not closed
    down = app_with(Target(alive=False))
    down.refresh_frame(quiet=True, background=False)
    assert down.frame_state == "offline" and closed == [down.target]


def test_pairing_server_needs_the_code_and_stops_after_pairing_or_guessing(monkeypatch):
    import tempfile
    import time
    import urllib.error
    import urllib.request
    from pathlib import Path

    from frameport.frame import pairing

    monkeypatch.setattr(pairing, "local_ip_towards", lambda *a: "127.0.0.1")
    monkeypatch.setattr(pairing, "MAX_FAILURES", 3)

    def get(server, path):
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}", timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code
        except OSError:
            return None

    seen = []
    s = pairing.PairingServer(on_paired=seen.append).start()
    other = pairing.PairingServer().start()  # a second server (port taken) falls back to another port
    assert other.port != s.port
    other.stop()
    assert len(s.code) == 8
    assert s.one_liner == f"curl -fsS 127.0.0.1:{s.port}/{s.code} | bash"  # typed by hand on the Frame: short
    assert s.requests == 0
    assert get(s, f"/{s.code}") == 200 and get(s, "/deadbeef") == 403  # the short form serves the script
    assert s.requests == 2  # any request (even a wrong code) proves the Frame can reach us: no firewall hint
    script = urllib.request.urlopen(f"http://127.0.0.1:{s.port}/{s.code}", timeout=5).read().decode()
    assert f"http://127.0.0.1:{s.port}" in script and s.code in script and "__PAIR_CODE__" not in script
    assert get(s, "/key?code=000000") == 403
    assert get(s, f"/paired?code={s.code}&user=steamos&host=frame") == 200 and seen
    for _ in range(50):
        if not s.running:
            break
        time.sleep(0.05)
    assert not s.running
    flag = Path(tempfile.mkdtemp()) / "pairing.flag"  # WSL: the temporary firewall rule lives while this exists
    flag.write_text("x")
    f = pairing.PairingServer().start()
    f.flag = flag
    f.stop()
    assert not flag.exists()
    g = pairing.PairingServer().start()
    for _ in range(3):
        get(g, "/key?code=guess")
    for _ in range(50):
        if not g.running:
            break
        time.sleep(0.05)
    assert not g.running


def test_update_all_asks_each_question_once_for_all_games(monkeypatch):
    """Update all = one batch per target: the Oculus-on-Frame question is one dialog with a checkbox per game (it used
    to come once per game, each with a single checkbox), "Can't run" games are updated too."""
    from types import SimpleNamespace

    from frameport.core import library
    from frameport.ui.app import FramePortApp

    games = {f"rift.g{i}": {"package": f"rift.g{i}", "kind": "rift", "title": f"G{i}", "analysis": {},
                            "recipe": {"patches": {"pcvr.revive": {}}, "status": "unsupported" if i == 2 else "works"}}
             for i in range(3)}
    games["com.quest"] = {"package": "com.quest", "title": "Q", "analysis": {}, "recipe": {"patches": {}}}
    monkeypatch.setattr(library, "game", games.get)
    dialogs, submitted = [], []
    app = object.__new__(FramePortApp)
    app.page = SimpleNamespace(show_dialog=dialogs.append, pop_dialog=lambda: None)
    app.jobs = SimpleNamespace(busy_with=lambda p: None)
    app.library_view = None
    app.toast = lambda *a, **k: None
    app.show_activity = lambda *a: None
    app._title = lambda p: games[p]["title"]
    app._submit_install = lambda p, to: submitted.append((p, to))
    app.updatable = lambda: [("rift.g0", "frame"), ("com.quest", "frame"), ("rift.g1", "frame"), ("rift.g2", "frame"),
                             ("rift.g0", "pc")]
    app.update_all()
    assert len(dialogs) == 1  # one question for the three Oculus games on the Frame
    boxes = [c for c in dialogs[0].content.content.controls if c.__class__.__name__ == "Checkbox"]
    assert sorted(b.label for b in boxes) == ["G0", "G1", "G2"]
    for b in boxes:
        b.value = True
    app.update_all()  # a second click while the question is open starts nothing
    assert len(dialogs) == 1
    ok = dialogs[0].actions[-1]
    ok.on_click(None)
    ok.on_click(None)  # double click while the dialog closes: acts once
    dialogs[0].on_dismiss(None)  # the close event after the button: ignored
    assert sorted(p for p, to in submitted if to == "frame") == ["com.quest", "rift.g0", "rift.g1", "rift.g2"]
    assert submitted.count(("rift.g0", "pc")) == 1  # the PC batch follows once the Frame batch is queued
    assert not app._asking


def test_closing_an_install_question_without_a_button_cancels_it(monkeypatch):
    from types import SimpleNamespace

    from frameport.core import library
    from frameport.ui.app import FramePortApp

    g = {"package": "rift.g", "kind": "rift", "title": "G", "analysis": {}, "recipe": {"patches": {"pcvr.revive": {}}}}
    monkeypatch.setattr(library, "game", {"rift.g": g}.get)
    dialogs, submitted = [], []
    app = object.__new__(FramePortApp)
    app.page = SimpleNamespace(show_dialog=dialogs.append, pop_dialog=lambda: None)
    app.jobs = SimpleNamespace(busy_with=lambda p: None)
    app.library_view, app.toast, app._title = None, lambda *a, **k: None, lambda p: "G"
    app._submit_install = lambda p, to: submitted.append(p)
    app.install_many(["rift.g"], "frame")
    dialogs[0].on_dismiss(None)  # Esc
    assert not app._asking and not submitted
    app.install_many(["rift.g"], "frame")  # a later install can ask again
    assert len(dialogs) == 2


def test_activity_pins_the_running_job_above_a_long_queue():
    """With 40 waiting installs the running one used to be listed after all of them (or cut off at 30)."""
    from types import SimpleNamespace

    from frameport.ui.jobs import Job, JobManager
    from frameport.ui.views.activity import ActivityPanel

    jm = JobManager(save_logs=False)
    done = [Job(f"done {i}", run=lambda j: None, state="done", created=i, finished=100 + i) for i in range(25)]
    running = Job("running", run=lambda j: None, state="running", created=50, started=50)
    queued = [Job(f"queued {i}", run=lambda j: None, created=60 + i) for i in range(40)]
    jm.jobs = [*done, running, *queued]
    order = jm.recent(20)
    assert order[0] is running and order[1:41] == queued and len(order) == 61
    assert [j.title for j in order[41:43]] == ["done 24", "done 23"]  # finished: newest first, at most 20
    panel = ActivityPanel(SimpleNamespace(jobs=jm, job_followups=lambda j: [], copy=None, show_log_file=None,
                                          show_activity=None))
    panel.root.width = 400  # open
    panel.refresh(update=False)
    assert len(panel.pinned.controls) == 1  # the running job, outside the scrolling list
    assert panel.list.controls[0].controls[0].value == "Waiting (40)"


def test_queue_waits_for_the_frame_instead_of_failing_every_install():
    """A job that loses the Frame goes back to the front and the queue pauses; resume() continues it."""
    import time

    from paramiko.ssh_exception import NoValidConnectionsError

    from frameport.ui.jobs import Job, JobManager

    jm = JobManager(save_logs=False)
    runs, online = [], {"up": False}

    def install(name):
        def run(job):
            runs.append(name)
            if not online["up"]:
                raise NoValidConnectionsError({("10.0.0.9", 22): OSError("down")})
            return "ok"
        return run

    a = jm.submit(Job("A", install("A"), "a", "install", needs_frame=True))
    b = jm.submit(Job("B", install("B"), "b", "install", needs_frame=True))
    for _ in range(200):
        if jm.paused:
            break
        time.sleep(0.01)
    assert jm.paused == "frame" and a.state == "queued" and b.state == "queued" and runs == ["A"]
    assert jm.has_frame_work()
    online["up"] = True
    jm.resume()
    assert jm.wait_idle(5) and a.state == b.state == "done" and runs == ["A", "A", "B"]
    assert a.retries == 1 and not jm.has_frame_work()
    pc = jm.submit(Job("PC", install("PC"), "p", "install"))  # doesn't use the Frame: fails normally
    online["up"] = False
    jm.wait_idle(5)
    assert pc.state in ("done", "failed") and not jm.paused


def test_free_space_is_checked_before_queueing(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from frameport.core import library
    from frameport.ui.app import FramePortApp

    apk = tmp_path / "g.apk"
    apk.write_bytes(b"x" * 1000)
    games = {p: {"package": p, "title": p, "apk": str(apk), "data_bytes": 3 * 2**30, "analysis": {},
                 "recipe": {"patches": {}}} for p in ("com.a", "com.b")}
    monkeypatch.setattr(library, "game", games.get)
    dialogs, submitted = [], []
    app = object.__new__(FramePortApp)
    app.page = SimpleNamespace(show_dialog=dialogs.append, pop_dialog=lambda: None)
    app.jobs = SimpleNamespace(busy_with=lambda p: None)
    app.library_view, app.toast, app._title = None, lambda *a, **k: None, lambda p: p
    app._submit_install = lambda p, to: submitted.append(p)
    app.frame_info = {"free_bytes": 5 * 2**30, "installed": [{"package": "com.b"}]}  # com.b: update, no data
    assert app.upload_estimate(["com.a", "com.b"]) == 3 * 2**30 + 2000
    app.install_many(["com.a", "com.b"], "frame")
    assert submitted == ["com.a", "com.b"] and not dialogs  # fits (5 GiB free, 1 GiB spare)
    app.frame_info["free_bytes"] = 3 * 2**30
    submitted.clear()
    app.install_many(["com.a", "com.b"], "frame")
    assert not submitted and len(dialogs) == 1  # asks first
    dialogs[0].actions[-1].on_click(None)  # Install anyway
    assert submitted == ["com.a", "com.b"] and not app._asking


def test_activity_progress_ticks_only_touch_the_progress_controls(monkeypatch):
    """Progress several times a second must not rebuild or re-diff the whole panel (that made the app sluggish)."""
    from types import SimpleNamespace

    from frameport.ui import components as C
    from frameport.ui.jobs import Job, JobManager
    from frameport.ui.views.activity import ActivityPanel

    jm = JobManager(save_logs=False)
    run = Job("running", run=lambda j: None, state="running", created=1, started=1, fraction=0.1)
    jm.jobs = [run, *[Job(f"q{i}", run=lambda j: None, created=2 + i) for i in range(30)]]
    panel = ActivityPanel(SimpleNamespace(jobs=jm, job_followups=lambda j: [], copy=None, show_log_file=None))
    panel.root.width = 400
    updated = []
    monkeypatch.setattr(C, "update", lambda *controls: updated.append(controls))
    panel.refresh()
    first_list = list(panel.list.controls)
    assert updated[-1] == (panel.root,)  # the first time: everything
    run.fraction = 0.5
    panel.refresh()
    bar = panel._live[run.id][0]
    assert bar.value == 0.5 and panel.root not in updated[-1] and bar in updated[-1]
    assert panel.list.controls == first_list  # nothing rebuilt


def test_flet_updates_are_serialized():
    import threading

    from flet.messaging.session import Session

    from frameport.ui.app import serialize_flet_updates

    original = Session.patch_control
    try:
        active, peak = [0], [0]
        guard = threading.Lock()

        def fake(self, *a, **k):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            threading.Event().wait(0.01)
            with guard:
                active[0] -= 1
        Session.patch_control = fake
        serialize_flet_updates()
        serialize_flet_updates()  # idempotent
        threads = [threading.Thread(target=Session.patch_control, args=(None,)) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert peak[0] == 1
    finally:
        Session.patch_control = original


def test_window_sessions_share_one_job_queue():
    """Every Flet session (a window reconnect builds a new app object) uses the same queue: a job started in an
    old session is visible and the next one waits for it, instead of a second build of the same game at once."""
    from frameport.ui import jobs

    first, second = jobs.shared(), jobs.shared()
    assert first is second
    seen = []
    listener = seen.append
    first.subscribe(listener)
    first.unsubscribe(listener)
    first.unsubscribe(listener)  # twice is harmless
    assert listener not in first._listeners


def test_one_build_per_game_at_a_time():
    from frameport import pipeline

    assert pipeline._build_lock("com.x.y") is pipeline._build_lock("com.x.y")
    assert pipeline._build_lock("com.x.y") is not pipeline._build_lock("com.x.z")


def test_window_geometry_restores_size_position_and_maximized():
    from frameport.ui.app import window_geometry

    first = window_geometry(None, 1.25)
    assert first == {"width": 1600, "height": 975}  # no position: the system places it
    got = window_geometry({"size": [1400, 900], "pos": [320, 140], "maximized": True}, 1.0)
    assert got == {"width": 1400, "height": 900, "left": 320, "top": 140, "maximized": True}
    assert window_geometry({"size": [800, 500]}, 1.0) == {"width": 1000, "height": 680}  # minimum size
    # a monitor to the left (negative x) is fine; far off or malformed positions are ignored (GitHub #32)
    assert window_geometry({"pos": [-1800, 50]}, 1.0)["left"] == -1800
    for bad in ([-20000, 0], [100, -400], [99999, 0], ["a", 1], [1]):
        assert "left" not in window_geometry({"pos": bad}, 1.0)


def test_superseded_patch_is_not_an_update():
    """A build that left haptic_fix out because the OVRPort runtime already fixes it (patches/upstream.py) showed
    "Update on Frame" forever: the recipe has the patch, the Frame's recorded recipe doesn't (VR HOT, Vader)."""
    from frameport.ui import components as C

    g = {"package": "com.x.y", "recipe": {"patches": {"frame.adapter": {}, "adapter.haptic_fix": {"value": 1}}},
         "build": {"sha256": "aa", "superseded": {"adapter.haptic_fix": "ovrport.haptic_envelope"}}}
    from frameport.patches.base import recipe_fingerprint

    g["build"]["recipe_fp"] = recipe_fingerprint(g["recipe"])
    fi = {"installed": [{"package": "com.x.y", "sha256": "aa", "recipe": {"patches": ["frame.adapter"]}}]}
    assert C.settings_diff(g, fi) is None and C.install_state(g, fi) == "installed"
    g["build"]["superseded"] = {}
    assert C.settings_diff(g, fi) == (["adapter.haptic_fix"], [])
