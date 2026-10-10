#!/usr/bin/env python3
"""FramePort agent: runs ON the Steam Frame (SteamOS, python3 stdlib only).

The PC app uploads this file to ~/.local/share/frameport/agent/ and calls:
    python3 frameport_agent.py <command>        (JSON arguments on stdin, one JSON object on stdout)

Commands: info, prepare, finalize, shortcuts, shortcut_status, launch_test, stop, set_settings, uninstall,
          install_lepton, list_installed, proton_status, install_proton, prepare_pcvr, finalize_pcvr,
          controller_models, list_screenshots, delete_screenshots (and more: see the cmd_* functions).
Streaming: python3 frameport_agent.py _keyboard   (a virtual keyboard: JSON lines on stdin, see keyboard_session)

Install layout (one Lepton container per game; same as the manual installs from 2026-09):
    ~/Applications/quest-frame/<pkg>/            anchor: launch.sh, deployment.json, artwork/ (always internal storage)
    <dest>/<pkg>/lepton-app/{game.apk,obb/}      game files (dest defaults to ~/Applications/quest-frame; another
                                                 drive: <mount>/FramePort, see `drives`; `move` moves a game)
    <dest>/<pkg>/lepton-data/                    container data + saves (kept across reinstalls)
    <dest>/<pkg>/lepton-shaders/, settings.conf, launch.log

PC VR (Oculus Rift) games packed for the Frame (id "rift.<slug>"), run by Proton (ARM64; x86 via FEX in Proton):
    ~/Applications/quest-frame/<id>/             anchor: launch.sh, deployment.json (kind "pcvr"), artwork/
    <dest>/<id>/game/                            the Windows game folder
    <dest>/<id>/revive/                          Revive (ReviveInjector.exe + DLLs)
    <dest>/<id>/compatdata/                      Proton prefix = saves (kept across reinstalls), launch.log
"""
import base64
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from types import SimpleNamespace

try:
    import fcntl
except ImportError:  # Windows: pc_revive loads this file for its VDF code only (GitHub #131)
    fcntl = None

AGENT_VERSION = 71
HOME = os.path.expanduser("~")
STEAM = os.path.join(HOME, ".local/share/Steam")
ANCHORS = os.path.join(HOME, "Applications/quest-frame")
LEPTON_APPID = "3029110"  # fallback when no appmanifest names Lepton
PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
VIDEO_CODEC_DIR = os.path.join(HOME, ".local/share/frameport/video-codec")
VIDEO_CODEC_FILES = ("libstagefrighthw.so", "media_codecs_frameport.xml", "podman.py", "COPYING.FFmpeg")
# Hardware video decoding (patch frame.hw_video_decode, per game): only launchers of games whose recipe has it put the
# shared codec's Podman wrapper first on Lepton's PATH (it adds the codec plugin to that game's container). Off for
# every game: FramePort's setting (VIDEO_CODEC_DIR/disabled); one game: FRAMEPORT_NO_HW_VIDEO=1 in its launch options.
VIDEO_CODEC_LINE = ('codec_dir="$HOME/.local/share/frameport/video-codec"\n'
                    '[[ "${FRAMEPORT_NO_HW_VIDEO:-0}" != 0 || -e "$codec_dir/disabled" || '
                    '! -x "$codec_dir/current/bin/podman" ]] || export PATH="$codec_dir/current/bin:$PATH"')
HW_VIDEO_PATCH = "frame.hw_video_decode"
# earlier codec lines: agent <= 70 (per-game codec extracted from the APK, line in every launcher) and PR #128's
# shared line (in every Lepton launcher)
OLD_CODEC_LINES = ('[[ ! -x "$app_dir/frameport-codec/bin/podman" ]] || '
                   'export PATH="$app_dir/frameport-codec/bin:$PATH"',
                   'codec_bin="$HOME/.local/share/frameport/video-codec/current/bin"\n'
                   '[[ ! -x "$codec_bin/podman" ]] || export PATH="$codec_bin:$PATH"')


class AgentError(Exception):
    pass


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", **kw)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def check_pkg(pkg):
    if not pkg or not PKG_RE.match(pkg):
        raise AgentError(f"bad package name {pkg!r}")
    return pkg


# ------------------------------------------------------------------------------------------ Steam / Lepton discovery
def steam_libraries():
    libs = [os.path.join(STEAM, "steamapps")]
    vdf = os.path.join(STEAM, "steamapps/libraryfolders.vdf")
    try:
        for path in re.findall(r'"path"\s+"([^"]+)"', open(vdf, encoding="utf-8", errors="replace").read()):
            p = os.path.join(path, "steamapps")
            if p not in libs and os.path.isdir(p):
                libs.append(p)
    except OSError:
        pass
    return libs


def find_app(name_regex):
    for lib in steam_libraries():
        for acf in glob.glob(os.path.join(lib, "appmanifest_*.acf")):
            try:
                text = open(acf, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            name = re.search(r'"name"\s+"([^"]*)"', text)
            if name and re.fullmatch(name_regex, name[1]):
                appid = re.search(r'"appid"\s+"(\d+)"', text)[1]
                installdir = re.search(r'"installdir"\s+"([^"]*)"', text)[1]
                return {"appid": appid, "name": name[1], "dir": os.path.join(lib, "common", installdir)}
    return None


def lepton_path():
    app = find_app(r"Lepton")
    candidates = (([os.path.join(app["dir"], "lepton")] if app else [])
                  + [os.path.join(STEAM, "steamapps/common/Lepton/lepton")])
    for c in candidates:
        if os.access(c, os.X_OK):
            return c, app
    return None, app


def find_app_id(appid):
    """{appid, name, dir} of an installed app by id (its appmanifest), or None."""
    for lib in steam_libraries():
        acf = os.path.join(lib, f"appmanifest_{appid}.acf")
        try:
            text = open(acf, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        installdir = re.search(r'"installdir"\s+"([^"]*)"', text)
        name = re.search(r'"name"\s+"([^"]*)"', text)
        state = re.search(r'"StateFlags"\s+"(\d+)"', text)
        d = os.path.join(lib, "common", installdir[1]) if installdir else None
        num = {k: int(m[1]) for k in ("BytesDownloaded", "BytesToDownload", "SizeOnDisk")
               for m in [re.search(rf'"{k}"\s+"(\d+)"', text)] if m}
        return {"appid": str(appid), "name": name[1] if name else "", "dir": d, "state": int(state[1]) if state else 0,
                "complete": bool(state and int(state[1]) & 4 and d and os.path.isdir(d)),
                "downloaded": num.get("BytesDownloaded", 0), "to_download": num.get("BytesToDownload", 0)}
    return None


# ------------------------------------------------------------------------------------------ Steam appinfo (binary)
APPINFO = os.path.join(STEAM, "appcache/appinfo.vdf")


def appinfo_entries(want=None, path=APPINFO):
    """Parse Steam's appinfo.vdf (v28/v29) into {appid: appinfo dict}; only the apps in `want` (or all)."""
    data = open(path, "rb").read()
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic >> 8 != 0x075644 or magic & 0xFF not in (0x28, 0x29):
        raise AgentError(f"unknown appinfo.vdf version {magic:#x}")
    v29 = magic & 0xFF == 0x29
    strings = []
    end = len(data)
    if v29:
        str_off = struct.unpack_from("<q", data, 8)[0]
        n = struct.unpack_from("<I", data, str_off)[0]
        p = str_off + 4
        for _ in range(n):
            e = data.index(b"\0", p)
            strings.append(data[p:e].decode("utf-8", "replace"))
            p = e + 1
        end = str_off

    def key(p):
        if v29:
            return strings[struct.unpack_from("<I", data, p)[0]], p + 4
        e = data.index(b"\0", p)
        return data[p:e].decode("utf-8", "replace"), e + 1

    def kv(p):
        obj = {}
        while True:
            t = data[p]
            p += 1
            if t == 8:
                return obj, p
            k, p = key(p)
            if t == 0:
                obj[k], p = kv(p)
            elif t == 1:
                e = data.index(b"\0", p)
                obj[k] = data[p:e].decode("utf-8", "replace")
                p = e + 1
            elif t in (2, 3, 4, 6):  # int32, float, pointer, color
                obj[k] = struct.unpack_from("<f" if t == 3 else "<i", data, p)[0]
                p += 4
            elif t in (7, 10):  # uint64 / int64
                obj[k] = struct.unpack_from("<Q", data, p)[0]
                p += 8
            else:
                raise AgentError(f"unsupported appinfo value type {t}")

    out, p = {}, 16 if v29 else 8
    while p + 8 <= end:
        appid, size = struct.unpack_from("<II", data, p)
        if appid == 0:
            break
        body = p + 8
        if want is None or appid in want:
            try:
                obj, _ = kv(body + 60)  # infostate, last updated, pics token, sha1, change number, binary sha1
                out[appid] = obj.get("appinfo", obj)
            except (AgentError, IndexError, struct.error, ValueError):
                pass
        p = body + size
    return out


def arm64_compat_tools():
    """Steam compat tools for this device from Valve's ARM64 compat list app (appinfo extended.compat_tools):
    {name: {appid, display_name, require_tool_appid, aliases, from_oslist}}. Found dynamically (no hardcoded ids)."""
    best = {}
    for info in appinfo_entries().values():
        tools = ((info.get("extended") or {}).get("compat_tools"))
        if isinstance(tools, dict) and any(k.endswith("-arm64") for k in tools):
            if len(tools) > len(best):
                best = tools
    return best


def _compat_entries(keep):
    """Compat tools from the ARM64 compat list that `keep(name, entry)` selects, newest first, with install state."""
    out = []
    for name, t in arm64_compat_tools().items():
        if not isinstance(t, dict) or "appid" not in t or not keep(name, t):
            continue
        app = find_app_id(t["appid"])
        req = t.get("require_tool_appid")
        if app and app["complete"]:
            req = tool_manifest(app["dir"]).get("require_tool_appid") or req
        req_app = find_app_id(req) if req else None
        ver = re.findall(r"\d+", name)
        out.append({"name": name, "appid": int(t["appid"]), "display_name": t.get("display_name", name),
                    "aliases": t.get("aliases", ""), "experimental": "experimental" in name,
                    "installed": bool(app and app["complete"]), "dir": app["dir"] if app else None,
                    "require_tool_appid": int(req) if req else None,
                    "require_installed": (not req) or bool(req_app and req_app["complete"]),
                    "require_dir": req_app["dir"] if req_app else None,
                    "sort": (0 if "experimental" in name else 1, int(ver[0]) if ver else 0)})
    out.sort(key=lambda t: t.pop("sort"), reverse=True)
    return out


def proton_tools():
    """Proton builds for Windows games on this (ARM64) device, newest first, with install state."""
    return _compat_entries(lambda name, t: t.get("from_oslist") == "windows")


def linux_x86_tools():
    """Compat tools that run Linux x86_64 programs on this ARM64 device: FEX-Emu (app "fex" 3127680; its
    fex-compat-tool runs the program with FEX on SteamOS's x86 guest root /usr/share/guestos/fex-mesa)."""
    # (the Steam Linux Runtimes are listed as Linux tools too: they are what FEX needs, not a translator)
    return _compat_entries(lambda name, t: "fex" in f"{name} {t.get('display_name', '')}".lower())


def compat_tools(kind):
    return linux_x86_tools() if kind == "linux_x86" else proton_tools()


def pick_tool(kind, tools, wanted=None):
    if kind != "linux_x86":
        return pick_proton(tools, wanted)
    if wanted:
        return next((t for t in tools if wanted in (t["name"], t["display_name"])), None)
    ready = [t for t in tools if t["installed"] and t["require_installed"]]
    return (ready or tools or [None])[0]


def tool_manifest(tool_dir):
    """commandline / require_tool_appid from a compat tool's toolmanifest.vdf (text KeyValues)."""
    try:
        text = open(os.path.join(tool_dir, "toolmanifest.vdf"), encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    out = {}
    for k in ("commandline", "require_tool_appid", "version"):
        m = re.search(rf'"{k}"\s+"((?:[^"\\]|\\.)*)"', text)
        if m:
            out[k] = m[1].replace('\\"', '"')
    if "require_tool_appid" in out:
        out["require_tool_appid"] = int(out["require_tool_appid"])
    return out


def compat_command(tool_dir, verb="waitforexitandrun", depth=0):
    """argv prefix Steam would run for a compat tool: its required runtime's command first, then the tool's own
    (e.g. SteamLinuxRuntime_4-arm64/_v2-entry-point --verb=... -- Proton/proton waitforexitandrun)."""
    man = tool_manifest(tool_dir)
    cmd = man.get("commandline")
    if not cmd:
        raise AgentError(f"no toolmanifest.vdf commandline in {tool_dir}")
    own = [os.path.join(tool_dir, a.lstrip("/")) if i == 0 else a
           for i, a in enumerate(shlex.split(cmd.replace("%verb%", verb)))]
    req = man.get("require_tool_appid")
    if req and depth < 3:
        app = find_app_id(req)
        if not app or not app["complete"]:
            raise AgentError(f"{os.path.basename(tool_dir)} needs Steam app {req} (runtime), which isn't installed")
        return compat_command(app["dir"], verb, depth + 1) + own
    return own


def pick_proton(tools, wanted=None):
    """The Proton build a game uses: the one asked for (name, display name or alias such as proton-experimental),
    else the newest stable one (owner's choice 2026-10-05 after an A/B in the headset: Rick and Morty felt much
    smoother on Proton 11 than on Experimental; a game that fails gets "try Proton Experimental" suggested).
    Returned even when not installed yet: the installer installs it."""
    def matches(t, w):
        return w in (t["name"], t["display_name"]) or w in [a.strip() for a in t["aliases"].split(",")]
    if wanted:
        return next((t for t in tools if matches(t, wanted)), None)
    stable = [t for t in tools if not t["experimental"]]
    ready = [t for t in stable if t["installed"] and t["require_installed"]]
    return (ready or stable or tools or [None])[0]


def openxr_runtime():
    for d in (os.path.join(HOME, ".config/openxr/1"), "/etc/xdg/openxr/1", "/usr/share/openxr/1"):
        p = os.path.join(d, "active_runtime.json")
        if os.path.exists(p):
            try:
                return {"path": os.path.realpath(p), "name": json.load(open(p))["runtime"].get("name")}
            except (OSError, ValueError, KeyError):
                return {"path": os.path.realpath(p), "name": None}
    return None


def cmd_proton_status(args):
    """Proton (kind "proton", the default) or the Linux x86_64 tool FEX (kind "linux_x86"): installed or not."""
    kind = args.get("kind") or "proton"
    try:
        tools = compat_tools(kind)
    except (OSError, AgentError) as exc:
        return {"tools": [], "ready": None, "error": str(exc), "openxr": openxr_runtime()}
    ready = pick_tool(kind, tools, args.get("tool"))
    ok = bool(ready and ready["installed"] and ready["require_installed"])
    download = {"done": 0, "total": 0}
    if ready and not ok:
        for a in (ready["appid"], ready.get("require_tool_appid")):
            app = find_app_id(a) if a else None
            if app and not app["complete"]:
                download["done"] += app["downloaded"]
                download["total"] += app["to_download"]
    return {"tools": tools, "ready": ready if ok else None, "suggested": ready, "openxr": openxr_runtime(),
            "download": download}


SELFTEST_DIR = os.path.join(HOME, ".local/share/frameport/proton-selftest")


SESSION_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "GAMESCOPE_WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE")


def session_env():
    """The display session variables of the running Steam client (gamescope's X/Wayland), for headless launches."""
    p = run(["pgrep", "-x", "steam"])
    for pid in p.stdout.split():
        try:
            raw = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
        except OSError:
            continue
        env = dict(kv.decode(errors="replace").split("=", 1) for kv in raw if b"=" in kv)
        return {k: env[k] for k in SESSION_VARS if k in env}
    return {}


def run_tree(cmd, env, cwd, log_path, timeout):
    """Run a command in its own process group with output to a file; on timeout kill the whole group (Wine leaves
    children that keep pipes open, so subprocess.run(timeout=...) would hang). Returns (output, exit code | None)."""
    import signal

    with open(log_path, "wb") as log:
        p = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        try:
            code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            code = None
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    p.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
    return open(log_path, errors="replace").read(), code


def stop_prefix(tool, prefix):
    """wineserver -k for a Proton prefix (ends every process of that prefix)."""
    wineserver = next(iter(glob.glob(os.path.join(tool["dir"], "files/bin*/wineserver"))), None)
    if wineserver and os.path.isdir(prefix):
        try:
            run([wineserver, "-k"], env=dict(os.environ, WINEPREFIX=prefix), timeout=20)
        except subprocess.TimeoutExpired:
            pass


def cmd_proton_selftest(args):
    """Run a Windows program (cmd.exe) under the Frame's Proton, the way FramePort launches PC VR games (SteamGameId
    set so Proton sets up VR), and report whether it ran and whether wineopenxr was registered as the OpenXR runtime."""
    tool = pick_proton(proton_tools(), args.get("tool"))
    if not tool or not (tool["installed"] and tool["require_installed"]):
        raise AgentError("Proton isn't installed on the Frame yet")
    os.makedirs(os.path.join(SELFTEST_DIR, "compatdata"), exist_ok=True)
    appid = str(shortcut_appid("frameport-proton-selftest", "FramePort"))
    env = dict(os.environ, SteamAppId=appid, STEAM_COMPAT_APP_ID=appid,
               STEAM_COMPAT_DATA_PATH=os.path.join(SELFTEST_DIR, "compatdata"),
               STEAM_COMPAT_CLIENT_INSTALL_PATH=STEAM, STEAM_COMPAT_INSTALL_PATH=SELFTEST_DIR,
               STEAM_COMPAT_LIBRARY_PATHS=SELFTEST_DIR, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}",
               PROTON_LOG_DIR=SELFTEST_DIR)
    for k, v in session_env().items():
        env.setdefault(k, v)
    if args.get("vr", True):
        env["SteamGameId"] = appid  # Proton sets up vrclient/wineopenxr only for "game" processes
    if args.get("log"):
        env["PROTON_LOG"] = "1"
    for k, v in (args.get("env") or {}).items():
        env[str(k)] = str(v)
    marker = os.path.join(SELFTEST_DIR, "marker.txt")
    if os.path.exists(marker):
        os.remove(marker)
    cmd = compat_command(tool["dir"], args.get("verb", "waitforexitandrun")) + \
        ["c:\\windows\\system32\\cmd.exe", "/c", f"echo FRAMEPORT_PROTON_OK>{windows_path(marker)}"]
    start = time.time()
    out, code = run_tree(cmd, env, SELFTEST_DIR, os.path.join(SELFTEST_DIR, "selftest.log"),
                         int(args.get("timeout_s", 600)))
    stop_prefix(tool, os.path.join(SELFTEST_DIR, "compatdata/pfx"))
    reg = os.path.join(SELFTEST_DIR, "compatdata/pfx/system.reg")
    text = open(reg, errors="replace").read() if os.path.exists(reg) else ""
    m = re.search(r'\[Software\\\\Khronos\\\\OpenXR\\\\1\][^\[]*"ActiveRuntime"="([^"]*)"', text)
    plog = os.path.join(SELFTEST_DIR, f"steam-{appid}.log")
    ran = os.path.exists(marker) and "FRAMEPORT_PROTON_OK" in open(marker, errors="replace").read()
    return {"tool": tool["name"], "ran": ran, "exit_code": code, "proton_log": plog
            if os.path.exists(plog) else None,
            "seconds": round(time.time() - start), "openxr_runtime": m[1] if m else None,
            "prefix_created": bool(text), "log_tail": out[-3000:]}


def xr_probe(payload):
    """(child process) Create an OpenXR instance with XR_KHR_convert_timespec_time through Proton's loader and call
    xrConvertTimespecTimeToTimeKHR. Prints one JSON line. The layer (if any) is enabled via the environment."""
    import ctypes as C

    args = json.loads(payload)
    out = {"loader": args["loader"]}
    try:
        xr = C.CDLL(args["loader"])
        xr.xrGetInstanceProcAddr.argtypes = [C.c_uint64, C.c_char_p, C.POINTER(C.c_void_p)]
        xr.xrDestroyInstance.argtypes = [C.c_uint64]

        class AppInfo(C.Structure):
            _fields_ = [("applicationName", C.c_char * 128), ("applicationVersion", C.c_uint32),
                        ("engineName", C.c_char * 128), ("engineVersion", C.c_uint32), ("apiVersion", C.c_uint64)]

        class CreateInfo(C.Structure):
            _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("createFlags", C.c_uint64), ("app", AppInfo),
                        ("layerCount", C.c_uint32), ("layers", C.c_void_p), ("extCount", C.c_uint32),
                        ("exts", C.POINTER(C.c_char_p))]

        class LayerProps(C.Structure):
            _fields_ = [("type", C.c_int), ("next", C.c_void_p), ("layerName", C.c_char * 256),
                        ("specVersion", C.c_uint64), ("layerVersion", C.c_uint32), ("description", C.c_char * 256)]

        n = C.c_uint32()
        xr.xrEnumerateApiLayerProperties(0, C.byref(n), None)
        props = (LayerProps * max(n.value, 1))()
        for p in props:
            p.type = 1  # XR_TYPE_API_LAYER_PROPERTIES
        xr.xrEnumerateApiLayerProperties(n.value, C.byref(n), props)
        out["layers"] = [props[i].layerName.decode() for i in range(n.value)]
        exts = (C.c_char_p * 1)(b"XR_KHR_convert_timespec_time")
        ci = CreateInfo(3, None, 0, AppInfo(b"FramePort timefix probe", 1, b"FramePort", 1, 1 << 48), 0, None, 1, exts)
        inst = C.c_uint64()
        out["create"] = xr.xrCreateInstance(C.byref(ci), C.byref(inst))
        if out["create"] == -4:  # the runtime sometimes fails the first attempt (as Proton's own probe sees)
            out["create"] = xr.xrCreateInstance(C.byref(ci), C.byref(inst))
        if out["create"] == 0:
            fn = C.c_void_p()
            out["proc"] = xr.xrGetInstanceProcAddr(inst.value, b"xrConvertTimespecTimeToTimeKHR", C.byref(fn))
            if out["proc"] == 0 and fn.value:
                class Ts(C.Structure):
                    _fields_ = [("tv_sec", C.c_long), ("tv_nsec", C.c_long)]
                now = Ts()
                C.CDLL(None).clock_gettime(1, C.byref(now))
                t = C.c_int64()
                conv = C.CFUNCTYPE(C.c_int, C.c_uint64, C.POINTER(Ts), C.POINTER(C.c_int64))(fn.value)
                out["convert"] = conv(inst.value, C.byref(now), C.byref(t))
                out["time"] = t.value
            xr.xrDestroyInstance(inst.value)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(out))


def cmd_xr_layer_test(args):
    """Does the timefix layer fix time conversion on this runtime? Runs xr_probe with Proton's OpenXR loader, without
    and with the layer (layer_dir = folder with XR_APILAYER_FRAMEPORT_timefix.json)."""
    tool = pick_proton(proton_tools(), args.get("tool"))
    loader = next(iter(glob.glob(os.path.join(tool["dir"], "files/lib/aarch64-linux-gnu/libopenxr_loader.so.1")))
                  if tool and tool.get("dir") else [], None) or "/opt/steamvr/bin/linuxarm64/libopenxr_loader.so"
    results = {}
    for name, layer in (("without_layer", None), ("with_layer", args.get("layer_dir"))):
        if name == "with_layer" and not layer:
            continue
        env = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}", XR_LOADER_DEBUG="error")
        env.pop("XR_API_LAYER_PATH", None)
        env.pop("XR_ENABLE_API_LAYERS", None)
        for k, v in session_env().items():
            env.setdefault(k, v)
        if layer:
            env.update(XR_API_LAYER_PATH=os.path.expanduser(layer), XR_ENABLE_API_LAYERS=XR_LAYER)
        try:
            p = run([sys.executable, os.path.abspath(__file__), "_xr_probe", json.dumps({"loader": loader})],
                    env=env, timeout=60)
            line = next((ln for ln in reversed(p.stdout.splitlines()) if ln.startswith("{")), None)
            results[name] = json.loads(line) if line else {"error": (p.stderr or p.stdout)[-800:]}
        except subprocess.TimeoutExpired:
            results[name] = {"error": "timed out"}
    return results


def write_stub_manifest(appid, name, installdir, lib=None):
    """An appmanifest with StateFlags 'update required': Steam downloads the app on its next start."""
    lib = lib or os.path.join(STEAM, "steamapps")
    path = os.path.join(lib, f"appmanifest_{appid}.acf")
    if os.path.exists(path):
        return False
    text = ('"AppState"\n{\n' + "".join(f'\t"{k}"\t\t"{v}"\n' for k, v in (
        ("appid", appid), ("Universe", 1), ("name", name), ("StateFlags", 1026), ("installdir", installdir),
        ("AutoUpdateBehavior", 0))) + "}\n")
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)
    return True


