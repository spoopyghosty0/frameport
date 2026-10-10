# Themes

FramePort comes with three color themes, all dark:

| Theme | Look |
|---|---|
| **Portal** (default) | The logo's two portals: orange for actions and the current selection, blue for secondary actions, progress and the PC side. |
| **Portal (OLED)** | Portal on true black, for OLED screens. |
| **Original** | FramePort's first look, with a violet accent. |

Switch in **Settings → Appearance**. The change applies at once.

## Your own theme

A theme is a small JSON file. To make one:

1. In **Settings → Appearance**, pick the theme closest to what you want and click **Copy this theme as a file**.
2. Paste it into a text editor and save it as `something.json`.
3. Change the name and the colors you want different. You can delete every color you keep: missing colors come
   from the `base` theme.
4. Back in **Settings → Appearance**, click **Install theme file…** and choose the file. FramePort checks it,
   switches to it and keeps a copy in its data folder (`themes/`), so you can delete the original.

To remove an installed theme, click the bin icon on its card.

### Format

```json
{
  "name": "Ember & Ice",
  "base": "portal",
  "dual": true,
  "colors": {
    "ACCENT": "#FF5A36",
    "SECONDARY": "#5CE1E6"
  }
}
```

| Field | Meaning |
|---|---|
| `name` | Shown on the theme's card (up to 40 characters). |
| `base` | The built-in theme that fills in every color the file leaves out: `portal` (default), `portal_oled` or `original`. |
| `dual` | `true`: two-color touches like Portal's (blue-to-orange sidebar edge, two-color "FramePort", the selected tab's fade, blue secondary buttons). `false`: one accent, like Original. Default: the base's. |
| `colors` | Any of the colors below, as `#RRGGBB` or `#RGB`. Names may be upper or lower case. |

| Color | Used for |
|---|---|
| `BG` | Window background |
| `SIDEBAR` | Sidebar and activity panel |
| `SURFACE`, `SURFACE_2`, `SURFACE_3` | Cards; raised and hovered parts; inputs and chips |
| `BORDER`, `BORDER_STRONG` | Outlines |
| `TEXT`, `TEXT_2`, `TEXT_3` | Main text; secondary text; captions and disabled text |
| `ACCENT`, `ACCENT_SOFT`, `ON_ACCENT` | Main buttons and the current selection; its tint; text on it |
| `SECONDARY`, `SECONDARY_SOFT` | Secondary buttons, progress, switch tracks; its tint |
| `OK`, `WARN`, `ERROR`, `INFO` | Status: works, needs attention, failed, information |
| `PC` | PC VR games and "on this PC" |

FramePort refuses a theme file it can't show well, and says why: a light window background (FramePort is dark only),
text that's hard to read on cards, an unknown color name or a value that isn't a color. An example is in
[`themes/example-theme.json`](themes/example-theme.json).
