"""Turn an Analysis into a suggested Recipe: catalog recipe when known, patch heuristics otherwise.

Every enabled patch carries a human-readable reason (shown next to its toggle in the UI).
"""
from __future__ import annotations

from ..core.models import Analysis, Recipe
from ..patches import base
from . import catalog
from .catalog import TOGGLED_DEVICE

# what a flat (non-VR) Windows game keeps of the PC VR patches
FLAT_WINDOWS_PATCHES = ("pcvr.proton_tool", "pcvr.proton_env", "pcvr.proton_log", "pcvr.no_crash_reporter")


def suggest(analysis: Analysis, use_catalog: bool = True) -> Recipe:
    entry = catalog.lookup(analysis.package) if use_catalog else None
    recipe = Recipe(analysis.package, title=analysis.label, catalog_rev=entry.rev() if entry else "")
    # 1. heuristics / defaults from every patch module
    rift = (analysis.extra or {}).get("kind") == "rift"
    for patch in base.all_patches():
        if not base.for_game(patch, analysis):
            continue  # Quest patches for Quest games, PC VR (Revive) options for Rift games
        if not patch.default_on and not patch.applies(analysis):
            continue  # irrelevant for this game (engine, XR API, graphics API, ...)
        s = patch.detect(analysis)
        if s and s.recommended:
            recipe.patches[patch.id] = dict(s.params)
            recipe.reasons[patch.id] = s.reason
    # 2. known-good catalog recipe overrides heuristics
    if rift:
        _rift_recipe(analysis, recipe, entry)
        if (analysis.extra or {}).get("flat"):  # a Windows game without VR: Proton only, no VR parts
            for pid in [p for p in recipe.patches if p not in FLAT_WINDOWS_PATCHES]:
                recipe.patches.pop(pid)
                recipe.reasons.pop(pid, None)
    elif entry:
        recipe.source = f"catalog ({entry.origin})"
        recipe.status, recipe.notes, recipe.title = entry.status, entry.notes, entry.title or recipe.title
        why = f"Known-good recipe for {entry.title} (tested {entry.verified.get('date', '?')})."
        for pid in entry.overport_remove:
            recipe.patches.pop(pid, None)
        for pid in entry.overport_extra + entry.frame + entry.device:
            if getattr(base.get(pid), "strict", False) and not base.get(pid).applies(analysis):
                continue  # e.g. a patch for one exact game build: another build of the game would fail to patch
            recipe.patches.setdefault(pid, {})
            recipe.reasons[pid] = why
        # heuristic-only suggestions the catalog didn't choose are dropped for exact reproducibility
        chosen = set(entry.overport_extra) | set(entry.frame) | set(entry.device)
        for pid in list(recipe.patches):
            p = base.get(pid)
            if pid not in chosen and not p.default_on and (p.category in ("overport", "frame")
                                                           or pid in TOGGLED_DEVICE):
                recipe.patches.pop(pid)
                recipe.reasons.pop(pid, None)
        for pid in entry.frame_remove:
            recipe.patches.pop(pid, None)
        for key, value in entry.adapter.items():
            recipe.patches[f"adapter.{key}"] = {"value": value}
            recipe.reasons[f"adapter.{key}"] = why
        if entry.device_files:
            recipe.patches["device.files"] = {"files": dict(entry.device_files)}
            recipe.reasons["device.files"] = why
        if entry.lepton_env:
            recipe.patches["device.lepton_env"] = {"env": dict(entry.lepton_env)}
        if entry.foveation:
            recipe.patches["device.foveation"] = {"mode": entry.foveation}
            recipe.reasons["device.foveation"] = why
        recipe.alt_patches = list(entry.alt_overport)
        recipe.use_alt = entry.use_alt
    else:
        recipe.source = "heuristics"
        if analysis.engine == "Unreal":
            recipe.alt_patches = ["patch_remove_unreal_force_quit"]  # build a fallback in case it quits itself
        if analysis.only_32bit:
            recipe.status = "unsupported"
            recipe.notes = ("32-bit only: the Steam Frame has no AArch32 support. Consider the PC (Rift) version "
                            "via Revive.")
        elif analysis.no_arm64:
            recipe.status = "unsupported"
            recipe.notes = f"No 64-bit ARM code ({', '.join(analysis.abis)}): the Steam Frame can't run it."
        if not rift:
            _static_blockers(analysis, recipe)
            _non_quest(analysis, recipe)
    if not rift and not entry and (analysis.extra or {}).get("frame_patched"):
        # already has FramePort's adapter (e.g. a PATCHED/ build): installing it unchanged is the safe default
        recipe.as_is = True
        recipe.notes = (recipe.notes + " " if recipe.notes else "") + \
            "This APK is already patched for the Frame, so FramePort installs it as it is."
    # requirements
    for pid in list(recipe.patches):
        for req in base.get(pid).requires:
            if req not in recipe.patches:
                recipe.patches[req] = {}
                recipe.reasons[req] = f"Needed by '{base.get(pid).title}'."
    return recipe


