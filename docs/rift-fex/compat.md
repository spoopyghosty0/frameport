# Rift through FEX: compatibility stream C

Compatibility is static evidence until a headset test verifies a build. First Contact remains the only verified
profile. No game entitlement, Platform SDK or Oculus signature checks are changed. A Meta account without ownership
must receive **Missing entitlement**; this is an informational result, not a bug to work around.

## Implementation and profile schema

`agent/fexrift_compat.py` registers a `GAME_INFO_HOOKS` handler, launcher environment handler and install-check
handler. `game_info()` obtains the exe and launch arguments from Meta's manifest, then applies a matching profile.
The helper reports `verified: false` for likely/untested builds. Likely profiles install with a warning (also in
`checks.profile_warnings`, which the parent agent forwards); untested builds need `force_unverified`; blocked
profiles are refused even with force. `checks` records the selected profile, status, route, cache eligibility and
32-bit warning. No agent version bump is included.

`agent/fexrift_games.json` is data, not executable configuration. Its `_comment` describes these fields:

| Field | Meaning |
|---|---|
| `match.canonical_names` | List of exact Meta canonical app names; no fuzzy title matching |
| `match.sha256` | List of exact executable SHA-256 strings |
| `match.executables` | List of `{name, size}` objects: case-insensitive basename **and** exact byte size must match |
| `status` | `verified`, `likely`, `untested`, `blocked` |
| `graphics` | `d3d11` (default), `d3d9`, `d3d12`, `opengl`, `vulkan` |
| `env` | String environment values |
| `ime_patch` | Optional fallback `{rva, old, new}`, with integer/hex RVA; null means no fallback |
| `fp_mem` | `off`, `candidate` (informational, no preload), `enabled` (verified per-game opt-in) |
| `bigcache_mb` | Cache size when enabled, default 256 |
| `dxvk` | List of dxvk.conf lines; launcher writes `$A/dxvk.conf` and exports `DXVK_CONFIG_FILE` |
| `args` | Optional argument list override; omit to preserve manifest arguments |
| `notes` | Evidence and first-launch cautions |
| `revive` | Optional, defaults true; false launches an OpenVR-only game directly |

Match alternatives are OR, while basename and size within an executable match are AND. Multiple matching profiles
are refused. With no `match`, the profile key is the exact canonical name (legacy First Contact compatibility).
The surveyed profiles use basename+size because the owner's folders do not establish authoritative Meta canonical
names; their keys identify profiles, **not** presumed Meta app IDs. They preserve the manifest's exe and arguments.
A different store build/size needs a new survey or an explicit untested install; do not guess its identity.

A SHA-only identity should be rechecked against the original executable after any game-byte patch: patching changes
its hash. Prefer canonical names or basename+size for profiles that need the IME fix. Size matches choose launch
settings only; the anchored finder independently verifies the bytes before patching.

`fp_mem: candidate` is set only when the surveyed lower bound of the UE version is >=4.18 (Binned2 big-block path).
It never enables the unzeroed cache. Every other game needs evidence that it fully overwrites reused memory before
an opt-in. First Contact's existing explicit environment/cache settings remain enabled.

## Older Unreal: Robo Recall

The premise that Robo Recall lacks `Slate.DeferWindowsMessageProcessing` was a search-location error. Its modular
`RoboRecall-Core-Win64-Shipping.dll` contains the UTF-16 CVar at file offset `0x1e1bb0`; the existing anchored finder
returns RVA `0x15ffe5`, old `c7860002000001000000`, new `c7860002000000000000`. This changes the constructor's
`mov dword ptr [rsi+0x200],1` immediate to zero, with the existing expected-byte guard and `.orig` backup.

