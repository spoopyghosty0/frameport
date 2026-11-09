"""Scrub personal data from everything that leaves this PC (diagnostics bundles, GitHub issue text).

Literal values known at runtime (the data dir, home dirs, Frame hosts, Steam account ids, user names) are replaced
exactly; patterns catch the rest (IP/MAC addresses, SteamID64s, e-mail addresses, home paths of any user).
Generic names that carry debug value and no identity (the Frame's `steamos` user, `root`) are kept.
"""
from __future__ import annotations

import getpass
import json
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

KEEP_USERS = {"steamos", "steamuser", "root", "user", "public", "default", "deck"}  # steamuser = Proton prefix user
KEEP_WORDS = KEEP_USERS | {"localhost", "steam", "frame", "steamframe", "windows", "linux"}

MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
IPV4 = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w.]*\d)")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){1,7}(?::|(?::[0-9A-Fa-f]{1,4}){1,7}|[0-9A-Fa-f]{1,4})(?![\w:])")
STEAMID64 = re.compile(r"\b7656119\d{10}\b")
USERDATA = re.compile(r"(userdata[\\/]+)(\d+)")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@(?:[A-Za-z][A-Za-z0-9-]*\.)+"
                   r"(?!(?:txt|log|gz|xz|zip|tmp|xml|json|png|jpg|dat|bin|so|dll|exe|conf)\b)[A-Za-z]{2,}\b")
# home folders of any user: /home/<u>, /Users/<u>, C:\Users\<u> (also JSON-escaped \\), /mnt/c/Users/<u>
HOME_PATHS = [
    re.compile(r"(/home/)([^/\s'\"\\:]+)"),
    re.compile(r"(/Users/)([^/\s'\"\\:]+)"),
    re.compile(r"(/mnt/[A-Za-z]/Users/)([^/\s'\"\\:]+)", re.I),
    re.compile(r"([A-Za-z]:\\{1,2}Users\\{1,2})([^\\/\s'\":]+)", re.I),
    re.compile(r"([A-Za-z]:/Users/)([^/\s'\":]+)", re.I),
]


def _ipv4_ok(m: re.Match) -> bool:
    """True if this looks like a real, identifying IPv4 address (not loopback, a netmask or a version number)."""
    octets = [int(x) for x in m.groups()]
    if any(o > 255 for o in octets) or any(len(x) > 1 and x.startswith("0") for x in m.groups()):
        return False
    first = octets[0]
    return first >= 10 and first != 127 and first != 255 and octets != [0, 0, 0, 0]


def _ipv6_ok(s: str) -> bool:
    if s in ("::", "::1") or s.count(":") < 2:
        return False
    groups = [g for g in s.split(":") if g]
    # needs a compressed "::" or all 8 groups, and some hex letter or "::" (timestamps are 12:34:56)
    if "::" not in s and len(groups) != 8:
        return False
    return "::" in s or any(re.search(r"[a-fA-F]", g) for g in groups)


