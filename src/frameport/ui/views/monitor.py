"""Monitor: a live view of the Frame — the running game (fps, CPU/GPU/memory), CPU, GPU, memory, temperatures, power
and battery with 2-minute sparklines, and its processes (end or kill one, end a game). Streamed by the agent's
`_monitor` session (frame/monitor.py) only while the tab is shown: app.go / app.disconnect / closing the window call
stop(), and the agent ends the stream at EOF, so nothing keeps running on the Frame.

Built once and updated in place (CLAUDE.md performance rules): a tick changes values, colours and sparkline paths;
the process table is a fixed pool of rows re-bound to the newest data, so clicks and menus keep working."""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import flet as ft

from ...errors import explain
from ...frame import monitor as M
from ...i18n import tr
from .. import components as C
from .. import theme as T

if TYPE_CHECKING:
    from ..app import FramePortApp

ROWS = 50            # process rows shown at most (the agent sends up to 150; the search narrows them)
RETRY_SECONDS = 5    # reconnect after the stream was lost, while the tab is open
LEVEL_COLOR = {"ok": T.TEXT, "warn": T.WARN, "error": T.ERROR}
GROUP_COLOR = {"Game": T.ACCENT, "Steam": T.INFO, "SteamVR": T.PC, "Desktop": T.TEXT_2, "Other": T.TEXT_3}
CLUSTER_COLORS = (T.INFO, T.ACCENT, T.PC, T.OK)
POWER_COLORS = (T.INFO, T.ACCENT, T.PC, T.TEXT_3)  # CPU, GPU, NPU, other


def label(name: str) -> str:
    """The shown (translated) name of a process group or sensor group from the stream (literal tr() calls so
    scripts/i18n_extract.py finds them)."""
    return {"Game": tr("Game"), "Steam": tr("Steam"), "SteamVR": tr("SteamVR"), "Desktop": tr("Desktop"),
            "Other": tr("Other"), "CPU": tr("CPU"), "GPU": tr("GPU"), "Memory": tr("Memory"),
            "Battery": tr("Battery"), "NPU": tr("NPU"), "Power ICs": tr("Power ICs"), "Modem": tr("Modem"),
            "Camera": tr("Camera"), "Storage": tr("Storage")}.get(name, name)


