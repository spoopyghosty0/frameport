#!/usr/bin/env python3
"""Build a native FramePort app bundle for the current OS.

    python scripts/package.py            # flet build (Flutter; best result, needs the Flutter SDK - flet installs it)
    python scripts/package.py --pyinstaller   # flet pack (PyInstaller one-folder app; no Flutter SDK needed)

Bundled data (catalog, prebuilt artifacts, Frame agent, bootstrap script) is copied into src/frameport/_data first,
which is where the installed app looks for it (see core/paths.py). Java/overport/apksigner are NOT bundled: the app
downloads and manages them on first run (Tools page), so bundles stay small and tools stay current.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src/frameport/_data"
TARGET = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[platform.system()]


def version() -> str:
    text = (ROOT / "src/frameport/_version.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def check_tag() -> None:
    """A release build (tag vX.Y.Z) must carry that version, or the app's update check would compare wrongly."""
    ref = os.environ.get("GITHUB_REF_NAME", "")
    if os.environ.get("GITHUB_REF_TYPE") == "tag" and ref.startswith("v") and ref[1:] != version():
        raise SystemExit(f"tag {ref} doesn't match src/frameport/_version.py ({version()}): bump it first")


def stage_data():
    shutil.rmtree(DATA, ignore_errors=True)
    for name in ("catalog", "artifacts", "agent", "bootstrap"):
        shutil.copytree(ROOT / name, DATA / name)
    # the Frame agent is uploaded as source; `flet build` turns every .py into .pyc (issue #2): keep a non-.py copy
    shutil.copy2(ROOT / "agent/frameport_agent.py", DATA / "agent/frameport_agent.py.txt")
    print(f"staged data in {DATA}")


def check_bundle(out: Path) -> None:
    """The finished bundle must contain the agent's source (the app uploads it to the Frame)."""
    found = [p for p in out.rglob("frameport_agent.py*") if p.suffix != ".pyc"]
    if not found:
        raise SystemExit(f"{out}: the Frame agent's source is missing from the bundle (only compiled?)")
    print("agent source in bundle:", ", ".join(str(p.relative_to(out)) for p in found))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pyinstaller", action="store_true")
    ap.add_argument("--keep-data", action="store_true", help="leave src/frameport/_data after building")
    args = ap.parse_args()
    check_tag()
    print(f"FramePort {version()}")
    stage_data()
    try:
        if args.pyinstaller:
            sep = ";" if TARGET == "windows" else ":"
            # --yes: a failed flet build's folder doesn't stop it
            cmd = ["flet", "pack", str(ROOT / "src/main.py"), "--name", "FramePort", "--product-name", "FramePort",
                   "--add-data", f"{DATA}{sep}frameport/_data",
                   # translations (data files PyInstaller doesn't pick up from the imports)
                   "--add-data", f"{ROOT / 'src/frameport/locales'}{sep}frameport/locales",
                   # the logo and the GUI's own glyphs (SVG)
                   "--add-data", f"{ROOT / 'src/frameport/ui/icons'}{sep}frameport/ui/icons",
                   "--icon", str(ROOT / "src/assets/icon.png"),
                   "--distpath", str(ROOT / "dist"), "--yes"]
            if TARGET == "linux":
                cmd.append("--onedir")  # a folder: the bundle check sees the agent source, the archive the app
        else:
            # --yes: install the Flutter SDK etc. without prompting; --no-rich-output: plain logs (CI, Windows consoles)
            cmd = ["flet", "build", TARGET, str(ROOT), "--project", "FramePort", "--product", "FramePort",
                   "--module-name", "main", "--output", str(ROOT / "dist" / TARGET), "--yes", "--no-rich-output"]
            if TARGET == "macos":  # flet's default bundled Python lacks prebuilt cryptography wheels for both Mac archs
                cmd += ["--python-version", "3.12", "--arch", "arm64"]  # Apple Silicon; x86_64 cross-build fails
        print(" ".join(cmd))
        rc = subprocess.call(cmd, cwd=ROOT)
        if rc == 0:
            check_bundle(ROOT / "dist" if args.pyinstaller else ROOT / "dist" / TARGET)
        return rc
    finally:
        if not args.keep_data:
            shutil.rmtree(DATA, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
