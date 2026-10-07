"""The GUI side of self-update: finds new releases in the background, shows them (sidebar card, Library bar, a dialog
with the release notes) and updates in one click. frameport.updates does the work."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

import flet as ft

from .. import __version__, updates
from ..core import applog, library
from ..errors import explain
from ..i18n import tr
from . import components as C
from . import glyphs as G
from . import theme as T

if TYPE_CHECKING:
    from .app import FramePortApp

FIRST_CHECK_DELAY = 10  # seconds after start: don't compete with the start-up work


class Updater:
    def __init__(self, app: FramePortApp):
        self.app = app
        self.found: updates.Update | None = None
        self.restart = None          # callable: what to do once the job queue is idle (install + quit)
        self.preparing = False
        self.build_card()

    def build_card(self) -> None:
        """The sidebar's "Update available" card (again after a theme change; keeps what it shows)."""
        old = getattr(self, "card", None)
        self._version = C.meta(getattr(self, "_version", None) and self._version.value or "", color=T.ON_ACCENT)
        self.card = ft.Container(
            ft.Row([ft.Icon(ft.Icons.SYSTEM_UPDATE_ROUNDED, size=T.px(18), color=T.ON_ACCENT),
                    ft.Column([C.body(tr("Update available"), T.ON_ACCENT, weight=ft.FontWeight.W_600), self._version],
                              spacing=0, expand=True)], spacing=T.S2),
            padding=T.S3, border_radius=T.RADIUS_SM, bgcolor=T.ACCENT, ink=True, visible=bool(old and old.visible),
            tooltip=tr("A new FramePort version is ready to install"), on_click=lambda e: self.show_dialog())

    # ---------------------------------------------------------------- checking
    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        time.sleep(FIRST_CHECK_DELAY)
        while True:
            if not updates.checks_disabled():
                try:
                    self._set(updates.check())
                except Exception:  # noqa: BLE001
                    applog.log.exception("update check failed")
            time.sleep(updates.CHECK_EVERY)

    def check_now(self) -> None:
        """Settings → "Check for updates": always asks GitHub, also finds a version the user chose to skip."""
        def work():
            up = updates.check(force=True)
            self._set(up)
            if up:
                self.app.page.run_thread(self.show_dialog)
            else:
                self.app.toast(tr("FramePort {version} is the latest version").format(version=__version__))
        self.app.run_bg(work)

    def _set(self, up: updates.Update | None) -> None:
        self.found = up
        self.card.visible = bool(up)
        self._version.value = tr("FramePort {version} · click to install").format(version=up.version) if up else ""
        C.update(self.card)
        self.app.refresh_view()
        if up and library.setting("update.auto_install", False) and updates.install_kind() == "bundle":
            self._prepare_quietly(up)

    def _prepare_quietly(self, up: updates.Update) -> None:
        """"Install updates automatically": download + verify now, install on the next start (main())."""
        if self.preparing or (updates.ready_update() or ("",))[0] == up.version:
            return
        self.preparing = True
        try:
            updates.prepare(up)
            self.app.toast(tr("FramePort {version} is downloaded and installs the next time FramePort starts")
                           .format(version=up.version),
                           action=tr("Restart now"), on_action=lambda e: self.install())
        except Exception as exc:  # noqa: BLE001
            applog.log.warning("automatic update download failed: %s", exc)
        finally:
            self.preparing = False

    # ---------------------------------------------------------------- the dialog
    def show_dialog(self) -> None:
        up = self.found
        if not up:
            return
        page = self.app.page
        kind = updates.install_kind()
        how = {"bundle": "FramePort downloads it, checks it, closes and opens again as the new version. Your games, "
                         "settings and Frame connection stay as they are.",
               "source": "Updates this source checkout (git pull + uv sync), then restarts FramePort.",
               "wheel": "Reinstalls FramePort from the release, then restarts it."}[kind]
        notes = up.notes.strip() or tr("No release notes.")

        def later(e):
            updates.skip(up.version)
            self._set(None)
            page.pop_dialog()

        def go(e):
            page.pop_dialog()
            self.install()
        page.show_dialog(ft.AlertDialog(
            title=ft.Text(tr("FramePort {version} is available").format(version=up.version), color=T.TEXT,
                          weight=ft.FontWeight.W_600),
            bgcolor=T.SURFACE_2,
            content=ft.Container(ft.Column([
                C.body(tr("You have {version}. {how}").format(version=__version__, how=how), T.TEXT_2),
                ft.Container(ft.Markdown(notes, selectable=True, extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                                         md_style_sheet=_notes_style(),
                                         on_tap_link=lambda e: page.launch_url(e.data)),
                             padding=T.S3, border_radius=T.RADIUS_SM, bgcolor=T.SURFACE,
                             border=ft.Border.all(1, T.BORDER)),
            ], spacing=T.S3, scroll=ft.ScrollMode.AUTO, tight=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                width=T.px(560), height=T.px(min(420, 130 + 26 * len(notes.splitlines())))),
            actions=[C.ghost(tr("Skip this version"), on_click=later),
                     C.ghost(tr("Release page"), ft.Icons.OPEN_IN_NEW_ROUNDED, lambda e: page.launch_url(up.page)),
                     C.primary(tr("Update now"), ft.Icons.SYSTEM_UPDATE_ROUNDED, go)]))

    # ---------------------------------------------------------------- dev builds
    def install_dev(self) -> None:
        """Settings → "Install the latest dev build": the rolling `dev` pre-release the maintainer publishes for
        testing a fix before a release (CI "Run workflow", dev build)."""
        def work():
            up = updates.check_dev()
            if not up:
                self.app.toast(tr("No dev build is published right now"), error=True)
                return
            if up.version == __version__:
                self.app.toast(tr("You already have the latest dev build ({version})").format(version=up.version))
                return
            self.app.page.run_thread(lambda: self._confirm_dev(up))
        self.app.run_bg(work)

    def _confirm_dev(self, up: updates.Update) -> None:
        page = self.app.page
        notes = up.notes.strip() or tr("No notes.")

        def go(e):
            page.pop_dialog()
            self.found = up
            self.install()
        page.show_dialog(ft.AlertDialog(
            title=ft.Text(tr("Install dev build {version}?").format(version=up.version), color=T.TEXT,
                          weight=ft.FontWeight.W_600),
            bgcolor=T.SURFACE_2,
            content=ft.Container(ft.Column([
                C.callout(C.body(tr("Dev builds let you test fixes before they're released. They're less tested "
                                    "than releases and may have bugs. You get the next release as a normal update "
                                    "(you have {version}).").format(version=__version__), T.TEXT), "warn"),
                ft.Container(ft.Markdown(notes, selectable=True, extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                                         md_style_sheet=_notes_style(),
                                         on_tap_link=lambda e: page.launch_url(e.data)),
                             padding=T.S3, border_radius=T.RADIUS_SM, bgcolor=T.SURFACE,
                             border=ft.Border.all(1, T.BORDER)),
            ], spacing=T.S3, scroll=ft.ScrollMode.AUTO, tight=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                width=T.px(560), height=T.px(min(440, 190 + 26 * len(notes.splitlines())))),
            actions=[C.ghost(tr("Cancel"), on_click=lambda e: page.pop_dialog()),
                     C.ghost(tr("Build page"), ft.Icons.OPEN_IN_NEW_ROUNDED, lambda e: page.launch_url(up.page)),
                     C.primary(tr("Install dev build"), G.TEST, go)]))

    # ---------------------------------------------------------------- installing
    def install(self) -> None:
        up = self.found or updates.ready_update_info()
        if not up:
            return
        kind = updates.install_kind()
        page = self.app.page
        if kind == "bundle":
            target = updates.bundle_root()
            if not target or not updates.can_replace(target):
                self.app.toast(tr("FramePort can't replace itself in {value} (no permission): opening the release "
                                  "page to download it").format(value=target or 'this folder'), error=True)
                page.launch_url(up.page)
                return
        if kind == "source" and updates.source_is_dirty():
            self.app.toast(tr("This source checkout has uncommitted changes: commit or stash them, then update"),
                           error=True)
            return
        if page.web:
            self.app.toast(tr("Updating works in the desktop app"))
            return

        def run(job):
            rep = job.reporter
            if kind == "bundle":
                ready = updates.ready_update()
                app = ready[1] if ready and ready[0] == up.version else updates.prepare(up, rep)
                self.restart = lambda: self._restart_bundle(app)
            else:
                for cmd in updates.upgrade_commands(up, kind):
                    rep.stage(" ".join(cmd[:3]))
                    try:
                        out = subprocess.run(cmd, capture_output=True, text=True, timeout=updates.UPGRADE_TIMEOUT,
                                             env=updates.upgrade_env(), stdin=subprocess.DEVNULL)
                    except subprocess.TimeoutExpired:
                        raise RuntimeError(tr("{value} took too long; update by hand: {join}")
                                           .format(value=cmd[0], join=' '.join(cmd))) from None
                    for line in (out.stdout + out.stderr).splitlines()[-20:]:
                        rep.log(line)
                    if out.returncode:
                        raise RuntimeError(tr("{value} failed ({returncode})")
                                           .format(value=cmd[0], returncode=out.returncode))
                self.restart = self._restart_process
            rep.stage("Restarting")
            return tr("FramePort {version} is ready: restarting").format(version=up.version)
        self.app.submit(tr("Update FramePort to {version}").format(version=up.version), run, None, kind="app-update",
                        open_panel=True)
        if self.app.jobs.current() and self.app.jobs.current().kind != "app-update":
            self.app.toast(tr("FramePort updates and restarts after the current job"))

    def on_jobs_changed(self) -> None:
        """Called when a job finishes: once nothing else runs, install and restart."""
        if self.restart and not any(j.active for j in self.app.jobs.jobs):
            restart, self.restart = self.restart, None
            self.app.page.run_thread(restart)

    def _restart_bundle(self, app) -> None:
        try:
            updates.apply(app)
        except Exception as exc:  # noqa: BLE001
            self.app.toast(tr("Update failed: {exc}").format(exc=explain(exc)), error=True)
            return
        self._quit()

    def _restart_process(self) -> None:
        """Source/wheel installs: start a fresh FramePort, then close this one."""
        subprocess.Popen([sys.executable, "-c", "from frameport.ui.app import main; main()"], close_fds=True,
                         start_new_session=sys.platform != "win32",
                         creationflags=0x00000008 if sys.platform == "win32" else 0)
        self._quit()

    def _quit(self) -> None:
        async def close():
            try:
                await self.app.page.window.close()
            except Exception:  # noqa: BLE001
                pass
        self.app.page.run_task(close)


def _notes_style() -> ft.MarkdownStyleSheet:
    """Release notes in the dark theme (Markdown's defaults are dark text)."""
    def text(color=T.TEXT, size=14, weight=None, **kw):
        return ft.TextStyle(color=color, size=T.px(size), weight=weight, **kw)
    return ft.MarkdownStyleSheet(
        p_text_style=text(T.TEXT_2), list_bullet_text_style=text(T.TEXT_2),
        strong_text_style=text(weight=ft.FontWeight.W_600), em_text_style=text(T.TEXT_2, italic=True),
        a_text_style=text(T.ACCENT), code_text_style=text(T.TEXT, 13, font_family="monospace"),
        h1_text_style=text(size=18, weight=ft.FontWeight.W_600),
        h2_text_style=text(size=16, weight=ft.FontWeight.W_600),
        h3_text_style=text(size=15, weight=ft.FontWeight.W_600), blockquote_text_style=text(T.TEXT_2))


def library_bar(app: FramePortApp) -> ft.Control | None:
    """The Library's "update available" bar (None when there's nothing to show)."""
    up = app.updater.found if getattr(app, "updater", None) else None
    if not up:
        return None
    return C.callout(ft.Row([
        C.body(tr("FramePort {version} is available (you have {version2}).")
               .format(version=up.version, version2=__version__), T.TEXT, expand=True),
        C.primary(tr("Update now"), ft.Icons.SYSTEM_UPDATE_ROUNDED, lambda e: app.updater.install()),
        C.ghost(tr("What's new"), on_click=lambda e: app.updater.show_dialog()),
        C.ghost(tr("Later"), on_click=lambda e: (updates.skip(up.version), app.updater._set(None))),
    ], spacing=T.S3), "info", ft.Icons.SYSTEM_UPDATE_ROUNDED)


def apply_pending_at_start() -> bool:
    """main(), before the window opens: with "Install updates automatically", a downloaded and verified newer version
    is installed now (FramePort quits and the update script starts the new version). True = quit now."""
    try:
        updates.cleanup_old()
        if not library.setting("update.auto_install", False) or updates.install_kind() != "bundle":
            return False
        ready = updates.ready_update()
        target = updates.bundle_root()
        if not ready or not target or not updates.can_replace(target):
            return False
        applog.log.info("installing the downloaded update %s at start", ready[0])
        updates.apply(ready[1], target)
        return True
    except Exception:  # noqa: BLE001
        applog.log.exception("installing the downloaded update failed; starting the current version")
        return False
