"""Install links ("Install with FrameDrop" buttons, frameport:// links): parsing, the safety rules, manifests,
downloads, the handler scripts and their inbox, drop routing, and the VR / flat window choice."""
import hashlib
import json
import os
import subprocess
import threading
import time
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import pytest

from frameport import deeplink, urlhandler
from frameport.deeplink import LinkError

MANIFEST = "https://cdn.example.com/game.framedrop.json"


# ------------------------------------------------------------------------------------------------- links
@pytest.mark.parametrize("link", [
    f"framedrop://install?manifest={quote(MANIFEST, safe='')}",
    f"frameport://install?manifest={quote(MANIFEST, safe='')}",
    f"https://frameport.app/install?manifest={quote(MANIFEST, safe='')}",
    f"https://frameport.app/install/?manifest={MANIFEST}",
    f"https://framedropvr.com/install?manifest={quote(MANIFEST, safe='')}",
    f"https://framedropvr.com/install/?manifest={MANIFEST}",
    MANIFEST,
    f'  "framedrop://install?manifest={quote(MANIFEST, safe="")}"  ',
])
def test_link_forms_name_the_manifest(link):
    assert deeplink.parse(link).manifest_url == MANIFEST


def test_direct_file_links():
    url = "https://github.com/x/y/releases/download/v1/Game-arm64.apk"
    assert deeplink.parse(f"framedrop://install?url={quote(url, safe='')}").file_url == url
    assert deeplink.parse(url).file_url == url
    assert deeplink.parse(deeplink.make_link(file_url=url)).file_url == url


@pytest.mark.parametrize("url, reason", [
    ("http://cdn.example.com/x.apk", "https"),
    ("https://user:pw@cdn.example.com/x.apk", "password"),
    ("https://192.168.1.20/x.apk", "local network"),
    ("https://10.0.0.1/x.apk", "local network"),
    ("https://[fe80::1]/x.apk", "local network"),
    ("https://cdn.example.com/builds/", "file name"),
    ("file:///C:/x.apk", "https"),
    ("ftp://cdn.example.com/x.apk", "https"),
])
def test_unsafe_urls_are_refused(url, reason):
    with pytest.raises(LinkError, match=reason):
        deeplink.check_url(url)
    with pytest.raises(LinkError):
        deeplink.parse(f"framedrop://install?url={quote(url, safe='')}")


def test_http_only_on_this_pc():
    for host in ("localhost", "127.0.0.1", "[::1]"):
        assert deeplink.check_url(f"http://{host}:8000/x.apk")


def test_other_links_are_refused():
    for bad in ("framedrop://uninstall?url=https%3A%2F%2Fa.com%2Fx.apk", "framedrop://install",
                "https://example.com/page.html", "nonsense", ""):
        with pytest.raises(LinkError):
            deeplink.parse(bad)


def test_names_resolving_to_a_lan_are_refused(monkeypatch):
    monkeypatch.setattr(deeplink.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("192.168.0.5", 443))])
    with pytest.raises(LinkError, match="resolves"):
        deeplink.check_url("https://evil.example.com/x.apk", resolve=True)


# ------------------------------------------------------------------------------------------------- manifests
def test_manifest_validation():
    m = deeplink.manifest_from_data({"schema": "framedrop.install/v1", "name": "  My   Game ", "extra": 1, "files": [
        {"url": "https://cdn.example.com/My%20Game-arm64.apk", "sha256": "AB" * 32},
        {"url": "https://cdn.example.com/main.1.com.my.game.obb", "sha256": "optional-but-better"}]})
    assert m.name == "My Game" and m.main.filename == "My Game-arm64.apk" and m.main.sha256 == "ab" * 32
    assert m.files[1].kind == deeplink.OBB and m.files[1].sha256 is None  # FrameDrop's placeholder text
    for bad in ({}, {"schema": "framedrop.install/v2", "name": "x", "files": [{"url": "https://a.com/x.apk"}]},
                {"schema": "framedrop.install/v1", "files": [{"url": "https://a.com/x.apk"}]},
                {"schema": "framedrop.install/v1", "name": "x", "files": []},
                {"schema": "framedrop.install/v1", "name": "x", "files": [{"url": "https://a.com/readme.txt"}]},
                {"schema": "framedrop.install/v1", "name": "x", "files": [{"url": "http://a.com/x.apk"}]}):
        with pytest.raises(LinkError):
            deeplink.manifest_from_data(bad)


