#!/usr/bin/python3
"""Add shared codecs to Lepton containers without editing their shared rootfs."""
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from xml.etree import ElementTree as ET


def mounts(directory, config, args):
    if not args or args[0] != "run":
        return []
    name = None
    for i, arg in enumerate(args):
        if arg == "--name" and i + 1 < len(args):
            name = args[i + 1]
        elif arg.startswith("--name="):
            name = arg.partition("=")[2]
    if config.get("scope") == "shared":
        # The common launcher supplies these values. Do not intercept arbitrary
        # Podman containers or depend on a game's package/modified APK.
        appid = os.environ.get("SteamAppId", "")
        if not re.fullmatch(r"[0-9]+", appid) or name != f"lepton-steamlaunch-{appid}":
            return []
        app = Path(os.environ.get("STEAM_COMPAT_INSTALL_PATH", ""))
        if app.name != "lepton-app" or not app.is_dir():
            return []
        root_arg = None
        for i, arg in enumerate(args):
            if arg == "--rootfs" and i + 1 < len(args):
                root_arg = args[i + 1]
            elif arg.startswith("--rootfs="):
                root_arg = arg.partition("=")[2]
        if not root_arg or not root_arg.endswith(":O"):
            return []
        root = Path(root_arg[:-2]).resolve()
    else:  # old per-game deployments remain compatible during migration
        if name != f"lepton-steamlaunch-{config['appid']}":
            return []
        root = Path(config["lepton"]).resolve().parent / "images" / "rootfs"
    expected_root = str(root) + ":O"
    if not any(arg in (expected_root, "--rootfs=" + expected_root) for arg in args):
        return []
    device = Path("/dev/video-dec0")
    runtime = root / "vendor/lib64/libstagefright_softomx.so"
    upstream_plugin = (root / "vendor/lib64/libstagefrighthw.so").exists()
    for i, arg in enumerate(args):
        if arg == "--mount" and i + 1 < len(args):
            fields = dict(item.split("=", 1) for item in args[i + 1].split(",") if "=" in item)
            target = fields.get("destination", fields.get("target"))
            if target == "/vendor/lib64/libstagefrighthw.so":
                upstream_plugin = True
            if target == "/vendor/lib64/libstagefright_softomx.so" and fields.get("source"):
                runtime = Path(fields["source"])
    if upstream_plugin:
        print("FramePort video: using the runtime's hardware codec plugin", file=sys.stderr)
        return []
    if not device.exists() or hashlib.sha256(runtime.read_bytes()).hexdigest() != config["runtime_sha256"]:
        print("FramePort video: device or runtime ABI differs; retaining the stock codecs", file=sys.stderr)
        return []
    xml = ET.parse(root / "vendor/etc/media_codecs.xml")
    if not any(node.get("href") == "media_codecs_frameport.xml" for node in xml.getroot().findall("Include")):
        ET.SubElement(xml.getroot(), "Include", href="media_codecs_frameport.xml")
    # Immutable plugin versions can serve simultaneous app launches and
    # different Lepton installations. Never share a temporary XML filename.
    # The version directory stays as the agent verified it: the merged list goes to the user's runtime dir.
    root_key = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    merged = merged_dir() / f"media_codecs.{root_key}.xml"
    temporary = merged.with_suffix(f".{os.getpid()}.tmp")
    xml.write(temporary, encoding="utf-8", xml_declaration=True)
    temporary.replace(merged)
    # Lepton supplies its own /dev tmpfs. A Podman --device node disappears
    # beneath it; a bind mount matches Lepton's existing GPU/sound device setup.
    result = ["--mount", f"type=bind,source={device},destination=/dev/video-dec0,rw"]
    for path, target in (
        (directory / "libstagefrighthw.so", "/vendor/lib64/libstagefrighthw.so"),
        (merged, "/vendor/etc/media_codecs.xml"),
        (directory / "media_codecs_frameport.xml", "/vendor/etc/media_codecs_frameport.xml"),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"missing video codec mount: {path}")
        result += ["--mount", f"type=bind,source={path},destination={target},ro"]
    print("FramePort video: loading the Iris hardware codec plugin for this container", file=sys.stderr)
    return result


def merged_dir():
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    base = Path(runtime) if runtime and Path(runtime).is_dir() else Path.home() / ".cache"
    path = base / "frameport-video"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def switched_off(directory):
    """FRAMEPORT_NO_HW_VIDEO=1 (e.g. in a game's Steam launch options) or the Frame-wide switch (FramePort's
    setting; the agent writes video-codec/disabled) leave every container with Android's stock codecs."""
    if os.environ.get("FRAMEPORT_NO_HW_VIDEO", "") not in ("", "0"):
        return True
    return directory.parent.name == "versions" and (directory.parent.parent / "disabled").exists()


def real_podman(directory):
    """Find Podman independently of deployment.json, without recursing into this wrapper."""
    own_bin = (directory / "bin").resolve()
    own_script = Path(__file__).resolve()
    # Lepton runs its `podman exec` calls (boot wait, app pid, logcat mirror) with the Android guest's PATH
    # (/product/bin:/system/bin:...), which has no host Podman: the system folders come after PATH. Failing there
    # broke Lepton's logcat mirror and app-pid checks, and the container was stopped early.
    entries = os.environ.get("PATH", os.defpath).split(os.pathsep) + ["/usr/local/bin", "/usr/bin", "/bin"]
    for entry in entries:
        folder = Path(entry or os.curdir).resolve()
        if folder == own_bin:
            continue
        found = shutil.which("podman", path=str(folder))
        if found:
            executable = Path(found).resolve()
            if executable.parent != own_bin and executable != own_script:
                return str(executable)
    raise RuntimeError("no real Podman executable found outside the codec wrapper directory")


def main():
    directory = Path(__file__).resolve().parent.parent
    args = sys.argv[1:]
    fallback = real_podman(directory)
    if switched_off(directory):
        os.execv(fallback, [fallback, *args])
    try:
        config = json.loads((directory / "deployment.json").read_text())
        podman = Path(config.get("podman", fallback)).resolve()
        if podman.parent == (directory / "bin").resolve() or podman == Path(__file__).resolve():
            raise ValueError("configured Podman points to the codec wrapper")
        extra = mounts(directory, config, args)
        launch_args = [args[0], *extra, *args[1:]] if extra else args
        os.execv(str(podman), [str(podman), *launch_args])
    except Exception as exc:  # a codec/configuration failure must never prevent the stock container from starting
        print(f"FramePort video: retaining stock codecs: {exc}", file=sys.stderr)
    os.execv(fallback, [fallback, *args])


if __name__ == "__main__":
    main()
