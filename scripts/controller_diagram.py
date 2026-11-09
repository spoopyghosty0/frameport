#!/usr/bin/env python3
"""Generate controller illustrations and the user mapping table from the shared input contract."""
from __future__ import annotations

import argparse
import ast
import json
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/input/quest_buttons.json"
# Frame controller face, own drawing after public photos (Wikimedia Commons "Steam Frame controllers.jpg",
# CC BY 4.0): offsets from the head's centre for a head of radius 100; `o` = the controller's outer side
# (-1 left, +1 right). Left: d-pad outer, View above it, stick inner, Steam below. Right: the mirror, with the
# face buttons as a diamond (Y top, X left, B right, A bottom) and Menu above them.
PANEL_W, PANEL_H = 520, 380
HEAD_R = 100
DOC = ROOT / "docs/CONTROLLERS.md"
START = "<!-- quest-buttons-table:start -->"
END = "<!-- quest-buttons-table:end -->"


def portal_colors() -> dict:
    # Read literal tokens without importing the GUI or consulting user preferences.
    tree = ast.parse((ROOT / "src/frameport/ui/theme.py").read_text(encoding="utf-8"))
    return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_PORTAL" for t in n.targets))


# the left/right thumbstick directions a d-pad button sends in a mode with dpad_stick (the drawing labels each
# d-pad arm "LS"/"RS": the arm shows the direction, and the app's SVG renderer has no arrow glyphs)
STICK_DIRECTIONS = {"dpad_up": "↑", "dpad_down": "↓", "dpad_left": "←", "dpad_right": "→"}


def inverse(mode: dict, hand: str, *, arrows: bool = False) -> dict:
    """Frame input on `hand` -> the Touch input it sends: same-hand buttons, other_hand buttons (FrameBridge reads
    the other controller's button for them) and dpad_stick (the d-pad moves this hand's thumbstick)."""
    out = {frame: touch.upper() if touch != "menu" else "Menu"
           for touch, frames in mode.get(hand, {}).items() for frame in frames}
    other = "left" if hand == "right" else "right"
    out.update({frame: touch.upper() for touch, frames in mode.get("other_hand", {}).get(other, {}).items()
                for frame in frames})
    stick = "LS" if hand == "left" else "RS"
    out.update({pad: f"{'Left' if hand == 'left' else 'Right'} stick {STICK_DIRECTIONS[pad]}" if arrows else stick
                for pad in mode.get("dpad_stick", {}).get(hand, [])})
    return out


def mapping_table(data: dict) -> str:
    modes = sorted(data["modes"], key=int)
    rows = ["| Frame input | " + " | ".join(f"{data['modes'][n]['name']} ({n})" for n in modes) + " |",
            "|---" * (len(modes) + 1) + "|"]
    inputs = (("left", "dpad_up", "Left d-pad ↑"), ("left", "dpad_down", "Left d-pad ↓"),
              ("left", "dpad_left", "Left d-pad ←"), ("left", "dpad_right", "Left d-pad →"),
              ("left", "view", "Left View"), ("right", "a", "Right A (bottom)"),
              ("right", "b", "Right B (right)"), ("right", "x", "Right X (left)"),
              ("right", "y", "Right Y (top)"), ("right", "menu", "Right Menu"),
              ("left", "bumper", "Left bumper"), ("right", "bumper", "Right bumper"))
    for hand, key, label in inputs:
        values = [inverse(data["modes"][n], hand, arrows=True).get(key, "—") for n in modes]
        rows.append("| " + " | ".join([label, *values]) + " |")
    return "\n".join(rows)


