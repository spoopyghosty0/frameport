"""Install a built game on a Steam Frame over SSH (via the FramePort agent)."""
from __future__ import annotations

import contextlib
import os
import posixpath
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from ..artwork import fetch as artwork
from ..build import sha256
from ..core.controllers import effective_adapter_settings
from ..core.events import Cancelled, Reporter
from ..core.models import Recipe, data_manifest
from ..frame.connection import Frame, sh_quote
from ..patches import base
from ..sources.quest_dump import frame_names


@dataclass
class InstallPlan:
    package: str
    title: str
    apk: Path
    data_dir: Path | None
    recipe: Recipe
    apk_only: bool = False  # reuse the data already on the Frame
    dest: str | None = None
    data_files: list[str] | None = None  # only these files of data_dir (SourceGame.data_files); None = all of it


def install_context(recipe: Recipe) -> base.InstallContext:
    ctx = base.InstallContext(recipe.package, {}, {}, {}, effective_adapter_settings(recipe.patches))
    # device.foveation last: the player's explicit choice beats a recipe's lepton_env VK_INSTANCE_LAYERS
    for pid, params in sorted(recipe.patches.items(), key=lambda kv: kv[0] == "device.foveation"):
        patch = base.get(pid)
        if patch.category == "device" and (patch.stage == "install" or pid == "device.foveation"):
            ctx.params = params or {}
            patch.install(ctx)
    return ctx


def flat_launcher(package: str, recipe: Recipe) -> bool:
    """The app opens a flat launcher that starts its VR part (analysis vr_activity; GitHub #475 Xelee, WiiCompiled)
    or is a hybrid app that starts as a 2D panel (GitHub #484 YouTube VR), unless frame.start_activity starts the VR
    part directly."""
    from ..core import library
    from ..patches.settings import hybrid_panel

    extra = ((library.game(package) or {}).get("analysis") or {}).get("extra") or {}
    return (bool(extra.get("vr_activity")) or hybrid_panel(extra)) and "frame.start_activity" not in recipe.patches


def deployment_recipe(recipe: Recipe) -> dict:
    """The recipe record in the Frame's deployment.json. flat_launcher: the agent keeps SteamVR's theater mode for
    the shown Android window (device.text_input_window), the only way such a launcher is seen."""
    record = {"patches": sorted(recipe.patches), "source": recipe.source, "alt": recipe.use_alt}
    if flat_launcher(recipe.package, recipe):
        record["flat_launcher"] = True
    return record


def configuration_args(entry: dict, recipe: Recipe) -> dict:
    """One complete configuration for both an APK install and the configuration-only path."""
    from ..game_update import configuration, configuration_fingerprint

    ctx = install_context(recipe)
    from ..patches.settings import foveation_env, foveation_preference

    overrides = foveation_env(recipe.package, foveation_preference(entry), ctx.adapter_settings, ctx.env)
    for key, value in overrides.items():
        if value is None:
            ctx.env.pop(key, None)
        else:
            ctx.env[key] = value
    original = entry["recipe"]
    build = entry.get("build") or {}
    applied = configuration(original, recipe.package, build.get("superseded"))
    settings = dict(applied["settings"])
    # Preferences affect the runtime payload, never the recorded fingerprint.
    settings["quest_buttons"] = ctx.adapter_settings["quest_buttons"]
    if foveation_preference(entry).get("mode") in ("tracked", "fixed"):
        settings.update(foveation_fix=1, foveation_storage_fix=1)
    return dict(settings=settings, files={k: v.decode() if isinstance(v, bytes) else v
                                        for k, v in ctx.files.items()}, env=ctx.env,
                flatscreen=show_window(ctx, _flatscreen(recipe.package)), config_sync=ctx.config_sync,
                recipe=deployment_recipe(recipe), configuration=applied,
                configuration_fp=configuration_fingerprint(original, recipe.package, build.get("superseded")),
                apk_recipe_fp=build.get("apk_recipe_fp"), source_version=build.get("source_version"))


def update_existing(frame: Frame, entry: dict, deployment: dict, plan, recipe: Recipe,
                    reporter: Reporter, manifest: dict | None = None) -> dict:
    """No local converted APK is needed for runtime/data updates to a matching deployed build."""
    args = configuration_args(entry, recipe)
    # the library's current Steam tags (user tags, genres): the shortcut check after this update writes them
    args.update(package=entry["package"], title=entry.get("title") or entry["package"],
                apk_sha256=deployment["sha256"], tags=_tags(entry["package"]))
    if plan.data:
        reporter.stage("Check game data")
        prep = frame.agent("prepare", package=entry["package"], title=args["title"])
        local = manifest or {}
        names = frame_names(entry["package"], local)
        manifest = {names.get(rel, rel): size for rel, size in local.items()}
        moved = _reuse_renamed(frame, names, local, prep, reporter)
        pending = [(Path(entry["data_dir"]) / rel, f"obb/{names.get(rel, rel)}", size) for rel, size in local.items()
                   if names.get(rel, rel) not in moved and prep["existing_obb"].get(names.get(rel, rel)) != size]
        need = sum(size for _, _, size in pending)
        if need > prep["free_bytes"] - (1 << 30):
            raise RuntimeError("not enough space on the Frame for the changed game data + 1 GiB headroom")
        with transfer_link(frame, reporter, need) as xfer:
            if pending:
                reporter.stage("Upload changed game data")
                upload_files(xfer, pending, prep["incoming"], reporter, max(need, 1))
        args.update(obb_manifest=manifest, existing_only=True, consume_artwork=False,
                    apk_name=deployment.get("apk", "game.apk"))
        reporter.stage("Finalize game data")
        result = frame.agent("finalize", **args)
        unpack_obb_images(frame, entry["package"], recipe, reporter)
        return result
    reporter.stage("Apply game configuration")
    result = frame.agent("configure", **args)
    if result.get("deployment", {}).get("configuration_fp") != args["configuration_fp"]:
        raise RuntimeError("the Frame did not confirm the applied configuration")
    reporter.check("Game configuration", True, "applied; used from the game's next start")
    unpack_obb_images(frame, entry["package"], recipe, reporter)
    return result


