"""Local Rift copies in Meta libraries. Runs on the Frame, stdlib only; never touches entitlement or login data."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zlib

SEGMENT_SIZE = 10000000                       # Meta's manifest format uses decimal 10 MB, not MiB


def canonical(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", value):
        raise ValueError("invalid Meta canonical name")
    return value


def relative(value):
    if not isinstance(value, str):
        raise ValueError("invalid relative game path")
    value = value.replace("\\", "/")
    if (not value or value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/"))
            or any(c in value for c in ':\x00\r\n')):
        raise ValueError("game path escapes its folder")
    return value


def skipped(name):
    name = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return (name.endswith((".7z", ".ag")) or bool(re.search(r"\.7z\.0", name))
            or name in ("data.bin", "agrepackinstaller.exe", "vd.bat"))


def confined(path, root):
    """Reject symlink components too: an upload or delete must never follow a game-controlled link."""
    root, path = os.path.abspath(root), os.path.abspath(path)
    if os.path.commonpath([path, root]) != root:
        raise ValueError("path escapes the library")
    cursor = path
    while True:
        if os.path.islink(cursor):
            raise ValueError("symlink in local game path")
        if cursor == root:
            break
        cursor = os.path.dirname(cursor)
    if os.path.realpath(root) != root:
        raise ValueError("symlink in library root")
    return path


def hash_file(path):
    total, segments = hashlib.sha256(), []
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(SEGMENT_SIZE)
            if not chunk:
                break
            size += len(chunk)
            total.update(chunk)
            # The fixture's last element is the zlib-compressed length, not the uncompressed chunk size. The first
            # element is an opaque Meta segment identifier: 0 explicitly means unknown for a local-only copy. These
            # records register existing bytes; they are NOT a CDN download/repair recipe (device validation needed).
            segments.append([0, hashlib.sha256(chunk).hexdigest(), len(zlib.compress(chunk))])
    return {"sha256": total.hexdigest(), "size": size, "segmentSize": SEGMENT_SIZE, "segments": segments}


def register(core):
    def record_path(app):
        return os.path.join(core.ROOT, "local-games", canonical(app) + ".json")

    def read_record(app):
        return core.read_json(record_path(app)) or {}

    def save_record(app, record):
        os.makedirs(os.path.dirname(record_path(app)), exist_ok=True)
        path = record_path(app)
        with open(path + ".tmp", "w") as f:
            json.dump(record, f, indent=1)
        os.replace(path + ".tmp", path)

    def layout(app, dest):
        app = canonical(app)
        if dest:
            # The main agent validates this against list_drives / resolve_dest on EVERY command. No fallback when a
            # drive disappears. Keep the physical game path simple while Meta sees its required Software/ layout.
            if not os.path.isabs(dest) or not os.path.isdir(dest):
                raise ValueError("destination drive is not mounted")
            base = confined(os.path.join(dest, "FramePort", "fexrift-games"), dest)
            library = confined(os.path.join(dest, "FramePort", "fexrift-library"), dest)
            guid = str(uuid.uuid5(uuid.NAMESPACE_URL, "frameport-fex-library:" + dest))
        else:
            library = os.path.join(core.META_DIR, "Software")
            base, guid = os.path.join(library, "Software"), core.LIBRARY_GUID
        return {"game_dir": confined(os.path.join(base, app), base), "library": library,
                "library_guid": guid, "dest": dest}

    def validated_record(app):
        rec = read_record(app)
        if not rec:
            raise ValueError("no local-copy upload reservation for this game")
        expected = layout(app, rec.get("dest"))
        if any(rec.get(k) != v for k, v in expected.items()):
            raise ValueError("local-copy reservation has an invalid storage path")
        return rec

    def prepare(args):
        app = canonical(args["app"])
        package = args["package"]
        files, seen = {}, set()
        for name, st in args["files"].items():
            rel = relative(name)
            if skipped(rel) or rel.casefold() in seen:
                raise ValueError("skipped or duplicate file in upload inventory")
            if not isinstance(st["size"], int) or st["size"] < 0 or not isinstance(st["mtime_ns"], int):
                raise ValueError("invalid source file stat")
            seen.add(rel.casefold())
            files[rel] = st
        if not files:
            raise ValueError("no game files to upload")
        old = read_record(app)
        dest = args.get("dest") or None
        loc = layout(app, dest)
        if old and (old.get("dest") != dest or old.get("package") != package):
            raise ValueError("local copy already has another package or drive; uninstall it before changing either")
        if not old and (os.path.exists(os.path.join(loc["library"], "Manifests", app + ".json"))
                        or os.path.exists(loc["game_dir"])):
            raise ValueError("game already exists outside FramePort's local-copy installer")
        existing = {}
        for rel, st in files.items():
            path = confined(os.path.join(loc["game_dir"], rel), loc["game_dir"])
            confined(path + ".part", loc["game_dir"])
            if old.get("files", {}).get(rel) != st:
                # Delete completed bytes too: if this new attempt is cancelled before uploading them, the next
                # prepare must not mistake the old same-size file for a completed transfer of the new source.
                for stale in (path, path + ".part"):
                    if os.path.isfile(stale):
                        os.unlink(stale)
            if (old.get("files", {}).get(rel) == st and os.path.isfile(path)
                    and os.path.getsize(path) == st["size"]):
                existing[rel] = st["size"]
        if old and set(old.get("files", {})) - set(files):
            raise ValueError("source removed files since the last upload; uninstall the local copy before replacing it")
        os.makedirs(loc["game_dir"], exist_ok=True)
        st = os.statvfs(loc["game_dir"])
        save_record(app, dict(old, **loc, app=app, package=package, files=files))
        return dict(loc, existing=existing, free_bytes=st.f_bavail * st.f_frsize)

    def registry_library(rec, remove=False):
        guid, dest = rec["library_guid"], rec.get("dest")
        key = rf"HKEY_CURRENT_USER\Software\Oculus VR, LLC\Oculus\Libraries\{guid}"
        if remove:
            text = f"Windows Registry Editor Version 5.00\r\n\r\n[-{key}]\r\n"
        else:
            if dest:
                devices = os.path.join(core.PFX, "dosdevices")
                os.makedirs(devices, exist_ok=True)
                volume = os.path.join(devices, "volume{" + guid + "}")
                if os.path.lexists(volume):
                    if not os.path.islink(volume) or os.path.realpath(volume) != dest:
                        raise ValueError("Wine volume mapping is already in use")
                else:
                    os.symlink(dest, volume)
                # Do not appropriate a user's drive letter. Reuse our mount mapping, else pick the first unused one.
                letter = next((c for c in "defghijklmnopqrstuvwxy"
                               if os.path.islink(os.path.join(devices, c + ":"))
                               and os.path.realpath(os.path.join(devices, c + ":")) == dest), None)
                if not letter:
                    letter = next((c for c in "defghijklmnopqrstuvwxy"
                                   if not os.path.lexists(os.path.join(devices, c + ":"))), None)
                    if not letter:
                        raise ValueError("no free Wine drive letter")
                    os.symlink(dest, os.path.join(devices, letter + ":"))
                rec["drive_letter"] = letter
                original = letter.upper() + ":\\FramePort\\fexrift-library"
                path = "\\\\?\\Volume{" + guid + "}\\FramePort\\fexrift-library"
            else:
                original = "C:\\Program Files\\Meta Horizon\\Software"
                path = "\\\\?\\Volume{00000000-0000-0000-0000-000000000043}\\Program Files\\Meta Horizon\\Software"
            text = "Windows Registry Editor Version 5.00\r\n\r\n"
            if not dest:
                text += (r"[HKEY_CURRENT_USER\Software\Oculus VR, LLC\Oculus\Libraries]" + "\r\n"
                         + '"DefaultLibrary"=' + core.meta_reg_str(guid) + "\r\n\r\n")
            text += (f"[{key}]\r\n" + '"OriginalPath"=' + core.meta_reg_str(original) + "\r\n"
                     + '"Path"=' + core.meta_reg_str(path) + "\r\n")
        reg = os.path.join(core.ROOT, "local-library.reg")
        core.put_text(reg, text)
        core.wine("regedit", "/S", core.winpath(reg), timeout=90)

    def register_local(args):
        app = canonical(args["app"])
        rec = validated_record(app)
        if args.get("package") != rec["package"]:
            raise ValueError("upload belongs to another package")
        exe = relative(args["exe"])
        if exe not in rec["files"]:
            raise ValueError("launch program is not an uploaded file")
        app_id = args["app_id"]
        if not isinstance(app_id, str) or not re.fullmatch(r"[1-9][0-9]*", app_id):
            raise ValueError("invalid Meta app ID")
        files = {}
        for rel, st in rec["files"].items():
            path = confined(os.path.join(rec["game_dir"], rel), rec["game_dir"])
            if not os.path.isfile(path) or os.path.getsize(path) != st["size"]:
                raise ValueError(f"upload incomplete: {rel}")
            files[rel] = hash_file(path)
        library = rec["library"]
        os.makedirs(library, exist_ok=True)
        if rec.get("dest"):
            software = os.path.join(library, "Software")
            target = os.path.dirname(rec["game_dir"])
            if os.path.lexists(software):
                if not os.path.islink(software) or os.path.realpath(software) != target:
                    raise ValueError("external Meta library Software path is already in use")
            else:
                os.symlink(target, software)
        args_list = args.get("args") or []
        if not isinstance(args_list, list) or any(not isinstance(a, str) or "\x00" in a for a in args_list):
            raise ValueError("launch arguments must be strings")
        # list2cmdline follows Windows argv quoting; Meta's own launchFile uses forward slashes. The local launcher
        # keeps the original argv list in the reservation, avoiding POSIX reinterpretation of Windows quotes.
        manifest = core.dawn_manifest({"packageType": "APP", "isCore": False, "appId": app_id,
                                      "canonicalName": app, "launchFile": exe,
                                      "launchParameters": subprocess.list2cmdline(args_list),
                                      "launchParameters2D": "", "version": str(args.get("version") or "0.0.0"),
                                      "versionCode": 1, "files": files})
        # App fixtures serialize the manifest's derived properties before the package base properties. Match that
        # order (core's runtime package writer uses base-first); both use the same defaults, CRLF and indentation.
        first = ("parentCanonicalName", "files", "dlcs", "assetFiles")
        manifest = {**{k: manifest[k] for k in first}, **{k: v for k, v in manifest.items() if k not in first}}
        manifests = os.path.join(library, "Manifests")
        os.makedirs(manifests, exist_ok=True)
        for suffix, content in ((".json", manifest), (".json.mini", dict(manifest, files={
                k: v for k, v in manifest["files"].items() if k.lower().endswith(".exe")}))):
            path = os.path.join(manifests, app + suffix)
            core.write_manifest(path + ".tmp", content)
            os.replace(path + ".tmp", path)
        registry_library(rec)
        rec.update({k: args.get(k) for k in ("app_id", "app_id_source", "store_app_id", "skipped", "title")})
        rec.update(source="local_copy", exe=exe, args=args_list, registered=True)
        save_record(app, rec)
        return local_info({"app": app})

    def local_info(args):
        rec = validated_record(args["app"])
        return {k: rec.get(k) for k in ("source", "app_id", "app_id_source", "store_app_id", "skipped", "dest",
                                        "package", "game_dir", "library_guid", "registered")}

    def unregister_local(args):
        app = canonical(args["app"])
        rec = validated_record(app)
        if args.get("package") != rec["package"]:
            raise ValueError("local copy belongs to another package")
        # Shared/default library stays. An external library is removed from the registry only after its last local
        # game goes; other games, the Wine prefix, sign-in and save directories remain intact.
        peers = [core.read_json(os.path.join(core.ROOT, "local-games", f)) or {}
                 for f in os.listdir(os.path.dirname(record_path(app))) if f.endswith(".json") and f != app + ".json"]
        if rec.get("dest") and not any(p.get("library_guid") == rec["library_guid"] for p in peers):
            registry_library(rec, remove=True)
        if os.path.isdir(rec["game_dir"]):
            shutil.rmtree(rec["game_dir"])
        for suffix in (".json", ".json.mini"):
            path = os.path.join(rec["library"], "Manifests", app + suffix)
            if os.path.isfile(path):
                os.unlink(path)
        os.unlink(record_path(app))
        return {"removed": True}

    def runtime_state(args):
        app = canonical(args["app"])
        rec = validated_record(app) if read_record(app) else None
        # Read only newly appended runtime log text. Never return log lines: runtime logs can contain account data.
        logdir = os.path.join(core.USER_DIR, "AppData", "Local", "Oculus")
        def logs():
            return [os.path.join(logdir, n) for n in os.listdir(logdir)
                    if n.lower().endswith((".txt", ".log")) and any(
                        s in n.lower() for s in ("server", "service"))] if os.path.isdir(logdir) else []
        offsets = {p: os.path.getsize(p) for p in logs()}
        librarian = os.path.join(logdir, "OVRLibrarian.log")
        lib_offset = os.path.getsize(librarian) if os.path.isfile(librarian) else 0
        probe = subprocess.run(["pgrep", "-f", "[O]VRServer_x64.exe"], capture_output=True, timeout=5)
        started, proc = probe.returncode != 0, None
        end = time.monotonic() + 95              # leave room for wineserver's bounded cleanup, total <=120 s
        state = "timeout"
        try:
            if started:
                runtime = os.path.join(core.META_DIR, "Support", "oculus-runtime")
                proc = subprocess.Popen([os.path.join(core.GEF, "files/bin/wine"), "./OVRServer_x64.exe"],
                                        cwd=runtime, env=core.wine_env(), stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
            while time.monotonic() < end:
                if started and os.path.isfile(librarian):
                    with open(librarian, errors="replace") as f:
                        if os.path.getsize(librarian) < lib_offset:
                            lib_offset = 0
                        f.seek(lib_offset)
                        done = "invocation completed" in f.read(65536).lower()
                        lib_offset = f.tell()
                    if done:
                        subprocess.run(["pkill", "-9", "-f", "[O]VRLibrarian.exe"], capture_output=True, timeout=3)
                for path in logs():
                    with open(path, errors="replace") as f:
                        off = offsets.get(path, 0)
                        f.seek(off if os.path.getsize(path) >= off else 0)
                        text = f.read(1 << 20)
                        offsets[path] = f.tell()
                    for line in text.splitlines():
                        if app not in line and (not rec or rec.get("app_id", "_") not in line):
                            continue
                        match = re.search(r"\bstate\s*:\s*(ready|install_available)\b", line, re.I)
                        if match:
                            state = match[1].lower()
                        # Meta's PackageManager lists every registered package at start ("Currently installed
                        # package: <name>, version code: N"); the "state:" line only comes later (app tracker / sign-in)
                        elif state == "timeout" and re.search(r"Currently installed package:\s*" + re.escape(app) + r"\s*,",
                                                              line):
                            state = "installed"
                if state != "timeout" or (proc is not None and proc.poll() is not None):
                    break
                time.sleep(1)
        finally:
            if started:
                subprocess.run([os.path.join(core.GEF, "files/bin/wineserver"), "-k"], env=core.wine_env(),
                               capture_output=True, timeout=10)
                if proc is not None:
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
        return {"runtime_state": state, "reused_server": not started}

    original_manifest = core.manifest_launch
    original_info = core.game_info

    def manifest_launch(app):
        rec = read_record(app)
        if not rec.get("registered"):
            return original_manifest(app)
        rec = validated_record(app)
        manifest = core.read_json(os.path.join(rec["library"], "Manifests", app + ".json")) or {}
        if not manifest.get("launchFile"):
            raise ValueError("local-copy Meta manifest is missing; register the copy again")
        return {"exe": relative(manifest["launchFile"]), "args": rec["args"], "title": rec["title"],
                "game_dir": rec["game_dir"], "args_argv": True}

    def game_info(app):
        rec = read_record(app)
        if not rec.get("registered"):
            return original_info(app)
        info = dict(core.GAMES.get(app) or {})
        info.update(manifest_launch(app))
        return info, app in core.GAMES

    core.manifest_launch = manifest_launch
    core.game_info = game_info
    core.COMMANDS.update(prepare_local=prepare, register_local=register_local, unregister_local=unregister_local,
                         local_info=local_info, runtime_state=runtime_state)
    core.INSTALL_CHECK_HOOKS.append(lambda app, info, result: {"local_copy": True} if read_record(app) else {})
