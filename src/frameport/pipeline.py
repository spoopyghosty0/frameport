"""High-level operations used by both the CLI and the GUI:

    add (scan/import) → analyze → suggest recipe → [user confirms] → build → static checks
        → install on target → add to library → launch test (+ triage suggestions)

Quest games (APK + OBB) are rebuilt with OVRPort and installed on the Frame (Lepton). Oculus Rift games (Windows PC VR
folders, ids "rift.<slug>") aren't modified: "build" checks them and fetches Revive, and they install either on this
PC (Revive + local Steam) or on the Frame (Proton + Revive).
"""
from __future__ import annotations

import dataclasses
import threading
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
from .patches import upstream
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
    if entry and is_linux(entry):  # a Linux app: the same source again, with this program
        folder = Path(entry["game_dir"])
        if not (folder / exe).is_file():
            raise FileNotFoundError(folder / exe)
        source = ((entry.get("analysis") or {}).get("extra") or {}).get("source") or entry["game_dir"]
        return add_linux_app(source, exe=exe)
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


def is_linux(entry: dict) -> bool:
    """A native arm64 Linux app (AppImage / folder / archive), installed as it is (GitHub #31)."""
    return entry.get("kind") == "linux"


def analysis_warnings(entry: dict) -> list[str]:
    """Blockers read from a Quest/Android game's APK, for the CLI (the game page shows them as callouts)."""
    if is_rift(entry) or is_linux(entry) or not entry.get("analysis"):
        return []
    out = engine.blocker_notes(library.analysis_from_dict(entry["analysis"]))
    if missing_obb(entry):
        out.append(MISSING_OBB_NOTE.format(package=entry.get("package", "<package>")))
    return out


MISSING_OBB_NOTE = ("This game's data file (.obb) wasn't found next to the APK. Put the .obb files in a folder named "
                    "{package} (or obb/) next to the APK and add the folder again; without it the game hangs at start.")


def missing_obb(entry: dict) -> bool:
    """A Quest game whose APK expects an OBB (analysis expects_obb: Unreal's bHasOBBFiles) but no data folder was
    found next to it (GitHub #85: TRIANGLE STRATEGY hung silently after OVRPlugin's JNI_OnLoad). Library fields
    only: no disk access (used by the game page)."""
    if is_rift(entry) or is_linux(entry):
        return False
    extra = (entry.get("analysis") or {}).get("extra") or {}
    return bool(extra.get("expects_obb")) and not (entry.get("data_dir") and entry.get("data_bytes") != 0)


def add_linux_app(path: Path | str, reporter: Reporter | None = None, exe: str | None = None) -> dict:
    """Add a Linux app to the library (no conversion: it's installed as it is): arm64, or x86_64 (run through FEX on
    the Frame). `exe` overrides the program FramePort picked (relative to the app's folder)."""
    from .analysis import linux
    from .core.models import Recipe

    path = Path(path)
    info = linux.inspect(path)
    if exe:
        info["exe"] = exe
    x86 = info["machine"] == linux.EM_X86_64
    if not linux.runs_on_frame(info["machine"]):
        raise ValueError(f"{path.name} is built for machine {info['machine']}, not arm64 (aarch64) or x86_64: it "
                         "can't run on the Frame. Look for an aarch64/arm64 download of it.")
    if x86 and reporter:
        reporter.log(f"{path.name} is an x86_64 build: the Frame runs it through FEX (x86 translation; an arm64 "
                     "build runs faster if there is one)")
    package = f"linux.{linux.slug(info['title'])}"
    if reporter:
        reporter.log(f"{info['title']}: program {info['exe']}{' (AppImage)' if info['appimage'] else ''}"
                     f"{', OpenXR (VR)' if info['openxr'] else ''}")
    recipe = Recipe(package=package, title=info["title"], overport=False, as_is=True, source="heuristics")
    analysis = {"package": package, "label": info["title"], "engine": "Linux", "xr": "OpenXR" if info["openxr"]
                else "none", "abis": ["x86_64" if x86 else "arm64-v8a"], "libs": [],
                "extra": {**info, "vr_kind": "openxr" if info["openxr"] else "none", "x86_64": x86,
                          "source": str(path)}}
    old = library.game(package) or {}
    root = Path(info["root"])
    try:  # what an install uploads (the library's size sort, the Frame's free-space check)
        size = sum((root / f).stat().st_size for f in info["files"]) if info["files"] else \
            sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    except OSError:
        size = 0
    fields = dict(kind="linux", name=path.name, apk=None, game_dir=info["root"], exe=info["exe"], data_dir=None,
                  origin=str(path.parent), analysis=analysis, suggested=library.recipe_to_dict(recipe),
                  data_bytes=size)
    if not old:
        fields.update(title=info["title"], recipe=library.recipe_to_dict(recipe), status="unknown")
    library.upsert_game(package, **fields)
    try:
        from .artwork import fetch

        fetch.artwork_dir(package)
        from .artwork import sources, steam

        icon = None if info["files"] else linux.find_icon(root)  # a lone AppImage's icon comes from the Frame
        if icon:
            sources.apply_app_icon(package, icon.read_bytes())
        steam.ensure_cover(package)  # placeholder art (name on a colour) until the user picks some
    except Exception:  # noqa: BLE001 - artwork is optional
        pass
    return library.game(package)