OBB_IMAGE_PATCH = "frame.obb_mount_emul"


def unpack_obb_images(frame: Frame, package: str, recipe: Recipe, reporter: Reporter) -> int:
    """frame.obb_mount_emul: the game's OBB disk images are unpacked on the Frame (agent unpack_obb), one call per
    image (each takes a while: ~2 GB of reading and writing). Returns how many were unpacked."""
    if OBB_IMAGE_PATCH not in recipe.patches:
        return 0
    pending = frame.agent("unpack_obb", package=package).get("pending") or []
    for i, image in enumerate(pending, 1):
        reporter.check_cancel()
        reporter.stage(f"Unpack game data {i}/{len(pending)}")
        reporter.progress((i - 1) / len(pending), f"Unpacking {image}")
        done = frame.agent("unpack_obb", package=package, image=image, timeout=None)
        reporter.log(f"unpacked {image}: {done['files']} files, {done['bytes'] / 2**30:.2f} GiB in "
                     f"{done['seconds']} s")
    return len(pending)


def local_data_manifest(data_dir: Path | None, files: list[str] | None = None) -> dict[str, int]:
    return data_manifest(data_dir, files)


class Speed:
    """Upload speed over the last few seconds (a sliding window, so it follows link changes) + time left."""

    def __init__(self, total: int, window: float = 5.0, clock=time.monotonic):
        self.total, self.window, self.clock, self.samples = total, window, clock, []

    def text(self, done: int) -> str:
        """"42.1 MB/s · ~3 min left", or "" until there is a second of samples."""
        now = self.clock()
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[1][0] >= self.window:
            self.samples.pop(0)
        (t0, d0) = self.samples[0]
        if now - t0 < 1.0 or done <= d0:
            return ""
        rate = (done - d0) / (now - t0)
        left = max(self.total - done, 0) / rate
        eta = f"{left / 3600:.0f} h {left % 3600 / 60:02.0f} min" if left >= 3600 else (
            f"{left / 60:.0f} min" if left >= 90 else f"{left:.0f} s")
        return f"{rate / 1e6:.1f} MB/s · ~{eta} left"


def install(frame: Frame, plan: InstallPlan, reporter: Reporter) -> dict:
    from ..core import library

    entry = library.game(plan.package)
    reporter.stage("Prepare Frame")
    apk_sha = sha256(plan.apk)
    prep = frame.agent("prepare", package=plan.package, title=plan.title, dest=plan.dest,
                       apk_sha256=apk_sha, apk_size=plan.apk.stat().st_size)
    if not prep.get("lepton"):
        raise RuntimeError("Lepton is not installed on the Frame (Setup → Install Lepton)")
    incoming = prep["incoming"]
    local = {} if plan.apk_only else local_data_manifest(plan.data_dir, plan.data_files)
    # expansion files with a download's copy suffix or another case go to the Frame under the name the game looks for
    # (GitHub #450/#481); the PC's files keep theirs. manifest = the data as the Frame stores it.
    names = frame_names(plan.package, local)
    source = {names.get(rel, rel): rel for rel in local}
    manifest = {names.get(rel, rel): size for rel, size in local.items()}
    for rel, wanted in names.items():
        reporter.log(f"{rel} goes to the Frame as {wanted} (the name the game looks for)")
    existing = dict(prep["existing_obb"])
    for rel in _reuse_renamed(frame, names, local, prep, reporter):
        existing[rel] = manifest[rel]
    to_send = [rel for rel, size in manifest.items() if existing.get(rel) != size]
    _relink_installed(frame, plan.data_dir, {r: s for r, s in manifest.items() if r not in to_send},
                      posixpath.join(prep["base"], "lepton-app", "obb"), reporter, source)
    need = sum(manifest[r] for r in to_send) + (0 if prep["same_apk"] else plan.apk.stat().st_size)
    # unpacking OBB disk images (frame.obb_mount_emul) holds one image twice for a while
    space = need + (max(manifest[r] for r in to_send) if OBB_IMAGE_PATCH in plan.recipe.patches and to_send else 0)
    if space > prep["free_bytes"] - (1 << 30):
        raise RuntimeError(f"not enough space on the Frame: need {space / 2**30:.1f} GiB + 1 GiB headroom, "
                           f"have {prep['free_bytes'] / 2**30:.1f} GiB")
    total = max(need, 1)
    sent = 0
    speed = Speed(total)

    with transfer_link(frame, reporter, need) as xfer:
        reporter.stage("Upload APK")
        if prep["same_apk"]:
            reporter.log("identical APK already installed; not re-sending")
        else:
            def apk_cb(done, size):
                reporter.check_cancel()
                reporter.progress(done / total, f"APK {done / 2**20:.0f}/{size / 2**20:.0f} MiB",
                                  speed=speed.text(done))
            if plan.apk.stat().st_size >= BIG_FILE:
                upload_files(xfer, [(plan.apk, "game.apk", plan.apk.stat().st_size)], incoming, reporter, total)
            else:
                reporter.log("uploading APK with Paramiko SFTP")
                xfer.put(plan.apk, posixpath.join(incoming, "game.apk"), apk_cb)
            sent += plan.apk.stat().st_size
        if to_send:
            reporter.stage(f"Upload data ({len(to_send)} files)")
            upload_files(xfer, [(plan.data_dir / source[rel], f"obb/{rel}", manifest[rel]) for rel in to_send],
                         incoming, reporter, total, sent, speed)
        elif manifest:
            reporter.log("game data already on the Frame; not re-sending")

    artwork_manifest = None
    if not prep.get("installed") or (entry or {}).get("steam_art_stale"):
        artwork.fetch(plan.package, plan.apk)
        artwork_manifest = upload_steam_art(frame, plan.package, prep["base"], reporter)

    reporter.stage("Finalize install")
    ctx = install_context(plan.recipe)
    config = configuration_args(entry, plan.recipe) if entry else dict(
        settings=ctx.adapter_settings, files={k: v.decode() if isinstance(v, bytes) else v
                                            for k, v in ctx.files.items()}, env=ctx.env,
        flatscreen=show_window(ctx, _flatscreen(plan.package)), config_sync=ctx.config_sync or None,
        recipe=deployment_recipe(plan.recipe))
    if artwork_manifest is not None:
        config["artwork_manifest"] = artwork_manifest
    result = frame.agent(
        "finalize", package=plan.package, title=plan.title, dest=plan.dest, apk_sha256=apk_sha,
        tags=_tags(plan.package),
        apk_name=plan.apk.name, obb_manifest=manifest if not plan.apk_only else None,
        preserve_data=plan.apk_only, consume_artwork=artwork_manifest is not None, **config,
    )
    reporter.log(f"installed at {result['base']} (Steam shortcut id {result['appid']})")
    unpack_obb_images(frame, plan.package, plan.recipe, reporter)
    log_controller_models(result.get("controller_models"), reporter)
    log_config_sync(result.get("config_sync"), reporter)
    sync_room_scanner(frame, ctx.adapter_settings, reporter)
    return result