class Redactor:
    def __init__(self, known: dict[str, list[str]] | None = None):
        self.counts: Counter = Counter()
        self.literals: list[tuple[str, str]] = []  # (value, placeholder), longest first
        for kind, values in (known or {}).items():
            for v in values or []:
                self.add(kind, v)

    def add(self, kind: str, value) -> None:
        v = str(value or "").strip().rstrip("/\\")
        if not v or v.lower() in KEEP_WORDS or v in ("/", "~"):
            return
        if kind == "steam-id" and (not v.isdigit() or len(v) < 6):
            return
        if kind in ("user", "host") and len(v) < 3:
            return
        variants = {v}
        if kind in ("data", "home"):
            variants |= {v.replace("\\", "/"), v.replace("/", "\\"), v.replace("\\", "\\\\")}
        for x in variants:
            if (x, f"<{kind}>") not in self.literals:
                self.literals.append((x, f"<{kind}>"))
        self.literals.sort(key=lambda p: -len(p[0]))

    def _sub(self, kind: str, pattern: re.Pattern, s: str, repl) -> str:
        def f(m):
            out = repl(m)
            if out != m.group(0):
                self.counts[kind] += 1
            return out
        return pattern.sub(f, s)

    def text(self, s: str) -> str:
        if not s:
            return s
        for value, ph in self.literals:
            if value in s:
                kind = ph.strip("<>")
                if kind in ("user", "host", "steam-id"):  # whole words only (short values)
                    s, n = re.subn(r"(?<![\w.-])" + re.escape(value) + r"(?![\w-])", ph, s)
                else:
                    n = s.count(value)
                    s = s.replace(value, ph)
                self.counts[kind] += n
        s = self._sub("email", EMAIL, s, lambda m: "<email>")
        s = self._sub("mac", MAC, s, lambda m: "<mac>")
        s = self._sub("ip", IPV4, s, lambda m: "<ip>" if _ipv4_ok(m) else m.group(0))
        s = self._sub("ip", IPV6, s, lambda m: "<ip>" if _ipv6_ok(m.group(0)) else m.group(0))
        s = self._sub("steam-id", STEAMID64, s, lambda m: "<steam-id>")
        s = self._sub("steam-id", USERDATA, s, lambda m: m.group(1) + "<steam-id>")
        for p in HOME_PATHS:
            s = self._sub("home", p, s,
                          lambda m: m.group(0) if m.group(2).lower() in KEEP_USERS or m.group(2).startswith("<")
                          else m.group(1) + "<user>")
        return s

    def obj(self, o):
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {self.text(str(k)) if isinstance(k, str) else k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o

    def json(self, o, **kw) -> str:
        return json.dumps(self.obj(o), indent=1, default=str, **kw)

    def summary(self) -> dict[str, int]:
        return dict(sorted(self.counts.items()))


def default(frame_info: dict | None = None) -> Redactor:
    """A Redactor that knows this PC's personal values (and the Frame's, from agent `info`)."""
    from ..core.paths import user_data_dir

    known: dict[str, list[str]] = {"data": [str(user_data_dir())], "home": [str(Path.home())], "user": [],
                                   "host": [], "steam-id": []}
    for name in ("USER", "USERNAME", "LOGNAME"):
        known["user"].append(os.environ.get(name, ""))
    try:
        known["user"].append(getpass.getuser())
    except Exception:  # noqa: BLE001
        pass
    for name in ("USERPROFILE", "HOMEPATH"):
        if os.environ.get(name):
            known["home"].append(os.environ[name])
    try:
        known["host"].append(os.uname().nodename)
    except AttributeError:
        known["host"].append(os.environ.get("COMPUTERNAME", ""))
    try:
        from ..frame.connection import saved_targets

        for t in saved_targets():
            known["host"] += [t.host, t.name]
    except Exception:  # noqa: BLE001
        pass
    if frame_info:
        known["host"].append(frame_info.get("hostname", ""))
        known["steam-id"] += [str(u) for u in frame_info.get("steam_users") or []]
    known["steam-id"].append(pc_steam_user() or "")
    known["secret"] = secret_values()
    return Redactor(known)


# settings that hold secrets (the user's own API keys): never in a diagnostics bundle or an issue text
SECRET_SETTINGS = ("art.steamgriddb_key",)


def secret_values() -> list[str]:
    """The secrets set in this PC's settings (replaced by "<secret>" wherever they appear)."""
    try:
        from ..core import library

        settings = library.load().get("settings", {}) or {}
    except Exception:  # noqa: BLE001
        return []
    return [str(settings[k]).strip() for k in SECRET_SETTINGS if str(settings.get(k) or "").strip()]


def scrub_settings(settings: dict) -> dict:
    """A copy of the settings with secret values replaced by "<secret>" (an empty one stays empty: whether a key is
    set has debug value, the key itself never)."""
    out = dict(settings or {})
    for k in SECRET_SETTINGS:
        if out.get(k):
            out[k] = "<secret>"
    return out


@lru_cache(maxsize=1)
def pc_steam_user() -> str | None:
    """This PC's Steam account id (Windows Steam; a few seconds under WSL, so cached)."""
    try:
        from ..core import winhost

        root = winhost.steam_root() if winhost.available() else None
        uid = winhost.steam_user(root) if root else None
        return str(uid) if uid else None
    except Exception:  # noqa: BLE001
        return None
