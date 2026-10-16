"""Launch-time performance experiments. Frame side, stdlib only; no account data is read here.

Settings contain numbers/booleans and a small environment whitelist, never arbitrary shell. A launch prepares its
own config and exports it before Proton starts. Bench owns the otherwise-idle shared prefix for at most 120 s.
"""
import collections
import ctypes as C
import fcntl
import hashlib
import json
import math
import os
import sys
import re
import shlex
import shutil
import signal
import struct
import subprocess
import time
from pathlib import Path

CORE = None
FEX_KEYS = {"FEX_TSOENABLED", "FEX_VECTORTSOENABLED", "FEX_MEMCPYSETTSOENABLED", "FEX_MULTIBLOCK",
            "FEX_X87REDUCEDPRECISION"}
DEFAULTS = {"dxvk_disable_msaa": False, "dxgi_sync_interval": -1, "dxgi_max_frame_latency": 0,
            "eye_scale": 1.5, "motion_smoothing": 2, "cores": "4-7", "ntsync": True,
            "fp_mem": False, "bigcache_mb": 256, "fp_mem_zero": False, "fex": {},
            "wine_large_address_aware": False}
BOOL_KEYS = {"dxvk_disable_msaa", "ntsync", "fp_mem", "fp_mem_zero", "wine_large_address_aware"}
INT_KEYS = {"dxgi_sync_interval": (-1, 4), "dxgi_max_frame_latency": (0, 16), "motion_smoothing": (0, 2),
            "bigcache_mb": (1, 6144)}


def app_name(app):
    if not isinstance(app, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", app):
        raise ValueError("invalid Meta app name")
    return app


def validate(values):
    if not isinstance(values, dict):
        raise ValueError("settings must be an object")
    out = {}
    for key, value in values.items():
        if key in BOOL_KEYS:
            if type(value) is not bool:
                raise ValueError(f"{key} must be true or false")
        elif key in INT_KEYS:
            lo, hi = INT_KEYS[key]
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{key} must be an integer in {lo}..{hi}")
        elif key == "eye_scale":
            if type(value) not in (float, int) or value not in (1.0, 1.25, 1.5):
                raise ValueError("eye_scale must be 1.0, 1.25 or 1.5 (a divisor)")
        elif key == "cores":
            if not isinstance(value, str) or not re.fullmatch(r"[0-7](?:-[0-7])?(?:,[0-7](?:-[0-7])?)*", value):
                raise ValueError("cores must be a taskset CPU list within 0..7")
            if any(len(p) == 3 and p[0] > p[2] for p in value.split(",")):
                raise ValueError("cores range is reversed")
        elif key == "fex":
            if not isinstance(value, dict) or any(k not in FEX_KEYS or type(v) not in (str, int) or
                                                   str(v) not in ("0", "1") for k, v in value.items()):
                raise ValueError("fex permits only boolean TSO, MULTIBLOCK and X87REDUCEDPRECISION keys")
            value = {k: str(v) for k, v in value.items()}
        else:
            raise ValueError(f"unknown performance setting: {key}")
        out[key] = value
    return out


def settings_path(app):
    return os.path.join(CORE.ROOT, "settings", app_name(app) + ".json")


def settings_get(args):
    app = app_name(args["app"])
    info = CORE.GAMES.get(app, {})
    env = info.get("env", {})
    defaults = dict(DEFAULTS, fp_mem=env.get("FP_MEM", "") not in ("", "0"),
                    bigcache_mb=int(env.get("FP_BIGCACHE_MB", 256)))
    defaults.update(validate(info.get("perf", {})))
    overrides = validate(CORE.read_json(settings_path(app), {}) or {})
    return {"app": app, "settings": dict(defaults, **overrides), "overrides": overrides}


def settings_set(args):
    app = app_name(args["app"])
    # Validate the whole request before writing. Null removes an override (the catalog/default becomes active).
    updates = {k: v for k, v in args.items() if k != "app"}
    if any(k not in DEFAULTS for k in updates):
        raise ValueError("unknown performance setting")
    values = settings_get({"app": app})["overrides"]
    values.update(validate({k: v for k, v in updates.items() if v is not None}))
    for k, v in updates.items():
        if v is None:
            values.pop(k, None)
    CORE.write_json(settings_path(app), values)
    return settings_get({"app": app})


def scale_folder(scale):
    return os.path.join(CORE.ROOT, "revive-scales", format(float(scale), "g"))


def eye_variant(data, scale):
    """Only the research Revive 3.2.0 eye-size site, with PE RVA translation. Never touch Oculus binaries.

    Accept the stock eye-size load or the research divisor load. Refuse other instructions rather than guessing
    at a new Revive build. Use a dedicated padding slot, leaving shared constants in the data section unchanged.
    """
    if data[:2] != b"MZ":
        raise ValueError("Revive eye scale: invalid PE")
    pe = struct.unpack_from("<I", data, 0x3c)[0]
    n, opt = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]

    def offset(rva):
        for i in range(n):
            size, va, rawsize, raw = struct.unpack_from("<IIII", data, pe + 24 + opt + i * 40 + 8)
            if va <= rva < va + min(size, rawsize):
                return raw + rva - va
        raise ValueError("Revive eye scale: RVA outside sections")
    ins, slot = offset(0x12eab), offset(0x12e9c)
    # Original movss xmm4,[1.0], or the research patch's load of the existing 1.5 at RVA 0x4eaa4.
    # Put our divisor in verified function-alignment padding: changing a shared rdata constant affects other code.
    old = data[ins:ins + 8]
    original = bytes.fromhex("f30f102515bb0400")
    research = b"\xf3\x0f\x10\x25" + struct.pack("<i", 0x4eaa4 - 0x12eb3)
    if old not in (original, research) or data[offset(0x12e91)] != 0xc3 or data[slot:slot + 4] != b"\xcc" * 4:
        raise ValueError("Revive eye scale: unexpected 3.2.0 eye-size instruction/padding")
    out = bytearray(data)
    struct.pack_into("<f", out, slot, scale)
    out[ins:ins + 8] = b"\xf3\x0f\x10\x25" + struct.pack("<i", 0x12e9c - 0x12eb3)
    return bytes(out)


