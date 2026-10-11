"""FramePort command line. Same operations as the GUI (for automation and headless use).
Exit codes: 0 done, 1 something failed, 2 wrong usage, 10 (`update --check`) a newer version exists.
Errors are one line on stderr; FRAMEPORT_DEBUG=1 shows the traceback.

    frameport tools install                      # portable Java + OVRPort + apksigner
    frameport scan "<folder with game dumps>"    # analyze + suggest recipes
    frameport list | show <pkg> | patches
    frameport rename <pkg> "<new name>"         # the name in the library and in Steam (--reset: automatic)
    frameport recipe <pkg> --enable frame.nodebug --set scale=1.2
    frameport build <pkg>|--all
    frameport frame discover | pair | info --frame steamos@frame.local
    frameport install <pkg> --frame steamos@frame.local [--apk-only]
    frameport test <pkg> --frame ...             # headless launch + triage
    frameport session <pkg> [--apply]            # triage the last play session (crashes, fps, focus dips)
    frameport frame send <files> --dest videos   # copy files to the Frame (where every game sees them)
    frameport frame proton [--install]           # Proton on the Frame, for PC VR games
    frameport install rift.<game> --to pc|frame  # PC VR games: this PC (Steam) or the Frame (Proton)
    frameport pc info                            # Windows Steam / SteamVR / Revive on this PC
    frameport parity --known-good <PATCHED dir>  # rebuild everything and diff against known-good APKs
    frameport diag collect <pkg>|--all [--no-frame]  # redacted diagnostics zip (attach it to a GitHub issue)
    frameport diag inspect <zip>                 # re-triage a diagnostics zip (no game files or Frame needed)
    frameport diag report <pkg>                  # zip + prefilled GitHub problem report
    frameport share-recipe <pkg> --status works  # prefilled GitHub issue submitting a working recipe
    frameport open-link "<install link>"         # FrameDrop button / frameport:// link: download, add, install
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from . import pipeline
from .core import library
from .core.events import printing_reporter
from .patches import base

FRAME_HELP = "the Frame as user@host (default: the remembered Frame)"
ALL_HELP = "every game in the library"
JSON_HELP = "print JSON"

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Run Quest games, PC VR games, Android and Linux apps on the Steam Frame.",
                  epilog="Exit codes: 0 done, 1 something failed, 2 wrong usage, 10 (update --check) a newer version "
                         "exists. The GUI is frameport-gui.")
tools_app = typer.Typer(help="Manage the portable toolchain.")
frame_app = typer.Typer(help="Find and connect to a Steam Frame.")
pc_app = typer.Typer(help="This PC as a target for PC VR games (Steam and SteamVR; Revive for Oculus games).")
diag_app = typer.Typer(help="Diagnostics bundles and problem reports.")
app.add_typer(tools_app, name="tools")
app.add_typer(frame_app, name="frame")
app.add_typer(pc_app, name="pc")
app.add_typer(diag_app, name="diag")


def _show_version(value: bool):
    if value:
        from . import __version__

        typer.echo(f"FramePort {__version__}")
        raise typer.Exit()


@app.callback()
def _setup(ctx: typer.Context,
           version: bool = typer.Option(False, "--version", is_eager=True, callback=_show_version,
                                        help="show the FramePort version and exit")):
    from .core import applog

    applog.setup("cli")
    if ctx.invoked_subcommand != "update":
        _update_hint()


def _update_hint() -> None:
    """At most once a day: a one-line note when a newer FramePort release exists. Never waits for the network: it
    reads what the last check cached and refreshes a stale cache in the background for next time."""
    import threading
    import time

    from . import updates

    if updates.checks_disabled():
        return
    age = updates.cache_age()
    if age is None or age > updates.CHECK_EVERY:
        threading.Thread(target=updates.refresh_cache, daemon=True).start()
        from .recommend import catalog as _catalog

        threading.Thread(target=_catalog.refresh_remote, daemon=True).start()  # confirmed configs from GitHub main
    up = updates.cached_update()
    today = time.strftime("%Y-%m-%d")
    if up and library.setting("update.cli_hint") != today:
        library.set_setting("update.cli_hint", today)
        typer.echo(f"FramePort {up.version} is available (you have {_version()}): run \"frameport update\"", err=True)


def _version() -> str:
    from . import __version__

    return __version__


@app.command("update")
def update_cmd(check: bool = typer.Option(False, "--check", help="only say whether a newer version exists"),
               yes: bool = typer.Option(False, "--yes", "-y", help="don't ask before updating")):
    """Update FramePort to the latest release (the app, a `uv tool`/pip install, or a source checkout)."""
    import subprocess

    from . import updates

    up = updates.check(force=True)
    if not up:
        typer.echo(f"FramePort {_version()} is the latest version.")
        return
    typer.echo(f"FramePort {up.version} is available (you have {_version()}). {up.page}")
    notes = [line for line in up.notes.strip().splitlines()][:25]
    if notes:
        typer.echo("\n" + "\n".join("  " + line for line in notes) + "\n")
    if check:
        raise typer.Exit(10)  # scripts: 10 = an update is available
    kind = updates.install_kind()
    if kind == "source" and updates.source_is_dirty():
        typer.echo("This source checkout has uncommitted changes: commit or stash them first.", err=True)
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Update now ({kind})?", default=True):
        raise typer.Exit()
    if kind == "bundle":
        rep = printing_reporter()
        app_dir = updates.prepare(up, rep)
        updates.apply(app_dir, relaunch=False)
        typer.echo(f"FramePort {up.version} installs as soon as this command exits.")
        return
    cmds = updates.upgrade_commands(up, kind)
    if kind == "wheel" and sys.platform == "win32":
        # the running frameport.exe can't be replaced while it runs: finish the upgrade right after this exits
        line = " && ".join(subprocess.list2cmdline(c) for c in cmds)
        updates.spawn_hidden(["cmd", "/c", f"ping -n 3 127.0.0.1 >nul && {line}"])
        typer.echo(f"Updating to FramePort {up.version} in the background: run `frameport --version` in a few seconds.")
        return
    for cmd in cmds:
        typer.echo("$ " + " ".join(cmd))
        if subprocess.call(cmd, timeout=updates.UPGRADE_TIMEOUT, env=updates.upgrade_env()):
            typer.echo("Update failed.", err=True)
            raise typer.Exit(1)
    typer.echo(f"Updated to FramePort {up.version}.")


def _target(frame: Optional[str], password: Optional[str] = None, to: str = "frame"):
    if to == "pc":
        from .targets.pc_revive import PcReviveTarget

        return PcReviveTarget()
    if to != "frame":
        raise typer.BadParameter("--to must be 'frame' or 'pc'")
    from .frame.connection import parse_target, saved_targets
    from .targets.frame_lepton import FrameLeptonTarget

    if frame:
        t = parse_target(frame)
    else:
        saved = saved_targets()
        if not saved:
            raise typer.BadParameter("no Frame given and none remembered; use --frame steamos@<host>")
        t = saved[0]
    return FrameLeptonTarget(t, password).connect()


def _pkgs(package: Optional[str], all_: bool) -> list[str]:
    if all_:
        return [g["package"] for g in library.games()]
    if not package:
        raise typer.BadParameter("give a package or --all")
    matches = [g["package"] for g in library.games()
               if package.lower() in (g["package"] + " " + (g.get("title") or "")).lower()]
    if package in [g["package"] for g in library.games()]:
        return [package]
    if len(matches) != 1:
        if not matches:
            raise typer.BadParameter(f"no game in the library matches {package!r} (see frameport list)")
        raise typer.BadParameter(f"{package!r} matches {len(matches)} games: {', '.join(matches[:5])}"
                                 + (" …" if len(matches) > 5 else ""))
    return matches


# ------------------------------------------------------------------------------------------ tools
@tools_app.command("status")
def tools_status(check_latest: bool = typer.Option(False, "--latest", help="also look up the newest versions")):
    """Which tools FramePort has (Java runtime, OVRPort, apksigner, Revive)."""
    from .tools import toolchain

    for s in toolchain.status(check_latest):
        latest = f" (latest {s.latest})" if s.latest else ""
        state = "installed" if s.installed else "missing"
        typer.echo(f"{s.name:10} {state:9} {s.version or '-'}{latest}  {s.path or ''}")


@tools_app.command("install")
def tools_install(update: bool = typer.Option(False, help="update to the newest versions"),
                  revive: bool = typer.Option(False, help="also install Revive (for Oculus PC VR games)")):
    """Download the tools FramePort needs into its own folder (nothing is installed system-wide)."""
    from .tools import overport as ov
    from .tools import toolchain

    for s in toolchain.ensure_all(update=update, optional=revive):
        if s.optional and not s.installed:
            continue
        typer.echo(f"{s.name:10} {s.version}  {s.path}")
    from .patches import overport as op

    added = op.refresh(ov.list_patches)
    if added:
        typer.echo("new OVRPort patches: " + ", ".join(added))


@tools_app.command("import-keys")
def tools_import_keys(dirs: list[Path] = typer.Argument(..., help="folders with <package>.keystore files")):
    """Import existing per-package signing keystores (keeps updates installable over existing installs)."""
    from .tools import overport as ov

    typer.echo(f"imported {ov.import_keystores(*dirs)} keystore(s) into {ov.workspace() / 'signatures'}")


# ------------------------------------------------------------------------------------------ library
@app.command()
def scan(path: Path = typer.Argument(..., help="a folder with game backups (APKs or PC VR game folders), or an APK")):
    """Find games under PATH, analyze them and suggest patches."""
    rep = printing_reporter(verbose=False)
    for g in pipeline.add_path(path, rep):
        r = g["recipe"]
        typer.echo(f"{g['package']:40} {g.get('title', '')[:34]:34} {r['status']:11} {r['source']}")
        for warning in pipeline.analysis_warnings(g):
            typer.echo(f"  ! {warning}")


@app.command("catalog-update")
def catalog_update():
    """Fetch confirmed game recipes from the repo's main branch now (normally automatic, every 6 h)."""
    from .recommend import catalog

    n = catalog.refresh_remote(force=True)
    st = catalog.remote_status()
    typer.echo(f"{n} recipe(s) downloaded; {st['entries']} on GitHub main")
    for pkg, why in sorted(st["skipped"].items()):
        typer.echo(f"  skipped {pkg}: {why}")


