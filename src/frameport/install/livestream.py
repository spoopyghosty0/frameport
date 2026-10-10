"""Live view: what the Frame shows, played in a browser window on this PC (the GUI's Live view tab).

The Frame sends fragmented MP4 (H.264) on the stdout of one SSH command (`source_command`); a small HTTP server on
127.0.0.1 relays it to a player page (`live_player.py`: Media Source Extensions, no plugin). Flet has no video control
outside packaged builds, so the picture is shown in the browser, which also decodes it in hardware.

The relay keeps the init segment (ftyp + moov) and the fragments since the last keyframe, so a viewer that opens later
starts at a keyframe. No image bytes ever go through Flet.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import queue
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_NON_SYNC = 0x10000  # ISO/IEC 14496-12 sample_is_non_sync_sample
CLIENT_QUEUE = 240  # fragments a slow viewer may lag behind before it's dropped (it reconnects at a keyframe)


# ------------------------------------------------------------------------------------------------ MP4 parsing
def iter_boxes(data: bytes, start: int = 0, end: int | None = None):
    """(type, payload start, box end) of each complete box in data[start:end]."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind.decode("latin-1"), pos + header, pos + size
        pos += size


def _child(data: bytes, start: int, end: int, kind: str):
    for k, s, e in iter_boxes(data, start, end):
        if k == kind:
            return s, e
    return None


def fragment_is_keyframe(moof: bytes, track_id: int | None = None) -> bool:
    """Whether a movie fragment starts with a sync sample in the given track (default: its first track): trun's
    first-sample flags, else its per-sample flags, else tfhd's default flags. Unknown or no such track → False: a
    viewer then waits for the next fragment that is one."""
    top = _child(moof, 0, len(moof), "moof")
    if not top:
        return False
    for kind, s, e in iter_boxes(moof, *top):
        if kind != "traf":
            continue
        default_flags = None
        tfhd = _child(moof, s, e, "tfhd")
        if tfhd:
            p = tfhd[0]
            flags = int.from_bytes(moof[p + 1:p + 4], "big")
            if track_id is not None and struct.unpack_from(">I", moof, p + 4)[0] != track_id:
                continue
            p += 8  # version/flags + track_ID
            for bit, size in ((0x1, 8), (0x2, 4), (0x8, 4), (0x10, 4)):
                if flags & bit:
                    p += size
            if flags & 0x20 and p + 4 <= tfhd[1]:
                default_flags = struct.unpack_from(">I", moof, p)[0]
        trun = _child(moof, s, e, "trun")
        if not trun:
            continue
        p = trun[0]
        flags = int.from_bytes(moof[p + 1:p + 4], "big")
        count = struct.unpack_from(">I", moof, p + 4)[0]
        p += 8
        if flags & 0x1:
            p += 4  # data_offset
        if flags & 0x4:
            return not struct.unpack_from(">I", moof, p)[0] & SAMPLE_NON_SYNC
        if count and flags & 0x400:
            if flags & 0x100:
                p += 4
            if flags & 0x200:
                p += 4
            return not struct.unpack_from(">I", moof, p)[0] & SAMPLE_NON_SYNC
        if default_flags is not None:
            return not default_flags & SAMPLE_NON_SYNC
        return False
    return False


def codec_string(init: bytes) -> str | None:
    """The MSE codecs parameter from the init segment: `avc1.PPCCLL` (from avcC), plus `mp4a.40.2` when it has an AAC
    track (ffmpeg's aac encoder writes AAC-LC)."""
    i = init.find(b"avcC")
    if i < 0 or i + 8 > len(init):
        return None
    profile, compat, level = init[i + 5], init[i + 6], init[i + 7]
    out = f"avc1.{profile:02x}{compat:02x}{level:02x}"
    if b"mp4a" in init:
        out += ", mp4a.40.2"
    return out


def video_track_id(init: bytes) -> int | None:
    """track_ID of the init segment's video track (its trak's hdlr is 'vide')."""
    moov = _child(init, 0, len(init), "moov")
    if not moov:
        return None
    for kind, s, e in iter_boxes(init, *moov):
        if kind != "trak":
            continue
        tkhd, mdia = _child(init, s, e, "tkhd"), _child(init, s, e, "mdia")
        hdlr = mdia and _child(init, *mdia, "hdlr")
        if tkhd and hdlr and init[hdlr[0] + 8:hdlr[0] + 12] == b"vide":
            version = init[tkhd[0]]
            return struct.unpack_from(">I", init, tkhd[0] + (20 if version == 1 else 12))[0]
    return None