def sync_room_scanner(frame: Frame, settings: dict, reporter: Reporter) -> None:
    """A game that can ask for a room scan (scene_emul + scene_scan_request) needs the scanner and its headset prompts
    on the Frame: until now they only got there when a scan was started from this PC. Synced by digest (no upload
    when current); a failure only costs the in-game scan, never the install."""
    if not settings.get("scene_emul") or not settings.get("scene_scan_request", 1):
        return
    from . import rooms

    try:
        rooms.ensure_scanner(frame)
        rooms.ensure_prompts(frame)
    except Exception as exc:  # noqa: BLE001 - the game still installs and plays with its saved room
        reporter.log(f"room scanner not synced ({exc}); in-game room scans use the copy already on the Frame")


def log_config_sync(report: dict | None, reporter: Reporter) -> None:
    """What the agent did to the game's saved settings (device.config_sync)."""
    if not report:
        return
    if report.get("changed"):
        reporter.log("game settings kept in step with the eye buffers: " + ", ".join(
            f"{k} {old} -> {new}" for k, (old, new) in sorted(report["changed"].items())))
    elif report.get("skipped"):
        reporter.log(f"game settings not changed: {report['skipped']}")


def log_controller_models(models: dict | None, reporter: Reporter) -> None:
    """Outcome of the agent's Steam Frame controller model conversion (adapter setting controller_models)."""
    if not models:
        return
    names = ", ".join(posixpath.basename(v) for v in (models.get("sources") or {}).values())
    reporter.check("Steam Frame controller models", bool(models.get("ok")),
                   f"from SteamVR: {names}" if models.get("ok") else str(models.get("error")))


@dataclass
class PcvrPlan:
    package: str  # "rift.<slug>"
    title: str
    game_dir: Path
    exe: str  # relative to game_dir
    recipe: Recipe
    revive_dir: Path | None  # None: launch the exe directly (OpenXR-native game)
    exe_sha256: str | None = None
    revive_version: str | None = None
    art_lookup: str | None = None  # package whose store art to use
    dest: str | None = None
    overlay: dict | None = None  # a SteamVR overlay app: the agent's overlay argument (analysis/vroverlay.py)
    source_id: str | None = None  # which version of the game folder (GitHub #161: a newer one was added)
    changed_since: float | None = None  # a newer version: game files changed after this are sent again (see below)


def changed_since(path: Path, since: float | None) -> bool:
    """A newer version of a Linux app / PC VR game (GitHub #161): uploads skip files the Frame has with the same
    size, so a changed file of the same size would stay old. Files written (or unpacked: ctime) after the last
    install are sent again; since=None (no newer version) never, 0 (last install unknown) always."""
    if since is None:
        return False
    try:
        st = path.stat()
    except OSError:
        return True
    return max(st.st_mtime, st.st_ctime) > since


def _recipe_record(recipe: dict | None, source_id: str | None) -> dict | None:
    """The deployment's recipe record, with the source version it was installed from (ui.components.source_replaced)."""
    if not source_id:
        return recipe
    return {**(recipe or {}), "source_id": source_id}


