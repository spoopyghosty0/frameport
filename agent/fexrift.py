#!/usr/bin/env python3
"""FramePort, experimental: Oculus Rift games that need Meta's PC runtime, on the Steam Frame.

Runs ON THE FRAME (python3 stdlib only), uploaded next to the binaries it installs (artifacts/fexrift) and started by
the FramePort agent (frameport_agent.py, commands fex_*). Everything lives under ~/.local/share/frameport/fexrift:
  GE-Proton11-7-x86_64/  GE-Proton x86_64, as released (verified SHA-512)
  ge/                    hard-linked copy that runs under FEX (wrappers, patched DLLs, native arm64 wineserver)
  x86libs/ vkthunk/      x86_64 GnuTLS for Wine (Arch archive, SHA-256 from the repo database), FEX Vulkan thunk
  revive/                Revive (patched copy uploaded by the PC)
  compat/pfx             the Wine prefix: Meta's runtime (from Meta's CDN, every signature check of Meta's installer
                         kept), the user's Meta login (imported from a PC prefix) and the games Meta's app installed
Commands (JSON on stdin or argv[2], JSON result on stdout):
  status | setup (long; progress in status.json) | import_login {tar} | install {app, anchor, title, ...}
  hidewin (launcher helper) | vrsetting {section, key, kind, value} (launcher helper)
Research and measurements: native/fexwine/README.md and CLAUDE.md "Rift via x86_64 Wine under FEX".
"""
import base64
import concurrent.futures as cf
import ctypes
import ctypes.util
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile
import zlib

VERSION = 1
HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, ".local/share/frameport/fexrift")
HERE = os.path.dirname(os.path.abspath(__file__))     # the uploaded artifacts folder
STEAM = os.path.join(HOME, ".local/share/Steam")
FEX_DIR = os.path.join(STEAM, "steamapps/common/FEX-Emu")
GE_NAME = "GE-Proton11-7-x86_64"
GE_URL = "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/GE-Proton11-7/GE-Proton11-7-x86_64.tar.gz"
GE_SHA512 = ("7db87e9787e20c35cbdac26018431d5794626b626e4067b050684e45a88cc2ca"
             "229d7d263519eafb2e168cde5bef57611065d159d3685aaec152ccb9abe3073f")
ARCH_REPO = "https://archive.archlinux.org/repos/2025/06/01/core/os/x86_64"   # glibc <= 2.41 like FEX's x86 root
TLS_PACKAGES = ("gnutls", "nettle", "leancrypto")
GAME_ID = 3999999001        # SteamAppId/SteamGameId the launcher sets (Proton only sets up VR with SteamGameId)
PE_DLLS = ("crypt32.dll", "sechost.dll", "dnsapi.dll", "windows.devices.enumeration.dll")
META_ROOT_WIN = "C:\\Program Files\\Meta Horizon\\"

GE = os.path.join(ROOT, GE_NAME)
GEF = os.path.join(ROOT, "ge")
COMPAT = os.path.join(ROOT, "compat")
PFX = os.path.join(COMPAT, "pfx")
X86LIBS = os.path.join(ROOT, "x86libs")
VKTHUNK = os.path.join(ROOT, "vkthunk")
REVIVE = os.path.join(ROOT, "revive")
STATUS = os.path.join(ROOT, "status.json")
LOG = os.path.join(ROOT, "setup.log")
USER_DIR = os.path.join(PFX, "drive_c", "users", "steamuser")
META_DIR = os.path.join(PFX, "drive_c", "Program Files", "Meta Horizon")


class FexError(Exception):
    pass


def log(msg):
    os.makedirs(ROOT, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(time.strftime("%H:%M:%S ") + msg + "\n")


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=1)
    os.replace(path + ".tmp", path)


def set_status(**kw):
    st = read_json(STATUS, {}) or {}
    st.update(kw, updated=int(time.time()))
    write_json(STATUS, st)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, path, sha=None, algo="sha256", tries=4):
    """Download to <path> (via .part), checking the hash; an existing file with the right hash is kept."""
    def ok():
        if not os.path.isfile(path):
            return False
        if not sha:
            return True
        h = hashlib.new(algo)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest() == sha
    if ok():
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FramePort"})
            with urllib.request.urlopen(req, timeout=120) as r, open(path + ".part", "wb") as out:
                shutil.copyfileobj(r, out, 1 << 20)
            os.replace(path + ".part", path)
            if ok():
                return path
            raise FexError(f"{os.path.basename(path)}: hash mismatch")
        except FexError:
            os.remove(path)
            raise
        except OSError as e:
            if i == tries - 1:
                raise FexError(f"download failed: {url}: {e}")
            time.sleep(3 * (i + 1))


# ------------------------------------------------------------------------------------------------ environment
def steam_display_env():
    """DISPLAY and friends from the running Steam (Wine needs gamescope's X server; SSH/agent sessions lack it)."""
    env = {}
    try:
        pid = subprocess.run(["pgrep", "-x", "steam"], capture_output=True, text=True).stdout.split()[0]
        with open(f"/proc/{pid}/environ", "rb") as f:
            for kv in f.read().split(b"\0"):
                k, _, v = kv.decode(errors="replace").partition("=")
                if k in ("DISPLAY", "WAYLAND_DISPLAY", "GAMESCOPE_WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE"):
                    env[k] = v
    except (IndexError, OSError):
        pass
    return env


def wine_env(extra=None):
    uid = os.getuid()
    env = dict(os.environ, STEAM_COMPAT_DATA_PATH=COMPAT, WINEPREFIX=PFX, WINEDEBUG="-all",
               WINEDLLOVERRIDES="winemenubuilder.exe=;mscoree=;mshtml=", XDG_RUNTIME_DIR=f"/run/user/{uid}",
               DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{uid}/bus", FP_NATIVE_WSERVER="1",
               PROTON_NO_NTSYNC="1", WINEFSYNC="0", WINEESYNC="0")
    env.update(steam_display_env())
    env.update(extra or {})
    return env


