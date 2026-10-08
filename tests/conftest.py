import os
import struct
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# modules that read settings at import time (UI) run during collection, before the per-test fixture below
os.environ["FRAMEPORT_HOME"] = tempfile.mkdtemp(prefix="frameport-tests-")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real user data dir (tools, keystores, library) from tests."""
    from frameport.core import library, paths

    monkeypatch.setenv("FRAMEPORT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(library, "REFRESH_ON_UPDATE", False)  # hand-made test libraries have no version marker
    yield
    paths._removed.clear()  # an uninstall test marks the data folder as removed for the rest of the process


# ---------------------------------------------------------------------------------------------- AXML builder
def build_axml(elements, strings_extra=()):
    """elements: list of ("start", name, [(attr, kind, value)]) / ("end", name). kind: "str" | "bool" | "int" | "ref".
    Produces a UTF-16 string-pool binary XML like aapt does (enough for our editor)."""
    ns = "http://schemas.android.com/apk/res/android"
    pool = []

    def idx(s):
        if s not in pool:
            pool.append(s)
        return pool.index(s)

    idx(ns)
    for e in elements:
        idx(e[1])
        if e[0] == "start":
            for a, kind, v in e[2]:
                idx(a)
                if kind == "str":
                    idx(v)
    for s in strings_extra:
        idx(s)
    data = b""
    offsets = []
    for s in pool:
        offsets.append(len(data))
        data += struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\0\0"
    data += b"\0" * (-len(data) % 4)
    hsz = 28
    sstart = hsz + 4 * len(pool)
    pool_chunk = struct.pack("<HHIIIIII", 0x0001, hsz, sstart + len(data), len(pool), 0, 0, sstart, 0)
    pool_chunk += struct.pack(f"<{len(pool)}I", *offsets) + data
    body = b""
    for e in elements:
        if e[0] == "start":
            attrs = b""
            for a, kind, v in e[2]:
                if kind == "str":
                    attrs += struct.pack("<IIIHBBI", idx(ns), idx(a), idx(v), 8, 0, 0x03, idx(v))
                elif kind in ("int", "ref"):  # decimal integer / resource reference (@string/…)
                    attrs += struct.pack("<IIIHBBI", idx(ns), idx(a), 0xFFFFFFFF, 8, 0, 0x10 if kind == "int" else 0x01,
                                         v)
                else:
                    attrs += struct.pack("<IIIHBBI", idx(ns), idx(a), 0xFFFFFFFF, 8, 0, 0x12, 0xFFFFFFFF if v else 0)
            ext = struct.pack("<IIHHHHHH", 0xFFFFFFFF, idx(e[1]), 20, 20, len(e[2]), 0, 0, 0)
            chunk = struct.pack("<HHIII", 0x0102, 16, 16 + len(ext) + len(attrs), 1, 0xFFFFFFFF) + ext + attrs
        else:
            chunk = struct.pack("<HHIII", 0x0103, 16, 24, 1, 0xFFFFFFFF) + struct.pack("<II", 0xFFFFFFFF, idx(e[1]))
        body += chunk
    total = 8 + len(pool_chunk) + len(body)
    return struct.pack("<HHI", 0x0003, 8, total) + pool_chunk + body


@pytest.fixture
def quest_manifest():
    return build_axml([
        ("start", "manifest", [("package", "str", "com.example.questgame")]),
        ("start", "uses-permission", [("name", "str", "com.oculus.permission.USE_SCENE")]),
        ("end", "uses-permission"),
        ("start", "uses-permission", [("name", "str", "android.permission.INTERNET")]),
        ("end", "uses-permission"),
        ("start", "application", [("debuggable", "bool", True)]),
        ("start", "activity", [("name", "str", "com.example.Main")]),
        ("start", "intent-filter", []),
        ("start", "action", [("name", "str", "android.intent.action.MAIN")]),
        ("end", "action"),
        ("start", "category", [("name", "str", "android.intent.category.INFO")]),
        ("end", "category"),
        ("end", "intent-filter"),
        ("end", "activity"),
        ("end", "application"),
        ("end", "manifest"),
    ])


def games_dir():
    d = os.environ.get("FRAMEPORT_GAMES")
    return Path(d) if d else None
