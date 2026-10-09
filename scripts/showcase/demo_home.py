#!/usr/bin/env python3
"""Build the demo library (docs/showcase/demo-library.yaml + demo-analyses.json) into a fresh, isolated data dir for
docs screenshots and the demo tour. No personal data: the games are catalog games, their analyses were exported
without paths, local paths are made up (D:/Games/…), and the art + store details come from the same public sources
the app uses (Meta/OculusDB, Steam), cached in ~/.cache/frameport-showcase so later builds (and CI) are offline and
identical. A game whose art can't be fetched keeps FramePort's own placeholder art; the build never fails over art.

    python scripts/showcase/demo_home.py build <dir> [--offline] [--refresh]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "docs" / "showcase" / "demo-library.yaml"
ANALYSES = REPO / "docs" / "showcase" / "demo-analyses.json"
CACHE = Path(os.environ.get("FRAMEPORT_SHOWCASE_CACHE") or Path.home() / ".cache" / "frameport-showcase")
DAY = 86400.0
FETCHED = 1790000000.0  # the "fetched" time stored with cached details (fixed: builds are identical)


def real_data_dir() -> Path:
    """The user's own FramePort data dir (never written by the showcase; not even created by asking)."""
    from frameport.core import paths

    saved = os.environ.pop("FRAMEPORT_HOME", None)
    removed = paths._removed.is_set()
    paths._removed.set()  # (user_data_dir creates the folder unless FramePort is being removed)
    try:
        return paths.user_data_dir().resolve()
    finally:
        if not removed:
            paths._removed.clear()
        if saved is not None:
            os.environ["FRAMEPORT_HOME"] = saved


def load_fixture(path: Path = FIXTURE) -> dict:
    import yaml

    data = yaml.safe_load(path.read_text())
    games = data.get("games") or []
    seen = set()
    for g in games:
        if not isinstance(g, dict) or not g.get("package"):
            raise ValueError(f"{path.name}: every game needs a package: {g!r}")
        if g["package"] in seen:
            raise ValueError(f"{path.name}: {g['package']} is listed twice")
        seen.add(g["package"])
        unknown = set(g) - {"package", "frame", "played", "added", "tested", "tags", "title", "art"}
        if unknown:
            raise ValueError(f"{path.name}: {g['package']}: unknown field(s) {sorted(unknown)}")
        if g.get("frame") not in (None, "internal", "sd", "outdated"):
            raise ValueError(f"{path.name}: {g['package']}: frame must be internal, sd or outdated")
    if (data.get("frame") or {}).get("running") not in (None, *seen):
        raise ValueError(f"{path.name}: frame.running isn't one of the games")
    return data


def fake_sha(package: str, salt: str = "") -> str:
    import hashlib

    return hashlib.sha1(f"showcase:{package}:{salt}".encode()).hexdigest()


def steam_appid(package: str, title: str) -> int:
    return (zlib.crc32(f'"/home/steamos/Applications/quest-frame/{package}/launch.sh"{title}'.encode())
            | 0x80000000) & 0xFFFFFFFF


def _entry(spec: dict, tech: dict, now: float) -> dict:
    """The library fields of one demo game, made the way pipeline.add_game / add_rift_game make them."""
    from frameport.analysis.detect import ANALYSIS_VERSION
    from frameport.core import library
    from frameport.patches.base import recipe_fingerprint
    from frameport.recommend import catalog, engine

    pkg = spec["package"]
    a = library.analysis_from_dict(dict(tech["analysis"], package=pkg))
    rift = pkg.startswith("rift.")
    if not rift:
        a.extra["analysis_version"] = ANALYSIS_VERSION
    recipe = engine.suggest(a)
    title = spec.get("title") or recipe.title or a.label or pkg
    rdict = library.recipe_to_dict(recipe)
    fields = dict(title=title, name=title, data_bytes=tech.get("data_bytes") or 0, analysis=a.to_dict(),
                  recipe=rdict, suggested=rdict, status=recipe.status, added=now - DAY * spec.get("added", 30),
                  build={"sha256": fake_sha(pkg), "ok": True, "recipe_fp": recipe_fingerprint(rdict),
                         "applied": sorted(p for p in rdict.get("patches") or {} if "." in p)})
    if rift:
        folder = f"D:/Games/PC VR/{title}"
        exe = tech.get("exe_rel") or tech.get("exe") or "Game.exe"
        entry = catalog.lookup(pkg)
        fields.update(kind="rift", apk=None, game_dir=folder, exe=exe.split("/", 1)[-1] if "/" in exe else exe,
                      exe_confirmed=True, origin="D:/Games/PC VR", quest_package=entry.quest_package if entry
                      else None, art_source="oculusdb")
        fields["build"].update(kind="rift", revive="3.2.0")
    else:
        folder = f"D:/Games/Quest/{title}"
        fields.update(apk=f"{folder}/{pkg}.apk", data_dir=f"{folder}/{pkg}" if tech.get("data_bytes") else None,
                      origin="D:/Games/Quest")
    if spec.get("tags"):
        fields["tags"] = list(spec["tags"])
    if spec.get("played") is not None:
        fields["last_played"] = now - DAY * spec["played"]
    installed = spec.get("frame") is not None
    if installed:
        t = now - DAY * (spec.get("played", 3) + 0.4)
        fields["installs"] = {"frame": {"result": {"ok": True, "appid": steam_appid(pkg, title)}, "time": t}}
    if spec.get("tested", installed):
        fields["last_test"] = {"state": "RUNNING", "verdict": "pass",
                               "milestone": "Game window created" if rift else "Submitting frames",
                               "fps": None if rift else 72.0, "findings": [], "suggestions": [],
                               "time": now - DAY * (spec.get("played", 3) + 0.39), "target": "frame"}
    return fields