def test_direct_link_manifest_and_titles():
    m = deeplink.fetch_manifest(deeplink.InstallRequest(file_url="https://a.com/dl/Cool_Game-1.2-arm64-v8a.apk"))
    assert m.direct and m.main.kind == deeplink.APK and m.name == "Cool Game"
    assert deeplink.classify("thing-linux-arm64.tar.gz") == deeplink.LINUX
    assert deeplink.classify("Tool.AppImage") == deeplink.LINUX and deeplink.classify("Setup.EXE") == deeplink.EXE


def test_local_manifest_file(tmp_path):
    f = tmp_path / "game.framedrop.json"
    f.write_text(json.dumps({"schema": "framedrop.install/v1", "name": "Local",
                             "files": [{"url": "https://a.com/x.zip"}]}))
    req = deeplink.parse(str(f))
    assert req.local_manifest == str(f) and deeplink.fetch_manifest(req).main.kind == deeplink.LINUX


@pytest.fixture
def server(tmp_path):
    """A web server on 127.0.0.1 (http is allowed there) serving tmp_path/www."""
    www = tmp_path / "www"
    www.mkdir()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(www)))
    httpd.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield www, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_fetch_and_download_a_manifest(server):
    www, base = server
    apk, obb = b"PK\x03\x04 fake apk" * 1000, b"obb data" * 500
    (www / "Game-arm64.apk").write_bytes(apk)
    (www / "main.1.com.x.game.obb").write_bytes(obb)
    (www / "game.framedrop.json").write_text(json.dumps({"schema": "framedrop.install/v1", "name": "Game", "files": [
        {"url": f"{base}/Game-arm64.apk", "sha256": hashlib.sha256(apk).hexdigest()},
        {"url": f"{base}/main.1.com.x.game.obb"}]}))
    m = deeplink.fetch_manifest(deeplink.parse(f"framedrop://install?manifest={quote(base + '/game.framedrop.json')}"))
    assert m.name == "Game" and m.host == "127.0.0.1" and not m.direct
    path = deeplink.download(m)
    assert path.name == "Game-arm64.apk" and path.read_bytes() == apk
    assert (path.parent / "obb" / "main.1.com.x.game.obb").read_bytes() == obb  # where the APK scan finds OBBs


def test_checksum_mismatch_leaves_nothing(server):
    www, base = server
    (www / "x.apk").write_bytes(b"real")
    m = deeplink.manifest_from_data({"schema": "framedrop.install/v1", "name": "X",
                                     "files": [{"url": f"{base}/x.apk", "sha256": "00" * 32}]}, f"{base}/m.json")
    with pytest.raises(LinkError, match="checksum"):
        deeplink.download(m)
    assert not list(deeplink.staging_dir(m).rglob("*.apk*"))


def test_oversized_manifest_is_refused(server):
    www, base = server
    (www / "big.json").write_bytes(b" " * (deeplink.MAX_MANIFEST + 10))
    with pytest.raises(LinkError, match="too large"):
        deeplink.fetch_manifest(deeplink.InstallRequest(manifest_url=f"{base}/big.json"))


def test_add_from_link_names_the_game(tmp_path):
    """A manifest's name becomes the (locked) title; the link is remembered on the entry."""
    from frameport import pipeline
    from frameport.analysis import linux

    head = bytearray(64)
    head[:4], head[4], head[5] = b"\x7fELF", 2, 1
    head[18:20] = linux.EM_AARCH64.to_bytes(2, "little")
    z = tmp_path / "tool-linux-arm64.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("tool/tool", bytes(head))
    m = deeplink.Manifest("Nice Tool", [deeplink.ManifestFile("https://a.com/tool-linux-arm64.zip")],
                          "https://a.com/tool.framedrop.json")
    g = pipeline.add_from_link(m, z)
    assert g["title"] == "Nice Tool" and g["title_locked"] and g["link"]["source"] == m.source


