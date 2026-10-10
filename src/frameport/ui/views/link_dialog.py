"""Install links ("Install with FrameDrop" buttons, frameport:// links, pasted links): read the manifest, ask the user,
download, add to the library and install on the Frame. The protocol itself is in deeplink.py (no Flet)."""
from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from ... import deeplink, pipeline
from ...core import library
from ...i18n import fmt_size, tr
from .. import components as C
from .. import theme as T

if TYPE_CHECKING:
    from ..app import FramePortApp
    from ..jobs import Job



def kind_label(kind: str | None) -> str:
    return {deeplink.APK: tr("Android app (APK)"), deeplink.LINUX: tr("Linux build"),
            deeplink.EXE: tr("Windows program"), deeplink.OBB: tr("game data (OBB)")}.get(kind or "", tr("other file"))


def show_paste_dialog(app: FramePortApp) -> None:
    """Add games → "Install from link…": paste a FrameDrop/FramePort button link, a manifest or a file URL."""
    field = C.field(label=tr("Link"), hint_text="https://framedropvr.com/install?manifest=…", autofocus=True,
                    expand=True)

    def go(e=None):
        text = (field.value or "").strip()
        if not text:
            return
        app.page.pop_dialog()
        open_link(app, text, pasted=True)
    field.on_submit = go
    app.page.show_dialog(C.dialog(
        tr("Install from a link"),
        ft.Column([
            C.body(tr("Paste an install link: a FrameDrop button's address, a manifest (.json) or a direct link to "
                      "an APK, .zip or .exe.")),
            ft.Row([field, C.help_icon("install_links")]),
        ], tight=True, spacing=T.S3),
        actions=[C.ghost(tr("Cancel"), on_click=lambda e: app.page.pop_dialog()),
                 C.primary(tr("Continue"), ft.Icons.ARROW_FORWARD_ROUNDED, on_click=go)]))


def open_link(app: FramePortApp, text: str, pasted: bool = False) -> None:
    """A link from a web page (framedrop://, frameport://) or pasted: read what it offers in the background, then ask
    before anything is downloaded."""
    try:
        req = deeplink.parse(text)
    except deeplink.LinkError as exc:
        app.toast(tr("Can't use that link: {reason}").format(reason=str(exc)), error=True)
        return

    def run(job: Job):
        job.reporter.stage(tr("Reading the install link"))
        m = deeplink.fetch_manifest(req)
        sizes = {f.url: deeplink.head_size(f.url) for f in m.files}
        icon = deeplink.fetch_icon(m)  # FramePort's manifest extension (None without one)
        app.page.run_thread(lambda: _confirm(app, m, sizes, pasted, icon))
        return tr("{name}: waiting for your answer").format(name=m.name)
    app.submit(tr("Install link: {host}").format(host=_host(req.source)), run, None, "task")


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or url) if "://" in url else url


def _header(m: deeplink.Manifest, icon) -> ft.Control | None:
    """The manifest's icon and description (FramePort-only fields), when it has them."""
    from ...artwork import thumbs

    if not icon and not m.description:
        return None
    text = m.description if len(m.description) <= 400 else m.description[:400].rsplit(" ", 1)[0] + "…"
    parts = []
    if icon:
        parts.append(ft.Image(src=thumbs.asset_url(icon), width=T.px(64), height=T.px(64), fit=ft.BoxFit.CONTAIN,
                              border_radius=T.RADIUS_SM))
    if text:
        parts.append(ft.Container(C.body(text, T.TEXT, selectable=True), expand=True))
    return ft.Row(parts, spacing=T.S3, vertical_alignment=ft.CrossAxisAlignment.START)


def _confirm(app: FramePortApp, m: deeplink.Manifest, sizes: dict, pasted: bool, icon=None) -> None:
    rows = []
    header = _header(m, icon)
    if header:
        rows.append(header)
    for f in m.files:
        size = sizes.get(f.url)
        # the file name on its own line (long names end in "…", the full name on hover), the facts below it
        rows.append(ft.Column([
            C.body(f.filename, T.TEXT, weight=ft.FontWeight.W_500, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                   tooltip=f.filename),
            C.meta(" · ".join(p for p in (kind_label(f.kind), fmt_size(size) if size else "",
                                          tr("checksum checked") if f.sha256 else tr("no checksum")) if p)),
        ], spacing=T.px(2), tight=True))
    frame = app.frame_state == "connected"
    notes = [C.callout(tr("Only install software from sites you trust. FramePort downloads it from {host} and "
                          "installs it on your Frame.").format(host=m.host or _host(m.source)), "warn")]
    if not frame:
        notes.append(C.callout(tr("No Frame connected: the game goes to your library now and installs when the Frame "
                                  "connects."), "info"))
    if m.main.kind == deeplink.LINUX:
        notes.append(C.body(tr("Linux builds should be made for arm64; others run slower."),
                            T.TEXT_3))
    heading = tr("Install {name}?").format(name=m.name)
    intro = tr("You pasted a link to {name}.") if pasted else tr("A web page asked FramePort to install {name}.")
    C.confirm(app.page, heading, intro.format(name=m.name), tr("Download and install"),
              lambda: download_and_install(app, m, icon),
              extra=ft.Column([*rows, *notes], spacing=T.S2, tight=True))


def download_and_install(app: FramePortApp, m: deeplink.Manifest, icon=None) -> Job:
    def run(job: Job):
        rep = job.reporter
        path = deeplink.download(m, rep)
        rep.stage(tr("Adding to the library"))
        g = pipeline.add_from_link(m, path, rep, icon=icon)
        pkg = g["package"]
        try:
            from ...artwork import thumbs

            thumbs.prewarm(pkg)
        except Exception:  # noqa: BLE001 - artwork is optional
            pass
        library.set_setting("ui.welcome_done", True)
        app.open_game(pkg)
        app.page.run_thread(lambda: app.install(pkg, "frame"))  # asks the usual install questions, then queues it
        return tr("Downloaded {name}").format(name=g.get("title") or m.name)
    return app.submit(tr("Download {name}").format(name=m.name), run, None, "task")
