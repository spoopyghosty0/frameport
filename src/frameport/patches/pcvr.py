"""PC VR (Oculus Rift) options, shown in the UI as patches like the OVRPort ones.

They don't edit the game: they decide how FramePort launches it. The analysis (analysis/rift.py launch_mode)
picks one of three ways, and the patches below follow it (the user can change each one):
  repack  pcvr.repack_launcher: the folder is already set up for SteamVR (bundled Revive + its loader DLL) → run
          the exe directly (on the Frame, Wine is told to load the loader DLL from the game folder)
  native  pcvr.launch_args: SteamVR/OpenXR-capable build → run the exe directly with the arguments that select it
  revive  pcvr.revive: Oculus-only → the Steam shortcut runs ReviveInjector.exe (on the Frame: under Proton, OpenXR
          bridged to the Frame runtime via wineopenxr)
"""
from __future__ import annotations

from .base import Param, Patch, Suggestion, register


def _rift(analysis) -> bool:
    return (analysis.extra or {}).get("kind") == "rift"


def _mode(analysis) -> str:
    """repack | native | revive (older analyses without "launch": from their flags)."""
    x = analysis.extra or {}
    if x.get("launch"):
        return x["launch"]
    return "native" if x.get("frame_native") else "revive"


class _PcvrPatch(Patch):
    category = "pcvr"
    stage = "install"

    def applies(self, analysis) -> bool:
        return _rift(analysis)


class Revive(_PcvrPatch):
    id = "pcvr.revive"
    title = "Revive (Oculus LibOVR → OpenXR)"
    description = ("Launch the game through Revive's injector, which translates the Oculus PC SDK (LibOVR) to OpenXR. "
                   "Needed by every Oculus Rift game that isn't OpenXR-native. FramePort downloads Revive itself.")
    order = 10
    conflicts = ("pcvr.repack_launcher",)

    def detect(self, analysis):
        # Oculus/LibOVR games need Revive to get VR (the bare exe runs flat). Off for games with no Oculus code, which
        # SteamVR (PC) and the Frame's wineopenxr run directly (e.g. Rick and Morty, handled by its catalog recipe).
        if not _rift(analysis):
            return None
        mode = _mode(analysis)
        if mode == "repack":
            return Suggestion(False, "The game folder already has its own Revive set up (see 'Use the repack's "
                                     "launcher'); a second Revive breaks it.")
        if mode == "native":
            return Suggestion(False, "Supports SteamVR/OpenXR itself: SteamVR / the Frame run it directly.")
        return Suggestion(True, "Oculus/LibOVR game: Revive translates it to SteamVR/OpenXR (without it it runs flat).")


class ReviveOpenVR(_PcvrPatch):
    id = "pcvr.revive_openvr"
    title = "Revive: SteamVR (OpenVR) backend on PC"
    description = ("On this PC, launch the game through Revive's OpenVR backend, which talks straight to SteamVR — the "
                   "original, most reliable Revive path. On by default for PC installs. Turn it off to use Revive's "
                   "newer OpenXR backend instead. Ignored on the Frame (which always uses OpenXR).")
    order = 20
    requires = ("pcvr.revive",)

    def detect(self, analysis):
        if not _rift(analysis):
            return None
        if _mode(analysis) != "revive":
            return Suggestion(False, "Not launched through FramePort's Revive.")
        return Suggestion(True, "PC: Revive's SteamVR/OpenVR backend is the most reliable path.")


class LibovrRedirect(_PcvrPatch):
    id = "pcvr.libovr_redirect"
    title = "Provide the Oculus runtime from Revive (Frame)"
    description = ("On the Frame, place Revive's runtime (FramePort's Revive, or the repack's bundled LibRevive) where "
                   "the game's Oculus SDK looks for LibOVRRT so it can load a VR runtime (pure runtime substitution — "
                   "what Revive's LoadLibrary hook does on Windows; that hook doesn't work under Proton on the Frame). "
                   "It does NOT bypass the Oculus runtime signature check: a game that verifies the runtime's "
                   "signature will still refuse it. Ignored on this PC (Revive's hook handles it there).")
    order = 12

    def detect(self, analysis):
        if not _rift(analysis):
            return None
        if _mode(analysis) == "native":
            return Suggestion(False, "Supports SteamVR/OpenXR itself: no Oculus runtime needed.")
        return Suggestion(True, "Lets the game find Revive's runtime on the Frame (doesn't bypass its signature "
                                "check).")