def prepare_scales():
    src = Path(CORE.REVIVE)
    data = (src / "LibRevive64.dll").read_bytes()
    # PC setup validates stock 3.2.0 and applies REVIVE_PATCHES (src/frameport/fexrift.py): the visibility fixes and
    # the eye-size redirect to the 1.5 constant. Accept only that upload, or the same without the eye-size redirect.
    uploaded = {"eda2a449d230832c95734a03e8c83eeb531668ce676693f636e19c315765af4e",    # all six patches
                "0a3a6733f5decec74abb16b85c1f72783a6f421160753f600ddae4e8cb6482c6"}    # visibility fixes only
    if hashlib.sha256(data).hexdigest() not in uploaded:
        raise ValueError("Revive eye scale: needs FramePort's patched Revive 3.2.0 upload")
    for scale in (1.0, 1.25, 1.5):
        patched = eye_variant(data, scale)  # validate before replacing any directory
        dest = Path(scale_folder(scale))
        tmp = dest.with_name(dest.name + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp, copy_function=os.link)
        # Break the hard link before changing the DLL: shared base and other variants stay intact.
        dll = tmp / "LibRevive64.dll"
        dll.unlink()
        dll.write_bytes(patched)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)


def perf_prepare(args):
    app = app_name(args["app"])
    s = settings_get({"app": app})["settings"]
    config = os.path.join(CORE.ROOT, "settings", app + ".dxvk.conf")
    os.makedirs(os.path.dirname(config), exist_ok=True)   # first launch of a game nobody changed settings for
    managed = ("d3d11.disableMsaa", "dxgi.syncInterval", "dxgi.maxFrameLatency")   # these settings win
    profile = [str(line) for line in args.get("dxvk", []) if "\n" not in str(line)
               and str(line).split("=", 1)[0].strip() not in managed]
    lines = profile + [f"{k} = {s[key]}" for k, key, default in (
        ("d3d11.disableMsaa", "dxvk_disable_msaa", False), ("dxgi.syncInterval", "dxgi_sync_interval", -1),
        ("dxgi.maxFrameLatency", "dxgi_max_frame_latency", 0)) if s[key] != default]
    # Defaults = exactly the launch verified in the headset (First Contact, 2026-10-10): no dxvk.conf, the uploaded
    # Revive (its eye-size patch is 1.5), no large-address-aware flag; only a changed setting adds something.
    if lines:
        CORE.put_text(config, "\n".join(lines + [""]))
    elif os.path.lexists(config):
        os.remove(config)
    if s["eye_scale"] == 1.5:
        revive = CORE.REVIVE
    else:
        revive = scale_folder(s["eye_scale"])
        if not os.path.isfile(os.path.join(revive, "LibRevive64.dll")):
            prepare_scales()
    exports = {"FP_CORES": s["cores"], "FP_REVIVE_DIR": revive,
               "FP_MEM": "1" if s["fp_mem"] else "", "FP_BIGCACHE_MB": str(s["bigcache_mb"]),
               "FP_MEM_ZERO": "1" if s["fp_mem_zero"] else "0", "PROTON_NO_NTSYNC": "0" if s["ntsync"] else "1",
               "FP_MOTION_SMOOTHING": json.dumps({"section": f"steam.app.{CORE.GAME_ID}",
                                                 "key": "motionSmoothingOverride", "kind": "i",
                                                 "value": s["motion_smoothing"]})}
    exports.update({k: str(s["fex"].get(k, "0" if "TSO" in k else "1" if k == "FEX_MULTIBLOCK" else "0"))
                    for k in FEX_KEYS})
    if lines:
        exports["DXVK_CONFIG_FILE"] = config
    if s["wine_large_address_aware"]:
        exports["WINE_LARGE_ADDRESS_AWARE"] = "1"
    shell = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in exports.items())
    if s["ntsync"]:
        shell += "\nunset PROTON_NO_NTSYNC"
    return {"shell": shell, "settings": s}