def video_size(init: bytes) -> tuple[int, int] | None:
    """Width and height from the init segment's avc1 sample entry."""
    i = init.find(b"avc1")
    if i < 0 or i + 4 + 28 > len(init):
        return None
    p = i + 4 + 24  # sample entry: 6 reserved + 2 data ref index + 16 pre_defined/reserved
    return struct.unpack_from(">HH", init, p)


class Mp4Splitter:
    """Cuts a byte stream into the init segment and movie fragments (moof + mdat), whatever the read sizes."""

    def __init__(self):
        self.buf = bytearray()
        self.init = bytearray()
        self.have_init = False
        self._moof: bytes | None = None

    def feed(self, data: bytes) -> list[tuple[str, bytes]]:
        """[("init", bytes) | ("fragment", bytes)] completed by this data."""
        self.buf += data
        out = []
        while True:
            if len(self.buf) < 8:
                break
            size, kind = struct.unpack_from(">I4s", self.buf, 0)
            header = 8
            if size == 1:
                if len(self.buf) < 16:
                    break
                size = struct.unpack_from(">Q", self.buf, 8)[0]
                header = 16
            if size < header:
                raise ValueError(f"bad MP4 box size {size}")
            if len(self.buf) < size:
                break
            box = bytes(self.buf[:size])
            del self.buf[:size]
            kind = kind.decode("latin-1")
            if not self.have_init:
                self.init += box
                if kind == "moov":
                    self.have_init = True
                    out.append(("init", bytes(self.init)))
            elif kind == "moof":
                self._moof = box
            elif kind == "mdat" and self._moof is not None:
                out.append(("fragment", self._moof + box))
                self._moof = None
            # other top-level boxes (styp, sidx, free) carry nothing a viewer needs
        return out


# ------------------------------------------------------------------------------------------------ relay
def _end(q: queue.Queue) -> None:
    """Tell a viewer's sender the stream is over: drop what it hasn't sent yet, then the end marker (None)."""
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(None)
    except queue.Full:
        pass


class Relay:
    """Fan-out of one MP4 stream to any number of HTTP viewers."""

    def __init__(self):
        self._lock = threading.Lock()
        self.init: bytes | None = None
        self.video_track: int | None = None
        self.gop: list[bytes] = []  # fragments since the last keyframe fragment (incl.)
        self.clients: list[queue.Queue] = []
        self.frames = 0
        self.bytes = 0
        self.started = time.time()
        self.ended: str | None = None  # why the source stopped
        self._splitter = Mp4Splitter()
        # on_join() asks the source for a keyframe now and says whether it will come (hardware encoder). Then a new
        # viewer waits for it instead of getting the frames since the last one (seconds old with a long GOP).
        self.on_join = None
        self._waiting: set[int] = set()  # id() of client queues that wait for the next keyframe

    def feed(self, data: bytes) -> None:
        for kind, chunk in self._splitter.feed(data):
            with self._lock:
                self.bytes += len(chunk)
                if kind == "init":
                    self.init = chunk
                    self.video_track = video_track_id(chunk)
                    continue
                self.frames += 1
                key = fragment_is_keyframe(chunk, self.video_track)
                if key:
                    self.gop = [chunk]
                    self._waiting.clear()
                elif self.gop:
                    self.gop.append(chunk)
                for q in list(self.clients):
                    if id(q) in self._waiting:
                        continue
                    try:
                        q.put_nowait(chunk)
                    except queue.Full:  # too slow: drop it, its page reconnects (never block: we hold the lock)
                        self.clients.remove(q)
                        _end(q)

    def finish(self, reason: str) -> None:
        with self._lock:
            self.ended = reason
            for q in self.clients:
                _end(q)
            self.clients.clear()

    def subscribe(self) -> tuple[bytes | None, list[bytes], queue.Queue]:
        q: queue.Queue = queue.Queue(CLIENT_QUEUE)
        with self._lock:
            self.clients.append(q)
            if self.on_join is None or self.init is None:
                return self.init, list(self.gop), q
            self._waiting.add(id(q))  # before asking: the keyframe may come back at once
        if self.on_join():
            return self.init, [], q
        with self._lock:  # no keyframe coming: start from the buffered group as before
            self._waiting.discard(id(q))
            return self.init, list(self.gop), q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self.clients:
                self.clients.remove(q)
            self._waiting.discard(id(q))

    def status(self) -> dict:
        with self._lock:
            init = self.init
            out = {"ready": init is not None, "ended": self.ended, "viewers": len(self.clients),
                   "fragments": self.frames, "bytes": self.bytes, "seconds": round(time.time() - self.started, 1)}
        if init:
            out["codec"] = codec_string(init)
            out["audio"] = b"mp4a" in init
            size = video_size(init)
            if size:
                out["width"], out["height"] = size
        return out


