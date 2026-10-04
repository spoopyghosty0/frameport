"""Language packs (`<BCP47 tag>.lang`) in a game's data folder (OBB / app files); see native/langpack."""
from __future__ import annotations

import os
import re
from pathlib import Path

MAX_DEPTH = 3  # same depth as the runtime scan in native/langpack/langpack.c
TAG = re.compile(r"^[A-Za-z0-9_-]+$")


def find_tags(data_dir: Path | str | None) -> list[str]:
    """Tags of the language packs below `data_dir` (de.lang -> "de"), sorted, first file per tag."""
    if not data_dir:
        return []
    root = Path(data_dir)
    if not root.is_dir():
        return []
    seen: dict[str, str] = {}
    base_depth = len(root.parts)
    for folder, dirs, files in os.walk(root):
        if len(Path(folder).parts) - base_depth >= MAX_DEPTH:
            dirs[:] = []
        for name in files:
            stem, dot, ext = name.rpartition(".")
            if dot and ext.lower() == "lang" and TAG.match(stem):
                seen.setdefault(stem.lower(), stem)
    return sorted(seen.values(), key=str.lower)