# ------------------------------------------------------------------------------------------------- handler
def test_inbox_and_heartbeat():
    assert not urlhandler.app_running()
    urlhandler.heartbeat()
    assert urlhandler.app_running() and not urlhandler.app_running(time.time() + urlhandler.FRESH + 1)
    urlhandler.drop_link("framedrop://install?url=a")
    old = urlhandler.drop_link("frameport://old")
    os.utime(old, (time.time() - urlhandler.MAX_LINK_AGE - 5,) * 2)
    assert urlhandler.take_links() == ["framedrop://install?url=a"]  # the stale one is dropped unopened
    assert urlhandler.take_links() == []


@pytest.mark.skipif(not os.path.exists("/bin/sh") or os.name == "nt", reason="needs sh")
def test_sh_handler_queues_and_starts_once(tmp_path):
    from frameport.core.paths import user_data_dir

    data = user_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "started"
    script = tmp_path / "h.sh"
    script.write_text(urlhandler.sh_script(data, ["sh", "-c", f"echo x >> '{marker}'"]))
    link = "framedrop://install?manifest=https%3A%2F%2Fa.com%2Fm.json&x='$(touch /tmp/pwned)'"
    subprocess.run(["sh", str(script), link], check=True, timeout=20)
    assert urlhandler.take_links() == [link]  # stored as text, never run
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.exists()
    subprocess.run(["sh", str(script), "frameport://second"], check=True, timeout=20)
    time.sleep(1)
    assert marker.read_text().count("x") == 1  # "starting" marker: not started twice
    urlhandler.heartbeat()  # running app: just queue
    subprocess.run(["sh", str(script), "frameport://third"], check=True, timeout=20)
    assert urlhandler.take_links() == ["frameport://second", "frameport://third"]


def test_ps1_script_and_commands(monkeypatch):
    from frameport.core.paths import user_data_dir

    text = urlhandler.ps1_script(user_data_dir(), [r"C:\Apps\FramePort\FramePort.exe"])
    assert urlhandler.MARK in text and "Start-Process -FilePath 'C:\\Apps\\FramePort\\FramePort.exe'" in text
    assert "param([string]$Link)" in text and "-ArgumentList" not in text
    assert '"%1"' in urlhandler.handler_command("windows") and urlhandler.is_ours(urlhandler.handler_command("windows"))
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert '-d "Ubuntu" -e sh' in urlhandler.handler_command("wsl")
    entry = urlhandler.desktop_entry("sh x %u", ["framedrop"])
    assert "x-scheme-handler/framedrop;" in entry and "frameport;" not in entry


def test_windows_registration_per_scheme(monkeypatch):
    """Each scheme on its own; a scheme FrameDrop owns is only taken with force; off removes only FramePort's."""
    reg = {"framedrop": '"C:\\Program Files\\FrameDrop\\FrameDrop.exe" "%1"'}
    monkeypatch.setattr(urlhandler, "platform_kind", lambda: "windows")
    monkeypatch.setattr(urlhandler, "windows_handler", lambda s: reg.get(s))
    monkeypatch.setattr(urlhandler, "_reg_set", lambda s, cmd: reg.__setitem__(s, cmd))
    monkeypatch.setattr(urlhandler, "_reg_delete", lambda s: reg.pop(s, None))
    st = urlhandler.register()
    assert st["framedrop"] == "other" and st["framedrop_by"] == "FrameDrop" and st["frameport"] == "ours"
    assert urlhandler.register(["framedrop"], force=True)["framedrop"] == "ours"
    st = urlhandler.set_enabled("framedrop", False)
    assert st["framedrop"] == "none" and st["frameport"] == "ours" and not urlhandler.enabled("framedrop")
    reg["framedrop"] = '"C:\\FrameDrop.exe" "%1"'
    assert urlhandler.unregister()["framedrop"] == "other"  # another program's registration stays