USER_FOLDERS = ("Downloads", "Desktop", "Documents", "Download")


def _shared_folder(folder: Path) -> bool:
    """A folder a lone program shouldn't take with it to the Frame: home, a user folder (Downloads…) or a drive root."""
    folder = folder.resolve()
    if folder.parent == folder or (str(folder).startswith("/mnt/") and str(folder).rstrip("/").count("/") <= 2):
        return True
    if folder == Path.home() or folder.name in USER_FOLDERS:
        return True
    try:
        from .core import winhost

        prof = winhost.env_path("USERPROFILE") if winhost.available() else None
        return bool(prof) and (folder == prof or folder.parent == prof and folder.name in USER_FOLDERS)
    except Exception:  # noqa: BLE001
        return False


def add_windows_exe(exe: Path | str, reporter: Reporter | None = None) -> dict:
    """Add one Windows program (.exe): a game folder whose program is that exe, run by Proton on the Frame (flat
    unless a VR runtime is found). A program in Downloads, the home folder or a drive root is copied alone into
    FramePort's data folder first, so an install doesn't upload everything next to it."""
    import shutil

    from .core.paths import user_data_dir

    exe = Path(exe)
    if exe.suffix.lower() != ".exe" or not exe.is_file():
        raise ValueError(f"{exe.name} isn't a Windows program (.exe)")
    folder = exe.parent
    if _shared_folder(folder):
        from .analysis import linux

        folder = user_data_dir() / "windows-apps" / linux.slug(exe.stem)
        folder.mkdir(parents=True, exist_ok=True)
        if not (folder / exe.name).exists() or (folder / exe.name).stat().st_size != exe.stat().st_size:
            if reporter:
                reporter.log(f"copying {exe.name} into {folder} (it sits in a shared folder)")
            shutil.copy2(exe, folder / exe.name)
    entry = add_rift_game(folder, reporter, exe=exe.name, force=True, art=True)
    if entry is None:
        raise ValueError(f"{exe.name} couldn't be added")
    return library.upsert_game(entry["package"], exe_confirmed=True)


