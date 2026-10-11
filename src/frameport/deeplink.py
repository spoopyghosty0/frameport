"""One-click installs from web pages: the "Install with FrameDrop" button protocol (no Flet).

FrameDrop (framedropvr.com) defined it: a page links to `https://framedropvr.com/install?manifest=<URL>` (or
`?url=<direct file URL>`), which opens `framedrop://install?manifest=…|url=…`. The manifest is JSON:
`{"schema": "framedrop.install/v1", "name": "<Steam title>", "files": [{"url": "https://…/x.apk", "sha256": "…"}]}`
with an arm64 APK or a zipped Linux ARM64 build. FramePort accepts the same links (`framedrop://` when it is the
system's handler, its own `frameport://`, FramePort's `https://frameport.app/install?…` button page, or an https link
pasted into "Install from link…"), follows the same
rules (https only, http only on this PC, no credentials, no LAN addresses, a URL that ends in a file name) and
always asks before it downloads anything a web page sent. `urlhandler.py` registers the schemes.

The homepage's example button points at one fixed demo manifest (DEMO_MANIFEST, also published on the site): every
form of that link becomes a request flagged `demo`, which never touches the network (fetch_manifest answers with
demo_manifest(), download refuses it) and the GUI answers with a placeholder dialog (ui/views/link_dialog.py).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .core.events import Reporter

SCHEMES = ("framedrop", "frameport")
# the https pages an install button links to: FramePort's own landing page, and FrameDrop's (older buttons)
WEB_HOSTS = ("frameport.app", "www.frameport.app", "framedropvr.com", "www.framedropvr.com")
SCHEMA = "framedrop.install/v1"
MAX_MANIFEST = 256 << 10
MAX_FILES = 16
MAX_DESCRIPTION = 2000
MAX_ICON = 2 << 20
LOOPBACK = ("localhost", "127.0.0.1", "[::1]", "::1")
APK, LINUX, EXE, OBB = "apk", "linux", "exe", "obb"
DEMO_MANIFEST = "https://frameport.app/demo/cool-game.json"  # the homepage's example button
DEMO_NAME = "Cool Game"
LINUX_EXTS = (".zip", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tar", ".appimage")


class LinkError(ValueError):
    """A link or manifest FramePort won't use (the message says why, in plain words)."""


@dataclass
class InstallRequest:
    manifest_url: str | None = None
    file_url: str | None = None
    local_manifest: str | None = None  # a .framedrop.json dropped on the window
    demo: bool = False  # the homepage's example button (DEMO_MANIFEST): nothing is fetched or installed

    @property
    def source(self) -> str:
        return self.manifest_url or self.file_url or self.local_manifest or ""


@dataclass
class ManifestFile:
    url: str
    sha256: str | None = None

    @property
    def filename(self) -> str:
        return filename_of(self.url)

    @property
    def kind(self) -> str | None:
        return classify(self.filename)


@dataclass
class Manifest:
    name: str
    files: list[ManifestFile] = field(default_factory=list)
    source: str = ""  # the manifest's (or the file's) URL
    direct: bool = False  # made from a bare file link (the name is only a guess from the file name)
    # FramePort-only extension (FrameDrop ignores it): "frameport": {"description": "…", "icon": "https://….png"}
    description: str = ""
    icon: str | None = None
    demo: bool = False  # demo_manifest(): never downloaded

    @property
    def host(self) -> str:
        return (urlsplit(self.source).hostname or "") if "://" in self.source else ""

    @property
    def main(self) -> ManifestFile:
        """The file that is installed (an APK first, then a Linux build, then a Windows program)."""
        for kind in (APK, LINUX, EXE):
            for f in self.files:
                if f.kind == kind:
                    return f
        raise LinkError("The link has no APK, Linux build (.zip/.tar/AppImage) or Windows program (.exe) to install.")


