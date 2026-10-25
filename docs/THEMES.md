# Themes

FramePort has three color themes, all dark. Switch in **Settings → Appearance**; the change applies at once.

| Theme | Look |
|---|---|
| **Portal** (default) | The logo's colors: orange for actions and selection, blue for secondary actions, progress and the PC side. |
| **Portal (OLED)** | Portal on true black, for OLED screens. |
| **Original** | FramePort's first look, with a violet accent. |

## Your own theme

A theme is a small JSON file.

1. In **Settings → Appearance**, pick the closest theme and click **Copy this theme as a file**.
2. Paste the theme into a text editor and save the file as `something.json`.
3. Change the name and the colors you want. Delete the colors you keep: missing ones come from the `base` theme.
4. Click **Install theme file…** and choose the file. FramePort checks the file, switches to the theme and keeps a
   copy, so you can delete the original.

The bin icon on a theme's card removes the theme.

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
| `ACCENT`, `ACCENT_SOFT`, `ON_ACCENT` | Main buttons and the current selection; the accent's tint; text on the accent |
| `SECONDARY`, `SECONDARY_SOFT` | Secondary buttons, progress, switch tracks; the secondary color's tint |
| `OK`, `WARN`, `ERROR`, `INFO` | Status: works, needs attention, failed, information |
| `PC` | PC VR games and "on this PC" |

FramePort refuses a theme it can't show well and says why: a light background (FramePort is dark only), hard-to-read
text, an unknown color name or a value that isn't a color. Example: [`themes/example-theme.json`](themes/example-theme.json).
