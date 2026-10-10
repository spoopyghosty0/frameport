"""Diagnostics bundle: one redacted zip with everything needed to debug a game later, without the game files, the
Frame or this app (see docs/DIAGNOSTICS.md for the layout). Users attach it to a GitHub issue."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import platform
import re
import sys
import time
import zipfile
from pathlib import Path

from ..core import applog, library
from ..core.events import Reporter
from ..core.paths import user_data_dir
from . import redact

SCHEMA = 1
MAX_LOG = 4 << 20  # per log file (tail kept)
MAX_TOTAL = 24 << 20  # GitHub's attachment limit is 25 MB
ENTRY_DROP = ("details", "art_source", "artwork", "art")  # store metadata: public, big, no debug value
ELF_SYMBOL_PREFIXES = ("ovr", "xr", "vrapi_", "OVR", "Java_", "JNI_", "eglGetProcAddress", "gl")

README = ("""# FramePort diagnostics bundle

Created by FramePort {version} on {created}. Personal data was replaced by placeholders such as `<ip>`, `<home>`,
`<user>`, `<data>` (FramePort's data folder), `<frame>` and `<steam-id>` (counts in manifest.json → redactions).

| Path | What |
|---|---|
| manifest.json | app / agent / tool versions, OS, which games are included, warnings |
| app/app.log, app/jobs/*.log | the app's log and recent GUI job logs (build, install, launch test, ...) |
| app/settings.json | library settings and catalog sources |
| frame/info.json, frame/host.json | the Frame: SteamOS build, Lepton/Proton, OpenXR runtime + layers, podman, kernel"""
""" keys |
| games/<pkg>/entry.json | the library entry: analysis, recipe, build checks, installs, last launch test |
| games/<pkg>/recipe.yaml | the recipe in catalog form (catalog/games/<pkg>.yaml) |
| games/<pkg>/triage.json | the newest launch log re-triaged with the triage signatures of this app version |
| games/<pkg>/package/ | stand-in for the game files: file list, AndroidManifest.xml, ELF imports/exports / PE imports |
| games/<pkg>/logs/ | launch-test logs saved on the PC |
| games/<pkg>/target/ | from the Frame (or PC): launch.sh, settings.conf, deployment.json, launch/logcat/Proton/game"""
""" logs, file list |
| games/<pkg>/target/shaders/<dir>/ | shader dumps (vk_shader_dump: fp_vk_shaders, zink_shader_dump: fp_spirv): the"""
""" SPIR-V modules of the newest run, else the newest written (binary, not redacted: compiled game shaders) and"""
""" index.txt (creation order and time) |

## Debugging from this bundle
1. `frameport diag inspect <this zip>` re-runs triage with the current signatures (catalog/triage.yaml).
2. Look up symptoms in docs/PLAYBOOK.md; runtime facts are in docs/FRAME_RUNTIME.md.
3. Compare `recipe.yaml` with `catalog/games/` recipes of games using the same engine / XR API (entry.json → analysis).
4. Headless launch tests can't show the picture: "RUNNING" only proves startup (see FRAME_RUNTIME.md).
""")


def _tail(text: str, limit: int = MAX_LOG) -> str:
    if len(text) <= limit:
        return text
    return f"[... first {len(text) - limit} characters cut ...]\n" + text[-limit:]


def _read_tail(path: Path, limit: int = MAX_LOG) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > limit:
                f.seek(size - limit)
            data = f.read().decode("utf-8", "replace")
        return (f"[... first {size - limit} bytes cut ...]\n" if size > limit else "") + data
    except OSError as exc:
        return f"(unreadable: {exc})"


def _version() -> str:
    from .. import __version__

    return __version__


class _Writer:
    """Collects redacted files; enforces the total size cap (drops the biggest logs first)."""

    def __init__(self, red: redact.Redactor):
        self.red = red
        self.files: dict[str, bytes] = {}
        self.warnings: list[str] = []

    def text(self, name: str, text: str, limit: int = MAX_LOG) -> None:
        self.files[name] = self.red.text(_tail(text or "", limit)).encode("utf-8", "replace")

    def json(self, name: str, obj) -> None:
        self.files[name] = self.red.json(obj).encode("utf-8")

    def fit(self, limit: int = MAX_TOTAL) -> None:
        def zipped():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for n, d in self.files.items():
                    z.writestr(n, d)
            return buf.tell()
        size = zipped()
        while size > limit:
            logs = sorted((n for n in self.files if n.endswith((".log", ".txt")) and len(self.files[n]) > 64 << 10),
                          key=lambda n: -len(self.files[n]))
            if not logs:
                # then shader modules (agent >= 74 sends up to 30 MB): the least recently used first (the agent
                # lists them last used first)
                spv = [n for n in self.files if n.endswith(".spv")]
                if not spv:
                    break
                drop = spv[-max(1, len(spv) // 8):]
                for n in drop:
                    del self.files[n]
                self.warnings.append(f"{len(drop)} shader module(s) left out to fit the size limit")
                size = zipped()
                continue
            n = logs[0]
            tail = self.files[n][-(len(self.files[n]) // 4):]
            self.files[n] = b"[... cut to fit the 25 MB attachment limit ...]\n" + tail
            self.warnings.append(f"{n} was cut to fit the size limit")
            size = zipped()


# ------------------------------------------------------------------------------------------ pieces
def add_shader_dumps(w: _Writer, prefix: str, shaders: dict) -> None:
    """The agent's shader dumps (collect_diag "shaders", agent >= 72): SPIR-V modules as they are (compiled game
    shaders, nothing personal), the index as redacted text."""
    for folder, res in shaders.items():
        if not isinstance(res, dict) or folder not in ("fp_vk_shaders", "fp_spirv"):
            continue
        base = f"{prefix}{folder}/"
        if res.get("index"):
            w.text(base + "index.txt", res["index"])
        modules = res.get("modules") or {}
        for name, b64 in modules.items():
            if not re.fullmatch(r"\d+_[0-9a-f]{64}\.spv", name):
                continue
            try:
                w.files[base + name] = base64.b64decode(b64, validate=True)
            except (ValueError, TypeError):
                w.warnings.append(f"{base}{name}: not base64")
        if "session" in res:  # agent >= 74: the newest session's modules (index.txt), not the newest written
            left = int(res.get("session") or 0) - len(modules)
            if left > 0:
                w.warnings.append(f"{base}: {left} module(s) of the newest session stayed on the Frame (size limit)")
            continue
        left = int(res.get("total") or 0) - len(modules)
        if left > 0:
            w.warnings.append(f"{base}: {left} older module(s) stayed on the Frame (size limit)")


def env_info(target_info: dict | None = None) -> dict:
    """Versions and platform facts (manifest.json and the "environment" of GitHub issues)."""
    from ..frame.connection import bundled_agent_version

    out = {"app": _version(), "agent_bundled": bundled_agent_version(), "python": sys.version.split()[0],
           "os": f"{platform.system()} {platform.release()}", "machine": platform.machine()}
    try:
        from ..core import winhost

        out["wsl"] = winhost.is_wsl()
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..tools import toolchain

        out["tools"] = {s.name: s.version for s in toolchain.status(False) if s.installed}
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..tools import revive

        out["revive"] = revive.installed_version()
    except Exception:  # noqa: BLE001
        pass
    if target_info:
        out["frame"] = {k: target_info.get(k) for k in ("os", "os_version", "build_id", "agent_version", "arch")}
        proton = target_info.get("proton") or {}
        if isinstance(proton, dict):
            ready = proton.get("ready")
            out["frame"]["proton"] = (ready.get("display_name") or ready.get("name")
                                      if isinstance(ready, dict) else ready)
    return out


def package_listing(g: dict) -> dict[str, object]:
    """A stand-in for the game files: what's in the APK (or Rift folder) and what its binaries link against."""
    out: dict[str, object] = {}
    if g.get("kind") == "rift":
        root = Path(g.get("game_dir") or "")
        if root.is_dir():
            files = []
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    files.append([p.relative_to(root).as_posix(), p.stat().st_size])
                    if len(files) >= 20000:
                        break
            out["files.json"] = files
            from ..analysis import rift

            pe = {}
            exes = [g.get("exe")] + [p for p, _ in files if p.lower().endswith((".exe", ".dll"))
                                     and any(k in p.lower() for k in ("ovr", "openxr", "openvr", "revive", "oculus"))]
            for rel in [e for e in exes if e][:40]:
                try:
                    info, _ = rift.read_pe(root / rel, limit=64 << 20)
                    pe[rel] = {"machine": info.machine, "subsystem": info.subsystem, "imports": info.imports,
                               "delay_imports": info.delay_imports}
                except Exception as exc:  # noqa: BLE001
                    pe[rel] = {"error": str(exc)}
            out["pe.json"] = pe
        return out
    if g.get("kind") == "linux":  # the app's file list (a lone AppImage: only it, not the folder it's in)
        root, extra = Path(g.get("game_dir") or ""), (g.get("analysis") or {}).get("extra") or {}
        paths = [root / f for f in extra["files"]] if extra.get("files") else \
            sorted(root.rglob("*")) if root.is_dir() else []
        files = []
        for p in paths:
            if p.is_file():
                files.append([p.relative_to(root).as_posix(), p.stat().st_size])
                if len(files) >= 20000:
                    break
        out["files.json"] = files
        return out
    apk = Path(g.get("apk") or "")
    if not apk.is_file():
        return out
    with zipfile.ZipFile(apk) as z:
        infos = z.infolist()
        out["apk_files.json"] = [[i.filename, i.file_size] for i in infos[:20000]]
        try:
            from pyaxmlparser.axmlprinter import AXMLPrinter

            out["AndroidManifest.xml"] = AXMLPrinter(z.read("AndroidManifest.xml")).get_xml().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            out["AndroidManifest.xml"] = f"(could not decode: {exc})"
        elf = {}
        for i in infos:
            if not (i.filename.startswith("lib/") and i.filename.endswith(".so")):
                continue
            elf[i.filename] = _elf_summary(z, i)
        out["elf.json"] = elf
    return out


def _elf_summary(z: zipfile.ZipFile, info: zipfile.ZipInfo) -> dict:
    import shutil
    import tempfile

    from elftools.elf.dynamic import DynamicSection
    from elftools.elf.elffile import ELFFile
    from elftools.elf.sections import SymbolTableSection

    try:
        with contextlib.ExitStack() as stack:
            f = stack.enter_context(z.open(info))
            if info.compress_type != zipfile.ZIP_STORED:
                # seeking in a deflated member re-decompresses from the start: unpack once (libs can be > 1 GB)
                tmp = stack.enter_context(tempfile.TemporaryFile())
                shutil.copyfileobj(f, tmp, 1 << 20)
                tmp.seek(0)
                f = tmp
            elf = ELFFile(f)
            res: dict = {"size": info.file_size, "needed": [], "exports": [], "imports": []}
            for sec in elf.iter_sections():
                if isinstance(sec, DynamicSection):
                    res["needed"] = [t.needed for t in sec.iter_tags() if t.entry.d_tag == "DT_NEEDED"]
                elif isinstance(sec, SymbolTableSection) and sec.name == ".dynsym":
                    for sym in sec.iter_symbols():
                        name = sym.name.split("@")[0]
                        if not name.startswith(ELF_SYMBOL_PREFIXES):
                            continue
                        (res["imports"] if sym["st_shndx"] == "SHN_UNDEF" else res["exports"]).append(name)
            for k in ("exports", "imports"):
                res[k] = sorted(set(res[k]))[:3000]
            return res
    except Exception as exc:  # noqa: BLE001
        return {"size": info.file_size, "error": str(exc)[:300]}


def retriage(package: str, log: str, state: str = "UNKNOWN") -> dict:
    from ..validate.triage import triage

    r = triage(log, state, package)
    return {"state": r.state, "verdict": r.verdict, "milestone": r.milestone, "milestones": r.milestones, "fps": r.fps,
            "findings": [f.__dict__ for f in r.findings], "suggestions": r.suggestions()}


SOURCE_KEYS = ("apk", "origin", "data_dir", "game_dir")  # where the user keeps game dumps: not shared, only names


def source_dirs(g: dict) -> list[str]:
    """Folders of the user's game dumps named by a library entry (redacted as <source>)."""
    out = []
    for k in SOURCE_KEYS:
        v = g.get(k)
        if isinstance(v, str) and v:
            p = Path(v)
            out.append(str(p.parent if k in ("apk",) or p.suffix else p))
    ex = (g.get("analysis") or {}).get("extra") or {}
    if isinstance(ex.get("folder"), str):
        out.append(ex["folder"])
    return [d for d in out if d not in ("", ".", "/")]


def _game(w: _Writer, g: dict, target, reporter: Reporter) -> dict:
    from ..recommend import catalog

    pkg = g["package"]
    d = f"games/{pkg}/"
    entry = {k: v for k, v in g.items() if k not in ENTRY_DROP}
    w.json(d + "entry.json", entry)
    try:
        w.text(d + "recipe.yaml", catalog.to_yaml(catalog.entry_from_library(g, status=g.get("status") or "unknown")))
    except Exception as exc:  # noqa: BLE001
        w.warnings.append(f"{pkg}: recipe: {exc}")
    cat = catalog.lookup(pkg)
    if cat:
        w.text(d + f"catalog-{cat.origin}.yaml", catalog.to_yaml(cat))
    logs = sorted((user_data_dir() / "logs").glob(f"{pkg}-*.log"))
    for p in logs:
        w.text(d + "logs/" + p.name, _read_tail(p))
    if logs:
        last = g.get("last_test") or {}
        w.json(d + "triage.json", retriage(pkg, _read_tail(logs[-1]), last.get("state") or "UNKNOWN"))
    reporter.log(f"{pkg}: game file listing")
    try:
        for name, data in package_listing(g).items():
            if isinstance(data, str):
                w.text(d + "package/" + name, data)
            else:
                w.json(d + "package/" + name, data)
    except Exception as exc:  # noqa: BLE001
        w.warnings.append(f"{pkg}: package listing: {exc}")
    remote = None
    if target is not None:
        reporter.log(f"{pkg}: logs from {target.label}")
        try:
            remote = target.collect_diag(pkg)
        except Exception as exc:  # noqa: BLE001
            w.warnings.append(f"{pkg}: {target.label}: {exc}")
    if remote:
        for name, text in (remote.get("files") or {}).items():
            w.text(d + "target/" + name.replace("/", "_"), text or "")
        if remote.get("listing"):
            w.json(d + "target/files.json", remote["listing"])
        add_shader_dumps(w, d + "target/shaders/", remote.get("shaders") or {})
        if not remote.get("installed", True):
            w.warnings.append(f"{pkg}: not installed on {target.label}")
    return {"package": pkg, "title": g.get("title"), "kind": g.get("kind", "quest"), "status": g.get("status"),
            "from_target": bool(remote and remote.get("installed"))}


def collect(packages: list[str] | None, target=None, reporter: Reporter | None = None,
            dest: Path | None = None, extra: dict[str, str] | None = None) -> Path:
    """Write the bundle; packages None/[] = app-wide only. target: a connected Target (None = PC-side data only).
    extra: more text files to include (e.g. {"report.md": ...})."""
    reporter = reporter or Reporter()
    reporter.stage("Collecting diagnostics")
    info = None
    if target is not None:
        try:
            info = target.describe()
        except Exception as exc:  # noqa: BLE001
            reporter.log(f"{target.label} not reachable: {exc}")
            target = None
    red = redact.default(info)
    for pkg in packages or []:
        for d in source_dirs(library.game(pkg) or {}):
            red.add("source", d)
    w = _Writer(red)
    # app-wide
    for p in sorted(applog.logs_dir().glob("app.log*"))[:2]:
        w.text("app/" + p.name, _read_tail(p, 2 << 20))
    jobs = sorted(applog.jobs_dir().glob("*.log"))
    wanted = [p for p in jobs if not packages or any(pkg in p.name for pkg in packages)][-15:]
    for p in sorted(set(wanted + jobs[-5:])):
        w.text("app/jobs/" + p.name, _read_tail(p, 1 << 20))
    try:
        from ..recommend import catalog

        entries = catalog.load()
        origins: dict[str, int] = {}
        for e in entries.values():
            origins[e.origin] = origins.get(e.origin, 0) + 1
        w.json("app/settings.json", {"settings": library.load().get("settings", {}), "catalog": origins,
                                     "games": len(library.games())})
    except Exception as exc:  # noqa: BLE001
        w.warnings.append(f"settings: {exc}")
    if info:
        w.json("frame/info.json", info)
    if target is not None:
        try:
            host = target.collect_diag(None)
            if host:
                w.json("frame/host.json", host.get("host") or host)
                for name, text in (host.get("files") or {}).items():
                    w.text("frame/" + name, text or "")
        except Exception as exc:  # noqa: BLE001
            w.warnings.append(f"{target.label}: host info: {exc}")
    games = []
    for pkg in packages or []:
        reporter.check_cancel()
        g = library.game(pkg)
        if not g:
            w.warnings.append(f"{pkg}: not in the library")
            continue
        games.append(_game(w, g, target, reporter))
    for name, text in (extra or {}).items():
        w.text(name, text)
    w.fit()
    created = time.strftime("%Y-%m-%d %H:%M:%S")
    manifest = {"schema": SCHEMA, "created": created, "env": env_info(info),
                "target": target.kind if target is not None else None, "games": games,
                "warnings": w.warnings}
    w.json("manifest.json", manifest)
    manifest["redactions"] = red.summary()  # after the last redaction
    w.json("manifest.json", manifest)
    w.files["README.md"] = README.format(version=_version(), created=created).encode()
    if dest is None:
        from ..uninstall import default_backup_dir

        dest = default_backup_dir()
    dest = Path(dest)
    if dest.suffix.lower() != ".zip":
        dest.mkdir(parents=True, exist_ok=True)
        tag = packages[0] if packages and len(packages) == 1 else ("games" if packages else "app")
        dest = dest / f"FramePort-diag-{tag}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        rest = sorted(n for n in w.files if n not in ("README.md", "manifest.json"))
        for name in ["README.md", "manifest.json"] + rest:
            z.writestr(name, w.files[name])
    reporter.check("Diagnostics bundle", True, f"{dest.name} ({dest.stat().st_size / 2**20:.1f} MB)")
    applog.log.info("diagnostics bundle %s", dest)
    return dest


def read(zip_path: Path) -> dict:
    """manifest + per-game summary of a bundle (for `frameport diag inspect`)."""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        manifest = json.loads(z.read("manifest.json"))
        out = {"manifest": manifest, "games": {}}
        for g in manifest.get("games", []):
            pkg = g["package"]
            d = f"games/{pkg}/"
            logs = sorted(n for n in names if n.startswith(d + "logs/"))
            target_logs = [n for n in names if n.startswith(d + "target/")
                           and n.rsplit("/", 1)[1] in ("launch.log", "launch-test.log")]
            entry = json.loads(z.read(d + "entry.json")) if d + "entry.json" in names else {}
            last = entry.get("last_test") or {}
            log_name = (logs or target_logs or [None])[-1]
            res = {"title": g.get("title"), "status": g.get("status"), "last_test": last, "log": log_name,
                   "files": [n[len(d):] for n in names if n.startswith(d)]}
            if log_name:
                res["triage"] = retriage(pkg, z.read(log_name).decode("utf-8", "replace"),
                                         last.get("state") or "UNKNOWN")
            out["games"][pkg] = res
        return out
