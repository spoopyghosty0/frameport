"""Find methods in an IL2CPP Unity game: Cpp2IL lists every method with its address ([Address(... Offset = "0x...")]
in its "diffable C#" output with the attribute injector). Offset is the method's position in the libil2cpp.so FILE
(not the virtual address: those differ, e.g. by 0x4000 in Stremio VR's library), which is what a byte patch needs.

Results are cached per game build (its global-metadata.dat), so a rebuild of the same game doesn't run Cpp2IL again,
and neither does the next patch of the same build (an earlier one has already changed libil2cpp.so's bytes, never its
layout)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from ..core.cache import cache_dir

_ADDRESS = re.compile(r'Offset = "0x([0-9A-Fa-f]+)", Length = "0x([0-9A-Fa-f]+)"')


def parse_methods(cs_text: str, names: list[str]) -> dict[str, tuple[int, int]]:
    """{method name: (file offset, length)} for the named methods of one Cpp2IL diffable-C# class file. A name
    "field:<f>" gives the instance field's offset instead: (offset, 0)."""
    lines = cs_text.splitlines()
    found: dict[str, tuple[int, int]] = {}
    for name in [n for n in names if n.startswith("field:")]:
        m = re.search(rf"\s{re.escape(name[6:])};\s*//Field offset: 0x([0-9A-Fa-f]+)", cs_text)
        if m:
            found[name] = (int(m.group(1), 16), 0)
    names = [n for n in names if not n.startswith("field:")]
    for i, line in enumerate(lines):
        for name in names:
            if name in found or not re.search(rf"\s{re.escape(name)}\s*\(", line) or "Token" in line:
                continue
            for back in range(i - 1, max(i - 4, -1), -1):
                m = _ADDRESS.search(lines[back])
                if m:
                    found[name] = (int(m.group(1), 16), int(m.group(2), 16))
                    break
    return found


def _last_error(output: str) -> str:
    """The exception line of Cpp2IL's output (its stack trace isn't useful to a user)."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    msg = next((ln for ln in reversed(lines) if "Exception" in ln and not ln.startswith("at ")), None)
    msg = msg or next((ln for ln in reversed(lines) if not ln.startswith("at ")), None)
    return (msg or "no output")[:300]


def _runtime_failure(error: str) -> bool:
    """.NET itself stopped before Cpp2IL read anything ("Process terminated", e.g. no libicu on the PC): not a fact
    about the game, so it isn't cached. Older FramePort versions cached it as the stack line it ends with."""
    return "Process terminated" in error or error.endswith("at Cpp2IL.Program.Main(System.String[])")


def _plain_version(version: str) -> str:
    """6000.2.7f2 -> 6000.2.7 (what Cpp2IL's --force-unity-version takes)."""
    m = re.match(r"\d+\.\d+\.\d+", version)
    return m.group(0) if m else version


def _cache_file(lib: bytes, metadata: bytes) -> Path:
    key = hashlib.sha256(metadata + len(lib).to_bytes(8, "little")).hexdigest()[:32]
    return cache_dir() / "il2cpp" / (key + ".json")


def find_methods(lib: bytes, metadata: bytes, unity_version: str, wanted: dict[str, list[str]],
                 cpp2il: Path | None = None, timeout: float = 900) -> dict[str, dict[str, tuple[int, int]]]:
    """wanted: {class file (relative to Cpp2IL's DiffableCs folder, e.g. "Unity.TextMeshPro/TMPro/TMP_InputField.cs"):
    [method names]} -> {class file: {method: (file offset, length)}}; classes or methods not in the game are left
    out."""
    cache_file = _cache_file(lib, metadata)
    from ..tools import cpp2il as tool

    nightly = False  # Cpp2IL's development build: for metadata newer than its release reads (Unity 6.5+)
    try:
        cached = json.loads(cache_file.read_text())
        if cached.get("error") and _runtime_failure(cached["error"]):
            pass  # the PC's .NET failed last time, not the game: run again
        elif cached.get("error"):  # Cpp2IL can't read this build (e.g. an old Unity): don't run it again per patch
            if cpp2il is not None or cached.get("nightly") or not tool.unsupported_metadata(cached["error"]):
                raise RuntimeError(cached["error"])
            nightly = True  # only the release couldn't: the development build gets one try
        elif all(cls in cached.get("classes", {}) or cls in cached.get("missing", []) for cls in wanted):
            return {cls: {m: tuple(v) for m, v in cached["classes"][cls].items() if m in names}
                    for cls, names in wanted.items() if cls in cached.get("classes", {})}
    except (OSError, ValueError, KeyError):
        pass
    explicit = cpp2il is not None
    exe = cpp2il or (tool.ensure_nightly() if nightly else tool.ensure())
    while True:
        try:
            out, missing = _run(exe, lib, metadata, unity_version, wanted, timeout)
            break
        except _Unreadable as exc:
            error = "Cpp2IL couldn't read this game's code: " + str(exc)
            if _runtime_failure(error):
                raise RuntimeError("Cpp2IL couldn't start on this PC: " + str(exc)) from None
            if not explicit and not nightly and tool.unsupported_metadata(error):
                nightly, exe = True, tool.ensure_nightly()
                continue
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"unity_version": unity_version, "error": error, "nightly": nightly}))
            raise RuntimeError(error) from None
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"unity_version": unity_version, "missing": missing, "nightly": nightly,
                                      "classes": {c: {m: list(v) for m, v in ms.items()} for c, ms in out.items()}}))
    return out


class _Unreadable(Exception):
    """Cpp2IL wrote no output; the message is its last exception line."""


def _run(cpp2il: Path, lib: bytes, metadata: bytes, unity_version: str, wanted: dict[str, list[str]],
         timeout: float) -> tuple[dict, list]:
    with tempfile.TemporaryDirectory(prefix="frameport-il2cpp-") as tmp:
        t = Path(tmp)
        (t / "libil2cpp.so").write_bytes(lib)
        (t / "global-metadata.dat").write_bytes(metadata)
        proc = subprocess.run([str(cpp2il), "--force-binary-path", str(t / "libil2cpp.so"),
                               "--force-metadata-path", str(t / "global-metadata.dat"),
                               "--force-unity-version", _plain_version(unity_version),
                               "--use-processor", "attributeinjector", "--output-as", "diffable-cs",
                               "--output-to", str(t / "out")], capture_output=True, text=True, errors="replace",
                              timeout=timeout, cwd=tmp,
                              # .NET aborts at start without libicu (fresh WSL/Linux installs); Cpp2IL needs no
                              # culture data
                              env={**os.environ, "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1"})
        root = t / "out" / "DiffableCs"
        if not root.is_dir():
            raise _Unreadable(_last_error(proc.stdout + proc.stderr))
        out, missing = {}, []
        for cls, names in wanted.items():
            f = root / cls
            if f.exists():
                out[cls] = parse_methods(f.read_text(encoding="utf-8", errors="replace"), names)
            else:
                missing.append(cls)
    return out, missing
