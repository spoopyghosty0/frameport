"""Triage of real play sessions: the agent's session_log (v70), the new signatures, the frame-rate and focus-dip
findings, value suggestions and applying FrameBridge settings without a rebuild."""
import os
import time

from test_agent import load_agent

from frameport import pipeline
from frameport.core import library
from frameport.validate import session
from frameport.validate.triage import split_suggestion, triage

PKG = "com.example.game"
START = "09-28 17:39:01.000  1000  1000 I ActivityManager: Start proc 1147:com.example.game/u0a55 for activity\n"


def fb(t: str, msg: str) -> str:
    return f"09-28 {t}.000  1147  1174 I FrameBridge: {msg}\n"


def pacing(fps_list, start_min=40):
    out = []
    for i, fps in enumerate(fps_list):
        s = 5 * i
        out.append(fb(f"17:{start_min + s // 60:02d}:{s % 60:02d}",
                      f"pacing: {fps:.1f} fps, displayTime vs predicted: avg 0.10 ms, max {2 + i % 3:.2f} ms"))
    return "".join(out)


def add_game(patches=None, engine="Unity", libs=("libil2cpp.so", "libunity.so", "libOVRPlugin.so")):
    library.upsert_game(PKG, title="Example", analysis={"package": PKG, "engine": engine, "graphics": "GLES3",
                                                       "libs": list(libs), "abis": ["arm64-v8a"]},
                        recipe={"package": PKG, "patches": dict(patches or {})})