def wine(*args, check=True, timeout=900, env=None):
    p = subprocess.run([os.path.join(GEF, "files/bin/wine"), *args], env=env or wine_env(), capture_output=True,
                       text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise FexError(f"wine {' '.join(args)} -> {p.returncode}: {(p.stderr or p.stdout)[-400:]}")
    return p


def wineserver_kill():
    subprocess.run([os.path.join(GEF, "files/bin/wineserver"), "-k"], env=wine_env(), capture_output=True,
                   timeout=60)


def winpath(path):
    return "Z:" + os.path.abspath(path).replace("/", "\\")


# ------------------------------------------------------------------------------------------------ setup steps
def step_proton():
    """GE-Proton x86_64 as released: download, SHA-512, extract."""
    marker = os.path.join(GE, ".fp-verified")
    if os.path.isfile(marker):
        return "present"
    tgz = download(GE_URL, os.path.join(ROOT, "dl", GE_NAME + ".tar.gz"), GE_SHA512, "sha512")
    shutil.rmtree(GE, ignore_errors=True)
    subprocess.run(["tar", "-xzf", tgz, "-C", ROOT], check=True)
    if not os.path.isfile(os.path.join(GE, "proton")):
        raise FexError("GE-Proton archive layout changed")
    open(marker, "w").close()
    os.remove(tgz)
    return "installed"


FEXRUN = r"""#!/bin/sh
# FramePort: run an x86_64 program of GE-Proton under FEX (generated by fexrift.py)
unset FEX_APP_CONFIG FEX_APP_CONFIG_LOCATION
export LD_LIBRARY_PATH="{x86libs}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"
# FP_VKTHUNK=1: libvulkan.so.1 = FEX's guest thunk -> the Frame's native Vulkan loader + Turnip
[ -n "$FP_VKTHUNK" ] && export LD_LIBRARY_PATH="{vkthunk}:$LD_LIBRARY_PATH"
# FP_MEM=1: guest preload fp_mem.so (FEX_ENV: the compat tool deletes LD_PRELOAD)
[ -n "$FP_MEM" ] && export FEX_ENV="LD_PRELOAD={fp_mem}"
exec "{fex}/fex-compat-tool" run -- "$@"
"""
WINE_WRAPPER = '#!/bin/sh\nexec "{fexrun}" "{bin}.x86" "$@"\n'
WINESERVER_WRAPPER = ('#!/bin/sh\n# FP_NATIVE_WSERVER=1: native arm64 build of the same GE source (protocol and requests '
                      'match GE\'s x86_64 server)\n[ -n "$FP_NATIVE_WSERVER" ] && exec "{bin}.arm64" "$@"\n'
                      'exec "{fexrun}" "{bin}.x86" "$@"\n')


def step_fex_copy():
    """ge/: a hard-linked copy of GE-Proton whose x86_64 entry points go through FEX. Files FramePort replaces are
    removed first and written fresh (never written through a hard link into the pristine copy)."""
    if not os.path.isdir(GEF):
        subprocess.run(["cp", "-al", GE, GEF], check=True)
    fexrun = os.path.join(GEF, "fexrun")
    put_text(fexrun, FEXRUN.format(x86libs=X86LIBS, vkthunk=VKTHUNK, fp_mem=os.path.join(ROOT, "fp_mem.so"),
                                   fex=FEX_DIR), 0o755)
    bindir = os.path.join(GEF, "files/bin")
    for name in ("wine", "wineserver"):
        x86 = os.path.join(bindir, name + ".x86")
        if not os.path.isfile(x86):
            replace_file(os.path.join(GE, "files/bin", name), x86)
    replace_file(os.path.join(HERE, "wineserver.arm64"), os.path.join(bindir, "wineserver.arm64"), 0o755)
    put_text(os.path.join(bindir, "wine"), WINE_WRAPPER.format(fexrun=fexrun, bin=os.path.join(bindir, "wine")), 0o755)
    put_text(os.path.join(bindir, "wineserver"),
             WINESERVER_WRAPPER.format(fexrun=fexrun, bin=os.path.join(bindir, "wineserver")), 0o755)
    # Proton runs two helpers (steam.exe, umu.exe) through wine-preloader directly: through FEX instead
    script = os.path.join(GEF, "proton")
    with open(os.path.join(GE, "proton")) as f:
        text = f.read()
    new, n = re.subn(r'argv = \[g_proton\.lib_dir \+ "/wine/x86_64-unix/wine-preloader"',
                     f'argv = [{fexrun!r}, g_proton.lib_dir + "/wine/x86_64-unix/wine-preloader"', text)
    if n != 2:
        raise FexError(f"Proton script layout changed ({n} preloader calls)")
    put_text(script, new, 0o755)
    replace_file(os.path.join(HERE, "fp_mem.so"), os.path.join(ROOT, "fp_mem.so"))
    return "ok"


def step_dlls():
    lib = os.path.join(GEF, "files/lib/wine/x86_64-windows")
    for name in PE_DLLS:
        replace_file(os.path.join(HERE, "dlls", name), os.path.join(lib, name))
    return "ok"


def step_tls():
    """x86_64 GnuTLS (+ nettle, leancrypto): Wine's TLS (Meta's HTTPS) under FEX; FEX's x86 root has none."""
    want = {p: None for p in TLS_PACKAGES}
    marker = os.path.join(X86LIBS, ".fp-ok")
    if os.path.isfile(marker):
        return "present"
    work = os.path.join(ROOT, "dl", "tls")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    db = download(ARCH_REPO + "/core.db", os.path.join(work, "core.db"))
    with tarfile.open(db) as t:
        for m in t.getmembers():
            if not m.name.endswith("/desc"):
                continue
            parts = {}
            for block in t.extractfile(m).read().decode().strip().split("\n\n"):
                lines = block.split("\n")
                parts[lines[0]] = lines[1:]
            name = parts.get("%NAME%", [""])[0]
            if name in want:
                want[name] = (parts["%FILENAME%"][0], parts["%SHA256SUM%"][0])
    if not all(want.values()):
        raise FexError(f"Arch repo snapshot lacks {[k for k, v in want.items() if not v]}")
    os.makedirs(X86LIBS, exist_ok=True)
    for name, (fname, sha) in want.items():
        pkg = download(f"{ARCH_REPO}/{fname}", os.path.join(work, fname), sha)
        subprocess.run(["tar", "--zstd", "-xf", pkg, "-C", work, "usr/lib"], check=True)
    for f in os.listdir(os.path.join(work, "usr/lib")):
        if re.match(r"lib(gnutls|nettle|hogweed|leancrypto)\.so", f):
            src = os.path.join(work, "usr/lib", f)
            dst = os.path.join(X86LIBS, f)
            if os.path.lexists(dst):
                os.remove(dst)
            if os.path.islink(src):
                os.symlink(os.readlink(src), dst)
            else:
                shutil.copy2(src, dst)
    shutil.rmtree(work, ignore_errors=True)
    open(marker, "w").close()
    return "installed"


def step_vkthunk():
    guest = os.path.join(FEX_DIR, "usr/share/fex-emu/GuestThunks/libvulkan-guest.so")
    if not os.path.isfile(guest):
        raise FexError("FEX's Vulkan guest thunk is missing (FEX app 3127680 not installed?)")
    os.makedirs(VKTHUNK, exist_ok=True)
    for name in ("libvulkan.so", "libvulkan.so.1"):
        p = os.path.join(VKTHUNK, name)
        if os.path.lexists(p):
            os.remove(p)
        os.symlink(guest, p)
    return "ok"


def step_revive():
    src = os.path.join(HERE, "revive")
    if not os.path.isdir(src):
        raise FexError("Revive missing from the upload")
    tmp = REVIVE + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp)
    shutil.rmtree(REVIVE, ignore_errors=True)
    os.replace(tmp, REVIVE)
    return "ok"