def launcher_env(app, info):
    # stdout is an isolated helper's JSON, then a tiny decoder emits only validated exports. Failure aborts launch:
    # continuing with an old config would give misleading experimental results (and could enable stale fp_mem).
    # the compat profile's dxvk.conf lines (stream C) go into the same per-game config as these settings
    dxvk = [str(line) for line in info.get("dxvk", []) if "\n" not in str(line) and "\r" not in str(line)]
    arg = shlex.quote(json.dumps({"app": app_name(app), "dxvk": dxvk}))
    decode = 'import json,sys; print(json.load(sys.stdin)["result"]["shell"])'
    return [f'FP_PERF=$(python3 -I "$R/artifacts/fexrift.py" perf_prepare {arg}) || exit 1',
            f'eval "$(printf \'%s\' "$FP_PERF" | python3 -I -c {shlex.quote(decode)})"']


# OpenVR IVRCompositor_022 Compositor_FrameTiming, including its 80-byte TrackedDevicePose_t. Keeping the tail
# prevents GetFrameTimings overwriting a truncated ctypes structure. Versions are pinned, never probed by index.
class FrameTiming(C.Structure):
    _fields_ = [(n, C.c_uint32) for n in ("size", "index", "presents", "mis_presents", "dropped", "reprojection")]
    _fields_ += [("system_time", C.c_double)]
    _fields_ += [(n, C.c_float) for n in ("pre_gpu", "post_gpu", "gpu_ms", "compositor_gpu", "compositor_cpu",
                                         "compositor_idle", "client_interval", "present_call", "wait_present",
                                         "submit", "poses_called", "poses_ready", "frame_ready", "update_start",
                                         "update_end", "render_start")]
    _fields_ += [("pose", C.c_byte * 80), ("vsyncs_ready", C.c_uint32), ("vsyncs_first", C.c_uint32)]


class VR:
    def __init__(self):
        self.core, self.method, self.settings = CORE.vr_settings()
        try:
            self.compositor = self.interface(b"IVRCompositor_022")
            self.system = self.interface(b"IVRSystem_022")
        except BaseException:
            self.close()
            raise

    def interface(self, name):
        err = C.c_int()
        obj = self.method(self.core, 3, C.c_void_p, C.c_char_p, C.POINTER(C.c_int))(name, C.byref(err))
        if not obj or err.value:
            raise ValueError("benchmark needs " + name.decode())
        return obj

    def activity(self):
        # IVRSystem_022::GetTrackedDeviceActivityLevel(HMD=0). UserInteraction (1) and its timeout (2) refuse.
        return self.method(self.system, 15, C.c_int, C.c_uint32)(0)

    def screen_timeout(self):
        err = C.c_int()
        value = self.method(self.settings, 7, C.c_float, C.c_char_p, C.c_char_p, C.POINTER(C.c_int))(
            b"power", b"turnOffScreensTimeout", C.byref(err))
        if err.value or not math.isfinite(value) or value <= 0:
            raise ValueError("cannot read screen timeout; benchmark refused")
        return value

    def timings(self):
        rows = (FrameTiming * 128)()
        for row in rows:
            row.size = C.sizeof(FrameTiming)
        n = self.method(self.compositor, 9, C.c_uint32, C.POINTER(FrameTiming), C.c_uint32)(rows, 128)
        return [{"index": r.index, "presents": r.presents, "gpu_ms": r.gpu_ms,
                 "cpu_ms": r.client_interval, "system_time": r.system_time} for r in rows[:min(n, 128)]]

    def scene_pid(self):
        return self.method(self.compositor, 22, C.c_uint32)()  # GetCurrentSceneFocusProcess

    def close(self):
        self.method(self.core, 1, None)()


