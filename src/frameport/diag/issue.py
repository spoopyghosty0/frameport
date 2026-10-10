"""Prefilled GitHub "new issue" links (issue forms in .github/ISSUE_TEMPLATE; field ids match the query keys).

No token: the browser opens the form, the user reviews it, drags the diagnostics zip in (GitHub has no API for issue
attachments) and submits. Everything is redacted; URLs stay under ~7.5k characters (longer ones get cut by
browsers/GitHub), trimming free text first and never the recipe.

Some browsers open the form with the title but every field empty (GitHub #167: Flatpak browsers on Linux), so each
link also comes as the Markdown body GitHub's issue forms write (`### <label>` sections, the recipe as a ```yaml
block): the GUI copies it to the clipboard and offers a plain issue (`?title=…&body=…`) with that body, which
scripts/catalog_from_issue.py reads the same way.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple
from urllib.parse import urlencode

from .. import REPO_URL as REPO
from . import redact

MAX_URL = 7500

# field id → (label, render) as in .github/ISSUE_TEMPLATE/*.yml (tests check they match)
WORKING_CONFIG_FIELDS = {"game": ("Game", None), "result": ("Result", None), "recipe": ("Recipe", "yaml"),
                         "environment": ("Environment", None), "notes": ("Notes", None)}
WORKING_CONFIG_CONFIRM = ("Confirmation", "I played the game in the headset with this recipe.")
PROBLEM_FIELDS = {"game": ("Game", None), "description": ("What happens?", None), "findings": ("Launch test", None),
                  "recipe": ("Recipe", "yaml"), "environment": ("Environment", None), "logs": ("Diagnostics", None)}
NO_RESPONSE = "_No response_"  # what GitHub writes for an empty field


class IssueLinks(NamedTuple):
    """The issue form link, a plain issue link with the same content, and that content as Markdown."""
    form: str
    plain: str
    body: str


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


def _fit(fields: dict[str, str], trim: list[str], build: Callable[[dict[str, str]], str]) -> str:
    """build(fields) with the keys in `trim` shortened (in that order) until the URL fits."""
    fields = {k: v for k, v in fields.items() if v}
    while True:
        url = build(fields)
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


def _url(template: str, fields: dict[str, str], trim: list[str]) -> str:
    """The issue form link: fields in order (query keys = the form's field ids)."""
    return _fit(fields, trim, lambda f: f"{REPO}/issues/new?" + urlencode({"template": template, **f}))


def markdown_body(fields: dict[str, str], layout: dict[str, tuple[str, str | None]],
                  confirm: tuple[str, str] | None = None) -> str:
    """The body GitHub writes for a submitted issue form: one `### <label>` section per field (empty ones say
    "_No response_", `render` fields in a code block), a checked confirmation box last."""
    parts = []
    for key, (label, render) in layout.items():
        value = (fields.get(key) or "").strip("\n")
        if not value.strip():
            value = NO_RESPONSE
        elif render:
            value = f"```{render}\n{value}\n```"
        parts.append(f"### {label}\n\n{value}")
    if confirm:
        parts.append(f"### {confirm[0]}\n\n- [X] {confirm[1]}")
    return "\n\n".join(parts) + "\n"


def plain_url(title: str, body: str, labels: str = "") -> str:
    """A plain "new issue" link (no form) with title and body; labels only apply for users who may set them."""
    q = {"title": title, "body": body}
    if labels:
        q["labels"] = labels
    return f"{REPO}/issues/new?" + urlencode(q)


def _links(template: str, fields: dict[str, str], trim: list[str], layout: dict, confirm=None) -> IssueLinks:
    """The form link, the plain link (trimmed the same way to fit MAX_URL) and the untrimmed Markdown body."""
    title, labels = fields.get("title", ""), fields.get("labels", "")
    content = {k: v for k, v in fields.items() if k not in ("title", "labels")}
    plain = _fit(content, trim, lambda f: plain_url(title, markdown_body(f, layout, confirm), labels))
    return IssueLinks(_url(template, fields, trim), plain, markdown_body(content, layout, confirm))


def _working_config_fields(g: dict, recipe_yaml: str, status: str, notes: str, env: dict,
                           red: redact.Redactor | None) -> dict[str, str]:
    red = red or redact.Redactor()
    title = g.get("title") or g["package"]
    kind = {"rift": "Oculus Rift (PC VR)", "linux": "Linux app (arm64)"}.get(g.get("kind"), "Meta Quest")
    a = g.get("analysis") or {}
    game = f"{title} — {g['package']} ({kind}, version {a.get('version') or '?'}, {a.get('engine') or '?'} / " \
           f"{a.get('xr') or '?'})"
    last = g.get("last_test") or {}
    result = f"{status}" + (f"; last launch test: {last.get('state')} ({last.get('verdict')}), furthest: "
                            f"{last.get('milestone') or '-'}" if last else "")
    return {
        "title": red.text(f"[Working recipe] {title}"),
        "labels": "working-config",
        "game": red.text(game),
        "result": red.text(result),
        "recipe": red.text(recipe_yaml),
        "environment": red.text(_env_text(env)),
        "notes": red.text(notes or ""),
    }


WORKING_CONFIG_TRIM = ["notes", "environment", "result"]


def working_config_links(g: dict, recipe_yaml: str, status: str, notes: str, env: dict,
                         red: redact.Redactor | None = None) -> IssueLinks:
    return _links("working-config.yml", _working_config_fields(g, recipe_yaml, status, notes, env, red),
                  WORKING_CONFIG_TRIM, WORKING_CONFIG_FIELDS, WORKING_CONFIG_CONFIRM)


def working_config_url(g: dict, recipe_yaml: str, status: str, notes: str, env: dict,
                       red: redact.Redactor | None = None) -> str:
    return _url("working-config.yml", _working_config_fields(g, recipe_yaml, status, notes, env, red),
                WORKING_CONFIG_TRIM)


def working_config_body(g: dict, recipe_yaml: str, status: str, notes: str, env: dict,
                        red: redact.Redactor | None = None) -> str:
    """The Markdown body of a "Working recipe" issue, as the issue form writes it."""
    return markdown_body(_working_config_fields(g, recipe_yaml, status, notes, env, red), WORKING_CONFIG_FIELDS,
                         WORKING_CONFIG_CONFIRM)


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


def _problem_fields(g: dict | None, description: str, env: dict, bundle_name: str | None, recipe_yaml: str,
                    red: redact.Redactor | None) -> dict[str, str]:
    red = red or redact.Redactor()
    title = (g.get("title") or g["package"]) if g else "FramePort"
    game = ""
    if g:
        a = g.get("analysis") or {}
        game = f"{title} — {g['package']} (version {a.get('version') or '?'}, {a.get('engine') or '?'} / " \
               f"{a.get('xr') or '?'})"
    logs = f"⬇ Drag and drop {bundle_name} (the diagnostics zip FramePort just saved) into this box." \
        if bundle_name else ""
    return {
        "title": red.text(f"[Problem] {title}"),
        "labels": "bug",
        "game": red.text(game),
        "description": red.text(description or ""),
        "findings": red.text(findings_text(g.get("last_test") if g else None)),
        "recipe": red.text(recipe_yaml),
        "environment": red.text(_env_text(env)),
        "logs": logs,
    }


PROBLEM_TRIM = ["findings", "description", "recipe", "environment"]


def problem_links(g: dict | None, description: str, env: dict, bundle_name: str | None,
                  recipe_yaml: str = "", red: redact.Redactor | None = None) -> IssueLinks:
    return _links("bug-report.yml", _problem_fields(g, description, env, bundle_name, recipe_yaml, red),
                  PROBLEM_TRIM, PROBLEM_FIELDS)


def problem_url(g: dict | None, description: str, env: dict, bundle_name: str | None,
                recipe_yaml: str = "", red: redact.Redactor | None = None) -> str:
    return _url("bug-report.yml", _problem_fields(g, description, env, bundle_name, recipe_yaml, red), PROBLEM_TRIM)
