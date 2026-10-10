"""'Find artwork' — search the Meta/Oculus store (via OculusDB) and Steam, pick a result, fetch automatically, or
choose your own image files."""
from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from ... import pipeline
from ...artwork import sources, thumbs
from ...core import library
from ...i18n import tr
from .. import components as C
from .. import theme as T

if TYPE_CHECKING:
    from ..app import FramePortApp


def show_art_dialog(app: FramePortApp, package: str) -> None:
    g = library.game(package)
    term = C.search(value=g.get("title") or package, expand=True)
    results = ft.GridView(max_extent=T.px(190), child_aspect_ratio=0.8, spacing=T.S3, run_spacing=T.S3,
                          height=T.px(400))
    status = C.meta("")

    def pick(choice):
        app.page.pop_dialog()

        def work():
            if not sources.apply_choice(package, choice):
                app.toast(tr("Couldn't download artwork from that {source} result. Try another one.")
                          .format(source=choice['source']), error=True)
                return
            thumbs.prewarm(package)
            installed = C.install_state(library.game(package), app.frame_info) in ("installed", "outdated")
            # until it's sent, the game page reminds that the Frame's Steam library shows the old art
            library.upsert_game(package, art_source=choice["source"].lower(), **({"steam_art_stale": True}
                                                                                 if installed else {}))
            app.refresh_view()
            if installed:
                app.toast(tr("Artwork updated for {get}. The Frame's Steam library still shows the old art.")
                          .format(get=g.get('title')),
                          action=tr("Update art on Frame"), on_action=lambda e: app.update_steam_art(package))
            else:
                app.toast(tr("Artwork updated for {get}").format(get=g.get('title')))
        app.run_bg(work)

    def search(e=None):
        status.value = tr("Searching…")
        results.controls = []
        C.update(status, results)

        def work():
            found = sources.search(term.value.strip())
            results.controls = [ft.Container(ft.Column([
                ft.Container(C.art_fill(r["preview"], radius=T.RADIUS_SM, height=T.px(120)), height=T.px(120)),
                C.body(r["name"] or "", T.TEXT, size=T.T_META, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                C.meta(r["source"]),
            ], spacing=T.px(4)), padding=T.S2, border_radius=T.RADIUS_SM, bgcolor=T.SURFACE, ink=True,
                on_click=lambda e, r=r: pick(r)) for r in found]
            status.value = (tr("{len} results").format(len=len(found)) if found
                            else tr("Nothing found. Try a shorter or different name."))
            C.update(status, results)
        app.run_bg(work)

    def auto(e):
        app.page.pop_dialog()

        def work():
            found = pipeline.fetch_art(package)
            thumbs.prewarm(package)  # regenerate the thumbnails the cards/hero use, or the view shows the old art
            src = found.get("source")
            installed = C.install_state(library.game(package), app.frame_info) in ("installed", "outdated")
            if src and src != "none" and installed:
                library.upsert_game(package, steam_art_stale=True)
            app.refresh_view()
            if src and src != "none" and installed:
                app.toast(tr("Artwork updated ({src}). The Frame's Steam library still shows the old art.")
                          .format(src=src),
                          action=tr("Update art on Frame"), on_action=lambda e: app.update_steam_art(package))
            else:
                app.toast(tr("Artwork: {value}").format(value=src or 'none found'))
        app.run_bg(work)

    term.on_submit = search
    app.page.show_dialog(C.dialog(
        tr("Artwork for {get}").format(get=g.get('title')),
        ft.Column([
            ft.Row([term, C.secondary(tr("Search"), ft.Icons.SEARCH_ROUNDED, search)], spacing=T.S2),
            status, results,
        ], spacing=T.S3, tight=True),
        actions=[C.ghost(tr("Use your own artwork…"), ft.Icons.UPLOAD_FILE_OUTLINED,
                         lambda e: (app.page.pop_dialog(), show_custom_art_dialog(app, package))),
                 C.ghost(tr("Find automatically"), ft.Icons.AUTO_AWESOME_ROUNDED, auto),
                 C.ghost(tr("Close"), on_click=lambda e: app.page.pop_dialog())]))
    search()


def custom_slots() -> tuple[tuple[str, str, str], ...]:
    """(kind, label, the shape Steam shows it in) for each kind of artwork the user can replace."""
    return (
        ("portrait", tr("Cover"), tr("tall, 600 × 900")),
        ("landscape", tr("Wide cover"), tr("920 × 430")),
        ("hero", tr("Banner"), tr("wide, 1920 × 620")),
        ("logo", tr("Logo"), tr("transparent PNG")),
        ("icon", tr("Icon"), tr("square, 256 × 256")),
    )


def show_custom_art_dialog(app: FramePortApp, package: str) -> None:
    """'Your own images': choose an image file for each kind of artwork (for games whose art can't be found
    automatically). Missing shapes are composed from the others for Steam, as with store art."""
    g = library.game(package)
    grid = ft.Row(wrap=True, spacing=T.S3, run_spacing=T.S3)

    def changed(message: str) -> None:
        thumbs.prewarm(package)
        installed = C.install_state(library.game(package), app.frame_info) in ("installed", "outdated")
        library.upsert_game(package, art_source="custom", **({"steam_art_stale": True} if installed else {}))
        render()
        app.refresh_view()
        if installed:
            app.toast(tr("{message} The Frame's Steam library still shows the old art.").format(message=message),
                      action=tr("Update art on Frame"), on_action=lambda e: app.update_steam_art(package))
        else:
            app.toast(message)

    async def choose(kind: str, label: str) -> None:
        files = await ft.FilePicker().pick_files(dialog_title=tr("Image for the {label}").format(label=label),
                                                 allowed_extensions=["png", "jpg", "jpeg", "webp"])
        path = next((f.path for f in files or [] if f.path), None)
        if not path:
            return

        def work():
            try:
                sources.apply_custom(package, kind, path)
            except sources.ArtError as exc:
                app.toast(str(exc), error=True)
                return
            changed(tr("{label} updated for {title}.").format(label=label, title=g.get("title")))
        app.run_bg(work)

    def remove(kind: str, label: str) -> None:
        def work():
            sources.remove_custom(package, kind)
            changed(tr("{label} removed for {title}.").format(label=label, title=g.get("title")))
        app.run_bg(work)

    def slot(kind: str, label: str, shape: str) -> ft.Control:
        art = thumbs.url(package, (kind,), 400)
        if art and kind in ("logo", "icon"):  # shown whole (a logo is often very wide, an icon small)
            box = ft.Container(ft.Image(src=art, fit=ft.BoxFit.CONTAIN), height=T.px(110), padding=T.S2,
                               border_radius=T.RADIUS_SM, bgcolor=T.SURFACE_3, alignment=ft.Alignment.CENTER)
        else:
            box = (C.art_fill(art, radius=T.RADIUS_SM, height=T.px(110)) if art else
                   ft.Container(C.meta(tr("None")), height=T.px(110), alignment=ft.Alignment.CENTER,
                                border_radius=T.RADIUS_SM, bgcolor=T.SURFACE_3))
        buttons = [C.ghost(tr("Choose…"), ft.Icons.UPLOAD_FILE_OUTLINED,
                           lambda e, k=kind, lb=label: app.page.run_task(choose, k, lb))]
        if art:
            buttons.append(C.icon_btn(ft.Icons.DELETE_OUTLINE_ROUNDED, tr("Remove"),
                                      lambda e, k=kind, lb=label: remove(k, lb), color=T.TEXT_3))
        return ft.Container(ft.Column([
            box,
            C.body(label, T.TEXT, size=T.T_META, weight=ft.FontWeight.W_600),
            C.meta(shape),
            ft.Row(buttons, spacing=0),
        ], spacing=T.px(4), tight=True), width=T.px(190), padding=T.S2, border_radius=T.RADIUS_SM,
            bgcolor=T.SURFACE)

    def render() -> None:
        grid.controls = [slot(*s) for s in custom_slots()]
        C.update(grid)

    grid.controls = [slot(*s) for s in custom_slots()]
    app.page.show_dialog(C.dialog(
        tr("Your own artwork for {title}").format(title=g.get("title")),
        ft.Column([
            C.body(tr("Choose PNG, JPEG or WebP images. Missing shapes are made from the ones you choose, and "
                      "FramePort never replaces them.")),
            grid,
        ], spacing=T.S3, tight=True),
        actions=[C.ghost(tr("Search the stores instead"), ft.Icons.IMAGE_SEARCH_ROUNDED,
                         lambda e: (app.page.pop_dialog(), show_art_dialog(app, package))),
                 C.ghost(tr("Close"), on_click=lambda e: app.page.pop_dialog())]))