@app.command("add-linux")
def add_linux(path: Path = typer.Argument(..., help="an arm64 AppImage, a folder, or a .zip/.tar.gz with the app"),
              exe: Optional[str] = typer.Option(None, help="the program to start (relative to the app's folder)")):
    """Add a native arm64 Linux app to the library (installed on the Frame as it is, with a Steam shortcut)."""
    try:
        g = pipeline.add_linux_app(path, printing_reporter(verbose=False), exe=exe)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    extra = g["analysis"]["extra"]
    typer.echo(f"{g['package']:40} {g.get('title', '')[:34]:34} program {extra['exe']}"
               f"{' (AppImage)' if extra['appimage'] else ''}{' VR' if extra['openxr'] else ''}")


@app.command("open-link")
def open_link(link: str = typer.Argument(..., help="an \"Install with FrameDrop\" button's address, a framedrop:// "
                                                   "or frameport:// link, a manifest (.json) or an APK/zip/exe URL"),
              yes: bool = typer.Option(False, "--yes", "-y", help="don't ask before downloading"),
              no_install: bool = typer.Option(False, help="only download and add to the library"),
              frame: Optional[str] = typer.Option(None, help=FRAME_HELP),
              gui: bool = typer.Option(False, help="hand the link to the GUI (started if it isn't running)")):
    """Install from a link (FrameDrop's button protocol): download, add to the library, install on the Frame."""
    from . import deeplink

    if gui:
        from . import urlhandler

        urlhandler.drop_link(link)
        if not urlhandler.app_running():
            import subprocess

            subprocess.Popen(urlhandler.start_command(), start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        typer.echo("handed to the FramePort window")
        return
    try:
        m = deeplink.fetch_manifest(deeplink.parse(link))
    except deeplink.LinkError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"{m.name}  (from {m.host or m.source})")
    if m.description:
        typer.echo(f"  {m.description[:400]}")
    for f in m.files:
        size = deeplink.head_size(f.url)
        typer.echo(f"  {f.filename}  {f.kind or 'other'}"
                   f"{f'  {size / 2**20:.0f} MiB' if size else ''}  {'sha256 checked' if f.sha256 else 'no checksum'}")
    if not yes and not typer.confirm("Only install software from sites you trust. Download and install it?"):
        raise typer.Exit(1)
    rep = printing_reporter(verbose=False)
    path = deeplink.download(m, rep)
    g = pipeline.add_from_link(m, path, rep, icon=deeplink.fetch_icon(m))
    pkg = g["package"]
    typer.echo(f"added {pkg} ({g.get('title')})")
    if no_install:
        return
    target = _target(frame)
    info = pipeline.build_game(pkg, rep)
    if not info["ok"]:
        typer.echo(f"{pkg}: the game didn't pass its checks", err=True)
        raise typer.Exit(1)
    pipeline.install_game(pkg, target, rep)
    typer.echo(f"installed {pkg} on {target.label}")