def drm_engines(text):
    """DRM fdinfo nanoseconds, deduplicated by drm-client-id by the caller (dup fds share one client)."""
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r"drm-engine-([\w-]+):\s*(\d+)\s+ns", line.strip())
        if m:
            out[m[1]] = int(m[2])
    return out


def process_sample(pids, proc="/proc"):
    engines, waits, seen = {}, collections.Counter(), set()
    for pid in pids:
        base = Path(proc) / str(pid)
        for fd in (base / "fdinfo").glob("*"):
            try:
                text = fd.read_text()
            except OSError:
                continue
            client = re.search(r"^drm-client-id:\s*(\d+)", text, re.M)
            device = re.search(r"^drm-pdev:\s*(.+)", text, re.M)
            if not client:
                continue
            identity = (device[1] if device else "", client[1])
            if identity in seen:
                continue
            seen.add(identity)
            for engine, ns in drm_engines(text).items():
                engines[(identity, engine)] = ns
        for task in (base / "task").glob("*"):
            try:
                waits[task.joinpath("wchan").read_text().strip()] += 1
            except OSError:
                pass
    return engines, waits


def gpu_delta(before, after):
    # Clients that open during the interval have no baseline; do not attribute their lifetime counters to one tick.
    return sum(max(0, v - before[k]) for k, v in after.items() if k in before)


def processes(proc="/proc"):
    out = {}
    for p in Path(proc).glob("[0-9]*"):
        try:
            if p.stat().st_uid != os.getuid():
                continue
            cmd = p.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            comm = p.joinpath("comm").read_text().strip()
            out[int(p.name)] = (comm, cmd)
        except OSError:
            pass
    return out


def busy_processes(rows):
    # Do not print cmdlines: another stream may be handling an Oculus browser sign-in.
    return [pid for pid, (comm, cmd) in rows.items() if
            (re.search(r"(?i)(\.exe(?: |$)|(?:^|/)(?:proton|wine(?:64)?(?:-preloader)?)(?: |$)|lepton-steamlaunch-)",
                       cmd)
             or (comm == "reaper" and not re.search(r"\bAppId=250820\b", cmd))
             or comm.startswith("wineserver") or comm in ("FEXInterpreter", "FEXLoader", "pressure-vessel"))]


def recent_steam_input(steam, now=None):
    """Steam input log is a supplementary guard, HMD activity is the authoritative one. Never read login logs."""
    now = time.time() if now is None else now
    path = Path(steam) / "logs/controller.txt"
    try:
        # A recent controller log write can be a device reconnection, so conservatively refuse it too.
        return now - path.stat().st_mtime < 60
    except FileNotFoundError:
        return False


def vr_set(section, key, kind, value):
    # In its own process: cmd_vrsetting in the process that holds VR()'s SteamVR connection segfaulted on the
    # second call (reproduced on the Frame 2026-10-16) after the first had turned the display's standby pause off.
    result = vrsetting_subprocess(section, key, kind, value)
    if result.get("error"):
        raise ValueError(f"SteamVR refused {section}/{key}")


def stop_launcher(child):
    # The shared prefix was verified idle before launch, so -k cannot interrupt an earlier game or login.
    try:
        subprocess.run([os.path.join(CORE.GEF, "files/bin/wineserver"), "-k"], env=CORE.wine_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)


START_WAIT = 180   # seconds the bench waits for the game's process after starting its launcher


