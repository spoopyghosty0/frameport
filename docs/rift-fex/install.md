# Stream B: unchanged local Rift copies and Meta library registration

Implemented on `rift/install`. Device validation is pending: this workspace cannot reach the Frame or create local
network sockets. No entitlement, Platform SDK, runtime signature check, sign-in data or repack crack is changed by
this work. A Meta **Missing entitlement** response remains a failure to report to the owner.

## Implementation

`frameport fex install --from-library rift.<slug>` takes the Rift entry's `game_dir`, selected `exe`, title and
`launch_args` (also supported in `analysis.extra`). `--folder DIR [--title T]` uses the existing Rift analyzer to pick
the exe; ambiguous choices must first be resolved in the library. Optional positional `APP` overrides the generated
canonical name. Otherwise use the analysis's `canonical_name`, then a lowercase title slug. Local unverified games
require `--force-unverified` **before** any upload. Existing prefix-game installs still work without either option.

`src/frameport/fexinstall.py` stats the folder and reuses `installer.transfer_link()` and `upload_files()` unchanged:
fast link, SFTP `.part` append, tar batches, progress and Reporter cancellation all follow the existing install path.
Finished files are skipped by source size/mtime plus remote size; changed sources discard old completed/partial
bytes before retry. The source is statted again after transfer. This avoids hashing all source files over NTFS;
it cannot detect an edit that deliberately preserves both size and mtime. Do not edit the source during an install.
The free-space check reserves 1 GiB in addition to all pending bytes (conservative when `.part` files already exist).

Skipped, logged and recorded: case-insensitive `*.7z`, `*.7z.0*` split volumes (including backup suffixes), `Data.bin`,
`AGRepackInstaller.exe`, `VD.bat`, and `*.AG`. Nothing is extracted or executed on the PC. Every included file's bytes
are uploaded as supplied. Existing install-time generic fixes, including the Unreal IME fix, still run on the Frame
after registration; they may change the **Frame** executable. The source folder stays unchanged. Symlinks, path
escapes, duplicate Windows filenames and collisions with Meta-managed copies are refused.

App ID precedence:

1. `[OnlineSubsystemOculus]` in unencrypted pak `Config/DefaultEngine.ini`, then loose `DefaultEngine.ini`,
   `Oculus.ini` or `OculusConfig.ini`; keys `RiftAppId`, `OculusAppId`, `AppId` in that order. Also checks only the
   known JSON names `OculusConfig.json` / `oculus-platform-config.json` for `RiftAppId`, `OculusAppId`, `AppId`,
   `app_id`. IDs must be positive numbers. No binary-string guessing or archive decryption.
2. Already cached OculusDB data: `details.store_url` (`fetch_details()` retains the ID in this URL, not an explicit
   Rift-ID field). No lookup/network request is introduced. A Quest-linked details record can name its Quest store
   product: check this fallback manually before testing a game with no config ID.
3. `--app-id ID`, used only if no earlier source supplies one.

`app_id_source` records the selected origin. `store_app_id` independently retains the cached store ID when present,
so a game ID such as First Contact's `1184903171584429` is not lost to store product `1217155751659625`. Manifest
`appId` uses the selected ID; device tests must establish whether Meta instead requires the store product ID for
particular builds. The version comes from a version-like folder name, then the exe's PE `.rsrc` FileVersion, else
`0.0.0`; versionCode is 1. The shared reader moved to `analysis/unreal_pak.py`; the survey re-exports it via imports
so its existing callers and tests keep working. Stream C should keep these imports when merging its survey edits.

`agent/fexrift_install.py` registers `prepare_local`, `register_local`, `local_info`, `unregister_local` and
`runtime_state`. Reservations/metadata live in `fexrift/local-games/<app>.json`, with no login information. They also
allow interrupted pre-registration uploads to resume. Registration hashes each file **on the Frame**, writes
`<library>/Manifests/<app>.json` and `.json.mini`, and imports the library registry entry through Wine. The extension
supplies the library manifest to `manifest_launch` / `game_info`; the existing static checks, IME patch and launcher
then run normally. The install check hook marks `local_copy: true`.

