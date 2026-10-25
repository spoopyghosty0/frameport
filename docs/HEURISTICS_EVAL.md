# Evaluating patch heuristics

`scripts/eval_heuristics.py` compares `engine.suggest(analysis, use_catalog=False)` with the bundled
`catalog/games/*.yaml` entries. It does not merge personal or remote catalog recipes into the benchmark.
It supports the original dumps command, or a precomputed `scripts/heuristics_snapshot.py` snapshot:

```sh
.venv/bin/python scripts/eval_heuristics.py '<dumps folder>' --verbose
.venv/bin/python scripts/eval_heuristics.py --snapshot analyses.json --split all --json baseline.json
.venv/bin/python scripts/eval_heuristics.py --snapshot analyses.json --fit-report --out fit-report.md
```

Use a scratch `FRAMEPORT_HOME`, `FRAMEPORT_NO_CATALOG_UPDATE=1`, and an empty `FRAMEPORT_WINDOWS_DATA` for offline
measurement. Snapshot mode reads no APKs or game data. Each snapshot read copies the source to a temporary file,
then parses the copy, retrying incomplete JSON. To compare multiple splits against an actively growing snapshot,
freeze one valid copy first and use that file for every run.

The default split is `dev`. `split_packages({package: analysis_engine})` sorts packages by SHA-256 within Unity,
Unreal, and Other strata, then holds out the nearest 20% (half rounded up). Pass only packages with both an analysis
and a catalog entry. The same input set always yields the same split regardless of iteration order; adding packages
can move the cutoff. Very small strata can have no holdout. Unsupported entries participate in splitting.

## What is scored

Ground truth contains catalog choices and patch registry defaults, with no inherited heuristic adapter settings.
Default expectations are determined per analysis, using each default patch's own detection in the same way as the
engine's initial heuristic pass. Thus a conditional default that is not detected (for example LTW on a game without LTW)
is not expected. Explicit catalog `overport_remove` and `frame_remove` entries remove expected defaults. An absent
expected default is represented as `remove:<patch id>`; enabled defaults do not inflate patch scores.

Opt-in overport/frame/device/PC VR IDs, default removals, adapter values, device-file names, foveation parameters,
and alternate-build patch IDs are compared. Alternate-build identities use `alt:<patch id>` so choosing one in a
fallback is distinct from choosing it in the primary build. Device-file content is not compared. Adapter values
must match: a wrong value contributes both an FP and an FN for `adapter.<key>`. Patch precision, recall and F1 are
micro-averaged across games; an empty denominator is reported as zero. Exact-game accuracy also compares the
unsupported status flag. Fields report matching games, including empty-field matches.

`RUNTIME_ONLY` lists choices excluded from the headline: `use_alt`, selected runtime adapter settings, Lepton
environment variables, and config-sync settings. They have their own exact, field and patch metrics. Catalog
entries with status `unsupported` are excluded from both headline and runtime supported-game metrics, and have
separate metrics and per-game diffs. Converted analyses are included and marked. JSON contains all metrics per
engine, all game signatures, split assignments, and missing/extra decisions.

## Dated score: catalog gaps vs. detection errors

The strict score treats the catalog as complete, but older entries were verified before many patches existed, and
a catalog recipe inherits every heuristic adapter setting and device patch (except `device.text_input_window`)
without listing them (`engine.suggest`), so the verified build had those. The dated score (`headline_dated`, printed
under "Dated") sets aside, on both sides:

- patches first committed after the entry's `verified.date` (the verifier couldn't have chosen them), and
- inherited adapter/device picks (`inherited_decisions`).

What remains are real disagreements: an extra on an entry verified after the patch existed, or a catalog choice
the heuristics don't make. Gaps are counted per patch ("Catalog gaps by patch"). The dated score needs the intro
date cache that `--fit-report` writes; without it only inherited picks are set aside.

Before tuning a rule to remove an "extra", check which kind it is: a gap on an old entry is a candidate for the
catalog (fit report), not a detection error. Before adding a patch to a working entry, weigh its benefit (does it
fix something the game hits?) against a rebuild prompt for everyone who installed it; adapter settings never need
adding (inherited). Results 2026-10-15: strict 5/65 exact, F1 73.8%; dated 39/65, F1 88.6% (dev 90.5%, holdout
79.5%). Remaining misses are one-game fixes found at runtime (for example Vader's gl_error_clear, BlazeRush's GL settings).

An evaluation writes JSON even when its headline has differences, then exits 1 for compatibility with the original
script. It exits 0 when exact, or after producing a fit report.

## New patch fit

The fit report covers all catalog entries, independently of `--split`. Candidates come from the final heuristic
recipe, preserving platform, flat-app, dependency, and 32-bit normalization rules. A candidate must also be opt-in,
applicable and recommended by its own `detect()`. Entries already listing or explicitly removing the patch are
excluded; a listed adapter key is excluded regardless of its value. This report proposes additions, not value edits.

Each candidate includes its reason, experimental/strict flags, first local git introduction date, catalog entry
date (`verified.date`; `updated` only without one, since it also moves for unrelated edits), and whether the
patch is newer. Unknown dates are marked unknown. Warnings
come from the full catalog recipe plus that one candidate; they may include pre-existing conflicts. Missing-analysis
entries appear as metadata-only rows. Intro dates, including unknown results, are cached in
`patch-intro-dates.json` beside the snapshot (or dumps folder); subsequent runs only query uncached patch IDs.

`tests/test_eval_heuristics.py` uses synthetic data. The opt-in real-dump test in `tests/test_games.py` ratchets the
supported all-split exact ratio and patch F1 against measured, rounded-down baseline constants. Machine-local
`.eval-home/` and `.eval-results/` scratch outputs are ignored by git.