def bench_run(args, launcher, vr):
    seconds = args.get("seconds", 120)
    if type(seconds) is not int or not 1 <= seconds <= 120:
        raise ValueError("seconds must be an integer in 1..120")
    if busy_processes(processes()) or recent_steam_input(CORE.STEAM) or vr.activity() != 3:
        raise ValueError("benchmark refused: game, Wine, container, HMD activity or recent Steam input")
    screen = vr.screen_timeout()  # no setting is changed until both safety checks and this read succeed
    vrserver_log = os.path.join(CORE.STEAM, "logs", "vrserver.txt")
    log_start = os.path.getsize(vrserver_log) if os.path.isfile(vrserver_log) else 0
    guard_write(screen=screen)    # before any change: the parent restores from it if this process dies
    child, old_handler = None, None
    settings = settings_get(args)["settings"]
    frames, waits, gpu, gpu_seconds = {}, collections.Counter(), 0, 0.0
    started = time.monotonic()

    def interrupted(signum, frame):
        raise ValueError("benchmark interrupted")
    try:
        old_handler = signal.signal(signal.SIGTERM, interrupted)
        vr_set("power", "pauseCompositorOnStandby", "b", False)
        vr_set("power", "turnOffScreensTimeout", "f", float(seconds + START_WAIT + 10))
        # Save the initial frame index so stale scene frames in the compositor ring are excluded.
        initial = {r["index"] for r in vr.timings()}
        with open(os.devnull, "wb") as sink:
            child = subprocess.Popen([launcher], stdin=subprocess.DEVNULL, stdout=sink, stderr=sink,
                                     start_new_session=True, close_fds=True)
        guard_write(screen=screen, pgid=getattr(child, "pid", None))
        # The launcher starts Meta's runtime and waits for it (60-100 s under FEX) before the game: the window
        # starts when the game's process appears, not at the launch.
        wait_end = time.monotonic() + START_WAIT
        while not [q for q, (_, cmd) in processes().items() if args["exe"].lower() in cmd.lower()]:
            if time.monotonic() > wait_end:
                raise ValueError(f"benchmark: the game didn't start within {START_WAIT} s; inspect launch.log")
            if child.poll() is not None:
                raise ValueError("benchmark launcher exited before the game started; inspect launch.log")
            level = vr.activity()
            if level == 1:   # controller.txt isn't a signal here: the starting game loads its input bindings
                raise ValueError(f"benchmark stopped: owner interaction (activity {level})")
            time.sleep(1)
        before, previous = {}, time.monotonic()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            # Before the launch the headset must be in standby (3); the game itself then wakes the compositor
            # (activity leaves standby), so during the run only real use stops it: 1 = user interaction (worn or
            # moved), or new controller input.
            level = vr.activity()
            if level == 1:   # controller.txt isn't a signal here: the starting game loads its input bindings
                raise ValueError(f"benchmark stopped: owner interaction (activity {level})")
            if child.poll() is not None:
                raise ValueError("benchmark launcher exited before the sampling window ended; inspect launch.log")
            rows = processes()
            # Only the actual game's exe (or FEX's argv containing it), not the runtime/compositor GPU client.
            pids = [p for p, (_, cmd) in rows.items() if args["exe"].lower() in cmd.lower()]
            after, w = process_sample(pids)
            now = time.monotonic()
            if before.keys() & after.keys():
                gpu += gpu_delta(before, after)
                gpu_seconds += now - previous
            before, previous = after, now
            waits.update(w)
            focus = vr.scene_pid()
            for r in vr.timings():
                if focus in pids and r["index"] not in initial:
                    frames[r["index"]] = r  # ring revisits update presents, not another frame
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    finally:
        # Each cleanup is attempted even when the preceding one fails. Display safety outranks benchmark success.
        try:
            stop_launcher(child)
        finally:
            try:
                vr_set("power", "pauseCompositorOnStandby", "b", True)
            finally:
                try:
                    vr_set("power", "turnOffScreensTimeout", "f", screen)
                finally:
                    if old_handler is not None:
                        signal.signal(signal.SIGTERM, old_handler)
    records = list(frames.values())
    # Without a tracking universe (IMU fallback, e.g. after a boot without the headset worn) Rift games wait at
    # startup forever: SteamVR's own frames are all a run then counts (seen 2026-10-16, ~1.9/s, game GPU ~0 %).
    imu_fallback = False
    try:
        with open(vrserver_log, errors="replace") as f:
            f.seek(log_start if os.path.getsize(vrserver_log) >= log_start else 0)
            imu_fallback = "IMU fallback is active" in f.read(8 << 20)
    except OSError:
        pass
    duration = max(0.001, time.monotonic() - started)
    times = [r["system_time"] for r in records]
    span = max(times) - min(times) if len(times) > 1 else 0
    return {"app": args["app"], "seconds": seconds, "elapsed_s": round(duration, 3), "frames": len(records),
            "imu_fallback": imu_fallback,   # true: the result says nothing about the game (put the headset on once)
            "valid": len(records) >= 2 and span > 0 and not imu_fallback, "sample_span_s": round(span, 3),
            "fps": round((len(records) - 1) / span, 2) if span > 0 else None,
            "hitches": sum(r["presents"] >= 3 for r in records),
            "hitches_per_20s": round(20 * sum(r["presents"] >= 3 for r in records) / span, 2) if span > 0 else None,
            "game_gpu_busy_pct": round(100 * gpu / (gpu_seconds * 1e9), 2) if gpu_seconds else None,
            "render_gpu_ms": round(sum(r["gpu_ms"] for r in records) / len(records), 3) if records else None,
            "client_interval_ms": round(sum(r["cpu_ms"] for r in records) / len(records), 3) if records else None,
            "thread_wchan_samples": dict(waits), "settings": settings}


