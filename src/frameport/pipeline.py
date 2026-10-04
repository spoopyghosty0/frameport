"""High-level operations used by both the CLI and the GUI:

    add (scan/import) → analyze → suggest recipe → [user confirms] → build → static checks
        → install on target → add to library → launch test (+ triage suggestions)

Quest games (APK + OBB) are rebuilt with OVRPort and installed on the Frame (Lepton). Oculus Rift games (Windows PC VR
folders, ids "rift.<slug>") aren't modified: "build" checks them and fetches Revive, and they install either on this
PC (Revive + local Steam) or on the Frame (Proton + Revive).
"""
from __future__ import annotations

import time
from pathlib import Path

from . import build as builder
from .analysis import langpacks
from .analysis.detect import analyze
from .artwork import fetch as artwork
from .core import library
from .core.events import Reporter
from .core.models import Recipe, SourceGame
from .core.paths import output_dir
from .recommend import engine
from .sources import quest_dump, rift_dump
from .targets.base import Target


def add_path(path: Path, reporter: Reporter | None = None, on_added=None, force_rift: bool = False,
             art: bool = False, only_new: bool = False) -> list[dict]:
    """Scan a file/folder (recursively), analyze every game found (Quest APKs and Rift game folders) and store it with
    a suggested recipe. on_added(entry) is called as each game lands in the library (the GUI streams cards in).
    force_rift: `path` is one Rift game folder (added even if no VR runtime is detected). art: fetch artwork too.
    only_new: skip Quest APKs already in the library (a rescan for games added since; Rift folders already skip
    unchanged games)."""
    path = Path(path)
    added = []
    known_apks = {str(Path(g["apk"]).resolve()) for g in library.games() if g.get("apk")} if only_new else set()
    known_packages = {g["package"] for g in library.games()} if only_new else set()

    def done(entry):
        if only_new and entry["package"] in known_packages:  # already in the library (e.g. an unchanged Rift folder)
            return
        added.append(entry)
        if on_added:
            on_added(entry)
    if not force_rift:
        for src in quest_dump.scan(path):
            if reporter:
                reporter.check_cancel()
            if only_new and str(Path(src.apk).resolve()) in known_apks:
                continue
            if only_new:  # another copy of a game already in the library (e.g. a patched build): leave it alone
                try:
                    if quest_dump._package_of(src.apk) in known_packages:
                        continue
                except Exception:  # noqa: BLE001
                    pass
            try:
                entry = add_game(src, reporter)
                if art:
                    try:
                        artwork.fetch(entry["package"], Path(entry["apk"]))
                        from .artwork.steam import ensure_cover

                        ensure_cover(entry["package"])  # no store art (2D apps): a cover with its name and icon
                    except Exception:  # noqa: BLE001
                        pass
                    if not entry.get("details"):
                        fetch_details(entry["package"], reporter)
                        entry = library.game(entry["package"])
                done(entry)
            except Exception as exc:  # keep scanning; report the broken one
                if reporter:
                    reporter.log(f"skipped {src.apk.name}: {exc}")
    if path.is_dir():
        trees: dict = {}
        if reporter:
            reporter.stage("Looking for PC VR games")
        folders = [path] if force_rift else rift_dump.scan(path, trees=trees)
        for i, folder in enumerate(folders):
            if reporter:
                reporter.check_cancel()
                reporter.progress(i / max(len(folders), 1), f"{i + 1} / {len(folders)} · {folder.name}")
            try:
                entry = add_rift_game(folder, reporter, tree=trees.get(folder), force=force_rift, art=art)
                if entry and art and not entry.get("details"):
                    fetch_details(entry["package"], reporter)
                    entry = library.game(entry["package"])
                if entry:
                    done(entry)
            except Exception as exc:
                if reporter:
                    reporter.log(f"skipped {folder.name}: {exc}")
    return added


def _norm_title(t: str) -> str:
    import re

    t = re.sub(r"\b(vr|quest( edition)?|rift|oculus)\b|[^a-z0-9]+", " ", (t or "").lower())
    return " ".join(t.split())


def quest_counterpart(title: str) -> str | None:
    """Package of the Quest version of a Rift game (catalog or library, by title) for linking and store artwork."""
    from .recommend import catalog

    want = _norm_title(title)
    if not want:
        return None
    for e in catalog.load().values():
        if e.kind != "rift" and _norm_title(e.title) == want:
            return e.package
    for g in library.games():
        if g.get("kind") != "rift" and _norm_title(g.get("title") or "") == want:
            return g["package"]
    return None


