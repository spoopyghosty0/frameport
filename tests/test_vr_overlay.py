"""SteamVR overlay apps (fpsVR, wrist watches): detection, the pcvr.vr_overlay patch, the agent's registration with
SteamVR (agent v75; IVRApplications faked) and what overlay apps skip."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from test_drives import linux_install, load

from frameport.analysis import linux, vroverlay
from frameport.core.models import Analysis, Recipe

MANIFEST = {"source": "builtin", "applications": [{
    "app_key": "temporalreality.overlay", "launch_type": "binary", "binary_path_linux": "TemporalReality",
    "is_dashboard_overlay": True, "image_path": "assets/icon.png", "arguments": "--quiet",
    "strings": {"en_us": {"name": "Temporal Reality", "description": "A watch."}}}]}


class FakeApps:
    """SteamVRApps stand-in: records manifests, auto-launch flags and launches."""

    def __init__(self):
        self.paths, self.auto, self.launched = [], {}, []

    def _keys(self):
        keys = set()
        for p in self.paths:
            keys |= {app["app_key"] for app in json.load(open(p))["applications"]}
        return keys

    def add(self, path):
        self.paths.append(path)

    def remove(self, path):
        self.paths = [p for p in self.paths if p != path]

    def installed(self, key):
        return key in self._keys()

    def set_autolaunch(self, key, on):
        self.auto[key] = on

    def autolaunch(self, key):
        return self.auto.get(key, False)

    def launch(self, key):
        self.launched.append(key)

    def pid(self, key):
        return 4242 if key in self.launched else 0

    def close(self):
        pass


def fake_steamvr(monkeypatch, a, running=True):
    apps = FakeApps()
    monkeypatch.setattr(a.SteamVRApps, "open", classmethod(lambda cls: apps if running else None))
    fake_systemctl(monkeypatch, a)
    return apps


class Systemctl(list):
    """`systemctl --user` calls recorded (never the host's own user manager); enable --now / restart make the unit
    active, disable --now inactive."""
    active = False

    def __call__(self, *args):
        self.append(args)
        if args[0] in ("restart", "enable") and (args[0] == "restart" or "--now" in args):
            self.active = True
        if args[0] == "disable":
            self.active = False
        code = 0 if args[0] != "is-active" or self.active else 3
        return subprocess.CompletedProcess(args, code, "", "")


def fake_systemctl(monkeypatch, a):
    calls = getattr(a, "_fake_systemctl", None)
    if calls is None:
        calls = a._fake_systemctl = Systemctl()
        monkeypatch.setattr(a, "user_systemctl", calls)
    return calls


# ------------------------------------------------------------------------------------------ detection
def test_bundled_manifest_marks_an_overlay_app(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "watch.vrmanifest").write_text(json.dumps(MANIFEST))
    found = vroverlay.detect(tmp_path, tmp_path / "app" / "Watch", b"", openvr=False)
    assert found["vr_overlay"] and "watch.vrmanifest" in found["vr_overlay_from"]
    assert found["vr_overlay_app"] == {"app": {k: MANIFEST["applications"][0][k] for k in vroverlay.APP_FIELDS},
                                       "manifest": "app/watch.vrmanifest", "manifest_dir": "app"}
    # a scene app's manifest isn't an overlay
    scene = {"applications": [{"app_key": "game", "launch_type": "binary"}]}
    (tmp_path / "app" / "watch.vrmanifest").write_text(json.dumps(scene))
    assert not vroverlay.detect(tmp_path, tmp_path / "app" / "Watch", b"", openvr=False)["vr_overlay"]


def test_overlay_heuristic_needs_openvr_overlay_and_self_registration(tmp_path):
    exe = tmp_path / "fpsVR.exe"
    code = "IVROverlay_027".encode("utf-16-le") + b"\0" + "vrmanifest".encode("utf-16-le")  # a C# app's strings
    assert vroverlay.detect(tmp_path, exe, code, openvr=True)["vr_overlay"]
    assert not vroverlay.detect(tmp_path, exe, code, openvr=False)["vr_overlay"]
    assert not vroverlay.detect(tmp_path, exe, code, openvr=True, engine="Unity")["vr_overlay"]  # games use overlays
    assert not vroverlay.detect(tmp_path, exe, b"IVROverlay_027 IVRCompositor_028", openvr=True)["vr_overlay"]


def test_linux_app_with_overlay_manifest(tmp_path):
    root = tmp_path / "Watch-1.0-linux-arm64"
    root.mkdir()
    shutil.copy("/bin/true", root / "Watch")
    (root / "watch.vrmanifest").write_text(json.dumps(MANIFEST))
    info = linux.inspect(root)
    assert info["vr_overlay"] and info["vr_overlay_app"]["app"]["app_key"] == "temporalreality.overlay"


def test_pcvr_patch_and_install_arguments():
    from frameport import pipeline
    from frameport.patches import base

    patch = base.get("pcvr.vr_overlay")
    extra = {"kind": "rift", "vr_overlay": True, "vr_overlay_from": "x declares a SteamVR dashboard overlay",
             "vr_overlay_app": {"app": {"app_key": "k.overlay"}, "manifest": "m.vrmanifest", "manifest_dir": "."}}
    a = Analysis("rift.watch", "", "Watch", ["x86_64"], "Other", "OpenVR", "D3D11", False, [], None, False, [],
                 False, 0, False, False, False, extra=extra)
    s = patch.detect(a)
    assert s.recommended and s.params == {"autostart": True} and patch.applies(a)
    recipe = Recipe(package="rift.watch", title="Watch", patches={"pcvr.vr_overlay": {"autostart": False}})
    entry = {"kind": "rift", "analysis": {"extra": extra}}
    assert pipeline.rift_overlay(entry, recipe) == {"autostart": False, "app": {"app_key": "k.overlay"},
                                                    "manifest_dir": "."}
    assert pipeline.rift_overlay(entry, Recipe(package="rift.watch", title="Watch")) is None
    linux_entry = {"kind": "linux", "analysis": {"extra": extra}}
    assert pipeline.linux_overlay(linux_entry)["autostart"] is True
    assert pipeline.linux_overlay({**linux_entry, "vr_overlay_autostart": False})["autostart"] is False
    assert pipeline.linux_overlay({**linux_entry, "vr_overlay": False}) is None


# ------------------------------------------------------------------------------------------ agent
def overlay_arg(autostart=True):
    return {"autostart": autostart, "app": MANIFEST["applications"][0], "manifest_dir": "bin"}


def test_linux_overlay_install_registers_with_steamvr(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    apps = fake_steamvr(monkeypatch, a)
    prep, res = linux_install(a, tmp_path, overlay=overlay_arg())
    anchor = Path(prep["anchor"])
    assert res["overlay"]["registered"] and res["overlay"]["autostart"]
    assert res["overlay"]["key"] == "temporalreality.overlay" and apps.auto == {"temporalreality.overlay": True}
    man = json.loads((anchor / a.OVERLAY_MANIFEST).read_text())["applications"][0]
    # SteamVR on the Frame reads binary_path_linux_arm only; the binary is FramePort's launcher
    assert man["binary_path_linux_arm"] == man["binary_path_linux"] == str(anchor / "launch.sh")
    assert man["is_dashboard_overlay"] and man["arguments"] == "--quiet"
    assert man["image_path"] == str(anchor / "artwork" / "icon.png")  # the app's own image isn't there
    assert man["strings"]["en_us"]["name"] == "Temporal Reality"
    dep = a.deployment("linux.true")
    assert dep["overlay"]["key"] == "temporalreality.overlay" and dep["overlay"]["registered"]
    text = (anchor / "launch.sh").read_text()
    assert "export FRAMEPORT_OVERLAY=1" in text  # started by SteamVR: no Steam-parent watchdog
    assert subprocess.run(["bash", "-n", str(anchor / "launch.sh")]).returncode == 0
    # no launch test, and it doesn't count as a game being played
    assert a.cmd_launch_test({"package": "linux.true"})["state"] == "SKIPPED"
    monkeypatch.setattr(a, "pcvr_pids", lambda base: ["123"])
    monkeypatch.setattr(a, "run", lambda *x, **k: subprocess.CompletedProcess(x, 0, "", ""))
    assert a.game_running() is False
    monkeypatch.setattr(a, "pcvr_pids", lambda base: [])
    # started through SteamVR
    assert a.cmd_launch_vr_overlay({"package": "linux.true"}) == {"started": True, "pid": 4242, "error": None}
    # uninstall: SteamVR forgets it
    a.cmd_uninstall({"package": "linux.true", "keep_data": False})
    assert apps.paths == [] and apps.auto["temporalreality.overlay"] is False


def test_overlay_app_image_from_its_files(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    fake_steamvr(monkeypatch, a)
    prep, _ = linux_install(a, tmp_path)
    img = Path(prep["base"]) / "app" / "bin" / "assets" / "icon.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"png")
    out = a.cmd_register_vr_overlay({"package": "linux.true", "autostart": False, "app": MANIFEST["applications"][0],
                                     "manifest_dir": "bin"})
    man = json.load(open(out["manifest"]))["applications"][0]
    assert man["image_path"] == str(img) and not out["autostart"]
    # turning it off (a reinstall without overlay) unregisters it
    assert a.finalize_overlay("linux.true", None) is None
    assert "overlay" not in a.deployment("linux.true")
    assert not os.path.exists(out["manifest"])


def test_overlay_registered_later_when_steamvr_was_off(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    fake_steamvr(monkeypatch, a, running=False)
    _, res = linux_install(a, tmp_path, overlay=overlay_arg())
    assert res["overlay"] == {"key": "temporalreality.overlay", "manifest": res["overlay"]["manifest"],
                              "registered": False, "autostart": False, "error": "steamvr",
                              "service": "installed"}  # FramePort's own autostart doesn't need SteamVR now
    apps = fake_steamvr(monkeypatch, a)
    assert a.overlay_registrations() == ["linux.true"]
    assert apps.installed("temporalreality.overlay") and a.deployment("linux.true")["overlay"]["registered"]
    assert a.overlay_registrations() == []  # nothing to do the next time


def test_pcvr_overlay_install(monkeypatch, tmp_path):
    from test_agent import fake_steam_tools

    a = load(monkeypatch, tmp_path)
    fake_steam_tools(a, Path(a.HOME))
    apps = fake_steamvr(monkeypatch, a)
    prep = a.cmd_prepare_pcvr({"package": "rift.fpsvr", "title": "fpsVR"})
    (Path(prep["incoming"]) / "game/fpsVR.exe").write_bytes(b"MZexe")
    res = a.cmd_finalize_pcvr({"package": "rift.fpsvr", "title": "fpsVR", "exe": "fpsVR.exe", "revive": False,
                               "manifests": {"game": {"fpsVR.exe": 5}}, "overlay": {"autostart": False}})
    assert res["overlay"]["registered"] and res["overlay"]["key"] == "frameport.rift.fpsvr"
    assert apps.auto == {"frameport.rift.fpsvr": False}
    man = json.loads((Path(prep["anchor"]) / a.OVERLAY_MANIFEST).read_text())["applications"][0]
    assert man["binary_path_linux_arm"].endswith("/rift.fpsvr/launch.sh")
    assert man["strings"]["en_us"]["name"] == "fpsVR"
    # autostart off: no autostart service
    assert not os.path.exists(a.VR_OVERLAY_UNIT_PATH) and res["overlay"]["service"] is None


# ------------------------------------------------------------------------------------------ autostart service (v76)
def test_autostart_service_follows_the_overlay_flags(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    fake_steamvr(monkeypatch, a)
    calls = fake_systemctl(monkeypatch, a)
    unit = Path(a.VR_OVERLAY_UNIT_PATH)
    assert a.ensure_overlay_service() is None and not unit.exists() and calls == []  # no overlay apps: nothing
    _, res = linux_install(a, tmp_path, overlay=overlay_arg(autostart=True))
    assert res["overlay"]["service"] == "installed"
    text = unit.read_text()
    assert f'"{os.path.abspath(a.__file__)}" _vr_overlay_watch' in text  # systemd quoting (a path with spaces)
    assert a.systemd_quote("/usr/bin/python3") == "/usr/bin/python3" and a.systemd_quote("a%b c") == '"a%%b c"'
    assert "Restart=always" in text and "WantedBy=default.target" in text
    assert ("enable", "frameport-vr-overlays.service") in calls and calls.active
    assert a.ensure_overlay_service() is None  # unchanged and running
    # the game page's switch off: the service goes (no other overlay app has autostart)
    out = a.cmd_register_vr_overlay({"package": "linux.true", "autostart": False})
    assert out["service"] == "removed" and not unit.exists() and not calls.active
    assert not any("overlay" in c for c in a.ensure_host_fixes())  # stays removed
    a.cmd_register_vr_overlay({"package": "linux.true", "autostart": True})
    assert unit.exists() and calls.active
    # kill switch
    Path(a.VR_OVERLAY_DISABLED).parent.mkdir(parents=True, exist_ok=True)
    Path(a.VR_OVERLAY_DISABLED).write_text("")
    assert "overlay autostart service removed" in a.ensure_host_fixes() and not unit.exists()
    Path(a.VR_OVERLAY_DISABLED).unlink()
    assert "overlay autostart service installed" in a.ensure_host_fixes()
    # uninstalling the last overlay app removes it
    a.cmd_uninstall({"package": "linux.true", "keep_data": False})
    assert not unit.exists() and not calls.active


def test_new_steamvr_starts_autostart_overlays_once(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    apps = fake_steamvr(monkeypatch, a)
    linux_install(a, tmp_path, overlay=overlay_arg(autostart=True))
    monkeypatch.setattr(a, "VR_OVERLAY_GRACE", 0)
    monkeypatch.setattr(a, "run_overlay_round", a.overlay_autostart_round)  # in-process (no child agent)
    running = []
    monkeypatch.setattr(a, "pcvr_pids", lambda base: list(running))
    vrserver = ["100:5"]
    monkeypatch.setattr(a, "vrserver_process", lambda known=None: vrserver[0])
    state = {}
    # a new vrserver (the watcher's first look counts too): the app isn't running → started once
    assert a.overlay_watch_tick(state) == {"linux.true": "started"}
    assert apps.launched == ["temporalreality.overlay"]
    assert a.overlay_watch_tick(state) is None and len(apps.launched) == 1  # same SteamVR: nothing more
    # a restarted watcher (agent update) remembers the SteamVR it handled
    assert json.load(open(a.VR_OVERLAY_STATE))["vrserver"] == "100:5"
    # SteamVR restarted, the app still (or already) runs: not started a second time
    vrserver[0], running[:] = "200:9", ["555"]
    assert a.overlay_watch_tick(state) == {"linux.true": "running"} and len(apps.launched) == 1
    # SteamVR knows its process (it started the app itself): skipped too
    running.clear()
    monkeypatch.setattr(apps, "pid", lambda key: 777)
    vrserver[0] = "250:1"
    assert a.overlay_watch_tick(state) == {"linux.true": "running"} and len(apps.launched) == 1
    monkeypatch.setattr(apps, "pid", lambda key: 4242 if key in apps.launched else 0)
    # autostart off: a new SteamVR starts nothing
    a.cmd_register_vr_overlay({"package": "linux.true", "autostart": False})
    vrserver[0] = "300:1"
    assert a.overlay_watch_tick(state) is None and len(apps.launched) == 1
    # no SteamVR: nothing
    a.cmd_register_vr_overlay({"package": "linux.true", "autostart": True})
    vrserver[0] = None
    assert a.overlay_watch_tick(state) is None
    # kill switch
    monkeypatch.setenv("FRAMEPORT_NO_OVERLAY_AUTOSTART", "1")
    vrserver[0] = "400:2"
    assert a.overlay_watch_tick(state) is None and len(apps.launched) == 1
    assert "linux.true: started" in open(a.VR_OVERLAY_LOG).read()


def test_overlay_log_is_capped(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "VR_OVERLAY_LOG_MAX", 200)
    for i in range(40):
        a.overlay_log(f"line {i}")
    assert os.path.getsize(a.VR_OVERLAY_LOG) <= 240 and os.path.getsize(a.VR_OVERLAY_LOG + ".1") <= 240


def test_vrserver_found_in_proc(monkeypatch, tmp_path):
    a = load(monkeypatch, tmp_path)
    found = a.vrserver_process()  # the test host has no SteamVR (or a real one: "<pid>:<start>")
    assert found is None or found.split(":")[0].isdigit()
    assert a.vrserver_ident(os.getpid()) is None  # pytest isn't a vrserver
    vr = tmp_path / "vrserver"
    os.symlink("/bin/sleep", vr)
    p = subprocess.Popen([str(vr), "30"])
    try:
        for _ in range(50):
            ident = a.vrserver_ident(p.pid)
            if ident:
                break
            time.sleep(0.05)
        assert ident and ident.startswith(f"{p.pid}:") and ident.split(":")[1].isdigit()
        assert a.vrserver_process(ident) == ident  # the known one is checked first
        assert a.vrserver_process("1:0") is not None
    finally:
        p.kill()
        p.wait()
