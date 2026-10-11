"""PC VR target: Oculus Rift games run on this Windows PC through Revive (LibOVR -> OpenXR/SteamVR), with a non-Steam
shortcut in the local Steam library (play on the Frame by streaming from Steam/SteamVR).

Works on native Windows and from WSL (Windows programs via interop). Revive is FramePort's portable copy
(tools/revive.py), so nothing is installed system-wide. Nothing is copied: the shortcut points at the game folder.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shutil
import time
from pathlib import Path

from ..core import winhost
from ..core.events import Reporter
from ..core.models import Recipe
from ..core.paths import agent_file, user_data_dir
from ..patches.pcvr import game_args
from ..validate.triage import triage
from .base import PC_LABEL, Target

TAG = "FramePort PC VR"  # marks the Steam shortcuts FramePort made (for updates and cleanup)
OLD_TAGS = ("Rift via Revive",)  # earlier name of the same tag
REVIVE_LOG = "Revive/ReviveInjector.txt"  # under %LOCALAPPDATA%


def _vdf():
    """The agent's binary-VDF/shortcut code (stdlib only), reused so there's a single implementation."""
    path = str(agent_file())  # an explicit loader: in release bundles the source has a .txt name (paths.agent_file)
    spec = importlib.util.spec_from_loader("frameport_agent_vdf",
                                           importlib.machinery.SourceFileLoader("frameport_agent_vdf", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def windows_revive(rdir: Path) -> Path:
    """Revive where Windows can run it: on WSL FramePort's data lives on the Linux filesystem (\\wsl$ paths are
    unreliable for process creation / DLL injection), so a copy goes to %LOCALAPPDATA%\\FramePort\\<revive-version>."""
    if not winhost.is_wsl() or str(rdir).startswith("/mnt/"):
        return rdir
    from ..tools import revive

    local = winhost.env_path("LOCALAPPDATA")
    if not local:
        return rdir
    dest = local / "FramePort" / rdir.name
    for f in revive.runtime_files(rdir):
        target = dest / f.relative_to(rdir)
        if not target.exists() or target.stat().st_size != f.stat().st_size:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
    return dest


def pc_dir() -> Path:
    path = user_data_dir() / "pc"
    path.mkdir(parents=True, exist_ok=True)
    return path


def shortcut_fields(dep: dict) -> tuple[str, str, str]:
    """(Exe, StartDir, LaunchOptions) for the Steam shortcut of an installed game."""
    exe_win = dep["exe_win"]
    start = '"' + exe_win.rsplit("\\", 1)[0] + '\\"'
    extra = " ".join(dep.get("game_args") or [])
    if dep.get("revive_win"):
        args = ("" if dep.get("backend") == "openvr" else "/openxr ") + f'"{exe_win}"' + (f" {extra}" if extra else "")
        return f'"{dep["revive_win"]}\\ReviveInjector.exe"', start, args
    return f'"{exe_win}"', start, extra


def local_installs() -> dict[str, dict]:
    """PC installs by package (just reads FramePort's records; works without Windows)."""
    out = {}
    for f in sorted((user_data_dir() / "pc").glob("*/deployment.json")):
        try:
            dep = json.loads(f.read_text(encoding="utf-8"))
            out[dep["package"]] = dep
        except (OSError, ValueError, KeyError):
            continue
    return out


class PcReviveTarget(Target):
    kind = "pc"
    label = PC_LABEL

    def __init__(self):
        if not winhost.available():
            raise RuntimeError("PC VR games need Windows (or FramePort running in WSL on Windows)")
        self.steam = winhost.steam_root()

    # ------------------------------------------------------------------ info
    def describe(self) -> dict:
        from ..tools import revive

        root = self.steam
        return {
            "platform": "wsl" if winhost.is_wsl() else "windows",
            "steam": str(root) if root else None,
            "steam_user": bool(root and winhost.steam_user(root)),
            "steam_running": winhost.process_running("steam.exe"),
            "steamvr": bool(root and winhost.app_installed(root, winhost.STEAMVR_APPID)),
            "revive": str(revive.revive_dir()) if revive.revive_dir() else None,
            "revive_version": revive.installed_version(),
            "installed": self.installed(),
        }

    def installed(self) -> list[dict]:
        out = []
        for f in sorted(pc_dir().glob("*/deployment.json")):
            try:
                dep = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            dep["apk_present"] = Path(dep.get("exe_local", "")).exists()
            out.append(dep)
        return out

    def _dep(self, package: str) -> dict:
        try:
            return json.loads((pc_dir() / package / "deployment.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RuntimeError(f"{package} is not installed on this PC") from None

    @staticmethod
    def _save_dep(package: str, dep: dict) -> None:
        path = pc_dir() / package / "deployment.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dep, indent=2), encoding="utf-8")
        tmp.replace(path)

    def rename(self, package: str, title: str, reporter: Reporter) -> dict:
        """A new name for an installed game's Steam shortcut on this PC (after Rename…): the shortcut is matched by
        its Exe, so it keeps its appid (Play, grid art); Steam restarts once if it runs."""
        dep = self._dep(package)
        if dep.get("title") == title:
            reporter.check("Steam library (PC)", True, f"already named {title}")
            return {"state": "done", "added": [], "errors": [], "unchanged": True}
        dep["title"] = title
        self._save_dep(package, dep)
        return self.add_to_library([package], reporter)

    def collect_diag(self, package=None):
        files = {}
        log_path = (winhost.env_path("LOCALAPPDATA") or Path("/nonexistent")) / REVIVE_LOG
        if log_path.exists():
            files["ReviveInjector.txt"] = log_path.read_text(encoding="utf-8", errors="replace")[-(2 << 20):]
        out = {"host": {"steamvr_running": winhost.steamvr_running()}, "files": files}
        if package:
            try:
                files["deployment.json"] = json.dumps(self._dep(package), indent=1)
                out["installed"] = True
            except RuntimeError:
                out["installed"] = False
        return out

    # ------------------------------------------------------------------ install
    def install(self, package, title, apk, data_dir, recipe, reporter, apk_only=False, data_files=None):
        raise NotImplementedError("Quest (APK) games install on the Steam Frame, not on the PC")

    def install_pcvr(self, package, title, game_dir, exe, recipe: Recipe, reporter: Reporter, **extra):
        from ..tools import revive

        reporter.stage("Prepare this PC")
        if not self.steam:
            raise RuntimeError("Steam for Windows was not found on this PC")
        reporter.check("Steam", True, str(self.steam))
        vr = winhost.app_installed(self.steam, winhost.STEAMVR_APPID)
        reporter.check("SteamVR", vr or None, "installed" if vr else "not installed: install SteamVR from Steam")
        use_revive = "pcvr.revive" in recipe.patches
        rdir = None
        if use_revive:
            rdir = revive.revive_dir() or revive.install(lambda f: reporter.progress(f, "downloading Revive"))
            rdir = windows_revive(rdir)
            reporter.check("Revive", True, f"{revive.installed_version()} at {winhost.to_windows(rdir)}")
        game_dir = Path(game_dir)
        exe_local = game_dir / exe
        if not exe_local.is_file():
            raise RuntimeError(f"game executable missing: {exe_local}")
        dep = {"package": package, "kind": "pcvr", "title": title, "game_dir": str(game_dir),
               "exe_local": str(exe_local), "exe_win": winhost.to_windows(exe_local),
               "revive_win": winhost.to_windows(rdir) if rdir else None,
               "revive_version": revive.installed_version() if rdir else None,
               "backend": ("openvr" if "pcvr.revive_openvr" in recipe.patches else "openxr") if rdir else None,
               "launch": "revive" if rdir else "repack" if "pcvr.repack_launcher" in recipe.patches else "direct",
               "game_args": game_args(recipe),
               "tuning": dict(recipe.params("pcvr.steamvr_tuning"), enabled=True)
               if "pcvr.steamvr_tuning" in recipe.patches else None,
               "sha256": extra.get("exe_sha256"), "art_lookup": extra.get("art_lookup"),
               "recipe": {"patches": sorted(recipe.patches), "source": recipe.source}, "time": time.time()}
        exe_field, _, _ = shortcut_fields(dep)
        d = pc_dir() / package
        try:  # a reinstall keeps the shortcut's appid: Steam's entry is matched by its Exe, whatever its name now
            old = json.loads((d / "deployment.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        same_exe = bool(old.get("appid") and old.get("exe_win") and shortcut_fields(old)[0] == exe_field)
        dep["appid"] = old["appid"] if same_exe else _vdf().shortcut_appid(exe_field, title)
        d.mkdir(parents=True, exist_ok=True)
        (d / "deployment.json").write_text(json.dumps(dep, indent=2), encoding="utf-8")
        reporter.log(f"ready to run from {dep['exe_win']}" + (" through Revive" if rdir else ""))
        return {"ok": True, "appid": dep["appid"], "exe": dep["exe_win"]}

    def add_to_library(self, packages, reporter):
        from ..artwork import fetch as artwork
        from ..artwork import sources
        from ..artwork.steam import steam_set_for, steam_tags
        from ..core import library

        reporter.stage("Add to Steam library (this PC)")
        root = self.steam
        user = winhost.steam_user(root) if root else None
        if not user:
            raise RuntimeError("could not tell which Steam user to add the games for (log in to Steam once)")
        vdf_mod = _vdf()
        vdf = root / "userdata" / user / "config" / "shortcuts.vdf"
        was_running = winhost.stop_steam(root)
        added, errors = [], []
        try:
            grid = vdf.parent / "grid"
            grid.mkdir(parents=True, exist_ok=True)
            for pkg in packages:
                try:
                    dep = self._dep(pkg)
                    exe, start, opts = shortcut_fields(dep)
                    entry = library.game(pkg) or {"kind": "rift"}
                    if not sources.has_art(pkg):
                        artwork.fetch(pkg, lookup=dep.get("art_lookup"))
                    art = steam_set_for(pkg)
                    for stale in vdf_mod.prune_shortcuts(str(vdf), dep["title"], exe, (TAG, *OLD_TAGS)):
                        from ..uninstall import grid_files

                        for old in grid_files(grid, stale):
                            old.unlink(missing_ok=True)
                        reporter.log(f"removed the old Steam entry for {dep['title']} (its launch command changed)")
                    ident = dep.get("appid") or vdf_mod.shortcut_appid(exe, dep["title"])
                    icon = dep["exe_win"]
                    if "icon" in art:  # Steam wants a local path for the shortcut icon
                        icon_path = grid / f"{ident}_icon.png"
                        shutil.copy(art["icon"], icon_path)
                        icon = winhost.to_windows(icon_path)
                    got = vdf_mod.upsert_shortcut(str(vdf), exe, dep["title"], start, icon, TAG, opts,
                                                  tags=steam_tags(entry, "pc"))
                    for kind, f in art.items():
                        suffix = {"portrait": "p", "landscape": "", "hero": "_hero", "logo": "_logo"}.get(kind)
                        if suffix is None:
                            continue
                        for old in grid.glob(f"{got}{suffix}.*"):
                            old.unlink()
                        shutil.copy(f, grid / f"{got}{suffix}{f.suffix}")
                    if got != dep.get("appid"):  # Play starts the shortcut by this id (a renamed game keeps its own)
                        dep["appid"] = got
                        self._save_dep(pkg, dep)
                    added.append({"package": pkg, "appid": got})
                    reporter.check(f"Steam library (PC): {dep['title']}", True, f"shortcut {got}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{pkg}: {exc}")
                    reporter.check("Steam library (PC)", False, f"{pkg}: {exc}")
        finally:
            if was_running:
                winhost.start_steam(root)
        return {"state": "done" if not errors else "failed", "added": added, "errors": errors}

    # ------------------------------------------------------------------ test / manage
    def launch(self, package):
        """Through Windows Steam's shortcut, like clicking Play in Steam. Starts SteamVR first (Revive needs it, or the
        game runs as a flat window)."""
        dep = self._dep(package)
        root = winhost.steam_root()
        if not root or not dep.get("appid"):
            raise RuntimeError("Steam or the game's Steam shortcut wasn't found on this PC")
        vr = winhost.start_steamvr(root)  # Revive binds to SteamVR; without it the game falls back to flatscreen
        tuning = self._tune(root, dep) if vr else None
        winhost.start_detached(root / "steam.exe", [f"steam://rungameid/{(int(dep['appid']) << 32) | 0x02000000}"])
        return {"package": package, "title": dep.get("title"), "steamvr": vr, "tuning": tuning}

    def _tune(self, root, dep) -> dict | None:
        """pcvr.steamvr_tuning: SteamVR per-app refresh rate / motion smoothing from the last session's frame drops."""
        if "tuning" in dep and dep["tuning"] is None:  # switched off in the recipe (installs before it: on)
            return None
        from ..core import applog, steamvr_perf

        try:
            result = steamvr_perf.tune(root, f"steam.app.{dep['appid']}", Path(dep["exe_local"]).name,
                                       dep.get("tuning") or {})
        except Exception as e:  # noqa: BLE001 - tuning must never block Play
            result = {"error": str(e)}
        applog.log.info("steamvr tuning %s: %s", dep.get("package"), result)
        return result

    def launch_test(self, package, reporter, seconds=45):
        reporter.stage("Launch test (this PC)")
        dep = self._dep(package)
        image = Path(dep["exe_local"]).name
        if winhost.process_running(image):
            raise RuntimeError(f"{dep['title']} is already running")
        root = winhost.steam_root()
        if root:  # a direct start bypasses Steam, so bring SteamVR up ourselves (Revive / the game bind to it)
            reporter.stage("Starting SteamVR")
            if not winhost.start_steamvr(root):
                reporter.log("SteamVR didn't start; the game may run as a flat window (no VR).")
        exe, _, opts = shortcut_fields(dep)
        exe_path = winhost.to_local(exe.strip('"'))
        import shlex

        args = shlex.split(opts, posix=True) if opts else []
        log_path = (winhost.env_path("LOCALAPPDATA") or Path("/nonexistent")) / REVIVE_LOG
        before = log_path.stat().st_mtime if log_path.exists() else 0
        winhost.start_detached(exe_path, args, cwd=Path(dep["exe_local"]).parent)
        start, seen, state = time.time(), False, "RUNNING"
        while time.time() - start < seconds:
            time.sleep(3)
            running = winhost.process_running(image)
            seen = seen or running
            if seen and not running:
                state = "EXITED"
                break
        if not seen:
            state = "NEVER_STARTED"
        winhost.kill(image)
        log = ""
        if log_path.exists() and log_path.stat().st_mtime >= before:
            log = "===== ReviveInjector.txt\n" + log_path.read_text(encoding="utf-8", errors="replace")
        result = triage(log, state, package)
        reporter.check("Process", state == "RUNNING", f"{state} after {round(time.time() - start)}s")
        for m in result.milestones:
            reporter.check(m, True)
        for f in result.findings:
            reporter.check(f.id, None if f.severity in ("warning", "info") else False,
                           f"{f.diagnosis} [{f.evidence[:160]}]")
        return result, log

    def set_settings(self, package, settings):
        return {"settings": {}}

    def uninstall(self, package, keep_data=True):
        dep = self._dep(package)
        root = self.steam
        user = winhost.steam_user(root) if root else None
        removed = False
        if user:
            vdf = root / "userdata" / user / "config" / "shortcuts.vdf"
            was_running = winhost.stop_steam(root)
            try:
                removed = _vdf().remove_shortcut(str(vdf), shortcut_fields(dep)[0])
            finally:
                if was_running:
                    winhost.start_steam(root)
        shutil.rmtree(pc_dir() / package, ignore_errors=True)
        return {"removed": True, "shortcut_removed": removed, "kept_saves": True}