class Tile:
    """One metric: label, big value, a sub-line and a 2-minute sparkline."""

    def __init__(self, label: str, icon: str, color: str, hi: float | None = None, help_key: str | None = None):
        self.color = color
        self.value = ft.Text("–", size=T.px(26), weight=ft.FontWeight.W_600, color=T.TEXT)
        self.sub = C.meta("", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self.spark = C.Sparkline(color, height=34, hi=hi)
        head = ft.Row([ft.Icon(icon, size=T.px(16), color=color), C.meta(label)], spacing=T.S2)
        if help_key:
            head.controls.append(C.help_icon(help_key, 14))
        self.control = C.card(ft.Column([head, self.value, self.sub, self.spark.control], spacing=T.px(4)),
                              padding=T.S3, col={"xs": 12, "sm": 6, "md": 4, "xl": 2})

    def set(self, value: str, sub: str, series: list, lvl: str = "ok", target: float | None = None) -> None:
        self.value.value, self.sub.value = value, sub
        self.value.color = LEVEL_COLOR[lvl]
        self.spark.set_color(self.color if lvl == "ok" else LEVEL_COLOR[lvl])
        self.spark.set(series, target=target)


class ProcRow:
    """A pooled process row: created once, bound to whichever process is shown at its position."""

    def __init__(self, view: MonitorView):
        self.view, self.proc = view, None
        self.lock = ft.Icon(ft.Icons.LOCK_OUTLINE_ROUNDED, size=T.px(14), color=T.WARN, visible=False,
                            tooltip=tr("Ending this stops Steam, SteamVR or the desktop"))
        self.name = ft.Text("", size=T.T_BODY, color=T.TEXT, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self.pid = C.meta("")
        self.group = ft.Text("", size=T.T_SMALL, weight=ft.FontWeight.W_600)
        self.group_box = ft.Container(self.group, border_radius=T.px(20),
                                      padding=ft.Padding(T.px(8), T.px(2), T.px(8), T.px(2)))
        self.cpu, self.gpu, self.mem, self.age = (ft.Text("", size=T.T_BODY, color=T.TEXT_2,
                                                          text_align=ft.TextAlign.RIGHT) for _ in range(4))
        dots = ft.GestureDetector(ft.Icon(ft.Icons.MORE_HORIZ_ROUNDED, size=T.px(18), color=T.TEXT_2),
                                  on_tap_down=lambda e: self.view.open_menu(self.proc, e.global_position),
                                  mouse_cursor=ft.MouseCursor.CLICK)
        cells = [ft.Container(ft.Row([self.lock, self.name], spacing=T.px(6), tight=True), expand=True),
                 ft.Container(self.pid, width=T.px(70)), ft.Container(self.group_box, width=T.px(96)),
                 ft.Container(self.cpu, width=T.px(64)), ft.Container(self.gpu, width=T.px(64)),
                 ft.Container(self.mem, width=T.px(84)), ft.Container(self.age, width=T.px(76)),
                 ft.Container(dots, width=T.px(32), alignment=ft.Alignment.CENTER)]
        self.box = ft.Container(ft.Row(cells, spacing=T.S2, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                padding=ft.Padding(T.S3, T.px(6), T.S2, T.px(6)), border_radius=T.RADIUS_SM,
                                on_hover=self._hover)
        self.control = ft.GestureDetector(self.box, visible=False,
                                          on_secondary_tap_down=lambda e: self.view.open_menu(self.proc,
                                                                                              e.global_position))

    def _hover(self, e) -> None:
        self.box.bgcolor = T.SURFACE_2 if e.data in (True, "true") else None
        C.update(self.box)

    def bind(self, p: dict | None) -> None:
        self.proc = p
        self.control.visible = p is not None
        if p is None:
            return
        group_name = M.group_label(p.get("group", ""))
        color = GROUP_COLOR[group_name]
        self.name.value = p.get("name", "")
        self.name.color = T.TEXT_3 if p.get("context") else T.TEXT
        self.name.tooltip = p.get("game") or None
        self.pid.value = str(p.get("pid"))
        self.group.value, self.group.color = label(group_name), color
        self.group_box.bgcolor = T.soft(color, 0.12)
        self.lock.visible = bool(p.get("critical") or p.get("locked"))
        self.lock.color = T.TEXT_3 if p.get("locked") else T.WARN
        self.cpu.value = f"{p.get('cpu', 0):.1f} %"
        self.cpu.color = LEVEL_COLOR[M.level("cpu", p.get("cpu"))] if p.get("cpu", 0) >= 1 else T.TEXT_2
        self.gpu.value = f"{p['gpu']:.0f} %" if p.get("gpu") else "–"
        self.mem.value = M.fmt_bytes(p.get("rss"))
        self.age.value = M.fmt_duration(p.get("age"))


class MonitorView:
    def __init__(self, app: FramePortApp):
        self.app = app
        self.session = None          # frame.monitor.MonitorSession while the tab is open
        self._connecting = False
        self._gen = 0                # bumped by stop(): a connect that finishes after the tab was left closes itself
        self.stopped = True
        self.root = None
        self.history = M.History()
        self.static: dict = {}
        self.procs: list[dict] = []
        self.sort_key, self.descending = "cpu", True
        self.paused = False
        self.games_meta: dict[str, dict] = {}  # package -> {"art": url | None, "in_library": bool}
        self.game_pkg: str | None = None
        self.last_games: list[dict] = []
        self.last_sample: dict | None = None
        self._build()

    # ---------------------------------------------------------------- building
    def _build(self) -> None:
        self.live_dot = C.dot(T.TEXT_3, T.px(8))
        self.live_text = C.meta(tr("Connecting…"))
        self.live = ft.Container(ft.Row([self.live_dot, self.live_text], spacing=T.S2, tight=True),
                                 bgcolor=T.SURFACE_3, border_radius=T.px(20),
                                 padding=ft.Padding(T.px(10), T.px(5), T.px(12), T.px(5)))
        self.overhead = C.meta("")
        self.interval = ft.Dropdown(value="1", dense=True, width=T.px(150), border_color=T.BORDER,
                                    options=[ft.DropdownOption(str(s), tr("Every {n} s").format(n=s))
                                             for s in (1, 2, 5)], text_size=T.T_BODY,
                                    on_select=self._set_interval, tooltip=tr("How often the Frame sends new numbers"))
        self.pause_btn = C.icon_btn(ft.Icons.PAUSE_ROUNDED, tr("Pause"), lambda e: self.toggle_pause())
        self.reconnect = C.secondary(tr("Reconnect"), ft.Icons.REFRESH_ROUNDED, lambda e: self.start())
        self.reconnect.visible = False

        # the running game
        self.game_art = ft.Container(width=T.px(96), height=T.px(96), border_radius=T.RADIUS_SM,
                                     bgcolor=T.SURFACE_2)
        self.game_title = ft.Text("", size=T.px(20), weight=ft.FontWeight.W_600, color=T.TEXT, max_lines=1,
                                  overflow=ft.TextOverflow.ELLIPSIS)
        self.game_sub = C.meta("")
        self.game_fps = ft.Text("–", size=T.px(40), weight=ft.FontWeight.W_700, color=T.TEXT)
        self.game_fps_sub = C.meta(tr("frames per second"))
        self.game_spark = C.Sparkline(T.OK, height=48)
        self.game_stats = C.body("", T.TEXT_2)
        self.end_btn = C.secondary(tr("End game"), ft.Icons.STOP_CIRCLE_OUTLINED, lambda e: self.ask_end_game())
        self.page_btn = C.ghost(tr("Game page"), ft.Icons.OPEN_IN_NEW_ROUNDED,
                                lambda e: self.game_pkg and self.app.open_game(self.game_pkg))
        self.game_card = C.card(ft.Row([
            self.game_art,
            ft.Column([C.pill(tr("Running now"), T.OK, ft.Icons.PLAY_ARROW_ROUNDED), self.game_title, self.game_sub,
                       self.game_stats, ft.Row([self.end_btn, self.page_btn], spacing=T.S2)],
                      spacing=T.px(6), expand=3),
            ft.Column([ft.Row([self.game_fps, self.game_fps_sub], spacing=T.S2,
                              vertical_alignment=ft.CrossAxisAlignment.END),
                       self.game_spark.control], spacing=T.px(4), expand=2,
                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        ], spacing=T.S4, vertical_alignment=ft.CrossAxisAlignment.CENTER), padding=T.S4, visible=False,
            bgcolor=T.soft(T.ACCENT, 0.06))
        self.game_card.border = ft.Border.all(1, T.soft(T.ACCENT, 0.35))

        # metric tiles
        self.t_cpu = Tile(tr("CPU"), ft.Icons.MEMORY_ROUNDED, T.INFO, hi=100)
        self.t_gpu = Tile(tr("GPU"), ft.Icons.VIEW_IN_AR_ROUNDED, T.ACCENT, hi=100, help_key="monitor_gpu")
        self.t_mem = Tile(tr("Memory"), ft.Icons.STORAGE_ROUNDED, T.PC, hi=100, help_key="monitor_pressure")
        self.t_temp = Tile(tr("Temperature"), ft.Icons.THERMOSTAT_ROUNDED, T.WARN)
        self.t_power = Tile(tr("Power"), ft.Icons.BOLT_ROUNDED, T.OK, help_key="monitor_power")
        self.t_bat = Tile(tr("Battery"), ft.Icons.BATTERY_STD_ROUNDED, T.OK, hi=100)
        self.tiles = ft.ResponsiveRow([t.control for t in (self.t_cpu, self.t_gpu, self.t_mem, self.t_temp,
                                                            self.t_power, self.t_bat)],
                                      spacing=T.S3, run_spacing=T.S3)

        # details (filled once the stream says how many cores / which sensors the Frame has)
        self.cores = ft.Column(spacing=T.px(6))
        self.temps = ft.Row(spacing=T.S2, run_spacing=T.S2, wrap=True)
        self.power_bar = C.MeterBar(list(POWER_COLORS), height=10)
        self.power_legend = ft.Row(spacing=T.S4, run_spacing=T.S2, wrap=True)
        self.net = ft.Column(spacing=T.px(4))
        self.core_bars: list[tuple[C.MeterBar, ft.Text]] = []
        self.temp_chips: dict[str, ft.Text] = {}
        self.legend_texts: list[ft.Text] = []
        self.net_texts: dict[str, ft.Text] = {}
        self.details = ft.ResponsiveRow([
            C.card(ft.Column([C.meta(tr("CPU cores")), self.cores], spacing=T.S2), padding=T.S3,
                   col={"xs": 12, "md": 4}),
            C.card(ft.Column([C.meta(tr("Temperatures")), self.temps], spacing=T.S2), padding=T.S3,
                   col={"xs": 12, "md": 4}),
            C.card(ft.Column([C.meta(tr("Where the power goes")), self.power_bar.control, self.power_legend,
                              ft.Container(height=T.px(4)), C.meta(tr("Network")), self.net], spacing=T.S2),
                   padding=T.S3, col={"xs": 12, "md": 4}),
        ], spacing=T.S3, run_spacing=T.S3, visible=False)
        self.details_btn = C.ghost(tr("Show details"), ft.Icons.EXPAND_MORE_ROUNDED, lambda e: self.toggle_details())

        # processes
        self.filter = ft.SegmentedButton(
            segments=[ft.Segment("game", label=tr("Game")), ft.Segment("steam", label=tr("Steam & SteamVR")),
                      ft.Segment("all", label=tr("All"))],
            selected=["game"], show_selected_icon=False, on_change=self._set_filter)
        self.search = ft.TextField(hint_text=tr("Search name, PID or game"), dense=True, width=T.px(240),
                                   border_color=T.BORDER, prefix_icon=ft.Icons.SEARCH_ROUNDED,
                                   on_change=lambda e: self._bind_rows(update=True))
        self.sort_btns: dict[str, ft.TextButton] = {}
        left, right = ft.Alignment.CENTER_LEFT, ft.Alignment.CENTER_RIGHT
        header_cells = [ft.Container(self._sort_btn("name", tr("Name")), expand=True, alignment=left),
                        ft.Container(C.meta(tr("PID")), width=T.px(70), alignment=left),
                        ft.Container(C.meta(tr("Group")), width=T.px(96), alignment=left),
                        ft.Container(self._sort_btn("cpu", tr("CPU")), width=T.px(64), alignment=right),
                        ft.Container(self._sort_btn("gpu", tr("GPU")), width=T.px(64), alignment=right),
                        ft.Container(self._sort_btn("rss", tr("Memory")), width=T.px(84), alignment=right),
                        ft.Container(C.meta(tr("Running")), width=T.px(76), alignment=right),
                        ft.Container(width=T.px(32))]
        self.header = ft.Container(ft.Row(header_cells, spacing=T.S2), padding=ft.Padding(T.S3, 0, T.S2, T.px(4)),
                                   border=ft.Border(bottom=ft.BorderSide(1, T.BORDER)))
        self.rows = [ProcRow(self) for _ in range(ROWS)]
        self.proc_note = C.meta("")
        self.no_game = C.callout(tr("No FramePort game is running. Start one in the headset or with Play, or switch "
                                    "the filter to see everything on the Frame."), icon=ft.Icons.SPORTS_ESPORTS_ROUNDED)
        self.no_game.visible = False
        self.menu = ft.ContextMenu(content=C.card(ft.Column(
            [self.header, ft.Column([r.control for r in self.rows], spacing=0), self.proc_note],
            spacing=T.px(4)), padding=T.S2), secondary_trigger=None, tertiary_trigger=None)
        self._update_sort_labels()

    def _sort_btn(self, key: str, label: str) -> ft.TextButton:
        b = ft.TextButton(label, on_click=lambda e, k=key: self.set_sort(k),
                          style=ft.ButtonStyle(padding=0, color=T.TEXT_3,
                                               text_style=ft.TextStyle(size=T.T_META, weight=ft.FontWeight.W_600)))
        self.sort_btns[key] = b
        return b

    def mount(self) -> ft.Control:
        app = self.app
        heading, sub = tr("Monitor"), tr("What your Frame is doing right now")
        if not (app.target and app.frame_state == "connected"):
            return ft.Column([
                app.top_bar(heading, sub),
                C.empty_state(ft.Icons.MONITOR_HEART_OUTLINED, tr("Connect your Frame first"),
                              tr("The monitor shows your Frame's games, load, temperatures and battery live once "
                                 "FramePort is connected to it."),
                              C.primary(tr("Connect"), ft.Icons.LINK_ROUNDED, lambda e: app.go("frame")))], expand=True)
        if self.root is None:
            self.root = ft.Column([
                app.top_bar(heading, sub, [self.overhead, self.live, self.reconnect, self.interval, self.pause_btn]),
                self.game_card,
                self.tiles,
                ft.Row([self.details_btn], alignment=ft.MainAxisAlignment.START),
                self.details,
                C.section(tr("Processes"), ft.Row([self.filter, self.search], spacing=T.S3, wrap=True,
                                                  run_spacing=T.S2, alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                          self.no_game, self.menu, help="monitor_filter"),
                ft.Container(height=T.S5),
            ], spacing=T.S4, scroll=ft.ScrollMode.AUTO, expand=True)
        self.start()
        return self.root

    # ---------------------------------------------------------------- session
    def start(self) -> None:
        self.stopped = False
        if self.session is not None or self._connecting:
            return
        self._connecting = True
        self._set_live(tr("Connecting…"), T.TEXT_3, update=self.root is not None)
        gen, target = self._gen, self.app.target
        self.app.run_bg(self._connect, gen, target)

    def _connect(self, gen: int, target) -> None:
        try:
            session = M.MonitorSession(target.frame, lambda s, g=gen: self._on_sample(g, s),
                                       lambda why, g=gen: self._on_end(g, why))
        except Exception as exc:  # noqa: BLE001
            self._connecting = False
            if gen == self._gen:
                self._set_live(tr("Not connected: {error}").format(error=explain(exc)), T.ERROR)
            return
        self._connecting = False
        if gen != self._gen:  # the tab was left while connecting
            session.close()
            return
        self.session = session
        if session.static != self.static:
            self.static = session.static
            self._build_static()
        if self.interval.value != "1":
            session.set_interval(int(self.interval.value))
        if self.filter.selected != ["game"]:
            session.set_filter(self.filter.selected[0])
        if self.paused:
            session.pause(True)
        self._set_live(tr("Live"), T.OK)

    def _on_end(self, gen: int, why: str) -> None:
        if gen != self._gen:
            return
        self.session = None
        self._set_live(tr("Connection lost, reconnecting…"), T.WARN)
        timer = threading.Timer(RETRY_SECONDS, lambda: gen == self._gen and not self.stopped and self.start())
        timer.daemon = True
        timer.start()

    def stop(self) -> None:
        """End the stream (leaving the tab, disconnecting, closing the app)."""
        self._gen += 1
        self.stopped = True
        session, self.session = self.session, None
        if session is not None:
            session.close()

    def _set_live(self, text: str, color: str, update: bool = True) -> None:
        self.live_text.value, self.live_dot.bgcolor = text, color
        self.reconnect.visible = color == T.ERROR
        if update:
            C.update(self.live, self.reconnect)

    def _set_interval(self, e) -> None:
        if self.session:
            self.session.set_interval(int(self.interval.value))

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        if self.session:
            self.session.pause(self.paused)
        self.pause_btn.icon = ft.Icons.PLAY_ARROW_ROUNDED if self.paused else ft.Icons.PAUSE_ROUNDED
        self.pause_btn.tooltip = tr("Resume") if self.paused else tr("Pause")
        self._set_live(tr("Paused") if self.paused else tr("Live"), T.TEXT_3 if self.paused else T.OK, update=False)
        C.update(self.pause_btn, self.live)

    def toggle_details(self) -> None:
        self.details.visible = not self.details.visible
        if self.details.visible and self.last_sample:  # fill it now, not at the next tick
            self._apply_details(self.last_sample)
        self.details_btn.content = tr("Hide details") if self.details.visible else tr("Show details")
        self.details_btn.icon = ft.Icons.EXPAND_LESS_ROUNDED if self.details.visible else ft.Icons.EXPAND_MORE_ROUNDED
        C.update(self.details, self.details_btn)

    def _set_filter(self, e) -> None:
        which = (self.filter.selected or ["game"])[0]
        if self.session:
            self.session.set_filter(which)

    def set_sort(self, key: str) -> None:
        if self.sort_key == key:
            self.descending = not self.descending
        else:
            self.sort_key, self.descending = key, key != "name"
        self._update_sort_labels()
        self._bind_rows(update=True)
        C.update(*self.sort_btns.values())

    def _update_sort_labels(self) -> None:
        names = {"name": tr("Name"), "cpu": tr("CPU"), "gpu": tr("GPU"), "rss": tr("Memory")}
        for key, b in self.sort_btns.items():
            arrow = (" ↓" if self.descending else " ↑") if key == self.sort_key else ""
            b.content = names[key] + arrow
            b.style.color = T.TEXT if key == self.sort_key else T.TEXT_3

    # ---------------------------------------------------------------- static layout from the Frame
    def _build_static(self) -> None:
        st = self.static
        colors = {}
        for i, cl in enumerate(st.get("clusters") or []):
            for cpu in cl.get("cpus") or []:
                colors[cpu] = CLUSTER_COLORS[i % len(CLUSTER_COLORS)]
        self.core_bars = []
        rows = []
        for cpu in range(st.get("cores") or 0):
            bar, val = C.MeterBar([colors.get(cpu, T.INFO)], height=6), C.meta("", width=T.px(42),
                                                                                text_align=ft.TextAlign.RIGHT)
            self.core_bars.append((bar, val))
            rows.append(ft.Row([C.meta(f"{cpu}", width=T.px(16)), ft.Container(bar.control, expand=True), val],
                               spacing=T.S2, vertical_alignment=ft.CrossAxisAlignment.CENTER))
        self.cores.controls = rows
        self.temp_chips = {}
        chips = []
        for group in st.get("temp_groups") or []:
            value = ft.Text("–", size=T.T_BODY, weight=ft.FontWeight.W_600, color=T.TEXT)
            self.temp_chips[group] = value
            chips.append(ft.Container(ft.Column([C.meta(label(group)), value], spacing=0, tight=True),
                                      bgcolor=T.SURFACE_2, border_radius=T.RADIUS_SM,
                                      padding=ft.Padding(T.px(10), T.px(6), T.px(10), T.px(6))))
        self.temps.controls = chips
        self.legend_texts = []
        legend = []
        for color, name in zip(POWER_COLORS, ("CPU", "GPU", "NPU", tr("Other")), strict=True):
            t = C.meta(f"{name} –")
            self.legend_texts.append(t)
            legend.append(ft.Row([C.dot(color, T.px(8)), t], spacing=T.px(6), tight=True))
        self.power_legend.controls = legend
        self.net.controls, self.net_texts = [], {}
        C.update(self.cores, self.temps, self.power_legend, self.net)

    # ---------------------------------------------------------------- samples
    def _on_sample(self, gen: int, s: dict) -> None:
        if gen != self._gen or self.root is None:
            return
        self.last_sample = s
        for name, value in M.series_of(s).items():
            self.history.add(name, value)
        changed = [self._apply_tiles(s), self._apply_game(s), self._apply_details(s)]
        if "procs" in s:
            self.procs = s.get("procs") or []
            changed.append(self._bind_rows(update=False))
        cpu_share = (s.get("self_ms") or 0) / 10 / max(1.0, s.get("dt") or 1.0) / max(1, self.static.get("cores", 1))
        self.overhead.value = tr("Monitor uses {pct} % of the Frame's CPU").format(pct=f"{cpu_share:.2f}")
        self.overhead.tooltip = tr("The time the Frame spends collecting these numbers")
        C.update(self.overhead, *[c for group in changed for c in group])

    def _apply_tiles(self, s: dict) -> list:
        h, st = self.history, self.static
        cpu = s.get("cpu") or {}
        mhz = cpu.get("mhz") or []
        clocks = " · ".join(f"{m / 1000:.1f}" for m in mhz) + (" GHz" if mhz else "")
        self.t_cpu.set(M.fmt_pct(cpu.get("total")), clocks, h.get("cpu"), M.level("cpu", cpu.get("total")))
        gpu = s.get("gpu") or {}
        gmax = st.get("gpu_max_mhz")
        sub = f"{gpu.get('mhz')} / {gmax} MHz" if gpu.get("mhz") is not None and gmax else ""
        self.t_gpu.set(M.fmt_pct(gpu.get("busy")), sub, h.get("gpu"), M.level("gpu", gpu.get("busy")))
        mem = s.get("mem") or {}
        total, used = mem.get("total") or 0, (mem.get("total") or 0) - (mem.get("avail") or 0)
        psi = (s.get("psi") or {}).get("memory")
        swap = (mem.get("swap_total") or 0) - (mem.get("swap_free") or 0)
        sub = tr("{used} of {total}").format(used=M.fmt_bytes(used), total=M.fmt_bytes(total))
        if swap > 64 * 1024 ** 2:
            sub += " · " + tr("swap {n}").format(n=M.fmt_bytes(swap))
        if psi:
            sub += " · " + tr("waiting {pct} %").format(pct=f"{psi:.0f}")
        lvl = max((M.level("mem", 100.0 * used / total if total else None), M.level("psi_memory", psi)),
                  key=("ok", "warn", "error").index)
        self.t_mem.set(M.fmt_pct(100.0 * used / total if total else None), sub, h.get("mem"), lvl)
        temps = s.get("temps") or {}
        hot = max(temps.items(), key=lambda kv: kv[1]) if temps else None
        fan = s.get("fan")
        sub = (tr("hottest: {part}").format(part=label(hot[0])) if hot else "") + \
            (f" · {tr('fan')} {fan} rpm" if fan else "")
        lvl = max((M.temp_level(g, v) for g, v in temps.items()), key=("ok", "warn", "error").index, default="ok")
        self.t_temp.set(f"{hot[1]:.0f} °C" if hot else "–", sub, h.get("temp"), lvl)
        pw = s.get("power") or {}
        self.t_power.set(M.fmt_watts(pw.get("system")),
                         " · ".join(f"{k.upper()} {M.fmt_watts(pw.get(k))}" for k in ("cpu", "gpu") if k in pw),
                         h.get("power"))
        bat = s.get("battery")
        lvl = "error" if bat and bat.get("percent", 100) <= 10 and bat.get("draining") else \
            "warn" if bat and bat.get("percent", 100) <= 20 and bat.get("draining") else "ok"
        self.t_bat.set(M.fmt_pct(bat.get("percent")) if bat else "–", M.battery_line(bat), h.get("battery"), lvl)
        self.t_bat.color = T.OK if bat and bat.get("plugged") else T.INFO
        return [t.control for t in (self.t_cpu, self.t_gpu, self.t_mem, self.t_temp, self.t_power, self.t_bat)]

    def _game_meta(self, pkg: str) -> dict:
        meta = self.games_meta.get(pkg)
        if meta is None:  # once per game, on the stream's thread (library + thumbnail lookups are file I/O)
            from ...artwork import thumbs
            from ...core import library
            from .library import CARD_ART

            entry = library.game(pkg)
            meta = {"in_library": entry is not None,
                    "art": thumbs.url(pkg, CARD_ART, wait=False) if entry else None}
            self.games_meta[pkg] = meta
        return meta

    def _apply_game(self, s: dict) -> list:
        games = s.get("games") or []
        self.last_games = games
        game = games[0] if games else None
        was = self.game_card.visible
        self.game_card.visible = game is not None
        self.no_game.visible = game is None and (self.filter.selected or ["game"])[0] == "game"
        if game is None:
            if self.game_pkg is not None:
                self.game_pkg = None
                self.history.series.pop("fps", None)
            return [self.game_card, self.no_game] if was else [self.no_game]
        pkg = game["package"]
        if pkg != self.game_pkg:
            self.game_pkg = pkg
            meta = self._game_meta(pkg)
            art = C.art_fill(meta["art"], radius=T.RADIUS_SM, width=T.px(96), height=T.px(96))
            self.game_art.image, self.game_art.gradient = art.image, art.gradient
            self.game_art.content, self.game_art.alignment = art.content, art.alignment
            self.page_btn.visible = meta["in_library"]
        kind = {"pcvr": tr("PC VR via Proton"), "linux": tr("Linux app")}.get(game.get("kind"), tr("Quest game"))
        self.game_title.value = game.get("title") or pkg
        self.game_sub.value = " · ".join(x for x in (kind, tr("running for {t}").format(
            t=M.fmt_duration(game.get("elapsed"))) if game.get("elapsed") is not None else "") if x)
        self.game_stats.value = tr("CPU {cpu} · GPU {gpu} · memory {mem} · {n} processes").format(
            cpu=M.fmt_pct(game.get("cpu")), gpu=M.fmt_pct(game.get("gpu")), mem=M.fmt_bytes(game.get("mem")),
            n=game.get("processes", 0))
        fps = game.get("fps")
        series = self.history.get("fps")
        target = M.fps_target(series)
        ratio = fps / target if fps and target else None
        lvl = M.level("fps_ratio", ratio)
        self.game_fps.value = f"{fps:.0f}" if fps is not None else "–"
        self.game_fps.color = LEVEL_COLOR[lvl] if fps is not None else T.TEXT_3
        if game.get("kind") in ("pcvr", "linux"):
            self.game_fps_sub.value = tr("fps isn't reported for PC VR and Linux games")
        elif fps is None:
            self.game_fps_sub.value = tr("fps: waiting for the game's first frames")
        else:
            ms = game.get("frame_ms")
            self.game_fps_sub.value = tr("fps · target {hz} Hz").format(hz=target) + (
                f" · {ms:.1f} ms " + tr("late") if ms and ms >= 0.5 else "")
        self.game_spark.set_color(T.OK if lvl == "ok" else LEVEL_COLOR[lvl])
        self.game_spark.set(series, target=target, hi=(target or 72) * 1.15)
        return [self.game_card, self.no_game]

    def _apply_details(self, s: dict) -> list:
        if not self.details.visible:
            return []
        out = []
        cores = (s.get("cpu") or {}).get("cores") or []
        for (bar, val), pct in zip(self.core_bars, cores, strict=False):
            bar.set([pct / 100.0])
            val.value = f"{pct:.0f} %"
            out += [bar.control, val]
        temps = s.get("temps") or {}
        zones = s.get("zones")
        for group, text in self.temp_chips.items():
            v = temps.get(group)
            text.value = f"{v:.1f} °C" if v is not None else "–"
            text.color = LEVEL_COLOR[M.temp_level(group, v)]
            if zones and group in zones:
                text.tooltip = "\n".join(f"{z}: {c:.1f} °C" for z, c in sorted(zones[group].items()))
            out.append(text)
        split = M.power_split(s.get("power") or {})
        total = sum(w for _n, w in split) or 1.0
        self.power_bar.set([w / total for _n, w in split])
        for t, (name, w) in zip(self.legend_texts, split, strict=False):
            t.value = f"{label(name)} {M.fmt_watts(w)}"
        out += [self.power_bar.control, *self.legend_texts]
        net = s.get("net") or {}
        new = [k for k in net if k not in self.net_texts]
        for k in new:
            self.net_texts[k] = C.meta("")
            self.net.controls.append(self.net_texts[k])
        for k, (rx, tx) in net.items():
            self.net_texts[k].value = f"{k}  ↓ {M.fmt_rate(rx)}  ↑ {M.fmt_rate(tx)}"
        out += [self.net] if new else list(self.net_texts.values())
        return out

    def _bind_rows(self, update: bool = True) -> list:
        procs = M.sort_filter(self.procs, self.search.value or "", self.sort_key, self.descending)
        for row, p in zip(self.rows, procs + [None] * ROWS, strict=False):
            row.bind(p)
        shown, total = min(len(procs), ROWS), len(self.procs)
        if len(procs) > ROWS:
            self.proc_note.value = tr("Showing the top {shown} of {n}: search to find others.").format(
                shown=shown, n=len(procs))
        elif self.search.value and not procs:
            self.proc_note.value = tr("No process matches.")
        else:
            self.proc_note.value = tr("{n} processes").format(n=total) if total else ""
        controls = [r.control for r in self.rows] + [self.proc_note]
        if update:
            C.update(*controls)
        return controls

    # ---------------------------------------------------------------- actions
    def open_menu(self, p: dict | None, position=None) -> None:
        if not p:
            return
        name, pid = p.get("name", "?"), p.get("pid")
        actions: list = []
        if not p.get("locked"):
            actions += [(tr("End process"), ft.Icons.CLOSE_ROUNDED, lambda e: self.ask_kill(p, "TERM")),
                        (tr("Force kill"), ft.Icons.DANGEROUS_OUTLINED, lambda e: self.ask_kill(p, "KILL"))]
        if p.get("game"):
            actions += [None, (tr("End the whole game"), ft.Icons.STOP_CIRCLE_OUTLINED,
                               lambda e: self.ask_end_game(p["game"]))]
        actions += [None, (tr("Copy PID {pid}").format(pid=pid), ft.Icons.CONTENT_COPY_ROUNDED,
                           lambda e: self.app.copy(str(pid))),
                    (tr("Copy name"), ft.Icons.CONTENT_COPY_ROUNDED, lambda e: self.app.copy(name))]
        self.menu.items = C.menu_items(actions)
        C.update(self.menu)
        self.app.page.run_task(self.menu.open, global_position=position)

    def ask_kill(self, p: dict, sig: str) -> None:
        name, pid = p.get("name", "?"), p.get("pid")
        if p.get("critical"):
            heading = tr("End {name}?").format(name=name)
            text = tr("{name} is part of Steam, SteamVR or the desktop. Ending it can close the headset view or "
                      "restart Steam, and anything running in the headset may stop.").format(name=name)
        elif sig == "KILL":
            heading = tr("Force kill {name}?").format(name=name)
            text = tr("{name} (PID {pid}) stops at once without saving anything.").format(name=name, pid=pid)
        else:
            heading = tr("End {name}?").format(name=name)
            text = tr("{name} (PID {pid}) is asked to close.").format(name=name, pid=pid)
            if p.get("game"):
                text += " " + tr("It belongs to a running game, which may stop with it.")
        label = tr("Force kill") if sig == "KILL" else tr("End process")
        C.confirm(self.app.page, heading, text, label, lambda: self.app.run_bg(self._kill, p, sig),
                  danger=sig == "KILL" or bool(p.get("critical")))

    def _kill(self, p: dict, sig: str) -> None:
        session = self.session
        if session is None:
            self.app.toast(tr("The monitor isn't connected."), error=True)
            return
        name = p.get("name", "?")
        try:
            res = session.kill(p["pid"], sig, force=bool(p.get("critical")))
        except M.ProcessCritical:  # the table was older than the Frame's view of it
            res = session.kill(p["pid"], sig, force=True)
        except Exception as exc:  # noqa: BLE001
            self.app.toast(explain(exc), error=True)
            return
        if res.get("ended"):
            self.app.toast(tr("{name} ended.").format(name=name))
        else:
            self.app.toast(tr("{name} is still running.").format(name=name), action=tr("Force kill"),
                           on_action=lambda e: self.app.run_bg(self._kill, p, "KILL"))

    def ask_end_game(self, pkg: str | None = None) -> None:
        pkg = pkg or self.game_pkg
        if not pkg:
            return
        title = next((g.get("title") for g in self.last_games if g.get("package") == pkg), None) or pkg
        C.confirm(self.app.page, tr("End {title}?").format(title=title),
                  tr("The game is closed the way Steam's Exit game does it; if it doesn't stop, FramePort stops it. "
                     "Progress that the game hasn't saved is lost."),
                  tr("End game"), lambda: self.app.run_bg(self._end_game, pkg, title), danger=True)

    def _end_game(self, pkg: str, title: str) -> None:
        session = self.session
        if session is None:
            self.app.toast(tr("The monitor isn't connected."), error=True)
            return
        self.end_btn.disabled = True
        C.update(self.end_btn)
        try:
            res = session.end_game(pkg)
        except Exception as exc:  # noqa: BLE001
            self.app.toast(explain(exc), error=True)
            return
        finally:
            self.end_btn.disabled = False
            C.update(self.end_btn)
        if res.get("ended"):
            self.app.toast(tr("{title} ended.").format(title=title))
        else:
            self.app.toast(tr("{title} is still running.").format(title=title), error=True)
