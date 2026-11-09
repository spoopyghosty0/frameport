"""Pure Quest update decisions shared by the pipeline and UI (no filesystem or network I/O)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .patches import base, upstream


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:32]


# These settings select APK support as well as runtime behaviour. Keep their revisions too: a newer shim can
# be needed even though the value did not change. Foveation remains an APK patch, including all its parameters.
APK_SETTINGS = {"adapter.haptic_fix", "adapter.controller_models", "adapter.surface_native",
                "adapter.zink_shader_fix", "adapter.zink_shader_dump"}


def apk_recipe_fingerprint(recipe: dict, package: str = "", superseded=None) -> str:
    base.load_all()
    patches = upstream.without_superseded(recipe.get("patches") or {}, superseded)
    selected = {}
    for pid, params in patches.items():
        patch = base.REGISTRY.get(pid)
        if patch is None or patch.stage != "install" or pid in APK_SETTINGS:
            selected[pid] = params
        elif patch.category == "adapter" and patch.revision_for(package) != 1:
            # Adapter revisions also track changes to the native code implementing a setting.
            selected[pid] = {"revision": patch.revision_for(package)}
    if recipe.get("as_is"):
        return base.recipe_fingerprint({"as_is": True, "overport": False}, package)
    return base.recipe_fingerprint({**recipe, "patches": selected, "use_alt": False}, package)


def configuration(recipe: dict, package: str = "", superseded=None) -> dict:
    from .patches.frame.headset_model import adapter_settings
    from .patches.settings import BASE_SETTINGS, SETTINGS

    patches = upstream.without_superseded(recipe.get("patches") or {}, superseded)
    # Explicit defaults override settings embedded in an older APK when a recipe removes an override.
    settings = {key: default for key, _, default, *_ in SETTINGS}
    settings.update(BASE_SETTINGS)
    settings.update(adapter_settings(patches))
    install = {pid: {"params": params or {}, "revision": base.get(pid).revision_for(package)}
               for pid, params in patches.items() if base.get(pid).stage == "install" and
               base.get(pid).category != "adapter"}
    return {"settings": settings, "patches": install}


def configuration_fingerprint(recipe: dict, package: str = "", superseded=None) -> str:
    return fingerprint(configuration(recipe, package, superseded))


def configuration_matches(recipe: dict, applied: str | None, package: str = "", superseded=None) -> bool:
    current = configuration(recipe, package, superseded)
    if fingerprint(current) == applied:
        return True
    if "adapter.quest_buttons" in (recipe.get("patches") or {}):
        return False
    # set_settings records the live value in the agent's configuration fingerprint. Any inherited mode is
    # current: changing the library default is applied explicitly, without making the whole library outdated.
    for mode in (1, 2, 3):
        current["settings"]["quest_buttons"] = mode
        if fingerprint(current) == applied:
            return True
    return False


@dataclass(frozen=True)
class UpdatePlan:
    rebuild: bool = False
    apk: bool = False
    data: bool = False
    configure: bool = False
    artwork: bool = False
    shortcut: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return any((self.rebuild, self.apk, self.data, self.configure, self.artwork, self.shortcut))

    @property
    def launch_test(self) -> bool:
        return self.rebuild or self.apk or self.data


def plan_update(game: dict, deployment: dict | None, *, apk_only: bool = False,
                data_manifest: dict | None = None) -> UpdatePlan:
    """Unknown legacy identities use the full path once. Catalog metadata never affects these identities."""
    recipe, build = game.get("recipe") or {}, game.get("build") or {}
    package = game.get("package", "")
    dep = deployment or {}
    missing = not dep or dep.get("apk_present") is False
    current = apk_recipe_fingerprint(recipe, package, build.get("superseded"))
    rebuild = not build.get("ok", True) or not build.get("apk_recipe_fp") or build["apk_recipe_fp"] != current
    if build.get("source_version") != (game.get("analysis") or {}).get("version"):
        rebuild = True
    if game.get("source_sha256") and build.get("source_sha256") != game["source_sha256"]:
        rebuild = True
    if recipe.get("use_alt") and not build.get("alt_sha256"):
        rebuild = True
    sha = build.get("alt_sha256") if recipe.get("use_alt") else build.get("sha256")
    apk = missing or rebuild or not sha or dep.get("sha256") != sha
    configure = apk or not configuration_matches(recipe, dep.get("configuration_fp"),
                                                 package, build.get("superseded"))
    if data_manifest is None:
        data_manifest = game.get("data_manifest")
    data = False
    if data_manifest:  # the Frame stores renamed expansion files under the names the game looks for (#481)
        from .sources.quest_dump import frame_manifest

        data_manifest = frame_manifest(package, data_manifest)
    if not apk_only and data_manifest:
        if "data_manifest" in dep:
            data = any(dep["data_manifest"].get(k) != v for k, v in data_manifest.items())
        else:
            data = fingerprint(data_manifest) != dep.get("data_fp")
    artwork = missing or bool(game.get("steam_art_stale"))
    shortcut = missing or bool(game.get("steam_name_stale")) or artwork
    reasons = tuple(name for name, needed in (("APK build inputs changed or unrecorded", rebuild),
                    ("APK differs", apk), ("Game data differs", data), ("Runtime configuration differs", configure),
                    ("Artwork differs", artwork), ("Steam entry differs", shortcut)) if needed)
    return UpdatePlan(rebuild, apk, data, configure, artwork, shortcut, reasons)