def _existing_rift(folder: Path) -> dict | None:
    """The library entry for this game folder: same folder, or the same game scanned one level up/down (a repack's
    wrapper folder vs. the game folder inside it)."""
    rifts = [g for g in library.games() if g.get("kind") == "rift" and g.get("game_dir")]
    exact = next((g for g in rifts if g["game_dir"] == str(folder)), None)
    if exact:
        return exact
    for g in rifts:
        other = Path(g["game_dir"])
        if other in folder.parents or folder in other.parents:
            return g
    return None


def add_rift_game(folder: Path, reporter: Reporter | None = None, tree=None, exe: str | None = None,
                  force: bool = False, art: bool = False) -> dict | None:
    """Analyze one Rift game folder and store it. Unchanged folders (same program, size, date) are not analyzed again;
    the user's exe choice, recipe and tags survive rescans. Returns None for folders without a VR runtime (unless
    force)."""
    from .analysis import rift
    from .recommend import catalog

    folder = Path(folder)
    old = _existing_rift(folder)
    old_extra = ((old or {}).get("analysis") or {}).get("extra") or {}
    forced = exe is not None  # set_exe: always analyze with the user's choice
    exe = exe or (old.get("exe") if old and old.get("exe_confirmed") else None)
    if old and not forced and old_extra.get("fingerprint") and \
            old_extra["fingerprint"] == rift.fingerprint(folder, old.get("exe") or ""):
        if reporter:
            reporter.log(f"{old.get('title')}: unchanged")
        if art and not _has_art(old["package"]):
            _rift_art(old, reporter)
        return library.game(old["package"])
    if reporter:
        reporter.log(f"analyzing {folder.name}")
    a = rift.analyze(folder, exe=exe, tree=tree)
    # no VR runtime in it: added on purpose ("Add one game folder…") = a flat Windows game run by Proton
    a.extra["flat"] = not a.extra.get("vr_found")
    if a.extra["flat"] and not force and not old:
        if reporter:
            reporter.log(f"skipped {folder.name}: no VR support (OpenXR, SteamVR or Oculus) found in {a.extra['exe']}")
        return None
    package = old["package"] if old else a.package
    a.package = package
    recipe = engine.suggest(a)
    entry = catalog.lookup(package)
    quest = (old or {}).get("quest_package") or (entry.quest_package if entry else None) or \
        quest_counterpart(recipe.title or a.label)
    fields = dict(kind="rift", name=folder.name, apk=None, game_dir=str(folder), exe=a.extra["exe"],
                  exe_confirmed=a.extra["exe_confirmed"], data_dir=None, data_bytes=a.extra["data_bytes"],
                  origin=str(folder.parent), quest_package=quest, analysis=a.to_dict(),
                  suggested=library.recipe_to_dict(recipe))
    if not old or (old.get("recipe") or {}).get("source") != "user":
        fields.update(recipe=library.recipe_to_dict(recipe), status=recipe.status)
    if not old or not (old.get("title_locked") or old.get("art_source") in ("oculusdb", "meta", "steam")):
        fields["title"] = recipe.title or a.label  # (store-matched titles are kept)
    stored = library.upsert_game(package, **fields)
    if art and not _has_art(package):
        _rift_art(stored, reporter)
    return library.game(package)


def _has_art(package: str) -> bool:
    from .artwork import sources

    return sources.has_art(package)


def _rift_art(entry: dict, reporter: Reporter | None = None) -> dict:
    """Find artwork for a Rift game (Quest version / OculusDB / Steam / exe icon) and record what matched."""
    from .artwork import sources, thumbs

    pkg = entry["package"]
    extra = (entry.get("analysis") or {}).get("extra") or {}
    try:
        found = sources.fetch_rift(pkg, entry.get("title") or pkg, extra.get("canonical_name"),
                                   entry.get("quest_package"), Path(entry["game_dir"]) / entry["exe"],
                                   oculus="LibOVR" in ((entry.get("analysis") or {}).get("xr") or ""))
    except Exception as exc:  # noqa: BLE001 - artwork is optional
        if reporter:
            reporter.log(f"{entry.get('title')}: no artwork ({exc})")
        return {}
    update = {"art_source": found.get("source") or "none"}  # "none": don't retry on every start
    if found.get("quest_package") and not entry.get("quest_package"):
        update["quest_package"] = found["quest_package"]
    if found.get("oculus_app_id"):
        update["oculus_app_id"] = found["oculus_app_id"]
    if found.get("title") and not entry.get("title_locked"):
        update["title"] = found["title"]
    library.upsert_game(pkg, **update)
    try:
        thumbs.prewarm(pkg)
    except Exception:  # noqa: BLE001
        pass
    if reporter:
        reporter.log(f"{entry.get('title')}: artwork from {found.get('source') or 'nowhere'}")
    return found


