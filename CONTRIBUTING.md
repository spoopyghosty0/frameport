# Contributing

Thanks for helping. The quickest contributions:

- **A game works on the Frame?** Game menu → **Recipe → Share working recipe…** opens a prefilled issue. After
  review the recipe becomes a built-in recipe (`catalog/games/<package>.yaml`), and installed copies of FramePort get
  the recipe within 6 hours.
- **Something doesn't work?** Game menu → **Troubleshoot → Report a problem…** saves a diagnostics zip (personal data
  removed) and opens a prefilled issue to attach the zip to.

Without the app: [working recipe form](https://github.com/spoopyghosty0/frameport/issues/new?template=working-config.yml)
· [problem report form](https://github.com/spoopyghosty0/frameport/issues/new?template=bug-report.yml).

## Development

```
uv sync --extra dev          # Python 3.11+, uv (https://docs.astral.sh/uv/)
uv run frameport-gui         # the app
uv run frameport --help      # the command line
uv run pytest                # tests (no headset or game files needed)
uv run ruff check .          # lint
```

- Where things are: `docs/ARCHITECTURE.md` (module notes in `CLAUDE.md`), `docs/PLAYBOOK.md` (symptoms and fixes),
  `docs/FRAME_RUNTIME.md` (runtime facts).
- A new fix = a patch module (`src/frameport/patches/`) + a triage signature (`catalog/triage.yaml`) + a PLAYBOOK row
  + a unit test.
- After changing a patch, game setting, recipe or triage signature: `python scripts/patch_docs.py`. After catalog
  changes also `python scripts/compat_list.py`.
- GUI texts go through `tr()`; after changing them run `python scripts/i18n_extract.py`. Wording rules:
  `docs/STYLE.md`.
- Docs screenshots and videos are rendered, never taken by hand: `docs/SHOWCASE.md`.
- Native binaries in `artifacts/`: `python native/build.py` (downloads the NDK).

## Translating FramePort

The app's texts can be translated, no programming needed. The command line, logs and diagnostics stay English.

1. Copy `src/frameport/locales/template.json` to `src/frameport/locales/<code>.json` (`de`, `fr`, `pt`, `ja`, …).
2. Add `"_language": "<the language's own name>"`, for example `"Deutsch"`.
3. Fill in the values. Each key is the English text; an empty value keeps the English. Plural entries take a list:
   `["<one>", "<other>"]`. Keep `{placeholders}` such as `{title}` exactly, and `…`, `·` and line breaks where the
   English has them.
4. Try the translation: `uv run frameport-gui`, pick the language in Settings → Appearance → Language, restart FramePort.
5. Open a pull request with the file. New English texts show in English until they're translated.

## Ground rules

- Use only games you own. Contributions that help bypass DRM, licence or entitlement checks, or that add game files,
  are not accepted.
- Never commit personal data (IP addresses, user names, home paths, Steam IDs).
- By contributing you agree to license your work under GPL-3.0-only.