@app.command("list")
def list_games():
    """The games in the library."""
    for g in library.games():
        a = g["analysis"]
        built = "built" if g.get("build", {}).get("ok") else ("build-failed" if g.get("build") else "")
        typer.echo(f"{g['package']:40} {(g.get('title') or '')[:30]:30} {a['engine']:6} {a['xr']:12} "
                   f"{g['recipe']['status']:11} {built}")


@app.command()
def show(package: str, as_json: bool = typer.Option(False, "--json", help=JSON_HELP),
         all_: bool = typer.Option(False, "--all", help="also list patches that don't apply to this game")):
    """A game's analysis and recipe (its patches, with the reason for each)."""
    [pkg] = _pkgs(package, False)
    g = library.game(pkg)
    if as_json:
        typer.echo(json.dumps(g, indent=1, default=str))
        return
    a, r = g["analysis"], g["recipe"]
    typer.echo(f"{g.get('title')}  ({pkg} {a['version']})\n  engine {a['engine']}, XR {a['xr']}, {a['graphics']}, "
               f"ABIs {', '.join(a['abis'])}, direct VrApi: {a['direct_vrapi']}")
    typer.echo(f"  status: {r['status']}  recipe source: {r['source']}  {r['notes']}")
    for warning in pipeline.analysis_warnings(g):
        if warning not in r["notes"]:
            typer.echo(f"  ! {warning}")
    from .recommend.engine import visible_patches

    shown, hidden = visible_patches(library.analysis_from_dict(a), library.recipe_from_dict(r))
    for p in (shown + hidden if all_ else shown):
        on = p.id in r["patches"]
        params = r["patches"].get(p.id) or {}
        typer.echo(f"  [{'x' if on else ' '}] {p.id:36} {p.title}{'  ' + json.dumps(params) if params else ''}"
                   f"{'  — ' + r['reasons'][p.id] if on and p.id in r.get('reasons', {}) else ''}")
    for pid, fix in ((g.get("build") or {}).get("superseded") or {}).items():
        typer.echo(f"  last build left out {pid}: fixed upstream ({fix})")
    if hidden and not all_:
        typer.echo(f"  ({len(hidden)} patches hidden as not relevant for this game; --all to list them)")
    if r.get("alt_patches"):
        which = "alt" if r["use_alt"] else "primary"
        typer.echo(f"  alternate build adds: {', '.join(r['alt_patches'])}  (installed: {which})")


@app.command()
def rename(package: str = typer.Argument(..., help="the game (package or part of its title)"),
           title: Optional[str] = typer.Argument(None, help="the new name"),
           reset: bool = typer.Option(False, "--reset", help="back to FramePort's own name (store or APK name)"),
           sync: bool = typer.Option(True, "--sync/--no-sync",
                                     help="also rename it in Steam where it's installed (Steam restarts once)"),
           frame: Optional[str] = typer.Option(None, help=FRAME_HELP)):
    """Rename a game in the library and in Steam. The name is kept from then on (scans and store lookups don't
    change it); its Steam entry keeps its id, so Play, artwork and saves stay."""
    [pkg] = _pkgs(package, False)
    if reset == bool(title):
        raise typer.BadParameter("give a new name or --reset")
    if not reset and not pipeline.clean_title(title):
        raise typer.BadParameter("the new name is empty")
    before = library.game(pkg).get("title")
    g = pipeline.rename_game(pkg, None if reset else title)
    typer.echo(f"{pkg}: {before} -> {g['title']}" + ("" if g.get("title_locked") else " (automatic name)"))
    if g["title"] == before or not sync:
        if g.get("steam_name_stale"):
            typer.echo("The Frame's Steam library keeps the old name until `frameport rename` runs with --sync or "
                       "the game is installed again.")
        return
    from .targets.base import PC_LABEL

    rep = printing_reporter(verbose=False)
    installs = g.get("installs") or {}
    if any(w != PC_LABEL for w in installs):
        try:
            pipeline.sync_title(pkg, _target(frame), rep)
        except Exception as exc:  # noqa: BLE001 - the library name is saved either way
            typer.echo(f"Steam on the Frame wasn't renamed ({exc}); run this again when the Frame is reachable.",
                       err=True)
    if g.get("kind") == "rift":
        from .targets.pc_revive import local_installs

        if pkg in local_installs():
            pipeline.sync_title(pkg, _target(None, to="pc"), rep)