def step_prefix():
    """The prefix as Proton creates it (default_pfx, wine-mono for Meta's .NET helpers)."""
    if os.path.isfile(os.path.join(PFX, "system.reg")):
        return "present"
    os.makedirs(COMPAT, exist_ok=True)
    env = wine_env({"STEAM_COMPAT_CLIENT_INSTALL_PATH": STEAM, "PROTON_NO_ZENITY": "1", "PROTONFIXES_DISABLE": "1",
                    "SteamAppId": str(GAME_ID), "SteamGameId": str(GAME_ID), "STEAM_COMPAT_APP_ID": str(GAME_ID)})
    env.pop("WINEDLLOVERRIDES", None)
    env.pop("STEAM_COMPAT_INSTALL_PATH", None)
    p = subprocess.run([os.path.join(GEF, "proton"), "run", "cmd", "/c", "exit"], env=env, capture_output=True,
                       text=True, timeout=1200)
    wineserver_kill()
    if not os.path.isfile(os.path.join(PFX, "system.reg")):
        raise FexError(f"prefix not created ({p.returncode}): {(p.stderr or p.stdout)[-400:]}")
    return "created"


def step_meta():
    """Meta's PC runtime from Meta's CDN (see meta_* below)."""
    marker = os.path.join(META_DIR, ".fp-meta.json")
    if os.path.isfile(marker):
        return "present"
    cfg_raw = meta_get("https://graph.oculus.com/bootstrap_installer_config?channel_name=LIVE&access_token=" + META_TOKEN)
    versions = meta_install(json.loads(cfg_raw), os.path.join(ROOT, "meta-pkgs"))
    write_json(marker, {"versions": versions, "installed": int(time.time())})
    return "installed " + ", ".join(f"{k} {v}" for k, v in versions.items())


DIGICERT_ROOT_SHA1 = "0563B8630D62D75ABBC8AB1E4BDFB5A899B24D43"   # DigiCert Assured ID Root CA (public)
LIBRARY_GUID = "4f1d6a2e-8c3b-4e57-9a61-f7a0e5d2c814"            # FramePort's id for Meta's default library


def step_fixes():
    """Prefix settings the Meta runtime needs under Wine (research 2026-10-08/09, see CLAUDE.md)."""
    der = open(os.path.join(HERE, "digicert-assured-id-root-ca.der"), "rb").read()
    if hashlib.sha1(der).hexdigest().upper() != DIGICERT_ROOT_SHA1:
        raise FexError("root certificate file damaged")
    path = os.path.join(PFX, "drive_c", "frameport-fixes.reg")
    with open(path, "w", encoding="utf-16") as f:
        f.write(fixes_reg(der))
    wine("regedit", "/S", r"C:\frameport-fixes.reg")
    shutil.copyfile(os.path.join(HERE, "windows.devices.wifi.dll"),
                    os.path.join(PFX, "drive_c", "windows", "system32", "windows.devices.wifi.dll"))
    # the runtime tests its download folder through Wine's C: volume name
    link = os.path.join(PFX, "dosdevices", "volume{00000000-0000-0000-0000-000000000043}")
    if not os.path.lexists(link):
        os.symlink("../drive_c", link)
    os.makedirs(os.path.join(META_DIR, "Software", "Software"), exist_ok=True)
    os.makedirs(os.path.join(META_DIR, "Software", "Manifests"), exist_ok=True)
    wineserver_kill()
    return "ok"


def fixes_reg(der):
    """The .reg text of step_fixes (regedit format, CRLF)."""
    blob = struct.pack("<III", 0x20, 1, len(der)) + der            # serialized store element: CERT_CERT_PROP_ID
    hexblob = ",".join(f"{b:02x}" for b in blob)
    revive_win = winpath(REVIVE).replace("\\", "\\\\")
    reg = "\r\n".join([
        "Windows Registry Editor Version 5.00", "",
        # OculusAppFramework validates its signer chain against Meta's templates: Wine must build the 4-certificate
        # chain via DigiCert Assured ID Root CA (with the crypt32 newest-issuer patch)
        rf"[HKEY_LOCAL_MACHINE\Software\Microsoft\SystemCertificates\Root\Certificates\{DIGICERT_ROOT_SHA1}]",
        f'"Blob"=hex:{hexblob}', "",
        # Meta's runtime relaunches/crash dialogs would hang a launch
        r"[HKEY_CURRENT_USER\Software\Wine\WineDbg]", '"ShowCrashDialog"=dword:00000000', "",
        # Revive's action manifest is found through this key (else SteamVR legacy input: focus losses)
        r"[HKEY_LOCAL_MACHINE\Software\Revive]", f'@="{revive_win}"', "",
        r"[HKEY_LOCAL_MACHINE\Software\WOW6432Node\Revive]", f'@="{revive_win}"', "",
        # Meta's default library (games Meta's app installs: Software\Software\<app> + Software\Manifests). Without
        # it the runtime lists an imported game as "install_available" and the game waits forever at startup.
        # Meta's app writes the path with the volume name of C: (Wine's mountmgr: Volume{...0043})
        r"[HKEY_CURRENT_USER\Software\Oculus VR, LLC\Oculus\Libraries]", f'"DefaultLibrary"="{LIBRARY_GUID}"', "",
        rf"[HKEY_CURRENT_USER\Software\Oculus VR, LLC\Oculus\Libraries\{LIBRARY_GUID}]",
        '"OriginalPath"="C:\\\\Program Files\\\\Meta Horizon\\\\Software"',
        '"Path"="\\\\\\\\?\\\\Volume{00000000-0000-0000-0000-000000000043}\\\\Program Files\\\\Meta Horizon\\\\Software"',
        "",
        # Air Link code in the runtime needs a Windows.Devices.WiFi.WiFiAdapter class (FramePort's stand-in)
        r"[HKEY_LOCAL_MACHINE\Software\Microsoft\WindowsRuntime\ActivatableClassId\Windows.Devices.WiFi.WiFiAdapter]",
        '"DllPath"="C:\\\\windows\\\\system32\\\\windows.devices.wifi.dll"', '"ActivationType"=dword:00000000',
        '"Threading"=dword:00000000', "",
    ]) + "\r\n"
    return reg


