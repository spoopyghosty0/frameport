"""Install targets. The pipeline only talks to this interface, so new targets plug in without touching the build,
recommend or UI layers. Quest games go through install(); Oculus Rift (PC VR) games through install_pcvr()."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..core.events import Reporter
from ..core.models import Recipe


class Target(ABC):
    kind: str = ""
    label: str = ""

    @abstractmethod
    def describe(self) -> dict: ...

    @abstractmethod
    def installed(self) -> list[dict]: ...

    @abstractmethod
    def install(self, package: str, title: str, apk: Path, data_dir: Path | None, recipe: Recipe,
                reporter: Reporter, apk_only: bool = False, data_files: list[str] | None = None) -> dict: ...

    @abstractmethod
    def add_to_library(self, packages: list[str], reporter: Reporter) -> dict: ...

    @abstractmethod
    def launch_test(self, package: str, reporter: Reporter, seconds: int = 45): ...

    def launch(self, package: str) -> dict:
        """Start an installed game for playing, through the target's Steam (Steam library shortcut)."""
        raise NotImplementedError(f"{self.label} can't launch games")

    def install_linux(self, package, title, root, exe, files, appimage, openxr, reporter, x86_64=False,
                      desktop_entry=True) -> dict:
        """Install a Linux app (only the Frame runs them; x86_64 ones through FEX)."""
        raise NotImplementedError(f"{self.label} can't install Linux apps")

    def session_log(self, package: str) -> dict:
        """The game's newest play session: {session: {start, end, test} | None, text (its log), crash, kernel}."""
        raise NotImplementedError(f"{self.label} doesn't keep play-session logs")

    def collect_diag(self, package: str | None = None) -> dict:
        """Debug data from the target for a diagnostics bundle: {"host": {...}, "files": {name: text}, ...}."""
        return {}

    @abstractmethod
    def set_settings(self, package: str, settings: dict) -> dict: ...

    @abstractmethod
    def uninstall(self, package: str, keep_data: bool = True) -> dict: ...

    def install_pcvr(self, package: str, title: str, game_dir: Path, exe: str, recipe: Recipe, reporter: Reporter,
                     **extra) -> dict:
        """Install an Oculus Rift (Windows PC VR) game: game_dir + its exe (relative), launched through Revive."""
        raise NotImplementedError(f"{self.label} can't install PC VR games")