def _art(pkg: str, refresh: bool, offline: bool, log, pick: bool = False) -> str:
    """Art + store details for one game: from the showcase cache, else fetched (and cached). Returns its source."""
    from frameport import pipeline
    from frameport.artwork import fetch
    from frameport.core import library

    cached = CACHE / "games" / (pkg + ("@pick" if pick else ""))
    dest = fetch.artwork_dir(pkg)
    if cached.is_dir() and not refresh:
        for f in cached.iterdir():
            if f.is_file() and f.name != "details.json":
                shutil.copy2(f, dest / f.name)
        details = cached / "details.json"
        if details.exists():
            library.upsert_game(pkg, details=json.loads(details.read_text()))
        return "cache"
    if offline:
        return "none"
    entry = library.game(pkg)
    try:
        if entry.get("kind") == "rift":
            pipeline._rift_art(entry)
        elif pick:  # what a user does when the store's art is a placeholder: the picker's Meta (OculusDB) result
            from frameport.artwork import sources

            choice = next((c for c in sources.search(entry.get("title") or pkg) if c.get("package") == pkg), None)
            if not (choice and sources.apply_choice(pkg, choice)):
                log(f"  {pkg}: the picker had no Meta result, store art instead")
                fetch.fetch(pkg, None, refresh=True)
        else:
            fetch.fetch(pkg, None, refresh=True)
    except Exception as exc:  # noqa: BLE001
        log(f"  {pkg}: no art ({exc})")
    d = pipeline.fetch_details(pkg)
    d["fetched"] = FETCHED
    library.upsert_game(pkg, details=d)
    have = [f for f in dest.iterdir() if f.is_file() and not f.name.startswith("t_")]  # (not thumbnails)
    if not have:
        return "none"
    if cached.exists():
        shutil.rmtree(cached)
    cached.mkdir(parents=True)
    for f in have:
        shutil.copy2(f, cached / f.name)
    (cached / "details.json").write_text(json.dumps(d, indent=1, sort_keys=True))
    return "fetched"


PROFILES = ("demo", "fresh")
SCAN_FILE = "showcase-scan.json"  # the fresh profile's games, found by the pretend folder scan (fakes.scan_job)