Older Unreal placed `FWindowsApplication` in Runtime/Core; newer trees split it into Runtime/ApplicationCore.
`bAllowedToDeferMessageProcessing` and `GetDeferMessages` control the same Windows message deferral path, so an old
engine is not exempt from the IME deadlock merely because ApplicationCore/Slate lack the marker. The extension
extends `find_ime_target()` to search `*-Core-Win64-Shipping.dll` (and Win32/Core.dll forms), using the unchanged
CVar/xref/storage finder. It never patches a guessed member offset. Synthetic tests cover successful Core discovery,
repeat installs, and a CVar name without an anchored reference. No Unreal source checkout was available offline;
the observed binary is the evidence for this build, rather than an inferred engine-version exemption.

## Graphics and remaining device questions

DXVK handles D3D9/11; D3D12 uses GE's own vkd3d-proton selection. The launcher clears inherited
`PROTON_USE_WINED3D`, `PROTON_NO_D3D12` and `PROTON_NO_D3D11`, keeps `FP_VKTHUNK=1`, and lets Proton select/copy its
shipped DLLs. It makes no renderer DLL edits in a game. `checks.vkd3d_dlls` reports installed d3d12 DLLs under
`files/{lib,lib64}/wine/vkd3d-proton/{,x86_64-windows/,i386-windows/}`. These are discovery candidates, not a hardcoded
installation prerequisite. GE-Proton's script/archive was not available locally, and network/device access was
prohibited: verify the exact 11-7 layout and its script logic on the device before claiming D3D12 support.

The `opengl` profile exports `PROTON_USE_WINED3D=1`. A native OpenGL game still calls OpenGL directly; this flag
selects Wine's GL renderer for any D3D calls. There is **no configured FEX GL thunk**. Host acceleration would need
FEX's matching guest libGL/libEGL thunk libraries, host counterparts and enabled GL thunk configuration, with a
working display/GLX/EGL bridge. Guest rootfs Mesa might instead use software rendering or Zink through Vulkan;
inspect the actual renderer before attributing a game failure to Wine or promising hardware acceleration.
The Climb prefers its imported D3D9 route; imports alone do not prove which renderer its config selects.

A 32-bit exe is flagged in the survey and install checks. GE WoW64 under FEX still needs working 32-bit graphics
and SteamVR's 32-bit vrclient bridge; it is not promoted to likely based on engine alone. XAudio2 and Bink are facts
to watch; Media Foundation is flagged for a real movie-playback check. Anti-cheat detected by filenames/imports
makes a generated profile blocked. Absence of a static marker is not proof that runtime-loaded middleware is absent.

## Reproduce the offline survey

Use an isolated `FRAMEPORT_HOME`; never set `FRAMEPORT_WINDOWS_DATA`. No program in a dump is executed, no archive
is extracted, and no file in a game folder is written. The report contains local paths and must stay uncommitted.

```bash
export FRAMEPORT_HOME="$PWD/.home"
uv run python - <<'PY'
import subprocess, sys
from pathlib import Path
root = Path('/mnt/d/Backup Data/Misc/emus/VR_META_QUEST/ARMGDDN_PCVR_OCULUS')
cmd = [sys.executable, 'scripts/rift_fex_survey.py', '--out', '.home/survey']
for folder in sorted(root.iterdir()):
    if folder.is_dir():
        cmd += ['--folder', str(folder)]
subprocess.run(cmd, check=True)
PY
```

Outputs: `.home/survey/survey.json`, `survey.md`, and `profiles.json` (ready to paste, but review argument overrides
and direct OpenVR routing). The Unreal pak/ini reader is deliberately left in place for stream B's extraction.

## Owner's library: static results

All 19 selected game exes are x86_64; no selected 32-bit client was found. Eighteen use the Platform SDK; Rick and
Morty is the exception. The table contains **10** likely titles (including Robo Recall and all three Vader episodes),
plus nine untested titles. Profile IDs below are data keys matched by exe basename+size, not canonical app names.
`candidate` means cache eligibility only; it is off at launch. No surveyed game is promoted to verified.