@app.command()
def patches():
    """List every available patch."""
    for p in base.all_patches():
        flag = " (experimental)" if p.experimental else ""
        typer.echo(f"{p.category:8} {p.id:36} {p.title}{flag}")


@app.command()
def recipe(package: str, enable: list[str] = typer.Option([], "--enable", help="patch id to turn on (repeatable)"),
           disable: list[str] = typer.Option([], "--disable", help="patch id to turn off (repeatable)"),
           set_: list[str] = typer.Option([], "--set", help="adapter setting key=value"),
           use_alt: Optional[bool] = typer.Option(None, "--use-alt/--no-alt", help="install the alternate build"),
           reset: bool = typer.Option(False, help="start again from the suggested recipe"),
           as_is: Optional[bool] = typer.Option(None, "--as-is/--patch", help="install unchanged (already patched)"),
           exe: Optional[str] = typer.Option(None, help="PC VR games: the program that starts the game (relative)")):
    """Change a game's patch selection."""
    [pkg] = _pkgs(package, False)
    r = pipeline.reset_recipe(pkg) if reset else library.recipe_from_dict(library.game(pkg)["recipe"])
    for pid in enable:
        base.get(pid)
        r.patches.setdefault(pid, {})
        r.reasons[pid] = "Enabled by user."
    for pid in disable:
        r.patches.pop(pid, None)
    for kv in set_:
        k, _, v = kv.partition("=")
        r.patches[f"adapter.{k}"] = {"value": float(v) if "." in v else int(v)}
    if use_alt is not None:
        r.use_alt = use_alt
    if as_is is not None:
        r.as_is = as_is
        if library.game(pkg).get("kind") == "rift":
            r.patches.pop("pcvr.revive", None) if as_is else r.patches.setdefault("pcvr.revive", {})
    if exe:
        pipeline.set_exe(pkg, exe)
        r = library.recipe_from_dict(library.game(pkg)["recipe"])
    r.source = "user" if (enable or disable or set_ or use_alt is not None or as_is is not None) else r.source
    pipeline.set_recipe(pkg, r)
    show(pkg)


# ------------------------------------------------------------------------------------------ build / install / test
@app.command()
def build(package: Optional[str] = typer.Argument(None), all_: bool = typer.Option(False, "--all", help=ALL_HELP),
          outdir: Optional[Path] = typer.Option(None, help="where to write the APKs (default: FramePort's data)"),
          verbose: bool = typer.Option(False, help="print every step")):
    """Patch and sign (primary + alternate build when the recipe has one)."""
    failed = []
    for pkg in _pkgs(package, all_):
        rep = printing_reporter(verbose)
        try:
            info = pipeline.build_game(pkg, rep, outdir / pkg if outdir and all_ else outdir)
            typer.echo(f"{pkg}: {'OK' if info['ok'] else 'CHECKS FAILED'} -> {info.get('apk') or 'ready (Rift game)'}")
            if not info["ok"]:
                failed.append(pkg)
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"{pkg}: FAILED {exc}", err=True)
            failed.append(pkg)
    raise typer.Exit(1 if failed else 0)


@app.command()
def install(package: Optional[str] = typer.Argument(None), all_: bool = typer.Option(False, "--all", help=ALL_HELP),
            frame: Optional[str] = typer.Option(None, help=FRAME_HELP),
            password: Optional[str] = typer.Option(None, help="the Frame's password (first connection only)"),
            apk_only: bool = typer.Option(False, help="reuse game data already on the Frame"),
            no_library: bool = typer.Option(False, help="don't add to the Steam library now"),
            to: str = typer.Option("frame", help="frame, or pc (PC VR games only: install on this PC)"),
            apk: Optional[Path] = typer.Option(None, help="install this APK instead of the last build (one game; "
                                                          "for example a test build signed with the game's key)"),
            dest: Optional[str] = typer.Option(None, help="drive for new installs: 'internal' or a drive's path from "
                                                          "'frameport frame drives' (default: the app's setting); "
                                                          "installed games stay where they are")):
    """Install games on the Frame (or PC VR games on this PC) and add them to the Steam library."""
    target = _target(frame, password, to)
    if dest is not None and hasattr(target, "dest"):
        target.dest = "" if dest == "internal" else dest
    pkgs = _pkgs(package, all_)
    if apk and len(pkgs) != 1:
        raise typer.BadParameter("--apk needs exactly one game")
    for pkg in pkgs:
        if not apk_only and pipeline.missing_obb(library.game(pkg) or {}):
            typer.echo(f"{pkg}: warning: " + pipeline.MISSING_OBB_NOTE.format(package=pkg), err=True)
        pipeline.install_game(pkg, target, printing_reporter(False), apk_only, add_to_library=False, apk=apk)
    if not no_library:
        target.add_to_library(pkgs, printing_reporter(False))