def _rift_recipe(analysis: Analysis, recipe: Recipe, entry) -> None:
    extra = analysis.extra
    if entry:
        recipe.source = f"catalog ({entry.origin})"
        recipe.status, recipe.notes, recipe.title = entry.status, entry.notes, entry.title or recipe.title
        why = f"Known-good recipe for {entry.title} (tested {entry.verified.get('date', '?')})."
        for pid in entry.pcvr:
            recipe.patches.setdefault(pid, {})
            recipe.reasons[pid] = why
        for pid in entry.pcvr_remove:
            recipe.patches.pop(pid, None)
        for pid in ("pcvr.repack_launcher", "pcvr.launch_args"):  # a verified recipe decides how the game starts
            if pid not in entry.pcvr:
                recipe.patches.pop(pid, None)
                recipe.reasons.pop(pid, None)
        if entry.proton_env:
            recipe.patches["pcvr.proton_env"] = {"env": "\n".join(f"{k}={v}" for k, v in entry.proton_env.items())}
            recipe.reasons["pcvr.proton_env"] = why
        if entry.proton_tool:
            recipe.patches["pcvr.proton_tool"] = {"tool": entry.proton_tool}
            recipe.reasons["pcvr.proton_tool"] = why
        recipe.as_is = entry.as_is
        # drop patches whose requirement the catalog removed (e.g. revive_openvr once revive is gone), so the
        # requirement pass in suggest() doesn't re-add it
        for pid in list(recipe.patches):
            if any(req not in recipe.patches for req in base.get(pid).requires):
                recipe.patches.pop(pid, None)
                recipe.reasons.pop(pid, None)
        return
    recipe.source = "heuristics"
    recipe.as_is = True  # the dump is installed unchanged; Revive (when on) is a launch-time wrapper, not a file edit
    notes = []
    mode = extra.get("launch") or ("native" if extra.get("frame_native") else "revive" if extra.get("needs_revive")
                                   else "")
    if mode == "repack":
        notes.append("The game folder is already set up for SteamVR (its own Revive, started by a loader DLL): "
                     "FramePort runs the game directly. On the Frame this is experimental (Wine is told to load the "
                     "loader DLL; the bundled Revive needs SteamVR's OpenVR under Proton).")
    elif mode == "native":
        args = extra.get("launch_args")
        notes.append("Supports SteamVR/OpenXR itself: runs directly, without Revive"
                     + (f" (arguments {args})." if args else "."))
    elif mode == "revive":
        notes.append("Oculus/LibOVR game: needs Revive, which works in PC mode with SteamVR running. Not supported on "
                     "the Steam Frame (Revive can't run there).")
    if extra.get("platform_sdk"):
        notes.append("Uses the Oculus Platform SDK (entitlement check): it needs the Oculus app (Meta Horizon) on the "
                     "PC with a license you own, so on the Frame it may not start.")
    if analysis.abis and analysis.abis[0] not in ("x86", "x86_64"):
        recipe.status = "unsupported"
        notes.append(f"Unexpected executable type {analysis.abis[0]}.")
    recipe.notes = " ".join(notes)


def warnings(recipe: Recipe) -> list[str]:
    out = []
    if recipe.package.startswith("rift."):
        for pid in recipe.patches:
            for r in base.get(pid).requires:
                if r not in recipe.patches:
                    out.append(f"{base.get(pid).title} needs {base.get(r).title}.")
            for c in base.get(pid).conflicts:
                if c in recipe.patches and pid < c:
                    out.append(f"{base.get(pid).title} conflicts with {base.get(c).title}.")
        return out
    for pid in recipe.patches:
        p = base.get(pid)
        for c in p.conflicts:
            if c in recipe.patches:
                out.append(f"{p.title} conflicts with {base.get(c).title}.")
        for r in p.requires:
            if r not in recipe.patches:
                out.append(f"{p.title} needs {base.get(r).title}.")
    if recipe.as_is or not recipe.overport:
        return out  # installed unchanged / an ordinary Android app: no VR translation expected
    if "patch_copy_libraries" not in recipe.patches:
        out.append("Without 'Copy OVRPort libraries' nothing is translated to OpenXR.")
    if "frame.adapter" not in recipe.patches:
        out.append("Without the FrameBridge adapter most games fail on the Frame runtime.")
    return out