| Game / profile ID | Route | Status | Platform SDK | fp_mem | Watch at first launch |
|---|---|---|---|---|---|
| Arktika 1 / `arktika-1` | d3d11 trial; API undetected | untested | yes | off | Establish actual renderer and custom-engine startup; do not infer D3D11 from this default |
| Asgard's Wrath / `asgards-wrath` | d3d11 | likely | yes | candidate | Large-block stalls/memory, Media Foundation movies, entitlement |
| Brass Tactics / `brass-tactics` | d3d11 | likely | yes | off | Media Foundation movies, tracking and IME startup |
| Chronos / `chronos` | d3d11 | likely | yes | off | Older UE D3D11 path, movies and controller input |
| Defector / `defector` | d3d11 | untested | yes | off | Custom-engine startup and Bink cutscenes |
| Hubris / `hubris` | d3d11 | likely | yes | candidate | XAudio2 sound, Media Foundation movies, shader/GPU cost |
| Lies Beneath / `lies-beneath` | d3d11 | likely | yes | candidate | Platform SDK delay load/entitlement; IME fix and movies |
| Lone Echo / `lone-echo` | d3d11 (also imports d3d9) | untested | yes | off | Runtime tracks client, entitlement; custom-engine startup and allocator stalls |
| Lone Echo II / `lone-echo-2` | d3d12 | untested | yes | off | vkd3d-proton DLL layout and Vulkan device creation on Turnip |
| Rick and Morty / `rick-and-morty-virtual-rick-ality` | opengl; direct OpenVR | untested | **no** | off | Actual GL renderer/acceleration; SteamVR bridge; speech WinRT; never inject Revive |
| Robo Recall / `robo-recall` | d3d11 | likely | yes | off | Core DLL IME fix at `0x15ffe5`; Meta entitlement and tracking |
| Skydance's Behemoth / `skydances-behemoth` | d3d11 | likely | yes | candidate | XAudio2 sound, Media Foundation movies and heavy GPU/memory use |
| Stormland / `stormland` | d3d11 | untested | yes | off | Custom engine, Media Foundation and Bink cutscenes |
| The Climb / `the-climb` | d3d9 preferred; also imports GL | untested | yes | off | Confirm CryEngine selects D3D9 and DXVK; GL import alone is not a selected renderer |
| The Unspoken / `the-unspoken` | d3d11 | untested | yes | off | Custom engine, Media Foundation and Bink cutscenes |
| Vader Immortal I / `vader-immortal-episode-i` | d3d11 | likely | yes | off (UE range starts at 4.16) | Movies, controller input, startup IME; leave MSAA at its game default |
| Vader Immortal II / `vader-immortal-episode-ii` | d3d11 | likely | yes | off (UE range starts at 4.16) | Movies, controller input, startup IME; leave MSAA at its game default |
| Vader Immortal III / `vader-immortal-episode-iii` | d3d11 | likely | yes | off (UE range starts at 4.16) | Movies, controller input, startup IME; leave MSAA at its game default |
| Wilson's Heart / `wilsons-heart` | d3d11 | untested | yes | off | Custom-engine startup and Bink cutscenes |

## Exact maintainer device checks (not executed here)

Only run one FEX game at a time: they share the Wine prefix and wineserver. The following uses the existing CLI;
stream A/B may add a direct login/install path at integration. Set `FRAMEPORT_HOME` to the development data folder
with the paired Frame configuration, then keep using that folder. The example `.home` is isolated from normal app
data and needs a paired Frame first. Never set `FRAMEPORT_WINDOWS_DATA`.

1. On the development PC, upload the integrated helper/extensions/profiles and verify setup:

   ```bash
   export FRAMEPORT_HOME="$PWD/.home"
   export FRAMEPORT_EXPERIMENTAL_FEX=1
   uv run frameport fex setup
   uv run frameport fex status
   ```

   Wait for setup to finish. Expect `proton`, `prefix` and `fex` true; `apps` lists Meta canonical names. No token or
   sign-in URL is needed in the output. If importing from an already signed-in PC Wine prefix, use the exact app
   name from its Meta manifest filename (replace both example values):

   ```bash
   app='actual-meta-canonical-name'
   prefix='/path/to/signed-in/wine-prefix'
   uv run frameport fex import-login "$prefix" --app "$app"
   uv run frameport fex install "$app"
   ```

   For a matching likely profile, expect `verified: false`, `checks.profile_status: likely`, a profile ID and
   `checks.profile_warnings`; no `--force-unverified` should be needed. If the downloaded build differs from the
   surveyed basename+size, expect refusal and review its own survey before forcing. An explicitly blocked profile
   must still refuse with `--force-unverified`. An untested game needs that switch. The PC reporter also warns
   that the game has not been verified on the Frame.