# ------------------------------------------------------------------------------------------------- links
def filename_of(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def classify(filename: str) -> str | None:
    name = filename.lower()
    if name.endswith(".apk"):
        return APK
    if name.endswith(".obb"):
        return OBB
    if name.endswith(".exe"):
        return EXE
    if name.endswith(LINUX_EXTS):
        return LINUX
    return None


def _private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or \
        ip.is_unspecified


def check_url(url: str, what: str = "link", resolve: bool = False) -> str:
    """The URL if FramePort may fetch it, else LinkError. FrameDrop's rules: https (http only on this PC), no
    user:password, no LAN address, a path that ends in a file name. `resolve` also refuses names that resolve to a LAN
    address (checked right before a download)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise LinkError(f"The {what} isn't a valid web address.") from None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host:
        raise LinkError(f"The {what} must be an https:// address (got {parts.scheme or 'none'}).")
    if parts.username or parts.password:
        raise LinkError(f"The {what} contains a user name or password: FramePort doesn't use those.")
    loopback = host in LOOPBACK or host == "::1"
    if parts.scheme == "http" and not loopback:
        raise LinkError(f"The {what} must use https:// (http:// is only allowed on this PC, for testing).")
    if parts.scheme == "https" and not loopback and _private(host):
        raise LinkError(f"The {what} points into a local network ({host}): FramePort only fetches public addresses.")
    if not parts.path or parts.path.endswith("/") or not filename_of(url):
        raise LinkError(f"The {what} must end in a file name, not a folder.")
    if resolve and not loopback and not _private(host):
        try:
            addrs = {a[4][0] for a in socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)}
        except OSError:
            addrs = set()  # the download itself reports the lookup failure
        if any(_private(a) for a in addrs):
            raise LinkError(f"{host} resolves to a local network address: FramePort only fetches public addresses.")
    return url.strip()


def is_demo(url: str | None) -> bool:
    """Whether a manifest address is the homepage's demo manifest (https, frameport.app, that path, nothing else)."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme.lower() == "https" and host in ("frameport.app", "www.frameport.app") and port is None \
        and not parts.username and not parts.password and parts.path == urlsplit(DEMO_MANIFEST).path \
        and not parts.query and not parts.fragment


def demo_request() -> InstallRequest:
    return InstallRequest(manifest_url=DEMO_MANIFEST, demo=True)


def demo_manifest() -> Manifest:
    """What the demo link "installs": built here, never fetched (site/public/demo/cool-game.json says the same)."""
    return Manifest(DEMO_NAME, [ManifestFile("https://frameport.app/demo/cool-game-arm64.apk")], DEMO_MANIFEST,
                    description="A placeholder game for the example \"Install with FramePort\" button. FramePort "
                    "recognises this link and shows a demo: nothing is downloaded or installed.",
                    demo=True)


def parse(text: str) -> InstallRequest:
    """framedrop://install?…, frameport://install?…, the https://framedropvr.com/install?… button link, a manifest
    URL (…json) or a direct file URL."""
    text = (text or "").strip().strip('"').strip("'")
    if not text:
        raise LinkError("Paste a link first.")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise LinkError("That isn't a link FramePort understands.") from None
    scheme = parts.scheme.lower()
    if is_demo(text):  # the demo manifest pasted on its own
        return demo_request()
    if scheme in SCHEMES or (scheme == "https" and (parts.hostname or "").lower() in WEB_HOSTS):
        action = (parts.netloc or parts.path.strip("/")) if scheme in SCHEMES else parts.path.strip("/")
        if action.lower().split("/")[0] != "install":
            raise LinkError("FramePort only understands install links (…/install?manifest=… or ?url=…).")
        query = parse_qs(parts.query)
        manifest, file = (query.get("manifest") or [None])[0], (query.get("url") or [None])[0]
        if manifest and is_demo(manifest):
            return demo_request()
        if manifest:
            return InstallRequest(manifest_url=check_url(manifest, "manifest address"))
        if file:
            return InstallRequest(file_url=check_url(file, "file address"))
        raise LinkError("The link doesn't say what to install (no manifest= or url=).")
    if scheme in ("https", "http"):
        url = check_url(text)
        name = filename_of(url).lower()
        if name.endswith(".json"):
            return InstallRequest(manifest_url=url)
        if classify(name) in (APK, LINUX, EXE):
            return InstallRequest(file_url=url)
        raise LinkError("The link isn't a FramePort/FrameDrop install link, a manifest (.json) or an APK/zip/exe.")
    if text.lower().endswith(".json") and Path(text).is_file():
        return InstallRequest(local_manifest=text)
    raise LinkError("That isn't a link FramePort understands.")


def make_link(manifest_url: str | None = None, file_url: str | None = None, scheme: str = "frameport") -> str:
    from urllib.parse import quote

    key, value = ("manifest", manifest_url) if manifest_url else ("url", file_url)
    return f"{scheme}://install?{key}={quote(value or '', safe='')}"


# ------------------------------------------------------------------------------------------------- manifest
def title_from_filename(filename: str) -> str:
    """A readable guess from a file name: "net.sourceforge.opencamera_96.apk" → "Opencamera",
    "Cool_Game-v1.2.3-arm64-v8a.apk" → "Cool Game" (the real title comes from the build once it's downloaded)."""
    stem = re.sub(r"(?i)(\.tar)?\.[a-z0-9]{2,8}$", "", filename)
    stem = re.sub(r"(?i)[-_.](arm64(-v8a)?|aarch64|x86_64|amd64|universal|release|signed|linux|android)\b", "", stem)
    stem = re.sub(r"(?i)[-_ ]v?\d+(\.\d+)*([-_.]?(beta|alpha|rc)\d*)?$", "", stem)  # a version number at the end
    if re.fullmatch(r"[a-z][a-z0-9_]*(\.[A-Za-z0-9_]+){2,}", stem):  # an Android package name: its last part
        stem = stem.rsplit(".", 1)[-1]
    words = re.sub(r"[-_.]+", " ", stem).strip()
    if words and words == words.lower():
        words = " ".join(w[:1].upper() + w[1:] for w in words.split())
    return words or filename


def manifest_from_data(data, source: str = "") -> Manifest:
    if not isinstance(data, dict):
        raise LinkError("The manifest isn't a JSON object.")
    if data.get("schema") != SCHEMA:
        raise LinkError(f"The manifest's schema is {data.get('schema')!r}; FramePort reads {SCHEMA!r}.")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise LinkError("The manifest has no name.")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise LinkError("The manifest lists no files.")
    if len(files) > MAX_FILES:
        raise LinkError(f"The manifest lists {len(files)} files; FramePort takes at most {MAX_FILES}.")
    out = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("url"), str):
            raise LinkError("A file in the manifest has no url.")
        sha = f.get("sha256")
        if sha is not None and (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha)):
            sha = None  # FrameDrop's example has a placeholder here; a wrong checksum is ignored like a missing one
        out.append(ManifestFile(check_url(f["url"], "file address"), sha.lower() if sha else None))
    m = Manifest(" ".join(name.split())[:120], out, source)
    m.main  # noqa: B018 - raises when nothing installable is listed
    extra = data.get("frameport")  # FramePort-only fields; anything wrong in them is ignored (they're optional)
    if isinstance(extra, dict):
        desc = extra.get("description")
        if isinstance(desc, str):
            m.description = desc.strip()[:MAX_DESCRIPTION]
        icon = extra.get("icon")
        if isinstance(icon, str):
            try:
                m.icon = check_url(icon, "icon address")
            except LinkError:
                pass
    return m