@app.command()
def test(package: Optional[str] = typer.Argument(None), all_: bool = typer.Option(False, "--all", help=ALL_HELP),
         frame: Optional[str] = typer.Option(None, help=FRAME_HELP),
         seconds: int = typer.Option(45, help="how long the game runs before it's stopped"),
         to: str = typer.Option("frame", help="frame, or pc (PC VR games installed on this PC)")):
    """Headless launch test on the Frame (or this PC) and log triage."""
    target = _target(frame, to=to)
    installed = {g["package"] for g in target.installed()}
    for pkg in _pkgs(package, all_):
        if pkg not in installed:
            typer.echo(f"{pkg}: not installed on {target.label}", err=True)
            continue
        s = pipeline.test_game(pkg, target, printing_reporter(False), seconds)
        typer.echo(f"{pkg}: {s['state']} ({s['verdict']}) furthest: {s['milestone']}"
                   + (f"; suggestions: {', '.join(s['suggestions'])}" if s["suggestions"] else ""))


@app.command()
def session(package: str = typer.Argument(..., help="the game (package or part of its title)"),
            frame: Optional[str] = typer.Option(None, help=FRAME_HELP),
            apply: bool = typer.Option(False, "--apply", help="apply the suggested fixes (FrameBridge settings are "
                                       "written on the Frame at once; other patches need `frameport install`)")):
    """Triage the game's last play session on the Frame: crashes, frame rate, focus dips, and what to try."""
    target = _target(frame)
    for pkg in _pkgs(package, False):
        s = pipeline.triage_session(pkg, target)
        if not s:
            typer.echo(f"{pkg}: no play session on the Frame yet")
            continue
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("ended") or s.get("start") or 0))
        fps = s.get("fps") or {}
        typer.echo(f"{pkg}: session ended {when}" + (" (still running)" if not s.get("ended") else "")
                   + (f"; median {fps['median']:g} fps, below {fps['target']} Hz {fps['slow_share']:.0%} of the time"
                      if fps else "; no frames logged") + f"; {s.get('focus_dips', 0)} focus dips")
        for f in s["findings"]:
            typer.echo(f"  {f['severity']:7} {f['id']}: {f['diagnosis']}\n          {f['evidence'][:200]}"
                       + (f"\n          question: {f['question']}" if f.get("question") else "")
                       + (f"\n          suggest: {', '.join(f['suggest'])}" if f.get("suggest") else ""))
        sugg = s.get("suggestions") or []
        if not apply or not sugg:
            if sugg:
                typer.echo(f"  suggestions: {', '.join(sugg)} (--apply to apply them)")
            continue
        installed = pkg in {g["package"] for g in target.installed()}
        if pipeline.apply_suggestions_live(pkg, sugg, target if installed else None):
            typer.echo(f"  applied on the Frame: {', '.join(sugg)} (used from the game's next start)")
        else:
            typer.echo(f"  added to the recipe: {', '.join(sugg)}; rebuild and install with: frameport install {pkg}")


@app.command()
def triage(logfile: Path = typer.Argument(..., help="a launch.log"),
           package: Optional[str] = typer.Option(None, help="the game's package (keeps only its log lines)")):
    """Classify a launch.log offline."""
    from .validate.triage import triage as run_triage

    r = run_triage(logfile.read_text(errors="replace"), "UNKNOWN", package)
    typer.echo(f"furthest milestone: {r.milestone}; fps {r.fps}")
    for f in r.findings:
        typer.echo(f"  {f.severity:7} {f.id}: {f.diagnosis}\n          {f.evidence[:200]}\n"
                   f"          suggest: {f.suggest}")


@app.command()
def settings(package: str, values: list[str] = typer.Argument(..., help="key=value pairs"),
             frame: Optional[str] = typer.Option(None, help=FRAME_HELP)):
    """Change FrameBridge settings of an installed game (no rebuild), for example scale=1.2."""
    target = _target(frame)
    kv = dict(v.split("=", 1) for v in values)
    typer.echo(json.dumps(target.set_settings(package, kv), indent=1))


# ------------------------------------------------------------------------------------------ frame
@frame_app.command("discover")
def frame_discover(seconds: float = typer.Option(4.0, help="how long to listen")):
    """Find Steam Frames (in Developer Mode) on the local network."""
    from .frame.discovery import browse

    for f in browse(seconds):
        typer.echo(f"{f.name:30} {f.host:16} {'FramePort' if f.is_frameport else 'ssh'}")


@frame_app.command("pair")
def frame_pair(timeout: float = typer.Option(600, help="seconds to wait for the Frame")):
    """Show the setup command for the Frame and wait until the Frame reports back."""
    import time

    from .frame.connection import FrameTarget, save_target
    from .frame.pairing import PairingServer

    # no mDNS announcement: the setup URL's requests need someone to click Allow, which only the GUI offers
    srv = PairingServer(announce=False).start()
    typer.echo("On the Frame, open the SteamVR dashboard → Launch a program → Desktop, then app menu → System → "
               "Konsole, and run:\n\n    " + srv.one_liner + "\n")
    end = time.time() + timeout
    while time.time() < end and not srv.paired:
        time.sleep(1)
    srv.stop()
    if not srv.paired:
        typer.echo("timed out")
        raise typer.Exit(1)
    info = srv.paired[0]
    save_target(FrameTarget(info["host"], info["user"], 22, info["name"]))
    typer.echo(f"connected to {info['name']} at {info['host']}")


@frame_app.command("connect")
def frame_connect(address: str = typer.Argument(..., help="steamos@<host or IP>"),
                  password: Optional[str] = typer.Option(None, prompt=False, help="the Frame's password (once)")):
    """Connect with SSH (password once), install FramePort's key and remember the Frame."""
    from .frame.connection import Frame, parse_target, save_target

    t = parse_target(address)
    f = Frame(t, password).connect()
    f.install_key()
    info = f.agent("info")
    t.name = info["hostname"]
    save_target(t)
    keys = ("hostname", "os", "os_version", "lepton", "steam_users", "free_bytes")
    typer.echo(json.dumps({k: info[k] for k in keys}, indent=1))