def bench(args):
    app = app_name(args["app"])
    # Launcher path is supplied by the owning agent's deployment record, not an arbitrary remote shell command.
    launcher = args["launcher"]
    if not os.path.isfile(launcher) or os.path.basename(launcher) != "launch.sh":
        raise ValueError("install the game before benchmarking")
    info, _ = CORE.game_info(app)
    exe = os.path.basename(info["exe"].replace("\\", "/"))
    os.makedirs(CORE.ROOT, exist_ok=True)
    with open(os.path.join(CORE.ROOT, ".bench.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return bench_guarded(dict(args, app=app, exe=exe), launcher)


# The measurement talks to SteamVR through ctypes (vrclient.so). A native crash there kills the process without
# running bench_run's finally, which once left pauseCompositorOnStandby=false (display kept on) on the Frame
# (2026-10-16). So it runs in a forked child, and the parent restores the display settings and stops the game from
# a guard file when the child returns no result, each restore in its own process (another crash can't skip it).
def guard_path():
    return os.path.join(CORE.ROOT, ".bench.guard.json")


def guard_write(**kw):
    CORE.write_json(guard_path(), kw)


def vrsetting_subprocess(section, key, kind, value):
    """fexrift.py vrsetting in a fresh process: {"error": code} from SteamVR, or {"error": "..."} if it failed."""
    arg = json.dumps({"section": section, "key": key, "kind": kind, "value": value})
    p = subprocess.run([sys.executable, "-I", os.path.join(CORE.HERE, "fexrift.py"), "vrsetting", arg],
                       capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": f"vrsetting exited {p.returncode}"}
    return out.get("result", {}) if out.get("ok") else {"error": out.get("error", "failed")}


def crash_cleanup():
    guard = CORE.read_json(guard_path(), {}) or {}
    try:
        if guard.get("pgid"):
            try:
                os.killpg(int(guard["pgid"]), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        subprocess.run([os.path.join(CORE.GEF, "files/bin/wineserver"), "-k"], env=CORE.wine_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    finally:
        vrsetting_subprocess("power", "pauseCompositorOnStandby", "b", True)
        if guard.get("screen") is not None:
            vrsetting_subprocess("power", "turnOffScreensTimeout", "f", float(guard["screen"]))


def bench_guarded(args, launcher):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:                                   # child: measure, report through the pipe
        os.close(read_fd)
        code = 0
        try:
            vr = VR()
            try:
                out = {"ok": True, "result": bench_run(args, launcher, vr)}
            finally:
                vr.close()
        except Exception as e:  # noqa: BLE001 - reported to the parent as the command's error
            out, code = {"ok": False, "error": f"{type(e).__name__}: {e}"}, 1
        with os.fdopen(write_fd, "w") as w:
            w.write(json.dumps(out))
        os._exit(code)
    os.close(write_fd)
    try:
        with os.fdopen(read_fd) as r:
            raw = r.read()
        _, status = os.waitpid(pid, 0)
        out = json.loads(raw) if raw.strip() else None
        if out is None:                             # crashed or killed: bench_run's own cleanup didn't run
            crash_cleanup()
            raise ValueError(f"benchmark process died ({'signal ' + str(-os.waitstatus_to_exitcode(status)) if os.WIFSIGNALED(status) else 'no result'}); display settings restored, game stopped")
        if not out["ok"]:
            raise ValueError(out["error"])
        return out["result"]
    finally:
        try:
            os.remove(guard_path())
        except FileNotFoundError:
            pass


def register(core):
    global CORE
    CORE = core
    core.COMMANDS.update(settings_get=settings_get, settings_set=settings_set, perf_prepare=perf_prepare, bench=bench)
    core.LAUNCHER_ENV_HOOKS.append(launcher_env)
    # Use the existing setup step so --steps revive prepares the variants too, without another download.
    def revive():
        result = core.step_revive()
        prepare_scales()
        return result
    core.STEPS = [(name, revive if name == "revive" else fn) for name, fn in core.STEPS]