def add_from_link(manifest, path: Path, reporter: Reporter | None = None, icon: Path | None = None) -> dict:
    """Add a build downloaded from an install link (deeplink.download) to the library: an APK (with any OBB files
    next to it), a Linux build or a Windows program. A manifest's name becomes the title (FrameDrop: "name is what
    shows up in Steam"); a direct file link keeps the title FramePort finds."""
    from . import deeplink

    kind = deeplink.classify(path.name)
    if kind == deeplink.APK:
        added = add_path(path, reporter, art=True)
        if not added:
            raise ValueError(f"{path.name} couldn't be read as an Android app")
        entry = added[0]
    elif kind == deeplink.LINUX:
        entry = add_linux_app(path, reporter)
    elif kind == deeplink.EXE:
        entry = add_windows_exe(path, reporter)
    else:
        raise ValueError(f"FramePort can't install {path.name}")
    pkg = entry["package"]
    fields = {"link": {"source": manifest.source, "name": manifest.name, "time": time.time()}}
    if not manifest.direct:  # a manifest (not a bare file link) names it
        fields.update(title=manifest.name, title_locked=True)
    # FramePort's manifest extension: a description where no store has one, the icon unless the user picked one
    details = dict(library.game(pkg).get("details") or {})
    if manifest.description and not details.get("description"):
        details.update(description=manifest.description, sources=[*(details.get("sources") or []), "link"])
        fields["details"] = details
    library.upsert_game(pkg, **fields)
    if icon:
        from .artwork import fetch, sources

        try:
            if not (fetch.artwork_dir(pkg) / fetch.PICKED).exists():  # the user's own pick stays
                sources.apply_custom(pkg, "icon", icon)
        except Exception as exc:  # noqa: BLE001 - artwork is optional
            if reporter:
                reporter.log(f"icon from the link not used: {exc}")
    return library.game(pkg)


def install_linux(package: str, target: Target, reporter: Reporter, add_to_library: bool = True) -> dict:
    entry = library.game(package)
    extra = (entry.get("analysis") or {}).get("extra") or {}
    result = target.install_linux(package, steam_title(entry), Path(entry["game_dir"]), entry["exe"],
                                  extra.get("files"), bool(extra.get("appimage")), bool(extra.get("openxr")),
                                  reporter, x86_64=bool(extra.get("x86_64")),
                                  desktop_entry=entry.get("desktop_entry", True) is not False)
    if extra.get("x86_64"):
        reporter.check("x86 translation", True, "runs through FEX on SteamOS's x86 system (its libraries come from "
                                                 "there; not checked ahead)")
    elif result.get("missing_libraries"):
        reporter.check("Libraries on the Frame", False, "missing: " + ", ".join(result["missing_libraries"]) +
                       " (SteamOS doesn't have them and the app doesn't bundle them: it won't start)")
    else:
        reporter.check("Libraries on the Frame", True, "everything the app needs is there")
    if add_to_library:
        target.add_to_library([package], reporter)
    _record_install(package, target.label, {"exe": entry["exe"], "result": result, "time": time.time()})
    return result


def add_game(src: SourceGame, reporter: Reporter | None = None) -> dict:
    if reporter:
        reporter.log(f"analyzing {src.apk.name}")
    a = analyze(src.apk, data_bytes=src.data_bytes())
    a.extra["lang_packs"] = langpacks.find_tags(_data_folder(src))
    a.extra["asset_files"] = langpacks.find_content_files(_data_folder(src))
    recipe = engine.suggest(a)
    return library.upsert_game(
        a.package, title=recipe.title or a.label, name=src.name, apk=str(src.apk),
        data_dir=str(src.data_dir) if src.data_dir else None, data_files=src.data_files, data_bytes=src.data_bytes(),
        origin=str(src.origin),
        analysis=a.to_dict(), recipe=library.recipe_to_dict(recipe), suggested=library.recipe_to_dict(recipe),
        status=recipe.status,
    )


def reanalyze(package: str, reporter: Reporter | None = None) -> dict:
    """Read a Quest/Android game's APK again (e.g. after FramePort learned to detect something new). The suggestion is
    refreshed; the recipe too unless the user changed it (then their choices stay)."""
    entry = library.game(package)
    if entry is None or is_rift(entry) or is_linux(entry):
        raise ValueError("only Quest/Android games can be analyzed again")
    return _store_analysis(package, _analyze_entry(entry, reporter))