def install_pcvr(frame: Frame, plan: PcvrPlan, reporter: Reporter) -> dict:
    """Pack a Windows PC VR game + Revive onto the Frame, run by Proton through a generated launch.sh."""
    from ..core.paths import artifacts_dir
    from ..patches.pcvr import game_args, launch_env
    from ..tools import revive as revive_tool

    tool = plan.recipe.params("pcvr.proton_tool").get("tool") or None
    reporter.stage("Prepare Frame")
    prep = frame.agent("prepare_pcvr", package=plan.package, title=plan.title, dest=plan.dest, tool=tool)
    if not prep.get("proton"):
        ensure_proton(frame, reporter, tool)
        prep = frame.agent("prepare_pcvr", package=plan.package, title=plan.title, dest=plan.dest, tool=tool)
        if not prep.get("proton"):
            raise RuntimeError("Proton is still not ready on the Frame")
    reporter.check("Proton on the Frame", True, prep["proton"]["display_name"])
    if prep.get("openxr"):
        reporter.check("OpenXR runtime", True, prep["openxr"].get("name") or prep["openxr"]["path"])
    else:
        reporter.check("OpenXR runtime", None, "no active_runtime.json found; the game may not see the headset")
    manifests = {"game": local_data_manifest(plan.game_dir)}
    sources = {"game": plan.game_dir}
    if plan.revive_dir:
        root = plan.revive_dir
        manifests["revive"] = {p.relative_to(root).as_posix(): p.stat().st_size
                               for p in revive_tool.runtime_files(root)}
        sources["revive"] = root
    xr_layer = "pcvr.xr_timefix" in plan.recipe.patches
    if xr_layer:
        layer = artifacts_dir() / "linux-arm64"
        manifests["xrlayer"] = {p.name: p.stat().st_size for p in sorted(layer.iterdir()) if p.is_file()}
        sources["xrlayer"] = layer
    oculus_hmd = "pcvr.oculus_unreal" in plan.recipe.patches
    if oculus_hmd:  # fp_oculushmd.exe: provides the OculusHMDConnected event Unreal's Oculus plugin checks for
        helpers = artifacts_dir() / "win-x64"
        manifests["helpers"] = {p.name: p.stat().st_size for p in sorted(helpers.iterdir()) if p.is_file()}
        sources["helpers"] = helpers
    to_send = [(t, rel) for t, m in manifests.items() for rel, size in m.items()
               if prep["existing"].get(t, {}).get(rel) != size
               or t == "game" and changed_since(plan.game_dir / rel, plan.changed_since)]
    need = sum(manifests[t][rel] for t, rel in to_send)
    if need > prep["free_bytes"] - (1 << 30):
        raise RuntimeError(f"not enough space on the Frame: need {need / 2**30:.1f} GiB + 1 GiB headroom, "
                           f"have {prep['free_bytes'] / 2**30:.1f} GiB")
    total = max(need, 1)
    if to_send:
        already = sum(sum(m.values()) for m in manifests.values()) - need
        reporter.stage(f"Upload game ({len(to_send)} files, {need / 2**30:.1f} GiB)"
                       + (f" — resuming, {already / 2**30:.1f} GiB already there" if already > 0 else ""))
        with transfer_link(frame, reporter, need) as xfer:
            upload_files(xfer, [(sources[t] / rel, f"{t}/{rel}", manifests[t][rel]) for t, rel in to_send],
                         prep["incoming"], reporter, total, 0)
    else:
        reporter.log("all game files already on the Frame; not re-sending")

    from ..artwork import sources

    if not sources.has_art(plan.package):
        artwork.fetch(plan.package, lookup=plan.art_lookup)
    upload_steam_art(frame, plan.package, prep["base"], reporter)

    reporter.stage("Finalize install")
    result = frame.agent(
        "finalize_pcvr", package=plan.package, title=plan.title, dest=plan.dest, tool=tool, exe=plan.exe,
        tags=_tags(plan.package),
        manifests=manifests, revive=plan.revive_dir is not None, xr_layer=xr_layer, oculus_hmd=oculus_hmd,
        env=launch_env(plan.recipe),
        game_args=game_args(plan.recipe), no_crash_reporter="pcvr.no_crash_reporter" in plan.recipe.patches,
        libovr_redirect="pcvr.libovr_redirect" in plan.recipe.patches,
        exe_sha256=plan.exe_sha256, revive_version=plan.revive_version, vr=not is_flat_windows(plan.package),
        recipe=_recipe_record({"patches": sorted(plan.recipe.patches), "source": plan.recipe.source}, plan.source_id),
        overlay=plan.overlay,
    )
    reporter.log(f"installed at {result['base']} (Proton {result['proton']}, Steam shortcut id {result['appid']})")
    log_overlay(result.get("overlay"), reporter)
    return result


def log_overlay(overlay: dict | None, reporter: Reporter) -> None:
    """Outcome of registering a SteamVR overlay app on the Frame (agent register_vr_overlay)."""
    if not overlay:
        return
    if overlay.get("registered"):
        reporter.check("SteamVR overlay app", True, f"registered with SteamVR as {overlay.get('key')}"
                       + (", starts with SteamVR" if overlay.get("autostart") else ""))
    elif overlay.get("error") == "steamvr":
        reporter.check("SteamVR overlay app", None, "SteamVR isn't running on the Frame: registered at the next "
                                                    "connection")
    else:
        reporter.check("SteamVR overlay app", False, f"SteamVR didn't take it: {overlay.get('error')}")


@dataclass
class LinuxPlan:
    package: str  # "linux.<slug>"
    title: str
    root: Path  # the app's folder (for a lone AppImage: the folder it's in, with files = [its name])
    exe: str  # relative to root
    files: list[str] | None = None  # only these files of root (a lone AppImage), else the whole folder
    appimage: bool = False
    openxr: bool = False
    x86_64: bool = False  # runs through FEX (installed on the Frame first, like Proton)
    dest: str | None = None  # a drive's install dir for a new install (GitHub #90); None = internal storage
    desktop_entry: bool = True  # an entry in Desktop Mode's menu and on its desktop (GitHub #84)
    desktop_icon: bool = True  # with it, a copy on Desktop Mode's desktop (GitHub #212)
    overlay: dict | None = None  # a SteamVR overlay app: the agent's overlay argument (analysis/vroverlay.py)
    source_id: str | None = None  # which version of the app (GitHub #161: a newer one was added)
    changed_since: float | None = None  # a newer version: files changed after this are sent again


def install_linux(frame: Frame, plan: LinuxPlan, reporter: Reporter) -> dict:
    """Upload an arm64 Linux app to the Frame (resumable, unchanged files aren't re-sent); the agent extracts
    AppImages, checks the app's libraries and writes its launcher (GitHub #31)."""
    if plan.x86_64:
        ensure_proton(frame, reporter, kind="linux_x86")
    reporter.stage("Prepare Frame")
    prep = frame.agent("prepare_linux", package=plan.package, title=plan.title, dest=plan.dest)
    if plan.files:
        manifest = {name: (plan.root / name).stat().st_size for name in plan.files}
    else:
        manifest = local_data_manifest(plan.root)
        manifest = {k: v for k, v in manifest.items() if not k.startswith(".unpacked")}
    to_send = [rel for rel, size in manifest.items() if prep["existing"].get("app", {}).get(rel) != size
               or changed_since(plan.root / rel, plan.changed_since)]
    need = sum(manifest[rel] for rel in to_send)
    if need > prep["free_bytes"] - (1 << 30):
        raise RuntimeError(f"not enough space on the Frame: need {need / 2**30:.1f} GiB + 1 GiB headroom, "
                           f"have {prep['free_bytes'] / 2**30:.1f} GiB")
    if to_send:
        reporter.stage(f"Upload app ({len(to_send)} files, {need / 2**20:.0f} MiB)")
        with transfer_link(frame, reporter, need) as xfer:
            upload_files(xfer, [(plan.root / rel, f"app/{rel}", manifest[rel]) for rel in to_send],
                         prep["incoming"], reporter, max(need, 1), 0)
    else:
        reporter.log("all app files already on the Frame; not re-sending")
    upload_steam_art(frame, plan.package, prep["base"], reporter)
    reporter.stage("Finalize install")
    executables = [rel for rel in manifest if rel != plan.exe and _is_elf(plan.root / rel)][:200]
    result = frame.agent("finalize_linux", package=plan.package, title=plan.title, exe=plan.exe,
                         appimage=plan.appimage, openxr=plan.openxr, x86_64=plan.x86_64, manifests={"app": manifest},
                         executables=executables, tags=_tags(plan.package), dest=plan.dest,
                         desktop_entry=plan.desktop_entry, desktop_icon=plan.desktop_icon, overlay=plan.overlay,
                         recipe=_recipe_record(None, plan.source_id), timeout=900)
    reporter.log(f"installed at {result['base']} (Steam shortcut id {result['appid']})")
    log_overlay(result.get("overlay"), reporter)
    apply_app_icon(frame, plan.package, prep.get("anchor"), result, reporter)
    if result.get("desktop_entry"):
        reporter.check("Desktop Mode entry", True, "in the application menu"
                       + (" and on the desktop" if result["desktop_entry"].get("desktop") else ""))
    return result


