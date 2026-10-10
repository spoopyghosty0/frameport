"""Quest games on this PC through AXRB (tools/axrb.py, targets/pc_android.py) against fakes; no Windows needed."""
import hashlib
import io
import json
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest
from test_pc_target import fake_steam, vdf_mod

from frameport.core import winhost
from frameport.core.events import Reporter
from frameport.core.models import Recipe
from frameport.tools import axrb


def make_asar(path: Path, files: dict[str, bytes]) -> None:
    """A minimal Electron asar: pickle sizes + JSON index + file data."""
    index, blob, offset = {"files": {}}, b"", 0
    for name, data in files.items():
        node = index
        *dirs, leaf = name.split("/")
        for d in dirs:
            node = node["files"].setdefault(d, {"files": {}})
        node["files"][leaf] = {"size": len(data), "offset": str(offset)}
        blob += data
        offset += len(data)
    js = json.dumps(index).encode()
    payload = struct.pack("<I", len(js)) + js
    payload += b"\0" * (-len(payload) % 4)
    header = struct.pack("<I", len(payload)) + payload
    path.write_bytes(struct.pack("<I", 4) + struct.pack("<I", len(header)) + header + blob)


def fake_app(tmp_path, comps) -> Path:
    app = tmp_path / "Programs" / "axrb-launcher"
    res = app / "resources" / "runtime"
    (res / "out/android/runtime-arm64-v8a").mkdir(parents=True)
    (res / axrb.RUNTIME_APK).write_bytes(b"runtime-apk")
    (app / "AXRB.exe").write_bytes(b"MZ")
    make_asar(app / "resources" / "app.asar", {"package.json": json.dumps({"version": "1.0.4"}).encode(),
                                                "core/components.json": json.dumps(comps).encode()})
    return app


