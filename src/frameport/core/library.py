"""Persistent app state: the games the user added, their confirmed recipes, builds and install results."""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .models import Analysis, Recipe, data_manifest
from .paths import localize, user_data_dir, write_atomic

# One lock for every read-modify-write of library.json: jobs, the poll thread and the UI all change it, and a change
# made between another thread's load() and save() would be lost. Re-entrant so edit() can call load()/save().
_lock = threading.RLock()
_last_retry = 0.0  # when migrations waiting for a game folder were last retried
_last_data_retry = 0.0  # unavailable SideQuest backups: retry at most every 10 minutes


def _path() -> Path:
    return user_data_dir() / "library.json"


_updating = threading.local()  # set while load() runs the migrations/catalog step (see load)


def load() -> dict:
    with _lock:
        try:
            data = localize(json.loads(_path().read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {"games": {}, "settings": {}}
        # The catalog step loads the catalog, whose first load reads a setting: that nested load() must not run the
        # step again (GitHub #58: ~140 nested loads and a 30 s blank window, or a crash at start-up)
        if getattr(_updating, "active", False):
            return data
        _updating.active = True
        try:
            changed = _migrate(data) | _follow_catalog(data)
        finally:
            _updating.active = False
        if changed:
            save(data)
        return data


def peek_setting(key: str, default=None):
    """A setting as stored, without migrations or the catalog step (for code that load() itself runs)."""
    with _lock:
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
    settings = data.get("settings") if isinstance(data, dict) else None
    return settings.get(key, default) if isinstance(settings, dict) else default


@contextmanager
def edit() -> Iterator[dict]:
    """A read-modify-write of the whole library as one step: `with edit() as data: ...` (saved on exit)."""
    with _lock:
        data = load()
        yield data
        save(data)


REFRESH_ON_UPDATE = True  # (tests switch it off: their hand-made libraries have no version marker)


def _follow_catalog(data: dict) -> bool:
    """Recipes FramePort derived (not ones the user edited) stay current: when their catalog entry is new or changed,
    and once after every app update (new automatic fixes and suggestions in FramePort's code), the game's recipe is
    derived again; a changed recipe shows Update on Frame. Data recovery skipped for unavailable backups is retried
    on later reads, at most every ten minutes."""
    from .. import __version__
    from ..recommend import catalog, engine

    settings = data.setdefault("settings", {})
    app_updated = REFRESH_ON_UPDATE and settings.get("recipes.app_version") != __version__
    changed = app_updated
    settings["recipes.app_version"] = __version__
    global _last_data_retry
    retry_data = time.time() - _last_data_retry > 600
    for pkg, g in (data.get("games") or {}).items():
        r, a = g.get("recipe"), g.get("analysis")
        if g.get("kind") == "linux":  # installed as it is: no recipe to derive
            continue
        data_refreshed = False
        if isinstance(a, dict) and (app_updated or (retry_data and g.get("data_refresh_pending"))):
            _last_data_retry = time.time()
            data_refreshed = _refresh_data_fields(g, a)
            changed |= data_refreshed
        if not isinstance(r, dict) or not isinstance(a, dict):
            continue
        if r.get("source") == "user":
            # the user's own recipe isn't replaced, but a changed catalog entry is offered on the game page (GitHub
            # #10: a VR4 fix never reached a recipe whose Game settings had been saved once)
            entry = catalog.lookup(pkg)
            offer = entry.rev() if entry is not None and r.get("catalog_rev") not in (None, entry.rev()) else None
            if g.get("catalog_update") != offer:
                if offer:
                    g["catalog_update"] = offer
                else:
                    g.pop("catalog_update", None)
                changed = True
            continue
        if g.pop("catalog_update", None) is not None:  # no longer the user's recipe (reset): nothing to offer
            changed = True
        entry = catalog.lookup(pkg)
        if not app_updated and not data_refreshed and (entry is None or r.get("catalog_rev") == entry.rev()):
            continue
        try:
            g["recipe"] = recipe_to_dict(engine.suggest(analysis_from_dict(a)))
        except Exception:  # noqa: BLE001 - a malformed entry keeps its recipe
            continue
        changed = True
    return changed


def _refresh_data_fields(g: dict, a: dict) -> bool:
    """Analysis fields read from the game's data folder (without full APK analysis) that newer FramePort versions added:
    filled in for games already in the library, so their patches are offered without a re-analysis."""
    extra = a.setdefault("extra", {})
    if a.get("package", "").startswith("rift."):
        return False
    from ..analysis import langpacks
    from ..sources import quest_dump

    repaired = False
    files = g.get("data_files")
    apk = Path(g.get("apk") or "")
    if (files and g.get("data_dir") and apk.parent.name.lower() in quest_dump.APK_FOLDERS
            and Path(g["data_dir"]).name.lower() in quest_dump.OBB_FOLDERS
            and Path(g["data_dir"]).parent.resolve() == apk.parent.parent.resolve()
            and all(quest_dump.OBB_NAME.match(name) for name in files)):
        # Older scans kept only named OBBs from SideQuest's sibling obb folder, dropping PAKs/language packs.
        # Recover only that same folder; keep manually selected data paths and shared folders as they were.
        if not apk.is_file() or not Path(g["data_dir"]).is_dir():
            return _defer_data_refresh(g)
        _, version_code = quest_dump._apk_ids(apk)
        try:
            data, selected = quest_dump.find_data(apk.parent, a.get("package"), version_code, strict=True)
        except OSError:
            return _defer_data_refresh(g)
        if data is not None and selected is None and data.resolve() == Path(g["data_dir"]).resolve():
            try:
                size = sum(data_manifest(data).values())
                tags = langpacks.find_tags(data)
                assets = langpacks.find_content_files(data)
            except OSError:
                return _defer_data_refresh(g)
            g["data_files"] = None
            g["data_bytes"] = size
            extra["data_bytes"] = size
            extra["lang_packs"] = tags
            extra["asset_files"] = assets
            repaired = True
    if "lang_packs" in extra and "asset_files" in extra:
        return bool(g.pop("data_refresh_pending", False)) or repaired

    data = g.get("data_dir") if g.get("data_files") is None else None  # expansion files only: no packs
    try:
        extra.setdefault("lang_packs", langpacks.find_tags(data))
        extra.setdefault("asset_files", langpacks.find_content_files(data))
    except OSError:
        return False
    g.pop("data_refresh_pending", None)
    return True


def _defer_data_refresh(g: dict) -> bool:
    changed = not g.get("data_refresh_pending")
    g["data_refresh_pending"] = True
    return changed


def _migrate(data: dict) -> bool:
    """One-time updates of stored recipes when a new default is introduced (each runs once per library)."""
    done = data.setdefault("settings", {}).setdefault("migrations", [])
    changed = False
    if "no_crash_reporter" not in done:
        # the Unreal crash reporter is disabled by default for PC VR Unreal games, also those added before
        for g in (data.get("games") or {}).values():
            a, r = g.get("analysis") or {}, g.get("recipe")
            if isinstance(r, dict) and (a.get("extra") or {}).get("kind") == "rift" and a.get("engine") == "Unreal":
                r.setdefault("patches", {}).setdefault("pcvr.no_crash_reporter", {})
                r.setdefault("reasons", {}).setdefault(
                    "pcvr.no_crash_reporter", "Unreal game: close on a crash instead of showing the crash reporter.")
        done.append("no_crash_reporter")
        changed = True
    if "xr_compat_layer" not in done:
        # the Frame OpenXR layer is needed by every PC VR game on the Frame (OpenXR 1.1 → 1.0 fallback)
        for g in (data.get("games") or {}).values():
            a, r = g.get("analysis") or {}, g.get("recipe")
            if isinstance(r, dict) and (a.get("extra") or {}).get("kind") == "rift":
                r.setdefault("patches", {}).setdefault("pcvr.xr_timefix", {})
                r.setdefault("reasons", {}).setdefault(
                    "pcvr.xr_timefix", "The Frame's OpenXR runtime rejects Proton's OpenXR 1.1 request without it.")
        done.append("xr_compat_layer")
        changed = True
    if "oculus_unreal" not in done:
        # the Oculus detection patch is on by default for Unreal PC VR games that use LibOVR, also those added before
        for g in (data.get("games") or {}).values():
            a, r = g.get("analysis") or {}, g.get("recipe")
            extra = a.get("extra") or {}
            if (isinstance(r, dict) and extra.get("kind") == "rift" and a.get("engine") == "Unreal"
                    and not extra.get("openxr_native")):
                r.setdefault("patches", {}).setdefault("pcvr.oculus_unreal", {})
                r.setdefault("reasons", {}).setdefault(
                    "pcvr.oculus_unreal",
                    "Unreal game: its Oculus plugin checks for the Oculus service before it starts VR.")
        done.append("oculus_unreal")
        changed = True
    if "rift_revive_correct" not in done:
        # Correct the earlier churn: re-derive every Rift recipe from the engine defaults (Oculus games get Revive for
        # VR; OpenXR/SteamVR-native and catalog games keep their own settings). The dump is never modified (as_is).
        from ..recommend import engine

        for g in (data.get("games") or {}).values():
            a = g.get("analysis") or {}
            if not (isinstance(g.get("recipe"), dict) and (a.get("extra") or {}).get("kind") == "rift"):
                continue
            try:
                rec = engine.suggest(analysis_from_dict(a))
            except Exception:  # noqa: BLE001 - a malformed entry keeps its recipe
                continue
            g["recipe"] = recipe_to_dict(rec)
        done.append("rift_revive_correct")
        changed = True
    if "rift_frame_native" not in done:
        # Re-analyze Rift games so existing entries pick up openvr_native/frame_native (honest Frame vs PC routing),
        # then re-derive their recipes. Needs the game files; entries without a readable game_dir keep what they have.
        from ..analysis import rift
        from ..recommend import engine

        for g in (data.get("games") or {}).values():
            a = g.get("analysis") or {}
            if not (isinstance(g.get("recipe"), dict) and (a.get("extra") or {}).get("kind") == "rift"):
                continue
            gd = g.get("game_dir") or (a.get("extra") or {}).get("folder")
            try:
                if gd and Path(gd).is_dir():
                    an = rift.analyze(Path(gd), exe=(a.get("extra") or {}).get("exe"))
                    g["analysis"] = asdict(an)
                else:  # no game files: derive frame_native from the old analysis flags (unknown -> Oculus/PC)
                    x = a.setdefault("extra", {})
                    x.setdefault("frame_native", bool(x.get("openxr_native") or x.get("openvr_native")))
                    an = analysis_from_dict(a)
                g["recipe"] = recipe_to_dict(engine.suggest(an))
            except Exception:  # noqa: BLE001
                continue
        done.append("rift_frame_native")
        changed = True
    if "rift_libovr_redirect" not in done:
        # Oculus Rift recipes gain the LoadLibrary redirect (provide Revive's runtime where the game looks for LibOVRRT)
        for g in (data.get("games") or {}).values():
            a, r = g.get("analysis") or {}, g.get("recipe")
            if (isinstance(r, dict) and (a.get("extra") or {}).get("kind") == "rift"
                    and "pcvr.revive" in (r.get("patches") or {})):
                r.setdefault("patches", {}).setdefault("pcvr.libovr_redirect", {})
                r.setdefault("reasons", {}).setdefault(
                    "pcvr.libovr_redirect", "Lets the game find Revive's runtime on the Frame.")
        done.append("rift_libovr_redirect")
        changed = True
    if "rift_launch_modes" not in done:
        # Rift games get a launch mode from their files (repack with bundled Revive → run directly; SteamVR/OpenXR-
        # capable → run directly with arguments; Oculus-only → FramePort's Revive). Re-analyze (keeping the chosen
        # exe) and re-derive the recipe; a repack launched through a second Revive failed (e.g. its entitlement check).
        waiting = []
        for pkg, g in (data.get("games") or {}).items():
            a = g.get("analysis") or {}
            if not (isinstance(g.get("recipe"), dict) and (a.get("extra") or {}).get("kind") == "rift"):
                continue
            if not _rift_launch_mode(g):
                waiting.append(pkg)  # game folder not reachable now (e.g. a drive not connected): retried later
        if waiting:
            data["settings"].setdefault("migrations_waiting", {})["rift_launch_modes"] = waiting
        done.append("rift_launch_modes")
        changed = True
    global _last_retry
    waiting = (data["settings"].get("migrations_waiting") or {}).get("rift_launch_modes") or []
    if waiting and time.time() - _last_retry > 600:  # games skipped because their folder wasn't there (checked
        _last_retry = time.time()                     # every 10 min: load() runs often, a missing drive is slow)
        still = [p for p in waiting if p in data.get("games", {}) and not _rift_launch_mode(data["games"][p])]
        if still != waiting:
            data["settings"]["migrations_waiting"]["rift_launch_modes"] = still
            changed = True
    if "quest_binary_fixes_v2" not in done:
        # Quest recipes gain the default-on binary fixes: the swapchain size guard (every overport build) and the
        # Vulkan shim (Unreal)
        from ..patches import base

        for g in (data.get("games") or {}).values():
            a, r = g.get("analysis") or {}, g.get("recipe")
            if not isinstance(r, dict) or (a.get("extra") or {}).get("kind") == "rift" or r.get("as_is"):
                continue
            try:
                an = analysis_from_dict(a)
            except Exception:  # noqa: BLE001
                continue
            for pid in ("frame.swapchain_limit", "frame.vk_sanitize"):
                s = base.get(pid).detect(an)
                if s and s.recommended:
                    r.setdefault("patches", {}).setdefault(pid, {})
                    r.setdefault("reasons", {}).setdefault(pid, s.reason)
        done.append("quest_binary_fixes_v2")
        changed = True
    if "flat_hide_navbar" not in done:
        # Android apps without VR gain the default-on "Hide Android's navigation bar" patch
        for g in (data.get("games") or {}).values():
            a, r = g.get("analysis") or {}, g.get("recipe")
            if isinstance(r, dict) and (a.get("extra") or {}).get("vr_kind") == "none" and g.get("kind") != "linux":
                r.setdefault("patches", {}).setdefault("device.hide_navbar", {})
                r.setdefault("reasons", {}).setdefault(
                    "device.hide_navbar", "2D app: Android's navigation buttons would cover the app's own controls.")
        done.append("flat_hide_navbar")
        changed = True
    if "rift_steamvr_tuning" not in done:
        # PC VR recipes gain automatic SteamVR performance settings (refresh rate / motion smoothing on frame drops)
        for g in (data.get("games") or {}).values():
            a, r = g.get("analysis") or {}, g.get("recipe")
            if isinstance(r, dict) and (a.get("extra") or {}).get("kind") == "rift":
                r.setdefault("patches", {}).setdefault("pcvr.steamvr_tuning", {})
                r.setdefault("reasons", {}).setdefault(
                    "pcvr.steamvr_tuning", "Adjusts the refresh rate / motion smoothing when the game can't keep up.")
        done.append("rift_steamvr_tuning")
        changed = True
    if "batman_video_patches" not in done:
        # Batman's cutscene playback (hardware HEVC + its native video renderer) was tied to its package name inside
        # frame.adapter; it is now frame.hw_video_decode + adapter.surface_native, so user-owned recipes keep it
        for pkg, g in (data.get("games") or {}).items():
            r = g.get("recipe")
            if pkg == BATMAN and isinstance(r, dict):
                r.setdefault("patches", {}).setdefault("frame.hw_video_decode", {})
                r["patches"].setdefault("adapter.surface_native", {"value": 1})
                r.setdefault("reasons", {}).setdefault(
                    "frame.hw_video_decode", "Its cutscenes are 8K HEVC video: decode them on the Frame's hardware.")
                r["reasons"].setdefault("adapter.surface_native", "Shows its cutscenes as stereo panoramas.")
        done.append("batman_video_patches")
        changed = True
    if "meta_spatial_permissions" not in done:
        # Apps that use Meta scene/spatial permissions gain frame.meta_permissions by default
        from ..patches import base

        for g in (data.get("games") or {}).values():
            a, r = g.get("analysis") or {}, g.get("recipe")
            if not isinstance(r, dict) or (a.get("extra") or {}).get("kind") == "rift" or r.get("as_is"):
                continue
            try:
                an = analysis_from_dict(a)
            except Exception:  # noqa: BLE001
                continue
            s = base.get("frame.meta_permissions").detect(an)
            if s and s.recommended:
                r.setdefault("patches", {}).setdefault("frame.meta_permissions", {})
                r.setdefault("reasons", {}).setdefault("frame.meta_permissions", s.reason)
        done.append("meta_spatial_permissions")
        changed = True
    if "meta_depth_occlusion" not in done:
        # Apps that use Meta depth occlusion gain frame.meta_depth_occlusion by default
        from ..patches import base
        from ..recommend import catalog

        for pkg, g in (data.get("games") or {}).items():
            a, r = g.get("analysis") or {}, g.get("recipe")
            if not isinstance(r, dict) or (a.get("extra") or {}).get("kind") == "rift" or r.get("as_is"):
                continue
            entry = catalog.lookup(pkg)
            if entry and "frame.meta_depth_occlusion" in entry.frame:
                r.setdefault("patches", {}).setdefault("frame.meta_depth_occlusion", {})
                r.setdefault("reasons", {}).setdefault(
                    "frame.meta_depth_occlusion", "Disables missing Meta depth sensor occlusion."
                )
            else:
                try:
                    an = analysis_from_dict(a)
                    s = base.get("frame.meta_depth_occlusion").detect(an)
                    if s and s.recommended:
                        r.setdefault("patches", {}).setdefault("frame.meta_depth_occlusion", {})
                        r.setdefault("reasons", {}).setdefault("frame.meta_depth_occlusion", s.reason)
                except Exception:  # noqa: BLE001
                    pass
        done.append("meta_depth_occlusion")
        changed = True
    return changed



BATMAN = "com.camouflaj.manta"


def _rift_launch_mode(g: dict) -> bool:
    """Re-analyze a Rift game (keeping the chosen exe) and re-derive its recipe. False if its folder isn't there."""
    from ..analysis import rift
    from ..recommend import engine

    a = g.get("analysis") or {}
    gd = g.get("game_dir") or (a.get("extra") or {}).get("folder")
    if not (gd and Path(gd).is_dir()):
        return False
    try:
        an = rift.analyze(Path(gd), exe=g.get("exe") or (a.get("extra") or {}).get("exe"))
        g["analysis"] = asdict(an)
        g["recipe"] = recipe_to_dict(engine.suggest(an))
    except Exception:  # noqa: BLE001 - an unreadable game keeps its recipe
        pass
    return True


def save(data: dict) -> None:
    with _lock:
        write_atomic(_path(), json.dumps(data, indent=1, default=str))


def upsert_game(package: str, **fields) -> dict:
    with edit() as data:
        entry = data["games"].setdefault(package, {"package": package, "added": time.time()})
        entry.update(fields)
    return entry


def update_game(package: str, fn: Callable[[dict], None]) -> dict | None:
    """Change one game's entry in place, atomically (fn mutates the entry). None if the game isn't there."""
    with edit() as data:
        entry = data["games"].get(package)
        if entry is not None:
            fn(entry)
    return entry


def game(package: str) -> dict | None:
    return load()["games"].get(package)


def games() -> list[dict]:
    return sorted(load()["games"].values(), key=lambda g: (g.get("title") or g["package"]).lower())


def remove_game(package: str) -> None:
    with edit() as data:
        data["games"].pop(package, None)


def setting(key: str, default=None):
    return load().get("settings", {}).get(key, default)


def set_setting(key: str, value) -> None:
    with edit() as data:
        data.setdefault("settings", {})[key] = value


def update_setting(key: str, fn: Callable, default=None):
    """Set a setting from its current value, atomically: value = fn(current or default). Returns the new value."""
    with edit() as data:
        settings = data.setdefault("settings", {})
        settings[key] = value = fn(settings.get(key, default))
    return value


def recipe_to_dict(r: Recipe) -> dict:
    return asdict(r)


def recipe_from_dict(d: dict) -> Recipe:
    return Recipe(**{k: v for k, v in d.items() if k in Recipe.__dataclass_fields__})


def analysis_from_dict(d: dict) -> Analysis:
    """Build an Analysis, filling in defaults for any field a partial/legacy entry is missing (so old library data
    still works with newer code)."""
    import dataclasses

    kwargs = {}
    for f in dataclasses.fields(Analysis):
        if f.name in d:
            kwargs[f.name] = d[f.name]
        elif f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            continue  # let the dataclass default apply
        else:
            kwargs[f.name] = ("" if f.type == "str" else [] if "list" in str(f.type) else
                              0 if f.type == "int" else False if f.type == "bool" else None)
    return Analysis(**kwargs)