class RepackLauncher(_PcvrPatch):
    id = "pcvr.repack_launcher"
    title = "Use the repack's launcher (bundled Revive)"
    description = ("Some game folders are already set up for SteamVR: a Revive copy (LibRevive64.dll) next to the "
                   "game, started by a small loader DLL (for example xinput1_3.dll) that Windows loads from the game's "
                   "folder. Then FramePort starts the game program directly — like double-clicking it — instead of "
                   "through its own Revive (two Revives conflict; for example the Oculus Platform check then fails). "
                   "On the Frame, Wine is told to use those loader DLLs from the game folder instead of its own.")
    order = 8
    conflicts = ("pcvr.revive",)
    params = [Param("dlls", "str", "", "loader DLLs next to the exe (comma-separated)")]

    def detect(self, analysis):
        if not _rift(analysis):
            return None
        if _mode(analysis) == "repack":
            dlls = ",".join(analysis.extra.get("loader_dlls") or [])
            return Suggestion(True, f"The game folder has its own Revive and loader ({dlls}): run the game directly.",
                              {"dlls": dlls})
        return None

    def applies(self, analysis) -> bool:
        return _rift(analysis) and bool((analysis.extra or {}).get("loader_dlls"))


class LaunchArgs(_PcvrPatch):
    id = "pcvr.launch_args"
    title = "Launch options (SteamVR or OpenXR)"
    description = ("Command-line arguments for the game program. Games that support several VR runtimes pick one "
                   "with an argument, for example Unreal's -hmd=OpenXR or -hmd=SteamVR, Unity's -vrmode OpenVR, or a "
                   "game's own switch (-Runtime=OpenXRHMD). FramePort fills them in from the game files (the "
                   "repack's VD.bat with the Oculus module swapped for OpenXR, else the engine's usual switch).")
    order = 9
    params = [Param("args", "str", "", "arguments, for example -hmd=OpenXR")]

    def detect(self, analysis):
        if not _rift(analysis):
            return None
        args = (analysis.extra or {}).get("launch_args") or ""
        if args:
            src = (analysis.extra or {}).get("launch_args_from") or "the game files"
            return Suggestion(True, f"Selects SteamVR/OpenXR ({src}).", {"args": args})
        return None


class XrTimefix(_PcvrPatch):
    id = "pcvr.xr_timefix"
    title = "Frame OpenXR compatibility layer"
    description = ("Loads FramePort's OpenXR layer under Proton on the Frame. The Frame's SteamVR runtime only accepts "
                   "OpenXR 1.0 apps, but Proton's VR helper asks for 1.1, so without the layer VR never starts (the "
                   "game shows as a flat window, or Revive fails with 'Unable to load LibOVRRT DLL'); the layer "
                   "retries as 1.0. It also emulates xrConvertTimespecTimeToTimeKHR if a runtime refuses it. "
                   "Ignored on this PC.")
    order = 25

    def detect(self, analysis):
        if _rift(analysis):
            return Suggestion(True, "The Frame's OpenXR runtime rejects Proton's OpenXR 1.1 request without it.")
        return None


class NoCrashReporter(_PcvrPatch):
    id = "pcvr.no_crash_reporter"
    title = "No Unreal crash reporter"
    description = ("Unreal games start CrashReportClient when they crash, which leaves a crash dialog instead of "
                   "simply closing. This passes -nocrashreports to the game and, on the Frame, renames the game's copy "
                   "of CrashReportClient.exe so it can't start (your game files on this PC aren't changed).")
    order = 15

    def detect(self, analysis):
        if _rift(analysis) and analysis.engine == "Unreal":
            return Suggestion(True, "Unreal game: close on a crash instead of showing the crash reporter.")
        return None

    def applies(self, analysis) -> bool:
        return _rift(analysis) and analysis.engine == "Unreal"


class OculusUnreal(_PcvrPatch):
    id = "pcvr.oculus_unreal"
    title = "Patch Oculus detection for Unreal (Frame)"
    description = ("The PC VR counterpart of OVRPort's 'Patch Oculus detection for Unreal'. Unreal's Oculus plugin "
                   "only starts when the Oculus service announces a headset (the Windows event 'OculusHMDConnected'); "
                   "without it the game runs as a flat window. On the Frame the launcher runs the game through "
                   "FramePort's small helper (fp_oculushmd.exe) that provides that event while the game runs, instead "
                   "of relying only on Revive's hook of the check. Ignored on this PC (the Oculus app or Revive "
                   "handle it there).")
    order = 18

    def detect(self, analysis):
        if not (_rift(analysis) and analysis.engine == "Unreal"):
            return None
        if _mode(analysis) == "native":
            return Suggestion(False, "Uses SteamVR/OpenXR, not its Oculus plugin.")
        return Suggestion(True, "Unreal game: its Oculus plugin checks for the Oculus service before it starts VR.")

    def applies(self, analysis) -> bool:
        return _rift(analysis) and analysis.engine == "Unreal"


