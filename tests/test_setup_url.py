"""The static setup URL: a Frame asks the pairing server (/hello), the user allows it, /wait hands over the code,
and bootstrap/setup.sh then runs the same bootstrap.sh as the typed line."""
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from frameport.frame import pairing
from frameport.frame.pairing import PairingServer, announce_properties, ask_digits, pc_words

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def server():
    srv = PairingServer(announce=False, host="127.0.0.1").start()
    yield srv
    srv.stop()


def get(srv, path, timeout=10):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}{path}", timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def hello(srv, nonce="abcdef0123456789", host="steamframe"):
    status, body = get(srv, f"/hello?host={host}&nonce={nonce}")
    assert status == 200
    return json.loads(body)


def test_allow_hands_out_the_code(server):
    asked = []
    server.on_ask = asked.append
    reply = hello(server)
    assert reply["digits"] == ask_digits("abcdef0123456789") and reply["words"] == pc_words()
    assert asked and asked[0].frame == "steamframe" and asked[0].digits == reply["digits"]
    threading.Timer(0.3, server.decide, (reply["id"], True)).start()
    status, body = get(server, f"/wait?id={reply['id']}")
    assert status == 200 and json.loads(body) == {"code": server.code}


def test_deny_and_limits(server):
    reply = hello(server)
    server.decide(reply["id"], False)
    assert get(server, f"/wait?id={reply['id']}")[0] == 403
    assert get(server, "/wait?id=nope")[0] == 404
    for i in range(pairing.MAX_ASKS):
        hello(server, nonce=f"nonce{i:011d}")
    assert get(server, "/hello?host=x&nonce=0123456789abcdef")[0] == 429
    assert get(server, "/hello?host=x&nonce=short")[0] == 400
    # nothing about the code without it
    assert get(server, "/bootstrap.sh")[0] == 403


def test_wait_times_out_politely(server, monkeypatch):
    monkeypatch.setattr(pairing, "WAIT", 0.2)
    reply = hello(server)
    assert get(server, f"/wait?id={reply['id']}")[0] == 202


def test_ping_and_announcement_never_carry_the_code(server):
    status, body = get(server, "/ping")
    info = json.loads(body)
    assert status == 200 and info["words"] == pc_words() and server.code not in body.decode()
    props = announce_properties("pc")
    assert server.code not in json.dumps(props) and props["words"] == pc_words()


def test_digits_match_setup_sh():
    """setup.sh computes the digits with this exact Python expression."""
    script = (ROOT / "bootstrap" / "setup.sh").read_text()
    expr = 'int(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:8], 16) % 10000:04d'
    assert expr in script
    nonce = "0f1e2d3c4b5a69788796a5b4"
    assert ask_digits(nonce) == f"{int(hashlib.sha256(nonce.encode()).hexdigest()[:8], 16) % 10000:04d}"


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash") or not shutil.which("curl"),
                    reason="needs bash and curl")
def test_setup_sh_end_to_end(tmp_path, monkeypatch):
    """The real setup.sh against a real server; bootstrap.sh is a stub that records the code it was served with."""
    stub_dir = tmp_path / "bootstrap"
    stub_dir.mkdir()
    marker = tmp_path / "ran.txt"
    (stub_dir / "bootstrap.sh").write_text(f'#!/usr/bin/env bash\necho "__PAIR_CODE__ __PC_URL__" > "{marker}"\n')
    monkeypatch.setattr(pairing, "bootstrap_dir", lambda: stub_dir)
    srv = PairingServer(announce=False, host="127.0.0.1")
    srv.on_ask = lambda ask: threading.Timer(0.3, srv.decide, (ask.id, True)).start()
    srv.start()
    try:
        r = subprocess.run(["bash", str(ROOT / "bootstrap" / "setup.sh"), "--pc", f"127.0.0.1:{srv.port}"],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert r.returncode == 0, r.stdout + r.stderr
        assert marker.read_text().split()[0] == srv.code
        digits = srv.asks[0].digits
        assert digits in r.stdout  # the terminal shows the same 4 digits as FramePort
    finally:
        srv.stop()


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="needs bash")
def test_setup_sh_denied_changes_nothing(tmp_path, monkeypatch):
    stub_dir = tmp_path / "bootstrap"
    stub_dir.mkdir()
    marker = tmp_path / "ran.txt"
    (stub_dir / "bootstrap.sh").write_text(f'#!/usr/bin/env bash\ntouch "{marker}"\n')
    monkeypatch.setattr(pairing, "bootstrap_dir", lambda: stub_dir)
    srv = PairingServer(announce=False, host="127.0.0.1")
    srv.on_ask = lambda ask: threading.Timer(0.2, srv.decide, (ask.id, False)).start()
    srv.start()
    try:
        r = subprocess.run(["bash", str(ROOT / "bootstrap" / "setup.sh"), "--pc", f"127.0.0.1:{srv.port}"],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert r.returncode == 1 and "Not allowed" in r.stdout and not marker.exists()
    finally:
        srv.stop()


def test_announcement_registers_and_goes(monkeypatch):
    """With zeroconf: the service is registered while the server runs and withdrawn when it stops."""
    events = []

    class FakeZC:
        def __init__(self, **kw):
            pass

        def register_service(self, info, **kw):
            events.append(("register", info.name, dict(info.properties)))

        def unregister_service(self, info):
            events.append(("unregister", info.name))

        def close(self):
            pass

    import zeroconf
    monkeypatch.setattr(zeroconf, "Zeroconf", FakeZC)
    srv = PairingServer(host="127.0.0.1").start()
    for _ in range(50):
        if events:
            break
        time.sleep(0.05)
    srv.stop()
    for _ in range(50):
        if len(events) > 1:
            break
        time.sleep(0.05)
    assert events[0][0] == "register" and events[0][1].endswith(pairing.SERVICE)
    assert all(srv.code.encode() not in v for v in events[0][2].values())
    assert events[-1][0] == "unregister"


def test_first_key_is_made_once_under_concurrency():
    """The setup page's announcement and its text ask for the app key at the same moment on a first start."""
    from frameport.frame.connection import app_public_key

    keys, errors = [], []

    def get_key():
        try:
            keys.append(app_public_key())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=get_key) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(set(keys)) == 1