def player_html() -> bytes:
    from .live_player import PLAYER_HTML

    return PLAYER_HTML.encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        live = self.server.live
        if path in ("/", "/index.html"):
            self._send(200, player_html(), "text/html; charset=utf-8")
        elif path == "/status":
            self._send(200, json.dumps(live.relay.status()).encode(), "application/json")
        elif path == "/stream.mp4":
            self._stream(live.relay)
        else:
            self._send(404, b"not found", "text/plain")

    def _stream(self, relay: Relay) -> None:
        if relay.ended:
            self._send(503, relay.ended.encode(), "text/plain")
            return
        init, gop, q = relay.subscribe()
        sent, why, t0 = 0, "the stream ended", time.time()
        try:
            deadline = time.time() + 30
            while init is None:  # the source hasn't sent its init segment yet
                if relay.ended or time.time() > deadline:
                    self._send(503, (relay.ended or "no picture yet").encode(), "text/plain")
                    return
                time.sleep(0.1)
                relay.unsubscribe(q)
                init, gop, q = relay.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(init)
            for f in gop:
                self._chunk(f)
            sent = len(gop)
            agent = self.headers.get("User-Agent", "?")[:60]
            log.info("live view: viewer joined (%s)", agent)
            while True:
                try:
                    f = q.get(timeout=5)
                except queue.Empty:
                    if relay.ended:
                        break
                    continue
                if f is None:  # finish() or dropped for lagging CLIENT_QUEUE fragments behind
                    why = "the stream ended" if relay.ended else "it fell too far behind"
                    break
                self._chunk(f)
                sent += 1
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            why = f"it disconnected ({type(exc).__name__})"
        finally:
            relay.unsubscribe(q)
        if init is not None:
            log.info("live view: viewer left after %.1f s, %d fragments: %s", time.time() - t0, sent, why)

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    live: LiveStream