def test_isolated_homes_never_register(monkeypatch):
    called = []
    monkeypatch.setattr(urlhandler, "register", lambda *a, **k: called.append(1))
    assert urlhandler.apply_setting() is None and not called  # FRAMEPORT_HOME is set by the tests


# ------------------------------------------------------------------------------------------------- drops, display
def test_drop_routing(tmp_path):
    from frameport.analysis import linux
    from frameport.ui import dropped

    head = bytearray(64)
    head[:4], head[4], head[5] = b"\x7fELF", 2, 1
    head[18:20] = linux.EM_AARCH64.to_bytes(2, "little")
    (tmp_path / "apks").mkdir()
    (tmp_path / "apks" / "a.apk").write_bytes(b"PK")
    (tmp_path / "linuxapp" / "bin").mkdir(parents=True)
    (tmp_path / "linuxapp" / "bin" / "app").write_bytes(bytes(head))
    (tmp_path / "prog").write_bytes(bytes(head))
    for name in ("x.apk", "Setup.exe", "m.framedrop.json", "notes.txt", "a.zip"):
        (tmp_path / name).write_bytes(b"PK" if name.endswith(".zip") else b"x")
    got = dict((os.path.basename(p), k) for k, p in dropped.route(
        [str(tmp_path / n) for n in ("apks", "linuxapp", "prog", "x.apk", "Setup.exe", "m.framedrop.json",
                                     "notes.txt", "a.zip")]))
    assert got == {"apks": "folder", "linuxapp": "linux", "prog": "linux", "x.apk": "apk", "Setup.exe": "exe",
                   "m.framedrop.json": "manifest", "notes.txt": "unknown", "a.zip": "linux"}


def test_display_mode_choice():
    from frameport.core.models import Recipe
    from frameport.install.installer import install_context, show_window

    def ctx(mode=None, text_window=False):
        patches = {"device.display_mode": {"mode": mode}} if mode is not None else {}
        if text_window:
            patches["device.text_input_window"] = {}
        return install_context(Recipe(package="com.x", patches=patches))
    assert show_window(ctx(), automatic=True) and not show_window(ctx(), automatic=False)
    assert show_window(ctx("flat"), automatic=False)  # a 2D app FramePort took for VR
    assert not show_window(ctx("vr"), automatic=True)  # a VR app FramePort took for 2D
    assert show_window(ctx("vr", text_window=True), automatic=True)  # typing still needs the window


def test_cli_open_link_downloads_and_adds(server):
    """`frameport open-link <link> --yes --no-install`: the same flow as the GUI, without the Frame."""
    from typer.testing import CliRunner

    from frameport.analysis import linux
    from frameport.cli import app
    from frameport.core import library

    www, base = server
    head = bytearray(64)
    head[:4], head[4], head[5] = b"\x7fELF", 2, 1
    head[18:20] = linux.EM_AARCH64.to_bytes(2, "little")
    with zipfile.ZipFile(www / "viewer-linux-arm64.zip", "w") as z:
        z.writestr("viewer/viewer", bytes(head))
    (www / "v.framedrop.json").write_text(json.dumps({"schema": "framedrop.install/v1", "name": "Viewer",
                                                      "files": [{"url": f"{base}/viewer-linux-arm64.zip"}]}))
    link = deeplink.make_link(manifest_url=f"{base}/v.framedrop.json", scheme="framedrop")
    r = CliRunner().invoke(app, ["open-link", link, "--yes", "--no-install"])
    assert r.exit_code == 0, r.output
    assert "added linux." in r.output and any(g.get("title") == "Viewer" for g in library.games())
    r = CliRunner().invoke(app, ["open-link", "framedrop://install?url=http%3A%2F%2Fevil.com%2Fx.apk", "--yes"])
    assert r.exit_code != 0 and "https" in r.output
