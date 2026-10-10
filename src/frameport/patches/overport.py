"""OVRPort CLI patches. These run inside OVRPort; we only choose which ones. The CLI comes from the downstream fork
github.com/Android-XR-Bridge/OVRPort (1.2.5+), originally github.com/ovrport/app.

The patch list is discovered dynamically (the CLI's `patches` command), and titles are fetched from ovrport/app's
strings.xml on GitHub (both cached; the fork dropped that file, so its new patches are described here). The table
below is the offline fallback and adds what we learned on the Steam Frame; `default` mirrors OVRPort's recommended
set (Patch(..., true) in its sources).
"""
from __future__ import annotations

from ..core.models import Analysis
from .base import Patch, Suggestion, register

# id, title, default, detail
OVERPORT_PATCHES = [
    ("patch_copy_libraries", "Copy OVRPort libraries", True,
     "Adds OVRPort's OpenXR loader dispatcher and platform loader. Required: without it nothing is translated."),
    ("patch_copy_ovrplugin_vrapi", "Copy OVRPlugin if VrApi is present", True,
     "Swaps in an OpenXR OVRPlugin for games that use VrApi through OVRPlugin (older Unity/Unreal)."),
    ("patch_replace_icon_label", "Replace application label and icon", True,
     "Uses the store title/icon (OVRPort image service) for the app label."),
    ("patch_fix_min_android_sdk", "Fix minimal Android SDK", True, "Raises minSdk where the loader needs it."),
    ("patch_generate_config", "Generate OVRPort config", True, "Writes liboverport.config.so with runtime options."),
    ("patch_remove_localized_names", "Remove localized app names", True, "Keeps one label so the title is stable."),
    ("patch_clean_up_frida", "Clean up Frida leftovers in smali", True, "Removes leftovers from dumped/modded APKs."),
    ("patch_oculus_unity", "Patch Oculus detection for Unity", True,
     "Makes Unity's Oculus checks pass on other runtimes."),
    ("patch_oculus_unreal", "Patch Oculus detection for Unreal", True,
     "Makes Unreal's Oculus checks pass on other runtimes."),
    ("patch_vr_metadata", "Pico/YVR/Quest metadata", True, "Adds the VR app metadata other launchers expect."),
    ("patch_launcher_entry", "Fix launcher icon entry", True, "Adds a launcher entry point (Lepton additionally needs "
     "category LAUNCHER, see the Frame 'launcher' fix)."),
    ("patch_remove_uses_library", "Remove uses-library", True, "Drops Quest-only shared library requirements."),
    ("patch_fix_unreal_crash", "Fix UE4 crash with Unity stub", True, "Works around a UE4 startup crash."),
    ("patch_meta_xr_audio", "Patch Meta XR Audio", True, "Neutralises Meta XR Audio's Quest-only calls (Unity/Wwise)."),
    ("patch_mark_as_debuggable", "Mark application as debuggable", True,
     "Lets you read logs/attach. Some Unreal games abort under CheckJNI when debuggable; the Frame 'nodebug' fix "
     "undoes it."),
    ("patch_mark_allow_backup", "Mark application to allow backup", True, "Allows data backup."),
    ("patch_remove_unreal_force_quit", "Remove Unreal's ForceQuit", False,
     "For Unreal games that close themselves right after starting (for example Phantom: Covert Ops). Also disables the "
     "in-game Quit."),
    ("patch_force_passthrough", "Force enable passthrough", False,
     "For mixed-reality-only games. On the Frame, passthrough is emulated by the FrameBridge adapter "
     "(grayscale cameras)."),
    ("patch_disable_space_warp", "Disable application space warp if used", False,
     "For heavy games that use application space warp: it causes artifacts or hangs on non-Quest runtimes "
     "(for example Asgard's Wrath 2, Batman: Arkham Shadow)."),
    ("patch_disable_controller_offset", "Disable controller tracking offset", False,
     "Removes OVRPort's controller pose offset if controllers look misplaced."),
    ("patch_remove_vrapi", "Remove VrApi library", False,
     "Not recommended: breaks games that load VrApi through OVRPlugin."),
    ("patch_vrapi_openxr", "VrApi → OpenXR adapter (OVRPort)", False,
     "OVRPort's own VrApi→OpenXR adapter for engines that call libvrapi.so directly: the same upstream code as "
     "FramePort's 'VrApi → OpenXR bridge' without its Frame-specific changes. Only in OVRPort's experimental CLI "
     "builds (the stable CLI lists it but can't apply it)."),
    ("patch_disable_meta_xr_audio_telemetry", "Disable Meta XR Audio telemetry", False,
     "Skips Meta XR Audio's telemetry under x86_64 ARM translation (emulators). Not needed on the Frame, which runs "
     "games natively."),
    ("patch_ac_nexus_no_appsw_72", "AC Nexus: no AppSW at 72 Hz", False,
     "Assassin's Creed Nexus (build 207706 only): turns off application space warp and runs at 72 Hz."),
    ("patch_ac_nexus_no_appsw_90", "AC Nexus: no AppSW at 90 Hz", False,
     "Assassin's Creed Nexus (build 207706 only): turns off application space warp and runs at 90 Hz."),
]
CONFLICTS = {"patch_vrapi_openxr": ("patch_remove_vrapi", "frame.vrapi_bridge"),
             "patch_ac_nexus_no_appsw_72": ("patch_ac_nexus_no_appsw_90",),
             "patch_ac_nexus_no_appsw_90": ("patch_ac_nexus_no_appsw_72",)}
