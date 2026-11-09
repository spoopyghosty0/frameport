"""The Room tab's editing cards: Clean-up (the scan clean-up settings, Apply runs it again on the Frame), Labels (what
the label rules decided, door / closet suggestions) and Furniture & screens (a box editor + doors and windows on a
wall; Save sends the edited room to the Frame).

Built once with the Room tab (persistent): refresh() brings the cards up to date with the room cache and returns the
controls it changed. Lists with buttons are rebuilt only when the room status or the edited room's structure changes
(a user action), never on the scan's progress ticks; sliders and readouts are updated in place.
The scan eraser of the clean-up page isn't here (the agent accepts erase spots: rooms.refine(erase=...))."""
from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING

import flet as ft

from ...i18n import tr
from ...install import rooms
from ...mr import room as R
from ...mr import view3d
from .. import components as C
from .. import room_state as RS
from .. import theme as T

if TYPE_CHECKING:
    from .room import RoomView


def _furniture_name(label: str) -> str:
    from .room import furniture_name

    return furniture_name(label)


def setting_label(key: str) -> str:
    return tr(rooms.REFINE_DEFAULTS[key]["label"])


def setting_text(key: str, value) -> str:
    """A clean-up setting's value in words (the readout next to its slider)."""
    meta = rooms.REFINE_DEFAULTS[key]
    if meta["kind"] == "choice":
        return tr(dict(meta["choices"])[value])
    if meta["kind"] == "bool":
        return tr("on") if value else tr("off")
    if "off" in meta and value == meta["off"]:
        return tr("off")
    if "all" in meta and value >= meta["all"]:
        return tr("keep all")
    unit = meta.get("unit")
    if unit == "cm":
        return tr("{n} cm").format(n=round(value * 100))
    if unit == "m2":
        return tr("{n} m²").format(n=f"{value:.2f}")
    if unit == "deg":
        return tr("{n}°").format(n=round(value))
    if unit == "times":
        return tr("{n}×").format(n=f"{value:g}")
    return f"{value:g}"


# what the clean-up report's counts are called (scan.json "report"; unknown keys read as their name)
def report_label(key: str) -> str:
    return {"triangles": tr("Scan triangles"), "kept": tr("Kept"), "structural": tr("Wall, floor and ceiling surfaces"),
            "near_walls": tr("Cleared near walls"), "outside": tr("Outside the room"),
            "small_pieces": tr("Small pieces"), "thin_pieces": tr("Thin floating pieces"), "erased": tr("Erased"),
            "walked": tr("Where you walked"), "tops": tr("Flattened onto furniture tops"),
            "panel_triangles": tr("Flat panel triangles"), "boxes": tr("Furniture boxes"),
            "removed_boxes": tr("Boxes removed"), "relabelled": tr("Boxes renamed"),
            "suggestions": tr("Door or closet suggestions"), "output_triangles": tr("Triangles in the room"),
            "output_volumes": tr("Furniture boxes in the room")}.get(key, key.replace("_", " ").capitalize())


# scan.json report keys that aren't counts (heights, flags: see report_notes) - docs/mr/ROOM_FORMAT.md
REPORT_NOT_COUNTS = ("floor_y", "ceiling_y", "walked_skipped", "truncated", "version")


def report_notes(report: dict | None) -> tuple[list[str], str]:
    """(notes, warning) about a clean-up: the walked pass skipped (no head path), triangles cut off (the room's
    triangle limit)."""
    report = report or {}
    notes = [tr("Where you walked: skipped, this scan has no head path")] if report.get("walked_skipped") else []
    cut = report.get("truncated")
    warning = tr("The cleaned-up scan had more triangles than a room can hold: {n} were left out.").format(
        n=f"{cut:,}" if isinstance(cut, int) else cut) if cut else ""
    return notes, warning


def report_rows(report: dict | None) -> list[tuple[str, str]]:
    """(label, value) for the clean-up's counts; nested groups are flattened, lists and empty values left out."""
    out = []

    def walk(d, prefix=""):
        for k, v in d.items():
            if k in REPORT_NOT_COUNTS:
                continue
            if isinstance(v, dict):
                walk(v, prefix)
            elif isinstance(v, bool) or v is None or isinstance(v, (list, str)):
                continue
            elif isinstance(v, (int, float)):
                out.append((report_label(k), f"{v:,}" if isinstance(v, int) else f"{v:,.2f}".rstrip("0").rstrip(".")))
    walk(report or {})
    return out


def _slider(lo, hi, step, value, on_change, width=None) -> ft.Slider:
    return ft.Slider(min=lo, max=hi, divisions=max(1, round((hi - lo) / step)), value=min(max(value, lo), hi),
                     active_color=T.ACCENT, on_change=on_change, width=width, height=T.px(28),
                     padding=ft.Padding(0, 0, 0, 0))  # the track spans the row: its ends under the label and value



