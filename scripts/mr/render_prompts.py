"""Render the in-headset room-scan prompts (SteamVR overlay images) into artifacts/roomscan/.

The Frame has no imaging library, so the agent shows pre-rendered PNGs through IVROverlay while it records the
passthrough depth mesh (agent `room_capture`). One image per state; "look around" comes in 13 variants (0-12 of 12
directions seen) so the progress ring can advance without drawing on the Frame. Colours = the Portal theme.

    uv run python scripts/mr/render_prompts.py [--check]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts" / "roomscan"
W, H = 1024, 400
BG, SURFACE, TEXT, TEXT_2 = (13, 14, 18, 235), (29, 31, 39, 255), (236, 238, 243, 255), (167, 172, 186, 255)
ACCENT, SECONDARY, OK = (255, 138, 31, 255), (58, 168, 255, 255), (74, 222, 128, 255)
DIRECTIONS = 12

PROMPTS = {
    "start": ("Turn on passthrough", "The scan runs while you can see your room:\n"
              "open the quick access menu and switch passthrough on."),
    "ceiling": ("Look up at the ceiling", "Hold it for a few seconds,\n"
                "then look back down and keep turning."),
    "floor": ("Look down at the floor", "Hold it for a few seconds,\nthen keep turning."),
    "done": ("Room scanned", "You can take the headset off or start a mixed reality game."),
    "failed": ("Scan stopped", "The headset's cameras sent no room view.\nStart the scan again from FramePort."),
}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for name in ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",):
        for base in ("/usr/share/fonts/truetype/dejavu", "C:/Windows/Fonts", "/Library/Fonts"):
            p = Path(base) / name
            if p.is_file():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size=size)


def panel(title: str, body: str, seen: int | None = None, accent=ACCENT) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, W - 4, H - 4), radius=48, fill=BG, outline=SURFACE, width=4)
    d.rounded_rectangle((4, 4, 20, H - 4), radius=8, fill=accent)  # portal edge
    x = 64
    if seen is not None:  # progress ring: one segment per 30 degree direction seen
        cx, cy, r = 150, H // 2, 92
        for i in range(DIRECTIONS):
            a0 = -90 + i * 360 / DIRECTIONS + 3
            d.arc((cx - r, cy - r, cx + r, cy + r), a0, a0 + 360 / DIRECTIONS - 6,
                  fill=(SECONDARY if i < seen else SURFACE), width=26)
        d.text((cx, cy), f"{seen}/{DIRECTIONS}", font=font(44, True), fill=TEXT, anchor="mm")
        x = 290
    d.text((x, 108), title, font=font(54, True), fill=TEXT, anchor="lm")
    y = 180
    for line in body.split("\n"):
        d.text((x, y), line, font=font(32), fill=TEXT_2, anchor="lm")
        y += 48
    return img


def images() -> dict[str, bytes]:
    out = {}
    for key, (title, body) in PROMPTS.items():
        out[key] = panel(title, body, accent=OK if key == "done" else ACCENT)
    for n in range(DIRECTIONS + 1):
        out[f"look_{n:02d}"] = panel("Look around the room", "Turn slowly until the ring is full:\n"
                                     "every wall and your furniture.", seen=n, accent=SECONDARY)
    data = {}
    for key, img in out.items():
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        data[f"{key}.png"] = buf.getvalue()
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if artifacts/roomscan is out of date")
    args = ap.parse_args()
    data = images()
    if args.check:
        stale = [n for n, b in data.items() if not (OUT / n).is_file() or (OUT / n).read_bytes() != b]
        if stale:
            print("out of date:", ", ".join(stale))
            return 1
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, b in data.items():
        p = OUT / name
        if not p.is_file() or p.read_bytes() != b:
            p.write_bytes(b)
    print(f"{len(data)} prompts in {OUT.relative_to(ROOT)} "
          f"({sum(map(len, data.values())) // 1024} KiB, {hashlib.sha256(b''.join(data.values())).hexdigest()[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
