"""One bounded Rift-on-Frame run. Uses the paired Frame and prints exactly one JSON summary on success.

    FRAMEPORT_EXPERIMENTAL_FEX=1 uv run python scripts/rift_fex_bench.py oculus-first-contact --seconds 120

Settings are changed separately with `frameport fex settings APP key=value`; this script never starts a sweep.
The Frame owns the timeout/cleanup, so closing this PC process does not leave an unbounded game running.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("app", nargs="?", default="oculus-first-contact")
    p.add_argument("--seconds", type=int, default=120, choices=range(1, 121), metavar="1..120")
    p.add_argument("--frame", help="paired Frame as user@host")
    args = p.parse_args(argv)
    # Reuse the CLI's feature gate, paired-host verification and JSON output. No second SSH implementation.
    from frameport.cli import fex_bench
    fex_bench(args.app, args.seconds, args.frame)
    return 0


if __name__ == "__main__":
    sys.exit(main())