def _analyze_entry(entry: dict, reporter: Reporter | None = None):
    src = source_of(entry)
    if reporter:
        reporter.log(f"analyzing {src.apk.name}")
    a = analyze(src.apk, data_bytes=src.data_bytes())
    a.extra["lang_packs"] = langpacks.find_tags(_data_folder(src))
    a.extra["asset_files"] = langpacks.find_content_files(_data_folder(src))
    return a


def _store_analysis(package: str, a) -> dict:
    """Replace only the entry's analysis and suggestion; the recipe follows unless it is the user's own (tags, art,
    title, builds and installs are untouched)."""
    suggested = engine.suggest(a)

    def change(g: dict) -> None:
        g["analysis"] = a.to_dict()
        g["suggested"] = library.recipe_to_dict(suggested)
        g.pop("analysis_failed", None)
        # checked under the library lock: the user may have saved a recipe while the APK was read
        if library.recipe_from_dict(g.get("recipe") or {"package": package}).source != "user":
            g["recipe"] = library.recipe_to_dict(suggested)
            g["status"] = suggested.status
    return library.update_game(package, change)


def analysis_outdated(entry: dict) -> bool:
    """A Quest/Android entry analysed by an older FramePort (fields newer patches depend on are missing) whose APK is
    still there, and whose re-analysis didn't already fail for this analysis version. Rift/Linux entries: no."""
    from .analysis.detect import ANALYSIS_VERSION

    a = entry.get("analysis")
    if not isinstance(a, dict) or not entry.get("apk") or is_rift(entry) or is_linux(entry):
        return False
    if str(a.get("package") or "").startswith("rift."):
        return False
    if ((a.get("extra") or {}).get("analysis_version") or 0) >= ANALYSIS_VERSION:
        return False
    if entry.get("analysis_failed") == ANALYSIS_VERSION:
        return False
    try:
        return Path(entry["apk"]).is_file()  # on a drive that isn't connected now: tried again at a later start
    except OSError:
        return False


def outdated_analyses() -> list[str]:
    return [g["package"] for g in library.games() if analysis_outdated(g)]


def refresh_analyses(packages: list[str] | None = None, reporter: Reporter | None = None) -> int:
    """Analyse again the entries an older FramePort analysed (see detect.ANALYSIS_VERSION): runs in the background at
    start (GUI) and before a build (CLI and GUI). Only the APK analysis (no OVRPort, no Cpp2IL). An APK that can't be
    read leaves the entry as it was and is marked, so it isn't read again at every start. Returns how many changed."""
    from .analysis.detect import ANALYSIS_VERSION

    todo = outdated_analyses() if packages is None else \
        [p for p in packages if analysis_outdated(library.game(p) or {})]
    done = 0
    for i, pkg in enumerate(todo):
        entry = library.game(pkg)
        if entry is None or not analysis_outdated(entry):
            continue
        if reporter:
            reporter.check_cancel()
            reporter.progress(i / len(todo), entry.get("title") or pkg)
        try:
            a = _analyze_entry(entry, reporter)
        except Exception as exc:  # noqa: BLE001 - unreadable APK: keep the old analysis, don't retry every start
            if reporter:
                reporter.log(f"{pkg}: couldn't analyze the APK again: {exc}")
            library.update_game(pkg, lambda g: g.__setitem__("analysis_failed", ANALYSIS_VERSION))
            continue
        if a.package != pkg:  # a different APK at that path now: leave the entry alone
            library.update_game(pkg, lambda g: g.__setitem__("analysis_failed", ANALYSIS_VERSION))
            continue
        _store_analysis(pkg, a)
        done += 1
    return done


def _data_folder(src: SourceGame) -> Path | None:
    """The data folder when all of it is the game's data (language packs and content files are looked for there);
    None when only expansion files found by name are (that folder also holds other things, e.g. the APK)."""
    return None if src.data_files is not None else src.data_dir


