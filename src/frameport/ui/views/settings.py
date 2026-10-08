"""Settings: the portable toolchain (incl. Revive), this PC for PC VR games, data folder, about."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from ... import REPO_URL, __version__, i18n, pipeline
from ...core.paths import user_data_dir
from ...errors import explain
from ...i18n import fmt_size, tr, tr_n
from ...recommend import catalog
from .. import components as C
from .. import glyphs as G
from .. import theme as T

if TYPE_CHECKING:
    from ..app import FramePortApp

TOOL_TITLES = {"java": tr("Java runtime"), "overport": "OVRPort", "apksigner": "apksigner", "revive": tr("Revive")}
TOOL_WHY = {"java": tr("Runs OVRPort and apksigner"), "overport": tr("Converts Quest games to OpenXR"),
            "apksigner": tr("Signs rebuilt games"), "revive": tr("Runs Oculus PC games on OpenXR")}


class SettingsView:
    def __init__(self, app: FramePortApp):
        self.app = app
        self.tools = ft.Column(spacing=0)
        self.pc = ft.Column(spacing=0)

    def fill_tools(self, check_latest=False):
        from ...tools import toolchain

        rows = []
        for s in toolchain.status(check_latest):
            newer = s.latest and s.version and s.latest != s.version and s.version not in ("external", "system")
            detail = TOOL_WHY.get(s.name, "")
            if s.installed:
                detail += (f" · {s.version or 'installed'}"
                           + (tr(" (update: {latest})").format(latest=s.latest) if newer else ""))
                if s.name == "revive" and "using" in (s.detail or ""):
                    detail += " · " + s.detail.split("·")[-1].strip()
            else:
                detail += tr(" · downloaded when first needed") if s.optional else tr(" · not installed yet")
            rows.append(C.status_row(True if s.installed else (None if s.optional else False),
                                     TOOL_TITLES.get(s.name, s.name), detail,
                                     help="revive" if s.name == "revive" else None))
        self.tools.controls = rows
        C.update(self.tools)

    def fill_pc(self):
        from ...core import winhost

        if not winhost.available():
            self.pc.controls = [C.status_row(
                None, tr("Windows not detected"),
                tr("PC VR games can run on this PC only with Windows (or WSL on Windows)"))]
        else:
            try:
                from ...targets.pc_revive import PcReviveTarget

                d = PcReviveTarget().describe()
                self.pc.controls = [
                    C.status_row(bool(d["steam"]), tr("Steam"),
                                 tr("Found") if d["steam"] else tr("Steam for Windows not found")),
                    C.status_row(d["steamvr"] or None, tr("SteamVR"),
                                 tr("Installed") if d["steamvr"]
                                 else tr("Install SteamVR from Steam to play PC VR games"),
                                 help="steamvr_pc"),
                    C.status_row(bool(d["revive"]), tr("Revive"), (f"{d['revive_version']} · {d['revive']}"
                                                              if d["revive"] else tr("Downloaded when first needed")),
                                 help="revive"),
                ]
            except Exception as exc:  # noqa: BLE001
                self.pc.controls = [C.status_row(False, tr("Couldn't check this PC"), explain(exc))]
        C.update(self.pc)

    def installing(self) -> ft.Control:
        from ...core import library

        def changed(e):
            library.set_setting("install.launch_test", bool(e.control.value))
        def keep_changed(e):
            library.set_setting("build.keep_copies", bool(e.control.value))

        def clean(e):
            def work():
                freed = pipeline.remove_all_converted_copies()
                self.app.toast(tr("Removed the converted copies ({size})").format(size=fmt_size(freed)))
            self.app.run_bg(work)
        keep = C.switch(tr("Keep converted copies on this PC after installing (FramePort converts again for every "
                           "install, so they're only needed for inspecting a build)"),
                        value=bool(library.setting("build.keep_copies", False)), on_change=keep_changed)
        launch = C.switch(tr("Launch test after installing on the Frame (starts the game once without the headset "
                             "and checks its log)"), value=bool(library.setting("install.launch_test", True)),
                          on_change=changed)
        return ft.Column([launch, keep, C.ghost(tr("Remove converted copies now"), ft.Icons.CLEANING_SERVICES_ROUNDED,
                                               clean, tooltip=tr("Your own game files aren't touched."))],
                         spacing=T.S2, horizontal_alignment=ft.CrossAxisAlignment.START)

    def updates_card(self) -> ft.Control:
        """Settings → Updates: FramePort's own updates (ui/updater.py, frameport/updates.py)."""
        import time

        from ... import updates
        from ...core import library

        app = self.app
        last = library.setting("update.last_check")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else tr("never")
        found = app.updater.found
        status = (C.callout(ft.Row([C.body(tr("FramePort {version} is available.").format(version=found.version),
                                           T.TEXT, expand=True),
                                    C.primary(tr("Update now"), ft.Icons.SYSTEM_UPDATE_ROUNDED,
                                              lambda e: app.updater.install())], spacing=T.S3), "info")
                  if found else C.meta(tr("You have the latest version as of the last check ({when}).")
                                       .format(when=when)))

        def auto_check(e):
            library.set_setting("update.auto_check", bool(e.control.value))

        def auto_install(e):
            library.set_setting("update.auto_install", bool(e.control.value))
        kind = {"bundle": tr("the downloaded app"), "source": tr("a source checkout (git pull + uv sync)"),
                "wheel": tr("an installed Python package (reinstalled from the release)")}[updates.install_kind()]
        return ft.Column([
            ft.Row([C.kv(tr("Installed"), tr("FramePort {version} · {kind}").format(version=__version__, kind=kind)),
                    ft.Container(expand=True),
                    C.secondary(tr("Check for updates"), ft.Icons.REFRESH_ROUNDED, lambda e: app.updater.check_now())],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            status,
            C.switch(tr("Check for new versions automatically"), value=bool(library.setting("update.auto_check", True)),
                      on_change=auto_check),
            C.switch(tr("Install updates automatically (downloads in the background, installs when FramePort "
                            "next starts)"), value=bool(library.setting("update.auto_install", False)),
                      on_change=auto_install),
            ft.Row([C.meta(tr("Asked to test a fix? Dev builds come before the next release."), expand=True),
                    C.ghost(tr("Install the latest dev build…"), G.TEST,
                            lambda e: app.updater.install_dev())],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=T.S3)

    def catalog_updates(self) -> ft.Control:
        """Settings → Data: confirmed game configs from GitHub main (recommend/catalog.refresh_remote)."""
        import time

        from ...core import library
        from ...recommend import catalog

        app = self.app
        st = catalog.remote_status()
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st["checked"])) if st["checked"] else tr("never")
        line = tr("Game recipes from GitHub: last checked {when}").format(when=when)
        if st["skipped"]:
            line += " · " + tr_n("{n} needs a newer FramePort", "{n} need a newer FramePort", len(st["skipped"]))

        def toggle(e):
            library.set_setting("catalog.auto_update", bool(e.control.value))
        return ft.Column([
            ft.Row([C.meta(line, expand=True),
                    C.ghost(tr("Check now"), ft.Icons.SYNC_ROUNDED,
                            lambda e: app.run_bg(lambda: app._refresh_catalog(force=True)))],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            C.switch(tr("Update game recipes from GitHub automatically (confirmed recipes arrive without a "
                        "FramePort update)"), value=bool(library.setting("catalog.auto_update", True)),
                     on_change=toggle),
        ], spacing=T.S2)

    def links_card(self) -> ft.Control:
        """Settings → Install links: one switch per scheme — framedrop:// ("Install with FrameDrop" buttons on web
        pages) and frameport:// (FramePort's own links) open FramePort (urlhandler)."""
        from ... import urlhandler

        app = self.app
        rows = {}
        last: dict = {}

        def show(st: dict | None):
            last.clear()
            last.update(st or {})
            for scheme, (_switch, state, take) in rows.items():
                take.visible = False
                if not last.get("supported", True):
                    state.value = tr("This system can't send web links to FramePort: use Add games → Install from a "
                                     "link…")
                elif not urlhandler.enabled(scheme):
                    state.value = tr("Off: these links open whatever app is set up for them, if any.")
                elif last.get(scheme) == "other":
                    state.value = tr("{app} opens these links on this PC.").format(
                        app=last.get(f"{scheme}_by") or tr("Another app"))
                    take.visible = True
                elif last.get(scheme) == "ours":
                    state.value = tr("These links open FramePort.")
                else:
                    state.value = tr("Not set up yet (FramePort registers itself when it starts).")
            C.update(*[c for r in rows.values() for c in r[1:]])

        def toggle(scheme):
            def changed(e):
                on = bool(e.control.value)
                app.run_bg(lambda: show(urlhandler.set_enabled(scheme, on)))
            return changed

        def force(scheme):
            def clicked(e):
                other = last.get(f"{scheme}_by") or tr("the other app")
                C.confirm(app.page, tr("Open {scheme}:// links with FramePort?").format(scheme=scheme),
                          tr("These links will open FramePort instead of {app}. You can switch back in {app}, or by "
                             "turning this setting off.").format(app=other),
                          tr("Use FramePort"), lambda: app.run_bg(lambda: show(urlhandler.register([scheme],
                                                                                                    force=True))))
            return clicked

        labels = {"framedrop": tr("Open \"Install with FrameDrop\" buttons (framedrop:// links) in FramePort"),
                  "frameport": tr("Open frameport:// links in FramePort")}
        controls = []
        for scheme in urlhandler.SCHEMES:
            switch = C.switch(labels[scheme], value=urlhandler.enabled(scheme), on_change=toggle(scheme))
            state = C.meta(tr("Checking which app opens these links…"))
            take = C.secondary(tr("Use FramePort for these links"), ft.Icons.LINK_ROUNDED, on_click=force(scheme))
            take.visible = False
            rows[scheme] = (switch, state, take)
            controls.append(ft.Column([switch, ft.Container(ft.Column([state, take], spacing=T.S2),
                                                            padding=ft.Padding(T.px(4), 0, 0, 0))],
                                      spacing=T.px(4)))
        app.run_bg(lambda: show(urlhandler.status()))
        return ft.Column(controls, spacing=T.S4)

    def theme_picker(self) -> ft.Control:
        """One card per colour theme (built-in and installed theme files: a swatch of its window and its two portal
        colours); a click switches at once. Theme files are installed from here (docs/THEMES.md)."""
        from ...core import library
        from ..app import themes_dir

        app = self.app
        about = {"portal": tr("Orange for actions, blue for the PC side: the logo's two portals"),
                 "portal_oled": tr("Portal on true black, for OLED screens"),
                 "original": tr("FramePort's original violet")}

        def pick(tid: str):
            if tid == T.THEME:
                return
            library.set_setting("ui.theme", tid)
            app.restyle(tid)

        def remove(tid: str):
            name = T.THEMES[tid]["name"]
            try:
                T.remove_theme(tid)
            except (T.ThemeError, OSError) as exc:
                app.toast(tr("Couldn't remove the theme: {error}").format(error=exc), error=True)
                return
            if tid == T.THEME:
                library.set_setting("ui.theme", T.DEFAULT_THEME)
                app.restyle(T.DEFAULT_THEME)
            else:
                app.render()
            app.toast(tr("Removed the theme \"{name}\"").format(name=name))

        async def install(e):
            files = await ft.FilePicker().pick_files(dialog_title=tr("Theme file to install"),
                                                     allowed_extensions=["json"])
            path = next((f.path for f in files or [] if f.path), None)
            if not path:
                return
            try:
                tid = T.install_theme(Path(path), themes_dir())
            except (T.ThemeError, OSError) as exc:
                app.toast(tr("That theme file can't be used: {error}").format(error=exc), error=True)
                return
            library.set_setting("ui.theme", tid)
            app.restyle(tid)
            app.toast(tr("Installed the theme \"{name}\"").format(name=T.THEMES[tid]["name"]))

        def card(tid: str) -> ft.Control:
            theme, on = T.THEMES[tid], tid == T.THEME
            c = theme["colors"]
            dot = lambda color: ft.Container(width=T.px(18), height=T.px(18), border_radius=T.px(9),  # noqa: E731
                                             bgcolor=color)
            bar = ft.Container(height=T.px(6), border_radius=T.px(3), expand=True,
                               gradient=ft.LinearGradient(colors=[c["SECONDARY"], c["ACCENT"]]) if theme["dual"]
                               else None, bgcolor=None if theme["dual"] else c["SURFACE_3"])
            swatch = ft.Container(ft.Row([dot(c["ACCENT"]), dot(c["SECONDARY"]), bar], spacing=T.px(6),
                                         vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                  bgcolor=c["BG"], border=ft.Border.all(1, c["BORDER"]), border_radius=T.RADIUS_SM,
                                  padding=ft.Padding(T.S3, T.S3, T.S3, T.S3))
            # small marks (an IconButton would make this card's name row taller than the others')
            marks = [C.as_icon(ft.Icons.CHECK_CIRCLE_ROUNDED, T.px(18), T.TEXT if T.DUAL else T.ACCENT)] if on else []
            if tid.startswith(T.USER_PREFIX):
                marks.append(ft.Container(C.as_icon(ft.Icons.DELETE_OUTLINE_ROUNDED, T.px(18), T.TEXT_2),
                                          tooltip=tr("Remove this theme"), border_radius=T.RADIUS_XS, ink=True,
                                          on_click=lambda e, t=tid: remove(t)))
            corner = ft.Row(marks, spacing=T.S2, tight=True)
            box = ft.Container(ft.Column([
                swatch,
                ft.Row([C.body(theme["name"], T.TEXT, weight=ft.FontWeight.W_600, expand=True), corner],
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                C.meta(about.get(tid) or tr("Installed theme file · {file}").format(file=Path(theme["path"]).name)),
            ], spacing=T.S2), width=T.px(240), padding=T.S3, border_radius=T.RADIUS,
                ink=True, on_click=lambda e, t=tid: pick(t),
                border=ft.Border.all(2, T.ACCENT if on else T.BORDER))  # same width: the content doesn't shift
            C.selected_style(box, on)
            return box

        return ft.Column([
            # no expand= in here: an expanding child in a wrap=True Row is an invalid layout (Flet's grey box)
            ft.Row([C.body(tr("Theme"), T.TEXT, weight=ft.FontWeight.W_500), ft.Container(width=T.S4),
                    C.ghost(tr("Copy this theme as a file"), ft.Icons.CONTENT_COPY_ROUNDED,
                            lambda e: (app.copy(T.theme_json(T.THEME)),
                                       app.toast(tr("Copied: save it as a .json file, change the colors and "
                                                    "install it")))),
                    C.secondary(tr("Install theme file…"), ft.Icons.FILE_OPEN_ROUNDED, install)],
                   spacing=T.S2, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([card(t) for t in T.THEMES], spacing=T.S3, run_spacing=T.S3, wrap=True,
                   vertical_alignment=ft.CrossAxisAlignment.START),
        ], spacing=T.S2)

    def appearance(self) -> ft.Control:
        from ...core import library

        current = library.setting("ui.scale", "auto")
        auto = T.detect_scale()
        options = [ft.dropdown.Option("auto", tr("Automatic ({auto:.0%})").format(auto=auto))] + [
            ft.dropdown.Option(str(f), f"{f:.0%}") for f in T.SCALE_CHOICES]
        note = C.meta(tr("Now {scale:.0%}. Changes apply the next time FramePort starts.").format(scale=T.SCALE))

        def changed(e):
            library.set_setting("ui.scale", e.control.value)
            new = T.scale_from_setting(e.control.value)
            note.value = (tr("Now {scale:.0%}; {new:.0%} after restarting FramePort.").format(scale=T.SCALE, new=new)
                          if abs(new - T.SCALE) > 0.01
                          else tr("Now {scale:.0%}.").format(scale=T.SCALE))
            C.update(note)

        dd = C.dropdown(label=tr("Text and layout size"), value=str(current) if current != "auto" else "auto",
                         options=options, width=T.px(260), on_select=changed)
        controls = [self.theme_picker(), ft.Container(height=T.S2), dd, note]
        languages = i18n.available()
        if len(languages) > 1:  # only once a translation exists
            def language_changed(e):
                library.set_setting("ui.language", e.control.value)
                C.update(lang_note)
            lang = C.dropdown(label=tr("Language"), value=i18n.language(), width=T.px(260), on_select=language_changed,
                               options=[ft.dropdown.Option(code, i18n.language_name(code)) for code in languages])
            lang_note = C.meta(tr("Changes apply the next time FramePort starts."))
            controls += [lang, lang_note]
        return ft.Column(controls, spacing=T.S2)

    def agent_text(self) -> str:
        """The agent version this app ships, and the one on the connected Frame (it's replaced on the next command
        whenever the files differ)."""
        from ...frame.connection import bundled_agent_version

        mine = bundled_agent_version()
        text = f"v{mine}" if mine else tr("unknown")
        info = self.app.frame_info or {}
        remote = info.get("agent_version")
        if self.app.frame_state == "connected" and remote:
            text += tr(" · on the Frame: ") + (f"v{remote}" if remote == mine
                                               else tr("v{remote} (updates on the next action)").format(remote=remote))
        else:
            text += tr(" · Frame not connected")
        return text

    def build(self) -> ft.Control:
        app = self.app
        self.tools.controls = [ft.Row([C.spinner(),
                                       C.meta(tr("Checking tools…"))], spacing=T.S2)]
        app.run_bg(self.fill_tools)
        self.pc.controls = [ft.Row([C.spinner(),
                                    C.meta(tr("Checking this PC…"))], spacing=T.S2)]
        app.run_bg(self.fill_pc)
        from ... import __version__ as ver
        data = str(user_data_dir())
        return ft.Column([
            app.top_bar(tr("Settings"), tr("Updates, tools, this PC and where FramePort keeps its data")),
            C.section(tr("Updates"), C.card(self.updates_card(), padding=T.S4), help="app_updates"),
            C.section(tr("Tools"), C.card(self.tools, padding=ft.Padding(T.S4, T.S2, T.S4, T.S2)),
                      subtitle=tr("FramePort manages its own copies; nothing is installed system-wide"),
                      action=ft.Row([C.ghost(tr("Update tools"), ft.Icons.UPDATE_ROUNDED,
                                             lambda e: app.update_tools(update=True)),
                                     C.secondary(tr("Install missing"), ft.Icons.DOWNLOAD_ROUNDED,
                                                 lambda e: app.update_tools())], spacing=T.S2)),
            C.section(tr("This PC (for PC VR games)"), C.card(self.pc, padding=ft.Padding(T.S4, T.S2, T.S4, T.S2))),
            C.section(tr("Data"), C.card(ft.Column([
                C.kv(tr("Data folder"), ft.Row([C.body(data, T.TEXT, selectable=True, expand=True),
                                            C.icon_btn(ft.Icons.CONTENT_COPY_ROUNDED, tr("Copy path"),
                                                       lambda e: app.copy(data))]), "data_folder"),
                C.kv(tr("Catalog"), tr("{len} known-good recipes (bundled, remote and yours)")
                     .format(len=len(catalog.load())), "catalog"),
                self.catalog_updates(),
            ], spacing=T.S2))),
            # text above, buttons below (side by side, the buttons squeezed the text in a narrow window)
            C.section(tr("Problems and feedback"), C.card(ft.Column([
                C.body(tr("Something not working? Collect a diagnostics zip (logs, settings, device info; personal "
                       "data removed) and attach it to a GitHub issue. For one game, use its menu instead.")),
                ft.Row([C.secondary(tr("Report a problem…"), ft.Icons.BUG_REPORT_ROUNDED,
                                    lambda e: app.report_problem_dialog()),
                        C.ghost(tr("Collect app logs"), ft.Icons.FOLDER_ZIP_ROUNDED, lambda e: app.collect_logs())],
                       spacing=T.S3, run_spacing=T.S2, wrap=True),
            ], spacing=T.S3)), help="diag_bundle"),
            C.section(tr("Remove FramePort"), C.card(ft.Column([
                C.body(tr("Removes everything FramePort created: its data and tools on this PC, the Steam entries it "
                       "added, and (optionally) its games and files on the Frame. Your game dumps aren't touched.")),
                C.danger(tr("Uninstall FramePort…"), ft.Icons.DELETE_FOREVER_ROUNDED, lambda e: app.uninstall_app(),
                         outline=True),
            ], spacing=T.S3, horizontal_alignment=ft.CrossAxisAlignment.START))),
            C.section(tr("Installing"), C.card(self.installing(), padding=T.S4), help="launch_test"),
            C.section(tr("Install links"), C.card(self.links_card(), padding=T.S4), help="install_links"),
            C.section(tr("Appearance"), C.card(self.appearance(), padding=T.S4), help="ui_scale"),
            C.section(tr("About"), C.card(ft.Column([
                C.kv(tr("Version"), ver),
                C.kv(tr("Frame agent"), self.agent_text(), "frame_agent"),
                C.kv(tr("Source"), C.ghost(REPO_URL.removeprefix("https://"), ft.Icons.OPEN_IN_NEW_ROUNDED,
                                           color=T.ACCENT, url=REPO_URL)),
                C.meta(tr("Uses OVRPort, Revive (LibreVR), Valve's Lepton and Proton. "
                          "Not affiliated with Valve or Meta.")),
            ], spacing=T.S2))),
        ], spacing=T.S5, scroll=ft.ScrollMode.AUTO, expand=True)
