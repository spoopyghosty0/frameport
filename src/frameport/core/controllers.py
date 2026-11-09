"""Controller preferences at the library boundary; recipe and configuration identities stay preference-free."""
from __future__ import annotations

from . import library

QUEST_BUTTONS = "adapter.quest_buttons"
CHOICES = ((0, "SteamVR default"), (1, "Mirror"), (2, "Quest pair"), (3, "One hand"))

# Test Quest Controllers (github.com/spoopyghosty0/TestQuestControllers): a small app that shows every Quest controller
# input it receives on a 3D model, for trying the controller button modes. Installed through its install link.
TESTER_PACKAGE = "io.github.spoopyghosty0.testquestcontrollers"
TESTER_MANIFEST = "https://raw.githubusercontent.com/spoopyghosty0/TestQuestControllers/main/frameport-install.json"


def quest_buttons_default() -> int:
    value = library.setting("input.quest_buttons", 0)
    return int(value) if value in (0, 1, 2, 3, "0", "1", "2", "3") else 0


def effective_adapter_settings(patches: dict) -> dict:
    from ..patches.frame.headset_model import adapter_settings

    return adapter_settings(patches, defaults={"quest_buttons": quest_buttons_default()})


def _adapter_current(game: dict, installed: dict) -> bool:
    from ..build import native_checksums

    build = game.get("build") or {}
    # A newer local build does not prove that its adapter reached the installed APK.
    sha = installed.get("sha256")
    if not sha or sha not in (build.get("sha256"), build.get("alt_sha256")):
        return False
    recorded = (build.get("build_inputs") or {}).get("native") or {}
    adapters = [name for name in recorded if name.endswith("/libopenxr_loader_generic.so")]
    current = native_checksums(adapters) or {}
    return bool(adapters) and all(recorded[name] and recorded[name] == current.get(name) for name in adapters)


def apply_default(target, reporter) -> dict[str, list[str]]:
    """Push only inherited Quest preferences. Read the library and device off the UI thread."""
    mode = quest_buttons_default()
    reporter.stage("Apply controller buttons")
    games = {g["package"]: g for g in library.games()}
    applied, needs_update = [], []
    for installed in target.installed():
        reporter.check_cancel()
        package = installed["package"]
        game = games.get(package, {})
        if installed.get("kind", "quest") != "quest" or game.get("kind", "quest") != "quest":
            continue
        analysis = game.get("analysis") or {}
        if (analysis.get("extra") or {}).get("vr_kind", "quest") != "quest":
            continue
        patches = (game.get("recipe") or {}).get("patches") or {}
        recorded = (installed.get("recipe") or {}).get("patches") or []
        if QUEST_BUTTONS in patches or (not game and QUEST_BUTTONS in recorded):
            continue
        target.set_settings(package, {"quest_buttons": mode})
        current = _adapter_current(game, installed)
        (applied if current else needs_update).append(package)
        when = "next launch" if current else "needs Update on Frame first"
        reporter.log(f"{package}: quest_buttons={mode} ({when})")
    return {"applied": applied, "needs_update": needs_update}