def fetch_manifest(req: InstallRequest) -> Manifest:
    """The manifest a request names (a direct file link becomes a one-file manifest titled after the file; the demo
    link's is built in, without a network request)."""
    if req.demo or is_demo(req.manifest_url):
        return demo_manifest()
    if req.file_url:
        name = filename_of(req.file_url)
        m = Manifest(title_from_filename(name), [ManifestFile(req.file_url)], req.file_url, direct=True)
        m.main  # noqa: B018
        return m
    if req.local_manifest:
        try:
            raw = Path(req.local_manifest).read_bytes()[:MAX_MANIFEST + 1]
        except OSError as exc:
            raise LinkError(f"Can't read {req.local_manifest}: {exc}") from None
        source = req.local_manifest
    else:
        from .core.cache import _session

        url = check_url(req.manifest_url or "", "manifest address", resolve=True)
        with _session.get(url, stream=True, timeout=20) as resp:
            resp.raise_for_status()
            check_url(resp.url, "manifest address")  # after redirects
            raw = b""
            for chunk in resp.iter_content(64 << 10):
                raw += chunk
                if len(raw) > MAX_MANIFEST:
                    break
        source = url
    if len(raw) > MAX_MANIFEST:
        raise LinkError("The manifest is too large to be a FrameDrop manifest.")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise LinkError("The manifest isn't valid JSON.") from None
    return manifest_from_data(data, source)


