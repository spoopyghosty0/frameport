"""Offline sign-in tests. Fake credentials must never reach files, logs, or Wine/SSH argv."""
import copy
import importlib.util
import io
import json
import queue
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from frameport import cli, fexlogin, urlhandler

ROOT = Path(__file__).resolve().parents[1]
PAGE = "https://auth.meta.com/native_sso/confirm?native_sso_etoken=FAKE_SECRET_DO_NOT_PERSIST"
CALLBACK = "oculus://link/frl_login?native_sso_etoken=FAKE_SECRET_DO_NOT_PERSIST"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ext(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("os.getuid", lambda: 1000, raising=False)   # Frame-only API; tests also run on Windows.
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in ("fexrift.py", "fexrift_games.json", "fexrift_login.py"):
        shutil.copyfile(ROOT / "agent" / name, artifacts / name)
    core = load(artifacts / "fexrift.py", "login_core")
    core.ROOT = str(tmp_path / "r")   # Unix socket paths are limited to 108 bytes on Linux.
    module = load(artifacts / "fexrift_login.py", "login_extension")
    module.register(core)
    monkeypatch.setattr(core, "wine", lambda *a, **kw: SimpleNamespace(stdout="", returncode=0))
    monkeypatch.setattr(core, "wineserver_kill", lambda: None)
    monkeypatch.setattr(core, "steam_display_env", lambda: {"DISPLAY": ":1"})
    module.folder().mkdir(parents=True)
    return module


def test_extension_discovery(ext):
    ext.CORE.load_extensions()
    assert "login" in ext.CORE.LOADED_EXTENSIONS
    assert {"login_start", "login_status", "login_stop"} <= ext.CORE.COMMANDS.keys()


def assert_no_credentials(path):
    for p in path.rglob("*"):
        if p.is_file():
            assert b"FAKE_SECRET_DO_NOT_PERSIST" not in p.read_bytes(), p


def test_open_url_helper_and_state_never_persist_query(ext, monkeypatch):
    broker = ext.Broker(False)
    seen = []
    monkeypatch.setattr(ext, "quiet", lambda argv, **kw: seen.append((argv, kw)))
    # Execute the generated helper with an in-memory transport (the build sandbox disallows socket.bind).
    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, path):
            assert path == str(ext.folder() / "broker.sock")

        def sendall(self, msg):
            self.reply = broker.handle(json.loads(msg))

        def shutdown(self, how):
            pass

        def recv(self, size):
            return json.dumps(self.reply).encode()

    monkeypatch.setattr(ext.socket, "socket", lambda *a: Socket())
    monkeypatch.setattr(sys, "argv", ["fp-open-url", PAGE])
    helper = ext.folder() / "fp-open-url"
    helper.write_text(ext.helper_script("page"))
    exec(compile(helper.read_text(), str(helper), "exec"), {})
    state = json.loads((ext.folder() / "state.json").read_text())
    assert state["page_url"] == PAGE.split("?")[0] and state["page_opened_at"] > 0
    assert seen[0][0] == ["xdg-open", PAGE]
    assert_no_credentials(ext.folder())


def test_memory_only_fallback_and_status_transitions(ext, monkeypatch):
    broker = ext.Broker(True)
    monkeypatch.setattr(ext, "running", lambda name: True)
    monkeypatch.setattr(ext, "request", broker.handle)
    assert ext.status({}) == {"runtime_running": True, "client_running": True, "page_opened_at": None,
                              "callback_received_at": None, "signed_in": False}
    assert broker.handle({"op": "page", "url": PAGE})["accepted"]
    assert "open_url" not in ext.status({})
    assert ext.status({"take_url": True})["open_url"] == PAGE
    assert "open_url" not in ext.status({"take_url": True})
    monkeypatch.setattr(ext, "deliver", lambda url: {"delivered": url == CALLBACK})
    assert broker.handle({"op": "callback", "url": CALLBACK}) == {"delivered": True}
    sessions = Path(ext.CORE.PFX) / "drive_c/users/steamuser/AppData/Roaming/Oculus/sessions"
    sessions.mkdir(parents=True)
    # real layout: Meta's app creates sessions/_oaf when the runtime starts, a sign-in adds sessions/<user id>/
    (sessions / "_oaf").mkdir()
    (sessions / "_oaf" / "data.sqlite").write_text("framework")
    (sessions / "empty").touch()
    assert not ext.status({})["signed_in"]
    (sessions / "1234567890-1275611841").mkdir()
    (sessions / "1234567890-1275611841" / "data.sqlite").write_text("test session")
    st = ext.status({})
    assert st["signed_in"] and st["page_opened_at"] and st["callback_received_at"]
    assert_no_credentials(ext.folder())