STEPS = [("proton", step_proton), ("fex", step_fex_copy), ("dlls", step_dlls), ("tls", step_tls),
         ("vkthunk", step_vkthunk), ("revive", step_revive), ("prefix", step_prefix), ("meta", step_meta),
         ("fixes", step_fixes)]


def cmd_setup(args):
    only = set(args.get("steps") or [])
    set_status(state="running", step=None, error=None, done=[], version=VERSION)
    done = []
    for name, fn in STEPS:
        if only and name not in only:
            continue
        set_status(step=name)
        log(f"step {name}")
        try:
            res = fn()
        except Exception as e:     # noqa: BLE001 - report every failure as the step's error
            log(f"step {name} failed: {e}")
            set_status(state="failed", error=f"{name}: {e}")
            raise
        log(f"step {name}: {res}")
        done.append({"step": name, "result": res})
        set_status(done=done)
    set_status(state="done", step=None)
    return {"done": done}


def cmd_status(args):
    st = read_json(STATUS, {}) or {}
    sessions = os.path.join(USER_DIR, "AppData", "Roaming", "Oculus", "sessions")
    apps = sorted(d for d in os.listdir(os.path.join(META_DIR, "Software", "Software"))
                  if not d.startswith(".")) if os.path.isdir(os.path.join(META_DIR, "Software", "Software")) else []
    return {"version": VERSION, "root": ROOT, "setup": st,
            "proton": os.path.isfile(os.path.join(GE, ".fp-verified")), "fex": os.path.isdir(FEX_DIR),
            "prefix": os.path.isfile(os.path.join(PFX, "system.reg")),
            "meta": read_json(os.path.join(META_DIR, ".fp-meta.json")),
            "login": os.path.isdir(sessions) and bool(os.listdir(sessions)), "apps": apps}


# ------------------------------------------------------------------------------------------------ login import
LOGIN_TREES = {   # tar top-level folder -> place in the prefix
    "sessions": os.path.join("drive_c", "users", "steamuser", "AppData", "Roaming", "Oculus", "sessions"),
    "CoreData": os.path.join("drive_c", "Program Files", "Meta Horizon", "CoreData"),
    "Manifests": os.path.join("drive_c", "Program Files", "Meta Horizon", "Software", "Manifests"),
    "Software": os.path.join("drive_c", "Program Files", "Meta Horizon", "Software", "Software"),
}


def safe_members(t):
    for m in t.getmembers():
        parts = m.name.replace("\\", "/").split("/")
        if m.name.startswith("/") or ".." in parts or parts[0] not in LOGIN_TREES or m.issym() or m.islnk() \
                or not (m.isfile() or m.isdir()):
            raise FexError(f"unexpected entry in login archive: {m.name!r}")
        yield m


def cmd_import_login(args):
    """The user's Meta login (sessions/ + CoreData) and the apps Meta's app downloaded, from a tar the PC built out of
    a signed-in prefix. sessions/ holds sign-in tokens: written owner-only, the archive is deleted."""
    tar = args["tar"]
    if not os.path.isfile(os.path.join(PFX, "system.reg")):
        raise FexError("run setup first")
    wineserver_kill()
    stage = os.path.join(ROOT, "login-stage")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, mode=0o700)
    try:
        with tarfile.open(tar) as t:
            t.extractall(stage, members=list(safe_members(t)))
        got = {}
        for top, rel in LOGIN_TREES.items():
            src = os.path.join(stage, top)
            if not os.path.isdir(src):
                continue
            dst = os.path.join(PFX, rel)
            os.makedirs(dst, exist_ok=True)
            if top in ("sessions", "CoreData"):
                shutil.rmtree(dst)
                shutil.copytree(src, dst)
                got[top] = len(os.listdir(dst))
            else:   # Manifests/ files and Software/<app> folders replace their own names only
                for name in os.listdir(src):
                    target = os.path.join(dst, name)
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    elif os.path.exists(target):
                        os.remove(target)
                    shutil.move(os.path.join(src, name), target)
                got[top] = sorted(os.listdir(src) if os.path.isdir(src) else []) or len(os.listdir(dst))
        sess = os.path.join(PFX, LOGIN_TREES["sessions"])
        for root, dirs, files in os.walk(sess):
            os.chmod(root, 0o700)
            for f in files:
                os.chmod(os.path.join(root, f), 0o600)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        try:
            os.remove(tar)
        except OSError:
            pass
    return {"imported": got, "status": cmd_status({})}


# ------------------------------------------------------------------------------------------------ games
# Per-game fixes for games verified on the Frame. "ime_patch": Unreal's FWindowsApplication constructor stores
# Slate.DeferWindowsMessageProcessing = 1 (`mov dword ptr [rsi+0x120], 1`): deferred IME messages deadlock against
# Wine's IME window ~1 start in 3 -> stored 0 (exe RVA, expected bytes, new bytes, exe SHA-256 before).
GAMES = {
    "oculus-first-contact": {
        "title": "Oculus First Contact",
        "exe": "TouchNUX/Binaries/Win64/TouchNUX-Win64-Shipping.exe",
        "args": ['-gamemode="experienceonly"'],
        "ime_patch": {"rva": 0x22a605, "old": "c78620010000" "01000000", "new": "c78620010000" "00000000"},
        # Unreal's large-block allocator gives 0.5-1.6 GB blocks back to the OS every few seconds and this game
        # overwrites them completely: fp_mem keeps them (see native/fexwine/fp_mem.c)
        "env": {"FP_MEM": "1", "FP_BIGCACHE_MB": "256"},
        "engine_ini": "TouchNUX/Saved/Config/WindowsNoEditor/Engine.ini",
    },
}


def pe_offset(data, rva):
    pe = struct.unpack_from("<I", data, 0x3c)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    sec = pe + 24 + struct.unpack_from("<H", data, pe + 20)[0]
    for i in range(nsec):
        _, vsz, va, rsz, raw = struct.unpack_from("<8sIIII", data, sec + 40 * i)
        if va <= rva < va + max(vsz, rsz):
            return raw + rva - va
    raise FexError(f"RVA {rva:#x} not in any section")


