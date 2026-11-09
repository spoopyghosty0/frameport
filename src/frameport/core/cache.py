"""Dynamic-first data fetching with an on-disk cache and bundled fallbacks.

Rule used throughout FramePort: fetch live data when online, cache it, and fall back to the last cached copy (or a
bundled default) when offline.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from .. import REPO_URL, __version__
from .paths import user_data_dir, write_atomic

USER_AGENT = f"FramePort/{__version__} (+{REPO_URL})"
_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT


def cache_dir() -> Path:
    path = user_data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def http_get(url: str, timeout: float = 20, **kw) -> requests.Response:
    resp = _session.get(url, timeout=timeout, **kw)
    resp.raise_for_status()
    return resp


def cached_text(name: str, url: str, max_age: float = 86400, fallback: str | None = None) -> str | None:
    path = cache_dir() / name
    fresh = path.exists() and time.time() - path.stat().st_mtime < max_age
    if not fresh:
        try:
            text = http_get(url).text
            write_atomic(path, text)
            return text
        except Exception:
            pass
    if path.exists():
        return path.read_text(encoding="utf-8")
    return fallback


_MISSING = object()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _MISSING


def cached_json(name: str, url: str, max_age: float = 86400, fallback=None):
    """Like cached_text, but only JSON is kept: a service's outage page (OculusDB redirects to an HTML page that
    answers 200, GitHub #510) isn't cached over the last good answer, and one cached by an older version counts as
    stale."""
    path = cache_dir() / name
    cached = _read_json(path) if path.exists() else _MISSING
    if cached is _MISSING or time.time() - path.stat().st_mtime >= max_age:
        try:
            text = http_get(url).text
            value = json.loads(text)
        except Exception:  # noqa: BLE001 - offline, an error status or not JSON: the last good copy
            pass
        else:
            write_atomic(path, text)
            return value
    return fallback if cached is _MISSING else cached


def download(url: str, dest: Path, progress=None, expected_sha256: str | None = None,
             expected_sha1: str | None = None) -> Path:
    """Stream url to dest (atomic), verifying a checksum when given."""
    import hashlib

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    h256, h1 = hashlib.sha256(), hashlib.sha1()
    with _session.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(1 << 20):
                f.write(chunk)
                h256.update(chunk)
                h1.update(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    if expected_sha256 and h256.hexdigest() != expected_sha256.lower():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {url}")
    if expected_sha1 and h1.hexdigest() != expected_sha1.lower():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {url}")
    tmp.replace(dest)
    return dest