# ------------------------------------------------------------------------------------------------ the Frame's side
# SteamVR on the Frame runs `steamvr-v4l2cam.service` (Valve's v4l2cam): it copies the compositor's "headset view"
# (what the wearer sees: games, SteamVR home, Steam's panels) into a v4l2loopback webcam named "SteamVR"
# (/dev/video99, 1920x1080 RGB24, frames at the display rate). It costs nothing until someone reads it. Steam's own
# recording/Remote Play capture gamescope instead (only the flat Steam UI in VR).
# Encoding: our own helper `fp_venc` (native/venc, spec in native/venc/SPEC.md) drives the Frame's hardware encoder
# (qcom-iris, V4L2) - the stock ffmpeg h264_v4l2m2m hangs on it. It emits one H.264 frame per slot of an even fps
# grid (half/third of the panel rate), so ffmpeg only adds sound and packs fMP4 (`-c:v copy`, `-framerate` = the
# helper's fps). When the helper isn't there or its `--probe` fails, the old path runs: x264 at 30 fps on the CPU.
DEVICE_NAME = "SteamVR"
# The headset view's size comes from SteamVR (1920x1080 on SteamOS 0.3.0; v4l2cam follows it, it has no size option),
# so qualities are heights the picture is scaled *down* to (never up: a 2K/4K setting would only add bytes); "full"
# sends it at its own size, whatever SteamVR makes it.
QUALITY = {  # max height (None = the headset view's own), bitrate
    "360p": (360, "1M"),
    "480p": (480, "1500k"),
    "720p": (720, "3M"),
    "1080p": (1080, "6M"),
    "full": (None, "10M"),
}
DEFAULT_QUALITY = "720p"
# Hardware path (fp_venc): bit/s targets, VBR up to HW_PEAK x the target. Bits are nearly free there (no CPU cost), and
# 3 Mbit/s CBR at 720p36 showed heavy compression artifacts in the headset test (2026-10-07): head turns move the whole
# picture, and CBR blurred exactly those frames.
HW_BITRATE = {"360p": 1_500_000, "480p": 2_500_000, "720p": 5_000_000, "1080p": 8_000_000, "full": 10_000_000}
HW_PEAK = 1.5
VIDEO_INPUT_OFFSET = "-0.25"  # s: ffmpeg's video input vs pulse's late audio (see source_command)
FPS = 30  # software (x264) path only; the hardware path picks an even fraction of the panel rate
# Clocks (x264 path): pulse stamps audio with the wall clock, v4l2 with CLOCK_MONOTONIC → `-ts mono2abs` puts the
# picture on the wall clock too. (Forcing -use_wallclock_as_timestamps on the pulse input gave bursts of AAC packets
# one shared time, a broken audio timeline.) aresample=async=1 keeps the sound continuous across hiccups.
AUDIO_BITRATE = "128k"  # AAC of the Frame's default output's monitor = what the headset plays (Lepton/Proton games)
ENCODER_THREADS = 3  # leaves the game most of the CPU
HELPER = "fp_venc"
HELPER_REMOTE = ".local/share/frameport/bin/fp_venc"  # under the Frame's home
# stderr lines the script prints for the PC side: `live: encoder=hardware fps=32` / `live: encoder=software fps=30`
INFO_PREFIX = "live: "


def bitrate_bps(rate: str) -> int:
    """ffmpeg-style "3M" / "1500k" → bit/s."""
    mult = {"k": 1_000, "M": 1_000_000}.get(rate[-1:], 1)
    return int(float(rate[:-1] if mult > 1 else rate) * mult)


