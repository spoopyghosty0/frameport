# Diagnostics and problem reports

FramePort sends results back in two ways, both without a GitHub account token:

- **Report a problem…** / **Collect logs** (game menu → Troubleshoot, Settings, the failure pop-up,
  `frameport diag report|collect`) writes a zip of logs with personal data removed ([Redaction](#redaction)) and
  opens a prefilled issue (`bug-report.yml`). You drag the zip into the issue: GitHub has no API for attachments.
  The zip stays under GitHub's 25 MB limit and holds no game files.
- **Share working recipe…** (game menu → Recipe; `frameport share-recipe <pkg>`) saves the recipe as your own catalog
  entry and opens a prefilled issue (`working-config.yml`).

Prefilled links are capped at about 7.5k characters; free text is trimmed first, the recipe never.

**For maintainers:** labelling a recipe issue `catalog-accepted` runs `.github/workflows/catalog-from-issue.yml`:
`scripts/catalog_from_issue.py` reads only the YAML block, validates it, writes `catalog/games/<pkg>.yaml` and the
workflow opens a PR. The workflow needs Settings → Actions → "Allow GitHub Actions to create and approve pull requests" and the
labels `working-config`, `catalog-accepted` and `bug`.

Code: `src/frameport/diag/` (`redact.py`, `bundle.py`, `issue.py`), agent command `collect_diag`,
`Target.collect_diag`, `core/applog.py` (app log + saved job logs).

## Bundle layout (schema 1)
```
README.md                    what's where (for whoever opens it)
manifest.json                versions (app, agent, tools, SteamOS build, Proton), games, warnings, redaction counts
app/app.log                  the app's log (<data>/logs/app.log, rotated 2 MB × 3)
app/jobs/*.log               saved GUI job logs (<data>/logs/jobs, last 30): build, install, test, ...
app/settings.json            library settings, catalog sources
app/display.json             Flet/Flutter versions and last window state; on native Windows, display layout,
                             resolution, refresh rate, GPU driver metadata and up to 60 recent display/device
                             System event records (last 30 minutes; timestamps, providers and IDs only)
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
                             stop-pause.log, exit-hook.log, theater.log, dashboard.log, plays.log: how the last
                             session ended (pause before the stop, Steam's Exit game hook; play start/end)
                             (game-log-N.txt: Unreal Saved/Logs + crash summaries, Unity Player.log / Player-prev.log /
                             output_log.txt + crash error.log from the Proton prefix)
                             threads.txt: saved while a Quest game runs (for example a hang): each thread's
                             state, CPU ms over 2 s, kernel wait and syscall, plus its TCP sockets (remote = loopback /
                             local / internet + port, no addresses); read once from /proc, the game isn't paused
games/<pkg>/target/shaders/  shader dumps, when the game wrote any:
    fp_vk_shaders/           adapter vk_shader_dump=1 (Vulkan shim): <size>_<sha256>.spv + index.txt
    fp_spirv/                adapter zink_shader_dump=1 (OpenGL ES, shader-fix layer under Zink): <size>_<sha256>.spv
```
Each log keeps at most its last 4 MB (2 MB per file from the Frame). Shader dumps, SPIR-V unchanged (compiled game
shaders, not redacted): Vulkan: every module the game's newest run (the last `# start` in `index.txt`)
created, last used first, each up to 4 MB and up to 30 MB in all, with that run's index lines, so a module created
well before a GPU hang comes along too (if the zip would pass 24 MB, the least recently used ones are left out
after the logs were cut); OpenGL ES (no index): the newest modules (by time written) up to 4 MB.
`index.txt` (Vulkan) lists every vkCreateShaderModule as
`<seq> <ms since the first module> <unix ms> <size>_<sha256>.spv new|known|again|failed`; match the unix time with
the kernel's `hangcheck detected gpu lockup` line (this-boot-kernel.txt). All modules stay in the game's storage on
the Frame (`Android/data/<pkg>/files/fp_vk_shaders/`, Files tab → the game's storage).

Display queries run only while collecting logs (the Windows event query times out after 5 s and says whether the query
found nothing or failed). `app.log` records window visibility, focus and move/resize events with UTC times, to
compare with Windows events. No display polling, no change to display settings. Empty System events don't rule out
a display problem.

## Redaction
`diag/redact.py` runs on every file and on the issue text.
- **Exact values:** the data dir → `<data>`, home dirs → `<home>`, user names → `<user>`, PC and Frame host names and
  saved Frame addresses → `<host>`, Steam account ids from the Frame's `info` and the PC's Steam → `<steam-id>`, the
  folders holding the user's game dumps → `<source>`.
- **Patterns:** IPv4 addresses (first octet ≥ 10, so version numbers survive), IPv6, MAC, SteamID64, `userdata/<id>`,
  email addresses (file names like `x@123.txt` excluded), and any `/home/<u>`, `/Users/<u>`, `C:\Users\<u>` or
  `/mnt/c/Users/<u>`.
- **Kept:** generic accounts that identify no one: `steamos`, `steamuser` (Proton), `root`, `deck`.

## Debugging from a bundle (no game, no Frame, no GUI)
1. `frameport diag inspect <zip>` (`--json` for everything). The command prints the versions and warnings, the last launch
   test, and a fresh triage of the newest launch log with the current `catalog/triage.yaml`.
2. Match the findings and log lines against `docs/PLAYBOOK.md` (symptom → cause → fix) and `docs/FRAME_RUNTIME.md`.
3. Check the analysis in `entry.json` (engine, XR API, `extra`: missing ovr symbols, features, Unreal version) and
   `package/elf.json` against the patches' heuristics (each patch's `detect()`; [HEURISTICS_EVAL.md](HEURISTICS_EVAL.md)). Compare `recipe.yaml` with catalog games that use the same
   engine and API.
4. Compare `manifest.json → env.frame.build_id` with the build the docs describe: SteamOS updates change the runtime.
5. A new failure signature means adding a patch module, a `triage.yaml` signature, a PLAYBOOK row and a unit test (see
   [CONTRIBUTING.md](../CONTRIBUTING.md)). Then `diag inspect` on the same zip should report the new signature.