def cmd_install_proton(args):
    """Install Proton for Windows games (plus the Steam Linux Runtime it needs). mode "request" asks Steam
    (steam://install; the user confirms in the headset); mode "unattended" writes appmanifest stubs and restarts
    Steam so it downloads them by itself. kind "linux_x86": FEX (+ its runtime) for x86_64 Linux apps instead."""
    kind = args.get("kind") or "proton"
    tools = compat_tools(kind)
    tool = pick_tool(kind, tools, args.get("tool"))
    if not tool:
        raise AgentError("this Steam has no ARM64 Proton in its compat list (update SteamOS/Steam)" if kind == "proton"
                         else "this Steam has no x86 translation tool (FEX) in its compat list (update SteamOS/Steam)")
    need = [a for a, ok in ((tool["appid"], tool["installed"]),
                            (tool["require_tool_appid"], tool["require_installed"])) if a and not ok]
    if not need:
        return {"tool": tool["name"], "installed": True, "requested": []}
    if args.get("mode") == "unattended":
        info = appinfo_entries(set(need))
        stubs = []
        for a in need:
            common = (info.get(a) or {}).get("common") or {}
            installdir = ((info.get(a) or {}).get("config") or {}).get("installdir")
            if not installdir:
                raise AgentError(f"Steam's app cache has no install folder for app {a}")
            stubs.append({"appid": a, "name": common.get("name", str(a)), "installdir": installdir})
        payload = json.dumps({"stubs": stubs})
        unit = f"frameport-tools-{int(time.time())}"
        run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}", "--setenv=HOME=" + HOME,
             sys.executable, os.path.abspath(__file__), "_tools_worker", payload])
        return {"tool": tool["name"], "installed": False, "requested": need, "mode": "unattended", "unit": unit,
                "hint": f"Steam restarts once and downloads {tool['display_name']} in the background "
                        "(a few hundred MB)."}
    for a in need:
        subprocess.Popen(["steam", "-ifrunning", f"steam://install/{a}"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    return {"tool": tool["name"], "installed": False, "requested": need, "mode": "request",
            "hint": "Put the headset on and confirm the install in Steam (one dialog per item)."}


def stop_steam():
    service = run(["systemctl", "--user", "is-active", "--quiet", "steam.service"]).returncode == 0
    if service:
        run(["systemctl", "--user", "stop", "steam.service"])
    else:
        run(["steam", "-shutdown"])
    for _ in range(40):
        if run(["pgrep", "-x", "steam"]).returncode:
            break
        time.sleep(1)
    if run(["pgrep", "-x", "steam"]).returncode == 0:
        raise AgentError("Steam did not close")
    for _ in range(15):  # Steam's helpers can still be writing its config (shortcuts.vdf) for a moment
        if run(["pgrep", "-f", "steamwebhelper|steam.sh|reaper SteamLaunch"]).returncode:
            break
        time.sleep(1)
    time.sleep(2)
    return service


def start_steam(service):
    if service:
        run(["systemctl", "--user", "start", "steam.service"])
    else:
        subprocess.Popen(["systemd-run", "--user", "--collect", f"--unit=frameport-steam-{int(time.time())}",
                          "/usr/bin/steam"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def tools_worker(payload):
    args = json.loads(payload)
    service = True
    try:
        service = stop_steam()
        for st in args["stubs"]:
            write_stub_manifest(st["appid"], st["name"], st["installdir"])
    finally:
        start_steam(service)


def steam_users():
    return sorted(d for d in os.listdir(os.path.join(STEAM, "userdata")) if d.isdigit() and d != "0") \
        if os.path.isdir(os.path.join(STEAM, "userdata")) else []


STEAMID64_BASE = 76561197960265728


def active_steam_user():
    """userdata folder (account id) of the most recent Steam login (config/loginusers.vdf), if it has one."""
    try:
        text = open(os.path.join(STEAM, "config/loginusers.vdf"), encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    users = steam_users()
    for sid, body in re.findall(r'"(\d{17})"\s*\{([^}]*)\}', text):
        if re.search(r'"MostRecent"\s+"1"', body) and str(int(sid) - STEAMID64_BASE) in users:
            return str(int(sid) - STEAMID64_BASE)
    return None


def library_users():
    """Steam accounts whose library gets FramePort's shortcuts: every account on the Frame, the signed-in one first
    (loginusers.vdf's MostRecent isn't always the account signed in on the Frame: GitHub #4/#21 got shortcuts in the
    other account; an extra shortcut in an unused account does no harm)."""
    users = steam_users()
    if not users:
        raise AgentError("Steam has no signed-in account on this Frame yet; sign in to Steam on the Frame first")
    active = active_steam_user()
    return [active] + [u for u in users if u != active] if active else users


def shortcut_appid_for(exe):
    """appid of the shortcut whose Exe is `exe` in the signed-in account's library (any account if unknown); None
    when it isn't in the library."""
    for u in library_users():
        vdf = os.path.join(STEAM, "userdata", u, "config/shortcuts.vdf")
        try:
            root = vdf_decode(open(vdf, "rb").read()) if os.path.exists(vdf) else {}
        except (OSError, AgentError):
            continue
        for v in (root.get("shortcuts") or {}).values():
            if isinstance(v, dict) and v.get("Exe") == exe and v.get("appid"):
                return v["appid"] & 0xFFFFFFFF
    return None


def container_running(appid):
    p = run(["podman", "ps", "--format", "{{.Names}}"])
    return f"lepton-steamlaunch-{appid}" in p.stdout.split()


CONTAINERS_CONF = os.path.join(HOME, ".config/containers/containers.conf")


def ensure_host_fixes():
    """Rootless podman (used by Lepton) leaks one kernel session keyring per container start; after ~200 launches
    since boot every game fails with 'crun: create keyring ...: Disk quota exceeded'. keyring=false stops that."""
    changed = []
    text = open(CONTAINERS_CONF).read() if os.path.exists(CONTAINERS_CONF) else ""
    if not re.search(r"^\s*keyring\s*=", text, re.M):
        note = "# FramePort: stop rootless podman leaking a kernel keyring per container start (Lepton launches)\n"
        if re.search(r"^\[containers\]\s*$", text, re.M):
            text = re.sub(r"^\[containers\]\s*$", "[containers]\n" + note + "keyring = false", text, count=1,
                          flags=re.M)
        else:
            sep = "\n" if text and not text.endswith("\n") else ""
            text = text + sep + "[containers]\n" + note + "keyring = false\n"
        os.makedirs(os.path.dirname(CONTAINERS_CONF), exist_ok=True)
        with open(CONTAINERS_CONF, "w") as f:
            f.write(text)
        changed.append("podman keyring=false")
    try:
        upgraded = upgrade_launchers()
    except Exception:  # noqa: BLE001
        upgraded = []
    if upgraded:
        changed.append(f"launchers: exit watchdog, dashboard, play log ({len(upgraded)})")
    try:
        old = remove_old_codec_dirs()
    except Exception:  # noqa: BLE001
        old = []
    if old:
        changed.append(f"per-game video codec folders removed ({len(old)})")
    try:
        entries = refresh_desktop_entries()
    except Exception:  # noqa: BLE001
        entries = []
    if entries:
        changed.append(f"Desktop Mode entries ({len(entries)})")
    return changed


def key_usage():
    try:
        for line in open("/proc/key-users"):
            f = line.split()
            if f[0].rstrip(":") == str(os.getuid()):
                used, limit = f[3].split("/")
                return {"keys": int(used), "max_keys": int(limit)}
    except (OSError, ValueError, IndexError):
        pass
    return {}


POWER_SUPPLY = "/sys/class/power_supply"
CHARGER_TYPES = ("Mains", "USB", "USB_C", "USB_PD", "USB_PD_DRP", "USB_DCP", "USB_CDP", "USB_ACA", "Wireless")


def battery_state():
    """The Frame's battery: {"percent", "status" (Charging/Discharging/Full/Not charging), "plugged", "draining"};
    None without one. "plugged" = a charger reports online (or the battery says it's charging/full); "draining" = the
    battery's own gauge says Discharging (with a charger: it supplies less than the Frame uses, or just booted).
    On the Frame (2026-10-03): max1720x_bat (Battery), pm8550b-charger (Unknown), tcpm …typec (USB, online=1)."""
    def read(path):
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            return ""
    battery, plugged = None, False
    try:
        names = sorted(os.listdir(POWER_SUPPLY))
    except OSError:
        return None
    for name in names:
        d = os.path.join(POWER_SUPPLY, name)
        kind = read(os.path.join(d, "type"))
        if kind == "Battery" and battery is None and read(os.path.join(d, "capacity")).isdigit():
            battery = {"percent": int(read(os.path.join(d, "capacity"))), "status": read(os.path.join(d, "status"))}
        elif kind in CHARGER_TYPES and read(os.path.join(d, "online")) == "1":
            plugged = True
    if battery is not None:
        battery["plugged"] = plugged or battery["status"] in ("Charging", "Full")
        battery["draining"] = battery["status"] == "Discharging"
    return battery


BOOT_STATE = os.path.join(HOME, ".cache/frameport-boot.json")
# what the last lines of a boot's journal say when it was shut down or rebooted on purpose (a crash, a GPU hang that
# reset the Frame or a pulled battery leaves none of these)
CLEAN_SHUTDOWN = ("Reached target System Power Off", "Reached target System Reboot", "Reached target Shutdown",
                  "System is powering down", "System is rebooting", "systemd-shutdown", "Power-Off", "Rebooting.")


def boot_state():
    """This boot and how the previous one ended: {"boot_id", "boot_time", "prev_clean" (True/False/None = unknown),
    "last_launch" ({"package", "title", "time"}: the FramePort game started last before this boot)}. Worked out once
    per boot (cached), so the app can say "your Frame restarted while <game> was running"."""
    try:
        boot_id = open("/proc/sys/kernel/random/boot_id").read().strip()
    except OSError:
        return None
    try:
        cached = json.load(open(BOOT_STATE))
        if cached.get("boot_id") == boot_id:
            return cached
    except (OSError, ValueError):
        pass
    boot_time = None
    try:
        for line in open("/proc/stat"):
            if line.startswith("btime "):
                boot_time = int(line.split()[1])
    except OSError:
        pass
    try:
        tail = run(["journalctl", "-b", "-1", "-n", "120", "-q", "--no-pager", "-o", "cat"]).stdout
    except OSError:
        tail = ""
    prev_clean = any(m in tail for m in CLEAN_SHUTDOWN) if tail.strip() else None
    last = None
    for d in cmd_list_installed({})["games"]:
        log = os.path.join(d["base"], "launch.log")
        try:
            t = os.path.getmtime(log)
        except OSError:
            continue
        if (boot_time is None or t < boot_time) and (last is None or t > last["time"]):
            last = {"package": d["package"], "title": d.get("title") or d["package"], "time": t}
    state = {"boot_id": boot_id, "boot_time": boot_time, "prev_clean": prev_clean, "last_launch": last}
    try:
        os.makedirs(os.path.dirname(BOOT_STATE), exist_ok=True)
        with open(BOOT_STATE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass
    return state


def cmd_battery(args):
    return {"battery": battery_state()}


def cmd_info(args):
    lepton, app = lepton_path()
    osr = {}
    try:
        for line in open("/etc/os-release"):
            k, _, v = line.strip().partition("=")
            osr[k] = v.strip('"')
    except OSError:
        pass
    st = os.statvfs(HOME)
    return {
        "agent_version": AGENT_VERSION, "hostname": os.uname().nodename, "arch": os.uname().machine,
        "os": osr.get("NAME"), "os_version": osr.get("VERSION_ID"), "build_id": osr.get("BUILD_ID"),
        "lepton": lepton, "lepton_app": app, "lepton_dev": find_app(r"Lepton Development"),
        "steam_users": steam_users(), "free_bytes": st.f_bavail * st.f_frsize,
        "installed": cmd_list_installed({})["games"],
        "steam_running": run(["pgrep", "-x", "steam"]).returncode == 0,
        "host_fixes": ensure_host_fixes(), "kernel_keys": key_usage(),
        "proton": cmd_proton_status({}) if args.get("proton", True) else None,
        "boot": boot_state(),
        "battery": battery_state(),
    }


def cmd_install_lepton(args):
    """Ask Steam to install Lepton (requires Developer Mode)."""
    lepton, app = lepton_path()
    if lepton:
        return {"installed": True, "path": lepton}
    appid = (app or {}).get("appid") or args.get("appid") or LEPTON_APPID
    subprocess.Popen(["steam", f"steam://install/{appid}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return {"installed": False, "requested": appid,
            "hint": "Enable Developer Mode, then confirm the install in Steam (or launch 'Lepton Development' once)."}


# ------------------------------------------------------------------------------------- Steam shortcuts (binary VDF)
TYPE_MAP, TYPE_STRING, TYPE_INT, TYPE_END = 0, 1, 2, 8


def shortcut_appid(exe, title):
    return zlib.crc32((exe + title).encode("utf-8")) | 0x80000000


def vdf_decode(data):
    pos = 0

    def cstring():
        nonlocal pos
        end = data.index(b"\0", pos)
        value = data[pos:end].decode("utf-8", "surrogateescape")
        pos = end + 1
        return value

    def node():
        nonlocal pos
        out = {}
        while True:
            kind = data[pos]
            pos += 1
            if kind == TYPE_END:
                return out
            key = cstring()
            if kind == TYPE_MAP:
                value = node()
            elif kind == TYPE_STRING:
                value = cstring()
            elif kind == TYPE_INT:
                value = struct.unpack_from("<I", data, pos)[0]
                pos += 4
            else:
                raise AgentError(f"unsupported VDF value type {kind}; not modifying shortcuts.vdf")
            if key in out:
                raise AgentError("duplicate VDF key; not modifying shortcuts.vdf")
            out[key] = value

    root = node()
    if any(b != TYPE_END for b in data[pos:]):
        raise AgentError("unexpected trailing data in shortcuts.vdf; not modifying it")
    return root


def vdf_encode(obj):
    out = bytearray()
    for key, value in obj.items():
        name = key.encode("utf-8", "surrogateescape") + b"\0"
        if isinstance(value, dict):
            out += bytes([TYPE_MAP]) + name + vdf_encode(value)
        elif isinstance(value, str):
            out += bytes([TYPE_STRING]) + name + value.encode("utf-8", "surrogateescape") + b"\0"
        else:
            out += bytes([TYPE_INT]) + name + struct.pack("<I", value & 0xFFFFFFFF)
    return bytes(out) + bytes([TYPE_END])


def upsert_shortcut(vdf_path, exe, title, start_dir, icon="", tag="Quest on Frame", launch_options="", tags=None,
                    openvr=True, write=True):
    """Add/update a non-Steam shortcut (matched by Exe, so its appid never changes). `tags` (genres, the user's tags)
    are merged with tags already on the shortcut, so ones set in Steam are kept. write=False: change nothing, return
    (appid, whether shortcuts.vdf would change)."""
    data = open(vdf_path, "rb").read() if os.path.exists(vdf_path) else b""
    root = vdf_decode(data) if data else {"shortcuts": {}}
    shortcuts = root.setdefault("shortcuts", {})
    entry = next((v for v in shortcuts.values() if isinstance(v, dict) and v.get("Exe") == exe), None)
    ident = entry["appid"] if entry else shortcut_appid(exe, title)
    if entry is None:
        entry = {"appid": ident, "LastPlayTime": 0, "tags": {"0": tag}}
        shortcuts[str(max([int(k) for k in shortcuts if k.isdigit()] + [-1]) + 1)] = entry
    if tags:
        have = [v for v in (entry.get("tags") or {}).values() if isinstance(v, str)]
        merged = list(dict.fromkeys(have + [t for t in [tag] + list(tags) if t]))
        entry["tags"] = {str(i): t for i, t in enumerate(merged)}
    entry.update(appname=title, Exe=exe, StartDir=start_dir, icon=icon or entry.get("icon", ""), ShortcutPath="",
                 LaunchOptions=launch_options, IsHidden=0, AllowDesktopConfig=1, AllowOverlay=1,
                 OpenVR=1 if openvr else 0, Devkit=0, DevkitGameID="", DevkitOverrideAppID=0, FlatpakAppID="")
    if not write:
        return ident, vdf_encode(root) != data
    if data:
        backup_vdf(vdf_path)
    os.makedirs(os.path.dirname(vdf_path), exist_ok=True)
    tmp = vdf_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(vdf_encode(root))
    os.replace(tmp, vdf_path)
    return ident


def prune_shortcuts(vdf_path, title, keep_exe, tag):
    """Remove FramePort-tagged shortcuts for `title` whose Exe differs from keep_exe (a reinstall that changed the
    launch command, e.g. Revive -> direct, would otherwise leave the old, crashing shortcut behind). Returns the
    removed appids."""
    if not os.path.exists(vdf_path):
        return []
    tags = (tag,) if isinstance(tag, str) else tuple(tag)  # one tag, or several (current + earlier names)
    root = vdf_decode(open(vdf_path, "rb").read())
    sc = root.get("shortcuts", {})

    def stale(v):
        return (isinstance(v, dict) and v.get("appname") == title and v.get("Exe") != keep_exe
                and any(t in (v.get("tags") or {}).values() for t in tags))
    removed = [v.get("appid") for v in sc.values() if stale(v)]
    if not removed:
        return []
    keep = [v for v in sc.values() if not stale(v)]
    root["shortcuts"] = {str(i): v for i, v in enumerate(keep)}
    backup_vdf(vdf_path)
    with open(vdf_path + ".tmp", "wb") as f:
        f.write(vdf_encode(root))
    os.replace(vdf_path + ".tmp", vdf_path)
    return removed


VDF_BACKUPS = 5


def backup_vdf(vdf_path):
    """Keep a copy before changing shortcuts.vdf (the last VDF_BACKUPS are kept)."""
    shutil.copy2(vdf_path, f"{vdf_path}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
    for old in sorted(glob.glob(f"{vdf_path}.backup-*"))[:-VDF_BACKUPS]:
        try:
            os.remove(old)
        except OSError:
            pass


def remove_tree(path):
    """Remove a file or folder tree, including what Lepton's containers leave in a game's data: overlayfs work dirs
    with mode 000 (and whiteout device files in them), which shutil.rmtree(ignore_errors=True) silently skipped, so
    "remove saves" left lepton-data behind. Last resort: podman unshare (files owned by the container's user ids)."""
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        os.remove(path)
        return
    for root, dirs, _files in os.walk(path):  # top-down: fix a folder's mode before walking into it
        for d in dirs:
            p = os.path.join(root, d)
            if not os.path.islink(p):
                try:
                    os.chmod(p, 0o700)
                except OSError:
                    pass
    shutil.rmtree(path, ignore_errors=True)
    if os.path.lexists(path) and shutil.which("podman"):
        run(["podman", "unshare", "rm", "-rf", path])


def grid_files(grid, appid):
    """A shortcut's grid artwork (<appid>p.jpg, <appid>_hero.png, …): exact names, never another appid that merely
    starts with the same digits."""
    pat = re.compile(rf"^{re.escape(str(appid))}(p|_hero|_logo|_icon)?\.[A-Za-z0-9]+$")
    try:
        return [os.path.join(grid, n) for n in os.listdir(grid) if pat.match(n)]
    except OSError:
        return []


def remove_shortcut(vdf_path, exe):
    if not os.path.exists(vdf_path):
        return False
    root = vdf_decode(open(vdf_path, "rb").read())
    sc = root.get("shortcuts", {})
    keep = [v for v in sc.values() if not (isinstance(v, dict) and v.get("Exe") == exe)]
    if len(keep) == len(sc):
        return False
    root["shortcuts"] = {str(i): v for i, v in enumerate(keep)}
    backup_vdf(vdf_path)
    with open(vdf_path + ".tmp", "wb") as f:
        f.write(vdf_encode(root))
    os.replace(vdf_path + ".tmp", vdf_path)
    return True


STATUS_FILE = os.path.join(HOME, ".local/share/frameport/shortcuts-status.json")


def cmd_shortcuts(args):
    """Add library entries (+ grid artwork) for installed games. Steam must be closed while shortcuts.vdf is
    rewritten, so the work runs in a detached systemd unit (terminals/SSH sessions started from Steam live in
    steam.service's cgroup and would be killed with it). Poll shortcut_status for the result."""
    packages = [check_pkg(p) for p in args.get("packages", [])]
    remove = [r for r in args.get("remove", [])
              if isinstance(r, dict) and str(r.get("exe", "")).startswith('"' + ANCHORS)]
    if not packages and not remove:
        return {"started": False}
    current = cmd_shortcut_status({})
    alive = time.time() - float(current.get("started") or 0) < 60  # a waiting worker rewrites its status every 10 s
    if current.get("state") == "waiting" and alive and set(packages) <= set(current.get("packages") or []) \
            and not remove:
        return {"started": False, "waiting": True}  # the update for these games already waits (Gaming Mode / game)
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump({"state": "running", "packages": packages, "started": time.time()}, f)
    unit = f"frameport-shortcuts-{int(time.time())}"
    payload = json.dumps({"packages": packages, "remove": remove, "restart": args.get("restart", True)})
    run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}", "--setenv=HOME=" + HOME,
         sys.executable, os.path.abspath(__file__), "_shortcuts_worker", payload])
    return {"started": True, "unit": unit}


def shortcut_args(pkg):
    """(exe, title, start dir, icon, tag, tags, openvr) of an installed game's shortcut."""
    anchor = os.path.join(ANCHORS, pkg)
    dep = json.load(open(os.path.join(anchor, "deployment.json")))
    icon = app_icon_for(dep, anchor, steam=True)[0]  # a Linux app's own icon unless the user chose one
    flat = dep.get("vr") is False
    kind = dep.get("kind")
    tag = ("Windows game on Frame" if flat else "PC VR on Frame") if kind == "pcvr" else \
        "Linux app on Frame" if kind == "linux" else "Quest on Frame"
    return f'"{anchor}/launch.sh"', dep["title"], anchor, icon, tag, dep.get("tags") or [], not flat


def library_changes(users, packages):
    """Whether shortcuts.vdf would change for any of these games (a reinstall usually changes nothing)."""
    for user in users:
        vdf = os.path.join(STEAM, "userdata", user, "config/shortcuts.vdf")
        for pkg in packages:
            exe, title, start, icon, tag, tags, openvr = shortcut_args(pkg)
            if upsert_shortcut(vdf, exe, title, start, icon, tag, tags=tags, openvr=openvr, write=False)[1]:
                return True
    return False


def shortcuts_lost(users, packages, wait=25):
    """Packages whose shortcut isn't in any account's shortcuts.vdf once Steam has started again (Steam saving its own
    copy over ours on the way out looked like a successful install with no game in the library, GitHub #27)."""
    for _ in range(wait):  # until Steam runs again (it rewrites shortcuts.vdf while starting, too)
        if run(["pgrep", "-x", "steam"]).returncode == 0:
            break
        time.sleep(1)
    time.sleep(8)
    lost = []
    for pkg in packages:
        exe = shortcut_args(pkg)[0]
        found = False
        for user in users:
            vdf = os.path.join(STEAM, "userdata", user, "config/shortcuts.vdf")
            try:
                root = vdf_decode(open(vdf, "rb").read()) if os.path.exists(vdf) else {}
            except (OSError, AgentError):
                continue
            entries = (root.get("shortcuts") or {}).values()
            found = found or any(isinstance(v, dict) and v.get("Exe") == exe for v in entries)
        if not found:
            lost.append(pkg)
    return lost


def desktop_mode():
    """Desktop Mode is open: it runs inside Steam's session, so a Steam restart would end it (and FramePort itself when
    it runs on the Frame); library changes wait until the user is back in Gaming Mode."""
    return run(["pgrep", "-x", "plasmashell"]).returncode == 0


def game_running():
    """A game is being played on the Frame (any Lepton game container, or a FramePort PC VR game)."""
    names = run(["podman", "ps", "--format", "{{.Names}}"]).stdout.split()
    if any(n.startswith("lepton-steamlaunch-") for n in names):
        return True
    return any(pcvr_pids(d["base"]) for d in cmd_list_installed({})["games"] if d.get("kind") in ("pcvr", "linux"))


def shortcuts_worker(payload):
    """One library update at a time: two at once each read shortcuts.vdf, changed it and wrote it back, so the second
    write brought back a shortcut the first had removed (uninstalling two games quickly left Roblox in Steam)."""
    import fcntl

    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _shortcuts_worker(payload)


def _shortcuts_worker(payload):
    args = json.loads(payload)
    result = {"state": "done", "added": [], "errors": [], "finished": None}
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    try:  # new art (e.g. the user's own icon) also reaches Linux apps' Desktop Mode entries
        refresh_desktop_entries()
    except Exception:  # noqa: BLE001
        pass
    try:
        users = library_users()
        # games Steam only knows through their devkit entry (this Frame's Steam drops FramePort's shortcuts.vdf
        # entries, GitHub #41/#42): rewriting the vdf changes nothing for them, and the Steam restart would drop the
        # live devkit entry too. They get their art only; Play re-adds a lost devkit entry live.
        devkit = [p for p in args["packages"] if devkit_gameid(p)]
        for pkg in devkit:
            appid = devkit_appid(devkit_gameid(pkg))
            if appid:
                copy_grid_art(pkg, appid)
        args["packages"] = [p for p in args["packages"] if p not in devkit]
        result["devkit"] = devkit
        if not args.get("remove") and not library_changes(users, args["packages"]):
            # nothing to change in shortcuts.vdf (e.g. a reinstall): only the artwork, no Steam restart
            for user in users:
                update_library(user, args["packages"], result, shortcuts=False)
            result["unchanged"] = True
            result["finished"] = time.time()
            with open(STATUS_FILE, "w") as f:
                json.dump(result, f)
            return
        deadline = time.time() + 12 * 3600
        while time.time() < deadline:  # restarting Steam would end the game being played, or Desktop Mode
            reason = "game" if game_running() else "desktop" if desktop_mode() else None
            if not reason:
                break
            with open(STATUS_FILE, "w") as f:
                json.dump({"state": "waiting", "reason": reason, "packages": args["packages"],
                           "started": time.time()}, f)
            time.sleep(10)
        try:
            service = stop_steam()
        except AgentError:
            raise AgentError("Steam did not close; library not modified") from None
        for user in steam_users():  # uninstalled games leave every account's library
            remove_from_library(user, args.get("remove", []), result)
        for user in users:
            update_library(user, args["packages"], result)
        if service or args.get("restart", True):
            start_steam(service)
            lost = shortcuts_lost(users, args["packages"])
            if lost:  # Steam wrote its old copy back over ours (it was still saving): once more, then report
                result["retried"] = lost
                service = stop_steam()
                for user in users:
                    update_library(user, lost, {"added": [], "errors": result["errors"]})
                start_steam(service)
                for pkg in shortcuts_lost(users, lost):
                    result["added"] = [a for a in result["added"] if a["package"] != pkg]
                    result["errors"].append(f"{pkg}: Steam removed the new library entry again after restarting")
    except Exception as exc:  # noqa: BLE001
        result["state"] = "failed"
        result["errors"].append(str(exc))
        run(["systemctl", "--user", "start", "steam.service"])
    result["finished"] = time.time()
    with open(STATUS_FILE, "w") as f:
        json.dump(result, f)


def remove_from_library(user, remove, result):
    """Remove uninstalled games' shortcuts + grid art from one Steam account (Steam is closed)."""
    vdf = os.path.join(STEAM, "userdata", user, "config/shortcuts.vdf")
    grid = os.path.join(os.path.dirname(vdf), "grid")
    for r in remove:
        try:
            if remove_shortcut(vdf, r["exe"]):
                result.setdefault("removed", []).append(r["exe"])
            for art in grid_files(grid, r.get("appid") or ""):
                os.remove(art)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"{r.get('exe')}: {exc}")


def update_library(user, packages, result, shortcuts=True):
    """Add/update installed games' shortcuts + grid art in one Steam account (Steam is closed; shortcuts=False: only
    the grid art of shortcuts that are already right, while Steam runs). A game's devkit entry (the Play fallback)
    gets the same art: on Frames where Steam ignores shortcuts.vdf it is the one in the library (GitHub #41)."""
    for pkg in packages:
        gameid = devkit_gameid(pkg)
        appid = devkit_appid(gameid) if gameid else None
        if appid:
            try:
                copy_grid_art(pkg, appid, [user])
            except OSError as exc:
                result.setdefault("errors", []).append(f"{pkg}: devkit art: {exc}")
    vdf = os.path.join(STEAM, "userdata", user, "config/shortcuts.vdf")
    grid = os.path.join(os.path.dirname(vdf), "grid")
    os.makedirs(grid, exist_ok=True)
    for pkg in packages:
        try:
            anchor = os.path.join(ANCHORS, pkg)
            dep = json.load(open(os.path.join(anchor, "deployment.json")))
            exe, title, start, icon, tag, tags, openvr = shortcut_args(pkg)
            got = upsert_shortcut(vdf, exe, title, start, icon, tag, tags=tags, openvr=openvr, write=shortcuts)
            got = got[0] if not shortcuts else got
            for kind, suffix in (("portrait", "p"), ("landscape", ""), ("hero", "_hero"), ("logo", "_logo")):
                img = next(iter(glob.glob(os.path.join(anchor, f"artwork/{kind}.*"))), None)
                if not img:
                    continue
                for old in glob.glob(os.path.join(grid, f"{got}{suffix}.*")):
                    os.remove(old)
                shutil.copy(img, os.path.join(grid, f"{got}{suffix}{os.path.splitext(img)[1]}"))
            if not any(a["package"] == pkg for a in result["added"]):
                result["added"].append({"package": pkg, "appid": got, "expected": dep["appid"]})
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"{pkg}: {exc}")


def cmd_shortcut_status(args):
    try:
        return json.load(open(STATUS_FILE))
    except (OSError, ValueError):
        return {"state": "none"}


# ------------------------------------------------------------------------------------------ drives (GitHub #90)
# Games can live on another drive (a microSD card): their files go to <mount>/FramePort/<pkg>; the anchor (launch.sh,
# deployment.json, artwork) always stays on internal storage, so the Steam shortcut never changes. SteamOS mounts
# removable drives under /run/media/<user>/<label or uuid>.
PROC_MOUNTS = "/proc/mounts"
MEDIA_ROOT = "/run/media"
DRIVE_DIR = "FramePort"
# Lepton's container data and Proton prefixes need Unix permissions, owners and symlinks: these can't hold them
UNUSABLE_FS = ("vfat", "msdos", "exfat", "ntfs", "ntfs3", "fuseblk")
MOVE_STATUS = os.path.join(HOME, ".cache/frameport-move.json")
MOVE_HEADROOM = 512 << 20
# what stays in the anchor when a game's files live there too (base == anchor): never moved
ANCHOR_ITEMS = ("launch.sh", "launch.sh.tmp", "deployment.json", "deployment.json.tmp", "artwork", "plays.log")


def _unescape_mount(field):
    """/proc/mounts writes spaces, tabs, newlines and backslashes in paths as octal escapes (\\040 ...)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), field)


def mounts(path=None):
    """[(device, mount point, fstype, options)] from /proc/mounts."""
    out = []
    try:
        with open(path or PROC_MOUNTS, encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.split()
                if len(p) >= 4:
                    out.append((_unescape_mount(p[0]), _unescape_mount(p[1]), p[2], p[3].split(",")))
    except OSError:
        pass
    return out


def _inside(path, root):
    path, root = os.path.normpath(path), os.path.normpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def mount_of(path, table=None):
    """The mount entry holding `path` (longest mount point prefix), or None."""
    best = None
    for m in mounts() if table is None else table:
        if _inside(path, m[1]) and (best is None or len(m[1]) > len(best[1])):
            best = m
    return best


def _space(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None, None
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


def _drive(path, install_dir, m, internal=False, steam_library=False):
    fstype = m[2] if m else ""
    free, total = _space(path)
    reason = ""
    if not internal:
        if fstype in UNUSABLE_FS:
            reason = (f"{fstype} can't hold game data (no Unix permissions or symlinks); format the drive in SteamOS "
                      f"to install games on it")
        elif m and "ro" in m[3]:
            reason = "mounted read-only"
        elif free is None:
            reason = "can't be read"
    return {"id": "internal" if internal else path, "path": path, "install_dir": install_dir,
            "label": "Internal storage" if internal else (os.path.basename(path.rstrip("/")) or path),
            "fstype": fstype, "device": m[0] if m else "", "internal": internal,
            "removable": not internal and _inside(path, MEDIA_ROOT), "steam_library": steam_library,
            "free_bytes": free, "total_bytes": total, "usable": not reason, "reason": reason}


def list_drives(table=None):
    """Internal storage, every drive mounted under /run/media and the drives of Steam library folders."""
    table = mounts() if table is None else table
    home_m = mount_of(HOME, table)
    libs = [os.path.dirname(lib) for lib in steam_libraries()]
    drives = [_drive(HOME, ANCHORS, home_m, internal=True, steam_library=any(_inside(lib, HOME) for lib in libs))]
    seen = {home_m[1] if home_m else HOME}
    for m in sorted(table, key=lambda m: m[1]):
        if _inside(m[1], MEDIA_ROOT) and m[1] != os.path.normpath(MEDIA_ROOT) and m[1] not in seen \
                and os.path.isdir(m[1]):
            seen.add(m[1])
            drives.append(_drive(m[1], os.path.join(m[1], DRIVE_DIR), m,
                                 steam_library=any(_inside(lib, m[1]) for lib in libs)))
    for lib in libs:  # a Steam library on a drive mounted elsewhere
        m = mount_of(lib, table)
        if m and m[1] not in seen and m[1] != "/" and not _inside(HOME, m[1]):
            seen.add(m[1])
            drives.append(_drive(m[1], os.path.join(m[1], DRIVE_DIR), m, steam_library=True))
    return drives


def cmd_drives(args):
    """Where games can be installed: internal storage and other drives (microSD), with free space and whether they can
    hold games (`usable`, else `reason`); `games` = how many installed games each holds."""
    drives = list_drives()
    games = cmd_list_installed({})["games"]
    for d in drives:
        d["games"] = sum(1 for g in games if drive_path(g["base"], drives) == d["path"])
    return {"drives": drives}


def drive_path(base, drives):
    """The path of the listed drive holding `base` (internal storage: HOME), or None."""
    best = None
    for d in drives:
        if not d["internal"] and _inside(base, d["path"]) and (best is None or len(d["path"]) > len(best)):
            best = d["path"]
    return best or (HOME if _inside(base, HOME) else None)


def drive_missing(base, table=None):
    """A game's files are on a drive that isn't there now (the microSD card was taken out)."""
    if _inside(base, HOME):
        return False
    m = mount_of(base, table)
    return m is None or m[1] == "/" or not os.path.isdir(base)


def resolve_dest(dest, table=None):
    """The install dir for new games: internal storage (None, "", "internal", ~/Applications/quest-frame) or a drive's
    FramePort folder (its mount point is accepted too). A drive that isn't mounted is an error, never a silent
    fallback to internal storage."""
    if not dest or dest == "internal":
        return ANCHORS
    dest = os.path.normpath(os.path.expanduser(dest))
    if dest == ANCHORS:
        return ANCHORS
    if not os.path.isabs(dest):
        raise AgentError(f"bad install location {dest!r}")
    for d in list_drives(table):
        if d["internal"] or not _inside(dest, d["path"]):
            continue
        if not d["usable"]:
            raise AgentError(f"{d['label']}: {d['reason']}")
        if dest == d["path"]:
            dest = d["install_dir"]
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as exc:
            raise AgentError(f"can't create {dest} on {d['label']}: {exc.strerror or exc}") from exc
        return dest
    raise AgentError(f"{dest}: that drive isn't inserted (or not mounted)")


def check_base_present(base, title):
    if drive_missing(base):
        raise AgentError(f"{title}'s files are on a drive that isn't inserted ({base})")


def new_base(args, pkg, dep):
    """(base, free bytes there) for prepare*: an installed game keeps its base; a new one goes to args["dest"]."""
    if dep:
        check_base_present(dep["base"], dep.get("title") or pkg)
        base = dep["base"]
        probe = base
        while not os.path.exists(probe) and probe != os.path.dirname(probe):
            probe = os.path.dirname(probe)
    else:
        dest = resolve_dest(args.get("dest"))
        base = os.path.join(dest, pkg)
        probe = dest if os.path.exists(dest) else HOME
    st = os.statvfs(probe)
    return base, st.f_bavail * st.f_frsize


# ------------------------------------------------------------------------------------------ install
def deployment(pkg):
    path = os.path.join(ANCHORS, pkg, "deployment.json")
    try:
        dep = json.load(open(path))
    except (OSError, ValueError):
        return None
    if not isinstance(dep, dict):
        return None
    dep.setdefault("base", os.path.dirname(path))
    dep.setdefault("title", pkg)
    return dep


def cmd_list_installed(args):
    games = []
    table = mounts()
    for dep_path in sorted(glob.glob(os.path.join(ANCHORS, "*/deployment.json"))):
        try:
            dep = json.load(open(dep_path))
        except (OSError, ValueError):
            continue
        # incomplete records (an interrupted install, another tool's files in this folder) are skipped or completed
        # instead of failing every connection with a KeyError (GitHub #40)
        if not isinstance(dep, dict) or not dep.get("package") or not dep.get("appid"):
            continue
        dep.setdefault("base", os.path.dirname(dep_path))
        dep.setdefault("title", dep["package"])
        dep.setdefault("kind", "quest")
        dep["anchor"] = os.path.dirname(dep_path)  # its artwork/ feeds the Steam grid (shortcuts)
        # GitHub #90: games on another drive (microSD); drive_missing = that drive isn't inserted now
        dep["drive_missing"] = drive_missing(dep["base"], table)
        if _inside(dep["base"], HOME):
            dep["drive"] = {"internal": True, "path": HOME, "label": "Internal storage"}
        else:
            m = mount_of(dep["base"], table)
            mp = m[1] if m and m[1] != "/" and not dep["drive_missing"] else \
                os.path.dirname(os.path.dirname(dep["base"].rstrip("/")))  # <mount>/FramePort/<pkg>
            dep["drive"] = {"internal": False, "path": mp, "label": os.path.basename(mp.rstrip("/")) or mp}
        if dep["kind"] == "linux":
            exe = os.path.join(dep["base"], "app", dep.get("exe", ""))
            dep["apk_present"] = os.path.isfile(exe)
            dep["apk_size"] = sum((dep.get("files") or {}).get("app", {}).values())
            dep.pop("files", None)
        elif dep["kind"] == "pcvr":
            exe = os.path.join(dep["base"], "game", dep.get("exe", ""))
            dep["apk_present"] = os.path.isfile(exe)
            dep["apk_size"] = sum((dep.get("files") or {}).get("game", {}).values())
            dep.pop("files", None)  # large; not needed by the PC
        else:
            apk = os.path.join(dep["base"], "lepton-app/game.apk")
            dep["apk_present"] = os.path.exists(apk)
            dep["apk_size"] = os.path.getsize(apk) if dep["apk_present"] else 0
        dep["last_play"] = last_play(dep["anchor"])  # agent v70: the PC triages a finished play session's log
        games.append(dep)
    return {"games": games}


def steam_gameid(appid):
    """steam://rungameid/ id of a non-Steam shortcut: the 32-bit shortcut appid in the high word, type 0x02000000."""
    return (int(appid) << 32) | 0x02000000


NOT_IN_LIBRARY = "not in the Frame's Steam library"

# Fallback library entry (GitHub #21/#30): on some Frames Steam never picks up the shortcuts FramePort writes into
# shortcuts.vdf (Play: "Game configuration unavailable", Steam's log: RequestingLicense → AppError_9), while games
# registered through Steam's devkit interface work there (Valve's Devkit Management Tool, MIT: the same pipe command,
# ~/devkit-game/<gameid>/ + <gameid>-argv.json/-settings.json). Steam adds those live, without a restart, and runs
# argv[0] relative to the game's folder: a link to the game's launch.sh. Steam names them "Devkit Game: <gameid>"
# (no spaces allowed in the id), so they're only used where the normal entry fails.
DEVKIT_GAMES = os.path.join(HOME, "devkit-game")


def devkit_request(command, timeout=15):
    """Send a devkit-1 command to the running Steam (its command pipe + session token) and wait for its answer file.
    Returns the answer text; AgentError on an error answer or no answer."""
    import tempfile
    from urllib.parse import quote_plus

    try:
        token = open(os.path.join(HOME, ".steam/steam.token")).read().strip()
    except OSError:
        raise AgentError("Steam isn't running on the Frame (no session token)") from None
    with tempfile.TemporaryDirectory(prefix="frameport-devkit") as tmp:
        resp = os.path.join(tmp, "response")
        line = f"devkit-1 steam://devkit-1/{token}/{command.format(response=quote_plus(resp))}\n"
        with open(os.path.realpath(os.path.join(HOME, ".steam/steam.pipe")), "wb", 0) as pipe:
            pipe.write(line.encode())
        for _ in range(timeout * 4):
            time.sleep(0.25)
            if os.path.exists(resp + ".error"):
                raise AgentError(f"Steam: {open(resp + '.error', errors='replace').read().strip()}")
            if os.path.exists(resp) and not os.path.exists(resp + ".lock"):
                return open(resp, errors="replace").read()
    raise AgentError("Steam didn't answer the devkit request")


def devkit_gameid(pkg):
    """The devkit game id whose launcher links to this game's launch.sh, if it has one."""
    target = os.path.join(ANCHORS, pkg, "launch.sh")
    try:
        names = sorted(os.listdir(DEVKIT_GAMES))
    except OSError:
        return None
    for name in names:
        link = os.path.join(DEVKIT_GAMES, name, "launch.sh")
        if os.path.islink(link) and os.readlink(link) == target:
            return name
    return None


def devkit_appid(gameid):
    """The appid Steam gave a devkit game (it picks it itself), from any account's shortcuts.vdf."""
    for user in steam_users():
        vdf = os.path.join(STEAM, "userdata", user, "config/shortcuts.vdf")
        try:
            root = vdf_decode(open(vdf, "rb").read()) if os.path.exists(vdf) else {}
        except (OSError, AgentError):
            continue
        for v in (root.get("shortcuts") or {}).values():
            if isinstance(v, dict) and v.get("DevkitGameID") == gameid and v.get("appid"):
                return v["appid"] & 0xFFFFFFFF
    return devkit_appid_from_log(gameid)


def devkit_appid_from_log(gameid):
    """The appid Steam picked for a devkit game, from its console log ('sanitize shortcut app id "<dir>/<id>/launch.sh":
    replacing 0 with N'). Steam adds the entry live but may never save it (GitHub #41/#42), so the log of the running
    Steam session is the fallback (console_log.previous.txt is the session before, whose live entries are gone). The
    newest line wins."""
    exe = os.path.join(DEVKIT_GAMES, gameid, "launch.sh")
    pat = re.compile(r'sanitize shortcut app id "' + re.escape(exe) + r'": replacing \d+ with (\d+)')
    found = None
    for name in ("console_log.txt",):  # this Steam session only: entries added live are gone after a restart
        try:
            with open(os.path.join(STEAM, "logs", name), errors="replace") as f:
                for line in f:
                    if "sanitize shortcut app id" in line:
                        m = pat.search(line)
                        if m:
                            found = int(m.group(1)) & 0xFFFFFFFF
        except OSError:
            continue
    return found


def devkit_register(pkg):
    """Add the game to Steam through the devkit interface (Steam must run); copies its art. Returns the appid."""
    dep = deployment(pkg) or {}
    gameid = devkit_gameid(pkg)
    if not gameid:
        # Steam only accepts ids like identifiers: a letter first, then letters, digits and "_" (no spaces or "-")
        slug = re.sub(r"[^A-Za-z0-9]+", "_", dep.get("title") or "").strip("_")[:60] or pkg.replace(".", "_")
        slug = re.sub(r"[^A-Za-z0-9_]", "_", slug)
        if not slug[:1].isalpha():
            slug = "Game_" + slug
        gameid, n = slug, 2
        while os.path.exists(os.path.join(DEVKIT_GAMES, gameid)):
            gameid, n = f"{slug}_{n}", n + 1
    folder = os.path.join(DEVKIT_GAMES, gameid)
    os.makedirs(folder, exist_ok=True)
    link = os.path.join(folder, "launch.sh")
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(os.path.join(ANCHORS, pkg, "launch.sh"), link)
    with open(os.path.join(DEVKIT_GAMES, f"{gameid}-argv.json"), "w") as f:
        json.dump(["launch.sh"], f)
    with open(os.path.join(DEVKIT_GAMES, f"{gameid}-settings.json"), "w") as f:
        json.dump({"steam_play": "0", "compat_tool": ""}, f)
    from urllib.parse import quote_plus

    try:
        devkit_request("create-shortcut?response={response}&gameid=" + gameid + "&directory=" +
                       quote_plus(DEVKIT_GAMES))
    except AgentError:
        _devkit_remove_files(gameid)
        raise
    appid = None
    for _ in range(20):  # Steam writes the new entry to shortcuts.vdf right after answering
        appid = devkit_appid(gameid)
        if appid:
            break
        time.sleep(0.5)
    if not appid:
        raise AgentError("Steam added the devkit entry but didn't save it")
    copy_grid_art(pkg, appid)
    return appid


def copy_grid_art(pkg, appid, users=None):
    """The game's Steam art (portrait, landscape, hero, logo) as grid/<appid>* in every (or the given) account."""
    anchor = os.path.join(ANCHORS, pkg)
    for user in users if users is not None else steam_users():
        grid = os.path.join(STEAM, "userdata", user, "config/grid")
        os.makedirs(grid, exist_ok=True)
        for kind, suffix in (("portrait", "p"), ("landscape", ""), ("hero", "_hero"), ("logo", "_logo")):
            img = next(iter(glob.glob(os.path.join(anchor, f"artwork/{kind}.*"))), None)
            if img:
                for old in grid_files(grid, appid):
                    if re.match(rf"^{appid}{re.escape(suffix)}\.", os.path.basename(old)):
                        os.remove(old)
                shutil.copy(img, os.path.join(grid, f"{appid}{suffix}{os.path.splitext(img)[1]}"))


def devkit_unregister(pkg, steam_running=True):
    """Remove the game's devkit entry: Steam's entry (live when Steam runs; else from shortcuts.vdf), its art and
    the ~/devkit-game files. Returns whether there was one."""
    gameid = devkit_gameid(pkg)
    if not gameid:
        return False
    appid = devkit_appid(gameid)
    exe = f'"{os.path.join(DEVKIT_GAMES, gameid, "launch.sh")}"'
    if steam_running:
        try:
            devkit_request("delete-shortcut?response={response}&gameid=" + gameid)
        except AgentError:
            pass
    else:
        for user in steam_users():
            remove_shortcut(os.path.join(STEAM, "userdata", user, "config/shortcuts.vdf"), exe)
    if appid:
        for user in steam_users():
            for art in grid_files(os.path.join(STEAM, "userdata", user, "config/grid"), appid):
                os.remove(art)
    _devkit_remove_files(gameid)
    return True


def _devkit_remove_files(gameid):
    remove_tree(os.path.join(DEVKIT_GAMES, gameid))
    for suffix in ("argv", "settings", "env"):
        p = os.path.join(DEVKIT_GAMES, f"{gameid}-{suffix}.json")
        if os.path.exists(p):
            os.remove(p)


def cmd_launch(args):
    """Start an installed game the way the headset's library does: ask the running Steam to launch its shortcut, so
    it gets Steam's VR session, overlay and controller setup (unlike launch_test's direct, headless start)."""
    pkg = check_pkg(args["package"])
    dep = deployment(pkg)
    if not dep or not dep.get("appid"):
        raise AgentError(f"{pkg} is not installed")
    if dep.get("base"):
        check_base_present(dep["base"], dep.get("title") or pkg)
    if run(["pgrep", "-x", "steam"]).returncode != 0:
        raise AgentError("Steam isn't running on the Frame")
    devkit = devkit_gameid(pkg)
    appid = devkit_appid(devkit) if devkit else None  # this Frame needed the fallback entry before: use it
    if devkit and not appid:
        # Steam forgot the devkit entry (it never saves it on some Frames, and a restart drops it, GitHub #41): add it
        # again live instead of reporting NOT_IN_LIBRARY, whose repair restarts Steam
        try:
            appid = devkit_register(pkg)
        except AgentError:
            appid = None
    via = "devkit" if appid else "shortcut"
    appid = appid or shortcut_appid_for(f'"{os.path.join(ANCHORS, pkg)}/launch.sh"')
    if appid is None:  # Steam would only say "Game configuration unavailable"
        raise AgentError(f"{NOT_IN_LIBRARY}: {dep.get('title') or pkg}")
    steam = steam_launch(appid, args.get("wait", 10))
    out = {"package": pkg, "gameid": steam_gameid(appid), "title": dep.get("title"), "via": via, "steam": steam}
    if steam["result"] == "error" and steam.get("code") == 9 and args.get("fallback", True):
        # Steam doesn't know the shortcut at all (it tried to license a store app with that id): devkit entry. A devkit
        # entry Steam never saved is gone after a Steam restart (every install restarts it, GitHub #41): add it again
        try:
            appid = devkit_register(pkg)
        except AgentError as exc:
            out["fallback_error"] = str(exc)
            return out
        out.update(via="devkit", gameid=steam_gameid(appid), first_try=steam,
                   steam=steam_launch(appid, args.get("wait", 10)))
    return out


def steam_launch(appid, wait=10):
    """Ask the running Steam to start shortcut `appid`; what its log says about it (steam_launch_result)."""
    log = os.path.join(STEAM, "logs/console_log.txt")
    start = os.path.getsize(log) if os.path.exists(log) else 0
    # systemd-run: the launch request must outlive this SSH session
    run(["systemd-run", "--user", "--collect", "--quiet", f"--unit=frameport-launch-{time.time_ns()}",
         "steam", "-ifrunning", f"steam://rungameid/{steam_gameid(appid)}"])
    return steam_launch_result(log, start, appid, wait=wait)


STEAM_LAUNCH_ERROR = re.compile(r"launch error (\d+) (\d+)(.*)")
STEAM_APP_ERROR = re.compile(r"LaunchApp failed with AppError_(\d+)")


def steam_launch_result(log, start, appid, wait=10):
    """What Steam's console log says about the launch of shortcut `appid` (lines written after byte `start`): "started"
    (Steam created the process), "error" (e.g. "launch error 9 <appid>" = Steam's "Game configuration unavailable",
    GitHub #21/#30), "silent" (Steam logged nothing about this app: it didn't know the shortcut) or "unknown"."""
    tag, lines = f"[AppID {appid},", []
    deadline = time.time() + wait
    while True:
        try:
            with open(log, "rb") as f:
                f.seek(start)
                new = f.read(1 << 20).decode("utf-8", "replace").splitlines()
        except OSError:
            return {"result": "unknown", "lines": []}
        lines = [ln for ln in new if tag in ln or "launch error" in ln or "rungameid" in ln][-40:]
        mine = [ln for ln in lines if tag in ln]
        err = next((m for m in map(STEAM_LAUNCH_ERROR.search, lines) if m and m.group(2) == str(appid)), None)
        if err:
            return {"result": "error", "code": int(err.group(1)), "detail": err.group(3).strip(), "lines": lines}
        # e.g. "GameAction [AppID 3554078061, ActionID 3] : LaunchApp failed with AppError_9" (GitHub #21)
        failed = next((m for m in map(STEAM_APP_ERROR.search, mine) if m), None)
        if failed:
            return {"result": "error", "code": int(failed.group(1)), "detail": failed.group(0), "lines": lines}
        if any(tag in ln and ("CreatingProcess" in ln or "WaitingGameWindow" in ln or "Completed" in ln)
               for ln in lines):
            return {"result": "started", "lines": lines}
        if time.time() >= deadline:
            return {"result": "silent" if not any(tag in ln for ln in lines) else "unknown", "lines": lines}
        time.sleep(1)


LEPTON_LINK = re.compile(r'ln -s "\$\{HOME\}/([^"/]+)" "\$\{TARGET_PATH\}/([^"/]+)"')
SHARED_DEFAULT = (("Documents", "Documents"), ("Downloads", "Download"), ("Videos", "Movies"))


def lepton_shared_folders():
    """(home folder, Android folder) pairs Lepton links into every app's storage (read from Lepton's mounting.sh;
    it only links folders that exist when an app starts)."""
    lepton, _app = lepton_path()
    text = ""
    if lepton:
        try:
            text = open(os.path.join(os.path.dirname(lepton), "liblepton", "mounting.sh"), errors="replace").read()
        except OSError:
            pass
    pairs = LEPTON_LINK.findall(text)
    return pairs or list(SHARED_DEFAULT)


# Folders Android itself creates in every app's /sdcard; anything else at the top level was made by the app.
ANDROID_STORAGE_DIRS = {"alarms", "android", "audiobooks", "dcim", "documents", "download", "movies", "music",
                        "notifications", "pictures", "podcasts", "recordings", "ringtones", "screenshots"}


def app_media_dirs(ext):
    """The app's own top-level folders in its /sdcard (e.g. 4XVR's 4XPlayer, which its "Internal Storage" list shows
    instead of /sdcard/Movies): real folders only (not Lepton's links to the shared folders), not hidden."""
    try:
        names = sorted(os.listdir(ext))
    except OSError:
        return []
    return [n for n in names if not n.startswith(".") and n.lower() not in ANDROID_STORAGE_DIRS
            and os.path.isdir(os.path.join(ext, n)) and not os.path.islink(os.path.join(ext, n))]


def lepton_external(pkg):
    dep = deployment(pkg)
    if not dep or dep.get("kind") == "pcvr":
        raise AgentError(f"{pkg} is not an installed Quest (Lepton) game")
    return os.path.join(dep["base"], "lepton-data", "external")


def cmd_storage_targets(args):
    """Where files for Lepton apps go: shared folders (seen by every app, e.g. ~/Videos = /sdcard/Movies) and, with a
    package, that app's own storage (/sdcard) and its files folder. Creates missing shared folders (Lepton links only
    existing ones). Android's media index doesn't work in Lepton, so apps must browse folders to find files."""
    out = []
    for home_name, android in lepton_shared_folders():
        path = os.path.join(HOME, home_name)
        os.makedirs(path, exist_ok=True)
        out.append({"id": home_name.lower(), "path": path, "android": "/sdcard/" + android, "shared": True})
    pkg = args.get("package")
    if pkg:
        pkg = check_pkg(pkg)
        ext = lepton_external(pkg)
        files = os.path.join(ext, "Android", "data", pkg, "files")
        os.makedirs(files, exist_ok=True)
        out.append({"id": "app", "path": ext, "android": "/sdcard", "shared": False})
        out.append({"id": "app-files", "path": files, "android": f"/sdcard/Android/data/{pkg}/files", "shared": False})
        for i, name in enumerate(app_media_dirs(ext)):  # the app's own folders (e.g. a video player's library)
            out.append({"id": "app-media" if i == 0 else f"app-media:{name}", "path": os.path.join(ext, name),
                        "android": f"/sdcard/{name}", "shared": False, "folder": name})
    return {"targets": out}


def cmd_link_media(args):
    """Make files from the shared folders (e.g. ~/Videos) appear in an app's own folder too, as hard links (no copy,
    no extra space). For players that list their own folder rather than /sdcard/Movies (4XVR: 4XPlayer). args:
    package, files (paths under ~/Videos, ~/Downloads, ~/Documents), folder (optional; default: the app's first own
    folder, see app_media_dirs). Returns {folder, android, linked, existing, missing}; folder None when the app has no
    own folder (it then finds the files in the shared folders)."""
    pkg = check_pkg(args["package"])
    ext = lepton_external(pkg)
    folder = args.get("folder") or next(iter(app_media_dirs(ext)), None)
    if not folder:
        return {"folder": None, "android": None, "linked": [], "existing": [], "missing": []}
    if "/" in folder or folder in (".", ".."):
        raise AgentError(f"bad folder {folder!r}")
    dest = os.path.join(ext, folder)
    os.makedirs(dest, exist_ok=True)
    shared = [os.path.realpath(os.path.join(HOME, h)) for h, _a in lepton_shared_folders()]
    linked, existing, missing = [], [], []
    for path in args.get("files") or []:
        real = os.path.realpath(path)
        if not any(real == s or real.startswith(s + os.sep) for s in shared):
            raise AgentError(f"{path} is not in a shared folder ({', '.join(shared)})")
        if not os.path.isfile(real):
            missing.append(path)
            continue
        target = os.path.join(dest, os.path.basename(real))
        if os.path.exists(target):
            if os.path.samefile(target, real):
                existing.append(os.path.basename(real))
                continue
            os.remove(target)  # an older file of the same name: show the one just sent
        try:
            os.link(real, target)
        except OSError:
            shutil.copy2(real, target)  # different file system (not the case with Lepton's layout)
        linked.append(os.path.basename(real))
    return {"folder": folder, "android": f"/sdcard/{folder}", "linked": linked, "existing": existing,
            "missing": missing}


def cmd_list_files(args):
    """Every file of an installed game on the Frame, for the PC's file browser: {roots: [{name, path, files:
    [[rel, size], ...]}], missing: [[rel, expected size, actual size or None], ...], truncated}. Roots are the install
    folder (game files, Proton prefix / Lepton data) and, when separate, the launcher folder (launch.sh, artwork).
    `missing` compares against the file list recorded at install time (PC VR games)."""
    pkg = check_pkg(args["package"])
    dep = deployment(pkg)
    if not dep:
        raise AgentError(f"{pkg} is not installed")
    limit = int(args.get("limit", 200000))
    anchor = os.path.join(ANCHORS, pkg)
    roots, count, truncated = [], 0, False
    for name, root in (("Install folder", dep["base"]), ("Launcher", anchor)):
        if not os.path.isdir(root) or any(os.path.realpath(root) == os.path.realpath(r["path"]) for r in roots):
            continue
        files = []
        for r, dirs, names in os.walk(root):
            dirs.sort()
            for n in sorted(names):
                if count >= limit:
                    truncated = True
                    break
                p = os.path.join(r, n)
                try:
                    files.append([os.path.relpath(p, root), os.lstat(p).st_size])
                except OSError:
                    continue
                count += 1
        roots.append({"name": name, "path": root, "files": files})
    missing = []
    for t, manifest in (dep.get("files") or {}).items():
        for rel, size in manifest.items():
            p = os.path.join(dep["base"], t, rel)
            actual = os.path.getsize(p) if os.path.isfile(p) else None
            if actual is None and rel.lower().endswith("crashreportclient.exe") and os.path.isfile(p + ".disabled"):
                continue  # renamed by the no-crash-reporter patch
            if actual != size:
                missing.append([f"{t}/{rel}", size, actual])
    return {"roots": roots, "missing": missing[:1000], "truncated": truncated, "kind": dep.get("kind", "quest")}


# ------------------------------------------------------------------------------------------ screenshots
STEAMVR_APPID = "250820"  # the Frame files every headset screenshot under SteamVR, whatever game was shown
PLAYS_LOG = "plays.log"  # <anchor>/plays.log: "start <unix>" / "end <unix>" lines written by launch.sh
OPEN_SESSION = 12 * 3600  # a session without an "end" (Proton launchers exec the game; power loss) lasts at most this
SESSION_GRACE = 5  # s after "end": the screenshot's time can trail the key press slightly
SHOT_NAME = re.compile(r"^(\d{14})_\d+\.(jpe?g|png)$", re.I)
KV_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*|([^\s"{}]+)')


def kv_parse(text):
    """Steam's text KeyValues (screenshots.vdf, ...) as nested dicts. Tolerant: an unbalanced or cut-off file gives
    what was read so far."""
    root, stack, key = {}, [], None
    cur = root
    for m in KV_TOKEN.finditer(text):
        quoted, brace, bare = m.groups()
        if brace == "{":
            new = {}
            if key is not None:
                cur[key] = new
            stack.append(cur)
            cur, key = new, None
        elif brace == "}":
            if not stack:
                break
            cur, key = stack.pop(), None
        elif quoted is not None or bare is not None:
            tok = quoted.replace('\\"', '"').replace("\\\\", "\\") if quoted is not None else bare
            if key is None:
                key = tok
            else:
                cur[key], key = tok, None
    return root


def play_sessions():
    """[(start, end, package)] from every FramePort game's plays.log, by start time. A start without an end (Proton
    launchers exec the game, so they log no end; a crash or power loss) lasts until the next game's start (one game
    runs at a time), at most OPEN_SESSION."""
    events = []
    for path in glob.glob(os.path.join(ANCHORS, "*", PLAYS_LOG)):
        pkg = os.path.basename(os.path.dirname(path))
        try:
            lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ("start", "end") and parts[1].isdigit():
                events.append((int(parts[1]), parts[0], pkg))
    sessions, open_ = [], {}  # open_: pkg -> start
    for t, kind, pkg in sorted(events):
        if kind == "start":
            if pkg in open_:  # an earlier start never ended: it ended by this one
                sessions.append([open_[pkg], t, pkg])
            open_[pkg] = t
        elif pkg in open_:
            sessions.append([open_.pop(pkg), t, pkg])
    sessions += [[s, None, pkg] for pkg, s in open_.items()]
    sessions.sort(key=lambda s: s[0])
    for i, s in enumerate(sessions):
        if s[1] is None:
            later = [x[0] for x in sessions[i + 1:] if x[0] > s[0]]
            s[1] = min([s[0] + OPEN_SESSION] + later)
    return [tuple(s) for s in sessions]


TEST_MARK_WINDOW = 60  # s: a "test <unix>" line in plays.log marks a session starting this soon after as a launch test


def last_play(anchor):
    """The newest play session in <anchor>/plays.log: {start, end (None while it runs, and for Proton launchers, which
    exec the game), test (started by a launch test, not by the player)}, or None."""
    text = _tail(os.path.join(anchor, PLAYS_LOG), 8192)
    if not text:
        return None
    start = end = None
    tests = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        t = int(parts[1])
        if parts[0] == "start":
            start, end = t, None
        elif parts[0] == "end" and start is not None and t >= start:
            end = t
        elif parts[0] == "test":
            tests.append(t)
    if start is None:
        return None
    return {"start": start, "end": end, "test": any(0 <= start - t <= TEST_MARK_WINDOW for t in tests)}


def mark_launch_test(anchor):
    """A launch test runs the game's launcher, which logs a play session: mark it so the PC doesn't triage it as one."""
    try:
        with open(os.path.join(anchor, PLAYS_LOG), "a") as f:
            f.write(f"test {int(time.time())}\n")
    except OSError:
        pass


def session_game(t, sessions):
    """Package whose play session contains time t (the latest start wins), else None."""
    hit = None
    for start, end, pkg in sessions:
        if start <= t <= end + SESSION_GRACE:
            hit = pkg
    return hit


def shot_time_from_name(name):
    m = SHOT_NAME.match(name)
    if not m:
        return None
    try:
        return int(time.mktime(time.strptime(m[1], "%Y%m%d%H%M%S")))  # Steam names the files in local time
    except (ValueError, OverflowError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def user_screenshots(user):
    """Every screenshot file of one Steam account: [{path, thumb, time, width, height, appid}]. screenshots.vdf gives
    times and sizes; files it doesn't list (Steam writes it at exit) come from the folders, timed by their name."""
    remote = os.path.join(STEAM, "userdata", user, "760", "remote")
    shots, seen = [], set()

    def under(rel):
        p = os.path.normpath(os.path.join(remote, rel)) if isinstance(rel, str) else None
        return p if p and p.startswith(remote + os.sep) and os.path.isfile(p) else None
    try:
        text = open(os.path.join(STEAM, "userdata", user, "760", "screenshots.vdf"), encoding="utf-8",
                    errors="replace").read()
    except OSError:
        text = ""
    entries = (kv_parse(text).get("screenshots") or {})
    for game in entries.values() if isinstance(entries, dict) else ():
        for e in game.values() if isinstance(game, dict) else ():
            path = under(e.get("filename")) if isinstance(e, dict) else None
            if not path or path in seen:
                continue  # deleted (Steam lists it until it rewrites the file)
            seen.add(path)
            shots.append({"path": path, "thumb": under(e.get("thumbnail")),
                          "time": _int(e.get("creation")) or shot_time_from_name(os.path.basename(path)),
                          "width": _int(e.get("width")), "height": _int(e.get("height")),
                          "appid": os.path.relpath(path, remote).split(os.sep)[0]})
    for path in sorted(glob.glob(os.path.join(remote, "*", "screenshots", "*"))):
        name = os.path.basename(path)
        if path in seen or not SHOT_NAME.match(name) or not os.path.isfile(path):
            continue
        thumb = os.path.join(os.path.dirname(path), "thumbnails", name)
        shots.append({"path": path, "thumb": thumb if os.path.isfile(thumb) else None,
                      "time": shot_time_from_name(name) or int(os.path.getmtime(path)), "width": None,
                      "height": None, "appid": os.path.relpath(path, remote).split(os.sep)[0]})
    return shots


def shortcut_names(user):
    """{shortcut appid (str): name} of an account's non-Steam shortcuts."""
    try:
        data = vdf_decode(open(os.path.join(STEAM, "userdata", user, "config", "shortcuts.vdf"), "rb").read())
    except (OSError, ValueError, IndexError, struct.error, AgentError):
        return {}
    out = {}
    for e in (data.get("shortcuts") or {}).values():
        if isinstance(e, dict) and isinstance(e.get("appid"), int):
            out[str(e["appid"] & 0xFFFFFFFF)] = e.get("AppName") or e.get("appname") or ""
    return out


def cmd_list_screenshots(args):
    """Steam screenshots on the Frame (every account), newest first: {shots: [{path, thumb, time, width, height, size,
    account, appid, package, title}], total, games: [{package, title, count}]}. The Frame files every headset
    screenshot under SteamVR (250820), so those are matched to the FramePort game whose play session (plays.log)
    contains their time; the rest stay "SteamVR" (package null). args: offset, limit, package (a package, or "" for
    the shots of no FramePort game); `total` and `games` count before offset/limit."""
    titles, by_appid = {}, {}
    for dep_path in glob.glob(os.path.join(ANCHORS, "*/deployment.json")):
        try:
            dep = json.load(open(dep_path))
        except (OSError, ValueError):
            continue
        pkg = os.path.basename(os.path.dirname(dep_path))
        titles[pkg] = dep.get("title") or pkg
        if dep.get("appid") is not None:
            by_appid[str(int(dep["appid"]) & 0xFFFFFFFF)] = pkg
    sessions = play_sessions()
    shots, names = [], {}
    for user in steam_users():
        names.update(shortcut_names(user))
        for s in user_screenshots(user):
            pkg = by_appid.get(s["appid"])
            if pkg is None and s["appid"] == STEAMVR_APPID and s["time"]:
                pkg = session_game(s["time"], sessions)
            if pkg:
                title = titles.get(pkg, pkg)
            elif s["appid"] == STEAMVR_APPID:
                title = "SteamVR"
            else:
                app = find_app_id(s["appid"]) if s["appid"] not in names else None
                title = names.get(s["appid"]) or (app or {}).get("name") or f"App {s['appid']}"
            try:
                size = os.path.getsize(s["path"])
            except OSError:
                continue
            shots.append({**s, "size": size, "account": user, "package": pkg, "title": title})
    shots.sort(key=lambda s: (-(s["time"] or 0), s["path"]))
    games = {}
    for s in shots:
        g = games.setdefault(s["package"] or s["title"], {"package": s["package"], "title": s["title"], "count": 0})
        g["count"] += 1
    want = args.get("package")
    if want is not None:
        shots = [s for s in shots if (s["package"] or "") == want]
    offset = max(0, int(args.get("offset") or 0))
    limit = int(args.get("limit") or 0)
    return {"shots": shots[offset:offset + limit] if limit > 0 else shots[offset:], "total": len(shots),
            "games": sorted(games.values(), key=lambda g: g["title"].lower())}


def screenshot_file(path):
    """`path` if it is a screenshot image in a Steam account's screenshot folder (userdata/<id>/760/remote/<appid>/
    screenshots/<name>), else AgentError. Links and '..' are refused: nothing outside those folders can be deleted."""
    if not isinstance(path, str) or "\0" in path or not path.startswith("/"):
        raise AgentError(f"not a screenshot: {path!r}")
    norm = os.path.normpath(path)
    parts = os.path.relpath(norm, os.path.join(STEAM, "userdata")).split(os.sep)
    if (norm != path or len(parts) != 6 or not parts[0].isdigit() or parts[1:3] != ["760", "remote"]
            or not parts[3].isdigit() or parts[4] != "screenshots" or not SHOT_NAME.match(parts[5])):
        raise AgentError(f"not a screenshot: {path}")
    if os.path.islink(norm) or not os.path.isfile(norm) or not inside_userdata(os.path.dirname(norm)):
        raise AgentError(f"not a screenshot file: {path}")
    return norm


def inside_userdata(folder):
    """The folder, links resolved, is inside Steam's userdata (a linked screenshots folder could point anywhere)."""
    return os.path.realpath(folder).startswith(os.path.realpath(os.path.join(STEAM, "userdata")) + os.sep)


def cmd_delete_screenshots(args):
    """Delete screenshots (+ their thumbnails); only image files in Steam's screenshot folders (screenshot_file; every
    path is checked before anything is deleted). screenshots.vdf is left alone: Steam keeps it in memory and rewrites
    it at exit, so an edit would be lost; Steam drops entries whose file is gone, list_screenshots skips them. Steam's
    own screenshot list may show the deleted ones until Steam restarts."""
    paths = [screenshot_file(p) for p in args.get("paths") or []]
    deleted = []
    for p in paths:
        os.unlink(p)
        deleted.append(p)
        thumbs = os.path.join(os.path.dirname(p), "thumbnails")
        if inside_userdata(thumbs):
            try:
                os.unlink(os.path.join(thumbs, os.path.basename(p)))
            except FileNotFoundError:
                pass
    return {"deleted": deleted}


def cmd_prepare(args):
    """Where to upload, and what the Frame already has (so unchanged data is not re-sent)."""
    pkg = check_pkg(args["package"])
    title = args["title"]
    ensure_host_fixes()
    anchor = os.path.join(ANCHORS, pkg)
    dep = deployment(pkg)
    base, free = new_base(args, pkg, dep)
    appid = dep["appid"] if dep else shortcut_appid(f'"{anchor}/launch.sh"', title)
    if container_running(appid):
        raise AgentError(f"{title} is running on the Frame. Close it first.")
    incoming = os.path.join(base, "incoming")
    os.makedirs(os.path.join(incoming, "obb"), exist_ok=True)
    app = os.path.join(base, "lepton-app")
    existing = {}
    obb = os.path.join(app, "obb")
    for root, _, files in os.walk(obb):
        for name in files:
            p = os.path.join(root, name)
            existing[os.path.relpath(p, obb)] = os.path.getsize(p)
    existing.update(tree_manifest(os.path.join(incoming, "obb")))  # uploaded before an interruption
    apk = os.path.join(app, "game.apk")
    want_sha = args.get("apk_sha256")
    same_apk = bool(want_sha and os.path.exists(apk) and os.path.getsize(apk) == args.get("apk_size")
                    and sha256_file(apk) == want_sha)
    lepton, _ = lepton_path()
    return {"package": pkg, "base": base, "anchor": anchor, "appid": appid, "incoming": incoming,
            "installed": bool(dep), "same_apk": same_apk, "existing_obb": existing,
            "free_bytes": free, "lepton": lepton}


LAUNCH_SH = (r"""#!/usr/bin/env bash
# Steam Frame launcher for {title} ({pkg}). Generated by FramePort.
set -euo pipefail
app_dir={base_q}
[[ -d "$app_dir/lepton-app" ]] || {{ echo "Game files missing at $app_dir (storage not mounted?)" >&2; exit 1; }}
{single}
# Some games (Unreal cloud saves, SUPERHOT's cloud/data: mode 1700) create folders without write/search permission
# for the app inside Lepton (it writes through the folder's group), which breaks saving or makes the game quit.
# Repair them before and during every launch.
fix_perms() {{ find "$app_dir/lepton-data/external" -type d \( ! -perm -u+rwx -o ! -perm -g+rwx \) """
r"""-exec chmod u+rwx,g+rwx {{}} + 2>/dev/null ||"""
r""" true; }}
# Android can't create an app's external files/cache folders inside Lepton ("Invalid mkdirs path ... not a known app
# path"): getExternalCacheDir() then returns nothing, which breaks e.g. Whirligig's video player cache. Create them"""
r""" here.
mkdir -p "$app_dir/lepton-data/external/Android/data/{pkg}/files" """
r""""$app_dir/lepton-data/external/Android/data/{pkg}/cache" 2>/dev/null || true
fix_perms
{watchdog}
export SteamAppId={appid}
export STEAM_COMPAT_INSTALL_PATH="$app_dir/lepton-app"
export STEAM_COMPAT_DATA_PATH="$app_dir/lepton-data"
export STEAM_COMPAT_SHADER_PATH="$app_dir/lepton-shaders"
export STEAM_COMPAT_LIBRARY_PATHS="$app_dir"
export LEPTON_ENV_FRAMEBRIDGE_CONFIG="$app_dir/settings.conf"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
export IS_PARENT=true
{extra_env}
{video_codec}
child=''
stop() {{
    trap - EXIT INT TERM
    kill $permfix 2>/dev/null || true
    [[ -n "$child" ]] && kill -TERM -- "-$child" 2>/dev/null || true
    podman kill "lepton-steamlaunch-$SteamAppId" >/dev/null 2>&1 || true
    {plays_end}
}}
trap stop EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
{plays_start}
setsid {lepton_q} start >"$app_dir/launch.log" 2>&1 &
child=$!
{dashboard}
{logcat}
wait "$child"
""")


# Every 2 s: repair folder permissions, and end the game when whoever started this launcher is gone. Steam starts it
# under its "reaper"; when Steam stops only the reaper (seen: SIGTERM to the reaper left launch.sh, Lepton and the
# container running), the game used to keep running with nothing in Steam to close it (GitHub #36).
OLD_WATCHDOG = "( while sleep 2 && kill -0 $$ 2>/dev/null; do fix_perms; done ) & permfix=$!"
# SteamVR's dashboard (Resume game / controller / VR options) is open when a FramePort game starts and has to be closed
# by hand (also when Lepton shows its Android launcher first). Once the game's first VR frames are logged, the agent
# closes it through Steam's UI (_dashboard_worker); opt out per game with FRAMEPORT_KEEP_DASHBOARD=1.
DASHBOARD_LINE = ('[[ -n "${{FRAMEPORT_KEEP_DASHBOARD:-}}" ]] || python3 {agent_q} _dashboard_worker '
                  '"$app_dir/launch.log" $$ >"$app_dir/dashboard.log" 2>&1 9>&- &')
WATCHDOG = ("parent=$PPID\n"
            "( while sleep 2 && kill -0 $$ 2>/dev/null; do fix_perms;"
            " if [[ $parent -gt 1 ]] && ! kill -0 $parent 2>/dev/null; then kill -TERM $$; fi; done )"
            " 9>&- & permfix=$!")


# One launcher per game: Play pressed again while Lepton still boots (~10 s with nothing to see) made the second Lepton
# stop the first one's container, and both died (Vader Immortal, BattleSisters). The lock is held by Lepton's process
# (inherited fd), so it is released when the game ends.
SINGLE_LINE = ('exec 9>"$app_dir/.launch.lock"; flock -n 9 || '
               '{ echo "$(date +%s) already starting or running: second launch ignored" >>"$app_dir/launch-dup.log"; '
               'exit 0; }')


# Lepton mirrors the game's logcat into launch.log, and that reader sometimes dies right after the game starts
# ("logcat: Unexpected EOF!", seen with Vader Immortal on Lepton 3.0.5 and Under Cover on 2.8.14). The game runs on,
# but launch.log stays empty: no dashboard auto-hide, no launch-test result. _logcat_keeper then reads the
# container's logcat itself and appends it to launch.log.
LOGCAT_LINE = ('python3 {agent_q} _logcat_keeper "$app_dir/launch.log" "$SteamAppId" $$ '
               '>"$app_dir/logcat-keeper.log" 2>&1 9>&- &')


def dashboard_line():
    return DASHBOARD_LINE.format(agent_q=shlex.quote(os.path.abspath(__file__)))


def logcat_line():
    return LOGCAT_LINE.format(agent_q=shlex.quote(os.path.abspath(__file__)))


def plays_lines(anchor):
    """launch.sh lines that log play sessions to <anchor>/plays.log (screenshots are matched to games by time: the
    Frame files every headset screenshot under SteamVR). Never fail the launcher."""
    q = shlex.quote(os.path.join(anchor, PLAYS_LOG))
    return (f'echo "start $(date +%s)" >>{q} 2>/dev/null || true',
            f'echo "end $(date +%s)" >>{q} 2>/dev/null || true')


def upgrade_launchers():
    """Give launchers written by older agents the parent watchdog, the dashboard closer and the play-session log (in
    place: a new file, so a running launcher keeps reading the old one). Returns the packages changed."""
    changed = []
    for path in glob.glob(os.path.join(ANCHORS, "*/launch.sh")):
        try:
            text = open(path).read()
        except OSError:
            continue
        new = text
        # the codec line follows the game's deployment (only Lepton launchers have this variable; Proton/Linux
        # launchers are never touched). Running launchers keep reading their old file.
        if "export LEPTON_ENV_FRAMEBRIDGE_CONFIG=" in new:
            new = set_codec_line(new, wants_hw_video(os.path.dirname(path)))
        if "a Linux app. Generated by FramePort" in new and "FRAMEPORT_DESKTOP" not in new:
            new = upgrade_linux_launcher(new)
        if OLD_WATCHDOG in new and "parent=$PPID" not in new:
            new = new.replace(OLD_WATCHDOG, WATCHDOG, 1)
        if "_dashboard_worker" not in new and 'child=$!\nwait "$child"' in new:
            new = new.replace('child=$!\nwait "$child"', 'child=$!\n' + dashboard_line() + '\nwait "$child"', 1)
        if "_logcat_keeper" not in new and "_dashboard_worker" in new and '\nwait "$child"' in new:
            new = new.replace('\nwait "$child"', '\n' + logcat_line() + '\nwait "$child"', 1)
        guard = '[[ -d "$app_dir/lepton-app" ]] ||'
        if ".launch.lock" not in new and guard in new:
            i = new.index("\n", new.index(guard)) + 1
            new = new[:i] + SINGLE_LINE + "\n" + new[i:]
            # the lock belongs to Lepton only: helpers that outlive the game must not keep it
            new = new.replace("; fi; done ) & permfix=$!", "; fi; done ) 9>&- & permfix=$!", 1)
            new = new.replace('>"$app_dir/dashboard.log" 2>&1 &', '>"$app_dir/dashboard.log" 2>&1 9>&- &', 1)
        if PLAYS_LOG not in new:
            start, end = plays_lines(os.path.dirname(path))
            if "\nsetsid " in new and "\n    trap - EXIT INT TERM\n" in new:  # Lepton launcher
                new = new.replace("\nsetsid ", f"\n{start}\nsetsid ", 1)
                new = new.replace("\n    trap - EXIT INT TERM\n", f"\n    trap - EXIT INT TERM\n    {end}\n", 1)
            elif "\nexec " in new:  # Proton launcher: exec replaces the shell, so only the start is logged
                new = new.replace("\nexec ", f"\n{start}\nexec ", 1)
        if new == text:
            continue
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(new)
        os.chmod(tmp, os.stat(path).st_mode)
        os.replace(tmp, path)
        changed.append(os.path.basename(os.path.dirname(path)))
    return changed


def wants_hw_video(anchor):
    """Whether this game's launcher gets the shared codec (frame.hw_video_decode): deployment.json's choice (written
    at finalize), else its recipe's patches. A game that had agent <= 70's per-game codec (extracted from its APK:
    Batman) keeps it; that choice is saved, since ensure_host_fixes then removes the old codec folder."""
    path = os.path.join(anchor, "deployment.json")
    try:
        with open(path) as f:
            dep = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(dep, dict):
        return False
    if "hw_video_decode" in dep:
        return bool(dep["hw_video_decode"])
    recipe = dep.get("recipe") if isinstance(dep.get("recipe"), dict) else {}
    if HW_VIDEO_PATCH in (recipe.get("patches") or []):
        return True
    base = dep.get("base")
    if isinstance(base, str) and os.path.exists(os.path.join(base, "frameport-codec/bin/podman")):
        dep["hw_video_decode"] = True
        with open(path + ".tmp", "w") as f:
            json.dump(dep, f, indent=2)
        os.replace(path + ".tmp", path)
        return True
    return False


def set_codec_line(text, want):
    """A Lepton launcher with the current codec line where it belongs (want) or none; earlier lines are dropped."""
    at = -1
    for line in (VIDEO_CODEC_LINE, *OLD_CODEC_LINES):
        i = text.find(line + "\n")
        while i >= 0:
            text = text[:i] + text[i + len(line) + 1:]
            at = i if at < 0 else min(at, i)  # the earliest one's place (text before it is unchanged)
            i = text.find(line + "\n")
    if want:
        if at < 0:
            for anchor in ("\nchild=''\n", "\nsetsid "):
                if anchor in text:
                    at = text.index(anchor) + 1
                    break
        if at >= 0:
            text = text[:at] + VIDEO_CODEC_LINE + "\n" + text[at:]
    return text


def remove_old_codec_dirs():
    """Agent <= 70 extracted a per-game codec into <base>/frameport-codec; the shared codec replaced it. Removed
    once the game's launcher no longer uses it (upgrade_launchers converted it; the choice is in deployment.json or
    the recipe's patches)."""
    removed = []
    for dep_path in glob.glob(os.path.join(ANCHORS, "*/deployment.json")):
        try:
            with open(dep_path) as f:
                dep = json.load(f)
            base = dep.get("base")
        except (OSError, ValueError, AttributeError):
            continue
        old = os.path.join(base, "frameport-codec") if isinstance(base, str) else ""
        if not old or not os.path.isdir(old) or os.path.islink(old):
            continue
        try:
            with open(os.path.join(os.path.dirname(dep_path), "launch.sh")) as f:
                launcher = f.read()
        except OSError:
            launcher = ""
        if "frameport-codec" in launcher:
            continue  # its launcher hasn't been converted yet
        shutil.rmtree(old, ignore_errors=True)
        removed.append(dep.get("package") or os.path.basename(os.path.dirname(dep_path)))
    return removed


def cmd_video_codec_switch(args):
    """FramePort's setting "Hardware video decoding" for every game on this Frame: off writes VIDEO_CODEC_DIR/disabled,
    which launchers and the wrapper check at every start (no launcher rewrite, running games keep what they have)."""
    flag = os.path.join(VIDEO_CODEC_DIR, "disabled")
    if args.get("enabled", True):
        if os.path.exists(flag):
            os.remove(flag)
    else:
        os.makedirs(VIDEO_CODEC_DIR, exist_ok=True)
        with open(flag, "w") as f:
            f.write("switched off in FramePort\n")
    return cmd_video_codec_status({})


def upgrade_linux_launcher(text):
    """A Linux app's launcher from before agent v63, made fit for Desktop Mode's menu entry (GitHub #84): no Steam
    parent watchdog and no display taken from Steam when FRAMEPORT_DESKTOP is set."""
    old_if = 'if [[ -z "${DISPLAY:-}" ]]; then'
    if old_if in text:
        text = text.replace(old_if, 'if [[ -z "${FRAMEPORT_DESKTOP:-}" && -z "${DISPLAY:-}" ]]; then', 1)
    if "\nparent=$PPID\n" in text:
        text = text.replace("\nparent=$PPID\n",
                            '\nparent=$PPID\n[[ -n "${FRAMEPORT_DESKTOP:-}" ]] && parent=1\n', 1)
    return text


def write_launcher(anchor, base, pkg, title, appid, lepton, env, hw_video=False):
    extra = "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in (env or {}).items()
                    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", k))
    text = LAUNCH_SH.format(title=title.replace("\n", " "), pkg=pkg, base_q=shlex.quote(base), appid=appid,
                            lepton_q=shlex.quote(lepton), extra_env=extra, watchdog=WATCHDOG,
                            dashboard=dashboard_line(), logcat=logcat_line(), single=SINGLE_LINE,
                            video_codec=VIDEO_CODEC_LINE if hw_video else "",
                            plays_start=plays_lines(anchor)[0],
                            plays_end=plays_lines(anchor)[1])
    path = os.path.join(anchor, "launch.sh")
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.chmod(path + ".tmp", 0o755)
    os.replace(path + ".tmp", path)


def data_files_dir(base, pkg):
    return os.path.join(base, "lepton-data/external/Android/data", pkg, "files")


def cmd_video_codec_status(args):
    """The shared codec installed on this Frame ({digest, revision}, {} if none or damaged) and whether FramePort's
    setting switched it off for every game ("disabled")."""
    status = video_codec_installed()
    status["disabled"] = os.path.exists(os.path.join(VIDEO_CODEC_DIR, "disabled"))
    return status


def video_codec_installed():
    path = os.path.join(VIDEO_CODEC_DIR, "current", "manifest.json")
    try:
        with open(path, "rb") as f:
            raw = f.read()
        manifest = json.loads(raw)
        with open(os.path.join(os.path.dirname(path), "deployment.json")) as f:
            config = json.load(f)
        if not isinstance(config, dict) or config.get("scope") != "shared" or \
                config.get("runtime_sha256") != manifest["runtime_sha256"]:
            return {}
        for name in VIDEO_CODEC_FILES:
            local = "bin/podman" if name == "podman.py" else name
            if sha256_file(os.path.join(os.path.dirname(path), local)) != manifest["files"][name]:
                return {}
        return {"digest": hashlib.sha256(raw).hexdigest(), "revision": manifest.get("revision", 1)}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def prune_video_codec_versions(versions, keep):
    """Each revision is ~15 MB. Keep the active one and the one it replaced (a launch that resolved the old
    'current' just before the switch still mounts its files); running containers hold their mounts anyway."""
    for name in os.listdir(versions):
        if name in keep or not re.fullmatch(r"[0-9a-f]{64}(\.previous-[0-9]+)?|\.install-.*", name):
            continue
        path = os.path.join(versions, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)


def cmd_install_video_codec(args):
    """Verify a shared payload, then publish its complete version in one step."""
    encoded = args["bundle"]
    if not isinstance(encoded, str) or len(encoded) > 32 * 1024 * 1024:
        raise AgentError("oversized video codec bundle")
    import io

    raw = base64.b64decode(encoded, validate=True)
    data = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if set(archive.namelist()) != {"manifest.json", *VIDEO_CODEC_FILES} or len(archive.infolist()) != 5:
            raise AgentError("unexpected video codec bundle files")
        for name in ("manifest.json", *VIDEO_CODEC_FILES):
            if archive.getinfo(name).file_size > 16 * 1024 * 1024:
                raise AgentError(f"oversized video codec asset: {name}")
            data[name] = archive.read(name)
    manifest = json.loads(data["manifest.json"])
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict) or \
            not isinstance(manifest.get("revision", 1), int) or manifest.get("revision", 1) < 1:
        raise AgentError("invalid video codec manifest")
    digest = hashlib.sha256(data["manifest.json"]).hexdigest()
    if digest != args["digest"]:
        raise AgentError("video codec manifest checksum mismatch")
    for name in VIDEO_CODEC_FILES:
        if hashlib.sha256(data[name]).hexdigest() != manifest["files"][name]:
            raise AgentError(f"video codec asset checksum mismatch: {name}")
    # A second PC with older FramePort must not downgrade the shared codec.
    os.makedirs(VIDEO_CODEC_DIR, exist_ok=True)
    with open(os.path.join(VIDEO_CODEC_DIR, "install.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = video_codec_installed()
        if current.get("digest") not in (None, digest) and current.get("revision", 0) >= manifest.get("revision", 1):
            # another PC's FramePort installed this revision (or a newer one) built differently: keep it, two
            # PCs mustn't replace each other's codec at every connection
            return dict(current, kept=True)
        versions = os.path.join(VIDEO_CODEC_DIR, "versions")
        os.makedirs(versions, exist_ok=True)
        version = os.path.join(versions, digest)
        previous = os.path.basename(os.path.realpath(os.path.join(VIDEO_CODEC_DIR, "current")))
        if current.get("digest") != digest:
            stage = tempfile.mkdtemp(prefix=".install-", dir=versions)
            try:
                os.mkdir(os.path.join(stage, "bin"))
                # Config first, executable last, then expose the entire version.
                config = {"scope": "shared", "runtime_sha256": manifest["runtime_sha256"]}
                with open(os.path.join(stage, "deployment.json"), "w") as f:
                    json.dump(config, f)
                for name in ("manifest.json", *[n for n in VIDEO_CODEC_FILES if n != "podman.py"], "podman.py"):
                    target = os.path.join(stage, "bin/podman" if name == "podman.py" else name)
                    with open(target, "wb") as f:
                        f.write(data[name])
                    os.chmod(target, 0o755 if name == "podman.py" else 0o644)
                if os.path.lexists(version):
                    # Preserve an interrupted/corrupt prior version for diagnosis.
                    os.rename(version, version + f".previous-{time.time_ns()}")
                os.rename(stage, version)
                link = os.path.join(VIDEO_CODEC_DIR, f".current-{os.getpid()}")
                if os.path.lexists(link):
                    os.unlink(link)
                os.symlink(os.path.join("versions", digest), link)
                os.replace(link, os.path.join(VIDEO_CODEC_DIR, "current"))
            finally:
                if os.path.isdir(stage):
                    shutil.rmtree(stage)
        prune_video_codec_versions(versions, {digest, previous})
        upgraded = upgrade_launchers()
    return {"digest": digest, "revision": manifest.get("revision", 1), "launchers": upgraded}


def set_flatscreen(app_dir, on):
    """Lepton shows an app as a flat (2D) window only when its app folder holds this marker
    (liblepton/app_metadata.sh); otherwise the app runs headless and only OpenXR output reaches the headset."""
    marker = os.path.join(app_dir, "lepton-show-flatscreen")
    if on:
        open(marker, "a").close()
    elif os.path.exists(marker):
        os.remove(marker)


def cmd_finalize(args):
    """Move uploaded files into place, write launcher/settings/config files/deployment.json. Keeps saves."""
    pkg = check_pkg(args["package"])
    title = args["title"]
    prep = cmd_prepare({"package": pkg, "title": title, "dest": args.get("dest")})
    base, anchor, appid, incoming = prep["base"], prep["anchor"], prep["appid"], prep["incoming"]
    lepton = prep["lepton"]
    if not lepton:
        raise AgentError("Lepton is not installed (Developer Mode → install/launch 'Lepton Development' once)")
    app = os.path.join(base, "lepton-app")
    os.makedirs(os.path.join(app, "obb"), exist_ok=True)
    os.makedirs(anchor, exist_ok=True)
    new_apk = os.path.join(incoming, "game.apk")
    inc_obb = os.path.join(incoming, "obb")
    expected = args.get("obb_manifest")  # rel path -> size
    if expected:  # check the data as it will be after the move, before replacing anything (a failure keeps the old)
        have = {}
        for top in (os.path.join(app, "obb"), inc_obb):
            for root, _, files in os.walk(top):
                for name in files:
                    if not name.endswith(".part"):
                        p = os.path.join(root, name)
                        have[os.path.relpath(p, top)] = os.path.getsize(p)
        bad = [k for k, v in expected.items() if have.get(k) != v]
        if bad:
            raise AgentError(f"{len(bad)} data file(s) missing or incomplete, e.g. {bad[0]}")
    if os.path.exists(new_apk):
        if args.get("apk_sha256") and sha256_file(new_apk) != args["apk_sha256"]:
            raise AgentError("uploaded APK checksum mismatch (transfer corrupted?)")
        cur = os.path.join(app, "game.apk")
        if os.path.exists(cur):
            os.replace(cur, os.path.join(base, "previous-game.apk"))  # one generation for rollback
        os.replace(new_apk, cur)
    elif not os.path.exists(os.path.join(app, "game.apk")):
        raise AgentError("no APK uploaded and none installed")
    moved = 0
    for root, _, files in os.walk(inc_obb):
        for name in files:
            if name.endswith(".part"):
                continue
            src = os.path.join(root, name)
            dst = os.path.join(app, "obb", os.path.relpath(src, inc_obb))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
            moved += 1
    shutil.rmtree(incoming, ignore_errors=True)
    if "flatscreen" in args:  # older clients don't send it: leave the marker as it is
        set_flatscreen(app, args["flatscreen"])
    # settings: settings.conf (read by the adapter via LEPTON_ENV_FRAMEBRIDGE_CONFIG) + framebridge.conf copy
    files_dir = data_files_dir(base, pkg)
    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(os.path.join(base, "lepton-shaders"), exist_ok=True)
    settings = args.get("settings") or {}
    conf = os.path.join(base, "settings.conf")
    if settings:
        if os.path.exists(conf):
            shutil.copy2(conf, conf + ".previous")
        text = "".join(f"{k}={v}\n" for k, v in settings.items())
        with open(conf, "w") as f:
            f.write(text)
        with open(os.path.join(files_dir, "framebridge.conf"), "w") as f:
            f.write(text)
    for rel, content in (args.get("files") or {}).items():
        target = os.path.normpath(os.path.join(files_dir, rel))
        if not target.startswith(files_dir + os.sep):
            raise AgentError(f"refusing to write outside the game's files dir: {rel}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write(content)
    models = install_controller_models(files_dir, str(settings.get("controller_models", 0)) not in ("0", "0.0"))
    recipe = args.get("recipe") if isinstance(args.get("recipe"), dict) else {}
    hw_video = HW_VIDEO_PATCH in (recipe.get("patches") or [])  # the shared codec (install_video_codec)
    write_launcher(anchor, base, pkg, title, appid, lepton, args.get("env"), hw_video)
    art_in = os.path.join(base, "incoming-artwork")
    if os.path.isdir(art_in):
        shutil.rmtree(os.path.join(anchor, "artwork"), ignore_errors=True)
        shutil.move(art_in, os.path.join(anchor, "artwork"))
    dep = {"package": pkg, "appid": int(appid), "base": base, "title": title, "tags": args.get("tags") or [],
           "apk": args.get("apk_name", "game.apk"),
           "sha256": args.get("apk_sha256"), "recipe": args.get("recipe"), "installed_by": "frameport",
           "hw_video_decode": hw_video, "agent_version": AGENT_VERSION, "time": time.time()}
    with open(os.path.join(anchor, "deployment.json"), "w") as f:
        json.dump(dep, f, indent=2)
    return {"ok": True, "base": base, "appid": appid, "moved_data_files": moved, "controller_models": models}


# --------------------------------------------------------------- Steam Frame controller models (XR_FB_render_model)
# The FrameBridge adapter (setting controller_models=1) serves these to games that ask the runtime for controller
# models.
# They are converted here, on the Frame, from the SteamVR render models the Frame already has (OBJ + PNG) into glTF
# binaries, so Valve's models never leave the device.
MODELS_CACHE = os.path.join(HOME, ".local/share/frameport/controller-models")
FRAME_MODEL_RE = re.compile(r"frame", re.I)
SKIP_COMPONENTS = ("status", "led", "scroll_wheel_touch")


def steamvr_roots():
    roots = []
    rt = openxr_runtime()
    if rt and rt.get("path"):
        roots.append(os.path.dirname(rt["path"]))
    roots += [os.path.join(lib, "steamapps/common/SteamVR") for lib in steam_libraries()]
    roots += ["/opt/steamvr", os.path.join(STEAM, "steamapps/common/SteamVR")]
    out = []
    for r in roots:
        r = os.path.realpath(r)
        if os.path.isdir(r) and r not in out:
            out.append(r)
    return out


def render_model_dirs(roots=None):
    """name -> directory of every SteamVR render model (a folder with .obj files)."""
    found = {}
    for root in roots if roots is not None else steamvr_roots():
        for pattern in ("resources/rendermodels/*", "drivers/*/resources/rendermodels/*"):
            for d in sorted(glob.glob(os.path.join(root, pattern))):
                if os.path.isdir(d) and glob.glob(os.path.join(d, "*.obj")):
                    found.setdefault(os.path.basename(d), d)
    return found


def model_side(name):
    n = name.lower()
    for side in ("left", "right"):
        if side in n:
            return side
    for side in ("left", "right"):  # e.g. controller_l / controller-r
        if re.search(rf"(^|[_\-. ]){side[0]}([_\-. ]|$)", n):
            return side
    return None


def pick_controller_models(dirs, pattern=FRAME_MODEL_RE):
    """{'left': dir, 'right': dir} for the Steam Frame controllers (names matching `pattern`), or {}."""
    best = {}
    for name, d in dirs.items():
        side = model_side(name)
        if not side or not pattern.search(name):
            continue
        score = ("controller" in name.lower()) * 10 - len(name)
        if side not in best or score > best[side][0]:
            best[side] = (score, d)
    return {s: d for s, (_, d) in best.items()} if len(best) == 2 else {}


def _rotation_xyz(deg):
    """3x3 rotation for SteamVR's rotate_xyz (degrees; applied about X, then Y, then Z)."""
    import math
    rx, ry, rz = (math.radians(float(v)) for v in (list(deg) + [0, 0, 0])[:3])
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    X = [[1, 0, 0], [0, cx, -sx], [0, sx, cx]]
    Y = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    Z = [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]
    mul = lambda a, b: [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return mul(Z, mul(Y, X))


def model_description(model_dir):
    """(obj files, grip transform or None) from the model's JSON (components) or the folder's .obj files."""
    name = os.path.basename(model_dir)
    desc = {}
    for cand in (os.path.join(model_dir, name + ".json"), *sorted(glob.glob(os.path.join(model_dir, "*.json")))):
        try:
            desc = json.load(open(cand))
            if isinstance(desc, dict) and "components" in desc:
                break
        except (OSError, ValueError):
            desc = {}
    objs, grip = [], None
    for cname, comp in (desc.get("components") or {}).items():
        if not isinstance(comp, dict):
            continue
        if cname == "openxr_grip" and isinstance(comp.get("component_local"), dict):
            local = comp["component_local"]
            grip = (list(local.get("origin") or [0, 0, 0]), _rotation_xyz(local.get("rotate_xyz") or [0, 0, 0]))
        fn = comp.get("filename")
        hidden = (comp.get("visibility") or {}).get("default") is False  # e.g. status LEDs
        if fn and not hidden and not any(s in cname.lower() for s in SKIP_COMPONENTS):
            p = os.path.join(model_dir, fn)
            if os.path.exists(p):
                objs.append(p)
    if not objs:
        whole = os.path.join(model_dir, name + ".obj")
        objs = [whole] if os.path.exists(whole) else sorted(glob.glob(os.path.join(model_dir, "*.obj")))
    return objs, grip


def parse_mtl(path):
    """material name -> texture file (map_Kd)."""
    out, cur = {}, None
    try:
        for line in open(path, errors="replace"):
            parts = line.strip().split(None, 1)
            if not parts:
                continue
            if parts[0] == "newmtl" and len(parts) > 1:
                cur = parts[1].strip()
            elif parts[0] == "map_Kd" and cur and len(parts) > 1:
                out[cur] = os.path.join(os.path.dirname(path), parts[1].strip().split()[-1])
    except OSError:
        pass
    return out


def parse_obj(path):
    """-> list of (texture path or None, positions, normals, uvs, triangles as (v, vt, vn) index triples)."""
    pos, nrm, uv = [], [], []
    groups = {}  # texture -> list of corner triples
    mats, tex = {}, None
    for line in open(path, errors="replace"):
        parts = line.split()
        if not parts:
            continue
        tag = parts[0]
        if tag == "v":
            pos.append(tuple(float(x) for x in parts[1:4]))
        elif tag == "vn":
            nrm.append(tuple(float(x) for x in parts[1:4]))
        elif tag == "vt":
            uv.append((float(parts[1]), float(parts[2]) if len(parts) > 2 else 0.0))
        elif tag == "mtllib":
            mats.update(parse_mtl(os.path.join(os.path.dirname(path), " ".join(parts[1:]))))
        elif tag == "usemtl":
            tex = mats.get(" ".join(parts[1:]))
        elif tag == "f":
            corners = []
            for c in parts[1:]:
                idx = (c.split("/") + ["", ""])[:3]
                ref = []
                for i, n in zip(idx, (len(pos), len(uv), len(nrm)), strict=True):
                    ref.append(None if not i else (int(i) - 1 if int(i) > 0 else n + int(i)))
                corners.append(tuple(ref))
            for i in range(1, len(corners) - 1):  # fan triangulation
                groups.setdefault(tex, []).extend((corners[0], corners[i], corners[i + 1]))
    return pos, nrm, uv, groups


def controller_glb(model_dir):
    """glTF binary (one mesh, one primitive per texture, PNG/JPEG textures, in the controller's OpenXR grip space
    when the model defines openxr_grip) for a SteamVR render model folder."""
    objs, grip = model_description(model_dir)
    if not objs:
        raise AgentError(f"no .obj files in {model_dir}")
    # texture -> (positions, normals, uvs, indices), vertices de-duplicated per corner triple
    prims = {}
    for obj in objs:
        pos, nrm, uv, groups = parse_obj(obj)
        for tex, corners in groups.items():
            if tex and os.path.splitext(tex)[1].lower() not in (".png", ".jpg", ".jpeg"):
                tex = None
            p = prims.setdefault(tex, {"pos": [], "nrm": [], "uv": [], "idx": [], "map": {}})
            for c in corners:
                key = (obj, c)
                if key not in p["map"]:
                    p["map"][key] = len(p["pos"])
                    v = pos[c[0]]
                    n = nrm[c[2]] if c[2] is not None and c[2] < len(nrm) else (0.0, 0.0, 1.0)
                    t = uv[c[1]] if c[1] is not None and c[1] < len(uv) else (0.0, 0.0)
                    if grip:  # raw device space -> grip space: R^T (v - origin)
                        o, r = grip
                        d = [v[k] - float(o[k]) for k in range(3)]
                        v = tuple(sum(r[k][j] * d[k] for k in range(3)) for j in range(3))
                        n = tuple(sum(r[k][j] * n[k] for k in range(3)) for j in range(3))
                    p["pos"].append(v)
                    p["nrm"].append(n)
                    p["uv"].append((t[0], 1.0 - t[1]))  # OBJ origin bottom-left, glTF top-left
                p["idx"].append(p["map"][key])
    blob = bytearray()
    views, accessors, images, textures, materials, primitives = [], [], [], [], [], []

    def add_view(data, target=None):
        while len(blob) % 4:
            blob.append(0)
        view = {"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)}
        if target:
            view["target"] = target
        blob.extend(data)
        views.append(view)
        return len(views) - 1

    def add_accessor(values, width, ctype, kind, target, minmax=False):
        fmt = {5126: "f", 5125: "I"}[ctype]
        flat = [x for v in values for x in (v if width > 1 else (v,))]
        acc = {"bufferView": add_view(struct.pack(f"<{len(flat)}{fmt}", *flat), target), "componentType": ctype,
               "count": len(values), "type": kind}
        if minmax:
            acc["min"] = [min(v[k] for v in values) for k in range(width)]
            acc["max"] = [max(v[k] for v in values) for k in range(width)]
        accessors.append(acc)
        return len(accessors) - 1

    for tex, p in prims.items():
        if not p["idx"]:
            continue
        attrs = {"POSITION": add_accessor(p["pos"], 3, 5126, "VEC3", 34962, minmax=True),
                 "NORMAL": add_accessor(p["nrm"], 3, 5126, "VEC3", 34962),
                 "TEXCOORD_0": add_accessor(p["uv"], 2, 5126, "VEC2", 34962)}
        indices = add_accessor(p["idx"], 1, 5125, "SCALAR", 34963)
        mat = {"pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 0.8}}
        if tex and os.path.exists(tex):
            mime = "image/png" if tex.lower().endswith(".png") else "image/jpeg"
            images.append({"bufferView": add_view(open(tex, "rb").read()), "mimeType": mime})
            textures.append({"source": len(images) - 1})
            mat["pbrMetallicRoughness"]["baseColorTexture"] = {"index": len(textures) - 1}
        else:
            mat["pbrMetallicRoughness"]["baseColorFactor"] = [0.1, 0.1, 0.1, 1.0]
        materials.append(mat)
        primitives.append({"attributes": attrs, "indices": indices, "material": len(materials) - 1})
    if not primitives:
        raise AgentError(f"no triangles in {model_dir}")
    while len(blob) % 4:
        blob.append(0)
    gltf = {"asset": {"version": "2.0", "generator": "FramePort agent"}, "scene": 0, "scenes": [{"nodes": [0]}],
            "nodes": [{"name": os.path.basename(model_dir), "mesh": 0}],
            "meshes": [{"primitives": primitives}], "materials": materials, "accessors": accessors,
            "bufferViews": views, "buffers": [{"byteLength": len(blob)}]}
    if images:
        gltf.update(images=images, textures=textures, samplers=[{}])
        for t in textures:
            t["sampler"] = 0
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(blob)
    return (struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<I4s", len(js), b"JSON") + js
            + struct.pack("<I4s", len(blob), b"BIN\x00") + bytes(blob))


def _tree_stamp(d):
    h = hashlib.sha256()
    for p in sorted(glob.glob(os.path.join(d, "*"))):
        st = os.stat(p)
        h.update(f"{os.path.basename(p)}:{st.st_size}:{int(st.st_mtime)}\n".encode())
    return h.hexdigest()[:16]


def frame_controller_models():
    """Converted (cached) glb paths for the Frame controllers: {'left': path, 'right': path, 'sources': {...}}."""
    picked = pick_controller_models(render_model_dirs())
    if not picked:
        raise AgentError("no Steam Frame controller render models found in SteamVR (looked in: "
                         + ", ".join(steamvr_roots() or ["no SteamVR install"]) + ")")
    os.makedirs(MODELS_CACHE, exist_ok=True)
    out = {"sources": picked}
    for side, d in picked.items():
        cached = os.path.join(MODELS_CACHE, f"{os.path.basename(d)}-{_tree_stamp(d)}.glb")
        if not os.path.exists(cached):
            data = controller_glb(d)
            with open(cached + ".tmp", "wb") as f:
                f.write(data)
            os.replace(cached + ".tmp", cached)
        out[side] = cached
    return out


def install_controller_models(files_dir, enabled):
    """Put (or remove) framebridge/controller_{left,right}.glb in a game's files dir. Never fails the install."""
    target = os.path.join(files_dir, "framebridge")
    if not enabled:
        for side in ("left", "right"):
            p = os.path.join(target, f"controller_{side}.glb")
            if os.path.exists(p):
                os.remove(p)
        return None
    try:
        models = frame_controller_models()
    except (AgentError, OSError, ValueError, IndexError) as e:
        return {"ok": False, "error": str(e)}
    os.makedirs(target, exist_ok=True)
    for side in ("left", "right"):
        shutil.copyfile(models[side], os.path.join(target, f"controller_{side}.glb"))
    return {"ok": True, "sources": models["sources"]}


def cmd_controller_models(args):
    """Diagnostics: SteamVR roots, every render model found, and which ones are used as the Frame controllers."""
    dirs = render_model_dirs()
    result = {"roots": steamvr_roots(), "render_models": dirs, "picked": pick_controller_models(dirs)}
    if args.get("convert"):
        try:
            result["converted"] = frame_controller_models()
        except (AgentError, OSError, ValueError, IndexError) as e:
            result["error"] = str(e)
    return result


# ------------------------------------------------------------------------------------------ PC VR (Rift) under Proton
PCVR_TREES = ("game", "revive", "xrlayer", "helpers")


def tree_manifest(root):
    out = {}
    for r, _, files in os.walk(root):
        for name in files:
            if name.endswith(".part"):
                continue
            p = os.path.join(r, name)
            rel = os.path.relpath(p, root)
            if name.lower() == "crashreportclient.exe.disabled":  # renamed by set_crash_reporter: same file
                rel = rel[:-len(".disabled")]
            out[rel] = os.path.getsize(p)
    return out


def pcvr_pids(base):
    """Processes of a PC VR game (the Proton/Wine command lines contain its install folder)."""
    p = run(["pgrep", "-f", re.escape(base.rstrip("/") + "/")])
    return [x for x in p.stdout.split() if x != str(os.getpid())]


def cmd_prepare_pcvr(args):
    """Where to upload a Windows game + Revive, and what the Frame already has (unchanged files aren't re-sent)."""
    pkg = check_pkg(args["package"])
    title = args["title"]
    anchor = os.path.join(ANCHORS, pkg)
    dep = deployment(pkg)
    base, free = new_base(args, pkg, dep)
    appid = dep["appid"] if dep else shortcut_appid(f'"{anchor}/launch.sh"', title)
    if dep and pcvr_pids(base):
        raise AgentError(f"{title} is running on the Frame. Close it first.")
    incoming = os.path.join(base, "incoming")
    for t in PCVR_TREES:
        os.makedirs(os.path.join(incoming, t), exist_ok=True)
    # files already in place plus files uploaded by an interrupted install (still in incoming/): not sent again
    existing = {t: {**tree_manifest(os.path.join(base, t)), **tree_manifest(os.path.join(incoming, t))}
                for t in PCVR_TREES}
    status = cmd_proton_status({"tool": args.get("tool")})
    return {"package": pkg, "base": base, "anchor": anchor, "appid": appid, "incoming": incoming,
            "installed": bool(dep), "existing": existing, "free_bytes": free,
            "proton": status.get("ready"), "proton_suggested": status.get("suggested"), "openxr": status.get("openxr")}


LAUNCH_PROTON_SH = r"""#!/usr/bin/env bash
# Steam Frame launcher for {title} ({pkg}): Windows PC VR game under Proton ({tool}). Generated by FramePort.
set -euo pipefail
base={base_q}
[[ -d "$base/game" ]] || {{ echo "Game files missing at $base (storage not mounted?)" >&2; exit 1; }}
export SteamAppId={appid}
{steam_game_id}export STEAM_COMPAT_APP_ID={appid}
export STEAM_COMPAT_DATA_PATH="$base/compatdata"
export STEAM_COMPAT_CLIENT_INSTALL_PATH={steam_q}
export STEAM_COMPAT_INSTALL_PATH="$base/game"
export STEAM_COMPAT_LIBRARY_PATHS="$base"
export STEAM_COMPAT_SHADER_PATH="$base/shadercache"
export PROTON_LOG_DIR="$base"
{xr_layer}export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
# Wine needs the display session (gamescope's X/Wayland). Steam passes it; headless launches take it from Steam.
if [[ -z "${{DISPLAY:-}}" ]]; then
    steam_pid=$(pgrep -x steam | head -n1 || true)
    if [[ -n "$steam_pid" && -r "/proc/$steam_pid/environ" ]]; then
        while IFS= read -r -d '' kv; do
            case "$kv" in DISPLAY=*|WAYLAND_DISPLAY=*|GAMESCOPE_WAYLAND_DISPLAY=*|XAUTHORITY=*|XDG_SESSION_TYPE=*)
                export "$kv";; esac
        done < "/proc/$steam_pid/environ"
    fi
fi
{extra_env}
mkdir -p "$STEAM_COMPAT_DATA_PATH" "$STEAM_COMPAT_SHADER_PATH"
cd "$base/game"{workdir}
echo "FramePort: launching {pkg} with {tool}" >"$base/launch.log"
{plays_start}
exec {command} >>"$base/launch.log" 2>&1
"""


def windows_path(path):
    """Unix path as Wine sees it through drive Z: (the root filesystem)."""
    return "Z:" + os.path.abspath(path).replace("/", "\\")


XR_LAYER = "XR_APILAYER_FRAMEPORT_timefix"
XR_LAYER_ENV = (f'# FramePort OpenXR layer: OpenXR 1.1 -> 1.0 fallback for the Frame runtime'
                f' (+ timespec time emulation)\n'
                f'export XR_API_LAYER_PATH="$base/xrlayer${{XR_API_LAYER_PATH:+:$XR_API_LAYER_PATH}}"\n'
                f'export XR_ENABLE_API_LAYERS="{XR_LAYER}${{XR_ENABLE_API_LAYERS:+:$XR_ENABLE_API_LAYERS}}"\n')


XR_LAYER_MANIFEST = os.path.join(HOME, ".local/share/openxr/1/api_layers/explicit.d", XR_LAYER + ".json")


def install_xr_layer(base):
    """Proton's Steam Linux Runtime container drops XR_API_LAYER_PATH, so the layer is registered where the OpenXR
    loader also looks for explicit layers ($XDG_DATA_HOME/openxr/1/api_layers/explicit.d, shared into the container
    with the home dir). Explicit layers only load when XR_ENABLE_API_LAYERS names them (launch.sh does), so other apps
    are unaffected. The library is a shared copy under the agent's folder."""
    src = os.path.join(base, "xrlayer")
    with open(os.path.join(src, XR_LAYER + ".json")) as f:
        manifest = json.load(f)
    lib_name = os.path.basename(manifest["api_layer"]["library_path"])
    dest = os.path.join(AGENT_HOME, "xrlayer")
    os.makedirs(dest, exist_ok=True)
    tmp = os.path.join(dest, lib_name + ".tmp")
    shutil.copyfile(os.path.join(src, lib_name), tmp)
    os.replace(tmp, os.path.join(dest, lib_name))
    manifest["api_layer"]["library_path"] = os.path.join(dest, lib_name)
    os.makedirs(os.path.dirname(XR_LAYER_MANIFEST), exist_ok=True)
    with open(XR_LAYER_MANIFEST + ".tmp", "w") as f:
        json.dump(manifest, f, indent=4)
    os.replace(XR_LAYER_MANIFEST + ".tmp", XR_LAYER_MANIFEST)


OCULUS_HMD_HELPER = "fp_oculushmd.exe"


def write_proton_launcher(anchor, base, pkg, title, appid, tool, exe_rel, revive, env, xr_layer=False, game_args=(),
                          oculus_hmd=False, vr=True):
    exe = os.path.join(base, "game", exe_rel)
    prefix = compat_command(tool["dir"])
    injector = os.path.join(base, "revive", "ReviveInjector.exe")
    if oculus_hmd:
        # FramePort's helper provides the OculusHMDConnected event (Unreal's Oculus plugin checks for it) and runs the
        # rest of its command line, staying alive until the game has exited
        argv = prefix + [os.path.join(base, "helpers", OCULUS_HMD_HELPER)]
        argv += [windows_path(injector), "/openxr", windows_path(exe)] if revive else [windows_path(exe)]
    elif revive:
        # ReviveInjector joins its arguments into one command line; /openxr = LibReviveXR (OpenXR -> wineopenxr)
        argv = prefix + [injector, "/openxr", windows_path(exe)]
    else:
        argv = prefix + [exe]
    argv += [a for a in game_args or () if re.fullmatch(r"[A-Za-z0-9_=.:/+-]+", a)]  # e.g. -hmd=OpenXR, -vrmode OpenVR
    extra = "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in (env or {}).items()
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k))
    workdir = os.path.dirname(exe_rel)
    text = LAUNCH_PROTON_SH.format(
        title=title.replace("\n", " "), pkg=pkg, base_q=shlex.quote(base), appid=appid, steam_q=shlex.quote(STEAM),
        tool=tool["name"], extra_env=extra, workdir=("/" + shlex.quote(workdir)) if workdir else "",
        # Proton sets up VR (vrclient, wineopenxr) only when SteamGameId is set: a flat Windows game goes without
        steam_game_id=f"export SteamGameId={appid}\n" if vr else "",
        xr_layer=XR_LAYER_ENV if xr_layer else "", plays_start=plays_lines(anchor)[0],
        command=" ".join(shlex.quote(a) for a in argv))
    path = os.path.join(anchor, "launch.sh")
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.chmod(path + ".tmp", 0o755)
    os.replace(path + ".tmp", path)


def set_crash_reporter(base, enabled):
    """Unreal's CrashReportClient.exe in the Frame copy of the game: renamed to .disabled so a crash just closes the
    game (and back when enabled)."""
    game = os.path.join(base, "game")
    for root, _, files in os.walk(game):
        for name in files:
            low = name.lower()
            p = os.path.join(root, name)
            if not enabled and low == "crashreportclient.exe":
                os.replace(p, p + ".disabled")
            elif enabled and low == "crashreportclient.exe.disabled":
                os.replace(p, p[:-len(".disabled")])


def set_libovr_redirect(base, exe_rel, enabled, bundled=False):
    """LoadLibrary redirect (pure runtime substitution): put LibOVRRT{64,32}_1.dll in the game's own DLL search dir as
    a symlink to Revive's LibReviveXR runtime, so the game's Oculus SDK finds a runtime to load. The symlink keeps the
    DLL in the revive/ folder, so its sibling dependencies still resolve. This does NOT touch the game's runtime
    signature check — a build that verifies the Oculus signature of LibOVRRT will still reject Revive's (unsigned)
    runtime; this only helps builds that don't verify it. We only ever create/remove our own symlink, never a real
    DLL the game shipped. Removed when disabled."""
    game = os.path.join(base, "game")
    exe_dir = os.path.dirname(os.path.join(game, exe_rel))
    # the runtime: FramePort's Revive (revive/LibReviveXR*), or with bundled=True the repack's own LibRevive*.dll
    # next to the exe (a repack set up for SteamVR, whose Windows loader hook doesn't take effect under Proton)
    runtimes = {bits: (os.path.join(exe_dir, f"LibRevive{bits}.dll") if bundled else
                       os.path.join(base, "revive", f"LibReviveXR{bits}.dll")) for bits in ("64", "32")}
    # where the Oculus SDK looks for LibOVRRT: the game exe's dir (monolithic engines carry the shim in the exe) AND
    # next to every OVRPlugin.dll (Unreal's shim searches its own module dir).
    dirs = {os.path.dirname(os.path.join(game, exe_rel))}
    for root, _, files in os.walk(game):
        for n in files:
            if n.lower() == "ovrplugin.dll":
                dirs.add(root)
    for d in dirs:
        for bits, target in runtimes.items():
            link = os.path.join(d, f"LibOVRRT{bits}_1.dll")
            ours = os.path.islink(link) and os.path.basename(os.path.realpath(link)).lower().startswith("librevive")
            if ours and os.path.realpath(link) != os.path.realpath(target):
                os.remove(link)  # switched between FramePort's and the bundled Revive
            if enabled and os.path.isfile(target):
                if os.path.islink(link) or not os.path.exists(link):  # never clobber a real game-shipped DLL
                    if os.path.lexists(link):
                        os.remove(link)
                    os.symlink(target, link)
            elif ours:
                os.remove(link)


def cmd_finalize_pcvr(args):
    """Move uploaded game/Revive files into place, delete files the new version no longer has, write the Proton
    launcher and deployment.json. The Proton prefix (saves) is kept."""
    pkg = check_pkg(args["package"])
    title = args["title"]
    prep = cmd_prepare_pcvr({"package": pkg, "title": title, "dest": args.get("dest"), "tool": args.get("tool")})
    base, anchor, appid, incoming = prep["base"], prep["anchor"], prep["appid"], prep["incoming"]
    tool = prep["proton"]
    if not tool:
        raise AgentError("Proton isn't installed on the Frame yet (FramePort: Frame → Install Proton)")
    exe_rel = os.path.normpath(args["exe"])
    if exe_rel.startswith("..") or os.path.isabs(exe_rel):
        raise AgentError(f"bad exe path {args['exe']!r}")
    old = (deployment(pkg) or {}).get("files") or {}
    manifests = args.get("manifests") or {}
    moved = 0
    for t in PCVR_TREES:
        src_root, dst_root = os.path.join(incoming, t), os.path.join(base, t)
        for rel in tree_manifest(src_root):
            dst = os.path.join(dst_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(os.path.join(src_root, rel), dst)
            moved += 1
        want = manifests.get(t)
        if want is None:
            continue
        for rel in set(old.get(t) or {}) - set(want):  # files of the previous version that are gone now
            p = os.path.normpath(os.path.join(dst_root, rel))
            if p.startswith(dst_root + os.sep) and os.path.isfile(p):
                os.remove(p)
        have = tree_manifest(dst_root)
        bad = [k for k, v in want.items() if have.get(k) != v]
        if bad:
            raise AgentError(f"{len(bad)} {t} file(s) missing or incomplete, e.g. {bad[0]}")
    if not os.path.isfile(os.path.join(base, "game", exe_rel)):
        raise AgentError(f"game executable {exe_rel} missing after upload")
    shutil.rmtree(incoming, ignore_errors=True)
    os.makedirs(anchor, exist_ok=True)
    revive = bool(args.get("revive", True))
    if revive and not os.path.isfile(os.path.join(base, "revive", "ReviveInjector.exe")):
        raise AgentError("Revive files missing")
    xr_layer = bool(args.get("xr_layer")) and os.path.isfile(os.path.join(base, "xrlayer", XR_LAYER + ".json"))
    if args.get("xr_layer") and not xr_layer:
        raise AgentError("timefix layer files missing")
    if xr_layer:
        install_xr_layer(base)
    oculus_hmd = bool(args.get("oculus_hmd"))
    if oculus_hmd and not os.path.isfile(os.path.join(base, "helpers", OCULUS_HMD_HELPER)):
        raise AgentError(f"{OCULUS_HMD_HELPER} missing")
    set_crash_reporter(base, enabled=not args.get("no_crash_reporter"))
    set_libovr_redirect(base, exe_rel, enabled=bool(args.get("libovr_redirect")), bundled=not revive)
    vr = args.get("vr", True) is not False  # False: a flat Windows game (no VR at all)
    write_proton_launcher(anchor, base, pkg, title, appid, tool, exe_rel, revive, args.get("env"), xr_layer,
                          args.get("game_args") or [], oculus_hmd, vr=vr)
    art_in = os.path.join(base, "incoming-artwork")
    if os.path.isdir(art_in):
        shutil.rmtree(os.path.join(anchor, "artwork"), ignore_errors=True)
        shutil.move(art_in, os.path.join(anchor, "artwork"))
    dep = {"package": pkg, "kind": "pcvr", "appid": int(appid), "base": base, "title": title, "exe": exe_rel,
           "tags": args.get("tags") or [],
           "sha256": args.get("exe_sha256"), "revive": revive, "revive_version": args.get("revive_version"),
           "proton": tool["name"], "xr_layer": xr_layer, "oculus_hmd": oculus_hmd, "vr": vr,
           "libovr_redirect": bool(args.get("libovr_redirect")),
           "launcher": {"env": args.get("env") or {}, "game_args": args.get("game_args") or []},
           "recipe": args.get("recipe"),
           "installed_by": "frameport",
           "files": {t: manifests.get(t) or {} for t in PCVR_TREES},
           "agent_version": AGENT_VERSION, "time": time.time()}
    with open(os.path.join(anchor, "deployment.json"), "w") as f:
        json.dump(dep, f, indent=2)
    return {"ok": True, "base": base, "appid": appid, "moved_files": moved, "proton": tool["name"]}


# ------------------------------------------------------------------------------------------ Linux apps (arm64)
# Native arm64 Linux programs (AppImages, zip/tar builds) installed from FramePort (GitHub #31): files in
# <base>/app, AppImages extracted once (no FUSE in Steam's launch environment needed, faster start), a launcher with
# the same exit watchdog as the Quest launcher, and a Steam shortcut (VR flag for OpenXR apps).
LINUX_LAUNCH_SH = r"""#!/usr/bin/env bash
# Steam Frame launcher for {title} ({pkg}), a Linux app. Generated by FramePort.
set -uo pipefail
cd {run_dir_q} || {{ echo "App files missing at {run_dir_q}" >&2; exit 1; }}
export XDG_RUNTIME_DIR="${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}"
# Steam passes the display session (gamescope's X/Wayland); headless launches (launch tests) take it from Steam.
# Started from Desktop Mode's menu (FRAMEPORT_DESKTOP=1, GitHub #84) the desktop's own session is used.
if [[ -z "${{FRAMEPORT_DESKTOP:-}}" && -z "${{DISPLAY:-}}" ]]; then
    steam_pid=$(pgrep -x steam | head -n1 || true)
    if [[ -n "$steam_pid" && -r "/proc/$steam_pid/environ" ]]; then
        while IFS= read -r -d '' kv; do
            case "$kv" in DISPLAY=*|WAYLAND_DISPLAY=*|GAMESCOPE_WAYLAND_DISPLAY=*|XAUTHORITY=*|XDG_SESSION_TYPE=*)
                export "$kv";; esac
        done < "/proc/$steam_pid/environ"
    fi
fi
{extra_env}parent=$PPID
# Desktop Mode's launcher exits right after starting this: no Steam parent to watch
[[ -n "${{FRAMEPORT_DESKTOP:-}}" ]] && parent=1
{exe_q} "$@" >{log_q} 2>&1 &
child=$!
trap 'kill -TERM $child 2>/dev/null' INT TERM
( while sleep 2 && kill -0 $child 2>/dev/null; do
    if [[ $parent -gt 1 ]] && ! kill -0 $parent 2>/dev/null; then kill -TERM $child; fi; done ) &
wait $child
"""


def linux_run_target(base, exe_rel, appimage):
    """(working directory, program) for a Linux app: an extracted AppImage runs its AppRun."""
    if appimage:  # absolute paths: the agent finds the app's processes by its folder in their command lines
        root = os.path.join(base, "app", "squashfs-root")
        return root, os.path.join(root, "AppRun")
    path = os.path.join(base, "app", exe_rel)
    return os.path.dirname(path), path


def missing_libraries(root, programs):
    """Shared libraries the app's main programs need (ldd follows their dependencies) that neither the Frame nor the
    app itself has, with the app's own library folders on LD_LIBRARY_PATH as AppImages' AppRun sets it: e.g.
    libwebkit2gtk-4.1 for Venera. Only the programs that start, not every bundled module (FramePort's own build
    ships optional Python modules needing libjvm/libtcl that never load)."""
    libdirs = set()
    for dirpath, _dirs, files in os.walk(root):
        if any(".so" in n for n in files):
            libdirs.add(dirpath)
    env = dict(os.environ, LD_LIBRARY_PATH=":".join(sorted(libdirs)))
    missing = set()
    for path in programs:
        p = subprocess.run(["ldd", path], capture_output=True, text=True, env=env, timeout=30)
        for line in p.stdout.splitlines():
            if "=> not found" in line:
                missing.add(line.split("=>")[0].strip())
    return sorted(missing)


def appimage_programs(root):
    """The ELF programs an extracted AppImage starts: top-level and usr/bin executables (not libraries)."""
    out = []
    for d in (root, os.path.join(root, "usr", "bin")):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            path = os.path.join(d, name)
            if ".so" in name or not os.path.isfile(path) or not os.access(path, os.X_OK):
                continue
            try:
                with open(path, "rb") as f:
                    if f.read(4) == b"\x7fELF":
                        out.append(path)
            except OSError:
                pass
    return out


def cmd_prepare_linux(args):
    """Where to upload a Linux app, and what the Frame already has (unchanged files aren't re-sent)."""
    pkg = check_pkg(args["package"])
    anchor = os.path.join(ANCHORS, pkg)
    dep = deployment(pkg)
    base, free = new_base(args, pkg, dep)
    appid = dep["appid"] if dep else shortcut_appid(f'"{anchor}/launch.sh"', args["title"])
    if dep and pcvr_pids(base):
        raise AgentError(f"{args['title']} is running on the Frame. Close it first.")
    incoming = os.path.join(base, "incoming")
    os.makedirs(os.path.join(incoming, "app"), exist_ok=True)
    existing = {"app": {**tree_manifest(os.path.join(base, "app")), **tree_manifest(os.path.join(incoming, "app"))}}
    existing["app"] = {k: v for k, v in existing["app"].items() if not k.startswith("squashfs-root/")}
    return {"package": pkg, "base": base, "anchor": anchor, "appid": appid, "incoming": incoming,
            "installed": bool(dep), "existing": existing, "free_bytes": free}


def cmd_finalize_linux(args):
    """Move the uploaded app into place (files the new version lacks are deleted), extract an AppImage, check its
    libraries, write the launcher and deployment.json."""
    pkg = check_pkg(args["package"])
    title = args["title"]
    prep = cmd_prepare_linux({"package": pkg, "title": title, "dest": args.get("dest")})
    base, anchor, appid, incoming = prep["base"], prep["anchor"], prep["appid"], prep["incoming"]
    exe_rel = os.path.normpath(args["exe"])
    if exe_rel.startswith("..") or os.path.isabs(exe_rel):
        raise AgentError(f"bad program path {args['exe']!r}")
    app = os.path.join(base, "app")
    want = (args.get("manifests") or {}).get("app")
    old = ((deployment(pkg) or {}).get("files") or {}).get("app") or {}
    moved = 0
    for rel in tree_manifest(os.path.join(incoming, "app")):
        dst = os.path.join(app, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(os.path.join(incoming, "app", rel), dst)
        moved += 1
    if want is not None:
        for rel in set(old) - set(want):
            p = os.path.normpath(os.path.join(app, rel))
            if p.startswith(app + os.sep) and os.path.isfile(p):
                os.remove(p)
        have = tree_manifest(app)
        bad = [k for k, v in want.items() if have.get(k) != v]
        if bad:
            raise AgentError(f"{len(bad)} app file(s) missing or incomplete, e.g. {bad[0]}")
    exe = os.path.join(app, exe_rel)
    if not os.path.isfile(exe):
        raise AgentError(f"program {exe_rel} missing after upload")
    shutil.rmtree(incoming, ignore_errors=True)
    os.chmod(exe, os.stat(exe).st_mode | 0o111)
    appimage = bool(args.get("appimage"))
    x86 = bool(args.get("x86_64"))
    prefix = []
    if x86:  # an x86_64 build: FEX runs it on SteamOS's x86 guest system (the chain Steam itself would use)
        tool = pick_tool("linux_x86", linux_x86_tools())
        if not tool or not tool["installed"] or not tool["require_installed"]:
            raise AgentError("FEX (x86 translation for Linux apps) isn't installed on the Frame yet")
        prefix = compat_command(tool["dir"])
    if appimage:
        shutil.rmtree(os.path.join(app, "squashfs-root"), ignore_errors=True)
        # an x86_64 AppImage's own extractor is x86 code too: it runs through FEX like the app
        p = subprocess.run(prefix + [exe, "--appimage-extract"], cwd=app, capture_output=True, text=True,
                           timeout=600)
        if not os.path.isfile(os.path.join(app, "squashfs-root", "AppRun")):
            raise AgentError(f"couldn't unpack the AppImage: {(p.stderr or p.stdout).strip()[-300:]}")
    run_dir, program = linux_run_target(base, exe_rel, appimage)
    for name in args.get("executables") or []:  # other programs the app starts (archives may lose the x bit)
        q = os.path.normpath(os.path.join(app, name))
        if q.startswith(app + os.sep) and os.path.isfile(q):
            os.chmod(q, os.stat(q).st_mode | 0o111)
    # ldd can't read x86_64 programs here: their libraries come from FEX's x86 root (/usr/share/guestos/fex-mesa)
    missing = [] if x86 else missing_libraries(app, appimage_programs(run_dir) if appimage else [exe])
    os.makedirs(anchor, exist_ok=True)
    write_linux_launcher(anchor, base, pkg, title, exe_rel, appimage, prefix, args.get("env"))
    art_in = os.path.join(base, "incoming-artwork")
    if os.path.isdir(art_in):
        shutil.rmtree(os.path.join(anchor, "artwork"), ignore_errors=True)
        shutil.move(art_in, os.path.join(anchor, "artwork"))
    vr = bool(args.get("openxr"))
    dep = {"package": pkg, "kind": "linux", "appid": int(appid), "base": base, "title": title, "exe": exe_rel,
           "appimage": appimage, "vr": vr, "x86_64": x86, "tags": args.get("tags") or [],
           "sha256": args.get("exe_sha256"),
           "missing_libraries": missing, "recipe": args.get("recipe"), "installed_by": "frameport",
           "launcher": {"env": args.get("env") or {}}, "desktop_entry": args.get("desktop_entry", True) is not False,
           "files": {"app": want or {}}, "agent_version": AGENT_VERSION, "time": time.time()}
    with open(os.path.join(anchor, "deployment.json"), "w") as f:
        json.dump(dep, f, indent=2)
    try:  # the app's own icon (GitHub #99), found again for every install: a new version may bring another
        own_icon = ensure_app_icon(dep, anchor, refresh=True)["icon"]
    except OSError:
        own_icon = None
    desktop = None
    try:
        if dep["desktop_entry"]:
            desktop = write_desktop_entry(pkg)
        else:
            remove_desktop_entries(pkg)
    except OSError:
        pass
    return {"ok": True, "base": base, "appid": appid, "moved_files": moved, "missing_libraries": missing,
            "desktop_entry": desktop, "app_icon": app_icon_result(own_icon)}


def write_linux_launcher(anchor, base, pkg, title, exe_rel, appimage, prefix, env):
    """launch.sh of a Linux app; prefix = FEX's command for x86_64 builds ([] for arm64)."""
    run_dir, program = linux_run_target(base, exe_rel, appimage)
    command = list(prefix) + [program]
    extra = "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in (env or {}).items()
                    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", k))
    if prefix:  # fex-compat-tool exits ("No compat data path?") without it; FEX keeps its config in <it>/fex-emu
        data = shlex.quote(os.path.join(base, "compatdata"))
        extra = f"mkdir -p {data}\nexport STEAM_COMPAT_DATA_PATH={data}\n" + extra
    text = LINUX_LAUNCH_SH.format(title=title.replace("\n", " "), pkg=pkg, run_dir_q=shlex.quote(run_dir),
                                  exe_q=" ".join(shlex.quote(a) for a in command),
                                  log_q=shlex.quote(os.path.join(base, "launch.log")),
                                  extra_env=extra)
    path = os.path.join(anchor, "launch.sh")
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.chmod(path + ".tmp", 0o755)
    os.replace(path + ".tmp", path)


# ------------------------------------------------------------------------------------------ Linux apps' own icons
# The icon an AppImage or app folder brings (GitHub #99): its .DirIcon, else the Icon= of its .desktop file, looked up
# next to it, in usr/share/icons/hicolor/<size>/apps and usr/share/pixmaps. Copied to <anchor>/artwork/app-icon.<ext>
# and used for the Desktop Mode entry and the Steam shortcut unless the user chose an icon on the PC: the PC writes
# what the art set's icon is into artwork/.icon-source ("custom" = the user's pick or store art, "app" = the app's own,
# "generated" = FramePort's placeholder). Never reads outside the app's folder (symlinks are resolved and checked).
APP_ICON = "app-icon"
ICON_SOURCE = ".icon-source"
APP_ICON_MAX = 4 << 20
APP_ICON_SEND = 1 << 20  # the PNG goes back to the PC (library artwork) up to this size
ICON_EXTS = (".png", ".svg")


def _real_file_in(root, path):
    """The real path of `path` when it is a file inside `root` (symlinks resolved), else None."""
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    if real.startswith(real_root + os.sep) and os.path.isfile(real):
        return real
    return None


def icon_format(path):
    """("png", width) or ("svg", 0) by content, None for anything else (xpm, ico, unreadable)."""
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        return "png", struct.unpack(">I", head[16:20])[0]
    if b"<svg" in head:
        return "svg", 0
    return None


def desktop_fields(path):
    """The [Desktop Entry] group of a .desktop file as {key: value} (localised keys left out)."""
    out, group = {}, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    group = line
                elif group == "[Desktop Entry]" and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    if "[" not in k:
                        out.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    return out


def app_desktop_file(root, appimage):
    """The app's own .desktop file: an AppImage's top-level one, else the first within three folder levels."""
    if appimage:
        found = sorted(glob.glob(os.path.join(glob.escape(root), "*.desktop")))
    else:
        found = []
        for dirpath, dirs, files in os.walk(root):
            depth = 0 if dirpath == root else os.path.relpath(dirpath, root).count(os.sep) + 1
            dirs[:] = sorted(dirs) if depth < 3 else []
            found += [os.path.join(dirpath, n) for n in sorted(files) if n.endswith(".desktop")]
    for path in found:
        if _real_file_in(root, path):
            return path
    return None


def _icon_candidates(base, name):
    """Files that may be the icon called `name` (an Icon= value) under the folder `base`."""
    if not name or os.path.isabs(name) or ".." in name.split("/"):
        return []
    stem = name[:-4] if name.lower().endswith(ICON_EXTS + (".xpm",)) else name
    out = [os.path.join(base, name)] if "/" in name or name != stem else []
    if "/" in name:
        return out
    esc = glob.escape
    for share in (os.path.join(base, "usr", "share"), os.path.join(base, "share")):
        for ext in ICON_EXTS:
            out += glob.glob(os.path.join(esc(share), "icons", "hicolor", "*", "apps", esc(stem) + ext))
            out.append(os.path.join(share, "pixmaps", stem + ext))
    out += [os.path.join(base, stem + ext) for ext in ICON_EXTS]
    return out


def find_app_icon(root, appimage):
    """{"icon": real path or None, "wmclass": the .desktop file's StartupWMClass or None} of an app's folder (an
    AppImage's squashfs-root). The icon: the biggest PNG when it is at least 128 px, else an SVG, else the biggest
    PNG; .DirIcon (an AppImage's own icon, often a symlink) counts like the .desktop file's Icon=."""
    if not os.path.isdir(root):
        return {"icon": None, "wmclass": None}
    desktop = app_desktop_file(root, appimage)
    fields = desktop_fields(desktop) if desktop else {}
    paths = [os.path.join(root, ".DirIcon")] if appimage else []
    bases = [root]
    if desktop:  # a folder app's .desktop file may sit in a subfolder with the icon next to it
        d = os.path.dirname(desktop)
        while d.startswith(root + os.sep):
            bases.append(d)
            d = os.path.dirname(d)
    for base in bases:
        paths += _icon_candidates(base, fields.get("Icon", ""))
    pngs, svgs, seen = [], [], set()
    for p in paths:
        real = _real_file_in(root, p)
        if not real or real in seen:
            continue
        seen.add(real)
        try:
            if os.path.getsize(real) > APP_ICON_MAX:
                continue
        except OSError:
            continue
        fmt = icon_format(real)
        if fmt and fmt[0] == "png":
            pngs.append((fmt[1], real))
        elif fmt:
            svgs.append(real)
    best = max(pngs, default=None, key=lambda t: t[0])
    icon = best[1] if best and best[0] >= 128 else svgs[0] if svgs else best[1] if best else None
    return {"icon": icon, "wmclass": fields.get("StartupWMClass") or None}


def linux_app_root(dep):
    app = os.path.join(dep["base"], "app")
    return os.path.join(app, "squashfs-root") if dep.get("appimage") else app


def ensure_app_icon(dep, anchor, refresh=False):
    """The Linux app's own icon in <anchor>/artwork/app-icon.<ext> (copied from its files when missing, or always with
    `refresh`, e.g. after an install), plus its StartupWMClass: {"icon": path or None, "wmclass": ...}."""
    found = find_app_icon(linux_app_root(dep), dep.get("appimage"))
    art = os.path.join(anchor, "artwork")
    have = sorted(glob.glob(os.path.join(glob.escape(art), APP_ICON + ".*")))
    icon = have[0] if have else None
    if found["icon"] and (refresh or not icon):
        dst = os.path.join(art, f"{APP_ICON}.{icon_format(found['icon'])[0]}")
        os.makedirs(art, exist_ok=True)
        for p in have:
            if p != dst:
                os.remove(p)
        shutil.copyfile(found["icon"], dst + ".tmp")
        os.replace(dst + ".tmp", dst)
        icon = dst
    elif refresh and not found["icon"]:
        for p in have:
            os.remove(p)
        icon = None
    return {"icon": icon, "wmclass": found["wmclass"]}


def icon_source(anchor):
    """What the art set's icon is, as the PC wrote it ("custom", "app", "generated"), or None (older PC app)."""
    try:
        with open(os.path.join(anchor, "artwork", ICON_SOURCE), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def app_icon_for(dep, anchor, steam=False):
    """(icon file, StartupWMClass) for a game's Desktop Mode entry / Steam shortcut: the user's chosen icon (PC:
    "custom") > a Linux app's own icon > the art set's icon (FramePort's placeholder). Steam gets PNGs only."""
    art_icon = next(iter(sorted(glob.glob(os.path.join(glob.escape(anchor), "artwork", "icon.*")))), "")
    if dep.get("kind") != "linux" or not dep.get("base"):
        return art_icon, None
    try:
        own = ensure_app_icon(dep, anchor)
    except OSError:
        return art_icon, None
    if icon_source(anchor) == "custom" and art_icon:
        return art_icon, own["wmclass"]
    if own["icon"] and (not steam or own["icon"].endswith(".png")):
        return own["icon"], own["wmclass"]
    return art_icon, own["wmclass"]


def app_icon_result(icon):
    """finalize_linux's report of the app's own icon; a PNG comes back (base64) for the PC's library artwork."""
    if not icon:
        return None
    out = {"file": os.path.basename(icon), "size": os.path.getsize(icon)}
    if icon.endswith(".png") and out["size"] <= APP_ICON_SEND:
        with open(icon, "rb") as f:
            out["png"] = base64.b64encode(f.read()).decode("ascii")
    return out


# ------------------------------------------------------------------------------------------ Desktop Mode entries
# Linux apps also get an entry in Desktop Mode's application menu and an icon on its desktop (GitHub #84: some apps
# work better with the desktop's mouse and keyboard than in Gaming Mode, where Steam Input owns the controllers).
DESKTOP_APPS = os.path.join(HOME, ".local/share/applications")
DESKTOP_DIR = os.path.join(HOME, "Desktop")
DESKTOP_KEY = "X-FramePort-Package"
GAME_TAGS = {"game", "games", "action", "adventure", "arcade", "casual", "fighting", "platformer", "puzzle", "racing",
             "rhythm", "rpg", "role playing", "shooter", "simulation", "sports", "strategy", "survival", "horror"}


def desktop_value(text):
    """A Desktop Entry string value: one line, backslashes escaped."""
    return re.sub(r"[\r\n\t]", " ", str(text).replace("\\", "\\\\"))


def desktop_exec_arg(arg):
    """One Exec argument (Desktop Entry spec): in double quotes with \\ " ` $ backslash-escaped and % doubled, then
    escaped once more as a string value."""
    quoted = '"' + re.sub(r'([\\"`$])', r"\\\1", str(arg)).replace("%", "%%") + '"'
    return desktop_value(quoted)


def desktop_file_name(pkg):
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", pkg[len("linux."):] if pkg.startswith("linux.") else pkg)
    return f"frameport-{slug.strip('-.') or 'app'}.desktop"


def desktop_entry_text(dep, anchor):
    icon, wmclass = app_icon_for(dep, anchor)
    tags = {str(t).lower() for t in dep.get("tags") or []}
    category = "Game;" if tags & GAME_TAGS else "Utility;"
    lines = ["[Desktop Entry]", "Type=Application", f"Name={desktop_value(dep.get('title') or dep['package'])}",
             "Comment=Installed by FramePort",
             f"Exec=env FRAMEPORT_DESKTOP=1 {desktop_exec_arg(os.path.join(anchor, 'launch.sh'))}",
             f"Path={desktop_value(anchor)}", "Terminal=false", f"Categories={category}"]
    if icon:
        lines.append(f"Icon={desktop_value(icon)}")
    if wmclass:  # KDE's task bar groups the app's window under this entry (and shows its icon)
        lines.append(f"StartupWMClass={desktop_value(wmclass)}")
    lines.append(f"{DESKTOP_KEY}={dep['package']}")
    return "\n".join(lines) + "\n"


def _write_if_changed(path, text, mode):
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text and os.stat(path).st_mode & 0o777 == mode:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path + ".tmp", mode)
    os.replace(path + ".tmp", path)
    return True


def desktop_entries(pkg=None, folder=None):
    """FramePort's .desktop files (of one app, or all) in the menu and desktop folders: {path: package}."""
    out = {}
    for d in [folder] if folder else (DESKTOP_APPS, DESKTOP_DIR):
        for path in sorted(glob.glob(os.path.join(d, "frameport-*.desktop"))):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    m = re.search(rf"^{DESKTOP_KEY}=(.+)$", f.read(), re.M)
            except OSError:
                continue
            if m and (pkg is None or m[1].strip() == pkg):
                out[path] = m[1].strip()
    return out


def write_desktop_entry(pkg):
    """The app's menu entry, plus a copy on ~/Desktop when that folder exists (Plasma starts executable .desktop files
    there without asking). Returns {"menu": path, "desktop": path or None, "changed": bool}."""
    anchor = os.path.join(ANCHORS, pkg)
    dep = deployment(pkg)
    if not dep:
        raise AgentError(f"{pkg} is not installed")
    text = desktop_entry_text(dep, anchor)
    name = desktop_file_name(pkg)
    for path in desktop_entries(pkg):  # the same app's entry under an older file name
        if os.path.basename(path) != name:
            os.remove(path)
    menu = os.path.join(DESKTOP_APPS, name)
    changed = _write_if_changed(menu, text, 0o755)
    desk = None
    if os.path.isdir(DESKTOP_DIR):
        desk = os.path.join(DESKTOP_DIR, name)
        changed = _write_if_changed(desk, text, 0o755) or changed
    return {"menu": menu, "desktop": desk, "changed": changed}


def remove_desktop_entries(pkg=None):
    removed = []
    for path in desktop_entries(pkg):
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def refresh_desktop_entries():
    """Entries for every installed Linux app that wants one (older installs, changed art or titles); entries of apps
    that are gone or switched off are removed. Returns the packages whose entries changed."""
    changed, wanted = [], set()
    for dep_path in sorted(glob.glob(os.path.join(ANCHORS, "*/deployment.json"))):
        try:
            with open(dep_path) as f:
                dep = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(dep, dict) or dep.get("kind") != "linux" or not dep.get("package") \
                or dep.get("desktop_entry", True) is False:
            continue
        wanted.add(dep["package"])
        try:
            if write_desktop_entry(dep["package"])["changed"]:
                changed.append(dep["package"])
        except (OSError, AgentError):
            pass
    for path, pkg in desktop_entries().items():
        if pkg not in wanted:
            os.remove(path)
            changed.append(pkg)
    return sorted(set(changed))


def cmd_desktop_entry(args):
    """Turn a Linux app's Desktop Mode entry on or off (deployment.json keeps the choice)."""
    pkg = check_pkg(args["package"])
    dep = deployment(pkg)
    if not dep or dep.get("kind") != "linux":
        raise AgentError(f"{pkg} is not an installed Linux app")
    on = bool(args.get("enabled", True))
    path = os.path.join(ANCHORS, pkg, "deployment.json")
    with open(path) as f:
        raw = json.load(f)
    raw["desktop_entry"] = on
    with open(path + ".tmp", "w") as f:
        json.dump(raw, f, indent=2)
    os.replace(path + ".tmp", path)
    if on:
        return {"enabled": True, **write_desktop_entry(pkg)}
    return {"enabled": False, "removed": remove_desktop_entries(pkg)}


def cmd_usb_link(args):
    """What the Frame's USB network link (usb0) looks like, for "connect with a USB cable" (read only): its address
    and state, the USB gadget functions behind it (ncm/ecm/rndis: decides which PCs need a driver), the USB speed,
    whether the Frame gives the PC an address (NetworkManager "shared" = DHCP server, dnsmasq) and whether sshd
    answers on it."""
    def sh(cmd):  # a missing tool (nmcli, ss) leaves its field empty instead of failing the whole probe
        try:
            return run(cmd)
        except (OSError, subprocess.SubprocessError):
            return SimpleNamespace(returncode=1, stdout="", stderr="")

    def read(path):
        try:
            return open(path).read().strip()
        except OSError:
            return None
    out = {"present": os.path.isdir("/sys/class/net/usb0")}
    out["operstate"] = read("/sys/class/net/usb0/operstate")
    out["carrier"] = read("/sys/class/net/usb0/carrier")
    out["mac"] = read("/sys/class/net/usb0/address")
    p = sh(["ip", "-j", "addr", "show", "usb0"])
    try:
        out["addresses"] = [f"{a['local']}/{a['prefixlen']}" for i in json.loads(p.stdout or "[]")
                            for a in i.get("addr_info", [])]
    except ValueError:
        out["addresses"] = []
    functions = []
    for g in glob.glob("/sys/kernel/config/usb_gadget/*"):
        functions += [f"{os.path.basename(g)}:{os.path.basename(f)}" for f in glob.glob(os.path.join(g, "functions/*"))]
        out.setdefault("udc", read(os.path.join(g, "UDC")))
        out.setdefault("gadget_ids", f"{read(os.path.join(g, 'idVendor'))}:{read(os.path.join(g, 'idProduct'))}")
    out["functions"] = functions
    out["speed"] = {os.path.basename(u): read(os.path.join(u, "current_speed")) for u in glob.glob("/sys/class/udc/*")}
    nm = sh(["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device"])
    line = next((ln for ln in nm.stdout.splitlines() if ln.startswith("usb0:")), "")
    out["nm"] = line
    conn = line.split(":", 2)[2] if line.count(":") >= 2 else ""
    if conn:
        out["nm_ipv4_method"] = sh(["nmcli", "-g", "ipv4.method", "connection", "show", conn]).stdout.strip()
    out["dhcp_servers"] = [ln for ln in sh(["pgrep", "-a", "dnsmasq|udhcpd|kea|dhcpd"]).stdout.splitlines()][:5]
    out["leases"] = sorted(glob.glob("/var/lib/NetworkManager/dnsmasq-usb0*.leases") +
                           glob.glob("/var/lib/misc/dnsmasq*.leases"))
    neigh = sh(["ip", "-j", "neigh", "show", "dev", "usb0"])
    try:
        out["neighbours"] = [n.get("dst") for n in json.loads(neigh.stdout or "[]")]
    except ValueError:
        out["neighbours"] = []
    out["sshd_listening"] = ":22 " in sh(["ss", "-ltn"]).stdout
    out["devkit_mode"] = os.path.exists("/etc/steamos-devkit-enabled")
    return out


POWER_ACTIONS = {"sleep": "suspend", "restart": "reboot", "shutdown": "poweroff"}


def cmd_power(args):
    """Put the Frame to sleep, restart or shut it down (the app's power button). logind only allows these from the
    user's own units ("yes"), not an SSH session ("challenge" = a password prompt), so a transient user timer runs
    systemctl a few seconds later: this command answers first. A running game is refused unless force."""
    action = args.get("action")
    if action not in POWER_ACTIONS:
        raise AgentError(f"unknown power action {action!r}")
    if game_running() and not args.get("force"):
        raise AgentError("a game is running on the Frame")
    delay = max(2, min(int(args.get("delay", 3)), 60))
    p = run(["systemd-run", "--user", "--collect", "--quiet", f"--unit=frameport-power-{int(time.time())}",
             f"--on-active={delay}", "systemctl", POWER_ACTIONS[action]])
    if p.returncode:
        raise AgentError(f"couldn't {action}: {(p.stderr or p.stdout).strip()[-300:]}")
    return {"action": action, "in_seconds": delay}


AWAKE_UNIT = "frameport-awake"


def cmd_keep_awake(args):
    """Keep the Frame from going idle/asleep while FramePort installs games (on=False ends it). An inhibitor lock held
    by a `sleep` in its own user unit, so it outlives this SSH command and ends by itself after `minutes` if FramePort
    disappears. Blocking sleep needs a local session (polkit inhibit-block-sleep: auth_admin_keep for others), so
    when logind refuses it, idle alone is blocked (allowed for any user)."""
    run(["systemctl", "--user", "stop", f"{AWAKE_UNIT}.service"])
    run(["systemctl", "--user", "reset-failed", f"{AWAKE_UNIT}.service"])
    if not args.get("on", True):
        return {"awake": False}
    seconds = int(min(max(float(args.get("minutes", 60)), 1), 240) * 60)
    for what in ("idle:sleep", "idle"):
        run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={AWAKE_UNIT}", "systemd-inhibit",
             f"--what={what}", "--who=FramePort", "--why=Installing games", "--mode=block", "sleep", str(seconds)])
        time.sleep(0.7)  # a refused lock ends the unit right away
        if run(["systemctl", "--user", "is-active", f"{AWAKE_UNIT}.service"]).stdout.strip() == "active":
            return {"awake": True, "what": what, "seconds": seconds}
        run(["systemctl", "--user", "reset-failed", f"{AWAKE_UNIT}.service"])
    return {"awake": False}


def cmd_set_settings(args):
    pkg = check_pkg(args["package"])
    dep = deployment(pkg) or {}
    if not dep:
        raise AgentError(f"{pkg} is not installed")
    base = dep["base"]
    conf = os.path.join(base, "settings.conf")
    current = {}
    if os.path.exists(conf):
        for line in open(conf):
            k, _, v = line.strip().partition("=")
            if k:
                current[k] = v
    for k, v in (args.get("settings") or {}).items():
        if not re.fullmatch(r"[a-z_]+", k) or not re.fullmatch(r"-?[0-9.]+", str(v)):
            raise AgentError(f"bad setting {k}={v}")
        current[k] = str(v)
    text = "".join(f"{k}={v}\n" for k, v in current.items())
    with open(conf, "w") as f:
        f.write(text)
    files_dir = data_files_dir(base, pkg)
    os.makedirs(files_dir, exist_ok=True)
    with open(os.path.join(files_dir, "framebridge.conf"), "w") as f:
        f.write(text)
    models = install_controller_models(files_dir, current.get("controller_models", "0") not in ("0", "0.0"))
    return {"settings": current, "controller_models": models}


def cmd_uninstall(args):
    pkg = check_pkg(args["package"])
    dep = deployment(pkg)
    if not dep:
        return {"removed": False}
    base = dep["base"]
    pcvr = dep.get("kind") == "pcvr"
    linux = dep.get("kind") == "linux"
    if (pcvr_pids(base) if pcvr or linux else container_running(dep["appid"])):
        raise AgentError("the game is running")
    keep_data = args.get("keep_data", True)
    names = ("game", "revive", "xrlayer", "shadercache", "incoming", "incoming-artwork") if pcvr else \
        ("app", "incoming", "incoming-artwork", "launch.log", "session.log") if linux else \
        ("lepton-app", "lepton-shaders", "incoming", "previous-game.apk")
    for name in names:
        p = os.path.join(base, name)
        if os.path.isdir(p):
            remove_tree(p)
        elif os.path.exists(p):
            os.remove(p)
    if not keep_data:
        remove_tree(base)
        remove_empty_drive_dir(base)
    if linux:
        remove_desktop_entries(pkg)
    anchor = os.path.join(ANCHORS, pkg)
    removed_sc = False
    if args.get("remove_shortcut") and steam_users():
        # Steam keeps its own copy of shortcuts.vdf and writes it back: change it only with Steam closed (worker)
        removed_sc = cmd_shortcuts({"remove": [{"exe": f'"{anchor}/launch.sh"', "appid": dep.get("appid")}]})["started"]
    if args.get("remove_shortcut"):
        try:
            devkit_unregister(pkg, steam_running=run(["pgrep", "-x", "steam"]).returncode == 0)
        except Exception:  # noqa: BLE001 (the files go anyway at the next purge)
            pass
    if not keep_data or base != anchor:
        remove_tree(anchor)
    else:  # saves live next to the launcher (Quest games): keep them, drop what marks the game as installed
        for name in ("deployment.json", "launch.sh", "artwork", "launch.log", "launch-test.log", "session.log"):
            p = os.path.join(anchor, name)
            remove_tree(p)
    return {"removed": True, "kept_saves": keep_data, "shortcut_removed": removed_sc}


# ------------------------------------------------------------------------------------------ move (GitHub #90)
def remove_empty_drive_dir(base):
    """<drive>/FramePort once its last game is gone."""
    parent = os.path.dirname(os.path.normpath(base))
    if os.path.basename(parent) != DRIVE_DIR or _inside(parent, HOME):
        return False
    try:
        os.rmdir(parent)
        return True
    except OSError:
        return False


def base_items(base, anchor):
    """The entries of a game's base that are the game's files (with base == anchor, the anchor's own files stay)."""
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    if os.path.normpath(base) == os.path.normpath(anchor):
        names = [n for n in names if n not in ANCHOR_ITEMS]
    return names


def as_owner():
    """Commands that must reach files owned by Lepton containers' user ids (overlayfs work dirs, the app's own
    files) run in podman's user namespace, where the steamos user owns them all."""
    return ["podman", "unshare"] if shutil.which("podman") else []


def tree_stats(root, names):
    """(files, bytes) of these entries of root: regular files and symlinks count (symlinks without size)."""
    paths = [os.path.join(root, n) for n in names if os.path.lexists(os.path.join(root, n))]
    if not paths:
        return 0, 0
    p = run(as_owner() + ["find"] + paths + ["(", "-type", "f", "-o", "-type", "l", ")", "-printf", r"%y %s\n"])
    files = size = 0
    for line in p.stdout.splitlines():
        kind, _, n = line.partition(" ")
        files += 1
        if kind == "f" and n.isdigit():
            size += int(n)
    return files, size


def replace_path(text, old, new):
    """launch.sh text with the game's old folder replaced (the plain path and Wine's Z:\\ form). Paths that need
    quoting differently can't be swapped as text: those launchers are written again instead."""
    if "'" in new or (shlex.quote(old) == old and shlex.quote(new) != new):
        raise AgentError(f"can't rewrite the launcher for {new!r}; reinstall the game once, then move it")
    for o, n in ((old, new), (windows_path(old), windows_path(new))):
        text = re.sub(re.escape(o) + r"(?=[/\\'\"\s;)|&]|$)", lambda m, n=n: n, text, flags=re.M)
    return text


def rebase_launcher(dep, anchor, new):
    """launch.sh pointing at the game's new folder: the Quest launcher's app_dir line, Linux and PC VR launchers written
    again from the install record (their settings are kept there since agent v63), else the path swapped as text."""
    pkg, old = dep["package"], dep["base"]
    path = os.path.join(anchor, "launch.sh")
    with open(path) as f:
        text = f.read()
    kind = dep.get("kind", "quest")
    launcher = dep.get("launcher")
    if kind == "quest":
        text, n = re.subn(r"^app_dir=.*$", lambda m: "app_dir=" + shlex.quote(new), text, count=1, flags=re.M)
        if not n:
            raise AgentError("launch.sh has no app_dir line; reinstall the game once, then move it")
    elif kind == "linux" and launcher is not None:
        prefix = []
        if dep.get("x86_64"):
            tool = pick_tool("linux_x86", linux_x86_tools())
            prefix = compat_command(tool["dir"]) if tool and tool.get("dir") else None
        if prefix is not None:
            write_linux_launcher(anchor, new, pkg, dep.get("title") or pkg, dep["exe"], dep.get("appimage"), prefix,
                                 launcher.get("env"))
            return
        text = replace_path(text, old, new)
    elif kind == "pcvr" and launcher is not None:
        tool = next((t for t in proton_tools() if t["name"] == dep.get("proton")), None)
        if tool and tool.get("dir"):
            write_proton_launcher(anchor, new, pkg, dep.get("title") or pkg, dep["appid"], tool, dep["exe"],
                                  dep.get("revive", True), launcher.get("env"), dep.get("xr_layer"),
                                  launcher.get("game_args") or [], dep.get("oculus_hmd"), vr=dep.get("vr", True))
            return
        text = replace_path(text, old, new)
    else:
        text = replace_path(text, old, new)
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.chmod(path + ".tmp", 0o755)
    if shutil.which("bash") and run(["bash", "-n", path + ".tmp"]).returncode != 0:
        os.remove(path + ".tmp")
        raise AgentError("the rewritten launcher isn't valid; nothing was moved")
    os.replace(path + ".tmp", path)


def retarget_symlinks(root, names, old, new):
    """Symlinks in the moved files that point into the old folder (PC VR: the LibOVRRT redirect to Revive's runtime,
    links in the Proton prefix) point into the new one."""
    fixed = 0
    for name in names:
        for r, dirs, files in os.walk(os.path.join(root, name)):
            for n in files + dirs:
                p = os.path.join(r, n)
                if not os.path.islink(p):
                    continue
                target = os.readlink(p)
                if os.path.isabs(target) and _inside(target, old):
                    try:
                        os.remove(p)
                        os.symlink(new + target[len(os.path.normpath(old)):], p)
                        fixed += 1
                    except OSError:
                        pass
    return fixed


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=2)
    os.replace(path + ".tmp", path)


def same_device(a, b):
    return os.stat(a).st_dev == os.stat(b).st_dev


def game_is_running(dep):
    if dep.get("kind") in ("pcvr", "linux"):
        return bool(pcvr_pids(dep["base"]))
    return container_running(dep["appid"])


def cmd_move(args):
    """Move an installed game's files to another drive (or back to internal storage): `dest` as for prepare (a drive's
    FramePort folder or its mount point, "internal"). The launcher, Steam shortcut and saves stay valid. Copies run
    detached (poll move_status); detach=False runs it here (tests)."""
    pkg = check_pkg(args["package"])
    dep = deployment(pkg)
    if not dep:
        raise AgentError(f"{pkg} is not installed")
    title = dep.get("title") or pkg
    old = os.path.normpath(dep["base"])
    check_base_present(old, title)
    dest = resolve_dest(args.get("dest"))
    new = os.path.normpath(os.path.join(dest, pkg))
    anchor = os.path.join(ANCHORS, pkg)
    if new == old:
        raise AgentError(f"{title} is already there")
    if game_is_running(dep):
        raise AgentError(f"{title} is running on the Frame. Close it first.")
    try:
        cur = json.load(open(MOVE_STATUS))
        if cur.get("state") == "running" and cur.get("pid") and os.path.exists(f"/proc/{cur['pid']}"):
            raise AgentError(f"another move is running ({cur.get('title') or cur.get('package')})")
    except (OSError, ValueError):
        pass
    items = base_items(old, anchor)
    clash = [n for n in items if os.path.lexists(os.path.join(new, n))]
    if new != anchor and os.path.isdir(new) and os.listdir(new):
        clash = clash or sorted(os.listdir(new))
    if clash:
        raise AgentError(f"{new} already has files ({clash[0]}); remove them first")
    os.makedirs(dest, exist_ok=True)
    same_fs = same_device(old, dest)
    files, size = tree_stats(old, items)
    if not same_fs:
        free = _space(dest)[0] or 0
        if size > free - MOVE_HEADROOM:
            raise AgentError(f"not enough space: {title} needs {size / 2**30:.1f} GiB, the drive has "
                             f"{free / 2**30:.1f} GiB free")
    job = {"package": pkg, "title": title, "from": old, "to": new, "items": items, "same_fs": same_fs,
           "files": files, "total_bytes": size, "status": args.get("status") or MOVE_STATUS}
    _write_json(job["status"], {"state": "running", "phase": "starting", "package": pkg, "title": title, "from": old,
                                "to": new, "done_bytes": 0, "total_bytes": size, "started": time.time()})
    if args.get("detach", True) is False:
        move_worker(json.dumps(job))
        return json.load(open(job["status"]))
    p = run(["systemd-run", "--user", "--collect", "--quiet", f"--unit=frameport-move-{int(time.time())}",
             "--setenv=HOME=" + HOME, sys.executable, os.path.abspath(__file__), "_move_worker", json.dumps(job)])
    if p.returncode != 0:
        _write_json(job["status"], {"state": "failed", "package": pkg, "error": p.stderr.strip()[-300:]})
        raise AgentError(f"couldn't start the move: {p.stderr.strip()[-300:]}")
    return {"started": True, "package": pkg, "from": old, "to": new, "total_bytes": size, "files": files,
            "same_drive": same_fs, "status": job["status"]}


def cmd_move_status(args):
    try:
        return json.load(open(args.get("status") or MOVE_STATUS))
    except (OSError, ValueError):
        return {"state": "none"}


def move_worker(payload):
    job = json.loads(payload)
    status = {"state": "running", "phase": "copying", "package": job["package"], "title": job["title"],
              "from": job["from"], "to": job["to"], "done_bytes": 0, "total_bytes": job["total_bytes"],
              "started": time.time(), "pid": os.getpid()}
    _write_json(job["status"], status)
    try:
        status.update(_move(job, status))
        status["state"] = "done"
    except Exception as exc:  # noqa: BLE001
        status.update(state="failed", error=str(exc) if isinstance(exc, AgentError) else f"{type(exc).__name__}: {exc}")
    status.update(finished=time.time(), phase=status["state"])
    _write_json(job["status"], status)


def _move(job, status):
    import threading

    pkg, old, new, items = job["package"], job["from"], job["to"], job["items"]
    anchor = os.path.join(ANCHORS, pkg)
    dep = deployment(pkg)
    if not dep or os.path.normpath(dep["base"]) != old:
        raise AgentError("the game's install record changed; nothing was moved")
    os.makedirs(new, exist_ok=True)
    done = []
    try:
        if job["same_fs"]:  # same drive: rename, nothing to copy
            for n in items:
                os.rename(os.path.join(old, n), os.path.join(new, n))
                done.append(n)
        else:
            stop = threading.Event()

            def watch():  # progress for the PC: what has arrived so far
                while not stop.wait(3):
                    arrived = [n for n in items if os.path.lexists(os.path.join(new, n))]
                    status["done_bytes"] = tree_stats(new, arrived)[1]
                    _write_json(job["status"], status)

            t = threading.Thread(target=watch, daemon=True)
            t.start()
            try:
                for n in items:
                    done.append(n)
                    p = run(as_owner() + ["cp", "-a", "--", os.path.join(old, n), new + "/"])
                    if p.returncode != 0:
                        raise AgentError(f"copy failed: {(p.stderr or p.stdout).strip()[-300:]}")
            finally:
                stop.set()
                t.join(5)
            status["phase"] = "verifying"
            _write_json(job["status"], status)
            have = tree_stats(new, items)
            if have != (job["files"], job["total_bytes"]):
                raise AgentError(f"the copy doesn't match ({have[0]} files, {have[1]} bytes; expected "
                                 f"{job['files']} files, {job['total_bytes']} bytes)")
            status["done_bytes"] = job["total_bytes"]
        status["relinked"] = retarget_symlinks(new, items, old, new)
        old_launcher = open(os.path.join(anchor, "launch.sh")).read()
        rebase_launcher(dep, anchor, new)
    except BaseException:
        if job["same_fs"]:
            for n in reversed(done):
                try:
                    os.rename(os.path.join(new, n), os.path.join(old, n))
                except OSError:
                    pass
        else:
            for n in done:
                _remove_owned(os.path.join(new, n))
        if new != anchor:
            try:
                os.rmdir(new)
            except OSError:
                pass
            remove_empty_drive_dir(new)
        raise
    dep_path = os.path.join(anchor, "deployment.json")
    try:
        with open(dep_path) as f:
            raw = json.load(f)
        raw.update(base=new, moved={"from": old, "time": time.time()})
        _write_json(dep_path, raw)
    except Exception:
        with open(os.path.join(anchor, "launch.sh"), "w") as f:  # keep the game where it was
            f.write(old_launcher)
        raise
    status["phase"] = "removing"
    _write_json(job["status"], status)
    if not job["same_fs"]:
        for n in items:
            _remove_owned(os.path.join(old, n))
    if old != os.path.normpath(anchor):
        try:
            os.rmdir(old)
        except OSError:
            pass
        remove_empty_drive_dir(old)
    return {"base": new}


def _remove_owned(path):
    """Remove a copied or moved tree, including files owned by the container's user ids."""
    remove_tree(path)
    if os.path.lexists(path) and as_owner():
        run(as_owner() + ["rm", "-rf", "--", path])


# ------------------------------------------------------------------------------------------ launch tests
def cmd_stop(args):
    dep = deployment(check_pkg(args["package"]))
    if dep:
        run(["systemctl", "--user", "stop", f"frameport-test-{dep['appid']}"])
        if dep.get("kind") == "pcvr":
            stop_pcvr(dep)
        elif dep.get("kind") == "linux":
            for pid in pcvr_pids(dep["base"]):
                run(["kill", "-TERM", pid])
        else:
            run(["podman", "kill", f"lepton-steamlaunch-{dep['appid']}"])
    return {"stopped": bool(dep)}


def stop_pcvr(dep):
    """Stop a Proton game: wineserver -k in its prefix, then anything still using its folder."""
    tool = next((t for t in proton_tools() if t["name"] == dep.get("proton")), None) if dep.get("proton") else None
    if tool and tool.get("dir"):
        stop_prefix(tool, os.path.join(dep["base"], "compatdata/pfx"))
    for pid in pcvr_pids(dep["base"]):
        run(["kill", "-TERM", pid])


PCVR_LOGS = ("compatdata/pfx/drive_c/users/steamuser/AppData/Local/Revive/ReviveInjector.txt",)
LOCAL_APPDATA = "compatdata/pfx/drive_c/users/steamuser/AppData/Local"


LOCAL_LOW = "compatdata/pfx/drive_c/users/steamuser/AppData/LocalLow"


def _head_tail(path, head=300, tail=1500):
    lines = open(path, errors="replace").read().splitlines()
    if len(lines) > head + tail:
        lines = lines[:head] + [f"[... {len(lines) - head - tail} lines left out ...]"] + lines[-tail:]
    return "\n".join(lines)


def unity_logs(base, since=0.0):
    """Unity's own logs of a PC VR game: LocalLow/<Company>/<Product>/Player.log + Player-prev.log (Unity 2018.3+;
    company and product from <game>/<Name>_Data/app.info, else every Player log in LocalLow), the older
    <Name>_Data/output_log.txt and the crash handler's Temp/<Company>/<Product>/Crashes/*/error.log. Unity logs VR
    start-up (which SDK, init errors) at the top, so the head is kept as well as the tail."""
    game = os.path.join(base, "game")
    low = os.path.join(base, LOCAL_LOW)
    temp = os.path.join(base, LOCAL_APPDATA, "Temp")
    names = []
    for info in glob.glob(os.path.join(glob.escape(game), "*_Data", "app.info")):
        try:
            lines = [ln.strip() for ln in open(info, errors="replace").read().splitlines()]
        except OSError:
            continue
        if len(lines) >= 2 and lines[0] and lines[1] and "/" not in lines[0] + lines[1] and \
                ".." not in (lines[0], lines[1]):
            names.append((lines[0], lines[1]))
    dirs = [os.path.join(low, c, p) for c, p in names if os.path.isdir(os.path.join(low, c, p))]
    logs = []
    for d in dirs or glob.glob(os.path.join(glob.escape(low), "*", "*")):
        logs += [os.path.join(d, n) for n in ("Player.log", "Player-prev.log")]
    logs += glob.glob(os.path.join(glob.escape(game), "*_Data", "output_log.txt"))
    crash_dirs = [os.path.join(temp, c, p) for c, p in names] or glob.glob(os.path.join(glob.escape(temp), "*", "*"))
    crashes = [x for d in crash_dirs for x in glob.glob(os.path.join(glob.escape(d), "Crashes", "*", "error.log"))]
    out = []
    for log in [x for x in logs if os.path.isfile(x)]:
        if os.path.getmtime(log) < since:  # left over from an earlier run
            continue
        out.append(f"===== unity log {os.path.relpath(log, base)}\n" + _head_tail(log))
    for err in sorted(crashes, key=os.path.getmtime, reverse=True)[:2]:
        if os.path.getmtime(err) >= since:
            out.append(f"===== unity crash {os.path.relpath(err, base)}\n" + _head_tail(err, 100, 400))
    return out


def game_logs(base, since=0.0):
    """The game's own logs from the Proton prefix, newest first: Unreal Saved/Logs/*.log (tail) and crash summaries
    (Saved/Crashes/*/CrashContext.runtime-xml → error message + call stack), Unity's Player.log / crash error.log
    (agent v67), Revive's logs."""
    out = unity_logs(base, since)
    local = os.path.join(base, LOCAL_APPDATA)
    for log in sorted(glob.glob(os.path.join(local, "*", "Saved", "Logs", "*.log")), key=os.path.getmtime,
                      reverse=True)[:1]:
        if os.path.getmtime(log) < since:  # left over from an earlier run
            continue
        text = open(log, errors="replace").read()
        out.append(f"===== game log {os.path.relpath(log, base)}\n" + "\n".join(text.splitlines()[-1500:]))
    for ctx in sorted(glob.glob(os.path.join(local, "*", "Saved", "Crashes", "*", "CrashContext.runtime-xml")),
                      key=os.path.getmtime, reverse=True)[:2]:
        if os.path.getmtime(ctx) < since:
            continue
        raw = open(ctx, "rb").read().decode("utf-8", "replace")
        fields = []
        for tag in ("ErrorMessage", "CrashType", "EngineVersion", "CallStack", "SourceContext"):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", raw, re.S)
            if m and m.group(1).strip():
                fields.append(f"{tag}: {m.group(1).strip()[:3000]}")
        out.append(f"===== crash {os.path.relpath(os.path.dirname(ctx), base)} (UE4CC)\n" + "\n".join(fields))
    for rv in glob.glob(os.path.join(local, "Revive", "*.txt")):
        if not rv.endswith("ReviveInjector.txt") and os.path.getmtime(rv) >= since:
            out.append(f"===== {os.path.relpath(rv, base)}\n" + open(rv, errors="replace").read()[-100000:])
    return out


LAUNCH_INSTALL_GRACE = 240  # s a launch test waits at most for Lepton's boot + app install before its own window


def cmd_launch_test(args):
    """Start the game headless (as Steam would), wait, classify, stop. Without the headset worn the OpenXR session
    never reaches FOCUSED, so this proves startup, not visuals."""
    pkg = check_pkg(args["package"])
    seconds = int(args.get("seconds", 45))
    dep = deployment(pkg)
    if not dep:
        raise AgentError(f"{pkg} is not installed")
    anchor = os.path.join(ANCHORS, pkg)
    log = os.path.join(dep["base"], "launch.log")
    appid = dep["appid"]
    if dep.get("kind") == "pcvr":
        return launch_test_pcvr(dep, anchor, log, seconds)
    if dep.get("kind") == "linux":
        return launch_test_linux(dep, anchor, log, seconds)
    if container_running(appid):
        raise AgentError("the game is already running")
    ensure_host_fixes()
    keys = key_usage()
    unit = f"frameport-test-{appid}"
    run(["systemctl", "--user", "reset-failed", unit])
    mark_launch_test(anchor)
    p = run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}", os.path.join(anchor, "launch.sh")])
    if p.returncode:
        raise AgentError("could not start the launcher: " + p.stderr[-300:])
    start = time.time()
    state = "RUNNING"
    app_start = None  # when Lepton started the app ("Waiting for app"): the test window counts from there
    while True:
        time.sleep(3)
        now = time.time()
        text = open(log, errors="replace").read() if os.path.exists(log) else ""
        if "Exited!" in text:
            state = "EXITED"
            break
        if "Early-exit" in text or not_started(text, now - start):
            state = "NEVER_STARTED"
            break
        if app_start is None and "Waiting for app" in text:
            app_start = now
        # the first start after an APK change boots Lepton and installs the app first (a minute or more): stopping the
        # container then left a half-installed APK ("base.apk is not zip") that never started again (VR HOT)
        if app_start is not None and now - app_start >= seconds:
            break
        if now - start >= seconds + LAUNCH_INSTALL_GRACE:
            break
    elapsed = round(time.time() - start)
    if state == "EXITED":  # Lepton dumps the container's logcat buffers (crash backtraces) after "Exited!"
        until = time.time() + 15
        while time.time() < until and "Dumping logcat" not in (open(log, errors="replace").read()
                                                                if os.path.exists(log) else ""):
            time.sleep(1)
        time.sleep(2)
    run(["systemctl", "--user", "stop", unit])
    run(["podman", "kill", f"lepton-steamlaunch-{appid}"])
    time.sleep(3)
    crash = os.path.join(STEAM, "logs", "lepton-logcats", f"steamlaunch-{appid}", "logcat-crash.log")
    fresh = os.path.exists(crash) and os.path.getmtime(crash) >= start - 1 and os.path.getsize(crash) > 0
    return {"state": state, "elapsed": elapsed, "log": log,
            "log_size": os.path.getsize(log) if os.path.exists(log) else 0,
            "crash_log": crash if fresh else None,
            "kernel_keys_before": keys, "kernel_keys_after": key_usage()}


def launch_test_linux(dep, anchor, log, seconds):
    """Start a Linux app headless (the launcher borrows Steam's display) and see whether it stays up."""
    base, appid = dep["base"], dep["appid"]
    if pcvr_pids(os.path.join(base, "app")):
        raise AgentError("the app is already running")
    unit = f"frameport-test-{appid}"
    run(["systemctl", "--user", "reset-failed", unit])
    mark_launch_test(anchor)
    start = time.time()
    p = run(["systemd-run", "--user", "--quiet", f"--unit={unit}", "--property=RemainAfterExit=no",
             os.path.join(anchor, "launch.sh")])
    if p.returncode:
        raise AgentError("could not start the launcher: " + p.stderr[-300:])
    state, seen = "RUNNING", False
    while time.time() - start < seconds:
        time.sleep(2)
        seen = seen or bool(pcvr_pids(os.path.join(base, "app")))
        if run(["systemctl", "--user", "is-active", "--quiet", unit]).returncode:
            state = "EXITED" if seen else "NEVER_STARTED"
            break
    elapsed = round(time.time() - start)
    run(["systemctl", "--user", "stop", unit])
    for pid in pcvr_pids(os.path.join(base, "app")):
        run(["kill", "-TERM", pid])
    combined = os.path.join(base, "launch-test.log")
    text = open(log, errors="replace").read()[-400000:] if os.path.exists(log) else ""
    missing = dep.get("missing_libraries") or []
    with open(combined, "w") as f:
        if missing:
            f.write("FramePort: libraries missing on the Frame: " + ", ".join(missing) + "\n")
        f.write(text)
    return {"state": state, "elapsed": elapsed, "log": combined, "log_size": os.path.getsize(combined),
            "kind": "linux", "game_process": seen, "missing_libraries": missing}


def launch_test_pcvr(dep, anchor, log, seconds):
    base, appid = dep["base"], dep["appid"]
    if pcvr_pids(base):
        raise AgentError("the game is already running")
    unit = f"frameport-test-{appid}"
    run(["systemctl", "--user", "reset-failed", unit])
    mark_launch_test(anchor)
    p = run(["systemd-run", "--user", "--quiet", f"--unit={unit}", "--property=RemainAfterExit=no",
             os.path.join(anchor, "launch.sh")])
    if p.returncode:
        raise AgentError("could not start the launcher: " + p.stderr[-300:])
    start = time.time()
    state = "RUNNING"
    game_seen = False
    exe_name = os.path.basename(dep.get("exe", ""))
    while time.time() - start < seconds:
        time.sleep(3)
        # Wine names game processes by their Windows path (X:\...\Game.exe); the Proton command line doesn't start so
        game_seen = game_seen or bool(exe_name and run(
            ["pgrep", "-if", r"^[a-z]:\\.*" + re.escape(exe_name)]).returncode == 0)
        active = run(["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0
        if not active:
            state = "EXITED" if game_seen else "NEVER_STARTED"
            break
    elapsed = round(time.time() - start)
    if state == "RUNNING" and not game_seen:
        state = "NEVER_STARTED"  # the launcher is up but the game process never appeared
    run(["systemctl", "--user", "stop", unit])
    stop_pcvr(dep)
    time.sleep(3)
    # one log for triage: launcher/Proton output + Revive's logs + the game's own (Unreal) log and crash summaries +
    # Proton's own log when enabled
    parts = []
    for rel in ("launch.log",) + PCVR_LOGS + (f"steam-{appid}.log",):
        path = os.path.join(base, rel)
        if os.path.exists(path) and os.path.getmtime(path) >= start - 5:  # not left over from an earlier run
            parts.append(f"===== {rel}\n" + open(path, errors="replace").read()[-400000:])
    parts += game_logs(base, since=start - 5)
    combined = os.path.join(base, "launch-test.log")
    with open(combined, "w") as f:
        f.write("\n".join(parts))
    return {"state": state, "elapsed": elapsed, "log": combined, "log_size": os.path.getsize(combined),
            "kind": "pcvr", "game_process": bool(game_seen)}


SESSION_LOG_MAX = 4 << 20  # bytes of a play session's log the PC triages
SESSION_READ_MAX = 64 << 20  # a longer launch.log is read from its end
SESSION_KEEP = re.compile(r"FrameBridge|focus|pacing|Fatal signal|FATAL|CRASH|#\d\d pc |Abort message|DEVICE.LOST|"
                          r"AndroidRuntime|vrclient|Start proc|lepton", re.I)
KERNEL_GPU = re.compile(r"hangcheck|gpu fault|adreno|kgsl|msm_drm.*(hang|recover)", re.I)


def slice_session_log(text, max_bytes=SESSION_LOG_MAX):
    """A long play session's log cut to max_bytes: its start (a quarter) and its end (half) whole, from the middle
    only FrameBridge's, focus, pacing and crash lines (oldest first, while they fit). Returns (text, cut)."""
    if len(text) <= max_bytes:
        return text, False
    head = text[:max_bytes // 4]
    head = head[:head.rfind("\n") + 1]
    tail = text[-(max_bytes // 2):]
    tail = tail[tail.find("\n") + 1:]
    middle = text[len(head):len(text) - len(tail)]
    budget, kept = max_bytes - len(head) - len(tail) - 200, []
    for line in middle.splitlines():
        if SESSION_KEEP.search(line):
            budget -= len(line) + 1
            if budget < 0:
                break
            kept.append(line)
    note = f"[FramePort: {len(middle)} bytes in the middle of this session cut, {len(kept)} lines kept]\n"
    return head + note + "".join(ln + "\n" for ln in kept) + tail, True


def session_kernel_lines(start, end):
    """Kernel GPU lines (hangs, faults, recoveries) logged during a play session."""
    until = (end or time.time()) + 120
    try:
        text = run(["journalctl", "-k", "--since", f"@{int(start)}", "--until", f"@{int(until)}", "-q", "--no-pager",
                    "-o", "short-unix"]).stdout
    except OSError:
        return ""
    lines = [ln for ln in text.splitlines() if KERNEL_GPU.search(ln)]
    return "".join(f"kernel: {ln}\n" for ln in lines[-200:])


def cmd_session_log(args):
    """The log of the game's newest play session (agent v70), for triage on the PC: {session: {start, end, test},
    log: path of the session's log (launch.log sliced to 4 MB; PC VR: + Revive's and the game's own logs), log_size,
    cut, crash: that session's crash logcat (tombstones), kernel: GPU hang/fault lines from the kernel log}.
    session is None when the game was never played."""
    pkg = check_pkg(args["package"])
    dep = deployment(pkg)
    if not dep:
        raise AgentError(f"{pkg} is not installed")
    anchor, base = os.path.join(ANCHORS, pkg), dep["base"]
    session = last_play(anchor)
    out = {"session": session, "log": None, "log_size": 0, "cut": False, "crash": "", "kernel": "",
           "kind": dep.get("kind", "quest")}
    if not session:
        return out
    start, end = session["start"], session["end"]
    parts = []
    log = os.path.join(base, "launch.log")
    if os.path.exists(log) and os.path.getmtime(log) >= start - 5:  # else no log of this session is left
        size = os.path.getsize(log)
        with open(log, "rb") as f:
            if size > SESSION_READ_MAX:
                f.seek(size - SESSION_READ_MAX)
            parts.append(f.read().decode("utf-8", "replace"))
    if dep.get("kind") == "pcvr":
        for rel in PCVR_LOGS:
            path = os.path.join(base, rel)
            if os.path.exists(path) and os.path.getmtime(path) >= start - 5:
                parts.append(f"===== {rel}\n" + (_tail(path, 400000) or ""))
        parts += game_logs(base, since=start - 5)
    text, cut = slice_session_log("\n".join(parts), int(args.get("max_bytes", SESSION_LOG_MAX)))
    path = os.path.join(base, "session.log")
    with open(path, "w") as f:
        f.write(text)
    out.update(log=path, log_size=os.path.getsize(path), cut=cut)
    crash = os.path.join(STEAM, "logs", "lepton-logcats", f"steamlaunch-{dep['appid']}", "logcat-crash.log")
    try:
        mtime = os.path.getmtime(crash)
        if start - 1 <= mtime <= (end or time.time()) + 120:
            out["crash"] = _tail(crash, 256 * 1024) or ""
    except OSError:
        pass
    out["kernel"] = session_kernel_lines(start, end)
    return out


def steam_library_report():
    """Why a game installed by FramePort may be missing from Steam or fail with "Game configuration unavailable"
    (GitHub #21/#30): where Steam really lives, Steam's client version/beta, each account's shortcuts.vdf (when it
    was written vs when Steam started, FramePort's entries in it) and Steam's log lines about these shortcuts.
    Accounts are numbered, not named (the PC redacts ids anyway)."""
    rep = {"steam_dir": STEAM}
    for link in ("~/.steam/steam", "~/.steam/root"):
        path = os.path.expanduser(link)
        rep[link] = os.path.realpath(path) if os.path.lexists(path) else None
    pkg_dir = os.path.join(STEAM, "package")
    rep["beta"] = (_tail(os.path.join(pkg_dir, "beta"), 200) or "").strip() or None
    rep["client_manifests"] = sorted(n for n in os.listdir(pkg_dir) if n.endswith(".manifest"))[:10] \
        if os.path.isdir(pkg_dir) else []
    p = run(["pgrep", "-o", "-x", "steam"])
    pid = p.stdout.split()[0] if p.returncode == 0 and p.stdout.split() else None
    rep["steam_started"] = os.stat(f"/proc/{pid}").st_mtime if pid and os.path.exists(f"/proc/{pid}") else None
    if pid:
        try:
            rep["steam_cmdline"] = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()[:500]
        except OSError:
            pass
    ours = {}
    for dep_path in glob.glob(os.path.join(ANCHORS, "*/deployment.json")):
        try:
            dep = json.load(open(dep_path))
        except (OSError, ValueError):
            continue
        ours[f'"{os.path.dirname(dep_path)}/launch.sh"'] = dep.get("package")
    active = active_steam_user()
    accounts, appids = [], set()
    for i, user in enumerate(steam_users()):
        acc = {"account": i + 1, "most_recent_login": user == active}
        cfg = os.path.join(STEAM, "userdata", user, "config")
        vdf = os.path.join(cfg, "shortcuts.vdf")
        acc["shortcuts_vdf_written"] = os.path.getmtime(vdf) if os.path.exists(vdf) else None
        acc["localconfig_written"] = os.path.getmtime(os.path.join(cfg, "localconfig.vdf")) \
            if os.path.exists(os.path.join(cfg, "localconfig.vdf")) else None
        acc["backups"] = len(glob.glob(vdf + ".backup-*"))
        try:
            root = vdf_decode(open(vdf, "rb").read()) if os.path.exists(vdf) else {}
            entries = [v for v in (root.get("shortcuts") or {}).values() if isinstance(v, dict)]
            acc["shortcuts"] = len(entries)
            acc["frameport"] = []
            for v in entries:
                if v.get("Exe") in ours:
                    appids.add(v.get("appid", 0) & 0xFFFFFFFF)
                    acc["frameport"].append({"package": ours[v["Exe"]], "appid": v.get("appid", 0) & 0xFFFFFFFF,
                                             "title": v.get("AppName", v.get("appname")),
                                             "start_dir": v.get("StartDir"), "openvr": v.get("OpenVR"),
                                             "options": v.get("LaunchOptions")})
            acc["exe_dupes"] = len(entries) - len({v.get("Exe") for v in entries})
        except Exception as exc:  # noqa: BLE001
            acc["error"] = f"shortcuts.vdf unreadable: {exc}"
        accounts.append(acc)
    rep["accounts"] = accounts
    rep["devkit_games"] = sorted(n for n in os.listdir(DEVKIT_GAMES) if os.path.isdir(os.path.join(DEVKIT_GAMES, n))) \
        if os.path.isdir(DEVKIT_GAMES) else []
    lines = []
    shortcut_log = _tail(os.path.join(STEAM, "logs/shortcuts.previous.txt"), 200000)
    if shortcut_log:
        lines += ["==> shortcuts.previous.txt", shortcut_log]
    for name in ("console_log.previous.txt", "console_log.txt"):
        text = _tail(os.path.join(STEAM, "logs", name), 8 << 20) or ""
        keep = [ln for ln in text.splitlines() if "launch error" in ln or "rungameid" in ln
                or ("shortcut" in ln.lower() and "path_shortcut" not in ln) or any(f"[AppID {a}" in ln for a in appids)]
        if keep:
            lines += [f"==> {name}"] + keep[-400:]
    return rep, "\n".join(lines)


def _tail(path, max_bytes):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        text = data.decode("utf-8", "replace")
        return (f"[... first {size - max_bytes} bytes cut ...]\n" if size > max_bytes else "") + text
    except OSError:
        return None


def cmd_collect_diag(args):
    """Everything useful for debugging without the game or the PC app: host runtime facts and, with a package, the
    game's launcher, settings, deployment, logs (launch, Lepton logcat, Proton/Revive/Unreal) and its file listing.
    Returns {"host": {...}, "files": {name: text}, "listing": ...}; the PC redacts and zips it."""
    max_bytes = int(args.get("max_bytes", 2 << 20))
    files, host = {}, {}
    host["openxr_runtime"] = openxr_runtime()
    layers = os.path.join(HOME, ".local/share/openxr/1/api_layers/explicit.d")
    host["openxr_layers"] = sorted(os.listdir(layers)) if os.path.isdir(layers) else []
    host["kernel_keys"] = key_usage()
    host["containers_conf"] = _tail(CONTAINERS_CONF, 20000)
    host["podman"] = run(["podman", "ps", "-a", "--filter", "name=lepton", "--format",
                          "{{.Names}} {{.Status}}"]).stdout[-20000:]
    host["steam_running"] = run(["pgrep", "-x", "steam"]).returncode == 0
    host["uptime"] = _tail("/proc/uptime", 200)
    host["boot"] = boot_state()
    # how the previous boot ended: errors and kernel (GPU/msm/kgsl, OOM, panic) warnings before a crash or reset; and
    # this boot's kernel warnings (a GPU hang the Frame recovered from only ends the game: no reboot, no tombstone)
    for name, boot, extra in (("previous-boot-errors.txt", "-1", ["-p", "err"]),
                              ("previous-boot-kernel.txt", "-1", ["-k", "-p", "warning"]),
                              ("this-boot-kernel.txt", "0", ["-k", "-p", "warning"])):
        try:
            text = run(["journalctl", "-b", boot, *extra, "-n", "400", "-q", "--no-pager"]).stdout[-max_bytes:]
        except OSError:
            text = ""
        if text.strip():
            files[name] = text
    host["installed"] = [{k: g.get(k) for k in ("package", "title", "kind", "appid", "version", "agent_version")}
                         for g in cmd_list_installed({})["games"]]
    try:
        host["steam_library"], console = steam_library_report()
        if console:
            files["steam-console.txt"] = console[-max_bytes:]
    except Exception as exc:  # noqa: BLE001 (diagnostics must not fail on it)
        host["steam_library"] = {"error": str(exc)}
    out = {"agent_version": AGENT_VERSION, "host": host, "files": files}
    pkg = args.get("package")
    if not pkg:
        # the OpenXR runtime's own log (XRService-<date>_<time>.log): the newest one
        xr = [p for p in glob.glob(os.path.join(STEAM, "logs/XRService-*.log")) +
              glob.glob(os.path.join(STEAM, "logs/XRService-*/XRService-*.log")) if os.path.isfile(p)]
        for p in sorted(xr, key=os.path.getmtime, reverse=True)[:1]:
            files[os.path.basename(p)] = _tail(p, max_bytes)
        return out
    pkg = check_pkg(pkg)
    dep = deployment(pkg)
    anchor = os.path.join(ANCHORS, pkg)
    if not dep:
        out["installed"] = False
        return out
    out["installed"] = True
    base, appid = dep["base"], dep.get("appid")
    dep = dict(dep)
    dep.pop("files", None)  # the install manifest is large; `listing` below has the real files
    files["deployment.json"] = json.dumps(dep, indent=1)
    for name, path in (("launch.sh", os.path.join(anchor, "launch.sh")),
                       ("settings.conf", os.path.join(base, "settings.conf")),
                       ("launch.log", os.path.join(base, "launch.log")),
                       ("launch-test.log", os.path.join(base, "launch-test.log"))):
        text = _tail(path, max_bytes)
        if text is not None:
            files[name] = text
    for rel in PCVR_LOGS + (f"steam-{appid}.log",):
        text = _tail(os.path.join(base, rel), max_bytes)
        if text is not None:
            files[os.path.basename(rel)] = text
    if dep.get("kind") == "pcvr":
        for i, part in enumerate(game_logs(base)):
            files[f"game-log-{i}.txt"] = part[-max_bytes:]
    logs = os.path.join(STEAM, "logs")
    text = _tail(os.path.join(logs, f"lepton-steamlaunch-{appid}.log"), max_bytes)  # Lepton's launcher log
    if text is not None:
        files[f"lepton-steamlaunch-{appid}.log"] = text
    logcats = os.path.join(logs, "lepton-logcats")
    # the container's logcat buffers: lepton-logcats/steamlaunch-<appid>/logcat-{main,crash,system,kernel,radio}.log
    order = ("main", "crash", "system", "kernel", "radio")
    cands = [p for p in glob.glob(os.path.join(logcats, f"steamlaunch-{appid}", "*")) if os.path.isfile(p)]
    for p in sorted(cands, key=lambda p: next((i for i, k in enumerate(order) if k in os.path.basename(p)), 9)):
        name = os.path.basename(p)
        files[name if name.startswith("logcat") else "logcat-" + name] = _tail(p, max_bytes)
    try:
        listing = cmd_list_files({"package": pkg, "limit": 20000})
        out["listing"] = {"missing": listing["missing"], "truncated": listing["truncated"],
                          "roots": [{"name": r["name"], "files": r["files"]} for r in listing["roots"]]}
    except AgentError:
        pass
    return out


AGENT_HOME = os.path.join(HOME, ".local/share/frameport")


def cmd_purge(args):
    """Remove everything FramePort put on this Frame: its games (keep_saves: leave Quest save data and PC VR Proton
    prefixes), their Steam shortcuts + grid art, ~/Applications/quest-frame, and ~/.local/share/frameport (this agent,
    Proton self-test prefix, timefix layer). Runs detached (Steam is closed while shortcuts.vdf changes); poll
    purge_status. Proton/Lepton stay installed (they're Steam apps) and the podman keyring fix stays (harmless)."""
    games = cmd_list_installed({})["games"]
    if any((pcvr_pids(d["base"]) if d.get("kind") in ("pcvr", "linux") else container_running(d["appid"]))
           for d in games):
        raise AgentError("a FramePort game is running on the Frame; close it first")
    status = os.path.join(HOME, ".cache/frameport-purge.json")
    os.makedirs(os.path.dirname(status), exist_ok=True)
    with open(status, "w") as f:
        json.dump({"state": "running", "started": time.time()}, f)
    payload = json.dumps({"keep_saves": bool(args.get("keep_saves", True)), "status": status})
    run(["systemd-run", "--user", "--collect", "--quiet", f"--unit=frameport-purge-{int(time.time())}",
         "--setenv=HOME=" + HOME, sys.executable, os.path.abspath(__file__), "_purge_worker", payload])
    return {"started": True, "games": len(games), "status": status}


def purge_worker(payload):
    args = json.loads(payload)
    keep = args["keep_saves"]
    result = {"state": "done", "removed": [], "kept": [], "errors": []}
    service = True
    try:
        games = cmd_list_installed({})["games"]
        users = steam_users()
        try:
            service = stop_steam()
        except AgentError as exc:
            result["errors"].append(str(exc))
            service = True
        for d in games:  # devkit fallback entries (Steam is closed: removed from shortcuts.vdf)
            try:
                if devkit_unregister(d["package"], steam_running=False):
                    result["removed"].append(f"Steam devkit entry: {d.get('title')}")
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"{d.get('title')}: {exc}")
        for u in users:
            vdf = os.path.join(STEAM, "userdata", u, "config/shortcuts.vdf")
            grid = os.path.join(STEAM, "userdata", u, "config/grid")
            for d in games:
                try:
                    if remove_shortcut(vdf, f'"{os.path.join(ANCHORS, d["package"])}/launch.sh"'):
                        result["removed"].append(f"Steam shortcut: {d.get('title')}")
                    for art in grid_files(grid, d["appid"]):
                        os.remove(art)
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(f"{d.get('title')}: {exc}")
            try:  # shortcuts left from games without an install record (older versions, interrupted removals)
                root = vdf_decode(open(vdf, "rb").read()) if os.path.exists(vdf) else {}
                for sc in list(root.get("shortcuts", {}).values()):
                    exe = sc.get("Exe", "") if isinstance(sc, dict) else ""
                    if exe.startswith(f'"{ANCHORS}/') and remove_shortcut(vdf, exe):
                        result["removed"].append(f"Steam shortcut: {sc.get('AppName') or sc.get('appname')}")
                        for art in grid_files(grid, sc.get("appid", 0) & 0xFFFFFFFF):
                            os.remove(art)
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"shortcuts: {exc}")
        for d in games:
            base = d["base"]
            saves = [os.path.join(base, n) for n in ("lepton-data", "compatdata")]
            if keep and any(os.path.exists(p) for p in saves):
                for name in os.listdir(base) if os.path.isdir(base) else []:
                    p = os.path.join(base, name)
                    if p not in saves:
                        remove_tree(p)
                result["kept"].append(base)
            else:
                remove_tree(base)
            if not keep and os.path.lexists(base):
                result["errors"].append(f"couldn't remove {base}")
            result["removed"].append(d.get("title") or d["package"])
        for path in remove_desktop_entries():  # Linux apps' Desktop Mode entries
            result["removed"].append(path)
        for d in games:  # <drive>/FramePort folders left empty
            if remove_empty_drive_dir(d["base"]):
                result["removed"].append(os.path.dirname(d["base"].rstrip("/")))
        if not keep or not result["kept"]:
            remove_tree(ANCHORS)
        else:  # anchors hold only launchers/artwork; saves live in the bases
            for d in games:
                anchor = os.path.join(ANCHORS, d["package"])
                if os.path.realpath(anchor) not in [os.path.realpath(k) for k in result["kept"]]:
                    remove_tree(anchor)
        remove_tree(AGENT_HOME)
        result["removed"].append(AGENT_HOME)
        if os.path.exists(XR_LAYER_MANIFEST):
            os.remove(XR_LAYER_MANIFEST)
            result["removed"].append(XR_LAYER_MANIFEST)
        for name in ("frameport-setup.sh", "frameport-setup.log"):  # left by bootstrap.sh
            p = os.path.join(HOME, ".cache", name)
            if os.path.exists(p):
                os.remove(p)
                result["removed"].append(p)
    except Exception as exc:  # noqa: BLE001
        result["state"] = "failed"
        result["errors"].append(str(exc))
    finally:
        start_steam(service)
    result["finished"] = time.time()
    with open(args["status"], "w") as f:
        json.dump(result, f)


def cmd_purge_status(args):
    try:
        return json.load(open(os.path.join(HOME, ".cache/frameport-purge.json")))
    except (OSError, ValueError):
        return {"state": "none"}


def cmd_cleanup(args):
    """Remove rollback copies (previous-game.apk, settings.conf.previous) and leftover uploads; optional extra paths
    under HOME (e.g. an old manual-install folder). Saves and installed games are never touched."""
    freed, removed = 0, []
    for dep in cmd_list_installed({})["games"]:
        base = dep["base"]
        for name in (["previous-game.apk", "settings.conf.previous"] if args.get("rollback", True) else []) + \
                ["incoming", "incoming-artwork"]:
            p = os.path.join(base, name)
            if os.path.isdir(p):
                freed += sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs)
                remove_tree(p)
                removed.append(p)
            elif os.path.exists(p):
                freed += os.path.getsize(p)
                os.remove(p)
                removed.append(p)
    bases = [d["base"] for d in cmd_list_installed({})["games"]]
    drive_dirs = [d["install_dir"] for d in list_drives() if not d["internal"]]
    for extra in args.get("paths", []):
        p = os.path.realpath(os.path.expanduser(extra))
        first = os.path.relpath(p, HOME).split(os.sep)[0] if p.startswith(HOME + os.sep) else ""
        # inside a drive's FramePort folder (leftovers of games on a microSD), never an installed game's files
        on_drive = any(p != os.path.realpath(d) and _inside(p, os.path.realpath(d)) for d in drive_dirs) \
            and not any(_inside(p, os.path.realpath(b)) or _inside(os.path.realpath(b), p) for b in bases)
        if not on_drive and (not first or first.startswith(".") or p == ANCHORS or p.startswith(ANCHORS + os.sep)):
            # only ordinary folders in the home folder: never dot folders (.ssh, .steam, .local, .config…)
            raise AgentError(f"refusing to remove {extra}")
        if os.path.exists(p):
            freed += (sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs)
                      if os.path.isdir(p) else os.path.getsize(p))
            remove_tree(p)
            removed.append(p)
    return {"removed": removed, "freed_bytes": freed}


# ------------------------------------------------------------------------------------------ virtual keyboard
# "Type on Frame": the PC's key presses become a real keyboard on the Frame (Linux uinput; the steamos user may open
# /dev/uinput, an ACL entry made for Steam Input, no root). A real input device reaches everything that has focus:
# Android windows (Lepton's wayland_keyboard), the Steam UI, the desktop, Proton games.
UINPUT = "/dev/uinput"
UI_SET_EVBIT, UI_SET_KEYBIT, UI_DEV_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = (0x40045564, 0x40045565, 0x405C5503,
                                                                         0x5501, 0x5502)
EV_SYN, EV_KEY, SYN_REPORT = 0, 1, 0
KEY_LAST = 248  # KEY_ESC (1) .. KEY_MICMUTE (248): every key a PC keyboard sends
KEY_LEFTSHIFT = 42


def text_keys():
    """US layout: character -> (Linux key code, shift)."""
    keys = {"\n": (28, False), "\t": (15, False), " ": (57, False), "\b": (14, False)}
    for plain, shifted, first in (("1234567890-=", "!@#$%^&*()_+", 2), ("qwertyuiop[]", "QWERTYUIOP{}", 16),
                                  ("asdfghjkl;'`", 'ASDFGHJKL:"~', 30), ("\\zxcvbnm,./", "|ZXCVBNM<>?", 43)):
        for i, (a, b) in enumerate(zip(plain, shifted, strict=True)):
            keys[a] = (first + i, False)
            keys[b] = (first + i, True)
    return keys


TEXT_KEYS = text_keys()


class VirtualKeyboard:
    """A uinput keyboard. `fd`/`write` can be replaced in tests; the device goes away with close()."""

    def __init__(self, fd=None, write=os.write, settle=0.8):
        self.write, self.held = write, set()
        self.fd = fd
        if fd is None:
            self.fd = os.open(UINPUT, os.O_WRONLY | os.O_NONBLOCK)
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for code in range(1, KEY_LAST + 1):
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack("HHHH80sI", 0x03, 0x1209, 0x4650, 1,
                                                            b"FramePort keyboard", 0))
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
            time.sleep(settle)  # let gamescope/libinput pick the new keyboard up before the first key

    def _event(self, kind, code, value):
        self.write(self.fd, struct.pack("llHHi", 0, 0, kind, code, value))

    def key(self, code, value):
        """value 1 = down, 0 = up, 2 = autorepeat."""
        if not 0 < int(code) <= KEY_LAST or value not in (0, 1, 2):
            return
        self._event(EV_KEY, int(code), value)
        self._event(EV_SYN, SYN_REPORT, 0)
        (self.held.add if value else self.held.discard)(int(code))

    def type_text(self, text, delay=0.008):
        """Type characters of the US layout; returns the characters it couldn't type."""
        skipped = ""
        for ch in text.replace("\r\n", "\n"):
            if ch not in TEXT_KEYS:
                skipped += ch
                continue
            code, shift = TEXT_KEYS[ch]
            if shift:
                self.key(KEY_LEFTSHIFT, 1)
            self.key(code, 1)
            self.key(code, 0)
            if shift:
                self.key(KEY_LEFTSHIFT, 0)
            time.sleep(delay)
        return skipped

    def close(self):
        for code in list(self.held):  # never leave a key stuck when the PC goes away mid-press
            self.key(code, 0)
        if self.fd is not None and isinstance(self.fd, int):
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self.fd)
        self.fd = None


def keyboard_session(stdin, stdout, keyboard=None):
    """Long-lived: prints {"ready": true} once the virtual keyboard exists, then reads one JSON object per line:
    {"k": code, "v": 1|0|2} (key down/up/repeat) or {"text": "..."}; ends (keyboard removed) at EOF."""
    try:
        kb = keyboard or VirtualKeyboard()
    except OSError as exc:
        stdout.write(json.dumps({"ready": False, "error": f"can't create a virtual keyboard: {exc}"}) + "\n")
        stdout.flush()
        return 1
    stdout.write(json.dumps({"ready": True}) + "\n")
    stdout.flush()
    try:
        for line in stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "k" in msg:
                kb.key(msg["k"], msg.get("v", 1))
            elif "text" in msg:
                skipped = kb.type_text(str(msg["text"]))
                stdout.write(json.dumps({"typed": True, "skipped": skipped}) + "\n")
                stdout.flush()
    except (OSError, ValueError):
        pass
    finally:
        kb.close()
    return 0


def steam_js(expression, timeout=5):
    """Evaluate JavaScript in Steam's UI (SharedJSContext) through its CEF devtools port (127.0.0.1:8080, SteamOS
    starts Steam with -cef-enable-debugging); returns the value. A minimal websocket client (stdlib only)."""
    import base64
    import socket
    import urllib.request

    targets = json.load(urllib.request.urlopen("http://127.0.0.1:8080/json", timeout=timeout))
    url = next(t["webSocketDebuggerUrl"] for t in targets if t.get("title") == "SharedJSContext")
    path = url.split("127.0.0.1:8080", 1)[1]
    with socket.create_connection(("127.0.0.1", 8080), timeout=timeout) as sock:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:8080\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1)
            if not chunk:
                raise AgentError("Steam's devtools closed the connection")
            head += chunk
        data = json.dumps({"id": 1, "method": "Runtime.evaluate",
                           "params": {"expression": expression, "awaitPromise": True,
                                      "returnByValue": True}}).encode()
        mask, n = os.urandom(4), len(data)
        size = bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
        sock.sendall(b"\x81" + size + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

        def exact(k):
            buf = b""
            while len(buf) < k:
                chunk = sock.recv(k - len(buf))
                if not chunk:
                    raise AgentError("Steam's devtools closed the connection")
                buf += chunk
            return buf
        while True:
            _b1, b2 = exact(2)
            k = b2 & 0x7F
            k = struct.unpack(">H", exact(2))[0] if k == 126 else struct.unpack(">Q", exact(8))[0] if k == 127 else k
            msg = json.loads(exact(k).decode("utf-8", "replace") or "{}")
            if msg.get("id") == 1:
                res = (msg.get("result") or {}).get("result") or {}
                return res.get("value")


FIRST_FRAME_MARKERS = (b"FrameBridge: new layer:", b"FrameBridge: pacing:")
# SteamVR's dashboard UI logs a dashboard opened with the controller's button as "[ToggleDashboard]
# toggle_dashboard_action"; Steam's own start-up menu comes without it (onShowOverlayRequestFromSteam / frame menu)
USER_DASHBOARD_MARKER = b"toggle_dashboard_action"


def user_opened_dashboard(log, pos):
    """(opened, new position): whether the player opened the dashboard (controller button) since `pos` in SteamVR's
    vrwebhelper_systemui.txt. A missing log answers no (the worker then behaves as before)."""
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            if pos is None or pos > end:  # first look, or the log was rotated: only what comes from now on
                return False, end
            f.seek(pos)
            return USER_DASHBOARD_MARKER in f.read(), end
    except OSError:
        return False, pos


LOGCAT_EOF = "logcat: Unexpected EOF"


def logcat_keeper(log, appid, parent, poll=2.0, restarts=5, popen=None):
    """Keep launch.log filling when Lepton's logcat mirror dies while the game runs (LOGCAT_LINE): once the log shows
    LOGCAT_EOF after "Waiting for app", read the container's logcat ourselves (from its last 2000 lines, so the start
    of the game isn't lost) and append it; restart it if it ends while the game still runs (at most `restarts` times).
    Ends with the launcher."""
    popen = popen or subprocess.Popen
    container = f"lepton-steamlaunch-{appid}"

    def alive():
        try:
            os.kill(int(parent), 0)
            return True
        except (OSError, ValueError):
            return False

    proc, started = None, 0
    while alive():
        if proc is None or proc.poll() is not None:
            try:
                with open(log, errors="replace") as f:
                    text = f.read()
            except OSError:
                text = ""
            i = text.find("Waiting for app")
            if i >= 0 and LOGCAT_EOF in text[i:] and started < restarts:
                started += 1
                with open(log, "a") as f:
                    f.write(f"FramePort: Lepton's logcat ended; reading {container}'s logcat again ({started})\n")
                out = open(log, "ab")
                try:
                    proc = popen(["podman", "exec", container, "logcat", "-v", "threadtime", "-T", "2000"],
                                 stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                except OSError as exc:
                    print(f"logcat_keeper: {exc}", flush=True)
                    proc = None
                finally:
                    out.close()
        time.sleep(poll)
    if proc is not None and proc.poll() is None:
        proc.terminate()


def dashboard_worker(log, parent, wait_start=240, window=120, poll=0.5, ui_log=None, max_hides=10):
    """Close SteamVR's dashboard (Steam's "Resume game" frame menu) that opens when the game submits its first VR
    frame: watch from FrameBridge's first "new layer:" line (the first submitted frame; Steam showed the menu ~0.3 s
    later with ITR2), checking every `poll` s for `window` s after it (at most `max_hides`; agent 59: 120 s / 10, the
    menu came back after the first 30 s in the owner's sessions). Waiting for the first
    "pacing:" summary (agent 44-51) was ~8 s too late: the owner had pressed Resume by then. Stops for good once the
    player opens the dashboard with the controller (agent 52 closed it 60 ms after each press: the game had paused
    for it and stayed paused). Ends with the launcher."""
    if ui_log is None:
        ui_log = os.path.join(STEAM, "logs/vrwebhelper_systemui.txt")
    _, ui_pos = user_opened_dashboard(ui_log, None)
    def alive():
        try:
            os.kill(int(parent), 0)
            return True
        except (OSError, ValueError):
            return False
    deadline, pos, tail = time.time() + wait_start, 0, b""
    while time.time() < deadline and alive():
        try:
            with open(log, "rb") as f:
                f.seek(pos)
                chunk = f.read()
                pos += len(chunk)
            text = tail + chunk
            if any(m in text for m in FIRST_FRAME_MARKERS):
                break
            tail = text[-64:]  # a marker split across two reads
        except OSError:
            pass
        time.sleep(poll)
    else:
        print("no VR frames logged; dashboard left as it is")
        return
    print(f"{time.strftime('%H:%M:%S')} first VR frame")
    hidden, end = 0, time.time() + window
    while time.time() < end and hidden < max_hides and alive():
        user, ui_pos = user_opened_dashboard(ui_log, ui_pos)
        if user:  # the player wants the dashboard (or the game's menu button opened it): never close it on them
            print(f"{time.strftime('%H:%M:%S')} dashboard opened with the controller: leaving it to the player")
            return
        try:
            if steam_js("SteamClient.OpenVR.VROverlay.IsDashboardVisible()"):
                steam_js("SteamClient.OpenVR.VROverlay.HideDashboard()")
                hidden += 1
                print(f"{time.strftime('%H:%M:%S')} dashboard hidden")
        except Exception as exc:  # noqa: BLE001 (no devtools port, Steam restarting: leave it)
            print(f"steam ui: {exc}")
            return
        time.sleep(poll)


def not_started(text, waited, grace=30):
    """Lepton's container didn't come up: "is not a running context" with no "Boot complete!" after it for `grace`
    seconds. The first start after an APK change prints that message while it waits for the boot and then boots
    normally (installing the new APK): agent 53 and older stopped those starts after a few seconds."""
    i = text.rfind("is not a running context")
    return i >= 0 and "Boot complete!" not in text[i:] and waited >= grace


# ------------------------------------------------------------------------------------------ monitor
# The GUI's Monitor tab: `_monitor` streams one JSON sample per tick (CPU/GPU/memory/temperatures/power/battery, the
# running FramePort games with fps, processes) and takes control lines (interval, process filter, end a process or a
# game) until stdin closes. Sources (dev Frame, SteamOS 0.4.3, kernel 6.18, 2026-10-07): GPU busy = summed DRM fdinfo
# `drm-engine-gpu` ns of every render-node fd (msm), GPU clock from devfreq 3d00000.gpu, 48 thermal zones grouped by
# type, max34417 power monitors (µW; vph = the whole system), the max1720x battery gauge, FrameBridge's `pacing:` lines
# for fps. Cheap by design: a naive sample (fd scan of ~520 processes) cost 43 ms CPU, so kernel threads are skipped
# after their first sighting and render fds are cached per process.
PROC = "/proc"
SYS = "/sys"
MON_INTERVALS = (0.1, 0.25, 0.5, 1, 2, 5)  # seconds between samples; processes are scanned every 2 s at any of them
MON_DEFAULT_INTERVAL = 0.5
SCAN_SECONDS = 2.0
MON_FILTERS = ("game", "steam", "all")
MON_PROC_LIMIT = 150
MON_CONTEXT = 3  # "game" filter: the busiest other processes, shown for context
# processes never signalled: the session, SSH and system plumbing (killing them logs the user out or drops FramePort)
MON_NEVER = ("systemd", "sshd", "sshd-session", "sshd-auth", "dbus-daemon", "dbus-broker", "dbus-broker-lau",
             "(sd-pam)", "login", "agetty")
# processes that end the headset session, Steam or the desktop when killed: the GUI asks again ("force")
MON_CRITICAL = ("steam", "steamwebhelper", "reaper", "vrserver", "vrcompositor", "vrmonitor", "vrwebhelper",
                "vrdashboard", "vrstartup", "XRServiceLoopTh", "XRService", "gamescope", "gamescope-wl", "Xwayland",
                "plasmashell", "kwin_wayland", "kwin_x11", "pipewire", "pipewire-pulse", "wireplumber", "V4L2Cam",
                "steamos-session", "gamescope-sessi")
# processes whose command line can name a game (only these are read: cmdlines of ~500 processes cost too much)
MON_ROOT_COMMS = ("conmon", "launch.sh", "reaper", "bash", "sh")
MON_ROOT_PREFIXES = ("python", "proton", "pv-", "wine", "steam-runtime")
MON_GROUPS = (  # (group, comm prefixes), first match wins; games are found by their process tree
    ("steamvr", ("vr", "XRService", "V4L2Cam", "eyetracking", "proxmicmute", "systemlayer")),
    ("steam", ("steam", "reaper", "fossilize")),
    ("desktop", ("gamescope", "Xwayland", "plasmashell", "kwin", "xdg-desktop", "pipewire", "wireplumber",
                 "kded", "ksmserver")),
)
TEMP_GROUPS = (  # (group, zone type prefixes): the hottest zone of each group is shown
    ("CPU", ("cpu",)), ("GPU", ("gpuss",)), ("Memory", ("ddr",)), ("Battery", ("max1720x",)), ("NPU", ("nsp",)),
    ("Power ICs", ("pm8",)), ("Modem", ("modem",)), ("Camera", ("camera", "video")), ("Storage", ("ufs",)),
)
RAIL_GROUPS = {"vph": "system", "gfx": "gpu", "apc0": "cpu", "apc1": "cpu", "apc2": "cpu", "nsp1": "npu",
               "nsp2": "npu"}
PACING_RE = re.compile(rb"FrameBridge: pacing: ([0-9.]+) fps(?:, displayTime vs predicted: avg ([0-9.]+) ms)?")
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _rd(path):
    try:
        with open(path) as f:
            return f.read()
    except (OSError, ValueError):
        return ""


def _num(text, default=None):
    try:
        return int(text.strip())
    except (ValueError, AttributeError):
        return default


def read_cpu_times():
    """[total, core0, core1, …] as (busy, all) jiffies from /proc/stat."""
    out = []
    for line in _rd(f"{PROC}/stat").splitlines():
        if not line.startswith("cpu"):
            break
        f = [int(x) for x in line.split()[1:9]]
        idle = f[3] + f[4]  # idle + iowait
        out.append((sum(f) - idle, sum(f)))
    return out


def cpu_percent(prev, cur):
    """Busy % per entry of read_cpu_times() between two readings (0 when nothing elapsed)."""
    res = []
    for (b0, a0), (b1, a1) in zip(prev, cur, strict=False):
        res.append(round(100.0 * (b1 - b0) / (a1 - a0), 1) if a1 > a0 else 0.0)
    return res


def read_clusters():
    """[{"cpus": [0, 1], "max_mhz": 2265, "policy": path}] per cpufreq policy."""
    out = []
    policies = glob.glob(f"{SYS}/devices/system/cpu/cpufreq/policy*")
    for p in sorted(policies, key=lambda s: _num(s.rsplit("policy", 1)[1], 0)):
        cpus = [int(x) for x in _rd(f"{p}/related_cpus").split() if x.isdigit()]
        out.append({"cpus": cpus, "max_mhz": (_num(_rd(f"{p}/cpuinfo_max_freq"), 0)) // 1000, "policy": p})
    return out


def find_gpu_devfreq():
    for d in sorted(glob.glob(f"{SYS}/class/devfreq/*")):
        if "gpu" in os.path.basename(d) or "gpu" in _rd(f"{d}/name"):
            return d
    return None


def read_meminfo():
    m = {}
    for line in _rd(f"{PROC}/meminfo").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            m[k] = _num(v.split()[0], 0) * 1024
    return {"total": m.get("MemTotal", 0), "avail": m.get("MemAvailable", 0), "swap_total": m.get("SwapTotal", 0),
            "swap_free": m.get("SwapFree", 0)}


def read_psi():
    """"some" avg10 of /proc/pressure/{cpu,memory,io}: % of time something waited on it in the last 10 s."""
    out = {}
    for k in ("cpu", "memory", "io"):
        m = re.search(r"^some avg10=([0-9.]+)", _rd(f"{PROC}/pressure/{k}"), re.M)
        if m:
            out[k] = float(m.group(1))
    return out


def temp_zones():
    """[(group, zone name, temp path)] for every thermal zone in a known group."""
    out = []
    for z in sorted(glob.glob(f"{SYS}/class/thermal/thermal_zone*"), key=lambda s: _num(s.rsplit("zone", 1)[1], 0)):
        name = _rd(f"{z}/type").strip()
        group = next((g for g, prefixes in TEMP_GROUPS if name.startswith(prefixes)), None)
        if group:
            out.append((group, name.replace("-thermal", ""), f"{z}/temp"))
    return out


def read_temps(zones):
    """({group: hottest °C}, {group: {zone: °C}})"""
    groups, detail = {}, {}
    for group, name, path in zones:
        v = _num(_rd(path))
        if v is None or v <= -40000:
            continue
        c = round(v / 1000.0, 1)
        detail.setdefault(group, {})[name] = c
        groups[group] = max(groups.get(group, c), c)
    return groups, detail


def power_rails():
    """[(label, power*_input path)] of the max34417 power monitors (µW) that RAIL_GROUPS names."""
    out = []
    for h in sorted(glob.glob(f"{SYS}/class/hwmon/hwmon*")):
        for lab in sorted(glob.glob(f"{h}/power*_label")):
            label = _rd(lab).strip()
            if label in RAIL_GROUPS:  # only the rails the Monitor shows: each read is an I2C transfer
                out.append((label, lab[:-len("_label")] + "_input"))
    return out


FAST_RAILS = ("vph", "gfx")  # read every tick; the others (CPU clusters, NPU) every 5 s


class PowerReader:
    """{"system", "cpu", "gpu", "npu": W, "rails": {label: W}} from the power monitors. Every read is an I2C transfer
    (~0.5 ms of kernel time each, more under load), so only the system and GPU rails are read every tick."""

    def __init__(self, rails):
        self.rails, self.watts, self.slow_at = rails, {}, -1e9

    def read(self, now):
        slow = now - self.slow_at >= 5
        if slow:
            self.slow_at = now
        for label, path in self.rails:
            if slow or label in FAST_RAILS:
                uw = _num(_rd(path))
                if uw is not None:
                    self.watts[label] = uw / 1e6
        out = {"rails": {k: round(w, 3) for k, w in self.watts.items()}}
        for label, w in self.watts.items():
            group = RAIL_GROUPS.get(label)
            if group:
                out[group] = round(out.get(group, 0.0) + w, 3)
        return out


def find_fan():
    for f in sorted(glob.glob(f"{SYS}/class/hwmon/hwmon*/fan1_input")):
        return f
    return None


class BatteryReader:
    """battery_state() fields + watts (negative = draining), seconds to empty/full, health, cycles, °C. The gauge sits
    on I2C (~0.7 ms of kernel time per file on the Frame), so the paths are found once, current and voltage are read
    every 2 s and everything else every 10 s."""
    FAST = (("current", "current_now"), ("voltage", "voltage_now"))
    SLOW = (("empty_s", "time_to_empty_now"), ("full_s", "time_to_full_now"), ("cycles", "cycle_count"),
            ("full_uah", "charge_full"), ("design_uah", "charge_full_design"), ("temp", "temp"))

    def __init__(self):
        self.dir, self.chargers, self.slow, self.slow_at = None, [], {}, -1e9
        self.fast, self.fast_at = {}, -1e9
        try:
            names = sorted(os.listdir(POWER_SUPPLY))
        except OSError:
            names = []
        for name in names:
            d = os.path.join(POWER_SUPPLY, name)
            kind = _rd(f"{d}/type").strip()
            if kind == "Battery" and self.dir is None and _rd(f"{d}/capacity").strip().isdigit():
                self.dir = d
            elif kind in CHARGER_TYPES:
                self.chargers.append(f"{d}/online")

    def read(self, now):
        if not self.dir:
            return None
        if now - self.fast_at >= 2:
            self.fast = {k: _num(_rd(f"{self.dir}/{f}")) for k, f in self.FAST}
            self.fast_at = now
        if now - self.slow_at >= 10:
            self.slow = {k: _num(_rd(f"{self.dir}/{f}")) for k, f in self.SLOW}
            if self.slow.get("temp") is not None:
                self.slow["temp"] = self.slow["temp"] / 10.0
            self.slow["health"] = _rd(f"{self.dir}/health").strip() or None
            self.slow["percent"] = _num(_rd(f"{self.dir}/capacity"))
            self.slow["status"] = _rd(f"{self.dir}/status").strip()
            self.slow["_charger"] = any(_rd(c).strip() == "1" for c in self.chargers)
            self.slow_at = now
        status = self.slow.get("status") or ""
        b = {"percent": self.slow.get("percent"), "status": status,
             "plugged": bool(self.slow.get("_charger")) or status in ("Charging", "Full"),
             "draining": status == "Discharging"}
        cur, volt = self.fast.get("current"), self.fast.get("voltage")
        if cur is not None and volt is not None:
            b["watts"] = round(cur * volt / 1e12, 2)
        b.update({k: x for k, x in self.slow.items() if x is not None and k not in b and not k.startswith("_")})
        return b


def read_net():
    """{iface: (rx bytes, tx bytes)} for the Frame's links."""
    out = {}
    for line in _rd(f"{PROC}/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if name.startswith(("wlan", "usb", "eth", "enp")):
            f = rest.split()
            out[name] = (int(f[0]), int(f[8]))
    return out


def proc_stat(pid):
    """(comm, ppid, utime+stime jiffies, start jiffies, rss pages) from /proc/<pid>/stat, or None."""
    s = _rd(f"{PROC}/{pid}/stat")
    r = s.rfind(")")
    if r < 0:
        return None
    comm = s[s.find("(") + 1:r]
    f = s[r + 2:].split()
    try:
        return comm, int(f[1]), int(f[11]) + int(f[12]), int(f[19]), int(f[21])
    except (IndexError, ValueError):
        return None


def drm_fds(pid):
    """File descriptors of a process that are GPU render nodes (/dev/dri/renderD*; Lepton games see /dev/kgsl-3d0)."""
    out = []
    try:
        fds = os.listdir(f"{PROC}/{pid}/fd")
    except OSError:
        return out
    for fd in fds:
        try:
            t = os.readlink(f"{PROC}/{pid}/fd/{fd}")
        except OSError:
            continue
        if t.startswith(("/dev/dri/render", "/dev/kgsl")):
            out.append(fd)
    return out


def drm_engine_ns(pid, fds):
    """Summed `drm-engine-gpu` ns over the process's render fds (several fds = several DRM clients); None if gone."""
    total, seen = 0, False
    for fd in fds:
        text = _rd(f"{PROC}/{pid}/fdinfo/{fd}")
        i = text.find("drm-engine-gpu:")
        if i < 0:
            continue
        seen = True
        total += _num(text[i + 15:].split(None, 1)[0], 0)
    return total if seen else None


def cgroup_dir(pid):
    """The cgroup v2 directory of a process that has memory/cpu stats (walks up from a leaf without them)."""
    for line in _rd(f"{PROC}/{pid}/cgroup").splitlines():
        if line.startswith("0::"):
            d = f"{SYS}/fs/cgroup" + line[3:].strip()
            while len(d) > len(f"{SYS}/fs/cgroup") and not os.path.exists(f"{d}/memory.current"):
                d = os.path.dirname(d)
            return d if os.path.exists(f"{d}/memory.current") else None
    return None


def cgroup_usage(d):
    """(cpu usage µs, memory bytes) of a cgroup directory."""
    m = re.search(r"^usage_usec (\d+)", _rd(f"{d}/cpu.stat"), re.M)
    return (int(m.group(1)) if m else None), _num(_rd(f"{d}/memory.current"))


def monitor_group(comm):
    for group, prefixes in MON_GROUPS:
        if comm.startswith(prefixes):
            return group
    return "other"


class Monitor:
    """Sampling state: previous counters for deltas, caches (kernel threads, render fds, zones, rails, games)."""

    def __init__(self, clock=time.monotonic, uid=None):
        self.clock = clock
        self.uid = os.getuid() if uid is None else uid
        self.ncpu = max(1, len(read_cpu_times()) - 1)
        self.clusters = read_clusters()
        self.gpu = find_gpu_devfreq()
        self.zones = temp_zones()
        self.rails = power_rails()
        self.fan = find_fan()
        self.battery = BatteryReader()
        self.power = PowerReader(self.rails)
        self.page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        self.btime = next((int(x.split()[1]) for x in _rd(f"{PROC}/stat").splitlines() if x.startswith("btime ")), 0)
        self.filter = "game"
        self.kthreads = set()
        self.drm = {}       # pid -> (fds, time of the fd scan)
        self.gpu_ns = {}    # pid -> last drm-engine ns
        self.gpu_pct = {}   # pid -> GPU % in the last tick
        self.gpu_acc = {}   # pid -> GPU ns since the last process scan (the table shows the average over it)
        self.scan_at = -1e9
        self.scan_prev = None  # time of the previous process scan
        self.proc_prev = {}  # pid -> (cpu jiffies, time)
        self.cpu_prev = None
        self.net_prev = None
        self.cg_prev = {}   # cgroup dir -> (usage µs, time)
        self.logs = {}      # launch.log path -> [offset, fps, avg ms, time of the line]
        self.deps = []
        self.deps_at = -1e9
        self.tick = 0
        self.last = None    # time of the previous sample
        self.disk_at = -1e9
        self.zone_detail_at = -1e9
        self.temps, self.zone_detail, self.temps_at = None, {}, -1e9
        self.procs = []     # last scan (all, unfiltered), for kill checks
        self.games = []
        self.roots = {}
        self.root_cache = {}  # (pid, start, comm) -> (package, kind) | None
        self.names = {}       # (pid, start, comm) -> full name of a process whose comm was truncated

    def static(self):
        gpu_max = _num(_rd(f"{self.gpu}/max_freq"), 0) // 1000000 if self.gpu else None
        return {"agent": AGENT_VERSION, "cores": self.ncpu, "gpu_max_mhz": gpu_max,
                "clusters": [{"cpus": c["cpus"], "max_mhz": c["max_mhz"]} for c in self.clusters],
                "mem_total": read_meminfo()["total"], "rails": [r[0] for r in self.rails],
                "temp_groups": sorted({z[0] for z in self.zones}, key=[g for g, _ in TEMP_GROUPS].index),
                "fan": bool(self.fan), "intervals": list(MON_INTERVALS), "filters": list(MON_FILTERS)}

    # -------------------------------------------------------------------- games
    def deployments(self, now):
        if now - self.deps_at > 30:
            self.deps = cmd_list_installed({})["games"]
            self.deps_at = now
        return self.deps

    def fps(self, base, now):
        """Latest FrameBridge pacing of a game's launch.log (read incrementally): (fps, avg ms) or (None, None)."""
        path = os.path.join(base, "launch.log")
        st = self.logs.setdefault(path, [None, None, None, 0.0])
        try:
            size = os.path.getsize(path)
        except OSError:
            return None, None
        if st[0] is None or size < st[0]:  # first look or a new launch truncated it: only the last 64 KiB
            st[0] = max(0, size - 65536)
        if size > st[0]:
            with open(path, "rb") as f:
                f.seek(st[0])
                data = f.read(min(size - st[0], 1 << 20))
            st[0] += len(data)
            for m in PACING_RE.finditer(data):
                st[1] = float(m.group(1))
                st[2] = float(m.group(2)) if m.group(2) else None
                st[3] = now
        if st[1] is None or now - st[3] > 15:  # pacing comes every ~5 s; older = loading or gone
            return None, None
        return st[1], st[2]

    # ---------------------------------------------------------------- processes
    def scan(self, now, wall):
        """Every process worth showing: uid 1000 + everything inside a game container. Groups by process tree."""
        raw = {}
        try:
            names = os.listdir(PROC)
        except OSError:
            names = []
        for name in names:
            if not name.isdigit():
                continue
            pid = int(name)
            if pid in self.kthreads:
                continue
            st = proc_stat(pid)
            if st is None:
                continue
            if st[1] == 2 or pid == 2:  # kernel threads: never shown, skipped from now on
                self.kthreads.add(pid)
                continue
            try:
                uid = os.stat(f"{PROC}/{pid}").st_uid
            except OSError:
                continue
            raw[pid] = (st, uid)
        self.kthreads &= {int(n) for n in names if n.isdigit()}
        deps = self.deployments(now)
        by_appid = {str(d.get("appid")): d for d in deps}
        roots = {}  # pid -> (package, "container" | "launcher"): a game container's conmon, its launch.sh/reaper,
        # Proton processes started in its folder (their whole process trees are the game)
        bases = [(d["base"].rstrip("/") + "/", d["package"]) for d in deps if d.get("kind") in ("pcvr", "linux")]
        anchors = [(os.path.join(ANCHORS, d["package"], "launch.sh"), d["package"]) for d in deps]
        for pid, ((comm, _ppid, _cpu, start, _rss), uid) in raw.items():
            if uid != self.uid or not (comm in MON_ROOT_COMMS or comm.startswith(MON_ROOT_PREFIXES)):
                continue
            key = (pid, start, comm)
            if key not in self.root_cache:
                cmd = _rd(f"{PROC}/{pid}/cmdline").replace("\0", " ")
                root = None
                if comm == "conmon":
                    m = re.search(r"lepton-steamlaunch-(\d+)", cmd)
                    if m and m.group(1) in by_appid:
                        root = (by_appid[m.group(1)]["package"], "container")
                else:
                    pkg = next((k for path, k in anchors if path in cmd), None) or \
                        next((k for base, k in bases if base in cmd), None)
                    root = (pkg, "launcher") if pkg else None
                self.root_cache[key] = root
            if self.root_cache[key]:
                roots[pid] = self.root_cache[key]
        children = {}
        for pid, ((_comm, ppid, *_), _uid) in raw.items():
            children.setdefault(ppid, []).append(pid)
        game_of = {}
        for root, (pkg, _kind) in roots.items():
            stack = [root]
            while stack:
                p = stack.pop()
                if p in game_of:
                    continue
                game_of[p] = pkg
                stack.extend(children.get(p, ()))
        out = []
        for pid, ((comm, ppid, cpu, start, rss), uid) in raw.items():
            pkg = game_of.get(pid)
            if uid != self.uid and not pkg:
                continue
            prev = self.proc_prev.get(pid)
            cpu_pct = 0.0
            if prev and now > prev[1]:
                cpu_pct = 100.0 * (cpu - prev[0]) / CLK_TCK / (now - prev[1]) / self.ncpu
            self.proc_prev[pid] = (cpu, now)
            name = comm
            if len(comm) == 15:  # truncated by the kernel (Android shows "lus4xvrplayerov"): the command line's name
                key = (pid, start, comm)
                if key not in self.names:
                    arg0 = _rd(f"{PROC}/{pid}/cmdline").split("\0", 1)[0]
                    self.names[key] = os.path.basename(arg0) if comm in arg0 else comm
                name = self.names[key] or comm
            out.append({"pid": pid, "ppid": ppid, "name": name, "group": f"game:{pkg}" if pkg else monitor_group(comm),
                        "game": pkg, "cpu": round(max(cpu_pct, 0.0), 1), "rss": rss * self.page,
                        "age": max(0, int(wall - (self.btime + start / CLK_TCK))) if self.btime else None,
                        "uid": uid, "critical": comm in MON_CRITICAL, "locked": comm in MON_NEVER})
            # render fds: new processes now; a busy game process without any every 4 s for its first 2 min (Android
            # games open the GPU a while after starting; a game container has ~90 mostly idle processes), others
            # every 60 s (listing ~300 fds of a steamwebhelper costs)
            fds = self.drm.get(pid)
            young_game = pkg and (p_age := out[-1]["age"]) is not None and p_age < 120 and out[-1]["cpu"] >= 1.0
            if fds is None or now - fds[1] > (4 if young_game and not fds[0] else 60):
                self.drm[pid] = (drm_fds(pid), now)
        alive = set(raw)
        for d in (self.proc_prev, self.drm, self.gpu_ns, self.gpu_pct):
            for pid in [p for p in d if p not in alive]:
                del d[pid]
        for cache in (self.root_cache, self.names):
            for key in [k for k in cache if k[0] not in alive]:
                del cache[key]
        self.procs = out
        self.roots = roots
        return out

    def gpu_sample(self, now, dt):
        """GPU % per process (from the cached render fds) and the summed busy %."""
        total = 0.0
        for pid, (fds, _t) in list(self.drm.items()):
            if not fds:
                continue
            ns = drm_engine_ns(pid, fds)
            if ns is None:
                continue
            prev = self.gpu_ns.get(pid)
            self.gpu_ns[pid] = ns
            if prev is not None and dt > 0 and ns >= prev:
                pct = min(100.0, (ns - prev) / 1e7 / dt)
                self.gpu_pct[pid] = pct
                self.gpu_acc[pid] = self.gpu_acc.get(pid, 0) + ns - prev
                total += pct
        return min(100.0, total)

    def games_sample(self, now):
        """Running FramePort games: title, kind, start time, CPU/memory of the container's cgroup, GPU %, fps."""
        deps = {d["package"]: d for d in self.deployments(now)}
        out = []
        for pkg in sorted({v[0] for v in self.roots.values()}):
            d = deps.get(pkg) or {}
            pids = [p["pid"] for p in self.procs if p["game"] == pkg]
            cpu = sum(p["cpu"] for p in self.procs if p["game"] == pkg)
            rss = sum(p["rss"] for p in self.procs if p["game"] == pkg)
            mem = None
            conmon = [pid for pid, (k, kind) in self.roots.items() if k == pkg and kind == "container"]
            kids = [p["pid"] for p in self.procs if p["ppid"] in conmon]
            cg = cgroup_dir(kids[0]) if kids else None
            if cg and cg != cgroup_dir(conmon[0]):  # the container's own cgroup (conmon's is the user's)
                usage, mem = cgroup_usage(cg)
                prev = self.cg_prev.get(cg)
                self.cg_prev[cg] = (usage, now)
                if usage is not None and prev and prev[0] is not None and now > prev[1]:
                    cpu = 100.0 * (usage - prev[0]) / 1e6 / (now - prev[1]) / self.ncpu
            ages = [p["age"] for p in self.procs if p["game"] == pkg and p["age"] is not None]
            fps, ms = self.fps(d["base"], now) if d.get("base") else (None, None)
            out.append({"package": pkg, "title": d.get("title") or pkg, "kind": d.get("kind", "quest"),
                        "appid": d.get("appid"), "elapsed": max(ages) if ages else None,
                        "cpu": round(max(cpu, 0.0), 1), "mem": mem if mem is not None else rss,
                        "gpu": round(sum(self.gpu_pct.get(p, 0.0) for p in pids), 1),
                        "fps": fps, "frame_ms": ms, "processes": len(pids)})
        self.games = out
        return out

    def filtered(self, now=None):
        procs = self.procs
        span = (now - self.scan_prev) if now is not None and self.scan_prev is not None else 0.0
        for p in procs:  # GPU % averaged over the time since the previous scan (per tick it's noisy at 0.1 s)
            acc = self.gpu_acc.get(p["pid"], 0)
            p["gpu"] = round(min(100.0, acc / 1e7 / span), 1) if span > 0 else \
                round(self.gpu_pct.get(p["pid"], 0.0), 1)
        self.gpu_acc = {}
        if self.filter == "game":
            shown = [p for p in procs if p["game"]]
            others = sorted((p for p in procs if not p["game"]), key=lambda p: (-p["cpu"], -p["rss"]))
            shown += [dict(p, context=True) for p in others[:MON_CONTEXT]]
        elif self.filter == "steam":
            shown = [p for p in procs if p["group"] in ("steam", "steamvr", "desktop")]
        else:
            shown = list(procs)
        shown.sort(key=lambda p: (-p["cpu"], -p["gpu"], -p["rss"]))
        return shown[:MON_PROC_LIMIT]

    # ------------------------------------------------------------------- sample
    def sample(self, wall=None):
        t0 = time.process_time()
        now = self.clock()
        wall = time.time() if wall is None else wall
        dt = now - self.last if self.last is not None else 0.0
        self.last = now
        out = {"t": round(wall, 3), "dt": round(dt, 3)}
        cur = read_cpu_times()
        if self.cpu_prev:
            pct = cpu_percent(self.cpu_prev, cur)
            out["cpu"] = {"total": pct[0] if pct else 0.0, "cores": pct[1:]}
        else:
            out["cpu"] = {"total": 0.0, "cores": [0.0] * self.ncpu}
        self.cpu_prev = cur
        out["cpu"]["mhz"] = [_num(_rd(f"{c['policy']}/scaling_cur_freq"), 0) // 1000 for c in self.clusters]
        out["mem"] = read_meminfo()
        out["psi"] = read_psi()
        if now - self.temps_at >= 2 or self.temps is None:  # 48 sensor files (~3 ms at idle clocks); heat is slow
            self.temps, self.zone_detail = read_temps(self.zones)
            self.temps_at = now
        temps, detail = self.temps, self.zone_detail
        out["temps"] = temps
        if now - self.zone_detail_at >= 5:
            out["zones"] = detail
            self.zone_detail_at = now
        if self.fan:
            out["fan"] = _num(_rd(self.fan))
        out["power"] = self.power.read(now)
        out["battery"] = self.battery.read(now)
        net = read_net()
        if self.net_prev and dt > 0:
            prev = self.net_prev
            out["net"] = {k: [max(0, int((v[0] - prev[k][0]) / dt)), max(0, int((v[1] - prev[k][1]) / dt))]
                          for k, v in net.items() if k in prev}
        self.net_prev = net
        if now - self.disk_at >= 30:
            try:
                st = os.statvfs(HOME)
                out["disk"] = {"free": st.f_bavail * st.f_frsize, "total": st.f_blocks * st.f_frsize}
            except OSError:
                pass
            self.disk_at = now
        scan = now - self.scan_at >= SCAN_SECONDS or not self.procs
        if scan:
            self.scan(now, wall)
            self.scan_at = now
        busy = self.gpu_sample(now, dt)
        out["gpu"] = {"busy": round(busy, 1) if dt > 0 else None,
                      "mhz": _num(_rd(f"{self.gpu}/cur_freq"), 0) // 1000000 if self.gpu else None}
        out["games"] = self.games_sample(now)
        if scan:
            out["procs"] = self.filtered(now)
            out["filter"] = self.filter
            self.scan_prev = now
        self.tick += 1
        out["self_ms"] = round((time.process_time() - t0) * 1000, 2)
        return out

    # ------------------------------------------------------------------ actions
    def ancestors(self):
        pids, p = set(), os.getpid()
        while p > 1:
            pids.add(p)
            st = proc_stat(p)
            if not st:
                break
            p = st[1]
        return pids

    def kill(self, pid, sig="TERM", force=False, wait=3.0):
        """Signal one process the Monitor shows. TERM waits up to `wait` s and reports whether it ended."""
        import signal

        pid = int(pid)
        st = proc_stat(pid)
        if st is None:
            return {"ended": True, "pid": pid, "note": "already gone"}
        comm = st[0]
        if pid <= 2 or pid in self.ancestors() or comm in MON_NEVER:
            raise AgentError(f"{comm} ({pid}) is part of the system or of FramePort's connection; it can't be ended")
        try:
            uid = os.stat(f"{PROC}/{pid}").st_uid
        except OSError:
            return {"ended": True, "pid": pid, "note": "already gone"}
        in_game = any(p["pid"] == pid and p["game"] for p in self.procs)
        if uid != self.uid and not in_game:
            raise AgentError(f"{comm} ({pid}) belongs to another user")
        if comm in MON_CRITICAL and not force:
            raise AgentError(f"CRITICAL: {comm}")
        signum = signal.SIGKILL if sig == "KILL" else signal.SIGTERM
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            return {"ended": True, "pid": pid}
        except PermissionError as exc:
            raise AgentError(f"not allowed to end {comm} ({pid}): {exc}") from exc
        end = time.monotonic() + wait
        while time.monotonic() < end:
            st = proc_stat(pid)
            if st is None or _rd(f"{PROC}/{pid}/status").find("State:\tZ") >= 0:
                return {"ended": True, "pid": pid, "name": comm}
            time.sleep(0.1)
        return {"ended": False, "pid": pid, "name": comm}


def monitor_end_game(pkg, wait=6.0):
    """Steam's Exit game for a FramePort game (so Steam's session ends cleanly), then cmd_stop if it still runs."""
    pkg = check_pkg(pkg)
    dep = deployment(pkg)
    if not dep:
        raise AgentError(f"{pkg} is not installed")

    def running():
        if dep.get("kind") in ("pcvr", "linux"):
            return bool(pcvr_pids(dep["base"]))
        return container_running(dep["appid"])
    if not running():
        return {"ended": True, "via": None, "package": pkg, "note": "not running"}
    via = None
    devkit = devkit_gameid(pkg)
    appid = (devkit_appid(devkit) if devkit else None) or dep.get("appid")
    try:
        steam_js(f"SteamClient.Apps.TerminateApp('{steam_gameid(appid)}', false)")
        via = "steam"
        end = time.monotonic() + wait
        while time.monotonic() < end and running():
            time.sleep(0.5)
    except Exception:  # noqa: BLE001  (Steam's UI unreachable: stop it directly)
        pass
    if running():
        cmd_stop({"package": pkg})
        via = "stop"
        end = time.monotonic() + wait
        while time.monotonic() < end and running():
            time.sleep(0.5)
    return {"ended": not running(), "via": via, "package": pkg}


def monitor_session(stdin, stdout, monitor=None, sleep=time.sleep, max_ticks=None):
    """Long-lived: prints {"ready": 1, "static": {...}}, then one sample per tick. Control lines on stdin:
    {"interval": 1|2|5}, {"procs": "game"|"steam"|"all"}, {"pause": bool}, {"id": n, "kill": pid, "sig": "TERM"|
    "KILL", "force": bool}, {"id": n, "end_game": package} (each with an id gets {"reply": n, "ok": …}). Ends at EOF
    or when the SSH session (parent) is gone."""
    import threading

    mon = monitor or Monitor()
    lock = threading.Lock()
    state = {"interval": MON_DEFAULT_INTERVAL, "stop": False, "pause": False}
    wake = threading.Event()

    def emit(obj):
        with lock:
            stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
            stdout.flush()

    def act(msg):
        try:
            if "kill" in msg:
                res = mon.kill(msg["kill"], msg.get("sig", "TERM"), bool(msg.get("force")))
            else:
                res = monitor_end_game(msg["end_game"])
            emit({"reply": msg.get("id"), "ok": True, "result": res})
        except AgentError as exc:
            emit({"reply": msg.get("id"), "ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            emit({"reply": msg.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        wake.set()  # a fresh sample shows the result

    def reader():
        try:
            for line in stdin:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("interval") in MON_INTERVALS:
                    state["interval"] = float(msg["interval"])
                if msg.get("procs") in MON_FILTERS:
                    mon.filter = msg["procs"]
                    mon.scan_at = -1e9  # rescan now
                if "pause" in msg:
                    state["pause"] = bool(msg["pause"])
                if "kill" in msg or "end_game" in msg:
                    threading.Thread(target=act, args=(msg,), daemon=True).start()
                    continue
                wake.set()
        except (OSError, ValueError):
            pass
        state["stop"] = True
        wake.set()

    emit({"ready": 1, "static": mon.static()})
    threading.Thread(target=reader, daemon=True).start()
    ticks = 0
    parent = os.getppid()
    try:
        while not state["stop"]:
            if not state["pause"]:
                emit(mon.sample())
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            wake.clear()
            if sleep is time.sleep:
                wake.wait(state["interval"])
            else:
                sleep(state["interval"])
            if os.getppid() != parent:  # SSH session gone without closing stdin
                break
    except (OSError, ValueError, BrokenPipeError):
        pass
    return 0


COMMANDS = {n[4:]: f for n, f in globals().items() if n.startswith("cmd_")}


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "_keyboard":
        return keyboard_session(sys.stdin, sys.stdout)
    if len(sys.argv) >= 2 and sys.argv[1] == "_monitor":
        return monitor_session(sys.stdin, sys.stdout)
    if len(sys.argv) >= 3 and sys.argv[1] == "_shortcuts_worker":
        shortcuts_worker(sys.argv[2])
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "_purge_worker":
        purge_worker(sys.argv[2])
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "_move_worker":
        move_worker(sys.argv[2])
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "_xr_probe":
        xr_probe(sys.argv[2])
        return 0
    if len(sys.argv) >= 5 and sys.argv[1] == "_logcat_keeper":
        logcat_keeper(sys.argv[2], sys.argv[3], sys.argv[4])
        return 0
    if len(sys.argv) >= 4 and sys.argv[1] == "_dashboard_worker":
        dashboard_worker(sys.argv[2], sys.argv[3])
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == "_tools_worker":
        tools_worker(sys.argv[2])
        return 0
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(json.dumps({"ok": False, "error": f"usage: frameport_agent.py <{'|'.join(sorted(COMMANDS))}>"}))
        return 2
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        args = json.loads(raw) if raw.strip() else {}
        result = COMMANDS[sys.argv[1]](args)
        print(json.dumps({"ok": True, "result": result}))
        return 0
    except AgentError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