def source_command(quality: str = DEFAULT_QUALITY, fps: int = FPS) -> str:
    """Shell script for the Frame: finds the headset-view device, streams fMP4 (H.264, + AAC sound of the
    default output when there is one) to stdout and stops when the SSH channel closes (stdin reaches EOF).
    Hardware encoder (fp_venc) when it probes fine, else x264. Exit 3 = no headset view device (SteamVR not running).
    `fps` applies to the x264 path."""
    h, rate = QUALITY.get(quality, QUALITY[DEFAULT_QUALITY])
    # width -2 keeps the aspect ratio (even, as yuv420p needs); full size only drops an odd last row/column
    scale = (",crop=trunc(iw/2)*2:trunc(ih/2)*2" if h is None
             else f",scale=-2:'min({h},ih)':flags=fast_bilinear")
    hw_rate = HW_BITRATE.get(quality, HW_BITRATE[DEFAULT_QUALITY])
    venc_args = (f'--source "$dev" --bitrate {hw_rate} --peak {int(hw_rate * HW_PEAK)}'
                 + (f" --height {h}" if h else ""))
    return f"""dev=
for d in /sys/class/video4linux/video*; do
  [ "$(cat "$d/name" 2>/dev/null)" = "{DEVICE_NAME}" ] && dev=/dev/${{d##*/}} && break
done
if [ -z "$dev" ]; then echo "no SteamVR headset view device: is SteamVR running?" >&2; exit 3; fi
export XDG_RUNTIME_DIR=${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}  # (an SSH session may lack it: PipeWire's socket)
# the PC kills this process group when a stop's stdin EOF doesn't end it (writes blocked on a full pipe)
echo "{INFO_PREFIX}pgid=$(ps -o pgid= $$ | tr -d ' ')" >&2
audio=(-an)
sink=$(pactl get-default-sink 2>/dev/null)
if [ -n "$sink" ]; then
  audio=(-thread_queue_size 1024 -f pulse -fragment_size 3840 -i "$sink.monitor"
         -map 0:v -map 1:a -af aresample=async=1 -c:a aac -b:a {AUDIO_BITRATE} -ac 2 -ar 48000)
fi
venc=${{FP_VENC:-$HOME/{HELPER_REMOTE}}}
info=
[ -x "$venc" ] && info=$("$venc" --probe {venc_args} 2>/dev/null)
hwfps=$(printf %s "$info" | sed -n 's/.*"fps":\\([0-9][0-9]*\\).*/\\1/p')
if [ -n "$hwfps" ]; then
  echo "{INFO_PREFIX}encoder=hardware fps=$hwfps" >&2
  # fp_venc reads the SSH channel (stdin): "k" = keyframe, EOF = stop; ffmpeg then ends with it (-shortest).
  # Raw H.264 has no timestamps. The input is stamped on arrival (wall clock, like pulse's audio) so ffmpeg reads both
  # inputs in step; setts then numbers the frames on fp_venc's fps grid. Measured on the Frame (2026-10-07):
  # arrival stamps alone bunched frames read together (gaps of 0-10 ms and 45+ ms: dropped-looking frames in the
  # player); setts alone (no input stamps) held all output ~7 s next to the sound; -framerate is ignored;
  # -fflags nobuffer lost the first seconds of a still picture's tiny frames; a big probesize waits seconds for one.
  # frag_keyframe: a requested keyframe starts its own fragment, where a joining viewer can begin.
  # -itsoffset {VIDEO_INPUT_OFFSET}: ffmpeg paces its inputs against each other, and pulse's audio arrives later than
  # the wall clock (more while something plays), so the video input counted as "ahead" and ffmpeg stopped reading it
  # for up to 0.7 s: fp_venc's writes blocked and 1080p dropped frames (owner's test 2026-10-07; busy 1080p replayed
  # with sound: 20 s took 34-47 s). Shifting the video input back fixes that, but too far makes ffmpeg hold video for
  # the interleave and release it in clumps (-1 s: output gaps up to 550 ms, a longer, stuttering delay in the
  # browser, worse with sound on). Measured on the Frame with sound playing: -0.25 s = no stalled writes at busy 1080p
  # and steady output (gaps 50-150 ms); -0.5 s already clumps (200-250 ms); none = 18 slow writes; PULSE_LATENCY_MSEC
  # =20 made it worse. Only the input side moves: setts sets the output times, audio and video spans stay equal.
  "$venc" {venc_args} --fps "$hwfps" | nice -n 10 ffmpeg -nostdin -hide_banner -loglevel error \\
    -probesize 32 -analyzeduration 0 -use_wallclock_as_timestamps 1 -itsoffset {VIDEO_INPUT_OFFSET} \\
    -f h264 -i pipe:0 "${{audio[@]}}" \\
    -c:v copy -bsf:v "setts=ts=N*(1/$hwfps)/TB:duration=(1/$hwfps)/TB" -shortest \\
    -f mp4 -movflags empty_moov+default_base_moof+frag_keyframe -frag_duration 100000 -
  exit $?
fi
echo "{INFO_PREFIX}encoder=software fps={fps}" >&2
exec 3<&0
nice -n 10 ffmpeg -nostdin -hide_banner -loglevel error \\
  -thread_queue_size 64 -ts mono2abs -f v4l2 -input_format rgb24 -i "$dev" "${{audio[@]}}" \\
  -vf "fps={fps}{scale},format=yuv420p" -c:v libx264 -preset ultrafast -tune zerolatency -threads {ENCODER_THREADS} \\
  -g {fps} -b:v {rate} -maxrate {rate} -bufsize {rate} \\
  -f mp4 -movflags empty_moov+default_base_moof -frag_duration 100000 - &
pid=$!
( cat <&3 >/dev/null; kill $pid 2>/dev/null ) >/dev/null 2>&1 &  # (a background job's own stdin is /dev/null)
wait $pid
"""


def helper_path() -> Path | None:
    """The bundled fp_venc (artifacts/linux-arm64-bin), None in builds without it."""
    from ..core.paths import artifacts_dir

    p = artifacts_dir() / "linux-arm64-bin" / HELPER
    return p if p.is_file() else None