def apply_byte_patch(path, patch):
    """Patch bytes at an RVA if they are the expected ones; .orig keeps the original once. Returns a state word."""
    with open(path, "rb") as f:
        data = bytearray(f.read())
    off = pe_offset(data, patch["rva"])
    old, new = bytes.fromhex(patch["old"]), bytes.fromhex(patch["new"])
    cur = bytes(data[off:off + len(old)])
    if cur == new:
        return "already"
    if cur != old:
        return "unknown build"     # another version of the game: leave it alone
    if not os.path.exists(path + ".orig"):
        shutil.copy2(path, path + ".orig")
    data[off:off + len(new)] = new
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)
    return "patched"


def set_ini_values(path, section, values):
    """Set keys in one section of an Unreal ini (CRLF kept), adding the section if needed."""
    text = open(path, newline="").read() if os.path.isfile(path) else ""
    nl = "\r\n" if "\r\n" in text or not text else "\n"
    lines = text.split(nl) if text else []
    try:
        start = lines.index(f"[{section}]")
    except ValueError:
        while lines and not lines[-1]:
            lines.pop()
        lines += ([""] if lines else []) + [f"[{section}]"]
        start = len(lines) - 1
    end = start + 1
    while end < len(lines) and not lines[end].startswith("["):
        end += 1
    body = [ln for ln in lines[start + 1:end] if ln.split("=", 1)[0] not in values]
    while body and not body[-1]:
        body.pop()
    body += [f"{k}={v}" for k, v in values.items()] + [""]
    lines[start + 1:end] = body
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        f.write(nl.join(lines))


LAUNCHER = r"""#!/usr/bin/env bash
# Steam Frame launcher for {title}: Oculus Rift game with Meta's PC runtime, x86_64 GE-Proton under FEX
# (FramePort, experimental). Generated by fexrift.py.
R={root_q}
A={anchor_q}
G="$R/ge"
exec 9>"$A/.launch.lock"; flock -n 9 || {{ echo "already running" >>"$A/launch-dup.log"; exit 0; }}
exec >"$A/launch.log" 2>&1
echo "start $(date +%s)" >>"$A/plays.log" 2>/dev/null || true
export STEAM_COMPAT_DATA_PATH="$R/compat" WINEPREFIX="$R/compat/pfx" WINEDLLOVERRIDES="winemenubuilder.exe="
export STEAM_COMPAT_CLIENT_INSTALL_PATH={steam_q} PROTON_NO_ZENITY=1 PROTONFIXES_DISABLE=1 WINEDEBUG=-all
unset STEAM_COMPAT_INSTALL_PATH STEAM_COMPAT_LIBRARY_PATHS
export SteamAppId={gid} SteamGameId={gid} STEAM_COMPAT_APP_ID={gid}
export XR_RUNTIME_JSON=/opt/steamvr/steamxr_linux64.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
# speed (native/fexwine/README.md): native arm64 wineserver + ntsync, FEX without x86 memory ordering, Vulkan thunk
export FP_NATIVE_WSERVER=1 FP_VKTHUNK=1 FEX_TSOENABLED=0 FEX_VECTORTSOENABLED=0 FEX_MEMCPYSETTSOENABLED=0
export FEX_MULTIBLOCK=1 FEX_PROFILESTATS=0
{game_env}if [[ -z "${{DISPLAY:-}}" ]]; then
    steam_pid=$(pgrep -x steam | head -n1 || true)
    [[ -n "$steam_pid" ]] && while IFS= read -r -d '' kv; do case "$kv" in
        DISPLAY=*|WAYLAND_DISPLAY=*|GAMESCOPE_WAYLAND_DISPLAY=*|XAUTHORITY=*|XDG_SESSION_TYPE=*) export "$kv";; esac
    done < "/proc/$steam_pid/environ"
fi
cleanup() {{
    kill $LK $HW $WD 2>/dev/null
    "$G/files/bin/wineserver" -k 2>/dev/null
    echo "end $(date +%s)" >>"$A/plays.log" 2>/dev/null || true
}}
trap cleanup EXIT
python3 -I "$R/artifacts/fexrift.py" vrsetting '{vrsetting}' || true
"$G/files/bin/wineserver" -k 2>/dev/null; sleep 1
echo "FramePort: starting Meta's runtime"
L="$WINEPREFIX/drive_c/users/steamuser/AppData/Local/Oculus"
( cd "$WINEPREFIX/drive_c/Program Files/Meta Horizon/Support/oculus-runtime" && setsid "$G/files/bin/wine" ./OVRServer_x64.exe >"$A/ovrserver.log" 2>&1 </dev/null & )
# OVRLibrarian (.NET) finishes but never exits under FEX, and the runtime then tracks no later client (the game):
# end it once it reports completion, and whenever it comes back
for i in $(seq 1 45); do sleep 2; tail -n 3 "$L/OVRLibrarian.log" 2>/dev/null | grep -q "invocation completed" && pgrep -f "[O]VRLibrarian.exe" >/dev/null && break; done
sleep 3; pkill -9 -f "[O]VRLibrarian.exe"; sleep 10
( while sleep 5; do pkill -9 -f "[O]VRLibrarian.exe"; done ) & LK=$!
python3 -I "$R/artifacts/fexrift.py" hidewin & HW=$!
# Steam's reaper gone (Exit game) -> end the game
( pp=$PPID; while sleep 2; do kill -0 "$pp" 2>/dev/null || {{ "$G/files/bin/wineserver" -k; break; }}; done ) & WD=$!
echo "FramePort: starting {title}"
cd {gamedir_q}
taskset -c 4-7 "$G/proton" run "$R/revive/ReviveInjector.exe" {exe_q} {args}
"""


def write_launcher(app, info, anchor, msaa=None):
    game_dir = os.path.join(META_DIR, "Software", "Software", app)
    exe = os.path.join(game_dir, info["exe"])
    env = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in info.get("env", {}).items())
    vr = json.dumps({"section": f"steam.app.{GAME_ID}", "key": "motionSmoothingOverride", "kind": "i", "value": 2})
    text = LAUNCHER.format(title=info["title"], root_q=shlex.quote(ROOT), anchor_q=shlex.quote(anchor),
                           steam_q=shlex.quote(STEAM), gid=GAME_ID, game_env=env, vrsetting=vr,
                           gamedir_q=shlex.quote(os.path.dirname(exe)), exe_q=shlex.quote(exe),
                           args=" ".join(info.get("args", [])))
    put_text(os.path.join(anchor, "launch.sh"), text, 0o755)
    return exe


