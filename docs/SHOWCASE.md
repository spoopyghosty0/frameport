# Screenshots and videos

The pictures in the docs and the videos are rendered by scripts, never taken by hand. The scripts show the real
FramePort GUI with a demo library and a pretend Steam Frame, drive it the way a person would, and film or photograph
it:

| What | Source | Output |
|---|---|---|
| Docs screenshots | `docs/showcase/shots.yaml` | `docs/images/*.png` |
| Demo tour (about 95 s) | `docs/showcase/videos/tour.yaml` | `docs/media/frameport-tour.mp4` (+ `.jpg` poster), `docs/images/tour-teaser.webp` (README) |
| Install tutorial (about 90 s) | `docs/showcase/videos/install.yaml` | `docs/media/frameport-install.mp4` (+ `.jpg` poster) |

Nothing comes from anyone's own library. The demo library (`docs/showcase/demo-library.yaml`) lists real catalog
games, so recipes and patches are the ones FramePort really uses. Their technical analysis (`demo-analyses.json`:
engine, VR API, libraries, sizes) was exported from real APK analyses without any paths. Art, in-game screenshots
and store details come from the same public sources the app uses and are cached in `~/.cache/frameport-showcase`.
The renderer refuses to run in FramePort's own data folder, scans the demo data for home paths and IP addresses
first, and stops when a store didn't send art (placeholders never reach the docs; `--allow-missing-art` overrides
that). Frame discovery is switched off during a render (no real device on your network can appear), and the setup
command shows placeholders for the address and code (each PC has its own), never this computer's.

## Running it

You need the dev extras, Chromium for Playwright and ffmpeg:

```
UV_LINK_MODE=copy uv sync --extra dev
uv run playwright install chromium        # once
uv run python scripts/showcase/render_docs.py               # all screenshots → docs/images (changed ones only)
uv run python scripts/showcase/render_docs.py --only game,monitor --out /tmp/shots
uv run python scripts/showcase/record_video.py install --draft   # quick 720p check of a storyboard edit
uv run python scripts/showcase/record_video.py              # every video → docs/media (+ the README teaser)
uv run python scripts/showcase/record_video.py tour install # some
```

