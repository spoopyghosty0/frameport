"""Diagnostics bundles, redaction, GitHub issue links and the issue → catalog script."""
import importlib.util
import json
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from frameport.core import library
from frameport.core.paths import user_data_dir
from frameport.diag import bundle, issue, redact
from frameport.recommend import catalog

ROOT = Path(__file__).resolve().parents[1]

LOG = ("""
09-28 17:39:01.000  1000  1000 I ActivityManager: Start proc 1150:com.example.game/u0a55 for activity
09-28 17:39:02.000  1150  1170 E AndroidRuntime: java.lang.UnsatisfiedLinkError: dlopen failed: cannot locate symbol"""
""" "ovr_User_GetLoggedInUser" referenced by "libgame.so"
connecting to 192.168.1.23 from /home/alice/.local/share/frameport, steam id 76561198012345678
""")


@pytest.fixture(autouse=True)
def _no_pc_steam(monkeypatch):
    monkeypatch.setattr(redact, "pc_steam_user", lambda: None)


# ------------------------------------------------------------------------------------------ redaction
def test_redactor_patterns():
    r = redact.Redactor({"host": ["myframe.local"], "steam-id": ["87654321"], "user": ["alice"]})
    s = r.text("ssh steamos@192.168.1.23 (myframe.local) fe80::1c2b:3d4e:5f60:7a8b mac aa:bb:cc:dd:ee:ff "
               "/home/bob/x /home/steamos/Applications C:\\Users\\Carol\\AppData C:\\\\Users\\\\Dan\\\\x "
               "/mnt/c/Users/Eve/Documents userdata/87654321/config 76561198012345678 me@example.com alice")
    for leak in ("192.168", "myframe", "1c2b", "aa:bb", "bob", "Carol", "Dan", "Eve", "87654321", "7656119",
                 "example.com", "alice"):
        assert leak not in s, (leak, s)
    assert "/home/steamos/Applications" in s  # the Frame's generic user stays (debug value, no identity)
    assert r.summary()["ip"] == 2 and r.summary()["home"] >= 4


def test_redactor_keeps_versions_and_times():
    r = redact.Redactor()
    s = ("overport 1.2.3.4 runtime 3.4.3 at 17:39:01.000 ::1 127.0.0.1 netmask 255.255.255.0 "
         "data/system/dropbox/system_server_wtf@1727712345.txt icon@2x.png")
    assert r.text(s) == s


def test_redactor_objects_and_data_dir(tmp_path):
    r = redact.default({"hostname": "steamframe-7", "steam_users": ["12345678"]})
    out = r.obj({"apk": str(user_data_dir() / "out" / "g.apk"), "list": ["steamframe-7", 12345678, "12345678"]})
    assert out == {"apk": "<data>/out/g.apk", "list": ["<host>", 12345678, "<steam-id>"]}


# ------------------------------------------------------------------------------------------ bundle
def _game(tmp_path):
    apk = tmp_path / "dumps" / "g.apk"
    apk.parent.mkdir()
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr("assets/x.bin", b"x" * 10)
        z.write(ROOT / "tests/fixtures/libfakeovrplugin_arm64.so", "lib/arm64-v8a/libOVRPlugin.so")
    logs = user_data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "com.example.game-20260930-120000.log").write_text(LOG)
    library.upsert_game("com.example.game", title="Example", apk=str(apk), status="issues",
                        analysis={"package": "com.example.game", "version": "1.0", "engine": "Unity", "xr": "OpenXR"},
                        recipe={"package": "com.example.game", "patches": {"frame.ovrstubs": {}, "frame.nodebug": {}}},
                        details={"description": "long store text"},
                        last_test={"state": "EXITED", "verdict": "fail", "milestone": "Process started",
                                   "findings": [{"id": "missing-ovr-symbol", "severity": "fatal",
                                                 "diagnosis": "d", "evidence": "e /home/alice/x"}],
                                   "suggestions": ["frame.ovrstubs"]})
    return library.game("com.example.game")