def test_cli_pair_does_not_announce(monkeypatch):
    """`frameport frame pair` can't answer the setup URL's requests (no Allow button), so it must not announce
    itself: a Frame running the setup URL would find it and wait for an answer that never comes."""
    from typer.testing import CliRunner

    from frameport import cli

    made = []

    class Fake:
        def __init__(self, **kw):
            made.append(kw)
            self.paired = []
            self.one_liner = "curl -fsS 127.0.0.1:8765/code | bash"

        def start(self):
            return self

        def stop(self):
            pass

    monkeypatch.setattr(pairing, "PairingServer", Fake)
    result = CliRunner().invoke(cli.app, ["frame", "pair", "--timeout", "0"])
    assert result.exit_code == 1 and "timed out" in result.output
    assert made and made[0].get("announce") is False


def test_open_requests_expire_when_polling_stops(server):
    """A Frame that stopped polling /wait (setup.sh ended) no longer blocks new requests, and its card goes."""
    gone = []
    server.on_expire = gone.extend
    for i in range(pairing.MAX_ASKS):
        hello(server, nonce=f"nonce{i:011d}")
    assert get(server, "/hello?host=x&nonce=0123456789abcdef")[0] == 429
    stale = server.asks[0]
    stale.seen -= 2 * pairing.WAIT + 1
    assert stale not in server.open_asks()  # the UI drops it at once
    status, _ = get(server, "/hello?host=x&nonce=0123456789abcdef")
    assert status == 200 and stale not in server.asks and stale.state == "expired" and gone == [stale]
    server.decide(stale.id, True)  # Allow on a stale toast: nothing
    assert stale.state == "expired"
    assert get(server, f"/wait?id={stale.id}")[0] == 404


def test_polled_and_decided_requests(server):
    now = time.time()
    a = pairing.Ask("a", "f", "1.2.3.4", "0000", created=now - 10 * pairing.WAIT)
    a.waiting = 1  # a /wait is held right now: still alive however old
    b = pairing.Ask("b", "f", "1.2.3.4", "0000", state="allowed", created=now - 60)
    b.decided = now - 60
    c = pairing.Ask("c", "f", "1.2.3.4", "0000", state="denied", created=now - pairing.DECIDED_KEEP - 100)
    c.decided = now - pairing.DECIDED_KEEP - 1
    server.asks.extend([a, b, c])
    assert server.prune(now) == [c] and server.asks == [a, b]
    assert server.open_asks() == [a]
    a.waiting = 0
    assert server.prune(now) == [a]


def test_polling_keeps_a_request_alive(server, monkeypatch):
    monkeypatch.setattr(pairing, "WAIT", 0.2)
    reply = hello(server)
    ask = server.asks[0]
    ask.seen -= 100
    assert get(server, f"/wait?id={reply['id']}")[0] == 202
    assert time.time() - ask.seen < 1 and ask.waiting == 0 and server.open_asks() == [ask]


def test_expiry_loop_runs(monkeypatch):
    monkeypatch.setattr(pairing, "EXPIRE_EVERY", 0.05)
    gone = []
    srv = PairingServer(announce=False, host="127.0.0.1", on_expire=gone.extend).start()
    try:
        srv.asks.append(pairing.Ask("x", "f", "1.2.3.4", "0000", created=time.time() - 3 * pairing.WAIT))
        for _ in range(100):
            if gone:
                break
            time.sleep(0.02)
        assert [a.id for a in gone] == ["x"] and not srv.asks
    finally:
        srv.stop()


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash") or not shutil.which("curl"),
                    reason="needs bash and curl")
def test_setup_sh_says_why_when_full(server):
    for i in range(pairing.MAX_ASKS):
        hello(server, nonce=f"nonce{i:011d}")
    r = subprocess.run(["bash", str(ROOT / "bootstrap" / "setup.sh"), "--pc", f"127.0.0.1:{server.port}"],
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 1 and "already has several setup requests" in r.stdout
    assert "didn't answer" not in r.stdout
