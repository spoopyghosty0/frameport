# Diagnostics bundles and shared recipes

Two ways users feed results back, both without a GitHub token:

- **Share working recipe** (game menu, `frameport share-recipe <pkg>`): saves the recipe as a user catalog entry and
  opens a prefilled issue from `.github/ISSUE_TEMPLATE/working-config.yml`. A maintainer checks it and adds the label
  `catalog-accepted`. Then `.github/workflows/catalog-from-issue.yml` runs `scripts/catalog_from_issue.py`, which reads
  only the YAML block, validates it, and writes `catalog/games/<pkg>.yaml`, and the workflow opens a PR.
  - The PR step needs Settings → Actions → "Allow GitHub Actions to create and approve pull requests".
  - Create the labels `working-config`, `catalog-accepted` and `bug` once.
- **Report a problem / Collect logs** (game menu, Settings, the failure pop-up, `frameport diag report|collect`): writes
  a redacted zip and opens a prefilled `bug-report.yml` issue.
  - GitHub has no API for issue attachments, so the user drags the zip in (25 MB max; the bundle stays under 24 MB).
  - Prefilled links are capped at about 7.5k characters. Free text is trimmed first; the recipe is never cut.

Code: `src/frameport/diag/` (`redact.py`, `bundle.py`, `issue.py`), agent command `collect_diag` (agent ≥ 21),
`Target.collect_diag`, `core/applog.py` (app log + saved job logs).

## Bundle layout (schema 1)
```
README.md                    what's where (for whoever opens it)
manifest.json                versions (app, agent, tools, SteamOS build, Proton), games, warnings, redaction counts
app/app.log                  the app's log (<data>/logs/app.log, rotated 2 MB × 3)
app/jobs/*.log               saved GUI job logs (<data>/logs/jobs, last 30): build, install, test, ...
app/settings.json            library settings, catalog sources
frame/info.json              agent `info` (SteamOS build, Lepton, Proton, installed games, kernel keys)
frame/host.json              OpenXR runtime + explicit layers, podman containers, containers.conf, uptime
frame/XRService-*.log        the Frame's OpenXR runtime (SteamVR XRService) log, newest
games/<pkg>/entry.json       library entry: analysis, recipe, build checks, installs, last_test (store text dropped)
games/<pkg>/recipe.yaml      the recipe in catalog form;  catalog-<origin>.yaml = the catalog entry it came from
games/<pkg>/triage.json      newest local launch log re-triaged with the bundling app's signatures
games/<pkg>/logs/*.log       launch-test logs kept on the PC (last 5)
games/<pkg>/package/         stand-in for the game files (no content):
    apk_files.json           every APK entry + size
    AndroidManifest.xml      decoded manifest
    elf.json                 per lib/*.so: DT_NEEDED, ovr*/xr*/vrapi_/gl*/JNI exports + imports
    files.json, pe.json      Rift: file list; PE imports / delay imports of the exe and VR DLLs
games/<pkg>/target/          from the Frame (or the PC): launch.sh, settings.conf, deployment.json, launch.log,
                             launch-test.log (PC VR), lepton-steamlaunch-<appid>.log, logcat-{main,crash,system,...}.log,
                             ReviveInjector.txt / steam-<appid>.log / game-log-N.txt (PC VR), files.json (+ missing)
```
Each log keeps at most its last 4 MB (2 MB per file from the Frame).

## Redaction
`diag/redact.py` runs on every file and on the issue text.
- **Exact values:** the data dir → `<data>`, home dirs → `<home>`, user names → `<user>`, PC and Frame host names and
  saved Frame addresses → `<host>`, Steam account ids from the Frame's `info` and the PC's Steam → `<steam-id>`, the
  folders holding the user's game dumps → `<source>`.
- **Patterns:** IPv4 addresses (first octet ≥ 10, so version numbers survive), IPv6, MAC, SteamID64, `userdata/<id>`,
  e-mail addresses (file names like `x@123.txt` excluded), and any `/home/<u>`, `/Users/<u>`, `C:\Users\<u>` or
  `/mnt/c/Users/<u>`.
- **Kept:** generic accounts that identify no one: `steamos`, `steamuser` (Proton), `root`, `deck`.

## Debugging from a bundle (no game, no Frame, no GUI)
1. `frameport diag inspect <zip>` (`--json` for everything). It prints the versions and warnings, the last launch
   test, and a fresh triage of the newest launch log with the current `catalog/triage.yaml`.
2. Match the findings and log lines against `docs/PLAYBOOK.md` (symptom → fix) and `docs/FRAME_RUNTIME.md`.
3. Check the analysis in `entry.json` (engine, XR API, `extra`: missing ovr symbols, features, Unreal version) and
   `package/elf.json` against the heuristics in CLAUDE.md. Compare `recipe.yaml` with catalog games that use the same
   engine and API.
4. Compare `manifest.json → env.frame.build_id` with the build the docs describe: SteamOS updates change the runtime.
5. A new failure signature means adding a patch module, a `triage.yaml` signature, a PLAYBOOK row and a unit test (see
   Conventions). Then `diag inspect` on the same zip should report it.