Manifest JSON matches the supplied real First Contact app fixtures: property order (derived properties first),
types, CRLF, two-space indentation, backslash `files` keys, forward-slash `launchFile`, and mini containing exe
records only. App packageType `APP`, isCore false, launchFile2D null, launchParameters2D empty string,
redistributables empty; other defaults come from `MANIFEST_DEFAULTS`. Launch parameters use Windows argv quoting;
the launcher preserves the original argv list (a small shared launcher hook avoids splitting local-copy arguments
while retaining the existing profiles' shell-fragment parsing).

Each file has its actual size and whole-file SHA-256, `segmentSize: 10000000`, and one segment per **decimal 10 MB**
chunk. Each triple is `[0, chunk_sha256, len(zlib.compress(chunk))]`. The fixture's third number is compressed length,
not uncompressed size. We do not know Meta's opaque first value and use 0 as an explicit placeholder; no fabricated
CDN identifiers or download URLs are written. Empty files have zero segments. **These manifests describe local
bytes, not a working Meta CDN repair recipe. Whether Meta accepts the placeholder without scheduling repair is an
open device question.** Do not initiate an update/repair from Meta's app to validate this.

Internal games: `<PFX>/drive_c/Program Files/Meta Horizon/Software/Software/<app>`, using Meta's existing default
library registration. `--dest <mount>` must exactly match an available external mount returned by the existing
`drives` command; existing `resolve_dest()` rejects unusable filesystems. No fallback on a missing card.

External games: `<mount>/FramePort/fexrift-games/<app>`. Their Meta library root is the sibling
`<mount>/FramePort/fexrift-library`, containing `Manifests/` and a `Software` symlink to `fexrift-games/`. A stable
UUID5 from the mount path identifies the registry library. A prefix `dosdevices/volume{GUID}` links to the mount,
and the first unused D:–Y: letter links there too (an existing letter for the same mount is reused). Registry
`Path` uses `\\?\Volume{GUID}\FramePort\fexrift-library`, `OriginalPath` uses the letter. The default library stays
default. Mount-path changes require uninstall/reinstall; moving these games through the general Move action has
not been implemented by this stream. Wine volume discovery and external-library scans need device validation.

Deployment kind stays `frame_fex`, with `source: local_copy`, `app_id`, `app_id_source`, `store_app_id`, `skipped`,
`dest`, and the existing launcher/check fields. Local-copy uninstall removes its files and both manifests. The last
game on an external library also removes that library's registry key; the shared default key, shared prefix, saves,
login and reusable drive/volume links remain. Imported Meta-managed games still keep their files on uninstall.
There is no agent-version bump; integration must bump it so updated agent commands reach existing devices.

The only other shared-agent helper change sends helper JSON on stdin, avoiding Linux's single-argument size limit
for large inventories. The extension files are already included by `artifact_files()` and need no packaging change.

## Runtime probe

`--runtime-check` calls the separate bounded `fex_runtime_state` command after install (helper `runtime_state`). It
reuses an existing OVRServer or starts it with the shared prefix's Wine environment. It examines only newly appended
Service/OVRServer log text, returning `runtime_state: ready`, `install_available` or `timeout` plus `reused_server`.
It never returns raw runtime log lines. If it started the server, it kills OVRLibrarian after a fresh invocation
completion and stops its Wine server at exit; a reused runtime is left running. The polling budget is 95 seconds,
with bounded process start/probe/cleanup, and the agent imposes a 120-second limit. A reused idle runtime may emit no
new state line and return `timeout`; that result is inconclusive. The probe does not start the game or prove account
ownership. The actual log naming/state-line format remains a device question.

## Offline validation

Use a writable UV cache in this sandbox (the default cache is read-only):

```sh
UV_CACHE_DIR=/tmp/frameport-uv-cache uv run --offline --no-sync pytest -q
UV_CACHE_DIR=/tmp/frameport-uv-cache uv run --offline --no-sync pytest -q \
  tests/test_fex_install.py tests/test_fexrift.py tests/test_fexrift_ext.py tests/test_rift_fex_survey.py
```

The full run reached 1,654 passed, 43 skipped; 11 failures + 16 setup errors all tried to create network sockets
for existing localhost HTTP/streaming/pairing tests, which this sandbox forbids (`PermissionError: Operation not
permitted`). Rerun the whole suite without that sandbox restriction at integration. No socket tests were changed
to conceal this limitation. Triage/patch catalog files were not touched, so `patch_docs.py --check` is not required.

Final follow-up: 1,658 passed, 43 skipped, 27 deselected when excluding precisely those socket-dependent tests.
The final install/FEX/survey run (including an additional split-volume suffix case) passed 68 tests, 1 skipped.
Ruff passed for the new modules/tests and the survey; `git diff --check` passed.

## Exact maintainer device test

Run these only on the maintainer's connected PC. Keep `FRAMEPORT_HOME` pointing to this worktree's `.home`, with
its paired Frame target and Rift library entries. Do not point at the Windows FramePort data directory. Ensure the
maintainer's integration agent version includes this branch. Wear the headset once after boot; stop any running
FEX game before the runtime probe (all use one prefix).

```sh
export FRAMEPORT_HOME="$PWD/.home"
export FRAMEPORT_EXPERIMENTAL_FEX=1
uv run frameport fex setup
uv run frameport fex status
uv run frameport list
```

Setup uploads this extension and binaries; it may fetch the signed Meta runtime on the device. Use the login stream
to sign in with the owner's account first. `status` should show prefix/meta installed and `login: true` without
printing any session content. Do not copy session files, CoreData or sign-in URL queries into a report.

Pick a library ID the account owns; example (substitute the exact ID from `frameport list`):

```sh
uv run frameport fex install --from-library rift.robo_recall --force-unverified --runtime-check
```

Expect `Skipped repack leftover: ...` entries, `Meta app ID ... (config:...)` or the cached/CLI fallback, upload
progress, `Hash and register the game on the Frame`, then generic checks/Steam registration. Final JSON should
have `checks.local_copy: true`, with `checks.runtime_state: ready` if Meta emitted the state line. Check
`app_id_source` and both IDs in deployment with the filtered command below. `install_available` means registration
did not take; stop and report the IDs, canonical name and manifest shape. `timeout` requires inspecting the log
format **locally on the device**, keeping any account data private.

A folder install uses the same flow:

```sh
uv run frameport fex install --folder '/path/to/a/game copy' --title 'My Rift Game' \
  --app-id 1234567890123456 --force-unverified --runtime-check
```

Replace the example ID with the game's actual ID; the option is a fallback and does not override a config ID.
Do not try registering over First Contact already imported by the login tar: the collision refusal is deliberate.

Inspect deployment fields and launch using the paired connection (edit the package below once):

```sh
uv run python - <<'PY'
import json
from frameport.cli import _fex_target
frame = _fex_target(None).frame
pkg = 'rift.robo_recall'
try:
    games = frame.agent('list_installed', package=pkg, details=True)['games']
    for game in games:
        print(json.dumps({k: game.get(k) for k in
              ('package', 'kind', 'source', 'app_id', 'app_id_source', 'store_app_id', 'skipped', 'dest')}, indent=1))
    print(frame.agent('launch', package=pkg))
finally:
    frame.close()
PY
```

Expect `kind: frame_fex`, `source: local_copy`, IDs/provenance, and a Steam launch. In the headset check both eyes,
controllers, audio and reaching a scene. Look locally for runtime `state: ready`, launcher `FramePort: starting
Meta's runtime` / `FramePort: starting <title>`, and the game log. **Missing entitlement** is a valid ownership
failure to report, never a reason to modify the copy or checks. Robo Recall may lack the generic IME patch site;
report a hang without assuming the registration failed.

Resume: interrupt the upload with Ctrl-C, then repeat the identical install command. Expect completed files to be
skipped and big `.part` files to continue. Compare a few **non-patched** files' SHA-256 locally on source/Frame;
manifests should list actual sizes/hashes. No archive, installer, `.AG` file or `VD.bat` should have been uploaded.

External drive: first uninstall the test copy using the rollback below, then:

```sh
uv run frameport frame drives
uv run frameport fex install --from-library rift.robo_recall --force-unverified \
  --dest /run/media/steamos/REPLACE_WITH_MOUNT --runtime-check
```

Use an actual `usable: true` mount. Expect physical files in `FramePort/fexrift-games/<canonical>`, sibling
`fexrift-library/Manifests`, a `Software` symlink, a separate registry library, and `runtime_state: ready`. With the
card removed, reinstall/uninstall must fail explicitly rather than writing/deleting internal storage. Reinsert at
the same mount path before rollback. Repeat with two games to confirm uninstalling one leaves the second library
registered; removing the last should remove its registry key.

## Undo

For an installed local copy, end the game in Steam first, then run (substitute its package):

```sh
uv run python - <<'PY'
from frameport.cli import _fex_target
frame = _fex_target(None).frame
try:
    print(frame.agent('uninstall', package='rift.robo_recall', remove_shortcut=True, keep_data=True))
finally:
    frame.close()
PY
```

Expect `removed: true`; game folder, both manifests, deployment and shortcut disappear. Shared prefix, login, saves
and unrelated games remain. For a cancelled upload that never produced a deployment, remove only its reservation
and uploaded copy through the helper on the Frame (replace canonical/package; stop shared-prefix games first):

```sh
python3 -I ~/.local/share/frameport/fexrift/artifacts/fexrift.py unregister_local \
  '{"app":"robo-recall","package":"rift.robo_recall"}'
```

Reusable drive links and the empty external library scaffolding can remain for later installs; they contain no
game/login data. Do not delete the shared prefix to undo one local installation.