Every screenshot is rendered twice, each in a fresh window, and kept only when both agree (the headless GPU now and
then draws a glyph wrong); a third render settles a disagreement, and `--fast` renders once for trying out an edit.
A shot whose steps fail is tried again and never written. A screenshot is rewritten only when it visibly changed
(`render_docs.changed`: anti-aliasing noise doesn't count), so running it twice changes nothing. `--check` writes
nothing and exits 1 when a docs image is out of date.

For videos, `--names` prints what can be clicked after each scene and `SHOWCASE_DEBUG=1` prints every step with its
start time (which step waited); `--draft --scenes a,b` records only some scenes. A video is encoded at the best
quality that fits its `budget_mb` (12 by default), a teaser at 4 MB.

## Videos

One storyboard per video in `docs/showcase/videos/<name>.yaml`; adding a file adds a video (CI and releases pick it
up by itself). The output must be `docs/media/frameport-<name>.mp4`: releases attach it as `FramePort-<name>.mp4`.

```yaml
output: docs/media/frameport-install.mp4
teaser: docs/images/tour-teaser.webp     # optional: a looping WebP from the scenes' `teaser` parts
budget_mb: 12
poster_at: 9                             # the poster frame, seconds after the title card
start:
  profile: fresh          # demo = FramePort in use (games installed, BONELAB running); fresh = its first start
  frame: disconnected     # connected (default) or disconnected
  route: welcome          # the page filming starts on (default library)
  stream: live            # the monitor stream: live (default) or frozen
setup:                    # steps before filming starts (a failure stops the recording)
  - hook: {name: first_run, args: ["D:/Games", 1.4]}
vars: {game: com.playful.LuckysTale}     # ${game} in any step
title: {heading: Installing FramePort, line: …, seconds: 3.4}
end: {heading: You're set, line: …, footer: github.com/spoopyghosty0/frameport, seconds: 4}
scenes:
  - name: connect                        # a filmed scene
    caption: ["Connect your Steam Frame", "Once: FramePort shows a command to run on the Frame"]
    teaser: [0.6, 4.0]                   # optional: this part goes into the teaser
    hold: 1.5                            # seconds of filming after the last step (default 0.6)
    steps: [...]
  - name: on-the-frame                   # an instruction card: for what happens outside FramePort
    seconds: 10
    card:
      eyebrow: "Step 3 · On the Frame, first time only"
      heading: Run the setup command
      steps: ["In the SteamVR dashboard: **Launch a program** → **Desktop**", …]   # **bold** = a button or menu
      code: "curl -fsS <your-PC-address>:8765/<one-time-code> | bash"
      note: "Your address and code differ."
```

Filmed scenes are one continuous session: each starts where the last one left off. Cards are rendered at the end
and slotted in, with crossfades between everything. The `fresh` profile starts FramePort as on a new computer: an
empty library, the welcome screen and a Frame that isn't set up. Its games wait for the pretend folder scan.

## Writing steps

Shots and scenes use one step language (`scripts/showcase/steps.py`). A step is a one-key mapping:

```yaml
- go: monitor                                  # a sidebar route
- nav: "Monitor"                               # click that tab (checked: the page really changed)
- open_game: ${game}                           # or {package: …, advanced: true}
- call: {fn: settings_dialog, args: ["${game}"]}   # any app method
- hover: "Batman: Arkham Shadow"               # the pointer: by name…
- click: {name: "Lucky's Tale", dy: 0.35}      # …offset inside the element (fraction of its size, or px)
- click: "Install on Frame"                    # right_click, double_click, drag: [from, …, to]
- type: "ri"                                   # press: Escape
- wait_for: "BONELAB · 2026-10-04 21:50"
- wait: 1.2
- hook: {name: held_install, args: ["${new_game}"]}   # the pretend Frame's scripted events
```

Elements are found by name through Flutter's accessibility tree: their visible text, a tooltip or a semantics
label. Exact names win over prefixes, and buttons and cards win over plain text. When several match, the innermost
one wins (a list carries the text of its rows; the row is meant). Icon-only buttons need a tooltip to be found,
which also helps screen readers. The pointer moves on eased, slightly curved paths and is drawn into the picture,
with a ring on every click. To open a game from a cover card, click its title (`dy: 0.35`): the card's middle is its
Play/Install button.

Hooks (`steps.HOOKS`) cover what a real Frame would do:
- `fake_install`: an install through the real job queue with every real stage name and a launch test that passes.
- `held_install`: an install that stops mid-upload, for screenshots of the progress.
- `connect` and `disconnect`
- `first_run`: a first start. The welcome screen's tool download is a pretend one, and "Scan a folder…" scans a
  folder at once (the native folder picker can't be scripted).
- `pairing_done`: the Frame ran the setup command, so FramePort connects.
- `live_stream`: a test picture through the real relay.
- `monitor_details`, `type_tab`, `select_files` and `settings_section`.

In a video, clicking Install runs the scripted install, and the game is then installed on the pretend Frame.

Pictures must not change from one run to the next:
- Screenshot and file dates are fixed.
- The Monitor's stream stops after its two-minute history.
- The pointer leaves the page before a still picture is taken.
- Each shot gets a fresh window and browser.
- A picture is taken once the page has stopped changing.

Live data, such as the live view's relay port and data rate, doesn't belong in a docs screenshot.

## How it runs on GitHub

The `showcase` workflow (`.github/workflows/showcase.yml`):
1. **When it runs:** on pushes to `main` that touch the UI, its translations or the showcase files, and on
   **Run workflow**.
2. **What it renders:** the screenshots every time. Videos when their storyboard changed (all of them when the
   scripts changed), or the ones named on a manual run (`all`, or e.g. `tour install`). UI tweaks alone don't
   re-commit 10 MB videos.
3. **What happens next:** every render is uploaded as the run's `showcase` artifact. When a picture changed, the
   workflow opens or updates the pull request on branch `showcase/update`, which you review and merge.

Releases attach every committed video as `FramePort-<name>.mp4`.

## Gallery

These are renders the README doesn't show (yet):

| | |
|---|---|
| ![Installing](images/install-progress.png) | ![Live view](images/live-view.png) |
| ![Appearance](images/appearance.png) | ![Game page](images/game.png) |