2. On the Frame, wear the headset once after boot and wait for tracking. Launch from the Steam shortcut. For these
   checks in a Frame terminal, replace `app` with the same canonical name:

   ```bash
   app='actual-meta-canonical-name'
   pkg="rift.${app//-/_}"
   anchor="$HOME/Applications/quest-frame/$pkg"
   python3 - "$anchor/deployment.json" <<'PY'
   import json, sys
   d = json.load(open(sys.argv[1]))
   for key in ('profile', 'profile_status', 'profile_warnings', 'graphics', 'fp_mem', 'exe_32bit', 'wow64', 'vkd3d_dlls'):
       if key in d.get('checks', {}):
           print(key, d['checks'][key])
   PY
   rg 'FramePort FEX: graphics=' "$anchor/launch.log"
   rg 'PROTON_USE_WINED3D|PROTON_NO_D3D12|DXVK_CONFIG_FILE|export FP_MEM' "$anchor/launch.sh"
   ```

   Expect the profile route. Candidate caches must have no `export FP_MEM=1`. For Rick and Morty also run
   `rg 'taskset.*proton' "$anchor/launch.sh"`: the command must invoke the game directly with no ReviveInjector.
   A graphics route marker proves selection of launcher settings, not successful rendering. Check both eyes,
   tracking/controllers, sound and a movie in the headset before changing a profile to verified.

3. For Robo Recall, `checks.ime.file` should name `RoboRecall-Core-Win64-Shipping.dll` and `fixes.ime_patch` should be
   `patched` on first install and `already` on repeat. On the Frame, verify the original backup exists and the
   constructor immediate is zero (only the installed copy is read):

   ```bash
   python3 - "$anchor/deployment.json" <<'PY'
   import importlib.util, json, os, sys
   d = json.load(open(sys.argv[1]))
   path = os.path.join(os.path.dirname(d['exe']), d['checks']['ime']['file'])
   helper = os.path.expanduser('~/.local/share/frameport/fexrift/artifacts/fexrift.py')
   spec = importlib.util.spec_from_file_location('fx', helper)
   fx = importlib.util.module_from_spec(spec)
   spec.loader.exec_module(fx)
   b = open(path, 'rb').read()
   p, reason = fx.find_ime_patch(b)
   assert p and b[fx.pe_offset(b, p['rva']):fx.pe_offset(b, p['rva']) + len(bytes.fromhex(p['new']))] == bytes.fromhex(p['new']), reason
   assert os.path.isfile(path + '.orig')
   print('Core IME default zero; original backup present')
   PY
   ```

4. For Lone Echo II (untested), install with `--force-unverified`. In a Frame terminal inspect only GE's files and
   script; no network fetch is needed:

   ```bash
   r="$HOME/.local/share/frameport/fexrift"
   rg --files "$r/ge/files" | rg '/vkd3d-proton/.*/?d3d12(core)?\.dll$|/vkd3d-proton/d3d12(core)?\.dll$'
   rg -n 'PROTON_USE_WINED3D|PROTON_NO_D3D12|vkd3d-proton|dxvk' "$r/ge/proton"
   ```

   Expect bundled vkd3d-proton and script selection when wined3d/no-D3D12 are unset. `checks.vkd3d_dlls` should name
   the installed layout. A vkd3d initialization failure is triaged `fex-vkd3d-init`; a successful Vulkan device plus
   frames in both eyes is the first actual evidence of this route. If the file layout differs, update discovery
   candidates without copying DLLs into the game. For GL, inspect the active **guest** renderer; an installed host
   Mesa library or the Vulkan thunk does not establish guest GL acceleration.