def _is_elf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def ensure_proton(frame: Frame, reporter: Reporter, tool: str | None = None, timeout: float = 45 * 60,
                  kind: str = "proton") -> dict:
    """Install Proton (ARM64) + its runtime on the Frame without user interaction: the agent writes Steam appmanifest
    stubs and restarts Steam, which downloads them. Waits until they're installed (progress from the appmanifests).
    kind "linux_x86": FEX + its runtime instead, for x86_64 Linux apps (agent v60)."""
    import time

    name = "Proton" if kind == "proton" else "FEX (x86 translation)"
    st = frame.agent("proton_status", tool=tool, kind=kind)
    if st.get("ready"):
        return st["ready"]
    reporter.stage(f"Install {name} on the Frame")
    r = frame.agent("install_proton", mode="unattended", tool=tool, kind=kind)
    reporter.log(r.get("hint") or f"installing {r.get('tool')}")
    end = time.time() + timeout
    last = None
    asked_runtime = False
    while time.time() < end:
        reporter.check_cancel()
        time.sleep(10)
        try:
            st = frame.agent("proton_status", tool=tool, kind=kind, timeout=60)
        except Exception:  # SSH can hiccup while Steam restarts
            continue
        if st.get("ready"):
            reporter.check(f"{name} installed", True, st["ready"]["display_name"])
            return st["ready"]
        dl = st.get("download") or {}
        sug = st.get("suggested") or {}
        if sug.get("installed") and not sug.get("require_installed") and not dl.get("total") and not asked_runtime:
            # Steam names the runtime a Proton needs (e.g. Experimental) only once Proton itself is installed: ask
            # for it now
            asked_runtime = True
            reporter.log("installing the Steam Linux Runtime this Proton needs")
            frame.agent("install_proton", mode="unattended", tool=tool, kind=kind)
            continue
        if dl.get("total"):
            reporter.progress(dl["done"] / dl["total"], f"downloading {name} {dl['done'] / 2**20:.0f}/"
                                                         f"{dl['total'] / 2**20:.0f} MiB")
        elif dl != last:
            reporter.log("waiting for Steam to start the download…")
        last = dl
    raise RuntimeError(f"{name} didn't finish installing on the Frame in time; check Steam's downloads on the Frame")


BIG_FILE = 8 << 20  # bigger files use resumable SFTP; smaller ones are streamed in batches
BATCH_BYTES, BATCH_FILES = 256 << 20, 2000