class FakeTarget:
    kind, label = "frame", "myframe.local"

    def describe(self):
        return {"hostname": "steamframe-7", "steam_users": ["87654321"], "build_id": "20260922", "os": "SteamOS",
                "agent_version": 21}

    def collect_diag(self, package=None):
        if package is None:
            return {"host": {"openxr_runtime": {"name": "SteamVR"}, "podman": "lepton-steamlaunch-1 Exited"},
                    "files": {}}
        return {"installed": True, "files": {"launch.log": LOG + "userdata/87654321/config steamframe-7",
                                             "settings.conf": "scale=1.0"},
                "listing": {"roots": [{"name": "Install folder", "files": [["game.apk", 1]]}], "missing": []}}


def test_bundle_contents_and_no_leaks(tmp_path):
    _game(tmp_path)
    from frameport.core import applog

    applog.save_job_log("install", "com.example.game", "failed", "log from /home/alice/x")
    out = bundle.collect(["com.example.game"], FakeTarget(), dest=tmp_path / "out")
    assert out.name.startswith("FramePort-diag-com.example.game-") and out.suffix == ".zip"
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        d = "games/com.example.game/"
        for n in ("README.md", "manifest.json", "app/settings.json", "frame/info.json", "frame/host.json",
                  d + "entry.json", d + "recipe.yaml", d + "triage.json", d + "package/apk_files.json",
                  d + "package/elf.json", d + "target/launch.log", d + "target/settings.conf",
                  d + "target/files.json"):
            assert n in names, n
        assert any(n.startswith("app/jobs/") for n in names)
        assert any(n.startswith(d + "logs/") for n in names)
        blob = b"".join(z.read(n) for n in names).decode("utf-8", "replace")
        for leak in (str(user_data_dir()), str(tmp_path), "192.168.1.23", "alice", "87654321", "steamframe-7",
                     "76561198012345678", "long store text"):
            assert leak not in blob, leak
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["games"][0]["from_target"] and manifest["env"]["frame"]["build_id"] == "20260922"
        assert manifest["redactions"]["ip"] >= 1
        elf = json.loads(z.read(d + "package/elf.json"))
        assert "lib/arm64-v8a/libOVRPlugin.so" in elf
        assert yaml.safe_load(z.read(d + "recipe.yaml"))["frame"] == ["frame.nodebug"]
    res = bundle.read(out)
    t = res["games"]["com.example.game"]["triage"]
    assert "missing-ovr-symbol" in {f["id"] for f in t["findings"]} and "frame.ovrstubs" in t["suggestions"]


def test_bundle_offline_and_app_only(tmp_path):
    out = bundle.collect(None, None, dest=tmp_path / "x.zip")
    assert out == tmp_path / "x.zip"
    with zipfile.ZipFile(out) as z:
        m = json.loads(z.read("manifest.json"))
    assert m["target"] is None and m["games"] == []


def test_bundle_size_cap():
    w = bundle._Writer(redact.Redactor())
    import os

    w.files["big.log"] = os.urandom(3 << 20).hex().encode()  # incompressible-ish
    w.fit(limit=1 << 20)
    assert len(w.files["big.log"]) < 3 << 20 and w.warnings


# ------------------------------------------------------------------------------------------ issue links
def _fields(url):
    q = parse_qs(urlparse(url).query)
    return {k: v[0] for k, v in q.items()}


def test_working_config_url(tmp_path):
    g = _game(tmp_path)
    e = catalog.entry_from_library(g, status="works", notes="n", verified={"app": "0.2.0"})
    assert e.verified["app"] == "0.2.0" and e.frame == ["frame.nodebug"]
    url = issue.working_config_url(g, catalog.to_yaml(e), "works", "played /home/alice fine" + "x" * 20000,
                                   {"app": "0.2.0", "os": "Linux"}, redact.default())
    assert url.startswith(issue.REPO + "/issues/new?") and len(url) <= issue.MAX_URL + 200
    f = _fields(url)
    assert f["template"] == "working-config.yml" and f["labels"] == "working-config"
    assert yaml.safe_load(f["recipe"])["package"] == "com.example.game"  # the recipe is never cut
    assert "alice" not in url and "[... cut" in f["notes"]


def test_problem_url(tmp_path):
    g = _game(tmp_path)
    url = issue.problem_url(g, "crashes", {"app": "x"}, "FramePort-diag-a.zip", "package: com.example.game\n")
    f = _fields(url)
    assert f["template"] == "bug-report.yml" and "FramePort-diag-a.zip" in f["logs"]
    assert "missing-ovr-symbol" in f["findings"] and "alice" not in url


