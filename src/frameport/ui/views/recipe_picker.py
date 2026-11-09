"""'Use a recipe from the catalog…' (GitHub #508): search FramePort's catalog and use another entry's recipe for this
game, e.g. a repack whose package id (ru.camouflaj.manta) never matched the store build's entry (com.camouflaj.manta).
The search and the preview are pipeline.catalog_choices / preview_catalog_recipe; applying is
pipeline.apply_catalog_recipe (the recipe then follows that entry's updates)."""
from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from ... import pipeline
from ...core import library
from ...errors import explain
from ...i18n import tr
from ...patches import base
from ...recommend.catalog import CatalogEntry, package_core
from .. import components as C
from .. import theme as T

if TYPE_CHECKING:
    from ..app import FramePortApp

LIMIT = 40


def relation(entry: CatalogEntry, package: str, current: str) -> str | None:
    """How a catalog entry relates to the game: "current" (its recipe is in use), "own" (the game's own entry),
    "same" (the same package id core: another build or a repack of the game), else None."""
    if entry.package == current:
        return "current"
    if entry.package == package:
        return "own"
    if package_core(entry.package) == package_core(package):
        return "same"
    return None


def facts(entry: CatalogEntry) -> str:
    """The entry's package · engine · VR interface · tested version line."""
    parts = [entry.package, entry.engine, entry.xr if entry.xr not in ("?", "none") else ""]
    if entry.tested_version:
        parts.append(tr("tested with {version}").format(version=entry.tested_version))
    return " · ".join(p for p in parts if p)


def show_recipe_picker(app: FramePortApp, package: str, query: str | None = None, choice: str | None = None) -> None:
    """`query` = the search to start with (default: the game's name), `choice` = the entry to select (default: the
    recipe in use, else the best match when it is the same game under another package name)."""
    g = library.game(package)
    if not g or not g.get("analysis"):
        return
    title = g.get("title") or package
    current = library.recipe_pick(g)
    state: dict = {"choice": choice or current or None}
    rows: dict[str, ft.Container] = {}
    results = ft.Column(spacing=T.px(6), scroll=ft.ScrollMode.AUTO, height=T.px(280),
                        horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
    preview = ft.Column(spacing=T.S2)
    use_button = C.primary(tr("Use this recipe"), ft.Icons.CHECK_ROUNDED, disabled=True)

    def paint() -> None:
        for pkg, row in rows.items():
            C.selected_style(row, pkg == state["choice"], idle_bg=T.SURFACE)

    def row_for(entry: CatalogEntry) -> ft.Container:
        tags = [C.status_chip(entry.status)]
        rel = relation(entry, package, current)
        if rel == "current":
            tags.append(C.pill(tr("In use"), T.ACCENT, ft.Icons.CHECK_ROUNDED))
        elif rel == "own":
            tags.append(C.pill(tr("This game's own recipe"), T.OK))
        elif rel == "same":
            tags.append(C.pill(tr("Same game, other package name"), T.ACCENT, ft.Icons.AUTO_AWESOME_ROUNDED))
        box = C.hoverable(ft.Container(ft.Column([
            ft.Row([C.body(entry.title, T.TEXT, weight=ft.FontWeight.W_600), *tags], spacing=T.S2, wrap=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            C.meta(facts(entry)),
        ], spacing=T.px(4)), padding=ft.Padding(T.S3, T.S2, T.S3, T.S2), border_radius=T.RADIUS_SM,
            bgcolor=T.SURFACE, on_click=lambda e, p=entry.package: choose(p)))
        return box

    def fill(query: str | None) -> list[CatalogEntry]:
        rows.clear()
        found = [e for _, e in pipeline.catalog_choices(package, query, limit=LIMIT)]
        results.controls = [rows.setdefault(e.package, row_for(e)) for e in found] or \
            [C.meta(tr("No recipe in the catalog matches. Try another name or the package name."))]
        paint()
        return found

    def show_preview() -> None:
        pkg = state["choice"]
        preview.controls = []
        use_button.disabled = True
        if not pkg:
            return
        try:
            pick = pipeline.preview_catalog_recipe(package, pkg, g)
        except Exception as exc:  # noqa: BLE001 - e.g. the entry left the catalog meanwhile
            preview.controls = [C.callout(explain(exc), "error")]
            return
        if pick.blocked:
            preview.controls = [C.callout("\n".join(pick.warnings), "error")]
            return
        use_button.disabled = pkg == current
        if pick.warnings:
            preview.controls.append(C.callout("\n".join(pick.warnings), "warn"))
        chosen = [p for p in pick.recipe.patches if p in base.REGISTRY and not base.get(p).default_on]
        if chosen:
            names = ", ".join(C.patch_title(base.get(p).title) for p in chosen)
            preview.controls.append(C.body(tr("Turns on: {names}").format(names=names), T.TEXT_2))
        if pick.dropped:
            names = ", ".join(C.patch_title(base.get(p).title) if p in base.REGISTRY else p for p in pick.dropped)
            preview.controls.append(C.meta(tr("Left out, they can't apply to this game: {names}").format(names=names)))
        if pick.entry.notes:
            preview.controls.append(C.meta(pick.entry.notes, max_lines=3, overflow=ft.TextOverflow.ELLIPSIS))

    def choose(pkg: str) -> None:
        state["choice"] = pkg
        paint()
        show_preview()
        C.update(results, preview, use_button)

    def on_search(e) -> None:
        fill(e.control.value or "")
        C.update(results)

    def use(e) -> None:
        pkg = state["choice"]
        app.page.pop_dialog()

        def work():
            try:
                pick = pipeline.apply_catalog_recipe(package, pkg)
            except Exception as exc:  # noqa: BLE001
                app.toast(tr("Couldn't use that recipe: {error}").format(error=explain(exc)), error=True)
                return
            installed = app.frame_state == "connected" and \
                C.install_state(library.game(package) or {}, app.frame_info) in ("installed", "outdated")
            msg = tr("Recipe of {title} in use") if pick.entry and pkg != package else tr("Recipe reset")
            msg = msg.format(title=pick.entry.title if pick.entry else pkg)
            app.toast(msg + (tr(" — update the game on the Frame to use it") if installed else ""))
            app.refresh_view()
        app.run_bg(work)

    use_button.on_click = use
    box = C.search(value=title if query is None else query, hint_text=tr("Game name or package name"),
                   autofocus=True, on_change=on_search)
    found = fill(query)  # (default: the game's own name) the closest entries at once
    if not state["choice"] and found and relation(found[0], package, current) == "same":
        state["choice"] = found[0].package  # e.g. a repack of a game in the catalog
        paint()
    show_preview()
    lead = tr("Use the tested recipe of a game in FramePort's catalog for {title}, for example when your copy has "
              "another package name than the store's. It follows that recipe's later fixes; Reset to suggested "
              "recipe ends it.").format(title=title)
    app.page.show_dialog(C.dialog(
        tr("Use a recipe from the catalog"),
        ft.Column([C.body(lead), box, results, preview], spacing=T.S3, scroll=ft.ScrollMode.AUTO, tight=True),
        size="l",
        actions=[C.ghost(tr("Cancel"), on_click=lambda e: app.page.pop_dialog()), use_button]))