@frame_app.command("cleanup")
def frame_cleanup(frame: Optional[str] = typer.Option(None, help=FRAME_HELP),
                  keep_rollback: bool = typer.Option(False, help="keep previous-game.apk copies"),
                  path: list[str] = typer.Option([], help="extra folder under the Frame's home to delete, "
                                                         "for example ~/PATCHED")):
    """Free space on the Frame: rollback APKs from reinstalls, leftover uploads, optional extra folders."""
    r = _target(frame).frame.agent("cleanup", rollback=not keep_rollback, paths=path)
    typer.echo(f"removed {len(r['removed'])} item(s), freed {r['freed_bytes'] / 2**30:.1f} GiB")


@frame_app.command("send")
def frame_send(paths: list[Path] = typer.Argument(..., help="files or folders to send"),
               to: str = typer.Option("videos", "--dest", "--to",
                                      help="destination: videos, downloads, documents, app, app-files"),
               game: Optional[str] = typer.Option(None, help="package of the game, for --dest app / app-files"),
               folder: str = typer.Option("", help="sub-folder inside the destination"),
               app: Optional[str] = typer.Option(None, help="package of a player: also add the files to its own folder "
                                                            "(for example 4XVR lists /sdcard/4XPlayer, not Movies)"),
               frame: Optional[str] = typer.Option(None, help=FRAME_HELP)):
    """Send files to apps on the Frame. videos/downloads/documents are shared by every Quest game (they appear as
    /sdcard/Movies, /sdcard/Download, /sdcard/Documents); app/app-files are one game's own storage. Apps find the
    files by browsing folders (Android's media index doesn't work in Lepton)."""
    from .install import files

    r = files.send_files(_target(frame).frame, paths, to, game, folder, printing_reporter(), link_app=app)
    typer.echo(f"sent {r['files']} file(s) ({r['bytes'] / 2**20:.0f} MiB, {r['skipped']} already there); "
               f"in the app: {r['android']}")


@frame_app.command("storage")
def frame_storage(game: Optional[str] = typer.Option(None, help="also show this game's own storage"),
                  frame: Optional[str] = typer.Option(None, help=FRAME_HELP)):
    """Where files for Lepton apps go on the Frame (and where the apps see them)."""
    from .install import files

    for t in files.storage_targets(_target(frame).frame, game):
        typer.echo(f"{t['id']:10} {t['android']:45} {t['path']}" + ("  (every app)" if t["shared"] else ""))


@frame_app.command("drives")
def frame_drives(frame: Optional[str] = typer.Option(None, help=FRAME_HELP),
                 as_json: bool = typer.Option(False, "--json", help=JSON_HELP)):
    """The Frame's drives (internal storage, a microSD card) games can be installed on or moved to."""
    from .install import drives

    found = _target(frame).drives()
    if as_json:
        typer.echo(json.dumps(found, indent=1))
        return
    current = drives.install_dest()
    for d in found:
        free = drives.free_text(d) or "?"
        mark = "*" if (d["internal"] and not current) or current in (d["install_dir"], d["path"]) else " "
        state = f"{d.get('games', 0)} game(s)" if d.get("usable") else f"can't be used: {d.get('reason')}"
        typer.echo(f"{mark} {d['label']:20} {d.get('fstype') or '':6} {free:>16}  {state}")
        if not d["internal"]:
            typer.echo(f"  {'':20} {d['path']}")
    typer.echo("* = where new games go (frameport install --dest, or the app's Frame page → Storage)")


@frame_app.command("move")
def frame_move(package: str = typer.Argument(..., help="an installed game"),
               to_: str = typer.Option(..., "--to", help="'internal' or a drive's path from 'frameport frame drives'"),
               frame: Optional[str] = typer.Option(None, help=FRAME_HELP)):
    """Move an installed game's files to another drive of the Frame (for example the microSD card) or back. Saves, the
    Steam library entry and settings stay as they are."""
    pkg = _pkgs(package, False)[0]
    st = _target(frame).move(pkg, to_, printing_reporter())
    typer.echo(f"moved to {st.get('base') or st.get('to')}")


@frame_app.command("info")
def frame_info(frame: Optional[str] = typer.Option(None, help=FRAME_HELP)):
    """The Frame's SteamOS build, runtimes, storage and installed games (JSON)."""
    typer.echo(json.dumps(_target(frame).describe(), indent=1, default=str))


@frame_app.command("usb-check")
def frame_usb_check(frame: Optional[str] = typer.Option(None, help=FRAME_HELP),
                    no_speed: bool = typer.Option(False, help="skip the upload speed test (2 × 128 MB)")):
    """Check the USB cable link to the Frame: what the Frame presents, this PC's address on it, SSH over it and its
    upload speed vs the normal connection (JSON)."""
    from .frame import usb

    typer.echo(json.dumps(usb.check(_target(frame).connect().frame, measure=not no_speed), indent=1, default=str))


@frame_app.command("controller-models")
def frame_controller_models(frame: Optional[str] = typer.Option(None, help=FRAME_HELP),
                            convert: bool = typer.Option(False, help="also convert them (cached on the Frame)")):
    """SteamVR render models on the Frame and which ones serve as Frame controller models (controller_models)."""
    typer.echo(json.dumps(_target(frame).frame.agent("controller_models", convert=convert), indent=1))


