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
from ..help import HELP

if TYPE_CHECKING:
    from ..app import FramePortApp

TOOL_TITLES = {"java": tr("Java runtime"), "overport": "OVRPort", "apksigner": "apksigner", "revive": tr("Revive")}
TOOL_WHY = {"java": tr("Runs OVRPort and apksigner"), "overport": tr("Converts Quest games to OpenXR"),
            "apksigner": tr("Signs rebuilt games"), "revive": tr("Runs Oculus PC games on SteamVR")}
# the page's sections in order (the index on the left lists them): what most users need first (updates of the app and
# their games, installing, where games come from, looks), then what only some need (link handlers, tools FramePort
# manages itself, PC VR on this PC), "Uninstall FramePort" last
SECTIONS = ("updates", "installing", "controllers", "folders", "artwork", "appearance", "data", "feedback", "links",
            "tools", "pc", "about", "remove")


def section_titles() -> dict[str, str]:
    """Index labels of SECTIONS (the sections' own headings may be longer)."""
    return {"appearance": tr("Appearance"), "updates": tr("Updates"), "links": tr("Install links"),
            "folders": tr("Library folders"), "artwork": tr("Artwork"), "controllers": tr("Controllers"),
            "installing": tr("Installing"), "tools": tr("Tools"), "pc": tr("This PC"), "data": tr("Data"),
            "feedback": tr("Problems and feedback"), "about": tr("About"), "remove": tr("Uninstall FramePort")}


def scroll_key(section: str) -> str:
    return f"settings-{section}"