def head_size(url: str) -> int | None:
    """Content-Length from a HEAD request (None when the server doesn't say)."""
    from .core.cache import _session

    try:
        resp = _session.head(url, allow_redirects=True, timeout=10)
        return int(resp.headers["content-length"]) if resp.ok and "content-length" in resp.headers else None
    except Exception:  # noqa: BLE001 - only for the confirmation's size column
        return None


def fetch_icon(m: Manifest) -> Path | None:
    """The manifest's icon (FramePort extension) as a checked PNG in the data folder, or None. Same URL rules as
    the files; at most 2 MiB; must be an image of at least 32 px."""
    if not m.icon:
        return None
    from io import BytesIO

    from PIL import Image

    from .core.cache import _session
    from .core.paths import user_data_dir

    try:
        check_url(m.icon, "icon address", resolve=True)
        raw = b""
        with _session.get(m.icon, stream=True, timeout=20) as resp:
            resp.raise_for_status()
            check_url(resp.url, "icon address")
            for chunk in resp.iter_content(64 << 10):
                raw += chunk
                if len(raw) > MAX_ICON:
                    return None
        with Image.open(BytesIO(raw)) as im:
            if min(im.size) < 32:
                return None
            im = im.convert("RGBA")
            im.thumbnail((512, 512))
            out = user_data_dir() / "downloads" / "icons" / (hashlib.sha256(m.icon.encode()).hexdigest()[:16] + ".png")
            out.parent.mkdir(parents=True, exist_ok=True)
            im.save(out, "PNG")
            return out
    except Exception:  # noqa: BLE001 - the icon is decoration: no icon rather than no install
        return None


# ------------------------------------------------------------------------------------------------- download
def staging_dir(m: Manifest) -> Path:
    from .core.paths import user_data_dir

    key = hashlib.sha256(m.source.encode()).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", m.name.lower()).strip("-")[:40] or "download"
    return user_data_dir() / "downloads" / f"{slug}-{key}"


def download_file(url: str, dest: Path, reporter: Reporter | None = None, sha256: str | None = None,
                  label: str = "") -> Path:
    """Stream one file to dest (via dest.part, removed on failure or cancel), checking its SHA-256 when given."""
    from .core.cache import _session

    check_url(url, "file address", resolve=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    try:
        with _session.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            check_url(resp.url, "file address")  # a redirect must follow the same rules
            total, done = int(resp.headers.get("content-length") or 0), 0
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(1 << 20):
                    if reporter:
                        reporter.check_cancel()
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if reporter and total:
                        reporter.progress(done / total, f"{label or dest.name}: {done >> 20} of {total >> 20} MiB")
        if sha256 and h.hexdigest() != sha256.lower():
            raise LinkError(f"{dest.name} doesn't match the manifest's checksum (download damaged or file changed).")
        tmp.replace(dest)
        return dest
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def download(m: Manifest, reporter: Reporter | None = None) -> Path:
    """Download every file into a fresh staging folder and return the path to add: the APK (OBB files go into the
    `obb/` folder next to it, where the APK scan finds them), the Linux build or the Windows program."""
    import shutil

    if m.demo or is_demo(m.source):
        raise LinkError("This is FramePort's demo link: there's nothing to download.")

    folder = staging_dir(m)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    main, out = m.main, None
    for i, f in enumerate(m.files, 1):
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", f.filename) or f"file{i}"
        sub = "obb" if f.kind == OBB else ("program" if f.kind == EXE and f is main else "")
        dest = folder / sub / name if sub else folder / name
        if reporter:
            reporter.stage("Downloading", f"Downloading {name} ({i} of {len(m.files)})")
        download_file(f.url, dest, reporter, f.sha256, f"{name} ({i}/{len(m.files)})")
        if f is main:
            out = dest
    return out