def set_range(s: ft.Slider, lo: float, hi: float, step: float) -> None:
    """A new range for `s`, its value kept inside it: Flet refuses a value outside min..max when the slider is next
    sent (GitHub #559: a room whose outline started past 0 m left the hidden box sliders at 0, and the next page
    switch crashed with "value (0) must be greater than or equal to min (0.33)")."""
    s.min, s.max = round(lo, 2), round(max(lo, hi), 2)
    s.divisions = max(1, round((s.max - s.min) / step))
    s.value = min(max(s.value if s.value is not None else s.min, s.min), s.max)


def _full(control: ft.Control) -> ft.Row:
    """`control` across the whole content width (a full segmented() track)."""
    return ft.Row([ft.Container(control, expand=True)])


def _even(button: ft.Control) -> ft.Control:
    """A button sharing its row equally with the others (same width and height)."""
    button.expand = True
    return button


def _labelled(title: str, readout: ft.Text, control: ft.Control, help_key: str | None = None) -> ft.Column:
    head = ft.Row([ft.Container(C.with_help(C.body(title, T.TEXT), help_key), expand=True), readout],
                  vertical_alignment=ft.CrossAxisAlignment.CENTER)
    return ft.Column([head, control], spacing=0, tight=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)


# ===================================================================================================== Clean-up
class CleanupCard:
    """Recommended (the defaults) or your own settings: the key ones in sight, the rest under More settings. Apply
    cleans up the saved room's scan again on the Frame (a background job) and saves the settings there."""

    def __init__(self, view: RoomView):
        self.view = view
        self.values = rooms.refine_defaults()
        self.mine: dict | None = None   # your settings while Recommended is shown
        self.touched = False            # changed here since the Frame's settings were loaded
        self._loaded = None             # the Frame's saved settings last shown (json)
        self._report_seen = "unset"
        self.sliders: dict[str, ft.Slider] = {}
        self.readouts: dict[str, ft.Text] = {}
        self.switches: dict[str, ft.Control] = {}
        self.preset = C.segmented([("recommended", tr("Recommended")), ("mine", tr("Your settings"))], "recommended",
                                  self._preset, full=True)
        walls = rooms.REFINE_DEFAULTS["walls"]
        self.walls = C.segmented([(v, tr(label)) for v, label in walls["choices"]], self.values["walls"],
                                 lambda v: self.set("walls", v), full=True)
        keys = [k for k, m in rooms.REFINE_DEFAULTS.items() if m["key"] and m["kind"] == "number"]
        visible = [_labelled(tr("Walls"), C.meta(""), _full(self.walls), "refine_walls")]
        visible += [self._row(k) for k in keys]
        groups = []
        for group, title in rooms.REFINE_GROUPS:
            rows = [self._row(k) for k, m in rooms.REFINE_DEFAULTS.items() if m["group"] == group and not m["key"]]
            if rows:
                groups.append(ft.Column([C.body(tr(title), T.TEXT_2, weight=ft.FontWeight.W_600), *rows],
                                        spacing=T.S2, tight=True))
        self.more = ft.Column(groups, spacing=T.S4, visible=False)
        self.more_btn = C.ghost(tr("More settings"), ft.Icons.EXPAND_MORE_ROUNDED, self.toggle_more)
        self.apply_btn = C.primary(tr("Apply"), ft.Icons.AUTO_FIX_HIGH_ROUNDED, self.apply,
                                   tooltip=C.tip(tr("Clean up your saved scan again with these settings. They are "
                                                    "also used for your next scans.")))
        self.state_text = C.meta("")
        self.progress = C.progress_bar(None)
        self.progress.visible = False
        self.result_title = C.meta(tr("Last clean-up"))
        self.result = ft.Column([], spacing=T.px(2), tight=True)
        # (no inset: the counts end at the same edge as the slider values above)
        self.result_notes = ft.Column([], spacing=T.px(2), tight=True)
        self.result_warn = C.callout("", "warn")
        self.result_warn.visible = False
        self.result_box = ft.Column([ft.Divider(height=1, color=T.BORDER), self.result_title, self.result,
                                     self.result_notes, self.result_warn], spacing=T.S2, tight=True, visible=False)
        self.control = C.card(ft.Column([
            C.section(tr("Clean-up"), _full(self.preset), *visible, self.more_btn, self.more,
                      ft.Row([self.apply_btn, ft.Container(self.state_text, expand=True)], spacing=T.S3,
                             vertical_alignment=ft.CrossAxisAlignment.CENTER),
                      self.progress, self.result_box, help="room_cleanup"),
        ], spacing=T.S3), padding=T.S4, col={"xs": 12, "md": 6, "xl": 4})
        self._show()

    def _row(self, key: str) -> ft.Control:
        meta = rooms.REFINE_DEFAULTS[key]
        if meta["kind"] == "bool":
            sw = C.switch(setting_label(key), value=bool(self.values[key]),
                          on_change=lambda e, k=key: self.set(k, 1 if e.control.value else 0))
            self.switches[key] = sw
            return C.with_help(sw, meta["help"])
        self.readouts[key] = C.body("", T.TEXT, weight=ft.FontWeight.W_600)
        self.sliders[key] = _slider(meta["min"], meta["max"], meta["step"], self.values[key],
                                    lambda e, k=key: self._slid(k, e.control.value))
        return _labelled(setting_label(key), self.readouts[key], self.sliders[key], meta["help"])

    # values -> controls
    def _show(self) -> list[ft.Control]:
        for k, s in self.sliders.items():
            meta = rooms.REFINE_DEFAULTS[k]
            s.value = min(max(self.values[k], meta["min"]), meta["max"])
            self.readouts[k].value = setting_text(k, self.values[k])
        for k, sw in self.switches.items():
            C.set_switch(sw, bool(self.values[k]))
        C.set_segmented(self.walls, self.values["walls"])
        C.set_segmented(self.preset, "recommended" if not rooms.normalize_refine(self.values) else "mine")
        self.sliders["near"].disabled = self.values["walls"] != "panels"
        return [*self.sliders.values(), *self.readouts.values(), *self.switches.values(), self.walls, self.preset]

    def _slid(self, key: str, value: float) -> None:
        meta = rooms.REFINE_DEFAULTS[key]
        value = round(round((value - meta["min"]) / meta["step"]) * meta["step"] + meta["min"], 4)
        if key in rooms.INT_SETTINGS:
            value = int(round(value))
        if self.values[key] == value:
            return
        self.values[key] = value
        self.readouts[key].value = setting_text(key, value)
        self.touched = True
        changed = self.preset.data != ("mine" if rooms.normalize_refine(self.values) else "recommended")
        C.set_segmented(self.preset, "recommended" if not rooms.normalize_refine(self.values) else "mine")
        C.update(self.readouts[key], *([self.preset] if changed else []))

    def set(self, key: str, value) -> None:
        self.values[key] = value
        self.touched = True
        C.update(*self._show())

    def _preset(self, which: str) -> None:
        if which == "recommended":
            self.mine = dict(self.values) if rooms.normalize_refine(self.values) else self.mine
            self.values = rooms.refine_defaults()
        else:
            self.values = dict(self.mine) if self.mine else self.values
        self.touched = True
        C.update(*self._show())

    def toggle_more(self, e=None) -> None:
        shown = self.more.visible = not self.more.visible
        self.more_btn.content = tr("Fewer settings") if shown else tr("More settings")
        self.more_btn.icon = ft.Icons.EXPAND_LESS_ROUNDED if shown else ft.Icons.EXPAND_MORE_ROUNDED
        C.update(self.more, self.more_btn)

    def apply(self, e=None) -> None:
        self.touched = False
        RS.refine_room(self.view.app, dict(self.values))

    # cache -> controls
    def refresh(self, cached: dict, status: dict, room: dict | None) -> list[ft.Control]:
        changed = [self.apply_btn, self.state_text, self.progress, self.result_box]
        saved = status.get("refine") if status else None
        key = json.dumps(saved, sort_keys=True) if saved is not None else None
        if key is not None and key != self._loaded and not self.touched:
            self._loaded = key
            self.values = rooms.current_refine(status)
            changed += self._show()
        working = cached.get("working")
        refining = working == "refine"
        self.apply_btn.disabled = bool(working) or not room or not status.get("scanner_installed", True) \
            or room.get("source") != "scan"
        self.progress.visible = refining
        if refining:
            self.state_text.value = cached.get("job_stage") or tr("Cleaning up the scan on the Frame…")
        elif room and room.get("source") != "scan":
            self.state_text.value = tr("Only a scanned room can be cleaned up.")
        elif status and status.get("edited"):
            self.state_text.value = tr("Your own boxes and doors stay.")
        else:
            self.state_text.value = ""
        report = (status or {}).get("report")
        if report is not self._report_seen:
            self._report_seen = report
            rows = report_rows(report)
            self.result.controls = [ft.Row([ft.Container(C.meta(a), expand=True), C.body(b, T.TEXT)],
                                           spacing=T.S2) for a, b in rows]
            notes, warning = report_notes(report)
            self.result_notes.controls = [C.meta(n) for n in notes]
            self.result_warn.visible = bool(warning)
            if warning:  # (callout: icon, then the text's container)
                self.result_warn.content.controls[1].content.value = warning
            self.result_box.visible = bool(rows or notes or warning)
        return changed


