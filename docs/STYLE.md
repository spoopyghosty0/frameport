# Writing style for FramePort

Short rules so every screen, help text, doc and web page sounds the same. Code style is enforced by `ruff`.

## Words
- **Steam Frame** on first mention in a screen or document, **the Frame** after that. **Headset** only for the
  physical device you wear ("put the headset on"). Buttons say "… on Frame" ("Install on Frame").
- **Quest game** for Meta Quest apps, **Android app** for ordinary Android apps, **Linux app** for Linux programs,
  **PC VR game** for Windows VR games (never "PCVR"). Say Oculus or Rift only for games that use Oculus's own SDK.
  **Game** when the kind doesn't matter; **APK** only when the file itself matters.
- **Patch** for anything in a game's patch list (never "fix" for the same thing). **Recipe** for a game's chosen
  patches and settings (never "config"); say what it is the first time a page uses it.
- Tools by their names: OVRPort, Revive, Proton, Lepton, SteamVR.
- **Install** (first time), **Update** (a newer build), **Reinstall** (the same build again); **Play** starts a game.
- **Set up** = the one-time setup of a Frame; **connect** = linking FramePort to it. **Pair** only in Valve's own
  **Pair new host**. The **setup line** is the fixed `curl -sL frameport.app/s | bash`; the
  **setup command** is the one with this PC's address and a one-time code. FramePort's button is **Allow**.
- **Konsole** is the Frame's terminal. The way there is "SteamVR dashboard → Launch a program → Desktop, then app
  menu → System → Konsole": say it in full once per page, then just "Konsole".
- **This PC** / **your PC**, not "computer". **Unpack** a download (zip and tar.gz alike), not "unzip".
- One label per thing everywhere: **Docs**, **Report a problem**, **Download FramePort**.

## Form
- Buttons and headings in sentence case ("Add games", "Report a problem…"); one label per action everywhere.
- "…" at the end of a label when the action asks for more input (a dialog, a file picker).
- Sentences end with a period, including tooltips and help texts; labels and headings don't (also on the website).
- "and", not "&", in text. An em dash (—) for asides, a middle dot (·) between short facts.
- No Oxford comma ("Windows, macOS and Linux"). Straight apostrophes and quotes in source.
- Sizes in GiB/MiB (`i18n.fmt_size`), dates as YYYY-MM-DD HH:MM (`i18n.fmt_datetime`).
- US spelling (customize, analyze, color, license).
- Plain, technical wording: say what happens ("Steam restarts once"), no marketing adjectives.

## Length
Short beats complete: say what the user does or gets; leave out how it works unless they need it to act.
- Buttons: four words at most. Tooltips: one short sentence.
- Dialog text: two short sentences. Help texts: three sentences at most (about 200 characters).
- Doc paragraphs: three sentences at most; steps as numbered lists.
- Explain a technical term in a few words where a reader first meets it, or leave it out.
- Say a thing once and link to it elsewhere.

## Translations
- Every text the GUI shows goes through `tr("…")` or `tr_n("…", "…", n)` (see `src/frameport/i18n.py`); use
  templates with `.format()`, never f-strings inside `tr()`, and never decide anything from a displayed text.
- After changing texts run `python scripts/i18n_extract.py` (a test checks the template is current).
- CLI output, logs, diagnostics, catalog notes and launch-test diagnoses stay English.
