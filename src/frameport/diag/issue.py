"""Prefilled GitHub "new issue" links (issue forms in .github/ISSUE_TEMPLATE; field ids match the query keys).

No token: the browser opens the form, the user reviews it, drags the diagnostics zip in (GitHub has no API for issue
attachments) and submits. Everything is redacted; URLs stay under ~7.5k characters (longer ones get cut by
browsers/GitHub), trimming free text first and never the recipe.
"""
from __future__ import annotations

from urllib.parse import urlencode

from .. import REPO_URL as REPO
from . import redact

MAX_URL = 7500


def _env_text(env: dict) -> str:
    lines = [f"- FramePort {env.get('app')} (agent {env.get('agent_bundled')}), {env.get('os')} {env.get('machine')}"
             + (" (WSL)" if env.get("wsl") else "")]
    if env.get("tools"):
        lines.append("- tools: " + ", ".join(f"{k} {v}" for k, v in env["tools"].items()))
    if env.get("revive") and "revive" not in (env.get("tools") or {}):
        lines.append(f"- Revive {env['revive']}")
    fr = env.get("frame")
    if fr:
        lines.append(f"- Frame: {fr.get('os')} {fr.get('os_version')} build {fr.get('build_id')}, "
                     f"agent {fr.get('agent_version')}" + (f", Proton: {fr['proton']}" if fr.get("proton") else ""))
    return "\n".join(lines)


CUT = "\n[... cut; the full text is in the diagnostics zip]"


def _url(template: str, fields: dict[str, str], trim: list[str]) -> str:
    """fields in order; the keys in `trim` are shortened (in that order) until the URL fits."""
    fields = {k: v for k, v in fields.items() if v}
    while True:
        url = f"{REPO}/issues/new?" + urlencode({"template": template, **fields})
        over = len(url) - MAX_URL
        if over <= 0:
            return url
        key = next((k for k in trim if k in fields), None)
        if key is None:
            return url  # only untrimmable fields left (e.g. a huge recipe): let GitHub cut it
        text = fields[key].removesuffix(CUT)
        # encoded text is 1-3x longer than raw text: cutting `over` raw characters (+ slack) always makes progress
        keep = len(text) - over - 100
        if keep < 40:
            fields.pop(key)
        else:
            fields[key] = text[:keep].rstrip() + CUT


def working_config_url(g: dict, recipe_yaml: str, status: str, notes: str, env: dict,
                       red: redact.Redactor | None = None) -> str:
    red = red or redact.Redactor()
    title = g.get("title") or g["package"]
    kind = {"rift": "Oculus Rift (PC VR)", "linux": "Linux app (arm64)"}.get(g.get("kind"), "Meta Quest")
    a = g.get("analysis") or {}
    game = f"{title} — {g['package']} ({kind}, version {a.get('version') or '?'}, {a.get('engine') or '?'} / " \
           f"{a.get('xr') or '?'})"
    last = g.get("last_test") or {}
    result = f"{status}" + (f"; last launch test: {last.get('state')} ({last.get('verdict')}), furthest: "
                            f"{last.get('milestone') or '-'}" if last else "")
    return _url("working-config.yml", {
        "title": red.text(f"[Working recipe] {title}"),
        "labels": "working-config",
        "game": red.text(game),
        "result": red.text(result),
        "recipe": red.text(recipe_yaml),
        "environment": red.text(_env_text(env)),
        "notes": red.text(notes or ""),
    }, trim=["notes", "environment", "result"])


def findings_text(summary: dict | None, limit: int = 8) -> str:
    if not summary:
        return ""
    lines = [f"Launch test: {summary.get('state')} ({summary.get('verdict')}), furthest milestone: "
             f"{summary.get('milestone') or '-'}" + (f", {summary['fps']:.0f} fps" if summary.get("fps") else "")]
    for f in (summary.get("findings") or [])[:limit]:
        lines.append(f"- {f.get('severity')} `{f.get('id')}`: {f.get('diagnosis')}")
        if f.get("evidence"):
            lines.append(f"  `{str(f['evidence'])[:200]}`")
    if summary.get("suggestions"):
        lines.append("Suggested patches: " + ", ".join(summary["suggestions"]))
    return "\n".join(lines)


def problem_url(g: dict | None, description: str, env: dict, bundle_name: str | None,
                recipe_yaml: str = "", red: redact.Redactor | None = None) -> str:
    red = red or redact.Redactor()
    title = (g.get("title") or g["package"]) if g else "FramePort"
    game = ""
    if g:
        a = g.get("analysis") or {}
        game = f"{title} — {g['package']} (version {a.get('version') or '?'}, {a.get('engine') or '?'} / " \
               f"{a.get('xr') or '?'})"
    logs = f"⬇ Drag and drop {bundle_name} (the diagnostics zip FramePort just saved) into this box." \
        if bundle_name else ""
    return _url("bug-report.yml", {
        "title": red.text(f"[Problem] {title}"),
        "labels": "bug",
        "game": red.text(game),
        "description": red.text(description or ""),
        "findings": red.text(findings_text(g.get("last_test") if g else None)),
        "recipe": red.text(recipe_yaml),
        "environment": red.text(_env_text(env)),
        "logs": logs,
    }, trim=["findings", "description", "recipe", "environment"])