# ===================================================================================================== Labels
LABEL_NAME_W = 72  # the Labels rows' first column (T.px)

class LabelsCard:
    """What the label rules decided for each box the scanner found (scanner label -> label, and why), and door /
    closet suggestions with Add as door / Add as closet (into the box editor, saved with Save)."""

    def __init__(self, view: RoomView):
        self.view = view
        self._seen = "unset"
        self.rows = ft.Column([], spacing=T.S2, tight=True)
        self.suggestions = ft.Column([], spacing=T.S2, tight=True)
        self.empty = C.meta(tr("Clean up a scan to see what the rules decided."))
        self.reset_btn = C.ghost(tr("Back to the scan's boxes"), ft.Icons.UNDO_ROUNDED, self._ask_reset,
                                 tooltip=C.tip(tr("Drop your own boxes, doors and windows and use the boxes the "
                                                  "scan found.")))
        self.reset_btn.visible = False
        self.control = C.card(ft.Column([
            C.section(tr("Labels"), self.empty, self.suggestions, self.rows, self.reset_btn, help="room_labels"),
        ], spacing=T.S3), padding=T.S4, col={"xs": 12, "md": 6, "xl": 4})

    def _ask_reset(self, e=None) -> None:
        C.confirm(self.view.app.page, tr("Back to the scan's boxes?"),
                  tr("Your own furniture boxes, doors and windows are removed and the scan is cleaned up again."),
                  tr("Use the scan's boxes"), lambda: RS.refine_room(self.view.app, None, keep_edits=False))

    def refresh(self, cached: dict, status: dict, room: dict | None) -> list[ft.Control]:
        self.reset_btn.visible = bool(status and status.get("edited"))
        self.reset_btn.disabled = bool(cached.get("working"))
        changed = [self.reset_btn]
        if cached.get("status") is self._seen:
            return changed
        self._seen = cached.get("status")
        labels = [x for x in (status or {}).get("labels") or [] if isinstance(x, dict)]
        suggestions = [x for x in (status or {}).get("suggestions") or [] if isinstance(x, dict)]
        self.rows.controls = [self._label_row(x) for x in labels]
        self.suggestions.controls = [self._suggestion_row(x) for x in suggestions]
        self.empty.visible = not labels and not suggestions
        return changed + [self.rows, self.suggestions, self.empty]

    @staticmethod
    def _size(box: dict) -> str:
        try:
            return size_text(float(box["w"]), float(box["d"]), float(box["h"]))
        except (KeyError, TypeError, ValueError):
            return ""

    def _label_row(self, x: dict) -> ft.Control:
        src, dst = str(x.get("from") or ""), x.get("to")
        if x.get("suggest") in ("door", "closet"):  # a tall thin slab: see the suggestions above
            change, color = (tr("→ door?") if x["suggest"] == "door" else tr("→ closet?")), T.OK
        elif dst is None:
            change, color = tr("removed"), T.TEXT_3
        elif dst == src:
            change, color = tr("kept"), T.TEXT_2
        else:
            change, color = "→ " + _furniture_name(str(dst)), T.ACCENT
        # columns: the scanner's name (fixed width), what it became, the size at the right edge
        head = ft.Row([ft.Container(ft.Text(_furniture_name(src), color=T.TEXT if dst is not None else T.TEXT_3,
                                            weight=ft.FontWeight.W_600, size=T.T_BODY, no_wrap=True),
                                    width=T.px(LABEL_NAME_W)),
                       ft.Container(ft.Text(change, color=color, size=T.T_BODY), expand=True),
                       C.meta(self._size(x.get("box") or {}))],
                      spacing=T.S2, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        reason = str(x.get("reason") or "")
        return ft.Column([head] + ([C.meta(reason)] if reason else []), spacing=T.px(2), tight=True)

    def _suggestion_row(self, x: dict) -> ft.Control:
        kind = x.get("kind")
        text = tr("A door?") if kind == "door" else tr("A closet?")
        editor = self.view.editor
        return ft.Container(ft.Column([
            ft.Row([ft.Icon(ft.Icons.DOOR_FRONT_DOOR_ROUNDED if kind == "door" else ft.Icons.SHELVES, color=T.OK,
                            size=T.ICON_S),
                    C.body(text, T.TEXT, weight=ft.FontWeight.W_600), ft.Container(expand=True),
                    C.meta(self._size(x.get("box") or {}))], spacing=T.S2,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            C.meta(str(x.get("reason") or "") or
                   tr("A tall, thin box by a wall: the scanner can't tell a door from a closet.")),
            ft.Row([_even(C.secondary(tr("Add as door"), None, lambda e, s=x: editor.accept(s, "door"))),
                    _even(C.secondary(tr("Add as closet"), None, lambda e, s=x: editor.accept(s, "closet")))],
                   spacing=T.S2),
        ], spacing=T.S2, tight=True), bgcolor=T.soft(T.OK, 0.07), border=ft.Border.all(1, T.soft(T.OK, 0.3)),
            border_radius=T.RADIUS_SM, padding=T.S3)


# ===================================================================================================== box editor
BOX_FIELDS = (  # attribute, label, min, max, step, unit ("" = metres, "deg")
    ("cx", "Left / right", None, None, 0.01, ""), ("cz", "Front / back", None, None, 0.01, ""),
    ("top_y", "Top height", 0.0, 3.0, 0.01, ""), ("yaw_deg", "Turn", -180.0, 180.0, 1.0, "deg"),
    ("w", "Width", 0.05, 4.0, 0.01, ""), ("d", "Depth", 0.05, 4.0, 0.01, ""), ("h", "Height", 0.02, 3.0, 0.01, ""))
PLANE_FIELDS = (("along", "Along the wall", 0.0, 1.0, 0.01, "%"), ("bottom", "Bottom edge", 0.0, 2.0, 0.01, ""),
                ("w", "Width", 0.3, 3.0, 0.01, ""), ("h", "Height", 0.2, 2.6, 0.01, ""))


CONTROL_H = 44  # the editors' dropdowns and their Delete button share this height (T.px)


def _picker(options, on_select) -> ft.Dropdown:
    """A dropdown filling its row, as tall as the Delete button next to it."""
    return C.dropdown(options=options, on_select=on_select, dense=True, expand=True,
                      content_padding=ft.Padding(T.px(12), 0, T.px(8), 0))


def _delete_button(on_click) -> ft.OutlinedButton:
    btn = C.secondary(tr("Delete"), ft.Icons.DELETE_OUTLINE_ROUNDED, on_click)
    btn.style.padding = ft.Padding(T.px(14), 0, T.px(14), 0)
    btn.height = T.px(CONTROL_H)
    return btn


def _panel(rows: list[ft.Control]) -> ft.Container:
    """The selected box's / door's editor: its rows across the whole width."""
    return ft.Container(ft.Column(rows, spacing=T.S3, tight=True,
                                  horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                        bgcolor=T.SURFACE_2, border_radius=T.RADIUS_SM, padding=T.S3, visible=False)


def metres(value: float) -> str:
    """A box size in metres: one decimal, two below 10 cm (a rug's 3 cm isn't "0.0")."""
    return f"{value:.2f}" if abs(value) < 0.095 else f"{value:.1f}"


def size_text(w: float, d: float, h: float) -> str:
    return tr("{w}×{d}×{h} m").format(w=metres(w), d=metres(d), h=metres(h))


def field_label(name: str) -> str:
    return {"Left / right": tr("Left / right"), "Front / back": tr("Front / back"), "Top height": tr("Top height"),
            "Turn": tr("Turn"), "Width": tr("Width"), "Depth": tr("Depth"), "Height": tr("Height"),
            "Along the wall": tr("Along the wall"), "Bottom edge": tr("Bottom edge")}[name]


def field_text(unit: str, value: float) -> str:
    if unit == "deg":
        return tr("{n}°").format(n=round(value))
    if unit == "%":
        return f"{round(value * 100)} %"
    return tr("{n} m").format(n=f"{value:.2f}")


class FurnitureEditor:
    """The room's furniture boxes and doors / windows, editable: select one (in the list or by clicking it in the 3D
    view), change its type, move / size / turn it; add or delete. Save sends the room to the Frame (rooms.put_room)."""

    def __init__(self, view: RoomView):
        self.view = view
        self.model = None                 # the room cache's model the edits started from
        self.room: R.Room | None = None   # the edited copy
        self.places: list[dict] = []      # per plane: wall, along, bottom (the editor's own terms)
        self.sel: tuple[str, int] | None = None
        self.dirty = False
        # boxes
        self.box_list = ft.Column([], spacing=T.px(2), tight=True)
        self.box_type = _picker([ft.DropdownOption(key=k, text=_furniture_name(k)) for k in R.VOLUME_LABELS],
                                lambda e: self._box_label(e.control.value))
        self.box_sliders: dict[str, ft.Slider] = {}
        self.box_readouts: dict[str, ft.Text] = {}
        rows = []
        for attr, label, lo, hi, step, _unit in BOX_FIELDS:  # one slider per row, the whole width
            self.box_readouts[attr] = C.body("", T.TEXT, weight=ft.FontWeight.W_600)
            self.box_sliders[attr] = _slider(lo if lo is not None else -5, hi if hi is not None else 5, step, 0,
                                             lambda e, a=attr: self._box_value(a, e.control.value))
            rows.append(_labelled(field_label(label), self.box_readouts[attr], self.box_sliders[attr]))
        self.box_delete = _delete_button(self._delete)
        self.box_panel = _panel([ft.Row([ft.Container(self.box_type, expand=True), self.box_delete],
                                        spacing=T.S2, vertical_alignment=ft.CrossAxisAlignment.CENTER), *rows])
        self.add_box = C.secondary(tr("Add box"), ft.Icons.ADD_BOX_ROUNDED, self._add_box)
        # doors / windows
        self.plane_list = ft.Column([], spacing=T.px(2), tight=True)
        self.plane_type = _picker([ft.DropdownOption(key=k, text=_furniture_name(k)) for k in R.PLANE_LABELS],
                                  lambda e: self._plane_label(e.control.value))
        self.plane_wall = _picker([], lambda e: self._plane_wall(e.control.value))
        self.plane_sliders: dict[str, ft.Slider] = {}
        self.plane_readouts: dict[str, ft.Text] = {}
        rows = []
        for attr, label, lo, hi, step, _unit in PLANE_FIELDS:
            self.plane_readouts[attr] = C.body("", T.TEXT, weight=ft.FontWeight.W_600)
            self.plane_sliders[attr] = _slider(lo, hi, step, lo,
                                               lambda e, a=attr: self._plane_value(a, e.control.value))
            rows.append(_labelled(field_label(label), self.plane_readouts[attr], self.plane_sliders[attr]))
        self.plane_delete = _delete_button(self._delete)
        self.plane_panel = _panel([ft.Row([ft.Container(self.plane_type, expand=True), self.plane_delete],
                                          spacing=T.S2, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                   self.plane_wall, *rows])
        self.add_door = C.secondary(tr("Add door"), ft.Icons.DOOR_FRONT_DOOR_ROUNDED,
                                    lambda e: self._add_plane("DOOR_FRAME"))
        self.add_window = C.secondary(tr("Add window"), ft.Icons.WINDOW_ROUNDED,
                                      lambda e: self._add_plane("WINDOW_FRAME"))
        # save
        self.save_btn = C.primary(tr("Save"), ft.Icons.SAVE_ROUNDED, self.save,
                                  tooltip=C.tip(tr("Send your boxes, doors and windows to the Frame. Games get them "
                                                   "at their next start.")))
        self.discard_btn = C.ghost(tr("Discard changes"), ft.Icons.UNDO_ROUNDED, self.discard)
        self.save_text = C.meta("")
        self.empty = C.meta(tr("No room saved yet"))
        self.body = ft.Column([
            C.meta(tr("Pick a box here or click it in the view, then fix its type, place or size.")),
            self.box_list, self.box_panel, ft.Row([self.add_box], wrap=True),
            ft.Container(height=T.S2),
            C.section(tr("Doors & windows"), C.meta(tr("Add them on a wall: games that ask for a door get it.")),
                      self.plane_list, self.plane_panel, ft.Row([self.add_door, self.add_window], spacing=T.S2,
                                                                wrap=True), help="room_doors"),
            ft.Divider(height=1, color=T.BORDER),
            ft.Row([self.save_btn, self.discard_btn, self.save_text], spacing=T.S2, wrap=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=T.S3, tight=True, visible=False)
        self.control = C.card(ft.Column([C.section(tr("Furniture & screens"), self.empty, self.body,
                                                   help="room_boxes")], spacing=T.S3),
                              padding=T.S4, col={"xs": 12, "md": 6, "xl": 4})

    # ------------------------------------------------------------------ model
    def load(self, model: dict | None) -> None:
        """A new room from the cache (a scan, a clean-up, a save): edits start again from it."""
        self.model = model
        self.room = copy.deepcopy(model["room"]) if model else None
        self.places = [R.plane_place(self.room, p) for p in self.room.planes] if self.room else []
        self.sel = None
        self.dirty = False
        if self.room is not None:
            xs = [x for x, _ in self.room.outline]
            zs = [z for _, z in self.room.outline]
            for attr, lo, hi in (("cx", min(xs) - 1, max(xs) + 1), ("cz", min(zs) - 1, max(zs) + 1),
                                 ("top_y", self.room.floor, self.room.floor + 3)):
                set_range(self.box_sliders[attr], lo, hi, 0.01)
            self.plane_wall.options = [
                ft.DropdownOption(key=str(i), text=(tr("Wall {n} ({length} m, unseen)") if w.label ==
                                                    "INVISIBLE_WALL_FACE" else tr("Wall {n} ({length} m)")).format(
                    n=i + 1, length=f"{((w.x1 - w.x0) ** 2 + (w.z1 - w.z0) ** 2) ** 0.5:.1f}"))
                for i, w in enumerate(self.room.walls)]
        self._lists()
        self._panels()

    def scene(self):
        """The 3D scene with the edited furniture (None while there's no room)."""
        if self.model is None or self.room is None:
            return None
        return view3d.with_furniture(self.model["scene"], self.room, self.sel)

    def _redraw(self) -> None:
        canvas = self.view.canvas
        if canvas is not None and self.model is not None:
            canvas.set_scene(self.scene(), fit=False)

    def _changed(self, structure: bool = False) -> None:
        self.dirty = True
        if structure:
            self._lists()
        self._redraw()
        C.update(*self._save_state(), *([self.box_list, self.plane_list] if structure else []))

    # ------------------------------------------------------------------ lists
    @staticmethod
    def _box_size(v) -> str:
        return size_text(v.w, v.d, v.h)

    def _plane_info(self, i: int) -> str:
        p = self.room.planes[i]
        return tr("wall {n} · {w}×{h} m").format(n=self.places[i]["wall"] + 1, w=f"{p.w:.2f}", h=f"{p.h:.2f}")

    def _item(self, kind: str, i: int, name: str, info: str, color: str) -> ft.Container:
        on = self.sel == (kind, i)
        text = ft.Text(name, size=T.T_BODY, color=T.TEXT if on else T.TEXT_2, weight=ft.FontWeight.W_600)
        box = ft.Container(ft.Row([ft.Container(width=T.px(10), height=T.px(10), bgcolor=color,
                                                border_radius=T.px(3)),
                                   text, ft.Container(expand=True), C.meta(info)], spacing=T.S2,
                                  vertical_alignment=ft.CrossAxisAlignment.CENTER),
                           padding=ft.Padding(T.S3, T.px(6), T.S3, T.px(6)), border_radius=T.RADIUS_XS,
                           on_click=lambda e: self.select(None if self.sel == (kind, i) else (kind, i)))
        C.selected_style(box, on, text=text)
        return C.hoverable(box)

    def _lists(self) -> None:
        room = self.room
        self.box_list.controls = [self._item("box", i, _furniture_name(v.label), self._box_size(v), T.ACCENT)
                                  for i, v in enumerate(room.volumes)] if room else []
        if room and not room.volumes:
            self.box_list.controls = [C.meta(tr("No furniture found"))]
        self.plane_list.controls = [self._item("plane", i, _furniture_name(p.label), self._plane_info(i), T.OK)
                                    for i, p in enumerate(room.planes)] if room else []

    # ------------------------------------------------------------------ panels
    def _panels(self) -> list[ft.Control]:
        room, sel = self.room, self.sel
        box = room.volumes[sel[1]] if room and sel and sel[0] == "box" and sel[1] < len(room.volumes) else None
        plane = room.planes[sel[1]] if room and sel and sel[0] == "plane" and sel[1] < len(room.planes) else None
        self.box_panel.visible = box is not None
        self.plane_panel.visible = plane is not None
        if box is not None:
            self.box_type.value = box.label if box.label in R.VOLUME_LABELS else "OTHER"
            for attr, *_rest, unit in BOX_FIELDS:
                s, value = self.box_sliders[attr], getattr(box, attr)
                if attr == "yaw_deg":
                    value = (value + 180) % 360 - 180
                s.value = min(max(value, s.min), s.max)
                self.box_readouts[attr].value = field_text(unit, value)
        if plane is not None:
            place = self.places[sel[1]]
            self.plane_type.value = plane.label if plane.label in R.PLANE_LABELS else "WALL_ART"
            self.plane_wall.value = str(place["wall"])
            values = {"along": place["along"], "bottom": place["bottom"], "w": plane.w, "h": plane.h}
            for attr, *_rest, unit in PLANE_FIELDS:
                s = self.plane_sliders[attr]
                s.value = min(max(values[attr], s.min), s.max)
                self.plane_readouts[attr].value = field_text(unit, values[attr])
        return [self.box_panel, self.plane_panel]

    def select(self, sel: tuple[str, int] | None) -> None:
        self.sel = sel
        self._lists()
        changed = self._panels()
        self._redraw()
        C.update(self.box_list, self.plane_list, *changed)

    # ------------------------------------------------------------------ edits
    def _box(self):
        if self.room and self.sel and self.sel[0] == "box" and self.sel[1] < len(self.room.volumes):
            return self.room.volumes[self.sel[1]]
        return None

    def _box_value(self, attr: str, value: float) -> None:
        v = self._box()
        if v is None:
            return
        step = next(f[4] for f in BOX_FIELDS if f[0] == attr)
        value = round(round(value / step) * step, 4)
        setattr(v, attr, value)
        unit = next(f[5] for f in BOX_FIELDS if f[0] == attr)
        self.box_readouts[attr].value = field_text(unit, value)
        row = self.box_list.controls[self.sel[1]] if self.sel[1] < len(self.box_list.controls) else None
        if row is not None:  # the list row's size, in place (no new clickable control on a slider tick)
            row.content.controls[-1].value = self._box_size(v)
        self._changed()
        C.update(self.box_readouts[attr], *([row] if row is not None else []))

    def _box_label(self, label: str) -> None:
        v = self._box()
        if v is not None and label:
            v.label = label
            self._changed(structure=True)

    def _plane_index(self) -> int | None:
        if self.room and self.sel and self.sel[0] == "plane" and self.sel[1] < len(self.room.planes):
            return self.sel[1]
        return None

    def _rebuild_plane(self, i: int) -> None:
        p, place = self.room.planes[i], self.places[i]
        self.room.planes[i] = R.plane_on_wall(self.room, place["wall"], place["along"], place["bottom"], p.w, p.h,
                                              p.label, p.uuid)

    def _plane_value(self, attr: str, value: float) -> None:
        i = self._plane_index()
        if i is None:
            return
        value = round(round(value / 0.01) * 0.01, 4)
        if attr in ("along", "bottom"):
            self.places[i][attr] = value
        else:
            setattr(self.room.planes[i], attr, value)
        self._rebuild_plane(i)
        unit = next(f[5] for f in PLANE_FIELDS if f[0] == attr)
        self.plane_readouts[attr].value = field_text(unit, value)
        row = self.plane_list.controls[i] if i < len(self.plane_list.controls) else None
        if row is not None:
            row.content.controls[-1].value = self._plane_info(i)
        self._changed()
        C.update(self.plane_readouts[attr], *([row] if row is not None else []))

    def _plane_label(self, label: str) -> None:
        i = self._plane_index()
        if i is not None and label:
            self.room.planes[i].label = label
            self._changed(structure=True)

    def _plane_wall(self, value: str) -> None:
        i = self._plane_index()
        if i is not None and value is not None:
            self.places[i]["wall"] = int(value)
            self._rebuild_plane(i)
            self._changed(structure=True)

    def _delete(self, e=None) -> None:
        if not self.room or not self.sel:
            return
        kind, i = self.sel
        if kind == "box" and i < len(self.room.volumes):
            del self.room.volumes[i]
        elif kind == "plane" and i < len(self.room.planes):
            del self.room.planes[i]
            del self.places[i]
        self.sel = None
        self._changed(structure=True)
        C.update(*self._panels())

    def _add_box(self, e=None) -> None:
        if self.room is None:
            return
        self.room.volumes.append(R.default_volume(self.room))
        self._select_new(("box", len(self.room.volumes) - 1))

    def _add_plane(self, label: str) -> None:
        if self.room is None:
            return
        plane = R.default_plane(self.room, label)
        self.room.planes.append(plane)
        self.places.append(R.plane_place(self.room, plane))
        self._select_new(("plane", len(self.room.planes) - 1))

    def accept(self, suggestion: dict, kind: str) -> None:
        """A label-rules suggestion taken as a door (on its wall) or a closet (a storage box against it)."""
        if self.room is None:
            return
        made = R.accept_suggestion(self.room, suggestion, kind)
        if isinstance(made, R.Plane):
            self.places.append(R.plane_place(self.room, made))
            self._select_new(("plane", len(self.room.planes) - 1))
        else:
            self._select_new(("box", len(self.room.volumes) - 1))
        self.view.app.toast(tr("Added. Check it in the view, then Save."))

    def _select_new(self, sel) -> None:
        self.dirty = True
        self.select(sel)
        C.update(*self._save_state())

    def discard(self, e=None) -> None:
        self.load(self.model)
        self._redraw()
        C.update(self.box_list, self.plane_list, self.box_panel, self.plane_panel, *self._save_state())

    def save(self, e=None) -> None:
        if self.room is None or self.model is None:
            return
        RS.save_room(self.view.app, copy.deepcopy(self.room), self.model.get("mesh"))

    # ------------------------------------------------------------------ cache -> controls
    def _save_state(self) -> list[ft.Control]:
        working = (getattr(self.view.app, "_room_state", None) or {}).get("working")
        self.save_btn.disabled = not self.dirty or bool(working)
        self.discard_btn.visible = self.dirty
        self.save_text.value = (tr("Saving…") if working == "save" else
                                tr("Not saved yet") if self.dirty else tr("No changes to save"))
        return [self.save_btn, self.discard_btn, self.save_text]

    def refresh(self, cached: dict, status: dict, room: dict | None) -> list[ft.Control]:
        model = cached.get("model")
        has = model is not None and room is not None
        self.body.visible = has
        self.empty.visible = not has
        self.empty.value = tr("Loading the room…") if room and model is None else tr("No room saved yet")
        return [self.body, self.empty, *self._save_state()]