def fetch_art(package: str, reporter: Reporter | None = None) -> dict:
    """(Re)fetch artwork for any game in the library."""
    entry = library.game(package)
    (artwork.artwork_dir(package) / artwork.PICKED).unlink(missing_ok=True)  # "find automatically" replaces a pick
    if entry.get("kind") == "rift":
        return _rift_art(entry, reporter)
    artwork.fetch(package, Path(entry["apk"]) if entry.get("apk") else None, refresh=True)
    return {"source": "meta"}


def fetch_details(package: str, reporter: Reporter | None = None) -> dict:
    """Description, genres, developer/publisher, release date, links and screenshots (see artwork/details.py)."""
    from .artwork import details, thumbs

    entry = library.game(package)
    try:
        d = details.fetch_details(entry)
    except Exception as exc:  # noqa: BLE001 - details are optional
        if reporter:
            reporter.log(f"{entry.get('title')}: no details ({exc})")
        d = {"sources": [], "fetched": time.time()}
    library.upsert_game(package, details=d)
    for shot in details.screenshot_files(package):
        try:
            thumbs.thumb(shot, 480, shot.stem)
        except Exception:  # noqa: BLE001
            pass
    if reporter:
        reporter.log(f"{entry.get('title')}: details from {', '.join(d.get('sources') or []) or 'nowhere'}"
                     + (f", {len(d.get('screenshots') or [])} screenshots" if d.get("screenshots") else ""))
    return d


def set_exe(package: str, exe: str) -> dict:
    """The user picked the program that starts a Rift game: re-analyze with it (keeps recipe choices and tags)."""
    entry = library.game(package)
    if not entry or entry.get("kind") != "rift":
        raise ValueError(f"{package} is not a Rift game")
    folder = Path(entry["game_dir"])
    if not (folder / exe).is_file():
        raise FileNotFoundError(folder / exe)
    return add_rift_game(folder, exe=exe, force=True)


def steam_title(entry: dict) -> str:
    """Name for the Steam library: "<Title> (Quest)" / "(Rift)" while both versions of a game are in the library."""
    from .core.titles import display_title, twins

    return display_title(entry, twins(library.games()))


def is_rift(entry: dict) -> bool:
    return entry.get("kind") == "rift"


def add_game(src: SourceGame, reporter: Reporter | None = None) -> dict:
    if reporter:
        reporter.log(f"analyzing {src.apk.name}")
    a = analyze(src.apk, data_bytes=src.data_bytes())
    a.extra["lang_packs"] = langpacks.find_tags(src.data_dir)
    recipe = engine.suggest(a)
    return library.upsert_game(
        a.package, title=recipe.title or a.label, name=src.name, apk=str(src.apk),
        data_dir=str(src.data_dir) if src.data_dir else None, data_bytes=src.data_bytes(), origin=str(src.origin),
        analysis=a.to_dict(), recipe=library.recipe_to_dict(recipe), suggested=library.recipe_to_dict(recipe),
        status=recipe.status,
    )


def reanalyze(package: str, reporter: Reporter | None = None) -> dict:
    """Read a Quest/Android game's APK again (e.g. after FramePort learned to detect something new). The suggestion is
    refreshed; the recipe too unless the user changed it (then their choices stay)."""
    entry = library.game(package)
    if entry is None or is_rift(entry):
        raise ValueError("only Quest/Android games can be analyzed again")
    src = source_of(entry)
    if reporter:
        reporter.log(f"analyzing {src.apk.name}")
    a = analyze(src.apk, data_bytes=src.data_bytes())
    a.extra["lang_packs"] = langpacks.find_tags(src.data_dir)
    suggested = engine.suggest(a)
    keep = library.recipe_from_dict(entry["recipe"]).source == "user"
    return library.upsert_game(package, analysis=a.to_dict(), suggested=library.recipe_to_dict(suggested),
                               **({} if keep else {"recipe": library.recipe_to_dict(suggested),
                                                   "status": suggested.status}))