@pytest.mark.parametrize("url", ["https://evil.test/?secret=x", "oculus://evil/frl_login?x",
                                  "https://auth.meta.com@evil.test/native_sso/confirm?x", CALLBACK + "\n"])
def test_reject_other_links_without_echoing(ext, url):
    assert not ext.Broker(True).handle({"op": "page", "url": url})["accepted"]
    assert not ext.deliver(url)["delivered"]


def test_callback_goes_to_pe_stdin(ext, monkeypatch):
    calls = []
    monkeypatch.setattr(ext, "quiet", lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=0))
    assert ext.deliver(CALLBACK) == {"delivered": True}
    argv, kw = calls[0]
    assert CALLBACK not in repr(argv) and kw["input"] == CALLBACK.encode()
    assert "LD_PRELOAD" not in kw["env"] and kw["env"]["ENABLE_GAMESCOPE_WSI"] == "0"


def test_start_generates_helpers_and_stop_restores_exactly(ext, monkeypatch, tmp_path):
    (Path(ext.CORE.HERE) / "fp_pipesend.exe").touch()
    client = Path(ext.CORE.META_DIR) / "Support/oculus-client/Client.exe"
    client.parent.mkdir(parents=True)
    client.touch()
    apps, _ = ext.desktop_paths()
    apps.mkdir(parents=True)
    desktop = apps / ext.DESKTOP
    desktop.write_bytes(b"previous desktop\r\n")
    mime = tmp_path / "config/mimeapps.list"
    mime.parent.mkdir(parents=True)
    before = b"[Default Applications]\r\nx-scheme-handler/oculus=meta.desktop;\r\n"
    mime.write_bytes(before)
    calls, active = [], []

    def wine(*args, **kw):
        calls.append(args)
        return SimpleNamespace(stdout="    Browsers    REG_EXPAND_SZ    /previous browser --flag\n", returncode=0)

    def quiet(argv, **kw):
        if argv[:2] == ["xdg-mime", "default"]:
            mime.write_text("temporary handler")
        return SimpleNamespace(returncode=0)

    def request(msg):
        if active and msg["op"] == "ping":
            return {"ready": True}
        raise OSError("no broker")

    monkeypatch.setattr(ext.CORE, "wine", wine)
    monkeypatch.setattr(ext, "running", lambda name: False)
    monkeypatch.setattr(ext, "request", request)
    monkeypatch.setattr(ext, "quiet", quiet)
    monkeypatch.setattr(ext.subprocess, "Popen", lambda *a, **kw: active.append(True))
    monkeypatch.setattr(ext.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="meta.desktop\n"))
    result = ext.start({"timeout": 900})
    assert not result["started"]
    entry = desktop.read_text()
    assert 'Exec="' in entry and "fp-oculus-callback\" %u" in entry
    assert "NoDisplay=true" in entry and "MimeType=x-scheme-handler/oculus;" in entry
    script = (ext.folder() / "launch.sh").read_text()
    assert "OVRServer_x64.exe" in script and "[O]VRLibrarian.exe" in script and "run ./Client.exe" in script
    assert "exec >/dev/null 2>&1" in script and "unset LD_PRELOAD" in script
    assert ext.stop({})["stopped"] and ext.stop({})["stopped"]
    assert mime.read_bytes() == before and desktop.read_bytes() == b"previous desktop\r\n"
    assert any("/previous browser --flag" in a and "REG_EXPAND_SZ" in a for a in calls)
    assert_no_credentials(ext.folder())


def test_missing_pe_has_actionable_error(ext):
    with pytest.raises(ext.CORE.FexError, match="fp_pipesend.exe missing"):
        ext.start({})


