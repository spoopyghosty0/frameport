# Contributing

Thanks for helping. The quickest contributions:

- **A game works on the Frame?** In FramePort: the game's menu → **Recipe** → **Share working recipe…** opens a
  prefilled issue with its recipe. Accepted recipes become built-in for everyone.
- **Something doesn't work?** The game's menu → **Troubleshoot** → **Report a problem…** saves a diagnostics zip
  (personal data removed) and opens a prefilled issue to attach it to.

## Development
```
uv sync --extra dev          # Python 3.11+, uv (https://docs.astral.sh/uv/)
uv run frameport-gui         # the app
uv run frameport --help      # the command line
uv run pytest                # tests (no headset or game files needed)
uv run ruff check .          # lint
```
- Code and architecture: `docs/ARCHITECTURE.md` (the detailed module notes are in `CLAUDE.md`); per-game symptoms
  and fixes: `docs/PLAYBOOK.md`; runtime facts: `docs/FRAME_RUNTIME.md`.
- Interface wording: `docs/STYLE.md`. GUI texts go through `tr()`; after changing them run
  `python scripts/i18n_extract.py`.
- Translations: see [Translating FramePort](#translating-frameport) below.
- New fixes are patch modules (`src/frameport/patches/`), with a triage signature (`catalog/triage.yaml`), a PLAYBOOK
  row and a unit test. After a new or changed patch, game setting, recipe or triage signature run
  `python scripts/patch_docs.py` (regenerates `docs/PATCHES.md`).
- Screenshots and videos in the docs are rendered, never taken by hand: see `docs/SHOWCASE.md`.
- Native binaries in `artifacts/` are rebuilt with `python native/build.py` (downloads the NDK).

## Adding a tested game

**Share working recipe…** (above) opens a prefilled GitHub issue with the exact recipe; after review a workflow
turns it into `catalog/games/<package>.yaml`, and installed copies of FramePort pick it up within 6 hours. Without the
app: [working recipe form](https://github.com/spoopyghosty0/frameport/issues/new?template=working-config.yml) ·
[problem report form](https://github.com/spoopyghosty0/frameport/issues/new?template=bug-report.yml).
After catalog changes, regenerate the games list (`python scripts/compat_list.py`) and the patch reference
(`python scripts/patch_docs.py`).

## Translating FramePort
The app's texts (screens, help, patch and setting descriptions) can be translated; the command line, logs and
diagnostics stay English. No programming needed:

1. Copy `src/frameport/locales/template.json` to `src/frameport/locales/<code>.json`, where `<code>` is the language
   code (`de`, `fr`, `pt`, `ja`, …).
2. Add `"_language": "<the language's own name>"` (for example `"Deutsch"`); it's shown in Settings → Appearance →
   Language.
3. Fill in the values. Each key is the English text; leave a value empty to keep the English text. Plural entries
   have a list value: `["<one>", "<other>"]`. Keep `{placeholders}` such as `{title}` or `{n}` exactly as they are,
   and keep `…`, `·` and line breaks where the English has them.
4. Try it: `uv run frameport-gui`, choose the language in Settings → Appearance → Language (shown once a
   translation exists), restart FramePort.
5. Open a pull request with the file. When the English texts change, `python scripts/i18n_extract.py` refreshes the
   template; new texts then show in English until they're translated.

Wording rules for English (and a guide for tone) are in `docs/STYLE.md`.

## Ground rules
- Use only games you own. Contributions that help bypass DRM, licence or entitlement checks, or that add game files,
  are not accepted.
- Never commit personal data (IP addresses, user names, home paths, Steam IDs).
- By contributing you agree to license your work under GPL-3.0-only.