def source_of(entry: dict) -> SourceGame:
    return SourceGame(entry.get("name") or entry["package"], Path(entry["apk"]),
                      Path(entry["data_dir"]) if entry.get("data_dir") else None,
                      Path(entry["origin"]) if entry.get("origin") else None)


def set_recipe(package: str, recipe: Recipe) -> None:
    library.upsert_game(package, recipe=library.recipe_to_dict(recipe))


def reset_recipe(package: str) -> Recipe:
    if is_rift(library.game(package) or {}):
        entry = library.game(package)
        a = library.analysis_from_dict(entry["analysis"])
        recipe = engine.suggest(a)
        set_recipe(package, recipe)
        return recipe
    entry = library.game(package)
    recipe = engine.suggest(library.analysis_from_dict(entry["analysis"]))
    set_recipe(package, recipe)
    return recipe


def prepare_rift(package: str, reporter: Reporter) -> dict:
    """Rift games aren't rebuilt: check the folder still matches the analysis and make sure Revive is available."""
    from .build import sha256
    from .tools import revive

    entry = library.game(package)
    recipe = library.recipe_from_dict(entry["recipe"])
    extra = entry["analysis"].get("extra", {})
    reporter.stage("Check game files")
    exe = Path(entry["game_dir"]) / entry["exe"]
    checks = []

    def check(name, ok, msg=""):
        checks.append({"name": name, "ok": ok, "message": msg})
        reporter.check(name, ok, msg)
    check("Game executable", exe.is_file(), str(exe))
    check("64-bit" if entry["analysis"]["abis"] == ["x86_64"] else "Executable type",
          True if entry["analysis"]["abis"][0] in ("x86", "x86_64") else False, entry["analysis"]["abis"][0])
    check("Oculus Platform SDK", None if extra.get("platform_sdk") else True,
          "entitlement check: needs the Oculus app with a license you own (PC mode only)"
          if extra.get("platform_sdk") else "not used")
    if "pcvr.revive" in recipe.patches:
        reporter.stage("Revive")
        rdir = revive.revive_dir() or revive.install(lambda f: reporter.progress(f, "downloading Revive"))
        check("Revive", True, f"{revive.installed_version()} ({rdir})")
    art, store_title = artwork.fetch(package, lookup=entry.get("quest_package"))
    info = {"sha256": sha256(exe) if exe.is_file() else None, "checks": checks,
            "ok": all(c["ok"] is not False for c in checks), "revive": revive.installed_version(), "kind": "rift"}
    library.upsert_game(package, build=info)
    return info


def prepare_as_is(package: str, reporter: Reporter) -> dict:
    """'Install as is': the APK is used unchanged (already patched). Only checks that it can start in Lepton."""
    from .apk import axml
    from .build import sha256

    entry = library.game(package)
    apk = Path(entry["apk"])
    a = entry["analysis"]
    reporter.stage("Checking the game (installing it as it is)")
    checks = []

    def check(name, ok, msg=""):
        checks.append({"name": name, "ok": ok, "message": msg})
        reporter.check(name, ok, msg)
    check("APK", apk.is_file(), str(apk))
    abis = a.get("abis") or []
    check("64-bit (arm64-v8a)", "arm64-v8a" in abis or not abis or None,
          ", ".join(abis) if abis else "no native code (Java/Kotlin only): runs on any CPU")
    try:
        import zipfile

        with zipfile.ZipFile(apk) as z:
            cats = axml.categories(z.read("AndroidManifest.xml"))
        check("Launcher entry for Lepton", axml.LAUNCHER in cats or None,
              "present" if axml.LAUNCHER in cats
              else "missing: Lepton may not find the game (turn off 'Install as is')")
    except Exception as exc:  # noqa: BLE001
        check("Manifest", None, str(exc))
    art, store_title = artwork.fetch(package, apk)
    info = {"apk": str(apk), "alt_apk": None, "sha256": sha256(apk), "alt_sha256": None, "applied": [],
            "checks": checks, "ok": all(c["ok"] is not False for c in checks), "as_is": True}
    library.upsert_game(package, build=info, title=entry.get("title") or store_title)
    return info