@pytest.mark.parametrize("template,layout", [("working-config.yml", issue.WORKING_CONFIG_FIELDS),
                                             ("bug-report.yml", issue.PROBLEM_FIELDS)])
def test_issue_body_labels_match_the_forms(template, layout):
    form = yaml.safe_load((ROOT / ".github/ISSUE_TEMPLATE" / template).read_text(encoding="utf-8"))
    fields = {f["id"]: (f["attributes"]["label"], f["attributes"].get("render"))
              for f in form["body"] if f.get("id") and f["type"] in ("input", "textarea")}
    assert fields == layout
    if template == "working-config.yml":
        confirm = next(f for f in form["body"] if f.get("id") == "confirm")["attributes"]
        assert issue.WORKING_CONFIG_CONFIRM == (confirm["label"], confirm["options"][0]["label"])


def test_working_config_body_round_trips_through_the_catalog_script(tmp_path):
    """GitHub #167: the plain-issue body (and the clipboard copy) is what the issue form would have written."""
    g = _game(tmp_path)
    e = catalog.entry_from_library(g, status="works", notes="n", verified={"app": "0.2.0"})
    env = {"app": "0.2.0", "os": "Linux"}
    body = issue.working_config_body(g, catalog.to_yaml(e), "works", "", env, redact.default())
    assert body.startswith("### Game\n\n") and "### Notes\n\n_No response_" in body
    assert "```yaml\npackage: com.example.game" in body and "- [X] I played the game" in body
    s = _script()
    assert s.main([str(_write(tmp_path, body)), "--issue", "9", "--out", str(tmp_path)]) == 0
    d = yaml.safe_load((tmp_path / "com.example.game.yaml").read_text())
    assert d["frame"] == ["frame.nodebug"] and d["verified"]["issue"] == 9
    links = issue.working_config_links(g, catalog.to_yaml(e), "works", "played /home/alice fine" + "x" * 20000, env,
                                       redact.default())
    assert links.form == issue.working_config_url(g, catalog.to_yaml(e), "works",
                                                  "played /home/alice fine" + "x" * 20000, env, redact.default())
    f = _fields(links.plain)
    assert "template" not in f and f["title"] == "[Working recipe] Example" and f["labels"] == "working-config"
    assert len(links.plain) <= issue.MAX_URL + 200 and "alice" not in links.plain
    assert "[... cut" in s.section(f["body"], "Notes")  # notes trimmed first, the recipe never
    assert s.validate(s.recipe_from_body(f["body"])).package == "com.example.game"
    assert "x" * 20000 in links.body  # the clipboard copy isn't trimmed


def test_problem_links(tmp_path):
    g = _game(tmp_path)
    links = issue.problem_links(g, "crashes", {"app": "x"}, "FramePort-diag-a.zip", "package: com.example.game\n")
    f = _fields(links.plain)
    assert f["labels"] == "bug" and f["body"] == links.body
    assert "### What happens?\n\ncrashes" in links.body and "FramePort-diag-a.zip" in links.body
    assert "### Recipe\n\n```yaml\npackage: com.example.game\n```" in links.body


def test_entry_from_library_matches_save_known_good_shape(tmp_path):
    g = _game(tmp_path)
    d = catalog.entry_from_library(g).to_dict()
    assert d["package"] == "com.example.game" and d["status"] == "works" and d["engine"] == "Unity"
    assert d["verified"]["date"] and "known_good_sha256" not in d["verified"]  # no build yet
    rift = {"package": "rift.x", "kind": "rift", "title": "X", "recipe": {"package": "rift.x", "patches": {
        "pcvr.xr_timefix": {}, "pcvr.proton_env": {"env": "A=1\nB=2"}}}, "analysis": {}}
    r = catalog.entry_from_library(rift)
    assert r.kind == "rift" and r.pcvr == ["pcvr.xr_timefix"] and r.proton_env == {"A": "1", "B": "2"}
    assert r.pcvr_remove == ["pcvr.revive"]


