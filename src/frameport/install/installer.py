"""Install a built game on a Steam Frame over SSH (via the FramePort agent)."""
from __future__ import annotations

import contextlib
import posixpath
import time
from dataclasses import dataclass
from pathlib import Path

from ..artwork import fetch as artwork
from ..build import sha256
from ..core.events import Reporter
from ..core.models import Recipe, data_manifest
from ..frame.connection import Frame, sh_quote
from ..patches import base
from ..patches.settings import adapter_settings


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
    ctx = base.InstallContext(recipe.package, {}, {}, {}, adapter_settings(recipe.patches))
    # device.foveation last: the player's explicit choice beats a recipe's lepton_env VK_INSTANCE_LAYERS
    for pid, params in sorted(recipe.patches.items(), key=lambda kv: kv[0] == "device.foveation"):
        patch = base.get(pid)
        if patch.stage == "install" and patch.category == "device":
            ctx.params = params or {}
            patch.install(ctx)
    return ctx


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
    reporter.stage("Prepare Frame")
    apk_sha = sha256(plan.apk)
    prep = frame.agent("prepare", package=plan.package, title=plan.title, dest=plan.dest,
                       apk_sha256=apk_sha, apk_size=plan.apk.stat().st_size)
    if not prep.get("lepton"):
        raise RuntimeError("Lepton is not installed on the Frame (Setup → Install Lepton)")
    incoming = prep["incoming"]
    manifest = {} if plan.apk_only else local_data_manifest(plan.data_dir, plan.data_files)
    existing = prep["existing_obb"]
    to_send = [rel for rel, size in manifest.items() if existing.get(rel) != size]
    need = sum(manifest[r] for r in to_send) + (0 if prep["same_apk"] else plan.apk.stat().st_size)
    if need > prep["free_bytes"] - (1 << 30):
        raise RuntimeError(f"not enough space on the Frame: need {need / 2**30:.1f} GiB + 1 GiB headroom, "
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
            xfer.put(plan.apk, posixpath.join(incoming, "game.apk"), apk_cb)
            sent += plan.apk.stat().st_size
        if to_send:
            reporter.stage(f"Upload data ({len(to_send)} files)")
            upload_files(xfer, [(plan.data_dir / rel, f"obb/{rel}", manifest[rel]) for rel in to_send], incoming,
                         reporter, total, sent, speed)
        elif manifest:
            reporter.log("game data already on the Frame; not re-sending")

    artwork.fetch(plan.package, plan.apk)
    upload_steam_art(frame, plan.package, prep["base"], reporter)

    reporter.stage("Finalize install")
    ctx = install_context(plan.recipe)
    result = frame.agent(
        "finalize", package=plan.package, title=plan.title, dest=plan.dest, apk_sha256=apk_sha,
        tags=_tags(plan.package),
        apk_name=plan.apk.name, settings=ctx.adapter_settings,
        files={k: v.decode() if isinstance(v, bytes) else v for k, v in ctx.files.items()}, env=ctx.env,
        obb_manifest=manifest or None, flatscreen=show_window(ctx, _flatscreen(plan.package)),
        recipe={"patches": sorted(plan.recipe.patches), "source": plan.recipe.source, "alt": plan.recipe.use_alt},
    )
    reporter.log(f"installed at {result['base']} (Steam shortcut id {result['appid']})")
    log_controller_models(result.get("controller_models"), reporter)
    return result


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
               if prep["existing"].get(t, {}).get(rel) != size]
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
        recipe={"patches": sorted(plan.recipe.patches), "source": plan.recipe.source}, overlay=plan.overlay,
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
    overlay: dict | None = None  # a SteamVR overlay app: the agent's overlay argument (analysis/vroverlay.py)


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
    to_send = [rel for rel, size in manifest.items() if prep["existing"].get("app", {}).get(rel) != size]
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
                         desktop_entry=plan.desktop_entry, overlay=plan.overlay, timeout=900)
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


BIG_FILE = 8 << 20  # bigger files go one by one (resumable); smaller ones are streamed in batches
BATCH_BYTES, BATCH_FILES = 256 << 20, 2000


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


def upload_files(frame: Frame, items: list[tuple[Path, str, int]], remote_root: str, reporter: Reporter,
                 total: int, sent: int = 0, speed: Speed | None = None) -> int:
    """Upload (local, relative path, size) items under remote_root. Interruptible (Cancel, dropped connection) and
    resumable: finished files are skipped next time (the agent counts files already in incoming/), a cut-off big file
    continues from its .part. Returns bytes sent."""
    big = [i for i in items if i[2] >= BIG_FILE]
    small = [i for i in items if i[2] < BIG_FILE]
    frame.mkdirs(posixpath.dirname(posixpath.join(remote_root, rel)) for _, rel, _ in big)
    state = {"sent": sent}
    speed = speed or Speed(total)

    def show(extra: int, label: str):
        reporter.check_cancel()
        done = state["sent"] + extra
        reporter.progress(done / total, label, speed=speed.text(done))
    for local, rel, size in big:
        reporter.check_cancel()
        frame.put(local, posixpath.join(remote_root, rel),
                  lambda done, size, rel=rel: show(done, f"{rel} {done / 2**20:.0f}/{size / 2**20:.0f} MiB"),
                  mkdir=False)
        state["sent"] += size
    batch: list[tuple[Path, str]] = []
    batch_bytes = 0

    def flush():
        nonlocal batch, batch_bytes
        if batch:
            base = state["sent"]
            frame.put_tar(batch, remote_root, lambda done, rel: show(done, f"{len(batch)} files · {rel}"))
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


def upload_steam_art(frame: Frame, package: str, base: str, reporter: Reporter) -> None:
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
    appid, so Play, grid art and saves stay). Placeholder art shows the name, so it's sent again too."""
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
            return add_to_steam(frame, [package], reporter)
        raise RuntimeError("no artwork to send")
    reporter.stage("Artwork for the Steam library")
    remote = posixpath.join(dep["anchor"], "artwork")
    frame.run(f"rm -rf {sh_quote(remote)} && mkdir -p {sh_quote(remote)}")
    for kind, f in art.items():
        frame.put(f, posixpath.join(remote, f"{kind}{f.suffix}"), resume=False)
    send_icon_source(frame, package, remote)
    reporter.check("Steam artwork", True, ", ".join(sorted(art)))
    return add_to_steam(frame, [package], reporter)


def add_to_steam(frame: Frame, packages: list[str], reporter: Reporter, wait: float = 120) -> dict:
    """Add library entries + artwork for installed games; Steam restarts once (a few seconds)."""
    import time

    reporter.stage("Add to Steam library")
    frame.agent("shortcuts", packages=packages, restart=True)
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