def zip_with(prefix: str, probe: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{prefix}/{probe}", b"x")
    return buf.getvalue()


def component(cid, prefix, probe, dest):
    data = zip_with(prefix, probe)
    return {"id": cid, "name": cid, "url": f"{axrb.DOWNLOAD_HOST}{cid}.zip", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "destination": dest, "prefix": prefix, "probe": probe}, data


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(winhost, "available", lambda: True)
    monkeypatch.setattr(winhost, "is_wsl", lambda: False)
    monkeypatch.setattr(winhost, "to_windows", lambda p: "W:" + str(p).replace("/", "\\"))


def test_asar_read_and_version(tmp_path):
    app = fake_app(tmp_path, [])
    assert axrb.installed_version(app) == "1.0.4"
    assert axrb.asar_read(app / "resources" / "app.asar", "core/components.json") == b"[]"
    with pytest.raises(KeyError):
        axrb.asar_read(app / "resources" / "app.asar", "core/missing.json")


def test_components_only_from_googles_repository(tmp_path):
    good, _ = component("emulator", "emulator", "emulator.exe", "emulator")
    assert axrb.components(fake_app(tmp_path, [good]))[0]["id"] == "emulator"
    bad = dict(good, url="https://example.com/emulator.zip")
    with pytest.raises(axrb.AxrbError):
        axrb.components(fake_app(tmp_path / "b", [bad]))


def test_setup_files_writes_axrbs_layout(tmp_path, monkeypatch, win):
    emu, emu_zip = component("emulator", "emulator", "emulator.exe", "emulator")
    img, img_zip = component("image", "x86_64", "system.img", "system-images/android-36/google_apis/x86_64")
    app = fake_app(tmp_path, [emu, img])
    root = tmp_path / "AXRB Runtime"
    blobs = {emu["url"]: emu_zip, img["url"]: img_zip}

    def download(url, dest, progress=None, expected_sha256=None, expected_sha1=None):
        assert hashlib.sha256(blobs[url]).hexdigest() == expected_sha256
        dest.write_bytes(blobs[url])
        if progress:
            progress(1.0)
        return dest
    monkeypatch.setattr(axrb.cache, "download", download)
    st = axrb.setup_state(root, app)
    assert st["components"] == ["emulator", "image"] and not st["ready"]
    seen = []
    axrb.setup_files(lambda f, name: seen.append(name), root=root, app=app)
    assert (root / "sdk/emulator/emulator.exe").exists()
    assert (root / "sdk/system-images/android-36/google_apis/x86_64/system.img").exists()
    cfg = (root / "avd" / f"{axrb.AVD}.avd" / "config.ini").read_text()
    assert "hw.gpu.mode=host" in cfg and "image.sysdir.1=W:" in cfg and cfg.rstrip().endswith("PlayStore.enabled=no")
    assert f"path=W:{str(root).replace('/', chr(92))}\\avd\\{axrb.AVD}.avd" in \
        (root / "avd" / f"{axrb.AVD}.ini").read_text()
    assert json.loads((root / "license-acceptance.json").read_text())["license"] == "android-sdk-license"
    assert not list((root / "downloads").iterdir()) and seen
    st = axrb.setup_state(root, app)
    assert st["components"] == [] and st["avd"] and not st["runtime_apk"]  # the runtime APK goes in after boot
    (root / "ready.json").write_text(json.dumps({"runtimeHash": axrb.runtime_hash(app)}))
    assert axrb.setup_state(root, app)["ready"]


def test_ps_command_sets_axrbs_environment(tmp_path, win):
    app = fake_app(tmp_path, [])
    cmd = axrb.ps_command("scripts/emulator/windows_android_emulator.ps1",
                          {"Action": "Start", "Port": 5584, "GpuSharing": True, "ColdBoot": False},
                          tmp_path / "AXRB Runtime", app)
    assert "$env:ANDROID_ADB_SERVER_PORT = '5038'" in cmd and "$env:AXRB_DATA_HOME = 'W:" in cmd
    assert "tools\\python' + ';' + $env:PATH" in cmd
    assert "-Action 'Start' -Port '5584' -GpuSharing" in cmd and "ColdBoot" not in cmd
    assert cmd.endswith("exit $LASTEXITCODE")
    assert axrb._ps_quote("it's") == "'it''s'"


def test_launcher_script_and_steam_arguments(tmp_path, win):
    app = fake_app(tmp_path, [])
    text = axrb.launcher_text(tmp_path / "AXRB Runtime", app)
    assert "run_windows_game.ps1" in text and f"-Avd '{axrb.AVD}' -Port {axrb.PORT}" in text
    assert "@" not in text.replace("@{", "")  # every placeholder filled
    args = axrb.launcher_args(r"C:\x\fp-axrb-run.ps1", "com.a.b", "com.a.b/.Main", 'Say "Hi" $now')
    assert '-File "C:\\x\\fp-axrb-run.ps1" -Package com.a.b -Activity com.a.b/.Main -GameName "Say Hi now"' in args


def test_read_log_handles_powershell_utf16(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"\xff\xfe" + "Host failed to start: x\n".encode("utf-16-le"))
    assert axrb.read_log(p) == "Host failed to start: x\n"
    p.write_bytes("﻿plain".encode())
    assert axrb.read_log(p, 3) == "ain"
    assert axrb.read_log(tmp_path / "missing.log") == ""


def test_logged_call_reports_its_exit_code(tmp_path, win):
    cmd = axrb.logged("& 'x.ps1' -A 1", r"C:\l.log")
    assert "*>> 'C:\\l.log'" in cmd and '"FP_EXIT $c"' in cmd and cmd.endswith("exit $c")
    log = tmp_path / "l.log"
    log.write_bytes(b"\xff\xfe" + "output\nFP_EXIT 4\n".encode("utf-16-le"))
    assert axrb.exit_code(log) == 4
    log.write_text("no marker")
    assert axrb.exit_code(log) is None


def test_release_sums_and_requirements():
    sums = axrb.parse_sums("310d" + "0" * 60 + "  AXRB-Setup-1.0.4.exe\nnot a line\n")
    assert sums == {"AXRB-Setup-1.0.4.exe": "310d" + "0" * 60}
    ok = {"hypervisor": True, "supportedGpu": True, "x64": True, "memoryGB": 64, "gpu": "RTX"}
    assert axrb.requirement_problems(ok) == []
    bad = axrb.requirement_problems(dict(ok, hypervisor=False, memoryGB=8))
    assert len(bad) == 2 and "Hypervisor" in bad[1] and "12 GB" in bad[0]


def test_pc_selection_keeps_ovrport_and_apk_fixes_only():
    from frameport import pipeline
    from frameport.patches.base import pc_selection

    patches = {"patch_oculus_unity": {}, "frame.adapter": {}, "frame.ovrstubs": {}, "frame.swapchain_limit": {},
               "adapter.focus_hold": {"ms": 5000}, "frame.vk_sanitize": {}}
    assert set(pc_selection(patches)) == {"patch_oculus_unity", "frame.ovrstubs", "frame.swapchain_limit"}
    r1 = {"patches": patches}
    r2 = {"patches": dict(patches, **{"adapter.scale": {"value": 0.8}})}  # a Frame-only change
    assert pipeline.pc_recipe_fingerprint(r1) == pipeline.pc_recipe_fingerprint(r2)
    assert pipeline.recipe_fingerprint(r1) != pipeline.recipe_fingerprint(r2)


def test_pc_installable():
    from frameport.pipeline import pc_installable

    assert pc_installable({"kind": "rift"})
    assert pc_installable({"analysis": {"extra": {"vr_kind": "quest"}}})
    assert pc_installable({"analysis": {"extra": {"vr_kind": "openxr"}}})
    assert pc_installable({})  # older entries without vr_kind are Quest games
    assert not pc_installable({"analysis": {"extra": {"vr_kind": "none"}}})
    assert not pc_installable({"kind": "linux"})


def test_shared_exe_shortcuts_are_matched_by_package(tmp_path):
    m = vdf_mod()
    vdf = str(tmp_path / "shortcuts.vdf")
    exe = '"C:\\Windows\\powershell.exe"'
    a = m.upsert_shortcut(vdf, exe, "Game A", "", "", "FramePort PC VR", "-Package com.a -Activity x",
                          options_key="-Package com.a ")
    b = m.upsert_shortcut(vdf, exe, "Game B", "", "", "FramePort PC VR", "-Package com.b -Activity y",
                          options_key="-Package com.b ")
    again = m.upsert_shortcut(vdf, exe, "Game A (renamed)", "", "", "FramePort PC VR", "-Package com.a -Activity x",
                              options_key="-Package com.a ")
    sc = m.vdf_decode(Path(vdf).read_bytes())["shortcuts"]
    assert a != b and again == a and len(sc) == 2
    assert m.remove_shortcut(vdf, exe, "-Package com.a ")
    left = list(m.vdf_decode(Path(vdf).read_bytes())["shortcuts"].values())
    assert [v["appname"] for v in left] == ["Game B"]


class FakeAdb:
    def __init__(self, remote=None):
        self.calls, self.remote = [], remote or {}

    def __call__(self, args, timeout=60, serial=True, root=None):
        self.calls.append(args)
        out = "Success\n"
        if args[:2] == ["shell", "cmd"]:
            out = "priority=0 preferredOrder=0\ncom.game.x/com.unity3d.player.UnityPlayerActivity\n"
        elif args[0] == "shell" and "find . -type f" in args[1]:
            out = "".join(f"{s} ./{n}\n" for n, s in self.remote.items())
        return subprocess.CompletedProcess(args, 0, out, "")


def test_install_into_axrb(tmp_path, monkeypatch, win):
    from frameport.targets import pc_android

    apk = tmp_path / "out" / "com.game.x.apk"
    apk.parent.mkdir()
    apk.write_bytes(b"apk")
    data = tmp_path / "obb"
    data.mkdir()
    (data / "main.1.com.game.x.obb").write_bytes(b"1" * 10)
    (data / "patch.1.com.game.x.obb").write_bytes(b"2" * 5)
    adb = FakeAdb(remote={"main.1.com.game.x.obb": 10})  # already there from an earlier install
    events = []
    monkeypatch.setattr(pc_android, "ensure_axrb", lambda rep: tmp_path / "app")
    monkeypatch.setattr(axrb, "start_emulator", lambda **k: events.append("start") or True)
    monkeypatch.setattr(axrb, "stop_emulator", lambda *a, **k: events.append("stop"))
    monkeypatch.setattr(axrb, "ensure_runtime_apk", lambda **k: events.append("runtime"))
    monkeypatch.setattr(axrb, "installed_version", lambda app=None: "1.0.4")
    monkeypatch.setattr(axrb, "write_launcher", lambda **k: tmp_path / "FramePort/axrb/fp-axrb-run.ps1")
    monkeypatch.setattr(axrb, "adb", adb)
    monkeypatch.setattr(pc_android, "powershell_win", lambda: r"C:\Windows\powershell.exe")
    recipe = Recipe("com.game.x", {"patch_oculus_unity": {}, "frame.adapter": {}, "frame.ovrstubs": {}})
    res = pc_android.install("com.game.x", "Game X", apk, data, recipe, Reporter(), record_dir=tmp_path / "rec",
                             appid=lambda exe, title: 0x80001234)
    assert res == {"ok": True, "appid": 0x80001234, "activity": "com.game.x/com.unity3d.player.UnityPlayerActivity"}
    assert events == ["start", "runtime", "stop"]
    pushes = [c for c in adb.calls if c[0] == "push"]
    assert len(pushes) == 1 and pushes[0][2] == "/sdcard/Android/obb/com.game.x/patch.1.com.game.x.obb"
    dep = json.loads((tmp_path / "rec" / "deployment.json").read_text())
    assert dep["kind"] == "android" and dep["recipe"]["patches"] == ["frame.ovrstubs", "patch_oculus_unity"]
    exe, start, opts = pc_android.shortcut_fields(dep)
    assert exe == '"C:\\Windows\\powershell.exe"' and "-Package com.game.x " in opts
    assert start.endswith('axrb\\"')


def test_signature_conflict_explained():
    from frameport.targets import pc_android

    r = subprocess.CompletedProcess([], 1, "", "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: ...]")
    with pytest.raises(RuntimeError, match="signed with another key"):
        pc_android._adb_ok(r, "installing the game")


def test_two_quest_games_get_their_own_pc_shortcuts(tmp_path, monkeypatch, win):
    from frameport.targets import pc_revive

    root = fake_steam(tmp_path)
    monkeypatch.setattr(winhost, "env_path", lambda name: None)
    monkeypatch.setattr(winhost, "steam_root", lambda: root)
    monkeypatch.setattr(winhost, "stop_steam", lambda r: True)
    monkeypatch.setattr(winhost, "start_steam", lambda r: None)
    monkeypatch.setattr("frameport.artwork.fetch.cache.http_get", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    for pkg, title in (("com.a.game", "Game A"), ("com.b.game", "Game B")):
        d = pc_revive.pc_dir() / pkg
        d.mkdir(parents=True)
        (d / "deployment.json").write_text(json.dumps({
            "package": pkg, "kind": "android", "title": title, "activity": f"{pkg}/.Main",
            "launcher_win": r"C:\Users\u\AppData\Local\FramePort\axrb\fp-axrb-run.ps1",
            "powershell_win": r"C:\Windows\powershell.exe"}))
    t = pc_revive.PcReviveTarget()
    assert {d["package"] for d in t.installed()} == {"com.a.game", "com.b.game"}
    t.add_to_library(["com.a.game", "com.b.game"], Reporter())
    sc = vdf_mod().vdf_decode((root / "userdata/12345/config/shortcuts.vdf").read_bytes())["shortcuts"]
    assert sorted(v["appname"] for v in sc.values()) == ["Game A", "Game B"]
    assert all(v["tags"]["0"] == pc_revive.TAG for v in sc.values())


def test_pc_outdated_for_android_follows_the_pc_build():
    from frameport.ui import components as C

    dep = {"kind": "android", "recipe": {"patches": ["frame.ovrstubs", "patch_oculus_unity"]}}
    g = {"recipe": {"patches": {"patch_oculus_unity": {}, "frame.ovrstubs": {}, "frame.adapter": {}}}}
    assert not C.pc_outdated(g, dep)  # Frame-only patches don't matter on the PC
    g["recipe"]["patches"]["patch_oculus_unreal"] = {}
    assert C.pc_outdated(g, dep)


def test_axrb_triage_uses_its_own_signatures():
    from frameport.validate.triage import triage

    host = ("AXRB OpenXR: adjacent loader \"openxr_loader.dll\": loaded (Windows error 0)\n"
            "AXRB OpenXR: failed to find D3D11 adapter requested by OpenXR runtime\n"
            "Host pose server did not become ready within 45 seconds.\n")
    r = triage(host, "NEVER_STARTED", "com.zenstudios.PFXVRQuest", kind="axrb")
    assert {f.id for f in r.findings} == {"axrb-no-headset", "axrb-host-not-ready"}
    guest = ("I OpenXR-Loader: Got runtime: package: com.axrb.openxrruntime, so filename: libopenxr_runtime.so\n"
             "I OVRPlugin: Preinitialize: xrCreateInstance() succeeded\n"
             "F DEBUG   :       #00 pc 000000000006fb32  /vendor/lib64/hw/vulkan.ranchu.so "
             "(gfxstream::vk::ResourceTracker::on_vkAllocateMemory(void*)+802)\n")
    r = triage(guest, "EXITED", "com.zenstudios.PFXVRQuest", kind="axrb")
    assert [f.id for f in r.findings] == ["axrb-gfxstream-crash"] and r.findings[0].report
    assert r.milestone == "OpenXR instance created"
    assert not [f for f in triage(host, "EXITED", "com.zenstudios.PFXVRQuest").findings if f.id.startswith("axrb")]