AC_NEXUS = "com.Ubisoft.ACNexusVR"
AC_NEXUS_BUILD = "MAIN.450412.207706.final"  # OVRPort's AC Nexus patches check this build's libil2cpp.so (SHA-256)
# patches that fail the whole overport run when used outside their game/build: a catalog recipe only gets them where
# applies() is true (recommend/engine.py)
STRICT = {"patch_ac_nexus_no_appsw_72", "patch_ac_nexus_no_appsw_90", "patch_vrapi_openxr"}
DEFAULT_OVERPORT = [pid for pid, _, default, _ in OVERPORT_PATCHES if default]


class OverportPatch(Patch):
    category = "overport"
    stage = "overport"

    def __init__(self, pid: str, title: str, default: bool, detail: str):
        self.id, self.title, self.default_on, self.description = pid, title, default, detail
        self.conflicts = CONFLICTS.get(pid, ())
        self.strict = pid in STRICT

    def detect(self, analysis: Analysis) -> Suggestion | None:
        from . import applicability as ap

        a = analysis
        if self.default_on:
            return Suggestion(True, "Part of every OVRPort conversion.")
        if self.id == "patch_force_passthrough" and a.extra.get("mr_only"):
            return Suggestion(True, "Mixed-reality-only game (passthrough required, no guardian): force "
                                    "passthrough on.")
        if self.id == "patch_disable_space_warp" and "libOVRPlugin.so" in a.libs:
            total = a.extra.get("data_bytes", 0) + a.extra.get("size", 0)
            if total >= 20 * 2**30:
                return Suggestion(True, f"Very heavy game ({total / 2**30:.0f} GiB) using OVRPlugin: application space "
                                        "warp causes artifacts/hangs off-Quest.")
            return Suggestion(False, "Enable if the game shows warping artifacts or hangs (uses OVRPlugin space warp).")
        if self.id == "patch_ac_nexus_no_appsw_90" and self.applies(a):
            # verified in the headset (owner preferred 90 Hz over 72): the catalog default, now also without it
            return Suggestion(True, "This Assassin's Creed Nexus build runs smoothly on the Frame without application "
                                    "space warp, at 90 Hz.")
        if self.id == "patch_remove_unreal_force_quit" and ap.is_unreal(a):
            return Suggestion(False, "Built as the alternate APK for Unreal games (use it if the game quits itself).")
        return None

    def applies(self, analysis: Analysis) -> bool:
        from . import applicability as ap

        rules = {
            "patch_oculus_unity": ap.is_unity, "patch_oculus_unreal": ap.is_unreal,
            "patch_fix_unreal_crash": ap.is_unreal, "patch_remove_unreal_force_quit": ap.is_unreal,
            "patch_copy_ovrplugin_vrapi": ap.has_vrapi, "patch_remove_vrapi": ap.has_vrapi,
            "patch_meta_xr_audio": lambda a: bool(ap.meta_audio_libs(a)),
            "patch_disable_space_warp": lambda a: "libOVRPlugin.so" in a.libs,
            "patch_vrapi_openxr": lambda a: a.direct_vrapi and "arm64-v8a" in a.abis,
            "patch_disable_meta_xr_audio_telemetry": lambda a: False,  # emulators only; the Frame is arm64
            "patch_ac_nexus_no_appsw_72": lambda a: a.package == AC_NEXUS and a.version == AC_NEXUS_BUILD,
            "patch_ac_nexus_no_appsw_90": lambda a: a.package == AC_NEXUS and a.version == AC_NEXUS_BUILD,
        }
        rule = rules.get(self.id)
        return rule(analysis) if rule else True


STRINGS_URL = "https://raw.githubusercontent.com/ovrport/app/HEAD/composeApp/src/commonMain/composeResources/values/strings.xml"


for _pid, _title, _default, _detail in OVERPORT_PATCHES:
    register(OverportPatch(_pid, _title, _default, _detail))


def refresh(list_patches=None, fetch_titles: bool = True) -> list[str]:
    """Register patches the installed OVRPort CLI offers that this table doesn't know yet, and update titles.
    `list_patches` is a callable returning patch ids (tools.overport.list_patches). Returns newly added ids."""
    import re

    from ..core.cache import cached_text
    from .base import REGISTRY

    added = []
    if list_patches is not None:
        try:
            ids = list_patches()
        except Exception:
            ids = []
        for pid in ids:
            if pid not in REGISTRY:
                register(OverportPatch(pid, pid.removeprefix("patch_").replace("_", " ").capitalize(), False,
                                       "New OVRPort patch (not yet described by FramePort)."))
                added.append(pid)
    if fetch_titles:
        text = cached_text("overport-strings.xml", STRINGS_URL, max_age=7 * 86400)
        known = {row[0] for row in OVERPORT_PATCHES}
        for pid, title in re.findall(r'<string name="(patch_[a-z0-9_]+)">([^<]+)</string>', text or ""):
            # only for patches FramePort doesn't describe yet: ours are worded for the Frame and are the keys of
            # their translations
            if pid in REGISTRY and REGISTRY[pid].category == "overport" and pid not in known:
                REGISTRY[pid].title = title.replace("\\'", "'")
    return added
