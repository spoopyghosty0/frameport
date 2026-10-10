"""Game settings (FrameBridge adapter settings) for non-technical users: only the settings that can matter for this
game, in plain words, with switches, sliders and choices instead of number fields; advanced ones on request.
Saved into the game's recipe (kept for reinstalls) and, when the game is on the Frame, applied there right away."""
from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from ... import pipeline
from ...core import library
from ...i18n import tr
from ...patches import base
from ...patches.settings import BASE_SETTINGS, GROUPS, HIDDEN, SETTINGS, UI, adapter_settings
from .. import components as C
from .. import theme as T

if TYPE_CHECKING:
    from ..app import FramePortApp

# the dialog's settings: numbers and switches (text settings such as vk_shader_fix are recipe data: saving the dialog
# keeps them as they are, as it keeps the HIDDEN ones)
SPECS = {key: (kind, default) for key, kind, default, _, _ in SETTINGS
         if kind in ("int", "float") and key not in HIDDEN}


def default(key: str):
    return BASE_SETTINGS.get(key, SPECS[key][1])


def _num(key: str, value):
    kind = SPECS[key][0]
    try:
        return float(value) if kind == "float" else int(float(value))
    except (TypeError, ValueError):
        return default(key)


def relevant(analysis, key: str, value) -> bool:
    """Shown when the setting can matter for this game (or is already changed from its default)."""
    if analysis is None:
        return True
    return base.get(f"adapter.{key}").applies(analysis) or value != default(key)


def recipe_values(g: dict) -> dict:
    vals = {key: default(key) for key in SPECS}
    vals.update({k: _num(k, v) for k, v in adapter_settings(library.recipe_from_dict(g["recipe"]).patches).items()
                 if k in SPECS})
    return vals


def save_to_recipe(package: str, values: dict) -> None:
    """Adapter patches = the settings that differ from their defaults (BASE_SETTINGS are written anyway)."""
    g = library.game(package)
    r = library.recipe_from_dict(g["recipe"])
    before = {pid: dict(v or {}) for pid, v in r.patches.items() if pid.startswith("adapter.")}
    for key in SPECS:
        pid = f"adapter.{key}"
        v = _num(key, values.get(key, default(key)))
        if v == default(key):
            r.patches.pop(pid, None)
            r.reasons.pop(pid, None)
        else:
            if (r.patches.get(pid) or {}).get("value") != v:
                r.reasons[pid] = tr("Set by you.")
            r.patches[pid] = {"value": v}
    after = {pid: dict(v or {}) for pid, v in r.patches.items() if pid.startswith("adapter.")}
    if after == before:  # saved without a change: the recipe keeps following FramePort's catalog (GitHub #10)
        return
    r.source = "user"
    pipeline.set_recipe(package, r)


def show_adapter_dialog(app: FramePortApp, package: str) -> None:
    g = library.game(package)
    if g is None:  # installed on the Frame but not in this library: read the values there first
        target = app.target
        if target is None:
            app.toast(tr("Connect your Frame first."), error=True)
            return

        def load():
            cur = target.set_settings(package, {})["settings"]  # no changes: returns the current values
            vals = {key: default(key) for key in SPECS}
            vals.update({k: _num(k, v) for k, v in cur.items() if k in SPECS})
            _open(app, package, None, vals)
        app.run_bg(load)
        return
    _open(app, package, g, recipe_values(g))


