# "Install with FramePort" button

A button for web pages and READMEs that opens a game straight in FramePort. Clicking it hands FramePort an install
link; FramePort shows what it would download and asks before it does anything (see
[Install links](INSTALL.md#install-links-install-with-framedrop-buttons)).

<p>
  <img src="badges/install-with-frameport-animated.svg" alt="Install with FramePort" width="216" height="60">
</p>

## Files

All files are in [`docs/badges/`](badges/). Each SVG is self-contained (no external fonts or images), 216 × 60.

| File | Use it for |
|---|---|
| [`install-with-frameport.svg`](badges/install-with-frameport.svg) | Everywhere. No motion. |
| [`install-with-frameport-animated.svg`](badges/install-with-frameport-animated.svg) | GitHub READMEs and other pages that only allow images. The glow plays by itself every 6 seconds: blue swells from the left, the portal between "Frame" and "Port" flares, then orange swells on the right. |
| [`install-with-frameport-hover.svg`](badges/install-with-frameport-hover.svg) | Websites where you can paste HTML. The glow brightens and the middle portal lights up while the pointer is on the button. It only reacts to the mouse when the SVG code is placed in the page itself (see below). |
| [`install-with-frameport@2x.png`](badges/install-with-frameport@2x.png) | Places without SVG support (some forums, email). 432 × 120; show it at 216 × 60. |

Both animated versions stop moving for visitors who turn on reduced motion in their system settings.

## The link

The button links to an install link, which names either a manifest or one file:

```
frameport://install?manifest=<URL-encoded https address of a manifest .json>
frameport://install?url=<URL-encoded https address of an .apk, a Linux build (.zip, .tar.*, .AppImage) or a Windows .exe>
```

The manifest is the FrameDrop format, which FramePort reads as it is:

```json
{
  "schema": "framedrop.install/v1",
  "name": "My Game",
  "files": [{ "url": "https://example.com/my-game.apk", "sha256": "<64 hex digits>" }],
  "frameport": { "description": "One sentence about the game.", "icon": "https://example.com/icon.png" }
}
```

`name` becomes the game's title in Steam. `sha256` is optional but recommended: FramePort checks the download
against it. The `frameport` object is optional and ignored by FrameDrop; its icon and description are shown in the
install question. Only public `https://` addresses are accepted.

To build a link from Python: `frameport.deeplink.make_link(manifest_url="https://…/my-game.json")`.

## On a website

Image version (any page):

```html
<a href="frameport://install?manifest=https%3A%2F%2Fexample.com%2Fmy-game.json">
  <img src="install-with-frameport.svg" alt="Install with FramePort" width="216" height="60">
</a>
```

Hover version: paste the contents of `install-with-frameport-hover.svg` inside the link instead of the `<img>`, and
give the link an accessible name:

```html
<a href="frameport://install?manifest=https%3A%2F%2Fexample.com%2Fmy-game.json" aria-label="Install with FramePort">
  <svg …>…</svg>  <!-- the whole file -->
</a>
```

Host the files yourself, or load them from jsDelivr, which serves them with the right content type:
`https://cdn.jsdelivr.net/gh/spoopyghosty0/frameport@main/docs/badges/install-with-frameport.svg` (replace `main`
with a release tag such as `v0.12.0` to pin a version; jsDelivr caches `@main` for up to 12 hours).

## In a GitHub README

GitHub removes links that don't start with `https://` or `http://` from READMEs, so a `frameport://` link there is
not clickable. Link the button to an `https://` page instead, for example your release page with instructions, or
FrameDrop's install page, which opens `framedrop://` links (FramePort opens those too while Settings → Install links
→ `framedrop://` is on):

```markdown
[![Install with FramePort](https://cdn.jsdelivr.net/gh/spoopyghosty0/frameport@main/docs/badges/install-with-frameport-animated.svg)](https://framedropvr.com/install?manifest=https%3A%2F%2Fexample.com%2Fmy-game.json)
```

Use the animated or still SVG; the hover version doesn't react inside a README, because GitHub shows SVGs as
images.

## If a visitor doesn't have FramePort

A `frameport://` link does nothing visible (or shows the browser's "no app for this link" message) when FramePort
isn't installed. Put a line next to the button such as "Needs [FramePort](https://github.com/spoopyghosty0/frameport)
on Windows or Linux." On macOS, web pages can't hand links to FramePort yet: users paste the link into Add games →
Add from a link….

## Usage rules

- Keep the alt text "Install with FramePort".
- Don't recolor, stretch, crop or redraw the button, and don't show it smaller than 40 px tall.
- Leave some space around it (at least 8 px) so it doesn't touch other buttons.
- It works on light and dark pages as it is; there is no light version.