def data_paths(entry: dict) -> list[Path]:
    """A Quest game's data on this PC: its data folder, or only its expansion files when those were found by name."""
    if not entry.get("data_dir"):
        return []
    d = Path(entry["data_dir"])
    return [d / n for n in entry["data_files"]] if entry.get("data_files") is not None else [d]


def source_of(entry: dict) -> SourceGame:
    return SourceGame(entry.get("name") or entry["package"], Path(entry["apk"]),
                      Path(entry["data_dir"]) if entry.get("data_dir") else None,
                      Path(entry["origin"]) if entry.get("origin") else None, data_files=entry.get("data_files"))


def set_recipe(package: str, recipe: Recipe) -> None:
    library.upsert_game(package, recipe=library.recipe_to_dict(recipe))


def reset_recipe(package: str) -> Recipe:
    """Back to the suggested recipe (catalog or heuristics) for the game's analysis (Quest and Rift alike)."""
    entry = library.game(package)
    recipe = engine.suggest(library.analysis_from_dict(entry["analysis"]))
    set_recipe(package, recipe)
    return recipe


def apply_catalog_update(package: str) -> Recipe:
    """Take the newer catalog recipe for a game whose recipe the user edited (the game page's "newer known-good
    config" offer): derived again from the catalog, keeping the user's FrameBridge settings the catalog doesn't set."""
    entry = library.game(package)
    old = library.recipe_from_dict(entry["recipe"])
    recipe = engine.suggest(library.analysis_from_dict(entry["analysis"]))
    for pid, params in old.patches.items():
        if pid.startswith("adapter.") and pid not in recipe.patches:
            recipe.patches[pid] = params
            if pid in old.reasons:
                recipe.reasons[pid] = old.reasons[pid]
    set_recipe(package, recipe)
    with library.edit() as data:
        data["games"].get(package, {}).pop("catalog_update", None)
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
            "ok": all(c["ok"] is not False for c in checks), "revive": revive.installed_version(), "kind": "rift",
            "recipe_fp": recipe_fingerprint(entry["recipe"])}
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
            "checks": checks, "ok": all(c["ok"] is not False for c in checks), "as_is": True,
            "recipe_fp": recipe_fingerprint(entry["recipe"])}
    library.upsert_game(package, build=info, title=entry.get("title") or store_title)
    return info


def recipe_fingerprint(recipe: dict) -> str:
    from .patches.base import recipe_fingerprint as fp

    return fp(recipe)


_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _build_lock(package: str) -> threading.Lock:
    with _build_locks_guard:
        return _build_locks.setdefault(package, threading.Lock())


def build_game(package: str, reporter: Reporter, outdir: Path | None = None) -> dict:
    if analysis_outdated(library.game(package) or {}):  # analysed by an older FramePort: new fields first
        refresh_analyses([package], reporter)
    entry = library.game(package)
    if is_linux(entry):  # nothing to convert: installed as it is
        return {"ok": True, "linux": True}
    if is_rift(entry):
        return prepare_rift(package, reporter)
    if library.recipe_from_dict(entry["recipe"]).as_is:
        return prepare_as_is(package, reporter)
    src = source_of(entry)
    a = library.analysis_from_dict(entry["analysis"])
    recipe = library.recipe_from_dict(entry["recipe"])
    out = outdir or (output_dir() / quest_dump.display_name(entry.get("name") or package))
    with _build_lock(package):  # one build per game at a time: builds share the game's work folder
        res = builder.build(src, a, recipe, out, reporter)
    art, store_title = artwork.fetch(package, res.apk)
    build_info = {"apk": str(res.apk), "alt_apk": str(res.alt_apk) if res.alt_apk else None, "sha256": res.sha256,
                  "alt_sha256": res.alt_sha256, "applied": res.applied, "checks": res.checks, "ok": res.ok,
                  "overport": res.meta.get("overport"), "recipe_fp": recipe_fingerprint(entry["recipe"]),
                  "superseded": res.meta.get("superseded") or {}}
    library.upsert_game(package, build=build_info, title=entry.get("title") or store_title)
    return build_info


