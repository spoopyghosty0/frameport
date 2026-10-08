"""Plain data types passed between the pipeline stages."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SourceGame:
    """A game found on disk: one APK plus optional expansion data (OBB or raw asset folder)."""

    name: str  # display/folder name, e.g. "PowerWash Simulator VR v3055+2.5.0"
    apk: Path
    data_dir: Path | None = None  # folder whose contents go to Android/obb/<package>/
    origin: Path | None = None  # the folder the game was found in
    alt_apks: list[Path] = field(default_factory=list)
    # only these files of data_dir are the game's data (expansion files found by name, e.g. next to the APK); None =
    # everything below data_dir
    data_files: list[str] | None = None

    def data_bytes(self) -> int:
        return sum(data_manifest(self.data_dir, self.data_files).values())


def data_manifest(data_dir: Path | None, files: list[str] | None = None) -> dict[str, int]:
    """{path relative to data_dir: size} of a game's data: every file below data_dir, or only `files` (names in it)."""
    if not data_dir or not data_dir.is_dir():
        return {}
    if files is not None:
        return {name: (data_dir / name).stat().st_size for name in sorted(files) if (data_dir / name).is_file()}
    return {p.relative_to(data_dir).as_posix(): p.stat().st_size for p in sorted(data_dir.rglob("*")) if p.is_file()}


@dataclass
class Analysis:
    """Everything detected about an APK that patch suggestions depend on."""

    package: str
    version: str
    label: str
    abis: list[str]
    engine: str  # Unity | Unreal | Other
    xr: str  # OpenXR | VrApi | OpenXR+VrApi | ?
    graphics: str  # Vulkan (...) | GLES or unknown (...)
    direct_vrapi: bool  # calls libvrapi.so itself (no OVRPlugin): needs the VrApi bridge
    libs: list[str]  # lib names in the primary ABI dir
    launcher_activity: str | None
    has_info_category: bool  # INFO category present (Lepton needs LAUNCHER)
    meta_permissions: list[str]  # com.oculus.* / horizonos.* permissions used but not declared
    uses_glad_gl: bool  # engine resolves GL through eglGetProcAddress (GLAD): candidate for the GL shim
    unity_msaa_levels: int  # Unity QualitySettings levels with antiAliasing > 1
    oculus_os_classes: bool  # native code references com/oculus/os/AnalyticsEvent
    is_overport_output: bool  # already contains overport's loader dispatcher
    debuggable: bool
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def abi(self) -> str | None:
        return next((a for a in ("arm64-v8a", "armeabi-v7a") if a in self.abis), None)

    @property
    def only_32bit(self) -> bool:
        return "arm64-v8a" not in self.abis and "armeabi-v7a" in self.abis

    @property
    def no_arm64(self) -> bool:
        """Native code, but none the Frame can run (32-bit ARM or x86 only)."""
        return bool(self.abis) and "arm64-v8a" not in self.abis

    @property
    def vr_kind(self) -> str:
        """See analysis.detect.vr_kind (entries analysed before it existed were all Quest games)."""
        return (self.extra or {}).get("vr_kind") or "quest"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Recipe:
    """What to do to a game: the confirmed patch selection plus install-time details."""

    package: str
    patches: dict[str, dict] = field(default_factory=dict)  # patch id -> params ({} when none)
    alt_patches: list[str] = field(default_factory=list)  # overport ids added for the alternate build
    use_alt: bool = False  # install the alternate build instead of the primary
    title: str | None = None
    status: str = "unknown"  # works | issues | unsupported | unknown
    notes: str = ""
    source: str = "heuristics"  # catalog | heuristics | user
    as_is: bool = False  # install the game unchanged (already patched): no overport / Frame fixes / Revive
    overport: bool = True  # False: an ordinary Android app: no VR translation (overport) and no FrameBridge adapter
    reasons: dict[str, str] = field(default_factory=dict)  # patch id -> why it was suggested
    catalog_rev: str = ""  # CatalogEntry.rev() it was derived from: a changed catalog entry re-derives the recipe

    def enabled(self, patch_id: str) -> bool:
        return patch_id in self.patches

    def params(self, patch_id: str) -> dict:
        return self.patches.get(patch_id) or {}


@dataclass
class BuildResult:
    package: str
    apk: Path
    alt_apk: Path | None
    sha256: str
    alt_sha256: str | None
    applied: list[str]
    checks: list[dict]
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(c["ok"] is not False for c in self.checks)