def cmd_install(args):
    """Install a game Meta's app downloaded (now in the prefix) for Steam: fixes + launcher in its anchor."""
    app, anchor = args["app"], args["anchor"]
    info = GAMES.get(app)
    if not info:
        raise FexError(f"{app} isn't a verified game for this mode (verified: {', '.join(GAMES)})")
    game_dir = os.path.join(META_DIR, "Software", "Software", app)
    exe = os.path.join(game_dir, info["exe"])
    if not os.path.isfile(exe):
        raise FexError(f"{app} isn't in the prefix: import it with the login first")
    res = {"exe": exe, "game_dir": game_dir}
    if info.get("ime_patch"):
        res["ime_patch"] = apply_byte_patch(exe, info["ime_patch"])
    msaa = args.get("msaa")
    if info.get("engine_ini") and msaa in (1, 2, 4):
        ini = os.path.join(USER_DIR, "AppData", "Local", info["engine_ini"])
        set_ini_values(ini, "SystemSettings", {"r.MobileMSAA": str(msaa)})
        set_ini_values(ini, "/Script/Engine.RendererSettings", {"r.MobileMSAA": str(msaa)})
        res["msaa"] = msaa
    os.makedirs(anchor, exist_ok=True)
    write_launcher(app, info, anchor)
    res["launcher"] = os.path.join(anchor, "launch.sh")
    res["title"] = info["title"]
    return res


# ------------------------------------------------------------------------------------------------ launcher helpers
def cmd_hidewin(args):
    """Unmap Proton's 400x300 "Steam" bridge window so it doesn't show in SteamVR's dashboard (loops until killed)."""
    x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                             ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                             ctypes.POINTER(ctypes.c_uint)]
    x.XFetchName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_char_p)]
    x.XUnmapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x.XFlush.argtypes = [ctypes.c_void_p]
    x.XFree.argtypes = [ctypes.c_void_p]
    os.environ.update(steam_display_env())
    dpy = x.XOpenDisplay(None)
    if not dpy:
        return {"error": "no display"}
    hidden = set()
    while True:
        root, parent = ctypes.c_ulong(), ctypes.c_ulong()
        children, n = ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
        if x.XQueryTree(dpy, x.XDefaultRootWindow(dpy), ctypes.byref(root), ctypes.byref(parent),
                        ctypes.byref(children), ctypes.byref(n)):
            for i in range(n.value):
                w = children[i]
                name = ctypes.c_char_p()
                if w not in hidden and x.XFetchName(dpy, w, ctypes.byref(name)) and name.value == b"Steam":
                    x.XUnmapWindow(dpy, w)
                    hidden.add(w)
                if name.value:
                    x.XFree(name)
            if children:
                x.XFree(children)
            x.XFlush(dpy)
        time.sleep(2)


def vr_settings():
    lib = ctypes.CDLL("/opt/steamvr/bin/linuxarm64/vrclient.so")
    lib.VRClientCoreFactory.restype = ctypes.c_void_p
    lib.VRClientCoreFactory.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]

    def method(obj, index, restype, *argtypes):
        vtbl = ctypes.cast(ctypes.c_void_p(obj), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])
        return lambda *a: fn(obj, *a)
    rc = ctypes.c_int(0)
    core = lib.VRClientCoreFactory(b"IVRClientCore_003", ctypes.byref(rc))
    if method(core, 0, ctypes.c_int, ctypes.c_int, ctypes.c_char_p)(3, None):    # VRApplication_Background
        raise FexError("SteamVR isn't running")
    settings = method(core, 3, ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int))(
        b"IVRSettings_003", ctypes.byref(rc))
    return core, method, settings


def cmd_vrsetting(args):
    """Set one SteamVR setting live (SteamVR persists it): e.g. this game's motion smoothing off."""
    core, method, s = vr_settings()
    err = ctypes.c_int(0)
    sec, key = args["section"].encode(), args["key"].encode()
    kind = args.get("kind", "i")
    if kind == "b":
        method(s, 1, None, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_bool, ctypes.POINTER(ctypes.c_int))(
            sec, key, bool(args["value"]), ctypes.byref(err))
    elif kind == "f":
        method(s, 3, None, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_float, ctypes.POINTER(ctypes.c_int))(
            sec, key, float(args["value"]), ctypes.byref(err))
    else:
        method(s, 2, None, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_int))(
            sec, key, int(args["value"]), ctypes.byref(err))
    method(core, 1, None)()
    return {"error": err.value}


# ------------------------------------------------------------------------------------------------ Meta's runtime
# Installs Meta's PC app the way Meta's installer (Dawn/Daybreak) does, from Meta's CDN, keeping every integrity check:
#  - manifest zip: RSA (PKCS#1 v1.5 type 1, raw) signature with Meta's primary key, backup key on failure;
#    plaintext = lowercase hex SHA-256 of the zip bytes
#  - every segment: zlib, SHA-256 of the inflated bytes == its strong hash; every file: SHA-256 == manifest
#  - redistributables: SHA-256 == RSA-decrypted signature
# Steps that don't apply to Wine (kernel drivers, firewall, shortcuts, uninstaller entry) are left out.
META_TOKEN = urllib.parse.quote("OC|1582076955407037|")   # the installer's public app token
META_UA = "Oculus/Daybreak 1.16.0.0"
META_PACKAGES = ["oculus-librarian", "oculus-runtime", "oculus-client", "oculus-platform-runtime", "oculus-home",
                 "oculus-compat"]        # login + runtime + Platform SDK; drivers/dash/diagnostics aren't needed