@frame_app.command("proton")
def frame_proton(frame: Optional[str] = typer.Option(None, help=FRAME_HELP),
                 install_: bool = typer.Option(False, "--install", help="install it (Steam on the Frame restarts once "
                                               "and downloads it; waits until done)"),
                 in_headset: bool = typer.Option(False, help="with --install: only ask Steam (confirm in the headset)"),
                 test: bool = typer.Option(False, "--test", help="run a Windows program under Proton on the Frame"),
                 tool: Optional[str] = typer.Option(None, help="compat tool name, for example "
                                                                "proton-experimental-arm64")):
    """Proton (ARM64) on the Frame, needed for PC VR games."""
    t = _target(frame)
    if test:
        r = t.proton_selftest(tool)
        r.pop("log_tail", None)
        typer.echo(json.dumps(r, indent=1))
        raise typer.Exit(0 if r.get("ran") else 1)
    if install_ and in_headset:
        typer.echo(json.dumps(t.install_proton(False, tool), indent=1))
        return
    if install_:
        from .install.installer import ensure_proton

        ready = ensure_proton(t.frame, printing_reporter(False), tool)
        typer.echo(f"ready: {ready['display_name']}")
        return
    st = t.proton_status(tool)
    for p in st["tools"]:
        state = "installed" if p["installed"] and p["require_installed"] else \
            "needs runtime" if p["installed"] else "not installed"
        typer.echo(f"{p['name']:28} {p['display_name']:32} {state}")
    typer.echo(f"ready: {st['ready']['name'] if st.get('ready') else 'no (frameport frame proton --install)'}")
    typer.echo(f"OpenXR runtime: {(st.get('openxr') or {}).get('name') or 'none'}")


# ------------------------------------------------------------------------------------------ pc (Revive)
@pc_app.command("info")
def pc_info():
    """Steam, SteamVR and Revive on this PC (JSON)."""
    typer.echo(json.dumps(_target(None, to="pc").describe(), indent=1, default=str))


@pc_app.command("install-revive")
def pc_install_revive():
    """Download Revive's latest release and unpack it into FramePort's tools (no installer, no admin rights)."""
    from .tools import revive

    path = revive.install(lambda f: None)
    typer.echo(f"Revive {revive.installed_version()} at {path}")


# ------------------------------------------------------------------------------------------ parity
@app.command()
def parity(known_good: Path = typer.Option(..., help="folder of known-good game folders (PATCHED layout)"),
           sources: Path = typer.Option(..., help="folder with the original game dumps"),
           outdir: Path = typer.Option(Path("parity-out"), help="where the rebuilt APKs go"),
           only: list[str] = typer.Option([], help="only games whose folder contains this (repeatable)"),
           report: Path = typer.Option(Path("parity-report.md"), help="the report to write"),
           keep: bool = typer.Option(False, help="keep work files")):
    """Rebuild every catalog game from its source dump and compare with the known-good APKs."""
    from .parity import run_parity

    ok = run_parity(known_good, sources, outdir, report, only, keep, printing_reporter(False))
    raise typer.Exit(0 if ok else 1)


@app.command()
def report(out: Path = typer.Option(Path("REPORT.md"), help="the file to write")):
    """Write a status report of every catalog game (Markdown)."""
    from .report import write

    typer.echo(f"wrote {write(out)}")


@app.command("parity-device")
def parity_device(results: Path = typer.Option(Path("parity-out/parity.json"), help="parity.json from `parity`"),
                  frame: Optional[str] = typer.Option(None, help=FRAME_HELP),
                  baseline: Optional[Path] = typer.Option(None, help="baseline launch.txt from before the change"),
                  report: Path = typer.Option(Path("parity-device-report.md"), help="the report to write"),
                  only: list[str] = typer.Option([], help="only these games (repeatable)"),
                  seconds: int = typer.Option(45, help="launch test length"),
                  test_only: bool = typer.Option(False, help="only re-run the launch tests")):
    """Install the APKs rebuilt by `parity` on the Frame (APK only) and compare headless launches with a baseline."""
    from .parity import install_and_test

    ok = install_and_test(results, _target(frame), baseline, report, printing_reporter(False), only, seconds, test_only)
    raise typer.Exit(0 if ok else 1)


@app.command("uninstall-app")
def uninstall_app(frame: bool = typer.Option(False, help="also remove FramePort's games and files from the Frame"),
                  keep_frame_saves: bool = typer.Option(True, help="with --frame: keep game saves on the Frame"),
                  backup_keys: Optional[Path] = typer.Option(None, help="folder for a zip of the signing keys "
                                                                          "(default: your Documents folder)"),
                  backup: bool = typer.Option(True, "--backup/--no-backup", help="back up the signing keys first"),
                  yes: bool = typer.Option(False, "--yes", "-y", help="don't ask for confirmation")):
    """Remove everything FramePort created on this PC (and optionally on the Frame). Then delete the app itself."""
    from . import uninstall as un

    target = _target(None) if frame else None
    info = target.describe() if target else None
    pl = un.plan(info)
    typer.echo("This removes:")
    for it in pl.items:
        typer.echo(f"  - {it.what}" + (f"  ({it.path})" if it.path else "") +
                   (f"  {it.size / 2**30:.1f} GiB" if it.size > 2**28 else ""))
    dest = (backup_keys or un.default_backup_dir()) if backup else None
    typer.echo(f"Signing keys: {len(pl.keys)} " + ("(not backed up!)" if dest is None else f"→ backup zip in {dest}"))
    if not yes and not typer.confirm("Uninstall FramePort?", default=False):
        raise typer.Exit(1)
    out = un.run(printing_reporter(False), target.frame if target else None, keep_frame_saves, dest, frame)
    typer.echo("Done." + (f" Keys backup: {out['backup']}" if out.get("backup") else "") +
               " Delete the FramePort program folder to finish (or `uv tool uninstall frameport`).")


# ------------------------------------------------------------------------------------------ diagnostics / sharing
def _diag_target(no_frame: bool, frame: Optional[str], to: str):
    if no_frame:
        return None
    try:
        return _target(frame, to=to)
    except Exception as exc:  # noqa: BLE001  (offline Frame: PC-side data only)
        typer.echo(f"({to} not reachable: {exc}; collecting the PC side only)", err=True)
        return None


