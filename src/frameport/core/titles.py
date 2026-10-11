"""Display titles for games that exist in both a Quest and a Rift version (kept as separate library entries).

While both versions are in the library they're shown — and named in Steam — as "<Title> (Quest)" / "<Title> (Rift)".
"""
from __future__ import annotations


def _norm(t: str) -> str:
    from ..pipeline import _norm_title

    return _norm_title(t)


def twins(games: list[dict]) -> set[str]:
    """Packages that have a counterpart of the other platform in the library (Quest ⟷ Rift version of one game)."""
    by_pkg = {g["package"]: g for g in games}
    quest_by_title = {}
    for g in games:
        if g.get("kind") not in ("rift", "linux"):  # (Linux apps have no Quest/Rift twin)
            quest_by_title.setdefault(_norm(g.get("title") or ""), []).append(g["package"])
    out = set()
    for g in games:
        if g.get("kind") != "rift":
            continue
        mates = set(quest_by_title.get(_norm(g.get("title") or ""), []))
        if g.get("quest_package") in by_pkg:
            mates.add(g["quest_package"])
        if mates:
            out |= mates | {g["package"]}
    return out


def display_title(game: dict, twin_set: set[str] | None = None) -> str:
    """The title shown everywhere (cards, pages, jobs, Steam shortcut): "<Title> (Quest)" / "(Rift)" while both
    versions of a game are in the library. A name the user chose (Rename…, title_locked) is shown as it is."""
    title = game.get("title") or game["package"]
    if twin_set is not None and game["package"] in twin_set and not game.get("title_locked"):
        return f"{title} ({'Rift' if game.get('kind') == 'rift' else 'Quest'})"
    return title


def counterparts(game: dict, games: list[dict]) -> list[dict]:
    """The other-platform versions of this game in the library."""
    if game.get("kind") == "linux":
        return []
    rift = game.get("kind") == "rift"
    t = _norm(game.get("title") or "")
    out = []
    for g in games:
        if g["package"] == game["package"] or (g.get("kind") == "rift") == rift or g.get("kind") == "linux":
            continue
        linked = (rift and game.get("quest_package") == g["package"]) or \
            (not rift and g.get("quest_package") == game["package"])
        if linked or (t and _norm(g.get("title") or "") == t):
            out.append(g)
    return out
