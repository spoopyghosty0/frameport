"""Post-production for the demo tour, with ffmpeg only: screencast frames → a constant-frame-rate video, one clip per
scene with its caption faded in, title and end cards, crossfades between everything, then the final MP4, a poster and
the short looping WebP teaser for the README.

The filter graphs are built by pure functions (tested without ffmpeg); `run` executes a command and fails loudly.
Cards and captions are small HTML pages rendered by the same Chromium (render_card), in the app's own colours.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FPS = 30
SIZE = (1920, 1080)


def run(args: list[str]) -> None:
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *map(str, args)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({r.returncode}): {' '.join(cmd)}\n{r.stderr[-3000:]}")


# ------------------------------------------------------------------ frames → video

def concat_list(frames: list[tuple[float, str]], end: float | None = None, min_frame: float = 1 / 120) -> str:
    """An ffconcat script showing each screencast frame until the next one came (Chrome sends frames only on
    changes); the last one until `end` (wall-clock), else for one frame time."""
    lines = ["ffconcat version 1.0"]
    for i, (t, name) in enumerate(frames):
        nxt = frames[i + 1][0] if i + 1 < len(frames) else (end if end is not None else t + 1 / FPS)
        lines += [f"file '{name}'", f"duration {max(min_frame, nxt - t):.6f}"]
    if frames:
        lines.append(f"file '{frames[-1][1]}'")  # ffconcat: the last entry's duration needs a following file
    return "\n".join(lines) + "\n"


def frames_to_video(folder: Path, out: Path, end: float | None = None, size: tuple[int, int] = SIZE) -> float:
    """The screencast (folder/frames.json + JPEGs) as a constant-frame-rate H.264 file (high quality, an
    intermediate). Returns the wall-clock time of its first frame (scene times are relative to it)."""
    frames = [tuple(f) for f in json.loads((folder / "frames.json").read_text())]
    if not frames:
        raise RuntimeError("the screencast has no frames")
    (folder / "frames.ffconcat").write_text(concat_list(frames, end))
    w, h = size
    run(["-f", "concat", "-safe", "0", "-i", folder / "frames.ffconcat",
         "-vf", f"fps={FPS},scale={w}:{h}:flags=lanczos:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", out])
    return frames[0][0]


# ------------------------------------------------------------------ scenes

def caption_window(duration: float, show: float = 4.2, fade: float = 0.45, lead: float = 0.35) -> tuple[float, float]:
    """When a scene's caption is on screen: from `lead` s in, for `show` s (shorter scenes: until 0.6 s before
    their end), fading `fade` s in and out. Returns (start, end)."""
    start = min(lead, max(0.0, duration / 4))
    end = min(start + show, max(start + 2 * fade, duration - 0.6))
    return start, end


def scene_filter(duration: float, fade: float = 0.45, size: tuple[int, int] = SIZE) -> str:
    """The filter graph laying a caption (input 1, a still RGBA PNG looped) over a scene (input 0)."""
    start, end = caption_window(duration, fade=fade)
    return (f"[1:v]scale={size[0]}:{size[1]}:flags=lanczos,format=rgba,fade=t=in:st={start:.3f}:d={fade}:alpha=1,"
            f"fade=t=out:st={end - fade:.3f}:d={fade}:alpha=1[cap];"
            f"[0:v][cap]overlay=0:0:format=auto:shortest=1,format=yuv420p[v]")


def cut_scene(raw: Path, start: float, duration: float, caption: Path | None, out: Path,
              size: tuple[int, int] = SIZE) -> None:
    """One scene from the raw recording, with its caption."""
    if caption is None:
        run(["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", raw, "-an",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p", out])
        return
    run(["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", raw,
         "-loop", "1", "-t", f"{duration:.3f}", "-i", caption,
         "-filter_complex", scene_filter(duration, size=size), "-map", "[v]", "-an", "-r", FPS,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", out])


def card_clip(image: Path, duration: float, out: Path, zoom: float = 0.03) -> None:
    """A title/end card: the still, slowly zooming in by `zoom` over its duration (alive, not frozen)."""
    frames = max(1, int(duration * FPS))
    w, h = SIZE
    run(["-loop", "1", "-t", f"{duration:.3f}", "-i", image,
         "-vf", f"scale={w * 2}:{h * 2},zoompan=z='1+{zoom}*on/{frames}':x='iw/2-(iw/zoom/2)':"
                f"y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps={FPS},format=yuv420p",
         "-frames:v", frames, "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", out])


# ------------------------------------------------------------------ joining

def xfade_offsets(durations: list[float], transition: float) -> list[float]:
    """Where each crossfade starts on the joined timeline: clip k+1 begins `transition` s before clip k ends."""
    offsets, total = [], 0.0
    for d in durations[:-1]:
        total += d - transition
        offsets.append(round(total, 3))
    return offsets


def xfade_graph(durations: list[float], transition: float, size: tuple[int, int] = SIZE) -> tuple[str, str]:
    """(filter graph, output label) crossfading inputs 0..n-1 in order. Every input is brought to the same size,
    frame rate, timebase and pixel format first (xfade refuses anything else)."""
    w, h = size
    parts = [f"[{k}:v]fps={FPS},scale={w}:{h}:flags=lanczos,setsar=1,format=yuv420p,settb=AVTB[n{k}]"
             for k in range(len(durations))]
    prev = "[n0]"
    for k, off in enumerate(xfade_offsets(durations, transition), start=1):
        label = f"[x{k}]"
        parts.append(f"{prev}[n{k}]xfade=transition=fade:duration={transition}:offset={off}{label}")
        prev = label
    parts.append(f"{prev}format=yuv420p[vout]")
    return ";".join(parts), "[vout]"


def joined_length(durations: list[float], transition: float) -> float:
    return sum(durations) - transition * (len(durations) - 1)


def join(clips: list[Path], durations: list[float], out: Path, transition: float = 0.5, crf: int = 23,
         size: tuple[int, int] = SIZE, extra: list[str] | None = None) -> None:
    graph, label = xfade_graph(durations, transition, size)
    args = []
    for c in clips:
        args += ["-i", c]
    run([*args, "-filter_complex", graph, "-map", label, "-an", "-r", FPS, "-c:v", "libx264", "-preset", "slow",
         "-crf", crf, "-pix_fmt", "yuv420p", "-movflags", "+faststart", *(extra or []), out])


def encode_within(clips: list[Path], durations: list[float], out: Path, budget_mb: float, transition: float = 0.5,
                  crf: int = 22, max_crf: int = 32) -> int:
    """The final MP4 at the best quality (lowest CRF) that fits `budget_mb`. Returns the CRF used."""
    while True:
        join(clips, durations, out, transition, crf)
        if out.stat().st_size <= budget_mb * 1e6 or crf >= max_crf:
            return crf
        crf += 2


def poster(video: Path, at: float, out: Path) -> None:
    run(["-ss", f"{at:.2f}", "-i", video, "-frames:v", "1", "-q:v", "2", out])


def teaser_webp(clips: list[Path], durations: list[float], out: Path, width: int = 960, fps: int = 15,
                budget_mb: float = 4.0, transition: float = 0.35) -> int:
    """A looping animated WebP of the highlight clips (README: plays inline where an MP4 wouldn't). Lowers the
    quality until it fits `budget_mb`. Returns the quality used."""
    graph, label = xfade_graph(durations, transition)
    graph = graph.replace("format=yuv420p[vout]", f"fps={fps},scale={width}:-2:flags=lanczos[vout]")
    args = []
    for c in clips:
        args += ["-i", c]
    quality = 72
    while True:
        run([*args, "-filter_complex", graph, "-map", label, "-an", "-c:v", "libwebp_anim", "-lossless", "0",
             "-quality", quality, "-compression_level", "6", "-loop", "0", out])
        if out.stat().st_size <= budget_mb * 1e6 or quality <= 30:
            return quality
        quality -= 8


# ------------------------------------------------------------------ cards and captions

def theme_colors(name: str = "portal") -> dict:
    from frameport.ui import theme as T

    T.set_theme(name)
    return {k: getattr(T, k) for k in ("BG", "SURFACE", "SURFACE_2", "BORDER", "TEXT", "TEXT_2", "TEXT_3", "ACCENT",
                                       "SECONDARY")}


FONT_CSS = ('<link rel="preconnect" href="https://fonts.gstatic.com">'
            '<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700;900&display=block" '
            'rel="stylesheet">')
FONT = "'Roboto', 'Segoe UI', system-ui, sans-serif"


def caption_html(title: str, subtitle: str, c: dict, left: int = 372, bottom: int = 56) -> str:
    """A lower third: a dark glass panel with a blue→orange edge (the logo's two portals), title + one line."""
    e = html.escape
    return f"""<!doctype html><html><head><meta charset="utf-8">{FONT_CSS}<style>
html,body{{margin:0;width:{SIZE[0]}px;height:{SIZE[1]}px;background:transparent;font-family:{FONT}}}
.cap{{position:absolute;left:{left}px;bottom:{bottom}px;max-width:1180px;padding:26px 40px 28px 44px;
border-radius:22px;background:rgba(12,14,20,.86);box-shadow:0 18px 50px rgba(0,0,0,.55),
inset 0 0 0 1px rgba(255,255,255,.08);overflow:hidden}}
.cap:before{{content:"";position:absolute;left:0;top:0;bottom:0;width:8px;
background:linear-gradient(180deg,{c['SECONDARY']},{c['ACCENT']})}}
h1{{margin:0;font-size:46px;font-weight:700;color:{c['TEXT']};letter-spacing:-.3px}}
p{{margin:10px 0 0;font-size:28px;font-weight:400;color:{c['TEXT_2']}}}
</style></head><body><div class="cap"><h1>{e(title)}</h1>{f'<p>{e(subtitle)}</p>' if subtitle else ''}</div>
</body></html>"""


def card_html(title: str, subtitle: str, footer: str, logo_svg: str, c: dict) -> str:
    """A full-screen card: the logo, the two-colour wordmark, a line, a footer; a soft portal glow behind."""
    e = html.escape
    word = ('<span style="color:{a}">Frame</span><span style="color:{b}">Port</span>'
            .format(a=c["ACCENT"], b=c["SECONDARY"])) if title == "FramePort" else e(title)
    return f"""<!doctype html><html><head><meta charset="utf-8">{FONT_CSS}<style>
html,body{{margin:0;width:{SIZE[0]}px;height:{SIZE[1]}px;background:{c['BG']};font-family:{FONT};overflow:hidden}}
.glow{{position:absolute;inset:0;
background:radial-gradient(ellipse 46% 52% at 42% 50%,{c['SECONDARY']}33,transparent 70%),
radial-gradient(ellipse 46% 52% at 58% 50%,{c['ACCENT']}2e,transparent 70%)}}
.wrap{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}}
.logo{{width:220px;height:220px;filter:drop-shadow(0 18px 40px rgba(0,0,0,.6))}}
.logo svg{{width:100%;height:100%}}
h1{{margin:34px 0 0;font-size:112px;font-weight:900;letter-spacing:-2px;color:{c['TEXT']}}}
p{{margin:18px 0 0;font-size:38px;color:{c['TEXT_2']};font-weight:400}}
.foot{{position:absolute;bottom:64px;left:0;right:0;text-align:center;font-size:26px;color:{c['TEXT_3']}}}
</style></head><body><div class="glow"></div><div class="wrap"><div class="logo">{logo_svg}</div>
<h1>{word}</h1><p>{e(subtitle)}</p></div><div class="foot">{e(footer)}</div></body></html>"""


def step_card_html(card: dict, c: dict) -> str:
    """An instruction card for what happens outside FramePort (a download, a command on the Frame): an eyebrow
    (where you are), a heading, numbered steps, an optional command in a terminal box and a note. Steps and the note
    may mark words **bold** (a button or menu name)."""
    e = html.escape

    def rich(text: str) -> str:
        return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", e(text))
    steps = "".join(f'<li><span class="n">{i}</span><span>{rich(s)}</span></li>'
                    for i, s in enumerate(card.get("steps") or [], 1))
    code = (f'<div class="code"><span class="prompt">$</span>{e(card["code"])}</div>' if card.get("code") else "")
    return f"""<!doctype html><html><head><meta charset="utf-8">{FONT_CSS}<style>
html,body{{margin:0;width:{SIZE[0]}px;height:{SIZE[1]}px;background:{c['BG']};font-family:{FONT};overflow:hidden}}
.glow{{position:absolute;inset:0;
background:radial-gradient(ellipse 40% 55% at 12% 30%,{c['SECONDARY']}26,transparent 70%),
radial-gradient(ellipse 45% 55% at 92% 80%,{c['ACCENT']}22,transparent 70%)}}
.wrap{{position:absolute;left:180px;right:180px;top:0;bottom:0;display:flex;flex-direction:column;
justify-content:center;gap:30px}}
.eyebrow{{font:600 26px/1 'Roboto Mono','Consolas',monospace;letter-spacing:3px;text-transform:uppercase;
color:{c['ACCENT']}}}
h1{{margin:0;font-size:76px;font-weight:700;letter-spacing:-1px;color:{c['TEXT']};line-height:1.08}}
.line{{margin:0;font-size:34px;color:{c['TEXT_2']};max-width:1400px;line-height:1.35}}
ol{{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-direction:column;gap:22px}}
li{{display:flex;gap:26px;align-items:baseline;font-size:36px;color:{c['TEXT']};line-height:1.35}}
li b{{color:{c['ACCENT']};font-weight:700}}
.n{{flex:none;width:56px;height:56px;border-radius:50%;display:inline-flex;align-items:center;
justify-content:center;font-weight:700;font-size:28px;color:{c['TEXT']};position:relative;top:-4px;
background:linear-gradient(135deg,{c['SECONDARY']}66,{c['ACCENT']}88)}}
.code{{margin-top:6px;align-self:flex-start;max-width:100%;padding:26px 36px;border-radius:18px;
background:#05060A;border:2px solid {c['BORDER']};font:500 40px/1.3 'Roboto Mono','Consolas',monospace;
color:#E8F1FF;box-shadow:0 20px 60px rgba(0,0,0,.5)}}
.prompt{{color:{c['SECONDARY']};margin-right:22px}}
.note{{margin:0;font-size:26px;color:{c['TEXT_3']};max-width:1400px}}
.note b{{color:{c['TEXT_2']}}}
.brand{{position:absolute;left:180px;bottom:54px;font-size:26px;font-weight:900;letter-spacing:-.3px}}
</style></head><body><div class="glow"></div><div class="wrap">
{f'<div class="eyebrow">{e(card["eyebrow"])}</div>' if card.get("eyebrow") else ''}
<h1>{e(card["heading"])}</h1>
{f'<p class="line">{rich(card["line"])}</p>' if card.get("line") else ''}
{f'<ol>{steps}</ol>' if steps else ''}{code}
{f'<p class="note">{rich(card["note"])}</p>' if card.get("note") else ''}
</div><div class="brand"><span style="color:{c['ACCENT']}">Frame</span><span style="color:{c['SECONDARY']}">Port</span>
</div></body></html>"""


def render_card(playwright, page_html: str, out: Path, transparent: bool = False) -> Path:
    """Render an HTML card to a PNG at the video size (fonts loaded first)."""
    browser = playwright.chromium.launch()
    try:
        page = browser.new_page(viewport={"width": SIZE[0], "height": SIZE[1]})
        page.set_content(page_html, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        page.screenshot(path=str(out), omit_background=transparent)
    finally:
        browser.close()
    return out
