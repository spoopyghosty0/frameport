"""SteamVR overlay apps (OpenVR VRApplication_Overlay: fpsVR, wrist watches, ...): programs that draw no scene of their
own; SteamVR's compositor draws their overlays over whatever runs, on the Frame Lepton (Quest) and Proton (PC VR)
games included (docs/FRAME_RUNTIME.md "Overlay apps"). FramePort registers them with SteamVR (pcvr.vr_overlay /
a Linux app's "vr_overlay" field; agent register_vr_overlay).

Detection, for Windows programs (analysis/rift.py) and Linux apps (analysis/linux.py):
  - proof: a SteamVR application manifest (*.vrmanifest) next to the program or at the folder's top that declares a
    dashboard overlay (`is_dashboard_overlay`). Its entry (app key, name, image, arguments) is kept so FramePort
    registers the app under its own key, with paths made absolute on the Frame;
  - heuristic: an OpenVR program without a game engine that uses IVROverlay and registers itself with SteamVR
    ("vrmanifest" / "VRApplication_Overlay" in its code; C# apps keep strings as UTF-16).
"""
from __future__ import annotations

import json
from pathlib import Path

OVERLAY_MARKERS = (b"IVROverlay_",)
SELF_REGISTER_MARKERS = (b"vrmanifest", b"VRApplication_Overlay")
APP_FIELDS = ("app_key", "image_path", "strings", "arguments")


def has_markers(blob: bytes, markers) -> bool:
    return any(m in blob or m.decode().encode("utf-16-le") in blob for m in markers)


def manifests(dirs) -> list[Path]:
    """*.vrmanifest files directly in these folders (no duplicates)."""
    found = []
    for d in dict.fromkeys(Path(x) for x in dirs):
        try:
            found += sorted(p for p in d.iterdir() if p.suffix.lower() == ".vrmanifest" and p.is_file())
        except OSError:
            pass
    return list(dict.fromkeys(found))


def bundled_overlay(root: Path, dirs) -> dict | None:
    """The first bundled manifest entry that declares a dashboard overlay:
    {"app": {app_key, image_path, strings, arguments}, "manifest": <path relative to root>, "manifest_dir": <its
    folder relative to root>}, or None."""
    root = Path(root)
    for m in manifests(dirs):
        try:
            data = json.loads(m.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            continue
        apps = data.get("applications") if isinstance(data, dict) else None
        for app in apps if isinstance(apps, list) else []:
            if isinstance(app, dict) and app.get("is_dashboard_overlay"):
                try:
                    rel = m.relative_to(root)
                except ValueError:
                    continue
                return {"app": {k: app[k] for k in APP_FIELDS if k in app}, "manifest": rel.as_posix(),
                        "manifest_dir": rel.parent.as_posix() or "."}
    return None


def detect(root: Path, program: Path, program_data: bytes, openvr: bool, engine: str = "Other") -> dict:
    """{"vr_overlay": bool, "vr_overlay_from": why, "vr_overlay_app": bundled_overlay() or None} for a program."""
    bundled = bundled_overlay(root, (program.parent, root))
    if bundled:
        return {"vr_overlay": True, "vr_overlay_app": bundled,
                "vr_overlay_from": f"{Path(bundled['manifest']).name} declares a SteamVR dashboard overlay"}
    if openvr and engine == "Other" and has_markers(program_data, OVERLAY_MARKERS) and \
            has_markers(program_data, SELF_REGISTER_MARKERS):
        return {"vr_overlay": True, "vr_overlay_app": None,
                "vr_overlay_from": f"{program.name} is an OpenVR overlay program (IVROverlay, registers itself with "
                                   "SteamVR)"}
    return {"vr_overlay": False, "vr_overlay_app": None, "vr_overlay_from": ""}


def install_args(extra: dict, autostart: bool) -> dict:
    """The agent's `overlay` argument (finalize_pcvr / finalize_linux) for an analysis' extra."""
    bundled = extra.get("vr_overlay_app") or {}
    return {"autostart": bool(autostart), "app": bundled.get("app"), "manifest_dir": bundled.get("manifest_dir") or "."}
