"""Turn a "Working recipe" issue (.github/ISSUE_TEMPLATE/working-config.yml) into catalog/games/<pkg>.yaml.

Used by .github/workflows/catalog-from-issue.yml after a maintainer labels the issue `catalog-accepted`; can also be
run by hand:  gh issue view 12 --json body -q .body | python scripts/catalog_from_issue.py --issue 12
The issue text is untrusted: only the YAML block of the "Recipe" section is read, parsed with yaml.safe_load, reduced
to known catalog fields and checked (package id, status, types) before anything is written.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from frameport.recommend.catalog import CatalogEntry, to_yaml  # noqa: E402

PKG_RE = re.compile(r"^(rift\.[a-z0-9_]{1,80}|[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+){1,8})$")
STATUSES = {"works", "issues", "unsupported", "unknown"}
LIST_FIELDS = {"overport_extra", "overport_remove", "alt_overport", "frame", "frame_remove", "pcvr", "pcvr_remove",
               "device"}
# FramePort's own patch ids must exist (OVRPort's are listed by its CLI at runtime, so not checked here)
OWN_PATCH_FIELDS = ("frame", "frame_remove", "pcvr", "pcvr_remove", "device")
DICT_FIELDS = {"adapter", "device_files", "lepton_env", "proton_env", "verified"}
ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")


class Invalid(ValueError):
    pass


def section(body: str, heading: str) -> str:
    m = re.search(r"^###\s+" + re.escape(heading) + r"\s*$(.*?)(?=^###\s|\Z)", body, re.M | re.S)
    if not m:
        raise Invalid(f"no '{heading}' section")
    return m.group(1).strip()


def recipe_from_body(body: str) -> dict:
    text = section(body, "Recipe")
    m = re.search(r"```(?:ya?ml)?\s*\n(.*?)```", text, re.S)
    raw = m.group(1) if m else text
    try:
        d = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise Invalid(f"recipe isn't valid YAML: {exc}") from None
    if not isinstance(d, dict):
        raise Invalid("recipe must be a YAML mapping")
    return d


def validate(d: dict) -> CatalogEntry:
    pkg = d.get("package")
    if not isinstance(pkg, str) or not PKG_RE.match(pkg):
        raise Invalid(f"bad package id {pkg!r}")
    if not isinstance(d.get("title"), str) or not d["title"].strip():
        raise Invalid("missing title")
    if d.get("status", "unknown") not in STATUSES:
        raise Invalid(f"bad status {d.get('status')!r}")
    unknown = sorted(set(d) - set(CatalogEntry.__dataclass_fields__) - {"origin"})
    if unknown:
        raise Invalid(f"unknown fields: {', '.join(unknown)}")
    for k in LIST_FIELDS & set(d):
        if not isinstance(d[k], list) or not all(isinstance(x, str) and ID_RE.match(x) for x in d[k]):
            raise Invalid(f"{k} must be a list of patch ids")
    from frameport.patches import base

    base.load_all()
    for k in OWN_PATCH_FIELDS:
        unknown_ids = [x for x in d.get(k) or [] if x not in base.REGISTRY]
        if unknown_ids:
            raise Invalid(f"{k}: unknown patch id(s) {', '.join(unknown_ids)}")
    for k in DICT_FIELDS & set(d):
        if not isinstance(d[k], dict):
            raise Invalid(f"{k} must be a mapping")
    for k in ("device_files",):
        for path in (d.get(k) or {}):
            if not isinstance(path, str) or path.startswith("/") or ".." in Path(path).parts:
                raise Invalid(f"{k}: bad path {path!r}")
    d = {k: v for k, v in d.items() if k != "origin"}
    return CatalogEntry.from_dict(d, "bundled")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("body", nargs="?", help="file with the issue body (default: stdin)")
    ap.add_argument("--issue", type=int, help="issue number (recorded in verified.issue)")
    ap.add_argument("--out", type=Path, default=ROOT / "catalog" / "games")
    a = ap.parse_args(argv)
    body = Path(a.body).read_text(encoding="utf-8") if a.body else sys.stdin.read()
    try:
        entry = validate(recipe_from_body(body))
    except Invalid as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if a.issue:
        entry.verified = {**entry.verified, "issue": a.issue}
    path = a.out / f"{entry.package}.yaml"
    existed = path.exists()
    path.write_text(to_yaml(entry), encoding="utf-8")
    print(f"{'updated' if existed else 'added'} {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    print(f"package={entry.package}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