def illustration(data: dict, colors: dict, mode: str | None = None) -> str:
    compact = mode is not None
    modes = sorted(data["modes"], key=int)
    rows = (len(modes) + 2) // 2  # the reference and every mode, two per row
    width, height = (PANEL_W, PANEL_H) if compact else (2 * PANEL_W + 40, rows * PANEL_H + 88 + rows * 16)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
             '<title id="title">Quest Touch buttons on Steam Frame controllers</title>',
             '<desc id="desc">' + escape(
                 (data["modes"][mode]["name"] + ": " + data["modes"][mode]["note"] if compact else
                  f"Quest Touch reference and {len(modes)} Frame mappings: "
                  + ", ".join(data["modes"][n]["name"] for n in modes[:-1])
                  + f", and {data['modes'][modes[-1]]['name']}.")
                 + " Each Steam Frame controller button is labelled with the Quest Touch button it sends. "
                   "A dash means it sends nothing. Left Touch buttons are X and Y; right are A and B.") + '</desc>',
             f'<rect width="{width}" height="{height}" rx="16" fill="{colors["BG"]}"/>',
             '<g font-family="DejaVu Sans,Arial,sans-serif">']

    def text(x, y, label, size=14, color=None, anchor="start", weight=400):
        parts.append(f'<text x="{x}" y="{y}" fill="{color or colors["TEXT"]}" font-size="{size}" '
                     f'font-weight="{weight}" text-anchor="{anchor}">{escape(label)}</text>')

    def tone(touch):
        if touch == "—":
            return colors["TEXT_3"]
        if touch in ("LS", "RS"):
            return colors["TEXT"]
        return colors["SECONDARY"] if touch in ("X", "A") else colors["ACCENT"]

    def sends(x, y, touch, size=15):
        text(x, y + size * 0.36, touch, size if len(touch) == 1 else size - 4, tone(touch), "middle", 700)

    def touch_button(x, y, label):
        parts.append(f'<circle cx="{x}" cy="{y}" r="25" fill="{colors["SURFACE_2"]}" '
                     f'stroke="{tone(label)}" stroke-width="1.5"/>')
        sends(x, y, label, 19)

    def controller(hx, hy, hand, mapped):
        o = -1 if hand == "left" else 1
        shell, edge = colors["BG"], colors["BORDER"]
        # the handle hangs below the head, a little toward the inner side
        s = -o * 8
        parts.append(f'<path d="M{hx - 46 + s},{hy + 70} C{hx - 44 + s},{hy + 128} {hx - 34 + s},{hy + 148} '
                     f'{hx - 12 + s},{hy + 152} C{hx + 10 + s},{hy + 156} {hx + 30 + s},{hy + 140} '
                     f'{hx + 46 + s},{hy + 70} Z" fill="{shell}" stroke="{edge}" stroke-width="2"/>')
        parts.append(f'<circle cx="{hx}" cy="{hy}" r="{HEAD_R}" fill="{shell}" stroke="{edge}" stroke-width="2"/>')
        # thumbstick (inner side) and Steam button (not mapped: always Steam)
        sx = hx - o * 34
        parts.append(f'<circle cx="{sx}" cy="{hy + 18}" r="24" fill="{colors["SURFACE"]}" stroke="{edge}"/>')
        parts.append(f'<circle cx="{sx}" cy="{hy + 18}" r="15" fill="{colors["SURFACE_2"]}" stroke="{edge}"/>')
        parts.append(f'<circle cx="{hx + o * 14}" cy="{hy + 62}" r="10" fill="{colors["SURFACE_2"]}" '
                     f'stroke="{edge}"/>')
        text(hx + o * 14, hy + 86, "Steam", 9, colors["TEXT_3"], "middle")
        # View (left) / Menu (right) pill above the d-pad / face buttons
        px, py = hx - o * 4, hy - 46
        pill = mapped.get("view" if hand == "left" else "menu", "—")
        parts.append(f'<rect x="{px - 13}" y="{py - 5}" width="26" height="10" rx="5" '
                     f'fill="{colors["SURFACE_2"]}" stroke="{tone(pill)}"/>')
        # a colon, not an arrow: the app's SVG renderer has no arrow glyphs
        text(px, py - 10, ("View" if hand == "left" else "Menu") + ": " + pill, 10, tone(pill), "middle", 600)
        cx, cy = hx + o * 42, hy + 2
        if hand == "left":  # d-pad: a cross of four arms
            parts.append(f'<path d="M{cx - 14},{cy - 44} h28 v30 h30 v28 h-30 v30 h-28 v-30 h-30 v-28 h30 Z" '
                         f'fill="{colors["SURFACE_2"]}" stroke="{edge}"/>')
            for dx, dy, name in ((0, -29, "dpad_up"), (-29, 0, "dpad_left"), (29, 0, "dpad_right"),
                                 (0, 29, "dpad_down")):
                sends(cx + dx, cy + dy, mapped.get(name, "—"), 14)
        else:  # face buttons as a diamond, each with its printed letter
            for dx, dy, name in ((0, -29, "y"), (-29, 0, "x"), (29, 0, "b"), (0, 29, "a")):
                touch = mapped.get(name, "—")
                parts.append(f'<circle cx="{cx + dx}" cy="{cy + dy}" r="14" fill="{colors["SURFACE_2"]}" '
                             f'stroke="{tone(touch)}" stroke-width="1.5"/>')
                sends(cx + dx, cy + dy, touch, 14)
                text(cx + dx + 15, cy + dy - 10, name.upper(), 9, colors["TEXT_3"], "middle")

    def panel(x, y, key):
        parts.append(f'<g transform="translate({x} {y})">')
        parts.append(f'<rect x="0" y="0" width="{PANEL_W}" height="{PANEL_H}" rx="12" '
                     f'fill="{colors["SURFACE"]}" stroke="{colors["BORDER"]}"/>')
        title = "Quest Touch reference" if key is None else data["modes"][key]["name"]
        text(20, 31, title, 20, weight=600)
        if key is None:
            text(20, 55, "Two face buttons per hand, plus Menu on the left", 13, colors["TEXT_2"])
        else:
            text(20, 55, "Steam Frame controllers: each button shows the Quest button it sends", 13,
                 colors["TEXT_2"])
        for hand, hx in (("left", 140), ("right", 380)):
            text(hx, 84 if key is None else 78, hand.capitalize(), 13, colors["TEXT_2"], "middle")
            if key is None:
                low, high = ("X", "Y") if hand == "left" else ("A", "B")
                inner = 23 if hand == "left" else -23  # Quest's second button sits up and toward the other hand
                touch_button(hx - inner, 206, low)
                touch_button(hx + inner, 151, high)
                if hand == "left":
                    parts.append(f'<rect x="{hx - 57}" y="{251}" width="114" height="32" rx="8" '
                                 f'fill="{colors["SURFACE_2"]}"/>')
                    text(hx, 272, "Menu", 12, colors["TEXT_2"], "middle")
            else:
                controller(hx, 186, hand, inverse(data["modes"][key], hand))
        footnote = ("Lower: X / A · Upper: Y / B" if key is None else
                    "LS: the d-pad also moves the left stick · bumpers send nothing"
                    if data["modes"][key].get("dpad_stick") else
                    "Sticks, triggers and grips unchanged · bumpers send nothing")
        text(PANEL_W / 2, PANEL_H - 14, footnote, 12, colors["TEXT_2"], "middle")
        parts.append('</g>')

    if compact:
        panel(0, 0, mode)
    else:
        text(20, 32, "Controller buttons", 24, weight=600)
        text(20, 57, "Blue: X / A · Orange: Y / B or Menu · —: sends nothing", 14, colors["TEXT_2"])
        for i, key in enumerate([None, *modes]):
            row, column = divmod(i, 2)
            last_alone = i == len(modes) and column == 0
            x = (width - PANEL_W) // 2 if last_alone else (16 if column == 0 else PANEL_W + 24)
            panel(x, 72 + row * (PANEL_H + 16), key)
    return "\n".join([*parts, '</g>', '</svg>', ''])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if generated files differ")
    parser.add_argument("--compact", action="store_true", help="generate one compact controller pair")
    parser.add_argument("--mode", choices=tuple(json.loads(CONTRACT.read_text(encoding="utf-8"))["modes"]),
                        default="1")
    parser.add_argument("--out", type=Path, help="destination for --compact (default: the bundled app asset)")
    args = parser.parse_args()
    data = json.loads(CONTRACT.read_text(encoding="utf-8"))
    colors = portal_colors()
    assets = ROOT / "src/frameport/ui/icons"
    if args.compact:
        outputs = {args.out or assets / f"quest-buttons-{args.mode}.svg": illustration(data, colors, args.mode)}
    else:
        outputs = {ROOT / "docs/images/controllers/quest-buttons.svg": illustration(data, colors)}
        outputs.update({assets / f"quest-buttons-{n}.svg": illustration(data, colors, n) for n in data["modes"]})
        doc = DOC.read_text(encoding="utf-8")
        before, rest = doc.split(START, 1)
        _, after = rest.split(END, 1)
        outputs[DOC] = before + START + "\n" + mapping_table(data) + "\n" + END + after
    stale = []
    for path, content in outputs.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    if stale:
        print("Regenerate with uv run python scripts/controller_diagram.py: " + ", ".join(stale))
    return int(bool(stale))


if __name__ == "__main__":
    raise SystemExit(main())