def test_desktop_handler_exec_uses_the_existing_desktop_quoting_rules(ext):
    agent = load(ROOT / "agent/frameport_agent.py", "login_quoting_agent")
    helper = '/tmp/login $d "quoted" \\50%/fp-oculus-callback'
    entry = ext.desktop_entry(helper)
    assert f"Exec={agent.desktop_exec_arg(helper)} %u\n" in entry


def test_cleanup_keeps_sessions_and_retries_after_failure(ext, monkeypatch):
    path = ext.folder() / "restore.json"
    path.write_text(json.dumps({"browser": None, "files": {}}))
    sessions = Path(ext.CORE.PFX) / "drive_c/users/steamuser/AppData/Roaming/Oculus/sessions"
    sessions.mkdir(parents=True)
    (sessions / "1234567890-1275611841").mkdir()
    (sessions / "1234567890-1275611841" / "fake").write_bytes(b"test session")
    monkeypatch.setattr(ext, "request", lambda msg: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(ext.CORE, "wineserver_kill", lambda: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        ext.stop({})
    assert path.exists()
    monkeypatch.setattr(ext.CORE, "wineserver_kill", lambda: None)
    assert ext.stop({})["signed_in"] and not path.exists()
    assert ext.stop({})["signed_in"] and (sessions / "1234567890-1275611841" / "fake").read_bytes() == b"test session"


def test_stop_signals_launcher_before_wine_and_registry(ext, monkeypatch):
    (ext.folder() / "launch.pid").write_text("12345 98765")
    (ext.folder() / "restore.json").write_text(json.dumps({"browser": None, "files": {}}))
    events = []
    ticks = iter(["98765", None])
    monkeypatch.setattr(ext, "process_tick", lambda pid: next(ticks))
    monkeypatch.setattr(ext.os, "kill", lambda pid, sig: events.append("launcher"))
    monkeypatch.setattr(ext.CORE, "wineserver_kill", lambda: events.append("wine"))
    monkeypatch.setattr(ext.CORE, "wine", lambda *a, **kw: events.append("registry"))
    ext.restore()
    assert events == ["launcher", "wine", "registry"]
    assert not (ext.folder() / "launch.pid").exists()


def test_stop_does_not_signal_a_reused_pid(ext, monkeypatch):
    (ext.folder() / "launch.pid").write_text("12345 98765")
    monkeypatch.setattr(ext, "process_tick", lambda pid: "other start time")
    monkeypatch.setattr(ext.os, "kill", lambda *a: pytest.fail("must not signal an unrelated process"))
    assert ext.stop_launcher() is None


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash")
def test_generated_launcher_shell_syntax(ext):
    script = ext.folder() / "launch.sh"
    script.write_text(ext.launcher())
    result = ext.subprocess.run(["bash", "-n", str(script)], capture_output=True)
    assert result.returncode == 0, result.stderr


def test_broker_expiry_restores_registration(ext, monkeypatch):
    events = []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, path):
            Path(path).touch()

        def listen(self, count):
            pass

        def settimeout(self, timeout):
            pass

    ticks = iter([0, 2])
    monkeypatch.setattr(ext.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(ext.socket, "socket", lambda *a: Socket())
    monkeypatch.setattr(ext, "restore", lambda: events.append("restored"))
    assert ext.broker({"timeout": 1, "via_pc": True})["stopped"]
    assert events == ["restored"] and not (ext.folder() / "broker.sock").exists()


def test_pc_protocol_callback_posts_into_memory_and_restores(monkeypatch, tmp_path):
    events, commands, handlers = [], [], []

    class Server:
        def __init__(self, address, handler):
            self.server_address = ("127.0.0.1", 12345)
            handlers.append(handler)

        def serve_forever(self):
            pass

        def shutdown(self):
            events.append("shutdown")

        def server_close(self):
            events.append("closed")

    @contextmanager
    def registration(command):
        commands.append(command)
        events.append("registered")
        try:
            yield
        finally:
            events.append("restored")

    def urlopen(req, **kw):
        handler = handlers[0].__new__(handlers[0])
        handler.path = req.selector
        handler.headers = {"Content-Length": str(len(req.data))}
        handler.rfile = io.BytesIO(req.data)
        handler.send_response = lambda code: events.append(code)
        handler.end_headers = lambda: None
        handler.do_POST()
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(fexlogin, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(urlhandler, "platform_kind", lambda: "linux")
    monkeypatch.setattr(urlhandler, "temporary_oculus_handler", registration)
    monkeypatch.setattr(fexlogin, "user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(fexlogin.urllib.request, "urlopen", urlopen)
    with fexlogin.pc_callback() as inbox:
        # Run the generated Python protocol helper, retaining its real HTTP request construction.
        script = next(tmp_path.rglob("callback.py"))
        monkeypatch.setattr(sys, "argv", [str(script), CALLBACK])
        exec(compile(script.read_text(), str(script), "exec"), {})
        assert inbox.get_nowait() == CALLBACK and commands[0].endswith(" %u")
        assert_no_credentials(tmp_path)
    assert events == ["registered", 204, "restored", "shutdown", "closed"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("exists", [True, False])
@pytest.mark.parametrize("kind", ["windows", "wsl"])
def test_temporary_windows_handler_exact_tree_restore(tmp_path, monkeypatch, exists, kind):
    monkeypatch.setattr(urlhandler, "platform_kind", lambda: kind)
    monkeypatch.setattr(urlhandler, "user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(urlhandler.winhost, "to_windows", str)
    # Unknown binary values and subkeys must be restored too, not just shell/open/command.
    old = {"values": {"": [1, "Meta"], "URL Protocol": [1, ""], "binary": [3, [0, 255]]},
           "shell": {"open": {"command": [2, '"%MetaHome%/Client.exe" "%1"']}}, "other": {"x": [4, 5]}}
    tree = copy.deepcopy(old) if exists else None

    def reg(argv, **kw):
        nonlocal tree
        op = argv[1]
        if op == "query":
            return SimpleNamespace(returncode=0 if tree is not None else 1)
        if op == "export":
            Path(argv[3]).write_text(json.dumps(tree))
        elif op == "delete":
            tree = None
        elif op == "import":
            tree = json.loads(Path(argv[2]).read_text())
        return SimpleNamespace(returncode=0)

    def register(scheme, command):
        nonlocal tree
        assert scheme == "oculus"
        tree = {"temporary": command}

    monkeypatch.setattr(urlhandler.winhost, "run_win", reg)
    monkeypatch.setattr(urlhandler, "_reg_set", register)
    with pytest.raises(RuntimeError, match="simulate interruption"):
        with urlhandler.temporary_oculus_handler("memory-only callback"):
            assert tree == {"temporary": "memory-only callback"}
            raise RuntimeError("simulate interruption")
    # only the handler's own files (another test's background thread may create folders in the data dir meanwhile)
    assert tree == (old if exists else None) and not list(tmp_path.glob("oculus-handler*"))


@pytest.mark.parametrize("exists", [True, False])
def test_temporary_linux_handler_restores_default_and_file(tmp_path, monkeypatch, exists):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(urlhandler, "platform_kind", lambda: "linux")
    monkeypatch.setattr(urlhandler.shutil, "which", lambda cmd: "/bin/" + cmd)
    mime = tmp_path / "config/mimeapps.list"
    mime.parent.mkdir()
    before = b"[Default Applications]\r\nx-scheme-handler/oculus=meta.desktop;\r\n"
    if exists:
        mime.write_bytes(before)

    def run(argv, **kw):
        mime.write_text("temporary mime default")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(urlhandler.subprocess, "run", run)
    with urlhandler.temporary_oculus_handler('"python" -I "/tmp/callback.py" %u'):
        assert "oculus;" in (urlhandler._desktop_path().parent / "frameport-oculus-login.desktop").read_text()
    assert mime.read_bytes() == before if exists else not mime.exists()
    assert not (urlhandler._desktop_path().parent / "frameport-oculus-login.desktop").exists()


class FakeFrame:
    def __init__(self, states=None, error=None):
        self.calls = []
        self.states = iter(states or [{"signed_in": True}])
        self.error = error

    def agent(self, cmd, **args):
        self.calls.append((cmd, args))
        if cmd == "fex_login":
            return {"started": args.get("start_now"), "package": None if args.get("start_now") else "linux.signin"}
        if cmd == "fex_login_status":
            if self.error:
                raise self.error
            return next(self.states)
        if cmd == "fex_login_callback":
            return {"delivered": True}
        return {"stopped": True}


def test_cli_success_with_fake_agent(monkeypatch):
    from frameport.install import installer

    monkeypatch.setenv("FRAMEPORT_EXPERIMENTAL_FEX", "1")
    frame = FakeFrame([{"page_opened_at": 1}, {"signed_in": True, "callback_received_at": 2}])
    monkeypatch.setattr(cli, "_fex_target", lambda f: SimpleNamespace(frame=frame))
    monkeypatch.setattr(fexlogin.time, "sleep", lambda n: None)
    added = []
    monkeypatch.setattr(installer, "add_to_steam", lambda f, pkgs, rep: added.extend(pkgs) or {"state": "done"})
    res = CliRunner().invoke(cli.app, ["fex", "login", "--timeout", "25"])
    assert res.exit_code == 0, res.output
    assert "Put on the headset" in res.output and "Signed in." in res.output
    assert added == ["linux.signin"]
    assert frame.calls[0][1]["login_timeout"] == 25 and frame.calls[-1][0] == "fex_login_cancel"


@pytest.mark.parametrize("error", [RuntimeError("SSH lost"), KeyboardInterrupt()])
def test_cli_flow_cleanup_on_exception(monkeypatch, error):
    monkeypatch.setenv("FRAMEPORT_EXPERIMENTAL_FEX", "1")
    frame = FakeFrame(error=error)
    with pytest.raises(type(error)):
        fexlogin.login(frame, start_now=True, emit=lambda s: None)
    assert frame.calls[-1][0] == "fex_login_cancel"


def test_timeout_and_stop(monkeypatch):
    monkeypatch.setenv("FRAMEPORT_EXPERIMENTAL_FEX", "1")
    ticks = iter([0, 1, 3])
    monkeypatch.setattr(fexlogin.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(fexlogin.time, "sleep", lambda n: None)
    frame = FakeFrame([{}])
    with pytest.raises(RuntimeError, match="timed out"):
        fexlogin.login(frame, start_now=True, timeout=2, emit=lambda s: None)
    assert frame.calls[-1][0] == "fex_login_cancel"
    frame = FakeFrame()
    fexlogin.login(frame, stop=True, emit=lambda s: None)
    assert frame.calls == [("fex_login_cancel", {})]


def test_via_pc_forwards_callback_in_agent_stdin_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setenv("FRAMEPORT_EXPERIMENTAL_FEX", "1")
    events, opened, messages = [], [], []

    @contextmanager
    def callback():
        events.append("registered")
        inbox = queue.Queue()
        inbox.put(CALLBACK)
        try:
            yield inbox
        finally:
            events.append("restored")

    monkeypatch.setattr(fexlogin, "pc_callback", callback)
    monkeypatch.setattr(fexlogin, "open_page", opened.append)
    monkeypatch.setattr(fexlogin.time, "sleep", lambda n: None)
    frame = FakeFrame([{"page_opened_at": 1, "open_url": PAGE}, {"signed_in": True}])
    fexlogin.login(frame, start_now=True, via_pc=True, emit=messages.append)
    assert events == ["registered", "restored"] and opened == [PAGE]
    assert ("fex_login_callback", {"url": CALLBACK}) in frame.calls
    assert CALLBACK not in "\n".join(messages) and PAGE not in "\n".join(messages)
    assert_no_credentials(tmp_path)


def test_agent_callback_helper_uses_stdin(monkeypatch, tmp_path):
    agent = load(ROOT / "agent/frameport_agent.py", "login_agent")
    helper = tmp_path / "fexrift.py"
    helper.touch()
    monkeypatch.setattr(agent, "FEX_HELPER", str(helper))
    calls = []
    monkeypatch.setattr(agent.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or
                        SimpleNamespace(stdout=json.dumps({"ok": True, "result": {"delivered": True}})))
    assert agent.cmd_fex_login_callback({"url": CALLBACK})["delivered"]
    assert CALLBACK not in repr(calls[0][0]) and json.loads(calls[0][1]["input"])["url"] == CALLBACK