def install_game(package: str, target: Target, reporter: Reporter, apk_only: bool = False,
                 add_to_library: bool = True, apk: Path | None = None) -> dict:
    """Install the game's last build (or `apk`, e.g. a test build of the same package signed with the same key)."""
    entry = library.game(package)
    if is_linux(entry):
        return install_linux(package, target, reporter, add_to_library)
    if is_rift(entry):
        return install_rift(package, target, reporter, add_to_library)
    test_build = apk is not None
    b = entry.get("build") or {}
    recipe = library.recipe_from_dict(entry["recipe"])
    if not test_build and not b.get("apk"):  # never built yet (e.g. `frameport install` right after a scan)
        build_game(package, reporter)
        return install_game(package, target, reporter, apk_only, add_to_library)
    apk = Path(apk) if apk else Path(b["alt_apk"] if recipe.use_alt and b.get("alt_apk") else b["apk"])
    if not test_build and not apk.exists():  # the converted copy was removed after an earlier install: make it again
        build_game(package, reporter)
        return install_game(package, target, reporter, apk_only, add_to_library)
    if not test_build and b.get("superseded"):  # workarounds this build left out (upstream fixed): not in settings.conf
        recipe = dataclasses.replace(recipe, patches=upstream.without_superseded(recipe.patches, b["superseded"]))
    data_dir = Path(entry["data_dir"]) if entry.get("data_dir") else None
    title = steam_title(entry)
    result = target.install(package, title, apk, data_dir, recipe, reporter, apk_only,
                            data_files=entry.get("data_files"))
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


def local_game_files(package: str) -> list[Path]:
    """The game's own files on this PC that "Uninstall → also delete the files on this PC" removes: a Quest game's
    APK and data (OBB) folder, a PC VR game's folder, and FramePort's converted copies of it. Never the signing keys,
    and never a path another library entry still uses (e.g. a shared folder)."""
    g = library.game(package) or {}
    paths: list[Path] = []
    if g.get("kind") == "rift":
        paths += [Path(g["game_dir"])] if g.get("game_dir") else []
    elif is_linux(g):
        paths += linux_local_files(g)
    else:
        paths += [Path(g["apk"])] if g.get("apk") else []
        paths += data_paths(g)
    b = g.get("build") or {}
    out = output_dir().resolve()
    paths += [Path(b[k]) for k in ("apk", "alt_apk") if b.get(k) and out in Path(b[k]).resolve().parents]
    others = [o for o in library.games() if o.get("package") != package]
    used = [Path(p).resolve() for o in others if not is_linux(o)
            for p in (o.get("apk"), o.get("game_dir"), *data_paths(o)) if p]
    used += [p.resolve() for o in others if is_linux(o) for p in linux_local_files(o)]  # (lone AppImages: the file)

    def shared(p: Path) -> bool:  # the same path, a path inside it, or a folder around it belongs to another game
        rp = p.resolve()
        return any(u == rp or rp in u.parents or u in rp.parents for u in used)
    files = [p for p in dict.fromkeys(paths) if p.exists() and not shared(p)]
    if g.get("kind") != "rift" and not is_linux(g):
        files += [m for m in _download_manifests(files) if not shared(m)]
    return files


def _download_manifests(files: list[Path]) -> list[Path]:
    """Download managers leave a manifest next to the game (e.g. "release.manifest": a "#filelist" section of
    "f;./name;size" / "d;./name;0" lines). It goes with the game when it lists nothing but files that are deleted
    anyway; otherwise the folder survives and the download manager still shows the game."""
    gone = [p.resolve() for p in files]
    found = []
    for folder in dict.fromkeys(p.parent for p in files if p.is_file()):
        for m in folder.glob("*.manifest"):
            try:
                lines = m.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            except OSError:
                continue
            start = next((i for i, line in enumerate(lines) if line.strip().lower() == "#filelist"), None)
            if start is None:
                continue
            listed = [line.split(";")[1] for line in lines[start + 1:]
                      if line.count(";") >= 2 and line.split(";")[0] in ("f", "d")]
            targets = [(folder / name).resolve() for name in listed]
            if targets and all(t in gone or any(d in t.parents for d in gone) for t in targets):
                found.append(m)
    return found


