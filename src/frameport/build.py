"""Build stage: OVRPort → Frame fixes (apk-stage patches) → sign → static validation."""
from __future__ import annotations

import dataclasses
import hashlib
import shutil
from pathlib import Path

from .apk import sign
from .apk.workspace import ApkWorkspace
from .core.events import Reporter
from .core.models import Analysis, BuildResult, Recipe, SourceGame
from .core.paths import work_dir
from .patches import base, upstream
from .patches.overport import OVERPORT_PATCHES
from .tools import overport as overport_tool
from .validate.static import check_apk


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def overport_ids(recipe: Recipe, alt: bool = False) -> list[str]:
    """OVRPort defaults (OVRPort's order) first, then extras in recipe order, then the alt-build extras."""
    chosen = [pid for pid in recipe.patches if base.get(pid).category == "overport"]
    order = [pid for pid, *_ in OVERPORT_PATCHES]
    defaults = [pid for pid in order if pid in chosen and base.get(pid).default_on]
    extras = [pid for pid in chosen if pid not in defaults]
    ids = defaults + extras
    if alt:
        ids += [pid for pid in recipe.alt_patches if pid not in ids]
    return ids


def apk_patches(selection: dict) -> list[base.Patch]:
    patches = [base.get(pid) for pid in selection if base.get(pid).stage == "apk"]
    return sorted(patches, key=lambda p: p.order)


def apply_frame_fixes(apk_in: Path, apk_out_unsigned: Path, analysis: Analysis, recipe: Recipe,
                      reporter: Reporter) -> tuple[list[str], list[dict], dict[str, str]]:
    """Apply the recipe's apk-stage patches; returns (applied ids, checks, workarounds left out because OVRPort's
    output already has the upstream fix (patches/upstream.py))."""
    applied, checks = [], []
    with ApkWorkspace(apk_in) as ws:
        selection = upstream.resolve(ws, recipe.patches, reporter)
        for patch in apk_patches(selection.patches):
            reporter.check_cancel()
            ctx = base.ApkContext(ws, analysis, selection.patches.get(patch.id) or {}, reporter, selection.patches)
            if patch.apply(ctx):
                applied.append(patch.id)
                reporter.log(f"applied {patch.id}" + (f" ({'; '.join(ctx.notes)})" if ctx.notes else ""))
            for name, ok, detail in patch.validate(ctx):
                checks.append({"name": name, "ok": ok, "detail": detail})
        ws.write(apk_out_unsigned)
    return applied, checks, selection.superseded


def build(source: SourceGame, analysis: Analysis, recipe: Recipe, outdir: Path, reporter: Reporter,
          keep_work: bool = False, pc: bool = False) -> BuildResult:
    """pc: the build for this PC (AXRB): only OVRPort's patches and the on_pc APK fixes (base.pc_selection)."""
    pkg = analysis.package
    if pc:
        recipe = dataclasses.replace(recipe, patches=base.pc_selection(recipe.patches))
    work = work_dir() / pkg
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    outdir.mkdir(parents=True, exist_ok=True)
    # an APK that is already an OVRPort/FramePort build (e.g. a copy from an earlier FramePort) isn't converted again
    # for the normal build: a second conversion replaces OVRPort's platform loader and drops what the first build
    # linked to it (Wallace & Gromit / Espire 2 then crashed with "cannot locate symbol ovr_..."). Only the Steam
    # Frame patches are applied on top. Its alternate build is the copy saved next to it, or (no copy) one more
    # OVRPort run for the alternate patches; the stand-ins patch relinks what that drops.
    converted = recipe.overport and analysis.is_overport_output
    if pc and converted:
        reporter.check("Original APK", None, "this copy was already converted for the Steam Frame and keeps its Frame "
                                             "fixes; add the game's original APK for a clean build for this PC")
    variants = [("primary", False)] + ([("alt", True)] if recipe.alt_patches and recipe.overport else [])
    alt_copy = Path(source.apk).with_name(f"{pkg}.alt-noforcequit.apk")
    results = {}
    all_checks, applied, superseded = [], [], {}
    try:
        for variant, alt in variants:
            input_apk = alt_copy if converted and alt and alt_copy.exists() else Path(source.apk)
            if recipe.overport and (not converted or alt and input_apk == Path(source.apk)):
                reporter.stage(f"OVRPort ({variant})")
                ids = overport_ids(recipe, alt)
                patched = overport_tool.patch(source.apk, work, f"{pkg}.{variant}.overport.apk", ids, reporter)
            else:  # an ordinary Android app, or an already converted build: only the Frame patches it needs
                if recipe.overport:
                    reporter.log(f"{input_apk.name} is already converted: applying only the Steam Frame patches")
                patched = work / f"{pkg}.{variant}.original.apk"
                shutil.copyfile(input_apk, patched)
            reporter.stage(f"Frame fixes ({variant})")
            unsigned = work / f"{pkg}.{variant}.unsigned.apk"
            applied, checks, left_out = apply_frame_fixes(patched, unsigned, analysis, recipe, reporter)
            if variant == "primary":
                superseded = left_out
            patched.unlink(missing_ok=True)
            reporter.stage(f"sign ({variant})")
            final = outdir / (f"{pkg}.apk" if not alt else f"{pkg}.alt-noforcequit.apk")
            sign.sign(unsigned, final, pkg)
            unsigned.unlink(missing_ok=True)
            reporter.stage(f"validate ({variant})")
            checks += check_apk(final, pkg, expect_adapter=recipe.overport and not pc)
            for c in checks:
                reporter.check(c["name"], c["ok"], c["detail"])
            results[variant] = final
            all_checks += [{**c, "variant": variant} for c in checks]
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
    primary, alt_apk = results["primary"], results.get("alt")
    return BuildResult(pkg, primary, alt_apk, sha256(primary), sha256(alt_apk) if alt_apk else None, applied,
                       all_checks, {"overport": overport_ids(recipe), "alt_overport": overport_ids(recipe, True)
                                    if alt_apk else None, "superseded": superseded})
