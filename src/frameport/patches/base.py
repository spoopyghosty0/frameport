"""The patch plugin interface.

A patch is one self-contained, optional change. Stages run in this order:
  OVRPort  -> passed to the OVRPort CLI as `--patches=` (see patches/overport.py)
  apk       -> edits the OVRPort output (FrameBridge adapter, manifest fixes, library fixes)
  install   -> files/env written on the Frame at install time (adapter settings, config files)

Each patch can suggest itself from an Analysis (`detect`), applies itself (`apply`) and can report checks
(`validate`). New patches only need a module that calls `register(...)`; nothing else changes.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..apk.workspace import ApkWorkspace
    from ..core.events import Reporter
    from ..core.models import Analysis

STAGES = ("overport", "apk", "install")
CATEGORIES = ("overport", "frame", "adapter", "device", "pcvr")


@dataclass
class Suggestion:
    recommended: bool
    reason: str
    params: dict = field(default_factory=dict)


@dataclass
class Param:
    key: str
    kind: str  # "float" | "int" | "bool" | "str" | "text"
    default: Any
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None


@dataclass
class ApkContext:
    ws: ApkWorkspace
    analysis: Analysis
    params: dict
    reporter: Reporter
    recipe_patches: dict  # full selection, for patches that depend on others
    notes: list[str] = field(default_factory=list)


@dataclass
class InstallContext:
    package: str
    params: dict
    files: dict[str, bytes]  # path relative to Android/data/<pkg>/files -> content
    env: dict[str, str]  # extra Lepton env exports
    adapter_settings: dict[str, Any]
    flatscreen: bool = False  # show the app's Android window (Lepton's lepton-show-flatscreen) even for a VR app
    display: str = ""  # device.display_mode: "vr" / "flat" = the user's choice, "" = automatic (2D apps get a window)


class Patch:
    id: str = ""
    title: str = ""
    description: str = ""
    category: str = "frame"
    stage: str = "apk"
    order: int = 50  # apk-stage patches run in ascending order
    params: list[Param] = []
    conflicts: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    default_on: bool = False  # part of the always-recommended baseline
    experimental: bool = False
    needs_vr: bool = True  # only matters for VR apps (hidden for Android apps without VR)
    on_pc: bool = False  # also in the build for this PC (AXRB): a fix of the APK itself, not of the Frame/Lepton
    revision: int = 1  # bump when apply() writes something different: installed builds then show "Update on Frame"
    package_revisions: dict[str, int] = {}  # narrower updates to otherwise shared patches

    def revision_for(self, package: str = "") -> int:
        return self.package_revisions.get(package, self.revision)

    @property
    def summary(self) -> str:
        """One plain sentence for the game page (patches/summaries.py); the description is the technical text."""
        from .summaries import SUMMARIES

        return SUMMARIES.get(self.id, "")

    def detect(self, analysis: Analysis) -> Suggestion | None:
        return Suggestion(True, "Recommended for every game.") if self.default_on else None

    def applies(self, analysis: Analysis) -> bool:
        """False when the patch can't matter for this game (wrong engine, no VrApi, ...). The UI hides such patches
        (they stay in the recipe if they are defaults, where they are no-ops)."""
        return True

    not_applicable_reason: str = ""

    def apply(self, ctx: ApkContext) -> bool:  # apk stage; returns True when something changed
        raise NotImplementedError

    def install(self, ctx: InstallContext) -> None:  # install stage
        raise NotImplementedError

    def validate(self, ctx: ApkContext) -> list[tuple[str, bool | None, str]]:
        return []

    def describe(self) -> dict:
        return {"id": self.id, "title": self.title, "description": self.description, "category": self.category,
                "stage": self.stage, "experimental": self.experimental,
                "params": [p.__dict__ for p in self.params], "conflicts": list(self.conflicts)}


REGISTRY: dict[str, Patch] = {}


def register(patch: Patch | type[Patch]) -> Patch:
    instance = patch() if isinstance(patch, type) else patch
    if not instance.id or instance.id in REGISTRY:
        raise ValueError(f"bad or duplicate patch id {instance.id!r}")
    if instance.stage not in STAGES or instance.category not in CATEGORIES:
        raise ValueError(f"{instance.id}: bad stage/category")
    REGISTRY[instance.id] = instance
    return instance


def get(patch_id: str) -> Patch:
    load_all()
    try:
        return REGISTRY[patch_id]
    except KeyError:
        raise KeyError(f"unknown patch {patch_id!r}") from None


def for_game(patch: Patch, analysis: Analysis) -> bool:
    """Quest patches only for Quest games, PC VR (Revive) patches only for Rift games."""
    rift = (analysis.extra or {}).get("kind") == "rift"
    return (patch.category == "pcvr") == rift


def pc_selection(patches: dict) -> dict:
    """The part of a Quest recipe that goes into the build for this PC (AXRB): OVRPort's patches and the APK fixes
    marked on_pc. Steam Frame fixes (FrameBridge, Lepton workarounds) stay out; AXRB has its own OpenXR runtime."""
    load_all()
    return {pid: params for pid, params in patches.items()
            if pid in REGISTRY and (REGISTRY[pid].category == "overport" or REGISTRY[pid].on_pc)}


def all_patches() -> list[Patch]:
    load_all()
    return sorted(REGISTRY.values(), key=lambda p: (CATEGORIES.index(p.category), p.order, p.id))


_loaded = False
_load_lock = threading.Lock()
_loading = threading.local()  # the loading thread's own lookups (from module imports) return early


def load_all() -> None:
    """Import every patch module once. Other threads wait until the registry is complete (a GUI background thread
    once looked up a patch while another thread was still importing them: "unknown patch")."""
    global _loaded
    if _loaded or getattr(_loading, "active", False):
        return
    with _load_lock:
        if _loaded:
            return
        _loading.active = True
        try:
            _import_modules()
        finally:
            _loading.active = False
        _loaded = True


def _import_modules() -> None:
    import importlib
    import pkgutil

    from . import frame, overport, pcvr, settings  # noqa: F401  (registration side effects)

    for mod in pkgutil.iter_modules(frame.__path__):
        importlib.import_module(f"{frame.__name__}.{mod.name}")


def recipe_fingerprint(recipe: dict, package: str = "") -> str:
    """What a build of this recipe contains: the patch selection with parameters, the build choices and each enabled
    patch's revision. A build whose fingerprint differs from the recipe's now is outdated (a catalog fix, a revised
    patch or a changed option), even when nothing was rebuilt yet."""
    import hashlib
    import json

    load_all()
    patches = recipe.get("patches") or {}
    revisions = {p: REGISTRY[p].revision_for(package) for p in sorted(patches) if p in REGISTRY}
    d = {"patches": patches, "use_alt": bool(recipe.get("use_alt")), "alt": sorted(recipe.get("alt_patches") or []),
         "as_is": bool(recipe.get("as_is")), "overport": recipe.get("overport", True),
         "rev": {p: r for p, r in revisions.items() if r != 1}}
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]


def revised_since_unrecorded(recipe: dict, package: str = "") -> bool:
    """For builds made before fingerprints were recorded: an enabled patch has been revised since (revision > 1)."""
    load_all()
    return any(p in REGISTRY and REGISTRY[p].revision_for(package) > 1 for p in recipe.get("patches") or {})