class SettingsView:
    def __init__(self, app: FramePortApp):
        self.app = app
        self.tools = ft.Column(spacing=0)
        self.pc = ft.Column(spacing=0)
        self.page_col: ft.Column | None = None  # the scrolling column of sections (build())
        self.index: dict[str, ft.Container] = {}  # section -> its entry in the index

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
            rows.append(C.Check(True if s.installed else ("warn" if s.optional else False),
                                TOOL_TITLES.get(s.name, s.name), detail, "revive" if s.name == "revive" else None,
                                None if s.optional else (tr("Install"), ft.Icons.DOWNLOAD_ROUNDED,
                                                         self.app.update_tools)))
        self.tools.controls = [C.checklist(rows)]
        C.update(self.tools)

    def fill_pc(self):
        from ...core import winhost

        if not winhost.available():
            items = [C.Check("warn", tr("Windows not detected"),
                             tr("PC VR games run on this PC only on Windows"))]
        else:
            try:
                from ...targets.pc_revive import PcReviveTarget

                d = PcReviveTarget().describe()
                items = [
                    C.Check(bool(d["steam"]), tr("Steam"),
                            tr("Found") if d["steam"] else tr("Steam for Windows not found")),
                    C.Check(True if d["steamvr"] else "warn", tr("SteamVR"),
                            tr("Installed") if d["steamvr"] else tr("Install SteamVR from Steam to play PC VR games"),
                            "steamvr_pc"),
                    C.Check(bool(d["revive"]), tr("Revive"), (f"{d['revive_version']} · {d['revive']}"
                                                             if d["revive"] else tr("Downloaded when first needed")),
                            "revive"),
                ]
            except Exception as exc:  # noqa: BLE001
                items = [C.Check(False, tr("Couldn't check this PC"), explain(exc))]
        self.pc.controls = [C.checklist(items)]
        C.update(self.pc)

    def installing(self) -> ft.Control:
        from ...core import library
        from ...frame.local import on_frame
        from ...install.installer import steam_library_mode, upload_threads

        def steam_changed(e):
            mode = e.control.value
            library.set_setting("install.steam_library", mode)
            if self.app.frame_state == "connected":
                self.app.run_bg(lambda: self.app.target.connect().frame.agent("library_mode", mode=mode))

        def restart(e):
            app = self.app
            if app.frame_state != "connected":
                app.toast(tr("Connect to the Frame first."))
                return
            def work(confirm=False):
                result = app.target.connect().frame.agent("restart_steam", confirm=confirm)
                if result.get("confirmation_required"):
                    C.confirm(app.page, tr("A game is running"),
                              tr("Restarting Steam ends the running game. Continue?"), tr("Restart Steam now"),
                              lambda: app.run_bg(lambda: work(True)), danger=True)
                else:
                    app.toast(tr("Steam is restarting. FramePort on the Frame will close."))
            C.confirm(app.page, tr("Restart Steam now?"),
                      tr("This applies pending library changes and closes Desktop Mode and FramePort on the Frame."),
                      tr("Restart Steam now"), lambda: app.run_bg(work))

        here = on_frame()  # FramePort on the Frame itself: Later is the default (GitHub #425)
        steam = ft.Row([C.dropdown(label=tr("Steam library"),
                                  value=steam_library_mode(), width=T.px(360),
                                  options=[ft.DropdownOption("now", tr("Now") if here else tr("Now (default)")),
                                           ft.DropdownOption("later", tr("Later — next Steam start (default)") if here
                                                             else tr("Later — next Steam start")),
                                           ft.DropdownOption("off", tr("Off — launch from FramePort"))],
                                  on_select=steam_changed), C.help_icon("steam_library")], spacing=T.S1)
        def changed(e):
            library.set_setting("install.launch_test", bool(e.control.value))
        def keep_changed(e):
            library.set_setting("build.keep_copies", bool(e.control.value))

        def threads_changed(e):
            library.set_setting("install.upload_threads", upload_threads(e.control.value))

        def video_changed(e):
            library.set_setting("video.hw_decode", bool(e.control.value))
            target = self.app.target if self.app.frame_state == "connected" else None
            if target is not None:  # else applied at the next connection
                self.app.run_bg(target.frame.apply_video_decoding)

        def clean(e):
            def work():
                freed = pipeline.remove_all_converted_copies()
                self.app.toast(tr("Removed the converted copies ({size})").format(size=fmt_size(freed)))
            self.app.run_bg(work)
        keep = C.switch(tr("Keep converted copies on this PC (only needed to inspect a build)"),
                        value=bool(library.setting("build.keep_copies", False)), on_change=keep_changed)
        launch = C.switch(tr("Run a launch test after installing on the Frame"),
                          value=bool(library.setting("install.launch_test", True)),
                          on_change=changed)
        video = ft.Row([ft.Container(C.switch(tr("Hardware video decoding on the Frame"),
                                             value=bool(library.setting("video.hw_decode", True)),
                                             on_change=video_changed), expand=True),
                        C.help_icon("hw_video_decode")], spacing=T.S1)
        parallel = C.dropdown(label=tr("Parallel uploads"), value=str(upload_threads()), width=T.px(240),
                              options=[ft.DropdownOption(str(n), str(n)) for n in range(1, 9)],
                              on_select=threads_changed)
        return ft.Column([steam, C.meta(tr("Now waits while a game, Desktop Mode, or local FramePort is open. "
                                          "Later lets you keep installing without restarting Steam. "
                                          "Off uses FramePort Play without Steam per-game integration.")),
                          C.ghost(tr("Restart Steam now"), ft.Icons.RESTART_ALT, restart),
                          launch, parallel,
                          C.meta(tr("Large files upload together using OpenSSH when available. "
                                    "Default: 3. Lower this on a busy network.")),
                          video, keep,
                          C.ghost(tr("Remove converted copies now"), ft.Icons.CLEANING_SERVICES_OUTLINED, clean,
                                  tooltip=tr("Your own game files aren't touched.")),
                          ft.Container(height=T.S2), self.ssh_keys()],
                         spacing=T.S2, horizontal_alignment=ft.CrossAxisAlignment.START)

    def controllers(self) -> ft.Control:
        from ...core import controllers, library
        from ..controllers import diagram, diagram_source

        app = self.app
        mode = controllers.quest_buttons_default()
        preview = diagram(mode)

        def changed(e):
            nonlocal mode
            mode = int(e.control.value)
            library.set_setting("input.quest_buttons", mode)
            preview.src = diagram_source(mode)
            C.update(preview)

        def apply(e):
            def run(job):
                result = controllers.apply_default(app.target, job.reporter)
                message = tr_n("Applied to {n} game. Takes effect at the next launch.",
                               "Applied to {n} games. Takes effect at the next launch.", len(result["applied"]))
                if result["needs_update"]:
                    message += " " + tr_n("{n} game needs Update on Frame first.",
                                          "{n} games need Update on Frame first.", len(result["needs_update"]))
                return message  # the job completion toast shows this result too
            app.submit(tr("Apply controller buttons to installed games"), run, kind="tool-frame")

        choice = C.dropdown(label=tr("Default controller buttons"),
                            value=str(mode), width=T.px(300),
                            options=[ft.DropdownOption(str(v), tr(label)) for v, label in controllers.CHOICES],
                            on_select=changed)
        # the tester app: installed through its install link (FramePort's own install question), else opened
        if library.game(controllers.TESTER_PACKAGE):
            tester = C.secondary(tr("Open Test Quest Controllers"), ft.Icons.SPORTS_ESPORTS_OUTLINED,
                                 lambda e: app.open_game(controllers.TESTER_PACKAGE),
                                 tooltip=C.tip(HELP["controller_tester"]))
        else:
            from .link_dialog import open_link

            tester = C.secondary(tr("Get Test Quest Controllers"), ft.Icons.SPORTS_ESPORTS_OUTLINED,
                                 lambda e: open_link(app, controllers.TESTER_MANIFEST),
                                 tooltip=C.tip(HELP["controller_tester"]))
        tip = C.callout(ft.Row([
            C.body(tr("Try the modes first: Test Quest Controllers shows which Quest button each Frame button "
                      "sends."), T.TEXT, expand=True),
            tester], vertical_alignment=ft.CrossAxisAlignment.CENTER), "info", ft.Icons.LIGHTBULB_OUTLINE_ROUNDED)
        return ft.Column([
            C.body(tr("Choose which Quest Touch buttons the Frame's face buttons send.")),
            choice, preview, tip,
            C.meta(tr("Used when installing or updating games that follow the global default. "
                      "Each game's Game settings can override it. Changes take effect at the next launch. "
                      "Games installed before this feature need Update on Frame once for the mode to work.")),
            C.link(tr("Controller mapping guide"), REPO_URL + "/blob/main/docs/CONTROLLERS.md"),
            *([C.secondary(tr("Apply to installed games now"), G.SYNC, apply)]
              if app.frame_state == "connected" else []),
        ], spacing=T.S3, horizontal_alignment=ft.CrossAxisAlignment.START)

    def library_folders(self) -> ft.Control:
        """Settings → Library folders: the folders "Rescan folders" scans (core/folders.py, GitHub discussion #351).
        Status (is it there, how many games come from it) is read off the UI thread."""
        from ...core import folders

        app = self.app
        rows = ft.Column(spacing=T.S2)

        def row(st: folders.FolderStatus) -> ft.Control:
            games = tr_n("{n} game in the library", "{n} games in the library", st.games)
            if st.exists:
                icon, color, state = ft.Icons.FOLDER_OUTLINED, T.TEXT_2, games
            else:
                icon, color = ft.Icons.FOLDER_OFF_OUTLINED, T.WARN
                state = tr("Not found (moved, deleted or on a drive that isn't connected)") + " · " + games
            return ft.Row([
                C.as_icon(icon, T.px(18), color),
                ft.Column([C.body(Path(st.path).name or st.path, T.TEXT, max_lines=1,
                                  overflow=ft.TextOverflow.ELLIPSIS),
                           C.meta(st.path, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS, tooltip=st.path),
                           C.meta(state, T.WARN if not st.exists else None)],
                          spacing=0, expand=True),
                C.icon_btn(ft.Icons.DELETE_OUTLINE_ROUNDED, tr("Stop scanning this folder"),
                           on_click=lambda e, s=st: remove(s)),
            ], spacing=T.S2, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        def fill():  # (run_bg: a folder on a sleeping network drive can take a while to answer)
            items = folders.status()
            rows.controls = [row(s) for s in items] or \
                [C.meta(tr("No folders yet. Add one here, or use Scan a folder… in the Library."))]
            C.update(rows)

        def remove(st: folders.FolderStatus):
            def ok():
                folders.remove(st.path)
                app.run_bg(fill)
            C.confirm(app.page, tr("Stop scanning this folder?"),
                      tr_n("Rescan folders won't look in {path} any more. The {n} game from it stays in your library "
                           "(nothing is deleted).",
                           "Rescan folders won't look in {path} any more. The {n} games from it stay in your library "
                           "(nothing is deleted).", st.games, path=st.path),
                      tr("Remove folder"), ok)

        async def add(e):
            path = await C.pick_dir(app.page, dialog_title=tr("Folder with games (APKs or PC VR games)"))
            if not path:
                return
            try:
                folders.add(path)
            except ValueError as exc:
                app.toast(str(exc), error=True)
                return
            app.run_bg(fill)
            app.scan(path, only_new=True)  # its games (new ones, and ones that moved there) come in now

        rows.controls = [C.meta(tr("Checking folders…"))]
        app.run_bg(fill)
        return ft.Column([
            C.meta(tr("Rescan folders in the Library looks in these folders for new games and for games that "
                      "moved there.")),
            rows,
            ft.Row([C.secondary(tr("Add folder…"), ft.Icons.CREATE_NEW_FOLDER_OUTLINED, add),
                    C.ghost(tr("Rescan folders"), ft.Icons.REFRESH_ROUNDED, app.rescan)],
                   spacing=T.S2, run_spacing=T.S2, wrap=True),
        ], spacing=T.S3, horizontal_alignment=ft.CrossAxisAlignment.START)

    def artwork_card(self) -> ft.Control:
        """Settings → Artwork: where automatic art comes from, and the optional SteamGridDB API key (a secret: stored
        in the settings, never logged, redacted from diagnostics; artwork/stores.py)."""
        from ...artwork import stores
        from ...core import library

        app = self.app
        status = C.meta("", visible=False)
        key = C.field(label=tr("SteamGridDB API key (optional)"), value=stores.sgdb_key(), password=True,
                      can_reveal_password=True, hint_text=tr("Not set"), expand=True)

        def show(text: str, color=None) -> None:
            status.value, status.color, status.visible = text, color or T.TEXT_3, bool(text)
            C.update(status)

        def save(e=None) -> None:
            value = (key.value or "").strip()
            if value == stores.sgdb_key():
                return
            library.set_setting(stores.SGDB_KEY_SETTING, value)
            show(tr("Saved. New artwork lookups use SteamGridDB too.") if value
                 else tr("No key: SteamGridDB isn't used."))

        def check(e) -> None:
            save()
            value = (key.value or "").strip()
            if not value:
                show(tr("Paste a key first."), T.WARN)
                return
            show(tr("Checking the key…"))

            def work():
                result = stores.check_sgdb_key(value)
                show({"ok": tr("The key works."),
                      "rejected": tr("SteamGridDB didn't accept this key. Copy it again from your SteamGridDB "
                                     "preferences."),
                      }.get(result, tr("Couldn't reach SteamGridDB. Try again later.")),
                     T.OK if result == "ok" else T.WARN)
            app.run_bg(work)

        def remove(e) -> None:
            key.value = ""
            C.update(key)
            save()

        key.on_submit = save
        key.on_blur = save
        return ft.Column([
            C.meta(tr("Covers come from the Meta store first, then from the Steam store and SideQuest when a game's "
                      "exact name is listed there. A SteamGridDB key adds art for more games.")),
            ft.Row([key, C.help_icon("steamgriddb_key")], spacing=T.S2,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            status,
            ft.Row([C.secondary(tr("Check key"), ft.Icons.KEY_ROUNDED, check),
                    C.ghost(tr("Remove key"), ft.Icons.DELETE_OUTLINE_ROUNDED, remove),
                    C.link(tr("Get a free key"), stores.SGDB_KEY_URL)],
                   spacing=T.S2, run_spacing=T.S2, wrap=True),
        ], spacing=T.S3, horizontal_alignment=ft.CrossAxisAlignment.START)

    def save_backups(self) -> ft.Control:
        """Settings → Data: how many save backups to keep per game (install/saves.py, setting saves.keep)."""
        from ...core import library, winhost
        from ...install import saves

        def changed(e):
            library.set_setting("saves.keep", int(e.control.value))

        def open_root(e):
            saves.root_dir().mkdir(parents=True, exist_ok=True)
            winhost.open_folder(saves.root_dir())
        keep = C.dropdown(label=tr("Save backups to keep per game"), value=str(saves.keep_count()), width=T.px(260),
                          options=[ft.DropdownOption(str(n), str(n))
                                   for n in sorted({*saves.KEEP_CHOICES, saves.keep_count()})],
                          on_select=changed)
        folder_label = C.meta(str(saves.root_dir()), selectable=True)

        def choose_folder(folder):
            def work():
                try:
                    dest = saves.set_folder(folder)
                    folder_label.value = str(dest)
                    C.update(folder_label)
                except saves.SavesError as exc:
                    self.app.toast(str(exc), error=True)
            self.app.run_bg(work)

        async def pick_folder(e):
            folder = await ft.FilePicker().get_directory_path(dialog_title=tr("Choose save backup folder"))
            if folder:
                choose_folder(folder)

        def auto_changed(e):
            library.set_setting("saves.auto", bool(e.control.value))
            self.app._sync_save_snapshots()

        return ft.Column([
            ft.Row([keep, C.help_icon("save_backups"),
                    C.ghost(tr("Open save backups"), ft.Icons.FOLDER_OPEN_ROUNDED, open_root)],
                   spacing=T.S2, wrap=True),
            folder_label,
            ft.Row([C.secondary(tr("Choose backup folder…"), ft.Icons.FOLDER_OPEN_ROUNDED, pick_folder),
                    C.ghost(tr("Use the default"), ft.Icons.RESTORE_ROUNDED, lambda e: choose_folder(None))],
                   spacing=T.S2, wrap=True),
            C.meta(tr("Existing backups stay in their original folder and remain available.")),
            C.switch(tr("Back up saves when a game closes"), value=bool(library.setting("saves.auto", False)),
                     on_change=auto_changed),
            C.meta(tr("Keeps three snapshots per game on the Frame and copies new ones here when connected.")),
        ], spacing=T.S2)

    def ssh_keys(self) -> ft.Control:
        """Settings → Installing: private key files FramePort logs in with besides its own (frame.connection
        EXTRA_KEYS_SETTING; the Frame connection and OpenSSH uploads both use them, GitHub #242)."""
        from ...frame import connection

        app = self.app
        rows = ft.Column(spacing=T.S1)

        def fill():
            items = []
            for path, problem in connection.extra_key_status():
                items.append(ft.Row([
                    C.as_icon(ft.Icons.KEY_ROUNDED if not problem else ft.Icons.WARNING_AMBER_ROUNDED, T.px(18),
                              T.TEXT_2 if not problem else T.WARN),
                    ft.Column([C.body(path.name, T.TEXT), C.meta(problem or str(path), selectable=True)],
                              spacing=0, expand=True),
                    C.icon_btn(ft.Icons.DELETE_OUTLINE_ROUNDED, tr("Stop using this key (the file stays)"),
                               on_click=lambda e, p=path: remove(p)),
                ], spacing=T.S2, vertical_alignment=ft.CrossAxisAlignment.CENTER))
            rows.controls = items or [C.meta(tr("Only FramePort's own key, and your SSH agent's keys."))]
            C.update(rows)

        def remove(path):
            connection.remove_extra_key(path)
            fill()

        async def add(e):
            for path in await C.pick_files(app.page, dialog_title=tr("Private SSH key file (not the .pub file)"),
                                           multiple=True):
                try:
                    stored = connection.add_extra_key(path)
                except connection.KeyFileError as exc:
                    app.toast(tr("That key can't be used: {error}").format(error=exc), error=True)
                    continue
                app.toast(tr("FramePort also logs in with {name} from the next connection.").format(name=stored.name))
            fill()

        fill()
        return ft.Column([
            ft.Row([C.body(tr("SSH keys"), T.TEXT, weight=ft.FontWeight.W_500), C.help_icon("ssh_keys")],
                   spacing=T.px(4), vertical_alignment=ft.CrossAxisAlignment.CENTER),
            rows,
            C.secondary(tr("Add a key file…"), ft.Icons.VPN_KEY_ROUNDED, add),
        ], spacing=T.S2, horizontal_alignment=ft.CrossAxisAlignment.START)

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
                  if found else C.meta(tr("You have the latest version (checked {when}).")
                                       .format(when=when)))

        def auto_check(e):
            library.set_setting("update.auto_check", bool(e.control.value))

        def auto_install(e):
            library.set_setting("update.auto_install", bool(e.control.value))
        kind = {"bundle": tr("the downloaded app"), "source": tr("a source checkout (git pull + uv sync)"),
                "wheel": tr("a Python package")}[updates.install_kind()]
        return ft.Column([
            ft.Row([C.kv(tr("Installed"), tr("FramePort {version} · {kind}").format(version=__version__, kind=kind)),
                    ft.Container(expand=True),
                    C.secondary(tr("Check for updates"), ft.Icons.REFRESH_ROUNDED, lambda e: app.updater.check_now())],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            status,
            C.switch(tr("Check for new versions automatically"), value=bool(library.setting("update.auto_check", True)),
                      on_change=auto_check),
            C.switch(tr("Install updates automatically when FramePort starts"),
                     value=bool(library.setting("update.auto_install", False)),
                      on_change=auto_install),
            ft.Row([C.meta(tr("What changed in this version and the ones before it."), expand=True),
                    C.ghost(tr("What's new…"), ft.Icons.NEW_RELEASES_OUTLINED, lambda e: app.updater.show_history())],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Divider(height=1, color=T.BORDER),
            ft.Row([C.body(tr("Games"), T.TEXT, weight=ft.FontWeight.W_500), C.help_icon("game_updates")],
                   spacing=T.px(4), vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self.catalog_updates(),
            self.link_updates(),
            ft.Divider(height=1, color=T.BORDER),
            ft.Row([C.meta(tr("Asked to test something? Dev builds come before the next release."), expand=True),
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
            C.switch(tr("Update game recipes from GitHub automatically"),
                     value=bool(library.setting("catalog.auto_update", True)),
                     on_change=toggle),
        ], spacing=T.S2)

    def link_updates(self) -> ft.Control:
        """Settings → Updates → Games: newer versions of games installed from install links whose manifest names a
        version (pipeline.check_link_updates; setting links.check_updates)."""
        import time

        from ...core import library

        app = self.app
        last = library.setting("links.last_update_check")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else tr("never")
        n = sum(1 for g in library.games() if g.get("link_update"))
        line = tr("Games from install links: last checked {when}").format(when=when)
        if n:
            line += " · " + tr_n("{n} update available", "{n} updates available", n)

        def toggle(e):
            library.set_setting("links.check_updates", bool(e.control.value))
        return ft.Column([
            ft.Row([C.meta(line, expand=True),
                    C.ghost(tr("Check now"), ft.Icons.SYNC_ROUNDED,
                            lambda e: app.run_bg(lambda: app._check_link_updates(force=True)))],
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            C.switch(tr("Check games from install links for new versions"),
                     value=bool(library.setting("links.check_updates", True)), on_change=toggle),
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
                    state.value = tr("This system can't send web links to FramePort. Use Add games → Add from a "
                                     "link… instead.")
                elif not urlhandler.enabled(scheme):
                    state.value = tr("Off: these links open whatever app is set up for them.")
                elif last.get(scheme) == "other":
                    state.value = tr("{app} opens these links on this PC.").format(
                        app=last.get(f"{scheme}_by") or tr("Another app"))
                    take.visible = True
                elif last.get(scheme) == "ours":
                    state.value = tr("These links open FramePort.")
                else:
                    state.value = tr("Not set up yet. FramePort sets it up when it starts.")
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
                          tr("These links will open FramePort instead of {app}. Turn this setting off to switch "
                             "back.").format(app=other),
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
            path = next(iter(await C.pick_files(app.page, dialog_title=tr("Theme file to install"),
                                                extensions=["json"])), None)
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
                                       app.toast(tr("Copied. Save it as a .json file, change the colors and "
                                                    "install it.")))),
                    C.secondary(tr("Install theme file…"), ft.Icons.FILE_OPEN_OUTLINED, install)],
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
        live_card = C.switch(tr("Show the running game on the Frame card"), value=self.app.live_card,
                             on_change=lambda e: self.app.set_live_card(e.control.value))
        live_note = C.meta(tr("Updates the game, frame rate and battery every 5 seconds. Turn off to save the "
                              "Frame's power."))
        motion = C.switch(tr("Reduce motion"),
                          value=self.app.reduce_motion, on_change=lambda e: self.app.set_reduce_motion(e.control.value))
        controls = [self.theme_picker(), ft.Container(height=T.S2), dd, note, ft.Container(height=T.S2), live_card,
                    live_note, motion]
        languages = i18n.available()  # first: whoever needs it needs it before anything else
        if len(languages) > 1:  # only once a translation exists
            def language_changed(e):
                library.set_setting("ui.language", e.control.value)
                C.update(lang_note)
            lang = C.dropdown(label=tr("Language"), value=i18n.language(), width=T.px(260), on_select=language_changed,
                               options=[ft.dropdown.Option(code, i18n.language_name(code)) for code in languages])
            lang_note = C.meta(tr("Changes apply the next time FramePort starts."))
            controls = [lang, lang_note, ft.Container(height=T.S2), *controls]
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

    def index_column(self) -> ft.Control:
        """The page's index (left): one entry per section; a click scrolls there and highlights the entry."""
        def entry(key: str, label: str) -> ft.Container:
            text = C.body(label, T.TEXT_2, weight=ft.FontWeight.W_500)

            async def click(e, k=key):
                await self.show_section(k)
            box = ft.Container(text, padding=ft.Padding(T.S3, T.px(7), T.S3, T.px(7)), border_radius=T.RADIUS_SM,
                               ink=True, on_click=click, data=text)
            self.index[key] = box
            return box
        titles = section_titles()
        col = ft.Column([entry(k, titles[k]) for k in SECTIONS], spacing=T.px(2))
        self._mark(SECTIONS[0])
        return ft.Container(col, width=T.px(180), padding=ft.Padding(0, T.px(2), T.S4, 0))

    def _mark(self, current: str) -> None:
        for key, box in self.index.items():
            C.selected_style(box, key == current, text=box.data)

    async def show_section(self, key: str) -> None:
        """Scroll the page to a section (index click; ui_smoke calls it too)."""
        self._mark(key)
        C.update(*self.index.values())
        if self.page_col is not None:
            try:
                await self.page_col.scroll_to(scroll_key=scroll_key(key),
                                              duration=0 if self.app.reduce_motion else 300)
            except RuntimeError:  # the page left the screen (or the window closed) before the scroll ran
                pass

    def build(self) -> ft.Control:
        app = self.app
        self.tools.controls = [C.checklist([C.Check(None, tr("Checking tools…"))])]
        app.run_bg(self.fill_tools)
        self.pc.controls = [C.checklist([C.Check(None, tr("Checking this PC…"))])]
        app.run_bg(self.fill_pc)
        from ... import __version__ as ver
        data = str(user_data_dir())
        sections = {
            "appearance": C.section(tr("Appearance"), C.card(self.appearance(), padding=T.S4), help="ui_scale"),
            "updates": C.section(tr("Updates"), C.card(self.updates_card(), padding=T.S4), help="app_updates"),
            "links": C.section(tr("Install links"), C.card(self.links_card(), padding=T.S4), help="install_links"),
            "folders": C.section(tr("Library folders"), C.card(self.library_folders(), padding=T.S4),
                                 help="library_folders"),
            "artwork": C.section(tr("Artwork"), C.card(self.artwork_card(), padding=T.S4), help="artwork_sources"),
            "installing": C.section(tr("Installing"), C.card(self.installing(), padding=T.S4), help="launch_test"),
            "controllers": C.section(tr("Controllers"), C.card(self.controllers(), padding=T.S4),
                                     help="quest_buttons",
                                     badge=C.pill(tr("experimental"), T.WARN, tooltip=C.tip(HELP["experimental"]))),
            "tools": C.section(tr("Tools"), C.card(self.tools, padding=ft.Padding(T.S4, T.S2, T.S4, T.S2)),
                               subtitle=tr("FramePort keeps its own copies; nothing is installed system-wide"),
                               action=ft.Row([C.ghost(tr("Update tools"), ft.Icons.UPDATE_ROUNDED,
                                                      lambda e: app.update_tools(update=True)),
                                              C.secondary(tr("Install missing"), ft.Icons.DOWNLOAD_ROUNDED,
                                                          lambda e: app.update_tools())], spacing=T.S2)),
            "pc": C.section(tr("This PC (for PC VR games)"),
                            C.card(self.pc, padding=ft.Padding(T.S4, T.S2, T.S4, T.S2))),
            "data": C.section(tr("Data"), C.card(ft.Column([
                C.kv(tr("Data folder"), ft.Row([C.body(data, T.TEXT, selectable=True, expand=True),
                                            C.icon_btn(ft.Icons.CONTENT_COPY_ROUNDED, tr("Copy path"),
                                                       lambda e: app.copy(data))]), "data_folder"),
                C.kv(tr("Catalog"), tr("{len} tested recipes (built in, online and yours)")
                     .format(len=len(catalog.load())), "catalog"),
                self.save_backups(),
            ], spacing=T.S2))),
            # text above, buttons below (side by side, the buttons squeezed the text in a narrow window)
            "feedback": C.section(tr("Problems and feedback"), C.card(ft.Column([
                C.body(tr("Something not working? Attach a diagnostics zip to a GitHub issue; personal data is "
                       "removed. For one game, use its menu instead.")),
                ft.Row([C.secondary(tr("Report a problem…"), ft.Icons.BUG_REPORT_OUTLINED,
                                    lambda e: app.report_problem_dialog()),
                        C.ghost(tr("Collect FramePort logs"), ft.Icons.FOLDER_ZIP_OUTLINED,
                                lambda e: app.collect_logs())],
                       spacing=T.S3, run_spacing=T.S2, wrap=True),
            ], spacing=T.S3)), help="diag_bundle"),
            "about": C.section(tr("About"), C.card(ft.Column([
                C.kv(tr("Version"), ver),
                C.kv(tr("Frame agent"), self.agent_text(), "frame_agent"),
                C.kv(tr("Source"), C.link(REPO_URL.removeprefix("https://"), REPO_URL)),
                C.meta(tr("Uses OVRPort, Revive (LibreVR), Valve's Lepton and Proton. "
                          "Not affiliated with Valve or Meta.")),
            ], spacing=T.S2))),
            "remove": C.section(tr("Uninstall FramePort"), C.card(ft.Column([
                C.body(tr("Removes FramePort's data, tools and Steam entries, and optionally its games on the "
                       "Frame. Your game files stay.")),
                C.danger(tr("Uninstall FramePort…"), ft.Icons.DELETE_FOREVER_OUTLINED, lambda e: app.uninstall_app(),
                         outline=True),
            ], spacing=T.S3, horizontal_alignment=ft.CrossAxisAlignment.START))),
        }
        # the bottom padding lets the last sections scroll up to the top too
        self.page_col = ft.Column([ft.Container(sections[k], key=ft.ScrollKey(scroll_key(k))) for k in SECTIONS]
                                  + [ft.Container(height=T.px(240))],
                                  spacing=T.S5, scroll=ft.ScrollMode.AUTO, expand=True)
        return ft.Column([
            app.top_bar(tr("Settings"), tr("Updates, installing, appearance and where FramePort keeps its data")),
            ft.Row([self.index_column(), self.page_col], expand=True, spacing=0,
                   vertical_alignment=ft.CrossAxisAlignment.START),
        ], spacing=T.S5, expand=True)