# ------------------------------------------------------------------------------------------ agent v70
def test_last_play_marks_launch_tests(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    assert a.last_play(str(anchor)) is None
    (anchor / "plays.log").write_text("start 1000\nend 1500\nstart 2000\nend 2600\n")
    assert a.last_play(str(anchor)) == {"start": 2000, "end": 2600, "test": False}
    with open(anchor / "plays.log", "a") as f:
        f.write("test 3000\nstart 3004\n")
    assert a.last_play(str(anchor)) == {"start": 3004, "end": None, "test": True}  # running, a launch test's
    a.mark_launch_test(str(anchor))
    assert (anchor / "plays.log").read_text().splitlines()[-1].startswith("test ")
    # the screenshot matcher ignores the test marks
    monkeypatch.setattr(a, "ANCHORS", str(tmp_path))
    assert [s[2] for s in a.play_sessions()] == ["anchor"] * 3


def test_session_log_slices_long_logs():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("fa", Path(__file__).resolve().parents[1] / "agent" /
                                                  "frameport_agent.py")
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    noise = "09-28 17:40:00.000  1147  1180 D Unity: lots of chatter here\n" * 3000
    keep = fb("17:40:01", "focus: back after 900 ms (lost to state 4)")
    text = "HEAD\n" + noise + keep + noise + "TAIL\n"
    out, cut = a.slice_session_log(text, 120000)
    assert cut and len(out) <= 120000 and out.startswith("HEAD\n") and out.endswith("TAIL\n")
    assert "focus: back after 900 ms" in out and "bytes in the middle of this session cut" in out
    assert a.slice_session_log("short\n", 1000) == ("short\n", False)


def test_session_log_command(monkeypatch, tmp_path):
    a = load_agent(monkeypatch, tmp_path)
    base = tmp_path / "Applications/quest-frame" / PKG
    base.mkdir(parents=True)
    now = int(time.time())
    (base / "deployment.json").write_text(f'{{"package": "{PKG}", "appid": 123, "base": "{base}"}}')
    out = a.cmd_session_log({"package": PKG})
    assert out["session"] is None and out["log"] is None
    (base / "plays.log").write_text(f"start {now - 600}\nend {now - 10}\n")
    (base / "launch.log").write_text(START + pacing([72] * 3))
    crash = tmp_path / ".local/share/Steam/logs/lepton-logcats/steamlaunch-123/logcat-crash.log"
    crash.parent.mkdir(parents=True)
    crash.write_text("F DEBUG: backtrace:\n")
    os.utime(crash, (now - 600 - 3600,) * 2)  # an older session's crash: not this one's
    calls = []

    class P:
        stdout = "1790000000.0 steamos kernel: msm_dpu ae01000.display-controller: [drm] hangcheck recover!\n" \
                 "1790000001.0 steamos kernel: usb 1-1: new device\n"
    monkeypatch.setattr(a, "run", lambda cmd, **kw: calls.append(cmd) or P())
    out = a.cmd_session_log({"package": PKG})
    assert out["session"] == {"start": now - 600, "end": now - 10, "test": False}
    assert open(out["log"]).read().startswith(START) and out["crash"] == ""
    assert out["kernel"].startswith("kernel: ") and "hangcheck" in out["kernel"] and "usb" not in out["kernel"]
    assert calls[0][:4] == ["journalctl", "-k", "--since", f"@{now - 600}"]
    os.utime(crash, None)
    assert "backtrace" in a.cmd_session_log({"package": PKG})["crash"]
    games = a.cmd_list_installed({})["games"]
    assert games[0]["last_play"]["end"] == now - 10


# ------------------------------------------------------------------------------------------ signatures
def test_space_warp_is_a_question_not_an_automatic_fix():
    log = START + fb("17:39:02", "xrCreateSwapchain 376x376 format=97 samples=1 array=2 faces=1 usage=0x21 "
                                 "flags=0x0 result=0")
    r = triage(log, "RUNNING", PKG)
    f = next(f for f in r.findings if f.id == "space-warp-used")
    assert f.severity == "info" and f.question and f.suggest == ["adapter.hide_space_warp"]
    assert "adapter.hide_space_warp" not in r.suggestions()  # launch tests never apply it on their own
    off = fb("17:39:01", "per-game: hide_space_warp=1 (XR_FB_space_warp hidden, space warp info removed)")
    assert "space-warp-used" not in {f.id for f in triage(off + log, "RUNNING", PKG).findings}


def test_gpu_hang_from_kernel_lines():
    kernel = "kernel: 1790000000.0 steamos kernel: msm_dpu: [drm:a6xx_hangcheck] hangcheck detected gpu lockup\n"
    r = triage(START, "UNKNOWN", PKG, kernel=kernel)
    f = next(f for f in r.findings if f.id == "gpu-hang")
    assert f.report and f.suggest == ["adapter.vk_shader_dump", "adapter.zink_shader_dump"] and f.severity == "fatal"
    assert "gpu-hang" not in {f.id for f in triage(START + "hangcheck in an app's own log\n", "UNKNOWN", PKG).findings}


def test_msrtt_crash_matches_the_find_rp_state_frame():
    crash = "      #00 pc 0000000000912345  /vendor/lib64/dri/libgallium_dri.so (find_rp_state+120)\n"
    assert "unreal-msrtt-crash" in {f.id for f in triage(START, "EXITED", PKG, crash=crash).findings}


def test_split_suggestion():
    assert split_suggestion("adapter.scale=0.85") == ("adapter.scale", "0.85")
    assert split_suggestion("frame.pac_hints") == ("frame.pac_hints", None)


# ------------------------------------------------------------------------------------------ session analysis
def test_slow_session_suggests_a_lower_scale_then_msaa_off():
    add_game({"adapter.scale": {"value": 1.2}})
    log = START + pacing([40, 50] + [72] * 5 + [55] * 10)
    s = session.analyze(log, PKG, library.game(PKG))
    assert s["fps"]["target"] == 72 and s["fps"]["slow_share"] > 0.3
    f = next(f for f in s["findings"] if f["id"] == "slow-frames")
    assert f["suggest"] == ["adapter.scale=1", "frame.unity_runtime_msaa_off"]
    smooth = session.analyze(START + pacing([90.1] * 20), PKG, library.game(PKG))
    assert smooth["fps"]["target"] == 90 and not any(f["id"] == "slow-frames" for f in smooth["findings"])


def test_unreal5_slow_session_suggests_turning_space_warp_off():
    add_game(engine="Unreal", libs=("libUnreal.so", "libOVRPlugin.so"))
    s = session.analyze(START + pacing([60] * 20), PKG, library.game(PKG))
    f = next(f for f in s["findings"] if f["id"] == "slow-frames")
    assert f["suggest"] == ["adapter.scale=0.85", "adapter.hide_space_warp"]


def test_focus_dips_longer_than_the_hold_suggest_a_longer_hold():
    add_game({"adapter.focus_hold_ms": {"value": 1000.0}})
    dips = "".join(fb(f"17:4{i}:00", f"focus: back after {ms} ms (lost to state 4)")
                   for i, ms in enumerate((1600, 1900, 2300, 400, 30000)))
    s = session.analyze(START + dips, PKG, library.game(PKG))
    assert s["focus_dips"] == 5
    f = next(f for f in s["findings"] if f["id"] == "focus-dips")
    assert f["suggest"] == ["adapter.focus_hold_ms=3000"]  # the 30 s one is the headset taken off
    library.upsert_game(PKG, recipe={"package": PKG, "patches": {"adapter.focus_hold": {"value": 0}}})
    f = next(f for f in session.analyze(START + dips, PKG, library.game(PKG))["findings"] if f["id"] == "focus-dips")
    assert f["suggest"] == ["adapter.focus_hold=1"]
    library.upsert_game(PKG, recipe={"package": PKG, "patches": {}})  # default: 5 s hides all of them
    assert not any(f["id"] == "focus-dips" for f in session.analyze(START + dips, PKG, library.game(PKG))["findings"])


# ------------------------------------------------------------------------------------------ pipeline
def test_value_suggestions_are_applied_and_filtered():
    add_game({"adapter.focus_hold_ms": {"value": 3000.0}})
    assert pipeline.useful_suggestions(PKG, ["adapter.focus_hold_ms=3000", "adapter.focus_hold_ms=4000",
                                             "adapter.scale=0.85"]) == ["adapter.focus_hold_ms=4000",
                                                                         "adapter.scale=0.85"]
    r = pipeline.apply_suggestions(PKG, ["adapter.focus_hold_ms=4000", "adapter.scale=0.85", "adapter.sync_guard"])
    assert r.patches["adapter.focus_hold_ms"] == {"value": 4000.0} and r.patches["adapter.scale"] == {"value": 0.85}
    assert r.patches["adapter.sync_guard"] == {"value": 1}


class FakeTarget:
    label = "Frame"

    def __init__(self, res=None):
        self.res, self.pushed = res, []

    def set_settings(self, package, settings):
        self.pushed.append((package, settings))
        return {"settings": settings}

    def session_log(self, package):
        return self.res


def test_adapter_fixes_are_pushed_live_others_need_a_rebuild():
    add_game()
    t = FakeTarget()
    assert pipeline.apply_suggestions_live(PKG, ["adapter.focus_hold_ms=3000"], t)
    assert t.pushed[-1][1]["focus_hold_ms"] == 3000.0 and t.pushed[-1][1]["scale"] == 1.0
    assert not pipeline.apply_suggestions_live(PKG, ["adapter.scale=0.85", "frame.pac_hints"], t)
    assert len(t.pushed) == 1 and "frame.pac_hints" in library.game(PKG)["recipe"]["patches"]


def test_triage_session_stores_the_last_session():
    add_game()
    now = time.time()
    log = START + pacing([72] * 14) + fb("17:41:00", "xrCreateSwapchain 376x376 format=97 samples=1 array=2 "
                                                     "faces=1 usage=0x21 flags=0x0 result=0")
    t = FakeTarget({"session": {"start": now - 900, "end": now - 60, "test": False}, "text": log, "crash": "",
                    "kernel": "", "cut": False})
    s = pipeline.triage_session(PKG, t)
    g = library.game(PKG)
    assert g["last_session"] == s and g["last_session_checked"] == now - 60
    assert [f["id"] for f in session.shown_findings(s)] == ["space-warp-used"]
    assert s["suggestions"] == []  # only a question: nothing is applied without asking
    assert pipeline.sessions_due([{"package": PKG, "last_play": {"start": now - 900, "end": now - 60}}]) == []
    due = pipeline.sessions_due([{"package": PKG, "last_play": {"start": now - 50, "end": now - 5, "test": True}},
                                 {"package": "not.in.library", "last_play": {"start": 1, "end": 2}}])
    assert due == [(PKG, {"start": now - 50, "end": now - 5, "test": True}, False)]  # a launch test: only marked
    assert pipeline.sessions_due([{"package": PKG, "last_play": {"start": now - 50, "end": now - 5}}])[0][2]


# ------------------------------------------------------------------------------------------ game page
def _texts(control) -> list[str]:
    out, stack = [], [control]
    while stack:
        c = stack.pop()
        for attr in ("value", "content", "text"):
            v = getattr(c, attr, None)
            if isinstance(v, str):
                out.append(v)
        for attr in ("content", "controls"):
            v = getattr(c, attr, None)
            if isinstance(v, list):
                stack.extend(v)
            elif v is not None and not isinstance(v, str):
                stack.append(v)
    return out


def test_last_session_callout():
    from types import SimpleNamespace

    from frameport.ui.views.game import GameView

    app = SimpleNamespace(apply_session_fix=print, answer_session_question=print, dismiss_session=print,
                          report_problem_dialog=print)
    view = object.__new__(GameView)
    view.app, view.package = app, PKG

    def finding(id_, severity, suggest=(), question="", report=False, diagnosis="x"):
        return {"id": id_, "severity": severity, "diagnosis": diagnosis, "suggest": list(suggest),
                "question": question, "report": report, "evidence": ""}
    ls = {"ended": time.time() - 120,
          "fps": {"median": 61.5, "target": 72, "slow_share": 0.6, "worst_ms": 3, "windows": 30},
          "findings": [finding("space-warp-used", "info", ["adapter.hide_space_warp"], "Did textures flicker?"),
                       finding("slow-frames", "warning", ["adapter.scale=0.85"]),
                       finding("gpu-hang", "fatal", report=True),
                       finding("dlopen-failed", "info", diagnosis="noise")]}
    view.g = {"package": PKG, "last_session": ls}
    texts = _texts(view.session_callout())
    assert "Did textures flicker?" in texts and "Yes, fix it" in texts and "Try this setting" in texts
    assert "Report a problem…" in texts and "noise" not in texts and any("61.5 fps" in t for t in texts)
    view.g = {"package": PKG, "last_session": {**ls, "applied": ["adapter.scale=0.85"],
                                               "answered": ["space-warp-used"]}}
    texts = _texts(view.session_callout())
    assert "Did textures flicker?" not in texts and "Try this setting" not in texts
    view.g = {"package": PKG, "last_session": {**ls, "dismissed": True}}
    assert view.session_callout() is None
    view.g = {"package": PKG, "last_session": {"findings": [ls["findings"][3]]}}
    assert view.session_callout() is None  # nothing worth showing


def test_suggestions_that_cant_matter_for_the_game_are_dropped():
    """GitHub #138: a Vulkan game (AC Nexus) was offered the GLES-only 360° emulation."""
    add_game()
    library.update_game(PKG, lambda e: e["analysis"].update(graphics="Vulkan"))
    assert pipeline.useful_suggestions(PKG, ["adapter.equirect_emul", "adapter.scale=0.85"]) == ["adapter.scale=0.85"]
    library.update_game(PKG, lambda e: e["analysis"].update(graphics="GLES3"))
    assert pipeline.useful_suggestions(PKG, ["adapter.equirect_emul"]) == ["adapter.equirect_emul"]