def build_game(package: str, reporter: Reporter, outdir: Path | None = None) -> dict:
    entry = library.game(package)
    if is_rift(entry):
        return prepare_rift(package, reporter)
    if library.recipe_from_dict(entry["recipe"]).as_is:
        return prepare_as_is(package, reporter)
    src = source_of(entry)
    a = library.analysis_from_dict(entry["analysis"])
    recipe = library.recipe_from_dict(entry["recipe"])
    out = outdir or (output_dir() / quest_dump.display_name(entry.get("name") or package))
    res = builder.build(src, a, recipe, out, reporter)
    art, store_title = artwork.fetch(package, res.apk)
    build_info = {"apk": str(res.apk), "alt_apk": str(res.alt_apk) if res.alt_apk else None, "sha256": res.sha256,
                  "alt_sha256": res.alt_sha256, "applied": res.applied, "checks": res.checks, "ok": res.ok,
                  "overport": res.meta.get("overport")}
    library.upsert_game(package, build=build_info, title=entry.get("title") or store_title)
    return build_info


def install_game(package: str, target: Target, reporter: Reporter, apk_only: bool = False,
                 add_to_library: bool = True, apk: Path | None = None) -> dict:
    """Install the game's last build (or `apk`, e.g. a test build of the same package signed with the same key)."""
    entry = library.game(package)
    if is_rift(entry):
        return install_rift(package, target, reporter, add_to_library)
    test_build = apk is not None
    b = entry.get("build") or {}
    recipe = library.recipe_from_dict(entry["recipe"])
    apk = Path(apk) if apk else Path(b["alt_apk"] if recipe.use_alt and b.get("alt_apk") else b["apk"])
    if not test_build and not apk.exists():  # the converted copy was removed after an earlier install: make it again
        build_game(package, reporter)
        return install_game(package, target, reporter, apk_only, add_to_library)
    data_dir = Path(entry["data_dir"]) if entry.get("data_dir") else None
    title = steam_title(entry)
    result = target.install(package, title, apk, data_dir, recipe, reporter, apk_only)
    if add_to_library:
        target.add_to_library([package], reporter)
    _record_install(package, target.label, {"apk": str(apk), "result": result, "time": time.time()})
    if not test_build:
        remove_converted_copies(package)
    return result


def converted_copies_size() -> int:
    return sum(p.stat().st_size for p in output_dir().rglob("*.apk") if p.is_file())


def remove_all_converted_copies() -> int:
    """Remove every converted APK in FramePort's output folder (installs convert again when needed)."""
    import shutil

    freed = converted_copies_size()
    for d in output_dir().iterdir():
        shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
    return freed


def remove_converted_copies(package: str) -> int:
    """The converted APKs are only needed until they're on the Frame (every install converts again): remove them
    unless the user keeps them (setting build.keep_copies). Only files in FramePort's own output folder, never the
    game files the user added. Returns the bytes freed."""
    if library.setting("build.keep_copies", False):
        return 0
    b = (library.game(package) or {}).get("build") or {}
    out, freed = output_dir().resolve(), 0
    for key in ("apk", "alt_apk"):
        p = Path(b[key]) if b.get(key) else None
        if p and p.exists() and out in p.resolve().parents:
            freed += p.stat().st_size
            p.unlink()
            if not any(p.parent.iterdir()):
                p.parent.rmdir()
    return freed


def _record_install(package: str, where: str, record: dict) -> None:
    """Merge into the game's current install records (the entry read before a long install may be stale)."""
    library.update_game(package, lambda g: g.setdefault("installs", {}).__setitem__(where, record))


def install_rift(package: str, target: Target, reporter: Reporter, add_to_library: bool = True) -> dict:
    from .tools import revive

    entry = library.game(package)
    if not (entry.get("build") or {}).get("ok"):
        prepare_rift(package, reporter)
        entry = library.game(package)
    recipe = library.recipe_from_dict(entry["recipe"])
    title = steam_title(entry)
    rdir = revive.revive_dir() if "pcvr.revive" in recipe.patches else None
    result = target.install_pcvr(package, title, Path(entry["game_dir"]), entry["exe"], recipe, reporter,
                                 revive_dir=rdir, revive_version=revive.installed_version() if rdir else None,
                                 exe_sha256=entry["build"].get("sha256"), art_lookup=entry.get("quest_package"))
    if add_to_library:
        target.add_to_library([package], reporter)
    _record_install(package, target.label, {"exe": entry["exe"], "result": result, "time": time.time()})
    return result