def ensure_helper(frame) -> bool:
    """Make sure the Frame has this app's fp_venc (sha256 compare, upload when different). False = it doesn't (the
    stream then uses x264); never raises."""
    local = helper_path()
    if local is None:
        return False
    try:
        from ..frame.connection import sh_quote

        remote = f"{frame.home}/{HELPER_REMOTE}"
        want = hashlib.sha256(local.read_bytes()).hexdigest()
        _, out, _ = frame.run(f"sha256sum {sh_quote(remote)} 2>/dev/null")
        if out.split()[:1] == [want]:
            return True
        frame.run(f"mkdir -p {sh_quote(posixpath.dirname(remote))}")
        tmp = f"{remote}.new-{os.getpid()}-{threading.get_ident()}"  # own name: two starts at once don't collide
        frame.put(local, tmp, resume=False)
        code, _, err = frame.run(f"chmod 755 {sh_quote(tmp)} && mv -f {sh_quote(tmp)} {sh_quote(remote)}")
        if code:
            raise RuntimeError(err.strip())
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("live view: couldn't put %s on the Frame (%s); using the software encoder", HELPER, exc)
        return False


STOP_GRACE = 2.0  # s to wait for the script to end after stdin EOF before killing its process group
DROP_WARN = 0.05  # share of a stats window's frame slots that were late or skipped before the panel warns


def window_stats(line: str, prev: dict[str, int]) -> tuple[dict[str, int], dict]:
    """One `fp_venc: stats k=v ...` line (counters since the start) → (its counters, this window's figures:
    frames, dropped = late + skipped slots, dropping = at least DROP_WARN of the window's slots, kbps)."""
    counters = {}
    for kv in line.split():
        k, _, v = kv.partition("=")
        if v.isdigit():
            counters[k] = int(v)
    d = {k: counters.get(k, 0) - prev.get(k, 0) for k in ("frames", "late", "skipped")}
    dropped = d["late"] + d["skipped"]
    slots = d["frames"] + d["skipped"]
    return counters, {"frames": d["frames"], "dropped": dropped, "kbps": counters.get("kbps", 0),
                      "dropping": slots > 0 and dropped >= DROP_WARN * slots}


class FrameSource:
    """The stdout of `source_command` on the Frame, on its own SSH channel of the existing connection."""

    def __init__(self, frame, quality: str):
        self.frame = frame
        self.quality = quality
        self.chan = None
        self.info: dict[str, str] = {}  # from the script's `live: k=v ...` stderr lines (encoder, fps)
        self.stats: dict = {}  # the hardware encoder's last 10 s (window_stats), for the panel
        self._counters: dict[str, int] = {}
        self._err = b""
        self._last_error = ""

    def open(self):
        ensure_helper(self.frame)
        transport = self.frame.client.get_transport()
        if transport is None or not transport.is_active():
            raise ConnectionError("not connected to the Frame")
        self.chan = transport.open_session()
        from ..frame.connection import sh_quote

        self.chan.exec_command("bash -c " + sh_quote(source_command(self.quality)))
        return self

    def _read_stderr(self, final: bool = False) -> None:
        while self.chan.recv_stderr_ready():
            self._err += self.chan.recv_stderr(4096)
        *lines, self._err = self._err.split(b"\n")
        if final:
            lines.append(self._err)
            self._err = b""
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith(INFO_PREFIX):
                self.info.update(kv.split("=", 1) for kv in line[len(INFO_PREFIX):].split() if "=" in kv)
            elif line.startswith(("fp_venc: info ", "fp_venc: stats ")):  # stats every 10 s: late/skipped frames
                log.info("live view: %s", line)
                if line.startswith("fp_venc: stats "):
                    self._counters, self.stats = window_stats(line, self._counters)
            elif line:
                self._last_error = line

    def read(self, n: int) -> bytes:
        data = self.chan.recv(n)
        self._read_stderr(final=not data)
        if not data and self._last_error:
            raise RuntimeError(self._last_error)
        return data

    def request_keyframe(self) -> bool:
        """Ask the hardware encoder for a keyframe now (a viewer joined). False on the x264 path (no such request:
        the relay then sends the frames since the last keyframe)."""
        if self.info.get("encoder") != "hardware" or self.chan is None:
            return False
        try:
            self.chan.sendall(b"k")
            return True
        except OSError:
            return False

    def close(self, grace: float = STOP_GRACE) -> None:
        """EOF on the script's stdin stops the encoder. But when the stream is backed up, fp_venc sits in a blocked
        write and never reads stdin, and once the channel is closed nothing drains ffmpeg's output either (found on
        the Frame at 1080p, 2026-10-07: both left running, holding SteamVR's headset view open). So: wait a moment,
        then end the script's process group from a second command."""
        if self.chan is None:
            return
        try:
            self.chan.shutdown_write()
            deadline = time.time() + grace
            while not self.chan.exit_status_ready() and time.time() < deadline:
                while self.chan.recv_ready():  # drain, so a blocked writer can get to stdin
                    self.chan.recv(65536)
                time.sleep(0.05)
            self._read_stderr()
            pgid = self.info.get("pgid", "")
            if not self.chan.exit_status_ready() and pgid.isdigit() and int(pgid) > 1:
                log.info("live view: the stream didn't stop by itself; ending process group %s", pgid)
                self.frame.run(f"kill -TERM -- -{pgid} 2>/dev/null; sleep 1; kill -KILL -- -{pgid} 2>/dev/null; true")
        except Exception as exc:  # noqa: BLE001
            log.warning("live view: stopping the stream on the Frame: %s", exc)
        finally:
            self.chan.close()