5. Exercise each triage signature offline on the PC with
   `uv run pytest -q tests/test_fexrift_compat.py -k triage`. On-device expected strings are `Missing entitlement`,
   `OVRServer ... install_available`, SteamVR's IMU-fallback message, vkd3d/wined3d initialization failure, or a
   ReviveInjector failure. Do not dump entire Meta log files: they may carry sign-in data. A Librarian completion
   line alone must not diagnose a hang. After completion, confirm persistence using `pgrep -f '[O]VRLibrarian.exe'`;
   only if it remains alive, record the safe diagnostic `OVRLibrarian still running` alongside the completion
   marker. The launcher now emits the fixed combined marker itself if Librarian persists after cleanup and
   completion is present; it never mirrors the log. The combined trace matches `fex-librarian-stuck`. Stop only that process after completion with
   `pkill -9 -f '[O]VRLibrarian.exe'`; the normal launcher already does this. For an unowned title, accept Meta's
   Missing entitlement result and stop that game test.

Undo: exit the game in Steam before touching files. Remove the selected profile from the device copy of
`artifacts/fexrift_games.json` and rerun install with `--force-unverified` to regenerate the default D3D11/Revive
launcher. For an OpenVR-only game, retain `revive: false` in an untested profile when comparing graphics routes.
Remove the anchor's `dxvk.conf` when no longer used. To undo the IME fix, restore **only the installed binary**
from its `.orig` copy after stopping the game; for example, on the Frame:

```bash
python3 - "$anchor/deployment.json" <<'PY'
import json, os, shutil, sys
record = json.load(open(sys.argv[1]))
path = os.path.join(os.path.dirname(record['exe']), record['checks']['ime']['file'])
shutil.copy2(path + '.orig', path)
print('Installed IME binary restored')
PY
```

Do not delete the shared prefix to undo a profile: it holds Meta's runtime and the owner's login/saves. Game dumps
are never modified. Re-running install reapplies the anchored IME fix; to compare launches, restore after install
and before Play.

## Validation in this worktree

- Offline survey completed for all 19 folders with no per-game errors. Every surveyed executable matched exactly
  its intended committed profile through the compatibility hook. Ten profiles are likely, nine untested; the
  separate First Contact profile stays verified. No selected or alternate candidate exe was 32-bit. No static
  anti-cheat marker was found; this does not rule out runtime-loaded middleware.
- Focused FEX/survey/triage tests: **96 passed, 1 skipped**. Ruff passed for the new extension, survey and their tests.
- `uv run python scripts/patch_docs.py` was run; `--check` passes and generated patch docs needed no committed change.
- Final whole-suite run: **1,646 passed, 43 skipped, 11 failed, 16 setup errors**. All 27 unsuccessful cases failed
  at local `socket()` creation with `PermissionError: [Errno 1] Operation not permitted`. They are existing local
  HTTP/pairing/stream tests in `test_deeplink`, `test_devkit`, `test_install_links_extra`, `test_livestream`,
  `test_setup_url`, and `test_ui_state`. They need a runner allowing loopback sockets; no application or test code
  was changed to hide this restriction. Re-run the full suite there before integration.
- The prebuilt virtual environment was used with `UV_CACHE_DIR=/tmp/frameport-rift-uv UV_NO_SYNC=1` because the default
  uv cache is read-only and offline metadata resolution cannot install hatchling. For example:
  `UV_CACHE_DIR=/tmp/frameport-rift-uv UV_NO_SYNC=1 uv run pytest -q`.
- No device or external network access was attempted. D3D12, guest OpenGL acceleration, WoW64 and gameplay remain
  device questions; the steps above specify the evidence needed. The GE script and Unreal source-history checkout
  were unavailable locally; Robo Recall's actual Core DLL supplies direct evidence for the IME fix instead.