def test_game(package: str, target: Target, reporter: Reporter, seconds: int = 45) -> dict:
    result, log = target.launch_test(package, reporter, seconds)
    from .core.paths import user_data_dir

    logs = user_data_dir() / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / f"{package}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_path.write_text(log or "", encoding="utf-8", errors="replace")
    for old in sorted(logs.glob(f"{package}-*.log"))[:-5]:  # keep the last 5 per game
        old.unlink(missing_ok=True)
    reporter.log(f"full launch log ({len((log or '').splitlines())} lines): {log_path}")
    summary = {"state": result.state, "verdict": result.verdict, "milestone": result.milestone, "fps": result.fps,
               "findings": [f.__dict__ for f in result.findings],
               "suggestions": useful_suggestions(package, result.suggestions()),
               "time": time.time(), "target": target.label, "log_path": str(log_path)}
    library.upsert_game(package, last_test=summary)
    return summary


def useful_suggestions(package: str, suggestions: list[str]) -> list[str]:
    """Triage suggestions that would change something: not already on, and not conflicting with the recipe (e.g. no
    Revive for a repack that runs its own)."""
    from .patches.base import REGISTRY

    entry = library.game(package) or {}
    on = set((entry.get("recipe") or {}).get("patches") or {})
    out = []
    for pid in suggestions:
        p = REGISTRY.get(pid)
        if pid in on or (p and any(c in on for c in p.conflicts)):
            continue
        out.append(pid)
    return out


def apply_suggestions(package: str, suggestions: list[str]) -> Recipe:
    """Add triage-suggested patches to the game's recipe (the user confirms in the UI before rebuilding)."""
    entry = library.game(package)
    recipe = library.recipe_from_dict(entry["recipe"])
    for pid in suggestions:
        if pid.startswith("adapter."):
            from .patches.base import get

            recipe.patches[pid] = {"value": 1 if get(pid).params[0].kind == "int" else get(pid).params[0].default}
        else:
            recipe.patches.setdefault(pid, {})
        recipe.reasons[pid] = "Suggested by log triage."
        if pid == "patch_remove_unreal_force_quit":
            recipe.use_alt = True
            if pid not in recipe.alt_patches:
                recipe.alt_patches.append(pid)
            recipe.patches.pop(pid, None)
    set_recipe(package, recipe)
    return recipe


# ------------------------------------------------------------------------------------------ sharing / diagnostics
def collect_diagnostics(packages: list[str] | None, target: Target | None, reporter: Reporter,
                        dest: Path | None = None, extra: dict[str, str] | None = None) -> Path:
    """Redacted diagnostics zip (docs/DIAGNOSTICS.md) for some games (None = app-wide only)."""
    from .diag import bundle

    return bundle.collect(packages, target, reporter, dest, extra)


def share_working_config(package: str, status: str = "works", notes: str = "",
                         frame_info: dict | None = None) -> str:
    """Save the game's recipe as a known-good user recipe and return a prefilled GitHub issue link that submits it
    to the catalog (a maintainer turns accepted ones into a PR)."""
    from .diag import bundle, issue, redact
    from .recommend import catalog

    g = library.game(package)
    if not g or not g.get("recipe"):
        raise ValueError(f"{package} has no recipe")
    env = bundle.env_info(frame_info)
    verified = {"app": env.get("app"), "overport_cli": (env.get("tools") or {}).get("overport"),
                "frame_build": (env.get("frame") or {}).get("build_id"),
                "agent": (env.get("frame") or {}).get("agent_version")}
    entry = catalog.entry_from_library(g, status=status, notes=notes or None, verified=verified)
    catalog.save_user_entry(entry)
    return issue.working_config_url(g, catalog.to_yaml(entry), status, notes, env, redact.default(frame_info))


def problem_report(package: str | None, description: str, bundle_path: Path | None,
                   frame_info: dict | None = None) -> str:
    """A prefilled GitHub bug-report link (the user attaches the diagnostics zip)."""
    from .diag import bundle, issue, redact
    from .recommend import catalog

    g = library.game(package) if package else None
    recipe = ""
    if g and g.get("recipe"):
        try:
            recipe = catalog.to_yaml(catalog.entry_from_library(g, status=g.get("status") or "unknown"))
        except Exception:  # noqa: BLE001
            recipe = ""
    return issue.problem_url(g, description, bundle.env_info(frame_info),
                             bundle_path.name if bundle_path else None, recipe, redact.default(frame_info))