# ------------------------------------------------------------------------------------------ issue → catalog
def _script():
    spec = importlib.util.spec_from_file_location("catalog_from_issue", ROOT / "scripts/catalog_from_issue.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BODY = """### Game

Example — com.example.game

### Result

works

### Recipe

```yaml
package: com.example.game
title: Example
status: works
frame:
- frame.ovrstubs
```

### Environment

_No response_
"""


def test_catalog_from_issue(tmp_path):
    s = _script()
    assert s.main([str(_write(tmp_path, BODY)), "--issue", "7", "--out", str(tmp_path)]) == 0
    d = yaml.safe_load((tmp_path / "com.example.game.yaml").read_text())
    assert d["frame"] == ["frame.ovrstubs"] and d["verified"]["issue"] == 7
    with_device = BODY.replace("- frame.ovrstubs", "- frame.ovrstubs\ndevice:\n- device.text_input_window")
    assert s.main([str(_write(tmp_path, with_device)), "--out", str(tmp_path)]) == 0
    assert yaml.safe_load((tmp_path / "com.example.game.yaml").read_text())["device"] == ["device.text_input_window"]


@pytest.mark.parametrize("bad", ["package: ../../etc/passwd\ntitle: x", "package: com.a.b\ntitle: x\nevil: 1",
                                 "package: com.a.b\ntitle: x\nstatus: great",
                                 "package: com.a.b\ntitle: x\nframe: [\"$(rm)\"]",
                                 "package: com.a.b\ntitle: x\ndevice_files: {'../x': 1}", "- a list",
                                 "package: com.a.b\ntitle: x\nframe: [frame.no_such_patch]",
                                 "package: com.a.b\ntitle: x\ndevice: device.text_input_window"])
def test_catalog_from_issue_rejects(tmp_path, bad):
    s = _script()
    body = BODY.split("```yaml")[0] + "```yaml\n" + bad + "\n```\n"
    assert s.main([str(_write(tmp_path, body)), "--out", str(tmp_path)]) == 1
    assert not list(tmp_path.glob("*.yaml"))


def _write(tmp_path, text):
    p = tmp_path / "body.md"
    p.write_text(text)
    return p


# ------------------------------------------------------------------------------------------ agent
def test_agent_collect_diag(monkeypatch, tmp_path):
    from test_agent import load_agent

    a = load_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "run", lambda cmd, **kw: type("P", (), {"stdout": "", "returncode": 1, "stderr": ""})())
    anchor = Path(a.ANCHORS) / "com.x.y"
    base = tmp_path / "games" / "com.x.y"
    anchor.mkdir(parents=True)
    base.mkdir(parents=True)
    (anchor / "deployment.json").write_text(json.dumps({"base": str(base), "appid": 123, "package": "com.x.y",
                                                        "files": {"game": {"a": 1}}}))
    (anchor / "launch.sh").write_text("#!/bin/sh\n")
    (base / "launch.log").write_text("x" * 5000)
    (base / "settings.conf").write_text("scale=1\n")
    logcat = Path(a.STEAM) / "logs/lepton-logcats/steamlaunch-123"
    logcat.mkdir(parents=True)
    (logcat / "logcat-radio.log").write_text("radio")
    (logcat / "logcat-main.log").write_text("logcat line")
    (Path(a.STEAM) / "logs/lepton-steamlaunch-123.log").write_text("lepton")
    xr = Path(a.STEAM) / "logs/XRService-2026.09.30"
    xr.mkdir()
    (xr / "XRService-12-31-50.log").write_text("xr")
    host = a.cmd_collect_diag({})
    assert host["agent_version"] == a.AGENT_VERSION and "openxr_layers" in host["host"]
    # (plus the previous boot's journal excerpts when this machine has a journal)
    assert {k: v for k, v in host["files"].items() if not k.startswith("previous-boot")} == \
        {"XRService-12-31-50.log": "xr"}
    res = a.cmd_collect_diag({"package": "com.x.y", "max_bytes": 1000})
    f = res["files"]
    assert res["installed"] and {"deployment.json", "launch.sh", "settings.conf", "launch.log", "logcat-main.log",
                                 "lepton-steamlaunch-123.log"} <= set(f)
    assert [n for n in f if n.startswith("logcat")] == ["logcat-main.log", "logcat-radio.log"]
    assert f["launch.log"].startswith("[... first 4000 bytes cut") and "files" not in json.loads(f["deployment.json"])
    assert res["listing"]["roots"]
    assert a.cmd_collect_diag({"package": "com.not.installed"})["installed"] is False