def linux_local_files(g: dict) -> list[Path]:
    """A Linux app's own files: a lone AppImage/program (never the folder it sits in, e.g. Downloads), the app's
    folder, or the archive it came from + FramePort's unpacked copy of it."""
    extra = (g.get("analysis") or {}).get("extra") or {}
    root = Path(g["game_dir"]) if g.get("game_dir") else None
    if root is None:
        return []
    if extra.get("files"):
        return [root / f for f in extra["files"]]
    source = Path(extra["source"]) if extra.get("source") else None
    if source is not None and source.is_file():  # an archive: it and its unpacked copy in FramePort's data folder
        return [source, root]
    return [root]


def delete_local_files(package: str) -> tuple[list[str], int]:
    """Delete local_game_files(package), then the game's folder if that left it empty, and drop the library entry (the
    game can't be rebuilt without its files). Returns (deleted paths, bytes freed)."""
    import shutil

    files = local_game_files(package)
    freed, done = 0, []
    # (not for Linux apps: a lone AppImage's folder is the user's, e.g. Downloads)
    parents = set() if is_linux(library.game(package) or {}) else {p.parent for p in files}
    for p in files:
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
        shutil.rmtree(p) if p.is_dir() else p.unlink()
        freed += size
        done.append(str(p))
    for d in parents:  # e.g. "Game v1.2/" that held only the APK and its OBB folder
        try:
            if d.is_dir() and not any(d.iterdir()) and d != output_dir():
                d.rmdir()
                done.append(str(d))
        except OSError:
            pass
    library.remove_game(package)
    return done, freed


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
    if missing_obb(library.game(package) or {}):
        from .validate.triage import add_missing_obb

        add_missing_obb(result)
    from .core.paths import user_data_dir

    logs = user_data_dir() / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / f"{package}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_path.write_text(log or "", encoding="utf-8", errors="replace")
    for old in sorted(logs.glob(f"{package}-*.log"))[:-5]:  # keep the last 5 per game
        old.unlink(missing_ok=True)
    reporter.log(f"full launch log ({len((log or '').splitlines())} lines): {log_path}")
    suggestions = useful_suggestions(package, result.suggestions())
    if proton_alternative_worth_trying(package, result.verdict):
        suggestions.append(PROTON_TOOL)
        reporter.log("it failed on the stable Proton: the suggestion is to try Proton Experimental")
    summary = {"state": result.state, "verdict": result.verdict, "milestone": result.milestone, "fps": result.fps,
               "findings": [f.__dict__ for f in result.findings],
               "suggestions": suggestions,
               "time": time.time(), "target": target.label, "log_path": str(log_path)}
    library.upsert_game(package, last_test=summary)
    return summary


PROTON_TOOL = "pcvr.proton_tool"  # as a suggestion: run the game with Proton Experimental instead of the stable one


def proton_alternative_worth_trying(package: str, verdict: str | None) -> bool:
    """A PC VR game failed its launch test on the stable Proton (the default): Proton Experimental is worth a try."""
    entry = library.game(package) or {}
    if not is_rift(entry) or verdict == "pass":
        return False
    chosen = ((entry.get("recipe") or {}).get("patches") or {}).get(PROTON_TOOL) or {}
    return "experimental" not in (chosen.get("tool") or "")


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
        if pid == PROTON_TOOL:
            recipe.patches[pid] = {"tool": "proton-experimental"}
            recipe.reasons[pid] = "It failed on the stable Proton: trying Proton Experimental."
            continue
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
