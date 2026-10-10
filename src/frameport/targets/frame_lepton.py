"""Steam Frame target: one Lepton container per Quest game, Proton for Oculus Rift (PC VR) games, plus a Steam
library shortcut for each."""
from __future__ import annotations

from pathlib import Path

from ..core.events import Reporter
from ..core.models import Recipe
from ..frame.connection import Frame, FrameTarget, sh_quote
from ..install import drives, installer
from ..validate import device
from .base import Target


class FrameLeptonTarget(Target):
    kind = "frame"

    def __init__(self, target: FrameTarget, password: str | None = None):
        self.target = target
        self.frame = Frame(target, password)
        self.label = target.label
        self.dest: str | None = None  # where new games go; None = the library setting (drives.install_dest)

    def install_dest(self) -> str | None:
        """A drive's install dir for new installs (GitHub #90), None = internal storage. Reinstalls stay where the
        game is (the agent keeps an installed game's folder)."""
        return self.dest if self.dest is not None else drives.install_dest()

    def connect(self) -> FrameLeptonTarget:
        if self.frame.client is not None and not self.frame.alive():
            self.frame.close()  # the link died (Frame asleep, Wi-Fi gone): open a fresh one
        if self.frame.client is None:
            self.frame.connect()
        return self

    def describe(self) -> dict:
        return self.connect().frame.agent("info")

    def installed(self) -> list[dict]:
        return self.connect().frame.agent("list_installed")["games"]

    def install(self, package, title, apk: Path, data_dir, recipe: Recipe, reporter: Reporter, apk_only=False,
                data_files=None):
        plan = installer.InstallPlan(package, title, apk, data_dir, recipe, apk_only, dest=self.install_dest(),
                                     data_files=data_files)
        return installer.install(self.connect().frame, plan, reporter)

    def add_to_library(self, packages, reporter):
        return installer.add_to_steam(self.connect().frame, packages, reporter)

    def update_steam_art(self, package, reporter):
        return installer.update_steam_art(self.connect().frame, package, reporter)

    def launch_test(self, package, reporter, seconds=45):
        return device.launch_test(self.connect().frame, package, reporter, seconds)

    def session_log(self, package):
        frame = self.connect().frame
        res = frame.agent("session_log", package=package, timeout=180)  # agent >= 70
        res["text"] = frame.get_text(res["log"]) if res.get("log") and res.get("log_size") else ""
        return res

    def collect_diag(self, package=None):
        return self.connect().frame.agent("collect_diag", timeout=180, package=package)

    def launch(self, package):
        return self.connect().frame.agent("launch", package=package, timeout=60)

    def set_settings(self, package, settings):
        return self.connect().frame.agent("set_settings", package=package, settings=settings)

    def uninstall(self, package, keep_data=True):
        return self.connect().frame.agent("uninstall", package=package, keep_data=keep_data, remove_shortcut=True)

    def install_pcvr(self, package, title, game_dir, exe, recipe, reporter, **extra):
        plan = installer.PcvrPlan(package, title, Path(game_dir), exe, recipe, extra.get("revive_dir"),
                                  extra.get("exe_sha256"), extra.get("revive_version"), extra.get("art_lookup"),
                                  dest=self.install_dest(), overlay=extra.get("overlay"))
        return installer.install_pcvr(self.connect().frame, plan, reporter)

    def install_linux(self, package, title, root, exe, files, appimage, openxr, reporter, x86_64=False,
                      desktop_entry=True, overlay=None):
        plan = installer.LinuxPlan(package, title, Path(root), exe, files, appimage, openxr, x86_64,
                                   dest=self.install_dest(), desktop_entry=desktop_entry, overlay=overlay)
        return installer.install_linux(self.connect().frame, plan, reporter)

    def drives(self) -> list[dict]:
        return drives.list_drives(self.connect().frame)

    def move(self, package: str, dest: str, reporter: Reporter) -> dict:
        """Move an installed game's files to another drive (or back to internal storage: dest "internal")."""
        return drives.move(self.connect().frame, package, dest, reporter)

    def set_desktop_entry(self, package: str, enabled: bool) -> dict:
        """A Linux app's Desktop Mode entry on or off (GitHub #84)."""
        return self.connect().frame.agent("desktop_entry", package=package, enabled=enabled)

    def set_vr_overlay(self, package: str, autostart: bool) -> dict:
        """An installed SteamVR overlay app's auto-start with SteamVR (agent >= 75)."""
        return self.connect().frame.agent("register_vr_overlay", package=package, autostart=autostart)

    def proton_status(self, tool: str | None = None) -> dict:
        return self.connect().frame.agent("proton_status", tool=tool)

    def install_proton(self, unattended: bool = False, tool: str | None = None) -> dict:
        """Ask Steam on the Frame to install Proton (ARM64) + its runtime. unattended: no confirmation in the headset
        (Steam restarts once and downloads in the background)."""
        return self.connect().frame.agent("install_proton", mode="unattended" if unattended else "request", tool=tool)

    def proton_selftest(self, tool: str | None = None, **opts) -> dict:
        """Runs cmd.exe under the Frame's Proton (first run creates the prefix: a minute or two). opts: vr (default
        True: SteamGameId set, Proton sets up wineopenxr), log (PROTON_LOG), env, timeout."""
        seconds = int(opts.pop("timeout", 600))
        if opts.pop("layer", False):  # also load FramePort's timefix OpenXR layer (as installed games do)
            from ..core.paths import artifacts_dir

            frame = self.connect().frame
            remote = f"{frame.home}/.local/share/frameport/xrlayer"
            frame.run(f"mkdir -p {sh_quote(remote)}")
            for f in sorted((artifacts_dir() / "linux-arm64").iterdir()):
                frame.put(f, f"{remote}/{f.name}", resume=False)
            env = dict(opts.get("env") or {})
            env.update(XR_API_LAYER_PATH=remote, XR_ENABLE_API_LAYERS="XR_APILAYER_FRAMEPORT_timefix")
            opts["env"] = env
        return self.connect().frame.agent("proton_selftest", timeout=seconds + 120, tool=tool,
                                          **{"timeout_s": seconds, **opts})

    def install_lepton(self) -> dict:
        return self.connect().frame.agent("install_lepton")

    def close(self):
        self.frame.close()