def _open(url: str, browser: bool) -> None:
    from .core import winhost

    typer.echo(url)
    if browser and not winhost.open_url(url):
        typer.echo("(couldn't open a browser: open the link above)", err=True)


@diag_app.command("collect")
def diag_collect(package: Optional[str] = typer.Argument(None, help="a game (omit for app-wide logs only)"),
                 all_: bool = typer.Option(False, "--all", help=ALL_HELP),
                 frame: Optional[str] = typer.Option(None, help=FRAME_HELP),
                 no_frame: bool = typer.Option(False, "--no-frame", help="don't contact the Frame"),
                 to: str = typer.Option("frame", help="where the game is installed: frame or pc"),
                 out: Optional[Path] = typer.Option(None, help="folder or .zip path (default: Documents)")):
    """Write a redacted diagnostics zip: logs, recipe, analysis, device info (no game files, no personal data)."""
    pkgs = _pkgs(package, all_) if (package or all_) else []
    path = pipeline.collect_diagnostics(pkgs, _diag_target(no_frame, frame, to), printing_reporter(False), out)
    typer.echo(f"wrote {path}")


@diag_app.command("inspect")
def diag_inspect(bundle: Path = typer.Argument(..., help="a diagnostics zip"),
                 as_json: bool = typer.Option(False, "--json", help=JSON_HELP)):
    """Summarize a diagnostics zip and re-triage its launch logs with this version's signatures."""
    from .diag import bundle as b

    res = b.read(bundle)
    if as_json:
        typer.echo(json.dumps(res, indent=1, default=str))
        return
    m = res["manifest"]
    env = m.get("env", {})
    typer.echo(f"created {m.get('created')} by FramePort {env.get('app')}; {env.get('os')}; "
               f"Frame: {(env.get('frame') or {}).get('build_id') or '-'}")
    for w in m.get("warnings", []):
        typer.echo(f"  warning: {w}")
    for pkg, g in res["games"].items():
        typer.echo(f"\n{pkg} — {g.get('title')} [{g.get('status')}]")
        last = g.get("last_test") or {}
        if last:
            typer.echo(f"  last test: {last.get('state')} ({last.get('verdict')}) furthest: {last.get('milestone')}")
        t = g.get("triage")
        if t:
            typer.echo(f"  re-triage of {g['log']}: {t['verdict']}, furthest: {t['milestone']}")
            for f in t["findings"]:
                typer.echo(f"    {f['severity']:7} {f['id']}: {f['diagnosis']}\n            {f['evidence'][:200]}")
            if t["suggestions"]:
                typer.echo(f"  suggested patches: {', '.join(t['suggestions'])}")


@diag_app.command("report")
def diag_report(package: Optional[str] = typer.Argument(None, help="the game (omit for an app problem)"),
                description: str = typer.Option("", "--message", "--text", help="what went wrong"),
                frame: Optional[str] = typer.Option(None, help=FRAME_HELP),
                no_frame: bool = typer.Option(False, "--no-frame", help="don't contact the Frame"),
                to: str = typer.Option("frame", help="where the game is installed: frame or pc"),
                out: Optional[Path] = typer.Option(None, help="folder or .zip path (default: Documents)"),
                browser: bool = typer.Option(True, "--browser/--no-browser", help="open the issue in a browser")):
    """Collect a diagnostics zip and open a prefilled GitHub problem report (attach the zip there)."""
    from .core import winhost

    pkgs = _pkgs(package, False) if package else []
    target = _diag_target(no_frame, frame, to)
    info = None
    if target is not None:
        try:
            info = target.describe()
        except Exception:  # noqa: BLE001
            target = None
    path = pipeline.collect_diagnostics(pkgs, target, printing_reporter(False), out)
    typer.echo(f"wrote {path} — attach it to the issue")
    if browser:
        winhost.open_folder(path, select=True)
    links = pipeline.problem_report_links(pkgs[0] if pkgs else None, description, path, info)
    _open(links.form, browser)
    typer.echo(f"If the form's fields are empty, open a plain issue instead:\n{links.plain}")


@app.command("share-recipe")
def share_recipe(package: str, status: str = typer.Option("works", help="works or issues"),
                 notes: str = typer.Option("", help="what you checked in the headset, known issues"),
                 frame: Optional[str] = typer.Option(None, help="include the Frame's SteamOS build"),
                 browser: bool = typer.Option(True, "--browser/--no-browser", help="open the issue in a browser")):
    """Share a working recipe: saves it as known-good and opens a prefilled GitHub issue."""
    if status not in ("works", "issues"):
        raise typer.BadParameter("--status must be works or issues")
    pkg = _pkgs(package, False)[0]
    info = None
    if frame:
        info = _target(frame).describe()
    links = pipeline.share_working_config_links(pkg, status, notes, info)
    _open(links.form, browser)
    typer.echo(f"If the form's fields are empty, open a plain issue instead:\n{links.plain}")


def main() -> None:
    """Entry point: errors end in one line on stderr and exit code 1 (FRAMEPORT_DEBUG=1 shows the traceback)."""
    import os

    try:
        app()
    except KeyboardInterrupt:
        typer.echo("Cancelled.", err=True)
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("FRAMEPORT_DEBUG"):
            raise
        typer.echo(f"Error: {_describe(exc)}", err=True)
        typer.echo("(set FRAMEPORT_DEBUG=1 to see the traceback)", err=True)
        sys.exit(1)


def _describe(exc: BaseException) -> str:
    """A readable one-line reason (shared with the GUI: frameport.errors)."""
    from .errors import explain

    return explain(exc)


if __name__ == "__main__":  # pragma: no cover
    main()