def upload_threads(value=None) -> int:
    """Bound the saved preference; the one-job FIFO still bounds concurrent installs."""
    from ..core import library

    if value is None:
        value = library.setting("install.upload_threads", 3)
    try:
        return max(1, min(8, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 3


def _upload_parallel(frame, items, remote_root, reporter, total, sent, workers):
    """Each worker owns one transport; failures stop the cohort and leave resumable .part files."""
    lock, stop, local = threading.Lock(), threading.Event(), threading.local()
    sessions, errors, progress, offsets = [], [], {}, {}
    speed = Speed(total - sent)

    def upload(item):
        path, rel, size = item
        try:
            reporter.check_cancel()
            if stop.is_set():
                raise Cancelled()
            if not hasattr(local, "frame"):
                session = frame.upload_session()
                with lock:
                    sessions.append(session)
                if stop.is_set():
                    session.close()
                    raise Cancelled()
                local.frame = session

            def show(done, size):
                reporter.check_cancel()
                if stop.is_set():
                    raise Cancelled()
                with lock:
                    offsets.setdefault(rel, done)
                    progress[rel] = max(progress.get(rel, 0), done)
                    completed = sum(progress.values())
                    resumed = sum(offsets.values())
                    speed.total = total - sent - resumed
                    reporter.progress((sent + completed) / max(total, 1),
                                      f"{rel} {done / 2**20:.0f}/{size / 2**20:.0f} MiB",
                                      speed=speed.text(completed - resumed))
            def check_cancel():
                reporter.check_cancel()
                if stop.is_set():
                    raise Cancelled()
            local.frame.put(path, posixpath.join(remote_root, rel), show, mkdir=False, check_cancel=check_cancel)
        except Exception as exc:
            with lock:
                if not errors:
                    errors.append(exc)
            stop.set()
            raise

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="frameport-upload") as pool:
            pending = {pool.submit(upload, item) for item in items}
            interrupted = False
            while pending:
                if reporter.cancelled.is_set():
                    with lock:
                        if not errors:
                            errors.append(Cancelled())
                    stop.set()
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    if not future.cancelled():
                        try:
                            future.result()
                        except Exception:
                            stop.set()
                if stop.is_set() and not interrupted:
                    interrupted = True
                    for future in pending:
                        future.cancel()
                    pending = {future for future in pending if not future.cancelled()}
                    with lock:
                        active = list(sessions)
                    for session in active:
                        session.close()  # interrupt blocked transfers; never close the control connection
            if errors:
                raise errors[0]
    finally:
        for session in sessions:
            session.close()
    return sent + sum(size for _, _, size in items)


FAST_LINK_MIN = 64 << 20  # below this a second connection isn't worth it


@contextlib.contextmanager
def transfer_link(frame: Frame, reporter: Reporter, need: int):
    """The connection bulk uploads should use: a direct link (USB cable / the Frame's hotspot) when this PC has one,
    else the normal connection. Agent commands keep using `frame`; both see the same files."""
    xfer, label = (frame, "")
    if need >= FAST_LINK_MIN:
        try:
            xfer, label = frame.fast_link()
        except Exception:  # noqa: BLE001 - discovery is best effort
            xfer, label = frame, ""
    reporter.check("Transfer link", True, label or f"{frame.target.host} (connect this PC to the Frame's hotspot "
                                                   "or a USB cable for faster uploads)" if need >= FAST_LINK_MIN
                   else frame.target.host)
    try:
        yield xfer
    finally:
        if xfer is not frame:
            xfer.close()


def _link_local(frame, items: list[tuple[Path, str, int]], remote_root: str, reporter: Reporter
                ) -> tuple[list[tuple[Path, str, int]], int]:
    """FramePort running on the Frame itself (GitHub #433): files on the same drive as the install are hardlinked into
    it instead of copied over SSH, so a 30 GB game doesn't take 60 GB (the original stays the source for rebuilds).
    Returns (the items still to upload, bytes linked). The agent moves them into place with os.replace, which keeps
    the link."""
    from ..frame.local import on_frame

    host = getattr(getattr(frame, "target", None), "host", None)
    if not items or host not in ("127.0.0.1", "localhost") or not on_frame():
        return items, 0
    rest, linked = [], 0
    for local, rel, size in items:
        dest = Path(posixpath.join(remote_root, rel))
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and dest.stat().st_size == size:
                linked += size
                continue
            dest.unlink(missing_ok=True)
            os.link(local, dest)
            linked += size
        except OSError:  # another drive (EXDEV), a filesystem without hardlinks: copy it as before
            rest.append((local, rel, size))
    if linked:
        reporter.log(f"linked {len(items) - len(rest)} file(s) on this Frame instead of copying them "
                     f"({linked / 2**30:.1f} GiB)")
    return rest, linked


def _relink_installed(frame, data_dir: Path, same: dict[str, int], obb_root: str, reporter: Reporter,
                      source: dict[str, str] | None = None) -> int:
    """FramePort on the Frame itself (GitHub #433): data an earlier install copied (before installs linked it, or from a
    PC) and that is unchanged (same size, the test that skips its upload) is replaced by a hardlink to the original,
    so updating a game frees the second copy without reinstalling it. same is keyed by the Frame's names; source maps
    a renamed file (frame_names) to its name in data_dir. Returns the bytes freed."""
    from ..frame.local import on_frame

    host = getattr(getattr(frame, "target", None), "host", None)
    if not same or host not in ("127.0.0.1", "localhost") or not on_frame():
        return 0
    freed = 0
    for rel, size in same.items():
        src, dest = Path(data_dir) / (source or {}).get(rel, rel), Path(posixpath.join(obb_root, rel))
        tmp = dest.with_name(dest.name + ".fplink")
        try:
            if not dest.is_file() or os.path.samefile(src, dest) or src.stat().st_size != size:
                continue
            tmp.unlink(missing_ok=True)
            os.link(src, tmp)
            os.replace(tmp, dest)  # atomic: the copy stays if anything above failed
            freed += size
        except OSError:  # another drive, no hardlinks: keep the copy
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
    if freed:
        reporter.log(f"replaced copied game data with links to the originals ({freed / 2**30:.1f} GiB freed)")
    return freed


def _reuse_renamed(frame, names: dict[str, str], local: dict[str, int], prep: dict, reporter: Reporter) -> set[str]:
    """A renamed expansion file (frame_names) that an earlier install put on the Frame under its old name is moved to
    incoming/ under the new one instead of being sent again (finalize moves it into place; resumable like an upload,
    prepare counts incoming/). Leaves no stale copy under the old name. Returns the Frame names reused."""
    if not names:
        return set()
    existing, done = prep["existing_obb"], set()
    app_obb = posixpath.join(prep["base"], "lepton-app", "obb")
    for rel, wanted in names.items():
        if existing.get(wanted) == local[rel] or existing.get(rel) != local[rel]:
            continue
        try:
            frame.sftp.rename(posixpath.join(app_obb, rel), posixpath.join(prep["incoming"], "obb", wanted))
        except (OSError, AttributeError) as exc:  # e.g. already moved by an interrupted run: sent the normal way
            reporter.log(f"{rel} on the Frame not reused as {wanted} ({exc}); sending it")
            continue
        reporter.log(f"renamed {rel} to {wanted} on the Frame (no upload needed)")
        done.add(wanted)
    return done


def upload_files(frame: Frame, items: list[tuple[Path, str, int]], remote_root: str, reporter: Reporter,
                 total: int, sent: int = 0, speed: Speed | None = None, workers: int | None = None,
                 prefer_native: bool = True) -> int:
    """Upload (local, relative path, size) items under remote_root. Interruptible (Cancel, dropped connection) and
    resumable: finished files are skipped next time (the agent counts files already in incoming/), a cut-off big file
    continues from its .part. Returns bytes sent."""
    items, linked = _link_local(frame, items, remote_root, reporter)
    sent += linked
    big = sorted((i for i in items if i[2] >= BIG_FILE), key=lambda item: item[2], reverse=True)
    small = [i for i in items if i[2] < BIG_FILE]
    reporter.check_cancel()
    frame.mkdirs(posixpath.dirname(posixpath.join(remote_root, rel)) for _, rel, _ in big)
    state = {"sent": sent}
    speed = speed or Speed(total)

    def show(extra: int, label: str):
        reporter.check_cancel()
        done = state["sent"] + extra
        reporter.progress(done / total, label, speed=speed.text(done))
    threads = min(upload_threads(workers), len(big))
    native_done = False
    if big and prefer_native and isinstance(frame, Frame):
        from ..frame import native_upload

        original = reporter
        class ProgressReporter:
            last = 0.0

            def __getattr__(self, name):
                return getattr(original, name)

            def progress(self, fraction, label, **kwargs):
                self.last = max(self.last, fraction)
                original.progress(self.last, label, **kwargs)

        reporter = ProgressReporter()  # fallback resumes in a different order; never move the progress bar backward
        try:
            state["sent"] = native_upload.upload(frame, big, remote_root, reporter, total, sent, max(threads, 1))
            native_done = True
        except Cancelled:
            raise
        except Exception as exc:  # native startup, authentication or transfer failure: retain the portable path
            reporter.check_cancel()
            reporter.log(f"OpenSSH unavailable or failed; resuming with Paramiko: {exc}")
        speed.samples.clear()
    if native_done:
        pass
    elif threads > 1 and hasattr(frame, "upload_session"):
        reporter.log(f"uploading large files with Paramiko SFTP ({threads} workers)")
        state["sent"] = _upload_parallel(frame, big, remote_root, reporter, total, sent, threads)
        speed.samples.clear()
    else:
        if big:
            reporter.log("uploading large files with Paramiko SFTP (1 worker)")
        for local, rel, size in big:
            reporter.check_cancel()
            frame.put(local, posixpath.join(remote_root, rel),
                      lambda done, size, rel=rel: show(done, f"{rel} {done / 2**20:.0f}/{size / 2**20:.0f} MiB"),
                      mkdir=False)
            state["sent"] += size
    batch: list[tuple[Path, str]] = []
    batch_bytes = 0

    use_native_tar = prefer_native and isinstance(frame, Frame)

    def flush():
        nonlocal batch, batch_bytes, use_native_tar
        if batch:
            base = state["sent"]
            last = 0

            def progress(done, rel):
                nonlocal last
                last = max(last, done)
                show(last, f"{len(batch)} files · {rel}")
            uploaded = False
            if use_native_tar:
                from ..frame import native_upload

                try:
                    reporter.log(f"uploading tar batch with OpenSSH SSH ({len(batch)} files)")
                    native_upload.put_tar(frame, batch, remote_root, reporter, progress)
                    uploaded = True
                except Cancelled:
                    raise
                except Exception as exc:
                    reporter.check_cancel()
                    reporter.log(f"OpenSSH unavailable or failed; resuming with Paramiko: {exc}")
                    use_native_tar = False
                    speed.samples.clear()
            if not uploaded:
                reporter.log(f"uploading tar batch with Paramiko ({len(batch)} files)")
                frame.put_tar(batch, remote_root, progress)
            state["sent"] = base + batch_bytes
        batch, batch_bytes = [], 0
    for local, rel, size in small:
        batch.append((local, rel))
        batch_bytes += size
        if batch_bytes >= BATCH_BYTES or len(batch) >= BATCH_FILES:
            flush()
    flush()
    return state["sent"]


def _tags(package: str) -> list[str]:
    from ..artwork.steam import steam_tags
    from ..core import library

    entry = library.game(package)
    return steam_tags(entry) if entry else []


def is_flat_windows(package: str) -> bool:
    """A Windows game without VR (added with "Add one game folder…"): Proton runs it as a window, no VR setup."""
    from ..core import library

    g = library.game(package) or {}
    return bool(((g.get("analysis") or {}).get("extra") or {}).get("flat"))


def show_window(ctx: base.InstallContext, automatic: bool) -> bool:
    """Lepton's flat window for this install: the user's device.display_mode choice, else automatic (an app without VR
    code), plus device.text_input_window (a VR app's window behind its VR view, for typing)."""
    if ctx.display == "flat" or ctx.flatscreen:
        return True
    return automatic and ctx.display != "vr"


def _flatscreen(package: str) -> bool:
    """An Android app without VR: Lepton must show it as a flat window (it never draws through OpenXR)."""
    from ..core import library

    g = library.game(package) or {}
    return bool(g.get("analysis")) and library.analysis_from_dict(g["analysis"]).vr_kind == "none"


def art_manifest(package: str, art: dict) -> dict[str, str]:
    import hashlib

    from ..artwork.sources import icon_source

    return {**{f"{kind}{path.suffix}": sha256(path) for kind, path in art.items()},
            ".icon-source": hashlib.sha256(icon_source(package).encode()).hexdigest()}


def upload_steam_art(frame: Frame, package: str, base: str, reporter: Reporter) -> dict[str, str]:
    """The complete Steam art set (portrait, wide, hero, logo, icon — composed where the store has no such shape)."""
    from ..artwork.steam import steam_set_for

    reporter.stage("Artwork for the Steam library")
    art = steam_set_for(package)
    remote_art = posixpath.join(base, "incoming-artwork")
    frame.run(f"rm -rf {sh_quote(remote_art)} && mkdir -p {sh_quote(remote_art)}")
    for kind, f in art.items():
        frame.put(f, posixpath.join(remote_art, f"{kind}{f.suffix}"), resume=False)
    send_icon_source(frame, package, remote_art)
    reporter.check("Steam artwork", bool(art) or None, ", ".join(sorted(art)) or "none found")
    return art_manifest(package, art)


def send_icon_source(frame: Frame, package: str, remote_art: str) -> None:
    """Tell the agent what the art set's icon is (artwork/.icon-source, agent v64): a Linux app's own icon is used
    for its Desktop Mode entry and Steam shortcut unless the user chose one ("custom")."""
    from ..artwork.sources import icon_source

    frame.run(f"printf %s {sh_quote(icon_source(package))} > {sh_quote(posixpath.join(remote_art, '.icon-source'))}")


def apply_app_icon(frame: Frame, package: str, anchor: str | None, result: dict, reporter: Reporter) -> None:
    """A Linux app's own icon the agent found in its files (GitHub #99) becomes the library's icon when the game has
    none (no user pick, no store icon); the Frame's art set is then sent again so Steam's art shows it too."""
    import base64

    from ..artwork import sources, steam

    icon = result.get("app_icon") or {}
    if icon:
        reporter.log(f"the app's own icon: {icon.get('file')}")
    if not icon.get("png") or not anchor:
        return
    try:
        stored = sources.apply_app_icon(package, base64.b64decode(icon["png"]))
        if not stored:
            return
        steam.ensure_cover(package)
        art = steam.steam_set_for(package)
    except Exception as exc:  # noqa: BLE001 - artwork is optional
        reporter.log(f"the app's own icon wasn't used for the library: {exc}")
        return
    remote = posixpath.join(anchor, "artwork")
    for kind, f in art.items():
        frame.put(f, posixpath.join(remote, f"{kind}{f.suffix}"), resume=False)
    send_icon_source(frame, package, remote)
    reporter.check("App icon", True, "the app's own icon is used for the library")


def update_steam_art(frame: Frame, package: str, reporter: Reporter, title: str | None = None) -> dict:
    """Replace an installed game's Steam library art with the current artwork (after the user picked new art):
    the art set goes to the game's anchor, then the shortcut is rewritten (Steam restarts once). `title`: the game's
    name in the library; a different name on the Frame is renamed first (agent v77 `rename`: the shortcut keeps its
    appid, so Play, grid art and saves stay). Placeholder art shows the name, so it's sent again too.
    The result has `library: True` when the shortcut was rewritten too (else the caller still checks it)."""
    from ..artwork.steam import steam_set_for

    dep = next((d for d in frame.agent("list_installed")["games"] if d.get("package") == package), None)
    if not dep:
        raise RuntimeError(f"{package} isn't installed on the Frame")
    if not dep.get("anchor"):
        raise RuntimeError("the Frame's FramePort agent is too old; reinstall the game to update its art")
    renamed = False
    if title and dep.get("title") != title:
        reporter.stage("Name in the Steam library")
        res = frame.agent("rename", package=package, title=title, shortcuts=False)
        renamed = bool(res.get("renamed"))
        reporter.check("Name", True, f"{dep.get('title')} -> {res.get('title')} (shortcut {res.get('appid')} kept)")
    art = steam_set_for(package)
    if not art:
        if renamed:
            return {**(add_to_steam(frame, [package], reporter) or {}), "library": True}
        raise RuntimeError("no artwork to send")
    manifest = art_manifest(package, art)
    previous = dep.get("artwork_manifest") or {}
    if manifest == previous:
        reporter.log("Steam artwork already current; not re-sending")
        return {**(add_to_steam(frame, [package], reporter) or {}), "library": True} if renamed else {"unchanged": True}
    reporter.stage("Artwork for the Steam library")
    remote = posixpath.join(dep["anchor"], "artwork")
    if not previous:
        # Unrecorded art (installs before agent v86): what's in the folder is unknown, and an old portrait.jpg next
        # to a new portrait.png would be picked at random for the Steam grid. Start clean as before; all is sent.
        frame.run(f"rm -rf -- {sh_quote(remote)}")
    frame.run(f"mkdir -p {sh_quote(remote)}")
    for name in previous.keys() - manifest.keys():
        # Deployment records are data, not shell paths. Never act on an unexpected name.
        if "/" not in name and name not in (".", ".."):
            frame.run(f"rm -f -- {sh_quote(posixpath.join(remote, name))}")
    for kind, f in art.items():
        name = f"{kind}{f.suffix}"
        if previous.get(name) != manifest[name]:
            frame.put(f, posixpath.join(remote, name), resume=False)
    if previous.get(".icon-source") != manifest[".icon-source"]:
        send_icon_source(frame, package, remote)
    frame.agent("record_artwork", package=package, manifest=manifest)
    reporter.check("Steam artwork", True, ", ".join(sorted(art)))
    return {**(add_to_steam(frame, [package], reporter) or {}), "library": True}


def steam_library_mode() -> str:
    """now / later / off. Unset, FramePort running on the Frame itself defaults to later: with now the restart waits
    until FramePort closes (it would close Desktop Mode and FramePort too), which looked stuck (GitHub #425)."""
    from ..core import library
    from ..frame.local import on_frame

    return library.setting("install.steam_library", "later" if on_frame() else "now")


def add_to_steam(frame: Frame, packages: list[str], reporter: Reporter, wait: float = 120) -> dict:
    """Add library entries + artwork for installed games; Steam restarts once (a few seconds)."""
    import time

    reporter.stage("Add to Steam library")
    mode = steam_library_mode()
    frame.agent("library_mode", mode=mode)
    submitted = frame.agent("shortcuts", packages=packages, restart=True, mode=mode)
    if submitted.get("disabled") or submitted.get("state") == "pending":
        reporter.check("Steam library", None, "disabled; use FramePort Play" if mode == "off" else
                       "queued for the next Steam start; you can keep installing")
        return submitted
    end = time.time() + wait
    status = {}
    while time.time() < end:
        time.sleep(3)
        try:
            status = frame.agent("shortcut_status", timeout=30)
        except Exception:  # the SSH session can hiccup while Steam restarts
            continue
        if status.get("state") in ("done", "failed"):
            break
        if status.get("state") == "waiting":  # Steam restarts later: after the game, or back in Gaming Mode
            reporter.check("Steam library", None,
                           "added after FramePort on the Frame closes (Steam has to restart for it); to add it "
                           "now: Settings → Installing → Restart Steam now"
                           if status.get("reason") == "frameport" else
                           "added when you're back in Gaming Mode (Steam has to restart for it)"
                           if status.get("reason") == "desktop" else
                           "added when the game that's running now is closed (Steam has to restart for it)")
            return status
    added = {a["package"] for a in status.get("added", [])}
    for a in status.get("added", []):
        reporter.check(f"Steam library: {a['package']}", True, f"shortcut {a['appid']}")
    for e in status.get("errors", []):
        reporter.check("Steam library", False, e)
    if not status.get("errors"):
        for pkg in packages:
            if pkg not in added:  # no answer in time: say so (Play adds a missing shortcut itself)
                reporter.check(f"Steam library: {pkg}", False, "the Frame didn't confirm the library entry in time")
    return status