def start(frame, quality: str = DEFAULT_QUALITY) -> LiveStream:
    """Start streaming the Frame's headset view; open `.url` in a browser to watch."""
    client = getattr(frame, "client", None)
    transport = client.get_transport() if client is not None else None
    if transport is None or not transport.is_active():
        # (e.g. the Frame slept: without this, the helper upload failed with "'NoneType' object has no attribute
        # 'exec_command'" and the start went on with the software encoder)
        raise ConnectionError("The Frame isn't connected (it may be asleep). Connect it on the Steam Frame page, "
                              "then start the live view.")
    src = FrameSource(frame, quality)
    live = LiveStream(src.open, src.close)
    live.relay.on_join = src.request_keyframe
    live.source_info = lambda: {**src.info, "stats": dict(src.stats)}
    return live.start()


class LiveStream:
    """One live view: the source command on the Frame + the local relay server.

    `open_source()` returns a file-like object with `read(n)`; it's the stdout of a command on the Frame (or, in tests,
    a local process). `stop()` ends both.
    """

    def __init__(self, open_source, close_source=None, host: str = "127.0.0.1", port: int = 0):
        self._open_source = open_source
        self._close_source = close_source
        self.relay = Relay()
        self.server = _Server((host, port), _Handler)
        self.server.live = self
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self.on_end = None  # callback(reason) when the source stops by itself
        # () -> {"encoder": "hardware"|"software", "fps": "32", "stats": window_stats(...)[1]} once the source said so
        self.source_info = dict

    def status(self) -> dict:
        """The relay's status + what the source reported (encoder, fps, the encoder's last 10 s: dropped frames)."""
        out = self.relay.status()
        info = self.source_info()
        if info.get("encoder"):
            out["encoder"] = info["encoder"]
        if str(info.get("fps", "")).isdigit():
            out["fps"] = int(info["fps"])
        stats = info.get("stats") or {}
        if stats:
            out["dropped"], out["dropping"] = stats.get("dropped", 0), bool(stats.get("dropping"))
        return out

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/"

    def start(self) -> LiveStream:
        t1 = threading.Thread(target=self.server.serve_forever, name="live-http", daemon=True)
        t2 = threading.Thread(target=self._pump, name="live-source", daemon=True)
        self._threads = [t1, t2]
        t1.start()
        t2.start()
        return self

    def _pump(self) -> None:
        reason = "stopped"
        try:
            src = self._open_source()
            while not self._stop.is_set():
                data = src.read(65536)
                if not data:
                    reason = "the Frame stopped sending a picture"
                    break
                self.relay.feed(data)
        except Exception as exc:  # noqa: BLE001
            from ..errors import explain

            reason = explain(exc)
        if self._stop.is_set():
            reason = "stopped"
        self.relay.finish(reason)
        if not self._stop.is_set() and self.on_end:
            self.on_end(reason)

    @property
    def running(self) -> bool:
        return not self._stop.is_set() and self.relay.ended is None

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._close_source:
            try:
                self._close_source()
            except Exception:  # noqa: BLE001
                pass
        self.relay.finish("stopped")
        self.server.shutdown()
        self.server.server_close()
