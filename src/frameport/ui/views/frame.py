"""Frame page: the connected device with a readiness checklist and installed games, or a connect wizard."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import flet as ft

from ...core import library
from ...errors import explain
from ...i18n import tr, tr_n
from .. import components as C
from .. import glyphs as G
from .. import theme as T
from ..battery import label as battery_label
from ..help import HELP

if TYPE_CHECKING:
    from ..app import FramePortApp


class FrameView:
    def __init__(self, app: FramePortApp):
        self.app = app

    # ---------------------------------------------------------------- connected
    def device_card(self, info: dict) -> ft.Control:
        app = self.app
        t = app.target
        free = (info.get("free_bytes") or 0) / 2**30
        return C.card(ft.Row([
            ft.Container(C.as_icon(G.FRAME, T.px(34), T.ACCENT), width=T.px(72),
                         height=T.px(72), border_radius=T.RADIUS, bgcolor=T.ACCENT_SOFT, alignment=ft.Alignment.CENTER),
            ft.Column([
                ft.Row([C.title(info.get("hostname") or t.label, T.T_DISPLAY),
                        C.pill(tr("Connected"), T.OK, ft.Icons.CIRCLE)], spacing=T.S3),
                C.body(tr("{user}@{host} · {get} {get2} (build {get3})")
                       .format(user=t.target.user, host=t.target.host, get=info.get('os'),
                               get2=info.get('os_version'), get3=info.get('build_id'))),
                C.meta(tr("{free:.0f} GiB free · {len} games installed")
                       .format(free=free, len=len(info.get('installed') or []))
                       + (tr(" · battery {value}").format(value=battery_label(info["battery"]))
                          if info.get("battery") else "")),
            ], spacing=T.px(4), expand=True),
            ft.Column([
                C.secondary(tr("Refresh"), ft.Icons.REFRESH_ROUNDED, lambda e: app.refresh_frame()),
                C.secondary(tr("Type on Frame"), G.KEYS, lambda e: app.type_on_frame(),
                            tooltip=tr("Use this PC's keyboard on the Frame")),
                C.ghost(tr("Switch Frame…"), ft.Icons.SWAP_HORIZ_ROUNDED, lambda e: self.switch_frame()),
                C.ghost(tr("Disconnect"), ft.Icons.LINK_OFF_ROUNDED, lambda e: app.disconnect()),
            ], spacing=T.S2, horizontal_alignment=ft.CrossAxisAlignment.END),
        ], spacing=T.S4), padding=T.S5)

    def switch_frame(self) -> None:
        """A small picker of the remembered Frames (click one to connect), or "Find another Frame…" = the setup and
        discovery page."""
        from ...frame.connection import saved_targets

        app = self.app
        page = app.page
        current = getattr(getattr(app.target, "target", None), "host", None)

        def pick(target):
            page.pop_dialog()
            app.connect(target)

        def find(e):
            page.pop_dialog()
            app.disconnect()
        rows = []
        for f in saved_targets():
            here = f.host == current
            rows.append(ft.Container(ft.Row([
                C.as_icon(G.FRAME, T.px(22), T.ACCENT),
                ft.Column([C.body(f.label, T.TEXT, weight=ft.FontWeight.W_500),
                           C.meta(f"{f.user}@{f.host}")], spacing=T.px(2), expand=True),
                C.pill(tr("Connected"), T.OK, ft.Icons.CIRCLE) if here else
                C.secondary(tr("Connect"), on_click=lambda e, f=f: pick(f)),
            ], spacing=T.S3), padding=ft.Padding(T.S3, T.px(8), T.S2, T.px(8)), bgcolor=T.SURFACE_3,
                border_radius=T.RADIUS_SM))
        if not rows:
            rows = [C.body(tr("No other Frames remembered yet."))]
        page.show_dialog(C.dialog(
            tr("Switch Frame"), ft.Column(rows, spacing=T.S2, tight=True, scroll=ft.ScrollMode.AUTO),
            [C.ghost(tr("Cancel"), on_click=lambda e: page.pop_dialog()),
             C.secondary(tr("Find another Frame…"), ft.Icons.SEARCH_ROUNDED, find)], size="s"))

    def readiness(self, info: dict) -> ft.Control:
        app = self.app
        items = []
        lepton = info.get("lepton")
        items.append(C.Check(bool(lepton), tr("Quest games (Lepton)"),
                             tr("Ready") if lepton
                             else tr("Valve's Android runtime isn't installed (needs Developer Mode)"),
                             "lepton", (tr("Install"), ft.Icons.DOWNLOAD_ROUNDED, app.install_lepton)))
        pr = info.get("proton") or {}
        ready, sug = pr.get("ready"), pr.get("suggested")
        if pr:
            items.append(C.Check(
                bool(ready), tr("PC VR games (Proton)"),
                tr("{display_name} installed").format(display_name=ready['display_name']) if ready else
                (tr("{display_name} can be installed (about 1 GiB; Steam restarts once)")
                 .format(display_name=sug['display_name']) if sug else
                 tr("Not offered by Steam on this Frame yet")),
                "proton", (tr("Install…"), ft.Icons.DOWNLOAD_ROUNDED, app.install_proton) if sug else None,
                C.secondary(tr("Test"), G.TEST, lambda e: app.test_proton()) if ready else None))
            xr = (pr.get("openxr") or {}).get("name")
            items.append(C.Check(bool(xr), tr("OpenXR runtime"), xr or tr("None found"), "openxr"))
        keys = info.get("kernel_keys") or {}
        if keys.get("max_keys"):
            high = keys["keys"] > keys["max_keys"] * 0.75
            items.append(C.Check(not high if keys["keys"] < keys["max_keys"] else False, tr("Game launch capacity"),
                                 tr("{used}/{max} kernel keys used").format(used=keys["keys"], max=keys["max_keys"])
                                 + (tr(" · restart the Frame soon to reset it") if high else ""),
                                 "kernel_keys",
                                 (tr("Restart the Frame…"), ft.Icons.RESTART_ALT_ROUNDED,
                                  lambda: app.frame_power("restart"))))
        return C.section(tr("Ready to play"), C.card(C.checklist(items), padding=ft.Padding(T.S4, T.S2, T.S4, T.S2)))

    def storage(self, info: dict) -> ft.Control:
        """Drives games can go on (GitHub #90) and where new games go. The list comes from the Frame in the
        background."""
        from ...install import drives

        app = self.app
        current = drives.install_dest()
        pick = C.dropdown(label=tr("Install new games to"), value=current or drives.INTERNAL, width=T.px(380),
                          dense=True, text_size=T.T_BODY, disabled=True,
                          options=[ft.DropdownOption(key=drives.INTERNAL, text=tr("Internal storage"))])
        rows = ft.Column([C.meta(tr("Looking for drives…"))], spacing=T.px(6))

        def choose(e):
            value = e.control.value or drives.INTERNAL
            drives.set_install_dest(None if value == drives.INTERNAL else value)
            label = next((o.text for o in pick.options if o.key == value), value)
            app.toast(tr("New games go to {label}").format(label=label))

        pick.on_select = choose

        def name(d: dict) -> str:
            return tr("Internal storage") if d["internal"] else d["label"]

        def fill():
            try:
                found = app.target.drives() if app.target else []
            except Exception as exc:  # noqa: BLE001 - shown in place of the list
                rows.controls = [C.meta(tr("Couldn't list the Frame's drives: {error}").format(error=explain(exc)))]
                C.update(rows)
                return
            opts, lines = [], []
            for d in found:
                free = drives.free_text(d)
                if d.get("usable"):
                    opts.append(ft.DropdownOption(key=drives.INTERNAL if d["internal"] else d["install_dir"],
                                                  text=" · ".join(filter(None, [name(d), free]))))
                state = tr_n("{n} game", "{n} games", d.get("games", 0)) if d.get("usable") else \
                    tr("can't hold games: {reason}").format(reason=d.get("reason") or "")
                lines.append(ft.Row([
                    ft.Icon(ft.Icons.SD_CARD_ROUNDED if d.get("removable") else ft.Icons.STORAGE_OUTLINED,
                            size=T.px(18), color=T.TEXT_2 if d.get("usable") else T.TEXT_3),
                    C.body(name(d), T.TEXT if d.get("usable") else T.TEXT_3, expand=True),
                    C.meta(" · ".join(filter(None, [d.get("fstype"), free, state]))),
                ], spacing=T.S2))
            if current and not any(o.key == current for o in opts):  # the chosen card isn't inserted now
                label = current.rstrip("/").rsplit("/", 2)[-2] if current.rstrip("/").endswith("/FramePort") \
                    else current
                opts.append(ft.DropdownOption(key=current, text=tr("{label} (not inserted)").format(label=label)))
            pick.options = opts or pick.options
            pick.disabled = len(opts) < 2
            rows.controls = lines or [C.meta(tr("Only internal storage"))]
            C.update(pick)
            C.update(rows)

        app.run_bg(fill)
        return C.section(tr("Storage"), C.card(ft.Column([pick, rows], spacing=T.S3), padding=T.S4),
                         help="install_drive")

    def installed(self, info: dict) -> ft.Control:
        """The list fills in the background (icon thumbnails may need creating the first time)."""
        items = info.get("installed") or []
        col = ft.Column([ft.Container(bgcolor=T.SURFACE_2, height=T.px(56), border_radius=T.RADIUS_SM, opacity=0.5)
                         for _ in items[:6]], spacing=T.px(4))
        self.app.run_bg(self._fill_installed, items, col)
        body = C.card(col, padding=T.S2) if items else \
            C.card(C.body(tr("Nothing installed yet. Pick a game in the Library and click Install.")), padding=T.S5)
        send = C.ghost(tr("Files"), ft.Icons.FOLDER_OPEN_ROUNDED, lambda e: self.app.go("files"),
                       tooltip=C.tip(HELP["files"]))
        return C.section(tr("Installed games ({len})").format(len=len(items)), body,
                         action=ft.Row([send, C.ghost(tr("Free up space…"), ft.Icons.CLEANING_SERVICES_OUTLINED,
                                                      lambda e: self.app.cleanup_frame(),
                                                      tooltip=C.tip(HELP["free_space"]))], spacing=T.S2, tight=True)
                         if items else send)

    def _fill_installed(self, items: list[dict], col: ft.Column) -> None:
        def settings_slot() -> ft.Control:
            spacer = C.icon_btn(ft.Icons.TUNE_ROUNDED, "", None, disabled=True)
            spacer.opacity = 0
            return spacer

        from ...artwork import thumbs
        from ...install.drives import game_drive as drive_of
        from .files_dialog import show_files_dialog
        from .library import display_title, twins

        app = self.app
        games = {g["package"]: g for g in library.games()}
        tw = twins(list(games.values()))
        rows = []
        for d in items:
            pkg = d["package"]
            pcvr = d.get("kind") == "pcvr"
            art = thumbs.url(pkg, ("icon", "square", "portrait"), 96)
            size = d.get("apk_size", 0)
            in_lib = pkg in games
            sub = (tr("PC VR · Proton") + (tr(" + Revive") if d.get("revive") else "")) if pcvr else \
                (C.platform(games[pkg])[0] if in_lib else tr("Linux") if d.get("kind") == "linux" else tr("Quest"))
            sub += (tr(" · {value:.1f} GiB").format(value=size / 2**30) if size >= 2**30
                    else tr(" · {value:.0f} MiB").format(value=size / 2**20)) if size or not d.get("drive_missing") \
                else ""
            sub += C.drive_note(drive_of(d))
            title = display_title(games[pkg], tw) if in_lib else (d.get("title") or pkg)
            rows.append(ft.Container(ft.Row([
                C.art_fill(art, radius=T.RADIUS_SM, width=T.px(44), height=T.px(44)),
                ft.Column([C.body(title, T.TEXT, weight=ft.FontWeight.W_500), C.meta(sub)],
                          spacing=T.px(2), expand=True),
                # same slots on every row: games without settings (PC VR, 2D apps) get an invisible spacer
                (settings_slot() if pcvr or not app.has_game_settings(games.get(pkg)) else
                 C.icon_btn(ft.Icons.TUNE_ROUNDED, C.tip(tr("Game settings. ") + HELP["adapter_settings"]),
                            lambda e, p=pkg: app.settings_dialog(p))),
                C.icon_btn(ft.Icons.FOLDER_OPEN_ROUNDED, tr("Files on the Frame: browse what this install contains"),
                           lambda e, p=pkg, t=title: show_files_dialog(app, p, t)),
                C.icon_btn(ft.Icons.PLAY_ARROW_ROUNDED, tr("Play: starts the game through the Frame's Steam (put the "
                           "headset on)"), lambda e, p=pkg: app.play(p, "frame"), color=T.ACCENT),
                C.icon_btn(G.TEST, C.tip(tr("Launch test. ") + HELP["launch_test"]),
                           lambda e, p=pkg: app.test_game(p, "frame")),
                C.icon_btn(ft.Icons.DELETE_OUTLINE_ROUNDED, C.tip(tr("Uninstall. ") + HELP["uninstall"]),
                           lambda e, p=pkg: app.uninstall(p, "frame")),
            ], spacing=T.S3), padding=ft.Padding(T.S3, T.px(8), T.S2, T.px(8)), border_radius=T.RADIUS_SM,
                on_click=(lambda e, p=pkg: app.open_game(p)) if in_lib else None, ink=in_lib))
        col.controls = rows
        col.spacing = 0
        C.update(col)

    # ---------------------------------------------------------------- not connected
    def wizard(self) -> ft.Control:
        from ...frame.connection import parse_target, saved_targets

        app = self.app
        found = ft.Column(spacing=T.S2)
        searching = ft.Row([C.spinner(),
                            C.meta(tr("Looking for Frames on your network…"))], spacing=T.S2)

        def discover():
            from ...frame.discovery import browse

            found.controls = [searching]
            C.update(found)
            res = browse(4)
            items = []
            for f in res:
                label = {"devkit": tr("SteamOS · Developer Mode"), "frameport": tr("Set up for FramePort"),
                         "saved": tr("Remembered"), "scan": tr("SSH found by network scan")}.get(f.source, f.source)
                items.append(ft.Container(ft.Row([
                    C.as_icon(G.FRAME if f.source != "scan" else ft.Icons.DEVICES_OTHER_ROUNDED, T.px(24), T.ACCENT),
                    ft.Column([C.body(f.name if f.source != "scan" else f.host, T.TEXT, weight=ft.FontWeight.W_500),
                               C.meta(f"{f.host} · {label}")], spacing=T.px(2), expand=True),
                    C.primary(tr("Connect"), on_click=lambda e, f=f: app.connect(parse_target(f"{f.user}@{f.host}"),
                                                                                   devkit=f.source == "devkit")),
                ], spacing=T.S3), padding=ft.Padding(T.S3, T.px(8), T.S2, T.px(8)), bgcolor=T.SURFACE_2,
                    border_radius=T.RADIUS_SM))
            found.controls = items or [ft.Row([C.body(tr("No Frames found. New Frame? Use first-time setup (step "
                                                         "1). Otherwise turn on Developer Mode and keep the Frame on "
                                                         "the same network."), expand=True),
                                               C.help_icon("developer_mode")], spacing=T.px(4))]
            found.controls.append(C.ghost(tr("Search again"), ft.Icons.REFRESH_ROUNDED, lambda e: app.run_bg(discover)))
            C.update(found)

        pair_box = ft.Column(spacing=T.S3)

        def pair(e, host: str = ""):
            from ...frame.connection import FrameTarget
            from ...frame.pairing import PairingServer

            if app.pairing:
                app.pairing.stop()

            def on_paired(info):
                app.connect(FrameTarget(info["host"], info["user"], 22, info["name"]))
            server = app.pairing = PairingServer(on_paired=on_paired, host=host).start()
            show_command()

            def check_network():
                from ...core import winhost
                from ...frame.pairing import HINT_AFTER, ensure_reachable, firewall_hint

                if winhost.is_wsl() and ensure_reachable(server) == "failed":
                    app.toast(tr("Windows' firewall for WSL blocks the Frame from reaching FramePort. Open the setup "
                                 "command again and allow the change when Windows asks (admin)."), error=True)
                for _ in range(HINT_AFTER):
                    time.sleep(1)
                    if server.requests or not server.running or app.pairing is not server:
                        return
                server.hint = firewall_hint(server.port) or tr(
                    "Check that the Frame and this PC are on the same network.")
                if app.route[0] == "frame" and app.pairing is server:
                    show_command()
            app.run_bg(check_network)

        def usb_explain(e):
            """The Frame turns its USB network on only in Developer Mode: say how first (Valve's labels on the current
            Frame OS: Settings → System → Enable Developer Mode), then watch for the cable."""
            def close():  # this dialog itself: pop_dialog() closes the topmost overlay, which may be a toast
                dlg.open = False
                C.update(dlg)

            def go(ev):
                close()
                usb_setup(ev)
            steps = [
                tr("On the Frame, open Steam's Settings (Steam button → Settings)."),
                tr("Go to System and turn on \"Enable Developer Mode\". A Developer page appears in Settings; nothing "
                   "else changes, your games and data stay as they are."),
                tr("Connect the Frame's USB-C port to this PC with a USB cable (a data cable, not a charge-only "
                   "one)."),
            ]
            dlg = C.dialog(
                tr("Turn on Developer Mode first"),
                ft.Column([
                    C.body(tr("The Frame only offers its USB connection in Developer Mode:"), T.TEXT),
                    *[ft.Row([ft.Container(ft.Text(str(i), weight=ft.FontWeight.W_700, color=T.ACCENT),
                                           width=T.px(24), height=T.px(24), border_radius=T.px(12),
                                           bgcolor=T.ACCENT_SOFT, alignment=ft.Alignment.CENTER),
                              C.body(text, T.TEXT_2, expand=True)], spacing=T.S3,
                             vertical_alignment=ft.CrossAxisAlignment.START)
                      for i, text in enumerate(steps, 1)],
                    C.meta(tr("FramePort then finds the Frame on the cable by itself.")),
                ], spacing=T.S3, tight=True), size="s",
                actions=[C.ghost(tr("Cancel"), on_click=lambda ev: close()),
                         C.primary(tr("Done: look for the cable"), ft.Icons.USB_ROUNDED, go)])
            app.page.show_dialog(dlg)

        def usb_setup(e):
            """Setup over a USB-C cable: no Wi-Fi, router, discovery or network firewall involved (the Frame's USB
            network gives this PC the fixed address 10.86.200.234). The Frame enables its USB network only in
            Developer Mode, so that comes first."""
            from ...frame import usb
            from ...frame.connection import FrameTarget, server_key

            status = ft.Row([C.spinner(),
                             C.meta(tr("Waiting for the cable…"))], spacing=T.S2)
            pair_box.controls = [
                C.body(tr("Looking for the Frame on a USB cable (Developer Mode on, USB-C port connected to this "
                          "PC)."), T.TEXT),
                status,
            ]
            C.update(pair_box)
            token = object()
            app._usb_setup = token

            def watch():
                for _ in range(600):  # up to 20 minutes
                    if app._usb_setup is not token or app.route[0] != "frame":
                        return
                    if usb.PC_USB_IP in usb.pc_usb_addresses(usb.FRAME_USB_IP) and server_key(usb.FRAME_USB_IP,
                                                                                                timeout=3):
                        break
                    time.sleep(2)
                else:
                    return
                status.controls = [ft.Icon(ft.Icons.USB_ROUNDED, size=T.px(16), color=T.OK),
                                   C.meta(tr("Cable connected."), T.OK)]
                C.update(status)
                target = FrameTarget(usb.FRAME_USB_IP, "steamos", 22)
                try:  # already set up for FramePort: just connect over the cable
                    from ...frame.connection import Frame

                    Frame(target, None).connect(timeout=10).close()
                    app.connect(target)
                    return
                except Exception:  # noqa: BLE001 - not set up yet: show the setup command for the cable
                    pass
                app.page.run_thread(lambda: pair(None, host=usb.PC_USB_IP))
            app.run_bg(watch)

        def show_command(update=True):
            """The setup command of the running pairing server: also when the page is redrawn (connection checks,
            discovery), which used to close it."""
            line = app.pairing.one_liner
            over_usb = bool(getattr(app.pairing, "host", ""))
            pair_box.controls = [
                C.body(tr("On the Frame: SteamVR dashboard → Launch a program → Desktop, then app menu → System → "
                          "Konsole, and run (it reaches this PC over the USB cable):") if over_usb else
                       tr("On the Frame: SteamVR dashboard → Launch a program → Desktop, then app menu → System → "
                          "Konsole, and run:"), T.TEXT),
                ft.Container(ft.Row([ft.Text(line, font_family="monospace", selectable=True, size=T.px(12),
                                             color=T.TEXT, expand=True),
                                     C.icon_btn(ft.Icons.CONTENT_COPY_ROUNDED, tr("Copy"), lambda e: app.copy(line))]),
                             padding=ft.Padding(T.S3, T.S2, T.S2, T.S2), bgcolor=T.BG, border_radius=T.RADIUS_SM,
                             border=ft.Border.all(1, T.BORDER)),
                ft.Row([C.spinner(),
                        C.meta(tr("Waiting for your Frame… (code {code})").format(code=app.pairing.code))],
                       spacing=T.S2),
                ft.Row([C.meta(tr("You only do this once: FramePort connects by itself when the setup has finished."),
                               expand=True), C.help_icon("first_time_setup")], spacing=T.px(4)),
            ]
            hint = getattr(app.pairing, "hint", "")
            if hint and not app.pairing.requests:  # nothing reached us yet: what may block it, and the other way in
                pair_box.controls.append(C.callout(ft.Column([
                    C.body(tr("Nothing has reached FramePort from the Frame yet (curl says “timed out”)?"), T.TEXT,
                           weight=ft.FontWeight.W_600),
                    C.body(hint, T.TEXT),
                    ft.Row([C.meta(tr("Or turn on Developer Mode on the Frame (Settings → System → Enable Developer "
                                      "Mode) and pair it under step 2: that needs no connection into this PC."),
                                   expand=True), C.help_icon("developer_mode")], spacing=T.px(4)),
                ], spacing=T.S2), "warn"))
            if update:
                C.update(pair_box)  # (a redraw of the page may have replaced it)

        if app.pairing and app.pairing.running:
            show_command(update=False)

        addr = C.field(hint_text=tr("steamos@frame.local or an IP address"), width=T.px(320))
        pw = C.field(hint_text=tr("Password (first time only)"), password=True, can_reveal_password=True,
                     width=T.px(240), tooltip=C.tip(HELP["password"]))
        saved = saved_targets()
        offline = None
        if getattr(app, "_not_paired", False):  # it answered, but doesn't know FramePort yet
            offline = C.callout(ft.Row([
                C.body(tr("Your Frame isn't set up for FramePort yet. Run the first-time setup once (step 1)."), T.TEXT,
                       expand=True),
                C.primary(tr("Show setup command"), ft.Icons.TERMINAL_ROUNDED, pair)]), "warn")
        elif saved and app.frame_state == "offline":
            unreachable = tr("{label} ({host}) isn't reachable. Make sure it's switched on and on the same "
                             "network.").format(label=saved[0].label, host=saved[0].host)
            offline = C.callout(ft.Row([C.body(unreachable, T.TEXT, expand=True),
                                        C.secondary(tr("Try again"), ft.Icons.REFRESH_ROUNDED,
                                                    lambda e: app.connect(saved[0]))]), "warn")
        elif app.frame_state == "connecting":
            connecting = tr("Connecting to {value}…").format(value=saved[0].label if saved else tr("your Frame"))
            offline = C.callout(ft.Row([C.spinner(),
                                        C.body(connecting, T.TEXT)],
                                       spacing=T.S3), "info")
        import time as _time

        if _time.time() - getattr(app, "_discovered_at", 0) > 20:  # not on every redraw
            app._discovered_at = _time.time()
            app.run_bg(discover)
        else:
            found.controls = [C.meta(tr("Searched a moment ago.")),
                              C.ghost(tr("Search again"), ft.Icons.REFRESH_ROUNDED, lambda e: app.run_bg(discover))]
        step = lambda n, head, text, *content, help=None: C.card(ft.Row([  # noqa: E731
            ft.Container(ft.Text(str(n), weight=ft.FontWeight.W_700, color=T.ACCENT), width=T.px(30), height=T.px(30),
                         border_radius=T.px(15), bgcolor=T.ACCENT_SOFT, alignment=ft.Alignment.CENTER),
            ft.Column([C.with_help(C.h2(head), help), C.body(text), *content], spacing=T.S3, expand=True),
        ], spacing=T.S4, vertical_alignment=ft.CrossAxisAlignment.START), padding=T.S5)
        return ft.Column([
            app.top_bar(tr("Connect your Steam Frame"), tr("FramePort installs games on the Frame over your network")),
            *([offline] if offline else []),
            step(1, tr("First-time setup"), tr("New Frame? Run one command on it and FramePort does the rest. Did this "
                                               "once already? Your Frame appears under step 2."),
                 ft.Row([C.primary(tr("Show setup command"), ft.Icons.TERMINAL_ROUNDED, pair),
                         C.meta(tr("OR"), weight=ft.FontWeight.W_600),
                         C.secondary(tr("Set up with a USB cable"), ft.Icons.USB_ROUNDED, usb_explain,
                                     tooltip=tr("No Wi-Fi needed: for networks that block the setup, and faster "
                                                "game uploads"))], wrap=True, spacing=T.S3,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER),
                 pair_box,
                 help="first_time_setup"),
            step(2, tr("Already set up: on your network"), tr("Frames in Developer Mode show up here."), found,
                 help="developer_mode"),
            step(3, tr("Enter the address"), tr("If you know the Frame's address."),
                 ft.Row([addr, pw, C.primary(tr("Connect"),
                                             on_click=lambda e: app.connect_manual(addr.value, pw.value))],
                        wrap=True, spacing=T.S3)),
        ], spacing=T.S4, scroll=ft.ScrollMode.AUTO, expand=True)

    def build(self) -> ft.Control:
        app = self.app
        info = app.frame_info
        if app.frame_state != "connected" or not info:
            return self.wizard()
        return ft.Column([
            app.top_bar(tr("Steam Frame"), tr("Your headset, what it's ready for and what's installed")),
            self.device_card(info), self.readiness(info), self.storage(info), self.installed(info),
        ], spacing=T.S5, scroll=ft.ScrollMode.AUTO, expand=True)