META_REDISTS = ["visual-cpp-2013", "visual-cpp-2013-x86", "visual-cpp-2015-update-3", "visual-cpp-2017"]
META_KEYS_B64 = (   # Meta's public keys (Daybreak Crypto.cs)
    "MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAwMPHxGhq2xwwcDwHa6QQ0mRY2TUeWmA/tI2+KxCsLwRNZO3JgxBd0F8swwzgW5agEMax"
    "34HXQcWmQ81vOqTbr6FuDAavMwB6GnhOghilYMLTFp7msOlEsUsVU4l1hXNKVMKWEulFs/PlrlI0m60Zc0n9QEEPuJlWZRy1iFpgT7LbOtzoxy0r"
    "7rzMFukehovvAUlbAod8rPSPv14SYFwDlvfIbhAMpaYbTEXfQ3vk5YL2xN9k+ujNeMSF9tdMDe8RIwojmNjOggv7ziK0Ny6bnn+rLH5KJjqtjUgC"
    "0E1kjKJqFWvrHzQeN9s98I/S6APhMADLh2huEo41rHRnN3WgUNWemb2kOJeRodifG5w9p5/JipPmA1Esf9oQ0P8ZRldAMT1w5ARJuK8WsATqLvxT"
    "z/ZidlDMQ77sFe2BYstwxcLWLUKjpgTFZm6o2aSChMmI3Z8qY2vNOsFBtkPlsUE55hl5j1J34hvt3dT4MVmdzkF0SoxQFoh4J5jYhxqogHyqs1s7"
    "MdmeR27imCcDy3QftfBo2KwVyf4HBy/pNj/HzPiBJZ4b38rhg3Lf5Oys3HDGERh/Z2r8bG5yI+iQajFjvYiaoDfSEnF0g74K5Vj/jb0eRF+KJeeP"
    "EX+iKHegusSwLWY2IVCK1bcU6O2DHDUZCc9STeTXIodVEs3fpgroB+UCAwEAAQ==",
    "MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAxIYJZxwnDITmzlWBEfCYtRczVP6C87kkmE4sLDB9tLebO1137zvWRXuY5TGnUW4l5z7l"
    "/rRKUbKjrKmMcBpvwdlTFBbFvfCP9d+7AzPdYJUxR3nTZedEvTIeweX05yVeE1gxDLLR6DJ93tqZXg4oLkEmyY9alwXYXv0wA/W8p7LjfKrKnZAf"
    "jr85Z5ArNAHDctUjc+vF6mUTOmhp92gedodvYu4yekTEOE2tOh0Z9g0oAW4p0accjFCB6O2P6xwV+4xvxc6iIMfeE+fzIeN4ckKraxPTG4QOGXLO"
    "Dapm8iFIse38t0qmtPLSr2kFCcFjNanAjP+0wgEWjVUVoofFvfOHmc7OqMwnzB9lqUciGbrbX1cCcsnnP0cZW+EDMWiWui0sfzz+Pmw6AQDjUGZI"
    "WFF5K+uL/BoPbR198da0sEAonoBlhpj/6aNyC6N4WMoWerORHHjExvgbEWM/54Xp2ytzxsUOJd9PlymLcbHvqQ/5YIJuIsuOrDpTGAJN8u2erDE+"
    "8FqD5FDjSmgAO5XyEItLLSee/3mrxfPoKY2GnEb43B8ZS0tF8YKRC/SAIgEy999/paUW5vRKAjPBBrLujkPQwV9yvHFCDsGQQcyBFnE3Y1JyBbmV"
    "PUBjce8uqN7t/ji+OlO+mbVO48TwxmUbp9Mc4TeaQ7TNOPJW+7TCcQkCAwEAAQ==",
)


