# Announcements

`current.md` is the one announcement FramePort shows as a pop-up in the app. Edit it on `main`: the app reads it
from GitHub `main` (raw.githubusercontent.com) at most every 6 hours, so a push reaches everyone without a release.

## Who sees it

- **Only people who already used FramePort before the announcement's `date`.** A new install never shows one, and
  nor does an announcement published before FramePort was installed.
- **Once.** Closing it in any way (the X, a click outside it, Escape, or its button) dismisses it for good. The app
  remembers dismissals in its settings (`announce.dismissed`).
- **A new announcement = a new `id`.** Fixing a typo with the same `id` doesn't show it again to anyone who closed it.
  A new `id` shows to everyone who used FramePort before its `date`, whether they closed the earlier one or not.

## The file

```markdown
---
active: true                    # false = nothing is shown (the fields are still checked)
id: meetup-2026-11             # letters, digits, . _ -  — change it for every new announcement
title: Steam Frame meetup on Discord  # required
date: 2026-11-02T18:00:00Z      # required: when it was published (UTC; a bare date = midnight UTC)
image: announcements/meetup.png # optional: a file in this folder, or an https URL (PNG, JPEG, WebP, GIF; ≤ 5 MiB)
image_alt: The meetup poster    # optional: what the picture shows (screen readers)
button: Join the meetup         # optional: an action button, with url
url: https://example.com/meetup
expires: 2026-11-30             # optional: not shown after this day (UTC, inclusive)
---
The body, in Markdown: **bold**, *italic*, `code`, [links](https://frameport.app/), lists, headings.
```

The body is Markdown (no HTML). A `date` in the future works as a schedule: nobody sees it before then.

Images look best at about 2:1 (for example 1200×600). The pop-up is 600 px wide and shows between 140 and 320 px of the
picture's height.

`pytest tests/test_announcements.py` checks this file, so a broken announcement fails CI instead of silently showing
nothing.

To turn announcements off for testing: `FRAMEPORT_NO_ANNOUNCEMENTS=1`.
