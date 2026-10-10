"""SSH connection to a Steam Frame and the remote agent protocol.

Authentication: FramePort's own keys first (<user data>/ssh/id_ed25519, installed by the bootstrap script, and
id_rsa, registered through Valve's devkit pairing, which only takes RSA keys), then the user's SSH agent/keys, then a
password if given.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import posixpath
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import paramiko

from ..core.paths import agent_file, artifacts_dir, ssh_dir, user_data_dir, write_atomic

log = logging.getLogger(__name__)

REMOTE_AGENT_DIR = ".local/share/frameport/agent"


def agent_version_of(text: str) -> int:
    """AGENT_VERSION from an agent's source (0 if missing)."""
    m = re.search(r"^AGENT_VERSION\s*=\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else 0


class FrameNotPaired(ConnectionError):
    """SSH works but none of FramePort's ways in was accepted: the Frame hasn't run the first-time setup (or the
    password was wrong). The message keeps "authentication failed" for older callers."""

    def __init__(self, tried_password: bool, details: list[str]):
        self.tried_password = tried_password
        self.details = details
        reason = ("the password was refused" if tried_password
                  else "this Frame doesn't know FramePort yet")
        super().__init__(f"SSH authentication failed: {reason}. Run the first-time setup on the Frame (Steam Frame → "
                         "Show setup command)." + (f" ({'; '.join(details)})" if details else ""))


def _no_auth_methods(exc: BaseException) -> bool:
    """paramiko ends a key/agent attempt that had nothing to offer (no SSH keys on this PC, no agent) with a plain
    SSHException, not an AuthenticationException: it's a failed login all the same, not a network problem."""
    return isinstance(exc, paramiko.SSHException) and "no authentication methods available" in str(exc).lower()


def app_key() -> paramiko.Ed25519Key:
    path = ssh_dir() / "id_ed25519"
    if not path.exists():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                 serialization.NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # private from the first byte
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        pub = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        (ssh_dir() / "id_ed25519.pub").write_text(pub.decode() + " frameport\n")
    return paramiko.Ed25519Key.from_private_key_file(str(path))


def app_public_key() -> str:
    app_key()
    return (ssh_dir() / "id_ed25519.pub").read_text().strip()


def devkit_key() -> paramiko.RSAKey:
    """FramePort's RSA key for Valve's devkit pairing (frame/devkit.py): its service accepts only ssh-rsa keys."""
    path = ssh_dir() / "id_rsa"
    if not path.exists():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                 serialization.NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        pub = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        (ssh_dir() / "id_rsa.pub").write_text(pub.decode() + " frameport\n")
    return paramiko.RSAKey.from_private_key_file(str(path))


def devkit_public_key() -> str:
    devkit_key()
    return (ssh_dir() / "id_rsa.pub").read_text().strip()


def app_keys() -> list[paramiko.PKey]:
    """The keys FramePort logs in with: the Ed25519 key, plus the RSA key once devkit pairing created it."""
    keys: list[paramiko.PKey] = [app_key()]
    if (ssh_dir() / "id_rsa").exists():
        keys.append(devkit_key())
    return keys


@dataclass
class FrameTarget:
    host: str
    user: str = "steamos"
    port: int = 22
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or self.host


_saved_cache: tuple[float, list] = (-1.0, [])


def saved_targets() -> list[FrameTarget]:
    """The remembered Frames (read again only when frames.json changed: the sidebar asks several times a second).
    FramePort running on a Frame remembers that Frame itself (127.0.0.1) the first time."""
    if not (user_data_dir() / "frames.json").exists():
        return _local_frame()
    return _read_saved()


def _read_saved() -> list[FrameTarget]:
    global _saved_cache
    path = user_data_dir() / "frames.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    if mtime != _saved_cache[0]:
        try:
            _saved_cache = (mtime, [d for d in json.loads(path.read_text()) if isinstance(d, dict)])
        except (OSError, ValueError):
            return []
    try:
        return [FrameTarget(**d) for d in _saved_cache[1]]
    except TypeError:
        return []


def is_unreachable(exc: BaseException) -> bool:
    """The Frame isn't there at all (asleep, off the network): retrying an upload at once won't help."""
    return isinstance(exc, (ConnectionRefusedError, TimeoutError, paramiko.ssh_exception.NoValidConnectionsError))


def _local_frame() -> list[FrameTarget]:
    """On a Frame with nothing remembered yet: this Frame, with FramePort's key authorized for the local user."""
    from . import local

    if not local.on_frame():
        return []
    try:
        local.authorize_self(app_public_key())
    except OSError:
        pass
    target = FrameTarget("127.0.0.1", local.local_user(), 22, local.LOCAL_NAME)
    save_target(target)
    return [target]


def replace_target(old_host: str, target: FrameTarget) -> None:
    """The remembered Frame at old_host is now at target.host (same device: its SSH host key matched)."""
    items = [FrameTarget(target.host, t.user, t.port, t.name) if t.host == old_host else t for t in _read_saved()]
    if target.host not in [t.host for t in items]:
        items.insert(0, target)
    seen, out = set(), []
    for t in items:
        if t.host not in seen:
            seen.add(t.host)
            out.append(t)
    write_atomic(user_data_dir() / "frames.json", json.dumps([t.__dict__ for t in out], indent=2))


def server_key(host: str, port: int = 22, timeout: float = 5) -> paramiko.PKey | None:
    """The SSH host key a device presents (no login)."""
    import socket

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        return transport.get_remote_server_key()
    except (OSError, paramiko.SSHException, EOFError):
        return None
    finally:
        transport.close()


_last_relocate = -1e9


def save_target(target: FrameTarget) -> None:
    items = [t for t in _read_saved() if t.host != target.host]
    items.insert(0, target)
    write_atomic(user_data_dir() / "frames.json", json.dumps([t.__dict__ for t in items], indent=2))


# direct links to the Frame, fastest first (interface → what it is)
FAST_LINKS = {"usb0": "USB cable", "wlanap": "the Frame's own Wi-Fi hotspot"}


def bundled_agent_version() -> int | None:
    """AGENT_VERSION of the agent this app ships (it's uploaded to the Frame whenever it differs)."""
    try:
        m = re.search(r"^AGENT_VERSION = (\d+)", agent_file().read_text(), re.M)
        return int(m.group(1)) if m else None
    except OSError:
        return None


RUN_TIMEOUT = 120  # seconds: default bound for one remote command's output


NOT_IN_LIBRARY = "not in the Frame's Steam library"  # agent cmd_launch's error when the shortcut is missing


class AgentFailed(RuntimeError):
    pass


SFTP_CHANNELS = 3  # OpenSSH allows 10 channels per connection (MaxSessions): leave room for commands


class SftpPool:
    """A few SFTP channels shared by all threads, lent out per operation (or while a remote file is open).
    paramiko's SFTPClient must not be used by two threads at once; one channel per thread was kept open for good and
    long-lived GUI worker threads ran the connection out of channels ("ChannelException(2, 'Connect failed')").
    A thread that already holds a channel reuses it for nested calls (no deadlock)."""

    def __init__(self, open_channel, size: int = SFTP_CHANNELS):
        self._open, self.size = open_channel, size
        self._free: list = []
        self._all: list = []
        self._cv = threading.Condition()
        self._local = threading.local()

    @contextlib.contextmanager
    def lease(self):
        loc = self._local
        if getattr(loc, "depth", 0):
            loc.depth += 1
            try:
                yield loc.client
            finally:
                loc.depth -= 1
            return
        with self._cv:
            while True:
                while self._free:
                    client = self._free.pop()
                    if not client.sock.closed:
                        break
                    self._all.remove(client)
                else:
                    client = None
                if client is not None or len(self._all) < self.size:
                    break
                self._cv.wait()
            if client is None:
                client = self._open()
                self._all.append(client)
        loc.client, loc.depth = client, 1
        try:
            yield client
        finally:
            loc.client, loc.depth = None, 0
            with self._cv:
                self._free.append(client)
                self._cv.notify()

    def close(self) -> None:
        with self._cv:
            clients, self._all, self._free = self._all, [], []
            self._cv.notify_all()
        for c in clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001 - closing anyway
                pass


class _LeasedFile:
    """A remote file that holds its SFTP channel until it's closed."""

    def __init__(self, f, lease):
        self._f, self._lease = f, lease

    def __getattr__(self, name):
        return getattr(self._f, name)

    def __iter__(self):
        return iter(self._f)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if self._lease is not None:
            lease, self._lease = self._lease, None
            try:
                self._f.close()
            finally:
                lease.__exit__(None, None, None)


class SftpProxy:
    """frame.sftp: looks like a paramiko SFTPClient; each call borrows a channel from the pool."""

    def __init__(self, pool: SftpPool):
        self._pool = pool

    def open(self, *args, **kwargs):
        lease = self._pool.lease()
        client = lease.__enter__()
        try:
            f = client.open(*args, **kwargs)
        except BaseException:
            lease.__exit__(None, None, None)
            raise
        return _LeasedFile(f, lease)

    def __getattr__(self, name):
        def call(*args, **kwargs):
            with self._pool.lease() as client:
                return getattr(client, name)(*args, **kwargs)
        return call


@dataclass
class Frame:
    target: FrameTarget
    password: str | None = None
    client: paramiko.SSHClient | None = None
    _pool: SftpPool | None = None  # SFTP channels shared by all threads (see sftp)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _agent_digest: str = ""  # the agent version known to be on the Frame (checked once per connection)
    _video_codec_digest: str = ""
    _video_codec_lock: threading.Lock = field(default_factory=threading.Lock)
    home: str = ""

    # ------------------------------------------------------------------ connect
    def connect(self, timeout: float = 10) -> Frame:
        try:
            return self._connect(timeout)
        except OSError:
            if self.target.host in ("127.0.0.1", "localhost"):  # FramePort on the Frame itself: sshd isn't running
                raise ConnectionError("FramePort runs on this Frame but can't reach it over SSH: turn on Developer "
                                      "Mode (Steam → Settings → System → Developer Mode), then try again") from None
            moved = self.relocate()  # the router gave the Frame a new address (it's remembered by address)
            if not moved:
                raise
            log.info("Frame moved from %s to %s", self.target.host, moved)
            old, self.target.host = self.target.host, moved
            self._connect(timeout)
            replace_target(old, self.target)
            return self

    def relocate(self) -> str | None:
        """The Frame's new address when it no longer answers at the remembered one: a device on the network whose
        SSH host key is the one this PC saw at the old address (checked before logging in). At most once a minute (the
        GUI retries every few seconds while a Frame sleeps)."""
        global _last_relocate
        if time.monotonic() - _last_relocate < 60 or self.target.host not in [t.host for t in saved_targets()]:
            return None
        _last_relocate = time.monotonic()
        hk = paramiko.HostKeys()
        try:
            hk.load(str(ssh_dir() / "known_hosts"))
        except OSError:
            return None
        known = hk.lookup(self.target.host if self.target.port == 22 else f"[{self.target.host}]:{self.target.port}")
        if not known:
            return None
        from .discovery import browse

        for f in browse(seconds=3.0):
            if f.host == self.target.host or f.port != self.target.port:
                continue
            key = server_key(f.host, f.port)
            if key is not None and known.get(key.get_name()) == key:
                return f.host
        return None

    def _connect(self, timeout: float) -> Frame:
        known = ssh_dir() / "known_hosts"
        known.touch(exist_ok=True)
        kwargs = dict(hostname=self.target.host, port=self.target.port, username=self.target.user, timeout=timeout,
                      banner_timeout=timeout, auth_timeout=timeout)
        errors = []
        attempts = [("app_key", k) for k in app_keys()] + [("agent", None), ("password", None)]
        for attempt, pkey in attempts:
            if attempt == "password" and not self.password:
                continue
            client = paramiko.SSHClient()  # a fresh client per attempt: a failed one keeps its transport otherwise
            client.load_host_keys(str(known))
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # trust on first use (pairing)
            try:
                if attempt == "app_key":
                    client.connect(pkey=pkey, allow_agent=False, look_for_keys=False, **kwargs)
                elif attempt == "agent":
                    client.connect(allow_agent=True, look_for_keys=True, **kwargs)
                else:
                    client.connect(password=self.password, allow_agent=False, look_for_keys=False, **kwargs)
                break
            except paramiko.AuthenticationException as exc:
                client.close()
                errors.append(f"{attempt}: {exc}")
            except paramiko.SSHException as exc:
                client.close()
                if not _no_auth_methods(exc):
                    raise
                # (Reddit, 2026-10-03: this ended the loop before the password was tried)
                errors.append(f"{attempt}: {exc}")
            except BaseException:
                client.close()
                raise
        else:
            raise FrameNotPaired(bool(self.password), errors)
        client.save_host_keys(str(known))
        transport = client.get_transport()
        transport.set_keepalive(15)
        self.client = client
        self.home = self.run("printf %s \"$HOME\"")[1].strip() or f"/home/{self.target.user}"
        return self

    def addresses(self) -> list[tuple[str, str]]:
        """(interface, IPv4) of every link the Frame has up."""
        out = self.run("ip -4 -o addr show up 2>/dev/null")[1]
        return re.findall(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)/", out, re.M)

    def fast_link(self, timeout: float = 1.5) -> tuple[Frame, str]:
        """A second connection over the fastest direct link, for bulk uploads: the USB cable (usb0) or the Frame's own
        hotspot (wlanap; the PC joins it with a Wi-Fi adapter, e.g. Valve's USB dongle) — several times faster than
        both going through the home router (measured 83-97 vs 15-18 MB/s). Used only if this PC can reach it and it
        presents the same SSH host key as this connection. Returns (frame, link name); (self, "") if none."""
        import socket

        if self.target.host in ("127.0.0.1", "localhost"):  # FramePort on the Frame itself: nothing is faster
            return self, ""
        key = self.client.get_transport().get_remote_server_key()
        for iface in FAST_LINKS:
            for name, ip in self.addresses():
                if name != iface or ip == self.target.host:
                    continue
                try:
                    socket.create_connection((ip, self.target.port), timeout=timeout).close()
                    other = Frame(FrameTarget(ip, self.target.user, self.target.port), self.password)
                    other.connect(timeout=5)
                except (OSError, ConnectionError, paramiko.SSHException):
                    continue
                if other.client.get_transport().get_remote_server_key() != key:  # not our Frame: don't use it
                    other.close()
                    continue
                return other, f"{FAST_LINKS[iface]} ({ip})"
        return self, ""

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool:
            pool.close()
        if self.client:
            self.client.close()
        self.client = None
        self._agent_digest = ""
        self._video_codec_digest = ""

    def alive(self) -> bool:
        """The SSH connection itself still works (a failed agent command doesn't mean the Frame is gone)."""
        transport = self.client.get_transport() if self.client else None
        return bool(transport and transport.is_active())

    @property
    def sftp(self) -> SftpProxy:
        """SFTP for any thread (see SftpPool): the same calls as paramiko's SFTPClient."""
        with self._lock:
            if self._pool is None:
                self._pool = SftpPool(lambda: self.client.open_sftp())
            return SftpProxy(self._pool)

    def run(self, command: str, stdin: str | None = None,
            timeout: float | None = RUN_TIMEOUT) -> tuple[int, str, str]:
        """Run a shell command. `timeout` (s) bounds waiting for output, so a dead link can't hang the caller;
        None only for commands that legitimately run long (installs, launch tests)."""
        chan_in, out, err = self.client.exec_command(command, timeout=timeout)
        if stdin is not None:
            chan_in.write(stdin)
            chan_in.channel.shutdown_write()
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        return out.channel.recv_exit_status(), o, e

    def install_key(self) -> None:
        """After a password login: add FramePort's public key to authorized_keys."""
        pub = app_public_key()
        self.run("mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
                 f"grep -qxF {sh_quote(pub)} ~/.ssh/authorized_keys || echo {sh_quote(pub)} >> ~/.ssh/authorized_keys"
                 " && chmod 600 ~/.ssh/authorized_keys")

    # ------------------------------------------------------------------ agent
    def ensure_agent(self) -> str:
        local = agent_file()
        text = local.read_bytes()
        digest = hashlib.sha256(text).hexdigest()[:16]
        remote_dir = posixpath.join(self.home, REMOTE_AGENT_DIR)
        remote = posixpath.join(remote_dir, "frameport_agent.py")
        if self._agent_digest == digest:
            self._ensure_video_codec_if_supported(text)
            return remote
        code, out, _ = self.run(f"sha256sum {sh_quote(remote)} 2>/dev/null | cut -c1-16; "
                                f"grep -m1 '^AGENT_VERSION' {sh_quote(remote)} 2>/dev/null")
        lines = out.split("\n")
        remote_version = agent_version_of(lines[1] if len(lines) > 1 else "")
        if lines[0].strip() != digest and remote_version > agent_version_of(text.decode("utf-8", "replace")):
            # a newer FramePort (another PC, or FramePort on the Frame) installed a newer agent: keep it. Agents
            # stay compatible with older apps; replacing it broke the newer one's launchers (2026-10-04: an older
            # app put agent 43 back over 45 every few seconds)
            self._agent_digest = digest
            self._ensure_video_codec_if_supported(text)
            return remote
        if lines[0].strip() != digest:
            self.run(f"mkdir -p {sh_quote(remote_dir)}")
            tmp = f"{remote}.{os.getpid()}.{threading.get_ident()}.tmp"  # two threads never share a temp file
            with self.sftp.open(tmp, "wb") as f:
                f.write(text)
            code, _, err = self.run(f"mv {sh_quote(tmp)} {sh_quote(remote)} && chmod 755 {sh_quote(remote)}")
            if code:
                raise AgentFailed(f"couldn't install the FramePort agent on the Frame: {err.strip()[-300:]}")
        self._agent_digest = digest
        self._ensure_video_codec_if_supported(text)
        return remote

    def _ensure_video_codec_if_supported(self, agent_source: bytes) -> None:
        if agent_version_of(agent_source.decode("utf-8", "replace")) < 71:
            return
        try:
            self.ensure_video_codec()
        except Exception as exc:
            # Optional acceleration must never block pairing, installs or play.
            log.warning("Hardware video decoder unavailable; retaining stock codecs: %s", exc)

    def ensure_video_codec(self) -> None:
        """Deploy once per Frame, independently of APKs and per-game recipes."""
        import io
        import zipfile

        with self._video_codec_lock:
            directory = artifacts_dir() / "hevc"
            raw = (directory / "manifest.json").read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if self._video_codec_digest == digest:
                return
            manifest = json.loads(raw)
            status = self.agent("video_codec_status", ensure=False)
            if status.get("digest") != digest and status.get("revision", 0) <= manifest.get("revision", 1):
                bundle = io.BytesIO()
                with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("manifest.json", raw)
                    for name, expected in manifest["files"].items():
                        path = directory / (name + ".txt" if name == "podman.py" else name)
                        data = path.read_bytes()
                        if hashlib.sha256(data).hexdigest() != expected:
                            raise AgentFailed(f"video codec asset checksum mismatch: {name}")
                        archive.writestr(name, data)
                self.agent("install_video_codec", ensure=False, digest=digest,
                           bundle=base64.b64encode(bundle.getvalue()).decode("ascii"))
            self._video_codec_digest = digest

    def agent(self, command: str, timeout: float | None = 600, ensure: bool = True, **args):
        """Run an agent command. ensure=False uses the agent already on the Frame (never re-uploads it)."""
        remote = self.ensure_agent() if ensure else posixpath.join(self.home, REMOTE_AGENT_DIR, "frameport_agent.py")
        code, out, err = self.run(f"python3 {sh_quote(remote)} {command}", stdin=json.dumps(args), timeout=timeout)
        line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("{")), "")
        try:
            reply = json.loads(line)
        except ValueError:
            raise AgentFailed(f"agent {command}: no reply (exit {code}) {err[-400:]}") from None
        if not reply.get("ok"):
            raise AgentFailed(reply.get("error", "unknown agent error"))
        return reply["result"]

    # ------------------------------------------------------------------ files
    def mkdirs(self, dirs) -> None:
        """Create many remote folders with one command."""
        dirs = sorted(set(dirs))
        if dirs:
            self.run("xargs -0 mkdir -p", stdin="\0".join(dirs))

    def put(self, local: Path, remote: str, progress=None, resume: bool = True, mkdir: bool = True,
            attempts: int = 3) -> None:
        """Upload with resume: data goes to <remote>.part, appended from the existing size, then renamed. progress may
        raise (e.g. Cancelled): the .part stays and the next put continues from there. A failed transfer is resumed
        up to `attempts` times (Windows: an occasional OSError [Errno 22] mid-upload that the next try got past)."""
        for attempt in range(attempts):
            try:
                return self._put(Path(local), remote, progress, resume, mkdir)
            except OSError as exc:
                if attempt == attempts - 1 or is_unreachable(exc):
                    raise
                log.warning("upload of %s failed (%s), resuming", Path(local).name, exc)
                time.sleep(2)
                if not self.alive():
                    self.close()
                    self.connect()

    def _put(self, local: Path, remote: str, progress, resume: bool, mkdir: bool) -> None:
        size = local.stat().st_size
        part = remote + ".part"
        if mkdir:
            self.run(f"mkdir -p {sh_quote(posixpath.dirname(remote))}")
        offset = 0
        if resume:
            try:
                offset = self.sftp.stat(part).st_size
                if offset > size:
                    offset = 0
            except OSError:
                offset = 0
        with open(local, "rb") as src, self.sftp.open(part, "ab" if offset else "wb") as dst:
            dst.set_pipelined(True)
            src.seek(offset)
            done = offset
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, size)
        self.run(f"mv -f {sh_quote(part)} {sh_quote(remote)}")

    def put_tar(self, files: list[tuple[Path, str]], remote_root: str, progress=None) -> None:
        """Upload many small files in one stream (tar → `tar -x` on the Frame): far faster than one SFTP transfer per
        file. files: (local path, path relative to remote_root). progress(done_bytes, rel) may raise to stop; files that
        arrived stay (a cut-off last file has the wrong size, so it's sent again next time)."""
        import tarfile

        stdin, stdout, stderr = self.client.exec_command(
            f"mkdir -p {sh_quote(remote_root)} && tar -xf - -C {sh_quote(remote_root)}")
        done = 0
        try:
            with tarfile.open(fileobj=stdin, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                for local, rel in files:
                    tar.add(str(local), arcname=rel, recursive=False)
                    done += Path(local).stat().st_size
                    if progress:
                        progress(done, rel)
        finally:
            try:
                stdin.channel.shutdown_write()
            except Exception:  # noqa: BLE001
                pass
            code = stdout.channel.recv_exit_status()
        if code:
            raise OSError(f"remote tar failed ({code}): {stderr.read().decode(errors='replace')[-300:]}")

    def get_text(self, remote: str, max_bytes: int = 64 << 20) -> str:
        with self.sftp.open(remote, "rb") as f:
            size = f.stat().st_size
            if size > max_bytes:
                f.seek(size - max_bytes)
            return f.read().decode("utf-8", "replace")

    def exists(self, remote: str) -> bool:
        try:
            self.sftp.stat(remote)
            return True
        except OSError:
            return False

    def is_dir(self, remote: str) -> bool:
        try:
            return stat.S_ISDIR(self.sftp.stat(remote).st_mode)
        except OSError:
            return False


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def parse_target(text: str) -> FrameTarget:
    m = re.fullmatch(r"(?:(?P<user>[^@]+)@)?(?P<host>[^:]+)(?::(?P<port>\d+))?", text.strip())
    if not m:
        raise ValueError(f"bad Frame address {text!r}")
    return FrameTarget(m["host"], m["user"] or "steamos", int(m["port"] or 22))
