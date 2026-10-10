"""SteamVR overlay apps (fpsVR, wrist watches): detection, the pcvr.vr_overlay patch, the agent's registration with
SteamVR (agent v75; IVRApplications faked) and what overlay apps skip."""
import json
import os
import shutil
import subprocess
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
    return apps


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
                              "registered": False, "autostart": False, "error": "steamvr"}
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