def _open(app: FramePortApp, package: str, g: dict | None, values: dict) -> None:
    analysis = library.analysis_from_dict(g["analysis"]) if g and g.get("analysis") else None
    suggested = recipe_values({"recipe": g["suggested"]}) if g and g.get("suggested") else \
        {key: default(key) for key in SPECS}
    values = dict(values)
    controls: dict[str, ft.Control] = {}   # key -> the input control
    rows: dict[str, ft.Container] = {}     # key -> its row (visibility)
    readouts: dict[str, ft.Text] = {}      # slider value labels
    advanced = {"on": any(UI[k]["level"] == "advanced" and values[k] != default(k) for k in SPECS)}

    def set_value(key, v):
        values[key] = _num(key, v)
        if key in readouts:
            readouts[key].value = UI[key]["control"][4].format(values[key])
            C.update(readouts[key])
        if any(UI[k].get("depends") == key for k in SPECS):
            apply_visibility()

    def control_for(key):
        kind, *spec = UI[key]["control"]
        v = values[key]
        if kind == "switch":
            return ft.Switch(value=bool(v), active_color=T.ACCENT,
                             on_change=lambda e: set_value(key, 1 if e.control.value else 0))
        if kind == "slider":
            lo, hi, step, fmt = spec
            readouts[key] = C.body(fmt.format(v), T.TEXT, weight=ft.FontWeight.W_600)
            return ft.Row([ft.Slider(min=lo, max=hi, divisions=round((hi - lo) / step), value=min(max(v, lo), hi),
                                     active_color=T.ACCENT, width=T.px(200),
                                     on_change=lambda e: set_value(key, round(e.control.value / step) * step)),
                           ft.Container(readouts[key], width=T.px(64))], spacing=0, tight=True)
        choices = list(spec[0])
        if v not in [c for c, _ in choices]:
            choices.append((v, str(v)))  # a value set elsewhere (CLI, catalog) stays selectable
        return C.dropdown(value=str(v), width=T.px(220),
                           options=[ft.DropdownOption(key=str(c), text=tr(label)) for c, label in choices],
                           on_select=lambda e: set_value(key, e.control.value))

    def row(key):
        ui = UI[key]
        label = tr(ui["label"]) if ui.get("label") else tr(base.get(f"adapter.{key}").title)
        help_text = tr(ui["help"]) if ui.get("help") else tr(base.get(f"adapter.{key}").description)
        controls[key] = control_for(key)
        rows[key] = ft.Container(ft.Row([
            ft.Column([C.body(label, T.TEXT, weight=ft.FontWeight.W_500), C.meta(help_text, T.TEXT_2)],
                      spacing=T.px(2), expand=True),
            controls[key]], spacing=T.S4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding(0, T.px(8), 0, T.px(8)))
        return rows[key]

    sections: list[tuple[str, ft.Control, list[str]]] = []
    body = []
    for group, title in GROUPS:
        keys = [k for k in UI if UI[k]["group"] == group and relevant(analysis, k, values[k])]
        if not keys:
            continue
        heading = C.body(tr(title), T.TEXT_2, weight=ft.FontWeight.W_600)
        section = ft.Column([heading, *[row(k) for k in keys]], spacing=0)
        sections.append((group, section, keys))
        body.append(section)

    def apply_visibility():
        for _group, section, keys in sections:
            shown = 0
            for k in keys:
                parent = UI[k].get("depends")
                vis = (advanced["on"] or UI[k]["level"] == "common") and (not parent or bool(values.get(parent)))
                rows[k].visible = vis
                shown += vis
            section.visible = bool(shown)
        C.update(*[s for _, s, _ in sections])

    def toggle_advanced(e):
        advanced["on"] = bool(e.control.value)
        apply_visibility()

    def reset(e):
        for k in list(values):
            values[k] = suggested.get(k, default(k))
        app.page.pop_dialog()
        _open(app, package, g, values)

    before = dict(values)

    def save(e):
        app.page.pop_dialog()
        if g is not None:
            save_to_recipe(package, values)
        reinstall = any(UI[k].get("reinstall") and values[k] != before[k] for k in SPECS)
        installed = app.target is not None and (g is None or C.install_state(g, app.frame_info) in
                                                ("installed", "outdated"))
        if not installed:
            app.toast(tr("Saved. They apply when the game is installed."))
            app.refresh_view()
            return
        target = app.target

        def push():
            target.set_settings(package, {k: values[k] for k in SPECS})
            app.toast(tr("Saved. Restart the game to use the new settings.") +
                      (tr(" Reinstall it for the controller models.") if reinstall else ""))
            app.refresh_view()
        app.run_bg(push)

    show_adv = any(UI[k]["level"] == "advanced" for _, _, keys in sections for k in keys)
    content = ft.Column([
        C.body(tr("Changes take effect the next time the game starts. Not sure? Leave a setting as it is."), T.TEXT_2),
        *body,
        *([C.switch(tr("Show advanced settings"), value=advanced["on"], on_change=toggle_advanced)]
          if show_adv else []),
    ], spacing=T.S3, scroll=ft.ScrollMode.AUTO, tight=True)
    title = tr("Game settings · {title}").format(title=app._title(package) if g else package)
    app.page.show_dialog(C.dialog(
        title, content, [C.ghost(tr("Reset to recommended"), ft.Icons.RESTART_ALT_ROUNDED, reset),
                         C.ghost(tr("Cancel"), on_click=lambda e: app.page.pop_dialog()),
                         C.primary(tr("Save"), on_click=save)], height=T.px(560)))
    apply_visibility()