def build(home: Path, offline: bool = False, refresh: bool = False, log=print, profile: str = "demo") -> dict:
    """Write the demo library into `home` (emptied first). Sets FRAMEPORT_HOME for this process.

    profile "demo": the library in use (games installed on the pretend Frame, one running). "fresh": FramePort's
    first start (the install tutorial): an empty library, the welcome screen, a Frame with nothing installed and no
    game running; the demo games (art and store details already in place) wait in showcase-scan.json for the
    pretend folder scan, as a scan would find them (no install or play history)."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r} ({', '.join(PROFILES)})")
    home = Path(home).resolve()
    if home == real_data_dir() or real_data_dir() in home.parents:
        raise SystemExit(f"refusing to build the demo library in FramePort's own data dir ({home})")
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    os.environ["FRAMEPORT_HOME"] = str(home)
    for var in ("FRAMEPORT_NO_UPDATE_CHECK", "FRAMEPORT_NO_CATALOG_UPDATE", "FRAMEPORT_NO_LINK_HANDLER"):
        os.environ[var] = "1"
    from frameport.artwork import thumbs
    from frameport.core import library

    fixture = load_fixture()
    tech = json.loads(ANALYSES.read_text())
    now = time.time()
    missing = [g["package"] for g in fixture["games"] if g["package"] not in tech]
    if missing:
        raise SystemExit(f"demo-analyses.json has no analysis for {missing}")
    for spec in fixture["games"]:
        library.upsert_game(spec["package"], **_entry(spec, tech[spec["package"]], now))
    # settings: what a first start would set (no welcome, no migrations or catalog refresh, no update nags)
    from frameport import __version__

    with library.edit() as data:
        s = data.setdefault("settings", {})
        # (ui.easter_eggs off: no holiday badge or milestone confetti in the docs; the hook easter_eggs turns them on)
        s.update({"ui.theme": "portal", "ui.scale": 1.0, "ui.reduce_motion": False, "ui.welcome_done": True,
                  "ui.easter_eggs": False,
                  "ui.language": "en",
                  "catalog.auto_update": False, "update.auto_check": False, "recipes.app_version": __version__,
                  "update.last_seen_version": __version__})
    # the pretend Frame
    installed = []
    for spec in fixture["games"]:
        where = spec.get("frame")
        if where:
            installed.append({"package": spec["package"], "drive": "sd" if where == "sd" else "internal",
                              **({"sha256": fake_sha(spec["package"], "older")} if where == "outdated" else {})})
    state = {"installed": installed, **{k: v for k, v in (fixture.get("frame") or {}).items()}}
    (home / "showcase-frame.json").write_text(json.dumps(state, indent=1))
    # art + store details
    sources: dict[str, list[str]] = {}
    for spec in fixture["games"]:
        src = _art(spec["package"], refresh, offline, log, pick=spec.get("art") == "pick")
        sources.setdefault(src, []).append(spec["package"])
        try:
            thumbs.prewarm(spec["package"])
        except Exception:  # noqa: BLE001
            pass
    # the managed tools are "installed" (no download toast in pictures): stand-in files
    tools = home / "showcase-tools"
    tools.mkdir()
    for var, name in (("FRAMEPORT_JAVA", "java"), ("FRAMEPORT_OVERPORT_JAR", "overport.jar"),
                      ("FRAMEPORT_APKSIGNER_JAR", "apksigner.jar")):
        (tools / name).write_bytes(b"")
        os.environ[var] = str(tools / name)
    if profile == "fresh":
        history = ("installs", "last_test", "last_played", "tags", "build")
        found = [{k: v for k, v in g.items() if k not in history} for g in library.games()]
        (home / SCAN_FILE).write_text(json.dumps(found))
        with library.edit() as data:
            data["games"] = {}
            data["settings"]["ui.welcome_done"] = False
        (home / "showcase-frame.json").write_text(json.dumps({"installed": [], "running": None,
                                                              "battery": state.get("battery", 95)}, indent=1))
    log(f"demo library ({profile}): {len(fixture['games'])} games in {home} (art: "
        + ", ".join(f"{k} {len(v)}" for k, v in sorted(sources.items())) + ")")
    if sources.get("none"):
        log(f"  without art (placeholders): {', '.join(sources['none'])}")
    return {"home": str(home), "games": len(fixture["games"]), "art": sources}


def build_for_render(home: Path, offline: bool = False, allow_missing_art: bool = False,
                     profile: str = "demo") -> dict:
    """build(), but a render must not go on with placeholder art (a store that didn't answer would otherwise put
    placeholders into the docs and the video): exits 4 unless allowed."""
    result = build(home, offline=offline, profile=profile)
    if result["art"].get("none") and not allow_missing_art:
        raise SystemExit(f"no art for {', '.join(result['art']['none'])} (a store didn't answer?): not rendering. "
                         "Try again later, or pass --allow-missing-art")
    return result


def tool_env(home: Path) -> dict:
    """The environment variables a render needs for a built demo home (also for child processes)."""
    tools = Path(home) / "showcase-tools"
    return {"FRAMEPORT_HOME": str(home), "FRAMEPORT_NO_UPDATE_CHECK": "1", "FRAMEPORT_NO_CATALOG_UPDATE": "1",
            "FRAMEPORT_NO_LINK_HANDLER": "1", "FRAMEPORT_JAVA": str(tools / "java"),
            "FRAMEPORT_OVERPORT_JAR": str(tools / "overport.jar"),
            "FRAMEPORT_APKSIGNER_JAR": str(tools / "apksigner.jar")}


def main() -> int:
    sys.path.insert(0, str(REPO / "src"))
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("home", type=Path)
    b.add_argument("--offline", action="store_true", help="only cached art (no network)")
    b.add_argument("--refresh", action="store_true", help="fetch art + details again (updates the cache)")
    b.add_argument("--profile", choices=PROFILES, default="demo", help="demo (in use) or fresh (first start)")
    args = ap.parse_args()
    build(args.home, offline=args.offline, refresh=args.refresh, profile=args.profile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