class ProtonLog(_PcvrPatch):
    id = "pcvr.proton_log"
    title = "Proton debug log (Frame)"
    description = ("Write Proton's log (PROTON_LOG=1) to steam-<appid>.log in the game folder on the Frame. Slower; "
                   "use while debugging a game that doesn't start.")
    order = 30


class ProtonTool(_PcvrPatch):
    id = "pcvr.proton_tool"
    title = "Proton version (Frame)"
    description = ("Which Proton build runs the game on the Frame: the stable Proton (the default: smoother in our "
                   "tests) or Proton Experimental (gets Valve's ARM64 fixes first; try it when a game doesn't start "
                   "or runs badly). Also takes any compat tool name from the Frame's ARM64 list.")
    order = 40
    params = [Param("tool", "str", "", "proton-stable (default), proton-experimental, or a compat tool name")]
    CHOICES = (("", "Stable (default)"), ("proton-experimental", "Experimental"))


class ProtonEnv(_PcvrPatch):
    id = "pcvr.proton_env"
    title = "Extra Proton settings (Frame)"
    description = ("Environment variables for the Proton launcher on the Frame (for example DXVK_HUD=fps), one "
                   "KEY=value per line.")
    order = 50
    params = [Param("env", "text", "", "KEY=value lines")]


class SteamvrTuning(_PcvrPatch):
    id = "pcvr.steamvr_tuning"
    title = "Automatic SteamVR performance settings (this PC)"
    description = ("Before each Play on this PC, FramePort reads SteamVR's record of the game's last session (frames "
                   "dropped; frame times from fpsVR if installed). If it dropped frames, it lowers the game's refresh "
                   "rate to one whose frame budget fits (for example 96 → 90 Hz) and turns motion smoothing on, "
                   "through SteamVR's per-application settings; each later session can step down again. Set a refresh "
                   "rate or smoothing mode here to choose yourself. Ignored on the Frame.")
    order = 45
    params = [Param("refresh", "float", 0.0, "refresh rate in Hz (0 = automatic)", 0, 144),
              Param("smoothing", "str", "auto", "motion smoothing: auto, on, off, always or global")]

    def detect(self, analysis):
        if _rift(analysis):
            return Suggestion(True, "Adjusts the refresh rate / motion smoothing when the game can't keep up.")
        return None


for _cls in (RepackLauncher, LaunchArgs, Revive, ReviveOpenVR, LibovrRedirect, NoCrashReporter, OculusUnreal, XrTimefix,
             ProtonLog, ProtonTool, ProtonEnv, SteamvrTuning):
    register(_cls)


def game_args(recipe) -> list[str]:
    """Extra command-line arguments for the game itself."""
    import shlex

    out = []
    if "pcvr.launch_args" in recipe.patches:
        raw = str(recipe.params("pcvr.launch_args").get("args") or "")
        try:
            out += shlex.split(raw, posix=True)
        except ValueError:
            out += raw.split()
    if "pcvr.no_crash_reporter" in recipe.patches and "-nocrashreports" not in out:
        out.append("-nocrashreports")
    return out


def loader_overrides(recipe) -> str:
    """WINEDLLOVERRIDES entry for the repack's loader DLLs (native first, then Wine's builtin)."""
    if "pcvr.repack_launcher" not in recipe.patches:
        return ""
    raw = str(recipe.params("pcvr.repack_launcher").get("dlls") or "")
    names = [n.strip().lower().removesuffix(".dll") for n in raw.replace(";", ",").split(",") if n.strip()]
    names = [n for n in names if n.replace("_", "").isalnum()]
    return f"{','.join(names)}=n,b" if names else ""


def launch_env(recipe) -> dict[str, str]:
    """Environment for the Frame launcher from the recipe's pcvr patches."""
    env = {}
    if "pcvr.proton_log" in recipe.patches:
        env["PROTON_LOG"] = "1"
    if loader_overrides(recipe):
        env["WINEDLLOVERRIDES"] = loader_overrides(recipe)
    raw = recipe.params("pcvr.proton_env").get("env") or ""
    items = raw.items() if isinstance(raw, dict) else (line.partition("=")[::2] for line in str(raw).splitlines())
    for k, v in items:
        k = k.strip()
        if k == "WINEDLLOVERRIDES" and env.get(k):  # the user's overrides extend the loader's
            env[k] = env[k] + ";" + str(v).strip()
        elif k:
            env[k] = str(v).strip()
    return env