def _der(buf, pos):
    tag = buf[pos]
    length = buf[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(buf[pos:pos + n], "big")
        pos += n
    return tag, buf[pos:pos + length], pos + length


def rsa_key(b64):
    der = base64.b64decode(b64)
    _, spki, _ = _der(der, 0)
    _, _, p = _der(spki, 0)              # AlgorithmIdentifier
    _, bits, _ = _der(spki, p)           # BIT STRING
    _, rsa, _ = _der(bits[1:], 0)
    _, n, p = _der(rsa, 0)
    _, e, _ = _der(rsa, p)
    return int.from_bytes(n, "big"), int.from_bytes(e, "big")


def rsa_public_decrypt(sig_b64, keys=None):
    """RSA_public_decrypt with PKCS#1 type 1 padding; primary key, then the backup key (Crypto.cs)."""
    s = int.from_bytes(base64.b64decode(sig_b64), "big")
    for n, e in keys or [rsa_key(k) for k in META_KEYS_B64]:
        k = (n.bit_length() + 7) // 8
        if s >= n:
            continue
        m = pow(s, e, n).to_bytes(k, "big")
        if m[:2] != b"\x00\x01":
            continue
        i = 2
        while i < len(m) and m[i] == 0xFF:
            i += 1
        if i - 2 < 8 or i >= len(m) or m[i] != 0:
            continue
        return m[i + 1:].decode("ascii")
    raise FexError("signature doesn't decrypt with Meta's keys")


def meta_get(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": META_UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except OSError:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


MANIFEST_ORDER = ["packageType", "isCore", "appId", "canonicalName", "launchFile", "launchParameters",
                  "launchFile2D", "launchParameters2D", "version", "versionCode", "redistributables",
                  "firewallExceptionsRequired", "thirdParty", "manifestVersion", "parentCanonicalName", "files", "dlcs",
                  "assetFiles"]
MANIFEST_DEFAULTS = {"launchFile2D": None, "launchParameters2D": None, "redistributables": [],
                     "firewallExceptionsRequired": False, "thirdParty": False, "manifestVersion": 1,
                     "parentCanonicalName": None, "dlcs": {}, "assetFiles": {}}


def dawn_manifest(raw):
    """A manifest as Meta's installer (Daybreak ManifestUtil) writes it: property order, backslash file keys."""
    m = dict(MANIFEST_DEFAULTS)
    m.update(raw)
    m["files"] = {k.replace("/", "\\"): v for k, v in (raw.get("files") or {}).items()}
    out = {k: m[k] for k in MANIFEST_ORDER if k in m}
    out.update({k: v for k, v in m.items() if k not in out})
    return out


def write_manifest(path, m):
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)


def meta_fetch_app(app, manifest, cache):
    root = os.path.join(cache, app["canonical_name"])
    os.makedirs(root, exist_ok=True)

    def one(item):
        name, info = item
        path = os.path.join(root, *name.replace("\\", "/").split("/"))
        if os.path.isfile(path) and os.path.getsize(path) == info["size"] and sha256_file(path) == info["sha256"]:
            return 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        h = hashlib.sha256()
        with open(path + ".part", "wb") as out:
            for _weak, strong, _clen in info["segments"]:
                data = zlib.decompress(meta_get(app["segments_base_uri"] + "&segment_sha256=" + strong
                                                + "&access_token=" + META_TOKEN))
                if hashlib.sha256(data).hexdigest() != strong:
                    raise FexError(f"segment hash mismatch in {name}")
                h.update(data)
                out.write(data)
        if h.hexdigest() != info["sha256"]:
            raise FexError(f"file hash mismatch: {name}")
        os.replace(path + ".part", path)
        return info["size"]

    with cf.ThreadPoolExecutor(8) as pool:
        got = sum(pool.map(one, manifest["files"].items()))
    log(f"  {app['canonical_name']} {manifest.get('version')}: {len(manifest['files'])} files verified, "
        f"{got >> 20} MiB downloaded")
    return root


def meta_reg_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def meta_install(config, cache):
    apps = {a["canonical_name"]: a for a in config["applications"]}
    drive_c = os.path.join(PFX, "drive_c")
    versions = {}
    manifests = {}
    for name in META_PACKAGES:
        raw = meta_get(apps[name]["manifest_uri"] + "&access_token=" + META_TOKEN)
        if rsa_public_decrypt(apps[name]["manifest_signature"]) != hashlib.sha256(raw).hexdigest():
            raise FexError(f"manifest signature mismatch for {name}")
        manifests[name] = json.loads(zipfile.ZipFile(io.BytesIO(raw)).read("manifest.json"))
        versions[name] = manifests[name].get("version")
        log(f"  manifest {name} {versions[name]}: signature OK")
    for name in META_PACKAGES:
        src = meta_fetch_app(apps[name], manifests[name], cache)
        dst = os.path.join(META_DIR, "Support", name)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".manifest.json", "*.part"))
    os.makedirs(os.path.join(META_DIR, "Manifests"), exist_ok=True)
    for name, m in manifests.items():
        dm = dawn_manifest(m)
        write_manifest(os.path.join(META_DIR, "Manifests", name + ".json"), dm)
        write_manifest(os.path.join(META_DIR, "Manifests", name + ".json.mini"),
                       dict(dm, files={k: v for k, v in dm["files"].items() if k.endswith(".exe")}))
    for d in ("Downloads", "CoreData", "Software"):
        os.makedirs(os.path.join(META_DIR, d), exist_ok=True)
    # not a platform Meta supports: no crash reports to Meta (dump folders become read-only files)
    for user in os.listdir(os.path.join(drive_c, "users")):
        appdata = os.path.join(drive_c, "users", user, "AppData")
        if not os.path.isdir(appdata):
            continue
        for rel in (("Local", "Oculus", "OVRServerBreakpad"), ("Roaming", "Client", "Crashpad")):
            d = os.path.join(appdata, *rel)
            if os.path.isdir(d):
                shutil.rmtree(d)
            if not os.path.exists(d):
                os.makedirs(os.path.dirname(d), exist_ok=True)
                open(d, "w").close()
                os.chmod(d, 0o444)
        os.makedirs(os.path.join(appdata, "Roaming", "Microsoft", "Internet Explorer", "Quick Launch", "User Pinned",
                                 "TaskBar"), exist_ok=True)
    for r in config.get("redistributables", []):
        if r["canonical_name"] not in META_REDISTS:
            continue
        path = os.path.join(cache, "redists", r["canonical_name"] + ".exe")
        want = rsa_public_decrypt(r["signature"])
        if not (os.path.isfile(path) and sha256_file(path) == want):
            data = meta_get(r["uri"])
            if hashlib.sha256(data).hexdigest() != want:
                raise FexError(f"redistributable signature mismatch: {r['canonical_name']}")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        p = wine(path, *r.get("arguments", "").split(), check=False)
        log(f"  {r['name']}: signature OK, exit {p.returncode}")
    support = META_ROOT_WIN + "Support"
    reg = "\r\n".join([
        "Windows Registry Editor Version 5.00", "",
        r"[HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Oculus VR, LLC\Oculus]",
        f'"Base"={meta_reg_str(META_ROOT_WIN)}', '"Active"=dword:00000001', '"InitialInstallerVersion"="1.115.0.0"',
        '"Gestalt"=dword:00000002', "",
        r"[HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Oculus VR, LLC\Oculus\Config]",
        '"CoreChannel"="NO_UPDATES"', '"Gestalt"=dword:00000002', "",
        r"[HKEY_LOCAL_MACHINE\System\CurrentControlSet\Control\Session Manager\Environment]",
        f'"OculusBase"={meta_reg_str(META_ROOT_WIN)}', "",
    ]) + "\r\n"
    with open(os.path.join(drive_c, "frameport-meta.reg"), "w", encoding="utf-16") as f:
        f.write(reg)
    wine("regedit", "/S", r"C:\frameport-meta.reg")
    cur = wine("reg", "query", r"HKLM\System\CurrentControlSet\Control\Session Manager\Environment", "/v",
               "PATH").stdout
    path_val = next((ln.split("REG_EXPAND_SZ")[-1].split("REG_SZ")[-1].strip() for ln in cur.splitlines()
                     if "PATH" in ln.upper() and "REG_" in ln), "")
    rt = support + r"\oculus-runtime"
    if rt.lower() not in path_val.lower():
        wine("reg", "add", r"HKLM\System\CurrentControlSet\Control\Session Manager\Environment", "/v", "PATH",
             "/t", "REG_EXPAND_SZ", "/d", rt + ";" + path_val, "/f")
    # Wine has no interactive-session API, so Meta's launcher can't start OVRServer for the user (and restarts it
    # after every crash): the service stays disabled and the launcher starts OVRServer itself
    for svc, exe, start in (("OVRLibraryService", support + r"\oculus-librarian\OVRLibraryService.exe", "demand"),
                            ("OVRService", support + r"\oculus-runtime\OVRServiceLauncher.exe", "disabled")):
        wine("sc", "delete", svc, check=False)
        wine("sc", "create", svc, "binPath=", f'"{exe}"', "start=", start, check=False)
    wineserver_kill()
    return versions


# ------------------------------------------------------------------------------------------------ file helpers
def replace_file(src, dst, mode=None):
    """Write dst fresh (remove first: dst may be a hard link into the pristine GE-Proton copy)."""
    if not os.path.isfile(src):
        raise FexError(f"missing {src}")
    if os.path.lexists(dst):
        os.remove(dst)
    shutil.copy2(src, dst)
    if mode:
        os.chmod(dst, mode)


def put_text(path, text, mode=None):
    if os.path.lexists(path):
        os.remove(path)
    with open(path + ".tmp", "w") as f:
        f.write(text)
    if mode:
        os.chmod(path + ".tmp", mode)
    os.replace(path + ".tmp", path)


COMMANDS = {"status": cmd_status, "setup": cmd_setup, "import_login": cmd_import_login, "install": cmd_install,
            "hidewin": cmd_hidewin, "vrsetting": cmd_vrsetting}


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "status"
    if name not in COMMANDS:
        print(json.dumps({"ok": False, "error": f"unknown command {name}"}))
        return 2
    raw = sys.argv[2] if len(sys.argv) > 2 else ("" if sys.stdin.isatty() else sys.stdin.read())
    try:
        result = COMMANDS[name](json.loads(raw) if raw.strip() else {})
    except (FexError, OSError, subprocess.SubprocessError, KeyError, ValueError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps({"ok": True, "result": result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
