"""Write docs/GAMES.md: the tested games from the bundled catalog as a simple table to share.
Run after catalog changes: python scripts/compat_list.py"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from frameport.recommend import catalog  # noqa: E402

STATUS = {"works": "✅ Works", "issues": "⚠️ Works with issues", "unsupported": "❌ Doesn't run"}
ORDER = {"works": 0, "issues": 1, "unsupported": 2}


def short(note: str) -> str:
    """The note's first sentence, without technical asides."""
    first = re.split(r"(?<=[.!?])\s", " ".join((note or "").split()), maxsplit=1)[0]
    return first if len(first) <= 160 else first[:157].rstrip() + "…"


def render() -> str:
    # the repo's own recipes only (a local user catalog would replace or hide entries)
    bundled = catalog._load_dir(catalog.catalog_dir() / "games", "bundled")
    entries = [e for e in bundled.values() if e.status in STATUS]
    entries.sort(key=lambda e: (ORDER[e.status], e.title.lower()))
    rows = ["| Game | Platform | Status | Notes |", "|---|---|---|---|"]
    for e in entries:
        platform = "PC VR" if e.kind == "rift" else "Quest"
        note = "" if e.status == "works" else short(e.notes).replace("|", "/")
        rows.append(f"| {e.title} | {platform} | {STATUS[e.status]} | {note} |")
    return ("# Tested games\n\n"
            "Games tested on the Steam Frame with FramePort. Games not listed may work too: FramePort suggests "
            "patches for them. Got one working? [Share its recipe](INSTALL.md#share-a-recipe-or-report-a-problem).\n\n"
            "Generated from [catalog/games](../catalog/games) by `scripts/compat_list.py`.\n\n"
            + "\n".join(rows) + "\n")


if __name__ == "__main__":
    out = ROOT / "docs" / "GAMES.md"
    out.write_text(render(), encoding="utf-8")
    print(f"wrote {out}")