def android_version_note(analysis: Analysis) -> str | None:
    """'Its manifest asks for Android 14 (API 34)…' when the APK's minimum Android is newer than the Frame's."""
    from ..analysis.detect import FRAME_API, android_version, too_new_android

    min_sdk = (analysis.extra or {}).get("min_sdk")
    if not too_new_android(min_sdk):
        return None
    return (f"Its manifest asks for Android {android_version(min_sdk)} (API {min_sdk}); the Frame's Android is "
            f"{android_version(FRAME_API)} (API {FRAME_API}). Apps like this often crash at start on newer Android "
            "parts, but not always: a launch test tells.")


def web_wrapper_note(analysis: Analysis) -> str | None:
    """A Trusted Web Activity: the APK only opens a website in Meta's browser (analysis.detect.web_wrapper)."""
    ww = (analysis.extra or {}).get("web_wrapper")
    if not ww:
        return None
    where = f"open {ww['url']} in a browser instead" if ww.get("url") else "open the website in a browser instead"
    return (f"This app looks like a website in an Android wrapper (Trusted Web Activity): it opens the site in "
            f"Meta's browser, which the Frame doesn't have. If nothing opens, {where}.")


def blocker_notes(analysis: Analysis) -> list[str]:
    """Warnings read from the APK's manifest that it may not run on the Frame (whatever the recipe says). Only
    warnings: the manifest alone never marks a game unsupported (owner's rule); a launch test's triage decides."""
    return [n for n in (android_version_note(analysis), web_wrapper_note(analysis)) if n]


def _static_blockers(analysis: Analysis, recipe: Recipe) -> None:
    for note in blocker_notes(analysis):
        recipe.notes = _add(recipe.notes, note)


def _non_quest(analysis: Analysis, recipe: Recipe) -> None:
    """Android apps that aren't Meta Quest apps (analysis.detect.vr_kind)."""
    kind = analysis.vr_kind
    if (analysis.extra or {}).get("split_apk"):
        recipe.notes = _add(recipe.notes, "Split APK: only the base APK was found; the game's code may live in the "
                                          "other parts of the set, which FramePort can't install.")
    if kind == "none":
        # an ordinary (2D) Android app: no OpenXR to translate. Install it unchanged, or with only the launcher fix
        recipe.overport = False
        # fixes that aren't about VR stay (navigation bar, SDL's clipboard, Unity text fields...), and the launcher
        # entry when the app only has an INFO activity
        needed = {pid for pid in recipe.patches if not base.get(pid).needs_vr and pid != "frame.launcher"}
        needed |= {"frame.launcher"} if analysis.has_info_category else set()
        recipe.patches = {pid: v for pid, v in recipe.patches.items() if pid in needed}
        recipe.reasons = {pid: v for pid, v in recipe.reasons.items() if pid in recipe.patches}
        recipe.alt_patches = []
        recipe.as_is = not any(base.get(pid).stage == "apk" for pid in recipe.patches)  # the APK stays unchanged
        recipe.notes = _add(recipe.notes, "Android app without VR: installed without FramePort's VR translation.")
    elif kind in ("pico_sdk", "wave"):
        recipe.status = "unsupported"
        recipe.notes = _add(recipe.notes, f"Uses {'Pico' if kind == 'pico_sdk' else 'HTC Vive Wave'}'s own VR SDK "
                                          "instead of OpenXR: there is no way to run it on the Steam Frame.")
    elif kind == "android_xr":
        recipe.status = "unsupported"
        recipe.notes = _add(recipe.notes, "Android XR app: it needs Android XR system services that the Steam "
                                          "Frame's Android runtime doesn't have.")
    elif kind == "openxr":
        recipe.notes = _add(recipe.notes, "OpenXR app for another headset: translated like Quest games; that "
                                          "headset's own extensions and store/platform services aren't available.")


def _add(notes: str, text: str) -> str:
    return f"{notes} {text}".strip() if text not in (notes or "") else notes


def visible_patches(analysis: Analysis, recipe: Recipe | None = None) -> tuple[list, list]:
    """(relevant, hidden) patches for this game. Enabled patches are always shown, so a recipe never hides a choice."""
    shown, hidden = [], []
    for p in base.all_patches():
        if not base.for_game(p, analysis):
            continue  # other kind of game entirely (Quest vs Rift): not even offered under "show all"
        on = recipe is not None and p.id in recipe.patches
        if p.needs_vr and analysis.vr_kind == "none" and not on:
            hidden.append(p)  # an Android app without VR: VR patches can't take effect
            continue
        (shown if on and not p.default_on or p.applies(analysis) else hidden).append(p)
    return shown, hidden
